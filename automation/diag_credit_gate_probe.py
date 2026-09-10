#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_credit_gate_probe.py — 크레딧 스프레드 게이트 Phase 0.5 프로브 (읽기 전용)

v1 → v2 (2026-09-10)
────────────────────
v1 은 P1~P4 를 전부 통과시켰지만 **통과가 정보를 주지 않았다.** 세 가지가 겹쳤다.

  A) **소스 선택 버그(내 잘못).** Q1 이 "평가 구간 2007~ 확정"이라 찍어놓고
     main() 은 `deep_ok` 를 안 보고 **행 수가 가장 많은 응답**을 골랐다
     (1826일 창 = 1254봉, 2021-09~). 실측이 2007년이 아니라 2021년부터 돌았다.
     → v2: deep_ok 면 2007-01-01 단일 창을 받아 그것만 쓴다.

  B) **병목은 FMP 가 아니라 FRED 였다.** `BAMLH0A0HYM2` 가 786점 /
     2023-09-11~ 만 왔다(이 시리즈는 1996년부터 있다). SPY 를 20년 받아도
     교집합은 3년이다. 200일선 워밍업까지 빼면 551봉 · SPY200 off 3에피소드 —
     "한두 사건에서 나온 숫자"라는 v1 자체 경고가 그대로 발동한 상태였다.
     → v2: 명시 observation_start 로 늘어나는지 재고, BAA10Y 대체안을 같이 잰다.

  C) **R1 이 애초에 게이트가 아니었다(설계 결함).** off 199일 = **36.1%** ·
     16에피소드. SPY200 은 10.0% · 3에피소드다. "자기 20일선 위"는 평균회귀
     오실레이터라 어떤 시계열이든 절반쯤 참이고, 2일 확인이 36%로 깎았을 뿐이다.
     Q4 의 "안정성 통과"가 이를 확증한다 — 10/20/30/50일이 전부 34.8~40.8%였다.
     **창을 바꿔도 비율이 안 변하는 건 튼튼하다는 뜻이 아니라 신호가 창과
     무관한 잡음이라는 뜻이다.**
     → v2: off 비율을 **사전 조건**으로 승격하고 룰 후보를 넷으로 늘린다.

⚠️ 이것은 재협상이 아니다 — 선을 여기 긋는다.
   "P1 이 0.85 나왔으니 기준선을 0.90 으로 옮기자" 는 재협상이다.
   C 는 **수익률이 아니라 구조**다: off 36% 는 성과를 보기 전에, 종이 위에서
   알 수 있었다. 룰이 게이트의 정의를 만족하지 않는다는 것이지 성과가 나쁘다는
   것이 아니다. 대신 조건 셋을 건다 —
     ① v1 결과는 지우지 않는다(아래 "v1 실측 기록"에 박제).
     ② 새 룰은 새 사전 약정을 받는다(아래 P5·P6).
     ③ off 비율 조건을 **결과를 보기 전에** 숫자로 고정한다.

  실행: python automation/diag_credit_gate_probe.py
        python automation/diag_credit_gate_probe.py --selftest   # 네트워크 불필요

아무것도 수정하지 않는다. 시트를 열지 않고 파일을 쓰지 않는다.
FMP 콜 4 · FRED 콜 5.


═══════════════════════════════════════════════════════════════════════════
v1 실측 기록 — 2026-09-10 06:02 UTC. 지우지 않는다.
═══════════════════════════════════════════════════════════════════════════
  Q1  창 없음 1253봉(2021-09-13~) · 1826일 1254봉 · 2019~2020 505봉(2019-01-02~)
      · 2007~2009 756봉(2007-01-03~)   → 창은 존중된다. HIST_MAX_DAYS 는 정책이다.
  Q2  BAMLH0A0HYM2 786점 2023-09-11~2026-09-08(지연 2일) · NFCI 2904점(지연 13일)
  Q3  평가 2024-06-27~2026-09-08(551봉) · SPY200 off 55일/10.0%/3회
      · R1 off 199일/36.1%/16회 · 동시 40일 · **Jaccard 0.187** · P(c|s) 0.727
      · 리드/래그 +22거래일(짝 3)
  Q4  MA10 40.8%/J0.107 · MA20 36.1%/J0.187 · MA30 36.1%/J0.210 · MA50 34.8%/J0.280
  판정 P1 통과 · P3 통과 — **그러나 위 C 때문에 통과를 채택 근거로 쓰지 않는다.**


═══════════════════════════════════════════════════════════════════════════
사전 커밋 판정 기준
  P1~P4: 2026-09-10 확정(v1). P5~P6: 2026-09-10 확정(v2, 결과 보기 전).
  결과를 본 뒤 재협상하지 않는다.
