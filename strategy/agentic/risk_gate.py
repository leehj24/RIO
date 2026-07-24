"""Deterministic handling of a Gemini semantic-risk assessment.

The LLM can explain why a signal is fragile, but it never gets permission to
create an order.  This module converts its bounded, structured assessment into
an auditable position-size constraint that the existing Python risk and order
builders can enforce.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class RiskGateDecision:
    allowed: bool
    size_multiplier: float
    verdict: str
    reasons: List[str]
    flags: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def apply_semantic_risk_assessment(
    assessment: Dict[str, Any] | None,
    *,
    default_allowed: bool = True,
) -> RiskGateDecision:
    """Convert a validated semantic-risk response into a safe code decision.

    Supported LLM verdicts are ``pass``, ``reduce`` and ``hold``.  Unknown,
    malformed, or unavailable results fail closed: they cannot increase a
    position and are treated as ``hold`` when this gate is being enforced.
    """
    raw = assessment or {}
    verdict = str(raw.get("verdict", raw.get("action", "hold"))).strip().lower()
    flags = _as_list(raw.get("risk_flags", raw.get("flags")))
    reasons = _as_list(raw.get("reasons", raw.get("reason")))

    if verdict == "pass":
        requested = raw.get("size_multiplier", 1.0)
        try:
            multiplier = min(1.0, max(0.0, float(requested)))
        except (TypeError, ValueError):
            multiplier = 1.0
        return RiskGateDecision(
            allowed=bool(default_allowed and multiplier > 0.0),
            size_multiplier=multiplier,
            verdict="pass",
            reasons=reasons or ["semantic risk assessment passed"],
            flags=flags,
        )

    if verdict == "reduce":
        requested = raw.get("size_multiplier", 0.5)
        try:
            multiplier = min(0.75, max(0.0, float(requested)))
        except (TypeError, ValueError):
            multiplier = 0.5
        return RiskGateDecision(
            allowed=bool(default_allowed and multiplier > 0.0),
            size_multiplier=multiplier,
            verdict="reduce",
            reasons=reasons or ["semantic risk assessment reduced exposure"],
            flags=flags,
        )

    return RiskGateDecision(
        allowed=False,
        size_multiplier=0.0,
        verdict="hold",
        reasons=reasons or ["semantic risk assessment unavailable, invalid, or hold"],
        flags=flags or (["semantic_risk_unavailable"] if not assessment else []),
    )
