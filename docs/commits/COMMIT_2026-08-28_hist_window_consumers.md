# refactor(hist): historical-price-eod 소비처 4곳 limit → from/to, A1 래칫 3파일 잠금

`fmp_extras._closes` · `run_hidden_alpha._fmp_price_history_close` ·
`run_narrative._fmp_close_series` · `run_drg_verify.verify_prediction` 의
`limit`(무시되는 파라미터)을 `from`/`to` 창으로 바꾼다. 요구 봉수는 옛 `limit`
숫자가 아니라 **소비 구문에서 역산**했다 — 그 결과 run_narrative 의 실요구가
130 이 아니라 **200봉**이라는 것이 드러났다.

곁들여 원시 `requests.get` 3곳을 `fmp_http` 로 옮겨 A1 기준선에서
`run_narrative`(2) · `run_drg_verify`(1) 항목을 제거하고, 그 과정에서 발견한
**A1 탐지기의 별칭 사각지대**를 고친다.

락스텝 7파일. FMP 콜 수 증가 0 · 시트 쓰기 0 · 이메일 스키마 변경 0 · 신규 시크릿 0.
페이로드는 1회 실행 기준 **주간 로테이션 경로에서 84~98% 절감**.

---

## 1. 변경 파일

| 파일 | 변경 전 | 변경 후 | 성격 |
|---|---|---|---|
| `fmp_extras.py` | 1,089줄 | **1,106줄** | `_closes` 창 전환 |
| `automation/run_narrative.py` | 922줄 | **945줄** | 본체 (창 + A1) |
| `automation/run_drg_verify.py` | 513줄 | **531줄** | 본체 (창 + A1 + 앵커) |
| `automation/run_hidden_alpha.py` | 768줄 | **784줄** | 본체 (창) |
| `automation/diag_fmp_ssot.py` | 1,103줄 | **1,185줄** | A1 기준선 + **탐지기 수정** |
| `automation/diag_hist_window_consumers.py` | — | **800줄** | 신규 회귀 스위트 |
| `.github/workflows/diag_hist_window_consumers.yml` | — | **155줄** | 신규 워크플로 |

(줄 수는 `check_freshness.py` 기준. GitHub 표시는 각각 1 적다:
1105 / 944 / 530 / 783 / 1184 / 799 / 154)

`app.py` · `run_earnings_watch.py` 는 이번에 건드리지 않았다.

---

## 2. 인수인계 메모의 오기 정정

메모에 대상이 *`fmp_extras.py:661`* 로 적혀 있었다. 661 은 `_movers` 이고
그 `limit` 은 **클라이언트 슬라이싱**(`data[:limit]`)이라 FMP 파라미터와 무관하다.
실제 대상은 **835 `_closes`** 였다.

또 메모는 이 5곳을 *"전부 얇아서 한 대화면 끝난다"* 고 적었는데, 실사 결과
`run_earnings_watch` 는 성격이 다르다(§7-4 참조) — 이번 스코프에서 뺐다.

---

## 3. 각 호출부가 실제로 소비하는 꼬리 깊이

`limit` 은 무시되므로 4곳 모두 **약 1,254봉**을 받고 있었다.
값은 옛 `limit` 숫자가 아니라 **소비 구문에서 역산**했다.

| 파일 | 소비 구문 | 요구 봉 | 옛 limit | 새 창 |
|---|---|---|---|---|
| `run_hidden_alpha` | `calculate_period_return(s, 21)` → `iloc[-(21+1)]` | **22** | 130 | 40일 |
| `run_narrative` SPY | `spy_close.iloc[-64]` | **64** | 130 | 101일 |
| `run_narrative` 종목 | `s.rolling(200, min_periods=150)` | **200** | 130 | 299일 |
| `fmp_extras` 시장필터 | `len(spy)>=200` · `spy.tail(200).mean()` | **200** | 260 | 299일 |
| `fmp_extras` 챔피언 | `_trailing_return(s, 126)` → `iloc[-1-126]` | **127** | 170 | 193일 |
| `run_drg_verify` | 예측일 종가 + 직전 종가 | **2** | 20 | 21일 |

