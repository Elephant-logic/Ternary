"""No-network tests for live-candle PAPER mode."""
from __future__ import annotations
import os, tempfile, time

from tests.framework import test, ok, approx
from data.store import Bar
from data.live_public import PublicSpotMarketData
from eventlog.log import EventLog
from service.state import AppState


@test("live PAPER data: public candles drive bars and paper quote")
def _():
    now = time.time_ns()
    def fake(symbol, interval, limit):
        return [Bar(now - (80-i)*3_600_000_000_000,
                    100+i, 101+i, 99+i, 100.5+i, 1000+i) for i in range(80)]
    m = PublicSpotMarketData(["BTC/USDT"], interval="1h", fetcher=fake, cache_seconds=60)
    h = m.refresh()
    ok(h["ok"] == 1, h)
    bars = m.bars("BTC/USDT")
    ok(len(bars) == 80)
    q = m.quote_at("BTC/USDT", time.time_ns())
    approx(q.mid, bars[-1].close, 1e-8)
    ok(q.bid < q.mid < q.ask)


@test("paper ledger: account reset starts a new price-domain reconstruction")
def _():
    lg = EventLog(profile="PAPER", clock=lambda: 123)
    lg.append("BALANCE", {"cash": 9000.0, "equity": 10000.0})
    lg.append("FILL", {"symbol":"BTC/USDT", "side":"BUY", "qty":1.0})
    lg.append("PAPER_ACCOUNT_RESET", {"cash": 9975.0, "domain":"live_public_v1"})
    lg.append("FILL", {"symbol":"ETH/USDT", "side":"BUY", "qty":0.5})
    r = lg.reconstruct_positions()
    approx(r["cash"], 9975.0)
    ok("BTC/USDT" not in r["positions"], r)
    approx(r["positions"]["ETH/USDT"], 0.5)


@test("hosted PAPER: runtime opt-in selects public candles but keeps paper broker")
def _():
    fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd); os.remove(path)
    old_live = os.environ.get("TERN_LIVE_PAPER")
    old_iv = os.environ.get("TERN_PAPER_CANDLE_INTERVAL")
    try:
        os.environ["TERN_LIVE_PAPER"] = "1"
        os.environ["TERN_PAPER_CANDLE_INTERVAL"] = "1h"
        s = AppState.load(path)
        ok(s.mode == "PAPER")
        ok(s.data_source == "public:1h", s.data_source)
        ok(s.broker == "paper", s.broker)
    finally:
        if old_live is None: os.environ.pop("TERN_LIVE_PAPER", None)
        else: os.environ["TERN_LIVE_PAPER"] = old_live
        if old_iv is None: os.environ.pop("TERN_PAPER_CANDLE_INTERVAL", None)
        else: os.environ["TERN_PAPER_CANDLE_INTERVAL"] = old_iv
        if os.path.exists(path): os.remove(path)
