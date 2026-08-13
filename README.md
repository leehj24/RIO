# Aggressive LLM-LMSR Toss Trading Bot

이 프로젝트는 네가 요구한 23개 요구사항을 하나의 토스 전용 자동매매 구조로 묶은 버전입니다.

## 핵심

- 서버비 없음
- 내 PC에서 Python 계속 실행
- 10분마다 자동 실행
- Polymarket 직접 거래 없음
- LMSR 내부 예측시장 엔진 사용
- Sapience SDK는 선택적 외부 예측시장 가격 참고 모듈로만 사용
- OpenClaw 같은 자율 에이전트 프레임워크는 v1에서 사용하지 않고 Python router로 통제
- LLM은 NVIDIA/Gemini 무료 또는 저비용 API 전제
- 뉴스/날씨/스포츠/온체인은 처음부터 유료 API를 붙이지 않고 LLM 검색/요약으로 대체 가능
- 멀티팩터: Value, Momentum, Quality, Risk, Liquidity, Growth/Revision, News/Event/Sentiment
- SDE/GBM 상승확률 계산
- Supertrend, Double Trend, Bolpa-like 추세 필터
- 예측시장형 Kelly Criterion
- 공격형: 8% edge, full Kelly 성향, 단일 포지션 6% cap, 총 노출 80% cap
- 기본 DRY_RUN=true
- 실제 매수/매도는 토스증권 API

## 설치

```powershell
conda create --name toss python=3.11 -y
conda activate toss
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
copy .env.example .env
```

패키지는 Anaconda `base`가 아닌 프로젝트 전용 `toss` 환경에 설치하세요.
이미 환경을 만든 경우 `conda activate toss`부터 실행하면 됩니다.

NumPy ABI 오류(`compiled using NumPy 1.x`)가 발생했다면 `toss` 환경을
활성화한 상태에서 바이너리 패키지를 호환 버전으로 다시 설치하세요.

```powershell
conda activate toss
python -m pip install --upgrade --force-reinstall "numpy>=2,<3" "pandas>=2.2,<3" "scipy>=1.13,<2" "pyarrow>=17"
python -c "import sys; print(sys.executable)"
python -m pip check
```

`.env`에 토스 토큰과 계좌번호를 넣으세요.

```env
TOSS_ACCESS_TOKEN=...
TOSS_ACCOUNT_SEQ=...
```

## 실행

한 번만 실행:

```powershell
python main.py --once
```

10분마다 계속 실행:

```powershell
python main.py
```

대시보드:

```powershell
python dashboard_server.py
```

브라우저:

```text
http://127.0.0.1:5000
```

## 연구 문서 기반 분석·감사 경로

`ENABLE_GEMINI_API2=true`이면 주문 판단 전에 종목별 가격·OHLCV·재무·기술지표를
고정한 ResearchPacket을 Gemini_Api2 역할팀과 Python 규칙이 함께 사용합니다. 누락
필드는 무작위 mock 값으로 채우지 않습니다.

- 13-bin 예측과 이후 결과는 `data/forecast_ledger.jsonl`에 append-only로 기록됩니다.
- Gemini prompt/input/response hash와 latency는 `data/agentic/audit.jsonl`에 기록됩니다.
- 선행-후행 Granger/FDR 후보는 `ENABLE_LEAD_LAG_RESEARCH=true`일 때만 분석 context에
  제공되며, 단독으로 주문을 만들지 않습니다.
- 문서 02~11의 기능·수식·한계 매핑은 [논문_자동투자_구현_매핑.md](docs/논문_자동투자_구현_매핑.md)에 있습니다.

## CLI

계좌 목록:

```powershell
python cli.py accounts
```

잔고/주문가능금액:

```powershell
python cli.py buying-power --account 1
```

보유종목:

```powershell
python cli.py positions --account 1
```

종목 기본정보:

```powershell
python cli.py stocks 005930,NVDA
```

현재가:

```powershell
python cli.py prices 005930,NVDA
```

차트:

```powershell
python cli.py candles 005930 --count 120
```

주문 생성:

```powershell
python cli.py order --account 1 --symbol 005930 --side BUY --order-type LIMIT --quantity 1 --price 70000
```

주문 정정:

```powershell
python cli.py modify <orderId> --account 1 --order-type LIMIT --quantity 2 --price 71000
```

주문 취소:

```powershell
python cli.py cancel <orderId> --account 1
```

