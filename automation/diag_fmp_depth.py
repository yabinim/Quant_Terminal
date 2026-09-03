#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FMP historical-price-eod/full 실제 이력 깊이 진단 (18A).

목적
────
run_signal_backtest v2.4 에서 TEST_LOOKBACK 을 1260→2140 으로 늘렸는데
평가 구간이 2022-06-10 그대로였고 유니버스가 227→159 로 줄었다.
가설: FMP 가 limit 을 올려도 그만큼의 봉을 주지 않는다(계정 플랜의 이력 한도).

이 스크립트는 아무것도 수정하지 않는다. 읽기 전용 진단이다.
  1) 유니버스 표본에 대해 실제 확보 봉수 분포를 집계
  2) 위 결과로 '실현 가능한 TEST_LOOKBACK' 을 역산해 출력

⚠️ v2.9 계약 (2026-09-03) — probe_limits 폐기
────────────────────────────────────────────
원래 [1]번은 `limit` 을 500·2000·5000 으로 바꿔가며 응답 깊이를 재는 프로브였다.
그 질문의 답은 확정됐다: **FMP 는 limit 을 조용히 무시하고 항상 ~1,255봉을
돌려준다.** 인수인계 §7 에 '재조사 금지'로 박혀 있다.

run_signal_backtest v2.9 가 `limit` 송신을 중단하고 `from`/`to` 창으로 넘어가면서
이 프로브는 **돌릴 수 없게 됐다** — 흔들 손잡이가 사라졌다. 달력일 창을 흔드는
프로브는 diag_fmp_window.py 가 이미 갖고 있으므로 여기서 되살리지 않는다.

남긴 것은 [1]·[2] 뿐이다. '유니버스가 실제로 몇 봉을 확보하는가' 는 여전히
살아 있는 질문이다 — 신규 상장·이력 짧은 종목은 5년 한도와 무관하게 얕다.

⚠️ v2.8 계약 (2026-08-26)
────────────────────────
`run_signal_backtest._fmp_price_history` 는 v2.8 부터 `(DataFrame, kind)` 를
돌려준다. 이 파일은 그 함수를 빌려 쓰는데 v2.8 배포 때 **호출부가 갱신되지
않았다.** 결과는 두 가지였다:

  probe_limits  : tuple.empty → AttributeError → 즉시 크래시
  probe_universe: len(tuple) == 2 → **예외 없이 전 종목이 "2봉"으로 집계**
                  → recommend() 가 그 2봉으로 분위수를 내고 음수 LOOKBACK 권고

두 번째가 더 위험하다. 이력 깊이를 재는 것이 이 스크립트의 존재 이유인데,
그 숫자가 조용히 틀렸다. 이제 kind 를 받아 사유별로 집계한다.

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


