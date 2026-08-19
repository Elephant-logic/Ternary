"""
Control plane (Phase 1 + chat).

FastAPI app exposing read + guarded-write endpoints and a minimal dashboard.
Safe by default: boots PAPER; LIVE requires credentials (profile) AND an explicit
/arm with confirm; the chatbot proposes writes that must be confirmed via /action.

Endpoints
  GET  /health                 liveness
  GET  /status                 worker + gateway status
  GET  /events?kind=&limit=    recent events from the immutable log
  GET  /integrity              verify the audit hash-chain
  POST /mode        {mode}     DEV|PAPER|LIVE  (LIVE needs creds; never auto-arms)
  POST /arm         {armed,confirm}   arm/disarm LIVE order placement
  POST /goals       {goals}    set declarative goals (validated + clamped)
  POST /preset      {name}     conservative|balanced|aggressive
  POST /kill        {reason}   engage kill switch
  POST /resume      {reason}   reset kill switch
  POST /chat        {message}  ask the governed bot (reads the log)
  POST /action      {action,confirm,...}  execute a bot-proposed write (guarded)
"""
from __future__ import annotations
import os

try:
    from fastapi import FastAPI, Body, Header
    from fastapi.responses import HTMLResponse, JSONResponse
    _HAVE_FASTAPI = True
except Exception:  # allow import without fastapi for tests
    _HAVE_FASTAPI = False

from eventlog.log import EventLog
from service.state import AppState
from service.worker import Worker
from service.persistence import RuntimePersistence
from service import chat as chatmod
from config.profiles import load_profile

STATE_PATH = os.environ.get("TERN_STATE", "state.json")
LOG_PATH = os.environ.get("TERN_EVENTLOG", "eventlog.db")


def _control_auth_reason(authorization: str | None, configured_token: str | None = None) -> str | None:
    """Return None only for a valid bearer token. Writes fail closed if unconfigured."""
    import hmac
    token = configured_token if configured_token is not None else os.environ.get("TERN_CONTROL_TOKEN")
    if not token:
        return "control_plane_writes_disabled"
    if not authorization or not authorization.startswith("Bearer "):
        return "missing_control_token"
    supplied = authorization[7:]
    if not hmac.compare_digest(supplied, token):
        return "invalid_control_token"
    return None


