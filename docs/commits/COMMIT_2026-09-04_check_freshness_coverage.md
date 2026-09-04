feat(freshness): 지문 표 디렉터리 스캔 전환 · 마커 보강 · 정합성 검사 전수 확대

사본 82개 중 **67개가 지문 표에 없어 줄 수 대조 자체가 불가능**했다. 그중
하나가 `check_freshness.py` 자신이었고, 실제로 이 파일의 사본이 배포본보다
16줄 낡아 있었는데도 **아무도 그 사실을 알 수 없었다.** 커버리지를 디렉터리
스캔으로 바꿔 파일이 표에서 빠지는 일을 구조적으로 없앤다.

FMP 호출 변화: 0콜. 시트·이메일 접촉 없음. Streamlit 재부팅 불필요.

---

## 이 커밋이 실제로 막은 사고

작업 착수 시 프로젝트 사본(187줄)을 기반으로 잡았으나,
`diag_hist_window_consumers.yml:255-261` 주석이 사본 코드로는 발생 불가능한
실패 예시(`run_narrative.py → fmp_extras`)를 들고 있어 배포본을 재요청했다.
**배포본은 203줄이었다.** 사본 기준으로 덮어썼다면 다음이 소실된다:

| 소실될 뻔한 것 | 배포본 위치 |
|---|---|
| `app.py` 마커 `cached_hidden_alpha_gates` | MARKERS |
| `rotation_core.py` 마커 5개 | MARKERS (항목 통째) |
| `run_hidden_alpha.py` 마커 3개 | MARKERS (항목 통째) |
| `CROSS_TARGETS` 의 `rotation_core` | 교차검사 대상 |
| `path()` 의 `automation/` 폴백 | 분할 배치 대응 |
| 정합성 루프의 `run_hidden_alpha.py` | AUTOMATION 튜플 |

이 파일이 **지문 표에 자기 자신을 넣지 않았기 때문에** 격차가 숨어 있었다.
이번 변경의 ①이 정확히 그 구멍을 막는다.

---

## 파일별 변경

### `check_freshness.py` (203 → 498줄)

**① 지문 표 — 하드코딩 목록 → 디렉터리 스캔**

루트와 `automation/` 을 모두 걷어 `.py` 전부를 출력한다. 세 그룹으로 나눈다:

| 그룹 | 판정 | 개수(사본 기준) |
|---|---|---|
| 코어 모듈 (`app` · `*_core` · `fmp_http` · `gs_retry` · `fmp_extras`) | 실패(exit 1) | 17 |
| 자동화 러너 (`run_` · `refresh_` · `backfill_` · `seed_`) | `run_`/`refresh_` 만 실패 | 13 |
| 진단 · 유틸 (`diag_` · `check_`) | 경고만 | 52 |

- 접두 판정이 접미보다 우선한다 — `diag_industry_core.py` 는 이름이 `_core.py`
  로 끝나지만 진단이다. 순서를 뒤집으면 진단 파일이 실패 판정 대상이 된다
- 진단·일회성 스크립트를 **경고**로 둔 이유: 스텁·합성 모듈을 다루므로 빨간불이
  정상일 수 있고, 여기서 exit 1 이 나면 워크플로 후속 단계가 조용히 건너뛰어진다
  (`diag_aum_field` A1 누락 때 실제로 발생)
- 이름 폭은 실측으로 잡는다. 분할 배치에서 `automation/` 접두가 붙으면 최장
  44자라 고정 폭이면 열이 어긋난다. 길면 2열 → 1열로 자동 전환
- 선택 인자로 필터 가능: `python3 check_freshness.py . regime_core diag_fmp_ssot`
  (지문 표만 걸러지고 정합성 검사는 항상 전체)

**② 마커 — 배포본과 합집합 병합 (38 → 95개, 소실 0)**

기존 마커를 덮지 않고 합쳤다. 규칙을 주석에 명시했다: **그 파일의 가장 최근
이행 심볼**을 넣는다. 오래된 심볼만 있으면 낡은 사본이 초록으로 통과한다.

주요 추가:

| 파일 | 추가 마커 | 이유 |
|---|---|---|
| `run_earnings_watch.py` | `hist_range_params` · `cc.is_market_open_today` | 6개 마커가 전부 창·캘린더 이행 **이전** 심볼이라 37줄 낡은 사본이 6/6 초록으로 통과했다 |
| `fmp_extras.py` | `hist_range_params` · `hist_days_for_bars` · `HIST_MAX_DAYS` | `limit=` 이전 버전 판별 |
| `calendar_core.py` | `nyse_early_close_days` 외 4 | v1.1.0 반장 지원 |
| `regime_core.py` | `replacement_hurdle` · `rank_weakest` · `is_weak_status` | 슬롯 교체 A단계 |
| `run_signal_backtest.py` | `HISTORY_BARS` 외 3 | 신규 등록(지문 표에 아예 없었음) |
| `scanner_core` · `portfolio_core` | 실제 심볼 | 배포본은 빈 목록 `[]` 이라 아무것도 증명 못 했다 |

