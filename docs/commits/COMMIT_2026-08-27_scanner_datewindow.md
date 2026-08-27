# feat(scanner): 가격 이력 `limit` → `from`/`to` 날짜창 전환 + 봉수 부족 보고(`full_metrics`)

> 2026-08-27 · 인수인계 §6-B2 · 락스텝 `regime_core.py` + `scanner_core.py` + `diag_regime_window.py`

---

## 1. 인수인계 §3-2 가 두 군데 틀렸다

착수 전 §3-2 를 실제 코드로 검증했고, **두 가지가 사실과 달랐다.**

### 1-1. 위험 지점이 다르다

§3-2: *"`limit=130` 을 실제로 강제하면 `classify_regime` 이 붕괴"*

**`limit=130` 인 두 곳은 `classify_regime` 을 타지 않는다.**

| 호출부 | limit | 소비처 | 실측 최대 룩백 |
|---|---|---|---|
| `route_candidates_by_regime:1310` | **252** | `classify_regime` | **252** |
| `score_opportunity_universe:1365` | **252** | `classify_regime` | **252** |
| `score_emerging_opportunity_universe:1546` | 130 | `close_df`/`volume_df` | **50** (ma50) |
| `score_expansion_opportunity_universe:1773` | 130 | `close_df`/`volume_df` | **30** |

`route_candidates_by_regime` 은 `limit` 기본값이 **252** 이고, 호출부 3곳
(`scanner_core:2028` · `app.py:18333` · `app.py:18510`)이 **전부 인자를 넘기지 않는다.**
즉 레짐 경로는 252, 짧은 경로는 130 — 서로 다른 함수다.

130 두 곳은 필요치의 2.6배·4.3배를 받고 있었다. 여유가 넘친다.

### 1-2. 붕괴 양상도 다르고, 더 나쁘다

봉수를 계단식으로 줄여 `classify_regime` 을 실제로 돌렸다:

| 봉수 | ma200 | ma200_slope | regime |
|---|---|---|---|
| **130** | NaN | NaN | **weak** |
| 150 | 59.09 | NaN | sideways |
| 170 | 58.32 | 0.26 | sideways ← 값은 나오지만 **틀림** |
| 200 | 57.07 | 0.47 | sideways ← 여전히 틀림 |
| **220** | 57.07 | **1.71** | **strong** ← 첫 정답 |
| 252 · 1254 | 57.07 | 1.71 | strong |

§3-2 는 *"sideways/unknown 으로 붕괴"* 라고 했으나 실제는 **`weak`** 다.
`weak` 는 `excluded` 로 가 **매도 레이더에 실린다.** 중립이 아니라 적극적으로
부정적인 오답이다.

### 1-3. 그래서 §3-2 의 권장값이 벼랑 끝이었다

§3-2 권장: `from = today − 380일`(≈252봉).

두 개의 요구치가 있고 **큰 쪽이 구속한다**:

- `ma200_slope` 수렴 = **220봉**
  (`ma200_series` 는 `min_periods=150` 이라 170봉부터 값이 나오지만, `t-20` 시점의
   MA 까지 진짜 200봉 평균이어야 기울기가 맞는다 → `200 + SLOPE_LOOKBACK(20)`)
- **52주 창 = 252봉** ← 이쪽이 크다

380일 ≈ **262봉**, 여유 **10봉**. `close.tail(252)` 는 252봉이 없으면 **있는 만큼만
반환**하므로, 데이터 공백 한 번이면 52주 창이 조용히 짧아진다 — 에러도 로그도 없다.
**이번 세션에 고친 그 결함이 입력 부족으로 재발하는 경로다.**

---

## 2. 파일별 변경

### 2-1. `regime_core.py` — 2,426 → **2,466** (+40)

**보고만 한다. 동작은 0 변경.**

```python
MIN_BARS_FULL = W52_BARS      # 252

# classify_regime 출력에 추가
"bars": int(len(close)),
"full_metrics": bool(len(close) >= MIN_BARS_FULL),
```

조기 반환 경로에도 `bars: 0` · `full_metrics: False` 를 넣었다 — 소비자가
`.get("bars")` 로 읽을 때 `None` 이 나오면 0봉인지 필드가 없는 버전인지 구분이 안 된다.

**왜 하드 게이트가 아닌가 (설계 선택 2-A → 2-A′ 로 수정)**

처음에는 *"252 미만이면 `regime="unknown"`"* 을 추천했다가 **코드를 보고 철회했다.**

