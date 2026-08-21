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
import users_core as uc   # noqa: E402  — Users 시트/수신자 SSOT (app.py와 동일 모듈)
import accounts_core as ac  # noqa: E402  — 계좌 프로필/자본금 순수 로직 SSOT (app.py와 동일 모듈)
import earnings_core as ec  # noqa: E402  — 실적 이벤트 리스크 SSOT (app.py와 동일 모듈)
import watchlist_metrics_core as wm  # noqa: E402  — 워치리스트 표시 지표 SSOT (app.py와 동일 모듈)
import calendar_core as cc  # noqa: E402  — 시장 캘린더(휴장일) SSOT

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

# app.py _WATCHLIST_SHEET_COLS 와 동일 (13열) — lockstep
# ⚠️ Account 는 맨 뒤(M열). Alert_LastState(L열) 하드코딩 기록 위치를 깨지 않기 위함.
_WL_COLS = ["ID", "Ticker", "Memo", "Alert_Price", "Alert_RSI", "Alert_MA200",
            "Saved_Price", "Date_Added", "Stop_Loss", "Target_Price",
            "Alert_States", "Alert_LastState", "Account"]
_WL_NCOL = len(_WL_COLS)
_WL_COL_ACCOUNT = 12  # 0-index
_WL_ALERT_DEFAULT = "entry,risk,watch"
_COL_ALERT_STATES = 10      # 0-index
_COL_ALERT_LASTSTATE = 11   # 0-index → 시트 L열

# 보유(Portfolio) — app.py 와 lockstep
_PF_WORKSHEET = "Portfolios"                  # [ID, Account, Ticker, AvgPrice, Quantity, Date_Added]
_PFSTATE_WORKSHEET = "Portfolio_Alert_State"  # [Key, Alert_States, Alert_LastState, Updated_At]
_PFSTATE_COLS = ["Key", "Alert_States", "Alert_LastState", "Updated_At", "Stop_Loss", "Target_Price"]
_PF_ALERT_DEFAULT = "exit,risk"               # 보유 기본 알림 (손절은 exit의 ATR 트레일링에 포함)
_PF_INTRADAY_EVENTS = ("exit", "risk")        # 장중 보유 행동 가능 이벤트
# Portfolios 시트 열 인덱스(0-base). D=평단, F=Date_Added.
# Date_Added 는 포지션(중장기) 트레일링 스톱의 '보유 고점' 기준일 → 매도 판정에 직접 영향.
_PF_COL_AVG = 3
_PF_COL_DATE_ADDED = 5

# 진입 시점 baseline(2A) 재구성용 — Date_Added 이전 200봉이 있어야 MA200 이 나온다.
_PF_HIST_LIMIT = 600

# ── 실적 진입 차단 게이트 ─────────────────────────────────────────────────────
# Earnings_Events 시트를 **공유 상태**로 읽는다(스크립트 실행 순서에 의존하지 않음).
# 행이 없으면 차단하지 않는다(fail-open) — 실적 워치가 실패했다고 매수 알림을
# 통째로 막아버리는 쪽이 더 나쁘다.
def load_earnings_blocks() -> dict:
    """{TICKER: 사유}. 조회 실패 시 빈 dict."""
    try:
        _, ws = _open_ws(ec.CALENDAR_WORKSHEET)
        cal = ec.parse_calendar(ws.get_all_values() or [])
        blocks = ec.blocked_from_calendar(cal, today=datetime.now(_ET).date())
        if blocks:
            print(f"[GATE] 실적 진입 차단 후보 {len(blocks)}종목: {', '.join(sorted(blocks))}")
        return blocks
    except Exception as e:
        print(f"[INFO] 실적 게이트 조회 생략(fail-open): {e}")
        return {}


def load_market_gate_users() -> set:
    """시장 진입 게이트를 적용할 사용자 ID 집합 (Users.Gate_Market = Y).

    ⚠️ 이 게이트는 매수 알림을 **실제로 막는다**(백테스트상 진입의 약 20%).
       토글이 꺼진 사용자에게는 적용하지 않는다 — 관리자 포함. 기본값이 "N" 이므로
       시트에서 명시적으로 켜기 전까지 기존 동작이 완전히 유지된다.
       조회 실패 시 빈 집합 = 차단 없음(fail-open).
    """
    try:
        gc = get_gspread_client()
        ws = gc.open(_SPREADSHEET_TITLE).worksheet("Users")
        uc.ensure_users_header_v4(ws)
        uids = {str(u).strip().lower()
                for u, _e in (uc.get_recipients(ws, "gate_market",
                                                admin_fallback_email=None) or [])}
        print(f"[GATE] 시장 게이트 적용 대상 {len(uids)}명"
              + (f": {', '.join(sorted(uids))}" if uids else " — 전원 미사용"))
        return uids
    except Exception as e:
        print(f"[INFO] 시장 게이트 대상 조회 실패 — 차단 미적용: {e}")
        return set()


def load_earnings_gate_users() -> set:
    """실적 게이트를 적용할 사용자 ID 집합 (Users.Alert_Earnings = Y).

    ⚠️ 게이트는 **매수 알림을 실제로 막는** 동작이다. 실적 레이더를 아직 신뢰하지
       않는 사용자의 기존 알림 흐름까지 바꾸면 안 되므로, 토글이 꺼진 사용자에게는
       차단을 적용하지 않는다(관리자 포함). 조회 실패 시 빈 집합 = 차단 없음(fail-open).
    """
    try:
        gc = get_gspread_client()
        ws = gc.open(_SPREADSHEET_TITLE).worksheet("Users")
        uc.ensure_users_header_v4(ws)
        uids = {str(u).strip().lower()
                for u, _e in (uc.get_recipients(ws, "earnings",
                                                admin_fallback_email=GMAIL_TO) or [])}
        print(f"[GATE] 실적 게이트 적용 대상 {len(uids)}명"
              + (f": {', '.join(sorted(uids))}" if uids else " — 전원 미사용"))
        return uids
    except Exception as e:
        print(f"[INFO] 실적 게이트 대상 조회 실패 — 차단 미적용: {e}")
        return set()
_DEEP_KEY = "__deep__"      # 티커와 충돌 불가한 센티넬 키


def _pf_hist(tk: str, hist_cache: dict):
    """보유 종목용 깊은 히스토리. 얕게 캐시된 프레임이 있으면 1회만 덮어쓴다.

    ⚠️ 깊은 조회 여부는 hist_cache 안에 기록한다. 모듈 전역에 두면 캐시가 새로
       만들어진 뒤 재조회를 건너뛰어 None 이 반환된다(평가 전체가 조용히 스킵됨).
    """
    deep = hist_cache.setdefault(_DEEP_KEY, set())
    if tk not in deep:
        deep.add(tk)
        h = _fmp_price_history(tk, limit=_PF_HIST_LIMIT)
        if h is not None and not h.empty:
            hist_cache[tk] = h
    return hist_cache.get(tk)


