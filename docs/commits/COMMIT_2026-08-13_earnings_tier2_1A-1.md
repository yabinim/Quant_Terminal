# feat(earnings): FMP HTTP 계층 SSOT 분리 + 실적 레이더 Tier 2 유니버스 (1-A)

실적 프리뷰 브리핑 3단계 중 **1-A(자동화 전용)**. app.py 무변경.

---

## 1. 파일별 변경

### 신규 `fmp_http.py`
FMP 호출의 레이트리밋·재시도·URL 조립 SSOT. **streamlit 무의존.**

기존에 FMP 호출 경로가 셋으로 갈라져 있었다.

| 경로 | 레이트리밋 | 재시도 |
|---|---|---|
| `fmp_extras.fmp_get()` | O (200/분) | O (3회) |
| `fmp_extras._get_json()` | ✗ | ✗ |
| `earnings_core._get()` | ✗ | ✗ |

아래 둘은 429 를 만나면 재시도 없이 `None` 을 돌려주고, 호출부는 그것을
"데이터 없음"으로 처리한다 → **조용히 틀린 값**. 종목 수가 적어 지금까지
드러나지 않았을 뿐이며, Tier 2(~140종목)를 붙이면 반드시 터진다.

`fmp_extras` 의 구현을 그대로 옮기고 API 키만 **주입식**으로 바꿨다
(`scanner_core.set_fmp_key_provider` 와 동일 패턴).

- `set_key_provider(fn)` / `fmp_key()` — 제공자 → 환경변수 폴백
- `fmp_rate_limit_acquire()` — 슬라이딩 윈도우
- `fmp_get(url, timeout, retries)` — 429/402/5xx 지수 백오프+지터, 4xx 즉시 포기
- `fmp_url(path, key)` / `fmp_get_json(path, timeout, retries, key)`
- `fmp_stats()` / `fmp_reset_stats()` / `fmp_stats_line()`

### `fmp_extras.py`
- `import fmp_http as _fh` 추가, `_fh.set_key_provider(_key)` 설치
  (앱은 `st.secrets`, 자동화는 환경변수 — 기존 `_key()` 동작 그대로)
- `_get_json()` → `_fh.fmp_get_json()` 위임. **반환 계약(비200 → `None`) 동일.**
- 레이트리미터 구현 블록(구 888~1006행) 삭제 → `_fh` 재노출로 대체
  - `FMP_RATE_LIMIT_PER_MIN`, `FMP_MAX_RETRIES`, `fmp_rate_limit_acquire`,
    `fmp_get`, `fmp_stats`, `fmp_reset_stats`, `fmp_stats_line`
  - `scanner_core` / `run_hidden_alpha` / `run_scanner_scan` / `app.py` 는 무변경
- `import random/threading/deque` 제거(해당 블록 전용이었음)

