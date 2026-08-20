"""Real OpenAI-backed adapter for the governed AI risk-review seam.

The adapter is intentionally narrow: it receives frozen mechanical facts and may
only return TAKE, REDUCE, DELAY, or VETO. Network/API failures and exhausted test
budgets fail closed to VETO rather than silently passing a trade.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone


_ACTIVE_ADAPTER = None
_LOCK = threading.Lock()


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
    # USD per 1M text tokens. Keep unknown models unpriced rather than guessing.
    if model == "gpt-5-mini" or model.startswith("gpt-5-mini-"):
        return 0.25, 2.00
    return None, None


def _usage_dict(state, model: str):
    day, hour = _buckets()
    goals = getattr(state, "goals", {}) if state is not None else {}
    u = dict(goals.get("ai_usage") or {})
    if u.get("day") != day:
        u.update({"day": day, "calls_today": 0, "input_tokens_today": 0, "output_tokens_today": 0, "estimated_cost_usd_today": 0.0})
    if u.get("hour") != hour:
        u.update({"hour": hour, "calls_this_hour": 0})
    u.setdefault("calls_today", 0)
    u.setdefault("calls_this_hour", 0)
    u.setdefault("input_tokens_today", 0)
    u.setdefault("output_tokens_today", 0)
    u.setdefault("estimated_cost_usd_today", 0.0)
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


def risk_usage_status():
    adapter = _ACTIVE_ADAPTER
    if adapter is None:
        return {"enabled": False, "max_calls_per_hour": 0, "calls_this_hour": 0, "calls_remaining": 0,
                "calls_today": 0, "input_tokens_today": 0, "output_tokens_today": 0,
                "estimated_cost_usd_today": 0.0, "model": None}
    return adapter.usage_status()


def set_risk_budget(max_calls_per_hour: int):
    adapter = _ACTIVE_ADAPTER
    if adapter is None:
        raise RuntimeError("AI risk adapter not ready")
    return adapter.set_budget(max_calls_per_hour)


def make_openai_risk_adapter(state=None):
    global _ACTIVE_ADAPTER
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    model = os.environ.get("OPENAI_RISK_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    timeout_s = max(5, min(int(os.environ.get("OPENAI_RISK_TIMEOUT_S", "30")), 60))

    if state is not None:
        goals = dict(state.goals or {})
        try:
            budget = int(goals.get("ai_risk_max_calls_per_hour", 20))
        except Exception:
            budget = 20
        budget = max(0, min(budget, 500))
        goals["ai_risk_max_calls_per_hour"] = budget
        state.goals = goals
    else:
        budget = max(0, min(int(os.environ.get("TERN_AI_RISK_MAX_CALLS_PER_HOUR", "20")), 500))

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": ["TAKE", "REDUCE", "DELAY", "VETO"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason_code": {"type": "string", "minLength": 1, "maxLength": 64},
            "expiry_horizon_s": {"type": "integer", "minimum": 0, "maximum": 86400}
        },
        "required": ["verdict", "confidence", "reason_code", "expiry_horizon_s"]
    }

    system = (
        "You are Ternary's governed risk critic. You review an already-created mechanical "
        "trade candidate using only the frozen facts supplied. You are not a trading oracle. "
        "You MUST NOT invent a symbol, create a trade, increase position size, or override "
        "the independent risk gateway. Your only permitted verdicts are TAKE, REDUCE, DELAY, "
        "or VETO. TAKE leaves the mechanical size unchanged; REDUCE halves it; DELAY and VETO "
        "place no order this cycle. Prefer the safer action when facts are weak, contradictory, "
        "stale, highly exposed, or otherwise uncertain. reason_code must be a short stable "
        "snake_case label, not prose."
    )

    def usage_status():
        with _LOCK:
            u = _usage_dict(state, model)
            max_calls = int((getattr(state, "goals", {}) or {}).get("ai_risk_max_calls_per_hour", budget)) if state is not None else budget
            max_calls = max(0, min(max_calls, 500))
            return {
                "enabled": True,
                "model": model,
                "max_calls_per_hour": max_calls,
                "calls_this_hour": int(u.get("calls_this_hour", 0)),
                "calls_remaining": max(0, max_calls - int(u.get("calls_this_hour", 0))),
                "calls_today": int(u.get("calls_today", 0)),
                "input_tokens_today": int(u.get("input_tokens_today", 0)),
                "output_tokens_today": int(u.get("output_tokens_today", 0)),
                "estimated_cost_usd_today": round(float(u.get("estimated_cost_usd_today", 0.0)), 6),
                "day_utc": u.get("day"),
                "hour_utc": u.get("hour"),
            }

    def set_budget(value):
        if state is None:
            raise RuntimeError("persistent AI budget requires application state")
        value = max(0, min(int(value), 500))
        with _LOCK:
            goals = dict(state.goals or {})
            goals["ai_risk_max_calls_per_hour"] = value
            state.goals = goals
            state.save()
        return usage_status()

    def adapter(facts: dict) -> dict:
        with _LOCK:
            u = _usage_dict(state, model)
            max_calls = int((getattr(state, "goals", {}) or {}).get("ai_risk_max_calls_per_hour", budget)) if state is not None else budget
            max_calls = max(0, min(max_calls, 500))
            if int(u.get("calls_this_hour", 0)) >= max_calls:
                _persist_usage(state, u)
                return {"verdict": "VETO", "confidence": 0.0, "reason_code": "ai_budget_exhausted", "expiry_horizon_s": 0}
            # Count the request before sending so failures cannot bypass the budget.
            u["calls_this_hour"] = int(u.get("calls_this_hour", 0)) + 1
            u["calls_today"] = int(u.get("calls_today", 0)) + 1
            _persist_usage(state, u)

        body = {
            "model": model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": "Frozen risk facts:\n" + json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)}
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "ternary_risk_review",
                    "description": "A bounded Ternary risk decision that can only preserve, reduce, delay, or veto a mechanical order.",
                    "schema": schema,
                    "strict": True
                }
            }
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "ternary-governed-risk/1.0"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            api_usage = payload.get("usage") or {}
            input_tokens = int(api_usage.get("input_tokens", 0) or 0)
            output_tokens = int(api_usage.get("output_tokens", 0) or 0)
            in_rate, out_rate = _pricing(model)
            with _LOCK:
                u = _usage_dict(state, model)
                u["input_tokens_today"] = int(u.get("input_tokens_today", 0)) + input_tokens
                u["output_tokens_today"] = int(u.get("output_tokens_today", 0)) + output_tokens
                if in_rate is not None and out_rate is not None:
                    cost = input_tokens * in_rate / 1_000_000 + output_tokens * out_rate / 1_000_000
                    u["estimated_cost_usd_today"] = float(u.get("estimated_cost_usd_today", 0.0)) + cost
                _persist_usage(state, u)
            text = _extract_text(payload)
            if not text:
                return {"verdict": "VETO", "confidence": 0.0, "reason_code": "ai_empty_response", "expiry_horizon_s": 0}
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError("structured response was not an object")
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
    adapter.set_budget = set_budget
    _ACTIVE_ADAPTER = adapter
    return adapter
