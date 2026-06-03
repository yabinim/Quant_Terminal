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

import numpy as np
import pandas as pd
import pytz
import requests
import streamlit as st

_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 7
_ET_TZ = pytz.timezone("America/New_York")


def _key() -> str:
    """app.py의 _fmp_key()와 동일. secrets 미설정 시 빈 문자열."""
    try:
        return str(st.secrets.get("FMP_API_KEY", "") or "").strip()
    except Exception:
        return ""


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
