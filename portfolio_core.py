# -*- coding: utf-8 -*-
"""portfolio_core.py — Portfolios 시트 SSOT (app.py + automation 공용).

`Portfolios` 시트는 **두 스키마가 공존**한다:
  new    (6열): ID, Account, Ticker, AvgPrice, Quantity, Date_Added
  legacy (5열): ID, Ticker, AvgPrice, Quantity, Date_Added        ← Account 없음

헤더가 new 인데 데이터 행이 5칸인 과도기 행도 존재하므로, 행 단위로 재판별해야 한다.
이 정규화 로직이 app.py 와 자동화에 각각 복사되면 "앱에서는 보유 종목인데 자동화는
아니라고 판단"하는 드리프트가 생긴다 — 그래서 여기 하나로 둔다.

설계 원칙:
- gspread Worksheet 를 인자로 받는다. 클라이언트 생성은 호출측 책임.
- streamlit / gspread 를 import 하지 않는다 (양쪽 환경 공용).

⚠️ lockstep: 변경 시 app.py + run_scanner_scan.py 를 함께 배포하고 Streamlit 리부트.
"""

import re
from datetime import datetime

import pytz

SSOT_VERSION = "2026-08-12a"

PORTFOLIOS_SHEET_COLS = ["ID", "Account", "Ticker", "AvgPrice", "Quantity", "Date_Added"]
PORTFOLIOS_LEGACY_HEADER = ["ID", "Ticker", "AvgPrice", "Quantity", "Date_Added"]
NCOL = len(PORTFOLIOS_SHEET_COLS)

_ET = pytz.timezone("America/New_York")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_TICKER_BLACKLIST = frozenset({"ID", "TICKER", "ACCOUNT", "DEFAULT", "TOTAL", "SUM"})


def _now_et_string() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d")


def _looks_like_ticker(v) -> bool:
    t = str(v or "").strip().upper()
    return bool(t) and t not in _TICKER_BLACKLIST and bool(_TICKER_RE.match(t))


def header_kind(header_row) -> str:
    """'new' | 'legacy' | 'unknown'."""
    h6 = [str(x).strip() for x in (header_row or [])[:6]]
    if len(h6) >= 6 and h6 == PORTFOLIOS_SHEET_COLS:
        return "new"
    h5 = [str(x).strip() for x in (header_row or [])[:5]]
    if h5 == PORTFOLIOS_LEGACY_HEADER:
        return "legacy"
    return "unknown"


def row_to_six(kind: str, row) -> list | None:
    """데이터 행을 항상 6칸 [ID, Account, Ticker, AvgPrice, Quantity, Date_Added] 으로.

    헤더가 new 여도 행이 5칸이고 2번째가 티커처럼 생겼으면 legacy 행으로 재판별한다
    (마이그레이션 도중 섞인 행 대응). app.py `_portfolio_row_to_new_six_cells` 와 동일 규칙.
    """
    row = list(row or [])
    effective = kind
    if kind == "new" and len(row) <= 5:
        cand = str(row[1]).strip().upper() if len(row) > 1 else ""
        if len(row) >= 5 and _looks_like_ticker(cand):
            effective = "legacy"

    if effective == "legacy":
        r = row + [""] * NCOL
        rid, tk = str(r[0]).strip(), str(r[1]).strip().upper()
        if not rid or not tk:
            return None
        return [rid, "Default", tk, r[2], r[3],
                str(r[4]).strip() if str(r[4]).strip() else _now_et_string()]

    r = row + [""] * NCOL
    rid, acct, tk = str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip().upper()
    if not rid or not acct or not tk:
        return None
    return [rid, acct, tk, r[3], r[4],
            str(r[5]).strip() if str(r[5]).strip() else _now_et_string()]


def load_rows(ws) -> list:
    """전체 보유 행을 6칸 정규화 리스트로 반환. 실패 시 빈 리스트."""
    try:
        vals = ws.get_all_values() or []
    except Exception:
        return []
    if len(vals) < 2:
        return []
    kind = header_kind(vals[0])
    if kind == "unknown":
        # 헤더가 깨진 경우에도 최대한 살린다 — new 로 가정하고 행 단위 재판별에 맡김
        kind = "new"
    out = []
    for r in vals[1:]:
        six = row_to_six(kind, r)
        if six:
            out.append(six)
    return out


def _to_float(v):
    try:
        s = str(v or "").replace(",", "").replace("$", "").strip()
        return float(s) if s else 0.0
    except Exception:
        return 0.0


def holdings_by_user(ws) -> dict:
    """{USER_ID(대문자): {TICKER: {"shares", "accounts"}}}.

    수량이 0 이하인 행은 보유로 보지 않는다(청산 잔재 행 방어).
    """
    out = {}
    for rid, acct, tk, _avg, qty, _dt in load_rows(ws):
        q = _to_float(qty)
        if q <= 0:
            continue
        u = out.setdefault(str(rid).strip().upper(), {})
        slot = u.setdefault(str(tk).strip().upper(), {"shares": 0.0, "accounts": []})
        slot["shares"] += q
        a = str(acct).strip()
        if a and a not in slot["accounts"]:
            slot["accounts"].append(a)
    return out


def held_tickers(ws, user_id: str) -> set:
    """해당 사용자가 실제 보유 중인 티커 집합 (수량 > 0)."""
    return set(holdings_by_user(ws).get(str(user_id or "").strip().upper(), {}).keys())
