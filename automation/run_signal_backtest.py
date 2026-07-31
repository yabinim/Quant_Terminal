# -*- coding: utf-8 -*-
"""
run_signal_backtest.py
──────────────────────
GitHub Actions 자동 실행: 레짐 엔진 신호 자기검증(워크포워드 백테스트). 월 1회/수동.

목적
----
라이브와 *동일한* regime_core.analyze_ticker 를 과거 시점마다(미래 훔쳐보기 없이) 호출하여,
verdict(entry/wait/overheat/trend_break/avoid)가 켜진 시점 이후 실제로 어떻게 됐는지
(+5/+20/+60일 수익 · SPY 대비 초과수익 · MFE · MAE)를 버킷별로 집계한다.

  → "🎯 entry 가 정말 돈이 됐나? 시장(SPY) 대비 알파가 있나? entry>wait>overheat>avoid 로 갈리나?"

v1.5 변경 (ETF / 개별주 분리 집계)
----------------------------------
유니버스 413종목 중 ETF_Universe 가 343개(83%)라 기존 집계는 사실상 'ETF 성적표'였다.
그러나 워치리스트 알림은 주로 개별주에 쓰인다 → 두 모집단을 섞으면 결론이 오염된다.

이제 Segment 열로 all / etf / stock 을 분리 집계한다(모드 × 세그먼트 = 6개 표).
분류 기준: ETF_Universe 시트에 있으면 etf, 아니면 stock.
  ※ 워치리스트/포트폴리오에만 있고 ETF_Universe 에 없는 ETF(예: 계좌 코어 ETF)는
    stock 으로 잡힐 수 있다. 정확도를 높이려면 해당 티커를 ETF_Universe 에 넣으면 된다.

v1.4 변경 (실제 알림 기준 모드 추가)
------------------------------------
기존 집계는 화면 판정(timing.code)이 확정된 '모든 날'을 셌다. 그러나 실제 이메일은
regime_core.evaluate_alert_transitions 상태머신(2일 확정 + 발동 후 재무장)을 통과한
날에만 나간다. 같은 entry 구간이 30일 이어져도 메일은 1통이다 → 두 모집단은 다르다.

이제 한 번의 워크포워드에서 두 모드를 동시에 집계한다:
  - Mode="verdict" : 화면 판정 기준 (기존 v1.1~1.3 로직 그대로 — 판별력 진단용)
  - Mode="alert"   : 라이브 알림과 *동일한* 상태머신 기준 (실전 성적표)
                     entry 발동 시 build_watchlist_plan 으로 R:R 게이트까지 재현해
                     alert_entry_pass / alert_entry_skip 으로 분리 → 게이트 효용 측정.
분석(analyze_ticker) 호출은 날짜당 1회로 공유하므로 실행 시간은 거의 늘지 않는다.

주의: 워치리스트 행별 사용자 설정(목표 매수가·RSI·200일선)은 과거 재현이 불가능하므로
watch 이벤트는 발동하지 않는다. 즉 alert 모드는 '시스템이 만드는 알림'만 측정한다.

v1.3 변경 (Sheets 일시 장애 내성)
--------------------------------
- gspread 호출 전부를 지수 백오프 재시도(`_gs`)로 감쌌다. 503/500/502/504/429 및
  네트워크 예외는 최대 6회(2·4·8·16·32·60초 + 지터, 누적 ~2분) 재시도한다.
  이유: 월 1회 무인 실행이라 일시적 503 한 번에 한 달치 결과가 통째로 날아간다.
  일간 워크플로는 다음날 자기복구되지만 이 잡은 그렇지 않다.
- 401/403(인증)·404(시트 없음) 같은 영구 오류는 재시도하지 않고 즉시 실패한다.

v1.2 변경 (진입 시점 현실화)
---------------------------
- **진입가를 신호일 종가 → 신호일 +ENTRY_LAG_DAYS(=1) 거래일 종가로 이동.**
  이유: 신호는 장 마감 종가로 확정되고 알림 메일은 그 *후* 16:00 ET 에 발송된다.
  즉 `close[t]` 는 구조적으로 체결 불가능한 가격이라 기존 집계는 실현 불가능한
  성과를 측정했다. 이제 실제로 잡을 수 있는 최초 가격으로 측정한다.
- forward-return / MFE·MAE / SPY 초과수익 모두 '진입 봉' 기준으로 재정렬
  (보유 h거래일 = 진입일로부터 h일). SPY 진입가도 같은 봉의 종가 → 알파 비교 정합.
- 결과 시트에 `Entry_Rule` 열 추가 → 구/신 규칙 행이 한 시트에 섞여도 구분 가능.

v1.1 변경
---------
- 초과수익(excess): 각 이벤트 +Nd 수익에서 SPY 동일 캘린더창 수익을 빼 베타 제거 → 알파 측정.
- 디플랩(de-flap): raw verdict 가 confirm_days(2) 연속 유지된 그날만 1회 이벤트로 확정,
  같은 code 는 cooldown_days(5) 내 재기록 금지 → 경계 진동(flapping)으로 인한 중복 폭증 제거.

흐름
----
  [1] 유니버스: ETF_Universe + Watchlist + Portfolios (합집합, 중복 제거)
  [2] SPY + 유니버스 장기 일봉 fetch (FMP /stable historical-price-eod/full)
  [3] 티커마다 워크포워드: hist[:D] 슬라이스 → analyze_ticker → 2일 확정 시 이벤트
      forward-return(+5/+20/+60일) · SPY 대비 초과수익 · MFE/MAE(20일창) 측정
  [4] 버킷별 집계: 이벤트수 · 승률 · 평균/중앙 수익 · MFE/MAE · 초과20d 평균 · 초과승률
  [5] Signal_Backtest 시트에 'run당 × 버킷당 1행' append (헤더 불일치 시 자동 갱신)

설계 메모
---------
- 무결성: 분류엔 D 이전 데이터만(엄격 슬라이스). 최소 220봉 선행. 미래 부족분 NaN.
- 실행 가능성: 분류일(t)과 진입일(t+ENTRY_LAG_DAYS)을 분리. 분류는 t 까지 데이터만 쓰고,
  진입가는 t 이후 봉 → 미래 훔쳐보기 없이 '메일 받고 다음날 매수' 흐름을 그대로 재현.
- SSOT: regime_core 를 '소비'만(재구현 금지). app.py·run_watchlist_alerts 와 동일 판정.
- 순수 엔진(_forward_metrics / walk_forward_events / aggregate_events)은 numpy/pandas 만 의존 →
  FMP·시트 없이 단위 테스트 가능(analyze_fn 주입). I/O·main 은 하단 분리.
- MFE/MAE 는 절대값(사이징·R:R 의 손절/목표 거리 입력). 초과수익은 점수익(5/20/60d)에만.

실행 주기: 월 1회 또는 workflow_dispatch 수동.
"""

