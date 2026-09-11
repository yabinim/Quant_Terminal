#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_pead_label_verify.py — PEAD 라벨 검증 **판정 실행** (1회 전용 · 읽기 전용 진단)

  실행: python automation/diag_pead_label_verify.py
        python automation/diag_pead_label_verify.py --selftest   # 네트워크 불필요

약정은 저장소 루트 `PEAD_PRECOMMIT.md` 다. 이 파일은 그 약정의 **구현**이며,
숫자를 두 번째로 적은 곳이다. 시작할 때 md 를 읽어 앵커를 대조하고, 하나라도
어긋나면 수익률을 계산하기 전에 멈춘다 — 한쪽만 고치는 드리프트를 막는다.

아무것도 수정하지 않는다. `PEAD_Label_Verify` 탭에 결과 행만 append 한다.


═══════════════════════════════════════════════════════════════════════════════
약정 요약 (PEAD_PRECOMMIT.md 2026-09-11 · 결과 보기 전 박제)
═══════════════════════════════════════════════════════════════════════════════
질문   라이브 `evaluate_pead` 의 두 라벨이, 아무도 본 적 없는 과거 사건에서
       라벨이 말하는 방향의 드리프트를 보이는가.
대상   `ec.evaluate_pead(m, move)` — regime 미전달(라이브 사후 패스와 같다).
       반응일은 `ec.measure_reaction(hist, d, "")` — A-TIME ❌ 이라 거래량 추론.
구간   판정 [2012-01-01, 2021-10-20) · 표시 [2021-10-20, 오늘−100일]
유니버스 Tier 1 = Watchlist ∪ Portfolios · Tier 2 = Earnings_Universe − Tier 1
       중 seed 20260911 · 60종목. **A4 와 같은 함수**(`V._universe`)를 부른다.
사건   발표일(epsActual 존재) → 분기말 흔적 ≤ 7일 제외 → 반응 ok →
       워밍업(2012 이후 직전 최대 12건, `expected_move` ok) → 청산가 존재
측정   D+k = C[i+k]. 진입 D+1 · 청산 D+60 · −SPY 초과수익.
       스프레드 = 라벨 평균 − 무조건 평균(같은 티어 판정 사건 전부, muted 포함)
문턱   P1 n ≥ 100 · P2 평균 스프레드 up ≥ +1.5%p / down ≤ −1.0%p ·
       P3 중앙값 스프레드 부호 · P4 라벨 ≥ 10건인 해 중 부호 맞는 해 ≥ 6
       **두 티어 × 4조건 = 8칸 전부 ✅ 여야 통과.** 표본 부족도 미달.
미달   라벨 문구에서 예측 의미를 뺀다(`🟢 강한 상승 반응` / `🔴 강한 하락 반응`).
       코드 값은 바꾸지 않는다 — Earnings_Events.PEAD_Verdict 과거 행 호환.
1회    결과 탭에 행이 하나라도 있으면 실행하지 않는다.


설계 메모
─────────
 1) **수익률은 STEP 3 에서 처음 나온다.** STEP 0~2(약정 대조 · 수집 · 무결성)를
    전부 통과하기 전에는 수익률 배열을 만들지도, 출력하지도 않는다. 게이트가
    실패하면 화면에 남는 것은 사건 수와 실패 사유뿐이다. 결과를 본 뒤 "측정
    결함"을 핑계로 다시 굴리는 경로를 없애는 것이 목적이다(`diag_beta_mom_ref`
    의 R0 게이트와 같은 구조).
 2) **재구현 금지.** 유니버스·표본·행 파서·분기말은 A4(`diag_pead_date_validity`)
    와 프로브의 함수를 그대로 부른다. 라벨·반응·예상 변동폭은 `earnings_core`
    SSOT 다. 이 파일이 따로 정의하는 계산은 **초과수익과 P1~P4** 뿐이다.
 3) **순수 함수 / IO 분리.** `build_events` · `attach_returns` · `judge_tier` 는
    네트워크를 모른다. `--selftest` 가 합성 세계로 그 셋을 그대로 때린다 —
    검증한 코드와 실행하는 코드가 같아야 검증에 의미가 있다.
 4) **뮤테이션 스위치는 셀프테스트 전용.** `build_events` · `attach_returns` 의
    키워드 인자(entry_off · horizon · use_spy · pe_filter · warmup)는 기본값이
    약정값이고, main() 은 그 값을 넘기지 않는다. 셀프테스트만 값을 바꿔
    "규칙을 어기면 숫자가 달라지는가"를 확인한다. 죽은 게이트를 잡는 장치다.
 5) **시트 기록 실패도 결과를 잃지 않는다.** 기록 직전에 결과 행 전체를 TSV 로
    로그에 찍는다. 수익률을 계산한 순간 그 실행은 판정 실행이고(§8), 기록이
    실패했다고 다시 굴릴 수는 없기 때문이다.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytz

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import diag_pead_date_validity as V     # noqa: E402  — 유니버스·표본·분기말 SSOT
import diag_pead_issuance_probe as P    # noqa: E402  — 행 파서·발표 사건·경계일
import diag_satellite_backtest as bt    # noqa: E402  — _gs · _safe_append_rows SSOT
import earnings_core as ec              # noqa: E402  — 검증 대상 SSOT
import fmp_extras as fx                 # noqa: E402  — 창(from/to) SSOT
import fmp_http as fh                   # noqa: E402  — 호출·레이트리밋 SSOT

_ET = pytz.timezone("US/Eastern")

# ══════════════════════════════════════════════════════════════════════════
# 약정 상수 — PEAD_PRECOMMIT.md 와 두 벌이다. `precommit_check()` 가 묶는다.
# ══════════════════════════════════════════════════════════════════════════
PRECOMMIT_FILE = "PEAD_PRECOMMIT.md"
RESULT_WORKSHEET = "PEAD_Label_Verify"

JUDGE_FROM = "2012-01-01"          # 포함
JUDGE_TO = "2021-10-20"            # **배타** — 당일은 표시 구간
ENTRY_OFF = 1                      # 진입 D+1
HORIZON = 60                       # 청산 D+60
PE_LAG_MAX = V.PE_LAG_MAX          # 분기말 흔적 ≤ 7일 → 제외
WARMUP_LIMIT = ec.GAP_QUARTERS + 4  # 12 — 라이브 past_earnings_dates 상한과 같다
POST_CAL_DAYS = P.POST_CAL_DAYS    # 100 — 오늘−100일 이후는 D+60 미완

