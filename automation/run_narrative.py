"""
run_narrative.py
────────────────
GitHub Actions 자동 실행 스크립트: 시장 내러티브 생성 + Google Sheets 저장 + 이메일 발송
실행 환경: GitHub Actions (Ubuntu), Python 3.11+
"""

import os
import sys
import json
import re
import time
import smtplib
import traceback
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import feedparser
import numpy as np
import pandas as pd
import pytz
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types as genai_types
from fredapi import Fred

# ── 공유 뉴스 파이프라인 (SSOT) ──────────────────────────────────────────────
# app.py 와 동일한 narrative_core 모듈을 import (드리프트 방지).
# 자동화는 automation/ 하위에서 실행되므로, narrative_core.py 가 위치한
# 레포 루트(app.py·fmp_extras.py와 동일 폴더)를 sys.path 에 추가한다.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)                    # automation/ (모듈이 여기 있을 경우 대비)
sys.path.insert(0, os.path.dirname(_here))   # 레포 루트 (app.py·fmp_extras.py·narrative_core.py 위치)
import narrative_core
from narrative_core import fetch_global_market_news

# ── 환경변수 로드 ──────────────────────────────────────────────────────────────
GOOGLE_API_KEY   = os.environ["GOOGLE_API_KEY"]
FRED_API_KEY     = os.environ["FRED_API_KEY"]
GMAIL_USER       = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO         = os.environ["GMAIL_TO"]
GSPREAD_KEY_JSON = os.environ["GSPREAD_KEY"]        # JSON 문자열

# GCP 서비스 계정: JSON 문자열 → dict
_gcp_info = json.loads(GSPREAD_KEY_JSON)

# ── 상수 ───────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")
_SPREADSHEET_TITLE   = "Quant_DB"
_NARRATIVES_WORKSHEET = "Narratives"
_ADMIN_USER_ID       = "yab"

# ── FRED 공휴일 체크용 NYSE 휴장일 목록 (고정 + 동적) ────────────────────────
_NYSE_FIXED_HOLIDAYS_2025 = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25",
}
_NYSE_FIXED_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}
_NYSE_HOLIDAYS = _NYSE_FIXED_HOLIDAYS_2025 | _NYSE_FIXED_HOLIDAYS_2026


def is_market_open_today() -> bool:
    """오늘(ET 기준)이 NYSE 개장일인지 확인."""
    et_now = datetime.now(_ET)
    weekday = et_now.weekday()   # 0=월 … 4=금
    date_str = et_now.strftime("%Y-%m-%d")
    if weekday >= 5:             # 토·일
        return False
    if date_str in _NYSE_HOLIDAYS:
        return False
    return True


# ── 경제지표 캘린더 (하드코딩 + FRED API 보조) ────────────────────────────────

# 2026년 주요 경제지표 발표 일정 (공식 캘린더 기준, ET 8:30 발표)
# 출처: federalreserve.gov, bls.gov
_HARDCODED_CALENDAR_2026 = {
    # FOMC 발표일 (2일차 = 결정 발표일)
    "FOMC 금리 결정": [
        "2026-01-28", "2026-03-18", "2026-04-29",
        "2026-06-17", "2026-07-29", "2026-09-16",
        "2026-10-28", "2026-12-09",
    ],
    # CPI 발표일 (BLS 공식 일정)
    "CPI 소비자물가지수": [
        "2026-01-14", "2026-02-11", "2026-03-11",
        "2026-04-10", "2026-05-13", "2026-06-10",
        "2026-07-15", "2026-08-12", "2026-09-11",
        "2026-10-13", "2026-11-12", "2026-12-10",
    ],
    # NFP 고용보고서 (BLS 공식 일정)
    "NFP 고용보고서": [
        "2026-01-09", "2026-02-06", "2026-03-06",
        "2026-04-03", "2026-05-08", "2026-06-05",
        "2026-07-10", "2026-08-07", "2026-09-04",
        "2026-10-02", "2026-11-06", "2026-12-04",
    ],
}

_MAJOR_RELEASE_KEYWORDS = [
    "consumer price", "cpi", "producer price", "ppi",
    "employment situation", "nonfarm", "payroll",
    "gdp", "gross domestic", "federal open market", "fomc",
    "personal consumption", "pce", "retail sales",
    "industrial production", "initial claims", "jobless",
]


