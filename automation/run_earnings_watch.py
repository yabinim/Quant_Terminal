# -*- coding: utf-8 -*-
"""
run_earnings_watch.py
─────────────────────
실적 레이더 자동화 — 평일 5PM ET 단일 실행, 3패스.

  1) 사전 (D-3 도달): 예상 변동폭 스냅샷 저장 → Earnings_Events
  2) 사후 (반응일 종가 확정): 갭 측정 · PEAD 판정 · (C층 도입 시) 예측 채점
  3) 지연 (D+5): D5_Return_Pct 채움

그리고 수신자별로 **본인 종목만** 담은 메일 1통을 보낸다.

왜 8AM job 이 없는가:
  반응 판정은 '종가'가 있어야 완결된다.
    · BMO 발표 → 당일이 반응일  → 당일 종가로 확정
    · AMC 발표 → 다음날이 반응일 → 다음날 종가로 확정
  둘 다 장 마감 후에만 확정되므로 5PM 단일 실행이 정확하다.

⚠️ lockstep: earnings_core / accounts_core / users_core / regime_core 와 함께 배포.
   Earnings_Events 시트는 티커 단위(관리자 소유·게스트 읽기 전용)이고,
   계좌별 축소 판정은 저장하지 않고 **수신자별 런타임 계산**한다.
   → 같은 티커를 여러 사용자가 보유해도 시트 행은 하나, FMP 조회도 1회.

실행: python automation/run_earnings_watch.py   (repo root 에서)
"""

import json
import os
import smtplib
import sys
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import gspread
import numpy as np
import pandas as pd
import pytz
import requests
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import accounts_core as ac    # noqa: E402
import earnings_core as ec    # noqa: E402
import users_core as uc       # noqa: E402

# ── 환경변수 ──────────────────────────────────────────────────────────────
FMP_API_KEY        = os.environ["FMP_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]
_gcp_info = json.loads(os.environ["GSPREAD_KEY"])

_ET = pytz.timezone("America/New_York")
_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 15
_SPREADSHEET_TITLE = "Quant_DB"
_WATCHLIST_WORKSHEET = "Watchlist"
_PF_WORKSHEET = "Portfolios"
_PROFILE_WORKSHEET = "Account_Profile"
_USERS_WORKSHEET = "Users"

_HIST_LIMIT = 900          # 8분기(≈2년) 갭 이력 + ATR 산출에 여유
_THESIS_WORKSHEET = "Thesis"     # app.py _THESIS_SHEET_COLS 와 lockstep
_CORE_CATEGORY = "core_dca"      # 코어/정기적립 마커 (app.py save_thesis_row 와 동일)

_NYSE_HOLIDAYS = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def is_market_open_today() -> bool:
    now = datetime.now(_ET)
    return now.weekday() < 5 and now.strftime("%Y-%m-%d") not in _NYSE_HOLIDAYS


# ── Sheets ────────────────────────────────────────────────────────────────
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    return gspread.authorize(Credentials.from_service_account_info(_gcp_info, scopes=scopes))


_SH = None


def _sheet():
    global _SH
    if _SH is None:
        _SH = get_gspread_client().open(_SPREADSHEET_TITLE)
    return _SH


def _ws(title: str, cols: list = None):
    """워크시트 조회. 없으면 헤더와 함께 생성(신규 배포 첫 실행 대비)."""
    sh = _sheet()
    try:
        return sh.worksheet(title)
    except Exception:
        if not cols:
            raise
        w = sh.add_worksheet(title=title, rows=2000, cols=max(len(cols), 26))
        w.update([cols], range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(cols))}",
                  value_input_option="USER_ENTERED")
        print(f"[INIT] '{title}' 시트 생성")
        return w


def _events_ws():
    return _ws(ec.EVENTS_WORKSHEET, ec.EVENTS_COLS)


def _col_a1(n: int) -> str:
    """1-base 열 번호 → A1 문자 (AA 이상 대응)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _safe_update(ws, values: list, first_row: int, ncol: int):
    """A열 앵커 명시 범위 기록 — append_row 계단 드리프트 방지(프로젝트 원칙 #3)."""
    if not values:
        return
    last = _col_a1(ncol)
    ws.update(values, range_name=f"A{first_row}:{last}{first_row + len(values) - 1}",
              value_input_option="USER_ENTERED")


# ── FMP ───────────────────────────────────────────────────────────────────
def fmp_price_history(ticker: str, limit: int = _HIST_LIMIT) -> pd.DataFrame:
    try:
        r = requests.get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker}&limit={limit}"
            f"&apikey={FMP_API_KEY}", timeout=_FMP_TIMEOUT)
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


