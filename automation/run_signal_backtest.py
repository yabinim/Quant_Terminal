# -*- coding: utf-8 -*-
"""
run_signal_backtest.py
──────────────────────
GitHub Actions 자동 실행: 레짐 엔진 신호 자기검증(워크포워드 백테스트). 월 1회/수동.

목적
----
라이브와 *동일한* regime_core.analyze_ticker 를 과거 시점마다(미래 훔쳐보기 없이) 호출하여,
verdict(entry/wait/overheat/trend_break/avoid)가 켜진 '전환 시점' 이후 실제로 어떻게 됐는지
(+5/+20/+60일 수익 · MFE · MAE)를 버킷별로 집계한다.

  → "🎯 entry 가 정말 돈이 됐나? entry > wait > overheat > avoid 순으로 갈리나?"를 숫자로.

흐름
----
  [STEP 1] 유니버스 로드: ETF_Universe + Watchlist + Portfolios (합집합, 중복 제거)
  [STEP 2] SPY + 유니버스 장기 일봉 fetch (FMP /stable historical-price-eod/full)
  [STEP 3] 티커마다 워크포워드: hist[:D] 슬라이스 → analyze_ticker → verdict 전환 = 이벤트
           각 이벤트의 forward-return(+5/+20/+60일) · MFE/MAE(20일창) 측정
  [STEP 4] verdict 버킷별 집계: 이벤트수 · 승률(+20일) · 평균/중앙값 수익 · 평균 MFE/MAE
  [STEP 5] Signal_Backtest 시트에 'run당 × 버킷당 1행' append (스냅샷 누적)

설계 메모
---------
- 무결성: 분류에는 D 이전 데이터만 사용(엄격 슬라이스). 200일선 위해 최소 220봉 선행 요구.
  +N일 미래가 아직 없는 최근 이벤트의 해당 수익은 NaN.
- SSOT: regime_core 를 '소비'만 한다(재구현 금지). app.py·run_watchlist_alerts 와 동일 판정.
- 순수 엔진(_forward_metrics / walk_forward_events / aggregate_events)은 numpy/pandas만 의존 →
  FMP·시트 없이 단위 테스트 가능(analyze_fn 주입). I/O·main 은 파일 하단에 분리.
- 2번(사이징·R:R)이 MFE/MAE 분포를 직접 소비하므로 v1부터 MFE/MAE 를 결과에 담는다.
  이벤트는 dict 라 3번(확신점수)에서 태그 추가만으로 additive 확장 가능.

실행 주기: 월 1회 또는 workflow_dispatch 수동.
"""

from __future__ import annotations

import os
import sys
import json
import time
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
]

_FMP_BASE    = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 8
_FETCH_WORKERS = 8           # FMP Starter 300 req/min 여유

# 백테스트 파라미터
HORIZONS         = (5, 20, 60)   # forward-return 측정 거래일
MFE_WINDOW       = 20            # MFE/MAE 측정 창(거래일)
MIN_PRIOR_BARS   = 220          # 200일선 산출 위한 최소 선행 봉수 (그 전엔 평가 안 함)
TEST_LOOKBACK    = 756          # 평가 구간(거래일 ≈ 3년). 부하·표본 균형 — 튜닝 한 곳.
HISTORY_LIMIT    = MIN_PRIOR_BARS + TEST_LOOKBACK + max(HORIZONS) + 40  # fetch 봉수 여유

# 집계 대상 버킷 (regime_core evaluate_timing code 와 일치, unknown 제외)
BUCKETS = ("entry", "wait", "overheat", "trend_break", "avoid")


# ════════════════════════════════════════════════════════════════════════════
# 순수 엔진 (numpy/pandas 만 의존 — FMP·시트 없이 테스트 가능)
# ════════════════════════════════════════════════════════════════════════════

