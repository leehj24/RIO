"""Weekly-return-bin forecasts and immutable evaluation records.

This implements the evaluation contract documented in
``ai_prompot/07_원문확인.md``.  It is a research evaluator, not an order
signal: a 13-bin forecast is logged at decision time and can only be scored
after its future observation window has closed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from strategy.agentic.audit_store import _exclusive_lock, json_safe_copy, utc_now


RETURN_BINS: tuple[str, ...] = ("D5-", "D5", "D4", "D3", "D2", "D1", "N", "U1", "U2", "U3", "U4", "U5", "U5+")
_BIN_INDEX = {name: index for index, name in enumerate(RETURN_BINS)}


def weekly_return_bin(return_pct: float) -> str:
    """Map a percentage return to the paper's 13 bins.

    Boundaries are deliberately explicit: ``-5.5`` belongs to ``D5-`` and
    ``+5.5`` belongs to ``U5+``.
    """

    value = float(return_pct)
    if value <= -5.5:
        return "D5-"
    if value <= -4.5:
        return "D5"
    if value <= -3.5:
        return "D4"
    if value <= -2.5:
        return "D3"
    if value <= -1.5:
        return "D2"
    if value < -0.5:
        return "D1"
    if value < 0.5:
        return "N"
    if value < 1.5:
        return "U1"
    if value < 2.5:
        return "U2"
    if value < 3.5:
        return "U3"
    if value < 4.5:
        return "U4"
    if value < 5.5:
        return "U5"
    return "U5+"


def bin_direction(bin_name: str) -> str:
    if bin_name not in _BIN_INDEX:
        raise ValueError(f"unknown return bin: {bin_name}")
    return "down" if _BIN_INDEX[bin_name] < _BIN_INDEX["N"] else "up" if _BIN_INDEX[bin_name] > _BIN_INDEX["N"] else "neutral"


def direction_hit(predicted_bin: str, actual_bin: str) -> bool:
    return bin_direction(predicted_bin) == bin_direction(actual_bin)


def bin_score(predicted_bin: str, actual_bin: str) -> int:
    """``max(0, 5 - abs(predicted_index - actual_index))``."""

    if predicted_bin not in _BIN_INDEX or actual_bin not in _BIN_INDEX:
        raise ValueError("predicted_bin and actual_bin must be known 13-bin labels")
    return max(0, 5 - abs(_BIN_INDEX[predicted_bin] - _BIN_INDEX[actual_bin]))


def five_point_trimmed_mean(values: Sequence[float]) -> float:
    """Paper convention for five predictions: remove high/low and average 3."""

    if len(values) != 5:
        raise ValueError("five_point_trimmed_mean requires exactly five values")
    numeric = sorted(float(value) for value in values)
    return sum(numeric[1:-1]) / 3.0


@dataclass(frozen=True)
class ForecastRecord:
    run_id: str
    decision_time_utc: str
    symbol: str
    decision_price: float
    predicted_bin: str
    confidence: float
    input_snapshot_hash: str
    horizon: str = "5_trading_days"
    actual_price: float | None = None
    actual_bin: str | None = None
    evaluated_at_utc: str | None = None

    def __post_init__(self) -> None:
        if self.predicted_bin not in _BIN_INDEX and self.predicted_bin != "unknown":
            raise ValueError("predicted_bin must be one of the 13 labels or unknown")
        if float(self.decision_price) <= 0.0:
            raise ValueError("decision_price must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ForecastLedger:
    """Append-only forecast records; evaluation is an additional record.

    The original decision is never overwritten.  A later evaluator appends a
    record with ``record_type=forecast_outcome`` linked through ``run_id`` and
    the decision record's immutable input hash.
    """

    def __init__(self, path: str | Path = "data/forecast_ledger.jsonl", *, durable: bool = True):
        self.path = Path(path)
        self.durable = durable
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")

    def _append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        persisted = json_safe_copy(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(persisted, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        with _exclusive_lock(self._lock_path):
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                if self.durable:
                    import os

                    os.fsync(handle.fileno())
        return persisted

    def append_prediction(self, forecast: ForecastRecord) -> dict[str, Any]:
        return self._append({"record_type": "forecast_prediction", "recorded_at_utc": utc_now(), **forecast.to_dict()})

    def append_outcome(
        self,
        forecast: ForecastRecord,
        *,
        actual_price: float,
        evaluated_at_utc: str | None = None,
    ) -> dict[str, Any]:
        price = float(actual_price)
        if price <= 0.0:
            raise ValueError("actual_price must be positive")
        realized_return_pct = (price / float(forecast.decision_price) - 1.0) * 100.0
        actual = weekly_return_bin(realized_return_pct)
        payload: dict[str, Any] = {
            "record_type": "forecast_outcome",
            "recorded_at_utc": utc_now(),
            "run_id": forecast.run_id,
            "symbol": forecast.symbol,
            "input_snapshot_hash": forecast.input_snapshot_hash,
            "decision_time_utc": forecast.decision_time_utc,
            "horizon": forecast.horizon,
            "decision_price": float(forecast.decision_price),
            "actual_price": price,
            "realized_return_pct": realized_return_pct,
            "actual_bin": actual,
            "evaluated_at_utc": evaluated_at_utc or utc_now(),
        }
        if forecast.predicted_bin != "unknown":
            payload.update(
                {
                    "predicted_bin": forecast.predicted_bin,
                    "direction_hit": direction_hit(forecast.predicted_bin, actual),
                    "bin_score": bin_score(forecast.predicted_bin, actual),
                }
            )
        return self._append(payload)

    def records(self) -> Iterable[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows
