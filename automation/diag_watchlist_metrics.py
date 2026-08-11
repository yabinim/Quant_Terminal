# -*- coding: utf-8 -*-
"""diag_watchlist_metrics.py — Watchlist_Metrics 파이프라인 회귀 테스트.

네트워크·시트·Streamlit 없이 합성 일봉으로 돈다.

검증 대상:
  R1. JSON 왕복 무결성 — analyze_ticker 출력이 시트를 거쳐도 값이 그대로인가
      (NaN 은 null 로 보내면 안 된다. 소비자가 pd.notna/float 로 다루므로
       null 이 되면 조용히 다른 분기를 타거나 터진다)
  R2. 행 길이 항상 NCOL — 칸 수가 어긋나면 옆 열로 밀려 드리프트
  R3. 신선도 판정 — 장중 미완성 봉 때문에 종일 폴백이 터지지 않는가
  R4. SSOT 일치 — 저장본에서 복원한 값 == 실시간 계산값
  R5. 방어 — 빈/짧은 일봉, 깨진 행, 빈 페이로드에서 죽지 않는가

마지막에 뮤테이션으로 각 가드가 실제로 동작하는지 확인한다.

실행: python diag_watchlist_metrics.py
"""
from __future__ import annotations

import importlib
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.normpath(os.path.join(_HERE, ".."))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import watchlist_metrics_core as wm  # noqa: E402


def mk_hist(n: int, seed: int = 1, end: str = "2026-08-11") -> pd.DataFrame:
    rs = np.random.RandomState(seed)
    idx = pd.bdate_range(end=end, periods=n)
    base = 100 + np.cumsum(rs.randn(n))
    return pd.DataFrame({"Open": base, "High": base + 1.5, "Low": base - 1.5,
                         "Close": base, "Volume": rs.randint(10**6, 5 * 10**6, n)},
                        index=idx)


def deep_eq(a, b, path=""):
    """NaN == NaN 으로 보는 재귀 비교. 불일치 경로를 문자열로 반환."""
    if isinstance(a, dict):
        if not isinstance(b, dict) or set(a) != set(b):
            return f"{path} 키 불일치"
        for k in a:
            r = deep_eq(a[k], b[k], f"{path}.{k}")
            if r:
                return r
        return None
    if isinstance(a, (list, tuple)):
        if not isinstance(b, (list, tuple)) or len(a) != len(b):
            return f"{path} 길이 불일치"
        for i, (x, y) in enumerate(zip(a, b)):
            r = deep_eq(x, y, f"{path}[{i}]")
            if r:
                return r
        return None
    if isinstance(a, float) and isinstance(b, float):
        if np.isnan(a) and np.isnan(b):
            return None
        return None if abs(a - b) < 1e-9 else f"{path} {a} != {b}"
    try:
        if pd.isna(a) and pd.isna(b):
            return None
    except (TypeError, ValueError):
        pass
    return None if a == b else f"{path} {a!r} != {b!r}"


