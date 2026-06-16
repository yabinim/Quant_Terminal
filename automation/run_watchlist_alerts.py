# -*- coding: utf-8 -*-
"""
run_watchlist_alerts.py
───────────────────────
GitHub Actions 자동 실행: Watchlist 종목의 레짐/타이밍 상태를 평가하여
상태 전환 기반 알림(2일 확정 + 재무장)을 발동하고, Alert_LastState 를 시트에 저장,
발동 건을 Gmail 로 발송한다.

⚠️ app.py · regime_core.py 와 lockstep:
   - 동일한 regime_core 엔진/임계값/RSI 밴드/알림 상태머신을 그대로 import.
   - Watchlist 12열 스키마는 app.py _WATCHLIST_SHEET_COLS 와 동일해야 함.
   - 데이터 소스는 app.py 와 동일한 FMP /stable/ historical-price-eod/full.
   - 하루 1회(장 마감 후) 호출 전제 — 호출당 pending 카운터가 1 진행되므로
     "2일 연속 확정"은 거래일 2일을 의미(휴장일은 아래 게이트로 건너뜀).

실행: python automation/run_watchlist_alerts.py   (repo root 에서)
"""

import os
import sys
import json
import smtplib
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import numpy as np
import pandas as pd
import pytz

import gspread
from google.oauth2.service_account import Credentials

# ── repo root 를 sys.path 에 추가 → regime_core(app.py와 동일 모듈) import ──────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import regime_core as rc  # noqa: E402

# ── 환경변수 ───────────────────────────────────────────────────────────────────
FMP_API_KEY        = os.environ["FMP_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]
GSPREAD_KEY_JSON   = os.environ["GSPREAD_KEY"]
_gcp_info = json.loads(GSPREAD_KEY_JSON)

# ── 상수 ───────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")
_FMP_BASE = "https://financialmodelingprep.com/stable"   # app.py 와 동일 (/stable/ 전용)
_FMP_TIMEOUT = 15
_SPREADSHEET_TITLE   = "Quant_DB"
_WATCHLIST_WORKSHEET = "Watchlist"

# app.py _WATCHLIST_SHEET_COLS 와 동일 (12열) — lockstep
_WL_COLS = ["ID", "Ticker", "Memo", "Alert_Price", "Alert_RSI", "Alert_MA200",
            "Saved_Price", "Date_Added", "Stop_Loss", "Target_Price",
            "Alert_States", "Alert_LastState"]
_WL_NCOL = len(_WL_COLS)
_WL_ALERT_DEFAULT = "entry,risk"
_COL_ALERT_STATES = 10      # 0-index
_COL_ALERT_LASTSTATE = 11   # 0-index → 시트 L열

# 보유(Portfolio) — app.py 와 lockstep
_PF_WORKSHEET = "Portfolios"                  # [ID, Account, Ticker, AvgPrice, Quantity, Date_Added]
_PFSTATE_WORKSHEET = "Portfolio_Alert_State"  # [Key, Alert_States, Alert_LastState, Updated_At]
_PFSTATE_COLS = ["Key", "Alert_States", "Alert_LastState", "Updated_At", "Stop_Loss", "Target_Price"]
_PF_ALERT_DEFAULT = "exit,risk"               # 보유 기본 알림 (손절은 exit의 ATR 트레일링에 포함)
_PF_INTRADAY_EVENTS = ("exit", "risk")        # 장중 보유 행동 가능 이벤트

# NYSE 휴장일 (run_narrative.py 와 동일 목록)
_NYSE_HOLIDAYS = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def is_market_open_today() -> bool:
    et_now = datetime.now(_ET)
    if et_now.weekday() >= 5:
        return False
    return et_now.strftime("%Y-%m-%d") not in _NYSE_HOLIDAYS


# ── Google Sheets ──────────────────────────────────────────────────────────────
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(_gcp_info, scopes=scopes)
    return gspread.authorize(creds)


# ── FMP 가격 히스토리 (app.py cached_timing_price_history 와 동일 컬럼: OHLCV) ──
def _fmp_price_history(ticker: str, limit: int = 252) -> pd.DataFrame:
    try:
        r = requests.get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker}&limit={limit}&apikey={FMP_API_KEY}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "date" not in df.columns or "close" not in df.columns:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        out = pd.DataFrame(index=df.index)
        for src, dst in [("open", "Open"), ("high", "High"), ("low", "Low"),
                         ("close", "Close"), ("volume", "Volume")]:
            if src in df.columns:
                out[dst] = pd.to_numeric(df[src], errors="coerce")
        return out.dropna(subset=["Close"])
    except Exception:
        return pd.DataFrame()


