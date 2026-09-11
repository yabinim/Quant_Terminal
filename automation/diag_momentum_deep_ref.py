#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_momentum_deep_ref.py — 깊은 창 참고 실행 (판정 아님 · 읽기 전용 진단)

  실행: python automation/diag_momentum_deep_ref.py
        python automation/diag_momentum_deep_ref.py --selftest   # 네트워크 불필요

아무것도 수정하지 않는다. Google Sheets `Momentum_Rule_Deep` 탭에 결과 행만
append 한다. `Momentum_Rule_Compare`(§4① 판정 기록)에는 **쓰지 않는다.**


═══════════════════════════════════════════════════════════════════════════════
깊은 창 참고 실행 — 사전 약정 (2026-09-10 확정, 결과 보기 전)
═══════════════════════════════════════════════════════════════════════════════
답하는 질문은 하나다: "각 룰이 하락·반등 구간에서 어떻게 깨지나."
"어느 룰이 이기나"에는 답하지 않는다 — T6(후보 풀 편향)은 창을 늘려도 남고,
§5 는 백테스트 비교를 판정 근거에서 제외한다. 그래서 승수 집계와 전체 기간
샤프 순위는 **출력하지 않는다.** 찍히면 읽힌다.

대상   blend · mom12_0 · mom12_1 (= VERDICT_RULES) × top5 균등 · swap · weekly
       × 시장 필터 none(룰 자체) / no_new(§3 근사 — "비중 축소"는 미모델)
       위험조정 룰(_ra)은 제외: MOM_VOL_BARS=252 의 근거가 1,255봉 창에 묶여
       있어, 넣으면 창 변경과 룰 변경이 섞인다.
사건   SPY 원종가(§3 필터와 같은 계열) 직전 고점 대비 −12% 이상.
       새 고점 회복 전의 재하락은 같은 사건. 하락 구간 = 고점~바닥,
       반등 구간 = 바닥 후 126봉. 평가 시작 전 고점은 절삭 표시.
       사건 목록은 SPY 만으로 정해지므로 룰 결과와 독립이다 — 그래서 지금 확정한다.
결과로 하지 않는 것
       A/B 룰 변경(§2) · 트리거 숫자 변경(§5). 결과가 바꿀 수 있는 것은
       §2 각주에 "알려진 약점"을 적는 것뿐. 대응(필터 조정 등)은 별도 작업 ·
       별도 사전 약정.

−12% 를 고른 근거(지수 기억치, 결과와 무관): −20% 는 2018(−19.8)·2011(−19.4)·
2025(−18.9)가 1%p 안쪽으로 빠지고, −15% 는 2015-16(−14.2)이 0.8%p 로 빠진다.
−12% 는 −14.2 ↔ −10.3(2023) 사이로 위아래 ≈2%p 가 비어 있다.

⚠️ 이 블록의 숫자는 SATELLITE_MANDATE §4① 과 diag_satellite_mandate J 그룹이
   서로 묶는다. 여기만 고치면 드리프트 가드가 빨간불이 된다 — 그게 목적이다.


설계 메모 (구현 선택의 이유)
─────────────────────────────
 1) **창 모양.** 판정 파일의 워크포워드(252봉 × 6창 균등 배치)를 깊은 창에
    쓰면 1년 창 6개가 2.6~3.5년 간격으로 흩어져 하락장에 걸릴지가 우연이 된다.
    그래서 사건 기준으로 자른다.
 2) **연속 시뮬 → 곡선 절단.** 창마다 재시작하면 "고점 직후에 새로 산
    포트폴리오"를 잰다. 급락(2020 · 23거래일)에서 중요한 것은 "고점에 무엇을
    들고 있었나"이고, 그건 경로 의존이다. 룰×필터마다 전 기간 1회 돌리고
    `bt.simulate` 가 돌려주는 `curve`·`log` 를 구간별로 자른다.
 3) **창 깊이.** bt.WINDOW_DAYS_OVERRIDE = DEEP_WINDOW_DAYS 로 **이 프로세스
    안에서만** 지정한다. 판정 경로의 WINDOW_DAYS_PIN(1826)은 건드리지 않는다.
    7400일은 FMP 단일 호출 상한(롤링 5,000 레코드 ≈ 19.8년)을 **일부러 넘긴**
    요청이다 — 넘기면 FMP 가 최근 5,000봉을 준다(2026-09-10 실측: `to` 를 밀면
    창이 따라 이동, 바닥은 하루씩 롤링). 그래서 수신 봉수를 기록하고, 5,000 이면
    "바닥은 FMP 상한이 정했다"고 밝힌다.
 4) **배당조정 창 게이트.** 5,000 레코드 상한은 `full` 에서만 실측됐다.
    dividend-adjusted 가 더 짧게 오면 build_panels 가 초반을 NaN 으로 채우고,
    simulate 의 sell() 은 NaN 가격에서 **조용히 아무것도 안 한다** — 성과가
    에러 없이 오염된다. 원종가보다 7일 넘게 늦게 시작하는 배당조정 계열이
    하나라도 있으면 시뮬 전에 중단한다.
 5) 2008 가시성은 롤링 바닥 때문에 **매일 하루씩 닫힌다.** 오늘 기준 평가 시작은
    ≈2007-11 이라 2008 하락의 첫 한 달가량이 잘린다(Clipped=True).
