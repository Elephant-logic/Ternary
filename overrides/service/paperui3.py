from __future__ import annotations

import service.paperui2 as base
from starlette.responses import HTMLResponse

# The trading universe and the market browser are deliberately separate:
# - /paper-universe controls what the PAPER engine may trade.
# - /market/symbols + /market/candles let the dashboard inspect every public
#   USDT spot symbol exposed by the market providers, whether or not it is in
#   the current trading universe.
EXTRA = r'''
<script>
(function(){
  function el(id){return document.getElementById(id)}
  async function jfetch(url,opt){
    var r=await fetch(url,opt||{}),t=await r.text(),d={};
    try{d=t?JSON.parse(t):{}}catch(e){throw new Error('HTTP '+r.status)}
    if(!r.ok)throw new Error(d.error||('HTTP '+r.status));
    return d;
  }

  var browserSymbols=[];
  var paperPositions={};

  function fillBrowserSymbols(preferred){
    var sel=el('chartSymbol');
    if(!sel||!browserSymbols.length)return;
    var old=preferred||sel.value||'';
    sel.innerHTML='';
    browserSymbols.forEach(function(row){
      var sym=typeof row==='string'?row:row.symbol;
      if(!sym)return;
      var o=document.createElement('option');o.value=sym;o.textContent=sym;sel.appendChild(o);
    });
    if(old && browserSymbols.some(function(x){return (typeof x==='string'?x:x.symbol)===old}))sel.value=old;
    else sel.value=(typeof browserSymbols[0]==='string'?browserSymbols[0]:browserSymbols[0].symbol);
  }

  async function refreshPaperPositions(){
    try{var p=await jfetch('/paper-universe');paperPositions=p.positions||{};}catch(e){}
  }

  // Override the original universe-bound chart loader. Public chart candles are
  // research/display data only; PAPER accounting continues to use the worker's
  // own frozen market snapshot and simulated broker.
  window.loadChart=async function(){
    var sel=el('chartSymbol');if(!sel)return;
    var sym=sel.value||'';if(!sym)return;
    try{
      var d=await jfetch('/market/candles?symbol='+encodeURIComponent(sym)+'&interval=1h&limit=1000');
      var bars=(d.bars||[]).map(function(b){return {
        ts_ns:Number(b.ts_ms||0)*1000000,
        open:Number(b.open),high:Number(b.high),low:Number(b.low),close:Number(b.close),
        volume:b.volume==null?null:Number(b.volume)
      }}).filter(function(b){return b.ts_ns&&isFinite(b.open)&&isFinite(b.high)&&isFinite(b.low)&&isFinite(b.close)});
      var intervalNs=null;
      if(bars.length>1)intervalNs=Math.max(1,bars[bars.length-1].ts_ns-bars[bars.length-2].ts_ns);
      chartData={symbol:d.symbol||sym,bars:bars,position_qty:Number(paperPositions[sym]||0),bar_interval_ns:intervalNs,total_bars:bars.length};
      viewEnd=bars.length;viewCount=Math.min(viewCount||100,bars.length||100);
      el('chartPos').textContent='position: '+Number(paperPositions[sym]||0).toFixed(6);
      setRange(viewCount||100);drawChart();
    }catch(e){el('ohlc').textContent='chart error: '+e.message;}
  };

  async function initialiseFullMarketBrowser(){
    try{
      await refreshPaperPositions();
      var d=await jfetch('/market/symbols');
      browserSymbols=d.symbols||[];
      var current=el('chartSymbol')&&el('chartSymbol').value;
      fillBrowserSymbols(current);
      var sel=el('chartSymbol');
      if(sel){sel.title='Full public USDT spot market · '+browserSymbols.length+' symbols';}
      await window.loadChart();
    }catch(e){var o=el('ohlc');if(o)o.textContent='market browser unavailable: '+e.message;}
  }

  window.addEventListener('load',function(){setTimeout(initialiseFullMarketBrowser,350);setInterval(refreshPaperPositions,5000)});
})();
</script>
'''

CONTROL_HTML = base.CONTROL_HTML
CONTROL_HTML = CONTROL_HTML.replace(
    '<div class="row"><b style="flex:1">Market</b><select id="chartSymbol"',
    '<div class="row"><b style="flex:1">Market browser <span class="k" style="font-weight:normal">· full public USDT list</span></b><select id="chartSymbol"'
)
CONTROL_HTML = CONTROL_HTML.replace(
    '<div class="ranges" style="margin-top:10px"><button id="r25"',
    '<div class="ranges" style="margin-top:10px"><span class="k">Candles:</span><button id="r25"'
)
CONTROL_HTML = CONTROL_HTML.replace(
    '25 / 50 / 100 / FULL control how many candles are visible.',
    'These 25 / 50 / 100 / FULL buttons control candle zoom only. The separate PAPER market universe controls choose what Ternary may trade.'
)
CONTROL_HTML = CONTROL_HTML.replace('</body></html>', EXTRA + '</body></html>')


async def app(scope, receive, send):
    if scope.get('type') == 'http' and scope.get('path') == '/' and scope.get('method') == 'GET':
        return await HTMLResponse(CONTROL_HTML)(scope, receive, send)
    await base.app(scope, receive, send)
