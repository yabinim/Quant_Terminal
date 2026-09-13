feat(satellite): Track C 크레딧 게이트 Phase 0 데이터 프로브 추가

## 배경

SATELLITE_MANDATE §3 시장 필터(SPY < 자기 200일선)에 크레딧 스프레드를 OR 축으로
추가하는 Track C 후보("크레딧 게이트")를 위한 Phase 0 읽기 전용 데이터 프로브.
신용시장이 주가보다 먼저 무너지는지를, 결합 문턱(몇 %p·몇 주)을 정하기 전에
데이터로만 확인한다. 사전 결정(대화 기록, 2026-09-13): 데이터 소스 FRED
BAMLH0A0HYM2(ICE BofA US High Yield OAS), 결합 로직 OR.

## 변경 파일

### 신규 — automation/diag_credit_gate_probe.py (373줄, GitHub 표시 372줄)

- [C-COVER]·[C-LEAD]·[C-NOISE]·[C-LAG-DATA]·[C-DECIDE] 5개 게이트. 판독 기준은
  실행 **전** docstring 에 고정.
- §3 과 대칭인 "OAS > 자기 200일선" 방식 채택 — 문턱을 보고 나서 고르는 것을
  피한다.
- 사건 목록은 `diag_momentum_deep_ref.find_episodes` 재사용(재구현 금지,
  `EPISODE_DD=-0.12` 그대로) — **락스텝**.
- 이동평균 교차는 `regime_core` 의 "2일 확인" 관례 재사용(`CONFIRM_DAYS=2`)으로
  단발 잡음 제거.
- 수익률 계산 0. 시트 읽기/쓰기 0 — 콘솔 출력뿐.
- 셀프테스트 5항목(네트워크 불필요): confirm-run 회귀(T1) · 사건추출+리드검출
  (T2·T3) · 격리 크로스업 카운트(T4) · 커버리지 게이트(T5).

### 신규 — automation/diag_credit_gate_probe.yml

- `workflow_dispatch` 전용(수동), `selftest` 입력값.
- `FMP_API_KEY` + `FRED_API_KEY` 시크릿 필요. `pip install`에 `fredapi` 포함.

### 수정 — check_freshness.py

- `MARKERS`에 `"diag_credit_gate_probe.py": ["C_LEAD_PASS_FRAC", "_confirmed_run_starts"]`
  추가. 다른 로직 변경 없음.

## 검증 결과 (이 세션에서 실행)

- `py_compile` 3/3 통과 (`diag_credit_gate_probe.py`, `.yml` 문법(PyYAML 파싱),
  `check_freshness.py`)
- `check_py311.py` 1/1 통과
- 임포트 스모크 통과 — `diag_momentum_deep_ref` 락스텝 확인(그쪽이 깨지면
  여기서 먼저 죽는다)
- `pyflakes` 0 경고
- 셀프테스트 **5/5 통과** — `--selftest` 인자, `SELFTEST=1` 환경변수 두 경로
  모두 확인
- `check_freshness.py` 재실행 — 신규 파일 2/2 마커, 기존 9개 모듈 정합성 그대로
  유지(회귀 없음)

## 발견된 버그 (수정하며 학습 — 향후 재발 방지용으로 기록)

pandas bool Series 를 `.shift(1).fillna(False)` 하면 NaN 도입 때문에 dtype 이
`object` 로 바뀐다. 그 위에서 `~True`(파이썬 bool)은 논리 부정이 아니라 정수
비트 NOT(`-2`)으로 평가되어 **항상 참**이 된다 — "확정 후 막 시작한 날" 판정이
매일 참으로 찍히는 조용한 오류였다(초기 프로토타입에서 800봉 중 227일이
크로스업으로 잘못 검출됨). `.shift(1, fill_value=False)`로 dtype 을 `bool` 로
유지해야 `~`가 제대로 동작한다. 셀프테스트 T1 이 이 회귀를 고정한다.

## 락스텝 배포 순서

1. `diag_credit_gate_probe.py` → `automation/`
2. `diag_credit_gate_probe.yml` → `.github/workflows/`
3. `check_freshness.py` → 루트 (마커 추가분만)

순서를 안 지켜도 서로를 깨뜨리지는 않는다(전부 신규 파일이라 기존 소비자가
아직 아무도 안 부른다) — 다만 마커-파일 짝이 어긋나지 않도록 세 개를 한
커밋으로 묶는다.

## Streamlit 리부팅

불필요 — `app.py` / `fmp_extras.py` 미변경.

## 한계 · 후속

- 이 프로브는 **아직 실행되지 않았다.** 실제 FMP·FRED 콜이 있는 실행은
  GitHub Actions(`workflow_dispatch`, secrets 필요)에서만 가능하다. 로컬/이
  세션 샌드박스는 두 API 도메인에 대한 네트워크 접근이 없다.
- `[C-DECIDE]` ✅ → Phase 1 사전 약정(`PEAD_PRECOMMIT.md`/`ISSUANCE_PRECOMMIT.md`
  꼴)으로 진행. 결합 문턱(몇 %p·몇 주) 숫자는 이 프로브가 보고하는 분포 통계로
  정한다.
- `[C-DECIDE]` ❌ → 종료 후보. net issuance 처럼 사전 약정 없이 여기서 끝날 수
  있다.
