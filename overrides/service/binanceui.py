from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import service.paperui3 as base
import service.api as api_mod
from starlette.responses import HTMLResponse, JSONResponse

BINANCE_BASE = os.environ.get("BINANCE_API_BASE", "https://api.binance.com").rstrip("/")


def _auth_reason(scope):
    auth = None
    for k, v in scope.get("headers", []):
        if k.lower() == b"authorization":
            auth = v.decode("utf-8", "replace")
            break
    return api_mod._control_auth_reason(auth)


def _json_request(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ternary-binance/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Binance HTTP {exc.code}: {detail}") from exc


def _binance_configured():
    return bool(os.environ.get("TERN_LIVE_API_KEY") and os.environ.get("TERN_LIVE_API_SECRET"))


def _signed_get(path, params=None):
    key = os.environ.get("TERN_LIVE_API_KEY")
    secret = os.environ.get("TERN_LIVE_API_SECRET")
    if not key or not secret:
        raise RuntimeError("Binance API credentials are not configured in Render")
    params = dict(params or {})
    server = _json_request(BINANCE_BASE + "/api/v3/time")
    params["timestamp"] = int(server.get("serverTime") or time.time() * 1000)
    params["recvWindow"] = 5000
    query = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    url = BINANCE_BASE + path + "?" + query + "&signature=" + sig
    return _json_request(url, headers={"X-MBX-APIKEY": key, "User-Agent": "ternary-binance/1.0"})


def _account_summary():
    account = _signed_get("/api/v3/account", {"omitZeroBalances": "true"})
    tickers = _json_request(BINANCE_BASE + "/api/v3/ticker/price")
    prices = {str(t.get("symbol")): float(t.get("price") or 0.0) for t in tickers if t.get("symbol")}
    rows = []
    total_usdt = 0.0
    unpriced = []
    for b in account.get("balances", []) or []:
        asset = str(b.get("asset") or "")
        free = float(b.get("free") or 0.0)
        locked = float(b.get("locked") or 0.0)
        qty = free + locked
        if qty <= 0:
            continue
        if asset == "USDT":
            price = 1.0
        else:
            price = prices.get(asset + "USDT")
        value = qty * price if price else None
        if value is not None:
            total_usdt += value
        else:
            unpriced.append(asset)
        rows.append({
            "asset": asset,
            "free": free,
            "locked": locked,
            "qty": qty,
            "price_usdt": price,
            "value_usdt": value,
        })
    rows.sort(key=lambda r: (r["value_usdt"] is not None, r["value_usdt"] or 0.0), reverse=True)
    return {
        "exchange": "binance",
        "account_type": account.get("accountType"),
        "can_trade": bool(account.get("canTrade")),
        "can_withdraw": bool(account.get("canWithdraw")),
        "can_deposit": bool(account.get("canDeposit")),
        "permissions": account.get("permissions") or [],
        "total_usdt_estimate": total_usdt,
        "available_usdt": next((r["free"] for r in rows if r["asset"] == "USDT"), 0.0),
        "balances": rows,
        "unpriced_assets": unpriced,
        "read_only_preview": True,
    }


def _runtime_mode():
    handles = getattr(api_mod, "_handles", {}) or {}
    state = handles.get("state")
    return {
        "mode": getattr(state, "mode", None),
        "live_armed": bool(getattr(state, "live_armed", False)) if state is not None else False,
    }


STYLE = r'''
<style>
body{max-width:980px!important;background:radial-gradient(circle at top,#142033 0,#0e1420 42%,#0b111b 100%)!important}
.card{box-shadow:0 10px 30px rgba(0,0,0,.16)}
.tabs{position:sticky;top:6px;z-index:8;background:rgba(14,20,32,.94);backdrop-filter:blur(10px);padding:6px;border-radius:14px}
#binanceCard{border-color:#5a4930;background:linear-gradient(180deg,#1b2130,#151c29)}
.bn-head{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}.bn-logo{font-weight:800;font-size:18px;letter-spacing:.6px}.bn-dot{color:#f0b90b}.bn-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:12px 0}.bn-stat{background:#101826;border:1px solid #273449;border-radius:11px;padding:11px}.bn-stat span{display:block;color:#7f8fa6;font-size:11px;margin-bottom:4px}.bn-stat b{font-size:16px}.bn-table{width:100%;border-collapse:collapse;margin-top:8px}.bn-table th,.bn-table td{padding:8px 5px;border-bottom:1px solid #253247;text-align:right}.bn-table th:first-child,.bn-table td:first-child{text-align:left}.bn-scroll{max-height:330px;overflow:auto}.bn-note{padding:10px;border-radius:10px;background:#111826;border:1px solid #273449;margin-top:10px}.bn-good{color:#4ec99a}.bn-warn{color:#e7b65c}.bn-bad{color:#e8695f}.bn-btn{background:#f0b90b!important;color:#251e00!important}.flowline{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.flowstep{padding:6px 9px;border-radius:999px;background:#101826;border:1px solid #2b3a50;color:#9aacbf;font-size:11px}.flowstep.active{color:#e8edf5;border-color:#5ac8e0}
@media(max-width:560px){.bn-grid{grid-template-columns:1fr}.tabs button{min-width:0;font-size:12px;padding:10px 6px}.bn-table{font-size:12px}}
</style>
'''

CARD = r'''
<div class="card" id="binanceCard">
  <div class="bn-head"><div><div class="bn-logo"><span class="bn-dot">◆</span> Binance portfolio</div><div class="k tiny">Read-only account preview first · no orders are placed from this panel</div></div><span id="bnStatus" class="pill warn">checking…</span></div>
  <div class="flowline"><span class="flowstep active">1 Connect</span><span class="flowstep">2 Verify holdings</span><span class="flowstep">3 Review risk</span><span class="flowstep">4 Arm LIVE separately</span></div>
  <div id="bnSetup" class="bn-note k">Checking Binance configuration…</div>
  <div id="bnAccount" style="display:none">
    <div class="bn-grid"><div class="bn-stat"><span>Portfolio estimate</span><b id="bnTotal">—</b></div><div class="bn-stat"><span>Available USDT</span><b id="bnCash">—</b></div><div class="bn-stat"><span>Spot trading permission</span><b id="bnTrade">—</b></div></div>
    <div class="row"><button class="bn-btn" onclick="loadBinanceAccount()">Refresh Binance</button><span id="bnPerms" class="k tiny"></span></div>
    <div class="bn-scroll"><table class="bn-table"><thead><tr><th>Asset</th><th>Qty</th><th>Price USDT</th><th>Value USDT</th></tr></thead><tbody id="bnRows"></tbody></table></div>
    <div id="bnUnpriced" class="k tiny" style="margin-top:8px"></div>
  </div>
  <div class="bn-note"><b>Safety:</b> keep Binance withdrawals disabled. Ternary remains PAPER until you explicitly change mode and arm LIVE. API credentials stay in Render environment secrets and are never returned to the browser.</div>
</div>
'''

SCRIPT = r'''
<script>
(function(){
  function e(id){return document.getElementById(id)}
  function money(v){return Number(v||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+' USDT'}
  function token(){return sessionStorage.getItem('tern_control_token')||''}
  async function getj(url,auth){var h={};if(auth&&token())h.Authorization='Bearer '+token();var r=await fetch(url,{headers:h}),t=await r.text(),d={};try{d=t?JSON.parse(t):{}}catch(x){throw new Error('HTTP '+r.status)}if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d}
  window.openBinance=function(){try{if(typeof tab==='function')tab('settings');else if(typeof showTab==='function')showTab('settings')}catch(x){}setTimeout(function(){e('binanceCard').scrollIntoView({behavior:'smooth',block:'start'})},80)}
  function addTab(){var tabs=document.querySelector('.tabs');if(!tabs||e('bnTab'))return;var b=document.createElement('button');b.id='bnTab';b.className='secondary';b.textContent='◆ Binance';b.onclick=openBinance;tabs.appendChild(b)}
  function moveCard(){var card=e('binanceCard'),pane=e('settings')||e('settingsPane');if(card&&pane&&card.parentNode!==pane)pane.appendChild(card)}
  async function status(){try{var d=await getj('/binance/status');var s=e('bnStatus'),setup=e('bnSetup');if(d.configured){s.textContent='configured';s.className='pill bn-good';setup.innerHTML='Credentials are present on Render. Unlock control access, then press <b>Refresh Binance</b> to read your Spot balances.'}else{s.textContent='not connected';s.className='pill bn-warn';setup.innerHTML='Add <b>TERN_LIVE_API_KEY</b> and <b>TERN_LIVE_API_SECRET</b> as secret environment variables in Render. Use a Binance API key with reading enabled and withdrawals disabled.'}if(d.mode==='LIVE')setup.innerHTML+='<div class="bn-warn" style="margin-top:7px">Runtime is currently LIVE'+(d.live_armed?' and ARMED.':' but not armed.')+'</div>';else setup.innerHTML+='<div class="bn-good" style="margin-top:7px">Runtime remains PAPER.</div>'}catch(x){e('bnStatus').textContent='unavailable';e('bnStatus').className='pill bn-bad'}}
  window.loadBinanceAccount=async function(){var setup=e('bnSetup');if(!token()){setup.innerHTML='<span class="bn-warn">Unlock Control access first. Binance balances are private account data.</span>';return}setup.textContent='Reading Binance Spot balances…';try{var d=await getj('/binance/account',true);e('bnAccount').style.display='block';e('bnTotal').textContent=money(d.total_usdt_estimate);e('bnCash').textContent=money(d.available_usdt);e('bnTrade').textContent=d.can_trade?'enabled':'disabled';e('bnTrade').className=d.can_trade?'bn-good':'bn-warn';e('bnPerms').textContent='account '+(d.account_type||'spot')+' · permissions '+(d.permissions||[]).join(', ');var body=e('bnRows');body.innerHTML='';(d.balances||[]).forEach(function(r){var tr=document.createElement('tr');tr.innerHTML='<td>'+r.asset+'</td><td>'+Number(r.qty).toLocaleString(undefined,{maximumFractionDigits:8})+'</td><td>'+(r.price_usdt==null?'—':Number(r.price_usdt).toLocaleString(undefined,{maximumFractionDigits:8}))+'</td><td>'+(r.value_usdt==null?'—':Number(r.value_usdt).toLocaleString(undefined,{maximumFractionDigits:2}))+'</td>';body.appendChild(tr)});e('bnUnpriced').textContent=(d.unpriced_assets||[]).length?'No USDT price found for: '+d.unpriced_assets.join(', '):'';setup.innerHTML='<span class="bn-good">Connection verified.</span> Holdings below are read-only. No Binance order was placed.'}catch(x){setup.innerHTML='<span class="bn-bad">Binance connection failed: '+String(x.message).replace(/[<>]/g,'')+'</span>'}}
  window.addEventListener('load',function(){addTab();moveCard();status();setInterval(status,15000)})
})();
</script>
'''

CONTROL_HTML = base.CONTROL_HTML
CONTROL_HTML = CONTROL_HTML.replace("</head>", STYLE + "</head>")
CONTROL_HTML = CONTROL_HTML.replace("</body></html>", CARD + SCRIPT + "</body></html>")


async def app(scope, receive, send):
    if scope.get("type") == "http":
        path = scope.get("path")
        method = scope.get("method")
        if path == "/" and method == "GET":
            return await HTMLResponse(CONTROL_HTML)(scope, receive, send)
        if path == "/binance/status" and method == "GET":
            out = {"configured": _binance_configured(), "base": BINANCE_BASE}
            out.update(_runtime_mode())
            return await JSONResponse(out)(scope, receive, send)
        if path == "/binance/account" and method == "GET":
            reason = _auth_reason(scope)
            if reason:
                code = 503 if reason == "control_plane_writes_disabled" else 401
                return await JSONResponse({"error": reason}, status_code=code)(scope, receive, send)
            try:
                return await JSONResponse(_account_summary())(scope, receive, send)
            except Exception as exc:
                return await JSONResponse({"error": str(exc)}, status_code=502)(scope, receive, send)
    await base.app(scope, receive, send)
