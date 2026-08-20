from __future__ import annotations

import json
import service.rotationui as base
import service.universeui as universeui
import service.marketui2 as market
import service.api as api_mod
from starlette.responses import HTMLResponse, JSONResponse


def _handles():
    return getattr(api_mod, "_handles", {}) or {}


def _status():
    h = _handles()
    state, worker = h.get("state"), h.get("worker")
    if state is None or worker is None:
        raise RuntimeError("runtime not ready")
    goals = state.goals or {}
    universe = list(goals.get("universe") or [])
    mode = str(goals.get("paper_universe_mode") or "").upper()
    if mode not in {"25", "50", "100", "ALL", "CUSTOM"}:
        mode = str(len(universe)) if len(universe) in (25, 50, 100) else "CUSTOM"
    st = worker.status()
    return {
        "mode": state.mode,
        "paper_universe_mode": mode,
        "universe": universe,
        "universe_count": len(universe),
        "positions": st.get("positions", {}) or {},
        "trading_enabled": bool(st.get("trading_enabled", True)),
        "cycle": st.get("cycle", 0),
        "equity": st.get("equity"),
        "cash": st.get("cash"),
        "persistence": st.get("persistence", {}),
    }


def _set_paper_universe(mode=None, symbols=None):
    h = _handles()
    state, worker, log, persistence = h.get("state"), h.get("worker"), h.get("log"), h.get("persistence")
    if state is None or worker is None or log is None:
        raise RuntimeError("runtime not ready")
    if str(state.mode).upper() != "PAPER":
        raise PermissionError("PAPER universe changes are disabled outside PAPER mode")

    rows = market._symbols()
    available = [str(r.get("symbol")) for r in rows if r.get("symbol")]
    available_set = set(available)
    requested_mode = str(mode or "CUSTOM").upper()

    if requested_mode in {"25", "50", "100"}:
        n = int(requested_mode)
        chosen = available[:n]
    elif requested_mode == "ALL":
        chosen = available
    else:
        requested_mode = "CUSTOM"
        chosen = []
        seen = set()
        for s in symbols or []:
            s = str(s).upper().strip().replace("-", "/")
            if s.endswith("USDT") and "/" not in s:
                s = s[:-4] + "/USDT"
            if s in available_set and s not in seen:
                chosen.append(s); seen.add(s)

    if not chosen:
        raise ValueError("choose at least one available USDT spot symbol")

    warns = state.set_goals({
        "universe": chosen,
        "paper_universe_mode": requested_mode,
    })
    log.append("CONFIG_CHANGE", {
        "component": "paper_universe",
        "paper_universe_mode": requested_mode,
        "universe_count": len(chosen),
        "universe": chosen,
        "authentication": "not_required_in_paper",
        "warnings": warns,
    })
    # Rebuild only the market/strategy configuration. Existing Book positions,
    # cash and the append-only event history are intentionally preserved.
    worker.apply_settings()
    if persistence:
        persistence.save(state, log, force=True)
    out = _status()
    out["warnings"] = warns
    return out


# Reuse the established candle/chart implementation, but make the trading
# universe controls reflect and mutate the durable PAPER state rather than a
# browser-session token.
MARKET_HTML = universeui.HTML
MARKET_HTML = MARKET_HTML.replace(
    '<span id="lockState" class="pill warn">controls locked</span>',
    '<span id="lockState" class="pill ok">PAPER · no password</span>'
)
MARKET_HTML = MARKET_HTML.replace(
    '<button class="secondary" onclick="pickTop(3)">Top 3</button><button class="secondary" onclick="pickTop(5)">Top 5</button><button class="secondary" onclick="pickTop(10)">Top 10</button><button class="secondary" onclick="pickTop(25)">Top 25</button><button class="secondary" onclick="clearPick()">Clear</button><button onclick="saveUniverse()">Save as PAPER universe</button>',
    '<button class="secondary" onclick="pickTop(25)">Use Top 25</button><button class="secondary" onclick="pickTop(50)">Use Top 50</button><button class="secondary" onclick="pickTop(100)">Use Top 100</button><button class="secondary" onclick="pickAll()">Use ALL</button><button onclick="saveUniverse()">Save checked symbols</button>'
)
MARKET_HTML = MARKET_HTML.replace(
    'Tick any symbols below. Saving changes what the PAPER worker is allowed to trade; simply opening a chart does not.',
    'PAPER universe changes do not need the control password. Your choice, positions and trading state are restored when you return. ALL can be heavy on a free Render instance.'
)