- `enough_data` 임계값을 50 → 252 로 올리면 **소비처 19곳**이 영향을 받는다
  (`app.py` 12곳 · `run_watchlist_alerts` 2곳 · `scanner_core` 2곳). 그 대부분은
  개별 종목 정밀검사·워치리스트 알림이고 스캐너와 목적이 다르다. 신규 상장주를
  워치리스트에 넣으면 아무 신호도 안 나오게 된다.
- `regime` 을 `unknown` 으로 바꾸면 `regime_core:629`(진입 시점 재구성 → 알림 억제
  판정)가 달라진다. 그 경로는 `len(sliced) >= 50` 만으로 통과시키는 **의도적인 짧은
  시리즈 호출부**다. **알림 동작 변경이 페이로드 절감 작업에 딸려 오면 안 된다.**

→ `regime_core` 는 보고, 판단은 호출부.

### 2-2. `scanner_core.py` — 2,119 → **2,172** (+53)

**(a) `limit` → `from`/`to` 전환**

```python
_REGIME_LOOKBACK_DAYS = 460   # ≈317거래일 · 요구 252봉 · 여유 65봉
_SHORT_LOOKBACK_DAYS  = 190   # ≈131거래일 · 요구  50봉 · 여유 81봉
```

값을 옮겨 적지 않고 **하류 요구치에서 유도**했다. §3-3 의 교훈: `limit` 숫자들은
한 번도 강제된 적이 없어 검증된 적도 없다. `from` 으로 바꾸는 순간 **처음으로
실제 상한**이 되므로, 그 숫자는 소비자가 무엇을 필요로 하는지에서 나와야 한다.

`_fmp_batch_price_history` 는 **기본값을 없앴다**(`*, lookback_days: int`).
호출부가 요구치를 반드시 밝히도록 강제한다 — "누가 언젠가 적어둔 값"이 다시
생기지 않게.

`_fmp_price_history` 의 기본값은 **큰 쪽(460)** 으로 뒀다. 직접 호출하는 사람이
생기면 데이터가 모자라는 쪽이 아니라 남는 쪽으로 틀리는 게 안전하다.

**(b) `full_metrics` 소비 2곳**

- `route_candidates_by_regime:1373` — **제외**하고 사유를 명시:
  `"이력 부족 (137봉 / 252봉 필요 — 신규 상장 가능)"`
- `score_opportunity_universe:1455` — `Regime Available` 열에 반영(**표시만**).
  이 함수의 순위는 레짐 외 요소로도 구성되고 해당 열이 이미 존재한다.

**이건 전환 위험 대비가 아니라 지금 있는 버그다.** `limit` 이 무시돼 1,254봉을
받아도 **상장 1년 미만 종목은 애초에 252봉이 없다.** 그런 종목은 오늘도
`weak`(약추세 Stage4) 오답을 받아 매도 레이더에 실리고 있다.

### 2-3. `diag_regime_window.py` — 342 → **496** (+154)

| 그룹 | 내용 |
|---|---|
| **B** (신설) | 봉수 보고 8건 — 상수 핀 · 251/252 경계 · 빈 입력 · **무변경 증명**(B7·B8) |
| **C** (신설) | **모듈 경계** 6건 — scanner_core 가 실제로 소비하는가 |
| S14~S18 (신설) | 새 탐지기 2개의 자체 판별력 |

**C 그룹이 이 커밋의 핵심 방어다.** §3-2 의 실패가 정확히 여기였다 —
`diag_fmp_window` [E] 는 파일 단위라 `scanner_core → regime_core` 소비를 구조적으로
못 봤고, 그래서 위험을 엉뚱한 곳에 지목했다. 인수인계 §5-9: *"한계를 적는 것과
대비하는 것은 다르다."*

- **C3** 은 `full_metrics` 를 읽는지가 아니라 **`continue` 로 제외까지 하는지**를 AST 로 본다.
  표시만 하면 `weak` 오답이 그대로 나가기 때문
- **C5** 는 룩백을 거래일로 환산해 `MIN_BARS_FULL + 30봉` 여유를 요구한다.
  **§3-2 원안(380일)은 여기서 걸린다**(뮤테이션 M6 로 확인)

`MIN_BARS_FULL` 핀은 **값이 아니라 연결**을 본다(`allow_name=True`). 둘 다 252 라고
따로 적어두면 한쪽만 바뀌어도 통과하기 때문 — §5-5 자기참조 핀 문제.

---

## 3. 검증

### 3-1. 구/신 동작 전수 대조 — **640케이스 불일치 0건**

40개 난수 시리즈 × 16개 봉수(10·49·50·60·100·150·170·200·219·220·251·252·253·300·504·1254)
에서 `regime` · `stage` · `score` · `topping` · `enough_data` · `color` · `rsi_band` ·
`components` **전 키**를 대조했다. **완전 동일.**

신규 필드 경계:

