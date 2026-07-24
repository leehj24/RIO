"""Append-only Teacher labels for later, independently validated distillation.

This is deliberately not a live 'student' model.  It creates reproducible
training candidates after the Gemini Teacher decision and its eventual market
outcome are known, matching the staged deployment recommended by reference 08.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List
import json


class TeacherLabelStore:
    def __init__(self, path: str = "data_cache/agentic/teacher_labels.jsonl") -> None:
        self.path = Path(path)

    def append(self, label: Dict[str, Any]) -> None:
        required = {"run_id", "data_cutoff_utc", "target_exposure", "confidence"}
        missing = sorted(key for key in required if key not in label)
        if missing:
            raise ValueError(f"teacher label missing required fields: {', '.join(missing)}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(label, ensure_ascii=False) + "\n")

    def pending_outcomes(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("realized_return") is None:
                rows.append(row)
        return rows
