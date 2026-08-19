from __future__ import annotations
import json, os, urllib.error, urllib.request
import service.api as api_mod
import service.marketui2 as market
import service.universeui as base
from starlette.responses import JSONResponse


def _read_body(receive):
    chunks=[]
    async def inner():
        more=True
        while more:
            msg=await receive()
            if msg.get('type')!='http.request':
                continue
            chunks.append(msg.get('body',b''))
            more=msg.get('more_body',False)
        return b''.join(chunks)
    return inner()


def _looks_market_question(text: str) -> bool:
    q=(text or '').lower()
    words=(
        'invest','investment','what is good','what looks good','best coin','best crypto',
        'which coin','which symbol','which asset','top pick','top picks','buy','opportunity',
        'market','momentum','rank','strongest','weakest','portfolio','allocate'
    )
    return any(w in q for w in words)


def _bulk_tickers():
    # One public request, provider-fallback. Returns normalized rows keyed BASE/USDT.
    errors=[]
    try:
        d=market._json('https://api.bybit.com/v5/market/tickers?category=spot')
        if int(d.get('retCode',-1))!=0:
            raise RuntimeError(str(d.get('retMsg') or 'request failed'))
        out={}
        for t in (d.get('result') or {}).get('list',[]):
            ex=(t.get('symbol') or '').upper()
            if not ex.endswith('USDT') or len(ex)<=4: continue
            sym=ex[:-4]+'/USDT'
            try: price=float(t.get('lastPrice') or 0)
            except Exception: price=0.0
            try: ch=float(t.get('price24hPcnt') or 0)*100
            except Exception: ch=0.0
            try: vol=float(t.get('turnover24h') or 0)
            except Exception: vol=0.0
            out[sym]={'symbol':sym,'price':price,'change_24h_pct':ch,'quote_volume_24h':vol,'provider':'bybit'}
        if out:return out
        raise RuntimeError('no USDT tickers')
    except Exception as e: errors.append('bybit: '+str(e))
    try:
        d=market._json('https://api.kucoin.com/api/v1/market/allTickers')
        if str(d.get('code'))!='200000': raise RuntimeError('request failed')
        out={}
        for t in ((d.get('data') or {}).get('ticker') or []):
            ex=(t.get('symbol') or '').upper()
            if not ex.endswith('-USDT'): continue
            sym=ex[:-5]+'/USDT'
            try: price=float(t.get('last') or 0)
            except Exception: price=0.0
            try: ch=float(t.get('changeRate') or 0)*100
            except Exception: ch=0.0
            try: vol=float(t.get('volValue') or 0)
            except Exception: vol=0.0
            out[sym]={'symbol':sym,'price':price,'change_24h_pct':ch,'quote_volume_24h':vol,'provider':'kucoin'}
        if out:return out
        raise RuntimeError('no USDT tickers')
    except Exception as e: errors.append('kucoin: '+str(e))
    # Last fallback: preserve the ranked symbol list even if price/change unavailable.
    rows=market._symbols()
    return {r['symbol']:{'symbol':r['symbol'],'price':None,'change_24h_pct':None,'quote_volume_24h':r.get('quote_volume_24h',0),'provider':r.get('provider')} for r in rows}


