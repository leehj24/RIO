import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import env_first
from strategy.agentic.orchestrator import GeminiAgentOrchestrator
from strategy.agentic.prompt_loader import PromptLoader
from strategy.agentic.risk_gate import apply_semantic_risk_assessment
from strategy.agentic.simulator import ContinuousDoubleAuction, SimOrder, SimulationLedger
from strategy.llm_providers import GeminiAPI2Client


def make_settings(tmp_path: Path, *, enabled: bool):
    return SimpleNamespace(
        enable_gemini_api2=enabled,
        gemini_api2_key="test-key",
        gemini_api2_model="gemini-test",
        gemini_api2_enable_grounding=False,
        gemini_api2_temperature=0.1,
        gemini_api2_max_output_tokens=256,
        gemini_api2_timeout_seconds=5,
        gemini_api2_prompt_root="ai_prompts",
        gemini_api2_daily_call_limit=30,
        gemini_api2_max_agents_per_run=20,
        gemini_api2_memory_top_k=3,
        gemini_api2_enable_response_schema=False,
        api_usage_path=str(tmp_path / "usage.json"),
        llm_log_path=str(tmp_path / "llm.jsonl"),
        gemini_api2_audit_path=str(tmp_path / "audit.jsonl"),
        gemini_api2_memory_path=str(tmp_path / "memory.jsonl"),
        gemini_api2_hypergraph_path=str(tmp_path / "hyperedges.jsonl"),
        gemini_api2_teacher_labels_path=str(tmp_path / "teacher_labels.jsonl"),
    )


def event():
    return SimpleNamespace(
        event_id="test-event",
        theme="Semiconductor",
        question="테스트 이벤트가 후보 종목에 미치는 영향은?",
    )


def test_disabled_agent_workflow_fails_closed(tmp_path):
    orchestrator = GeminiAgentOrchestrator(make_settings(tmp_path, enabled=False))
    result = orchestrator.analyze_event(event(), ["005930"], [{"symbol": "005930"}])

    assert result["confidence"] == 0.0
    assert result["buy_allowed"] is False
    assert result["block_reason"] == "gemini_api2_disabled"


def test_api2_compatibility_key_and_request_body(monkeypatch):
    monkeypatch.delenv("GEMINI_API2_KEY", raising=False)
    monkeypatch.setenv("Gemini_Api2", "legacy-key")
    assert env_first("GEMINI_API2_KEY", "Gemini_Api2") == "legacy-key"

    client = GeminiAPI2Client(api_key="test-key", model="gemini-test", max_output_tokens=99)
    body = client.build_request_body(
        "input",
        system_instruction="role",
        response_schema={"type": "object"},
    )
    assert body["systemInstruction"]["parts"][0]["text"] == "role"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["maxOutputTokens"] == 99
    assert client.capabilities["token_level_speculative_decoding"] is False


def test_all_registered_prompt_assets_are_api2_only_and_order_safe():
    root = Path("ai_prompts")
    registry = json.loads((root / "agent_registry.json").read_text(encoding="utf-8"))
    loader = PromptLoader(root)
    assert registry["direct_order_execution"] is False
    assert len(registry["agents"]) == 28
    for entry in registry["agents"]:
        prompt = loader.load(entry["path"])
        assert prompt.metadata["id"] == entry["id"]
        assert prompt.metadata["provider"] == "gemini_api2"
        assert prompt.metadata["direct_order_execution"] is False
        assert "JSON" in prompt.body


