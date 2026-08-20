"""
Engine assembly + adapter registry.

PAPER can use either deterministic synthetic data (tests/research) or public live
spot candles while retaining the simulated PaperBroker. LIVE remains a separate
broker/credential path.
"""
from __future__ import annotations
import random

from eventlog.log import EventLog
from gateway.risk_gateway import RiskGateway, GatewayLimits
from ai.governed import GovernedAI
from sim.simulator import SimConfig, SymbolRules
from execution.broker import PaperBroker
from execution.authorization import ExecutionAuthority
from data.store import HistoricalStore, Bar
from core.orchestrator import Orchestrator, Target
from core.signals import strategy_signal
from service.state import AppState, goals_to_limits


def _synthetic_store(symbols, n=600, seed=7):
    data, rng, ts0 = {}, random.Random(seed), 1_700_000_000_000_000_000
    for i, sym in enumerate(symbols):
        price, t, bars = 100 + i * 15, ts0, []
        for k in range(n):
            drift = 0.0012 * (1 if (k // 120) % 2 == 0 else -1)
            c = max(0.5, price * (1 + drift + rng.uniform(-0.006, 0.006)))
            bars.append(Bar(t, price, max(price, c) * 1.001, min(price, c) * 0.999, c, 6000 + rng.random() * 1500))
            price, t = c, t + 3_600_000_000_000
        data[sym] = bars
    return HistoricalStore(data, spread_bps=6, depth_frac=0.15)


def _public_store(symbols, cfg):
    from data.live_public import PublicSpotMarketData
    interval = "1h"
    if isinstance(cfg.data_source, str) and cfg.data_source.startswith("public:"):
        interval = cfg.data_source.split(":", 1)[1] or "1h"
    return PublicSpotMarketData(symbols, interval=interval, limit=600,
                                cache_seconds=max(10, min(int(cfg.interval_seconds or 60), 30)),
                                spread_bps=6, depth_frac=0.05)


DATA_ADAPTERS = {
    "synthetic": lambda symbols, cfg: _synthetic_store(symbols),
    "public:*": _public_store,
}

BROKER_ADAPTERS = {
    "paper": lambda cfg, authority: PaperBroker(authority,
        SimConfig(fee_rate=0.001, slippage_coeff=0.5, partial_prob=0.15, max_quote_age_ms=180_000, latency_ms=50),
        SymbolRules(tick=0.01, lot=1e-5, min_notional=10, liquidity_cap=0.4), seed=9),
}


def register_data_adapter(name, factory):
    DATA_ADAPTERS[name] = factory


def register_broker_adapter(name, factory):
    BROKER_ADAPTERS[name] = factory


def _resolve(name, registry, kind):
    if name in registry:
        return registry[name]
    prefix = name.split(":", 1)[0] + ":*"
    if prefix not in registry:
        if name.startswith("ccxt:"):
            if kind == "data":
                from adapters.ccxt_data import register as reg
            else:
                from adapters.ccxt_broker import register as reg
            reg(registry)
    if prefix in registry:
        return registry[prefix]
    raise ValueError(f"no {kind} adapter for {name!r} (have: {list(registry)})")


def turnover_weighting(turnover: str):
    return {"low": 0.10, "medium": 0.12, "high": 0.15}.get(turnover, 0.10)


def build_engine(state: AppState, eventlog: EventLog, ai_adapter=None):
    """Assemble a wired Orchestrator from current state."""
    symbols = state.goals["universe"]
    lim = goals_to_limits(state.goals)
    # Public live PAPER quotes must fail stale much sooner than replay bars.
    if isinstance(state.data_source, str) and state.data_source.startswith("public:"):
        lim["max_quote_age_ms"] = 180_000
    limits = GatewayLimits(**lim)
    authority = ExecutionAuthority()
    gw = RiskGateway(limits, eventlog, authority=authority)

    # Real server-side OpenAI risk adapter. Missing credentials fail closed.
    if ai_adapter is None:
        from service.openai_risk import make_openai_risk_adapter
        ai_adapter = make_openai_risk_adapter()
    if ai_adapter is None:
        def ai_adapter(facts):
            return {"verdict": "VETO", "confidence": 0.0,
                    "reason_code": "ai_unavailable", "expiry_horizon_s": 0}
        ai_adapter.provider = "none"
        ai_adapter.model = "unavailable"

    ai = GovernedAI(adapter=ai_adapter, eventlog=eventlog,
                    model_version=getattr(ai_adapter, "model", "configured"))

    data = _resolve(state.data_source, DATA_ADAPTERS, "data")(symbols, state)
    broker = _resolve(state.broker, BROKER_ADAPTERS, "broker")(state, authority)

    maxw = turnover_weighting(state.goals["turnover"])
    maxpos = state.goals["max_positions"]

    def optimizer(cands, equity):
        if not cands:
            return []
        chosen = cands[:maxpos]
        w = min(maxw, state.goals["max_exposure_pct"] / max(1, len(chosen)))
        return [Target(s, w) for s in chosen]

    orch = Orchestrator(market=data, broker=broker, gateway=gw, ai=ai, eventlog=eventlog,
                        signal_fn=strategy_signal, optimizer_fn=optimizer,
                        qualified_universe=symbols, profile=_profile_for(state))
    return orch, gw, ai


def _profile_for(state: AppState):
    from config.profiles import load_profile
    return load_profile(state.mode)
