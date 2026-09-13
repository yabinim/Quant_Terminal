#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_credit_gate_probe.py — Track C 크레딧 게이트 Phase 0 데이터 프로브 (읽기 전용 · 1회성)

무엇을 묻나
──────────
SATELLITE_MANDATE §3 시장 필터("SPY < 자기 200일선 → 신규 매수 중단·비중 축소")에
크레딧 스프레드를 OR 축으로 추가하는 것이 애초에 의미가 있는지 — 즉 신용시장이
주가보다 **먼저** 무너지는지를, 결합 문턱(몇 %p·몇 주)을 정하기 전에 데이터로만
확인한다. 가설 검증이 아니라 이 후보가 Phase 1 사전 약정으로 넘어갈 자격이
있는지를 잰다.

  적용 지점(2026-09-13 확정, 대화 기록) — SATELLITE_MANDATE §3 의 두 번째 축.
    결합 로직 OR, 데이터 소스 FRED BAMLH0A0HYM2(ICE BofA US High Yield OAS) —
    둘 다 사전 결정. OAS 를 고른 이유: 이력이 1996년까지 닿아 위성 15년 창
    (HIST_MAX_DAYS=5478)과 맞고, HYG/LQD(기존 DRG 방식) 대비 두 ETF 의 듀레이션
    차이로 인한 금리 신호 혼입이 없다.

  기존 run_drg_predict.fetch_macro_context 의 "신호 2: 신용 스프레드(HYG/LQD)"
  와의 관계 — 그쪽은 **다음날 시장 방향 예측**용 일봉 6봉 창(5일 변화율)이다.
  위성 게이트는 **주간 리밸런싱** 신호라 호라이즌이 다르다. 재사용이 아니라
  같은 아이디어를 다른 시간축으로 다시 설계한다.

⚠️ 이 스크립트는 **수익률을 한 줄도 계산하지 않는다.** SPY·OAS 모두 이동평균
   교차 "날짜"만 비교한다. 사전 약정 전에 결과를 보면 약정이 무의미해진다.

⚠️ 시트 **쓰기 0.** 콘솔 출력뿐. 신규 매수·매도·알림 상태 머신 미접촉.

측정 방법 — 문턱을 아직 정하지 않고, §3 과 대칭인 방법만 쓴다
──────────────────────────────────────────────────────────
§3 은 "SPY < 자기 200일선"이다. 그래서 OAS 쪽도 대칭으로 "OAS > 자기 200일선"을
쓴다 — 두 축의 "형태"가 결과를 보기 전에 이미 §3 과 일관되므로, 문턱을 골라서
결과에 끼워맞춘 것이 아니다. 교차 판정에는 regime_core 의 기존 관례(2일 확인)를
그대로 가져와 단발성 잡음을 거른다.

사건 목록은 diag_momentum_deep_ref.find_episodes 를 그대로 재사용한다
(EPISODE_DD=-0.12, 재구현 금지) — 사건 정의가 이미 SATELLITE_MANDATE §4① 과
diag_satellite_mandate J 그룹에 묶여 있어, 여기서 새로 만들면 사건 정의가
두 갈래로 갈라진다.

판독 기준 — 실행 **전**에 적는다
──────────────────────────────
  [C-COVER]    OAS 이력이 각 사건의 고점일 기준 이전 200거래일(자기 200일선
               계산에 필요)을 커버하는 사건 비율. 전 사건 100% 미만이면 그
               사건은 C-LEAD 판정에서 제외하고 보고서에 "커버리지 부족"으로
               표시한다.
  [C-LEAD]     사건별 리드/래그일 = SPY 200일선 이탈일 − (그 사건에 가장 가까운
               OAS 200일선 상향교차일, ±LEAD_SEARCH_DAYS 안에서). 양수면
               크레딧이 먼저 걸린 것.
               **판정**: 커버되는 사건 중 **과반 이상**에서 리드(양수)여야
               "조기경보 있음". 4/6 문턱은 §4① 워크포워드 판정
               (diag_momentum_rule_compare T2)과 같은 관례를 그대로 가져온
               것 — 결과와 무관하게 지금 고정한다.
  [C-NOISE]    사건 윈도우(고점 −NOISE_EXCLUDE_DAYS ~ 저점 +NOISE_EXCLUDE_DAYS)
               밖에서의 OAS 상향교차 빈도 — 연 몇 회. 보고 전용이지만 §2 각주의
               "V자 반등 비용" 문제와 직결 — 연 3회를 넘으면 Phase 1 사전
               약정에 빈도 상한을 넣어야 한다는 뜻으로 기록만 한다.
  [C-LAG-DATA] FRED 최신치의 발행 지연 실측 — 오늘 날짜 대비 마지막 관측치.
               월요일 판정에 직전 금요일치가 실제로 잡히는지 확인한다.
  [C-DECIDE]   전 사건 C-COVER ✅ 이고 C-LEAD ≥ 과반 → Phase 1 사전 약정 진행.
               미달 → 종료 후보(§9 관례 — net issuance 처럼 사전 약정 없이
               여기서 끝날 수 있다). 이 스크립트는 판정만 출력하고 아무 파일도
               고치지 않는다.

