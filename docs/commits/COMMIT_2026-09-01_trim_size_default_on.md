feat(trim): 매도 규모 권고를 기본 켜짐으로 전환 + 기본 분할 0:100 · 축소폭 33%

## 배경

보유 종목 알림에 "🟡 줄이기" 판정이 떠도 **얼마를 팔아야 하는지**가 메일에
나오지 않았다. 원인은 기능 자체의 부재가 아니라 **옵트인**이었다.

`rc.trim_size_plan()` 의 첫 관문이 다음과 같았다.

```python
w = resolve_swing_weight(swing_weight_pct, None)
if w is None:
    return out          # enabled=False → 렌더러가 블록을 통째로 생략
```

`accounts_core.DEFAULTS["Swing_Weight_Pct"] = None` 이므로 계좌 프로필에서
스윙 몫을 한 번도 저장하지 않은 계좌는 판정 라벨만 받고 수량은 침묵했다.
2026-08-28 발송 메일(MRNA / Fidelity Roth IRA)에서 실제로 확인된 증상이다.

## 설계 판단 — 왜 기본값만 바꾸지 않았나

`Swing_Weight_Pct` 는 세 가지 일을 겸하고 있었다.

1. 매도 규모 표시 on/off 스위치 (`None` = 끔)
2. 스윙 : 포지션 몫 분할 비율
3. `default_events_for_weight()` 의 입력 — **Alert_States 미설정 종목의
   기본 알림 이벤트 파생**

`DEFAULTS` 를 `None → 0` 으로만 바꾸면 3번이 함께 움직인다. 미설정 종목의
기본 이벤트가 `exit,risk`(스윙) → `pexit,ptrim`(포지션)으로 갈아엎어져
**게스트 포함 전 사용자가 받던 스윙 알림이 조용히 사라진다.** 알림이
늘어나는 것보다 사라지는 쪽이 훨씬 위험하다(손실 방지 철학 정면 위배).

따라서 **표시 스위치를 별도 컬럼으로 분리**하고, 이벤트 파생 경로
(`default_events_for_weight`)는 **한 줄도 건드리지 않았다.**

## 파일별 변경

### 1. `regime_core.py` (2,727 → 2,728줄 · GitHub 표시 기준)

- `TRIM_RATIO_DEFAULT_PCT` **50.0 → 33.0**
  - 3회 분할 축소를 기본으로 본다. 1회에 절반은 과하다.
  - ⚠️ 이 상수는 **Account_Profile 에 저장된 적 없는 계좌에만** 적용된다.
    이미 저장된 계좌의 시트값(50)은 그대로 남는다. 아래 배포 후 절차 참조.
- `TRIM_SWING_WEIGHT_FALLBACK_PCT = 0.0` 신설
  - 몫 미설정 계좌의 기본 분할 = 스윙 0% / 포지션 100%.
  - "단기 흔들림에는 팔지 않는다"가 기본 태도.
  - ⚠️ 주석으로 명시: 이 상수를 `default_events_for_weight()` 에 넘기면
    안 된다. 넘기는 순간 알림 이벤트가 바뀐다.
- `resolve_trim_size_show(raw, default=True)` 신설
  - 빈칸·미인식 값 → **켜짐**. 끄기는 명시적 `N` 계열만.
  - 근거: 이 열이 없던 시절의 행이 자동으로 꺼지면 안 된다. 또한 뜻밖의
    값 하나로 매도 규모가 통째로 사라지는 쪽이 잘못 표시되는 것보다 나쁘다.
- `trim_size_plan()` 시그니처에 `show: bool = True` 추가
  - `show=False` → 즉시 `enabled=False` 반환(유일한 끄기 경로)
  - `w is None` → fallback 적용 + `assumed=True` 로 고지
  - 반환 dict 에 `assumed`, `muted` 키 추가
- **`muted` 도입 — 거짓말 문구 제거**
  - 종전: 몫이 0이라 억제된 경우와 애초에 매도 신호가 없는 경우가 모두
    `"권장 매도 없음 — 해당 호흡에 매도 신호 없음"` 으로 표시됐다.
    "🟡 줄이기" 바로 밑에 "매도 신호 없음"이 붙는 **거짓말**이었다.
  - 변경: 신호가 났는데 그 호흡 몫이 0%면
    `"권장 매도 없음 — 스윙 몫 0% (참고용 신호)"` + 설정 안내 note.
    `muted=True` 로 렌더러가 두 경우를 구분한다.
- 수량이 나온 경우에도 `assumed` 면 note 에 기본 분할 사용을 고지한다.

### 2. `accounts_core.py` (293 → 309줄)

