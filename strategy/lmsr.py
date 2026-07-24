import math
from dataclasses import dataclass
from typing import Optional, Dict, Any
import numpy as np


@dataclass
class LMSRMarket:
    event_id: str
    question: str
    b: float = 100.0
    q_yes: float = 0.0
    q_no: float = 0.0

    def cost(self, q_yes: Optional[float] = None, q_no: Optional[float] = None) -> float:
        qy = self.q_yes if q_yes is None else q_yes
        qn = self.q_no if q_no is None else q_no
        return self.b * math.log(math.exp(qy / self.b) + math.exp(qn / self.b))

    def yes_price(self) -> float:
        ey = math.exp(self.q_yes / self.b)
        en = math.exp(self.q_no / self.b)
        return ey / (ey + en)

    def update_toward_probability(self, llm_prob: float, alpha: float = 0.40) -> Dict[str, Any]:
        llm_prob = float(np.clip(llm_prob, 0.01, 0.99))
        before = self.yes_price()
        target = before + alpha * (llm_prob - before)
        target = float(np.clip(target, 0.01, 0.99))

        current_diff = self.q_yes - self.q_no
        target_diff = self.b * math.log(target / (1.0 - target))
        delta = target_diff - current_diff

        if delta >= 0:
            self.q_yes += delta
            side = "YES"
        else:
            self.q_no += abs(delta)
            side = "NO"

        after = self.yes_price()
        return {
            "before": before,
            "llm_prob": llm_prob,
            "target": target,
            "after": after,
            "virtual_side": side,
            "virtual_delta": abs(delta),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "question": self.question,
            "b": self.b,
            "q_yes": self.q_yes,
            "q_no": self.q_no,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "LMSRMarket":
        return LMSRMarket(
            event_id=d["event_id"],
            question=d["question"],
            b=float(d.get("b", 100.0)),
            q_yes=float(d.get("q_yes", 0.0)),
            q_no=float(d.get("q_no", 0.0)),
        )
