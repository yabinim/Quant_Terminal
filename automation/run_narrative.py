"""
run_narrative.py (v2)
─────────────────────
app.py와 완전히 동일한 형식으로 Narratives 시트에 저장.
핵심: record 구조 = {saved_at, session_label, language, analysis}
      Content = json.dumps(record)
      Date    = KST YYYY-MM-DD HH:MM:SS
      Category = "market_narrative"
      Title   = 첫 번째 테마 제목
"""

import os, sys, json, re, time, smtplib, traceback
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests, feedparser
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

# ── 타임존 ────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")

# ── 상수 ─────────────────────────────────────────────────────────────────────
_SPREADSHEET_TITLE    = "Quant_DB"
_NARRATIVES_WORKSHEET = "Narratives"
_USER_ID              = "yab"

_NYSE_HOLIDAYS = {
    "2025-01-01","2025-01-20","2025-02-17","2025-04-18",
    "2025-05-26","2025-06-19","2025-07-04","2025-09-01",
    "2025-11-27","2025-12-25",
    "2026-01-01","2026-01-19","2026-02-16","2026-04-03",
    "2026-05-25","2026-06-19","2026-07-03","2026-09-07",
    "2026-11-26","2026-12-25",
}

def is_market_open_today() -> bool:
    et_now = datetime.now(_ET)
    return et_now.weekday() < 5 and et_now.strftime("%Y-%m-%d") not in _NYSE_HOLIDAYS

# ── 세션 라벨 (app.py와 동일 로직) ───────────────────────────────────────────
def _session_label_for_utc(dt_utc: datetime) -> str:
    dt_et = dt_utc.astimezone(_ET)
    m = dt_et.hour * 60 + dt_et.minute
    if 240 <= m <= 569:  return "🌅 Pre-market Prep"
    if 570 <= m <= 960:  return "🟢 Market Hours Analysis"
    if 961 <= m <= 1200: return "🔔 Daily Recap (Post-Market)"
    return "🌙 Overnight Strategy"

# ── KST 날짜 문자열 (app.py _narrative_now_kst_string과 동일) ─────────────────
def _now_kst_string(dt_utc: datetime) -> str:
    return dt_utc.astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S")

# ── FRED 캘린더 ───────────────────────────────────────────────────────────────
_MAJOR_KEYWORDS = [
    "consumer price","cpi","producer price","ppi",
    "employment situation","nonfarm","payroll",
    "gdp","gross domestic","federal open market","fomc",
    "personal consumption","pce","retail sales",
    "industrial production","initial claims","jobless",
]

def get_todays_major_releases(fred: Fred) -> list[str]:
    today_str = datetime.now(_ET).strftime("%Y-%m-%d")
    try:
        df = fred.get_releases_for_date(today_str)
        if df is None or df.empty: return []
        name_col = next((c for c in df.columns if "name" in c.lower()), None)
        if not name_col: return []
        return [str(n).strip() for n in df[name_col].dropna()
                if any(kw in str(n).lower() for kw in _MAJOR_KEYWORDS)]
    except Exception as e:
        print(f"[WARN] FRED 캘린더 조회 실패: {e}")
        return []

# ── 뉴스 수집 ─────────────────────────────────────────────────────────────────
def _clean(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(raw or ""))).strip()

def fetch_global_market_news():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    sources = {
        "Yahoo Finance":       ("https://finance.yahoo.com/news/rssindex", 1.0),
        "CNBC":                ("https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000", 0.9),
        "Google News Finance": ("https://news.google.com/rss/search?q=finance+market+economy&hl=en-US&gl=US&ceid=US:en", 0.8),
        "MarketWatch":         ("http://feeds.marketwatch.com/marketwatch/marketpulse/", 0.7),
    }
    all_news = []
    for src, (url, w) in sources.items():
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200: continue
            for e in getattr(feedparser.parse(r.content), "entries", []):
                title   = _clean(getattr(e, "title", ""))
                summary = _clean(getattr(e, "summary", "") or "")
                if not (title or summary): continue
                p = getattr(e, "published_parsed", None)
                pub_dt = datetime(*p[:6], tzinfo=timezone.utc) if p else datetime.min.replace(tzinfo=timezone.utc)
                all_news.append({"title": title, "summary": summary,
                                  "published": str(getattr(e,"published","") or "N/A"),
                                  "published_dt": pub_dt, "source": src, "weight": w})
        except Exception: continue

    deduped = []
    for news in all_news:
        dup = next((i for i,k in enumerate(deduped)
                    if SequenceMatcher(None, news["title"].lower(), k["title"].lower()).ratio() >= 0.7), None)
        if dup is None: deduped.append(news)
        else:
            k = deduped[dup]
            if news["weight"] > k["weight"] or (news["weight"]==k["weight"] and news["published_dt"]>k["published_dt"]):
                deduped[dup] = news

    ranked = sorted(deduped, key=lambda x:(x["weight"],x["published_dt"]), reverse=True)[:50]
    chunks = [f"[{i}] Source: {n['source']}\nPublished: {n['published']}\nTitle: {n['title']}\nSummary: {n['summary']}"
              for i,n in enumerate(ranked,1)]
    for n in ranked: n.pop("published_dt", None)
    return ranked, "\n\n".join(chunks), len(all_news)

