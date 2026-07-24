---
id: distillation_policy
version: 1.0.0
provider: gemini_api2
direct_order_execution: false
---

# 저빈도 Teacher / 고빈도 Student 정책

Gemini_Api2 토론팀은 느린 System 2 Teacher다. 장전·장후·새 공시·중요 이벤트에서 구조적 가설과 위험 조건을 만든다. 실시간 Student는 별도 모델/규칙이며 매 틱마다 LLM 토론을 호출하지 않는다.

Teacher가 주장한 인과 경로는 하이퍼엣지 ID, 관련 자산/이벤트, 시간 창, 검증 상태를 붙인다. LLM의 그럴듯한 관계는 `hypothesis` 상태일 뿐, 통계 검증·전이 엔트로피·롤링 안정성 등의 외부 검증을 통과하기 전에는 `validated`가 아니다.

Student 학습/배포에는 Teacher 입력·출력 버전, 데이터 구간, 비용·지연, 성능 저하 감시, 재학습 기준을 기록한다. Student 역시 브로커 주문 권한을 직접 갖지 않으며 Python 리스크 게이트 뒤에만 사용할 수 있다.
