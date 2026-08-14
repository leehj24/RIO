"""AI-driven, registry-external cash symbol discovery.

See system/14_1분봉단타실행엔진및확률모델설계.md 13.5 for the design rationale.

Flow (once per Korean research day, cached in state["cash_symbol_discovery_cache"]):

1. Ask an AI model to propose up to
   ``settings.cash_symbol_discovery_max_candidates`` stock symbols that may
   not yet be in ``data/symbols.csv``, using only qualitative reasoning
   (liquidity fit / GBM momentum / OU mean-reversion) about the current
   affordable cash budget. The model is never told or asked to invent a
   price, probability, or expected return.
2. Prefer Gemini (``GeminiAPI2Client``, real web-search grounding via
   ``ENABLE_GEMINI_API2_GROUNDING``) when
   ``settings.cash_symbol_discovery_prefer_gemini`` and a Gemini API2 key
   is configured. Fall back to the existing NVIDIA orchestrator's
   standalone ``cash_symbol_discovery`` pipeline (no live search, model's
   trained knowledge only) when Gemini is unavailable or fails.
3. Every ``symbol_guess`` returned by either provider is re-verified
   against the REAL Toss API via
   ``strategy.toss_symbol_verifier.verify_symbol_guesses`` -- this uses
   only read-only Toss endpoints (``stocks``/``prices``), which hit the
   real broker regardless of ``DRY_RUN``/``PAPER_TRADING``, and never
   ``create_order``/``modify_order``/``cancel_order``. Fail-closed: any
   symbol Toss cannot confirm is discarded, never traded.
4. Only Toss-verified symbols that are not already present in
   ``data/symbols.csv`` (enabled or not) are appended, with
   ``resolved="ai_discovered_verified"`` and ``enabled=true``. All existing
   downstream logic (``symbols_by_market``, cash-affordable candidate
   scan, survival gate, hard safety gates, order construction) picks these
   up automatically the next time it reads the CSV -- no other code path
   needs to change.

This module never creates a broker order and never mutates an existing row
in data/symbols.csv; it only appends brand-new rows.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Mapping

from strategy.llm_providers import GeminiAPI2Client
from strategy.symbol_registry import EXECUTION_MARKETS, load_symbols
from strategy.toss_symbol_verifier import verify_symbol_guesses


SYMBOLS_CSV_COLUMNS = (
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


def _load_role_prompt_body(prompt_root: str) -> str:
    """Return the cash_symbol_discovery.md persona/rules body, no frontmatter.

    Reusing the exact same text as the NVIDIA agent role prompt keeps both
    providers under identical constraints (max 4 candidates, KR/US only, no
    fabricated numbers, symbol_guess is not trusted until Toss-verified).
    """
    path = Path(prompt_root) / "agents" / "cash_symbol_discovery.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
    return text.strip()


def _discovery_prompt(
    *,
    role_body: str,
    krw_buying_power: float,
    usd_buying_power: float,
    usd_krw_rate: float,
    known_universe_sample: list[dict[str, Any]],
) -> str:
    packet = {
        "krw_buying_power": float(krw_buying_power or 0.0),
        "usd_buying_power": float(usd_buying_power or 0.0),
        "usd_krw_rate": float(usd_krw_rate or 0.0),
        "known_universe_sample": known_universe_sample[:60],
    }
    return (
        f"{role_body}\n\n"
        "다음은 이번 발굴 요청의 입력 데이터다 (JSON). 이 데이터의 범위를 벗어나는 "
        "가격/확률/수익률 숫자를 만들어내지 마라:\n"
        f"{json.dumps(packet, ensure_ascii=False)}\n\n"
        "위 지침을 지키며 JSON만 반환하라."
    )


def _normalize_candidates(raw_candidates: Any, *, source: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not isinstance(raw_candidates, list):
        return candidates
    for item in raw_candidates[:4]:
        if not isinstance(item, Mapping):
            continue
        symbol_guess = str(item.get("symbol_guess") or "").strip()
        if not symbol_guess:
            continue
        candidates.append(
            {
                "symbol_guess": symbol_guess,
                "symbol_confidence": str(item.get("symbol_confidence") or "low"),
                "name": str(item.get("name") or ""),
                "market_guess": str(item.get("market_guess") or "").strip().upper(),
                "lens": str(item.get("lens") or ""),
                "thesis": str(item.get("thesis") or "")[:500],
                "source": source,
            }
        )
    return candidates


def _discover_via_gemini(
    settings: Any,
    *,
    krw_buying_power: float,
    usd_buying_power: float,
    usd_krw_rate: float,
    known_universe_sample: list[dict[str, Any]],
) -> dict[str, Any]:
    if not settings.gemini_api2_key:
        return {"status": "unavailable", "reason": "gemini_api2_key_missing", "candidates": []}

    role_body = _load_role_prompt_body(getattr(settings, "nvidia_nemotron_prompt_root", "agent"))
    if not role_body:
        return {"status": "unavailable", "reason": "role_prompt_file_missing", "candidates": []}

    client = GeminiAPI2Client(
        api_key=settings.gemini_api2_key,
        model=settings.gemini_api2_model,
        enable_grounding=bool(settings.gemini_api2_enable_grounding),
        temperature=settings.gemini_api2_temperature,
        max_output_tokens=settings.gemini_api2_max_output_tokens,
        timeout=settings.gemini_api2_timeout_seconds,
        min_request_interval_seconds=settings.gemini_api2_min_request_interval_seconds,
        max_retries=settings.gemini_api2_max_retries,
    )
    prompt = _discovery_prompt(
        role_body=role_body,
        krw_buying_power=krw_buying_power,
        usd_buying_power=usd_buying_power,
        usd_krw_rate=usd_krw_rate,
        known_universe_sample=known_universe_sample,
    )
    try:
        parsed = client.generate_json(prompt, enable_grounding=bool(settings.gemini_api2_enable_grounding))
    except Exception as exc:  # network/HTTP/parse failure -> fail closed, let NVIDIA fallback try
        return {"status": "unavailable", "reason": f"gemini_error:{exc}", "candidates": []}

    if not isinstance(parsed, Mapping) or parsed.get("status") != "ok":
        return {"status": "unavailable", "reason": "gemini_role_report_not_ok", "candidates": []}

    return {"status": "ok", "candidates": _normalize_candidates(parsed.get("candidates"), source="gemini_api2")}


def run_cash_symbol_discovery(
    settings: Any,
    state: dict,
    *,
    orchestrator: Any,
    toss: Any,
    research_day: str,
    krw_buying_power: float,
    usd_buying_power: float,
    usd_krw_rate: float,
    symbols_path: str = "data/symbols.csv",
) -> dict[str, Any]:
    """Run the once-per-day discover -> Toss-verify -> CSV-append flow.

    Always returns a summary dict; never raises. Any unexpected exception
    anywhere in this flow (provider HTTP error, malformed model JSON, Toss
    lookup failure) is caught and recorded as this run's result so a flaky
    discovery cycle never blocks the real, already-working cash-affordable
    candidate scan that runs right after this in main.py.
    """
    cache = state.setdefault("cash_symbol_discovery_cache", {})
    if not isinstance(cache, dict):
        cache = {}
        state["cash_symbol_discovery_cache"] = cache
    if cache.get("research_day") == research_day:
        cached = {key: value for key, value in cache.items() if key != "research_day"}
        return {"status": "cached", "research_day": research_day, **cached}

    try:
        known = load_symbols(symbols_path, include_disabled=True, include_unresolved=True)
        known_universe_sample = [
            {
                "symbol": symbol,
                "name": row.get("name", ""),
                "market": row.get("market", ""),
                "theme": row.get("theme", ""),
            }
            for symbol, row in list(known.items())[:60]
        ]

        result: dict[str, Any] = {"status": "unavailable", "candidates": [], "provider": None}
        if settings.cash_symbol_discovery_prefer_gemini:
            gemini_result = _discover_via_gemini(
                settings,
                krw_buying_power=krw_buying_power,
                usd_buying_power=usd_buying_power,
                usd_krw_rate=usd_krw_rate,
                known_universe_sample=known_universe_sample,
            )
            if gemini_result.get("status") == "ok":
                result = {**gemini_result, "provider": "gemini_api2"}

        if result["status"] != "ok":
            nvidia_result = orchestrator.discover_cash_symbols(
                krw_buying_power=krw_buying_power,
                usd_buying_power=usd_buying_power,
                krw_usd_rate=usd_krw_rate,
                known_universe_sample=known_universe_sample,
            )
            if nvidia_result.get("status") == "ok":
                result = {**nvidia_result, "provider": "nvidia_nemotron"}

        if result["status"] != "ok" or not result.get("candidates"):
            summary = {
                "status": result["status"],
                "reason": result.get("reason", "no_candidates"),
                "provider": result.get("provider"),
                "proposed": 0,
                "verified": 0,
                "appended_symbols": [],
            }
            cache.clear()
            cache.update({"research_day": research_day, **summary})
            return {"research_day": research_day, **summary}

        max_candidates = max(0, int(settings.cash_symbol_discovery_max_candidates))
        candidates = result["candidates"][:max_candidates]

        # Fail-closed re-verification against the REAL Toss API. Read-only
        # (client.stocks/client.prices); works the same regardless of
        # DRY_RUN/PAPER_TRADING and never places an order.
        verification = verify_symbol_guesses(
            toss,
            candidates,
            batch_size=int(settings.cash_symbol_discovery_verify_batch_size),
        )

        already_known = set(known.keys())
        appended_rows: list[dict[str, str]] = []
        verified_count = 0
        for candidate in candidates:
            symbol_guess = str(candidate.get("symbol_guess") or "").strip().upper()
            verdict = verification.get(symbol_guess) or {}
            if not verdict.get("verified"):
                continue
            verified_count += 1
            if symbol_guess in already_known:
                # Already tracked (possibly disabled/unresolved elsewhere in
                # the registry) -- never duplicate or silently re-enable an
                # existing row from this discovery path.
                continue
            market_hint = str(verdict.get("market_hint") or "").strip().upper()
            market = market_hint if market_hint in EXECUTION_MARKETS else candidate.get("market_guess", "")
            if market not in EXECUTION_MARKETS:
                continue
            appended_rows.append(
                {
                    "symbol": symbol_guess,
                    "name": verdict.get("name") or candidate.get("name") or symbol_guess,
                    "raw_name": candidate.get("name") or "",
                    "market": market,
                    "sector": "",
                    "theme": "AI Discovered",
                    "enabled": "true",
                    "resolved": "ai_discovered_verified",
                    "note": (
                        f"ai_discovery_source={candidate.get('source', '')};"
                        f"ai_lens={candidate.get('lens', '')};"
                        f"ai_confidence={candidate.get('symbol_confidence', '')};"
                        f"discovered_research_day={research_day}"
                    ),
                }
            )
            already_known.add(symbol_guess)

        if appended_rows:
            _append_symbols_csv(symbols_path, appended_rows)

        summary = {
            "status": "ok",
            "provider": result.get("provider"),
            "proposed": len(candidates),
            "verified": verified_count,
            "appended_symbols": [row["symbol"] for row in appended_rows],
        }
        cache.clear()
        cache.update({"research_day": research_day, **summary})
        return {"research_day": research_day, **summary}
    except Exception as exc:
        summary = {
            "status": "error",
            "reason": str(exc),
            "provider": None,
            "proposed": 0,
            "verified": 0,
            "appended_symbols": [],
        }
        cache.clear()
        cache.update({"research_day": research_day, **summary})
        return {"research_day": research_day, **summary}


def _append_symbols_csv(path: str, rows: list[dict[str, str]]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_exists = destination.exists() and destination.stat().st_size > 0
    with destination.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SYMBOLS_CSV_COLUMNS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
