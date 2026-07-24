"""Bounded, diversified live-buy allocation in KRW.

The values returned here are only sizing inputs.  The caller must still pass
the strategy, technical, market-open, tradability, and broker-order guards
before sending a live order.
"""

from __future__ import annotations

from typing import Any, Mapping


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def position_value_krw(position: Mapping[str, Any] | None, usd_krw_fx_rate: float) -> float:
    """Use the broker evaluation amount, converting explicitly labelled USD."""

    if not position:
        return 0.0
    value = max(0.0, _number(position.get("_market_value")))
    currency = str(
        position.get("currency")
        or position.get("marketCurrency")
        or position.get("settlementCurrency")
        or "KRW"
    ).upper()
    return value * max(1.0, float(usd_krw_fx_rate)) if currency in {"USD", "$"} else value


def current_exposure_krw(positions: Mapping[str, Mapping[str, Any]], usd_krw_fx_rate: float) -> float:
    return sum(position_value_krw(position, usd_krw_fx_rate) for position in positions.values())


def buy_budget_krw(settings: Any, cash_krw: float, positions: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    """Return cash- and portfolio-cap-constrained budget for this run."""

    cash = max(0.0, float(cash_krw))
    cash_after_reserve = cash * max(0.0, 1.0 - float(settings.cash_reserve_fraction))
    explicit_total_cap = max(0.0, float(settings.auto_buy_total_cap_krw))
    exposure_fraction_cap = max(0.0, float(getattr(settings, "max_total_exposure_fraction", 1.0)))
    # The explicit small-account cap and the percentage cap are independent
    # hard limits.  Apply whichever is tighter; neither may silently replace
    # the other.
    total_cap = min(explicit_total_cap, cash * exposure_fraction_cap)
    exposure = current_exposure_krw(positions, float(settings.usd_krw_fx_rate))
    portfolio_room = max(0.0, total_cap - exposure)
    return {
        "cash_after_reserve_krw": cash_after_reserve,
        "total_exposure_cap_krw": total_cap,
        "current_exposure_krw": exposure,
        "portfolio_room_krw": portfolio_room,
        "run_budget_krw": min(cash_after_reserve, portfolio_room),
    }


def diversified_order_value_krw(
    settings: Any,
    *,
    run_budget_krw: float,
    deployed_this_run_krw: float,
    new_buys_this_run: int,
    current_position: Mapping[str, Any] | None,
    risk_sized_order_krw: float,
) -> float:
    """Size one approved buy within total and per-symbol portfolio caps.

    A strategy-approved buy receives an equal share of the remaining budget,
    subject to the per-symbol cap.  The larger quantitative risk size is kept
    when it remains inside those same hard limits.
    """

    remaining = max(0.0, float(run_budget_krw) - float(deployed_this_run_krw))
    if remaining <= 0:
        return 0.0

    per_symbol_cap = max(0.0, float(settings.auto_buy_per_symbol_cap_krw))
    already_in_symbol = position_value_krw(current_position, float(settings.usd_krw_fx_rate))
    symbol_room = max(0.0, per_symbol_cap - already_in_symbol)
    if symbol_room <= 0:
        return 0.0

    minimum_ticket = max(
        float(settings.min_order_krw),
        float(settings.min_order_usd) * float(settings.usd_krw_fx_rate),
    )
    slots_left = max(1, int(settings.max_new_buys_per_loop) - int(new_buys_this_run))
    affordable_slots = max(1, int(remaining // minimum_ticket))
    allocation_slots = min(slots_left, affordable_slots)
    equal_share = remaining / allocation_slots

    target = max(float(risk_sized_order_krw), equal_share)
    return min(target, remaining, symbol_room)
