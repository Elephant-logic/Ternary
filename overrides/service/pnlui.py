from __future__ import annotations
import json
import service.finalfix as base
import service.marketui as marketui
import service.api as api_mod
from service import openai_risk
from starlette.responses import JSONResponse


def _handles():
    return getattr(api_mod, "_handles", {}) or {}


def _authorization(scope):
    headers = dict(scope.get("headers") or [])
    return headers.get(b"authorization", b"").decode("utf-8", "replace")


def _trading_state():
    h = _handles()
    state = h.get("state")
    worker = h.get("worker")
    if state is None or worker is None:
        raise RuntimeError("runtime not ready")
    return {
        "trading_enabled": bool((state.goals or {}).get("trading_enabled", True)),
        "mode": state.mode,
        "cycle": worker.status().get("cycle", 0),
        "positions": worker.status().get("positions", {}),
    }


def _set_trading(enabled: bool):
    h = _handles()
    state, worker, log, persistence = h.get("state"), h.get("worker"), h.get("log"), h.get("persistence")
    if state is None or worker is None or log is None:
        raise RuntimeError("runtime not ready")
    with worker._lock:
        goals = dict(state.goals or {})
        goals["trading_enabled"] = bool(enabled)
        state.goals = goals
        state.save()
        log.append("CONFIG_CHANGE", {
            "component": "trading_control",
            "trading_enabled": bool(enabled),
            "action": "resume" if enabled else "pause",
        })
    if persistence:
        persistence.save(state, log)
    return _trading_state()


def _set_ai_policy(max_calls_per_hour: int, min_interval_seconds: int = 0):
    h = _handles()
    state, log, persistence = h.get("state"), h.get("log"), h.get("persistence")
    if state is None or log is None:
        raise RuntimeError("runtime not ready")
    max_calls_per_hour = max(0, min(int(max_calls_per_hour), 500))
    min_interval_seconds = max(0, min(int(min_interval_seconds), 3600))
    status = openai_risk.set_risk_policy(
        max_calls_per_hour=max_calls_per_hour,
        min_interval_seconds=min_interval_seconds,
    )
    log.append("CONFIG_CHANGE", {
        "component": "ai_risk_budget",
        "max_calls_per_hour": max_calls_per_hour,
        "min_interval_seconds": min_interval_seconds,
        "pacing_mode": "auto" if min_interval_seconds == 0 else "custom",
    })
    if persistence:
        persistence.save(state, log)
    return status


