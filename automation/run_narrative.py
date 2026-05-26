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
_ADMIN_USER_ID       = "admin"

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


# ── FRED 경제지표 캘린더 ──────────────────────────────────────────────────────
_MAJOR_RELEASE_KEYWORDS = [
    "consumer price", "cpi", "producer price", "ppi",
    "employment situation", "nonfarm", "payroll",
    "gdp", "gross domestic",
    "federal open market", "fomc",
    "personal consumption", "pce",
    "retail sales", "industrial production",
    "initial claims", "jobless",
]

def get_todays_major_releases(fred: Fred) -> list[str]:
    """오늘 날짜의 주요 경제지표 발표 목록 반환 (FRED REST API 직접 호출)."""
    today_str = datetime.now(_ET).strftime("%Y-%m-%d")
    releases = []
    try:
        # 1단계: 오늘 발표 예정인 release_id 목록 조회
        url = "https://api.stlouisfed.org/fred/releases/dates"
        params = {
            "api_key": FRED_API_KEY,
            "realtime_start": today_str,
            "realtime_end": today_str,
            "file_type": "json",
            "include_release_dates_with_no_data": "true",
            "limit": 100,
        }
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            print(f"[WARN] FRED releases/dates API 오류: {resp.status_code}")
            return []
        data = resp.json()
        release_dates = data.get("release_dates", [])
        if not release_dates:
            return []

        # 2단계: release_id별 이름 조회 (중복 제거)
        seen_ids = set()
        for rd in release_dates:
            rid = rd.get("release_id")
            if not rid or rid in seen_ids:
                continue
            seen_ids.add(rid)
            try:
                r2 = requests.get(
                    "https://api.stlouisfed.org/fred/release",
                    params={"api_key": FRED_API_KEY, "release_id": rid, "file_type": "json"},
                    timeout=8,
                )
                if r2.status_code != 200:
                    continue
                name = r2.json().get("releases", [{}])[0].get("name", "")
                if any(kw in str(name).lower() for kw in _MAJOR_RELEASE_KEYWORDS):
                    releases.append(str(name).strip())
            except Exception:
                continue
    except Exception as e:
        print(f"[WARN] FRED 캘린더 조회 실패: {e}")
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


def fetch_global_market_news():
    """멀티소스 RSS 수집 + 중복 제거 + 가중치 정렬. (top_news, context_text, raw_count)"""
    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    rss_sources = {
        "Yahoo Finance":       {"url": "https://finance.yahoo.com/news/rssindex", "weight": 1.0},
        "CNBC":                {"url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000", "weight": 0.9},
        "Google News Finance": {"url": "https://news.google.com/rss/search?q=finance+market+economy&hl=en-US&gl=US&ceid=US:en", "weight": 0.8},
        "MarketWatch":         {"url": "http://feeds.marketwatch.com/marketwatch/marketpulse/", "weight": 0.7},
    }

    all_news = []
    for source_name, cfg in rss_sources.items():
        try:
            resp = requests.get(cfg["url"], headers=browser_headers, timeout=8)
            if resp.status_code != 200:
                continue
            feed = feedparser.parse(resp.content)
            for entry in getattr(feed, "entries", []):
                title   = _clean_text(getattr(entry, "title", ""))
                summary = _clean_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
                if not (title or summary):
                    continue
                published_raw = str(getattr(entry, "published", "") or "").strip()
                parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
                if parsed:
                    try:
                        pub_dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pub_dt = datetime.min.replace(tzinfo=timezone.utc)
                else:
                    pub_dt = datetime.min.replace(tzinfo=timezone.utc)
                all_news.append({
                    "title": title, "summary": summary,
                    "published": published_raw or "N/A",
                    "published_dt": pub_dt,
                    "source": source_name, "weight": cfg["weight"],
                })
        except Exception:
            continue

    # 중복 제거
    deduped = []
    for news in all_news:
        dup_idx = None
        for i, kept in enumerate(deduped):
            if SequenceMatcher(None, news["title"].lower(), kept["title"].lower()).ratio() >= 0.7:
                dup_idx = i
                break
        if dup_idx is None:
            deduped.append(news)
        else:
            k = deduped[dup_idx]
            if news["weight"] > k["weight"] or (news["weight"] == k["weight"] and news["published_dt"] > k["published_dt"]):
                deduped[dup_idx] = news

    ranked = sorted(deduped, key=lambda x: (x["weight"], x["published_dt"]), reverse=True)
    top = ranked[:50]

    chunks = []
    for i, item in enumerate(top, 1):
        chunks.append(
            f"[{i}] Source: {item['source']} (Weight: {item['weight']:.1f})\n"
            f"Published: {item['published']}\n"
            f"Title: {item['title']}\n"
            f"Summary: {item['summary']}"
        )
    context_text = "\n\n".join(chunks).strip()
    for item in top:
        item.pop("published_dt", None)
    return top, context_text, len(all_news)


