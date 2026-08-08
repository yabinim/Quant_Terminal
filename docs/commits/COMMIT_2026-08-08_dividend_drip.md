# feat(portfolio): 배당 재투자(DRIP) — 종목별 자동/확인 모드 + 멱등 처리 로그

포트폴리오 보유 종목의 배당을 자동으로 감지해, 종목별 설정에 따라 **재투자(DRIP)** 하거나
**현금으로 적립**한다. 미설정 종목은 앱 접속 시 확인 카드를 띄워 사용자가 직접 선택한다.

---

## 변경 파일

### `fmp_extras.py`
- **`fmp_dividend_history(ticker)` 신규** — 배당 이력 전체를 `ex_date` 오름차순으로 반환.
  `{ex_date, record_date, pay_date, declaration_date, amount, frequency}`
  - 기존 `fmp_dividends()`는 "다가오는 1건"만 줘서 소급 처리가 불가능했다.
- **`fmp_dividends()` 재작성** — 내부 파싱을 `fmp_dividend_history`(SSOT)로 위임.
  - ⚠️ 반환 키(`ex_date` / `amount` / `is_upcoming`)와 선택 규칙(미래 건이 있으면 가장 가까운
    미래, 없으면 가장 최근 과거)은 **불변** → 기존 호출부 호환 유지. `pay_date`·`frequency` 추가.
- `_iso_date_or_blank()` 헬퍼 추가.

### `app.py`
**신규 시트 2종** (Portfolios 6열 스키마는 건드리지 않음)

| 시트 | 컬럼 | 역할 |
|---|---|---|
| `Dividend_Prefs` | `ID, Account, Ticker, Mode, Updated_At` | 종목별 처리 모드 (`ask` / `auto_drip` / `auto_cash`) |
| `Dividend_Log` | `ID, Account, Ticker, Ex_Date, Pay_Date, Per_Share, Shares_Held, Gross_Amount, Action, Reinvest_Price, Reinvest_Shares, Status, Decided_At, Note` | 확정 결정 이력 = 멱등 게이트 |

> **멱등 키 = `(ID, Account, Ticker, Ex_Date)`.** 로그에 있으면 절대 재처리하지 않는다.

**신규 함수**
- 시트 I/O: `open_dividend_prefs_worksheet` / `open_dividend_log_worksheet`,
  `load_dividend_prefs`, `get_dividend_mode`, `save_dividend_pref`(행 단위 upsert),
  `load_dividend_log`, `load_dividend_done_keys`, `append_dividend_log_row`
- **`dividend_shares_at()`** — 배당락일 시점 보유 수량 재구성
  - `Trade_History`를 되감아 계산. 배당 자격은 ex-date **개장 시점** 보유 기준이므로
    체결일이 `ex_date` **미만**인 거래까지만 합산 (ex-date 당일 매수는 배당 없음)
  - `basis` 3종: `ledger`(신뢰) / `ledger_mismatch`(원장 누락 의심) / `current`(매수 원장 없어 폴백)
  - 원장 재구성 현재값과 `Portfolios` 수량이 1% 이상 어긋나면 자동으로 경고 표시
- **`_dividend_reinvest_price()`** — 재투자 체결가 = **지급일 종가**.
  휴장일이면 이후 첫 거래일 종가. 미래·미확보면 `None` → 기록하지 않고 다음 접속에 재시도
- **`scan_pending_dividends()`** — 미처리 배당 목록 (소급 상한 90일, 미래 배당락 제외)
- **`apply_dividend_decision()`** — 확정 반영
  - `drip`: 현금 `+배당` → `Portfolios` 수량·평단 갱신 → `Trade_History` **BUY(−동액)** → 로그
  - `cash`: 현금 `+배당` → `Trade_History` **DIV** 행 → 로그
  - `skip`: 아무것도 바꾸지 않고 로그만 남겨 **재질문 차단**
  - 보유 갱신 실패 시 입금한 현금을 **롤백**

**UI** — `[4단계] 포트폴리오 매도 레이더` 탭 상단
- 미처리 배당이 있으면 경고 배너 + 확인 카드 (`🔁 재투자` / `💵 현금으로` / `🚫 이때 미보유`)
- 자동 모드 종목은 묻지 않고 즉시 반영 후 결과 1줄 표시
- 종목별 기본 처리 방식 설정 폼 + 처리 이력 표 + 누적 수령 배당
- FMP 호출 절감: 스캔은 **세션당 하루 1회**, 결정 직후에만 강제 재스캔