### ⚠️ 옛 `limit` 을 그대로 창으로 옮겼다면 판정이 조용히 바뀐다

`run_narrative` 종목 창이 그 사례다. 요구가 130 이라고 믿고 130봉 창을 주면:

```python
ma200 = float(s.rolling(200, min_periods=150).mean().iloc[-1]) if len(s) >= 150 else np.nan
above_ma200 = bool(s.iloc[-1] > ma200) if pd.notna(ma200) else None
```

`len(s)` 가 150 미만이라 가드가 실패 → `ma200 = NaN` → `above_ma200 = None`.
그러면 verdict 4분기 중 **"❌ 하락 추세 (대기)" 가 통째로 도달 불가**가 되고,
해당 종목은 "⏳ 신호 대기" 로 흘러간다. 예외도 로그도 남지 않는다.

150~199봉 구간은 **더 나쁘다** — `min_periods=150` 때문에 **에러 없이** 더 짧은
창의 평균이 나오고, `above_ma200` 이 조용히 틀린다.

즉 옛 `limit` 숫자는 **아무도 검증한 적이 없는 값**이다. 무시돼 왔으므로
검증될 기회 자체가 없었다. 이 커밋의 요구 봉수는 전부 소비 구문에서 뽑았다.

### 페이로드

| 호출부 | 콜/실행 | 전 | 후 |
|---|---|---|---|
| `run_hidden_alpha` 배치 | ETF 유니버스 N | 1,254N | 27N |
| `run_narrative` SPY | 1 | 1,254 | 69 |
| `run_narrative` 종목 | Emerging M | 1,254M | 205M |
| `fmp_extras` 시장필터 | 1 | 1,254 | 205 |
| `fmp_extras` 챔피언 | 후보 K | 1,254K | 132K |
| `run_drg_verify` | 미검증 예측 P | 1,254P | 14P |

절감률은 호출부별 84%(종목 200봉) ~ 98.9%(drg_verify 2봉).
**콜 수는 한 건도 늘지 않는다.**

---

## 4. `run_drg_verify` 만 창의 기준점이 다르다

나머지 3곳은 룩백 기준점이 **오늘**이다. 이 한 곳만 **`pred_date`** 다.

```python
fmp_data = fh.fmp_get_json(
    "historical-price-eod/full?symbol=" + str(bench_etf)
    + fx.hist_range_params(fx.hist_days_for_bars(2), today=pred_date.date()))
```

오늘 기준으로 창을 만들면, 미검증 예측이 며칠 밀려 있을 때(주말·휴장·워크플로
실패·수동 재실행) 창이 `pred_date` 봉을 아예 안 담는다. 그러면
`hist_on_pred.empty` 로 빠져 `("", NaN, "")` 이 돌아가고 그 예측은 **영구
미검증**으로 남는다. 이메일에는 "검증할 예측 없음" 한 줄만 뜬다.

`hist_range_params(calendar_days, today=None)` 이 이미 앵커 인자를 받고 있었다.

### 죽은 변수 2개 정리

```python
start_date = pred_date - pd.Timedelta(days=14)   # 계산만 하고
end_date   = pred_date + pd.Timedelta(days=2)    # 한 번도 안 쓰였다
```

원래 의도가 정확히 이 창이었다. 여기서 되살린 셈이고, 이제 정책 변환기가
같은 일을 한다(`hist_days_for_bars(2)` → `HIST_MIN_DAYS` 바닥이 걸려 21일).

---

## 5. 승인안에 없던 변경 2건

### 5-1. `_closes` · `_fmp_price_history_close` · `_fmp_batch_close_df` · `_fmp_close_series` 의 기본값 제거

`_closes(ticker, limit=170)` 은 두 호출부의 요구가 **다른데** 기본값 하나로
가려져 있었다 — 시장 필터는 200봉(`tail(200)`), 챔피언 선발은 127봉.
기본값 170 은 시장 필터 요구(200)에 **미달**이었고, `limit` 이 무시된 덕에
우연히 문제가 안 났을 뿐이다.

`run_narrative._fmp_close_series` 도 같다(SPY 64봉 vs 종목 200봉).