═══════════════════════════════════════════════════════════════════════════

  P1  **중단 기준.** 후보의 off-day 집합과 SPY200 off-day 집합의 Jaccard 겹침이
      **0.80 이상**이면 그 후보는 탈락. 전 후보가 탈락하면 프로젝트를 종료한다
      — 200일선의 다른 이름에 모듈을 만들지 않는다.
      ⚠️ 리드/래그가 좋아 보인다는 이유로 무르지 않는다(출력 전용 열이다).

  P2  **구간 확정.** Q1 이 정한다. deep_ok 면 2007~, 아니면 응답 최소일~.
      구간을 사후에 넓히지 않는다.

  P4  Q2 가 실패하면(쓸 만한 스프레드 시리즈가 하나도 없으면) 나머지 판정은
      **무효**다. 부분 데이터로 룰 심사를 읽지 않는다.

  P5  **[v2 신규] off 비율 관문.** 후보의 off 비율이 **5.0%~15.0%** 밖이면
      **자동 탈락.** 근거는 성과가 아니라 정의다 — SPY200 이 10.0%이고,
      게이트라 부르려면 같은 자릿수여야 한다. 36% 를 막는 것은 게이트가
      아니라 동전 던지기다.
      ⚠️ 구간을 넓혀 후보를 살리지 않는다. 넓히는 순간 이 관문은 없는 것이다.

  P6  **[v2 신규] 에피소드 하한.** 후보의 off 에피소드가 **3회 미만**이면
      판정 불가로 처리한다(탈락이 아니라 **읽을 수 없음**). 1~2회짜리 통계는
      룰의 실력이 아니라 그 한 사건이다.

  ⚠️ **생존자 중에서 고르지 않는다.** P5·P6·P1 을 통과한 후보는 **전부**
     Phase 1 백테스트로 넘긴다. 여기서 "제일 좋아 보이는 것"을 고르면 심사에
     쓴 그 데이터로 선택까지 하는 것이고, 그건 T6 과 같은 함정이다.
     선택은 Phase 1 에서 별도 사전 약정으로 한다.

출력 전용 (어떤 판정에도 쓰지 않는다)
  · 리드/래그 중앙값 · 조건부 확률 P(off|spy_off) · 에피소드 길이 중앙값
  · NFCI 계열 전부
  · v1 의 P3(파라미터 민감도)는 v2 에서 **판정 지위를 잃었다.** 잡음 신호에서도
    통과했으므로 정보가 없다는 것이 v1 의 교훈이다.


이 프로브가 답할 수 없는 것 (구조적 한계 — 실행 전에 읽을 것)
─────────────────────────────────────────────────────────────
 1) **수익률을 재지 않는다.** off 비율이 맞다고 게이트가 값을 한다는 뜻이 아니다.
    P5 는 "게이트의 모양인가"만 묻는다. "값을 하는가"는 Phase 1 이다.
 2) **as-of 지연을 재현하지 않는다.** 완성된 시계열을 뒤에서 본다. OAS 는
    개정되지 않지만 발표가 1~2일 밀린다(v1 실측 2일). Phase 1 백테스트는
    반드시 지연을 넣어야 한다.
 3) **구간이 짧으면 전부 한 사건이다.** OAS 이력이 3년에 머물면 SPY200 하락
    국면이 2~3회뿐이라 P1 의 Jaccard 는 사실상 그 사건들에서 나온다.
    P6 이 잡는 것은 후보 쪽 에피소드일 뿐, SPY 쪽 빈약함은 못 잡는다 —
    출력의 'SPY200 에피소드' 수를 직접 볼 것.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

# ── 리포 루트 + 자기 폴더를 sys.path 에 (실행 위치 무관) ─────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_extras as fx      # noqa: E402  — 창/봉 정책 SSOT (읽기만 한다)
import fmp_http as fh        # noqa: E402  — FMP 호출 SSOT (A1: 생 requests 금지)

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()

# ── 시리즈 ───────────────────────────────────────────────────────────────────
HY_OAS = "BAMLH0A0HYM2"     # ICE BofA US High Yield OAS, 일별, 1996~ (문서상)
BAA10Y = "BAA10Y"           # 무디스 Baa − 10Y 국채, 일별, 1986~ — 전장 대체 후보
NFCI = "NFCI"               # 시카고연준 금융환경지수 — 출력 전용
DEEP_START = "1996-01-01"   # 명시 observation_start 프로브용

SPY_MA_BARS = 200           # §3 기존 시장 필터
CONFIRM_DAYS = 2            # 2일 연속 확인 + 재무장 (regime_core 와 같은 형태)

# 사전 약정 상수
P1_JACCARD_ABORT = 0.80
P5_OFF_MIN, P5_OFF_MAX = 0.05, 0.15
P6_MIN_EPISODES = 3

_SEP = "=" * 76


# ══════════════════════════════════════════════════════════════════════════
# 순수 로직 — 네트워크 없이 검증 가능한 부분은 전부 여기 (selftest 대상)
# ══════════════════════════════════════════════════════════════════════════
def confirmed_state(raw_on, confirm_days: int = CONFIRM_DAYS) -> pd.Series:
    """원시 조건 → **N일 연속 확인 + 재무장** 상태 시계열.

    켜질 때도 꺼질 때도 같은 N일을 요구한다. 한쪽만 확인하면 게이트가
    비대칭이 되고, 그 비대칭은 백테스트에서 '방향성 있는 실력'처럼 보인다.

    NA(판정 불가)는 **직전 런의 값을 이어받는다** — 채권 휴장·발표 지연으로
    난 구멍이 "조건 해제"로 읽히면 2일짜리 구멍 하나에 게이트가 조용히 꺼진다.
    첫 confirm_days-1 일은 판정 불가라 False 로 시작한다(NaN 으로 두면 하류
    집합 연산에서 조용히 빠져 겹침 분모가 달라진다).
    """
    v = pd.Series(raw_on, dtype=object).astype("boolean")
    out = np.zeros(len(v), dtype=bool)
    state = False
    run_val, run_len = None, 0
    for i, x in enumerate(v):
        cur = bool(x) if x is not pd.NA else run_val
        if cur is None:
            out[i] = state
            continue
        if cur == run_val:
            run_len += 1
        else:
            run_val, run_len = cur, 1
        if run_len >= confirm_days and cur != state:
            state = cur
        out[i] = state
    return pd.Series(out, index=v.index, name="on")


