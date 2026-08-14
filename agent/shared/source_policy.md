---
id: source_policy
version: 1.0.0
provider: nvidia_nemotron
direct_order_execution: false
---

# 출처·시점·사실/주관 정책

## Provenance

모든 주장에는 입력에서 온 `source_id` 또는 `evidence_id`를 연결한다. 입력에 URL·발행 시각이 있으면 그대로 보존하며, 없으면 `null`로 둔다. 출처를 추정하거나 지어내지 않는다.

```json
{
  "evidence_id": "입력 ID",
  "claim": "짧고 검증 가능한 주장",
  "source_type": "filing | market_data | news | chart | financial_statement | social | memory",
  "published_at": "원문 발행 시각 또는 null",
  "observed_at": "수집 시각 또는 null",
  "data_cutoff_utc": "의사결정에 허용된 마감 시각",
  "reliability": 0.0
}
```

## 사실과 주관의 분리

- **fact**: 가격·거래량·재무 수치, 공시, 규제 발표처럼 검증 가능한 사건/수치다.
- **subjectivity**: 의견, 전망, 루머, 감정, 인플루언서 발언처럼 발화자와 시장 반응이 중요한 신호다.
- 하나의 기사에 둘 다 있으면 문장/주장 단위로 나눈다. 해석이나 전망을 `facts`에 넣지 않는다.

## 시간 일관성

- `published_at` 또는 `observed_at`이 `data_cutoff_utc` 이후이면 근거로 사용하지 않고 `future_data_risk`를 남긴다.
- 뉴스의 게시 시각과 사건 발생 시각을 혼동하지 않는다.
- 차트 이미지는 보조 증거다. 수치 가격·거래량 원본이 있으면 원본을 우선한다.
- 과거 기억은 가설 후보일 뿐이며 최신 시장 데이터로 다시 검증한다.

## 불확실성

서로 충돌하는 출처, 오래된 정보, 낮은 신뢰도의 소셜 신호, 결측 데이터는 숨기지 않고 `risk_flags`와 `counter_evidence`에 남긴다. 근거가 하나뿐이거나 검증되지 않은 인과 관계는 강한 결론의 근거가 될 수 없다.
