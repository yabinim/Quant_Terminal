docs(fmp): 낡은 'FMP 이력 상한 ~1,254봉' 주장 정정 — 주석·독스트링·출력문만 (로직 변경 0)

## 배경

2026-09-10 실측(diag_hist_ceiling)으로 FMP `historical-price-eod` 단일 호출 상한은
**롤링 5,000 레코드(≈19.8년)** 로 확인됐고 `HIST_MAX_DAYS` 는 1826 → 5478 로 올라갔다.
저장소 곳곳에는 여전히 "FMP 이력 상한 ~1,254봉 / 5년 한도"가 사실로 적혀 있었다.

**1,254봉의 정체는 둘이었다** (인수인계 §배경 1번은 반만 맞다):

| 시기 | 1,254봉이 나온 이유 |
|---|---|
| limit 시절 (~2026-08-28) | `from` 없이 보낸 요청에 FMP 가 주는 **기본 응답 창(≈5년)**. limit 은 무시됐다 |
| from/to 시절 (08-28~09-10) | 그 기본 창을 본뜬 정책 상수 `HIST_MAX_DAYS=1826` 의 클램프 |

둘 다 상한이 아니었다. 착오의 뿌리는 **기본값을 상한으로 읽은 것**이다.
그래서 이번 정정의 공통 표기는 "기본 창"이다 (정의: `fmp_extras.HIST_MAX_DAYS` 주석).

## 처리 결과 — 인수인계 51곳

| 처리 | 건수 | 기준 |
|---|---|---|
| 정정 | 24 | 1,254봉/5년을 한도·상한·"전폭"으로 적은 줄, 5478 로 현재 동작이 달라진 줄 |
| 태그 | 17 | "무엇을 적어도 ~1,254봉이 온다" 류 — limit 시절엔 참. "(기본 창)" 표기 추가 |
| 보존+주석 | 4 | 당시 판정·가설 기록 (fmp_extras:92, diag_fmp_depth:9·18, diag_satellite_backtest:338) |
| 보존 | 6 | 상한으로 읽힐 여지 없는 사실 서술·이미 정정된 블록·테스트 픽스처 |

보존 6건: `scanner_core:501`(페이로드 산술), `diag_earnings_preview_backtest:490`(요청≠수신 교훈),
`diag_satellite_backtest:27·241·1422`(09-10 정정 블록 안), `diag_regime_window:465`(S18 판별 픽스처 — 테스트 입력).

## 추가 발견 — 인수인계 목록 밖 12곳 (전부 같은 24파일 안)

grep 패턴(1,254/1,255)에 안 걸린 같은 착오:
- `run_signal_backtest` — TEST_LOOKBACK 주석("FMP 이력 한도에 맞춘 상한"), "2018·2020 데이터 자체가 없다",
  "1034 넘으면 FMP 5년 한도가 먼저 막는다", 전·후반 분할 주석, **출력문 1줄**
- `regime_core:1768` — 시장 게이트 검증 한계 "FMP 5년 한도로 2018/2020 검증 불가"
- `diag_satellite_backtest` — "FMP 가 상한을 올리면", `_fmp_eod` 독스트링, **출력문 1줄**
- `diag_fmp_depth:26`, `fmp_extras` 모멘텀 변동성 근거(§4① 핀 창 기준임을 명시)

**그리고 `fmp_extras.HIST_MAX_DAYS` 주석의 영향 범위가 틀려 있었다.** "5478 상향 때 상한에 걸려 있던 곳은
둘뿐"이라고 적혀 있었으나 pad 5봉 때문에 경계를 넘긴 곳이 셋 더 있었다:

| 호출부 | 요구 | 옛 창 → 새 창 | 영향 |
|---|---|---|---|
| run_signal_backtest | 1255봉 | 1826 → 1834일 | 월간 백테스트가 ~6봉 더 받는다. 평가 구간(마지막 934봉)은 동일, 앞쪽 워밍업만 길어짐 |
| diag_earnings_preview_backtest | 1250봉 | 1826 → 1827일 | 1일 |
| diag_fmp_depth | run_signal_backtest 설정 차용 | 동일 | 진단 전용 |

