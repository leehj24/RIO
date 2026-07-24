import json
import random
from typing import Dict, Any
import numpy as np

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


class LLMClient:
    def estimate_event_probability(self, question: str, context: str) -> Dict[str, Any]:
        raise NotImplementedError


class MockLLMClient(LLMClient):
    def estimate_event_probability(self, question: str, context: str) -> Dict[str, Any]:
        return {
            "yes_probability": float(np.clip(0.50 + random.uniform(-0.22, 0.22), 0.03, 0.97)),
            "confidence": random.uniform(0.55, 0.88),
            "news_score": random.uniform(-0.7, 0.7),
            "reason": "mock LLM result",
        }


class NvidiaLLMClient(LLMClient):
    def __init__(self, api_key: str, base_url: str, model: str):
        if OpenAI is None:
            raise RuntimeError("openai package is not installed")
        if not api_key:
            raise RuntimeError("NVIDIA_API_KEY missing")

        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    def estimate_event_probability(self, question: str, context: str) -> Dict[str, Any]:
        prompt = f"""
너는 공격형 퀀트 이벤트 확률 예측 엔진이다.

질문:
{question}

컨텍스트:
{context}

반드시 JSON만 반환해라.

스키마:
{{
  "yes_probability": 0.0,
  "confidence": 0.0,
  "news_score": 0.0,
  "reason": "짧은 근거"
}}

규칙:
- yes_probability: 0~1
- confidence: 0~1
- news_score: -1~1
- 모르면 낮은 confidence를 줘라.
- 근거 없는 과신 금지.
"""
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=0.1,
            max_tokens=512,
            messages=[
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        return json.loads(resp.choices[0].message.content.strip())


def make_llm(settings):
    if settings.llm_provider.lower() == "nvidia":
        return NvidiaLLMClient(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            model=settings.nvidia_model,
        )
    return MockLLMClient()
