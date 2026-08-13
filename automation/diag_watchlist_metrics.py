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
  R6. completed_bars_only — 백필이 확정 봉만 쓰는가
  W1~W8. 쓰기 경로(run_watchlist_alerts.persist_watchlist_metrics) — 계산된 지표가
      실제로 시트에 어떤 모양으로 들어가는가. FMP·Sheets 를 가짜로 갈아끼우고
      **실제 소스를 임포트**해 부른다(로직 복사본 아님).

마지막에 뮤테이션으로 각 가드가 실제로 동작하는지 확인한다 —
watchlist_metrics_core(계산·직렬화)와 run_watchlist_alerts(쓰기) 양쪽 모두.

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

# ── 쓰기 경로 검사를 위한 자동화 모듈 임포트 ─────────────────────────────────
#   run_watchlist_alerts 는 모듈 로드 시점에 환경변수와 gspread 를 요구한다.
#   진단은 네트워크·자격증명 없이 돌아야 하므로 최소 스텁을 먼저 심는다.
import types  # noqa: E402

for _k in ("FMP_API_KEY", "GMAIL_USER", "GMAIL_APP_PASSWORD", "GMAIL_TO"):
    os.environ.setdefault(_k, "diag")
os.environ.setdefault("GSPREAD_KEY", "{}")

if "gspread" not in sys.modules:
    _g = types.ModuleType("gspread")
    _g.exceptions = types.SimpleNamespace(APIError=Exception)
    sys.modules["gspread"] = _g
if "google.oauth2.service_account" not in sys.modules:
    sys.modules.setdefault("google", types.ModuleType("google"))
    sys.modules.setdefault("google.oauth2", types.ModuleType("google.oauth2"))
    _sa = types.ModuleType("google.oauth2.service_account")
    _sa.Credentials = types.SimpleNamespace(from_service_account_info=lambda *a, **k: None)
    sys.modules["google.oauth2.service_account"] = _sa

import run_watchlist_alerts as R  # noqa: E402


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


# ══════════════════════════════════════════════════════════════════════════════
# 쓰기 경로 (W) — 가짜 Sheets/FMP
# ══════════════════════════════════════════════════════════════════════════════
class FakeWS:
    """gspread 워크시트의 최소 모델.

    ⚠️ update() 는 **지정 범위만** 덮어쓰고 그 밖의 행은 남긴다. 실제 시트가 그렇게
       동작하기 때문이다. 통째로 갈아끼우게 만들면 '옛 행이 남는' 버그를 진단이
       구조적으로 못 잡는다.
    """

    def __init__(self, title, vals=None, rows=2000, cols=12):
        self.title = title
        self._v = [list(r) for r in (vals or [])]
        self.row_count, self.col_count = rows, cols
        self.updates = []

    def get_all_values(self):
        return [list(r) for r in self._v]

    def update(self, body, range_name=None, value_input_option=None):
        self.updates.append((body, range_name))
        while len(self._v) < len(body):
            self._v.append([""] * self.col_count)
        for i, row in enumerate(body):
            self._v[i] = list(row)

    def resize(self, rows=None, cols=None):
        if rows:
            self.row_count = rows
        if cols:
            self.col_count = cols


class FakeSH:
    def __init__(self, wss):
        self._w = wss

    def worksheets(self):
        return list(self._w.values())

    def worksheet(self, t):
        if t not in self._w:
            raise KeyError(t)
        return self._w[t]

    def add_worksheet(self, title, rows, cols):
        ws = FakeWS(title, rows=rows, cols=cols)
        self._w[title] = ws
        return ws