def _forward_metrics(close, high, low, pos: int,
                     horizons=HORIZONS, mfe_window: int = MFE_WINDOW) -> dict:
    """pos 시점 종가를 진입가로 보고 forward-return 과 MFE/MAE 산출.

    - ret_{h}d : pos+h 종가 / 진입가 - 1   (미래 봉 부족 시 NaN)
    - mfe      : [pos+1, pos+mfe_window] 최고가 / 진입가 - 1  (최대 상승, +)
    - mae      : [pos+1, pos+mfe_window] 최저가 / 진입가 - 1  (최대 하락, 보통 -)
    """
    n = len(close)
    out = {f"ret_{h}d": np.nan for h in horizons}
    out["mfe"] = np.nan
    out["mae"] = np.nan
    if pos < 0 or pos >= n:
        return out
    entry = float(close[pos])
    if not np.isfinite(entry) or entry <= 0:
        return out

    for h in horizons:
        j = pos + h
        if j < n and np.isfinite(close[j]):
            out[f"ret_{h}d"] = float(close[j]) / entry - 1.0

    end = min(pos + mfe_window, n - 1)
    if end > pos:
        hwin = high[pos + 1:end + 1]
        lwin = low[pos + 1:end + 1]
        if np.any(np.isfinite(hwin)):
            out["mfe"] = float(np.nanmax(hwin)) / entry - 1.0
        if np.any(np.isfinite(lwin)):
            out["mae"] = float(np.nanmin(lwin)) / entry - 1.0
    return out


def walk_forward_events(hist: pd.DataFrame, spy_close=None,
                        min_prior: int = MIN_PRIOR_BARS,
                        test_lookback: int = TEST_LOOKBACK,
                        horizons=HORIZONS, mfe_window: int = MFE_WINDOW,
                        analyze_fn=None):
    """한 티커의 워크포워드 평가.

    각 평가 시점 i 에서 hist[:i+1] 슬라이스를 analyze_fn 에 넘겨 verdict code 를 얻고,
    code 가 직전과 '다르게 전환'될 때만 이벤트로 기록(실제 매매 = 한 번 진입과 정합).
    분류는 i 까지의 데이터만 사용 → 미래 훔쳐보기 없음. forward 측정만 미래 종가 참조.

    반환: (events: list[dict], first_eval_date, last_eval_date)
    """
    if analyze_fn is None:
        analyze_fn = rc.analyze_ticker

    events: list[dict] = []
    if hist is None or hist.empty or "Close" not in hist.columns:
        return events, None, None

    h = hist.sort_index()
    close = pd.to_numeric(h["Close"], errors="coerce").to_numpy(dtype=float)
    high = (pd.to_numeric(h["High"], errors="coerce").to_numpy(dtype=float)
            if "High" in h.columns else close.copy())
    low = (pd.to_numeric(h["Low"], errors="coerce").to_numpy(dtype=float)
           if "Low" in h.columns else close.copy())
    dates = h.index
    n = len(close)
    if n < min_prior + 2:
        return events, None, None

    start_i = max(min_prior, n - test_lookback)
    prev_code = None
    first_eval_date = None
    last_eval_date = None

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
            code = "unknown"

        if first_eval_date is None:
            first_eval_date = date_i
        last_eval_date = date_i

        if code == "unknown":
            prev_code = "unknown"
            continue
        if prev_code is None:
            prev_code = code           # 최초 평가 = 기준점(발동 안 함)
            continue
        if code != prev_code:
            fm = _forward_metrics(close, high, low, i, horizons, mfe_window)
            ev = {"date": str(pd.Timestamp(date_i).date()),
                  "code": code, "entry_price": float(close[i])}
            ev.update(fm)
            events.append(ev)
        prev_code = code

    return events, first_eval_date, last_eval_date


def _nan_pct(arr, fn) -> float:
    """fn(유한값) * 100, 소수 2자리. 유효값 없으면 NaN."""
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return np.nan
    return round(float(fn(a)) * 100.0, 2)


