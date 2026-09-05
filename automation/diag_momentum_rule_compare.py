#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_momentum_rule_compare.py — 랭킹 룰 × 집중도 비교 (읽기 전용 진단)

목적
────
HSA 위성 슬리브의 두 가지 열린 질문에만 답한다.

  Q1. 랭킹 룰 — 현행 blend(1M40/3M40/6M20) vs 학술 12-1 vs 12-0
  Q2. 집중도  — Top5 균등(현행) vs Top3 vs 차등 가중

엔진은 `diag_satellite_backtest` 를 그대로 쓴다(재구현 금지). 이 파일이 추가하는
것은 **축과 판정 규칙**뿐이다. 점수식은 `fmp_extras.MOM_RULES` 가 유일한 출처다.

  실행: python automation/diag_momentum_rule_compare.py
        python automation/diag_momentum_rule_compare.py --selftest   # 네트워크 불필요

아무것도 수정하지 않는다. Google Sheets `Momentum_Rule_Compare` 탭에 결과 행만
append 한다. 기존 `Satellite_Backtest` 탭과 그 24설정 그리드는 **건드리지 않는다.**


═══════════════════════════════════════════════════════════════════════════════
사전 커밋 판정 기준 — 2026-09-05 확정. 결과를 본 뒤 재협상하지 않는다.
═══════════════════════════════════════════════════════════════════════════════
왜 코드 안에 적어두나: 산업 모멘텀(item B) 때 배운 것이다. 임계값이 대화에만
있으면 결과를 본 뒤 "이번엔 창이 짧으니까" 로 흔들린다. 파일에 박아두면 흔들 때
diff 가 남는다.

  T1  mom12_1 이 blend 를 대체 —— (mom12_1, top5_eq) 가 (blend, top5_eq) 를
      **6창 중 4창 이상**에서 샤프로 이길 것.
  T2  mom12_0 이 blend 를 대체 —— 위와 동일 조건, 짝만 mom12_0.
  T3  Top3 가 Top5 를 대체 —— **6창 중 5창 이상** 샤프 우위 **그리고**
      6창 평균 MDD 가 악화되지 않을 것(≤ +0.0%p).
  T4  차등 가중이 균등을 대체 —— **6창 중 5창 이상** 샤프 우위를
      **세 룰(blend·mom12_1·mom12_0) 전부에서 같은 방향으로** 만족할 것.
  T5  Top1 몰빵은 **사전 배제**. 출력은 하되 어떤 판정에도 쓰지 않는다.
      근거 셋: (a) 25년 문헌(9섹터 ETF, 12M/1M 롱온리)에서 4종목 이상이 샤프
      우위, (b) 4년 구간은 Top1/Top5 차이를 검출할 검정력이 없다(2σ 한계
      ≈ 연 12%p), (c) 손실 방어 우선이라는 계좌 원칙과 정면 충돌.
  T6  QQQ/VOO/SPY 대비는 **출력 전용**. 후보 풀이 2026년에 사람이 고른 목록이라
      과거로 돌리면 미래를 아는 풀이다. 절대 비교는 무효 — 판정에 쓰지 않는다.

⚠️ 비교는 **지정된 짝**끼리만 한다. 21개 설정 중 최고를 뽑는 짓은 하지 않는다.
   4년 강세장에 21번 질문하면 QQQ 를 이기는 설정은 반드시 나온다.


이 진단이 답할 수 없는 것 (구조적 한계 — 실행 전에 읽을 것)
─────────────────────────────────────────────────────────────
 1) **워밍업이 하락장을 먹는다.** 12-1/12-0 은 253봉이 필요하고, 공정 비교를
    위해 blend 도 같은 253봉을 쓴다. FMP 이력 상한 ≈1,254봉이므로 평가 가능
    구간은 ≈1,000봉(≈4년)이고 시작점이 2022년 하반기로 밀린다. 즉 2022년
    하락의 대부분이 평가 구간 **밖**이다. "어느 룰이 덜 잃는가"는 답이 안 나온다.
 2) **후보 풀 선택 편향.** fmp_extras.SECTOR_THEME_ETFS 는 2026년 시점의 선택이다.
    이 편향은 세 룰에 **똑같이** 걸리므로 상대 비교(T1~T4)는 유효하지만
    절대 비교(T6)는 무효다.
 3) **집중도 검정력 부족.** Top1 월변동성 6.5% / Top5 4.5% / 상관 0.85 가정 시
    4년 평균차이 표준오차 ≈ 6.2%p → 2σ 검출 한계 ≈ 연 12.4%p. T3 임계를 5/6 로
    높게 잡은 이유가 이것이다.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd
import pytz

# ── 리포 루트 + 자기 폴더를 sys.path 에 (실행 위치 무관) ─────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_extras as fx                 # noqa: E402  — 후보 풀 · 점수 룰 SSOT
import fmp_http as fh                   # noqa: E402  — 레이트리밋 SSOT
import diag_satellite_backtest as bt    # noqa: E402  — 시뮬레이션 엔진 (재구현 금지)

