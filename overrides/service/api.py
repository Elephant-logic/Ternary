"""Control plane (Phase 1 + chat), with diskless JSON restore support."""
from __future__ import annotations
import os
try:
    from fastapi import FastAPI, Body, Header
    from fastapi.responses import HTMLResponse, JSONResponse
    _HAVE_FASTAPI=True
except Exception:
    _HAVE_FASTAPI=False
from eventlog.log import EventLog
from service.state import AppState
from service.worker import Worker
from service.persistence import RuntimePersistence
from service import chat as chatmod
from config.profiles import load_profile
STATE_PATH=os.environ.get("TERN_STATE","state.json")
LOG_PATH=os.environ.get("TERN_EVENTLOG","eventlog.db")

def _control_auth_reason(authorization: str|None, configured_token: str|None=None)->str|None:
    import hmac
    token=configured_token if configured_token is not None else os.environ.get("TERN_CONTROL_TOKEN")
    if not token:return "control_plane_writes_disabled"
    if not authorization or not authorization.startswith("Bearer "):return "missing_control_token"
    if not hmac.compare_digest(authorization[7:],token):return "invalid_control_token"
    return None

def build_app():
    persistence=RuntimePersistence(STATE_PATH,LOG_PATH); persistence.restore()
    state=AppState.load(STATE_PATH); log=EventLog(path=LOG_PATH,profile=state.mode)
    llm_adapter=_make_llm_adapter(); ai_adapter=_make_ai_risk_adapter()
    worker=Worker(state,log,ai_adapter=ai_adapter,persistence=persistence)
    if os.environ.get("TERN_AUTOSTART","1")=="1":worker.start()
    if not _HAVE_FASTAPI:return None,{"state":state,"log":log,"worker":worker,"persistence":persistence}
    app=FastAPI(title="Ternary Control Plane")
    def guard_write(auth):
        reason=_control_auth_reason(auth)
        if reason:return JSONResponse({"error":reason},status_code=503 if reason=="control_plane_writes_disabled" else 401)
    @app.get("/health")
    def health():return {"ok":True}
    @app.get("/status")
    def status():return worker.status()
    @app.get("/events")
    def events(kind:str=None,limit:int=30):return chatmod.query_events(log,kind,limit)
    @app.get("/integrity")
    def integrity():return chatmod.log_integrity(log)
    @app.post("/mode")
    def set_mode(mode:str=Body(...,embed=True),authorization:str=Header(None)):
        if (blocked:=guard_write(authorization)):return blocked
        mode=mode.upper()
        try:load_profile(mode)
        except Exception as e:return JSONResponse({"error":str(e)},status_code=400)
        state.mode=mode
        if mode!="LIVE":state.live_armed=False
        state.save(); worker.apply_settings(); return {"mode":state.mode,"live_armed":state.live_armed}
    @app.post("/arm")
    def arm(armed:bool=Body(...),confirm:bool=Body(False),authorization:str=Header(None)):
        if (blocked:=guard_write(authorization)):return blocked
        if armed:
            if state.mode!="LIVE":return JSONResponse({"error":"arm only in LIVE mode"},status_code=400)
            if not confirm:return JSONResponse({"error":"confirm required to arm live orders"},status_code=400)
        state.live_armed=bool(armed); state.save(); log.append("CONFIG_CHANGE",{"component":"arm","live_armed":state.live_armed}); persistence.save(state,log)
        return {"live_armed":state.live_armed}
    @app.post("/goals")
    def goals(goals:dict=Body(...,embed=True),authorization:str=Header(None)):
        if (blocked:=guard_write(authorization)):return blocked
        warns=state.set_goals(goals); worker.apply_settings(); return {"goals":state.goals,"warnings":warns}
    @app.post("/preset")
    def preset(name:str=Body(...,embed=True),authorization:str=Header(None)):
        if (blocked:=guard_write(authorization)):return blocked
        warns=state.apply_preset(name); worker.apply_settings(); return {"goals":state.goals,"warnings":warns}
    @app.post("/kill")
    def kill(reason:str=Body("manual",embed=True),authorization:str=Header(None)):
        if (blocked:=guard_write(authorization)):return blocked
        worker.kill(reason); return {"killed":True,"reason":reason}
    @app.post("/resume")
    def resume(reason:str=Body("manual",embed=True),authorization:str=Header(None)):
        if (blocked:=guard_write(authorization)):return blocked
        worker.reset_kill(reason); return {"killed":False}
    @app.post("/chat")
    def chat(message:str=Body(...,embed=True)):return chatmod.chat(message,worker,log,llm_adapter=llm_adapter)
    @app.post("/action")
    def action(action:str=Body(...),confirm:bool=Body(False),preset:str=Body(None),authorization:str=Header(None)):
        if (blocked:=guard_write(authorization)):return blocked
        if not confirm:return JSONResponse({"error":"confirm required"},status_code=400)
        if action=="kill":worker.kill("chat_confirmed");return {"done":"killed"}
        if action=="flatten":log.append("CONFIG_CHANGE",{"component":"action","change":"flatten_requested"});persistence.save(state,log);return {"done":"flatten_requested"}
        if action=="apply_preset" and preset:
            warns=state.apply_preset(preset);worker.apply_settings();return {"done":"preset_applied","goals":state.goals,"warnings":warns}
        if action=="arm_live":
            if state.mode!="LIVE":return JSONResponse({"error":"switch to LIVE mode first"},status_code=400)
            state.live_armed=True;state.save();log.append("CONFIG_CHANGE",{"component":"action","change":"armed_via_chat"});persistence.save(state,log);return {"done":"armed"}
        return JSONResponse({"error":f"unknown action {action}"},status_code=400)
    @app.get("/",response_class=HTMLResponse)
    def dashboard():return DASHBOARD_HTML
    return app,{"state":state,"log":log,"worker":worker,"persistence":persistence}

