---
id: nvidia_glm_final_comparison
provider: nvidia_nemotron
client: NvidiaNemotronAgentClient
credential_env:
  - NVIDIA_FINAL_API_KEY
model_group: final_comparison
model_env: NVIDIA_FINAL_MODEL
timeout_env: NVIDIA_NEMOTRON_TIMEOUT_SECONDS
daily_call_limit_env: NVIDIA_NEMOTRON_DAILY_CALL_LIMIT
json_only: true
direct_order_execution: false
---

# 최종 비교 모델 정책

이 문서는 `final_comparison` 역할군의 보조 정책이다. 최종 비교 모델은 NVIDIA 호환 endpoint에서 `NVIDIA_FINAL_MODEL`로 선택하며 기본 모델은 `z-ai/glm-5.2`다.

## 실행 규칙

1. 최종 비교 모델은 분석 보고서, Bull/Bear 결과, 위험 보고서를 입력받아 서로의 일치·충돌·누락을 비교한다.
2. 원본 뉴스·재무·차트를 새로 분석하거나 새로운 evidence ID를 만들지 않는다.
3. Risk의 `hold/reject` 또는 치명적 flag를 다수결로 지우지 않는다.
4. 모델 ID, 시간 제한, 호출 한도는 환경 변수/코드가 우선하며 실제 모델 ID는 감사 로그에 남긴다.
5. 구조화 JSON, 동일한 `data_cutoff_utc`, 후보 종목 일치가 확인되지 않으면 `hold`로 실패시킨다.
6. 출력은 Python 리스크 게이트에 전달할 승인 제안일 뿐 직접 주문이 아니다.

## 비교 우선순위

1. 데이터 시점과 후보 심볼 일치
2. 근거 ID의 입력 포함 여부
3. Bull/Bear의 핵심 반대 근거와 미해결 쟁점
4. Risk manager와 semantic filter 중 더 제한적인 판단
5. 위 조건을 통과한 경우에만 Python 리스크 게이트 전달 여부
