fix(satellite): 크레딧 게이트 Phase 0 프로브 — FRED 3년 제한으로 데이터 벤더 교체
(FRED OAS → FMP HYG/LQD)

## 배경

1차 실행(GitHub Actions, 2026-09-13) 결과 `[C-COVER]` 6개 사건 중 1개만 커버.
원인은 경제적 결과가 아니라 데이터 단절 — FRED `BAMLH0A0HYM2` 페이지 확인 결과
2026-04 ICE Data 라이선스 변경으로 공개 API 이력이 롤링 3년으로 제한됨(수신
787행 2023-09-12~2026-09-10 이 FRED 메타데이터 `PeriodOfTime` 과 정확히 일치 —
확인됨). §5 는 "결과를 보고 문턱·구간·정의를 바꾸는 것"을 막지, "벤더가 데이터를
끊어서 벤더를 바꾸는 것"은 막지 않는다 — `diag_issuance_probe` 의 경로 M/S
선택과 같은 성격의 교체.

## 변경 파일

### 수정 — automation/diag_credit_gate_probe.py (398줄, GitHub 표시 397줄)

- 크레딧신호 정의를 FRED OAS(상승=스트레스) → FMP HYG/LQD 종가비율(하락=스트레스)
  로 교체. 방향이 반대라 `ma_crossdowns()` 함수를 신설(기존 `ma_crossups()` 와
  대칭, `_confirmed_run_starts` 공용 — 그쪽 로직은 안 건드림).
- FRED·`fredapi` 의존 완전 제거. SPY 수신도 `_fetch_fmp_deep()` 로 통합해
  중복 제거.
- 사건 정의(`EPISODE_DD`) · 문턱(`C_LEAD_PASS_FRAC` · `COVER_TRADING_DAYS` ·
  `LEAD_SEARCH_DAYS` · `NOISE_EXCLUDE_DAYS`) · 확인일수(`CONFIRM_DAYS`)
  **전부 불변** — 재협상 없음.
- 셀프테스트 5항목은 부호만 반전(하락 방향 합성 데이터), 항목 수·판정 로직 동일.
- `check_freshness.py` 마커(`C_LEAD_PASS_FRAC`, `_confirmed_run_starts`)는
  둘 다 이 파일에 여전히 존재 — **변경 불필요**.

### 수정 — automation/diag_credit_gate_probe.yml

- `FRED_API_KEY` 시크릿 · `fredapi` pip 설치 제거(더 이상 불필요).
- 나머지 워크플로 구조(수동 전용, 셀프테스트 우선 실행, py311 체크) 불변.

## 검증 결과 (이 세션에서 재실행)

- `py_compile` 통과, `.yml` PyYAML 파싱 통과
- `check_py311.py` 1/1 통과, `pyflakes` 0 경고
- 임포트 스모크 통과 (`diag_momentum_deep_ref` 락스텝 확인)
- 셀프테스트 **5/5 통과** (`--selftest`, `SELFTEST=1` 두 경로 모두)
- `check_freshness.py` 재실행 — 신규 파일 마커 2/2 그대로, 다른 회귀 없음

## 락스텝 배포 순서

1. `diag_credit_gate_probe.py` → `automation/` (덮어쓰기)
2. `diag_credit_gate_probe.yml` → `.github/workflows/` (덮어쓰기)

`check_freshness.py`는 이번 변경분에 포함되지 않음 — 1차 딜리버리에서 이미
반영됐고 마커 이름이 그대로라 재수정 불필요.

## Streamlit 리부팅

불필요.

## 한계 · 후속

- HYG(2007-04 상장 추정) / LQD(2002 상장) 특성상 사건1(2007-10 고점)은
  커버리지 부족으로 여전히 제외될 가능성이 높다 — 추정일 뿐, 실행 결과가
  확정한다.
- `[C-DECIDE]` ✅ 면 Phase 1 사전 약정으로 진행. ❌ 면 이번엔 데이터 단절이
  아니라 실제 경제적 근거로 종료 — 크레딧 게이트 후보 자체가 닫힌다.
