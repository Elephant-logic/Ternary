import json
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


def _chart_payload(symbol: str | None, limit: int = 120):
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
    bars = list(worker.orch.market.bars(symbol) or [])[-max(20, min(int(limit), 300)):]
    out = []
    for b in bars:
        ts = _pick(b, "ts_ns", "timestamp_ns", "ts", "time")
        o = _pick(b, "open", "o")
        h = _pick(b, "high", "h")
        l = _pick(b, "low", "l")
        c = _pick(b, "close", "c")
        v = _pick(b, "volume", "v", default=None)
        if None in (ts, o, h, l, c):
            continue
        out.append({"ts_ns": int(ts), "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": None if v is None else float(v)})
    pos = (worker.book.positions or {}).get(symbol) or {}
    qty = _pick(pos, "qty", default=0.0) or 0.0
    return {"symbol": symbol, "universe": universe, "bars": out, "position_qty": float(qty)}


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Ternary Control</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0 auto;max-width:900px;padding:14px;background:#0e1420;color:#e8edf5;font:14px ui-monospace,monospace}.card{background:#141c2b;border:1px solid #243247;border-radius:14px;padding:14px;margin:12px 0}h1{color:#5ac8e0;letter-spacing:2px;font-size:18px}.tabs{display:flex;gap:8px}.tabs button{flex:1}.hidden{display:none}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.field{display:flex;flex-direction:column;gap:4px}.full{grid-column:1/-1}.k{color:#7f8fa6}.ok{color:#4ec99a}.bad{color:#e8695f}.warn{color:#e7b65c}button,input,select{font:inherit;border-radius:9px;padding:10px;border:1px solid #2b3a50}button{background:#5ac8e0;color:#04222b;font-weight:700}button.secondary{background:#1d2939;color:#dbe6f4}button.danger{background:#e8695f;color:#2a0e0c}input,select{background:#111826;color:#e8edf5;width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#7f8fa6}.pill{padding:4px 8px;border:1px solid #243247;border-radius:999px}.flash{display:none;padding:10px;border-radius:9px;background:#152337;margin-bottom:8px}.chartwrap{height:330px;background:#0f1724;border:1px solid #243247;border-radius:10px;overflow:hidden}.chartwrap canvas{display:block;width:100%;height:100%}.chartmeta{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:8px 0}.chartmeta b{color:#dce8f7}@media(max-width:520px){.grid{grid-template-columns:1fr}.full{grid-column:1}.chartwrap{height:300px}}
</style></head><body><h1>TERN▲RY · control</h1><div id="flash" class="flash"></div><div id="status" class="card">starting…</div><div class="tabs"><button class="secondary" onclick="tab('monitor')">Monitor</button><button class="secondary" onclick="tab('settings')">⚙ Settings</button></div>
<div id="monitor">
<div class="card"><div class="row"><b style="flex:1">Market</b><select id="chartSymbol" style="width:auto;min-width:140px" onchange="loadChart()"></select></div><div class="chartmeta"><span id="ohlc" class="k">loading candles…</span><span id="chartPos" class="k"></span></div><div class="chartwrap"><canvas id="candles"></canvas></div><div class="k" style="margin-top:7px">Candles use the exact bar store seen by the Ternary worker. Green/red markers show recent BUY/SELL events when their market timestamp can be matched.</div></div>
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
var chartData=null, recentEvents=[];
function refresh(){api('/status').then(renderStatus).catch(function(e){q('status').innerHTML='<span class="bad">backend error: '+e.message+'</span>'});api('/events?limit=100').then(function(ev){recentEvents=ev;q('events').textContent=ev.slice(-8).map(function(e){return '#'+e.seq+' '+e.kind+' '+JSON.stringify(e.payload).slice(0,100)}).join('\n');if(chartData)drawChart()}).catch(function(e){q('events').textContent='events error: '+e.message})}
function loadChart(){var sym=q('chartSymbol').value||'';api('/chart?symbol='+encodeURIComponent(sym)+'&limit=120').then(function(d){chartData=d;var sel=q('chartSymbol');if(!sel.options.length){d.universe.forEach(function(s){var o=document.createElement('option');o.value=s;o.textContent=s;sel.appendChild(o)});sel.value=d.symbol}q('chartPos').textContent='position: '+Number(d.position_qty||0).toFixed(6);var b=d.bars[d.bars.length-1];q('ohlc').textContent=b?('O '+b.open.toFixed(2)+'  H '+b.high.toFixed(2)+'  L '+b.low.toFixed(2)+'  C '+b.close.toFixed(2)):'no bars';drawChart()}).catch(function(e){q('ohlc').textContent='chart error: '+e.message})}
function drawChart(){if(!chartData||!chartData.bars||!chartData.bars.length)return;var c=q('candles'),r=c.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(280,r.width),h=Math.max(220,r.height);c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);var x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);var bars=chartData.bars,padL=12,padR=54,padT=14,padB=24,pw=w-padL-padR,ph=h-padT-padB;var lo=Math.min.apply(null,bars.map(function(b){return b.low})),hi=Math.max.apply(null,bars.map(function(b){return b.high}));if(hi===lo){hi+=1;lo-=1}var span=hi-lo,yp=function(v){return padT+(hi-v)/span*ph};x.strokeStyle='#243247';x.fillStyle='#7f8fa6';x.font='10px ui-monospace,monospace';for(var g=0;g<=4;g++){var yy=padT+ph*g/4,price=hi-span*g/4;x.beginPath();x.moveTo(padL,yy);x.lineTo(w-padR,yy);x.stroke();x.fillText(price.toFixed(2),w-padR+5,yy+3)}var step=pw/bars.length,bw=Math.max(2,Math.min(8,step*.65));bars.forEach(function(b,i){var cx=padL+(i+.5)*step,up=b.close>=b.open,col=up?'#4ec99a':'#e8695f';x.strokeStyle=col;x.fillStyle=col;x.beginPath();x.moveTo(cx,yp(b.high));x.lineTo(cx,yp(b.low));x.stroke();var y1=yp(Math.max(b.open,b.close)),y2=yp(Math.min(b.open,b.close));x.fillRect(cx-bw/2,y1,bw,Math.max(1,y2-y1))});var first=bars[0].ts_ns,last=bars[bars.length-1].ts_ns;recentEvents.forEach(function(e){var p=e.payload||{},side=p.side||'',ts=p.quote_ts_ns||p.ts_ns||p.source_ts_ns||null;if((side!=='BUY'&&side!=='SELL')||!ts||ts<first||ts>last)return;var i=Math.max(0,Math.min(bars.length-1,Math.round((ts-first)/(last-first||1)*(bars.length-1)))),cx=padL+(i+.5)*step,cy=side==='BUY'?h-padB-8:padT+8;x.fillStyle=side==='BUY'?'#4ec99a':'#e8695f';x.beginPath();if(side==='BUY'){x.moveTo(cx,cy-6);x.lineTo(cx-5,cy+4);x.lineTo(cx+5,cy+4)}else{x.moveTo(cx,cy+6);x.lineTo(cx-5,cy-4);x.lineTo(cx+5,cy-4)}x.closePath();x.fill()});var d0=new Date(first/1e6),d1=new Date(last/1e6);x.fillStyle='#7f8fa6';x.fillText(d0.toLocaleDateString(),padL,h-7);var end=d1.toLocaleDateString();x.fillText(end,w-padR-x.measureText(end).width,h-7)}
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
if(tok()){q('lock').textContent='unlocked';q('lock').className='pill ok'}refresh();loadChart();setInterval(refresh,3000);setInterval(loadChart,15000);window.addEventListener('resize',function(){setTimeout(drawChart,80)});
</script></body></html>'''


async def app(scope, receive, send):
    if scope.get('type') == 'http' and scope.get('path') == '/' and scope.get('method') == 'GET':
        response = HTMLResponse(HTML)
        await response(scope, receive, send)
        return
    if scope.get('type') == 'http' and scope.get('path') == '/chart' and scope.get('method') == 'GET':
        try:
            raw = scope.get('query_string', b'').decode('utf-8', 'replace')
            from urllib.parse import parse_qs
            qs = parse_qs(raw)
            symbol = (qs.get('symbol') or [None])[0]
            limit = int((qs.get('limit') or ['120'])[0])
            response = JSONResponse(_chart_payload(symbol, limit))
        except ValueError as exc:
            response = JSONResponse({'error': str(exc)}, status_code=400)
        except Exception as exc:
            response = JSONResponse({'error': 'chart unavailable: ' + str(exc)}, status_code=503)
        await response(scope, receive, send)
        return
    await base_app(scope, receive, send)