# NYSE 휴장일 (run_narrative.py 와 동일 목록)
# ── 휴장일 판정 — calendar_core SSOT 로 단일화 ────────────────────────────────
# 기존 하드코딩 집합은 2026-12-25 에서 끝나 2027-01-01 부터 모든 휴장일을
# 거래일로 오판했다(같은 상수가 5개 자동화 파일에 중복돼 있었다).
#
# calendar_core 는 **규칙 계산**이라 FMP·시트 접근이 없다. 이 가드는 시트를
# 열기 전 main() 최상단에 있으므로, 네트워크나 시트 왕복을 붙이면 "휴장일이라
# 즉시 종료"하는 실행에까지 비용이 생긴다. 호출 비용은 기존과 같은 0 이다.
def is_market_open_today() -> bool:
    return cc.is_market_open_today()


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
def send_email(subject: str, html_body: str, to_addr: str | None = None) -> bool:
    """단일 수신자 발송. to_addr 미지정 시 GMAIL_TO(관리자)."""
    _to = str(to_addr or GMAIL_TO).strip()
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = _to
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, _to, msg.as_string())
        print(f"[OK] 이메일 발송 완료 → {_to}")
        return True
    except Exception as e:
        print(f"[ERROR] 이메일 발송 실패({_to}): {e}")
        return False


_ACCOUNT_PROFILE_CACHE = {"loaded": False, "values": []}


def _load_account_profile_values():
    """Account_Profile 시트 전체값 로드(1회 캐시). 실패 시 빈 리스트(기본값 동작)."""
    if _ACCOUNT_PROFILE_CACHE["loaded"]:
        return _ACCOUNT_PROFILE_CACHE["values"]
    vals = []
    try:
        gc = get_gspread_client()
        ws = gc.open(_SPREADSHEET_TITLE).worksheet(ac.WORKSHEET_TITLE)
        vals = ws.get_all_values() or []
    except Exception as e:
        print(f"[INFO] Account_Profile 시트 없음/실패 — 기본값으로 사이징: {e}")
    _ACCOUNT_PROFILE_CACHE["loaded"] = True
    _ACCOUNT_PROFILE_CACHE["values"] = vals
    return vals


def _account_context_for(uid: str, account: str, hist_cache: dict) -> dict:
    """유저·계좌의 자본금 컨텍스트 + 프로필. Portfolios 에서 보유를 모아 자본금 산출.

    자본금 시세는 hist_cache 의 마지막 종가를 재사용(추가 FMP 호출 없음).
    반환: {profile, equity, invested_value, cash, slots_used, ok}
    """
    profs = ac.parse_profiles(_load_account_profile_values(), uid)
    prof = ac.get_profile(profs, account)
    ctx = {"profile": prof, "equity": float(prof.get("Cash", 0.0) or 0.0),
           "invested_value": 0.0, "cash": float(prof.get("Cash", 0.0) or 0.0),
           "slots_used": 0, "ok": bool(str(account or "").strip())}
    acct = str(account or "").strip()
    if not acct:
        return ctx
    try:
        _sh, pf_ws = _open_ws(_PF_WORKSHEET)
        pf_vals = pf_ws.get_all_values() or []
    except Exception:
        pf_vals = []
    holdings = []
    price_map = {}
    for r in pf_vals[1:] if len(pf_vals) > 1 else []:
        r = (list(r) + [""] * 6)[:6]
        if str(r[0]).strip().upper() != str(uid).strip().upper():
            continue
        if str(r[1]).strip().lower() != acct.lower():
            continue
        tk = str(r[2]).strip().upper()
        if not tk:
            continue
        avg = pd.to_numeric(r[3], errors="coerce")
        qty = pd.to_numeric(r[4], errors="coerce")
        holdings.append((tk, qty, float(avg) if pd.notna(avg) else None))
        # 자본금용 현재가: hist_cache 종가 재사용
        try:
            h = hist_cache.get(tk)
            if h is not None and not h.empty:
                price_map[tk] = float(pd.to_numeric(h["Close"], errors="coerce").dropna().iloc[-1])
        except Exception:
            pass
    eqinfo = ac.compute_equity(holdings, price_map, prof.get("Cash", 0.0))
    ctx.update({"equity": eqinfo["equity"], "invested_value": eqinfo["invested_value"],
                "cash": eqinfo["cash"], "slots_used": eqinfo["slots_used"]})
    return ctx


def _radar_recipients() -> list[tuple[str, str]]:
    """Users 시트에서 매매 레이더 수신자 [(uid, email)] 조회. 실패 시 빈 목록(관리자 발송은 영향 없음)."""
    try:
        gc = get_gspread_client()
        ws = gc.open(_SPREADSHEET_TITLE).worksheet("Users")
        uc.ensure_users_header_v3(ws)   # 자동화가 앱보다 먼저 돌 수 있으므로 방어
        _rcpts = uc.get_recipients(ws, "radar", admin_fallback_email=GMAIL_TO)
        if _rcpts:
            print(f"[RADAR] 수신자 {len(_rcpts)}명: "
                  + ", ".join(str(u).strip() for u, _ in _rcpts))
        else:
            print("[WARN] 레이더 수신자 0명 — Users 시트 확인 필요 "
                  "(Status=approved / Alert_Radar=Y / Email 유효 3가지 모두 충족해야 함).")
        return _rcpts
    except Exception as e:
        print(f"[WARN] Users 수신자 조회 실패 — 관리자 메일만 발송: {e}")
        return []


# Gmail 검색용 고정 문구. 나중에 "시장 경고 구간"으로 검색하면 해당 날짜의 메일이
# 전부 나오고, 그날 온 매수 신호가 이후 어떻게 됐는지 되짚을 수 있다.
MARKET_GATE_MAIL_TAG = "시장 경고 구간"


def build_market_gate_banner(gate: dict | None, applied_users=None) -> str:
    """5pm 메일 상단 관찰 배너.

    경고가 임계 미만이면 **아무것도 붙이지 않는다** — 매일 뜨면 잡음이 되고,
    관찰 목적상 필요한 건 '경고 구간이었던 날'의 기록뿐이다.

    게이트가 실제로 적용 중인지(Users.Gate_Market)에 따라 문구가 갈린다:
      · 미적용(관찰 모드) — 아래 매수 신호는 정상 발송된 것이고, 배너는
        "게이트가 켜져 있었다면 막혔을 것"이라는 반사실 기록이다.
      · 적용 중 — 매수 신호가 실제로 동결됐다는 안내.
    """
    if not gate or not gate.get("blocked"):
        return ""
    cnt = gate.get("count")
    cnt_s = f"{cnt:.0f}" if isinstance(cnt, (int, float)) else "?"
    thr = gate.get("threshold", rc.MARKET_GATE_THRESHOLD)
    names = ", ".join(getattr(rc, "MARKET_WARNING_LABELS", ()) or ())
    live = bool(applied_users)
    if live:
        head = (f"🚦 <b>{MARKET_GATE_MAIL_TAG}</b> — 시장 경고 {cnt_s}/"
                f"{rc.MARKET_WARNING_MAX} (임계 {thr})")
        body = ("신규 <b>매수</b> 알림이 동결됐습니다. 보유 관리 알림(줄이기·청산)은 "
                "정상 발송됩니다. 동결은 상태를 보존하므로 게이트가 풀리면 "
                "멈춘 지점에서 재개됩니다.")
        bg, fg = "#fdecea", "#a4342a"
    else:
        head = (f"🚦 <b>{MARKET_GATE_MAIL_TAG}</b> — 시장 경고 {cnt_s}/"
                f"{rc.MARKET_WARNING_MAX} (임계 {thr}) · 관찰 모드")
        body = ("게이트가 켜져 있었다면 아래 <b>매수</b> 신호는 발송되지 "
                "않았을 구간입니다. 지금은 <code>Users.Gate_Market = N</code> 이라 "
                "평소대로 발송됩니다 — 이 종목들이 이후 어떻게 되는지 보고 "
                "게이트를 켤지 판단하세요.")
        bg, fg = "#fff8e1", "#8a6d00"
    return (f"<p style='background:{bg};padding:10px;border-radius:6px;color:{fg};"
            f"line-height:1.5'>{head}<br>{body}"
            f"<br><span style='font-size:12px;opacity:.8'>판정 신호: {names}</span></p>")


