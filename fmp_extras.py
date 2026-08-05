# -*- coding: utf-8 -*-
"""
fmp_extras.py — app.py 보조 모듈.

기존 app.py가 아직 쓰지 않던 FMP `stable` 엔드포인트들을 한곳에 모았습니다.
모든 함수는 app.py와 동일한 규칙을 따릅니다:
  - 키: st.secrets["FMP_API_KEY"]
  - 캐시: @st.cache_data(ttl=3600)
  - 실패 시 절대 예외를 던지지 않고 빈 dict/list/None 반환 (앱 크래시 방지)

app.py 상단에 다음 한 줄만 추가하면 됩니다:
    import fmp_extras as fx
이후 fx.fmp_dcf("AAPL") 처럼 호출하세요.
"""

from __future__ import annotations

import datetime as _dt
import os as _os
import time as _time

import numpy as np
import pandas as pd
import pytz
import requests

# streamlit 은 앱 환경에서만 존재 — 자동화(GitHub Actions)에서도 이 모듈을
# import 할 수 있도록 선택적 임포트 + 무해한 심(shim)으로 대체한다.
# (심: cache_data 는 no-op 데코레이터, secrets 는 빈 dict → _key 가 환경변수로 폴백)
try:
    import streamlit as st  # type: ignore
except Exception:  # pragma: no cover — 자동화 환경
    class _StShim:
        secrets: dict = {}
        @staticmethod
        def cache_data(*a, **k):
            def _wrap(fn):
                return fn
            return _wrap
    st = _StShim()  # type: ignore

_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 7
_ET_TZ = pytz.timezone("America/New_York")


def _key() -> str:
    """app.py의 _fmp_key()와 동일 + 자동화 폴백: st.secrets 미설정 시 환경변수 FMP_API_KEY."""
    try:
        k = str(st.secrets.get("FMP_API_KEY", "") or "").strip()
        if k:
            return k
    except Exception:
        pass
    return str(_os.environ.get("FMP_API_KEY", "") or "").strip()


def _get_json(path: str):
    """공통 GET → JSON. 실패 시 None. (path 예: 'profile?symbol=AAPL')"""
    k = _key()
    if not k:
        return None
    sep = "&" if "?" in path else "?"
    url = f"{_FMP_BASE}/{path}{sep}apikey={k}"
    try:
        r = requests.get(url, timeout=_FMP_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _f(v, default=np.nan) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════
# 3단계 — 밸류에이션 / 정밀 검사 보강
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=3600, show_spinner=False)
def fmp_dcf(ticker: str) -> dict:
    """DCF 적정주가 vs 현재가 괴리율.
    반환: {"dcf": float, "price": float, "gap_pct": float, "verdict": str} 또는 {}."""
    data = _get_json(f"discounted-cash-flow?symbol={ticker}")
    if not data:
        return {}
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return {}
    dcf = _f(row.get("dcf"))
    price = _f(row.get("Stock Price", row.get("stockPrice", row.get("price"))))
    if np.isnan(dcf) or np.isnan(price) or price <= 0:
        return {}
    gap = (dcf / price - 1) * 100
    if gap >= 15:
        verdict = "🟢 저평가 (DCF가 현재가보다 높음)"
    elif gap <= -15:
        verdict = "🔴 고평가 (DCF가 현재가보다 낮음)"
    else:
        verdict = "🟡 적정 범위"
    return {"dcf": round(dcf, 2), "price": round(price, 2),
            "gap_pct": round(gap, 1), "verdict": verdict}


@st.cache_data(ttl=3600, show_spinner=False)
def fmp_stock_peers(ticker: str) -> list[str]:
    """동종업계 peer 티커 목록."""
    data = _get_json(f"stock-peers?symbol={ticker}")
    if not data:
        return []
    out = []
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if isinstance(row, dict):
            sym = row.get("symbol") or row.get("peersList")
            if isinstance(sym, list):
                out.extend(sym)
            elif sym:
                out.append(sym)
        elif isinstance(row, str):
            out.append(row)
    # 자기 자신 제외, 중복 제거
    seen, clean = set(), []
    for s in out:
        s = str(s).upper().strip()
        if s and s != str(ticker).upper() and s not in seen:
            seen.add(s)
            clean.append(s)
    return clean[:10]


@st.cache_data(ttl=3600, show_spinner=False)
def fmp_revenue_product_segmentation(ticker: str) -> dict:
    """가장 최근 기간의 제품별 매출 비중. 반환: {제품명: 비중%}."""
    data = _get_json(f"revenue-product-segmentation?symbol={ticker}")
    return _segmentation_latest(data)


@st.cache_data(ttl=3600, show_spinner=False)
def fmp_revenue_geographic_segmentation(ticker: str) -> dict:
    """가장 최근 기간의 지역별 매출 비중. 반환: {지역명: 비중%}."""
    data = _get_json(f"revenue-geographic-segmentation?symbol={ticker}")
    return _segmentation_latest(data)


def _segmentation_latest(data) -> dict:
    """FMP segmentation 응답에서 최신 기간의 {세그먼트: 비중%} 추출."""
    if not isinstance(data, list) or not data:
        return {}
    try:
        # 최신 기간(보통 첫 항목). data[*] = {"date":..., "data":{seg: amount}}
        latest = data[0]
        seg = latest.get("data", latest) if isinstance(latest, dict) else {}
        nums = {k: _f(v) for k, v in seg.items() if not np.isnan(_f(v))}
        total = sum(v for v in nums.values() if v > 0)
        if total <= 0:
            return {}
        return {k: round(v / total * 100, 1) for k, v in nums.items()}
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════
# 4단계 — 포트폴리오 매도 레이더 보강
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=3600, show_spinner=False)
def fmp_price_target_summary(ticker: str) -> dict:
    """애널리스트 목표가 요약(평균/최저/최고). price-target-consensus보다 분포가 상세."""
    data = _get_json(f"price-target-summary?symbol={ticker}")
    if not data:
        return {}
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return {}
    out = {}
    for k_out, keys in {
        "avg": ("lastMonthAvgPriceTarget", "allTimeAvgPriceTarget", "lastQuarterAvgPriceTarget"),
        "high": ("allTimeHighPriceTarget", "lastMonthHighPriceTarget"),
        "low": ("allTimeLowPriceTarget", "lastMonthLowPriceTarget"),
        "count": ("allTimeCount", "lastMonthCount"),
    }.items():
        for kk in keys:
            v = _f(row.get(kk))
            if not np.isnan(v):
                out[k_out] = round(v, 2)
                break
    return out


