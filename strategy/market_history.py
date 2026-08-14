"""Persistent OHLCV history for the strategy and dashboard.

The Toss candle endpoint is treated as an incremental source, not as the
dashboard's storage layer.  Raw one-minute and daily bars are de-duplicated in
SQLite; weekly/monthly display bars are derived from those raw observations.
This keeps one canonical history per symbol while allowing a newly listed
instrument to return its genuinely available (shorter) history.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping

import pandas as pd

from strategy.market_data import fetch_candles_paginated


UTC = dt.timezone.utc


@dataclass(frozen=True)
class ChartRangePlan:
    key: str
    label: str
    base_interval: str
    display_interval: str
    target_bars: int
    lookback_days: int
    tolerance_days: int


# Buttons express a viewing range.  The server chooses a useful candle
# resolution for that range, as ordinary brokerage charts do.
CHART_RANGE_PLANS: Dict[str, ChartRangePlan] = {
    "1d": ChartRangePlan("1d", "1일", "1m", "1m", 450, 1, 1),
    "1w": ChartRangePlan("1w", "1주", "1m", "15m", 2_100, 7, 3),
    "1m": ChartRangePlan("1m", "1개월", "1d", "1d", 32, 31, 10),
    "3m": ChartRangePlan("3m", "3개월", "1d", "1d", 80, 93, 18),
    "1y": ChartRangePlan("1y", "1년", "1d", "1d", 270, 365, 50),
    "3y": ChartRangePlan("3y", "3년", "1d", "1w", 800, 365 * 3, 75),
    "5y": ChartRangePlan("5y", "5년", "1d", "1w", 1_350, 365 * 5, 100),
    "10y": ChartRangePlan("10y", "10년", "1d", "1mo", 2_700, 365 * 10, 150),
}


def chart_range_plan(value: str) -> ChartRangePlan:
    key = str(value or "1y").strip().lower()
    if key not in CHART_RANGE_PLANS:
        raise ValueError(f"unsupported chart range: {value}")
    return CHART_RANGE_PLANS[key]


def _utc_now() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(value: object) -> float | None:
    parsed = _parse_utc(value)
    if parsed is None:
        return None
    return max(0.0, (dt.datetime.now(UTC) - parsed).total_seconds())


def _first(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _number(value: object, *, default: float | None = None) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _candle_items(response: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    result: Any = (response or {}).get("result", response or {})
    if isinstance(result, Mapping):
        for key in ("candles", "items", "data", "prices"):
            if isinstance(result.get(key), list):
                result = result[key]
                break
    return [item for item in result if isinstance(item, Mapping)] if isinstance(result, list) else []


def normalize_candles(
    response: Mapping[str, Any] | None,
    *,
    symbol: str,
    interval: str,
    source: str = "toss",
    fetched_at_utc: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize provider rows without inventing timestamps or prices."""

    fetched_at = fetched_at_utc or _utc_now()
    normalized: list[dict[str, Any]] = []
    for item in _candle_items(response):
        timestamp = pd.to_datetime(
            _first(item, "timestamp", "time", "date"),
            errors="coerce",
            utc=True,
        )
        if pd.isna(timestamp):
            continue
        open_price = _number(_first(item, "open", "openPrice", "o"))
        high_price = _number(_first(item, "high", "highPrice", "h"))
        low_price = _number(_first(item, "low", "lowPrice", "l"))
        close_price = _number(_first(item, "close", "closePrice", "price", "c"))
        volume = _number(_first(item, "volume", "v"), default=0.0)
        if None in (open_price, high_price, low_price, close_price):
            continue
        normalized.append(
            {
                "symbol": str(symbol),
                "interval": str(interval),
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
                "volume": float(volume or 0.0),
                "currency": str(item.get("currency") or ""),
                "source": str(source),
                "fetched_at_utc": fetched_at,
            }
        )
    return normalized


