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
    # ── 실적 레이더 (맨 뒤 append — Updated_At 인덱스 9 하드코딩을 깨지 않는다) ──
    "Earn_Preset", "Earn_Trim_Cap_Pct",
    # ── 트랜치 매도 사이징 (동일하게 맨 뒤 append) ──
    "Swing_Weight_Pct", "Trim_Ratio_Pct",
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
    "Earn_Preset": "",          # 미설정 — 아래 EARN_PRESET_FALLBACK 이 임시 적용된다
    "Earn_Trim_Cap_Pct": 0.0,   # 0 = 미저장 → 프리셋 기본값을 계산해서 쓴다
    # ⚠️ None 과 0 은 다르다. None = 트랜치 사이징 미사용(수량 권고 표시 안 함),
    #    0 = '스윙 0% / 포지션 100%' 라는 명시적 선택. 0 을 기본값으로 두면
    #    설정한 적 없는 계좌가 포지션 전용으로 오해된다.
    "Swing_Weight_Pct": None,
    "Trim_Ratio_Pct": rc.TRIM_RATIO_DEFAULT_PCT,
}

SIZING_MODE_LABELS = {
    "risk_based": "리스크 기반 (신호별 진입)",
    "equal_weight": "균등 배분 (기계적 회전)",
    "off": "사용 안 함",
}

# ── 실적 축소 임계 프리셋 ────────────────────────────────────────────────
# 축 = **매도 시 세금 발생 여부**. 이게 축소의 실제 비용을 좌우한다.
#   비과세(IRA/Roth/HSA/401k) : 매도가 공짜 → 리스크 예산에 가깝게 붙인다.
#   과세(일반 위탁계좌)        : 오른 종목을 실적 회피로 팔면 단기 양도세가 확정된다.
#                               회피하려는 손실보다 확정 비용이 큰 경우가 실제로 생기므로
#                               '정말 위험할 때만' 발동하도록 크게 벌린다.
#   장기적립 전용              : 계좌 전체가 인덱스 DCA → 축소 제안 자체를 끈다
#                               (캘린더·진입 차단 게이트는 그대로 유지된다).
# 값은 Risk_Pct 배수. 아무것도 안 건드리면 Risk_Pct 를 따라가므로 자동 스케일되고,
# 명시 저장(custom)하면 Risk_Pct 와 분리된다.
EARN_PRESET_MULT = {
    "tax_free": 1.5,
    "taxable":  3.0,
    "dca_only": None,     # None = 축소 판정 미사용
    "custom":   None,     # 저장된 Earn_Trim_Cap_Pct 값을 그대로 사용
}
EARN_PRESETS = tuple(EARN_PRESET_MULT.keys())

EARN_PRESET_LABELS = {
    "tax_free": "🟦 비과세 계좌 (IRA·Roth·HSA·401k)",
    "taxable":  "🟨 과세 계좌 (일반 위탁)",
    "dca_only": "🟩 장기적립 전용 (축소 제안 없음)",
    "custom":   "⚙️ 직접 입력",
}
EARN_PRESET_HELP = {
    "tax_free": "매도해도 세금이 없어 축소가 자유롭습니다. 한도 = 거래당 리스크 × 1.5",
    "taxable":  "매도 시 양도세가 확정되므로 정말 위험할 때만 발동합니다. 한도 = 거래당 리스크 × 3.0",
    "dca_only": "실적 캘린더와 진입 차단은 유지하되, 보유 축소 제안만 끕니다.",
    "custom":   "Risk_Pct 와 분리된 절대값을 직접 지정합니다.",
}

