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

FMP_API_KEY = os.environ["FMP_API_KEY"]
_FMP_BASE = "https://financialmodelingprep.com/stable"
_TIMEOUT = 20

SURPRISE_LOOKBACK = 8
RS_WINDOW = 20
GRADE_WINDOW = 60
COST_BPS = 5.0
MIN_EVENTS = 40
HIST_LIMIT = 1400
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


def price_history(ticker, limit=HIST_LIMIT):
    d = _get(f"historical-price-eod/full?symbol={ticker}&limit={limit}")
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
    for path in (f"earnings?symbol={ticker}&limit=60",
                 f"earnings-surprises?symbol={ticker}&limit=60"):
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


# ── 등급 변경 (F4) — v1 에서 전 이벤트 0 이었던 부분 ────────────────────
_GRADE_RANK = {
    "strong sell": 0, "sell": 1, "underweight": 1, "underperform": 1, "reduce": 1,
    "negative": 1, "sector underperform": 1, "market underperform": 1,
    "hold": 2, "neutral": 2, "market perform": 2, "equal weight": 2, "in-line": 2,
    "sector perform": 2, "peer perform": 2, "equal-weight": 2, "inline": 2,
    "buy": 3, "overweight": 3, "outperform": 3, "accumulate": 3, "positive": 3,
    "sector outperform": 3, "market outperform": 3, "add": 3, "over weight": 3,
    "strong buy": 4, "conviction buy": 4, "top pick": 4,
}
_UP_WORDS = ("upgrade", "raised", "initiat", "resumed", "positive")
_DOWN_WORDS = ("downgrade", "lowered", "cut", "negative")
_SCHEMA_SHOWN = [False]


def _grade_dir(prev, new):
    a = _GRADE_RANK.get(str(prev).strip().lower())
    b = _GRADE_RANK.get(str(new).strip().lower())
    if a is None or b is None or a == b:
        return "other"
    return "up" if b > a else "down"


def grade_changes(ticker, show_schema=False):
    """[{date, action}]. FMP 필드명이 판올림마다 달라 후보를 폭넓게 훑는다."""
    raw = _get(f"grades-historical?symbol={ticker}&limit=1000") or []
    if show_schema and raw and not _SCHEMA_SHOWN[0] and isinstance(raw[0], dict):
        _SCHEMA_SHOWN[0] = True
        print(f"\n  [진단] grades-historical 원본 스키마 ({ticker})")
        print(f"         키: {sorted(raw[0].keys())}")
        print(f"         샘플: {json.dumps(raw[0], ensure_ascii=False)[:280]}\n")

    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        d = ec._d(it.get("date") or it.get("publishedDate") or it.get("gradeDate"))
        if d is None:
            continue
        a = " ".join(str(it.get(k) or "") for k in
                     ("action", "ratingChange", "gradeChange", "newsTitle")).lower()
        prev = (it.get("previousGrade") or it.get("previousRating")
                or it.get("gradePrevious") or "")
        new = (it.get("newGrade") or it.get("newRating")
               or it.get("gradeNew") or it.get("grade") or "")
        if any(w in a for w in _UP_WORDS):
            act = "up"
        elif any(w in a for w in _DOWN_WORDS):
            act = "down"
        else:
            act = _grade_dir(prev, new)
        out.append({"date": d, "action": act})
    return out


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


def build_events(ticker, hist, spy, recs, grades):
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

            lo = base_dt - pd.Timedelta(days=GRADE_WINDOW)
            ups = sum(1 for g in grades if lo <= g["date"] <= base_dt and g["action"] == "up")
            dns = sum(1 for g in grades if lo <= g["date"] <= base_dt and g["action"] == "down")

            pc = float(hist["Close"].iloc[i - 1])
            gap = (float(hist["Close"].iloc[i]) - pc) / pc * 100.0

            e = {"ticker": ticker, "date": rec["date"], "F2_beat": f2_beat,
                 "F2_surp": f2_surp, "F3_rs": f3, "F4_net": ups - dns,
                 "gap": gap, "grade_any": ups + dns}

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
    ("F4_net", 0.0, "등급 순상향"),
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

    ga = sum(1 for e in evs if e.get("grade_any"))
    print(f"\n[진단] 등급 변경 창({GRADE_WINDOW}일) 안에 변동 있는 이벤트 {ga}/{n}건")
    if ga < 50:
        print("       → F4 는 표본 부족으로 검증 불가 (3요인 결과로 읽을 것)")

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
            grades = grade_changes(tk, show_schema=True)
            evs = build_events(tk, h, spy, recs, grades)
            all_evs += evs
            print(f"  {tk:6} 봉 {len(h):>4} · 실적 {len(recs):>2} · "
                  f"등급 {len(grades):>3} → 이벤트 {len(evs)}")
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
