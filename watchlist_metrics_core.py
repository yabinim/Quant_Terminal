# -*- coding: utf-8 -*-
"""watchlist_metrics_core.py — 워치리스트 표시용 지표의 SSOT.

[왜 존재하는가]
  워치리스트 탭은 종목마다 rc.analyze_ticker(레짐 + 타이밍 + 청산신호)를 돌린다.
  55종목이면 리런마다 10초가 걸렸고, 저장·위젯 조작 때마다 통째로 재계산됐다.
  이 계산은 **일봉 파생**이라 장중에 거의 움직이지 않는다. 그래서 자동화
  (run_watchlist_alerts)가 하루치를 미리 계산해 Watchlist_Metrics 시트에 적어두고,
  앱은 읽기만 한다.

[신선도 규칙 — 시계가 아니라 거래일 기준]
  "N분 지났으면 낡음"이 아니라 "**계산에 쓰인 마지막 봉 날짜(Trade_Date)가
  기준 봉 날짜와 다르면 낡음**"으로 판정한다. 일봉 데이터에는 이게 의미상 맞고,
  장중에 불필요한 폴백이 터지지 않는다.
  → 낡거나 없으면 앱이 그 종목만 실시간 계산한다(폴백).

[무엇이 여기 없는가]
  현재가(등록 시 대비 %, 손절·목표가 도달)는 여기서 다루지 않는다. 빠르게 변하므로
  앱이 fetch_latest_prices_for_tickers(ttl=60)로 실시간 유지한다. 느린 일봉 파생과
  빠른 현재가를 분리하는 것이 이 설계의 핵심이다.

[데이터 소유]
  티커 단위다. NVDA의 RSI는 모든 사용자에게 같으므로 per-user 데이터가 아니다.
  자동화가 전 사용자 워치리스트의 **합집합**을 한 번만 계산해 쓴다. 게스트는 읽기만.
  이 시트에는 어떤 개인 데이터도 들어가지 않는다.

SSOT: app.py 와 run_watchlist_alerts.py 가 함께 임포트한다. 변경 시 동시 배포.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

import regime_core as rc

SSOT_VERSION = "wlm-1.0.0"

SHEET_TITLE = "Watchlist_Metrics"
COLS = [
    "Ticker",       # A  키
    "Trade_Date",   # B  계산에 쓰인 마지막 봉 날짜 — 신선도 판정 기준
    "Updated_At",   # C  계산 시각(ET) — 사람이 보는 용도
    "RSI",          # D ┐
    "MA200",        # E │
    "ATR",          # F │ 시트에서 눈으로 확인하는 용도.
    "High_120",     # G │ 앱이 실제로 쓰는 값은 L열 Payload_JSON 이다.
    "Last_Close",   # H │
    "Dec_Key",      # I │
    "Regime",       # J │
    "Timing",       # K ┘
    "Payload_JSON", # L  analyze_ticker 전체 dict (앱이 읽는 본체)
]
NCOL = len(COLS)

# 폴백/디버깅용 — 사람이 읽는 라벨
_COL = {c: i for i, c in enumerate(COLS)}


# ══════════════════════════════════════════════════════════════════════════════
# JSON 살균 — NaN 은 유효한 JSON 이 아니다
# ══════════════════════════════════════════════════════════════════════════════
_NAN_TOKEN = "__nan__"


def _json_safe(o):
    """numpy 스칼라·NaN·Timestamp 를 JSON 으로 실을 수 있는 형태로 바꾼다.

    ⚠️ NaN 을 null 로 보내면 안 된다. analyze_ticker 출력의 결측 숫자는 전부 NaN 이고,
       소비자(build_buy_card 등)가 pd.notna / float() 로 다룬다. None 으로 바뀌면
       float(None) 에서 터지거나 조용히 다른 분기를 탄다. 전용 토큰으로 왕복시킨다.
    """
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return _NAN_TOKEN if (math.isnan(f) or math.isinf(f)) else f
    if o is None:
        return None
    if isinstance(o, str):
        return o
    if isinstance(o, (pd.Timestamp,)):
        try:
            return o.strftime("%Y-%m-%d")
        except Exception:
            return str(o)
    try:
        if pd.isna(o):
            return _NAN_TOKEN
    except (TypeError, ValueError):
        pass
    return str(o)


def _json_restore(o):
    """_json_safe 의 역변환. 토큰을 np.nan 으로 되돌린다."""
    if isinstance(o, dict):
        return {k: _json_restore(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_json_restore(v) for v in o]
    if o == _NAN_TOKEN:
        return np.nan
    return o


def _f(v):
    """시트 문자열 → float. 빈칸/파싱실패는 NaN."""
    try:
        if v is None or str(v).strip() == "":
            return np.nan
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def _fmt(v, nd=4):
    """float → 시트 문자열. NaN 은 빈칸."""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return ""
        return str(round(f, nd))
    except (TypeError, ValueError):
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# 계산
# ══════════════════════════════════════════════════════════════════════════════
def trade_date_of(hist) -> str:
    """일봉의 마지막 봉 날짜(YYYY-MM-DD). 신선도 판정의 기준값."""
    try:
        if hist is None or hist.empty:
            return ""
        idx = hist.index[-1]
        if hasattr(idx, "strftime"):
            return idx.strftime("%Y-%m-%d")
        return str(idx)[:10]
    except Exception:
        return ""


def compute_metrics(ticker: str, hist, spy_close=None, updated_at: str = "") -> dict | None:
    """한 종목의 표시용 지표 묶음. app 과 automation 이 반드시 이 함수를 쓴다.

    반환 dict 키는 앱의 맵 이름과 1:1로 맞춘다:
      rsi / ma200 / atr / high_120 / last_close / dec_key / analysis / trade_date
    """
    if hist is None or getattr(hist, "empty", True):
        return None
    try:
        close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
        if close.empty:
            return None

        # RSI 는 regime_core.compute_rsi 를 쓴다. app.py 의 calculate_rsi(scanner_core)와
        # 동일한 단순 롤링 평균 방식이며(compute_rsi 독스트링에 명시), 자동화가
        # scanner_core 를 임포트하지 않아도 되게 해준다.
        rsi_series = rc.compute_rsi(close).dropna()
        rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else np.nan
        ma200 = (float(close.rolling(200, min_periods=200).mean().iloc[-1])
                 if len(close) >= 200 else np.nan)
        atr = rc.compute_atr(hist)
        hi_src = (pd.to_numeric(hist["High"], errors="coerce")
                  if "High" in hist.columns else close).dropna()
        high_120 = float(hi_src.tail(120).max()) if not hi_src.empty else np.nan

        analysis = rc.analyze_ticker(hist, spy_close=spy_close)
        dec_key = "na"
        try:
            if analysis:
                dec_key = rc.buy_decision(analysis["timing"].get("code"), None,
                                          analysis["regime"].get("regime"))["key"]
        except Exception:
            dec_key = "na"

        return {
            "ticker": str(ticker).strip().upper(),
            "trade_date": trade_date_of(hist),
            "updated_at": str(updated_at or ""),
            "rsi": rsi,
            "ma200": ma200,
            "atr": atr,
            "high_120": high_120,
            "last_close": float(close.iloc[-1]),
            "dec_key": dec_key,
            "analysis": analysis,
        }
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 직렬화
# ══════════════════════════════════════════════════════════════════════════════
def to_row(m: dict) -> list:
    """metrics dict → 시트 행(정확히 NCOL 칸).

    ⚠️ 길이를 강제 정규화한다. 칸 수가 어긋나면 옆 열로 밀려 드리프트가 된다.
    """
    if not m:
        return [""] * NCOL
    _an = m.get("analysis") or {}
    try:
        payload = json.dumps(_json_safe(_an), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        payload = ""
    row = [
        str(m.get("ticker", "")).strip().upper(),
        str(m.get("trade_date", "")),
        str(m.get("updated_at", "")),
        # ⚠️ 소수 4자리. 앱이 이 열을 그대로 읽으므로 반올림이 곧 앱↔자동화 값 차이가
        #    된다. 2자리로 줄이면 실시간 계산 폴백과 저장본이 미세하게 갈린다.
        _fmt(m.get("rsi"), 4),
        _fmt(m.get("ma200"), 4),
        _fmt(m.get("atr"), 4),
        _fmt(m.get("high_120"), 4),
        _fmt(m.get("last_close"), 4),
        str(m.get("dec_key", "na")),
        str((_an.get("regime") or {}).get("regime", "")),
        str((_an.get("timing") or {}).get("code", "")),
        payload,
    ]
    return (row + [""] * NCOL)[:NCOL]


def from_row(r: list) -> dict | None:
    """시트 행 → metrics dict. 티커나 페이로드가 없으면 None."""
    try:
        r = (list(r) + [""] * NCOL)[:NCOL]
        tk = str(r[_COL["Ticker"]]).strip().upper()
        if not tk:
            return None
        raw = str(r[_COL["Payload_JSON"]]).strip()
        analysis = _json_restore(json.loads(raw)) if raw else None
        if not analysis:
            return None
        return {
            "ticker": tk,
            "trade_date": str(r[_COL["Trade_Date"]]).strip(),
            "updated_at": str(r[_COL["Updated_At"]]).strip(),
            "rsi": _f(r[_COL["RSI"]]),
            "ma200": _f(r[_COL["MA200"]]),
            "atr": _f(r[_COL["ATR"]]),
            "high_120": _f(r[_COL["High_120"]]),
            "last_close": _f(r[_COL["Last_Close"]]),
            "dec_key": str(r[_COL["Dec_Key"]]).strip() or "na",
            "analysis": analysis,
        }
    except Exception:
        return None


def completed_bars_only(hist, today_et_str: str):
    """오늘(ET) 봉을 제외한 확정 봉만 남긴다.

    FMP EOD 일봉은 장중에 당일 미완성 봉을 포함한다. 백필은 아무 때나 돌 수 있어야
    하므로, 저장되는 지표를 항상 '확정된 마지막 세션' 기준으로 고정한다.
    이렇게 하면 실행 시각과 무관하게 결과가 결정적이고, is_fresh 의 기준
    (last_completed_session)과도 정확히 맞는다.

    ⚠️ 정기 EOD 실행(마감 후)에는 쓰지 않는다. 그때는 당일 봉이 이미 확정이라
       자르면 하루 낡은 값을 저장하게 된다.
    ⚠️ 자를 봉이 하나도 안 남으면 원본을 그대로 돌려준다(비정상 데이터 방어).
    """
    try:
        if hist is None or getattr(hist, "empty", True):
            return hist
        _t = str(today_et_str or "").strip()[:10]
        if not _t:
            return hist
        mask = [
            (i.strftime("%Y-%m-%d") if hasattr(i, "strftime") else str(i)[:10]) < _t
            for i in hist.index
        ]
        if not any(mask):
            return hist
        if all(mask):
            return hist
        return hist[pd.Series(mask, index=hist.index)]
    except Exception:
        return hist


def last_completed_session(hist, today_et_str: str) -> str:
    """오늘(ET)보다 앞선 마지막 봉 날짜 = 최근 '완료된' 세션.

    ⚠️ FMP EOD 일봉은 장중에 **당일 미완성 봉을 포함**한다. 그래서 신선도를
       "마지막 봉 날짜와 정확히 일치"로 잡으면, 자동화가 5PM 에 쓴 계산본이
       다음 날 개장하는 순간 전부 낡음으로 판정돼 장중 내내 폴백이 터진다.
       (사용자의 실제 사용 시간대가 미국 장중이라 개선 효과가 통째로 사라진다)
       기준을 '완료된 마지막 세션'으로 잡으면 하루 종일 안정적이다.
    """
    try:
        if hist is None or hist.empty:
            return ""
        _t = str(today_et_str or "").strip()[:10]
        for idx in reversed(list(hist.index)):
            d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            if not _t or d < _t:
                return d
        return ""
    except Exception:
        return ""


def is_fresh(m: dict, ref_trade_date: str) -> bool:
    """최근 완료 세션 이후의 데이터로 계산됐는가.

    `>=` 비교인 이유: 장 마감 후(5PM 자동화 직후)에는 저장본의 Trade_Date 가
    기준일보다 하루 앞선다. 등호만 쓰면 그 구간이 통째로 낡음이 된다.

    ref_trade_date 를 모르면(빈 문자열) 낡음으로 본다 — 판정 불가 상태에서
    캐시본을 쓰느니 실시간 계산으로 떨어지는 쪽이 안전하다.
    """
    if not m or not ref_trade_date:
        return False
    stored = str(m.get("trade_date", "")).strip()
    if not stored:
        return False
    return stored >= str(ref_trade_date).strip()
