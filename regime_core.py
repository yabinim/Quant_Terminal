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
# 표시 라벨 (직관적 단어 — app.py 배지/범례/툴팁이 이 한 곳을 공유)
# ──────────────────────────────────────────────────────────────────────────
REGIME_LABEL = {
    "strong":   "🟢 강세 (대장주)",
    "sideways": "🟡 횡보",
    "weak":     "🔴 약세",
    "unknown":  "⚪ 판정 불가",
}

# 워치리스트 압축 배지 (timing code → 짧고 직관적인 라벨)
TIMING_BADGE = {
    "entry":       "🎯 지금 매수 구간",
    "wait":        "⏳ 조금 기다리기",
    "overheat":    "🔴 과열(비쌈)",
    "trend_break": "⚠️ 추세 흔들림",
    "avoid":       "⛔ 약세 회피",
}
TOPPING_BADGE = "🔺 고점 신호"

# 범례/툴팁용 한 줄 설명
TIMING_HELP = {
    "entry":       "강세 종목이 눌려서 싸진 타이밍 — 분할 매수 고려",
    "wait":        "추세는 좋은데 아직 안 눌림(상대적으로 비쌈) — 눌림 대기",
    "overheat":    "너무 올라 지금 사면 고점 매수 위험 — 식을 때까지 대기",
    "trend_break": "상승 추세가 흔들리는 신호 — 신규 진입 자제",
    "avoid":       "하락/약세 추세 — 매수 대상 아님",
}
TOPPING_HELP = "상투 가능성 — 보유 중이면 비중 점검"


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
            out.update({"verdict": "⚠️ 추세 흔들림", "code": "trend_break", "reasons": reasons})
        elif pd.notna(rsi) and rsi >= STRONG_OVERHEAT_RSI:
            reasons.append(f"RSI {rsi:.0f} ≥ {STRONG_OVERHEAT_RSI:.0f}(강세 과열) — 식을 때까지 대기")
            out.update({"verdict": "🔴 과열(비쌈)", "code": "overheat", "reasons": reasons})
        elif (pd.notna(rsi) and STRONG_ENTRY_RSI_LO <= rsi <= STRONG_ENTRY_RSI_HI
              and (near_ma20 or near_ma50) and pd.notna(ma50) and price > ma50):
            tgt = "20일선" if near_ma20 else "50일선"
            reasons.append(f"RSI {rsi:.0f}(강세 지지대) + 상승 {tgt} 눌림")
            out.update({"verdict": "🎯 지금 매수 구간", "code": "entry", "reasons": reasons, "is_entry": True})
        else:
            reasons.append(f"강세 유지 · RSI {rsi:.0f}(아직 안 식음)" if pd.notna(rsi) else "강세 유지")
            out.update({"verdict": "⏳ 조금 기다리기", "code": "wait", "reasons": reasons})

    elif regime == "sideways":
        # 박스 하단 검증: 200일선 위에서만 "박스 저점"으로 인정 (하락 추세 위장 차단)
        ma200 = c.get("ma200")
        above_ma200 = pd.notna(ma200) and pd.notna(price) and price > ma200
        if regime_res.get("topping"):
            reasons.append("천장(Stage3) 신호 — 신규 진입 자제")
            out.update({"verdict": "🔺 고점 신호 주의", "code": "trend_break", "reasons": reasons})
        elif pd.notna(rsi) and rsi <= SIDEWAYS_ENTRY_RSI and above_ma200:
            reasons.append(f"RSI {rsi:.0f}(박스 하단 지지) · 200일선 위")
            out.update({"verdict": "🎯 지금 매수 구간 (횡보 저점)", "code": "entry", "reasons": reasons, "is_entry": True})
        elif pd.notna(rsi) and rsi <= SIDEWAYS_ENTRY_RSI:
            reasons.append(f"RSI {rsi:.0f} 저점이지만 200일선 아래 — 박스 하단 아님(하락 추세 의심)")
            out.update({"verdict": "⚠️ 추세 흔들림", "code": "trend_break", "reasons": reasons})
        elif pd.notna(rsi) and rsi >= SIDEWAYS_OVERBOUGHT_RSI:
            reasons.append(f"RSI {rsi:.0f}(박스 상단)")
            out.update({"verdict": "🔴 박스 상단(비쌈)", "code": "overheat", "reasons": reasons})
        else:
            out.update({"verdict": "⏳ 관망 (횡보)", "code": "wait", "reasons": ["뚜렷한 방향 없음"]})

    else:  # weak
        band_lo_w, band_hi_w = RSI_BAND["weak"]
        if pd.notna(rsi) and rsi >= band_hi_w:
            reasons.append(f"RSI {rsi:.0f}가 약세 저항 {band_hi_w:.0f} 회복 시도 — 레짐 전환 관찰")
            out.update({"verdict": "👀 약세 · 전환 관찰", "code": "avoid", "reasons": reasons})
        else:
            out.update({"verdict": "⛔ 약세 회피", "code": "avoid", "reasons": ["200일선 아래/하락 추세"]})

    return out


# ──────────────────────────────────────────────────────────────────────────
# 3) 청산 신호 (v1: RSI 과열 + 50일선 이탈 + 샹들리에 트레일링)
#    reasons 리스트 구조 → v2(네거티브 리버설) additive
# ──────────────────────────────────────────────────────────────────────────

# 네거티브 리버설(Cardwell) — 조기 토핑 경고 파라미터
NEG_REV_PIVOT_N = 3       # 스윙 고점 피벗: 좌우 N봉보다 높아야
NEG_REV_LOOKBACK = 120    # 최근 N봉 내에서 고점 2개 탐색
NEG_REV_RSI_MIN = 55.0    # 두 고점 모두 RSI 이 값 이상일 때만(상단에서만 의미)
NEG_REV_MIN_GAP = 5       # 두 고점 최소 간격(봉)


def _find_swing_highs(values, pivot_n: int) -> list:
    """좌우 pivot_n 봉보다 엄격히 높은 스윙 고점 인덱스 목록(우측 pivot_n 봉으로 확정)."""
    n = len(values)
    out = []
    for i in range(pivot_n, n - pivot_n):
        c = values[i]
        if not np.isfinite(c):
            continue
        left = values[i - pivot_n:i]
        right = values[i + 1:i + pivot_n + 1]
        if left.size and right.size and c > np.nanmax(left) and c > np.nanmax(right):
            out.append(i)
    return out


def _detect_negative_reversal(close, rsi, pivot_n: int = NEG_REV_PIVOT_N,
                              lookback: int = NEG_REV_LOOKBACK,
                              rsi_min: float = NEG_REV_RSI_MIN,
                              min_gap: int = NEG_REV_MIN_GAP) -> dict:
    """Cardwell 네거티브 리버설: 가격은 더 낮은 고점인데 RSI는 더 높은 고점 → 토핑 경고.
    노이즈 억제: 두 고점 RSI ≥ rsi_min, 간격 ≥ min_gap, 현재가·RSI가 2번째 고점에서 꺾임(확정).
    반환: {detected, message, detail}."""
    out = {"detected": False, "message": "", "detail": {}}
    try:
        df = pd.concat([pd.to_numeric(close, errors="coerce"),
                        pd.to_numeric(rsi, errors="coerce")], axis=1).dropna()
    except Exception:
        return out
    if len(df) < (pivot_n * 2 + min_gap + 2):
        return out
    df = df.tail(lookback)
    cv = df.iloc[:, 0].to_numpy(dtype=float)
    rv = df.iloc[:, 1].to_numpy(dtype=float)
    highs = _find_swing_highs(cv, pivot_n)
    if len(highs) < 2:
        return out
    p1, p2 = highs[-2], highs[-1]
    if (p2 - p1) < min_gap:
        return out
    price_lower_high = cv[p2] < cv[p1]
    rsi_higher_high = rv[p2] > rv[p1]
    upper_range = (rv[p1] >= rsi_min) and (rv[p2] >= rsi_min)
    confirmed = (cv[-1] < cv[p2]) and (rv[-1] < rv[p2])   # 2번째 고점에서 꺾임
    if price_lower_high and rsi_higher_high and upper_range and confirmed:
        out["detected"] = True
        out["message"] = (f"네거티브 리버설: 고점 ${cv[p1]:.2f}→${cv[p2]:.2f}(낮아짐)인데 "
                          f"RSI {rv[p1]:.0f}→{rv[p2]:.0f}(높아짐) — 토핑 경고")
        out["detail"] = {"high1": float(cv[p1]), "high2": float(cv[p2]),
                         "rsi1": float(rv[p1]), "rsi2": float(rv[p2])}
    return out


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

    # --- v2: 네거티브 리버설 (조기 경고 — is_exit 미반영, 거짓 청산 방지) ---
    warnings: list[str] = []
    neg_rev = _detect_negative_reversal(close, rsi)
    if neg_rev["detected"]:
        warnings.append(neg_rev["message"])

    out["is_exit"] = len(reasons) > 0
    out["reasons"] = reasons
    out["warnings"] = warnings
    out["detail"] = {"price": price, "ma50": ma50, "rsi": rsi_last,
                     "atr": atr, "chandelier": chandelier,
                     "negative_reversal": bool(neg_rev["detected"])}
    return out