def build_app():
    persistence = RuntimePersistence(STATE_PATH, LOG_PATH)
    persistence.restore()
    state = AppState.load(STATE_PATH)
    log = EventLog(path=LOG_PATH, profile=state.mode)

    # optional server-side LLM adapter (key stays here, never in browser)
    llm_adapter = _make_llm_adapter()
    ai_adapter = _make_ai_risk_adapter()

    worker = Worker(state, log, ai_adapter=ai_adapter, persistence=persistence)
    if os.environ.get("TERN_AUTOSTART", "1") == "1":
        worker.start()

    if not _HAVE_FASTAPI:
        return None, {"state": state, "log": log, "worker": worker, "persistence": persistence}  # test handle

    app = FastAPI(title="Ternary Control Plane")

    def guard_write(authorization):
        reason = _control_auth_reason(authorization)
        if reason:
            code = 503 if reason == "control_plane_writes_disabled" else 401
            return JSONResponse({"error": reason}, status_code=code)
        return None

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/status")
    def status():
        out = worker.status()
        out["llm"] = {
            "enabled": llm_adapter is not None,
            "provider": "openai" if llm_adapter is not None else None,
            "model": getattr(llm_adapter, "model", None),
        }
        return out

    @app.get("/events")
    def events(kind: str = None, limit: int = 30):
        return chatmod.query_events(log, kind, limit)

    @app.get("/integrity")
    def integrity():
        return chatmod.log_integrity(log)

    @app.post("/mode")
    def set_mode(mode: str = Body(..., embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)): return blocked
        mode = mode.upper()
        try:
            load_profile(mode)  # LIVE raises without creds
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        state.mode = mode
        if mode != "LIVE":
            state.live_armed = False
        state.save(); worker.apply_settings()
        return {"mode": state.mode, "live_armed": state.live_armed}

    @app.post("/arm")
    def arm(armed: bool = Body(...), confirm: bool = Body(False), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)): return blocked
        if armed:
            if state.mode != "LIVE":
                return JSONResponse({"error": "arm only in LIVE mode"}, status_code=400)
            if not confirm:
                return JSONResponse({"error": "confirm required to arm live orders"}, status_code=400)
        state.live_armed = bool(armed); state.save()
        log.append("CONFIG_CHANGE", {"component": "arm", "live_armed": state.live_armed})
        persistence.save(state, log)
        return {"live_armed": state.live_armed}

    @app.post("/goals")
    def goals(goals: dict = Body(..., embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)): return blocked
        warns = state.set_goals(goals); worker.apply_settings()
        return {"goals": state.goals, "warnings": warns}

    @app.post("/preset")
    def preset(name: str = Body(..., embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)): return blocked
        warns = state.apply_preset(name); worker.apply_settings()
        return {"goals": state.goals, "warnings": warns}

    @app.post("/kill")
    def kill(reason: str = Body("manual", embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)): return blocked
        worker.kill(reason)
        return {"killed": True, "reason": reason}

    @app.post("/resume")
    def resume(reason: str = Body("manual", embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)): return blocked
        worker.reset_kill(reason)
        return {"killed": False}

    @app.post("/chat")
    def chat(message: str = Body(..., embed=True)):
        return chatmod.chat(message, worker, log, llm_adapter=llm_adapter)

    @app.post("/action")
    def action(action: str = Body(...), confirm: bool = Body(False), preset: str = Body(None), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)): return blocked
        if not confirm:
            return JSONResponse({"error": "confirm required"}, status_code=400)
        if action == "kill":
            worker.kill("chat_confirmed"); return {"done": "killed"}
        if action == "flatten":
            # flatten is a guarded write: sell all via gateway on next cycles (simplified: mark intent)
            log.append("CONFIG_CHANGE", {"component": "action", "change": "flatten_requested"})
            return {"done": "flatten_requested"}
        if action == "apply_preset" and preset:
            warns = state.apply_preset(preset); worker.apply_settings()
            return {"done": "preset_applied", "goals": state.goals, "warnings": warns}
        if action == "arm_live":
            if state.mode != "LIVE":
                return JSONResponse({"error": "switch to LIVE mode first"}, status_code=400)
            state.live_armed = True; state.save()
            log.append("CONFIG_CHANGE", {"component": "action", "change": "armed_via_chat"})
            persistence.save(state, log)
            return {"done": "armed"}
        return JSONResponse({"error": f"unknown action {action}"}, status_code=400)

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD_HTML

    return app, {"state": state, "log": log, "worker": worker, "persistence": persistence}


def _make_llm_adapter():
    """Return a small server-side OpenAI Responses API adapter when configured."""
    import json
    import urllib.error
    import urllib.request

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    model = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

    def adapter(text, context):
        system = (
            "You are the read-only Ternary trading-system assistant. Explain the supplied "
            "runtime status clearly and concisely. Never claim an order was placed or a "
            "setting changed. Write-actions are handled separately by Ternary's governed "
            "proposal/confirmation path. If context is insufficient, say so."
        )
        body = {
            "model": model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": text + "\n\nRuntime context:\n" + json.dumps(context, sort_keys=True)},
            ],
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "ternary-control",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"OpenAI HTTP {exc.code}: {detail}") from exc
        pieces = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                text_value = content.get("text")
                if text_value:
                    pieces.append(text_value)
        if not pieces and isinstance(payload.get("output_text"), str):
            pieces.append(payload["output_text"])
        if not pieces:
            raise RuntimeError("OpenAI response contained no text output")
        return "\n".join(pieces).strip()

    adapter.provider = "openai"
    adapter.model = model
    return adapter

def _make_ai_risk_adapter():
    # The governed AI risk model's network call. None -> conservative stub (keyless PAPER).
    return None