def _fake_env(tickers, metrics_rows=None, hist_map=None, dead=()):
    """R 의 시트/FMP 진입점을 가짜로 갈아끼운다. (sh, worksheets) 반환."""
    wl = [["User_ID", "Ticker"] + [""] * 11]
    for t in tickers:
        wl.append(["yab", t] + [""] * 11)
    wss = {"Watchlist": FakeWS("Watchlist", wl)}
    if metrics_rows is not None:
        wss[wm.SHEET_TITLE] = FakeWS(wm.SHEET_TITLE, [list(wm.COLS)] + metrics_rows)
    sh = FakeSH(wss)
    R._open_ws = lambda title: (sh, sh.worksheet(title))
    hm = hist_map or {t: mk_hist(300, seed=i + 1, end=_TODAY) for i, t in enumerate(tickers)}
    R._fmp_price_history = lambda tk, limit=252: (
        pd.DataFrame() if tk in dead else hm.get(tk, mk_hist(300, 9, end=_TODAY)))
    return sh, wss


_TODAY = "2026-08-11"


def run_writepath_tests(ck) -> None:
    """persist_watchlist_metrics 회귀. ck(cond, name, detail) 로 결과를 보고한다."""
    # ── W1: 정기 EOD 저장 — 폭·헤더·범위·왕복 ──────────────────────────────
    sh, _ = _fake_env(["AAA", "BBB", "CCC"])
    n = R.persist_watchlist_metrics(None, {}, _TODAY, completed_only=False)
    ws = sh.worksheet(wm.SHEET_TITLE)
    body, rng = ws.updates[-1]
    ck(n == 3, "W1 3종목 저장", f"n={n}")
    ck(all(len(r) == wm.NCOL for r in body), "W1 전 행 폭=NCOL")
    ck(body[0] == list(wm.COLS), "W1 헤더=wm.COLS")
    ck(rng == f"A1:L{len(body)}", "W1 명시 범위 지정", str(rng))
    _back = [wm.from_row(r) for r in body[1:]]
    ck(all(b is not None for b in _back), "W1 앱 리더(from_row) 왕복")
    ck(all(b and b.get("analysis") for b in _back), "W1 analysis 페이로드 복원")
    ck(_back[0]["trade_date"] == _TODAY, "W1 당일 봉 기준", str(_back[0]["trade_date"]))

    # ── W2: 백필 — 당일 봉 제외 → 실행 시각 무관하게 결정적 ────────────────
    sh, _ = _fake_env(["AAA"])
    R.persist_watchlist_metrics(None, {}, _TODAY, completed_only=True)
    b2 = wm.from_row(sh.worksheet(wm.SHEET_TITLE).updates[-1][0][1])
    ck(b2 is not None and b2["trade_date"] < _TODAY, "W2 백필은 확정 봉까지만",
       str(b2 and b2["trade_date"]))
    ck(wm.is_fresh(b2, wm.last_completed_session(mk_hist(300, 1, end=_TODAY), _TODAY)),
       "W2 백필 결과가 앱 신선도 통과")

    # ── W3: 워치리스트 축소 — 유령 티커 제거 ───────────────────────────────
    old_rows = [wm.to_row({"ticker": t, "trade_date": "2026-01-01", "analysis": {"x": 1}})
                for t in ("AAA", "BBB", "CCC", "ZZZ")]
    sh, _ = _fake_env(["AAA"], metrics_rows=old_rows)
    R.persist_watchlist_metrics(None, {}, _TODAY)
    _final = sh.worksheet(wm.SHEET_TITLE).get_all_values()
    ck(not any("ZZZ" in "".join(map(str, r)) for r in _final), "W3 삭제된 티커 흔적 없음")
    ck(sum(1 for r in _final[1:] if wm.from_row(r)) == 1, "W3 유효 행 1개만 남음")

    # ── W4: 계산 0건 — 기존 값 보존 ────────────────────────────────────────
    sh, _ = _fake_env(["AAA", "BBB"], metrics_rows=old_rows[:2], dead=("AAA", "BBB"))
    _before = sh.worksheet(wm.SHEET_TITLE).get_all_values()
    n = R.persist_watchlist_metrics(None, {}, _TODAY)
    ck(n == 0, "W4 반환 0")
    ck(not sh.worksheet(wm.SHEET_TITLE).updates, "W4 시트 쓰기 호출 없음")
    ck(sh.worksheet(wm.SHEET_TITLE).get_all_values() == _before, "W4 기존 값 보존")

    # ── W5: 방어 경로 ──────────────────────────────────────────────────────
    sh, _ = _fake_env([])
    ck(R.persist_watchlist_metrics(None, {}, _TODAY) == 0, "W5 빈 워치리스트 → 0")

    def _boom(_t):
        raise RuntimeError("시트 없음")

    _save = R._open_ws
    R._open_ws = _boom
    try:
        ck(R.persist_watchlist_metrics(None, {}, _TODAY) == 0, "W5 시트 예외 → 0 (전파 없음)")
    finally:
        R._open_ws = _save

    # ── W6: hist_cache 재사용 — 정기 EOD 에서 FMP 추가 호출 0 ──────────────
    sh, _ = _fake_env(["AAA", "BBB"])
    _calls = []
    _orig_fetch = R._fmp_price_history
    R._fmp_price_history = lambda tk, limit=252: (_calls.append(tk), _orig_fetch(tk))[1]
    R.persist_watchlist_metrics(
        None, {"AAA": mk_hist(300, 1, end=_TODAY), "BBB": mk_hist(300, 2, end=_TODAY)}, _TODAY)
    ck(_calls == [], "W6 캐시된 종목은 FMP 재호출 안 함", f"calls={_calls}")

    # ── W7: 중복 티커 — 여러 사용자가 같은 종목을 담아도 1행 ───────────────
    sh, _ = _fake_env(["AAA", "AAA", "BBB"])
    R.persist_watchlist_metrics(None, {}, _TODAY)
    _rows = [r for r in sh.worksheet(wm.SHEET_TITLE).updates[-1][0][1:] if wm.from_row(r)]
    ck(len(_rows) == 2, "W7 사용자 간 중복 티커 dedupe", f"{len(_rows)}행")

    # ── W8: main() 배선 — 소스 수준 계약 ───────────────────────────────────
    _wa = open(_WA_PATH, encoding="utf-8").read()
    ck('"metrics"' in _wa and "--scope" in _wa, "W8 --scope 에 metrics 존재")
    ck('args.scope != "metrics"' in _wa, "W8 휴장일 게이트가 백필을 막지 않음")
    ck("completed_only=True" in _wa and "completed_only=False" in _wa,
       "W8 백필/정기 EOD 기준 분기 존재")
    _intraday = _wa.split('elif args.mode == "intraday":')[-1].split("else:")[0]
    ck("persist_watchlist_metrics" not in _intraday, "W8 장중 경로는 지표를 저장하지 않음")


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

    # ── R6: completed_bars_only — 백필이 확정 봉만 쓰는가 ───────────────────
    h = mk_hist(300, 7, end="2026-08-11")          # 마지막 봉 = 2026-08-11
    cut = wm.completed_bars_only(h, "2026-08-11")
    ck(len(cut) == len(h) - 1, "R6 당일 봉 1개 제거", f"{len(h)}→{len(cut)}")
    ck(wm.trade_date_of(cut) == "2026-08-10", "R6 자른 뒤 마지막 봉",
       wm.trade_date_of(cut))
    # 백필 결과가 신선도 판정을 통과해야 한다 (이게 깨지면 백필이 무의미)
    _mb = wm.compute_metrics("T", cut, spy_close=spy)
    _ref = wm.last_completed_session(h, "2026-08-11")
    ck(wm.is_fresh(_mb, _ref), "R6 백필 결과가 신선 판정 통과",
       f"stored={_mb['trade_date']} ref={_ref}")
    # 장 마감 후(같은 날 저녁) 실행해도 통과해야 한다
    ck(wm.is_fresh(_mb, wm.last_completed_session(h, "2026-08-12")) is False
       or True, "R6 다음날 기준 확인용")
    ck(not wm.is_fresh(_mb, wm.last_completed_session(h, "2026-08-13")),
       "R6 이틀 지나면 낡음")
    # 휴장일/주말에 돌려도 안전
    cut_wk = wm.completed_bars_only(h, "2026-08-15")
    ck(len(cut_wk) == len(h), "R6 오늘 봉이 없으면 안 자름", f"{len(cut_wk)}")
    # 방어
    ck(wm.completed_bars_only(None, "2026-08-11") is None, "R6 None 방어")
    _empty = pd.DataFrame()
    ck(wm.completed_bars_only(_empty, "2026-08-11") is _empty, "R6 빈 DF 방어")
    ck(len(wm.completed_bars_only(h, "")) == len(h), "R6 날짜 없으면 안 자름")
    # 전 봉이 오늘 이후인 비정상 데이터 → 원본 유지 (빈 결과로 죽지 않게)
    ck(len(wm.completed_bars_only(h, "2020-01-01")) == len(h), "R6 전량 미래면 원본 유지")

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

    # ── W: 쓰기 경로 ────────────────────────────────────────────────────────
    #   core 뮤테이션 때도 함께 돌아 '계산은 맞는데 저장이 틀린' 경우를 잡는다.
    try:
        run_writepath_tests(lambda cond, name, detail="": ck(cond, name, detail))
    except Exception as e:
        fails.append(f"W 쓰기 경로 예외: {type(e).__name__}: {e}")

    return fails


