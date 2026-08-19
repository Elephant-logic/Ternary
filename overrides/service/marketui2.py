from __future__ import annotations
import json, time, urllib.parse, urllib.request
import service.marketui as ui

_CACHE={"symbols":None,"ts":0.0,"provider":None}

def _json(url, timeout=12):
    req=urllib.request.Request(url, headers={"User-Agent":"ternary-market-browser/2.0","Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def _bybit_symbols():
    d=_json("https://api.bybit.com/v5/market/tickers?category=spot")
    if int(d.get("retCode",-1))!=0:
        raise RuntimeError("Bybit: "+str(d.get("retMsg") or "request failed"))
    rows=[]
    for t in (d.get("result") or {}).get("list",[]):
        ex=(t.get("symbol") or "").upper()
        if not ex.endswith("USDT") or len(ex)<=4: continue
        base=ex[:-4]
        try: vol=float(t.get("turnover24h") or 0)
        except Exception: vol=0.0
        rows.append({"symbol":base+"/USDT","exchange_symbol":ex,"quote_volume_24h":vol,"provider":"bybit"})
    rows.sort(key=lambda x:x["quote_volume_24h"], reverse=True)
    if not rows: raise RuntimeError("Bybit returned no USDT spot symbols")
    return rows

def _kucoin_symbols():
    d=_json("https://api.kucoin.com/api/v1/market/allTickers")
    if str(d.get("code"))!="200000": raise RuntimeError("KuCoin request failed")
    rows=[]
    for t in ((d.get("data") or {}).get("ticker") or []):
        ex=(t.get("symbol") or "").upper()
        if not ex.endswith("-USDT"): continue
        base=ex[:-5]
        try: vol=float(t.get("volValue") or 0)
        except Exception: vol=0.0
        rows.append({"symbol":base+"/USDT","exchange_symbol":ex,"quote_volume_24h":vol,"provider":"kucoin"})
    rows.sort(key=lambda x:x["quote_volume_24h"], reverse=True)
    if not rows: raise RuntimeError("KuCoin returned no USDT spot symbols")
    return rows

def _symbols():
    now=time.time()
    if _CACHE["symbols"] and now-_CACHE["ts"]<120: return _CACHE["symbols"]
    errors=[]
    for name,fn in (("bybit",_bybit_symbols),("kucoin",_kucoin_symbols),("binance",ui._symbols)):
        try:
            rows=fn(); _CACHE.update(symbols=rows,ts=now,provider=name); return rows
        except Exception as e: errors.append(name+": "+str(e))
    raise RuntimeError("all market providers unavailable — "+" | ".join(errors))

def _norm_symbol(symbol):
    s=(symbol or "").upper().strip().replace("-","/")
    if s.endswith("USDT") and "/" not in s: s=s[:-4]+"/USDT"
    if not s.endswith("/USDT"): raise ValueError("symbol must be a USDT pair")
    return s

def _bybit_candles(symbol, interval, limit):
    s=_norm_symbol(symbol); ex=s.replace("/","")
    imap={"1m":"1","5m":"5","15m":"15","30m":"30","1h":"60","4h":"240","1d":"D","1w":"W"}
    if interval not in imap: raise ValueError("unsupported interval")
    q=urllib.parse.urlencode({"category":"spot","symbol":ex,"interval":imap[interval],"limit":max(25,min(int(limit),1000))})
    d=_json("https://api.bybit.com/v5/market/kline?"+q)
    if int(d.get("retCode",-1))!=0: raise RuntimeError("Bybit: "+str(d.get("retMsg") or "request failed"))
    raw=(d.get("result") or {}).get("list") or []
    bars=[{"ts_ms":int(k[0]),"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4]),"volume":float(k[5])} for k in reversed(raw)]
    if not bars: raise RuntimeError("Bybit returned no candles")
    return {"symbol":s,"interval":interval,"bars":bars,"provider":"bybit"}

def _kucoin_candles(symbol, interval, limit):
    s=_norm_symbol(symbol); ex=s.replace("/","-")
    tmap={"1m":"1min","5m":"5min","15m":"15min","30m":"30min","1h":"1hour","4h":"4hour","1d":"1day","1w":"1week"}
    if interval not in tmap: raise ValueError("unsupported interval")
    q=urllib.parse.urlencode({"symbol":ex,"type":tmap[interval]})
    d=_json("https://api.kucoin.com/api/v1/market/candles?"+q)
    if str(d.get("code"))!="200000": raise RuntimeError("KuCoin request failed")
    raw=(d.get("data") or [])[:max(25,min(int(limit),1500))]
    bars=[]
    for k in reversed(raw):
        bars.append({"ts_ms":int(k[0])*1000,"open":float(k[1]),"close":float(k[2]),"high":float(k[3]),"low":float(k[4]),"volume":float(k[5])})
    if not bars: raise RuntimeError("KuCoin returned no candles")
    return {"symbol":s,"interval":interval,"bars":bars,"provider":"kucoin"}

def _candles(symbol, interval="1h", limit=1000):
    errors=[]
    for name,fn in (("bybit",_bybit_candles),("kucoin",_kucoin_candles),("binance",ui._candles)):
        try: return fn(symbol,interval,limit)
        except Exception as e: errors.append(name+": "+str(e))
    raise RuntimeError("all candle providers unavailable — "+" | ".join(errors))

# marketui.app resolves these globals at request time, so replace them without
# duplicating the dashboard HTML or the control-plane routing.
ui._symbols=_symbols
ui._candles=_candles
app=ui.app