# 미설정 계좌에 임시 적용할 프리셋 — 엄격한 쪽(D1 원칙).
# 앱은 주문을 내지 않고 '제안'만 하므로, 경고가 뜨면 사용자가 세금을 보고 거를 수 있다.
# 반대로 경고가 아예 안 뜨면 거를 기회조차 없다.
EARN_PRESET_FALLBACK = "tax_free"


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
    for key in ("Cash", "Risk_Pct", "Max_Position_Pct", "Cash_Reserve_Pct",
                "Min_Trade_Dollars", "Earn_Trim_Cap_Pct"):
        v = pd.to_numeric(r[idx[key]], errors="coerce")
        if pd.notna(v) and float(v) >= 0:
            prof[key] = float(v)
    v = pd.to_numeric(r[idx["Max_Positions"]], errors="coerce")
    if pd.notna(v) and int(v) > 0:
        prof["Max_Positions"] = int(v)
    m = str(r[idx["Sizing_Mode"]]).strip()
    if m in rc.SIZING_MODES:
        prof["Sizing_Mode"] = m
    p = str(r[idx["Earn_Preset"]]).strip().lower()
    if p in EARN_PRESETS:
        prof["Earn_Preset"] = p

    # 트랜치 비율 — 빈 칸이면 NaN 이 되어 건너뛰고 기본값 None 이 유지된다.
    # 0 은 유효한 명시값이므로 >= 0 조건으로 통과시킨다.
    v = pd.to_numeric(r[idx["Swing_Weight_Pct"]], errors="coerce")
    if pd.notna(v) and 0 <= float(v) <= 100:
        prof["Swing_Weight_Pct"] = float(v)
    v = pd.to_numeric(r[idx["Trim_Ratio_Pct"]], errors="coerce")
    if pd.notna(v) and float(v) > 0:
        prof["Trim_Ratio_Pct"] = float(
            max(rc.TRIM_RATIO_MIN_PCT, min(rc.TRIM_RATIO_MAX_PCT, float(v))))
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
        str(prof.get("Earn_Preset", "") or ""),
        float(prof.get("Earn_Trim_Cap_Pct", 0.0) or 0.0),
        # 미설정은 빈 문자열로 저장한다. 0.0 으로 쓰면 '포지션 100%' 라는
        # 명시적 선택과 구분되지 않는다.
        ("" if prof.get("Swing_Weight_Pct") is None
         else float(prof.get("Swing_Weight_Pct"))),
        float(prof.get("Trim_Ratio_Pct", rc.TRIM_RATIO_DEFAULT_PCT)
              or rc.TRIM_RATIO_DEFAULT_PCT),
    ]


# ── 실적 축소 한도 해석 (SSOT) ──────────────────────────────────────────
# app.py 와 automation 이 각자 기본값을 두면 즉시 드리프트한다. 반드시 여기만 쓴다.
# earnings_core 는 해석이 끝난 숫자만 받는다(계좌 프로필을 침범하지 않는다).

def preset_default_cap(preset: str, risk_pct) -> float | None:
    """프리셋 코드 + Risk_Pct → 기본 한도(%). dca_only/custom 은 None."""
    mult = EARN_PRESET_MULT.get(str(preset or "").strip().lower())
    if mult is None:
        return None
    try:
        rp = float(risk_pct)
    except (TypeError, ValueError):
        rp = float(rc.DEFAULT_RISK_PCT)
    if not (np.isfinite(rp) and rp > 0):
        rp = float(rc.DEFAULT_RISK_PCT)
    return round(rp * float(mult), 4)


def resolve_earn_trim_cap(prof: dict) -> dict:
    """프로필 → 실적 축소 한도 해석 결과.

    저장 방식(C1): 프리셋 '코드'와 '확정값'을 둘 다 저장하고 **계산은 값만** 쓴다.
      → 나중에 프리셋 정의(배수)를 바꿔도 기존 사용자의 알림 동작이 조용히 바뀌지 않는다.
      → 값이 비어 있을 때만 프리셋 배수로 계산한다(신규 계좌·미설정 계좌).

    반환: {cap_pct, preset, preset_label, is_set, is_default, default_cap, disabled}
      cap_pct : evaluate_trim 에 넘길 한도(%). None 이면 축소 판정 미사용.
      is_set  : 사용자가 프리셋을 명시했는가 (False 면 UI 가 미설정 배너를 띄운다)
    """
    p = dict(prof or {})
    raw_preset = str(p.get("Earn_Preset", "") or "").strip().lower()
    is_set = raw_preset in EARN_PRESETS
    preset = raw_preset if is_set else EARN_PRESET_FALLBACK
    risk = p.get("Risk_Pct", rc.DEFAULT_RISK_PCT)

    stored = pd.to_numeric(p.get("Earn_Trim_Cap_Pct"), errors="coerce")
    stored = float(stored) if (pd.notna(stored) and float(stored) > 0) else None
    default_cap = preset_default_cap(preset, risk)

    if preset == "dca_only":
        cap = None
    elif preset == "custom":
        cap = stored          # 직접 입력인데 값이 없으면 판정 불가 → None
    else:
        cap = stored if stored is not None else default_cap

    return {
        "cap_pct": cap,
        "preset": preset,
        "preset_label": EARN_PRESET_LABELS.get(preset, preset),
        "is_set": is_set,
        "is_default": (stored is None),
        "default_cap": default_cap,
        "disabled": (preset == "dca_only"),
    }


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
