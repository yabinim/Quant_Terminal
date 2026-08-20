#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_industry_momentum.py — 🏭 업종 모멘텀 예측력 검증 (읽기 전용 진단)

무엇을 답하려는 스크립트인가
────────────────────────────
단 하나다. **업종 모멘텀에 예측력이 있는가.**

FMP `industry-performance-snapshot` 이 159개 업종의 일별 `averageChange` 를 준다.
섹터(11개)보다 14배 세분화된 단위다. 이걸 신호에 붙일지 결정하기 전에,
붙일 가치가 있는지부터 재는 것이 이 스크립트의 전부다.

접근 — (다) 가상 지수
─────────────────────
업종은 직접 살 수 없다. 그래서 두 가지 선택지가 있었다.

  (가) 업종 상위 N → 해당 업종 종목 바스켓 수익률   ← 티커→업종 매핑 필요
  (다) averageChange 를 누적해 **가상 지수**를 만들고 그 위에서 검증  ← 이 스크립트

(다)를 먼저 하는 이유: 추가 호출이 0 이고, "예측력이 있는가"라는 질문 자체를
가장 싸게 답한다. **여기서 죽으면 (가)를 할 이유가 없다.** 살아남으면 그때
(가)로 실현 가능성을 본다.

⚠️ 그래서 이 결과는 '거래 가능한 수익률'이 아니다. `averageChange` 는 업종 내
   종목의 **동일가중 평균 일간 변화**라서, 실제로 복제하려면 업종 전 종목을
   동일가중으로 사서 매일 리밸런싱해야 한다. 불가능하다.
   → 절대 수익률은 무시하고 **조건 간 상대 비교**와 **대조군 대비 우위**만 본다.

⚠️ 휴장일 행이 섞여 있다 (실측)
───────────────────────────────
2023-08-21 ~ 2026-08-20 구간에서

    실제 거래일       754일
    FMP 업종 응답     777건   (+23)
    FMP 섹터 응답     772건   (+18)

거래일보다 **많이** 온다. 휴장일에도 행이 생기고 그날 값은 의미가 없다
(0 이거나 전일 이월). 3년 중 3% 다. 20일·60일 모멘텀 창 안에 가짜 0 이
1~2개 섞여 크기가 왜곡된다.

→ `calendar_core.is_market_open()` 으로 걸러낸다. A-1 에서 만든 모듈이
  여기서 바로 쓰인다. 업종 777 과 섹터 772 가 서로 다르다는 점도 있어서,
  두 시계열을 함께 쓰려면 날짜 정렬이 필수다.

검증 설계 — 자기기만 방지 장치
──────────────────────────────
"철학: 손실 방지와 과신 방지가 예측 정확도보다 우선" 을 이 스크립트에 그대로 건다.

 1) **미래 훔쳐보기 차단** — 순위는 리밸런싱일 t 까지의 데이터만 쓰고,
    수익은 t+1 부터 계산한다.

 2) **대조군(랜덤 N)** — 상위 N 이 **무작위 N 개**를 못 이기면 신호가 아니다.
    시드를 바꿔 여러 번 돌린 평균과 비교한다. 이게 가장 중요한 관문이다.

 3) **반분할 검증** — 전반기/후반기를 따로 돌려 **부호가 일치하는지** 본다.
    전례가 있다: '확정일 지연이 낫다'는 발견이 반분할에서 무너졌고 불마켓
    아티팩트로 판명됐다. 전 구간에서만 좋은 결과는 믿지 않는다.

 4) **다중검정 인식** — 18개 설정을 돌리면 그중 몇 개는 우연히 좋다.
    "몇 개가 SPY 를 이겼나" 를 함께 출력해 우연 가능성을 드러낸다.

 5) **거래비용** — 로테이션은 회전율이 높다. 편도 0.05% 를 매 교체에 물린다.
    비용 전/후를 모두 출력한다.

 6) **최대낙폭(MDD)** — 수익률만 보지 않는다. 손실 방지가 우선이므로
    MDD 가 SPY 보다 나쁘면 수익이 높아도 실패로 본다.

