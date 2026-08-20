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
      var eq=Number(s.equity||0),cash=Number(s.cash||0),start=10000,invested=eq-cash;
      var pnl=eq-start,pct=start?100*pnl/start:0;
      var delta=(lastEq===null||lastCycle===s.cycle)?null:eq-lastEq;
      var d=document.createElement('div'); d.id='paperPnlLine'; d.className='k';
      var sign=function(v){return (v>=0?'+':'')+v.toFixed(2)};
      d.innerHTML='PAPER equity: <b>£'+eq.toFixed(2)+'</b> · P&amp;L <span style="color:'+(pnl>=0?'#4ec99a':'#e8695f')+'">£'+sign(pnl)+' ('+sign(pct)+'%)</span> · cash £'+cash.toFixed(2)+' · invested £'+invested.toFixed(2)+(delta===null?'':' · Δ cycle <span style="color:'+(delta>=0?'#4ec99a':'#e8695f')+'">£'+sign(delta)+'</span>');
      box.appendChild(d);
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