# ── 대상 수집 ─────────────────────────────────────────────────────────────
def load_universe():
    """수신자별 관심 종목 수집.

    반환: (holdings, watch, all_tickers)
      holdings  : {uid: {account: [{ticker, qty, avg}]}}
      watch     : {uid: {ticker: account}}
      wl_stops  : {(uid, TICKER): Stop_Loss}  — 게이트 손절폭 산출용
    """
    holdings, watch, tickers, wl_stops = {}, {}, set(), {}

    try:
        vals = _ws(_PF_WORKSHEET).get_all_values() or []
        for r in vals[1:]:
            r = (list(r) + [""] * 8)[:8]
            uid, acct, tk = str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip().upper()
            if not uid or not tk:
                continue
            avg = pd.to_numeric(r[3], errors="coerce")
            qty = pd.to_numeric(r[4], errors="coerce")
            if pd.isna(qty) or float(qty) <= 0:
                continue
            holdings.setdefault(uid, {}).setdefault(acct, []).append({
                "ticker": tk, "qty": float(qty),
                "avg": (float(avg) if pd.notna(avg) else None),
            })
            tickers.add(tk)
    except Exception as e:
        print(f"[WARN] Portfolios 로드 실패: {e}")

    try:
        vals = _ws(_WATCHLIST_WORKSHEET).get_all_values() or []
        for r in vals[1:]:
            r = (list(r) + [""] * 13)[:13]
            uid, tk = str(r[0]).strip(), str(r[1]).strip().upper()
            if not uid or not tk:
                continue
            watch.setdefault(uid, {})[tk] = str(r[12]).strip()
            _sl = pd.to_numeric(r[8], errors="coerce")   # I열 = Stop_Loss
            if pd.notna(_sl) and float(_sl) > 0:
                wl_stops[(uid, tk)] = float(_sl)
            tickers.add(tk)
    except Exception as e:
        print(f"[WARN] Watchlist 로드 실패: {e}")

    return holdings, watch, sorted(tickers), wl_stops


def load_profiles(uid: str, cache: dict):
    if "_vals" not in cache:
        try:
            cache["_vals"] = _ws(_PROFILE_WORKSHEET).get_all_values() or []
        except Exception:
            cache["_vals"] = []
    if uid not in cache:
        cache[uid] = ac.parse_profiles(cache["_vals"], uid)
    return cache[uid]


def load_core_keys() -> set:
    """코어/정기적립 보유 키 {(uid_lower, account_lower, TICKER)}.

    Portfolios 시트에는 메모 컬럼이 없다. 코어 여부는 Thesis 시트의
    Narrative_Category == 'core_dca' 가 SSOT (app.py save_thesis_row 와 동일).
    """
    keys = set()
    try:
        vals = _ws(_THESIS_WORKSHEET).get_all_values() or []
        for r in vals[1:]:
            r = (list(r) + [""] * 7)[:7]
            if str(r[4]).strip().lower() != _CORE_CATEGORY:
                continue
            uid = str(r[0]).strip().lower()
            tk = str(r[1]).strip().upper()
            acct = str(r[2]).strip().lower()
            if uid and tk:
                keys.add((uid, acct, tk))
    except Exception as e:
        print(f"[INFO] Thesis 조회 생략(코어 면제 미적용): {e}")
    return keys


