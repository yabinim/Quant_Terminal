# -*- coding: utf-8 -*-
"""regime_core 포지션 사이징·R:R 게이트 단위 테스트 (#2)."""
import math
import numpy as np
import regime_core as rc

PASS = FAIL = 0
def ap(a, b, tol=1e-6):
    if b is None or (isinstance(b, float) and math.isnan(b)):
        return a is None or (isinstance(a, float) and math.isnan(a))
    return abs(float(a) - float(b)) <= tol
def ck(n, c):
    global PASS, FAIL
    if c: PASS += 1; print(f"  ✓ {n}")
    else: FAIL += 1; print(f"  ✗ FAIL: {n}")

# 1) evaluate_rr
print("[1] evaluate_rr")
r = rc.evaluate_rr(100, 95, 115)
ck("risk=5", ap(r["risk"], 5)); ck("reward=15", ap(r["reward"], 15))
ck("R:R=3.0", ap(r["r_multiple"], 3.0)); ck("label 1:3.0", r["label"] == "1:3.0")
ck("target<=entry → NaN", math.isnan(rc.evaluate_rr(100, 95, 100)["r_multiple"]))
ck("stop>=entry → NaN", math.isnan(rc.evaluate_rr(100, 100, 115)["r_multiple"]))

# 2) position_size
print("[2] position_size")
p = rc.position_size(10000, 1.0, 100, 95)   # 예산$100 / 리스크주당5 → 20주
ck("shares=20", p["shares"] == 20); ck("dollars=2000", ap(p["dollars"], 2000))
ck("risk_dollars=100", ap(p["risk_dollars"], 100)); ck("position_pct=20", ap(p["position_pct"], 20.0))
ck("미초과 → capped False", p["capped"] is False)
pc = rc.position_size(10000, 5.0, 100, 98)   # 예산$500/주당2=250주=$25000 > 상한$2000
ck("상한 적용 shares=20", pc["shares"] == 20); ck("capped True", pc["capped"] is True)
ck("stop>=entry → shares 0", rc.position_size(10000, 1.0, 100, 100)["shares"] == 0)
ck("equity<=0 → shares 0", rc.position_size(0, 1.0, 100, 95)["shares"] == 0)

# 3) resolve_stop
print("[3] resolve_stop")
s, src = rc.resolve_stop(100, 3, 90, 2, "atr")
ck("ATR 손절=94", ap(s, 94) and src == "atr")
s2, src2 = rc.resolve_stop(100, 3, 90, 2, "ma200")
ck("200MA 손절=88.2", ap(s2, 88.2) and src2 == "ma200")
s3, src3 = rc.resolve_stop(100, 3, 90, 2, "manual", manual_stop=92)
ck("수동 손절=92", ap(s3, 92) and src3 == "manual")
ck("ATR 0 → NaN", math.isnan(rc.resolve_stop(100, 0, None, 2, "atr")[0]))
ck("수동 손절>=진입 → NaN", math.isnan(rc.resolve_stop(100, 3, 90, 2, "manual", manual_stop=105)[0]))

# 4) resolve_target (우선순위)
print("[4] resolve_target")
t, b = rc.resolve_target(100, 6, 2, recent_high=115, manual_target=120)
ck("수동 우선 → 120/manual", ap(t, 120) and b == "manual")
t2, b2 = rc.resolve_target(100, 6, 2, recent_high=115, manual_target=None)
ck("구조적 고점 → 115/structural_high", ap(t2, 115) and b2 == "structural_high")
t3, b3 = rc.resolve_target(100, 6, 2, recent_high=95, manual_target=None)  # 고점이 진입 아래 → 파생
ck("고점<진입 → rr_derived=112", ap(t3, 112) and b3 == "rr_derived")

# 5) build_trade_plan 게이트
print("[5] build_trade_plan 게이트")
base = dict(entry=100, atr=3, ma200=90, equity=10000, risk_pct=1, atr_mult=2, rr_target=2)
# avoid → 회피
g_av = rc.build_trade_plan("avoid", **base)
ck("avoid → gate avoid & enter_ok False", g_av["gate"] == "avoid" and g_av["enter_ok"] is False)
ck("avoid 손절=94(ATR)", ap(g_av["stop"], 94))
# entry + 실목표 R:R=2.5(>=2) → fit
g_fit = rc.build_trade_plan("entry", manual_target=115, **base)
ck("entry 좋은 R:R → fit & enter_ok", g_fit["gate"] == "fit" and g_fit["enter_ok"] is True)
ck("R:R=2.5", ap(g_fit["r_multiple"], (115 - 100) / 6))
# entry + 실목표 R:R<2 → skip
g_skip = rc.build_trade_plan("entry", manual_target=104, **base)
ck("entry 나쁜 R:R → skip & enter_ok False", g_skip["gate"] == "skip" and g_skip["enter_ok"] is False)
# entry + 독립목표 없음 → rr_derived → fit(정보용, 필터 안함)
g_info = rc.build_trade_plan("entry", **base)
ck("독립목표 없음 → fit & basis rr_derived", g_info["gate"] == "fit" and g_info["target_basis"] == "rr_derived")
# overheat → caution
ck("overheat → caution", rc.build_trade_plan("overheat", **base)["gate"] == "caution")
# 손절 산출 불가 → na
g_na = rc.build_trade_plan("entry", entry=100, atr=0, ma200=None, equity=10000, risk_pct=1, atr_mult=2, rr_target=2)
ck("ATR0·MA없음 → gate na", g_na["gate"] == "na")
# 사이즈 포함 확인
ck("fit 에 shares>0", g_fit["shares"] > 0)

print(f"\n결과: {PASS} passed, {FAIL} failed")
import sys; sys.exit(1 if FAIL else 0)
