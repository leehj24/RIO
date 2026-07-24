import datetime as dt
from types import SimpleNamespace

from main import (
    cache_live_llm_result,
    execute_order,
    get_cached_live_llm_result,
    persist_pending_live_order,
    pick_ohlcv,
    reconcile_pending_live_orders,
)
from strategy.market_data import fetch_candles_paginated
from strategy.portfolio_budget import buy_budget_krw, diversified_order_value_krw
from strategy.live_data_guard import (
    evaluate_market_orderbook,
    parse_holdings_risk_snapshot,
    parse_order_status,
    parse_orderbook,
    parse_positions,
)


def settings():
    return SimpleNamespace(
        cash_reserve_fraction=0.15,
        auto_buy_total_cap_krw=30_000,
        auto_buy_per_symbol_cap_krw=15_000,
        max_new_buys_per_loop=2,
        min_order_krw=10_000,
        min_order_usd=5,
        usd_krw_fx_rate=1_350,
    )


def test_current_cash_is_split_into_two_capped_live_buy_tickets():
    budget = buy_budget_krw(settings(), 105_568, {})

    assert budget["cash_after_reserve_krw"] == 89_732.8
    assert budget["run_budget_krw"] == 30_000
    assert diversified_order_value_krw(
        settings(),
        run_budget_krw=budget["run_budget_krw"],
        deployed_this_run_krw=0,
        new_buys_this_run=0,
        current_position=None,
        risk_sized_order_krw=5_384,
    ) == 15_000
    assert diversified_order_value_krw(
        settings(),
        run_budget_krw=budget["run_budget_krw"],
        deployed_this_run_krw=15_000,
        new_buys_this_run=1,
        current_position=None,
        risk_sized_order_krw=5_384,
    ) == 15_000


def test_existing_position_reduces_total_and_per_symbol_room():
    positions = {
        "A": {"_market_value": 15_000, "currency": "KRW"},
        "B": {"_market_value": 5, "currency": "USD"},
    }
    budget = buy_budget_krw(settings(), 105_568, positions)

    assert budget["current_exposure_krw"] == 21_750
    assert budget["run_budget_krw"] == 8_250
    assert diversified_order_value_krw(
        settings(),
        run_budget_krw=budget["run_budget_krw"],
        deployed_this_run_krw=0,
        new_buys_this_run=0,
        current_position=positions["A"],
        risk_sized_order_krw=15_000,
    ) == 0


