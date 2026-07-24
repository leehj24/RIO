import csv
import re
from pathlib import Path
from typing import Dict, List, Optional


EXECUTION_MARKETS = frozenset({"KR", "US"})


def _bool(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def _norm(v: str) -> str:
    return str(v or "").strip()


def is_execution_market(market: str) -> bool:
    """Return whether the current Toss order path explicitly supports a market."""

    return _norm(market).upper() in EXECUTION_MARKETS


def load_symbols(
    path: str = "data/symbols.csv",
    *,
    include_disabled: bool = False,
    include_unresolved: bool = False,
    market: Optional[str] = None,
    theme: Optional[str] = None,
) -> Dict[str, dict]:
    """
    symbols.csv를 읽는다.

    strategy에서 실제 매매 대상으로 쓸 때는:
    - enabled=true
    - symbol 값 존재
    인 row만 사용한다.

    대시보드에서 전체 watchlist 확인할 때는 include_disabled=True, include_unresolved=True 사용.
    """
    p = Path(path)
    if not p.exists():
        return {}

    out: Dict[str, dict] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = _norm(row.get("symbol"))
            enabled = _bool(row.get("enabled", "true"))
            row_market = _norm(row.get("market"))
            row_theme = _norm(row.get("theme"))

            if not include_disabled and not enabled:
                continue
            # A candidate from a local exchange (JP, IN, CN, ...) may be shown
            # in the dashboard, but must not enter the KR/US-only execution
            # universe even if somebody accidentally flips enabled=true.
            if enabled and not is_execution_market(row_market):
                continue
            if not include_unresolved and not symbol:
                continue
            if market and row_market != market:
                continue
            if theme and row_theme != theme:
                continue

            key = symbol or _norm(row.get("raw_name")) or _norm(row.get("name"))
            if key:
                out[key] = row
    return out


def load_watchlist_raw(path: str = "data/watchlist_raw.csv") -> List[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def enabled_symbols(path: str = "data/symbols.csv") -> List[str]:
    return list(load_symbols(path).keys())


def symbols_by_market(market: str, path: str = "data/symbols.csv") -> List[str]:
    return list(load_symbols(path, market=market).keys())


def symbols_by_theme(theme: str, path: str = "data/symbols.csv") -> List[str]:
    return list(load_symbols(path, theme=theme).keys())


def symbols_by_sector(sector: str, path: str = "data/symbols.csv") -> List[str]:
    """Return enabled execution symbols that share the given sector label."""
    return [
        symbol
        for symbol, row in load_symbols(path).items()
        if _norm(row.get("sector")) == _norm(sector)
    ]


def country_exposure(row: dict) -> str:
    """Return the investment-country label without changing the order market.

    Country ETFs use ``theme=Country: …``.  A company can retain its ordinary
    strategy theme (such as ``AI Semiconductor``) and declare the country in
    its note, so it remains available to both thematic and country events.
    """

    theme = _norm(row.get("theme"))
    match = re.match(r"^Country:\s*(.+)$", theme, flags=re.IGNORECASE)
    if match:
        return _norm(match.group(1)).replace("_", " ")

    note = _norm(row.get("note"))
    match = re.search(r"(?:^|;)country_exposure=([^;]+)", note, flags=re.IGNORECASE)
    return _norm(match.group(1)).replace("_", " ") if match else ""


def symbols_by_country(country: str, path: str = "data/symbols.csv") -> List[str]:
    """Return enabled Toss-executable symbols for one country or ``ALL``."""

    target = _norm(country).replace("_", " ").casefold()
    return [
        symbol
        for symbol, row in load_symbols(path).items()
        if (exposure := country_exposure(row))
        and (target == "all" or exposure.casefold() == target)
    ]


def describe_symbol(symbol: str, path: str = "data/symbols.csv") -> dict:
    data = load_symbols(path, include_disabled=True, include_unresolved=True)
    return data.get(symbol, {"symbol": symbol})
