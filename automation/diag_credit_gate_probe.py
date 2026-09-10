#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_credit_gate_probe.py — 크레딧 스프레드 게이트 Phase 0 프로브 (읽기 전용)

목적
────
백테스트를 짜기 **전에** 답해야 하는 네 가지 사실 확인. 수익률은 계산하지
않는다 — 성과를 먼저 보면 아래 사전 약정 숫자를 그림을 아는 상태에서 쓰게
된다.

  Q1. FMP `historical-price-eod` 창이 ~1,254봉 **밖**으로 나가나
  Q2. FRED `BAMLH0A0HYM2`(하이일드 OAS) · `NFCI` 가 쓸 만한 상태인가
  Q3. 크레딧 게이트가 SPY 200일선과 **다른 축**인가
  Q4. R1 의 20일선이 우연인가 (파라미터 안정성)

  실행: python automation/diag_credit_gate_probe.py
        python automation/diag_credit_gate_probe.py --selftest   # 네트워크 불필요

아무것도 수정하지 않는다. 시트를 열지 않고, 파일을 쓰지 않는다.
FMP 콜 4 · FRED 콜 2.


═══════════════════════════════════════════════════════════════════════════
사전 커밋 판정 기준 — 2026-09-10 확정. 결과를 본 뒤 재협상하지 않는다.
═══════════════════════════════════════════════════════════════════════════
왜 코드 안에 적나: 산업 모멘텀(item B)과 실적 프리뷰(F1~F5)에서 배운 것이다.
임계값이 대화에만 있으면 결과를 본 뒤 "이번엔 구간이 짧으니까" 로 흔들린다.
파일에 박아두면 흔들 때 diff 가 남는다.

  P1  **중단 기준.** SPY200 off-day 집합과 R1 off-day 집합의 Jaccard 겹침이
      **0.80 이상**이면 이 프로젝트를 여기서 종료한다. 200일선이 이미 하던
      일에 모듈을 새로 만들 이유가 없다.
      ⚠️ "겹치지만 며칠 빠르다" 는 P1 의 예외가 아니다. 리드/래그는 아래
         출력 전용 항목이고, 판정을 되돌리는 데 쓰지 않는다. 속도가 값을
         한다는 주장은 **수익률로** 해야 하고 그건 Phase 1 의 일이다.

  P2  **구간 확정.** Q1 결과가 백테스트 평가 구간을 정한다. 통과하면
      2007~ 을 쓰고, 실패하면 응답의 최소일~ 로 좁히되 **"2020-03 을 못
      봤다"를 Phase 1 판정문에 박는다.** 구간을 사후에 넓히지 않는다.

  P3  **안정성.** MA 창 10/20/30/50 중 **최소 3개**에서
      (a) off-day 수가 창 길이에 대해 단조적이고
      (b) Jaccard 가 20일 기준 ±0.10 이내
      여야 R1 을 Phase 1 백테스트로 넘긴다. 20일에서만 그림이 나오고 옆칸이
      전부 다르면 그 20일은 우연이다.
      ⚠️ 이건 최적화가 아니다. **최고 창을 고르는 데 쓰지 않는다** — R1 의
         20일은 이미 고정이고, 여기서 묻는 건 "그 옆이 절벽인가"뿐이다.

  P4  Q2 가 실패하면(시리즈 결측·중단) 나머지 판정은 **무효**다. 부분 데이터로
      Q3 를 읽지 않는다.

출력 전용 (어떤 판정에도 쓰지 않는다)
  · 리드/래그 중앙값 — 신용이 먼저 조인다는 전제의 참고치
  · NFCI 계열 전부 — 판정은 R1(=HY OAS) 하나만 본다
  · 조건부 확률 P(credit_off | spy_off) — Jaccard 를 읽는 보조 수치


