# -*- coding: utf-8 -*-
"""
diag_earnings_preview_backtest.py
─────────────────────────────────
실적 발표 **전** 진입 전략의 요인 유효성 검증. workflow_dispatch 전용 진단 스크립트.

검증 대상 요인 (3B — ⑤ 과거 갭 방향 편향은 과적합 위험으로 제외)
  F2 서프라이즈 지속성 : 직전 8분기 beat 비율 · 평균 서프라이즈%
  F3 발표 전 상대강도  : D-20~D-1 수익률 − SPY 동일 기간
  F4 등급 변경 흐름    : D-30~D-1 상향 건수 − 하향 건수
  (F1 EPS 추정치 리비전은 FMP 가 과거 시계열을 주지 않아 검증 불가.
   run_earnings_watch 가 오늘부터 Earnings_Calendar 에 스냅샷을 쌓는다.)

전략 2종 (2C)
  A  D-10 종가 매수 → **D-1 종가 매도**        갭을 아예 회피
  B  D-10 종가 매수 → 갭 통과 → 청산 신호/D+20  서프라이즈를 노림

비교군
  · SPY 동일 보유기간
  · 무조건 진입(요인 필터 없음)  ← 요인이 실제로 걸러내는지 확인
  · 매수후보유(전 기간)

검증 규율 (위성 백테스트 교훈)
  · look-ahead 차단: 모든 요인은 **진입 시점 이전 데이터만** 사용
  · half-split: 시간 기준 전반/후반. **부호가 양쪽에서 일치하는 요인만 채택**
  · 거래비용 반영(진입·청산 각 5bp)

실행: python automation/diag_earnings_preview_backtest.py
"""

import json
import os
import sys
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

# ── 파라미터 ──────────────────────────────────────────────────────────────
ENTRY_OFFSET = 11        # 반응일 기준 몇 세션 전에 진입할지 (= 대략 D-10)
EXIT_A_OFFSET = 1        # 갭 직전 마지막 세션 (반응일 −1)
HOLD_B_MAX = 20          # 전략 B 최대 보유 세션
SURPRISE_LOOKBACK = 8    # F2 산출 분기 수
RS_WINDOW = 20           # F3 창(세션)
GRADE_WINDOW = 30        # F4 창(일)
COST_BPS = 5.0           # 편도 거래비용 (bp)
MIN_EVENTS = 40          # 이 미만이면 결론 보류
HIST_LIMIT = 1400        # 약 5.5년 — FMP 상한 근처

_GSPREAD = None
_SPREADSHEET_TITLE = "Quant_DB"


# ── FMP ───────────────────────────────────────────────────────────────────
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
    """[{date, timing, surprise_pct, beat}] 최신순. look-ahead 없이 쓰려면 날짜로 필터."""
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
            sp = None
            if act is not None and est is not None and abs(est) > 1e-9:
                sp = (act - est) / abs(est) * 100.0
            if sp is None:
                continue
            seen.add(ds)
            rows.append({"date": ds, "timing": ec._timing_of(it),
                         "surprise_pct": sp, "beat": bool(sp > 0)})
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows


def grade_changes(ticker):
    """[{date, action}] — action: up/down/other."""
    out = []
    for it in (_get(f"grades-historical?symbol={ticker}&limit=1000") or []):
        if not isinstance(it, dict):
            continue
        d = ec._d(it.get("date"))
        if d is None:
            continue
        a = str(it.get("action") or "").strip().lower()
        prev, new = str(it.get("previousGrade") or ""), str(it.get("newGrade") or "")
        if "up" in a:
            act = "up"
        elif "down" in a:
            act = "down"
        else:
            act = _grade_dir(prev, new)
        out.append({"date": d, "action": act})
    return out


_GRADE_RANK = {"strong sell": 0, "sell": 1, "underweight": 1, "underperform": 1,
               "hold": 2, "neutral": 2, "market perform": 2, "equal weight": 2,
               "buy": 3, "overweight": 3, "outperform": 3, "accumulate": 3,
               "strong buy": 4}


def _grade_dir(prev, new):
    a, b = _GRADE_RANK.get(str(prev).strip().lower()), _GRADE_RANK.get(str(new).strip().lower())
    if a is None or b is None or a == b:
        return "other"
    return "up" if b > a else "down"


# ── 대상 티커 ─────────────────────────────────────────────────────────────
def load_tickers():
    """워치리스트 + 보유 종목. 시트 접근 실패 시 환경변수 TICKERS 폴백."""
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
        sh = gc.open(_SPREADSHEET_TITLE)
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
        print(f"[WARN] 시트 접근 실패 — TICKERS 환경변수를 쓰세요: {e}")
        return []