`run_watchlist_alerts.py` 에 `hist_range_params` 를 **넣지 않았다** — 이 파일은
`fx.hist_days_for_holding` 계열을 쓴다. 없는 게 정상이며, 넣으면 영구 ⚠ 누락이
된다.

**③ 정합성 검사 — 자동화 3개 → 임포터 전수 51개**

`CROSS_TARGETS` 10 → 16 (`calendar_core` · `rotation_core` · `industry_core` ·
`reminders_core` · `fmp_http` · `gs_retry` 추가). `import` 로 공용 모듈을
끌어쓰는 **모든 파일**을 검사한다. `app.py` 기준 8개 모듈 → **13개 모듈 ·
220개 심볼**.

두 가지 오탐을 처리했다:

- **별칭 섀도잉** — `diag_hist_window.py:120` 의 `with open(...) as fh:` 가 모듈
  별칭 `fh`(fmp_http)를 가린다. 함수 스코프별 지역 바인딩을 계산해 그 함수
  안에서만 검사를 끈다. **같은 파일의 다른 사용처는 그대로 검사된다**(M5b 로 증명)
- **던더** — `fx.__file__` 류를 제외

`top_level_names` 도 보강했다: `import` / `from ... import` 바인딩과 최상위
`try:` / `if:` / `with:` 안의 정의를 인식한다(조건부 import 관용구 오탐 방지).

**④ 신설 — 분할 배치 드리프트 검사**

루트와 `automation/` 에 같은 파일이 다른 줄 수로 있으면 실패. 락스텝이 깨진
상태가 조용히 지나가지 않는다.

**남은 한계 (주석에 명시)**

정합성 검사는 **방향이 하나뿐**이다. "소비자가 부르는 심볼이 모듈에 없다"만
잡고, 소비자가 낡아서 **새 SSOT 심볼을 아직 안 부르는** 경우는 원리상 못 잡는다
(`run_earnings_watch` 가 정확히 그 케이스였다). 그 방향은 마커(②)와 Actions 의
diag 스위트(`diag_market_calendar` I군 · `diag_hist_window_consumers`)가 맡는다.
**여기에 같은 규칙을 복제하지 않는다** — 소유권이 갈리면 표류한다.

마커 누락은 exit 코드에 반영하지 않는다(경고). 배포본 동작을 유지한 것이며,
빨간불로 바꾸면 Actions 파이프라인이 막힌다.

---

## 검증 결과

| 항목 | 결과 |
|---|---|
| `py_compile` | ✅ 통과 |
| `check_py311.py` | ✅ 통과 |
| `pyflakes` | 0건 (배포본도 0건 · 델타 0) |
| 배포본 마커 보존 | ✅ 소실 0 (38 → 95개) |
| `CROSS_TARGETS` 보존 | ✅ 소실 0 (10 → 16) |
| 평평 배치 실행 | ✅ EXIT=0 · 82개 파일 · 임포터 51개 |
| **분할 배치 재현** (루트 19 + `automation/` 63) | ✅ EXIT=0 · 마커 전건 해결 |

**변이 13종 전건 검출**

| 변이 | 기대 | 결과 |
|---|---|---|
| M0 무결 기준선 | EXIT 0 | ✅ |
| M1 캘린더 이행 이전 사본 | 마커 `7/8` + 누락 심볼명 | ✅ |
| M2 코어 심볼 불일치 | EXIT 1 | ✅ (5건 탐지) |
| M3 진단 심볼 불일치 | 경고만 · EXIT 0 | ✅ |
| M4 분할 배치 드리프트 | EXIT 1 | ✅ |
| M4b 양쪽 동일 사본 | EXIT 0 | ✅ |
| M5 `fh.read` 오탐 | 미발생 | ✅ |
| M5b 섀도잉 파일의 다른 사용처 | 검출 | ✅ |
| M5c 합성 대조 — 최상위 `fh.NOPE` | 검출 | ✅ |
| M5d 합성 대조 — 섀도우 안 `fh.NOPE` | 억제 | ✅ |
| M7a 진단 파일 구문 오류 | EXIT 0 | ✅ |
| M7b 코어 파일 구문 오류 | EXIT 1 | ✅ |
| M8 고아 마커(사본 없음) | 검출 | ✅ |

