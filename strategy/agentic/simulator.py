"""A small, isolated continuous-double-auction simulator (reference 05).

This module deliberately has no Toss imports and no path to live execution.
It is useful for testing agent personas, order semantics, and partial fills
before a strategy is evaluated with the application's normal dry-run flow.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence
import itertools


def expected_dividend_stats(outcomes: Sequence[tuple[float, float]]) -> Dict[str, float]:
    """Return ``E[D]`` and ``Var(D)`` for a simulator-only dividend process."""

    if not outcomes:
        raise ValueError("at least one dividend outcome is required")
    probabilities = [float(probability) for _, probability in outcomes]
    if any(probability < 0.0 for probability in probabilities) or abs(sum(probabilities) - 1.0) > 1e-9:
        raise ValueError("dividend probabilities must be non-negative and sum to one")
    mean = sum(float(dividend) * float(probability) for dividend, probability in outcomes)
    variance = sum(float(probability) * (float(dividend) - mean) ** 2 for dividend, probability in outcomes)
    return {"expected_dividend": float(mean), "dividend_variance": float(variance), "dividend_std": float(variance ** 0.5)}


def perpetuity_value(expected_dividend: float, per_round_rate: float) -> float:
    """Simulator formula ``V = E[D] / r``; not a live-stock valuation model."""

    if float(per_round_rate) <= 0.0:
        raise ValueError("per_round_rate must be positive")
    return float(expected_dividend) / float(per_round_rate)


def finite_horizon_value(
    expected_dividend: float,
    per_round_rate: float,
    *,
    current_round: int,
    final_round: int,
    terminal_value: float,
) -> float:
    """``sum E[D]/(1+r)^k + K/(1+r)^n`` for a finite simulated market."""

    if per_round_rate <= 0.0 or final_round < current_round:
        raise ValueError("invalid finite-horizon valuation parameters")
    remaining = int(final_round) - int(current_round) + 1
    dividends = sum(float(expected_dividend) / (1.0 + float(per_round_rate)) ** step for step in range(1, remaining + 1))
    terminal = float(terminal_value) / (1.0 + float(per_round_rate)) ** remaining
    return float(dividends + terminal)


def price_to_value_ratio(price: float, intrinsic_value: float) -> float:
    """Simulator price deviation ``rho = P / V``."""

    if float(intrinsic_value) <= 0.0:
        raise ValueError("intrinsic_value must be positive")
    return float(price) / float(intrinsic_value)


def volume_weighted_average_price(prices: Sequence[float], volumes: Sequence[float]) -> float:
    """``VWAP = sum(P*V) / sum(V)`` for a simulator/research data packet."""

    if len(prices) != len(volumes) or len(prices) == 0:
        raise ValueError("prices and volumes must be non-empty sequences of equal length")
    pairs = [(float(price), float(volume)) for price, volume in zip(prices, volumes)]
    if any(price < 0.0 or volume < 0.0 for price, volume in pairs):
        raise ValueError("prices and volumes must be non-negative")
    total_volume = sum(volume for _, volume in pairs)
    if total_volume <= 0.0:
        raise ValueError("total volume must be positive")
    return float(sum(price * volume for price, volume in pairs) / total_volume)


def order_book_metrics(bids: Sequence[Mapping[str, Any]], asks: Sequence[Mapping[str, Any]]) -> Dict[str, float | None]:
    """Best bid/ask, spread, midpoint, depth and order-book imbalance."""

    def available(rows: Sequence[Mapping[str, Any]]) -> list[tuple[float, float]]:
        result: list[tuple[float, float]] = []
        for row in rows:
            try:
                price = float(row.get("limit_price"))
                quantity = float(row.get("remaining", row.get("quantity", 0.0)))
            except (TypeError, ValueError):
                continue
            if price > 0.0 and quantity > 0.0:
                result.append((price, quantity))
        return result

    bid_rows = available(bids)
    ask_rows = available(asks)
    best_bid = max((price for price, _ in bid_rows), default=None)
    best_ask = min((price for price, _ in ask_rows), default=None)
    bid_depth = sum(quantity for _, quantity in bid_rows)
    ask_depth = sum(quantity for _, quantity in ask_rows)
    total_depth = bid_depth + ask_depth
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    midpoint = (best_bid + best_ask) / 2.0 if best_bid is not None and best_ask is not None else None
    imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0.0 else None
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "midpoint": midpoint,
        "bid_depth": float(bid_depth),
        "ask_depth": float(ask_depth),
        "order_book_imbalance": imbalance,
    }


@dataclass
class SimOrder:
    agent_id: str
    side: str
    quantity: float
    order_type: str = "limit"
    limit_price: Optional[float] = None
    order_id: str = ""
    sequence: int = 0
    remaining: float = 0.0

    def __post_init__(self) -> None:
        self.side = self.side.upper()
        self.order_type = self.order_type.lower()
        self.quantity = float(self.quantity)
        self.remaining = self.quantity if self.remaining == 0.0 else float(self.remaining)
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if self.order_type not in {"limit", "market"}:
            raise ValueError("order_type must be limit or market")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type == "limit" and (self.limit_price is None or float(self.limit_price) <= 0):
            raise ValueError("limit orders require a positive limit_price")


@dataclass(frozen=True)
class SimFill:
    buy_order_id: str
    sell_order_id: str
    price: float
    quantity: float

    def to_dict(self) -> Dict[str, float | str]:
        return asdict(self)


@dataclass
class SimAccount:
    """Cash and inventory used only by :class:`SimulationLedger`."""

    cash: float
    inventory: Dict[str, float] = field(default_factory=dict)
    dividend_cash: float = 0.0
    reserved_cash: float = 0.0
    reserved_inventory: Dict[str, float] = field(default_factory=dict)


class SimulationLedger:
    """Pre-trade validation and settlement for simulator-only accounts.

    A symbol is deliberately supplied by the market/simulation caller rather
    than inferred from a prompt.  This makes cash, inventory, and partial fill
    accounting explicit and keeps this class entirely outside Toss execution.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = str(symbol)
        self.accounts: Dict[str, SimAccount] = {}

    def register(self, agent_id: str, *, cash: float, inventory: float = 0.0) -> None:
        if cash < 0 or inventory < 0:
            raise ValueError("cash and inventory must be non-negative")
        self.accounts[str(agent_id)] = SimAccount(float(cash), {self.symbol: float(inventory)})

    def _account(self, agent_id: str) -> SimAccount:
        if agent_id not in self.accounts:
            raise ValueError(f"simulator account is not registered: {agent_id}")
        return self.accounts[agent_id]

    def reserve(self, order: SimOrder) -> None:
        account = self._account(order.agent_id)
        if order.order_type == "market":
            raise ValueError("ledger-backed market orders require an explicit max-notional model")
        if order.side == "BUY":
            needed = float(order.limit_price) * order.remaining
            if account.cash - account.reserved_cash + 1e-12 < needed:
                raise ValueError("insufficient simulator cash")
            account.reserved_cash += needed
            return
        available = account.inventory.get(self.symbol, 0.0) - account.reserved_inventory.get(self.symbol, 0.0)
        if available + 1e-12 < order.remaining:
            raise ValueError("insufficient simulator inventory")
        account.reserved_inventory[self.symbol] = account.reserved_inventory.get(self.symbol, 0.0) + order.remaining

    def settle(self, buy: SimOrder, sell: SimOrder, *, price: float, quantity: float) -> None:
        buyer = self._account(buy.agent_id)
        seller = self._account(sell.agent_id)
        # The buyer's limit-price reservation is decreased at the reserved
        # value; paying the lower resting ask automatically releases price
        # improvement back to available cash.
        buyer.reserved_cash = max(0.0, buyer.reserved_cash - float(buy.limit_price) * quantity)
        buyer.cash -= price * quantity
        buyer.inventory[self.symbol] = buyer.inventory.get(self.symbol, 0.0) + quantity
        seller.reserved_inventory[self.symbol] = max(
            0.0, seller.reserved_inventory.get(self.symbol, 0.0) - quantity
        )
        seller.inventory[self.symbol] = seller.inventory.get(self.symbol, 0.0) - quantity
        seller.cash += price * quantity

    def release(self, order: SimOrder) -> None:
        account = self._account(order.agent_id)
        if order.order_type != "limit":
            return
        if order.side == "BUY":
            account.reserved_cash = max(0.0, account.reserved_cash - float(order.limit_price) * order.remaining)
        else:
            account.reserved_inventory[self.symbol] = max(
                0.0, account.reserved_inventory.get(self.symbol, 0.0) - order.remaining
            )

    def credit_dividend(self, dividend_per_share: float) -> None:
        """Credit dividends to a non-tradeable simulator sub-account.

        This mirrors the paper's experimental separation of main cash and
        dividend cash.  It intentionally has no connection to a real broker
        account, where dividend reinvestment rules may differ.
        """
        amount = float(dividend_per_share)
        if amount < 0.0:
            raise ValueError("dividend_per_share must be non-negative")
        for account in self.accounts.values():
            account.dividend_cash += account.inventory.get(self.symbol, 0.0) * amount

    def wealth(self, agent_id: str, mark_price: float) -> float:
        account = self._account(agent_id)
        if float(mark_price) < 0.0:
            raise ValueError("mark_price must be non-negative")
        return float(account.cash + account.dividend_cash + account.inventory.get(self.symbol, 0.0) * float(mark_price))

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        return {agent_id: asdict(account) for agent_id, account in self.accounts.items()}


