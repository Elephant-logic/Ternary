"""No-SQL JSON persistence regression tests."""
from __future__ import annotations
import os
from tests.framework import test, ok, approx
from eventlog.log import EventLog
from service.state import AppState
from service.persistence import RuntimePersistence

@test("json event log: persists and reconstructs without SQLite")
def _():
    path="/tmp/tern_test_eventlog.json"
    if os.path.exists(path):os.remove(path)
    lg=EventLog(path=path,profile="PAPER",clock=lambda:100)
    lg.append("BALANCE",{"cash":9000.0})
    lg.append("FILL",{"symbol":"BTC/USDT","side":"BUY","qty":0.25})
    ok(lg.backend=="json");ok(lg.verify()[0])
    lg2=EventLog(path=path,profile="PAPER",clock=lambda:200)
    rec=lg2.reconstruct_positions();approx(rec["cash"],9000.0);approx(rec["positions"]["BTC/USDT"],0.25)
    ok(lg2.head()==lg.head(),"JSON journal head did not survive reopen")

@test("remote JSON snapshot: restores state and event journal after local loss")
def _():
    class MemRemote:
        value=None
        def load(self):return self.value
        def save(self,value):self.value=value
    sp="/tmp/tern_snapshot_state.json";ep="/tmp/tern_snapshot_events.json"
    for path in (sp,ep):
        if os.path.exists(path):os.remove(path)
    remote=MemRemote();ps=RuntimePersistence(sp,ep,remote=remote);st=AppState.load(sp)
    st.goals["max_positions"]=7;st.save();lg=EventLog(path=ep,profile="PAPER",clock=lambda:123);lg.append("BALANCE",{"cash":8123.0})
    ok(ps.save(st,lg),ps.last_error);os.remove(sp);os.remove(ep)
    ps2=RuntimePersistence(sp,ep,remote=remote);ok(ps2.restore(),ps2.last_error)
    st2=AppState.load(sp);lg2=EventLog(path=ep,profile="PAPER")
    ok(st2.goals["max_positions"]==7);approx(lg2.reconstruct_positions()["cash"],8123.0);ok(lg2.verify()[0])