→ 전부 `bars` 필수 인자로 바꾸고 호출부에 요구를 명시했다.
요구를 못 밝히면 `TypeError` 로 즉시 죽는 편이 낫다.
`run_hidden_alpha._fmp_batch_close_df` 같은 **중간 계층**에도 기본값을 두지
않는다 — 중간이 기본값을 가지면 최종 호출부의 요구가 가려진다.

기본값 부활은 **그 자체로는 동작을 안 바꾸므로 런타임 검사에 안 잡힌다.**
`C4b` 가 정적으로 못 박는다(`diag_hist_window` 의 `D0e` 와 같은 층).

### 5-2. `diag_fmp_ssot` 의 A1 탐지기 — 별칭 사각지대

변이 P10 을 만들다 드러났다. 탐지기가 이렇게 돼 있었다:

```python
and c.func.value.id == "requests"
```

즉 **별칭 한 줄이면 래칫이 통째로 우회된다**:

```python
import requests as _rq
_rq.get(url, timeout=8)          # ← A1 이 못 봤다
```

이 커밋은 기준선에서 파일 2개를 제거하면서 *"이제 한 곳이라도 생기면 신규
우회로 실패한다"* 고 적는다. 그 문장이 별칭 앞에서는 **사실이 아니었다.**
`from requests import get` 과 `_g = requests.get` 도 같은 구멍이다.

→ `_requests_names(tree)` 를 신설해 requests 모듈/함수를 가리키는 이름을
전부 모으고, 그 이름으로 나가는 `.get()` 과 bare `get()` 을 잡는다.
`M10` 이 별칭 3형태 + **오탐 대조**(requests 와 무관한 `cfg.get(url)` 은 잡히면
안 된다)를 검사한다. 오탐 대조가 없으면 "attr=='get' 이면 전부 히트" 로
바꿔도 통과한다.

기존 부채 총계는 변하지 않았다(73곳) — 저장소에 별칭 사용처가 없었다.
탐지기만 넓어졌다.

---

## 6. 신규 스위트 `[C] [R] [E] [B]` — 왜 정적 분석으로 부족한가

이 파일들의 실패 모드는 **전부 조용한 값 오류**다.

`[E]` 는 4개 모듈을 **실제로 임포트해 스텁 FMP 위에서 실행**한다. 스텁은
요청된 `from`/`to` 창 안의 평일을 봉으로 생성하므로, 창이 짧으면 봉이 실제로
모자라진다 — 창 길이를 문자열로 비교하는 게 아니라 **결과를 관측**한다.

| 검사 | 내용 |
|---|---|
| `C1~C3` | URL 의 limit 부재 · from/to 결합 · `import requests` 부재(래칫) |
| `C4` | 창 함수 정의부의 **기본값 금지**, 옛 `limit` 인자 제거 |
| `C5~C7` | 호출부 `bars=` 명시 · 순수 환산기 직접 호출 금지 · `0.6871` 복제 금지 |
| `C8` | run_drg_verify 의 `today=` 앵커 존재 · 죽은 변수 부재 |
| `C-STOP` | 원시 GET 잔존 시 **런타임 검사 중단** |
| `R0a~R0c` | **역산기 양성대조** — rolling / iloc / 함수 경계 넘김 |
| `R1a·R1b` | 소비 구문 역산치 == 호출부 `bars=` 선언치 |
| `R2a·R2b` | 선언의 근거(rolling 200 · 하드게이트 150)가 소스에 실재하는가 |
| `E1·E4·E5` | 관측 창 == 정책 변환기 산출값, 공급 봉수 ≥ 요구 |
| **`E2`** | **`above_ma200` 이 None 이 아님을 관측** — 이 결함의 결정적 검사 |
| **`E3`** | `to` == `pred_date+1` 확인 + 방향/수익률이 실제로 산출되는가 |
| `B` | **7달력일 연속 휴장 주입** 후 `[E]` 전부 재실행 |

### 요구치도 선언치도 진단이 들고 있지 않다

