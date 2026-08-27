#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regime_core 52주 창 결함 — 영향 측정 (읽기 전용).

무엇을 재는가
─────────────
`regime_core.classify_regime` 의 216·217행:

    high_52w = float(close.max())     # 이름은 52주
    low_52w  = float(close.min())     # 계산은 들어온 길이 전체

`limit` 이 FMP 에서 무시되어 지금 **1254봉(약 5년)** 이 들어온다. 즉 "52주 고점
대비"가 실제로는 **5년 고점 대비**다. 그리고 이건 표시가 아니라 신호다:

    pct_from_high → W_NEAR_HIGH(15) ┐
    pct_from_low  → W_ABOVE_LOW(10) ┴→ score → regime → leaders/setups/excluded

이 스크립트는 **현재 코드와 tail(252) 로 고친 코드를 같은 데이터에 돌려**
얼마나 달라지는지만 잰다. 아무것도 수정하지 않는다.
  · 시트 쓰기 없음 · 파일 쓰기 없음 · regime_core 원본 무변경
  · FMP 읽기 = 종목 수 + 1(SPY)

⚠️ 이것은 채택 여부를 정하는 측정이 아니다
──────────────────────────────────────────
216·217행은 **버그**다. 변수명과 주석("52주 고점 근접")이 의도를 명시하는데
계산이 그걸 안 지킨다. 같은 파일 2398행은 `close.tail(252).max()` 로,
2310행은 주석까지 달아 `_DD_FALLBACK_WINDOW` 로 **이미 제대로 하고 있다** —
"명시 고정하지 않으면 다년 고점이 되어 소비자마다 답이 갈린다"고 적혀 있다.
같은 실패 모드를 인식하고 고친 흔적이 있는데 216·217행만 빠졌다.

따라서 "백테스트가 좋아지면 채택"으로 걸면 **결과를 보고 correctness 를
협상하는 것**이 된다. 이 측정의 목적은 채택 판단이 아니라 셋뿐이다:

  (1) 배포일에 레짐이 몇 종목이나 뒤집히는가 → 알림 폭증 대비
  (2) score 이동 폭이 상수 튜닝(NEAR_HIGH_ZERO 등) 재검토를 부르는 크기인가
  (3) 지금 실제로 몇 봉이 들어오고 있는가 (5년 가설의 실측 확인)

**그래서 이 스크립트는 판정(pass/fail)을 내지 않는다.** 분포만 보고한다.
사전 기준을 세울 대상이 아니기 때문이다.

어떻게 두 버전을 만드는가
─────────────────────────
`regime_core.py` **소스를 읽어 단일 앵커로 2줄만 치환**하고 별도 네임스페이스에
exec 한다. 재구현하지 않는다 — 대역(mock)은 반드시 실물에서 멀어지고(§6-4),
score 공식을 베끼면 상수가 바뀔 때 조용히 갈라진다. 이 방식은 "실물 함수에
정확히 한 곳만 다른 쌍둥이"를 보장한다.

디스크의 regime_core.py 는 건드리지 않는다.

실행
────
    python automation/diag_regime_window.py                # 실측
    python automation/diag_regime_window.py --max 40       # 종목 수 제한
    python automation/diag_regime_window.py --selftest     # 네트워크 없음
