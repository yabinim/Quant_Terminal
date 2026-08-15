# chore(earnings): 3단계 C블록 대체 후보 생존 확인 프로브 추가

트랜스크립트 402 로 C블록 원안이 불가능해졌다. 대체 후보를 **고르기 전에**
이 요금제에서 실제로 살아 있는지부터 확인한다. 설계 확정 전이라 되돌릴 것이 없다.

---

## 변경 파일

### 신규 `automation/diag_earnings_cblock_probe.py` (245줄)

일회성 프로브. 종목당 5개 경로를 찔러 status/kind 와 실제 반환 필드를 찍는다.

| 후보 | 경로 | 무엇을 보는가 |
|---|---|---|
| 다-1 | `news/press-releases?symbols=` | 회사 원문 보도자료 — **본문 필드 존재·길이** |
| 다-2 | `insider-trading/search?symbol=` | 실적 전 내부자 순매수/매도 (수치) |
| 다-2b | `insider-trade-statistics?symbol=` | 위의 집계판 — 1콜로 끝나면 이쪽이 싸다 |
| 다-3 | `sec-filings-search/symbol?symbol=&from=&to=` | 실적 전 120일 8-K 건수/유형 (수치) |
| (나) | `news/stock?symbols=` | **본문(text) 필드가 정말 오는가** |

마지막 항목을 넣은 이유: 핸드오프의 (나)안은 `news/stock` 에 본문이 있다고
전제하지만, 현재 `earnings_core.fetch_stock_news` 는 `date/title/site/url` 넷만
뽑는다. 본문 필드는 **아무도 확인한 적이 없다.** (나)안 자신이
"호출부가 있다 ≠ 이 플랜에서 동작한다" 오류에 걸려 있다.

**판정 분기 5종**
- `402` 전 종목 → 후보에서 제거
- 호출 실패(404 등) → 경로·파라미터 규약 재확인 필요 (즉사 아님)
- `200` + 전 종목 0건 → 사실상 사용 불가
- `200` + 본문 없음 → 서술 후보 탈락
- `200` + 본문 `BODY_MIN_USEFUL`(400자) 미만 → **발췌지 전문이 아님. 사실상 탈락**
- `200` + 본문 400자 이상 → 서술 후보 성립. 길이가 셀 예산(49,000자)과 요약 비용을 결정

기존 `diag_earnings_revenue_field.py` 관례를 그대로 따랐다 —
`sys.path` 자체 부트스트랩, `fmp_get_json_ex` 로 status/kind 구분,
말미 `fmp_stats_line()`, 종료 코드 항상 0.

응답 형태가 리스트든 `{data:[...]}` 든 `_rows()` 로 정규화한다.
필드명은 후보 목록으로 넓게 잡아 FMP 표기 변경에 견디게 했다.

### 신규 `.github/workflows/diag_earnings_cblock_probe.yml` (62줄)

`workflow_dispatch` **전용**. `repository_dispatch` 없음 —
일회성 프로브가 매일 콜을 태우면 안 된다.
`pandas` 미설치(날짜는 `datetime` 으로 계산), `GOOGLE_API_KEY` 불필요.

---

## 검증

- `py_compile` ✅
- `check_py311.py` ✅
- YAML 파싱 ✅ (job 1개 / step 5개)
- **스텁 테스트** — `fmp_http` 를 가짜로 바꿔 3개 시나리오 실행:
  A(전부 생존) / B(본문 없음 + 404 혼재) / C(전부 402).
  판정 분기 5종이 모두 의도대로 출력됨
- **변이 테스트** — 초기 구현은 120자 blurb 를 "서술 후보 성립"으로 오판했다.
  `BODY_MIN_USEFUL` 임계값을 넣어 "발췌다, 사실상 탈락"으로 뒤집히는 것을 확인.
  이 프로브가 막아야 할 오독이 정확히 이 유형이다

---

## 배포

공용 모듈 미변경. `app.py`·`earnings_core.py`·`run_earnings_watch.py` 무접촉.
락스텝 대상 없음. Streamlit 리부트 불필요.

    automation/diag_earnings_cblock_probe.py
    .github/workflows/diag_earnings_cblock_probe.yml

GitHub 직접 덮어쓰기 → Actions 에서 수동 실행.

---

## 남은 한계

- 프로브는 **경로 생존과 필드 존재**만 본다. 데이터 품질(보도자료가 어닝
  보도자료인지, 내부자 거래가 유의미한 빈도인지)은 결과를 보고 판단한다
- 직전 어닝 보도자료를 날짜로 골라내는 로직은 후보 확정 후 구현한다
- 13F(`institutional-ownership`)는 제출이 45일 지연이라 실적 이벤트에는
  이미 낡은 정보다. 후보에서 뺐다