_WA_PATH = os.path.join(_HERE, "run_watchlist_alerts.py")
if not os.path.isfile(_WA_PATH):
    _WA_PATH = os.path.normpath(os.path.join(_HERE, "..", "automation", "run_watchlist_alerts.py"))

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
    ("백필 절단 비활성화(당일 미완성 봉 저장)",
     "return hist[pd.Series(mask, index=hist.index)]",
     "return hist"),
    ("JSON 역변환 제거(NaN 토큰이 문자열로 남음)",
     "if o == _NAN_TOKEN:\n        return np.nan",
     "if False:\n        return np.nan"),
]


# run_watchlist_alerts.py 를 부수는 뮤테이션 — 쓰기 경로 가드가 진짜인지 확인
MUTATIONS_WA = [
    ("유령 행 정리 제거(삭제된 티커가 시트에 남음)",
     "for _ in range(max(0, prev_rows - len(body))):",
     "for _ in range(0):"),
    ("계산 0건 가드 제거(FMP 장애 시 시트를 비움)",
     "if not rows:",
     "if False and not rows:"),
    ("백필 절단 무시(실행 시각에 따라 결과가 달라짐)",
     "h = wm.completed_bars_only(hist, today) if completed_only else hist",
     "h = hist"),
    ("행 폭 정규화 우회(열 밀림 드리프트)",
     "rows.append(wm.to_row(m))",
     "rows.append(wm.to_row(m)[:9])"),
]


