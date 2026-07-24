import argparse
import json
import time
import datetime as dt
import threading
import uuid
import webbrowser
from pathlib import Path

import numpy as np

from config import Settings
from toss_client import TossClient, TossAPIError
from strategy.events import load_events
from strategy.lmsr import LMSRMarket
from strategy.data_sources import SapienceClient, make_mock_ohlcv
from strategy.symbol_registry import describe_symbol
from strategy.llm_pipeline import DualProviderLLMPipeline
from strategy.naver_search import NaverSearchClient
from strategy.agentic.orchestrator import GeminiAgentOrchestrator
from strategy.factors import compute_factor_scores
from strategy.technical_entries import compute_technical_rules
from strategy.sde import build_sde_inputs, gbm_prob_up
from strategy.kelly import prediction_market_kelly_yes, prediction_market_kelly_no, aggressive_position_fraction
from strategy.risk import survival_gate, calculate_final_alpha, decide_action
from strategy.exit_rules import evaluate_exit_rules
from strategy.order_builder import build_buy_order, build_sell_order
from strategy.position_manager import get_positions_map, apply_dry_run_fill
from strategy.portfolio_budget import buy_budget_krw, diversified_order_value_krw
from strategy.market_data import candles_response_to_ohlcv, fetch_candles_paginated
from strategy.free_financial_sources import FreeFinancialSources
from strategy.live_data_guard import (
    LiveDataError,
    strict_live,
    parse_buying_power,
    parse_current_price,
    parse_exchange_rate,
    parse_holdings_risk_snapshot,
    parse_positions,
    parse_sellable_quantity,
    validate_sellable_quantity,
    validate_order_response,
    parse_order_status,
    parse_stock_tradability,
    is_market_open_from_clock,
    parse_orderbook,
    evaluate_market_orderbook,
)
from strategy.forecast_evaluation import ForecastLedger, ForecastRecord
from strategy.lead_lag import generate_candidates
from strategy.research_packet import (
    build_research_packet,
    build_research_universe,
    compute_market_features,
)
from strategy.signal_fusion import fusion_from_agent_reports
from strategy.order_lifecycle import OrderLifecycleLedger
from strategy.agentic.audit_store import utc_now


def now_iso() -> str:
    # Audit and broker lifecycle records must be comparable across a local
    # dashboard, Toss responses, and later reconciliation runs.
    return utc_now()


class OrderSubmissionUnknown(LiveDataError):
    """A broker POST may have reached Toss, but cannot be safely classified.

    The caller must halt duplicate submissions and reconcile the order list
    before it is allowed to create another order for the symbol.
    """

    def __init__(self, client_order_id: str, reason: str, *, lifecycle_id: str | None = None):
        super().__init__(f"[LIVE BLOCKED] order submission needs reconciliation ({client_order_id}): {reason}")
        self.client_order_id = client_order_id
        self.reason = reason
        self.lifecycle_id = lifecycle_id


def make_toss_client(settings: Settings) -> TossClient:
    """Create the one broker client configuration used by live and preflight paths."""

    return TossClient(
        base_url=settings.toss_base_url,
        access_token=None,
        default_account_seq=settings.toss_account_seq,
        dry_run=settings.dry_run,
        auto_account=settings.toss_auto_account,
        rate_limit_safety_factor=settings.toss_rate_limit_safety_factor,
        max_get_retries=settings.toss_max_get_retries,
        retry_backoff_max_seconds=settings.toss_retry_backoff_max_seconds,
    )


def persist_unresolved_order_submission(
    state: dict,
    *,
    state_path: str,
    error: OrderSubmissionUnknown,
    symbol: str,
    event_id: str,
    order_spec: dict,
    lifecycle_id: str | None = None,
    ledger: OrderLifecycleLedger | None = None,
) -> dict:
    """Persist an ambiguous POST before the loop can attempt another order."""

    record = {
        "lifecycle_id": lifecycle_id or error.lifecycle_id,
        "client_order_id": error.client_order_id,
        "symbol": symbol,
        "event_id": event_id,
        "recorded_at_utc": now_iso(),
        "submission_state": "unknown",
        "reason": error.reason,
        "order_spec": dict(order_spec),
    }
    state.setdefault("unresolved_order_submissions", {})[error.client_order_id] = record
    # Write before returning to the surrounding loop: an ambiguous broker POST
    # must survive a later crash as a duplicate-order guard.
    save_state(state_path, state)
    if ledger and record["lifecycle_id"]:
        ledger.record(
            "submission_unknown_guarded",
            record["lifecycle_id"],
            client_order_id=error.client_order_id,
            symbol=symbol,
            event_id=event_id,
            reason=error.reason,
            order_spec=dict(order_spec),
        )
    return record


def persist_pending_live_order(
    state: dict,
    *,
    state_path: str,
    execution: dict,
    symbol: str,
    event_id: str,
    order_spec: dict,
    lifecycle_id: str,
    ledger: OrderLifecycleLedger | None = None,
) -> dict:
    """Durably retain an accepted but non-terminal order for reconciliation."""

    client_order_id = str(execution.get("client_order_id") or "")
    order_id = execution.get("order_id")
    if not client_order_id or not order_id:
        raise LiveDataError("cannot persist a pending live order without client/order ID")

    status = execution.get("order_status") if isinstance(execution.get("order_status"), dict) else {}
    record = {
        "lifecycle_id": lifecycle_id,
        "client_order_id": client_order_id,
        "order_id": str(order_id),
        "symbol": symbol,
        "event_id": event_id,
        "recorded_at_utc": now_iso(),
        "submission_state": "accepted_pending_reconciliation",
        "reason": (
            "order_status_lookup_failed"
            if execution.get("reconciliation_pending")
            else "broker_order_not_terminal"
        ),
        "order_spec": dict(order_spec),
        "last_status": {
            "status": status.get("_status"),
            "filled_quantity": status.get("_filled_quantity"),
            "remaining_quantity": status.get("_remaining_quantity"),
            "average_fill_price": status.get("_avg_filled_price"),
        },
    }
    state.setdefault("pending_live_orders", {})[client_order_id] = record
    save_state(state_path, state)
    if ledger:
        ledger.record(
            "reconciliation_pending",
            lifecycle_id,
            client_order_id=client_order_id,
            broker_order_id=str(order_id),
            symbol=symbol,
            event_id=event_id,
            reason=record["reason"],
            order_spec=dict(order_spec),
            last_status=record["last_status"],
        )
    return record


def research_indicator_settings(settings: Settings) -> dict:
    """Expose every code-calculated indicator convention in the audit packet."""
    return {
        "rsi_period": settings.research_rsi_period,
        "sma_short_period": settings.research_sma_short_period,
        "sma_long_period": settings.research_sma_long_period,
        "macd_fast_period": settings.research_macd_fast_period,
        "macd_slow_period": settings.research_macd_slow_period,
        "macd_signal_period": settings.research_macd_signal_period,
        "vwma_period": settings.research_vwma_period,
        "cci_period": settings.research_cci_period,
        "adx_period": settings.research_adx_period,
    }


def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {
            "lmsr": {},
            "positions": {},
            "sim_positions": {},
            "api_error_count": 0,
            "daily_pnl_pct": 0.0,
            "total_drawdown_pct": 0.0,
            "last_date": dt.date.today().isoformat(),
            "pending_live_orders": {},
            "unresolved_order_submissions": {},
            "live_agent_decision_cache": {},
        }
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("sim_positions", {})
    data.setdefault("pending_live_orders", {})
    data.setdefault("unresolved_order_submissions", {})
    data.setdefault("live_agent_decision_cache", {})
    return data


def save_state(path: str, state: dict) -> None:
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def append_jsonl(path: str, row: dict) -> None:
    row = {"ts": now_iso(), **row}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def get_lmsr(state: dict, event) -> LMSRMarket:
    raw = state.setdefault("lmsr", {}).get(event.event_id)
    if raw:
        return LMSRMarket.from_dict(raw)
    return LMSRMarket(event_id=event.event_id, question=event.question, b=event.b)


def put_lmsr(state: dict, market: LMSRMarket) -> None:
    state.setdefault("lmsr", {})[market.event_id] = market.to_dict()


def pick_price(settings: Settings, toss: TossClient, symbol: str) -> tuple[float, dict]:
    """
    LIVE:
    - Toss prices 응답에서 현재가를 실제로 파싱해야 한다.
    - 실패/0원/fallback이면 주문 차단.

    DRY_RUN:
    - fallback 가격 허용.
    """
    try:
        price_data = toss.prices([symbol])
        if settings.dry_run:
            price = {
                "005930": 80000,
                "000660": 200000,
                "NVDA": 150,
                "MSTR": 160,
                "COIN": 250,
                "IBIT": 60,
                "TLT": 95,
                "QQQ": 520,
                "SMH": 280,
            }.get(symbol, 100.0)
            return float(price), {"source": "dry_run_fallback", "raw": price_data}

        price = parse_current_price(price_data, symbol)
        if price <= 0:
            raise LiveDataError(f"[LIVE BLOCKED] invalid current price for {symbol}")
        return float(price), {"source": "toss_prices_real", "raw": price_data}

    except Exception as e:
        if settings.dry_run:
            return 100.0, {"source": "dry_run_exception_fallback", "error": str(e)}
        raise LiveDataError(f"[LIVE BLOCKED] real current price required for {symbol}: {e}")


def fetch_prices_batched(settings: Settings, toss: TossClient, symbols: list[str]) -> tuple[dict[str, tuple[float, dict]], dict[str, str]]:
    """Fetch up to 200 Toss prices per request, retaining per-symbol failures.

    A broken/unsupported symbol must not abort all other candidates in a live
    scan.  Missing prices are recorded as explicit data skips and can never
    fall back to a synthetic live price.
    """

    values: dict[str, tuple[float, dict]] = {}
    errors: dict[str, str] = {}
    unique_symbols = list(dict.fromkeys(str(symbol) for symbol in symbols if str(symbol).strip()))
    if settings.dry_run:
        for symbol in unique_symbols:
            try:
                values[symbol] = pick_price(settings, toss, symbol)
            except Exception as exc:
                errors[symbol] = str(exc)
        return values, errors

    for start in range(0, len(unique_symbols), 200):
        chunk = unique_symbols[start : start + 200]
        try:
            raw = toss.prices(chunk)
        except Exception as exc:
            reason = f"batch price request failed: {exc}"
            errors.update({symbol: reason for symbol in chunk})
            continue
        for symbol in chunk:
            try:
                price = parse_current_price(raw, symbol)
                result_rows = raw.get("result", raw) if isinstance(raw, dict) else []
                item = next(
                    (
                        row for row in result_rows
                        if isinstance(row, dict)
                        and str(row.get("symbol") or row.get("stockCode") or row.get("ticker") or "") == symbol
                    ),
                    None,
                ) if isinstance(result_rows, list) else None
                values[symbol] = (
                    float(price),
                    {"source": "toss_prices_real_batch", "batch_size": len(chunk), "raw": {"result": [item]} if item else {}},
                )
            except Exception as exc:
                errors[symbol] = f"real current price required: {exc}"
    return values, errors


