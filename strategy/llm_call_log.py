import json
import datetime as dt
from pathlib import Path
from typing import Dict, Any


class LLMCallLogger:
    def __init__(self, path: str = "llm_calls.jsonl"):
        self.path = Path(path)

    def log(self, row: Dict[str, Any]) -> None:
        row = {
            "ts": dt.datetime.now().replace(microsecond=0).isoformat(),
            **row,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