from __future__ import annotations

import os
import sys
import json
import time
import random
import concurrent.futures
from datetime import datetime

import numpy as np
import pandas as pd
import pytz

# ── repo root 를 sys.path 에 추가 → regime_core(app.py와 동일 모듈) import ──────
#    (automation/ 하위에 두는 전제: dirname(dirname(file)) = 레포 루트)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import regime_core as rc  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# 환경변수 (기존 run_*.py 와 동일 시크릿). 테스트/CI 에서 import 만 해도 깨지지 않도록 .get 사용.
# ──────────────────────────────────────────────────────────────────────────
FMP_API_KEY      = os.environ.get("FMP_API_KEY", "")
GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")

# ── 상수 ───────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")

_SPREADSHEET_TITLE = "Quant_DB"

# 유니버스 소스 (다른 run_*.py 와 lockstep 한 시트명/컬럼 위치)
_ETF_UNIVERSE_WORKSHEET = "ETF_Universe"   # A열 = Ticker
_WATCHLIST_WORKSHEET    = "Watchlist"      # col idx 1 = Ticker  (_WL_COLS[1])
_PORTFOLIO_WORKSHEET    = "Portfolios"     # col idx 2 = Ticker  ([ID,Account,Ticker,...])

# 결과 시트
_RESULT_WORKSHEET = "Signal_Backtest"
_RESULT_COLS = [
    "Run_Date", "History_Start", "History_End", "Universe_Size", "Verdict",
    "Event_Count", "WinRate_20d", "Ret_5d_Mean", "Ret_20d_Mean", "Ret_20d_Median",
    "Ret_60d_Mean", "MFE_20d_Mean", "MAE_20d_Mean",
    "Excess_20d_Mean", "ExcessWin_20d",   # v1.1: SPY 대비 알파
    "Entry_Rule",                          # v1.2: 진입가 규칙(close[t+N]) — 구/신 행 구분
    "Mode",                                # v1.4: verdict(화면 판정) | alert(실제 이메일)
    "Segment",                             # v1.5: all | etf | stock
]

_FMP_BASE    = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 8
_FETCH_WORKERS = 8           # FMP Starter 300 req/min 여유

# 백테스트 파라미터 (튜닝 한 곳)
HORIZONS       = (5, 20, 60)   # forward-return 측정 거래일
MFE_WINDOW     = 20            # MFE/MAE 측정 창(거래일)
MIN_PRIOR_BARS = 220           # 200일선 산출 위한 최소 선행 봉수
TEST_LOOKBACK  = 756           # 평가 구간(거래일 ≈ 3년)
CONFIRM_DAYS   = 2             # v1.1: raw code 가 N일 연속 유지돼야 이벤트 확정(라이브 알림과 동일 철학)
COOLDOWN_DAYS  = 5             # v1.1: 같은 code 재기록 최소 간격(경계 진동 압축)
ENTRY_LAG_DAYS = 1             # v1.2: 신호일(t) 대비 실제 진입 거래일 지연.
                               #   1 = 메일(16:00 ET) 받고 '다음 거래일' 매수 = 라이브 구조.
                               #   0 = 구버전(신호일 종가 진입) — 체결 불가능. 비교용으로만.