# ══════════════════════════════════════════════════════════════════════════
# 데이터 미수신 추적 (A-2a)
# ══════════════════════════════════════════════════════════════════════════
# 왜 필요한가
# ───────────
# 가격 이력이 빈 배열로 오면 평가 루프가 `continue` 로 **조용히 건너뛴다.**
# 티커 변경·상장폐지가 대표적 원인이고, 그 결과는 심각도가 다르다.
#
#   워치리스트에서 발생 → 매수 기회 상실
#   보유에서 발생       → **매도 신호가 영구히 오지 않는다** (손실방지 직격)
#
# 지금까지는 로그에도 남지 않았다. 최소한 사용자에게 알리는 것이 이 블록이다.
# FMP 추가 호출은 0 이다 — 이미 실패한 조회의 결과를 주워 담을 뿐이다.
#
# 왜 '연속 N일' 이 아니라 '비율' 인가
# ──────────────────────────────────
# 일시적 API 장애로 여러 티커가 한꺼번에 빌 수 있다. 그걸 종목별 경고로 쏟으면
# 메일 폭탄이 된다. '연속 N일' 조건은 판별력이 좋지만 **상태 저장이 필요해서
# 시트 쓰기가 생긴다.**
#
# 비율은 상태 없이 같은 판별을 한다 — 소수가 비면 종목 문제, 다수가 비면 API
# 문제다. 시트 쓰기 0, 추가 호출 0.
_NODATA_RATIO_ALERT = 0.30   # 이 비율을 넘으면 종목 문제가 아니라 API 장애로 본다
_NODATA_MIN_SAMPLE = 5       # 표본이 이보다 적으면 비율이 의미 없다 → 개별 보고

_nodata = {"by_user": {}, "attempted": 0, "missing": 0}


def reset_nodata() -> None:
    """평가 시작 전 초기화. main() 에서 한 번 호출한다."""
    _nodata["by_user"] = {}
    _nodata["attempted"] = 0
    _nodata["missing"] = 0


def record_attempt() -> None:
    """이력 조회를 시도한 종목 1건. 비율의 분모가 된다."""
    _nodata["attempted"] += 1


def record_nodata(uid: str, ticker: str, where: str) -> None:
    """이력이 비어 건너뛴 종목 기록.

    같은 사용자가 같은 티커를 워치리스트와 보유 양쪽에 갖고 있으면 두 번
    불린다. 티커 기준으로 합치되 발생 위치는 둘 다 남긴다 — 보유 쪽에서
    발생했다는 사실이 심각도를 결정하므로 지워선 안 된다.
    """
    u = str(uid or "").strip()
    tk = str(ticker or "").strip().upper()
    if not u or not tk:
        return
    _nodata["missing"] += 1
    slot = _nodata["by_user"].setdefault(u, {})
    if tk in slot:
        if where not in slot[tk]:
            slot[tk].append(where)
    else:
        slot[tk] = [where]


def nodata_is_systemic() -> bool:
    """미수신이 광범위한가 — 종목 문제가 아니라 API 장애로 볼 것인가."""
    n = _nodata["attempted"]
    if n < _NODATA_MIN_SAMPLE:
        return False
    return (_nodata["missing"] / float(n)) > _NODATA_RATIO_ALERT


def nodata_for_user(uid: str) -> list:
    """해당 사용자의 미수신 목록 → [(티커, "워치리스트/보유"), ...] 정렬본."""
    slot = _nodata["by_user"].get(str(uid or "").strip()) or {}
    return [(tk, "·".join(slot[tk])) for tk in sorted(slot)]


def nodata_total() -> int:
    return sum(len(v) for v in _nodata["by_user"].values())


def nodata_weight(uid: str) -> int:
    """제목 건수에 더할 가중치.

    광범위 장애일 때는 티커 수만큼 세면 제목이 부풀어 실제 매매 신호가 묻힌다.
    그때는 '1건'으로만 센다.
    """
    n = len(nodata_for_user(uid))
    if not n:
        return 0
    return 1 if nodata_is_systemic() else n


def nodata_log_summary() -> None:
    n_att, n_mis = _nodata["attempted"], _nodata["missing"]
    if not n_mis:
        print(f"[DATA-OK] 이력 미수신 0건 (조회 {n_att}건)")
        return
    pct = (100.0 * n_mis / n_att) if n_att else 0.0
    tag = "DATA-SYSTEMIC" if nodata_is_systemic() else "DATA-MISSING"
    print(f"[{tag}] 이력 미수신 {n_mis}/{n_att}건 ({pct:.0f}%)")
    for u, slot in sorted(_nodata["by_user"].items()):
        for tk in sorted(slot):
            print(f"    {u}/{tk}: {'·'.join(slot[tk])}")


def render_nodata_html(uid: str) -> str:
    """미수신 섹션 HTML. 없으면 빈 문자열."""
    items = nodata_for_user(uid)
    if not items:
        return ""
    if nodata_is_systemic():
        return (
            "<h2 style='color:#b45309;border-bottom:2px solid #b45309;"
            "padding-bottom:6px;margin-top:24px'>⚠️ 데이터 미수신 (광범위)</h2>"
            "<div style='margin:10px 0;padding:12px;border:1px solid #fde68a;"
            "border-radius:8px;background:#fffbeb'>"
            f"<div style='font-weight:700'>{len(items)}개 종목의 가격 이력을 받지 못했습니다.</div>"
            "<div style='color:#555;font-size:14px;margin-top:6px'>"
            "미수신 비율이 높아 <b>개별 종목 문제가 아니라 데이터 공급 장애</b>로 "
            "판단했습니다. 해당 종목들은 오늘 평가에서 제외됐습니다 — "
            "매수·매도 신호가 나오지 않은 것이 '신호 없음'을 뜻하지 않습니다. "
            "내일도 같은 경고가 반복되면 확인이 필요합니다.</div></div>"
        )
    rows = "".join(
        f"<li style='margin:4px 0'><b>{tk}</b> "
        f"<span style='color:#666;font-size:13px'>· {where}</span></li>"
        for tk, where in items
    )
    return (
        "<h2 style='color:#b45309;border-bottom:2px solid #b45309;"
        "padding-bottom:6px;margin-top:24px'>⚠️ 데이터 미수신</h2>"
        "<div style='margin:10px 0;padding:12px;border:1px solid #fde68a;"
        "border-radius:8px;background:#fffbeb'>"
        f"<ul style='margin:0;padding-left:20px'>{rows}</ul>"
        "<div style='color:#555;font-size:14px;margin-top:10px'>"
        "위 종목은 가격 이력이 비어 <b>오늘 평가에서 제외</b>됐습니다. "
        "<b>보유</b> 종목이라면 매도 신호가 나오지 않습니다. "
        "티커 변경·상장폐지가 흔한 원인이니 확인하세요.</div></div>"
    )


