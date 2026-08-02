#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_satellite_backtest.py — 🛰️ HSA 위성 섹터 로테이션 백테스트 (읽기 전용 진단)

목적
────
주말 Hidden Alpha 이메일의 '위성 섹터 Top10'을 보고 Top5 를 보유하다가
순위가 바뀌면 즉시 교체하는 실제 운용 방식의 과거 성과를 재현한다.

  자본금 $5,000 · 최초 1~5위에 $1,000 씩 · 추가 납입 없음 · 소수점 매수

랭킹 로직은 fmp_extras.compute_satellite_top10 을 **재구현하지 않고 그대로 복제**한다
(SSOT 원칙 — 후보 풀·섹터 라벨은 fmp_extras 에서 import, 점수식만 과거 시점 슬라이스로 반복 계산).

  점수 = 1M×0.40 + 3M×0.40 + 6M×0.20   (1주는 노이즈 — 점수 제외)
  GICS 섹터당 최고점 1개만 챔피언 → 점수 내림차순
  히스토리 127봉 미만 티커 제외 (라이브와 동일)
  시장 필터 = SPY 종가 vs 최근 200봉 평균 (라이브의 spy.tail(200).mean() 과 동일)

검증 설계
─────────
· 미래 훔쳐보기 차단: 랭킹은 신호일 t 까지의 데이터만 사용.
· 체결 지연: 신호일(금요일 종가) → **다음 거래일(월요일) 종가** 체결.
  이메일이 주말에 오고 월요일 10시 이후 집행하는 실제 흐름과 동일.
· 슬리피지: 편도 0.05% (매수·매도 각각). 수수료 0 (Fidelity ETF·소수점 매수).
· 성과는 adjClose(배당 재투자) 기준, 랭킹은 close 기준 — 라이브 랭킹과 정합.
  adjClose 가 close 와 사실상 동일하면 배당 미반영으로 판단해 경고를 출력한다.

⚠️ 결과 해석 시 반드시 감안할 편향 (이 스크립트로 제거 불가)
────────────────────────────────────────────────────────────
 1) **후보 풀 선택 편향**: fmp_extras.SECTOR_THEME_ETFS 56개는 2026년 현재 시점에
    사람이 고른 목록이다. 과거로 돌리면 '미래를 아는 풀'이라 결과가 실제보다 좋게 나온다.
 2) **구간 편향**: FMP 계정 이력 한도가 1255봉(5년 롤링)이라 2020 코로나·2022 초입
    하락장이 데이터에 없다. 커버 구간은 대체로 강세장이다.
 → 따라서 절대 수익률은 '상한선'으로 보고, **조건 간 상대 비교**(주간 vs 월간,
   시장필터 유무, 밴드 룰 유무)에만 신뢰를 두는 것이 맞다.

실행
────
  python automation/diag_satellite_backtest.py            # 전체 백테스트
  python automation/diag_satellite_backtest.py --selftest  # 엔진 자체검증(네트워크 불필요)

아무것도 수정하지 않는다. Google Sheets `Satellite_Backtest` 탭에 결과 행만 append.
(GSPREAD_KEY 가 없으면 시트 기록은 건너뛰고 콘솔 출력만 한다.)
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime

import numpy as np
import pandas as pd
import pytz

# ── 리포 루트 + 자기 폴더를 sys.path 에 (실행 위치 무관) ─────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_extras as fx  # noqa: E402  — 후보 풀 SSOT

# ── 환경 ──────────────────────────────────────────────────────────────────────
FMP_API_KEY      = os.environ.get("FMP_API_KEY", "")
GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")

_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")

_FMP_BASE      = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT   = 12
_FETCH_WORKERS = 8            # FMP Starter 300 req/min 여유

_SPREADSHEET_TITLE = "Quant_DB"
_RESULT_WORKSHEET  = "Satellite_Backtest"

# ── 백테스트 파라미터 (튜닝 한 곳) ───────────────────────────────────────────
CAPITAL        = 5_000.0      # 시작 자본
SLOTS          = 5            # 보유 슬롯 (Top5)
BAND_SLOTS     = 7            # 3B 밴드 룰: Top7 안이면 계속 보유
SLIPPAGE       = 0.0005       # 편도 0.05%
COMMISSION_PER_TRADE = 0.0    # Fidelity HSA: 미국 주식·ETF 온라인 매매 $0 (확인 완료).
#   ⚠️ 단, 일부 소수 ETF 는 건당 $100 서비스 수수료 대상 — 후보 풀에 해당 종목이
#   있는지는 Fidelity 목록에서 직접 확인해야 한다. 민감도 테스트용 노브로 남겨둔다.
SELL_ASSESSMENT = 0.00002     # 매도 시 SEC 부과금 ≈ 원금 $1,000당 $0.02
ENTRY_LAG_DAYS = 1            # 신호일 → 체결일 (금 종가 신호 → 월 종가 체결)
HISTORY_LIMIT  = 1300         # FMP 실제 상한 1255봉 — 여유 요청

SEG_BARS       = 252          # 연도별 분해 단위(12개월)
SEG_MAX        = 6            # 최대 분해 구간 수

WARMUP_BARS    = 127          # 6M(126봉) 계산 최소 — compute_satellite_top10 과 동일
MA200_BARS     = 200          # 시장 필터
WINDOWS        = {"1년": 252, "2년": 504, "3년": 756}   # 거래일 기준

BENCH_TICKERS  = ("SPY", "QQQ")

# 점수 가중치 (fmp_extras.compute_satellite_top10 과 동일)
_W_1M, _W_3M, _W_6M = 0.40, 0.40, 0.20
_BARS_1M, _BARS_3M, _BARS_6M = 21, 63, 126

# ── 설정 그리드 ───────────────────────────────────────────────────────────────
FREQS      = ("weekly", "monthly")           # 1A/1B
SWAPS      = ("swap", "rebal")               # 2A(교체분만) / 2B(매주 균등 재조정)
SELLRULES  = ("top5", "top7")                # 3A / 3B
MKTFILTERS = ("none", "no_new", "all_cash")  # 4A / 4B / 4C

BASELINE = ("weekly", "swap", "top5", "none")   # 실제 운용 방식

_RESULT_COLS = [
    "Run_Date", "Window", "Freq", "Swap", "SellRule", "MktFilter",
    "Start", "End", "Capital", "Final_Equity", "Total_Ret_Pct", "CAGR_Pct",
    "MDD_Pct", "Sharpe", "Trades", "WinRate_Pct", "Turnover_x",
    "Slippage_Cost", "vs_SPY_pp", "Div_Basis",
]