# ── Gemini 내러티브 생성 ──────────────────────────────────────────────────────
def generate_market_narrative(news_text: str, fred_alert: str = "") -> dict:
    client = genai.Client(api_key=GOOGLE_API_KEY)
    prompt = f"""당신은 월가 수석 퀀트 전략가입니다.
아래 뉴스를 종합 분석하여 지정된 JSON 스키마 그대로만 응답하세요.

핵심 원칙:
- 뉴스(정성) + 가격 모멘텀(정량)이 동시에 확인된 종목만 Winners로 선정
- 뉴스만 좋고 가격이 안 받쳐주는 종목은 emerging으로만 분류
- 각 theme의 winners는 반드시 3~6개 티커

중요 규칙:
1) 반드시 순수 JSON 텍스트만 출력 (마크다운 금지)
2) 모든 키를 빠짐없이 포함
3) themes는 최소 3개 이상
4) winners/emerging은 티커를 쉼표로 구분한 문자열
5) expanding_to는 반드시 객체 배열
6) momentum_note: "강함"/"보통"/"약함" 중 하나만
7) 결과는 반드시 한국어로
{fred_alert}

[뉴스 데이터]
{news_text}

[출력 JSON 스키마]
{{
  "source": "market_narrative",
  "regime": {{"risk": "Risk On 또는 Risk Off", "growth_value": "Growth 선호 또는 Value 선호", "liquidity": "Expanding 또는 Tightening"}},
  "themes": [
    {{
      "title": "테마명",
      "driver": "촉발 원인",
      "winners": "NVDA, MSFT, SOXX",
      "emerging": "ARM, MRVL",
      "momentum_note": "강함",
      "expanding_to": [{{"stage": "기업용 AI", "expected_tickers": "CRM, NOW"}}],
      "risk": "위험 요인"
    }}
  ],
  "rotation": "과열 섹터 -> 수혜 섹터",
  "top_quant_picks": "NVDA, MSFT, SOXX",
  "summary": "전체 시장 핵심 요약"
}}
You MUST respond ONLY with a valid JSON object. No markdown."""

    for attempt in range(3):
        try:
            cfg = genai_types.GenerateContentConfig(temperature=0.3, top_p=1, max_output_tokens=8192)
            resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt, config=cfg)
            raw = str(getattr(resp, "text", "") or "").strip()
            raw = re.sub(r"^```json|^```", "", raw, flags=re.IGNORECASE).rstrip("`").strip()
            result = json.loads(raw)
            # source 키 보장
            if "source" not in result:
                result["source"] = "market_narrative"
            return result
        except Exception as e:
            print(f"[WARN] Gemini 시도 {attempt+1}/3 실패: {e}")
            if attempt < 2: time.sleep([5,15][attempt])
    return {}

# ── 티커 파싱 ─────────────────────────────────────────────────────────────────
def parse_tickers(text: str) -> list[str]:
    return [t.strip().upper() for t in re.split(r"[,\s]+", str(text or "")) if re.match(r"^[A-Z]{1,5}$", t.strip().upper())]

def winners_from_analysis(analysis: dict) -> list[str]:
    out, seen = [], set()
    for theme in analysis.get("themes", []):
        for t in parse_tickers(theme.get("winners", "")):
            if t not in seen: seen.add(t); out.append(t)
    return out

def emerging_from_analysis(analysis: dict) -> list[str]:
    out, seen = [], set()
    for theme in analysis.get("themes", []):
        for flow in theme.get("expanding_to", []):
            for t in parse_tickers(flow.get("expected_tickers", "")):
                if t not in seen: seen.add(t); out.append(t)
    return out

# ── Google Sheets 저장 (app.py와 완전히 동일한 형식) ─────────────────────────
def get_gspread_client():
    creds = Credentials.from_service_account_info(_gcp_info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)

