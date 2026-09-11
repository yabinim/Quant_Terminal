#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_pead_date_validity.py — C 트랙 Phase 0 · A4: 실적 발표일 유효성 (읽기 전용 · 1회성)

왜 필요한가
──────────
Phase 0 프로브(diag_pead_issuance_probe, 2026-09-11)의 A-DEPTH 는 ✅ 였지만 그 게이트는
"날짜가 있는가"만 쟀다. 필요한 것은 "그 날짜가 **실제 발표일인가**"다.
  · 네 종목의 최초 행이 전부 1985-09-30 / 1985-10-31 — 분기말이다.
  · MSFT 는 1986-03-13 상장이다. 1985-09-30 행은 발표일일 수 없다.
  · 반면 2012 년 행은 실제 발표일과 맞았다(JPM 01-13 · MSFT 01-19 · AAPL 01-24 · WMT 02-21).
→ 어딘가에서 날짜 의미가 '분기말'에서 '발표일'로 바뀐다. 판정 구간(가격 바닥 2006-10 ~
  2021-10-20)의 앞부분이 위험하다. 분기말로 반응을 재면 사건 없는 날에 라벨이 붙는다.

이 스크립트는 PEAD 사전 약정의 **판정 구간 시작 연도를 기계적으로** 정한다.
사람이 연도를 고르지 않는다.

무엇을 재나 — 사건마다 두 가지 (둘 다 종가를 쓰지 않는다)
────────────────────────────────────────────────
  ① 분기말 흔적: (발표일 − 직전 분기말) ≤ PE_LAG_MAX 일.
     분기말 = income-statement 분기 `date`.
     ⚠️ 설계 대화에서는 '2일 이하'였다. 실행 전에 7일로 넓혔다 — FMP 실적 행의 분기말이
        달력 월말(09-30)이고 재무제표가 회계 토요일(AAPL 09-27)이면 3일 차이가 나서
        2일 규칙을 빠져나간다. 대형주 최단 발표 지연은 약 10일(은행 약 2주)이므로
        7일 이내 발표는 사실상 없다. 판정 결과를 보기 전의 조임이다.
  ② 거래량 급증: 반응 세션 거래량 ÷ 직전 VOLUME_BASELINE_BARS(20) 봉 중앙값 ≥ 1.5.
     반응 세션 = ec.resolve_reaction_index(hist, 날짜, "") — 러너가 쓸 바로 그 SSOT.
     (A-TIME ❌ 이라 러너는 시각 미상 → D·D+1 중 거래량 큰 쪽.) 비율 공식은
     ec.measure_reaction 432~440행과 같다. 가격 프레임에 **Volume 열만** 넣는다 —
     종가를 실수로라도 읽을 수 없게.
     ①만으로는 날짜가 10-Q 공시일(분기말+40일쯤, 반응 이후)인 경우를 못 거른다.

판독 기준 — 실행 **전**에 적는다  [A4-YEAR]
──────────────────────────────────────
  티어(Tier 1 보유·워치 / Tier 2 대형주 표본)마다 따로 판정한다.
  · 기준 = 2022~2025 각 연도 급증률의 중앙값(표시 구간 = 내부 대조군. 절대 수준을 몰라도 된다)
  · 연도 Y **유효** ⇔ n ≥ 30  그리고  분기말 흔적률 ≤ 5%  그리고  급증률 ≥ 0.8 × 기준
  · 티어 시작 연도 = Y 부터 2021 까지 **모든 해가 유효**한 가장 이른 Y (연속 조건 — 골라 담기 금지)
    2021 이 무효면 그 티어는 판정 구간 없음.
  · **판정 시작 연도 = 두 티어 시작 연도 중 늦은 쪽.** 유니버스 결정(약정)과 무관하게 성립하도록.
    한 티어라도 '없음'이면 결과는 '없음' → PEAD 재설계.
  · 추가로 사건 단위 규칙을 러너에 넘긴다: 유효 연도 안에서도 ①분기말 흔적 사건은 개별 제외.
    (가격 정보를 쓰지 않는 규칙이라 결과와 독립)

⚠️ 결과값(D+60 초과수익)은 여전히 한 줄도 계산하지 않는다. 거래량 비율은 라벨의 거래량 조건
   (≥ 2.0배)과 재료가 겹치므로 **연도별 집계만** 출력하고 사건별 값은 출력하지 않는다.
