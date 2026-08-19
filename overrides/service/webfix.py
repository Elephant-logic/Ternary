import service.api as api_mod
from service.api import app as base_app
from starlette.responses import HTMLResponse, JSONResponse


def _pick(obj, *names, default=None):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    return default


def _chart_payload(symbol=None, limit=5000):
    handles = getattr(api_mod, "_handles", {}) or {}
    worker = handles.get("worker")
    state = handles.get("state")
    if worker is None or state is None:
        raise RuntimeError("worker not ready")
    universe = list((state.goals or {}).get("universe") or [])
    if not universe:
        raise RuntimeError("universe is empty")
    symbol = symbol or universe[0]
    if symbol not in universe:
        raise ValueError("symbol is not in configured universe")
    raw = list(worker.orch.market.bars(symbol) or [])
    limit = max(20, min(int(limit or 5000), 5000))
    raw = raw[-limit:]
    bars = []
    for b in raw:
        ts = _pick(b, "ts_ns", "timestamp_ns", "ts", "time")
        o = _pick(b, "open", "o")
        h = _pick(b, "high", "h")
        l = _pick(b, "low", "l")
        c = _pick(b, "close", "c")
        v = _pick(b, "volume", "v", default=None)
        if None in (ts, o, h, l, c):
            continue
        bars.append({
            "ts_ns": int(ts), "open": float(o), "high": float(h),
            "low": float(l), "close": float(c),
            "volume": None if v is None else float(v),
        })
    pos = (worker.book.positions or {}).get(symbol) or {}
    qty = float(_pick(pos, "qty", default=0.0) or 0.0)
    interval_ns = None
    if len(bars) > 1:
        diffs = [bars[i]["ts_ns"] - bars[i - 1]["ts_ns"] for i in range(1, min(len(bars), 20))]
        diffs = [d for d in diffs if d > 0]
        if diffs:
            diffs.sort()
            interval_ns = diffs[len(diffs) // 2]
    return {
        "symbol": symbol,
        "universe": universe,
        "bars": bars,
        "position_qty": qty,
        "bar_interval_ns": interval_ns,
        "total_bars": len(bars),
    }


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Ternary Control</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0 auto;max-width:900px;padding:14px;background:#0e1420;color:#e8edf5;font:14px ui-monospace,monospace}.card{background:#141c2b;border:1px solid #243247;border-radius:14px;padding:14px;margin:12px 0}h1{color:#5ac8e0;letter-spacing:2px;font-size:18px}.tabs,.row,.ranges{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.tabs button{flex:1}.hidden{display:none}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.field{display:flex;flex-direction:column;gap:4px}.full{grid-column:1/-1}.k{color:#7f8fa6}.ok{color:#4ec99a}.bad{color:#e8695f}.warn{color:#e7b65c}button,input,select{font:inherit;border-radius:9px;padding:10px;border:1px solid #2b3a50}button{background:#5ac8e0;color:#04222b;font-weight:700}button.secondary{background:#1d2939;color:#dbe6f4}button.active{outline:2px solid #5ac8e0}button.danger{background:#e8695f;color:#2a0e0c}input,select{background:#111826;color:#e8edf5;width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#7f8fa6}.pill{padding:4px 8px;border:1px solid #243247;border-radius:999px}.flash{display:none;padding:10px;border-radius:9px;background:#152337;margin-bottom:8px}.chartwrap{height:350px;background:#0f1724;border:1px solid #243247;border-radius:10px;overflow:hidden;touch-action:none;user-select:none}.chartwrap canvas{display:block;width:100%;height:100%}.chartmeta{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:8px 0}.ranges button{padding:7px 10px}.hint{font-size:12px;color:#7f8fa6;margin-top:7px}@media(max-width:520px){.grid{grid-template-columns:1fr}.full{grid-column:1}.chartwrap{height:320px}}
</style></head><body><h1>TERN▲RY · control</h1><div id="flash" class="flash"></div><div id="status" class="card">starting…</div><div class="tabs"><button class="secondary" onclick="tab('monitor')">Monitor</button><button class="secondary" onclick="tab('settings')">⚙ Settings</button></div>
<div id="monitor"><div class="card">
<div class="row"><b style="flex:1">Market</b><select id="chartSymbol" style="width:auto;min-width:145px" onchange="loadChart()"></select></div>
<div class="ranges" style="margin-top:10px"><button id="r25" class="secondary" onclick="setRange(25)">25</button><button id="r50" class="secondary" onclick="setRange(50)">50</button><button id="r100" class="secondary active" onclick="setRange(100)">100</button><button id="rfull" class="secondary" onclick="setRange('full')">FULL</button><span id="rangeInfo" class="k"></span></div>
<div class="chartmeta"><span id="ohlc" class="k">loading candles…</span><span id="chartPos" class="k"></span></div>
<div class="chartwrap" id="chartWrap"><canvas id="candles"></canvas></div>
<div class="hint">Drag left/right to move through history. Pinch on phone or use the mouse wheel to zoom. 25 / 50 / 100 / FULL control how many candles are visible.</div></div>
<div class="card"><div id="chatlog" style="min-height:160px;background:#111826;border-radius:9px;padding:10px;margin-bottom:8px"></div><div class="row"><input id="msg" style="flex:1" placeholder="ask: why did it stop?"><button onclick="sendChat()">Send</button></div></div><div class="card"><div class="k">recent events</div><pre id="events">loading…</pre></div></div>
<div id="settings" class="hidden"><div class="card"><div class="row"><b>Control access</b><span id="lock" class="pill warn">locked</span></div><div class="k" style="margin:6px 0">Enter the TERN_CONTROL_TOKEN you set in Render.</div><div class="row"><input id="token" type="password" style="flex:1" placeholder="TERN_CONTROL_TOKEN"><button onclick="unlock()">Unlock</button><button class="secondary" onclick="forgetToken()">Forget</button></div></div>
<div class="card"><b>Trading mode</b><div class="row" style="margin-top:10px"><button class="secondary" onclick="setMode('PAPER')">PAPER</button><button onclick="setMode('LIVE')">LIVE</button><span id="modeHint" class="k"></span></div><div class="row" style="margin-top:10px"><button class="danger" onclick="arm(true)">ARM LIVE</button><button class="secondary" onclick="arm(false)">Disarm</button></div></div>
<div class="card"><b>Risk preset</b><div class="row" style="margin:10px 0"><button class="secondary" onclick="preset('conservative')">Conservative</button><button class="secondary" onclick="preset('balanced')">Balanced</button><button class="secondary" onclick="preset('aggressive')">Aggressive</button></div><div class="grid"><label class="field full">Universe<input id="universe"></label><label class="field">Max drawdown %<input id="dd" type="number" step="0.1"></label><label class="field">Daily loss %<input id="dl" type="number" step="0.1"></label><label class="field">Max position %<input id="mp" type="number" step="0.1"></label><label class="field">Max exposure %<input id="me" type="number" step="0.1"></label><label class="field">Max positions<input id="mpos" type="number"></label><label class="field">Turnover<select id="turn"><option>low</option><option>medium</option><option>high</option></select></label></div><button style="margin-top:10px" onclick="saveRisk()">Save risk settings</button></div>
<div class="card"><b>Runtime</b><div class="grid" style="margin-top:10px"><label class="field">Cycle interval seconds<input id="interval" type="number"></label><label class="field">Data source<input id="data" disabled></label><label class="field">Broker<input id="broker" disabled></label><label class="field">LLM model<input id="llm" disabled></label></div><button style="margin-top:10px" onclick="saveRuntime()">Save interval</button></div>
<div class="card"><b>Emergency</b><div class="row" style="margin-top:10px"><button class="danger" onclick="kill()">KILL SWITCH</button><button class="secondary" onclick="resume()">Resume</button></div></div><div class="card"><b>Persistence</b><div id="persist" class="k" style="margin-top:8px">loading…</div></div></div>
<script>
function q(id){return document.getElementById(id)}
function flash(m,bad){var e=q('flash');e.style.display='block';e.style.color=bad?'#ff8b84':'#9fe3c6';e.textContent=m;setTimeout(function(){e.style.display='none'},4500)}
function tab(n){q('monitor').className=n==='monitor'?'':'hidden';q('settings').className=n==='settings'?'':'hidden';if(n==='settings')loadSettings();else setTimeout(drawChart,30)}
function tok(){return sessionStorage.getItem('tern_control_token')||''}
function api(url,opt){opt=opt||{};return fetch(url,opt).then(function(r){return r.text().then(function(t){var d={};try{d=t?JSON.parse(t):{}}catch(e){throw new Error('HTTP '+r.status+': '+t.slice(0,120))}if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d})})}
function write(url,body){if(!tok())return Promise.reject(new Error('Settings locked'));return api(url,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok()},body:JSON.stringify(body)})}
function renderStatus(s){var p=s.persistence||{},l=s.llm||{};q('status').innerHTML='<b>'+s.mode+'</b> · '+(s.running?'running':'stopped')+' · cycle '+s.cycle+' · equity £'+Math.round(s.equity||0)+(s.killed?' · <span class="bad">HALTED: '+(s.kill_reason||'')+'</span>':' · <span class="ok">healthy</span>')+'<div class="k">positions: '+JSON.stringify(s.positions||{})+'</div><div class="k">LLM: '+(l.enabled?'on · '+(l.model||'configured'):'off')+' · JSON backup: '+(p.remote_json?'on':'off')+(p.restored_from_remote?' · restored':'')+'</div>'}
var chartData=null,recentEvents=[],viewCount=100,viewEnd=0,dragging=false,dragX=0,dragStartEnd=0,pinchDist=0,pinchCount=0;
function refresh(){api('/status').then(renderStatus).catch(function(e){q('status').innerHTML='<span class="bad">backend error: '+e.message+'</span>'});api('/events?limit=100').then(function(ev){recentEvents=ev;q('events').textContent=ev.slice(-8).map(function(e){return '#'+e.seq+' '+e.kind+' '+JSON.stringify(e.payload).slice(0,100)}).join('\n');if(chartData)drawChart()}).catch(function(e){q('events').textContent='events error: '+e.message})}
function intervalLabel(ns){if(!ns)return '';var s=Math.round(ns/1e9);if(s%86400===0)return (s/86400)+'d';if(s%3600===0)return (s/3600)+'h';if(s%60===0)return (s/60)+'m';return s+'s'}
function loadChart(){var sym=q('chartSymbol').value||'';api('/chart?symbol='+encodeURIComponent(sym)+'&limit=5000').then(function(d){chartData=d;var sel=q('chartSymbol'),old=sel.value;sel.innerHTML='';d.universe.forEach(function(s){var o=document.createElement('option');o.value=s;o.textContent=s;sel.appendChild(o)});sel.value=d.symbol||old;viewEnd=d.bars.length;viewCount=Math.min(viewCount,d.bars.length||viewCount);q('chartPos').textContent='position: '+Number(d.position_qty||0).toFixed(6);setRange(viewCount||100);drawChart()}).catch(function(e){q('ohlc').textContent='chart error: '+e.message})}
function setRange(n){if(!chartData)return;var total=chartData.bars.length;if(n==='full')viewCount=total;else viewCount=Math.max(10,Math.min(Number(n),total));viewEnd=total;['25','50','100','full'].forEach(function(k){var e=q('r'+k);if(e)e.classList.remove('active')});var id=n==='full'?'rfull':'r'+n;if(q(id))q(id).classList.add('active');drawChart()}
function visibleBars(){if(!chartData)return[];var total=chartData.bars.length;viewCount=Math.max(10,Math.min(viewCount,total));viewEnd=Math.max(viewCount,Math.min(viewEnd||total,total));return chartData.bars.slice(viewEnd-viewCount,viewEnd)}
function zoomAt(factor,anchor){if(!chartData)return;var total=chartData.bars.length,old=viewCount,next=Math.max(10,Math.min(total,Math.round(old*factor)));if(next===old)return;anchor=Math.max(0,Math.min(1,anchor==null?.5:anchor));var start=viewEnd-old,anchorIndex=start+old*anchor,newStart=Math.round(anchorIndex-next*anchor);newStart=Math.max(0,Math.min(total-next,newStart));viewCount=next;viewEnd=newStart+next;drawChart()}
function panPixels(dx){if(!chartData)return;var w=q('chartWrap').clientWidth||300,shift=Math.round(-dx/w*viewCount);viewEnd=Math.max(viewCount,Math.min(chartData.bars.length,dragStartEnd+shift));drawChart()}
function drawChart(){if(!chartData||!chartData.bars.length)return;var bars=visibleBars(),c=q('candles'),r=c.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(280,r.width),h=Math.max(220,r.height);c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);var x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);var padL=12,padR=58,padT=14,padB=30,pw=w-padL-padR,ph=h-padT-padB,lo=Math.min.apply(null,bars.map(function(b){return b.low})),hi=Math.max.apply(null,bars.map(function(b){return b.high}));if(hi===lo){hi+=1;lo-=1}var span=hi-lo,yp=function(v){return padT+(hi-v)/span*ph};x.strokeStyle='#243247';x.fillStyle='#7f8fa6';x.font='10px ui-monospace,monospace';for(var g=0;g<=4;g++){var yy=padT+ph*g/4,price=hi-span*g/4;x.beginPath();x.moveTo(padL,yy);x.lineTo(w-padR,yy);x.stroke();x.fillText(price.toFixed(2),w-padR+5,yy+3)}var step=pw/bars.length,bw=Math.max(1,Math.min(10,step*.68));bars.forEach(function(b,i){var cx=padL+(i+.5)*step,up=b.close>=b.open,col=up?'#4ec99a':'#e8695f';x.strokeStyle=col;x.fillStyle=col;x.beginPath();x.moveTo(cx,yp(b.high));x.lineTo(cx,yp(b.low));x.stroke();var y1=yp(Math.max(b.open,b.close)),y2=yp(Math.min(b.open,b.close));x.fillRect(cx-bw/2,y1,bw,Math.max(1,y2-y1))});var first=bars[0].ts_ns,last=bars[bars.length-1].ts_ns;recentEvents.forEach(function(e){var p=e.payload||{},side=p.side||'',ts=p.quote_ts_ns||p.ts_ns||p.source_ts_ns||null;if((side!=='BUY'&&side!=='SELL')||!ts||ts<first||ts>last)return;var i=Math.max(0,Math.min(bars.length-1,Math.round((ts-first)/(last-first||1)*(bars.length-1)))),cx=padL+(i+.5)*step,cy=side==='BUY'?h-padB-10:padT+10;x.fillStyle=side==='BUY'?'#4ec99a':'#e8695f';x.beginPath();if(side==='BUY'){x.moveTo(cx,cy-6);x.lineTo(cx-5,cy+4);x.lineTo(cx+5,cy+4)}else{x.moveTo(cx,cy+6);x.lineTo(cx-5,cy-4);x.lineTo(cx+5,cy-4)}x.closePath();x.fill()});var ticks=Math.min(5,bars.length);for(var t=0;t<ticks;t++){var idx=Math.round(t*(bars.length-1)/(ticks-1||1)),d=new Date(bars[idx].ts_ns/1e6),lab=d.toLocaleDateString(undefined,{month:'short',day:'numeric'}),xx=padL+(idx+.5)*step;x.fillStyle='#7f8fa6';x.fillText(lab,Math.min(w-padR-x.measureText(lab).width,Math.max(padL,xx-x.measureText(lab).width/2)),h-8)}var b=bars[bars.length-1];q('ohlc').textContent='O '+b.open.toFixed(2)+'  H '+b.high.toFixed(2)+'  L '+b.low.toFixed(2)+'  C '+b.close.toFixed(2)+' · '+intervalLabel(chartData.bar_interval_ns);q('rangeInfo').textContent=bars.length+' / '+chartData.bars.length+' candles'}
var wrap=q('chartWrap');wrap.addEventListener('wheel',function(e){e.preventDefault();var r=wrap.getBoundingClientRect(),a=(e.clientX-r.left)/r.width;zoomAt(e.deltaY>0?1.2:.82,a)},{passive:false});wrap.addEventListener('pointerdown',function(e){if(e.pointerType==='touch')return;dragging=true;dragX=e.clientX;dragStartEnd=viewEnd;wrap.setPointerCapture(e.pointerId)});wrap.addEventListener('pointermove',function(e){if(dragging)panPixels(e.clientX-dragX)});wrap.addEventListener('pointerup',function(){dragging=false});wrap.addEventListener('touchstart',function(e){if(e.touches.length===1){dragging=true;dragX=e.touches[0].clientX;dragStartEnd=viewEnd}else if(e.touches.length===2){dragging=false;pinchDist=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);pinchCount=viewCount}},{passive:false});wrap.addEventListener('touchmove',function(e){e.preventDefault();if(e.touches.length===1&&dragging){panPixels(e.touches[0].clientX-dragX)}else if(e.touches.length===2&&pinchDist){var d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY),mid=(e.touches[0].clientX+e.touches[1].clientX)/2,r=wrap.getBoundingClientRect(),anchor=(mid-r.left)/r.width,f=pinchDist/d;viewCount=Math.max(10,Math.min(chartData.bars.length,Math.round(pinchCount*f)));var total=chartData.bars.length,start=Math.max(0,Math.min(total-viewCount,viewEnd-pinchCount+Math.round((pinchCount-viewCount)*anchor)));viewEnd=start+viewCount;drawChart()}},{passive:false});wrap.addEventListener('touchend',function(){dragging=false;pinchDist=0});
function unlock(){var v=q('token').value.trim();if(!v)return flash('Enter token',true);sessionStorage.setItem('tern_control_token',v);write('/auth-check',{}).then(function(){q('lock').textContent='unlocked';q('lock').className='pill ok';q('token').value='';flash('Unlocked')}).catch(function(e){sessionStorage.removeItem('tern_control_token');flash(e.message,true)})}
function forgetToken(){sessionStorage.removeItem('tern_control_token');q('lock').textContent='locked';q('lock').className='pill warn'}
function loadSettings(){api('/settings').then(function(s){var g=s.goals||{},p=s.persistence||{};q('modeHint').textContent='current: '+s.mode+(s.live_armed?' · ARMED':'');q('universe').value=(g.universe||[]).join(', ');q('dd').value=((g.max_drawdown||0)*100).toFixed(1);q('dl').value=((g.max_daily_loss||0)*100).toFixed(1);q('mp').value=((g.max_position_pct||0)*100).toFixed(1);q('me').value=((g.max_exposure_pct||0)*100).toFixed(1);q('mpos').value=g.max_positions||5;q('turn').value=g.turnover||'low';q('interval').value=s.interval_seconds||60;q('data').value=s.data_source||'';q('broker').value=s.broker||'';q('llm').value=s.llm_model||'off';q('persist').textContent='backend: '+(p.remote_backend||'local JSON only')+' · remote: '+(p.remote_json?'on':'off')+' · restored: '+(p.restored_from_remote?'yes':'no')+(p.error?' · ERROR: '+p.error:'')}).catch(function(e){flash('Settings error: '+e.message,true)})}
function setMode(m){if(m==='LIVE'&&!confirm('Switch to LIVE? This does not arm orders.'))return;write('/mode',{mode:m}).then(function(){flash('Mode set to '+m);loadSettings();refresh()}).catch(function(e){flash(e.message,true)})}
function arm(on){if(on&&!confirm('ARM LIVE ORDER PLACEMENT?'))return;write('/arm',{armed:on,confirm:on}).then(function(){flash(on?'LIVE armed':'LIVE disarmed');loadSettings();refresh()}).catch(function(e){flash(e.message,true)})}
function preset(n){if(n==='aggressive'&&!confirm('Apply aggressive preset?'))return;write('/preset',{name:n}).then(function(){flash(n+' preset applied');loadSettings()}).catch(function(e){flash(e.message,true)})}
function saveRisk(){var u=q('universe').value.split(',').map(function(x){return x.trim()}).filter(Boolean);var goals={universe:u,max_drawdown:Number(q('dd').value)/100,max_daily_loss:Number(q('dl').value)/100,max_position_pct:Number(q('mp').value)/100,max_exposure_pct:Number(q('me').value)/100,max_positions:Number(q('mpos').value),turnover:q('turn').value};write('/goals',{goals:goals}).then(function(){flash('Risk settings saved');loadSettings();loadChart()}).catch(function(e){flash(e.message,true)})}
function saveRuntime(){write('/runtime-settings',{settings:{interval_seconds:Number(q('interval').value)}}).then(function(){flash('Interval saved');loadSettings()}).catch(function(e){flash(e.message,true)})}
function kill(){if(!confirm('Engage kill switch?'))return;write('/kill',{reason:'dashboard_manual'}).then(function(){flash('KILL SWITCH ENGAGED',true);refresh()}).catch(function(e){flash(e.message,true)})}
function resume(){if(!confirm('Resume governed trading?'))return;write('/resume',{reason:'dashboard_manual'}).then(function(){flash('Resumed');refresh()}).catch(function(e){flash(e.message,true)})}
function sendChat(){var m=q('msg').value;if(!m)return;q('msg').value='';api('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})}).then(function(r){q('chatlog').textContent+='\n'+(r.reply||'No reply')}).catch(function(e){q('chatlog').textContent+='\nerror: '+e.message})}
if(tok()){q('lock').textContent='unlocked';q('lock').className='pill ok'}refresh();loadChart();setInterval(refresh,3000);window.addEventListener('resize',drawChart);
</script></body></html>'''


async def app(scope, receive, send):
    if scope.get("type") == "http" and scope.get("path") == "/" and scope.get("method") == "GET":
        await HTMLResponse(HTML)(scope, receive, send)
        return
    if scope.get("type") == "http" and scope.get("path") == "/chart" and scope.get("method") == "GET":
        try:
            qs = scope.get("query_string", b"").decode()
            from urllib.parse import parse_qs
            args = parse_qs(qs)
            symbol = (args.get("symbol") or [None])[0]
            limit = int((args.get("limit") or [5000])[0])
            response = JSONResponse(_chart_payload(symbol, limit))
        except Exception as exc:
            response = JSONResponse({"error": str(exc)}, status_code=400)
        await response(scope, receive, send)
        return
    await base_app(scope, receive, send)