## 주의

이 코드는 기본적으로 dry-run 구조입니다. `DRY_RUN=false`로 바꾸기 전에 다음을 확인해야 합니다.

1. 토스 endpoint가 현재 공식 문서와 맞는지
2. 계좌 목록 조회가 되는지
3. 주문 가능 금액 조회가 되는지
4. 보유 종목 조회가 되는지
5. 주문 생성/정정/취소가 소액 dry-run 또는 테스트 환경에서 검증됐는지
6. `MAX_MANAGED_BANKROLL_KRW`가 의도한 값인지
7. `MAX_POSITION_FRACTION`이 너무 크지 않은지

## v2 변경점: 토큰 자동 갱신 + accountSeq 자동 선택

토스 access token은 유효기간이 있으므로 매일 수동으로 `.env`에 넣는 방식이 아니라 `auth.py`의 `TossTokenManager`가 자동 처리합니다.

흐름:

```text
TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 읽기
→ .toss_token_cache.json 확인
→ 토큰 유효하면 재사용
→ 만료됐으면 POST /oauth2/token
→ 새 access token 저장
→ 옵션에 따라 .env의 TOSS_ACCESS_TOKEN도 갱신
```

계좌도 수동으로 accountSeq를 몰라도 됩니다.

```text
TOSS_ACCOUNT_SEQ가 비어 있음
→ GET /api/v1/accounts 호출
→ 첫 BROKERAGE 계좌의 accountSeq 추출
→ X-Tossinvest-Account 헤더에 자동 사용
```

수동 테스트:

```powershell
python cli.py token
python cli.py accounts
python cli.py buying-power
```


## v3 변경점: main 실행 시 대시보드 자동 실행

이제 `python main.py`를 실행하면 전략 루프와 대시보드가 같이 뜹니다.

```powershell
python main.py
```

출력:

```text
[dashboard] http://127.0.0.1:5000
```

브라우저도 자동으로 열립니다. 끄고 싶으면 `.env`에서:

```env
ENABLE_DASHBOARD=false
```

로 바꾸면 됩니다.

## 종목 코드는 어디서 확인하나?

`data/symbols.csv`에서 확인합니다.

```csv
symbol,name,market,sector,theme,enabled
NVDA,NVIDIA,US,Semiconductor,AI Semiconductor,true
005930,삼성전자,KR,Semiconductor,AI Semiconductor,true
```

대시보드에서도 `종목 CSV 목록` 버튼을 누르면 현재 코드가 읽는 종목 목록을 볼 수 있습니다.

## 이벤트 질문은 어디서 확인/수정하나?

`data/event_questions.csv`에서 확인합니다.

```csv
event_id,theme,question,mapped_symbols,b,enabled
ai_semiconductor_demand,AI Semiconductor,향후 3개월 동안 AI 반도체 수요 기대가 더 강해질 가능성은?,"NVDA,SMH,QQQ,005930,000660",100,true
```

## 질문이 매번 똑같으면 안 되는 문제

`data/question_templates.csv`를 추가했습니다.  
`ROTATE_EVENT_QUESTIONS=true`이면 같은 theme 안에서 질문 템플릿을 랜덤으로 선택합니다.

예:

```text
AI Semiconductor theme
→ AI 반도체 수요
→ 빅테크 CAPEX
→ HBM/AI 서버 수요
```

즉 매번 같은 질문만 던지는 게 아니라, 같은 테마 안에서 조금씩 다른 질문을 던질 수 있습니다.


## v4 변경점: 대형 관심종목 universe + 동적 종목 매핑

사용자가 준 전체 관심종목 목록을 `data/watchlist_raw.csv`에 저장했습니다.

실제 전략에서 쓰는 파일은 `data/symbols.csv`입니다.

- `symbol`이 있는 행: 전략/조회/주문 후보로 사용 가능
- `symbol`이 비어 있는 행: 이름은 등록했지만 Toss 주문 심볼이 아직 확인되지 않은 상태
- `enabled=true`: 전략 후보
- `enabled=false`: 아직 비활성 후보

대시보드에서 확인:

```text
종목 CSV 목록
원본 관심종목
```

## 국내 주식 질문 처리 방식

`data/event_questions.csv`에 아래 row가 들어갔습니다.

```csv
kr_domestic_top_candidates,KR Domestic,향후 3개월 동안 국내 주식의 가능성이 높은 종목은?,MARKET:KR,100,true
```