EXTRA = r'''
<script>
(function(){
  var lastEq=null,lastCycle=null;
  function byId(x){return document.getElementById(x)}
  function controlToken(){return sessionStorage.getItem('tern_control_token')||''}
  async function jsonFetch(u,o){
    var r=await fetch(u,o||{}),t=await r.text(),d={};
    try{d=t?JSON.parse(t):{}}catch(e){throw new Error('HTTP '+r.status)}
    if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;
  }
  function ensureTradingCard(){
    var pane=byId('settingsPane');if(!pane||byId('tradingControlCard'))return;
    var card=document.createElement('div');card.className='card';card.id='tradingControlCard';
    card.innerHTML='<div class="section-title">Trading control</div><div class="k" style="margin-bottom:10px">Pause stops new strategy cycles and orders without deleting positions, cash, settings, or history. The pause is saved and survives a Render restart.</div><div class="row"><button class="danger" id="pauseTradingBtn">Pause Trading</button><button id="resumeTradingBtn">Resume Trading</button><span id="tradingControlState" class="pill warn">loading…</span></div>';
    pane.insertBefore(card,pane.firstChild);
    byId('pauseTradingBtn').onclick=function(){setTrading(false)};
    byId('resumeTradingBtn').onclick=function(){setTrading(true)};
  }
  function ensureAICard(){
    var pane=byId('settingsPane');if(!pane||byId('aiBudgetCard'))return;
    var card=document.createElement('div');card.className='card';card.id='aiBudgetCard';
    card.innerHTML='<div class="section-title">Execution AI test budget & pacing</div><div class="k" style="margin-bottom:10px">Fresh OpenAI risk reviews are spread through the hour instead of being spent immediately. Set pacing to 0 for automatic spacing (3600 ÷ calls/hour). Repeated materially-similar candidates may use the cache for free. PAPER continues through deterministic risk checks between AI slots; LIVE remains fail-closed.</div><div class="grid"><label class="field">Max calls / hour<input id="aiBudgetInput" type="number" min="0" max="500" step="1" value="20"></label><label class="field">Minimum seconds between fresh AI calls<input id="aiPaceInput" type="number" min="0" max="3600" step="1" value="0"><span class="k tiny">0 = auto spread across the hour</span></label></div><div class="row" style="margin-top:10px"><button id="saveAiBudgetBtn">Save AI policy</button></div><div id="aiUsageDetail" class="k" style="margin-top:12px">loading…</div>';
    var first=byId('tradingControlCard');if(first&&first.nextSibling)pane.insertBefore(card,first.nextSibling);else pane.appendChild(card);
    byId('saveAiBudgetBtn').onclick=saveAiBudget;
  }
  async function refreshTradingState(){
    try{var s=await jsonFetch('/trading-state'),e=byId('tradingControlState');if(e){e.textContent=s.trading_enabled?'ACTIVE':'PAUSED';e.className='pill '+(s.trading_enabled?'up':'warn')}}catch(e){}
  }
  async function refreshAIUsage(){
    try{
      var u=await jsonFetch('/ai-usage'),d=byId('aiUsageDetail'),i=byId('aiBudgetInput'),p=byId('aiPaceInput');
      if(i&&document.activeElement!==i)i.value=u.max_calls_per_hour;
      if(p&&document.activeElement!==p)p.value=u.min_interval_seconds||0;
      if(d)d.innerHTML='Risk calls: <b>'+u.calls_this_hour+' / '+u.max_calls_per_hour+'</b> this hour · '+u.calls_remaining+' remaining<br>Pacing: <b>'+u.pacing_mode+'</b> · one fresh review at most every '+u.effective_min_interval_seconds+'s · next slot in '+u.next_fresh_call_in_seconds+'s<br>Cache hits this hour: '+(u.cache_hits_this_hour||0)+' · paced PAPER passes: '+(u.pace_bypasses_this_hour||0)+' · budget PAPER passes: '+(u.budget_bypasses_this_hour||0)+'<br>Today UTC: '+u.calls_today+' calls · '+u.input_tokens_today.toLocaleString()+' input tokens · '+u.output_tokens_today.toLocaleString()+' output tokens · estimated cost <b>$'+Number(u.estimated_cost_usd_today||0).toFixed(6)+'</b>';
    }catch(e){var d=byId('aiUsageDetail');if(d)d.textContent='AI usage unavailable: '+e.message}
  }
  async function saveAiBudget(){
    if(!controlToken()){if(typeof flash==='function')flash('Unlock Settings with TERN_CONTROL_TOKEN first.',true);return}
    var v=parseInt(byId('aiBudgetInput').value,10),p=parseInt(byId('aiPaceInput').value,10);
    if(!Number.isFinite(v)||v<0||v>500){if(typeof flash==='function')flash('AI budget must be between 0 and 500 calls/hour.',true);return}
    if(!Number.isFinite(p)||p<0||p>3600){if(typeof flash==='function')flash('AI pacing must be 0 to 3600 seconds. 0 means automatic.',true);return}
    try{await jsonFetch('/ai-budget',{method:'POST',headers:{'Authorization':'Bearer '+controlToken(),'Content-Type':'application/json'},body:JSON.stringify({max_calls_per_hour:v,min_interval_seconds:p})});if(typeof flash==='function')flash('Execution AI budget and pacing saved.');refreshAIUsage()}catch(e){if(typeof flash==='function')flash(e.message,true)}
  }
  async function setTrading(on){
    if(!controlToken()){if(typeof flash==='function')flash('Unlock Settings with TERN_CONTROL_TOKEN first.',true);return}
    if(!on&&!confirm('Pause trading? Existing PAPER positions and account state will be kept, but no new strategy cycles or orders will run until you resume.'))return;
    try{
      await jsonFetch(on?'/trading-resume':'/trading-pause',{method:'POST',headers:{'Authorization':'Bearer '+controlToken()}});
      if(typeof flash==='function')flash(on?'Trading resumed.':'Trading paused.');
      await refreshTradingState();
    }catch(e){if(typeof flash==='function')flash(e.message,true)}
  }
  async function getStatus(){
    try{
      var r=await fetch('/status');
      var s=await r.json();
      if(!r.ok)throw new Error(s.error||('HTTP '+r.status));
      var box=byId('status'); if(!box)return;
      var old=byId('paperPnlLine'); if(old)old.remove();
      var oldm=byId('paperMarketLine'); if(oldm)oldm.remove();
      var oldt=byId('tradingStateLine'); if(oldt)oldt.remove();
      var olda=byId('aiBudgetLine'); if(olda)olda.remove();
      var eq=Number(s.equity||0),cash=Number(s.cash||0),start=10000,invested=eq-cash;
      var pnl=eq-start,pct=start?100*pnl/start:0;
      var delta=(lastEq===null||lastCycle===s.cycle)?null:eq-lastEq;
      var d=document.createElement('div'); d.id='paperPnlLine'; d.className='k';
      var sign=function(v){return (v>=0?'+':'')+v.toFixed(2)};
      d.innerHTML='PAPER equity: <b>£'+eq.toFixed(2)+'</b> · P&amp;L <span style="color:'+(pnl>=0?'#4ec99a':'#e8695f')+'">£'+sign(pnl)+' ('+sign(pct)+'%)</span> · cash £'+cash.toFixed(2)+' · invested £'+invested.toFixed(2)+(delta===null?'':' · Δ cycle <span style="color:'+(delta>=0?'#4ec99a':'#e8695f')+'">£'+sign(delta)+'</span>');
      box.appendChild(d);
      var t=document.createElement('div');t.id='tradingStateLine';t.className='k';t.innerHTML='Trading: <b style="color:'+(s.trading_enabled===false?'#e7b65c':'#4ec99a')+'">'+(s.trading_enabled===false?'PAUSED':'ACTIVE')+'</b>';box.appendChild(t);
      var mh=s.market_health||{},m=document.createElement('div');m.id='paperMarketLine';m.className='k';
      var age=(mh.max_age_s===null||mh.max_age_s===undefined)?'n/a':Number(mh.max_age_s).toFixed(0)+'s';
      m.textContent='PAPER market: '+(s.data_source||'unknown')+' · provider '+(mh.provider||'connecting')+' · data age '+age+(mh.errors&&Object.keys(mh.errors).length?' · partial errors '+Object.keys(mh.errors).length:'');
      box.appendChild(m);
      try{var u=await jsonFetch('/ai-usage'),a=document.createElement('div');a.id='aiBudgetLine';a.className='k';a.textContent='Execution AI: '+u.calls_this_hour+'/'+u.max_calls_per_hour+' calls · pace '+u.effective_min_interval_seconds+'s · next '+u.next_fresh_call_in_seconds+'s · today ~$'+Number(u.estimated_cost_usd_today||0).toFixed(4);box.appendChild(a)}catch(e){}
      lastEq=eq; lastCycle=s.cycle;
      refreshTradingState(); refreshAIUsage();
    }catch(e){}
  }
  window.addEventListener('load',function(){ensureTradingCard();ensureAICard();setTimeout(getStatus,700);setInterval(getStatus,3000)});
})();
</script>
'''