# ── 패스 1: 사전 스냅샷 ───────────────────────────────────────────────────
def pass_snapshot(tickers, existing, hist_cache, today, now_et):
    """D-SNAPSHOT_DAYS 도달 종목의 예상 변동폭 스냅샷. 이미 있으면 건너뜀."""
    new_rows, snapshots = [], {}
    for tk in tickers:
        try:
            ev = ec.fetch_next_earnings(tk, today=today, key=FMP_API_KEY)
            if not ev:
                continue
            dd = ev["days_until"]
            if dd > ec.SCAN_HORIZON_DAYS:
                continue
            eid = ec.event_id(tk, ev["earnings_date"])
            if eid in existing:
                snapshots[tk] = existing[eid]
                continue
            if dd > ec.SNAPSHOT_DAYS:
                continue                       # 아직 스냅샷 시점 아님

            hist = hist_cache.get(tk)
            if hist is None:
                hist = hist_cache[tk] = fmp_price_history(tk)
            if hist is None or hist.empty:
                print(f"  [WARN] {tk} 가격 이력 없음 — 스냅샷 생략")
                continue

            past = ec.past_earnings_dates(tk, today=today, key=FMP_API_KEY)
            gaps = ec.gap_history(hist, past)
            move = ec.expected_move(gaps, atr_pct=ec.atr_pct_of(hist))
            px = float(hist["Close"].iloc[-1])
            row = ec.snapshot_row(ev, move, price=px, now_et=now_et)
            new_rows.append(row)
            rec = {c: row[i] for i, c in enumerate(ec.EVENTS_COLS)}
            rec["_move"] = move
            rec["_event"] = ev
            snapshots[tk] = rec
            print(f"  [SNAP] {tk} D-{dd} {ev['earnings_date']}({ev['date_source']}) "
                  f"±{move.get('median_pct')}% n={move.get('sample_n')}")
        except Exception as e:
            print(f"  [WARN] {tk} 스냅샷 실패: {e}")
    return new_rows, snapshots


# ── 패스 2·3: 사후 측정 / 지연 ────────────────────────────────────────────
def pass_verify(rows, hist_cache, today):
    """반응일 종가가 확정된 행의 갭·PEAD 측정. 반환: (updates, results)
       updates: [(row_index, {col: value})]"""
    updates, results = [], []
    for r in rows:
        try:
            if str(r.get("Gap_Pct") or "").strip():
                continue                      # 이미 측정 완료
            d = ec._d(r.get("Earnings_Date"))
            if d is None or d > pd.Timestamp(today):
                continue
            tk = str(r.get("Ticker") or "").strip().upper()
            if not tk:
                continue
            hist = hist_cache.get(tk)
            if hist is None:
                hist = hist_cache[tk] = fmp_price_history(tk)
            if hist is None or hist.empty:
                continue

            m = ec.measure_reaction(hist, r.get("Earnings_Date"), r.get("Timing", ""))
            if not m["ok"]:
                continue
            # 반응일이 아직 오늘 이후면 종가 미확정 → 다음 실행으로 미룬다
            rd = ec._d(m["reaction_date"])
            if rd is None or rd > pd.Timestamp(today):
                continue

            move = {"ok": True, "median_pct": ec._num(r.get("Exp_Median_Pct")),
                    "worst_down_pct": ec._num(r.get("Exp_Worst_Pct"))}
            if move["median_pct"] is None:
                move["ok"] = False
            pead = ec.evaluate_pead(m, move)
            hit = ec.score_prediction(r.get("Pred_Direction"), m["gap_pct"])

            patch = {
                "Gap_Pct": m["gap_pct"],
                "Volume_Ratio": ("" if m["volume_ratio"] is None else m["volume_ratio"]),
                "Gap_Held": ("" if m["gap_held"] is None else ("TRUE" if m["gap_held"] else "FALSE")),
                "PEAD_Verdict": pead["code"],
                "Pred_Hit": hit,
                "Verified_At": datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET"),
            }
            updates.append((r["_row"], patch))
            results.append({**r, **patch, "_pead": pead, "_reaction": m})
            print(f"  [VERIFY] {tk} 갭 {m['gap_pct']:+.1f}% → {pead['code']}")
        except Exception as e:
            print(f"  [WARN] 측정 실패({r.get('Ticker')}): {e}")
    return updates, results


def pass_delayed(rows, hist_cache, today):
    """발표 D+5 도달 행의 D5_Return_Pct 채움."""
    updates = []
    for r in rows:
        try:
            if str(r.get("D5_Return_Pct") or "").strip():
                continue
            if not str(r.get("Gap_Pct") or "").strip():
                continue
            d = ec._d(r.get("Earnings_Date"))
            if d is None or (pd.Timestamp(today) - d).days < 7:
                continue
            tk = str(r.get("Ticker") or "").strip().upper()
            hist = hist_cache.get(tk)
            if hist is None:
                hist = hist_cache[tk] = fmp_price_history(tk)
            if hist is None or hist.empty:
                continue
            i = ec.resolve_reaction_index(hist, r.get("Earnings_Date"), r.get("Timing", ""))
            if i is None or i + 5 >= len(hist):
                continue
            base, end = float(hist["Close"].iloc[i]), float(hist["Close"].iloc[i + 5])
            if base <= 0:
                continue
            updates.append((r["_row"], {"D5_Return_Pct": round((end - base) / base * 100.0, 2)}))
        except Exception as e:
            print(f"  [WARN] D+5 실패({r.get('Ticker')}): {e}")
    return updates