# ──────────────────────────────────────────────────────────────────────────
# 4) 편의 오케스트레이터 — app/automation 한 방 호출용
# ──────────────────────────────────────────────────────────────────────────

def analyze_ticker(hist: pd.DataFrame, spy_close=None, entry_price: float | None = None) -> dict:
    regime = classify_regime(hist, spy_close=spy_close)
    timing = evaluate_timing(hist, regime)
    exits = compute_exit_signals(hist, entry_price=entry_price)
    return {"regime": regime, "timing": timing, "exit": exits}


# ──────────────────────────────────────────────────────────────────────────
# 5) 상태 기반 알림 (전환 감지 + 2일 확정 + 재무장) — 순수, app/automation 공유
# ──────────────────────────────────────────────────────────────────────────

import json as _json

ALERT_CONFIRM_DAYS = 2
ALERT_EVENTS = ("entry", "regime", "risk", "exit", "price", "watch")
ALERT_EVENT_LABELS = {
    "entry":  "🟢 매수 신호",
    "regime": "🔄 레짐 전환",
    "risk":   "🟡 줄이기 (추세 흔들림·리버설)",
    "exit":   "🔴 청산",
    "price":  "📌 손절/목표 도달",
    "watch":  "🎯 관심가 도달 (목표가·RSI·200일선)",
    "entry_invalid": "🚫 매수 신호 무효화",
}
_REGIME_KR = {"strong": "🟢 강세", "sideways": "🟡 횡보", "weak": "🔴 약세"}


def _isna(x) -> bool:
    try:
        return x is None or pd.isna(x)
    except Exception:
        return x is None


def resolve_alert_events(raw, default_csv: str = "entry,risk") -> list:
    """저장된 alert_states(문자열 또는 리스트) → 실제 적용할 이벤트 리스트.

    규칙 (app.py·automation 공유 SSOT):
      - "" / 미설정 / 빈 리스트  → 기본값(default_csv)   (신규·마이그레이션 종목)
      - "none"                    → []  (사용자가 알림 전부 해제)
      - "entry,risk" 등           → 유효 이벤트만 파싱
    """
    if isinstance(raw, (list, tuple)):
        toks = [str(s).strip() for s in raw if str(s).strip()]
    else:
        toks = [s.strip() for s in str(raw or "").split(",") if s.strip()]
    if not toks:
        return [s.strip() for s in default_csv.split(",") if s.strip()]
    if toks == ["none"]:
        return []
    return [t for t in toks if t in ALERT_EVENTS]


def watch_condition_msgs(price=None, rsi=None, ma200=None,
                         alert_price=None, alert_rsi=None, alert_ma200=False) -> list:
    """수동 관심 조건(목표 매수가·RSI·200일선 근접) 충족 메시지 리스트.

    app.py 인앱 배너(check_watchlist_alerts) · automation 이메일 레이더(watch 이벤트)
    공유 SSOT. 어느 하나라도 충족 시 해당 메시지를 담아 반환(빈 리스트 = 미충족).
    임계값은 기존 app.py 인앱 체크와 동일하게 유지.
    """
    msgs = []
    if not _isna(alert_price) and not _isna(price) and float(price) <= float(alert_price):
        msgs.append(f"💰 목표가 도달: 현재 ${float(price):.2f} ≤ 설정 ${float(alert_price):.2f}")
    if not _isna(alert_rsi) and not _isna(rsi) and float(rsi) <= float(alert_rsi):
        msgs.append(f"📉 RSI 과매도: 현재 RSI {float(rsi):.1f} ≤ 설정 {float(alert_rsi):.1f}")
    if alert_ma200 and not _isna(price) and not _isna(ma200) and float(ma200) > 0:
        gap_pct = (float(price) / float(ma200) - 1.0) * 100
        if abs(gap_pct) <= 3.0:
            msgs.append(
                f"📊 200일선 근접: 현재가 ${float(price):.2f} / 200일선 ${float(ma200):.2f} (괴리 {gap_pct:+.1f}%)"
            )
    return msgs


def alert_conditions(analysis: dict, price=None, stop_loss=None, target_price=None,
                     alert_price=None, alert_rsi=None, alert_ma200=False) -> dict:
    """현재 시점의 이벤트별 (조건 bool, 설명) 산출. regime 은 별도 처리."""
    reg = analysis.get("regime", {}) or {}
    tim = analysis.get("timing", {}) or {}
    exi = analysis.get("exit", {}) or {}
    conds = {}
    conds["entry"] = (bool(tim.get("is_entry")), tim.get("verdict", ""))
    neg_rev = bool(exi.get("detail", {}).get("negative_reversal")) or bool(exi.get("warnings"))
    risk_on = ((tim.get("code") == "trend_break") or bool(reg.get("topping"))
               or (reg.get("regime") == "weak") or neg_rev)
    _risk_msgs = list(tim.get("reasons", []))
    if neg_rev:
        _risk_msgs += list(exi.get("warnings", []))
    conds["risk"] = (bool(risk_on), " · ".join([m for m in _risk_msgs if m]) or "추세 약화")
    conds["exit"] = (bool(exi.get("is_exit")), " · ".join(exi.get("reasons", [])))
    price_on, price_msg = False, ""
    if not _isna(price):
        if not _isna(stop_loss) and float(price) <= float(stop_loss):
            price_on, price_msg = True, f"손절가 ${float(stop_loss):.2f} 도달(현재 ${float(price):.2f})"
        elif not _isna(target_price) and float(price) >= float(target_price):
            price_on, price_msg = True, f"목표가 ${float(target_price):.2f} 도달(현재 ${float(price):.2f})"
    conds["price"] = (price_on, price_msg)
    # 수동 관심 조건(watch): 목표 매수가·RSI·200일선 근접 — 공유 SSOT
    _comp = reg.get("components") or {}
    _wmsgs = watch_condition_msgs(
        price=price, rsi=_comp.get("rsi"), ma200=_comp.get("ma200"),
        alert_price=alert_price, alert_rsi=alert_rsi, alert_ma200=alert_ma200,
    )
    conds["watch"] = (bool(_wmsgs), " · ".join(_wmsgs))
    return conds