이 프로브가 답할 수 없는 것 (구조적 한계 — 실행 전에 읽을 것)
─────────────────────────────────────────────────────────────
 1) **겹침은 구간에 의존한다.** Q1 이 실패해 4.5년만 남으면 하락 국면이
    2022 한 번뿐이고, 겹침 비율은 사실상 **그 한 번의 사건**에서 나온다.
    P1 이 통과해도 "독립 축임을 확인했다"가 아니라 "한 사건에서 갈렸다"다.
 2) **OAS 는 개정되지 않지만 발표가 밀린다.** FRED 최신 1~2일은 비어 있을 수
    있다. 실운용 게이트는 그 지연을 견뎌야 하는데, 이 프로브는 완성된
    시계열을 뒤에서 보므로 **지연을 재현하지 않는다.** Phase 1 백테스트는
    반드시 as-of 지연을 넣어야 한다.
 3) **휴장일 정렬.** OAS 는 채권 휴장, SPY 는 주식 휴장이라 달력이 다르다.
    여기서는 교집합 날짜만 쓴다 — ffill 로 메우면 게이트가 데이터 없는 날에
    켜진 것처럼 보인다.
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

# ── 축 정의 ──────────────────────────────────────────────────────────────────
HY_OAS_SERIES = "BAMLH0A0HYM2"   # ICE BofA US High Yield OAS, 일별, 1996~
NFCI_SERIES = "NFCI"             # 시카고연준 금융환경지수, 주별 — 출력 전용

R1_MA_BARS = 20                  # R1 고정. P3 는 이 값을 바꾸지 않는다.
R1_CONFIRM_DAYS = 2              # 2일 연속 확인 + 재무장 (regime_core 와 같은 형태)
SPY_MA_BARS = 200                # §3 기존 시장 필터
MA_SENSITIVITY = (10, 20, 30, 50)   # P3

P1_JACCARD_ABORT = 0.80
P3_MIN_STABLE = 3
P3_JACCARD_TOL = 0.10

_SEP = "=" * 76


# ══════════════════════════════════════════════════════════════════════════
# 순수 로직 — 네트워크 없이 검증 가능한 부분은 전부 여기 (selftest 대상)
# ══════════════════════════════════════════════════════════════════════════
def confirmed_state(raw_on: pd.Series, confirm_days: int = R1_CONFIRM_DAYS) -> pd.Series:
    """원시 조건 → **N일 연속 확인 + 재무장** 상태 시계열.

    켜질 때도 꺼질 때도 같은 N일을 요구한다. 한쪽만 확인하면 게이트가
    비대칭이 되고, 그 비대칭은 백테스트에서 '방향성 있는 실력'처럼 보인다.

    ⚠️ 첫 confirm_days-1 일은 판정 불가라 **False 로 시작한다.** NaN 으로 두면
       하류 집합 연산에서 조용히 빠져 겹침 분모가 달라진다.
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


def jaccard(a: pd.Series, b: pd.Series) -> float:
    """두 불리언 시계열의 off-day 집합 겹침 = |A∩B| / |A∪B|.

    왜 단순 일치율이 아닌가: off-day 는 소수다. 게이트가 둘 다 대부분 꺼져
    있으면 '같이 꺼져 있는 날'이 분자를 채워 일치율이 0.9 를 넘는다.
    그건 두 게이트가 닮았다는 뜻이 아니라 **하락장이 드물다**는 뜻이다.
    """
    a = a.astype(bool)
    b = b.astype(bool)
    inter = int((a & b).sum())
    union = int((a | b).sum())
    return float(inter / union) if union else float("nan")


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


def lead_lag_days(a: pd.Series, b: pd.Series, max_gap: int = 60) -> float:
    """a 의 각 에피소드 시작에 대해 가장 가까운 b 시작과의 차이(거래일) 중앙값.

    **양수 = b 가 먼저.** 짝이 없는 에피소드(±max_gap 안에 b 시작 없음)는
    센다는 표시 없이 버리면 안 되므로 호출부에 개수를 같이 돌려준다.

    ⚠️ 출력 전용. P1 을 되돌리는 데 쓰지 않는다.
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


