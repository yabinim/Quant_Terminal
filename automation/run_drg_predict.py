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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import numpy as np
import pandas as pd
import pytz
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types as genai_types
from fredapi import Fred

# ── repo root 를 sys.path 에 추가 → narrative_core(app.py와 동일 SSOT 모듈) import ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import narrative_core

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
    "is_correct", "review_comment",
    # ── 2단계: 발표 후 9am 갱신 (revised_*) ──
    "revised_direction", "revised_full_text", "revision_reason", "revised_at", "is_revised"
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




# ── 거시지표 수집 (앱의 Daily Risk Gauge 5가지 신호와 동일) ──────────────────
def fetch_macro_context(fred: Fred, full_events: list = None) -> str:
    """앱의 compute_daily_risk_gauge와 동일한 8가지 선행 신호 수집
    (VIX·신용·대장주·프리마켓·리스크오프·이벤트·섹터로테이션·시장폭)."""
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

    # ── 신호 4: 프리마켓 방향 (SPY/QQQ 실시간 확장시간 가격 기준) ───────────
    # [중요] FMP는 선물(ES=F/NQ=F)을 지원하지 않는다. 과거 구현은 stock-price-change
    # 의 1D(전일 정규장 종가-대-종가 변화율)를 "프리마켓"이라 라벨만 붙여 사용했는데,
    # 이는 '오늘 프리마켓'이 아니라 '직전 거래일 움직임'이라 라이브와 정반대로 나올 수
    # 있었다(예: 월요일 아침엔 금요일 종가 움직임을 표시).
    # → 실제 확장시간(프리/애프터) 체결가를 aftermarket-trade/aftermarket-quote 로
    #    받아 전일 종가 대비 % 를 계산한다. 확장시간 데이터가 없으면(플랜 미포함 등)
    #    stock-price-change(1D)로 폴백하되 라벨을 '전일 종가 기준'으로 정직하게 표기.
    def _prev_close(sym: str):
        # 전일(직전 정규장) 종가. quote.previousClose 가 가장 명확하다.
        # [중요] historical-price-eod(limit=1)는 장중에 '오늘'의 진행중 봉을 돌려줘서
        # 프리마켓 % 가 (오늘÷오늘)=~0% 로 뭉개지는 버그가 있었다. previousClose 사용.
        try:
            r = requests.get(f"{FMP_BASE}/quote?symbol={sym}&apikey={FMP_KEY}", timeout=8)
            if r.status_code == 200:
                d = r.json()
                it = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
                pc = it.get("previousClose")
                if pc not in (None, "", 0):
                    return float(pc)
        except Exception:
            pass
        # 폴백: historical-price-eod 2개 받아 '오늘'이 아닌 직전 종가 사용
        try:
            r = requests.get(
                f"{FMP_BASE}/historical-price-eod/full?symbol={sym}&limit=2&apikey={FMP_KEY}",
                timeout=8,
            )
            if r.status_code == 200:
                d = r.json()
                rows = d.get("historical", d) if isinstance(d, dict) else d
                if isinstance(rows, list) and rows:
                    today = datetime.now(_ET).strftime("%Y-%m-%d")
                    for row in rows:
                        if str(row.get("date", ""))[:10] != today:
                            return float(row["close"])
                    return float(rows[-1]["close"])
        except Exception:
            pass
        return None

    def _ext_price(sym: str):
        """실시간 확장시간 체결가. (price) | None"""
        # 1차: aftermarket-trade (최근 확장시간 체결가)
        try:
            r = requests.get(f"{FMP_BASE}/aftermarket-trade?symbol={sym}&apikey={FMP_KEY}", timeout=8)
            if r.status_code == 200:
                d = r.json()
                it = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
                for f in ("price", "lastSalePrice", "tradePrice", "last"):
                    v = it.get(f)
                    if v not in (None, "", 0):
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
        except Exception:
            pass
        # 2차: aftermarket-quote (bid/ask 중간값)
        try:
            r = requests.get(f"{FMP_BASE}/aftermarket-quote?symbol={sym}&apikey={FMP_KEY}", timeout=8)
            if r.status_code == 200:
                d = r.json()
                it = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
                bid = it.get("bidPrice", it.get("bid"))
                ask = it.get("askPrice", it.get("ask"))
                try:
                    bid = float(bid); ask = float(ask)
                    if bid > 0 and ask > 0:
                        return (bid + ask) / 2.0
                except (TypeError, ValueError):
                    pass
                v = it.get("price", it.get("lastPrice"))
                if v not in (None, "", 0):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
        return None

    try:
        _pm = {}          # sym -> chg_pct
        _pm_src = "조회실패"
        _is_live = False
        if FMP_KEY:
            # 1차: 실시간 확장시간 가격 vs 전일 종가
            for sym in ("SPY", "QQQ"):
                px = _ext_price(sym)
                prev = _prev_close(sym)
                if px and prev and prev > 0:
                    _pm[sym] = (px / prev - 1) * 100
                    print(f"[INFO] 프리마켓(실시간) {sym}: ext={px} prev={prev} → {_pm[sym]:+.2f}%")
            if len(_pm) == 2:
                _is_live = True
                _pm_src = "FMP aftermarket(실시간 확장시간)"
            # 2차 폴백: 확장시간 데이터 없음 → stock-price-change 1D (전일 종가 기준)
            if not _is_live:
                _pm = {}
                try:
                    r_chg = requests.get(
                        f"{FMP_BASE}/stock-price-change?symbol=SPY,QQQ&apikey={FMP_KEY}",
                        timeout=8,
                    )
                    if r_chg.status_code == 200:
                        data_chg = r_chg.json()
                        items = data_chg if isinstance(data_chg, list) else [data_chg]
                        for item in items:
                            sym = str(item.get("symbol", "")).upper()
                            raw = item.get("1D", item.get("1d", item.get("day")))
                            try:
                                chg1d = float(raw)
                            except (TypeError, ValueError):
                                chg1d = None
                            if sym in ("SPY", "QQQ") and chg1d is not None:
                                _pm[sym] = chg1d
                    if _pm:
                        _pm_src = "FMP stock-price-change 1D (전일 종가 기준·실시간 아님)"
                        print(f"[WARN] 프리마켓 실시간 조회 실패 → 전일 종가(1D) 폴백: {_pm}")
                except Exception:
                    pass
        if _pm:
            avg_chg = float(np.mean(list(_pm.values())))
            pm_alert = avg_chg <= -0.5
            _pm_lines = " / ".join(f"{s} {_pm[s]:+.2f}%" for s in ("SPY", "QQQ") if s in _pm)
            status = "⚠️ 갭다운우려" if pm_alert else "✅ 정상"
            _label = "프리마켓 방향(실시간)" if _is_live else "프리마켓 방향(⚠️전일 종가 기준)"
            lines.append(f"- {_label}: {_pm_lines} (평균 {avg_chg:+.2f}%) [{status}] · 출처: {_pm_src}")
            signals_summary.append(f"프리마켓 {'경고' if pm_alert else '정상'}")
        else:
            lines.append("- 프리마켓 방향: 조회 실패")
    except Exception:
        lines.append("- 프리마켓 방향: 조회 실패")

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


    # ── 신호 6: 이벤트 리스크 (당일~3일 이내 고임팩트 경제 이벤트) ──────────
    # 앱의 compute_daily_risk_gauge 신호 6과 동일 개념. full_events 는 main()에서
    # get_todays_major_releases 로 받은 FMP economic-calendar 결과(오늘~+3일).
    try:
        upcoming = full_events or []
        high_events = [
            e for e in upcoming
            if str(e.get("impact", "")).lower() in ("high", "3")
        ]
        if high_events:
            ev_names = ", ".join(str(e.get("event", "")) for e in high_events[:3])
            ev_date = str(high_events[0].get("date", ""))
            lines.append(
                f"- 이벤트 리스크: 고임팩트 {len(high_events)}건 ({ev_date}~) "
                f"[⚠️ 변동성 확대 주의] {ev_names[:60]}"
            )
            signals_summary.append("이벤트 경고")
        elif upcoming:
            lines.append(f"- 이벤트 리스크: 고임팩트 없음 (예정 {len(upcoming)}건) [✅ 정상]")
            signals_summary.append("이벤트 정상")
        else:
            lines.append("- 이벤트 리스크: 캘린더 조회 불가 [✅ 정상]")
            signals_summary.append("이벤트 정상")
    except Exception:
        lines.append("- 이벤트 리스크: 조회 실패")

    # ── 신호 7·8: 섹터 로테이션 + 시장 폭(breadth) ─────────────────────────
    # sector-performance-snapshot 1콜로 두 신호를 함께 산출(앱과 동일 로직).
    # app.py의 compute_daily_risk_gauge 신호 7·8 / fmp_extras.compute_sector_signals 와 동일.
    try:
        _FMP_KEY7 = os.environ.get("FMP_API_KEY", "")
        _FMP_BASE7 = "https://financialmodelingprep.com/stable"
        _DEF = {"Utilities", "Consumer Staples", "Consumer Defensive",
                "Healthcare", "Health Care", "Real Estate"}
        _CYC = {"Technology", "Consumer Cyclical", "Consumer Discretionary",
                "Industrials", "Financial Services", "Financials",
                "Energy", "Basic Materials", "Materials", "Communication Services"}
        _snap = []
        if _FMP_KEY7:
            _today_et = datetime.now(pytz.timezone("America/New_York")).date()
            for _back in range(0, 6):
                _d = _today_et - timedelta(days=_back)
                if _d.weekday() >= 5:
                    continue
                _r7 = requests.get(
                    f"{_FMP_BASE7}/sector-performance-snapshot?date={_d.isoformat()}&apikey={_FMP_KEY7}",
                    timeout=8,
                )
                if _r7.status_code == 200:
                    _j7 = _r7.json()
                    if isinstance(_j7, list) and _j7:
                        _snap = _j7
                        break
        _vals = {}
        for _row7 in _snap:
            if not isinstance(_row7, dict):
                continue
            _sec = _row7.get("sector")
            _chg = _row7.get("averageChange", _row7.get("changesPercentage", _row7.get("change")))
            try:
                _chg = float(_chg)
            except (TypeError, ValueError):
                continue
            if _sec:
                _vals[str(_sec)] = _chg
        if _vals:
            _defs = [v for s, v in _vals.items() if s in _DEF]
            _cycs = [v for s, v in _vals.items() if s in _CYC]
            _neg_frac = sum(1 for v in _vals.values() if v < 0) / len(_vals)
            _def_avg = float(np.mean(_defs)) if _defs else float("nan")
            _cyc_avg = float(np.mean(_cycs)) if _cycs else float("nan")
            _rotation = (_def_avg - _cyc_avg) if (_defs and _cycs) else float("nan")

            # 신호 7: 섹터 로테이션 (방어주 > 경기민감주 = 리스크오프)
            _rot_alert = (not np.isnan(_rotation)) and _rotation > 0.3
            _rot_status = "⚠️ 방어주 로테이션(리스크오프)" if _rot_alert else "✅ 정상"
            if not np.isnan(_rotation):
                lines.append(
                    f"- 섹터 로테이션: 방어주 {_def_avg:+.2f}% vs 경기민감주 {_cyc_avg:+.2f}% "
                    f"(스프레드 {_rotation:+.2f}%p) [{_rot_status}]"
                )
                signals_summary.append(f"섹터로테이션 {'경고' if _rot_alert else '정상'}")

            # 신호 8: 시장 폭 (하락 섹터 비중)
            _br_alert = _neg_frac >= 0.7
            _br_status = "⚠️ 광범위 약세" if _br_alert else "✅ 정상"
            lines.append(
                f"- 시장 폭(breadth): 하락 섹터 {_neg_frac*100:.0f}% ({sum(1 for v in _vals.values() if v<0)}/{len(_vals)}) [{_br_status}]"
            )
            signals_summary.append(f"시장폭 {'경고' if _br_alert else '정상'}")
        else:
            lines.append("- 섹터 로테이션/시장 폭: 조회 실패")
    except Exception:
        lines.append("- 섹터 로테이션/시장 폭: 조회 실패")

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
    total_signals = len(signals_summary)
    # 앱(compute_daily_risk_gauge)과 동일한 경고-개수 기반 임계값으로 동기화.
    # 6+ HIGH / 4~5 CAUTION / 2~3 MODERATE / 0~1 LOW
    if warning_count >= 6:
        risk_level = "🔴 HIGH RISK"
    elif warning_count >= 4:
        risk_level = "🟡 CAUTION"
    elif warning_count >= 2:
        risk_level = "🟢 MODERATE"
    else:
        risk_level = "🟢 LOW RISK"
    lines.insert(0, f"[선행 신호 종합: {risk_level} | 경고 {warning_count}/{total_signals}개 (VIX·신용·대장주·프리마켓·리스크오프·이벤트·섹터로테이션·시장폭)]")

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

    # 고임팩트 발표일이면 시나리오 블록 추가 (단일 방향 판단 라인은 유지 → 검증 호환).
    # fred_section 은 오늘 고임팩트 이벤트(또는 하드코딩 발표)가 있을 때만 채워지므로
    # 그 자체를 '발표일' 여부 플래그로 재사용한다.
    scenario_section = ""
    if fred_section:
        scenario_section = (
            "**📅 발표 시나리오별 대응** (오늘 고임팩트 지표 발표 → 필수 작성, 각 1문장):\n"
            "- 예상 상회(서프라이즈 ↑) 시: \n"
            "- 예상 부합 시: \n"
            "- 예상 하회(서프라이즈 ↓) 시: \n\n"
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
        + scenario_section +
        "*본 분석은 AI 참고용이며 투자 권유가 아닙니다.*"
    )

    _RETRY_WAITS = [10, 30, 60, 120]
    for attempt in range(5):
        try:
            # gemini-2.5-flash는 thinking이 기본 ON이고, 사고 토큰이 max_output_tokens
            # 예산을 함께 깎는다 → 4096이면 본문이 문장 중간에 잘린다.
            # 앱(app.py)과 동일하게 사고를 끄고(0) 출력 예산을 8192로 올린다.
            cfg = genai_types.GenerateContentConfig(
                temperature=0.7,
                max_output_tokens=8192,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            )
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
        _ensure_drg_header(ws)  # 16컬럼 스키마로 헤더 자동 확장(폭 일치 유지)

        row = [
            _ADMIN_USER_ID, pred_date, direction, sector, bench_etf,
            str(spy_close) if not np.isnan(spy_close) else "",
            full_text, "", "", "", "",
            "", "", "", "", ""   # revised_direction, revised_full_text, revision_reason, revised_at, is_revised
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        print(f"[OK] DRG 예측 저장 완료: {pred_date} | {direction}")
        return True
    except Exception as e:
        print(f"[ERROR] Sheets 저장 실패: {e}")
        return False


# ── 2단계: 발표 후 9am 갱신 (revise mode) ────────────────────────────────────
def _a1col(n: int) -> str:
    """1-indexed 컬럼 번호 → A1 컬럼 문자 (1~26: A~Z, 그 이상도 처리)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _ensure_drg_header(ws):
    """시트 헤더에 _DRG_SHEET_COLS 의 누락 컬럼을 뒤에 덧붙여 자동 마이그레이션.
    기존 컬럼 순서는 건드리지 않으므로 기존 데이터/리더와 호환."""
    rows = ws.get_all_values()
    header = [str(c).strip() for c in rows[0]] if rows else []
    missing = [c for c in _DRG_SHEET_COLS if c not in header]
    if missing:
        new_header = header + missing
        ws.update([new_header],
                  range_name=f"A1:{_a1col(len(new_header))}1",
                  value_input_option="USER_ENTERED")
        print(f"[OK] DRG 헤더 자동 확장: +{missing}")


def load_today_baseline(pred_date: str) -> dict | None:
    """오늘(pred_date) admin 예측 행에서 8am 베이스라인(direction, full_text)을 로드.
    같은 날 여러 행이면 가장 마지막(최근) 것을 사용."""
    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        ws = sh.worksheet(_DRG_PREDICTIONS_WORKSHEET)
        rows = ws.get_all_values()
        if len(rows) < 2:
            return None
        header = [str(c).strip() for c in rows[0]]
        idx = {c: header.index(c) for c in ("user_id", "pred_date", "direction", "full_text")
               if c in header}
        match = None
        for r in rows[1:]:
            def g(c):
                return r[idx[c]] if c in idx and len(r) > idx[c] else ""
            if str(g("user_id")).strip() == _ADMIN_USER_ID and str(g("pred_date")).strip() == pred_date:
                match = {"direction": g("direction"), "full_text": g("full_text")}
        return match
    except Exception as e:
        print(f"[ERROR] 베이스라인 로드 실패: {e}")
        return None


def update_drg_revision(pred_date: str, revised_direction: str,
                         revised_full_text: str, revision_reason: str) -> bool:
    """오늘 8am 행의 revised_* 컬럼을 갱신(같은 행 = 단일 소스, 일관성 유지)."""
    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        ws = sh.worksheet(_DRG_PREDICTIONS_WORKSHEET)
        _ensure_drg_header(ws)
        rows = ws.get_all_values()
        header = [str(c).strip() for c in rows[0]]
        ui  = header.index("user_id") + 1
        pdc = header.index("pred_date") + 1
        cols = {c: header.index(c) + 1 for c in
                ("revised_direction", "revised_full_text", "revision_reason",
                 "revised_at", "is_revised")}
        target = None
        for i, r in enumerate(rows[1:], start=2):
            u = r[ui - 1]  if len(r) >= ui  else ""
            d = r[pdc - 1] if len(r) >= pdc else ""
            if str(u).strip() == _ADMIN_USER_ID and str(d).strip() == pred_date:
                target = i
        if target is None:
            print("[WARN] 갱신할 8am 행을 찾지 못함.")
            return False
        now_et = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
        ws.update_cell(target, cols["revised_direction"], revised_direction)
        ws.update_cell(target, cols["revised_full_text"], revised_full_text)
        ws.update_cell(target, cols["revision_reason"], revision_reason)
        ws.update_cell(target, cols["revised_at"], now_et)
        ws.update_cell(target, cols["is_revised"], "TRUE")
        print(f"[OK] 갱신 저장: {pred_date} → {revised_direction}")
        return True
    except Exception as e:
        print(f"[ERROR] 갱신 저장 실패: {e}")
        return False


def _format_release_results(high_today: list) -> str:
    """발표 결과 요약 (FMP actual/estimate best-effort)."""
    parts = []
    for e in high_today[:4]:
        name = str(e.get("event", ""))
        act  = e.get("actual")
        est  = e.get("estimate")
        seg = name
        if act not in (None, ""):
            seg += f" 실제={act}"
            if est not in (None, ""):
                seg += f" vs 예상={est}"
        elif est not in (None, ""):
            seg += f" (예상={est}, 실제값 미반영)"
        parts.append(seg)
    return "; ".join(parts) if parts else "발표 결과 수치 미확보"


def _extract_reason(text: str) -> str:
    """갱신 본문에서 '8am 대비 변경 사유' 문단만 best-effort 추출."""
    m = re.search(r"변경 사유\*\*\s*(.+?)(?:\n\s*\*\*|\Z)", text, re.S)
    return (m.group(1).strip()[:400] if m else "")


def _extract_revised_direction(text: str) -> str:
    """갱신 본문에서 '방향 판단' 라인만 보고 방향 추출.
    변경 사유에 옛 방향(예: '8am 상승 우세를 …')이 언급돼도 오판하지 않도록
    전체 텍스트가 아닌 판단 라인만 검사한다."""
    m = re.search(r"방향 판단\s*[:：]\s*\[?\s*(상승 우세|중립|하락 우세)", text)
    if m:
        return m.group(1)
    for line in text.splitlines():
        if "방향 판단" in line:
            if "상승 우세" in line:
                return "상승 우세"
            if "하락 우세" in line:
                return "하락 우세"
            if "중립" in line:
                return "중립"
    return extract_direction(text)  # 최후 폴백


def generate_drg_revision(orig_direction: str, orig_full_text: str,
                           macro_summary: str, release_results: str) -> str:
    """발표 후 프리마켓 반응을 주 신호로 8am 예측을 갱신.
    단일 방향 라인(상승/중립/하락 우세)은 유지 → 검증 호환."""
    client = genai.Client(api_key=GOOGLE_API_KEY)
    prompt = (
        "당신은 월가 수석 퀀트 전략가입니다. 지금은 미국 장 시작 전, "
        "오늘 08:30 ET 경제지표 발표가 끝난 직후입니다.\n\n"
        f"[오전 8시(발표 전) 예측 방향] {orig_direction}\n"
        f"[오전 8시 예측 전문 앞 800자]\n{str(orig_full_text)[:800]}\n\n"
        f"[오늘 발표 결과] {release_results}\n"
        f"[발표 후 거시·프리마켓 신호]\n{macro_summary}\n\n"
        "위 발표 결과와 '발표 후 프리마켓 반응(SPY/QQQ 방향)'을 반영해 예측을 갱신하세요. "
        "프리마켓 반응이 가장 신뢰도 높은 핵심 신호입니다. 아래 형식을 정확히 지키세요:\n\n"
        "## 갱신 시장 방향 판단: [상승 우세 / 중립 / 하락 우세]\n\n"
        "**🔄 8am 대비 변경 사유** (2~3문장: 발표 결과와 프리마켓 반응이 8am 예측을 어떻게 바꿨는지. "
        "방향이 그대로라면 왜 유지되는지)\n\n"
        "**🎯 개장 대응** (1~2문장)\n\n"
        "*본 분석은 AI 참고용이며 투자 권유가 아닙니다.*"
    )
    _RETRY = [10, 30, 60]
    for attempt in range(3):
        try:
            cfg = genai_types.GenerateContentConfig(
                temperature=0.3, max_output_tokens=4096,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            )
            resp = client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt, config=cfg)
            t = str(getattr(resp, "text", "") or "").strip()
            if t:
                return t
            raise ValueError("빈 응답")
        except Exception as e:
            wait = _RETRY[min(attempt, len(_RETRY) - 1)]
            print(f"[WARN] 갱신 생성 시도 {attempt+1}/3 실패: {e} → {wait}초 대기")
            if attempt < 2:
                time.sleep(wait)
    return ""


def _md_to_html(text: str) -> str:
    """간단 마크다운 → HTML (## 헤더, **굵게**, 줄바꿈)."""
    html = str(text)
    html = re.sub(r"^##\s*(.+)$",
                  r'<div style="font-size:16px;font-weight:800;color:#60a5fa;margin:14px 0 6px;">\1</div>',
                  html, flags=re.M)
    html = re.sub(r"\*\*(.*?)\*\*", r'<strong style="color:#f1f5f9;">\1</strong>', html)
    html = html.replace("\n", "<br>")
    return html


def build_revision_email_html(orig_dir: str, revised_dir: str, revised_text: str,
                               release_results: str, macro_summary: str) -> str:
    now_et = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
    arrow_color = "#dc2626" if "하락" in revised_dir else ("#16a34a" if "상승" in revised_dir else "#6b7280")
    body_html = _md_to_html(revised_text)
    macro_html = _md_to_html(macro_summary)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;">
  <div style="background:linear-gradient(135deg,#3b1e5f,#0f172a);border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #334155;">
    <div style="font-size:22px;font-weight:800;color:#a78bfa;">🔄 Quant Terminal</div>
    <div style="font-size:14px;color:#94a3b8;margin-top:4px;">DRG 발표 후 갱신 예측 · 09:00 ET (개장 전)</div>
    <div style="font-size:13px;color:#64748b;margin-top:8px;">{now_et}</div>
  </div>

  <div style="background:#1e293b;border-radius:10px;padding:16px;margin-bottom:16px;border:1px solid #334155;">
    <div style="font-size:13px;color:#94a3b8;margin-bottom:6px;">방향 갱신</div>
    <div style="font-size:18px;font-weight:800;">
      <span style="color:#94a3b8;">{orig_dir or 'N/A'}</span>
      <span style="color:#a78bfa;"> → </span>
      <span style="color:{arrow_color};">{revised_dir}</span>
    </div>
    <div style="font-size:13px;color:#cbd5e1;margin-top:10px;"><strong>발표 결과:</strong> {release_results}</div>
  </div>

  <div style="background:#1e293b;border-radius:10px;padding:18px;margin-bottom:16px;border:1px solid #334155;font-size:14px;line-height:1.7;color:#e2e8f0;">
    {body_html}
  </div>

  <div style="background:#0f172a;border-radius:10px;padding:14px;margin-bottom:16px;border:1px solid #334155;font-size:12px;line-height:1.6;color:#94a3b8;">
    <div style="font-weight:700;color:#cbd5e1;margin-bottom:6px;">📊 발표 후 거시·프리마켓</div>
    {macro_html}
  </div>

  <div style="text-align:center;color:#64748b;font-size:12px;padding:10px;">
    ⚠️ 이 갱신은 8:30 발표를 반영한 개장 전 예측입니다. AI 참고용이며 투자 권유가 아닙니다.
  </div>
</div></body></html>"""


def run_revise():
    """발표 후(9am ET) 예측 갱신 모드 — 고임팩트 발표일에만 동작."""
    print("=" * 60)
    print(f"[START] DRG 발표후 갱신: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    if not is_market_open_today():
        print("[SKIP] 오늘은 NYSE 휴장일. 갱신 스킵.")
        sys.exit(0)

    fred = Fred(api_key=FRED_API_KEY)
    fred_releases, full_events = get_todays_major_releases(fred)
    today = datetime.now(_ET).strftime("%Y-%m-%d")
    high_today = [e for e in (full_events or [])
                  if str(e.get("impact", "")).lower() in ("high", "3")
                  and str(e.get("date", ""))[:10] == today]
    if not (high_today or fred_releases):
        print("[SKIP] 오늘 고임팩트 발표 없음 → 갱신 불필요.")
        sys.exit(0)

    baseline = load_today_baseline(today)
    if not baseline or not str(baseline.get("full_text", "")).strip():
        print("[SKIP] 오늘 8am 예측 행 없음 → 갱신 대상 없음.")
        sys.exit(0)

    print("[STEP 1] 발표 후 거시·프리마켓 수집 중...")
    macro_summary = fetch_macro_context(fred, full_events=full_events)
    release_results = _format_release_results(high_today) if high_today else ", ".join(fred_releases)
    print(f"[INFO] 발표 결과: {release_results}")

    print("[STEP 2] Gemini 갱신 예측 생성 중...")
    revised_text = generate_drg_revision(
        baseline.get("direction", ""), baseline.get("full_text", ""),
        macro_summary, release_results)
    if not revised_text:
        print("[ERROR] 갱신 예측 생성 실패.")
        sys.exit(1)

    revised_dir = _extract_revised_direction(revised_text)
    revision_reason = _extract_reason(revised_text)
    print(f"[INFO] 방향 갱신: {baseline.get('direction','')} → {revised_dir}")

    print("[STEP 3] 갱신 저장 중...")
    update_drg_revision(today, revised_dir, revised_text, revision_reason)

    print("[STEP 4] 갱신 이메일 발송 중...")
    subject = (f"🔄 [DRG 갱신] {baseline.get('direction','')}→{revised_dir} · "
               f"{datetime.now(_ET).strftime('%m/%d')} (발표후)")
    html_body = build_revision_email_html(
        baseline.get("direction", ""), revised_dir, revised_text,
        release_results, macro_summary)
    send_email(subject, html_body)

    print(f"[DONE] 갱신 완료: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)


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
    _top, rss_news_text, raw_count, _news_meta = narrative_core.fetch_global_market_news(
        os.environ.get("FMP_API_KEY", "").strip(), top_n=60)
    print(f"[INFO] 뉴스 소스: {_news_meta.get('source_log', '')}")
    print(f"[INFO] 뉴스 {raw_count}건 수집 완료")

    print("[STEP 2] 거시지표 수집 중...")
    macro_summary = fetch_macro_context(fred, full_events=full_events)
    print(f"[INFO] 거시지표:\n{macro_summary}")

    print("[STEP 3] Gemini DRG 예측 생성 중...")
    full_text = generate_drg_prediction(rss_news_text, macro_summary, fred_releases, full_events=full_events)
    if not full_text:
        print("[ERROR] DRG 예측 생성 실패.")
        sys.exit(1)

    direction = extract_direction(full_text)
    print(f"[INFO] 예측 방향: {direction}")

    # ── 발표일 가드레일 ──────────────────────────────────────────────────────
    # 8:30 ET 발표 이전 예측임을 결정론적으로 명시(LLM 출력에 의존하지 않음).
    # full_text 에 prepend → 시트·앱·이메일이 모두 같은 배너를 보게 됨(단일 소스).
    # direction 은 위에서 '깨끗한' 본문으로 이미 추출했으므로 검증 점수에 영향 없음.
    _today_et_str = datetime.now(_ET).strftime("%Y-%m-%d")
    _high_today = [
        e for e in (full_events or [])
        if str(e.get("impact", "")).lower() in ("high", "3")
        and str(e.get("date", ""))[:10] == _today_et_str
    ]
    _ev_names = (
        [str(e.get("event", "")) for e in _high_today][:3]
        if _high_today else list(fred_releases)[:3]
    )
    _ev_names = [n for n in _ev_names if n]
    if _ev_names:
        _names_str = ", ".join(_ev_names)
        _guard = (
            f"⚠️ **발표 전 예측 주의** — 오늘 고임팩트 경제지표 발표 예정: {_names_str}. "
            "본 예측은 발표(통상 08:30 ET) 이전 데이터 기준이며, 발표 직후 시장 방향이 "
            "급변할 수 있습니다. 발표 수치와 시장 초기 반응을 확인하기 전에는 "
            "신규 진입을 보류하세요.\n\n"
        )
        full_text = _guard + full_text
        print(f"[INFO] 발표일 가드레일 추가: {_names_str}")

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
    _MODE = "predict"
    if "--mode" in sys.argv:
        _i = sys.argv.index("--mode")
        if _i + 1 < len(sys.argv):
            _MODE = sys.argv[_i + 1].strip().lower()
    if _MODE == "revise":
        run_revise()
    else:
        main()