P1_MIN_N = 100
P2_UP_PP = 1.5
P2_DOWN_PP = -1.0
P4_MIN_YEAR_N = 10
P4_MIN_YEARS = 6
JUDGE_YEARS = tuple(range(2012, 2022))   # 2012~2021 (2021 은 10-20 까지)

PRICE_WINDOW_DAYS = V.PRICE_WINDOW_DAYS  # 7400 — A4 와 같은 창 요청
SPY_FLOOR = "2011-12-01"                 # 무결성 ②: SPY 첫 봉이 이보다 늦으면 중단
FAIL_RATE_MAX = 0.05                     # 무결성 ③: 티어별 조회 실패율 상한

TARGET_LABELS = ("up_continue", "down_break")
FAIL_TEXT = {"up_continue": "🟢 강한 상승 반응", "down_break": "🔴 강한 하락 반응"}

# 동결 상수 — 이 값이 바뀌면 "지금의 라벨"이 아니다.
FROZEN = {"PEAD_VOLUME_MIN": 2.0, "PEAD_SURPRISE_RATIO": 0.8,
          "VOLUME_BASELINE_BARS": 20, "MIN_SAMPLE": 4, "GAP_QUARTERS": 8}

_CACHE: dict = {}


def _json(path: str):
    """(data, kind). 같은 경로는 한 번만 부른다."""
    if path not in _CACHE:
        data, _st, kind = fh.fmp_get_json_ex(path)
        _CACHE[path] = (data, kind)
    return _CACHE[path]


def _f(v, nd=2):
    return "n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.{nd}f}"


def _mark(ok) -> str:
    return "✅" if ok is True else ("❌" if ok is False else "—")


# ══════════════════════════════════════════════════════════════════════════
# STEP 0 — 약정 대조 · 동결 상수
# ══════════════════════════════════════════════════════════════════════════

def _md_path() -> str:
    """약정 문서 경로. 러너는 automation/ 에 있고 md 는 저장소 루트에 있다."""
    for base in (os.path.dirname(_HERE), _HERE, os.getcwd()):
        p = os.path.join(base, PRECOMMIT_FILE)
        if os.path.exists(p):
            return p
    return ""


def precommit_check(md: str) -> tuple:
    """약정문 ↔ 이 파일의 상수 대조. (ok, 줄들).

    문자열 존재 검사가 아니라 **값 추출 후 비교**다. md 에서 1.5 를 2.0 으로
    고치고 러너를 그대로 두면 여기서 걸린다 — 그 반대도 마찬가지다.
    """
    lines, ok = [], True

    def hit(name, pat, want, cast=str):
        nonlocal ok
        m = re.search(pat, md)
        got = cast(m.group(1)) if m else None
        good = (got is not None) and (got == want)
        ok = ok and good
        lines.append(f"  {_mark(good)} {name:22} md={got!s:24} 러너={want!s}")

    # 문턱 — md §6 표. 음수 부호는 U+2212(−) 다. 하이픈으로 쓰면 여기서 걸린다.
    hit("P1 최소 표본", r"n\(L\) ≥ (\d+)", P1_MIN_N, int)
    hit("P2 up 문턱", r"\*\*≥ \+([0-9.]+)%p\*\*", P2_UP_PP, float)
    hit("P2 down 문턱", r"\*\*≤ \u2212([0-9.]+)%p\*\*", abs(P2_DOWN_PP), float)
    hit("P4 최소 연도 표본", r"L_y ≥ (\d+)건", P4_MIN_YEAR_N, int)
    hit("P4 최소 연도 수", r"\*\*(\d+)개 이상\*\*", P4_MIN_YEARS, int)
    # 구간 · 사건 규칙
    hit("판정 구간", r"판정 구간 \*\*\[([0-9\-]+, [0-9\-]+)\)\*\*",
        f"{JUDGE_FROM}, {JUDGE_TO}")
    hit("진입", r"진입 D\+(\d+)", ENTRY_OFF, int)
    hit("청산", r"청산 D\+(\d+)", HORIZON, int)
    hit("분기말 흔적", r"\*\*(\d+)일 이하\*\*", PE_LAG_MAX, int)
    hit("워밍업 상한", r"최대 (\d+)건", WARMUP_LIMIT, int)
    hit("표시 꼬리", r"실행일 − (\d+)달력일", POST_CAL_DAYS, int)
    hit("Tier 2 seed", r"seed (\d+)", P.T2_SEED, int)
    hit("Tier 2 표본", r"(\d+)종목", P.T2_SAMPLE, int)
    hit("SPY 바닥", r"첫 봉이 ([0-9\-]+) 보다 늦다", SPY_FLOOR)
    hit("조회 실패 상한", r"실패한 종목이 (\d+)% 를 넘는다",
        int(FAIL_RATE_MAX * 100), int)

    # 라벨 문구 — 현재 문구(검증 대상)와 미달 시 문구(사전 확정)
    for code in TARGET_LABELS:
        cur, new = ec.PEAD_LABELS[code], FAIL_TEXT[code]
        good = (f"`{cur}`" in md) and (f"**`{new}`**" in md)
        ok = ok and good
        lines.append(f"  {_mark(good)} 라벨 {code:12} {cur} → {new}")
    return ok, lines


def frozen_check() -> tuple:
    """동결 상수 — earnings_core 실제 값 ↔ 러너 ↔ md 표."""
    md = ""
    p = _md_path()
    if p:
        md = open(p, encoding="utf-8").read()
    lines, ok = [], True
    for k, want in FROZEN.items():
        got = getattr(ec, k, None)
        same = (got is not None) and (float(got) == float(want))
        in_md = re.search(rf"`{k}` \| {re.escape(str(want))} \|", md) is not None
        good = same and in_md
        ok = ok and good
        lines.append(f"  {_mark(good)} {k:22} ec={got!s:8} 러너={want!s:8} "
                     f"md={'있음' if in_md else '없음'}")
    return ok, lines


# ══════════════════════════════════════════════════════════════════════════
# STEP 1 — 수집 (IO)
# ══════════════════════════════════════════════════════════════════════════