def test_full_agent_workflow_uses_structured_decision_and_risk_gate(tmp_path, monkeypatch):
    orchestrator = GeminiAgentOrchestrator(make_settings(tmp_path, enabled=True))
    seen = []
    packets_seen = []

    def fake_generate_json(prompt, **kwargs):
        agent_id = prompt.split("AGENT_ID: ", 1)[1].split("\n", 1)[0]
        seen.append(agent_id)
        packets_seen.append("packet-hash-1" in prompt)
        if agent_id in {"trader", "portfolio_manager"}:
            response = {
                "status": "ok",
                "direction": "buy",
                "target_exposure": 0.4,
                "confidence": 0.8,
                "evidence_ids": ["e-1"],
                "invalidation": ["data becomes stale"],
                "risk_flags": [],
            }
            if agent_id == "portfolio_manager":
                response["approved_for_python_risk_gate"] = True
            return response
        if agent_id == "semantic_risk_filter":
            return {
                "status": "ok",
                "recommendation": "allow",
                "filter_reason": "timing coherent",
                "confidence": 0.8,
                "evidence_ids": ["e-1"],
                "invalidation": ["source revised"],
                "risk_flags": [],
            }
        if agent_id == "risk_manager":
            return {
                "status": "ok",
                "recommendation": "allow",
                "exposure_multiplier_suggestion": 1.0,
                "risk_consensus": "within research limits",
                "risk_flags": [],
            }
        if agent_id == "sentiment_analyst":
            return {"status": "ok", "sentiment_score": 0.2, "risk_flags": []}
        return {"status": "ok", "risk_flags": []}

    monkeypatch.setattr(orchestrator.client, "generate_json", fake_generate_json)
    result = orchestrator.analyze_event(
        event(),
        ["005930"],
        [{"symbol": "005930", "name": "Samsung"}],
        {"symbols": {"005930": {"input_snapshot_hash": "packet-hash-1", "technical_indicators": {"values": {"rsi": 55}}}}},
    )

    assert len(seen) == 19
    assert result["buy_allowed"] is True
    assert result["size_multiplier"] == 1.0
    assert result["confidence"] == 0.8
    assert result["agentic_analysis"]["decision"]["direction"] == "buy"
    assert any(packets_seen)
    assert (tmp_path / "audit.jsonl").exists()
    assert (tmp_path / "memory.jsonl").exists()
    assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) >= 20


def test_compact_live_pipeline_uses_four_contract_checked_agents(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, enabled=True)
    settings.gemini_api2_pipeline = "live_event_research"
    settings.gemini_api2_max_agents_per_run = 4
    orchestrator = GeminiAgentOrchestrator(settings)
    seen = []

    def fake_generate_json(prompt, **_kwargs):
        agent_id = prompt.split("AGENT_ID: ", 1)[1].split("\n", 1)[0]
        cutoff = re.search(r'"data_cutoff_utc":\s*"([^"]+)"', prompt).group(1)
        seen.append(agent_id)
        common = {
            "status": "ok",
            "symbol": "005930",
            "data_cutoff_utc": cutoff,
            "confidence": 0.8,
            "evidence_ids": ["price:005930"],
            "invalidation": ["data becomes stale"],
            "risk_flags": [],
            "requires_python_risk_gate": True,
        }
        if agent_id == "live_trader":
            return {**common, "direction": "buy", "target_exposure": 0.4}
        if agent_id in {"live_risk_manager", "live_semantic_risk_filter"}:
            return {
                **common,
                "recommendation": "allow",
                "size_multiplier": 1.0,
                "filter_reason": "technical_only snapshot is internally consistent",
                "source_timing_valid": True,
                "has_mechanism": True,
                "expected_sign": "same",
                "common_cause_risk": "low",
                "reverse_causality_risk": "low",
                "structural_break_conditions": ["trend reverses"],
            }
        if agent_id == "live_portfolio_manager":
            return {
                **common,
                "direction": "buy",
                "target_exposure": 0.4,
                "approval": "allow",
                "approved_for_python_risk_gate": True,
            }
        raise AssertionError(f"unexpected agent {agent_id}")

    monkeypatch.setattr(orchestrator.client, "generate_json", fake_generate_json)
    result = orchestrator.analyze_event(
        event(),
        ["005930"],
        [{"symbol": "005930", "name": "Samsung"}],
        {
            "symbols": {
                "005930": {
                    "sources": [{"evidence_id": "price:005930"}],
                    "input_snapshot_hash": "packet-hash-1",
                }
            }
        },
    )

    assert seen == ["live_trader", "live_risk_manager", "live_semantic_risk_filter", "live_portfolio_manager"]
    assert result["buy_allowed"] is True
    assert result["selected_symbols"] == ["005930"]
    assert all(item["ok"] for item in result["agentic_analysis"]["live_contract_validation"].values())


def test_semantic_hold_never_increases_exposure():
    decision = apply_semantic_risk_assessment({"verdict": "hold", "risk_flags": ["stale"]})
    assert decision.allowed is False
    assert decision.size_multiplier == 0.0