"""
from __future__ import annotations

import ast
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np   # noqa: E402
import pandas as pd  # noqa: E402

import fmp_http as fh  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════════════════════════
W52_BARS = 252             # 52주 = 약 252 거래일
ENDPOINT = "historical-price-eod/full"
TIMEOUT = 20.0
MAX_TICKERS_DEFAULT = 80   # 콜 수 상한 (종목당 1콜)
BENCH = "SPY"

_WATCHLIST_WS = "Watchlist"     # col0=uid, col1=ticker
_PORTFOLIO_WS = "Portfolios"    # col0=uid, col2=ticker
_SPREADSHEET = "Quant_DB"

# gspread 실패 시 대체 표본 — 섹터가 갈리도록 고른다.
FALLBACK_SAMPLE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    "JPM", "XOM", "UNH", "JNJ", "PG", "HD", "CAT", "LMT",
    "SPY", "QQQ", "IWM", "XBI", "IBB", "ARKG", "GNOM", "SCHD",
]

# ── 단일 앵커 패치 ────────────────────────────────────────────────────────
PATCH_OLD = ("    high_52w = float(close.max())\n"
             "    low_52w = float(close.min())")
PATCH_NEW = (f"    _w52 = close.tail({W52_BARS})\n"
             f"    high_52w = float(_w52.max())\n"
             f"    low_52w = float(_w52.min())")


# ══════════════════════════════════════════════════════════════════════════
# 순수 함수 — selftest 대상
# ══════════════════════════════════════════════════════════════════════════
def apply_patch(src):
    """regime_core 소스에 52주 창 고정을 적용. 앵커가 정확히 1회여야 한다.

    다중 치환을 절대 허용하지 않는다 — 조용히 두 곳이 바뀌면 무엇을 측정한
    것인지 알 수 없게 된다.
    """
    n = src.count(PATCH_OLD)
    if n != 1:
        raise ValueError(f"패치 앵커가 {n}회 발견됨 (1회여야 함) — "
                         "regime_core.py 가 바뀌었다. 앵커를 갱신할 것.")
    return src.replace(PATCH_OLD, PATCH_NEW)


def load_pair(regime_path):
    """(원본모듈, 패치모듈). 디스크의 regime_core.py 는 건드리지 않는다."""
    with open(regime_path, "r", encoding="utf-8") as f:
        src = f.read()
    orig = types.ModuleType("regime_core_orig")
    orig.__dict__["__file__"] = regime_path
    exec(compile(src, "regime_core_orig", "exec"), orig.__dict__)

    patched = types.ModuleType("regime_core_w52")
    patched.__dict__["__file__"] = regime_path
    exec(compile(apply_patch(src), "regime_core_w52", "exec"), patched.__dict__)
    return orig, patched


def rows_to_df(rows):
    """FMP 레코드 → OHLCV DataFrame. 생산 경로와 동일 규칙."""
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    df = pd.DataFrame([r for r in rows if isinstance(r, dict)])
    if df.empty or "date" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df = df.rename(columns={"close": "Close", "open": "Open", "high": "High",
                            "low": "Low", "volume": "Volume",
                            "adjClose": "Adj Close"})
    for c in ("Close", "Open", "High", "Low", "Volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def compare_one(orig, patched, hist, spy_close=None):
    """한 종목의 현재/수정후 결과 비교. Returns: dict | None"""
    try:
        a = orig.classify_regime(hist, spy_close=spy_close)
        b = patched.classify_regime(hist, spy_close=spy_close)
    except Exception as e:
        return {"error": type(e).__name__ + ": " + str(e)[:60]}
    ca, cb = a.get("components", {}), b.get("components", {})

    def _f(d, k):
        v = d.get(k, np.nan)
        try:
            return float(v)
        except (TypeError, ValueError):
            return np.nan

    return {
        "bars": int(len(hist)),
        "regime_a": str(a.get("regime")), "regime_b": str(b.get("regime")),
        "stage_a": int(a.get("stage") or 0), "stage_b": int(b.get("stage") or 0),
        "score_a": _f(a, "score"), "score_b": _f(b, "score"),
        "top_a": bool(a.get("topping")), "top_b": bool(b.get("topping")),
        "pfh_a": _f(ca, "pct_from_high"), "pfh_b": _f(cb, "pct_from_high"),
        "pfl_a": _f(ca, "pct_from_low"), "pfl_b": _f(cb, "pct_from_low"),
        "enough_a": bool(a.get("enough_data")),
    }


def transition_matrix(recs):
    """{(현재, 수정후): 건수}. 대각선이 '변화 없음'."""
    m = {}
    for r in recs:
        if "error" in r:
            continue
        k = (r["regime_a"], r["regime_b"])
        m[k] = m.get(k, 0) + 1
    return m


def alert_candidates(recs):
    """배포일에 알림을 부를 후보 — strong/weak 경계를 넘는 종목만.

    sideways↔unknown 은 알림 상태기계가 다루지 않으므로 세지 않는다.
    과대집계하면 배포 계획이 필요 이상으로 보수적이 된다.
    """
    hot = {"strong", "weak"}
    return [r for r in recs
            if "error" not in r
            and r["regime_a"] != r["regime_b"]
            and (r["regime_a"] in hot or r["regime_b"] in hot)]


def dist(vals, label, unit=""):
    """분포 한 줄. 빈 입력에서 조용히 사라지지 않는다."""
    v = [x for x in vals if x is not None and not (isinstance(x, float) and np.isnan(x))]
    if not v:
        return f"  {label:<22} (유효값 없음)"
    s = pd.Series(v, dtype="float64")
    return (f"  {label:<22} 중앙 {s.median():>8.2f}{unit} · 평균 {s.mean():>8.2f}{unit}"
            f" · 최소 {s.min():>8.2f} · 최대 {s.max():>8.2f} · N={len(s)}")


# ══════════════════════════════════════════════════════════════════════════
# 유니버스 · 데이터
# ══════════════════════════════════════════════════════════════════════════
def load_universe(max_n):
    """Watchlist + Portfolios 티커 (읽기 전용). 실패하면 고정 표본."""
    key = os.environ.get("GSPREAD_KEY", "")
    if not key:
        print("  [INFO] GSPREAD_KEY 없음 — 고정 표본 사용")
        return FALLBACK_SAMPLE[:max_n], "고정표본"
    try:
        import json
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(key),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly",
                    "https://www.googleapis.com/auth/drive.readonly"])
        sh = gspread.authorize(creds).open(_SPREADSHEET)
        out = []
        for ws_name, col in ((_WATCHLIST_WS, 1), (_PORTFOLIO_WS, 2)):
            try:
                for r in (sh.worksheet(ws_name).get_all_values() or [])[1:]:
                    if len(r) > col:
                        t = str(r[col]).strip().upper()
                        if t and t not in out:
                            out.append(t)
            except Exception as e:
                print(f"  [WARN] {ws_name} 읽기 실패: {type(e).__name__}")
        if out:
            return out[:max_n], f"Watchlist+Portfolios({len(out)}종목)"
    except Exception as e:
        print(f"  [WARN] gspread 실패({type(e).__name__}) — 고정 표본 사용")
    return FALLBACK_SAMPLE[:max_n], "고정표본"


def fetch_hist(ticker):
    """생산 경로와 동일하게 파라미터 없이 받는다 — 지금 실제로 오는 그대로."""
    r, status, kind = fh.fmp_get_ex(
        fh.fmp_url(f"{ENDPOINT}?symbol={ticker}"), timeout=TIMEOUT)
    if r is None or kind != "ok":
        return pd.DataFrame(), kind
    try:
        d = r.json()
        rows = d.get("historical", d) if isinstance(d, dict) else d
        df = rows_to_df(rows)
        return df, ("ok" if not df.empty else "empty")
    except Exception:
        return pd.DataFrame(), "bad_json"


# ══════════════════════════════════════════════════════════════════════════
# 보고
# ══════════════════════════════════════════════════════════════════════════
def report(recs, source_label):
    print("\n" + "=" * 74)
    print("결과")
    print("=" * 74)

    errs = [r for r in recs if "error" in r]
    ok = [r for r in recs if "error" not in r]
    print(f"\n  유니버스: {source_label} · 비교 성공 {len(ok)}종목"
          + (f" · 실패 {len(errs)}" if errs else ""))
    for r in errs[:5]:
        print(f"    실패: {r['error']}")

    if not ok:
        print("  비교할 데이터가 없습니다.")
        return

    # (3) 지금 실제로 몇 봉이 들어오는가
    bars = [r["bars"] for r in ok]
    print(f"\n── 실제 수신 봉수 (5년 가설 확인) ──")
    print(dist(bars, "봉수", "봉"))
    over = sum(1 for b in bars if b > W52_BARS)
    print(f"  {W52_BARS}봉 초과: {over}/{len(bars)}종목 "
          f"— 이 종목들만 결과가 달라질 수 있다")

    # (1) 레짐 전이
    print(f"\n── 레짐 전이 (현재 → tail({W52_BARS}) 수정후) ──")
    m = transition_matrix(ok)
    same = sum(v for (a, b), v in m.items() if a == b)
    diff = sum(v for (a, b), v in m.items() if a != b)
    for (a, b), v in sorted(m.items(), key=lambda kv: -kv[1]):
        mark = "  " if a == b else "→ "
        print(f"  {mark}{a:>9} → {b:<9} {v:>4}종목")
    print(f"\n  변화 없음 {same} · **변화 {diff}** "
          f"({diff / len(ok) * 100:.1f}%)")

    cands = alert_candidates(ok)
    print(f"\n── 배포일 알림 후보 (strong/weak 경계를 넘는 것만) ──")
    print(f"  {len(cands)}종목")
    for r in cands[:15]:
        print(f"    {r['regime_a']:>9} → {r['regime_b']:<9} "
              f"score {r['score_a']:>5.1f} → {r['score_b']:>5.1f} "
              f"(고점대비 {r['pfh_a']:>7.1f}% → {r['pfh_b']:>7.1f}%)")
    if len(cands) > 15:
        print(f"    … 외 {len(cands) - 15}종목")

    # (2) score 이동 폭
    print(f"\n── score / 지표 이동 폭 ──")
    ds = [r["score_b"] - r["score_a"] for r in ok]
    print(dist(ds, "score 변화", "점"))
    print(dist([r["pfh_b"] - r["pfh_a"] for r in ok], "고점대비 변화", "%p"))
    print(dist([r["pfl_b"] - r["pfl_a"] for r in ok], "저점대비 변화", "%p"))
    up = sum(1 for d in ds if d > 0.05)
    dn = sum(1 for d in ds if d < -0.05)
    print(f"  score 상승 {up} · 하락 {dn} · 변화없음 {len(ds) - up - dn}")

    tflip = sum(1 for r in ok if r["top_a"] != r["top_b"])
    print(f"\n  천장(topping) 판정 변화: {tflip}종목")

    print(f"\n  ⚠️ 이 수치는 채택 판단용이 아니다. 216·217행은 버그이고,")
    print(f"     이 측정의 목적은 배포일 알림 폭증 대비와 상수 재검토 필요성")
    print(f"     판단이다. 자세한 근거는 파일 상단 독스트링 참조.")


# ══════════════════════════════════════════════════════════════════════════
# selftest
# ══════════════════════════════════════════════════════════════════════════
def _mkhist(closes):
    idx = pd.date_range("2020-01-01", periods=len(closes), freq="B")
    c = pd.Series(closes, index=idx, dtype="float64")
    return pd.DataFrame({"Close": c, "Open": c, "High": c, "Low": c,
                         "Volume": pd.Series(1e6, index=idx)})


def _raw_get_calls(path=None, src=None):
    """diag_fmp_ssot.raw_fmp_gets 와 동일 규칙 (A1 래칫)."""
    if src is None:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    hits = []
    for c in ast.walk(tree):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "get" and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "requests"):
            continue
        try:
            arg = ast.unparse(c.args[0]) if c.args else ""
        except Exception:
            arg = ""
        if "financialmodelingprep" in arg or "apikey" in arg:
            hits.append((c.lineno, arg[:60]))
    return hits


def selftest(regime_path):
    fails, ran = [], []

    def check(name, got, want):
        ran.append(name)
        ok = (got == want)
        print(f"  {'✅' if ok else '❌'} {name}: {got}"
              + ("" if ok else f"  (기대 {want})"))
        if not ok:
            fails.append(name)

    print("\n" + "=" * 74)
    print("selftest — 네트워크·시트 접근 없음")
    print("=" * 74)

    print("\n 패치 적용")
    with open(regime_path, "r", encoding="utf-8") as f:
        src = f.read()
    check("S1 앵커가 정확히 1회", src.count(PATCH_OLD), 1)
    patched_src = apply_patch(src)
    check("S2 패치 후 원본 앵커 소멸", patched_src.count(PATCH_OLD), 0)
    check("S3 패치가 실제로 다르다", patched_src != src, True)
    # ⚠️ check() 에 파일 내용을 넘기면 실패 시 regime_core.py 전문이 로그를
    #    뒤덮는다(초안이 그랬다). 불리언만 넘긴다 — §6-5 "진단이 진단을 가린다".
    with open(regime_path, "r", encoding="utf-8") as _f:
        _still = _f.read()
    check("S4 디스크 원본 무변경", _still == src, True)
    try:
        apply_patch("no anchor here")
        got = "예외 없음"
    except ValueError:
        got = "ValueError"
    check("S5 앵커 없으면 예외 (조용한 무동작 금지)", got, "ValueError")
    try:
        apply_patch(src + "\n" + PATCH_OLD)
        got2 = "예외 없음"
    except ValueError:
        got2 = "ValueError"
    check("S6 앵커 2회면 예외 (다중 치환 금지)", got2, "ValueError")

    orig, patched = load_pair(regime_path)
    check("S7 두 모듈 모두 classify_regime 보유",
          hasattr(orig, "classify_regime") and hasattr(patched, "classify_regime"), True)
    check("S8 두 모듈은 서로 다른 객체", orig is patched, False)

    print("\n 불변식 — 길이가 창 이하면 두 버전이 **정확히 일치해야** 한다")
    # 100봉: tail(252) == 전체 → 결과가 같아야 한다.
    # 이 케이스는 창 크기가 252 가 아닌 값으로 바뀌면(예: 52) 깨진다.
    # ⚠️ 난수를 쓰지 않는다. 초안은 rng 상태를 앞 케이스와 공유해서 뽑히는
    #    값에 따라 S14 가 통과하기도 실패하기도 했다. 성질을 검증하는
    #    테스트는 그 성질이 **반드시 성립하는** 데이터로 해야 한다.
    short = [50.0 + 0.1 * i for i in range(100)]      # 100봉 단조 상승
    r_short = compare_one(orig, patched, _mkhist(short))
    check("S9 100봉 — regime 동일",
          r_short.get("regime_a") == r_short.get("regime_b"), True)
    check("S10 100봉 — score 동일",
          r_short.get("score_a") == r_short.get("score_b"), True)
    check("S11 100봉 — 고점대비 동일",
          r_short.get("pfh_a") == r_short.get("pfh_b"), True)

    print("\n 역검증 — 창 밖에 최고점이 있으면 **반드시 달라야** 한다")
    # 앞 100봉 안에 500 짜리 고점, 이후 400봉은 55→75 단조 상승.
    # tail(252) 는 전부 상승 구간 안에 들어가므로 그 고점을 못 본다.
    #   전체 고점 500 → 고점대비 -85%  ·  tail(252) 고점 75 → 0%
    #   전체 저점  50 → 저점대비 +50%  ·  tail(252) 저점 ~62 → +20%
    # 두 항목 모두 clip 구간 안이라 score 차이가 **반드시** 난다.
    spike = [50.0] * 100
    spike[50] = 500.0
    spike += [55.0 + 20.0 * i / 399.0 for i in range(400)]
    r_long = compare_one(orig, patched, _mkhist(spike))
    # ⚠️ `r.get(k) or 0` 을 쓰지 않는다. pfh_b 는 정확히 0.0 이 될 수 있고
    #    0.0 은 falsy 라 폴백값으로 바뀐다(초안에서 S13 이 그래서 실패했다).
    def _v(d_, k):
        x = d_.get(k)
        return float("nan") if x is None else float(x)

    check("S12 550봉 — 고점대비가 달라진다",
          abs(_v(r_long, "pfh_a") - _v(r_long, "pfh_b")) > 1.0, True)
    check("S13 550봉 — 수정후가 고점에 더 가깝다",
          _v(r_long, "pfh_b") > _v(r_long, "pfh_a"), True)
    check("S14 550봉 — score 가 달라진다",
          abs(_v(r_long, "score_a") - _v(r_long, "score_b")) > 1e-9, True)
    check("S14b 550봉 — 저점대비도 달라진다",
          abs(_v(r_long, "pfl_a") - _v(r_long, "pfl_b")) > 1.0, True)
    check("S14c 550봉 — 레짐이 실제로 전이한다 (sideways→strong)",
          (r_long.get("regime_a"), r_long.get("regime_b")), ("sideways", "strong"))

    print("\n 집계 함수")
    R = [{"regime_a": "strong", "regime_b": "strong", "score_a": 1, "score_b": 1},
         {"regime_a": "sideways", "regime_b": "strong", "score_a": 1, "score_b": 2},
         {"regime_a": "weak", "regime_b": "sideways", "score_a": 1, "score_b": 2},
         {"regime_a": "sideways", "regime_b": "unknown", "score_a": 1, "score_b": 2},
         {"error": "boom"}]
    m = transition_matrix(R)
    check("S15 전이 매트릭스 — 에러행 제외", sum(m.values()), 4)
    check("S16 대각선 집계", m.get(("strong", "strong")), 1)
    cands = alert_candidates(R)
    check("S17 알림후보 — strong/weak 경계만", len(cands), 2)
    check("S18 sideways→unknown 은 제외",
          any(r["regime_b"] == "unknown" for r in cands), False)
    check("S19 변화 없는 행 제외",
          any(r["regime_a"] == r["regime_b"] for r in cands), False)

    print("\n dist — 빈 입력에서 조용히 사라지지 않는다")
    check("S20 빈 입력 표시", "유효값 없음" in dist([], "x"), True)
    check("S21 NaN 만 있어도 표시", "유효값 없음" in dist([float("nan")], "x"), True)
    check("S22 정상 입력", "N=3" in dist([1.0, 2.0, 3.0], "x"), True)

    print("\n rows_to_df")
    check("S23 정렬 오름차순",
          rows_to_df([{"date": "2026-06-02", "close": 2},
                      {"date": "2026-06-01", "close": 1}])["Close"].iloc[0], 1.0)
    check("S24 빈 입력 → 빈 DF", rows_to_df([]).empty, True)
    check("S25 date 없으면 빈 DF", rows_to_df([{"close": 1}]).empty, True)

    print("\n 구조 (diag_fmp_ssot A1 래칫)")
    me = os.path.abspath(__file__)
    check("S26 원시 requests.get 0곳", len(_raw_get_calls(me)), 0)
    check("S27 탐지기 역검증",
          len(_raw_get_calls(src="import requests\n"
                                 "r=requests.get(f'{B}/q?apikey={k}')\n")), 1)

    print("\n" + "=" * 74)
    if fails:
        print(f"❌ {len(ran)}건 중 {len(fails)}건 실패: {', '.join(fails)}")
        return 1
    print(f"✅ 전 항목 통과 ({len(ran)}/{len(ran)})")
    return 0


# ══════════════════════════════════════════════════════════════════════════
def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    regime_path = os.path.join(root, "regime_core.py")
    if not os.path.exists(regime_path):
        regime_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "regime_core.py")
    if not os.path.exists(regime_path):
        print("[ERR] regime_core.py 를 찾을 수 없습니다")
        return 1

    if "--selftest" in sys.argv:
        return selftest(regime_path)

    max_n = MAX_TICKERS_DEFAULT
    if "--max" in sys.argv:
        try:
            max_n = max(1, int(sys.argv[sys.argv.index("--max") + 1]))
        except (IndexError, ValueError):
            pass

    if not fh.fmp_key():
        print("[ERR] FMP_API_KEY 없음")
        return 1

    print("=" * 74)
    print(f"regime_core 52주 창 영향 측정 — 읽기 전용 · 시트 쓰기 없음")
    print(f"현재: close 전체 집계  vs  수정후: close.tail({W52_BARS})")
    print("=" * 74)

    orig, patched = load_pair(regime_path)
    print("\n  패치 적용 완료 (단일 앵커 2줄 · 디스크 원본 무변경)")

    tickers, source_label = load_universe(max_n)
    print(f"  대상 {len(tickers)}종목 · FMP {len(tickers) + 1}콜 예정")

    spy_df, spy_kind = fetch_hist(BENCH)
    spy_close = spy_df["Close"] if ("Close" in spy_df.columns) else None
    if spy_close is None:
        print(f"  [WARN] {BENCH} 실패({spy_kind}) — RS 항목 없이 진행 "
              "(양쪽 동일 조건이므로 비교는 성립)")

    recs, nodata = [], []
    for i, tk in enumerate(tickers, 1):
        if tk == BENCH:
            hist, kind = spy_df, spy_kind
        else:
            hist, kind = fetch_hist(tk)
        if hist.empty:
            nodata.append((tk, kind))
            continue
        r = compare_one(orig, patched, hist, spy_close=spy_close)
        r["ticker"] = tk
        recs.append(r)
        if i % 20 == 0:
            print(f"    … {i}/{len(tickers)}")

    if nodata:
        print(f"\n  데이터 없음 {len(nodata)}종목: "
              + ", ".join(f"{t}({k})" for t, k in nodata[:8]))

    report(recs, source_label)
    print(f"\n  {fh.fmp_stats_line()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