_KST = pytz.timezone("Asia/Seoul")
_ET = pytz.timezone("America/New_York")

GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")

_RESULT_WORKSHEET = "Momentum_Rule_Compare"

# ── 축 정의 ──────────────────────────────────────────────────────────────────
RULES = ("blend", "mom12_1", "mom12_0")

# (이름, 슬롯, swap모드, 가중스킴키 or None)
#
# 왜 eq3/eq5 가 swap 과 rebal 양쪽에 다 있나
# ──────────────────────────────────────────
# 차등 가중은 rebal 에서만 정의된다(swap 은 목표 비중 개념이 없다). 그런데
# 현행 운용은 swap 이다. 차등 vs 균등을 swap-균등과 rebal-차등으로 비교하면
# **가중 방식과 리밸런싱 방식이 한 덩어리로 움직여** 어느 쪽이 효과를 냈는지
# 알 수 없다. 그래서 rebal-균등을 대조군으로 같이 돌린다. T4 는 rebal 끼리만
# 비교한다.
VARIANTS = (
    ("top1",         1, "swap",  None),      # T5 — 출력 전용, 판정 배제
    ("top3_eq",      3, "swap",  None),
    ("top5_eq",      5, "swap",  None),      # ★ 현행 실제 운용
    ("top3_eq_rb",   3, "rebal", None),
    ("top5_eq_rb",   5, "rebal", None),
    ("top3_tier_rb", 3, "rebal", "tier3"),   # 50/30/20
    ("top5_tier_rb", 5, "rebal", "tier5"),   # 30/25/20/15/10
)

BASE_VARIANT = "top5_eq"          # 현행. 모든 룰 비교의 기준 짝.
EXCLUDED_FROM_VERDICT = ("top1",)  # T5

# 고정 축 — 실제 운용을 그대로 박는다.
#   freq="weekly"   : 주말 Hidden Alpha 이메일을 보고 순위 바뀌면 교체한다(확인함).
#                     ⚠️ 12-1 은 느린 신호라 주간 점검에서 교체가 드물게 일어난다.
#                     이건 12-1 에게 불리한 조건이 아니라 **유리한** 조건이다
#                     (회전 비용이 적게 든다). blend 는 1M 에 40% 라 회전이 많다.
#   mktfilter="none": 랭킹 룰 질문을 200MA 필터와 섞지 않는다. 필터는 이미
#                     Satellite_Backtest 그리드가 따로 재고 있다.
#   sellrule="top5" : 문자열은 옛 이름이지만 의미는 '슬롯 수만큼'이다(밴드 없음).
FIXED_FREQ = "weekly"
FIXED_MKTFILTER = "none"
FIXED_SELLRULE = "top5"

BENCH = ("SPY", "QQQ", "VOO")     # VOO 추가 — 질문이 "QQQ나 VOO보다 나은가" 였다.

# ── 워크포워드 창 ────────────────────────────────────────────────────────────
WF_WINDOWS = 6            # item B(산업 모멘텀)와 같은 모양으로 맞춘다
WF_BARS = 252             # 창 길이 = 12개월
MIN_SWAPS_PER_WINDOW = 3  # 창당 교체가 이보다 적으면 '정보 부족'으로 표시한다
#   왜 필요한가: 12-1 은 느려서 창 안에서 한 번도 교체를 안 할 수 있다. 그러면
#   그 창의 결과는 '룰의 실력'이 아니라 '그때 1등이 뭐였나' 다. 임계 판정에서
#   빼지는 않지만(사후 규칙 변경이 되므로) **반드시 눈에 보이게** 찍는다.

_RESULT_COLS = [
    "Run_Date", "Rule", "Variant", "Slots", "Swap", "Weights",
    "Win_Idx", "Start", "End",
    "Total_Ret_Pct", "CAGR_Pct", "MDD_Pct", "Sharpe",
    "Trades", "Swaps", "WinRate_Pct", "Turnover_x", "MaxW_Peak_Pct",
    "SPY_Ret_Pct", "QQQ_Ret_Pct", "VOO_Ret_Pct",
    "Warmup_Bars", "Universe_Hash",
]


