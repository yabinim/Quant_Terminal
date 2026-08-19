# -*- coding: utf-8 -*-
"""diag_dividend_drip.py — 배당 DRIP 합성 회귀 테스트 — app.py 의 **실제 소스**를 추출해 실행한다.

복사본이 아니라 배포될 코드를 그대로 exec 하므로, 배포본만 고치고 테스트를
안 고치는 드리프트가 생기지 않는다. 네트워크·시트 접근은 전부 스텁.
"""
import ast
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz

SRC = open("app.py", encoding="utf-8").read()
TREE = ast.parse(SRC)
LINES = SRC.splitlines(keepends=True)

WANT = {"dividend_shares_at", "_dividend_reinvest_price", "apply_dividend_decision",
        "scan_pending_dividends", "get_dividend_mode"}


def grab(name):
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef,)) and node.name == name:
            start = node.lineno - 1
            # 데코레이터가 있으면 그 위부터
            if node.decorator_list:
                start = min(d.lineno for d in node.decorator_list) - 1
            return "".join(LINES[start:node.end_lineno])
    raise KeyError(name)


# ── 실행 네임스페이스 (스텁) ────────────────────────────────────────────────
CASH = {}          # (uid, acct) -> float
TRADES = []        # append_trade_history_row 호출 기록
PORTFOLIO = {}     # (acct, ticker) -> [qty, avg]
LOGROWS = []
HIST = {}          # ticker -> DataFrame


def adjust_account_cash(uid, account, delta, note=""):
    CASH[(uid, account)] = CASH.get((uid, account), 0.0) + float(delta)
    return True, CASH[(uid, account)]


def append_trade_history_row(uid, account, ticker, action, shares, price, date, memo=""):
    TRADES.append(dict(uid=uid, account=account, ticker=ticker, action=action,
                       shares=float(shares), price=float(price), date=date, memo=memo))
    # ⚠️ 실제 app.py 와 동일하게 BUY 는 현금을 차감한다 — 이 동작을 재현하지 않으면
    #    함정 2(현금 이중 차감)를 테스트가 잡아내지 못한다.
    act = str(action).strip().upper()
    amt = float(shares) * float(price)
    if act == "BUY" and amt > 0:
        adjust_account_cash(uid, account, -amt, note="stub buy")
    elif act == "SELL" and amt > 0:
        adjust_account_cash(uid, account, +amt, note="stub sell")
    return True, ""


def load_portfolio():
    return pd.DataFrame([
        {"Account": a, "Ticker": t, "Purchase_Price": v[1], "Quantity": v[0]}
        for (a, t), v in PORTFOLIO.items()
    ])


def save_portfolio(df):
    PORTFOLIO.clear()
    for _, r in df.iterrows():
        PORTFOLIO[(str(r["Account"]), str(r["Ticker"]))] = [float(r["Quantity"]),
                                                            float(r["Purchase_Price"])]


def append_dividend_log_row(uid, item, action, status, reinvest_price=None,
                            reinvest_shares=None, note=""):
    LOGROWS.append(dict(uid=uid, account=item["account"], ticker=item["ticker"],
                        ex=item["ex_date"], action=action, status=status,
                        price=reinvest_price, shares=reinvest_shares))
    return True, ""


def load_dividend_done_keys(uid):
    return {(r["account"].lower(), r["ticker"], r["ex"]) for r in LOGROWS if r["uid"] == uid}


def _fmp_price_history(ticker, limit=260):
    return HIST.get(str(ticker).upper(), pd.DataFrame())


def _cached_dividend_history(ticker):
    return DIVHIST.get(str(ticker).upper(), [])


def load_dividend_prefs(uid):
    return PREFS


DIVHIST, PREFS = {}, {}
_MARKET_ET_TZ = pytz.timezone("America/New_York")
_DIVIDEND_MODES = ("ask", "auto_drip", "auto_cash")
_DIVIDEND_MODE_DEFAULT = "ask"
_DIVIDEND_BACKFILL_DAYS = 90
_DIVIDEND_TRADE_ACTION = "DIV"
_DIVIDEND_MEMO_TAG_DRIP = "[배당재투자]"
_DIVIDEND_MEMO_TAG_CASH = "[배당현금]"

NS = dict(globals())
for fn in WANT:
    exec(grab(fn), NS)
NS["load_dividend_done_keys"] = load_dividend_done_keys

dividend_shares_at = NS["dividend_shares_at"]
_dividend_reinvest_price = NS["_dividend_reinvest_price"]
apply_dividend_decision = NS["apply_dividend_decision"]
scan_pending_dividends = NS["scan_pending_dividends"]

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def mk_trades(rows):
    return pd.DataFrame(rows, columns=["user_id", "account", "ticker", "action",
                                       "shares", "price", "date", "memo"])


print("\n[1] dividend_shares_at — 배당락일 경계 (ex_date 당일 매수는 배당 없음)")
pf = pd.DataFrame([{"Account": "Roth", "Ticker": "SCHD",
                    "Purchase_Price": 27.0, "Quantity": 130.0}])