# ── 이벤트 생성 ───────────────────────────────────────────────────────────
def build_events(ticker, hist, spy, recs, grades):
    """이벤트별 요인 + 성과. 요인은 **진입 시점 이전 데이터만** 사용."""
    if hist is None or hist.empty or not recs:
        return []
    idx = hist.index
    evs = []
    recs_sorted = sorted(recs, key=lambda x: x["date"])   # 과거→최신

    for k, rec in enumerate(recs_sorted):
        try:
            i = ec.resolve_reaction_index(hist, rec["date"], rec.get("timing", ""))
            if i is None:
                continue
            e_i = i - ENTRY_OFFSET          # 진입
            x_i = i - EXIT_A_OFFSET         # 전략 A 청산 (갭 직전 마지막 종가)
            if e_i < RS_WINDOW + 1 or x_i <= e_i:
                continue
            entry_dt = idx[e_i]

            # ── 요인 (전부 entry 이전 정보) ──
            prior = [r for r in recs_sorted[:k] if r["date"] < entry_dt.strftime("%Y-%m-%d")]
            prior = prior[-SURPRISE_LOOKBACK:]
            if len(prior) < 4:
                continue
            beat_rate = sum(1 for r in prior if r["beat"]) / len(prior) * 100.0
            avg_surp = float(np.mean([r["surprise_pct"] for r in prior]))

            c0, c1 = float(hist["Close"].iloc[e_i - RS_WINDOW]), float(hist["Close"].iloc[e_i])
            stock_r = (c1 - c0) / c0 * 100.0
            rs = stock_r
            if spy is not None and not spy.empty:
                try:
                    s_slice = spy.loc[:entry_dt]
                    if len(s_slice) > RS_WINDOW:
                        s0 = float(s_slice.iloc[-RS_WINDOW - 1]); s1 = float(s_slice.iloc[-1])
                        rs = stock_r - (s1 - s0) / s0 * 100.0
                except Exception:
                    pass

            lo = entry_dt - pd.Timedelta(days=GRADE_WINDOW)
            ups = sum(1 for g in grades if lo <= g["date"] <= entry_dt and g["action"] == "up")
            dns = sum(1 for g in grades if lo <= g["date"] <= entry_dt and g["action"] == "down")

            # ── 성과 ──
            px_in = float(hist["Close"].iloc[e_i])
            cost = COST_BPS / 100.0 * 2
            ret_a = (float(hist["Close"].iloc[x_i]) - px_in) / px_in * 100.0 - cost

            ret_b, exit_kind = None, ""
            if i + HOLD_B_MAX < len(idx):
                ma50 = hist["Close"].rolling(50).mean()
                ret_b, exit_kind = None, "hold"
                for j in range(i, min(i + HOLD_B_MAX + 1, len(idx))):
                    c = float(hist["Close"].iloc[j])
                    m = ma50.iloc[j]
                    if pd.notna(m) and c < float(m):
                        ret_b, exit_kind = (c - px_in) / px_in * 100.0 - cost, "signal"
                        break
                if ret_b is None:
                    c = float(hist["Close"].iloc[i + HOLD_B_MAX])
                    ret_b, exit_kind = (c - px_in) / px_in * 100.0 - cost, "timeout"

            spy_a = None
            if spy is not None and not spy.empty:
                try:
                    ss = spy.loc[idx[e_i]:idx[x_i]]
                    if len(ss) >= 2:
                        spy_a = (float(ss.iloc[-1]) - float(ss.iloc[0])) / float(ss.iloc[0]) * 100.0
                except Exception:
                    pass

            gap = None
            if i >= 1:
                pc = float(hist["Close"].iloc[i - 1])
                gap = (float(hist["Close"].iloc[i]) - pc) / pc * 100.0

            evs.append({
                "ticker": ticker, "date": rec["date"], "entry": entry_dt.strftime("%Y-%m-%d"),
                "F2_beat": beat_rate, "F2_surp": avg_surp, "F3_rs": rs,
                "F4_net": ups - dns,
                "ret_a": ret_a, "ret_b": ret_b, "spy_a": spy_a,
                "gap": gap, "exit_kind": exit_kind,
            })
        except Exception:
            continue
    return evs


# ── 분석 ──────────────────────────────────────────────────────────────────
def _stat(vals):
    v = [x for x in vals if x is not None and np.isfinite(x)]
    if not v:
        return None
    a = np.array(v, dtype=float)
    return {"n": len(a), "mean": float(a.mean()), "median": float(np.median(a)),
            "win": float((a > 0).mean() * 100.0), "std": float(a.std(ddof=1)) if len(a) > 1 else 0.0}


