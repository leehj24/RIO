"""Small, dependency-free contracts for agent output.

LLM text is untrusted input.  The orchestration layer should validate it before
it is persisted, used as a trading signal, or passed to a risk gate.  These
contracts intentionally stop short of authorising an order: an
``AgentDecision`` is only a structured recommendation.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Any


class DecisionValidationError(ValueError):
    """Raised when a model response does not satisfy the decision contract."""

    def __init__(self, issues: Mapping[str, str]):
        self.issues = dict(issues)
        detail = "; ".join(f"{field}: {message}" for field, message in self.issues.items())
        super().__init__(f"Invalid agent decision ({detail})")


class JSONOutputError(ValueError):
    """Raised when an expected JSON object cannot be recovered from an output."""


_DIRECTION_ALIASES = {
    "buy": "buy",
    "long": "buy",
    "매수": "buy",
    "sell": "sell",
    "short": "sell",
    "매도": "sell",
    "hold": "hold",
    "neutral": "hold",
    "flat": "hold",
    "보유": "hold",
    "관망": "hold",
}


def _find_first_json_object(text: str) -> dict[str, Any] | None:
    """Return the first balanced JSON object in prose without regex guessing."""

    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(text)):
            current = text[end]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[start : end + 1])
                    except json.JSONDecodeError:
                        break
                    return candidate if isinstance(candidate, dict) else None
    return None


def json_object(value: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
    """Coerce an object or a typical fenced LLM JSON response into a dict.

    The function does not attempt to repair malformed JSON.  That keeps a
    retry path honest and prevents an ambiguous response from becoming a trade
    recommendation silently.
    """

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise JSONOutputError("Expected a mapping, JSON string, or UTF-8 bytes")

    text = value.strip()
    if not text:
        raise JSONOutputError("Response is empty")

    candidates = [text]
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))

    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
        raise JSONOutputError("JSON response must be an object")

    embedded = _find_first_json_object(text)
    if embedded is not None:
        return embedded
    raise JSONOutputError("Could not find a valid JSON object in the response")


def _number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError("must be a number, not a boolean")
    if isinstance(value, Real):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise ValueError("must be numeric") from exc
    else:
        raise ValueError("must be numeric")
    if not math.isfinite(number):
        raise ValueError("must be finite")
    if not minimum <= number <= maximum:
        raise ValueError(f"must be between {minimum:g} and {maximum:g}")
    return number


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise ValueError("must not be empty")
    return result


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError("must be an array of strings")
    values: list[str] = []
    for index, item in enumerate(value):
        try:
            values.append(_string(item, field))
        except ValueError as exc:
            raise ValueError(f"item {index} {exc}") from exc
    return tuple(values)


@dataclass(frozen=True, slots=True)
class AgentDecision:
    """Normalized recommendation emitted by a trading-related agent.

    ``target_exposure`` is a signed target in the inclusive range ``[-1, 1]``.
    A negative value is useful for research and simulation, but it is *not* an
    instruction to short a live account.  The deterministic risk/order layer
    remains responsible for applying the account's actual constraints.
    """

    direction: str
    target_exposure: float
    confidence: float
    evidence_ids: tuple[str, ...]
    invalidation: tuple[str, ...]
    risk_flags: tuple[str, ...]

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | str | bytes) -> "AgentDecision":
        return validate_agent_decision(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "target_exposure": self.target_exposure,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "invalidation": list(self.invalidation),
            "risk_flags": list(self.risk_flags),
        }


def validate_agent_decision(value: Mapping[str, Any] | str | bytes) -> AgentDecision:
    """Validate and normalize the common final-decision JSON contract.

    Required keys are intentionally strict so a malformed response can be
    retried or blocked before it reaches a live-trading path.  Unknown keys are
    tolerated; callers can retain the raw response in their audit event.
    """

    try:
        payload = json_object(value)
    except JSONOutputError as exc:
        raise DecisionValidationError({"response": str(exc)}) from exc

    required = (
        "direction",
        "target_exposure",
        "confidence",
        "evidence_ids",
        "invalidation",
        "risk_flags",
    )
    issues: dict[str, str] = {
        field: "is required" for field in required if field not in payload
    }
    if issues:
        raise DecisionValidationError(issues)

    try:
        direction_source = _string(payload["direction"], "direction")
        direction = _DIRECTION_ALIASES.get(direction_source.casefold())
        if direction is None:
            raise ValueError("must be one of buy, sell, or hold")
    except ValueError as exc:
        issues["direction"] = str(exc)
        direction = "hold"

    fields: dict[str, Any] = {}
    validations = (
        ("target_exposure", lambda: _number(payload["target_exposure"], "target_exposure", minimum=-1.0, maximum=1.0)),
        ("confidence", lambda: _number(payload["confidence"], "confidence", minimum=0.0, maximum=1.0)),
        ("evidence_ids", lambda: _string_list(payload["evidence_ids"], "evidence_ids")),
        ("invalidation", lambda: _invalidation_list(payload["invalidation"])),
        ("risk_flags", lambda: _string_list(payload["risk_flags"], "risk_flags")),
    )
    for field, validator in validations:
        try:
            fields[field] = validator()
        except ValueError as exc:
            issues[field] = str(exc)

    if issues:
        raise DecisionValidationError(issues)
    return AgentDecision(direction=direction, **fields)


def _invalidation_list(value: Any) -> tuple[str, ...]:
    """Normalise a legacy string or the current list-based invalidation field."""

    if isinstance(value, str):
        return (_string(value, "invalidation"),)
    items = _string_list(value, "invalidation")
    if not items:
        raise ValueError("must contain at least one invalidation condition")
    return items
