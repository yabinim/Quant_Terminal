fix(calendar): 휴장일 하드코딩 5중 중복 제거 — calendar_core SSOT 신설

## 🔴 고치는 문제 — 2027-01-01 확정 발생

휴장일 상수가 **5개 자동화 파일에 중복**돼 있었고, 전부 `2026-12-25` 에서 끝난다.

| 파일 | 위치 |
|---|---|
| `run_watchlist_alerts.py` | 165 `_NYSE_HOLIDAYS` |
| `run_drg_predict.py` | 58/63/68 `_2025 \| _2026` |
| `run_drg_verify.py` | 54/59/64 `_2025 \| _2026` |
| `run_earnings_watch.py` | 85 `_NYSE_HOLIDAYS` |
| `run_narrative.py` | 57/62/64 `_NYSE_FIXED_*` |

⚠️ 직전 보고에서 **3개 파일이라고 했으나 실제로는 5개**였다. 두 개를 빠뜨렸다.

2027-01-01 부터 다섯 곳 전부 **모든 휴장일을 거래일로 오판**한다. 결과는 조용하다.

- 알림 상태머신의 2일 확정 카운터가 헛돌아 **진행**된다 (휴장일에 진행시키지 않으려고 멈추는 설계인데 그 전제가 깨진다)
- DRG 가 존재하지 않는 종가를 검증하려 든다
- 로그에 에러가 남지 않는다

## 설계 — 왜 규칙 계산이 판정 경로인가

프로브에서 `holidays-by-exchange` 는 정상으로 확인됐다(2027년 10건, `isClosed` +
`adjOpenTime`/`adjCloseTime`). 그런데 **판정 경로에 넣지 않았다.**

다섯 파일 전부 개장 여부 가드가 **시트를 열기 전 `main()` 최상단**에 있다.

    run_watchlist_alerts.py:1121   if not is_market_open_today() and args.scope != "metrics":
    run_drg_predict.py:903, 1154   if not is_market_open_today():
    run_drg_verify.py:447          if not is_market_open_today():
    run_earnings_watch.py:1042     if not is_market_open_today():
    run_narrative.py:853           market_day = is_market_open_today()

여기에 시트나 네트워크를 붙이면 **"휴장일이라 즉시 종료"하던 실행에까지 왕복이
생긴다.** 로딩 시간을 늘리지 않는 것이 이번 작업의 제약이었으므로 비용 순으로
판정한다.

    1) 주말                 → 휴장   [FMP 0 · 시트 0]
    2) 규칙 계산 정규 휴일  → 휴장   [FMP 0 · 시트 0]   ← 연 10일
    3) 그 외                → 개장   [FMP 0 · 시트 0]   ← 나머지 250일

**핫 패스 비용이 기존 집합 조회와 동일한 0 이다.** `app.py` 는 이번에 건드리지
않으므로 앱 로딩 시간 영향도 정확히 0 이다.

판정 불가 시 **개장(True)** 으로 폴백한다. 알림을 통째로 놓치는 것보다 헛도는
쪽이 덜 위험하다는 판단이다.

## 그럼 FMP 는 어디에 쓰나 — 규칙으로 못 잡는 것

**임시 휴장.** 대통령 국장일에 NYSE 는 하루 닫는다.

    2025-01-09  카터 전 대통령 국장일   ← 기존 하드코딩 5벌 어디에도 없다
    2018-12-05  부시 전 대통령 국장일

**즉 2025-01-09 에 이 시스템의 자동화는 전부 헛돌았다.** 이미 일어난 사고다.
규칙 계산으로도 잡을 수 없다. API 가 알려주는 수밖에 없다.

그래서 FMP 조회를 **주 1회 갱신 잡**으로 분리했다. 판정은 규칙이 하고, 갱신 잡은
규칙이 놓친 것만 찾는다.

## 변경 내역

### `calendar_core.py` (신규, 394줄 · repo root)

시장 캘린더 SSOT. `reminders_core.py` 패턴을 따라 **모듈 레벨에서 gspread 를
import 하지 않는다.** `requests` 도 함수 안에서 지연 import 한다 — 나중에
`app.py` 가 흡수해도 임포트 비용이 붙지 않게.

