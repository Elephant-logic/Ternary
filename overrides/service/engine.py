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


class _UnavailablePublicMarketData:
    """Boot-safe live feed placeholder.

    This is not a trading fallback and never fabricates prices. It exists only so
    the HTTP control plane remains available if the public-feed module cannot be
    imported or initialized. The worker sees zero healthy symbols and stays idle.
    """
    is_live = True
    source_name = "public_spot_unavailable"

    def __init__(self, symbols, interval, error):
        self.symbols = tuple(symbols)
        self.interval = interval
        self.error = str(error)

    def refresh(self, force=False):
        return {"ok": 0, "total": len(self.symbols), "errors": {"adapter": self.error}, "provider": None}

    def bars(self, symbol):
        return ()

    def quote_at(self, symbol, ts_ns):
        return None

    def health(self):
        return {"source": self.source_name, "interval": self.interval, "provider": None,
                "cached": 0, "max_age_s": None, "errors": {"adapter": self.error}}


def _public_store(symbols, cfg):
    interval = "1h"
    if isinstance(cfg.data_source, str) and cfg.data_source.startswith("public:"):
        interval = cfg.data_source.split(":", 1)[1] or "1h"
    try:
        from data.live_public import PublicSpotMarketData
        return PublicSpotMarketData(symbols, interval=interval, limit=600,
                                    cache_seconds=max(10, min(int(cfg.interval_seconds or 60), 30)),
                                    spread_bps=6, depth_frac=0.05)
    except Exception as exc:
        return _UnavailablePublicMarketData(symbols, interval, exc)


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
    if isinstance(state.data_source, str) and state.data_source.startswith("public:"):
        lim["max_quote_age_ms"] = 180_000
    limits = GatewayLimits(**lim)
    authority = ExecutionAuthority()
    gw = RiskGateway(limits, eventlog, authority=authority)

    if ai_adapter is None:
        from service.openai_risk import make_openai_risk_adapter
        ai_adapter = make_openai_risk_adapter(state)
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

    def _price(symbol):
        """Use the already-prepared market snapshot; never invent a price."""
        try:
            bars = data.bars(symbol)
            if bars:
                return float(bars[-1].close)
        except Exception:
            pass
        return 0.0

    def optimizer(cands, equity):
        """Rank candidates, respect exposure headroom, and rotate instead of over-buying.

        The worker publishes an ephemeral snapshot of current positions/cash before
        each cycle. Near the exposure ceiling, the optimiser emits reductions/exits
        first and additions second. The independent gateway still validates every
        resulting order and remains the final authority.
        """
        if not cands or equity <= 0:
            return []

        # Preserve deterministic ranking order while removing duplicates.
        ranked = list(dict.fromkeys(cands))
        maxpos = max(1, int(state.goals.get("max_positions", 5)))
        desired = ranked[:maxpos]
        max_exposure = float(state.goals.get("max_exposure_pct", 0.60))
        maxw = turnover_weighting(state.goals.get("turnover", "low"))
        target_w = min(maxw, max_exposure / max(1, len(desired)))

        runtime_positions = getattr(state, "_runtime_positions", {}) or {}
        runtime_cash = getattr(state, "_runtime_cash", None)
        held = {s: p for s, p in runtime_positions.items()
                if float((p or {}).get("qty", 0.0)) > 1e-12}

        try:
            exposure = max(0.0, (float(equity) - float(runtime_cash)) / float(equity)) if runtime_cash is not None else 0.0
        except Exception:
            exposure = 0.0

        rotation_enabled = bool(state.goals.get("rotation_enabled", True))
        near_cap = exposure >= max_exposure * 0.97
        if not rotation_enabled or not near_cap or not held:
            return [Target(s, target_w) for s in desired]

        current_weights = {}
        for sym, pos in held.items():
            px = _price(sym)
            try:
                current_weights[sym] = max(0.0, float(pos.get("qty", 0.0)) * px / float(equity)) if px > 0 else 0.0
            except Exception:
                current_weights[sym] = 0.0

        # Holdings outside the currently highest-ranked desired set are rotation
        # candidates. Limit exits per cycle so a noisy ranking cannot churn the
        # whole portfolio at once.
        exits = [s for s in held if s not in desired]
        exits.sort(key=lambda s: current_weights.get(s, 0.0))
        max_rotate = max(1, min(int(state.goals.get("rotation_max_per_cycle", 1)), 3))
        exits = exits[:max_rotate]

        targets = [Target(s, 0.0) for s in exits]

        # Emit reductions before additions. This makes exposure headroom available
        # before the gateway evaluates new buys when the orchestrator rebalances.
        reductions, additions = [], []
        for sym in desired:
            t = Target(sym, target_w)
            if sym in held and current_weights.get(sym, 0.0) > target_w * 1.02:
                reductions.append(t)
            else:
                additions.append(t)
        targets.extend(reductions)
        targets.extend(additions)
        return targets

    orch = Orchestrator(market=data, broker=broker, gateway=gw, ai=ai, eventlog=eventlog,
                        signal_fn=strategy_signal, optimizer_fn=optimizer,
                        qualified_universe=symbols, profile=_profile_for(state))
    return orch, gw, ai


def _profile_for(state: AppState):
    from config.profiles import load_profile
    return load_profile(state.mode)
