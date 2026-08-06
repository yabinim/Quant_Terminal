"""earnings_core.py — 실적 이벤트 리스크 SSOT.

설계 철학 (프로젝트 원칙 #4 — 손실 방지 우선):
  이 모듈은 **새로운 매수/매도 신호를 만들지 않는다.** 하는 일은 셋뿐:
    1) 차단 — 실적 직전 신규 진입 게이트를 닫는다      (evaluate_entry_gate)
    2) 축소 — 갭 리스크가 한도를 넘는 보유를 줄인다     (evaluate_trim)
    3) 관찰 — 발표 후 반응을 측정·기록한다             (measure_reaction / evaluate_pead)
  매수는 여전히 regime_core 의 레짐·타이밍·R:R 게이트가, 매도는 스윙/포지션
  이중 신호가 결정한다. 여기서 나온 판정은 그 위에 얹히는 '제약'이지 신호가 아니다.

옵션 데이터 미사용 (의도적):
  FMP 는 옵션 체인을 제공하지 않고, 내재 변동폭은 정의상 대칭이라 방향 정보가 없다.
  대신 **과거 8분기 실적 반응일의 close-to-close 변동률**을 쓴다. 실현 움직임을
  직접 재는 값이라 사이징·손절 유효성 판단에는 오히려 더 잘 맞고, 소급 계산이
  가능해 배포 첫날부터 완전 동작한다(내재 변동폭은 이력 축적이 불가능).

IO 경계:
  FMP 조회는 이 모듈이 담당(narrative_core 와 동일 패턴). Sheets IO 는 소비처가 담당.
  streamlit 을 import 하지 않는다 → app.py 와 automation 이 동일 코드를 공유한다.
  app.py 는 @st.cache_data 래퍼만 씌운다.

lockstep 대상:
  - 이 파일 변경 시: app.py + automation/run_earnings_watch.py 동시 배포.
  - 계좌 프리셋 해석은 accounts_core 가 SSOT. 이 모듈은 해석 끝난 trim_cap_pct 만 받는다.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 7


# ──────────────────────────────────────────────────────────────────────────
# 튜닝 상수 (한 곳에서 관리)
# ──────────────────────────────────────────────────────────────────────────

ENTRY_BLOCK_DAYS = 3          # D-N 이내 신규 진입 차단
SNAPSHOT_DAYS = 3             # 사전 스냅샷/이메일 발송 시점 (D-N)
SCAN_HORIZON_DAYS = 10        # 상세 조회 대상 (API 호출 절감)
CALENDAR_HORIZON_DAYS = 90    # 캘린더 조회 창

GAP_QUARTERS = 8              # 예상 변동폭 표본 분기 수
MIN_SAMPLE = 4                # 이 미만이면 산출 거부 (추정치로 지시 금지)

TAIL_ALERT_PCT = 5.0          # 최악 시 계좌 타격 임계 (%) — 계좌 무관 고정
TRIM_HARD_RATIO = 2.0         # 목표 대비 이 배수 초과 → 대폭 축소

PEAD_VOLUME_MIN = 2.0         # PEAD 인정 최소 거래량 배수
PEAD_SURPRISE_RATIO = 0.8     # 실제 갭 / 예상 중앙값 — 이 이상이면 '유의미한 반응'
VOLUME_BASELINE_BARS = 20     # 거래량 배수 기준 구간

MOVE_CONF_LOW_IQR = 1.0       # IQR/중앙값 이 값 초과 → 신뢰 낮음
MOVE_CONF_MID_IQR = 0.5


# ──────────────────────────────────────────────────────────────────────────
# 표시 라벨 (앱 탭 · 이메일이 공유하는 SSOT)
# ──────────────────────────────────────────────────────────────────────────

TRIM_LABELS = {
    "hold":      "🟢 그대로 두세요",
    "trim":      "🟠 줄이세요",
    "trim_hard": "🔴 절반 이하로 줄이세요",
    "core":      "🔵 코어 — 대상 아님",
    "disabled":  "⚪ 축소 판정 미사용 계좌",
    "na":        "⚪ 표본 부족 — 수동 판단",
}

GATE_LABELS = {
    "open":    "✅ 진입 가능",
    "blocked": "⛔ 매수 보류",
    "na":      "⚪ 판정 보류",
}

PEAD_LABELS = {
    "up_continue":   "🟢 상승 지속 후보",
    "up_faded":      "🟡 상승 갭 되돌림 — 관망",
    "down_break":    "🔴 하락 이탈 — 근거 훼손",
    "down_recovered": "🟡 하락 갭 회복 — 관망",
    "muted":         "⚪ 반응 미미",
    "na":            "⚪ 측정 불가",
}

TIMING_LABELS = {
    "bmo": "장 시작 전",
    "amc": "장 마감 후",
    "":    "시각 미상",
}

DATE_SOURCE_LABELS = {
    "confirmed": "확정",
    "estimated": "추정",
    "":          "미상",
}

MOVE_CONF_LABELS = {
    "high":         "신뢰 높음",
    "medium":       "신뢰 보통",
    "low":          "신뢰 낮음",
    "insufficient": "표본 부족",
}


# ──────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────

def _fmp_key() -> str:
    return str(os.environ.get("FMP_API_KEY", "") or "").strip()


def _num(x):
    """안전한 float 변환. 실패/비유한 → None."""
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _get(path: str, key: str = "") -> list | dict | None:
    """FMP stable GET. 실패 시 None (예외 전파 금지)."""
    k = key or _fmp_key()
    if not k:
        return None
    sep = "&" if "?" in path else "?"
    try:
        r = requests.get(f"{_FMP_BASE}/{path}{sep}apikey={k}", timeout=_FMP_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _d(s) -> pd.Timestamp | None:
    """'YYYY-MM-DD...' → Timestamp(날짜만). 실패 → None."""
    ds = str(s or "")[:10]
    if len(ds) != 10:
        return None
    try:
        return pd.Timestamp(ds)
    except Exception:
        return None


def _timing_of(item: dict) -> str:
    """FMP 실적 항목에서 BMO/AMC 추출. 필드명이 판올림마다 달라 다중 키 탐색."""
    for key in ("time", "when", "timing", "hour", "announcementTime"):
        v = str(item.get(key) or "").strip().lower()
        if not v:
            continue
        if "bmo" in v or "before" in v or "pre" in v:
            return "bmo"
        if "amc" in v or "after" in v or "post" in v:
            return "amc"
        # "08:30" 형태 — 09:30 ET 개장 기준
        if ":" in v:
            try:
                hh = int(v.split(":")[0])
                return "bmo" if hh < 12 else "amc"
            except (TypeError, ValueError):
                continue
    return ""


# ──────────────────────────────────────────────────────────────────────────
# 1) 실적 캘린더 — 확정/추정 구분 필수
#    FMP 실적일 상당수가 '추정일'(회사 공식 공지 전 전년 동기 기준 추정).
#    D-3 알림을 보냈는데 실제론 D-9 이면 신뢰가 무너지므로 2개 소스를 교차
#    확인하고, 불일치하면 **더 이른 날짜를 채택**한다(보수적).
# ──────────────────────────────────────────────────────────────────────────

def fetch_next_earnings(ticker: str, today=None, horizon_days: int = CALENDAR_HORIZON_DAYS,
                        key: str = "") -> dict | None:
    """다음 실적 발표 예정 1건. 없으면 None.

    반환: {ticker, earnings_date, days_until, timing, date_source, eps_estimate,
           sources:[...], conflict:bool}
      date_source : 'confirmed' (2개 소스 일치) / 'estimated' (단일 소스 or 불일치)
    """
    tk = str(ticker or "").strip().upper()
    if not tk:
        return None
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    hi = t0 + pd.Timedelta(days=int(horizon_days))
    k = key or _fmp_key()

    cands = []   # (date, timing, eps_est, source)

    def _scan(items, source):
        for it in (items if isinstance(items, list) else []):
            if not isinstance(it, dict):
                continue
            d = _d(it.get("date") or it.get("fiscalDateEnding"))
            if d is None or d < t0 or d > hi:
                continue
            eps = _num(it.get("epsEstimated") or it.get("estimatedEPS")
                       or it.get("epsEstimate") or it.get("estimatedEarning"))
            cands.append((d, _timing_of(it), eps, source))

    frm, to = t0.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")
    _scan(_get(f"earnings-calendar?symbol={tk}&from={frm}&to={to}", k), "calendar")
    _scan(_get(f"earnings?symbol={tk}", k), "earnings")

    # quote.earningsAnnouncement — 단일 날짜 폴백
    q = _get(f"quote?symbol={tk}", k)
    qi = q[0] if isinstance(q, list) and q else (q if isinstance(q, dict) else {})
    if isinstance(qi, dict):
        d = _d(qi.get("earningsAnnouncement"))
        if d is not None and t0 <= d <= hi:
            cands.append((d, _timing_of({"time": str(qi.get("earningsAnnouncement") or "")[11:16]}),
                          None, "quote"))

    if not cands:
        return None

    cands.sort(key=lambda x: x[0])
    best_d = cands[0][0]
    srcs = sorted({c[3] for c in cands})
    dates = {c[0] for c in cands}
    conflict = len(dates) > 1
    timing = next((c[1] for c in cands if c[0] == best_d and c[1]), "")
    eps = next((c[2] for c in cands if c[0] == best_d and c[2] is not None), None)

    # 2개 이상 소스가 같은 날짜를 가리키면 '확정'으로 본다.
    agree = sum(1 for c in cands if c[0] == best_d and c[3] in ("calendar", "earnings"))
    return {
        "ticker": tk,
        "earnings_date": best_d.strftime("%Y-%m-%d"),
        "days_until": int((best_d - t0).days),
        "timing": timing,
        "date_source": "confirmed" if (agree >= 2 and not conflict) else "estimated",
        "eps_estimate": eps,
        "sources": srcs,
        "conflict": conflict,
    }


def fetch_upcoming(tickers, today=None, horizon_days: int = SCAN_HORIZON_DAYS,
                   key: str = "") -> list[dict]:
    """여러 티커의 다가오는 실적 — horizon_days 이내만. 날짜순 정렬."""
    out = []
    seen = set()
    for tk in (tickers or []):
        t = str(tk or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        ev = fetch_next_earnings(t, today=today, key=key)
        if ev and 0 <= ev["days_until"] <= int(horizon_days):
            out.append(ev)
    out.sort(key=lambda x: (x["days_until"], x["ticker"]))
    return out


def past_earnings_dates(ticker: str, today=None, limit: int = GAP_QUARTERS + 4,
                        key: str = "") -> list[dict]:
    """과거 실적 발표일 목록 (최신순). [{date, timing, surprise_pct}]"""
    tk = str(ticker or "").strip().upper()
    if not tk:
        return []
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    rows, seen = [], set()
    for path in (f"earnings?symbol={tk}", f"earnings-surprises?symbol={tk}"):
        for it in (_get(path, key) or []):
            if not isinstance(it, dict):
                continue
            d = _d(it.get("date") or it.get("fiscalDateEnding"))
            if d is None or d >= t0:
                continue
            ds = d.strftime("%Y-%m-%d")
            if ds in seen:
                continue
            seen.add(ds)
            act = _num(it.get("epsActual") or it.get("actualEarningResult") or it.get("eps"))
            est = _num(it.get("epsEstimated") or it.get("estimatedEarning") or it.get("epsEstimated"))
            sp = None
            if act is not None and est is not None and abs(est) > 1e-9:
                sp = (act - est) / abs(est) * 100.0
            rows.append({"date": ds, "timing": _timing_of(it), "surprise_pct": sp})
        if len(rows) >= limit:
            break
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows[:limit]


# ──────────────────────────────────────────────────────────────────────────
# 2) 반응 측정 — 갭은 '반응일 종가 vs 직전 종가'(close-to-close)
#    BMO 발표 → 당일이 반응일 / AMC 발표 → 다음 거래일이 반응일.
#    timing 미상이면 D·D+1 중 거래량이 큰 쪽을 반응일로 추정한다.
# ──────────────────────────────────────────────────────────────────────────

def resolve_reaction_index(hist: pd.DataFrame, event_date, timing: str = "") -> int | None:
    """hist(DatetimeIndex, 오름차순)에서 반응 세션의 위치 인덱스. 없으면 None."""
    if hist is None or hist.empty:
        return None
    d = _d(event_date)
    if d is None:
        return None
    idx = hist.index
    # 발표일 이후(포함) 첫 거래일
    pos = int(idx.searchsorted(d, side="left"))
    if pos >= len(idx):
        return None
    t = str(timing or "").strip().lower()
    if t == "bmo":
        cand = pos
    elif t == "amc":
        cand = pos + 1 if idx[pos] == d else pos
    else:
        # 미상 — D 와 D+1 중 거래량이 큰 세션
        a, b = pos, min(pos + 1, len(idx) - 1)
        if "Volume" in hist.columns and a != b:
            va = _num(hist["Volume"].iloc[a]) or 0.0
            vb = _num(hist["Volume"].iloc[b]) or 0.0
            cand = b if vb > va else a
        else:
            cand = pos
    if cand <= 0 or cand >= len(idx):
        return None
    return cand


def measure_reaction(hist: pd.DataFrame, event_date, timing: str = "") -> dict:
    """실적 반응 측정 (관찰만 — 예측 없음).

    반환: {ok, reaction_date, gap_pct, open_gap_pct, volume_ratio, gap_held, reason}
      gap_pct     : (반응일 종가 − 직전 종가) / 직전 종가 × 100
      gap_held    : 상승 갭이면 종가 ≥ 시가, 하락 갭이면 종가 ≤ 시가
    """
    out = {"ok": False, "reaction_date": "", "gap_pct": None, "open_gap_pct": None,
           "volume_ratio": None, "gap_held": None, "reason": ""}
    i = resolve_reaction_index(hist, event_date, timing)
    if i is None:
        out["reason"] = "반응일 확인 불가"
        return out
    try:
        pre_close = _num(hist["Close"].iloc[i - 1])
        rc = _num(hist["Close"].iloc[i])
        ro = _num(hist["Open"].iloc[i]) if "Open" in hist.columns else None
    except Exception:
        out["reason"] = "가격 데이터 부족"
        return out
    if pre_close is None or rc is None or pre_close <= 0:
        out["reason"] = "가격 데이터 부족"
        return out

    gap = (rc - pre_close) / pre_close * 100.0
    out["gap_pct"] = round(gap, 2)
    out["reaction_date"] = pd.Timestamp(hist.index[i]).strftime("%Y-%m-%d")
    if ro is not None and ro > 0:
        out["open_gap_pct"] = round((ro - pre_close) / pre_close * 100.0, 2)
        out["gap_held"] = bool(rc >= ro) if gap >= 0 else bool(rc <= ro)

    if "Volume" in hist.columns and i >= 2:
        base = pd.to_numeric(
            hist["Volume"].iloc[max(0, i - VOLUME_BASELINE_BARS):i], errors="coerce"
        ).dropna()
        v = _num(hist["Volume"].iloc[i])
        if v is not None and not base.empty:
            med = float(base.median())
            if med > 0:
                out["volume_ratio"] = round(v / med, 2)

    out["ok"] = True
    return out


def gap_history(hist: pd.DataFrame, events: list, quarters: int = GAP_QUARTERS) -> list[dict]:
    """과거 실적 반응 목록 (최신순). events: past_earnings_dates() 결과."""
    rows = []
    for ev in (events or []):
        m = measure_reaction(hist, ev.get("date"), ev.get("timing", ""))
        if m["ok"] and m["gap_pct"] is not None:
            rows.append({"date": ev.get("date"), "gap_pct": m["gap_pct"],
                         "volume_ratio": m.get("volume_ratio")})
        if len(rows) >= int(quarters):
            break
    return rows


# ──────────────────────────────────────────────────────────────────────────
# 3) 예상 변동폭 — 중앙값이 메인, 최악 하락폭은 테일 경보용
#    평균이 아니라 중앙값을 쓰는 이유: 실적 갭 분포는 꼬리가 두꺼워
#    한 번의 -22% 가 평균을 오염시킨다.
# ──────────────────────────────────────────────────────────────────────────

def expected_move(gaps: list, atr_pct=None, min_sample: int = MIN_SAMPLE) -> dict:
    """과거 갭 목록 → 예상 변동폭 통계.

    gaps: gap_history() 결과 또는 [숫자, ...]
    반환: {ok, median_pct, worst_down_pct, worst_abs_pct, p25, p75,
           atr_multiple, sample_n, confidence, confidence_label, note}
      worst_down_pct 는 **양수 크기**로 반환 (예: -23% → 23.0)
    """
    out = {"ok": False, "median_pct": None, "worst_down_pct": None, "worst_abs_pct": None,
           "p25": None, "p75": None, "atr_multiple": None, "sample_n": 0,
           "confidence": "insufficient", "confidence_label": MOVE_CONF_LABELS["insufficient"],
           "note": ""}

    vals = []
    for g in (gaps or []):
        v = _num(g.get("gap_pct")) if isinstance(g, dict) else _num(g)
        if v is not None:
            vals.append(float(v))
    out["sample_n"] = len(vals)
    if len(vals) < int(min_sample):
        out["note"] = f"표본 {len(vals)}분기 — 최소 {int(min_sample)}분기 필요"
        return out

    arr = np.array(vals, dtype=float)
    absa = np.abs(arr)
    med = float(np.median(absa))
    p25, p75 = float(np.percentile(absa, 25)), float(np.percentile(absa, 75))
    downs = arr[arr < 0]
    worst_down = float(abs(downs.min())) if downs.size else 0.0

    out.update({
        "ok": True,
        "median_pct": round(med, 2),
        "worst_down_pct": round(worst_down, 2),
        "worst_abs_pct": round(float(absa.max()), 2),
        "p25": round(p25, 2), "p75": round(p75, 2),
    })

    a = _num(atr_pct)
    if a is not None and a > 0:
        out["atr_multiple"] = round(med / a, 1)

    # 신뢰도 = 표본 수 + 분산(IQR/중앙값)
    iqr_ratio = ((p75 - p25) / med) if med > 0 else 99.0
    if len(vals) < GAP_QUARTERS - 2 or iqr_ratio > MOVE_CONF_LOW_IQR:
        conf = "low"
    elif iqr_ratio > MOVE_CONF_MID_IQR:
        conf = "medium"
    else:
        conf = "high"
    out["confidence"] = conf
    out["confidence_label"] = MOVE_CONF_LABELS[conf]
    if conf == "low":
        out["note"] = f"분기별 편차 큼 ({p25:.1f}~{p75:.1f}%) — 참고용"
    return out


def atr_pct_of(hist: pd.DataFrame, window: int = 22) -> float | None:
    """평소 일간 변동성(ATR%) — '평소의 몇 배' 환산용."""
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    try:
        c = pd.to_numeric(hist["Close"], errors="coerce")
        h = pd.to_numeric(hist["High"], errors="coerce") if "High" in hist.columns else c
        lo = pd.to_numeric(hist["Low"], errors="coerce") if "Low" in hist.columns else c
        pc = c.shift(1)
        tr = pd.concat([(h - lo).abs(), (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(int(window)).mean().iloc[-1])
        last = float(c.iloc[-1])
        if not (np.isfinite(atr) and np.isfinite(last) and last > 0):
            return None
        return round(atr / last * 100.0, 3)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────
# 4) 진입 게이트 — "손절이 작동하는가"만 본다. 비중과 무관.
# ──────────────────────────────────────────────────────────────────────────

def derived_stop_pct(hist, price=None, atr_mult=None) -> float | None:
    """수동 손절이 없을 때 ATR 기반 손절폭(%) 추정 — R:R 게이트와 동일 규약.

    워치리스트에 Stop_Loss 를 넣지 않는 종목이 많은데, 손절 미상이라고 무조건
    차단하면 '예상 갭 vs 손절폭' 비교라는 이 게이트의 핵심이 아예 작동하지 않는다
    (±3% 종목과 ±19% 종목의 판정이 같아진다). regime_core.build_watchlist_plan 이
    수동 손절 없을 때 쓰는 것과 같은 ATR 손절을 추정치로 쓴다.

    반환: 양수 % (예: 6.4). 산출 불가 시 None.
    """
    try:
        import regime_core as _rc
    except Exception:
        return None
    if hist is None or getattr(hist, "empty", True) or "Close" not in hist.columns:
        return None
    try:
        px = _num(price)
        if px is None:
            px = _num(pd.to_numeric(hist["Close"], errors="coerce").dropna().iloc[-1])
        if px is None or px <= 0:
            return None
        mult = _num(atr_mult)
        if mult is None:
            mult = float(_rc.regime_params({})["atr_mult"])
        atr = _num(_rc.compute_atr(hist, _rc.ATR_WINDOW))
        if atr is None or atr <= 0:
            return None
        return round(atr * mult / px * 100.0, 2)
    except Exception:
        return None


def evaluate_entry_gate(move: dict, planned_stop_pct=None, days_until=None,
                        block_days: int = ENTRY_BLOCK_DAYS,
                        earnings_date: str = "", stop_source: str = "") -> dict:
    """실적 직전 신규 진입 차단 판정.

    planned_stop_pct: 계획 손절폭 (양수 %, 예: -7% 손절 → 7.0). None 이면 보수적 차단.
    stop_source     : 'manual' | 'atr'(추정) — 사유 문구에만 반영.
    반환: {blocked, code, label, reason, unblock_after, stop_pct, stop_source}
    """
    out = {"blocked": False, "code": "open", "label": GATE_LABELS["open"],
           "reason": "", "unblock_after": "",
           "stop_pct": _num(planned_stop_pct), "stop_source": str(stop_source or "")}
    d = _num(days_until)
    if d is None or d < 0 or d > int(block_days):
        return out

    out["unblock_after"] = str(earnings_date or "")
    mv = (move or {}).get("median_pct") if isinstance(move, dict) else None
    ok = bool((move or {}).get("ok")) if isinstance(move, dict) else False
    sp = _num(planned_stop_pct)
    _sfx = " (추정)" if str(stop_source or "").lower() == "atr" else ""

    if not ok or mv is None:
        # 측정 불가 + 실적 임박 → 보수적 차단 (D1 원칙: 미상이면 엄격)
        out.update({"blocked": True, "code": "blocked", "label": GATE_LABELS["blocked"],
                    "reason": f"실적 D-{int(d)} · 예상 변동폭 산출 불가 — 발표 후 재평가"})
        return out

    if sp is None or sp <= 0:
        out.update({"blocked": True, "code": "blocked", "label": GATE_LABELS["blocked"],
                    "reason": f"실적 D-{int(d)} · 예상 갭 ±{mv:.1f}% · 손절 산출 불가 — 발표 후 재평가"})
        return out

    if mv > sp:
        out.update({"blocked": True, "code": "blocked", "label": GATE_LABELS["blocked"],
                    "reason": (f"실적 D-{int(d)} · 예상 갭 ±{mv:.1f}% > 손절 {sp:.1f}%{_sfx} "
                               f"— 손절이 작동하지 않는 구간")})
        return out

    out["reason"] = f"실적 D-{int(d)} · 예상 갭 ±{mv:.1f}% ≤ 손절 {sp:.1f}%{_sfx}"
    return out


# ──────────────────────────────────────────────────────────────────────────
# 5) 축소 판정 — 두 제약을 동시에 만족하는 최대 금액이 목표
#      cap  : 비중 × 중앙 갭 ≤ trim_cap_pct        (상시 판정)
#      tail : 비중 × 최악 하락폭 ≤ TAIL_ALERT_PCT   (테일 경보, 계좌 무관 고정)
#    목표 = min(두 제약). 현재 금액이 목표의 몇 배인지로 3단 판정.
# ──────────────────────────────────────────────────────────────────────────

def evaluate_trim(position_value, equity, move: dict, trim_cap_pct=None,
                  min_trade_dollars: float = 0.0, is_core: bool = False) -> dict:
    """보유 포지션의 실적 갭 노출 진단.

    trim_cap_pct: accounts_core 가 해석한 계좌별 한도(%). None → 축소 판정 미사용(dca_only).
    is_core     : 코어/정기적립 thesis → 축소 대상 제외.
    반환: {code, label, position_value, position_pct, target_value, target_pct,
           sell_dollars, impact_pct, impact_dollars, cap_multiple,
           tail_pct, tail_flag, binding, reason, below_min}
    """
    out = {"code": "na", "label": TRIM_LABELS["na"],
           "position_value": 0.0, "position_pct": None,
           "target_value": None, "target_pct": None, "sell_dollars": 0.0,
           "impact_pct": None, "impact_dollars": None, "cap_multiple": None,
           "tail_pct": None, "tail_flag": False, "binding": "",
           "reason": "", "below_min": False}

    pv, eq = _num(position_value), _num(equity)
    if pv is None or eq is None or pv <= 0 or eq <= 0:
        out["reason"] = "포지션/자본 데이터 없음"
        return out
    out["position_value"] = round(pv, 2)
    out["position_pct"] = round(pv / eq * 100.0, 2)

    if is_core:
        out.update({"code": "core", "label": TRIM_LABELS["core"],
                    "reason": "코어/정기적립 — 실적으로 흔들지 않음"})
        return out

    cap = _num(trim_cap_pct)
    if cap is None or cap <= 0:
        out.update({"code": "disabled", "label": TRIM_LABELS["disabled"],
                    "reason": "장기적립 전용 계좌 — 축소 제안 없음"})
        return out

    if not (isinstance(move, dict) and move.get("ok")):
        out["reason"] = (move or {}).get("note") or "예상 변동폭 산출 불가"
        return out

    med = _num(move.get("median_pct"))
    worst = _num(move.get("worst_down_pct")) or 0.0
    if med is None or med <= 0:
        out["reason"] = "예상 변동폭 산출 불가"
        return out

    # 현재 노출
    impact_d = pv * med / 100.0
    out["impact_dollars"] = round(impact_d, 2)
    out["impact_pct"] = round(impact_d / eq * 100.0, 2)
    out["cap_multiple"] = round(out["impact_pct"] / cap, 2) if cap > 0 else None
    if worst > 0:
        out["tail_pct"] = round(pv * worst / 100.0 / eq * 100.0, 2)
        out["tail_flag"] = bool(out["tail_pct"] > TAIL_ALERT_PCT)

    # 목표 금액 = 두 제약의 교집합
    cap_target = eq * cap / med
    tail_target = (eq * TAIL_ALERT_PCT / worst) if worst > 0 else float("inf")
    if tail_target < cap_target:
        target, binding = tail_target, "tail"
    else:
        target, binding = cap_target, "cap"
    out["binding"] = binding
    out["target_value"] = round(target, 2)
    out["target_pct"] = round(target / eq * 100.0, 2)

    ratio = pv / target if target > 0 else float("inf")
    if ratio <= 1.0:
        out.update({"code": "hold", "label": TRIM_LABELS["hold"],
                    "sell_dollars": 0.0,
                    "reason": (f"예상 타격 {out['impact_pct']:.1f}% — 한도 {cap:.2f}% 이내")})
        return out

    sell = max(pv - target, 0.0)
    _min = _num(min_trade_dollars) or 0.0
    if _min > 0 and sell < _min:
        out.update({"code": "hold", "label": TRIM_LABELS["hold"], "sell_dollars": 0.0,
                    "below_min": True,
                    "reason": f"축소 필요액 ${sell:,.0f} < 최소 거래금액 ${_min:,.0f} — 제안 생략"})
        return out

    code = "trim_hard" if ratio > TRIM_HARD_RATIO else "trim"
    if binding == "tail":
        reason = (f"최악 시 계좌 타격 {out['tail_pct']:.1f}% "
                  f"— 테일 경보 기준 {TAIL_ALERT_PCT:.0f}% 초과")
    else:
        reason = (f"예상 타격 {out['impact_pct']:.1f}% "
                  f"— 한도 {cap:.2f}% 의 {out['cap_multiple']:.1f}배")
    out.update({"code": code, "label": TRIM_LABELS[code],
                "sell_dollars": round(sell, 2), "reason": reason})
    return out


# ──────────────────────────────────────────────────────────────────────────
# 6) PEAD — 예측이 아니라 관찰. 갭 방향 · 거래량 · 갭 유지 3요소만.
#    상승 갭만 '후보'가 되고, 하락 갭은 신규 진입 대상이 아니라
#    기존 근거 훼손 신호로만 쓴다(모멘텀 철학).
# ──────────────────────────────────────────────────────────────────────────

def evaluate_pead(reaction: dict, move: dict = None, regime: str = "") -> dict:
    """발표 후 반응 판정. 행동을 지시하지 않고 '상태'만 돌려준다.

    반환: {code, label, reasons[], is_candidate, is_damage}
    """
    out = {"code": "na", "label": PEAD_LABELS["na"], "reasons": [],
           "is_candidate": False, "is_damage": False}
    if not (isinstance(reaction, dict) and reaction.get("ok")):
        out["reasons"].append((reaction or {}).get("reason") or "측정 불가")
        return out

    gap = _num(reaction.get("gap_pct"))
    if gap is None:
        out["reasons"].append("갭 산출 불가")
        return out

    vr = _num(reaction.get("volume_ratio"))
    held = reaction.get("gap_held")
    med = _num((move or {}).get("median_pct")) if isinstance(move, dict) else None

    # 반응 크기: 예상 대비 유의미한가
    significant = True
    if med is not None and med > 0:
        significant = abs(gap) >= med * PEAD_SURPRISE_RATIO
    reasons = [f"갭 {gap:+.1f}%"]
    if med is not None:
        reasons.append(f"예상 ±{med:.1f}%")
    if vr is not None:
        reasons.append(f"거래량 {vr:.1f}배")
    if held is not None:
        reasons.append("갭 유지" if held else "갭 되돌림")

    vol_ok = (vr is None) or (vr >= PEAD_VOLUME_MIN)

    if not significant:
        out.update({"code": "muted", "label": PEAD_LABELS["muted"], "reasons": reasons})
        return out

    if gap > 0:
        if held is not False and vol_ok:
            out.update({"code": "up_continue", "label": PEAD_LABELS["up_continue"],
                        "is_candidate": True})
            if str(regime or "") == "weak":
                out["reasons"] = reasons + ["단 레짐 약세 — 진입 근거로는 부족"]
                out["is_candidate"] = False
                return out
        else:
            out.update({"code": "up_faded", "label": PEAD_LABELS["up_faded"]})
    else:
        if held is not False and vol_ok:
            out.update({"code": "down_break", "label": PEAD_LABELS["down_break"],
                        "is_damage": True})
        else:
            out.update({"code": "down_recovered", "label": PEAD_LABELS["down_recovered"]})
    out["reasons"] = reasons
    return out


# ──────────────────────────────────────────────────────────────────────────
# 7) 방향 예측 채점 (C층 — 2차 도입). 1차에는 예측 생성을 하지 않지만
#    채점·집계 함수는 미리 둔다: 탭의 성적표 섹션이 '표본 0건'을 정상 렌더링하고,
#    시트 컬럼도 처음부터 확정되어 나중에 중간 삽입이 필요 없다.
#    ※ 기준선은 50% 가 아니다. 시장이 우상향하므로 '무조건 상승' 대조군이
#      진짜 기준선이며, accuracy_summary 는 그 값을 항상 함께 돌려준다.
# ──────────────────────────────────────────────────────────────────────────

PRED_MIN_SAMPLE = 30
PRED_MIN_ACCURACY = 55.0

DIRECTION_UP = "상승 우세"
DIRECTION_DOWN = "하락 우세"
DIRECTION_NEUTRAL = "중립"


def score_prediction(pred_direction: str, gap_pct) -> str:
    """예측 채점. '중립'은 채점 제외(표본에 넣지 않는다)."""
    p = str(pred_direction or "").strip()
    g = _num(gap_pct)
    if g is None or p not in (DIRECTION_UP, DIRECTION_DOWN):
        return ""      # 미채점
    if g > 0:
        return "hit" if p == DIRECTION_UP else "miss"
    if g < 0:
        return "hit" if p == DIRECTION_DOWN else "miss"
    return ""


def accuracy_summary(rows: list) -> dict:
    """[{Pred_Direction, Gap_Pct}, ...] → 적중률 + 대조군 + 유효성.

    반환: {n, hits, accuracy, baseline_accuracy, edge, valid, banner}
      baseline_accuracy: '무조건 상승 예측' 대조군 성적 (진짜 기준선)
    """
    scored, ups, total = 0, 0, 0
    hits = 0
    for r in (rows or []):
        g = _num((r or {}).get("Gap_Pct") if isinstance(r, dict) else None)
        if g is None or g == 0:
            continue
        total += 1
        if g > 0:
            ups += 1
        s = score_prediction((r or {}).get("Pred_Direction"), g)
        if s == "hit":
            scored += 1
            hits += 1
        elif s == "miss":
            scored += 1

    acc = round(hits / scored * 100.0, 1) if scored else None
    base = round(ups / total * 100.0, 1) if total else None
    out = {"n": scored, "hits": hits, "accuracy": acc,
           "baseline_accuracy": base, "edge": None, "valid": False, "banner": ""}
    if acc is not None and base is not None:
        out["edge"] = round(acc - base, 1)

    if scored < PRED_MIN_SAMPLE:
        out["banner"] = (f"표본 {scored}건 (최소 {PRED_MIN_SAMPLE}건) — "
                         f"참고 불가. 성적이 안정되기 전까지 판단에 쓰지 마세요.")
    elif acc is None or acc < PRED_MIN_ACCURACY:
        out["banner"] = (f"적중률 {acc:.1f}% — 현재 이 신호는 유의미하지 않습니다. "
                         f"판단에 반영하지 마세요.")
    elif base is not None and acc <= base:
        out["banner"] = (f"적중률 {acc:.1f}% ≤ 무조건상승 대조군 {base:.1f}% — "
                         f"엣지 없음. 판단에 반영하지 마세요.")
    else:
        out["valid"] = True
        out["banner"] = ""
    return out


# ──────────────────────────────────────────────────────────────────────────
# 8) Earnings_Events 시트 스키마 (SSOT)
#    C층 컬럼을 1차부터 확정해 둔다 — 나중 중간 삽입은 automation 의
#    범위 지정을 깨뜨린다(Watchlist Account 컬럼 사례).
# ──────────────────────────────────────────────────────────────────────────

EVENTS_WORKSHEET = "Earnings_Events"
EVENTS_COLS = [
    # 사전 (D-3 스냅샷)
    "Event_ID", "Ticker", "Earnings_Date", "Date_Source", "Timing",
    "Snapshot_At", "Price_At_Snapshot",
    "Exp_Median_Pct", "Exp_Worst_Pct", "Exp_P25_Pct", "Exp_P75_Pct",
    "ATR_Multiple", "Sample_N", "Move_Confidence",
    # 예측 (C층 — 1차 공란)
    "Pred_Direction", "Pred_Score", "Pred_Confidence", "Feature_JSON", "Pred_Narrative",
    # 사후 (반응일 5pm)
    "Actual_EPS", "Est_EPS", "Surprise_Pct", "Gap_Pct", "Volume_Ratio",
    "Gap_Held", "PEAD_Verdict", "Pred_Hit", "Verified_At",
    # 지연 (D+5)
    "D5_Return_Pct", "Notes",
]
EVENTS_NCOL = len(EVENTS_COLS)


def event_id(ticker: str, earnings_date: str) -> str:
    return f"{str(ticker or '').strip().upper()}_{str(earnings_date or '')[:10]}"


def snapshot_row(ev: dict, move: dict, price=None, now_et: str = "") -> list:
    """사전 스냅샷 → 시트 행. C층 컬럼과 사후 컬럼은 공란으로 자리만 확보."""
    m = move or {}
    row = [
        event_id(ev.get("ticker"), ev.get("earnings_date")),
        str(ev.get("ticker") or "").upper(),
        str(ev.get("earnings_date") or ""),
        str(ev.get("date_source") or ""),
        str(ev.get("timing") or ""),
        str(now_et or ""),
        ("" if _num(price) is None else round(float(price), 4)),
        _blank(m.get("median_pct")), _blank(m.get("worst_down_pct")),
        _blank(m.get("p25")), _blank(m.get("p75")),
        _blank(m.get("atr_multiple")), int(m.get("sample_n") or 0),
        str(m.get("confidence") or ""),
    ]
    row += [""] * 5                       # 예측 (C층)
    row += [""] * 8                       # 사후
    row += [""] * 2                       # 지연
    return (row + [""] * EVENTS_NCOL)[:EVENTS_NCOL]


def _blank(v):
    n = _num(v)
    return "" if n is None else n


def blocked_tickers(rows: list, today=None, block_days: int = ENTRY_BLOCK_DAYS) -> dict:
    """Earnings_Events 행 목록 → {TICKER: 사유} (현재 진입 차단 중인 종목).

    run_watchlist_alerts 가 시트 1회 조회로 게이트를 참조하기 위한 순수 함수.
    스크립트 간 실행 순서에 의존하지 않도록 **시트를 공유 상태로** 쓴다.
      · 아직 발표 전(Gap_Pct 공란)이고 D-block_days 이내인 행만 차단 대상.
      · 행이 없으면 차단하지 않는다(fail-open) — 실적 워치가 한 번도 안 돌았거나
        실패한 상황에서 매수 알림을 통째로 막아버리는 쪽이 더 나쁘다.
    """
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    out = {}
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        if str(r.get("Gap_Pct") or "").strip():
            continue                       # 이미 발표·측정 완료
        d = _d(r.get("Earnings_Date"))
        if d is None:
            continue
        dd = int((d - t0).days)
        if not (0 <= dd <= int(block_days)):
            continue
        tk = str(r.get("Ticker") or "").strip().upper()
        if not tk:
            continue
        mv = _num(r.get("Exp_Median_Pct"))
        why = f"실적 D-{dd}"
        if mv is not None:
            why += f" · 예상 갭 ±{mv:.1f}%"
        prev = out.get(tk)
        if prev is None or dd < prev[0]:
            out[tk] = (dd, why)
    return {k: v[1] for k, v in out.items()}


def parse_events(values: list) -> list[dict]:
    """Earnings_Events get_all_values → dict 목록 (헤더 제외)."""
    out = []
    if not values or len(values) < 2:
        return out
    for i, r in enumerate(values[1:], start=2):
        r = (list(r) + [""] * EVENTS_NCOL)[:EVENTS_NCOL]
        d = {c: r[j] for j, c in enumerate(EVENTS_COLS)}
        d["_row"] = i
        out.append(d)
    return out