`diag_hist_window [D] D3` 은 요구치 표를 진단 안에 하드코딩했다(그 커밋 §9-3
의 이월 한계). 여기서는 반복하지 않는다 — **양쪽 다 소스에서 뽑는다**:

- 요구치 → 소비 구문(`rolling` · `tail` · `iloc[-N]` · 함수 경계 넘김) AST 역산
- 선언치 → 호출부의 `bars=` 키워드 판독

두 경로가 서로 독립이라 대조가 공허해지지 않는다. 변이 `P12`
(`rolling(200)`→`rolling(150)`, 선언은 그대로)가 이걸 실증한다.

### 하네스는 복제하지 않고 import 한다

스텁 FMP(`_drg_stub`)와 AST 헬퍼(`offset_of` · `required_bars_in` · `FakeResp`)를
`diag_hist_window` 에서 가져온다. 대가로 두 파일이 **락스텝**이다 —
워크플로가 원본 스위트도 같이 돌려 어느 쪽이 깨졌는지 가른다.

한 가지는 재현할 수밖에 없었다: 요구 봉수 **누산 골격**이다.
`dhw.offset_of` 는 Subscript 를 만나면 첨자를 버리고 base 로 내려가서
`iloc[-64]` 를 0 으로 읽는다(rolling/tail 만 보면 됐던 `run_drg_predict` 에는
충분했다). 계산기를 갈아끼울 훅이 없어 골격만 재현하고, `R0a`(iloc 없는
코드에서 dhw 와 답이 같다) · `R0b`(iloc 있는 코드에서 포크가 더 크다)로
두 구현이 갈리는 것을 막는다.

### `_drg_stub` 의 `supply["days"]` 를 쓰지 않는다

그쪽은 `today - from` 이라 **오늘 기준**인데, `run_drg_verify` 의 창은
`pred_date` 앵커라 값이 어긋난다. 호출 경로 문자열에서 `from`/`to` 를 직접
뽑으면 앵커와 무관하게 정확하다.

---

## 7. 검증

### 7-1. 스위트

| 스위트 | 결과 |
|---|---|
| `diag_hist_window_consumers.py` (신규) | **95/95 통과** |
| `diag_hist_window.py` | **127/127 통과** (회귀 없음) |
| `diag_fmp_ssot.py` | **45건 전부 통과** (44 → +1, M10), A1 부채 76 → **73곳** |
| `diag_pfstate_align` · `diag_watchlist_metrics` · `diag_nodata_radar` | 전부 통과 |
| `py_compile` · `check_py311.py` | 6개 파일 전부 ✅ |

### 7-2. 역검증 — 검사가 결함을 실제로 잡는가

패치 **이전** 4개 파일에 신규 스위트를 투입:

- `diag_hist_window_consumers` → **68건 실패**
  (`C1b` ×4 · `C2a` ×4 · `C3` ×3 · `C4a/C4c` · `C5` · `C8a` · `C8b` · `R1a/R1b` …)
- `C-STOP` 이 작동해 런타임 군을 건너뛰었다 — 진짜 FMP 로 나가지 않았다.

### 7-3. 변이 16종 — 전건 검출

`__pycache__` 를 매 변이마다 삭제하고, 복원은 SHA 대조로 검증했다.

| # | 변이 | 검출 |
|---|---|---|
| P1 | run_narrative 종목 200→130 (옛 limit 이식) | `R1b` **`E2b`** |
| P2 | run_narrative SPY 64→200 (과다 선언) | `R1b` |
| P3 | `_closes` 기본값 부활 | `C4b` |
| P4 | fmp_extras SPY 200→127 (두 요구 혼동) | `R1b` |
| P5 | run_hidden_alpha 22→6 | `R1b` **`E5c`** |
| P6 | run_drg_verify `today=` 앵커 제거 | `C8a` **`E3b`** |
| P7 | 순수 환산기 직접 호출 (마진 우회) | `C6` `E3c` |
| P8 | `limit` 부활 | `C1b` (+`C-STOP`) |
| P9 | `import requests` 만 부활 | `C3` |
| P10 | 원시 GET 실제 부활 | `diag_fmp_ssot A1` |
| P11 | 손으로 `&from=` 작성 (정책 복제) | `C2a` `E5a` |
| P12 | `rolling(200)`→`rolling(150)`, 선언은 그대로 | `R1b` `R2a` |
| P13 | `_fmp_batch_close_df` 기본값 부활 (중간 계층) | `C4b` |
| P14 | `HIST_MIN_DAYS` 21→3 | `diag_hist_window T7a T9` |
| P15 | 기준선 0 파일에 **별칭** 원시 GET 신설 | `diag_fmp_ssot A1` |
| P16 | A1 기준선만 낮추고 코드는 그대로 | `diag_fmp_ssot A1` |