def aggregate_events(events: list[dict]) -> dict:
    """이벤트 리스트 → 버킷별 통계 dict."""
    agg = {}
    for code in BUCKETS:
        evs = [e for e in events if e.get("code") == code]
        r20 = np.array([e.get("ret_20d", np.nan) for e in evs], dtype=float)
        valid20 = r20[np.isfinite(r20)]
        winrate = round(float(np.mean(valid20 > 0)) * 100.0, 2) if valid20.size else np.nan
        agg[code] = {
            "count": len(evs),
            "winrate_20d": winrate,
            "ret_5d_mean":  _nan_pct([e.get("ret_5d") for e in evs], np.mean),
            "ret_20d_mean": _nan_pct(r20, np.mean),
            "ret_20d_median": _nan_pct(r20, np.median),
            "ret_60d_mean": _nan_pct([e.get("ret_60d") for e in evs], np.mean),
            "mfe_20d_mean": _nan_pct([e.get("mfe") for e in evs], np.mean),
            "mae_20d_mean": _nan_pct([e.get("mae") for e in evs], np.mean),
        }
    return agg


def build_result_rows(agg: dict, run_date: str, hist_start: str, hist_end: str,
                      universe_size: int) -> list[list]:
    """집계 dict → Signal_Backtest 시트 행들(_RESULT_COLS 순서)."""
    def _cell(v):
        return "" if (v is None or (isinstance(v, float) and not np.isfinite(v))) else v
    rows = []
    for code in BUCKETS:
        a = agg.get(code, {})
        rows.append([
            run_date, hist_start, hist_end, universe_size, code,
            _cell(a.get("count")), _cell(a.get("winrate_20d")),
            _cell(a.get("ret_5d_mean")), _cell(a.get("ret_20d_mean")),
            _cell(a.get("ret_20d_median")), _cell(a.get("ret_60d_mean")),
            _cell(a.get("mfe_20d_mean")), _cell(a.get("mae_20d_mean")),
        ])
    return rows


# ════════════════════════════════════════════════════════════════════════════
# I/O — FMP 데이터 / Google Sheets (자동화 전용)
# ════════════════════════════════════════════════════════════════════════════

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
    vals = ws.get_all_values() or []
    out = []
    for row in vals[1:]:
        if len(row) > col_idx:
            t = str(row[col_idx]).strip().upper()
            if t:
                out.append(t)
    return out


def load_universe(gc) -> list:
    """ETF_Universe + Watchlist + Portfolios 합집합(중복 제거). 시트 없으면 해당 소스만 스킵."""
    sh = gc.open(_SPREADSHEET_TITLE)
    titles = {ws.title for ws in sh.worksheets()}
    tickers: set[str] = set()

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
            ws = sh.worksheet(title)
            got = _read_col(ws, col_idx)
            tickers.update(got)
            print(f"[OK] '{title}' 에서 {len(got)}개 로드")
        except Exception as e:
            print(f"[WARN] '{title}' 로드 실패 — 스킵: {e}")

    tickers.discard("SPY")  # SPY 는 벤치마크로 별도 fetch
    return sorted(tickers)


def open_result_worksheet(gc):
    """Signal_Backtest 탭. 없으면 생성 + 헤더."""
    sh = gc.open(_SPREADSHEET_TITLE)
    titles = [ws.title for ws in sh.worksheets()]
    if _RESULT_WORKSHEET in titles:
        return sh.worksheet(_RESULT_WORKSHEET)
    ws = sh.add_worksheet(title=_RESULT_WORKSHEET, rows=2000, cols=len(_RESULT_COLS))
    last_col = chr(ord("A") + len(_RESULT_COLS) - 1)
    ws.update([_RESULT_COLS], range_name=f"A1:{last_col}1", value_input_option="USER_ENTERED")
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
    existing = ws.get_all_values() or []
    last_row = 0
    for idx, r in enumerate(existing, start=1):
        if any(str(c).strip() != "" for c in r):
            last_row = idx
    start_row = last_row + 1
    end_row = start_row + len(rows) - 1
    try:
        if end_row > ws.row_count:
            ws.add_rows(end_row - ws.row_count + 50)
    except Exception:
        pass
    last_col = chr(ord("A") + max(0, ncols - 1))
    ws.update(rows, range_name=f"A{start_row}:{last_col}{end_row}",
              value_input_option=value_input_option)


