import math
from typing import Dict
import numpy as np
from scipy.stats import norm


def gbm_prob_up(
    spot: float,
    mu: float,
    sigma: float,
    horizon_days: int = 20,
    target_return: float = 0.03,
) -> float:
    """
    GBM:
    dS/S = mu dt + sigma dW
    """
    if spot <= 0 or sigma <= 0:
        return 0.5

    T = horizon_days / 252.0
    threshold = math.log(1.0 + target_return)
    mean = (mu - 0.5 * sigma * sigma) * T
    std = sigma * math.sqrt(T)

    z = (threshold - mean) / std
    return float(1.0 - norm.cdf(z))


def build_sde_inputs(row) -> Dict[str, float]:
    base_mu = 0.03

    mu = (
        base_mu
        + 0.12 * float(row["factor_score"])
        + 0.15 * float(row["event_edge"])
        + 0.10 * float(row["trend_score"])
        + 0.06 * float(row["news_event_score"])
    )

    hist_vol = max(0.05, float(row["volatility"]))
    disclosure_multiplier = 1.0 + max(0.0, float(row.get("disclosure_risk", 0.0)))
    news_multiplier = 1.0 + max(0.0, -float(row["news_event_score"])) * 0.20
    risk_multiplier = 1.0 + max(0.0, -float(row["risk_score"])) * 0.20

    sigma = hist_vol * disclosure_multiplier * news_multiplier * risk_multiplier

    return {
        "mu": float(mu),
        "sigma": float(np.clip(sigma, 0.05, 1.50)),
    }