@st.cache_data(ttl=21600, show_spinner=False)
def fmp_dividends(ticker: str) -> dict:
    """다가오는/최근 배당 정보. 반환: {"ex_date","amount","yield_hint"} 또는 {}."""
    data = _get_json(f"dividends?symbol={ticker}")
    if not isinstance(data, list) or not data:
        return {}
    today = _dt.date.today()
    upcoming = None
    for row in data:
        if not isinstance(row, dict):
            continue
        ds = str(row.get("date", row.get("recordDate", "")))[:10]
        try:
            d = _dt.date.fromisoformat(ds)
        except Exception:
            continue
        if d >= today:
            upcoming = row  # 가장 가까운 미래(데이터가 최신순이면 마지막으로 갱신)
    target = upcoming or data[0]
    return {
        "ex_date": str(target.get("date", ""))[:10],
        "amount": _f(target.get("dividend", target.get("adjDividend"))),
        "is_upcoming": upcoming is not None,
    }


@st.cache_data(ttl=21600, show_spinner=False)
def fmp_splits_calendar(days_ahead: int = 30) -> list[dict]:
    """향후 days_ahead일 이내 주식 분할 일정. 반환: [{symbol, date, ratio}]."""
    today = _dt.date.today()
    to = today + _dt.timedelta(days=days_ahead)
    data = _get_json(f"splits-calendar?from={today.isoformat()}&to={to.isoformat()}")
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        out.append({
            "symbol": str(row.get("symbol", "")).upper(),
            "date": str(row.get("date", ""))[:10],
            "ratio": f"{row.get('numerator', '?')}:{row.get('denominator', '?')}",
        })
    return out


@st.cache_data(ttl=21600, show_spinner=False)
def fmp_senate_trades(ticker: str) -> list[dict]:
    """특정 종목에 대한 미 상원의원 거래 내역(최근)."""
    return _congress_trades("senate-trades", ticker)


@st.cache_data(ttl=21600, show_spinner=False)
def fmp_house_trades(ticker: str) -> list[dict]:
    """특정 종목에 대한 미 하원의원 거래 내역(최근)."""
    return _congress_trades("house-trades", ticker)


def _congress_trades(endpoint: str, ticker: str) -> list[dict]:
    data = _get_json(f"{endpoint}?symbol={ticker}")
    if not isinstance(data, list):
        return []
    out = []
    for row in data[:20]:
        if not isinstance(row, dict):
            continue
        typ = str(row.get("type", row.get("transactionType", ""))).lower()
        action = "매도" if "sale" in typ or "sell" in typ else ("매수" if "purchase" in typ or "buy" in typ else typ)
        out.append({
            "name": row.get("firstName", "") and f"{row.get('firstName','')} {row.get('lastName','')}".strip()
                    or row.get("representative", row.get("office", "")),
            "action": action,
            "date": str(row.get("transactionDate", row.get("dateRecieved", "")))[:10],
            "amount": row.get("amount", ""),
        })
    return out


# ══════════════════════════════════════════════════════════════════════════
# 배치 호출 (레이트리밋 대응) — 포트폴리오/워치리스트 N종목을 1콜로
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=900, show_spinner=False)
def fmp_batch_quote_short(tickers: tuple) -> dict:
    """여러 종목 현재가/변동률/거래량을 한 번에. 반환: {ticker: {price, change_pct, volume}}.
    ⚠️ 캐시 키 안정성을 위해 tickers는 tuple로 전달하세요."""
    syms = ",".join(sorted({str(t).upper().strip() for t in tickers if t}))
    if not syms:
        return {}
    data = _get_json(f"batch-quote-short?symbols={syms}")
    if not isinstance(data, list):
        return {}
    out = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        out[sym] = {
            "price": _f(row.get("price")),
            "change_pct": _f(row.get("changesPercentage", row.get("change"))),
            "volume": _f(row.get("volume")),
        }
    return out


@st.cache_data(ttl=900, show_spinner=False)
def fmp_batch_etf_quotes(tickers: tuple) -> dict:
    """여러 ETF 현재가/변동률을 한 번에. 반환: {ticker: {price, change_pct}}."""
    syms = ",".join(sorted({str(t).upper().strip() for t in tickers if t}))
    if not syms:
        return {}
    data = _get_json(f"batch-etf-quotes?symbols={syms}")
    if not isinstance(data, list):
        # 일부 플랜은 symbols 파라미터 미지원 → batch-quote-short로 폴백
        return fmp_batch_quote_short(tickers)
    out = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper()
        if sym:
            out[sym] = {"price": _f(row.get("price")),
                        "change_pct": _f(row.get("changesPercentage", row.get("change")))}
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def fmp_batch_market_cap(tickers: tuple) -> dict:
    """여러 종목 시가총액을 한 번에. 반환: {ticker: market_cap(float)}."""
    syms = ",".join(sorted({str(t).upper().strip() for t in tickers if t}))
    if not syms:
        return {}
    data = _get_json(f"batch-market-capitalization?symbols={syms}")
    if not isinstance(data, list):
        return {}
    out = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper()
        mc = _f(row.get("marketCap", row.get("marketCapitalization")))
        if sym and not np.isnan(mc):
            out[sym] = mc
    return out


# ══════════════════════════════════════════════════════════════════════════
# 2단계 — ETF 분석 (stable 엔드포인트로 교체/보강)
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=86400, show_spinner=False)
def fmp_etf_holdings(symbol: str) -> pd.DataFrame:
    """ETF 보유 종목(stable: etf/holdings). legacy etf-holder/ 대체.
    반환: columns=[asset, name, weight_pct]."""
    data = _get_json(f"etf/holdings?symbol={symbol}")
    if not isinstance(data, list) or not data:
        return pd.DataFrame()
    rows = []
    for row in data:
        if not isinstance(row, dict):
            continue
        w = _f(row.get("weightPercentage", row.get("weight")))
        if 0 < w <= 1:  # 비율(0~1)이면 %로
            w *= 100
        rows.append({
            "asset": str(row.get("asset", row.get("symbol", ""))).upper(),
            "name": row.get("name", ""),
            "weight_pct": round(w, 3) if not np.isnan(w) else np.nan,
        })
    df = pd.DataFrame(rows)
    if not df.empty and "weight_pct" in df.columns:
        df = df.sort_values("weight_pct", ascending=False, na_position="last")
    return df