HISTORY_LIMIT  = MIN_PRIOR_BARS + TEST_LOOKBACK + max(HORIZONS) + ENTRY_LAG_DAYS + 40

# 결과 시트 provenance — 어떤 진입 규칙으로 잰 행인지 표시(구/신 혼재 방지)
_ENTRY_RULE_LABEL = f"close[t+{ENTRY_LAG_DAYS}]"

# 집계 대상 버킷 (regime_core evaluate_timing code 와 일치, unknown 제외)
BUCKETS = ("entry", "wait", "overheat", "trend_break", "avoid")

# v1.4 — 실제 이메일 알림 기준 버킷 (run_watchlist_alerts.py 와 동일 상태머신)
ALERT_ENABLED_EVENTS = ("entry", "risk", "watch")   # _WL_ALERT_DEFAULT 와 동일
ALERT_BUCKETS = (
    "alert_entry_pass",     # 매수 메일 발송 + R:R 게이트 통과 → 실제로 사는 신호
    "alert_entry_skip",     # 매수 메일 발송 + 게이트 미통과(건너뛰기 권고)
    "alert_entry_na",       # 매수 메일 발송 + 게이트 판단 보류(플랜 산출 불가)
    "alert_risk",           # 위험 알림
    "alert_entry_invalid",  # 직전 매수 신호 조건 해제(무효화)
)


# ════════════════════════════════════════════════════════════════════════════
# 순수 엔진 (numpy/pandas 만 의존 — FMP·시트 없이 테스트 가능)
# ════════════════════════════════════════════════════════════════════════════

def _forward_metrics(close, high, low, pos: int, horizons=HORIZONS,
                     mfe_window: int = MFE_WINDOW, spy_arr=None,
                     entry_lag: int = ENTRY_LAG_DAYS) -> dict:
    """신호일 pos → 진입봉 epos(=pos+entry_lag) 종가 기준 forward-return / 초과수익 / MFE·MAE.

    - entry_price : close[epos] — 실제로 체결 가능한 최초 종가 (미래 부족 시 NaN)
    - ret_{h}d    : epos+h 종가 / 진입가 - 1            (보유 h거래일; 미래 부족 시 NaN)
    - excess_{h}d : ret_{h}d - (SPY 동일창 수익)        (spy_arr 정렬 제공 시; 베타 제거 알파)
    - mfe / mae   : [epos+1, epos+mfe_window] 최고/최저 / 진입가 - 1 (절대값, 사이징 입력)
    spy_arr: 종목 거래일에 정렬(ffill)된 SPY 종가 배열(없으면 초과수익 NaN).
             SPY 진입가도 epos 종가를 써서 종목과 같은 봉에서 출발 → 알파 비교 정합.
    entry_lag: 신호일 대비 진입 지연 거래일수. 0 이면 구버전(신호일 종가 진입).
    """
    n = len(close)
    out = {f"ret_{h}d": np.nan for h in horizons}
    out.update({f"excess_{h}d": np.nan for h in horizons})
    out["mfe"] = np.nan
    out["mae"] = np.nan
    out["entry_price"] = np.nan
    out["entry_pos"] = -1
    if pos < 0 or pos >= n:
        return out
    # 진입봉: 신호일 종가로 판정 → 메일 발송 → entry_lag 거래일 뒤 체결
    epos = pos + int(max(0, entry_lag))
    if epos >= n:
        return out          # 진입할 미래 봉이 없음 → 측정 불가(호출부에서 이벤트 제외)
    entry = float(close[epos])
    if not np.isfinite(entry) or entry <= 0:
        return out
    out["entry_price"] = entry
    out["entry_pos"] = epos

    spy_entry = None
    if spy_arr is not None and epos < len(spy_arr) and np.isfinite(spy_arr[epos]) and spy_arr[epos] > 0:
        spy_entry = float(spy_arr[epos])

    for h in horizons:
        j = epos + h
        if j < n and np.isfinite(close[j]):
            r = float(close[j]) / entry - 1.0
            out[f"ret_{h}d"] = r
            if spy_entry is not None and j < len(spy_arr) and np.isfinite(spy_arr[j]):
                out[f"excess_{h}d"] = r - (float(spy_arr[j]) / spy_entry - 1.0)

    end = min(epos + mfe_window, n - 1)
    if end > epos:
        hwin = high[epos + 1:end + 1]
        lwin = low[epos + 1:end + 1]
        if np.any(np.isfinite(hwin)):
            out["mfe"] = float(np.nanmax(hwin)) / entry - 1.0
        if np.any(np.isfinite(lwin)):
            out["mae"] = float(np.nanmin(lwin)) / entry - 1.0
    return out