def dispatch_radar_emails(wl_res: dict, pf_res: dict, today: str,
                          subject_emoji: str, subject_label: str,
                          banner_html: str = "") -> None:
    """레이더 이메일 발송 오케스트레이션 — 완전 유저 격리.

    각 유저는 '본인 섹션만' 담은 개별 메일을 받는다. 유저 간 데이터는 절대 섞이지 않는다.
      - 관리자(yab): 본인 데이터만 → GMAIL_TO. Users 시트의 radar 토글/이메일 상태와
        무관하게 항상 발송(관리자가 자기 알림을 놓치지 않도록 하는 안전장치).
      - 나머지 유저: 본인 데이터만 → Users 시트의 본인 이메일. 실패는 유저 단위
        격리(다음 유저 진행). 관리자 uid/이메일과 겹치면 위에서 이미 보냈으므로 건너뜀.
    """
    wl_res = wl_res or {}
    pf_res = pf_res or {}
    total = (sum(len(v) for v in wl_res.values())
             + sum(len(v) for v in pf_res.values()))
    if not total:
        print("[INFO] 발동/활성 건 없음 — 이메일 생략.")
        return

    # 데이터 키(uid)를 소문자로 인덱싱 — Users 시트 ID 와 Watchlist/Portfolio 시트 uid 의
    # 대소문자가 달라도 매칭되도록(대소문자 불일치로 인한 조용한 미발송 방지).
    _wl_key = {str(k).strip().lower(): k for k in wl_res}
    _pf_key = {str(k).strip().lower(): k for k in pf_res}

    # 발동 건이 실제로 존재하는 uid 집합(소문자 기준) — 배달 누락 진단용
    _fired = {str(k).strip().lower() for k, v in wl_res.items() if v}
    _fired |= {str(k).strip().lower() for k, v in pf_res.items() if v}
    # 미수신만 있는 사용자도 메일 대상이다. 배달 누락 진단에 포함시키지 않으면
    # "미수신 경고가 나갔어야 하는데 안 나갔다"를 아무도 알아채지 못한다.
    _fired |= {str(k).strip().lower() for k in _nodata["by_user"] if nodata_for_user(k)}
    _delivered: set[str] = set()

    def _send_one_user(uid_s: str, to_addr: str | None) -> None:
        """단일 유저의 본인 섹션만 담아 발송. 발동 0건이면 생략."""
        _u_l = str(uid_s).strip().lower()
        _k_wl = _wl_key.get(_u_l)
        _k_pf = _pf_key.get(_u_l)
        _wl_u = {uid_s: wl_res[_k_wl]} if (_k_wl and wl_res.get(_k_wl)) else {}
        _pf_u = {uid_s: pf_res[_k_pf]} if (_k_pf and pf_res.get(_k_pf)) else {}
        # 데이터 미수신은 **그 자체가 알림**이다. 발동 0건이어도 보유 종목이
        # 침묵 중이라면 반드시 알려야 하므로 건수에 합산한다. 합산하지 않으면
        # 침묵을 알리는 메일이 같은 이유로 침묵한다.
        _nd_html = render_nodata_html(uid_s)
        _n_u = (sum(len(v) for v in _wl_u.values())
                + sum(len(v) for v in _pf_u.values())
                + nodata_weight(uid_s))
        if not _n_u:
            print(f"[RADAR] '{uid_s}' 발동 0건 — 발송 생략.")
            return
        _ok = send_email(f"{subject_emoji} {subject_label} — {_n_u}건 ({today} ET)",
                         banner_html + build_email_html(_wl_u, _pf_u, today,
                                                        nodata_html=_nd_html),
                         to_addr=to_addr)
        if _ok:
            _delivered.add(_u_l)
            print(f"[RADAR] '{uid_s}' {_n_u}건 발송 완료 → {to_addr or GMAIL_TO}")
        else:
            print(f"[WARN] '{uid_s}' {_n_u}건 발송 실패 → {to_addr or GMAIL_TO}")

    _admin_uid = str(uc.ADMIN_CONTENT_OWNER_ID).strip()
    _admin_uid_u = _admin_uid.upper()
    _admin_email_l = str(GMAIL_TO).strip().lower()

    print(f"[RADAR] 발동 uid: {sorted(_fired) or '없음'} (총 {total}건)")

    # 수신자 전원(관리자 포함)에게 **본인 데이터만** 발송.
    # ⚠️ 예전에는 관리자를 무조건 먼저 보내서 Alert_Radar 토글로 끌 수 없었다.
    #    이제 yab 행도 다른 사용자와 동일하게 Users 시트 토글을 따른다.
    _rcpt_uids = set()
    for _uid, _email in _radar_recipients():
        try:
            _uid_s = str(_uid).strip()
            _rcpt_uids.add(_uid_s.lower())
            # 관리자는 GMAIL_TO 기본값 경로를 유지(to_addr=None)해 기존 동작과 동일하게
            _to = None if _uid_s.upper() == _admin_uid_u else _email
            _send_one_user(_uid_s, _to)
        except Exception as e:
            print(f"[WARN] {_uid} 개별 발송 실패(다음 유저 진행): {e}")

    # 3) 배달 누락 진단 — 발동 건은 있는데 메일이 나가지 않은 uid 명시
    for _u in sorted(_fired - _delivered - {_admin_uid.lower()}):
        if _u not in _rcpt_uids:
            print(f"[WARN] '{_u}' 발동 건이 있으나 수신자 목록에 없음 — "
                  "Users 시트 확인(Status=approved / Alert_Radar=Y / Email 유효).")
        else:
            print(f"[WARN] '{_u}' 수신자이나 메일 미발송 — 발송 실패 또는 데이터 키 불일치 확인.")


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
        _plan = ev.get("plan")
        if isinstance(_plan, dict):
            html += _render_sizing_line(_plan, ev.get("account"))
    if h.get("swing") or h.get("position"):
        def _vcolor(_l):
            if "SELL" in _l or "청산" in _l:
                return "#d62728"
            if "익절" in _l or "줄이기" in _l:
                return "#f59e0b"
            if "HOLD" in _l or "보유" in _l:
                return "#16a34a"
            return "#555"
        html += "<div style='margin-top:8px;padding-top:6px;border-top:1px dashed #e1e4e8'>"
        sw, po = h.get("swing"), h.get("position")
        if sw:
            html += (f"<div style='margin-top:2px'>📈 <b>스윙(단기)</b>: "
                     f"<span style='color:{_vcolor(sw[0])};font-weight:700'>{sw[0]}</span>"
                     + (f" — {sw[1]}" if sw[1] else "") + "</div>")
        if po:
            html += (f"<div style='margin-top:2px'>🛡 <b>포지션(중장기)</b>: "
                     f"<span style='color:{_vcolor(po[0])};font-weight:700'>{po[0]}</span>"
                     + (f" — {po[1]}" if po[1] else "") + "</div>")
        html += "</div>"
    return html + "</div>"