- `COLS` 맨 뒤에 `"Trim_Size_Show"` append (NCOL 14 → 15, 마지막 열 N → O)
  - 맨 뒤 append 이므로 `Updated_At` 인덱스 9 하드코딩을 깨지 않는다.
- `DEFAULTS["Trim_Size_Show"] = True`
- `_coerce_row()` — `rc.resolve_trim_size_show()` 위임(파싱 규칙 SSOT는 rc)
- `to_row()` — `Y` / `N` 명시 기록
  - 켜짐도 `Y` 를 쓴다. 빈칸으로 두면 사람이 시트를 볼 때 '미설정'과
    '켜기로 선택함'이 구분되지 않는다.
- 하위호환 검증: 14칸 옛 행 → 패딩 `""` → `Trim_Size_Show=True`

### 3. `app.py` (28,038 → 28,039줄)

- 계좌 프로필 폼의 트랜치 블록: **체크박스를 두 개로 분리**
  - `매도 권장 수량 표시` → `Trim_Size_Show` (기본 켜짐)
  - `스윙/포지션 몫 직접 지정` → `Swing_Weight_Pct` 저장 여부
  - ⚠️ 하나로 합치면 "수량을 보고 싶다"는 이유만으로 `Swing_Weight_Pct` 에
    값이 써지고, 그 순간 미설정 종목의 알림 이벤트가 바뀐다. 분리가 목적.
  - 몫 미지정 시 캡션으로 명시: 알림 이벤트 기본 해석은 종전 그대로
    (`exit,risk`)이고, 스윙 몫 0%라 스윙 신호에는 수량이 나오지 않는다.
- `save_account_profile()` 호출에 `"Trim_Size_Show": bool(_v_swshow)` 추가
- 매도 레이더 렌더의 `rc.trim_size_plan()` 호출에 `show=` 전달
- **렌더 분기 버그 수정**: 종전 분기는 `blocked / full_exit / qty>0` 세 개뿐이라
  `muted`(수량 0)는 어느 분기에도 걸리지 않아 label 이 통째로 사라졌다.
  `elif muted → st.caption` 추가. 반대로 '매도 신호 없음'(muted=False)은
  보유 전 종목에 뜨는 소음이므로 계속 숨긴다(`_shown` 플래그).

### 4. `run_watchlist_alerts.py` (2,132 → 2,140줄)

- 보유 평가 루프의 `rc.trim_size_plan()` 호출에
  `show=bool(_prof.get("Trim_Size_Show", True))` 전달
  - `_profile_for()` 가 accounts_core 프로필 dict 를 그대로 돌려주므로
    스키마 추가만으로 값이 따라온다.
- `_render_hit_card()` 의 ✂️ 매도 규모 블록: `muted` 를 표시 조건에 포함하고,
  `muted`/`blocked` 는 회색(#6b7280)으로 렌더. 수량 0 + muted=False(진짜
  신호 없음)는 발동 카드에서 숨긴다 — 다른 호흡의 판정을 부정하는 것처럼
  읽히기 때문이다.

### 5. `diag_trim_size.py` (184 → 253줄 · 76 케이스)

- **계약 반전 명시**: A-1/A-2 가 잠그던 `None → enabled=False` 를 뒤집었다.
  대신 끄기 경로를 새로 잠근다.
  - A-3a/A-3b: `show=False → enabled=False`, 수량 0
  - A-3c: `TRIM_SWING_WEIGHT_FALLBACK_PCT == 0.0`
  - A-3d: 미설정 수량 == 명시 0 수량 (fallback 이 실제로 0 인지 판별)
- B-0: 기본 트림폭 33.0 고정(제품 결정이므로 값 자체를 잠근다)
- F-6/F-7: 음수 인덱스 → `ac.COLS.index(...)` 로 교체.
  열이 하나 늘 때마다 조용히 다른 칸을 검사하게 되는 함정이었다.
- F-8: `ac.NCOL` 14 → 15
- **H군 신설(16 케이스)**: `Trim_Size_Show` 파싱 왕복 + `muted` 문구
  - H-2: 열 자체가 없는 옛 행 → 켜짐(하위호환)
  - H-5: 알 수 없는 값 → 켜짐
  - H-10: muted 라벨에 "매도 신호 없음"이 들어가면 실패(거짓말 판별자)
  - H-13/H-14: 진짜 신호 없음은 muted=False 이고 종전 문구 유지
- G군 양성대조 2건 추가(G-2 fallback 경로, G-3 show 게이트)

## 검증 결과

