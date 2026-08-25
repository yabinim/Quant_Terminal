# fix(alerts): 확정 카운터를 '호출 수'가 아닌 '날짜 수'로 센다

`evaluate_alert_transitions` 의 pending 카운터에 날짜 가드를 넣어, 5PM 워크플로가
하루에 두 번 돌아도 2일 확정이 하루 만에 통과하지 않도록 한다.

---

## 배경

`regime_core.py:1019` 독스트링:

    ※ 하루 1회 호출 전제(자동화). 호출 1회 = 평가 1회로 pending 카운터가 1 진행된다.

**전제일 뿐 강제되지 않았다.** 카운터는 호출 횟수를 셌다:

    if status == "armed":
        pending += 1
        if pending >= confirm_days:
            fired.append(...)

같은 날 두 번 실행되면(수동 재실행 · 재시도 · repository_dispatch 중복)
2일 확정이 하루 만에 통과해 **알림이 조기 발동**한다. 2일 확정은 하루짜리
노이즈를 걸러내려고 넣은 장치인데, 그 장치가 무력화된다.

이 위험 때문에 A-2c 선제 상장상태 점검을 `force_liveness` 로 강제 실행하지
못하고 금요일 자연 실행을 기다려야 했다.

---

## 변경 내역 — `regime_core.py` 단독

카운터를 **호출 수가 아니라 날짜 수**로 센다. 이벤트별로 마지막으로 카운터를
올린 날짜(`pday`)를 state 에 저장하고, 같은 `today_str` 로 다시 불리면 올리지
않는다.

    if status == "armed":
        if (not today_str) or (pday != today_str):
            pending += 1
            pday = today_str
        if pending >= confirm_days:
            ...
            status, pending, pday = "fired", 0, ""

레짐 전환 카운터에도 동일한 가드를 넣었다. 단, **후보(cand)가 바뀐 것은 같은
날이라도 새 사건**이므로 그때는 `pending=1` 로 새로 시작한다.

### 왜 '통째 스킵'이 아닌가 (기각한 대안)

가장 단순한 구현은 함수 진입부에서 `state["ts"] == today_str` 이면 즉시
반환하는 것이다. **이건 안 된다.** 1차 실행이 데이터 오류나 FMP 장애로 실패했을
때, 재실행이 아무것도 복구하지 못한다. 재시도가 무의미해진다.

카운터만 막고 평가 자체는 그대로 돈다 — 악화 감지(`new_keys`), 재무장,
발동, `entry_invalid` 는 전부 정상 동작한다. 회귀 A-6 이 이 성질을 지킨다.

### 하위호환

- `today_str` 이 비면 종전대로 매번 진행한다 → `run_signal_backtest` 와
  기존 테스트 호출부 무영향.
- `pday` 가 없는 구버전 state 는 `""` 로 읽혀 `pday != today_str` 이 참이 되므로
  **'오늘 아직 안 올림'** 으로 취급된다 → 배포 직후 첫 실행이 정상 진행되고,
  보유 종목이 한꺼번에 멈추거나 몰려서 발동하는 일이 없다.
- 비활성 이벤트 리셋 · 재무장 경로에도 `pday: ""` 를 넣어 잔여 상태가 남지 않게 했다.

---

## 곁다리 — `diag_market_gate.py` 영구 실패 해소

회귀 확인 중 이 스위트가 **패치 전에도** 실패하고 있었음을 발견했다:

    ❌ 매니페스트 모듈명이 모두 알려진 공용 모듈 — 미상 ['industry_core', 'reminders_core']

`_TARGETS` 허용목록이 낡아 나중에 추가된 공용 모듈을 모른다. 상태머신 검증
자체(4·5절)는 통과하고 있었으므로 이 커밋의 변경과 무관한 기존 부채다.

**영구 빨간불은 아무도 읽지 않는 스위트가 된다.** "항상 통과하는 스위트는
판별력이 없다" 의 반대편이다. `industry_core` · `reminders_core` ·
`calendar_core` · `accounts_core` · `gemini_core` 를 추가하고, 새 공용 모듈을
만들면 여기도 갱신하라는 경고 주석을 달았다.

---

## 검증

`diag_alert_dateguard.py` — `evaluate_alert_transitions` **실제 함수**를 호출한다
(로직 복사 없음). **15/15 통과.**

| 군 | 내용 |
|---|---|
| A-1~3 | 같은 날 5회 → 발동 0 · pending 1 · pday 기록 |
| A-4~5 | 이틀 → 발동 1 · 하루 3회씩 이틀 → 발동 1 (조기도 소실도 아님) |
| A-6 | 발동 후 같은 날 재호출 → 중복 발동 0 (통째 스킵이 아님을 확인) |
| A-7 | `today_str` 빈 값 → 종전 동작 |
| A-8 | `pday` 없는 구버전 state → 정상 진행 |
| A-9 | 날짜별 1회 10일 → 백테스트 패턴 무영향 |
| B-0~2 | 레짐 전환 카운터도 동일 가드 |
| C-1 | 양성 대조 |

**역검증 (필수) — 가드 이전 코드에 동일 스위트:**

    통과 10 / 실패 5
      ❌ A-1 같은 날 5회 → 발동 0    기대: 0   실제: 1
      ❌ A-2 같은 날 5회 → pending 1  기대: 1   실제: 5
      ❌ A-3 pday 가 그날로 기록됨
      ❌ B-1 레짐: 같은 날 5회 → 발동 0
      ❌ B-2 레짐: 다음 날 → 발동 1

**같은 날 5번 돌리면 실제로 알림이 발동했다.** 가설이 아니라 실측이다.

**기존 스위트 회귀:** `diag_trim_size` 52/52 · `diag_pfstate_align` 7/7 ·
`diag_market_gate` 전체 통과(위 수정 후) · `check_py311` 3개 파일 통과 ·
`check_freshness` 정합성 ✅

---

## 배포

    1) regime_core.py                        2393 → 2415
    2) automation/diag_alert_dateguard.py    신규
    3) .github/workflows/diag_alert_dateguard.yml  신규
    4) automation/diag_market_gate.py         490 → 491

    → Streamlit 재부팅

lockstep 대상 없음(`regime_core.py` 단독 변경, 시그니처 불변). 1·4 는 서로
무관하므로 순서는 상관없다. 시트 스키마 변경 없음.

**롤백:** 4개 파일을 직전 커밋으로 되돌린다. state JSON 의 `pday` 필드는 남지만
낡은 코드는 그 키를 읽지 않으므로 무해하다.

---

## 남은 한계

- 가드는 `today_str` 문자열 동일성으로만 판단한다. 호출부가 날짜를 잘못 넘기면
  (예: 항상 같은 값) 카운터가 영영 안 오른다. 현재 호출부 3곳
  (`run_watchlist_alerts` ×2, `run_signal_backtest`) 은 모두 올바른 날짜를 넘긴다.
- 하루 경계는 `today_str` 을 만드는 쪽(ET 기준)에 위임한다. 이 함수는 시간대를
  알지 못한다.
