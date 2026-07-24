import math
from typing import Dict, Any, Tuple


def infer_market(symbol: str, symbol_info: Dict[str, Any]) -> str:
    market = str(symbol_info.get("market", "") or "").upper()
    if market in {"KR", "US"}:
        return market
    # A declared local-exchange market must never fall through to the US
    # amount-order branch.  The current broker integration only implements
    # KR/US execution; return a sentinel so callers can fail closed.
    if market:
        return "UNSUPPORTED"
    # 6자리 숫자는 국내 주식 코드로 간주
    if str(symbol).isdigit() and len(str(symbol)) == 6:
        return "KR"
    return "US"


def build_buy_order(settings, symbol: str, symbol_info: Dict[str, Any], price: float, order_value_krw: float) -> Tuple[bool, Dict[str, Any]]:
    """
    KR: 원화 금액을 현재가로 나눠 quantity 주문.
    US: 원화 금액을 USD로 환산해서 orderAmount 주문.

    주의:
    - 실제 Toss 주문 필드/금액 단위는 공식 문서와 실계좌 테스트로 확인 필요.
    - dry-run 기본값 유지.
    """
    market = infer_market(symbol, symbol_info)

    if market == "UNSUPPORTED":
        return False, {
            "reason": "unsupported_market_for_current_toss_execution",
            "market": str(symbol_info.get("market", "") or "").upper(),
            "symbol": symbol,
        }

    if price <= 0 or order_value_krw <= 0:
        return False, {"reason": "invalid_price_or_order_value"}

    if order_value_krw < settings.min_order_krw:
        return False, {"reason": "below_min_order_krw", "order_value_krw": order_value_krw, "min_order_krw": settings.min_order_krw}

    if market == "KR":
        quantity = math.floor(order_value_krw / price)
        if quantity < 1:
            return False, {
                "reason": "cannot_afford_one_share",
                "market": market,
                "price": price,
                "order_value_krw": order_value_krw,
            }

        return True, {
            "market": market,
            "symbol": symbol,
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": str(int(quantity)),
            "order_amount": None,
            "notional_krw": float(quantity * price),
        }

    # US amount order. order_value_krw -> USD.
    fx = max(float(settings.usd_krw_fx_rate), 1.0)
    order_amount_usd = round(order_value_krw / fx, 2)

    if order_amount_usd < settings.min_order_usd:
        return False, {
            "reason": "below_min_order_usd",
            "market": market,
            "order_amount_usd": order_amount_usd,
            "min_order_usd": settings.min_order_usd,
        }

    return True, {
        "market": market,
        "symbol": symbol,
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": None,
        "order_amount": str(order_amount_usd),
        "notional_krw": float(order_value_krw),
    }


def build_sell_order(settings, symbol: str, symbol_info: Dict[str, Any], price: float, position: Dict[str, Any], sell_fraction: float) -> Tuple[bool, Dict[str, Any]]:
    market = infer_market(symbol, symbol_info)
    if market == "UNSUPPORTED":
        return False, {
            "reason": "unsupported_market_for_current_toss_execution",
            "market": str(symbol_info.get("market", "") or "").upper(),
            "symbol": symbol,
        }
    qty = float(position.get("_quantity", 0.0) or 0.0)
    if qty <= 0:
        return False, {"reason": "no_position_quantity"}

    sell_fraction = max(0.0, min(1.0, float(sell_fraction)))
    raw_qty = qty * sell_fraction

    if market == "KR":
        quantity = math.floor(raw_qty)
        if quantity < 1:
            return False, {"reason": "sell_quantity_below_one_share", "raw_qty": raw_qty}
        return True, {
            "market": market,
            "symbol": symbol,
            "side": "SELL",
            "order_type": "MARKET",
            "quantity": str(int(quantity)),
            "order_amount": None,
            "notional_krw": float(quantity * price),
        }

    # 미국주식 매도는 안전하게 quantity 기반으로 둔다.
    # 실제 fractional sell 허용 여부는 Toss 문서/계좌 테스트 필요.
    quantity = round(raw_qty, 6)
    if quantity <= 0:
        return False, {"reason": "sell_quantity_below_zero", "raw_qty": raw_qty}
    return True, {
        "market": market,
        "symbol": symbol,
        "side": "SELL",
        "order_type": "MARKET",
        "quantity": str(quantity),
        "order_amount": None,
        "notional_krw": float(quantity * price * settings.usd_krw_fx_rate),
    }