# ══════════════════════════════════════════════════════════════════════════════
# 워크포워드 창 생성
# ══════════════════════════════════════════════════════════════════════════════
def walkforward_windows(n_bars: int, warmup: int, n_win: int = WF_WINDOWS,
                        win_bars: int = WF_BARS) -> list:
    """[warmup, n_bars-1] 안에 win_bars 짜리 창 n_win 개를 균등 간격으로 배치.

    반환: [(lo, hi), ...] — 마지막 창은 반드시 데이터 끝(n_bars-1)에 붙는다.

    창은 서로 겹친다(step < win_bars). 겹침은 의도된 것이지만 **독립 표본이
    아니다**. 6창 중 4승이 곧 이항검정 4/6 이 아니라는 뜻이다. 유효 독립 표본은
    대략 (평가봉수 / 252) ≈ 4 개다. 임계를 4/6·5/6 으로 잡은 것은 통계적 유의성
    선언이 아니라 **일관성 요구**다 — 한두 창의 운으로 결론이 뒤집히지 않게 하는
    장치일 뿐이다.
    """
    end_i = n_bars - 1
    first_lo = int(warmup)
    if end_i - first_lo + 1 < win_bars:
        return []
    last_lo = end_i - win_bars + 1
    if n_win <= 1 or last_lo <= first_lo:
        return [(last_lo, end_i)]
    span = last_lo - first_lo
    step = span / float(n_win - 1)
    los = sorted({int(round(first_lo + step * k)) for k in range(n_win)})
    return [(lo, lo + win_bars - 1) for lo in los]


def count_swaps(m: dict) -> int:
    """리밸런싱 로그에서 실제 교체(매도 발생) 횟수. 초기 구축은 세지 않는다."""
    log = m.get("log") or []
    return sum(1 for r in log[1:] if r.get("sold"))


# ══════════════════════════════════════════════════════════════════════════════
# 그리드 실행
# ══════════════════════════════════════════════════════════════════════════════
def run_grid(close_df: pd.DataFrame, adj_df: pd.DataFrame, warmup: int,
             windows: list, verbose: bool = True) -> dict:
    """{(rule, variant): [창별 metrics dict, ...]} + 벤치마크."""
    results: dict = {}
    engines = {}
    for rule in RULES:
        # ⚠️ 세 룰 모두 **같은** warmup 을 넘긴다. 룰별 기본값을 쓰면 blend 만
        #    127봉으로 더 일찍 시작해 다른 구간을 비교하게 된다.
        engines[rule] = bt.RankEngine(close_df, rank_rule=rule, warmup=warmup)

    for rule in RULES:
        eng = engines[rule]
        for vname, slots, swap, wkey in VARIANTS:
            weights = bt.WEIGHT_SCHEMES[wkey] if wkey else None
            cfg = (FIXED_FREQ, swap, FIXED_SELLRULE, FIXED_MKTFILTER)
            per_win = []
            for lo, hi in windows:
                try:
                    m = bt.simulate(cfg, eng, close_df, adj_df, lo, hi,
                                    slots=slots, weights=weights)
                except Exception as exc:
                    if verbose:
                        print(f"[WARN] {rule}/{vname} 창({lo},{hi}) 실패: {exc}")
                    m = {}
                per_win.append(m or {})
            results[(rule, vname)] = per_win
            if verbose:
                ok = sum(1 for m in per_win if m)
                print(f"  · {rule:<8} {vname:<14} {ok}/{len(windows)}창 완료")

    bench = {}
    for b in BENCH:
        bench[b] = [bt.buy_hold([b], adj_df, lo, hi) if b in adj_df.columns else {}
                    for lo, hi in windows]
    results["_bench"] = bench
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 지정 짝 비교 — 최고 뽑기가 아니라 사전에 정한 짝만 센다
# ══════════════════════════════════════════════════════════════════════════════
def pairwise(results: dict, a: tuple, b: tuple, metric: str = "sharpe") -> dict:
    """a 가 b 를 몇 창에서 이겼는가. 둘 다 결과가 있는 창만 센다."""
    ra, rb = results.get(a) or [], results.get(b) or []
    wins = comparable = 0
    deltas = []
    for ma, mb in zip(ra, rb):
        va, vb = ma.get(metric, np.nan), mb.get(metric, np.nan)
        if not (np.isfinite(va) and np.isfinite(vb)):
            continue
        comparable += 1
        deltas.append(va - vb)
        if va > vb:
            wins += 1
    return {"wins": wins, "n": comparable,
            "mean_delta": float(np.mean(deltas)) if deltas else float("nan")}


def mean_metric(results: dict, key: tuple, metric: str) -> float:
    vals = [m.get(metric, np.nan) for m in (results.get(key) or [])]
    vals = [v for v in vals if np.isfinite(v)]
    return float(np.mean(vals)) if vals else float("nan")


