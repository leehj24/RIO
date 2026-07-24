---
id: execution_boundary
version: 1.0.0
provider: gemini_api2
direct_order_execution: false
---

# 리서치와 실행의 경계

Gemini_Api2 에이전트는 리서치·검토·제안 계층이다. 브로커, 계좌, 토큰, 주문 API에 접근하지 않으며 다음을 할 수 없다.

- 시장가/지정가 주문 제출, 취소, 변경
- 주문 수량·현금 배분·레버리지 확정
- 리스크 한도 무시 또는 변경
- 실거래/모의거래 모드 전환

에이전트의 산출물은 `direction`, `target_exposure`, `confidence`, `evidence_ids`, `invalidation`, `risk_flags`처럼 정규화된 의견이다. 이후 Python 리스크 게이트가 포지션 한도, 손실 한도, 시장 상태, 유동성, 중복 주문, 데이터 신선도, dry-run 여부를 검사한다. 게이트 실패 시 주문은 생성하지 않는다.

외부 TradingAgents류 프레임워크 또는 미래의 Student 모델도 같은 경계를 따른다. 리서치 결과 형식이 바뀌거나 API가 지연·실패해도 주문 실행으로 직접 전파되면 안 된다.
