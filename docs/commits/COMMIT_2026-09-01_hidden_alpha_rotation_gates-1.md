# fix(hidden-alpha): 로테이션 게이트 신설 — 레버리지 누출·기초자산 중복·유동성 미달 차단

2026-08-29 주간 이메일이 Top 5 로 다음을 내보냈다.

```
1 SPAX  +6.33% / +44.83%  99.3  NEW   ← T-REX 2X Long SPCX Daily Target ETF (2배 레버리지)
2 THYP  +6.14% / +44.33%  99.0        ← 21Shares Hyperliquid  (HYPE 현물)
3 HYPG  +6.44% / +44.21%  99.0        ← Grayscale Hyperliquid (HYPE 현물)
4 AIQU  +8.63% / +43.19%  98.9  NEW
5 BHYP  +6.46% / +44.06%  98.9        ← Bitwise Hyperliquid   (HYPE 현물)
7 KMCA  +7.75% / +25.28%  97.1  NEW   ← 거래대금 극소
```

서로 다른 세 가지 고장이 동시에 드러났다. 하나씩 다르게 고쳤다.

---

## 1. 무엇이 왜 고장났나

### ① 레버리지가 뚫렸다 — 필터 결함이 아니라 **입력 결함**

`fmp_extras.is_rotation_excluded()` 의 판별 경로는 둘뿐이다.
① `LEVERAGED_ETF_MAP` 티커 매핑 ② `name` 정규식.
SPAX 는 맵에 없고, **`name` 이 빈 문자열로 넘어와 정규식이 볼 문자열이 없었다.**

이름이 빈 경로를 역추적한 결과가 이번 조사의 핵심이다.

| | 이름 취득 | 결과 |
|---|---|---|
| `app.py` | `_fmp_etf_symbol_name_map()` → `{심볼: 이름}` | SPAX 를 **걸렀다** |
| `run_hidden_alpha.py` | `_fmp_etf_symbol_set()` → 심볼 집합만 (**이름 폐기**) | SPAX 가 **통과했다** |

둘은 `/stable/etf-list` 라는 **같은 엔드포인트**를 부르고 **같은 함수**로 판정한다.
다른 것은 입력뿐이었다. 그래서 화면은 정상인데 이메일만 뚫렸고,
`run_hidden_alpha.py` 의 주석 "app.py 의 [2단계] 유니버스 랭킹도 동일 필터 —
이메일과 앱 화면이 항상 일치" 는 **거짓이었다.**

발견 단계에도 같은 손실이 있었다. `_discover_new_etfs()` 는 AUM 때문에
`_fmp_profile()` 을 이미 호출하면서 **`companyName` 을 손에 쥐고도 버리고**
`sector`/`industry` 만 `category` 로 저장했다. 정식명이 이미 응답에 있었다.

부수 결함으로 정규식 자체도 좁았다. `\b3X\b|\b2X\b|\b1\.5X\b` 세 개만 알아서
4X·5X·1.75X·1.25X 를 못 잡았다. **열거형 필터는 모르는 것에 대해 항상 통과를
반환한다** — 발행사가 새 배수를 내놓을 때마다 조용히 뚫린다.

### ② 같은 기초자산 래퍼 3개가 슬롯 3개를 먹었다

THYP·BHYP·HYPG 는 전부 HYPE 현물 ETF다. 레버리지가 아니라 **포장지만 다른
같은 베팅**이다. 1개월 수익률 44.33 / 44.06 / 44.21 이 사실상 동일한 게 물증이다.

점수가 수익률 백분위 가중합뿐이므로 **래퍼들은 구조적으로 나란히 상위권에
붙는다.** $50 × 5 = $250 을 분산한 것처럼 보이지만 실제로는 $150 이 한 토큰이었다.
손실 방지 우선 철학과 정면 충돌한다.

### ③ 유동성 게이트가 아예 없었다

랭킹 단계에 거래량·AUM 조건이 0개였다. AUM 게이트는 발견 시점 1회뿐이고
그마저 이렇게 쓰여 있었다.

```python
aum = float(p.get("totalAssets") or p.get("mktCap") or 0) / 1_000_000
if aum and aum < _DISCOVERY_MIN_AUM_M:   # ← aum 이 0/None 이면 통과
    continue
```

신규 상장 직후엔 `totalAssets` 가 거의 항상 비므로, **이 게이트는 막으려던
대상에게만 정확히 무력했다.** KMCA 가 들어온 경로로 추정된다.

