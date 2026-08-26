# fix(fmp): v2.8 계약 파손 복구 + 위성 백테스트 SSOT 전환 + 저장소 전역 가드 신설

## 배경

2026-08-26 1차 세션에서 `run_signal_backtest.py` 를 v2.8 로 올리며 두 가지를 바꿨다.

1. `_fmp_price_history` 를 `fh.fmp_get_ex`(레이트리밋/429 SSOT)로 전환
2. 반환을 `DataFrame` → **`(DataFrame, kind)`** 로 변경

**(2) 의 소비자를 갱신하지 않았다.** 그 결과 두 파일이 깨진 채 배포됐다.

또한 인수인계 §6-B1 이 "동일 결함 3개 파일" 로 적어둔 목록이 두 군데 틀렸다.
이 커밋은 그 정정까지 포함한다.

---

## 변경 파일

| 파일 | 위치 | 줄 수 | 이전 |
|---|---|---|---|
| `diag_fmp_ssot.py` | `automation/` | **774** | 신규 |
| `diag_fmp_ssot.yml` | `.github/workflows/` | **79** | 신규 |
| `diag_satellite_backtest.py` | `automation/` | **1,326** | 1,195 |
| `diag_satellite_backtest.yml` | `.github/workflows/` | **61** | 34 |
| `diag_fmp_depth.py` | `automation/` | **149** | 121 |
| `diag_trade_history.py` | `automation/` | **287** | 279 |

---

## 1. P0 — v2.8 계약 파손 복구

### `diag_fmp_depth.py`

이 파일은 원시 `requests.get` 을 쓰지 않는다. `bt._fmp_price_history` 를 빌려
쓴다. 인수인계의 "동일 결함(fmp_http 미사용)" 진단은 **틀렸다.**

```python
# 40행 (probe_limits)
df = bt._fmp_price_history(ticker, limit=lim)
if df.empty:            # tuple 에 .empty 없음 → AttributeError → 크래시

# 56행 (probe_universe, ThreadPool)
counts[tk] = len(df) if df is not None else 0
#            ↑ len(tuple) == 2 → 예외 없이 전 종목이 "2봉"으로 집계
```

두 번째가 더 위험하다. 예외가 안 난다. `recommend()` 가 그 2봉으로 분위수를
내고 음수 `TEST_LOOKBACK` 을 권고한다. **이력 깊이 측정이 존재 이유인 스크립트가
조용히 틀린 답을 내고 있었다.**

수정: 두 곳 모두 `(df, kind)` 언팩. `kind` 를 사유별로 집계해 출력하고
`fh.fmp_stats_line()` 을 찍는다. 부분 롤백 상태면 스택 트레이스 대신
"v2.8 이전 버전이다" 로 명시 중단.

### `diag_trade_history.py`

인수인계 목록에 **없던** 파일. 같은 구계약을 쓴다.

```python
spy = bt._fmp_price_history(SPY_TICKER, limit=bt.HISTORY_LIMIT)
if spy is None or spy.empty:   # 크래시
```

`main()` 이 `attach_spy(t)` 를 감싸지 않아 STEP1·STEP2 를 다 출력한 뒤
STEP3 직전에 죽는다 — 거래 성적표를 한 줄도 못 본다.

수정: 언팩 + 실패 시 사유를 찍고 `excess_pct` 만 NaN 으로 진행.

---

## 2. P1 — `diag_satellite_backtest.py` v2.8 전환

### 레이트리밋은 이 파일의 주된 문제가 아니었다

실측: 후보 풀 56 + 벤치 2 = 58종목 × 2엔드포인트 = **116콜**. 분당 300 미만이라
단독 실행이면 캡에 안 걸린다. 인수인계가 시사한 "run_signal_backtest 와 같은
사고가 진행 중" 은 과장이다.

### 진짜 문제는 배당조정 무성 대체였다

```python
adj[tk] = raw[tk]          # 배당조정 실패 → 원 종가로 폴백
```

`dividend-adjusted` 페치가 실패하면 그 종목만 **배당 미반영 시리즈로 성과를
측정**한다. `[WARN]` 은 찍지만 게이트가 없어 그대로 `Satellite_Backtest` 에
쓴다. 지문 열도 없어 **어느 행이 온전한 기준이었는지 사후 판별 불가** —
B1 §6-D 의 "기존 행 복구 불가" 와 정확히 같은 구조다.

### 수정 내용

