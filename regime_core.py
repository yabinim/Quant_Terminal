# -*- coding: utf-8 -*-
"""
regime_core.py — Quant Terminal 레짐(국면) 분류 + 타이밍 판정 공유 엔진 (SSOT)

설계 원칙
---------
- 순수 모듈: pandas / numpy 만 의존. streamlit · requests · FMP 호출 없음.
  → app.py 와 automation(run_*.py)이 동일하게 import 한다. (narrative_core.py 선례)
- 데이터 페치/캐싱은 호출 측(app.py·automation) 책임. 이 모듈은 OHLCV DataFrame을 받아
  지표를 계산하고 dict를 반환만 한다.
- 모든 임계값은 파일 상단 상수에 모아둔다(튜닝 한 곳).
- 청산 판정은 {"is_exit": bool, "reasons": [...]} 구조 → v2(네거티브 리버설·앵커드 VWAP)는
  reasons 에 detector 하나를 append 하는 것만으로 additive 하게 붙는다.

공개 API
--------
- classify_regime(hist, spy_close=None) -> dict
- rsi_band_for_regime(regime) -> (lo, hi)
- evaluate_timing(hist, regime_res) -> dict
- compute_exit_signals(hist, entry_price=None) -> dict
- analyze_ticker(hist, spy_close=None, entry_price=None) -> dict   # 편의 오케스트레이터

RSI는 app.py 의 calculate_rsi 와 동일한 '단순 롤링 평균' 방식 → 배지/개별종목 탭 수치 일치.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ──────────────────────────────────────────────────────────────────────────
# 튜닝 상수 (한 곳에서 관리)
# ──────────────────────────────────────────────────────────────────────────

RSI_WINDOW = 14
ATR_WINDOW = 22
SLOPE_LOOKBACK = 20          # 200일선 기울기 측정 봉 수
RS_LOOKBACK = 63             # 약 3개월(거래일)
TREND_HIGH_LOOKBACK = 20     # 천장(고점 하락) 판정용

# 강도 점수 배점 (합계 100)
W_MA_STACK = 30              # 정배열 (price>MA50, MA50>MA150, MA150>MA200) 각 10
W_MA200_SLOPE = 15
W_RS = 20
W_NEAR_HIGH = 15
W_ABOVE_LOW = 10
W_ABOVE_MA200 = 10

# 버킷 임계값
SCORE_STRONG = 70.0
SCORE_WEAK = 35.0

# RS 점수 스케일
RS_FULL_PT = 10.0            # RS(%p) 가 이 값 이상이면 만점

# 52주 위치 스케일
NEAR_HIGH_FULL = 0.0         # 고점 대비 0% → 만점
NEAR_HIGH_ZERO = -25.0       # 고점 대비 -25% → 0점
ABOVE_LOW_FULL = 25.0        # 저점 대비 +25% 이상 → 만점

# Cardwell RSI 밴드 (oversold/지지, overbought/저항)
RSI_BAND = {
    "strong":   (40.0, 80.0),
    "sideways": (40.0, 70.0),
    "weak":     (20.0, 60.0),
}

# 타이밍 판정 임계값
STRONG_ENTRY_RSI_LO = 40.0
STRONG_ENTRY_RSI_HI = 52.0
STRONG_OVERHEAT_RSI = 80.0
MA_PULLBACK_TOL = 1.02       # 상승 이평선 +2% 이내면 '눌림'
TREND_BREAK_PULLBACK = 0.10  # 최근 고점 대비 -10% 초과 → 추세 이탈
SIDEWAYS_ENTRY_RSI = 43.0
SIDEWAYS_OVERBOUGHT_RSI = 68.0

# 청산
CHANDELIER_ATR_MULT = 3.0
CHANDELIER_LOOKBACK = 22     # entry_price 없을 때 최고가 룩백


# ──────────────────────────────────────────────────────────────────────────
# 지표 헬퍼 (순수)
# ──────────────────────────────────────────────────────────────────────────

def compute_rsi(close, window: int = RSI_WINDOW) -> pd.Series:
    """app.py calculate_rsi 와 동일한 단순 롤링 평균 방식."""
    if close is None:
        return pd.Series(dtype=float)
    c = pd.to_numeric(close, errors="coerce").dropna()
    if c.empty:
        return pd.Series(dtype=float)
    delta = c.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.rolling(window=window, min_periods=window).mean()
    avg_loss = losses.rolling(window=window, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_atr(hist: pd.DataFrame, window: int = ATR_WINDOW) -> float:
    """True Range 기반 ATR. High/Low 없으면 종가 변동으로 근사."""
    if hist is None or hist.empty or "Close" not in hist.columns:
        return np.nan
    close = pd.to_numeric(hist["Close"], errors="coerce")
    if "High" in hist.columns and "Low" in hist.columns:
        high = pd.to_numeric(hist["High"], errors="coerce")
        low = pd.to_numeric(hist["Low"], errors="coerce")
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
    else:
        tr = close.diff().abs()
    atr = tr.rolling(window=window, min_periods=max(2, window // 2)).mean()
    atr = atr.dropna()
    return float(atr.iloc[-1]) if not atr.empty else np.nan


def _ma_last(close: pd.Series, window: int, min_p: int | None = None) -> float:
    mp = min_p if min_p is not None else window
    s = close.rolling(window=window, min_periods=mp).mean().dropna()
    return float(s.iloc[-1]) if not s.empty else np.nan


def _slope_norm(series: pd.Series, lookback: int = SLOPE_LOOKBACK) -> float:
    """최근 lookback 봉의 정규화 기울기(%, 현재가 대비). 양수=상승."""
    s = series.dropna()
    if len(s) < lookback + 1:
        return np.nan
    a, b = float(s.iloc[-lookback - 1]), float(s.iloc[-1])
    if a == 0 or not np.isfinite(a):
        return np.nan
    return (b / a - 1.0) * 100.0


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ──────────────────────────────────────────────────────────────────────────
# 1) 레짐 분류
# ──────────────────────────────────────────────────────────────────────────

def classify_regime(hist: pd.DataFrame, spy_close=None) -> dict:
    """OHLCV DataFrame → 레짐 분류.

    반환 dict:
      regime: "strong" | "sideways" | "weak" | "unknown"
      stage:  1~4 (Weinstein) | 0(unknown)
      score:  0~100
      rsi_band: (lo, hi)
      topping: bool (Stage 3 서브플래그)
      enough_data: bool
      components: {price, ma20, ma50, ma150, ma200, ma200_slope, rsi,
                   rs, pct_from_high, pct_from_low}
      color: "🟢"|"🟡"|"🔴"|"⚪"
    """
    out = {
        "regime": "unknown", "stage": 0, "score": np.nan,
        "rsi_band": RSI_BAND["sideways"], "topping": False,
        "enough_data": False, "components": {}, "color": "⚪",
    }
    if hist is None or hist.empty or "Close" not in hist.columns:
        return out

    close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
    if len(close) < 50:           # 최소 데이터 가드
        out["components"] = {"price": float(close.iloc[-1]) if not close.empty else np.nan}
        return out

    price = float(close.iloc[-1])
    ma20 = _ma_last(close, 20)
    ma50 = _ma_last(close, 50)
    ma150 = _ma_last(close, 150, min_p=120)
    ma200 = _ma_last(close, 200, min_p=150)
    ma200_series = close.rolling(200, min_periods=150).mean()
    ma200_slope = _slope_norm(ma200_series, SLOPE_LOOKBACK)
    rsi = compute_rsi(close)
    rsi_last = float(rsi.dropna().iloc[-1]) if not rsi.dropna().empty else np.nan

    high_52w = float(close.max())
    low_52w = float(close.min())
    pct_from_high = (price / high_52w - 1.0) * 100.0 if high_52w > 0 else np.nan
    pct_from_low = (price / low_52w - 1.0) * 100.0 if low_52w > 0 else np.nan

    # RS vs SPY (3개월 상대 모멘텀)
    rs = np.nan
    if spy_close is not None:
        spy = pd.to_numeric(spy_close, errors="coerce").dropna()
        if len(close) >= RS_LOOKBACK + 1 and len(spy) >= RS_LOOKBACK + 1:
            stock_mom = (close.iloc[-1] / close.iloc[-RS_LOOKBACK - 1] - 1.0) * 100.0
            spy_mom = (spy.iloc[-1] / spy.iloc[-RS_LOOKBACK - 1] - 1.0) * 100.0
            rs = float(stock_mom - spy_mom)

    # ── 강도 점수 ─────────────────────────────────────────────
    score = 0.0
    # 정배열 (각 10)
    if pd.notna(ma50) and price > ma50:
        score += W_MA_STACK / 3
    if pd.notna(ma50) and pd.notna(ma150) and ma50 > ma150:
        score += W_MA_STACK / 3
    if pd.notna(ma150) and pd.notna(ma200) and ma150 > ma200:
        score += W_MA_STACK / 3
    # 200일선 기울기
    if pd.notna(ma200_slope):
        score += W_MA200_SLOPE * _clip01((ma200_slope + 1.0) / 3.0)  # -1%~+2% → 0~1
    # RS
    if pd.notna(rs):
        score += W_RS * _clip01(rs / RS_FULL_PT)
    # 52주 고점 근접
    if pd.notna(pct_from_high):
        frac = (pct_from_high - NEAR_HIGH_ZERO) / (NEAR_HIGH_FULL - NEAR_HIGH_ZERO)
        score += W_NEAR_HIGH * _clip01(frac)
    # 52주 저점 대비
    if pd.notna(pct_from_low):
        score += W_ABOVE_LOW * _clip01(pct_from_low / ABOVE_LOW_FULL)
    # 200일선 위
    if pd.notna(ma200) and price > ma200:
        score += W_ABOVE_MA200

    score = round(float(score), 1)

    above_ma200 = pd.notna(ma200) and price > ma200
    slope_up = pd.notna(ma200_slope) and ma200_slope > 0

    # ── 버킷 ─────────────────────────────────────────────────
    if score >= SCORE_STRONG and above_ma200 and slope_up:
        regime, stage, color = "strong", 2, "🟢"
    elif score <= SCORE_WEAK or (not above_ma200 and pd.notna(ma200_slope) and ma200_slope < 0):
        regime, stage, color = "weak", 4, "🔴"
    else:
        regime, color = "sideways", "🟡"
        stage = 1  # 천장 판정 후 갱신

    # ── 천장(Stage 3) 서브플래그 ─────────────────────────────
    topping = False
    if regime != "weak":
        recent_high = float(close.tail(TREND_HIGH_LOOKBACK).max()) if len(close) >= TREND_HIGH_LOOKBACK else np.nan
        prior_high = (
            float(close.tail(TREND_HIGH_LOOKBACK * 2).head(TREND_HIGH_LOOKBACK).max())
            if len(close) >= TREND_HIGH_LOOKBACK * 2 else np.nan
        )
        lower_highs = pd.notna(recent_high) and pd.notna(prior_high) and recent_high < prior_high
        rolling_over = (pd.notna(rs) and rs < 0) or (pd.notna(ma50) and price < ma50)
        if above_ma200 and rolling_over and lower_highs:
            topping = True
            if regime == "sideways":
                stage = 3

    out.update({
        "regime": regime, "stage": stage, "score": score,
        "rsi_band": RSI_BAND.get(regime, RSI_BAND["sideways"]),
        "topping": topping, "enough_data": True, "color": color,
        "components": {
            "price": price, "ma20": ma20, "ma50": ma50, "ma150": ma150,
            "ma200": ma200, "ma200_slope": ma200_slope, "rsi": rsi_last,
            "rs": rs, "pct_from_high": pct_from_high, "pct_from_low": pct_from_low,
        },
    })
    return out


def rsi_band_for_regime(regime: str) -> tuple[float, float]:
    return RSI_BAND.get(str(regime), RSI_BAND["sideways"])


# ──────────────────────────────────────────────────────────────────────────
# 2) 타이밍 판정 (v1: 이평선 눌림 + Cardwell RSI 밴드)
# ──────────────────────────────────────────────────────────────────────────

def evaluate_timing(hist: pd.DataFrame, regime_res: dict) -> dict:
    """레짐 결과 기반 매수 타이밍 판정.

    반환 dict:
      verdict: 라벨 (🎯/⏳/⛔/🚫/🟡/🔴 ...)
      code:    "entry"|"wait"|"overheat"|"trend_break"|"avoid"|"unknown"
      reasons: [설명 문자열 ...]
      is_entry: bool
    """
    out = {"verdict": "데이터 부족", "code": "unknown", "reasons": [], "is_entry": False}
    if not regime_res or not regime_res.get("enough_data"):
        return out

    c = regime_res["components"]
    regime = regime_res["regime"]
    price, ma20, ma50, rsi = c.get("price"), c.get("ma20"), c.get("ma50"), c.get("rsi")
    band_lo, band_hi = regime_res["rsi_band"]
    reasons: list[str] = []

    if regime == "strong":
        recent_high = np.nan
        close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
        if len(close) >= TREND_HIGH_LOOKBACK:
            recent_high = float(close.tail(TREND_HIGH_LOOKBACK).max())
        pullback = (price / recent_high - 1.0) if pd.notna(recent_high) and recent_high > 0 else np.nan

        below_ma50 = pd.notna(ma50) and price < ma50
        deep_pullback = pd.notna(pullback) and pullback < -TREND_BREAK_PULLBACK
        rsi_lost = pd.notna(rsi) and rsi < band_lo

        near_ma20 = pd.notna(ma20) and price <= ma20 * MA_PULLBACK_TOL
        near_ma50 = pd.notna(ma50) and price <= ma50 * MA_PULLBACK_TOL

        if below_ma50 or deep_pullback or rsi_lost:
            if below_ma50:
                reasons.append("50일선 종가 이탈")
            if deep_pullback:
                reasons.append(f"최근 고점 대비 {pullback * 100:.0f}% 눌림(추세 위험)")
            if rsi_lost:
                reasons.append(f"RSI {rsi:.0f} < 강세 지지 {band_lo:.0f} 이탈")
            out.update({"verdict": "🚫 추세 이탈 위험", "code": "trend_break", "reasons": reasons})
        elif pd.notna(rsi) and rsi >= STRONG_OVERHEAT_RSI:
            reasons.append(f"RSI {rsi:.0f} ≥ {STRONG_OVERHEAT_RSI:.0f}(강세 과열) — 식을 때까지 대기")
            out.update({"verdict": "⛔ 단기 과열", "code": "overheat", "reasons": reasons})
        elif (pd.notna(rsi) and STRONG_ENTRY_RSI_LO <= rsi <= STRONG_ENTRY_RSI_HI
              and (near_ma20 or near_ma50) and pd.notna(ma50) and price > ma50):
            tgt = "20일선" if near_ma20 else "50일선"
            reasons.append(f"RSI {rsi:.0f}(강세 지지대) + 상승 {tgt} 눌림")
            out.update({"verdict": "🎯 매수 적기", "code": "entry", "reasons": reasons, "is_entry": True})
        else:
            reasons.append(f"강세 유지 · RSI {rsi:.0f}(아직 안 식음)" if pd.notna(rsi) else "강세 유지")
            out.update({"verdict": "⏳ 눌림 대기", "code": "wait", "reasons": reasons})

    elif regime == "sideways":
        if regime_res.get("topping"):
            reasons.append("천장(Stage3) 신호 — 신규 진입 자제")
            out.update({"verdict": "⚠️ 천장 주의", "code": "trend_break", "reasons": reasons})
        elif pd.notna(rsi) and rsi <= SIDEWAYS_ENTRY_RSI:
            reasons.append(f"RSI {rsi:.0f}(박스 하단 지지)")
            out.update({"verdict": "🎯 매수 적기(횡보 저점)", "code": "entry", "reasons": reasons, "is_entry": True})
        elif pd.notna(rsi) and rsi >= SIDEWAYS_OVERBOUGHT_RSI:
            reasons.append(f"RSI {rsi:.0f}(박스 상단)")
            out.update({"verdict": "⛔ 박스 상단", "code": "overheat", "reasons": reasons})
        else:
            out.update({"verdict": "⏳ 관망(횡보)", "code": "wait", "reasons": ["뚜렷한 방향 없음"]})

    else:  # weak
        band_lo_w, band_hi_w = RSI_BAND["weak"]
        if pd.notna(rsi) and rsi >= band_hi_w:
            reasons.append(f"RSI {rsi:.0f}가 약세 저항 {band_hi_w:.0f} 회복 시도 — 레짐 전환 관찰")
            out.update({"verdict": "👀 약세 · 전환 관찰", "code": "avoid", "reasons": reasons})
        else:
            out.update({"verdict": "🚫 약세 회피", "code": "avoid", "reasons": ["200일선 아래/하락 추세"]})

    return out


# ──────────────────────────────────────────────────────────────────────────
# 3) 청산 신호 (v1: RSI 과열 + 50일선 이탈 + 샹들리에 트레일링)
#    reasons 리스트 구조 → v2(네거티브 리버설) additive
# ──────────────────────────────────────────────────────────────────────────

def compute_exit_signals(hist: pd.DataFrame, entry_price: float | None = None) -> dict:
    """보유 종목 청산 신호. 강세 추세가 꺾이는지 점검.

    반환 dict: {is_exit: bool, reasons: [...], detail: {...}}
    """
    out = {"is_exit": False, "reasons": [], "detail": {}}
    if hist is None or hist.empty or "Close" not in hist.columns:
        return out
    close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
    if len(close) < 50:
        return out

    price = float(close.iloc[-1])
    ma50 = _ma_last(close, 50)
    rsi = compute_rsi(close)
    rsi_last = float(rsi.dropna().iloc[-1]) if not rsi.dropna().empty else np.nan
    atr = compute_atr(hist, ATR_WINDOW)

    reasons: list[str] = []

    # 1) RSI 강세 과열
    if pd.notna(rsi_last) and rsi_last >= STRONG_OVERHEAT_RSI:
        reasons.append(f"RSI {rsi_last:.0f} ≥ {STRONG_OVERHEAT_RSI:.0f}(과열)")

    # 2) 50일선 종가 이탈
    if pd.notna(ma50) and price < ma50:
        reasons.append(f"50일선(${ma50:.2f}) 종가 이탈")

    # 3) 샹들리에(ATR 트레일링) 스톱
    chandelier = np.nan
    if pd.notna(atr) and atr > 0:
        if entry_price is not None and pd.notna(entry_price):
            # 진입 이후 최고가 추적 (entry_price 이후 구간 근사: 전체 룩백 최고가와 진입가 중 큰 값)
            hh = float(close.tail(max(CHANDELIER_LOOKBACK, 22)).max())
            ref_high = max(hh, float(entry_price))
        else:
            hh = float(close.tail(CHANDELIER_LOOKBACK).max())
            ref_high = hh
        chandelier = ref_high - CHANDELIER_ATR_MULT * atr
        if price < chandelier:
            reasons.append(f"ATR 트레일링 스톱(${chandelier:.2f}) 하회")

    # --- v2 seam: 네거티브 리버설 detector 를 여기 reasons 에 append 하면 끝 ---

    out["is_exit"] = len(reasons) > 0
    out["reasons"] = reasons
    out["detail"] = {"price": price, "ma50": ma50, "rsi": rsi_last,
                     "atr": atr, "chandelier": chandelier}
    return out


# ──────────────────────────────────────────────────────────────────────────
# 4) 편의 오케스트레이터 — app/automation 한 방 호출용
# ──────────────────────────────────────────────────────────────────────────

def analyze_ticker(hist: pd.DataFrame, spy_close=None, entry_price: float | None = None) -> dict:
    regime = classify_regime(hist, spy_close=spy_close)
    timing = evaluate_timing(hist, regime)
    exits = compute_exit_signals(hist, entry_price=entry_price)
    return {"regime": regime, "timing": timing, "exit": exits}