@st.cache_data(ttl=86400, show_spinner=False)
def fmp_etf_info(symbol: str) -> dict:
    """ETF 기초 정보: 운용보수(expense ratio), AUM, 보유종목 수 등."""
    data = _get_json(f"etf/info?symbol={symbol}")
    if not data:
        return {}
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return {}
    return {
        "name": row.get("name", ""),
        "expense_ratio": _f(row.get("expenseRatio")),
        "aum": _f(row.get("aum", row.get("assetsUnderManagement"))),
        "holdings_count": _f(row.get("holdingsCount", row.get("numberOfHoldings"))),
        "domicile": row.get("domicile", ""),
    }


@st.cache_data(ttl=86400, show_spinner=False)
def fmp_etf_sector_weighting(symbol: str) -> dict:
    """ETF 섹터 비중. 반환: {섹터: 비중%}."""
    data = _get_json(f"etf/sector-weighting?symbol={symbol}")
    if not isinstance(data, list):
        return {}
    out = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sec = row.get("sector", row.get("industry"))
        w = _f(row.get("weightPercentage", row.get("weight")))
        if 0 < w <= 1:
            w *= 100
        if sec and not np.isnan(w):
            out[str(sec)] = round(w, 2)
    return out


# ══════════════════════════════════════════════════════════════════════════
# Emerging 종목 추적기 보강 — 시장 모멘텀 발굴
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=1800, show_spinner=False)
def fmp_biggest_gainers(limit: int = 20) -> list[dict]:
    """장중 급등 상위 종목."""
    return _movers("biggest-gainers", limit)


@st.cache_data(ttl=1800, show_spinner=False)
def fmp_biggest_losers(limit: int = 20) -> list[dict]:
    """장중 급락 상위 종목."""
    return _movers("biggest-losers", limit)


def _movers(endpoint: str, limit: int) -> list[dict]:
    data = _get_json(endpoint)
    if not isinstance(data, list):
        return []
    out = []
    for row in data[:limit]:
        if not isinstance(row, dict):
            continue
        out.append({
            "symbol": str(row.get("symbol", "")).upper(),
            "name": row.get("name", ""),
            "change_pct": _f(row.get("changesPercentage", row.get("change"))),
            "price": _f(row.get("price")),
        })
    return out


# ══════════════════════════════════════════════════════════════════════════
# DRG 신호 7·8 공용 데이터 — sector-performance-snapshot 1콜로 두 신호 산출
#   app.py(compute_daily_risk_gauge)와 run_drg_predict.py가 동일 로직을 쓰도록
#   계산부를 여기로 분리. (run_drg_predict.py는 streamlit 미사용이라 inline 복제)
# ══════════════════════════════════════════════════════════════════════════
_DEFENSIVE_SECTORS = {"Utilities", "Consumer Staples", "Consumer Defensive",
                      "Healthcare", "Health Care", "Real Estate"}
_CYCLICAL_SECTORS = {"Technology", "Consumer Cyclical", "Consumer Discretionary",
                     "Industrials", "Financial Services", "Financials",
                     "Energy", "Basic Materials", "Materials",
                     "Communication Services"}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sector_snapshot() -> list:
    """가장 최근 거래일의 섹터 퍼포먼스 스냅샷(원본 리스트)."""
    today = _dt.datetime.now(_ET_TZ).date()
    for back in range(0, 6):
        d = today - _dt.timedelta(days=back)
        if d.weekday() >= 5:  # 토/일 건너뜀
            continue
        data = _get_json(f"sector-performance-snapshot?date={d.isoformat()}")
        if isinstance(data, list) and data:
            return data
    return []


def compute_sector_signals(snapshot: list) -> dict:
    """스냅샷 → 방어/경기민감 로테이션 + 시장 폭(breadth) 지표.
    app.py와 run_drg_predict.py 양쪽에서 공통 호출 가능(streamlit 비의존)."""
    if not isinstance(snapshot, list) or not snapshot:
        return {}
    vals = {}
    for row in snapshot:
        if not isinstance(row, dict):
            continue
        sec = row.get("sector")
        chg = row.get("averageChange", row.get("changesPercentage", row.get("change")))
        try:
            chg = float(chg)
        except (TypeError, ValueError):
            continue
        if sec:
            vals[str(sec)] = chg
    if not vals:
        return {}
    defs = [v for s, v in vals.items() if s in _DEFENSIVE_SECTORS]
    cycs = [v for s, v in vals.items() if s in _CYCLICAL_SECTORS]
    neg_frac = sum(1 for v in vals.values() if v < 0) / len(vals)
    def_avg = float(np.mean(defs)) if defs else float("nan")
    cyc_avg = float(np.mean(cycs)) if cycs else float("nan")
    rotation = (def_avg - cyc_avg) if (defs and cycs) else float("nan")
    return {
        "vals": vals,
        "neg_frac": neg_frac,
        "def_avg": def_avg,
        "cyc_avg": cyc_avg,
        "rotation": rotation,  # 양수 = 방어주가 경기민감주보다 강함 = 리스크오프 경향
    }


# ══════════════════════════════════════════════════════════════════════════
# 🛰️ 위성 섹터 Top10 — SSOT (app.py 탭 표시 + run_hidden_alpha 주간 이메일 공유)
# ══════════════════════════════════════════════════════════════════════════

