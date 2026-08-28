# fix(app): historical-price-eod 조회 창을 limit → from/to 로 전환하고 scanner_core 시그니처 결합을 복구

`app.py` 의 가격 히스토리 조회를 전부 `from`/`to`(달력일) 기반으로 옮기고,
2026-08-27 `scanner_core` 전환 때 따라오지 못해 **전건 TypeError 상태였던
호출부 18곳**을 복구한다. 환산 상수·보유창 규칙은 `fmp_extras` 로 단일화한다.

---

## 1. 왜 지금인가 — 발견된 결함 3건

### (a) 🔴 `scanner_core` 시그니처 드리프트 — 호출부 18곳 전건 TypeError

`scanner_core._fmp_price_history` 는 2026-08-27 에 `limit` → `lookback_days`(달력일)
로 바뀌었고 `_fmp_batch_price_history` 는 `lookback_days` 가 **필수 키워드**가 됐다.
그런데 `app.py` 호출부는 하나도 갱신되지 않았다. AST 전수 조사 결과 `lookback_days=`
호출 **0곳**, `limit=` 호출 **18곳**.

전부 `try: ... except Exception:` 안이라 예외가 삼켜지고 빈 DataFrame 으로 떨어졌다.
관측된 영향:

| 호출부 | 결과 |
|---|---|
| 워치리스트 SPY (2곳) | `_spy_close = None` → RS·레짐의 **벤치마크 비교가 통째로 꺼짐** |
| 포트폴리오 SPY | 매도 레이더 기준선 상실 |
| 배당 DRIP | "가격 이력 조회 실패" — 재투자 미기록 |
| 섹터 리더/스캐너 | 빈 결과 |

**`check_freshness.py` 정합성 검사가 왜 못 잡았나:** "app.py 가 쓰는 55개 심볼 모두
존재" — **심볼 존재만** 본다. 시그니처는 안 본다. `_fmp_price_history` 는 존재하므로
✅ 가 떴다. 이번에 `[A4]` 결합 검사를 추가해 이 사각지대를 덮는다.

### (b) 🔴 `cached_timing_price_history` — 시장 진입 게이트가 처음부터 꺼져 있었다

```python
r = requests.get(f"...&limit=260&apikey={k}")   # limit 은 FMP 가 무시 → 5년 피드
...
cutoff = pd.Timestamp.now() - pd.DateOffset(years=1)
df = df[df.index >= cutoff]                      # ← 진짜 구속 조건. 250~252봉
```

실제 공급은 **251봉**이었고 `limit` 값과 무관했다. 하류 요구는 그보다 위에 있다:

- `regime_core.market_warnings` **260봉 하드게이트** → 미달 시 `None`
  → `market_gate_status.available=False` → **fail-open 으로 게이트가 통째로 꺼진다**
- 같은 함수 rolling 체인(vol 20창 → 252 중앙값) 요구 **272봉**
- `classify_regime` / `compute_position_drawdown` 요구 **252봉**

251 < 260 이므로 게이트는 **한 번도 켜진 적이 없다.** 게다가 워치리스트 배너는
`if _wl_mkt.get("available")` 안에 있어 배너조차 안 떴다 — 바로 그 위 주석이
막으려던 상황("배너가 없으면 '메일은 안 왔는데 앱엔 매수 신호'가 설명되지 않는다")이
발생 중이었다.

### (c) 🟡 보유 종목 창이 앱과 자동화에서 갈렸다

트레일링 스톱 기준 고점은 `close[index >= Date_Added]` 의 최대값 — **보유 기간에
비례**한다. 자동화는 이미 `_hist_days_for_holding` 으로 동적 창을 쓰고 있었으나
앱은 고정 600봉이었다. 3년 보유 종목이면 앱 쪽 기준 고점이 조용히 낮아져
**같은 종목을 두고 메일과 화면의 매도 판정이 갈렸다**(앱이 덜 뜨는 방향 = 더 위험).

---

## 2. 파일별 변경

### `fmp_extras.py` (915 → 1,049줄) — 창 환산·보유창 SSOT

신규:

| 심볼 | 역할 |
|---|---|
| `HIST_TD_PER_CD = 0.6871` | 실측 거래일÷달력일 비율 |
| `HIST_WINDOW_DAYS = 460` | 기본 창(달력일) ≈ 316봉 |
| `HIST_MAX_DAYS = 1826` | 상한(약 5년) |
| `bars_for_calendar_days()` / `calendar_days_for_bars()` | 명시 단위 변환기 |
| `hist_range_params(days)` | `&from=..&to=..` 조립 (`to` = 오늘+1일 방어) |
| `hist_days_for_holding()` | 보유 기간 비례 창 — `run_watchlist_alerts` 에서 **승격** |
| `hist_days_for_target_date()` | 특정 과거 날짜 커버 창 — 배당 지급일용 **신규** |