def _gate(raw: pd.Series, invalid: pd.Series, confirm_days: int = CONFIRM_DAYS) -> pd.Series:
    """원시 불리언 + 무효 마스크 → 확인된 게이트 상태.

    ⚠️ nullable "boolean" 으로 캐스팅한 뒤에 NA 를 넣는다. 넘파이 bool
       시리즈에 pd.NA 를 대입하면 TypeError 다(pandas 2.x 부터).
    """
    r = raw.astype("boolean")
    r[invalid] = pd.NA
    return confirmed_state(r, confirm_days=confirm_days)


def spy_gate_off(close: pd.Series, ma_bars: int = SPY_MA_BARS) -> pd.Series:
    """§3 기존 필터: 종가 < N일선 → off. 확인일 없음(기존 동작 그대로)."""
    ma = close.rolling(ma_bars).mean()
    return _gate(close < ma, ma.isna(), confirm_days=1)


# ── 룰 후보 ──────────────────────────────────────────────────────────────────
# 스프레드는 **높을수록 나쁘다** — 부등호가 SPY 와 반대다. 한 글자가 뒤집히면
# 게이트가 정확히 거꾸로 돌고 백테스트는 에러 없이 '역방향으로 잘 맞는 신호'를
# 보고한다. selftest 가 네 후보 전부를 양방향으로 지킨다.
def rule_ma20(s: pd.Series) -> pd.Series:
    """C1 — v1 의 R1. **대조군으로만 남긴다**(off 36% 로 P5 탈락 예상).

    지우지 않는 이유: 새 후보가 C1 보다 나은지가 아니라 **C1 이 왜 게이트가
    아닌지**가 이 표의 요점이다. 빼면 다음 사람이 같은 룰을 다시 제안한다.
    """
    ma = s.rolling(20).mean()
    return _gate(s > ma, ma.isna())


def rule_pct80(s: pd.Series) -> pd.Series:
    """C2 — 2년(504봉) 롤링 80분위 초과. '지금이 최근 2년 중 상위 20%인가'."""
    q = s.rolling(504).quantile(0.80)
    return _gate(s > q, q.isna())


def rule_chg60(s: pd.Series) -> pd.Series:
    """C3 — 60봉 전 대비 **+100bp 이상 확대**. 수준이 아니라 속도를 본다."""
    chg = s - s.shift(60)
    return _gate(chg >= 1.00, chg.isna())      # 단위: %포인트 (100bp = 1.00)


def rule_z10(s: pd.Series) -> pd.Series:
    """C4 — 1년(252봉) 평균 대비 z ≥ 1.0."""
    m = s.rolling(252).mean()
    sd = s.rolling(252).std()
    z = (s - m) / sd
    return _gate(z >= 1.0, z.isna() | (sd <= 0))


RULES = {
    "C1 ma20 (v1 R1·대조군)": rule_ma20,
    "C2 pct80 (2년 80분위)": rule_pct80,
    "C3 chg60 (+100bp/60봉)": rule_chg60,
    "C4 z10 (1년 z>=1.0)": rule_z10,
}
RULE_WARMUP = {"C1 ma20 (v1 R1·대조군)": 20, "C2 pct80 (2년 80분위)": 504,
               "C3 chg60 (+100bp/60봉)": 60, "C4 z10 (1년 z>=1.0)": 252}


# ── 집합 통계 ────────────────────────────────────────────────────────────────
def jaccard(a: pd.Series, b: pd.Series) -> float:
    """off-day 집합 겹침 = |A∩B| / |A∪B|.

    왜 단순 일치율이 아닌가: off-day 는 소수다. 둘 다 대부분 꺼져 있으면
    '같이 꺼져 있는 날'이 분자를 채워 일치율이 0.9 를 넘는다. 그건 두 게이트가
    닮았다는 뜻이 아니라 **하락장이 드물다**는 뜻이다.
    """
    a, b = a.astype(bool), b.astype(bool)
    union = int((a | b).sum())
    return float(int((a & b).sum()) / union) if union else float("nan")


def episodes(on: pd.Series) -> list:
    """연속 True 구간 → [(시작 idx, 끝 idx)] (양끝 포함)."""
    v = on.astype(bool).to_numpy()
    out, start = [], None
    for i, x in enumerate(v):
        if x and start is None:
            start = i
        elif not x and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(v) - 1))
    return out


