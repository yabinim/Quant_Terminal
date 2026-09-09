# feat(calendar): 세션 구간 SSOT 추출 — 반일장·휴장을 라벨 판정에 반영

`calendar_core` 에 `session_phase` / `narrative_session_label` 을 추가하고,
app.py · run_narrative.py 에 복사돼 있던 밴드 표를 제거한다. DRG 프롬프트의
`market_session` 이 마감 시각을 캘린더에서 받도록 바꾸고, 반일장 규칙 이탈을
Reminders 로 배달한다.

---

## 왜

반일장 2PM 가드(`run_watchlist_alerts._intraday_close_passed`)와 앱 시장 상태
헤더(`get_market_status`)는 이미 v1.1.0 의 반일장 규칙을 쓰고 있었다. 그런데
**세션 라벨 세 곳은 그걸 안 봤다.**

    app.py:_narrative_session_label_for_et_dt   240/569/570/960/961/1200 하드코딩
    run_narrative.py:_session_label_for_utc     동일 표가 **복사**돼 있음
    app.py DRG 프롬프트 market_session          et_hour < 16 하드코딩

결과:

| 시각 | 실제 | 표시/저장 |
|---|---|---|
| 2026-11-27 13:30 (반일장) | 마감 후 | 🟢 Market Hours Analysis |
| 2026-11-26 12:00 (추수감사절) | 휴장 | 🟢 Market Hours Analysis |
| 2026-11-27 14:00 (반일장) | 마감 후 | Gemini 프롬프트에 "(장 중)" |

셋 다 **숫자는 맞고 라벨만 틀린다.** 예외도 에러 로그도 없다.

두 라벨 함수는 `run_narrative` 가 Narratives 시트에 저장하고 app.py 가 그 시트를
읽어 표시하는 관계라 **반드시 같은 문자열**이어야 하는데, 검증은 docstring 의
"app.py와 동일한 세션 라벨" 이라는 선언뿐이었다.

---

## 파일별 변경

### `calendar_core.py` v1.1.0 → v1.2.0 (551 → 669)

`session_phase(d_et, extra_closed=None, half_map=None)` 추가. 반환은
`unknown` / `closed` / `pre` / `open` / `post` / `overnight` 6종.
`session_close_time` 을 재사용하므로 **FMP·시트 접근 0** — 핫 패스 불변식 유지.

`narrative_session_label()` 과 라벨 표 `NARRATIVE_SESSION_LABELS` 를 함께 둔다.
표시 문자열을 캘린더 모듈에 두는 건 냄새가 나지만, 밴드만 올리고 매핑을 남기면
복사본이 그대로 남는다. 문자열까지 올려야 드리프트가 구조적으로 불가능해진다.

설계 판단 3가지를 주석으로 고정했다.

- **내림차순 판정.** 마감 기준을 먼저 본다. 오름차순이면 `half_map` 이 이상한
  값(`Adj_Close="05:00"`)을 주입했을 때 이미 닫힌 장을 `pre` 로 부른다.
  시트 값은 사람이 손댈 수 있으므로 이 방어가 필요하다.
- **경계는 `get_market_status` 에 맞춘다.** 마감 정각은 `open` 이 아니라 `post`,
  마감+4h 정각은 `post` 가 아니라 `overnight`.
- **`d_et` 에 기본값을 주지 않는다.** 이 모듈의 다른 함수는 `d=None` 을 "오늘"로
  읽지만 `session_phase` 는 **시각**을 본다. 앱은 임의 시각에 rerun 되고 자동화는
  고정 시각에 돌아서, 두 호출부가 서로 다른 기본값을 기대하면 조용히 갈린다.
  `None` 은 "모름"이다. (`fmp_extras` 의 `bars=` 와 같은 이유)

판정 불가는 `open` 이 아니라 `unknown` 이다. `is_market_open` 계열은 개장으로
fail-open 하지만(알림을 놓치는 것보다 헛도는 쪽이 낫다), 이 함수의 산출물은
사람이 읽는 라벨이라 판단 근거가 아니다. 모를 때 "장중"이라고 우기는 것보다
모른다고 말하는 쪽이 정직하다.

### `app.py` (28570 → 28589)