주석에 기록하고, 앞으로 점검 기준을 `bars` 가 아니라 `hist_days_for_bars(bars) > 옛 상한` 으로 적었다.

## 출력 문자열 변경 (로그 문구가 바뀌는 4곳)

- `diag_hist_window` R6: `상한이 limit 시절 수신량(약 1,254봉 · 기본 창) 이상 → 3763봉` (임계 `>= 1200` 불변)
- `diag_universe_funnel` S4: `타임아웃이 15초 이상 (5년치 ≈1,260봉 페이로드)` (임계 `>= 15` 불변)
- `run_signal_backtest` [전·후반 재현성]: `평가 구간(TEST_LOOKBACK)에 2018·2020 하락장이 들어 있지 않다.`
- `diag_satellite_backtest` 해석 주의 2): `조회 창 1,826달력일(WINDOW_DAYS_PIN)이라 2020 코로나·2022 초입 하락장이 평가 구간에 없다.`

## 검증

**"주석만 바꿨다"의 직접 증명 — AST 비교** (수정 전 사본 대비, 20개 .py):
- 모든 str 상수를 가린 AST 덤프가 20/20 **완전 동일** → 숫자·이름·비교식·구조 불변
- 달라진 str 상수 = 독스트링 13 + 위 출력 문자열 4 뿐 (허용 목록 외 0)
- 검증기 자체 뮤테이션: `_FMP_TIMEOUT 20→15` ❌ 검출 · 허용 밖 리터럴 1글자 변경 ❌ 검출
- yml 4개: YAML 파싱 결과 동일 (주석만 변경)

| 검사 | 결과 (수정 전과 동일) |
|---|---|
| py_compile | ✅ 20/20 |
| pyflakes | 델타 0 (기존 25건 그대로) |
| diag_fmp_ssot | ✅ 66건 |
| diag_hist_window | ✅ 128/128 |
| diag_hist_window_consumers | ✅ 134/134 |
| diag_satellite_backtest --selftest | ✅ 전 항목 |
| diag_momentum_rule_compare --selftest | ✅ 전 항목 |
| diag_regime_window | ✅ 46/46 |
| diag_universe_funnel | ✅ 76건 |
| diag_satellite_mandate | ✅ 문서와 코드 일치 |
| check_py311 · check_freshness | ✅ |

## 배포

로직 변경 0 — 락스텝 순서 무관. 24파일 직접 덮어쓰기(dev). Streamlit 리부트는 규칙상 1회(동작 변화 없음).

## 한계 · 후속

- `app.py:3740` "5년 전체 피드(≈1,254행)" — 같은 착오. 이 한 줄 때문에 2만8천 줄 모놀리스를 재배포하지
  않는다. 다음 app.py 변경 때 L49 주석 부채와 함께 처리.
- `diag_fmp_window.part_ce` 의 `ACTUAL = 1254` — 코드 상수라 범위 밖. limit 시절 호출부 감사용이라 의미상 정확.
- 인수인계 "같이 볼 것"(diag_fmp_newcaps 7년 창) — **종결.** `_D_FROM_7Y` 는 업종·섹터·업종PER
  엔드포인트에만 쓰였고 `historical-price-eod` 에는 한 번도 쓰이지 않았다. 반증 데이터는 거기 없었다.
- 작업 A 참고: `fmp_extras.MOM_VOL_BARS=252`(MSCI 3년 주간 대신 1년 일간) 선택 근거가 1,255봉 창에 묶여 있다.
  깊은 창에서는 근거가 사라지지만 이것은 룰 변경이므로 A 와 분리할 것.
- HANDOFF_DETAIL.md §배경의 "1,254봉 = 정책 상수" 문장을 위 두 정체 표로 갱신할 것.