def evaluate_alert_transitions(analysis: dict, enabled_events, last_state_json: str = "",
                               today_str: str = "", price=None, stop_loss=None,
                               target_price=None, confirm_days: int = ALERT_CONFIRM_DAYS,
                               alert_price=None, alert_rsi=None, alert_ma200=False):
    """상태 전환 기반 알림 평가 (2일 확정 + 재무장). 순수 함수.

    ※ 하루 1회 호출 전제(자동화). 호출 1회 = 평가 1회로 pending 카운터가 1 진행된다.
       앱(rerun마다 호출)에서는 절대 호출하지 말 것 — 미리보기는 alert_conditions 사용.

    반환: (fired: list[{event,label,message}], new_last_state_json: str)
    """
    try:
        state = _json.loads(last_state_json) if last_state_json else {}
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    events_state = state.get("events", {}) or {}
    baseline_regime = state.get("regime")

    reg = analysis.get("regime", {}) or {}
    cur_regime = reg.get("regime") if reg.get("enough_data") else None

    enabled = set(enabled_events or [])
    conds = alert_conditions(analysis, price, stop_loss, target_price,
                             alert_price=alert_price, alert_rsi=alert_rsi, alert_ma200=alert_ma200)
    fired = []

    # 일반 이벤트: 조건 지속 + confirm_days 확정 + 재무장(조건 해제 시)
    for e in ("entry", "risk", "exit", "price", "watch"):
        if e not in enabled:
            events_state[e] = {"status": "armed", "pending": 0}
            continue
        cond, msg = conds.get(e, (False, ""))
        s = events_state.get(e) or {"status": "armed", "pending": 0}
        status = s.get("status", "armed")
        pending = int(s.get("pending", 0) or 0)
        if cond:
            if status == "armed":
                pending += 1
                if pending >= confirm_days:
                    fired.append({"event": e, "label": ALERT_EVENT_LABELS.get(e, e), "message": msg})
                    status, pending = "fired", 0
            # fired 면 유지(재발동 금지)
        else:
            # D-2: 발동됐던 매수 신호의 조건 해제 → 무효화 알림 1회 (재무장 전환 시점에만)
            if e == "entry" and status == "fired":
                fired.append({
                    "event": "entry_invalid",
                    "label": ALERT_EVENT_LABELS["entry_invalid"],
                    "message": (f"직전 매수 신호 조건 해제 — 현재 판정: {msg}"
                                if msg else "직전 매수 신호 조건 해제 — 재평가 필요"),
                })
            status, pending = "armed", 0  # 재무장
        events_state[e] = {"status": status, "pending": pending}

    # 레짐 전환: baseline 대비 변경 + 확정 → 발동 후 baseline 갱신(자동 재무장)
    if cur_regime is not None:
        if baseline_regime is None:
            baseline_regime = cur_regime  # 최초 기준점, 발동 안 함
            events_state["regime"] = {"cand": None, "pending": 0}
        elif "regime" in enabled and cur_regime != baseline_regime:
            rs = events_state.get("regime") or {"cand": None, "pending": 0}
            if rs.get("cand") == cur_regime:
                rs["pending"] = int(rs.get("pending", 0) or 0) + 1
            else:
                rs = {"cand": cur_regime, "pending": 1}
            if rs["pending"] >= confirm_days:
                fired.append({
                    "event": "regime", "label": ALERT_EVENT_LABELS["regime"],
                    "message": f"{_REGIME_KR.get(baseline_regime, baseline_regime)} → {_REGIME_KR.get(cur_regime, cur_regime)}",
                })
                baseline_regime = cur_regime
                rs = {"cand": None, "pending": 0}
            events_state["regime"] = rs
        else:
            events_state["regime"] = {"cand": None, "pending": 0}

    new_state = {"regime": baseline_regime, "events": events_state, "ts": today_str}
    return fired, _json.dumps(new_state, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────────
# 6) 포지션 사이징 · R:R 게이트 (#2) — 순수, app/automation 공유 SSOT
#    철학(백테스트 기반): 손실 회피 우선. avoid=음알파→진입 비추, entry=약한 +알파→정상 사이즈.
#    거래당 리스크 고정(고정 분율). 손절/목표 '가격'은 app.suggest_stop_and_target 가 공급,
#    국면 적응 배수/손익비는 app._regime_params 가 공급 → 여기서 '조합'만(드리프트 방지).
# ──────────────────────────────────────────────────────────────────────────

# 게이트 코드 → 라벨 (UI 공유)
GATE_LABELS = {
    "fit":     "✅ 진입 적합",
    "skip":    "⚠️ 자리 나쁨 — 건너뛰기",
    "avoid":   "⛔ 진입 비추 — 회피 구간",
    "caution": "🔶 신중 — 분할·관망 고려",
    "na":      "⚪ 판단 보류(데이터 부족)",
}
DEFAULT_RISK_PCT = 1.0       # 거래당 자본 대비 리스크 %
DEFAULT_MAX_POSITION_PCT = 20.0  # 단일 종목 최대 비중 %
DEFAULT_MAX_POSITIONS = 5        # 계좌 동시 보유 종목 수(슬롯) 기본값
DEFAULT_RESERVE_PCT = 0.0        # 예비 현금 비율 % (0 = 미사용)
DEFAULT_MIN_TRADE_DOLLARS = 0.0  # 이 금액 미만이면 집행 무의미 (0 = 미사용)

# 투입 금액을 최종적으로 결정한 제약(=binding constraint) 라벨
BINDING_LABELS = {
    "risk":    "리스크 기준",
    "cap":     "비중 상한",
    "reserve": "투자 여유",
    "cash":    "가용 현금",
    "equal":   "균등 배분",
    "off":     "사이징 미사용",
    "none":    "-",
}

# 계좌별 사이징 모드 — Account_Profile.Sizing_Mode 와 동일 코드계 (SSOT)
SIZING_MODES = ("risk_based", "equal_weight", "off")
SIZING_MODE_DEFAULT = "risk_based"


def evaluate_rr(entry, stop, target) -> dict:
    """손익비(R:R) 산출. target 은 진입가보다 위여야 의미 있음.
    반환: {risk, reward, r_multiple, label}. 산출 불가 시 NaN/"-"."""
    out = {"risk": np.nan, "reward": np.nan, "r_multiple": np.nan, "label": "-"}
    try:
        e, s, t = float(entry), float(stop), float(target)
    except (TypeError, ValueError):
        return out
    if not (np.isfinite(e) and np.isfinite(s) and np.isfinite(t)):
        return out
    if e <= 0 or s >= e or t <= e:        # 손절은 진입 아래, 목표는 진입 위
        return out
    risk = e - s
    reward = t - e
    r = reward / risk if risk > 0 else np.nan
    out.update({"risk": risk, "reward": reward, "r_multiple": r,
                "label": (f"1:{r:.1f}" if np.isfinite(r) else "-")})
    return out


def position_size(equity, risk_pct, entry, stop,
                  max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
                  cash=None, reserve_pct: float = DEFAULT_RESERVE_PCT,
                  invested_value: float = 0.0,
                  slots_used=None, max_positions=None,
                  min_trade_dollars: float = DEFAULT_MIN_TRADE_DOLLARS,
                  sizing_mode: str = SIZING_MODE_DEFAULT) -> dict:
    """고정 분율 리스크 사이징 — **금액(dollars)이 불변량, 주(shares)는 파생값**.

    설계 원칙(중요):
      floor() 를 계산 마지막이 아니라 *표시 직전*에만 적용한다. 소수점 매수 사용자는
      dollars/shares_exact 를, 정수주만 가능한 사용자는 shares_whole 을 쓴다.
      먼저 floor 하면 금액을 복원할 수 없어 소액 계좌에서 결과가 0으로 파괴된다.

    제약 4종 중 가장 작은 값이 투입 금액을 결정하고, 무엇이 결정했는지를 binding 으로 돌려준다.
      risk    : equity × risk_pct% ÷ 손절폭%      (리스크 고정)
      cap     : equity × max_position_pct%        (단일 종목 집중도)
      reserve : equity × (1-reserve_pct%) - invested_value  (예비 현금 확보)
      cash    : cash                              (실제 집행 가능액)

    전환점: 손절폭% > risk_pct ÷ max_position_pct 이면 risk 가, 아니면 cap 이 결정한다.
      예) 3%/20% → 15% · 1%/20% → 5%

    슬롯(max_positions/slots_used)과 min_trade_dollars 는 금액을 깎지 않고 blocked 플래그로만
    알린다 — 금액은 그대로 보여주되 "지금 집행하면 안 되는 이유"를 함께 준다.

    sizing_mode (계좌 전략별 분기):
      risk_based   : 위 4종 제약 (신호별 진입 계좌)
      equal_weight : 투자가능액 ÷ 슬롯 = 균등 몫. 비중 상한·현금 제약은 그대로 적용.
                     기계적 회전 계좌용 — 손절폭이 금액에 영향을 주지 않는다.
      off          : 사이징 미사용. dollars=0, binding="off" 로 반환하고 소비처가 표시를 생략한다.

    반환 키(기존 5개는 하위호환 유지):
      shares(=shares_whole) · dollars · risk_dollars · position_pct · capped(=binding=='cap')
      shares_whole · shares_exact · binding · binding_label · limits
      slots_used · max_positions · slots_full · below_min · whole_share_ok · blocked · block_reason
    """
    out = {
        "shares": 0, "shares_whole": 0, "shares_exact": 0.0,
        "dollars": 0.0, "risk_dollars": 0.0, "position_pct": 0.0,
        "capped": False, "binding": "none", "binding_label": BINDING_LABELS["none"],
        "limits": {}, "slots_used": slots_used, "max_positions": max_positions,
        "slots_full": False, "below_min": False, "whole_share_ok": False,
        "blocked": False, "block_reason": "", "sizing_mode": SIZING_MODE_DEFAULT,
    }
    try:
        eq, rp, e, s = float(equity), float(risk_pct), float(entry), float(stop)
        mp = float(max_position_pct)
    except (TypeError, ValueError):
        return out
    if not all(np.isfinite(v) for v in (eq, rp, e, s, mp)):
        return out
    if eq <= 0 or rp <= 0 or e <= 0 or s >= e:
        return out

    risk_per_share = e - s
    stop_frac = risk_per_share / e            # 손절폭 (0 < frac < 1)
    if not (np.isfinite(stop_frac) and stop_frac > 0):
        return out

    mode = str(sizing_mode or SIZING_MODE_DEFAULT).strip().lower()
    if mode not in SIZING_MODES:
        mode = SIZING_MODE_DEFAULT
    out["sizing_mode"] = mode
    if mode == "off":
        out.update({"binding": "off", "binding_label": BINDING_LABELS["off"]})
        return out

    # ── 제약 후보 산출 (삽입 순서 = 동점 시 우선순위) ────────────────────
    if mode == "equal_weight":
        try:
            _slots = int(max_positions) if max_positions else int(DEFAULT_MAX_POSITIONS)
        except (TypeError, ValueError):
            _slots = int(DEFAULT_MAX_POSITIONS)
        _slots = max(_slots, 1)
        try:
            _rv0 = float(reserve_pct)
        except (TypeError, ValueError):
            _rv0 = 0.0
        if not (np.isfinite(_rv0) and _rv0 >= 0):
            _rv0 = 0.0
        limits = {"equal": max(eq * (1.0 - _rv0 / 100.0), 0.0) / _slots}
    else:
        limits = {"risk": (eq * (rp / 100.0)) / stop_frac}
    if np.isfinite(mp) and mp > 0:
        limits["cap"] = eq * (mp / 100.0)
    try:
        rv = float(reserve_pct)
    except (TypeError, ValueError):
        rv = 0.0
    if np.isfinite(rv) and rv > 0 and mode != "equal_weight":
        # equal_weight 는 균등 몫에서 예비금을 이미 차감 → 중복 적용 금지
        try:
            iv = float(invested_value)
        except (TypeError, ValueError):
            iv = 0.0
        if not (np.isfinite(iv) and iv >= 0):
            iv = 0.0
        limits["reserve"] = max(eq * (1.0 - rv / 100.0) - iv, 0.0)
    if cash is not None:
        try:
            cv = float(cash)
        except (TypeError, ValueError):
            cv = np.nan
        if np.isfinite(cv) and cv >= 0:
            limits["cash"] = cv

    binding = min(limits, key=lambda k: limits[k])
    dollars = max(float(limits[binding]), 0.0)

    shares_exact = dollars / e
    shares_whole = int(np.floor(shares_exact))
    if shares_whole < 0:
        shares_whole = 0
    risk_dollars = dollars * stop_frac

    # ── 슬롯 / 최소금액 (금액은 깎지 않고 플래그만) ───────────────────────
    slots_full = False
    try:
        if max_positions is not None and slots_used is not None:
            _mx, _us = int(max_positions), int(slots_used)
            if _mx > 0 and _us >= _mx:
                slots_full = True
    except (TypeError, ValueError):
        slots_full = False
    try:
        _min = float(min_trade_dollars)
    except (TypeError, ValueError):
        _min = 0.0
    below_min = bool(np.isfinite(_min) and _min > 0 and 0.0 < dollars < _min)

    reasons = []
    if slots_full:
        reasons.append(f"슬롯 만석 ({slots_used}/{max_positions}) — 최약 보유와 교체 검토")
    if dollars <= 0:
        reasons.append(f"{BINDING_LABELS.get(binding, binding)} 여유 없음")
    if below_min:
        reasons.append(f"최소 거래금액 ${_min:,.0f} 미만")

    out.update({
        "shares": shares_whole,          # 하위호환 (기존 소비처는 정수주를 기대)
        "shares_whole": shares_whole,
        "shares_exact": round(shares_exact, 4),
        "dollars": round(dollars, 2),
        "risk_dollars": round(risk_dollars, 2),
        "position_pct": round(dollars / eq * 100.0, 2),
        "capped": (binding == "cap"),
        "binding": binding,
        "binding_label": BINDING_LABELS.get(binding, binding),
        "limits": {k: round(float(v), 2) for k, v in limits.items()},
        "slots_full": slots_full,
        "below_min": below_min,
        "whole_share_ok": shares_whole >= 1,
        "blocked": bool(reasons),
        "block_reason": " · ".join(reasons),
    })
    return out


def resolve_stop(entry, atr, ma200, atr_mult, source: str = "atr", manual_stop=None):
    """손절가 결정. source: 'atr'|'ma200'|'manual'. 반환: (stop, used_source).
    유효(진입 아래·양수) 못 만들면 (NaN, source)."""
    try:
        e = float(entry)
    except (TypeError, ValueError):
        return np.nan, source
    stop = np.nan
    used = source
    if source == "manual" and manual_stop is not None and np.isfinite(float(manual_stop)):
        stop = float(manual_stop)
    elif source == "ma200" and ma200 is not None and np.isfinite(float(ma200)) and float(ma200) > 0:
        stop = float(ma200) * 0.98
    else:  # atr 기본
        try:
            a, m = float(atr), float(atr_mult)
            if np.isfinite(a) and a > 0 and np.isfinite(m):
                stop = e - m * a
                used = "atr"
        except (TypeError, ValueError):
            stop = np.nan
    if not (np.isfinite(stop) and 0 < stop < e):
        return np.nan, used
    return stop, used


def resolve_target(entry, risk_per_share, rr_target, recent_high=None, manual_target=None):
    """목표가 결정 (우선순위: 수동 > 구조적 고점 > R:R 파생).
    반환: (target, basis). basis: 'manual'|'structural_high'|'rr_derived'|'na'.
    rr_derived 는 R:R 게이트의 '실제 필터'로 쓰지 않음(자기참조)."""
    try:
        e = float(entry)
    except (TypeError, ValueError):
        return np.nan, "na"
    if manual_target is not None:
        try:
            mt = float(manual_target)
            if np.isfinite(mt) and mt > e:
                return mt, "manual"
        except (TypeError, ValueError):
            pass
    if recent_high is not None:
        try:
            rh = float(recent_high)
            rps = float(risk_per_share)
            # 최근 고점은 '의미 있는' 목표일 때만 사용: 진입 위로 최소 1R(손절거리) 이상.
            # (신고가 부근이라 고점이 코앞이면 +0.5% 같은 허수 목표 → R:R 파생으로 폴백)
            if (np.isfinite(rh) and rh > e and np.isfinite(rps) and rps > 0
                    and (rh - e) >= rps):
                return rh, "structural_high"
        except (TypeError, ValueError):
            pass
    try:
        rps, rr = float(risk_per_share), float(rr_target)
        if np.isfinite(rps) and rps > 0 and np.isfinite(rr) and rr > 0:
            return e + rr * rps, "rr_derived"
    except (TypeError, ValueError):
        pass
    return np.nan, "na"


def build_trade_plan(verdict_code, entry, atr, ma200, equity, risk_pct, atr_mult, rr_target,
                     stop_source: str = "atr", manual_stop=None, manual_target=None,
                     recent_high=None, max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
                     cash=None, reserve_pct: float = DEFAULT_RESERVE_PCT,
                     invested_value: float = 0.0,
                     slots_used=None, max_positions=None,
                     min_trade_dollars: float = DEFAULT_MIN_TRADE_DOLLARS,
                     sizing_mode: str = SIZING_MODE_DEFAULT) -> dict:
    """verdict + 손절/목표 + 자본 → 게이트 판정 + 사이즈 일괄. 순수 조합 함수.

    게이트(백테스트 반영):
      avoid → 회피(사이즈 0 권고) · entry/wait & R:R≥목표 → 적합 · 좋아도 R:R<목표 → 건너뛰기
      overheat/trend_break → 신중. (R:R 필터는 독립 목표가 있을 때만; rr_derived 면 정보용)
    """
    plan = {
        "entry": np.nan, "stop": np.nan, "stop_source": stop_source, "stop_pct": np.nan,
        "target": np.nan, "target_basis": "na", "target_pct": np.nan,
        "risk_per_share": np.nan, "r_multiple": np.nan, "rr_label": "-",
        "shares": 0, "dollars": 0.0, "risk_dollars": 0.0, "position_pct": 0.0, "capped": False,
        "shares_whole": 0, "shares_exact": 0.0, "binding": "none",
        "binding_label": BINDING_LABELS["none"], "limits": {},
        "slots_used": slots_used, "max_positions": max_positions,
        "slots_full": False, "below_min": False, "whole_share_ok": False,
        "blocked": False, "block_reason": "", "rr_measured": False,
        "sizing_mode": SIZING_MODE_DEFAULT,
        "gate": "na", "gate_label": GATE_LABELS["na"], "gate_reason": "",
        "atr_mult": atr_mult, "rr_target": rr_target, "enter_ok": False,
    }
    try:
        e = float(entry)
    except (TypeError, ValueError):
        return plan
    if not (np.isfinite(e) and e > 0):
        return plan
    plan["entry"] = e

    stop, used_src = resolve_stop(e, atr, ma200, atr_mult, stop_source, manual_stop)
    plan["stop"], plan["stop_source"] = stop, used_src
    if not np.isfinite(stop):
        plan["gate_reason"] = "손절가 산출 불가(ATR/200MA/입력 확인)"
        return plan
    plan["stop_pct"] = round((stop / e - 1.0) * 100.0, 2)
    risk_ps = e - stop
    plan["risk_per_share"] = round(risk_ps, 4)

    target, basis = resolve_target(e, risk_ps, rr_target, recent_high, manual_target)
    plan["target"], plan["target_basis"] = target, basis
    if np.isfinite(target):
        plan["target_pct"] = round((target / e - 1.0) * 100.0, 2)
    rr = evaluate_rr(e, stop, target)
    plan["r_multiple"], plan["rr_label"] = rr["r_multiple"], rr["label"]

    sz = position_size(equity, risk_pct, e, stop, max_position_pct,
                       cash=cash, reserve_pct=reserve_pct, invested_value=invested_value,
                       slots_used=slots_used, max_positions=max_positions,
                       min_trade_dollars=min_trade_dollars, sizing_mode=sizing_mode)
    plan.update({k: sz[k] for k in (
        "shares", "shares_whole", "shares_exact", "dollars", "risk_dollars",
        "position_pct", "capped", "binding", "binding_label", "limits",
        "slots_used", "max_positions", "slots_full", "below_min",
        "whole_share_ok", "blocked", "block_reason", "sizing_mode")})

    # ── 게이트 판정 ──────────────────────────────────────────────────────
    code = str(verdict_code or "")
    rr_val = plan["r_multiple"]
    rr_is_real = (basis in ("manual", "structural_high")) and np.isfinite(rr_val)
    plan["rr_measured"] = bool(rr_is_real)   # 신호 품질 바: 독립 목표 기반 R:R 실측 여부

    if code == "avoid":
        plan.update({"gate": "avoid", "gate_label": GATE_LABELS["avoid"],
                     "gate_reason": "약세 회피 구간 — 백테스트상 음(-)의 초과수익", "enter_ok": False})
    elif code in ("entry", "wait"):
        try:
            bar = float(rr_target)
        except (TypeError, ValueError):
            bar = np.nan
        if rr_is_real and np.isfinite(bar) and rr_val < bar:
            plan.update({"gate": "skip", "gate_label": GATE_LABELS["skip"],
                         "gate_reason": f"R:R {plan['rr_label']} < 목표 1:{bar:.1f} — 손익비 부족",
                         "enter_ok": False})
        else:
            note = "독립 목표 미설정(R:R 정보용)" if not rr_is_real else f"R:R {plan['rr_label']} 충족"
            plan.update({"gate": "fit", "gate_label": GATE_LABELS["fit"],
                         "gate_reason": note, "enter_ok": True})
    elif code in ("overheat", "trend_break"):
        plan.update({"gate": "caution", "gate_label": GATE_LABELS["caution"],
                     "gate_reason": "과열/추세 흔들림 — 분할 진입·관망 권장", "enter_ok": False})
    else:
        plan.update({"gate": "na", "gate_label": GATE_LABELS["na"],
                     "gate_reason": "타이밍 판정 데이터 부족", "enter_ok": False})

    return plan


def regime_params(drg: dict) -> dict:
    """DRG risk_score(경고 개수 0~8) → 손절 ATR배수·손익비·라벨.
    app.py _regime_params 에서 이관(SSOT, v2 게이트) — app 은 알리아스로 참조.
    점수가 없으면 risk_level 텍스트(영문 HIGH/CAUTION/MODERATE/LOW 또는 한글)로 폴백.
    """
    d = drg or {}
    s = None
    try:
        s = float(d.get("risk_score"))
    except Exception:
        s = None
    if s is not None:
        if s >= 6:
            return {"atr_mult": 1.5, "rr": 1.5, "label": "🔴 위험 (방어)", "note": "손절 타이트 · 목표 짧게 · 신규 자제"}
        if s >= 4:
            return {"atr_mult": 1.8, "rr": 1.8, "label": "🟡 경계", "note": "보수적 운용"}
        if s >= 2:
            return {"atr_mult": 2.2, "rr": 2.5, "label": "🟢 양호", "note": "표준 운용"}
        return {"atr_mult": 2.5, "rr": 3.0, "label": "🟢 안전 (공격)", "note": "손절 넓게 · 러너 허용"}
    lvl = str(d.get("risk_level") or "").upper()
    if "HIGH" in lvl or "위험" in lvl:
        return {"atr_mult": 1.5, "rr": 1.5, "label": "🔴 위험 (방어)", "note": "손절 타이트 · 목표 짧게 · 신규 자제"}
    if "CAUTION" in lvl or "경계" in lvl:
        return {"atr_mult": 1.8, "rr": 1.8, "label": "🟡 경계", "note": "보수적 운용"}
    if "MODERATE" in lvl:
        return {"atr_mult": 2.2, "rr": 2.5, "label": "🟢 양호", "note": "표준 운용"}
    if "LOW" in lvl or "안전" in lvl:
        return {"atr_mult": 2.5, "rr": 3.0, "label": "🟢 안전 (공격)", "note": "손절 넓게 · 러너 허용"}
    return {"atr_mult": 2.0, "rr": 2.0, "label": "⚪ 중립", "note": "기본 손익비 1:2"}


_WL_RECENT_HIGH_WINDOW = 120  # app.py [7] 워치리스트 탭과 동일(lockstep)


def build_watchlist_plan(hist, an: dict, manual_stop=None, manual_target=None,
                         entry=None, atr_mult=None, rr_target=None,
                         equity: float = 0.0, risk_pct: float = 1.0,
                         max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
                         cash=None, reserve_pct: float = DEFAULT_RESERVE_PCT,
                         invested_value: float = 0.0,
                         slots_used=None, max_positions=None,
                         min_trade_dollars: float = DEFAULT_MIN_TRADE_DOLLARS,
                         sizing_mode: str = SIZING_MODE_DEFAULT) -> dict:
    """워치리스트 진입 게이트용 트레이드 플랜 조립 — app.py [7] 탭과 동일 입력 규약(lockstep).

    app 조립부와의 대응: ATR=compute_atr(hist), ma200=analysis components,
    recent_high=최근 120일 High 최대, stop_source=수동 손절 있으면 manual 아니면 atr.
    entry 미지정 시 마지막 종가. atr_mult/rr_target 미지정 시 중립 국면(regime_params({})).
    equity=0 이면 사이징(주수)은 0으로 나오지만 게이트/R:R 판정에는 영향 없음(순수 분리).
    """
    if atr_mult is None or rr_target is None:
        _rp = regime_params({})
        atr_mult = _rp["atr_mult"] if atr_mult is None else atr_mult
        rr_target = _rp["rr"] if rr_target is None else rr_target
    an = an or {}
    code = (an.get("timing") or {}).get("code")
    comp = (an.get("regime") or {}).get("components") or {}
    ma200 = comp.get("ma200", np.nan)
    e = entry
    atr, recent_high = np.nan, None
    try:
        if hist is not None and not hist.empty and "Close" in hist.columns:
            close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
            if e is None and not close.empty:
                e = float(close.iloc[-1])
            atr = compute_atr(hist, ATR_WINDOW)
            hi = (pd.to_numeric(hist["High"], errors="coerce")
                  if "High" in hist.columns else close)
            hi = hi.dropna().tail(_WL_RECENT_HIGH_WINDOW)
            recent_high = float(hi.max()) if not hi.empty else None
    except Exception:
        pass
    ms = None
    try:
        ms = float(manual_stop) if manual_stop is not None and pd.notna(manual_stop) else None
    except (TypeError, ValueError):
        ms = None
    mt = None
    try:
        mt = float(manual_target) if manual_target is not None and pd.notna(manual_target) else None
    except (TypeError, ValueError):
        mt = None
    return build_trade_plan(
        verdict_code=code,
        entry=e,
        atr=atr,
        ma200=float(ma200) if pd.notna(ma200) else np.nan,
        equity=equity, risk_pct=risk_pct,
        atr_mult=atr_mult, rr_target=rr_target,
        stop_source=("manual" if ms is not None else "atr"),
        manual_stop=ms, manual_target=mt,
        recent_high=recent_high,
        max_position_pct=max_position_pct,
        cash=cash, reserve_pct=reserve_pct, invested_value=invested_value,
        slots_used=slots_used, max_positions=max_positions,
        min_trade_dollars=min_trade_dollars, sizing_mode=sizing_mode,
    )


def decorate_entry_alert(ev: dict, plan: dict, regime: str = "unknown") -> dict:
    """entry 알림 dict 에 R:R 게이트 결과를 반영(v2 — 이메일도 앱과 동일 게이트 판정).

    1B 방식: entry 이벤트/상태머신은 그대로 두고 라벨·메시지만 게이트로 구분.
      통과   → 🟢 매수 신호 (✅ 게이트 통과) + 손절/목표/R:R 라인
      미통과 → ⚠️ 매수 신호 — 게이트 미통과·건너뛰기 권고 + 사유
    plan 산출 불가(gate=na)면 정직하게 '게이트 판단 보류'로 표기(신호 억제 없음).
    """
    ev = ev or {}
    p = plan or {}
    dec = buy_decision("entry", p.get("gate"), regime)
    gate = str(p.get("gate") or "na")
    base_msg = str(ev.get("message") or "")

    def _f(v):
        try:
            f = float(v)
            return f"${f:.2f}" if np.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    stop_s, tgt_s = _f(p.get("stop")), _f(p.get("target"))
    rr_s = p.get("rr_label") if p.get("rr_label") not in (None, "-") else None
    plan_bits = []
    if stop_s:
        try:
            plan_bits.append(f"손절 {stop_s}({float(p.get('stop_pct')):.1f}%)")
        except (TypeError, ValueError):
            plan_bits.append(f"손절 {stop_s}")
    if tgt_s:
        plan_bits.append(f"목표 {tgt_s}")
    if rr_s:
        plan_bits.append(f"R:R {rr_s}")
    plan_line = " · ".join(plan_bits)

    if dec.get("key") in ("buy", "buy_split") and gate == "fit":
        ev["label"] = "🟢 매수 신호 (✅ R:R 게이트 통과)"
        ev["message"] = base_msg + (f" · {plan_line}" if plan_line else "")
        note = str(p.get("gate_reason") or "")
        if "정보용" in note:
            ev["message"] += " · 독립 목표 미설정(R:R 정보용)"
    elif gate == "na":
        ev["label"] = "🟢 매수 신호 (⚪ 게이트 판단 보류)"
        ev["message"] = base_msg + f" · {p.get('gate_reason') or '플랜 산출 불가'}"
    else:
        ev["label"] = "⚠️ 매수 신호 — 게이트 미통과·건너뛰기 권고"
        reason = str(p.get("gate_reason") or "손익비/구간 부적합")
        ev["message"] = base_msg + f" · {reason}" + (f" · {plan_line}" if plan_line else "")
    ev["gate"] = gate
    ev["decision"] = dec.get("key")
    return ev


# ──────────────────────────────────────────────────────────────────────────
# 7) 근거 중첩도(Confluence) 점수 (#3) — 순수, app/scanner 공유 SSOT
#    철학: '확신/예측'이 아니라 '확률을 높이는 근거의 합'. 보장 아님.
#    정직성: 결측 팩터는 점수에서 제외 + 가중 재정규화(허수 방지).
#    백테스트 반영: 타이밍은 약알파라 과대가중 금지, avoid 는 점수와 무관하게 회피 우선.
# ──────────────────────────────────────────────────────────────────────────

CONFLUENCE_WEIGHTS = {
    "timing": 2.0, "rs": 2.0, "fundamental": 2.0, "insider": 1.0, "analyst": 1.0,
}
CONFLUENCE_FACTOR_LABELS = {
    "timing": "추세·타이밍",
    "rs": "상대강도(RS)",
    "fundamental": "펀더멘털 건전성",
    "insider": "내부자 순매수",
    "analyst": "애널리스트 상승여력",
}
_TIMING_SIGNAL = {"entry": 1.0, "wait": 0.5, "overheat": 0.0, "trend_break": -0.5, "avoid": -1.0}


def _confluence_label(score, avoid_flag: bool) -> str:
    if avoid_flag:
        return "⛔ 회피 우선"
    if score is None:
        return "⚪ 데이터 부족"
    if score >= 70:
        return "🟢 근거 강함"
    if score >= 50:
        return "🟡 근거 보통"
    if score >= 35:
        return "🟠 근거 약함"
    return "🔴 근거 희박"


def confluence_score(verdict_code=None, rs_excess=None, piotroski=None, altman_z=None,
                     insider_ratio=None, analyst_upside=None, short_pct=None,
                     earnings_days=None, weights: dict = None) -> dict:
    """여러 근거(정량)를 -1/0/+1 신호로 환산 → 가중 합산 → 0~100 정규화.

    입력(모두 선택; None 이면 해당 팩터 제외 후 재정규화):
      verdict_code   : analyze_ticker timing code
      rs_excess      : 종목 63일 수익 − SPY 63일 수익 (소수, 0.08=+8%p)
      piotroski      : Piotroski F-Score (0~9)
      altman_z       : Altman Z-Score
      insider_ratio  : 내부자 매수/매도 비율
      analyst_upside : (목표가평균/현재가 − 1) (소수)
      short_pct      : 공매도 비율(점수 미반영 — 정보 표시용)
      earnings_days  : 다음 실적까지 일수(점수 미반영 — D-5 이내 갱리스크 플래그)

    반환: {score, raw_score, label, avoid_flag, earnings_warning, n_factors,
           breakdown:[{key,label,signal,weight,contribution,value,available}], short_note}
    """
    w = dict(CONFLUENCE_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})

    def _num(x):
        try:
            v = float(x)
            return v if np.isfinite(v) else None
        except (TypeError, ValueError):
            return None

    signals = {}   # key -> (signal or None, value_str)

    # 1) 추세·타이밍
    code = str(verdict_code) if verdict_code is not None else ""
    if code in _TIMING_SIGNAL:
        signals["timing"] = (_TIMING_SIGNAL[code], code)
    else:
        signals["timing"] = (None, "데이터 없음")

    # 2) 상대강도(RS): SPY 대비 초과수익
    rs = _num(rs_excess)
    if rs is None:
        signals["rs"] = (None, "데이터 없음")
    else:
        sig = 1.0 if rs > 0.05 else (-1.0 if rs < -0.05 else 0.0)
        signals["rs"] = (sig, f"{rs * 100:+.1f}%p vs SPY(63일)")

    # 3) 펀더멘털 건전성: Piotroski + Altman 평균
    subs, parts = [], []
    p = _num(piotroski)
    if p is not None:
        subs.append(1.0 if p >= 7 else (-1.0 if p <= 3 else 0.0))
        parts.append(f"P {p:.0f}/9")
    z = _num(altman_z)
    if z is not None:
        subs.append(1.0 if z > 3 else (-1.0 if z < 1.8 else 0.0))
        parts.append(f"Z {z:.1f}")
    if subs:
        signals["fundamental"] = (float(np.mean(subs)), " · ".join(parts))
    else:
        signals["fundamental"] = (None, "데이터 없음")

    # 4) 내부자 순매수
    ir = _num(insider_ratio)
    if ir is None:
        signals["insider"] = (None, "데이터 없음")
    else:
        sig = 1.0 if ir > 1.2 else (-1.0 if ir < 0.8 else 0.0)
        signals["insider"] = (sig, f"매수/매도 {ir:.2f}")

    # 5) 애널리스트 상승여력
    au = _num(analyst_upside)
    if au is None:
        signals["analyst"] = (None, "데이터 없음")
    else:
        sig = 1.0 if au > 0.15 else (-1.0 if au < 0.0 else 0.0)
        signals["analyst"] = (sig, f"상승여력 {au * 100:+.0f}%")

    # ── 점수 산출 (가용 팩터만, 재정규화) ──
    breakdown = []
    raw = 0.0
    total_w = 0.0
    n_factors = 0
    for key in ("timing", "rs", "fundamental", "insider", "analyst"):
        sig, val = signals[key]
        wt = w.get(key, 0.0)
        avail = sig is not None
        contrib = (sig * wt) if avail else 0.0
        if avail:
            raw += contrib
            total_w += wt
            n_factors += 1
        breakdown.append({
            "key": key, "label": CONFLUENCE_FACTOR_LABELS[key],
            "signal": (round(sig, 2) if avail else None), "weight": wt,
            "contribution": round(contrib, 2), "value": val, "available": avail,
        })

    score = round((raw + total_w) / (2 * total_w) * 100.0, 1) if total_w > 0 else None
    raw_score = score
    avoid_flag = (code == "avoid")
    if avoid_flag and score is not None:
        score = min(score, 35.0)

    # 공매도: 정보 표시용(점수 미반영)
    sp = _num(short_pct)
    if sp is None:
        short_note = ""
    elif sp >= 20:
        short_note = f"🔥 공매도 {sp:.1f}% — 스퀴즈 연료/경고"
    elif sp >= 10:
        short_note = f"⚠️ 공매도 {sp:.1f}%"
    else:
        short_note = f"공매도 {sp:.1f}%(낮음)"

    ed = _num(earnings_days)
    earnings_warning = (ed is not None and 0 <= ed <= 5)

    return {
        "score": score, "raw_score": raw_score,
        "label": _confluence_label(score, avoid_flag),
        "avoid_flag": avoid_flag, "earnings_warning": earnings_warning,
        "n_factors": n_factors, "breakdown": breakdown, "short_note": short_note,
    }