def save_narrative_to_sheet(analysis: dict, fred_releases: list[str]) -> bool:
    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        try:
            ws = sh.worksheet(_NARRATIVES_WORKSHEET)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=_NARRATIVES_WORKSHEET, rows=3000, cols=7)
            ws.append_row(["ID","Date","Category","Title","Content","Winners","Emerging"],
                          value_input_option="USER_ENTERED")

        now_utc = datetime.now(timezone.utc)

        # ── app.py append_narrative_history_record 와 완전히 동일한 record 구조 ──
        session_label = _session_label_for_utc(now_utc)
        record = {
            "saved_at":      now_utc.isoformat(),
            "session_label": session_label,
            "language":      "ko",
            "analysis":      analysis,
        }

        # ── app.py _narrative_record_to_sheet_row 와 동일한 변환 ──
        date_kst = _now_kst_string(now_utc)          # YYYY-MM-DD HH:MM:SS
        category = str(analysis.get("source") or "market_narrative")

        # 제목: 첫 번째 테마 제목
        themes = analysis.get("themes", [])
        if themes and isinstance(themes[0], dict):
            title = str(themes[0].get("title", "") or "").strip() or "시장 내러티브 스냅샷"
        else:
            title = "시장 내러티브 스냅샷"
        if len(title) > 500: title = title[:497] + "..."

        content  = json.dumps(record, ensure_ascii=False)   # record 전체를 JSON으로
        w_csv    = ",".join(winners_from_analysis(analysis))
        e_csv    = ",".join(emerging_from_analysis(analysis))

        row = [_USER_ID, date_kst, category, title, content, w_csv, e_csv]
        ws.append_row(row, value_input_option="USER_ENTERED")
        print(f"[OK] Sheets 저장 완료: {title} | {session_label}")
        return True
    except Exception as e:
        print(f"[ERROR] Sheets 저장 실패: {e}")
        traceback.print_exc()
        return False

# ── 이메일 ────────────────────────────────────────────────────────────────────
def build_email_html(analysis: dict, news_count: int, fred_releases: list[str], market_day: bool) -> str:
    now_et  = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
    now_kst = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
    regime  = analysis.get("regime", {})
    risk    = regime.get("risk", "N/A")
    gv      = regime.get("growth_value", "N/A")
    liq     = regime.get("liquidity", "N/A")
    risk_color = "#16a34a" if "On" in risk else "#dc2626"

    themes_html = ""
    for i, theme in enumerate(analysis.get("themes", []), 1):
        mom = str(theme.get("momentum_note", "보통"))
        mom_color = {"강함":"#16a34a","보통":"#d97706","약함":"#dc2626"}.get(mom,"#6b7280")
        themes_html += f"""
        <div style="background:#1e293b;border-radius:8px;padding:14px 16px;margin-bottom:12px;border-left:4px solid {mom_color};">
          <div style="font-size:15px;font-weight:700;color:#f1f5f9;">{i}. {theme.get('title','?')}
            <span style="font-size:12px;color:{mom_color};margin-left:8px;">모멘텀 {mom}</span></div>
          <div style="font-size:13px;color:#94a3b8;margin-top:6px;">📌 {theme.get('driver','')}</div>
          <div style="margin-top:8px;font-size:13px;">
            <span style="color:#34d399;">✅ Winners: {theme.get('winners','')}</span><br>
            <span style="color:#60a5fa;">🔍 Emerging: {theme.get('emerging','')}</span>
          </div>
          <div style="font-size:12px;color:#f87171;margin-top:6px;">⚠️ Risk: {theme.get('risk','')}</div>
        </div>"""

    fred_html = ""
    if fred_releases:
        items = "".join(f"<li>{r}</li>" for r in fred_releases)
        fred_html = f"""<div style="background:#7c2d12;border-radius:8px;padding:12px 16px;margin-bottom:16px;border:1px solid #ea580c;">
          <div style="font-weight:700;color:#fed7aa;">⚠️ 오늘의 주요 경제지표 발표 (FRED)</div>
          <ul style="color:#fdba74;margin:8px 0 0;padding-left:18px;font-size:13px;">{items}</ul>
          <div style="color:#fb923c;font-size:12px;margin-top:6px;">→ 발표 전후 변동성 증가 예상.</div></div>"""

    market_badge = ('<span style="background:#16a34a;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;">📈 장 열리는 날</span>'
                    if market_day else
                    '<span style="background:#6b7280;color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;">🔒 장 닫힌 날</span>')

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;">
  <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #334155;">
    <div style="font-size:22px;font-weight:800;color:#60a5fa;">📰 Quant Terminal</div>
    <div style="font-size:14px;color:#94a3b8;margin-top:4px;">시장 내러티브 자동 분석 리포트</div>
    <div style="font-size:13px;color:#64748b;margin-top:6px;">{now_et} | {now_kst} | {market_badge}</div>
    <div style="font-size:12px;color:#64748b;margin-top:4px;">뉴스 {news_count}건 · Gemini 2.5 Flash</div>
  </div>
  <div style="background:#1e293b;border-radius:10px;padding:16px;margin-bottom:16px;display:flex;gap:12px;flex-wrap:wrap;">
    <div style="flex:1;text-align:center;padding:10px;background:#0f172a;border-radius:8px;">
      <div style="font-size:11px;color:#64748b;">RISK MODE</div>
      <div style="font-size:18px;font-weight:700;color:{risk_color};margin-top:4px;">{risk}</div>
    </div>
    <div style="flex:1;text-align:center;padding:10px;background:#0f172a;border-radius:8px;">
      <div style="font-size:11px;color:#64748b;">STYLE</div>
      <div style="font-size:16px;font-weight:700;color:#a78bfa;margin-top:4px;">{gv}</div>
    </div>
    <div style="flex:1;text-align:center;padding:10px;background:#0f172a;border-radius:8px;">
      <div style="font-size:11px;color:#64748b;">LIQUIDITY</div>
      <div style="font-size:16px;font-weight:700;color:#38bdf8;margin-top:4px;">{liq}</div>
    </div>
  </div>
  {fred_html}
  <div style="background:#1e293b;border-radius:10px;padding:16px;margin-bottom:16px;">
    <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:8px;">💡 시장 핵심 요약</div>
    <div style="font-size:14px;color:#cbd5e1;line-height:1.7;">{analysis.get('summary','')}</div>
  </div>
  <div style="background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;">🔄 섹터 로테이션</div>
    <div style="font-size:13px;color:#94a3b8;margin-top:6px;">{analysis.get('rotation','')}</div>
  </div>
  <div style="background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;">🎯 Top Quant Picks</div>
    <div style="font-size:15px;font-weight:700;color:#34d399;margin-top:6px;">{analysis.get('top_quant_picks','')}</div>
  </div>
  <div style="margin-bottom:16px;">
    <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:10px;">📊 주요 투자 테마</div>
    {themes_html}
  </div>
  <div style="text-align:center;padding:16px;">
    <a href="https://quantterminal.streamlit.app" style="background:#2563eb;color:#fff;text-decoration:none;padding:12px 32px;border-radius:8px;font-weight:700;font-size:14px;">🚀 Quant Terminal 열기</a>
  </div>
  <div style="text-align:center;font-size:11px;color:#475569;margin-top:16px;">본 리포트는 AI 참고용이며 투자 권유가 아닙니다.</div>