# ══════════════════════════════════════════════════════════════════════════════
# 데이터 수집
# ══════════════════════════════════════════════════════════════════════════════
def _fmp_eod(ticker: str, endpoint: str, limit: int = HISTORY_LIMIT) -> pd.DataFrame:
    """/stable/historical-price-eod/{endpoint} → DatetimeIndex + 'px' 컬럼.

    endpoint='full'              : 원 종가 (랭킹용 — 라이브 compute_satellite_top10 과 동일)
    endpoint='dividend-adjusted' : 배당 재투자 반영 종가 (성과 측정용)
    """
    import requests
    if not FMP_API_KEY:
        return pd.DataFrame()
    for attempt in range(3):
        try:
            r = requests.get(
                f"{_FMP_BASE}/historical-price-eod/{endpoint}"
                f"?symbol={ticker}&limit={limit}&apikey={FMP_API_KEY}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code != 200:
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return pd.DataFrame()
            data = r.json()
            rows = data.get("historical", data) if isinstance(data, dict) else data
            if not isinstance(rows, list) or not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            if "date" not in df.columns:
                return pd.DataFrame()
            # dividend-adjusted 는 adjClose 로 오기도 하고 close 가 이미 조정치이기도 하다
            col = "adjClose" if "adjClose" in df.columns else "close"
            if col not in df.columns:
                return pd.DataFrame()
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"]).set_index("date").sort_index()
            df = df[~df.index.duplicated(keep="last")]
            out = pd.DataFrame(index=df.index)
            out["px"] = pd.to_numeric(df[col], errors="coerce")
            return out.dropna(subset=["px"])
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    return pd.DataFrame()


def _batch_fetch(tickers: list) -> tuple:
    """(raw_close{}, div_adj{}, fallback_list) — 두 엔드포인트를 병렬로 수집."""
    raw, adj = {}, {}
    jobs = [(tk, "full") for tk in tickers] + [(tk, "dividend-adjusted") for tk in tickers]
    with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
        futs = {ex.submit(_fmp_eod, tk, ep): (tk, ep) for tk, ep in jobs}
        for fut in concurrent.futures.as_completed(futs):
            tk, ep = futs[fut]
            try:
                df = fut.result()
            except Exception:
                df = pd.DataFrame()
            if df.empty:
                continue
            (raw if ep == "full" else adj)[tk] = df["px"]
    fallback = []
    for tk in list(raw):
        if tk not in adj:
            adj[tk] = raw[tk]          # 배당조정 실패 → 원 종가로 폴백
            fallback.append(tk)
    return raw, adj, fallback


def build_panels(raw: dict, adj: dict, calendar_ticker: str = "SPY"):
    """{ticker: Series} 2벌 → (close_df, adj_df) — SPY 거래일 캘린더에 정렬."""
    if calendar_ticker not in raw:
        raise RuntimeError(f"{calendar_ticker} 히스토리 확보 실패 — 캘린더 기준을 만들 수 없다")
    cal = raw[calendar_ticker].index
    close = pd.DataFrame(index=cal)
    adjp = pd.DataFrame(index=cal)
    for tk, s in raw.items():
        close[tk] = s.reindex(cal)
        a = adj.get(tk)
        adjp[tk] = (a.reindex(cal) if a is not None else s.reindex(cal))
    # 산발적 결측(휴장 차이 등)만 최대 3봉 보간 — 상장 이전 구간은 NaN 유지
    close = close.ffill(limit=3)
    adjp = adjp.ffill(limit=3)
    adjp = adjp.where(adjp > 0)
    return close, adjp


# ══════════════════════════════════════════════════════════════════════════════
# 랭킹 엔진 — compute_satellite_top10 의 과거 시점 복제
# ══════════════════════════════════════════════════════════════════════════════
def _trailing_return(vals: np.ndarray, bars: int):
    if len(vals) <= bars:
        return np.nan
    prev = vals[-1 - bars]
    if not np.isfinite(prev) or prev == 0:
        return np.nan
    return float((vals[-1] / prev - 1.0) * 100.0)


class RankEngine:
    """티커별 유효 종가 배열을 미리 잘라두고, 날짜별 챔피언 랭킹을 계산한다."""

    def __init__(self, close_df: pd.DataFrame):
        self.pool = fx.satellite_candidate_pool()          # {섹터: [후보들]}
        self.sector_of = {}
        self.series = {}
        for sec, cands in self.pool.items():
            for tk in cands:
                self.sector_of[tk] = sec
                if tk in close_df.columns:
                    s = close_df[tk].dropna()
                    if not s.empty:
                        self.series[tk] = (s.index.values, s.to_numpy(dtype=float))
        self._cache = {}

    def rank_at(self, date) -> list:
        """date(포함) 까지 데이터만으로 산출한 챔피언 랭킹 [{ticker, sector, score}, ...]."""
        key = pd.Timestamp(date)
        if key in self._cache:
            return self._cache[key]
        champions = []
        for sec, cands in self.pool.items():
            best = None
            for tk in cands:
                ser = self.series.get(tk)
                if ser is None:
                    continue
                idx, vals = ser
                n = int(np.searchsorted(idx, np.datetime64(key), side="right"))
                if n < WARMUP_BARS:                     # 라이브의 len(s) < 127 스킵과 동일
                    continue
                v = vals[:n]
                r1m = _trailing_return(v, _BARS_1M)
                r3m = _trailing_return(v, _BARS_3M)
                r6m = _trailing_return(v, _BARS_6M)
                if not all(np.isfinite(x) for x in (r1m, r3m, r6m)):
                    continue
                score = _W_1M * r1m + _W_3M * r3m + _W_6M * r6m
                if best is None or score > best["score"]:
                    best = {"ticker": tk, "sector": sec, "score": score}
            if best is not None:
                champions.append(best)
        champions.sort(key=lambda r: r["score"], reverse=True)
        self._cache[key] = champions
        return champions


def market_risk_on(spy_close: pd.Series, date) -> bool:
    """SPY 종가 vs 최근 200봉 평균 — 라이브 compute_satellite_top10 과 동일 정의."""
    s = spy_close.loc[:date].dropna()
    if len(s) < MA200_BARS:
        return True                                   # 판단 불가 시 정상 운용
    return bool(float(s.iloc[-1]) > float(s.tail(MA200_BARS).mean()))


# ══════════════════════════════════════════════════════════════════════════════
# 리밸런싱 날짜
# ══════════════════════════════════════════════════════════════════════════════
def signal_dates(index: pd.DatetimeIndex, freq: str) -> list:
    """freq 별 신호일 = 각 주/월의 마지막 거래일."""
    s = pd.Series(index, index=index)
    if freq == "weekly":
        grouped = s.groupby([index.isocalendar().year, index.isocalendar().week])
    else:
        grouped = s.groupby([index.year, index.month])
    return sorted(grouped.max().tolist())


# ══════════════════════════════════════════════════════════════════════════════
# 시뮬레이터
# ══════════════════════════════════════════════════════════════════════════════
def simulate(cfg: tuple, engine: RankEngine, close_df: pd.DataFrame, adj_df: pd.DataFrame,
             start_i: int, end_i: int, capital: float = CAPITAL) -> dict:
    """cfg=(freq, swap, sellrule, mktfilter) 로 [start_i, end_i] 구간을 시뮬레이션."""
    freq, swap, sellrule, mktfilter = cfg
    index = adj_df.index
    keep_thresh = SLOTS if sellrule == "top5" else BAND_SLOTS

    all_sig = signal_dates(index, freq)
    pos_of = {d: i for i, d in enumerate(index)}
    # 신호일 + 체결일(신호일 + ENTRY_LAG_DAYS)이 모두 구간 안에 들어오는 것만
    plan = []
    for d in all_sig:
        i = pos_of.get(pd.Timestamp(d))
        if i is None:
            continue
        j = i + ENTRY_LAG_DAYS
        if j > end_i or i < start_i:
            continue
        plan.append((i, j))
    if not plan:
        return {}

    cash = float(capital)
    shares: dict = {}          # {ticker: 주식수}
    basis: dict = {}           # {ticker: 취득원가($)}
    trades: list = []
    slip_cost = 0.0
    traded_notional = 0.0
    exec_map = {j: i for i, j in plan}
    rebal_log: list = []

    equity_dates, equity_vals = [], []
    sim_start = plan[0][1]

    def px(tk, i):
        v = adj_df[tk].iloc[i] if tk in adj_df.columns else np.nan
        return float(v) if pd.notna(v) and v > 0 else np.nan

    def sell(tk, i, frac=1.0):
        nonlocal cash, slip_cost, traded_notional
        p = px(tk, i)
        if not np.isfinite(p) or tk not in shares:
            return
        qty = shares[tk] * frac
        gross = qty * p
        if gross <= 0:
            return
        cost = gross * (SLIPPAGE + SELL_ASSESSMENT) + COMMISSION_PER_TRADE
        cash += gross - cost
        slip_cost += cost
        traded_notional += gross
        shares[tk] -= qty
        if frac >= 1.0 or shares[tk] <= 1e-9:
            b = basis.pop(tk, 0.0)
            ret_pct = ((gross - cost) / b - 1.0) * 100.0 if b > 1e-9 else float("nan")
            trades.append({"ticker": tk, "ret_pct": ret_pct, "exit": index[i]})
            shares.pop(tk, None)
        else:
            basis[tk] = basis.get(tk, 0.0) * (1 - frac)

    def buy(tk, i, dollars):
        nonlocal cash, slip_cost, traded_notional
        p = px(tk, i)
        if not np.isfinite(p) or dollars <= 0.01:
            return
        dollars = min(dollars, cash)
        if dollars <= COMMISSION_PER_TRADE:
            return
        cost = dollars * SLIPPAGE + COMMISSION_PER_TRADE
        qty = (dollars - cost) / p
        if qty <= 0:
            return
        cash -= dollars
        slip_cost += cost
        traded_notional += dollars
        shares[tk] = shares.get(tk, 0.0) + qty
        basis[tk] = basis.get(tk, 0.0) + dollars

    prev_held: set = set()
    maxw_track: list = []
    risk_on = True
    for i in range(sim_start, end_i + 1):
        if i in exec_map:
            prev_held = set(shares)
            sig_i = exec_map[i]
            sig_date = index[sig_i]
            champs = engine.rank_at(sig_date)
            rank_of = {c["ticker"]: n for n, c in enumerate(champs, 1)}
            risk_on = True
            if mktfilter != "none" and "SPY" in close_df.columns:
                risk_on = market_risk_on(close_df["SPY"], sig_date)

            if mktfilter == "all_cash" and not risk_on:
                keep, buys = [], []
            else:
                keep = [tk for tk in list(shares) if rank_of.get(tk, 10**6) <= keep_thresh]
                free = SLOTS - len(keep)
                if mktfilter == "no_new" and not risk_on:
                    buys = []
                else:
                    top = [c["ticker"] for c in champs[:SLOTS]]
                    buys = [tk for tk in top if tk not in keep][:max(0, free)]

            for tk in [t for t in list(shares) if t not in keep]:
                sell(tk, i)

            if swap == "swap":
                if buys:
                    each = cash / len(buys)
                    for tk in buys:
                        buy(tk, i, each)
            else:  # rebal — 목표 종목 균등 재조정
                targets = keep + buys
                if targets:
                    held_val = sum(shares.get(t, 0.0) * px(t, i) for t in targets
                                   if np.isfinite(px(t, i)))
                    equity = cash + held_val
                    tgt = equity / len(targets)
                    for tk in targets:                      # 초과분 먼저 매도
                        p = px(tk, i)
                        if not np.isfinite(p):
                            continue
                        cur = shares.get(tk, 0.0) * p
                        if cur > tgt * 1.005:
                            sell(tk, i, frac=min(1.0, (cur - tgt) / cur))
                    for tk in targets:                      # 부족분 매수
                        p = px(tk, i)
                        if not np.isfinite(p):
                            continue
                        cur = shares.get(tk, 0.0) * p
                        if cur < tgt * 0.995:
                            buy(tk, i, min(tgt - cur, cash))

        val = cash + sum(q * px(tk, i) for tk, q in shares.items()
                         if np.isfinite(px(tk, i)))
        equity_dates.append(index[i])
        equity_vals.append(val)
        if val > 0 and shares:
            top_w = max((q * px(tk, i) for tk, q in shares.items()
                         if np.isfinite(px(tk, i))), default=0.0) / val
            maxw_track.append(top_w)

        if i in exec_map:
            rebal_log.append({
                "sig": index[exec_map[i]], "exec": index[i],
                "risk_on": risk_on,
                "sold": sorted(t for t in prev_held if t not in shares),
                "bought": sorted(t for t in shares if t not in prev_held),
                "held": sorted(shares), "cash": cash, "equity": val,
            })

    curve = pd.Series(equity_vals, index=pd.DatetimeIndex(equity_dates))
    m = _metrics(curve, trades, slip_cost, traded_notional, capital)
    if m:
        m["log"] = rebal_log
        m["maxw_mean"] = float(np.mean(maxw_track)) * 100.0 if maxw_track else float("nan")
        m["maxw_peak"] = float(np.max(maxw_track)) * 100.0 if maxw_track else float("nan")
    return m


def _metrics(curve: pd.Series, trades: list, slip_cost: float,
             traded_notional: float, capital: float) -> dict:
    if curve.empty or len(curve) < 5:
        return {}
    final = float(curve.iloc[-1])
    total_ret = (final / capital - 1.0) * 100.0
    years = max(len(curve) / 252.0, 1e-9)
    cagr = ((final / capital) ** (1 / years) - 1.0) * 100.0
    dd = (curve / curve.cummax() - 1.0)
    mdd = float(dd.min()) * 100.0
    rets = curve.pct_change().dropna()
    sharpe = (float(rets.mean()) / float(rets.std()) * np.sqrt(252)
              if len(rets) > 5 and float(rets.std()) > 0 else float("nan"))
    closed = [t for t in trades if np.isfinite(t.get("ret_pct", np.nan))]
    win = (sum(1 for t in closed if t["ret_pct"] > 0) / len(closed) * 100.0) if closed else float("nan")
    avg_eq = float(curve.mean())
    turnover = (traded_notional / 2.0) / avg_eq / years if avg_eq > 0 else float("nan")
    return {"final": final, "total_ret": total_ret, "cagr": cagr, "mdd": mdd,
            "sharpe": sharpe, "trades": len(closed), "win": win,
            "turnover": turnover, "slip": slip_cost,
            "start": curve.index[0], "end": curve.index[-1], "curve": curve}


def buy_hold(tickers: list, adj_df: pd.DataFrame, start_i: int, end_i: int,
             capital: float = CAPITAL) -> dict:
    """벤치마크 — 시작일에 균등 매수 후 만기까지 보유(슬리피지 편도 1회)."""
    usable = [t for t in tickers if t in adj_df.columns
              and pd.notna(adj_df[t].iloc[start_i]) and adj_df[t].iloc[start_i] > 0]
    if not usable:
        return {}
    each = capital / len(usable)
    qty = {t: (each * (1 - SLIPPAGE)) / float(adj_df[t].iloc[start_i]) for t in usable}
    sub = adj_df.iloc[start_i:end_i + 1][usable]
    curve = (sub * pd.Series(qty)).sum(axis=1)
    return _metrics(curve, [], capital * SLIPPAGE, capital, capital)


# ══════════════════════════════════════════════════════════════════════════════
# 출력
# ══════════════════════════════════════════════════════════════════════════════
def _fmt(v, nd=2, suffix=""):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "N/A"
    return f"{v:,.{nd}f}{suffix}"


_LBL = {"weekly": "주간", "monthly": "월간", "swap": "교체분만", "rebal": "균등재조정",
        "top5": "Top5이탈", "top7": "Top7밴드", "none": "필터없음",
        "no_new": "신규중단", "all_cash": "전량현금"}


def print_window_table(win_label: str, results: dict, benches: dict) -> None:
    print(f"\n{'=' * 108}")
    print(f"■ {win_label} 백테스트 — 자본 ${CAPITAL:,.0f} · CAGR 내림차순")
    print("=" * 108)
    print(f"{'주기':<5}{'교체':<11}{'매도룰':<10}{'시장필터':<10}"
          f"{'최종$':>10}{'총수익%':>9}{'CAGR%':>8}{'MDD%':>8}{'샤프':>7}"
          f"{'거래':>6}{'승률%':>7}{'회전x':>7}{'vsSPY':>8}")
    print("-" * 108)
    spy_cagr = benches.get("SPY", {}).get("cagr", float("nan"))
    for cfg, m in sorted(results.items(), key=lambda kv: -(kv[1].get("cagr") or -1e9)):
        if not m:
            continue
        mark = " ◀ 실제운용" if cfg == BASELINE else ""
        vs = m["cagr"] - spy_cagr if np.isfinite(spy_cagr) else float("nan")
        print(f"{_LBL[cfg[0]]:<5}{_LBL[cfg[1]]:<11}{_LBL[cfg[2]]:<10}{_LBL[cfg[3]]:<10}"
              f"{_fmt(m['final'], 0):>10}{_fmt(m['total_ret'], 1):>9}{_fmt(m['cagr'], 1):>8}"
              f"{_fmt(m['mdd'], 1):>8}{_fmt(m['sharpe'], 2):>7}{m['trades']:>6}"
              f"{_fmt(m['win'], 0):>7}{_fmt(m['turnover'], 1):>7}{_fmt(vs, 1, 'pp'):>8}{mark}")
    print("-" * 108)
    for name, m in benches.items():
        if not m:
            continue
        print(f"{'[벤치]':<5}{name:<31}"
              f"{_fmt(m['final'], 0):>10}{_fmt(m['total_ret'], 1):>9}{_fmt(m['cagr'], 1):>8}"
              f"{_fmt(m['mdd'], 1):>8}{_fmt(m['sharpe'], 2):>7}{'-':>6}{'-':>7}{'-':>7}")


def build_segments(idx: pd.DatetimeIndex, end_i: int) -> list:
    """끝에서부터 252봉(12개월) 단위로 자른다. 앞쪽 자투리(<252봉)는 버린다."""
    segs = []
    hi = end_i
    while len(segs) < SEG_MAX:
        lo = hi - SEG_BARS + 1
        if lo < WARMUP_BARS:
            break
        segs.append((lo, hi))
        hi = lo - 1
    return list(reversed(segs))


def run_segments(engine: RankEngine, close_df: pd.DataFrame, adj_df: pd.DataFrame,
                 segs: list) -> list:
    """구간별 기준선 vs SPY vs QQQ — 3년 성과가 어느 해에서 갈렸는지 격리한다."""
    idx = adj_df.index
    out = []
    for lo, hi in segs:
        m = simulate(BASELINE, engine, close_df, adj_df, lo, hi)
        if not m:
            continue
        row = {"lo": lo, "hi": hi, "from": idx[lo], "to": idx[hi], "m": m}
        for b in BENCH_TICKERS:
            row[b] = buy_hold([b], adj_df, lo, hi)
        spy_ret = (row.get("SPY") or {}).get("total_ret", float("nan"))
        row["excess"] = m["total_ret"] - spy_ret
        out.append(row)
    return out


def print_segments(rows: list) -> None:
    if not rows:
        return
    print(f"\n{'=' * 108}")
    print("■ 12개월 구간별 분해 — 기준선(실제 운용 방식) vs 벤치마크")
    print("=" * 108)
    print(f"{'구간':<26}{'기준선%':>9}{'MDD%':>8}{'거래':>6}{'승률%':>7}"
          f"{'SPY%':>9}{'QQQ%':>9}{'vsSPY':>10}")
    print("-" * 108)
    for r in rows:
        m = r["m"]
        spy = (r.get("SPY") or {}).get("total_ret", float("nan"))
        qqq = (r.get("QQQ") or {}).get("total_ret", float("nan"))
        label = f"{r['from'].date()} ~ {r['to'].date()}"
        print(f"{label:<26}{_fmt(m['total_ret'], 1):>9}{_fmt(m['mdd'], 1):>8}"
              f"{m['trades']:>6}{_fmt(m['win'], 0):>7}"
              f"{_fmt(spy, 1):>9}{_fmt(qqq, 1):>9}{_fmt(r['excess'], 1, 'pp'):>10}")
    print("-" * 108)
    worst = min(rows, key=lambda r: r["excess"] if np.isfinite(r["excess"]) else 1e9)
    best = max(rows, key=lambda r: r["excess"] if np.isfinite(r["excess"]) else -1e9)
    print(f"최악 구간: {worst['from'].date()} ~ {worst['to'].date()} "
          f"(SPY 대비 {_fmt(worst['excess'], 1, 'pp')})")
    print(f"최고 구간: {best['from'].date()} ~ {best['to'].date()} "
          f"(SPY 대비 {_fmt(best['excess'], 1, 'pp')})")
    print("→ 구간별 편차가 크면 3년 평균은 '전략의 실력'이 아니라 '구간 운'에 가깝다.")
    return worst


def run_segment_matrix(engine: RankEngine, close_df: pd.DataFrame, adj_df: pd.DataFrame,
                       segs: list) -> dict:
    """구간 × 설정 전체 매트릭스 — 나쁜 구간을 방어한 설정이 무엇인지 격리한다."""
    out = {}
    for freq in FREQS:
        for swap in SWAPS:
            for sr in SELLRULES:
                for mf in MKTFILTERS:
                    cfg = (freq, swap, sr, mf)
                    row = []
                    for lo, hi in segs:
                        try:
                            row.append(simulate(cfg, engine, close_df, adj_df, lo, hi))
                        except Exception:
                            row.append({})
                    out[cfg] = row
    return out


def print_segment_matrix(matrix: dict, seg_rows: list, bad_k: int = 2) -> list:
    """설정별 구간 수익률 표. 최악 구간 성과 순으로 정렬 — 손실 방지 관점의 랭킹."""
    if not matrix or not seg_rows:
        return []
    labels = [f"{r['from'].strftime('%y/%m')}~{r['to'].strftime('%y/%m')}" for r in seg_rows]
    spy_rets = [(r.get("SPY") or {}).get("total_ret", float("nan")) for r in seg_rows]
    # SPY 대비 열위가 큰 구간 = '나쁜 구간'
    order = sorted(range(len(seg_rows)),
                   key=lambda k: seg_rows[k]["excess"] if np.isfinite(seg_rows[k]["excess"])
                   else 1e9)
    bad_idx = order[:bad_k]

    rows = []
    for cfg, ms in matrix.items():
        rets = [(m or {}).get("total_ret", float("nan")) for m in ms]
        trades = sum((m or {}).get("trades", 0) for m in ms)
        valid = [x for x in rets if np.isfinite(x)]
        if not valid:
            continue
        bad = [rets[k] for k in bad_idx if np.isfinite(rets[k])]
        rows.append({
            "cfg": cfg, "rets": rets, "trades": trades,
            "mean": float(np.mean(valid)), "worst": float(np.min(valid)),
            "bad_mean": float(np.mean(bad)) if bad else float("nan"),
        })
    rows.sort(key=lambda r: -r["worst"])

    print(f"\n{'=' * 118}")
    print("■ 구간 × 설정 매트릭스 — 각 12개월 구간 총수익률(%) · 최악 구간 성과 내림차순")
    print("=" * 118)
    hdr = f"{'주기':<5}{'교체':<11}{'매도룰':<10}{'시장필터':<10}"
    for lb in labels:
        hdr += f"{lb:>14}"
    hdr += f"{'평균':>8}{'최악':>8}{'나쁜2평균':>11}{'총거래':>7}"
    print(hdr)
    print("-" * 118)
    for r in rows:
        cfg = r["cfg"]
        line = f"{_LBL[cfg[0]]:<5}{_LBL[cfg[1]]:<11}{_LBL[cfg[2]]:<10}{_LBL[cfg[3]]:<10}"
        for x in r["rets"]:
            line += f"{_fmt(x, 1):>14}"
        line += (f"{_fmt(r['mean'], 1):>8}{_fmt(r['worst'], 1):>8}"
                 f"{_fmt(r['bad_mean'], 1):>11}{r['trades']:>7}")
        if cfg == BASELINE:
            line += "  ◀ 실제운용"
        print(line)
    print("-" * 118)
    line = f"{'[SPY]':<36}"
    for x in spy_rets:
        line += f"{_fmt(x, 1):>14}"
    print(line)
    print(f"→ '나쁜2평균' = SPY 대비 열위가 컸던 구간 {bad_k}개의 평균. "
          f"여기서 살아남는 설정이 진짜 방어력이 있는 설정이다.")
    print("→ 최근 1구간만 좋고 나머지가 나쁜 설정은 '구간 운'이지 실력이 아니다.")
    return rows


def print_reset_vs_continuous(engine: RankEngine, close_df: pd.DataFrame,
                              adj_df: pd.DataFrame, segs: list, matrix: dict,
                              n_years: int = 3) -> None:
    """매년 $5,000 리셋 vs 연속 운용 — 14pp 격차의 원인이 집중도 드리프트인지 확인."""
    use = segs[-n_years:]
    if len(use) < n_years:
        return
    lo, hi = use[0][0], use[-1][1]
    print(f"\n{'=' * 118}")
    print(f"■ 매년 리셋 vs 연속 운용 ({n_years}년) — 경로 차이의 원인 진단")
    print("=" * 118)
    print(f"{'주기':<5}{'교체':<11}{'매도룰':<10}{'시장필터':<10}"
          f"{'연속%':>9}{'리셋체이닝%':>13}{'격차pp':>9}"
          f"{'연속 평균최대비중%':>20}{'연속 최대비중피크%':>20}")
    print("-" * 118)
    targets = [BASELINE,
               ("weekly", "rebal", "top5", "none"),
               ("monthly", "rebal", "top5", "no_new")]
    for cfg in targets:
        cont = simulate(cfg, engine, close_df, adj_df, lo, hi)
        if not cont:
            continue
        seg_ms = matrix.get(cfg) or []
        chain = 1.0
        ok = True
        for (slo, shi) in use:
            k = next((j for j, (a, b) in enumerate(segs) if (a, b) == (slo, shi)), None)
            m = seg_ms[k] if (k is not None and k < len(seg_ms)) else None
            if not m:
                ok = False
                break
            chain *= (1.0 + m["total_ret"] / 100.0)
        if not ok:
            continue
        chain_ret = (chain - 1.0) * 100.0
        print(f"{_LBL[cfg[0]]:<5}{_LBL[cfg[1]]:<11}{_LBL[cfg[2]]:<10}{_LBL[cfg[3]]:<10}"
              f"{_fmt(cont['total_ret'], 1):>9}{_fmt(chain_ret, 1):>13}"
              f"{_fmt(chain_ret - cont['total_ret'], 1):>9}"
              f"{_fmt(cont.get('maxw_mean'), 1):>20}{_fmt(cont.get('maxw_peak'), 1):>20}")
    print("-" * 118)
    print("→ '교체분만' 은 승자를 계속 태우므로 시간이 갈수록 한 종목 비중이 커진다.")
    print("   평균/피크 최대비중이 20%(=1/5 균등)를 크게 넘으면 격차의 원인은 집중도 드리프트다.")
    print("   '균등재조정' 의 격차가 훨씬 작다면 그 해석이 맞다는 확증이다.")


def print_rebalance_log(m: dict, last_n: int | None = 12, title: str | None = None) -> None:
    """리밸런싱 내역 — last_n=None 이면 전체 출력."""
    log = (m or {}).get("log") or []
    if not log:
        return
    shown = log if last_n is None else log[-last_n:]
    head = title or f"기준선 최근 {len(shown)}회 리밸런싱 (네 이메일과 대조 검증용)"
    print(f"\n▶ {head}")
    print(f"   {'신호일':<12}{'체결일':<12}{'매도':<28}{'매수':<28}{'보유 Top5':<30}{'자산$':>10}")
    for r in shown:
        print(f"   {str(r['sig'].date()):<12}{str(r['exec'].date()):<12}"
              f"{(','.join(r['sold']) or '-'):<28}{(','.join(r['bought']) or '-'):<28}"
              f"{(','.join(r['held']) or '(전량현금)'):<30}{r['equity']:>10,.0f}")


def print_factor_summary(win_label: str, results: dict) -> None:
    """기준선(실제 운용) 대비 한 축씩만 바꿨을 때의 차이 — 인과 해석이 가능한 비교."""
    base = results.get(BASELINE)
    if not base:
        return
    print(f"\n▶ {win_label} · 기준선(주간·교체분만·Top5이탈·필터없음) 대비 1축 변경 효과")
    axes = [(0, FREQS, "리밸런싱 주기"), (1, SWAPS, "교체 방식"),
            (2, SELLRULES, "매도 룰"), (3, MKTFILTERS, "시장 필터")]
    for pos, opts, title in axes:
        lines = []
        for o in opts:
            if o == BASELINE[pos]:
                continue
            cfg = tuple(o if k == pos else BASELINE[k] for k in range(4))
            m = results.get(cfg)
            if not m:
                continue
            lines.append(f"    {_LBL[o]:<10} CAGR {_fmt(m['cagr'], 1):>7}% "
                         f"({_fmt(m['cagr'] - base['cagr'], 1, 'pp'):>8}) · "
                         f"MDD {_fmt(m['mdd'], 1):>7}% ({_fmt(m['mdd'] - base['mdd'], 1, 'pp'):>8}) · "
                         f"거래 {m['trades']:>3}건")
        if lines:
            print(f"  · {title}")
            print("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# Google Sheets 기록
# ══════════════════════════════════════════════════════════════════════════════
_GS_MAX_ATTEMPTS = 6
_GS_BACKOFF = (2, 4, 8, 16, 32, 60)
_GS_RETRY_STATUS = {429, 500, 502, 503, 504}


def _gs_is_transient(exc) -> bool:
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
        try:
            a0 = exc.args[0]
            if isinstance(a0, dict):
                code = int((a0.get("error") or {}).get("code") or a0.get("code"))
        except Exception:
            code = None
    return code is not None and int(code) in _GS_RETRY_STATUS


def _gs(fn, *args, **kwargs):
    import gspread
    last = None
    for attempt in range(_GS_MAX_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except (gspread.exceptions.SpreadsheetNotFound,
                gspread.exceptions.WorksheetNotFound):
            raise
        except Exception as exc:  # noqa: BLE001
            if not _gs_is_transient(exc) or attempt == _GS_MAX_ATTEMPTS - 1:
                raise
            last = exc
            wait = _GS_BACKOFF[min(attempt, len(_GS_BACKOFF) - 1)]
            wait += random.uniform(0, wait * 0.25)
            print(f"[WARN] Sheets 일시 오류 — {wait:.1f}초 후 재시도: {exc}", flush=True)
            time.sleep(wait)
    raise last


def _safe_append_rows(ws, rows, ncols: int) -> None:
    """append_row 계단식 드리프트 회피 — A열 기준 마지막 다음 행에 명시 range update."""
    if not rows:
        return
    rows = [list(r) for r in rows if r is not None]
    existing = _gs(ws.get_all_values) or []
    last_row = 0
    for idx, r in enumerate(existing, start=1):
        if any(str(c).strip() != "" for c in r):
            last_row = idx
    start_row, end_row = last_row + 1, last_row + len(rows)
    try:
        if end_row > ws.row_count:
            _gs(ws.add_rows, end_row - ws.row_count + 50)
    except Exception:
        pass
    last_col = chr(ord("A") + max(0, ncols - 1))
    _gs(ws.update, rows, range_name=f"A{start_row}:{last_col}{end_row}",
        value_input_option="USER_ENTERED")


def write_results(all_rows: list) -> None:
    if not GSPREAD_KEY_JSON:
        print("\n[INFO] GSPREAD_KEY 없음 — 시트 기록 생략(콘솔 출력만).")
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GSPREAD_KEY_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"])
        gc = gspread.authorize(creds)
        sh = _gs(gc.open, _SPREADSHEET_TITLE)
        titles = [w.title for w in _gs(sh.worksheets)]
        last_col = chr(ord("A") + len(_RESULT_COLS) - 1)
        if _RESULT_WORKSHEET in titles:
            ws = _gs(sh.worksheet, _RESULT_WORKSHEET)
            if (_gs(ws.row_values, 1) or []) != _RESULT_COLS:
                _gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1",
                    value_input_option="USER_ENTERED")
                print(f"[INFO] {_RESULT_WORKSHEET} 헤더 갱신")
        else:
            ws = _gs(sh.add_worksheet, title=_RESULT_WORKSHEET,
                     rows=2000, cols=len(_RESULT_COLS))
            _gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1",
                value_input_option="USER_ENTERED")
        _safe_append_rows(ws, all_rows, ncols=len(_RESULT_COLS))
        print(f"[OK] {_RESULT_WORKSHEET} 시트에 {len(all_rows)}행 기록")
    except Exception as exc:
        print(f"[WARN] 시트 기록 실패(콘솔 결과는 유효): {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# 자체검증 (네트워크 불필요)
# ══════════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    print("=" * 70)
    print("자체검증 — 합성 데이터로 엔진 기계적 정합성 확인")
    print("=" * 70)
    fails = []

    # 1) trailing return
    v = np.array([100.0] * 30 + [110.0])
    r = _trailing_return(v, 21)
    if abs(r - 10.0) > 1e-9:
        fails.append(f"trailing_return 오차: {r}")

    # 2) 합성 패널 — 한 종목만 계속 상승, 나머지는 평탄
    n = 400
    idx = pd.bdate_range("2023-01-02", periods=n)
    pool = fx.satellite_candidate_pool()
    tickers = sorted({t for lst in pool.values() for t in lst} | {"SPY", "QQQ"})
    close = pd.DataFrame(index=idx)
    for tk in tickers:
        close[tk] = 100.0
    winner = "XLK"
    close[winner] = 100.0 * (1.0005 ** np.arange(n))     # 꾸준한 우상향
    close["SPY"] = 100.0 * (1.0002 ** np.arange(n))
    adj = close.copy()

    eng = RankEngine(close)
    champs = eng.rank_at(idx[-1])
    if not champs or champs[0]["ticker"] != winner:
        fails.append(f"랭킹 1위가 {winner} 가 아님: {[c['ticker'] for c in champs[:3]]}")

    # 3) 섹터당 1개 제약
    secs = [c["sector"] for c in champs]
    if len(secs) != len(set(secs)):
        fails.append("같은 섹터가 2개 이상 랭킹에 존재")

    # 4) 무비용 가정 시 시뮬레이터 ≈ 매수후보유
    global SLIPPAGE
    old_slip = SLIPPAGE
    SLIPPAGE = 0.0
    try:
        m = simulate(BASELINE, eng, close, adj, start_i=200, end_i=n - 1)
        if not m:
            fails.append("시뮬레이터가 결과를 내지 못함")
        else:
            # 평탄 종목이 대부분이라 최종 자산은 자본금 이상이어야 하고 폭발하면 안 됨
            if not (CAPITAL * 0.95 <= m["final"] <= CAPITAL * 1.5):
                fails.append(f"최종 자산 비정상: {m['final']:.2f}")
            if m["mdd"] > 0.01:
                fails.append(f"MDD 부호 이상: {m['mdd']}")
        bh = buy_hold(["SPY"], adj, 200, n - 1)
        expect = CAPITAL * float(adj["SPY"].iloc[n - 1] / adj["SPY"].iloc[200])
        if not bh or abs(bh["final"] - expect) > 1.0:
            fails.append(f"buy_hold 검증 실패: {bh.get('final')} vs {expect:.2f}")
    finally:
        SLIPPAGE = old_slip

    # 5) 슬리피지가 성과를 낮추는 방향인지
    SLIPPAGE_TEST = 0.01
    old = SLIPPAGE
    m_free = simulate(BASELINE, eng, close, adj, 200, n - 1)
    SLIPPAGE = SLIPPAGE_TEST
    m_cost = simulate(BASELINE, eng, close, adj, 200, n - 1)
    SLIPPAGE = old
    if m_free and m_cost and m_cost["final"] > m_free["final"]:
        fails.append("슬리피지를 키웠는데 성과가 좋아짐")

    # 6) 신호일/체결일 분리 확인
    sd = signal_dates(idx, "weekly")
    if any(pd.Timestamp(d).weekday() != 4 for d in sd[1:-1]):
        fails.append("주간 신호일이 금요일이 아님")

    # 7) 12개월 구간 분해 — 겹치지 않고 워밍업 침범 없이 끝에서부터 잘리는가
    segs = build_segments(idx, n - 1)
    if not segs:
        fails.append("구간 분해 결과가 비어 있음")
    else:
        if segs[-1][1] != n - 1:
            fails.append("마지막 구간이 데이터 끝에 붙어 있지 않음")
        if any(hi - lo + 1 != SEG_BARS for lo, hi in segs):
            fails.append("구간 길이가 252봉이 아님")
        if any(segs[k][1] >= segs[k + 1][0] for k in range(len(segs) - 1)):
            fails.append("구간이 서로 겹침")
        if segs[0][0] < WARMUP_BARS:
            fails.append("구간이 워밍업 영역을 침범함")

    # 8) 수수료를 키우면 성과가 나빠지는가
    global COMMISSION_PER_TRADE
    old_c = COMMISSION_PER_TRADE
    m_free2 = simulate(BASELINE, eng, close, adj, 200, n - 1)
    COMMISSION_PER_TRADE = 20.0
    m_fee = simulate(BASELINE, eng, close, adj, 200, n - 1)
    COMMISSION_PER_TRADE = old_c
    if m_free2 and m_fee and m_fee["final"] >= m_free2["final"]:
        fails.append("건당 수수료를 $20 로 올렸는데 성과가 나빠지지 않음")

    # 9) 배당조정 패널이 성과에 반영되는가 (adj 를 1% 더 올리면 최종자산도 올라야)
    adj_up = adj * 1.0
    adj_up.iloc[-1] = adj_up.iloc[-1] * 1.01
    m_up = simulate(BASELINE, eng, close, adj_up, 200, n - 1)
    if m_up and m_free2 and m_up["final"] <= m_free2["final"]:
        fails.append("성과 패널(adj)이 최종 자산에 반영되지 않음")

    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 전 항목 통과 (수익률·섹터제약·무비용정합·슬리피지방향·신호일·"
          "구간분해·수수료방향·배당패널반영)")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════
def main() -> None:
    t0 = time.time()
    print("=" * 108)
    print(f"🛰️  위성 섹터 로테이션 백테스트 — {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 108)

    pool = fx.satellite_candidate_pool()
    universe = sorted({t for lst in pool.values() for t in lst})
    fetch_list = sorted(set(universe) | set(BENCH_TICKERS))
    print(f"[STEP 1] 후보 풀 {len(universe)}개 + 벤치 {len(BENCH_TICKERS)}개 = "
          f"{len(fetch_list)}종목 × 2엔드포인트(원종가·배당조정) 수집 중...")
    raw, adjmap, fallback = _batch_fetch(fetch_list)
    print(f"[INFO] 확보 {len(raw)}/{len(fetch_list)}종목")
    missing = sorted(set(fetch_list) - set(raw))
    if missing:
        print(f"[WARN] 이력 미확보: {missing}")
    if fallback:
        print(f"[WARN] 배당조정 실패 → 원 종가 폴백 {len(fallback)}종목: {fallback[:15]}")
    if "SPY" not in raw:
        print("[ERROR] SPY 이력 확보 실패 — 중단")
        sys.exit(1)

    close_df, adj_df = build_panels(raw, adjmap)
    idx = close_df.index
    print(f"[INFO] 캘린더 {len(idx)}봉 · {idx[0].date()} ~ {idx[-1].date()}")

    # 배당 반영이 실제로 작동했는지 검증 — 총수익/가격수익 괴리를 연율로 환산
    gaps = []
    for tk in sorted(raw):
        c, a = close_df[tk].dropna(), adj_df[tk].dropna()
        if len(c) < 260 or len(a) < 260:
            continue
        n = min(len(c), len(a))
        cr = float(c.iloc[-1] / c.iloc[-n])
        ar = float(a.iloc[-1] / a.iloc[-n])
        if cr > 0 and ar > 0:
            gaps.append((ar / cr) ** (252.0 / n) - 1.0)
    med_gap = float(np.median(gaps)) * 100.0 if gaps else 0.0
    div_basis = "dividend-adjusted(배당재투자)" if med_gap > 0.15 else "close(배당미반영)"
    print(f"[INFO] 성과 기준: {div_basis} · 배당 기여 중앙값 ≈ 연 {med_gap:.2f}%p")
    if med_gap <= 0.15:
        print("[WARN] 배당조정 시리즈가 원 종가와 사실상 동일 — 배당이 반영되지 않았다.")
    spy_gap = 0.0
    try:
        c, a = close_df["SPY"].dropna(), adj_df["SPY"].dropna()
        n = min(len(c), len(a))
        spy_gap = (((float(a.iloc[-1] / a.iloc[-n])) / (float(c.iloc[-1] / c.iloc[-n])))
                   ** (252.0 / n) - 1.0) * 100.0
        print(f"[INFO] 참고 — SPY 배당 기여 연 {spy_gap:.2f}%p "
              f"(위성 풀 중앙값 {med_gap:.2f}%p 와의 차이가 상대비교에 미치는 영향)")
    except Exception:
        pass

    max_eval = len(idx) - WARMUP_BARS - ENTRY_LAG_DAYS
    print(f"[INFO] 워밍업 {WARMUP_BARS}봉 제외 후 평가 가능 최대 {max_eval}거래일 "
          f"(≈{max_eval / 252:.1f}년)")

    engine = RankEngine(close_df)
    end_i = len(idx) - 1
    all_rows = []
    run_date = datetime.now(_ET).strftime("%Y-%m-%d")

    for win_label, win_bars in WINDOWS.items():
        start_i = end_i - win_bars + 1
        if start_i < WARMUP_BARS:
            print(f"\n[SKIP] {win_label} — 이력 부족(필요 {win_bars + WARMUP_BARS}봉 / "
                  f"보유 {len(idx)}봉)")
            continue
        print(f"\n[STEP 2] {win_label} 시뮬레이션 중... "
              f"({idx[start_i].date()} ~ {idx[end_i].date()})")

        results = {}
        for freq in FREQS:
            for swap in SWAPS:
                for sr in SELLRULES:
                    for mf in MKTFILTERS:
                        cfg = (freq, swap, sr, mf)
                        try:
                            results[cfg] = simulate(cfg, engine, close_df, adj_df,
                                                    start_i, end_i)
                        except Exception as exc:
                            print(f"[WARN] {cfg} 실패: {exc}")
                            results[cfg] = {}

        benches = {}
        for b in BENCH_TICKERS:
            benches[b] = buy_hold([b], adj_df, start_i, end_i)
        # 초기 Top5 고정 보유 — 로테이션이 값을 더했는지의 직답
        try:
            first_sig = [d for d in signal_dates(idx, "weekly")
                         if start_i <= idx.get_loc(d) <= end_i][0]
            top5_0 = [c["ticker"] for c in engine.rank_at(first_sig)[:SLOTS]]
            benches[f"초기Top5 고정({','.join(top5_0)})"] = buy_hold(
                top5_0, adj_df, idx.get_loc(first_sig) + ENTRY_LAG_DAYS, end_i)
        except Exception as exc:
            print(f"[WARN] 초기 Top5 벤치 실패: {exc}")

        print_window_table(win_label, results, benches)
        print_factor_summary(win_label, results)
        if win_label == list(WINDOWS)[-1]:
            print_rebalance_log(results.get(BASELINE))

        spy_cagr = benches.get("SPY", {}).get("cagr", float("nan"))
        for cfg, m in results.items():
            if not m:
                continue
            all_rows.append([
                run_date, win_label, _LBL[cfg[0]], _LBL[cfg[1]], _LBL[cfg[2]], _LBL[cfg[3]],
                str(m["start"].date()), str(m["end"].date()),
                round(CAPITAL, 2), round(m["final"], 2), round(m["total_ret"], 2),
                round(m["cagr"], 2), round(m["mdd"], 2),
                round(m["sharpe"], 3) if np.isfinite(m["sharpe"]) else "",
                m["trades"], round(m["win"], 1) if np.isfinite(m["win"]) else "",
                round(m["turnover"], 2), round(m["slip"], 2),
                round(m["cagr"] - spy_cagr, 2) if np.isfinite(spy_cagr) else "",
                div_basis,
            ])

    # ── STEP 3: 12개월 구간별 분해 — 3년 성과가 어느 해에서 갈렸는지 격리 ──────
    segs = build_segments(idx, end_i)
    if segs:
        print(f"\n[STEP 3] 12개월 구간 {len(segs)}개 분해 중...")
        seg_rows = run_segments(engine, close_df, adj_df, segs)
        worst = print_segments(seg_rows)
        if worst:
            print_rebalance_log(
                worst["m"], last_n=None,
                title=f"최악 구간 전체 리밸런싱 내역 "
                      f"({worst['from'].date()} ~ {worst['to'].date()}) — 실패 모드 진단")
        for r in seg_rows:
            m = r["m"]
            spy_c = (r.get("SPY") or {}).get("cagr", float("nan"))
            all_rows.append([
                run_date, f"구간 {r['from'].date()}~{r['to'].date()}",
                _LBL[BASELINE[0]], _LBL[BASELINE[1]], _LBL[BASELINE[2]], _LBL[BASELINE[3]],
                str(m["start"].date()), str(m["end"].date()),
                round(CAPITAL, 2), round(m["final"], 2), round(m["total_ret"], 2),
                round(m["cagr"], 2), round(m["mdd"], 2),
                round(m["sharpe"], 3) if np.isfinite(m["sharpe"]) else "",
                m["trades"], round(m["win"], 1) if np.isfinite(m["win"]) else "",
                round(m["turnover"], 2), round(m["slip"], 2),
                round(m["cagr"] - spy_c, 2) if np.isfinite(spy_c) else "",
                div_basis,
            ])

        # ── STEP 4: 구간 × 설정 매트릭스 ────────────────────────────────────
        print(f"\n[STEP 4] 구간 {len(segs)}개 × 설정 24개 매트릭스 계산 중...")
        matrix = run_segment_matrix(engine, close_df, adj_df, segs)
        mrows = print_segment_matrix(matrix, seg_rows)
        print_reset_vs_continuous(engine, close_df, adj_df, segs, matrix)

        for r in (mrows or []):
            cfg = r["cfg"]
            for (lo, hi), m in zip(segs, matrix[cfg]):
                if not m:
                    continue
                all_rows.append([
                    run_date, f"매트릭스 {idx[lo].date()}~{idx[hi].date()}",
                    _LBL[cfg[0]], _LBL[cfg[1]], _LBL[cfg[2]], _LBL[cfg[3]],
                    str(m["start"].date()), str(m["end"].date()),
                    round(CAPITAL, 2), round(m["final"], 2), round(m["total_ret"], 2),
                    round(m["cagr"], 2), round(m["mdd"], 2),
                    round(m["sharpe"], 3) if np.isfinite(m["sharpe"]) else "",
                    m["trades"], round(m["win"], 1) if np.isfinite(m["win"]) else "",
                    round(m["turnover"], 2), round(m["slip"], 2), "", div_basis,
                ])

    write_results(all_rows)

    print("\n" + "=" * 108)
    print("⚠️  해석 주의 — 이 숫자는 상한선이다")
    print("   1) 후보 풀 56개는 2026년 현재 시점에 고른 목록 → 미래를 아는 풀(선택 편향).")
    print("   2) FMP 이력 5년 한도로 2020 코로나·2022 초입 하락장이 데이터에 없다.")
    print("   3) 이 룰의 실거래 기록은 아직 없다 — 리밸런싱 로그가 과거 실계좌와 겹쳐 보여도")
    print("      그건 검증이 아니다. 진짜 대조는 앞으로 쌓일 주차 기록으로만 가능하다.")
    print("   → 절대 수익률이 아니라 '조건 간 상대 비교'만 신뢰할 것.")
    print(f"[DONE] {time.time() - t0:.0f}초 소요")
    print("=" * 108)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