# ── Gemini 내러티브 생성 ───────────────────────────────────────────────────────
def generate_market_narrative(news_text: str, fred_alert: str = "") -> dict:
    """뉴스 텍스트 → Gemini → 내러티브 JSON."""
    client = genai.Client(api_key=GOOGLE_API_KEY)

    fred_section = fred_alert if fred_alert else ""

    prompt = f"""당신은 월가 수석 퀀트 전략가입니다.
아래 뉴스와 경제지표 정보를 종합 분석하여 지정된 JSON 스키마 그대로만 응답하세요.

핵심 원칙:
- 뉴스(정성) + 가격 모멘텀(정량)이 동시에 확인된 종목만 Winners로 선정
- 뉴스만 좋고 가격이 안 받쳐주는 종목은 emerging으로만 분류
- 각 theme의 winners는 반드시 3~6개 티커

중요 규칙:
1) 반드시 순수 JSON 텍스트만 출력 (```json 같은 마크다운 금지)
2) 모든 키를 빠짐없이 포함
3) themes는 최소 3개 이상 생성
4) winners/emerging은 티커를 쉼표로 구분한 문자열
5) 각 theme의 expanding_to는 반드시 객체 배열(list)이어야 함
6) expanding_to의 각 객체는 반드시 "stage"와 "expected_tickers" 키를 포함
7) expected_tickers는 각 stage마다 반드시 2~4개 티커를 쉼표 구분 문자열로 작성
8) momentum_note: 반드시 "강함", "보통", "약함" 셋 중 하나만 출력
9) 결과는 반드시 한국어로, 금융 전문 용어를 사용하여 가장 자연스럽게 작성
{fred_section}

[뉴스 데이터]
{news_text}

[출력 JSON 스키마]
{{
  "regime": {{
    "risk": "Risk On 또는 Risk Off",
    "growth_value": "Growth 선호 또는 Value 선호",
    "liquidity": "Expanding 또는 Tightening"
  }},
  "themes": [
    {{
      "title": "테마명 (예: AI Capex Expansion)",
      "driver": "무엇이 이 테마를 촉발했는가?",
      "winners": "정량+정성 모두 확인된 수혜주 (예: NVDA, MSFT, SOXX)",
      "emerging": "뉴스 모멘텀은 있으나 가격 확인 필요 종목 (예: ARM, MRVL)",
      "momentum_note": "강함/보통/약함 중 하나만 선택",
      "expanding_to": [
        {{"stage": "기업용 AI 솔루션", "expected_tickers": "CRM, NOW, WDAY"}},
        {{"stage": "AI 기반 사이버 보안", "expected_tickers": "CRWD, PANW, FTNT"}}
      ],
      "risk": "이 테마가 무너질 수 있는 위험 요인"
    }}
  ],
  "rotation": "과열 섹터 -> 수혜 섹터 플로우 요약",
  "top_quant_picks": "내러티브와 일치하는 최우선 종목 3~5개 (쉼표 구분)",
  "summary": "월가 퀀트 리포트 스타일 전체 시장 핵심 요약"
}}
You MUST respond ONLY with a valid JSON object. No markdown tags, no greetings."""

    for attempt in range(5):
        try:
            cfg = genai_types.GenerateContentConfig(temperature=0.3, top_p=1, max_output_tokens=8192)
            response = client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt, config=cfg
            )
            raw = str(getattr(response, "text", "") or "").strip()
            raw = re.sub(r"^```json", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"^```", "", raw.strip()).rstrip("`").strip()
            return json.loads(raw)
        except Exception as e:
            wait = [10, 30, 60, 120][min(attempt, 3)]
            print(f"[WARN] Gemini 시도 {attempt+1}/5 실패: {e} → {wait}초 대기")
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