- `_narrative_session_label_for_et_dt` (13362) → `mcal.narrative_session_label`
  위임. 밴드 리터럴 제거.
- DRG 프롬프트 `market_session` (17199~) → `session_close_time` + `close_minutes`
  기반 4분기. 휴장 분기 신설.
- `get_market_status` (13294)는 **손대지 않았다** — 이미 반일장 처리 완료.

`market_session` 에 `session_phase` 를 쓰지 않은 이유: 이 라벨은 00:00~03:59 를
"장 전", 20:00~23:59 를 "장 후" 로 부르는데 phase 로는 둘 다 `overnight` 이라
한 값으로 접히지 않는다. 기존 동작을 보존하려면 마감 시각만 받아 나누는 쪽이 맞다.

### `run_narrative.py` (945 → 952)

`_session_label_for_utc` → `cc.narrative_session_label` 위임. app.py 와 **같은
함수를 부른다** — docstring 의 선언이 아니라 구조로 보장된다.

### `refresh_market_calendar.py` (274 → 312)

STEP 5 추가. 반일장 불일치(`half_mismatch`)도 `add_reminder` 를 타게 한다.

**배달 경로의 비대칭이 문제였다.** 임시 휴장(`extra`)은 Reminders 시트로 가는데
반일장 불일치는 STEP 2 에서 `print` 만 했다. 주 1회 Actions 배치라 사람이 로그를
열지 않으면 아무도 모르고, Actions 는 초록불로 끝난다.

이 규칙이 틀리면 조용히 깨지는 것: 앱 시장 상태 헤더 · 2PM 장중 가드 ·
세션 라벨 — 셋 다 이 규칙 하나에 걸려 있다.

중복 생성은 `add_reminder` 가 제목 기준으로 막는다. 제목에 날짜가 들어가 있어
같은 불일치가 매주 재발해도 항목은 하나만 열린다.

### `check_freshness.py` (241 → 276)

- `calendar_core.py` 신규 등록 — 마커 4개
- `run_narrative.py` · `refresh_market_calendar.py` 신규 등록 — 짝 마커
- `run_watchlist_alerts.py` 마커를 `[]` → `["_intraday_close_passed",
  "session_close_time"]`. 지금까지 0개라, `_intraday_close_passed` 없는 낡은
  사본을 올려도 관문이 못 잡았다.
- `CROSS_TARGETS` 에 `calendar_core` 추가. app.py 가 `mcal.` 로 5개, 자동화가
  `cc.` 로 부르는데 지금까지 교차 검사 대상이 아니었다.
- 자동화 교차 검사 루프에 `run_narrative.py` · `refresh_market_calendar.py` 추가.

### `diag_market_calendar.py` (798 → 1245) · `.yml` (63 → 79)

**J절 (24항목)** — `session_phase` / `narrative_session_label` 경계 계약.
정규장·반일장·휴장·주말 × 정각 경계, `extra_closed`/`half_map` 보강 우선순위,
망가진 `half_map` 에서의 내림차순 방어, 라벨 표 계약(기존 문자열 4개 보존).

**K절 (35항목)** — 소비자 배선 AST 래칫. 밴드 리터럴 **부재**를 본다.
I절이 "휴장 게이트를 부르는가"라면 K절은 "밴드를 다시 적지 않는가"다.
역검증 픽스처 10개는 **변경 전 실제 소스**라 그대로 회귀 케이스다.

**G절** — J-M1(마감을 항상 16:00) · J-M2(`close_minutes` 960 하드코딩) 추가.

125 → 187 항목.

---

## 검증

```
diag_market_calendar.py    ✅ 187/0     (125 → 187)
diag_halfday_gate.py       ✅  36/0     (회귀 없음)
diag_reminders.py            52/2       기준선과 동일 (yml 경로, 무관)
check_freshness.py         ✅ 정합성 9/9 · 마커 전원 만점
check_py311.py             ✅ 6/6
py_compile                 ✅ 6/6
pyflakes 델타              ✅ 0         (6개 파일 전부 기준선 동일)
배포 레이아웃(automation/) ✅ 187/0     평면 사본과 동일
app.py DRG 블록 실동작     ✅ 12/12     소스에서 추출해 exec
```

