fix(calendar): run_earnings_watch 휴장일 하드코딩 제거 + 소비자 배선 래칫 추가

## 배경 — 조용히 남아 있던 마지막 미이관분

`calendar_core.py` 는 **5개 자동화 파일**에 중복된 하드코딩 휴장일 집합을
대체하려고 만들었다. 그 집합은 전부 `2026-12-25` 에서 끝나므로 **2027-01-01
부터 모든 휴장일을 거래일로 오판**한다.

네 개(`run_watchlist_alerts` / `run_drg_predict` / `run_drg_verify` /
`run_narrative`)는 이관됐다. **`run_earnings_watch.py` 하나가 빠졌다.**

```
run_earnings_watch.py:116   _NYSE_HOLIDAYS = { ... "2026-12-25" }
run_earnings_watch.py:124   def is_market_open_today():   # 하드코딩 집합 조회
```

오판의 결과가 조용하다. 휴장일에 실적 레이더가 돌면 존재하지 않는 세션 기준
행이 `Earnings_Events` 에 쌓이고, 그 시트를 공유 상태로 쓰는 워치리스트 진입
차단 게이트가 그 값을 그대로 읽는다. 로그에 에러는 남지 않는다.

## 왜 4개월간 안 잡혔나 — 이 커밋의 진짜 주제

`diag_market_calendar.py` (기존 87검사)는 **calendar_core 자체만** 검사했다.
골든 휴일, 대체휴일 규칙, 부활절, 파싱, FMP 대조, 반일장, 뮤테이션 — 전부
모듈 내부다. **소비자가 그 모듈을 실제로 부르는지 보는 검사가 하나도 없었다.**

게이트가 공허하게 통과한 것이다. 0건 검사는 0건 실패다. 그래서 이 커밋은
결함 수정과 **그 결함을 놓친 구조적 원인** 을 함께 닫는다.

---

## 파일별 변경

### `automation/run_earnings_watch.py`  (1234 → 1242줄)

| 위치 | 변경 |
|---|---|
| 임포트부 | `import calendar_core as cc` 추가 (알파벳 순, `accounts_core` 다음) |
| 116~126 | `_NYSE_HOLIDAYS` 20개 집합 **삭제** |
| `is_market_open_today()` | `return cc.is_market_open_today()` 로 위임 |

나머지 4개 파일과 **동일한 형태**로 맞췄다. `sys.path.insert` 는 이미 있어
추가 배선이 필요 없다.

주석에 재발 금지를 명시했다 — 다시 하드코딩하면 `diag_market_calendar.py`
I군이 배포 게이트에서 막는다는 사실을 코드 옆에 적어 둔다.

**동작 등가성 실측:** 2025-01-01 ~ 2026-12-31 **730일 전수 대조, 판정 차이 0건.**
2027년은 구 코드가 0일, 신 코드가 정상 10일을 낸다.

```
2027 휴장일: 01-01, 01-18, 02-15, 03-26, 05-31,
             06-18, 07-05, 09-06, 11-25, 12-24
```

### `automation/diag_market_calendar.py`  (468 → 798줄)

**[I군] 소비자 배선** 신설. 알파벳 `C` 는 부활절이 이미 쓰고 있어 `I` 로 배정했다.

| 검사 | 내용 |
|---|---|
| I-0 | automation 파일 탐색 13개 (하한 8) — **탐색 실패 시 아래가 전부 공허하게 통과하므로 하한을 못 박는다** |
| I-1 | 게이트 소비자 5개 전원 탐색됨 |
| I-2 ×13 | 탐색된 **전 파일** 하드코딩 휴장일표 0건 (AST) |
| I-3 ×5 | 게이트 소비자 `calendar_core` import 존재 |
| I-4 ×5 | 게이트 소비자 `is_market_open*` **호출** 존재 |
| I-R0~R11 | 역검증 12건 |

두 갈래로 나눈 이유:
- **하드코딩 금지**는 고정 목록이 아니라 **탐색**이라 새 파일도 자동으로 걸린다(래칫)
- **import + 호출**은 게이트 소비자 5개에만 적용 — 새 스크립트가 캘린더를 쓸
  이유가 없을 수도 있으므로 전 파일에 강제하지 않는다

I-3 과 I-4 를 분리한 것이 핵심이다. **import 만 하고 배선을 잊는 것**이 가장
그럴듯한 회귀인데, 그러면 파일은 '이관 완료'처럼 보이면서 게이트는 사라진다.
I-R5 가 두 검사가 실제로 서로 다른 것을 본다는 것을 확인한다.

#### 판정 기준 — 이름이 아니라 형상

초안은 `(파일명, 변수명)` 면제 목록이었다. 두 개의 정당한 날짜 테이블이 걸렸다.

| 테이블 | 날짜 | 휴장일 겹침 | 정체 |
|---|---:|---:|---|
| `_NYSE_HOLIDAYS` (이관 전) | 20 | **20 (100%)** | 휴장일표 |
| `_HARDCODED_CALENDAR_2026` | 32 | 1 (3%) | FOMC·CPI·NFP 발표 일정 |
| `SEEDS` (seed_reminders) | 4 | 0 (0%) | 리마인더 `due=` 날짜 |

면제 목록을 버린 이유는 유지비가 아니다. **`_NYSE_HOLIDAYS` 를 면제된 이름으로
바꾸는 것만으로 래칫을 빠져나가기 때문이다.**

