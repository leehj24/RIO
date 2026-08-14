"""Verify the configured KR/US execution candidates against Toss market APIs.

Toss exposes lookups for known symbols, not an endpoint that downloads the
entire broker master.  This utility therefore verifies the project-owned
candidate list in batches and writes a separate audit manifest.  It never
enables a symbol or creates an order on its own.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any, Iterable, Mapping

from strategy.symbol_registry import load_symbols


VERIFICATION_COLUMNS = (
    "symbol",
    "name",
    "market",
    "theme",
    "stock_info_found",
    "price_found",
    "last_price",
    "currency",
    "verification_status",
    "failure_reason",
    "verified_at_utc",
)


def _result(response: Mapping[str, Any] | None) -> Any:
    if not isinstance(response, Mapping):
        return None
    return response.get("result", response)


def _items(response: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    value = _result(response)
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        nested = value.get("items") or value.get("stocks")
        if isinstance(nested, list):
            return [dict(item) for item in nested if isinstance(item, Mapping)]
        return [dict(value)]
    return []


def _symbol(item: Mapping[str, Any]) -> str:
    for key in ("symbol", "stockCode", "ticker", "code", "securityCode"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _value(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def verify_execution_symbols(
    client: Any,
    *,
    symbols_path: str = "data/symbols.csv",
    batch_size: int = 200,
    verified_at_utc: str | None = None,
) -> list[dict[str, Any]]:
    """Read enabled KR/US candidates and record stock/price lookup outcomes.

    A failed batch is retried symbol-by-symbol.  This prevents one delisted or
    malformed ticker from falsely marking every other candidate unsupported.
    """

    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")

    candidates = load_symbols(symbols_path)
    ordered_symbols = list(candidates)
    stock_by_symbol: dict[str, dict[str, Any]] = {}
    price_by_symbol: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    for batch in _chunks(ordered_symbols, batch_size):
        try:
            stock_rows = _items(client.stocks(batch))
            price_rows = _items(client.prices(batch))
        except Exception:
            # A broker response can reject a batch because one input symbol is
            # unavailable.  Retry singly to preserve the usable candidates.
            for symbol in batch:
                try:
                    stock_rows = _items(client.stocks([symbol]))
                    price_rows = _items(client.prices([symbol]))
                except Exception as exc:  # broker-specific response text is audit data
                    errors[symbol] = str(exc)
                    continue
                for item in stock_rows:
                    item_symbol = _symbol(item)
                    if item_symbol:
                        stock_by_symbol[item_symbol] = item
                for item in price_rows:
                    item_symbol = _symbol(item)
                    if item_symbol:
                        price_by_symbol[item_symbol] = item
            continue

        for item in stock_rows:
            item_symbol = _symbol(item)
            if item_symbol:
                stock_by_symbol[item_symbol] = item
        for item in price_rows:
            item_symbol = _symbol(item)
            if item_symbol:
                price_by_symbol[item_symbol] = item

    timestamp = verified_at_utc or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    report: list[dict[str, Any]] = []
    for symbol in ordered_symbols:
        stock = stock_by_symbol.get(symbol, {})
        price = price_by_symbol.get(symbol, {})
        stock_found = bool(stock)
        price_value = _value(price, "lastPrice", "closePrice", "currentPrice", "price")
        price_found = price_value not in (None, "", 0, "0", "0.0")
        failure_reason = errors.get(symbol, "")
        status = "verified" if stock_found and price_found else "unverified"
        report.append({
            "symbol": symbol,
            "name": candidates[symbol].get("name", ""),
            "market": candidates[symbol].get("market", ""),
            "theme": candidates[symbol].get("theme", ""),
            "stock_info_found": stock_found,
            "price_found": price_found,
            "last_price": price_value or "",
            "currency": _value(price, "currency") or _value(stock, "currency"),
            "verification_status": status,
            "failure_reason": failure_reason,
            "verified_at_utc": timestamp,
        })
    return report


def verify_symbol_guesses(
    client: Any,
    guesses: Iterable[Mapping[str, Any]] | Iterable[str],
    *,
    batch_size: int = 50,
    verified_at_utc: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Verify AI-proposed symbol guesses against the real Toss API.

    ``guesses`` may be plain symbol strings or dicts with at least a
    ``symbol_guess``/``symbol`` key (as produced by the
    ``cash_symbol_discovery`` agent role). This is fail-closed: on any
    lookup error, or when Toss does not return both a stock record and a
    usable last price, the symbol is marked unverified and MUST NOT be
    treated as tradable. This function never writes to data/symbols.csv or
    any execution config on its own -- callers decide what to do with a
    verified result.

    Returns a dict keyed by the (uppercased/stripped) input symbol guess,
    each value containing at minimum: symbol, verified (bool), name,
    market_hint (best-effort, from Toss data when available), last_price,
    currency, failure_reason.
    """

    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")

    normalized: list[str] = []
    seen: set[str] = set()
    for guess in guesses:
        if isinstance(guess, Mapping):
            raw = guess.get("symbol_guess") or guess.get("symbol") or ""
        else:
            raw = guess
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)

    stock_by_symbol: dict[str, dict[str, Any]] = {}
    price_by_symbol: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    for batch in _chunks(normalized, batch_size):
        try:
            stock_rows = _items(client.stocks(batch))
            price_rows = _items(client.prices(batch))
        except Exception:
            # One malformed/unknown AI-guessed ticker must not sink the
            # rest of the batch -- retry singly, same pattern as
            # verify_execution_symbols.
            for symbol in batch:
                try:
                    stock_rows = _items(client.stocks([symbol]))
                    price_rows = _items(client.prices([symbol]))
                except Exception as exc:
                    errors[symbol] = str(exc)
                    continue
                for item in stock_rows:
                    item_symbol = _symbol(item)
                    if item_symbol:
                        stock_by_symbol[item_symbol.upper()] = item
                for item in price_rows:
                    item_symbol = _symbol(item)
                    if item_symbol:
                        price_by_symbol[item_symbol.upper()] = item
            continue

        for item in stock_rows:
            item_symbol = _symbol(item)
            if item_symbol:
                stock_by_symbol[item_symbol.upper()] = item
        for item in price_rows:
            item_symbol = _symbol(item)
            if item_symbol:
                price_by_symbol[item_symbol.upper()] = item

    timestamp = verified_at_utc or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    results: dict[str, dict[str, Any]] = {}
    for symbol in normalized:
        stock = stock_by_symbol.get(symbol, {})
        price = price_by_symbol.get(symbol, {})
        stock_found = bool(stock)
        price_value = _value(price, "lastPrice", "closePrice", "currentPrice", "price")
        price_found = price_value not in (None, "", 0, "0", "0.0")
        failure_reason = errors.get(symbol, "")
        if not stock_found:
            failure_reason = failure_reason or "toss_stock_lookup_empty"
        elif not price_found:
            failure_reason = failure_reason or "toss_price_lookup_empty"
        verified = stock_found and price_found and not failure_reason
        results[symbol] = {
            "symbol": symbol,
            "verified": verified,
            "name": _value(stock, "name", "stockName", "korName") or "",
            "market_hint": _value(stock, "market", "exchange", "nation") or "",
            "last_price": price_value or "",
            "currency": _value(price, "currency") or _value(stock, "currency") or "",
            "failure_reason": "" if verified else failure_reason,
            "verified_at_utc": timestamp,
        }
    return results


def write_verification_report(rows: Iterable[Mapping[str, Any]], path: str | Path) -> Path:
    """Atomically write the lookup report; execution configuration is untouched."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=VERIFICATION_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination
