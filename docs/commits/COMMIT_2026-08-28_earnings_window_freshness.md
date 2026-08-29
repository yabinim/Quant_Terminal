# refactor(earnings): run_earnings_watch 창 limit=900 → 12분기 from/to, 지문표에 자동화 7개 편입

두 갈래다.

**① `run_earnings_watch.fmp_price_history`** 의 `limit=900`(무시되는 파라미터)을
`from`/`to` 창으로 바꾼다. 요구 분기수는 옛 주석의 "8분기"가 아니라 **소비 구문에서
역산한 12분기**다. 원시 `requests.get` 도 `fmp_http` 로 옮겨 A1 기준선에서
`run_earnings_watch`(1) 항목을 제거한다.

**② `check_freshness.py`** 지문표에 자동화·진단 7개를 추가하고, 모듈 간 정합성
검사를 자동화 2개 → **6개**로 확대한다. 이 구멍 때문에 2026-08-28 세션에서 편집
대상 4개가 미검증 상태로 납품됐다.

곁들여 작업 중 발견한 **`diag_hist_window` [D] 군의 시계 불일치**를 고친다 —
하루 4~5시간 동안 무조건 실패하던 결함이다.

락스텝 6파일. FMP 콜 수 증가 0 · 시트 쓰기 0 · 이메일 스키마 변경 0 · 신규 시크릿 0.
실적 레이더 가격 이력 페이로드는 **38% 절감**(1,254봉 → 778봉).

---

## 1. 변경 파일

| 파일 | 변경 전 | 변경 후 | 성격 |
|---|---|---|---|
| `automation/run_earnings_watch.py` | 1,166줄 | **1,234줄** | 본체 (창 + A1 + 판별자) |
| `check_freshness.py` | 187줄 | **224줄** | 지문표 + 정합성 확대 |
| `automation/diag_hist_window_consumers.py` | 800줄 | **926줄** | [Q] 군 + C2b 강화 |
| `automation/diag_hist_window.py` | 1,293줄 | **1,306줄** | [D] 군 시계 통일 |
| `automation/diag_fmp_ssot.py` | 1,185줄 | **1,190줄** | A1 기준선 73 → 72 |
| `.github/workflows/diag_hist_window_consumers.yml` | 155줄 | **219줄** | 문서 + 락스텝 단계 |

(줄 수는 `check_freshness.py` 기준. GitHub 표시는 각각 1 적다:
1233 / 223 / 925 / 1305 / 1189 / 218)

`earnings_core.py` 는 **건드리지 않았다** — §2 참조.

---

## 2. 인수인계 메모의 오기 정정 — `iloc[i-1]` 무가드는 존재하지 않는다

메모는 이렇게 적혀 있었다:

> `earnings_core.measure_reaction:415` 의 `iloc[i-1]` 무가드(i=0이면 `iloc[-1]` 로
> 감겨 최신 종가가 '직전 종가'가 됨) 동반 수정 필요

**틀렸다.** 상류에 가드가 이미 있다:

```python
# resolve_reaction_index 끝
if cand <= 0 or cand >= len(idx):
    return None
```

`i=0` 은 `measure_reaction` 에 도달할 수 없다. `iloc[-1]` 로 감기는 경로는 없다.
고쳤다면 **없는 병을 고치는 코드**가 영구히 남았을 것이다.

### 다만 실제 실패 모드는 따로 있다 — 그게 이 커밋의 요지다

이벤트가 창 밖이면 `searchsorted` 가 `pos=0` → `cand=0` → `None` 을 돌린다.
값이 틀리는 게 아니라 **측정이 통째로 사라진다.** 예외도 로그도 없다.

창을 줄일 때 확률이 올라가는 것은 정확히 이쪽이다. 그래서 값을 고치는 대신
**관측 장치**(`_hist_window_bound` + `[WARN]` 한 줄)를 넣었다.

---

## 3. 요구는 8분기가 아니라 12분기다

옛 상수: `_HIST_LIMIT = 900  # 8분기(≈2년) 갭 이력 + ATR 산출에 여유`

소비 구문:

```python
past = ec.past_earnings_dates(tk, ...)   # limit = GAP_QUARTERS + 4 = 12개 이벤트
ec.gap_history(hist, past)               # 성공 8건 모일 때까지 12개를 순회
```

`gap_history` 는 측정 실패한 이벤트를 **건너뛰고 계속 돈다**. 8분기로 창을 자르면
9~12번째가 창 밖으로 나가고, 앞 8개 중 하나라도 실패하면 표본이 8 아래로 떨어진다.
**6 미만이면 `expected_move` 의 confidence 가 강등된다**
(`earnings_core`: `len(vals) < GAP_QUARTERS - 2`).

| 창 후보 | 공급 봉 | 절감 | 위험 |
|---|---|---|---|
| 8분기 (760일) | ≈522 | 58% | 표본 8 → 6 강등 가능 |
| **12분기 (1,133일)** | **778** | **38%** | 없음 — 이벤트 목록 상한과 일치 |

**옛 `limit` 숫자를 요구의 근거로 쓰지 않았다.** `limit` 은 무시돼 온 값이라 한
번도 검증된 적이 없다. `run_narrative` 가 실증이다(limit 130 / 실요구 200봉).

### 창 산출

```python
_HIST_QUARTERS = ec.GAP_QUARTERS + 4       # = 12. past_earnings_dates 상한과 동일
_QUARTER_DAYS = 91.31                      # 365.25 / 4
_HIST_BARS = (fx.bars_for_calendar_days(_HIST_QUARTERS * _QUARTER_DAYS)  # 752봉
              + ec.VOLUME_BASELINE_BARS    # 거래량 기준 20봉
              + 1)                         # 직전 종가 1봉  → 773봉
_HIST_DAYS = fx.hist_days_for_bars(_HIST_BARS)   # 1,133달력일 ≈ 778봉
```

매직넘버가 아니라 `earnings_core` 상수에서 유도한다. `[Q]` 군이 이 유도를 지킨다.

| 구분 | 봉 |
|---|---|
| 공급 | 778 |
| 하드 요구 (12분기 도달 + 직전 종가) | 753 |
| 소프트 요구 (+거래량 기준 20봉) | 773 |
| 여유 | +25 / +5 |

거래량 기준 20봉은 `iloc[max(0, i-20):i]` 로 감싸여 있어 모자라도 조용히 퇴화할
뿐 실패하지 않는다. 따라서 **실질 여유는 +25봉**이다.

---

## 4. 왜 호출부별 `bars` 인자를 두지 않았나

§7 은 "창을 만드는 함수에 `bars` 기본값 금지"라고 못 박았다. 여기서는 **반대로
단일 창이 정답**이다 — 호출부 6곳이 `hist_cache` 를 공유하기 때문이다:

```python
hist = hist_cache.get(tk)
if hist is None:
    hist = hist_cache[tk] = fmp_price_history(tk)
```

먼저 부른 쪽의 창이 캐시에 박힌다. 요구가 다른 창을 섞으면 나중 호출부는 **자기
요구보다 얕은 이력을 받고도 그 사실을 모른다.** §7 의 규칙은 호출부가 캐시를
공유하지 않는 경우의 규칙이고, 이 파일에는 적용하지 않는다.
`.yml` 헤더와 `[Q]` 군 주석에 이 예외를 명시했다.

---

## 5. `check_freshness.py` — 구멍 메우기

### 추가한 7개와 마커

| 파일 | 마커 |
|---|---|
| `run_narrative.py` | `hist_days_for_bars` · `fmp_extras` · `narrative_core` |
| `run_drg_verify.py` | `hist_range_params` · `fmp_extras` |
| `run_drg_predict.py` | `hist_days_for_bars` · `fmp_http` |
| `run_hidden_alpha.py` | `hist_days_for_bars` · `fmp_extras` |
| `diag_hist_window.py` | `required_bars_in` · `hard_gate_in` |
| `diag_hist_window_consumers.py` | `required_bars_in` · `diag_hist_window` |
| `diag_fmp_ssot.py` | `_RAW_GET_BASELINE` · `_fmp_url_names` |

마커는 줄 수보다 강하다 — 줄 수는 사람이 GitHub 과 눈으로 대조해야 하지만 마커는
자동 판정된다.

### 정합성 검사 확대 (실제 방어선)

`AUTOMATION` 튜플 신설. 기존 2개(`run_earnings_watch`·`run_watchlist_alerts`) →
**6개**. 여기 없는 자동화 파일은 공용 모듈이 바뀌어도 아무 경고가 안 뜬다.

실측 — `fmp_extras.hist_days_for_bars` 를 지우면:

```
❌ run_earnings_watch.py → fmp_extras  1개 없음 → ['hist_days_for_bars']
❌ run_narrative.py      → fmp_extras  1개 없음 → ['hist_days_for_bars']
❌ run_drg_verify.py     → fmp_extras  1개 없음 → ['hist_days_for_bars']
❌ run_drg_predict.py    → fmp_extras  1개 없음 → ['hist_days_for_bars']
❌ run_hidden_alpha.py   → fmp_extras  1개 없음 → ['hist_days_for_bars']
❌ 정합성 문제 5건
```

전환 전에는 이 5건이 **전부 침묵**이었다.

### `automation/` 경로 폴백

`SUBDIRS = ("", "automation", ".github/workflows")`. 사본 폴더는 평면이지만
레포는 `automation/` 하위다. 레포에서 돌렸을 때 자동화 파일이 통째로 "사본 없음"
으로 나오면 그 경고가 일상이 되고, **일상이 된 경고는 아무도 안 읽는다.**

성공 시에도 한 줄씩 찍게 했다. 전에는 실패했을 때만 출력돼 검사가 돌긴 했는지
알 수 없었다.

---

## 6. 곁가지 — `diag_hist_window` [D] 군의 시계 불일치 (기존 결함)

배포 전 검증을 돌리다 발견했다. **이 커밋의 변경 때문이 아니다** — 패치 전
기준선에서도 동일하게 재현된다.

| 쪽 | 시계 |
|---|---|
| `fx.hist_range_params` (창을 만드는 쪽) | `datetime.now(_ET_TZ)` — **ET** |
| `diag_hist_window` [D] 군 4곳 | `datetime.now()` — 시스템 로컬 = **UTC** |

같은 파일 644줄은 `datetime.now(m._ET).date()` 로 제대로 쓰고 있어, 파일 안에서도
일관되지 않았다.

**결과**: 매일 **20:00~24:00 ET** 사이에 하루가 어긋나 `D1d`·`D2a`·`D2b`·`D3b`
4건이 무조건 실패한다. 코드는 멀쩡한데 스위트만 빨간불이다. GitHub Actions 는
UTC 로 도니 같은 창에서 재현된다.

```
2026-08-29 00:27 UTC (= 08-28 20:27 ET) 실행 → 123/127
❌ D1d to 가 오늘+1 (2026-08-30) — hist_range_params 규약
❌ D2a 관측 창 [22, 52] ⊆ 정책 산출 [21, 51]
```

`_et_today()` 헬퍼로 통일. 수정 후 **127/127**, 되돌리면 정확히 같은 4건이 다시
실패한다(역검증 확인).

이 결함을 안 고치면 이 커밋의 배포 전 검증 자체를 신뢰할 수 없다 —
"127/127 이 기대값"이라고 적어놔도 시각에 따라 123 이 나오고, 그러면 제 변경이
깬 것인지 구분할 수 없다.

---

## 7. 검증

| 항목 | 결과 |
|---|---|
| `diag_hist_window_consumers` | **114/114** (기존 95 → [Q]군 12 + C 확장 7) |
| `diag_hist_window` | **127/127** (시계 수정 후) |
| `diag_fmp_ssot` | **45/45**, A1 부채 **72곳 / 10개 파일** (73 / 11 에서 감소) |
| `check_freshness` | 정합성 통과, 자동화 6개 전부 초록 |
| `check_py311` | 8개 파일 통과 |
| 변이 시험 | **8/8 탐지** |
| 역검증 (구 `run_earnings_watch`) | **6건 실패 + `[C-STOP]` 작동** |
| 역검증 (시계 되돌림) | **4건 실패 재현** |

### 변이 8종

| # | 변이 | 탐지 |
|---|---|---|
| M1 | `_HIST_QUARTERS` 를 `GAP_QUARTERS` 로 축소(8분기) | `Q2` |
| M2 | `import requests` 부활 | `C3` |
| M3 | URL 에 `limit=900` 부활 | `C1b` (+`C-STOP`) |
| M4 | 손으로 `&from=` 을 **연결 체인 옆 항**에 작성 | `C2b` |
| M5 | `_hist_window_bound` 를 항상 `False` 로 | `Q5b` |
| M6 | 경계 부등호 `<=` → `<` (판별 기준 축소) | `Q5b` |
| M7 | 창 산출에서 `VOLUME_BASELINE_BARS` 누락 | `Q4c` |
| M8 | `past_earnings_dates(limit=8)` 직접 지정 | `Q3` |

### 이번 세션의 가장 교훈적인 대목 — M4

M4 는 처음에 **예상한 `C2b` 가 아니라 `C2a` 로 잡혔다.** 변이는 탐지됐으니
넘어갈 수도 있었지만, 태그가 다르다는 것이 사각지대의 신호였다.

`hist_url_nodes` 는 **문자열 노드 단위**로 본다. 그래서
`f"...?symbol={t}" + "&from=..."` 처럼 연결 체인의 **옆 항**에 붙이면 URL 노드의
리터럴에는 `from=` 이 없어 `C2b` 가 못 본다. 지금은 `C2a`(개수 비교)가 가려주고
있었지만, `hist_range_params` 와 수동 `from=` 을 **둘 다** 쓰면 개수도 맞아
양쪽이 동시에 눈을 감는다.

URL 을 품은 **문 전체**를 `ast.unparse` 해서 보도록 고쳤다.

**그리고 그렇게 강화한 `C2b` 가 곧바로 자기 오탐을 냈다** —
`fmp_extras:141`, `hist_range_params` 의 독스트링이다. 그 독스트링에
"historical-price-eod" 와 "&from=" 이 둘 다 들어 있는데, 독스트링 배제가
`Expr` 노드가 아니라 **내부 `Constant` 의 id** 를 비교하고 있었다. 문자열만 있는
`Expr` 을 통째로 제외해 해결.

교훈 둘: **변이가 잡혔어도 예상과 다른 태그로 잡혔으면 그건 사각지대의 신호다.**
그리고 **탐지기를 넓히면 그 즉시 자기 자신을 다시 시험해야 한다.**

---

## 8. `[Q]` 군의 설계 — 왜 12 를 리터럴로 안 쓰나

```
Q1a  earnings_core.past_earnings_dates 의 limit 기본값을 AST 로 판독  → 12
Q1b  run_earnings_watch._HIST_QUARTERS 존재                          → 12
Q2   창 분기수 ≥ 이벤트 수
Q3   호출부가 past_earnings_dates(limit=) 를 직접 넘기지 않는다
Q4b  공급 778봉 ≥ 하드 요구 753봉
Q4c  공급 778봉 ≥ 소프트 요구 773봉
Q4d  창 1,133일 ≤ 상한 1,826일
Q5b  창 하단에 맞닿은 이력 → True
Q5c  훨씬 뒤에서 시작하는 이력 → False
Q5d  빈 이력 → False (단정하지 않는다)
```

요구치는 `earnings_core` 의 **시그니처 기본값**에서, 선언치는
`run_earnings_watch` 의 **모듈 상수**에서 — 서로 다른 파일에서 독립으로 뽑는다.
진단이 `12` 를 들고 있으면 `GAP_QUARTERS + 4` 를 `GAP_QUARTERS` 로 바꿔도 진단은
여전히 12 와 대조해 **통과한다.** `diag_hist_window [D] D3` 이 남긴 한계를 여기서는
반복하지 않는다.

`Q3` 이 없으면 위 대조가 공허해진다 — 호출부가 `limit` 을 직접 넘기면
시그니처 기본값은 아무 의미가 없기 때문이다.

---

## 9. 기각한 대안