def price_frame(tk: str) -> tuple:
    """(OHLCV, kind). 라이브 `run_earnings_watch.fmp_price_history` 와 같은 엔드포인트·열.

    kind 를 함께 돌려주는 이유는 무결성 게이트 때문이다. "조회 실패"와 "조회는
    됐는데 그 종목에 그 데이터가 없다"는 전혀 다른 사건인데, 빈 결과만 보면
    구분되지 않는다.
    """
    data, kind = _json(f"historical-price-eod/full?symbol={tk}"
                       f"{fx.hist_range_params(PRICE_WINDOW_DAYS)}")
    rows = P._rows(data)
    if not rows:
        return pd.DataFrame(), kind
    df = pd.DataFrame(rows)
    if "date" not in df.columns or "close" not in df.columns:
        return pd.DataFrame(), kind
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    out = pd.DataFrame(index=df.index)
    for src, dst in (("open", "Open"), ("high", "High"), ("low", "Low"),
                     ("close", "Close"), ("volume", "Volume")):
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src], errors="coerce")
    if "Close" not in out.columns:
        return pd.DataFrame(), kind
    return out.dropna(subset=["Close"]), kind


def earnings_rows(tk: str) -> tuple:
    """([(날짜, 행)] 오름차순, kind) — 프로브 `_reported` 규칙 그대로."""
    data, kind = _json(f"earnings?symbol={tk}&limit=1000")
    return P._reported(P._rows(data)), kind


def quarter_ends(tk: str) -> tuple:
    """(분기말 날짜 오름차순, kind).

    `V._quarters` 와 같은 엔드포인트·같은 파싱이다. 따로 두는 이유는 하나뿐 —
    `V._quarters` 는 kind 를 버리는데, 무결성 게이트가 그 값을 봐야 한다.
    공시일(filingDate)은 쓰지 않으므로 분기말만 뽑는다.
    """
    data, kind = _json(f"income-statement?symbol={tk}&period=quarter&limit=80")
    out = []
    for r in P._rows(data):
        pe = ec._d(r.get("date"))
        if pe is not None:
            out.append(pe)
    return sorted(set(out)), kind


def classify_status(kinds, has_bars: bool, n_rep: int, n_q: int,
                    prof_kind: str = "", is_fund=None) -> str:
    """종목 1건의 상태 — "ok" | "fail" | "fund". **순수 함수**(셀프테스트 T6).

    약정 §8 의 무결성 게이트는 "가격·실적·분기 재무제표 **조회에 실패한** 종목"을
    센다. ETF 처럼 애초에 실적이 없는 종목은 조회 실패가 아니라 사건이 0건인
    종목이다 — 첫 실행(2026-09-11 23:30 ET)이 이 둘을 뭉뚱그려 Tier 1 의 ETF
    17종목을 '실패 16%'로 세고 중단했다. 여기서 가른다.

    단, 기준을 "실적 0행이면 제외"로 두면 게이트의 이빨이 빠진다 — 진짜 데이터
    구멍도 조용히 빠져나간다. 그래서 **펀드로 확인된 종목만** 제외하고,
    나머지 실적 0행은 그대로 실패로 센다.
    """
    if any(str(k or "") != "ok" for k in kinds) or not has_bars:
        return "fail"
    if n_rep > 0 and n_q > 0:
        return "ok"
    if str(prof_kind or "") == "ok" and is_fund is True:
        return "fund"
    return "fail"


def fetch_ticker(tk: str) -> tuple:
    """(status, hist, rep, qs). status = classify_status 결과."""
    hist, pk = price_frame(tk)
    rep, ek = earnings_rows(tk)
    qs, qk = quarter_ends(tk)
    prof_kind, is_fund = "", None
    if not (rep and qs) and all(k == "ok" for k in (pk, ek, qk)) and not hist.empty:
        data, prof_kind = _json(f"profile?symbol={tk}")
        rows = P._rows(data)
        if rows:
            is_fund = bool(rows[0].get("isEtf")) or bool(rows[0].get("isFund"))
    return (classify_status((pk, ek, qk), not hist.empty, len(rep), len(qs),
                            prof_kind, is_fund), hist, rep, qs)


# ══════════════════════════════════════════════════════════════════════════
# STEP 2 — 사건 구성 (순수 · 수익률 없음)
# ══════════════════════════════════════════════════════════════════════════

def _prev_quarter_end(qends: list, d: pd.Timestamp):
    """발표일 직전(이하) 분기말. 없으면 None."""
    prev = None
    for pe in qends:
        if pe is None or pe > d:
            break
        prev = pe
    return prev


def build_events(hist: pd.DataFrame, reported: list, qends: list, *,
                 pe_filter: bool = True, warmup: bool = True,
                 today=None) -> tuple:
    """발표 사건 → 라벨 붙은 사건 목록. **수익률을 계산하지 않는다.**

    반환: (events, drop) — events 원소
      {date, i(반응 세션 위치), code, gap_pct, vol_ratio, move_n}

    pe_filter / warmup 은 셀프테스트 전용 뮤테이션 스위치다(설계 메모 4).
    """
    keys = ("분기말흔적", "분기말미상", "반응불가", "워밍업부족", "반응중복")
    drop = {k: 0 for k in keys}
    drop_j = {k: 0 for k in keys}      # 판정창 [2012-01-01, 2021-10-20) 안에서만
    out, used_i = [], set()

    def _bye(k, d):
        """버린 사유 집계. 전체와 판정창을 따로 센다 — 첫 실행 로그에서 판정창
        밖(2006~2011) 사건이 카운터를 뒤덮어 판정창의 실제 손실이 보이지 않았다."""
        drop[k] += 1
        if pd.Timestamp(JUDGE_FROM) <= d < pd.Timestamp(JUDGE_TO):
            drop_j[k] += 1

    if hist is None or hist.empty or not reported:
        return out, drop, drop_j

    j_from = pd.Timestamp(JUDGE_FROM)
    kept_dates = []          # 워밍업 표본 — 규칙 2 를 통과한 사건만 (오름차순)

    for d, _row in reported:
        if pe_filter:
            pe = _prev_quarter_end(qends, d)
            if pe is None:
                _bye("분기말미상", d)
                continue
            if (d - pe).days <= PE_LAG_MAX:
                _bye("분기말흔적", d)
                continue
        # 여기까지가 규칙 2 통과. 워밍업 표본에는 판정 여부와 무관하게 들어간다.
        past = [x for x in kept_dates if x < d and x >= j_from]
        kept_dates.append(d)

        i = ec.resolve_reaction_index(hist, d, "")
        m = ec.measure_reaction(hist, d, "")
        if i is None or not m.get("ok"):
            _bye("반응불가", d)
            continue
        if i in used_i:
            # 두 사건이 같은 반응 세션에 걸리면 이른 날짜 하나만 남긴다.
            _bye("반응중복", d)
            continue

        move = None
        if warmup:
            ev = [{"date": x, "timing": ""} for x in reversed(past[-WARMUP_LIMIT:])]
            move = ec.expected_move(ec.gap_history(hist, ev))
            if not move.get("ok"):
                _bye("워밍업부족", d)
                continue

        pead = ec.evaluate_pead(m, move)
        used_i.add(i)
        out.append({"date": d, "i": int(i), "code": pead["code"],
                    "gap_pct": m.get("gap_pct"), "vol_ratio": m.get("volume_ratio"),
                    "move_n": int((move or {}).get("sample_n") or 0)})
    return out, drop, drop_j


