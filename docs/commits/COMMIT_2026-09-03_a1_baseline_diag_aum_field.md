fix(diag): A1 래칫 기준선에 diag_aum_field 등재 — 관문 둘을 실명시키던 부채 해소

## 왜

`diag_aum_field.py` 는 9-02 세션에서 만든 일회성 프로브다(ETF AUM 필드가
`marketCap` 인지 확인). 원시 `requests` 를 쓰는데 A1 기준선에 등재되지 않아
**"기준선에 없는 신규 우회"** 로 잡혔다.

문제는 빨간불 자체가 아니라 그 파급이다. GitHub Actions 는 스텝이 실패하면
이후 스텝을 건너뛴다. `diag_hist_window_consumers.yml` 에서 `diag_fmp_ssot.py`
가 3번째 스텝이라, 뒤에 있는 두 관문이 **한 번도 실행되지 못했다**:

```
2026-09-03 03:38 실측 (run 54ef8e3)
  ✅ diag_hist_window_consumers.py   125/125
  ✅ diag_hist_window.py             127/127
  ❌ diag_fmp_ssot.py                 44/45   → exit code 1
  —  check_freshness.py                        스킵 (락스텝 업로드 누락 탐지)
  —  check_py311.py                            스킵 (3.11 문법 호환)
```

`check_freshness.py` 는 락스텝 파일이 하나라도 빠졌을 때 **정기 실행 전에 그
사실을 알 수 있는 유일한 자동 관문**이다(yml 주석). 부채 한 건이 그것을 포함해
관문 둘을 조용히 실명시키고 있었다.

## 무엇을 바꿨나

### `automation/diag_fmp_ssot.py` (1,190 → 1,202줄)

`_RAW_GET_BASELINE` 에 한 줄 + 사유 주석:

```python
"diag_aum_field.py": 1,
```

**왜 SSOT 전환이 아니라 예외 등재인가.** 그 파일 63행이 설계 의도를 명시한다 —
`프로젝트 모듈 import 없음 (requests 만 사용) → 사본 신선도와 무관`. `fmp_extras`
나 `fmp_http` 를 임포트하는 순간 그 독립성이 깨지고, **사본이 낡았을 때 API
사실을 확인할 수단이 사라진다.** 9-02 marketCap 프로브가 정확히 그 상황에서
쓰였다. 기준선에 이미 같은 성격의 일회성 진단이 여섯 개 등재돼 있다
(`diag_fmp_endpoints` · `diag_fmp_newcaps` · `diag_industry_momentum` ·
`diag_nodata_cause` · `diag_industry_mapping` · `diag_earnings_preview_backtest`).
이번 항목은 그 여섯과 완전히 같은 모양이다.

부채 총계는 실측 기준이므로 **73곳 / 11개 파일** 로 표시된다(기준선 등재는
집계를 줄이지 않는다 — "알고 있고 아직 안 고쳤다"는 표시일 뿐이다).

## 검증

```
diag_fmp_ssot.py                 45/45  (직전 44/45)
py_compile                       OK
pyflakes 델타                    0건 (원본 0 · 수정본 0)

그동안 스킵되던 두 스텝이 처음으로 실행됨:
  check_freshness.py             exit 0 — 정합성 8모듈 통과
  check_py311.py                 exit 0 — 9개 파일 3.11 호환

워크플로 4스텝 전체:
  diag_hist_window_consumers.py  125/125  exit 0
  diag_hist_window.py            127/127  exit 0
  diag_fmp_ssot.py                45건    exit 0
  check_freshness / check_py311            exit 0
```

**변이 3건 — 등재가 래칫을 이완시키지 않았음을 확인:**

| 변이 | 결과 |
|---|---|
| `diag_aum_field` 에 원시 호출 1건 추가 (1 → 2곳) | ❌ A1 `diag_aum_field.py: 2곳 (기준선 1) — 늘었다` **탐지** |
| 원시 호출 제거 (1 → 0곳) | ✅ 통과 + `⚠️ 기준선 갱신 필요: 0곳 (기준선 1) — 전부 정리됨, 기준선에서 제거할 것` |
| 기준선만 부풀리기 (1 → 5), 코드는 그대로 | ✅ 통과하되 `⚠️ 1곳 (기준선 5) — 줄었다, 기준선을 낮출 것` **경고로 드러남** |

세 번째가 P16(“기준선만 조작”) 계열이다. A1 은 이 방향을 **실패로 만들지는
않지만**(래칫은 위쪽만 막는 설계) 경고로 드러내므로 조용히 넘어가지 않는다.

## 배포

**변경 파일 1개.**

```
automation/diag_fmp_ssot.py      1,202줄 (GitHub 표시 1,201)
```

Streamlit 재부팅 불필요. `diag_aum_field.py` 는 **손대지 않았다** — 코드가 아니라
기준선이 낡아 있었다.

## 롤백

1,190줄본으로 되돌리면 끝. 데이터 손실 없음. 되돌리면 A1 이 다시 빨간불이 되고
`check_freshness` · `check_py311` 두 스텝이 다시 스킵된다.

## 남은 부채

A1 실측 총계 **73곳 / 11개 파일**(0 이 목표). 최대 항목은 `app.py` 62곳 —
`@st.cache_data` 대화형 경로라 한 번에 손대면 회귀 위험이 크다(§6-B5).
`calendar_core.py` 1곳은 **코어 모듈**이라 우선 정리 대상으로 남아 있다 —
`refresh_market_calendar` 가 주말마다 타는 경로인데 429 재시도가 없어 실패하면
캘린더가 조용히 낡는다.
