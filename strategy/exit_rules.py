from typing import Dict, Any


def _to_float(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def evaluate_exit_rules(settings, row, technical: Dict[str, Any], position: Dict[str, Any] | None, current_price: float) -> Dict[str, Any]:
    """
    기존 EventEdge/FinalAlpha/SDE/Trend 기반 매도 + 사용자가 말한 기술적 매도 룰을 통합.

    출력:
    - HOLD
    - SELL
    - PARTIAL_SELL
    """
    position = position or {}
    qty = _to_float(position.get("_quantity", 0.0))
    avg = _to_float(position.get("_avg_price", 0.0))
    has_position = qty > 0 and avg > 0

    triggers = []
    sell_fraction = 0.0

    if not has_position:
        # 포지션이 없으면 신규 매도 주문은 만들 수 없지만 signal은 남긴다.
        if (
            row["event_edge"] <= -settings.min_event_edge
            or row["kelly_no"] > settings.max_position_fraction
            or row["final_alpha"] <= settings.sell_if_final_alpha_below
            or row["trend_score"] <= settings.sell_if_trend_score_below
            or row["sde_prob_up"] <= settings.sell_if_sde_prob_up_below
            or technical.get("sell_signal")
        ):
            return {
                "action": "SELL_SIGNAL_NO_POSITION",
                "sell_fraction": 0.0,
                "triggers": ["sell_signal_but_no_position"],
                "pnl_pct": None,
            }
        return {"action": "HOLD", "sell_fraction": 0.0, "triggers": [], "pnl_pct": None}

    pnl_pct = (float(current_price) - avg) / avg if avg > 0 else 0.0

    # 가격 기반 exit
    if pnl_pct <= -abs(settings.stop_loss_pct):
        triggers.append("stop_loss_pct")
        sell_fraction = 1.0

    if pnl_pct >= abs(settings.take_profit_pct):
        triggers.append("take_profit_pct")
        sell_fraction = max(sell_fraction, 1.0)

    # trailing stop은 state 기반 고점 갱신이 필요하므로 v6에서는 신호값만 로그화.
    # 실전 고도화 시 position["highest_price"]를 저장해서 적용.
    if pnl_pct >= abs(settings.trailing_activation_pct) and technical.get("sell_signal"):
        triggers.append("technical_trailing_exit_after_profit")
        sell_fraction = max(sell_fraction, 1.0)

    # 기존 alpha/model 기반 exit
    if row["event_edge"] <= -settings.min_event_edge:
        triggers.append("event_edge_negative")
        sell_fraction = max(sell_fraction, 1.0)

    if row["kelly_no"] > settings.max_position_fraction:
        triggers.append("kelly_no_high")
        sell_fraction = max(sell_fraction, 1.0)

    if row["final_alpha"] <= settings.sell_if_final_alpha_below:
        triggers.append("final_alpha_low")
        sell_fraction = max(sell_fraction, 1.0)

    if row["trend_score"] <= settings.sell_if_trend_score_below:
        triggers.append("trend_score_low")
        sell_fraction = max(sell_fraction, 1.0)

    if row["sde_prob_up"] <= settings.sell_if_sde_prob_up_below:
        triggers.append("sde_prob_up_low")
        sell_fraction = max(sell_fraction, 1.0)

    # 기술적 exit
    exit_triggers = technical.get("exit_triggers", [])
    if "dolpa_midline_close_break_long" in exit_triggers:
        triggers.append("dolpa_midline_stop")
        sell_fraction = max(sell_fraction, 1.0)

    if "box_breakout_down" in exit_triggers:
        triggers.append("box_breakdown_exit")
        sell_fraction = max(sell_fraction, 1.0)

    if technical.get("sell_signal"):
        triggers.extend([f"technical_{x}" for x in technical.get("sell_triggers", [])])
        sell_fraction = max(sell_fraction, 1.0)

    # 실선 반대신호 2회: 50% 분할익절
    if technical.get("partial_exit_signal") and pnl_pct > 0:
        triggers.append("fast_opposite_signal_twice_partial_take_profit")
        sell_fraction = max(sell_fraction, settings.partial_take_profit_fraction)

    if not triggers:
        return {"action": "HOLD", "sell_fraction": 0.0, "triggers": [], "pnl_pct": pnl_pct}

    if 0.0 < sell_fraction < 1.0:
        return {"action": "PARTIAL_SELL", "sell_fraction": sell_fraction, "triggers": triggers, "pnl_pct": pnl_pct}

    return {"action": "SELL", "sell_fraction": 1.0, "triggers": triggers, "pnl_pct": pnl_pct}