즉 질문이 “향후 3개월 동안 국내 주식의 가능성이 높은 종목은?”이면,
코드가 `symbols.csv`에서 `market=KR`이고 `enabled=true`이며 `symbol`이 있는 종목을 자동으로 가져옵니다.

## 동적 mapped_symbols 문법

`event_questions.csv`의 `mapped_symbols`는 이제 아래 문법을 지원합니다.

```text
MARKET:KR
THEME:AI Semiconductor
THEME:Battery,THEME:Power Grid
005930,NVDA,MSFT
```

예:

```csv
global_ai_semiconductor,AI Semiconductor,향후 3개월 동안 AI 반도체 관련 종목 중 가능성이 높은 종목은?,THEME:AI Semiconductor,100,true
```

이렇게 쓰면 `symbols.csv`에서 `theme=AI Semiconductor`인 종목을 자동으로 가져옵니다.

## 너무 많은 종목 호출 방지

기본값:

```env
MAX_SYMBOLS_PER_EVENT=50
```

`MARKET:KR`처럼 종목이 너무 많은 이벤트는 한 번 실행에서 앞의 50개만 평가합니다.
전체 평가를 원하면 값을 키우면 됩니다.

## 국가·테마 실제 투자 종목

대시보드는 `data/symbols.csv`에서 토스가 실제로 현재가·차트·주문을 지원하는 KR/US 실행 종목만 표시한다. 국가 탭에는 해당 국가에 투자하는 미국 상장 ETF가 나타나며, 모두 자동매매와 토스 캔들 차트 대상이다.

예를 들어 `EWJ`(일본), `INDA`(인도), `VNM`(베트남), `EWT`(대만), `MCHI`(중국), `KSA`(사우디), `EZA`(남아공), `QAT`(카타르)가 있다. 나이지리아는 토스에서 단일국가 ETF가 확인되지 않아 `AFK` 아프리카 ETF를 국가 노출 대체 수단으로 사용한다.

바이오·AI·반도체·자율주행·로봇·양자·방산·에너지·채권·배터리·우주·친환경·헬스케어·건설·구리·자원·재생에너지·은행 ETF도 같은 실행 목록에 있다. `SECTOR:Country ETF` 이벤트가 국가 ETF 전체를 자동매매 평가 대상으로 연결한다.

```bash
# 추가될 행 수·중복 여부만 확인
python cli.py import-global-seed

# 실제 병합: 모든 글로벌 행은 enabled=false 후보로만 추가
python cli.py import-global-seed --apply
```

현재 Toss 주문 경로는 KR·US만 지원한다. 일본·인도·중국 등 현지 거래소 티커는 대시보드 관심종목으로 보관할 수 있지만, 이 프로젝트에서는 자동주문 대상이 될 수 없다. 실수로 `enabled=true`로 바꿔도 주문 빌더가 차단한다.

## 토스 실행 종목 검증

토스는 전체 종목 마스터 다운로드 API 대신 알려진 심볼의 조회 API를 제공한다. 따라서 실행 후보는 실제 토스 종목 정보와 현재가 응답으로 주기적으로 검증한다.

```powershell
python cli.py verify-execution-symbols
```

검증 결과는 `data/toss_execution_verification.csv`에 저장된다. 이 명령은 조회만 하며, 종목을 활성화하거나 주문을 만들지 않는다.


# v5 변경점: Google + NVIDIA 실제 호출 파이프라인

v5는 사용자가 요구한 대로 Google API와 NVIDIA API를 둘 다 사용한다.

## API 역할 분리

### Google/Gemini

역할:

```text
최신 뉴스/정치/경제/기상/스포츠/온체인/테마 evidence 검색/요약
```

하루 호출 제한:

```env
GOOGLE_DAILY_CALL_LIMIT=10
```

질문 예시:

```text
너는 투자 리서치용 검색/증거 수집 엔진이다.

이벤트:
- event_id: kr_domestic_top_candidates
- theme: KR Domestic
- question: 향후 3개월 동안 국내 주식의 가능성이 높은 종목은?

반드시 확인할 영역:
- 경제: 금리, 환율, 인플레이션, 경기, 수급
- 정치/정책: 정부정책, 규제, 보조금, 지정학
- 뉴스: 최근 기업/섹터 뉴스
- 기상: 전력, 농산물, 운송, 재생에너지에 관련 있을 때만
- 스포츠: 스포츠/엔터/방송/이벤트 관련 있을 때만
- 온체인: BTC/크립토/거래소/채굴/ETF 관련 있을 때만
```