# ──────────────────────────────────────────────────────────────────────────
# 8) 통일 결정(Decision) 합성 — 표시 SSOT (재계산 없음, 있는 값 조립만)
#    매수: 매수 / 매수(분할) / 대기(눌림) / 대기(횡보) / 회피
#    매도: 보유 / 줄이기 / 청산
#    [5]·[6] 결정 카드 + [7] 글랜스 뱃지가 모두 이 함수를 공유.
# ──────────────────────────────────────────────────────────────────────────

BUY_DECISION_LABELS = {
    "buy": "🟢 매수", "buy_split": "🟢 매수(분할)",
    "wait_pullback": "🟡 대기(눌림)", "wait_range": "🟡 대기(횡보)",
    "avoid": "🔴 회피", "na": "⚪ 판단보류",
}
SELL_DECISION_LABELS = {
    "hold": "🟢 보유", "trim": "🟡 줄이기", "exit": "🔴 청산", "na": "⚪ 판단보류",
}
_BUY_TONE = {"buy": "success", "buy_split": "success", "wait_pullback": "warning",
             "wait_range": "warning", "avoid": "error", "na": "info"}
_SELL_TONE = {"hold": "success", "trim": "warning", "exit": "error", "na": "info"}


def buy_decision(verdict_code, gate_code=None, regime: str = "unknown") -> dict:
    """타이밍 코드 + R:R 게이트 + 레짐 → 통일 매수 결정(순수 라벨 매핑)."""
    code = str(verdict_code or "")
    g = str(gate_code or "")
    strong = (regime == "strong")
    if code in ("avoid", "trend_break") or g == "avoid":
        key = "avoid"
    elif code == "overheat":
        key = "buy_split"
    elif code == "entry":
        key = "buy" if g != "skip" else ("wait_pullback" if strong else "wait_range")
    elif code == "wait":
        key = "wait_pullback" if strong else "wait_range"
    else:
        key = "na"
    return {"key": key, "label": BUY_DECISION_LABELS[key], "tone": _BUY_TONE[key]}