| 함수 | 용도 |
|---|---|
| `nyse_regular_holidays(year)` | 정규 휴일 10개 규칙 산출 |
| `is_market_open(d, extra_closed)` / `is_market_open_today()` | 판정 진입점 |
| `easter_sunday(year)` | 익명 그레고리력 — 굿프라이데이 기준 |
| `holiday_name` / `next_trading_day` / `prev_trading_day` | 부수 유틸 |
| `parse_calendar_values(values)` | 시트 파싱 (순수 함수, gspread 무관) |
| `rows_from_fmp` / `diff_against_rules` | FMP 응답 → 시트 행 / 규칙 대조 |
| `fetch_calendar_fmp` | FMP 조회 (갱신 잡 전용) |

`_RULE_CACHE` 프로세스 캐시 — `run_drg_predict` 는 903 과 1154 두 곳에서 묻는다.
연도당 한 번만 계산한다.

### 자동화 5개 — 호출부 무변경

각 파일에서 상수+함수 정의만 교체했다. **`is_market_open_today()` 라는 지역 함수
이름을 그대로 유지**해서 호출부 6곳은 한 줄도 건드리지 않았다(회귀 위험 최소화).

| 파일 | 줄 수 |
|---|---|
| `run_watchlist_alerts.py` | 1209 → **1206** |
| `run_drg_predict.py` | 1249 → **1243** |
| `run_drg_verify.py` | 518 → **512** |
| `run_earnings_watch.py` | 1197 → **1196** |
| `run_narrative.py` | 933 → **921** |

### `automation/refresh_market_calendar.py` (신규, 227줄)

주 1회: FMP 조회 → `Market_Calendar` 시트 저장 → 규칙 대조 → 불일치 시 `Reminders` 항목 생성.

- `[CALENDAR-ALERT]` — 규칙에 없는 휴장일 = **임시 휴장 후보**
- `[CALENDAR-MISSING]` — 규칙에는 있는데 FMP 응답에 없음
- `[CALENDAR-OK]` — 일치

반일장(`adjCloseTime`)은 **저장만 하고 판정에 쓰지 않는다.** 2PM 워크플로 가드는
별건으로 분리했다(설계 확인 시 "(가) 데이터만" 선택).

`Market_Calendar` 시트: `Date, Exchange, Name, Is_Closed, Adj_Open, Adj_Close, Source, Updated_At`

### `.github/workflows/market_5pm_weekend.yml` (128 → 145줄)

`🗓️ 시장 캘린더 갱신 + 규칙 대조` 스텝 추가. `if: always()` +
`continue-on-error: true` — 이 스텝이 실패해도 판정 경로에 영향이 없으므로
워크플로를 실패시키지 않는다.

### `automation/diag_market_calendar.py` (신규, 290줄) + `.yml` (62줄)

회귀 검증 53항목. 네트워크·시트 접근 없음.

## 다중 사용자 3결정

1. **데이터 소유** — 시장 캘린더는 **전역 공유**. 관리자 소유 1벌을 모두가 쓴다. 사용자별 소유 개념 없음
2. **이메일 라우팅** — 해당 없음 (메일을 보내지 않는다)
3. **토글** — **추가하지 않는다.** 시장 개폐는 개인이 켜고 끌 성질이 아니다. Users 스키마 변경 없음 → v5 마이그레이션 불필요

임시 휴장 발견 시에는 `Reminders` 를 쓴다. Reminders 는 설계상 관리자 전용
개발 로드맵이고, 시장 캘린더 이상은 투자 알림이 아니라 시스템 점검 항목이므로
성격이 정확히 맞는다. 새 토글 없이 기존 배관을 재사용한다.

## 검증

### 골든 재현 — 이게 통과해야 교체가 안전하다

교체 전 5개 파일에 하드코딩돼 있던 **2025·2026 휴장일 20개를 규칙 계산이 완전히
재현**한다.

    2025  10/10 일치
    2026  10/10 일치   (7/04 토요일 → 7/03 관측 포함)

### 다년 산출 (교차 확인)

    2027: 10일   ← 프로브 실측 "10건"과 일치
    2028:  9일   ← 신정이 토요일이라 관측 안 함 (NYSE 규칙)
    2029: 10일 · 2030: 10일

### 🔴 뮤테이션 테스트가 실제 버그를 잡았다

M3(부활절 +1일)을 처음에 **검출하지 못했다.** 원인을 추적한 결과 테스트가 아니라
**코드 결함**이었다.

    부활절 4/20(일) +1 → 굿프라이데이 4/19(토)
    → _observed 가 토요일→전날 금요일 보정 → 4/18
    → 틀린 계산이 정답으로 되돌아옴 ✗

