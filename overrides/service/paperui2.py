from __future__ import annotations

import service.paperui as base
import service.marketui as marketui
from starlette.responses import HTMLResponse

EXTRA = r'''
<div class="card" id="paperUniverseQuickCard">
  <div class="section-title">PAPER market universe</div>
  <div class="k" style="margin-bottom:10px">Choose how much of the live USDT spot market Ternary may scan/trade in PAPER mode. No control password is required here. The choice is saved and restored after refresh/restart.</div>
  <div class="row">
    <button class="secondary" onclick="setPaperUniverseQuick('25')">Top 25</button>
    <button class="secondary" onclick="setPaperUniverseQuick('50')">Top 50</button>
    <button class="secondary" onclick="setPaperUniverseQuick('100')">Top 100</button>
    <button class="secondary" onclick="setPaperUniverseQuick('ALL')">ALL</button>
    <span id="paperUniverseQuickStatus" class="pill warn">loading…</span>
  </div>
  <div class="k tiny" style="margin-top:8px">ALL may be heavy on a free Render instance. Existing PAPER positions/cash are preserved when changing universe.</div>
</div>
<script>
(function(){
  function q(id){return document.getElementById(id)}
  async function api(u,o){
    var r=await fetch(u,o||{}),t=await r.text(),d={};
    try{d=t?JSON.parse(t):{}}catch(e){throw new Error('HTTP '+r.status)}
    if(!r.ok)throw new Error(d.error||('HTTP '+r.status));
    return d
  }
  function paint(d){
    var e=q('paperUniverseQuickStatus'); if(!e)return;
    var m=String(d.paper_universe_mode||'CUSTOM').toUpperCase();
    e.textContent=m+' · '+Number(d.universe_count||0)+' symbols';
    e.className='pill up';
  }
  window.setPaperUniverseQuick=async function(mode){
    var e=q('paperUniverseQuickStatus'); if(e){e.textContent='saving…';e.className='pill warn'}
    try{
      var d=await api('/paper-universe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:mode})});
      paint(d);
      if(typeof flash==='function')flash('PAPER universe saved: '+d.paper_universe_mode+' · '+d.universe_count+' symbols');
    }catch(err){
      if(e){e.textContent='error';e.className='pill down'}
      if(typeof flash==='function')flash(err.message,true);
    }
  };
  async function refresh(){try{paint(await api('/paper-universe'))}catch(e){var s=q('paperUniverseQuickStatus');if(s){s.textContent='unavailable';s.className='pill down'}}}
  window.addEventListener('load',function(){setTimeout(refresh,500);setInterval(refresh,5000)});
})();
</script>
'''

CONTROL_HTML = marketui.CONTROL_HTML.replace('</body></html>', EXTRA + '</body></html>')

async def app(scope, receive, send):
    if scope.get('type') == 'http' and scope.get('path') == '/' and scope.get('method') == 'GET':
        return await HTMLResponse(CONTROL_HTML)(scope, receive, send)
    await base.app(scope, receive, send)