실행:  python automation/diag_credit_gate_probe.py
       python automation/diag_credit_gate_probe.py --selftest   # 네트워크 불필요
       SELFTEST=1 python automation/diag_credit_gate_probe.py   # 위와 동일(워크플로용)
       FRED_API_KEY=.. FMP_API_KEY=.. python automation/diag_credit_gate_probe.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

# ── 리포 루트 + 자기 폴더를 sys.path 에 (실행 위치 무관) ─────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import diag_momentum_deep_ref as dmr   # noqa: E402 — find_episodes SSOT (재구현 금지)

# ── 사전 약정 상수 (2026-09-13 · 결과 보기 전) ───────────────────────────────
MA_WINDOW = 200                 # SPY·OAS 공통 이동평균 창(거래일) — §3 과 대칭
CONFIRM_DAYS = 2                # regime_core 의 "2일 확인" 관례 재사용 — 단발 잡음 제거
EPISODE_DD = dmr.EPISODE_DD     # -0.12, dmr 과 동일 사건 정의 (드리프트 방지)
LEAD_SEARCH_DAYS = 252          # OAS 교차를 찾는 창 — SPY 이탈일 기준 ±1년(달력일)
NOISE_EXCLUDE_DAYS = 60         # C-NOISE 계산 시 사건 주변 제외 폭(전후, 달력일)
C_LEAD_PASS_FRAC = 0.5          # 문턱 — 과반. §4① 워크포워드 관례(4/6)와 같은 성격
FRED_SERIES = "BAMLH0A0HYM2"    # ICE BofA US High Yield OAS
DEEP_WINDOW_DAYS = 7400         # FMP 단일 호출 상한 초과 요청 — diag_momentum_deep_ref 관례
COVER_TRADING_DAYS = 200        # 사건 고점 이전 OAS 가 갖춰야 할 최소 거래일 수(근사)


# ══════════════════════════════════════════════════════════════════════════════
# 이동평균 교차 — SPY·OAS 공용, 대칭 정의
# ══════════════════════════════════════════════════════════════════════════════
def _confirmed_run_starts(cond: pd.Series, confirm: int = CONFIRM_DAYS) -> pd.Series:
    """cond 가 confirm 일 연속 True 로 막 확정된 날 → True.

    ⚠️ pandas bool shift 함정: `cond.shift(1).fillna(False)` 는 NaN 도입 때문에
       dtype 이 object 로 바뀌고, 그 위에서 `~True`(Python bool) 는 -2 로
       평가되어 **항상 참**이 된다 — confirm 이 걸린 모든 날이 "막 시작한 날"로
       잘못 찍힌다(셀프테스트 T2 가 이 회귀를 잡는다). `shift(1, fill_value=False)`
       로 dtype 을 bool 로 유지해야 `~` 가 제대로 동작한다.
    """
    confirmed = cond.rolling(confirm).sum() >= confirm
    prev = confirmed.shift(1, fill_value=False)
    return confirmed & (~prev)


def ma_crossups(s: pd.Series, window: int = MA_WINDOW,
                 confirm: int = CONFIRM_DAYS) -> pd.DatetimeIndex:
    """s가 자신의 window일 이동평균을 아래→위로 '확정' 교차하는 날짜들."""
    ma = s.rolling(window).mean()
    above = (s > ma) & ma.notna()
    return s.index[_confirmed_run_starts(above, confirm)]