굿프라이데이는 정의상 항상 금요일이라 대체 휴일 규칙이 걸릴 일이 없는데, 그래도
적용해 두면 **부활절 계산 오류를 조용히 덮어 버린다.** `_observed` 적용을 제거하고
금요일 여부를 확인하도록 고쳤다. 이후 M3 정상 검출.

전체 뮤테이션 5종 모두 검출:

    M1 n번째 요일 +1주        → 검출 ✅
    M2 메모리얼데이 마지막→첫째 → 검출 ✅
    M3 부활절 +1일             → 검출 ✅ (코드 수정 후)
    M4 대체 휴일 규칙 제거      → 검출 ✅
    M5 extra_closed 계약        → 설계대로 ✅

### 배포 전 개발 중 잡은 결함 3건

| # | 결함 | 발견 방법 |
|---|---|---|
| 1 | `gsr.with_retry` — **존재하지 않는 함수**(실제는 `gsr.call`) | AST 심볼 대조 |
| 2 | 시크릿 이름 `GSPREAD_KEY_JSON` — 실제는 `GSPREAD_KEY` | 타 자동화 대조 |
| 3 | `diag` 가 `automation/` 실행 시 repo root 를 못 찾음 | 배포 레이아웃 시뮬레이션 |

셋 다 그대로 올렸으면 런타임에서야 터졌다.

### 최종

    회귀 검증        53/53 통과 (A 골든 · B 대체휴일 · C 부활절 · D 판정계약
                                · E 시트파싱 · F FMP대조 · G 뮤테이션)
    py_compile       8/8 OK
    check_py311.py   8/8 Python 3.11 호환
    YAML 파싱        market_5pm_weekend(10스텝) · diag_market_calendar(5스텝)
    AST 심볼 대조    cc.* / gsr.* / rc.* 전부 실재 확인
    배포 레이아웃    automation/ 하위 실행 시뮬레이션 통과
    임시휴장 검출    가짜 국장일 주입 → [CALENDAR-ALERT] 정상 발화

## 락스텝 배포 순서

⚠️ **`calendar_core.py` 를 먼저 올려야 한다.** 자동화 5개가 이걸 import 하므로,
순서가 뒤바뀌면 그 사이에 도는 자동화가 `ModuleNotFoundError` 로 죽는다.

    1. calendar_core.py                              (repo root, 394줄)  ★ 최우선
    2. automation/run_watchlist_alerts.py            (1206줄)
    3. automation/run_drg_predict.py                 (1243줄)
    4. automation/run_drg_verify.py                  (512줄)
    5. automation/run_earnings_watch.py              (1196줄)
    6. automation/run_narrative.py                   (921줄)
    7. automation/refresh_market_calendar.py         (227줄)
    8. automation/diag_market_calendar.py            (290줄)
    9. .github/workflows/market_5pm_weekend.yml      (145줄)
   10. .github/workflows/diag_market_calendar.yml    (62줄)

**Streamlit 재부팅 불필요** — `app.py` 를 건드리지 않았다.

⚠️ 7·8 은 반드시 `automation/` 하위에. 루트 중복본이 생기면 Actions 가 낡은 코드를 돈다.

## 남은 한계

- **임시 휴장은 여전히 판정에 반영되지 않는다.** 갱신 잡이 발견해서 `Reminders` 에
  올릴 뿐, `is_market_open_today()` 는 규칙만 본다. `extra_closed` 인자는 이미
  받도록 만들어 뒀으므로, 실제로 임시 휴장이 검출되면 그때 `Market_Calendar`
  시트를 판정에 연결하는 설계를 하면 된다. 지금 미리 연결하면 **매 실행 시트 왕복이
  생겨** 이번 작업의 제약과 충돌한다.
- **반일장은 저장만 한다.** `Adj_Close` 가 시트에 쌓이지만 아무도 읽지 않는다.
  `market_2pm_weekday.yml` 가드는 다음 차수 별건.
- **`app.py:12644` 는 여전히 `weekday()` 만 본다.** 휴장일을 모른다. SSOT 원칙상
  흡수가 맞지만 락스텝이 11파일이 되고 앱 재부팅이 필요해져 다음 차수로 분리했다.
- 자동화에는 **SSOT 버전 스탬프 검사가 없다**(현재 `app.py` 에만 있다).
  `CALENDAR_CORE_VERSION` 을 넣어 뒀으나 확인하는 쪽이 아직 없다.
