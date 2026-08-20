"""Background worker with restart recovery and optional remote JSON snapshots."""
from __future__ import annotations
import threading, time, traceback, os
from eventlog.log import EventLog
from core.orchestrator import Book
from service.engine import build_engine
from service.state import AppState

class Worker:
    LIVE_PAPER_DOMAIN = "live_public_v1"

    def __init__(self, state: AppState, eventlog: EventLog, ai_adapter=None, clock=None, persistence=None):
        self.state=state; self.log=eventlog; self.ai_adapter=ai_adapter; self.persistence=persistence
        self._clock=clock or (lambda: time.time_ns()); self._lock=threading.Lock(); self._stop=threading.Event(); self._thread=None
        self.book=Book(cash=10000); self.last={"cycle":0,"equity":self.book.cash,"halted":False,"ts":None,"fills":0,"drift":{},"data_health":{}}; self._cycle=0
        self._rebuild(); self._recover()

    def _rebuild(self):
        self.orch,self.gw,self.ai=build_engine(self.state,self.log,self.ai_adapter)

    def _ensure_live_paper_boundary(self):
        if self.state.mode != "PAPER" or getattr(self.orch.market, "source_name", "") != "public_spot_candles":
            return
        events=self.log.all()
        if any(e.kind == "PAPER_ACCOUNT_RESET" and e.payload.get("domain") == self.LIVE_PAPER_DOMAIN for e in events):
            return
        starting=10000.0
        for e in reversed(events):
            if e.kind == "BALANCE":
                try:
                    starting=float(e.payload.get("equity", e.payload.get("cash", starting)))
                    if starting > 0: break
                except Exception:
                    pass
        self.log.append("PAPER_ACCOUNT_RESET", {
            "cash": round(starting, 8),
            "domain": self.LIVE_PAPER_DOMAIN,
            "reason": "switch_from_synthetic_to_live_public_candles",
            "preserved_equity": round(starting, 8),
        })

    def _recover(self):
        self._ensure_live_paper_boundary()
        recon=self.log.reconstruct_positions()
        if recon.get("cash") is not None: self.book.cash=recon["cash"]
        for sym,qty in recon.get("positions",{}).items(): self.book.positions[sym]={"qty":qty,"entry":0.0}
        self.log.append("RECONCILE",{"boot":True,"restored":len(self.book.positions),"data_source":self.state.data_source})
        if self.persistence: self.persistence.save(self.state,self.log)

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._thread=threading.Thread(target=self._loop,daemon=True); self._thread.start()
    def stop(self): self._stop.set()
    def kill(self,reason="manual"):
        with self._lock:
            self.gw.kill(reason)
            if self.persistence: self.persistence.save(self.state,self.log)
    def reset_kill(self,reason="manual"):
        with self._lock:
            self.gw.reset_kill(reason)
            if self.persistence: self.persistence.save(self.state,self.log)
    def apply_settings(self):
        with self._lock:
            self.log.append("CONFIG_CHANGE",{"component":"worker","change":"settings_applied","mode":self.state.mode,"goals":self.state.goals,"data":self.state.data_source,"broker":self.state.broker})
            self._rebuild()
            self._ensure_live_paper_boundary()
            if self.persistence: self.persistence.save(self.state,self.log)
    def status(self):
        with self._lock:
            market_health={}
            try:
                if hasattr(self.orch.market,"health"): market_health=self.orch.market.health()
            except Exception as exc:
                market_health={"error":str(exc)}
            return {"mode":self.state.mode,"live_armed":self.state.live_armed,"running":bool(self._thread and self._thread.is_alive()),
                    "trading_enabled":bool(self.state.goals.get("trading_enabled",True)),
                    "killed":self.gw.killed,"kill_reason":self.gw.kill_reason,"cycle":self._cycle,**self.last,
                    "positions":{s:round(p["qty"],6) for s,p in self.book.positions.items()},"cash":round(self.book.cash,2),
                    "log_head":self.log.head()[:16],"ai_enabled":self.ai.enabled,"data_source":self.state.data_source,
                    "market_health":market_health,
                    "rotation_enabled":bool(self.state.goals.get("rotation_enabled",True)),
                    "max_exposure_pct":float(self.state.goals.get("max_exposure_pct",0.60)),
                    "persistence":self.persistence.status() if self.persistence else {"remote_json":False}}

    def _tick_source_ts(self):
        """Compatibility helper used by existing tests/replay callers.

        It intentionally does not refresh a live market. The live worker loop uses
        _prepare_tick() so external HTTP is never performed while the control lock
        is held.
        """
        market=self.orch.market
        if getattr(market,"is_live",False):
            return int(self._clock())
        universe=list(self.state.goals.get("universe") or [])
        if not universe:
            return None
        bars=market.bars(universe[0])
        if not bars:
            return None
        return bars[min(60+self._cycle,len(bars)-1)].ts_ns

    def _prepare_tick(self):
        """Fetch external market data without holding the control-plane lock."""
        with self._lock:
            market=self.orch.market
            trading_enabled=bool(self.state.goals.get("trading_enabled",True))
        if not trading_enabled:
            return market, None, {"paused":True}
        if getattr(market,"is_live",False):
            health=market.refresh()
            if int(health.get("ok",0)) <= 0:
                return market, None, health
            return market, int(self._clock()), health
        with self._lock:
            ts=self._tick_source_ts()
        return market, ts, {}

    def _publish_runtime_portfolio(self):
        """Expose a non-persisted book snapshot to the optimiser for this cycle."""
        self.state._runtime_positions={
            sym:{"qty":float(pos.get("qty",0.0)),"entry":float(pos.get("entry",0.0) or 0.0)}
            for sym,pos in self.book.positions.items()
        }
        self.state._runtime_cash=float(self.book.cash)

    def _loop(self):
        while not self._stop.is_set():
            try:
                market,ts,health=self._prepare_tick()
                if ts is not None:
                    with self._lock:
                        if market is not self.orch.market:
                            continue
                        if not self.state.goals.get("trading_enabled",True):
                            continue
                        self._publish_runtime_portfolio()
                        out=self.orch.run_cycle(ts,self.book); self._cycle+=1
                        self.last={"cycle":self._cycle,"equity":round(out.get("equity",0),2),"halted":out.get("halted",False),"ts":ts,"fills":len(out.get("fills",[])),"drift":out.get("drift",{}),"data_health":health}
                        cp=os.environ.get("TERN_AUDIT_CHECKPOINT_PATH"); ck=os.environ.get("TERN_AUDIT_SIGNING_KEY")
                        if cp and ck: self.log.checkpoint(cp,ck)
                        if self.persistence: self.persistence.save(self.state,self.log)
                else:
                    with self._lock:
                        self.last["data_health"]=health
            except Exception:
                try:
                    self.log.append("CONFIG_CHANGE",{"component":"worker","error":traceback.format_exc()[:500]})
                except Exception:
                    pass
            self._stop.wait(max(1,self.state.interval_seconds))