PERSIST_SCRIPT = r'''
<script>
(function(){
  function paperApi(u,o){return fetch(u,o||{}).then(function(r){return r.text().then(function(t){var d={};try{d=t?JSON.parse(t):{}}catch(e){throw new Error('HTTP '+r.status)}if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d})})}
  window.updatePicked=function(){
    q('uCount').textContent=picked.size+' selected';
    q('lockState').textContent='PAPER · no password';q('lockState').className='pill ok';
  };
  function reflect(d){
    picked=new Set(d.universe||[]);
    var mode=String(d.paper_universe_mode||'CUSTOM').toUpperCase();
    shown=(mode==='25'||mode==='50'||mode==='100')?Number(mode):(mode==='ALL'?0:shown);
    q('universeNow').textContent='Current PAPER universe: '+mode+' · '+picked.size+' symbols · open positions '+Object.keys(d.positions||{}).length+' · trading '+(d.trading_enabled?'ACTIVE':'PAUSED');
    updatePicked();renderSymbols();
    if(!selected && d.universe && d.universe.length){selected=d.universe[0];loadChart()}
  }
  window.loadSettings=function(){paperApi('/paper-universe').then(reflect).catch(function(e){q('universeNow').textContent='PAPER state unavailable: '+e.message})};
  function saveMode(mode,symbols){
    var body={mode:String(mode)};if(symbols)body.symbols=symbols;
    q('universeNow').textContent='saving PAPER universe…';
    return paperApi('/paper-universe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(d){reflect(d);flash('PAPER universe saved: '+d.paper_universe_mode+' · '+d.universe_count+' symbols')}).catch(function(e){flash(e.message,true);loadSettings()})
  }
  window.pickTop=function(n){if(!all.length)return flash('Market symbols are still loading.',true);picked=new Set(all.slice(0,n).map(function(x){return x.symbol}));updatePicked();renderSymbols();saveMode(String(n))};
  window.pickAll=function(){if(!all.length)return flash('Market symbols are still loading.',true);picked=new Set(all.map(function(x){return x.symbol}));updatePicked();renderSymbols();saveMode('ALL')};
  window.clearPick=function(){picked.clear();updatePicked();renderSymbols()};
  window.saveUniverse=function(){if(!picked.size)return flash('Choose at least one symbol.',true);saveMode('CUSTOM',Array.from(picked))};
  setTimeout(loadSettings,250);
  setInterval(function(){paperApi('/paper-universe').then(function(d){
    // Refresh authoritative state without overwriting checkbox edits while the
    // user is actively touching the symbol list.
    if(document.activeElement && document.activeElement.type==='checkbox')return;
    var current=Array.from(picked).sort().join('|'),server=(d.universe||[]).slice().sort().join('|');
    if(current!==server)reflect(d);
  }).catch(function(){})},5000);
})();
</script>
'''
MARKET_HTML = MARKET_HTML.replace("</body></html>", PERSIST_SCRIPT + "</body></html>")


async def _read_json(receive):
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    return json.loads(body.decode("utf-8") or "{}")


async def app(scope, receive, send):
    if scope.get("type") == "http":
        path = scope.get("path")
        method = scope.get("method")
        if path == "/market" and method == "GET":
            return await HTMLResponse(MARKET_HTML)(scope, receive, send)
        if path == "/paper-universe" and method == "GET":
            try:
                response = JSONResponse(_status())
            except Exception as exc:
                response = JSONResponse({"error": str(exc)}, status_code=503)
            return await response(scope, receive, send)
        if path == "/paper-universe" and method == "POST":
            try:
                payload = await _read_json(receive)
                response = JSONResponse(_set_paper_universe(payload.get("mode"), payload.get("symbols")))
            except PermissionError as exc:
                response = JSONResponse({"error": str(exc)}, status_code=409)
            except Exception as exc:
                response = JSONResponse({"error": str(exc)}, status_code=400)
            return await response(scope, receive, send)
    await base.app(scope, receive, send)
