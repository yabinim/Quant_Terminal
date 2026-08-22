# fix(portfolio): Portfolio_Alert_State 의 손절/목표가 위치 결합 제거

`eval_portfolio_eod` 가 A:D 만 `Portfolios` 행 순서로 통째 덮어쓰는 탓에,
E/F(Stop_Loss·Target_Price)가 물리적 행에 붙박여 **다른 종목의 값으로 조용히
어긋나던** 결함을 고친다. E열 이후를 키에 재부착해 A:F 전체를 쓴다.

---

## 배경 — 무엇이 잘못돼 있었나

`run_watchlist_alerts.py:1552-1557` (패치 전):

    prev_len = max(0, len(st_vals) - 1)
    padded = new_rows + [["", "", "", ""]] * max(0, prev_len - len(new_rows))
    body = [_PFSTATE_COLS[:4]] + padded   # A:D만 관리(상태머신). E/F(손절·목표)는 보존
    state_ws.update(body, range_name=f"A1:D{len(body)}", value_input_option="RAW")

주석은 "E/F 는 보존"이라고 말하지만, 보존되는 것은 **물리적 행 위치**일 뿐
**키와의 연결이 아니다.**

`new_rows` 의 순서는 `Portfolios` 시트의 행 순서다(`for r in holdings`).
그런데 E/F 는 쓰이지 않으므로 원래 행에 그대로 남는다. 두 순서가 어긋나는
순간 값이 다른 종목에 달라붙는다.

### 재현

| | Key (A) | Stop_Loss (E) | Target_Price (F) |
|---|---|---|---|
| 행2 | `yab\|Roth\|MRNA` | 130 | 200 |
| 행3 | `yab\|Roth\|PNC` | 240 | 300 |

MRNA 전량 매도 → `Portfolios` 에서 행 삭제 → 다음 5PM 실행:

- `new_rows` = `[[yab|Roth|PNC, ...]]` (1행)
- A:D 만 덮어쓰기 → **행2 = PNC**
- 행2 의 E/F 는 여전히 **130 / 200** (MRNA 것)

`app.py:1262-1272` 의 `load_portfolio_alert_states` 는 `r[0]`(키)과
`r[4]/r[5]`(손절·목표)를 **같은 행에서** 읽는다:

    key = str(r[0]).strip()
    _sl = to_float(r[4]); _tp = to_float(r[5])

→ **PNC 의 손절가가 240 에서 130 으로 조용히 바뀐다.** 오류도, 경고도 없다.

행 삭제뿐 아니라 순서 역전·중간 삽입에서도 동일하게 깨진다. 회귀 스위트를
패치 전 코드에 돌리면 A-3(순서 역전)에서 MRNA 와 PNC 의 손절가가 **서로
맞바뀌는 것**이 확인된다.

### 왜 지금 고치는가

트랜치 비율(`Swing_Weight_Pct`)을 G열에 얹을 예정이다. 같은 결함을 물려받으면
결과가 더 나쁘다 — 손절가 **표시**가 틀리는 것에서 **매도 권장 수량**이
틀리는 것으로 올라간다. 선행 수정이 필요하다.

---

## 변경 내역

### `run_watchlist_alerts.py` (단독, lockstep 대상 없음)

**1. `_read_state_map()` 신설 — E열 이후를 키에 붙여 보관**

    r = (list(r) + [""] * 4)[:4]     # 이전: 4칸에서 잘림
    → _r = (list(_r) + [""] * _NPF)[:_NPF]
      out[_k] = {..., "extra": [str(x) for x in _r[4:_NPF]]}

`_NPF = len(_PFSTATE_COLS)` 로 폭을 상수에서 끌어온다. G열 추가 시
`_PFSTATE_COLS` 한 줄만 늘리면 이 로직은 그대로 동작한다.

**2. `_pf_row()` 신설 — 3개 append 사이트 통일**

    new_rows.append([key, states_csv, prev, today])
    → new_rows.append(_pf_row(key, states_csv, prev))

nodata 경로 · 정상 경로 · except 경로 세 곳 모두 교체. 한 곳이라도 빠지면
그 경로를 탄 종목만 E/F 가 날아가므로 세 곳을 각각 앵커로 잡았다.

**3. 쓰기 직전 재조회 병합**

    _fresh = _read_state_map(state_ws.get_all_values() or [])
    if _fresh:
        for _row in new_rows:
            _ex = _fresh.get(_row[0], {}).get("extra")
            if _ex is not None:
                _row[4:] = (list(_ex) + [""] * (_NPF - 4))[:_NPF - 4]

평가 루프는 종목당 FMP 호출이 있어 수 분이 걸린다. 시작 시점 스냅샷으로
E/F 를 되쓰면, **그 사이 앱에서 손절가를 고친 것을 덮어쓴다** — 이 수정이
새로 만들어내는 위험이다. 쓰기 직전 재조회로 경합 창을 수 분에서 약 1초로
줄인다. 재조회 실패 시에는 시작 스냅샷으로 폴백하고 `[WARN]` 을 남긴다.

