# -*- coding: utf-8 -*-
"""diag_pfstate_align.py — Portfolio_Alert_State 키/값 정렬 회귀 스위트.

검증 대상: run_watchlist_alerts.eval_portfolio_eod 의 **실제 함수**를 실행한다.
로직을 테스트에 복사하지 않는다(복사하면 거짓 통과가 난다).
gspread/FMP 는 가짜 객체로 대체하고, 시트 쓰기 결과만 검사한다.

핵심 불변식:
  쓰기 후 모든 행에서 (Key, Stop_Loss, Target_Price) 의 짝이
  쓰기 전과 동일해야 한다 — Portfolios 행 순서가 어떻게 바뀌든.

사용법:
  FMP_API_KEY=x GSPREAD_KEY='{}' GMAIL_USER=a GMAIL_APP_PASSWORD=b \\
  GMAIL_TO=c python3 diag_pfstate_align.py
"""
import os
import sys

for _k, _v in (("FMP_API_KEY", "x"), ("GSPREAD_KEY", "{}"), ("GMAIL_USER", "a"),
               ("GMAIL_APP_PASSWORD", "b"), ("GMAIL_TO", "c")):
    os.environ.setdefault(_k, _v)

import numpy as np
import pandas as pd

import run_watchlist_alerts as m

TODAY = "2026-08-24"
PASS, FAIL = [], []


# ── 가짜 gspread ──────────────────────────────────────────────────────────
class FakeWS:
    def __init__(self, title, values):
        self.title = title
        self._v = [list(r) for r in values]
        self.writes = []

    def get_all_values(self):
        return [list(r) for r in self._v]

    def update(self, body, range_name=None, value_input_option=None):
        self.writes.append((range_name, [list(r) for r in body]))
        # 실제 시트처럼 반영: 지정 범위 폭만큼만 덮어쓰고 나머지 열은 유지
        width = len(body[0]) if body else 0
        for i, row in enumerate(body):
            while len(self._v) <= i:
                self._v.append([])
            cur = self._v[i]
            while len(cur) < width:
                cur.append("")
            cur[:width] = list(row) + [""] * (width - len(row))

    def add_worksheet(self, *a, **k):
        raise AssertionError("테스트 중 시트 생성 시도")


class FakeSH:
    def __init__(self, wss):
        self._wss = wss

    def worksheets(self):
        return list(self._wss.values())

    def worksheet(self, t):
        return self._wss[t]


def _hist():
    n = 300
    idx = pd.bdate_range(end="2026-08-22", periods=n)
    close = pd.Series(np.linspace(100.0, 130.0, len(idx)), index=idx)
    return pd.DataFrame({"Open": close, "High": close * 1.01,
                         "Low": close * 0.99, "Close": close,
                         "Volume": [1_000_000] * len(idx)}, index=idx)


def run_case(name, pf_rows, state_rows, expect_pairs, nodata_tickers=()):
    """eval_portfolio_eod 실행 후 (Key, E, F) 짝을 검사."""
    pf_ws = FakeWS(m._PF_WORKSHEET,
                   [["ID", "Account", "Ticker", "AvgPrice", "Quantity", "Date_Added"]] + pf_rows)
    st_ws = FakeWS(m._PFSTATE_WORKSHEET, state_rows)
    sh = FakeSH({m._PF_WORKSHEET: pf_ws, m._PFSTATE_WORKSHEET: st_ws})

    saved = {k: getattr(m, k) for k in ("_open_ws", "_open_pf_state", "_pf_hist",
                                        "record_attempt", "record_nodata")}
    m._open_ws = lambda t: (sh, pf_ws)
    m._open_pf_state = lambda _sh: st_ws
    # ⚠️ 시그니처는 run_watchlist_alerts._pf_hist 와 락스텝이다.
    #    2026-08-28 에 date_added·today 가 붙었다(조회 창이 Date_Added 에서 결정됨).
    #    스텁이 낡으면 TypeError 가 try 블록에 먹혀 '평가 실패' WARN 만 남고
    #    스위트는 7/7 초록을 유지한다 — 아무것도 검증하지 않는 가짜 초록불이다.
    m._pf_hist = (lambda tk, cache, date_added=None, today=None:
                  (None if tk in nodata_tickers else _hist()))
    m.record_attempt = lambda: None
    m.record_nodata = lambda *a, **k: None
    try:
        m.eval_portfolio_eod(spy_close=_hist()["Close"], hist_cache={}, today=TODAY)
    finally:
        for k, v in saved.items():
            setattr(m, k, v)

    got = {}
    for r in st_ws.get_all_values()[1:]:
        r = (list(r) + [""] * 6)[:6]
        if str(r[0]).strip():
            got[str(r[0]).strip()] = (str(r[4]).strip(), str(r[5]).strip())

    if got == expect_pairs:
        PASS.append(name)
    else:
        FAIL.append((name, expect_pairs, got))