def spy_gate_off(close: pd.Series, ma_bars: int = SPY_MA_BARS) -> pd.Series:
    """§3 기존 필터: 종가 < N일선 → off(신규 매수 중단)."""
    ma = close.rolling(ma_bars).mean()
    # ⚠️ nullable "boolean" 으로 캐스팅한 뒤에 NA 를 넣는다. 넘파이 bool
    #    시리즈에 pd.NA 를 대입하면 TypeError 다(pandas 2.x 부터).
    raw = (close < ma).astype("boolean")
    raw[ma.isna()] = pd.NA
    return confirmed_state(raw, confirm_days=1)      # 기존 필터는 확인일 없음


def credit_gate_off(oas: pd.Series, ma_bars: int = R1_MA_BARS,
                    confirm_days: int = R1_CONFIRM_DAYS) -> pd.Series:
    """R1: HY OAS > 자기 N일선, N일 연속 확인 → off.

    스프레드는 **높을수록 나쁘다** — 부등호 방향이 SPY 와 반대다.
    이 한 글자가 뒤집히면 게이트가 정확히 거꾸로 돌고, 백테스트는
    에러 없이 '역방향으로 잘 맞는 신호'를 보고한다. selftest 가 지킨다.
    """
    ma = oas.rolling(ma_bars).mean()
    raw = (oas > ma).astype("boolean")
    raw[ma.isna()] = pd.NA
    return confirmed_state(raw, confirm_days=confirm_days)


# ══════════════════════════════════════════════════════════════════════════
# Q1 — FMP 창이 1,254봉 밖으로 나가나
# ══════════════════════════════════════════════════════════════════════════
def _fetch_spy(from_date: str = "", to_date: str = "") -> tuple:
    """SPY 일봉. (DataFrame, kind) — 실패 시 (빈 DF, kind).

    ⚠️ 여기서 `fx.hist_range_params()` 를 쓰지 않는다. 그 함수는 **오늘 기준
       룩백** 정책이고(from = 오늘 − N일), 이 프로브가 묻는 것은 **절대 구간**
       (2007-01-01~2009-12-31)이 오는가다. 정책 헬퍼로 과거 창을 만들려면
       '오늘로부터 며칠 전'을 역산해야 하는데, 그러면 이 파일이 실행 날짜에
       따라 다른 질문을 하게 된다.
       → 프로덕션 코드는 계속 `hist_range_params` 만 쓴다. 이 예외는 진단
         파일 안에, 이 함수 하나에 갇혀 있다.
    """
    path = "historical-price-eod/full?symbol=SPY"
    if from_date:
        path += f"&from={from_date}"
    if to_date:
        path += f"&to={to_date}"
    data, status, kind = fh.fmp_get_json_ex(path)
    rows = data.get("historical", data) if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame(), (kind if kind != "ok" else "empty")
    df = pd.DataFrame(rows)
    if "date" not in df.columns or "close" not in df.columns:
        return pd.DataFrame(), "bad_schema"
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    out = pd.DataFrame({"Close": pd.to_numeric(df["close"], errors="coerce")})
    return out.dropna(), "ok"


