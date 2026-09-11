#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_beta_mom_ref.py — β중립 12-0 참고 실행 (판정 아님 · 1회 전용 · 읽기 전용 진단)

  실행: python automation/diag_beta_mom_ref.py
        python automation/diag_beta_mom_ref.py --selftest   # 네트워크 불필요

아무것도 수정하지 않는다. `Momentum_Beta_Ref` 탭에 결과 행만 append 한다.
`Momentum_Rule_Deep`(작업 A 결과)은 **읽기만** 한다 — R0 재현 대조의 원본이다.
`Momentum_Rule_Compare`(§4① 판정 기록)에는 쓰지도 읽지도 않는다.


═══════════════════════════════════════════════════════════════════════════════
β중립 12-0 참고 실행 — 사전 약정 (2026-09-11 확정, 결과 보기 전)
═══════════════════════════════════════════════════════════════════════════════
질문   "12-0 의 모멘텀 붕괴 약점을 β항 제거가 완화하는가, 그 대가는 얼마인가."
       약점의 출처: 깊은 창 참고 실행(§2 각주) — 필터 없이 12-0 은 2009 반등에서
       SPY 대비 −17.0%p, 2018 Q4 하락에서 −7.6%p.
대상   mom12_0 (A반) vs mom12_0_bn = 12-0(종목) − β̂ × 12-0(SPY)
       β̂ = 최근 50주 주간수익률 기울기(날짜 정렬 · 5봉 간격). 필요 봉수 253 = 12-0.
       × top5 균등 · swap · weekly (작업 A 와 같은 축) × 필터 none / no_new
기준일 AS_OF = 2026-09-10 **상수** (입력 아님 — 날짜를 바꿔 다시 굴리는 경로를 없앤다)
R0     12-0 행이 작업 A 실행(Momentum_Rule_Deep)과 ±0.10%p — 수익률·초과·MDD·교체.
       A 에서 n/a 였던 칸(사건 1 하락 구간)은 건너뛴다. 대조 불가·불일치 → 기록 전 중단.
판정   필터 none 만. no_new 는 표시만 한다(바닥에서 현금이라 룰이 거의 안 움직인다).
       Δ = 초과(bn) − 초과(12-0), 같은 실행 · 같은 구간.
  C1   대상 두 구간 **모두** Δ ≥ +5.0%p — 사건 1 반등(바닥 2009-03) · 2018 하락(바닥 2018-12)
  C2   나머지 구간(절삭·절단 제외) Δ 중앙값 ≥ −2.0%p, 최악 ≥ −10.0%p
  C3   평시 구간(사건 구간 밖) 연율 Δ ≥ −2.0%p/년
결과   C1·C2·C3 모두 → "완화 관찰됨" → 종이 C반 설계는 별도 작업. A/B 불변(§2·§5).
       하나라도 미달 → β중립 모멘텀 **종료**. 변형·문턱·구간·기준일을 바꾼 재시험 금지.
1회    이 탭에 행이 하나라도 있으면 실행하지 않는다. 측정 결함으로 기록 전에 중단한
       경우만 재실행할 수 있고, 그 사유는 §7 에 남긴다.

+5.0%p 는 두 top5 장부가 한 구간에서 경로 우연만으로 벌어질 수 있는 폭보다는 커야
한다는 판단값이다. 대상 구간이 n=2 라 통계적 문턱이 아니다 — 그래서 결론은
"우월"이 아니라 "완화 관찰됨"까지만 쓴다.

⚠️ 이 블록의 숫자는 SATELLITE_MANDATE §4① 과 diag_satellite_mandate K 그룹이 서로
   묶는다. 여기만 고치면 드리프트 가드가 빨간불이 된다 — 그게 목적이다.


설계 메모
─────────
 1) **게이트 전에는 bn 수치를 한 줄도 출력하지 않는다.** R0 가 실패하면 12-0 불일치만
    찍고 멈춘다. 결과를 본 뒤 "측정 결함"을 핑계로 다시 굴리는 경로를 막는다.
 2) **재구현 금지.** 사건·구간·측정·게이트는 diag_momentum_deep_ref 의 함수를 쓴다.
    그 파일(작업 A 약정)은 건드리지 않는다 — 룰 목록이 동결 튜플이라 여기서 따로 돈다.
 3) **평시 구간.** 사건 구간(하락 [고점, 바닥], 반등 [바닥, +126봉])의 수익 봉 (lo, hi]
    밖 전부. 사건 구간과 평시 구간의 일간 수익이 전 기간 수익을 정확히 나눈다.
    전체 기간 샤프·승수는 여전히 출력하지 않는다 — 평시는 "대가"를 재는 칸이다.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytz

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_extras as fx                       # noqa: E402  — 룰 SSOT
import fmp_http as fh                         # noqa: E402  — 레이트리밋 SSOT
import diag_satellite_backtest as bt          # noqa: E402  — 엔진 (재구현 금지)
import diag_momentum_rule_compare as rc       # noqa: E402  — 고정 축 SSOT
import diag_momentum_deep_ref as d            # noqa: E402  — 사건·구간·게이트 (재구현 금지)

_KST = pytz.timezone("Asia/Seoul")
_ET = pytz.timezone("America/New_York")

GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")
_RESULT_WORKSHEET = "Momentum_Beta_Ref"
_DEEP_WORKSHEET = d._RESULT_WORKSHEET          # R0 대조 원본 — 읽기만

# ── 사전 약정 상수 (2026-09-11 · 결과 보기 전) ───────────────────────────────
RULE_BASE = "mom12_0"                          # A반
RULE_TEST = "mom12_0_bn"                       # β중립 12-0
RULES = (RULE_BASE, RULE_TEST)
FILTERS = d.FILTERS                            # none / no_new — 작업 A 와 같은 튜플
VERDICT_FILTER = "none"
AS_OF = "2026-09-10"                           # 작업 A 실행의 수신 끝날
R0_TOL_PP = 0.10
R0_MIN_ROWS = 20
C1_MIN_PP = 5.0
C2_MEDIAN_MIN_PP = -2.0
C2_WORST_MIN_PP = -10.0
C3_CALM_MIN_PP = -2.0
TARGET_LEGS = (("rebound", "2009-03"), ("drawdown", "2018-12"))   # (구간, 바닥 연-월)