def get_todays_major_releases(fred: Fred) -> list[str]:
    """오늘 날짜의 주요 경제지표 발표 목록 반환 (하드코딩 캘린더만 사용)."""
    today_str = datetime.now(_ET).strftime("%Y-%m-%d")
    releases = []
    for event_name, dates in _HARDCODED_CALENDAR_2026.items():
        if today_str in dates:
            releases.append(event_name)
            print(f"[INFO] 경제지표 발표일: {event_name}")
    return releases



def build_fred_alert_text(releases: list[str]) -> str:
    """FRED 발표일 경고 텍스트 생성."""
    if not releases:
        return ""
    items = "\n".join(f"  - {r}" for r in releases)
    return (
        f"\n⚠️ [오늘의 주요 경제지표 발표 — FRED 캘린더]\n"
        f"{items}\n"
        f"→ 위 지표 발표 전후 시장 변동성이 높아질 수 있습니다. "
        f"예측의 불확실성이 평소보다 높습니다. 보수적으로 해석하세요.\n"
    )


# ── 뉴스 수집 ─────────────────────────────────────────────────────────────────
def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(raw or ""))
    return re.sub(r"\s+", " ", text).strip()


# ── 뉴스 파이프라인 ─────────────────────────────────────────────────────────
# 뉴스 수집/디둡/랭킹/컨텍스트는 공유 모듈 narrative_core 로 이전 (SSOT, app.py와 공유).
# fetch_global_market_news 는 narrative_core 에서 import 한다(상단 import 참조).


# ── Gemini 내러티브 생성 ───────────────────────────────────────────────────────
def generate_market_narrative(news_text: str, fred_alert: str = "") -> dict:
    """뉴스 텍스트 → Gemini → 내러티브 JSON.
    프롬프트/파싱은 공유 모듈 narrative_core (SSOT, app.py와 동일).
    Gemini 호출·재시도는 자동화 자체 google-genai 클라이언트로 수행.
    """
    client = genai.Client(api_key=GOOGLE_API_KEY)
    prompt = narrative_core.build_narrative_prompt(news_text, target_language="ko", fred_alert=fred_alert)

    for attempt in range(5):
        try:
            cfg = genai_types.GenerateContentConfig(temperature=0.3, top_p=1, max_output_tokens=8192)
            response = client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt, config=cfg
            )
            raw = str(getattr(response, "text", "") or "").strip()
            result = narrative_core.parse_narrative_json(raw)
            if result is not None:
                return result
            print(f"[WARN] Gemini 시도 {attempt+1}/5: 파싱 실패")
        except Exception as e:
            print(f"[WARN] Gemini 시도 {attempt+1}/5 실패: {e}")
        wait = [10, 30, 60, 120][min(attempt, 3)]
        if attempt < 4:
            time.sleep(wait)
    return {}

# ── Google Sheets 저장 ────────────────────────────────────────────────────────
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(_gcp_info, scopes=scopes)
    return gspread.authorize(creds)


# ── Emerging_Tracker (app.py와 동일한 시트 구조) ──────────────────────────────
_EMERGING_TRACKER_WORKSHEET = "Emerging_Tracker"
_EMERGING_TRACKER_COLS = ["ID", "Ticker", "Theme", "First_Seen", "Last_Seen",
                          "Count", "Best_Verdict", "RS_Score", "Status"]
# 검증에 쓸 FMP (app.py와 동일한 stable API)
_FMP_BASE    = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 10
FMP_API_KEY  = os.environ.get("FMP_API_KEY", "").strip()


def _safe_append_rows(ws, rows, value_input_option: str = "USER_ENTERED") -> None:
    """gspread append_row의 '계단식 드리프트' 버그 회피. 항상 A열 기준 마지막 데이터 다음에 기록."""
    if rows is None:
        return
    if len(rows) > 0 and not isinstance(rows[0], (list, tuple)):
        rows = [rows]
    rows = [list(r) for r in rows if r is not None]
    if not rows:
        return
    existing = ws.get_all_values() or []
    last_row = 0
    for idx, r in enumerate(existing, start=1):
        if any(str(c).strip() != "" for c in r):
            last_row = idx
    start_row = last_row + 1
    end_row = start_row + len(rows) - 1
    try:
        if end_row > ws.row_count:
            ws.add_rows(end_row - ws.row_count + 50)
    except Exception:
        pass
    n_cols = max(len(r) for r in rows)
    end_cell = gspread.utils.rowcol_to_a1(end_row, n_cols)
    ws.update(rows, range_name=f"A{start_row}:{end_cell}",
              value_input_option=value_input_option)


