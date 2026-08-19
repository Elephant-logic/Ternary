"""
Application state & settings (Phase 1).

Holds mode, settings, and goals, persisted as JSON. On ephemeral hosts a remote
JSON snapshot backend can restore them after a restart. Goals are declarative and get *compiled* into gateway limits +
optimiser settings (Phase 3 uses this), and every change is written to the
immutable event log as CONFIG_CHANGE. Safe defaults: PAPER, conservative limits.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict, field

DEFAULT_GOALS = {
    "objective": "grow_paper", "max_drawdown": 0.10, "max_daily_loss": 0.03,
    "max_positions": 5, "max_position_pct": 0.12, "max_exposure_pct": 0.60,
    "turnover": "low", "universe": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "timeframes": ["1h", "1d"],
}
PRESETS = {
    "conservative": {"max_drawdown": 0.08, "max_daily_loss": 0.02, "max_positions": 3,
                     "max_position_pct": 0.10, "max_exposure_pct": 0.40, "turnover": "low"},
    "balanced": {"max_drawdown": 0.12, "max_daily_loss": 0.03, "max_positions": 5,
                 "max_position_pct": 0.12, "max_exposure_pct": 0.60, "turnover": "medium"},
    "aggressive": {"max_drawdown": 0.18, "max_daily_loss": 0.05, "max_positions": 8,
                   "max_position_pct": 0.15, "max_exposure_pct": 0.80, "turnover": "high"},
}
TURNOVER_CAP = {"low": 2.0, "medium": 4.0, "high": 8.0}

def _clamp(v, lo, hi): return max(lo, min(hi, v))

def validate_goals(g: dict) -> tuple[dict, list]:
    warns = []
    out = {**DEFAULT_GOALS, **(g or {})}
    checks = {"max_drawdown": (0.01, 0.30), "max_daily_loss": (0.005, 0.10),
              "max_position_pct": (0.01, 0.25), "max_exposure_pct": (0.05, 0.95)}
    for k, (lo, hi) in checks.items():
        v = float(out.get(k, DEFAULT_GOALS[k])); c = _clamp(v, lo, hi)
        if c != v: warns.append(f"{k} {v} clamped to {c}")
        out[k] = c
    out["max_positions"] = int(_clamp(int(out.get("max_positions", 5)), 1, 20))
    if out.get("turnover") not in TURNOVER_CAP:
        out["turnover"] = "low"; warns.append("turnover reset to 'low'")
    return out, warns

def goals_to_limits(g: dict) -> dict:
    return {"max_position_pct": g["max_position_pct"], "max_exposure_pct": g["max_exposure_pct"],
            "max_daily_loss_pct": g["max_daily_loss"], "max_drawdown_pct": g["max_drawdown"],
            "max_concentration_pct": min(0.25, g["max_position_pct"] + 0.03),
            "max_daily_turnover": TURNOVER_CAP[g["turnover"]], "max_order_value": 1e9,
            "max_quote_age_ms": 7_200_000}

@dataclass
class AppState:
    mode: str = "PAPER"
    live_armed: bool = False
    interval_seconds: int = 60
    data_source: str = "synthetic"
    broker: str = "paper"
    goals: dict = field(default_factory=lambda: dict(DEFAULT_GOALS))
    path: str = "state.json"

    @classmethod
    def load(cls, path: str):
        if os.path.exists(path):
            with open(path) as f: d = json.load(f)
            d["path"] = path
            return cls(**d)
        s = cls(path=path); s.save(); return s

    def save(self):
        d = asdict(self); d.pop("path", None)
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, sort_keys=True); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def apply_preset(self, name: str) -> list:
        if name not in PRESETS: return [f"unknown preset {name!r}"]
        self.goals = {**self.goals, **PRESETS[name]}
        self.goals, warns = validate_goals(self.goals); self.save(); return warns

    def set_goals(self, g: dict) -> list:
        self.goals, warns = validate_goals({**self.goals, **(g or {})}); self.save(); return warns