# ── 장중 실시간 가격 (잠정 봉 주입용) ───────────────────────────────────────────
def _fmp_quote_price(ticker: str):
    """/stable/quote → 현재가(price). 장중 잠정 판정용."""
    try:
        r = requests.get(f"{_FMP_BASE}/quote?symbol={ticker}&apikey={FMP_API_KEY}",
                         timeout=_FMP_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
        if not row:
            return None
        px = row.get("price")
        return float(px) if px not in (None, "") else None
    except Exception:
        return None


def _with_intraday_price(hist: pd.DataFrame, live_price) -> pd.DataFrame:
    """완성된 일봉 시리즈에 '오늘 실시간 가격'을 마지막(잠정) 봉으로 주입.
    FMP full 히스토리가 이미 오늘 진행 중 봉을 포함하면 Close만 실시간으로 교체.
    """
    if hist is None or hist.empty or live_price is None:
        return hist
    today = pd.Timestamp(datetime.now(_ET).date())
    h = hist.copy()
    # 숫자 컬럼 float 보장 (잠정 가격 주입 시 int dtype 충돌 방지)
    for _c in ("Open", "High", "Low", "Close", "Volume"):
        if _c in h.columns:
            h[_c] = pd.to_numeric(h[_c], errors="coerce").astype(float)
    if len(h) and pd.Timestamp(h.index[-1]).normalize() == today:
        h.iloc[-1, h.columns.get_loc("Close")] = float(live_price)
    else:
        row = {c: float(live_price) for c in ("Open", "High", "Low", "Close") if c in h.columns}
        if "Volume" in h.columns:
            row["Volume"] = np.nan
        h = pd.concat([h, pd.DataFrame([row], index=[today])])
    return h


# ── 이메일 ─────────────────────────────────────────────────────────────────────
def send_email(subject: str, html_body: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_TO
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())
        print(f"[OK] 이메일 발송 완료 → {GMAIL_TO}")
        return True
    except Exception as e:
        print(f"[ERROR] 이메일 발송 실패: {e}")
        return False


_REGIME_KR = {"strong": "🟢 강세(대장주)", "sideways": "🟡 횡보", "weak": "🔴 약세"}


def _render_hit_card(h) -> str:
    """단일 알림 카드 HTML (워치리스트·보유 공용)."""
    an = h["an"]
    reg = an.get("regime", {})
    reg_label = _REGIME_KR.get(reg.get("regime"), "⚪")
    score = reg.get("score")
    band = reg.get("rsi_band", (None, None))
    html = (
        f"<div style='margin:10px 0;padding:12px;border:1px solid #e1e4e8;border-radius:8px'>"
        f"<div style='font-size:17px;font-weight:700'>{h['ticker']} "
        f"<span style='font-weight:400;color:#555;font-size:14px'>· {reg_label}"
        + (f" (강도 {score:.0f}/100 · RSI밴드 {band[0]:.0f}~{band[1]:.0f})"
           if score is not None and band[0] is not None else "")
        + "</span></div>"
    )
    for ev in h["fired"]:
        html += (
            f"<div style='margin-top:6px'><b>{ev.get('label','')}</b> — "
            f"{ev.get('message','')}</div>"
        )
    return html + "</div>"


def build_email_html(wl_by_user: dict, pf_by_user: dict, today: str) -> str:
    """매매 레이더 이메일 — 🔭 Watchlist(매수)와 💼 Portfolio(보유·매도) 섹션 분리,
    Portfolio는 account별로 그룹핑."""
    wl_by_user = wl_by_user or {}
    pf_by_user = pf_by_user or {}
    wl_total = sum(len(v) for v in wl_by_user.values())
    pf_total = sum(len(v) for v in pf_by_user.values())
    total = wl_total + pf_total

    parts = [
        "<div style='font-family:Apple SD Gothic Neo,Arial,sans-serif;max-width:680px;margin:0 auto'>",
        f"<h2 style='color:#1f6feb'>🔔 매매 레이더 — {total}건</h2>",
        f"<p style='color:#666'>{today} (ET) 장 마감 후 평가 · 2일 연속 확정된 상태 전환만 발송</p>",
    ]

    # 🔭 Watchlist (매수 후보)
    if wl_total:
        parts.append(
            "<h2 style='color:#1f6feb;border-bottom:2px solid #1f6feb;"
            "padding-bottom:6px;margin-top:24px'>🔭 Watchlist (매수 후보)</h2>"
        )
        for uid, hits in wl_by_user.items():
            parts.append(f"<h3 style='border-bottom:1px solid #eee;padding-bottom:6px'>👤 {uid}</h3>")
            for h in hits:
                parts.append(_render_hit_card(h))

    # 💼 Portfolio (보유 · 매도/관리) — account별 그룹
    if pf_total:
        parts.append(
            "<h2 style='color:#d29922;border-bottom:2px solid #d29922;"
            "padding-bottom:6px;margin-top:24px'>💼 Portfolio (보유 · 매도/관리)</h2>"
        )
        for uid, hits in pf_by_user.items():
            parts.append(f"<h3 style='border-bottom:1px solid #eee;padding-bottom:6px'>👤 {uid}</h3>")
            by_acct = {}
            for h in hits:
                by_acct.setdefault(str(h.get("account") or "기타"), []).append(h)
            for acct, ah in by_acct.items():
                parts.append(f"<h4 style='margin:14px 0 4px;color:#444'>🏦 {acct}</h4>")
                for h in ah:
                    parts.append(_render_hit_card(h))

    parts.append(
        "<p style='color:#999;font-size:12px;margin-top:20px'>"
        "본 메일은 regime_core 엔진(앱과 동일)으로 자동 평가되었습니다. "
        "상세는 Quant Terminal의 [3단계] 개별 종목 정밀 검사에서 확인하세요.</p></div>"
    )
    return "".join(parts)


# ── 평가 함수 (각 함수는 fired/hits dict 반환, 이메일은 main에서 합쳐 1회 발송) ──
def _open_ws(title):
    gc = get_gspread_client()
    sh = gc.open(_SPREADSHEET_TITLE)
    return sh, sh.worksheet(title)


def _open_pf_state(sh):
    titles = [w.title for w in sh.worksheets()]
    if _PFSTATE_WORKSHEET in titles:
        return sh.worksheet(_PFSTATE_WORKSHEET)
    ws = sh.add_worksheet(title=_PFSTATE_WORKSHEET, rows=2000, cols=len(_PFSTATE_COLS))
    _lc = chr(ord("A") + len(_PFSTATE_COLS) - 1)
    ws.update([_PFSTATE_COLS], range_name=f"A1:{_lc}1", value_input_option="USER_ENTERED")
    return ws


def _merge_fired(a, b):
    out = {k: list(v) for k, v in (a or {}).items()}
    for k, v in (b or {}).items():
        out.setdefault(k, []).extend(v)
    return out


def eval_watchlist_eod(spy_close, hist_cache, today):
    """워치리스트: 상태 전환 2일 확정 + Alert_LastState(L열) 저장. → fired_by_user"""
    fired_by_user = {}
    try:
        sh, ws = _open_ws(_WATCHLIST_WORKSHEET)
    except Exception as e:
        print(f"[INFO] Watchlist 시트 없음/실패 — 스킵: {e}")
        return fired_by_user
    vals = ws.get_all_values() or []
    if len(vals) < 2:
        print("[INFO] Watchlist 비어 있음.")
        return fired_by_user
    data_rows = vals[1:]
    laststate_col, n_eval = [], 0
    for r in data_rows:
        r = (list(r) + [""] * _WL_NCOL)[:_WL_NCOL]
        uid, tk = str(r[0]).strip(), str(r[1]).strip().upper()
        prev_state = str(r[_COL_ALERT_LASTSTATE]).strip()
        if not uid or not tk:
            laststate_col.append([prev_state]); continue
        enabled = rc.resolve_alert_events(r[_COL_ALERT_STATES], _WL_ALERT_DEFAULT)
        sl = pd.to_numeric(r[8], errors="coerce")
        tp = pd.to_numeric(r[9], errors="coerce")
        try:
            if tk not in hist_cache:
                hist_cache[tk] = _fmp_price_history(tk)
            hist = hist_cache[tk]
            if hist is None or hist.empty:
                laststate_col.append([prev_state]); continue
            an = rc.analyze_ticker(hist, spy_close=spy_close)
            fired, new_state = rc.evaluate_alert_transitions(
                an, enabled, prev_state, today_str=today, price=float(hist["Close"].iloc[-1]),
                stop_loss=(float(sl) if pd.notna(sl) else None),
                target_price=(float(tp) if pd.notna(tp) else None),
            )
            laststate_col.append([new_state]); n_eval += 1
            if fired:
                fired_by_user.setdefault(uid, []).append({"ticker": tk, "fired": fired, "an": an})
                print(f"  [FIRE-WL] {uid}/{tk}: {[e['event'] for e in fired]}")
        except Exception as e:
            print(f"  [WARN] {uid}/{tk} 평가 실패: {e}")
            laststate_col.append([prev_state])
    try:
        ws.update(laststate_col, range_name=f"L2:L{len(data_rows) + 1}", value_input_option="RAW")
        print(f"[OK] Watchlist Alert_LastState 저장: {len(laststate_col)}행 (평가 {n_eval})")
    except Exception as e:
        print(f"[ERROR] Watchlist 상태 저장 실패: {e}")
    return fired_by_user


def eval_portfolio_eod(spy_close, hist_cache, today):
    """보유: 청산/추세흔들림 상태 전환 2일 확정 + Portfolio_Alert_State 저장. → fired_by_user"""
    fired_by_user = {}
    try:
        sh, pf_ws = _open_ws(_PF_WORKSHEET)
    except Exception as e:
        print(f"[INFO] Portfolios 시트 없음 — 보유 스킵: {e}")
        return fired_by_user
    pf_vals = pf_ws.get_all_values() or []
    if len(pf_vals) < 2:
        print("[INFO] Portfolios 비어 있음.")
        return fired_by_user
    holdings = pf_vals[1:]

    state_ws = _open_pf_state(sh)
    st_vals = state_ws.get_all_values() or []
    state_map = {}
    for r in st_vals[1:]:
        r = (list(r) + [""] * 4)[:4]
        if str(r[0]).strip():
            state_map[str(r[0]).strip()] = {"states": str(r[1]).strip(), "last": str(r[2]).strip()}

    new_rows, n_eval = [], 0
    for r in holdings:
        r = (list(r) + [""] * 6)[:6]
        uid, account, tk = str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip().upper()
        if not uid or not tk:
            continue
        avg = pd.to_numeric(r[3], errors="coerce")
        key = f"{uid}|{account}|{tk}"
        states_csv = state_map.get(key, {}).get("states", "")
        enabled = rc.resolve_alert_events(states_csv, _PF_ALERT_DEFAULT)
        prev = state_map.get(key, {}).get("last", "")
        try:
            if tk not in hist_cache:
                hist_cache[tk] = _fmp_price_history(tk)
            hist = hist_cache[tk]
            if hist is None or hist.empty:
                new_rows.append([key, states_csv, prev, today]); continue
            an = rc.analyze_ticker(hist, spy_close=spy_close,
                                   entry_price=float(avg) if pd.notna(avg) else None)
            fired, new_state = rc.evaluate_alert_transitions(
                an, enabled, prev, today_str=today, price=float(hist["Close"].iloc[-1]),
            )
            new_rows.append([key, states_csv, new_state, today]); n_eval += 1
            if fired:
                fired_by_user.setdefault(uid, []).append(
                    {"ticker": tk, "account": account, "fired": fired, "an": an})
                print(f"  [FIRE-PF] {uid}/{account}/{tk}: {[e['event'] for e in fired]}")
        except Exception as e:
            print(f"  [WARN] {uid}/{account}/{tk} 평가 실패: {e}")
            new_rows.append([key, states_csv, prev, today])

    # 전체 덮어쓰기(드리프트 없음). 이전이 더 길면 빈 행으로 잔재 정리.
    try:
        prev_len = max(0, len(st_vals) - 1)
        padded = new_rows + [["", "", "", ""]] * max(0, prev_len - len(new_rows))
        body = [_PFSTATE_COLS[:4]] + padded   # A:D만 관리(상태머신). E/F(손절·목표)는 보존
        state_ws.update(body, range_name=f"A1:D{len(body)}", value_input_option="RAW")
        print(f"[OK] Portfolio_Alert_State 저장: {len(new_rows)}행 (평가 {n_eval})")
    except Exception as e:
        print(f"[ERROR] Portfolio_Alert_State 저장 실패: {e}")
    return fired_by_user


def _pf_state_map_readonly(sh):
    try:
        state_ws = _open_pf_state(sh)
        st_vals = state_ws.get_all_values() or []
    except Exception:
        return {}
    m = {}
    for r in st_vals[1:]:
        r = (list(r) + [""] * 6)[:6]
        if str(r[0]).strip():
            _sl = pd.to_numeric(r[4], errors="coerce")
            _tp = pd.to_numeric(r[5], errors="coerce")
            m[str(r[0]).strip()] = {
                "states": str(r[1]).strip(),
                "stop_loss": float(_sl) if pd.notna(_sl) else None,
                "target_price": float(_tp) if pd.notna(_tp) else None,
            }
    return m


def eval_watchlist_intraday(spy_close, hist_cache, quote_cache, today):
    hits_by_user = {}
    try:
        sh, ws = _open_ws(_WATCHLIST_WORKSHEET)
    except Exception:
        return hits_by_user
    vals = ws.get_all_values() or []
    if len(vals) < 2:
        return hits_by_user
    ev = ("entry", "risk", "exit", "price")
    for r in vals[1:]:
        r = (list(r) + [""] * _WL_NCOL)[:_WL_NCOL]
        uid, tk = str(r[0]).strip(), str(r[1]).strip().upper()
        if not uid or not tk:
            continue
        enabled = rc.resolve_alert_events(r[_COL_ALERT_STATES], _WL_ALERT_DEFAULT)
        if not any(e in enabled for e in ev):
            continue
        sl = pd.to_numeric(r[8], errors="coerce")
        tp = pd.to_numeric(r[9], errors="coerce")
        try:
            if tk not in hist_cache:
                hist_cache[tk] = _fmp_price_history(tk)
                quote_cache[tk] = _fmp_quote_price(tk)
            hist = _with_intraday_price(hist_cache[tk], quote_cache[tk])
            if hist is None or hist.empty:
                continue
            an = rc.analyze_ticker(hist, spy_close=spy_close)
            if not an.get("regime", {}).get("enough_data"):
                continue
            live = quote_cache[tk] if quote_cache[tk] is not None else float(hist["Close"].iloc[-1])
            conds = rc.alert_conditions(an, price=live,
                                        stop_loss=(float(sl) if pd.notna(sl) else None),
                                        target_price=(float(tp) if pd.notna(tp) else None))
            active = [{"event": e, "label": rc.ALERT_EVENT_LABELS[e], "message": conds[e][1]}
                      for e in ev if e in enabled and conds.get(e, (False, ""))[0]]
            if active:
                hits_by_user.setdefault(uid, []).append({"ticker": tk, "fired": active, "an": an})
                print(f"  [INTRADAY-WL] {uid}/{tk}: {[a['event'] for a in active]}")
        except Exception as e:
            print(f"  [WARN] {uid}/{tk} 장중 평가 실패: {e}")
    return hits_by_user


def eval_portfolio_intraday(spy_close, hist_cache, quote_cache, today):
    hits_by_user = {}
    try:
        sh, pf_ws = _open_ws(_PF_WORKSHEET)
    except Exception:
        return hits_by_user
    pf_vals = pf_ws.get_all_values() or []
    if len(pf_vals) < 2:
        return hits_by_user
    state_map = _pf_state_map_readonly(sh)
    for r in pf_vals[1:]:
        r = (list(r) + [""] * 6)[:6]
        uid, account, tk = str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip().upper()
        if not uid or not tk:
            continue
        avg = pd.to_numeric(r[3], errors="coerce")
        key = f"{uid}|{account}|{tk}"
        _pf_st = state_map.get(key, {})
        enabled = rc.resolve_alert_events(_pf_st.get("states"), _PF_ALERT_DEFAULT)
        _sl = _pf_st.get("stop_loss")
        _tp = _pf_st.get("target_price")
        _has_plan = (_sl is not None) or (_tp is not None)
        # 손절/목표를 설정해 둔 보유는 가격 도달도 평가(설정 = 암묵적 알림 동의)
        if not (any(e in enabled for e in _PF_INTRADAY_EVENTS) or _has_plan):
            continue
        try:
            if tk not in hist_cache:
                hist_cache[tk] = _fmp_price_history(tk)
                quote_cache[tk] = _fmp_quote_price(tk)
            hist = _with_intraday_price(hist_cache[tk], quote_cache[tk])
            if hist is None or hist.empty:
                continue
            an = rc.analyze_ticker(hist, spy_close=spy_close,
                                   entry_price=float(avg) if pd.notna(avg) else None)
            if not an.get("regime", {}).get("enough_data"):
                continue
            live = quote_cache[tk] if quote_cache[tk] is not None else float(hist["Close"].iloc[-1])
            conds = rc.alert_conditions(an, price=live, stop_loss=_sl, target_price=_tp)
            active = [{"event": e, "label": rc.ALERT_EVENT_LABELS[e], "message": conds[e][1]}
                      for e in _PF_INTRADAY_EVENTS if e in enabled and conds.get(e, (False, ""))[0]]
            # 손절/목표 도달(price)은 플랜이 설정된 보유에서 항상 발화
            if _has_plan and conds.get("price", (False, ""))[0]:
                active.append({"event": "price", "label": rc.ALERT_EVENT_LABELS["price"],
                               "message": conds["price"][1]})
            if active:
                hits_by_user.setdefault(uid, []).append(
                    {"ticker": tk, "account": account, "fired": active, "an": an})
                print(f"  [INTRADAY-PF] {uid}/{account}/{tk}: {[a['event'] for a in active]}")
        except Exception as e:
            print(f"  [WARN] {uid}/{account}/{tk} 장중 평가 실패: {e}")
    return hits_by_user


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["eod", "intraday"], default="eod",
                        help="eod=마감 후 확정(기본) / intraday=장중 잠정 헤드업")
    parser.add_argument("--scope", choices=["watchlist", "portfolio", "both"], default="both",
                        help="평가 대상 (기본 both)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"[START] 알림 ({args.mode}/{args.scope}): "
          f"{datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    if not is_market_open_today():
        print("[SKIP] 오늘은 NYSE 휴장일 — 평가 건너뜀(카운터 미진행).")
        return

    today = datetime.now(_ET).strftime("%Y-%m-%d")
    do_wl = args.scope in ("watchlist", "both")
    do_pf = args.scope in ("portfolio", "both")

    spy_hist = _fmp_price_history("SPY")
    spy_close = spy_hist["Close"] if (spy_hist is not None and not spy_hist.empty) else None
    hist_cache, quote_cache = {}, {}

    try:
        if args.mode == "intraday":
            wl_res, pf_res = {}, {}
            if do_wl:
                wl_res = eval_watchlist_intraday(spy_close, hist_cache, quote_cache, today)
            if do_pf:
                pf_res = eval_portfolio_intraday(spy_close, hist_cache, quote_cache, today)
            total = (sum(len(v) for v in wl_res.values())
                     + sum(len(v) for v in pf_res.values()))
            if total:
                html = ("<p style='background:#fff8e1;padding:8px;border-radius:6px;color:#8a6d00'>"
                        "⏱️ <b>장중 잠정 신호</b> — 진행 중인 봉 기준이라 마감까지 바뀔 수 있습니다. "
                        "분할 매수/청산 참고용.</p>") + build_email_html(wl_res, pf_res, today)
                send_email(f"⏱️ [장중] 후보 — {total}건 ({today} ET)", html)
            else:
                print("[INFO] 장중 활성 후보 없음 — 이메일 생략.")
        else:
            wl_res, pf_res = {}, {}
            if do_wl:
                wl_res = eval_watchlist_eod(spy_close, hist_cache, today)
            if do_pf:
                pf_res = eval_portfolio_eod(spy_close, hist_cache, today)
            total = (sum(len(v) for v in wl_res.values())
                     + sum(len(v) for v in pf_res.values()))
            if total:
                send_email(f"🔔 매매 레이더 — {total}건 ({today} ET)",
                           build_email_html(wl_res, pf_res, today))
            else:
                print("[INFO] 발동된 알림 없음 — 이메일 생략.")
    except Exception as e:
        print(f"[ERROR] 평가 실패: {e}")
        traceback.print_exc()

    print(f"[DONE] {datetime.now(_KST).strftime('%H:%M KST')}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
