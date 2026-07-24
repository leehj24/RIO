import datetime as dt
from typing import Any, Dict, List, Optional


class LiveDataError(RuntimeError):
    pass


def is_live(settings) -> bool:
    return not bool(settings.dry_run)


def strict_live(settings) -> bool:
    return is_live(settings) and bool(getattr(settings, "require_real_data_for_live", True))


def _pick(d: Dict[str, Any], *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _to_float(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


def _to_int(v, default=None):
    try:
        return int(float(v))
    except Exception:
        return default


def unwrap_result(response: Dict[str, Any]) -> Any:
    if isinstance(response, dict) and "result" in response:
        return response["result"]
    return response


def require_live_value(settings, value, name: str):
    if strict_live(settings) and value in (None, "", [], {}, 0, 0.0):
        raise LiveDataError(f"[LIVE BLOCKED] required real data missing: {name}")
    return value


def parse_buying_power(response: Dict[str, Any]) -> float:
    result = unwrap_result(response)
    if isinstance(result, dict):
        v = _pick(
            result,
            "orderableAmount",
            "availableAmount",
            "cashBuyingPower",
            "cash",
            "buyingPower",
            "withdrawableAmount",
            "krwOrderableAmount",
            "orderableCash",
            default=None,
        )
        parsed = _to_float(v, None)
        if parsed is not None:
            return parsed

    # 일부 API가 [{...}] 구조로 줄 가능성 대비
    if isinstance(result, list) and result:
        for item in result:
            if isinstance(item, dict):
                try:
                    return parse_buying_power({"result": item})
                except LiveDataError:
                    continue

    # 키 이름이 변형된 경우(예: krwCashBuyingPower, usdOrderableAmount 등)
    # 이름에 매수가능/주문가능 의미가 들어간 숫자 필드를 마지막으로 탐색한다.
    if isinstance(result, dict):
        for key, value in result.items():
            lowered = str(key).lower()
            if ("buyingpower" in lowered or "orderable" in lowered or "available" in lowered) and "currency" not in lowered:
                parsed = _to_float(value, None)
                if parsed is not None:
                    return parsed

    raise LiveDataError("cannot parse buying power from Toss response")


def parse_exchange_rate(response: Dict[str, Any]) -> float:
    """Return a positive KRW-per-USD reference rate from Toss's envelope."""

    result = unwrap_result(response)
    if isinstance(result, dict):
        value = _pick(result, "rate", "midRate", "exchangeRate", default=None)
        parsed = _to_float(value, None)
        if parsed is not None and parsed > 0:
            return parsed
    raise LiveDataError("cannot parse exchange rate from Toss response")


def parse_sellable_quantity(response: Dict[str, Any]) -> float:
    result = unwrap_result(response)
    if isinstance(result, dict):
        value = _pick(
            result,
            "sellableQuantity",
            "sellableQty",
            "availableQuantity",
            "availableQty",
            "orderableQuantity",
            default=None,
        )
        parsed = _to_float(value, None)
        if parsed is not None and parsed >= 0:
            return parsed
    raise LiveDataError("cannot parse sellable quantity from Toss response")


def parse_holdings_risk_snapshot(response: Dict[str, Any], usd_krw_fx_rate: float) -> Dict[str, float]:
    """Parse the official holdings summary used to reconcile live risk state.

    ``GET /holdings`` returns position items plus a KRW/USD account summary.
    The bot deliberately keeps this separate from the per-symbol parser: the
    former is used for portfolio peak/drawdown checks while the latter drives
    per-symbol sizing.
    """

    result = unwrap_result(response)
    if not isinstance(result, dict):
        raise LiveDataError("holdings response is not object-like")

    market_value = result.get("marketValue")
    if not isinstance(market_value, dict):
        raise LiveDataError("holdings marketValue summary missing")

    # ``amountAfterCost`` is the conservative liquidation-value estimate when
    # supplied by the broker; otherwise use the raw market amount.
    amount = market_value.get("amountAfterCost")
    if not isinstance(amount, dict):
        amount = market_value.get("amount")
    if not isinstance(amount, dict):
        raise LiveDataError("holdings marketValue amount summary missing")

    krw_value = _to_float(_pick(amount, "krw", "KRW", default=0), None)
    usd_value = _to_float(_pick(amount, "usd", "USD", default=0), None)
    if krw_value is None or usd_value is None:
        raise LiveDataError("cannot parse holdings market value currencies")

    daily = result.get("dailyProfitLoss") if isinstance(result.get("dailyProfitLoss"), dict) else {}
    daily_rate = _to_float(_pick(daily, "rate", "dailyRate", default=0), None)
    if daily_rate is None:
        raise LiveDataError("cannot parse holdings daily profit/loss rate")

    return {
        "market_value_krw": max(0.0, float(krw_value)) + max(0.0, float(usd_value)) * max(1.0, float(usd_krw_fx_rate)),
        "market_value_krw_native": max(0.0, float(krw_value)),
        "market_value_usd": max(0.0, float(usd_value)),
        "daily_pnl_pct": float(daily_rate),
    }


def parse_current_price(response: Dict[str, Any], symbol: str | None = None) -> float:
    result = unwrap_result(response)

    def parse_one(item):
        if not isinstance(item, dict):
            return None
        if symbol:
            item_symbol = _pick(item, "symbol", "stockCode", "ticker", "code", "securityCode", default=None)
            if item_symbol and str(item_symbol) != str(symbol):
                return None
        v = _pick(item, "price", "currentPrice", "lastPrice", "tradePrice", "closePrice", default=None)
        return _to_float(v, None)

    if isinstance(result, list):
        for item in result:
            parsed = parse_one(item)
            if parsed and parsed > 0:
                return parsed

    if isinstance(result, dict):
        parsed = parse_one(result)
        if parsed and parsed > 0:
            return parsed

    raise LiveDataError(f"cannot parse current price for {symbol}")


def _parse_orderbook_timestamp(raw_timestamp: Any) -> dt.datetime:
    """Parse the broker timestamp without silently treating an unknown time as fresh."""

    if raw_timestamp in (None, ""):
        raise LiveDataError("orderbook timestamp missing")
    try:
        parsed = dt.datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LiveDataError("invalid orderbook timestamp") from exc
    if parsed.tzinfo is None:
        raise LiveDataError("orderbook timestamp has no timezone")
    return parsed.astimezone(dt.timezone.utc)


def parse_orderbook(response: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a Toss order book into a safe, side-aware snapshot.

    The API examples do not promise that the arrays are ordered best-first.
    We therefore derive the best ask/bid from every valid level rather than
    trusting index zero.  Empty, crossed, or untimestamped books are unsafe
    for an unattended market order and fail closed.
    """

    result = unwrap_result(response)
    if not isinstance(result, dict):
        raise LiveDataError("orderbook response is not object-like")

    timestamp_utc = _parse_orderbook_timestamp(_pick(result, "timestamp", "time", "updatedAt", default=None))
    currency = str(_pick(result, "currency", "currencyCode", default="") or "").upper()

    def normalise_levels(raw_levels: Any, side: str) -> List[Dict[str, float]]:
        if not isinstance(raw_levels, list):
            raise LiveDataError(f"orderbook {side} levels missing")
        levels: List[Dict[str, float]] = []
        for level in raw_levels:
            if not isinstance(level, dict):
                continue
            price = _to_float(_pick(level, "price", "orderPrice", default=None), None)
            volume = _to_float(_pick(level, "volume", "quantity", "qty", "remainingQuantity", default=None), None)
            if price is None or volume is None or price <= 0 or volume <= 0:
                continue
            levels.append({"price": float(price), "volume": float(volume)})
        if not levels:
            raise LiveDataError(f"orderbook {side} has no positive price/volume levels")
        return levels

    asks = sorted(normalise_levels(result.get("asks"), "asks"), key=lambda row: row["price"])
    bids = sorted(normalise_levels(result.get("bids"), "bids"), key=lambda row: row["price"], reverse=True)
    best_ask = asks[0]
    best_bid = bids[0]
    if best_ask["price"] <= best_bid["price"]:
        raise LiveDataError("orderbook is crossed or locked")

    mid_price = (best_ask["price"] + best_bid["price"]) / 2.0
    spread_bps = (best_ask["price"] - best_bid["price"]) / mid_price * 10_000.0
    return {
        "timestamp_utc": timestamp_utc,
        "timestamp": timestamp_utc.isoformat(),
        "currency": currency,
        "asks": asks,
        "bids": bids,
        "best_ask": best_ask["price"],
        "best_ask_volume": best_ask["volume"],
        "best_bid": best_bid["price"],
        "best_bid_volume": best_bid["volume"],
        "mid_price": mid_price,
        "spread_bps": spread_bps,
    }


def evaluate_market_orderbook(
    orderbook: Dict[str, Any],
    *,
    side: str,
    quantity: Any = None,
    order_amount: Any = None,
    max_spread_bps: float,
    max_book_impact_bps: float,
    max_age_seconds: float,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Estimate market-order execution against the fresh visible book.

    A ``quantity`` order consumes shares from the correct side of the book.
    A US ``orderAmount`` buy consumes notional rather than assuming that the
    first level can satisfy it.  The endpoint exposes only visible depth, so
    a shortage is blocked instead of assuming hidden liquidity.
    """

    normalized_side = str(side or "").upper()
    if normalized_side not in {"BUY", "SELL"}:
        return {"ok": False, "reason": "invalid_orderbook_side", "side": normalized_side}

    if quantity not in (None, "") and order_amount not in (None, ""):
        return {"ok": False, "reason": "ambiguous_quantity_and_order_amount", "side": normalized_side}
    if quantity in (None, "") and order_amount in (None, ""):
        return {"ok": False, "reason": "missing_quantity_or_order_amount", "side": normalized_side}

    current_time = now or dt.datetime.now(dt.timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=dt.timezone.utc)
    else:
        current_time = current_time.astimezone(dt.timezone.utc)
    timestamp_utc = orderbook.get("timestamp_utc")
    if not isinstance(timestamp_utc, dt.datetime) or timestamp_utc.tzinfo is None:
        return {"ok": False, "reason": "orderbook_timestamp_unavailable", "side": normalized_side}
    age_seconds = (current_time - timestamp_utc.astimezone(dt.timezone.utc)).total_seconds()

    summary: Dict[str, Any] = {
        "ok": False,
        "side": normalized_side,
        "currency": orderbook.get("currency"),
        "timestamp": orderbook.get("timestamp"),
        "age_seconds": round(age_seconds, 3),
        "best_ask": orderbook.get("best_ask"),
        "best_ask_volume": orderbook.get("best_ask_volume"),
        "best_bid": orderbook.get("best_bid"),
        "best_bid_volume": orderbook.get("best_bid_volume"),
        "spread_bps": round(float(orderbook.get("spread_bps", 0.0) or 0.0), 3),
    }
    if max_age_seconds < 0 or max_spread_bps < 0 or max_book_impact_bps < 0:
        summary["reason"] = "invalid_orderbook_guard_configuration"
        return summary
    # Allow a few seconds of harmless broker/client clock skew, but never use
    # a materially future timestamp as a signal that the book is fresh.
    if age_seconds < -5.0:
        summary["reason"] = "orderbook_clock_skew"
        return summary
    if age_seconds > float(max_age_seconds):
        summary["reason"] = "orderbook_stale"
        return summary
    if float(orderbook.get("spread_bps", 0.0) or 0.0) > float(max_spread_bps):
        summary["reason"] = "spread_exceeds_limit"
        summary["max_spread_bps"] = float(max_spread_bps)
        return summary

    levels = orderbook.get("asks") if normalized_side == "BUY" else orderbook.get("bids")
    if not isinstance(levels, list) or not levels:
        summary["reason"] = "missing_executable_book_side"
        return summary

    requested_quantity = _to_float(quantity, None) if quantity not in (None, "") else None
    requested_amount = _to_float(order_amount, None) if order_amount not in (None, "") else None
    if requested_quantity is not None and requested_quantity <= 0:
        summary["reason"] = "invalid_orderbook_quantity"
        return summary
    if requested_amount is not None and requested_amount <= 0:
        summary["reason"] = "invalid_orderbook_amount"
        return summary

    available_quantity = sum(float(level["volume"]) for level in levels)
    available_amount = sum(float(level["price"]) * float(level["volume"]) for level in levels)
    summary["visible_quantity"] = available_quantity
    summary["visible_notional"] = available_amount

    filled_quantity = 0.0
    filled_notional = 0.0
    if requested_quantity is not None:
        summary["requested_quantity"] = requested_quantity
        remaining_quantity = requested_quantity
        for level in levels:
            take = min(remaining_quantity, float(level["volume"]))
            filled_quantity += take
            filled_notional += take * float(level["price"])
            remaining_quantity -= take
            if remaining_quantity <= 1e-9:
                break
        if remaining_quantity > 1e-9:
            summary["reason"] = "insufficient_visible_orderbook_depth"
            summary["unfilled_quantity"] = remaining_quantity
            return summary
    else:
        summary["requested_order_amount"] = requested_amount
        remaining_amount = float(requested_amount)
        for level in levels:
            available_at_level = float(level["price"]) * float(level["volume"])
            take_amount = min(remaining_amount, available_at_level)
            filled_notional += take_amount
            filled_quantity += take_amount / float(level["price"])
            remaining_amount -= take_amount
            if remaining_amount <= 1e-9:
                break
        if remaining_amount > 1e-9:
            summary["reason"] = "insufficient_visible_orderbook_depth"
            summary["unfilled_order_amount"] = remaining_amount
            return summary

    if filled_quantity <= 0 or filled_notional <= 0:
        summary["reason"] = "zero_estimated_orderbook_fill"
        return summary
    expected_vwap = filled_notional / filled_quantity
    best_price = float(orderbook["best_ask"] if normalized_side == "BUY" else orderbook["best_bid"])
    if normalized_side == "BUY":
        impact_bps = (expected_vwap / best_price - 1.0) * 10_000.0
    else:
        impact_bps = (best_price / expected_vwap - 1.0) * 10_000.0
    summary.update(
        {
            "estimated_fill_quantity": filled_quantity,
            "estimated_vwap": expected_vwap,
            "estimated_notional": filled_notional,
            "book_impact_bps": round(impact_bps, 3),
            "max_book_impact_bps": float(max_book_impact_bps),
        }
    )
    if impact_bps > float(max_book_impact_bps):
        summary["reason"] = "book_impact_exceeds_limit"
        return summary

    summary["ok"] = True
    summary["reason"] = "orderbook_ok"
    return summary


def parse_positions(response: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result = unwrap_result(response)

    if isinstance(result, dict):
        for key in ["positions", "items", "stocks", "balances", "holdings", "assets"]:
            if isinstance(result.get(key), list):
                result = result[key]
                break

    if not isinstance(result, list):
        raise LiveDataError("positions response is not list-like")

    out: Dict[str, Dict[str, Any]] = {}
    for pos in result:
        if not isinstance(pos, dict):
            continue
        symbol = _pick(pos, "symbol", "stockCode", "ticker", "code", "securityCode", default=None)
        if not symbol:
            continue

        quantity = _pick(pos, "quantity", "qty", "holdingQuantity", "balanceQuantity", default=0)
        sellable = _pick(pos, "sellableQuantity", "sellableQty", "availableQuantity", "availableQty", "orderableQuantity", default=quantity)
        avg_price = _pick(pos, "averagePrice", "avgPrice", "purchasePrice", "averagePurchasePrice", default=0)
        current_price = _pick(pos, "currentPrice", "lastPrice", "price", "marketPrice", default=0)
        market_value = _pick(pos, "evaluationAmount", "evalAmount", "marketValue", default=0)

        # Official Toss holdings responses nest the evaluated amount under
        # ``marketValue.amount``.  Keeping that dict as-is makes the value
        # silently parse to zero, which in turn would make a live portfolio
        # look empty to the exposure and per-symbol cap checks.
        if isinstance(market_value, dict):
            market_value = _pick(
                market_value,
                "amountAfterCost",
                "amount",
                "marketValue",
                "value",
                default=0,
            )

        q = _to_float(quantity, 0.0) or 0.0
        sq = _to_float(sellable, 0.0) or 0.0

        out[str(symbol)] = {
            **pos,
            "_symbol": str(symbol),
            "_quantity": q,
            "_sellable_quantity": sq,
            "_avg_price": _to_float(avg_price, 0.0) or 0.0,
            "_current_price": _to_float(current_price, 0.0) or 0.0,
            "_market_value": _to_float(market_value, 0.0) or 0.0,
        }

    return out


def validate_sellable_quantity(position: Dict[str, Any] | None, requested_qty: float, symbol: str):
    if not position:
        raise LiveDataError(f"[LIVE BLOCKED] no real position for sell: {symbol}")

    sellable = _to_float(position.get("_sellable_quantity"), None)
    if sellable is None:
        raise LiveDataError(f"[LIVE BLOCKED] sellableQuantity missing for {symbol}")

    if sellable <= 0:
        raise LiveDataError(f"[LIVE BLOCKED] sellableQuantity is zero for {symbol}")

    if requested_qty > sellable + 1e-9:
        raise LiveDataError(
            f"[LIVE BLOCKED] requested sell qty {requested_qty} > sellable qty {sellable} for {symbol}"
        )


def parse_order_status(response: Dict[str, Any]) -> Dict[str, Any]:
    result = unwrap_result(response)
    if isinstance(result, list) and result:
        result = result[0]
    if not isinstance(result, dict):
        raise LiveDataError("order status response is not dict-like")

    status = _pick(result, "status", "orderStatus", "state", "orderState", default="")
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    filled_qty = _pick(
        result,
        "filledQuantity",
        "executedQuantity",
        "filledQty",
        "executedQty",
        default=_pick(execution, "filledQuantity", "executedQuantity", "filledQty", "executedQty", default=0),
    )
    remaining_qty = _pick(
        result,
        "remainingQuantity",
        "unfilledQuantity",
        "remainingQty",
        "unfilledQty",
        default=_pick(execution, "remainingQuantity", "unfilledQuantity", "remainingQty", "unfilledQty", default=0),
    )
    avg_price = _pick(
        result,
        "averageFilledPrice",
        "executedPrice",
        "avgExecutionPrice",
        "averagePrice",
        default=_pick(execution, "averageFilledPrice", "executedPrice", "avgExecutionPrice", "averagePrice", default=0),
    )

    normalized_status = str(status).strip().upper()
    rejected = any(token in normalized_status for token in ("REJECT", "FAIL", "ERROR"))
    canceled = any(token in normalized_status for token in ("CANCEL", "VOID"))
    filled = any(token in normalized_status for token in ("FILLED", "EXECUTED", "COMPLETED"))

    return {
        **result,
        "_status": normalized_status,
        "_filled_quantity": _to_float(filled_qty, 0.0) or 0.0,
        "_remaining_quantity": _to_float(remaining_qty, 0.0) or 0.0,
        "_avg_filled_price": _to_float(avg_price, 0.0) or 0.0,
        "_is_rejected": rejected,
        "_is_canceled": canceled,
        "_is_filled": filled,
    }


def validate_order_response(settings, response: Dict[str, Any]) -> Dict[str, Any]:
    if not strict_live(settings):
        return response

    if not isinstance(response, dict):
        raise LiveDataError("[LIVE BLOCKED] order response is not dict")

    if response.get("dry_run"):
        raise LiveDataError("[LIVE BLOCKED] dry_run response received while live mode")

    result = unwrap_result(response)
    if not isinstance(result, dict):
        raise LiveDataError("[LIVE BLOCKED] order result missing")

    order_id = _pick(result, "orderId", "id", "orderNo", "orderNumber", default=None)
    if not order_id:
        raise LiveDataError("[LIVE BLOCKED] orderId missing in live order response")

    return response


def parse_stock_tradability(response: Dict[str, Any], symbol: str | None = None) -> Dict[str, Any]:
    """
    종목 상태/주문 가능 여부 파싱.
    Toss 응답 필드명은 문서 버전에 따라 다를 수 있으므로 보수적으로 처리한다.

    아래 값 중 하나라도 거래불가를 암시하면 tradable=False.
    """
    result = unwrap_result(response)
    if isinstance(result, list) and result:
        target = None
        for item in result:
            if not isinstance(item, dict):
                continue
            item_symbol = _pick(item, "symbol", "stockCode", "ticker", "code", "securityCode", default=None)
            if symbol is None or str(item_symbol) == str(symbol):
                target = item
                break
        if symbol is not None and target is None:
            return {"tradable": None, "reason": "requested_symbol_not_returned", "raw": response}
        result = target or result[0]

    if not isinstance(result, dict):
        return {"tradable": False, "reason": "stock_info_not_dict", "raw": response}

    status_text = str(_pick(result, "status", "tradingStatus", "marketStatus", "orderStatus", "state", default="")).upper()
    halt = _pick(result, "halted", "isHalted", "tradingHalt", "suspended", "isSuspended", default=False)
    delisted = _pick(result, "delisted", "isDelisted", default=False)
    orderable = _pick(result, "orderable", "isOrderable", "tradeable", "tradable", default=None)
    korean_detail = result.get("koreanMarketDetail") if isinstance(result.get("koreanMarketDetail"), dict) else {}
    korean_halt = any(
        bool(korean_detail.get(key))
        for key in ("liquidationTrading", "krxTradingSuspended", "nxtTradingSuspended")
    )

    bad_words = ["HALT", "SUSPEND", "DELIST", "STOP", "CLOSED", "UNTRADABLE", "NOT_ORDERABLE"]
    text_block = " ".join(
        [status_text, str(halt).upper(), str(delisted).upper(), str(orderable).upper(), str(korean_halt).upper()]
    )

    if any(w in text_block for w in bad_words):
        return {"tradable": False, "reason": "status_indicates_not_tradable", "raw": result}

    if isinstance(orderable, bool) and not orderable:
        return {"tradable": False, "reason": "orderable_false", "raw": result}

    if korean_halt:
        return {"tradable": False, "reason": "korean_market_detail_blocks_order", "raw": result}

    # The official stock-info endpoint exposes an ACTIVE listing state.  A
    # known non-active listing is not a safe target for an unattended order.
    if status_text and status_text not in {"ACTIVE", "OPEN", "TRADING"}:
        return {"tradable": False, "reason": "listing_status_not_active", "raw": result}

    if not status_text and orderable is None:
        return {"tradable": None, "reason": "tradability_not_confirmed", "raw": result}

    # 정보가 없으면 live strict에서는 별도 market clock과 order rejection에 맡긴다.
    return {"tradable": True, "reason": "no_blocking_status_found", "raw": result}


def is_market_open_from_clock(response: Dict[str, Any]) -> Dict[str, Any]:
    result = unwrap_result(response)
    if isinstance(result, dict):
        v = _pick(result, "isOpen", "open", "marketOpen", "isMarketOpen", default=None)
        if isinstance(v, bool):
            return {"is_open": v, "raw": result}
        text = str(_pick(result, "status", "marketStatus", "state", default="")).upper()
        if text:
            if any(x in text for x in ["OPEN", "TRADING", "REGULAR"]):
                return {"is_open": True, "raw": result}
            if any(x in text for x in ["CLOSE", "CLOSED", "BEFORE", "AFTER", "HOLIDAY"]):
                return {"is_open": False, "raw": result}
        # Official Toss market-calendar responses carry session start/end
        # timestamps rather than a boolean.  Use only the regular session for
        # unattended market/amount orders: US amount orders are explicitly
        # regular-hours-only, and treating every pre/after session as tradable
        # would make the broker reject otherwise valid signals.
        today = result.get("today")
        if isinstance(today, dict):
            integrated = today.get("integrated")
            regular = integrated.get("regularMarket") if isinstance(integrated, dict) else today.get("regularMarket")
            if isinstance(regular, dict):
                start_raw = regular.get("startTime")
                end_raw = regular.get("endTime")
                try:
                    start = dt.datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
                    end = dt.datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                    now = dt.datetime.now(start.tzinfo or dt.timezone.utc)
                    return {
                        "is_open": start <= now <= end,
                        "session": "regularMarket",
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                        "raw": result,
                    }
                except (TypeError, ValueError):
                    return {"is_open": None, "reason": "invalid_market_calendar_timestamps", "raw": result}
    return {"is_open": None, "raw": response}
