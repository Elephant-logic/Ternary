from __future__ import annotations
import os
import service.api as api_mod
import service.marketchat as base
import service.marketui as marketui
from starlette.responses import JSONResponse


def _worker():
    return (getattr(api_mod, '_handles', {}) or {}).get('worker')


def _ai_status():
    w=_worker()
    if w is None:
        return {'enabled':False,'provider':None,'model':None,'state':'worker_not_ready'}
    ai=getattr(w,'ai',None)
    model=getattr(ai,'model_version',None)
    enabled=bool(getattr(ai,'enabled',False))
    if not os.environ.get('OPENAI_API_KEY'):
        return {'enabled':False,'provider':None,'model':model or 'unavailable','state':'missing_api_key'}
    if model in (None,'configured','unavailable'):
        return {'enabled':enabled,'provider':'openai','model':model,'state':'degraded'}
    return {'enabled':enabled,'provider':'openai','model':model,'state':'active'}


def _position(symbol):
    w=_worker()
    if w is None:
        raise RuntimeError('worker not ready')
    status=w.status()
    positions=status.get('positions',{}) or {}
    return {'symbol':symbol,'position_qty':float(positions.get(symbol,0) or 0)}


EXTRA = r'''
<script>
(function(){
  function byId(x){return document.getElementById(x)}
  async function tinyFetch(u,o){var r=await fetch(u,o||{});var t=await r.text();var d={};try{d=t?JSON.parse(t):{}}catch(e){throw new Error('HTTP '+r.status)}if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d}
  async function showAI(){try{var a=await tinyFetch('/ai-risk-status'),s=byId('status');if(!s)return;var old=byId('aiRiskLine');if(old)old.remove();var d=document.createElement('div');d.id='aiRiskLine';d.className='k';d.textContent='AI Risk: '+(a.enabled?'on':'off')+' · '+(a.model||a.state||'unavailable')+(a.state&&a.state!=='active'?' · '+a.state:'');s.appendChild(d)}catch(e){}}
  async function syncPos(){var sel=byId('chartSymbol'),p=byId('chartPos');if(!sel||!p||!sel.value)return;var sym=sel.value;p.textContent='position: …';try{var d=await tinyFetch('/chart-position?symbol='+encodeURIComponent(sym));if(sel.value===d.symbol)p.textContent='position: '+Number(d.position_qty||0).toFixed(6)}catch(e){if(sel.value===sym)p.textContent='position unavailable'}}
  window.addEventListener('load',function(){var sel=byId('chartSymbol');if(sel)sel.addEventListener('change',function(){setTimeout(syncPos,50)});setTimeout(syncPos,800);showAI();setInterval(showAI,3100)});
  if(typeof window.sendChat==='function'){
    var apiFn=window.api,qFn=window.q;
    window.sendChat=function(){var m=qFn('msg').value;if(!m)return;qFn('msg').value='';var log=qFn('chatlog');log.textContent='you: '+m+'\n\nTERNARY: analysing…';apiFn('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})}).then(function(r){log.textContent='you: '+m+'\n\nTERNARY:\n'+(r.reply||'No reply')}).catch(function(e){log.textContent='you: '+m+'\n\nTERNARY error: '+e.message})}
  }
})();
</script>
'''

# marketui serves this global for the control page. Patch once at import time.
if EXTRA not in marketui.CONTROL_HTML:
    marketui.CONTROL_HTML = marketui.CONTROL_HTML.replace('</body></html>', EXTRA + '</body></html>')


async def app(scope, receive, send):
    if scope.get('type')=='http' and scope.get('method')=='GET':
        path=scope.get('path')
        if path=='/ai-risk-status':
            return await JSONResponse(_ai_status())(scope,receive,send)
        if path=='/chart-position':
            from urllib.parse import parse_qs
            q=parse_qs(scope.get('query_string',b'').decode())
            sym=(q.get('symbol') or [''])[0]
            try:
                return await JSONResponse(_position(sym))(scope,receive,send)
            except Exception as e:
                return await JSONResponse({'error':str(e)},status_code=400)(scope,receive,send)
    await base.app(scope,receive,send)