def _render_sizing_line(plan: dict, account=None) -> str:
    """entry 알림용 사이징 요약(금액 우선 + 제한 사유 + 차단). c안."""
    _acct = str(account or "").strip()
    _acct_tag = (f"<span style='color:#888'>🏦 {_acct}</span> · " if _acct else "")
    mode = str(plan.get("sizing_mode", "risk_based"))
    if mode == "off":
        return ("<div style='margin-top:4px;font-size:13px;color:#666'>"
                f"{_acct_tag}💰 <i>사이징 미사용 계좌</i></div>")
    dollars = float(plan.get("dollars", 0.0) or 0.0)
    if dollars <= 0 and not plan.get("blocked"):
        return ""   # 자본금 미상(계좌 미지정) → 금액 생략, 게이트/R:R 은 위에 이미 표시됨
    _sh_ex = float(plan.get("shares_exact", 0.0) or 0.0)
    _whole = int(plan.get("shares_whole", 0) or 0)
    _pos = float(plan.get("position_pct", 0.0) or 0.0)
    _bind = str(plan.get("binding_label", "-"))
    _1r = float(plan.get("risk_dollars", 0.0) or 0.0)
    line = (f"<div style='margin-top:4px;font-size:13px;color:#24292e'>"
            f"{_acct_tag}💰 <b>${dollars:,.2f}</b> "
            f"<span style='color:#888'>({_sh_ex:.3f}주 · 정수 {_whole}주 · "
            f"비중 {_pos:.1f}% · 제한 {_bind})</span>"
            f" · 1R -${_1r:,.2f}</div>")
    if plan.get("blocked"):
        line += ("<div style='margin-top:2px;font-size:13px;color:#b08800'>"
                 f"🚧 {plan.get('block_reason','')}</div>")
    if not plan.get("rr_measured", True):
        line += ("<div style='margin-top:2px;font-size:12px;color:#999'>"
                 "⚠️ R:R 미실측(독립 목표 없음) — 자금 빠듯하면 후순위</div>")
    return line


def build_email_html(wl_by_user: dict, pf_by_user: dict, today: str,
                     nodata_html: str = "") -> str:
    """매매 레이더 이메일 — 🔭 Watchlist(매수)와 💼 Portfolio(보유·매도) 섹션 분리,
    Portfolio는 account별로 그룹핑.

    nodata_html: 데이터 미수신 섹션(render_nodata_html 결과). 기본값이 빈
      문자열이라 기존 호출부(인자 3개)는 그대로 동작한다.
    """
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
            _wl_by_acct = {}
            for h in hits:
                _wl_by_acct.setdefault(str(h.get("account") or "미지정"), []).append(h)
            for acct, ah in _wl_by_acct.items():
                if len(_wl_by_acct) > 1 or acct != "미지정":
                    parts.append(f"<h4 style='margin:14px 0 4px;color:#444'>🏦 {acct}</h4>")
                for h in ah:
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

    # ⚠️ 데이터 미수신 — 매매 섹션 뒤, 푸터 앞.
    #    맨 위가 아닌 이유: 실제 매매 신호가 우선이고, 미수신은 '평가되지 않은 것'
    #    이라 신호와 성격이 다르다. 다만 발동 0건일 때는 이게 유일한 본문이 된다.
    if nodata_html:
        parts.append(nodata_html)

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


def _open_metrics_ws(sh):
    """Watchlist_Metrics 탭 핸들. 없으면 헤더까지 만들어 돌려준다."""
    titles = [w.title for w in sh.worksheets()]
    if wm.SHEET_TITLE in titles:
        return sh.worksheet(wm.SHEET_TITLE)
    ws = sh.add_worksheet(title=wm.SHEET_TITLE, rows=2000, cols=wm.NCOL)
    _lc = chr(ord("A") + wm.NCOL - 1)
    ws.update([wm.COLS], range_name=f"A1:{_lc}1", value_input_option="USER_ENTERED")
    print(f"[OK] {wm.SHEET_TITLE} 시트 생성")
    return ws


def persist_watchlist_metrics(spy_close, hist_cache, today, completed_only=False):
    """전 사용자 워치리스트 티커의 표시용 지표를 미리 계산해 시트에 적어둔다.

    [왜]
      앱의 워치리스트 탭은 종목마다 rc.analyze_ticker 를 돌린다(55종목 = 리런마다 10초).
      이 값들은 일봉 파생이라 장중에 거의 안 움직인다. 자동화가 하루치를 미리 계산해
      두면 앱은 읽기만 하면 된다. 계산식은 watchlist_metrics_core(SSOT)에 있고
      app.py 도 같은 모듈로 폴백 계산한다 — 저장본과 실시간 계산이 구조적으로 같다.

    [티커 단위 공용 데이터]
      NVDA 의 RSI 는 모든 사용자에게 같다. 전 사용자 워치리스트의 **합집합**을
      한 번만 계산한다. 개인 데이터는 이 시트에 들어가지 않는다.

    completed_only:
      True  → 오늘(ET) 봉을 제외한 확정 봉까지만 사용(백필 전용).
              백필은 아무 때나 돌 수 있어야 하므로 실행 시각과 무관하게 결정적이어야 한다.
      False → 정기 EOD(마감 후). 당일 봉이 이미 확정이라 자르면 하루 낡은 값이 된다.

    ⚠️ 장중(mode=intraday)에는 호출하지 않는다. 미완성 봉 기준값이 저장돼
       다음 EOD 까지 앱 전체에 퍼진다.
    """
    try:
        sh, ws_wl = _open_ws(_WATCHLIST_WORKSHEET)
    except Exception as e:
        print(f"[INFO] Watchlist 시트 없음/실패 — 지표 계산 스킵: {e}")
        return 0

    vals = ws_wl.get_all_values() or []
    if len(vals) < 2:
        print("[INFO] Watchlist 비어 있음 — 지표 계산 스킵.")
        return 0

    tickers = []
    for r in vals[1:]:
        r = (list(r) + [""] * _WL_NCOL)[:_WL_NCOL]
        tk = str(r[1]).strip().upper()
        if tk and tk not in tickers:
            tickers.append(tk)
    if not tickers:
        print("[INFO] 워치리스트 티커 없음 — 지표 계산 스킵.")
        return 0

    # 백필은 SPY 도 같은 기준으로 잘라야 RS 가 실행 시각에 따라 흔들리지 않는다.
    _spy = spy_close
    if completed_only and spy_close is not None:
        try:
            _spy = wm.completed_bars_only(spy_close.to_frame("Close"), today)["Close"]
        except Exception:
            _spy = spy_close

    _now = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
    rows, n_ok = [], 0
    for tk in tickers:
        try:
            if tk not in hist_cache:
                hist_cache[tk] = _fmp_price_history(tk)
            hist = hist_cache[tk]
            if hist is None or hist.empty:
                continue
            h = wm.completed_bars_only(hist, today) if completed_only else hist
            m = wm.compute_metrics(tk, h, spy_close=_spy, updated_at=_now)
            if not m:
                continue
            rows.append(wm.to_row(m))
            n_ok += 1
        except Exception as e:
            print(f"  [WARN] {tk} 지표 계산 실패: {e}")

    if not rows:
        print("[WARN] 계산된 지표 0건 — 시트를 건드리지 않는다(기존 값 보존).")
        return 0

    try:
        ws = _open_metrics_ws(sh)
        prev_rows = len(ws.get_all_values() or [])
        body = [list(wm.COLS)] + rows
        # 종목이 줄었을 때 남는 옛 행을 빈칸으로 덮는다(삭제 API 대신 1콜로 해결).
        for _ in range(max(0, prev_rows - len(body))):
            body.append([""] * wm.NCOL)
        if ws.row_count < len(body):
            ws.resize(rows=len(body) + 100, cols=max(ws.col_count, wm.NCOL))
        _lc = chr(ord("A") + wm.NCOL - 1)
        ws.update(body, range_name=f"A1:{_lc}{len(body)}", value_input_option="RAW")
        _basis = "확정 봉" if completed_only else "당일 봉 포함"
        print(f"[OK] {wm.SHEET_TITLE} 저장: {n_ok}/{len(tickers)}종목 ({_basis})")
    except Exception as e:
        print(f"[ERROR] {wm.SHEET_TITLE} 저장 실패: {e}")
        return 0
    return n_ok