```
n=   0  bars=   0  full_metrics=False  enough_data=False  regime=unknown
n=  49  bars=  49  full_metrics=False  enough_data=False  regime=unknown
n=  50  bars=  50  full_metrics=False  enough_data=True   regime=weak
n= 251  bars= 251  full_metrics=False  enough_data=True   regime=sideways
n= 252  bars= 252  full_metrics=True   enough_data=True   regime=sideways
```

### 3-2. 역검증 — 가드가 결함을 실제로 잡는가

**수정 전 코드에 새 가드를 걸었다: 45건 중 10건 실패.**
`C6` 이 `&limit=` 잔재를, `C3` 이 제외 게이트 부재를, `C4` 가 상수 부재를 잡았다.

### 3-3. 뮤테이션 **9/9 검출** · 복원 후 재실행 초록

| # | 변형 | 잡은 검사 |
|---|---|---|
| M1 | `MIN_BARS_FULL = W52_BARS` → `252` (자기참조 끊기) | B1 |
| M2 | 요구치 252 → 200 | B1·B2·B3 |
| M3 | `full_metrics` 항상 True | B3 |
| M4 | `bars` 상수화 | B5 |
| M5 | 조기 반환 필드 누락 | B6 |
| M6 | **`_REGIME_LOOKBACK_DAYS` 460 → 380 (§3-2 원안)** | **C5** |
| M7 | 460 → 300 | C5 |
| M8 | 제외(`continue`) → 표시만(`pass`) | C3 |
| M9 | `from/to` → `limit=252` 회귀 | C6 |

### 3-4. 스위트

```
✅ diag_regime_window   전 항목 통과 (46/46)      ← 이전 27/27
✅ diag_fmp_ssot        전부 통과 — 43건          ← A4 유지
✅ py_compile · check_py311  3개 파일 Python 3.11 호환
```

`diag_sell_verdict` 는 신규 필드를 참조하지 않아 **코드 변경 없음**. 배포 후 재실행만.

### 3-5. 페이로드

| 경로 | 이전 | 이후 | 절감 |
|---|---|---|---|
| 레짐 (route / opportunity) | 1,254봉 · 280KB | 317봉 · 71KB | **4.0배** |
| emerging / expansion | 1,254봉 · 280KB | 131봉 · 29KB | **9.6배** |

(봉당 229바이트 · `diag_fmp_window` 실측)

---

## 4. 만드는 과정에서 잡은 자기 결함 2건

**둘 다 수정본에서 거짓 빨간불로 드러났다.** 기록해 둔다 — 탐지기를 먼저 검증하지
않으면 이런 것이 그대로 배포된다.

1. **C6 을 파일 전역 `"&limit=" not in src` 로 짰다.** 같은 파일의
   `income-statement` · `analyst-estimates` · `balance-sheet` 는 `limit` 이 정상인데
   그걸 잔재로 셌다. → `_fmp_price_history` **함수 본문**으로 범위를 좁혔다.
2. **범위를 좁힌 뒤에도 실패했다** — `limit=` 이 내가 쓴 **독스트링** 안에 있었다
   (전환 이력 설명). `ast.get_source_segment` 는 주석·독스트링까지 준다.
   → `ast.unparse(본문 − 독스트링)` 으로 **코드만** 본다. S18 이 이 케이스를 고정한다.

또한 `diag_regime_window` 에 `_hist` 를 새로 추가했다가 **기존 정의를 죽은 코드로
만들 뻔했다**(같은 이름이 이미 있었고, 내 것이 앞에 붙어 원본의 `idx = pd.date_range`
이하가 unreachable 이 됐다). 제거하고 원본을 살렸다.

---

## 5. 배포 후 체크리스트

### [1] 파일 배치 · 락스텝 순서

| 순서 | 파일 | 저장소 경로 | 기대 GitHub 줄 수 |
|---|---|---|---|
| **1** | `regime_core.py` | **루트** | **2,466** |
| **2** | `scanner_core.py` | **루트** | **2,172** |
| **3** | `diag_regime_window.py` | `automation/` | **496** |

**순서를 지킬 것.** `scanner_core` 를 먼저 올리면 `rc.MIN_BARS_FULL` 이 아직 없어
`route_candidates_by_regime` 이 `AttributeError` 로 죽는다 — **스캐너 전체가 멈춘다.**
`regime_core` 를 먼저 올리는 것은 무해하다(필드가 추가될 뿐 아무도 안 읽는다).

**Streamlit 재부팅 필요.** `app.py` 는 안 바뀌었지만 `regime_core` · `scanner_core`
를 임포트하므로 재부팅해야 새 코드가 로드된다.