⚠️ 시트 쓰기 0. 유니버스는 diag_pead_issuance_probe 와 같은 함수 · 같은 seed 로 뽑는다.
   Earnings_Universe 는 주 1회 갱신되므로 A2 와 표본이 몇 종목 다를 수 있다.

호출량: 실적 있는 종목당 3콜(실적 · 분기 재무제표 · 가격) + 실적 없는 종목당 1콜.
         Tier 1 약 106 + Tier 2 표본 60 → 약 470콜 · 약 3분.

실행:  python automation/diag_pead_date_validity.py
       T2_SAMPLE=60 · TICKERS=... 는 diag_pead_issuance_probe 와 같은 의미
"""
from __future__ import annotations

import os
import random
import sys
from datetime import date, timedelta

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import diag_pead_issuance_probe as P   # noqa: E402  — 유니버스 · seed · 행 파서 · 경계일 SSOT
import earnings_core as ec              # noqa: E402  — resolve_reaction_index · VOLUME_BASELINE_BARS
import fmp_extras as fx                 # noqa: E402  — 창(from/to) SSOT
import fmp_http as fh                   # noqa: E402

# ══════════════════════════════════════════════════════════════════════════
# 판독 기준 — docstring 과 같은 값이어야 한다
# ══════════════════════════════════════════════════════════════════════════
SEEN_FROM = P.SEEN_FROM              # 2021-10-20 — 판정/표시 경계
PE_LAG_MAX = 7                       # ① 분기말 흔적 (일)
PE_RATE_MAX = 0.05
SPIKE_MIN = 1.5                      # ② 급증 배수
SPIKE_REL_MIN = 0.80
BASE_YEARS = (2022, 2023, 2024, 2025)
MIN_YEAR_N = 30
LAST_JUDGE_YEAR = 2021
POST_FILING_SLACK = 5                # 참고용: 발표일이 10-Q 공시일 + 5일보다 늦은 비율
PRICE_WINDOW_DAYS = 7400             # FMP 롤링 5,000 레코드 상한을 일부러 넘긴 요청
POST_CAL_DAYS = P.POST_CAL_DAYS      # 오늘-100일 이후 사건은 제외(D+60 미완)

TODAY = date.today()


# ══════════════════════════════════════════════════════════════════════════
# 데이터 — 가격은 캐시하지 않는다(166종목 × 5,000행 dict 는 메모리를 태운다)
# ══════════════════════════════════════════════════════════════════════════
def _json(path: str):
    data, _st, kind = fh.fmp_get_json_ex(path)
    return data, kind


def _volume_frame(tk: str) -> pd.DataFrame:
    data, _k = _json(f"historical-price-eod/full?symbol={tk}"
                     f"{fx.hist_range_params(PRICE_WINDOW_DAYS)}")
    v = {}
    for r in P._rows(data):
        d = ec._d(r.get("date"))
        x = ec._num(r.get("volume"))
        if d is not None and x is not None:
            v[d] = x
    if not v:
        return pd.DataFrame()
    return pd.DataFrame({"Volume": pd.Series(v, dtype=float)}).sort_index()


def _quarters(tk: str) -> list:
    """[(분기말, 공시일|None)] 오름차순."""
    data, _k = _json(f"income-statement?symbol={tk}&period=quarter&limit=80")
    out = []
    for r in P._rows(data):
        pe = ec._d(r.get("date"))
        if pe is None:
            continue
        out.append((pe, ec._d(r.get("filingDate") or r.get("acceptedDate"))))
    return sorted(set(out), key=lambda x: x[0])


def _vol_ratio(hist: pd.DataFrame, i: int):
    """ec.measure_reaction 432~440행과 같은 공식. 기준 봉이 다 차야 한다(i ≥ 20)."""
    nb = int(ec.VOLUME_BASELINE_BARS)
    if i is None or i < nb:
        return None
    base = pd.to_numeric(hist["Volume"].iloc[i - nb:i], errors="coerce").dropna()
    v = ec._num(hist["Volume"].iloc[i])
    if v is None or base.empty:
        return None
    med = float(base.median())
    return (v / med) if med > 0 else None


def ticker_events(tk: str) -> dict:
    """사건 목록(메모리 안에서만). 반환: {has_earn, events:[{year, date, lag, pe, spike, post_filing}]}"""
    data, _k = _json(f"earnings?symbol={tk}&limit=1000")
    rep = P._reported(P._rows(data))
    if not rep:
        return {"has_earn": False, "events": []}
    quarters = _quarters(tk)
    hist = _volume_frame(tk)
    if hist.empty:
        return {"has_earn": True, "events": []}
    end = pd.Timestamp(TODAY - timedelta(days=POST_CAL_DAYS))
    first_bar = hist.index[0]
    pes = pd.DatetimeIndex([q[0] for q in quarters])
    evs = []
    for d, _row in rep:
        if d < first_bar or d > end:
            continue
        i = ec.resolve_reaction_index(hist, d, "")
        r = _vol_ratio(hist, i)
        if r is None:
            continue
        k = int(pes.searchsorted(d, side="right")) - 1 if len(pes) else -1
        if k < 0:
            continue                          # 직전 분기말이 없다 — 짝 없음
        pe_date, filing = quarters[k]
        lag = int((d - pe_date).days)
        evs.append({
            "year": int(d.year), "date": d, "lag": lag,
            "pe": lag <= PE_LAG_MAX,
            "spike": r >= SPIKE_MIN,
            "post_filing": (None if filing is None
                            else bool(d > filing + pd.Timedelta(days=POST_FILING_SLACK))),
        })
    return {"has_earn": True, "events": evs}


# ══════════════════════════════════════════════════════════════════════════
# 판정 — 순수 함수 (하네스·뮤테이션 대상)
# ══════════════════════════════════════════════════════════════════════════
def year_stats(events: list) -> dict:
    ys = {}
    for e in events:
        s = ys.setdefault(e["year"], {"n": 0, "pe": 0, "spike": 0, "pf": 0, "pf_n": 0, "lags": []})
        s["n"] += 1
        s["pe"] += int(e["pe"])
        s["spike"] += int(e["spike"])
        if e["post_filing"] is not None:
            s["pf_n"] += 1
            s["pf"] += int(e["post_filing"])
        s["lags"].append(e["lag"])
    for s in ys.values():
        s["pe_rate"] = s["pe"] / s["n"]
        s["spike_rate"] = s["spike"] / s["n"]
        s["pf_rate"] = (s["pf"] / s["pf_n"]) if s["pf_n"] else None
        s["lag_med"] = float(pd.Series(s["lags"]).median())
    return ys


def baseline(ys: dict):
    vals = [ys[y]["spike_rate"] for y in BASE_YEARS if y in ys and ys[y]["n"] >= MIN_YEAR_N]
    return float(pd.Series(vals).median()) if vals else None


def year_valid(s: dict, base) -> bool:
    return (base is not None and s["n"] >= MIN_YEAR_N
            and s["pe_rate"] <= PE_RATE_MAX
            and s["spike_rate"] >= SPIKE_REL_MIN * base)


def start_year(ys: dict):
    """Y..2021 이 **전부** 유효한 가장 이른 Y. 2021 이 무효(또는 없음)면 None."""
    base = baseline(ys)
    if base is None:
        return None
    start = None
    for y in range(LAST_JUDGE_YEAR, min(ys) - 1 if ys else LAST_JUDGE_YEAR, -1):
        if y in ys and year_valid(ys[y], base):
            start = y
        else:
            break
    return start


def combine(starts: dict):
    """두 티어 중 늦은 쪽. 하나라도 None 이면 None."""
    vals = list(starts.values())
    if not vals or any(v is None for v in vals):
        return None
    return max(vals)


def judge_count(events: list, start) -> dict:
    """판정 구간 [start-01-01, SEEN_FROM) 사건 수 — 분기말 흔적 개별 제외 전·후 (워밍업 차감 전)."""
    if start is None:
        return {"raw": 0, "kept": 0}
    lo, hi = pd.Timestamp(f"{start}-01-01"), pd.Timestamp(SEEN_FROM)
    inw = [e for e in events if lo <= e["date"] < hi]
    return {"raw": len(inw), "kept": sum(1 for e in inw if not e["pe"])}


# ══════════════════════════════════════════════════════════════════════════
def _universe() -> dict:
    t1, t2 = P._load_universe()
    out = {"Tier 1": t1}
    if t2 and P.T2_SAMPLE > 0:
        out["Tier 2"] = sorted(random.Random(P.T2_SEED).sample(t2, min(P.T2_SAMPLE, len(t2))))
    return out


def _print_tier(label: str, tickers: list, per: dict, ys: dict):
    base = baseline(ys)
    n_earn = sum(1 for tk in tickers if per[tk]["has_earn"])
    n_ev = sum(len(per[tk]["events"]) for tk in tickers)
    print(f"\n  [{label}] {len(tickers)}종목 · 실적 있음 {n_earn} · 측정 사건 {n_ev}")
    print("    연도    n   분기말흔적   급증률   공시후   지연중앙   판정")
    for y in sorted(ys):
        s = ys[y]
        pf = f"{s['pf_rate']:6.1%}" if s["pf_rate"] is not None else "     -"
        tag = ""
        if y <= LAST_JUDGE_YEAR:
            tag = "유효" if year_valid(s, base) else "무효"
        elif y in BASE_YEARS:
            tag = "기준"
        print(f"    {y}  {s['n']:4}   {s['pe_rate']:7.1%}   {s['spike_rate']:6.1%}   {pf}"
              f"   {s['lag_med']:6.0f}일   {tag}")
    if base is not None:
        print(f"    기준(2022~2025 급증률 중앙) {base:.1%} → 문턱 {SPIKE_REL_MIN * base:.1%}")
    else:
        print("    기준 산출 불가 — 2022~2025 에 n ≥ 30 인 해가 없다")
    # 종목별 전환 연도 = 분기말 흔적이 마지막으로 나온 해 + 1 (없으면 '처음부터')
    sw = {}
    for tk in tickers:
        evs = per[tk]["events"]
        if not evs:
            continue
        pes = [e["year"] for e in evs if e["pe"]]
        key = (max(pes) + 1) if pes else "처음부터"
        sw[key] = sw.get(key, 0) + 1
    if sw:
        items = sorted(sw.items(), key=lambda x: (isinstance(x[0], str), x[0]))
        print("    종목별 전환 연도(분기말 흔적 마지막 해+1): "
              + " ".join(f"{k}:{v}" for k, v in items))


def main() -> int:
    if not fh.fmp_key():
        print("[ABORT] FMP_API_KEY 없음")
        return 2
    print(f"diag_pead_date_validity — {TODAY} · 읽기 전용 · 종가·수익률 미사용 · 사건별 값 미출력")
    print(f"분기말 흔적 ≤ {PE_LAG_MAX}일 · 급증 ≥ {SPIKE_MIN}배(기준 {ec.VOLUME_BASELINE_BARS}봉 중앙값)"
          f" · 연도 유효: n ≥ {MIN_YEAR_N}, 흔적 ≤ {PE_RATE_MAX:.0%}, 급증률 ≥ {SPIKE_REL_MIN} × 기준")
    print("=" * 76)
    print("A4) 발표일 유효성 — 연도별 집계")
    print("=" * 76)

    tiers = _universe()
    starts, counts = {}, {}
    for label, tickers in tiers.items():
        if not tickers:
            print(f"\n  [{label}] 없음 — 생략")
            continue
        per = {tk: ticker_events(tk) for tk in tickers}
        events = [e for tk in tickers for e in per[tk]["events"]]
        ys = year_stats(events)
        _print_tier(label, tickers, per, ys)
        starts[label] = start_year(ys)
        counts[label] = events
        print(f"    → 시작 연도: {starts[label] if starts[label] is not None else '없음'}")

    final = combine(starts)
    print()
    print("=" * 76)
    print("요약 — 판독 기준은 docstring 에 실행 전 기록")
    print("=" * 76)
    for label, st in starts.items():
        print(f"[A4-{label.replace(' ', '')}] 시작 연도 {st if st is not None else '없음'}")
    if final is None:
        print("[A4-YEAR] ❌ 판정 구간 없음 → PEAD 재설계")
    else:
        print(f"[A4-YEAR] 판정 시작 연도 {final} (두 티어 중 늦은 쪽) → "
              f"판정 구간 [{final}-01-01, {SEEN_FROM})")
        for label, events in counts.items():
            c = judge_count(events, final)
            print(f"[A4-EVENTS] {label}: 판정 구간 {c['raw']}건 → 분기말 흔적 개별 제외 후 "
                  f"{c['kept']}건 (워밍업 차감 전)")
    print(fh.fmp_stats_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