tr = mk_trades([
    ["yab", "Roth", "SCHD", "BUY", 100.0, 26.0, "2026-01-10", ""],
    ["yab", "Roth", "SCHD", "BUY", 30.0, 28.0, "2026-06-24", ""],   # 배당락 전날
])
r = dividend_shares_at(tr, pf, "Roth", "SCHD", "2026-06-24")
check("ex_date 당일 매수 제외 (100주)", abs(r["shares"] - 100.0) < 1e-9, f"{r}")
r2 = dividend_shares_at(tr, pf, "Roth", "SCHD", "2026-06-25")
check("ex_date 전날 매수 포함 (130주)", abs(r2["shares"] - 130.0) < 1e-9, f"{r2}")
check("원장 정합 → basis=ledger", r2["basis"] == "ledger", f"{r2}")

print("\n[2] dividend_shares_at — 매도 반영 + 원장 누락 탐지")
tr_sell = mk_trades([
    ["yab", "Roth", "QQQI", "BUY", 50.0, 50.0, "2026-01-05", ""],
    ["yab", "Roth", "QQQI", "SELL", 50.0, 55.0, "2026-05-01", ""],
])
pf_sold = pd.DataFrame([{"Account": "Roth", "Ticker": "QQQI",
                         "Purchase_Price": 50.0, "Quantity": 0.0}])
r3 = dividend_shares_at(tr_sell, pf_sold, "Roth", "QQQI", "2026-06-20")
check("전량 매도 후 배당락 → 0주", abs(r3["shares"]) < 1e-9, f"{r3}")
r4 = dividend_shares_at(tr_sell, pf_sold, "Roth", "QQQI", "2026-03-20")
check("매도 전 배당락 → 50주", abs(r4["shares"] - 50.0) < 1e-9, f"{r4}")

pf_bad = pd.DataFrame([{"Account": "Roth", "Ticker": "SCHD",
                        "Purchase_Price": 27.0, "Quantity": 500.0}])  # 원장과 불일치
r5 = dividend_shares_at(tr, pf_bad, "Roth", "SCHD", "2026-06-25")
check("원장 누락 탐지 → ledger_mismatch", r5["basis"] == "ledger_mismatch", f"{r5}")

print("\n[3] dividend_shares_at — 매수 원장 없음 → 현재 수량 폴백")
r6 = dividend_shares_at(mk_trades([]), pf, "Roth", "SCHD", "2026-06-25")
check("basis=current", r6["basis"] == "current" and abs(r6["shares"] - 130.0) < 1e-9, f"{r6}")

print("\n[4] _dividend_reinvest_price — 휴장일 → 다음 거래일 종가")
idx = pd.to_datetime(["2026-06-26", "2026-06-29", "2026-06-30"])
HIST["SCHD"] = pd.DataFrame({"Close": [27.0, 27.5, 28.0]}, index=idx)
px, pdate, note = _dividend_reinvest_price("SCHD", "2026-06-27", "2026-06-25")  # 토요일
check("휴장일 → 다음 거래일", abs(px - 27.5) < 1e-9 and pdate == "2026-06-29", f"{px} {pdate}")
px2, _, _ = _dividend_reinvest_price("SCHD", "2026-06-30", "2026-06-25")
check("정확히 일치하는 날", abs(px2 - 28.0) < 1e-9, f"{px2}")
px3, _, n3 = _dividend_reinvest_price("SCHD", "2026-12-31", "2026-06-25")  # 미래
check("미래 지급일 → None (기록 안 함)", px3 is None, f"{px3} {n3}")

print("\n[5] apply_dividend_decision(drip) — 🔥 현금 net 0 (함정 2)")
CASH.clear(); TRADES.clear(); LOGROWS.clear(); PORTFOLIO.clear()
CASH[("yab", "Roth")] = 1000.0
PORTFOLIO[("Roth", "SCHD")] = [130.0, 27.0]
item = dict(account="Roth", ticker="SCHD", ex_date="2026-06-25", pay_date="2026-06-30",
            per_share=0.27, shares_held=130.0, gross=35.10, price=28.0,
            price_date="2026-06-30", price_note="")
ok, err = apply_dividend_decision("yab", item, "drip")
check("반영 성공", ok, err)
check("현금 net 0 (1000 유지)", abs(CASH[("yab", "Roth")] - 1000.0) < 1e-6,
      f"실제 {CASH[('yab','Roth')]}")
exp_sh = 35.10 / 28.0
check("수량 증가", abs(PORTFOLIO[("Roth", "SCHD")][0] - (130.0 + exp_sh)) < 1e-6,
      f"{PORTFOLIO[('Roth','SCHD')]}")
exp_avg = (27.0 * 130.0 + 35.10) / (130.0 + exp_sh)
check("평단 재계산", abs(PORTFOLIO[("Roth", "SCHD")][1] - exp_avg) < 1e-6,
      f"{PORTFOLIO[('Roth','SCHD')][1]} vs {exp_avg}")