SECTOR_THEME_ETFS = {
    "XLK":  [("SOXX", "반도체"), ("IGV", "소프트웨어"), ("CIBR", "사이버보안"), ("SKYY", "클라우드"), ("BOTZ", "AI·로봇")],
    "XLV":  [("XBI", "바이오테크"), ("IHI", "의료기기"), ("IHF", "의료서비스"), ("PPH", "제약"), ("GNOM", "유전체")],
    "XLE":  [("XOP", "탐사·생산"), ("OIH", "오일서비스"), ("URA", "우라늄/원자력"), ("TAN", "태양광"), ("AMLP", "미드스트림")],
    "XLI":  [("ITA", "방산·항공"), ("JETS", "항공"), ("IYT", "운송"), ("PAVE", "인프라"), ("UFO", "우주")],
    "XLF":  [("KRE", "지방은행"), ("KBE", "은행"), ("KIE", "보험"), ("IAI", "증권·브로커"), ("FINX", "핀테크")],
    "XLY":  [("XRT", "소매"), ("ITB", "주택건설"), ("IBUY", "온라인소매"), ("PEJ", "레저·여행"), ("BETZ", "게이밍·베팅")],
    "XLB":  [("GDX", "금광"), ("COPX", "구리광"), ("LIT", "리튬·배터리"), ("SLX", "철강"), ("WOOD", "목재")],
    "XLRE": [("VNQ", "리츠 광범위"), ("REZ", "주거 리츠"), ("SRVR", "데이터센터 리츠"), ("INDS", "산업 리츠"), ("REM", "모기지 리츠")],
    "XLC":  [("FDN", "인터넷"), ("SOCL", "소셜미디어"), ("ESPO", "게임·e스포츠")],
    "XLU":  [("GRID", "스마트그리드"), ("NLR", "원자력")],
    "XLP":  [],
}