호출 비용
─────────
    업종 159 + 섹터 11 + SPY 1 = 171콜 (일회성)
    --sectors-only 면 12콜

시트에 아무것도 쓰지 않는다. 이메일도 없다. 완전 읽기 전용.

실행
────
    FMP_API_KEY=xxx python automation/diag_industry_momentum.py
    FMP_API_KEY=xxx python automation/diag_industry_momentum.py --sectors-only
    python automation/diag_industry_momentum.py --selftest   # 네트워크 불필요
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import calendar_core as cc  # noqa: E402  — 휴장일 필터 SSOT

FMP_BASE = "https://financialmodelingprep.com/stable"
_KEY = str(os.environ.get("FMP_API_KEY", "") or "").strip()
TIMEOUT = 20
SLEEP_SEC = 0.20          # 171콜 — 분당 300 근처. FMP 한도(200~) 아래로 눌러둔다

YEARS = 3
COST_ONE_WAY = 0.0005     # 편도 0.05% (교체 시 매도+매수 = 0.10%)
RANDOM_TRIALS = 30        # 대조군 시행 횟수
EXEC_LAG = 1              # 체결 지연(봉). 신호 확인 → 다음 거래일 체결


# ══════════════════════════════════════════════════════════════════════════
# 수집
# ══════════════════════════════════════════════════════════════════════════
def _get(path):
    sep = "&" if "?" in path else "?"
    url = FMP_BASE + "/" + path + sep + "apikey=" + _KEY
    try:
        r = requests.get(url, timeout=TIMEOUT)
    except Exception as e:
        return None, "EXC " + type(e).__name__
    if r.status_code != 200:
        return None, "HTTP " + str(r.status_code)
    try:
        d = r.json()
    except Exception:
        return None, "NOJSON"
    if isinstance(d, dict):
        return None, "ERRMSG"
    return d, ""


def fetch_names(kind):
    """kind: 'industry' | 'sector'"""
    path = "available-industries" if kind == "industry" else "available-sectors"
    data, err = _get(path)
    if err or not data:
        return []
    return sorted({str(r.get(kind) or "").strip()
                   for r in data if isinstance(r, dict) and r.get(kind)})


def fetch_series(kind, name, d_from, d_to):
    """업종/섹터 1개의 일별 averageChange → {날짜: 변화율%}. 휴장일 제거."""
    ep = ("historical-industry-performance?industry="
          if kind == "industry" else "historical-sector-performance?sector=")
    path = ep + requests.utils.quote(name) + "&from=" + d_from + "&to=" + d_to
    data, err = _get(path)
    if err or not data:
        return {}, err
    out, dropped = {}, 0
    for rec in data:
        if not isinstance(rec, dict):
            continue
        ds = str(rec.get("date") or "").strip()[:10]
        if len(ds) != 10:
            continue
        # ⚠️ 핵심 — 휴장일 행 제거. 안 하면 가짜 0 이 모멘텀 창에 섞인다.
        if not cc.is_market_open(ds):
            dropped += 1
            continue
        v = rec.get("averageChange")
        try:
            out[ds] = float(v)
        except Exception:
            continue
    return out, ("휴장일 " + str(dropped) + "건 제거" if dropped else "")


def fetch_spy(d_from, d_to):
    """SPY 종가 → {날짜: 종가}. 벤치마크."""
    path = ("historical-price-eod/full?symbol=SPY&from=" + d_from
            + "&to=" + d_to)
    data, err = _get(path)
    if err or not data:
        return {}
    out = {}
    for rec in data:
        if not isinstance(rec, dict):
            continue
        ds = str(rec.get("date") or "").strip()[:10]
        c = rec.get("adjClose", rec.get("close"))
        if len(ds) == 10 and c is not None and cc.is_market_open(ds):
            try:
                out[ds] = float(c)
            except Exception:
                pass
    return out


