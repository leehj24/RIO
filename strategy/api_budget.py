import json
import datetime as dt
from pathlib import Path
from typing import Dict, Any


class DailyAPIBudget:
    """
    provider별 하루 호출 횟수를 제한한다.

    목적:
    - Google Gemini Search/Grounding: 하루 최대 N회
    - NVIDIA NIM 판단: 하루 최대 N회

    기본값은 각각 10회.
    """

    def __init__(self, path: str = "api_usage_state.json"):
        self.path = Path(path)

    def _today(self) -> str:
        return dt.date.today().isoformat()

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"date": self._today(), "providers": {}}

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {"date": self._today(), "providers": {}}

        if data.get("date") != self._today():
            data = {"date": self._today(), "providers": {}}

        data.setdefault("providers", {})
        return data

    def save(self, data: Dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_count(self, provider: str) -> int:
        data = self.load()
        return int(data.get("providers", {}).get(provider, {}).get("count", 0))

    def remaining(self, provider: str, daily_limit: int) -> int:
        return max(0, int(daily_limit) - self.get_count(provider))

    def can_call(self, provider: str, daily_limit: int) -> bool:
        return self.remaining(provider, daily_limit) > 0

    def record_call(self, provider: str, meta: Dict[str, Any] | None = None) -> None:
        data = self.load()
        providers = data.setdefault("providers", {})
        p = providers.setdefault(provider, {"count": 0, "calls": []})
        p["count"] = int(p.get("count", 0)) + 1
        p.setdefault("calls", []).append({
            "ts": dt.datetime.now().replace(microsecond=0).isoformat(),
            "meta": meta or {},
        })
        self.save(data)

    def snapshot(self, limits: Dict[str, int]) -> Dict[str, Any]:
        data = self.load()
        out = {"date": data.get("date"), "providers": {}}
        for provider, limit in limits.items():
            count = self.get_count(provider)
            out["providers"][provider] = {
                "limit": int(limit),
                "used": count,
                "remaining": max(0, int(limit) - count),
            }
        return out