def lead_lag_days(a: pd.Series, b: pd.Series, max_gap: int = 60) -> tuple:
    """a 의 각 에피소드 시작 vs 가장 가까운 b 시작의 차이(거래일) 중앙값.

    **양수 = b 가 먼저.** 출력 전용 — P1 을 되돌리는 데 쓰지 않는다.
    """
    sa = [s for s, _ in episodes(a)]
    sb = [s for s, _ in episodes(b)]
    if not sa or not sb:
        return float("nan"), 0, len(sa)
    diffs, unmatched = [], 0
    for s in sa:
        near = min(sb, key=lambda t: abs(t - s))
        if abs(near - s) <= max_gap:
            diffs.append(s - near)
        else:
            unmatched += 1
    if not diffs:
        return float("nan"), 0, len(sa)
    return float(np.median(diffs)), len(diffs), unmatched


def screen(cand: pd.Series, spy_off: pd.Series) -> dict:
    """후보 하나에 **P5 → P6 → P1** 순으로 적용. 판정과 근거를 같이 담는다.

    순서에 의미가 있다. off 비율은 단순 비율이라 에피소드가 몇 회든 읽을 수
    있고, off 100%(항상 켜짐)·0%(안 켜짐)는 에피소드를 볼 것도 없이 게이트가
    아니다 — 그래서 P5 가 먼저다. 반대로 겹침(P1)은 에피소드가 1~2회면 그
    사건 하나에서 나온 수치라 읽을 수 없다 — 그래서 P6 이 P1 앞에 선다.

    ⚠️ '탈락'과 '판정불가'는 다르다. 판정불가는 **데이터가 짧다**는 뜻이지
       룰이 틀렸다는 뜻이 아니다. 섞으면 다음 사람이 멀쩡한 후보를 영구히
       버린다. selftest 가 두 상태를 각각 강제한다.
    """
    n = len(cand)
    off = int(cand.sum())
    rate = off / n if n else float("nan")
    eps = episodes(cand)
    med_len = float(np.median([e - s + 1 for s, e in eps])) if eps else float("nan")
    j = jaccard(spy_off, cand)
    both = int((spy_off.astype(bool) & cand.astype(bool)).sum())
    sa = int(spy_off.sum())
    cond = (both / sa) if sa else float("nan")
    med, matched, _ = lead_lag_days(spy_off, cand)

    if not (P5_OFF_MIN <= rate <= P5_OFF_MAX):
        verdict = "탈락"
        why = (f"P5 off {rate * 100:.1f}% 가 "
               f"{P5_OFF_MIN * 100:.0f}~{P5_OFF_MAX * 100:.0f}% 밖")
    elif len(eps) < P6_MIN_EPISODES:
        verdict = "판정불가"
        why = f"P6 에피소드 {len(eps)}회 < {P6_MIN_EPISODES} — 탈락이 아니라 읽을 수 없음"
    elif np.isfinite(j) and j >= P1_JACCARD_ABORT:
        verdict = "탈락"
        why = f"P1 겹침 {j:.3f} >= {P1_JACCARD_ABORT} — SPY200 의 다른 이름"
    else:
        verdict = "생존"
        why = f"off {rate * 100:.1f}% · 에피 {len(eps)}회 · J {j:.3f}"
    return {"n": n, "off": off, "rate": rate, "eps": len(eps), "med_len": med_len,
            "jaccard": j, "cond": cond, "lead": med, "matched": matched,
            "verdict": verdict, "why": why}


# ══════════════════════════════════════════════════════════════════════════
# Q1 — FMP 창
# ══════════════════════════════════════════════════════════════════════════
def _fetch_spy(from_date: str = "", to_date: str = "") -> tuple:
    """SPY 일봉. (DataFrame, kind).

    ⚠️ 여기서 `fx.hist_range_params()` 를 쓰지 않는다. 그 함수는 **오늘 기준
       룩백** 정책(from = 오늘 − N일)이고, 이 프로브가 묻는 것은 **절대 구간**
       (2007-01-01~)이 오는가다. 정책 헬퍼로 과거 창을 만들면 이 파일이 실행
       날짜마다 다른 질문을 하게 된다.
       → 프로덕션 코드는 계속 `hist_range_params` 만 쓴다. 이 예외는 진단
         파일 안, 이 함수 하나에 갇혀 있다.
    """
    path = "historical-price-eod/full?symbol=SPY"
    if from_date:
        path += f"&from={from_date}"
    if to_date:
        path += f"&to={to_date}"
    data, _status, kind = fh.fmp_get_json_ex(path)
    rows = data.get("historical", data) if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame(), (kind if kind != "ok" else "empty")
    df = pd.DataFrame(rows)
    if "date" not in df.columns or "close" not in df.columns:
        return pd.DataFrame(), "bad_schema"
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return pd.DataFrame({"Close": pd.to_numeric(df["close"], errors="coerce")}).dropna(), "ok"