### `automation/diag_dividend_drip.py` (신규 · 읽기 전용)
- `app.py`의 **실제 소스를 AST로 추출해 실행**하는 합성 회귀 테스트 32항목.
  코드 복사본이 아니라 배포될 코드를 그대로 검증하므로 테스트-구현 드리프트가 생기지 않는다.
- 네트워크·Google Sheets 접근 없음. `workflow_dispatch` 전용.

---

## 설계 근거 — 함정 4가지와 대응

| # | 함정 | 대응 |
|---|---|---|
| 1 | 배당락일 시점 보유 수량을 `Portfolios` 스냅샷으로는 알 수 없음 | `Trade_History` 되감기 + 원장 누락 자동 탐지 |
| 2 | **`append_trade_history_row`가 BUY 시 현금을 자동 차감** → 재투자를 BUY로만 기록하면 잔고가 실제로 줄어듦 | 현금 `+배당`을 **먼저** 넣고 BUY로 상쇄. **net 0** |
| 3 | 앱 접속마다 같은 배당을 재처리 | `Dividend_Log` 멱등 키 + 반영 직전 재확인 |
| 4 | 커버드콜 ETF(QQQI·JEPQ)의 ROC 구분을 FMP가 미제공 | UI에 **"장부 근사치"** 명시 |

**Portfolios 스키마 확장을 택하지 않은 이유** — 7번째 열을 붙이면 `portfolio_core.py`(6열)가
깨져 automation 5종을 전부 lockstep 해야 한다. 별도 시트 분리로 **automation 무변경** 달성.

**`DIV` 액션 코드 안전성** — `Trade_History` 소비처 전수 확인 결과 `compute_realized_pnl`,
`compute_closed_trades_detail`, 미매칭 매도 점검, `diag_trade_history.py` 모두 `== "BUY"` /
`== "SELL"` 완전일치라 `DIV` 행은 조용히 무시된다. **실현손익 불변.**

---

## 검증

```
py_compile app.py fmp_extras.py            → OK
automation/diag_dividend_drip.py           → 32/32 통과
```

**변이 테스트(mutation test)로 회귀 테스트의 유효성을 먼저 확인함.**
버그 3종을 일부러 주입했을 때 정확히 해당 항목만 실패:

| 주입한 버그 | 실패한 항목 |
|---|---|
| drip에서 현금 입금 단계 제거 | `현금 net 0` |
| 배당락일 경계를 inclusive로 변경 | `ex_date 당일 매수 제외` |
| 멱등 게이트 제거 | `두 번째 호출 차단`, `현금 불변`, `수량 불변` |

원복 후 전체 재통과 확인.

주요 검증 항목: 배당락일 경계(당일 매수 배제) · 매도 후 배당 0주 · 원장 누락 탐지 ·
매수 원장 없을 때 현재 수량 폴백 · 휴장일 지급일 → 다음 거래일 종가 · 미래 지급일 `None` ·
drip 현금 net 0 · 평단 재계산(총 원가 = 기존 + 배당) · 멱등성 · cash 수량 불변 ·
skip 무변경 · 소급 상한 90일 · 미래 배당락 제외

---

## 배포 순서 (lockstep)

1. `fmp_extras.py` 덮어쓰기
2. `app.py` 덮어쓰기
3. `automation/diag_dividend_drip.py` 추가 *(선택 — 앱 동작과 무관)*
4. **Streamlit 리부트**

`Dividend_Prefs` / `Dividend_Log` 시트는 첫 접속 시 자동 생성 — Google Sheets 수동 작업 없음.
`portfolio_core.py` 및 `automation/run_*.py` **무변경**.

---

## 남은 한계 · 후속 과제

- **ROC 미반영** — QQQI·JEPQ 등 커버드콜 ETF는 배당의 상당분이 원금환급이라 세무상
  평단가를 낮춰야 하지만 FMP가 구분을 주지 않는다. 장부 근사치로만 사용할 것.
- **세전 금액 재투자** — Robinhood 과세계좌 배당은 세전 총액 기준으로 재투자된다.
  Roth IRA·HSA는 비과세라 영향 없음.
- **실제 증권사 DRIP 체결가와 차이** — 지급일 종가로 근사한다.
- **매도 완료 종목 미스캔** — 스캔 유니버스가 현재 보유 종목이라, 보유 중 받았으나 미처리된
  배당은 전량 매도 시 누락된다(예: QQQI). 필요하면 "최근 90일 내 SELL 이력 티커" 추가 — 소규모 변경.
- 자동화(5PM) 사전 계산 후 앱은 표시만 하는 D6-C 구조로의 이행은 안정화 후 검토.