def _fmt(s):
    return "—" if s is None else (f"n={s['n']:<4} 평균 {s['mean']:+6.2f}% "
                                  f"중앙 {s['median']:+6.2f}% 승률 {s['win']:5.1f}%")


def factor_split(evs, key, thresh, ret_key):
    """요인 상위/하위 그룹 성과 차이. 한쪽이 비면 None."""
    hi = [e[ret_key] for e in evs if e.get(key) is not None and e[key] > thresh]
    lo = [e[ret_key] for e in evs if e.get(key) is not None and e[key] <= thresh]
    sh, sl = _stat(hi), _stat(lo)
    if sh is None or sl is None or sh["n"] < 5 or sl["n"] < 5:
        return None
    return {"hi": sh, "lo": sl, "edge": sh["mean"] - sl["mean"]}


def median_thresh(evs, key):
    v = [e[key] for e in evs if e.get(key) is not None and np.isfinite(e[key])]
    return float(np.median(v)) if v else None


FACTORS = [
    ("F2_beat", 60.0, "서프라이즈 지속성 (직전 beat율)"),
    ("F2_surp", 2.0, "평균 서프라이즈 폭"),
    ("F3_rs", 0.0, "발표 전 상대강도 (SPY 대비)"),
    ("F4_net", 0.0, "등급 변경 순상향"),
]


