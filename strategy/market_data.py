import json
import re
import time
from pathlib import Path
from typing import Dict, Any

import pandas as pd


def _candle_page(response: Dict[str, Any]) -> tuple[list[dict], str | None]:
    """Extract a Toss candle page and its cursor without assuming one envelope."""

    result = response.get("result", response) if isinstance(response, dict) else response
    if isinstance(result, dict):
        candles = result.get("candles") or result.get("items") or result.get("data") or result.get("prices") or []
        next_before = result.get("nextBefore")
    elif isinstance(result, list):
        candles = result
        next_before = None
    else:
        candles = []
        next_before = None
    return [dict(item) for item in candles if isinstance(item, dict)], str(next_before) if next_before else None


def fetch_candles_paginated(
    client: Any,
    symbol: str,
    *,
    interval: str,
    total_count: int,
    page_size: int = 200,
    cache_dir: str | Path | None = None,
    cache_ttl_seconds: float = 0.0,
) -> tuple[Dict[str, Any], dict[str, int]]:
    """Fetch more than one Toss candle page using the official ``before`` cursor.

    Toss limits a page to 200 candles. ``nextBefore`` is inclusive, so every
    page after the first asks for one extra candle and rows are deduplicated by
    timestamp before the requested most-recent history is returned.
    """

    target = max(1, int(total_count))
    maximum = min(200, max(1, int(page_size)))
    cache_path: Path | None = None
    if cache_dir and float(cache_ttl_seconds) > 0:
        safe_symbol = re.sub(r"[^A-Za-z0-9._-]+", "_", str(symbol))
        cache_path = Path(cache_dir) / f"{safe_symbol}_{interval}_{target}.json"
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            saved_at = float(cached.get("saved_at_epoch", 0.0))
            cached_response = cached.get("response")
            cached_candles, _ = _candle_page(cached_response if isinstance(cached_response, dict) else {})
            age = max(0.0, time.time() - saved_at)
            if age <= float(cache_ttl_seconds) and len(cached_candles) >= target:
                return {"result": {"candles": cached_candles[:target]}}, {
                    "requested_count": target,
                    "page_count": 0,
                    "raw_count": target,
                    "cache_hit": 1,
                    "cache_age_seconds": int(age),
                    "source_exhausted": 0,
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A cache miss/corrupt cache must never make a real-data request
            # look valid.  Fetch a fresh payload below instead.
            pass
    cursor: str | None = None
    pages = 0
    all_candles: list[dict] = []
    seen_timestamps: set[str] = set()
    source_exhausted = False

    while len(all_candles) < target:
        remaining = target - len(all_candles)
        requested = min(maximum, remaining + (1 if cursor else 0))
        page = client.candles(symbol, interval=interval, count=requested, before=cursor)
        candles, next_before = _candle_page(page)
        pages += 1
        if not candles:
            source_exhausted = True
            break

        for candle in candles:
            timestamp = str(candle.get("timestamp") or candle.get("time") or candle.get("date") or "")
            # Use the whole row as a fallback key for older response shapes
            # that do not expose a timestamp field.
            key = timestamp or repr(sorted(candle.items()))
            if key in seen_timestamps:
                continue
            seen_timestamps.add(key)
            all_candles.append(candle)
            if len(all_candles) >= target:
                break

        if not next_before or next_before == cursor:
            source_exhausted = True
            break
        cursor = next_before

    response = {"result": {"candles": all_candles}}
    if cache_path is not None and all_candles:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"saved_at_epoch": time.time(), "response": response},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary.replace(cache_path)
        except OSError:
            # Caching is an optimization.  The returned source remains the
            # successfully fetched Toss response even if the cache cannot be
            # persisted.
            pass

    return response, {
        "requested_count": target,
        "page_count": pages,
        "raw_count": len(all_candles),
        "cache_hit": 0,
        "cache_age_seconds": 0,
        "source_exhausted": int(source_exhausted and len(all_candles) < target),
    }


def candles_response_to_ohlcv(response: Dict[str, Any]) -> pd.DataFrame:
    """
    Toss candles 응답 필드명이 달라도 최대한 OHLCV로 변환.
    실패하면 빈 DataFrame 반환.
    """
    if not response:
        return pd.DataFrame()

    result = response.get("result", response)
    if isinstance(result, dict):
        for key in ["candles", "items", "data", "prices"]:
            if isinstance(result.get(key), list):
                result = result[key]
                break

    if not isinstance(result, list) or not result:
        return pd.DataFrame()

    rows = []
    for item in result:
        if not isinstance(item, dict):
            continue
        rows.append({
            "_timestamp": item.get("timestamp") or item.get("time") or item.get("date"),
            "open": item.get("open") or item.get("openPrice") or item.get("o"),
            "high": item.get("high") or item.get("highPrice") or item.get("h"),
            "low": item.get("low") or item.get("lowPrice") or item.get("l"),
            "close": item.get("close") or item.get("closePrice") or item.get("price") or item.get("c"),
            "volume": item.get("volume") or item.get("v") or 0,
        })

    df = pd.DataFrame(rows)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # Toss returns the newest candle first.  All indicator code below assumes
    # chronological order, so explicitly sort on the exchange timestamp before
    # calculating a rolling window.  Do not reverse blindly: older response
    # shapes without timestamps keep their supplied order rather than inventing
    # chronology.
    parsed_timestamp = pd.to_datetime(df["_timestamp"], errors="coerce", utc=True)
    if parsed_timestamp.notna().any():
        df = df.assign(_parsed_timestamp=parsed_timestamp).sort_values(
            "_parsed_timestamp", kind="stable", na_position="last"
        )
    df = df.drop(columns=["_timestamp", "_parsed_timestamp"], errors="ignore")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return df
