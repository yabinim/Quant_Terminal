"""
run_drg_predict.py
──────────────────
GitHub Actions 자동 실행: DRG 시장 예측 생성 + Google Sheets 저장 + 이메일 발송
실행 시간: 평일 8AM ET (장 열리는 날만)
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

# ── 환경변수 ──────────────────────────────────────────────────────────────────
GOOGLE_API_KEY     = os.environ["GOOGLE_API_KEY"]
FRED_API_KEY       = os.environ["FRED_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]
GSPREAD_KEY_JSON   = os.environ["GSPREAD_KEY"]
_gcp_info          = json.loads(GSPREAD_KEY_JSON)

# ── 상수 ───────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")
_SPREADSHEET_TITLE        = "Quant_DB"
_DRG_PREDICTIONS_WORKSHEET = "DRG_Predictions"
_DRG_SHEET_COLS = [
    "user_id", "pred_date", "direction", "sector_filter", "benchmark_etf",
    "spy_close_at_pred", "full_text", "actual_direction", "actual_return_pct",
    "is_correct", "review_comment"
]
_ADMIN_USER_ID = "yab"

_NYSE_HOLIDAYS_2025 = {
    "2025-01-01","2025-01-20","2025-02-17","2025-04-18",
    "2025-05-26","2025-06-19","2025-07-04","2025-09-01",
    "2025-11-27","2025-12-25",
}
_NYSE_HOLIDAYS_2026 = {
    "2026-01-01","2026-01-19","2026-02-16","2026-04-03",
    "2026-05-25","2026-06-19","2026-07-03","2026-09-07",
    "2026-11-26","2026-12-25",
}
_NYSE_HOLIDAYS = _NYSE_HOLIDAYS_2025 | _NYSE_HOLIDAYS_2026


def is_market_open_today() -> bool:
    et_now = datetime.now(_ET)
    return et_now.weekday() < 5 and et_now.strftime("%Y-%m-%d") not in _NYSE_HOLIDAYS


# ── 경제지표 캘린더 (하드코딩 + FRED API 보조) ────────────────────────────────

# 2026년 주요 경제지표 발표 일정 (공식 캘린더 기준)
# 출처: federalreserve.gov, bls.gov
_HARDCODED_CALENDAR_2026 = {
    "FOMC 금리 결정": [
        "2026-01-28", "2026-03-18", "2026-04-29",
        "2026-06-17", "2026-07-29", "2026-09-16",
        "2026-10-28", "2026-12-09",
    ],
    "CPI 소비자물가지수": [
        "2026-01-14", "2026-02-11", "2026-03-11",
        "2026-04-10", "2026-05-13", "2026-06-10",
        "2026-07-15", "2026-08-12", "2026-09-11",
        "2026-10-13", "2026-11-12", "2026-12-10",
    ],
    "NFP 고용보고서": [
        "2026-01-09", "2026-02-06", "2026-03-06",
        "2026-04-03", "2026-05-08", "2026-06-05",
        "2026-07-10", "2026-08-07", "2026-09-04",
        "2026-10-02", "2026-11-06", "2026-12-04",
    ],
}

_MAJOR_RELEASE_KEYWORDS = [
    "consumer price","cpi","producer price","ppi",
    "employment situation","nonfarm","payroll",
    "gdp","gross domestic",
    "federal open market","fomc",
    "personal consumption","pce",
    "retail sales","industrial production",
    "initial claims","jobless",
]

def get_todays_major_releases(fred: Fred = None) -> tuple:
    """오늘~3일 이내 주요 경제지표 발표. (release_names, full_events)
    1차: FMP Economic Calendar API  2차: 하드코딩 폴백
    """
    today_str = datetime.now(_ET).strftime("%Y-%m-%d")
    to_str    = (datetime.now(_ET) + timedelta(days=3)).strftime("%Y-%m-%d")
    FMP_KEY_C = os.environ.get("FMP_API_KEY", "")
    FMP_BASE_C = "https://financialmodelingprep.com/stable"
    releases, full_events = [], []

    if FMP_KEY_C:
        try:
            r = requests.get(
                f"{FMP_BASE_C}/economic-calendar?from={today_str}&to={to_str}&apikey={FMP_KEY_C}",
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    for ev in data:
                        country = str(ev.get("country","") or "").upper()
                        if country not in ("US","USD","UNITED STATES",""):
                            continue
                        impact  = str(ev.get("impact","") or ev.get("importance","") or "").capitalize()
                        ev_name = str(ev.get("event","") or ev.get("name",""))
                        ev_date = str(ev.get("date",""))[:10]
                        if not ev_name:
                            continue
                        full_events.append({"date":ev_date,"event":ev_name,"impact":impact,
                                            "actual":ev.get("actual"),
                                            "estimate":ev.get("estimate"),
                                            "prior":ev.get("previous") or ev.get("prior")})
                        if impact.lower() in ("high","3") and ev_date == today_str:
                            releases.append(ev_name)
                    print(f"[INFO] FMP 캘린더: {len(full_events)}개, 오늘 고임팩트 {len(releases)}개")
                    if full_events:
                        return releases, full_events
        except Exception as e:
            print(f"[WARN] FMP 캘린더 실패: {e}")

    for event_name, dates in _HARDCODED_CALENDAR_2026.items():
        if today_str in dates:
            releases.append(event_name)
            full_events.append({"date":today_str,"event":event_name,"impact":"High",
                                 "actual":None,"estimate":None,"prior":None})
            print(f"[INFO] 하드코딩 캘린더: {event_name}")
    return releases, full_events


# ── 뉴스 수집 ─────────────────────────────────────────────────────────────────
def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def fetch_global_market_news() -> tuple[list, str, int]:
    browser_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    rss_sources = {
        "Yahoo Finance":       {"url": "https://finance.yahoo.com/news/rssindex", "weight": 1.0},
        "CNBC":                {"url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000", "weight": 0.9},
        "Google News Finance": {"url": "https://news.google.com/rss/search?q=finance+market+economy&hl=en-US&gl=US&ceid=US:en", "weight": 0.8},
        "MarketWatch":         {"url": "http://feeds.marketwatch.com/marketwatch/marketpulse/", "weight": 0.7},
    }
    all_news = []
    for src, cfg in rss_sources.items():
        try:
            resp = requests.get(cfg["url"], headers=browser_headers, timeout=8)
            if resp.status_code != 200:
                continue
            for entry in getattr(feedparser.parse(resp.content), "entries", []):
                title   = _clean_text(getattr(entry, "title", ""))
                summary = _clean_text(getattr(entry, "summary", "") or "")
                if not (title or summary):
                    continue
                parsed = getattr(entry, "published_parsed", None)
                pub_dt = datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else datetime.min.replace(tzinfo=timezone.utc)
                all_news.append({"title": title, "summary": summary,
                                  "published": str(getattr(entry,"published","") or "N/A"),
                                  "published_dt": pub_dt, "source": src, "weight": cfg["weight"]})
        except Exception:
            continue

    deduped = []
    for news in all_news:
        dup_idx = None
        for i, kept in enumerate(deduped):
            if SequenceMatcher(None, news["title"].lower(), kept["title"].lower()).ratio() >= 0.7:
                dup_idx = i; break
        if dup_idx is None:
            deduped.append(news)
        else:
            k = deduped[dup_idx]
            if news["weight"] > k["weight"] or (news["weight"] == k["weight"] and news["published_dt"] > k["published_dt"]):
                deduped[dup_idx] = news

    ranked = sorted(deduped, key=lambda x: (x["weight"], x["published_dt"]), reverse=True)[:30]
    chunks = [
        f"- [{item['source']}] {item['title']} | {item['summary'][:80]}"
        for item in ranked
    ]
    for item in ranked:
        item.pop("published_dt", None)
    return ranked, "\n".join(chunks), len(all_news)


# ── 거시지표 수집 (앱의 Daily Risk Gauge 5가지 신호와 동일) ──────────────────
def fetch_macro_context(fred: Fred) -> str:
    """앱의 compute_daily_risk_gauge와 동일한 5가지 선행 신호 수집."""
    lines = []
    signals_summary = []

    # ── 신호 1: VIX 방향 ─────────────────────────────────────────────────────
    vix_close = None
    try:
        vix_s = fred.get_series("VIXCLS")
        vix_s = pd.to_numeric(vix_s, errors="coerce").dropna().tail(30)
        if len(vix_s) >= 10:
            vix_now = float(vix_s.iloc[-1])
            vix_trend = vix_now - float(vix_s.iloc[-6]) if len(vix_s) >= 6 else 0
            vix_20d = float(vix_s.tail(20).mean())
            vix_alert = vix_trend > 2 or vix_now > vix_20d * 1.15
            vix_close = vix_s
            status = "⚠️ 상승전환" if vix_alert else "✅ 정상"
            lines.append(f"- VIX: {vix_now:.1f} ({vix_trend:+.1f}/5일) [{status}]")
            signals_summary.append(f"VIX {'경고' if vix_alert else '정상'}")
    except Exception:
        lines.append("- VIX: 조회 실패")

    # ── 신호 2: 신용 스프레드 (HYG/LQD) ────────────────────────────────────
    try:
        FMP_KEY = os.environ.get("FMP_API_KEY", "")
        FMP_BASE = "https://financialmodelingprep.com/stable"
        def _fmp_hist(sym, limit=30):
            r = requests.get(f"{FMP_BASE}/historical-price-eod/full?symbol={sym}&limit={limit}&apikey={FMP_KEY}", timeout=8)
            data = r.json()
            rows = data.get("historical", data) if isinstance(data, dict) else data
            if not isinstance(rows, list) or not rows: return pd.Series(dtype=float)
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            return pd.to_numeric(df["close"], errors="coerce").dropna()
        hyg = _fmp_hist("HYG")
        lqd = _fmp_hist("LQD")
        if len(hyg) >= 6 and len(lqd) >= 6:
            spread_now = float(hyg.iloc[-1] / lqd.iloc[-1])
            spread_5d  = float(hyg.iloc[-6] / lqd.iloc[-6])
            spread_chg = (spread_now / spread_5d - 1) * 100
            alert = spread_chg < -0.5
            status = "⚠️ 축소(위험)" if alert else "✅ 정상"
            lines.append(f"- 신용 스프레드(HYG/LQD): {spread_chg:+.2f}%/5일 [{status}]")
            signals_summary.append(f"신용스프레드 {'경고' if alert else '정상'}")
    except Exception:
        lines.append("- 신용 스프레드: 조회 실패")

    # ── 신호 3: 대장주 모멘텀 (SPY, QQQ, NVDA, AAPL, MSFT) ─────────────────
    try:
        leaders = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]
        close_dict = {}
        for sym in leaders:
            s = _fmp_hist(sym, limit=30)
            if not s.empty:
                close_dict[sym] = s
        close_df = pd.DataFrame(close_dict).sort_index() if close_dict else pd.DataFrame()
        spy_s = pd.to_numeric(close_df["SPY"], errors="coerce").dropna()
        spy_5d = float((spy_s.iloc[-1]/spy_s.iloc[-6] - 1)*100) if len(spy_s) >= 6 else 0
        weak_count = 0
        checked = 0
        for ldr in ["QQQ", "NVDA", "AAPL", "MSFT"]:
            if ldr not in close_df.columns:
                continue
            s = pd.to_numeric(close_df[ldr], errors="coerce").dropna()
            if len(s) < 10:
                continue
            ret_5d = float((s.iloc[-1]/s.iloc[-6] - 1)*100) if len(s) >= 6 else 0
            ma20 = float(s.rolling(20, min_periods=10).mean().iloc[-1])
            below_ma20 = float(s.iloc[-1]) < ma20
            rel_strength = ret_5d - spy_5d
            if below_ma20 or rel_strength < -3:
                weak_count += 1
            checked += 1
        alert = weak_count >= 2
        status = f"⚠️ {weak_count}/{checked}개 약세" if alert else f"✅ {weak_count}/{checked}개 약세"
        lines.append(f"- 대장주 모멘텀: {status}")
        signals_summary.append(f"대장주 {'경고' if alert else '정상'}")
    except Exception:
        lines.append("- 대장주 모멘텀: 조회 실패")

    # ── 신호 4: 선물 프리마켓 방향 (ES=F / NQ=F) ───────────────────────────
    try:
        _fut_syms = ["ES=F", "NQ=F"]
        _fut_chgs = []
        _fut_lines = []
        for sym in _fut_syms:
            try:
                r_fut = requests.get(
                    f"{FMP_BASE}/quote/{sym}?apikey={FMP_KEY}", timeout=8
                )
                if r_fut.status_code == 200:
                    d_fut = r_fut.json()
                    item = d_fut[0] if isinstance(d_fut, list) and d_fut else d_fut
                    price = float(item.get("price") or item.get("lastPrice") or 0)
                    prev  = float(item.get("previousClose") or item.get("prevClose") or 0)
                    if prev > 0 and price > 0:
                        chg = (price / prev - 1) * 100
                        _fut_chgs.append(chg)
                        _fut_lines.append(f"{sym} {chg:+.2f}%")
            except Exception:
                pass
        if _fut_chgs:
            avg_chg = float(np.mean(_fut_chgs))
            fut_alert = avg_chg <= -0.5
            status = "⚠️ 갭다운우려" if fut_alert else "✅ 정상"
            lines.append(f"- 선물 프리마켓: {' / '.join(_fut_lines)} (평균 {avg_chg:+.2f}%) [{status}]")
            signals_summary.append(f"선물 {'경고' if fut_alert else '정상'}")
        else:
            lines.append("- 선물 프리마켓: 조회 실패")
    except Exception:
        lines.append("- 선물 프리마켓: 조회 실패")

    # ── 신호 5: 달러·금·국채 리스크오프 신호 ────────────────────────────────
    try:
        _safety = {"UUP": "달러", "GLD": "금", "IEF": "국채"}
        _safety_chgs = []
        _safety_lines = []
        for sym5, name5 in _safety.items():
            try:
                r5 = requests.get(
                    f"{FMP_BASE}/historical-price-eod/full?symbol={sym5}&limit=10&apikey={FMP_KEY}",
                    timeout=8
                )
                if r5.status_code == 200:
                    d5 = r5.json()
                    rows5 = d5.get("historical", d5) if isinstance(d5, dict) else d5
                    if isinstance(rows5, list) and len(rows5) >= 6:
                        df5 = pd.DataFrame(rows5)
                        df5["date"] = pd.to_datetime(df5["date"])
                        df5 = df5.set_index("date").sort_index()
                        s5 = pd.to_numeric(df5["close"], errors="coerce").dropna()
                        chg5 = float((s5.iloc[-1] / s5.iloc[-6] - 1) * 100) if len(s5) >= 6 else np.nan
                        if not np.isnan(chg5):
                            _safety_chgs.append((sym5, chg5))
                            _safety_lines.append(f"{name5} {chg5:+.1f}%")
            except Exception:
                pass
        if _safety_chgs:
            _rising = [s for s, c in _safety_chgs if c > 0.3]
            spy_5d_chg = 0.0
            try:
                r_spy2 = requests.get(
                    f"{FMP_BASE}/historical-price-eod/full?symbol=SPY&limit=10&apikey={FMP_KEY}",
                    timeout=8
                )
                if r_spy2.status_code == 200:
                    d_spy2 = r_spy2.json()
                    rows_spy2 = d_spy2.get("historical", d_spy2) if isinstance(d_spy2, dict) else d_spy2
                    if isinstance(rows_spy2, list) and len(rows_spy2) >= 6:
                        df_spy2 = pd.DataFrame(rows_spy2)
                        df_spy2["date"] = pd.to_datetime(df_spy2["date"])
                        df_spy2 = df_spy2.set_index("date").sort_index()
                        s_spy2 = pd.to_numeric(df_spy2["close"], errors="coerce").dropna()
                        spy_5d_chg = float((s_spy2.iloc[-1] / s_spy2.iloc[-6] - 1) * 100) if len(s_spy2) >= 6 else 0.0
            except Exception:
                pass
            riskoff_alert = len(_rising) >= 2 and spy_5d_chg < -0.5
            status_ro = "⚠️ 리스크오프" if riskoff_alert else ("🟡 안전자산강세" if len(_rising) >= 2 else "✅ 정상")
            lines.append(f"- 달러·금·국채: {' / '.join(_safety_lines)} [{status_ro}]")
            signals_summary.append(f"리스크오프 {'경고' if riskoff_alert else '정상'}")
        else:
            lines.append("- 달러·금·국채: 조회 실패")
    except Exception:
        lines.append("- 달러·금·국채: 조회 실패")


    # ── FRED 기준금리 + CPI (추가 컨텍스트) ────────────────────────────────
    try:
        rate = float(fred.get_series("FEDFUNDS").dropna().iloc[-1])
        lines.append(f"- 기준금리(Fed Funds): {rate:.2f}%")
    except Exception:
        pass
    try:
        cpi = fred.get_series("CPIAUCSL").dropna()
        if len(cpi) >= 13:
            yoy = (cpi.iloc[-1] / cpi.iloc[-13] - 1) * 100
            lines.append(f"- CPI YoY: {yoy:.2f}%")
    except Exception:
        pass

    # ── 종합 신호 요약 ───────────────────────────────────────────────────────
    warning_count = sum(1 for s in signals_summary if "경고" in s)
    risk_level = "🔴 HIGH RISK" if warning_count >= 3 else ("🟡 MEDIUM RISK" if warning_count >= 1 else "🟢 LOW RISK")
    lines.insert(0, f"[선행 신호 종합: {risk_level} | 경고 {warning_count}/5개 (VIX·신용·대장주·선물·리스크오프)]")

    return "\n".join(lines) if lines else "데이터 없음"


# ── Gemini DRG 예측 생성 ──────────────────────────────────────────────────────
def generate_drg_prediction(rss_news_text: str, macro_summary: str,
                             fred_releases: list[str], sector: str = "전체 시장",
                             full_events: list = None) -> str:
    client = genai.Client(api_key=GOOGLE_API_KEY)
    now_kst = datetime.now(_KST)
    now_et  = datetime.now(_ET)

    fred_section = ""
    _today_et = datetime.now(_ET).strftime("%Y-%m-%d")
    if full_events:
        high_today = [e for e in full_events
                      if e.get("impact","").lower() in ("high","3")
                      and e.get("date","") == _today_et]
        if high_today:
            ev_lines = []
            for ev in high_today[:4]:
                line = f"  - {ev['event']}"
                if ev.get("estimate") is not None:
                    line += f" (예상: {ev['estimate']}"
                    if ev.get("prior") is not None:
                        line += f" / 이전: {ev['prior']}"
                    line += ")"
                ev_lines.append(line)
            fred_section = (
                "\n⚠️ [오늘의 고임팩트 경제지표 (FMP Calendar)]\n"
                + "\n".join(ev_lines) + "\n"
                "→ 예상치 대비 실제값 방향으로 급변동 가능. 각 지표 예상치를 반드시 활용해 예측하세요.\n"
            )
    elif fred_releases:
        items = "\n".join(f"  - {r}" for r in fred_releases)
        fred_section = (
            f"\n⚠️ [오늘의 주요 경제지표]\n{items}\n"
            "→ 발표 전후 변동성 증가 예상.\n"
        )

    prompt = (
        "당신은 월가 수석 퀀트 전략가입니다. "
        "아래 Pre-Market 실시간 데이터를 바탕으로 오늘 미국 주식시장을 예측하세요.\n\n"
        f"[현재 시각] {now_kst.strftime('%Y-%m-%d %H:%M')} KST / {now_et.strftime('%H:%M')} ET (Pre-Market)\n"
        f"[분석 섹터] {sector}\n\n"
        f"[선행 신호 종합 + 선물·안전자산]\n{macro_summary}\n\n"
        f"[오늘의 주요 뉴스 (오버나잇 포함)]\n{rss_news_text}\n"
        f"{fred_section}\n"
        "---\n"
        "아래 4개 항목을 각각 작성하세요. "
        "반드시 위 수치(선물 %, VIX 값, 이벤트 예상치, 뉴스 종목명)를 직접 인용해 근거를 만드세요. "
        "일반론·빈말 금지.\n\n"
        "## 오늘 시장 방향 판단: [상승 우세 / 중립 / 하락 우세]\n\n"
        "**📊 핵심 근거** (선물 방향·VIX·이벤트 예상치 직접 인용, 2~3문장):\n\n"
        "**📰 뉴스 영향** (오버나잇 뉴스 중 오늘 시장 영향 종목/이슈, 2문장):\n\n"
        "**⚠️ 오늘 주목할 리스크** (구체적 수치/종목/이벤트 기반, 2가지):\n"
        "1. \n"
        "2. \n\n"
        "**🎯 실전 대응** (보유/매수/현금 각 1문장):\n"
        "- 보유 중: \n"
        "- 매수 타이밍 보는 중: \n"
        "- 현금 대기 중: \n\n"
        "*본 분석은 AI 참고용이며 투자 권유가 아닙니다.*"
    )

    _RETRY_WAITS = [10, 30, 60, 120]
    for attempt in range(5):
        try:
            cfg = genai_types.GenerateContentConfig(temperature=0.7, max_output_tokens=4096)
            response = client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt, config=cfg
            )
            result = str(getattr(response, "text", "") or "").strip()
            if result:
                return result
            raise ValueError("빈 응답")
        except Exception as e:
            wait = _RETRY_WAITS[min(attempt, len(_RETRY_WAITS)-1)]
            print(f"[WARN] Gemini 시도 {attempt+1}/5 실패: {e} → {wait}초 대기")
            if attempt < 4:
                time.sleep(wait)
    return ""


def extract_direction(text: str) -> str:
    if "상승 우세" in text:
        return "상승 우세"
    elif "하락 우세" in text:
        return "하락 우세"
    return "중립"


# ── Google Sheets 저장 ────────────────────────────────────────────────────────
def get_gspread_client():
    creds = Credentials.from_service_account_info(_gcp_info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)


def save_drg_to_sheet(pred_date: str, direction: str, sector: str,
                       bench_etf: str, spy_close: float, full_text: str) -> bool:
    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        try:
            ws = sh.worksheet(_DRG_PREDICTIONS_WORKSHEET)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=_DRG_PREDICTIONS_WORKSHEET, rows=2000, cols=len(_DRG_SHEET_COLS))
            ws.append_row(_DRG_SHEET_COLS, value_input_option="USER_ENTERED")

        row = [
            _ADMIN_USER_ID, pred_date, direction, sector, bench_etf,
            str(spy_close) if not np.isnan(spy_close) else "",
            full_text, "", "", "", ""
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        print(f"[OK] DRG 예측 저장 완료: {pred_date} | {direction}")
        return True
    except Exception as e:
        print(f"[ERROR] Sheets 저장 실패: {e}")
        return False


# ── HTML 이메일 ───────────────────────────────────────────────────────────────
def build_drg_email_html(full_text: str, direction: str, spy_close: float,
                          fred_releases: list[str], macro_summary: str,
                          full_events: list = None) -> str:
    now_et  = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
    now_kst = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")

    dir_color = {"상승 우세": "#16a34a", "중립": "#d97706", "하락 우세": "#dc2626"}.get(direction, "#6b7280")
    dir_emoji = {"상승 우세": "📈", "중립": "➡️", "하락 우세": "📉"}.get(direction, "❓")

    _ACTION_MAP_EMAIL = {
        "fomc":"신규 매수 자제. 발표 전후 변동성 2배 구간.",
        "federal reserve":"연준 발언 매파 시 기술주 급락 가능.",
        "cpi":"예상 상회→금리 우려·성장주 압박. 예상 하회→랠리 가능.",
        "nonfarm":"고용 호조=금리 우려. 고용 부진=침체 우려.",
        "payroll":"고용 호조=금리 우려. 고용 부진=침체 우려.",
        "pce":"예상 상회→금리 인하 지연, 성장주 부담.",
        "gdp":"예상 하회→침체 우려. 예상 상회→과열 우려.",
        "ism":"50 이하=수축. 하회 시 산업재 주의.",
        "pmi":"50 이하 수축. 예상 하회 시 경기 우려.",
        "retail sales":"급감 시 소비 위축·경기 둔화 신호.",
        "initial claims":"급증 시 고용 악화 선행 신호.",
    }
    def _get_act(n):
        nl = n.lower()
        for k,v in _ACTION_MAP_EMAIL.items():
            if k in nl: return v
        return "발표 전후 변동성 주의."

    _today_et_email = datetime.now(_ET).strftime("%Y-%m-%d")
    fred_html = ""
    if full_events:
        high_today_e = [e for e in full_events
                        if e.get("impact","").lower() in ("high","3")
                        and e.get("date","") == _today_et_email]
        if high_today_e:
            ev_cards = ""
            for ev in high_today_e[:4]:
                ev_ctx = ""
                if ev.get("estimate") is not None:
                    ev_ctx += f" | 예상: <b>{ev['estimate']}</b>"
                if ev.get("prior") is not None:
                    ev_ctx += f" / 이전: <b>{ev['prior']}</b>"
                ev_cards += (
                    f'<div style="border-left:3px solid #ea580c;padding:6px 10px;'
                    f'margin:6px 0;background:#451a03;border-radius:4px;">'
                    f'<div style="color:#fed7aa;font-weight:700;font-size:13px;">'
                    f'{ev["event"]} ({ev["date"]}){ev_ctx}</div>'
                    f'<div style="color:#fb923c;font-size:12px;margin-top:3px;">'
                    f'→ {_get_act(ev["event"])}</div></div>'
                )
            fred_html = (
                '<div style="background:#3b1414;border-radius:8px;padding:14px 16px;'
                'margin-bottom:16px;border:1px solid #ea580c;">'
                '<div style="font-weight:700;color:#fed7aa;margin-bottom:8px;">'
                '⚠️ 오늘의 고임팩트 경제지표 · 행동 지침</div>'
                + ev_cards + '</div>'
            )
    if not fred_html and fred_releases:
        items = "".join(f"<li>{r}</li>" for r in fred_releases)
        fred_html = (
            '<div style="background:#7c2d12;border-radius:8px;padding:12px 16px;'
            'margin-bottom:16px;border:1px solid #ea580c;">'
            '<div style="font-weight:700;color:#fed7aa;">⚠️ 오늘의 주요 경제지표 발표</div>'
            f'<ul style="color:#fdba74;margin:8px 0 0 0;padding-left:18px;font-size:13px;">{items}</ul>'
            '<div style="color:#fb923c;font-size:12px;margin-top:6px;">→ 발표 전후 변동성 증가 예상.</div>'
            '</div>'
        )

    spy_str = f"${spy_close:.2f}" if not np.isnan(spy_close) else "N/A"



    # full_text 마크다운 → HTML 간단 변환
    text_html = full_text.replace("\n", "<br>")
    text_html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text_html)
    text_html = re.sub(r"## (.*?)<br>", r"<h3 style='color:#f1f5f9;margin:16px 0 8px;'>\1</h3>", text_html)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;">

  <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #334155;">
    <div style="font-size:22px;font-weight:800;color:#60a5fa;">🚨 Quant Terminal</div>
    <div style="font-size:14px;color:#94a3b8;margin-top:4px;">DRG 시장 예측 리포트 · Pre-Market 8AM</div>
    <div style="font-size:13px;color:#64748b;margin-top:6px;">{now_et} &nbsp;|&nbsp; {now_kst}</div>
  </div>

  <!-- 방향 판단 -->
  <div style="background:{dir_color}22;border:2px solid {dir_color};border-radius:12px;padding:20px;margin-bottom:16px;text-align:center;">
    <div style="font-size:36px;">{dir_emoji}</div>
    <div style="font-size:24px;font-weight:800;color:{dir_color};margin-top:8px;">{direction}</div>
    <div style="font-size:13px;color:#94a3b8;margin-top:6px;">SPY 기준가: {spy_str}</div>
  </div>

  {fred_html}

  <!-- 거시지표 요약 -->
  <div style="background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:8px;">📊 주요 거시지표</div>
    <div style="font-size:13px;color:#94a3b8;font-family:monospace;line-height:1.8;">{macro_summary.replace(chr(10),'<br>')}</div>
  </div>

  <!-- 전체 분석 -->
  <div style="background:#1e293b;border-radius:10px;padding:16px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:10px;">🤖 AI 전체 분석</div>
    <div style="font-size:13px;color:#cbd5e1;line-height:1.8;">{text_html}</div>
  </div>

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
    print(f"[START] DRG 예측 시작: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    if not is_market_open_today():
        print("[SKIP] 오늘은 NYSE 휴장일. DRG 예측 스킵.")
        sys.exit(0)

    fred = Fred(api_key=FRED_API_KEY)
    fred_releases, full_events = get_todays_major_releases(fred)
    if fred_releases:
        print(f"[INFO] 오늘의 주요 경제지표: {fred_releases}")
    if full_events:
        print(f"[INFO] FMP 이벤트 캘린더: {len(full_events)}개")

    print("[STEP 1] 뉴스 수집 중...")
    _, rss_news_text, raw_count = fetch_global_market_news()
    print(f"[INFO] 뉴스 {raw_count}건 수집 완료")

    print("[STEP 2] 거시지표 수집 중...")
    macro_summary = fetch_macro_context(fred)
    print(f"[INFO] 거시지표:\n{macro_summary}")

    print("[STEP 3] Gemini DRG 예측 생성 중...")
    full_text = generate_drg_prediction(rss_news_text, macro_summary, fred_releases, full_events=full_events)
    if not full_text:
        print("[ERROR] DRG 예측 생성 실패.")
        sys.exit(1)

    direction = extract_direction(full_text)
    print(f"[INFO] 예측 방향: {direction}")

    # SPY 현재가 — FMP
    try:
        FMP_KEY_MAIN = os.environ.get("FMP_API_KEY", "")
        spy_r = requests.get(f"https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=SPY&limit=2&apikey={FMP_KEY_MAIN}", timeout=8)
        spy_data = spy_r.json()
        spy_rows = spy_data.get("historical", spy_data) if isinstance(spy_data, dict) else spy_data
        spy_close = float(spy_rows[0]["close"]) if isinstance(spy_rows, list) and spy_rows else np.nan
    except Exception:
        spy_close = np.nan

    print("[STEP 4] Sheets 저장 중...")
    pred_date = datetime.now(_ET).strftime("%Y-%m-%d")
    save_drg_to_sheet(pred_date, direction, "전체 시장", "SPY", spy_close, full_text)

    print("[STEP 5] 이메일 발송 중...")
    fred_tag = " ⚠️FRED발표" if fred_releases else ""
    subject = f"🚨 [DRG 예측] {direction} · {datetime.now(_ET).strftime('%m/%d')}{fred_tag}"
    html_body = build_drg_email_html(full_text, direction, spy_close, fred_releases, macro_summary, full_events=full_events)
    send_email(subject, html_body)

    print(f"[DONE] 완료: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