def probe_window() -> tuple:
    print(f"\n{_SEP}\n■ Q1 — FMP 창 (P2: 평가 구간 확정)\n{_SEP}")
    print("판정 통계는 레코드 수가 아니라 **최소 날짜**다. 창이 무시되면")
    print("개수는 그대로인데 날짜가 안 움직인다 — 개수로는 구분이 안 된다.\n")

    today = date.today().strftime("%Y-%m-%d")
    cap_from = (date.today() - timedelta(days=fx.HIST_MAX_DAYS)).strftime("%Y-%m-%d")
    cases = [
        ("창 없음(기본)", "", ""),
        (f"정책 상한 {fx.HIST_MAX_DAYS}일", cap_from, ""),
        ("2007-01-01~2009-12-31", "2007-01-01", "2009-12-31"),
        ("2007-01-01~오늘 (D3)", "2007-01-01", today),   # v2 신규 — 단일 장기 호출
    ]
    print(f"{'창':<26}{'봉수':>8}{'최소일':>14}{'최대일':>14}   상태")
    print("-" * 76)
    got = {}
    for label, f_, t_ in cases:
        df, kind = _fetch_spy(f_, t_)
        got[label] = df
        if df.empty:
            print(f"{label:<26}{'—':>8}{'—':>14}{'—':>14}   ⚠️ {kind}")
        else:
            print(f"{label:<26}{len(df):>8}{str(df.index[0].date()):>14}"
                  f"{str(df.index[-1].date()):>14}   ok")

    long = got["2007-01-01~오늘 (D3)"]
    deep_ok = (not long.empty) and long.index[0].date() <= date(2007, 3, 1)
    print()
    if deep_ok:
        yrs = (long.index[-1] - long.index[0]).days / 365.25
        print(f"  [O] D3 단일 호출로 {len(long)}봉 · {yrs:.1f}년이 온다 → P2: 2007~ 확정.")
        print("      ⚠️ 별건(이 프로브의 질문 아님): fx.HIST_MAX_DAYS=1826 은 API")
        print("         한계가 아니라 정책 상수다. diag_momentum_rule_compare 상단의")
        print("         '평가 가능 구간 ≈4년' 전제가 이 사실로 반증된다. 여기서")
        print("         고치지 말고 별도 항목으로 올릴 것.")
    else:
        print("  [X] 장기 단일 호출이 잘린다 → P2: 응답 최소일~ 로 좁힌다.")
        print("      Phase 1 판정문에 '어느 하락장을 못 봤는지' 명시할 것.")
    return got, deep_ok


# ══════════════════════════════════════════════════════════════════════════
# Q2 — FRED 시리즈 (P4)
# ══════════════════════════════════════════════════════════════════════════
def _fred_series(fred, sid: str, start: str = None) -> pd.Series:
    try:
        s = (fred.get_series(sid) if start is None
             else fred.get_series(sid, observation_start=start))
        return pd.to_numeric(s, errors="coerce")
    except Exception as e:
        tag = "" if start is None else f" (start={start})"
        print(f"  [X] {sid}{tag} 조회 실패: {e}")
        return pd.Series(dtype=float)


def probe_fred() -> dict:
    print(f"\n{_SEP}\n■ Q2 — FRED 스프레드 시리즈 (P4: 여기가 죽으면 나머지는 무효)\n{_SEP}")
    if not FRED_API_KEY:
        print("  [X] FRED_API_KEY 없음")
        return {}
    try:
        from fredapi import Fred
    except Exception as e:
        print(f"  [X] fredapi 임포트 실패: {e}")
        return {}
    fred = Fred(api_key=FRED_API_KEY)

    # D1 — 명시 observation_start 가 이력을 늘리는가.
    # ⚠️ 판정 통계는 **최소 날짜**다. 점 개수로 재면 "늘었다"와 "다른 구간이
    #    왔다"를 구분할 수 없다(v1 창 프로브에서 배운 것과 같은 형태).
    print("  [D1] 명시 observation_start 프로브 — 판정 통계는 최소 날짜\n")
    print(f"{'시리즈':<16}{'호출':<22}{'점수':>8}{'최소일':>14}{'최대일':>14}")
    print("-" * 76)
    cands = {}
    for sid in (HY_OAS, BAA10Y):
        for tag, start in (("기본(start 없음)", None), (f"start={DEEP_START}", DEEP_START)):
            s = _fred_series(fred, sid, start).dropna()
            if s.empty:
                print(f"{sid:<16}{tag:<22}{'—':>8}{'—':>14}{'—':>14}")
                continue
            print(f"{sid:<16}{tag:<22}{len(s):>8}{str(s.index[0].date()):>14}"
                  f"{str(s.index[-1].date()):>14}")
            prev = cands.get(sid)
            if prev is None or s.index[0] < prev.index[0]:
                cands[sid] = s

    print()
    for sid, s in cands.items():
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        lag = (pd.Timestamp(date.today()) - s.index[-1]).days
        note = ("충분" if yrs >= 10 else
                "빈약 — 하락 국면 표본이 몇 개인지 볼 것" if yrs >= 3 else "불가")
        print(f"  {sid:<14} 채택 {len(s)}점 · {yrs:.1f}년 · 최신지연 {lag}일  → {note}")
        if lag > 10:
            print("      ⚠️ 실운용 게이트는 이 지연을 견뎌야 한다 — Phase 1 에 as-of 지연 필수.")

    n = _fred_series(fred, NFCI).dropna()      # 출력 전용
    if not n.empty:
        print(f"  {NFCI:<14} [출력 전용] {len(n)}점 · {n.index[0].date()}~{n.index[-1].date()}")

    print(f"\n  → P4 {'통과' if cands else '실패 — 여기서 중단한다'}")
    return cands