def test_live_ohlcv_request_is_clamped_to_toss_maximum():
    class Toss:
        def __init__(self):
            self.count = None

        def candles(self, symbol, *, interval, count, before=None):
            self.count = count
            return {
                "result": [
                    {"timestamp": f"t{index}", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
                    for index in range(60)
                ]
            }

    toss = Toss()
    live = SimpleNamespace(dry_run=False, research_ohlcv_bars=260, block_live_on_ohlcv_fallback=True)
    ohlcv, meta = pick_ohlcv(live, toss, "000100")

    assert len(ohlcv) == 60
    assert toss.count == 200
    assert meta["requested_count"] == 260
    assert meta["candle_count"] == 60


def test_candle_pagination_uses_next_before_and_deduplicates_the_boundary():
    class Toss:
        def __init__(self):
            self.calls = []

        def candles(self, symbol, *, interval, count, before=None):
            self.calls.append((symbol, interval, count, before))
            if before is None:
                return {
                    "result": {
                        "candles": [{"timestamp": f"t{n}", "close": n} for n in range(260, 60, -1)],
                        "nextBefore": "t61",
                    }
                }
            return {
                "result": {
                    "candles": [{"timestamp": f"t{n}", "close": n} for n in range(61, 0, -1)],
                    "nextBefore": None,
                }
            }

    toss = Toss()
    raw, meta = fetch_candles_paginated(toss, "000100", interval="1d", total_count=260)

    assert toss.calls == [("000100", "1d", 200, None), ("000100", "1d", 61, "t61")]
    assert len(raw["result"]["candles"]) == 260
    assert meta["requested_count"] == 260
    assert meta["page_count"] == 2
    assert meta["raw_count"] == 260
    assert meta["cache_hit"] == 0


def test_official_holdings_shape_preserves_live_exposure_and_last_price():
    raw = {
        "result": {
            "marketValue": {
                "amount": {"krw": "7200000", "usd": "1785"},
                "amountAfterCost": {"krw": "7050000", "usd": "1771.43"},
            },
            "dailyProfitLoss": {"rate": "0.0141"},
            "items": [
                {
                    "symbol": "005930",
                    "currency": "KRW",
                    "quantity": "100",
                    "lastPrice": "72000",
                    "averagePurchasePrice": "65000",
                    "marketValue": {"amount": "7200000", "amountAfterCost": "7050000"},
                },
                {
                    "symbol": "AAPL",
                    "currency": "USD",
                    "quantity": "10",
                    "lastPrice": "178.5",
                    "averagePurchasePrice": "155.3",
                    "marketValue": {"amount": "1785", "amountAfterCost": "1771.43"},
                },
            ],
        }
    }

    positions = parse_positions(raw)
    assert positions["005930"]["_current_price"] == 72_000
    assert positions["005930"]["_market_value"] == 7_050_000
    assert positions["AAPL"]["_market_value"] == 1_771.43

    snapshot = parse_holdings_risk_snapshot(raw, 1_350)
    assert snapshot["market_value_krw"] == 7_050_000 + 1_771.43 * 1_350
    assert snapshot["daily_pnl_pct"] == 0.0141


def test_official_order_status_nested_execution_is_logged_as_a_fill():
    status = parse_order_status(
        {
            "result": {
                "status": "FILLED",
                "execution": {"filledQuantity": "2", "averageFilledPrice": "185.25"},
            }
        }
    )

    assert status["_is_filled"] is True
    assert status["_filled_quantity"] == 2
    assert status["_avg_filled_price"] == 185.25


def test_post_acceptance_with_status_lookup_failure_stays_submitted_for_reconciliation():
    class Toss:
        def create_order(self, **kwargs):
            return {"result": {"orderId": "order-1"}}

        def order_status(self, *_args, **_kwargs):
            raise RuntimeError("temporary order-history outage")

    live = SimpleNamespace(
        dry_run=False,
        toss_account_seq="account",
        require_order_status_after_live_order=True,
        usd_krw_fx_rate=1_350,
    )
    execution = execute_order(
        live,
        Toss(),
        {},
        {"side": "BUY", "order_type": "MARKET", "quantity": "1", "notional_krw": 10_000},
        "event",
        "005930",
        10_000,
    )

    assert execution["submitted"] is True
    assert execution["reconciliation_pending"] is True
    assert execution["executed"] is False


def test_accepted_order_with_status_outage_is_durably_guarded_then_reconciled(tmp_path):
    class Toss:
        def create_order(self, **kwargs):
            return {"result": {"orderId": "order-1"}}

        def order_status(self, *_args, **_kwargs):
            if not getattr(self, "recovered", False):
                raise RuntimeError("temporary order-history outage")
            return {
                "result": {
                    "status": "FILLED",
                    "execution": {"filledQuantity": "1", "averageFilledPrice": "10000"},
                }
            }

    toss = Toss()
    state_path = tmp_path / "state.json"
    live = SimpleNamespace(
        dry_run=False,
        toss_account_seq="account",
        require_order_status_after_live_order=True,
        usd_krw_fx_rate=1_350,
        state_path=str(state_path),
    )
    order_spec = {"side": "BUY", "order_type": "MARKET", "quantity": "1", "notional_krw": 10_000}
    execution = execute_order(live, toss, {}, order_spec, "event", "005930", 10_000, lifecycle_id="lifecycle")

    assert execution["reconciliation_pending"] is True
    state = {"pending_live_orders": {}}
    pending = persist_pending_live_order(
        state,
        state_path=str(state_path),
        execution=execution,
        symbol="005930",
        event_id="event",
        order_spec=order_spec,
        lifecycle_id="lifecycle",
    )
    assert pending["order_id"] == "order-1"
    assert execution["client_order_id"] in state["pending_live_orders"]

    toss.recovered = True
    reconciliation = reconcile_pending_live_orders(live, toss, state)
    assert reconciliation["remaining"] == 0
    assert reconciliation["reconciled"] == [execution["client_order_id"]]
    assert state["pending_live_orders"] == {}


def test_market_orderbook_uses_best_prices_not_response_array_order_and_walks_depth():
    book = parse_orderbook(
        {
            "result": {
                "timestamp": "2026-07-16T14:00:00+09:00",
                "currency": "KRW",
                # Deliberately not best-first, matching the ordering ambiguity
                # in the API examples.
                "asks": [{"price": "102", "volume": "4"}, {"price": "101", "volume": "3"}],
                "bids": [{"price": "99", "volume": "9"}, {"price": "100", "volume": "6"}],
            }
        }
    )

    assert book["best_ask"] == 101
    assert book["best_bid"] == 100
    check = evaluate_market_orderbook(
        book,
        side="BUY",
        quantity="5",
        max_spread_bps=100,
        max_book_impact_bps=50,
        max_age_seconds=15,
        now=dt.datetime(2026, 7, 16, 5, 0, 2, tzinfo=dt.timezone.utc),
    )

    assert check["ok"] is True
    assert check["estimated_fill_quantity"] == 5
    assert check["estimated_vwap"] == 101.4


def test_market_orderbook_blocks_when_visible_depth_cannot_cover_amount_order():
    book = parse_orderbook(
        {
            "result": {
                "timestamp": "2026-07-16T14:00:00+09:00",
                "currency": "USD",
                "asks": [{"price": "100", "volume": "1"}],
                "bids": [{"price": "99", "volume": "1"}],
            }
        }
    )

    check = evaluate_market_orderbook(
        book,
        side="BUY",
        order_amount="150",
        max_spread_bps=200,
        max_book_impact_bps=50,
        max_age_seconds=15,
        now=dt.datetime(2026, 7, 16, 5, 0, 1, tzinfo=dt.timezone.utc),
    )

    assert check["ok"] is False
    assert check["reason"] == "insufficient_visible_orderbook_depth"


def test_valid_live_agent_report_is_reused_only_as_explicitly_expiring_research_cache():
    event = SimpleNamespace(event_id="theme-event")
    live_settings = SimpleNamespace(live_llm_decision_ttl_seconds=3_600)

    class Pipeline:
        def usage_snapshot(self):
            return {"gemini": {"remaining": 10}}

    original = {
        "yes_probability": 0.7,
        "confidence": 0.8,
        "news_score": 0.0,
        "buy_allowed": True,
        "size_multiplier": 0.5,
        "block_reason": "",
        "selected_symbols": ["005930"],
        "selected_symbol_reason": "agent_selected_symbol",
        "reasoning_signals": {},
        "agentic_analysis": {"status": "ok", "run_id": "agent-run"},
    }
    state = {}

    assert cache_live_llm_result(live_settings, state, event, ["005930"], original) is True
    cached = get_cached_live_llm_result(live_settings, state, event, ["005930"], Pipeline())

    assert cached is not None
    assert cached["buy_allowed"] is True
    assert cached["agentic_analysis"]["status"] == "cached"
    assert cached["agentic_analysis"]["source_agent_run_id"] == "agent-run"