받는 답변 JSON 예시:

```json
{
  "event_id": "kr_domestic_top_candidates",
  "searched_domains": ["economy", "politics", "news"],
  "positive_evidence": [
    {
      "title": "반도체 업황 개선",
      "summary": "AI 서버 수요가 HBM 및 반도체 장비 수요를 지지",
      "affected_symbols": ["000660", "042700"],
      "strength": 0.74
    }
  ],
  "negative_evidence": [
    {
      "title": "환율/금리 부담",
      "summary": "고금리와 환율 변동이 성장주 밸류에이션에 부담",
      "affected_symbols": ["035420", "035720"],
      "strength": 0.45
    }
  ],
  "sentiment_score": 0.35,
  "probability_hint": 0.62,
  "confidence": 0.71,
  "top_relevant_symbols": ["000660", "042700", "005930"],
  "risk_flags": ["금리", "수출규제"]
}
```

### NVIDIA

역할:

```text
Google evidence + 이벤트 질문 + 후보 종목을 보고 최종 확률/JSON 판단
```

하루 호출 제한:

```env
NVIDIA_DAILY_CALL_LIMIT=10
```

질문 예시:

```text
너는 공격형 퀀트 이벤트 확률 판단 엔진이다.

Google evidence와 이벤트 질문을 바탕으로
LMSR/Kelly/SDE/팩터 계산에 사용할 확률 입력값을 JSON으로 만든다.

반환 JSON 스키마:
{
  "yes_probability": 0.0,
  "confidence": 0.0,
  "news_score": 0.0,
  "top_candidate_symbols": [],
  "avoid_symbols": [],
  "main_positive_thesis": "",
  "main_negative_thesis": "",
  "risk_flags": [],
  "reason": ""
}
```

받는 답변 JSON 예시:

```json
{
  "event_id": "kr_domestic_top_candidates",
  "yes_probability": 0.66,
  "confidence": 0.74,
  "news_score": 0.35,
  "top_candidate_symbols": [
    {"symbol": "000660", "score_hint": 0.78, "reason": "HBM 수요 민감도"},
    {"symbol": "042700", "score_hint": 0.73, "reason": "반도체 장비 모멘텀"}
  ],
  "avoid_symbols": [
    {"symbol": "035720", "reason": "성장주 밸류에이션 부담"}
  ],
  "main_positive_thesis": "AI 인프라 투자와 HBM 수요가 국내 반도체 후보군에 유리",
  "main_negative_thesis": "금리·환율·수출규제 리스크",
  "risk_flags": ["금리", "환율", "수출규제"],
  "reason": "긍정 근거가 더 크지만 거시 리스크가 남아 있어 confidence는 중간 수준"
}
```

## 하루 10개씩만 쓰는 구조

사용량은 `api_usage_state.json`에 저장된다.

```json
{
  "date": "2026-07-01",
  "providers": {
    "google": {"count": 3, "calls": []},
    "nvidia": {"count": 3, "calls": []}
  }
}
```

예산이 다 떨어지면 API를 더 호출하지 않고 mock fallback을 사용한다.

```text
Google 10회 소진 → Google mock evidence
NVIDIA 10회 소진 → NVIDIA mock judgement
```

## 질문/답변 로그

모든 LLM 질문과 답변은 `llm_calls.jsonl`에 저장된다.

대시보드 버튼:

```text
API 사용량
LLM 질문/답변 로그
```

여기서 아래를 볼 수 있다.

```text
provider
called_api
prompt_type
question
prompt
response
budget
```

# 사용자가 요구한 항목 체크리스트