def spy_breach_date(spy: pd.Series, ma: pd.Series, peak_i: int, trough_i: int,
                     confirm: int = CONFIRM_DAYS):
    """사건 구간(peak_i −10봉 여유 ~ trough_i) 안에서 SPY 가 자기 200일선을
    처음 '확정' 이탈하는 날짜. §3 필터의 그 정의와 같다."""
    lo = max(0, peak_i - 10)
    seg, seg_ma = spy.iloc[lo:trough_i + 1], ma.iloc[lo:trough_i + 1]
    below = (seg < seg_ma) & seg_ma.notna()
    conf = below.rolling(confirm).sum() >= confirm
    hit = conf[conf].index
    return hit[0] if len(hit) else None


def nearest_in_window(dates: pd.DatetimeIndex, anchor, window_days: int):
    """anchor ± window_days(달력일) 안에서 anchor 에 가장 가까운 날짜. 없으면 None."""
    if len(dates) == 0:
        return None
    lo, hi = anchor - pd.Timedelta(days=window_days), anchor + pd.Timedelta(days=window_days)
    cand = dates[(dates >= lo) & (dates <= hi)]
    if len(cand) == 0:
        return None
    diffs = np.abs((cand - anchor).days)
    return cand[int(np.argmin(diffs))]


# ══════════════════════════════════════════════════════════════════════════════
# 분석 본체 — 수익률 계산 없음, 날짜 비교만
# ══════════════════════════════════════════════════════════════════════════════
def analyze(spy: pd.Series, oas: pd.Series) -> dict:
    spy, oas = spy.sort_index(), oas.sort_index()
    spy_ma = spy.rolling(MA_WINDOW).mean()
    oas_crossups = ma_crossups(oas, MA_WINDOW, CONFIRM_DAYS)

    episodes = dmr.find_episodes(spy, dd=EPISODE_DD)

    rows, windows = [], []
    for idx, ep in enumerate(episodes, start=1):
        peak_i, trough_i = ep["peak_i"], ep["trough_i"]
        peak_date, trough_date = spy.index[peak_i], spy.index[trough_i]

        need_from = peak_date - pd.Timedelta(days=int(COVER_TRADING_DAYS * 1.45))
        cover_ok = bool(len(oas) and oas.index[0] <= need_from
                        and oas.index[-1] >= peak_date)

        breach = spy_breach_date(spy, spy_ma, peak_i, trough_i)
        oas_hit = lead_days = None
        if breach is not None:
            oas_hit = nearest_in_window(oas_crossups, breach, LEAD_SEARCH_DAYS)
            if oas_hit is not None:
                lead_days = int((breach - oas_hit).days)

        rows.append({"ep": idx, "peak": peak_date, "trough": trough_date,
                     "dd_pct": round(ep["dd"] * 100, 1), "spy_breach": breach,
                     "oas_crossup": oas_hit, "lead_days": lead_days,
                     "cover_ok": cover_ok})
        windows.append((peak_date - pd.Timedelta(days=NOISE_EXCLUDE_DAYS),
                        trough_date + pd.Timedelta(days=NOISE_EXCLUDE_DAYS)))

    outside = [d for d in oas_crossups if not any(lo <= d <= hi for lo, hi in windows)]
    years = max((oas.index[-1] - oas.index[0]).days, 1) / 365.25
    noise_per_year = len(outside) / years

    covered = [r for r in rows if r["cover_ok"]]
    countable = [r for r in covered if r["lead_days"] is not None]
    lead_count = sum(1 for r in countable if r["lead_days"] > 0)

    return {"rows": rows, "noise_per_year": noise_per_year, "n_outside": len(outside),
           "lead_count": lead_count, "countable": len(countable),
           "n_covered": len(covered), "n_episodes": len(rows),
           "oas_last_date": oas.index[-1] if len(oas) else None,
           "spy_last_date": spy.index[-1] if len(spy) else None}


def decide(result: dict) -> bool:
    """C-DECIDE — 전 사건 커버 + 리드 과반. 결과와 무관하게 사전 고정된 규칙."""
    if result["n_covered"] < result["n_episodes"]:
        return False
    if result["countable"] == 0:
        return False
    return (result["lead_count"] / result["countable"]) >= C_LEAD_PASS_FRAC


