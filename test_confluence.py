# -*- coding: utf-8 -*-
"""regime_core.confluence_score 단위 테스트 (#3)."""
import math, regime_core as rc
P = F = 0
def ap(a, b, t=1e-6): return (b is None and a is None) or (a is not None and abs(float(a)-float(b)) <= t)
def ck(n, c):
    global P, F
    if c: P += 1; print(f"  ✓ {n}")
    else: F += 1; print(f"  ✗ FAIL: {n}")

print("[1] 전 팩터 양수 → 100")
r = rc.confluence_score(verdict_code="entry", rs_excess=0.10, piotroski=8, altman_z=5,
                        insider_ratio=2.0, analyst_upside=0.30)
ck("score=100", ap(r["score"], 100.0)); ck("n_factors=5", r["n_factors"] == 5)
ck("label 근거 강함", "강함" in r["label"]); ck("avoid_flag False", r["avoid_flag"] is False)

print("[2] 중립 → 50")
r = rc.confluence_score(verdict_code="overheat", rs_excess=0.0, piotroski=5, altman_z=2.5,
                        insider_ratio=1.0, analyst_upside=0.05)
ck("score=50", ap(r["score"], 50.0))

print("[3] 결측 제외 + 재정규화 (timing+rs만, 둘다 +1 → 100)")
r = rc.confluence_score(verdict_code="entry", rs_excess=0.10)
ck("score=100", ap(r["score"], 100.0)); ck("n_factors=2", r["n_factors"] == 2)
miss = [b for b in r["breakdown"] if b["key"] == "insider"][0]
ck("insider available False", miss["available"] is False)

print("[4] avoid → 점수 상한 35 + 플래그")
r = rc.confluence_score(verdict_code="avoid", rs_excess=0.10, piotroski=8, altman_z=5,
                        insider_ratio=2.0, analyst_upside=0.30)
ck("raw_score=75", ap(r["raw_score"], 75.0)); ck("score 상한 35", ap(r["score"], 35.0))
ck("avoid_flag True", r["avoid_flag"] is True); ck("label 회피", "회피" in r["label"])

print("[5] 펀더멘털 일부만(Piotroski만)")
r = rc.confluence_score(verdict_code="wait", piotroski=8)
fb = [b for b in r["breakdown"] if b["key"] == "fundamental"][0]
ck("fundamental available True(Piotroski만)", fb["available"] is True and ap(fb["signal"], 1.0))

print("[6] 어닝 임박 플래그 / 공매도 노트 / 무팩터")
ck("earnings D-3 → warning", rc.confluence_score(verdict_code="entry", earnings_days=3)["earnings_warning"] is True)
ck("earnings D-10 → no warning", rc.confluence_score(verdict_code="entry", earnings_days=10)["earnings_warning"] is False)
ck("공매도 25% → 스퀴즈 노트", "스퀴즈" in rc.confluence_score(verdict_code="entry", short_pct=25)["short_note"])
rn = rc.confluence_score()
ck("팩터 전무 → score None & label 데이터 부족", rn["score"] is None and "데이터" in rn["label"])

print("[7] 음수 팩터 → 낮은 점수")
r = rc.confluence_score(verdict_code="trend_break", rs_excess=-0.10, piotroski=2, altman_z=1.0,
                        insider_ratio=0.5, analyst_upside=-0.05)
ck("score < 35 (근거 희박)", r["score"] is not None and r["score"] < 35 and "희박" in r["label"])

print(f"\n결과: {P} passed, {F} failed")
import sys; sys.exit(1 if F else 0)