| 요구사항 | 반영 여부 | 위치 |
|---|---:|---|
| 서버비 안 씀 | O | 로컬 Python |
| 내 PC에서 Python 계속 실행 | O | main.py |
| 10분마다 자동 실행 | O | LOOP_SECONDS |
| Polymarket 직접 거래 안 함 | O | 없음 |
| LMSR 내부 예측시장 | O | strategy/lmsr.py |
| Sapience 선택 외부가격 | O | strategy/data_sources.py placeholder |
| OpenClaw 안 쓰고 Python router | O | main.py |
| Gemini/Google API 사용 | O | strategy/llm_pipeline.py, strategy/llm_providers.py |
| NVIDIA API 사용 | O | strategy/llm_pipeline.py, strategy/llm_providers.py |
| Google은 뉴스/정치/경제/기상/스포츠/온체인 evidence | O | Google prompt |
| NVIDIA는 확률/JSON 최종 판단 | O | NVIDIA prompt |
| 유료 뉴스 API 없이 LLM 검색 우선 | O | Google Gemini grounding |
| 멀티팩터 계산 | O | strategy/factors.py |
| 7대 팩터 | O | Value/Momentum/Quality/Risk/Liquidity/Growth/News |
| SDE/GBM | O | strategy/sde.py |
| Supertrend/DoubleTrend/Bolpa-like | O | strategy/trend.py |
| 예측시장형 Kelly | O | strategy/kelly.py |
| 공격형 운용금/진입비율 | O | .env |
| 운용상한 적용 | O | main.py effective_bankroll |
| 기본 dry-run | O | DRY_RUN=true |
| live 전환 가능 | O | DRY_RUN=false |
| 생존 메커니즘 | O | strategy/risk.py |
| 실제 주문 Toss API | O | toss_client.py |
| Toss token 자동갱신 | O | auth.py |
| accountSeq 자동조회 | O | toss_client.py accounts/resolve_account_seq |
| 종목 CSV | O | data/symbols.csv |
| 원본 관심종목 | O | data/watchlist_raw.csv |
| 질문 CSV | O | data/event_questions.csv |
| 질문 회전 | O | data/question_templates.csv |
| 대시보드 자동 실행 | O | main.py |
| API 각각 10회 제한 | O | strategy/api_budget.py |
| 어떤 질문/답변인지 확인 | O | llm_calls.jsonl + dashboard |


# v6 변경점: 실전형 기술적 매매/매도 룰 추가

v6는 기존 v5 요구사항을 유지하면서 사용자가 추가로 요구한 매매법을 실제 BUY/SELL/EXIT 신호로 분리했다.

## 추가 파일

```text
strategy/technical_entries.py
strategy/exit_rules.py
strategy/order_builder.py
strategy/position_manager.py
strategy/market_data.py
data/actual_data_requirements.csv
```

## 추가 반영된 매매법

| 매매법 | v6 반영 |
|---|---:|
| 슈퍼트렌드 색상 전환 시 즉시 진입 | O |
| 쌍바닥/쌍고점 패턴 결합 | O, 단순 감지 |
| 라운드 패턴 | △, 명시적 라운딩은 아직 단순 패턴으로 대체 |
| 슈퍼트렌드 리테스트 매매 | O |
| 추세 전환 후 10개 이상 캔들 유지 조건 | O |
| 추세선 작도와 슈퍼트렌드 중첩 | △, ST line과 EMA midline 중첩으로 근사 |
| 횡보구간 신호 3회 이상 감지 | O |
| 더블트렌드 점선 돌파 시 진입 | O |
| 실선 반대신호 2회 시 50% 익절 | O |
| 돌파트렌드 실제 시간가중 ATR | X, 비공개 지표라 정확 복제 불가 |
| 돌파트렌드 유사 채널 | O, EMA 중단선 + ATR 상/하단선 |
| 노란색/파란색 채널 상태 | O, close >= midline이면 yellow, 아니면 blue |
| 화살표 신호 발생 | O, ATR 채널 상단/하단 돌파로 근사 |
| 중단선 손절 | O |
| 손익비 1:2 익절 | O, level 계산 및 exit 참고 |
| 채널 중단선 눌림/되돌림 반등 진입 | O |
| 박스권 하단 매수/상단 매도 | O |
| 박스권 돌파/이탈 exit | O |
| 볼린저밴드 추세 유지 중 중단선 눌림 진입 | O |
| 채널 중단 라인 부근 반등 진입 | O |
| 실제 진입 박스 생성 | O, technical.levels에 box_high/box_low 저장 |

## 실제 매매 흐름

```text
Google/Gemini evidence
→ NVIDIA 확률 판단
→ LMSR/EventEdge
→ Kelly
→ 멀티팩터
→ SDE/GBM
→ technical_entries.py의 기술적 진입/매도 신호
→ exit_rules.py의 손절/익절/분할익절/중단선 손절
→ order_builder.py가 KR quantity / US orderAmount 분기
→ Toss create_order
```

## 중요한 주의