def sell_decision(exit_dict: dict, timing_code=None, topping: bool = False) -> dict:
    """청산 신호 dict + (선택)타이밍/토핑 → 통일 매도 결정.
    청산(하드) > 줄이기(경고: 리버설·추세 흔들림·토핑) > 보유."""
    ex = exit_dict or {}
    if ex.get("is_exit"):
        key = "exit"
    elif ex.get("warnings") or str(timing_code or "") == "trend_break" or topping:
        key = "trim"
    elif ex:
        key = "hold"
    else:
        key = "na"
    return {"key": key, "label": SELL_DECISION_LABELS[key], "tone": _SELL_TONE[key]}


def _fmt(v, dollar=True):
    try:
        f = float(v)
        if not np.isfinite(f):
            return "-"
        return f"${f:.2f}" if dollar else f"{f:.2f}"
    except (TypeError, ValueError):
        return "-"


def build_buy_card(hist, analysis: dict, plan: dict, confluence=None,
                   lookback: int = NEG_REV_LOOKBACK) -> dict:
    """[5] 매수 결정 카드 + [7] 글랜스용 표시 dict. 모두 기존 값 조립(재계산 없음).
    반환: {key,label,tone,headline,trigger,invalidation,plan_line,why,badge,glance_num}."""
    timing = analysis.get("timing", {}) if analysis else {}
    regime = analysis.get("regime", {}) if analysis else {}
    reg = regime.get("regime", "unknown")
    dec = buy_decision(timing.get("code"), (plan or {}).get("gate"), reg)
    key = dec["key"]

    # 레벨(있는 값 읽기)
    price = ma20 = ma50 = ma200 = rhigh = rlow = rsi_last = np.nan
    try:
        close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
        if not close.empty:
            price = float(close.iloc[-1])
            ma20 = _ma_last(close, 20)
            ma50 = _ma_last(close, 50)
            ma200 = _ma_last(close, 200)
            tail = close.tail(lookback)
            hi_src = pd.to_numeric(hist["High"], errors="coerce").dropna() if "High" in hist.columns else close
            lo_src = pd.to_numeric(hist["Low"], errors="coerce").dropna() if "Low" in hist.columns else close
            rhigh = float(hi_src.tail(lookback).max())
            rlow = float(lo_src.tail(lookback).min())
            r = compute_rsi(close).dropna()
            rsi_last = float(r.iloc[-1]) if not r.empty else np.nan
    except Exception:
        pass

    stop = (plan or {}).get("stop")
    target = (plan or {}).get("target")
    rr = (plan or {}).get("rr_label", "-")
    shares = (plan or {}).get("shares", 0)
    pos_pct = (plan or {}).get("position_pct", 0.0)
    _dollars = float((plan or {}).get("dollars", 0.0) or 0.0)
    # 금액 불변량: 소수점 매수 기준 금액을 우선 표기하고, 정수주는 괄호로 보조 표기
    _size_txt = (f"${_dollars:,.0f} (비중 {pos_pct:.0f}% · 정수주 {shares:,}주)"
                 if _dollars > 0 else f"{shares:,}주 (비중 {pos_pct:.0f}%)")
    stop_pct = (plan or {}).get("stop_pct")
    target_pct = (plan or {}).get("target_pct")

    trigger = invalidation = plan_line = headline = ""
    trigger_price = np.nan

    if key == "buy":
        headline = "진입 확인됨 — R:R·근거 충족."
        plan_line = (f"진입 {_fmt(price)} · {_size_txt} · "
                     f"손절 {_fmt(stop)} ({stop_pct:.1f}%) · 목표 {_fmt(target)} · R:R {rr}")
        invalidation = f"{_fmt(stop)} ({stop_pct:.1f}%)" if stop_pct is not None else _fmt(stop)
    elif key == "buy_split":
        headline = "강세지만 과열 — 일괄은 고점 위험, 관망은 놓칠 위험. 분할 권장."
        add_lv = ma20 if (np.isfinite(ma20) and ma20 < price) else (ma50 if np.isfinite(ma50) else np.nan)
        trigger = f"1차 지금 (목표의 절반) · 2차 추가 {_fmt(add_lv)} 눌림 회복 시 (나머지)"
        plan_line = f"진입 시 총 {_size_txt} · 손절 {_fmt(stop)} · R:R {rr}"
        invalidation = f"{_fmt(stop)} ({stop_pct:.1f}%)" if stop_pct is not None else _fmt(stop)
        trigger_price = price
    elif key == "wait_pullback":
        headline = "강세 추세의 눌림 — 진입 확인 아직."
        if np.isfinite(ma20) and price < ma20:
            trigger = f"20일선({_fmt(ma20)}) 위 종가 회복"; trigger_price = ma20
        elif np.isfinite(ma50) and price < ma50:
            trigger = f"50일선({_fmt(ma50)}) 회복"; trigger_price = ma50
        else:
            trigger = f"최근 고점({_fmt(rhigh)}) 돌파"; trigger_price = rhigh
        invalidation = (f"200일선({_fmt(ma200)}) 하회" if np.isfinite(ma200)
                        else (f"{_fmt(stop)} 하회" if np.isfinite(stop) else "-"))
        plan_line = f"진입 시 {shares:,}주 · R:R {rr}"
    elif key == "wait_range":
        headline = "박스권 — 방향 미정. 돌파/이탈 확인 후 대응."
        trigger = f"박스 상단({_fmt(rhigh)}) 돌파"; trigger_price = rhigh
        invalidation = f"박스 하단({_fmt(rlow)}) 이탈"
        plan_line = f"돌파 시 {shares:,}주 · R:R {rr}"
    elif key == "avoid":
        headline = "약세 회피 구간 — 신규 진입 비권장. (백테스트상 음의 초과수익)"
    else:
        headline = "데이터 부족 — 판단 보류."

    why_bits = []
    if confluence is not None:
        try:
            why_bits.append(f"근거중첩 {float(confluence):.0f}")
        except (TypeError, ValueError):
            pass
    why_bits.append(REGIME_LABEL.get(reg, "⚪"))
    if np.isfinite(rsi_last):
        why_bits.append(f"RSI {rsi_last:.0f}")
    why = " · ".join(why_bits)

    if key in ("buy",):
        glance_num = f"R:R {rr}"
    elif key == "buy_split":
        glance_num = "1차 지금"
    elif key in ("wait_pullback", "wait_range"):
        glance_num = f"트리거 {_fmt(trigger_price)}"
    else:
        glance_num = "—"

    return {"key": key, "label": dec["label"], "tone": dec["tone"],
            "headline": headline, "trigger": trigger, "invalidation": invalidation,
            "plan_line": plan_line, "why": why, "badge": dec["label"],
            "glance_num": glance_num, "trigger_price": trigger_price}


