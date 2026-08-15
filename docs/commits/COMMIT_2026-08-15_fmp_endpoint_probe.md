chore(diag): FMP 엔드포인트 경로 실측 프로브 추가

## 배경

FMP 공식 문서(`api-docs.md`, 278 엔드포인트)를 확보해 기존
`FMP_API_list.pdf`(스크린샷 복붙본, 259행)와 대조했다. 기존 리스트는
**경로 오기 43건 / 존재하지 않는 경로 6건 / 누락 20건**이었고, 그중 **6곳이
실제 코드에 박혀 있다.**

| 코드 위치 | 현재 경로 | 공식 문서 |
|---|---|---|
| `fmp_extras.py:452` | `etf/sector-weighting` | `etf/sector-weightings` |
| `fmp_extras.py:389` | `batch-market-capitalization` | `market-capitalization-batch` |
| `narrative_core.py:220` | `press-releases-latest` | `news/press-releases-latest` |
| `app.py:5507, 14834` | `stock-news?symbols=` | `news/stock?symbols=` |
| `app.py:6012, 7123` | `etf-holder/{sym}` | `etf/holdings?symbol=` |
| `earnings_core.py:339`<br>`app.py:5165`<br>`diag_earnings_preview_backtest.py:91` | `earnings-surprises?symbol=` | 개별 심볼용 없음<br>(`earnings-surprises-bulk?year=` 만 존재) |

여섯 곳 전부 `try-except` 로 예외를 삼킨다. 죽어 있어도 "데이터 없음"으로
보이지 오류로 드러나지 않는다 — `fmp_http.py` 상단에 명시된 실패 모드
그대로다.

특히 `narrative_core.py:220` 의 Layer B 는 4개 뉴스 레이어 중 **가중치가 가장
높다(weight 1.3).** 죽어 있었다면 내러티브 품질이 지속적으로 손상돼 왔다는
뜻이다.

`app.py:6012, 7123` 은 `fmp_extras.py:408` 에 "legacy etf-holder/ 대체"
주석까지 달아놓고 호출부만 고치지 않은 케이스다.

## 왜 바로 고치지 않고 프로브를 먼저 만드나

FMP 가 일부 레거시 경로에 별칭을 유지할 가능성이 있다. 문서에 없다는 것만으로
6곳을 일괄 수정하면, 멀쩡히 동작하던 경로를 건드려 새 회귀를 만들 수 있다.
실측이 먼저다.

## 변경 파일

### `automation/diag_fmp_endpoints.py` (신규)

6쌍(현재 경로 / 공식 경로)을 각각 호출해 원본 상태 코드와 응답 형태를
기록하고, 쌍 단위로 판정한다.

**`fmp_http` 를 쓰지 않는다.** `fmp_http.fmp_get` 은 429/402 에서 재시도한 뒤
`None` 을 돌려주는데, 프로브는 **첫 응답의 원본 상태 코드**가 필요하다
(402 인지 404 인지가 판정을 가른다). 부수 효과로 이 스크립트는 프로젝트
모듈을 하나도 import 하지 않아, 사본 신선도와 무관하게 결과가 재현된다.

판정 분류:

| 결과 | 의미 |
|---|---|
| ✅ `LIVE` | 200 + 비어 있지 않은 응답 |
| ⚠️ `EMPTY` | 200 인데 빈 배열 — **코드가 '데이터 없음'으로 오독하는 경우** |
| ❌ `ERRMSG` | 200 인데 `{"Error Message": ...}` |
| ❌ `404` | 경로 없음 |
| 🔒 `PLAN` | **402 — 경로는 맞으나 이 플랜에 미포함** |
| ⏳ `RATE` / 💥 `EXC` | 판정 불가 — 재실행 |

**`PLAN`(402)과 `404` 를 분리한 것이 핵심이다.** 1차 프로브에서
`analyst-estimates` 와 `earning-call-transcript-*` 가 공식 문서에 있으면서도
이 플랜에서 402 로 죽는 것이 이미 확인됐다. "문서에 있다" ≠ "쓸 수 있다"이며,
둘을 뭉개면 코드 수정으로 해결되지 않는 문제에 코드 수정을 시도하게 된다.

최종 출력은 네 갈래다.

- 🔴 **수정 필수** — 현재 죽음 / 공식 삶 → 경로 교체
- ⚫ **둘 다 실패** — 경로 교체로 해결 안 됨 → 기능 제거·대체 검토
- 🟡 **현행 유지** — 현재 살아 있음(별칭) → 통일 권장, 우선순위 낮음
- ⏳ **판정 불가** — 레이트리밋/네트워크 → 재실행

마지막 줄에 `PROBE_JSON {...}` 한 줄을 남겨 워크플로 로그에서 grep 가능하게
했다.

### `.github/workflows/diag_fmp_endpoints.yml` (신규)

`workflow_dispatch` 전용. **`repository_dispatch` 없음** — 일회성 프로브가
매일 콜을 태우면 안 된다.

- 시트 접근 없음 / 이메일 없음 / 알림 상태 머신 미접촉 → 재실행 부작용 없음
- 비용: 기본 12콜, `extra=true` 시 15콜
- 의존성 `requests` 만 (`pandas` 불필요)

## 검증

| 항목 | 결과 |
|---|---|
| `py_compile` | ✅ |
| `check_py311.py` | ✅ 1개 파일 Python 3.11 호환 |
| YAML 파싱 | ✅ `workflow_dispatch` 단일 트리거, 5 steps |
| 스텁 단위 테스트 | ✅ `LIVE`/`EMPTY`/`ERRMSG`/`404`/`PLAN` 전 분기 통과 |
| 변이 테스트 | ✅ 429·TimeoutError 가 "경로 없음"으로 오독되지 않고 **판정 불가**로 격리 |

변이 테스트를 넣은 이유: 레이트리밋을 경로 오류로 오판하면 멀쩡한 코드를
고치게 된다. 이 프로브에서 가장 위험한 오류 클래스이므로 회귀 대상에 명시했다.

## 배포

신규 파일 2개뿐이며 기존 파일을 건드리지 않는다. **lockstep 불필요.**

1. `automation/diag_fmp_endpoints.py`
2. `.github/workflows/diag_fmp_endpoints.yml`

Streamlit 재부팅 불필요(앱 코드 미변경).

## 남은 한계

- 이 프로브는 **경로 존재 여부만** 본다. 응답 스키마가 코드의 파싱 로직과
  맞는지는 검증하지 않는다. 예컨대 `etf/sector-weightings` 가 살아 있어도
  `weightPercentage` 필드명이 바뀌었다면 여전히 조용히 실패한다. 프로브가
  첫 항목의 키 목록을 출력하는 이유이며, 수정 시 육안 대조가 필요하다.
- 402 로 판정된 항목은 코드 수정 대상이 아니라 **설계 변경 대상**이다.
- 기존 `FMP_API_list.pdf` 는 폐기됨. 이후 엔드포인트 SSOT 는 `api-docs.pdf`.