if EXTRA not in marketui.CONTROL_HTML:
    marketui.CONTROL_HTML = marketui.CONTROL_HTML.replace('</body></html>', EXTRA + '</body></html>')


async def app(scope, receive, send):
    if scope.get("type") == "http":
        path = scope.get("path")
        method = scope.get("method")
        if path == "/trading-state" and method == "GET":
            try:
                response = JSONResponse(_trading_state())
            except Exception as exc:
                response = JSONResponse({"error": str(exc)}, status_code=503)
            return await response(scope, receive, send)
        if path == "/ai-usage" and method == "GET":
            return await JSONResponse(openai_risk.risk_usage_status())(scope, receive, send)
        if path == "/ai-budget" and method == "POST":
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
                response = JSONResponse(_set_ai_policy(
                    payload.get("max_calls_per_hour", 20),
                    payload.get("min_interval_seconds", 0),
                ))
            except Exception as exc:
                response = JSONResponse({"error": str(exc)}, status_code=400)
            return await response(scope, receive, send)
        if path in ("/trading-pause", "/trading-resume") and method == "POST":
            reason = api_mod._control_auth_reason(_authorization(scope))
            if reason:
                code = 503 if reason == "control_plane_writes_disabled" else 401
                return await JSONResponse({"error": reason}, status_code=code)(scope, receive, send)
            try:
                response = JSONResponse(_set_trading(path == "/trading-resume"))
            except Exception as exc:
                response = JSONResponse({"error": str(exc)}, status_code=500)
            return await response(scope, receive, send)
    await base.app(scope, receive, send)