def parse_open_order_symbols(response: dict) -> set[str]:
    """Extract symbols from a Toss OPEN-orders response without assuming one envelope."""

    result = response.get("result", response) if isinstance(response, dict) else response
    if isinstance(result, dict):
        for key in ("orders", "items", "data", "content"):
            if isinstance(result.get(key), list):
                result = result[key]
                break
    if not isinstance(result, list):
        return set()
    symbols: set[str] = set()
    for row in result:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol") or row.get("stockCode") or row.get("ticker") or row.get("code")
        if symbol:
            symbols.add(str(symbol))
    return symbols


def live_price_for_order(settings: Settings, toss: TossClient, symbol: str, analyzed_price: float) -> tuple[float, dict]:
    """Re-read the broker price immediately before a live order is created."""

    current_price, price_meta = pick_price(settings, toss, symbol)
    if not settings.dry_run and analyzed_price > 0:
        move = abs(float(current_price) / float(analyzed_price) - 1.0)
        if move > float(getattr(settings, "max_live_price_move_pct", 0.02)):
            raise LiveDataError(
                f"[LIVE BLOCKED] price moved {move:.2%} since analysis for {symbol}; re-analysis required"
            )
    return current_price, price_meta


def validate_market_order_cost(settings: Settings, toss: TossClient, symbol: str, order_spec: dict) -> dict:
    """Fail closed if the just-read visible order book is unsafe for this order.

    This must run after the final price/sizing refresh and before
    ``create_order``.  It intentionally uses an uncached single-symbol
    request: historical candles and batch prices are not adequate evidence
    that a market order can be executed at a tolerable cost right now.
    """

    if settings.dry_run:
        return {"ok": True, "reason": "dry_run_skip_orderbook"}
    if str(order_spec.get("order_type") or "").upper() != "MARKET":
        return {"ok": True, "reason": "non_market_orderbook_not_required"}

    try:
        book = parse_orderbook(toss.orderbook(symbol))
        return evaluate_market_orderbook(
            book,
            side=str(order_spec.get("side") or ""),
            quantity=order_spec.get("quantity"),
            order_amount=order_spec.get("order_amount"),
            max_spread_bps=float(getattr(settings, "max_live_spread_bps", 100.0)),
            max_book_impact_bps=float(getattr(settings, "max_live_book_impact_bps", 50.0)),
            max_age_seconds=float(getattr(settings, "max_live_orderbook_age_seconds", 15.0)),
        )
    except Exception as exc:
        # A missing or malformed book must never be treated as a reason to
        # send a blind market order.  Preserve the error in the audit record.
        return {"ok": False, "reason": "orderbook_lookup_or_parse_failed", "detail": str(exc)}


def begin_order_lifecycle(
    ledger: OrderLifecycleLedger,
    *,
    runtime_run_id: str,
    event_id: str,
    symbol: str,
    symbol_info: dict,
    action: str,
    action_reason: str,
    research_packet_hash: str,
    llm_result: dict,
    order_spec: dict,
    orderability: dict,
    orderbook_check: dict,
    pretrade_price: float,
    gate_ok: bool,
    gate_reason: str,
    cash_krw: float,
    position: dict | None,
) -> str:
    """Append the immutable decision and risk stages before the broker POST."""

    lifecycle_id = uuid.uuid4().hex
    agentic = llm_result.get("agentic_analysis") if isinstance(llm_result.get("agentic_analysis"), dict) else {}
    ledger.record(
        "decision",
        lifecycle_id,
        runtime_run_id=runtime_run_id,
        agent_run_id=agentic.get("run_id"),
        model_pipeline=str(llm_result.get("model_pipeline") or "gemini_api2_live_event_research"),
        event_id=event_id,
        symbol=symbol,
        market=symbol_info.get("market"),
        action=action,
        action_reason=action_reason,
        research_packet_hash=research_packet_hash,
        agent_buy_allowed=bool(llm_result.get("buy_allowed", False)),
        agent_selected_symbols=list(llm_result.get("selected_symbols", []) or []),
    )
    ledger.record(
        "risk_checked",
        lifecycle_id,
        runtime_run_id=runtime_run_id,
        event_id=event_id,
        symbol=symbol,
        python_gate_ok=bool(gate_ok),
        python_gate_reason=gate_reason,
        orderability={"ok": orderability.get("ok"), "reason": orderability.get("reason")},
        orderbook_check=orderbook_check,
        order_spec=dict(order_spec),
        pretrade_price=float(pretrade_price),
        available_cash_krw=float(cash_krw),
        existing_position_quantity=float((position or {}).get("_quantity", 0.0) or 0.0),
    )
    return lifecycle_id


def live_llm_cache_key(event, candidate_symbols: list[str]) -> str:
    """Key a reusable research verdict to one theme and exact candidate set."""

    event_id = str(getattr(event, "event_id", "") or "")
    return event_id + "|" + ",".join(sorted(str(symbol) for symbol in candidate_symbols))


