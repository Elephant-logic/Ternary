from __future__ import annotations
import json
import service.pnlui as base
import service.marketui as marketui
import service.api as api_mod
from starlette.responses import JSONResponse


def _handles():
    return getattr(api_mod, "_handles", {}) or {}


def _authorization(scope):
    headers = dict(scope.get("headers") or [])
    return headers.get(b"authorization", b"").decode("utf-8", "replace")


def _portfolio_policy():
    h = _handles()
    state = h.get("state")
    worker = h.get("worker")
    if state is None or worker is None:
        raise RuntimeError("runtime not ready")
    goals = state.goals or {}
    st = worker.status()
    equity = float(st.get("equity", 0) or 0)
    cash = float(st.get("cash", 0) or 0)
    exposure = max(0.0, (equity - cash) / equity) if equity > 0 else 0.0
    return {
        "rotation_enabled": bool(goals.get("rotation_enabled", True)),
        "rotation_max_per_cycle": int(goals.get("rotation_max_per_cycle", 1)),
        "max_exposure_pct": float(goals.get("max_exposure_pct", 0.60)),
        "current_exposure_pct": exposure,
        "max_positions": int(goals.get("max_positions", 5)),
    }


def _set_portfolio_policy(max_exposure_pct, rotation_enabled, rotation_max_per_cycle=1):
    h = _handles()
    state, worker, log, persistence = h.get("state"), h.get("worker"), h.get("log"), h.get("persistence")
    if state is None or worker is None or log is None:
        raise RuntimeError("runtime not ready")
    try:
        max_exposure_pct = float(max_exposure_pct)
    except Exception:
        raise ValueError("max_exposure_pct must be a number")
    if max_exposure_pct > 1.0:
        max_exposure_pct = max_exposure_pct / 100.0
    if max_exposure_pct < 0.05 or max_exposure_pct > 0.95:
        raise ValueError("max exposure must be between 5% and 95%")
    rotation_max_per_cycle = max(1, min(int(rotation_max_per_cycle), 3))
    warns = state.set_goals({
        "max_exposure_pct": max_exposure_pct,
        "rotation_enabled": bool(rotation_enabled),
        "rotation_max_per_cycle": rotation_max_per_cycle,
    })
    log.append("CONFIG_CHANGE", {
        "component": "portfolio_rotation",
        "max_exposure_pct": state.goals.get("max_exposure_pct"),
        "rotation_enabled": state.goals.get("rotation_enabled"),
        "rotation_max_per_cycle": state.goals.get("rotation_max_per_cycle"),
        "warnings": warns,
    })
    worker.apply_settings()
    if persistence:
        persistence.save(state, log)
    out = _portfolio_policy()
    out["warnings"] = warns
    return out


EXTRA = r'''
<script>
(function(){
  function byId(x){return document.getElementById(x)}
  function controlToken(){return sessionStorage.getItem('tern_control_token')||''}
  async function jsonFetch(u,o){var r=await fetch(u,o||{}),t=await r.text(),d={};try{d=t?JSON.parse(t):{}}catch(e){throw new Error('HTTP '+r.status)}if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d}
  function ensureRotationCard(){
    var pane=byId('settingsPane');if(!pane||byId('rotationCard'))return;
    var card=document.createElement('div');card.className='card';card.id='rotationCard';
    card.innerHTML='<div class="section-title">Portfolio rotation</div><div class="k" style="margin-bottom:10px">When the portfolio is near its exposure ceiling, Ternary can rebalance toward the highest-ranked mechanical candidates instead of repeatedly attempting extra BUY orders. Reductions/exits are targeted before additions and the independent risk gateway still approves every order.</div><div class="grid"><label class="field">Max portfolio exposure %<input id="rotationExposure" type="number" min="5" max="95" step="1" value="60"></label><label class="field">Max rotations per cycle<select id="rotationMax"><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></label></div><label class="row" style="margin-top:12px"><input id="rotationEnabled" type="checkbox" checked style="width:auto"> Enable portfolio rotation</label><div class="row" style="margin-top:12px"><button id="saveRotationPolicy">Save portfolio policy</button><span id="rotationStatus" class="k"></span></div>';
    var ai=byId('aiBudgetCard');if(ai&&ai.nextSibling)pane.insertBefore(card,ai.nextSibling);else pane.appendChild(card);
    byId('saveRotationPolicy').onclick=saveRotation;
  }
  async function refreshRotation(){
    try{
      var d=await jsonFetch('/portfolio-policy');
      var e=byId('rotationExposure'),r=byId('rotationEnabled'),m=byId('rotationMax'),s=byId('rotationStatus');
      if(e&&document.activeElement!==e)e.value=Math.round(Number(d.max_exposure_pct||0)*100);
      if(r)r.checked=!!d.rotation_enabled;if(m)m.value=String(d.rotation_max_per_cycle||1);
      if(s)s.textContent='Current exposure '+(Number(d.current_exposure_pct||0)*100).toFixed(1)+'% · limit '+(Number(d.max_exposure_pct||0)*100).toFixed(0)+'%';
    }catch(e){}
  }
  async function saveRotation(){
    if(!controlToken()){if(typeof flash==='function')flash('Unlock Settings with TERN_CONTROL_TOKEN first.',true);return}
    var exp=Number(byId('rotationExposure').value),enabled=!!byId('rotationEnabled').checked,maxr=parseInt(byId('rotationMax').value,10)||1;
    if(!Number.isFinite(exp)||exp<5||exp>95){if(typeof flash==='function')flash('Max exposure must be between 5% and 95%.',true);return}
    try{
      await jsonFetch('/portfolio-policy',{method:'POST',headers:{'Authorization':'Bearer '+controlToken(),'Content-Type':'application/json'},body:JSON.stringify({max_exposure_pct:exp,rotation_enabled:enabled,rotation_max_per_cycle:maxr})});
      if(typeof flash==='function')flash('Portfolio rotation policy saved.');refreshRotation();
    }catch(e){if(typeof flash==='function')flash(e.message,true)}
  }
  window.addEventListener('load',function(){ensureRotationCard();setTimeout(refreshRotation,900);setInterval(refreshRotation,5000)});
})();
</script>
'''

if EXTRA not in marketui.CONTROL_HTML:
    marketui.CONTROL_HTML = marketui.CONTROL_HTML.replace('</body></html>', EXTRA + '</body></html>')


async def app(scope, receive, send):
    if scope.get("type") == "http":
        path = scope.get("path")
        method = scope.get("method")
        if path == "/portfolio-policy" and method == "GET":
            try:
                response = JSONResponse(_portfolio_policy())
            except Exception as exc:
                response = JSONResponse({"error": str(exc)}, status_code=503)
            return await response(scope, receive, send)
        if path == "/portfolio-policy" and method == "POST":
            reason = api_mod._control_auth_reason(_authorization(scope))
            if reason:
                code = 503 if reason == "control_plane_writes_disabled" else 401
                return await JSONResponse({"error": reason}, status_code=code)(scope, receive, send)
            try:
                body = b""
                while True:
                    message = await receive()
                    body += message.get("body", b"")
                    if not message.get("more_body"):
                        break
                payload = json.loads(body.decode("utf-8") or "{}")
                response = JSONResponse(_set_portfolio_policy(
                    payload.get("max_exposure_pct", 60),
                    payload.get("rotation_enabled", True),
                    payload.get("rotation_max_per_cycle", 1),
                ))
            except Exception as exc:
                response = JSONResponse({"error": str(exc)}, status_code=400)
            return await response(scope, receive, send)
    await base.app(scope, receive, send)
