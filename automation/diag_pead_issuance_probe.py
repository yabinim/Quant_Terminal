#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_pead_issuance_probe.py — C 트랙 Phase 0 데이터 프로브 (읽기 전용 · 1회성)

무엇을 묻나
──────────
C 트랙 다음 두 후보가 **데이터 때문에** 성립하는지부터 확인한다. 가설은 여기서
검증하지 않는다.

  A) PEAD — 라이브 라벨(`ec.evaluate_pead`) 검증에 쓸 과거 실적 발표일
     A1  `earnings?symbol=` 이 과거로 얼마나 깊이 가는가, `limit` 이 먹는가
     A3  `includeReportTimes=true` 가 과거 행에도 장전/장후를 주는가
     A2  유니버스(Tier 1 보유·워치 + Tier 2 대형주 표본)의 판정·표시 구간 사건 수

  B) 순주식발행 — 시점 기준(point-in-time) 주식 수 이력
     B1~B3  `historical-market-capitalization` ÷ 종가 = 역산 주식 수
            · 시점 기준인가(현재 주식 수 × 과거 가격이면 비율이 늘 1.00)
            · 액면분할에서 끊기지 않는가
     B4  `income-statement` 의 weightedAverageShsOut 깊이·분할 조정 여부
     B5  `splits?symbol=` 제공 여부

⚠️ 이 스크립트는 **수익률을 한 줄도 계산하지 않는다.** 라벨도 매기지 않는다.
   사전 약정 전에 결과를 보면 약정이 무의미해지기 때문이다. 세는 것은 날짜·행 수·
   주식 수 비율뿐이다.

⚠️ 시트 **쓰기 0.** 읽기는 유니버스 티커 목록(Watchlist · Portfolios ·
   Earnings_Universe)뿐이다. GSPREAD_KEY 가 없으면 A2 를 건너뛴다(실패 아님).

판독 기준 — 실행 **전**에 적는다
──────────────────────────────
각 게이트는 "통과할 수 있고 실패할 수도 있는" 통계로 골랐다. 행 수처럼 파라미터가
무시돼도 그럴듯하게 나오는 숫자는 쓰지 않는다(2026-08 교훈: 카디널리티로 재라).

  [A-DEPTH]  아래 4종목 중 3종목 이상이 2010-01-01 이전 발표일을 가진다
             → 판정 구간(데이터 바닥 ~ 2021-10-20)이 약 12년 확보된다.
             미달이면 결정 4(미관찰 구간 판정)가 성립하지 않는다 → 재설계.
  [A-LIMIT]  `limit=1000` 의 가장 이른 발표일이 `limit=60` 보다 이르면 "limit 적용".
             (판정용이 아니라 러너 URL 결정용 — 가장 깊은 변형을 쓴다)
  [A-TIME]   알려진 장후 2종목(AAPL·MSFT) + 장전 2종목(JPM·WMT) 모두에서
             2021-10-20 이전 행의 채움률 ≥ 80% 이고 일치율 ≥ 80%.
             한 방향만 맞아도 안 된다 — 늘 같은 값을 주는 필드를 걸러내기 위해서다.
             미달이면 러너는 기존 거래량 추론(`ec.resolve_reaction_index`)을 쓴다.
  [B-MCAP]   네 조건 모두:
             ① AAPL 역산 주식 수 10년 비율 ≤ 0.85 (자사주 매입)
             ② TSLA 역산 주식 수 10년 비율 ≥ 1.15 (발행)
             ③ 창 안의 알려진 분할일 전후 5일 중앙값 비율이 모두 0.95~1.05
             ④ 종가 대비 시총 결측 ≤ 5%
             ①②를 둘 다 요구하는 이유: 현재 주식 수를 과거 가격에 곱한 가짜 이력이면
             두 비율이 모두 1.00 이 된다. 한쪽만 보면 우연히 통과할 수 있다.
  [B-STMT]   AAPL·NVDA 연간 행이 12년 이상이고, 연속 두 해의 주식 수 비율이
             1.8 배를 넘거나 0.55 배 미만인 점프가 없다(분할 미조정이면 4배·10배 점프).
  [B-PATH]   B-MCAP ✅ → 시총 경로 · 아니면 B-STMT ✅ → 재무제표 경로 ·
             둘 다 ❌ → 순주식발행은 "데이터 없음"으로 종료 후보.

