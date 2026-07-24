import numpy as np
import pandas as pd


def robust_zscore(s: pd.Series) -> pd.Series:
    """Cross-sectional robust z-score with an explicit neutral missing value.

    A missing fundamental or an unavailable order-book field is not evidence of
    a good or bad company.  Returning zero for it preserves that distinction
    and, importantly, prevents a partially populated real-data row from being
    mixed with a random demo value.
    """
    values = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)
    finite = values.dropna()
    if finite.empty:
        return pd.Series(np.zeros(len(values)), index=values.index, dtype=float)
    med = finite.median()
    mad = (finite - med).abs().median()
    if mad == 0 or np.isnan(mad):
        return pd.Series(np.zeros(len(values)), index=values.index, dtype=float)
    score = 0.6745 * (values - med) / mad
    return score.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def squash(x: float) -> float:
    return float(np.tanh(x))


def compute_factor_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["value_score"] = (
        robust_zscore(-out["per"])
        + robust_zscore(-out["pbr"])
        + robust_zscore(-out["ev_ebitda"])
        + robust_zscore(out["fcf_yield"])
        + robust_zscore(out["earnings_yield"])
        + robust_zscore(out["book_to_market"])
    ) / 6

    out["momentum_score"] = (
        robust_zscore(out["mom_12_1"])
        + robust_zscore(out["ret_6m"])
        + robust_zscore(out["ret_3m"])
        + robust_zscore(out["residual_momentum"])
        + robust_zscore(out["volume_breakout"])
        + robust_zscore(out["intraday_momentum"])
    ) / 6

    out["quality_score"] = (
        robust_zscore(out["roe"])
        + robust_zscore(out["roic"])
        + robust_zscore(out["gross_profitability"])
        + robust_zscore(out["operating_margin"])
        + robust_zscore(out["net_margin"])
        + robust_zscore(out["fcf_margin"])
        + robust_zscore(-out["accruals"])
        + robust_zscore(-out["debt_ratio"])
    ) / 8

    # 높을수록 안전한 Risk score
    out["risk_score"] = (
        robust_zscore(-out["beta"])
        + robust_zscore(-out["volatility"])
        + robust_zscore(-out["downside_beta"])
        + robust_zscore(-out["mdd"])
        + robust_zscore(-out["idio_vol"])
        + robust_zscore(-out["delisting_risk"])
    ) / 6

    out["liquidity_score"] = (
        robust_zscore(out["trading_value"])
        + robust_zscore(out["volume"])
        + robust_zscore(out["market_cap"])
        + robust_zscore(out["turnover"])
        + robust_zscore(-out["amihud"])
        + robust_zscore(-out["bid_ask_spread"])
    ) / 6

    out["growth_revision_score"] = (
        robust_zscore(out["sales_growth"])
        + robust_zscore(out["op_income_growth"])
        + robust_zscore(out["eps_growth"])
        + robust_zscore(out["earnings_revision"])
        + robust_zscore(out["target_price_revision"])
        + robust_zscore(out["order_backlog_growth"])
        + robust_zscore(out["margin_improvement"])
    ) / 7

    out["news_event_score"] = (
        robust_zscore(out["llm_sentiment"])
        + robust_zscore(out["disclosure_score"])
        + robust_zscore(out["policy_benefit"])
        + robust_zscore(-out["short_interest"])
        + robust_zscore(-out["crowding"])
        + robust_zscore(out["similar_event_score"])
    ) / 6

    out["factor_score_raw"] = (
        0.16 * out["value_score"]
        + 0.20 * out["momentum_score"]
        + 0.16 * out["quality_score"]
        + 0.12 * out["risk_score"]
        + 0.12 * out["liquidity_score"]
        + 0.14 * out["growth_revision_score"]
        + 0.10 * out["news_event_score"]
    )

    out["factor_score"] = out["factor_score_raw"].apply(lambda x: squash(float(x)))
    return out