`fmp_extras` 를 고른 이유: 이미 streamlit 무의존(shim)이라 앱·자동화 양쪽에서
임포트되고, `app.py` 가 이미 `fx` 로 임포트하고 있다.

### `run_watchlist_alerts.py` (2,162 → 2,133줄) — 재수출로 전환

`_HIST_TD_PER_CD` / `_HIST_WINDOW_DAYS` / `_HIST_MAX_DAYS` /
`_bars_for_calendar_days` / `_calendar_days_for_bars` / `_hist_days_for_holding`
을 `fx.*` 재수출로 교체. **모듈 레벨 이름은 유지**했다 — `diag_hist_window` 의
`[R][T][G][H]` 가 `m._HIST_WINDOW_DAYS` 처럼 속성으로 접근하므로 이름을 지우면
검증군이 죽는다. 동작·반환값·`[WARN]` 문자열 전부 동일하다.

### `app.py` (27,376 → 27,542줄)

**1부 — historical-price-eod 6곳 limit → from/to**

| 호출부 | 구 limit | 신 창(달력일) | ≈봉 | 비고 |
|---|---|---|---|---|
| VIX FMP 폴백 | 252 | 400 + `.tail(252)` | 252 | **정합성 수정**(아래) |
| WTI USO/OIL | 5 | 20 | 13 | |
| DXY `_calc` UUP | 400 | 600 | 412 | 구속 252(rolling) |
| `_hist_fetch_dxy` UUP | 500 | 800 | 549 | 구속 500(`tail`) |
| `cached_timing_price_history` | 260 | **460** | 316 | **cutoff 후처리 제거** |
| DRG `_prev_close` 폴백 | 2 | 20 | 13 | |

**2부 — `_fmp_price_history_robust` 체인**

- `limit`(봉) → `calendar_days`(달력일), **키워드 전용**
- 원시 `requests.get` → `fh.fmp_get_ex(retries=0)`
- `_fmp_robust_batch_history_report` 기본 190일(≈130봉, 구속 75봉)
- `_fmp_robust_batch_close` 기본 **460일**(≈316봉) — 구 220봉은 MA200(200) 대비
  마진 10%뿐이라 연휴 구간에서 `min_periods=150` 폴백으로 조용히 내려앉았다

**3부 — `scanner_core` 호출부 복구 (26곳 + 로컬 래퍼 4개)**

로컬 래퍼 4개(`_fmp_batch_to_close_df` · `_wl_prefetch_histories` ·
`_pf_prefetch_histories` · `_factcheck_download_closes`)도 `calendar_days`
키워드 전용으로 전환. 상수로 못 푸는 3곳은 동적 창:

- `_dividend_reinvest_price` → `fx.hist_days_for_target_date(target)`
- `_pf_prefetch_histories` → `fx.hist_days_for_holding(Date_Added)` (호출부가
  계좌별 중복 티커의 **가장 오래된** Date_Added 를 넘김)
- `21552` PF 루프 폴백 → 같은 규칙(이 경로만 얕아지는 것 방지)

**4부 — 주석 부채**

- `~21014` "정밀검사 1회당 FMP 2콜 절감" → **실제 0콜**로 정정.
  `fetch_senate_house_trading` 은 `@st.cache_data(ttl=3600)` 이고 같은 탭의 화면
  표가 어차피 먼저 호출한다. 이 변경의 실제 이득은 콜 절감이 아니라 **낡은
  데이터가 LLM 판단 입력으로 들어가지 않는 것**이다.
- ~~line 49 주석의 3523/3761/4002~~ → **이미 해결돼 있었다.** 현재 48–52행은
  grep 패턴을 쓰고 있고 저 숫자는 파일 전체에 없다. 인수인계 기록이 낡았다.
  (실제 shadowing 위치는 3599/3837/4078.)

### `diag_hist_window.py` (625 → 959줄) — `[A][W][X]` 추가
### `diag_hist_window.yml` — 문서 갱신 (66 → 97항목)
### `diag_fmp_ssot.py` — A1 래칫 기준선 `app.py: 63 → 62`

---

## 3. 설계 근거와 기각한 대안

**환산 상수를 app.py 에 복제하지 않은 이유.** 가장 빠른 길은 `0.6871` 을 app.py 에
한 벌 더 두는 것이었다. 기각한다 — 상수가 두 벌이 되면 한쪽만 갱신되고, 창이
조용히 짧아지는 실패는 **에러 로그를 남기지 않는다**. `[X1]` 이 저장소 전역에서
이 리터럴의 중복을 금지한다(주석은 허용 — 왜 그런지가 기록이다).