### 역검증 — 락스텝이 실제로 작동하는가

| 되돌린 파일 | 결과 |
|---|---|
| 낡은 `app.py` | ❌ K-2·3·4·5·6·7 **6건** |
| 낡은 `run_narrative.py` | ❌ K-2·3 **2건** |
| 낡은 `refresh_market_calendar.py` | ❌ K-8 **1건** |
| 낡은 `calendar_core.py` | 💥 `AttributeError: session_phase` — 시끄럽게 죽음 |
| 낡은 `calendar_core` + 새 소비자 | ❌ check_freshness 정합성 2건 (양방향 검출) |

### 작업 중 잡은 결함 2건

**죽은 게이트.** J절 뮤테이션을 `muts.append()` 로 썼는데 소비 루프
(`for name, caught in muts`)가 그 지점보다 **위에** 있었다. 항목이 영원히
소비되지 않고 실패 수는 그대로 — 초록불 속의 죽은 게이트다. `check()` 직접
호출로 바꾸고 재발 방지 주석을 박았다.

**K-R7 판별력 없음.** 변경 전 `market_session` 은 `if` **문**이 아니라 한 줄
삼항식(`IfExp`)이라, 감싼 `If` 의 test 만 보는 검사에 안 걸렸다 — `et_hour` 가
판정에 살아 있는데 조용히 통과했다. 대입식 자체의 이름도 보도록 고쳤다.

---

## 락스텝 배포 순서

**중간 어느 시점에도 관문이 빨간불이 되지 않는 순서다.**
`check_freshness.py` 가 `market_8am.yml` · `market_5pm_weekend.yml` 안에서
하드 실패로 돌기 때문에 순서가 중요하다.

| # | 파일 | 경로 | GitHub 표시 줄수 |
|---|---|---|---:|
| 1 | `calendar_core.py` | repo root | 668 |
| 2 | `run_narrative.py` | `automation/` | 951 |
| 3 | `refresh_market_calendar.py` | `automation/` | 311 |
| 4 | `app.py` | repo root | 28588 |
| 5 | `check_freshness.py` | repo root | 275 |
| 6 | `diag_market_calendar.py` | `automation/` | 1244 |
| 7 | `diag_market_calendar.yml` | `.github/workflows/` | 78 |

1번은 순수 추가라 낡은 소비자와 공존해도 안 깨진다. 5번은 올리는 순간부터
강제되므로 소비자 3개 뒤에 둔다. 6번은 K절이 app.py 를 읽으므로 4번 뒤에 둔다.

**Streamlit 리부트 필요** (1번·4번이 앱 경로).

---

## 동작 변경 2건 (의도)

- **16:00 정각.** 통합 전 라벨은 `570 <= m <= 960` 이라 마감 정각을 "장중"으로,
  `get_market_status` 는 `m < close_m` 이라 "After-hours"로 불렀다. 그래서 1분
  동안 한 화면에 두 상태가 동시에 떴다. 헤더 쪽으로 통일.
- **20:00 정각.** 같은 성격. `961 <= m <= 1200` → `m < close_m + 240`.

## 한계·후속

- **시트 보강분(`half_map` / `extra_closed`)은 여전히 소비되지 않는다.**
  `Market_Calendar` 에 `Adj_Close` 가 매주 쌓이지만 판정은 100% 규칙 계산이다.
  카터 국장일류 임시 휴장은 규칙으로 못 잡는다. J-E 군이 "보강 > 규칙" 계약을
  미리 고정해 뒀으니, 연결할 때 방향을 다시 정할 필요는 없다.
  → 별도 설계 대화 필요 (핫 패스 I/O 0 불변식을 건드린다)
- `run_drg_predict.py` 의 `[현재 시각]` 은 `(Pre-Market)` 고정이다. 8AM·9AM
  개장 전에만 돌아서 문제가 없지만, app.py 와 문자열이 다른 상태는 유지된다.
- `run_drg_verify.py` · `refresh_industry_perf.py` 는 아직 check_freshness 의
  자동화 교차 검사 루프에 없다. 이번 변경과 무관해서 넣지 않았다.