</div></body></html>"""

def send_email(subject: str, html_body: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = GMAIL_TO
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())
        print(f"[OK] 이메일 발송 완료 → {GMAIL_TO}")
        return True
    except Exception as e:
        print(f"[ERROR] 이메일 발송 실패: {e}")
        return False

# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"[START] {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    market_day = is_market_open_today()
    print(f"[INFO] NYSE 개장일: {market_day}")

    fred = Fred(api_key=FRED_API_KEY)
    fred_releases = get_todays_major_releases(fred)
    fred_alert = ""
    if fred_releases:
        print(f"[INFO] FRED 발표: {fred_releases}")
        items = "\n".join(f"  - {r}" for r in fred_releases)
        fred_alert = f"\n⚠️ [오늘의 주요 경제지표 발표]\n{items}\n→ 예측 불확실성이 높습니다.\n"

    print("[STEP 1] 뉴스 수집 중...")
    top_news, context_text, raw_count = fetch_global_market_news()
    print(f"[INFO] {raw_count}건 → Top {len(top_news)}건")

    if not context_text:
        print("[ERROR] 뉴스 수집 실패"); sys.exit(1)

    print("[STEP 2] Gemini 내러티브 생성 중...")
    analysis = generate_market_narrative(context_text, fred_alert)
    if not analysis:
        print("[ERROR] 내러티브 생성 실패"); sys.exit(1)
    print(f"[INFO] 테마 수: {len(analysis.get('themes',[]))}")

    print("[STEP 3] Google Sheets 저장 중...")
    save_narrative_to_sheet(analysis, fred_releases)

    print("[STEP 4] 이메일 발송 중...")
    now_et = datetime.now(_ET)
    session = "Pre-Market 8AM" if now_et.hour < 12 else "After-Close 5PM"
    fred_tag = " ⚠️FRED발표" if fred_releases else ""
    subject = f"📰 [{session}] 시장 내러티브 리포트 {now_et.strftime('%m/%d')}{fred_tag}"
    send_email(subject, build_email_html(analysis, raw_count, fred_releases, market_day))

    print(f"[DONE] {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)

if __name__ == "__main__":
    main()
