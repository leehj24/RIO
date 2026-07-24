import json
import random
from typing import Dict, Any, List

import numpy as np

from strategy.api_budget import DailyAPIBudget
from strategy.llm_call_log import LLMCallLogger
from strategy.llm_providers import GoogleGeminiGroundingClient, NvidiaNIMClient


def _clip(v: float, low: float, high: float) -> float:
    return float(np.clip(float(v), low, high))


class DualProviderLLMPipeline:
    """
    Google + NVIDIA 2단계 LLM 파이프라인.

    1단계: Google Gemini
    - 최신 뉴스/정치/경제/기상/스포츠/온체인/테마 evidence 검색/요약
    - 하루 GOOGLE_DAILY_CALL_LIMIT회까지만 호출

    2단계: NVIDIA NIM
    - Google evidence + 이벤트 질문 + 종목 후보를 보고 최종 확률 판단
    - 하루 NVIDIA_DAILY_CALL_LIMIT회까지만 호출

    예산 초과 또는 키 없음:
    - mock fallback 사용
    - 어떤 질문을 했고 어떤 답변을 받았는지 llm_calls.jsonl에 기록
    """

    def __init__(self, settings):
        self.settings = settings
        self.budget = DailyAPIBudget(settings.api_usage_path)
        self.logger = LLMCallLogger(settings.llm_log_path)

        self.google = GoogleGeminiGroundingClient(
            api_key=settings.google_api_key or settings.gemini_api_key,
            model=settings.gemini_model,
            enable_grounding=settings.enable_google_grounding,
        )

        self.nvidia = NvidiaNIMClient(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            model=settings.nvidia_model,
        )

    def usage_snapshot(self) -> Dict[str, Any]:
        return self.budget.snapshot({
            "google": self.settings.google_daily_call_limit,
            "nvidia": self.settings.nvidia_daily_call_limit,
        })

    def build_google_prompt(self, event, candidate_symbols: List[str], symbol_infos: List[Dict[str, Any]]) -> str:
        return f"""
너는 투자 리서치용 검색/증거 수집 엔진이다.

목표:
아래 이벤트 질문에 대해 최신 정보 기반 evidence를 수집하고 JSON으로 요약한다.

이벤트:
- event_id: {event.event_id}
- theme: {event.theme}
- question: {event.question}

후보 종목:
{json.dumps(symbol_infos[:80], ensure_ascii=False, indent=2)}

반드시 확인할 영역:
- 경제: 금리, 환율, 인플레이션, 경기, 수급
- 정치/정책: 정부정책, 규제, 보조금, 지정학
- 뉴스: 최근 기업/섹터 뉴스
- 기상: 전력, 농산물, 운송, 재생에너지에 관련 있을 때만
- 스포츠: 스포츠/엔터/방송/이벤트 관련 있을 때만
- 온체인: BTC/크립토/거래소/채굴/ETF 관련 있을 때만
- 테마: 후보 종목의 sector/theme에 직접 관련된 이슈

반환 형식은 JSON만 허용한다.

스키마:
{{
  "event_id": "{event.event_id}",
  "question": "{event.question}",
  "searched_domains": ["economy", "politics", "news"],
  "positive_evidence": [
    {{"title": "근거 제목", "summary": "짧은 요약", "affected_symbols": ["005930"], "strength": 0.0}}
  ],
  "negative_evidence": [
    {{"title": "리스크 제목", "summary": "짧은 요약", "affected_symbols": ["005930"], "strength": 0.0}}
  ],
  "neutral_evidence": [],
  "macro_summary": "거시환경 요약",
  "theme_summary": "테마환경 요약",
  "sentiment_score": 0.0,
  "probability_hint": 0.5,
  "confidence": 0.5,
  "top_relevant_symbols": ["005930", "000660"],
  "risk_flags": ["리스크1", "리스크2"]
}}

점수 규칙:
- sentiment_score: -1.0 ~ +1.0
- probability_hint: 0.0 ~ 1.0
- confidence: 0.0 ~ 1.0
- 모르면 confidence를 낮게 준다.
- 직접 매수 추천 금지. evidence만 정리한다.
"""

    def build_nvidia_prompt(
        self,
        event,
        candidate_symbols: List[str],
        symbol_infos: List[Dict[str, Any]],
        google_evidence: Dict[str, Any],
    ) -> str:
        return f"""
너는 공격형 퀀트 이벤트 확률 판단 엔진이다.

아래 Google/Gemini evidence와 이벤트 질문을 바탕으로
LMSR/Kelly/SDE/팩터 계산에 사용할 확률 입력값을 JSON으로 만든다.

이벤트:
- event_id: {event.event_id}
- theme: {event.theme}
- question: {event.question}

후보 종목:
{json.dumps(symbol_infos[:80], ensure_ascii=False, indent=2)}

Google evidence:
{json.dumps(google_evidence, ensure_ascii=False, indent=2)[:12000]}

반환 JSON 스키마:
{{
  "event_id": "{event.event_id}",
  "yes_probability": 0.0,
  "confidence": 0.0,
  "news_score": 0.0,
  "top_candidate_symbols": [
    {{"symbol": "005930", "score_hint": 0.0, "reason": "짧은 이유"}}
  ],
  "avoid_symbols": [
    {{"symbol": "000000", "reason": "리스크 이유"}}
  ],
  "main_positive_thesis": "상승 논리",
  "main_negative_thesis": "하락/리스크 논리",
  "risk_flags": ["리스크1", "리스크2"],
  "reason": "짧은 최종 판단 근거"
}}

규칙:
- yes_probability: 이벤트가 향후 3개월 동안 후보군에 긍정적으로 작용할 확률, 0~1
- confidence: 판단 신뢰도, 0~1
- news_score: 뉴스/이벤트 점수, -1~1
- top_candidate_symbols는 직접 주문 결정이 아니라 다음 계산의 후보 우선순위 힌트다.
- 근거가 약하면 confidence를 낮춰라.
- JSON 외 텍스트 금지.
"""

    def mock_google_evidence(self, event, candidate_symbols: List[str]) -> Dict[str, Any]:
        return {
            "event_id": event.event_id,
            "question": event.question,
            "searched_domains": ["mock"],
            "positive_evidence": [],
            "negative_evidence": [],
            "neutral_evidence": [],
            "macro_summary": "mock evidence: Google API not called or daily budget exceeded",
            "theme_summary": f"mock theme summary for {event.theme}",
            "sentiment_score": random.uniform(-0.3, 0.3),
            "probability_hint": float(np.clip(0.50 + random.uniform(-0.12, 0.12), 0.05, 0.95)),
            "confidence": 0.35,
            "top_relevant_symbols": candidate_symbols[:5],
            "risk_flags": ["mock_fallback"],
            "_fallback": True,
        }

    def mock_nvidia_judgement(self, event, candidate_symbols: List[str], google_evidence: Dict[str, Any]) -> Dict[str, Any]:
        hint = float(google_evidence.get("probability_hint", 0.5) or 0.5)
        sentiment = float(google_evidence.get("sentiment_score", 0.0) or 0.0)
        yes = float(np.clip(hint + 0.10 * sentiment + random.uniform(-0.08, 0.08), 0.03, 0.97))
        return {
            "event_id": event.event_id,
            "yes_probability": yes,
            "confidence": 0.40,
            "news_score": float(np.clip(sentiment, -1, 1)),
            "top_candidate_symbols": [
                {"symbol": s, "score_hint": 0.0, "reason": "mock fallback"}
                for s in candidate_symbols[:5]
            ],
            "avoid_symbols": [],
            "main_positive_thesis": "mock fallback",
            "main_negative_thesis": "mock fallback",
            "risk_flags": ["mock_fallback"],
            "reason": "NVIDIA API not called or daily budget exceeded",
            "_fallback": True,
        }

    def google_based_judgement(
        self,
        event,
        candidate_symbols: List[str],
        google_evidence: Dict[str, Any],
        nvidia_error: str,
    ) -> Dict[str, Any]:
        """NVIDIA 판단 실패 시, '실제' Google evidence로 2차 판단을 구성한다.

        2026-07 운영 로그에서 NVIDIA NIM이 404를 반환해 모든 판단이
        confidence=0.40 mock으로 강등되었고, 그 결과 survival gate
        (MIN_LLM_CONFIDENCE)를 영원히 통과하지 못했다.  Google evidence가
        실제 호출 결과일 때는 그 probability_hint/confidence를 사용하되,
        단일 소스 판단임을 반영해 confidence에 0.85 페널티를 준다.
        Google evidence 자체가 mock이면 기존 mock fallback(0.40)을 유지해
        fail-closed 성질은 그대로 남는다.
        """
        derived = {
            "event_id": event.event_id,
            "yes_probability": _clip(google_evidence.get("probability_hint", 0.5) or 0.5, 0.01, 0.99),
            "confidence": _clip(0.85 * float(google_evidence.get("confidence", 0.0) or 0.0), 0.0, 1.0),
            "news_score": _clip(google_evidence.get("sentiment_score", 0.0) or 0.0, -1.0, 1.0),
            "top_candidate_symbols": [
                {"symbol": str(s), "score_hint": 0.0, "reason": "google evidence top_relevant_symbols"}
                for s in (google_evidence.get("top_relevant_symbols") or [])[:5]
                if str(s) in set(map(str, candidate_symbols))
            ],
            "avoid_symbols": [],
            "main_positive_thesis": str(google_evidence.get("macro_summary", ""))[:500],
            "main_negative_thesis": "; ".join(map(str, google_evidence.get("risk_flags", [])))[:500],
            "risk_flags": list(google_evidence.get("risk_flags", [])),
            "reason": f"nvidia unavailable ({nvidia_error[:120]}); derived from real google evidence",
            "judgement_source": "google_evidence_derived",
            "_fallback": False,
        }
        return derived

    def call_google(self, event, candidate_symbols: List[str], symbol_infos: List[Dict[str, Any]]) -> Dict[str, Any]:
        prompt = self.build_google_prompt(event, candidate_symbols, symbol_infos)
        provider = "google"
        can = self.budget.can_call(provider, self.settings.google_daily_call_limit)

        if not self.settings.enable_google_api or not can or not (self.settings.google_api_key or self.settings.gemini_api_key):
            response = self.mock_google_evidence(event, candidate_symbols)
            self.logger.log({
                "provider": provider,
                "called_api": False,
                "reason": "disabled_or_budget_exceeded_or_missing_key",
                "budget": self.usage_snapshot(),
                "prompt_type": "evidence_search",
                "question": event.question,
                "prompt": prompt,
                "response": response,
            })
            return response

        self.budget.record_call(provider, {"event_id": event.event_id, "prompt_type": "evidence_search"})
        try:
            response = self.google.generate_json(prompt)
            response["_fallback"] = False
        except Exception as e:
            response = self.mock_google_evidence(event, candidate_symbols)
            response["_error"] = str(e)

        self.logger.log({
            "provider": provider,
            "called_api": True,
            "budget": self.usage_snapshot(),
            "prompt_type": "evidence_search",
            "question": event.question,
            "prompt": prompt,
            "response": response,
        })
        return response

    def call_nvidia(
        self,
        event,
        candidate_symbols: List[str],
        symbol_infos: List[Dict[str, Any]],
        google_evidence: Dict[str, Any],
    ) -> Dict[str, Any]:
        prompt = self.build_nvidia_prompt(event, candidate_symbols, symbol_infos, google_evidence)
        provider = "nvidia"
        can = self.budget.can_call(provider, self.settings.nvidia_daily_call_limit)

        if not self.settings.enable_nvidia_api or not can or not self.settings.nvidia_api_key:
            if not google_evidence.get("_fallback", True):
                response = self.google_based_judgement(
                    event, candidate_symbols, google_evidence, "nvidia disabled or daily budget exceeded"
                )
            else:
                response = self.mock_nvidia_judgement(event, candidate_symbols, google_evidence)
            self.logger.log({
                "provider": provider,
                "called_api": False,
                "reason": "disabled_or_budget_exceeded_or_missing_key",
                "budget": self.usage_snapshot(),
                "prompt_type": "probability_judgement",
                "question": event.question,
                "prompt": prompt,
                "response": response,
            })
            return response

        self.budget.record_call(provider, {"event_id": event.event_id, "prompt_type": "probability_judgement"})
        try:
            response = self.nvidia.generate_json(prompt)
            response["_fallback"] = False
        except Exception as e:
            if not google_evidence.get("_fallback", True):
                # 실제 Google evidence가 있으면 무작위 mock 대신 그것으로 판단.
                response = self.google_based_judgement(event, candidate_symbols, google_evidence, str(e))
            else:
                response = self.mock_nvidia_judgement(event, candidate_symbols, google_evidence)
            response["_error"] = str(e)

        self.logger.log({
            "provider": provider,
            "called_api": True,
            "budget": self.usage_snapshot(),
            "prompt_type": "probability_judgement",
            "question": event.question,
            "prompt": prompt,
            "response": response,
        })
        return response

    def analyze_event(self, event, candidate_symbols: List[str], symbol_infos: List[Dict[str, Any]]) -> Dict[str, Any]:
        google_evidence = self.call_google(event, candidate_symbols, symbol_infos)
        nvidia_judgement = self.call_nvidia(event, candidate_symbols, symbol_infos, google_evidence)

        yes_probability = _clip(nvidia_judgement.get("yes_probability", 0.5), 0.01, 0.99)
        confidence = _clip(nvidia_judgement.get("confidence", 0.4), 0.0, 1.0)
        news_score = _clip(nvidia_judgement.get("news_score", google_evidence.get("sentiment_score", 0.0)), -1.0, 1.0)

        return {
            "yes_probability": yes_probability,
            "confidence": confidence,
            "news_score": news_score,
            "google_evidence": google_evidence,
            "nvidia_judgement": nvidia_judgement,
            "api_usage": self.usage_snapshot(),
        }