def build_sell_card(analysis: dict, plan: dict = None) -> dict:
    """[6] 매도 결정 카드 표시 dict."""
    ex = (analysis or {}).get("exit", {})
    tv = (analysis or {}).get("timing", {})
    rg = (analysis or {}).get("regime", {})
    dec = sell_decision(ex, tv.get("code"), rg.get("topping", False))
    key = dec["key"]
    headline = {"exit": "추세 꺾임 — 청산 신호 발생.",
                "trim": "추세 흔들림·토핑 경고 — 분할 청산·스톱 타이트닝 고려.",
                "hold": "추세 유지 — 청산 신호 없음.",
                "na": "데이터 부족."}[key]
    if key == "exit":
        detail = " · ".join(ex.get("reasons", []))
    elif key == "trim":
        bits = list(ex.get("warnings", []))
        if str(tv.get("code") or "") == "trend_break" or rg.get("topping"):
            bits += list(tv.get("reasons", []))
        detail = " · ".join([b for b in bits if b])
    else:
        detail = ""
    stop = (plan or {}).get("stop")
    target = (plan or {}).get("target")
    stop_pct = (plan or {}).get("stop_pct")
    line = ""
    if stop is not None and (np.isfinite(stop) if isinstance(stop, (int, float)) else False):
        line = f"손절 {_fmt(stop)}" + (f" ({stop_pct:.1f}%)" if stop_pct is not None else "")
        if target is not None and np.isfinite(target):
            line += f" · 목표 {_fmt(target)}"
    return {"key": key, "label": dec["label"], "tone": dec["tone"],
            "headline": headline, "detail": detail, "line": line, "badge": dec["label"]}