**`fmp_get_ex` 에 완전 위임하지 않은 이유.** `_fmp_price_history_robust` 의 바깥
재시도 루프만이 `deadline_ts`(배치 시간 예산 420초)를 안다. 완전 위임하면 예산이
사라져 스캐너가 예산을 넘겨 돈다. `retries=0` 으로 넘겨 **백오프는 한 곳에만**
남기고, 얻는 것은 레이트리밋 토큰·통계의 SSOT 화와 **402/4xx 즉시 중단**이다
(이전엔 404 를 5회 백오프로 재시도했다). 상태 어휘(`ok/no_data/exhausted/no_key`)는
**넓히지 않았다** — 넓히면 `_RS_SCAN_MISS_REASONS` 와 미수집 사유 표시까지 파급된다.

**cutoff 후처리를 남기지 않은 이유.** 창을 정의하는 곳이 둘(`from/to` + `cutoff`)이면
**둘 중 짧은 쪽이 조용히 이긴다.** 그 짧은 쪽이 251봉이었다. `[W3]` 이 부활을 막는다.

**VIX 를 페이로드 최적화가 아니라 정합성 수정으로 다룬 이유.** Fear&Greed 백분위는
`(_vix_s < vix_val).sum() / len(_vix_s)` — **분모가 시리즈 전체 길이**다. FRED 경로는
`tail(252)`, FMP 폴백은 `limit` 무시로 5년 피드였다. 즉 **어느 소스가 이겼는지에 따라
같은 VIX 값에 다른 점수**가 나왔고 화면에는 아무 표시도 없었다. `.tail(252)` 로
두 경로를 맞춘다. `calculate_style_scores` 의 `vols.mean()` 도 같은 부류라
`.tail(65)` 로 고정했다.

**간접 호출부 8곳을 "등가 이전"으로 처리한 이유.** `_fmp_batch_to_close_df` 의
간접 호출부는 하류를 개별 추적하지 않고 "종전 봉수 ≥ 등가 달력일"로 옮겼다.
항상 **확대 방향**이라 축소 위험이 없다. 개별 요구치 유도는 백로그.

**`limit=900` 2곳을 유예한 이유.** `app.py:2085`(실적 심층 캐시)와
`run_earnings_watch.py:242` 는 락스텝 쌍이다. 지금은 둘 다 `limit` 이 무시돼
**전체 피드를 받는다는 점에서 우연히 일치**한다. 한쪽만 from/to 로 바꾸면 실적 갭
표본 수가 갈려 앱 화면과 이메일의 예상 변동폭이 달라진다. `[A1]` 래칫이 이 예외를
1곳으로 못 박아 확산을 막는다.

---

## 4. 검증

- `py_compile` + `check_py311.py` — 5개 파일 전부 ✅
- `diag_hist_window.py` — **97/97 통과**
- **역검증:** `[A4]` 결합 검사를 패치 **이전** app.py 에 돌려 18/18 검출 확인.
  패치 후 36/36 결합 성공, 실패 0.
- **뮤테이션 25종 전건 탐지 + 전건 복원 확인**(`__pycache__` 매 회 삭제).
  2차 추가 11종: 창 회귀·cutoff 부활·tail 제거·상수 회귀·사본 부활 등.

> ⚠️ **`[W6]` 은 처음에 놓쳤다.** `any(tail(252))` 로 검사했는데 같은 함수의 FRED
> 경로에도 `tail(252)` 가 있어서 FMP 쪽을 지워도 참이었다. 판별력 있는 통계는
> '존재'가 아니라 **'개수'(2개)** 다. 뮤테이션이 없었으면 초록불로 남았을 검사다.
> — 이번 세션의 가장 값비싼 교훈.

---

## 5. 배포 순서 (락스텝 — dev 브랜치)

**반드시 이 순서로.** `fmp_extras` 가 먼저 올라가지 않으면 나머지가 ImportError.

1. `fmp_extras.py` (1,049)
2. `run_watchlist_alerts.py` (2,133)
3. `app.py` (27,542)
4. `diag_hist_window.py` (959)
5. `diag_hist_window.yml` (147)
6. `diag_fmp_ssot.py` (1,099)

GitHub 표시 줄 수 = 위 값 − 1.

---

## 6. 남은 한계 · 후속

1. **`limit=900` 락스텝 쌍** — `app.py:2085` + `run_earnings_watch.py:242`.
   같은 커밋에서 함께 전환할 것. 지금은 `[A1]` 래칫 기준선 1로 고정.
2. **`run_drg_predict.py`** 에도 `limit=2/10` historical-price-eod 호출이 남아 있다
   (184·263·381·403·1213행). 값은 같지만 5년 피드를 받는 낭비.
3. **간접 8곳 요구치 유도** — 등가 이전으로 처리했다. 개별 감사 미실시.
4. **A1 래칫 62곳** — `@st.cache_data` 대화형 경로. 한 번에 손대면 회귀 위험이
   커서 계속 조인다(§6-B5).
5. **`check_freshness.py` 정합성 검사** — 여전히 심볼 존재만 본다. 시그니처 결합은
   `diag_hist_window [A4]` 가 덮지만, `check_freshness` 자체에 얹는 편이 세션
   시작에서 더 빨리 잡힌다. 별도 검토.
