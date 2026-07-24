"""Immutable lifecycle records for real broker orders.

The normal trade log is useful for explaining a model decision, but an order
can be accepted, partially filled, cancelled, or reconciled long after that
decision record was written.  This small ledger keeps those facts append-only
and linked by one lifecycle ID.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from strategy.agentic.audit_store import AppendOnlyAuditStore, utc_now


class OrderLifecycleLedger:
    """Write durable, append-only events for one broker-order lifecycle."""

    def __init__(self, path: str | Path = "data/order_lifecycle.jsonl", *, durable: bool = True):
        self.store = AppendOnlyAuditStore(path, durable=durable)

    def record(self, event_type: str, lifecycle_id: str, **fields: Any) -> dict[str, Any]:
        if not lifecycle_id:
            raise ValueError("lifecycle_id is required")
        return self.store.append_event(
            event_type,
            lifecycle_id=str(lifecycle_id),
            occurred_at_utc=utc_now(),
            **fields,
        )