| 항목 | 결과 |
|---|---|
| `py_compile` | 5개 파일 전부 통과 |
| `pyflakes` 델타 | 0 (regime 2→2 · accounts 0→0 · app 34→34 · alerts 2→2 · diag 0→0) |
| `diag_trim_size.py` | **76 / 76 통과** |
| 변이 테스트 | **13개 변이 전부 검출 · 생존 0 · 무효 0** |
| `diag_sell_verdict.py` | 순수 18 케이스 통과(이후 블록은 FMP 키 부재로 환경 실패) |
| `diag_pfstate_align.py` | 7 / 7 통과 |
| 하위호환 | 14칸 옛 행 → `Trim_Size_Show=True` 확인 |

### 변이 테스트 상세 (py_compile 선검증 포함)

| 변이 | 검출 |
|---|---|
| M1 show 게이트 제거 | A-3a, A-3b, G-3 |
| M2 fallback 을 50 으로 | A-3c, A-3d |
| M3 미설정을 옛 계약(끔)으로 되돌림 | A-1, A-1b, A-2, A-3d |
| M4 트림폭 기본 50 회귀 | B-0 |
| M5 빈칸을 꺼짐으로 읽음 | H-1, H-2, H-8 |
| M6 미인식 값을 꺼짐으로 | H-5 |
| M7 muted 플래그 미설정 | H-9, H-15 |
| M8 muted 를 옛 문구로 통합 | H-10, H-12 |
| M9 muted 판정에서 trim 누락 | H-9, H-10, H-12 |
| M10 Trim_Size_Show 파싱 누락 | H-3, H-6 |
| M11 to_row 에서 열 누락 | IndexError (하드 실패) |
| M12 COLS 에서 신규 열 제거 | KeyError (하드 실패) |
| M13 assumed 항상 False | A-1b |

M11/M12 는 테스트 실패가 아니라 예외 종료로 잡혔다. 하네스 고장이 아니라
실제 결함(시트 행 길이 불일치 / 스키마 붕괴)임을 개별 재현으로 확인했다.

### 역검증 — 실제 메일 렌더 시뮬레이션

`_render_hit_card()` 를 직접 호출해 4개 시나리오를 확인했다.

| 시나리오 | 결과 |
|---|---|
| ① MRNA 양쪽 줄이기 (100주 @ $28, 신규 계좌) | `권장 매도 33주 · 보유의 33% (약 $924) — 포지션 몫의 33%` + 기본 분할 고지 |
| ② 스윙만 줄이기 (포지션 보유) | `권장 매도 없음 — 스윙 몫 0% (참고용 신호)` + 설정 안내 |
| ③ 계좌에서 표시 끔 | ✂️ 블록 **미노출** |
| ④ 진짜 신호 없음(양쪽 보유) | ✂️ 블록 **미노출**(소음 억제) |
| ⑤ 명시 50:50 · 스윙청산+포지션줄이기 | `66.5주 · 보유의 66% — 스윙 몫 전량 + 포지션 몫의 33%` |

## Lockstep 배포 순서

스키마 변경(NCOL 14 → 15)이 포함되므로 **부분 배포 금지**.
`accounts_core.py` 만 먼저 올리면 `to_row()` 가 15칸을 반환하는데 `app.py` 의
`_ACCT_PROF_LAST_COL` 은 여전히 `N` 이라 마지막 열이 잘려 나간다.

1. `regime_core.py` (repo root)
2. `accounts_core.py` (repo root)
3. `run_watchlist_alerts.py` (`automation/`)
4. `app.py` (repo root)
5. `diag_trim_size.py` (`automation/`)
6. Streamlit 리부트 (**필수** — 스키마 폭이 바뀌었다)

## 남은 한계

- **저장된 계좌의 축소폭은 자동으로 33% 가 되지 않는다.** 상수 변경은
  미저장 계좌에만 적용된다. 이미 `Trim_Ratio_Pct=50` 이 시트에 있는 계좌는
  프로필 폼에서 한 번 저장해야 한다.
- 트랜치 실행 추적 미구현 — 중간 비율(0<w<100)에서 한쪽 몫만 먼저 판 뒤에도
  잔여가 설정 비율대로 남아 있다고 가정한다(종전과 동일, note 로 고지).
- 종목 단위 끄기는 없다. 끄기는 계좌 단위(`Trim_Size_Show`)뿐이다.
  종목 단위 몫 오버라이드(`Portfolio_Alert_State` G열)는 종전대로 동작한다.
- 기본 분할이 스윙 0% 이므로, `Alert_States` 가 기본값(`exit,risk`)인 종목은
  포지션 판정이 독립적으로 줄이기/청산일 때만 수량이 나온다. 스윙 신호에도
  수량을 받으려면 계좌 프로필에서 스윙 몫을 0 초과로 지정해야 한다.