**P10 은 처음에 놓쳤다.** 그 발견이 §5-2(A1 별칭 인지)로 이어졌다.
**P15 도 한 번 갈렸다** — 기준선이 **1** 인 파일에서 호출을 별칭으로 바꾸면
개수가 그대로 1 이라 A1 은 정상 통과한다. 그건 우회가 아니다. 래칫을
시험하려면 **기준선에서 제거된 파일**(0)에 새로 만들어야 한다.

### 7-4. 검사기 자체의 결함 6건 (작성 중 발견·수정)

스위트가 자기 버그를 먼저 잡은 사례들이라 진단 주석에 남겼다.

1. **독스트링 오탐.** 문자열 상수를 무차별로 훑어 URL 을 찾았더니, 이 커밋이
   독스트링에 적은 변경 이력(*"`limit=130` → from/to 창"*)이 코드로 잡혔다.
   검사기가 자기가 만든 문서를 결함으로 신고하는 셈이라, 통과시키려면
   **문서를 지워야 한다** — 정확히 반대 방향의 압력이다.
   → 쿼리 시작(`/full?`) 기준 + 독스트링 노드 배제.
2. **f-string 내부 Constant 이중계수.** URL 하나가 JoinedStr 1 + 내부 Constant 1
   로 두 번 세어져 `C2a` 가 "URL 2건 vs hist_range_params 1회" 로 오탐했다.
3. **`_seen` 단락.** 함수 경계 재귀의 캐시 키에 `consts` 가 빠져 있어,
   같은 함수를 다른 인자로 두 번 부르면(`calculate_period_return(s, 5)` 와
   `(s, 21)`) **뒤 호출이 단락**됐다. walk 순서에 따라 22 대신 **6** 이 나왔다.
4. **선언치를 진단이 들고 있었다.** 초안은 `_SITES` 에 `bars` 숫자를 적어
   대조했다 — 소스의 `bars=200` 을 130 으로 바꿔도 진단은 여전히 200 과
   대조해 **통과한다.** `D3` 한계의 재발이라 소스 판독으로 바꿨다.
5. **언더스코어 좌변.** `spy_close, _ = ...` 의 좌변 집합이 seeds 와 안 맞아
   선언치 판독이 통째로 실패했다.
6. **정책 소유자 오탐.** `fmp_extras` 자신이 `calendar_days_for_bars` 와
   `0.6871` 을 가지고 있는데 `C6`/`C7` 이 그걸 위반으로 신고했다
   (`fmp_http` 가 A1 에서 면제되는 것과 같은 이유로 면제).

---

## 8. 기각한 대안

| 대안 | 기각 사유 |
|---|---|
| 옛 `limit` 숫자를 그대로 창으로 환산 | **run_narrative 가 조용히 깨진다**(§3). limit 은 무시돼 왔으므로 그 숫자는 검증된 적이 없다 |
| `run_earnings_watch` 도 이번에 함께 | 요구가 창 상수가 아니라 **8분기 이벤트 커버리지**(≈500봉)라 산정 방식이 다르고, `earnings_core.measure_reaction:415` 의 `iloc[i-1]` 무가드(i=0이면 `iloc[-1]` 로 감겨 **최신 종가가 '직전 종가'** 가 된다)를 동반 수정해야 한다. 섞으면 "동작 불변" 검증이 흐려진다 → §9 이월 |
| `hist_days_for_bars` 기본 `pad_bars` 5→8 상향 | 여유 0 문제(§9-2)는 실재하지만, 상향하면 `run_drg_predict` 창(51→56)과 `diag_hist_window` D3 표·T7~T9 가 락스텝에 끌려 들어와 검증 비용이 이번 작업보다 커진다 → §9 이월 |
| 신규 스위트를 `diag_hist_window` 에 `[E]` 군으로 확장 | 1,292 → 약 1,700줄. 하네스는 import 로 재사용할 수 있으므로 파일을 나누는 편이 납품·리뷰가 싸다 |
| 하네스를 새 파일에 복제 | `_drg_stub` 이 두 벌이 되면 한쪽만 갱신된다. 그 실패는 로그를 안 남긴다 |
| `_closes` 의 두 호출부를 200 으로 통일 | 챔피언 선발이 요구(127)의 1.6배를 받는다. 요구가 다르면 다르게 선언하는 것이 이 커밋의 요지다 |