**4. A:D → A:F 전체 쓰기**

    body = [_PFSTATE_COLS[:4]] + padded ; range_name=f"A1:D{...}"
    → body = [_PFSTATE_COLS] + padded ; range_name=f"A1:{_lc}{...}"

패딩도 `[["", "", "", ""]] * n` → 리스트 컴프리헨션으로 바꿨다. 곱셈 패턴은
같은 리스트 객체를 n개 참조하므로, 이후 누가 행을 제자리 수정하면 전부
같이 바뀐다. 지금은 읽기 전용이라 무해하지만 G열 작업 중 밟기 쉬운 함정이다.

---

## 설계 근거 / 기각한 대안

**기각 1 — 기존 행 순서를 유지하도록 `new_rows` 를 정렬**
기존 키는 원래 물리 위치에 두고 신규 키만 뒤에 붙이는 방식. 쓰기 범위를
A:D 로 유지할 수 있어 변경이 작다. 그러나 위치 결합 자체는 남는다. 시트를
손으로 정렬하거나 행을 지우는 순간 다시 깨지고, 그때는 **원인을 추적할 단서가
없다.** 결합을 없애는 쪽이 맞다.

**기각 2 — E/F 를 별도 시트로 분리**
가장 깨끗하지만 `app.py` 의 읽기·쓰기 두 함수와 시트 하나가 추가된다.
지금 문제를 푸는 데 필요한 최소 변경을 넘어선다.

**채택 — 키 재부착 + 전체 쓰기**
쓰기 폭이 4 → 6 칸으로 늘지만 시트 1회 업데이트는 그대로다. `_NPF` 가
`_PFSTATE_COLS` 에서 파생되므로 G열 확장이 자동으로 따라온다.

---

## 검증

`diag_pfstate_align.py` — `eval_portfolio_eod` **실제 함수**를 가짜 gspread 위에서
실행한다(로직 복사 없음). 불변식: 쓰기 후 모든 행의 `(Key, E, F)` 짝이 쓰기
전과 같아야 한다.

| 케이스 | 내용 |
|---|---|
| A-1 | 순서 유지 |
| A-2 | 앞 행 삭제 (핵심 결함 재현) |
| A-3 | 순서 역전 |
| A-4 | 중간 삽입 — 신규 종목은 빈 E/F |
| A-5 | nodata 경로 |
| A-6 | E/F 미설정 종목이 값 있는 종목을 오염시키지 않음 |
| B-1 | 양성 대조 — 낡은 A:D 쓰기에서 오염이 실제로 발생함을 확인 |

**패치 후: 7/7 통과.**

**역검증 (필수) — 패치 전 코드에 동일 스위트:**

    통과 2 / 실패 5
      ❌ A-2  기대 PNC=(240,300) / 실제 PNC=(130,200)
      ❌ A-3  MRNA 와 PNC 의 손절가가 서로 맞바뀜
      ❌ A-4  SILJ 가 PNC 의 값을 가져감
      ❌ A-5  nodata 경로에서도 동일하게 어긋남
      ❌ A-6  PNC 가 빈 값으로 덮임

실패 5건이 예측된 결함 양상과 정확히 일치한다. 스위트가 거짓 통과가 아님이
확인됐다. A-1(순서 불변)이 패치 전에도 통과하는 것은 정상이다 — 순서가
안 바뀌면 위치 결합이 드러나지 않는다.

**기타:** `py_compile` 통과 · `check_py311.py` 통과 · 패치 앵커 5/5
`count == 1` 검증.

---

## 배포

**lockstep 대상 없음** — `run_watchlist_alerts.py` 단일 파일.
`app.py` 는 이미 키로 읽고 있어 변경이 필요 없다. Streamlit 재부팅 불필요
(automation 전용 파일).

    1) run_watchlist_alerts.py   1863 → 1899 줄

※ `check_freshness.py` 기준 줄 수. GitHub 표시는 1 적은 1898.

**적용 방법:** 전체 파일을 덮어쓰지 않는다. `patch_p0_pfstate_align.py` 를
저장소 루트에서 실행한다. 앵커가 하나도 안 맞으면 `AssertionError` 로 즉시
멈추므로, 사본이 낡았을 경우 조용히 잘못된 파일을 만들지 않는다.

    python3 patch_p0_pfstate_align.py run_watchlist_alerts.py

---

## 남은 한계 · 후속

- **경합이 완전히 사라지지는 않았다.** 재조회와 쓰기 사이 약 1초 창이 남는다.
  5PM 실행 중 그 순간에 손절가를 저장해야 겹치므로 실질 위험은 무시할 수준이다.
  완전 제거는 시트 분리(기각 2)가 필요하다.
- **과거에 이미 오염된 값은 복구되지 않는다.** 이 수정은 앞으로의 오염만 막는다.
  배포 후 `Portfolio_Alert_State` 의 E/F 를 눈으로 한 번 검수해야 한다.
- **G열(`Swing_Weight_Pct`)은 이 커밋에 포함되지 않는다.** 후속 B안에서
  `_PFSTATE_COLS` 에 한 줄 추가하면 `_NPF` 파생 로직이 자동으로 따라온다.