def probe_window() -> tuple:
    """Q1 — 창 3종 + 정책 상한 1종. 판정 통계는 **응답의 가장 이른 날짜**."""
    print(f"\n{_SEP}\n■ Q1 — FMP 창이 ~1,254봉 밖으로 나가나\n{_SEP}")
    print("판정 통계는 레코드 수가 아니라 **최소 날짜**다. 창이 무시되면")
    print("개수는 그대로인데 날짜가 안 움직인다 — 개수로는 구분이 안 된다.\n")

    today = date.today()
    cap_from = (today - timedelta(days=fx.HIST_MAX_DAYS)).strftime("%Y-%m-%d")
    cases = [
        ("창 없음(기본)", "", ""),
        (f"정책 상한 {fx.HIST_MAX_DAYS}일", cap_from, ""),
        ("2019-01-01~2020-12-31", "2019-01-01", "2020-12-31"),
        ("2007-01-01~2009-12-31", "2007-01-01", "2009-12-31"),
    ]
    print(f"{'창':<26}{'봉수':>8}{'최소일':>14}{'최대일':>14}   상태")
    print("-" * 76)
    got = {}
    for label, f_, t_ in cases:
        df, kind = _fetch_spy(f_, t_)
        got[label] = (df, kind)
        if df.empty:
            print(f"{label:<26}{'—':>8}{'—':>14}{'—':>14}   ⚠️ {kind}")
        else:
            print(f"{label:<26}{len(df):>8}{str(df.index[0].date()):>14}"
                  f"{str(df.index[-1].date()):>14}   ok")

    deep = got["2007-01-01~2009-12-31"][0]
    mid = got["2019-01-01~2020-12-31"][0]
    deep_ok = (not deep.empty) and deep.index[0].date() <= date(2007, 3, 1)
    mid_ok = (not mid.empty) and mid.index[0].date() <= date(2019, 3, 1)

    print()
    if deep_ok:
        print("  [O] 2008 구간이 온다 → P2: 평가 구간 2007~ 로 확정.")
        print("      ⚠️ 부수 발견: fx.HIST_MAX_DAYS=1826 은 **API 한계가 아니라**")
        print("         정책 상수였다는 뜻이다. 그 자체로 별건 검토 대상이다")
        print("         (여기서 바꾸지 말 것 — 이 프로브의 질문이 아니다).")
    elif mid_ok:
        print("  [~] 2020 은 오지만 2008 은 안 온다 → P2: 평가 구간 2019~.")
        print("      코로나 급락은 포함, 금융위기는 제외. 판정문에 명시할 것.")
    else:
        print("  [X] 과거 창이 무시된다 → P2: 응답 최소일~ 로 좁힌다.")
        print("      **'2020-03 을 못 봤다'를 Phase 1 판정문에 박는다.**")
        print("      크레딧 스프레드가 가장 크게 일한 구간이 평가 밖이라는 뜻이므로,")
        print("      통과하더라도 결론 강도를 낮춰서 읽어야 한다.")

    base = got["창 없음(기본)"][0]
    if not base.empty:
        print(f"\n  [참고] 기본 창 실측 {len(base)}봉 — HIST_TD_PER_CD 검산용.")
    return got, {"deep_ok": deep_ok, "mid_ok": mid_ok}


# ══════════════════════════════════════════════════════════════════════════
# Q2 — FRED 시리즈 건강 상태
# ══════════════════════════════════════════════════════════════════════════
def probe_fred() -> tuple:
    print(f"\n{_SEP}\n■ Q2 — FRED 시리즈 상태 (P4: 여기가 죽으면 나머지는 무효)\n{_SEP}")
    if not FRED_API_KEY:
        print("  [X] FRED_API_KEY 없음 — Q2/Q3/Q4 를 실행할 수 없다.")
        return None, None
    try:
        from fredapi import Fred
    except Exception as e:
        print(f"  [X] fredapi 임포트 실패: {e}")
        return None, None

    fred = Fred(api_key=FRED_API_KEY)
    out = {}
    for sid, note in ((HY_OAS_SERIES, "판정용"), (NFCI_SERIES, "출력 전용")):
        try:
            s = pd.to_numeric(fred.get_series(sid), errors="coerce")
        except Exception as e:
            print(f"  [X] {sid} 조회 실패: {e}")
            out[sid] = None
            continue
        clean = s.dropna()
        if clean.empty:
            print(f"  [X] {sid} 전부 결측")
            out[sid] = None
            continue
        tail = s.tail(250)
        gap_days = (pd.Timestamp(date.today()) - clean.index[-1]).days
        print(f"  {sid:<16}({note:<6}) {len(clean):>6}점  "
              f"{clean.index[0].date()} ~ {clean.index[-1].date()}  "
              f"최근250 결측 {int(tail.isna().sum())}  최신지연 {gap_days}일")
        if gap_days > 10:
            print(f"      ⚠️ 최신값이 {gap_days}일 낡았다. 실운용 게이트는 이 지연을")
            print("         견뎌야 한다 — Phase 1 백테스트에 as-of 지연 필수.")
        out[sid] = clean

    ok = out.get(HY_OAS_SERIES) is not None
    print(f"\n  → P4 {'통과' if ok else '실패 — 여기서 중단한다'}")
    return out.get(HY_OAS_SERIES), out.get(NFCI_SERIES)