def window_of(d: pd.Timestamp, today: date) -> str:
    """judge / display / (빈 문자열 = 대상 아님)."""
    if pd.Timestamp(JUDGE_FROM) <= d < pd.Timestamp(JUDGE_TO):
        return "judge"
    tail = pd.Timestamp(today) - pd.Timedelta(days=POST_CAL_DAYS)
    if pd.Timestamp(JUDGE_TO) <= d <= tail:
        return "display"
    return ""


# ══════════════════════════════════════════════════════════════════════════
# STEP 3 — 수익률 · 판정 (순수). **게이트 통과 후에만 부른다.**
# ══════════════════════════════════════════════════════════════════════════

def attach_returns(events: list, hist: pd.DataFrame, spy: pd.Series, *,
                   entry_off: int = ENTRY_OFF, horizon: int = HORIZON,
                   use_spy: bool = True) -> tuple:
    """사건에 초과수익 x(%p)를 붙인다. 청산가·SPY 가 없으면 버린다.

    x = (C[i+h]/C[i+e] − 1)×100 − (SPY[d_h]/SPY[d_e] − 1)×100
    """
    drop = {"청산봉없음": 0, "가격이상": 0, "SPY없음": 0}
    out = []
    n = len(hist.index) if hist is not None else 0
    for ev in events:
        i = ev["i"]
        a, b = i + entry_off, i + horizon
        if b >= n:
            drop["청산봉없음"] += 1
            continue
        try:
            pa = float(hist["Close"].iloc[a])
            pb = float(hist["Close"].iloc[b])
        except Exception:
            drop["가격이상"] += 1
            continue
        if not (pa > 0 and pb > 0):
            drop["가격이상"] += 1
            continue
        r = (pb / pa - 1.0) * 100.0
        if use_spy:
            da, db = hist.index[a], hist.index[b]
            if spy is None or da not in spy.index or db not in spy.index:
                drop["SPY없음"] += 1
                continue
            sa, sb = float(spy.loc[da]), float(spy.loc[db])
            if not (sa > 0 and sb > 0):
                drop["SPY없음"] += 1
                continue
            r -= (sb / sa - 1.0) * 100.0
        out.append({**ev, "x": r, "year": int(pd.Timestamp(ev["date"]).year)})
    return out, drop


def judge_tier(rows: list, code: str, *, excl_label_from_u: bool = False) -> dict:
    """한 티어 · 한 라벨의 P1~P4.

    U = 이 티어의 판정 사건 전부(라벨 무관 · muted 포함) · L = 그중 code 인 것.
    """
    L = [r for r in rows if r["code"] == code]
    U = [r for r in rows if not (excl_label_from_u and r["code"] == code)]
    up = (code == "up_continue")
    sgn = 1.0 if up else -1.0
    thr = P2_UP_PP if up else P2_DOWN_PP

    res = {"code": code, "n_l": len(L), "n_u": len(U),
           "mean_l": None, "mean_u": None, "mean_d": None,
           "med_l": None, "med_u": None, "med_d": None,
           "years_valid": 0, "years_ok": 0, "year_detail": [],
           "p1": False, "p2": False, "p3": False, "p4": False, "pass": False}
    if not L or not U:
        return res

    xl = np.array([r["x"] for r in L], dtype=float)
    xu = np.array([r["x"] for r in U], dtype=float)
    res["mean_l"], res["mean_u"] = float(xl.mean()), float(xu.mean())
    res["med_l"], res["med_u"] = float(np.median(xl)), float(np.median(xu))
    res["mean_d"] = res["mean_l"] - res["mean_u"]
    res["med_d"] = res["med_l"] - res["med_u"]

    for y in JUDGE_YEARS:
        ly = [r["x"] for r in L if r["year"] == y]
        uy = [r["x"] for r in U if r["year"] == y]
        if len(ly) < P4_MIN_YEAR_N or not uy:
            res["year_detail"].append((y, len(ly), None))
            continue
        d = float(np.mean(ly)) - float(np.mean(uy))
        res["years_valid"] += 1
        if sgn * d > 0:
            res["years_ok"] += 1
        res["year_detail"].append((y, len(ly), d))

    res["p1"] = res["n_l"] >= P1_MIN_N
    res["p2"] = (res["mean_d"] >= thr) if up else (res["mean_d"] <= thr)
    res["p3"] = (sgn * res["med_d"]) > 0
    res["p4"] = res["years_ok"] >= P4_MIN_YEARS
    res["pass"] = all((res["p1"], res["p2"], res["p3"], res["p4"]))
    return res


# ══════════════════════════════════════════════════════════════════════════
# 시트
# ══════════════════════════════════════════════════════════════════════════
_HDR = ["실행ET", "구간", "티어", "라벨", "n_L", "n_U", "평균L", "평균U", "평균Δ",
        "중앙L", "중앙U", "중앙Δ", "유효연도", "부호일치연도", "P1", "P2", "P3",
        "P4", "판정", "약정커밋", "비고"]
_NCOL = len(_HDR)


def result_rows(now: str, per: dict, meta: dict) -> list:
    rows = []
    for win in ("judge", "display"):
        for tier in ("Tier 1", "Tier 2"):
            for code in TARGET_LABELS:
                r = (per.get(win, {}).get(tier, {}) or {}).get(code)
                if not r:
                    continue
                verdict = ("통과" if r["pass"] else "미달") if win == "judge" else "참고"
                rows.append([now, win, tier, code, r["n_l"], r["n_u"],
                             _f(r["mean_l"]), _f(r["mean_u"]), _f(r["mean_d"]),
                             _f(r["med_l"]), _f(r["med_u"]), _f(r["med_d"]),
                             r["years_valid"], r["years_ok"],
                             _mark(r["p1"]), _mark(r["p2"]), _mark(r["p3"]),
                             _mark(r["p4"]), verdict, meta.get("commit", ""),
                             ";".join(f"{y}:{n}:{_f(d,1)}" for y, n, d in r["year_detail"])])
    for tier in ("Tier 1", "Tier 2"):
        rows.append([now, "meta", tier, "tickers", len(meta["univ"].get(tier, [])), "",
                     "", "", "", "", "", "", "", "", "", "", "", "", "기록",
                     meta.get("commit", ""), ",".join(meta["univ"].get(tier, []))])
    rows.append([now, "meta", "-", "run", meta.get("calls", ""), "", "", "", "", "",
                 "", "", "", "", "", "", "", "", "기록", meta.get("commit", ""),
                 meta.get("note", "")])
    return rows


