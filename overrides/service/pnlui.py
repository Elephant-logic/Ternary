from __future__ import annotations
import service.finalfix as base
import service.marketui as marketui

EXTRA = r'''
<script>
(function(){
  var lastEq=null,lastCycle=null;
  function byId(x){return document.getElementById(x)}
  async function getStatus(){
    try{
      var r=await fetch('/status');
      var s=await r.json();
      if(!r.ok)throw new Error(s.error||('HTTP '+r.status));
      var box=byId('status'); if(!box)return;
      var old=byId('paperPnlLine'); if(old)old.remove();
      var oldm=byId('paperMarketLine'); if(oldm)oldm.remove();
      var eq=Number(s.equity||0),cash=Number(s.cash||0),start=10000,invested=eq-cash;
      var pnl=eq-start,pct=start?100*pnl/start:0;
      var delta=(lastEq===null||lastCycle===s.cycle)?null:eq-lastEq;
      var d=document.createElement('div'); d.id='paperPnlLine'; d.className='k';
      var sign=function(v){return (v>=0?'+':'')+v.toFixed(2)};
      d.innerHTML='PAPER equity: <b>£'+eq.toFixed(2)+'</b> · P&amp;L <span style="color:'+(pnl>=0?'#4ec99a':'#e8695f')+'">£'+sign(pnl)+' ('+sign(pct)+'%)</span> · cash £'+cash.toFixed(2)+' · invested £'+invested.toFixed(2)+(delta===null?'':' · Δ cycle <span style="color:'+(delta>=0?'#4ec99a':'#e8695f')+'">£'+sign(delta)+'</span>');
      box.appendChild(d);
      var mh=s.market_health||{},m=document.createElement('div');m.id='paperMarketLine';m.className='k';
      var age=(mh.max_age_s===null||mh.max_age_s===undefined)?'n/a':Number(mh.max_age_s).toFixed(0)+'s';
      m.textContent='PAPER market: '+(s.data_source||'unknown')+' · provider '+(mh.provider||'connecting')+' · data age '+age+(mh.errors&&Object.keys(mh.errors).length?' · partial errors '+Object.keys(mh.errors).length:'');
      box.appendChild(m);
      lastEq=eq; lastCycle=s.cycle;
    }catch(e){}
  }
  window.addEventListener('load',function(){setTimeout(getStatus,700);setInterval(getStatus,3000)});
})();
</script>
'''

if EXTRA not in marketui.CONTROL_HTML:
    marketui.CONTROL_HTML = marketui.CONTROL_HTML.replace('</body></html>', EXTRA + '</body></html>')

app = base.app
