# 멀티모델 에이전트 프롬프트 자산

이 디렉터리는 `ai_prompt/02_트레이딩팀.md`부터 `11_추측디코딩.md`까지의 연구 아이디어를 실제 런타임에서 읽는 프롬프트 자산으로 옮긴 것이다. 원본 연구 메모는 변경하지 않고 참고 자료로 유지한다.

## 운용 원칙

- 모든 역할은 NVIDIA의 OpenAI 호환 API 클라이언트를 사용하지만, 역할군마다 서로 다른 모델을 사용한다. 에이전트 MD의 `model_group`과 `model_env`가 라우팅 의도를 선언하고, 실제 모델 ID·시간 제한·호출 한도·API 키는 코드와 환경 변수에서 관리한다.
- `evidence_analysis`는 뉴스·재무·기술·통계·사실/주관 분석 전용 모델군이며 `NVIDIA_SUPER_MODEL`을 사용한다.
- `debate_risk`는 사실/주관 추론, Bull/Bear 토론, 거래 제안, 위험위원회 전용 모델군이며 `NVIDIA_NEMOTRON_MODEL`을 사용한다.
- `final_comparison`은 앞선 분석·찬반·위험 결과를 독립 비교하는 최종 승인 모델군이며 `NVIDIA_FINAL_MODEL`(기본 `z-ai/glm-5.2`)을 사용한다. 이 모델은 새 근거를 만들거나 위험 판정을 완화하지 않는다.
- 어떤 프롬프트도 브로커 주문·계좌 변경·파일 변경 권한을 갖지 않는다. 에이전트의 `target_exposure`는 **제안 비중**일 뿐이다. 실제 주문은 Python 리스크 게이트와 Toss 주문 모듈만 생성할 수 있다.
- 모든 답변은 해당 프롬프트의 JSON 계약만 반환해야 하며, 입력에 없는 사실·출처·시각을 만들어서는 안 된다.

## 디렉터리

```text
providers/                  NVIDIA 멀티모델 provider·라우팅 정책
shared/                     모든 에이전트가 함께 따라야 하는 계약과 정책
agents/                     역할별 system instruction
agent_registry.json         실행 순서·의존성·문서 연결 정보
```

## 연구 메모와 런타임 자산의 연결

| 참고 문서 | 런타임 반영 |
| --- | --- |
| 02 트레이딩팀 | 분석팀, Bull/Bear 토론, 위험위원회, 포트폴리오 관리자 |
| 03 사실주관 | 통계·사실·주관 분리, 별도 추론, 회고 반영 |
| 04 재현감사 | `shared/audit_reproducibility.md`의 입력·모델·체결 감사 필드 |
| 05 매매시뮬 | 브로커와 분리된 `simulation_designer`와 시뮬레이션 정책 |
| 06 구현가이드 | 역할군별 모델 라우팅, 비밀·주문 권한 분리 |
| 07 원문확인 | 차트·뉴스·재무·기술지표를 함께 검토하는 `technical_vision_analyst` |
| 08 이중증류 | 저빈도 Teacher 판단과 고빈도 Student의 분리 계획 |
| 09 에이전트기억 | 원자적 기억 노트, provenance, 버전·링크 규칙 |
| 10 위험필터 | 통계 신호를 의미적으로 축소·보류하는 `semantic_risk_filter` |
| 11 추측디코딩 | API 모델에 적용하지 않는 지연·서빙 최적화 검토 역할 |

## 기본 실행 흐름

```text
시점이 고정된 데이터 스냅샷
  -> evidence_analysis: 통계·사실·주관·펀더멘털·뉴스·심리·기술/차트
  -> debate_risk: 사실/주관 추론, Bull vs Bear 토론, 거래 제안
  -> debate_risk: 위험 3관점·위험관리자·의미 필터
  -> final_comparison: 찬반·위험 결과 독립 비교와 최종 분석 승인
  -> Python 리스크 게이트와 주문 모듈
  -> 감사 로그·회고·기억
```

모델 간 전달은 자유 대화가 아니라 구조화 JSON 보고서로만 이뤄진다. 분석 모델은 최종 결정을 내리지 않고, Bull/Bear·Risk 모델은 원본에 없는 사실을 추가하지 않으며, 최종 비교 모델은 앞선 위험의 `hold/reject`를 `allow`로 뒤집지 않는다. 장중의 빠른 실행과 실제 주문 여부는 기존 Python 규칙이 담당한다.

각 역할 MD의 `depends_on`은 설명용 문구가 아니라 실제 실행 선행 조건이다. 현재 선택된 파이프라인 안에서 선행 역할의 보고서가 없거나 `status: ok`가 아니면 후속 역할은 모델을 호출하지 않고 `required_dependency_unavailable`로 차단된다. 따라서 상세 연구 경로의 `portfolio_manager`는 Bull·Bear·토론 조정·거래 제안·Risk·의미 필터 결과가 모두 유효할 때만 최종 비교를 수행한다.

`live_event_research`는 호출 지연을 줄인 4역할 경로라 `debate_risk`와 `final_comparison`만 호출한다. 뉴스·재무·기술 분석 모델과 Bull/Bear까지 실제로 모두 실행하려면 `NVIDIA_NEMOTRON_PIPELINE=event_research`와 충분한 `NVIDIA_NEMOTRON_MAX_AGENTS_PER_RUN`을 사용해야 한다. 이는 호출 수와 지연을 크게 늘리므로 DRY RUN에서 먼저 검증한다.
