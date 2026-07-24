import csv
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict

from strategy.symbol_registry import symbols_by_country, symbols_by_market, symbols_by_sector, symbols_by_theme


@dataclass
class EventSpec:
    event_id: str
    question: str
    mapped_symbols: List[str]
    theme: str = ""
    b: float = 100.0


def _bool(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def load_question_templates(path: str = "data/question_templates.csv") -> Dict[str, List[str]]:
    p = Path(path)
    templates: Dict[str, List[str]] = {}
    if not p.exists():
        return templates

    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            theme = row.get("theme", "").strip()
            template = row.get("template", "").strip()
            if theme and template:
                templates.setdefault(theme, []).append(template)
    return templates


def resolve_symbol_tokens(value: str) -> List[str]:
    """
    event_questions.csv의 mapped_symbols를 해석한다.

    지원:
    - 직접 심볼: 005930,NVDA
    - 시장 전체: MARKET:KR
    - 테마 전체: THEME:AI Semiconductor
    - 섹터 전체: SECTOR:Country ETF
    - 국가 노출 전체/개별: COUNTRY:ALL,COUNTRY:India
    - 혼합: THEME:Battery,THEME:Power Grid,005930
    """
    out: List[str] = []

    tokens = [x.strip() for x in str(value or "").split(",") if x.strip()]
    for token in tokens:
        if token.startswith("MARKET:"):
            market = token.split(":", 1)[1].strip()
            out.extend(symbols_by_market(market))
        elif token.startswith("THEME:"):
            theme = token.split(":", 1)[1].strip()
            out.extend(symbols_by_theme(theme))
        elif token.startswith("SECTOR:"):
            sector = token.split(":", 1)[1].strip()
            out.extend(symbols_by_sector(sector))
        elif token.startswith("COUNTRY:"):
            country = token.split(":", 1)[1].strip()
            out.extend(symbols_by_country(country))
        else:
            out.append(token)

    # dedupe preserving order
    deduped: List[str] = []
    seen = set()
    for s in out:
        if s and s not in seen:
            deduped.append(s)
            seen.add(s)
    return deduped


def load_events(
    path: str = "data/event_questions.csv",
    *,
    rotate_questions: bool = True,
) -> List[EventSpec]:
    p = Path(path)
    if not p.exists():
        return default_events()

    templates = load_question_templates()
    events: List[EventSpec] = []

    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not _bool(row.get("enabled", "true")):
                continue

            theme = row.get("theme", "").strip()
            base_question = row.get("question", "").strip()
            question = base_question

            if rotate_questions and theme in templates and templates[theme]:
                question = random.choice(templates[theme])

            mapped_symbols = resolve_symbol_tokens(row.get("mapped_symbols", ""))

            if not mapped_symbols:
                continue

            events.append(
                EventSpec(
                    event_id=row.get("event_id", "").strip(),
                    theme=theme,
                    question=question,
                    mapped_symbols=mapped_symbols,
                    b=float(row.get("b", 100) or 100),
                )
            )

    return events


def default_events() -> List[EventSpec]:
    return [
        EventSpec(
            event_id="kr_domestic_top_candidates",
            theme="KR Domestic",
            question="향후 3개월 동안 국내 주식의 가능성이 높은 종목은?",
            mapped_symbols=symbols_by_market("KR"),
        ),
    ]