K1, K2, K3 = "yab|Roth|MRNA", "yab|Roth|PNC", "yab|Robinhood|SILJ"
HDR = ["Key", "Alert_States", "Alert_LastState", "Updated_At", "Stop_Loss", "Target_Price"]


def state(rows):
    return [HDR] + rows


def pf(tickers):
    out = []
    for uid, acct, tk in tickers:
        out.append([uid, acct, tk, "100", "10", "2026-01-05"])
    return out


# ── A군: 정렬 불변식 ──────────────────────────────────────────────────────
# A-1 순서 불변 — 아무것도 안 바뀌어야 한다
run_case("A-1 순서 유지",
         pf([("yab", "Roth", "MRNA"), ("yab", "Roth", "PNC")]),
         state([[K1, "exit", "armed", "2026-08-21", "130", "200"],
                [K2, "exit", "armed", "2026-08-21", "240", "300"]]),
         {K1: ("130", "200"), K2: ("240", "300")})

# A-2 첫 종목 전량 매도로 행 삭제 → 남은 종목의 손절가가 어긋나면 안 된다 (핵심 결함)
run_case("A-2 앞 행 삭제",
         pf([("yab", "Roth", "PNC")]),
         state([[K1, "exit", "armed", "2026-08-21", "130", "200"],
                [K2, "exit", "armed", "2026-08-21", "240", "300"]]),
         {K2: ("240", "300")})

# A-3 순서 뒤집기
run_case("A-3 순서 역전",
         pf([("yab", "Roth", "PNC"), ("yab", "Roth", "MRNA")]),
         state([[K1, "exit", "armed", "2026-08-21", "130", "200"],
                [K2, "exit", "armed", "2026-08-21", "240", "300"]]),
         {K1: ("130", "200"), K2: ("240", "300")})

# A-4 중간 삽입 (신규 종목은 빈 E/F)
run_case("A-4 중간 삽입",
         pf([("yab", "Roth", "MRNA"), ("yab", "Robinhood", "SILJ"), ("yab", "Roth", "PNC")]),
         state([[K1, "exit", "armed", "2026-08-21", "130", "200"],
                [K2, "exit", "armed", "2026-08-21", "240", "300"]]),
         {K1: ("130", "200"), K2: ("240", "300"), K3: ("", "")})

# A-5 nodata 경로도 E/F 를 유지해야 한다
run_case("A-5 nodata 경로",
         pf([("yab", "Roth", "PNC"), ("yab", "Roth", "MRNA")]),
         state([[K1, "exit", "armed", "2026-08-21", "130", "200"],
                [K2, "exit", "armed", "2026-08-21", "240", "300"]]),
         {K1: ("130", "200"), K2: ("240", "300")},
         nodata_tickers={"MRNA"})

# A-6 E/F 미설정 종목이 값 있는 종목을 오염시키지 않음
run_case("A-6 부분 미설정",
         pf([("yab", "Roth", "PNC")]),
         state([[K1, "exit", "armed", "2026-08-21", "", ""],
                [K2, "exit", "armed", "2026-08-21", "240", "300"]]),
         {K2: ("240", "300")})


# ── B군: 양성 대조 — 하네스가 진짜 결함을 잡는지 ─────────────────────────
def positive_control():
    """일부러 위치 기반으로 되쓰는 가짜 구현 → A-2 가 반드시 실패해야 한다."""
    pf_ws = FakeWS(m._PF_WORKSHEET,
                   [["ID", "Account", "Ticker", "AvgPrice", "Quantity", "Date_Added"]]
                   + pf([("yab", "Roth", "PNC")]))
    st_ws = FakeWS(m._PFSTATE_WORKSHEET,
                   state([[K1, "exit", "armed", "2026-08-21", "130", "200"],
                          [K2, "exit", "armed", "2026-08-21", "240", "300"]]))
    # 낡은 동작 재현: A:D 만 덮어쓰기
    st_ws.update([HDR[:4], [K2, "exit", "armed", TODAY]], range_name="A1:D2")
    r2 = st_ws.get_all_values()[1]
    got = (str(r2[0]).strip(), str(r2[4]).strip())
    if got == (K2, "130"):
        PASS.append("B-1 양성대조(낡은 동작에서 오염 재현)")
    else:
        FAIL.append(("B-1 양성대조", (K2, "130"), got))


positive_control()

print("=" * 70)
print(f"통과 {len(PASS)} / 실패 {len(FAIL)}  (총 {len(PASS) + len(FAIL)})")
print("=" * 70)
for n in PASS:
    print(f"  ✅ {n}")
for n, exp, got in FAIL:
    print(f"  ❌ {n}\n       기대: {exp}\n       실제: {got}")
sys.exit(1 if FAIL else 0)