def report(evs):
    print("\n" + "=" * 74)
    print(f"표본 {len(evs)}건 · 종목 {len({e['ticker'] for e in evs})}개 · "
          f"{min(e['date'] for e in evs)} ~ {max(e['date'] for e in evs)}")
    print("=" * 74)
    if len(evs) < MIN_EVENTS:
        print(f"⚠️ 표본 {len(evs)}건 < 최소 {MIN_EVENTS}건 — 결론 보류")

    print("\n■ 비교군 (전략 A: D-10 매수 → D-1 매도)")
    print(f"  무조건 진입      {_fmt(_stat([e['ret_a'] for e in evs]))}")
    print(f"  SPY 동일기간     {_fmt(_stat([e['spy_a'] for e in evs]))}")
    ex = _stat([e['ret_a'] - e['spy_a'] for e in evs if e['spy_a'] is not None])
    print(f"  초과수익(A−SPY)  {_fmt(ex)}")

    print("\n■ 비교군 (전략 B: D-10 매수 → 갭 통과 → 50MA 이탈/D+20)")
    print(f"  무조건 진입      {_fmt(_stat([e['ret_b'] for e in evs]))}")
    kinds = {}
    for e in evs:
        kinds[e.get("exit_kind") or "-"] = kinds.get(e.get("exit_kind") or "-", 0) + 1
    print(f"  청산 사유        {kinds}")
    print(f"  실제 갭 분포     {_fmt(_stat([e['gap'] for e in evs]))}")

    mid = sorted(e["date"] for e in evs)[len(evs) // 2]
    h1 = [e for e in evs if e["date"] < mid]
    h2 = [e for e in evs if e["date"] >= mid]

    # 임계값 2종을 함께 본다:
    #   fixed  = 배포에 쓸 해석 가능한 절대 기준
    #   median = 표본을 반씩 가르는 상대 기준(한쪽이 비는 문제를 없앰)
    for ret_key, label in (("ret_a", "전략 A"), ("ret_b", "전략 B")):
        for mode in ("fixed", "median"):
            print(f"\n■ 요인별 성과 — {label} · {mode} 임계   (half-split ~{mid})")
            print(f"  {'요인':<28} {'임계':>7} {'전체':>9} {'전반':>8} {'후반':>8}  판정")
            for key, th0, name in FACTORS:
                th = th0 if mode == "fixed" else median_thresh(evs, key)
                if th is None:
                    print(f"  {name:<28} {'—':>7}"); continue
                full = factor_split(evs, key, th, ret_key)
                a = factor_split(h1, key, th, ret_key)
                b = factor_split(h2, key, th, ret_key)
                if full is None:
                    print(f"  {name:<28} {th:>7.1f} {'그룹부족':>9}"); continue
                ea = a["edge"] if a else None
                eb = b["edge"] if b else None
                ok = (ea is not None and eb is not None
                      and np.sign(ea) == np.sign(eb) and full["edge"] > 0)
                print(f"  {name:<28} {th:>7.1f} {full['edge']:>+8.2f}%p "
                      f"{('—' if ea is None else f'{ea:+7.2f}')} "
                      f"{('—' if eb is None else f'{eb:+7.2f}')}  "
                      f"{'✅ 채택' if ok else '❌ 기각'}")

    # ── 스코어 버킷 (C1 동일가중) ──
    # AND 조합은 요인이 늘수록 표본이 급감해(예: 4개 전부 만족 = 몇 건) 결론을
    # 낼 수 없다. 대신 '양의 요인 개수'로 버킷을 나눠 단조성을 본다.
    print("\n■ 스코어 버킷 — 양의 요인 개수별 성과 (C1 동일가중)")
    for ret_key, label in (("ret_a", "전략 A"), ("ret_b", "전략 B")):
        for e in evs:
            e["_score"] = sum(1 for k, t, _ in FACTORS
                              if e.get(k) is not None and e[k] > t)
        print(f"  {label}")
        rows = []
        for sc in range(len(FACTORS) + 1):
            st = _stat([e[ret_key] for e in evs if e["_score"] == sc])
            if st:
                rows.append((sc, st))
                print(f"    스코어 {sc}/{len(FACTORS)}  {_fmt(st)}")
        # ⚠️ 가중 회귀 기울기만 보면 중간 버킷에 끌려가 **거짓 양성**이 난다.
        #    (랜덤 데이터에서 0/4=-1.5%, 4/4=-2.3% 인데 기울기는 +로 나오는 사례 확인)
        #    → 세 조건을 모두 요구한다: 상단>하단 · 기울기>0 · half-split 부호 일치
        def _slope(sub):
            rs = []
            for sc in range(len(FACTORS) + 1):
                st2 = _stat([e[ret_key] for e in sub if e["_score"] == sc])
                if st2 and st2["n"] >= 5:
                    rs.append((sc, st2))
            if len(rs) < 3:
                return None, None, None
            xs = np.array([r[0] for r in rs], float)
            ys = np.array([r[1]["mean"] for r in rs], float)
            ws = np.array([r[1]["n"] for r in rs], float)
            return float(np.polyfit(xs, ys, 1, w=ws)[0]), rs[0][1], rs[-1][1]

        sl, bot, top = _slope(evs)
        if sl is None:
            print("    → 버킷 부족 — 판단 보류"); continue
        sl1, _, _ = _slope(h1)
        sl2, _, _ = _slope(h2)
        spread = top["mean"] - bot["mean"]
        half_ok = (sl1 is not None and sl2 is not None
                   and np.sign(sl1) == np.sign(sl2) == np.sign(sl))
        ok = (sl > 0 and spread > 0 and half_ok)
        print(f"    기울기 {sl:+.2f}%p/점 · 상단−하단 {spread:+.2f}%p "
              f"· half-split {('일치' if half_ok else '불일치')}"
              f" ({'—' if sl1 is None else f'{sl1:+.2f}'}/"
              f"{'—' if sl2 is None else f'{sl2:+.2f}'})")
        print(f"    → {'✅ 요인 조합에 근거 있음' if ok else '❌ 근거 없음 (세 조건 모두 필요)'}")


def main():
    print("=" * 74)
    print(f"실적 프리뷰 요인 백테스트 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 74)
    tickers = load_tickers()
    if not tickers:
        print("[ERROR] 대상 티커 없음"); return
    print(f"대상 {len(tickers)}종목: {', '.join(tickers)}")

    spy_h = price_history("SPY")
    spy = spy_h["Close"] if not spy_h.empty else None
    print(f"SPY 기준 {len(spy_h)}봉\n")

    all_evs = []
    for tk in tickers:
        try:
            h = price_history(tk)
            if h.empty:
                print(f"  {tk:6} 가격 이력 없음"); continue
            recs = earnings_records(tk)
            grades = grade_changes(tk)
            evs = build_events(tk, h, spy, recs, grades)
            all_evs += evs
            print(f"  {tk:6} 봉 {len(h):>4} · 실적 {len(recs):>2} · 등급 {len(grades):>3} "
                  f"→ 이벤트 {len(evs)}")
        except Exception as e:
            print(f"  {tk:6} 실패: {e}")

    if not all_evs:
        print("\n[ERROR] 유효 이벤트 0건"); return
    report(all_evs)
    print("\n" + "=" * 74)
    print("해석 주의")
    print("  1) 다중검정: 요인 4 × 임계 2 × 전략 2 = 16회 검정이다. 순수 랜덤에서도")
    print("     1~2개는 '채택'이 나온다. 단일 요인 채택은 근거로 부족하고,")
    print("     **스코어 버킷이 세 조건을 모두 통과할 때만** 의미를 둘 것.")
    print("  2) 전략 B 청산은 50MA 이탈 근사다(실제 매도 레이더의 스윙/포지션")
    print("     이중 신호와 다름). 방향성 판단용으로만 보라.")
    print("  3) F1(EPS 추정치 리비전)은 여기 없다 — FMP가 과거 시계열을 주지 않는다.")
    print("     run_earnings_watch 가 오늘부터 스냅샷을 쌓는다.")
    print("=" * 74)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
