from typing import Tuple, Dict, Any


def survival_gate(settings, state: Dict[str, Any], confidence: float) -> Tuple[bool, str]:
    daily_pnl_pct = float(state.get("daily_pnl_pct", 0.0))
    total_drawdown_pct = float(state.get("total_drawdown_pct", 0.0))
    api_error_count = int(state.get("api_error_count", 0))

    if daily_pnl_pct <= -abs(settings.max_daily_loss_pct):
        return False, "daily loss limit reached"

    if total_drawdown_pct <= -abs(settings.max_total_drawdown_pct):
        return False, "total drawdown limit reached"

    if api_error_count >= settings.max_api_errors:
        return False, "too many api errors"

    if confidence < settings.min_llm_confidence:
        return False, "llm confidence too low"

    return True, "survival gate passed"


def calculate_final_alpha(row) -> float:
    import numpy as np

    final_alpha = (
        0.25 * row["factor_score"]
        + 0.25 * row["event_edge"]
        + 0.20 * row["sde_prob_score"]
        + 0.20 * row["trend_score"]
        + 0.10 * row["news_event_score"]
        - 0.10 * max(0.0, -row["risk_score"])
    )
    return float(np.clip(final_alpha, -1.0, 1.0))


def decide_action(settings, row) -> str:
    if (
        row["event_edge"] >= settings.min_event_edge
        and row["kelly_yes"] > 0
        and row["final_alpha"] >= settings.min_final_alpha
        and row["sde_prob_up"] >= settings.min_sde_prob_up
        and row["trend_score"] >= settings.min_trend_score
        and row["liquidity_score"] >= -0.30
    ):
        return "BUY"

    # SELL 임계값은 .env의 SELL_IF_* 설정을 따른다.  이전에는 0.10/-0.30/0.45가
    # 하드코딩되어 있어, event_edge≈0(LMSR 가격이 LLM 확률로 수렴한 상태)인
    # 평상시에 사실상 전 종목이 SELL로 분류되었다(운영 로그의
    # SELL_SIGNAL_NO_POSITION 2,449건).  포지션 청산 자체는 exit_rules가
    # 종목별 손절/익절/트레일링으로 따로 처리하므로 여기서는 신호 분류만 한다.
    if (
        row["event_edge"] <= float(getattr(settings, "sell_if_event_edge_below", -0.05))
        or row["kelly_no"] > settings.max_position_fraction
        or row["final_alpha"] <= float(getattr(settings, "sell_if_final_alpha_below", 0.10))
        or row["trend_score"] <= float(getattr(settings, "sell_if_trend_score_below", -0.30))
        or row["sde_prob_up"] <= float(getattr(settings, "sell_if_sde_prob_up_below", 0.45))
    ):
        return "SELL"

    return "HOLD"
