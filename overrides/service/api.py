"""
Ternary control plane with authenticated mobile settings UI.

Read endpoints remain public for monitoring. Every state-changing endpoint fails
closed unless the request carries the Render-configured TERN_CONTROL_TOKEN.
LIVE mode never auto-arms; arming is a separate confirmed action.
"""
from __future__ import annotations
import os

try:
    from fastapi import FastAPI, Body, Header
    from fastapi.responses import HTMLResponse, JSONResponse
    _HAVE_FASTAPI = True
except Exception:
    _HAVE_FASTAPI = False

from eventlog.log import EventLog
from service.state import AppState, PRESETS
from service.worker import Worker
from service.persistence import RuntimePersistence
from service import chat as chatmod
from config.profiles import load_profile

STATE_PATH = os.environ.get("TERN_STATE", "state.json")
LOG_PATH = os.environ.get("TERN_EVENTLOG", "eventlog.db")


def _control_auth_reason(authorization: str | None, configured_token: str | None = None) -> str | None:
    import hmac
    token = configured_token if configured_token is not None else os.environ.get("TERN_CONTROL_TOKEN")
    if not token:
        return "control_plane_writes_disabled"
    if not authorization or not authorization.startswith("Bearer "):
        return "missing_control_token"
    if not hmac.compare_digest(authorization[7:], token):
        return "invalid_control_token"
    return None


def build_app():
    persistence = RuntimePersistence(STATE_PATH, LOG_PATH)
    persistence.restore()
    state = AppState.load(STATE_PATH)
    log = EventLog(path=LOG_PATH, profile=state.mode)
    llm_adapter = _make_llm_adapter()
    ai_adapter = _make_ai_risk_adapter()
    worker = Worker(state, log, ai_adapter=ai_adapter, persistence=persistence)
    if os.environ.get("TERN_AUTOSTART", "1") == "1":
        worker.start()

    if not _HAVE_FASTAPI:
        return None, {"state": state, "log": log, "worker": worker, "persistence": persistence}

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

    @app.get("/settings")
    def settings():
        return {
            "mode": state.mode,
            "live_armed": state.live_armed,
            "interval_seconds": state.interval_seconds,
            "data_source": state.data_source,
            "broker": state.broker,
            "goals": state.goals,
            "presets": list(PRESETS.keys()),
            "llm_model": getattr(llm_adapter, "model", None),
            "persistence": persistence.status(),
        }

    @app.get("/events")
    def events(kind: str = None, limit: int = 30):
        return chatmod.query_events(log, kind, limit)

    @app.get("/integrity")
    def integrity():
        return chatmod.log_integrity(log)

    @app.post("/auth-check")
    def auth_check(authorization: str = Header(None)):
        if (blocked := guard_write(authorization)):
            return blocked
        return {"ok": True}

    @app.post("/mode")
    def set_mode(mode: str = Body(..., embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)):
            return blocked
        mode = mode.upper()
        try:
            load_profile(mode)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        state.mode = mode
        if mode != "LIVE":
            state.live_armed = False
        state.save()
        worker.apply_settings()
        return {"mode": state.mode, "live_armed": state.live_armed}

    @app.post("/arm")
    def arm(armed: bool = Body(...), confirm: bool = Body(False), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)):
            return blocked
        if armed:
            if state.mode != "LIVE":
                return JSONResponse({"error": "arm only in LIVE mode"}, status_code=400)
            if not confirm:
                return JSONResponse({"error": "confirm required to arm live orders"}, status_code=400)
        state.live_armed = bool(armed)
        state.save()
        log.append("CONFIG_CHANGE", {"component": "arm", "live_armed": state.live_armed})
        persistence.save(state, log)
        return {"live_armed": state.live_armed}

    @app.post("/goals")
    def goals(goals: dict = Body(..., embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)):
            return blocked
        warns = state.set_goals(goals)
        worker.apply_settings()
        return {"goals": state.goals, "warnings": warns}

    @app.post("/runtime-settings")
    def runtime_settings(settings: dict = Body(..., embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)):
            return blocked
        allowed = {"interval_seconds"}
        unknown = sorted(set(settings) - allowed)
        if unknown:
            return JSONResponse({"error": "unsupported settings: " + ", ".join(unknown)}, status_code=400)
        if "interval_seconds" in settings:
            try:
                value = int(settings["interval_seconds"])
            except Exception:
                return JSONResponse({"error": "interval_seconds must be an integer"}, status_code=400)
            if value < 1 or value > 3600:
                return JSONResponse({"error": "interval_seconds must be between 1 and 3600"}, status_code=400)
            state.interval_seconds = value
        state.save()
        log.append("CONFIG_CHANGE", {"component": "runtime_settings", "interval_seconds": state.interval_seconds})
        persistence.save(state, log)
        return {"interval_seconds": state.interval_seconds}

    @app.post("/preset")
    def preset(name: str = Body(..., embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)):
            return blocked
        warns = state.apply_preset(name)
        if name not in PRESETS:
            return JSONResponse({"error": warns[0]}, status_code=400)
        worker.apply_settings()
        return {"goals": state.goals, "warnings": warns}

    @app.post("/kill")
    def kill(reason: str = Body("manual", embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)):
            return blocked
        worker.kill(reason)
        return {"killed": True, "reason": reason}

    @app.post("/resume")
    def resume(reason: str = Body("manual", embed=True), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)):
            return blocked
        worker.reset_kill(reason)
        return {"killed": False}

    @app.post("/chat")
    def chat(message: str = Body(..., embed=True)):
        return chatmod.chat(message, worker, log, llm_adapter=llm_adapter)

    @app.post("/action")
    def action(action: str = Body(...), confirm: bool = Body(False), preset: str = Body(None), authorization: str = Header(None)):
        if (blocked := guard_write(authorization)):
            return blocked
        if not confirm:
            return JSONResponse({"error": "confirm required"}, status_code=400)
        if action == "kill":
            worker.kill("chat_confirmed")
            return {"done": "killed"}
        if action == "flatten":
            log.append("CONFIG_CHANGE", {"component": "action", "change": "flatten_requested"})
            persistence.save(state, log)
            return {"done": "flatten_requested"}
        if action == "apply_preset" and preset:
            warns = state.apply_preset(preset)
            worker.apply_settings()
            return {"done": "preset_applied", "goals": state.goals, "warnings": warns}
        if action == "arm_live":
            if state.mode != "LIVE":
                return JSONResponse({"error": "switch to LIVE mode first"}, status_code=400)
            state.live_armed = True
            state.save()
            log.append("CONFIG_CHANGE", {"component": "action", "change": "armed_via_chat"})
            persistence.save(state, log)
            return {"done": "armed"}
        return JSONResponse({"error": f"unknown action {action}"}, status_code=400)

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD_HTML

    return app, {"state": state, "log": log, "worker": worker, "persistence": persistence}