⚠️ `_fmp_batch_price_history` 의 TTL 캐시(30분) 키가 바뀐다. 재부팅 직후 첫 스캔은
**캐시 미스로 평소보다 느리다.** 정상이다.

### [2] 배포 전/직후 검증

Actions → **`diag_regime_window`** 수동 실행.

- **go**: `✅ 전 항목 통과 (46/46)`
- `27/27` → `diag_regime_window.py` 가 안 올라갔다
- `B*` 실패 → `regime_core.py` 가 안 올라갔다
- `C2`·`C3`·`C4`·`C6` 실패 → `scanner_core.py` 가 안 올라갔다 (순서 오류)
- `C5` 실패 → 룩백이 여유 30봉 미만. **여유를 줄였다면 되돌릴 것**

이어서 **`diag_fmp_ssot`** 실행 → `✅ 전부 통과 — 43건`
(락스텝 짝. `integrated_sell_verdict` 계약이 그대로인지 확인)

### [3] 재부팅 직후 화면에서 볼 것

**AI 스톡 스캐너 → 3버킷 스캔** 을 한 번 돌린다.

- `excluded` 목록에 **`이력 부족 (N봉 / 252봉 필요 — 신규 상장 가능)`** 사유가
  새로 보일 수 있다. **이게 정상이다** — 이전에는 같은 종목이 `약추세(Stage4)` 로
  잘못 나가고 있었다
- 그런 종목이 **하나도 안 보이는 것도 정상**이다. 유니버스에 신규 상장주가 없으면
  안 나온다. 없다고 해서 기능이 안 도는 게 아니다
- `leaders` / `setups` 구성은 **거의 그대로여야 한다.** 크게 달라졌다면 보고할 것 —
  `classify_regime` 은 640케이스 무변경이 확인됐으므로 예상 밖이다

**정밀검사·워치리스트 알림은 아무것도 달라지지 않아야 한다.** `enough_data` 를
건드리지 않았기 때문. 달라졌다면 그것도 예상 밖이다.

### [4] 다음 스캔 실행에서 확인

- **속도**: 페이로드가 4~9.6배 줄었으므로 스캔이 눈에 띄게 빨라져야 한다
- `Opportunity_Universe` 시트 **`Regime Available`** 열 — `FALSE` 가 늘 수 있다
  (252봉 미만 종목이 이제 여기서 걸린다). 사유는 시트에 안 남으므로,
  이상해 보이면 스캐너 화면의 `excluded` 사유와 대조할 것

### [5] ⚠️ 되돌릴 신호

다음이 보이면 **롤백하고 보고**할 것:

- `excluded` 에 `이력 부족` 이 **대량으로**(예: 유니버스의 20% 이상) 뜬다
  → FMP 가 `from` 창을 예상과 다르게 해석하고 있을 수 있다
- 스캔이 오히려 느려졌다 → `to=` 파라미터가 문제일 수 있다
- `AttributeError: MIN_BARS_FULL` → 업로드 순서 오류

### [6] 롤백

세 파일을 이전 버전(`regime_core` 2,426 · `scanner_core` 2,119 ·
`diag_regime_window` 342)으로 되돌리고 **Streamlit 재부팅**.

**데이터 손실 없음** — 시트 쓰기 경로를 건드리지 않았다. 이미 기록된
`Opportunity_Universe` 행의 `Regime Available` 값은 그대로 남는다(다음 스캔에서 덮인다).

**FMP 콜 0회 · 시트 쓰기 0회** (진단 스크립트 기준).

---

## 6. 남은 한계

- **C 그룹은 `scanner_core` 만 본다.** `classify_regime` 을 부르는 다른 소비자
  (`app.py:20048` · `regime_core:629` · `diag_sell_verdict`)는 `full_metrics` 를
  읽지 않는다 — **의도된 것**이지만 자동 검사가 없다
- **`app.py` 는 전환하지 않았다.** `app.py` 의 원시 `requests.get` 58곳은
  인수하기 §6-B9 의 별도 항목이다. 스캐너 페이로드만 줄었다
- **C5 의 거래일 환산(× 0.690)은 근사다.** 휴장이 몰린 해에는 몇 봉 어긋난다.
  여유 65봉이 그걸 흡수하도록 잡았다
- **`_SHORT_LOOKBACK_DAYS` 경로에는 가드가 없다.** emerging/expansion 의 요구치
  (50·30봉)를 검사하는 자동 검사가 없어, 그쪽에 200일선 지표가 추가되면
  조용히 깨진다. 필요해지면 C 그룹에 추가할 것
- `route_candidates_by_regime` 의 `lookback_days` 인자는 아무도 안 넘긴다.
  넘기는 호출부가 생기면 C5 의 상수 검사를 우회한다
