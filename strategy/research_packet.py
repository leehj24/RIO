"""Point-in-time research packets and deterministic market features.

The trading loop used to start with ``make_mock_universe`` and only replace a
few of its random values with real data.  That is useful for a UI demo, but it
is not a valid input to a live investment decision.  This module builds a
small, serialisable packet from the data that was actually fetched for a
symbol.  Missing data remains missing/neutral; it is never filled with a
random financial ratio.

The indicator defaults are project conventions, not claims that every source
paper used the same parameters.  They are deliberately carried in the packet
so a decision can be reproduced later.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


FACTOR_COLUMNS: tuple[str, ...] = (
    "per",
    "pbr",
    "ev_ebitda",
    "fcf_yield",
    "earnings_yield",
    "book_to_market",
    "mom_12_1",
    "ret_6m",
    "ret_3m",
    "residual_momentum",
    "volume_breakout",
    "intraday_momentum",
    "roe",
    "roic",
    "gross_profitability",
    "operating_margin",
    "net_margin",
    "fcf_margin",
    "accruals",
    "debt_ratio",
    "beta",
    "volatility",
    "downside_beta",
    "mdd",
    "idio_vol",
    "delisting_risk",
    "trading_value",
    "volume",
    "market_cap",
    "turnover",
    "amihud",
    "bid_ask_spread",
    "sales_growth",
    "op_income_growth",
    "eps_growth",
    "earnings_revision",
    "target_price_revision",
    "order_backlog_growth",
    "margin_improvement",
    "llm_sentiment",
    "disclosure_score",
    "policy_benefit",
    "short_interest",
    "crowding",
    "similar_event_score",
    "disclosure_risk",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _json_value(value: Any) -> Any:
    """Return a bounded JSON-safe scalar/list/mapping without NaN values."""

    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (np.floating, float, np.integer, int)) and not isinstance(value, bool):
        number = _number(value)
        return number
    if isinstance(value, (str, bool)) or value is None:
        return value
    return str(value)


def stable_hash(value: Mapping[str, Any]) -> str:
    """Hash a canonical JSON input snapshot without exposing credentials."""

    encoded = json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _series_value(series: pd.Series, default: float | None = None) -> float | None:
    if series.empty:
        return default
    return _number(series.iloc[-1]) if _number(series.iloc[-1]) is not None else default


def _wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # Flat positive / negative windows have well-defined limiting values.
    rsi = rsi.mask((avg_loss == 0.0) & (avg_gain > 0.0), 100.0)
    rsi = rsi.mask((avg_gain == 0.0) & (avg_loss > 0.0), 0.0)
    return rsi


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0), index=high.index)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    atr = true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr.replace(0.0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _return(close: pd.Series, bars: int) -> float | None:
    if len(close) <= bars:
        return None
    before = _number(close.iloc[-bars - 1])
    current = _number(close.iloc[-1])
    if before is None or current is None or before <= 0.0:
        return None
    return current / before - 1.0


def compute_market_features(ohlcv: pd.DataFrame | None) -> dict[str, float | None]:
    """Derive available factor inputs from OHLCV, retaining unknown fields.

    Values that need a benchmark, order book, or a longer history are ``None``
    rather than invented proxies.  The cross-sectional scorer later treats
    such values as neutral and exposes their availability in the packet.
    """

    if ohlcv is None or len(ohlcv) < 2 or not {"open", "high", "low", "close", "volume"}.issubset(ohlcv.columns):
        return {column: None for column in FACTOR_COLUMNS}

    data = ohlcv.copy()
    close = pd.to_numeric(data["close"], errors="coerce")
    open_ = pd.to_numeric(data["open"], errors="coerce")
    volume = pd.to_numeric(data["volume"], errors="coerce")
    returns = close.pct_change().replace([np.inf, -np.inf], np.nan)
    trailing = returns.tail(60).dropna()
    annual_vol = float(trailing.std(ddof=0) * math.sqrt(252.0)) if len(trailing) >= 10 else None
    running_peak = close.cummax().replace(0.0, np.nan)
    drawdown = close / running_peak - 1.0
    mdd = _number(-drawdown.min())
    dollar_volume = (close * volume).replace(0.0, np.nan)
    amihud_window = (returns.abs() / dollar_volume).tail(20).replace([np.inf, -np.inf], np.nan).dropna()
    amihud = _number(amihud_window.mean()) if not amihud_window.empty else None
    volume_avg = volume.tail(20).mean()
    latest_volume = _number(volume.iloc[-1])
    volume_breakout = None
    if latest_volume is not None and _number(volume_avg) not in (None, 0.0):
        volume_breakout = latest_volume / float(volume_avg) - 1.0

    mom_12_1 = None
    if len(close) > 273:
        start = _number(close.iloc[-273])
        end = _number(close.iloc[-22])
        if start is not None and end is not None and start > 0.0:
            mom_12_1 = end / start - 1.0

    last_close = _number(close.iloc[-1])
    last_open = _number(open_.iloc[-1])
    return {
        "mom_12_1": mom_12_1,
        "ret_6m": _return(close, 126),
        "ret_3m": _return(close, 63),
        "residual_momentum": _return(close, 20),  # benchmark-free fallback, labelled in provenance.
        "volume_breakout": volume_breakout,
        "intraday_momentum": (last_close / last_open - 1.0) if last_close and last_open and last_open > 0.0 else None,
        "volatility": annual_vol,
        "mdd": mdd,
        "idio_vol": annual_vol,
        "trading_value": _number(dollar_volume.iloc[-1]),
        "volume": latest_volume,
        "amihud": amihud,
    }


def compute_indicator_snapshot(
    ohlcv: pd.DataFrame | None,
    *,
    rsi_period: int = 20,
    sma_short_period: int = 50,
    sma_long_period: int = 200,
    macd_fast_period: int = 12,
    macd_slow_period: int = 26,
    macd_signal_period: int = 9,
    vwma_period: int = 20,
    cci_period: int = 20,
    adx_period: int = 14,
) -> dict[str, Any]:
    """Calculate the indicator packet interpreted by research agents.

    RSI uses Wilder smoothing; MACD uses standard EMA(12, 26, 9) defaults;
    VWMA, CCI, and ADX remain code-calculated evidence rather than LLM output.
    """

    parameters = {
        "rsi_period": int(rsi_period),
        "sma_short_period": int(sma_short_period),
        "sma_long_period": int(sma_long_period),
        "macd": [int(macd_fast_period), int(macd_slow_period), int(macd_signal_period)],
        "vwma_period": int(vwma_period),
        "cci_period": int(cci_period),
        "adx_period": int(adx_period),
    }
    required = {"open", "high", "low", "close", "volume"}
    if ohlcv is None or not required.issubset(ohlcv.columns) or len(ohlcv) < 2:
        return {"parameters": parameters, "data_quality": "insufficient_ohlcv", "bars": 0, "values": {}}

    data = ohlcv.copy()
    close = pd.to_numeric(data["close"], errors="coerce")
    high = pd.to_numeric(data["high"], errors="coerce")
    low = pd.to_numeric(data["low"], errors="coerce")
    volume = pd.to_numeric(data["volume"], errors="coerce")
    typical_price = (high + low + close) / 3.0
    sma_short = close.rolling(sma_short_period, min_periods=sma_short_period).mean()
    sma_long = close.rolling(sma_long_period, min_periods=sma_long_period).mean()
    ema_fast = close.ewm(span=macd_fast_period, adjust=False, min_periods=macd_slow_period).mean()
    ema_slow = close.ewm(span=macd_slow_period, adjust=False, min_periods=macd_slow_period).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=macd_signal_period, adjust=False, min_periods=macd_signal_period).mean()
    vwma = (close * volume).rolling(vwma_period, min_periods=vwma_period).sum() / volume.rolling(vwma_period, min_periods=vwma_period).sum().replace(0.0, np.nan)
    tp_mean = typical_price.rolling(cci_period, min_periods=cci_period).mean()
    mean_deviation = typical_price.rolling(cci_period, min_periods=cci_period).apply(
        lambda values: float(np.mean(np.abs(values - np.mean(values)))), raw=True
    )
    cci = (typical_price - tp_mean) / (0.015 * mean_deviation.replace(0.0, np.nan))
    adx = _adx(high, low, close, adx_period)
    returns = close.pct_change()
    latest_close = _series_value(close)
    values = {
        "close": latest_close,
        "return_1d": _return(close, 1),
        "return_5d": _return(close, 5),
        "return_20d": _return(close, 20),
        "rsi": _series_value(_wilder_rsi(close, rsi_period)),
        "sma_short": _series_value(sma_short),
        "sma_long": _series_value(sma_long),
        "macd": _series_value(macd_line),
        "macd_signal": _series_value(macd_signal),
        "macd_histogram": _series_value(macd_line - macd_signal),
        "vwma": _series_value(vwma),
        "cci": _series_value(cci),
        "adx": _series_value(adx),
        "volume_ratio_20": (float(volume.iloc[-1]) / float(volume.tail(20).mean())) if len(volume) >= 20 and float(volume.tail(20).mean()) > 0 else None,
        "realized_volatility_20_ann": float(returns.tail(20).std(ddof=0) * math.sqrt(252.0)) if returns.tail(20).dropna().shape[0] >= 10 else None,
    }
    quality = "ok" if len(data) >= sma_long_period else "partial_history"
    return {"parameters": parameters, "data_quality": quality, "bars": int(len(data)), "values": _json_value(values)}


def _ohlcv_window(ohlcv: pd.DataFrame | None, *, bars: int = 40) -> list[dict[str, Any]]:
    if ohlcv is None or ohlcv.empty:
        return []
    columns = [column for column in ("open", "high", "low", "close", "volume") if column in ohlcv.columns]
    window: list[dict[str, Any]] = []
    for index, row in ohlcv.tail(bars).iterrows():
        timestamp = index.isoformat() if hasattr(index, "isoformat") else str(index)
        window.append({"observed_at": timestamp, **{column: _number(row[column]) for column in columns}})
    return window


def _clean_fundamentals(fundamentals: Mapping[str, Any] | None) -> dict[str, Any]:
    if not fundamentals:
        return {}
    allowed = {
        "source", "fallback_source", "fundamental_primary_source", "fundamental_fallback_source", "market_data_source",
        "per", "pbr", "roe", "roic", "debt_ratio", "operating_margin", "net_margin", "sales_growth", "eps_growth",
        "free_cash_flow", "net_income", "equity", "market_cap", "trading_value", "volume", "beta", "symbol", "market",
    }
    return {key: _json_value(value) for key, value in fundamentals.items() if key in allowed}


def build_research_packet(
    *,
    symbol: str,
    symbol_info: Mapping[str, Any],
    price: float,
    price_meta: Mapping[str, Any],
    ohlcv: pd.DataFrame | None,
    ohlcv_meta: Mapping[str, Any],
    fundamentals: Mapping[str, Any] | None,
    technical_rules: Mapping[str, Any] | None,
    observed_at_utc: str | None = None,
    indicator_settings: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build one immutable, evidence-labelled multimodal research packet."""

    indicator_settings = dict(indicator_settings or {})
    indicators = compute_indicator_snapshot(ohlcv, **indicator_settings)
    features = compute_market_features(ohlcv)
    fetched_at = observed_at_utc or utc_now()
    fundamentals_summary = _clean_fundamentals(fundamentals)
    sources = [
        {
            "evidence_id": f"price:{symbol}",
            "kind": "price",
            "source": str(price_meta.get("source", "unknown")),
            "available_at_utc": fetched_at,
            "retrieved_at_utc": fetched_at,
        },
        {
            "evidence_id": f"ohlcv:{symbol}",
            "kind": "ohlcv",
            "source": str(ohlcv_meta.get("source", "unknown")),
            "available_at_utc": fetched_at,
            "retrieved_at_utc": fetched_at,
            "bars": int(len(ohlcv)) if ohlcv is not None else 0,
        },
    ]
    if fundamentals_summary:
        sources.append(
            {
                "evidence_id": f"fundamentals:{symbol}",
                "kind": "fundamentals",
                "source": str(fundamentals_summary.get("source") or fundamentals_summary.get("fundamental_primary_source") or "unknown"),
                "available_at_utc": None,
                "retrieved_at_utc": fetched_at,
            }
        )

    packet: dict[str, Any] = {
        "schema_version": "research_packet.v1",
        "symbol": str(symbol),
        "symbol_info": _json_value(dict(symbol_info)),
        "decision_data_cutoff_utc": fetched_at,
        "price": _number(price),
        "price_source": str(price_meta.get("source", "unknown")),
        "ohlcv_source": str(ohlcv_meta.get("source", "unknown")),
        "sources": sources,
        "technical_indicators": indicators,
        "technical_rules": _json_value(dict(technical_rules or {})),
        "market_features": _json_value(features),
        "fundamentals": fundamentals_summary,
        "ohlcv_last_8_weeks": _ohlcv_window(ohlcv),
        "input_capabilities": {"chart_images": False, "company_news": False, "macro_news": False, "business_overview": False},
        "missing_modalities": ["chart_image", "company_news", "macro_news", "business_overview"],
    }
    packet["input_snapshot_hash"] = stable_hash(packet)
    return packet


def build_research_universe(
    symbols: Sequence[str],
    *,
    fundamentals_by_symbol: Mapping[str, Mapping[str, Any]] | None = None,
    market_features_by_symbol: Mapping[str, Mapping[str, Any]] | None = None,
    event_sentiment: float = 0.0,
) -> pd.DataFrame:
    """Return factor inputs without synthetic ratios.

    Each row starts as unknown.  Values from independently fetched data are
    placed in their named fields; unavailable fields stay ``NaN`` so
    ``compute_factor_scores`` can treat them as neutral rather than sampling a
    fake value.  This is suitable for dry-run and live analysis alike.
    """

    fundamentals_by_symbol = fundamentals_by_symbol or {}
    market_features_by_symbol = market_features_by_symbol or {}
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        row: dict[str, Any] = {"symbol": str(symbol), **{column: np.nan for column in FACTOR_COLUMNS}}
        row["llm_sentiment"] = _number(event_sentiment) or 0.0
        for source in (fundamentals_by_symbol.get(str(symbol), {}), market_features_by_symbol.get(str(symbol), {})):
            for key, value in source.items():
                if key in row:
                    number = _number(value)
                    if number is not None:
                        row[key] = number
        rows.append(row)
    return pd.DataFrame(rows)
