"""backfill_insider_stats.py — 과거 실적 이벤트에 내부자 분기 통계를 소급 결합.

왜 별도 스크립트인가
────────────────────
  라이브 스냅샷(run_earnings_watch pass_preview)은 insider-trading/search 를 쓴다.
  행 단위라 날짜·price 가 있어 **최근 90일 달러**를 정확히 낸다. 대신 100행 ≈ 1년치다.

  insider-trading/statistics 는 반대다. 종목당 94~102개 분기(약 24년치)를 1콜에
  주지만 분기 집계라 지연되고 금액이 아니라 건수다.

  → 라이브는 search, **소급 검증은 statistics**. 그래서 분리했다.
     이 스크립트가 없으면 "내부자 매도가 실적 반응과 관계있나"를 알기까지
     라이브 스냅샷이 3개월 쌓이기를 기다려야 한다. statistics 로 백필하면
     몇 주 안에 읽을 수 있다.

무엇을 하는가
─────────────
  1. Earnings_Events 를 읽는다 (읽기 전용 — 절대 쓰지 않는다)
  2. 종목별 insider-trading/statistics 1콜
  3. 각 이벤트마다 **발표일 이전에 마감된 가장 최근 분기**를 고른다
     · 진행 중인 분기는 쓰지 않는다. totalSales 가 마감까지 계속 늘어나
       같은 분기라도 조회 시점마다 값이 달라진다
     · 발표일 이후 분기도 쓰지 않는다 — 미래 정보 누출이다
  4. 새 시트 Insider_Backfill 에 결과 + Pre_Ret_D1/D3/D7 을 나란히 적재

  **Earnings_Events 스키마는 건드리지 않는다.** 21열 그대로 두고 별도 시트에
  쓴다. 일회성 분석 산출물이 본 스키마를 오염시키면 안 된다.

비용: 종목당 1콜. MAX_TICKERS 로 상한을 건다(기본 150).

실행
────
    python automation/backfill_insider_stats.py
    MAX_TICKERS=20 DRY_RUN=1 python automation/backfill_insider_stats.py
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

import earnings_core as ec  # noqa: E402

# gspread / google-auth / gs_retry 는 _open_db() 안에서만 import 한다.
# 순수 함수(pick_closed_quarter 등)를 diag 가 시트 의존성 없이 검증할 수 있어야
# 하기 때문이다. 모듈 최상단에 두면 회귀 스위트가 통째로 죽는다.

FMP_API_KEY = str(os.environ.get("FMP_API_KEY", "") or "").strip()
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", "150") or 150)
DRY_RUN = str(os.environ.get("DRY_RUN", "") or "").strip() not in ("", "0")

BACKFILL_WORKSHEET = "Insider_Backfill"
BACKFILL_COLS = [
    "Event_ID", "Ticker", "Earnings_Date",
    "Insider_Q",            # 어느 분기를 썼나 (마감된 분기만)
    "Insider_Q_End",        # 그 분기 종료일
    "Insider_Lag_D",        # 발표일까지 며칠 묵었나 — 신선도 보정용
    "Insider_Sales_Q",      # 재량 매도 건수 (totalSales)
    "Insider_Purch_Q",      # 재량 매수 건수 (totalPurchases)
    "Insider_Sales_TTM4",   # 직전 4개 마감 분기 합 — 그 종목의 평소 수준
    # 결과 — Earnings_Events 에서 그대로 옮겨 적는다 (재계산 금지)
    "Gap_Pct", "Pre_Ret_D1_Pct", "Pre_Ret_D3_Pct", "Pre_Ret_D7_Pct",
]


def _q_end(y, q):
    """분기 종료일 Timestamp. 값이 이상하면 None."""
    try:
        y, q = int(y), int(q)
    except (TypeError, ValueError):
        return None
    if q not in (1, 2, 3, 4):
        return None
    try:
        return pd.Timestamp(year=y, month=q * 3, day=1) + pd.offsets.MonthEnd(0)
    except Exception:
        return None


def fetch_insider_quarters(ticker: str, key: str = "") -> list[dict]:
    """insider-trading/statistics → [{end, y, q, sales, purch}] 최신순.

    분기 표기를 해석하지 못한 행은 버린다. 순서를 추측해 채우면
    '마감된 가장 최근 분기' 선택이 조용히 틀린다.
    """
    tk = str(ticker or "").strip().upper()
    if not tk:
        return []
    items = ec._get(f"insider-trading/statistics?symbol={tk}", key)
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        end = _q_end(it.get("year"), it.get("quarter"))
        if end is None:
            continue
        out.append({
            "end": end,
            "y": int(it.get("year")), "q": int(it.get("quarter")),
            "sales": ec._num(it.get("totalSales")),
            "purch": ec._num(it.get("totalPurchases")),
        })
    out.sort(key=lambda x: x["end"], reverse=True)
    return out


def pick_closed_quarter(quarters: list, ed) -> tuple:
    """발표일 이전에 **마감된** 가장 최근 분기와 직전 4분기 합.

    반환 (선택분기 or None, TTM4 합 or None)

    진행 중인 분기를 제외하는 이유 — 마감 전 값은 계속 늘어난다.
    발표일 이후 분기를 제외하는 이유 — 그건 그 시점에 알 수 없던 정보다.
    백테스트에서 미래를 보면 결과가 전부 무의미해진다.
    """
    d = ec._d(ed)
    if d is None or not quarters:
        return None, None
    closed = [q for q in quarters if q["end"] < d]
    if not closed:
        return None, None
    pick = closed[0]
    vals = [q["sales"] for q in closed[:4] if q["sales"] is not None]
    ttm4 = sum(vals) if len(vals) == 4 else None
    return pick, ttm4


def _open_db():
    import json

    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ.get("GSPREAD_KEY", "") or ""
    if not raw:
        raise RuntimeError("GSPREAD_KEY 없음")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds).open("Quant_DB")


def _col_a1(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
    import gs_retry as gsr

    if not FMP_API_KEY:
        print("❌ FMP_API_KEY 없음 — 종료")
        return 0

    sh = _open_db()

    # ── 1. Earnings_Events 읽기 (읽기 전용) ──
    ews = gsr.call(sh.worksheet, ec.EVENTS_WORKSHEET, _label="ws(events)")
    vals = gsr.call(ews.get_all_values, _label="Earnings_Events") or []
    if len(vals) < 2:
        print("⚠️ Earnings_Events 가 비어 있다 — 백필할 것이 없다")
        return 0
    hdr = [str(c).strip() for c in vals[0]]
    idx = {c: i for i, c in enumerate(hdr)}

    def cell(r, name):
        i = idx.get(name)
        return "" if i is None or i >= len(r) else str(r[i]).strip()

    events = []
    for r in vals[1:]:
        tk = cell(r, "Ticker").upper()
        ed = cell(r, "Earnings_Date")
        if tk and ed:
            events.append({"tk": tk, "ed": ed, "row": r})
    print(f"이벤트 {len(events)}건 · 종목 {len({e['tk'] for e in events})}개")

    # ── 2. 종목별 1콜 ──
    tickers = sorted({e["tk"] for e in events})
    if len(tickers) > MAX_TICKERS:
        print(f"⚠️ 종목 {len(tickers)}개 > 상한 {MAX_TICKERS} — 앞 {MAX_TICKERS}개만 처리")
        tickers = tickers[:MAX_TICKERS]
    keep = set(tickers)

    qcache, n_fail = {}, 0
    for i, tk in enumerate(tickers, 1):
        try:
            qs = fetch_insider_quarters(tk, key=FMP_API_KEY)
            qcache[tk] = qs
            if not qs:
                n_fail += 1
            if i % 20 == 0:
                print(f"  ... {i}/{len(tickers)}")
        except Exception as e:
            n_fail += 1
            print(f"  [WARN] {tk} 실패: {e}")
    print(f"조회 완료 · 분기 데이터 없음 {n_fail}종목")

    # ── 3. 이벤트별 결합 ──
    rows, n_skip = [], 0
    for e in events:
        if e["tk"] not in keep:
            continue
        pick, ttm4 = pick_closed_quarter(qcache.get(e["tk"]) or [], e["ed"])
        if pick is None:
            n_skip += 1
            continue
        d = ec._d(e["ed"])
        lag = "" if d is None else int((d - pick["end"]).days)
        rows.append([
            ec.event_id(e["tk"], e["ed"]), e["tk"], e["ed"],
            f"{pick['y']}Q{pick['q']}", pick["end"].strftime("%Y-%m-%d"), lag,
            ec._blank(pick["sales"]), ec._blank(pick["purch"]),
            ec._blank(ttm4),
            cell(e["row"], "Gap_Pct"),
            cell(e["row"], "Pre_Ret_D1_Pct"),
            cell(e["row"], "Pre_Ret_D3_Pct"),
            cell(e["row"], "Pre_Ret_D7_Pct"),
        ])
    print(f"결합 {len(rows)}행 · 마감 분기 없어 제외 {n_skip}건")

    if not rows:
        print("⚠️ 기록할 행이 없다")
        return 0

    # 신선도 분포 — 이 백필을 믿어도 되는지 판단할 근거
    lags = [r[5] for r in rows if isinstance(r[5], int)]
    if lags:
        lags.sort()
        print(f"분기 지연 중앙값 {lags[len(lags) // 2]}일 "
              f"(최소 {lags[0]} · 최대 {lags[-1]})")

    if DRY_RUN:
        print("\n[DRY_RUN] 시트에 쓰지 않는다. 앞 5행:")
        for r in rows[:5]:
            print("  ", r)
        return 0

    # ── 4. 새 시트에 적재 (Earnings_Events 는 건드리지 않는다) ──
    try:
        bws = gsr.call(sh.worksheet, BACKFILL_WORKSHEET, _label="ws(backfill)")
        gsr.call(bws.clear, _label="clear(backfill)")
    except Exception:
        bws = gsr.call(sh.add_worksheet, title=BACKFILL_WORKSHEET,
                       rows=max(len(rows) + 10, 200), cols=len(BACKFILL_COLS),
                       _label="add_worksheet(backfill)")
        print(f"[INIT] '{BACKFILL_WORKSHEET}' 시트 생성")

    end = _col_a1(len(BACKFILL_COLS))
    gsr.call(bws.update, [BACKFILL_COLS], range_name=f"A1:{end}1",
             value_input_option="USER_ENTERED", _label="header(backfill)")
    gsr.call(bws.update, rows,
             range_name=f"A2:{end}{len(rows) + 1}",
             value_input_option="USER_ENTERED", _label="rows(backfill)")
    print(f"[OK] '{BACKFILL_WORKSHEET}' {len(rows)}행 기록")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
