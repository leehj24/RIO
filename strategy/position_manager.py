from typing import Dict, Any, Optional

from strategy.live_data_guard import parse_positions, LiveDataError, strict_live


def _pick(d: Dict[str, Any], *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _to_float(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def extract_positions_map(response: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Toss positions 응답을 실제 보유수량/매도가능수량/평단으로 파싱한다.
    """
    return parse_positions(response)


def dry_run_positions_from_state(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = state.setdefault("sim_positions", {})
    out = {}
    for symbol, pos in raw.items():
        quantity = _to_float(pos.get("quantity", 0))
        avg_price = _to_float(pos.get("avg_price", 0))
        out[symbol] = {
            "_symbol": symbol,
            "_quantity": quantity,
            "_sellable_quantity": float(pos.get("sellable_quantity", quantity)),
            "_avg_price": avg_price,
            "_current_price": _to_float(pos.get("current_price", avg_price)),
            "_market_value": quantity * avg_price,
            **pos,
        }
    return out


def get_positions_map(toss, settings, state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if settings.dry_run:
        return dry_run_positions_from_state(state)

    try:
        return extract_positions_map(toss.positions(account_seq=settings.toss_account_seq))
    except Exception as exc:
        if strict_live(settings):
            # Treating a failed broker holdings request as an empty portfolio
            # can create duplicate buys or an invalid sell.  Live execution
            # must fail closed until the account can be reconciled again.
            raise LiveDataError(f"[LIVE BLOCKED] cannot reconcile broker positions: {exc}") from exc
        return {}


def apply_dry_run_fill(
    state: Dict[str, Any],
    *,
    symbol: str,
    side: str,
    price: float,
    quantity: float,
    order_value: float,
) -> None:
    """
    dry-run에서만 단순 simulated position을 갱신한다.
    """
    positions = state.setdefault("sim_positions", {})
    pos = positions.setdefault(symbol, {"quantity": 0.0, "avg_price": 0.0, "realized_pnl": 0.0})

    old_qty = _to_float(pos.get("quantity", 0.0))
    old_avg = _to_float(pos.get("avg_price", 0.0))

    if side.upper() == "BUY":
        new_qty = old_qty + float(quantity)
        if new_qty <= 0:
            pos["quantity"] = 0.0
            pos["avg_price"] = 0.0
        else:
            pos["avg_price"] = ((old_qty * old_avg) + float(order_value)) / new_qty
            pos["quantity"] = new_qty
        pos["current_price"] = float(price)
        return

    if side.upper() == "SELL":
        sell_qty = min(old_qty, float(quantity))
        pnl = (float(price) - old_avg) * sell_qty
        pos["quantity"] = max(0.0, old_qty - sell_qty)
        pos["realized_pnl"] = _to_float(pos.get("realized_pnl", 0.0)) + pnl
        pos["current_price"] = float(price)
        if pos["quantity"] <= 0:
            positions.pop(symbol, None)