- `_fmp_eod`: `requests.get` → `fh.fmp_get_ex`. 반환 `(df, kind)`.
  기존 자체 재시도(1.5초·3초)는 분당 한도 앞에서 무력했다 — 같은 1분 안에
  다시 쏘기 때문
- `_FMP_TIMEOUT` 12 → **20**. 레이트리밋만 고치면 탈락 사유만 옮겨간다
- `_batch_fetch`: `(raw, adj, fallback, reasons, failed)` 반환.
  `reasons` 는 `{(엔드포인트, kind): 건수}`. `except Exception: df = pd.DataFrame()`
  로 예외를 삼키던 경로 제거
- **`MIN_FETCH_RATE`(0.98) + abort 게이트 2개** — 시트 기록 **전에** `return 1`
  1. 원종가 성공률 < 임계 → 중단 (Top5 모집단이 달라진다)
  2. 배당조정 **인프라성** 실패율 > (1 − 임계) → 중단
- `_INFRA_KINDS` 로 `empty`(원래 그 시리즈가 없음)와 인프라성 실패를 분리.
  섞어 세면 게이트가 영구 빨간불이 되거나 반대로 진짜 오염을 놓친다
- 우회 스위치(`SKIP_*`/`FORCE_*`) **없음**
- **`Universe_Hash` 열 신설**(U열) — 랭킹 모집단(`후보풀 ∩ 확보분`) SHA1 8자 +
  `u` 접두어. `USER_ENTERED` 가 16진수 8자를 수로 삼키는 경우(≈2.3%) 방지
- `Div_Basis` 에 오염 표식 — 폴백이 있었으면 `· 혼합(N종목 close 대체)` 를
  붙인다. **로그는 사라지지만 시트는 남는다**
- `main() -> int` + `sys.exit(main() or 0)` — 게이트가 종료 코드로 드러나게

### SSOT 에 대한 의도적 절충 (기록)

`_env_fetch_rate` · `universe_hash` 는 `run_signal_backtest.py` 와 **같은 구현을
복제**했다. 공유 모듈로 빼려면 `run_signal_backtest` 와 그 락스텝 짝인
`diag_universe_funnel`(68/68 통과 중)까지 함께 손대야 하고, 후자의 AST 검사
(`universe_hash` 안에 `sorted` 가 있는가)가 위임으로 바뀌면 깨진다.

**대신 복제가 어긋나지 않는지를 `diag_fmp_ssot.py` B25·B26 이 두 모듈을 직접
호출해 대조한다.** 중복 부채를 감시되는 불변식으로 바꾼 것이다.

---

## 3. 신규 — `diag_fmp_ssot.py` 저장소 전역 가드

### 왜 만드나

`fmp_extras.py` 70행 주석에 같은 사고가 **이미 한 번 기록돼 있었다**
("이전에는 이 경로만 레이트리밋을 건너뛰어…"). 그때 교훈으로 "같은 패턴을 전
저장소에서 grep 한다" 고 적었지만, **grep 을 사람이 하기로 했고 하지 않았다.**
v2.8 의 계약 변경도 같은 방식으로 놓쳤다.

→ grep 을 도구로 옮긴다.

### 검사 구조 (38항목)

| | 내용 |
|---|---|
| A1 | 원시 FMP `requests.get` 지점이 기준선보다 늘었는가 (**래칫**) |
| A2 | 튜플 반환 함수를 단일 이름으로 받고 첫 원소인 척 쓰는가 (허용목록 없음) |
| A3 | `ex.submit(mod.튜플함수,…)` 후 `.result()` 를 튜플로 언팩하는가 |
| B1~B4 | 위성 스로틀 배선 (AST) |
| B5~B11, P1 | 실패 분류 — 스텁 주입으로 실제 함수 호출 + 양성대조 |
| B12~B20 | 게이트 존재·연산자·이탈·**순서**·개수·임계·결과 열 |
| B21~B26 | 지문 불변식 + run_signal_backtest 와의 복제 대조 |
| B27~B28 | 우회 스위치 부재 (AST `os.environ` 접근으로 판정) |
| M1~M7 | 뮤테이션 역검증 |

### A1 이 래칫인 이유

저장소에 이미 **83곳**의 원시 FMP 호출이 있다. 전부 하드 실패로 잡으면 첫날부터
빨간불이고, 빨간불인 스위트는 아무도 안 본다. 기준선보다 늘면 실패, 줄면 통과하되
"기준선을 낮추라"고 경고한다.

⚠️ 기준선은 '괜찮다'가 아니라 **'알고 있고 아직 안 고쳤다'**는 뜻이다.

