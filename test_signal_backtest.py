# -*- coding: utf-8 -*-
"""run_signal_backtest 순수 엔진 단위 테스트 v1.1 (FMP·시트 불필요)."""
import math
import numpy as np
import pandas as pd
import run_signal_backtest as bt

PASS = FAIL = 0
def approx(a, b, tol=1e-6):
    if b is None or (isinstance(b, float) and math.isnan(b)):
        return a is None or (isinstance(a, float) and math.isnan(a))
    return abs(float(a) - float(b)) <= tol
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {name}")
    else: FAIL += 1; print(f"  ✗ FAIL: {name}")


# ── 1) _forward_metrics + 초과수익 ────────────────────────────────────────────
print("[1] _forward_metrics (+ 초과수익)")
close = np.array([100, 110, 90, 120, 95, 130], float)
high = close.copy(); low = close.copy()
m = bt._forward_metrics(close, high, low, 0, horizons=(1, 2, 3), mfe_window=3)
check("ret_1d=+10%", approx(m["ret_1d"], 0.10))
check("ret_2d=-10%", approx(m["ret_2d"], -0.10))
check("ret_3d=+20%", approx(m["ret_3d"], 0.20))
check("mfe(3d)=+20%", approx(m["mfe"], 0.20))
check("mae(3d)=-10%", approx(m["mae"], -0.10))
check("spy_arr 없으면 excess NaN", all(math.isnan(m[f"excess_{h}d"]) for h in (1, 2, 3)))
# 초과수익: SPY 동일창 수익 차감
spy = np.array([100, 105, 108, 110, 112, 120], float)
me = bt._forward_metrics(close, high, low, 0, horizons=(1, 2, 3), mfe_window=3, spy_arr=spy)
check("excess_1d = 10% - 5% = 5%", approx(me["excess_1d"], 0.05))
check("excess_2d = -10% - 8% = -18%", approx(me["excess_2d"], -0.18))
check("excess_3d = 20% - 10% = 10%", approx(me["excess_3d"], 0.10))
check("MFE/MAE 는 절대값 유지(초과 아님)", approx(me["mfe"], 0.20) and approx(me["mae"], -0.10))


# ── 2) walk_forward_events : 2일 확정 + 쿨다운 디플랩 ─────────────────────────
print("[2] walk_forward_events (확정+쿨다운 디플랩)")
n = 16
idx = pd.date_range("2020-01-01", periods=n, freq="B")
cs = pd.Series(np.arange(10, 10 + n), index=idx, dtype=float)
hist = pd.DataFrame({"Close": cs, "High": cs, "Low": cs})
# 인덱스별 raw code (단발 깜빡임 wait 포함, entry 재출현, 장기 avoid)
_script = {0: "wait", 1: "wait", 2: "entry", 3: "entry", 4: "wait", 5: "entry",
           6: "entry", 7: "entry", 8: "overheat", 9: "overheat", 10: "wait",
           11: "avoid", 12: "avoid", 13: "avoid", 14: "avoid", 15: "avoid"}
def fake(slice_, spy_close=None):
    return {"timing": {"code": _script[len(slice_) - 1]}}

ev, d0, d1 = bt.walk_forward_events(hist, spy_close=None, min_prior=2, test_lookback=100,
                                    confirm_days=2, cooldown_days=3, analyze_fn=fake)
seq = [(e["code"], e["date"][-2:]) for e in ev]   # (code, 일자 끝2자리)
check(f"이벤트 4개 (got {len(ev)})", len(ev) == 4)
check("순서 entry→entry(쿨다운경과)→overheat→avoid",
      [e["code"] for e in ev] == ["entry", "entry", "overheat", "avoid"])
cnt = {c: sum(1 for e in ev if e["code"] == c) for c in bt.BUCKETS}
check("entry=2, overheat=1, avoid=1, wait=0(단발 깜빡임 필터)",
      cnt == {"entry": 2, "wait": 0, "overheat": 1, "trend_break": 0, "avoid": 1})

# 쿨다운=4 면 두번째 entry(gap=3<4) 억제 → 3개
ev2, _, _ = bt.walk_forward_events(hist, spy_close=None, min_prior=2, test_lookback=100,
                                   confirm_days=2, cooldown_days=4, analyze_fn=fake)