# ── 수신자별 판정 (계좌 프로필 반영) ──────────────────────────────────────
def build_user_report(uid, holdings, watch, snapshots, results, prof_cache, hist_cache,
                      core_keys=None, wl_stops=None):
    """수신자 1명분 리포트. 계좌별 축소 판정은 여기서 런타임 계산한다."""
    profiles = load_profiles(uid, prof_cache)
    pre, blocked, post = [], [], []

    # 보유 — 축소 판정
    for acct, rows in (holdings.get(uid) or {}).items():
        prof = ac.get_profile(profiles, acct)
        cap = ac.resolve_earn_trim_cap(prof)
        price_map = {}
        for h in rows:
            hist = hist_cache.get(h["ticker"])
            if hist is not None and not hist.empty:
                price_map[h["ticker"]] = float(hist["Close"].iloc[-1])
        eqc = ac.compute_equity(
            [(h["ticker"], h["qty"], h["avg"]) for h in rows], price_map, prof["Cash"])
        equity = eqc["equity"]
        for h in rows:
            snap = snapshots.get(h["ticker"])
            if not snap:
                continue
            px = price_map.get(h["ticker"]) or h["avg"]
            if not px:
                continue
            move = snap.get("_move") or {
                "ok": ec._num(snap.get("Exp_Median_Pct")) is not None,
                "median_pct": ec._num(snap.get("Exp_Median_Pct")),
                "worst_down_pct": ec._num(snap.get("Exp_Worst_Pct")),
            }
            t = ec.evaluate_trim(px * h["qty"], equity, move,
                                 trim_cap_pct=cap["cap_pct"],
                                 min_trade_dollars=prof["Min_Trade_Dollars"],
                                 is_core=((str(uid).lower(), acct.lower(), h["ticker"])
                                          in (core_keys or set())))
            pre.append({"ticker": h["ticker"], "account": acct, "snap": snap,
                        "trim": t, "cap": cap, "move": move})

    # 워치리스트 — 진입 차단
    wl_stops = wl_stops or {}
    for tk, acct in (watch.get(uid) or {}).items():
        snap = snapshots.get(tk)
        if not snap:
            continue
        move = snap.get("_move") or {
            "ok": ec._num(snap.get("Exp_Median_Pct")) is not None,
            "median_pct": ec._num(snap.get("Exp_Median_Pct")),
        }
        ev = snap.get("_event") or {}
        dd = ev.get("days_until")
        if dd is None:
            d = ec._d(snap.get("Earnings_Date"))
            dd = int((d - pd.Timestamp.today().normalize()).days) if d is not None else None
        # 손절폭: 워치리스트 수동 손절 우선, 없으면 ATR 추정(app.py 와 동일 규약)
        stop_pct, stop_src = None, ""
        hist = hist_cache.get(tk)
        px = None
        if hist is not None and not hist.empty:
            px = float(hist["Close"].iloc[-1])
        try:
            sl = float(wl_stops.get((uid, tk)) or 0.0)
            if sl > 0 and px and px > 0:
                stop_pct, stop_src = abs((px - sl) / px * 100.0), "manual"
        except (TypeError, ValueError):
            stop_pct = None
        if stop_pct is None and hist is not None and not hist.empty:
            stop_pct = ec.derived_stop_pct(hist, price=px)
            stop_src = "atr" if stop_pct is not None else ""
        g = ec.evaluate_entry_gate(move, planned_stop_pct=stop_pct, days_until=dd,
                                   earnings_date=snap.get("Earnings_Date"),
                                   stop_source=stop_src)
        if g["blocked"]:
            blocked.append({"ticker": tk, "account": acct, "gate": g, "snap": snap})

    # 사후 — 본인 관심 종목만
    mine = set((watch.get(uid) or {}).keys())
    for acct_rows in (holdings.get(uid) or {}).values():
        mine |= {h["ticker"] for h in acct_rows}
    for r in results:
        if str(r.get("Ticker") or "").upper() in mine:
            post.append(r)

    order = {"trim_hard": 0, "trim": 1, "core": 3, "disabled": 4, "na": 5, "hold": 6}
    pre.sort(key=lambda x: (order.get(x["trim"]["code"], 9), -(x["trim"]["position_value"] or 0)))
    return {"pre": pre, "blocked": blocked, "post": post}