# ══════════════════════════════════════════════════════════════════════════
# Q3 — 두 게이트가 독립인가  (P1 중단 기준)
# ══════════════════════════════════════════════════════════════════════════
def probe_overlap(spy: pd.DataFrame, oas: pd.Series) -> float:
    print(f"\n{_SEP}\n■ Q3 — 크레딧 게이트가 SPY200 과 다른 축인가 (P1)\n{_SEP}")
    if spy.empty or oas is None or oas.empty:
        print("  [X] 데이터 부족 — 판정 불가")
        return float("nan")

    # 교집합 날짜만 쓴다. ffill 로 메우면 데이터 없는 날에 게이트가 켜진
    # 것처럼 보이고, 그 날들이 겹침 분모에 들어간다.
    idx = spy.index.intersection(oas.index)
    if len(idx) < SPY_MA_BARS + 60:
        print(f"  [X] 공통 날짜 {len(idx)}일 — 200일선 워밍업 후 남는 구간이 없다")
        return float("nan")
    close = spy.loc[idx, "Close"]
    o = oas.loc[idx]

    g_spy = spy_gate_off(close)
    g_cr = credit_gate_off(o)
    warm = max(SPY_MA_BARS, R1_MA_BARS)
    g_spy, g_cr = g_spy.iloc[warm:], g_cr.iloc[warm:]

    n = len(g_spy)
    a, b = int(g_spy.sum()), int(g_cr.sum())
    j = jaccard(g_spy, g_cr)
    both = int((g_spy & g_cr).sum())
    cond = (both / a) if a else float("nan")
    med, matched, unmatched = lead_lag_days(g_spy, g_cr)

    print(f"  평가 구간   {idx[warm].date()} ~ {idx[-1].date()}  ({n}봉)")
    print(f"  SPY200 off  {a:>5}일 ({a / n * 100:.1f}%)   에피소드 {len(episodes(g_spy))}회")
    print(f"  R1 off      {b:>5}일 ({b / n * 100:.1f}%)   에피소드 {len(episodes(g_cr))}회")
    print(f"  동시 off    {both:>5}일")
    print(f"\n  ★ Jaccard 겹침 = {j:.3f}   (P1 중단선 {P1_JACCARD_ABORT})")
    print(f"    [출력 전용] P(credit_off | spy_off) = {cond:.3f}")
    print(f"    [출력 전용] 리드/래그 중앙값 = {med:+.0f}거래일 "
          f"(양수=신용이 먼저 · 짝 {matched}/미짝 {unmatched})")

    print()
    if not np.isfinite(j):
        print("  [X] 판정 불가 — off-day 가 없다. 평가 구간에 하락 국면이 없는 것이다.")
    elif j >= P1_JACCARD_ABORT:
        print(f"  [X] **P1 발동 — 프로젝트 종료.** 겹침 {j:.3f} ≥ {P1_JACCARD_ABORT}.")
        print("      200일선의 다른 이름에 모듈을 새로 만들지 않는다.")
        print("      ⚠️ 리드/래그가 좋아 보여도 P1 을 무르지 않는다(사전 약정).")
    else:
        print(f"  [O] P1 통과 — 겹침 {j:.3f} < {P1_JACCARD_ABORT}. 독립 축의 여지가 있다.")
        print("      ⚠️ '독립임을 확인했다'가 아니다. 평가 구간의 하락 국면이")
        print("         몇 번인지 위 에피소드 수를 볼 것 — 1~2회면 이 숫자는")
        print("         사실상 그 한 사건에서 나왔다.")
    return j