def _snapshot():
    h=getattr(api_mod,'_handles',{}) or {}
    state=h.get('state'); worker=h.get('worker')
    if state is None or worker is None: raise RuntimeError('runtime not ready')
    universe=list((state.goals or {}).get('universe') or [])
    ticks=_bulk_tickers()
    rows=[]
    positions={s:float((p or {}).get('qty',0) if isinstance(p,dict) else 0) for s,p in (worker.book.positions or {}).items()}
    for sym in universe:
        r=dict(ticks.get(sym) or {'symbol':sym,'price':None,'change_24h_pct':None,'quote_volume_24h':None,'provider':None})
        qty=float(positions.get(sym,0) or 0)
        r['position_qty']=qty
        r['position_value']=None if r.get('price') is None else qty*float(r['price'])
        rows.append(r)
    # deterministic research score: liquidity first, then positive 24h momentum; no hidden model.
    vols=[float(r.get('quote_volume_24h') or 0) for r in rows]
    vmax=max(vols) if vols else 1.0
    for r in rows:
        liq=(float(r.get('quote_volume_24h') or 0)/vmax) if vmax else 0
        ch=r.get('change_24h_pct')
        mom=0 if ch is None else max(-1,min(1,float(ch)/10.0))
        r['research_score']=round(0.65*liq+0.35*mom,4)
    ranked=sorted(rows,key=lambda r:r['research_score'],reverse=True)
    status=worker.status()
    return {
        'mode':state.mode,'universe':universe,'assets':ranked,
        'cash':status.get('cash'),'equity':status.get('equity'),'positions':status.get('positions',{}),
        'limits':(state.goals or {}),
        'note':'research_score is a transparent heuristic (65% relative liquidity, 35% capped 24h momentum), not an expected-return forecast.'
    }


def _ask_openai(question, context):
    key=os.environ.get('OPENAI_API_KEY')
    if not key: raise RuntimeError('OPENAI_API_KEY not configured')
    model=os.environ.get('OPENAI_MODEL','gpt-5-mini')
    system=(
        'You are Ternary market research for PAPER trading. The user is asking about the selected trading universe. '
        'Use ONLY the supplied live-ish market snapshot and runtime state. Rank concrete candidates when the data supports it. '
        'For each candidate explain price move, liquidity, existing exposure, and the main risk. Do not say you lack market prices if they are supplied. '
        'Do not claim certainty, future returns, or that an asset is objectively a good investment. Call them PAPER research candidates. '
        'Do not place orders or imply that chat changes the trading engine. Keep the answer concise and useful. '
        'If the user asks what looks good, give a ranked shortlist (normally 3-5), then a short avoid/watch list and portfolio-risk observation.'
    )
    body={'model':model,'input':[{'role':'system','content':system},{'role':'user','content':question+'\n\nTernary market snapshot:\n'+json.dumps(context,sort_keys=True)}]}
    req=urllib.request.Request('https://api.openai.com/v1/responses',data=json.dumps(body).encode(),method='POST',headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','User-Agent':'ternary-market-chat'})
    try:
        with urllib.request.urlopen(req,timeout=35) as resp: payload=json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode('utf-8','replace')[:300]
        raise RuntimeError('OpenAI HTTP %s: %s'%(exc.code,detail)) from exc
    parts=[]
    for item in payload.get('output',[]):
        for c in item.get('content',[]):
            if c.get('text'):parts.append(c['text'])
    if not parts and isinstance(payload.get('output_text'),str):parts.append(payload['output_text'])
    if not parts:raise RuntimeError('OpenAI response contained no text')
    return '\n'.join(parts).strip()


async def app(scope, receive, send):
    if scope.get('type')=='http' and scope.get('path')=='/chat' and scope.get('method')=='POST':
        raw=await _read_body(receive)
        try:
            data=json.loads(raw.decode('utf-8') or '{}'); msg=str(data.get('message') or '')
            if _looks_market_question(msg):
                snap=_snapshot(); reply=_ask_openai(msg,snap)
                return await JSONResponse({'reply':reply,'market_context':{'universe_count':len(snap['universe']),'source':'public spot tickers','mode':snap['mode']}})(scope,receive,send)
        except Exception as e:
            return await JSONResponse({'reply':'Market analysis unavailable: '+str(e)},status_code=200)(scope,receive,send)
        # Recreate request body for the underlying FastAPI app.
        sent=False
        async def replay():
            nonlocal sent
            if not sent:
                sent=True; return {'type':'http.request','body':raw,'more_body':False}
            return {'type':'http.request','body':b'','more_body':False}
        return await base.app(scope,replay,send)
    await base.app(scope,receive,send)