# ── 이메일 ────────────────────────────────────────────────────────────────
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_email(rep: dict, today_str: str) -> tuple:
    act = [p for p in rep["pre"] if p["trim"]["code"] in ("trim", "trim_hard")]
    hold = [p for p in rep["pre"] if p["trim"]["code"] not in ("trim", "trim_hard")]
    n_act = len(act) + len(rep["blocked"])
    subj = f"📅 실적 레이더 {today_str} — 조치 {n_act}건"

    h = ["<div style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
         "max-width:640px;margin:0 auto;color:#1a1a1a'>",
         f"<h2 style='margin:0 0 4px'>📅 실적 레이더</h2>"
         f"<div style='color:#666;font-size:13px;margin-bottom:16px'>{today_str} · "
         f"D-{ec.SNAPSHOT_DAYS} 사전 경고 + 발표 후 결과</div>"]

    if not (act or rep["blocked"] or rep["post"] or hold):
        h.append("<p>오늘 조치가 필요한 실적 이벤트가 없습니다.</p></div>")
        return subj, "".join(h)

    for p in act:
        t, s = p["trim"], p["snap"]
        color = "#c0392b" if t["code"] == "trim_hard" else "#e67e22"
        mv = ec._num(s.get("Exp_Median_Pct"))
        wv = ec._num(s.get("Exp_Worst_Pct"))
        h.append(
            f"<div style='border-left:4px solid {color};background:#fdf6f3;padding:12px 14px;"
            f"margin:0 0 12px;border-radius:4px'>"
            f"<div style='font-size:17px;font-weight:700'>{_esc(t['label'])} — "
            f"{_esc(p['ticker'])}</div>"
            f"<div style='color:#555;font-size:13px;margin:2px 0 10px'>"
            f"{_esc(s.get('Earnings_Date'))}"
            f"({_esc(ec.DATE_SOURCE_LABELS.get(s.get('Date_Source'), ''))}) · "
            f"{_esc(ec.TIMING_LABELS.get(s.get('Timing'), ''))} · {_esc(p['account'])}</div>"
            f"<div style='font-size:15px'><b>약 ${t['sell_dollars']:,.0f} 매도</b> "
            f"— 비중 {t['position_pct']:.1f}% → {t['target_pct']:.1f}%</div>"
            f"<div style='color:#444;font-size:13px;margin-top:8px'>"
            f"<b>왜:</b> {_esc(t['reason'])}<br>"
            f"통상 ±{mv:.1f}%" + (f" · 최악 −{wv:.1f}%" if wv else "") +
            f" · 표본 {_esc(s.get('Sample_N'))}분기</div></div>")

    for b in rep["blocked"]:
        h.append(
            f"<div style='border-left:4px solid #7f8c8d;background:#f4f6f6;padding:12px 14px;"
            f"margin:0 0 12px;border-radius:4px'>"
            f"<div style='font-size:17px;font-weight:700'>⛔ {_esc(b['ticker'])} — 매수 보류</div>"
            f"<div style='color:#555;font-size:13px;margin:2px 0 8px'>"
            f"{_esc(b['snap'].get('Earnings_Date'))} · 워치리스트</div>"
            f"<div style='color:#444;font-size:13px'>{_esc(b['gate']['reason'])}<br>"
            f"발표 결과 확인 후 재평가합니다.</div></div>")

    if rep["post"]:
        h.append("<h3 style='margin:20px 0 8px;font-size:15px'>발표 완료</h3>")
        for r in rep["post"]:
            pead = r.get("_pead") or {}
            gp = ec._num(r.get("Gap_Pct")) or 0.0
            h.append(
                f"<div style='border:1px solid #e0e0e0;padding:10px 12px;margin:0 0 8px;"
                f"border-radius:4px'><b>{_esc(r.get('Ticker'))}</b> "
                f"갭 {gp:+.1f}% · {_esc(pead.get('label', ''))}"
                f"<div style='color:#666;font-size:12px;margin-top:4px'>"
                f"{_esc(' · '.join(pead.get('reasons', [])))}</div></div>")

    if hold:
        names = ", ".join(f"{p['ticker']}({p['trim']['label'].split()[-1]})" for p in hold)
        h.append(f"<div style='color:#666;font-size:12px;margin-top:14px'>"
                 f"조치 없음: {_esc(names)}</div>")

    h.append("<div style='color:#999;font-size:11px;margin-top:20px;border-top:1px solid #eee;"
             "padding-top:10px'>이 메일은 매수·매도 신호가 아니라 <b>이벤트 리스크 제약</b>입니다. "
             "매수는 레짐·타이밍·R:R 게이트가, 매도는 스윙/포지션 신호가 결정합니다.<br>"
             "본 정보는 참고용이며 투자 권유가 아닙니다.</div></div>")
    return subj, "".join(h)