---

## 2. 파일별 변경

### `rotation_core.py` — 신규 (390줄, 리포 루트)

게이트 판정 SSOT. **streamlit·gspread·requests 를 import 하지 않는다** —
네트워크 조회는 호출부가 하고 여기엔 판정만 들어온다(테스트 가능성 불변식).

사전 확정 임계값 (2026-09-01, 결과 보기 전에 잠갔다 · 재협상 금지):

| 상수 | 값 | 의미 |
|---|---|---|
| `MIN_DOLLAR_VOLUME` | $3,000,000 | 20일 평균 달러거래대금 하한 |
| `MIN_AUM_M` | $50M | 순자산 하한 |
| `CORR_THRESHOLD` | 0.90 | 이 이상이면 같은 베팅 |
| `CORR_LOOKBACK` | 60 | 상관 계산 구간(거래일) |
| `CORR_MIN_OVERLAP` | 40 | 공통 관측일 하한 |
| `CRYPTO_SLOT_CAP` | 2 | Top 5 중 크립토 최대 슬롯 |
| `REQUIRED_BARS` | 61 | 위 요구의 최댓값(파생 상수) |

주요 함수: `avg_dollar_volume` · `passes_liquidity` · `passes_aum` ·
`is_crypto` · `dedup_by_correlation` · `select_top_slots` · `apply_rotation_gates`

**"모르면" 규칙이 두 갈래인 이유 (의도적 비대칭):**

- 유동성·AUM 은 **위험 크기** 판정이다 → 모르면 **제외** ("모르면 안 산다")
- 상관·크립토는 **분류** 판정이다 → 모르면 **통과**

후자를 제외로 밀면 이력이 짧은 신규 상장 ETF 가 전부 걸려
Hidden Alpha 의 존재 이유가 사라진다. 이력이 짧다는 것은 중복이라는
증거가 아니라 증거가 없다는 뜻이다.

`avg_dollar_volume` 은 종가·거래량을 **날짜 인덱스 교집합**으로 맞춘다.
길이만 맞춰 자르면 서로 다른 날의 종가와 거래량을 곱하는 조용한 오류가 난다
(돌연변이 M10 으로 검증).

### `fmp_extras.py` — 1106 → 1139줄

- 배수 판별을 **열거 → 일반형 캡처**로 교체
  `_LEV_MAG_NX = r"(?<![A-Z0-9.])(\d+(?:\.\d+)?)\s?X\b"` + 범위검사 `1.0 < v ≤ 20.0`
  → 2X·4X·5X·1.75X 를 배수 종류와 무관하게 잡는다. 20 초과는 이름에 섞인
  다른 숫자(연도 등)로 보고 배수로 읽지 않는다.
- 단어형 분리: `_LEV_MAG_WORD_3X`(ULTRAPRO/TRIPLE), `_LEV_MAG_WORD_2X`(ULTRASHORT/ULTRA/DOUBLE)
  → UltraShort 가 이제 `-2.0` 을 반환한다(기존 `-1.0` 으로 뭉개짐)
- `is_leverage_suspect()` 신설 — `DAILY TARGET|LEVERAGED`
  **배수 판정이 아니라 의심 플래그로만 쓴다.** 배수를 모르면서 아는 척하지
  않기 위해 `get_leverage_multiplier` 가 아니라 `is_rotation_excluded` 에서만 본다.

### `run_hidden_alpha.py` — 784 → 967줄

| 변경 | 내용 |
|---|---|
| A-1 | `_fmp_etf_symbol_set()` → `_fmp_etf_symbol_name_map()` — 이름 폐기 중단 (**콜 추가 0**) |
| A-1 | 발견 단계 이름 우선순위: `profile.companyName` > `etf-list` > `ipos-calendar` |
| A-2 | `verify_and_gate()` 신설 — 상위 15개만 profile 재조회 후 재판정 (**콜 15개**) |
| C | `_fmp_price_history_close` → `_fmp_price_history_ohlcv` (Close, Volume 쌍) |
| C | `_fmp_batch_close_df` → `_fmp_batch_ohlcv_df` — 거래량이 이미 응답에 있다 (**콜 추가 0**) |
| C | AUM 게이트 반전: `if aum and aum < MIN` → `rc.passes_aum(aum)` (모르면 제외) |
| B | 조회 봉 수 22 → `rc.REQUIRED_BARS` (61). 숫자를 직접 쓰지 않는다 |
| — | `_HOLD_SLOTS = rc.HOLD_SLOTS` — 상수 복사 제거 |
| — | 스냅샷에 `selected` 명시 저장 · 이메일 표에 🚫 + 제외 사유 표시 |