def _open_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GSPREAD_KEY"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    return bt._gs(gspread.authorize(creds).open, bt._SPREADSHEET_TITLE)


def _tab_rows(sh, title: str) -> tuple:
    if title not in [w.title for w in bt._gs(sh.worksheets)]:
        return False, []
    vals = bt._gs(bt._gs(sh.worksheet, title).get_all_values) or []
    return True, [r for r in vals[1:] if any(str(c).strip() for c in r)]


def write_results(sh, rows: list) -> bool:
    try:
        if RESULT_WORKSHEET in [w.title for w in bt._gs(sh.worksheets)]:
            ws = bt._gs(sh.worksheet, RESULT_WORKSHEET)
        else:
            ws = bt._gs(sh.add_worksheet, title=RESULT_WORKSHEET, rows=100, cols=_NCOL)
            bt._safe_append_rows(ws, [_HDR], ncols=_NCOL)
        bt._safe_append_rows(ws, rows, ncols=_NCOL)
        print(f"[OK] {RESULT_WORKSHEET} 시트에 {len(rows)}행 기록")
        return True
    except Exception as e:
        print(f"[FAIL] 시트 기록 실패: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    now_et = datetime.now(_ET)
    today = now_et.date()
    commit = str(os.environ.get("PRECOMMIT_COMMIT", "") or "").strip()
    print("═" * 78)
    print(f"PEAD 라벨 검증 판정 실행 — {now_et:%Y-%m-%d %H:%M ET}")
    print(f"약정 {PRECOMMIT_FILE}" + (f" · 커밋 {commit}" if commit else " · 커밋 미기재"))
    print("═" * 78)

    # ── STEP 0 ─────────────────────────────────────────────────────────────
    print("\n[STEP 0] 약정 대조 · 1회 가드")
    p = _md_path()
    if not p:
        print(f"  ❌ {PRECOMMIT_FILE} 을 찾지 못했다 — 약정 없이 판정하지 않는다.")
        return 1
    ok_md, lines = precommit_check(open(p, encoding="utf-8").read())
    print("\n".join(lines))
    ok_fz, flines = frozen_check()
    print("\n".join(flines))
    if not (ok_md and ok_fz):
        print("\n[ABORT] 약정문과 러너가 어긋난다. 수익률을 계산하지 않았다 — "
              "판정 실행이 아니다(§8 무결성 중단).")
        return 1
    if not fh.fmp_key():
        print("[ABORT] FMP_API_KEY 없음.")
        return 1

    sh = None
    try:
        sh = _open_sheet()
        exists, prev = _tab_rows(sh, RESULT_WORKSHEET)
        if exists and prev:
            print(f"[ABORT] {RESULT_WORKSHEET} 에 이미 {len(prev)}행 — 1회 전용이다(§8).")
            return 1
    except Exception as e:
        print(f"[ABORT] 시트 접근 실패: {e} — 1회 가드를 확인할 수 없으면 돌지 않는다.")
        return 1
    print("  ✅ 약정 대조 통과 · 결과 탭 비어 있음")

    # ── STEP 1 ─────────────────────────────────────────────────────────────
    print("\n[STEP 1] 유니버스 · 수집")
    univ = V._universe()
    for t, ts in univ.items():
        print(f"  {t}: {len(ts)}종목")
    if not univ.get("Tier 1") or not univ.get("Tier 2"):
        print("[ABORT] 두 티어가 모두 필요하다(약정 §6 은 AND 결합).")
        return 1

    spy_df = price_frame("SPY")
    if spy_df.empty:
        print("[ABORT] SPY 가격 수신 실패.")
        return 1
    spy = spy_df["Close"]
    first = pd.Timestamp(spy.index[0]).date()
    print(f"  SPY {len(spy)}봉 · {first} ~ {pd.Timestamp(spy.index[-1]).date()}")
    if pd.Timestamp(first) > pd.Timestamp(SPY_FLOOR):
        print(f"[ABORT] SPY 첫 봉 {first} > {SPY_FLOOR} — 판정 구간 앞부분이 잘린다(§8).")
        return 1

    per_tier_events, fails, funds = {}, {}, {}
    for tier, tickers in univ.items():
        evs, bad, fnd, dropsum, dropj = [], [], [], {}, {}
        for tk in tickers:
            status, hist, rep, qs = fetch_ticker(tk)
            if status == "fund":
                fnd.append(tk)       # ETF·펀드 — 실적 사건이 없는 종목. 실패 아님
                continue
            if status != "ok":
                bad.append(tk)
                continue
            e, drop, dj = build_events(hist, rep, qs)
            for k, v in drop.items():
                dropsum[k] = dropsum.get(k, 0) + v
            for k, v in dj.items():
                dropj[k] = dropj.get(k, 0) + v
            for ev in e:
                w = window_of(ev["date"], today)
                if w:
                    evs.append({**ev, "tk": tk, "win": w, "hist": hist})
        per_tier_events[tier] = evs
        fails[tier], funds[tier] = bad, fnd
        denom = max(1, len(tickers) - len(fnd))     # 분모는 실적이 있는 종목
        rate = len(bad) / denom
        print(f"  {tier}: 사건 {len(evs)}건 · 실적 종목 {denom} · "
              f"펀드 제외 {len(fnd)}종목 · 조회 실패 {len(bad)}종목({rate:.1%})")
        print(f"    버림(전체)   {dropsum}")
        print(f"    버림(판정창) {dropj}")
        if fnd:
            print(f"    펀드 제외: {', '.join(fnd)}")
        if rate > FAIL_RATE_MAX:
            print(f"[ABORT] {tier} 조회 실패율 {rate:.1%} > {FAIL_RATE_MAX:.0%}(§8). "
                  f"실패 {len(bad)}종목: {', '.join(bad)}")
            return 1

    # ── STEP 2 ─────────────────────────────────────────────────────────────
    print("\n[STEP 2] 무결성 통과 — 여기부터 수익률을 계산한다")
    per = {"judge": {}, "display": {}}
    for tier, evs in per_tier_events.items():
        for win in ("judge", "display"):
            sel = [e for e in evs if e["win"] == win]
            rows, dtot = [], {}
            for tk in sorted({e["tk"] for e in sel}):
                sub = [e for e in sel if e["tk"] == tk]
                got, drop = attach_returns(sub, sub[0]["hist"], spy)
                rows.extend(got)
                for k, v in drop.items():
                    dtot[k] = dtot.get(k, 0) + v
            per[win][tier] = {c: judge_tier(rows, c) for c in TARGET_LABELS}
            dist = {}
            for r in rows:
                dist[r["code"]] = dist.get(r["code"], 0) + 1
            print(f"  {win:8} {tier}: 판정 가능 {len(rows)}건 · 라벨 {dist} · 제외 {dtot}")

    # ── STEP 3 ─────────────────────────────────────────────────────────────
    print("\n[STEP 3] 판정")
    verdict = {}
    for code in TARGET_LABELS:
        print(f"\n  ── {code} ({ec.PEAD_LABELS[code]}) ──")
        allp = True
        for tier in ("Tier 1", "Tier 2"):
            r = per["judge"][tier][code]
            allp = allp and r["pass"]
            print(f"    {tier}  n={r['n_l']:4}/{r['n_u']:4}  "
                  f"평균Δ {_f(r['mean_d'])}%p  중앙Δ {_f(r['med_d'])}%p  "
                  f"연도 {r['years_ok']}/{r['years_valid']}  "
                  f"P1{_mark(r['p1'])} P2{_mark(r['p2'])} P3{_mark(r['p3'])} P4{_mark(r['p4'])}")
        verdict[code] = allp
        print(f"    → {'통과' if allp else '미달'}"
              + ("" if allp else f" · 문구 교체: {ec.PEAD_LABELS[code]} → {FAIL_TEXT[code]}"))

    meta = {"univ": univ, "commit": commit, "calls": fh.fmp_stats_line(),
            "note": f"SPY {first} ~ · 실패 "
                    + " ".join(f"{t}:{len(v)}" for t, v in fails.items())
                    + " · 펀드제외 "
                    + " ".join(f"{t}:{','.join(v)}" for t, v in funds.items() if v)}
    rows = result_rows(now_et.strftime("%Y-%m-%d %H:%M"), per, meta)

    # 설계 메모 5 — 기록 실패에 대비해 결과를 먼저 로그에 박는다.
    print("\n[STEP 4] 결과 행(TSV · 시트 기록 실패 대비 사본)")
    print("\t".join(_HDR))
    for r in rows:
        print("\t".join(str(c) for c in r))
    write_results(sh, rows)

    print("\n" + fh.fmp_stats_line())
    print("\n다음: PEAD_PRECOMMIT.md §11 에 결과 행 1줄을 추가한다. "
          "미달 라벨은 다음 커밋에서 문구를 교체한다(§7).")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# 셀프테스트 — 네트워크 없음. 약정 규칙을 어기면 숫자가 달라지는지 확인한다.
# ══════════════════════════════════════════════════════════════════════════

def _synth(seed=7, n_years=12, eff_up=0.0, eff_down=0.0):
    """합성 세계: 영업일 인덱스 · 분기 실적 · 반응일 갭 · SPY."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2010-01-04", periods=252 * n_years)
    m = len(idx)
    base = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, m)))
    opn_adj = np.full(m, -0.001)      # 평시: 시가 < 종가
    spy = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.009, m))), index=idx)
    vol = rng.lognormal(15, 0.25, m)
    ev_pos = list(range(80, m - 80, 63))
    rep, qends = [], []
    for k, pos in enumerate(ev_pos):
        d = idx[pos]
        gap = rng.normal(0, 6.0)
        base[pos:] *= (1 + gap / 100.0)
        vol[pos] *= 4.0
        # 라벨이 붙을 만한 갭(|gap| ≥ 4 ≈ 중앙값)에만 효과를 준다. 모든 갭에
        # 주면 무조건 집합 U 가 같이 끌려 올라가 스프레드가 희석된다 — 1차
        # 실행에서 +3%p 주입이 Δ1.15 로 찍힌 원인이다.
        drift = ((eff_up if gap > 0 else eff_down) / 100.0) if abs(gap) >= 4.0 else 0.0
        base[pos + 1:pos + 61] *= np.linspace(1, 1 + drift, min(60, m - pos - 1))
        # 갭 방향과 같은 쪽으로 시가를 벌린다 — 그래야 gap_held 가 상승·하락
        # 양쪽에서 True 가 되고, down_break 가 실제로 생긴다. 시가를 늘 종가보다
        # 낮게 두면 하락 갭이 전부 down_recovered 로 빠져 라벨 하나가 통째로
        # 검증되지 않는다(1차 실행에서 실제로 발생).
        opn_adj[pos] = -0.004 if gap > 0 else 0.004
        rep.append((d, {"epsActual": 1.0}))
        qends.append(pd.Timestamp(d) - pd.Timedelta(days=30))
        if k % 9 == 4:      # 분기말 흔적 사건 — pe_filter 가 잡아야 한다
            dd = idx[pos + 5]
            rep.append((dd, {"epsActual": 1.0}))
            qends.append(pd.Timestamp(dd) - pd.Timedelta(days=3))
    hist = pd.DataFrame({"Open": base * (1 + opn_adj), "High": base * 1.01,
                         "Low": base * 0.99, "Close": base, "Volume": vol}, index=idx)
    rep.sort(key=lambda x: x[0])
    return hist, rep, sorted(set(qends)), spy


def _draw_rows(rng, n_lab, n_mut, eff, code, sd=12.0, yshock_sd=1.0):
    """판정 게이트의 오탐률을 재기 위한 빠른 표본 생성기(가격 시뮬 없음).

    약정 §9 의 가정을 그대로 쓴다 — 60세션 초과수익 sd 12%, t(4) 꼬리,
    연도 × 라벨 충격 1%p. 파이프라인(build_events)은 T1·T3 가 따로 때린다.
    """
    ys = rng.normal(0, yshock_sd, len(JUDGE_YEARS))
    rows = []
    for k in range(n_lab):
        j = k % len(JUDGE_YEARS)
        rows.append({"code": code, "year": JUDGE_YEARS[j],
                     "x": float(rng.standard_t(4)) * sd / np.sqrt(2.0) + eff + ys[j]})
    for k in range(n_mut):
        j = k % len(JUDGE_YEARS)
        rows.append({"code": "muted", "year": JUDGE_YEARS[j],
                     "x": float(rng.standard_t(4)) * sd / np.sqrt(2.0)})
    return rows


def _pass_rate(eff, code, reps=120, seed=99) -> float:
    """두 티어 AND 로 판정이 통과하는 비율 — 약정 §9 표를 직접 재현한다."""
    rng = np.random.default_rng(seed)
    hit = 0
    for _ in range(reps):
        ok = True
        for n_all, n_lab in ((2530, 280), (1790, 195)):   # Tier 1 · Tier 2 추정
            r = judge_tier(_draw_rows(rng, n_lab, n_all - n_lab, eff, code), code)
            ok = ok and r["pass"]
        hit += 1 if ok else 0
    return hit / reps


def _rows_for(*, per_year=22, years_filled=10, val_ok=3.0, val_bad=-0.1,
              years_ok=10, u_per_year=40, u_val=0.0, spike=None,
              sd=0.01, seed=3):
    """judge_tier 입력을 직접 짓는다 — 게이트를 **하나씩만** 깨는 데 쓴다.

    ⚠️ U 는 L 을 포함한다(약정 §5). 그래서 L 값을 키우면 U 평균도 같이 올라가
       스프레드가 희석된다. 아래 기본값은 그 희석을 감안해 고른 것이다 —
       무심코 val_ok 를 낮추면 P2 가 딸려 실패해 '하나만 깨기'가 깨진다.
    """
    rng = np.random.default_rng(seed)
    rows, n = [], 0
    for k, y in enumerate(JUDGE_YEARS[:years_filled]):
        v = val_ok if k < years_ok else val_bad
        for _ in range(per_year):
            x = v + rng.normal(0, sd)
            if spike is not None:
                x = spike if n % 20 == 0 else -0.2
            rows.append({"code": "up_continue", "x": x, "year": y})
            n += 1
    for y in JUDGE_YEARS:
        for _ in range(u_per_year):
            rows.append({"code": "muted", "x": u_val + rng.normal(0, sd), "year": y})
    return rows


def _selftest() -> int:
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {_mark(bool(cond))} {name}{(' — ' + extra) if extra else ''}")

    print("\n[T0] 약정 대조 — 러너 상수 ↔ md")
    p = _md_path()
    if p:
        md = open(p, encoding="utf-8").read()
        a, lines = precommit_check(md)
        print("\n".join(lines))
        chk("약정 앵커 전부 일치", a)
        b, fl = frozen_check()
        print("\n".join(fl))
        chk("동결 상수 일치", b)
        # 뮤테이션: md 숫자를 바꾸면 반드시 실패해야 한다
        chk("md 문턱 변조 탐지",
            not precommit_check(md.replace("**≥ +1.5%p**", "**≥ +9.9%p**"))[0])
        chk("md 구간 변조 탐지",
            not precommit_check(md.replace("2012-01-01", "2009-01-01"))[0])
    else:
        chk("약정 파일 존재", False, f"{PRECOMMIT_FILE} 없음")

    print("\n[T1] 합성 세계 — 효과 없음 / up +6%p / down −6%p (전체 파이프라인)")
    worlds = {"효과0": (0.0, 0.0), "up+6": (6.0, 0.0), "down-6": (0.0, -6.0)}
    got = {}
    for name, (eu, ed) in worlds.items():
        rows = []
        for sd in range(16):         # 종목 16개 — P1(n ≥ 100)·P4(연 10건) 가 서도록
            hist, rep, qe, spy = _synth(seed=sd + 1, eff_up=eu, eff_down=ed)
            evs, _d1, _dj1 = build_events(hist, rep, qe)
            got_r, _d2 = attach_returns(evs, hist, spy)
            rows.extend(r for r in got_r if 2012 <= r["year"] <= 2021)
        got[name] = {c: judge_tier(rows, c) for c in TARGET_LABELS}
        u = got[name]["up_continue"]
        d = got[name]["down_break"]
        print(f"  {name:7} up Δ{_f(u['mean_d'])} (n={u['n_l']}) · "
              f"down Δ{_f(d['mean_d'])} (n={d['n_l']})")
    # 개별 문턱이 아니라 **판정 전체(P1~P4)**로 본다. 약정 §9 가 주장하는 성질이
    # 바로 이것이다 — 효과가 없으면 통과하지 못하고, 큰 효과는 통과한다.
    chk("up+6 세계에서 up_continue 통과", got["up+6"]["up_continue"]["pass"])
    chk("down-6 세계에서 down_break 통과", got["down-6"]["down_break"]["pass"])
    # 스프레드는 무조건 집합 U 대비다(§5). 한쪽 라벨에만 드리프트를 주면 U 가
    # 끌려 올라가 **반대 라벨의 스프레드가 반대로 움직인다** — 버그가 아니라
    # 지표의 정의다. 그래서 효과 없는 세계 하나로 오탐을 논하면 안 된다(T1b).
    chk("한쪽 효과가 반대 라벨 스프레드를 밀어낸다(정의대로)",
        got["up+6"]["down_break"]["mean_d"] < got["효과0"]["down_break"]["mean_d"])
    chk("두 라벨 모두 P1 급 표본이 생긴다",
        min(got[w][c]["n_l"] for w in got for c in TARGET_LABELS) >= P1_MIN_N)

    print("\n[T1b] 게이트 오탐률 — 약정 §9 표 재현 (두 티어 AND · 120회)")
    r0u = _pass_rate(0.0, "up_continue")
    r0d = _pass_rate(0.0, "down_break")
    r3u = _pass_rate(2.5, "up_continue")
    r3d = _pass_rate(-2.5, "down_break")
    print(f"  효과0   up {r0u:.0%} · down {r0d:.0%}")
    print(f"  ±2.5%p  up {r3u:.0%} · down {r3d:.0%}")
    chk("효과 0 에서 통과율 ≤ 5%", max(r0u, r0d) <= 0.05)
    chk("효과 ±2.5%p 에서 통과율 ≥ 50%", min(r3u, r3d) >= 0.50)

    print("\n[T2] 죽은 게이트 검사 — 조건을 하나씩만 깨뜨린다")
    def _one(name, want_fail, **kw):
        r = judge_tier(_rows_for(**kw), "up_continue")
        flags = {"p1": r["p1"], "p2": r["p2"], "p3": r["p3"], "p4": r["p4"]}
        good = (flags[want_fail] is False) and all(v for k, v in flags.items()
                                                   if k != want_fail)
        chk(name, good, f"n={r['n_l']} Δ{_f(r['mean_d'])} 중앙Δ{_f(r['med_d'])} "
                        f"연도 {r['years_ok']}/{r['years_valid']} "
                        + " ".join(f"{k.upper()}{_mark(v)}" for k, v in flags.items()))

    r = judge_tier(_rows_for(), "up_continue")
    chk("기준 입력은 P1~P4 전부 통과", r["pass"], f"n={r['n_l']} Δ{_f(r['mean_d'])}")
    _one("표본만 줄이면 P1 만 ❌", "p1", per_year=10, years_filled=9)
    _one("스프레드만 줄이면 P2 만 ❌", "p2", val_ok=0.5)
    _one("소수 대박이 끌면 P3 만 ❌", "p3", spike=100.0)
    _one("연도 5/10 이면 P4 만 ❌", "p4", val_ok=8.0, years_ok=5)

    print("\n[T3] 뮤테이션 — 약정을 어기면 숫자가 달라져야 한다")
    hist, rep, qe, spy = _synth(eff_up=2.0, eff_down=-2.0)
    b_ev, b_drop, b_dropj = build_events(hist, rep, qe)
    b_rows, _ = attach_returns(b_ev, hist, spy)
    b_up = judge_tier(b_rows, "up_continue")
    base_mean, base_n = b_up["mean_d"], len(b_rows)

    m1, _ = attach_returns(b_ev, hist, spy, entry_off=0)
    chk("M1 진입 C[i] (룩어헤드) 탐지",
        abs(judge_tier(m1, "up_continue")["mean_d"] - base_mean) > 1e-6)
    m2, _ = attach_returns(b_ev, hist, spy, horizon=59)
    chk("M2 청산 D+59 탐지",
        abs(judge_tier(m2, "up_continue")["mean_d"] - base_mean) > 1e-6)
    m3, _ = attach_returns(b_ev, hist, spy, use_spy=False)
    chk("M3 SPY 미차감 탐지",
        abs(judge_tier(m3, "up_continue")["mean_d"] - base_mean) > 1e-6)
    m4_ev, _, _ = build_events(hist, rep, qe, pe_filter=False)
    chk("M4 분기말 제외 해제 탐지", len(m4_ev) > len(b_ev),
        f"{len(b_ev)} → {len(m4_ev)}")
    m5_ev, _, _ = build_events(hist, rep, qe, warmup=False)
    chk("M5 워밍업 해제 탐지", len(m5_ev) > len(b_ev), f"{len(b_ev)} → {len(m5_ev)}")
    chk("M6 무조건 집합에서 라벨 제외 탐지",
        abs(judge_tier(b_rows, "up_continue", excl_label_from_u=True)["mean_d"]
            - base_mean) > 1e-6)

    print("\n[T4] 단위 — 사건 구성 규칙")
    chk("분기말 흔적 사건이 실제로 걸린다", b_drop["분기말흔적"] > 0,
        f"{b_drop['분기말흔적']}건")
    chk("워밍업이 앞쪽 사건을 소모한다", b_drop["워밍업부족"] > 0,
        f"{b_drop['워밍업부족']}건")
    chk("반응 세션 중복 없음", len({(e['i']) for e in b_ev}) == len(b_ev))
    t_today = date(2026, 9, 11)
    chk("2021-10-20 당일은 판정 밖",
        window_of(pd.Timestamp("2021-10-20"), t_today) == "display")
    chk("2021-10-19 는 판정 안",
        window_of(pd.Timestamp("2021-10-19"), t_today) == "judge")
    chk("2011-12-31 은 대상 아님", window_of(pd.Timestamp("2011-12-31"), t_today) == "")
    chk("오늘−99일 사건은 대상 아님",
        window_of(pd.Timestamp(t_today - timedelta(days=99)), t_today) == "")
    chk("판정 사건 수가 0 이 아니다", base_n > 0, f"{base_n}건")

    print("\n[T6] 종목 상태 분류 — 조회 실패 / 펀드 / 정상")
    # 첫 실행(2026-09-11 23:30 ET)이 여기서 걸렸다. ETF 17종목을 '조회 실패'로
    # 세어 Tier 1 실패율 16% → 무결성 중단. 진리표로 못 박는다.
    cases = [
        ("정상 종목", ("ok", "ok", "ok"), True, 80, 60, "", None, "ok"),
        ("ETF(실적 0 · 펀드 확인)", ("ok", "ok", "ok"), True, 0, 0, "ok", True, "fund"),
        ("실적 0 인데 펀드 아님", ("ok", "ok", "ok"), True, 0, 0, "ok", False, "fail"),
        ("실적 0 · 프로필 조회 실패", ("ok", "ok", "ok"), True, 0, 0, "http_error", None, "fail"),
        ("플랜 제한", ("ok", "plan_limited", "ok"), True, 80, 60, "", None, "fail"),
        ("레이트 리밋", ("rate_limited", "ok", "ok"), True, 80, 60, "", None, "fail"),
        ("가격 0봉", ("ok", "ok", "ok"), False, 80, 60, "", None, "fail"),
        ("분기재무만 0 · 펀드 확인", ("ok", "ok", "ok"), True, 80, 0, "ok", True, "fund"),
        ("분기재무만 0 · 펀드 아님", ("ok", "ok", "ok"), True, 80, 0, "ok", False, "fail"),
    ]
    for name, kinds, bars, nr, nq, pk, isf, want in cases:
        got = classify_status(kinds, bars, nr, nq, pk, isf)
        chk(f"{name} → {want}", got == want, f"결과 {got}")

    print("\n[T5] 회귀 — 결과 행 모양")
    per = {"judge": {"Tier 1": {c: judge_tier(b_rows, c) for c in TARGET_LABELS},
                     "Tier 2": {c: judge_tier(b_rows, c) for c in TARGET_LABELS}},
           "display": {}}
    rows = result_rows("2026-09-11 09:00",
                       per, {"univ": {"Tier 1": ["AAPL"], "Tier 2": ["MSFT"]},
                             "commit": "abc1234", "calls": "x", "note": "y"})
    chk("모든 행의 열 수가 헤더와 같다", all(len(r) == _NCOL for r in rows),
        f"{_NCOL}열 · {len(rows)}행")

    print("\n" + ("✅ 셀프테스트 전부 통과" if ok else "❌ 셀프테스트 실패"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else main())