def _alert_bucket(ev: dict, hist_slice, analysis: dict):
    """발동된 알림 1건 → 집계 버킷. entry 는 R:R 게이트로 pass/skip/na 분리.

    라이브에서 게이트는 메일을 '막지' 않고 라벨만 바꾼다(decorate_entry_alert).
    따라서 여기서도 억제하지 않고 버킷만 나눠, 게이트가 실제로 걸러주는지 측정한다.
    """
    e = str((ev or {}).get("event") or "")
    if e == "risk":
        return "alert_risk"
    if e == "entry_invalid":
        return "alert_entry_invalid"
    if e != "entry":
        return None                      # watch/exit/price/regime 은 집계 대상 아님
    try:
        plan = rc.build_watchlist_plan(hist_slice, analysis)
        gate = str((plan or {}).get("gate") or "na")
    except Exception:
        gate = "na"
    if gate == "na":
        return "alert_entry_na"
    if gate in ("skip", "avoid"):
        return "alert_entry_skip"
    return "alert_entry_pass"


def walk_forward_events(hist: pd.DataFrame, spy_close=None,
                        min_prior: int = MIN_PRIOR_BARS,
                        test_lookback: int = TEST_LOOKBACK,
                        horizons=HORIZONS, mfe_window: int = MFE_WINDOW,
                        confirm_days: int = CONFIRM_DAYS,
                        cooldown_days: int = COOLDOWN_DAYS,
                        entry_lag: int = ENTRY_LAG_DAYS,
                        alert_enabled=ALERT_ENABLED_EVENTS,
                        analyze_fn=None):
    """한 티커의 워크포워드 평가 (v1.1: 2일 확정 + 쿨다운 디플랩).

    각 평가일 i 에서 hist[:i+1] → analyze_fn → raw code. 분류는 i 까지 데이터만(미래 차단).
    raw code 가 confirm_days 연속 유지된 '그날' 1회 확정 → 직전 기록과 다르거나(또는 같아도
    cooldown_days 경과 시) 이벤트로 기록. forward 측정만 미래 종가 참조.

    v1.2: 확정일(t)과 진입일(t+entry_lag)을 분리한다. 진입 봉이 데이터 끝을 넘어가
    체결 자체가 불가능한 이벤트는 기록하지 않는다(측정 불가 이벤트로 카운트 오염 방지).

    v1.4: 같은 루프에서 라이브 알림 상태머신(evaluate_alert_transitions)도 함께 돌려
    '실제로 메일이 나갔을 날'만 뽑은 alert 이벤트를 별도로 반환한다. analyze 호출은
    날짜당 1회를 두 모드가 공유하므로 추가 비용이 거의 없다.

    반환: (events, alert_events, first_eval_date, last_eval_date)
    """
    if analyze_fn is None:
        analyze_fn = rc.analyze_ticker

    events: list[dict] = []
    alert_events: list[dict] = []
    if hist is None or hist.empty or "Close" not in hist.columns:
        return events, alert_events, None, None

    h = hist.sort_index()
    close = pd.to_numeric(h["Close"], errors="coerce").to_numpy(dtype=float)
    high = (pd.to_numeric(h["High"], errors="coerce").to_numpy(dtype=float)
            if "High" in h.columns else close.copy())
    low = (pd.to_numeric(h["Low"], errors="coerce").to_numpy(dtype=float)
           if "Low" in h.columns else close.copy())
    dates = h.index
    n = len(close)
    if n < min_prior + 2:
        return events, alert_events, None, None

    # SPY 를 종목 거래일에 정렬(ffill) → 같은 캘린더창 초과수익 계산
    spy_arr = None
    if spy_close is not None:
        try:
            spy_arr = (pd.to_numeric(spy_close, errors="coerce")
                       .reindex(dates, method="ffill").to_numpy(dtype=float))
        except Exception:
            spy_arr = None

    start_i = max(min_prior, n - test_lookback)
    first_eval_date = None
    last_eval_date = None

    _alert_state = ""       # v1.4: 알림 상태머신 누적 상태(JSON) — 티커별로 이어짐
    pending_code = None     # 현재 연속 유지 중인 raw code
    pending_count = 0       # 연속 유지 일수
    last_rec_code = None    # 마지막으로 '기록'된 code
    last_rec_pos = -10 ** 9  # 마지막 기록 위치(쿨다운용)

    for i in range(start_i, n):
        date_i = dates[i]
        slice_ = h.iloc[:i + 1]
        spy_slice = None
        if spy_close is not None:
            try:
                spy_slice = spy_close.loc[:date_i]
            except Exception:
                spy_slice = spy_close
        try:
            res = analyze_fn(slice_, spy_close=spy_slice)
            code = (res.get("timing") or {}).get("code", "unknown")
        except Exception:
            res, code = None, "unknown"

        # ── [모드 B] 실제 이메일 알림 재현 (상태머신 SSOT 그대로 호출) ──
        if res is not None and alert_enabled:
            try:
                _fired, _alert_state = rc.evaluate_alert_transitions(
                    res, alert_enabled, _alert_state,
                    today_str=str(pd.Timestamp(date_i).date()),
                    price=float(close[i]) if np.isfinite(close[i]) else None,
                )
            except Exception:
                _fired = []
            for _ev_a in (_fired or []):
                _bucket = _alert_bucket(_ev_a, slice_, res)
                if _bucket is None:
                    continue
                _fma = _forward_metrics(close, high, low, i, horizons, mfe_window,
                                        spy_arr=spy_arr, entry_lag=entry_lag)
                if not np.isfinite(_fma.get("entry_price", np.nan)):
                    continue
                _ea = {"date": str(pd.Timestamp(date_i).date()),
                       "entry_date": str(pd.Timestamp(dates[int(_fma["entry_pos"])]).date()),
                       "code": _bucket}
                _ea.update(_fma)
                alert_events.append(_ea)

        if first_eval_date is None:
            first_eval_date = date_i
        last_eval_date = date_i

        # 연속 유지 카운트
        if code == pending_code:
            pending_count += 1
        else:
            pending_code = code
            pending_count = 1

        if code not in BUCKETS:
            continue
        # 확정: confirm_days 에 '도달한 그날'만 1회 (이후 같은 run 은 재발동 안 함)
        if pending_count != confirm_days:
            continue
        # de-dup: 직전 기록과 다른 code 이거나, 같아도 쿨다운 경과 시에만 기록
        if (code != last_rec_code) or (i - last_rec_pos >= cooldown_days):
            fm = _forward_metrics(close, high, low, i, horizons, mfe_window,
                                  spy_arr=spy_arr, entry_lag=entry_lag)
            _ep = fm.get("entry_price", np.nan)
            if not np.isfinite(_ep):
                continue        # 진입 봉 없음 → 체결 불가. 기록/쿨다운 모두 건드리지 않음
            _epos = int(fm.get("entry_pos", i))
            ev = {"date": str(pd.Timestamp(date_i).date()),          # 신호 확정일
                  "entry_date": str(pd.Timestamp(dates[_epos]).date()),  # 실제 진입일
                  "code": code}
            ev.update(fm)
            events.append(ev)
            last_rec_code = code
            last_rec_pos = i

    return events, alert_events, first_eval_date, last_eval_date