def run_tests() -> list:
    fails = []

    def ck(cond, name, detail=""):
        if not cond:
            fails.append(f"{name}{(' — ' + detail) if detail else ''}")

    spy = mk_hist(300, 7)["Close"]

    # ── R1/R2/R4: 왕복 무결성 · 행 길이 · SSOT 일치 ─────────────────────────
    for n, label in [(300, "300봉"), (210, "210봉"), (150, "150봉(MA200결측)"),
                     (30, "30봉"), (5, "5봉")]:
        h = mk_hist(n, seed=n)
        m = wm.compute_metrics("TEST", h, spy_close=spy, updated_at="2026-08-11 17:00 ET")
        if m is None:
            ck(n <= 5, f"{label} compute_metrics None", "충분한 봉인데 None")
            continue
        row = wm.to_row(m)
        ck(len(row) == wm.NCOL, f"R2 {label} 행길이", f"{len(row)}칸")
        ck(all(isinstance(c, str) for c in row), f"R2 {label} 전 칸 문자열")
        back = wm.from_row(row)
        ck(back is not None, f"R1 {label} 복원")
        if back:
            ck(deep_eq(m["analysis"], back["analysis"]) is None,
               f"R1 {label} analysis 왕복", str(deep_eq(m["analysis"], back["analysis"])))
            for k in ("rsi", "ma200", "atr", "high_120", "last_close"):
                a, b = m[k], back[k]
                same = (np.isnan(a) and np.isnan(b)) if (isinstance(a, float) and isinstance(b, float)) else a == b
                ck(same or abs(float(a) - float(b)) < 1e-3, f"R4 {label} {k}", f"{a} vs {b}")
            ck(m["dec_key"] == back["dec_key"], f"R4 {label} dec_key")
            ck(m["trade_date"] == back["trade_date"], f"R4 {label} trade_date")

    # NaN 이 null 로 나가지 않는지 (150봉은 MA200 결측이 있다)
    _m150 = wm.compute_metrics("T", mk_hist(150, 3), spy_close=spy)
    _row150 = wm.to_row(_m150)
    ck("null" not in _row150[-1], "R1 NaN을 null로 직렬화하지 않음", _row150[-1][:80])
    _b150 = wm.from_row(_row150)
    ck(_b150 is not None, "R1 150봉 복원")

    # ── R3: 신선도 — 장중 미완성 봉 시나리오 ────────────────────────────────
    #   자동화가 8/10 마감 후 저장(trade_date=2026-08-10).
    #   8/11 장중 앱의 SPY 일봉에는 8/11 미완성 봉이 들어 있다.
    h_intraday = mk_hist(300, 7, end="2026-08-11")
    ref = wm.last_completed_session(h_intraday, "2026-08-11")
    ck(ref == "2026-08-10", "R3 기준일=직전 완료 세션", f"실제={ref}")
    ck(wm.is_fresh({"trade_date": "2026-08-10"}, ref), "R3 전일 마감 저장본은 신선")
    ck(wm.is_fresh({"trade_date": "2026-08-11"}, ref), "R3 당일 저장본도 신선(마감 후)")
    ck(not wm.is_fresh({"trade_date": "2026-08-07"}, ref), "R3 며칠 전 저장본은 낡음")
    ck(not wm.is_fresh({"trade_date": ""}, ref), "R3 날짜 없으면 낡음")
    ck(not wm.is_fresh(None, ref), "R3 metrics 없으면 낡음")
    ck(not wm.is_fresh({"trade_date": "2026-08-10"}, ""), "R3 기준일 모르면 낡음")
    # 주말/휴장: 오늘 봉이 아예 없으면 마지막 봉이 기준
    ref_wk = wm.last_completed_session(h_intraday, "2026-08-15")
    ck(ref_wk == "2026-08-11", "R3 휴장일 기준", f"실제={ref_wk}")

    # ── R5: 방어 ────────────────────────────────────────────────────────────
    ck(wm.compute_metrics("X", None) is None, "R5 hist=None")
    ck(wm.compute_metrics("X", pd.DataFrame()) is None, "R5 빈 DataFrame")
    ck(wm.to_row(None) == [""] * wm.NCOL, "R5 to_row(None)")
    ck(wm.from_row([]) is None, "R5 빈 행")
    ck(wm.from_row(["TSLA"] + [""] * (wm.NCOL - 1)) is None, "R5 페이로드 없는 행")
    ck(wm.from_row(["TSLA", "2026-08-10", "", "", "", "", "", "", "", "", "", "{깨진json"]) is None,
       "R5 깨진 JSON")
    ck(wm.from_row([""] * wm.NCOL) is None, "R5 티커 없는 행")
    ck(wm.last_completed_session(None, "2026-08-11") == "", "R5 last_completed_session(None)")
    # 구 스키마(짧은 행)도 죽지 않아야 한다
    ck(wm.from_row(["TSLA", "2026-08-10"]) is None, "R5 짧은 행")

    # 시트 라운드트립: to_row 결과가 시트를 거치면 전부 문자열이 된다
    _m = wm.compute_metrics("NVDA", mk_hist(300, 11), spy_close=spy)
    _sheet_row = [str(c) for c in wm.to_row(_m)]
    _back = wm.from_row(_sheet_row)
    ck(_back is not None and deep_eq(_m["analysis"], _back["analysis"]) is None,
       "R1 시트 문자열화 후에도 왕복")

    return fails


MUTATIONS = [
    ("NaN 을 null 로 직렬화",
     'return _NAN_TOKEN if (math.isnan(f) or math.isinf(f)) else f',
     "return None if (math.isnan(f) or math.isinf(f)) else f"),
    ("행 길이 정규화 제거",
     'return (row + [""] * NCOL)[:NCOL]',
     "return row + ['DRIFT']"),
    ("신선도를 등호 비교로 되돌림(장중 폴백 폭주 재발)",
     "return stored >= str(ref_trade_date).strip()",
     "return stored == str(ref_trade_date).strip()"),
    ("기준일을 '마지막 봉'으로 되돌림",
     "if not _t or d < _t:",
     "if True:"),
    ("JSON 역변환 제거(NaN 토큰이 문자열로 남음)",
     "if o == _NAN_TOKEN:\n        return np.nan",
     "if False:\n        return np.nan"),
]


def main():
    path = os.path.join(_HERE, "watchlist_metrics_core.py")
    if not os.path.isfile(path):
        path = os.path.normpath(os.path.join(_HERE, "..", "watchlist_metrics_core.py"))
    src = open(path, encoding="utf-8").read()

    print("=" * 74)
    print("1) 원본 검증")
    print("=" * 74)
    fails = run_tests()
    if fails:
        for f in fails:
            print(f"  [NG] {f}")
        print(f"\n>>> 원본에서 {len(fails)}건 실패 — 중단")
        return 1
    print("  [OK] 전 항목 통과")

    print()
    print("=" * 74)
    print("2) 뮤테이션 검증")
    print("=" * 74)
    weak = 0
    _bak = src
    try:
        for name, old, new in MUTATIONS:
            if src.count(old) != 1:
                print(f"  [SKIP] {name}: 앵커 {src.count(old)}회 — 뮤테이션 갱신 필요")
                weak += 1
                continue
            open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
            importlib.reload(wm)
            try:
                mf = run_tests()
            except Exception as e:
                mf = [f"예외: {type(e).__name__}"]
            if mf:
                print(f"  [OK]   {name}: {len(mf)}건 탐지 (예: {mf[0][:52]})")
            else:
                print(f"  [WEAK] {name}: 부쉈는데도 통과 — 테스트가 무력함")
                weak += 1
    finally:
        open(path, "w", encoding="utf-8").write(_bak)
        importlib.reload(wm)

    print()
    print("=" * 74)
    if weak:
        print(f">>> 결과: 뮤테이션 {weak}건 미탐지 — 보강 필요")
        return 1
    print(">>> 결과: 원본 통과 + 뮤테이션 전건 탐지.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