# ──────────────────────────────────────────────────────────────────────────
# 9) 포지션(Position) 매도 판정 — SSOT (앱·자동화·이메일 공유)
#    스윙(Swing) = compute_exit_signals/sell_decision (50일선·ATR·RSI 과열, 며칠~수주)
#    포지션(Position) = integrated_sell_verdict (200일선·-15% 트레일링·MACD, 수주~수개월)
# ──────────────────────────────────────────────────────────────────────────

_DEFAULT_TRAILING_STOP_PCT = 15.0


def integrated_sell_verdict(*, above_ma200, one_month_return, rsi, macd_signal,
                            pct_from_52w_high, drawdown_from_high_pct,
                            trailing_stop_pct: float = _DEFAULT_TRAILING_STOP_PCT):
    """200일선(후행) + 트레일링 스톱 + MACD/1개월/과열(선행)을 한 점수로 종합한
    포지션(중장기) 매도 판정. 반환: (label, reason). 라벨은 청산/줄이기/보유 통일 용어
    (괄호에 SELL/익절/HOLD 키워드 유지 → 기존 파서·스타일러 호환)."""
    reasons = []
    score = 0
    if above_ma200 is False:
        score += 4
        reasons.append("200일선 이탈(추세 붕괴)")
    if pd.notna(drawdown_from_high_pct) and drawdown_from_high_pct <= -abs(trailing_stop_pct):
        score += 3
        reasons.append(f"고점 대비 {drawdown_from_high_pct:.0f}% 하락(트레일링 스톱 -{abs(trailing_stop_pct):.0f}%)")
    if macd_signal == "DEAD_CROSS":
        score += 2
        reasons.append("MACD 데드크로스(추세 꺾임)")
    elif macd_signal == "BELOW_SIGNAL":
        score += 1
    if pd.notna(one_month_return) and one_month_return < 0:
        score += 1
        reasons.append(f"1개월 {one_month_return:+.1f}%")
    overheated = (pd.notna(rsi) and rsi > 70
                  and pd.notna(pct_from_52w_high) and pct_from_52w_high > -3)
    if overheated:
        score += 1
        reasons.append(f"RSI {rsi:.0f} 과열 + 신고가 부근(단기 조정 위험)")
    if score >= 4:
        label = "🔴 청산 (SELL)"
    elif score >= 2:
        label = "🟡 줄이기 (일부 익절)"
    else:
        label = "✅ 보유 (HOLD)"
        if not reasons:
            reasons.append("추세 유지 · 이상 신호 없음")
    return label, " · ".join(reasons)


