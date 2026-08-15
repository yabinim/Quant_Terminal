# chore(earnings): C블록 3차 프로브 — insider-trading/search 커버리지

2차 결과로 통계형 3컬럼이 확정 가능해졌다. 그보다 나은 안(달러 환산)이
성립하는지만 확인하고 3단계 C블록을 닫는다.

---

## 2차에서 확정된 것과 남은 것

**확정** — `insider-trading/statistics` 3/3 생존, 종목당 94~102건(약 24년치 분기).
과거 실적 이벤트 소급 계산이 가능해 백테스트에 유리하다.

**2차 로그가 뒤집은 것** — 제안했던 4컬럼 중 2개가 무너졌다.

| 컬럼 | 판정 | 근거 |
|---|---|---|
| `Insider_AD_Ratio` | ❌ 폐기 | AAPL 2026Q1 이 **매수 0 · 매도 0 인데 비율 1.500**. 이 비율은 `totalAcquired/totalDisposed`(전체 주식 수) 기준이라 A-Award·F-InKind·M-Exempt 가 그대로 섞인다. 재량 필터가 걸린 건 `totalPurchases`/`totalSales` 둘뿐 |
| `Insider_Purchases_N` | ❌ 폐기 | 12개 관측(3종목 × 4분기) **전부 0**. 상수 컬럼은 정보가 없다 |
| `Insider_Sales_N` | ✅ 유지 | NVDA 190→86→7→0 등 변동 존재 |
| `Insider_Q` | ✅ 유지 | 3종목 전부 진행 중인 2026Q3(마감 46일 전)을 최신으로 반환 |

**남은 한계 둘**
- 분기 지연 — 8월 이벤트에 6월말 마감 분기를 붙인다 (46~138일 묵음)
- 건수지 금액이 아니다 — `totalSales` 는 거래 건수.
  주식 수(`totalDisposed`)는 부여가 섞여 오염돼 못 쓴다

둘 다 `insider-trading/search` 로 풀린다. 행 단위라 `transactionDate` 와 `price` 가 있고,
2차에서 18종 코드 목록을 확보해 유형 필터도 해결됐다.
**남은 의문은 커버리지 하나** — 1차에서 `limit=20` 이 AAPL 약 2개월, WMT 는 하루밖에 못 덮었다.

---

## 변경 파일

### 신규 `automation/diag_earnings_cblock_probe3.py` (308줄)

총 6콜. 종목당 `page=0`/`page=1` × `limit=100`.

종목별로 넷을 판정하고, **넷이 다 통과해야** 달러 환산이 성립한다고 본다.

| # | 판정 | 깨지면 |
|---|---|---|
| 1 | `page=0` 이 90일을 덮는가 | 종목마다 콜 수가 달라져 상한을 못 정한다 |
| 2 | 창 안 재량(P/S) 비중 | 95%가 F-InKind 잡음이면 실효 커버가 작다 |
| 3 | 재량 행의 `price` 건전성 | 전량 결측이면 환산 불가, 일부면 과소계상 |
| 4 | `page=1` 이 더 과거인가 | 중복이면 창을 완성할 방법이 없다 |

날짜는 `transactionDate` 를 쓴다. `filingDate` 는 신고일이라 거래일보다 늦다
(Form 4 는 2영업일 내 신고). 비어 있는 행은 `filingDate` 로 대체하고 **별도로 센다.**

`I-Discretionary` 는 16b-3(f) 계획 내 재량이라 성격이 달라 P/S 와 분리해 집계한다.

### 신규 `.github/workflows/diag_earnings_cblock_probe3.yml` (67줄)

`workflow_dispatch` 전용. `repository_dispatch` 없음. `GOOGLE_API_KEY` 불필요.

---

## 검증

- `py_compile` ✅ / `check_py311.py` ✅ / YAML 파싱 ✅ (job 1 / step 5)
- **스텁 테스트** 4종:
  A(넓은 커버리지·재량 다수·price 정상) / B(창 미달·페이징 정상) /
  C(price 결측·페이징 중복) / D(402). 판정 분기 전부 의도대로 출력
- **변이 테스트에서 잡은 것** — 초기 구현은 재량 행 **전량** price 결측인 경우도
  "과소계상"으로 판정했다. 전량 결측은 과소계상이 아니라 **환산 자체가 불가능**하다.
  대응이 다르므로(전자는 건수 후퇴, 후자는 보정) 별도 분기로 분리

---

## 배포

공용 모듈 미변경. `app.py`·`earnings_core.py`·`run_earnings_watch.py` 무접촉.
락스텝 대상 없음. Streamlit 리부트 불필요.

    automation/diag_earnings_cblock_probe3.py
    .github/workflows/diag_earnings_cblock_probe3.yml

---

## 결과에 따른 분기

| 결과 | C블록 확정안 | 스냅샷 비용 |
|---|---|---|
| 네 조건 통과 | search 기반 — 실적 전 90일 재량 매도 달러 | 4콜 → 5콜 |
| 하나라도 실패 | 2차 통계형 3컬럼 `Insider_Sales_Q` / `Insider_Sales_TTM4` / `Insider_Q` | 4콜 → 5콜 |

어느 쪽이든 비용은 같다. 정확도만 다르다.

**어느 쪽으로 가든 유보는 그대로다.** 실적 레이더는 방향성 엣지가 확인되지 않은
radar-only 모드이고, 내부자 블록도 검증된 신호가 아니라 **축적할 가설**이다.
`Pre_Ret_D1/D3/D7_Pct` 가 3개월 쌓인 뒤 무의미하면 컬럼째 버린다.

비율 컬럼(Q ÷ TTM4/4)은 어느 안에서도 넣지 않는다.
0·1·4·14 같은 작은 정수로 비율을 만들면 없는 정밀도를 지어내게 된다.
원자료만 저장하고 판정은 백테스트에 맡긴다.