# 라이브(FMP /etf/holdings)가 비어 있을 때 사용하는 대표 보유종목 폴백.
# (요금제에 ETF Holdings 엔드포인트가 없을 때를 대비 — 라이브가 오면 라이브 우선)
ETF_CONSTITUENTS = {
    # GICS 11개 대형 섹터
    "XLK": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "ADBE", "CRM", "AMD", "CSCO", "INTU", "QCOM", "AMAT", "TXN", "NOW", "IBM"],
    "XLC": ["GOOGL", "META", "NFLX", "TMUS", "DIS", "VZ", "T", "CHTR", "CMCSA", "EA", "TTWO", "FOXA", "WBD", "DASH", "SPOT"],
    "XLY": ["AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "NKE", "SBUX", "TJX", "CMG", "RCL", "MAR", "GM", "F", "ORLY"],
    "XLP": ["COST", "WMT", "PG", "KO", "PEP", "PM", "MO", "MDLZ", "CL", "TGT", "KMB", "GIS", "KVUE", "KHC", "STZ"],
    "XLV": ["LLY", "UNH", "JNJ", "MRK", "ABBV", "PFE", "TMO", "DHR", "AMGN", "GILD", "BMY", "ISRG", "VRTX", "SYK", "CVS"],
    "XLF": ["JPM", "BRK-B", "V", "MA", "BAC", "WFC", "GS", "MS", "SCHW", "BLK", "AXP", "C", "PGR", "AIG", "USB"],
    "XLE": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "KMI", "HAL", "BKR", "DVN", "FANG", "WMB"],
    "XLI": ["GE", "CAT", "RTX", "HON", "UNP", "LMT", "DE", "ETN", "BA", "NOC", "UPS", "FDX", "WM", "EMR", "ITW"],
    "XLB": ["LIN", "APD", "SHW", "ECL", "NUE", "FCX", "DOW", "DD", "CTVA", "NEM", "MLM", "VMC", "PPG", "LYB", "MOS"],
    "XLRE": ["AMT", "PLD", "EQIX", "SPG", "O", "WELL", "PSA", "DLR", "CCI", "CBRE", "VICI", "AVB", "EQR", "ESS", "EXR"],
    "XLU": ["NEE", "SO", "DUK", "CEG", "AEP", "EXC", "SRE", "XEL", "D", "PCG", "PEG", "ED", "EIX", "WEC", "ETR"],
    # 테마/세부산업 ETF
    "SOXX": ["NVDA", "AVGO", "AMD", "MU", "TXN", "AMAT", "QCOM", "INTC", "LRCX", "KLAC", "ADI", "MCHP", "MRVL", "NXPI", "ON"],
    "IGV": ["MSFT", "ORCL", "CRM", "ADBE", "NOW", "PLTR", "PANW", "CRWD", "SNOW", "INTU", "FTNT", "DDOG", "WDAY", "TEAM", "ANSS"],
    "CIBR": ["CRWD", "PANW", "FTNT", "ZS", "CSCO", "GEN", "CYBR", "OKTA", "CHKP", "NET", "TENB", "S", "QLYS", "RPD", "AKAM"],
    "SKYY": ["ORCL", "MSFT", "GOOGL", "AMZN", "NOW", "CRM", "SNOW", "NET", "DDOG", "MDB", "ANET", "IBM", "ZS", "AKAM", "FTNT"],
    "BOTZ": ["NVDA", "ABB", "ISRG", "PATH", "SYM", "ROK", "FANUY", "TER", "OMCL", "CGNX", "IRBT", "UI", "NARI", "MDT", "KEYS"],
    "XBI": ["GILD", "BIIB", "REGN", "VRTX", "ALNY", "MRNA", "BNTX", "AMGN", "ILMN", "SRPT", "CRSP", "EXEL", "NBIX", "INCY", "ARGX"],
    "IHI": ["ISRG", "ABT", "BSX", "MDT", "SYK", "BDX", "EW", "DXCM", "ZBH", "RMD", "STE", "GEHC", "PODD", "BAX", "COO"],
    "IHF": ["UNH", "ELV", "CI", "HCA", "CVS", "CNC", "HUM", "MCK", "COR", "DVA", "MOH", "EHC", "UHS", "THC", "ENSG"],
    "PPH": ["LLY", "JNJ", "ABBV", "MRK", "NVS", "NVO", "AZN", "PFE", "BMY", "ZTS", "GSK", "AMGN", "TAK", "HLN", "VTRS"],
    "GNOM": ["CRSP", "NTLA", "BEAM", "TWST", "EXAS", "ARWR", "IONS", "PACB", "EDIT", "FATE", "RXRX", "DNA", "VCYT", "SDGR", "NVCR"],
    "XOP": ["COP", "EOG", "FANG", "DVN", "OXY", "HES", "MPC", "VLO", "PSX", "APA", "CTRA", "OVV", "EQT", "AR", "MUR"],
    "OIH": ["SLB", "HAL", "BKR", "TS", "FTI", "NOV", "CHX", "WFRD", "RIG", "LBRT", "HP", "NBR", "OII", "PTEN", "VAL"],
    "URA": ["CCJ", "BWXT", "NXE", "UEC", "DNN", "LEU", "UUUU", "SMR", "OKLO", "LTBR", "URG", "EU", "UROY"],
    "TAN": ["FSLR", "ENPH", "NXT", "SEDG", "RUN", "SHLS", "ARRY", "MAXN", "CSIQ", "FLNC", "NOVA", "DQ", "JKS", "SPWR", "CSLR"],
    "AMLP": ["MPLX", "ET", "EPD", "PAA", "WES", "ENLC", "HESM", "DTM", "SUN", "NS", "CQP", "DMLP", "GLP"],
    "ITA": ["RTX", "LMT", "NOC", "GD", "BA", "HII", "TXT", "TDG", "HEI", "KTOS", "AVAV", "LDOS", "LHX", "CW", "MRCY"],
    "JETS": ["DAL", "UAL", "LUV", "AAL", "ALK", "BA", "RYAAY", "ALGT", "SKYW", "HA", "GD", "CPA", "JBLU", "MESA", "HXL"],
    "IYT": ["UBER", "UPS", "UNP", "FDX", "CSX", "NSC", "ODFL", "JBHT", "CHRW", "EXPD", "R", "KNX", "LSTR", "WERN", "SAIA"],
    "PAVE": ["PWR", "ETN", "URI", "NUE", "EMR", "JCI", "FAST", "DE", "CARR", "MLM", "VMC", "HUBB", "AME", "TT", "WAB"],
    "UFO": ["PLTR", "RKLB", "LHX", "RTX", "NOC", "BA", "IRDM", "ASTS", "SPIR", "SATL", "VSAT", "GSAT", "TDY", "HEI", "CACI"],
    "KRE": ["TFC", "USB", "FITB", "RF", "HBAN", "MTB", "KEY", "CFG", "FCNCA", "ZION", "CMA", "WAL", "WBS", "EWBC", "SNV"],
    "KBE": ["COF", "GS", "MS", "BK", "STT", "JPM", "BAC", "WFC", "C", "USB", "PNC", "TFC", "FITB", "RF", "HBAN"],
    "KIE": ["PGR", "ALL", "MET", "PRU", "TRV", "AIG", "HIG", "CB", "AFL", "CINF", "L", "GL", "AIZ", "ACGL", "RGA"],
    "IAI": ["GS", "MS", "SCHW", "ICE", "CME", "SPGI", "MCO", "COIN", "IBKR", "MKTX", "NDAQ", "CBOE", "RJF", "LPLA", "TROW"],
    "FINX": ["PYPL", "COIN", "FI", "GPN", "AFRM", "SOFI", "NU", "FIS", "MELI", "TOST", "BILL", "HOOD", "FOUR", "FLYW", "MQ"],
    "XRT": ["ANF", "GAP", "W", "RH", "CVNA", "BBY", "DKS", "ULTA", "ROST", "TJX", "BURL", "FL", "KSS", "M", "GME"],
    "ITB": ["DHI", "LEN", "PHM", "NVR", "TOL", "KBH", "TPH", "MTH", "BLDR", "MAS", "SHW", "LOW", "HD", "MHK", "FBIN"],
    "IBUY": ["CHWY", "CVNA", "ETSY", "W", "AMZN", "EBAY", "MELI", "SE", "SHOP", "DASH", "ABNB", "EXPE", "PINS", "RVLV", "WSM"],
    "PEJ": ["BKNG", "MAR", "HLT", "RCL", "CCL", "NCLH", "LVS", "WYNN", "MGM", "DAL", "UAL", "CMG", "SBUX", "YUM", "DPZ"],
    "BETZ": ["DKNG", "FLUT", "PENN", "CZR", "MGM", "LVS", "WYNN", "BYD", "RSI", "GENI", "SGHC", "LNW", "GDEN", "CHDN", "ACEL"],
    "GDX": ["NEM", "AEM", "GOLD", "WPM", "FNV", "KGC", "GFI", "AU", "RGLD", "PAAS", "BVN", "HMY", "SSRM", "EGO", "OR"],
    "COPX": ["FCX", "SCCO", "TECK", "ERO", "HBM", "IVN", "FM", "LUN", "TGB", "CMMC", "AGI", "WRN", "CS", "ANTO", "GLEN"],
    "LIT": ["ALB", "SQM", "TSLA", "BYDDY", "PCRFY", "LAC", "PLL", "SLI", "MP", "FREY", "ENVX", "AMPX", "QS", "SES", "LICY"],
    "SLX": ["VALE", "NUE", "RIO", "STLD", "RS", "TX", "CLF", "MT", "GGB", "X", "CMC", "ATI", "WOR", "TMST", "SID"],
    "WOOD": ["WY", "PCH", "RYN", "WFG", "IP", "PKG", "SW", "SON", "LPX", "UFPI", "BCC", "OSB", "MERC", "SLVM", "DTC"],
    "VNQ": ["PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "DLR", "CCI", "CBRE", "EXR", "AVB", "VICI", "IRM", "EQR"],
    "REZ": ["WELL", "AVB", "EQR", "INVH", "VTR", "ESS", "MAA", "UDR", "AMH", "ELS", "SUI", "CPT", "DOC", "NNN", "STAG"],
    "SRVR": ["EQIX", "DLR", "AMT", "CCI", "SBAC", "IRM", "UNIT", "DBRG", "GLPI", "LAMR", "FYBR", "T", "VZ", "CSGP", "WY"],
    "INDS": ["PLD", "EXR", "PSA", "CUBE", "EGP", "FR", "REXR", "STAG", "TRNO", "NSA", "ILPT", "PLYM", "LXP", "GTY", "COLD"],
    "REM": ["AGNC", "NLY", "STWD", "RITM", "BXMT", "ABR", "CIM", "TWO", "RC", "ARI", "PMT", "NYMT", "DX", "EFC", "MFA"],
    "FDN": ["AMZN", "META", "GOOGL", "NFLX", "CRM", "UBER", "ABNB", "PYPL", "SHOP", "SNOW", "COIN", "DASH", "PINS", "SPOT", "Z"],
    "SOCL": ["META", "GOOGL", "PINS", "SNAP", "RDDT", "MTCH", "BIDU", "YELP", "BMBL", "NTES", "Z", "CARG", "DJT", "CARS", "TTGT"],
    "ESPO": ["NVDA", "NTES", "EA", "RBLX", "TTWO", "SE", "BILI", "U", "AMD", "LOGI", "CRSR", "PLTK", "SCPL", "GRVY", "SLGG"],
    "GRID": ["ABB", "ETN", "GEV", "PWR", "AME", "HUBB", "JCI", "EMR", "SU", "APH", "GLW", "ENPH", "BMI", "ITRI", "POWL"],
    "NLR": ["CEG", "CCJ", "BWXT", "PEG", "DUK", "SO", "EXC", "PWR", "GEV", "OKLO", "SMR", "NRG", "VST", "TLN", "LEU"],
}


