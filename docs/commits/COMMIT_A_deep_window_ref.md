feat(diag): 깊은 창 참고 실행 — 사전 약정 + 러너 (판정 아님 · 결과 보기 전 커밋)

## 배경

2026-09-10 실측으로 FMP 단일 호출 상한이 롤링 5,000 레코드(≈19.8년)임이 밝혀졌다.
§4① 은 "판정 재실행은 1,826달력일 핀 그대로, 깊은 창은 참고용으로 따로"라고 사전 결정했다.
이 커밋은 그 "따로"를 구현하고, **실행 전에** 사전 약정을 코드·문서에 박는다.
이 커밋의 시각이 재협상 방지 장치다 — 결과는 두 번째 커밋에 기록한다.

## 사전 약정 (SATELLITE_MANDATE §4① · 러너 헤더 — 두 곳이 가드로 묶여 있다)

- 질문: "각 룰이 하락·반등 구간에서 어떻게 깨지나" 하나. 승수·전체 기간 샤프 순위는 출력하지 않는다
- 대상: `blend` · `mom12_1` · `mom12_0` × top5 균등 · swap · weekly × 필터 `none` / `no_new`
- 사건: SPY 원종가 직전 고점 대비 −12%, 회복 전 재하락은 같은 사건, 반등 구간 126봉
- 창: FMP 단일 호출 상한까지. 평가 시작 전 고점은 절삭 표시
- 결과로 하지 않는 것: A/B 룰 변경 · 트리거 숫자 변경. 쓸 수 있는 곳은 §2 각주 "알려진 약점"뿐

## 파일별 변경

**`automation/diag_momentum_deep_ref.py` (신규)**
- 사건은 SPY 만으로 기계적으로 추출하고, 룰 결과보다 **먼저** 출력한다
- 룰×필터별로 전 기간 1회 `bt.simulate` 를 돌리고, `curve`·`log` 를 구간별로 자른다. 창마다 재시작하지 않는 이유는 경로 의존 보유("고점에 무엇을 들고 있었나")를 재기 위해서다
- 창은 `bt.WINDOW_DAYS_OVERRIDE = 7400` 으로 **이 프로세스 안에서만** 켜고 `finally` 에서 되돌린다
- 중단 게이트 (시트에 쓰지 않음):
  - SPY 없음
  - 페치율 미달
  - SPY 배당조정 실패
  - 배당조정 계열이 원종가보다 7일 넘게 늦게 시작 — dividend-adjusted 깊이는 미실측이다. NaN 가격에서 `sell()` 이 조용히 무동작해 성과가 오염된다
  - SPY 수신 4,500봉 미만 — 약정한 자가 아니다
- 결과는 `Momentum_Rule_Deep` 전용 탭에 기록한다. NaN 셀은 공란으로 정규화한다 (NaN 이 섞이면 Sheets API 가 요청 전체를 거절한다)
- 엔진·룰·고정 축은 재구현하지 않는다: `rc.VERDICT_RULES`, `rc.FIXED_*`, `rc.BASE_VARIANT` 를 그대로 쓴다
- `--selftest` 16개 항목, 네트워크 불필요

**`.github/workflows/diag_momentum_deep_ref.yml` (신규)** — 표시명 `📐 진단 — 깊은 창 참고 실행 (판정 아님 · 수동 전용)`
- 수동 전용이다. 입력은 `as_of` · `min_fetch_rate`
- 실행 순서: SSOT 가드 → 엔진·룰 비교·러너 자체검증 → 본 실행

**`automation/diag_satellite_backtest.py`** — `WINDOW_DAYS_OVERRIDE = None` 을 신설했다
- `_window_days_for` 첫 분기: 값이 있으면 봉수 환산·`HIST_MAX_DAYS`·핀을 모두 건너뛰고, 알림을 1회 찍는다
- **판정 경로(핀 1826)는 불변이다.** 수정 전후 selftest 출력이 전량 동일하다
- 시그니처 대신 모듈 전역을 택한 이유: `_fmp_eod`·`_batch_fetch` 시그니처를 바꾸면 SSOT 가드의 `_stub_eod` 와 어긋나고, 그 TypeError 가 워커의 `except` 에 삼켜진다(P2 모양)

