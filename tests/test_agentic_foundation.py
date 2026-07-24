import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy.agentic.audit_store import AppendOnlyAuditStore
from strategy.agentic.memory_store import AgentMemoryStore
from strategy.agentic.prompt_loader import PromptFrontMatterError, PromptLoader, load_prompt
from strategy.agentic.schemas import DecisionValidationError, validate_agent_decision


def test_prompt_loader_parses_metadata_and_renders_nested_context(tmp_path: Path):
    prompt = tmp_path / "agents" / "example.md"
    prompt.parent.mkdir()
    prompt.write_text(
        """---
id: example
enabled: true
tags: [research, \"시장 분석\"]
settings:
  temperature: 0.1
---
종목={{ snapshot.symbol }}\n자료={{ snapshot.evidence | json }}
""",
        encoding="utf-8",
    )

    loaded = PromptLoader(tmp_path).load("agents/example")
    assert loaded.metadata["enabled"] is True
    assert loaded.metadata["settings"]["temperature"] == 0.1
    rendered = loaded.render({"snapshot": {"symbol": "005930", "evidence": ["e-1"]}})
    assert "종목=005930" in rendered
    assert '자료=["e-1"]' in rendered
    assert len(loaded.source_hash) == 64


def test_prompt_loader_rejects_unclosed_front_matter(tmp_path: Path):
    prompt = tmp_path / "bad.md"
    prompt.write_text("---\nid: bad\nbody", encoding="utf-8")
    with pytest.raises(PromptFrontMatterError):
        load_prompt(prompt)


def test_agent_decision_accepts_fenced_json_and_normalizes_contract():
    decision = validate_agent_decision(
        """```json
        {"direction":"매수","target_exposure":"0.25","confidence":0.8,
         "evidence_ids":["news-1"],"invalidation":["earnings miss"],"risk_flags":[]}
        ```"""
    )
    assert decision.direction == "buy"
    assert decision.target_exposure == 0.25
    assert decision.invalidation == ("earnings miss",)
    assert decision.to_dict()["risk_flags"] == []


def test_agent_decision_rejects_unsafe_or_incomplete_values():
    with pytest.raises(DecisionValidationError) as error:
        validate_agent_decision(
            {
                "direction": "buy",
                "target_exposure": 1.2,
                "confidence": 0.5,
                "evidence_ids": [],
                "invalidation": [],
                "risk_flags": [],
            }
        )
    assert "target_exposure" in error.value.issues
    assert "invalidation" in error.value.issues


def test_audit_store_appends_without_mutating_previous_records(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    store = AppendOnlyAuditStore(path, durable=False)
    first = store.append({"event_type": "agent_response", "model": "gemini-api2"})
    second = store.append_event("risk_gate", allowed=False)

    records = store.read_all()
    assert [record["audit_id"] for record in records] == [first["audit_id"], second["audit_id"]]
    assert records[0]["model"] == "gemini-api2"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_memory_store_is_append_only_and_retrieves_tagged_notes(tmp_path: Path):
    path = tmp_path / "memory.jsonl"
    store = AgentMemoryStore(path, durable=False)
    first = store.append_note("CPI surprise pressured growth shares", tags=["macro", "cpi"])
    store.append_note("Semiconductor demand remained resilient", tags=["sector"])

    matches = store.retrieve("CPI growth", tags=["macro"], include_scores=True)
    assert [match["memory_id"] for match in matches] == [first["memory_id"]]
    assert matches[0]["retrieval_score"] > 0
    with pytest.raises(ValueError, match="already exists"):
        store.append({"memory_id": first["memory_id"], "content": "duplicate", "tags": []})

    persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(persisted) == 2
