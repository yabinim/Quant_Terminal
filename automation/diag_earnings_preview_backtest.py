# -*- coding: utf-8 -*-
"""
diag_earnings_preview_backtest.py  (v2)
───────────────────────────────────────
실적 발표 **전** 진입 판단의 요인 유효성 검증. workflow_dispatch 전용.

v1 대비 바뀐 것
  1) F4(등급 변경) 파싱 수정 + **원본 스키마 진단 출력**
     v1 은 1130건 전부 F4=0 이었다. FMP 응답 필드명이 코드 가정과 다른 것으로 보여
     여러 후보 필드를 훑고, 첫 레코드의 원본 키를 그대로 찍어 확인 가능하게 했다.
  2) **방향 적중률 직접 측정** — 스코어별 '실제 갭 상승 비율'.
     v1 은 수익률 크기만 봤다. "오를까 내릴까"에 답하려면 방향을 직접 재야 한다.
     기준선은 50% 가 아니라 **무조건상승 대조군**(시장이 우상향하므로).
  3) **임계 설정 가능성 판정** — 스코어별 상승 확률이 단조 증가해야 "N점 이상 매수"를
     정당화할 수 있다. 단조가 아니면 어떤 임계도 근거가 없고, 그 자체가 결론이다.
  4) **진입 5 x 청산 7 격자** — 언제 사서 언제 팔지를 전부 비교.
     '반응일종가' 진입(갭을 보고 나서 삼)이 격자에 있어, 예측이 관찰보다 나은지 갈린다.
  5) ETF 자동 제외 + 종목 간 sleep (레이트리밋)

검증 규율
  - look-ahead 차단: 모든 요인은 진입 시점 **이전** 데이터만 사용
  - half-split: 시간 기준 전반/후반 부호 일치 필수
  - 초과수익(-SPY) 기준: 강세장 베타를 엣지로 오인하지 않기 위해
  - 다중검정 경고: 격자 35칸은 우연히 좋아 보이는 칸을 반드시 만든다

실행: python automation/diag_earnings_preview_backtest.py

═══════════════════════════════════════════════════════════════════════════
판정 — 2026-09-05 실행 결과: **불합격. 이 신호는 닫혔다.**
═══════════════════════════════════════════════════════════════════════════
사전 약정은 위 `3) 임계 설정 가능성 판정` 이다. 실행 **전에** 이 파일에 적혀
있던 기준이고, "단조가 아니면 어떤 임계도 근거가 없고, 그 자체가 결론이다."
라고 못박아 뒀다. 결과를 보고 만든 기준이 아니다.

  표본 1,537건 · 83종목 · 2021-10-20 ~ 2026-08-06
  기준선(무조건상승) 48.9%
    0/5 n= 58  50.0% (+1.1%p)   3/5 n=512  49.4% (+0.5%p)
    1/5 n=174  47.1% (-1.8%p)   4/5 n=355  51.0% (+2.1%p)
    2/5 n=336  48.8% (-0.1%p)   5/5 n=102  41.2% (-7.7%p)  ← 최고점이 최악
  단조 증가: 아니오  →  [X] 어떤 임계도 정당화되지 않는다

  ⚠️ 여섯 버킷 **전부** 노이즈 범위다(|z| < 1.96). 5/5 의 -7.7%p 도 z=-1.56 로,
     "역방향 신호" 라고 주장할 근거조차 없다. 그냥 예측력이 없다.

  ⚠️ 격자의 '*' 를 근거로 되살리지 말 것. 35칸 × 2표 = 70회 검정이고,
     유의수준 0.05 로 70회면 **우연히 최소 한 칸이 유의할 확률이 97.2%** 다.
     '*' 가 보이는 건 기대되는 일이지 발견이 아니다. 고득점 표의 57.8%(n=102)
     는 95% 구간이 [48.1%, 67.5%] 로 하단이 50%를 못 넘는다.

F1(EPS 리비전)이 빠진 상태의 결과이지만, 그걸 이유로 판정을 무르지 않는다.
한 번 무르면 다음엔 다른 핑계가 생긴다. 대신 **F1 을 못 쌓던 원인을 고쳤다**:
  earnings_core.calendar_row 가 분기 전환 때 Est_History_JSON 을 버렸고
  아카이브가 없었다. "오늘부터 스냅샷을 쌓는다" 는 주석은 의도였을 뿐
  구현이 그걸 못 했다 — 6개월을 기다려도 매 분기 초기화된 버퍼만 남았다.
  → Earnings_Est_Archive 시트 신설(2026-09-05). 분기 전환 직전에 건져낸다.

재평가하려면 **먼저 새 사전 약정을 걸어야 한다.** 데이터가 쌓였다는 사실만으로
다시 여는 것은 이 판정을 무의미하게 만든다. 아카이브는 오늘 이후 전환분만
잡으므로 재평가 가능 시점은 2~3 실적 시즌 뒤(약 6~9개월)다.

이 신호는 연구 전용이었다 — F1~F5 요인은 이 파일 밖에 존재하지 않는다.
따라서 종료에 프로덕션 코드 변경이 없다(업종 모멘텀 종료와 동일).
═══════════════════════════════════════════════════════════════════════════
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import earnings_core as ec   # noqa: E402
import fmp_extras as fx      # noqa: E402  — 창 환산 SSOT

FMP_API_KEY = os.environ["FMP_API_KEY"]
_FMP_BASE = "https://financialmodelingprep.com/stable"
_TIMEOUT = 20

SURPRISE_LOOKBACK = 8
RS_WINDOW = 20
COST_BPS = 5.0
MIN_EVENTS = 40
# ⚠️ 이름이 예전엔 `HIST_LIMIT = 1400` 이었다(2026-09-04 개명·수정).
#    두 가지가 틀려 있었다:
#      ① FMP `historical-price-eod` 는 **`limit=` 을 조용히 무시한다.**
#         요청과 무관하게 항상 ~1,254봉이 온다. 러너·코어는 이미 from/to 창으로
#         옮겼는데 이 진단만 남아 있었다.
#      ② 1400 은 **FMP 상한(~1,254봉 ≈ 5.0년)을 넘는 값**이다. limit 이 먹는다
#         쳐도 못 받는 숫자였다. 검증된 요구치가 아니라 limit 이 작동하는 줄
#         알고 적은 숫자다.
#    개명은 락스텝 강제 장치다 — 옛 이름을 쓰는 호출부가 남아 있으면 즉시 터진다
#    (`HISTORY_LIMIT → HISTORY_BARS` 때와 같은 이유).
#
# 요구치는 코드에서 유도한다. 이벤트 1건당:
#    가장 이른 진입 D-11(ENTRIES) 11봉 + RS_WINDOW 20봉 + ma50 rolling 50봉
#    + 가장 늦은 청산 max(D+20, SIGNAL_MAX) 20봉 = **101봉**
# 종목당 이벤트는 분기 실적이라 63봉 간격. 상한 1,254봉이면 약 18건이다.
# 즉 **상한이 곧 요구치**다 — 더 받을 수 있으면 이벤트가 더 나온다.
HIST_BARS = 1250          # 상한 부근. hist_days_for_bars 가 HIST_MAX_DAYS 로 포화한다.
EVENT_MIN_BARS = 101      # 위 유도값. 미달 종목은 이벤트가 0건이다.
SLEEP_SEC = 0.35
SIGNAL_MAX = 20

ENTRIES = [("D-10", -11), ("D-5", -6), ("D-2", -3),
           ("발표당일종가", -1), ("반응일종가", 0)]
EXITS = [("D-1갭회피", -1), ("D+1", 1), ("D+3", 3), ("D+5", 5),
         ("D+10", 10), ("D+20", 20), ("매도신호", "signal")]


def _get(path):
    try:
        sep = "&" if "?" in path else "?"
        r = requests.get(f"{_FMP_BASE}/{path}{sep}apikey={FMP_API_KEY}", timeout=_TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def price_history(ticker, *, bars=HIST_BARS):
    """가격 이력. ⚠️ `limit=` 이 아니라 **from/to 창**으로 받는다.

    창 환산은 `fmp_extras` 가 SSOT 다 — 0.6871 비율도 여유 마진도 상한도
    전부 거기 있다. 여기서 달력일을 직접 계산하면 정책이 두 벌이 되고
    한쪽만 갱신된다. 그 실패는 로그를 남기지 않는다.
    """
    d = _get(f"historical-price-eod/full?symbol={ticker}"
             f"{fx.hist_range_params(fx.hist_days_for_bars(bars))}")
    rows = d.get("historical", d) if isinstance(d, dict) else d
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "date" not in df.columns or "close" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    out = pd.DataFrame(index=df.index)
    for a, b in (("open", "Open"), ("high", "High"), ("low", "Low"),
                 ("close", "Close"), ("volume", "Volume")):
        if a in df.columns:
            out[b] = pd.to_numeric(df[a], errors="coerce")
    return out.dropna(subset=["Close"])


def earnings_records(ticker):
    rows, seen = [], set()
    # earnings-surprises 는 stable 에 개별 심볼용이 없다(404 실측 확인). 제거.
    for path in (f"earnings?symbol={ticker}&limit=60",):
        for it in (_get(path) or []):
            if not isinstance(it, dict):
                continue
            d = ec._d(it.get("date") or it.get("fiscalDateEnding"))
            if d is None:
                continue
            ds = d.strftime("%Y-%m-%d")
            if ds in seen:
                continue
            act = ec._num(it.get("epsActual") or it.get("actualEarningResult") or it.get("eps"))
            est = ec._num(it.get("epsEstimated") or it.get("estimatedEarning"))
            if act is None or est is None or abs(est) < 1e-9:
                continue
            seen.add(ds)
            rows.append({"date": ds, "timing": ec._timing_of(it),
                         "surprise_pct": (act - est) / abs(est) * 100.0,
                         "beat": bool(act > est)})
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows


# ── 애널리스트 의견 (F4/F5) ──────────────────────────────────────────────
# ⚠️ grades-historical 은 '등급 변경 내역'이 아니라 **월별 의견 분포 스냅샷**이다.
#    실제 스키마: {symbol, date, analystRatingsStrongBuy, analystRatingsBuy,
#                  analystRatingsHold, analystRatingsSell, analystRatingsStrongSell}
#    previousGrade/newGrade/action 필드는 존재하지 않는다 — v1/v2 초안이 이걸
#    가정해서 1209건 전부 F4=0 이 나왔다.
#    → 매수의견 비율 = (StrongBuy + Buy) / 전체 로 환산해 두 가지 요인을 만든다.
#         F4_drift : 최근 GRADE_DRIFT_DAYS 간 매수의견 비율 **변화**(%p) — 흐름
#         F5_level : 진입 시점의 매수의견 비율 **수준**(%)          — 절대 강도

GRADE_DRIFT_DAYS = 90     # 월별 데이터라 60일이면 관측점이 1~2개뿐
_SCHEMA_SHOWN = [False]


def grade_series(ticker, show_schema=False):
    """[(date, buy_pct)] 오름차순. 매수의견 비율 = (StrongBuy+Buy)/전체 × 100."""
    raw = _get(f"grades-historical?symbol={ticker}&limit=1000") or []
    if show_schema and raw and not _SCHEMA_SHOWN[0] and isinstance(raw[0], dict):
        _SCHEMA_SHOWN[0] = True
        print(f"\n  [진단] grades-historical 원본 스키마 ({ticker})")
        print(f"         키: {sorted(raw[0].keys())}")
        print(f"         샘플: {json.dumps(raw[0], ensure_ascii=False)[:220]}\n")

    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        d = ec._d(it.get("date") or it.get("publishedDate"))
        if d is None:
            continue
        sb = ec._num(it.get("analystRatingsStrongBuy")) or 0.0
        b = ec._num(it.get("analystRatingsBuy")) or 0.0
        h = ec._num(it.get("analystRatingsHold")) or 0.0
        sl = ec._num(it.get("analystRatingsSell")) or 0.0
        ss = ec._num(it.get("analystRatingsStrongSell")) or 0.0
        tot = sb + b + h + sl + ss
        if tot <= 0:
            continue
        out.append((d, (sb + b) / tot * 100.0))
    out.sort(key=lambda x: x[0])
    return out


def grade_factors(series, base_dt, drift_days=GRADE_DRIFT_DAYS):
    """(F4_drift, F5_level). look-ahead 차단: base_dt 이전 관측만 사용."""
    if not series:
        return None, None
    past = [(d, v) for d, v in series if d <= base_dt]
    if not past:
        return None, None
    level = past[-1][1]
    cutoff = base_dt - pd.Timedelta(days=int(drift_days))
    older = [v for d, v in past if d <= cutoff]
    if not older:
        return None, level
    return level - older[-1], level


def load_tickers():
    env = str(os.environ.get("TICKERS", "") or "").strip()
    if env:
        return sorted({t.strip().upper() for t in env.split(",") if t.strip()})
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        info = json.loads(os.environ["GSPREAD_KEY"])
        gc = gspread.authorize(Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets",
                          "https://www.googleapis.com/auth/drive"]))
        sh = gc.open("Quant_DB")
        tks = set()
        for name, col in (("Watchlist", 1), ("Portfolios", 2)):
            try:
                for r in (sh.worksheet(name).get_all_values() or [])[1:]:
                    if len(r) > col and str(r[col]).strip():
                        tks.add(str(r[col]).strip().upper())
            except Exception as e:
                print(f"[WARN] {name} 로드 실패: {e}")
        return sorted(tks)
    except Exception as e:
        print(f"[WARN] 시트 접근 실패 — TICKERS 환경변수 사용: {e}")
        return []


def build_events(ticker, hist, spy, recs, gseries):
    if hist is None or hist.empty or not recs:
        return []
    idx = hist.index
    ma50 = hist["Close"].rolling(50).mean()
    evs = []
    recs_sorted = sorted(recs, key=lambda x: x["date"])
    back = max(abs(o) for _, o in ENTRIES)
    fwd = max(o for _, o in EXITS if isinstance(o, int))

    for k, rec in enumerate(recs_sorted):
        try:
            i = ec.resolve_reaction_index(hist, rec["date"], rec.get("timing", ""))
            if i is None or i - back - RS_WINDOW < 1 or i + fwd >= len(idx):
                continue

            base_i = i - 11                 # 요인 산출 기준 = 가장 이른 진입 시점
            base_dt = idx[base_i]

            prior = [r for r in recs_sorted[:k]
                     if r["date"] < base_dt.strftime("%Y-%m-%d")][-SURPRISE_LOOKBACK:]
            if len(prior) < 4:
                continue
            f2_beat = sum(1 for r in prior if r["beat"]) / len(prior) * 100.0
            f2_surp = float(np.mean([r["surprise_pct"] for r in prior]))

            c0 = float(hist["Close"].iloc[base_i - RS_WINDOW])
            c1 = float(hist["Close"].iloc[base_i])
            f3 = (c1 - c0) / c0 * 100.0
            if spy is not None and not spy.empty:
                try:
                    ss = spy.loc[:base_dt]
                    if len(ss) > RS_WINDOW:
                        s0, s1 = float(ss.iloc[-RS_WINDOW - 1]), float(ss.iloc[-1])
                        f3 -= (s1 - s0) / s0 * 100.0
                except Exception:
                    pass

            f4, f5 = grade_factors(gseries, base_dt)

            pc = float(hist["Close"].iloc[i - 1])
            gap = (float(hist["Close"].iloc[i]) - pc) / pc * 100.0

            e = {"ticker": ticker, "date": rec["date"], "F2_beat": f2_beat,
                 "F2_surp": f2_surp, "F3_rs": f3,
                 "F4_drift": f4, "F5_level": f5, "gap": gap}

            cost = COST_BPS / 100.0 * 2
            for en, eo in ENTRIES:
                ei = i + eo
                if ei < 1:
                    continue
                px_in = float(hist["Close"].iloc[ei])
                for xn, xo in EXITS:
                    if xo == "signal":
                        xi = None
                        for j in range(ei + 1, min(ei + SIGNAL_MAX + 1, len(idx))):
                            m = ma50.iloc[j]
                            if pd.notna(m) and float(hist["Close"].iloc[j]) < float(m):
                                xi = j
                                break
                        if xi is None:
                            xi = min(ei + SIGNAL_MAX, len(idx) - 1)
                    else:
                        xi = i + xo
                    if xi <= ei or xi >= len(idx):
                        continue
                    r = (float(hist["Close"].iloc[xi]) - px_in) / px_in * 100.0 - cost
                    sr = None
                    if spy is not None and not spy.empty:
                        try:
                            sb = spy.loc[idx[ei]:idx[xi]]
                            if len(sb) >= 2:
                                sr = ((float(sb.iloc[-1]) - float(sb.iloc[0]))
                                      / float(sb.iloc[0]) * 100.0)
                        except Exception:
                            pass
                    e[f"exc::{en}::{xn}"] = None if sr is None else r - sr
            evs.append(e)
        except Exception:
            continue
    return evs


def _stat(vals):
    v = [x for x in vals if x is not None and np.isfinite(x)]
    if not v:
        return None
    a = np.array(v, float)
    return {"n": len(a), "mean": float(a.mean()), "median": float(np.median(a)),
            "win": float((a > 0).mean() * 100.0)}


FACTORS = [
    ("F2_beat", 60.0, "서프라이즈 지속성(beat율)"),
    ("F2_surp", 2.0, "평균 서프라이즈 폭"),
    ("F3_rs", 0.0, "발표 전 상대강도"),
    ("F4_drift", 0.0, "매수의견 비율 상승(90일)"),
    ("F5_level", 60.0, "매수의견 비율 수준"),
]


def report(evs):
    for e in evs:
        e["_score"] = sum(1 for k, t, _ in FACTORS
                          if e.get(k) is not None and e[k] > t)
    n = len(evs)
    print("\n" + "=" * 78)
    print(f"표본 {n}건 · 종목 {len({e['ticker'] for e in evs})}개 · "
          f"{min(e['date'] for e in evs)} ~ {max(e['date'] for e in evs)}")
    print("=" * 78)
    if n < MIN_EVENTS:
        print(f"[!] 표본 {n} < {MIN_EVENTS} — 결론 보류")

    g4 = sum(1 for e in evs if e.get("F4_drift") is not None)
    g5 = sum(1 for e in evs if e.get("F5_level") is not None)
    print(f"\n[진단] 매수의견 산출 가능 — 변화(F4) {g4}/{n}건 · 수준(F5) {g5}/{n}건")
    if g4 < 50 or g5 < 50:
        print("       → 해당 요인은 표본 부족으로 검증 불가")
    else:
        d4 = _stat([e["F4_drift"] for e in evs])
        d5 = _stat([e["F5_level"] for e in evs])
        print(f"       F4 변화폭 분포: 중앙 {d4['median']:+.1f}%p · 평균 {d4['mean']:+.1f}%p")
        print(f"       F5 수준 분포  : 중앙 {d5['median']:.1f}% · 평균 {d5['mean']:.1f}%")

    print("\n■ 방향 적중률 — 스코어별 실제 '갭 상승' 비율")
    up_all = sum(1 for e in evs if (e["gap"] or 0) > 0) / n * 100.0
    print(f"  기준선(무조건상승 대조군) {up_all:.1f}%  ← 이걸 넘어야 예측력이 있다")
    rows = []
    for sc in range(len(FACTORS) + 1):
        sub = [e for e in evs if e["_score"] == sc]
        if len(sub) < 20:
            continue
        up = sum(1 for e in sub if (e["gap"] or 0) > 0) / len(sub) * 100.0
        g = _stat([e["gap"] for e in sub])
        rows.append((sc, len(sub), up))
        print(f"  스코어 {sc}/{len(FACTORS)}  n={len(sub):<5} 상승비율 {up:5.1f}% "
              f"({up - up_all:+5.1f}%p) · 갭 평균 {g['mean']:+5.2f}% "
              f"중앙 {g['median']:+5.2f}%")

    print("\n■ 임계 설정 가능성 — 'N점 이상 매수' 규칙이 정당한가")
    if len(rows) < 3:
        print("  버킷 부족 — 판단 보류")
        hi_sc = None
    else:
        ups = [r[2] for r in rows]
        mono = all(ups[j] <= ups[j + 1] for j in range(len(ups) - 1))
        best = max(rows, key=lambda r: r[2])
        # 격자용 '고득점' 그룹은 **최고 점수 버킷**이다(상승비율 1위 버킷이 아님).
        # 0점이 상승비율 1위로 뽑히면 고득점 그룹이 전체와 같아져 비교가 무의미해진다.
        hi_sc = max(r[0] for r in rows)
        print(f"  상승비율 단조 증가: {'예' if mono else '아니오'}"
              f"  ({' → '.join(f'{u:.0f}%' for u in ups)})")
        print(f"  최고 버킷: {best[0]}점  상승비율 {best[2]:.1f}%  (기준선 {up_all:.1f}%)")
        if mono and best[2] > up_all:
            print(f"  → [OK] '{best[0]}점 이상 매수' 임계에 근거 있음")
        elif not mono:
            print("  → [X] 단조가 아니다 — 어떤 임계도 정당화되지 않는다")
        else:
            print("  → [X] 최고 버킷도 기준선을 못 넘는다")

    mid = sorted(e["date"] for e in evs)[n // 2]
    groups = [(evs, "전체 이벤트")]
    if hi_sc is not None:
        hi = [e for e in evs if e["_score"] >= hi_sc]
        if hi:
            groups.append((hi, f"고득점({hi_sc}점 이상)"))

    for group, glabel in groups:
        print(f"\n■ 진입x청산 격자 — 초과수익(-SPY) 중앙값 / 승률   "
              f"[{glabel}] n={len(group)}")
        print(" " * 14 + "".join(f"{xn:>14}" for xn, _ in EXITS))
        for en, _ in ENTRIES:
            cells = []
            for xn, _ in EXITS:
                key = f"exc::{en}::{xn}"
                s = _stat([e.get(key) for e in group])
                if s is None or s["n"] < 20:
                    cells.append("—")
                    continue
                a = _stat([e.get(key) for e in group if e["date"] < mid])
                b = _stat([e.get(key) for e in group if e["date"] >= mid])
                ok = (a and b and np.sign(a["median"]) == np.sign(b["median"])
                      and s["median"] > 0)
                cells.append(f"{s['median']:+5.2f}/{s['win']:4.1f}{'*' if ok else ''}")
            print(f"  {en:<12}" + "".join(f"{c:>14}" for c in cells))
        print("   * = 중앙값>0 이고 half-split 부호 일치")

    print("\n" + "=" * 78)
    print("해석 주의")
    print("  - 모든 수치는 초과수익(-SPY). 원수익은 강세장 베타가 섞인다.")
    print("  - 격자 35칸은 다중검정이다. '*' 하나로 결론 내지 말 것.")
    print("  - '반응일종가' 진입은 갭을 보고 사는 관찰 전략이다. 이게 예측 진입보다")
    print("    나으면 방향 예측 요인은 쓸모가 없다는 뜻이다.")
    print("  - F1(EPS 리비전)은 없다 — FMP가 과거 시계열을 안 준다.")
    print("    run_earnings_watch 가 오늘부터 스냅샷을 쌓는다.")
    print("=" * 78)


def main():
    print("=" * 78)
    print(f"실적 프리뷰 요인 백테스트 v2 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 78)
    tickers = load_tickers()
    if not tickers:
        print("[ERROR] 대상 티커 없음")
        return
    print(f"대상 {len(tickers)}종목\n")

    spy_h = price_history("SPY")
    spy = spy_h["Close"] if not spy_h.empty else None

    all_evs, skipped = [], []
    for tk in tickers:
        try:
            recs = earnings_records(tk)
            if not recs:
                skipped.append(tk)
                time.sleep(SLEEP_SEC)
                continue
            h = price_history(tk)
            if h.empty:
                skipped.append(tk)
                time.sleep(SLEEP_SEC)
                continue
            gseries = grade_series(tk, show_schema=True)
            evs = build_events(tk, h, spy, recs, gseries)
            all_evs += evs
            # ⚠️ 봉이 모자라면 **과거 끝쪽 이벤트가 조용히 탈락한다.**
            #    build_events 의 `i - back - RS_WINDOW < 1` 가 그냥 continue 하기
            #    때문에 로그에 아무 흔적이 없다. 예전엔 1,400봉을 요청해놓고
            #    1,254봉을 받으면서도 아무도 몰랐다. 눈에 보이게 만든다.
            _short = " ⚠️봉부족" if len(h) < EVENT_MIN_BARS else ""
            _lost = len(recs) - len(evs)
            print(f"  {tk:6} 봉 {len(h):>4}{_short} · 실적 {len(recs):>2} · "
                  f"의견 {len(gseries):>3} → 이벤트 {len(evs)}"
                  + (f" (실적 {_lost}건은 창 밖)" if _lost > 0 else ""))
        except Exception as e:
            print(f"  {tk:6} 실패: {e}")
        time.sleep(SLEEP_SEC)

    if skipped:
        print(f"\n  제외 {len(skipped)}종목(실적 없음/ETF): {', '.join(skipped)}")
    if not all_evs:
        print("\n[ERROR] 유효 이벤트 0건")
        return
    report(all_evs)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