def _merge_fired(a, b):
    out = {k: list(v) for k, v in (a or {}).items()}
    for k, v in (b or {}).items():
        out.setdefault(k, []).extend(v)
    return out


def _sizing_kwargs_for(uid: str, account: str, hist_cache: dict) -> dict:
    """계좌 컨텍스트 → build_watchlist_plan 사이징 kwargs.

    d안: 계좌 미지정/프로필 없으면 사이징 생략(equity=0 → 금액 0, 게이트/R:R만).
    off 모드도 마찬가지로 금액이 0으로 나오되 mode 로 이메일 표시가 갈린다.
    """
    acct = str(account or "").strip()
    if not acct:
        return {}   # 계좌 미지정 → 사이징 생략(자본금 미상)
    try:
        ctx = _account_context_for(uid, acct, hist_cache)
    except Exception:
        return {}
    if not ctx.get("ok") or float(ctx.get("equity", 0.0)) <= 0:
        return {}
    p = ctx["profile"]
    return {
        "equity": float(ctx["equity"]),
        "risk_pct": float(p["Risk_Pct"]),
        "max_position_pct": float(p["Max_Position_Pct"]),
        "cash": (float(ctx["cash"]) if float(ctx["cash"]) > 0 else None),
        "reserve_pct": float(p["Cash_Reserve_Pct"]),
        "invested_value": float(ctx["invested_value"]),
        "slots_used": int(ctx["slots_used"]),
        "max_positions": int(p["Max_Positions"]),
        "min_trade_dollars": float(p["Min_Trade_Dollars"]),
        "sizing_mode": str(p["Sizing_Mode"]),
    }