| 대안 | 기각 사유 |
|---|---|
| 8분기(760일)로 더 줄이기 | 절감은 58%로 크지만 표본이 8 → 6 으로 떨어질 수 있고, 그 강등은 **로그를 안 남긴다**. 20%p 절감을 위해 예상 변동폭의 신뢰도를 거는 거래는 손실 방지 원칙에 반한다 |
| `measure_reaction` 의 `iloc[i-1]` 에 가드 추가 | 상류 `resolve_reaction_index` 가 `cand <= 0` 에서 이미 `None` 을 돌린다. 도달 불가 경로에 가드를 넣으면 **없는 병을 고치는 코드**가 영구히 남는다 |
| 호출부별 `bars=` 인자 | 6곳이 `hist_cache` 를 공유한다. 캐시 선점 순서가 이력 깊이를 정하게 된다(§4) |
| 창을 동적으로 (가장 오래된 이벤트 날짜 앵커) | `fmp_price_history` 가 `past_earnings_dates` **보다 먼저** 불린다. 순서를 뒤집으려면 6개 호출부를 전부 손대야 하고, `hist_cache` 공유 구조와 충돌한다 |
| 표본 부족을 개수만으로 경고 | 상장 1년 된 종목의 `n=4` 는 정상이다. 개수만 보면 "창이 짧다"와 "종목 이력이 짧다"가 구분되지 않아 경고가 곧 소음이 된다 → `_hist_window_bound` 판별자 |
| `[D]` 군 시계 수정을 별도 커밋으로 | 이 커밋의 **배포 전 검증 절차 자체가 못 쓰게 된다.** 127/127 이 기대값인데 시각에 따라 123 이 나오면 제 변경이 깬 것인지 구분할 수 없다 |
| `check_freshness` 를 CI 에 안 넣기 | 락스텝 업로드 누락을 **다음 정기 실행 전에** 알 수 있는 유일한 자동 관문이다 |

---

## 10. 락스텝 배포 순서

**순서를 지켜야 한다.**

| # | 파일 | 경로 | 줄 수(GitHub 표시) |
|---|---|---|---|
| ① | `run_earnings_watch.py` | `automation/` | 1233 |
| ② | `diag_hist_window.py` | `automation/` | 1305 |
| ③ | `diag_hist_window_consumers.py` | `automation/` | 925 |
| ④ | `diag_fmp_ssot.py` | `automation/` | 1189 |
| ⑤ | `check_freshness.py` | **레포 루트** | 223 |
| ⑥ | `diag_hist_window_consumers.yml` | `.github/workflows/` | 218 |

- **①을 ③보다 먼저** — ③이 `import run_earnings_watch` 를 하므로, 옛 파일이면
  `Q1b`/`Q4a`/`Q5a` 가 실패한다(실패로 끝나지 교착은 아니다).
- **②를 ③보다 먼저** — ③이 ②에서 하네스(`_drg_stub`·`required_bars_in`)를
  import 한다. ②가 낡으면 ③이 임포트에서 죽는다.
- **④는 ①보다 나중** — ①이 안 올라간 상태에서 ④만 올리면 A1 이
  "`run_earnings_watch`: 1곳 — 기준선에 없는 **신규 우회**" 로 실패한다.
- **⑥은 마지막** — 워크플로가 ⑤를 실행하므로 ⑤가 없으면 단계가 죽는다.
- **Streamlit 재부팅 불필요** — `app.py` 와 공용 모듈을 안 건드렸다.

---

## 11. 남은 한계 · 이월

1. **`run_signal_backtest.py:1046`** — `limit` 잔존. 백테스트라 장기 데이터가
   목적이고 `limit` 이 무시된 덕에 **우연히** 맞고 있다.
2. **`narrative_core.py`** A1 기준선 2곳 잔존.
3. **`app.py`** A1 기준선 62곳 — `@st.cache_data` 대화형 경로. 별도 검토 사안.
4. **`hist_days_for_bars` 의 `pad_bars=5` 여유 문제**는 이 파일에는 해당 없다
   (소프트 요구 20봉이 실질 완충이라 +25봉). 다른 소비처의 이월 항목은 그대로다.
5. **`earnings_core.py:35` 의 `import requests` 가 죽어 있다** — 호출부는
   전부 `_fh.fmp_get_json_ex` 로 옮겨졌는데 임포트만 남았다. A1 기준선에
   없으므로(0곳) 지금 지워도 무해하지만, 이번 스코프 밖이라 두었다.
   지우면 그 파일도 "되살리려면 임포트부터" 래칫에 들어간다.
6. **`[D] D3` 요구치 표는 여전히 `diag_hist_window` 안에 하드코딩**이다
   (AST 역산 아님). `[Q]` 군은 그 한계를 반복하지 않았지만 `[D]` 는 그대로다.
7. **`check_freshness` 정합성 검사가 시그니처를 안 본다** — 심볼의 존재만 보고
   인자 개수·이름은 보지 않는다. `_pf_hist` 4인자 전환 때 실제로 문제가 됐던
   실패 모드다.