def _nan_pct(arr, fn) -> float:
    """fn(유한값) * 100, 소수 2자리. 유효값 없으면 NaN."""
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return np.nan
    return round(float(fn(a)) * 100.0, 2)


def aggregate_events(events: list[dict], buckets=BUCKETS) -> dict:
    """이벤트 리스트 → 버킷별 통계 dict. buckets 로 verdict/alert 모드 공용."""
    agg = {}
    for code in buckets:
        evs = [e for e in events if e.get("code") == code]
        r20 = np.array([e.get("ret_20d", np.nan) for e in evs], dtype=float)
        valid20 = r20[np.isfinite(r20)]
        winrate = round(float(np.mean(valid20 > 0)) * 100.0, 2) if valid20.size else np.nan
        ex20 = np.array([e.get("excess_20d", np.nan) for e in evs], dtype=float)
        ex20v = ex20[np.isfinite(ex20)]
        excess_win = round(float(np.mean(ex20v > 0)) * 100.0, 2) if ex20v.size else np.nan
        agg[code] = {
            "count": len(evs),
            "winrate_20d": winrate,
            "ret_5d_mean":  _nan_pct([e.get("ret_5d") for e in evs], np.mean),
            "ret_20d_mean": _nan_pct(r20, np.mean),
            "ret_20d_median": _nan_pct(r20, np.median),
            "ret_60d_mean": _nan_pct([e.get("ret_60d") for e in evs], np.mean),
            "mfe_20d_mean": _nan_pct([e.get("mfe") for e in evs], np.mean),
            "mae_20d_mean": _nan_pct([e.get("mae") for e in evs], np.mean),
            "excess_20d_mean": _nan_pct(ex20, np.mean),
            "excess_win_20d": excess_win,
        }
    return agg


def build_result_rows(agg: dict, run_date: str, hist_start: str, hist_end: str,
                      universe_size: int, buckets=BUCKETS,
                      mode: str = "verdict", segment: str = "all") -> list[list]:
    """집계 dict → Signal_Backtest 시트 행들(_RESULT_COLS 순서).

    mode: "verdict"(화면 판정) | "alert"(실제 이메일 발송 기준) — 시트 Mode 열로 구분.
    """
    def _cell(v):
        return "" if (v is None or (isinstance(v, float) and not np.isfinite(v))) else v
    rows = []
    for code in buckets:
        a = agg.get(code, {})
        rows.append([
            run_date, hist_start, hist_end, universe_size, code,
            _cell(a.get("count")), _cell(a.get("winrate_20d")),
            _cell(a.get("ret_5d_mean")), _cell(a.get("ret_20d_mean")),
            _cell(a.get("ret_20d_median")), _cell(a.get("ret_60d_mean")),
            _cell(a.get("mfe_20d_mean")), _cell(a.get("mae_20d_mean")),
            _cell(a.get("excess_20d_mean")), _cell(a.get("excess_win_20d")),
            _ENTRY_RULE_LABEL, mode, segment,
        ])
    return rows