_DD_FALLBACK_WINDOW = 252     # 매수가·매수일 둘 다 없을 때 폴백 고점 룩백(약 1년)


def compute_position_drawdown(close_series, purchase_price, current_price, date_added: str = ""):
    """포지션의 '고점 대비 하락'을 보수적으로 계산 + 데이터 오류 플래그.

    ※ app.py 에서 이관(SSOT). 앱 표·이메일·백테스트가 이 한 곳을 공유한다.
      이관 전에는 자동화(position_sell_verdict)가 매수가·매수일을 무시하고
      `close.tail(120).max()` 를 썼기 때문에, 눌린 종목을 매수하면 진입 즉시
      트레일링 스톱이 걸리는 허위 청산 신호가 발생했다.

    (a) 매수일 이후 고점, (b) 매수가를 바닥선으로 본 고점 중
    더 보수적인(=덜 깊은 하락) 쪽을 채택해 허수 낙폭을 줄인다.
    비현실적 낙폭(스플릿 미조정·스파이크)으로 의심되면 data_error=True.

    반환: (drawdown_pct, data_error: bool)
      - drawdown_pct: 보통 0 이하의 % (예: -12.3). 계산 불가 시 np.nan.
                      진입 후 상승 중이면 양수가 될 수 있으나 판정 조건이
                      `<= -trailing_stop_pct` 라 오발하지 않는다.
      - data_error:   True면 트레일링 경보/판정에서 낙폭을 신뢰하지 않음
    """
    try:
        if close_series is None or len(close_series) == 0 or pd.isna(current_price) or current_price <= 0:
            return np.nan, False

        # (a) 매수일 이후 고점
        high_since_buy = np.nan
        if date_added:
            try:
                buy_dt = pd.to_datetime(str(date_added)[:10], errors="coerce")
                if pd.notna(buy_dt) and isinstance(close_series.index, pd.DatetimeIndex):
                    idx = close_series.index
                    tz = getattr(idx, "tz", None)
                    if tz is not None and buy_dt.tzinfo is None:
                        buy_dt = buy_dt.tz_localize(tz)
                    sliced = close_series[close_series.index >= buy_dt]
                    if not sliced.empty:
                        high_since_buy = float(sliced.max())
            except Exception:
                high_since_buy = np.nan

        # (b) 폴백 고점 — 입력 길이에 무관하게 최근 1년으로 고정.
        #     (앱/이메일은 252봉을 넘기지만 백테스트는 훨씬 긴 hist 를 넘기므로
        #      명시 고정하지 않으면 다년 고점이 되어 소비자마다 답이 갈린다.)
        _tail = close_series.tail(_DD_FALLBACK_WINDOW)
        high_1y = float(_tail.max()) if len(_tail) else np.nan

        # 매수가 바닥선: 고점이 적어도 매수가보다는 높아야 '하락'이 의미 있음
        pp = float(purchase_price) if (purchase_price is not None and pd.notna(purchase_price) and purchase_price > 0) else np.nan

        # 트레일링 스톱 고점: 매수가를 바닥선으로, 매수일 이후 실제 고점이 있으면 래칫업.
        # 매수일 이후 데이터가 없어도(매수일이 최신 가격일 이후 등) 52주 최고가(=매수 전
        # 고점)로 새지 않도록 매수가를 고점으로 본다 → 본전 부근 종목의 허위 경보 방지.
        # 매수가·매수일 둘 다 없을 때만 1년 고점으로 폴백.
        _high_cands = [v for v in (high_since_buy, pp) if pd.notna(v)]
        ref_high = max(_high_cands) if _high_cands else high_1y
        if pd.isna(ref_high) or ref_high <= 0:
            return np.nan, False

        # '매수일 이후 고점' 대비 낙폭을 기본으로 사용(트레일링 스톱의 본래 의미).
        # 단, 고점이 비현실적으로 높으면(스플릿 미조정 등) 매수가 기준으로 보정.
        dd_from_high = (float(current_price) / ref_high - 1.0) * 100.0
        drawdown_pct = dd_from_high

        # 데이터 오류 의심 — 낙폭이 -70%보다 깊은데 현재 수익이 플러스면 모순,
        # 또는 참조고점이 현재가의 5배를 초과하면(스플릿 미조정 전형) 오류로 간주
        data_error = False
        if pd.notna(pp):
            in_profit = float(current_price) >= pp
            if dd_from_high <= -70.0 and in_profit:
                data_error = True
        if ref_high > float(current_price) * 5:
            data_error = True

        if data_error:
            # 오류 시 고점을 신뢰하지 않고 매수가 기준 낙폭만 사용(없으면 NaN)
            if pd.notna(pp):
                drawdown_pct = min(0.0, (float(current_price) / pp - 1.0) * 100.0)
            else:
                drawdown_pct = np.nan

        return drawdown_pct, data_error
    except Exception:
        return np.nan, False


DD_DATA_ERROR_NOTE = "⚠️ 가격데이터 확인 필요(스플릿/이상치 의심)"


def _macd_state(close: pd.Series) -> str:
    """MACD(12/26/9) 상태: DEAD_CROSS / BELOW_SIGNAL / ABOVE_SIGNAL / N/A."""
    c = pd.to_numeric(close, errors="coerce").dropna()
    if len(c) < 35:
        return "N/A"
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    m, s = float(macd.iloc[-1]), float(sig.iloc[-1])
    mp, sp = float(macd.iloc[-2]), float(sig.iloc[-2])
    if mp >= sp and m < s:
        return "DEAD_CROSS"
    if m < s:
        return "BELOW_SIGNAL"
    return "ABOVE_SIGNAL"


def position_sell_verdict(hist, entry_price=None, entry_date=None,
                          trailing_stop_pct: float = _DEFAULT_TRAILING_STOP_PCT):
    """raw 시세(hist)에서 포지션(중장기) 매도 판정을 직접 계산 → integrated_sell_verdict.
    자동화/이메일용. 반환: (label, reason).

    entry_price / entry_date: 보유 기준 낙폭 산출용(Portfolios 평단·Date_Added).
      둘 다 없으면 최근 1년 고점으로 폴백한다(이관 전 동작에 근접).
    앱 표는 compute_position_drawdown → integrated_sell_verdict 를 같은 순서로 호출하므로
    동일 입력이면 동일 판정이 나온다(SSOT)."""
    try:
        close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
    except Exception:
        return ("N/A", "데이터 부족")
    if len(close) < 60:
        return ("N/A", "데이터 부족")
    price = float(close.iloc[-1])
    ma200 = _ma_last(close, 200)
    above_ma200 = bool(price > ma200) if np.isfinite(ma200) else None
    one_month_return = np.nan
    if len(close) > 22:
        p0 = float(close.iloc[-22])
        if p0 > 0:
            one_month_return = (price / p0 - 1.0) * 100.0
    r = compute_rsi(close).dropna()
    rsi = float(r.iloc[-1]) if not r.empty else np.nan
    macd_signal = _macd_state(close)
    hi52 = float(close.tail(252).max())
    pct_from_52w_high = (price / hi52 - 1.0) * 100.0 if hi52 > 0 else np.nan

    # 보유 고점 = max(매수일 이후 고점, 매수가) — app.py Sell Radar 와 동일 SSOT.
    # (이전에는 매수가·매수일을 버리고 종목의 120일 고점을 썼기 때문에,
    #  눌린 종목을 매수하면 진입 즉시 트레일링 청산이 뜨는 허위 신호가 있었다.)
    dd_pct, dd_err = compute_position_drawdown(close, entry_price, price, entry_date)
    drawdown_from_high_pct = np.nan if dd_err else dd_pct

    label, reason = integrated_sell_verdict(
        above_ma200=above_ma200, one_month_return=one_month_return, rsi=rsi,
        macd_signal=macd_signal, pct_from_52w_high=pct_from_52w_high,
        drawdown_from_high_pct=drawdown_from_high_pct, trailing_stop_pct=trailing_stop_pct,
    )
    if dd_err:
        reason = (reason + " · " + DD_DATA_ERROR_NOTE).strip(" ·")
    return label, reason