check("Trade_History BUY 1건", len(TRADES) == 1 and TRADES[0]["action"] == "BUY", f"{TRADES}")
check("총 원가 = 기존 + 배당", abs(PORTFOLIO[("Roth", "SCHD")][0]
                                * PORTFOLIO[("Roth", "SCHD")][1]
                                - (27.0 * 130.0 + 35.10)) < 1e-6)

print("\n[6] 멱등성 — 같은 배당 재처리 차단")
cash_before = CASH[("yab", "Roth")]
qty_before = PORTFOLIO[("Roth", "SCHD")][0]
ok2, msg2 = apply_dividend_decision("yab", item, "drip")
check("두 번째 호출 차단", ok2 and "이미" in msg2, msg2)
check("현금 불변", abs(CASH[("yab", "Roth")] - cash_before) < 1e-9)
check("수량 불변", abs(PORTFOLIO[("Roth", "SCHD")][0] - qty_before) < 1e-9)

print("\n[7] apply_dividend_decision(cash) — 현금만 +, 수량 불변")
CASH.clear(); TRADES.clear(); LOGROWS.clear(); PORTFOLIO.clear()
CASH[("yab", "Robinhood")] = 500.0
PORTFOLIO[("Robinhood", "QQQI")] = [40.0, 50.0]
item_c = dict(account="Robinhood", ticker="QQQI", ex_date="2026-07-22",
              pay_date="2026-07-31", per_share=0.55, shares_held=40.0, gross=22.0,
              price=51.0, price_date="2026-07-31", price_note="")
ok3, err3 = apply_dividend_decision("yab", item_c, "cash")
check("반영 성공", ok3, err3)
check("현금 +22", abs(CASH[("yab", "Robinhood")] - 522.0) < 1e-6,
      f"{CASH[('yab','Robinhood')]}")
check("수량 불변", abs(PORTFOLIO[("Robinhood", "QQQI")][0] - 40.0) < 1e-9)
check("DIV 액션 기록 (BUY/SELL 아님)", TRADES[0]["action"] == "DIV", f"{TRADES}")

print("\n[8] skip — 아무것도 바꾸지 않음")
CASH.clear(); TRADES.clear(); LOGROWS.clear()
CASH[("yab", "Roth")] = 100.0
item_s = dict(account="Roth", ticker="SCHD", ex_date="2026-03-25", pay_date="2026-03-30",
              per_share=0.26, shares_held=10.0, gross=2.6, price=27.0,
              price_date="2026-03-30", price_note="")
ok4, _ = apply_dividend_decision("yab", item_s, "skip")
check("성공", ok4)
check("현금 불변", abs(CASH[("yab", "Roth")] - 100.0) < 1e-9)
check("거래 기록 없음", len(TRADES) == 0)
check("로그는 남음(재질문 차단)", len(LOGROWS) == 1 and LOGROWS[0]["status"] == "skipped")

print("\n[9] scan_pending_dividends — 소급 상한 · 미래 배당락 제외 · 멱등")
LOGROWS.clear()
today = datetime.now(_MARKET_ET_TZ).date()
DIVHIST["SCHD"] = [
    {"ex_date": (today - timedelta(days=200)).isoformat(), "pay_date": "", "amount": 0.25,
     "frequency": "Quarterly"},                                        # 소급 밖
    {"ex_date": (today - timedelta(days=30)).isoformat(),
     "pay_date": (today - timedelta(days=25)).isoformat(), "amount": 0.27,
     "frequency": "Quarterly"},                                        # 대상
    {"ex_date": (today + timedelta(days=30)).isoformat(), "pay_date": "", "amount": 0.28,
     "frequency": "Quarterly"},                                        # 미래
]
d0 = today - timedelta(days=25)
HIST["SCHD"] = pd.DataFrame({"Close": [28.0]}, index=pd.to_datetime([d0.isoformat()]))
pf9 = pd.DataFrame([{"Account": "Roth", "Ticker": "SCHD",
                     "Purchase_Price": 27.0, "Quantity": 130.0}])
tr9 = mk_trades([["yab", "Roth", "SCHD", "BUY", 130.0, 27.0, "2026-01-10", ""]])
items = scan_pending_dividends("yab", pf9, tr9, {}, set())
check("대상 1건만", len(items) == 1, f"{[i['ex_date'] for i in items]}")
if items:
    check("금액 = 0.27 × 130", abs(items[0]["gross"] - 0.27 * 130.0) < 1e-6)
    check("ready=True", items[0]["ready"] is True)
    check("기본 모드 ask", items[0]["mode"] == "ask")
    done = {("roth", "SCHD", items[0]["ex_date"])}
    check("done_keys 로 제외됨",
          len(scan_pending_dividends("yab", pf9, tr9, {}, done)) == 0)

print("\n[10] scan — 배당락일 미보유(매수 이후) 종목은 질문조차 안 함")
tr10 = mk_trades([["yab", "Roth", "SCHD", "BUY", 130.0, 27.0, today.isoformat(), ""]])
check("0건", len(scan_pending_dividends("yab", pf9, tr10, {}, set())) == 0)

print("\n" + "=" * 60)
if FAILS:
    print(f"❌ 실패 {len(FAILS)}건: {FAILS}")
    sys.exit(1)
print("✅ 전체 통과")