class MarketHistoryStore:
    """SQLite-backed raw market history with incremental provider refreshes."""

    def __init__(self, path: str | Path, *, legacy_cache_dir: str | Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_cache_dir = Path(legacy_cache_dir) if legacy_cache_dir else None
        self._lock = threading.RLock()
        self._ensure_schema()
        self._migrate_legacy_json_once()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ohlcv_bars (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    fetched_at_utc TEXT NOT NULL,
                    PRIMARY KEY (symbol, interval, timestamp)
                );
                CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_interval_time
                    ON ohlcv_bars(symbol, interval, timestamp DESC);

                CREATE TABLE IF NOT EXISTS market_history_sync (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    requested_bars INTEGER NOT NULL DEFAULT 0,
                    history_exhausted INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at_utc TEXT,
                    last_success_at_utc TEXT,
                    last_error TEXT,
                    PRIMARY KEY (symbol, interval)
                );

                CREATE TABLE IF NOT EXISTS market_history_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _upsert_rows(connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        connection.executemany(
            """
            INSERT INTO ohlcv_bars (
                symbol, interval, timestamp, open, high, low, close, volume,
                currency, source, fetched_at_utc
            ) VALUES (
                :symbol, :interval, :timestamp, :open, :high, :low, :close,
                :volume, :currency, :source, :fetched_at_utc
            )
            ON CONFLICT(symbol, interval, timestamp) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                currency=excluded.currency,
                source=excluded.source,
                fetched_at_utc=excluded.fetched_at_utc
            """,
            rows,
        )

    def _migrate_legacy_json_once(self) -> None:
        """Import the old per-symbol JSON cache once, without deleting it."""

        if self.legacy_cache_dir is None or not self.legacy_cache_dir.exists():
            return
        with self._lock, self._connect() as connection:
            done = connection.execute(
                "SELECT value FROM market_history_meta WHERE key='legacy_json_migration_v1'"
            ).fetchone()
            if done is not None:
                return
            imported = 0
            for path in self.legacy_cache_dir.glob("*.json"):
                parts = path.stem.rsplit("_", 2)
                if len(parts) != 3 or parts[1] not in {"1d", "1m"}:
                    continue
                symbol, interval, requested = parts
                try:
                    response = json.loads(path.read_text(encoding="utf-8")).get("response", {})
                    fetched_at = dt.datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(
                        microsecond=0
                    ).isoformat().replace("+00:00", "Z")
                    rows = normalize_candles(
                        response,
                        symbol=symbol,
                        interval=interval,
                        source="toss_legacy_json",
                        fetched_at_utc=fetched_at,
                    )
                    self._upsert_rows(connection, rows)
                    requested_bars = int(requested)
                    connection.execute(
                        """
                        INSERT INTO market_history_sync (
                            symbol, interval, requested_bars, history_exhausted,
                            last_attempt_at_utc, last_success_at_utc, last_error
                        ) VALUES (?, ?, ?, 0, ?, ?, NULL)
                        ON CONFLICT(symbol, interval) DO UPDATE SET
                            requested_bars=MAX(requested_bars, excluded.requested_bars),
                            last_success_at_utc=excluded.last_success_at_utc
                        """,
                        (symbol, interval, requested_bars, fetched_at, fetched_at),
                    )
                    imported += len(rows)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
            connection.execute(
                "INSERT OR REPLACE INTO market_history_meta(key, value) VALUES (?, ?)",
                ("legacy_json_migration_v1", json.dumps({"rows": imported, "at": _utc_now()})),
            )

    def _sync_row(self, connection: sqlite3.Connection, symbol: str, interval: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM market_history_sync WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
        return dict(row) if row is not None else {}

    def _load_rows(
        self,
        connection: sqlite3.Connection,
        symbol: str,
        interval: str,
        limit: int,
    ) -> pd.DataFrame:
        rows = connection.execute(
            """
            SELECT timestamp, open, high, low, close, volume, currency, source
            FROM ohlcv_bars
            WHERE symbol=? AND interval=?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (symbol, interval, max(1, int(limit))),
        ).fetchall()
        if not rows:
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume", "currency", "source"]
            )
        frame = pd.DataFrame([dict(row) for row in reversed(rows)])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
        return frame.dropna(subset=["timestamp", "open", "high", "low", "close"]).reset_index(drop=True)

    def ensure_base_history(
        self,
        client: Any,
        symbol: str,
        *,
        interval: str,
        target_bars: int,
        freshness_seconds: float,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Return stored raw bars and fetch only when depth/freshness requires it."""

        symbol = str(symbol or "").strip()
        interval = str(interval or "").strip().lower()
        if not symbol:
            raise ValueError("symbol is required")
        if interval not in {"1m", "1d"}:
            raise ValueError(f"unsupported raw interval: {interval}")
        target = max(1, int(target_bars))
        freshness = max(0.0, float(freshness_seconds))

        with self._lock:
            with self._connect() as connection:
                existing = self._load_rows(connection, symbol, interval, target)
                sync = self._sync_row(connection, symbol, interval)

            age = _age_seconds(sync.get("last_success_at_utc"))
            stale = age is None or age > freshness
            exhausted = bool(sync.get("history_exhausted"))
            needs_depth = len(existing) < target and not exhausted
            fetch_target = 0
            if existing.empty or needs_depth:
                fetch_target = target
            elif stale:
                # Refresh the newest page only.  Older rows are immutable and
                # remain in SQLite; this avoids downloading ten years again.
                fetch_target = min(200, target)

            attempted_fetch = fetch_target > 0
            fetched_rows: list[dict[str, Any]] = []
            page_meta: dict[str, Any] = {}
            fetch_error = ""
            attempt_at = _utc_now()
            if attempted_fetch:
                try:
                    response, page_meta = fetch_candles_paginated(
                        client,
                        symbol,
                        interval=interval,
                        total_count=fetch_target,
                        page_size=200,
                        cache_dir=None,
                        cache_ttl_seconds=0,
                    )
                    fetched_rows = normalize_candles(
                        response,
                        symbol=symbol,
                        interval=interval,
                        source="toss",
                        fetched_at_utc=attempt_at,
                    )
                    if not fetched_rows:
                        fetch_error = "provider_returned_no_valid_candles"
                except Exception as exc:
                    fetch_error = str(exc)[:1000]

                with self._connect() as connection:
                    if fetched_rows:
                        self._upsert_rows(connection, fetched_rows)
                    newly_exhausted = bool(page_meta.get("source_exhausted"))
                    connection.execute(
                        """
                        INSERT INTO market_history_sync (
                            symbol, interval, requested_bars, history_exhausted,
                            last_attempt_at_utc, last_success_at_utc, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol, interval) DO UPDATE SET
                            requested_bars=MAX(requested_bars, excluded.requested_bars),
                            history_exhausted=MAX(history_exhausted, excluded.history_exhausted),
                            last_attempt_at_utc=excluded.last_attempt_at_utc,
                            last_success_at_utc=COALESCE(excluded.last_success_at_utc, last_success_at_utc),
                            last_error=excluded.last_error
                        """,
                        (
                            symbol,
                            interval,
                            target,
                            int(newly_exhausted),
                            attempt_at,
                            attempt_at if fetched_rows else None,
                            fetch_error or None,
                        ),
                    )

            with self._connect() as connection:
                frame = self._load_rows(connection, symbol, interval, target)
                final_sync = self._sync_row(connection, symbol, interval)

            if frame.empty and fetch_error:
                raise RuntimeError(fetch_error)

            return frame, {
                "symbol": symbol,
                "base_interval": interval,
                "requested_bars": target,
                "stored_bars": int(len(frame)),
                "storage": "sqlite",
                "database_path": str(self.path),
                "provider_fetch": attempted_fetch,
                "provider_pages": int(page_meta.get("page_count", 0) or 0),
                "provider_rows": len(fetched_rows),
                "history_exhausted": bool(final_sync.get("history_exhausted")),
                "last_success_at_utc": final_sync.get("last_success_at_utc"),
                "last_error": final_sync.get("last_error"),
            }

    @staticmethod
    def _aggregate(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
        if frame.empty or interval in {"1m", "1d"}:
            return frame.copy()
        rules = {
            "15m": "15min",
            "1h": "1h",
            "1w": "W-FRI",
            "1mo": "ME",
        }
        rule = rules.get(interval)
        if rule is None:
            raise ValueError(f"unsupported display interval: {interval}")
        indexed = frame.set_index("timestamp").sort_index()
        aggregation: dict[str, str] = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "currency": "last",
            "source": "last",
        }
        if interval in {"1w", "1mo"}:
            # Resample labels incomplete periods by their theoretical period
            # end. Preserve the last real observation so a current month never
            # appears to have data from a future month-end date.
            indexed["_last_observed_timestamp"] = indexed.index
            aggregation["_last_observed_timestamp"] = "last"
        aggregated = indexed.resample(rule).agg(aggregation)
        aggregated = aggregated.dropna(subset=["open", "high", "low", "close"]).reset_index()
        if "_last_observed_timestamp" in aggregated.columns:
            aggregated["timestamp"] = aggregated.pop("_last_observed_timestamp")
        return aggregated

    def chart_payload(
        self,
        client: Any,
        symbol: str,
        *,
        range_key: str,
        freshness_seconds: float,
    ) -> dict[str, Any]:
        plan = chart_range_plan(range_key)
        base, sync = self.ensure_base_history(
            client,
            symbol,
            interval=plan.base_interval,
            target_bars=plan.target_bars,
            freshness_seconds=freshness_seconds,
        )
        requested_start: dt.datetime | None = None
        if not base.empty:
            last_observation = base["timestamp"].iloc[-1].to_pydatetime()
            requested_start = last_observation - dt.timedelta(days=plan.lookback_days)

        # A one-day chart should contain the latest trading session, not the
        # tail of yesterday plus today's bars merely because the provider page
        # contains a fixed number of observations.  A multi-hour gap reliably
        # separates regular sessions for both KR and US minute data while still
        # allowing an in-progress or shortened session to be shown as-is.
        if plan.key == "1d" and len(base) > 1:
            session_breaks = base["timestamp"].diff() > pd.Timedelta(hours=4)
            if bool(session_breaks.any()):
                latest_session_start = int(session_breaks[session_breaks].index[-1])
                base = base.iloc[latest_session_start:].reset_index(drop=True)
        elif requested_start is not None:
            # Fetch targets are deliberately generous to survive weekends and
            # holidays. The chart itself must still honor the calendar range
            # named by the button instead of showing every prefetched bar.
            ranged = base[base["timestamp"] >= requested_start]
            if not ranged.empty:
                base = ranged.reset_index(drop=True)
        display = self._aggregate(base, plan.display_interval)
        if display.empty:
            return {
                "result": {"candles": []},
                "meta": {
                    **sync,
                    "range": plan.key,
                    "range_label": plan.label,
                    "display_interval": plan.display_interval,
                    "history_status": "unavailable",
                },
            }

        # Availability describes the source observations, not an aggregation
        # bucket label such as calendar month-end.
        first = base["timestamp"].iloc[0].to_pydatetime()
        last = base["timestamp"].iloc[-1].to_pydatetime()
        requested_start = requested_start or (last - dt.timedelta(days=plan.lookback_days))
        partial = first > requested_start + dt.timedelta(days=plan.tolerance_days)
        if partial:
            reason = (
                "listed_later_or_source_history_limited"
                if sync.get("history_exhausted")
                else "history_backfill_incomplete"
            )
        else:
            reason = ""

        candles = [
            {
                "timestamp": row.timestamp.isoformat().replace("+00:00", "Z"),
                "openPrice": float(row.open),
                "highPrice": float(row.high),
                "lowPrice": float(row.low),
                "closePrice": float(row.close),
                "volume": float(row.volume),
                "currency": str(row.currency or ""),
            }
            for row in display.itertuples(index=False)
        ]
        return {
            "result": {"candles": candles},
            "meta": {
                **sync,
                "range": plan.key,
                "range_label": plan.label,
                "display_interval": plan.display_interval,
                "order": "ascending",
                "display_bars": len(candles),
                "requested_start_utc": requested_start.isoformat().replace("+00:00", "Z"),
                "available_from_utc": first.isoformat().replace("+00:00", "Z"),
                "available_to_utc": last.isoformat().replace("+00:00", "Z"),
                "history_status": "partial" if partial else "full",
                "partial_history": partial,
                "partial_reason": reason,
            },
        }

    def strategy_ohlcv(
        self,
        client: Any,
        symbol: str,
        *,
        bars: int,
        freshness_seconds: float,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        frame, meta = self.ensure_base_history(
            client,
            symbol,
            interval="1d",
            target_bars=bars,
            freshness_seconds=freshness_seconds,
        )
        ohlcv = frame[["open", "high", "low", "close", "volume"]].copy()
        return ohlcv.reset_index(drop=True), meta