### `earnings_core.py`
- `import fmp_http as _fh` — 이 파일의 설계 불변식("streamlit 을 import 하지
  않는다")을 깨지 않는다. `fmp_extras` 를 직접 import 하지 않은 이유가 이것이다.
- `_get()` → `_fh.fmp_get_json(path, timeout, key=k)` 위임.
  **`key` 인자를 반드시 전달한다** — app.py 는 `st.secrets` 키를
  `ec.fetch_next_earnings(..., key=_fmp_key())` 로 넘긴다. 무시하면 앱에서
  실적 조회가 죽는다.
- `SOURCE_USER` / `SOURCE_UNIVERSE`, `normalize_source()`, `is_universe_only()`
- `CALENDAR_COLS` 20 → **21열**, `Source` 를 **맨 끝(Notes 뒤)** 에 추가
- `calendar_row(..., source="")` — 빈 값이면 `prev` 보존
- `Earnings_Universe` 스키마: `UNIVERSE_WORKSHEET/COLS/NCOL`,
  `UNIVERSE_MIN_MARKET_CAP`(1,500억 달러), `UNIVERSE_REFRESH_WEEKDAY`(월)
- `fetch_market_universe()` — NDX100 ∪ (S&P500 ∩ 시총 하한) = **3콜**
- `universe_row()` / `parse_universe()` / `merge_universe_sources()`

### `run_earnings_watch.py`
- `_universe_ws()` / `load_market_universe()` / `pass_universe()`
- `pass_calendar(..., user_set=)` — 집합 밖 티커는 `SOURCE_UNIVERSE` 로 기록
- `pass_snapshot()` — `is_universe_only()` 행 건너뜀(스냅샷/이메일/축소 제외)
- `main()` — 패스 U 신설. **Tier 1 을 먼저 스캔**하고 유니버스를 뒤에 붙인다
  (타임아웃이 나도 사용자 종목은 갱신되도록)

### 신규 `diag_earnings_tier2.py`
네트워크·Sheets 없는 순수 로직 회귀 검사. `workflow_dispatch` 전용.

---

## 2. 설계 근거 · 기각안

### `Source` 를 맨 끝에 둔 이유
`Notes` 앞에 끼우면 기존 행의 `Notes` 값이 `Source` 로 오독된다. 맨 끝이면
구 행은 공란 → `normalize_source("")` → `user` 로 해석되어 **마이그레이션이
불필요**하다. 실패 방향도 안전한 쪽이다 — 잘못 `universe` 로 읽히면 기존
종목의 스냅샷·이메일이 조용히 끊긴다.

### 시총 "상위 100" 이 아니라 "하한선" 인 이유
FMP 에 S&P 100 전용 엔드포인트가 없다. 시총 상위 100 을 뽑아 "S&P 100"
이라 부르는 것은 **틀린 라벨**이다 — 실제 OEX 는 위원회가 섹터 균형·옵션
유동성을 보고 고른다. 목적이 지수 복제가 아니라 "대형주 실적 지형"이므로
하한선이 맞고, 시장이 커지면 자연히 편입되어 자기 유지된다.

### 기각: Tier 2 에 풀 브리핑
관심 표명이 없는 130여 종목에 매일 카드를 띄우면 발굴 피드가 된다.
실적 방향 예측은 `diag_earnings_preview_backtest` 에서 엣지가 확인되지
않았고(beat율·서프라이즈 폭·상대강도·등급 drift·매수의견 모두 단조성 없음),
스캐너·내러티브·Hidden Alpha 가 이미 담당하는 퍼널과 중복된다.

### 기각: `earnings_core` 가 `fmp_extras` 를 직접 import
`fmp_extras` 에 streamlit 심(shim)이 있어 동작은 하지만, `earnings_core` 의
명시적 설계 불변식을 깬다. 무의존 모듈로 분리하는 편이 카운터 단일화라는
목적에도 부합한다.

### 기각: `earnings_core` 에 스로틀 자체 구현
카운터가 둘로 분리되어 프로세스 합산 호출량이 한도의 2배가 된다.

### 유니버스 시트를 append 가 아니라 전체 덮어쓰기로 한 이유
멤버십 스냅샷이므로 이전 행을 남기면 편출 종목이 영원히 남는다.
단, `fetch_market_universe()` 가 `ok=False` 면 **기존 시트를 그대로 둔다** —
부분 결과로 덮으면 멤버십이 조용히 반토막 나고 전체 덮어쓰기라 복구가 안 된다.

---

## 3. 검증

- `py_compile` — 4개 파일 통과
- `check_py311.py` — 4개 파일 Python 3.11 호환
- `diag_earnings_tier2.py` — 13개 항목 전부 통과
  - 구 20열 행의 `Notes` 보존 및 `user` 해석
  - `calendar_row` 의 `source` 기록 / `prev` 보존
  - `pass_snapshot` 의 Tier 2 제외
  - `merge_universe_sources` 출처 라벨·시총 정렬
  - `parse_universe` 왕복
- 재노출 동일성 — `fx.fmp_get is fh.fmp_get` = `True`
- 키 주입 — `st.secrets` → 제공자 → 환경변수 폴백 3단계 확인
- `diag_market_gate.py` — SSOT 매니페스트 검사 통과
  (실패 2건은 `automation/` 경로 전제에 따른 것으로, 이번 변경과 무관)

---

## 4. 배포 순서 (락스텝)

1. `fmp_http.py` (신규)
2. `fmp_extras.py`
3. `earnings_core.py`
4. `automation/run_earnings_watch.py`
5. `automation/diag_earnings_tier2.py` (선택)

**1번을 먼저 올릴 것.** 2·3번이 `fmp_http` 를 import 하므로 없으면
`ModuleNotFoundError` 로 앱과 자동화가 동시에 죽는다.

app.py 무변경이므로 Streamlit 재부팅은 2·3번 반영을 위해서만 필요하다.

첫 실행 시 자동으로 일어나는 일:
- `Earnings_Calendar` 21열로 확장 + 헤더 재기록 (`_ws` 의 `[MIGRATE]` 로그)
- `Earnings_Universe` 시트 생성 (`[INIT]` 로그)
- 요일과 무관하게 유니버스 1회 계산 (시트가 비어 있으므로)

---

## 5. 남은 한계 · 후속

- **`company-screener` 응답 미검증** — 네트워크 없는 환경에서 만들었다.
  첫 실행 로그의 `[UNIV]` 줄에서 `screener=` 개수를 반드시 확인할 것.
  0 이면 파라미터명(`isEtf`/`isActivelyTrading`)이나 플랜 제한 문제다.
  실패해도 시트를 덮지 않으므로 안전하게 실패한다.
- **첫 실행 콜 수 급증** — 유니버스 ~140종목이 캘린더에 새로 들어오면서
  전원 경량 조회 + D-10 이내는 정밀 조회로 승격된다. 25~30분 타임아웃에
  걸릴 수 있다. 걸리면 다음 실행이 이어서 채운다(`needs_refresh` 가 생략 처리).
- **`diag_earnings_tier2.yml` 미작성** — `workflow_dispatch` 트리거 필요.
- **1-B 대기** — app.py 지형표 + `_SSOT_NEEDS` 에 `earnings_core` 항목 추가
  (현재 목록에 없음). 데이터가 한 주 쌓인 뒤 붙이는 것이 낫다.
- **2단계 예고** — `Earnings_Preview` 시트(1행=1스냅샷), A/B 블록,
  D-7 / D-3 / 최종(AMC=D-1, BMO=D-2) 3회 스냅샷.

---

# 개정 (2026-08-13 2차) — 유니버스 정의 K-3

첫 실행 로그에서 `[UNIV] 조회 실패(ndx=0 sp500=0 screener=196)` 가 나온 뒤의 수정.

## 원인

업로드된 FMP API 문서(`FMP_API_list.pdf`) 전문 검색 결과:

| 경로 | 존재 |
|---|---|
| `stable/sp500-constituent` | ✅ (그러나 이 계정에서 빈 응답) |
| `stable/dow-jones-constituent` | ✅ |
| `stable/historical-nasdaq-constituent` | ✅ (현재 명단 아님 — 편입/편출 이력) |
| `stable/nasdaq-constituent` | ❌ **존재하지 않음** |

`sp500-constituent` 가 있으니 대칭으로 `nasdaq-constituent` 도 있으리라 가정하고
문서 확인 없이 작성한 것이 원인이다. 문서가 프로젝트에 있었으므로 방지 가능했다.

`company-screener` 는 정상(196종목)이었고, 안전 실패 설계가 의도대로 작동해
`Earnings_Universe` 시트를 덮어쓰지 않았다.

## 변경

### `fmp_http.py`
- `fmp_get_ex()` / `fmp_get_json_ex()` 추가 — `(data, status, kind)` 반환.
  실패를 `None` 하나로 뭉개면 **404(경로 오류)·403(플랜)·빈 200 응답을 구분할 수
  없다.** 이번 사고에서 로그만으로 원인을 못 짚은 이유가 정확히 이것이다.
- `fmp_get()` / `fmp_get_json()` 은 위 함수로 위임 — 기존 계약 불변.

### `earnings_core.py`
유니버스 정의를 **K-3(ETF 멤버십 + 스크리너 보강)** 으로 교체.

- `_etf_membership(etf, key, top_n)` — `/etf/holdings` 기반
  - **QQQ 전량** ≈ 나스닥 100
  - **SPY 비중 상위 100** ≈ S&P 500 시총 상위 100 (비중 = 시총 가중)
  - `/etf/holdings` 는 app.py 4249·6691(ETF 유니버스·Hidden Alpha 중복도)에서
    이미 프로덕션으로 도는 **동작 확인된 유일한 멤버십 경로**다.
- `_screener_map(min_cap, key)` — `exchange=NASDAQ,NYSE` 필터 추가
- `fetch_market_universe()` — 스크리너를 **폴백이 아니라 항상 호출**한다.
  `Market_Cap`/섹터 보강용이며, ETF 두 경로가 모두 실패할 때만 유니버스 소스로 승격.
- 상수 분리
  - `UNIVERSE_SPY_TOP_N = 100`
  - `UNIVERSE_SCREENER_MIN_CAP = 250억` — 보강용(QQQ 하위권까지 덮도록 넉넉히)
  - `UNIVERSE_MIN_MARKET_CAP = 1,500억` — **폴백 유니버스 전용** 하한
- `UNIVERSE_EXCLUDE_TICKERS` — ETF 현금 항목 차단. 스텁 테스트에서 `USD` 가
  문자 규칙(3~5자 알파벳)을 통과했다. `XTSLA`(BlackRock 현금) 등 포함.
- 복수클래스(`BRK.B`) · 중복 티커 · 빈 티커 제거

### `run_earnings_watch.py`
- `[UNIV]` 로그에 HTTP 상태·출처·시총 채움 건수 노출

### 신규 `earnings_only.yml`
실적 레이더 **단독** 수동 워크플로. `workflow_dispatch` 전용.

5PM 워크플로를 수동 실행하면 `run_watchlist_alerts.py` 가 같이 돌아
**알림 상태 머신이 하루치 더 진행된다**(`regime_core` 826행: "하루 1회 호출 전제.
호출 1회 = 평가 1회로 pending 카운터가 1 진행된다"). 2일 확정이 같은 날 1일 만에
확정되어 신호가 조기 발동한다. 2026-08-13 21:46 수동 실행에서 실제로 발생했다.

`repository_dispatch` 는 **의도적으로 넣지 않았다** — Cloud Scheduler 가 자동
호출하면 5PM 실행과 중복되어 캘린더가 두 번 갱신된다.

## 규모 변화

SPY 상위 100 + QQQ 100 → 중복 제거 후 **약 130~140종목** 예상.
캘린더 대상 = Tier 1 87 + Tier 2 ~135 ≈ **220종목**.

- 첫 실행: 전원 경량 조회 + D-10 이내는 정밀(3콜) 승격 → 약 250~320콜
- 정상 운영: far 30일·mid 7일·near 매일 티어 규칙으로 하루 100~150콜
- 200콜/분 스로틀 기준 시간·타임아웃 모두 여유가 있다. 첫 실행이 30분을
  넘기더라도 `needs_refresh` 생략 처리로 다음 실행이 이어서 채우며,
  Tier 1 을 먼저 스캔하므로 사용자 종목은 어떤 경우에도 갱신된다.

## 검증

- `py_compile` · `check_py311.py` — 5개 파일 통과
- `diag_earnings_tier2.py` — **22개 항목 전부 통과** (9개 추가)
  - ETF 응답 스텁: 현금(`USD`/`XTSLA`) 제외, `BRK.B` 제외, 중복 제거
  - 교집합 `BOTH` 라벨 + 스크리너 시총 보강
  - ETF 전멸 → 스크리너 폴백 승격 + 1,500억 하한 적용
  - 전멸 → `ok=False` 안전 실패(시트 미갱신)
  - HTTP 상태가 진단 문자열에 노출되는지
- `earnings_only.yml` — YAML 파싱 및 트리거 검증

## 남은 한계

- **ETF 보유 데이터는 신고 기준이라 며칠~한 달 지연될 수 있다.** 멤버십 용도로는
  무해하다(지수 편입은 드물게 바뀐다).
- `sp500-constituent` 가 왜 빈 응답인지는 미규명. 이제 `fmp_get_json_ex` 가 있으므로
  필요하면 HTTP 코드로 플랜 제한 여부를 즉시 확인할 수 있다.
- **`fmp_extras._top_holdings_set`(693행) 잠재 버그** — `live["symbol"]` 컬럼을
  찾는데 `fmp_etf_holdings` 반환 컬럼은 `[asset, name, weight_pct]` 다.
  `symbol` 이 없어 라이브 경로가 **항상 하드코딩 폴백 맵으로 떨어진다.**
  위성 섹터 중복도 계산에 영향. 이번 범위 밖 — 별도 처리 필요.
- 2026-08-13 21:46 재실행으로 조기 확정된 워치리스트 신호는 되돌릴 수 없다
  (`Alert_LastState` 백업 없음). 해당 신호는 확정 강도가 절반이다.