def _make_llm_adapter():
    key=os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    return None if not key else None

def _make_ai_risk_adapter():return None

DASHBOARD_HTML='''<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Ternary Control</title><style>body{background:#0e1420;color:#e8edf5;font:14px ui-monospace,monospace;margin:0;padding:20px;max-width:900px;margin:auto}h1{color:#5ac8e0;font-size:18px;letter-spacing:2px}.card{background:#141c2b;border:1px solid #243247;border-radius:12px;padding:16px;margin:12px 0}.k{color:#7f8fa6}.up{color:#4ec99a}.down{color:#e8695f}button,input{background:#111826;border:1px solid #243247;color:#e8edf5;border-radius:8px;padding:8px;font-family:inherit}button{background:#5ac8e0;color:#04222b;font-weight:700}#chatlog{height:180px;overflow:auto;background:#111826;border-radius:8px;padding:10px;margin-bottom:8px}pre{white-space:pre-wrap;color:#7f8fa6;font-size:12px}</style></head><body><h1>TERN&#9650;RY · control</h1><div class=card id=status>loading…</div><div class=card><span class=k>read-only monitor · write controls require an authenticated API client</span></div><div class=card><div id=chatlog></div><input id=msg placeholder="ask: why did it stop?" style="width:70%"><button onclick="send()">Send</button></div><div class=card><div class=k>recent events</div><pre id=events></pre></div><script>async function j(u,o){return(await fetch(u,o)).json()}async function refresh(){const s=await j('/status');const p=s.persistence||{};document.getElementById('status').innerHTML=`<b>${s.mode}</b> · ${s.running?'running':'stopped'} · cycle ${s.cycle} · equity £${(s.equity||0).toFixed(0)}<div class=k>positions: ${JSON.stringify(s.positions||{})}</div><div class=k>remote JSON: ${p.remote_json?'on':'off'}${p.restored_from_remote?' · restored':''}${p.error?' · error: '+p.error:''}</div>`;const ev=await j('/events?limit=8');document.getElementById('events').textContent=ev.map(e=>`#${e.seq} ${e.kind} ${JSON.stringify(e.payload).slice(0,80)}`).join('\n')}async function send(){const m=document.getElementById('msg').value;if(!m)return;const r=await j('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})});document.getElementById('chatlog').innerHTML+=`<div>${r.reply}</div>`}refresh();setInterval(refresh,3000)</script></body></html>'''
if _HAVE_FASTAPI:
    try:app,_handles=build_app()
    except Exception:app=None
