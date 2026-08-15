# chore(earnings): C블록 후보 2차 프로브 — 내부자 통계 경로 정정판

1차 프로브의 404 오판을 정정하고, 정식 경로로 다시 확인한다.

---

## 왜 다시 찌르는가

1차 프로브에서 `insider-trade-statistics?symbol=` 로 찔러 404 를 받고
**"플랜 제한"으로 오판**했다. 공식 API 레퍼런스 확인 결과 경로 오류였다.

| | 경로 |
|---|---|
| 1차에서 쓴 것 | `insider-trade-statistics?symbol=` ❌ |
| 정식 경로 | `insider-trading/statistics?symbol=` ✅ |

반면 아래 셋은 경로가 정확했고 402 도 진짜다. 되살릴 여지 없음.

| 엔드포인트 | 문서상 경로 | 1차 결과 |
|---|---|---|
| 보도자료 | `news/press-releases?symbols=` | 경로 일치 → 402 확정 |
| 트랜스크립트 | `earning-call-transcript-dates?symbol=` | 경로 일치 → 402 확정 |
| 애널리스트 예측 | `analyst-estimates?symbol=&period=` | 경로 일치 → 402 확정 |

`insider-trading/statistics` 가 살아 있으면 1차에서 지적한 함정 두 개가
설계 없이 자동으로 풀린다.

| 1차 함정 | 통계 엔드포인트가 푸는 방식 |
|---|---|
| A-Award / F-InKind 오염 | `totalPurchases`/`totalSales` 가 이미 재량 P/S 만 센 값 |
| `limit=20` 이 종목마다 다른 기간을 덮음 | 분기 버킷 반환 → 창 길이 고정 |

20행 파싱 + 유형 필터 + 창 보정 코드가 통째로 사라지고 **1콜**로 대체된다.

---

## 변경 파일

### 신규 `automation/diag_earnings_cblock_probe2.py` (241줄)

총 4콜. `insider-trading/statistics` × 3종목 + `insider-trading-transaction-type` × 1.

**200 이라고 곧바로 채택하지 않는다.** 경로 생존과 실사용 가능성은 별개다.
스크립트가 직접 판정하는 것:

- 설계안 컬럼이 쓸 필드 5개(`year`/`quarter`/`acquiredDisposedRatio`/
  `totalPurchases`/`totalSales`) 존재 여부
- 복수 분기 반환 여부 — 실적일에 가장 가까운 분기를 고를 수 있는가
- **신선도** — 분기 종료일 기준 경과일수
  - `≤ 45일` 신선 / `≤ 120일` 1분기 지연, `Insider_Q` 기록 필수
  - `> 120일` 2분기 이상 묵음 → 실적 이벤트용 부적합, C블록 재검토
  - **`< 0일` 진행 중인 분기 = 미완결**

### 신규 `.github/workflows/diag_earnings_cblock_probe2.yml` (68줄)

`workflow_dispatch` 전용. `repository_dispatch` 없음.
`GOOGLE_API_KEY` 불필요 — 서술 노선이 죽어 Gemini 의존이 사라졌다.

---

## 검증

- `py_compile` ✅ / `check_py311.py` ✅ / YAML 파싱 ✅ (job 1 / step 5)
- **스텁 테스트** 4종:
  A(신선·복수분기) / B(1분기 지연·단일분기) / C(묵음 + 필드 누락) / D(402).
  판정 분기 전부 의도대로 출력
- **변이 테스트에서 잡은 것** — 초기 구현은 진행 중인 분기를 경과 −46일로
  계산해 "신선도 양호"로 판정했다. 실제로는 미완결이라 `totalSales` 가
  분기 마감까지 계속 늘어나고, **같은 분기라도 스냅샷 시점마다 값이 달라진다.**
  백테스트에서 스냅샷 간 비교가 성립하지 않는다는 뜻이다.
  음수 경과를 별도 분기로 빼고 경고를 출력하도록 수정

---

## 배포

공용 모듈 미변경. `app.py`·`earnings_core.py`·`run_earnings_watch.py` 무접촉.
락스텝 대상 없음. Streamlit 리부트 불필요.

    automation/diag_earnings_cblock_probe2.py
    .github/workflows/diag_earnings_cblock_probe2.yml

---

## 결과에 따른 분기

| 프로브 결과 | 다음 |
|---|---|
| 생존 + 신선 | 설계안대로 컬럼 4개 구현. 스냅샷당 4콜 → 5콜 |
| 생존 + 미완결 분기 | 컬럼에 분기 마감 여부 플래그 추가 후 구현 |
| 생존 + 120일 초과 지연 | C블록 재검토. 근거가 약하다 |
| 402 | 1차에서 3/3 확인된 `insider-trading/search` 20행 파싱으로 후퇴 (함정 1·2 부활) |

---

## 참고 — 문서 파일 취급

`api-docs.pdf` 는 확장자만 `.pdf` 이고 실제로는 마크다운 텍스트다.
`unzip`(구 `FMP_API_list.pdf` 방식)도 `pdftotext` 도 실패한다. 그냥 텍스트로 읽는다.

구 `FMP_API_list.pdf` 를 "플랜 목록"으로 취급하던 것도 오류였다.
그 문서에는 402 나는 엔드포인트가 그대로 실려 있다 — 플랜 목록이 아니라 전체 카탈로그다.
"문서에 있다 = 내 플랜에서 된다"는 추론은 성립하지 않는다.
