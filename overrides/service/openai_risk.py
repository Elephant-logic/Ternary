"""Real OpenAI-backed adapter for the governed AI risk-review seam.

The adapter is intentionally narrow: it receives frozen mechanical facts and may
only return TAKE, REDUCE, DELAY, or VETO. Network/API failures fail closed to
VETO rather than silently passing a trade.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


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


def make_openai_risk_adapter():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    model = os.environ.get("OPENAI_RISK_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    timeout_s = max(5, min(int(os.environ.get("OPENAI_RISK_TIMEOUT_S", "30")), 60))

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

    def adapter(facts: dict) -> dict:
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
    return adapter