**스냅샷 형식 변경이 이번 작업에서 가장 위험했던 지점이다.**
기존 `compute_actions` 는 `prev_map[tk] <= 5` 로 "지난주 보유"를 역산했다.
게이트가 순위와 보유를 분리한 이상, 이대로 두면 **크립토 캡에 걸려 사지도
않은 종목에 다음 주 매도 지시가 나간다.** `selected` 를 명시 저장하고
`compute_actions(selected=, prev_selected=)` 로 실제 선정 결과를 비교한다.
옛 스냅샷(`selected` 없음)은 순위≤5 로 자동 복원한다.

### `app.py` — 27830 → 27840줄

`load_hidden_alpha_ranks()` 반환을 `(ranks, date)` → `(ranks, date, selected)` 로 확장.
게이트가 순위와 보유를 분리했으므로 앱이 "순위 ≤ 5 = 보유"로 역산하면 화면과
이메일이 어긋난다. 자동화가 저장한 선정 결과를 그대로 읽는다.
호출부는 1곳(`build_portfolio_sell_radar_df`), `_pf_rank_meta["selected"]` 로 전달.
옛 스냅샷 하위호환 포함.

A-3 정규식 강화는 `load_etf_universe_tickers_merged()` 가 이미
`fx.filter_rotation_universe` 를 쓰고 있어 **자동으로 적용**된다.

### `check_freshness.py` — 186 → 192줄

- `rotation_core.py` · `run_hidden_alpha.py` 를 지문 표에 등록
  (`run_hidden_alpha.py` 는 그동안 **미등록**이라 대조 기준이 없었다)
- `fmp_extras.py` 마커에 `is_leverage_suspect`·`_LEV_MAG_NX` 추가
- `CROSS_TARGETS` 에 `rotation_core` 추가

### `diag_rotation_policy.py` / `.yml` — 신규 (302줄, `automation/` · `.github/workflows/`)

> **[2026-09-01 수정]** 최초 `.yml` 의 의존성이 `numpy pandas` 뿐이라 CI 가
> `ModuleNotFoundError: No module named 'pytz'` 로 실패했다. `fmp_extras` 가
> `pytz`·`requests` 를 요구한다. `diag_fmp_ssot.yml` / `diag_confirm_sweep.yml`
> 과 동일한 의존 집합(`numpy pandas pytz requests gspread google-auth`)으로 통일했다.
> 원인은 검증 환경 오염 — 컨테이너에 `pytz` 가 임시 설치돼 있어 로컬 통과가
> CI 통과를 보장하지 못했다. 재검증은 **깨끗한 venv** 에서 수행했다.

이 정책을 지키는 진단이 **하나도 없었다.** SPAX 가 뚫린 걸 아무도 못 잡은
이유이기도 하다. A~H 8개 그룹 80항목.

---

## 3. 검증 결과

```
검증 환경             깨끗한 venv (numpy pandas pytz requests gspread google-auth)
                      ※ streamlit 미설치 상태로 통과 — 선택 의존임을 확인
py_compile              전 파일 통과
check_py311             5/5 호환
pyflakes 델타           0  (app.py 34건 baseline 동일, fmp_extras/run_hidden_alpha 각 1건 동일)
diag_rotation_policy    80 / 80 통과
돌연변이 시험           16 / 16 검출, 생존 0
통합 스모크             사고 재현 → 정상 차단
```

### 돌연변이 시험 — 1차에 3개가 살아남았다

`py_compile` 사전검증으로 무효 돌연변이를 걸러낸 뒤 16개를 주입했다.
1차 결과 3개 생존 = **진단이 옳은 이유로 통과한 게 아니었다.**

| 생존 돌연변이 | 왜 안 잡혔나 | 보강 |
|---|---|---|
| M3 AUM `a <= 0` 가드 제거 | 최종 비교(`0 >= 50`)가 결과를 대신 막아 차이가 안 남 | C-7~9: 임계 0 케이스 |
| M15 배수 뒤 경계조건 제거 | 오탐 케이스("2026X")를 범위검사가 대신 막음 | A-19: `2XU` (실존 의류 브랜드) |
| M16 `ULTRA` 계열 제거 | UltraShort→인버스 경로, UltraPro→3배 경로가 구멍을 가림 | A-20~23: **배수 크기**까지 검증 |