GICS_SECTOR_LABELS = {
    "XLK": "기술", "XLC": "통신", "XLY": "자유소비재", "XLP": "필수소비재",
    "XLV": "헬스케어", "XLF": "금융", "XLE": "에너지", "XLI": "산업재",
    "XLB": "소재", "XLRE": "부동산", "XLU": "유틸리티",
}

# 위성 후보 풀: GICS 섹터 대표 ETF + 각 섹터의 테마 ETF (레버리지·인버스 없음)
def satellite_candidate_pool() -> dict:
    """{GICS 섹터티커: [후보 ETF들(섹터 대표 포함)]}"""
    pool = {}
    for sec in GICS_SECTOR_LABELS:
        pool[sec] = [sec] + [t for t, _ in SECTOR_THEME_ETFS.get(sec, [])]
    return pool


def _closes(ticker: str, limit: int = 170) -> pd.Series:
    """일별 종가 시리즈(오름차순·중복 날짜 제거). 일시 실패 2회 재시도."""
    for attempt in range(3):
        data = _get_json(f"historical-price-eod/full?symbol={ticker}&limit={limit}")
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if isinstance(rows, list) and rows:
            df = pd.DataFrame(rows)
            if "date" in df.columns and "close" in df.columns:
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                s = (pd.Series(pd.to_numeric(df["close"], errors="coerce").values, index=df["date"])
                     .dropna().sort_index())
                return s[~s.index.duplicated(keep="last")]
            return pd.Series(dtype=float)  # 형식 이상 = 확정 실패
        if data is not None:
            return pd.Series(dtype=float)  # 200·빈 응답 = 데이터 없음(재시도 무의미)
        _time.sleep(1.5 * (attempt + 1))   # None = 네트워크/429 → 재시도
    return pd.Series(dtype=float)


def _trailing_return(s: pd.Series, bars: int):
    if len(s) <= bars:
        return np.nan
    try:
        return float((s.iloc[-1] / s.iloc[-1 - bars] - 1) * 100)
    except Exception:
        return np.nan


def _top_holdings_set(ticker: str, fallback_map: dict, top_n: int = 15) -> set:
    """중복 계산용 구성종목 집합 — 라이브(/etf/holdings) 우선, 폴백 맵 차선."""
    try:
        live = fmp_etf_holdings(ticker)
        if live is not None and not live.empty and "symbol" in live.columns:
            syms = [str(x).strip().upper() for x in live["symbol"].head(top_n) if str(x).strip()]
            syms = [x for x in syms if "." not in x]
            if len(syms) >= 3:
                return set(syms)
    except Exception:
        pass
    fb = [str(x).strip().upper() for x in (fallback_map or {}).get(ticker, []) if "." not in str(x)]
    return set(fb[:top_n])


def compute_satellite_top10(top_n: int = 10, overlap_floor: float = 10.0,
                            pause_sec: float = 0.12) -> dict:
    """🛰️ 위성 섹터 Top10 — 월간 리밸런싱 후보 리스트 (SSOT).

    점수 = 1M×0.40 + 3M×0.40 + 6M×0.20  (1주 수익률은 노이즈 — 점수 제외, 표시만)
    GICS 섹터당 최고점 후보 1개만 → 상위 top_n. 같은 섹터 중복이 구조적으로 차단된다.

    시장 필터: SPY 종가 vs 200일선 — 아래면 '위성 신규 중단·축소' 수동 룰 발동 신호.
    후보 간 중복: 구성종목 상위 15개 집합의 교집합 비율(작은 쪽 기준 %),
                 overlap_floor 미만은 생략(전체는 matrix 에 보존).

    반환 dict:
      as_of, market_filter {spy, ma200, risk_on} | None,
      rows [{rank, ticker, sector, sector_label, theme_label, score, r1w, r1m, r3m, r6m,
             overlaps [(other, pct)]}],
      matrix {"A|B": pct}(전체 쌍), skipped [(ticker, 이유)]
    """
    out = {"as_of": _dt.datetime.now(_ET_TZ).strftime("%Y-%m-%d %H:%M ET"),
           "market_filter": None, "rows": [], "matrix": {}, "skipped": []}

    # ── 🚦 시장 필터 (SPY vs 200일선) ──
    spy = _closes("SPY", limit=260)
    if len(spy) >= 200:
        ma200 = float(spy.tail(200).mean())
        last = float(spy.iloc[-1])
        out["market_filter"] = {"spy": round(last, 2), "ma200": round(ma200, 2),
                                "risk_on": bool(last > ma200)}

    # ── 섹터별 챔피언 선발 ──
    theme_label_map = {t: lbl for lst in SECTOR_THEME_ETFS.values() for t, lbl in lst}
    champions = []
    for sec, cands in satellite_candidate_pool().items():
        best = None
        for tk in cands:
            s = _closes(tk, limit=170)
            _time.sleep(pause_sec)
            if len(s) < 127:  # 6M(126봉) 계산 불가
                out["skipped"].append((tk, f"히스토리 부족({len(s)}봉)"))
                continue
            r1w, r1m = _trailing_return(s, 5), _trailing_return(s, 21)
            r3m, r6m = _trailing_return(s, 63), _trailing_return(s, 126)
            if any(pd.isna(x) for x in (r1m, r3m, r6m)):
                out["skipped"].append((tk, "수익률 계산 실패"))
                continue
            score = 0.40 * r1m + 0.40 * r3m + 0.20 * r6m
            row = {"ticker": tk, "sector": sec,
                   "sector_label": GICS_SECTOR_LABELS.get(sec, sec),
                   "theme_label": ("섹터 대표" if tk == sec else theme_label_map.get(tk, "")),
                   "score": round(score, 2),
                   "r1w": round(r1w, 2) if pd.notna(r1w) else None,
                   "r1m": round(r1m, 2), "r3m": round(r3m, 2), "r6m": round(r6m, 2)}
            if best is None or row["score"] > best["score"]:
                best = row
        if best is not None:
            champions.append(best)

    champions.sort(key=lambda r: r["score"], reverse=True)
    rows = champions[:max(1, int(top_n))]
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # ── 후보 간 구성종목 중복 (전체 쌍 matrix + 행별 10%↑ 나열) ──
    hold = {r["ticker"]: _top_holdings_set(r["ticker"], ETF_CONSTITUENTS) for r in rows}
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            sa, sb = hold.get(a["ticker"], set()), hold.get(b["ticker"], set())
            pct = (len(sa & sb) / min(len(sa), len(sb)) * 100.0) if (sa and sb) else 0.0
            out["matrix"][f"{a['ticker']}|{b['ticker']}"] = round(pct, 1)
    for r in rows:
        partners = []
        for key, pct in out["matrix"].items():
            x, y = key.split("|")
            if r["ticker"] in (x, y) and pct >= overlap_floor:
                partners.append((y if x == r["ticker"] else x, pct))
        partners.sort(key=lambda p: -p[1])
        r["overlaps"] = partners

    out["rows"] = rows
    return out


