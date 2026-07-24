"""Math and capability checks for exact speculative decoding.

This module does *not* try to emulate speculative decoding by issuing two
remote Gemini requests.  Exact decoding needs compatible draft/target token
probabilities, a shared tokenizer, and target-model verification access.  The
helpers are useful for an eventual self-hosted runtime and for honest latency
benchmarks today.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Sequence

import numpy as np


def _distribution(values: Sequence[float]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or len(array) == 0 or not np.all(np.isfinite(array)) or np.any(array < 0.0):
        raise ValueError("distribution must be a finite, non-negative one-dimensional sequence")
    total = float(array.sum())
    if total <= 0.0:
        raise ValueError("distribution must have positive mass")
    return array / total


def acceptance_probability(target_probability: float, draft_probability: float) -> float:
    """``min(1, p(x) / q(x))`` for one proposed token."""

    p = float(target_probability)
    q = float(draft_probability)
    if p < 0.0 or q < 0.0 or not math.isfinite(p) or not math.isfinite(q):
        raise ValueError("probabilities must be finite and non-negative")
    if q == 0.0:
        return 1.0 if p > 0.0 else 0.0
    return min(1.0, p / q)


def correction_distribution(target: Sequence[float], draft: Sequence[float]) -> np.ndarray:
    """Normalised ``[p - q]_+`` used when a draft token is rejected."""

    p = _distribution(target)
    q = _distribution(draft)
    if p.shape != q.shape:
        raise ValueError("target and draft distributions must have equal vocabulary size")
    residual = np.maximum(p - q, 0.0)
    mass = float(residual.sum())
    # Equal distributions have no rejection correction.  Returning target is
    # a stable convention for callers that still need a valid distribution.
    return p if mass <= 1e-15 else residual / mass


def total_variation(target: Sequence[float], draft: Sequence[float]) -> float:
    p = _distribution(target)
    q = _distribution(draft)
    if p.shape != q.shape:
        raise ValueError("target and draft distributions must have equal vocabulary size")
    return float(0.5 * np.abs(p - q).sum())


def mean_acceptance_rate(target: Sequence[float], draft: Sequence[float]) -> float:
    """``alpha = 1 - TV(p, q)`` for a token distribution pair."""

    return float(1.0 - total_variation(target, draft))


def expected_emitted_tokens(acceptance_rate: float, gamma: int) -> float:
    """``E[N] = (1-alpha^(gamma+1)) / (1-alpha)`` with alpha=1 limit."""

    alpha = float(acceptance_rate)
    if not 0.0 <= alpha <= 1.0 or int(gamma) < 1:
        raise ValueError("acceptance_rate must be in [0, 1] and gamma positive")
    gamma = int(gamma)
    if alpha == 1.0:
        return float(gamma + 1)
    return float((1.0 - alpha ** (gamma + 1)) / (1.0 - alpha))


def expected_walltime_speedup(acceptance_rate: float, gamma: int, draft_to_target_cost_ratio: float) -> float:
    """``E[N] / (1 + gamma*c)`` under the document's simple cost model."""

    cost = float(draft_to_target_cost_ratio)
    if cost < 0.0 or not math.isfinite(cost):
        raise ValueError("draft_to_target_cost_ratio must be finite and non-negative")
    return expected_emitted_tokens(acceptance_rate, gamma) / (1.0 + int(gamma) * cost)


def speculation_is_worthwhile(acceptance_rate: float, draft_to_target_cost_ratio: float) -> bool:
    """Necessary condition from the source: ``alpha > c``."""

    return float(acceptance_rate) > float(draft_to_target_cost_ratio)


@dataclass(frozen=True)
class SpeculativeDecodingCapability:
    supported: bool
    reason: str
    target_logits: bool = False
    draft_logits: bool = False
    shared_tokenizer: bool = False
    kv_cache_control: bool = False

    def to_dict(self) -> dict[str, bool | str]:
        return asdict(self)


def managed_api_capability(provider_name: str) -> SpeculativeDecodingCapability:
    """Describe why ordinary managed text APIs cannot run exact speculation."""

    return SpeculativeDecodingCapability(
        supported=False,
        reason=f"{provider_name} managed generate API does not expose compatible draft/target logits or KV-cache control",
    )


def latency_percentiles(milliseconds: Iterable[float]) -> dict[str, float | int | None]:
    """Small, dependency-free p50/p95 telemetry summary for agent calls."""

    values = np.asarray(list(milliseconds), dtype=float)
    values = values[np.isfinite(values) & (values >= 0.0)]
    if len(values) == 0:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "mean_ms": None}
    return {
        "count": int(len(values)),
        "p50_ms": float(np.percentile(values, 50)),
        "p95_ms": float(np.percentile(values, 95)),
        "mean_ms": float(np.mean(values)),
    }