보강 후 16/16 검출. 이 판별 케이스들을 지우면 위 셋이 다시 조용히 통과한다
— `.yml` 주석에 명시해 두었다.

### 통합 스모크 (2026-08-29 사고 재현)

```
랭킹(점수순): SPAX THYP BHYP HYPG AIQU WCLD KMCA SPY
[GATE] 레버리지/인버스 재검출: SPAX — T-REX 2X Long SPCX Daily Target ETF
[GATE] 제외 SPAX: 레버리지/인버스
[GATE] 제외 KMCA: 거래대금 미달 — 20일 평균 $0.12M
[GATE] 제외 BHYP: 기초자산 중복 — THYP 와 ρ=1.00
[GATE] 제외 HYPG: 기초자산 중복 — THYP 와 ρ=1.00
[GATE] 최종 슬롯: THYP AIQU WCLD SPY        ← 슬롯을 비우지 않고 다음 순위가 승계
스냅샷 왕복 OK · 옛 형식 복원 OK
```

---

## 4. API 콜 영향

| 항목 | 변화 |
|---|---|
| `etf-list` 이름 유지 | **+0** (같은 응답에서 이름을 버리지 않을 뿐) |
| 거래량 취득 | **+0** (`historical-price-eod` 응답에 이미 있다) |
| 발견 단계 `companyName` | **+0** (AUM 때문에 이미 호출) |
| A-2 상위 15개 재검증 | **+15** |
| 봉 수 22 → 61 | **+0** (FMP `limit` 은 비작동, `from`/`to` 창만 넓어짐) |

**순증 15콜.**

---

## 5. 락스텝 배포 순서

의존 방향의 **역순**으로 올린다. 새 모듈을 먼저 올려야 소비자가 착지할 때
`ImportError` 가 나지 않는다.

| # | 파일 | 위치 | 예상 줄수(GitHub) |
|---|---|---|---|
| 1 | `rotation_core.py` | 루트 | 389 |
| 2 | `fmp_extras.py` | 루트 | 1138 |
| 3 | `run_hidden_alpha.py` | `automation/` | 966 |
| 4 | `app.py` | 루트 | 27839 |
| 5 | `diag_rotation_policy.py` | `automation/` | 301 |
| 6 | `check_freshness.py` | 루트 | 191 |
| 7 | `diag_rotation_policy.yml` | `.github/workflows/` | — |

⚠️ **1→2→3 을 쪼개지 말 것.** `run_hidden_alpha.py` 만 먼저 올리면
`import rotation_core` 가 실패해 주간 실행이 통째로 죽는다.
`fmp_extras.py` 만 먼저 올려도 `run_hidden_alpha` 는 옛 반환형을 기대해 깨진다.

4번(app.py) 업로드 후 **Streamlit 리부트 필요.**

---

## 6. 남은 한계 (의도적으로 안 한 것 포함)

- **A-4 시트 Name 빈 행 백필 미포함.** 런타임 이름 배관이 두 겹(etf-list →
  profile.companyName)이라 시트가 비어도 막힌다. 필요하면 별도 일회성 스크립트.
- **`$3M` 임계값은 실제 분포로 검증되지 않았다.** 사전 확정 원칙에 따라
  결과를 보기 전에 잠갔다. 분포 프로브는 사후 기록용으로만 돌리고,
  **첫 결과를 보고 조정하지 않는다.**
- **크립토 분류는 이름/섹터 기반이다.** 이름이 없고 섹터도 비면 비크립토로
  통과한다. A-2 재검증이 profile 이름을 확보하므로 상위 15개에 대해서는
  대부분 채워지지만, 100% 는 아니다.
- **`rotation_core` 를 app.py 는 import 하지 않는다.** app.py 는 계산을 반복하지
  않고 스냅샷의 `selected` 를 읽는다. 계산 지점이 하나라 드리프트는 없지만,
  "app.py 와 공용 모듈"이라는 표현은 정확하지 않다 — 명시해 둔다.
- **상관 60일은 최근 국면만 본다.** 국면이 바뀌어 상관이 무너지면 같은 자산도
  분리될 수 있다. 크립토 캡이 그 경우의 2차 방어선이다.
- **반감기 없는 영구 유니버스 문제는 그대로다.** 편입 후 AUM 이 쪼그라든
  종목은 여전히 시트에 남는다. 다만 이제 랭킹 상위 15에 들면 A-2 에서
  AUM 재검사를 받는다.