cnt2 = {c: sum(1 for e in ev2 if e["code"] == c) for c in bt.BUCKETS}
check("쿨다운=4 → entry 1개로 압축(총 3)", len(ev2) == 3 and cnt2["entry"] == 1)

# 확정일 = 연속 2일째. 첫 entry 확정 진입가 = close@i=3
e_first = next(e for e in ev if e["code"] == "entry")
check("첫 entry 확정 진입가 = close[i=3] = 13", approx(e_first["entry_price"], 13.0))


# ── 3) aggregate_events (+ 초과수익 집계) ────────────────────────────────────
print("[3] aggregate_events (+ 초과)")
evs = [
    {"code": "entry", "ret_5d": 0.02, "ret_20d": 0.10, "ret_60d": float("nan"), "mfe": 0.15, "mae": -0.04, "excess_20d": 0.03},
    {"code": "entry", "ret_5d": -0.01, "ret_20d": -0.05, "ret_60d": 0.08, "mfe": 0.05, "mae": -0.09, "excess_20d": -0.02},
    {"code": "entry", "ret_5d": 0.03, "ret_20d": 0.20, "ret_60d": 0.25, "mfe": 0.30, "mae": -0.02, "excess_20d": 0.05},
    {"code": "avoid", "ret_5d": -0.02, "ret_20d": -0.10, "ret_60d": -0.15, "mfe": 0.03, "mae": -0.18, "excess_20d": -0.08},
]
agg = bt.aggregate_events(evs)
en = agg["entry"]
check("entry count=3", en["count"] == 3)
check("entry winrate20d=66.67", approx(en["winrate_20d"], 66.67, 0.01))
check("entry ret_20d_mean=8.33", approx(en["ret_20d_mean"], 8.33, 0.01))
check("entry ret_60d_mean=16.5(NaN무시)", approx(en["ret_60d_mean"], 16.5, 0.01))
check("entry excess_20d_mean=2.0", approx(en["excess_20d_mean"], 2.0, 0.01))
check("entry excess_win_20d=66.67 (2/3 알파)", approx(en["excess_win_20d"], 66.67, 0.01))
check("avoid excess_win_20d=0.0", approx(agg["avoid"]["excess_win_20d"], 0.0, 0.01))
check("wait count=0 & excess NaN", agg["wait"]["count"] == 0 and math.isnan(agg["wait"]["excess_20d_mean"]))

rows = bt.build_result_rows(agg, "2026-06-14", "2023-01-01", "2026-01-01", 42)
check("결과 행=버킷 수(5)", len(rows) == len(bt.BUCKETS))
check("행 길이=컬럼 수(15)", all(len(r) == len(bt._RESULT_COLS) == 15 for r in rows))
wait_row = next(r for r in rows if r[4] == "wait")
check("wait 행 초과수익 셀 빈문자열", wait_row[13] == "" and wait_row[14] == "")


# ── 4) 스모크: 실제 regime_core + SPY(초과수익) ─────────────────────────────
print("[4] smoke (실제 regime_core + 초과수익)")
N = 320
di = pd.date_range("2023-01-02", periods=N, freq="B")
t = np.arange(N)
px = 100 * (1.0015 ** t) + 8 * np.sin(t / 9.0)
sc = pd.Series(px, index=di)
shist = pd.DataFrame({"Close": sc, "High": sc * 1.01, "Low": sc * 0.99})
spy_s = pd.Series(100 * (1.0005 ** t), index=di)
try:
    sev, sd0, sd1 = bt.walk_forward_events(shist, spy_close=spy_s, min_prior=220, test_lookback=80)
    sagg = bt.aggregate_events(sev)
    counts = {c: sagg[c]["count"] for c in bt.BUCKETS}
    n_excess = sum(1 for e in sev if not math.isnan(e.get("excess_20d", float("nan"))))
    print(f"    합성 이벤트 {len(sev)}개, 버킷 {counts}, 초과수익 산출 {n_excess}건")
    check("예외 없이 list 반환", isinstance(sev, list))
    check("평가 구간 설정", sd0 is not None and sd1 is not None)
    check("초과수익 일부 산출됨(SPY 정렬 동작)", n_excess >= 1)
except Exception as e:
    check(f"스모크 예외 없음 (got {type(e).__name__}: {e})", False)

print(f"\n결과: {PASS} passed, {FAIL} failed")
import sys as _s; _s.exit(1 if FAIL else 0)