def _make_llm_adapter():
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
    return None


DASHBOARD_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Ternary Control</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{background:#0e1420;color:#e8edf5;font:14px ui-monospace,monospace;margin:0;padding:18px;max-width:900px;margin:auto}h1{color:#5ac8e0;font-size:18px;letter-spacing:2px;margin:8px 0 16px}.card{background:#141c2b;border:1px solid #243247;border-radius:14px;padding:16px;margin:12px 0}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.between{justify-content:space-between}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.field{display:flex;flex-direction:column;gap:5px}.full{grid-column:1/-1}.k{color:#7f8fa6}.up{color:#4ec99a}.down{color:#e8695f}.warn{color:#e7b65c}.tiny{font-size:11px}.hidden{display:none}button,input,select{font:inherit;border-radius:9px;padding:10px;border:1px solid #2b3a50}input,select{background:#111826;color:#e8edf5;width:100%}button{background:#5ac8e0;color:#04222b;font-weight:700;cursor:pointer}button.secondary{background:#1d2939;color:#dbe6f4}button.danger{background:#e8695f;color:#2a0e0c}button.warnbtn{background:#e7b65c;color:#2a2107}button:disabled{opacity:.45;cursor:not-allowed}.tabs{display:flex;gap:6px;margin-bottom:12px}.tab{flex:1}.tab.active{outline:2px solid #5ac8e0}.section-title{font-weight:700;margin-bottom:10px;color:#b9c7da}.sep{height:1px;background:#243247;margin:14px 0}.pill{padding:4px 7px;border-radius:999px;background:#101827;border:1px solid #243247}#chatlog{height:180px;overflow:auto;background:#111826;border-radius:9px;padding:10px;margin-bottom:8px}.msg{margin:5px 0}.me{color:#5ac8e0}.bot{color:#e8edf5}pre{white-space:pre-wrap;color:#7f8fa6;font-size:12px;margin:8px 0 0;overflow-wrap:anywhere}#flash{position:sticky;top:8px;z-index:10;margin:0 0 8px;padding:10px;border-radius:9px;background:#152337;border:1px solid #34506e;display:none}@media(max-width:520px){body{padding:12px}.grid{grid-template-columns:1fr}.full{grid-column:1}.card{padding:14px}h1{font-size:17px}}
</style></head><body>
<h1>TERN&#9650;RY · control</h1><div id=flash></div>
<div class=card id=status>loading…</div>
<div class=tabs><button class="tab active secondary" id=monitorTab onclick="showTab('monitor')">Monitor</button><button class="tab secondary" id=settingsTab onclick="showTab('settings')">⚙ Settings</button></div>

<div id=monitorPane>
<div class=card><div id=chatlog></div><div class=row><input id=msg placeholder="ask: why did it stop?" style="flex:1"><button onclick="send()">Send</button></div></div>
<div class=card><div class=k>recent events</div><pre id=events></pre></div>
</div>

<div id=settingsPane class=hidden>
<div class=card>
 <div class="row between"><div><div class=section-title>Control access</div><div class="k tiny">Uses TERN_CONTROL_TOKEN. Kept only in this browser session.</div></div><span id=lockState class="pill warn">locked</span></div>
 <div class=row style="margin-top:10px"><input id=token type=password placeholder="TERN_CONTROL_TOKEN" style="flex:1"><button onclick="unlock()">Unlock</button><button class=secondary onclick="forgetToken()">Forget</button></div>
</div>

<div class=card>
 <div class=section-title>Trading mode</div>
 <div class=row><button class=secondary onclick="setMode('PAPER')">PAPER</button><button class=warnbtn onclick="setMode('LIVE')">LIVE</button><span id=modeHint class=k></span></div>
 <div class=sep></div>
 <div class="row between"><div><b>LIVE order arming</b><div class="k tiny">Switching to LIVE does not arm orders.</div></div><div class=row><button class=danger onclick="armLive(true)">ARM LIVE</button><button class=secondary onclick="armLive(false)">Disarm</button></div></div>
</div>

<div class=card>
 <div class=section-title>Risk preset</div><div class=row><button class=secondary onclick="applyPreset('conservative')">Conservative</button><button class=secondary onclick="applyPreset('balanced')">Balanced</button><button class=secondary onclick="applyPreset('aggressive')">Aggressive</button></div>
 <div class=sep></div>
 <div class=grid>
  <label class="field full"><span>Universe <span class=k>(comma separated)</span></span><input id=universe placeholder="BTC/USDT, ETH/USDT, SOL/USDT"></label>
  <label class=field><span>Max drawdown %</span><input id=maxDrawdown type=number step=.1 min=1 max=30></label>
  <label class=field><span>Daily loss limit %</span><input id=maxDailyLoss type=number step=.1 min=.5 max=10></label>
  <label class=field><span>Max position %</span><input id=maxPosition type=number step=.1 min=1 max=25></label>
  <label class=field><span>Max exposure %</span><input id=maxExposure type=number step=.1 min=5 max=95></label>
  <label class=field><span>Max positions</span><input id=maxPositions type=number min=1 max=20></label>
  <label class=field><span>Turnover</span><select id=turnover><option>low</option><option>medium</option><option>high</option></select></label>
 </div><div style="margin-top:12px"><button onclick="saveRisk()">Save risk settings</button></div>
</div>

<div class=card>
 <div class=section-title>Runtime</div><div class=grid>
  <label class=field><span>Cycle interval (seconds)</span><input id=interval type=number min=1 max=3600></label>
  <label class=field><span>Data source</span><input id=dataSource disabled></label>
  <label class=field><span>Broker</span><input id=broker disabled></label>
  <label class=field><span>LLM model</span><input id=llmModel disabled></label>
 </div><div style="margin-top:12px"><button onclick="saveRuntime()">Save interval</button></div>
</div>

<div class=card>
 <div class=section-title>Emergency controls</div><div class=row><button class=danger onclick="killSwitch()">KILL SWITCH</button><button class=secondary onclick="resumeTrading()">Resume</button></div>
</div>

<div class=card><div class=section-title>Persistence</div><div id=persistDetail class=k>loading…</div></div>
</div>

<script>
const $=id=>document.getElementById(id);let refreshing=false,lastSettings=null;
function flash(msg,bad=false){const e=$('flash');e.textContent=msg;e.style.display='block';e.style.color=bad?'#ff8b84':'#9fe3c6';clearTimeout(flash.t);flash.t=setTimeout(()=>e.style.display='none',4500)}
async function j(u,o){const c=new AbortController(),t=setTimeout(()=>c.abort(),12000);try{const r=await fetch(u,{...(o||{}),signal:c.signal});const raw=await r.text();let data;try{data=raw?JSON.parse(raw):{}}catch(e){throw new Error(`HTTP ${r.status}: ${raw.slice(0,160)}`)}if(!r.ok)throw new Error(data.error||`HTTP ${r.status}`);return data}finally{clearTimeout(t)}}
function token(){return sessionStorage.getItem('tern_control_token')||''}function authHeaders(){return {'Content-Type':'application/json','Authorization':'Bearer '+token()}}
async function write(url,body){if(!token())throw new Error('Settings are locked. Enter TERN_CONTROL_TOKEN first.');return j(url,{method:'POST',headers:authHeaders(),body:JSON.stringify(body)})}
function showTab(n){$('monitorPane').classList.toggle('hidden',n!=='monitor');$('settingsPane').classList.toggle('hidden',n!=='settings');$('monitorTab').classList.toggle('active',n==='monitor');$('settingsTab').classList.toggle('active',n==='settings');if(n==='settings')loadSettings()}
async function unlock(){const v=$('token').value.trim();if(!v)return flash('Enter your control token.',true);sessionStorage.setItem('tern_control_token',v);try{await write('/auth-check',{});$('lockState').textContent='unlocked';$('lockState').className='pill up';$('token').value='';flash('Controls unlocked for this browser session.')}catch(e){sessionStorage.removeItem('tern_control_token');$('lockState').textContent='locked';$('lockState').className='pill warn';flash(e.message,true)}}
function forgetToken(){sessionStorage.removeItem('tern_control_token');$('lockState').textContent='locked';$('lockState').className='pill warn';flash('Control token forgotten.')}if(token()){$('lockState').textContent='unlocked';$('lockState').className='pill up'}
async function refresh(){if(refreshing)return;refreshing=true;try{const s=await j('/status'),llm=s.llm||{},p=s.persistence||{};$('status').innerHTML=`<b>${s.mode}</b> ${s.live_armed?'· <span class=down>ARMED</span>':''} · ${s.running?'running':'stopped'} · cycle ${s.cycle} · equity <b class="${(s.equity>=10000)?'up':'down'}">£${(s.equity||0).toFixed(0)}</b>${s.killed?` · <span class=down>HALTED: ${s.kill_reason}</span>`:' · healthy'}<div class=k style="margin-top:6px">positions: ${JSON.stringify(s.positions||{})}</div><div class=k>LLM: ${llm.enabled?'on · '+(llm.model||'configured'):'off'} · JSON backup: ${p.remote_json?'on':'off'}${p.restored_from_remote?' · restored':''}${p.error?' · error':''}</div>`;const ev=await j('/events?limit=8');$('events').textContent=ev.map(e=>`#${e.seq} ${e.kind} ${JSON.stringify(e.payload).slice(0,100)}`).join('\n')}catch(e){$('status').innerHTML=`<span class=down>backend error: ${String(e.message||e)}</span>`}finally{refreshing=false}}
async function loadSettings(){try{const s=await j('/settings');lastSettings=s;const g=s.goals||{};$('modeHint').textContent=`current: ${s.mode}${s.live_armed?' · ARMED':''}`;$('universe').value=(g.universe||[]).join(', ');$('maxDrawdown').value=((g.max_drawdown||0)*100).toFixed(1);$('maxDailyLoss').value=((g.max_daily_loss||0)*100).toFixed(1);$('maxPosition').value=((g.max_position_pct||0)*100).toFixed(1);$('maxExposure').value=((g.max_exposure_pct||0)*100).toFixed(1);$('maxPositions').value=g.max_positions||5;$('turnover').value=g.turnover||'low';$('interval').value=s.interval_seconds||60;$('dataSource').value=s.data_source||'';$('broker').value=s.broker||'';$('llmModel').value=s.llm_model||'off';const p=s.persistence||{};$('persistDetail').textContent=`backend: ${p.remote_backend||'local JSON only'} · remote: ${p.remote_json?'on':'off'} · restored this boot: ${p.restored_from_remote?'yes':'no'} · last save: ${p.last_saved_ns?new Date(p.last_saved_ns/1e6).toLocaleString():'not yet'}${p.error?' · ERROR: '+p.error:''}`}catch(e){flash('Could not load settings: '+e.message,true)}}
async function setMode(m){if(m==='LIVE'&&!confirm('Switch to LIVE mode? This does NOT arm orders. LIVE will still refuse to start if required exchange credentials are missing.'))return;try{const r=await write('/mode',{mode:m});flash(`Mode set to ${r.mode}.`);await loadSettings();await refresh()}catch(e){flash(e.message,true)}}
async function armLive(on){if(on&&!confirm('ARM LIVE ORDER PLACEMENT? Only continue if you intend to allow real orders and LIVE credentials are correctly configured.'))return;try{const r=await write('/arm',{armed:on,confirm:on});flash(r.live_armed?'LIVE orders ARMED.':'LIVE orders disarmed.');await loadSettings();await refresh()}catch(e){flash(e.message,true)}}
async function applyPreset(n){if(n==='aggressive'&&!confirm('Apply aggressive risk preset?'))return;try{await write('/preset',{name:n});flash(`${n} preset applied.`);await loadSettings()}catch(e){flash(e.message,true)}}
async function saveRisk(){const uni=$('universe').value.split(',').map(x=>x.trim()).filter(Boolean);if(!uni.length)return flash('Universe cannot be empty.',true);const goals={universe:uni,max_drawdown:Number($('maxDrawdown').value)/100,max_daily_loss:Number($('maxDailyLoss').value)/100,max_position_pct:Number($('maxPosition').value)/100,max_exposure_pct:Number($('maxExposure').value)/100,max_positions:Number($('maxPositions').value),turnover:$('turnover').value};try{const r=await write('/goals',{goals});flash('Risk settings saved.'+(r.warnings&&r.warnings.length?' '+r.warnings.join('; '):''));await loadSettings()}catch(e){flash(e.message,true)}}
async function saveRuntime(){try{await write('/runtime-settings',{settings:{interval_seconds:Number($('interval').value)}});flash('Cycle interval saved.');await loadSettings()}catch(e){flash(e.message,true)}}
async function killSwitch(){if(!confirm('Engage the Ternary kill switch now?'))return;try{await write('/kill',{reason:'dashboard_manual'});flash('KILL SWITCH ENGAGED.',true);await refresh()}catch(e){flash(e.message,true)}}
async function resumeTrading(){if(!confirm('Reset the kill switch and allow governed trading cycles to resume?'))return;try{await write('/resume',{reason:'dashboard_manual'});flash('Kill switch reset.');await refresh()}catch(e){flash(e.message,true)}}
function add(who,txt){const d=$('chatlog');const el=document.createElement('div');el.className='msg '+who;el.textContent=(who==='me'?'you: ':'bot: ')+txt;d.appendChild(el);d.scrollTop=d.scrollHeight}
async function send(){const m=$('msg').value;if(!m)return;add('me',m);$('msg').value='';try{const r=await j('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})});add('bot',r.reply||'No reply.');if(r.proposal)add('bot',`proposed: ${r.proposal.action} — ${r.proposal.warning||'confirmation required'}`)}catch(e){add('bot','error: '+e.message)}}
$('msg').addEventListener('keydown',e=>{if(e.key==='Enter')send()});refresh();setInterval(refresh,3000);
</script></body></html>"""


if _HAVE_FASTAPI:
    try:
        app, _handles = build_app()
    except Exception:
        app = None