def _session_label_for_utc(dt_utc) -> str:
    """app.py와 동일한 세션 라벨."""
    dt_et = dt_utc.astimezone(_ET)
    m = dt_et.hour * 60 + dt_et.minute
    if 240 <= m <= 569:  return "🌅 Pre-market Prep"
    if 570 <= m <= 960:  return "🟢 Market Hours Analysis"
    if 961 <= m <= 1200: return "🔔 Daily Recap (Post-Market)"
    return "🌙 Overnight Strategy"


def parse_tickers_from_csv(text: str) -> list[str]:
    return [t.strip().upper() for t in str(text or "").split(",")
            if re.match(r"^[A-Z]{1,5}$", t.strip().upper())]


def winners_from_analysis(analysis: dict) -> list[str]:
    out, seen = [], set()
    for theme in analysis.get("themes", []):
        for t in parse_tickers_from_csv(theme.get("winners", "")):
            if t not in seen: seen.add(t); out.append(t)
    return out


def emerging_from_analysis(analysis: dict) -> list[str]:
    out, seen = [], set()
    for theme in analysis.get("themes", []):
        for flow in theme.get("expanding_to", []):
            for t in parse_tickers_from_csv(flow.get("expected_tickers", "")):
                if t not in seen: seen.add(t); out.append(t)
    return out


def save_narrative_to_sheet(analysis: dict, news_count: int, fred_releases: list[str]) -> bool:
    """app.py와 완전히 동일한 형식으로 Narratives 시트에 저장."""
    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        try:
            ws = sh.worksheet(_NARRATIVES_WORKSHEET)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=_NARRATIVES_WORKSHEET, rows=3000, cols=7)
            _safe_append_rows(ws, ["ID","Date","Category","Title","Content","Winners","Emerging"],
                              value_input_option="USER_ENTERED")

        from datetime import timezone
        now_utc = datetime.now(timezone.utc)

        # ── app.py append_narrative_history_record 와 동일한 record 구조 ──
        session_label = _session_label_for_utc(now_utc)
        record = {
            "saved_at":      now_utc.isoformat(),
            "session_label": session_label,
            "language":      "ko",
            "analysis":      analysis,
        }

        # ── app.py _narrative_record_to_sheet_row 와 동일한 변환 ──
        date_kst  = now_utc.astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S")
        category  = str(analysis.get("source") or "market_narrative")
        themes    = analysis.get("themes", [])
        if themes and isinstance(themes[0], dict):
            title = str(themes[0].get("title", "") or "").strip() or "시장 내러티브 스냅샷"
        else:
            title = "시장 내러티브 스냅샷"
        if len(title) > 500: title = title[:497] + "..."

        content = json.dumps(record, ensure_ascii=False)
        w_csv   = ",".join(winners_from_analysis(analysis))
        e_csv   = ",".join(emerging_from_analysis(analysis))

        row = [_ADMIN_USER_ID, date_kst, category, title, content, w_csv, e_csv]
        _safe_append_rows(ws, row, value_input_option="USER_ENTERED")
        print(f"[OK] Sheets 저장 완료: {title} | {session_label}")
        return True
    except Exception as e:
        print(f"[ERROR] Sheets 저장 실패: {e}")
        traceback.print_exc()
        return False


# ── Emerging 정량 검증 + 추적기 적재 (app.py 로직 포팅) ───────────────────────
def _fmp_close_series(ticker: str, limit: int = 130):
    """FMP stable historical-price-eod → 종가 Series (오래된→최신 정렬)."""
    if not FMP_API_KEY:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    try:
        r = requests.get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker}&limit={limit}&apikey={FMP_API_KEY}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code != 200:
            return pd.Series(dtype=float), pd.Series(dtype=float)
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.Series(dtype=float), pd.Series(dtype=float)
        df = pd.DataFrame(rows)
        if "date" not in df.columns or "close" not in df.columns:
            return pd.Series(dtype=float), pd.Series(dtype=float)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        vol = pd.to_numeric(df["volume"], errors="coerce").dropna() if "volume" in df.columns else pd.Series(dtype=float)
        return close, vol
    except Exception:
        return pd.Series(dtype=float), pd.Series(dtype=float)


