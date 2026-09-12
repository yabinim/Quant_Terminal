#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_issuance_probe.py — C 트랙 순주식발행 Phase 0 데이터 프로브 (읽기 전용 · 1회성)

무엇을 묻나
──────────
순주식발행(자사주 매입 / 희석)을 **개별 종목 플래그**로 만들 수 있는지를,
가설을 건드리기 전에 데이터 쪽에서만 확인한다.

  적용 지점(2026-09-12 확정) — 개별 종목 진단 · 워치리스트 플래그.
    위성 후보 풀은 섹터·테마 ETF 약 60개다(SATELLITE_MANDATE §2). ETF 의 주식 수
    변동은 설정/환매 flow 이지 희석이 아니므로 **순주식발행은 위성 룰 후보가 아니다.**
    PEAD 라벨과 같은 모양(문구가 예측을 주장하는 배지)으로 간다.

  기존 `diag_pead_issuance_probe` B 섹션과의 관계 — 그쪽은 종목 3개(AAPL·TSLA·NVDA)
    에서 "시총÷종가가 시점 기준인가"만 봤다. 횡단면 정렬에 쓰려면 **유니버스 규모**
    에서 다시 재야 하고, 그 프로브가 던지지 않은 질문이 하나 있다 — 두 데이터 경로
    중 어느 쪽을 쓸 것인가.

  경로 M  시총 ÷ 종가 = 역산 주식 수 (일간 · 지연 처리 불명 → 래그를 **실측**해야 함)
  경로 S  income-statement 의 주식 수 + filingDate (분기 · 지연이 구조적으로 해결)

⚠️ 이 스크립트는 **수익률을 한 줄도 계산하지 않는다.** 종가는 시총의 **분모**로만
   쓴다. 두 날짜의 종가 비율(=수익률)은 어디에서도 계산하지 않는다. 사전 약정 전에
   결과를 보면 약정이 무의미해지기 때문이다. 세는 것은 날짜·행 수·주식 수 비율뿐이다.

⚠️ 시트 **쓰기 0.** 읽기는 유니버스 티커 목록(Watchlist · Portfolios ·
   Earnings_Universe)뿐이다. GSPREAD_KEY 가 없으면 TICKERS 환경변수로만 돈다.

판독 기준 — 실행 **전**에 적는다
──────────────────────────────
각 게이트는 "통과할 수 있고 실패할 수도 있는" 통계로 골랐다. 행 수처럼 파라미터가
무시돼도 그럴듯하게 나오는 숫자는 쓰지 않는다(2026-08 교훈: 카디널리티로 재라).
분포를 보고 문턱을 고치지 않는다 — 여기서 정하는 것은 **경로**지 가설 문턱이 아니다.

  [I-COVER]  경로별로, 펀드가 아닌 표본 종목 중 START_TARGET(2012-01-01) 이전까지
             주식 수 이력이 닿는 비율이 **≥ 80%**.
             미달이면 그 경로는 2012~2021 판정 구간을 덮지 못한다.
  [I-LAG]    경로 M 의 주식 수 **계단**(일간 변화 ≥ 0.3%)을 **그 계단이 속한 분기**에
             귀속시키고, 그 분기의 filingDate 와 비교한다. filing 에서 가장 가까운
             계단을 고르면 안 된다 — 계단이 분기마다 서므로 어떤 창을 잡아도 늘
             무언가 잡히고, 측정된 지연이 분기 주기로 접힌다(셀프테스트 T3).
             두 조건 모두:
             ① 대응률 ≥ 60% — 분기의 그만큼에서 계단이 실제로 검출된다
             ② 필요 래그 ≤ 120일 — 계단의 90% 가 `계단일 + 래그 ≥ filingDate` 를
                만족하는 최소 래그. 이 값이 그대로 러너의 래그 상수가 된다
             ②가 크다는 것은 FMP 가 주식 수를 분기말로 소급 반영했다는 뜻이다.
             120일을 넘으면 신호가 너무 낡은 정보가 되어 경로 M 은 탈락한다.
  [I-NOISE]  경로 M 에서 분할 전후(±7일)와 계단일을 뺀 날의 일간 |Δ주식수|:
             무변화일(|Δ| < 0.01%) 비율 ≥ 50% **이고**, 무변화가 아닌 날의
             중앙 |Δ| ≤ 0.05%. 두 조건은 서로 다른 고장을 잡는다 — 앞은 '계단이
             아니라 보간됐나', 뒤는 '흔들릴 때 12개월 변화율을 덮을 만큼 흔들리나'.
             매끄러우면(계단이 아니면) 보간된 이력이다 — 래그 설계가 성립하지 않는다.
             시총 규모에 따라 반올림 잡음이 커질 수 있어 3분위로도 보고한다.
  [I-ARTIF]  분할로 설명되지 않는 분기 점프(>25%) 비율이 종목-분기의 **≤ 2%**.
             초과하면 세정 규칙(윈저화·제외)을 사전 약정에 넣어야 한다.
  [I-DISP]   과거 기준일 3개(2014-06-30 · 2018-06-30 · 2021-06-30)에서 12개월 주식 수
             변화율의 **횡단면 분포**. 기준일 3개 중 **2개 이상**에서
             IQR ≥ 2%p **이고** ±3% 밖 비율 ≥ 15%.
             분산이 없으면 정렬할 것이 없다. 독립변수 분포만 본다 — 수익률 아님.
  [I-BREADTH] Earnings_Universe 전체 종목 수와 표본 커버율로 환산한 사용 가능 종목 수.
             보고 전용. 유니버스 크기는 약정 시점에 검정력 계산과 함께 정한다.
  [I-COST]   종목당 실측 콜 수. 보고 전용 — 러너 콜 예산의 근거.
  [I-PATH]   경로 결정. 실행 전 고정:
             · 경로 M 자격 = I-COVER(M) ✅ + I-LAG ✅ + I-NOISE ✅ + I-ARTIF(M) ✅
             · 경로 S 자격 = I-COVER(S) ✅ + filingDate 존재율 ≥ 90% + I-ARTIF(S) ✅
             · **둘 다 자격이면 M 을 쓴다.** M 은 실제 발행주식수를, S 는 가중평균
               주식수(평활·희석 기준)를 잰다. 이상현상이 말하는 양은 전자이고, M 의
               유일한 약점(지연)은 I-LAG 가 측정한 래그로 닫히기 때문이다
             · M 만 자격 → M · S 만 자격 → S(래그는 filingDate)
             · 둘 다 실격 → **순주식발행 종료 후보** — 약정 없이 여기서 끝낸다
             어느 경우든 I-DISP 가 채택 경로에서 ❌ 면 종료 후보다

