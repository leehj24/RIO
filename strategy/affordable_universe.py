"""Cash-aware, bounded candidate selection for the daily opportunity event.

This module never recommends or orders a symbol. It converts current buying
power, prices, and the existing enabled KR/US registry into a deterministic
set that the normal research and Python risk pipeline may evaluate.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from strategy.order_builder import build_buy_order


CASH_AFFORDABLE_EVENT_ID = "cash_affordable_candidates"


def is_cash_affordable_event(event: Any) -> bool:
    return str(getattr(event, "event_id", "") or "") == CASH_AFFORDABLE_EVENT_ID


def _daily_rank(research_day: str, symbol: str) -> str:
    return hashlib.sha256(f"{research_day}|{symbol}".encode("utf-8")).hexdigest()


def _candidate_sort_key(research_day: str, priority_symbols: set[str], item: Mapping[str, Any]) -> tuple:
    symbol = str(item.get("symbol") or "")
    return (0 if symbol in priority_symbols else 1, _daily_rank(research_day, symbol), symbol)


def cash_market_order_budgets(
    settings: Any,
    *,
    deployable_bankroll_krw: float,
    deployed_this_run_krw: float,
    buying_power_after_reserve: Mapping[str, float],
    deployed_by_market: Mapping[str, float],
) -> dict[str, float]:
    """Return the largest possible new ticket for KR and US right now."""

    run_room = max(0.0, float(deployable_bankroll_krw) - float(deployed_this_run_krw))
    per_symbol_cap = max(0.0, float(settings.auto_buy_per_symbol_cap_krw))
    budgets: dict[str, float] = {}
    for market in ("KR", "US"):
        market_room = max(
            0.0,
            float(buying_power_after_reserve.get(market, 0.0))
            - float(deployed_by_market.get(market, 0.0)),
        )
        budgets[market] = min(run_room, per_symbol_cap, market_room)
    return budgets


def select_cash_affordable_candidates(
    settings: Any,
    *,
    candidate_symbols: Sequence[str],
    symbol_infos: Mapping[str, Mapping[str, Any]],
    price_values: Mapping[str, tuple[float, Mapping[str, Any]]],
    market_order_budgets_krw: Mapping[str, float],
    research_day: str,
    priority_symbols: Sequence[str] = (),
    held_symbols: Sequence[str] = (),
    total_limit: int = 40,
    per_market_limit: int = 20,
) -> dict[str, Any]:
    """Build a deterministic, balanced subset that can pass order construction.

    KR candidates must support at least one whole share inside the current
    ticket. US candidates use the project's amount-order branch and therefore
    need only satisfy the KRW/USD minimum ticket. Exact buying power, price,
    orderability, and order book are checked again before any order.
    """

    held = {str(symbol) for symbol in held_symbols}
    priority = {str(symbol) for symbol in priority_symbols}
    limits = {
        "total": max(0, int(total_limit)),
        "per_market": max(0, int(per_market_limit)),
    }
    affordable_by_market: dict[str, list[dict[str, Any]]] = {"KR": [], "US": []}
    rejection_counts: dict[str, int] = {}
    scanned_by_market = {"KR": 0, "US": 0}

    for raw_symbol in candidate_symbols:
        symbol = str(raw_symbol or "").strip()
        info = symbol_infos.get(symbol) or {}
        market = str(info.get("market") or "").upper()
        if not symbol or market not in affordable_by_market:
            rejection_counts["unsupported_market"] = rejection_counts.get("unsupported_market", 0) + 1
            continue
        scanned_by_market[market] += 1
        if symbol in held:
            rejection_counts["already_held"] = rejection_counts.get("already_held", 0) + 1
            continue
        price_record = price_values.get(symbol)
        if not price_record:
            rejection_counts["price_unavailable"] = rejection_counts.get("price_unavailable", 0) + 1
            continue
        price = float(price_record[0])
        order_budget = max(0.0, float(market_order_budgets_krw.get(market, 0.0)))
        ok, order_spec = build_buy_order(settings, symbol, dict(info), price, order_budget)
        if not ok:
            reason = str(order_spec.get("reason") or "order_not_buildable")
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            continue
        affordable_by_market[market].append(
            {
                "symbol": symbol,
                "name": str(info.get("name") or symbol),
                "market": market,
                "price": price,
                "max_order_krw": order_budget,
                "quantity": order_spec.get("quantity"),
                "order_amount_usd": order_spec.get("order_amount"),
                "priority_theme_candidate": symbol in priority,
            }
        )

    for market in affordable_by_market:
        affordable_by_market[market].sort(
            key=lambda item: _candidate_sort_key(research_day, priority, item)
        )

    selected_by_market = {
        market: rows[: limits["per_market"]]
        for market, rows in affordable_by_market.items()
    }
    selected: list[dict[str, Any]] = []
    # Interleave KR/US so a downstream total cap cannot silently erase one
    # currency merely because symbols.csv lists the other market first.
    for index in range(limits["per_market"]):
        for market in ("KR", "US"):
            rows = selected_by_market[market]
            if index < len(rows) and len(selected) < limits["total"]:
                selected.append(rows[index])

    return {
        "event_id": CASH_AFFORDABLE_EVENT_ID,
        "research_day": str(research_day),
        "market_order_budgets_krw": {
            market: float(market_order_budgets_krw.get(market, 0.0)) for market in ("KR", "US")
        },
        "scanned_by_market": scanned_by_market,
        "affordable_counts": {
            market: len(rows) for market, rows in affordable_by_market.items()
        },
        "selected_counts": {
            market: sum(1 for row in selected if row["market"] == market) for market in ("KR", "US")
        },
        "rejection_counts": rejection_counts,
        "selected_candidates": selected,
        "selected_symbols": [row["symbol"] for row in selected],
        "selection_policy": {
            "existing_registry_only": True,
            "whole_share_required_for_kr": True,
            "amount_order_used_for_us": True,
            "priority_then_daily_rotation": True,
            "total_limit": limits["total"],
            "per_market_limit": limits["per_market"],
        },
    }


def build_cash_affordable_question(
    base_question: str,
    *,
    krw_buying_power: float,
    usd_buying_power: float,
    usd_krw_rate: float,
    affordability: Mapping[str, Any],
) -> str:
    budgets = affordability.get("market_order_budgets_krw") or {}
    counts = affordability.get("affordable_counts") or {}
    selected_counts = affordability.get("selected_counts") or {}
    usd_budget = float(budgets.get("US", 0.0)) / max(float(usd_krw_rate), 1.0)
    return (
        f"{str(base_question).strip()} "
        f"현재 주문가능 현금은 KRW {float(krw_buying_power):,.0f}, USD {float(usd_buying_power):,.2f}, "
        f"참조환율은 USD/KRW {float(usd_krw_rate):,.2f}다. "
        f"Python 위험한도를 적용한 종목당 최대 검토금액은 국내 {float(budgets.get('KR', 0.0)):,.0f}원, "
        f"미국 {usd_budget:,.2f}달러 상당이다. "
        f"가격 기준 구매 가능 수는 KR {int(counts.get('KR', 0))}, US {int(counts.get('US', 0))}이며 "
        f"이번 정밀분석 목록은 KR {int(selected_counts.get('KR', 0))}, US {int(selected_counts.get('US', 0))}다. "
        "반드시 입력 candidate_symbols 안에서만 상위 종목과 정확한 심볼을 제시하고, "
        "잔액은 수익 근거가 아니라 주문 가능성 제약으로만 사용하라."
    )
