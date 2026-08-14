---
id: nvidia_nemotron
provider: nvidia_nemotron
client: NvidiaNemotronAgentClient
credential_env:
  - NVIDIA_NEMOTRON_API_KEY
  - NVIDIA_SUPER_API_KEY
  - NVIDIA_FINAL_API_KEY
model_group_env:
  evidence_analysis: NVIDIA_SUPER_MODEL
  debate_risk: NVIDIA_NEMOTRON_MODEL
  final_comparison: NVIDIA_FINAL_MODEL
timeout_env: NVIDIA_NEMOTRON_TIMEOUT_SECONDS
daily_call_limit_env: NVIDIA_NEMOTRON_DAILY_CALL_LIMIT
json_only: true
direct_order_execution: false
---

# NVIDIA 멀티모델 에이전트 Provider

이 provider는 하나의 NVIDIA 호환 API 전송 계층 안에서 역할군별 모델을 분리한다. 모든 `agents/*.md`는 `provider: nvidia_nemotron`과 명시적인 `model_group`을 선언한다. `provider`가 같다는 것은 모델이 같다는 뜻이 아니다. 실제 호출 모델은 각 turn의 감사 로그에 기록한다.

## 실행 규칙

1. 런타임은 역할군에 대응하는 전용 NVIDIA 키만 읽는다. 다른 역할의 키로 대체하지 않으며, 프롬프트와 응답에는 키, 토큰, 계좌 식별자, 브로커 비밀값을 넣지 않는다.
2. 모델, 온도, 시간 제한, 일일 호출 한도는 환경 변수/코드 설정이 우선한다. 이 문서는 역할 정책이지 비밀 설정 파일이 아니다.
3. 각 역할의 system instruction은 해당 Markdown 본문과 `shared/` 문서를 합쳐서 만든다. 입력 데이터는 별도의 사용자 메시지/컨텍스트로 전달한다.
4. 구조화된 JSON 출력이 검증되지 않거나 데이터 기준 시각이 없으면 `hold` 또는 `insufficient_data`로 실패시킨다.
5. 검색·grounding은 런타임이 해당 역할에 명시적으로 제공했을 때만 쓴다. 모델이 확인하지 못한 외부 정보는 사실처럼 쓰지 않는다.
6. 이 provider는 직접 주문을 호출하지 않는다. `target_exposure`와 `proposed_actions`는 후속 Python 리스크 엔진에 전달할 분석 제안이다.
7. 호출은 OpenAI 호환 SDK의 streaming completion을 사용한다. Ultra·Super는 `reasoning_content`와 `content`를 분리 수집하고, 최종 JSON은 `content`에서 파싱한다. 최종 비교 GLM은 thinking을 요청하지 않는다.

## 모델 그룹

| `model_group` | 역할 | 모델 환경 변수 | 판단 한계 |
|---|---|---|---|
| `evidence_analysis` | 통계·사실·주관·뉴스·재무·심리·기술 분석 | `NVIDIA_SUPER_MODEL` | 개별 증거 보고서만 작성, 최종 승인 금지 |
| `debate_risk` | 추론, Bull/Bear, 토론 조정, 거래 제안, 위험 검토 | `NVIDIA_NEMOTRON_MODEL` | 앞선 근거만 사용, 주문·최종 승인 금지 |
| `final_comparison` | 포트폴리오/실거래 최종 비교 | `NVIDIA_FINAL_MODEL` | 새 사실 생성 금지, 위험 완화 금지, Python 전달 여부만 결정 |

`model_profile`은 structured, multimodal, offline 같은 출력·호출 성격을 보조 설명한다. 실제 모델 선택 권한은 `model_group`에만 있다.