def print_report(result: dict) -> None:
    print(f"\n[C-COVER] 사건 {result['n_episodes']}개 중 커버 {result['n_covered']}개")
    print("\n[C-LEAD] 사건별 리드(+)/래그(−) — SPY 200일선 이탈일 기준, 거래일 아닌 달력일")
    for r in result["rows"]:
        cov = "✅" if r["cover_ok"] else "❌커버부족"
        lead = f"{r['lead_days']:+d}일" if r["lead_days"] is not None else "n/a"
        print(f"  사건{r['ep']} {r['peak'].date()}~{r['trough'].date()} "
              f"({r['dd_pct']}%) {cov} | SPY이탈={r['spy_breach']} "
              f"OAS교차={r['oas_crossup']} 리드={lead}")
    frac = (result["lead_count"] / result["countable"]) if result["countable"] else 0.0
    print(f"  → 리드 {result['lead_count']}/{result['countable']} ({frac:.0%})"
          f" — 문턱 {C_LEAD_PASS_FRAC:.0%}")
    print(f"\n[C-NOISE] 사건 윈도우 밖 OAS 상향교차 연 {result['noise_per_year']:.2f}회"
          f" (총 {result['n_outside']}건)")
    print(f"\n[C-LAG-DATA] OAS 최신치 {result['oas_last_date']} · "
          f"SPY 최신치 {result['spy_last_date']}")
    verdict = decide(result)
    print(f"\n[C-DECIDE] {'✅ Phase 1 사전 약정 진행' if verdict else '❌ 종료 후보 — 조기경보 없음'}")


# ══════════════════════════════════════════════════════════════════════════════
# 실데이터 수신 — FMP(SPY) · FRED(OAS)
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_spy_deep() -> pd.Series:
    import fmp_extras as fx
    import fmp_http as fh
    data = fh.fmp_get_json(
        "historical-price-eod/full?symbol=SPY" + fx.hist_range_params(DEEP_WINDOW_DAYS))
    rows = data.get("historical", data) if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return pd.to_numeric(df["close"], errors="coerce").dropna()