# ══════════════════════════════════════════════════════════════════════════
# Q3 — 룰 후보 심사 (P6 · P5 · P1)
# ══════════════════════════════════════════════════════════════════════════
def probe_rules(spy: pd.DataFrame, series: dict) -> list:
    print(f"\n{_SEP}\n■ Q3 — 룰 후보 심사 (P5 off비율 · P6 에피소드 · P1 겹침)\n{_SEP}")
    print(f"  P5 관문: off 비율 {P5_OFF_MIN * 100:.0f}~{P5_OFF_MAX * 100:.0f}%. "
          f"SPY200 이 10% 대이므로 같은 자릿수여야 게이트다.")
    print("  ⚠️ 생존자 중에서 고르지 않는다. 통과한 것은 전부 Phase 1 로 넘긴다.\n")

    survivors = []
    for sid, oas in series.items():
        idx = spy.index.intersection(oas.index)
        if len(idx) < SPY_MA_BARS + 120:
            print(f"  ── {sid}: 공통 날짜 {len(idx)}일 — 워밍업 후 남는 구간이 없다. 건너뜀\n")
            continue
        close, o = spy.loc[idx, "Close"], oas.loc[idx]
        spy_full = spy_gate_off(close)

        print(f"  ── {sid} — 공통 {len(idx)}봉 ({idx[0].date()} ~ {idx[-1].date()})")
        print(f"  {'후보':<24}{'평가봉':>8}{'off%':>8}{'에피':>6}{'중앙길이':>9}"
              f"{'J(SPY200)':>11}{'판정':>10}")
        print("  " + "-" * 74)
        for name, fn in RULES.items():
            cand_full = fn(o)
            warm = max(SPY_MA_BARS, RULE_WARMUP[name])
            if len(idx) - warm < 60:
                print(f"  {name:<24}{'—':>8}{'—':>8}{'—':>6}{'—':>9}{'—':>11}"
                      f"{'판정불가':>10}")
                print(f"      └ 워밍업 {warm}봉 + 평가 60봉을 못 채운다 "
                      f"(공통 {len(idx)}봉)")
                continue
            c, s_ = cand_full.iloc[warm:], spy_full.iloc[warm:]
            r = screen(c, s_)
            print(f"  {name:<24}{r['n']:>8}{r['rate'] * 100:>7.1f}%{r['eps']:>6}"
                  f"{r['med_len']:>9.0f}{r['jaccard']:>11.3f}{r['verdict']:>10}")
            print(f"      └ {r['why']}   [출력전용] P(off|spy_off) {r['cond']:.2f} · "
                  f"리드 {r['lead']:+.0f}일(짝 {r['matched']})")
            if r["verdict"] == "생존":
                survivors.append((sid, name, r))

        s_eval = spy_full.iloc[SPY_MA_BARS:]
        se = len(episodes(s_eval))
        print(f"      SPY200 기준: off {int(s_eval.sum())}일 "
              f"({int(s_eval.sum()) / len(s_eval) * 100:.1f}%) · 에피소드 {se}회")
        if se < 3:
            print("      ⚠️ SPY 쪽 하락 국면이 3회 미만이다. P6 은 후보만 보므로")
            print("         이걸 못 잡는다 — 위 J 값은 사실상 그 사건들에서 나왔다.")
        print()
    return survivors


