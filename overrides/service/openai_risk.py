"""Real OpenAI-backed adapter for Ternary's governed AI risk-review seam.

The adapter can only TAKE, REDUCE, DELAY, or VETO. Fresh OpenAI reviews are
bounded by an hourly budget, paced across the hour, and cached by materially
normalised risk facts so repeated reviews do not waste calls. PAPER may continue
through the deterministic gateway when a paced slot or test budget is unavailable;
LIVE remains fail-closed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

_ACTIVE_ADAPTER = None
_LOCK = threading.Lock()
_VOLATILE_KEYS = {
    "ts", "ts_ns", "timestamp", "timestamp_ns", "client_id", "order_id",
    "request_id", "cycle", "event_seq", "event_id",
}


def _extract_text(payload: dict) -> str:
    pieces = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                pieces.append(content["text"])
    if pieces:
        return "\n".join(pieces).strip()
    out = payload.get("output_text")
    return out.strip() if isinstance(out, str) else ""


def _buckets():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%dT%H")


def _pricing(model: str):
    if model == "gpt-5-mini" or model.startswith("gpt-5-mini-"):
        return 0.25, 2.00
    return None, None


def _usage_dict(state, model: str):
    day, hour = _buckets()
    goals = getattr(state, "goals", {}) if state is not None else {}
    u = dict(goals.get("ai_usage") or {})
    if u.get("day") != day:
        u.update({
            "day": day,
            "calls_today": 0,
            "cache_hits_today": 0,
            "budget_bypasses_today": 0,
            "pace_bypasses_today": 0,
            "input_tokens_today": 0,
            "output_tokens_today": 0,
            "estimated_cost_usd_today": 0.0,
        })
    if u.get("hour") != hour:
        u.update({
            "hour": hour,
            "calls_this_hour": 0,
            "cache_hits_this_hour": 0,
            "budget_bypasses_this_hour": 0,
            "pace_bypasses_this_hour": 0,
        })
    for k in (
        "calls_today", "calls_this_hour", "cache_hits_today", "cache_hits_this_hour",
        "budget_bypasses_today", "budget_bypasses_this_hour", "pace_bypasses_today",
        "pace_bypasses_this_hour", "input_tokens_today", "output_tokens_today",
    ):
        u.setdefault(k, 0)
    u.setdefault("estimated_cost_usd_today", 0.0)
    u.setdefault("last_fresh_call_epoch", 0.0)
    u["model"] = model
    return u


def _persist_usage(state, usage):
    if state is None:
        return
    goals = dict(state.goals or {})
    goals["ai_usage"] = usage
    state.goals = goals
    try:
        state.save()
    except Exception:
        pass


def _round_sig(value: float, sig: int = 3):
    if not math.isfinite(value) or value == 0:
        return value
    return round(value, sig - int(math.floor(math.log10(abs(value)))) - 1)


def _normalise_material(value, key: str = ""):
    if isinstance(value, dict):
        out = {}
        for k, v in sorted(value.items(), key=lambda kv: str(kv[0])):
            ks = str(k)
            if ks.lower() in _VOLATILE_KEYS:
                continue
            out[ks] = _normalise_material(v, ks)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalise_material(v, key) for v in value]
    if isinstance(value, float):
        return _round_sig(value, 3)
    return value


def _symbol_side(facts: dict):
    symbol = facts.get("symbol")
    side = facts.get("side")
    for nested_key in ("order", "intent", "candidate", "target"):
        nested = facts.get(nested_key)
        if isinstance(nested, dict):
            symbol = symbol or nested.get("symbol")
            side = side or nested.get("side")
    return str(symbol or "unknown"), str(side or "unknown").upper()


def _cache_key(facts: dict):
    symbol, side = _symbol_side(facts)
    material = _normalise_material(facts)
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"{symbol}|{side}|{hashlib.sha256(raw).hexdigest()}"


def _policy_values(state, default_budget: int):
    goals = getattr(state, "goals", {}) if state is not None else {}
    try:
        max_calls = int(goals.get("ai_risk_max_calls_per_hour", default_budget))
    except Exception:
        max_calls = default_budget
    max_calls = max(0, min(max_calls, 500))
    try:
        override = int(goals.get("ai_risk_min_interval_seconds", 0))
    except Exception:
        override = 0
    override = max(0, min(override, 3600))
    if override > 0:
        spacing = override
        mode = "custom"
    elif max_calls > 0:
        spacing = max(1, math.ceil(3600 / max_calls))
        mode = "auto"
    else:
        spacing = 3600
        mode = "auto"
    return max_calls, override, spacing, mode


def risk_usage_status():
    adapter = _ACTIVE_ADAPTER
    if adapter is None:
        return {
            "enabled": False, "max_calls_per_hour": 0, "calls_this_hour": 0,
            "calls_remaining": 0, "calls_today": 0, "cache_hits_today": 0,
            "cache_hits_this_hour": 0, "budget_bypasses_today": 0,
            "budget_bypasses_this_hour": 0, "pace_bypasses_today": 0,
            "pace_bypasses_this_hour": 0, "min_interval_seconds": 0,
            "effective_min_interval_seconds": 0, "pacing_mode": "auto",
            "next_fresh_call_in_seconds": 0, "cache_ttl_s": 0,
            "input_tokens_today": 0, "output_tokens_today": 0,
            "estimated_cost_usd_today": 0.0, "model": None,
        }
    return adapter.usage_status()


def set_risk_budget(max_calls_per_hour: int):
    adapter = _ACTIVE_ADAPTER
    if adapter is None:
        raise RuntimeError("AI risk adapter not ready")
    return adapter.set_policy(max_calls_per_hour=max_calls_per_hour)


def set_risk_policy(max_calls_per_hour=None, min_interval_seconds=None):
    adapter = _ACTIVE_ADAPTER
    if adapter is None:
        raise RuntimeError("AI risk adapter not ready")
    return adapter.set_policy(max_calls_per_hour=max_calls_per_hour,
                              min_interval_seconds=min_interval_seconds)


def make_openai_risk_adapter(state=None):
    global _ACTIVE_ADAPTER
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None

    model = os.environ.get("OPENAI_RISK_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    timeout_s = max(5, min(int(os.environ.get("OPENAI_RISK_TIMEOUT_S", "30")), 60))
    cache_ttl_s = max(60, min(int(os.environ.get("TERN_AI_RISK_CACHE_TTL_S", "900")), 3600))
    decision_cache = {}

    if state is not None:
        goals = dict(state.goals or {})
        try:
            budget = int(goals.get("ai_risk_max_calls_per_hour", 20))
        except Exception:
            budget = 20
        budget = max(0, min(budget, 500))
        goals["ai_risk_max_calls_per_hour"] = budget
        try:
            pace_override = int(goals.get("ai_risk_min_interval_seconds", 0))
        except Exception:
            pace_override = 0
        goals["ai_risk_min_interval_seconds"] = max(0, min(pace_override, 3600))
        state.goals = goals
    else:
        budget = max(0, min(int(os.environ.get("TERN_AI_RISK_MAX_CALLS_PER_HOUR", "20")), 500))

    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": ["TAKE", "REDUCE", "DELAY", "VETO"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason_code": {"type": "string", "minLength": 1, "maxLength": 64},
            "expiry_horizon_s": {"type": "integer", "minimum": 0, "maximum": 86400},
        },
        "required": ["verdict", "confidence", "reason_code", "expiry_horizon_s"],
    }
    system = (
        "You are Ternary's governed risk critic. Review only the already-created mechanical "
        "trade candidate using the frozen facts supplied. Never invent a symbol, create a "
        "trade, increase size, or override the independent risk gateway. Your only verdicts "
        "are TAKE, REDUCE, DELAY, or VETO. TAKE leaves size unchanged; REDUCE halves it; "
        "DELAY and VETO place no order this cycle. Prefer the safer action when facts are weak, "
        "contradictory, stale, highly exposed, or uncertain. reason_code must be short snake_case."
    )

    def usage_status():
        with _LOCK:
            u = _usage_dict(state, model)
            max_calls, override, spacing, pacing_mode = _policy_values(state, budget)
            now_mono = time.monotonic()
            live_cache = sum(1 for item in decision_cache.values() if item[0] > now_mono)
            elapsed = max(0.0, time.time() - float(u.get("last_fresh_call_epoch", 0.0)))
            next_in = 0 if int(u.get("calls_this_hour", 0)) == 0 else max(0, math.ceil(spacing - elapsed))
            return {
                "enabled": True, "model": model, "max_calls_per_hour": max_calls,
                "calls_this_hour": int(u.get("calls_this_hour", 0)),
                "calls_remaining": max(0, max_calls - int(u.get("calls_this_hour", 0))),
                "calls_today": int(u.get("calls_today", 0)),
                "cache_hits_this_hour": int(u.get("cache_hits_this_hour", 0)),
                "cache_hits_today": int(u.get("cache_hits_today", 0)),
                "budget_bypasses_this_hour": int(u.get("budget_bypasses_this_hour", 0)),
                "budget_bypasses_today": int(u.get("budget_bypasses_today", 0)),
                "pace_bypasses_this_hour": int(u.get("pace_bypasses_this_hour", 0)),
                "pace_bypasses_today": int(u.get("pace_bypasses_today", 0)),
                "min_interval_seconds": override,
                "effective_min_interval_seconds": spacing,
                "pacing_mode": pacing_mode,
                "next_fresh_call_in_seconds": next_in,
                "cache_ttl_s": cache_ttl_s, "cached_decisions": live_cache,
                "input_tokens_today": int(u.get("input_tokens_today", 0)),
                "output_tokens_today": int(u.get("output_tokens_today", 0)),
                "estimated_cost_usd_today": round(float(u.get("estimated_cost_usd_today", 0.0)), 6),
                "day_utc": u.get("day"), "hour_utc": u.get("hour"),
            }

    def set_policy(max_calls_per_hour=None, min_interval_seconds=None):
        if state is None:
            raise RuntimeError("persistent AI policy requires application state")
        with _LOCK:
            goals = dict(state.goals or {})
            if max_calls_per_hour is not None:
                goals["ai_risk_max_calls_per_hour"] = max(0, min(int(max_calls_per_hour), 500))
            if min_interval_seconds is not None:
                goals["ai_risk_min_interval_seconds"] = max(0, min(int(min_interval_seconds), 3600))
            state.goals = goals
            state.save()
        return usage_status()

    def paper_fallback(u, reason_code, counter_prefix):
        u[counter_prefix + "_this_hour"] = int(u.get(counter_prefix + "_this_hour", 0)) + 1
        u[counter_prefix + "_today"] = int(u.get(counter_prefix + "_today", 0)) + 1
        _persist_usage(state, u)
        return {"verdict": "TAKE", "confidence": 0.0,
                "reason_code": reason_code, "expiry_horizon_s": 0}

    def adapter(facts: dict) -> dict:
        key_hash = _cache_key(facts)
        now_mono = time.monotonic()
        with _LOCK:
            cached = decision_cache.get(key_hash)
            if cached and cached[0] > now_mono:
                u = _usage_dict(state, model)
                u["cache_hits_this_hour"] = int(u.get("cache_hits_this_hour", 0)) + 1
                u["cache_hits_today"] = int(u.get("cache_hits_today", 0)) + 1
                _persist_usage(state, u)
                return dict(cached[1])
            if cached:
                decision_cache.pop(key_hash, None)

            u = _usage_dict(state, model)
            max_calls, _override, spacing, _pacing_mode = _policy_values(state, budget)
            paper = state is not None and str(getattr(state, "mode", "")).upper() == "PAPER"

            if int(u.get("calls_this_hour", 0)) >= max_calls:
                if paper:
                    return paper_fallback(u, "paper_budget_deterministic_only", "budget_bypasses")
                _persist_usage(state, u)
                return {"verdict": "VETO", "confidence": 0.0,
                        "reason_code": "ai_budget_exhausted", "expiry_horizon_s": 0}

            if int(u.get("calls_this_hour", 0)) > 0:
                elapsed = max(0.0, time.time() - float(u.get("last_fresh_call_epoch", 0.0)))
                if elapsed < spacing:
                    if paper:
                        return paper_fallback(u, "paper_ai_paced_deterministic_only", "pace_bypasses")
                    _persist_usage(state, u)
                    return {"verdict": "VETO", "confidence": 0.0,
                            "reason_code": "ai_pacing_wait", "expiry_horizon_s": max(0, int(spacing - elapsed))}

            u["calls_this_hour"] = int(u.get("calls_this_hour", 0)) + 1
            u["calls_today"] = int(u.get("calls_today", 0)) + 1
            u["last_fresh_call_epoch"] = time.time()
            _persist_usage(state, u)

        body = {
            "model": model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": "Frozen risk facts:\n" + json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)},
            ],
            "text": {"format": {
                "type": "json_schema", "name": "ternary_risk_review",
                "description": "A bounded Ternary risk decision.", "schema": schema, "strict": True,
            }},
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses", data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "ternary-governed-risk/1.2"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            api_usage = payload.get("usage") or {}
            input_tokens = int(api_usage.get("input_tokens", 0) or 0)
            output_tokens = int(api_usage.get("output_tokens", 0) or 0)
            in_rate, out_rate = _pricing(model)
            text = _extract_text(payload)
            if not text:
                return {"verdict": "VETO", "confidence": 0.0, "reason_code": "ai_empty_response", "expiry_horizon_s": 0}
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError("structured response was not an object")
            with _LOCK:
                u = _usage_dict(state, model)
                u["input_tokens_today"] = int(u.get("input_tokens_today", 0)) + input_tokens
                u["output_tokens_today"] = int(u.get("output_tokens_today", 0)) + output_tokens
                if in_rate is not None and out_rate is not None:
                    u["estimated_cost_usd_today"] = float(u.get("estimated_cost_usd_today", 0.0)) + input_tokens * in_rate / 1_000_000 + output_tokens * out_rate / 1_000_000
                _persist_usage(state, u)
                decision_cache[key_hash] = (time.monotonic() + cache_ttl_s, dict(obj))
                if len(decision_cache) > 500:
                    expired = [k for k, v in decision_cache.items() if v[0] <= time.monotonic()]
                    for k in expired:
                        decision_cache.pop(k, None)
                    while len(decision_cache) > 500:
                        decision_cache.pop(next(iter(decision_cache)))
            return obj
        except urllib.error.HTTPError as exc:
            return {"verdict": "VETO", "confidence": 0.0, "reason_code": f"ai_http_{exc.code}", "expiry_horizon_s": 0}
        except urllib.error.URLError:
            return {"verdict": "VETO", "confidence": 0.0, "reason_code": "ai_network_error", "expiry_horizon_s": 0}
        except TimeoutError:
            return {"verdict": "VETO", "confidence": 0.0, "reason_code": "ai_timeout", "expiry_horizon_s": 0}
        except Exception:
            return {"verdict": "VETO", "confidence": 0.0, "reason_code": "ai_parse_error", "expiry_horizon_s": 0}

    adapter.provider = "openai"
    adapter.model = model
    adapter.usage_status = usage_status
    adapter.set_policy = set_policy
    adapter.set_budget = lambda value: set_policy(max_calls_per_hour=value)
    _ACTIVE_ADAPTER = adapter
    return adapter