복원 검증: 변조 잔존 0 · 잔여 파일 0.

**역검증** — 동일 결함(`fmp_http.fmp_get_json` 제거)에서
배포본 **0건 탐지 · EXIT 0**, 신규본 **14건 탐지 · EXIT 1**.
배포본은 `fmp_http` 를 `CROSS_TARGETS` 에 두지 않아 원리상 못 본다.

**변이 설계 중 자체 발견한 테스트 버그**: `"def fmp_get_json"` 이
`fmp_get_json_ex`(212행, 더 앞)의 접두라 `str.replace(..., 1)` 이 엉뚱한 함수를
바꿨고, 결과가 미탐처럼 보였다. `^def fmp_get_json\(` 로 경계를 지정해 재검하니
정상 검출. — **접두 충돌 치환은 미탐으로 위장한다.**

---

## 배포

루트 1개 파일. **락스텝 없음 · Streamlit 재부팅 불필요.**

1. `check_freshness.py` — GitHub 표시 **497줄**

### 배포 직후 확인

로컬에서:

```
python3 check_freshness.py .
```

- 헤더에 `automation/ 하위 디렉터리 감지 — 함께 스캔합니다.` 가 뜬다
- 표가 **코어 / 자동화 러너 / 진단·유틸** 3그룹으로 갈린다
- 마지막 줄이 `합계 82개 파일 · 마커 선언 26개 · 줄 수만 56개` 형태
- `automation/run_earnings_watch.py` 가 `8/8`, `automation/run_hidden_alpha.py`
  가 `7/7` 이면 정상

### 다음 Actions 실행에서 볼 것

`🔬 진단 — 조회 창 소비처` 워크플로의
`🔁 락스텝 스위트 — 사본 지문 + 모듈 간 정합성` 단계.

- `검사한 임포터 51개 (실패 판정 대상 28개 중)` 가 찍힌다
- `⚠ 진단·일회성 스크립트 경고 N건 (exit 코드에는 반영 안 함)` 는 **정상**이며
  단계를 실패시키지 않는다
- `❌ 정합성 문제` 가 뜨면 **반쪽 배포**다 — 지목된 파일을 올려야 한다

`diag_hist_window_consumers.yml:255-261` 주석이 예시로 든
`run_narrative.py → fmp_extras: ['hist_days_for_bars'] 없음` 은 **이번 변경으로
처음으로 실제 발생 가능해졌다.** 배포본은 `run_narrative` 를 검사 대상에 두지
않아 그 보장이 문서에만 있었다.

### 정상적으로 **안 보이는** 것

- 시트 변화 0 · 이메일 0 · FMP 콜 0
- app.py / Streamlit 화면 변화 0
- 마커 누락 경고는 exit 코드에 반영되지 않는다(의도)

### 롤백

파일 하나 되돌리면 끝. **데이터 손실 없음** — 읽기 전용 스크립트다.

---

## 남은 한계 · 후속

1. **마커는 손으로 유지해야 한다.** 이행을 끝낼 때 마커 갱신을 이행 작업의
   일부로 넣는 규율 외에 자동 강제 수단이 없다. `run_scanner_scan` ·
   `run_weekly_report` · `backfill_*` · `seed_*` 는 아직 마커가 없다(줄 수만).
2. **진단 파일 52개는 전부 마커가 없다.** 줄 수 대조만 가능하다. 자주 통째로
   덮어쓰는 파일들이므로 최소한 상위 몇 개(`diag_fmp_ssot` · `diag_rotation_policy`
   · `diag_hist_window_consumers`)에는 마커를 두는 것을 검토할 것.
3. **시그니처 드리프트는 여전히 못 본다.** 이 검사는 '심볼 존재'만 본다
   (`diag_hist_window.yml:11-12` 에 기록된 2026-08-27 사고 — 18곳 전건
   `TypeError`). 그 방향은 `diag_hist_window` `[A4]` 가 맡는다.
4. **`_locally_bound` 는 중첩 함수까지 함께 걷어 다소 과수집한다.** 과수집은
   검사를 건너뛰는 쪽이라 오탐 대신 미탐이 된다. 섀도잉이 드물어 감수했으나,
   중첩 함수에서 별칭을 가리는 코드가 늘면 재검토할 것.
5. **`diag_hist_window_consumers.yml` 주석의 숫자가 낡았다.** "지문표에 자동화
   7개 · 정합성 6개 자동화 파일" 이라 적혀 있으나 배포본 실제는 3개/3개였다.
   지금은 전수(51개)이므로 주석을 갱신할 것 — 별건.
