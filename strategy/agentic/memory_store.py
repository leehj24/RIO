"""Append-only, searchable agent memory backed by portable JSONL."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .audit_store import _exclusive_lock, json_safe_copy, utc_now


class MemoryReadError(ValueError):
    """An existing memory JSONL file contains an invalid record."""


_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z_가-힣]+", flags=re.UNICODE)


def _tokens(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {token.casefold() for token in _TOKEN_PATTERN.findall(value)}


def _normalise_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError("tags must be an array of non-empty strings")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("tags must be an array of non-empty strings")
        tag = item.strip()
        if tag not in tags:
            tags.append(tag)
    return tags


def _parse_timestamp(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Timestamp filters must be ISO-8601 strings")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise ValueError("Timestamp filters must be ISO-8601 strings") from exc


class AgentMemoryStore:
    """Store immutable observations, hypotheses, decisions, and outcomes.

    Corrections must be added as a new note with ``supersedes`` set to the old
    ``memory_id``.  Existing records are never rewritten, so a later reflection
    can reconstruct what an agent knew at the time.
    """

    def __init__(self, path: str | Path = "data/agentic/memory.jsonl", *, durable: bool = True):
        self.path = Path(path)
        self.durable = durable
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.link_path = self.path.with_name(f"{self.path.stem}_links.jsonl")
        self._link_lock_path = self.link_path.with_name(f"{self.link_path.name}.lock")

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Append an atomic memory note and return the exact persisted record."""

        persisted = json_safe_copy(record)
        content = persisted.get("content", persisted.get("summary"))
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Memory record requires a non-empty 'content' or 'summary' string")
        persisted.setdefault("content", content.strip())
        persisted["tags"] = _normalise_tags(persisted.get("tags"))
        persisted.setdefault("memory_id", uuid.uuid4().hex)
        if not isinstance(persisted["memory_id"], str) or not persisted["memory_id"].strip():
            raise ValueError("memory_id must be a non-empty string")
        persisted.setdefault("created_at_utc", utc_now())
        persisted.setdefault("record_type", "memory_note")

        # IDs are the stable targets of `supersedes` and links.  Checking under
        # the same append lock makes accidental duplicate IDs fail loudly.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self._lock_path):
            if self.get(persisted["memory_id"]) is not None:
                raise ValueError(f"memory_id already exists: {persisted['memory_id']}")
            import json
            import os

            line = json.dumps(persisted, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                if self.durable:
                    os.fsync(handle.fileno())
        return persisted

    def append_note(
        self,
        content: str,
        *,
        tags: Sequence[str] | None = None,
        record_type: str = "memory_note",
        **metadata: Any,
    ) -> dict[str, Any]:
        return self.append({"content": content, "tags": list(tags or ()), "record_type": record_type, **metadata})

    def append_derived_note(
        self,
        content: str,
        *,
        source_memory_ids: Sequence[str],
        version_reason: str,
        tags: Sequence[str] | None = None,
        **metadata: Any,
    ) -> dict[str, Any]:
        """Append a versioned interpretation without altering source records."""

        source_ids = [str(memory_id).strip() for memory_id in source_memory_ids if str(memory_id).strip()]
        if not source_ids:
            raise ValueError("derived notes require at least one source_memory_id")
        missing = [memory_id for memory_id in source_ids if self.get(memory_id) is None]
        if missing:
            raise ValueError(f"derived note references unknown memory IDs: {', '.join(missing)}")
        return self.append_note(
            content,
            tags=tags,
            record_type="derived_memory_note",
            derived_from=source_ids,
            version_reason=str(version_reason),
            **metadata,
        )

    def append_link(
        self,
        source_memory_id: str,
        target_memory_id: str,
        *,
        relation: str,
        reason: str = "",
        confidence: float | None = None,
        valid_from: str = "",
        valid_to: str = "",
    ) -> dict[str, Any]:
        """Persist a separately auditable link between two immutable notes."""

        source = str(source_memory_id).strip()
        target = str(target_memory_id).strip()
        if not source or not target or source == target:
            raise ValueError("memory links require two distinct non-empty memory IDs")
        if relation not in {"supports", "contradicts", "related"}:
            raise ValueError("relation must be supports, contradicts, or related")
        if self.get(source) is None or self.get(target) is None:
            raise ValueError("memory links must reference existing memory IDs")
        if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("link confidence must be in [0, 1]")
        record = {
            "record_type": "memory_link",
            "link_id": uuid.uuid4().hex,
            "created_at_utc": utc_now(),
            "source_memory_id": source,
            "target_memory_id": target,
            "relation": relation,
            "reason": str(reason),
            "confidence": None if confidence is None else float(confidence),
            "valid_from": str(valid_from),
            "valid_to": str(valid_to),
        }
        self.link_path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self._link_lock_path):
            import json
            import os

            line = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
            with self.link_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                if self.durable:
                    os.fsync(handle.fileno())
        return record

    def links_for(self, memory_id: str) -> list[dict[str, Any]]:
        """Return link records touching a note, preserving append order."""

        if not self.link_path.exists():
            return []
        import json

        links: list[dict[str, Any]] = []
        for line in self.link_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and memory_id in {row.get("source_memory_id"), row.get("target_memory_id")}:
                links.append(row)
        return links

    def iter_records(self, *, strict: bool = True) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        import json

        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    if strict:
                        raise MemoryReadError(f"Invalid JSON at {self.path}:{line_number}: {exc.msg}") from exc
                    continue
                if not isinstance(record, dict):
                    if strict:
                        raise MemoryReadError(f"Non-object record at {self.path}:{line_number}")
                    continue
                yield record

    def get(self, memory_id: str) -> dict[str, Any] | None:
        if not isinstance(memory_id, str) or not memory_id:
            return None
        for record in self.iter_records():
            if record.get("memory_id") == memory_id:
                return record
        return None

    def retrieve(
        self,
        query: str = "",
        *,
        limit: int = 5,
        tags: Sequence[str] | None = None,
        record_types: Sequence[str] | None = None,
        before_utc: str | None = None,
        after_utc: str | None = None,
        include_scores: bool = False,
    ) -> list[dict[str, Any]]:
        """Return deterministic lexical matches, newest first when tied.

        This is intentionally a local retrieval baseline.  An embedding index
        can be added later without changing the immutable source-of-truth JSONL
        records.
        """

        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        tag_filter = {tag.casefold() for tag in _normalise_tags(tags)}
        type_filter = set(record_types or ())
        if any(not isinstance(value, str) or not value for value in type_filter):
            raise ValueError("record_types must be an array of non-empty strings")
        before = _parse_timestamp(before_utc)
        after = _parse_timestamp(after_utc)
        if before and after and after > before:
            raise ValueError("after_utc must not be later than before_utc")

        query_tokens = _tokens(query)
        ranked: list[tuple[float, int, dict[str, Any]]] = []
        for order, record in enumerate(self.iter_records()):
            stored_tags = {tag.casefold() for tag in record.get("tags", []) if isinstance(tag, str)}
            if tag_filter and not tag_filter.issubset(stored_tags):
                continue
            if type_filter and record.get("record_type") not in type_filter:
                continue
            timestamp = _parse_timestamp(record.get("created_at_utc"))
            if before and timestamp and timestamp > before:
                continue
            if after and timestamp and timestamp < after:
                continue

            searchable = " ".join(
                str(record.get(key, "")) for key in ("content", "summary", "hypothesis", "outcome", "invalidation")
            )
            searchable_tokens = _tokens(searchable)
            matched = query_tokens & searchable_tokens
            score = float(len(matched))
            if query.strip() and query.casefold() in searchable.casefold():
                score += 2.0
            score += 2.0 * len(query_tokens & stored_tags)
            if query_tokens and score <= 0:
                continue
            ranked.append((score, order, record))

        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        results: list[dict[str, Any]] = []
        for score, _, record in ranked[:limit]:
            result = dict(record)
            if include_scores:
                result["retrieval_score"] = score
            results.append(result)
        return results