호출량: 표본 종목당 최대 5콜(시총 · 종가 · 분기 손익 · 분할 · 필요시 profile).
        기본 표본 40종목 → 약 160~200콜. fmp_http 경유(A1 래칫 기준선 불변).

실행:  python automation/diag_issuance_probe.py
       S1_SAMPLE=20  → Tier 1(보유·워치)에서 뽑을 표본 수
       S2_SAMPLE=20  → Tier 2(Earnings_Universe − Tier 1)에서 뽑을 표본 수
       TICKERS=A,B   → 시트 대신 이 목록을 Tier 1 로
"""
from __future__ import annotations

import math
import os
import random
import sys
from datetime import date, timedelta

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import earnings_core as ec      # noqa: E402  — _d · _num SSOT
import fmp_extras as fx         # noqa: E402  — 창(from/to) SSOT
import fmp_http as fh           # noqa: E402  — 호출·레이트리밋 SSOT
import diag_pead_issuance_probe as P   # noqa: E402
#   _get(캐시·콜 집계) · _rows · _series · _mark · _load_universe 를 그대로 쓴다.
#   유니버스 정의를 두 벌로 만들면 나중에 한쪽만 바뀐다 — 그 실패는 로그를 남기지 않는다.

# ══════════════════════════════════════════════════════════════════════════
# 상수 — 판독 기준 (docstring 과 같은 값이어야 한다)
# ══════════════════════════════════════════════════════════════════════════
S1_SAMPLE = max(0, int(os.environ.get("S1_SAMPLE", "20") or 20))
S2_SAMPLE = max(0, int(os.environ.get("S2_SAMPLE", "20") or 20))
SEED = 20260912
#   PEAD 의 T2_SEED(20260911) 를 재사용하지 않는다. 표본의 목적이 다르고(사건 수 vs
#   주식 수 이력), 여기서 뽑힌 종목은 결과에 그대로 기록돼 약정이 참조할 수 있다.

START_TARGET = "2012-01-01"      # I-COVER 목표 시작일 — PEAD 판정 구간 시작과 같다
COVER_MIN = 0.80

STEP_MIN = 0.003                 # 계단 판정: 일간 |Δ주식수| / 직전
STEP_CLUSTER_DAYS = 3            # 이 안에 붙은 계단은 하나로 본다(가장 큰 것)
LAG_MATCH_WIN = 90               # 마지막 분기의 귀속 구간 길이(달력일) — 꼬리 처리용
LAG_MATCH_MIN = 0.60             # 대응률 하한
LAG_COVER_Q = 0.90               # 계단의 이 비율이 '알 수 있었다'가 되게 하는 래그
I_LAG_MAX_DAYS = 120             # 필요 래그 상한 — 넘으면 경로 M 탈락

NOISE_WIGGLE_MAX = 0.0005        # 0.05% — **무변화가 아닌 날**의 중앙 |Δ|
#   전체 날의 중앙값으로 재면 이 게이트는 죽는다: 무변화일이 50% 를 넘으면 중앙값은
#   자동으로 0 이 되어 NOISE_FLAT_MIN 과 AND 로 묶는 순간 절대 걸리지 않는다
#   (뮤테이션 M8 생존으로 드러남). '흔들릴 때 얼마나 흔들리나'를 따로 잰다.
NOISE_FLAT_EPS = 0.0001          # 0.01% 미만이면 '무변화'
NOISE_FLAT_MIN = 0.50
SPLIT_GUARD_DAYS = 7             # 분할 전후 제외(달력일)

ART_JUMP = 0.25                  # 분기 점프 문턱
ART_MAX = 0.02                   # 종목-분기 대비 허용 비율

DISP_ASOF = ["2014-06-30", "2018-06-30", "2021-06-30"]
DISP_IQR_MIN = 0.02
DISP_TAIL_BAND = 0.03
DISP_TAIL_MIN = 0.15
DISP_MIN_ASOF = 2                # 기준일 3개 중 몇 개가 통과해야 하나

STMT_FILING_MIN = 0.90           # filingDate 존재율
STMT_LIMIT = 80                  # 분기 행 수 — 2006 까지 닿는다(2026-09-11 실측)
AT_WIN = 5                       # 기준일 값: 그 이하 마지막 N 관측의 중앙값

TODAY = date.today()
WIN = fx.hist_range_params(fx.HIST_MAX_DAYS)
#   fx.HIST_MAX_DAYS = 5478(15년) 는 정책 상수다. 여기서 우회하지 않는다 —
#   바닥이 2011 년대 초반이라 START_TARGET(2012-01-01) 을 여유 있게 덮는다.


# ══════════════════════════════════════════════════════════════════════════
# 공통
# ══════════════════════════════════════════════════════════════════════════
def _q(vals: list, p: float):
    """오름차순 분위수. 빈 목록이면 None."""
    xs = sorted(v for v in vals if v is not None and not _isnan(v))
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    pos = max(0.0, min(1.0, p)) * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def _isnan(v) -> bool:
    try:
        return bool(math.isnan(float(v)))
    except Exception:
        return False


def _at(s: pd.Series, d: pd.Timestamp):
    """기준일 이하 마지막 AT_WIN 관측의 중앙값. 하루짜리 튐을 피한다."""
    part = s[s.index <= d].tail(AT_WIN)
    return float(part.median()) if len(part) else None


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:+.2f}%"


def _calls() -> int:
    """누적 FMP 콜 수. fmp_stats 에 합계 키가 없어 fmp_stats_line 과 같은 식으로 센다."""
    s = fh.fmp_stats()
    return int(s["ok"] + s["rate_limited"] + s.get("plan_limited", 0)
               + s["http_error"] + s["exception"])


# ══════════════════════════════════════════════════════════════════════════
# 수집 — 종목 1개
# ══════════════════════════════════════════════════════════════════════════
def _mcap_shares(tk: str):
    """(역산 주식 수 일간 시계열, 결측률, 응답종류들). 종가는 분모로만 쓴다."""
    mc_raw, mk = P._get(f"historical-market-capitalization?symbol={tk}{WIN}")
    px_raw, pk = P._get(f"historical-price-eod/full?symbol={tk}{WIN}")
    mc = P._series(P._rows(mc_raw), "marketCap")
    px = P._series(P._rows(px_raw), "close")
    if mc.empty or px.empty:
        return pd.Series(dtype=float), 1.0, (mk, pk)
    lo, hi = max(mc.index[0], px.index[0]), min(mc.index[-1], px.index[-1])
    px_in = px[(px.index >= lo) & (px.index <= hi)]
    j = pd.concat([mc.rename("mc"), px.rename("px")], axis=1, join="inner")
    j = j[(j.index >= lo) & (j.index <= hi)]
    miss = 1.0 - (len(j) / len(px_in)) if len(px_in) else 1.0
    sh = (j["mc"] / j["px"]).dropna().sort_index()
    return sh, miss, (mk, pk)


def _stmt_frame(tk: str):
    """(DataFrame[period_end · filing · shares · shares_dil], 응답종류, 키 표본)."""
    raw, kind = P._get(f"income-statement?symbol={tk}&period=quarter&limit={STMT_LIMIT}")
    rows = P._rows(raw)
    recs, keys = [], sorted(rows[0].keys()) if rows else []
    for r in rows:
        pe = ec._d(r.get("date"))
        sh = ec._num(r.get("weightedAverageShsOut"))
        if pe is None or sh is None or sh <= 0:
            continue
        recs.append({"pe": pe,
                     "filing": ec._d(r.get("filingDate")) or ec._d(r.get("acceptedDate")),
                     "sh": float(sh),
                     "sh_dil": ec._num(r.get("weightedAverageShsOutDil"))})
    df = pd.DataFrame(recs)
    if not df.empty:
        df = df.sort_values("pe").reset_index(drop=True)
    return df, kind, keys


def _split_dates(tk: str) -> list:
    raw, _kind = P._get(f"splits?symbol={tk}")
    out = []
    for r in P._rows(raw):
        d = ec._d(r.get("date"))
        if d is not None:
            out.append(d)
    return sorted(out)


def _is_fund(tk: str):
    """(is_fund|None, kind). 조회가 비었을 때만 부른다 — PEAD 러너의 분류 규칙."""
    raw, kind = P._get(f"profile?symbol={tk}")
    rows = P._rows(raw)
    if not rows:
        return None, kind
    return (bool(rows[0].get("isEtf")) or bool(rows[0].get("isFund"))), kind


def collect(tk: str) -> dict:
    """종목 1개의 두 경로. 수익률 계산 없음."""
    sh, miss, mkinds = _mcap_shares(tk)
    df, skind, keys = _stmt_frame(tk)
    splits = _split_dates(tk)
    fund, fkind = (None, "")
    if sh.empty or df.empty:
        fund, fkind = _is_fund(tk)
    return {"tk": tk, "sh": sh, "miss": miss, "stmt": df, "splits": splits,
            "fund": fund, "kinds": mkinds + (skind, fkind), "keys": keys}


# ══════════════════════════════════════════════════════════════════════════
# 계단 검출 — I-LAG · I-NOISE 의 공통 재료
# ══════════════════════════════════════════════════════════════════════════
def _near_split(d: pd.Timestamp, splits: list) -> bool:
    return any(abs((d - s).days) <= SPLIT_GUARD_DAYS for s in splits)


def detect_steps(sh: pd.Series, splits: list) -> list:
    """[(날짜, 변화율)] — 일간 |Δ| ≥ STEP_MIN, 분할 근처 제외, 붙은 것은 병합."""
    if len(sh) < 2:
        return []
    chg = (sh / sh.shift(1) - 1.0).dropna()
    raw = [(d, float(v)) for d, v in chg.items()
           if abs(float(v)) >= STEP_MIN and not _near_split(d, splits)]
    out = []
    for d, v in raw:
        if out and (d - out[-1][0]).days <= STEP_CLUSTER_DAYS:
            if abs(v) > abs(out[-1][1]):
                out[-1] = (d, v)          # 같은 사건이 이틀에 걸친 경우 — 큰 쪽만
            continue
        out.append((d, v))
    return out


# ══════════════════════════════════════════════════════════════════════════
# STEP 1 — I-COVER · I-COST
# ══════════════════════════════════════════════════════════════════════════
def probe_cover(data: list) -> dict:
    print("=" * 76)
    print(f"I-COVER) 주식 수 이력이 {START_TARGET} 이전까지 닿는가 — 경로별")
    print("=" * 76)
    tgt = pd.Timestamp(START_TARGET)
    funds = [d["tk"] for d in data if d["fund"] is True]
    fails = [d["tk"] for d in data if d["fund"] is not True
             and (d["sh"].empty and d["stmt"].empty)]
    live = [d for d in data if d["fund"] is not True]
    m_ok = [d["tk"] for d in live if not d["sh"].empty and d["sh"].index[0] <= tgt]
    s_ok = [d["tk"] for d in live if not d["stmt"].empty and d["stmt"]["pe"].iloc[0] <= tgt]
    n = max(1, len(live))
    m_rate, s_rate = len(m_ok) / n, len(s_ok) / n

    have_f = sum(1 for d in live if not d["stmt"].empty
                 and d["stmt"]["filing"].notna().all())
    part_f = sum(int(d["stmt"]["filing"].notna().sum()) for d in live if not d["stmt"].empty)
    part_n = sum(int(len(d["stmt"])) for d in live if not d["stmt"].empty)
    f_rate = (part_f / part_n) if part_n else 0.0

    m_first = sorted(d["sh"].index[0] for d in live if not d["sh"].empty)
    s_first = sorted(d["stmt"]["pe"].iloc[0] for d in live if not d["stmt"].empty)
    print(f"  표본 {len(data)}종목 · 펀드 {len(funds)} 제외 · 양 경로 모두 빈 종목 {len(fails)}")
    if funds:
        print(f"    펀드: {', '.join(funds)}")
    if fails:
        print(f"    실패(펀드 아닌데 이력 없음): {', '.join(fails)}")
    print(f"  경로 M 시총÷종가  {len(m_ok)}/{n} = {m_rate:5.1%}  "
          f"최초일 중앙값 {m_first[len(m_first)//2].strftime('%Y-%m-%d') if m_first else '-'}"
          f"  {P._mark(m_rate >= COVER_MIN)}")
    print(f"  경로 S 분기재무제표 {len(s_ok)}/{n} = {s_rate:5.1%}  "
          f"최초일 중앙값 {s_first[len(s_first)//2].strftime('%Y-%m-%d') if s_first else '-'}"
          f"  {P._mark(s_rate >= COVER_MIN)}")
    print(f"  filingDate 존재율 {f_rate:5.1%} (전 분기 완비 {have_f}/{len(live)})  "
          f"{P._mark(f_rate >= STMT_FILING_MIN)}")
    miss = [d["miss"] for d in live if not d["sh"].empty]
    if miss:
        print(f"  경로 M 종가 대비 시총 결측: 중앙값 {_q(miss, 0.5):.1%} · "
              f"최악 {max(miss):.1%}")
    if data and data[0]["keys"]:
        print(f"  income-statement 키: {data[0]['keys']}")
    return {"m": m_rate >= COVER_MIN, "s": s_rate >= COVER_MIN,
            "m_rate": m_rate, "s_rate": s_rate, "f_rate": f_rate,
            "filing_ok": f_rate >= STMT_FILING_MIN, "live": len(live),
            "funds": len(funds), "fails": len(fails)}


# ══════════════════════════════════════════════════════════════════════════
# STEP 2 — I-LAG (경로 M 의 핵심 질문)
# ══════════════════════════════════════════════════════════════════════════
def probe_lag(data: list) -> dict:
    print()
    print("=" * 76)
    print("I-LAG) 주식 수 계단은 언제 생기나 — filingDate 대비")
    print("=" * 76)
    d_fil, d_pe, matched, total = [], [], 0, 0
    per_tk = []
    for d in data:
        sh, df = d["sh"], d["stmt"]
        if sh.empty or df.empty:
            continue
        steps = detect_steps(sh, d["splits"])
        if not steps:
            per_tk.append((d["tk"], 0, 0))
            continue
        # 계단을 '그 계단이 속한 분기'에 귀속시킨다. filing 에서 가장 가까운 계단을
        # 고르면 안 된다 — 계단이 분기마다 서게 되므로 ±90일 창은 **언제나** 뭔가를
        # 찾아내고, 측정된 지연이 약 91일 주기로 접힌다(셀프테스트 T3 가 잡았다:
        # 실제 200일 지연이 18일로 보였다).
        pes = [(row["pe"], row["filing"]) for _i, row in df.iterrows()]
        hit = 0
        for k, (pe, f) in enumerate(pes):
            nxt = pes[k + 1][0] if k + 1 < len(pes) else pe + timedelta(days=LAG_MATCH_WIN + 10)
            if f is None or pd.isna(f) or pe < sh.index[0] or pe > sh.index[-1]:
                continue
            total += 1
            in_q = [sd for sd, _v in steps if pe <= sd < nxt]
            if not in_q:
                continue
            matched += 1
            hit += 1
            sd = in_q[0]
            d_fil.append((sd - f).days)
            d_pe.append((sd - pe).days)
        per_tk.append((d["tk"], hit, len(steps)))

    rate = (matched / total) if total else 0.0
    med_f = _q(d_fil, 0.5)
    med_p = _q(d_pe, 0.5)
    # 필요 래그: 계단의 LAG_COVER_Q 가 '계단일 + 래그 ≥ filingDate' 를 만족하는 최소값.
    #   계단이 filing 보다 이르면 (sd - f) 가 음수 → 그 절대값만큼 밀어야 한다.
    need = _q([-x for x in d_fil], LAG_COVER_Q)
    need_lag = int(math.ceil(max(0.0, need))) if need is not None else None

    print(f"  대응: 분기 {total}개 중 그 분기 안에 계단이 선 것 {matched}개 = "
          f"{rate:5.1%}  {P._mark(rate >= LAG_MATCH_MIN)}")
    print(f"  계단일 − filingDate : 중앙값 {med_f if med_f is None else f'{med_f:+.0f}'}일 · "
          f"10% {_q(d_fil, 0.10)} · 90% {_q(d_fil, 0.90)}")
    print(f"  계단일 − 분기말     : 중앙값 {med_p if med_p is None else f'{med_p:+.0f}'}일")
    print(f"  → 필요 래그 {need_lag}일 (계단의 {LAG_COVER_Q:.0%} 가 알 수 있는 정보가 되는 최소값) "
          f"· 상한 {I_LAG_MAX_DAYS}일  "
          f"{P._mark(need_lag is not None and need_lag <= I_LAG_MAX_DAYS)}")
    dead = [tk for tk, _h, ns in per_tk if ns == 0]
    if dead:
        print(f"  계단이 하나도 없는 종목 {len(dead)}: {', '.join(dead[:8])}"
              f"{' …' if len(dead) > 8 else ''}")
    ok = (rate >= LAG_MATCH_MIN and need_lag is not None
          and need_lag <= I_LAG_MAX_DAYS)
    return {"ok": ok, "rate": rate, "need_lag": need_lag,
            "med_filing": med_f, "med_pe": med_p, "n": total}


# ══════════════════════════════════════════════════════════════════════════
# STEP 3 — I-NOISE (경로 M 이 계단 구조인가)
# ══════════════════════════════════════════════════════════════════════════
def probe_noise(data: list) -> dict:
    print()
    print("=" * 76)
    print("I-NOISE) 코퍼레이트 액션 없는 날의 주식 수 흔들림")
    print("=" * 76)
    rows = []
    for d in data:
        sh = d["sh"]
        if len(sh) < 30:
            continue
        steps = {sd for sd, _ in detect_steps(sh, d["splits"])}
        chg = (sh / sh.shift(1) - 1.0).dropna()
        vals = [abs(float(v)) for dt, v in chg.items()
                if dt not in steps and not _near_split(dt, d["splits"])]
        if not vals:
            continue
        nz = [v for v in vals if v >= NOISE_FLAT_EPS]
        med = _q(nz, 0.5) if nz else 0.0
        flat = sum(1 for v in vals if v < NOISE_FLAT_EPS) / len(vals)
        # 규모 3분위용 대표 시총 대신 최근 주식 수 — 시총은 종가에 의존해 흔들린다
        rows.append({"tk": d["tk"], "med": med, "flat": flat, "n": len(vals),
                     "sh": float(sh.iloc[-1])})
    if not rows:
        print("  표본 없음")
        return {"ok": False, "med": None, "flat": None}
    med = _q([r["med"] for r in rows], 0.5)
    flat = _q([r["flat"] for r in rows], 0.5)
    ok = (med is not None and med <= NOISE_WIGGLE_MAX
          and flat is not None and flat >= NOISE_FLAT_MIN)
    print(f"  무변화 아닌 날의 중앙 |Δ| {med * 100:.4f}% (상한 {NOISE_WIGGLE_MAX * 100:.2f}%)  "
          f"{P._mark(med <= NOISE_WIGGLE_MAX)}")
    print(f"  무변화일 비율의 중앙값   {flat:5.1%} (하한 {NOISE_FLAT_MIN:.0%})  "
          f"{P._mark(flat >= NOISE_FLAT_MIN)}")
    rows.sort(key=lambda r: r["sh"])
    k = max(1, len(rows) // 3)
    for lab, part in (("소", rows[:k]), ("중", rows[k:2 * k]), ("대", rows[2 * k:])):
        if not part:
            continue
        print(f"    주식수 {lab} {len(part):3}종목: 흔들림 "
              f"{_q([r['med'] for r in part], 0.5) * 100:.4f}% · 무변화일 "
              f"{_q([r['flat'] for r in part], 0.5):.1%}")
    worst = sorted(rows, key=lambda r: -r["med"])[:5]
    print("  가장 시끄러운 5종목: " + ", ".join(f"{r['tk']}({r['med']*100:.3f}%)"
                                         for r in worst))
    return {"ok": ok, "med": med, "flat": flat}


# ══════════════════════════════════════════════════════════════════════════
# STEP 4 — I-ARTIF (분할로 설명 안 되는 점프)
# ══════════════════════════════════════════════════════════════════════════
def _quarterly(sh: pd.Series) -> pd.Series:
    """분기별 마지막 관측. 인덱스는 **실제 관측일**이다.

    합성 월초 날짜(예: 6-01)를 인덱스로 쓰면 분할 가드 창이 통째로 어긋난다 —
    6월 중순 분할이 창 밖으로 밀려 '설명되지 않는 점프'로 오분류됐다(셀프테스트 T6).
    """
    if sh.empty:
        return sh
    out = {}
    for dt, v in sh.items():
        out[(dt.year, (dt.month - 1) // 3 + 1)] = (dt, float(v))
    pairs = sorted(out.values(), key=lambda x: x[0])
    return pd.Series([v for _d, v in pairs],
                     index=pd.DatetimeIndex([d for d, _v in pairs]), dtype=float)


def _jump_count(s: pd.Series, splits: list) -> tuple:
    if len(s) < 2:
        return 0, 0, []
    bad, hits = 0, []
    r = (s / s.shift(1)).dropna()
    for dt, v in r.items():
        if v <= 0:
            continue
        if abs(float(v) - 1.0) < ART_JUMP:
            continue
        lo = dt - timedelta(days=100)
        if any(lo <= sp <= dt + timedelta(days=SPLIT_GUARD_DAYS) for sp in splits):
            continue                      # 분할로 설명된다
        bad += 1
        hits.append(f"{dt.strftime('%Y-%m')}×{float(v):.2f}")
    return bad, len(r), hits


def probe_artifact(data: list) -> dict:
    print()
    print("=" * 76)
    print(f"I-ARTIF) 분할로 설명되지 않는 분기 점프 (>{ART_JUMP:.0%})")
    print("=" * 76)
    out = {}
    for path, getter in (("M", lambda d: _quarterly(d["sh"])),
                         ("S", lambda d: pd.Series(
                             list(d["stmt"]["sh"]) if not d["stmt"].empty else [],
                             index=pd.DatetimeIndex(
                                 list(d["stmt"]["pe"]) if not d["stmt"].empty else []),
                             dtype=float))):
        bad = tot = 0
        shown = []
        for d in data:
            if d["fund"] is True:
                continue
            b, t, hits = _jump_count(getter(d), d["splits"])
            bad += b
            tot += t
            if hits:
                shown.append(f"{d['tk']}:{','.join(hits[:2])}")
        rate = (bad / tot) if tot else 0.0
        ok = rate <= ART_MAX
        out[path] = {"ok": ok, "rate": rate, "bad": bad, "tot": tot}
        print(f"  경로 {path}: {bad}/{tot} 종목-분기 = {rate:5.2%} (상한 {ART_MAX:.0%})  "
              f"{P._mark(ok)}")
        if shown:
            print(f"    예: {' · '.join(shown[:6])}{' …' if len(shown) > 6 else ''}")
    return out


# ══════════════════════════════════════════════════════════════════════════
# STEP 5 — I-DISP (독립변수 분포만 · 수익률 아님)
# ══════════════════════════════════════════════════════════════════════════
def _chg_mcap(d: dict, asof: pd.Timestamp):
    sh = d["sh"]
    if sh.empty:
        return None
    now = _at(sh, asof)
    prev = _at(sh, asof - timedelta(days=365))
    if not now or not prev:
        return None
    return now / prev - 1.0


def _chg_stmt(d: dict, asof: pd.Timestamp):
    """filing 기준 — 그 시점에 실제로 공시됐던 두 값만 쓴다."""
    df = d["stmt"]
    if df.empty:
        return None
    known = df[df["filing"].notna() & (df["filing"] <= asof)]
    prev_known = df[df["filing"].notna() & (df["filing"] <= asof - timedelta(days=365))]
    if known.empty or prev_known.empty:
        return None
    a = float(known["sh"].iloc[-1])
    b = float(prev_known["sh"].iloc[-1])
    if a <= 0 or b <= 0:
        return None
    return a / b - 1.0


def probe_dispersion(data: list) -> dict:
    print()
    print("=" * 76)
    print("I-DISP) 12개월 주식 수 변화율의 횡단면 분포 — 독립변수만")
    print("=" * 76)
    out = {}
    for path, fn in (("M", _chg_mcap), ("S", _chg_stmt)):
        passed = 0
        for a in DISP_ASOF:
            asof = pd.Timestamp(a)
            vals = [v for v in (fn(d, asof) for d in data if d["fund"] is not True)
                    if v is not None]
            if len(vals) < 10:
                print(f"  경로 {path} {a}: 표본 {len(vals)} — 부족")
                continue
            q1, q3 = _q(vals, 0.25), _q(vals, 0.75)
            iqr = q3 - q1
            tail = sum(1 for v in vals if abs(v) > DISP_TAIL_BAND) / len(vals)
            ok = iqr >= DISP_IQR_MIN and tail >= DISP_TAIL_MIN
            passed += int(ok)
            print(f"  경로 {path} {a}: n={len(vals):3} · 중앙 {_pct(_q(vals, 0.5))} · "
                  f"IQR {iqr * 100:5.2f}%p · ±{DISP_TAIL_BAND:.0%} 밖 {tail:5.1%} · "
                  f"최저 {_pct(min(vals))} / 최고 {_pct(max(vals))}  {P._mark(ok)}")
        ok = passed >= DISP_MIN_ASOF
        out[path] = {"ok": ok, "passed": passed}
        print(f"  → 경로 {path}: 기준일 {passed}/{len(DISP_ASOF)} 통과 "
              f"(필요 {DISP_MIN_ASOF})  {P._mark(ok)}")
    return out


# ══════════════════════════════════════════════════════════════════════════
# STEP 6 — I-PATH (실행 전 고정된 결정 규칙)
# ══════════════════════════════════════════════════════════════════════════
def decide_path(cover: dict, lag: dict, noise: dict, artif: dict, disp: dict) -> tuple:
    """(경로, 사유). docstring [I-PATH] 와 같은 규칙이어야 한다."""
    m_ok = cover["m"] and lag["ok"] and noise["ok"] and artif["M"]["ok"]
    s_ok = cover["s"] and cover["filing_ok"] and artif["S"]["ok"]
    if m_ok and s_ok:
        chosen = "M"
        why = "둘 다 자격 → M 우선(실제 발행주식수 · 래그로 지연을 닫는다)"
    elif m_ok:
        chosen, why = "M", "M 만 자격"
    elif s_ok:
        chosen, why = "S", "S 만 자격(래그는 filingDate)"
    else:
        return "없음", "두 경로 모두 실격 → 순주식발행 종료 후보"
    if not disp[chosen]["ok"]:
        return "없음", f"{chosen} 자격이나 I-DISP 미달 → 정렬할 분산이 없다 · 종료 후보"
    return chosen, why


# ══════════════════════════════════════════════════════════════════════════
# 셀프테스트 — 합성 세계 · FMP 콜 0 · 게이트가 실제로 뒤집히는지만 본다
# ══════════════════════════════════════════════════════════════════════════
def _fake(tk: str, *, step_days: list, step_pct: float = 0.01,
          filing_lag: int = 40, splits: list = (), start="2013-01-01",
          end="2022-06-30", drift: float = 0.0, wiggle: float = 0.0,
          wiggle_period: int = 5, stmt_days: list = None) -> dict:
    """합성 종목 1개. step_days 에서 주식 수가 계단으로 뛴다."""
    idx = pd.date_range(start, end, freq="B")

    def _snap(x):
        # 분기말은 주말에 자주 걸린다. 영업일로 당기지 않으면 계단이 그냥 사라지고,
        # 합성 표본의 연간 변화율이 의도보다 작아진다(뮤테이션 M9 생존의 원인).
        later = idx[idx >= pd.Timestamp(x)]
        return later[0] if len(later) else None

    vals, cur = [], 1.0e9
    steps = {d for d in (_snap(x) for x in step_days) if d is not None}
    for i, d in enumerate(idx):
        if d in steps:
            cur *= (1.0 + step_pct)
        cur *= (1.0 + drift)
        # wiggle: 주기마다 하루만 튄다 → 무변화일 비율은 높은데 흔들릴 땐 크다
        vals.append(cur * (1.0 + wiggle) if (wiggle and i % wiggle_period == 4) else cur)
    sh = pd.Series(vals, index=idx, dtype=float)
    recs = []
    for d in sorted({pd.Timestamp(x) for x in (stmt_days if stmt_days is not None
                                               else step_days)}):
        recs.append({"pe": d, "filing": d + timedelta(days=filing_lag),
                     "sh": float(sh.get(d, 1.0e9)), "sh_dil": None})
    df = pd.DataFrame(recs)
    if not df.empty:
        df = df.sort_values("pe").reset_index(drop=True)
    return {"tk": tk, "sh": sh, "miss": 0.0, "stmt": df,
            "splits": [pd.Timestamp(s) for s in splits], "fund": None,
            "kinds": ("ok",), "keys": []}


def _quiet(fn, *a, **k):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn(*a, **k)


def selftest() -> int:
    fails = []

    def chk(name, cond):
        print(f"  {P._mark(bool(cond))} {name}")
        if not cond:
            fails.append(name)

    print("=" * 76)
    print("셀프테스트 — 합성 세계 · FMP 콜 0")
    print("=" * 76)

    qe = [f"{y}-{m}" for y in range(2014, 2021) for m in ("03-31", "06-30", "09-30", "12-31")]
    base = [_fake(f"T{i}", step_days=qe) for i in range(12)]

    # T1 계단 검출: 계단이 있으면 잡고, 분할 근처면 버린다
    s1 = detect_steps(base[0]["sh"], [])
    s2 = detect_steps(base[0]["sh"], [pd.Timestamp(d) for d in qe])
    chk("T1 계단 검출 — 무분할 %d건 · 분할 근처 제외 %d건" % (len(s1), len(s2)),
        len(s1) >= 20 and len(s2) == 0)

    # T2 I-LAG: 계단이 filing 보다 40일 이르면 필요 래그 ≈ 40일 → 통과
    r = _quiet(probe_lag, base)
    chk("T2 I-LAG 정상(래그 40일) → ok · need=%s" % r["need_lag"],
        r["ok"] and r["need_lag"] is not None and 35 <= r["need_lag"] <= 45)

    # T3 I-LAG: 공시가 200일 늦으면 필요 래그가 상한을 넘어 ❌ (게이트가 살아 있다)
    late = [_fake(f"L{i}", step_days=qe, filing_lag=200) for i in range(12)]
    r3 = _quiet(probe_lag, late)
    chk("T3 I-LAG 지연 200일 → ❌ · need=%s" % r3["need_lag"],
        (not r3["ok"]) and r3["need_lag"] is not None and r3["need_lag"] > I_LAG_MAX_DAYS)

    # T4 I-LAG: 계단이 없으면 대응률 0 → ❌
    flat = [_fake(f"F{i}", step_days=[]) for i in range(12)]
    r4 = _quiet(probe_lag, flat)
    chk("T4 I-LAG 계단 없음 → ❌", not r4["ok"])

    # T4b I-LAG: 계단이 분기의 절반에만 서면 대응률 50% → ❌ (대응률 게이트 단독)
    half = [_fake(f"H{i}", step_days=qe[::2], stmt_days=qe) for i in range(12)]
    r4b = _quiet(probe_lag, half)
    chk("T4b I-LAG 대응률 %.0f%% → ❌" % (r4b["rate"] * 100),
        (not r4b["ok"]) and r4b["rate"] < LAG_MATCH_MIN
        and r4b["need_lag"] is not None and r4b["need_lag"] <= I_LAG_MAX_DAYS)

    # T5 I-NOISE: 계단형은 통과, 매끄러운(보간) 이력은 ❌
    n1 = _quiet(probe_noise, base)
    smooth = [_fake(f"S{i}", step_days=[], drift=0.0004) for i in range(12)]
    n2 = _quiet(probe_noise, smooth)
    chk("T5 I-NOISE 계단형 ✅ / 매끄러움 ❌", n1["ok"] and not n2["ok"])

    # T5b I-NOISE: 무변화일은 많은데(60%) 흔들릴 때 크면 ❌ — 흔들림 게이트 단독 검증
    wig = [_fake(f"W{i}", step_days=[], wiggle=0.002) for i in range(12)]
    n3 = _quiet(probe_noise, wig)
    chk("T5b I-NOISE 무변화 %s / 흔들림 %s → ❌"
        % (f"{n3['flat']:.0%}", f"{n3['med']*100:.2f}%"),
        (not n3["ok"]) and n3["flat"] >= NOISE_FLAT_MIN and n3["med"] > NOISE_WIGGLE_MAX)

    # T6 I-ARTIF: 분할로 설명되는 점프는 세지 않고, 설명 안 되는 점프는 센다
    jump = _fake("J", step_days=["2016-06-30"], step_pct=3.0)
    b1, t1_, _ = _jump_count(_quarterly(jump["sh"]), [])
    b2, _t, _ = _jump_count(_quarterly(jump["sh"]), [pd.Timestamp("2016-06-15")])
    chk("T6 I-ARTIF 미설명 %d건 / 분할설명 %d건" % (b1, b2), b1 >= 1 and b2 == 0 and t1_ > 0)

    # T7 I-DISP: 분산이 없으면 ❌, 종목마다 다르면 ✅
    same = [_fake(f"Z{i}", step_days=qe, step_pct=0.0005) for i in range(12)]
    d1 = _quiet(probe_dispersion, same)
    vary = [_fake(f"V{i}", step_days=qe, step_pct=(i - 6) * 0.01) for i in range(14)]
    d2 = _quiet(probe_dispersion, vary)
    chk("T7 I-DISP 무분산 ❌ / 유분산 ✅", (not d1["M"]["ok"]) and d2["M"]["ok"])

    # T7b I-DISP: 꼬리는 충분한데 IQR 이 없으면 ❌ — IQR 게이트 단독 검증
    thin = ([_fake(f"N{i}", step_days=[]) for i in range(12)]
            + [_fake(f"B{i}", step_days=qe, step_pct=0.05) for i in range(3)])
    d3 = _quiet(probe_dispersion, thin)
    chk("T7b I-DISP 꼬리만 있고 IQR 없음 → ❌", not d3["M"]["ok"])

    # T7c I-DISP: IQR 은 충분한데 꼬리가 없으면 ❌ — 꼬리 게이트 단독 검증
    qe_all = [f"{y}-{m}" for y in range(2013, 2023)
              for m in ("03-31", "06-30", "09-30", "12-31")]
    spread = [_fake(f"P{i}", step_days=qe_all, step_pct=(i - 7.5) * 0.00083)
              for i in range(16)]
    d4 = _quiet(probe_dispersion, spread)
    chk("T7c I-DISP IQR 만 있고 꼬리 없음 → ❌", not d4["M"]["ok"])

    # T8 I-PATH 진리표 — 8칸이 모두 규칙대로 갈리는지
    def _cv(m, s, f=True):
        return {"m": m, "s": s, "filing_ok": f, "m_rate": 0.9, "s_rate": 0.9}
    okg = {"ok": True}
    nog = {"ok": False}
    art = {"M": {"ok": True}, "S": {"ok": True}}
    dok = {"M": {"ok": True}, "S": {"ok": True}}
    cases = [
        (_cv(True, True), okg, okg, art, dok, "M"),      # 둘 다 자격 → M
        (_cv(False, True), okg, okg, art, dok, "S"),     # M 커버 미달 → S
        (_cv(True, True), nog, okg, art, dok, "S"),      # 래그 미달 → S
        (_cv(True, True), okg, nog, art, dok, "S"),      # 잡음 미달 → S
        (_cv(True, False), okg, okg, art, dok, "M"),     # S 커버 미달 → M
        (_cv(True, True, False), okg, okg, art, dok, "M"),   # filingDate 미달 → M
        (_cv(False, False), okg, okg, art, dok, "없음"),  # 둘 다 실격
        (_cv(True, True), okg, okg, art,
         {"M": {"ok": False}, "S": {"ok": True}}, "없음"),   # 채택 경로 분산 없음
    ]
    got = [decide_path(c, l, n, a, d)[0] for c, l, n, a, d, _ in cases]
    want = [w for *_x, w in cases]
    chk("T8 I-PATH 진리표 8칸 %s" % ("일치" if got == want else f"{got} ≠ {want}"),
        got == want)

    print()
    if fails:
        print(f"❌ 셀프테스트 실패 {len(fails)}건: {fails}")
        return 1
    print("✅ 셀프테스트 12/12 통과 — FMP 콜 0")
    return 0


# ══════════════════════════════════════════════════════════════════════════
def build_sample() -> list:
    t1, t2 = P._load_universe()
    rnd = random.Random(SEED)
    s1 = sorted(rnd.sample(t1, min(S1_SAMPLE, len(t1)))) if (t1 and S1_SAMPLE) else []
    s2 = sorted(rnd.sample(t2, min(S2_SAMPLE, len(t2)))) if (t2 and S2_SAMPLE) else []
    print(f"  Tier 1 {len(t1)}종목 → 표본 {len(s1)} · "
          f"Tier 2 {len(t2)}종목 → 표본 {len(s2)} (seed {SEED})")
    print(f"  [I-BREADTH] Earnings_Universe 비Tier1 종목 수 = {len(t2)}")
    if s1:
        print(f"    S1: {', '.join(s1)}")
    if s2:
        print(f"    S2: {', '.join(s2)}")
    return sorted(set(s1) | set(s2)), len(t2)


def main() -> int:
    if str(os.environ.get("SELFTEST", "") or "").strip() not in ("", "0", "false"):
        return selftest()
    if not fh.fmp_key():
        print("[ABORT] FMP_API_KEY 없음")
        return 2
    print(f"diag_issuance_probe — {TODAY} · 읽기 전용 · 수익률 계산 없음 · 시트 쓰기 0")
    print(f"창 {WIN} (fx.HIST_MAX_DAYS={fx.HIST_MAX_DAYS}) · 목표 시작 {START_TARGET}\n")

    print("=" * 76)
    print("STEP 0) 표본")
    print("=" * 76)
    tickers, t2_total = build_sample()
    if not tickers:
        print("[ABORT] 표본 0종목 — TICKERS 또는 GSPREAD_KEY 를 주십시오")
        return 2

    c0 = _calls()
    data = []
    for i, tk in enumerate(tickers, 1):
        data.append(collect(tk))
        if i % 10 == 0:
            print(f"    … {i}/{len(tickers)}")
    calls = _calls() - c0
    print(f"  [I-COST] 표본 {len(tickers)}종목 · {calls}콜 = "
          f"종목당 {calls / max(1, len(tickers)):.1f}콜\n")

    cover = probe_cover(data)
    lag = probe_lag(data)
    noise = probe_noise(data)
    artif = probe_artifact(data)
    disp = probe_dispersion(data)
    path, why = decide_path(cover, lag, noise, artif, disp)

    usable = int(round(t2_total * cover["m_rate" if path == "M" else "s_rate"])) \
        if path in ("M", "S") else 0

    print()
    print("=" * 76)
    print("요약 — 판독 기준은 docstring 에 실행 전 기록")
    print("=" * 76)
    print(f"[I-COVER]  M {P._mark(cover['m'])} {cover['m_rate']:.1%} · "
          f"S {P._mark(cover['s'])} {cover['s_rate']:.1%} · "
          f"filingDate {P._mark(cover['filing_ok'])} {cover['f_rate']:.1%} "
          f"(하한 {COVER_MIN:.0%} / {STMT_FILING_MIN:.0%})")
    print(f"[I-LAG]    {P._mark(lag['ok'])} 대응 {lag['rate']:.1%} · "
          f"필요 래그 {lag['need_lag']}일 (상한 {I_LAG_MAX_DAYS})")
    n_med = "-" if noise["med"] is None else f"{noise['med'] * 100:.4f}%"
    n_flat = "-" if noise["flat"] is None else f"{noise['flat']:.1%}"
    print(f"[I-NOISE]  {P._mark(noise['ok'])} 중앙 |Δ| {n_med} · 무변화일 {n_flat}")
    print(f"[I-ARTIF]  M {P._mark(artif['M']['ok'])} {artif['M']['rate']:.2%} · "
          f"S {P._mark(artif['S']['ok'])} {artif['S']['rate']:.2%} (상한 {ART_MAX:.0%})")
    print(f"[I-DISP]   M {P._mark(disp['M']['ok'])} {disp['M']['passed']}/{len(DISP_ASOF)} · "
          f"S {P._mark(disp['S']['ok'])} {disp['S']['passed']}/{len(DISP_ASOF)}")
    print(f"[I-BREADTH] Earnings_Universe 비Tier1 {t2_total}종목 → 채택 경로 커버율 환산 "
          f"약 {usable}종목")
    print(f"[I-PATH]   **{path}** — {why}")
    if path != "없음":
        print(f"           → 약정에 박을 래그 상수 후보: "
              f"{lag['need_lag'] if path == 'M' else 0}일"
              f"{' (S 는 filingDate 로 구조적 해결)' if path == 'S' else ''}")
    print(fh.fmp_stats_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