_RESULT_COLS = [
    "Run_Date", "Rule", "MktFilter", "Episode_Idx", "Leg", "Target", "Start", "End",
    "Bars", "Clipped", "Truncated", "SPY_Ret_Pct", "Port_Ret_Pct", "Excess_pp",
    "Delta_pp", "Port_MDD_Pct", "Swaps", "Meas_Start", "Verdict",
    "As_Of", "Recv_From", "Recv_To", "Recv_Bars", "Window_Days", "Universe_Hash",
]
_NAN = float("nan")


# ══════════════════════════════════════════════════════════════════════════════
# 대상 구간 · 평시 구간
# ══════════════════════════════════════════════════════════════════════════════
def find_targets(legs: list, episodes: list, idx: pd.DatetimeIndex,
                 targets=TARGET_LEGS) -> dict:
    """{(구간, 바닥 연-월): 구간} — 각각 **정확히 하나**여야 한다. 아니면 ValueError.

    대상은 SPY 만으로 정해진다(사건 목록과 같다). 룰 결과와 독립이라 미리 박을 수 있다.
    """
    out = {}
    for leg_kind, ym in targets:
        hit = [L for L in legs
               if L["leg"] == leg_kind
               and str(idx[episodes[L["ep"] - 1]["trough_i"]].date())[:7] == ym]
        if len(hit) != 1:
            raise ValueError(f"대상 구간 ({leg_kind}, 바닥 {ym}) 이 {len(hit)}개 — 정확히 1개여야 한다")
        out[(leg_kind, ym)] = hit[0]
    return out


def calm_mask(legs: list, eval_i: int, n: int) -> np.ndarray:
    """수익 봉 t(= t-1 → t) 가 평시면 True. 평가 시작 봉과 사건 구간 (lo, hi] 는 False."""
    m = np.zeros(n, dtype=bool)
    m[eval_i + 1:] = True
    for L in legs:
        m[L["lo"] + 1:L["hi"] + 1] = False
    return m


