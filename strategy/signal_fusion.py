"""Auditable fusion of fact, subjectivity, and quantitative signals.

The LLM may propose a direction and confidence for the first two paths, while
the numerical path is calculated in Python.  This module intentionally only
produces a research/attribution signal; the existing hard risk rules remain
the authority for an executable order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping

import numpy as np


@dataclass(frozen=True)
class SignalFusionWeights:
    fact: float = 0.4
    subjectivity: float = 0.2
    quantitative: float = 0.4

    def __post_init__(self) -> None:
        values = (float(self.fact), float(self.subjectivity), float(self.quantitative))
        if any(value < 0.0 or not math.isfinite(value) for value in values):
            raise ValueError("signal fusion weights must be finite and non-negative")
        if not math.isclose(sum(values), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("fact + subjectivity + quantitative weights must equal one")

    def to_dict(self) -> dict[str, float]:
        return {key: float(value) for key, value in asdict(self).items()}


def direction_score(direction: Any, confidence: Any = 1.0) -> float:
    """Map a bounded direction/confidence report to ``[-1, 1]``."""

    normalized = str(direction or "hold").strip().lower()
    sign = {"buy": 1.0, "long": 1.0, "sell": -1.0, "short": -1.0, "hold": 0.0}.get(normalized, 0.0)
    try:
        strength = float(confidence)
    except (TypeError, ValueError):
        strength = 0.0
    return float(sign * np.clip(strength, 0.0, 1.0))


def fuse_signals(
    fact_signal: float,
    subjectivity_signal: float,
    quantitative_signal: float,
    *,
    weights: SignalFusionWeights | None = None,
) -> dict[str, Any]:
    """Compute ``s_t = wF*sF + wS*sS + wQ*sQ`` and attribution fields."""

    weights = weights or SignalFusionWeights()
    components = {
        "fact": float(np.clip(float(fact_signal), -1.0, 1.0)),
        "subjectivity": float(np.clip(float(subjectivity_signal), -1.0, 1.0)),
        "quantitative": float(np.clip(float(quantitative_signal), -1.0, 1.0)),
    }
    contributions = {
        "fact": weights.fact * components["fact"],
        "subjectivity": weights.subjectivity * components["subjectivity"],
        "quantitative": weights.quantitative * components["quantitative"],
    }
    score = float(np.clip(sum(contributions.values()), -1.0, 1.0))
    non_zero_signs = {int(math.copysign(1, value)) for value in components.values() if abs(value) > 1e-12}
    return {
        "formula": "s_t = w_fact*s_fact + w_subjectivity*s_subjectivity + w_quantitative*s_quantitative",
        "weights": weights.to_dict(),
        "components": components,
        "contributions": contributions,
        "score": score,
        "agreement": "mixed" if len(non_zero_signs) > 1 else "aligned" if non_zero_signs else "neutral",
        "research_only": True,
    }


def fusion_from_agent_reports(reasoning_signals: Mapping[str, Any] | None, quantitative_signal: float) -> dict[str, Any]:
    """Use only structured agent direction hints; malformed values become 0."""

    reports = reasoning_signals or {}
    return fuse_signals(
        direction_score(reports.get("fact_direction_hint"), reports.get("fact_confidence")),
        direction_score(reports.get("subjectivity_direction_hint"), reports.get("subjectivity_confidence")),
        quantitative_signal,
    )