def verify_emerging_with_quant(emerging_tickers: list) -> list[dict]:
    """app.py verify_emerging_with_quant와 동일한 판정 기준으로 Emerging 종목 검증."""
    if not emerging_tickers or not FMP_API_KEY:
        return []
    unique = list(dict.fromkeys(str(t).strip().upper() for t in emerging_tickers if str(t).strip()))

    # SPY 3개월 수익률 (RS 기준선)
    spy_close, _ = _fmp_close_series("SPY", limit=130)
    spy_3m = float((spy_close.iloc[-1] / spy_close.iloc[-64] - 1) * 100) if len(spy_close) >= 64 else 0.0

    results = []
    for tk in unique:
        s, vol = _fmp_close_series(tk, limit=130)
        if len(s) < 22:
            continue
        mom_1m = float((s.iloc[-1] / s.iloc[-22] - 1) * 100) if len(s) >= 22 else np.nan
        mom_3m = float((s.iloc[-1] / s.iloc[-64] - 1) * 100) if len(s) >= 64 else np.nan
        rs_score = float(mom_3m - spy_3m) if pd.notna(mom_3m) else np.nan
        ma200 = float(s.rolling(200, min_periods=150).mean().iloc[-1]) if len(s) >= 150 else np.nan
        above_ma200 = bool(s.iloc[-1] > ma200) if pd.notna(ma200) else None
        vol_surge = float(vol.tail(5).mean() / vol.tail(21).mean()) if len(vol) >= 21 else np.nan

        # app.py와 100% 동일한 판정 분기
        if pd.notna(rs_score) and rs_score < 0 and pd.notna(vol_surge) and vol_surge >= 1.5:
            verdict = "🎯 최적 매수 타이밍"
        elif pd.notna(rs_score) and rs_score < 3 and above_ma200 and pd.notna(mom_1m) and mom_1m > 2:
            verdict = "🌱 얼리버드 기회"
        elif pd.notna(rs_score) and rs_score > 5 and above_ma200:
            verdict = "✅ 이미 강세 (진입 시 고점 주의)"
        elif above_ma200 is False:
            verdict = "❌ 하락 추세 (대기)"
        else:
            verdict = "⏳ 신호 대기"

        results.append({
            "ticker": tk,
            "rs_score": round(rs_score, 2) if pd.notna(rs_score) else None,
            "verdict": verdict,
        })
        time.sleep(0.15)  # FMP rate-limit 완화
    return results


def upsert_emerging_tracker(ws, vals_cache: list, user_id: str, ticker: str,
                            theme: str, verdict: str, rs_score) -> list:
    """app.py upsert_emerging_tracker와 동일: 같은 티커면 Count++/Last_Seen 갱신, 없으면 신규.
    vals_cache: 현재 시트 전체 값(중복 read 방지용). 갱신 후 최신 캐시를 반환."""
    uid_u = str(user_id).strip().upper()
    tk_u = str(ticker).strip().upper()
    now_str = datetime.now(_ET).strftime("%Y-%m-%d %H:%M")
    rs_str = f"{float(rs_score):.2f}" if rs_score is not None and rs_score == rs_score else ""

    found_row = None
    for i, r in enumerate(vals_cache[1:], start=2):
        r = (r + [""] * 9)[:9]
        if str(r[0]).strip().upper() == uid_u and str(r[1]).strip().upper() == tk_u:
            found_row = (i, r)
            break

    verdict_priority = {"🎯 최적 매수 타이밍": 0, "🌱 얼리버드 기회": 1, "✅ 이미 강세 (진입 시 고점 주의)": 2}

    if found_row:
        row_idx, old_r = found_row
        old_count = int(old_r[5]) if str(old_r[5]).isdigit() else 1
        new_count = old_count + 1
        best_verdict = verdict if verdict_priority.get(verdict, 99) < verdict_priority.get(old_r[6], 99) else old_r[6]
        if new_count >= 5:
            status = "🔥 지속 등장 (강한 신호)"
        elif new_count >= 3:
            status = "📌 반복 등장"
        else:
            status = "🆕 신규"
        ws.update([[uid_u, tk_u, theme, old_r[3], now_str, str(new_count), best_verdict, rs_str, status]],
                  range_name=f"A{row_idx}:I{row_idx}", value_input_option="USER_ENTERED")
        # 캐시 갱신
        vals_cache[row_idx - 1] = [uid_u, tk_u, theme, old_r[3], now_str, str(new_count), best_verdict, rs_str, status]
    else:
        new_row = [uid_u, tk_u, str(theme)[:60], now_str, now_str, "1", verdict, rs_str, "🆕 신규"]
        _safe_append_rows(ws, new_row, value_input_option="USER_ENTERED")
        vals_cache.append(new_row)
    return vals_cache