DASHBOARD_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Ternary Control</title><style>
body{background:#0e1420;color:#e8edf5;font:14px ui-monospace,monospace;margin:0;padding:20px;max-width:900px;margin:auto}
h1{color:#5ac8e0;font-size:18px;letter-spacing:2px}.card{background:#141c2b;border:1px solid #243247;border-radius:12px;padding:16px;margin:12px 0}
button{background:#5ac8e0;color:#04222b;border:none;border-radius:8px;padding:8px 12px;font-family:inherit;font-weight:700;cursor:pointer;margin:2px}
button.danger{background:#e8695f;color:#2a0e0c}.k{color:#7f8fa6}.up{color:#4ec99a}.down{color:#e8695f}
input,select{background:#111826;border:1px solid #243247;color:#e8edf5;border-radius:8px;padding:8px;font-family:inherit}
#chatlog{height:180px;overflow:auto;background:#111826;border-radius:8px;padding:10px;margin-bottom:8px}
.msg{margin:4px 0}.me{color:#5ac8e0}.bot{color:#e8edf5}pre{white-space:pre-wrap;color:#7f8fa6;font-size:12px}
</style></head><body>
<h1>TERN&#9650;RY · control</h1>
<div class=card id=status>loading…</div>
<div class=card><span class=k>read-only monitor · write controls require an authenticated API client</span></div>
<div class=card>
  <div id=chatlog></div>
  <input id=msg placeholder="ask: why did it stop? how am I doing?" style="width:70%">
  <button onclick="send()">Send</button>
</div>
<div class=card><div class=k>recent events</div><pre id=events></pre></div>
<script>
async function j(u,o){const c=new AbortController();const t=setTimeout(()=>c.abort(),10000);try{const r=await fetch(u,{...(o||{}),signal:c.signal});const raw=await r.text();let data;try{data=raw?JSON.parse(raw):{}}catch(e){throw new Error(`HTTP ${r.status}: ${raw.slice(0,160)}`)}if(!r.ok)throw new Error(data.error||`HTTP ${r.status}`);return data}finally{clearTimeout(t)}}
let refreshing=false;
async function refresh(){
  if(refreshing)return; refreshing=true;
  try{
    const s=await j('/status'); const llm=s.llm||{};
    document.getElementById('status').innerHTML=
      `<b>${s.mode}</b> ${s.live_armed?'· ARMED':''} · ${s.running?'running':'stopped'} · cycle ${s.cycle}`
      +` · equity <b class="${(s.equity>=10000)?'up':'down'}">£${(s.equity||0).toFixed(0)}</b>`
      +(s.killed?` · <span class=down>HALTED: ${s.kill_reason}</span>`:' · healthy')
      +`<div class=k style="margin-top:6px">positions: ${JSON.stringify(s.positions||{})}</div>`
      +`<div class=k>LLM: ${llm.enabled?'on · '+(llm.model||'configured'):'off (OPENAI_API_KEY not set)'}</div>`;
    const ev=await j('/events?limit=8');
    document.getElementById('events').textContent=ev.map(e=>`#${e.seq} ${e.kind} ${JSON.stringify(e.payload).slice(0,80)}`).join('\\n');
  }catch(e){
    document.getElementById('status').innerHTML=`<span class=down>backend error: ${String(e.message||e)}</span><div class=k>Open /health and /status to diagnose.</div>`;
  }finally{refreshing=false}
}
function add(who,txt){const d=document.getElementById('chatlog');d.innerHTML+=`<div class="msg ${who}">${who=='me'?'you':'bot'}: ${txt}</div>`;d.scrollTop=d.scrollHeight}
async function send(){const m=document.getElementById('msg').value;if(!m)return;add('me',m);document.getElementById('msg').value='';
  try{const r=await j('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})});
  add('bot',r.reply);if(r.proposal){add('bot',`proposed: ${r.proposal.action} — ${r.proposal.warning} (confirm in UI)`)}}catch(e){add('bot','error: '+String(e.message||e))}}
refresh();setInterval(refresh,3000);
</script></body></html>"""


# module-level app for uvicorn: `uvicorn service.api:app`
if _HAVE_FASTAPI:
    try:
        app, _handles = build_app()
    except Exception:
        app = None
