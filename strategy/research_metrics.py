"""Reproducible portfolio and execution-cost calculations for research.

These functions are intentionally separate from broker execution.  They make
the assumptions used in a backtest explicit (next-period return, turnover,
and a cost scenario) without pretending that a generic bps number is a Toss
fill fee.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping, Sequence

import numpy as np


def _finite_array(values: Sequence[float] | np.ndarray) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise ValueError("returns must be a one-dimensional sequence")
    if not np.all(np.isfinite(array)):
        raise ValueError("returns must contain only finite values")
    return array


@dataclass(frozen=True)
class CostScenario:
    """Research-only execution assumptions expressed in basis points."""

    commission_bps: float = 0.0
    spread_bps: float = 0.0
    slippage_bps: float = 0.0
    market_impact_bps: float = 0.0
    tax_bps: float = 0.0
    borrow_bps: float = 0.0

    @property
    def total_bps(self) -> float:
        return float(sum(asdict(self).values()))

    @property
    def rate(self) -> float:
        return self.total_bps / 10_000.0

    def estimated_cost(self, turnover: float) -> float:
        return max(0.0, float(turnover)) * self.rate

    def to_dict(self) -> dict[str, float]:
        return {**{key: float(value) for key, value in asdict(self).items()}, "total_bps": self.total_bps}


def individual_return(price_t: float, price_t1: float) -> float:
    """``r_i,t+1 = P_i,t+1 / P_i,t - 1``."""

    start = float(price_t)
    end = float(price_t1)
    if not math.isfinite(start) or not math.isfinite(end) or start <= 0.0:
        raise ValueError("prices must be finite and the starting price positive")
    return end / start - 1.0


def gross_portfolio_return(weights: Mapping[str, float], next_returns: Mapping[str, float]) -> float:
    """``R_gross = sum_i w_i,t * r_i,t+1`` with an explicit data check."""

    result = 0.0
    for symbol, weight in weights.items():
        if symbol not in next_returns:
            raise ValueError(f"next return missing for {symbol}")
        result += float(weight) * float(next_returns[symbol])
    if not math.isfinite(result):
        raise ValueError("gross return is not finite")
    return float(result)


def portfolio_turnover(current_weights: Mapping[str, float], previous_weights: Mapping[str, float]) -> float:
    """``TO_t = sum_i |w_i,t - w_i,t-1|`` (buy and sell legs both count)."""

    universe = set(current_weights) | set(previous_weights)
    turnover = sum(abs(float(current_weights.get(symbol, 0.0)) - float(previous_weights.get(symbol, 0.0))) for symbol in universe)
    if not math.isfinite(turnover):
        raise ValueError("turnover is not finite")
    return float(turnover)


def net_portfolio_return(gross_return: float, turnover: float, costs: CostScenario | float = 0.0) -> float:
    """``R_net = R_gross - c * TO`` for one explicitly declared cost scenario."""

    rate = costs.rate if isinstance(costs, CostScenario) else float(costs)
    if not math.isfinite(float(gross_return)) or not math.isfinite(float(turnover)) or not math.isfinite(rate):
        raise ValueError("gross return, turnover, and cost rate must be finite")
    return float(gross_return - rate * turnover)


def backtest_metrics(
    net_returns: Sequence[float] | np.ndarray,
    *,
    gross_returns: Sequence[float] | np.ndarray | None = None,
    turnovers: Sequence[float] | np.ndarray | None = None,
    trading_days_per_year: int = 252,
    risk_free_daily: float = 0.0,
) -> dict[str, Any]:
    """Calculate the published audit metrics from a net-return time series.

    - cumulative: ``prod(1+R_net)-1``
    - annual return: ``(1+CR) ** (252/T)-1``
    - annual volatility: population daily std times ``sqrt(252)``
    - Sharpe/Sortino: daily excess return divided by population standard
      deviation, annualised by ``sqrt(252)``
    - MDD: minimum drawdown of the wealth curve
    """

    returns = _finite_array(net_returns)
    if len(returns) == 0:
        raise ValueError("at least one net return is required")
    if trading_days_per_year <= 0:
        raise ValueError("trading_days_per_year must be positive")
    if np.any(returns <= -1.0):
        raise ValueError("returns must be greater than -100%")

    wealth = np.cumprod(1.0 + returns)
    cumulative_return = float(wealth[-1] - 1.0)
    annual_return = float(wealth[-1] ** (float(trading_days_per_year) / len(returns)) - 1.0)
    daily_volatility = float(np.std(returns, ddof=0))
    annual_volatility = float(daily_volatility * math.sqrt(trading_days_per_year))
    excess = returns - float(risk_free_daily)
    sharpe = None if daily_volatility == 0.0 else float(np.mean(excess) / daily_volatility * math.sqrt(trading_days_per_year))
    downside = np.minimum(excess, 0.0)
    downside_deviation = float(np.sqrt(np.mean(np.square(downside))))
    sortino = None if downside_deviation == 0.0 else float(np.mean(excess) / downside_deviation * math.sqrt(trading_days_per_year))
    peaks = np.maximum.accumulate(wealth)
    drawdowns = wealth / peaks - 1.0
    max_drawdown = float(np.min(drawdowns))

    result: dict[str, Any] = {
        "observations": int(len(returns)),
        "cumulative_return": cumulative_return,
        "annual_return": annual_return,
        "daily_volatility": daily_volatility,
        "annual_volatility": annual_volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "final_wealth_multiple": float(wealth[-1]),
        "risk_free_daily": float(risk_free_daily),
        "trading_days_per_year": int(trading_days_per_year),
    }
    if gross_returns is not None:
        gross = _finite_array(gross_returns)
        if len(gross) != len(returns):
            raise ValueError("gross_returns and net_returns must have the same length")
        result["gross_cumulative_return"] = float(np.prod(1.0 + gross) - 1.0)
    if turnovers is not None:
        turnover_values = _finite_array(turnovers)
        if len(turnover_values) != len(returns):
            raise ValueError("turnovers and net_returns must have the same length")
        result["mean_turnover"] = float(np.mean(turnover_values))
        result["total_turnover"] = float(np.sum(turnover_values))
    return result