def should_load_emerging_tracker() -> bool:
    """추적기 적재 여부 판단 — 하루 1회 규칙.
    · 평일(월~금): ET 오전(낮 12시 이전, =8AM 실행)에만 적재. 평일 5PM 실행은 건너뜀.
    · 주말(토·일): 5PM 실행 1회만 있으므로 항상 적재.
    """
    et_now = datetime.now(_ET)
    weekday = et_now.weekday()  # 0=월 … 5=토, 6=일
    if weekday >= 5:            # 주말
        return True
    return et_now.hour < 12     # 평일은 오전(8AM)만


def run_emerging_tracking(analysis: dict) -> None:
    """내러티브의 Emerging 티커를 정량 검증 후 Emerging_Tracker에 적재 (하루 1회)."""
    if not should_load_emerging_tracker():
        print("[INFO] Emerging 추적기 적재 스킵 (평일 오후 실행 — 하루 1회 규칙).")
        return
    if not FMP_API_KEY:
        print("[WARN] FMP_API_KEY 없음 → Emerging 검증 건너뜀 (내러티브 저장은 정상).")
        return

    emerging = emerging_from_analysis(analysis)
    if not emerging:
        print("[INFO] 내러티브에 Emerging 티커 없음 → 추적기 적재 없음.")
        return

    print(f"[STEP 3.5] Emerging 종목 {len(emerging)}개 정량 검증 중...")
    verified = verify_emerging_with_quant(emerging)
    if not verified:
        print("[WARN] Emerging 검증 결과 없음 (FMP 데이터 부족 가능).")
        return

    # 티커 → 테마 제목 매핑 (app.py와 동일하게 emerging에 등장한 테마명)
    theme_of = {}
    for theme in analysis.get("themes", []):
        if not isinstance(theme, dict):
            continue
        title = str(theme.get("title", "") or "").strip()
        for flow in theme.get("expanding_to", []):
            for t in parse_tickers_from_csv(flow.get("expected_tickers", "")):
                theme_of.setdefault(t, title)

    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        try:
            ws = sh.worksheet(_EMERGING_TRACKER_WORKSHEET)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=_EMERGING_TRACKER_WORKSHEET, rows=2000, cols=9)
            _safe_append_rows(ws, _EMERGING_TRACKER_COLS, value_input_option="USER_ENTERED")

        vals_cache = ws.get_all_values() or [_EMERGING_TRACKER_COLS]
        n_new = n_upd = 0
        for v in verified:
            before = len(vals_cache)
            vals_cache = upsert_emerging_tracker(
                ws, vals_cache, _ADMIN_USER_ID, v["ticker"],
                theme_of.get(v["ticker"], "내러티브"), v["verdict"], v["rs_score"],
            )
            if len(vals_cache) > before:
                n_new += 1
            else:
                n_upd += 1
        print(f"[OK] Emerging 추적기 적재 완료: 신규 {n_new} · 갱신 {n_upd} (검증 {len(verified)}개)")
    except Exception as e:
        print(f"[ERROR] Emerging 추적기 적재 실패: {e}")
        traceback.print_exc()


