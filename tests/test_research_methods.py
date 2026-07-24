import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy.agentic.simulator import (
    finite_horizon_value,
    order_book_metrics,
    perpetuity_value,
    price_to_value_ratio,
    volume_weighted_average_price,
)
from strategy.agentic.causal_graph import regime_weight
from strategy.agentic.memory_store import AgentMemoryStore
from strategy.factors import compute_factor_scores
from strategy.forecast_evaluation import (
    ForecastLedger,
    ForecastRecord,
    bin_score,
    direction_hit,
    five_point_trimmed_mean,
    weekly_return_bin,
)
from strategy.lead_lag import generate_candidates, granger_f_test, log_returns
from strategy.research_metrics import (
    CostScenario,
    backtest_metrics,
    gross_portfolio_return,
    net_portfolio_return,
    portfolio_turnover,
)
from strategy.research_packet import (
    build_research_packet,
    build_research_universe,
    compute_indicator_snapshot,
    compute_market_features,
)
from strategy.speculative_decoding import (
    acceptance_probability,
    correction_distribution,
    expected_emitted_tokens,
    expected_walltime_speedup,
    mean_acceptance_rate,
    speculation_is_worthwhile,
)
from strategy.signal_fusion import SignalFusionWeights, fuse_signals


def make_ohlcv(rows: int = 260) -> pd.DataFrame:
    close = np.linspace(100.0, 160.0, rows) + np.sin(np.arange(rows) / 4.0)
    return pd.DataFrame(
        {
            "open": close * 0.997,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.linspace(100_000, 200_000, rows),
        }
    )


def test_research_packet_uses_calculated_indicators_and_stable_snapshot_hash():
    ohlcv = make_ohlcv()
    indicators = compute_indicator_snapshot(ohlcv)
    assert indicators["data_quality"] == "ok"
    assert indicators["values"]["rsi"] is not None
    assert indicators["values"]["sma_long"] is not None
    assert indicators["parameters"]["macd"] == [12, 26, 9]

    packet = build_research_packet(
        symbol="005930",
        symbol_info={"symbol": "005930", "market": "KR"},
        price=160.0,
        price_meta={"source": "toss_prices_real"},
        ohlcv=ohlcv,
        ohlcv_meta={"source": "toss_candles_real"},
        fundamentals={"source": "opendart", "roe": 0.12, "market_cap": 1000},
        technical_rules={"trend_score": 0.4},
        observed_at_utc="2026-07-15T00:00:00Z",
    )
    assert len(packet["input_snapshot_hash"]) == 64
    assert packet["technical_indicators"]["values"]["macd"] is not None
    assert len(packet["ohlcv_last_8_weeks"]) == 40
    assert packet["sources"][0]["available_at_utc"] == "2026-07-15T00:00:00Z"


def test_real_research_universe_neutralizes_missing_values_instead_of_randomizing():
    features = compute_market_features(make_ohlcv())
    universe = build_research_universe(
        ["A", "B"],
        fundamentals_by_symbol={"A": {"roe": 0.2, "market_cap": 1_000}},
        market_features_by_symbol={"A": features},
        event_sentiment=0.3,
    )
    scored = compute_factor_scores(universe)
    assert scored["factor_score"].map(math.isfinite).all()
    assert scored.loc[scored["symbol"] == "B", "roe"].isna().all()
    assert scored.loc[scored["symbol"] == "A", "llm_sentiment"].iloc[0] == 0.3


def test_backtest_cost_turnover_and_metrics_follow_explicit_equations():
    turnover = portfolio_turnover({"A": 0.10, "B": 0.0}, {"A": 0.0, "B": 0.10})
    assert turnover == 0.20
    gross = gross_portfolio_return({"A": 0.5, "B": 0.5}, {"A": 0.10, "B": -0.02})
    assert gross == 0.04
    net = net_portfolio_return(gross, turnover, CostScenario(commission_bps=10))
    assert net == 0.0398
    metrics = backtest_metrics([0.10, -0.05], gross_returns=[0.10, -0.05], turnovers=[0.2, 0.1])
    assert metrics["cumulative_return"] == pytest.approx(0.045)
    assert metrics["max_drawdown"] == pytest.approx(-0.05)
    assert metrics["mean_turnover"] == pytest.approx(0.15)