# ════════════════════════════════════════════════════════════════════════════
# I/O — FMP 데이터 / Google Sheets (자동화 전용)
# ════════════════════════════════════════════════════════════════════════════

# ── Sheets 일시 장애 재시도 (v1.3) ────────────────────────────────────────────
_GS_MAX_ATTEMPTS = 6
_GS_BACKOFF      = (2, 4, 8, 16, 32, 60)   # 초 — 누적 최대 약 2분
_GS_RETRY_STATUS = {429, 500, 502, 503, 504}


def _gs_is_transient(exc) -> bool:
    """재시도할 가치가 있는 예외인가? (인증/권한/부재 오류는 재시도 무의미)"""
    import requests
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        requests.exceptions.ChunkedEncodingError)):
        return True
    code = None
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
    if code is None:
        # gspread APIError 는 args[0] 에 dict 를 담기도 한다
        try:
            a0 = exc.args[0]
            if isinstance(a0, dict):
                code = int((a0.get("error") or {}).get("code") or a0.get("code"))
        except Exception:
            code = None
    if code is None:
        return False
    return int(code) in _GS_RETRY_STATUS


def _gs(fn, *args, **kwargs):
    """gspread 호출 재시도 래퍼. 일시 오류만 지수 백오프 + 지터로 재시도.

    사용: _gs(gc.open, TITLE) / _gs(ws.get_all_values) / _gs(ws.update, rows, range_name=...)
    """
    import gspread
    last = None
    for attempt in range(_GS_MAX_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.SpreadsheetNotFound:
            raise                                  # 영구 오류 — 재시도 무의미
        except gspread.exceptions.WorksheetNotFound:
            raise
        except Exception as exc:                   # noqa: BLE001
            if not _gs_is_transient(exc) or attempt == _GS_MAX_ATTEMPTS - 1:
                raise
            last = exc
            wait = _GS_BACKOFF[min(attempt, len(_GS_BACKOFF) - 1)]
            wait += random.uniform(0, wait * 0.25)  # 지터 — 동시 재시도 충돌 완화
            print(f"[WARN] Sheets 일시 오류({type(exc).__name__}) — "
                  f"{wait:.1f}초 후 재시도 {attempt + 1}/{_GS_MAX_ATTEMPTS - 1}: {exc}", flush=True)
            time.sleep(wait)
    raise last  # 도달 불가(방어)


def get_gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(GSPREAD_KEY_JSON)
    creds = Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)