# ══════════════════════════════════════════════════════════════════════════
# Q4 — 파라미터 안정성 (P3)
# ══════════════════════════════════════════════════════════════════════════
def probe_sensitivity(spy: pd.DataFrame, oas: pd.Series) -> None:
    print(f"\n{_SEP}\n■ Q4 — R1 의 20일선이 우연인가 (P3)\n{_SEP}")
    print("최적화가 아니다. R1 의 20일은 고정이고, 여기서 묻는 건 옆칸이 절벽인가뿐이다.\n")
    if spy.empty or oas is None or oas.empty:
        print("  [X] 데이터 부족 — 판정 불가")
        return

    idx = spy.index.intersection(oas.index)
    close, o = spy.loc[idx, "Close"], oas.loc[idx]
    g_spy_full = spy_gate_off(close)

    print(f"{'MA창':>6}{'off일수':>10}{'off비율':>10}{'에피소드':>10}{'Jaccard(vs SPY200)':>22}")
    print("-" * 76)
    rows = {}
    for m in MA_SENSITIVITY:
        g = credit_gate_off(o, ma_bars=m)
        warm = max(SPY_MA_BARS, m)
        gg, gs = g.iloc[warm:], g_spy_full.iloc[warm:]
        j = jaccard(gs, gg)
        rows[m] = (int(gg.sum()), len(gg), len(episodes(gg)), j)
        mark = "  ← R1" if m == R1_MA_BARS else ""
        print(f"{m:>6}{rows[m][0]:>10}{rows[m][0] / rows[m][1] * 100:>9.1f}%"
              f"{rows[m][2]:>10}{j:>22.3f}{mark}")

    counts = [rows[m][0] for m in MA_SENSITIVITY]
    mono = all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1)) or \
        all(counts[i] <= counts[i + 1] for i in range(len(counts) - 1))
    j20 = rows[R1_MA_BARS][3]
    near = sum(1 for m in MA_SENSITIVITY
               if np.isfinite(rows[m][3]) and abs(rows[m][3] - j20) <= P3_JACCARD_TOL)

    print(f"\n  단조성: {'O' if mono else 'X'}   "
          f"Jaccard ±{P3_JACCARD_TOL} 안에 드는 창: {near}/{len(MA_SENSITIVITY)}개")
    if mono and near >= P3_MIN_STABLE:
        print("  [O] P3 통과 — R1 을 Phase 1 백테스트로 넘긴다.")
    else:
        print("  [X] P3 미달 — 20일 주변이 절벽이다. R1 은 이 구간의 우연일 가능성이")
        print("      높다. 창을 바꿔 통과시키지 말 것 — 그건 최적화고, 사전 약정 위반이다.")