def test_offline_agents_persist_only_unvalidated_hypotheses(tmp_path, monkeypatch):
    orchestrator = GeminiAgentOrchestrator(make_settings(tmp_path, enabled=True))
    seen = []

    def fake_generate_json(prompt, **kwargs):
        agent_id = prompt.split("AGENT_ID: ", 1)[1].split("\n", 1)[0]
        seen.append(agent_id)
        if agent_id == "distillation_teacher":
            return {
                "status": "ok",
                "candidate_hyperedges": [
                    {"edge_id": "edge-1", "nodes": ["CPI", "005930"], "validation": "hypothesis"}
                ],
                "teacher_examples": [
                    {
                        "run_id": "teacher-1",
                        "target_exposure": 0.2,
                        "confidence": 0.7,
                        "teacher_hypothesis": "test hypothesis",
                    }
                ],
                "risk_flags": [],
            }
        return {"status": "ok", "risk_flags": []}

    monkeypatch.setattr(orchestrator.client, "generate_json", fake_generate_json)
    reports = orchestrator.run_offline_experiments({"scenario": "test"})

    assert seen == ["simulation_designer", "distillation_teacher", "latency_optimizer"]
    assert reports["distillation_teacher"]["status"] == "ok"
    graph_rows = [
        json.loads(line)
        for line in (tmp_path / "hyperedges.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    edge = next(row for row in graph_rows if row["record_type"] == "hyperedge")
    assert edge["active"] is False
    assert len((tmp_path / "teacher_labels.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_post_decision_reflection_creates_append_only_memory(tmp_path, monkeypatch):
    orchestrator = GeminiAgentOrchestrator(make_settings(tmp_path, enabled=True))

    def fake_generate_json(prompt, **kwargs):
        agent_id = prompt.split("AGENT_ID: ", 1)[1].split("\n", 1)[0]
        if agent_id == "reflection":
            return {"status": "ok", "what_happened": "outcome reviewed", "risk_flags": []}
        if agent_id == "memory_curator":
            return {
                "status": "ok",
                "note": {
                    "observation": "earnings surprise changed the trend",
                    "hypothesis": "event timing matters",
                    "tags": ["earnings", "005930"],
                },
                "provenance": {"raw_source_ids": ["e-1"]},
                "candidate_links": [],
                "risk_flags": [],
            }
        raise AssertionError(f"unexpected agent {agent_id}")

    monkeypatch.setattr(orchestrator.client, "generate_json", fake_generate_json)
    reports = orchestrator.run_post_decision(
        {"run_id": "decision-1", "decision_time_utc": "2026-07-15T00:00:00Z"},
        {"outcome_window": "5d", "realized_return": 0.02},
    )

    assert set(reports) == {"reflection", "memory_curator"}
    memory = [json.loads(line) for line in (tmp_path / "memory.jsonl").read_text(encoding="utf-8").splitlines()]
    assert memory[-1]["record_type"] == "reflection_memory"
    assert "event timing matters" in memory[-1]["content"]


def test_simulator_uses_price_time_priority_and_partial_fills():
    book = ContinuousDoubleAuction()
    first_sell = SimOrder(agent_id="seller-1", side="SELL", quantity=3, limit_price=100)
    second_sell = SimOrder(agent_id="seller-2", side="SELL", quantity=3, limit_price=100)
    book.submit(first_sell)
    book.submit(second_sell)
    fills = book.submit(SimOrder(agent_id="buyer", side="BUY", quantity=4, limit_price=101))

    assert [(fill.sell_order_id, fill.quantity) for fill in fills] == [
        (first_sell.order_id, 3.0),
        (second_sell.order_id, 1.0),
    ]
    assert book.snapshot()["asks"][0]["remaining"] == 2.0


def test_simulator_ledger_reserves_cash_and_inventory_before_settlement():
    ledger = SimulationLedger("005930")
    ledger.register("buyer", cash=1_000)
    ledger.register("seller", cash=0, inventory=5)
    book = ContinuousDoubleAuction(ledger=ledger)

    book.submit(SimOrder(agent_id="seller", side="SELL", quantity=5, limit_price=100))
    book.submit(SimOrder(agent_id="buyer", side="BUY", quantity=3, limit_price=110))
    accounts = book.snapshot()["ledger"]

    assert accounts["buyer"]["cash"] == 700.0
    assert accounts["buyer"]["inventory"]["005930"] == 3.0
    assert accounts["seller"]["cash"] == 300.0
    assert accounts["seller"]["inventory"]["005930"] == 2.0
    assert accounts["seller"]["reserved_inventory"]["005930"] == 2.0