돌파트렌드는 비공개 지표이므로 v6는 정확한 복제가 아니라 아래 방식의 응용 구현이다.

```text
EMA 중단선
+ ATR 상단선/하단선
+ 중단선 리테스트
+ 채널 돌파 화살표 근사
+ 중단선 종가 이탈 손절
```

실제 데이터가 없으면 일부는 fallback/mock으로 작동한다.
실거래 전에는 `DRY_RUN=true`로 충분히 로그 확인 후 전환해야 한다.

## 실제 데이터 요구사항

대시보드 버튼:

```text
실제 데이터 요구사항
```

또는 파일:

```text
data/actual_data_requirements.csv
```

에서 확인한다.


# v7 변경점: 실거래 핵심 데이터는 반드시 실제 데이터로 강제

사용자가 요구한 아래 항목을 v7에서 live strict rule로 넣었다.

| 데이터 | v7 처리 |
|---|---|
| 주문가능금액 | LIVE에서는 Toss buying_power 실제 응답 파싱 실패 시 주문 차단 |
| 보유수량 | LIVE에서는 Toss positions 실제 응답 사용 |
| 매도가능수량 | LIVE에서는 sellableQuantity/availableQuantity 없거나 부족하면 매도 차단 |
| 현재가 | LIVE에서는 Toss prices 실제 현재가 파싱 실패 시 주문 차단 |
| 주문 가능 여부 | LIVE에서는 stock_info/market_status 확인 실패 시 주문 차단 |
| 체결 결과 | LIVE에서는 order response의 orderId 확인, 설정 시 order_status 조회 |

## 추가 파일

```text
strategy/live_data_guard.py
```

## 추가 설정

```env
REQUIRE_REAL_DATA_FOR_LIVE=true
BLOCK_LIVE_ON_PRICE_FALLBACK=true
BLOCK_LIVE_ON_OHLCV_FALLBACK=true
REQUIRE_ORDER_STATUS_AFTER_LIVE_ORDER=true
ENABLE_MARKET_STATUS_CHECK=true
ENABLE_STOCK_TRADABILITY_CHECK=true

TOSS_ENDPOINT_ORDER_STATUS=/api/v1/orders/{orderId}
TOSS_ENDPOINT_STOCK_INFO=/api/v1/stocks/{symbol}
TOSS_ENDPOINT_MARKET_STATUS=/api/v1/markets/status
```

## 핵심 원칙

```text
DRY_RUN=true:
mock/fallback 허용

DRY_RUN=false:
주문가능금액, 현재가, 보유수량, 매도가능수량, 주문가능상태, 체결결과가 실제 데이터가 아니면 주문 차단
```

## LIVE에서 막히는 예시

```text
현재가 파싱 실패 → BUY_BLOCKED
보유수량 없음 → SELL_SIGNAL_NO_POSITION 또는 SELL_BLOCKED
매도가능수량 부족 → SELL_BLOCKED
시장 닫힘 → BUY_BLOCKED / SELL_BLOCKED
orderId 없음 → LIVE BLOCKED
order status 조회 실패 → LIVE BLOCKED
```

## 주의

Toss의 정확한 endpoint 경로와 응답 필드명은 최신 Toss OpenAPI 문서와 계좌별 응답으로 반드시 확인해야 한다.
v7은 endpoint를 .env에서 바꿀 수 있게 만들었다.


# v8 최종 검증

- 요구사항 체크 항목: 141개
- Python compile errors: 0개
- 상세 문서:
  - docs/requirements_checklist_v8.csv
  - docs/requirements_checklist_v8.json
  - docs/formulas_v8.md
  - docs/code_map_v8.md
  - docs/verification_report_v8.json

## v8 추가

1. SEC EDGAR submissions/companyfacts
2. yfinance 해외 재무/가격
3. Naver/WiseReport 국내 재무요약
4. 무료 재무데이터를 factor universe에 반영
5. 요구사항 100개 이상 검증 문서

## 남은 제한

- Sapience는 placeholder
- OpenDART/KRX/pykrx는 추천/향후 항목이며 client 미구현
- 돌파트렌드는 비공개 지표라 정확 복제가 아니라 Dolpa-like 응용
- 완전한 Top-N portfolio allocator는 아직 약함
- Toss endpoint/응답 필드는 최신 문서와 실계좌 응답으로 검증 필요


# v9 변경점: 부분반영 항목 실제 구현

아래 4개 항목을 부분반영에서 실제 코드 구현으로 올렸다.

