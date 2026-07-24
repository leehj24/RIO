"""네이버 검색 API(뉴스·블로그) 기반 국내 evidence 수집기.

설계 원칙 (ai_prompot 논문 매핑):
- 03 사실주관: 뉴스는 '사실 채널', 블로그는 '주관(심리) 채널'로 분리 라벨링해
  LLM에 전달한다. 채널을 섞어 하나의 감성 점수로 뭉개지 않는다.
- 10 위험필터: 네이버 검색 결과는 절대 직접 주문을 생성하지 않는다.
  LLM 판단(증거 요약 → news_score/확률)에 입력되는 재료일 뿐이다.
- 04 재현감사: 수집 시각(observed_at_utc)과 쿼리를 기록해 어떤 시점에
  무엇을 알고 있었는지 재현 가능하게 한다.

호출 예산: DailyAPIBudget provider="naver_search" 로 관리 (기본 300회/일,
네이버 무료 한도는 25,000회/일이지만 봇 루프가 과호출하지 않도록 보수적으로 제한).
"""

import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import requests

from strategy.api_budget import DailyAPIBudget

_TAG_RE = re.compile(r"</?b>|&quot;|&amp;|&lt;|&gt;")


def _clean(text: str) -> str:
    return _TAG_RE.sub(lambda m: {"&quot;": '"', "&amp;": "&", "&lt;": "<", "&gt;": ">"}.get(m.group(0), ""), str(text or "")).strip()


class NaverSearchClient:
    """네이버 오픈API 뉴스/블로그 검색. 캐시 + 일일 예산 + 실패 시 빈 결과."""

    def __init__(self, settings):
        self.settings = settings
        self.client_id = str(getattr(settings, "naver_client_id", "") or "")
        self.client_secret = str(getattr(settings, "naver_client_secret", "") or "")
        self.enabled = bool(getattr(settings, "enable_naver_search", True)) and bool(self.client_id and self.client_secret)
        self.daily_limit = int(getattr(settings, "naver_search_daily_call_limit", 300))
        self.cache_ttl = int(getattr(settings, "naver_search_cache_ttl_seconds", 1800))
        self.cache_dir = Path(getattr(settings, "naver_search_cache_dir", "data_cache/naver_search"))
        self.budget = DailyAPIBudget(getattr(settings, "api_usage_path", "api_usage_state.json"))
        self.timeout = 10

    # ------------------------------------------------------------
    # 저수준 호출
    # ------------------------------------------------------------

    def _cache_path(self, kind: str, query: str) -> Path:
        key = hashlib.md5(f"{kind}:{query}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, kind: str, query: str):
        p = self._cache_path(kind, query)
        if p.exists() and (time.time() - p.stat().st_mtime) < self.cache_ttl:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def _write_cache(self, kind: str, query: str, data) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(kind, query).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _search(self, kind: str, query: str, display: int, sort: str = "date") -> List[Dict[str, Any]]:
        """kind: 'news' | 'blog'. 실패/한도초과 시 빈 리스트 (fail-open이지만 증거 없음으로만 작용)."""
        if not self.enabled:
            return []
        cached = self._read_cache(kind, query)
        if cached is not None:
            return cached
        if not self.budget.can_call("naver_search", self.daily_limit):
            return []
        try:
            self.budget.record_call("naver_search", {"kind": kind, "query": query[:60]})
            resp = requests.get(
                f"https://openapi.naver.com/v1/search/{kind}.json",
                params={"query": query, "display": display, "sort": sort},
                headers={
                    "X-Naver-Client-Id": self.client_id,
                    "X-Naver-Client-Secret": self.client_secret,
                },
                timeout=self.timeout,
            )
            if resp.status_code >= 400:
                return []
            items = resp.json().get("items", [])
        except Exception:
            return []

        out = [
            {
                "title": _clean(item.get("title")),
                "summary": _clean(item.get("description")),
                "published": item.get("pubDate") or item.get("postdate") or "",
                "link": item.get("originallink") or item.get("link") or "",
                "query": query,
            }
            for item in items
        ]
        self._write_cache(kind, query, out)
        return out

    # ------------------------------------------------------------
    # 이벤트 단위 evidence 구성
    # ------------------------------------------------------------

    def build_event_evidence(self, event, symbol_infos: List[Dict[str, Any]]) -> Dict[str, Any]:
        """이벤트(테마) + 국내 종목명 기준으로 뉴스(사실)·블로그(주관) 증거를 만든다.

        쿼리 수를 이벤트당 최대 5개로 제한해 루프당 호출량을 통제한다.
        """
        if not self.enabled:
            return {"enabled": False, "reason": "naver keys missing or disabled"}

        theme = str(getattr(event, "theme", "") or "")
        queries: List[str] = []
        if theme:
            queries.append(f"{theme} 주식")
        for info in symbol_infos:
            name = str(info.get("name", "") or info.get("raw_name", "") or "")
            market = str(info.get("market", "")).upper()
            # 한국 종목명 위주 (네이버 검색은 국문 뉴스가 강함)
            if name and market == "KR":
                queries.append(name)
        queries = queries[:5]

        news: List[Dict[str, Any]] = []
        blogs: List[Dict[str, Any]] = []
        for q in queries:
            news.extend(self._search("news", q, display=5, sort="date"))
        # 블로그(주관 채널)는 테마 쿼리 1개만: 심리 참고용이지 사실 소스가 아니다.
        if queries:
            blogs = self._search("blog", queries[0], display=5, sort="sim")

        return {
            "enabled": True,
            "source": "naver_open_api",
            "observed_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "queries": queries,
            # 03 사실주관: 채널 분리 라벨
            "fact_channel_news": news[:20],
            "subjective_channel_blogs": blogs[:5],
            "usage_note": (
                "news는 검증 가능한 사건 중심의 사실 채널, blogs는 개인 의견·기대 중심의 "
                "주관 채널이다. 주관 채널은 시장 심리 참고용으로만 사용하고 사실 판단의 "
                "근거로 쓰지 마라. 이 데이터는 직접 매매 결정이 아니라 증거 요약용이다."
            ),
        }