def overlap_grade(pct: float) -> str:
    """중복 % → 색 등급 이모지 (🟢<25 · 🟡25~40 · 🔴40+)."""
    if pct >= 40:
        return "🔴"
    if pct >= 25:
        return "🟡"
    return "🟢"


# ═══════════════════════════════════════════════════════════════════════════════
# 레버리지/인버스 ETF 판별 SSOT — app.py · run_hidden_alpha.py 공용
# ═══════════════════════════════════════════════════════════════════════════════
# Hidden Alpha 로테이션 유니버스 필터 정책 (2026-07 확정):
#   - 인버스(음수 배수)  : 제외 확정 — 롱 모멘텀 로테이션과 구조적으로 상충
#   - 레버리지 롱(>1배)  : 제외가 디폴트 — 실전 운용 관행 코드화.
#                          Signal_Backtest 플래그 on/off 비교로 최종 판정 예정.
# 두 플래그는 app.py 랭킹과 run_hidden_alpha.py 이메일이 반드시 같은 값을 공유한다.
EXCLUDE_INVERSE: bool = True          # 인버스(-1x 포함) 로테이션 제외
EXCLUDE_LEVERAGED_LONG: bool = True   # 레버리지 롱(2x/3x/1.5x) 로테이션 제외

import re as _re

# 알려진 레버리지/인버스 ETF → 배수 매핑. 양수=레버리지 롱, 음수=인버스.
# 유니버스(미국 상장 ETF) 위주라 이 매핑이 이름 파싱보다 정확하다.
LEVERAGED_ETF_MAP = {
    # ── 3x 롱 ──
    "TQQQ": 3, "UPRO": 3, "SPXL": 3, "SOXL": 3, "TECL": 3, "FAS": 3,
    "TNA": 3, "LABU": 3, "UDOW": 3, "WEBL": 3, "FNGU": 3, "BULZ": 3,
    "NAIL": 3, "DPST": 3, "RETL": 3, "DFEN": 3, "CURE": 3, "DRN": 3,
    "GUSH": 3, "ERX": 3, "YINN": 3, "TPOR": 3, "UTSL": 3, "MIDU": 3,
    "URTY": 3, "TMF": 3, "DUST": 3, "JNUG": 3, "NUGT": 3, "USD": 3,
    # ── 2x 롱 ──
    "QLD": 2, "SSO": 2, "DDM": 2, "ROM": 2, "UWM": 2, "SAA": 2,
    "USD2X": 2, "NVDL": 2, "TSLL": 2, "AAPU": 2, "MSFU": 2, "GGLL": 2,
    "AMZU": 2, "FBL": 2, "NVDU": 2, "CONL": 2, "UYG": 2, "ROKT": 2,
    "BITX": 2, "BITU": 2, "ETHT": 2, "AGQ": 2, "UGL": 2, "BOIL": 2,
    "UCO": 2,
    # ── 1.5x ──
    "TSLR": 1.5,
    # ── 인버스 ──
    "SQQQ": -3, "SPXU": -3, "SOXS": -3, "TECS": -3, "FAZ": -3,
    "TZA": -3, "LABD": -3, "SDOW": -3, "WEBS": -3, "YANG": -3,
    "DRV": -3, "ERY": -3, "JDST": -3, "DGAZ": -3, "KOLD": -3, "SCO": -3,
    "SDS": -2, "QID": -2, "DXD": -2, "TWM": -2, "SKF": -2,
    "SH": -1, "PSQ": -1, "DOG": -1, "RWM": -1, "EUM": -1,
}

# 이름 문자열 보조 추정용 (매핑에 없을 때만): 배수 크기와 인버스 방향을 따로 판별.
_LEV_MAG_3X = _re.compile(r"\b3X\b|ULTRAPRO|TRIPLE", _re.I)
_LEV_MAG_2X = _re.compile(r"\b2X\b|\bULTRA\b|DOUBLE", _re.I)
_LEV_MAG_15 = _re.compile(r"\b1\.5X\b", _re.I)
_LEV_INVERSE = _re.compile(r"INVERSE|SHORT|BEAR", _re.I)
# 'SHORT'의 채권 만기 표현 오탐 방지 (Short-Term Treasury 등)
_LEV_SHORT_FALSE_POS = _re.compile(r"SHORT[\s\-]?(TERM|DURATION|MATURITY)", _re.I)


def get_leverage_multiplier(ticker: str, name: str = "") -> float | None:
    """티커(필요시 이름)로 레버리지 배수를 반환. 일반 ETF/주식이면 None.
    양수=레버리지 롱, 음수=인버스."""
    t = str(ticker).strip().upper()
    if t in LEVERAGED_ETF_MAP:
        return LEVERAGED_ETF_MAP[t]
    if name:
        n = str(name)
        if _LEV_MAG_3X.search(n):
            mag = 3.0
        elif _LEV_MAG_2X.search(n):
            mag = 2.0
        elif _LEV_MAG_15.search(n):
            mag = 1.5
        else:
            mag = None
        is_inverse = bool(_LEV_INVERSE.search(n)) and not _LEV_SHORT_FALSE_POS.search(n)
        if mag is not None:
            return -mag if is_inverse else mag
        if is_inverse:
            return -1.0  # 배수 명시 없는 단순 인버스(-1x)
    return None


