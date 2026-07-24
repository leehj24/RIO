---
id: audit_reproducibility
version: 1.0.0
provider: gemini_api2
direct_order_execution: false
---

# 재현성·감사 정책

모든 에이전트 호출과 이후의 주문/모의체결은 아래의 감사 레코드에 연결될 수 있어야 한다. 이 문서는 LLM이 로그를 쓰라는 명령이 아니라, 런타임이 보존해야 할 필드의 계약이다.

```json
{
  "run_id": "실행 ID",
  "decision_time_utc": "신호 확정 시각",
  "data_cutoff_utc": "모델 입력의 가장 늦은 허용 시각",
  "input_snapshot_hash": "입력 스냅샷 해시",
  "source_ids": ["원문/데이터 ID"],
  "prompt_id": "agents/*.md의 id",
  "prompt_version": "프롬프트 버전",
  "prompt_hash": "렌더된 프롬프트 해시",
  "provider": "gemini_api2",
  "model": "실제 호출 모델",
  "response_hash": "검증된 JSON 응답 해시",
  "execution_rule": "dry_run | next_open | next_close | limit | none",
  "fill_price": null,
  "fee_spread_slippage": null,
  "turnover": null,
  "universe_snapshot_id": "거래 가능 종목 스냅샷",
  "fallback_or_error": null
}
```

실제 체결 가격과 비용은 LLM이 추측하지 않는다. 주문/체결 모듈이 기록한다. 재현 시험은 동일 입력 스냅샷, 프롬프트 해시, 모델 ID, 실행 규칙, 비용 가정을 사용하며, 미래 데이터와 누락된 외부 데이터 건수를 별도 보고한다.