# ══════════════════════════════════════════════════════════════════════════
# selftest — 네트워크 불필요. 순수 로직만 양방향으로 검증한다.
# ══════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    fails = []
    idx = pd.date_range("2020-01-01", periods=40, freq="D")

    # 1) 확인일 — N일 연속이어야 켜지고, 같은 N일이어야 꺼진다(대칭)
    raw = pd.Series([False] * 10 + [True] + [False] * 5 + [True] * 5 + [False] * 19,
                    index=idx)
    st = confirmed_state(raw, confirm_days=2)
    if bool(st.iloc[10]):
        fails.append("확인일: 1일짜리 스파이크에 게이트가 켜졌다 — 확인 로직이 죽었다")
    if not bool(st.iloc[17]):
        fails.append("확인일: 2일 연속인데 안 켜졌다")
    if bool(st.iloc[10:16].any()):
        fails.append("확인일: 스파이크 뒤 잔상이 남는다")
    # 꺼짐도 2일을 요구하는지 — 켜진 뒤 1일만 False 면 유지돼야 한다
    raw2 = pd.Series([True] * 5 + [False] + [True] * 5 + [False] * 29, index=idx)
    st2 = confirmed_state(raw2, confirm_days=2)
    if not bool(st2.iloc[5]):
        fails.append("확인일: 1일 False 에 게이트가 꺼졌다 — 재무장이 비대칭이다")

    # 2) 부등호 방향 — **스프레드는 높을수록 나쁘다**. 뒤집히면 백테스트가
    #    에러 없이 역방향 신호를 보고한다. 양방향으로 잰다.
    rising = pd.Series(np.linspace(3.0, 9.0, 120), index=pd.date_range("2020-01-01", periods=120))
    falling = pd.Series(np.linspace(9.0, 3.0, 120), index=pd.date_range("2020-01-01", periods=120))
    if not bool(credit_gate_off(rising).iloc[-1]):
        fails.append("부등호: OAS 가 계속 확대되는데 게이트가 꺼져 있다")
    if bool(credit_gate_off(falling).iloc[-1]):
        fails.append("부등호: OAS 가 계속 축소되는데 게이트가 켜져 있다 — 방향이 뒤집혔다")

    # 3) SPY 게이트도 같은 양방향 (종가는 **낮을수록** 나쁘다 — 반대 방향)
    up = pd.Series(np.linspace(100.0, 300.0, 400), index=pd.date_range("2020-01-01", periods=400))
    down = pd.Series(np.linspace(300.0, 100.0, 400), index=pd.date_range("2020-01-01", periods=400))
    if bool(spy_gate_off(up).iloc[-1]):
        fails.append("부등호: SPY 상승 추세인데 게이트가 켜져 있다")
    if not bool(spy_gate_off(down).iloc[-1]):
        fails.append("부등호: SPY 하락 추세인데 게이트가 꺼져 있다")

    # 4) Jaccard — 동일/배타/부분의 세 극단
    t = pd.Series([True] * 5 + [False] * 5)
    f = pd.Series([False] * 5 + [True] * 5)
    if abs(jaccard(t, t) - 1.0) > 1e-9:
        fails.append("Jaccard: 동일 집합이 1.0 이 아니다")
    if jaccard(t, f) != 0.0:
        fails.append("Jaccard: 배타 집합이 0.0 이 아니다")
    # 둘 다 대부분 꺼져 있을 때 단순 일치율이면 0.9 가 나오는 상황 —
    # Jaccard 는 낮아야 한다. 이 검사가 '왜 일치율이 아닌가'를 고정한다.
    a = pd.Series([True] + [False] * 99)
    b = pd.Series([False] + [True] + [False] * 98)
    if jaccard(a, b) > 0.01:
        fails.append("Jaccard: off-day 희소 구간에서 값이 부풀었다 — 일치율로 퇴화했다")

    # 5) 에피소드 + 리드/래그 부호 — 양수가 'b 가 먼저'여야 한다
    ea = pd.Series([False] * 10 + [True] * 5 + [False] * 10)
    eb = pd.Series([False] * 7 + [True] * 5 + [False] * 13)
    if len(episodes(ea)) != 1:
        fails.append("에피소드: 연속 구간 1개를 못 셌다")
    med, _, _ = lead_lag_days(ea, eb)
    if not (med > 0):
        fails.append(f"리드/래그: b 가 3일 먼저인데 부호가 {med:+.0f} — 부호 규약이 뒤집혔다")

    # 6) 워밍업 구간이 False 로 나가는가 (NaN 이면 집합 연산에서 조용히 빠진다)
    short = pd.Series(np.linspace(3.0, 9.0, 30), index=pd.date_range("2020-01-01", periods=30))
    g = credit_gate_off(short, ma_bars=20)
    if g.iloc[:19].any():
        fails.append("워밍업: MA 미수렴 구간에서 게이트가 켜졌다")
    if g.isna().any():
        fails.append("워밍업: 결과에 NaN 이 있다 — 집합 연산에서 조용히 빠진다")

    # 7) **시리즈 중간 결측**에 게이트가 꺼지지 않는가.
    #    OAS 는 채권 휴장·발표 지연으로 중간에 구멍이 난다. `oas > NaN` 은
    #    False 라서, NA 처리를 빼면 그 구멍이 "조건 해제" 로 읽히고
    #    2일짜리 구멍 하나에 게이트가 조용히 꺼진다. 워밍업 구간만 보는
    #    검사로는 이걸 못 잡는다(그쪽은 두 경로가 같은 값을 낸다).
    holed = pd.Series(np.linspace(3.0, 9.0, 120),
                      index=pd.date_range("2020-01-01", periods=120))
    #    ⚠️ 마지막 값으로 재면 안 된다. 구멍이 지나가면 게이트가 스스로
    #       다시 켜져서 꼬리만 보면 멀쩡해 보인다 — **구멍 한가운데**를 본다.
    #       (rolling 창이 20이라 2일 구멍은 ma 를 22봉 오염시킨다.)
    if not bool(credit_gate_off(holed).iloc[70]):
        fails.append("결측: 대조군(구멍 없음)이 이미 꺼져 있다 — 검사가 무의미하다")
    holed.iloc[60:62] = np.nan
    g7 = credit_gate_off(holed)
    if not bool(g7.iloc[70]):
        fails.append("결측: 중간 2일 구멍에 게이트가 꺼졌다 — 휴장일이 해제 신호로 읽힌다")
    if not bool(g7.iloc[-1]):
        fails.append("결측: 구멍이 지나간 뒤에도 게이트가 안 돌아왔다")

    print(f"\n{_SEP}\n■ selftest — 순수 로직 (네트워크 불필요)\n{_SEP}")
    if fails:
        for f_ in fails:
            print(f"  [X] {f_}")
        print(f"\n  {len(fails)}건 실패")
        return 1
    print("  [O] 15개 검사 전부 통과 (확인일 대칭 · 부등호 양방향 ×2 · "
          "Jaccard 3종 · 에피소드 · 부호 · 워밍업 2종 · 중간결측 3종)")
    return 0