def send_mail(to_addr, subject, html) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = str(to_addr or GMAIL_TO).strip()
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"[ERROR] 메일 발송 실패({to_addr}): {e}")
        return False


# ── main ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"실적 레이더 시작 — {datetime.now(_ET):%Y-%m-%d %H:%M ET}")
    print("=" * 60)
    if not is_market_open_today():
        print("[SKIP] 휴장일 — 종료")
        return

    today = pd.Timestamp(datetime.now(_ET).date())
    today_str = today.strftime("%Y-%m-%d")
    now_et = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")

    ews = _events_ws()
    rows = ec.parse_events(ews.get_all_values() or [])
    existing = {str(r.get("Event_ID") or ""): r for r in rows}
    print(f"[INFO] 기존 이벤트 {len(rows)}건")

    holdings, watch, tickers, wl_stops = load_universe()
    print(f"[INFO] 대상 티커 {len(tickers)}개 "
          f"(보유 {len(holdings)}명 · 워치 {len(watch)}명)")

    hist_cache = {}

    print("\n▶ 패스 2: 사후 측정")
    v_updates, results = pass_verify(rows, hist_cache, today)
    print("\n▶ 패스 3: D+5 지연")
    d_updates = pass_delayed(rows, hist_cache, today)
    print("\n▶ 패스 1: 사전 스냅샷")
    new_rows, snapshots = pass_snapshot(tickers, existing, hist_cache, today, now_et)

    # ── 시트 기록: 갱신 먼저, 추가는 마지막 (부분 실패 시 재시도 가능) ──
    idx = {c: i for i, c in enumerate(ec.EVENTS_COLS)}
    for row_i, patch in (v_updates + d_updates):
        try:
            cur = next((r for r in rows if r["_row"] == row_i), None)
            if cur is None:
                continue
            vals = [cur.get(c, "") for c in ec.EVENTS_COLS]
            for c, v in patch.items():
                vals[idx[c]] = v
            _safe_update(ews, [vals], row_i, ec.EVENTS_NCOL)
        except Exception as e:
            print(f"[ERROR] 행 {row_i} 갱신 실패: {e}")
    if new_rows:
        try:
            _safe_update(ews, new_rows, len(rows) + 2, ec.EVENTS_NCOL)
            print(f"[OK] 신규 스냅샷 {len(new_rows)}건 저장")
        except Exception as e:
            print(f"[ERROR] 스냅샷 저장 실패: {e}")

    # ── 메일 ──
    print("\n▶ 발송")
    try:
        uws = _ws(_USERS_WORKSHEET, uc.USER_SHEET_COLS)
        uc.ensure_users_header_v4(uws)
        rcpts = uc.get_recipients(uws, "earnings", admin_fallback_email=GMAIL_TO)
    except Exception as e:
        print(f"[WARN] 수신자 조회 실패 — 관리자에게만 발송: {e}")
        rcpts = [(uc.ADMIN_CONTENT_OWNER_ID, GMAIL_TO)]

    core_keys = load_core_keys()
    prof_cache, sent = {}, 0
    for uid, email in (rcpts or []):
        try:
            rep = build_user_report(uid, holdings, watch, snapshots, results,
                                    prof_cache, hist_cache, core_keys=core_keys,
                                    wl_stops=wl_stops)
            if not (rep["pre"] or rep["blocked"] or rep["post"]):
                print(f"  [SKIP] {uid} — 해당 이벤트 없음")
                continue
            subj, html = render_email(rep, today_str)
            to = None if str(uid).lower() == uc.ADMIN_CONTENT_OWNER_ID else email
            if send_mail(to or GMAIL_TO, subj, html):
                sent += 1
                print(f"  [SENT] {uid} → {to or GMAIL_TO}")
        except Exception as e:
            print(f"  [ERROR] {uid} 리포트 실패: {e}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"완료 — 스냅샷 {len(new_rows)} · 측정 {len(v_updates)} · "
          f"D+5 {len(d_updates)} · 메일 {sent}통")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