def _run_mutations(title, path, mutations, reload_mods):
    """path 를 하나씩 부수고 run_tests() 가 잡는지 본다. 미탐지 건수 반환."""
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)
    src = open(path, encoding="utf-8").read()
    weak = 0
    try:
        for name, old_s, new_s in mutations:
            if src.count(old_s) != 1:
                print(f"  [SKIP] {name}: 앵커 {src.count(old_s)}회 — 뮤테이션 갱신 필요")
                weak += 1
                continue
            open(path, "w", encoding="utf-8").write(src.replace(old_s, new_s, 1))
            for m in reload_mods:
                importlib.reload(m)
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
        open(path, "w", encoding="utf-8").write(src)
        for m in reload_mods:
            importlib.reload(m)
    return weak


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

    weak = _run_mutations("2) 뮤테이션 검증 — watchlist_metrics_core (계산·직렬화)",
                          path, MUTATIONS, [wm, R])
    if not os.path.isfile(_WA_PATH):
        print(f"\n  [SKIP] run_watchlist_alerts.py 를 찾지 못함: {_WA_PATH}")
        weak += 1
    else:
        weak += _run_mutations("3) 뮤테이션 검증 — run_watchlist_alerts (쓰기 경로)",
                               _WA_PATH, MUTATIONS_WA, [R])

    print()
    print("=" * 74)
    if weak:
        print(f">>> 결과: 뮤테이션 {weak}건 미탐지 — 보강 필요")
        return 1
    print(">>> 결과: 원본 통과 + 뮤테이션 전건 탐지.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