호출량: 고정 31콜 + 유니버스(Tier 1 전 종목 + Tier 2 표본 T2_SAMPLE개).
         fmp_http 경유(A1 래칫 기준선 불변).

실행:  python automation/diag_pead_issuance_probe.py
       TICKERS=AAPL,MSFT  → Tier 1 을 시트 대신 이 목록으로
       T2_SAMPLE=60       → Tier 2 표본 크기(0 이면 Tier 2 생략)
"""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import date, timedelta

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import earnings_core as ec   # noqa: E402  — _d · _num · _timing_of · MIN_SAMPLE SSOT
import fmp_extras as fx      # noqa: E402  — 창(from/to) SSOT
import fmp_http as fh        # noqa: E402  — 호출·레이트리밋 SSOT

# ══════════════════════════════════════════════════════════════════════════
# 상수 — 판독 기준 (docstring 과 같은 값이어야 한다)
# ══════════════════════════════════════════════════════════════════════════
SEEN_FROM = "2021-10-20"
#   프리뷰 백테스트(2026-09-05)가 이미 본 표본의 시작일. 이 날 이후 사건은
#   '반응일종가 × D+1~D+20' 무조건 수치로 이미 관찰됐다 → 판정/표시 경계.

A_KNOWN_TIMING = {"AAPL": "amc", "MSFT": "amc", "JPM": "bmo", "WMT": "bmo"}
A_VARIANTS = [("bare", ""), ("limit=60", "&limit=60"), ("limit=1000", "&limit=1000")]
A_DEPTH_TARGET = "2010-01-01"
A_DEPTH_MIN_TICKERS = 3
A_TIME_MIN_FILL = 0.80
A_TIME_MIN_AGREE = 0.80

FLOOR_CAL_DAYS = 7277
#   가격 이력 바닥 근사. fmp_extras 주석의 실측(롤링 5,000 레코드 ≈ 7,277달력일).
#   사건 수 **표시용**이다 — 러너는 실제 수신 봉으로 다시 자른다.
POST_CAL_DAYS = 100
#   D+60 봉 ≈ 88달력일 + 여유. 표시 구간 끝을 자르는 근사.
T2_SAMPLE = max(0, int(os.environ.get("T2_SAMPLE", "60") or 60))
T2_SEED = 20260911

B_TICKERS = ["AAPL", "TSLA", "NVDA"]
B_SPLITS = {                      # 공개된 분할 발효일 — ③ 연속성 검사 대상
    "AAPL": ["2014-06-09", "2020-08-31"],
    "TSLA": ["2020-08-31", "2022-08-25"],
    "NVDA": ["2021-07-20", "2024-06-10"],
}
B_BASE_DAYS = 3653                # "10년 전" 기준일
B_BUYBACK_MAX = 0.85
B_ISSUE_MIN = 1.15
B_SPLIT_BAND = (0.95, 1.05)
B_MISSING_MAX = 0.05
B_STMT_TICKERS = ["AAPL", "NVDA"]
B_STMT_MIN_YEARS = 12.0
B_STMT_JUMP = (0.55, 1.8)
B_WIN = 5                         # 전후 중앙값 창(거래일)

TODAY = date.today()


# ══════════════════════════════════════════════════════════════════════════
# 공통
# ══════════════════════════════════════════════════════════════════════════
_CACHE: dict = {}


def _get(path: str):
    """(data, kind). 같은 경로는 한 번만 부른다."""
    if path not in _CACHE:
        data, _st, kind = fh.fmp_get_json_ex(path)
        _CACHE[path] = (data, kind)
    return _CACHE[path]


def _rows(data) -> list:
    if isinstance(data, dict):
        data = data.get("historical", data.get("data", []))
    return [r for r in (data or []) if isinstance(r, dict)] if isinstance(data, list) else []


def _mark(ok) -> str:
    return "✅" if ok is True else ("❌" if ok is False else "—")


def _reported(rows: list) -> list:
    """발표가 끝난 행: 날짜가 오늘 이전이고 epsActual 이 있다. 날짜 중복 제거."""
    out, seen = [], set()
    t = pd.Timestamp(TODAY)
    for r in rows:
        d = ec._d(r.get("date"))
        if d is None or d >= t or d in seen:
            continue
        if ec._num(r.get("epsActual")) is None:
            continue
        seen.add(d)
        out.append((d, r))
    out.sort(key=lambda x: x[0])
    return out


# ══════════════════════════════════════════════════════════════════════════
# A) PEAD 발표일
# ══════════════════════════════════════════════════════════════════════════
def probe_a1():
    print("=" * 76)
    print("A1) earnings?symbol= — 깊이와 limit 반응")
    print("=" * 76)
    res = {}
    for tk in A_KNOWN_TIMING:
        res[tk] = {}
        for name, q in A_VARIANTS:
            data, kind = _get(f"earnings?symbol={tk}{q}")
            rows = _rows(data)
            rep = _reported(rows)
            first = rep[0][0].strftime("%Y-%m-%d") if rep else "-"
            past_na = sum(1 for r in rows
                          if (ec._d(r.get("date")) or pd.Timestamp(TODAY)) < pd.Timestamp(TODAY)
                          and ec._num(r.get("epsActual")) is None)
            res[tk][name] = {"kind": kind, "rows": len(rows), "rep": len(rep),
                             "first": first, "past_na": past_na}
            print(f"  {tk:5} {name:11} kind={kind:12} 행 {len(rows):4} · 발표완료 {len(rep):4}"
                  f" · 최초 {first} · 과거인데 epsActual 없음 {past_na}")
        if res[tk]["bare"]["rows"]:
            smp = _rows(_get(f"earnings?symbol={tk}")[0])[0]
            print(f"        키: {sorted(smp.keys())}")

    # 가장 깊은 변형 — 가장 이른 발표일, 동률이면 행 수
    def _depth(name):
        firsts = [res[tk][name]["first"] for tk in res if res[tk][name]["first"] != "-"]
        return (min(firsts) if firsts else "9999", -sum(res[tk][name]["rows"] for tk in res))

    # 동률이면 limit=1000 → bare → limit=60 순. 이 4종목은 이력이 짧아 동률이어도
    # 유니버스의 오래된 종목(수십 년)에서는 기본 행 수에 잘릴 수 있다.
    best = min(("limit=1000", "bare", "limit=60"), key=_depth)
    best_q = dict(A_VARIANTS)[best]

    deep = [tk for tk in res if res[tk][best]["first"] != "-"
            and res[tk][best]["first"] < A_DEPTH_TARGET]
    depth_ok = len(deep) >= A_DEPTH_MIN_TICKERS

    moved = [tk for tk in res
             if res[tk]["limit=1000"]["first"] != "-" and res[tk]["limit=60"]["first"] != "-"
             and res[tk]["limit=1000"]["first"] < res[tk]["limit=60"]["first"]]
    limit_applied = len(moved) >= A_DEPTH_MIN_TICKERS
    print(f"\n  최심 변형: {best}  ·  {A_DEPTH_TARGET} 이전 발표 보유 {len(deep)}/4 {deep}")
    print(f"  limit 반응: limit=1000 이 limit=60 보다 이른 종목 {len(moved)}/4 → "
          f"{'적용됨' if limit_applied else '무시되거나 불명'}")
    return {"best": best, "best_q": best_q, "depth_ok": depth_ok, "deep": deep,
            "limit_applied": limit_applied}


def _timing_any(row: dict) -> str:
    """장전/장후 추출. 필드 이름을 모르므로 넓게 본다.

    ① ec._timing_of — 기존 SSOT 키(time·when·timing·hour·announcementTime)
    ② 이름에 time/hour/when 이 든 **다른** 키(예: reportTime)를 같은 규칙으로
    ③ ISO 타임스탬프('T' 와 ':' 포함)는 UTC 로 보고 ec._timing_from_utc
    넓게 보는 대신 판독은 알려진 장전·장후 종목 **양쪽** 일치율로 한다 —
    엉뚱한 필드를 집으면 한쪽이 틀린다.
    """
    t = ec._timing_of(row)
    if t:
        return t
    for k, v in row.items():
        if k in ("date", "lastUpdated") or not isinstance(v, str) or not v.strip():
            continue
        kl = k.lower()
        if "T" in v and ":" in v and ("time" in kl or "date" in kl):
            t = ec._timing_from_utc(v)
        elif any(s in kl for s in ("time", "hour", "when")):
            t = ec._timing_of({"time": v})
        else:
            t = ""
        if t:
            return t
    return ""


def probe_a3(best_q: str):
    print()
    print("=" * 76)
    print("A3) includeReportTimes=true — 과거 행의 장전/장후")
    print("=" * 76)
    cut = pd.Timestamp(SEEN_FROM)
    ok_all = True
    for tk, want in A_KNOWN_TIMING.items():
        data, kind = _get(f"earnings?symbol={tk}{best_q}&includeReportTimes=true")
        rows = _rows(data)
        rep = [(d, r) for d, r in _reported(rows) if d < cut]
        got = [_timing_any(r) for _, r in rep]
        filled = [g for g in got if g]
        fill = (len(filled) / len(rep)) if rep else 0.0
        agree = (sum(1 for g in filled if g == want) / len(filled)) if filled else 0.0
        ok = bool(rep) and fill >= A_TIME_MIN_FILL and agree >= A_TIME_MIN_AGREE
        ok_all = ok_all and ok
        extra = sorted({k for _, r in rep for k in r.keys()
                        if any(s in k.lower() for s in ("time", "hour", "when"))})
        smp = {k: rep[-1][1].get(k) for k in extra} if rep else {}
        print(f"  {tk:5} 기대 {want} · kind={kind:12} {SEEN_FROM} 이전 {len(rep):3}행 · "
              f"채움 {fill:5.1%} · 일치 {agree:5.1%}  {_mark(ok)}")
        print(f"        시각 후보 키 {extra} 샘플 {json.dumps(smp, ensure_ascii=False)[:160]}")
    return ok_all


def _load_universe():
    """(tier1, tier2) — 시트 읽기만. 실패하면 빈 목록."""
    env = str(os.environ.get("TICKERS", "") or "").strip()
    t1, t2 = set(), set()
    if env:
        t1 = {t.strip().upper() for t in env.split(",") if t.strip()}
    raw = os.environ.get("GSPREAD_KEY", "")
    if not raw:
        print("  [WARN] GSPREAD_KEY 없음 — 시트 유니버스 생략")
        return sorted(t1), []
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        gc = gspread.authorize(Credentials.from_service_account_info(
            json.loads(raw), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly",
                                     "https://www.googleapis.com/auth/drive.readonly"]))
        sh = gc.open("Quant_DB")
        if not env:
            for name, col in (("Watchlist", 1), ("Portfolios", 2)):
                try:
                    for r in (sh.worksheet(name).get_all_values() or [])[1:]:
                        if len(r) > col and str(r[col]).strip():
                            t1.add(str(r[col]).strip().upper())
                except Exception as e:
                    print(f"  [WARN] {name} 읽기 실패: {e}")
        try:
            for r in ec.parse_universe(sh.worksheet(ec.UNIVERSE_WORKSHEET).get_all_values()):
                t2.add(str(r.get("Ticker") or "").strip().upper())
        except Exception as e:
            print(f"  [WARN] {ec.UNIVERSE_WORKSHEET} 읽기 실패: {e}")
    except Exception as e:
        print(f"  [WARN] 시트 접근 실패: {e}")
    t2.discard("")
    return sorted(t1), sorted(t2 - t1)


def _count_events(tk: str, best_q: str) -> dict:
    data, kind = _get(f"earnings?symbol={tk}{best_q}")
    rep = _reported(_rows(data))
    floor = pd.Timestamp(TODAY - timedelta(days=FLOOR_CAL_DAYS))
    cut = pd.Timestamp(SEEN_FROM)
    end = pd.Timestamp(TODAY - timedelta(days=POST_CAL_DAYS))
    judge_raw = [d for d, _ in rep if floor <= d < cut]
    # expected_move 워밍업: 앞선 반응 MIN_SAMPLE 개가 있어야 라벨이 선다(보수적 차감)
    judge = judge_raw[ec.MIN_SAMPLE:]
    show = [d for d, _ in rep if cut <= d <= end]
    return {"kind": kind, "n_rep": len(rep), "judge": judge, "show": len(show),
            "first": rep[0][0] if rep else None}


def _summ_universe(label: str, tickers: list, best_q: str, scale: float = 1.0):
    stats = {tk: _count_events(tk, best_q) for tk in tickers}
    has = [tk for tk, s in stats.items() if s["n_rep"]]
    judge = sum(len(s["judge"]) for s in stats.values())
    show = sum(s["show"] for s in stats.values())
    with_j = [tk for tk, s in stats.items() if s["judge"]]
    kinds = {}
    for s in stats.values():
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    firsts = sorted(s["first"] for s in stats.values() if s["first"] is not None)
    med_first = firsts[len(firsts) // 2].strftime("%Y-%m-%d") if firsts else "-"
    years = {}
    for s in stats.values():
        for d in s["judge"]:
            years[d.year] = years.get(d.year, 0) + 1
    print(f"\n  [{label}] {len(tickers)}종목 · 실적 있음 {len(has)} · 응답 {kinds}")
    print(f"    판정 구간 사건(워밍업 {ec.MIN_SAMPLE}건 차감) {judge}건 · "
          f"1건 이상 {len(with_j)}종목 · 표시 구간 {show}건 · 최초 발표일 중앙값 {med_first}")
    if scale != 1.0:
        print(f"    → 표본 비율로 환산한 Tier 2 전체 추정: 판정 ≈ {judge * scale:.0f}건 · "
              f"표시 ≈ {show * scale:.0f}건")
    if years:
        print("    판정 구간 연도별: " + " ".join(f"{y}:{n}" for y, n in sorted(years.items())))
    shallow = sorted((s["first"], tk) for tk, s in stats.items() if s["first"] is not None)[-5:]
    if shallow:
        print("    가장 얕은 5종목: " + ", ".join(f"{tk}({d.strftime('%Y-%m')})" for d, tk in shallow))
    return {"n": len(tickers), "judge": judge, "show": show, "with_j": len(with_j),
            "scale": scale}


def probe_a2(best_q: str):
    print()
    print("=" * 76)
    print("A2) 유니버스 사건 수 — 판정 [바닥, %s) · 표시 [%s, 오늘-%d일]"
          % (SEEN_FROM, SEEN_FROM, POST_CAL_DAYS))
    print("=" * 76)
    t1, t2 = _load_universe()
    out = {}
    if t1:
        out["t1"] = _summ_universe("Tier 1 보유·워치", t1, best_q)
    else:
        print("  Tier 1 없음 — 생략")
    if t2 and T2_SAMPLE > 0:
        smp = sorted(random.Random(T2_SEED).sample(t2, min(T2_SAMPLE, len(t2))))
        out["t2"] = _summ_universe(f"Tier 2 표본 {len(smp)}/{len(t2)} (seed {T2_SEED})",
                                   smp, best_q, scale=len(t2) / max(1, len(smp)))
    elif t2:
        print(f"  Tier 2 {len(t2)}종목 — T2_SAMPLE=0 이라 생략")
    return out


# ══════════════════════════════════════════════════════════════════════════
# B) 순주식발행
# ══════════════════════════════════════════════════════════════════════════
def _series(rows: list, field: str) -> pd.Series:
    s = {}
    for r in rows:
        d = ec._d(r.get("date"))
        v = ec._num(r.get(field))
        if d is not None and v is not None and v > 0:
            s[d] = v
    return pd.Series(s, dtype=float).sort_index()


def _med_around(s: pd.Series, d: pd.Timestamp, before: bool):
    part = s[s.index < d].tail(B_WIN) if before else s[s.index >= d].head(B_WIN)
    return float(part.median()) if len(part) else None


def probe_b_mcap():
    print()
    print("=" * 76)
    print("B1~B3) historical-market-capitalization ÷ 종가 = 역산 주식 수")
    print("=" * 76)
    win = fx.hist_range_params(fx.HIST_MAX_DAYS)
    variants = [("bare", ""), ("window", win), ("window+limit=5000", win + "&limit=5000"),
                ("deep7400", fx.hist_range_params(7400))]
    #   deep7400: 가격의 롤링 5,000 레코드 상한을 일부러 넘긴 요청. 시총 엔드포인트에도
    #   같은 상한이 있는지(또는 더 짧은지) 본다 — 발행 백테스트 창 깊이의 상한이 된다.
    best, best_key = None, None
    for name, q in variants:
        data, kind = _get(f"historical-market-capitalization?symbol=AAPL{q}")
        s = _series(_rows(data), "marketCap")
        first = s.index[0] if len(s) else None
        print(f"  AAPL {name:18} kind={kind:12} 행 {len(s):5} · 최초 "
              f"{first.strftime('%Y-%m-%d') if first is not None else '-'}")
        # 가장 이른 최초일, 동률이면 행 수가 많은 쪽
        key = (first, -len(s)) if first is not None else None
        if key is not None and (best_key is None or key < best_key):
            best, best_key = (name, q), key
    if best is None:
        print("  [B-MCAP] ❌ 시총 이력 없음")
        return False, {}
    print(f"  → 최심 변형: {best[0]}")

    out, ok = {}, True
    base_day = pd.Timestamp(TODAY - timedelta(days=B_BASE_DAYS))
    for tk in B_TICKERS:
        mc = _series(_rows(_get(f"historical-market-capitalization?symbol={tk}{best[1]}")[0]),
                     "marketCap")
        px = _series(_rows(_get(f"historical-price-eod/full?symbol={tk}{win}")[0]), "close")
        if mc.empty or px.empty:
            print(f"  {tk}: 시총 {len(mc)} · 종가 {len(px)} — 산출 불가")
            ok = False
            continue
        lo, hi = max(mc.index[0], px.index[0]), min(mc.index[-1], px.index[-1])
        px_in = px[(px.index >= lo) & (px.index <= hi)]
        j = pd.concat([mc.rename("mc"), px.rename("px")], axis=1, join="inner")
        j = j[(j.index >= lo) & (j.index <= hi)]
        miss = 1.0 - (len(j) / len(px_in)) if len(px_in) else 1.0
        sh = (j["mc"] / j["px"]).dropna()
        tail = float(sh.tail(B_WIN).median()) if len(sh) else None
        base_part = sh[sh.index >= base_day].head(B_WIN)
        base = float(base_part.median()) if len(base_part) else None
        ratio = (tail / base) if (tail and base) else None
        rtxt = f"{ratio:.3f}" if ratio is not None else "-"
        print(f"  {tk:5} 겹침 {lo.strftime('%Y-%m-%d')}~{hi.strftime('%Y-%m-%d')} · "
              f"결측 {miss:5.1%} · 역산 주식수 {base or 0:,.0f} → {tail or 0:,.0f} "
              f"(10년 비율 {rtxt})")
        splits = []
        for ds in B_SPLITS.get(tk, []):
            d = pd.Timestamp(ds)
            if not (lo < d <= hi):
                print(f"        분할 {ds}: 창 밖 — 검사 생략")
                continue
            r_sh = [_med_around(sh, d, True), _med_around(sh, d, False)]
            r_mc = [_med_around(j["mc"], d, True), _med_around(j["mc"], d, False)]
            r_px = [_med_around(j["px"], d, True), _med_around(j["px"], d, False)]
            q_sh = (r_sh[1] / r_sh[0]) if all(r_sh) else None
            q_mc = (r_mc[1] / r_mc[0]) if all(r_mc) else None
            q_px = (r_px[1] / r_px[0]) if all(r_px) else None
            sok = q_sh is not None and B_SPLIT_BAND[0] <= q_sh <= B_SPLIT_BAND[1]
            splits.append(sok)
            print(f"        분할 {ds}: 주식수 후/전 {q_sh if q_sh is not None else float('nan'):.3f}"
                  f" · 시총 {q_mc if q_mc is not None else float('nan'):.3f}"
                  f" · 종가 {q_px if q_px is not None else float('nan'):.3f}  {_mark(sok)}")
        out[tk] = {"ratio": ratio, "miss": miss, "splits": splits}

    a, t = out.get("AAPL", {}), out.get("TSLA", {})
    c1 = a.get("ratio") is not None and a["ratio"] <= B_BUYBACK_MAX
    c2 = t.get("ratio") is not None and t["ratio"] >= B_ISSUE_MIN
    all_splits = [x for v in out.values() for x in v["splits"]]
    c3 = bool(all_splits) and all(all_splits)
    c4 = bool(out) and all(v["miss"] <= B_MISSING_MAX for v in out.values())
    ok = ok and c1 and c2 and c3 and c4
    print(f"\n  ① AAPL ≤ {B_BUYBACK_MAX} {_mark(c1)} · ② TSLA ≥ {B_ISSUE_MIN} {_mark(c2)} · "
          f"③ 분할 연속 {sum(all_splits)}/{len(all_splits)} {_mark(c3)} · "
          f"④ 결측 ≤ {B_MISSING_MAX:.0%} {_mark(c4)}")
    return ok, out


def probe_b_stmt():
    print()
    print("=" * 76)
    print("B4) income-statement — weightedAverageShsOut 깊이 · 분할 조정")
    print("=" * 76)
    ok = True
    for tk in B_STMT_TICKERS:
        for period, lim in (("annual", 30), ("quarter", 80)):
            data, kind = _get(f"income-statement?symbol={tk}&period={period}&limit={lim}")
            s = _series(_rows(data), "weightedAverageShsOut")
            yrs = ((pd.Timestamp(TODAY) - s.index[0]).days / 365.25) if len(s) else 0.0
            print(f"  {tk:5} {period:7} kind={kind:12} 행 {len(s):3} · 최초 "
                  f"{s.index[0].strftime('%Y-%m-%d') if len(s) else '-'} ({yrs:4.1f}년)")
            if period != "annual":
                continue
            jumps = []
            for i in range(1, len(s)):
                q = s.iloc[i] / s.iloc[i - 1]
                if q < B_STMT_JUMP[0] or q > B_STMT_JUMP[1]:
                    jumps.append(f"{s.index[i].strftime('%Y-%m')}×{q:.2f}")
            good = yrs >= B_STMT_MIN_YEARS and not jumps
            ok = ok and good
            print(f"        연속 연도 점프 {jumps if jumps else '없음'} · "
                  f"{B_STMT_MIN_YEARS:.0f}년 이상 {_mark(yrs >= B_STMT_MIN_YEARS)}  {_mark(good)}")
    return ok


def probe_b_splits():
    print()
    print("=" * 76)
    print("B5) splits?symbol= — 제공 여부")
    print("=" * 76)
    for tk in ("AAPL", "NVDA"):
        data, kind = _get(f"splits?symbol={tk}")
        rows = _rows(data)
        ds = sorted(str(r.get("date") or "")[:10] for r in rows)
        print(f"  {tk:5} kind={kind:12} 행 {len(rows):3} · 최근 {ds[-4:]}")


# ══════════════════════════════════════════════════════════════════════════
def main() -> int:
    if not fh.fmp_key():
        print("[ABORT] FMP_API_KEY 없음")
        return 2
    print(f"diag_pead_issuance_probe — {TODAY} · 읽기 전용 · 수익률 계산 없음")
    print(f"판정/표시 경계 {SEEN_FROM} · 가격 바닥 근사 오늘-{FLOOR_CAL_DAYS}일\n")

    a1 = probe_a1()
    a3 = probe_a3(a1["best_q"])
    a2 = probe_a2(a1["best_q"])
    b_mcap, _ = probe_b_mcap()
    b_stmt = probe_b_stmt()
    probe_b_splits()

    path = "시총 경로" if b_mcap else ("재무제표 경로" if b_stmt else "없음 → 순주식발행 종료 후보")
    print()
    print("=" * 76)
    print("요약 — 판독 기준은 docstring 에 실행 전 기록")
    print("=" * 76)
    print(f"[A-DEPTH] {_mark(a1['depth_ok'])} {A_DEPTH_TARGET} 이전 발표 보유 "
          f"{len(a1['deep'])}/4 (기준 {A_DEPTH_MIN_TICKERS}) · 최심 변형 {a1['best']}")
    print(f"[A-LIMIT] {'적용됨' if a1['limit_applied'] else '무시되거나 불명'}")
    print(f"[A-TIME]  {_mark(a3)} "
          f"{'includeReportTimes 사용 가능' if a3 else '→ 러너는 기존 거래량 추론(SSOT) 사용'}")
    for k, lab in (("t1", "Tier 1"), ("t2", "Tier 2")):
        if k in a2:
            v = a2[k]
            est = f" (전체 추정 ≈ {v['judge'] * v['scale']:.0f})" if v["scale"] != 1.0 else ""
            print(f"[A-UNIV]  {lab}: 판정 구간 {v['judge']}건{est} · "
                  f"{v['with_j']}/{v['n']}종목 · 표시 구간 {v['show']}건")
    print(f"[B-MCAP]  {_mark(b_mcap)}")
    print(f"[B-STMT]  {_mark(b_stmt)}")
    print(f"[B-PATH]  {path}")
    print(fh.fmp_stats_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