def _fmp_price_history(ticker: str, limit: int = HISTORY_LIMIT) -> pd.DataFrame:
    """app.py·run_watchlist_alerts 와 동일한 /stable historical-price-eod/full.
    OHLCV(날짜 오름차순 인덱스) DataFrame 반환. 실패 시 빈 DataFrame."""
    import requests
    if not FMP_API_KEY:
        return pd.DataFrame()
    try:
        r = requests.get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker}&limit={limit}&apikey={FMP_API_KEY}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "date" not in df.columns:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df = df.rename(columns={"close": "Close", "open": "Open", "high": "High",
                                "low": "Low", "volume": "Volume", "adjClose": "Adj Close"})
        for col in ["Close", "Open", "High", "Low", "Volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


def _batch_fetch_history(tickers: list, limit: int = HISTORY_LIMIT) -> dict:
    """ThreadPoolExecutor 병렬 fetch → {ticker: DataFrame}. (app._fmp_batch_price_history 동일 철학)"""
    out = {}
    if not tickers:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
        futs = {ex.submit(_fmp_price_history, tk, limit): tk for tk in tickers}
        for fut in concurrent.futures.as_completed(futs):
            tk = futs[fut]
            try:
                df = fut.result()
                if not df.empty:
                    out[tk] = df
            except Exception:
                pass
    return out


def _read_col(ws, col_idx: int) -> list:
    """워크시트 헤더 제외 특정 컬럼의 비어있지 않은 값들(대문자 정리)."""
    vals = _gs(ws.get_all_values) or []
    out = []
    for row in vals[1:]:
        if len(row) > col_idx:
            t = str(row[col_idx]).strip().upper()
            if t:
                out.append(t)
    return out


def load_universe(gc):
    """ETF_Universe + Watchlist + Portfolios 합집합(중복 제거).

    v1.5: (tickers, segment_map) 반환. segment_map[ticker] ∈ {"etf", "stock"} —
    ETF_Universe 시트 소속이면 etf, 아니면 stock.
    """
    sh = _gs(gc.open, _SPREADSHEET_TITLE)
    titles = {ws.title for ws in _gs(sh.worksheets)}
    tickers: set[str] = set()
    etf_set: set[str] = set()

    sources = [
        (_ETF_UNIVERSE_WORKSHEET, 0),
        (_WATCHLIST_WORKSHEET, 1),
        (_PORTFOLIO_WORKSHEET, 2),
    ]
    for title, col_idx in sources:
        if title not in titles:
            print(f"[INFO] '{title}' 시트 없음 — 스킵")
            continue
        try:
            ws = _gs(sh.worksheet, title)
            got = _read_col(ws, col_idx)
            tickers.update(got)
            if title == _ETF_UNIVERSE_WORKSHEET:
                etf_set.update(got)
            print(f"[OK] '{title}' 에서 {len(got)}개 로드")
        except Exception as e:
            print(f"[WARN] '{title}' 로드 실패 — 스킵: {e}")

    tickers.discard("SPY")  # SPY 는 벤치마크로 별도 fetch
    etf_set.discard("SPY")
    uni = sorted(tickers)
    seg = {t: ("etf" if t in etf_set else "stock") for t in uni}
    print(f"[INFO] 세그먼트: ETF {sum(1 for v in seg.values() if v == 'etf')}종목 · "
          f"개별주 {sum(1 for v in seg.values() if v == 'stock')}종목")
    return uni, seg


def open_result_worksheet(gc):
    """Signal_Backtest 탭. 없으면 생성 + 헤더. 헤더가 현재 스키마와 다르면 헤더만 갱신(마이그레이션)."""
    sh = _gs(gc.open, _SPREADSHEET_TITLE)
    titles = [ws.title for ws in _gs(sh.worksheets)]
    last_col = chr(ord("A") + len(_RESULT_COLS) - 1)
    if _RESULT_WORKSHEET in titles:
        ws = _gs(sh.worksheet, _RESULT_WORKSHEET)
        try:
            if (_gs(ws.row_values, 1) or []) != _RESULT_COLS:
                _gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1",
                          value_input_option="USER_ENTERED")
                print("[INFO] Signal_Backtest 헤더 갱신(스키마 변경 반영)")
        except Exception:
            pass
        return ws
    ws = _gs(sh.add_worksheet, title=_RESULT_WORKSHEET, rows=2000, cols=len(_RESULT_COLS))
    _gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1", value_input_option="USER_ENTERED")
    return ws


def _safe_append_rows(ws, rows, ncols: int, value_input_option: str = "USER_ENTERED") -> None:
    """append_row 계단식 드리프트 회피 — A열 기준 마지막 다음 행에 update. (app.py 동일 로직)"""
    if not rows:
        return
    if not isinstance(rows[0], (list, tuple)):
        rows = [rows]
    rows = [list(r) for r in rows if r is not None]
    if not rows:
        return
    existing = _gs(ws.get_all_values) or []
    last_row = 0
    for idx, r in enumerate(existing, start=1):
        if any(str(c).strip() != "" for c in r):
            last_row = idx
    start_row = last_row + 1
    end_row = start_row + len(rows) - 1
    try:
        if end_row > ws.row_count:
            _gs(ws.add_rows, end_row - ws.row_count + 50)
    except Exception:
        pass
    last_col = chr(ord("A") + max(0, ncols - 1))
    _gs(ws.update, rows, range_name=f"A{start_row}:{last_col}{end_row}",
              value_input_option=value_input_option)


# ════════════════════════════════════════════════════════════════════════════
# 오케스트레이션
# ════════════════════════════════════════════════════════════════════════════

SEGMENTS = ("all", "etf", "stock")


def run_backtest(universe: list, spy_hist: pd.DataFrame, hist_cache: dict,
                 segment_map: dict | None = None):
    """유니버스 전체 워크포워드 → (aggs, meta).

    v1.5: aggs[(mode, segment)] = 집계 dict. mode ∈ {verdict, alert},
    segment ∈ SEGMENTS. segment_map 미제공 시 전부 stock 으로 간주.
    """
    spy_close = spy_hist["Close"] if (spy_hist is not None and "Close" in spy_hist.columns) else None

    all_events: list[dict] = []
    all_alerts: list[dict] = []
    eval_starts, eval_ends = [], []
    n_with_data = 0

    for tk in universe:
        hist = hist_cache.get(tk)
        if hist is None or hist.empty:
            continue
        events, alerts, d0, d1 = walk_forward_events(hist, spy_close=spy_close)
        if d0 is not None:
            n_with_data += 1
            eval_starts.append(pd.Timestamp(d0))
            eval_ends.append(pd.Timestamp(d1))
        _seg = (segment_map or {}).get(tk, "stock")
        for _e in events:
            _e["segment"] = _seg
        for _a in alerts:
            _a["segment"] = _seg
        all_events.extend(events)
        all_alerts.extend(alerts)

    def _seg_filter(evs, seg):
        return evs if seg == "all" else [e for e in evs if e.get("segment") == seg]

    aggs = {}
    for seg in SEGMENTS:
        aggs[("verdict", seg)] = aggregate_events(_seg_filter(all_events, seg))
        aggs[("alert", seg)] = aggregate_events(_seg_filter(all_alerts, seg),
                                                buckets=ALERT_BUCKETS)
    _n_etf = sum(1 for t in universe if (segment_map or {}).get(t) == "etf")
    meta = {
        "universe_size": n_with_data,
        "hist_start": str(min(eval_starts).date()) if eval_starts else "",
        "hist_end": str(max(eval_ends).date()) if eval_ends else "",
        "total_events": len(all_events),
        "total_alerts": len(all_alerts),
        "n_etf": _n_etf,
        "n_stock": len(universe) - _n_etf,
    }
    return aggs, meta


