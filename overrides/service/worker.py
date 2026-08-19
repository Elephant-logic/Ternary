"""Background worker with restart recovery and optional remote JSON snapshots."""
from __future__ import annotations
import threading, time, traceback, os
from eventlog.log import EventLog
from core.orchestrator import Book
from service.engine import build_engine
from service.state import AppState

class Worker:
    def __init__(self, state: AppState, eventlog: EventLog, ai_adapter=None, clock=None, persistence=None):
        self.state=state; self.log=eventlog; self.ai_adapter=ai_adapter; self.persistence=persistence
        self._clock=clock or (lambda: time.time_ns()); self._lock=threading.Lock(); self._stop=threading.Event(); self._thread=None
        self.book=Book(cash=10000); self.last={"cycle":0,"equity":self.book.cash,"halted":False,"ts":None,"fills":0,"drift":{}}; self._cycle=0
        self._rebuild(); self._recover()
    def _rebuild(self): self.orch,self.gw,self.ai=build_engine(self.state,self.log,self.ai_adapter)
    def _recover(self):
        recon=self.log.reconstruct_positions()
        if recon.get("cash") is not None: self.book.cash=recon["cash"]
        for sym,qty in recon.get("positions",{}).items(): self.book.positions[sym]={"qty":qty,"entry":0.0}
        self.log.append("RECONCILE",{"boot":True,"restored":len(self.book.positions)})
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
            if self.persistence: self.persistence.save(self.state,self.log)
    def status(self):
        with self._lock:
            return {"mode":self.state.mode,"live_armed":self.state.live_armed,"running":bool(self._thread and self._thread.is_alive()),
                    "killed":self.gw.killed,"kill_reason":self.gw.kill_reason,"cycle":self._cycle,**self.last,
                    "positions":{s:round(p["qty"],6) for s,p in self.book.positions.items()},"cash":round(self.book.cash,2),
                    "log_head":self.log.head()[:16],"ai_enabled":self.ai.enabled,
                    "persistence":self.persistence.status() if self.persistence else {"remote_json":False}}
    def _tick_source_ts(self):
        sym=self.state.goals["universe"][0]; bars=self.orch.market.bars(sym)
        if not bars: return None
        return bars[min(60+self._cycle,len(bars)-1)].ts_ns
    def _loop(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    ts=self._tick_source_ts()
                    if ts is not None:
                        out=self.orch.run_cycle(ts,self.book); self._cycle+=1
                        self.last={"cycle":self._cycle,"equity":round(out.get("equity",0),2),"halted":out.get("halted",False),"ts":ts,"fills":len(out.get("fills",[])),"drift":out.get("drift",{})}
                        cp=os.environ.get("TERN_AUDIT_CHECKPOINT_PATH"); ck=os.environ.get("TERN_AUDIT_SIGNING_KEY")
                        if cp and ck: self.log.checkpoint(cp,ck)
                        if self.persistence: self.persistence.save(self.state,self.log)
            except Exception:
                self.log.append("CONFIG_CHANGE",{"component":"worker","error":traceback.format_exc()[:500]})
            self._stop.wait(max(1,self.state.interval_seconds))
