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
        if regime_res.get("topping"):
            reasons.append("천장(Stage3) 신호 — 신규 진입 자제")
            out.update({"verdict": "🔺 고점 신호 주의", "code": "trend_break", "reasons": reasons})
        elif pd.notna(rsi) and rsi <= SIDEWAYS_ENTRY_RSI:
            reasons.append(f"RSI {rsi:.0f}(박스 하단 지지)")
            out.update({"verdict": "🎯 지금 매수 구간 (횡보 저점)", "code": "entry", "reasons": reasons, "is_entry": True})
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
ALERT_EVENTS = ("entry", "regime", "risk", "exit", "price")
ALERT_EVENT_LABELS = {
    "entry":  "🎯 지금 매수 구간",
    "regime": "🔄 레짐 전환",
    "risk":   "🚫 추세 이탈/위험",
    "exit":   "💰 청산 신호",
    "price":  "📌 가격(손절/목표) 도달",
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


def alert_conditions(analysis: dict, price=None, stop_loss=None, target_price=None) -> dict:
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
    return conds


def evaluate_alert_transitions(analysis: dict, enabled_events, last_state_json: str = "",
                               today_str: str = "", price=None, stop_loss=None,
                               target_price=None, confirm_days: int = ALERT_CONFIRM_DAYS):
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
    conds = alert_conditions(analysis, price, stop_loss, target_price)
    fired = []

    # 일반 이벤트: 조건 지속 + confirm_days 확정 + 재무장(조건 해제 시)
    for e in ("entry", "risk", "exit", "price"):
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
                  max_position_pct: float = DEFAULT_MAX_POSITION_PCT) -> dict:
    """고정 분율 리스크 사이징. 거래당 잃을 금액을 자본의 risk_pct% 로 고정.
    shares = floor((equity*risk_pct%) / (entry-stop)), 단일 종목 max_position_pct% 상한.
    반환: {shares, dollars, risk_dollars, position_pct, capped}."""
    out = {"shares": 0, "dollars": 0.0, "risk_dollars": 0.0,
           "position_pct": 0.0, "capped": False}
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
    risk_budget = eq * (rp / 100.0)
    shares = int(np.floor(risk_budget / risk_per_share))
    if shares < 0:
        shares = 0
    capped = False
    max_dollars = eq * (mp / 100.0)
    if shares * e > max_dollars and e > 0:
        shares = int(np.floor(max_dollars / e))
        capped = True
    dollars = shares * e
    out.update({
        "shares": shares,
        "dollars": round(dollars, 2),
        "risk_dollars": round(shares * risk_per_share, 2),
        "position_pct": round((dollars / eq * 100.0) if eq > 0 else 0.0, 2),
        "capped": capped,
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
                     recent_high=None, max_position_pct: float = DEFAULT_MAX_POSITION_PCT) -> dict:
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

    sz = position_size(equity, risk_pct, e, stop, max_position_pct)
    plan.update({k: sz[k] for k in ("shares", "dollars", "risk_dollars", "position_pct", "capped")})

    # ── 게이트 판정 ──────────────────────────────────────────────────────
    code = str(verdict_code or "")
    rr_val = plan["r_multiple"]
    rr_is_real = (basis in ("manual", "structural_high")) and np.isfinite(rr_val)

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
    stop_pct = (plan or {}).get("stop_pct")
    target_pct = (plan or {}).get("target_pct")

    trigger = invalidation = plan_line = headline = ""
    trigger_price = np.nan

    if key == "buy":
        headline = "진입 확인됨 — R:R·근거 충족."
        plan_line = (f"진입 {_fmt(price)} · {shares:,}주 (비중 {pos_pct:.0f}%) · "
                     f"손절 {_fmt(stop)} ({stop_pct:.1f}%) · 목표 {_fmt(target)} · R:R {rr}")
        invalidation = f"{_fmt(stop)} ({stop_pct:.1f}%)" if stop_pct is not None else _fmt(stop)
    elif key == "buy_split":
        headline = "강세지만 과열 — 일괄은 고점 위험, 관망은 놓칠 위험. 분할 권장."
        add_lv = ma20 if (np.isfinite(ma20) and ma20 < price) else (ma50 if np.isfinite(ma50) else np.nan)
        trigger = f"1차 지금 (목표의 절반) · 2차 추가 {_fmt(add_lv)} 눌림 회복 시 (나머지)"
        plan_line = f"진입 시 총 {shares:,}주 · 손절 {_fmt(stop)} · R:R {rr}"
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
