fix(diag): run_hidden_alpha 진단 배선 복구 — 죽은 관문 4개 되살리고 [H] 군 신설

## 왜

9-01 Hidden Alpha 작업에서 가격 조회를 Close 단일 → `(Close, Volume)` 쌍으로
바꾸며 함수를 리네임했는데 `diag_hist_window_consumers.py` 가 따라가지 않았다.
진단은 **68/74 빨간불**이었고, 실패 6건보다 나쁜 것은 **로그 없이 죽은 관문이
4개** 있었다는 점이다:

| 관문 | 무엇을 지키던 것인가 | 어떻게 죽었나 |
|---|---|---|
| `C4b` | 창 함수 정의부 **기본값 금지** | `C4a` 가 옛 이름으로 정의를 못 찾아 `continue` → 아예 미실행. 검증된 변이 **P3**·**P13** 을 못 잡는 상태 |
| `C5` | 호출부 **위치 인자 금지** | 스캔 대상을 `_DEFS` 에서 뽑는데 옛 이름이라 `calls_to()` 가 0건 → `miss=[]` 로 **공허하게 초록** |
| `R1a/R1b` | 역산 요구 == 선언 창 | `bars=rc.REQUIRED_BARS` 가 `ast.Attribute` 라 `_const_int` 가 못 접음 → 영구 실패 |
| `E0` → `[E]`·`[B]` | 런타임 관측 전부 | 위 판독 실패로 **런타임 군을 통째로 스킵** |

실패 개수(6건)만 보면 죽은 관문 4개가 안 보인다. 초록 스위트가 충분하지 않은
것과 같은 이유로, **빨간 스위트의 실패 개수도 충분하지 않다.**

## 무엇을 바꿨나

### `automation/diag_hist_window_consumers.py` (926 → 1,105줄)

**① 리네임 락스텝 (6곳)**
- 독스트링 · `_DEFS` ×2 · `_SITES` · `group_E` 의 `B_HID` · `E5`
- `_fmp_price_history_close` → `_fmp_price_history_ohlcv`
- `_fmp_batch_close_df` → `_fmp_batch_ohlcv_df`

**② 반환형 계약 (`E5`)**
```python
ser, vol_s = hid._fmp_price_history_ohlcv("CCC", bars=B_HID)   # 옛: ser = ...
```
리네임만 하고 언패킹을 안 하면 `len(ser)` 가 **2** 가 되어 E5b 가 "공급 2봉"을
보고하고 E5c 에서 `ValueError: Buffer has wrong number of dimensions` 로 죽는다
(실측 확인). 조용한 오답이 아니라 요란한 크래시지만 `[B]` 군이 통째로 안 돈다.

거래량 계약 2건 추가:
- `E5d` 거래량 봉수 == 종가 봉수
- `E5e` 거래량이 종가와 다른 시리즈 — 같으면 `avg_dollar_volume` 이 종가²를
  평균내어 유동성 게이트가 **조용히** 틀린다

**③ `[H]` 군 신설, `_SITES` 에서 run_hidden_alpha 제거**

`[R]` 의 전제는 "창을 선언하는 함수가 곧 유일한 소비자"다. 이 사이트만 깨진다 —
`build_ranked_table` 이 선언하는 창 **하나**를 세 소비자가 나눠 쓰고, 둘은 다른
모듈에 있다:

```
build_ranked_table       calculate_period_return(s,21) → iloc[-22]  →  22봉
rc.daily_returns_frame   CORR_LOOKBACK + 1 (pct_change)             →  61봉
rc.avg_dollar_volume     DOLLAR_VOLUME_WINDOW                       →  20봉
                                     ↓
              rc.REQUIRED_BARS = max(...) = 61   ← 창 깊이 소유권
```

역산 22 와 선언 61 을 `==` 로 묶으면 영원히 빨간불이고, `≥` 로 완화하면
**과다 선언을 못 잡는다**(검증된 변이 P2 = "SPY bars 64→200" 이 그 형태).
그래서 지키는 명제를 바꿨다:

| | 검사 |
|---|---|
| `H1` | 호출부가 정책을 **이름으로 참조**하는가 (리터럴 복사 금지) |
| `H2` | 정책이 `max(...)` 로 살아 있는가 (리터럴로 굳지 않았는가) |
| `H3` | 세 요구의 이름이 정책의 `max` 안에 실제로 있는가 |
| `H4` | 정책 안의 **리터럴**이 국소 소비자 역산치와 일치하는가 ← 양방향 |
| `H5` | 정책 값이 국소 요구를 덮는가 |
| `H6` | 창이 한 벌인가 (여러 벌이면 깊이가 갈린다) |

`H1` + `H4` 가 과다 선언을 양방향으로 잡는다. 특히 `H4` 는 지금까지 **아무도
지키지 않던 자리**다 — `calculate_period_return(s, 21)` → `(s, 63)` 으로 요구가
깊어지면 역산 64 ≠ 정책 리터럴 22 로 잡힌다.