| 항목 | v9 상태 | 파일 |
|---|---:|---|
| Sapience 외부 예측시장 가격 | O | `strategy/data_sources.py` |
| OpenDART | O | `strategy/opendart_client.py` |
| KRX/pykrx | O | `strategy/krx_pykrx_client.py` |
| 라운드 패턴 | O | `strategy/technical_entries.py` |

상세 문서:

```text
docs/v9_actual_integrations.md
docs/requirements_checklist_v9.csv
docs/requirements_checklist_v9.json
```


# v10 변경점: Gemini_Api2 멀티에이전트 연구 워크플로

> 현재 Bull/Bear 토론 및 리스크 역할팀은 NVIDIA Nemotron NIM으로 전환되었다.
> NVIDIA Build의 `deepseek-ai/deepseek-v4-pro` 호스티드 엔드포인트는 종료되었으므로,
> 기본 모델은 현재 계정에서 조회되는 `nvidia/nemotron-3-ultra-550b-a55b`이다.

```env
ENABLE_NVIDIA_DEEPSEEK_AGENTS=true
NVIDIA_DEEPSEEK_API_KEY=your_nvidia_key
NVIDIA_DEEPSEEK_MODEL=nvidia/nemotron-3-ultra-550b-a55b
```

`ai_prompot/02_트레이딩팀.md`부터 `11_추측디코딩.md`까지의 연구 메모를 실행 가능한 역할 프롬프트와 Python 안전장치로 옮겼다. 런타임 프롬프트는 [ai_prompts/README.md](ai_prompts/README.md)에 있으며, 모든 에이전트는 전용 `Gemini_Api2` 키만 사용한다.

## 활성화 전제

기본값은 비활성화다. 기존 `.env`의 `Gemini_Api2` 키를 그대로 사용할 수 있지만, 새 설정명은 `GEMINI_API2_KEY`다. 먼저 `DRY_RUN=true`로 확인한 뒤에만 활성화한다.

```env
ENABLE_GEMINI_API2=true
GEMINI_API2_KEY=your_dedicated_agent_key
GEMINI_API2_MODEL=gemini-2.5-flash
GEMINI_API2_DAILY_CALL_LIMIT=30
GEMINI_API2_MAX_AGENTS_PER_RUN=20
GEMINI_API2_TIMEOUT_SECONDS=60
ENABLE_GEMINI_API2_GROUNDING=false
GEMINI_API2_ENABLE_RESPONSE_SCHEMA=false
```

`GEMINI_API2_MAX_AGENTS_PER_RUN=20`은 한 이벤트의 전체 분석팀·토론·위험위원회를 실행할 수 있는 상한이다. 일일 한도보다 이벤트 수가 많으면 이후 실행은 안전하게 `hold` 처리된다. `Gemini_Api2` 키가 없거나 호출·검증에 실패해도 다른 Gemini/NVIDIA 키로 자동 폴백하지 않으며, 신규 매수는 차단된다.

## 반영 범위

| 연구 메모 | 코드·프롬프트 반영 |
|---|---|
| 02, 03 | 분석팀, 사실/주관 분리, Bull/Bear 토론, 거래 제안 |
| 04 | 입력 스냅샷·프롬프트 해시·모델·응답 append-only 감사 로그 |
| 05 | 실거래와 분리된 가격-시간 우선·부분체결 주문장 시뮬레이터 |
| 06, 07 | Gemini_Api2 전용 provider와 멀티모달/차트 분석 역할 |
| 08 | 검증 전 Teacher 결과·하이퍼엣지·증류 레이블 저장소 |
| 09 | 수정 불가 기억 노트와 키워드·태그 검색 |
| 10 | 의미 위험필터가 신규 매수를 허용·축소·보류하고 Python 리스크가 최종 강제 |
| 11 | 호스티드 Gemini API에서 직접 구현하지 않고, 호출 예산·짧은 JSON·캐시 가능한 설계로 분리 |

에이전트는 `direction`, `target_exposure`, `confidence`, `evidence_ids`, `invalidation`, `risk_flags`만 제안한다. 실제 주문 수량·가격·시장 상태·손절 및 Toss 호출은 계속 Python 리스크/주문 모듈만 결정한다. LLM의 `hold` 또는 위험필터 차단은 신규 매수만 막으며, 기존 포지션의 결정론적 손절·청산은 막지 않는다.
