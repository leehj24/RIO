# v8 코드 구조

- main.py: 전체 루프, 조회, 계산, 주문
- config.py: .env 설정
- auth.py: Toss token 자동 갱신
- toss_client.py: Toss 계좌/잔고/가격/캔들/주문
- strategy/live_data_guard.py: LIVE 실데이터 강제
- strategy/llm_pipeline.py: Google evidence + NVIDIA 확률
- strategy/lmsr.py: LMSR 내부 예측시장
- strategy/kelly.py: 예측시장형 Kelly
- strategy/factors.py: 7대 팩터
- strategy/sec_edgar_client.py: SEC submissions/companyfacts
- strategy/yfinance_client.py: yfinance 해외 재무/가격
- strategy/naver_finance.py: Naver/WiseReport 국내 재무요약
- strategy/free_financial_sources.py: 무료 재무데이터 통합
- strategy/sde.py: GBM 상승확률
- strategy/technical_entries.py: Supertrend/DoubleTrend/Dolpa-like/Bollinger/Box
- strategy/exit_rules.py: 손절/익절/분할익절/기술적 매도
- strategy/order_builder.py: KR quantity / US orderAmount
- dashboard_server.py: 로컬 대시보드 API