def _fetch_oas() -> pd.Series:
    from fredapi import Fred
    fred = Fred(api_key=os.environ.get("FRED_API_KEY", "") or None)
    s = pd.to_numeric(fred.get_series(FRED_SERIES), errors="coerce").dropna()
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def main() -> int:
    print(f"[STEP 1] SPY(FMP, {DEEP_WINDOW_DAYS}일 요청) · OAS(FRED {FRED_SERIES}) 수신")
    spy = _fetch_spy_deep()
    oas = _fetch_oas()
    print(f"  SPY {len(spy)}봉 {spy.index[0].date() if len(spy) else '-'} ~ "
          f"{spy.index[-1].date() if len(spy) else '-'}")
    print(f"  OAS {len(oas)}행 {oas.index[0].date() if len(oas) else '-'} ~ "
          f"{oas.index[-1].date() if len(oas) else '-'}")
    if len(spy) < MA_WINDOW or len(oas) < MA_WINDOW:
        print("[ABORT] 이동평균 계산에 필요한 최소 이력 미달")
        return 1
    print("\n[STEP 2] 사건 목록 · 리드/래그 분석 (수익률 계산 없음)")
    result = analyze(spy, oas)
    print_report(result)
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# 셀프테스트 — 네트워크 불필요, 합성 데이터
# ══════════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    fails = []

    # T1 — pandas bool shift 함정 회귀 테스트. 이 테스트가 없으면 위 docstring
    #      경고가 다음 리팩터에서 조용히 재발할 수 있다.
    cond = pd.Series([False, True, True, False, True, True, True, False])
    starts = _confirmed_run_starts(cond, confirm=2)
    if starts.tolist() != [False, False, True, False, False, True, False, False]:
        fails.append(f"T1 confirm-run 회귀: {starts.tolist()}")

    # T2 — 합성 이벤트: 260봉 평탄 → 20봉 −15% 급락 → 회복. find_episodes 재사용
    #      + SPY 200일선 이탈일 계산.
    n = 800
    dates = pd.bdate_range("2015-01-01", periods=n)
    spy_vals = np.empty(n)
    spy_vals[:260] = np.linspace(100, 108, 260)
    drop_len = 20
    spy_vals[260:260 + drop_len] = np.linspace(108, 108 * 0.85, drop_len)
    trough_val = spy_vals[260 + drop_len - 1]
    spy_vals[260 + drop_len:] = np.linspace(trough_val, 115, n - (260 + drop_len))
    spy = pd.Series(spy_vals, index=dates)

    episodes = dmr.find_episodes(spy, dd=EPISODE_DD)
    if len(episodes) != 1:
        fails.append(f"T2 사건 수 {len(episodes)} != 1")
    else:
        spy_ma = spy.rolling(MA_WINDOW).mean()
        ep = episodes[0]
        breach = spy_breach_date(spy, spy_ma, ep["peak_i"], ep["trough_i"])
        if breach is None:
            fails.append("T2 SPY 이탈일이 검출되지 않음")
        else:
            breach_i = spy.index.get_loc(breach)

            # T3 — OAS 가 SPY 이탈보다 15거래일 먼저 자기 200일선을 확정 상향
            #      교차 → lead_days 가 양수·15±3 안에서 검출돼야 한다.
            oas_vals = np.full(n, 4.0)
            lead_target = 15
            start_widen = breach_i - lead_target
            oas_vals[start_widen:start_widen + 30] += np.linspace(0, 2.0, 30)
            oas_vals[start_widen + 30:] += 2.0
            oas = pd.Series(oas_vals, index=dates)

            result = analyze(spy, oas)
            r0 = result["rows"][0]
            if not r0["cover_ok"]:
                fails.append("T3 cover_ok 이 False — 합성 데이터가 커버리지를 만족해야 함")
            if r0["lead_days"] is None or not (12 <= r0["lead_days"] <= 18):
                fails.append(f"T3 lead_days 기대 15±3, 실제 {r0['lead_days']}")
            if not decide(result):
                fails.append("T3 decide() 가 False — 리드 1/1(100%)로 True 여야 함")

            # T4 — SPY 사건과 무관한 독립 시나리오: 평탄 기준선 + 완전히 격리된
            #      2개의 신용 확대 블립(각각 기준선으로 복귀 후 재상승) → 두 번째
            #      블립 앞에서 '확정 상태'가 완전히 리셋돼야 두 번째가 새 크로스업
            #      으로 잡힌다. 사건이 하나도 없는 시리즈이므로 noise 윈도우 제외가
            #      전혀 없다 — n_outside 는 곧 전체 크로스업 수와 같아야 한다.
            def _blip(vals, start, up=15, hold=5, down=15, height=1.0):
                vals[start:start + up] = 4.0 + np.linspace(0, height, up)
                vals[start + up:start + up + hold] = 4.0 + height
                vals[start + up + hold:start + up + hold + down] = (
                    4.0 + height - np.linspace(0, height, down))
                return vals

            flat_dates = pd.bdate_range("2015-01-01", periods=900)
            flat_spy = pd.Series(np.linspace(100, 120, 900), index=flat_dates)  # 사건 0개
            noise_vals = np.full(900, 4.0)
            noise_vals = _blip(noise_vals, 220)
            noise_vals = _blip(noise_vals, 550)
            noise_oas = pd.Series(noise_vals, index=flat_dates)

            noise_episodes = dmr.find_episodes(flat_spy, dd=EPISODE_DD)
            if len(noise_episodes) != 0:
                fails.append(f"T4 전제 위반 — 상승 기준선에서 사건 {len(noise_episodes)}개 발견")
            result4 = analyze(flat_spy, noise_oas)
            if result4["n_outside"] != 2:
                fails.append(f"T4 격리 크로스업 기대 2, 실제 {result4['n_outside']} "
                             f"({result4['noise_per_year']:.2f}/년)")

    # T5 — C-COVER: OAS 이력이 사건 고점보다 늦게 시작하면(=커버 안 됨) cover_ok=False.
    short_oas = pd.Series(np.full(120, 4.0),
                          index=pd.bdate_range("2015-01-01", periods=120))
    result5 = analyze(spy, short_oas) if len(episodes) == 1 else None
    if result5 is not None and result5["rows"][0]["cover_ok"]:
        fails.append("T5 짧은 OAS 이력인데 cover_ok=True (커버리지 게이트 미작동)")

    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 전 항목 통과 (confirm-run 회귀 · 사건추출 · 리드검출 · noise카운트 · cover게이트)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv or str(os.environ.get("SELFTEST", "")).strip() not in ("", "0", "false"):
        sys.exit(_selftest())
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
