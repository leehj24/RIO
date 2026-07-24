"""Numerical lead--lag candidate generation for the semantic risk filter.

LLMs do not discover time-series relationships in this module.  They may only
later assess a *numerically generated* candidate's economic mechanism and
data timing.  The calculation uses log returns (rather than price levels), a
directional lagged OLS F-test, Benjamini--Hochberg FDR control, and optional
rolling-window stability.  It is research-only until a validated data source
and an explicit runtime policy enable it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.stats import f as f_distribution


def _as_finite(values: Sequence[float] | np.ndarray, *, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or len(array) < 2:
        raise ValueError(f"{name} must be a one-dimensional sequence with at least two values")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    return array


def log_returns(prices: Sequence[float] | np.ndarray) -> np.ndarray:
    """``r_t = log(P_t) - log(P_t-1)`` used for stock-price candidates."""

    values = _as_finite(prices, name="prices")
    if np.any(values <= 0.0):
        raise ValueError("prices must be positive")
    return np.diff(np.log(values))


def benjamini_hochberg(p_values: Sequence[float], *, alpha: float = 0.05) -> list[bool]:
    """Return FDR discoveries in the original input order."""

    if not 0.0 < float(alpha) < 1.0:
        raise ValueError("alpha must be between zero and one")
    values = np.asarray(p_values, dtype=float)
    if values.ndim != 1 or not np.all(np.isfinite(values)) or np.any((values < 0.0) | (values > 1.0)):
        raise ValueError("p_values must be finite values in [0, 1]")
    if len(values) == 0:
        return []
    order = np.argsort(values)
    ranked = values[order]
    thresholds = float(alpha) * (np.arange(1, len(values) + 1) / len(values))
    passing = np.where(ranked <= thresholds)[0]
    discoveries = np.zeros(len(values), dtype=bool)
    if len(passing):
        discoveries[order[: int(passing[-1]) + 1]] = True
    return discoveries.tolist()


@dataclass(frozen=True)
class GrangerResult:
    lag: int
    observations: int
    f_statistic: float
    p_value: float
    restricted_rss: float
    unrestricted_rss: float
    correlation: float
    leader_lag_effect: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _design_lagged(target: np.ndarray, leader: np.ndarray, lag: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if len(target) != len(leader):
        raise ValueError("target and leader must have equal observations")
    if lag < 1:
        raise ValueError("lag must be positive")
    if len(target) <= 3 * lag + 5:
        raise ValueError("insufficient observations for requested lag")
    y = target[lag:]
    target_lags = np.column_stack([target[lag - step : len(target) - step] for step in range(1, lag + 1)])
    leader_lags = np.column_stack([leader[lag - step : len(leader) - step] for step in range(1, lag + 1)])
    restricted = np.column_stack([np.ones(len(y)), target_lags])
    unrestricted = np.column_stack([restricted, leader_lags])
    return y, restricted, unrestricted


def granger_f_test(leader_returns: Sequence[float], follower_returns: Sequence[float], *, lag: int = 1) -> GrangerResult:
    """Test whether lagged ``leader`` returns improve prediction of ``follower``.

    This is the conventional restricted/unrestricted OLS F statistic.  It is
    predictive Granger evidence, never proof of structural causality.
    """

    leader = _as_finite(leader_returns, name="leader_returns")
    follower = _as_finite(follower_returns, name="follower_returns")
    y, restricted, unrestricted = _design_lagged(follower, leader, int(lag))
    restricted_fit, *_ = np.linalg.lstsq(restricted, y, rcond=None)
    unrestricted_fit, *_ = np.linalg.lstsq(unrestricted, y, rcond=None)
    restricted_rss = float(np.sum(np.square(y - restricted @ restricted_fit)))
    unrestricted_rss = float(np.sum(np.square(y - unrestricted @ unrestricted_fit)))
    q = int(lag)
    dof = len(y) - unrestricted.shape[1]
    if dof <= 0 or unrestricted_rss < 0.0:
        raise ValueError("insufficient degrees of freedom for requested lag")
    if unrestricted_rss <= 1e-18:
        f_stat = math.inf if restricted_rss > unrestricted_rss else 0.0
        p_value = 0.0 if math.isinf(f_stat) else 1.0
    else:
        f_stat = max(0.0, ((restricted_rss - unrestricted_rss) / q) / (unrestricted_rss / dof))
        p_value = float(f_distribution.sf(f_stat, q, dof))
    correlation = float(np.corrcoef(leader, follower)[0, 1]) if len(leader) > 1 else 0.0
    if not math.isfinite(correlation):
        correlation = 0.0
    return GrangerResult(
        lag=int(lag),
        observations=int(len(y)),
        f_statistic=float(f_stat),
        p_value=float(np.clip(p_value, 0.0, 1.0)),
        restricted_rss=restricted_rss,
        unrestricted_rss=unrestricted_rss,
        correlation=correlation,
        leader_lag_effect=float(np.sum(unrestricted_fit[-q:])),
    )


def select_granger_lag(
    leader_returns: Sequence[float],
    follower_returns: Sequence[float],
    *,
    max_lag: int = 3,
) -> GrangerResult:
    """Choose the smallest p-value, resolving ties in favour of shorter lag."""

    if max_lag < 1:
        raise ValueError("max_lag must be positive")
    results: list[GrangerResult] = []
    for lag in range(1, int(max_lag) + 1):
        try:
            results.append(granger_f_test(leader_returns, follower_returns, lag=lag))
        except ValueError:
            continue
    if not results:
        raise ValueError("insufficient observations for every requested lag")
    return min(results, key=lambda result: (result.p_value, result.lag))


def rolling_stability(
    leader_returns: Sequence[float],
    follower_returns: Sequence[float],
    *,
    lag: int,
    window: int = 60,
    step: int = 20,
    alpha: float = 0.05,
) -> tuple[float, int]:
    """Share of rolling windows with a same-direction significant relation."""

    leader = _as_finite(leader_returns, name="leader_returns")
    follower = _as_finite(follower_returns, name="follower_returns")
    if len(leader) != len(follower):
        raise ValueError("leader_returns and follower_returns must have equal observations")
    if window <= 3 * lag + 5 or step < 1:
        raise ValueError("window/step do not support the requested lag")
    outcomes: list[bool] = []
    for end in range(window, len(leader) + 1, step):
        result = granger_f_test(leader[end - window : end], follower[end - window : end], lag=lag)
        outcomes.append(result.p_value <= alpha)
    return (float(np.mean(outcomes)) if outcomes else 0.0, len(outcomes))


@dataclass(frozen=True)
class LeadLagCandidate:
    leader: str
    follower: str
    lag: int
    observations: int
    f_statistic: float
    p_value: float
    fdr_significant: bool
    correlation: float
    expected_sign: str
    stability: float
    stability_windows: int
    statistical_score: float
    research_only: bool = True
    preprocessing: str = "log_returns"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_candidates(
    price_series: Mapping[str, Sequence[float]],
    *,
    max_lag: int = 3,
    fdr_alpha: float = 0.05,
    stability_window: int = 60,
    stability_step: int = 20,
    minimum_observations: int = 80,
) -> list[LeadLagCandidate]:
    """Generate directional, FDR-labelled candidates from aligned price series."""

    returns_by_symbol: dict[str, np.ndarray] = {}
    for symbol, prices in price_series.items():
        try:
            returns = log_returns(prices)
        except ValueError:
            continue
        if len(returns) >= minimum_observations:
            returns_by_symbol[str(symbol)] = returns

    raw: list[tuple[str, str, GrangerResult, float, int]] = []
    symbols = sorted(returns_by_symbol)
    for leader in symbols:
        for follower in symbols:
            if leader == follower:
                continue
            length = min(len(returns_by_symbol[leader]), len(returns_by_symbol[follower]))
            if length < minimum_observations:
                continue
            leader_returns = returns_by_symbol[leader][-length:]
            follower_returns = returns_by_symbol[follower][-length:]
            try:
                result = select_granger_lag(leader_returns, follower_returns, max_lag=max_lag)
                stability, windows = rolling_stability(
                    leader_returns,
                    follower_returns,
                    lag=result.lag,
                    window=min(stability_window, length),
                    step=stability_step,
                    alpha=fdr_alpha,
                )
            except ValueError:
                continue
            raw.append((leader, follower, result, stability, windows))

    discoveries = benjamini_hochberg([item[2].p_value for item in raw], alpha=fdr_alpha)
    candidates: list[LeadLagCandidate] = []
    for (leader, follower, result, stability, windows), fdr_significant in zip(raw, discoveries):
        p_strength = max(0.0, min(1.0, 1.0 - result.p_value))
        statistical_score = 0.7 * p_strength + 0.3 * stability
        candidates.append(
            LeadLagCandidate(
                leader=leader,
                follower=follower,
                lag=result.lag,
                observations=result.observations,
                f_statistic=result.f_statistic,
                p_value=result.p_value,
                fdr_significant=bool(fdr_significant),
                correlation=result.correlation,
                # Direction comes from the lagged leader coefficients, not
                # the contemporaneous Pearson correlation (which can differ
                # in a genuine one-period lead--lag relation).
                expected_sign="same" if result.leader_lag_effect >= 0.0 else "opposite",
                stability=stability,
                stability_windows=windows,
                statistical_score=statistical_score,
            )
        )
    return sorted(candidates, key=lambda item: (item.fdr_significant, item.statistical_score, -item.p_value), reverse=True)


def composite_candidate_score(
    candidate: LeadLagCandidate,
    *,
    semantic_score: float = 0.0,
    liquidity_score: float = 0.0,
    cost_penalty: float = 0.0,
    weights: Mapping[str, float] | None = None,
) -> float:
    """Transparent implementation of the document's hybrid ranking example.

    ``w1*Granger_FDR + w2*stability + w3*semantic + w4*liquidity - w5*cost``.
    The default weights are a project convention and are returned by callers
    in their audit record; they are not asserted to be paper parameters.
    """

    weights = {"granger": 0.35, "stability": 0.25, "semantic": 0.25, "liquidity": 0.10, "cost": 0.05, **dict(weights or {})}
    fdr_score = candidate.statistical_score if candidate.fdr_significant else 0.0
    return float(
        weights["granger"] * fdr_score
        + weights["stability"] * float(candidate.stability)
        + weights["semantic"] * float(np.clip(semantic_score, 0.0, 1.0))
        + weights["liquidity"] * float(np.clip(liquidity_score, 0.0, 1.0))
        - weights["cost"] * max(0.0, float(cost_penalty))
    )
