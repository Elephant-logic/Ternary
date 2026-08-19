from __future__ import annotations
import json, time, urllib.parse, urllib.request
import service.webfix as webfix
from starlette.responses import HTMLResponse, JSONResponse

_CACHE={"symbols":None,"ts":0.0}

def _json(url,timeout=12):
    req=urllib.request.Request(url,headers={"User-Agent":"ternary-market-browser/1.0"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def _symbols():
    now=time.time()
    if _CACHE["symbols"] and now-_CACHE["ts"]<120:return _CACHE["symbols"]
    info=_json("https://api.binance.com/api/v3/exchangeInfo")
    ticks=_json("https://api.binance.com/api/v3/ticker/24hr")
    vols={t.get("symbol"):float(t.get("quoteVolume") or 0) for t in ticks if t.get("symbol")}
    rows=[]
    for s in info.get("symbols",[]):
        if s.get("status")!="TRADING" or s.get("quoteAsset")!="USDT":continue
        perms=s.get("permissions") or []
        if perms and "SPOT" not in perms:continue
        base=s.get("baseAsset"); ex=s.get("symbol")
        if base and ex:rows.append({"symbol":base+"/USDT","exchange_symbol":ex,"quote_volume_24h":vols.get(ex,0.0)})
    rows.sort(key=lambda x:x["quote_volume_24h"],reverse=True)
    _CACHE["symbols"],_CACHE["ts"]=rows,now
    return rows

def _candles(symbol,interval="1h",limit=1000):
    symbol=(symbol or "").upper().strip()
    if symbol.endswith("/USDT"):ex=symbol.replace("/","")
    elif symbol.endswith("USDT"):ex=symbol;symbol=symbol[:-4]+"/USDT"
    else:raise ValueError("symbol must be a USDT pair")
    allowed={"1m","5m","15m","30m","1h","4h","1d","1w"}
    if interval not in allowed:raise ValueError("unsupported interval")
    limit=max(25,min(int(limit),1000))
    url="https://api.binance.com/api/v3/klines?"+urllib.parse.urlencode({"symbol":ex,"interval":interval,"limit":limit})
    raw=_json(url)
    bars=[{"ts_ms":int(k[0]),"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4]),"volume":float(k[5])} for k in raw]
    return {"symbol":symbol,"interval":interval,"bars":bars}

MARKET_HTML=r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Ternary Market</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0 auto;max-width:1100px;padding:14px;background:#0e1420;color:#e8edf5;font:14px ui-monospace,monospace}h1{color:#5ac8e0;letter-spacing:2px;font-size:18px}.card{background:#141c2b;border:1px solid #243247;border-radius:14px;padding:14px;margin:12px 0}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.k{color:#7f8fa6}button,input,select{font:inherit;border-radius:9px;padding:9px;border:1px solid #2b3a50}button{background:#5ac8e0;color:#04222b;font-weight:700}button.secondary{background:#1d2939;color:#dbe6f4}input,select{background:#111826;color:#e8edf5}.grid{display:grid;grid-template-columns:280px 1fr;gap:12px}.list{max-height:560px;overflow:auto;background:#0f1724;border-radius:10px;padding:6px}.sym{display:flex;justify-content:space-between;padding:9px;border-bottom:1px solid #1e2b3e;cursor:pointer}.sym:hover,.sym.active{background:#1a2940}.chartwrap{height:500px;background:#0f1724;border:1px solid #243247;border-radius:10px;overflow:hidden;touch-action:none;user-select:none}.chartwrap canvas{display:block;width:100%;height:100%}@media(max-width:760px){.grid{grid-template-columns:1fr}.list{max-height:240px}.chartwrap{height:360px}}
</style></head><body><div class="row"><h1 style="flex:1">TERN▲RY · market</h1><button class="secondary" onclick="location.href='/'">← Control</button></div>
<div class="card"><div class="row"><b style="flex:1">Symbols</b><button class="secondary" onclick="setCount(25)">Top 25</button><button class="secondary" onclick="setCount(50)">50</button><button class="secondary" onclick="setCount(100)">100</button><button class="secondary" onclick="setCount(0)">ALL</button></div><div class="row" style="margin-top:8px"><input id="search" placeholder="search symbol…" style="flex:1" oninput="renderSymbols()"><select id="interval" onchange="loadChart()"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option selected>1h</option><option>4h</option><option>1d</option><option>1w</option></select><button class="secondary" onclick="zoomPreset(25)">25 candles</button><button class="secondary" onclick="zoomPreset(50)">50</button><button class="secondary" onclick="zoomPreset(100)">100</button><button class="secondary" onclick="zoomPreset(0)">FULL</button></div></div>
<div class="grid"><div class="card"><div id="count" class="k"></div><div id="symbols" class="list">loading…</div></div><div class="card"><div id="meta" class="k" style="margin-bottom:8px">choose a symbol</div><div class="chartwrap"><canvas id="chart"></canvas></div><div class="k" style="margin-top:7px">Drag to pan · pinch/mouse-wheel to zoom. Viewing a symbol does not add it to the trading universe.</div></div></div>
<script>
function q(i){return document.getElementById(i)}function api(u){return fetch(u).then(function(r){return r.text().then(function(t){var d=JSON.parse(t);if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d})})}
var all=[],shown=25,selected='',data=null,viewN=100,viewEnd=0;
function loadSymbols(){api('/market/symbols').then(function(d){all=d.symbols||[];renderSymbols();if(all.length){selected=all[0].symbol;loadChart()}}).catch(function(e){q('symbols').textContent='market unavailable: '+e.message})}
function setCount(n){shown=n;renderSymbols()}function fmt(v){v=Number(v||0);if(v>=1e9)return '$'+(v/1e9).toFixed(1)+'B';if(v>=1e6)return '$'+(v/1e6).toFixed(1)+'M';if(v>=1e3)return '$'+(v/1e3).toFixed(1)+'K';return '$'+v.toFixed(0)}
function renderSymbols(){var t=(q('search').value||'').toUpperCase(),a=all.filter(function(s){return !t||s.symbol.indexOf(t)>=0});if(shown)a=a.slice(0,shown);q('count').textContent=(shown?'Top '+shown:'ALL')+' · '+a.length+' shown · '+all.length+' available';q('symbols').innerHTML='';a.forEach(function(s){var d=document.createElement('div');d.className='sym'+(s.symbol===selected?' active':'');d.innerHTML='<span>'+s.symbol+'</span><span class="k">'+fmt(s.quote_volume_24h)+'</span>';d.onclick=function(){selected=s.symbol;renderSymbols();loadChart()};q('symbols').appendChild(d)})}
function loadChart(){if(!selected)return;var iv=q('interval').value;api('/market/candles?symbol='+encodeURIComponent(selected)+'&interval='+encodeURIComponent(iv)+'&limit=1000').then(function(d){data=d;viewEnd=d.bars.length;viewN=Math.min(viewN||100,d.bars.length);var b=d.bars[d.bars.length-1];q('meta').textContent=d.symbol+' · '+iv+' · '+d.bars.length+' bars · O '+b.open.toFixed(4)+' H '+b.high.toFixed(4)+' L '+b.low.toFixed(4)+' C '+b.close.toFixed(4);draw()}).catch(function(e){q('meta').textContent='chart unavailable: '+e.message})}
function zoomPreset(n){if(!data)return;viewN=n?Math.min(n,data.bars.length):data.bars.length;viewEnd=data.bars.length;draw()}function visible(){if(!data)return[];var n=Math.max(10,Math.min(viewN,data.bars.length)),e=Math.max(n,Math.min(viewEnd,data.bars.length));return data.bars.slice(e-n,e)}
function draw(){var b=visible();if(!b.length)return;var c=q('chart'),r=c.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(280,r.width),h=Math.max(240,r.height);c.width=w*dpr;c.height=h*dpr;var x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);var L=10,R=62,T=12,B=30,pw=w-L-R,ph=h-T-B,lo=Math.min.apply(null,b.map(function(z){return z.low})),hi=Math.max.apply(null,b.map(function(z){return z.high}));if(hi===lo){hi++;lo--}var sp=hi-lo,yp=function(v){return T+(hi-v)/sp*ph};x.font='10px monospace';for(var g=0;g<=4;g++){var y=T+ph*g/4;x.strokeStyle='#243247';x.beginPath();x.moveTo(L,y);x.lineTo(w-R,y);x.stroke();x.fillStyle='#7f8fa6';x.fillText((hi-sp*g/4).toFixed(4),w-R+4,y+3)}var step=pw/b.length,bw=Math.max(1,Math.min(9,step*.66));b.forEach(function(z,i){var cx=L+(i+.5)*step,col=z.close>=z.open?'#4ec99a':'#e8695f';x.strokeStyle=col;x.fillStyle=col;x.beginPath();x.moveTo(cx,yp(z.high));x.lineTo(cx,yp(z.low));x.stroke();var y1=yp(Math.max(z.open,z.close)),y2=yp(Math.min(z.open,z.close));x.fillRect(cx-bw/2,y1,bw,Math.max(1,y2-y1))});x.fillStyle='#7f8fa6';x.fillText(new Date(b[0].ts_ms).toLocaleString(),L,h-8);var t=new Date(b[b.length-1].ts_ms).toLocaleString();x.fillText(t,w-R-x.measureText(t).width,h-8)}
var drag=false,lastX=0;q('chart').addEventListener('pointerdown',function(e){drag=true;lastX=e.clientX;q('chart').setPointerCapture(e.pointerId)});q('chart').addEventListener('pointermove',function(e){if(!drag||!data)return;var dx=e.clientX-lastX;if(Math.abs(dx)>8){var sh=Math.round(-dx/8);viewEnd=Math.max(viewN,Math.min(data.bars.length,viewEnd+sh));lastX=e.clientX;draw()}});q('chart').addEventListener('pointerup',function(){drag=false});q('chart').addEventListener('wheel',function(e){if(!data)return;e.preventDefault();viewN=Math.max(10,Math.min(data.bars.length,Math.round(viewN*(e.deltaY>0?1.18:.84))));viewEnd=Math.max(viewN,Math.min(viewEnd,data.bars.length));draw()},{passive:false});var pinch=0;q('chart').addEventListener('touchstart',function(e){if(e.touches.length===2)pinch=Math.abs(e.touches[0].clientX-e.touches[1].clientX)},{passive:true});q('chart').addEventListener('touchmove',function(e){if(e.touches.length===2&&pinch&&data){var d=Math.abs(e.touches[0].clientX-e.touches[1].clientX),ratio=pinch/d;viewN=Math.max(10,Math.min(data.bars.length,Math.round(viewN*ratio)));pinch=d;draw()}},{passive:true});loadSymbols();
</script></body></html>'''

CONTROL_HTML=webfix.HTML.replace('<button class="secondary" onclick="tab(\'settings\')">⚙ Settings</button>','<button class="secondary" onclick="location.href=\'/market\'">Market</button><button class="secondary" onclick="tab(\'settings\')">⚙ Settings</button>')

async def app(scope,receive,send):
    if scope.get("type")=="http":
        path=scope.get("path");method=scope.get("method")
        if path=="/" and method=="GET":return await HTMLResponse(CONTROL_HTML)(scope,receive,send)
        if path=="/market" and method=="GET":return await HTMLResponse(MARKET_HTML)(scope,receive,send)
        if path=="/market/symbols" and method=="GET":
            try:return await JSONResponse({"symbols":_symbols(),"source":"binance_spot_usdt"})(scope,receive,send)
            except Exception as e:return await JSONResponse({"error":str(e)},status_code=502)(scope,receive,send)
        if path=="/market/candles" and method=="GET":
            p=urllib.parse.parse_qs(scope.get("query_string",b"").decode())
            try:return await JSONResponse(_candles((p.get("symbol")or[""])[0],(p.get("interval")or["1h"])[0],int((p.get("limit")or["1000"])[0])))(scope,receive,send)
            except Exception as e:return await JSONResponse({"error":str(e)},status_code=400)(scope,receive,send)
    await webfix.app(scope,receive,send)