def is_rotation_excluded(ticker: str, name: str = "") -> bool:
    """Hidden Alpha 로테이션 유니버스에서 제외 대상인지 (플래그 정책 적용)."""
    m = get_leverage_multiplier(ticker, name)
    if m is None:
        return False
    if m < 0:
        return EXCLUDE_INVERSE
    if m > 1:
        return EXCLUDE_LEVERAGED_LONG
    return False


def filter_rotation_universe(pairs: list) -> tuple[list, list]:
    """(ticker, name) 쌍 목록 → (유지 티커 목록, 제외 티커 목록).

    name 이 없는 항목은 빈 문자열로 취급 (티커 매핑만으로 판별).
    """
    kept, excluded = [], []
    for item in pairs:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            tk, nm = str(item[0]).strip().upper(), str(item[1] or "")
        else:
            tk, nm = str(item).strip().upper(), ""
        if not tk:
            continue
        (excluded if is_rotation_excluded(tk, nm) else kept).append(tk)
    return kept, excluded


# ══════════════════════════════════════════════════════════════════════════
# FMP 레이트 리미터 (SSOT) — Starter 플랜 300 calls/min 대응
#
# 배경: 주간 3버킷 스캐너가 61초에 약 950콜을 쏴서 한도를 3배 초과했고,
#       그 직후 실행된 Hidden Alpha 는 324티커 전부 조회에 실패해 종료됐다.
#       확산주도 76종목 중 73종목이 가격 데이터를 못 받아 결과가 오염됐다.
#
# scanner_core / run_hidden_alpha / 그 밖의 FMP 소비자는 **반드시** fmp_get() 을 쓴다.
# 프로세스별 슬라이딩 윈도우이므로, 워크플로에서 스텝 사이 쿨다운도 함께 둘 것.
# ══════════════════════════════════════════════════════════════════════════
import random as _random
import threading as _threading
from collections import deque as _deque

# 한도의 약 83% 로 보수적 설정. 워크플로에서 환경변수로 조절 가능.
FMP_RATE_LIMIT_PER_MIN = max(30, int(_os.environ.get("FMP_RATE_LIMIT_PER_MIN", "200") or 200))
# 429/402 를 만났을 때 재시도 횟수. 재시도가 없으면 한 번 밀린 호출이 그대로 유실되어
# 해당 티커가 dropna 로 탈락한다(대기주 15종목 중 13종목 유실 사고의 원인).
FMP_MAX_RETRIES = max(0, int(_os.environ.get("FMP_MAX_RETRIES", "3") or 3))
_FMP_WINDOW_SEC = 60.0

_fmp_rate_lock = _threading.Lock()
_fmp_call_times = _deque()
_fmp_stats = {"ok": 0, "rate_limited": 0, "http_error": 0, "exception": 0,
              "throttle_waits": 0, "throttle_sec": 0.0,
              "retries": 0, "recovered": 0, "gave_up": 0}


def fmp_rate_limit_acquire() -> float:
    """슬라이딩 윈도우 토큰 확보. 한도 초과 시 여유가 생길 때까지 대기.

    Returns: 실제 대기한 초(0.0 이면 즉시 통과).
    """
    waited = 0.0
    while True:
        with _fmp_rate_lock:
            now = _time.time()
            while _fmp_call_times and (now - _fmp_call_times[0]) >= _FMP_WINDOW_SEC:
                _fmp_call_times.popleft()
            if len(_fmp_call_times) < FMP_RATE_LIMIT_PER_MIN:
                _fmp_call_times.append(now)
                if waited > 0:
                    _fmp_stats["throttle_waits"] += 1
                    _fmp_stats["throttle_sec"] += waited
                return waited
            sleep_for = _FMP_WINDOW_SEC - (now - _fmp_call_times[0]) + 0.01
        sleep_for = min(max(sleep_for, 0.01), 5.0)
        _time.sleep(sleep_for)
        waited += sleep_for


def fmp_get(url: str, timeout: float = None, retries: int = None):
    """레이트 리밋 + 429 백오프 재시도를 적용한 GET.

    429(레이트리밋)·402(쿼터)·5xx 는 지수 백오프로 재시도한다. 4xx(잘못된 심볼 등)는
    재시도해도 소용없으므로 즉시 포기한다.

    Returns: requests.Response | None
    """
    n = FMP_MAX_RETRIES if retries is None else max(0, int(retries))
    last_kind = None
    for attempt in range(n + 1):
        fmp_rate_limit_acquire()
        try:
            r = requests.get(url, timeout=(timeout or _FMP_TIMEOUT))
        except Exception:
            last_kind = "exception"
            r = None
        else:
            if r.status_code == 200:
                with _fmp_rate_lock:
                    _fmp_stats["ok"] += 1
                    if attempt > 0:
                        _fmp_stats["recovered"] += 1
                return r
            if r.status_code in (429, 402):
                last_kind = "rate_limited"
            elif r.status_code >= 500:
                last_kind = "http_error"
            else:
                with _fmp_rate_lock:
                    _fmp_stats["http_error"] += 1
                return None  # 4xx 는 재시도 무의미

        if attempt < n:
            with _fmp_rate_lock:
                _fmp_stats["retries"] += 1
            # 지수 백오프 + 지터 (스레드가 동시에 몰려 재차 429 나는 것 방지)
            _time.sleep(min(2.0 * (2 ** attempt), 12.0) + _random.uniform(0, 1.5))

    with _fmp_rate_lock:
        _fmp_stats[last_kind or "exception"] += 1
        _fmp_stats["gave_up"] += 1
    return None


def fmp_stats() -> dict:
    with _fmp_rate_lock:
        return dict(_fmp_stats)


def fmp_reset_stats() -> None:
    with _fmp_rate_lock:
        for k in _fmp_stats:
            _fmp_stats[k] = 0 if k != "throttle_sec" else 0.0


def fmp_stats_line() -> str:
    s = fmp_stats()
    total = s["ok"] + s["rate_limited"] + s["http_error"] + s["exception"]
    return (f"FMP {total}콜 — 성공 {s['ok']}(재시도 회복 {s['recovered']}) · "
            f"레이트리밋 {s['rate_limited']} · HTTP오류 {s['http_error']} · "
            f"예외 {s['exception']} · 최종포기 {s['gave_up']} · "
            f"재시도 {s['retries']}회 · 스로틀 {s['throttle_sec']:.0f}초 "
            f"(한도 {FMP_RATE_LIMIT_PER_MIN}/분, 재시도 {FMP_MAX_RETRIES}회)")