def verdicts(results: dict) -> list:
    """사전 커밋 기준 T1~T4 판정. 반환: [(코드, 설명, 통과여부, 근거문자열)]"""
    out = []

    # T1 / T2 — 랭킹 룰 (짝: 각 룰의 top5_eq vs blend 의 top5_eq)
    for code, rule in (("T1", "mom12_1"), ("T2", "mom12_0")):
        p = pairwise(results, (rule, BASE_VARIANT), ("blend", BASE_VARIANT))
        ok = p["n"] > 0 and p["wins"] >= 4
        out.append((code, f"{fx.MOM_RULE_LABELS[rule]} 가 현행을 대체", ok,
                    f"샤프 {p['wins']}/{p['n']}창 우위 (임계 4/6) · "
                    f"평균차 {p['mean_delta']:+.3f}"))

    # 승리 룰 = T1/T2 통과한 것 중 창 승수가 큰 쪽. 아무도 없으면 blend 유지.
    cand = [(c, r) for (c, r) in (("T1", "mom12_1"), ("T2", "mom12_0"))
            if any(o[0] == c and o[2] for o in out)]
    if cand:
        win_rule = max(
            (r for _, r in cand),
            key=lambda r: pairwise(results, (r, BASE_VARIANT),
                                   ("blend", BASE_VARIANT))["wins"])
    else:
        win_rule = "blend"

    # T3 — 집중도 (승리 룰 안에서 top3 vs top5, 둘 다 swap)
    p3 = pairwise(results, (win_rule, "top3_eq"), (win_rule, BASE_VARIANT))
    mdd3 = mean_metric(results, (win_rule, "top3_eq"), "mdd")
    mdd5 = mean_metric(results, (win_rule, BASE_VARIANT), "mdd")
    # MDD 는 음수다. '악화되지 않음' = mdd3 가 mdd5 보다 더 음수가 아님.
    mdd_ok = np.isfinite(mdd3) and np.isfinite(mdd5) and (mdd3 >= mdd5 - 1e-9)
    ok3 = p3["n"] > 0 and p3["wins"] >= 5 and mdd_ok
    out.append(("T3", f"Top3 가 Top5 를 대체 (룰={win_rule})", ok3,
                f"샤프 {p3['wins']}/{p3['n']}창 (임계 5/6) · "
                f"평균MDD {mdd3:.1f}% vs {mdd5:.1f}% → "
                f"{'악화없음' if mdd_ok else '악화'}"))

    # T4 — 차등 가중 (rebal 끼리, 세 룰 전부에서 같은 방향)
    for slots, tier_v, eq_v in ((3, "top3_tier_rb", "top3_eq_rb"),
                                (5, "top5_tier_rb", "top5_eq_rb")):
        details, all_ok = [], True
        for rule in RULES:
            p = pairwise(results, (rule, tier_v), (rule, eq_v))
            good = p["n"] > 0 and p["wins"] >= 5
            all_ok &= good
            details.append(f"{rule}:{p['wins']}/{p['n']}")
        out.append((f"T4-{slots}", f"Top{slots} 차등 가중이 균등을 대체", all_ok,
                    " · ".join(details) + " (임계: 세 룰 전부 5/6)"))

    return out


# ══════════════════════════════════════════════════════════════════════════════
# 출력
# ══════════════════════════════════════════════════════════════════════════════
def _f(v, nd=2, suffix=""):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "N/A"
    return f"{v:,.{nd}f}{suffix}"


def print_summary(results: dict, windows: list, idx: pd.DatetimeIndex) -> None:
    print(f"\n{'=' * 112}")
    print("■ 창 평균 요약 — 6개 워크포워드 창의 평균 (판정은 아래 별도)")
    print("=" * 112)
    print(f"{'룰':<10}{'변형':<15}{'수익%':>9}{'CAGR%':>9}{'MDD%':>9}"
          f"{'샤프':>8}{'교체':>7}{'회전x':>8}{'최대비중%':>11}")
    print("-" * 112)
    for rule in RULES:
        for vname, slots, swap, wkey in VARIANTS:
            key = (rule, vname)
            mark = " ★" if vname == BASE_VARIANT and rule == "blend" else ""
            mark = " ✋" if vname in EXCLUDED_FROM_VERDICT else mark
            print(f"{rule:<10}{vname + mark:<15}"
                  f"{_f(mean_metric(results, key, 'total_ret'), 1):>9}"
                  f"{_f(mean_metric(results, key, 'cagr'), 1):>9}"
                  f"{_f(mean_metric(results, key, 'mdd'), 1):>9}"
                  f"{_f(mean_metric(results, key, 'sharpe'), 2):>8}"
                  f"{np.mean([count_swaps(m) for m in results[key] if m] or [np.nan]):>7.1f}"
                  f"{_f(mean_metric(results, key, 'turnover'), 2):>8}"
                  f"{_f(mean_metric(results, key, 'maxw_peak'), 1):>11}")
        print("-" * 112)
    print("★ = 현행 실제 운용 · ✋ = 사전 배제(T5, 판정에 쓰지 않음)")

    bench = results.get("_bench") or {}
    line = "  ".join(
        f"{b} {_f(np.mean([m.get('total_ret', np.nan) for m in (bench.get(b) or []) if m]), 1)}%"
        for b in BENCH)
    print(f"\n[참고 · 판정 제외] 벤치마크 창 평균 수익 — {line}")
    print("  ⚠️ 후보 풀이 2026년 시점 선택이라 절대 비교는 무효다(T6). 보기만 할 것.")