현재 기준선(파일별 곳수):

```
app.py 58 · run_drg_predict 11 · run_watchlist_alerts 4 ·
narrative_core 2 · run_narrative 2 · run_drg_verify 1 ·
run_earnings_watch 1 · industry_core 1 ·
diag_earnings_preview_backtest 1 · diag_industry_mapping 1 ·
diag_sell_verdict 1
```

`fmp_http.py` 는 SSOT 구현체이므로 면제.

### 설계 중 걸러낸 오탐 2건 (기록)

초안 A2 는 "튜플을 단일 이름으로 받으면 위반" 으로 잡았다가 오탐이 났다.

- `run_watchlist_alerts.py:1645,1828` — `_posv = rc.position_sell_verdict(...)`
  는 `(label, reason)` 을 **통째로 다음 함수에 넘기는 정당한 패턴**
- `diag_earnings_preview.py:156` — 다음 줄에서 `fut, pst = recs` 로 언팩

→ 규칙 정밀화: 단일 이름 바인딩 자체는 무죄. **그렇게 받아놓고 `X.<속성>` 으로
쓸 때만** 위반. 또 `len(mod.f())` 검사는 **삭제**했다 —
`diag_universe_funnel.py:355` 가 반환 길이를 일부러 확인하는 정당한 용법이라
구분할 방법이 없었다. *구분할 수 없으면 잡지 않는다. 오탐이 나오는 가드는 곧
무시당한다.*

---

## 검증 결과

| 항목 | 결과 |
|---|---|
| `diag_fmp_ssot.py` | **38/38 통과** (뮤테이션 M1~M7 전부 검출) |
| `diag_satellite_backtest.py --selftest` | 전 항목 통과 |
| `py_compile` · AST 파싱 | 4개 파일 통과 |
| `check_py311.py` | 4개 파일 Python 3.11 호환 |
| YAML 파싱 | 2개 워크플로 통과 |

### 역검증 (필수 — 스위트가 미패치 코드에서 실패하는가)

| 상태 | 종료 코드 | 관측 |
|---|---|---|
| 전부 패치본 | **0** | 38/38 |
| `diag_satellite_backtest` 만 원본 | **1** | B1~B4 실패 + "v2.8 이전 버전이다" 명시 중단 |
| `diag_fmp_depth` 만 원본 | **1** | A2 가 40행·56행 두 위반을 지목 |

**A2 의 판별력은 자연 실증됐다** — 패치 전 저장소에 돌렸을 때 이번에 고친
3개 지점(`diag_fmp_depth` 2곳 · `diag_trade_history` 1곳)만 정확히 잡고
나머지는 통과했다.

---

## 락스텝 배포 순서

`diag_fmp_ssot.py` 는 `diag_satellite_backtest.py` 의 v2.8 심볼을 요구하므로
**후자를 먼저** 올려야 한다.

1. `automation/diag_satellite_backtest.py` (1,326)
2. `automation/diag_fmp_depth.py` (149)
3. `automation/diag_trade_history.py` (287)
4. `automation/diag_fmp_ssot.py` (774)
5. `.github/workflows/diag_fmp_ssot.yml` (79)
6. `.github/workflows/diag_satellite_backtest.yml` (61)

Streamlit 리부트 **불필요** — `app.py` 및 앱이 import 하는 공유 모듈은 손대지
않았다.

---

## 남은 한계

- **기존 `Satellite_Backtest` 행은 지문이 없다.** 어느 행이 배당 폴백 상태에서
  나왔는지 사후 판별 불가. 데이터 손실은 아님
- **`diag_sell_verdict.py` 는 이번에 손대지 않았다**(P2). 읽기 전용이고 시트에
  쓰지 않아 오염 경로가 없다. A1 기준선에 1곳으로 등재
- **`run_watchlist_alerts.py` 4곳은 실운용 경로다.** 순차 호출이라 병렬 폭주는
  없고 결측을 `_nodata` 회계 + A-2b 원인분류가 잡아내지만(무성 탈락이 아니다),
  스로틀·429 재시도가 없어 유니버스가 커지면 위험하다. **다음 우선순위 후보**
- `app.py` 58곳은 대화형 + `@st.cache_data` 라 위험도가 다르다. 별도 검토 사안
- A1 래칫은 곳수만 센다. 같은 파일 안에서 한 곳을 고치고 다른 곳을 새로
  추가하면 총량이 같아 통과한다