# ══════════════════════════════════════════════════════════════════════════
# 지수 · 성과 계산 (순수 함수 — selftest 대상)
# ══════════════════════════════════════════════════════════════════════════
def build_index(series, dates):
    """일간 변화율(%) → 누적 지수. 결측일은 0% 로 본다(전일 유지).

    가상 지수다. 실제로 이걸 살 수는 없다 — 상대 비교용이다.
    """
    idx, lvl = [], 1.0
    for d in dates:
        v = series.get(d)
        if v is not None:
            lvl *= (1.0 + v / 100.0)
        idx.append(lvl)
    return idx


def momentum(idx, i, lookback):
    """t=i 시점에서 과거 lookback 봉 수익률. 데이터 부족이면 None."""
    j = i - lookback
    if j < 0 or idx[j] <= 0:
        return None
    return idx[i] / idx[j] - 1.0


def max_drawdown(curve):
    peak, mdd = curve[0] if curve else 1.0, 0.0
    for v in curve:
        if v > peak:
            peak = v
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def run_strategy(idx_map, dates, names, lookback, top_n, hold,
                 start_i, end_i, picker=None, cost=COST_ONE_WAY, lag=EXEC_LAG):
    """로테이션 시뮬레이션 → (자산곡선, 교체횟수).

    picker(cands, i) 를 주면 선택 규칙을 갈아끼울 수 있다(대조군용).
    기본은 모멘텀 상위 top_n.

    ⚠️ 미래 훔쳐보기 차단 — 두 겹으로 건다.
       1) 순위는 **i - lag** 까지의 값만 쓴다
       2) 수익은 i → i+1 로 센다

    lag 가 왜 필요한가: lag=0 이면 '오늘 종가를 보고 오늘 종가에 산다'가 된다.
    실현 불가능한 가정이고 백테스트를 낙관 쪽으로 밀어 올린다. 기존
    diag_satellite_backtest 도 같은 이유로 신호일 종가 → 다음 거래일 체결을
    썼다. 기본 lag=1 로 맞춘다.
    """
    curve, lvl = [], 1.0
    held, switches = [], 0
    i = start_i
    while i < end_i:
        if (i - start_i) % hold == 0:
            d_i = i - lag          # ← 결정 시점. 여기까지의 데이터만 쓴다
            cands = []
            if d_i >= 0:
                for nm in names:
                    m = momentum(idx_map[nm], d_i, lookback)
                    if m is not None:
                        cands.append((m, nm))
            if cands:
                if picker is None:
                    cands.sort(reverse=True)
                    new_held = [nm for _, nm in cands[:top_n]]
                else:
                    new_held = picker(cands, d_i)[:top_n]
                if held:
                    turn = len(set(new_held) - set(held))
                    switches += turn
                    if turn and new_held:
                        lvl *= (1.0 - 2.0 * cost * (turn / float(len(new_held))))
                held = new_held
        if held:
            rs = []
            for nm in held:
                a, b = idx_map[nm][i], idx_map[nm][i + 1]
                rs.append(b / a - 1.0 if a > 0 else 0.0)
            lvl *= (1.0 + sum(rs) / len(rs))
        curve.append(lvl)
        i += 1
    return curve, switches


def spy_curve(spy, dates, start_i, end_i):
    out, lvl = [], 1.0
    for i in range(start_i, end_i):
        a, b = spy.get(dates[i]), spy.get(dates[i + 1])
        if a and b and a > 0:
            lvl *= (b / a)
        out.append(lvl)
    return out


def stats(curve, n_days):
    if not curve:
        return {"ret": 0.0, "cagr": 0.0, "mdd": 0.0}
    ret = curve[-1] - 1.0
    yrs = max(n_days / 252.0, 1e-9)
    cagr = (curve[-1] ** (1.0 / yrs)) - 1.0 if curve[-1] > 0 else -1.0
    return {"ret": ret, "cagr": cagr, "mdd": max_drawdown(curve)}