def _parse_utc_iso(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _llm_usage_snapshot(llm_pipeline) -> dict:
    snapshot = getattr(llm_pipeline, "usage_snapshot", None)
    if not callable(snapshot):
        return {}
    try:
        value = snapshot()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def skipped_live_llm_result(llm_pipeline, reason: str) -> dict:
    """Represent an intentional no-call decision without pretending it is AI output."""

    return {
        "yes_probability": 0.5,
        "confidence": 0.0,
        "news_score": 0.0,
        "buy_allowed": False,
        "size_multiplier": 0.0,
        "block_reason": reason,
        "selected_symbols": [],
        "selected_symbol_reason": "no_llm_call",
        "agentic_analysis": {"status": "skipped", "reason": reason},
        "google_evidence": {"status": "skipped", "reason": reason},
        "nvidia_judgement": {"status": "skipped", "reason": reason},
        "api_usage": _llm_usage_snapshot(llm_pipeline),
        "model_pipeline": "python_pre_screen",
    }


def get_cached_live_llm_result(settings: Settings, state: dict, event, candidate_symbols: list[str], llm_pipeline) -> dict | None:
    """Return only an unexpired, successful LLM research verdict.

    The original full report remains in the agent audit ledger.  This compact
    copy exists solely to keep a low-rate LLM from being called again every
    bot loop for the same daily-bar candidate set.
    """

    ttl_seconds = max(0, int(getattr(settings, "live_llm_decision_ttl_seconds", 0)))
    if ttl_seconds <= 0:
        return None
    cache = state.get("live_agent_decision_cache", {})
    if not isinstance(cache, dict):
        return None
    key = live_llm_cache_key(event, candidate_symbols)
    record = cache.get(key)
    if not isinstance(record, dict):
        return None
    cached_at = _parse_utc_iso(record.get("cached_at_utc"))
    compact = record.get("result")
    if cached_at is None or not isinstance(compact, dict):
        return None
    age_seconds = (dt.datetime.now(dt.timezone.utc) - cached_at).total_seconds()
    if age_seconds < 0 or age_seconds > ttl_seconds:
        return None
    source_run_id = str(record.get("source_agent_run_id") or "")
    return {
        **json.loads(json.dumps(compact, ensure_ascii=False)),
        "agentic_analysis": {
            "run_id": source_run_id or None,
            "status": "cached",
            "source_agent_run_id": source_run_id or None,
            "cached_at_utc": record.get("cached_at_utc"),
            "expires_at_utc": record.get("expires_at_utc"),
        },
        "google_evidence": {"status": "cached", "source_agent_run_id": source_run_id or None},
        "nvidia_judgement": {"status": "cached", "source_agent_run_id": source_run_id or None},
        "api_usage": _llm_usage_snapshot(llm_pipeline),
        "model_pipeline": "gemini_api2_live_event_research_cached",
        "live_agent_cache": {"hit": True, "age_seconds": round(age_seconds, 1)},
    }


def cache_live_llm_result(settings: Settings, state: dict, event, candidate_symbols: list[str], llm_result: dict) -> bool:
    """Cache only a contract-valid live report and retain its source run ID."""

    ttl_seconds = max(0, int(getattr(settings, "live_llm_decision_ttl_seconds", 0)))
    agentic = llm_result.get("agentic_analysis") if isinstance(llm_result.get("agentic_analysis"), dict) else {}
    if ttl_seconds <= 0 or agentic.get("status") != "ok":
        return False
    source_run_id = str(agentic.get("run_id") or "")
    if not source_run_id:
        return False
    compact_keys = (
        "yes_probability",
        "confidence",
        "news_score",
        "buy_allowed",
        "size_multiplier",
        "block_reason",
        "predicted_return_bin",
        "reasoning_signals",
        "selected_symbols",
        "selected_symbol_reason",
    )
    compact = {key: llm_result.get(key) for key in compact_keys}
    cached_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    expires_at = cached_at + dt.timedelta(seconds=ttl_seconds)
    cache = state.setdefault("live_agent_decision_cache", {})
    if not isinstance(cache, dict):
        state["live_agent_decision_cache"] = {}
        cache = state["live_agent_decision_cache"]
    cache[live_llm_cache_key(event, candidate_symbols)] = {
        "cached_at_utc": cached_at.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": expires_at.isoformat().replace("+00:00", "Z"),
        "source_agent_run_id": source_run_id,
        "candidate_symbols": sorted(str(symbol) for symbol in candidate_symbols),
        "result": compact,
    }
    return True


def pick_ohlcv(settings: Settings, toss: TossClient, symbol: str):
    """
    LIVE:
    - technical_entries/SDE에 쓰는 캔들은 실제 candles가 필요하다.
    - BLOCK_LIVE_ON_OHLCV_FALLBACK=true이면 mock OHLCV로 주문하지 않는다.
    """
    if settings.dry_run:
        return make_mock_ohlcv(getattr(settings, "research_ohlcv_bars", 260)), {"source": "dry_run_mock_ohlcv"}

    try:
        requested_count = int(getattr(settings, "research_ohlcv_bars", 200))
        candle_count = max(60, requested_count)
        raw, page_meta = fetch_candles_paginated(
            toss,
            symbol,
            interval="1d",
            total_count=candle_count,
            page_size=200,
            cache_dir=getattr(settings, "toss_candle_cache_dir", None),
            cache_ttl_seconds=float(getattr(settings, "toss_candle_cache_ttl_seconds", 0)),
        )
        df = candles_response_to_ohlcv(raw)
        if len(df) >= 60:
            return df, {
                "source": "toss_candles_cached_real" if page_meta.get("cache_hit") else "toss_candles_real",
                **page_meta,
                "candle_count": len(df),
                "raw": raw,
            }

        if settings.block_live_on_ohlcv_fallback:
            raise LiveDataError(f"[LIVE BLOCKED] insufficient real candles for {symbol}: {len(df)} rows")

        return make_mock_ohlcv(getattr(settings, "research_ohlcv_bars", 260)), {"source": "live_mock_fallback_allowed", "raw": raw}

    except Exception as e:
        if settings.block_live_on_ohlcv_fallback:
            raise LiveDataError(f"[LIVE BLOCKED] real OHLCV required for {symbol}: {e}")
        return make_mock_ohlcv(getattr(settings, "research_ohlcv_bars", 260)), {"source": "live_mock_fallback_allowed_due_to_error", "error": str(e)}


def execute_order(
    settings: Settings,
    toss: TossClient,
    state: dict,
    order_spec: dict,
    event_id: str,
    symbol: str,
    price: float,
    *,
    lifecycle_id: str | None = None,
    ledger: OrderLifecycleLedger | None = None,
):
    """Submit one idempotent order and preserve its broker lifecycle.

    ``submitted`` means Toss accepted the POST; it never means that the
    exchange filled the order.  An unknown response or unavailable status is
    deliberately retained for reconciliation instead of inviting a retry.
    """

    if not order_spec:
        return {"executed": False, "reason": "empty_order_spec"}

    lifecycle_id = lifecycle_id or uuid.uuid4().hex
    client_order_id = f"bot-{lifecycle_id}"[:36]
    lifecycle_fields = {
        "client_order_id": client_order_id,
        "symbol": symbol,
        "event_id": event_id,
        "side": order_spec.get("side"),
        "order_type": order_spec.get("order_type"),
        "requested_quantity": order_spec.get("quantity"),
        "requested_order_amount": order_spec.get("order_amount"),
        "notional_krw": order_spec.get("notional_krw"),
    }
    if ledger:
        # This is before the broker side effect.  A durable-audit failure is
        # a safe reason not to create an unattended live order.
        try:
            ledger.record("submission_attempt", lifecycle_id, **lifecycle_fields)
        except Exception as exc:
            raise LiveDataError(f"[LIVE BLOCKED] order lifecycle audit unavailable: {exc}") from exc

    def record_after_post(event_type: str, **fields):
        if not ledger:
            return
        try:
            ledger.record(event_type, lifecycle_id, **lifecycle_fields, **fields)
        except Exception as exc:
            # Once a POST may have reached Toss, an audit write failure must
            # be treated as reconciliation-required, never as a retryable
            # local logging issue.
            raise OrderSubmissionUnknown(
                client_order_id,
                f"order lifecycle audit write failed: {exc}",
                lifecycle_id=lifecycle_id,
            ) from exc

    try:
        execution = toss.create_order(
            account_seq=settings.toss_account_seq,
            symbol=symbol,
            side=order_spec["side"],
            order_type=order_spec["order_type"],
            quantity=order_spec.get("quantity"),
            order_amount=order_spec.get("order_amount"),
            client_order_id=client_order_id,
        )
    except TossAPIError as exc:
        # A 5xx/409 response can happen after the broker received the POST.
        # Keep the submission id and reconcile instead of creating a new
        # order on a later loop.
        if exc.status_code in {409, 500, 502, 503, 504}:
            if ledger:
                try:
                    ledger.record("submission_unknown", lifecycle_id, **lifecycle_fields, reason=str(exc))
                except Exception:
                    pass
            raise OrderSubmissionUnknown(client_order_id, str(exc), lifecycle_id=lifecycle_id) from exc
        raise
    except Exception as exc:
        # A transport timeout is similarly ambiguous after a side-effecting
        # broker POST, so it must never become an automatic retry.
        if ledger:
            try:
                ledger.record("submission_unknown", lifecycle_id, **lifecycle_fields, reason=str(exc))
            except Exception:
                pass
        raise OrderSubmissionUnknown(client_order_id, str(exc), lifecycle_id=lifecycle_id) from exc

    if settings.dry_run and execution.get("dry_run"):
        if order_spec["side"] == "BUY":
            qty = float(order_spec.get("quantity") or 0.0)
            if qty <= 0 and order_spec.get("order_amount"):
                # US amount order dry-run: 대략 수량 환산
                qty = float(order_spec["notional_krw"]) / max(float(price) * settings.usd_krw_fx_rate, 1.0)
            apply_dry_run_fill(
                state,
                symbol=symbol,
                side="BUY",
                price=float(price),
                quantity=qty,
                order_value=float(order_spec.get("notional_krw", 0.0)),
            )

        if order_spec["side"] == "SELL":
            qty = float(order_spec.get("quantity") or 0.0)
            apply_dry_run_fill(
                state,
                symbol=symbol,
                side="SELL",
                price=float(price),
                quantity=qty,
                order_value=float(order_spec.get("notional_krw", 0.0)),
            )

        record_after_post("submitted", simulated=True)
        record_after_post("fill_observed", simulated=True, filled_quantity=qty, average_fill_price=float(price))
        record_after_post("terminal", simulated=True, terminal_status="FILLED")
        return {
            **execution,
            "submitted": True,
            "executed": True,
            "terminal": True,
            "live": False,
            "lifecycle_id": lifecycle_id,
            "client_order_id": client_order_id,
        }

    # LIVE: an absent or malformed order ID is ambiguous after the POST.
    try:
        validate_order_response(settings, execution)
        result = execution.get("result", execution)
        order_id = result.get("orderId") or result.get("id") or result.get("orderNo") or result.get("orderNumber")
        if not order_id:
            raise LiveDataError("[LIVE BLOCKED] orderId missing in accepted broker response")
    except Exception as exc:
        if ledger:
            try:
                ledger.record("submission_unknown", lifecycle_id, **lifecycle_fields, reason=str(exc))
            except Exception:
                pass
        raise OrderSubmissionUnknown(client_order_id, str(exc), lifecycle_id=lifecycle_id) from exc

    record_after_post("submitted", broker_order_id=str(order_id), broker_response_received=True)

    if settings.require_order_status_after_live_order:
        try:
            status_raw = toss.order_status(str(order_id), account_seq=settings.toss_account_seq)
            status = parse_order_status(status_raw)
        except Exception as exc:
            # The POST has already been accepted.  The caller persists a
            # pending broker-order record and blocks new orders until this ID
            # is reconciled in a later read-only status query.
            record_after_post("status_lookup_failed", broker_order_id=str(order_id), reason=str(exc))
            return {
                "order_response": execution,
                "order_id": order_id,
                "client_order_id": client_order_id,
                "lifecycle_id": lifecycle_id,
                "submitted": True,
                "executed": False,
                "terminal": False,
                "reconciliation_pending": True,
                "order_status_error": str(exc),
                "live": True,
            }

        filled_quantity = float(status.get("_filled_quantity", 0.0) or 0.0)
        average_fill_price = float(status.get("_avg_filled_price", 0.0) or 0.0)
        rejected = bool(status.get("_is_rejected"))
        canceled = bool(status.get("_is_canceled"))
        terminal = bool(status.get("_is_filled") or rejected or canceled)
        status_summary = {
            "status": status.get("_status"),
            "filled_quantity": filled_quantity,
            "remaining_quantity": float(status.get("_remaining_quantity", 0.0) or 0.0),
            "average_fill_price": average_fill_price,
            "rejected": rejected,
            "canceled": canceled,
            "terminal": terminal,
        }
        record_after_post("status_observed", broker_order_id=str(order_id), **status_summary)
        if filled_quantity > 0:
            record_after_post(
                "fill_observed",
                broker_order_id=str(order_id),
                filled_quantity=filled_quantity,
                average_fill_price=average_fill_price,
                cumulative=True,
            )
        if terminal:
            record_after_post(
                "terminal",
                broker_order_id=str(order_id),
                terminal_status=status.get("_status"),
                rejected=rejected,
                canceled=canceled,
            )
        return {
            "order_response": execution,
            "order_status": status,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "lifecycle_id": lifecycle_id,
            # A broker accepting an order and the exchange filling it are
            # intentionally separate states in the immutable trade record.
            "submitted": True,
            "executed": filled_quantity > 0,
            "terminal": terminal,
            "rejected": rejected,
            "canceled": canceled,
            "filled_quantity": filled_quantity,
            "average_fill_price": average_fill_price,
            "live": True,
        }

    # No status read means a fill cannot be claimed and the accepted order
    # must remain pending until a future reconciliation succeeds.
    return {
        "order_response": execution,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "lifecycle_id": lifecycle_id,
        "submitted": True,
        "executed": False,
        "terminal": False,
        "reconciliation_pending": True,
        "order_status_error": "post_order_status_check_disabled",
        "live": True,
    }


def reconcile_pending_live_orders(
    settings: Settings,
    toss: TossClient,
    state: dict,
    ledger: OrderLifecycleLedger | None = None,
) -> dict:
    """Read broker status for each accepted-but-nonterminal bot order.

    The function is deliberately read-only.  A pending order is cleared only
    after Toss reports a terminal lifecycle state; partial/open orders remain
    a global live-order guard rather than being inferred from an open-orders
    symbol list.
    """

    pending = state.get("pending_live_orders", {})
    if not isinstance(pending, dict) or not pending:
        return {"remaining": 0, "reconciled": [], "errors": []}

    reconciled: list[str] = []
    errors: list[dict] = []
    changed = False
    for client_order_id, record in list(pending.items()):
        if not isinstance(record, dict):
            errors.append({"client_order_id": str(client_order_id), "reason": "invalid_pending_record"})
            continue
        lifecycle_id = str(record.get("lifecycle_id") or uuid.uuid4().hex)
        record["lifecycle_id"] = lifecycle_id
        order_id = record.get("order_id")
        if not order_id:
            errors.append({"client_order_id": str(client_order_id), "reason": "pending_order_id_missing"})
            if ledger:
                ledger.record(
                    "reconciliation_failed",
                    lifecycle_id,
                    client_order_id=str(client_order_id),
                    reason="pending_order_id_missing",
                )
            continue
        try:
            status = parse_order_status(toss.order_status(str(order_id), account_seq=settings.toss_account_seq))
        except Exception as exc:
            errors.append({"client_order_id": str(client_order_id), "reason": str(exc)})
            if ledger:
                ledger.record(
                    "status_lookup_failed",
                    lifecycle_id,
                    client_order_id=str(client_order_id),
                    broker_order_id=str(order_id),
                    reconciliation=True,
                    reason=str(exc),
                )
            continue

        filled_quantity = float(status.get("_filled_quantity", 0.0) or 0.0)
        average_fill_price = float(status.get("_avg_filled_price", 0.0) or 0.0)
        rejected = bool(status.get("_is_rejected"))
        canceled = bool(status.get("_is_canceled"))
        terminal = bool(status.get("_is_filled") or rejected or canceled)
        status_summary = {
            "status": status.get("_status"),
            "filled_quantity": filled_quantity,
            "remaining_quantity": float(status.get("_remaining_quantity", 0.0) or 0.0),
            "average_fill_price": average_fill_price,
            "rejected": rejected,
            "canceled": canceled,
            "terminal": terminal,
        }
        if ledger:
            ledger.record(
                "status_observed",
                lifecycle_id,
                client_order_id=str(client_order_id),
                broker_order_id=str(order_id),
                reconciliation=True,
                **status_summary,
            )
            if filled_quantity > 0:
                ledger.record(
                    "fill_observed",
                    lifecycle_id,
                    client_order_id=str(client_order_id),
                    broker_order_id=str(order_id),
                    reconciliation=True,
                    filled_quantity=filled_quantity,
                    average_fill_price=average_fill_price,
                    cumulative=True,
                )

        if terminal:
            pending.pop(client_order_id, None)
            reconciled.append(str(client_order_id))
            changed = True
            if ledger:
                ledger.record(
                    "terminal",
                    lifecycle_id,
                    client_order_id=str(client_order_id),
                    broker_order_id=str(order_id),
                    reconciliation=True,
                    terminal_status=status.get("_status"),
                    rejected=rejected,
                    canceled=canceled,
                )
        else:
            record["last_reconciled_at_utc"] = now_iso()
            record["last_status"] = status_summary
            changed = True

    if changed:
        save_state(settings.state_path, state)
    return {"remaining": len(pending), "reconciled": reconciled, "errors": errors}


def validate_orderability(settings: Settings, toss: TossClient, symbol: str, symbol_info: dict) -> dict:
    """
    LIVE 주문 전:
    - 종목 거래 가능 상태
    - 시장 open 여부
    를 실제 API로 확인한다.

    Endpoint는 Toss 최신 문서에 맞게 .env에서 바꿀 수 있다.
    """
    if settings.dry_run:
        return {"ok": True, "reason": "dry_run_skip_orderability"}

    checks = {}

    if settings.enable_stock_tradability_check:
        try:
            stock_raw = toss.stock_info(symbol)
            tradability = parse_stock_tradability(stock_raw, symbol)
            checks["stock_tradability"] = tradability
            if tradability.get("tradable") is not True:
                return {"ok": False, "reason": "stock_not_tradable", "checks": checks}
        except Exception as e:
            if strict_live(settings):
                return {"ok": False, "reason": f"stock_tradability_check_failed: {e}", "checks": checks}
            checks["stock_tradability_error"] = str(e)

    if settings.enable_market_status_check:
        try:
            market_raw = toss.market_status(symbol_info.get("market"))
            market_open = is_market_open_from_clock(market_raw)
            checks["market_status"] = market_open
            if market_open.get("is_open") is not True:
                return {"ok": False, "reason": "market_closed_or_unconfirmed", "checks": checks}
        except Exception as e:
            if strict_live(settings):
                return {"ok": False, "reason": f"market_status_check_failed: {e}", "checks": checks}
            checks["market_status_error"] = str(e)

    return {"ok": True, "reason": "orderability_ok", "checks": checks}


def run_once(settings: Settings) -> None:
    state = load_state(settings.state_path)
    runtime_run_id = uuid.uuid4().hex
    forecast_ledger = ForecastLedger(settings.forecast_ledger_path)

    if not settings.dry_run and state.get("unresolved_order_submissions"):
        unresolved = state["unresolved_order_submissions"]
        raise LiveDataError(
            "[LIVE BLOCKED] unresolved broker POST exists; reconcile it in Toss before new orders: "
            + ", ".join(str(key) for key in unresolved)
        )

    toss = make_toss_client(settings)
    order_ledger = OrderLifecycleLedger(settings.order_lifecycle_ledger_path)

    # Known broker order IDs are reconciled by read-only status calls before
    # any new signal work.  An accepted market order that is still open or
    # whose status is temporarily unavailable blocks fresh orders globally.
    if not settings.dry_run:
        reconciliation = reconcile_pending_live_orders(settings, toss, state, order_ledger)
        if reconciliation["remaining"]:
            raise LiveDataError(
                "[LIVE BLOCKED] accepted broker order still needs reconciliation before new orders: "
                + ", ".join(str(item) for item in state.get("pending_live_orders", {}))
            )
        if reconciliation["reconciled"]:
            print("[broker] reconciled terminal orders: " + ", ".join(reconciliation["reconciled"]))

    # The Gemini_Api2 team is opt-in and fail-closed.  Keeping the legacy
    # dual-provider pipeline as the default prevents a key in .env from
    # changing live behaviour until the new workflow has been dry-run tested.
    if settings.enable_gemini_api2:
        llm_pipeline = GeminiAgentOrchestrator(settings)
        print("[llm] Gemini_Api2 multi-agent workflow enabled")
    else:
        llm_pipeline = DualProviderLLMPipeline(settings)
        print("[llm] legacy dual-provider workflow enabled")
    financial_sources = FreeFinancialSources(settings)
    sapience = SapienceClient(settings)
    naver_search = NaverSearchClient(settings)

    # Broker preflight is deliberately read-only.  It proves that balance,
    # positions, currency, and outstanding-order data can all be reconciled
    # before the old API-error circuit breaker is released.
    if settings.dry_run:
        krw_buying_power = 300000.0
        usd_buying_power = 300000.0 / max(settings.usd_krw_fx_rate, 1.0)
        live_fx = float(settings.usd_krw_fx_rate)
        positions_map = get_positions_map(toss, settings, state)
        portfolio_risk_snapshot = {
            "market_value_krw": sum(
                float(position.get("_market_value", 0.0) or 0.0) for position in positions_map.values()
            ),
            "daily_pnl_pct": float(state.get("daily_pnl_pct", 0.0) or 0.0),
        }
        pending_order_symbols: set[str] = set()
        commission_snapshot: dict = {"dry_run": True}
    else:
        # 매수가능금액 파싱 실패가 루프 전체를 죽이면 exit/청산 로직까지 함께
        # 멈춘다(2026-07-15~16 야간 "cannot parse buying power" 69회 연속 중단).
        # 파싱 실패 통화는 0원으로 두어 "그 통화 신규매수만 불가" 상태로
        # 강등하고, 루프(포지션 감시·exit)는 계속 돌게 한다. fail-closed.
        krw_buying = toss.buying_power(account_seq=settings.toss_account_seq, currency="KRW")
        try:
            krw_buying_power = parse_buying_power(krw_buying)
        except LiveDataError as exc:
            krw_buying_power = 0.0
            append_jsonl(settings.log_path, {
                "action": "BUYING_POWER_UNAVAILABLE",
                "currency": "KRW",
                "error": str(exc),
                "raw_keys": sorted(krw_buying.keys()) if isinstance(krw_buying, dict) else str(type(krw_buying)),
            })
        try:
            usd_buying = toss.buying_power(account_seq=settings.toss_account_seq, currency="USD")
            usd_buying_power = parse_buying_power(usd_buying)
        except (LiveDataError, TossAPIError) as exc:
            usd_buying_power = 0.0
            append_jsonl(settings.log_path, {
                "action": "BUYING_POWER_UNAVAILABLE",
                "currency": "USD",
                "error": str(exc),
            })
        fx_raw = toss.exchange_rate("USD", "KRW")
        live_fx = parse_exchange_rate(fx_raw)
        if krw_buying_power < 0 or usd_buying_power < 0:
            raise LiveDataError("[LIVE BLOCKED] buying power is invalid")
        settings.usd_krw_fx_rate = live_fx
        holdings_raw = toss.positions(account_seq=settings.toss_account_seq)
        positions_map = parse_positions(holdings_raw)
        portfolio_risk_snapshot = parse_holdings_risk_snapshot(holdings_raw, live_fx)
        open_orders_raw = toss.orders(account_seq=settings.toss_account_seq, status="OPEN")
        pending_order_symbols = parse_open_order_symbols(open_orders_raw)
        commission_snapshot = toss.commissions(account_seq=settings.toss_account_seq)
        # Recovery is intentional: historic 429s must not keep blocking every
        # new loop after a complete, real broker preflight succeeds.
        state["api_error_count"] = 0
        state["last_broker_preflight_utc"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    cash = krw_buying_power + usd_buying_power * live_fx
    if not settings.dry_run:
        equity_krw = cash + float(portfolio_risk_snapshot["market_value_krw"])
        previous_peak = float(state.get("portfolio_peak_equity_krw", 0.0) or 0.0)
        peak_equity = max(previous_peak, equity_krw)
        state["portfolio_peak_equity_krw"] = peak_equity
        state["portfolio_equity_krw"] = equity_krw
        state["daily_pnl_pct"] = float(portfolio_risk_snapshot["daily_pnl_pct"])
        state["total_drawdown_pct"] = (equity_krw / peak_equity - 1.0) if peak_equity > 0 else 0.0
        state["last_portfolio_reconciliation_utc"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    effective_bankroll = min(cash, settings.max_managed_bankroll_krw)
    allocation_budget = buy_budget_krw(settings, effective_bankroll, positions_map)
    deployable_bankroll = allocation_budget["run_budget_krw"]
    new_buys_this_loop = 0
    deployed_this_loop = 0.0
    deployed_by_market = {"KR": 0.0, "US": 0.0}
    buying_power_after_reserve = {
        "KR": max(0.0, krw_buying_power * (1.0 - settings.cash_reserve_fraction)),
        "US": max(0.0, usd_buying_power * live_fx * (1.0 - settings.cash_reserve_fraction)),
    }

    print("=" * 120)
    print(
        f"[{now_iso()}] DRY_RUN={settings.dry_run} "
        f"cash={cash:,.0f} (KRW={krw_buying_power:,.0f}, USD={usd_buying_power:,.2f}) "
        f"effective={effective_bankroll:,.0f} "
        f"deployable={deployable_bankroll:,.0f} "
        f"exposure={allocation_budget['current_exposure_krw']:,.0f}/"
        f"{settings.auto_buy_total_cap_krw:,.0f} positions={len(positions_map)}"
    )

    for event in load_events(rotate_questions=settings.rotate_event_questions):
        if settings.max_symbols_per_event > 0 and len(event.mapped_symbols) > settings.max_symbols_per_event:
            event.mapped_symbols = event.mapped_symbols[:settings.max_symbols_per_event]

        print(f"[event] {event.event_id} | theme={event.theme} | symbols={len(event.mapped_symbols)} | q={event.question}")

        lmsr = get_lmsr(state, event)
        lmsr_price_before = lmsr.yes_price()

        # Build the immutable research input *before* the Gemini team runs.
        # The same price/OHLCV/fundamental snapshots are then reused by Python
        # entry/exit rules below.  This replaces the old random mock-universe
        # baseline with real values where available and explicit unknowns where
        # they are not available.
        event_started_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        indicator_settings = research_indicator_settings(settings)
        event_symbols = list(event.mapped_symbols)
        symbol_info_map = {symbol: describe_symbol(symbol) for symbol in event_symbols}
        fundamentals_map = {}
        factor_inputs_map = {}
        price_cache, price_errors = fetch_prices_batched(settings, toss, event_symbols)
        ohlcv_cache = {}
        technical_cache = {}
        market_features_map = {}
        research_packets = {}

        for symbol in event_symbols:
            symbol_info = symbol_info_map[symbol]
            if symbol not in price_cache:
                append_jsonl(settings.log_path, {
                    "runtime_run_id": runtime_run_id,
                    "decision_time_utc": event_started_utc,
                    "event_id": event.event_id,
                    "symbol": symbol,
                    "action": "DATA_SKIPPED",
                    "data_error": price_errors.get(symbol, "real current price unavailable"),
                    "execution": {"executed": False, "submitted": False, "reason": "market_data_price_unavailable"},
                })
                continue
            price, price_meta = price_cache[symbol]
            try:
                ohlcv, ohlcv_meta = pick_ohlcv(settings, toss, symbol)
            except Exception as exc:
                append_jsonl(settings.log_path, {
                    "runtime_run_id": runtime_run_id,
                    "decision_time_utc": event_started_utc,
                    "event_id": event.event_id,
                    "symbol": symbol,
                    "action": "DATA_SKIPPED",
                    "data_error": str(exc),
                    "execution": {"executed": False, "submitted": False, "reason": "market_data_ohlcv_unavailable"},
                })
                continue
            technical = compute_technical_rules(ohlcv, settings)
            fundamentals = financial_sources.build(symbol, symbol_info) if settings.enable_free_financial_data else {}
            factor_inputs = financial_sources.to_factor_overrides(fundamentals) if fundamentals else {}
            market_features = compute_market_features(ohlcv)

            price_cache[symbol] = (price, price_meta)
            ohlcv_cache[symbol] = (ohlcv, ohlcv_meta)
            technical_cache[symbol] = technical
            fundamentals_map[symbol] = fundamentals
            factor_inputs_map[symbol] = factor_inputs
            market_features_map[symbol] = market_features
            research_packets[symbol] = build_research_packet(
                symbol=symbol,
                symbol_info=symbol_info,
                price=price,
                price_meta=price_meta,
                ohlcv=ohlcv,
                ohlcv_meta=ohlcv_meta,
                fundamentals=fundamentals,
                technical_rules=technical,
                # Record the time this concrete packet was built, after its
                # price/candle/fundamental reads completed; never pre-date a
                # data snapshot as though it had already been observed.
                observed_at_utc=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                indicator_settings=indicator_settings,
            )

        active_symbols = list(research_packets)
        if not active_symbols:
            print(f"[event] {event.event_id} skipped: no candidates with real price and OHLCV")
            continue

        # This is the event-wide decision cutoff.  Every packet source was
        # retrieved at or before this point; the LLM must echo it in its
        # structured report, allowing the live contract to reject stale or
        # mismatched analysis.
        observation_time_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        # The 10 risk-filter method is deliberately opt-in and research-only:
        # it offers statistically generated candidates to the semantic risk
        # agent but never turns one directly into a broker order.
        lead_lag_candidates = []
        lead_lag_status = "disabled"
        if settings.enable_lead_lag_research:
            try:
                price_series = {
                    symbol: ohlcv_cache[symbol][0]["close"].astype(float).tolist()
                    for symbol in active_symbols[: max(1, settings.lead_lag_max_symbols)]
                    if symbol in ohlcv_cache and "close" in ohlcv_cache[symbol][0]
                }
                lead_lag_candidates = [
                    candidate.to_dict()
                    for candidate in generate_candidates(
                        price_series,
                        max_lag=settings.lead_lag_max_lag,
                        fdr_alpha=settings.lead_lag_fdr_alpha,
                        minimum_observations=settings.lead_lag_minimum_observations,
                    )[: settings.lead_lag_max_candidates]
                ]
                lead_lag_status = "ok"
            except Exception as exc:
                lead_lag_status = f"unavailable: {exc}"

        # Rank a bounded, data-ready candidate set before asking the low-rate
        # LLM team for a single symbol decision.  The selection is deterministic
        # and auditable; LLM output cannot expand the universe on its own.
        preliminary_universe = compute_factor_scores(build_research_universe(
            active_symbols,
            fundamentals_by_symbol=factor_inputs_map,
            market_features_by_symbol=market_features_map,
            event_sentiment=0.0,
        ))
        preliminary_scores = {
            str(row["symbol"]): float(row["factor_score"]) + 0.50 * float(technical_cache[str(row["symbol"])]["trend_score"])
            for _, row in preliminary_universe.iterrows()
        }
        ranked_llm_symbols = sorted(
            active_symbols,
            key=lambda symbol: (preliminary_scores.get(symbol, -float("inf")), symbol),
            reverse=True,
        )[: max(1, int(getattr(settings, "live_llm_candidate_limit", 20)))]
        # The LLM is a bounded research reviewer, not a polling dependency.
        # If the Python entry rules cannot nominate any *new* buy candidate,
        # do not spend a scarce LLM request merely to produce another HOLD.
        llm_symbols = [
            symbol
            for symbol in ranked_llm_symbols
            if symbol not in positions_map
            and (
                not settings.use_technical_entry_rules
                or bool(technical_cache[symbol].get("buy_signal"))
            )
        ]
        symbol_infos_for_llm = [symbol_info_map[symbol] for symbol in llm_symbols]
        # 네이버 뉴스(사실)·블로그(주관) evidence — LLM 판단 재료로만 사용되며
        # 직접 주문을 생성하지 않는다. 실패해도 증거 없음으로만 작용한다.
        try:
            naver_evidence = naver_search.build_event_evidence(event, symbol_infos_for_llm)
        except Exception as exc:
            naver_evidence = {"enabled": False, "error": str(exc)}
        research_context = {
            "symbols": {symbol: research_packets[symbol] for symbol in llm_symbols},
            "naver_news": naver_evidence,
            "lead_lag": {
                "status": lead_lag_status,
                "research_only": True,
                "preprocessing": "log_returns",
                "candidates": lead_lag_candidates,
            },
        }
        if settings.enable_gemini_api2:
            llm_result = get_cached_live_llm_result(settings, state, event, llm_symbols, llm_pipeline)
            if llm_result is None and llm_symbols:
                llm_result = llm_pipeline.analyze_event(
                    event,
                    llm_symbols,
                    symbol_infos_for_llm,
                    research_context,
                )
                llm_result.setdefault("model_pipeline", "gemini_api2_live_event_research")
                cache_live_llm_result(settings, state, event, llm_symbols, llm_result)
            elif llm_result is None:
                llm_result = skipped_live_llm_result(
                    llm_pipeline,
                    "no_python_pre_screened_new_buy_candidate",
                )
        else:
            # Retain the legacy provider's historical behaviour when it is
            # explicitly configured; only the live Gemini workflow is
            # constrained by this project's low-rate research budget.
            legacy_symbols = ranked_llm_symbols
            llm_result = llm_pipeline.analyze_event(
                event,
                legacy_symbols,
                [symbol_info_map[symbol] for symbol in legacy_symbols],
                extra_evidence=naver_evidence,
            )
            llm_symbols = legacy_symbols

        llm_prob = float(np.clip(llm_result["yes_probability"], 0.01, 0.99))
        confidence = float(np.clip(llm_result["confidence"], 0.0, 1.0))
        event_news_score = float(np.clip(llm_result.get("news_score", 0.0), -1.0, 1.0))
        # Gemini agents can approve, reduce, or block a *new* buy.  They never
        # create an order; deterministic rules and exit logic remain in charge.
        agent_buy_allowed = bool(llm_result.get("buy_allowed", True))
        agent_size_multiplier = float(np.clip(llm_result.get("size_multiplier", 1.0), 0.0, 1.0))
        agent_block_reason = str(llm_result.get("block_reason", ""))
        agent_selected_symbols = {str(symbol) for symbol in llm_result.get("selected_symbols", []) if str(symbol) in llm_symbols}
        if not agent_selected_symbols:
            legacy_candidates = (llm_result.get("nvidia_judgement") or {}).get("top_candidate_symbols", [])
            for candidate in legacy_candidates if isinstance(legacy_candidates, list) else []:
                selected = str(candidate.get("symbol", "")) if isinstance(candidate, dict) else str(candidate)
                if selected in llm_symbols:
                    agent_selected_symbols.add(selected)
                    break

        gate_ok, gate_reason = survival_gate(settings, state, confidence)

        sapience_price = sapience.get_event_price(event.event_id)
        blended_price = lmsr_price_before if sapience_price is None else 0.7 * lmsr_price_before + 0.3 * float(sapience_price)

        event_edge = llm_prob - blended_price
        kelly_yes = prediction_market_kelly_yes(llm_prob, blended_price)
        kelly_no = prediction_market_kelly_no(llm_prob, blended_price)

        # alpha=0.40이면 LMSR 내부가격이 몇 루프 만에 LLM 확률로 수렴해
        # event_edge(=llm_prob - 내부가격)가 0으로 붕괴한다. 그 결과
        # MIN_EVENT_EDGE 게이트를 두 번 다시 통과할 수 없게 되어 지속적
        # 강세 판단이 매수로 이어지지 않는다. 수렴 속도는 설정으로 낮춘다.
        lmsr_update = lmsr.update_toward_probability(
            llm_prob, alpha=float(getattr(settings, "lmsr_update_alpha", 0.15))
        )
        put_lmsr(state, lmsr)

        universe = build_research_universe(
            active_symbols,
            fundamentals_by_symbol=factor_inputs_map,
            market_features_by_symbol=market_features_map,
            event_sentiment=event_news_score,
        )
        universe = compute_factor_scores(universe)

        for _, row in universe.iterrows():
            symbol = row["symbol"]
            symbol_info = symbol_info_map[symbol]
            fundamentals = fundamentals_map.get(symbol, {})
            position = positions_map.get(symbol)
            price, price_meta = price_cache[symbol]
            ohlcv, ohlcv_meta = ohlcv_cache[symbol]
            technical = technical_cache[symbol]
            trend_score = float(technical["trend_score"])

            temp = row.copy()
            temp["event_edge"] = event_edge
            temp["trend_score"] = trend_score

            sde_inputs = build_sde_inputs(temp)
            sde_prob_up = gbm_prob_up(
                spot=price,
                mu=sde_inputs["mu"],
                sigma=sde_inputs["sigma"],
                horizon_days=int(getattr(settings, "sde_horizon_days", 20)),
                target_return=float(getattr(settings, "sde_target_return", 0.0)),
            )
            sde_prob_score = 2.0 * (sde_prob_up - 0.5)

            temp["sde_prob_up"] = sde_prob_up
            temp["sde_prob_score"] = sde_prob_score
            temp["kelly_yes"] = kelly_yes
            temp["kelly_no"] = kelly_no
            temp["final_alpha"] = calculate_final_alpha(temp)
            quantitative_signal = float(np.clip(
                0.45 * temp["factor_score"] + 0.35 * trend_score + 0.20 * sde_prob_score,
                -1.0,
                1.0,
            ))
            reasoning_fusion = fusion_from_agent_reports(llm_result.get("reasoning_signals"), quantitative_signal)

            base_action = decide_action(settings, temp)
            exit_plan = evaluate_exit_rules(settings, temp, technical, position, price)

            action = base_action
            action_reason = "model_action"

            # 매도는 보유 포지션이 있는 경우 기술적/수익률/alpha exit가 우선.
            if exit_plan["action"] in {"SELL", "PARTIAL_SELL"}:
                action = "SELL"
                action_reason = "exit_rules"
            elif exit_plan["action"] == "SELL_SIGNAL_NO_POSITION" and base_action == "SELL":
                action = "SELL_SIGNAL_NO_POSITION"
                action_reason = "sell_signal_no_position"
            elif base_action == "SELL":
                # Event-level LLM probability is shared across a theme.  It
                # must not liquidate every holding in that theme by itself;
                # live sells require a symbol-specific deterministic exit
                # rule (stop, take-profit, trailing stop, or technical exit).
                action = "HOLD"
                action_reason = "event_level_sell_not_symbol_specific"
            elif base_action == "BUY" and settings.use_technical_entry_rules and not technical.get("buy_signal"):
                action = "HOLD"
                action_reason = "technical_entry_not_confirmed"

            position_fraction = aggressive_position_fraction(
                kelly=kelly_yes,
                max_position_fraction=settings.max_position_fraction,
                multiplier=settings.kelly_multiplier,
            )
            raw_order_value = deployable_bankroll * position_fraction * confidence * agent_size_multiplier
            order_value = diversified_order_value_krw(
                settings,
                run_budget_krw=deployable_bankroll,
                deployed_this_run_krw=deployed_this_loop,
                new_buys_this_run=new_buys_this_loop,
                current_position=position,
                risk_sized_order_krw=raw_order_value,
            )
            if action != "BUY":
                # This field is an intended *new-buy* allocation only.  Do not
                # print a 15,000 KRW "order" beside a SELL/HOLD signal.
                order_value = 0.0

            # A low-confidence LLM or semantic-risk filter blocks only a new
            # BUY.  It must not prevent an existing position's deterministic
            # stop-loss/exit rules from reducing risk.
            if action == "BUY" and not gate_ok:
                action = "HOLD"
                order_value = 0.0
                action_reason = gate_reason
            elif action == "BUY" and not agent_buy_allowed:
                action = "HOLD"
                order_value = 0.0
                action_reason = agent_block_reason or "Gemini_Api2 did not approve a new buy"
            elif action == "BUY" and symbol not in agent_selected_symbols:
                action = "HOLD"
                order_value = 0.0
                action_reason = "agent_did_not_select_this_symbol"

            execution = {"executed": False, "action": action, "reason": "no executable signal"}
            order_spec = {}
            pretrade_price = None
            pretrade_price_meta = None

            if not settings.dry_run and (
                state.get("unresolved_order_submissions") or state.get("pending_live_orders")
            ):
                execution = {
                    "executed": False,
                    "action": "ORDER_RECONCILIATION_REQUIRED",
                    "reason": "a prior broker order is unresolved or non-terminal",
                }
                action = "ORDER_RECONCILIATION_REQUIRED"
                action_reason = "prior_order_reconciliation_required"
            elif action == "BUY":
                if new_buys_this_loop >= settings.max_new_buys_per_loop:
                    execution = {"executed": False, "action": "HOLD", "reason": "max_new_buys_per_loop reached"}
                    action = "HOLD"
                elif not position and (
                    len(positions_map) + new_buys_this_loop
                    >= settings.max_open_positions
                ):
                    execution = {"executed": False, "action": "BUY_BLOCKED", "reason": "max_open_positions reached"}
                    action = "BUY_BLOCKED"
                elif symbol in pending_order_symbols:
                    execution = {"executed": False, "action": "BUY_BLOCKED", "reason": "pending_broker_order_exists"}
                    action = "BUY_BLOCKED"
                else:
                    market = str(symbol_info.get("market", "") or "").upper()
                    market_room = max(0.0, buying_power_after_reserve.get(market, 0.0) - deployed_by_market.get(market, 0.0))
                    order_value = min(order_value, market_room)
                    ok, order_spec = build_buy_order(settings, symbol, symbol_info, price, order_value)
                    if ok:
                        orderability = validate_orderability(settings, toss, symbol, symbol_info)
                        if not orderability.get("ok"):
                            execution = {"executed": False, "action": "BUY_BLOCKED", **orderability}
                            action = "BUY_BLOCKED"
                        else:
                            try:
                                pretrade_price, pretrade_price_meta = live_price_for_order(settings, toss, symbol, price)
                                ok, order_spec = build_buy_order(settings, symbol, symbol_info, pretrade_price, order_value)
                                if not ok:
                                    execution = {"executed": False, "action": "BUY_SKIPPED", **order_spec}
                                    action = "BUY_SKIPPED"
                                else:
                                    orderbook_check = validate_market_order_cost(settings, toss, symbol, order_spec)
                                    if not orderbook_check.get("ok"):
                                        execution = {
                                            "executed": False,
                                            "action": "BUY_BLOCKED",
                                            "reason": orderbook_check.get("reason", "orderbook_check_failed"),
                                            "orderability": orderability,
                                            "orderbook_check": orderbook_check,
                                        }
                                        action = "BUY_BLOCKED"
                                    else:
                                        lifecycle_id = begin_order_lifecycle(
                                            order_ledger,
                                            runtime_run_id=runtime_run_id,
                                            event_id=event.event_id,
                                            symbol=symbol,
                                            symbol_info=symbol_info,
                                            action="BUY",
                                            action_reason=action_reason,
                                            research_packet_hash=research_packets[symbol]["input_snapshot_hash"],
                                            llm_result=llm_result,
                                            order_spec=order_spec,
                                            orderability=orderability,
                                            orderbook_check=orderbook_check,
                                            pretrade_price=pretrade_price,
                                            gate_ok=gate_ok,
                                            gate_reason=gate_reason,
                                            cash_krw=cash,
                                            position=position,
                                        )
                                        execution = execute_order(
                                            settings,
                                            toss,
                                            state,
                                            order_spec,
                                            event.event_id,
                                            symbol,
                                            pretrade_price,
                                            lifecycle_id=lifecycle_id,
                                            ledger=order_ledger,
                                        )
                                        execution["orderability"] = orderability
                                        execution["orderbook_check"] = orderbook_check
                                        if execution.get("submitted") and not execution.get("terminal"):
                                            pending = persist_pending_live_order(
                                                state,
                                                state_path=settings.state_path,
                                                execution=execution,
                                                symbol=symbol,
                                                event_id=event.event_id,
                                                order_spec=order_spec,
                                                lifecycle_id=lifecycle_id,
                                                ledger=order_ledger,
                                            )
                                            execution["reconciliation_required"] = True
                                            execution["pending_live_order"] = pending
                                            action = "BUY_RECONCILIATION_REQUIRED"
                                        if (
                                            execution.get("submitted")
                                            and not execution.get("rejected")
                                            and not execution.get("canceled")
                                        ):
                                            notional = float(order_spec.get("notional_krw", order_value))
                                            new_buys_this_loop += 1
                                            deployed_this_loop += notional
                                            deployed_by_market[market] = deployed_by_market.get(market, 0.0) + notional
                                            pending_order_symbols.add(symbol)
                            except OrderSubmissionUnknown as exc:
                                unresolved = persist_unresolved_order_submission(
                                    state,
                                    state_path=settings.state_path,
                                    error=exc,
                                    symbol=symbol,
                                    event_id=event.event_id,
                                    order_spec=order_spec,
                                    lifecycle_id=exc.lifecycle_id,
                                    ledger=order_ledger,
                                )
                                # Reserve this notional for the remainder of
                                # the run because the broker may have accepted
                                # it even though the response was lost.
                                notional = float(order_spec.get("notional_krw", order_value))
                                new_buys_this_loop += 1
                                deployed_this_loop += notional
                                deployed_by_market[market] = deployed_by_market.get(market, 0.0) + notional
                                pending_order_symbols.add(symbol)
                                execution = {
                                    "executed": False,
                                    "submitted": None,
                                    "action": "BUY_RECONCILIATION_REQUIRED",
                                    "reason": str(exc),
                                    "client_order_id": exc.client_order_id,
                                    "lifecycle_id": exc.lifecycle_id,
                                    "reconciliation_required": True,
                                    "unresolved_submission": unresolved,
                                }
                                action = "BUY_RECONCILIATION_REQUIRED"
                            except Exception as exc:
                                execution = {"executed": False, "action": "BUY_BLOCKED", "reason": str(exc)}
                                action = "BUY_BLOCKED"
                    else:
                        execution = {"executed": False, "action": "BUY_SKIPPED", **order_spec}
                        action = "BUY_SKIPPED"

            elif action == "SELL":
                sell_fraction = float(exit_plan.get("sell_fraction", 1.0) or 1.0)
                ok, order_spec = build_sell_order(settings, symbol, symbol_info, price, position or {}, sell_fraction)
                if ok:
                    try:
                        if not settings.dry_run:
                            validate_sellable_quantity(position, float(order_spec.get("quantity") or 0.0), symbol)
                            broker_sellable = parse_sellable_quantity(
                                toss.sellable_quantity(symbol, account_seq=settings.toss_account_seq)
                            )
                            if float(order_spec.get("quantity") or 0.0) > broker_sellable + 1e-9:
                                raise LiveDataError(
                                    f"[LIVE BLOCKED] broker sellable quantity {broker_sellable} below requested quantity"
                                )
                        if symbol in pending_order_symbols:
                            raise LiveDataError("[LIVE BLOCKED] pending broker order exists for symbol")
                        orderability = validate_orderability(settings, toss, symbol, symbol_info)
                        if not orderability.get("ok"):
                            execution = {"executed": False, "action": "SELL_BLOCKED", **orderability}
                            action = "SELL_BLOCKED"
                        else:
                            pretrade_price, pretrade_price_meta = live_price_for_order(settings, toss, symbol, price)
                            ok, order_spec = build_sell_order(
                                settings, symbol, symbol_info, pretrade_price, position or {}, sell_fraction
                            )
                            if not ok:
                                execution = {"executed": False, "action": "SELL_SIGNAL_ONLY", **order_spec}
                            else:
                                orderbook_check = validate_market_order_cost(settings, toss, symbol, order_spec)
                                if not orderbook_check.get("ok"):
                                    execution = {
                                        "executed": False,
                                        "action": "SELL_BLOCKED",
                                        "reason": orderbook_check.get("reason", "orderbook_check_failed"),
                                        "orderability": orderability,
                                        "orderbook_check": orderbook_check,
                                    }
                                    action = "SELL_BLOCKED"
                                else:
                                    lifecycle_id = begin_order_lifecycle(
                                        order_ledger,
                                        runtime_run_id=runtime_run_id,
                                        event_id=event.event_id,
                                        symbol=symbol,
                                        symbol_info=symbol_info,
                                        action="SELL",
                                        action_reason=action_reason,
                                        research_packet_hash=research_packets[symbol]["input_snapshot_hash"],
                                        llm_result=llm_result,
                                        order_spec=order_spec,
                                        orderability=orderability,
                                        orderbook_check=orderbook_check,
                                        pretrade_price=pretrade_price,
                                        gate_ok=gate_ok,
                                        gate_reason=gate_reason,
                                        cash_krw=cash,
                                        position=position,
                                    )
                                    execution = execute_order(
                                        settings,
                                        toss,
                                        state,
                                        order_spec,
                                        event.event_id,
                                        symbol,
                                        pretrade_price,
                                        lifecycle_id=lifecycle_id,
                                        ledger=order_ledger,
                                    )
                                    execution["orderability"] = orderability
                                    execution["orderbook_check"] = orderbook_check
                                    if execution.get("submitted") and not execution.get("terminal"):
                                        pending = persist_pending_live_order(
                                            state,
                                            state_path=settings.state_path,
                                            execution=execution,
                                            symbol=symbol,
                                            event_id=event.event_id,
                                            order_spec=order_spec,
                                            lifecycle_id=lifecycle_id,
                                            ledger=order_ledger,
                                        )
                                        execution["reconciliation_required"] = True
                                        execution["pending_live_order"] = pending
                                        action = "SELL_RECONCILIATION_REQUIRED"
                                    if (
                                        execution.get("submitted")
                                        and not execution.get("rejected")
                                        and not execution.get("canceled")
                                    ):
                                        pending_order_symbols.add(symbol)
                    except OrderSubmissionUnknown as exc:
                        unresolved = persist_unresolved_order_submission(
                            state,
                            state_path=settings.state_path,
                            error=exc,
                            symbol=symbol,
                            event_id=event.event_id,
                            order_spec=order_spec,
                            lifecycle_id=exc.lifecycle_id,
                            ledger=order_ledger,
                        )
                        pending_order_symbols.add(symbol)
                        execution = {
                            "executed": False,
                            "submitted": None,
                            "action": "SELL_RECONCILIATION_REQUIRED",
                            "reason": str(exc),
                            "client_order_id": exc.client_order_id,
                            "lifecycle_id": exc.lifecycle_id,
                            "reconciliation_required": True,
                            "unresolved_submission": unresolved,
                        }
                        action = "SELL_RECONCILIATION_REQUIRED"
                    except Exception as e:
                        execution = {"executed": False, "action": "SELL_BLOCKED", "reason": str(e)}
                        action = "SELL_BLOCKED"
                else:
                    execution = {"executed": False, "action": "SELL_SIGNAL_ONLY", **order_spec}

            elif action == "SELL_SIGNAL_NO_POSITION":
                execution = {
                    "executed": False,
                    "action": "SELL_SIGNAL_NO_POSITION",
                    "reason": "sell signal generated but no position quantity found",
                    "symbol": symbol,
                }
            else:
                execution = {
                    "executed": False,
                    "action": action,
                    "reason": action_reason,
                }

            result = {
                "runtime_run_id": runtime_run_id,
                "decision_time_utc": observation_time_utc,
                "data_cutoff_utc": observation_time_utc,
                "event_id": event.event_id,
                "question": event.question,
                "symbol": symbol,
                "symbol_info": symbol_info,
                "fundamentals": fundamentals,
                "price": price,
                "price_meta": price_meta,
                "ohlcv_meta": ohlcv_meta,
                "research_packet_hash": research_packets[symbol]["input_snapshot_hash"],
                "research_sources": research_packets[symbol]["sources"],
                "research_missing_modalities": research_packets[symbol]["missing_modalities"],
                "lead_lag_status": lead_lag_status,
                "lead_lag_candidate_count": len(lead_lag_candidates),
                "llm_candidate_symbols": llm_symbols,
                "agent_selected_symbols": sorted(agent_selected_symbols),
                "position": position,
                "llm_prob": llm_prob,
                "confidence": confidence,
                "agent_buy_allowed": agent_buy_allowed,
                "agent_size_multiplier": agent_size_multiplier,
                "agent_block_reason": agent_block_reason,
                "pretrade_price": pretrade_price,
                "pretrade_price_meta": pretrade_price_meta,
                "broker_preflight": {
                    "krw_buying_power": krw_buying_power,
                    "usd_buying_power": usd_buying_power,
                    "usd_krw_reference_rate": live_fx,
                    "pending_order_symbols": sorted(pending_order_symbols),
                    "commission_snapshot": commission_snapshot,
                },
                "lmsr_price_before": lmsr_price_before,
                "sapience_price": sapience_price,
                "blended_price": blended_price,
                "event_edge": event_edge,
                "kelly_yes": kelly_yes,
                "kelly_no": kelly_no,
                "position_fraction": position_fraction,
                "factor_score": float(temp["factor_score"]),
                "value_score": float(temp["value_score"]),
                "momentum_score": float(temp["momentum_score"]),
                "quality_score": float(temp["quality_score"]),
                "risk_score": float(temp["risk_score"]),
                "liquidity_score": float(temp["liquidity_score"]),
                "growth_revision_score": float(temp["growth_revision_score"]),
                "news_event_score": float(temp["news_event_score"]),
                "technical": technical,
                "trend_score": float(trend_score),
                "sde_mu": sde_inputs["mu"],
                "sde_sigma": sde_inputs["sigma"],
                "sde_prob_up": float(sde_prob_up),
                "sde_prob_score": float(sde_prob_score),
                "final_alpha": float(temp["final_alpha"]),
                "reasoning_fusion": reasoning_fusion,
                "base_action": base_action,
                "exit_plan": exit_plan,
                "gate_ok": gate_ok,
                "gate_reason": gate_reason,
                "action": action,
                "action_reason": action_reason,
                "order_value": float(order_value),
                "order_spec": order_spec,
                "lmsr_update": lmsr_update,
                "google_evidence": llm_result.get("google_evidence"),
                "nvidia_judgement": llm_result.get("nvidia_judgement"),
                "agentic_analysis": llm_result.get("agentic_analysis"),
                "predicted_return_bin": llm_result.get("predicted_return_bin", "unknown"),
                "reasoning_signals": llm_result.get("reasoning_signals", {}),
                "api_usage": llm_result.get("api_usage"),
                "execution": execution,
            }

            # A forecast is immutable at decision time.  Its actual 5-day
            # outcome is appended later by an evaluator, never written back
            # into this signal/order record.
            try:
                agentic = llm_result.get("agentic_analysis") or {}
                forecast = ForecastRecord(
                    run_id=str(agentic.get("run_id") or runtime_run_id),
                    decision_time_utc=observation_time_utc,
                    symbol=str(symbol),
                    decision_price=float(price),
                    predicted_bin=str(llm_result.get("predicted_return_bin", "unknown")),
                    confidence=confidence,
                    input_snapshot_hash=research_packets[symbol]["input_snapshot_hash"],
                )
                result["forecast_record"] = forecast_ledger.append_prediction(forecast)
            except Exception as exc:
                # Audit failure must be visible, but should not turn an already
                # validated real order into a second, accidental failure path.
                result["forecast_record_error"] = str(exc)

            append_jsonl(settings.log_path, result)

            print(
                f"{action:>16} | {symbol:<8} | "
                f"p={llm_prob:.2f} C={blended_price:.2f} "
                f"edge={event_edge:+.3f} "
                f"kelly={kelly_yes:+.3f} "
                f"frac={position_fraction:.3f} "
                f"alpha={temp['final_alpha']:+.3f} "
                f"sdeP={sde_prob_up:.2f} "
                f"trend={trend_score:+.2f} "
                f"tech_buy={technical.get('buy_signal')} "
                f"order={order_value:,.0f} "
                f"gate={gate_ok}"
            )

    save_state(settings.state_path, state)


def run_broker_preflight(settings: Settings) -> bool:
    """Exercise every read-only dependency of the live order path.

    This command intentionally never calls ``create_order``.  It is the
    deploy-time check for credentials, account headers, balances, holdings,
    order history, reference data, current prices, candles, and calendars.
    """

    report: dict = {
        "checked_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "dry_run": bool(settings.dry_run),
        "ok": False,
        "checks": {},
        "errors": [],
    }
    broker_state = load_state(settings.state_path)
    unresolved = broker_state.get("unresolved_order_submissions", {})
    pending_live_orders = broker_state.get("pending_live_orders", {})
    if unresolved:
        report["unresolved_order_submissions"] = list(unresolved)
        report["errors"].append("Unresolved broker POST exists; reconcile it before enabling new live orders.")
    if pending_live_orders:
        report["pending_live_orders"] = list(pending_live_orders)
        report["errors"].append("Accepted broker order remains non-terminal; reconcile it before enabling new live orders.")
    if settings.dry_run:
        report["errors"].append("Set DRY_RUN=false before a real broker preflight.")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return False

    try:
        toss = make_toss_client(settings)
        account_seq = toss.resolve_account_seq()
        krw_raw = toss.buying_power(account_seq=account_seq, currency="KRW")
        usd_raw = toss.buying_power(account_seq=account_seq, currency="USD")
        fx_raw = toss.exchange_rate("USD", "KRW")
        krw = parse_buying_power(krw_raw)
        usd = parse_buying_power(usd_raw)
        fx = parse_exchange_rate(fx_raw)

        holdings_raw = toss.positions(account_seq=account_seq)
        positions = parse_positions(holdings_raw)
        risk_snapshot = parse_holdings_risk_snapshot(holdings_raw, fx)
        open_orders_raw = toss.orders(account_seq=account_seq, status="OPEN")
        open_order_symbols = parse_open_order_symbols(open_orders_raw)
        commissions_raw = toss.commissions(account_seq=account_seq)

        sample_symbols = ["005930", "AAPL"]
        stock_raw = toss.stocks(sample_symbols)
        stock_checks = {
            symbol: parse_stock_tradability(stock_raw, symbol).get("tradable") is True
            for symbol in sample_symbols
        }
        price_raw = toss.prices(sample_symbols)
        sample_prices = {symbol: parse_current_price(price_raw, symbol) for symbol in sample_symbols}
        orderbook = parse_orderbook(toss.orderbook("005930"))
        candle_raw = toss.candles("005930", interval="1d", count=1)
        candle_rows = candles_response_to_ohlcv(candle_raw)
        if candle_rows.empty:
            raise LiveDataError("preflight candle sample is empty")
        kr_calendar = toss.market_status("KR")
        us_calendar = toss.market_status("US")

        report["checks"] = {
            "account_resolved": bool(account_seq),
            "buying_power": {"KRW": krw >= 0, "USD": usd >= 0},
            "exchange_rate": fx > 0,
            "holdings_reconciled": True,
            "open_orders_reconciled": True,
            "commissions_read": isinstance(commissions_raw, dict),
            "stock_info": stock_checks,
            "prices": {symbol: price > 0 for symbol, price in sample_prices.items()},
            # Freshness is intentionally not a preflight requirement: a
            # preflight can run while the exchange is closed.  The actual
            # order path checks it immediately before every market-order POST.
            "orderbook": bool(orderbook["best_ask"] > orderbook["best_bid"] and orderbook["spread_bps"] >= 0),
            "candles": int(len(candle_rows)) >= 1,
            "market_calendar": {
                "KR": is_market_open_from_clock(kr_calendar).get("is_open"),
                "US": is_market_open_from_clock(us_calendar).get("is_open"),
            },
        }
        report["summary"] = {
            "cash_krw": krw,
            "cash_usd": usd,
            "usd_krw_reference_rate": fx,
            "position_count": len(positions),
            "open_order_symbols": sorted(open_order_symbols),
            "holdings_market_value_krw": risk_snapshot["market_value_krw"],
            "daily_pnl_pct": risk_snapshot["daily_pnl_pct"],
            "sample_prices": sample_prices,
            "sample_orderbook": {
                "timestamp": orderbook["timestamp"],
                "currency": orderbook["currency"],
                "best_ask": orderbook["best_ask"],
                "best_bid": orderbook["best_bid"],
                "spread_bps": orderbook["spread_bps"],
            },
        }
        report["ok"] = all(
            [
                report["checks"]["account_resolved"],
                all(report["checks"]["buying_power"].values()),
                report["checks"]["exchange_rate"],
                report["checks"]["holdings_reconciled"],
                report["checks"]["open_orders_reconciled"],
                report["checks"]["commissions_read"],
                all(report["checks"]["stock_info"].values()),
                all(report["checks"]["prices"].values()),
                report["checks"]["orderbook"],
                report["checks"]["candles"],
                not bool(unresolved),
                not bool(pending_live_orders),
            ]
        )
    except Exception as exc:
        report["errors"].append(str(exc))

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return bool(report["ok"])


def acknowledge_unresolved_submission(settings: Settings, client_order_id: str) -> bool:
    """Explicitly clear an ambiguous POST guard after manual Toss reconciliation.

    This is deliberately a separate operator action; the bot itself never
    clears an unknown broker POST merely because time has passed.
    """

    state = load_state(settings.state_path)
    unresolved = state.get("unresolved_order_submissions", {})
    record = unresolved.get(client_order_id) if isinstance(unresolved, dict) else None
    if not record:
        print(json.dumps({"ok": False, "reason": "unresolved_client_order_id_not_found"}, ensure_ascii=False))
        return False
    lifecycle_id = str(record.get("lifecycle_id") or uuid.uuid4().hex)
    try:
        OrderLifecycleLedger(settings.order_lifecycle_ledger_path).record(
            "manual_reconciliation_acknowledged",
            lifecycle_id,
            client_order_id=client_order_id,
            symbol=record.get("symbol"),
            event_id=record.get("event_id"),
            verification="operator asserted manual Toss reconciliation; no automated broker outcome was inferred",
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "reason": f"order_lifecycle_audit_failed: {exc}"}, ensure_ascii=False))
        return False
    unresolved.pop(client_order_id, None)
    save_state(settings.state_path, state)
    append_jsonl(
        settings.log_path,
        {
            "action": "UNRESOLVED_ORDER_ACKNOWLEDGED",
            "client_order_id": client_order_id,
            "lifecycle_id": lifecycle_id,
            "unresolved_submission": record,
        },
    )
    print(json.dumps({"ok": True, "acknowledged_client_order_id": client_order_id}, ensure_ascii=False))
    return True


def start_dashboard_if_enabled(settings: Settings) -> None:
    if not settings.enable_dashboard:
        return

    def _run():
        from dashboard_server import app
        app.run(
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            debug=False,
            use_reloader=False,
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    url = f"http://{settings.dashboard_host}:{settings.dashboard_port}"
    print(f"[dashboard] {url}")

    if settings.dashboard_open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="Read-only Toss broker readiness check; never creates an order")
    parser.add_argument(
        "--acknowledge-unresolved",
        metavar="CLIENT_ORDER_ID",
        help="Clear an ambiguous POST guard only after manually reconciling the order in Toss",
    )
    args = parser.parse_args()

    settings = Settings()
    if args.preflight:
        raise SystemExit(0 if run_broker_preflight(settings) else 1)
    if args.acknowledge_unresolved:
        raise SystemExit(0 if acknowledge_unresolved_submission(settings, args.acknowledge_unresolved) else 1)
    start_dashboard_if_enabled(settings)

    while True:
        # 대시보드의 자동투자 시작/정지 버튼 (data/bot_control.json)
        try:
            control_path = Path("data/bot_control.json")
            if control_path.exists():
                control = json.loads(control_path.read_text(encoding="utf-8"))
                if control.get("run") is False:
                    print(f"[{now_iso()}] 자동투자 일시정지 상태 (대시보드에서 시작 버튼을 누르면 재개)")
                    if args.once:
                        break
                    time.sleep(min(30, settings.loop_seconds))
                    continue
        except Exception as exc:
            print(f"[control] bot_control.json 읽기 실패, 계속 진행: {exc}")

        try:
            run_once(settings)
            state = load_state(settings.state_path)
            state["api_error_count"] = 0
            save_state(settings.state_path, state)

        except Exception as e:
            print(f"[ERROR] {e}")
            state = load_state(settings.state_path)
            state["api_error_count"] = int(state.get("api_error_count", 0)) + 1
            save_state(settings.state_path, state)
            append_jsonl(settings.log_path, {"error": str(e)})

        if args.once:
            break

        print(f"sleep {settings.loop_seconds} seconds...")
        time.sleep(settings.loop_seconds)


if __name__ == "__main__":
    main()