# ── HTML 이메일 생성 ───────────────────────────────────────────────────────────
def build_email_html(analysis: dict, news_count: int, fred_releases: list[str], is_market_day: bool, news_meta: dict = None) -> str:
    now_et  = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
    now_kst = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
    regime  = analysis.get("regime", {})
    risk    = regime.get("risk", "N/A")
    gv      = regime.get("growth_value", "N/A")
    liq     = regime.get("liquidity", "N/A")
    risk_color = "#16a34a" if "On" in risk else "#dc2626"

    # 테마 섹션
    themes_html = ""
    for i, theme in enumerate(analysis.get("themes", []), 1):
        mom = str(theme.get("momentum_note", "보통"))
        mom_color = {"강함": "#16a34a", "보통": "#d97706", "약함": "#dc2626"}.get(mom, "#6b7280")
        themes_html += f"""
        <div style="background:#1e293b;border-radius:8px;padding:14px 16px;margin-bottom:12px;border-left:4px solid {mom_color};">
          <div style="font-size:15px;font-weight:700;color:#f1f5f9;">
            {i}. {theme.get('title','?')}
            <span style="font-size:12px;font-weight:500;color:{mom_color};margin-left:8px;">모멘텀 {mom}</span>
          </div>
          <div style="font-size:13px;color:#94a3b8;margin-top:6px;">📌 {theme.get('driver','')}</div>
          <div style="margin-top:8px;font-size:13px;">
            <span style="color:#34d399;">✅ Winners: {theme.get('winners','')}</span><br>
            <span style="color:#60a5fa;">🔍 Emerging: {theme.get('emerging','')}</span>
          </div>
          <div style="font-size:12px;color:#f87171;margin-top:6px;">⚠️ Risk: {theme.get('risk','')}</div>
        </div>"""

    # FRED 경보
    fred_html = ""
    if fred_releases:
        items = "".join(f"<li>{r}</li>" for r in fred_releases)
        fred_html = f"""
        <div style="background:#7c2d12;border-radius:8px;padding:12px 16px;margin-bottom:16px;border:1px solid #ea580c;">
          <div style="font-weight:700;color:#fed7aa;">⚠️ 오늘의 주요 경제지표 발표 (FRED 캘린더)</div>
          <ul style="color:#fdba74;margin:8px 0 0 0;padding-left:18px;font-size:13px;">{items}</ul>
          <div style="color:#fb923c;font-size:12px;margin-top:6px;">→ 발표 전후 변동성 증가 예상. 예측 불확실성 높음.</div>
        </div>"""

    market_badge = (
        '<span style="background:#16a34a;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;">📈 장 열리는 날</span>'
        if is_market_day else
        '<span style="background:#6b7280;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;">🔒 장 닫힌 날</span>'
    )

    # 뉴스 신선도 라인 (실시간성 가시화) — RSS 헤드라인 기반 요약임을 명시
    _m = news_meta or {}
    if _m.get("newest"):
        _newest_min = _m.get("newest_min_ago")
        _fresh_color = "#16a34a" if (isinstance(_newest_min, int) and _newest_min <= 180) else "#d97706"
        fresh_line = (
            f'<div style="font-size:12px;color:{_fresh_color};margin-top:4px;">'
            f'🕐 뉴스 신선도: 최신 {_newest_min}분 전 · 6시간 이내 {_m.get("fresh_6h",0)}/{_m.get("total",0)}건 · '
            f'소스 {len(_m.get("sources",[]))}곳 (FMP 뉴스 API 기반, 실시간 웹검색 아님)</div>'
        )
    else:
        fresh_line = (
            '<div style="font-size:12px;color:#d97706;margin-top:4px;">'
            '🕐 뉴스 신선도: 발행시각 정보 없음 (FMP 뉴스 API 기반, 실시간 웹검색 아님)</div>'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;">

  <!-- 헤더 -->
  <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #334155;">
    <div style="font-size:22px;font-weight:800;color:#60a5fa;">📰 Quant Terminal</div>
    <div style="font-size:14px;color:#94a3b8;margin-top:4px;">시장 내러티브 자동 분석 리포트</div>
    <div style="margin-top:8px;font-size:13px;color:#64748b;">{now_et} &nbsp;|&nbsp; {now_kst} &nbsp;|&nbsp; {market_badge}</div>
    <div style="font-size:12px;color:#64748b;margin-top:4px;">뉴스 {news_count}건 수집 → 상위 {_m.get('total', news_count)}건 분석 · Gemini 2.5 Flash</div>
    {fresh_line}
  </div>

  <!-- 레짐 -->
  <div style="background:#1e293b;border-radius:10px;padding:16px;margin-bottom:16px;display:flex;gap:12px;flex-wrap:wrap;">
    <div style="flex:1;text-align:center;padding:10px;background:#0f172a;border-radius:8px;">
      <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Risk Mode</div>
      <div style="font-size:18px;font-weight:700;color:{risk_color};margin-top:4px;">{risk}</div>
    </div>
    <div style="flex:1;text-align:center;padding:10px;background:#0f172a;border-radius:8px;">
      <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Style</div>
      <div style="font-size:16px;font-weight:700;color:#a78bfa;margin-top:4px;">{gv}</div>
    </div>
    <div style="flex:1;text-align:center;padding:10px;background:#0f172a;border-radius:8px;">
      <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Liquidity</div>
      <div style="font-size:16px;font-weight:700;color:#38bdf8;margin-top:4px;">{liq}</div>
    </div>
  </div>

  {fred_html}

  <!-- 요약 -->
  <div style="background:#1e293b;border-radius:10px;padding:16px;margin-bottom:16px;">
    <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:8px;">💡 시장 핵심 요약</div>
    <div style="font-size:14px;color:#cbd5e1;line-height:1.7;">{analysis.get('summary','')}</div>
  </div>

  <!-- 섹터 로테이션 -->
  <div style="background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;">🔄 섹터 로테이션</div>
    <div style="font-size:13px;color:#94a3b8;margin-top:6px;">{analysis.get('rotation','')}</div>
  </div>

  <!-- Top Picks -->
  <div style="background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;">🎯 Top Quant Picks</div>
    <div style="font-size:15px;font-weight:700;color:#34d399;margin-top:6px;">{analysis.get('top_quant_picks','')}</div>
  </div>

  <!-- 테마 -->
  <div style="margin-bottom:16px;">
    <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:10px;">📊 주요 투자 테마</div>
    {themes_html}
  </div>

  <!-- 앱 링크 -->
  <div style="text-align:center;padding:16px;">
    <a href="https://quantdb.streamlit.app" 
       style="background:#2563eb;color:#fff;text-decoration:none;padding:12px 32px;border-radius:8px;font-weight:700;font-size:14px;">
      🚀 Quant Terminal 열기
    </a>
  </div>

  <div style="text-align:center;font-size:11px;color:#475569;margin-top:16px;">
    본 리포트는 AI 참고용이며 투자 권유가 아닙니다. · Quant Terminal Auto Report
  </div>
</div>
</body></html>"""


def send_email(subject: str, html_body: str) -> bool:
    """Gmail SMTP로 HTML 이메일 발송."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = GMAIL_TO
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())
        print(f"[OK] 이메일 발송 완료 → {GMAIL_TO}")
        return True
    except Exception as e:
        print(f"[ERROR] 이메일 발송 실패: {e}")
        return False


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"[START] 시장 내러티브 생성 시작: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    market_day = is_market_open_today()
    print(f"[INFO] NYSE 개장일 여부: {market_day}")

    # FRED 캘린더 조회
    fred = Fred(api_key=FRED_API_KEY)
    fred_releases = get_todays_major_releases(fred)
    if fred_releases:
        print(f"[INFO] 오늘의 주요 경제지표: {fred_releases}")
    else:
        print("[INFO] 오늘 주요 경제지표 발표 없음")

    fred_alert = build_fred_alert_text(fred_releases)

    # 뉴스 수집
    print("[STEP 1] 뉴스 수집 중...")
    top_news, context_text, raw_count, news_meta = fetch_global_market_news(FMP_API_KEY)
    print(f"[INFO] 수집 완료: {raw_count}건 → Top {len(top_news)}건 사용")

    if not context_text:
        print("[ERROR] 뉴스 수집 실패. 종료.")
        sys.exit(1)

    # Gemini 내러티브 생성
    print("[STEP 2] Gemini 내러티브 생성 중...")
    analysis = generate_market_narrative(context_text, fred_alert)
    if not analysis:
        print("[ERROR] 내러티브 생성 실패. 종료.")
        sys.exit(1)
    print(f"[INFO] 내러티브 생성 완료. 테마 수: {len(analysis.get('themes', []))}")

    # Sheets 저장
    print("[STEP 3] Google Sheets 저장 중...")
    save_narrative_to_sheet(analysis, raw_count, fred_releases)

    # Emerging 추적기 적재 (하루 1회: 평일 8AM / 주말 5PM)
    try:
        run_emerging_tracking(analysis)
    except Exception as e:
        print(f"[WARN] Emerging 추적기 단계 예외(무시하고 계속): {e}")

    # 이메일 발송
    print("[STEP 4] 이메일 발송 중...")
    now_et = datetime.now(_ET)
    session_label = "Pre-Market 8AM" if now_et.hour < 12 else "After-Close 5PM"
    fred_tag = " ⚠️FRED발표" if fred_releases else ""
    subject = f"📰 [{session_label}] 시장 내러티브 리포트 {now_et.strftime('%m/%d')}{fred_tag}"

    html_body = build_email_html(analysis, raw_count, fred_releases, market_day, news_meta=news_meta)
    send_email(subject, html_body)

    print(f"[DONE] 완료: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