**`automation/diag_fmp_ssot.py`** — 66 → 70건
- B4p: override 기본값이 None 이다 (소스·런타임, bt 내부 재대입 없음)
- B4q: 판정 경로 창 = 핀 = 1826
- B4r: 참고 경로가 지정값을 그대로 쓴다 (상한 초과 허용, 알림 1회, 해제 후 복귀)
- B4s: override 대입은 허용 목록뿐이다 (AST, 판정 파일 금지)
- `_NEED` 에 두 심볼을 추가했다

**`SATELLITE_MANDATE.md`** — §4① 끝에 사전 약정을 추가했다. §7 에 행 1개를 추가했다 (트리거·룰 불변)

**`automation/diag_satellite_mandate.py`** — 102 → 115건
- J0~J7: md 사전 약정 숫자·룰·필터 = 러너 상수 = 판정 파일 동결 튜플, 기록 탭 분리, 워크플로 배선
- H14~H16: 양성 대조
- 러너는 import 하지 않고 AST 로 읽는다 (가드는 의존성 없이 돌아야 한다)

**`check_freshness.py`** — 지문 표에 §4① 락스텝 4파일을 등록했다 (bt · 룰 비교 · 딥 러너 · SSOT 가드)
- 이번 세션 시작 때 표에 없어서 따로 세어야 했다
- 옛 사본에서 `⚠ 누락` 이 뜨는 것을 확인했다

## 검증

| 검사 | 결과 |
|---|---|
| py_compile · check_py311 | ✅ (46파일) |
| pyflakes | 델타 0 |
| diag_fmp_ssot | ✅ 70건 (기존 66 + B4p~B4s) |
| diag_satellite_mandate | ✅ 115건 (기존 102 + J 10 + H 3) |
| diag_satellite_backtest --selftest · diag_momentum_rule_compare --selftest | ✅ **수정 전후 출력 전량 동일** |
| diag_hist_window · consumers | ✅ 128/128 · 134/134 (출력 동일) |
| diag_momentum_deep_ref --selftest | ✅ 16개 항목 |
| 뮤테이션 | **19 killed / 0 survived** — 사망 사유가 의도한 검사인지 개별 확인 |

역방향 락스텝:

| 조합 | 결과 | 판단 |
|---|---|---|
| 새 러너 + 옛 bt | AttributeError | 시끄러운 실패 |
| 새 SSOT + 옛 bt | `_NEED` 중단 | 시끄러운 실패 |
| 새 가드 + 옛 md | 5건 실패 | 시끄러운 실패 |
| 새 bt + 옛 SSOT | 통과 | 허용 — 대입자가 없다 |
| 새 md + 옛 가드 | 통과 | 허용 |

작성 중 양성 대조가 잡은 약한 검사가 1건 있었다. H15 의 치환 앵커가 §2 의 `mom12_0` 에 먼저 걸려, 대조가 아무것도 안 지키는 상태였다. 앵커를 좁혀 수정했다.

## 배포 순서 (dev 직접 덮어쓰기)

1. `automation/diag_satellite_backtest.py`
2. `automation/diag_fmp_ssot.py`
3. `automation/diag_momentum_deep_ref.py`
4. `.github/workflows/diag_momentum_deep_ref.yml`
5. `SATELLITE_MANDATE.md`
6. `automation/diag_satellite_mandate.py`
7. `check_freshness.py`

Streamlit 리부트 1회 — md 가 앱 위성 섹션에 렌더된다.

## 한계 · 후속

- 스펙 대비 추가된 것: 시트 열 `Truncated` · `Episode_DD_Pct` · `Recv_Bars` · `As_Of`, 수신 깊이 게이트, 셀 정규화
- dividend-adjusted 엔드포인트의 깊이는 미실측이다. 게이트가 걸리면 그 자체가 발견이다
- 타임아웃은 bt 기본 20초를 유지한다. 부족하면 페치율 게이트가 시트 기록 전에 중단시킨다
- 2008 가시성은 롤링 바닥 때문에 매일 줄어든다. 첫 사건은 절삭될 가능성이 높다
- **커밋 ②**: 실행 로그의 수신 창·사건 목록·구간 요약을 §7 에 기록하고, 필요하면 §2 각주를 추가한다