def _print_summary(aggs: dict, meta: dict) -> None:
    print(f"\n[백테스트 요약] 유니버스 {meta['universe_size']}종목 · "
          f"구간 {meta['hist_start']}~{meta['hist_end']} · "
          f"판정 이벤트 {meta['total_events']} · 알림 이벤트 {meta.get('total_alerts', 0)} · "
          f"ETF {meta.get('n_etf', 0)} / 개별주 {meta.get('n_stock', 0)}")

    def _s(v):
        return "-" if (v is None or (isinstance(v, float) and not np.isfinite(v))) else f"{v}"

    def _table(title, agg_d, buckets):
        print(f"\n── {title} ──")
        print(f"{'버킷':<20}{'N':>6}{'승률20d':>9}{'평균5d':>8}{'평균20d':>8}{'중앙20d':>8}"
              f"{'평균60d':>8}{'MFE20':>7}{'MAE20':>7}{'초과20d':>8}{'초과승률':>9}")
        for code in buckets:
            a = agg_d.get(code, {})
            print(f"{code:<20}{a.get('count', 0):>6}{_s(a.get('winrate_20d')):>9}"
                  f"{_s(a.get('ret_5d_mean')):>8}{_s(a.get('ret_20d_mean')):>8}"
                  f"{_s(a.get('ret_20d_median')):>8}{_s(a.get('ret_60d_mean')):>8}"
                  f"{_s(a.get('mfe_20d_mean')):>7}{_s(a.get('mae_20d_mean')):>7}"
                  f"{_s(a.get('excess_20d_mean')):>8}{_s(a.get('excess_win_20d')):>9}")

    _seg_kr = {"all": "전체", "etf": "ETF만", "stock": "개별주만"}
    for seg in SEGMENTS:
        for mode, buckets in (("alert", ALERT_BUCKETS), ("verdict", BUCKETS)):
            _t = "실제 이메일 발송 기준" if mode == "alert" else "화면 판정 기준"
            _table(f"[{mode}/{seg}] {_t} — {_seg_kr.get(seg, seg)}",
                   aggs.get((mode, seg), {}), buckets)


def main():
    if not FMP_API_KEY or not GSPREAD_KEY_JSON:
        print("[ERROR] FMP_API_KEY / GSPREAD_KEY 환경변수 필요 — 중단")
        return 1

    t0 = time.time()
    run_date = datetime.now(_ET).strftime("%Y-%m-%d %H:%M")
    print(f"[START] 신호 백테스트 run_date={run_date} (ET) · confirm={CONFIRM_DAYS}d cooldown={COOLDOWN_DAYS}d")

    gc = get_gspread_client()

    universe, segment_map = load_universe(gc)
    print(f"[STEP1] 유니버스 {len(universe)}종목")
    if not universe:
        print("[INFO] 유니버스 비어 있음 — 중단")
        return 0

    spy_hist = _fmp_price_history("SPY")
    if spy_hist.empty:
        print("[WARN] SPY 이력 fetch 실패 — 초과수익 NaN 으로 진행")
    hist_cache = _batch_fetch_history(universe)
    print(f"[STEP2] 이력 확보 {len(hist_cache)}/{len(universe)}종목 "
          f"(SPY {'OK' if not spy_hist.empty else '실패'})")

    aggs, meta = run_backtest(universe, spy_hist, hist_cache, segment_map=segment_map)
    _print_summary(aggs, meta)

    rows = []
    for seg in SEGMENTS:
        rows += build_result_rows(aggs.get(("alert", seg), {}), run_date,
                                  meta["hist_start"], meta["hist_end"],
                                  meta["universe_size"], buckets=ALERT_BUCKETS,
                                  mode="alert", segment=seg)
        rows += build_result_rows(aggs.get(("verdict", seg), {}), run_date,
                                  meta["hist_start"], meta["hist_end"],
                                  meta["universe_size"], mode="verdict", segment=seg)
    try:
        ws = open_result_worksheet(gc)
        _safe_append_rows(ws, rows, ncols=len(_RESULT_COLS))
        print(f"[OK] '{_RESULT_WORKSHEET}' 에 {len(rows)}행 저장")
    except Exception as e:
        print(f"[ERROR] 결과 저장 실패: {e}")
        return 1

    print(f"[DONE] {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