def save_narrative_to_sheet(analysis: dict, news_count: int, fred_releases: list[str]) -> bool:
    """내러티브 결과를 Narratives 시트에 저장."""
    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        try:
            ws = sh.worksheet(_NARRATIVES_WORKSHEET)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=_NARRATIVES_WORKSHEET, rows=3000, cols=7)
            ws.append_row(["ID", "Date", "Category", "Title", "Content", "Winners", "Emerging"],
                          value_input_option="USER_ENTERED")

        now_kst = datetime.now(_KST)
        date_str = now_kst.strftime("%Y-%m-%d %H:%M KST")
        session = "Pre-Market" if now_kst.hour < 9 else ("After-Close" if now_kst.hour >= 17 else "Mid-Session")

        # Winners / Emerging 추출
        winners_set, emerging_set = set(), set()
        for theme in analysis.get("themes", []):
            for t in str(theme.get("winners", "")).split(","):
                t = t.strip().upper()
                if t: winners_set.add(t)
            for t in str(theme.get("emerging", "")).split(","):
                t = t.strip().upper()
                if t: emerging_set.add(t)

        fred_note = f" [FRED: {', '.join(fred_releases[:3])}]" if fred_releases else ""
        summary = str(analysis.get("summary", ""))[:500]
        regime  = analysis.get("regime", {})
        title   = f"[{session}] {regime.get('risk','?')} | {regime.get('growth_value','?')}{fred_note}"

        row = [
            _ADMIN_USER_ID,
            date_str,
            "자동생성",
            title,
            json.dumps(analysis, ensure_ascii=False),
            ", ".join(sorted(winners_set)),
            ", ".join(sorted(emerging_set)),
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        print(f"[OK] Sheets 저장 완료: {title}")
        return True
    except Exception as e:
        print(f"[ERROR] Sheets 저장 실패: {e}")
        traceback.print_exc()
        return False


# ── HTML 이메일 생성 ───────────────────────────────────────────────────────────
def build_email_html(analysis: dict, news_count: int, fred_releases: list[str], is_market_day: bool) -> str:
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

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;">

  <!-- 헤더 -->
  <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #334155;">
    <div style="font-size:22px;font-weight:800;color:#60a5fa;">📰 Quant Terminal</div>
    <div style="font-size:14px;color:#94a3b8;margin-top:4px;">시장 내러티브 자동 분석 리포트</div>
    <div style="margin-top:8px;font-size:13px;color:#64748b;">{now_et} &nbsp;|&nbsp; {now_kst} &nbsp;|&nbsp; {market_badge}</div>
    <div style="font-size:12px;color:#64748b;margin-top:4px;">뉴스 {news_count}건 수집 · Gemini 2.5 Flash 분석</div>
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
    top_news, context_text, raw_count = fetch_global_market_news()
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

    # 이메일 발송
    print("[STEP 4] 이메일 발송 중...")
    now_et = datetime.now(_ET)
    session_label = "Pre-Market 8AM" if now_et.hour < 12 else "After-Close 5PM"
    fred_tag = " ⚠️FRED발표" if fred_releases else ""
    subject = f"📰 [{session_label}] 시장 내러티브 리포트 {now_et.strftime('%m/%d')}{fred_tag}"

    html_body = build_email_html(analysis, raw_count, fred_releases, market_day)
    send_email(subject, html_body)

    print(f"[DONE] 완료: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
