import json
import re
import threading
import time
from typing import Dict, Any, Optional

import requests

from strategy.json_utils import extract_json_object


class GeminiAPI2Client:
    """Dedicated Gemini client for the multi-agent workflow.

    This is intentionally separate from :class:`GoogleGeminiGroundingClient`.
    The latter powers the legacy Google-evidence stage and may use the normal
    Google/Gemini key.  This client only receives the ``Gemini_Api2`` /
    ``GEMINI_API2_KEY`` key selected by ``Settings.gemini_api2_key``.

    Agent prompts can provide their persona as ``system_instruction`` and
    request strict JSON (optionally with a Gemini response JSON schema).  The
    client has no trading or file-write capability: it only sends model input
    and returns parsed model output.
    """

    provider_id = "gemini_api2"
    supports_system_instruction = True
    supports_structured_output = True
    supports_google_search_grounding = True
    # The managed API returns generated text, not per-token target/draft logits
    # or KV-cache controls.  Never simulate exact speculative decoding with
    # two remote requests.
    supports_token_level_speculative_decoding = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash",
        enable_grounding: bool = False,
        system_instruction: str = "",
        temperature: float = 0.1,
        max_output_tokens: Optional[int] = 2048,
        endpoint_template: str = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        timeout: int = 60,
        min_request_interval_seconds: float = 0.0,
        max_retries: int = 0,
    ):
        self.api_key = (api_key or "").strip()
        self.model = model
        self.enable_grounding = enable_grounding
        self.system_instruction = system_instruction.strip()
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.endpoint_template = endpoint_template
        self.timeout = timeout
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))
        self.max_retries = max(0, int(max_retries))
        self._pacer_lock = threading.Lock()
        self._next_request_at = 0.0

    def _wait_for_turn(self) -> None:
        """Serialize agent calls at the configured provider request cadence."""

        if self.min_request_interval_seconds <= 0:
            return
        with self._pacer_lock:
            now = time.monotonic()
            scheduled = max(now, self._next_request_at)
            self._next_request_at = scheduled + self.min_request_interval_seconds
        delay = scheduled - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _provider_retry_delay(response: requests.Response, attempt: int) -> float:
        """Recover the documented Gemini retry hint without trusting it blindly."""

        for header in ("Retry-After", "retry-after"):
            try:
                if response.headers.get(header) is not None:
                    return min(60.0, max(0.0, float(response.headers[header])))
            except (TypeError, ValueError):
                pass
        match = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", response.text, flags=re.IGNORECASE)
        if match:
            return min(60.0, max(0.0, float(match.group(1))))
        return min(60.0, float(2**attempt))

    @property
    def capabilities(self) -> Dict[str, bool]:
        """Capabilities consumed by an agent registry without provider checks."""
        return {
            "system_instruction": self.supports_system_instruction,
            "structured_output": self.supports_structured_output,
            "google_search_grounding": self.supports_google_search_grounding,
            "token_level_speculative_decoding": self.supports_token_level_speculative_decoding,
        }

    def build_request_body(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        enable_grounding: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Build a Gemini ``generateContent`` body without making a request.

        Keeping this public makes request formation straightforward to test and
        lets an orchestrator inspect the effective provider options for audit
        logging without exposing the API key.
        """
        effective_instruction = (
            self.system_instruction if system_instruction is None else system_instruction.strip()
        )
        effective_grounding = self.enable_grounding if enable_grounding is None else enable_grounding
        generation_config: Dict[str, Any] = {
            "temperature": self.temperature if temperature is None else temperature,
            "responseMimeType": "application/json",
        }
        if self.max_output_tokens is not None:
            generation_config["maxOutputTokens"] = int(self.max_output_tokens)
        if response_schema is not None:
            generation_config["responseJsonSchema"] = response_schema

        body: Dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": generation_config,
        }
        if effective_instruction:
            body["systemInstruction"] = {"parts": [{"text": effective_instruction}]}
        if effective_grounding:
            # The v1beta Gemini REST API uses this Google Search tool spelling.
            body["tools"] = [{"google_search": {}}]
        return body

    def generate_json(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        enable_grounding: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Run one agent turn and return the first JSON object in its response."""
        if not self.api_key:
            raise RuntimeError("GEMINI_API2_KEY/Gemini_Api2 missing")

        url = self.endpoint_template.format(model=self.model)
        body = self.build_request_body(
            prompt,
            system_instruction=system_instruction,
            response_schema=response_schema,
            temperature=temperature,
            enable_grounding=enable_grounding,
        )
        # Keep the dedicated key out of URL/query logs.  Gemini accepts this
        # documented header form in addition to the legacy ``?key=`` form.
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        for attempt in range(self.max_retries + 1):
            self._wait_for_turn()
            resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)
            if resp.status_code != 429 or attempt >= self.max_retries:
                break
            # A 429 is safe to retry because Gemini did not produce a model
            # response.  The bot never retries any broker order this way.
            time.sleep(self._provider_retry_delay(resp, attempt))

        if resp.status_code >= 400:
            raise RuntimeError(f"Gemini API2 error {resp.status_code}: {resp.text}")

        data = resp.json()
        text = ""
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "\n".join(part.get("text", "") for part in parts if "text" in part)
        except (KeyError, IndexError, TypeError):
            text = json.dumps(data, ensure_ascii=False)

        parsed = extract_json_object(text)
        parsed["_raw_provider_response"] = data
        return parsed