def probe_universe(tickers: list, *, bars: int) -> pd.Series:
    # v2.9: 인자가 limit → bars 다. 단위는 예전부터 봉수였고 이름만 틀렸었다.
    #       실제 조회 창은 _fmp_price_history 안에서 fmp_extras 가 환산한다.
    _days = bt.fx.hist_days_for_bars(bars)
    print(f"\n[1] 유니버스 표본 {len(tickers)}종목 실제 봉수 "
          f"(요구 {bars}봉 → 조회창 {_days}달력일)")
    counts = {}
    reasons: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=bt._FETCH_WORKERS) as ex:
        # ⚠️ keyword 로 넘긴다. v2.9 는 keyword-only 라 위치 인자는 TypeError 다.
        futs = {ex.submit(bt._fmp_price_history, tk, bars=bars): tk
                for tk in tickers}
        for fut in concurrent.futures.as_completed(futs):
            tk = futs[fut]
            try:
                # ⚠️ 반드시 2개로 언팩한다. 단일 이름으로 받으면 tuple 이 들어와
                #    len() 이 항상 2 가 되고, 예외가 안 나서 발견되지 않는다.
                df, kind = fut.result()
                counts[tk] = len(df) if df is not None else 0
            except Exception:
                kind = "exception"
                counts[tk] = 0
            reasons[kind] = reasons.get(kind, 0) + 1
    s = pd.Series(counts).sort_values()
    ok = s[s > 0]
    print(f"   응답 성공 {len(ok)}/{len(tickers)}  ·  빈 응답 {int((s == 0).sum())}")
    if reasons:
        print("   사유별 — " + " · ".join(
            f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])))
    print("   " + bt.fh.fmp_stats_line())
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
        print("\n[2] 유효 응답이 없어 권고를 낼 수 없습니다.")
        return
    print(f"\n[2] 실현 가능한 TEST_LOOKBACK 역산")
    print(f"    (TEST_LOOKBACK = 실제봉수 − MIN_PRIOR_BARS({MIN_PRIOR}) − 꼬리({TAIL_PAD}))")
    print(f"{'유니버스 유지율':>16}{'필요 봉수':>10}{'가능 LOOKBACK':>14}{'평가기간':>10}")
    for frac in (0.95, 0.90, 0.80, 0.70, 0.50):
        need = ok.quantile(1 - frac)
        lb = int(need - MIN_PRIOR - TAIL_PAD)
        print(f"{int(frac * 100):>15}%{need:>10.0f}{lb:>14}"
              f"{max(lb, 0) / 252:>9.1f}년")
    print(f"\n    현재 설정: TEST_LOOKBACK={bt.TEST_LOOKBACK} "
          f"(HISTORY_BARS={bt.HISTORY_BARS}, {bt.HISTORY_BARS / 252:.1f}년 요구)")
    n_ok = int((ok >= bt.HISTORY_BARS * 0.95).sum())
    print(f"    현재 요구를 충족하는 종목: {n_ok}/{len(ok)} "
          f"({n_ok / len(ok) * 100:.0f}%)")


def main() -> int:
    if not bt.FMP_API_KEY:
        print("[ERR] FMP_API_KEY 없음")
        return 1

    # 부분 롤백 상태에서 스택 트레이스 대신 원인을 말하게 한다.
    if not hasattr(bt, "fh"):
        print("[ERR] run_signal_backtest 가 v2.8 이전 버전이다 "
              "(fmp_http 미도입) — 두 파일을 함께 배포할 것")
        return 1
    if not hasattr(bt, "HISTORY_BARS"):
        print("[ERR] run_signal_backtest 가 v2.9 이전 버전이다 "
              "(HISTORY_LIMIT → HISTORY_BARS 개명 전) — 두 파일을 함께 배포할 것")
        return 1
    if not hasattr(bt, "fx"):
        print("[ERR] run_signal_backtest 에 fmp_extras 가 없다 "
              "(v2.9 창 환산 SSOT 미도입) — 두 파일을 함께 배포할 것")
        return 1

    try:
        gc = bt.get_gspread_client()
        universe, segment_map = bt.load_universe(gc)
    except Exception as e:
        print(f"\n[WARN] 유니버스 로드 실패({e}) — 고정 표본으로 대체")
        universe = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "SPY", "QQQ", "SCHD"]
        segment_map = {}

    stocks = [t for t in universe if segment_map.get(t) != "etf"]
    sample = (stocks or universe)[:60]
    # v2.9: 예전엔 max(HISTORY_BARS, 3000) 으로 '넉넉히' 요청했다. 이제 그 여유는
    #       무의미하다 — hist_days_for_bars 가 HIST_MAX_DAYS(1826일)에서 클램프해
    #       1255봉이든 3000봉이든 같은 창이 나간다. 실제 설정값으로 잰다.
    bars = probe_universe(sample, bars=bt.HISTORY_BARS)
    recommend(bars)

    print("\n[참고] 개별주 유니버스 전체:", len(stocks), "종목 · 표본:", len(sample))
    return 0


if __name__ == "__main__":
    sys.exit(main())