`max` 안의 이름은 중첩까지 훑되(`CORR_LOOKBACK + 1`), 리터럴은 **최상위 인자만**
센다. 중첩까지 세면 `+ 1` 의 1 이 섞여 `H4` 가 무너진다.

⚠️ `rotation_core` 는 `_MODULES` 에 넣지 않았다. FMP URL 이 없어 `C1a` 가
오탐한다. 창 깊이의 **정책 소유자**일 뿐 소비처가 아니다.

**④ `B_HID` 조달 경로 변경**
```python
B_HID = getattr(rc, "REQUIRED_BARS", None)
```
`ast.Attribute` 는 접히지 않으므로 정책 소유자에서 직접 읽는다. 진단이 들고 있는
숫자가 아니라 소스의 값이므로 대조가 공허해지지 않는다. 이름 참조가 유지되는지는
`H1` 이 별도로 지킨다 — 리터럴로 바꾸면 `H1` 이 죽는다.

### `.github/workflows/diag_hist_window_consumers.yml` (233 → 276줄)

- 항목수 114 → **125**
- `[H]` 군 설명 + `rotation_core` 상수를 사용 시점 목록에 추가
- 6차 변이 기록(16건) + 죽은 관문 4개의 사후 분석
- `check_py311.py` 대상에 `rotation_core.py` 추가

## 검증

```
결과: 125/125 통과                       (직전 68/74 — C-STOP 으로 런타임 군 스킵)
py_compile                               OK
pyflakes 델타                            0건 (기존 'os' unused 1건 동일)
check_py311.py (8+1 파일)                OK
락스텝 diag_hist_window.py               127/127
락스텝 check_freshness.py                정합성 8모듈 통과
```

**변이 16건 전건 탐지 (무효 0 — 전부 py_compile 사전 통과):**

| 변이 | 잡은 관문 |
|---|---|
| P3 `_closes` 기본값 부활 | C4b |
| P13 `_fmp_batch_ohlcv_df` 기본값 부활 (중간 계층) | C4b |
| P13b `_fmp_price_history_ohlcv` 기본값 부활 | C4b |
| H1a 호출부에 리터럴 61 하드코딩 | H1 |
| H1b 과다 선언 리터럴 200 (P2 형) | H1 |
| H1c `bars=` 를 위치 인자로 | **C5** H1 |
| H2 `REQUIRED_BARS` 를 리터럴로 굳힘 | H2 |
| H3a `daily_returns_frame` 기본값을 리터럴로 | H3a |
| H3b `max` 에서 `DOLLAR_VOLUME_WINDOW` 누락 | H3b |
| H4a 국소 요구 21→63, 정책 리터럴은 그대로 | H4 **H5** |
| H4b 정책 리터럴 과다 22→200 | H4 |
| H4c 정책 리터럴 축소 22→6 (P5 형) | H4 |
| H6 배치 호출을 두 벌로 | H6 |
| E5e 거래량 대신 종가를 두 번 반환 | E5e |
| C1b URL 에 `limit=` 부활 | C1b (+C-STOP) |
| C4a 리네임 되돌리기 | C4a |

P3 · P13 은 이 커밋 직전 **못 잡던** 변이다. C5 는 H1c 로 판별력 복구를 실증했다.

## 배포

**변경 파일 2개.** `run_hidden_alpha.py` · `rotation_core.py` 는 **손대지 않았다**
— 코드는 이미 옳았고 진단만 낡아 있었다.

```
automation/diag_hist_window_consumers.py      1,105줄 (GitHub 표시 1,104)
.github/workflows/diag_hist_window_consumers.yml  276줄 (GitHub 표시 275)
```

Streamlit 재부팅 불필요 (`app.py` 무관, 수동 전용 워크플로).

## 남은 한계

- `diag_hist_window.offset_of` 의 `tail` 규칙이 `max(base, w-1)` 이라 **체인된
  파생 시리즈에서 base 를 잃는다.** `num.pct_change().tail(60)` 의 실요구는
  61봉인데 60 이 나온다. 지금 문제가 안 나는 것은 다른 소비처들이 `tail` 을 원시
  시리즈에만 써서 `base=0` 이기 때문이다. 올바른 규칙은 `base + w - 1` 로 보이나,
  `[D]` 군과 공유하는 하네스라 별건 조사로 남긴다. **이번 [H] 설계가 rotation_core
  역산을 피한 실질적 이유가 이것이다.**
- `_const_int` 가 `int(x)` 껍질을 못 벗긴다(`ast.Call` 미지원). 위와 같은 이유로
  `rotation_core` 의 요구를 AST 로 역산할 수 없다.
- `check_freshness.py` 지문표에 `run_hidden_alpha.py` · `rotation_core.py` 가
  없다. `rc.REQUIRED_BARS` 같은 크로스모듈 참조가 정합성 검사 대상 밖이다.