def eval_watchlist_eod(spy_close, hist_cache, today, earn_blocks=None, earn_users=None,
                       mkt_gate=None, mkt_users=None):
    """워치리스트: 상태 전환 2일 확정 + Alert_LastState(L열) 저장. → fired_by_user

    mkt_gate: rc.market_gate_status() 결과. blocked=True 면 mkt_users 에 속한
      사용자의 **모든 종목** entry 이벤트를 동결한다(시장 전체 조건이므로 종목 무관).
      실적 동결과 OR 로 결합된다 — 둘 중 하나라도 참이면 동결.
    mkt_users: 시장 게이트를 적용할 uid 집합 (Users.Gate_Market = Y).

    earn_blocks: {TICKER: 사유} — 실적 임박 종목의 entry 이벤트를 **동결**한다.
      동결은 상태를 손대지 않으므로, 차단이 풀리면 멈춘 지점에서 그대로 재개된다.
      (이메일 층 드롭 → 영구 침묵 / cond=False 억제 → entry_invalid 오발송)
    """
    fired_by_user = {}
    _blocks = earn_blocks or {}
    _mkt = mkt_gate or {}
    _mkt_users = {str(u).strip().lower() for u in (mkt_users or set())}
    _mkt_blocked = bool(_mkt.get("blocked"))
    _earn_users = earn_users or set()
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
        acct = str(r[_WL_COL_ACCOUNT]).strip()
        prev_state = str(r[_COL_ALERT_LASTSTATE]).strip()
        if not uid or not tk:
            laststate_col.append([prev_state]); continue
        enabled = rc.resolve_alert_events(r[_COL_ALERT_STATES], _WL_ALERT_DEFAULT)
        sl = pd.to_numeric(r[8], errors="coerce")
        tp = pd.to_numeric(r[9], errors="coerce")
        ap = pd.to_numeric(r[3], errors="coerce")           # Alert_Price (목표 매수가)
        ar = pd.to_numeric(r[4], errors="coerce")           # Alert_RSI
        am = str(r[5]).strip().lower() == "true"            # Alert_MA200
        try:
            if tk not in hist_cache:
                hist_cache[tk] = _fmp_price_history(tk)
            hist = hist_cache[tk]
            record_attempt()
            if hist is None or hist.empty:
                # 조용히 건너뛰지 않는다 — 티커 변경·상장폐지면 이 종목은
                # 영구히 평가에서 빠진다. FMP 추가 호출은 없다.
                record_nodata(uid, tk, "워치리스트")
                laststate_col.append([prev_state]); continue
            an = rc.analyze_ticker(hist, spy_close=spy_close)
            _eblk = (_blocks.get(tk)
                     if str(uid).strip().lower() in _earn_users else None)
            # 시장 게이트: 종목 무관 전역 조건. 실적 동결과 OR 결합.
            _mblk = (_mkt.get("reason") or "시장 경고"
                     if (_mkt_blocked and str(uid).strip().lower() in _mkt_users) else None)
            fired, new_state = rc.evaluate_alert_transitions(
                an, enabled, prev_state, today_str=today, price=float(hist["Close"].iloc[-1]),
                entry_blocked=bool(_eblk or _mblk),
                stop_loss=(float(sl) if pd.notna(sl) else None),
                target_price=(float(tp) if pd.notna(tp) else None),
                alert_price=(float(ap) if pd.notna(ap) else None),
                alert_rsi=(float(ar) if pd.notna(ar) else None),
                alert_ma200=am,
            )
            laststate_col.append([new_state]); n_eval += 1
            if _eblk:
                print(f"  [GATE-WL] {uid}/{tk}: entry 동결 — {_eblk}")
            elif _mblk:
                print(f"  [GATE-MKT] {uid}/{tk}: entry 동결 — {_mblk}")
            if fired:
                # v2: entry 알림에 R:R 게이트 결과 반영 (앱 [7] 탭과 동일 판정 — regime_core SSOT)
                for _ev in fired:
                    if _ev.get("event") == "entry":
                        try:
                            _sz = _sizing_kwargs_for(uid, acct, hist_cache)
                            _plan = rc.build_watchlist_plan(
                                hist, an,
                                manual_stop=(float(sl) if pd.notna(sl) else None),
                                manual_target=(float(tp) if pd.notna(tp) else None),
                                **_sz,
                            )
                            rc.decorate_entry_alert(_ev, _plan,
                                                    an.get("regime", {}).get("regime"))
                            _ev["plan"] = _plan          # 이메일 표시용
                            _ev["account"] = acct
                        except Exception as _ge:
                            print(f"  [WARN] {uid}/{tk} 게이트 산출 실패(신호는 유지): {_ge}")
                fired_by_user.setdefault(uid, []).append({"ticker": tk, "fired": fired, "an": an, "account": acct})
                print(f"  [FIRE-WL] {uid}/{acct or '미지정'}/{tk}: {[e['event'] for e in fired]}")
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
        avg = pd.to_numeric(r[_PF_COL_AVG], errors="coerce")
        date_added = str(r[_PF_COL_DATE_ADDED]).strip()
        key = f"{uid}|{account}|{tk}"
        states_csv = state_map.get(key, {}).get("states", "")
        enabled = rc.resolve_alert_events(states_csv, _PF_ALERT_DEFAULT)
        prev = state_map.get(key, {}).get("last", "")
        try:
            hist = _pf_hist(tk, hist_cache)
            record_attempt()
            if hist is None or hist.empty:
                # 🔴 보유 종목의 미수신은 **매도 신호가 영구히 오지 않는다**는
                #    뜻이다. 워치리스트보다 심각도가 높으므로 반드시 기록한다.
                record_nodata(uid, tk, "보유")
                new_rows.append([key, states_csv, prev, today]); continue
            _entry = float(avg) if pd.notna(avg) else None
            an = rc.analyze_ticker(hist, spy_close=spy_close,
                                   entry_price=_entry, entry_date=date_added)
            # 2A: 진입 시점 baseline 을 매 실행 시 Date_Added 로 재구성(시트 저장 없음).
            _bl = rc.compute_entry_baseline(hist, entry_price=_entry,
                                            entry_date=date_added, spy_close=spy_close)
            # 포지션(중장기) 판정은 상태머신 '입력'이다 — pexit/ptrim 이벤트의 트리거이므로
            # 발동 여부와 무관하게 먼저 계산해서 넘긴다. (이전에는 fired 이후에만 계산돼
            #  스윙이 울려야만 포지션 판정이 사용자에게 도달했다.)
            _posv = rc.position_sell_verdict(
                hist, float(avg) if pd.notna(avg) else None, entry_date=date_added)
            fired, new_state = rc.evaluate_alert_transitions(
                an, enabled, prev, today_str=today, price=float(hist["Close"].iloc[-1]),
                pos_verdict=_posv, entry_baseline=_bl,
            )
            new_rows.append([key, states_csv, new_state, today]); n_eval += 1
            if fired:
                _swc = rc.build_sell_card(an, None)
                fired_by_user.setdefault(uid, []).append(
                    {"ticker": tk, "account": account, "fired": fired, "an": an,
                     "swing": (_swc["label"], _swc["detail"] or _swc["headline"]),
                     "position": _posv})
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
    ev = ("entry", "risk", "exit", "price", "watch")
    for r in vals[1:]:
        r = (list(r) + [""] * _WL_NCOL)[:_WL_NCOL]
        uid, tk = str(r[0]).strip(), str(r[1]).strip().upper()
        acct = str(r[_WL_COL_ACCOUNT]).strip()
        if not uid or not tk:
            continue
        enabled = rc.resolve_alert_events(r[_COL_ALERT_STATES], _WL_ALERT_DEFAULT)
        if not any(e in enabled for e in ev):
            continue
        sl = pd.to_numeric(r[8], errors="coerce")
        tp = pd.to_numeric(r[9], errors="coerce")
        ap = pd.to_numeric(r[3], errors="coerce")
        ar = pd.to_numeric(r[4], errors="coerce")
        am = str(r[5]).strip().lower() == "true"
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
                                        target_price=(float(tp) if pd.notna(tp) else None),
                                        alert_price=(float(ap) if pd.notna(ap) else None),
                                        alert_rsi=(float(ar) if pd.notna(ar) else None),
                                        alert_ma200=am)
            active = [{"event": e, "label": rc.ALERT_EVENT_LABELS[e], "message": conds[e][1]}
                      for e in ev if e in enabled and conds.get(e, (False, ""))[0]]
            # v2: 장중 entry 알림에도 동일 게이트 반영 (live 가격 기준)
            for _ev in active:
                if _ev.get("event") == "entry":
                    try:
                        _sz = _sizing_kwargs_for(uid, acct, hist_cache)
                        _plan = rc.build_watchlist_plan(
                            hist, an,
                            manual_stop=(float(sl) if pd.notna(sl) else None),
                            manual_target=(float(tp) if pd.notna(tp) else None),
                            entry=float(live),
                            **_sz,
                        )
                        rc.decorate_entry_alert(_ev, _plan,
                                                an.get("regime", {}).get("regime"))
                        _ev["plan"] = _plan
                        _ev["account"] = acct
                    except Exception as _ge:
                        print(f"  [WARN] {uid}/{tk} 장중 게이트 산출 실패(신호는 유지): {_ge}")
            if active:
                hits_by_user.setdefault(uid, []).append({"ticker": tk, "fired": active, "an": an, "account": acct})
                print(f"  [INTRADAY-WL] {uid}/{acct or '미지정'}/{tk}: {[a['event'] for a in active]}")
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
        avg = pd.to_numeric(r[_PF_COL_AVG], errors="coerce")
        date_added = str(r[_PF_COL_DATE_ADDED]).strip()
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
            _base_hist = _pf_hist(tk, hist_cache)
            if tk not in quote_cache:
                quote_cache[tk] = _fmp_quote_price(tk)
            hist = _with_intraday_price(_base_hist, quote_cache[tk])
            if hist is None or hist.empty:
                continue
            _entry = float(avg) if pd.notna(avg) else None
            an = rc.analyze_ticker(hist, spy_close=spy_close,
                                   entry_price=_entry, entry_date=date_added)
            if not an.get("regime", {}).get("enough_data"):
                continue
            live = quote_cache[tk] if quote_cache[tk] is not None else float(hist["Close"].iloc[-1])
            _posv = rc.position_sell_verdict(
                hist, float(avg) if pd.notna(avg) else None, entry_date=date_added)
            conds = rc.alert_conditions(an, price=live, stop_loss=_sl, target_price=_tp,
                                        pos_verdict=_posv)
            # 2A: EOD 와 동일 규칙으로 장중에도 억제(공용 SSOT 헬퍼)
            _bl = rc.compute_entry_baseline(hist, entry_price=_entry,
                                            entry_date=date_added, spy_close=spy_close)
            _sup = rc.baseline_suppressed_events(an, _bl, pos_verdict=_posv)
            active = [{"event": e, "label": rc.ALERT_EVENT_LABELS[e], "message": conds[e][1]}
                      for e in _PF_INTRADAY_EVENTS
                      if e in enabled and e not in _sup and conds.get(e, (False, ""))[0]]
            # 손절/목표 도달(price)은 플랜이 설정된 보유에서 항상 발화
            if _has_plan and conds.get("price", (False, ""))[0]:
                active.append({"event": "price", "label": rc.ALERT_EVENT_LABELS["price"],
                               "message": conds["price"][1]})
            if active:
                _swc = rc.build_sell_card(an, None)
                hits_by_user.setdefault(uid, []).append(
                    {"ticker": tk, "account": account, "fired": active, "an": an,
                     "swing": (_swc["label"], _swc["detail"] or _swc["headline"]),
                     "position": _posv})
                print(f"  [INTRADAY-PF] {uid}/{account}/{tk}: {[a['event'] for a in active]}")
        except Exception as e:
            print(f"  [WARN] {uid}/{account}/{tk} 장중 평가 실패: {e}")
    return hits_by_user


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["eod", "intraday"], default="eod",
                        help="eod=마감 후 확정(기본) / intraday=장중 잠정 헤드업")
    parser.add_argument("--scope", choices=["watchlist", "portfolio", "both", "metrics"],
                        default="both",
                        help="평가 대상 (기본 both). metrics=알림 없이 Watchlist_Metrics 만 백필")
    args = parser.parse_args()

    print("=" * 60)
    print(f"[START] 알림 ({args.mode}/{args.scope}): "
          f"{datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    # metrics 백필은 '확정된 마지막 세션' 기준이라 휴장일에 돌려도 결과가 같다.
    # 알림 경로만 휴장일에 멈춘다(상태머신 카운터를 진행시키면 안 되므로).
    if not is_market_open_today() and args.scope != "metrics":
        print("[SKIP] 오늘은 NYSE 휴장일 — 평가 건너뜀(카운터 미진행).")
        return

    today = datetime.now(_ET).strftime("%Y-%m-%d")
    do_wl = args.scope in ("watchlist", "both")
    do_pf = args.scope in ("portfolio", "both")
    # 지표 프리컴퓨트: 정기 EOD 에 곁들이거나(추가 FMP 호출 0 — hist_cache 재사용),
    # metrics 스코프로 단독 백필한다. 장중에는 미완성 봉이 저장되므로 하지 않는다.
    do_metrics = (args.mode == "eod") and args.scope in ("watchlist", "both", "metrics")

    reset_nodata()          # 실행마다 미수신 집계 초기화
    spy_hist = _fmp_price_history("SPY")
    spy_close = spy_hist["Close"] if (spy_hist is not None and not spy_hist.empty) else None
    hist_cache, quote_cache = {}, {}

    try:
        if args.scope == "metrics":
            # 알림/이메일 없음. 확정 봉 기준으로만 계산해 실행 시각에 무관하게 만든다.
            persist_watchlist_metrics(spy_close, hist_cache, today, completed_only=True)
        elif args.mode == "intraday":
            wl_res, pf_res = {}, {}
            if do_wl:
                wl_res = eval_watchlist_intraday(spy_close, hist_cache, quote_cache, today)
            if do_pf:
                pf_res = eval_portfolio_intraday(spy_close, hist_cache, quote_cache, today)
            total = (sum(len(v) for v in wl_res.values())
                     + sum(len(v) for v in pf_res.values()))
            if total:
                _banner = ("<p style='background:#fff8e1;padding:8px;border-radius:6px;color:#8a6d00'>"
                           "⏱️ <b>장중 잠정 신호</b> — 진행 중인 봉 기준이라 마감까지 바뀔 수 있습니다. "
                           "분할 매수/청산 참고용.</p>")
                dispatch_radar_emails(wl_res, pf_res, today,
                                      "⏱️", "[장중] 후보", banner_html=_banner)
            else:
                print("[INFO] 장중 활성 후보 없음 — 이메일 생략.")
        else:
            wl_res, pf_res = {}, {}
            if do_wl:
                # 실적 임박 종목의 entry 는 동결(freeze)한다 — 상태 보존 → 발표 후 재개
                _e_users = load_earnings_gate_users()
                # 시장 진입 게이트 — SPY 는 이미 확보돼 있어 추가 API 호출 없음.
                _m_users = load_market_gate_users()
                # 판정은 **토글과 무관하게 항상** 수행한다.
                #   _m_users 는 '실제로 동결할 대상'만 정하고, 판정 결과는 관찰 배너에
                #   쓰인다. 토글이 전원 N 이어도 "게이트가 켜져 있었다면 어땠을까"를
                #   기록해야 켤지 말지 판단할 근거가 쌓인다.
                #   SPY 는 이미 확보돼 있어 추가 API 호출은 없다.
                _m_gate = rc.market_gate_status(spy_close)
                print(f"[GATE] 시장 진입 게이트: {_m_gate['reason']}"
                      + (f" · 적용 대상 {len(_m_users)}명" if _m_users else " · 적용 대상 없음(관찰만)"))
                wl_res = eval_watchlist_eod(
                    spy_close, hist_cache, today,
                    earn_blocks=(load_earnings_blocks() if _e_users else {}),
                    earn_users=_e_users,
                    mkt_gate=_m_gate, mkt_users=_m_users)
            if do_pf:
                pf_res = eval_portfolio_eod(spy_close, hist_cache, today)
            total = (sum(len(v) for v in wl_res.values())
                     + sum(len(v) for v in pf_res.values()))
            nodata_log_summary()
            # 발동 0건이어도 데이터 미수신이 있으면 발송한다. 미수신은 그 자체가
            # 알림이고, 여기서 걸러 버리면 침묵을 알리는 경로가 같은 이유로
            # 침묵한다. 개별 사용자 단위 판단은 _send_one_user 가 다시 한다.
            if total or nodata_total():
                dispatch_radar_emails(wl_res, pf_res, today, "🔔", "매매 레이더",
                                      banner_html=build_market_gate_banner(_m_gate, _m_users))
            else:
                print("[INFO] 발동된 알림 없음 · 미수신 없음 — 이메일 생략.")
    except Exception as e:
        print(f"[ERROR] 평가 실패: {e}")
        traceback.print_exc()

    # 알림 성패와 분리한다. 알림이 실패해도 앱이 쓸 지표는 남기고,
    # 지표 저장이 실패해도 이미 나간 이메일에는 영향이 없다(앱은 실시간 계산 폴백).
    if do_metrics and args.scope != "metrics":
        try:
            persist_watchlist_metrics(spy_close, hist_cache, today, completed_only=False)
        except Exception as e:
            print(f"[ERROR] 지표 저장 실패(알림에는 영향 없음): {e}")
            traceback.print_exc()

    print(f"[DONE] {datetime.now(_KST).strftime('%H:%M KST')}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