# ══════════════════════════════════════════════════════════════════════════
def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

    print(f"\n{_SEP}")
    print("크레딧 스프레드 게이트 Phase 0 프로브 — 읽기 전용")
    print("수익률은 계산하지 않는다. 사전 약정 P1~P4 는 파일 상단에 고정돼 있다.")
    print(_SEP)

    if _selftest() != 0:
        print("\n[중단] selftest 실패 상태에서 실측을 읽지 않는다.")
        return 1

    got, q1 = probe_window()
    oas, nfci = probe_fred()
    if oas is None:
        print("\n[중단] P4 — FRED 실패. Q3/Q4 를 부분 데이터로 읽지 않는다.")
        return 1

    # P2 로 정해진 구간의 SPY 를 고른다 — 가장 긴 유효 응답.
    best, best_df = None, pd.DataFrame()
    for label, (df, kind) in got.items():
        if not df.empty and len(df) > len(best_df):
            best, best_df = label, df
    print(f"\n  [P2] 평가 구간 소스 = '{best}' ({len(best_df)}봉, "
          f"{best_df.index[0].date()}~)")

    probe_overlap(best_df, oas)
    probe_sensitivity(best_df, oas)

    print(f"\n{_SEP}")
    print(f"FMP 통계 — {fh.fmp_stats_line()}")
    print("이 프로브는 아무것도 수정하지 않았다. 시트 접촉 0 · 파일 쓰기 0.")
    print(_SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
