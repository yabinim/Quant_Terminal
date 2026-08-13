# -*- coding: utf-8 -*-
"""diag_gate_relabel.py — v3 게이트 강등(skip → 정보 표기) 회귀 검증.

검증 대상
  1) GATE_LABELS/skip 문구에 억제 어휘가 없다
  2) build_watchlist_plan: skip 이어도 enter_ok=True, gate_reason 에 '부족' 없음
  3) buy_decision: entry + skip → "buy" (강등 없음), avoid 는 여전히 강등
  4) decorate_entry_alert: skip → 🟢 라벨, 억제 어휘 없음, 플랜 숫자는 유지
  5) 회귀 방어: fit / avoid / caution / na 경로는 이전과 동일
  6) 백테스트 버킷터: rr_measured=False → alert_entry_na (pass 오염 제거)
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

# automation/ 에서 실행되든 루트에서 실행되든 공용 모듈을 찾게 한다(기존 diag 관례).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _find(name: str) -> str:
    """소스 파일 실경로. 루트 → automation/ 순으로 찾는다(실행 위치 무관)."""
    for base in (_ROOT, _HERE, os.getcwd()):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(name)


import regime_core as rc  # noqa: E402

FAIL: list[str] = []
BAN = ("건너뛰기", "미통과", "부족", "금지", "비권장")


def ck(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAIL.append(msg)


def synth(n=300, start=50.0, drift=0.0006, seed=7, high_gap=None):
    """상승 추세 OHLC. high_gap 을 주면 마지막 종가를 120일 고점 대비 그 비율 아래로 맞춘다."""
    rng = np.random.default_rng(seed)
    px = start * np.exp(np.cumsum(rng.normal(drift, 0.010, n)))
    if high_gap is not None:
        px[-1] = px[-120:].max() * (1.0 - high_gap)
    idx = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame({"Close": px, "High": px * 1.012, "Low": px * 0.988,
                         "Open": px, "Volume": 1_000_000}, index=idx)


_SEEDS = range(0, 40)
_GAPS = (0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.20)
_RRS = (1.5, 2.0, 2.5, 3.0)


def find_gate(want_gate, want_measured=True):
    """entry/wait 타이밍이 실제로 발생하는 합성 조합을 훑어 원하는 게이트를 찾는다.

    합성 데이터는 대부분 overheat/trend_break 로 판정되므로, 게이트 분기(entry/wait)를
    타는 조합을 탐색으로 확보해야 한다. 못 찾으면 테스트가 실패해야 하며,
    조용히 통과시키면 안 된다.
    """
    for seed in _SEEDS:
        for gap in _GAPS:
            h = synth(seed=seed, high_gap=gap)
            an = rc.analyze_ticker(h)
            if (an.get("timing") or {}).get("code") not in ("entry", "wait"):
                continue
            for rr in _RRS:
                p = rc.build_watchlist_plan(h, an, atr_mult=2.0, rr_target=rr)
                if p.get("gate") == want_gate and bool(p.get("rr_measured")) == want_measured:
                    p["_scenario"] = f"seed={seed} gap={gap} rr_target={rr}"
                    return p
    return None


print("=" * 66)
print("1) GATE_LABELS")
print("=" * 66)
lbl = rc.GATE_LABELS["skip"]
ck(not any(b in lbl for b in BAN), f"skip 라벨에 억제 어휘 없음 → {lbl!r}")
ck(rc.GATE_LABELS["avoid"].startswith("⛔"), "avoid 라벨은 그대로 ⛔ 유지")

print()
print("=" * 66)
print("2) build_watchlist_plan — skip 경로")
print("=" * 66)
# 고점 대비 2% 아래 = 여력이 손절폭보다는 크고 목표배수에는 못 미치는 구간을 노림.
skip_plan = find_gate("skip", want_measured=True)
ck(skip_plan is not None, "skip 게이트를 발생시키는 시나리오 확보")
if skip_plan:
    print(f"     시나리오: {skip_plan['_scenario']} · "
          f"R:R {skip_plan['rr_label']} · 목표기준 {skip_plan['target_basis']}")
if skip_plan:
    ck(skip_plan["enter_ok"] is True, "skip → enter_ok=True (억제 해제)")
    ck(skip_plan["rr_measured"] is True, "skip 은 R:R 실측 케이스여야 함")
    ck(not any(b in skip_plan["gate_reason"] for b in BAN),
       f"gate_reason 억제 어휘 없음 → {skip_plan['gate_reason']!r}")
    ck(np.isfinite(skip_plan["stop"]) and np.isfinite(skip_plan["target"]),
       "손절·목표 숫자는 그대로 산출됨")
    ck(skip_plan["shares"] is not None and skip_plan["dollars"] is not None,
       "수량·금액 계산이 게이트와 무관하게 유지됨")

print()
print("=" * 66)
print("3) buy_decision — skip 강등 제거 / avoid 유지")
print("=" * 66)
ck(rc.buy_decision("entry", "skip", "strong")["key"] == "buy",
   "entry + skip + strong → buy (강등 없음)")
ck(rc.buy_decision("entry", "skip", "weak")["key"] == "buy",
   "entry + skip + weak → buy (강등 없음)")
ck(rc.buy_decision("entry", "fit", "strong")["key"] == "buy",
   "entry + fit → buy (회귀 없음)")
ck(rc.buy_decision("entry", "avoid", "strong")["key"] == "avoid",
   "entry + avoid → avoid (억제 유지)")
ck(rc.buy_decision("avoid", "fit", "strong")["key"] == "avoid",
   "verdict=avoid → avoid (억제 유지)")
ck(rc.buy_decision("overheat", "caution", "strong")["key"] == "buy_split",
   "overheat → buy_split (회귀 없음)")
ck(rc.buy_decision("wait", "fit", "strong")["key"] == "wait_pullback",
   "wait → wait_pullback (회귀 없음)")

print()
print("=" * 66)
print("4) decorate_entry_alert — skip 메시지")
print("=" * 66)
if skip_plan:
    ev = rc.decorate_entry_alert(
        {"event": "entry", "message": "🎯 지금 매수 구간"}, skip_plan, "strong")
    print(f"     label   : {ev['label']}")
    print(f"     message : {ev['message']}")
    ck(ev["label"].startswith("🟢"), "skip 라벨이 🟢 (경고 아이콘 아님)")
    ck(not any(b in ev["label"] for b in BAN), "라벨에 억제 어휘 없음")
    ck(not any(b in ev["message"] for b in BAN), "본문에 억제 어휘 없음")
    ck("손절" in ev["message"] and "목표" in ev["message"],
       "손절·목표 숫자는 본문에 유지")
    ck(ev["gate"] == "skip", "gate 값 자체는 skip 으로 보존(계속 측정)")
    ck(ev["decision"] == "buy", "decision=buy")

print()
print("=" * 66)
print("5) 회귀 방어 — fit / avoid / caution / na")
print("=" * 66)
fit_plan = find_gate("fit", want_measured=True)
ck(fit_plan is not None, "fit(R:R 실측) 시나리오 확보")
if fit_plan:
    ev = rc.decorate_entry_alert(
        {"event": "entry", "message": "🎯 지금 매수 구간"}, fit_plan, "strong")
    ck("게이트 통과" in ev["label"], f"fit 라벨 회귀 없음 → {ev['label']!r}")
    ck(fit_plan["gate_reason"] and "충족" in fit_plan["gate_reason"],
       "fit gate_reason 회귀 없음")
    ck(fit_plan["enter_ok"] is True, "fit → enter_ok=True")

av = rc.decorate_entry_alert({"event": "entry", "message": "m"},
                             {"gate": "avoid", "gate_reason": "약세 회피 구간"}, "weak")
ck(av["label"].startswith("⛔") and "회피" in av["label"],
   f"avoid 는 억제 라벨 유지(⛔) → {av['label']!r}")
ck("건너뛰기" not in av["label"], "avoid 라벨에서 skip 어휘 제거됨")

ca = rc.decorate_entry_alert({"event": "entry", "message": "m"},
                             {"gate": "caution", "gate_reason": "과열/추세 흔들림"}, "strong")
ck(ca["label"].startswith("⚠️") and "신중" in ca["label"],
   f"caution 은 경고 라벨(⚠️ 신중) → {ca['label']!r}")

na = rc.decorate_entry_alert({"event": "entry", "message": "m"},
                             {"gate": "na", "gate_reason": "플랜 산출 불가"}, "strong")
ck("판단 보류" in na["label"], f"na 라벨 회귀 없음 → {na['label']!r}")

bad = rc.build_watchlist_plan(pd.DataFrame(), {}, atr_mult=2.0, rr_target=2.0)
ck(bad["gate"] == "na" and bad["enter_ok"] is False,
   "데이터 없음 → gate=na, enter_ok=False (앱의 '플랜 없음' 분기 보존)")

print()
print("=" * 66)
print("6) 백테스트 버킷터 — rr_measured 분리")
print("=" * 66)
# run_signal_backtest 를 통째로 import 하면 gspread/google-auth/requests/pytz 까지
# 끌어온다 — 순수 로직 하나 검증하자고 네트워크 라이브러리를 설치할 이유가 없다.
# (실제로 러너에서 ModuleNotFoundError: pytz 로 실패했다, 2026-08-12)
# 대신 소스에서 _alert_bucket 만 추출해 격리 실행한다. 검사 대상은 여전히
# **저장소의 진짜 소스**이고, import 환경과 무관하게 로직을 고정할 수 있다.
import ast

try:
    _bt_path = _find("automation/run_signal_backtest.py")
    with open(_bt_path, encoding="utf-8") as _f:
        _bt_src = _f.read()
    _fn = next((n for n in ast.walk(ast.parse(_bt_src))
                if isinstance(n, ast.FunctionDef) and n.name == "_alert_bucket"), None)
    ck(_fn is not None, "_alert_bucket 을 소스에서 찾음")

    if _fn is not None:
        _ns = {"rc": rc}
        exec(compile(ast.Module(body=[_fn], type_ignores=[]),
                     _bt_path, "exec"), _ns)          # noqa: S102
        _bucket = _ns["_alert_bucket"]

        _orig = rc.build_watchlist_plan
        cases = [
            ({"gate": "fit", "rr_measured": True},   "alert_entry_pass", "fit+실측 → pass"),
            ({"gate": "fit", "rr_measured": False},  "alert_entry_na",   "fit+미실측 → na (오염 제거)"),
            ({"gate": "skip", "rr_measured": True},  "alert_entry_skip", "skip → skip"),
            ({"gate": "avoid", "rr_measured": True}, "alert_entry_skip", "avoid → skip 버킷(기존 유지)"),
            ({"gate": "na", "rr_measured": False},   "alert_entry_na",   "na → na"),
        ]
        try:
            for fake, want, tag in cases:
                rc.build_watchlist_plan = (lambda *a, _f=fake, **k: _f)
                got = _bucket({"event": "entry"}, pd.DataFrame(), {})
                ck(got == want, f"{tag} (got={got})")

            def _boom(*a, **k):
                raise RuntimeError("plan fail")
            rc.build_watchlist_plan = _boom
            ck(_bucket({"event": "entry"}, pd.DataFrame(), {}) == "alert_entry_na",
               "예외 → na (신호 유실 없음)")
        finally:
            rc.build_watchlist_plan = _orig

        ck(_bucket({"event": "risk"}, pd.DataFrame(), {}) == "alert_risk",
           "risk 이벤트 회귀 없음")
        ck(_bucket({"event": "entry_invalid"}, pd.DataFrame(), {}) == "alert_entry_invalid",
           "entry_invalid 회귀 없음")
        ck(_bucket({"event": "exit"}, pd.DataFrame(), {}) is None,
           "집계 대상 아닌 이벤트는 None")
except Exception as e:  # noqa: BLE001
    ck(False, f"버킷터 검증 실패: {type(e).__name__}: {e}")

print()
print("=" * 66)
if FAIL:
    print(f"❌ 실패 {len(FAIL)}건")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("✅ 전체 통과")