def print_windows(results: dict, windows: list, idx: pd.DatetimeIndex) -> None:
    print(f"\n{'=' * 112}")
    print("■ 창별 샤프 — 지정 짝 비교의 원자료")
    print("=" * 112)
    hdr = "".join(f"{f'창{k + 1}':>9}" for k in range(len(windows)))
    print(f"{'룰/변형':<26}{hdr}")
    for k, (lo, hi) in enumerate(windows, 1):
        print(f"    창{k}: {idx[lo].date()} ~ {idx[hi].date()}")
    print("-" * 112)
    for rule in RULES:
        for vname, *_ in VARIANTS:
            row = "".join(f"{_f(m.get('sharpe'), 2):>9}"
                          for m in results[(rule, vname)])
            print(f"{rule + '/' + vname:<26}{row}")
    print("-" * 112)

    thin = []
    for rule in RULES:
        for vname, *_ in VARIANTS:
            for k, m in enumerate(results[(rule, vname)], 1):
                if m and count_swaps(m) < MIN_SWAPS_PER_WINDOW:
                    thin.append(f"{rule}/{vname} 창{k}({count_swaps(m)}회)")
    if thin:
        print(f"[정보부족 경고] 창당 교체 {MIN_SWAPS_PER_WINDOW}회 미만 — "
              f"{len(thin)}건")
        print("  " + " · ".join(thin[:12]) + (" …" if len(thin) > 12 else ""))
        print("  → 그 창의 성과는 '룰의 실력'이 아니라 '그때 1등이 뭐였나' 에 가깝다.")


def print_verdicts(vs: list) -> None:
    print(f"\n{'=' * 112}")
    print("■ 사전 커밋 판정 — 임계는 파일 상단에 박혀 있다(2026-09-05 확정)")
    print("=" * 112)
    for code, desc, ok, why in vs:
        print(f"  {'✅ 통과' if ok else '❌ 미달'}  {code:<6} {desc}")
        print(f"           {why}")
    print("-" * 112)
    passed = [c for c, _, ok, _ in vs if ok]
    if not passed:
        print("결론: 변경 없음. 현행(blend · Top5 균등 · 주간 swap) 유지.")
        print("      T1/T2 미달 시 해당 룰은 영구 종결 — 다시 재지 않는다.")
    else:
        print(f"결론: {', '.join(passed)} 통과. 해당 항목만 변경 검토 대상이다.")
        print("      통과하지 못한 항목은 이번 결과로 재협상하지 않는다.")
    print("\n⚠️ 어떤 판정도 QQQ/VOO 대비 우위를 뜻하지 않는다(T6). 그 질문의 유일한")
    print("   정답은 실계좌 전진 기록이다.")


# ══════════════════════════════════════════════════════════════════════════════
# 시트 기록
# ══════════════════════════════════════════════════════════════════════════════
def build_rows(results: dict, windows: list, idx: pd.DatetimeIndex,
               warmup: int, uhash: str) -> list:
    run_date = datetime.now(_ET).strftime("%Y-%m-%d")
    bench = results.get("_bench") or {}
    rows = []
    for rule in RULES:
        for vname, slots, swap, wkey in VARIANTS:
            wlabel = wkey or "equal"
            for k, ((lo, hi), m) in enumerate(zip(windows, results[(rule, vname)]), 1):
                if not m:
                    continue
                rows.append([
                    run_date, rule, vname, slots, swap, wlabel,
                    k, str(idx[lo].date()), str(idx[hi].date()),
                    round(m.get("total_ret", float("nan")), 2),
                    round(m.get("cagr", float("nan")), 2),
                    round(m.get("mdd", float("nan")), 2),
                    round(m.get("sharpe", float("nan")), 3),
                    m.get("trades", 0), count_swaps(m),
                    round(m.get("win", float("nan")), 1),
                    round(m.get("turnover", float("nan")), 2),
                    round(m.get("maxw_peak", float("nan")), 1),
                    *[round(((bench.get(b) or [{}] * len(windows))[k - 1] or {})
                            .get("total_ret", float("nan")), 2) for b in BENCH],
                    warmup, uhash,
                ])
    return rows