class ContinuousDoubleAuction:
    """Price-time-priority limit book with partial fills and cancellation."""

    def __init__(self, *, ledger: Optional[SimulationLedger] = None) -> None:
        self._sequence = itertools.count(1)
        self._bids: List[SimOrder] = []
        self._asks: List[SimOrder] = []
        self.fills: List[SimFill] = []
        self.ledger = ledger

    @staticmethod
    def _buy_priority(order: SimOrder) -> tuple[float, int]:
        return (-float(order.limit_price or float("inf")), order.sequence)

    @staticmethod
    def _sell_priority(order: SimOrder) -> tuple[float, int]:
        return (float(order.limit_price or 0.0), order.sequence)

    def _sort_books(self) -> None:
        self._bids.sort(key=self._buy_priority)
        self._asks.sort(key=self._sell_priority)

    def submit(self, order: SimOrder) -> List[SimFill]:
        order.sequence = next(self._sequence)
        order.order_id = order.order_id or f"sim-{order.sequence}"
        before = len(self.fills)
        if self.ledger is not None:
            self.ledger.reserve(order)

        if order.side == "BUY":
            self._match_buy(order)
            if order.remaining > 0 and order.order_type == "limit":
                self._bids.append(order)
        else:
            self._match_sell(order)
            if order.remaining > 0 and order.order_type == "limit":
                self._asks.append(order)

        self._sort_books()
        if self.ledger is not None and order.remaining <= 0:
            self.ledger.release(order)
        return self.fills[before:]

    def _match_buy(self, buy: SimOrder) -> None:
        self._sort_books()
        while buy.remaining > 0 and self._asks:
            sell = self._asks[0]
            if buy.order_type == "limit" and float(buy.limit_price) < float(sell.limit_price):
                break
            qty = min(buy.remaining, sell.remaining)
            buy.remaining -= qty
            sell.remaining -= qty
            self.fills.append(SimFill(buy.order_id, sell.order_id, float(sell.limit_price), qty))
            if self.ledger is not None:
                self.ledger.settle(buy, sell, price=float(sell.limit_price), quantity=qty)
            if sell.remaining <= 0:
                if self.ledger is not None:
                    self.ledger.release(sell)
                self._asks.pop(0)

    def _match_sell(self, sell: SimOrder) -> None:
        self._sort_books()
        while sell.remaining > 0 and self._bids:
            buy = self._bids[0]
            if sell.order_type == "limit" and float(sell.limit_price) > float(buy.limit_price):
                break
            qty = min(sell.remaining, buy.remaining)
            sell.remaining -= qty
            buy.remaining -= qty
            self.fills.append(SimFill(buy.order_id, sell.order_id, float(buy.limit_price), qty))
            if self.ledger is not None:
                self.ledger.settle(buy, sell, price=float(buy.limit_price), quantity=qty)
            if buy.remaining <= 0:
                if self.ledger is not None:
                    self.ledger.release(buy)
                self._bids.pop(0)

    def cancel(self, order_id: str) -> bool:
        for book in (self._bids, self._asks):
            for index, order in enumerate(book):
                if order.order_id == order_id:
                    if self.ledger is not None:
                        self.ledger.release(order)
                    book.pop(index)
                    return True
        return False

    def snapshot(self) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {
            "bids": [asdict(order) for order in self._bids],
            "asks": [asdict(order) for order in self._asks],
            "fills": [fill.to_dict() for fill in self.fills],
            "ledger": self.ledger.snapshot() if self.ledger is not None else None,
        }
        snapshot["market_quality"] = order_book_metrics(snapshot["bids"], snapshot["asks"])
        return snapshot
