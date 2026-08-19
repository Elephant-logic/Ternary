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
        'market','momentum','rank','strongest','weakest','portfolio','allocate','candidate'
    )
    return any(w in q for w in words)


def _bulk_tickers():
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
            out[sym]={'symbol':sym,'market_price':price,'market_change_24h_pct':ch,'market_quote_volume_24h':vol,'market_provider':'bybit'}
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
            out[sym]={'symbol':sym,'market_price':price,'market_change_24h_pct':ch,'market_quote_volume_24h':vol,'market_provider':'kucoin'}
        if out:return out
        raise RuntimeError('no USDT tickers')
    except Exception as e: errors.append('kucoin: '+str(e))
    rows=market._symbols()
    return {r['symbol']:{'symbol':r['symbol'],'market_price':None,'market_change_24h_pct':None,'market_quote_volume_24h':r.get('quote_volume_24h',0),'market_provider':r.get('provider')} for r in rows}


def _paper_mark(worker, sym):
    """Return the PAPER engine mark at the worker's current replay timestamp.

    Public exchange prices must never be used for PAPER accounting because the
    current PAPER data adapter is synthetic/replay data with a different price domain.
    """
    try:
        bars=list(worker.orch.market.bars(sym) or [])
        if not bars:
            return None
        ts=(worker.last or {}).get('ts')
        if ts is None:
            return float(getattr(bars[-1],'close'))
        chosen=None
        for b in bars:
            bts=getattr(b,'ts_ns',None)
            if bts is None: continue
            if int(bts)<=int(ts): chosen=b
            else: break
        if chosen is None: chosen=bars[0]
        return float(getattr(chosen,'close'))
    except Exception:
        return None


def _snapshot():
    h=getattr(api_mod,'_handles',{}) or {}
    state=h.get('state'); worker=h.get('worker')
    if state is None or worker is None: raise RuntimeError('runtime not ready')
    universe=list((state.goals or {}).get('universe') or [])
    ticks=_bulk_tickers()
    rows=[]
    status=worker.status()
    status_positions=status.get('positions',{}) or {}
    for sym in universe:
        r=dict(ticks.get(sym) or {
            'symbol':sym,'market_price':None,'market_change_24h_pct':None,
            'market_quote_volume_24h':None,'market_provider':None
        })
        qty=float(status_positions.get(sym,0) or 0)
        paper_price=_paper_mark(worker,sym)
        r['paper_position_qty']=qty
        r['paper_mark_price']=paper_price
        r['paper_position_value']=None if paper_price is None else qty*paper_price
        rows.append(r)

    # Research ranking uses only public-market momentum/liquidity. It is never
    # used for PAPER accounting, gateway exposure, or simulated P&L.
    vols=[float(r.get('market_quote_volume_24h') or 0) for r in rows]
    vmax=max(vols) if vols else 1.0
    for r in rows:
        liq=(float(r.get('market_quote_volume_24h') or 0)/vmax) if vmax else 0
        ch=r.get('market_change_24h_pct')
        mom=0 if ch is None else max(-1,min(1,float(ch)/10.0))
        r['research_score']=round(0.65*liq+0.35*mom,4)
    ranked=sorted(rows,key=lambda r:r['research_score'],reverse=True)

    paper_values=[r['paper_position_value'] for r in ranked if r.get('paper_position_value') is not None]
    paper_gross=sum(abs(v) for v in paper_values)
    equity=float(status.get('equity') or 0)
    return {
        'mode':state.mode,
        'universe':universe,
        'assets':ranked,
        'paper_accounting':{
            'cash':status.get('cash'),
            'equity':status.get('equity'),
            'positions':status_positions,
            'gross_position_value':paper_gross,
            'gross_exposure_pct':(paper_gross/equity) if equity>0 else None,
            'price_domain':'Ternary PAPER/replay marks only'
        },
        'limits':(state.goals or {}),
        'research_data_note':'market_price, market_change_24h_pct and market_quote_volume_24h are public spot research data only. Never multiply PAPER quantities by market_price for accounting or exposure.',
        'ranking_note':'research_score is a transparent heuristic (65% relative liquidity, 35% capped 24h momentum), not an expected-return forecast.'
    }


def _ask_openai(question, context):
    key=os.environ.get('OPENAI_API_KEY')
    if not key: raise RuntimeError('OPENAI_API_KEY not configured')
    model=os.environ.get('OPENAI_MODEL','gpt-5-mini')
    system=(
        'You are Ternary market research for PAPER trading. Use only the supplied snapshot. '
        'There are TWO PRICE DOMAINS and you must never mix them: public market_* fields are for research ranking/momentum/liquidity only; '
        'paper_* fields and paper_accounting are the only valid source for PAPER position value, exposure, equity, P&L or concentration maths. '
        'Never multiply paper_position_qty by market_price. Never compare market_price-derived values with PAPER equity. '
        'When the user asks for N candidates, return exactly N if at least N assets have usable research data; otherwise state how many are available. '
        'For each candidate explain public-market 24h move, liquidity, current PAPER exposure using paper fields, and the main risk. '
        'Do not claim certainty, future returns, or that an asset is objectively a good investment. Call them PAPER research candidates. '
        'Do not place orders or imply that chat changes the trading engine. Keep one coherent answer; do not append a second generic runtime answer. '
        'If asked what looks good without a count, give 5 candidates when possible, then a short watch/avoid section and a PAPER-accounting risk observation.'
    )
    body={'model':model,'input':[{'role':'system','content':system},{'role':'user','content':question+'\n\nTernary snapshot:\n'+json.dumps(context,sort_keys=True)}]}
    req=urllib.request.Request('https://api.openai.com/v1/responses',data=json.dumps(body).encode(),method='POST',headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','User-Agent':'ternary-market-chat/2.0'})
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
                return await JSONResponse({
                    'reply':reply,
                    'market_context':{
                        'universe_count':len(snap['universe']),
                        'research_source':'public spot tickers',
                        'paper_accounting_source':'Ternary PAPER engine',
                        'mode':snap['mode']
                    }
                })(scope,receive,send)
        except Exception as e:
            return await JSONResponse({'reply':'Market analysis unavailable: '+str(e)},status_code=200)(scope,receive,send)
        sent=False
        async def replay():
            nonlocal sent
            if not sent:
                sent=True; return {'type':'http.request','body':raw,'more_body':False}
            return {'type':'http.request','body':b'','more_body':False}
        return await base.app(scope,replay,send)
    await base.app(scope,receive,send)