# ══════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    fails = []
    di = pd.date_range("2020-01-01", periods=40, freq="D")

    # 1) 확인일 — 대칭(켜짐·꺼짐 둘 다 N일 요구)
    raw = pd.Series([False] * 10 + [True] + [False] * 5 + [True] * 5 + [False] * 19, index=di)
    st = confirmed_state(raw, 2)
    if bool(st.iloc[10]):
        fails.append("확인일: 1일 스파이크에 켜졌다")
    if not bool(st.iloc[17]):
        fails.append("확인일: 2일 연속인데 안 켜졌다")
    raw2 = pd.Series([True] * 5 + [False] + [True] * 5 + [False] * 29, index=di)
    if not bool(confirmed_state(raw2, 2).iloc[5]):
        fails.append("확인일: 1일 False 에 꺼졌다 — 재무장이 비대칭이다")

    # 2) 네 후보 **전부** 부등호 양방향. 하나라도 뒤집히면 그 후보만 조용히
    #    역방향으로 돈다 — 표에서는 'off 비율이 좀 다른 줄'로만 보인다.
    n = 900
    ix = pd.date_range("2018-01-01", periods=n, freq="B")
    stress = pd.Series(np.concatenate([np.full(600, 3.0), np.linspace(3.0, 9.0, 300)]), index=ix)
    calm = pd.Series(np.concatenate([np.full(600, 9.0), np.linspace(9.0, 3.0, 300)]), index=ix)
    for nm, fn in RULES.items():
        if not bool(fn(stress).iloc[-1]):
            fails.append(f"부등호[{nm}]: 스프레드가 크게 확대됐는데 꺼져 있다")
        if bool(fn(calm).iloc[-1]):
            fails.append(f"부등호[{nm}]: 스프레드가 축소되는데 켜져 있다 — 방향이 뒤집혔다")

    # 3) SPY 게이트는 **반대 방향**(종가는 낮을수록 나쁘다)
    up = pd.Series(np.linspace(100.0, 300.0, 400), index=pd.date_range("2020-01-01", periods=400))
    down = pd.Series(np.linspace(300.0, 100.0, 400), index=pd.date_range("2020-01-01", periods=400))
    if bool(spy_gate_off(up).iloc[-1]):
        fails.append("부등호[SPY]: 상승 추세인데 켜져 있다")
    if not bool(spy_gate_off(down).iloc[-1]):
        fails.append("부등호[SPY]: 하락 추세인데 꺼져 있다")

    # 4) C3 는 **속도** 룰이다 — 높은 수준에서 평평하면 꺼져야 한다.
    #    이 검사가 없으면 C3 를 수준 룰로 잘못 구현해도 2)를 통과한다.
    flat_high = pd.Series(np.full(300, 12.0),
                          index=pd.date_range("2020-01-01", periods=300, freq="B"))
    if bool(rule_chg60(flat_high).iloc[-1]):
        fails.append("C3: 수준만 높고 변화가 0인데 켜졌다 — 속도 룰이 아니라 수준 룰이다")

    # 5) C3 임계 양방향 — +102bp 에서 켜지고 +90bp 에서는 안 켜진다.
    #    ⚠️ 계단 **직후 60봉 안**에서 재야 한다. 꼬리(iloc[-1])로 재면 계단이
    #       60봉 밖으로 밀려 chg 가 0 이고, 임계가 뭐든 항상 꺼져 있다 —
    #       그러면 이 검사는 통과하는 게 아니라 **아무것도 안 재는** 것이다.
    for bump, want in ((1.02, True), (0.90, False)):
        s = pd.Series(np.concatenate([np.full(100, 3.0), np.full(100, 3.0 + bump)]),
                      index=pd.date_range("2020-01-01", periods=200, freq="B"))
        if bool(rule_chg60(s).iloc[130]) != want:      # 계단 100 + 30봉
            fails.append(f"C3 임계: +{bump * 100:.0f}bp 에서 기대와 반대")
    # 같은 계단이 60봉 밖으로 밀리면 꺼져야 한다(속도 룰의 정의)
    s_far = pd.Series(np.concatenate([np.full(100, 3.0), np.full(100, 4.02)]),
                      index=pd.date_range("2020-01-01", periods=200, freq="B"))
    if bool(rule_chg60(s_far).iloc[-1]):
        fails.append("C3: 계단이 60봉 밖인데 켜져 있다 — 창이 안 미끄러진다")

    # 6) Jaccard 3종 — 동일 / 배타 / 희소구간(일치율 퇴화 방지)
    t = pd.Series([True] * 5 + [False] * 5)
    f = pd.Series([False] * 5 + [True] * 5)
    if abs(jaccard(t, t) - 1.0) > 1e-9:
        fails.append("Jaccard: 동일 집합이 1.0 이 아니다")
    if jaccard(t, f) != 0.0:
        fails.append("Jaccard: 배타 집합이 0.0 이 아니다")
    if jaccard(pd.Series([True] + [False] * 99),
               pd.Series([False, True] + [False] * 98)) > 0.01:
        fails.append("Jaccard: 희소 구간에서 부풀었다 — 일치율로 퇴화했다")

    # 7) 에피소드 · 리드/래그 부호(양수 = b 가 먼저)
    ea = pd.Series([False] * 10 + [True] * 5 + [False] * 10)
    eb = pd.Series([False] * 7 + [True] * 5 + [False] * 13)
    if len(episodes(ea)) != 1:
        fails.append("에피소드: 연속 구간 1개를 못 셌다")
    if not (lead_lag_days(ea, eb)[0] > 0):
        fails.append("리드/래그: 부호 규약이 뒤집혔다")

    # 8) 중간 결측이 게이트를 끄지 않는가. **구멍 한가운데**를 본다 —
    #    꼬리로 재면 구멍이 지나간 뒤 스스로 다시 켜져서 멀쩡해 보인다.
    holed = pd.Series(np.linspace(3.0, 9.0, 120),
                      index=pd.date_range("2020-01-01", periods=120))
    if not bool(rule_ma20(holed).iloc[70]):
        fails.append("결측: 대조군이 이미 꺼져 있다 — 검사가 무의미하다")
    holed.iloc[60:62] = np.nan
    if not bool(rule_ma20(holed).iloc[70]):
        fails.append("결측: 중간 2일 구멍에 꺼졌다 — 휴장일이 해제 신호로 읽힌다")

    # 9) 관문이 실제로 무는가 — 양방향. **통과만 재면 관문이 죽어도 초록불이다.**
    #    기준 SPY200: 12% off · 4에피소드 (P1 검사에 에피소드가 필요하다)
    spy_off = pd.Series(([False] * 220 + [True] * 30) * 4)
    if screen(pd.Series([True] * 1000), spy_off)["verdict"] != "탈락":
        fails.append("P5: off 100% 후보가 탈락하지 않았다 — 관문이 죽었다")
    if screen(pd.Series([False] * 1000), spy_off)["verdict"] != "탈락":
        fails.append("P5: off 0% 후보가 탈락하지 않았다 — 안 켜지는 것도 게이트가 아니다")
    #    ⚠️ 경계를 **양쪽에서** 못박는다. off 100%/0% 만 재면 상한을 0.15→0.95
    #       로 늘려도 검사가 전부 통과한다 — 관문이 사실상 사라진 것을 못 잡는다.
    #       아래 36% 는 v1 의 R1 실측값이다. 이 검사가 곧 회귀 앵커다.
    r1_like = pd.Series(([True] * 18 + [False] * 32) * 20)          # 36% · 20회
    if screen(r1_like, spy_off)["verdict"] != "탈락":
        fails.append("P5 상한: v1 R1 과 같은 off 36% 후보가 탈락하지 않았다")
    thin = pd.Series(([True] * 3 + [False] * 97) * 10)              # 3% · 10회
    if screen(thin, spy_off)["verdict"] != "탈락":
        fails.append("P5 하한: off 3% 후보가 탈락하지 않았다")
    two = pd.Series([True] * 50 + [False] * 900 + [True] * 50)      # 10% 인데 2회
    if screen(two, spy_off)["verdict"] != "판정불가":
        fails.append("P6: 에피소드 2회가 판정불가로 안 갔다 — P5 만 보고 통과시켰다")
    ok = pd.Series(([True] * 30 + [False] * 220) * 4)               # 12% · 4회 · 겹침 0
    if screen(ok, spy_off)["verdict"] != "생존":
        fails.append("P5/P6: 조건을 만족하는 후보가 생존으로 안 갔다 — 관문이 과하다")
    if screen(spy_off.copy(), spy_off)["verdict"] != "탈락":
        fails.append("P1: SPY200 과 동일한 후보가 탈락하지 않았다")

    print(f"\n{_SEP}\n■ selftest — 순수 로직 (네트워크 불필요)\n{_SEP}")
    if fails:
        for x in fails:
            print(f"  [X] {x}")
        print(f"\n  {len(fails)}건 실패")
        return 1
    print("  [O] 29개 검사 통과 — 확인일 3 · 부등호 양방향 8(후보 4종×2) · "
          "SPY 방향 2 · C3 속도/임계 4 · Jaccard 3 · 에피소드/부호 2 · "
          "결측 2 · 관문 7(P5 경계 4·P6 1·P1 2)")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

    print(f"\n{_SEP}")
    print("크레딧 스프레드 게이트 Phase 0.5 프로브 (v2) — 읽기 전용")
    print("수익률은 계산하지 않는다. 사전 약정 P1~P6 은 파일 상단에 고정돼 있다.")
    print(_SEP)

    if _selftest() != 0:
        print("\n[중단] selftest 실패 상태에서 실측을 읽지 않는다.")
        return 1

    got, deep_ok = probe_window()

    # ⚠️ v1 버그 수정 지점. 예전엔 '행 수가 가장 많은 응답'을 골랐고, 그래서
    #    Q1 이 2007~ 이라 찍어도 실측은 2021~ 로 돌았다. 이제 deep_ok 를 본다.
    if deep_ok:
        src, spy = "2007-01-01~오늘 (D3)", got["2007-01-01~오늘 (D3)"]
    else:
        src, spy = None, pd.DataFrame()
        for label, df in got.items():
            if not df.empty and len(df) > len(spy):
                src, spy = label, df
    if spy.empty:
        print("\n[중단] SPY 이력을 받지 못했다.")
        return 1
    print(f"\n  [P2] 평가 구간 소스 = '{src}' ({len(spy)}봉, {spy.index[0].date()}~)")

    series = probe_fred()
    if not series:
        print("\n[중단] P4 — 쓸 만한 스프레드 시리즈가 없다. 부분 데이터로 읽지 않는다.")
        return 1

    survivors = probe_rules(spy, series)

    print(f"{_SEP}\n■ 판정 요약\n{_SEP}")
    if not survivors:
        print("  [X] 생존 후보 0 — **프로젝트 종료 검토.** 스프레드에서 SPY200 과")
        print("      다른 축의 게이트를 못 만들었다는 뜻이다.")
        print("      ⚠️ 룰을 더 만들어 통과시키지 말 것 — 후보를 늘리면 결국 뭐")
        print("         하나는 걸린다. '판정불가'가 섞여 있으면 그건 데이터가")
        print("         짧다는 뜻이지 룰이 틀렸다는 뜻이 아니다(구분할 것).")
    else:
        print(f"  [O] 생존 {len(survivors)}건 → **전부** Phase 1 백테스트로 넘긴다.")
        for sid, name, r in survivors:
            print(f"      · {sid} / {name} — off {r['rate'] * 100:.1f}% · "
                  f"에피 {r['eps']}회 · J {r['jaccard']:.3f}")
        print("      ⚠️ 여기서 최고를 고르지 않는다 — 심사에 쓴 데이터로 선택까지")
        print("         하는 것이고, 그건 T6 과 같은 함정이다.")

    print(f"\n{_SEP}")
    print(f"FMP 통계 — {fh.fmp_stats_line()}")
    print("이 프로브는 아무것도 수정하지 않았다. 시트 접촉 0 · 파일 쓰기 0.")
    print(_SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
