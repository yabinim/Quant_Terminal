#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FMP historical-price-eod/full 실제 이력 깊이 진단 (18A).

목적
────
run_signal_backtest v2.4 에서 TEST_LOOKBACK 을 1260→2140 으로 늘렸는데
평가 구간이 2022-06-10 그대로였고 유니버스가 227→159 로 줄었다.
가설: FMP 가 limit 을 올려도 그만큼의 봉을 주지 않는다(계정 플랜의 이력 한도).

이 스크립트는 아무것도 수정하지 않는다. 읽기 전용 진단이다.
  1) limit 값을 바꿔가며 SPY 가 실제로 몇 봉/어느 날짜부터 오는지 확인
  2) 유니버스 표본에 대해 실제 확보 봉수 분포를 집계
  3) 위 결과로 '실현 가능한 TEST_LOOKBACK' 을 역산해 출력

실행:  python automation/diag_fmp_depth.py
"""
from __future__ import annotations

import concurrent.futures
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_signal_backtest as bt  # noqa: E402

MIN_PRIOR = bt.MIN_PRIOR_BARS          # 220 (200일선 산출용 선행 봉)
TAIL_PAD = max(bt.HORIZONS) + bt.ENTRY_LAG_DAYS + 40


def probe_limits(ticker: str, limits) -> None:
    print(f"\n[1] limit 별 실제 응답 — {ticker}")
    print(f"{'요청 limit':>10}{'실제 봉수':>10}{'시작일':>14}{'종료일':>14}{'연수':>7}")
    prev = None
    for lim in limits:
        df = bt._fmp_price_history(ticker, limit=lim)
        if df.empty:
            print(f"{lim:>10}{'실패':>10}")
            continue
        n = len(df)
        print(f"{lim:>10}{n:>10}{str(df.index[0].date()):>14}"
              f"{str(df.index[-1].date()):>14}{n / 252:>7.1f}")
        if prev is not None and n == prev:
            print(f"{'':>10}  ↑ 이전 limit 과 봉수 동일 → 여기서 한도에 걸렸을 가능성")
        prev = n


def probe_universe(tickers: list, limit: int) -> pd.Series:
    print(f"\n[2] 유니버스 표본 {len(tickers)}종목 실제 봉수 (limit={limit})")
    counts = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=bt._FETCH_WORKERS) as ex:
        futs = {ex.submit(bt._fmp_price_history, tk, limit): tk for tk in tickers}
        for fut in concurrent.futures.as_completed(futs):
            tk = futs[fut]
            try:
                df = fut.result()
                counts[tk] = len(df) if df is not None else 0
            except Exception:
                counts[tk] = 0
    s = pd.Series(counts).sort_values()
    ok = s[s > 0]
    print(f"   응답 성공 {len(ok)}/{len(tickers)}  ·  빈 응답 {int((s == 0).sum())}")
    if ok.empty:
        return s
    for q in (0.05, 0.25, 0.50, 0.75, 0.95):
        v = ok.quantile(q)
        print(f"   {int(q * 100):>3}% 분위: {v:>7.0f}봉 ({v / 252:.1f}년)")
    print(f"   최소 {ok.min():.0f} · 최대 {ok.max():.0f}")
    return s


def recommend(bars: pd.Series, keep_frac: float = 0.80) -> None:
    ok = bars[bars > 0]
    if ok.empty:
        print("\n[3] 유효 응답이 없어 권고를 낼 수 없습니다.")
        return
    print(f"\n[3] 실현 가능한 TEST_LOOKBACK 역산")
    print(f"    (TEST_LOOKBACK = 실제봉수 − MIN_PRIOR_BARS({MIN_PRIOR}) − 꼬리({TAIL_PAD}))")
    print(f"{'유니버스 유지율':>16}{'필요 봉수':>10}{'가능 LOOKBACK':>14}{'평가기간':>10}")
    for frac in (0.95, 0.90, 0.80, 0.70, 0.50):
        need = ok.quantile(1 - frac)
        lb = int(need - MIN_PRIOR - TAIL_PAD)
        print(f"{int(frac * 100):>15}%{need:>10.0f}{lb:>14}"
              f"{max(lb, 0) / 252:>9.1f}년")
    print(f"\n    현재 설정: TEST_LOOKBACK={bt.TEST_LOOKBACK} "
          f"(HISTORY_LIMIT={bt.HISTORY_LIMIT}, {bt.HISTORY_LIMIT / 252:.1f}년 요구)")
    n_ok = int((ok >= bt.HISTORY_LIMIT * 0.95).sum())
    print(f"    현재 요구를 충족하는 종목: {n_ok}/{len(ok)} "
          f"({n_ok / len(ok) * 100:.0f}%)")


def main() -> int:
    if not bt.FMP_API_KEY:
        print("[ERR] FMP_API_KEY 없음")
        return 1

    probe_limits("SPY", [500, 1301, 2000, 2461, 3000, 5000])

    try:
        gc = bt.get_gspread_client()
        universe, segment_map = bt.load_universe(gc)
    except Exception as e:
        print(f"\n[WARN] 유니버스 로드 실패({e}) — 고정 표본으로 대체")
        universe = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "SPY", "QQQ", "SCHD"]
        segment_map = {}

    stocks = [t for t in universe if segment_map.get(t) != "etf"]
    sample = (stocks or universe)[:60]
    bars = probe_universe(sample, limit=max(bt.HISTORY_LIMIT, 3000))
    recommend(bars)

    print("\n[참고] 개별주 유니버스 전체:", len(stocks), "종목 · 표본:", len(sample))
    return 0


if __name__ == "__main__":
    sys.exit(main())