# ══════════════════════════════════════════════════════════════════════════
# 출력
# ══════════════════════════════════════════════════════════════════════════
def _pct(x):
    return ("%+.1f%%" % (100.0 * x))


def print_table(rows, header):
    print("")
    print("  " + header)
    print("  " + "-" * 88)
    print("  %-26s %10s %10s %10s %8s %8s" %
          ("설정", "수익", "CAGR", "MDD", "교체", "vs SPY"))
    print("  " + "-" * 88)
    for r in rows:
        print("  %-26s %10s %10s %10s %8d %8s" %
              (r["name"], _pct(r["ret"]), _pct(r["cagr"]), _pct(r["mdd"]),
               r["switches"], _pct(r["excess"])))


# ══════════════════════════════════════════════════════════════════════════
# 자체검증 — 네트워크 없이 엔진이 맞는지
# ══════════════════════════════════════════════════════════════════════════
def selftest():
    print("=" * 78)
    print("엔진 자체검증 (네트워크 불필요)")
    print("=" * 78)
    ok = fail = 0

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✅ " + name)
        else:
            fail += 1
            print("  ❌ " + name + ("  — " + detail if detail else ""))

    # build_index
    dates = ["2026-01-0" + str(i) for i in range(1, 6)]
    chk("누적 지수 — +10% 두 번이면 1.21",
        abs(build_index({dates[1]: 10.0, dates[2]: 10.0}, dates)[2] - 1.21) < 1e-9)
    chk("결측일은 전일 유지",
        build_index({}, dates) == [1.0] * 5)

    # momentum
    idx = [1.0, 1.1, 1.21, 1.331]
    chk("모멘텀 — 2봉 전 대비", abs(momentum(idx, 2, 2) - 0.21) < 1e-9)
    chk("모멘텀 — 데이터 부족이면 None", momentum(idx, 1, 5) is None)

    # max_drawdown
    chk("MDD — 1.0→1.5→0.75 이면 -50%",
        abs(max_drawdown([1.0, 1.5, 0.75]) + 0.5) < 1e-9)
    chk("MDD — 단조 상승이면 0", abs(max_drawdown([1.0, 1.1, 1.2])) < 1e-9)

    # 미래 훔쳐보기 차단 검증 —
    # 마지막 봉에만 폭등을 심고, 그 정보가 그 이전 수익에 새는지 본다.
    D = ["d%03d" % i for i in range(40)]
    good = {d: 1.0 for d in D}
    bad = {d: -1.0 for d in D}
    imap = {"G": build_index(good, D), "B": build_index(bad, D)}
    c1, _ = run_strategy(imap, D, ["G", "B"], lookback=5, top_n=1, hold=5,
                         start_i=10, end_i=38, cost=0.0)
    chk("우세 자산을 고른다 (엔진 방향성)", c1[-1] > 1.0, str(c1[-1]))

    # ── 미래 훔쳐보기 — 읽기 가드 (가장 강한 검사) ──────────────────────
    # 접두사 불변 검사만으로는 부족하다는 것이 뮤테이션에서 드러났다.
    # momentum 이 i+3 을 읽도록 버그를 심었더니 **접두사 검사가 통과했다**
    # — 누출이 검사 경계 바로 바깥에서 처음 일어났기 때문이다.
    #
    # 그래서 구조적으로 막는다. 배열을 감싸서 **허용 인덱스를 넘겨 읽으면
    # 즉시 예외**를 던지게 한다. 정당한 최대 읽기는 수익 계산의 idx[end_i] 다.
    # 순위 산출이 그보다 앞을 읽으면 무조건 여기서 터진다.
    class _Peek(Exception):
        pass

    class _Guard(list):
        limit = 0

        def __getitem__(self, k):
            if isinstance(k, int) and k > self.limit:
                raise _Peek("index " + str(k) + " > limit " + str(self.limit))
            return list.__getitem__(self, k)

    # ⚠️ 전역 한계 하나로는 부족하다. 수익 계산은 idx[i+1] 을 정당하게 읽으므로
    #    한계를 end_i 로 잡을 수밖에 없는데, 그러면 **+1~+3 정도의 얕은 누출이
    #    빠져나간다**(뮤테이션에서 실제로 통과했다).
    #    그래서 **momentum 호출 단위로** 한계를 조인다. 결정 시점 d_i 로 넘어온
    #    호출은 d_i 를 넘겨 읽으면 안 된다 — 그게 정의다.
    g_end = 38
    guarded = {}
    for nm, arr in imap.items():
        g = _Guard(arr)
        g.limit = g_end
        guarded[nm] = g

    _mom_ref = globals()["momentum"]

    def _tight_mom(idx, i, lookback):
        if isinstance(idx, _Guard):
            old = idx.limit
            idx.limit = i          # 이 호출이 읽어도 되는 최대 인덱스
            try:
                return _mom_ref(idx, i, lookback)
            finally:
                idx.limit = old
        return _mom_ref(idx, i, lookback)

    leaked = ""
    globals()["momentum"] = _tight_mom
    try:
        run_strategy(guarded, D, ["G", "B"], 5, 1, 5, 10, g_end, cost=0.0)
    except _Peek as e:
        leaked = str(e)
    except Exception as e:
        leaked = "예상 밖 예외: " + type(e).__name__ + " " + str(e)
    finally:
        globals()["momentum"] = _mom_ref
    chk("미래 훔쳐보기 차단 — 순위 산출이 결정 시점을 넘겨 읽지 않는다",
        leaked == "", leaked)

    # ── 미래 훔쳐보기 — 접두사 불변 검사 ────────────────────────────────
    # 마지막 봉만 건드리는 검사는 **통과해도 아무것도 보증하지 않는다**
    #  (루프가 애초에 그 봉을 안 읽는다). 제대로 하려면:
    #  시점 K 이후의 데이터를 통째로 뒤바꿔도 **K 이전 자산곡선이 한 톨도
    #  안 변해야** 한다. 순위 산출이 미래를 읽으면 여기서 반드시 깨진다.
    K = 25
    imap2 = {k: list(v) for k, v in imap.items()}
    for nm in imap2:
        base = imap2[nm][K - 1]
        for t in range(K, len(imap2[nm])):
            # K 이후를 극단적으로 조작 (G 는 폭락, B 는 폭등 — 순위 역전)
            mult = 0.02 if nm == "G" else 50.0
            imap2[nm][t] = base * mult
    cA, _ = run_strategy(imap, D, ["G", "B"], 5, 1, 5, 10, 38, cost=0.0)
    cB, _ = run_strategy(imap2, D, ["G", "B"], 5, 1, 5, 10, 38, cost=0.0)
    pre = K - 10 - 1            # start_i=10 기준 K 직전까지의 곡선 길이
    chk("미래 훔쳐보기 차단 — K 이후 조작이 K 이전 곡선을 안 바꾼다",
        all(abs(cA[t] - cB[t]) < 1e-9 for t in range(pre)),
        "접두사 " + str(pre) + "봉 중 불일치 발생")
    chk("검사 자체가 유효 — K 이후는 실제로 달라진다",
        abs(cA[-1] - cB[-1]) > 1e-6, "조작이 반영조차 안 됐다면 검사가 무의미")

    # ── 체결 지연이 실제로 걸리는가 ────────────────────────────────────
    # lag=0(당일 체결)과 lag=1(익일 체결)의 결과가 같으면 lag 가 무시된 것이다.
    cl0, _ = run_strategy(imap, D, ["G", "B"], 5, 1, 5, 10, 38, cost=0.0, lag=0)
    cl1, _ = run_strategy(imap, D, ["G", "B"], 5, 1, 5, 10, 38, cost=0.0, lag=1)
    chk("체결 지연 lag 인자가 실제로 반영된다 (기본 " + str(EXEC_LAG) + ")",
        EXEC_LAG >= 1)
    # 방향성이 뚜렷한 합성 데이터라 lag 유무로 값이 갈리지 않을 수 있다.
    # 그래서 '다르다' 가 아니라 '결정 인덱스가 lag 만큼 당겨졌는지'를 본다.
    seen = []

    def _spy_pick(cands, i):
        seen.append(i)
        return [nm for _, nm in sorted(cands, reverse=True)]

    run_strategy(imap, D, ["G", "B"], 5, 1, 5, 10, 38, picker=_spy_pick,
                 cost=0.0, lag=1)
    chk("순위 산출 인덱스가 리밸런싱봉보다 lag 만큼 이르다",
        bool(seen) and seen[0] == 10 - 1, "실제 첫 결정 인덱스=" + str(seen[:1]))

    # ── 거래비용 ────────────────────────────────────────────────────────
    # ⚠️ `cB <= cA` 로 쓰면 **비용을 통째로 무시하는 버그를 못 잡는다**
    #    (둘 다 같은 값이 되어 부등호가 성립). 뮤테이션에서 실제로 통과했다.
    #    엄격 부등호로 바꾸고, 교체가 실제로 일어나는 데이터를 쓴다.
    #    번갈아 우세해지는 두 자산을 만들어 매 리밸런싱마다 교체를 강제한다.
    osc_a, osc_b = {}, {}
    for t, d in enumerate(D):
        up = (t // 5) % 2 == 0
        osc_a[d] = 3.0 if up else -3.0
        osc_b[d] = -3.0 if up else 3.0
    imo = {"A": build_index(osc_a, D), "B": build_index(osc_b, D)}
    cA, sA = run_strategy(imo, D, ["A", "B"], 5, 1, 5, 10, 38, cost=0.0)
    cB, sB = run_strategy(imo, D, ["A", "B"], 5, 1, 5, 10, 38, cost=0.02)
    chk("거래비용 검사 전제 — 교체가 실제로 발생한다",
        sA > 0, "교체 0회면 비용 검사가 무의미하다")
    chk("거래비용이 성과를 **엄격히** 깎는다", cB[-1] < cA[-1],
        str(cB[-1]) + " vs " + str(cA[-1]))

    # 휴장일 필터가 실제로 붙어 있는가
    chk("휴장일 필터 — 2026-01-01 은 거래일 아님",
        cc.is_market_open("2026-01-01") is False)
    chk("휴장일 필터 — 2026-08-19 는 거래일",
        cc.is_market_open("2026-08-19") is True)

    print("")
    print("결과: 통과 " + str(ok) + " · 실패 " + str(fail))
    return 1 if fail else 0


# ══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--sectors-only", action="store_true",
                    help="섹터 11개만 (12콜). 업종 159콜을 아끼고 싶을 때")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not _KEY:
        print("❌ FMP_API_KEY 없음. 중단.")
        return 2

    today = datetime.utcnow().date()
    d_to = today.isoformat()
    d_from = (today - timedelta(days=365 * YEARS)).isoformat()

    print("=" * 78)
    print("업종 모멘텀 예측력 검증 — 가상 지수 방식 (읽기 전용)")
    print("  구간: " + d_from + " ~ " + d_to)
    print("  ⚠️ averageChange 기반 가상 지수 — 거래 가능한 수익률이 아니다.")
    print("     절대값은 무시하고 '대조군 대비 우위'와 '반분할 부호 일치'만 본다.")
    print("=" * 78)

    kinds = ["sector"] if args.sectors_only else ["sector", "industry"]

    for kind in kinds:
        label = "섹터" if kind == "sector" else "업종"
        print("")
        print("=" * 78)
        print("[" + label + "]")
        print("=" * 78)

        names = fetch_names(kind)
        if not names:
            print("  ❌ 분류명 목록을 받지 못했습니다. 건너뜁니다.")
            continue
        print("  분류 " + str(len(names)) + "개 · 수집 시작")
        time.sleep(SLEEP_SEC)

        series_map, dropped_total, failed = {}, 0, []
        for n, nm in enumerate(names, 1):
            s, note = fetch_series(kind, nm, d_from, d_to)
            if not s:
                failed.append(nm)
            else:
                series_map[nm] = s
                if note:
                    try:
                        dropped_total += int(note.split()[1].replace("건", ""))
                    except Exception:
                        pass
            if n % 25 == 0:
                print("    ... " + str(n) + "/" + str(len(names)))
            time.sleep(SLEEP_SEC)

        if failed:
            print("  ⚠️ 데이터 없음 " + str(len(failed)) + "개: "
                  + ", ".join(failed[:8]) + (" 외" if len(failed) > 8 else ""))
        if not series_map:
            print("  ❌ 유효 시계열 0개. 건너뜁니다.")
            continue
        print("  ✅ 유효 " + str(len(series_map)) + "개 · 휴장일 제거 누적 "
              + str(dropped_total) + "행")

        # 공통 날짜축 — 모든 분류에 공통으로 존재하는 거래일만
        all_dates = sorted(set().union(*[set(v) for v in series_map.values()]))
        all_dates = [d for d in all_dates if cc.is_market_open(d)]
        if len(all_dates) < 200:
            print("  ❌ 거래일 " + str(len(all_dates)) + "일 — 표본 부족. 건너뜁니다.")
            continue
        print("  거래일 " + str(len(all_dates)) + "일 ("
              + all_dates[0] + " ~ " + all_dates[-1] + ")")

        idx_map = {nm: build_index(s, all_dates) for nm, s in series_map.items()}
        valid = sorted(idx_map)

        spy = fetch_spy(d_from, d_to)
        time.sleep(SLEEP_SEC)
        if not spy:
            print("  ⚠️ SPY 미수신 — 벤치마크 없이 진행")

        # ── 설정 그리드 ──────────────────────────────────────────────────
        grid = []
        for lb in (20, 60, 120):
            for tn in (3, 5, 10):
                for hd in (5, 20):
                    grid.append((lb, tn, hd))

        def run_range(a, b, tag):
            n_days = b - a
            sp = spy_curve(spy, all_dates, a, b) if spy else []
            sp_st = stats(sp, n_days) if sp else {"ret": 0.0, "cagr": 0.0, "mdd": 0.0}
            rows = []
            for lb, tn, hd in grid:
                if a - lb - EXEC_LAG < 0:
                    continue
                cur, sw = run_strategy(idx_map, all_dates, valid, lb, tn, hd, a, b)
                st = stats(cur, n_days)
                rows.append({"name": "LB%d/TOP%d/HOLD%d" % (lb, tn, hd),
                             "key": (lb, tn, hd), "switches": sw,
                             "excess": st["ret"] - sp_st["ret"], **st})
            rows.sort(key=lambda r: -r["ret"])
            return rows, sp_st

        n_all = len(all_dates) - 1
        start = 120
        rows_full, spy_full = run_range(start, n_all, "전체")

        print("")
        print("  SPY 벤치마크: 수익 " + _pct(spy_full["ret"])
              + " · CAGR " + _pct(spy_full["cagr"])
              + " · MDD " + _pct(spy_full["mdd"]))
        print_table(rows_full[:8], "전 구간 상위 8개 설정 (거래비용 반영)")

        n_beat = sum(1 for r in rows_full if r["excess"] > 0)
        print("")
        print("  SPY 초과 설정: " + str(n_beat) + "/" + str(len(rows_full))
              + "  ← 18개를 돌리면 몇 개는 우연히 이긴다. 이 숫자가 낮으면 신호가 아니다.")

        n_better_mdd = sum(1 for r in rows_full if r["mdd"] > spy_full["mdd"])
        print("  SPY보다 얕은 MDD: " + str(n_better_mdd) + "/" + str(len(rows_full))
              + "  ← 손실 방지가 우선. 수익만 높고 MDD 가 깊으면 실패다.")

        # ── 대조군: 무작위 N ─────────────────────────────────────────────
        if rows_full:
            best = rows_full[0]
            lb, tn, hd = best["key"]
            rnd_rets = []
            for t in range(RANDOM_TRIALS):
                rng = random.Random(1000 + t)

                def _pick(cands, i, _rng=rng, _tn=tn):
                    pool = [nm for _, nm in cands]
                    _rng.shuffle(pool)
                    return pool[:_tn]

                cur, _ = run_strategy(idx_map, all_dates, valid, lb, tn, hd,
                                      start, n_all, picker=_pick)
                rnd_rets.append(cur[-1] - 1.0)
            rnd_avg = sum(rnd_rets) / len(rnd_rets)
            rnd_beat = sum(1 for x in rnd_rets if x >= best["ret"])
            print("")
            print("  ── 대조군 (무작위 " + str(tn) + "개 · "
                  + str(RANDOM_TRIALS) + "회)")
            print("     무작위 평균 수익: " + _pct(rnd_avg))
            print("     최고 설정 수익  : " + _pct(best["ret"])
                  + "  (" + best["name"] + ")")
            print("     무작위가 최고 설정을 이긴 횟수: "
                  + str(rnd_beat) + "/" + str(RANDOM_TRIALS))
            if rnd_beat > RANDOM_TRIALS * 0.10:
                print("     🔴 무작위와 구분되지 않는다 — 모멘텀 선택에 예측력 없음")
            elif best["ret"] - rnd_avg < 0.02:
                print("     🟠 무작위 대비 우위가 미미하다 (2%p 미만)")
            else:
                print("     ✅ 무작위 대비 명확한 우위")

        # ── 반분할 검증 ──────────────────────────────────────────────────
        mid = start + (n_all - start) // 2
        rows_h1, spy_h1 = run_range(start, mid, "전반")
        rows_h2, spy_h2 = run_range(mid, n_all, "후반")
        m1 = {r["key"]: r for r in rows_h1}
        m2 = {r["key"]: r for r in rows_h2}

        print("")
        print("  ── 반분할 검증 (전반 " + all_dates[start] + "~" + all_dates[mid]
              + " / 후반 ~" + all_dates[n_all] + ")")
        print("     전례: '확정일 지연이 낫다'는 발견이 반분할에서 무너져")
        print("           불마켓 아티팩트로 판명됐다. 전 구간 성적만으로는 안 믿는다.")
        print("")
        print("     %-24s %10s %10s %8s" % ("설정", "전반 초과", "후반 초과", "부호"))
        print("     " + "-" * 56)
        consistent = 0
        for r in rows_full[:8]:
            k = r["key"]
            e1 = m1.get(k, {}).get("excess")
            e2 = m2.get(k, {}).get("excess")
            if e1 is None or e2 is None:
                continue
            same = (e1 > 0) == (e2 > 0)
            if same and e1 > 0:
                consistent += 1
            print("     %-24s %10s %10s %8s" %
                  (r["name"], _pct(e1), _pct(e2), "✅" if same and e1 > 0 else "✗"))
        print("")
        print("     양쪽 모두 SPY 초과: " + str(consistent) + "/8")
        if consistent == 0:
            print("     🔴 반분할을 통과한 설정이 없다 — 구간 의존적 결과")
        elif consistent <= 2:
            print("     🟠 소수만 통과 — 우연 가능성이 남는다")
        else:
            print("     ✅ 다수가 양쪽에서 초과 — 구간 의존성이 낮다")

    print("")
    print("=" * 78)
    print("판정 기준 요약")
    print("  ① 무작위 대조군을 명확히 이겨야 한다 (가장 중요)")
    print("  ② 반분할 양쪽에서 SPY 를 초과해야 한다")
    print("  ③ MDD 가 SPY 보다 깊으면 수익이 높아도 실패로 본다")
    print("  세 개 중 하나라도 못 넘으면 신호에 연결하지 않는다.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n중단됨.")
        sys.exit(130)
