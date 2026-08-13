---
id: nvidia_nemotron
provider: nvidia_nemotron
client: NvidiaNemotronAgentClient
credential_env:
  - NVIDIA_DEEPSEEK_API_KEY
  - GEMINI_API2_API_KEY
  - Gemini_Api2
model_env: NVIDIA_DEEPSEEK_MODEL
temperature_env: NVIDIA_DEEPSEEK_TEMPERATURE
timeout_env: NVIDIA_DEEPSEEK_TIMEOUT_SECONDS
daily_call_limit_env: NVIDIA_DEEPSEEK_DAILY_CALL_LIMIT
default_model: nvidia/nemotron-3-ultra-550b-a55b
default_temperature: 0.1
json_only: true
direct_order_execution: false
---

# Gemini_Api2 전용 에이전트 Provider

이 provider는 이 프로젝트의 에이전트 분석 전용 Gemini 연결이다. 모든 `agents/*.md`는 `provider: nvidia_nemotron`를 선언하며 다른 LLM provider로 자동 폴백하지 않는다.

## 실행 규칙

1. 런타임은 `Gemini_Api2` 또는 표준화된 `NVIDIA_DEEPSEEK_API_KEY`에서만 API 키를 읽는다. 프롬프트와 응답에는 키, 토큰, 계좌 식별자, 브로커 비밀값을 넣지 않는다.
2. 모델, 온도, 시간 제한, 일일 호출 한도는 환경 변수/코드 설정이 우선한다. 이 문서는 역할 정책이지 비밀 설정 파일이 아니다.
3. 각 역할의 system instruction은 해당 Markdown 본문과 `shared/` 문서를 합쳐서 만든다. 입력 데이터는 별도의 사용자 메시지/컨텍스트로 전달한다.
4. 구조화된 JSON 출력이 검증되지 않거나 데이터 기준 시각이 없으면 `hold` 또는 `insufficient_data`로 실패시킨다.
5. 검색·grounding은 런타임이 해당 역할에 명시적으로 제공했을 때만 쓴다. 모델이 확인하지 못한 외부 정보는 사실처럼 쓰지 않는다.
6. 이 provider는 직접 주문을 호출하지 않는다. `target_exposure`와 `proposed_actions`는 후속 Python 리스크 엔진에 전달할 분석 제안이다.

## 모델 프로필

- `analysis`: 근거가 많은 분석·토론·회고 역할. 낮은 온도를 우선한다.
- `structured`: 사실 추출·필터 역할. JSON 검증과 낮은 온도를 우선한다.
- `multimodal`: 런타임이 차트 이미지를 제공할 때만 쓰는 보조 분석 역할이다.
- `offline`: 장후 시뮬레이션·증류·지연 최적화 설계처럼 주문 경로 밖에서 쓰는 역할이다.

프로필은 모델명 자체가 아니라 호출 정책을 표현한다. 실제 모델 변경은 환경 변수와 코드 설정으로만 한다.