판별자는 따로 있었다 — 휴장일표는 정의상 NYSE 휴장일과 겹친다. 기준을
`겹침 ≥ 3건 AND 겹침비율 ≥ 0.5` 로 잡으면 이름·파일·변수 구조와 무관해지고
면제 목록 자체가 사라진다.

> `_HARDCODED_CALENDAR_2026` 의 겹침 1건은 **2026-04-03** 이다. 굿프라이데이에도
> BLS 는 NFP 를 낸다 — 장은 닫혀도 통계는 나온다.

#### 검사가 스스로 무너지는 경로를 막았다

겹침 판정은 `cc.nyse_regular_holidays` 에 기댄다. 그게 빈 값을 돌려주면 겹침이
0 이 되어 **I-2 가 전부 조용히 통과한다.** `I-R0` 이 그 전제를 명시적으로 고정한다.

---

## 검증 결과

```
py_compile            ✅  2개 파일
check_py311           ✅  2개 파일 Python 3.11 호환
pyflakes 델타          ✅  0  (기존 경고 2건 동일 — numpy 미사용, _single 미사용)
diag_market_calendar  ✅  87 → 124 통과 · 실패 0
diag_halfday_gate     ✅  36/36  (인접 스위트 무영향)
분리배치 재현          ✅  root=calendar_core / automation/=나머지
                          → `python automation/diag_market_calendar.py` 13개 탐색 정상
```

### 역검증 — 미패치 원본에서 실제로 빨간불이 나는가 (필수)

패치 전 `run_earnings_watch.py` 를 그대로 넣고 돌린 결과:

```
❌ I-2  run_earnings_watch.py 하드코딩 휴장일표 0건
        — _NYSE_HOLIDAYS 줄116 (날짜 20개 중 휴장일 20개 일치)
❌ I-3  run_earnings_watch.py calendar_core import  — import 없음
❌ I-4  run_earnings_watch.py is_market_open* 호출  — import 만 있고 배선이 없다

결과: 통과 121 · 실패 3
⚠️ 배포하지 말 것.
```

**이 입력이 곧 회귀 케이스다.** `_BAD_FIXTURE` 로 스위트 안에 박아 두어
결함이 재발하면 파일 없이도 먼저 걸린다.

### 역검증 12건 상세

| ID | 확인 내용 |
|---|---|
| I-R0 | 겹침 판정의 전제 — 2026 휴장일 10개가 실제로 나온다 |
| I-R1/R2 | 이관 전 픽스처 → 날짜표 검출 + import 없음 판정 |
| I-R3/R4 | 이관 후 픽스처 → 0건 (과민하지 않음) + import·호출 인식 |
| I-R5 | import 만 있고 배선 없는 픽스처 → **호출 검사만** 실패 |
| I-R6 | 낱개 날짜 2개는 통과 (문턱 3) |
| I-R7 | dict 형태 휴장일표도 검출 |
| I-R8 | **이름을 바꿔도 휴장일표는 검출** (이름 우회 불가) |
| I-R9 | 경제지표 발표 일정은 통과 |
| I-R10 | 설정 구조에 흩어진 날짜는 통과 (seed_reminders 형태) |
| I-R11 | 휴장일 절반 섞인 표도 검출 (비율 0.5) |

---

## 락스텝 배포 순서

`calendar_core.py` 는 **무변경**이다. 두 파일 모두 `automation/` 이다.

1. `automation/diag_market_calendar.py` — GitHub 표시 **797줄**
2. `automation/run_earnings_watch.py` — GitHub 표시 **1241줄**

순서 이유: **1번을 먼저 올리면 2번을 올리기 전 워크플로가 빨간불(실패 3건)이
된다.** 그게 정상이고, 래칫이 살아 있다는 증거다. 2번을 올린 뒤 초록불이 되면
배선이 실제로 완료된 것이다. 반대 순서로 올리면 이 확인 기회가 없다.

**Streamlit 재부팅 불필요** — app.py 및 root 모듈 무변경, automation 전용.

## 롤백

두 파일을 이전 버전으로 되돌리면 끝. **데이터 손실 없음** — 시트 스키마·쓰기
경로·알림 상태머신 미접촉. FMP 호출량 변화 0.

## 남은 한계 / 후속

- **`_HARDCODED_CALENDAR_2026` 도 2026년으로 끝난다.** FOMC·CPI·NFP 발표 일정이
  `run_drg_predict.py` / `run_narrative.py` 두 곳에 중복돼 있고 2027-01-01 부터
  빈 값이 된다. FRED API 보조가 있어 즉시 침묵하지는 않지만 **calendar_core 와
  똑같은 구조의 문제이고 별건 과제다.** I군이 이걸 통과시키는 것은 의도된 설계다
  (휴장일표가 아니므로).
- `extra_closed` / `half_map` 시트 보강분은 여전히 어느 소비자에도 배선돼 있지
  않다. **의도된 유예다** — 판정 경로에 시트 왕복을 붙이지 않는 것이
  calendar_core 의 설계 제약이고, 임시 휴장 발견 경로는
  `refresh_market_calendar.py` STEP 4 의 리마인더가 덮는다.
- I군은 `automation/` 만 훑는다. `app.py` 는 `import calendar_core as mcal` 로
  이미 배선돼 있고 하드코딩 잔재가 없으나, I군의 관할이 아니다(`diag_startup.py`
  영역).
