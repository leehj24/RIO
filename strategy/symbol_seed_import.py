"""Safe import of a user-supplied global symbol seed into ``symbols.csv``.

The project can display research candidates from non-KR/US exchanges, but the
current Toss execution path only supports KR and US symbols.  This importer
therefore keeps every imported seed row disabled and explicitly marks it as a
research-only candidate.  It never turns a row into an executable order.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


SYMBOL_COLUMNS: tuple[str, ...] = (
    "symbol",
    "name",
    "raw_name",
    "market",
    "sector",
    "theme",
    "enabled",
    "resolved",
    "note",
)


class SymbolSeedImportError(ValueError):
    """The seed cannot be safely merged into the project symbol registry."""


def _text(value: Any, field: str, *, required: bool = False, maximum: int = 500) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SymbolSeedImportError(f"{field} must be text")
    result = value.strip()
    if required and not result:
        raise SymbolSeedImportError(f"{field} is required")
    if len(result) > maximum:
        raise SymbolSeedImportError(f"{field} must be at most {maximum} characters")
    if any(ord(character) < 32 for character in result):
        raise SymbolSeedImportError(f"{field} must not contain control characters")
    return result


def read_symbol_rows(path: str | Path) -> list[dict[str, str]]:
    source = Path(path)
    if not source.exists():
        raise SymbolSeedImportError(f"CSV file not found: {source}")
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != SYMBOL_COLUMNS:
            raise SymbolSeedImportError(
                f"{source} header must be exactly: {', '.join(SYMBOL_COLUMNS)}"
            )
        return [dict(row) for row in reader]


def _normalise_candidate(row: Mapping[str, Any]) -> dict[str, str]:
    symbol = _text(row.get("symbol"), "symbol", required=True, maximum=80)
    market = _text(row.get("market"), "market", required=True, maximum=32).upper()
    name = _text(row.get("name"), "name", required=True, maximum=200)
    raw_name = _text(row.get("raw_name"), "raw_name", maximum=200) or name
    note = _text(row.get("note"), "note", maximum=1_000)

    safety_markers = [
        "seed_candidate",
        f"country_market={market}",
        "research_only",
        "current_toss_live_support=KR_US_only",
    ]
    note_parts = [part for part in note.split(";") if part]
    for marker in safety_markers:
        if marker not in note_parts:
            note_parts.append(marker)

    return {
        "symbol": symbol,
        "name": name,
        "raw_name": raw_name,
        "market": market,
        "sector": _text(row.get("sector"), "sector", maximum=160),
        "theme": _text(row.get("theme"), "theme", maximum=160),
        # A seed is a research universe, never a live-order approval.
        "enabled": "false",
        "resolved": "unresolved",
        "note": ";".join(note_parts),
    }


def prepare_seed_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Normalise rows and reject duplicate source symbols before merging."""

    prepared = [_normalise_candidate(row) for row in rows]
    counts = Counter(row["symbol"] for row in prepared)
    duplicates = sorted(symbol for symbol, count in counts.items() if count > 1)
    if duplicates:
        raise SymbolSeedImportError(f"duplicate symbols in seed: {', '.join(duplicates[:10])}")
    return prepared


def preview_seed_merge(
    *,
    seed_path: str | Path = "global_theme_universe_seed.csv",
    symbols_path: str | Path = "data/symbols.csv",
) -> dict[str, Any]:
    """Validate a merge and return its effect without changing any file."""

    existing = read_symbol_rows(symbols_path)
    incoming = prepare_seed_rows(read_symbol_rows(seed_path))
    current_symbols = {row.get("symbol", "").strip() for row in existing if row.get("symbol", "").strip()}
    to_add = [row for row in incoming if row["symbol"] not in current_symbols]
    skipped = [row["symbol"] for row in incoming if row["symbol"] in current_symbols]
    issuer_name_overlap = sorted(
        {
            row["name"]
            for row in to_add
            if row["name"].casefold()
            in {
                (existing_row.get("name") or existing_row.get("raw_name") or "").strip().casefold()
                for existing_row in existing
            }
        }
    )
    return {
        "seed_path": str(seed_path),
        "symbols_path": str(symbols_path),
        "existing_rows": len(existing),
        "seed_rows": len(incoming),
        "add_rows": len(to_add),
        "skip_existing_symbol_rows": len(skipped),
        "skip_existing_symbols_sample": skipped[:20],
        "issuer_name_overlap_count": len(issuer_name_overlap),
        "issuer_name_overlap": issuer_name_overlap,
        "all_imported_rows_disabled": all(row["enabled"] == "false" for row in to_add),
        "market_counts": dict(sorted(Counter(row["market"] for row in to_add).items())),
    }


def merge_seed_into_symbols(
    *,
    seed_path: str | Path = "global_theme_universe_seed.csv",
    symbols_path: str | Path = "data/symbols.csv",
) -> dict[str, Any]:
    """Atomically append new, disabled seed candidates to ``symbols.csv``."""

    report = preview_seed_merge(seed_path=seed_path, symbols_path=symbols_path)
    existing = read_symbol_rows(symbols_path)
    incoming = prepare_seed_rows(read_symbol_rows(seed_path))
    current_symbols = {row.get("symbol", "").strip() for row in existing if row.get("symbol", "").strip()}
    to_add = [row for row in incoming if row["symbol"] not in current_symbols]

    destination = Path(symbols_path)
    temporary = destination.with_name(f".{destination.name}.seed-import.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(SYMBOL_COLUMNS), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(existing)
            writer.writerows(to_add)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()

    return {
        **report,
        "imported_rows": len(to_add),
        "total_rows_after_import": len(existing) + len(to_add),
    }
