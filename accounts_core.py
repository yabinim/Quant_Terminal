"""accounts_core.py — 계좌 프로필 순수 로직 SSOT.

app.py 와 automation(run_watchlist_alerts 등)이 동일한 규칙으로 계좌 프로필을
해석하도록 하는 공유 모듈. IO(gspread 클라이언트)는 각 소비처가 담당하고,
이 모듈은 **시트 값 → 프로필 dict / 자본금 계산**의 순수 변환만 제공한다.

방향: app.py → automation (app 이 SSOT). 이 모듈은 양쪽이 import 한다.

lockstep 대상:
  - Account_Profile 스키마 변경 시: 이 파일 + app.py(헬퍼) + 소비 automation 동시 배포.
  - Sizing_Mode 코드계는 regime_core.SIZING_MODES 와 일치해야 한다.
"""

import numpy as np
import pandas as pd

import regime_core as rc

# ── 스키마 (Google Sheets: Account_Profile 탭) ──────────────────────────
WORKSHEET_TITLE = "Account_Profile"
COLS = [
    "ID", "Account", "Cash", "Sizing_Mode", "Risk_Pct", "Max_Position_Pct",
    "Max_Positions", "Cash_Reserve_Pct", "Min_Trade_Dollars", "Updated_At",
]
NCOL = len(COLS)

# 행이 없는 계좌 = 아래 기본값 = 사이징 기능 도입 전과 동일 동작.
DEFAULTS = {
    "Cash": 0.0,
    "Sizing_Mode": rc.SIZING_MODE_DEFAULT,
    "Risk_Pct": rc.DEFAULT_RISK_PCT,
    "Max_Position_Pct": rc.DEFAULT_MAX_POSITION_PCT,
    "Max_Positions": rc.DEFAULT_MAX_POSITIONS,
    "Cash_Reserve_Pct": rc.DEFAULT_RESERVE_PCT,
    "Min_Trade_Dollars": rc.DEFAULT_MIN_TRADE_DOLLARS,
}

SIZING_MODE_LABELS = {
    "risk_based": "리스크 기반 (신호별 진입)",
    "equal_weight": "균등 배분 (기계적 회전)",
    "off": "사용 안 함",
}


def default_profile(account: str = "") -> dict:
    """저장된 행이 없을 때의 기본 프로필. _exists=False."""
    prof = dict(DEFAULTS)
    prof["Account"] = str(account or "").strip()
    prof["Updated_At"] = ""
    prof["_exists"] = False
    return prof


def _coerce_row(row: list) -> dict:
    """시트 한 행(list) → 타입 정규화된 프로필 dict. 유효하지 않은 값은 기본값으로."""
    r = (list(row) + [""] * NCOL)[:NCOL]
    prof = default_profile(str(r[1]).strip())
    prof["_exists"] = True
    prof["Updated_At"] = str(r[9]).strip()

    idx = {c: i for i, c in enumerate(COLS)}
    for key in ("Cash", "Risk_Pct", "Max_Position_Pct", "Cash_Reserve_Pct", "Min_Trade_Dollars"):
        v = pd.to_numeric(r[idx[key]], errors="coerce")
        if pd.notna(v) and float(v) >= 0:
            prof[key] = float(v)
    v = pd.to_numeric(r[idx["Max_Positions"]], errors="coerce")
    if pd.notna(v) and int(v) > 0:
        prof["Max_Positions"] = int(v)
    m = str(r[idx["Sizing_Mode"]]).strip()
    if m in rc.SIZING_MODES:
        prof["Sizing_Mode"] = m
    return prof


def parse_profiles(values: list, user_id: str) -> dict:
    """Account_Profile 전체 시트값(get_all_values) → {account_lower: profile} (해당 user만).

    values[0] 은 헤더로 간주하고 건너뛴다.
    """
    out = {}
    uid = str(user_id or "").strip().upper()
    if not uid or not values or len(values) < 2:
        return out
    for r in values[1:]:
        r = (list(r) + [""] * NCOL)[:NCOL]
        if str(r[0]).strip().upper() != uid:
            continue
        acct = str(r[1]).strip()
        if not acct:
            continue
        out[acct.lower()] = _coerce_row(r)
    return out


def get_profile(profiles: dict, account: str) -> dict:
    """parse_profiles 결과에서 계좌 조회. 없으면 기본값."""
    acct = str(account or "").strip()
    if not acct:
        return default_profile(acct)
    hit = (profiles or {}).get(acct.lower())
    return hit if hit else default_profile(acct)


def to_row(user_id: str, account: str, prof: dict, now_et: str) -> list:
    """프로필 dict → 시트 행(list). app.py 저장 경로에서 사용."""
    return [
        str(user_id or "").strip(),
        str(account or "").strip(),
        float(prof.get("Cash", 0.0) or 0.0),
        str(prof.get("Sizing_Mode", rc.SIZING_MODE_DEFAULT)),
        float(prof.get("Risk_Pct", rc.DEFAULT_RISK_PCT)),
        float(prof.get("Max_Position_Pct", rc.DEFAULT_MAX_POSITION_PCT)),
        int(prof.get("Max_Positions", rc.DEFAULT_MAX_POSITIONS)),
        float(prof.get("Cash_Reserve_Pct", 0.0) or 0.0),
        float(prof.get("Min_Trade_Dollars", 0.0) or 0.0),
        str(now_et or ""),
    ]


def compute_equity(holdings: list, price_map: dict, cash: float) -> dict:
    """계좌 자본금 산출 = cash + Σ(수량 × 현재가).

    holdings: [(ticker, quantity, fallback_price), ...] — 한 계좌의 보유 목록.
      fallback_price 는 시세 미수신 시 대체값(예: 매수 평단). 없으면 None/nan.
    price_map: {TICKER_UPPER: 현재가}
    반환: {equity, invested_value, slots_used, priced_ok, missing[]}
    """
    try:
        cash_v = float(cash)
    except (TypeError, ValueError):
        cash_v = 0.0
    if not (np.isfinite(cash_v) and cash_v >= 0):
        cash_v = 0.0

    total = 0.0
    tickers = set()
    priced_ok = True
    missing = []
    for h in (holdings or []):
        try:
            tk, qty, fb = h[0], h[1], (h[2] if len(h) > 2 else None)
        except (TypeError, IndexError):
            continue
        tk = str(tk or "").strip().upper()
        if not tk:
            continue
        tickers.add(tk)
        q = pd.to_numeric(qty, errors="coerce")
        if pd.isna(q) or float(q) <= 0:
            continue
        px = pd.to_numeric((price_map or {}).get(tk), errors="coerce")
        if pd.isna(px) or float(px) <= 0:
            px = pd.to_numeric(fb, errors="coerce")
            priced_ok = False
            missing.append(tk)
        if pd.isna(px) or float(px) <= 0:
            continue
        total += float(px) * float(q)

    return {
        "equity": round(cash_v + total, 2),
        "invested_value": round(total, 2),
        "slots_used": len(tickers),
        "cash": round(cash_v, 2),
        "priced_ok": priced_ok,
        "missing": missing,
    }