"""
from __future__ import annotations

import ast
import contextlib
import io
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

import fmp_extras as fx                       # noqa: E402  — 룰·후보 풀 SSOT
import fmp_http as fh                         # noqa: E402  — 레이트리밋 SSOT
import diag_satellite_backtest as bt          # noqa: E402  — 엔진 (재구현 금지)
import diag_momentum_rule_compare as rc       # noqa: E402  — 룰 축·고정 축 SSOT

_KST = pytz.timezone("Asia/Seoul")
_ET = pytz.timezone("America/New_York")

GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")
_RESULT_WORKSHEET = "Momentum_Rule_Deep"

# ── 사전 약정 상수 (2026-09-10 · 결과 보기 전) ───────────────────────────────
EPISODE_DD = -0.12            # SPY 원종가 직전 고점 대비
REBOUND_BARS = 126            # 바닥 후 반등 구간 길이
RULES = rc.VERDICT_RULES      # blend · mom12_1 · mom12_0 — 동결 튜플을 그대로 쓴다
FILTERS = ("none", "no_new")  # 룰 자체 / §3 근사
DEEP_WINDOW_DAYS = 7400       # FMP 5,000 레코드 상한을 일부러 넘긴 요청 (설계 메모 3)
FMP_RECORD_CAP = 5000         # 2026-09-10 실측 — 로그 해석에만 쓴다
ADJ_FLOOR_TOL_DAYS = 7        # 배당조정 바닥이 원종가보다 이만큼 넘게 늦으면 중단
MIN_DEEP_FRAC = 0.9           # SPY 수신 봉수가 상한의 90% 미만이면 '깊은 창'이 아니다 → 중단
#   약정은 "FMP 단일 호출 상한까지(≈19.8년)"다. 그보다 얕게 왔다면 override 가 안
#   먹었거나(옛 bt) FMP 가 상한을 줄인 것이다. 어느 쪽이든 약정과 다른 자로 잰
#   결과이므로 시트에 쓰지 않는다 — 자를 조용히 바꾸는 것이 이 작업이 막으려는 것이다.

# 변형은 판정 파일의 현행 기준 짝(BASE_VARIANT = top5_eq)을 그대로 가져온다.
_BASE = next(v for v in rc.VARIANTS if v[0] == rc.BASE_VARIANT)
VARIANT_NAME, SLOTS, SWAP_MODE, _WKEY = _BASE

_RESULT_COLS = [
    "Run_Date", "Rule", "MktFilter", "Episode_Idx", "Leg", "Start", "End", "Bars",
    "Clipped", "Truncated", "Episode_DD_Pct",
    "SPY_Ret_Pct", "Port_Ret_Pct", "Excess_pp", "Port_MDD_Pct", "Swaps",
    "RiskOff_Weeks", "Sectors_Avail", "Cands_Avail",
    "Recv_From", "Recv_To", "Recv_Bars", "Window_Days", "As_Of", "Universe_Hash",
]


# ══════════════════════════════════════════════════════════════════════════════
# 사건 추출 — SPY 만 본다 (룰 결과와 독립)
# ══════════════════════════════════════════════════════════════════════════════
def find_episodes(spy: pd.Series, dd: float = EPISODE_DD) -> list:
    """직전 고점 대비 dd 이하로 내려간 사건 → [{peak_i, trough_i, recover_i, dd}, ...].

    위치는 **입력 시리즈의 정수 위치**다(build_panels 캘린더 = SPY 캘린더).
    사건은 고점에서 시작해 종가가 그 고점을 **회복(≥)** 할 때 끝난다. 그 사이의
    반등·재하락은 전부 같은 사건이고, 바닥은 그 구간 전체의 최저점이다.
    회복하지 못한 채 데이터가 끝나면 recover_i=None.
    """
    vals = pd.to_numeric(spy, errors="coerce").ffill().to_numpy(dtype=float)
    n = len(vals)
    out = []
    peak_i, i = 0, 0
    while i < n:
        if not np.isfinite(vals[i]):
            i += 1
            continue
        if not np.isfinite(vals[peak_i]) or vals[i] >= vals[peak_i]:
            peak_i = i
            i += 1
            continue
        if vals[i] / vals[peak_i] - 1.0 <= dd:
            j = i
            while j < n and vals[j] < vals[peak_i]:
                j += 1
            seg = vals[peak_i:j]
            trough_i = peak_i + int(np.nanargmin(seg))
            out.append({"peak_i": peak_i, "trough_i": trough_i,
                        "recover_i": (j if j < n else None),
                        "dd": float(vals[trough_i] / vals[peak_i] - 1.0)})
            if j >= n:
                break
            peak_i, i = j, j
            continue
        i += 1
    return out


def episode_legs(episodes: list, eval_start_i: int, n_bars: int,
                 rebound_bars: int = REBOUND_BARS) -> list:
    """사건 → 평가 가능한 구간들. 평가 시작 전 부분은 잘라내고 표시한다.

    하락 구간 [max(고점, 평가시작), 바닥], 반등 구간 [max(바닥, 평가시작),
    min(바닥 + rebound_bars, 끝)]. 길이가 0 이하가 되는 구간은 버린다 — 예컨대
    바닥까지 전부 워밍업 안에 있는 사건의 하락 구간.
    """
    last = n_bars - 1
    legs = []
    for k, ep in enumerate(episodes, 1):
        p, t = ep["peak_i"], ep["trough_i"]
        lo, hi = max(p, eval_start_i), t
        if hi > lo:
            legs.append({"ep": k, "leg": "drawdown", "lo": lo, "hi": hi,
                         "clipped": p < eval_start_i, "truncated": False,
                         "dd": ep["dd"]})
        lo, hi = max(t, eval_start_i), min(t + int(rebound_bars), last)
        if hi > lo:
            legs.append({"ep": k, "leg": "rebound", "lo": lo, "hi": hi,
                         "clipped": t < eval_start_i,
                         "truncated": t + int(rebound_bars) > last,
                         "dd": ep["dd"]})
    return legs


# ══════════════════════════════════════════════════════════════════════════════
# 구간 측정 — 연속 곡선을 자른다
# ══════════════════════════════════════════════════════════════════════════════
def _asof(s: pd.Series, d) -> float:
    sub = s.loc[:d].dropna()
    return float(sub.iloc[-1]) if len(sub) else float("nan")


def leg_metrics(curve: pd.Series, log: list, lo_d, hi_d) -> dict:
    """[lo_d, hi_d] 구간의 수익률·MDD·교체 수. 교체는 (lo_d, hi_d] 체결분만 센다.

    lo_d 당일 체결은 구간 **진입 전** 보유를 만든 거래라 세지 않는다.
    곡선이 lo_d 이후에 시작하면(첫 체결 전) 값이 없으므로 nan.
    """
    v0, v1 = _asof(curve, lo_d), _asof(curve, hi_d)
    ret = (v1 / v0 - 1.0) * 100.0 if np.isfinite(v0) and v0 > 0 else float("nan")
    sub = curve.loc[lo_d:hi_d].dropna()
    mdd = float((sub / sub.cummax() - 1.0).min() * 100.0) if len(sub) > 1 else float("nan")
    swaps = sum(1 for r in (log or [])
                if lo_d < pd.Timestamp(r["exec"]) <= hi_d and r.get("sold"))
    return {"ret": ret, "mdd": mdd, "swaps": swaps}


def risk_off_weeks(spy_close: pd.Series, idx: pd.DatetimeIndex, lo_d, hi_d) -> int:
    """구간 안 주간 신호일 중 §3 필터가 위험 구간(SPY < 200MA)이던 주 수 — 룰과 무관."""
    return sum(1 for d in bt.signal_dates(idx, rc.FIXED_FREQ)
               if lo_d < d <= hi_d and not bt.market_risk_on(spy_close, d))


def availability(eng, d) -> tuple:
    """(챔피언이 선 섹터 수, 랭킹 가능한 후보 수) @ d — 워밍업은 공통값이라 룰과 무관."""
    key = np.datetime64(pd.Timestamp(d))
    cands = sum(1 for idx_v, _ in eng.series.values()
                if int(np.searchsorted(idx_v, key, side="right")) >= eng.warmup)
    return len(eng.rank_at(d)), cands


# ══════════════════════════════════════════════════════════════════════════════
# 분석 본체 — 패널을 받아 행을 만든다 (main 과 selftest 가 같은 경로를 탄다)
# ══════════════════════════════════════════════════════════════════════════════
def _f(v, nd=1):
    return "n/a" if v is None or not np.isfinite(v) else f"{v:+.{nd}f}"


def analyze(close_df: pd.DataFrame, adj_df: pd.DataFrame, warmup: int,
            meta: dict, verbose: bool = True) -> list:
    """사건 목록 출력 → 룰×필터 연속 시뮬 → 구간 표 → 시트 행."""
    idx = close_df.index
    n = len(idx)
    eval_i = int(warmup)
    spy_c = close_df["SPY"]
    episodes = find_episodes(spy_c)
    legs = episode_legs(episodes, eval_i, n)

    say = print if verbose else (lambda *a, **k: None)
    say(f"\n[STEP 2] 사건 목록 — SPY 원종가 고점 대비 {EPISODE_DD * 100:.0f}% 이상 "
        f"(룰 결과를 보기 **전에** 출력한다)")
    say(f"         평가 시작 {idx[eval_i].date()} (워밍업 {warmup}봉) · 캘린더 "
        f"{idx[0].date()} ~ {idx[-1].date()} ({n}봉)")
    for k, ep in enumerate(episodes, 1):
        rec = idx[ep["recover_i"]].date() if ep["recover_i"] is not None else "미회복"
        tag = " · ⚠️ 평가 시작 전 고점(절삭)" if ep["peak_i"] < eval_i else ""
        say(f"   {k:>2}. 고점 {idx[ep['peak_i']].date()} → 바닥 {idx[ep['trough_i']].date()} "
            f"({ep['trough_i'] - ep['peak_i']}봉) · 회복 {rec} · {ep['dd'] * 100:.1f}%{tag}")
    if not episodes:
        say("   (사건 없음)")

    engines = {r: bt.RankEngine(close_df, rank_rule=r, warmup=warmup) for r in RULES}
    sims = {}
    say(f"\n[STEP 3] 연속 시뮬 {len(RULES)}룰 × {len(FILTERS)}필터 = "
        f"{len(RULES) * len(FILTERS)}회 · {VARIANT_NAME}({SLOTS}슬롯·{SWAP_MODE}·"
        f"{rc.FIXED_FREQ}) · [{idx[eval_i].date()} ~ {idx[-1].date()}]")
    for r in RULES:
        for flt in FILTERS:
            cfg = (rc.FIXED_FREQ, SWAP_MODE, rc.FIXED_SELLRULE, flt)
            m = bt.simulate(cfg, engines[r], close_df, adj_df, eval_i, n - 1, slots=SLOTS)
            sims[(r, flt)] = m or {}
            if not m:
                say(f"[WARN] {r}/{flt} 시뮬 결과 없음")

    say("\n[STEP 4] 구간별 — 판정 아님 · 승수·전체 기간 순위는 출력하지 않는다(사전 약정)")
    run_date = datetime.now(_ET).strftime("%Y-%m-%d")
    rows = []
    eng0 = engines[RULES[0]]
    for L in legs:
        lo_d, hi_d = idx[L["lo"]], idx[L["hi"]]
        spy_ret = (_asof(adj_df["SPY"], hi_d) / _asof(adj_df["SPY"], lo_d) - 1.0) * 100.0
        roff = risk_off_weeks(spy_c, idx, lo_d, hi_d)
        sec, cands = availability(eng0, lo_d)
        flags = (" · 절삭" if L["clipped"] else "") + (" · 데이터 끝 절단" if L["truncated"] else "")
        sec_warn = f" ⚠️ 섹터 {sec}/{len(eng0.pool)}" if sec < len(eng0.pool) else ""
        say(f"\n── 사건 {L['ep']} · {'하락' if L['leg'] == 'drawdown' else '반등'} "
            f"{lo_d.date()} → {hi_d.date()} ({L['hi'] - L['lo']}봉) · SPY {_f(spy_ret)}% · "
            f"섹터 {sec}/{len(eng0.pool)} · 후보 {cands} · risk-off {roff}주{flags}{sec_warn}")
        say(f"     {'룰':<9}{'필터':<8}{'포트%':>8}{'초과%p':>9}{'MDD%':>8}{'교체':>6}")
        for r in RULES:
            for flt in FILTERS:
                m = sims[(r, flt)]
                lm = (leg_metrics(m["curve"], m.get("log"), lo_d, hi_d) if m
                      else {"ret": float("nan"), "mdd": float("nan"), "swaps": 0})
                ex = lm["ret"] - spy_ret
                say(f"     {r:<9}{flt:<8}{_f(lm['ret']):>8}{_f(ex):>9}"
                    f"{_f(lm['mdd']):>8}{lm['swaps']:>6}")
                rows.append([
                    run_date, r, flt, L["ep"], L["leg"], str(lo_d.date()), str(hi_d.date()),
                    L["hi"] - L["lo"], L["clipped"], L["truncated"], round(L["dd"] * 100, 2),
                    round(spy_ret, 2), round(lm["ret"], 2), round(ex, 2), round(lm["mdd"], 2),
                    lm["swaps"], roff, sec, cands,
                    meta.get("recv_from", ""), meta.get("recv_to", ""),
                    meta.get("recv_bars", ""), DEEP_WINDOW_DAYS,
                    meta.get("as_of", ""), meta.get("uhash", ""),
                ])
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# 시트 기록 — 전용 탭. 판정 탭에는 쓰지 않는다.
# ══════════════════════════════════════════════════════════════════════════════
def _cell(v):
    """시트 셀 정규화 — NaN/inf 는 공란, numpy 스칼라는 파이썬 값.

    NaN 을 그대로 보내면 JSON 인코딩이 `NaN` 을 내고 Sheets API 가 요청 전체를
    거절한다. 그 실패는 [WARN] 한 줄로 끝나 결과 전체가 기록되지 않는다.
    """
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and not np.isfinite(v):
        return ""
    return v


def write_results(rows: list) -> None:
    rows = [[_cell(c) for c in r] for r in rows]
    if not GSPREAD_KEY_JSON:
        print("\n[INFO] GSPREAD_KEY 없음 — 시트 기록 생략(콘솔 출력만).")
        return
    if not rows:
        print("\n[INFO] 기록할 행 없음.")
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
            ws = bt._gs(sh.add_worksheet, title=_RESULT_WORKSHEET, rows=1000, cols=ncol)
            bt._gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1",
                   value_input_option="USER_ENTERED")
        bt._safe_append_rows(ws, rows, ncols=ncol)
        print(f"[OK] {_RESULT_WORKSHEET} 시트에 {len(rows)}행 기록")
    except Exception as exc:
        print(f"[WARN] 시트 기록 실패(콘솔 결과는 유효): {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════
def _adj_floor_gaps(raw: dict, adjmap: dict, fallback: list) -> list:
    """배당조정 계열이 원종가보다 늦게 시작하는 종목 [(tk, raw0, adj0, 일수)]."""
    out = []
    for tk, s in raw.items():
        if tk in fallback or tk not in adjmap or s is None or not len(s):
            continue
        a = adjmap[tk]
        if a is None or not len(a):
            continue
        gap = (a.index[0] - s.index[0]).days
        if gap > ADJ_FLOOR_TOL_DAYS:
            out.append((tk, s.index[0].date(), a.index[0].date(), gap))
    return out


def _depth_ok(recv_bars: int) -> bool:
    return int(recv_bars) >= int(FMP_RECORD_CAP * MIN_DEEP_FRAC)


def main() -> int:
    t0 = time.time()
    print("=" * 112)
    print(f"📐 깊은 창 참고 실행 — {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 112)
    print("판정이 아니다. 사전 약정은 이 파일 상단 · SATELLITE_MANDATE §4① 에 있다.")

    warmup = fx.mom_warmup_bars(RULES)
    pool = fx.satellite_candidate_pool()
    universe = sorted({t for lst in pool.values() for t in lst})
    fetch_list = sorted(set(universe) | {"SPY"})
    _as_of = bt._env_as_of()
    saved = bt.WINDOW_DAYS_OVERRIDE
    try:
        # ⚠️ 이 대입이 이 파일의 존재 이유다. 프로세스 안에서만 유효하고 finally 에서
        #    되돌린다. 판정 파일은 이 값을 쓰지 않는다(diag_fmp_ssot B4s).
        bt.WINDOW_DAYS_OVERRIDE = DEEP_WINDOW_DAYS
        print(f"\n[STEP 1] 후보 풀 {len(universe)}개 + SPY = {len(fetch_list)}종목 × 2엔드포인트 · "
              f"공통 워밍업 {warmup}봉")
        print(f"[STEP 1] 요청 창 — {DEEP_WINDOW_DAYS}달력일 "
              f"({fx.hist_range_params(DEEP_WINDOW_DAYS, today=_as_of).lstrip('&').replace('&', ' ')}) · "
              + (f"AS_OF={_as_of} (고정)" if _as_of else "AS_OF=미지정 → 오늘 기준"))
        # bars 는 계약상 필수 키워드라 넘기지만, override 경로에서는 창을 정하지 않는다.
        raw, adjmap, fallback, reasons, failed = bt._batch_fetch(
            fetch_list, bars=bt.HISTORY_BARS, as_of=_as_of)
    finally:
        bt.WINDOW_DAYS_OVERRIDE = saved

    n = len(fetch_list)
    rate = (len(raw) / n) if n else 1.0
    print(f"[STEP 1] 원종가 {len(raw)}/{n} ({rate * 100:.1f}%) · 배당조정 {len(raw) - len(fallback)}/{n}")
    print("[STEP 1] " + fh.fmp_stats_line())
    if "SPY" not in raw:
        print("[ERROR] SPY 이력 확보 실패 — 중단")
        return 1
    bi = raw["SPY"].index
    meta = {"recv_from": str(bi[0].date()), "recv_to": str(bi[-1].date()),
            "recv_bars": len(bi), "as_of": str(_as_of or "")}
    print(f"[STEP 1] 수신 창 — SPY 원종가 {len(bi)}봉 {bi[0].date()} ~ {bi[-1].date()}"
          + (f" · 배당조정 {len(adjmap['SPY'])}봉 {adjmap['SPY'].index[0].date()} ~"
             if "SPY" in adjmap else ""))
    if len(bi) >= FMP_RECORD_CAP:
        print(f"[STEP 1] ⓘ SPY {len(bi)}봉 = FMP 단일 호출 상한({FMP_RECORD_CAP}) — "
              "창 바닥은 요청이 아니라 FMP 상한이 정했다.")
    _sp = [len(v) for v in raw.values() if v is not None and len(v)]
    print(f"[STEP 1] 전 종목 봉수 {min(_sp)}~{max(_sp)} (상장 시점 차이 — 신규 ETF 는 짧다)")
    if not _depth_ok(len(bi)):
        print(f"[ABORT] SPY 수신 {len(bi)}봉 < {int(FMP_RECORD_CAP * MIN_DEEP_FRAC)}봉 — 약정한 "
              "'FMP 상한까지'의 깊은 창이 아니다(override 미적용 또는 FMP 상한 축소). 중단.")
        return 1

    if rate < bt.MIN_FETCH_RATE:
        print(f"[ABORT] 페치 성공률 {rate * 100:.1f}% < {bt.MIN_FETCH_RATE * 100:.1f}% — 시트에 쓰지 않고 중단.")
        return 1
    if "SPY" in fallback:
        print("[ABORT] SPY 배당조정 실패 — 원종가로 대체되면 SPY 대비 수치의 기준이 달라진다. 중단.")
        return 1
    gaps = _adj_floor_gaps(raw, adjmap, fallback)
    if gaps:
        print(f"[ABORT] 배당조정 계열이 원종가보다 {ADJ_FLOOR_TOL_DAYS}일 넘게 늦게 시작하는 종목 "
              f"{len(gaps)}개 — 초반 성과가 NaN 으로 조용히 오염된다. 시뮬 전에 중단.")
        for tk, r0, a0, g in gaps[:12]:
            print(f"   - {tk}: 원종가 {r0} · 배당조정 {a0} ({g}일)")
        print("   → dividend-adjusted 엔드포인트의 깊이는 미실측이다. 이 결과 자체가 발견이다.")
        return 1
    if fallback:
        print(f"[WARN] 배당조정 실패 → 원종가 대체 {len(fallback)}종목: {sorted(fallback)[:10]}")

    close_df, adj_df = bt.build_panels(raw, adjmap)
    if len(close_df.index) <= warmup + REBOUND_BARS:
        print(f"[ABORT] 캘린더 {len(close_df.index)}봉 — 워밍업+반등 구간도 안 된다. 중단.")
        return 1
    meta["uhash"] = bt.universe_hash(universe)
    rows = analyze(close_df, adj_df, warmup, meta)
    write_results(rows)
    print("\n⚠️ 이 결과로 A/B 룰·트리거 숫자를 바꾸지 않는다(§2·§5). 쓸 수 있는 곳은 §2 각주의"
          " '알려진 약점' 뿐이다.")
    print(f"⏱️ 소요 {time.time() - t0:.1f}초 · 유니버스 지문 {meta['uhash']}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# 자체검증 (네트워크 불필요)
# ══════════════════════════════════════════════════════════════════════════════
def _piecewise(points, seg=30):
    """[(값), ...] 꼭짓점을 seg 봉씩 직선 연결한 시리즈."""
    vals = []
    for a, b in zip(points[:-1], points[1:]):
        vals.extend(np.linspace(a, b, seg, endpoint=False))
    vals.append(points[-1])
    return np.array(vals, dtype=float)


def _selftest() -> int:
    fails = []

    # ── 1. 사건 추출 ────────────────────────────────────────────────────
    #   120 → 102(−15%, 포함) → 125 새 고점 → 112(−10.4%, 제외) → 130 새 고점
    #   → 118 → 125(회복 못함) → 110(같은 사건, 바닥은 110) → 135 회복
    #   → 115(−14.8%, 미회복으로 끝남)
    pts = [100, 120, 102, 125, 112, 130, 118, 125, 110, 135, 115]
    v = _piecewise(pts)
    spy = pd.Series(v, index=pd.bdate_range("2010-01-04", periods=len(v)))
    eps = find_episodes(spy)
    if len(eps) != 3:
        fails.append(f"사건 수 {len(eps)} != 3 (−10.4% 제외 · 재하락 병합 · 미회복 포함)")
    else:
        e1, e2, e3 = eps
        if abs(v[e1["trough_i"]] - 102) > 1e-9 or abs(e1["dd"] - (102 / 120 - 1)) > 1e-9:
            fails.append(f"사건1 바닥/낙폭 오류: {v[e1['trough_i']]}, {e1['dd']}")
        if abs(v[e2["peak_i"]] - 130) > 1e-9 or abs(v[e2["trough_i"]] - 110) > 1e-9:
            fails.append("사건2: 회복 전 재하락이 병합되지 않았거나 바닥이 최저점이 아님")
        if e2["recover_i"] is None or v[e2["recover_i"]] < 130:
            fails.append("사건2 회복 지점이 고점 회복(≥)이 아님")
        if e3["recover_i"] is not None:
            fails.append("사건3 미회복인데 recover_i 가 채워짐")
    if find_episodes(spy, dd=-0.16):
        fails.append("임계 −16% 에서도 사건이 잡힘 — dd 인자가 무시된다")
    if len(find_episodes(spy, dd=-0.10)) != 4:
        fails.append("임계 −10% 에서 −10.4% 사건이 안 잡힘 — 비교 방향 오류")

    # ── 2. 구간 절삭·절단 ───────────────────────────────────────────────
    if len(eps) == 3:
        e1 = eps[0]
        mid = (e1["peak_i"] + e1["trough_i"]) // 2
        legs = episode_legs(eps, mid, len(v))
        d1 = [L for L in legs if L["ep"] == 1 and L["leg"] == "drawdown"]
        if not d1 or not d1[0]["clipped"] or d1[0]["lo"] != mid:
            fails.append("평가 시작 전 고점이 절삭 표시되지 않음")
        r3 = [L for L in legs if L["ep"] == 3 and L["leg"] == "rebound"]
        if r3 and not r3[0]["truncated"]:
            fails.append("데이터 끝에 걸린 반등 구간이 절단 표시되지 않음")
        late = episode_legs(eps, e1["trough_i"] + 1, len(v))
        if any(L["ep"] == 1 and L["leg"] == "drawdown" for L in late):
            fails.append("바닥까지 워밍업 안인 사건의 하락 구간이 버려지지 않음")
        rb = [L for L in legs if L["leg"] == "rebound" and not L["truncated"]]
        if any(L["hi"] - L["lo"] != REBOUND_BARS for L in rb if not L["clipped"]):
            fails.append("반등 구간 길이가 REBOUND_BARS 가 아님")

    # ── 3. 곡선 절단 경계 ───────────────────────────────────────────────
    ci = pd.bdate_range("2020-01-01", periods=10)
    curve = pd.Series([100, 110, 99, 121, 90, 108, 120, 130, 117, 140.0], index=ci)
    log = [{"exec": ci[2], "sold": ["A"]}, {"exec": ci[5], "sold": ["B"]},
           {"exec": ci[7], "sold": []}, {"exec": ci[8], "sold": ["C"]}]
    lm = leg_metrics(curve, log, ci[2], ci[7])
    if abs(lm["ret"] - (130 / 99 - 1) * 100) > 1e-9:
        fails.append(f"구간 수익률 경계 오류: {lm['ret']}")
    if abs(lm["mdd"] - (90 / 121 - 1) * 100) > 1e-9:
        fails.append(f"구간 MDD 오류: {lm['mdd']}")
    if lm["swaps"] != 1:
        fails.append(f"교체 수 {lm['swaps']} != 1 (lo 당일 제외 · 빈 sold 제외 · hi 포함)")
    if leg_metrics(curve, log, ci[5], ci[8])["swaps"] != 1:
        fails.append("hi 당일 체결이 교체 수에서 빠짐")
    pre = leg_metrics(curve.iloc[3:], log, ci[1], ci[5])
    if np.isfinite(pre["ret"]):
        fails.append("첫 체결 전 구간에서 수익률이 나옴 — nan 이어야 한다")

    # ── 4. 사전 약정 상수 ───────────────────────────────────────────────
    got = (EPISODE_DD, REBOUND_BARS, RULES, FILTERS, VARIANT_NAME, SLOTS, SWAP_MODE, _WKEY)
    want = (-0.12, 126, ("blend", "mom12_1", "mom12_0"), ("none", "no_new"),
            "top5_eq", 5, "swap", None)
    if got != want:
        fails.append(f"사전 약정 상수가 바뀜: {got}")
    if RULES is not rc.VERDICT_RULES:
        fails.append("RULES 가 rc.VERDICT_RULES 를 그대로 쓰지 않는다 — 사본은 갈라진다")
    if any(r.endswith("_ra") for r in RULES):
        fails.append("위험조정 룰이 들어감 — 사전 약정 위반")
    if _RESULT_WORKSHEET == rc._RESULT_WORKSHEET:
        fails.append("판정 탭(Momentum_Rule_Compare)에 기록하려 한다")

    # ── 5·6. AST — 판정 출력 금지 · 판정 탭 금지 · as_of 전파 ──────────
    try:
        tree = ast.parse(open(__file__, encoding="utf-8").read())
    except OSError:
        tree = None
        fails.append("AST: 자기 소스를 읽지 못했다")
    if tree is not None:
        banned = {"verdicts", "pairwise", "print_verdicts", "print_summary",
                  "print_windows", "walkforward_windows", "mean_metric"}
        used = sorted({n.attr for n in ast.walk(tree)
                       if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id == "rc" and n.attr in banned})
        if used:
            fails.append(f"판정용 함수 참조: rc.{used} — 승수·순위는 출력하지 않는다")
        if any(isinstance(n, ast.Constant) and n.value == rc._RESULT_WORKSHEET
               for n in ast.walk(tree)):
            fails.append("판정 탭 이름 문자열이 코드에 있다")
        calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Attribute) and c.func.attr == "_batch_fetch"]
        if not calls:
            fails.append("as_of 전파: _batch_fetch 호출을 못 찾았다 — 검사가 무효")
        for c in calls:
            if not any(k.arg == "as_of" for k in c.keywords):
                fails.append(f"as_of 전파: L{c.lineno} 에 as_of= 가 없다")

    # ── 7. override 는 페치 **중에만** 켜지고 끝나면 복구된다 (런타임) ──
    seen = []
    saved_fetch, saved_env = bt._batch_fetch, os.environ.get("AS_OF")
    try:
        os.environ["AS_OF"] = ""
        bt._batch_fetch = lambda tks, *, bars, as_of: (
            seen.append(bt.WINDOW_DAYS_OVERRIDE), ({}, {}, [], {}, []))[1]
        with contextlib.redirect_stdout(io.StringIO()):
            rc_main = main()
    finally:
        bt._batch_fetch = saved_fetch
        if saved_env is None:
            os.environ.pop("AS_OF", None)
        else:
            os.environ["AS_OF"] = saved_env
    if seen != [DEEP_WINDOW_DAYS]:
        fails.append(f"페치 시점 override = {seen} — {DEEP_WINDOW_DAYS} 이어야 한다")
    if bt.WINDOW_DAYS_OVERRIDE is not None:
        fails.append("main() 뒤 override 가 남아 있다 — 같은 프로세스의 판정 경로가 깊어진다")
    if rc_main != 1:
        fails.append("SPY 없는 페치에서 main() 이 중단(1)하지 않음")

    # ── 8. 배당조정 바닥 게이트 ──────────────────────────────────────────
    di = pd.bdate_range("2007-01-01", periods=40)
    r_ = {"SPY": pd.Series(1.0, index=di), "XLK": pd.Series(1.0, index=di)}
    a_ = {"SPY": pd.Series(1.0, index=di), "XLK": pd.Series(1.0, index=di[10:])}
    g = _adj_floor_gaps(r_, a_, [])
    if [x[0] for x in g] != ["XLK"]:
        fails.append(f"배당조정 바닥 게이트가 늦은 계열을 못 잡음: {g}")
    if _adj_floor_gaps(r_, a_, ["XLK"]):
        fails.append("원종가 대체 종목까지 바닥 게이트에 걸림(대체는 같은 계열이다)")

    # ── 9. 합성 패널로 analyze 전 경로 — 행 폭·구간 수 ──────────────────
    idx_s, close_s, adj_s = rc._synthetic(n=900, seed=7)
    sv = _piecewise([100, 130, 105, 140, 118, 150], seg=130)[:900]
    sv = np.concatenate([np.linspace(90, 100, 900 - len(sv)), sv]) if len(sv) < 900 else sv
    close_s["SPY"] = sv
    adj_s["SPY"] = sv
    warm = fx.mom_warmup_bars(RULES)
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        rows = analyze(close_s, adj_s, warm, {"uhash": "TEST"}, verbose=True)
    txt = buf.getvalue()
    if not rows:
        fails.append("합성 패널에서 행이 0개 — 전 경로가 돌지 않았다")
    if any(len(r) != len(_RESULT_COLS) for r in rows):
        fails.append(f"행 폭 != 헤더 폭({len(_RESULT_COLS)})")
    nlegs = len(episode_legs(find_episodes(close_s["SPY"]), warm, len(idx_s)))
    if len(rows) != nlegs * len(RULES) * len(FILTERS):
        fails.append(f"행 수 {len(rows)} != 구간 {nlegs} × 룰 {len(RULES)} × 필터 {len(FILTERS)}")
    if txt.find("[STEP 2] 사건 목록") > txt.find("[STEP 3]") or "[STEP 2] 사건 목록" not in txt:
        fails.append("사건 목록이 시뮬보다 먼저 출력되지 않음")
    if _depth_ok(1255) or _depth_ok(3764) or not _depth_ok(5000) or not _depth_ok(4963):
        fails.append("깊이 게이트 오류 — 판정 창(1255)·15년(3764)은 막고 상한 근처는 통과해야 한다")
    if [_cell(x) for x in (float("nan"), np.float64("inf"), np.int64(3), 1.5, True)] \
            != ["", "", 3, 1.5, True]:
        fails.append("시트 셀 정규화 오류 — NaN 이 API 요청 전체를 거절시킨다")
    for bad in ("샤프", "승 /", "창 중"):
        if bad in txt:
            fails.append(f"판정성 출력 '{bad}' 가 찍힘 — 사전 약정 위반")

    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 전 항목 통과 (사건추출·병합·임계방향·절삭·절단·곡선경계·교체경계·"
          "사전약정상수·판정출력금지·판정탭금지·as_of전파·override복구·배당조정게이트·"
          "셀정규화·깊이게이트·전경로)")
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
