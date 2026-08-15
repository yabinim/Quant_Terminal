# chore(earnings): 2단계 프리뷰 브리핑 필드 스키마 프로브 추가

실적 레이더 2단계(프리뷰 브리핑) 구현 **전** 선행 검증 단계.
본 구현 코드는 이 커밋에 포함되지 않는다.

---

## 파일별 변경

### `automation/diag_earnings_preview_fields.py` (신규, 190줄)

2단계 A/B/C 블록이 의존하는 FMP 필드가 **이 플랜에서 실제로 오는지** 확인하는
일회성 진단 스크립트. 6개 엔드포인트 × 티커별 조회 후,
(1) 호출 종류(ok/plan_limited/http_error), (2) 응답 첫 항목의 실제 키 목록,
(3) 2단계가 쓰려는 기대 필드의 존재·값 유무를 출력한다.

| 블록 | 엔드포인트 | 확인 목적 |
|---|---|---|
| A | `analyst-estimates?period=quarter` | **★ 매출 컨센서스 키 (최대 미지수)** |
| A | `price-target-consensus` | 목표주가 — 기존 사용처 회귀 확인 |
| B | `grades-historical` | 매수의견 비율 5개 필드 |
| B | `earnings` | beat율 · 평균 서프라이즈 폭 원천 |
| C | `news/stock?symbols=` | 종목별 뉴스 (시장 전체 경로와 별개) |
| — | `earning-call-transcript-dates` | 3단계 의존 경로 사전 확인 |

### `.github/workflows/diag_earnings_preview_fields.yml` (신규)

`workflow_dispatch` 전용. `tickers` 입력(비우면 AAPL,NVDA,WMT).

---

## 설계 근거

**왜 프로브를 먼저 돌리나.**
`analyst-estimates` 응답의 매출 추정치 키를 이 코드베이스가 한 번도 읽어본 적이
없다. 코드 전수 검색 결과 `revenueAvg` 류를 참조하는 곳이 0건이다.
그리고 "코드에 호출부가 있다 ≠ 그 경로가 이 플랜에서 동작한다"로 이미 세 번 틀렸다.

- `nasdaq-constituent` — stable 에 존재하지 않음
- `/etf/holdings` — 이 플랜에서 HTTP 402
- `earnings-calendar?symbol=` — symbol 파라미터 자체가 없음(시장 전체 전용)

추정으로 본 구현에 들어가면 틀렸을 때 스키마·시트·앱 카드를 한꺼번에 되돌려야 한다.
프로브 1회(기본 18콜)로 그 위험을 없앤다.

**`repository_dispatch` 트리거 미포함.**
Cloud Scheduler 가 자동 호출하면 일회성 프로브가 매일 콜을 태운다.
`earnings_only.yml` 과 같은 이유.

**종료 코드 항상 0.**
진단 목적이므로 필드가 없어도 워크플로를 실패로 만들지 않는다.
판정은 사람이 요약표를 보고 한다.

**`fmp_get_json_ex` 사용 (`fmp_get_json` 아님).**
`kind` 를 받아야 402(플랜 미제공)와 200-빈응답을 구분할 수 있다.
이 구분이 프로브의 존재 이유다.

## 기각한 대안

| 안 | 기각 사유 |
|---|---|
| 필드명 추정으로 바로 본 구현 | 틀리면 시트 스키마까지 되돌려야 함 |
| 앱에서 수동 확인 | 앱은 실적 탭에서 FMP 를 부르지 않는 설계 — 부수효과 위험 |
| `earnings_only.yml` 에 스텝 추가 | 그 워크플로는 시트에 쓴다. 순수 조회와 섞지 않음 |

---

## 검증

- `py_compile` 통과
- `check_py311.py` 통과 (1개 파일, Python 3.11 호환)
- 모의 응답 스모크 테스트 — 6개 엔드포인트 전부 정상 파싱, 요약표 정확
- **변이 테스트** — 세 가지 실패 유형을 주입해 전부 탐지 확인
  - 402(플랜 미제공) → `❌ 전 종목 호출 실패` + 재검토 목록 등재
  - 200 + 기대 필드 전무 → `⚠️ 기대 필드 중 값을 받은 것이 없음` + 등재
  - 200 + 0건 → `⚠️ 응답은 200 인데 항목 0건` (경로 오류와 구분)

시트 접근 없음 · 이메일 없음 · 알림 상태 머신 미접촉. 재실행 부작용 없음.

---

## 배포

락스텝 대상 없음. 공용 모듈을 수정하지 않으므로 Streamlit 재시작 불필요.

1. `automation/diag_earnings_preview_fields.py` 업로드
2. `.github/workflows/diag_earnings_preview_fields.yml` 업로드
3. Actions → 🔬 진단 — 실적 프리뷰 필드 스키마 → Run workflow
4. 로그 요약표 공유

---

## 한계 · 후속

- **티커 3종목은 표본이 작다.** 대형주에서만 오는 필드일 수 있으니, 워치리스트에
  중소형주가 있으면 `tickers` 입력으로 한 번 더 돌릴 것
- 키가 있는데 `null` 인 경우와 키 자체가 없는 경우는 의미가 다르다.
  전자는 종목 문제, 후자는 설계 변경 사유 — 요약표가 이를 구분해 출력한다
- 본 구현(스키마 27열 · `pass_preview` · `Pre_Ret` 3열 · 앱 카드)은 이 프로브
  결과 확인 후 별도 커밋
