"""Durable append-only JSONL audit records for the agent workflow."""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class AuditReadError(ValueError):
    """An existing audit JSONL file contains an invalid record."""


_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(path.resolve(), threading.RLock())


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Use a small sidecar lock for best-effort inter-process JSONL appends."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _path_lock(lock_path)
    with process_lock:
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            unlock: Any = None
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

                    def unlock() -> None:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

                    def unlock() -> None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

                yield
            finally:
                if unlock is not None:
                    unlock()


def utc_now() -> str:
    """Return a timezone-aware, sortable timestamp with a UTC ``Z`` suffix."""

    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_safe_copy(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a record through strict JSON to reject unserialisable audit data."""

    if not isinstance(record, Mapping):
        raise TypeError("Audit record must be a mapping")
    try:
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Audit record must be JSON serializable: {exc}") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # Defensive: a Mapping must encode to an object.
        raise TypeError("Audit record must encode to a JSON object")
    return decoded


class AppendOnlyAuditStore:
    """Append audit events as one JSON object per line.

    The public API never updates or deletes an existing record.  A sidecar lock
    keeps writers from this project serialized across processes; the JSONL file
    itself remains portable and inspectable with ordinary tools.
    """

    def __init__(self, path: str | Path = "data/agentic/audit.jsonl", *, durable: bool = True):
        self.path = Path(path)
        self.durable = durable
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one copied record and return the exact object written."""

        persisted = json_safe_copy(record)
        persisted.setdefault("audit_id", uuid.uuid4().hex)
        persisted.setdefault("recorded_at_utc", utc_now())
        line = json.dumps(persisted, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self._lock_path):
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                if self.durable:
                    os.fsync(handle.fileno())
        return persisted

    def append_event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        return self.append({"event_type": event_type.strip(), **fields})

    def iter_records(self, *, strict: bool = True) -> Iterator[dict[str, Any]]:
        """Yield records in write order; invalid historical lines are surfaced."""

        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    if strict:
                        raise AuditReadError(f"Invalid JSON at {self.path}:{line_number}: {exc.msg}") from exc
                    continue
                if not isinstance(record, dict):
                    if strict:
                        raise AuditReadError(f"Non-object record at {self.path}:{line_number}")
                    continue
                yield record

    def read_all(self, *, strict: bool = True) -> list[dict[str, Any]]:
        return list(self.iter_records(strict=strict))