class GoogleGeminiGroundingClient:
    """
    Google Gemini API client.

    역할:
    - 최신 뉴스/정치/경제/기상/스포츠/온체인/테마 evidence 요약
    - 가능하면 Google Search grounding 사용
    - 응답은 JSON으로 받으려고 요청

    환경변수:
    - GOOGLE_API_KEY 또는 GEMINI_API_KEY
    - GEMINI_MODEL
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash",
        enable_grounding: bool = True,
        endpoint_template: str = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        timeout: int = 60,
    ):
        self.api_key = api_key
        self.model = model
        self.enable_grounding = enable_grounding
        self.endpoint_template = endpoint_template
        self.timeout = timeout

    def generate_json(self, prompt: str) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("GOOGLE_API_KEY/GEMINI_API_KEY missing")

        url = self.endpoint_template.format(model=self.model)
        params = {"key": self.api_key}

        body: Dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
            },
        }

        if self.enable_grounding:
            # 최신 Gemini API에서는 google_search tool을 사용한다.
            # 계정/모델 상태에 따라 지원되지 않으면 .env에서 ENABLE_GOOGLE_GROUNDING=false로 끄면 된다.
            body["tools"] = [{"google_search": {}}]
        else:
            body["generationConfig"]["responseMimeType"] = "application/json"

        resp = requests.post(url, params=params, json=body, timeout=self.timeout)

        if resp.status_code >= 400:
            detail = resp.text
            raise RuntimeError(f"Gemini API error {resp.status_code}: {detail}")

        data = resp.json()

        text = ""
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "\n".join([p.get("text", "") for p in parts if "text" in p])
        except Exception:
            text = json.dumps(data, ensure_ascii=False)

        parsed = extract_json_object(text)
        parsed["_raw_provider_response"] = data
        return parsed


class NvidiaNIMClient:
    """
    NVIDIA NIM OpenAI-compatible client.

    역할:
    - Google evidence를 받아 최종 확률 판단
    - yes_probability / confidence / news_score / top_candidate_symbols JSON 생성
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://integrate.api.nvidia.com/v1",
        model: str = "nvidia/nemotron-3-ultra-550b-a55b",
        timeout: int = 60,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def generate_json(self, prompt: str) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("NVIDIA_API_KEY missing")

        url = self.base_url + "/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        body = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 1200,
            "messages": [
                {
                    "role": "system",
                    "content": "Return strict JSON only. Do not include markdown.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        }

        resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)

        if resp.status_code >= 400:
            raise RuntimeError(f"NVIDIA API error {resp.status_code}: {resp.text}")

        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = extract_json_object(text)
        parsed["_raw_provider_response"] = data
        return parsed


class NvidiaNemotronAgentClient(NvidiaNIMClient):
    """OpenAI-compatible NVIDIA NIM client for the debate and risk agents.

    NVIDIA's hosted endpoint does not expose Gemini-style response schemas or
    Google Search grounding.  The orchestration layer consequently keeps its
    existing JSON validation and deterministic risk gate as the final guard.
    """

    provider_id = "nvidia_nemotron"
    supports_system_instruction = True
    supports_structured_output = False
    supports_google_search_grounding = False
    supports_token_level_speculative_decoding = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://integrate.api.nvidia.com/v1",
        enable_grounding: bool = False,
        temperature: float = 0.1,
        max_output_tokens: Optional[int] = 2048,
        top_p: float = 0.95,
        reasoning_budget: Optional[int] = None,
        timeout: int = 90,
        min_request_interval_seconds: float = 0.0,
        max_retries: int = 0,
        enable_thinking: bool = True,
    ):
        super().__init__(api_key=api_key, base_url=base_url, model=model, timeout=timeout)
        self.enable_grounding = enable_grounding
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.top_p = top_p
        self.reasoning_budget = reasoning_budget
        self.min_request_interval_seconds = min_request_interval_seconds
        self.max_retries = max_retries
        self.enable_thinking = enable_thinking

    @property
    def capabilities(self) -> Dict[str, bool]:
        return {
            "system_instruction": self.supports_system_instruction,
            "structured_output": self.supports_structured_output,
            "google_search_grounding": self.supports_google_search_grounding,
            "token_level_speculative_decoding": self.supports_token_level_speculative_decoding,
        }

    def generate_json(
        self,
        prompt: str,
        *,
        system_instruction: str = "",
        response_schema: Optional[Dict[str, Any]] = None,
        temperature: float = 0.1,
        enable_grounding: bool = False,
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("NVIDIA_NEMOTRON_API_KEY/NVIDIA_API_KEY missing")
        if enable_grounding:
            raise RuntimeError("NVIDIA NIM Nemotron does not support Google Search grounding")

        url = self.base_url + "/chat/completions"
        messages = []
        if system_instruction.strip():
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": self.model,
            "temperature": float(temperature),
            "top_p": self.top_p,
            "max_tokens": self.max_output_tokens or 2048,
            "messages": messages,
        }
        if self.enable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": True}
        if self.reasoning_budget:
            body["reasoning_budget"] = self.reasoning_budget
        # `response_schema` is intentionally not sent: this model endpoint is
        # OpenAI-compatible but does not guarantee JSON-schema support.
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"NVIDIA API error {response.status_code}: {response.text}")
        data = response.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = extract_json_object(text)
        parsed["_raw_provider_response"] = data
        return parsed