---

## 9. 남은 한계 · 이월

1. **`run_earnings_watch.py:245`** — `limit=900` 잔존. A1 기준선에도 1곳 남아 있다.
   `measure_reaction` 의 `iloc[i-1]` 무가드와 함께 단독 대화로 다룬다.
2. **`hist_days_for_bars` 의 마진 여유가 0 이다.** `pad_bars=5` 는 7달력일
   비상 휴장으로 잃는 봉수(≈5봉)를 **정확히 상쇄만** 한다. 요구가 15봉을
   넘으면 `HIST_MIN_DAYS=21` 바닥도 안 걸리므로 슬랙이 없다:

   | 대상 | 요구 | 창 | 정상 공급 | 7일 휴장 시 | 여유 |
   |---|---|---|---|---|---|
   | run_hidden_alpha | 22봉 | 40일 | 27봉 | 22봉 | **+0** |
   | fmp_extras 챔피언 | 127봉 | 193일 | 132봉 | 127봉 | **+0** |
   | run_narrative 종목 | 200봉 | 299일 | 205봉 | 200봉 | **+0** |
   | run_drg_verify | 2봉 | 21일 | 14봉 | 9봉 | +7 |

   요구 충족은 되므로 실패는 아니다. `[B]` 군이 매 실행마다 이 사실을 다시
   확인한다. 상향 결정은 별도 항목.
3. **`run_signal_backtest.py:1046`** — `limit` 잔존. 백테스트라 장기 데이터가
   목적이고 `limit` 이 무시된 덕에 **우연히** 맞고 있다. 창으로 바꾸려면
   요구를 명시해야 한다.
4. **`narrative_core.py`** A1 기준선 2곳 잔존 — 이번 스코프 밖.
5. **`check_freshness.py` 지문표**에 `run_narrative` · `run_drg_verify` ·
   `run_hidden_alpha` · `diag_hist_window` · `diag_fmp_ssot` 가 없다.
   이번 세션에서 이 구멍 때문에 편집 대상 4개가 미검증 상태였다. 추가 필요.

---

## 10. 락스텝 배포 순서

**순서를 지켜야 한다.** 중간 상태에서 자동화가 돌면 깨진다.

1. `fmp_extras.py` (**레포 루트**) — 먼저. `_closes` 시그니처가 바뀌므로
   이 파일이 낡으면 아래가 `TypeError` 로 죽는다
2. `automation/run_hidden_alpha.py` · `automation/run_narrative.py` ·
   `automation/run_drg_verify.py`
3. `automation/diag_fmp_ssot.py` — 2번보다 먼저 올리면 `A1` 이 실패한다
   (기준선에서 뺀 파일에 원시 GET 이 아직 남아 있는 상태)
4. `automation/diag_hist_window_consumers.py` (신규)
5. `.github/workflows/diag_hist_window_consumers.yml` (신규)

브랜치: `dev`.

Streamlit 재부팅은 **정합성상 불필요**하다. `_closes` 는 `fmp_extras` 내부
전용이고(`app.py`·`run_weekly_report` 는 `compute_satellite_top10()` 만 부른다),
그 공개 시그니처는 안 바뀌었다. 푸시하면 어차피 재배포되므로, 재배포 후
위성 로테이션 화면을 한 번 열어 값이 나오는지만 확인하면 된다.