# ════════════════════════════════════════════════════════════════════════════
# 오케스트레이션
# ════════════════════════════════════════════════════════════════════════════

def run_backtest(universe: list, spy_hist: pd.DataFrame, hist_cache: dict):
    """유니버스 전체 워크포워드 → (집계 dict, meta). hist_cache: {ticker: DataFrame}."""
    spy_close = spy_hist["Close"] if (spy_hist is not None and "Close" in spy_hist.columns) else None

    all_events: list[dict] = []
    eval_starts, eval_ends = [], []
    n_with_data = 0

    for tk in universe:
        hist = hist_cache.get(tk)
        if hist is None or hist.empty:
            continue
        events, d0, d1 = walk_forward_events(hist, spy_close=spy_close)
        if d0 is not None:
            n_with_data += 1
            eval_starts.append(pd.Timestamp(d0))
            eval_ends.append(pd.Timestamp(d1))
        all_events.extend(events)

    agg = aggregate_events(all_events)
    meta = {
        "universe_size": n_with_data,
        "hist_start": str(min(eval_starts).date()) if eval_starts else "",
        "hist_end": str(max(eval_ends).date()) if eval_ends else "",
        "total_events": len(all_events),
    }
    return agg, meta


def _print_summary(agg: dict, meta: dict) -> None:
    print(f"\n[백테스트 요약] 유니버스 {meta['universe_size']}종목 · "
          f"구간 {meta['hist_start']}~{meta['hist_end']} · 총 이벤트 {meta['total_events']}")
    print(f"{'버킷':<12}{'N':>6}{'승률20d':>9}{'평균5d':>9}{'평균20d':>9}{'중앙20d':>9}"
          f"{'평균60d':>9}{'MFE20':>8}{'MAE20':>8}")
    for code in BUCKETS:
        a = agg[code]
        def s(v):
            return "-" if (v is None or (isinstance(v, float) and not np.isfinite(v))) else f"{v}"
        print(f"{code:<12}{a['count']:>6}{s(a['winrate_20d']):>9}{s(a['ret_5d_mean']):>9}"
              f"{s(a['ret_20d_mean']):>9}{s(a['ret_20d_median']):>9}{s(a['ret_60d_mean']):>9}"
              f"{s(a['mfe_20d_mean']):>8}{s(a['mae_20d_mean']):>8}")


def main():
    if not FMP_API_KEY or not GSPREAD_KEY_JSON:
        print("[ERROR] FMP_API_KEY / GSPREAD_KEY 환경변수 필요 — 중단")
        return 1

    t0 = time.time()
    run_date = datetime.now(_ET).strftime("%Y-%m-%d")
    print(f"[START] 신호 백테스트 run_date={run_date} (ET)")

    gc = get_gspread_client()

    # STEP 1 — 유니버스
    universe = load_universe(gc)
    print(f"[STEP1] 유니버스 {len(universe)}종목")
    if not universe:
        print("[INFO] 유니버스 비어 있음 — 중단")
        return 0

    # STEP 2 — SPY + 유니버스 이력
    spy_hist = _fmp_price_history("SPY")
    if spy_hist.empty:
        print("[WARN] SPY 이력 fetch 실패 — RS 없이 진행")
    hist_cache = _batch_fetch_history(universe)
    print(f"[STEP2] 이력 확보 {len(hist_cache)}/{len(universe)}종목 "
          f"(SPY {'OK' if not spy_hist.empty else '실패'})")

    # STEP 3~4 — 워크포워드 + 집계
    agg, meta = run_backtest(universe, spy_hist, hist_cache)
    _print_summary(agg, meta)

    # STEP 5 — 저장
    rows = build_result_rows(agg, run_date, meta["hist_start"], meta["hist_end"],
                             meta["universe_size"])
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