def test_weekly_bins_scores_trimmed_mean_and_append_only_forecast_ledger(tmp_path: Path):
    assert weekly_return_bin(-5.5) == "D5-"
    assert weekly_return_bin(-0.5) == "N"
    assert weekly_return_bin(5.5) == "U5+"
    assert direction_hit("U3", "U1") is True
    assert bin_score("U3", "D2") == 0
    assert five_point_trimmed_mean([1, 2, 3, 4, 100]) == 3.0

    ledger = ForecastLedger(tmp_path / "forecasts.jsonl", durable=False)
    forecast = ForecastRecord(
        run_id="run-1",
        decision_time_utc="2026-07-15T00:00:00Z",
        symbol="005930",
        decision_price=100.0,
        predicted_bin="U2",
        confidence=0.8,
        input_snapshot_hash="a" * 64,
    )
    ledger.append_prediction(forecast)
    outcome = ledger.append_outcome(forecast, actual_price=102.0)
    assert outcome["actual_bin"] == "U2"
    rows = list(ledger.records())
    assert [row["record_type"] for row in rows] == ["forecast_prediction", "forecast_outcome"]


def test_numerical_lead_lag_candidate_uses_returns_granger_and_fdr():
    rng = np.random.default_rng(7)
    leader_returns = rng.normal(0.0002, 0.01, 180)
    follower_returns = np.zeros_like(leader_returns)
    follower_returns[1:] = 0.9 * leader_returns[:-1] + rng.normal(0.0, 0.002, len(leader_returns) - 1)
    result = granger_f_test(leader_returns, follower_returns, lag=1)
    assert result.p_value < 0.001

    leader_price = 100 * np.exp(np.cumsum(np.r_[0.0, leader_returns]))
    follower_price = 100 * np.exp(np.cumsum(np.r_[0.0, follower_returns]))
    candidates = generate_candidates({"LEADER": leader_price, "FOLLOWER": follower_price}, minimum_observations=80)
    candidate = next(item for item in candidates if item.leader == "LEADER" and item.follower == "FOLLOWER")
    assert candidate.fdr_significant is True
    assert candidate.expected_sign == "same"
    assert len(log_returns(leader_price)) == len(leader_returns)


def test_simulator_valuation_and_book_metrics_are_explicitly_simulator_only():
    assert perpetuity_value(1.4, 0.05) == pytest.approx(28.0)
    assert finite_horizon_value(1.4, 0.05, current_round=1, final_round=3, terminal_value=28.0) == pytest.approx(28.0)
    assert price_to_value_ratio(35.0, 28.0) == pytest.approx(1.25)
    metrics = order_book_metrics(
        [{"limit_price": 99, "remaining": 2}, {"limit_price": 100, "remaining": 3}],
        [{"limit_price": 102, "remaining": 4}, {"limit_price": 101, "remaining": 1}],
    )
    assert metrics["best_bid"] == 100.0
    assert metrics["best_ask"] == 101.0
    assert metrics["spread"] == 1.0
    assert volume_weighted_average_price([100, 110], [1, 3]) == pytest.approx(107.5)
    assert regime_weight(0.8, 0.6, alpha=0.7, beta=0.3) == pytest.approx(0.74)


def test_speculative_decoding_math_is_available_but_not_a_remote_api_emulation():
    assert acceptance_probability(0.2, 0.4) == 0.5
    corrected = correction_distribution([0.7, 0.3], [0.2, 0.8])
    assert np.allclose(corrected, [1.0, 0.0])
    assert mean_acceptance_rate([0.7, 0.3], [0.2, 0.8]) == pytest.approx(0.5)
    assert expected_emitted_tokens(0.5, 2) == pytest.approx(1.75)
    assert expected_walltime_speedup(0.5, 2, 0.1) == pytest.approx(1.75 / 1.2)
    assert speculation_is_worthwhile(0.5, 0.1) is True


def test_fact_subjectivity_quantitative_fusion_is_bounded_and_auditable():
    fused = fuse_signals(0.8, -0.5, 0.4, weights=SignalFusionWeights(0.5, 0.2, 0.3))
    assert fused["score"] == pytest.approx(0.42)
    assert fused["agreement"] == "mixed"
    assert fused["research_only"] is True


def test_memory_derivations_and_links_are_append_only(tmp_path: Path):
    store = AgentMemoryStore(tmp_path / "memory.jsonl", durable=False)
    original = store.append_note("original earnings observation", tags=["earnings"])
    derived = store.append_derived_note(
        "later interpretation",
        source_memory_ids=[original["memory_id"]],
        version_reason="outcome review",
        tags=["earnings"],
    )
    link = store.append_link(derived["memory_id"], original["memory_id"], relation="supports", confidence=0.7)
    assert derived["derived_from"] == [original["memory_id"]]
    assert link["relation"] == "supports"
    assert store.links_for(original["memory_id"])[0]["link_id"] == link["link_id"]