def write_results(rows: list) -> None:
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
        sh = bt._gs(gc.open, bt._SPREADSHEET_TITLE)
        titles = [w.title for w in bt._gs(sh.worksheets)]
        ncol = len(_RESULT_COLS)
        last_col = chr(ord("A") + ncol - 1)
        if _RESULT_WORKSHEET in titles:
            ws = bt._gs(sh.worksheet, _RESULT_WORKSHEET)
            if (bt._gs(ws.row_values, 1) or []) != _RESULT_COLS:
                bt._gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1",
                       value_input_option="USER_ENTERED")
                print(f"[INFO] {_RESULT_WORKSHEET} 헤더 갱신")
        else:
            ws = bt._gs(sh.add_worksheet, title=_RESULT_WORKSHEET,
                        rows=2000, cols=ncol)
            bt._gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1",
                   value_input_option="USER_ENTERED")
        bt._safe_append_rows(ws, rows, ncols=ncol)
        print(f"[OK] {_RESULT_WORKSHEET} 시트에 {len(rows)}행 기록")
    except Exception as exc:
        print(f"[WARN] 시트 기록 실패(콘솔 결과는 유효): {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# 자체검증 (네트워크 불필요)
# ══════════════════════════════════════════════════════════════════════════════
def _synthetic(n=700, seed=5):
    idx = pd.bdate_range("2022-01-03", periods=n)
    pool = fx.satellite_candidate_pool()
    tks = sorted({t for lst in pool.values() for t in lst} | set(BENCH))
    rng = np.random.default_rng(seed)
    close = pd.DataFrame(index=idx)
    for tk in tks:
        close[tk] = 100.0 * np.exp(np.cumsum(
            rng.normal(rng.normal(0.0003, 0.0005), 0.015, n)))
    return idx, close, close * 1.0


def _selftest() -> int:
    print("=" * 78)
    print("자체검증 — 축·판정 로직 (합성 데이터, 네트워크 불필요)")
    print("=" * 78)
    fails = []
    warmup = fx.mom_warmup_bars(RULES)

    # 1) 공통 워밍업이 253 이고, 세 룰 모두 그 값을 쓰는가
    if warmup != 253:
        fails.append(f"공통 워밍업이 253 이 아님: {warmup}")
    # 1b) [구멍 메움] 선언된 필요 봉수가 **실제** 필요 봉수와 정확히 일치하는가.
    #     변이 테스트에서 잡힌 구멍: score_mom12_1 의 back 을 231→252 로 바꾸면
    #     실제 필요 봉수는 274 가 되는데 MOM_RULES 는 253 이라고 계속 주장한다.
    #     그러면 랭킹은 조용히 '데이터 부족 종목 다수 탈락' 상태로 돌아간다.
    #     양방향으로 잰다 — need 에서 값이 나와야 하고, need-1 에서는 안 나와야 한다.
    _ramp = np.linspace(100.0, 300.0, 800)
    for rule, (fn, need) in fx.MOM_RULES.items():
        if not np.isfinite(fn(_ramp[:need])):
            fails.append(f"{rule}: 선언 필요봉수 {need}봉인데 점수가 nan "
                         f"— 식이 선언보다 더 긴 이력을 요구한다")
        if np.isfinite(fn(_ramp[:need - 1])):
            fails.append(f"{rule}: {need - 1}봉에서도 점수가 나옴 "
                         f"— 선언 필요봉수가 실제보다 크다(워밍업을 낭비한다)")

    idx, close, adj = _synthetic()
    for rule in RULES:
        e = bt.RankEngine(close, rank_rule=rule, warmup=warmup)
        if e.warmup != warmup:
            fails.append(f"{rule} 엔진 워밍업 불일치: {e.warmup}")
        if e.rank_rule != rule:
            fails.append(f"{rule} 엔진 룰 불일치: {e.rank_rule}")

    # 2) 룰이 실제로 다른 랭킹을 내는가 (셋이 같으면 축이 죽은 것)
    #    ⚠️ '실패 개수'만 보면 죽은 축을 못 잡는다 — 산업 모멘텀 때 배운 것.
    ranks = {r: [c["ticker"] for c in
                 bt.RankEngine(close, rank_rule=r, warmup=warmup).rank_at(idx[-1])[:5]]
             for r in RULES}
    if ranks["blend"] == ranks["mom12_1"] == ranks["mom12_0"]:
        fails.append(f"세 룰의 Top5 가 완전히 동일 — 룰 축이 작동하지 않음: {ranks}")

    # 3) 12-1 이 정말 직전 1개월을 무시하는가 (양성 대조)
    #    최근 21봉만 급등시킨 종목: 12-0 점수는 오르고 12-1 점수는 그대로여야 한다.
    v = np.full(400, 100.0)
    v[-21:] = 200.0
    s10, s11 = fx.score_mom12_0(v), fx.score_mom12_1(v)
    if not (s10 > 50.0):
        fails.append(f"12-0 이 직전 급등을 반영하지 못함: {s10}")
    if abs(s11) > 1e-9:
        fails.append(f"12-1 이 직전 1개월을 무시하지 못함: {s11} (0 이어야 함)")

    # 4) 슬롯 수가 실제로 보유 종목 수를 바꾸는가
    e = bt.RankEngine(close, rank_rule="blend", warmup=warmup)
    lo = warmup
    for slots in (1, 3, 5):
        m = bt.simulate((FIXED_FREQ, "swap", FIXED_SELLRULE, FIXED_MKTFILTER),
                        e, close, adj, lo, len(idx) - 1, slots=slots)
        held = max((len(r["held"]) for r in (m.get("log") or [])), default=0)
        if held != slots:
            fails.append(f"slots={slots} 인데 최대 보유 {held}종목")

    # 5) 차등 가중이 실제로 비중을 기울이는가 (양성 대조)
    m_eq = bt.simulate((FIXED_FREQ, "rebal", FIXED_SELLRULE, FIXED_MKTFILTER),
                       e, close, adj, lo, len(idx) - 1, slots=5)
    m_tr = bt.simulate((FIXED_FREQ, "rebal", FIXED_SELLRULE, FIXED_MKTFILTER),
                       e, close, adj, lo, len(idx) - 1, slots=5,
                       weights=bt.WEIGHT_SCHEMES["tier5"])
    if not (m_eq and m_tr):
        fails.append("가중 비교 시뮬레이션이 결과를 내지 못함")
    elif not (m_tr["maxw_peak"] > m_eq["maxw_peak"] + 1.0):
        fails.append(f"차등 가중인데 최대비중이 안 올라감: "
                     f"{m_tr.get('maxw_peak')} vs {m_eq.get('maxw_peak')}")

    # 6) swap + 차등은 거부되는가 (조용히 통과하면 의미 없는 비교가 시트에 남는다)
    try:
        bt.simulate((FIXED_FREQ, "swap", FIXED_SELLRULE, FIXED_MKTFILTER),
                    e, close, adj, lo, len(idx) - 1, slots=5,
                    weights=bt.WEIGHT_SCHEMES["tier5"])
        fails.append("swap + 차등 가중이 예외 없이 통과함")
    except ValueError:
        pass
    # ⚠️ 예외 **종류**까지 본다. 변이 테스트에서 잡힌 구멍: 길이 검증을 지우면
    #    (5, (0.5,0.5)) 는 합계 1.0 을 통과해 시뮬 안에서 IndexError 로 터진다.
    #    `except ValueError` 만 쓰면 그 IndexError 가 자체검증 밖으로 새어나가
    #    스크립트가 죽고, ❌ 는 한 줄도 안 찍힌다 — 통과처럼 보인다.
    for bad in ((5, (0.5, 0.5)), (3, (0.5, 0.3, 0.3)), (5, (0.2,) * 4)):
        try:
            bt.simulate((FIXED_FREQ, "rebal", FIXED_SELLRULE, FIXED_MKTFILTER),
                        e, close, adj, lo, len(idx) - 1, slots=bad[0], weights=bad[1])
            fails.append(f"잘못된 weights 가 통과함: {bad}")
        except Exception as exc:
            if not isinstance(exc, ValueError):
                fails.append(f"잘못된 weights {bad} 가 ValueError 가 아닌 "
                             f"{type(exc).__name__} 로 터짐 — 입력 검증이 아니라 "
                             f"우연히 죽은 것이다")

    # 7) 워크포워드 창 — 개수·길이·끝 정렬·워밍업 침범
    wins = walkforward_windows(len(idx), warmup)
    if len(wins) != WF_WINDOWS:
        fails.append(f"창 개수 {len(wins)} != {WF_WINDOWS}")
    if any(hi - lo + 1 != WF_BARS for lo, hi in wins):
        fails.append("창 길이가 252봉이 아님")
    if wins and wins[-1][1] != len(idx) - 1:
        fails.append("마지막 창이 데이터 끝에 붙어 있지 않음")
    if wins and wins[0][0] < warmup:
        fails.append("창이 워밍업 영역을 침범함")
    if len(wins) != len({lo for lo, _ in wins}):
        fails.append("창 시작점이 중복됨")

    # 8) pairwise 가 방향을 맞게 세는가 (양성 대조 — 검사기 자체 검증)
    fake = {("A", "x"): [{"sharpe": s} for s in (1, 1, 1, 0, 0, 0)],
            ("B", "x"): [{"sharpe": s} for s in (0, 0, 0, 1, 1, 1)]}
    p = pairwise(fake, ("A", "x"), ("B", "x"))
    if not (p["wins"] == 3 and p["n"] == 6 and abs(p["mean_delta"]) < 1e-9):
        fails.append(f"pairwise 계수 오류: {p}")
    p2 = pairwise({("A", "x"): [{"sharpe": np.nan}] * 6,
                   ("B", "x"): [{"sharpe": 1.0}] * 6}, ("A", "x"), ("B", "x"))
    if p2["n"] != 0:
        fails.append(f"nan 창을 비교 가능으로 셈: {p2}")

    # 9) [회귀] 판정이 임계를 실제로 강제하는가 — 3/6 은 떨어져야 한다
    def _mk(wins_a):
        r = {}
        for rule in RULES:
            for vn, *_ in VARIANTS:
                r[(rule, vn)] = [{"sharpe": 1.0, "mdd": -10.0} for _ in range(6)]
        r[("mom12_1", BASE_VARIANT)] = [
            {"sharpe": 2.0 if k < wins_a else 0.0, "mdd": -10.0} for k in range(6)]
        r["_bench"] = {b: [{}] * 6 for b in BENCH}
        return r
    if any(c == "T1" and ok for c, _, ok, _ in verdicts(_mk(3))):
        fails.append("T1 이 3/6 에서 통과함 — 임계가 강제되지 않는다")
    if not any(c == "T1" and ok for c, _, ok, _ in verdicts(_mk(4))):
        fails.append("T1 이 4/6 에서 통과하지 못함 — 임계가 잘못 걸려 있다")

    # 10) [회귀] T3 의 MDD 게이트가 살아 있는가 — 샤프 6/6 이어도 MDD 악화면 탈락
    r = {}
    for rule in RULES:
        for vn, *_ in VARIANTS:
            r[(rule, vn)] = [{"sharpe": 1.0, "mdd": -10.0} for _ in range(6)]
    r[("blend", "top3_eq")] = [{"sharpe": 2.0, "mdd": -25.0} for _ in range(6)]
    r["_bench"] = {b: [{}] * 6 for b in BENCH}
    if any(c == "T3" and ok for c, _, ok, _ in verdicts(r)):
        fails.append("T3 이 MDD 악화(-25% vs -10%)에도 통과함")

    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 전 항목 통과 (공통워밍업·룰축분리·12-1스킵·슬롯·차등가중·"
          "부정입력거부·창생성·pairwise방향·임계강제·MDD게이트)")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    t0 = time.time()
    print("=" * 112)
    print(f"📐 랭킹 룰 × 집중도 비교 — {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 112)
    print("판정 기준은 이 파일 상단에 사전 커밋되어 있다. 결과를 보고 바꾸지 않는다.")

    warmup = fx.mom_warmup_bars(RULES)
    pool = fx.satellite_candidate_pool()
    universe = sorted({t for lst in pool.values() for t in lst})
    fetch_list = sorted(set(universe) | set(BENCH))
    print(f"\n[STEP 1] 후보 풀 {len(universe)}개 + 벤치 {len(BENCH)}개 = "
          f"{len(fetch_list)}종목 수집 · 공통 워밍업 {warmup}봉")

    raw, adjmap, fallback, reasons, failed = bt._batch_fetch(
        fetch_list, bars=bt.HISTORY_BARS)
    n = len(fetch_list)
    rate = (len(raw) / n) if n else 1.0
    print(f"[STEP 1] 원종가 {len(raw)}/{n} ({rate * 100:.1f}%) · "
          f"배당조정 {len(raw) - len(fallback)}/{n}")
    print("[STEP 1] " + fh.fmp_stats_line())
    if "SPY" not in raw:
        print("[ERROR] SPY 이력 확보 실패 — 중단")
        return 1
    if rate < bt.MIN_FETCH_RATE:
        print(f"[ABORT] 페치 성공률 {rate * 100:.1f}% < {bt.MIN_FETCH_RATE * 100:.1f}% "
              f"— 후보 풀이 줄면 모집단 자체가 달라진다. 시트에 쓰지 않고 중단.")
        return 1
    missing = sorted(set(BENCH) - set(raw))
    if missing:
        print(f"[WARN] 벤치 미확보: {missing} — 해당 열은 N/A 로 남는다(판정 무관).")

    close_df, adj_df = bt.build_panels(raw, adjmap)
    idx = close_df.index
    print(f"[INFO] 캘린더 {len(idx)}봉 · {idx[0].date()} ~ {idx[-1].date()}")

    windows = walkforward_windows(len(idx), warmup)
    if len(windows) < WF_WINDOWS:
        print(f"[ABORT] 워크포워드 창 {len(windows)}/{WF_WINDOWS}개만 확보 — "
              f"필요 {warmup + WF_BARS}봉 / 보유 {len(idx)}봉. "
              f"창 수를 줄이면 사전 커밋 임계(4/6·5/6)가 무의미해진다.")
        return 1
    evaluable = len(idx) - warmup
    print(f"[INFO] 평가 가능 {evaluable}봉 (≈{evaluable / 252:.1f}년) · "
          f"창 {len(windows)}개 × {WF_BARS}봉")
    print(f"[INFO] ⚠️ 평가 시작 {idx[windows[0][0]].date()} — 2022년 하락의 대부분은 "
          f"워밍업({warmup}봉)에 먹혔다. 하락장 방어력은 이 진단의 답변 범위 밖이다.")

    print("\n[STEP 2] 그리드 실행 "
          f"({len(RULES)}룰 × {len(VARIANTS)}변형 × {len(windows)}창 = "
          f"{len(RULES) * len(VARIANTS) * len(windows)}회 시뮬)")
    results = run_grid(close_df, adj_df, warmup, windows)

    print_summary(results, windows, idx)
    print_windows(results, windows, idx)
    vs = verdicts(results)
    print_verdicts(vs)

    uhash = bt.universe_hash(universe)
    rows = build_rows(results, windows, idx, warmup, uhash)
    write_results(rows)

    print(f"\n⏱️ 소요 {time.time() - t0:.1f}초 · 유니버스 지문 {uhash}")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
