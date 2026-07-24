"""Evidence-backed hyperedge candidates for the slow (post-market) path.

Gemini may propose causal hypotheses, but this store never activates an edge
without a numerical validation metric supplied by code.  It is intentionally a
small persistence primitive rather than a claimed causal-inference engine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List
import json


def regime_weight(structural_strength: float, regime_attention: float, *, alpha: float = 0.5, beta: float = 0.5) -> float:
    """Compute the documented graph weight ``alpha*S + beta*R(regime)``.

    Inputs are expected to be independently normalised scores.  This helper
    records a transparent calculation; it does not itself infer causality or
    activate an edge.
    """
    if alpha < 0.0 or beta < 0.0 or alpha + beta <= 0.0:
        raise ValueError("alpha and beta must be non-negative with positive total")
    return float(alpha * float(structural_strength) + beta * float(regime_attention))


@dataclass
class GraphNode:
    node_id: str
    node_type: str
    label: str
    observed_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"record_type": "graph_node", **asdict(self)}


@dataclass
class Hyperedge:
    edge_id: str
    node_ids: List[str]
    direction: str
    rationale: str
    evidence_ids: List[str] = field(default_factory=list)
    validation_metric: str = ""
    p_value: float | None = None
    active: bool = False
    valid_from: str = ""
    valid_to: str = ""
    validation_metadata: Dict[str, Any] = field(default_factory=dict)
    regime: str = ""
    score: float | None = None
    version: int = 1

    def validate(
        self,
        *,
        metric: str,
        p_value: float,
        alpha: float = 0.05,
        metadata: Dict[str, Any] | None = None,
        score: float | None = None,
    ) -> None:
        """Activate only a numerically validated, time-bounded hypothesis.

        ``metadata`` records the window, lag, estimator, FDR policy and any
        regime condition used by the validator.  A raw LLM rationale alone is
        never enough to activate an edge.
        """
        self.validation_metric = metric
        self.p_value = float(p_value)
        self.active = 0.0 <= self.p_value <= float(alpha)
        if metadata is not None:
            self.validation_metadata = dict(metadata)
        if score is not None:
            self.score = float(score)

    def is_active_at(self, timestamp: str | None = None) -> bool:
        """Check active flag and optional validity interval lexicographically.

        ISO-8601 UTC timestamps sort lexicographically, which keeps this
        lightweight persistence primitive free of a date-parser dependency.
        """
        if not self.active:
            return False
        if not timestamp:
            return True
        if self.valid_from and timestamp < self.valid_from:
            return False
        return not (self.valid_to and timestamp > self.valid_to)

    def to_dict(self) -> Dict[str, Any]:
        return {"record_type": "hyperedge", **asdict(self)}


class HypergraphStore:
    def __init__(self, path: str = "data_cache/agentic/hyperedges.jsonl") -> None:
        self.path = Path(path)

    def append(self, edge: Hyperedge) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(edge.to_dict(), ensure_ascii=False) + "\n")

    def append_node(self, node: GraphNode) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(node.to_dict(), ensure_ascii=False) + "\n")

    def active_edges(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("record_type", "hyperedge") == "hyperedge" and row.get("active"):
                rows.append(row)
        return rows
