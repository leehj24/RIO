# v9 추가 구현

사용자가 부분반영이 아니라 실제 구현을 요구한 4개 항목을 추가했다.

## 1. Sapience 외부 예측시장 가격

파일:

```text
strategy/data_sources.py
```

구현:

```text
SapienceClient.get_event_price(event_id)
→ GET {SAPIENCE_BASE_URL}{SAPIENCE_PRICE_ENDPOINT}
→ yes_price/yesProbability/probability/price/orderbook midpoint 파싱
→ 0~1 probability만 반환
```

설정:

```env
ENABLE_SAPIENCE_PRICE=false
SAPIENCE_BASE_URL=
SAPIENCE_PRICE_ENDPOINT=/markets/{event_id}
SAPIENCE_API_KEY=
```

주의: 직접 거래가 아니라 외부가격 reference만 읽는다.

## 2. OpenDART

파일:

```text
strategy/opendart_client.py
strategy/free_financial_sources.py
```

구현:

```text
corpCode.xml 다운로드
stock_code -> corp_code 매핑
fnlttSinglAcntAll.json 호출
매출/영업이익/순이익/자산/부채/자본 추출
ROE, 부채비율, 영업이익률, 순이익률 계산
```

계산:

```text
ROE = NetIncome / Equity
DebtRatio = Liabilities / Assets
OperatingMargin = OperatingIncome / Revenue
NetMargin = NetIncome / Revenue
```

설정:

```env
ENABLE_OPENDART=true
OPEN_DART_API_KEY=
OPENDART_CACHE_DIR=data_cache/opendart
```

## 3. KRX/pykrx

파일:

```text
strategy/krx_pykrx_client.py
strategy/free_financial_sources.py
```

구현:

```text
pykrx.stock.get_market_cap_by_ticker()
pykrx.stock.get_market_ohlcv_by_date()
시총/거래량/거래대금/상장주식수/종가 추출
```

계산:

```text
Turnover = TradingValue / MarketCap
```

설정:

```env
ENABLE_PYKRX=true
PYKRX_CACHE_DIR=data_cache/pykrx
```

## 4. 라운드 패턴

파일:

```text
strategy/technical_entries.py
```

구현:

```text
round_bottom_top()
```

라운드 바닥 조건:

```text
2차식 계수 a > 0
저점이 중앙부
왼쪽 기울기 < 0
오른쪽 기울기 > 0
종가가 중단선/목선 위로 회복
```

라운드 탑 조건:

```text
2차식 계수 a < 0
고점이 중앙부
왼쪽 기울기 > 0
오른쪽 기울기 < 0
종가가 중단선/기준선 아래로 이탈
```

트리거:

```text
round_bottom_pattern_buy
round_top_pattern_sell
```