def _bar_returns(s: pd.Series, idx: pd.DatetimeIndex) -> np.ndarray:
    v = s.reindex(idx).to_numpy(dtype=float)
    r = np.full(len(v), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        r[1:] = v[1:] / v[:-1] - 1.0
    return r


def _ann(r: np.ndarray) -> float:
    if len(r) == 0:
        return _NAN
    g = float(np.prod(1.0 + r))
    return (g ** (252.0 / len(r)) - 1.0) * 100.0 if g > 0 else _NAN


def calm_stats(sims: dict, adj_spy: pd.Series, idx: pd.DatetimeIndex, mask: np.ndarray,
               flt: str) -> dict:
    """필터 하나에 대해 평시 연율(%) — 두 룰·SPY 를 **같은 봉 집합**에서 잰다."""
    rb = _bar_returns(sims[(RULE_BASE, flt)].get("curve", pd.Series(dtype=float)), idx)
    rt = _bar_returns(sims[(RULE_TEST, flt)].get("curve", pd.Series(dtype=float)), idx)
    rs = _bar_returns(adj_spy, idx)
    use = mask & np.isfinite(rb) & np.isfinite(rt) & np.isfinite(rs)
    out = {"bars": int(use.sum()), "spy": _ann(rs[use]),
           RULE_BASE: _ann(rb[use]), RULE_TEST: _ann(rt[use])}
    out["delta"] = out[RULE_TEST] - out[RULE_BASE]
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 구간 표 · R0 · 판정 — 전부 순수 함수 (자체검증이 그대로 부른다)
# ══════════════════════════════════════════════════════════════════════════════
def leg_table(sims: dict, adj_df: pd.DataFrame, idx: pd.DatetimeIndex, legs: list,
              tgt: dict) -> list:
    tkey = {id(L): f"{k[0]}:{k[1]}" for k, L in tgt.items()}
    out = []
    for L in legs:
        lo_d, hi_d = idx[L["lo"]], idx[L["hi"]]
        spy_ret = d.span_ret(adj_df["SPY"], lo_d, hi_d)
        ex_of = {}
        for flt in FILTERS:
            for r in RULES:
                m = sims.get((r, flt)) or {}
                lm = (d.leg_metrics(m["curve"], m.get("log"), lo_d, hi_d) if m
                      else {"ret": _NAN, "mdd": _NAN, "swaps": 0, "start": lo_d})
                spy_row = d.span_ret(adj_df["SPY"], lm["start"], hi_d) \
                    if lm["start"] != lo_d else spy_ret
                ex = lm["ret"] - spy_row
                ex_of[(r, flt)] = ex
                out.append({"ep": L["ep"], "leg": L["leg"], "lo_d": lo_d, "hi_d": hi_d,
                            "bars": L["hi"] - L["lo"], "clipped": L["clipped"],
                            "truncated": L["truncated"], "target": tkey.get(id(L), ""),
                            "rule": r, "flt": flt, "spy": spy_row, "ret": lm["ret"],
                            "ex": ex, "mdd": lm["mdd"], "swaps": lm["swaps"],
                            "meas_start": pd.Timestamp(lm["start"]),
                            "delta": (ex - ex_of[(RULE_BASE, flt)]) if r == RULE_TEST else _NAN})
    return out


def _num(v) -> float:
    if v is None or (isinstance(v, str) and not v.strip()):
        return _NAN
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except ValueError:
        return _NAN


def r0_check(tbl: list, deep_records: list, meta: dict) -> tuple:
    """(ok, 대조 행 수, 메시지들). 12-0 행만 본다 — bn 은 여기 들어오지 않는다.

    원본 선택: Rule=mom12_0 · 같은 Universe_Hash · 같은 Recv_From/To · 같은 Window_Days.
    여러 번 기록돼 있으면 **가장 이른 Run_Date**(= 약정 후 첫 실행)만 쓴다.
    """
    want = {"Universe_Hash": str(meta.get("uhash", "")),
            "Recv_From": str(meta.get("recv_from", "")),
            "Recv_To": str(meta.get("recv_to", "")),
            "Window_Days": str(d.DEEP_WINDOW_DAYS)}
    src = [r for r in deep_records
           if str(r.get("Rule", "")) == RULE_BASE
           and all(str(r.get(k, "")).strip() == v for k, v in want.items())]
    if not src:
        return False, 0, [f"대조 원본 없음 — {_DEEP_WORKSHEET} 에 같은 창·유니버스의 "
                          f"{RULE_BASE} 행이 없다 ({want})"]
    first = min(str(r.get("Run_Date", "")) for r in src)
    src = [r for r in src if str(r.get("Run_Date", "")) == first]
    ref = {}
    for r in src:
        k = (str(r.get("MktFilter")), int(_num(r.get("Episode_Idx"))), str(r.get("Leg")))
        if k in ref:
            return False, 0, [f"대조 원본 키 중복 {k} (Run_Date {first})"]
        ref[k] = r
    msgs, n_cmp = [], 0
    for t in tbl:
        if t["rule"] != RULE_BASE:
            continue
        k = (t["flt"], int(t["ep"]), t["leg"])
        a = ref.get(k)
        if a is None:
            msgs.append(f"{k}: 원본에 없는 구간")
            continue
        if (str(a.get("Start")), str(a.get("End"))) != (str(t["lo_d"].date()), str(t["hi_d"].date())):
            msgs.append(f"{k}: 구간 경계 {a.get('Start')}~{a.get('End')} != "
                        f"{t['lo_d'].date()}~{t['hi_d'].date()}")
            continue
        if not np.isfinite(_num(a.get("Excess_pp"))):
            continue                                    # 작업 A 의 n/a 칸 — 대조 불가, 건너뜀
        n_cmp += 1
        for col, key in (("Port_Ret_Pct", "ret"), ("Excess_pp", "ex"), ("Port_MDD_Pct", "mdd")):
            av, tv = _num(a.get(col)), round(float(t[key]), 2)
            if not (np.isfinite(av) and np.isfinite(tv)) or abs(av - tv) > R0_TOL_PP:
                msgs.append(f"{k}: {col} 원본 {a.get(col)} · 재실행 {tv:+.2f}")
        if int(_num(a.get("Swaps"))) != int(t["swaps"]):
            msgs.append(f"{k}: Swaps 원본 {a.get('Swaps')} · 재실행 {t['swaps']}")
    if n_cmp < R0_MIN_ROWS:
        msgs.append(f"대조 행 {n_cmp} < {R0_MIN_ROWS}")
    return (not msgs), n_cmp, msgs


def verdict(tbl: list, calm_none: dict) -> dict:
    """C1·C2·C3 — 필터 none 만. 필요한 수치에 nan 이 있으면 defect 목록을 채운다."""
    rows = [t for t in tbl if t["rule"] == RULE_TEST and t["flt"] == VERDICT_FILTER]
    defect = []
    c1 = {t["target"]: t["delta"] for t in rows if t["target"]}
    if len(c1) != len(TARGET_LEGS):
        defect.append(f"대상 구간 {len(c1)}/{len(TARGET_LEGS)}")
    rest = [t["delta"] for t in rows
            if not t["target"] and not t["clipped"] and not t["truncated"]]
    if not rest:
        defect.append("C2 대상 구간 0개")
    for name, v in list(c1.items()) + [(f"C2[{i}]", x) for i, x in enumerate(rest)] \
            + [("C3", calm_none.get("delta", _NAN))]:
        if not np.isfinite(v):
            defect.append(name)
    c1_min = min(c1.values()) if c1 else _NAN
    med = float(np.median(rest)) if rest else _NAN
    worst = float(np.min(rest)) if rest else _NAN
    c3 = calm_none.get("delta", _NAN)
    ok1 = bool(c1) and all(v >= C1_MIN_PP for v in c1.values())
    ok2 = bool(rest) and med >= C2_MEDIAN_MIN_PP and worst >= C2_WORST_MIN_PP
    ok3 = bool(np.isfinite(c3) and c3 >= C3_CALM_MIN_PP)
    return {"defect": defect, "c1": c1, "c1_min": c1_min, "c2_median": med,
            "c2_worst": worst, "c2_n": len(rest), "c3": c3,
            "ok1": ok1, "ok2": ok2, "ok3": ok3, "final": ok1 and ok2 and ok3}


# ══════════════════════════════════════════════════════════════════════════════
# 분석 본체 — 패널을 받아 판정까지 (main 과 selftest 가 같은 경로를 탄다)
# ══════════════════════════════════════════════════════════════════════════════
def _f(v, nd=1):
    return "n/a" if v is None or not np.isfinite(v) else f"{v:+.{nd}f}"


def run_core(close_df: pd.DataFrame, adj_df: pd.DataFrame, warmup: int, meta: dict,
             deep_records: list, targets=TARGET_LEGS) -> dict:
    """{"status": "ok"|"abort", "reason", "rows", "verdict"}. 게이트 전 bn 출력 없음."""
    idx = close_df.index
    n = len(idx)
    eval_i = int(warmup)
    episodes = d.find_episodes(close_df["SPY"])
    legs = d.episode_legs(episodes, eval_i, n)

    print(f"\n[STEP 2] 사건 목록 — SPY 원종가 고점 대비 {d.EPISODE_DD * 100:.0f}% 이상 "
          f"(룰과 독립 · 작업 A 와 같은 정의)")
    print(f"         평가 시작 {idx[eval_i].date()} (워밍업 {warmup}봉) · "
          f"캘린더 {idx[0].date()} ~ {idx[-1].date()} ({n}봉)")
    for k, ep in enumerate(episodes, 1):
        rec = idx[ep["recover_i"]].date() if ep["recover_i"] is not None else "미회복"
        print(f"   {k:>2}. 고점 {idx[ep['peak_i']].date()} → 바닥 {idx[ep['trough_i']].date()} "
              f"· 회복 {rec} · {ep['dd'] * 100:.1f}%")
    try:
        tgt = find_targets(legs, episodes, idx, targets)
    except ValueError as exc:
        print(f"[ABORT] {exc}")
        return {"status": "abort", "reason": str(exc), "rows": [], "verdict": None}
    for (kind, ym), L in tgt.items():
        print(f"   ▶ 대상 {kind}:{ym} = 사건 {L['ep']} {idx[L['lo']].date()} → {idx[L['hi']].date()}")

    print(f"\n[STEP 3] 연속 시뮬 {len(RULES)}룰 × {len(FILTERS)}필터 — 결과는 게이트 통과 후 출력")
    engines = {r: bt.RankEngine(close_df, rank_rule=r, warmup=warmup) for r in RULES}
    sims = {}
    for r in RULES:
        for flt in FILTERS:
            cfg = (rc.FIXED_FREQ, d.SWAP_MODE, rc.FIXED_SELLRULE, flt)
            with contextlib.redirect_stdout(io.StringIO()):
                m = bt.simulate(cfg, engines[r], close_df, adj_df, eval_i, n - 1, slots=d.SLOTS)
            sims[(r, flt)] = m or {}
    tbl = leg_table(sims, adj_df, idx, legs, tgt)
    mask = calm_mask(legs, eval_i, n)
    calm = {flt: calm_stats(sims, adj_df["SPY"], idx, mask, flt) for flt in FILTERS}

    ok, n_cmp, msgs = r0_check(tbl, deep_records, meta)
    print(f"\n[STEP 4] R0 재현 게이트 — {RULE_BASE} 행 vs 작업 A ({_DEEP_WORKSHEET}) · "
          f"허용 ±{R0_TOL_PP:.2f}%p · 대조 {n_cmp}행")
    if not ok:
        for s in msgs[:20]:
            print(f"   ✗ {s}")
        print("[ABORT] R0 불일치 — 자가 같은 자가 아니다(엔진 변경 · 데이터 창 · 유니버스).\n"
              f"        β중립({RULE_TEST}) 수치는 출력하지 않는다. 시트에 쓰지 않는다.")
        return {"status": "abort", "reason": "R0", "rows": [], "verdict": None}
    print(f"   ✅ {n_cmp}행 일치 — 12-0 은 작업 A 와 같은 경로를 탔다")

    v = verdict(tbl, calm[VERDICT_FILTER])
    if v["defect"]:
        print(f"[ABORT] 판정 수치에 n/a — 측정 결함: {v['defect']}\n"
              f"        β중립({RULE_TEST}) 수치는 출력하지 않는다. 시트에 쓰지 않는다. "
              "결함 수정 후 재실행은 허용 — §7 에 사유를 남긴다.")
        return {"status": "abort", "reason": "defect", "rows": [], "verdict": None}

    # ── 여기서부터 bn 수치를 출력한다 ──
    print("\n[STEP 5] 구간별 — Δ = 초과(bn) − 초과(12-0) · 판정은 필터 none 만")
    for L in legs:
        sub = [t for t in tbl if t["ep"] == L["ep"] and t["leg"] == L["leg"]]
        t0 = sub[0]
        tag = (f" ▶ 대상 {t0['target']}" if t0["target"] else "") \
            + (" · 절삭" if t0["clipped"] else "") + (" · 절단" if t0["truncated"] else "")
        print(f"\n── 사건 {L['ep']} · {'하락' if L['leg'] == 'drawdown' else '반등'} "
              f"{t0['lo_d'].date()} → {t0['hi_d'].date()} ({t0['bars']}봉){tag}")
        print(f"     {'룰':<12}{'필터':<8}{'SPY%':>8}{'포트%':>8}{'초과%p':>9}{'Δ%p':>8}{'MDD%':>8}{'교체':>6}")
        for t in sub:
            print(f"     {t['rule']:<12}{t['flt']:<8}{_f(t['spy']):>8}{_f(t['ret']):>8}"
                  f"{_f(t['ex']):>9}{_f(t['delta']):>8}{_f(t['mdd']):>8}{t['swaps']:>6}")
    print("\n── 평시 구간 (사건 구간 밖) · 연율")
    for flt in FILTERS:
        c = calm[flt]
        print(f"     {flt:<8} {c['bars']}봉 · SPY {_f(c['spy'])}% · {RULE_BASE} {_f(c[RULE_BASE])}% · "
              f"{RULE_TEST} {_f(c[RULE_TEST])}% · Δ {_f(c['delta'])}%p/년")

    print("\n[STEP 6] 판정 (사전 약정 · 필터 none)")
    print(f"   C1 완화   {' · '.join(f'{k} {_f(x)}%p' for k, x in v['c1'].items())} "
          f"(각 ≥ +{C1_MIN_PP:.1f}) → {'✅' if v['ok1'] else '❌'}")
    print(f"   C2 사건대가 {v['c2_n']}구간 중앙값 {_f(v['c2_median'])}%p (≥ {C2_MEDIAN_MIN_PP:+.1f}) · "
          f"최악 {_f(v['c2_worst'])}%p (≥ {C2_WORST_MIN_PP:+.1f}) → {'✅' if v['ok2'] else '❌'}")
    print(f"   C3 평시대가 {_f(v['c3'])}%p/년 (≥ {C3_CALM_MIN_PP:+.1f}) → {'✅' if v['ok3'] else '❌'}")
    final = "완화 관찰됨" if v["final"] else "미관찰 — 종료"
    print(f"   ⇒ {final}")

    run_date = datetime.now(_ET).strftime("%Y-%m-%d")
    tail = [meta.get("as_of", ""), meta.get("recv_from", ""), meta.get("recv_to", ""),
            meta.get("recv_bars", ""), d.DEEP_WINDOW_DAYS, meta.get("uhash", "")]
    rows = []
    for t in tbl:
        rows.append([run_date, t["rule"], t["flt"], t["ep"], t["leg"], t["target"],
                     str(t["lo_d"].date()), str(t["hi_d"].date()), t["bars"], t["clipped"],
                     t["truncated"], round(t["spy"], 2), round(t["ret"], 2), round(t["ex"], 2),
                     round(t["delta"], 2), round(t["mdd"], 2), t["swaps"],
                     str(t["meas_start"].date()), ""] + tail)
    for flt in FILTERS:
        c = calm[flt]
        for r in RULES:
            rows.append([run_date, r, flt, "", "calm", "", "", "", c["bars"], False, False,
                         round(c["spy"], 2), round(c[r], 2), round(c[r] - c["spy"], 2),
                         round(c["delta"], 2) if r == RULE_TEST else _NAN, _NAN, "", "", ""] + tail)
    for leg, val, okv in (("C1", v["c1_min"], v["ok1"]), ("C2_median", v["c2_median"], v["ok2"]),
                          ("C2_worst", v["c2_worst"], v["ok2"]), ("C3", v["c3"], v["ok3"]),
                          ("FINAL", _NAN, v["final"])):
        rows.append([run_date, "VERDICT", VERDICT_FILTER, "", leg, "", "", "", "", "", "",
                     _NAN, _NAN, _NAN, round(val, 2) if np.isfinite(val) else _NAN, _NAN, "", "",
                     (final if leg == "FINAL" else ("PASS" if okv else "FAIL"))] + tail)
    return {"status": "ok", "reason": "", "rows": rows, "verdict": v}


# ══════════════════════════════════════════════════════════════════════════════
# 시트 — 이 탭에만 쓴다. 작업 A 탭은 읽기만.
# ══════════════════════════════════════════════════════════════════════════════
def _open_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(GSPREAD_KEY_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    return bt._gs(gspread.authorize(creds).open, bt._SPREADSHEET_TITLE)


def _tab_records(sh, title: str):
    """(존재 여부, 데이터 행 dict 목록). 헤더 행 기준."""
    if title not in [w.title for w in bt._gs(sh.worksheets)]:
        return False, []
    vals = bt._gs(bt._gs(sh.worksheet, title).get_all_values) or []
    if not vals:
        return True, []
    head = vals[0]
    return True, [dict(zip(head, r)) for r in vals[1:] if any(str(c).strip() for c in r)]


def write_results(sh, rows: list) -> None:
    rows = [[d._cell(c) for c in r] for r in rows]
    ncol = len(_RESULT_COLS)
    last_col = chr(ord("A") + ncol - 1)
    try:
        if _RESULT_WORKSHEET in [w.title for w in bt._gs(sh.worksheets)]:
            ws = bt._gs(sh.worksheet, _RESULT_WORKSHEET)
        else:
            ws = bt._gs(sh.add_worksheet, title=_RESULT_WORKSHEET, rows=200, cols=ncol)
        bt._gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1",
               value_input_option="USER_ENTERED")
        bt._safe_append_rows(ws, rows, ncols=ncol)
        print(f"[OK] {_RESULT_WORKSHEET} 시트에 {len(rows)}행 기록")
    except Exception as exc:
        print(f"[WARN] 시트 기록 실패 — 콘솔 결과가 유일한 기록이다. 로그를 보관할 것: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    t0 = time.time()
    print("=" * 112)
    print(f"📐 β중립 12-0 참고 실행 — {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 112)
    print("판정이 아니다. 사전 약정은 이 파일 상단 · SATELLITE_MANDATE §4① 에 있다. 1회 전용.")

    if not GSPREAD_KEY_JSON:
        print("[ABORT] GSPREAD_KEY 없음 — R0 원본과 1회 가드를 읽을 수 없다. 실행하지 않는다.")
        return 1
    try:
        sh = _open_sheet()
        own_exists, own = _tab_records(sh, _RESULT_WORKSHEET)
        deep_exists, deep = _tab_records(sh, _DEEP_WORKSHEET)
    except Exception as exc:
        print(f"[ABORT] 시트 읽기 실패 — {exc}")
        return 1
    if own:
        print(f"[ABORT] {_RESULT_WORKSHEET} 에 이미 {len(own)}행 — 1회 전용이다. "
              "재시험은 사전 약정이 금지한다(§4① β중립).")
        return 1
    if not deep:
        print(f"[ABORT] {_DEEP_WORKSHEET} 탭이 비었거나 없다 — R0 대조 원본 없음.")
        return 1
    print(f"[STEP 0] 1회 가드 통과 · R0 원본 {_DEEP_WORKSHEET} {len(deep)}행")

    warmup = fx.mom_warmup_bars(RULES)
    if warmup != fx.mom_rule_need(RULE_BASE):
        print(f"[ABORT] 공통 워밍업 {warmup} != {RULE_BASE} {fx.mom_rule_need(RULE_BASE)} — "
              "시작일이 작업 A 와 달라진다(약정 전제 붕괴).")
        return 1
    as_of = date.fromisoformat(AS_OF)
    pool = fx.satellite_candidate_pool()
    universe = sorted({t for lst in pool.values() for t in lst})
    fetch_list = sorted(set(universe) | {"SPY"})
    saved = bt.WINDOW_DAYS_OVERRIDE
    try:
        # 작업 A 와 같은 창 지정. 프로세스 안에서만 유효, finally 에서 되돌린다(B4s).
        bt.WINDOW_DAYS_OVERRIDE = d.DEEP_WINDOW_DAYS
        print(f"\n[STEP 1] 후보 {len(universe)} + SPY = {len(fetch_list)}종목 × 2엔드포인트 · "
              f"워밍업 {warmup}봉 · AS_OF={AS_OF} (상수)")
        raw, adjmap, fallback, reasons, failed = bt._batch_fetch(
            fetch_list, bars=bt.HISTORY_BARS, as_of=as_of)
    finally:
        bt.WINDOW_DAYS_OVERRIDE = saved

    n = len(fetch_list)
    rate = (len(raw) / n) if n else 1.0
    print(f"[STEP 1] 원종가 {len(raw)}/{n} ({rate * 100:.1f}%) · 배당조정 {len(raw) - len(fallback)}/{n}")
    print("[STEP 1] " + fh.fmp_stats_line())
    if "SPY" not in raw:
        print("[ABORT] SPY 이력 확보 실패")
        return 1
    bi = raw["SPY"].index
    meta = {"recv_from": str(bi[0].date()), "recv_to": str(bi[-1].date()),
            "recv_bars": len(bi), "as_of": AS_OF, "uhash": bt.universe_hash(universe)}
    print(f"[STEP 1] 수신 창 — SPY {len(bi)}봉 {bi[0].date()} ~ {bi[-1].date()} · "
          f"유니버스 지문 {meta['uhash']}")
    if not d._depth_ok(len(bi)):
        print(f"[ABORT] SPY {len(bi)}봉 — 깊은 창이 아니다")
        return 1
    if rate < bt.MIN_FETCH_RATE:
        print(f"[ABORT] 페치 성공률 {rate * 100:.1f}% < {bt.MIN_FETCH_RATE * 100:.1f}%")
        return 1
    if "SPY" in fallback:
        print("[ABORT] SPY 배당조정 실패")
        return 1
    gaps = d._adj_floor_gaps(raw, adjmap, fallback)
    if gaps:
        print(f"[ABORT] 배당조정 계열이 원종가보다 늦게 시작하는 종목 {len(gaps)}개: "
              f"{[g[0] for g in gaps[:10]]}")
        return 1
    if fallback:
        print(f"[WARN] 배당조정 실패 → 원종가 대체 {len(fallback)}종목: {sorted(fallback)[:10]}")

    close_df, adj_df = bt.build_panels(raw, adjmap)
    res = run_core(close_df, adj_df, warmup, meta, deep)
    if res["status"] != "ok":
        print(f"⏱️ 소요 {time.time() - t0:.1f}초")
        return 1
    write_results(sh, res["rows"])
    if res["verdict"]["final"]:
        print("\n⇒ 완화 관찰됨. 다음은 종이 C반 설계 — 별도 작업·별도 약정. A/B 는 그대로다(§2·§5).")
    else:
        print("\n⇒ 미관찰. 사전 약정대로 β중립 모멘텀을 종료한다. 변형·문턱을 바꾼 재시험 금지.")
    print(f"⏱️ 소요 {time.time() - t0:.1f}초")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# 자체검증 (네트워크 불필요)
# ══════════════════════════════════════════════════════════════════════════════
def _mk_pair(n=600, seed=11, beta=1.3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    m = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, n))
    rm = np.r_[0.0, m[1:] / m[:-1] - 1]
    t = 50 * np.cumprod(1 + beta * rm + rng.normal(0.0002, 0.008, n))
    return idx, t, m


def _oracle_bn(dates, vals, mdates, mvals) -> float:
    """독립 구현(pandas 날짜 조인) — fx 식의 대조군. 여기 식이 바뀌면 둘이 갈라진다."""
    s = pd.Series(vals, index=pd.DatetimeIndex(dates)).iloc[-253:]
    mk = pd.Series(mvals, index=pd.DatetimeIndex(mdates)).reindex(s.index)
    a, m = s.to_numpy(), mk.to_numpy()
    w = np.arange(2, 253, 5)
    ra, rm = a[w][1:] / a[w][:-1] - 1, m[w][1:] / m[w][:-1] - 1
    b = np.cov(ra, rm, ddof=1)[0, 1] / np.var(rm, ddof=1)
    return (a[-1] / a[0] - 1) * 100 - b * (m[-1] / m[0] - 1) * 100


def _selftest() -> int:
    fails = []
    sc = lambda dt, v, md, mv: fx.mom_score_mkt(dt, v, md, mv, RULE_TEST)   # noqa: E731

    # ── 1. 사전 약정 상수 ────────────────────────────────────────────────
    if RULES != ("mom12_0", "mom12_0_bn") or RULE_BASE != fx.SATELLITE_RANK_RULE:
        fails.append(f"대상 룰 {RULES} — A반({fx.SATELLITE_RANK_RULE}) 대 β중립 한 쌍이어야 한다")
    if FILTERS is not d.FILTERS or VERDICT_FILTER != "none":
        fails.append("필터 축이 작업 A 와 갈라졌거나 판정 필터가 none 이 아니다")
    if not (C1_MIN_PP > 0 and C2_MEDIAN_MIN_PP < 0 and C2_WORST_MIN_PP < C2_MEDIAN_MIN_PP
            and C3_CALM_MIN_PP < 0 and 0 < R0_TOL_PP < 1):
        fails.append("문턱 부호/순서 오류")
    if _RESULT_WORKSHEET in (d._RESULT_WORKSHEET, rc._RESULT_WORKSHEET):
        fails.append("기록 탭이 작업 A 탭 또는 판정 탭과 같다")
    if fx.mom_warmup_bars(RULES) != 253 or fx.mom_warmup_bars() != 253:
        fails.append("워밍업이 253 이 아니다 — 시작일이 작업 A 와 달라진다")

    # ── 2. 룰 식 — 항등식 · 대조군 · 경계 ────────────────────────────────
    idx, t, m = _mk_pair()
    iv = idx.values
    if abs(sc(iv, m, iv, m)) > 1e-9 or abs(sc(iv, m * 3.7, iv, m)) > 1e-9:
        fails.append("종목 = 시장(β=1)인데 점수가 0 이 아니다")
    got, want = sc(iv, t, iv, m), _oracle_bn(iv, t, iv, m)
    if not (np.isfinite(got) and abs(got - want) < 1e-9):
        fails.append(f"대조군 불일치 {got} != {want}")
    beta = fx.mom_beta_weekly(t[-253:], m[-253:])
    rm12 = (m[-1] / m[-253] - 1) * 100
    if abs((fx.mom_score(t, "mom12_0") - beta * rm12) - got) > 1e-9:
        fails.append("bn != 12-0 − β̂·시장12-0 — 12-0 과의 차이가 β항 하나가 아니다")
    if not (np.isfinite(sc(iv[-253:], t[-253:], iv, m)) and not np.isfinite(sc(iv[-252:], t[-252:], iv, m))):
        fails.append("필요 봉수 경계 오류 — 253 에서 O, 252 에서 X 여야 한다")

    # ── 3. 날짜 정렬 — 결측이 있는 종목 ──────────────────────────────────
    keep = np.ones(len(t), bool)
    keep[len(t) - 100] = False
    g_d, g_v = iv[keep], t[keep]
    got_g, want_g = sc(g_d, g_v, iv, m), _oracle_bn(g_d, g_v, iv, m)
    if abs(got_g - want_g) > 1e-9:
        fails.append(f"결측 종목의 날짜 정렬 오류 {got_g} != {want_g}")
    pos_naive = _oracle_bn(iv[-253:], g_v[-253:], iv[-253:], m[-253:])   # 위치 정렬(오답)
    if abs(pos_naive - got_g) < 1e-6:
        fails.append("정렬 검사에 검정력이 없다 — 위치 정렬 오답과 구별되지 않는다")
    if np.isfinite(sc(g_d, g_v, iv[keep][:-1], m[keep][:-1])):
        fails.append("시장 계열에 끝 날짜가 없는데 점수가 나왔다")
    # 중간 날짜 — 끝 날짜만 빼면 `pos >= len` 쪽에서 걸려 정확 일치 검사가 시험되지
    # 않는다(2026-09-11 뮤테이션 M05 생존). 가까운 날로 대체하면 조용히 틀린 값이 나온다.
    hole = np.ones(len(iv), bool)
    hole[len(iv) - 50] = False
    if np.isfinite(sc(iv, t, iv[hole], m[hole])):
        fails.append("시장 계열에 중간 날짜가 없는데 점수가 나왔다(가까운 날로 대체 금지)")
    if np.isfinite(sc(iv[::-1], t[::-1], iv, m)):
        fails.append("역순 날짜를 조용히 받아들였다")

    # ── 4. 룩어헤드 · 호출 계약 ─────────────────────────────────────────
    cut = 450
    fut = np.r_[m[:cut], m[cut:] * 7.0]
    if sc(iv[:cut], t[:cut], iv, m) != sc(iv[:cut], t[:cut], iv, fut):
        fails.append("종목 마지막 날 이후 시장 값이 점수를 바꿨다 — 미래를 본다")
    try:
        fx.mom_score(t, RULE_TEST)
        fails.append("시장 룰을 단일 계열 mom_score 로 불러도 던지지 않는다")
    except TypeError:
        pass
    try:
        fx.mom_score_mkt(iv, t, iv, m, "mom12_0")
        fails.append("단일 계열 룰을 mom_score_mkt 로 불러도 던지지 않는다")
    except KeyError:
        pass
    if RULE_TEST in fx.MOM_RULES:
        fails.append("시장 룰이 MOM_RULES 에 들어갔다 — 동결 가드 S3·1b 가 깨진다")

    # ── 5. 엔진 경로 — 시장 룰 점수 = fx 직접 호출, 단일 룰 경로 불변 ──────
    idx_s, close_s, adj_s = rc._synthetic(n=900, seed=7)
    # 종목마다 결측 하루(SPY 는 온전) — 위치 정렬 뮤턴트는 여기서만 드러난다
    close_g = close_s.copy()
    close_g.loc[idx_s[500], [c for c in close_g.columns if c != "SPY"]] = np.nan
    e_bn = bt.RankEngine(close_g, rank_rule=RULE_TEST, warmup=253)
    e_12 = bt.RankEngine(close_g, rank_rule=RULE_BASE, warmup=253)
    dd_ = idx_s[600]
    top = e_bn.rank_at(dd_)[:1]
    if not top:
        fails.append("엔진이 시장 룰로 랭킹을 못 만들었다")
    else:
        tk = top[0]["ticker"]
        s = close_g[tk].dropna().loc[:dd_]
        spy = close_g["SPY"].dropna()
        direct = fx.mom_score_mkt(s.index.values, s.to_numpy(), spy.index.values,
                                  spy.to_numpy(), RULE_TEST)
        if abs(direct - top[0]["score"]) > 1e-9:
            fails.append("엔진 점수 != fx 직접 호출 — 엔진이 식을 다시 쓰고 있다")
    r12 = e_12.rank_at(dd_)
    if r12 and abs(r12[0]["score"] - fx.mom_score(close_g[r12[0]["ticker"]].dropna().loc[:dd_].to_numpy(),
                                                   "mom12_0")) > 1e-9:
        fails.append("단일 계열 룰 엔진 경로가 바뀌었다")
    try:
        bt.RankEngine(close_g.drop(columns=["SPY"]), rank_rule=RULE_TEST, warmup=253)
        fails.append("SPY 없는 패널에서 시장 룰 엔진이 조용히 생성됐다")
    except KeyError:
        pass

    # ── 6. 평시 구간 — 사건 구간과 평시가 전 기간을 정확히 나눈다 ─────────
    legs_ = [{"lo": 300, "hi": 350}, {"lo": 350, "hi": 420}, {"lo": 600, "hi": 640}]
    mk = calm_mask(legs_, 253, 900)
    r = _bar_returns(adj_s["SPY"], idx_s)
    tot = np.prod(1 + r[254:])
    parts = np.prod(1 + r[mk]) * np.prod([np.prod(1 + r[L["lo"] + 1:L["hi"] + 1]) for L in legs_])
    if abs(tot - parts) > 1e-9 or mk[253] or mk[301] or not mk[300] or not mk[421]:
        fails.append("평시 마스크가 전 기간을 정확히 나누지 않는다 (경계 (lo, hi])")

    # ── 7. 판정 함수 — 경계 · 뒤집기 · 결함 ─────────────────────────────
    def _tb(d1, d2, rest, clip_bad=-50.0):
        mk_ = lambda tg, dl, cl=False: {"rule": RULE_TEST, "flt": "none", "target": tg,   # noqa: E731
                                        "delta": dl, "clipped": cl, "truncated": False}
        return ([mk_("rebound:2009-03", d1), mk_("drawdown:2018-12", d2), mk_("", clip_bad, True)]
                + [mk_("", x) for x in rest]
                + [dict(mk_("", -99.0), flt="no_new")])        # no_new 는 판정에 안 들어간다
    ok_v = verdict(_tb(5.0, 7.0, [0.0, -1.0, -3.0, 2.0]), {"delta": -2.0})
    if not ok_v["final"] or ok_v["defect"]:
        fails.append(f"경계값(=+5.0 · 중앙값 −0.5 · 평시 −2.0)이 통과하지 않는다: {ok_v}")
    for lbl, tb_, cm in (("C1 한쪽 미달", _tb(4.99, 9.0, [0.0]), {"delta": 0.0}),
                         ("C2 중앙값", _tb(6, 6, [-2.5, -2.1, -3.0]), {"delta": 0.0}),
                         ("C2 최악", _tb(6, 6, [0.0, 1.0, -10.01]), {"delta": 0.0}),
                         ("C3", _tb(6, 6, [0.0]), {"delta": -2.01})):
        if verdict(tb_, cm)["final"]:
            fails.append(f"{lbl} 가 떨어져야 하는데 통과했다")
    if not verdict(_tb(6, 6, [0.0]), {"delta": _NAN})["defect"]:
        fails.append("평시 Δ 가 n/a 인데 결함으로 안 잡힌다")
    if not verdict(_tb(6, 6, []), {"delta": 0.0})["defect"]:
        fails.append("C2 대상 0개인데 결함으로 안 잡힌다")

    # ── 8. 합성 패널 전 경로 — R0 통과/실패 · 출력 순서 ───────────────────
    sv = d._piecewise([100, 130, 105, 140, 118, 150], seg=130)[:900]
    sv = np.concatenate([np.linspace(90, 100, 900 - len(sv)), sv]) if len(sv) < 900 else sv
    close_s["SPY"], adj_s["SPY"] = sv, sv
    eps = d.find_episodes(close_s["SPY"])
    lg = d.episode_legs(eps, 253, 900)
    reb = [L for L in lg if L["leg"] == "rebound" and not L["clipped"]]
    dwn = [L for L in lg if L["leg"] == "drawdown" and not L["clipped"]]
    if not (reb and dwn):
        fails.append("합성 패널에 대상으로 쓸 구간이 없다 — 전 경로 검사 무효")
    else:
        tg = (("rebound", str(idx_s[eps[reb[0]["ep"] - 1]["trough_i"]].date())[:7]),
              ("drawdown", str(idx_s[eps[dwn[-1]["ep"] - 1]["trough_i"]].date())[:7]))
        meta = {"uhash": "TEST", "recv_from": "a", "recv_to": "b", "as_of": AS_OF}
        with contextlib.redirect_stdout(io.StringIO()) as b1:
            res0 = run_core(close_s, adj_s, 253, meta, [], targets=tg)
        out0 = b1.getvalue()
        if res0["status"] != "abort" or res0["rows"]:
            fails.append("R0 원본 없이도 진행했다")
        if RULE_TEST in out0.split("[STEP 4]")[-1] and "수치는 출력하지 않는다" not in out0:
            fails.append("R0 실패인데 bn 수치가 찍혔다")
        if any(ln.strip().startswith(RULE_TEST) for ln in out0.splitlines()):
            fails.append("R0 실패 전 bn 행이 출력됐다 — 게이트 전 출력 금지 위반")
        # 원본 = 같은 엔진으로 만든 12-0 행 (시트처럼 문자열로)
        eng = {r_: bt.RankEngine(close_s, rank_rule=r_, warmup=253) for r_ in RULES}
        sims = {}
        for r_ in RULES:
            for flt in FILTERS:
                with contextlib.redirect_stdout(io.StringIO()):
                    sims[(r_, flt)] = bt.simulate((rc.FIXED_FREQ, d.SWAP_MODE, rc.FIXED_SELLRULE, flt),
                                                  eng[r_], close_s, adj_s, 253, 899, slots=d.SLOTS) or {}
        tgt_ = find_targets(lg, eps, idx_s, tg)
        tbl = leg_table(sims, adj_s, idx_s, lg, tgt_)
        src = [{"Run_Date": "2026-09-10", "Rule": RULE_BASE, "MktFilter": x["flt"],
                "Episode_Idx": str(x["ep"]), "Leg": x["leg"], "Start": str(x["lo_d"].date()),
                "End": str(x["hi_d"].date()), "Port_Ret_Pct": f"{x['ret']:.2f}",
                "Excess_pp": f"{x['ex']:.2f}", "Port_MDD_Pct": f"{x['mdd']:.2f}",
                "Swaps": str(x["swaps"]), "Universe_Hash": "TEST", "Recv_From": "a",
                "Recv_To": "b", "Window_Days": str(d.DEEP_WINDOW_DAYS)}
               for x in tbl if x["rule"] == RULE_BASE]
        global R0_MIN_ROWS
        _saved_min, R0_MIN_ROWS = R0_MIN_ROWS, 1          # 합성 패널은 구간이 적다
        try:
            ok_r0, n_r0, _ = r0_check(tbl, src, meta)
            if not ok_r0:
                fails.append("같은 엔진으로 만든 원본인데 R0 가 실패한다")
            bad = [dict(x) for x in src]
            bad[0]["Excess_pp"] = f"{_num(bad[0]['Excess_pp']) + 0.2:.2f}"
            if r0_check(tbl, bad, meta)[0]:
                fails.append("원본을 0.2%p 틀어도 R0 가 통과한다")
            if r0_check(tbl, src, dict(meta, uhash="OTHER"))[0]:
                fails.append("유니버스 지문이 달라도 R0 가 통과한다")
            blank = [dict(x) for x in src]
            blank[0]["Excess_pp"] = ""
            ok_b, n_b, _ = r0_check(tbl, blank, meta)
            if not ok_b or n_b != n_r0 - 1:
                fails.append("작업 A 의 n/a 칸을 건너뛰지 않는다")
            later = [dict(x, Run_Date="2026-09-20", Excess_pp="999") for x in src]
            if not r0_check(tbl, src + later, meta)[0]:
                fails.append("가장 이른 Run_Date 만 쓰지 않는다 — 나중 기록이 원본을 덮었다")
            with contextlib.redirect_stdout(io.StringIO()) as b2:
                res1 = run_core(close_s, adj_s, 253, meta, src, targets=tg)
        finally:
            R0_MIN_ROWS = _saved_min
        out1 = b2.getvalue()
        if res1["status"] == "ok":
            if any(len(rw) != len(_RESULT_COLS) for rw in res1["rows"]):
                fails.append(f"행 폭 != 헤더 폭({len(_RESULT_COLS)})")
            if out1.find("[STEP 4]") > out1.find("[STEP 5]"):
                fails.append("R0 보다 구간 결과가 먼저 찍혔다")
            if sum(1 for rw in res1["rows"] if rw[1] == "VERDICT") != 5:
                fails.append("판정 행 5개(C1·C2 중앙값·C2 최악·C3·FINAL)가 아니다")
        elif res1["reason"] != "defect":
            fails.append(f"합성 전 경로가 {res1['reason']} 로 멈췄다")
        for bad_w in ("샤프", "승 /", "창 중"):
            if bad_w in out1:
                fails.append(f"판정성 출력 '{bad_w}' 가 찍힘 — 사전 약정 위반")

    if fails:
        print("❌ 자체검증 실패:")
        for f_ in fails:
            print(f"   - {f_}")
        return 1
    print("✅ 전 항목 통과 (약정상수·β=1항등·대조군·β항분해·필요봉수경계·결측정렬·정렬검정력·"
          "시장끝결측거부·시장중간결측거부·역순거부·룩어헤드·호출계약·레지스트리분리·엔진위임·단일룰불변·SPY필수·"
          "평시분할·판정경계·판정뒤집기·결함검출·R0통과·R0오차·R0지문·R0 n/a·R0첫실행·"
          "게이트전출력금지·출력순서·행폭)")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else main())
