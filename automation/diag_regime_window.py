#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regime_core 52주 창 회귀 가드 (읽기 전용 · 네트워크 없음).

무엇을 지키는가
───────────────
`classify_regime` 의 52주 고점/저점은 **입력 길이에 무관해야 한다.**

예전 코드:

    high_52w = float(close.max())     # 이름은 52주
    low_52w  = float(close.min())     # 계산은 들어온 길이 전체

FMP 가 `limit` 을 무시해 1254봉(약 5년)이 들어오면서 "52주 고점 대비"가 실제로는
"5년 고점 대비"였다. 그리고 이건 표시가 아니라 신호다:

    pct_from_high → W_NEAR_HIGH(15) ┐
    pct_from_low  → W_ABOVE_LOW(10) ┴→ score → regime → leaders/setups/excluded

게다가 `classify_regime` 은 **이미 가변 길이로 호출된다** — `regime_core:618` 은
진입 시점 재구성용 `sliced`(짧음), `:637` 은 현재 `hist`(1254봉). 즉 같은 종목의
then/now 비교가 서로 다른 고점 창을 쓰고 있었다.

왜 가드가 필요한가
──────────────────
`regime_core` 에는 **같은 실패 모드가 이미 주석으로 기록돼 있다**:

  > 폴백 고점 — 입력 길이에 무관하게 최근 1년으로 고정. (…명시 고정하지 않으면
  > 다년 고점이 되어 소비자마다 답이 갈린다.)

`close.tail(252).max()` 로 제대로 하는 곳도 따로 있다. **인식하고 두 곳을
고쳐놓고도 216·217행은 놓쳤다.** 사람 눈으로 잡는 방식이 이미 한 번 실패했다.
그래서 도구로 옮긴다.

검사 구조
─────────
  R  구조 (AST)   `classify_regime` 안에서 창 없는 `.max()/.min()` 금지
  W  상수 핀      W52_BARS == 252 (자기참조 테스트는 상수 변경을 못 잡는다)
  L  길이 불변식  더 오래된 봉을 앞에 붙여도 52주 지표가 **바뀌지 않아야** 한다
  X  역검증       옛 코드를 되살려 L 이 **실패하는지** 확인 (판별력 자체 증명)
  S  자체 점검    탐지기가 오탐/누락을 내지 않는가

X 그룹이 핵심이다. 스위트가 자기 판별력을 매 실행마다 증명한다 — L 이 통과하는데
X 도 통과하면 L 은 아무것도 검사하지 않는 것이다(§6-1 "None 을 기대하는 테스트는
잘못된 이유로도 통과한다").

안전성
──────
· 네트워크 없음 · 시트 없음 · FMP 콜 0 · 파일 쓰기 없음
· `regime_core.py` 는 읽기만 한다. 역검증용 옛 버전은 메모리에서만 만든다.
· 부작용 없다. 몇 번을 돌려도 같은 결과.

실행:  python automation/diag_regime_window.py
"""
from __future__ import annotations

import ast
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════
# 사전 확정값
# ══════════════════════════════════════════════════════════════════════════
EXPECT_W52_BARS = 252        # 명시 핀. regime_core 가 이 값을 바꾸면 실패한다.
EXPECT_MIN_BARS_FULL = 252   # 명시 핀. 52주 창 요구치(220 슬로프 수렴보다 크다).
TARGET_FN = "classify_regime"
AGG_ATTRS = {"max", "min"}   # 창 없이 쓰면 길이 의존이 생기는 집계
WINDOWING = {"tail", "head", "iloc", "loc", "rolling", "last", "first"}

# 역검증용 — 옛 코드 복원 (메모리에서만)
NEW_BODY = ("    _w52 = close.tail(W52_BARS)\n"
            "    high_52w = float(_w52.max())\n"
            "    low_52w = float(_w52.min())")
OLD_BODY = ("    high_52w = float(close.max())\n"
            "    low_52w = float(close.min())")

_fails, _ran = [], []


def check(name, got, want):
    _ran.append(name)
    ok = (got == want)
    print(f"  {'✅' if ok else '❌'} {name}: {got}" + ("" if ok else f"  (기대 {want})"))
    if not ok:
        _fails.append(name)


# ══════════════════════════════════════════════════════════════════════════
# 순수 함수
# ══════════════════════════════════════════════════════════════════════════
def _fn_node(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _is_windowed_expr(node):
    """이 식이 '행 범위를 좁힌' 결과인가.

    ⚠️ 컬럼 선택과 행 슬라이싱을 반드시 구분한다. 초안은 "Subscript 가 있으면
       창을 거친 것"으로 봤는데, `close = pd.to_numeric(hist["Close"], ...)` 의
       `hist["Close"]` 가 Subscript 라서 **close 자신이 안전 목록에 들어갔다.**
       그 결과 옛 코드의 `close.max()` 를 한 건도 못 잡았다(X4 가 발견).
       R1 의 '0곳'도 같은 이유로 잘못 통과하고 있었다.

    규칙:
      · .tail/.head/.iloc/.loc/.rolling/.last/.first  → 창
      · Subscript 의 첨자가 슬라이스이거나 불리언 마스크 → 창
      · Subscript 의 첨자가 문자열 상수(= 컬럼 이름)     → 창 아님
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in WINDOWING:
            return True
        if isinstance(sub, ast.Subscript):
            idx = sub.slice
            if isinstance(idx, ast.Constant) and isinstance(idx.value, str):
                continue                      # 컬럼 선택 — 행을 안 줄인다
            return True
    return False


def windowed_names(fn):
    """함수 안에서 '창을 거친' 시리즈로 대입된 이름들.

    `_w52 = close.tail(252)` → {"_w52"}
    이 이름들에 대한 .max()/.min() 은 길이 의존이 없으므로 허용한다.
    """
    out = set()
    if fn is None:
        return out
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign):
            continue
        if not _is_windowed_expr(n.value):
            continue
        for t in n.targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
    return out


def unscoped_aggs(src, fn_name=TARGET_FN):
    """`fn_name` 안에서 **창 없이** .max()/.min() 을 부르는 지점.

    Returns: [(lineno, "close.max"), ...]

    판정 규칙 — 좁게 잡는다. 오탐이 나오는 가드는 곧 무시당한다(§6-3):
      · 대상이 **단순 이름**인 경우만 본다. `close.max()` 는 잡고,
        `close.tail(252).max()` 는 잡지 않는다.
      · 창을 거쳐 대입된 이름(`_w52`, `post` 등)도 잡지 않는다.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [(0, "구문오류")]
    fn = _fn_node(tree, fn_name)
    if fn is None:
        return [(0, f"{fn_name} 없음")]
    safe = windowed_names(fn)
    hits = []
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in AGG_ATTRS):
            continue
        base = n.func.value
        if not isinstance(base, ast.Name):
            continue                      # 체인 호출 — 창을 거친 것으로 본다
        if base.id in safe:
            continue
        hits.append((n.lineno, f"{base.id}.{n.func.attr}"))
    return hits


def module_const(src, name, allow_name: bool = False):
    """모듈 최상위 상수 값. 없으면 None.

    allow_name=True 면 `A = B` 처럼 **다른 상수를 가리키는** 대입도 받아
    그 이름(str)을 돌려준다. MIN_BARS_FULL 이 W52_BARS 를 그대로 가리키는지
    확인하려면 값이 아니라 **연결 자체**를 봐야 하기 때문 — 둘 다 252 라고
    따로 적어두면 한쪽만 바뀌어도 통과한다(§5-5 자기참조 핀 문제).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for n in tree.body:
        if not isinstance(n, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
            continue
        if isinstance(n.value, ast.Constant) and isinstance(n.value.value, int):
            return n.value.value
        if allow_name and isinstance(n.value, ast.Name):
            return n.value.id
    return None


def load_module(src, tag):
    m = types.ModuleType(tag)
    exec(compile(src, tag, "exec"), m.__dict__)
    return m


def price_window_ok(src: str, tree) -> bool:
    """_fmp_price_history 본문이 from/to 창을 쓰고 limit 을 안 쓰는가.

    ⚠️ 파일 전역 문자열 검색으로 하면 안 된다. 같은 파일의 재무제표 호출
       (income-statement · analyst-estimates · balance-sheet)은 limit 이
       정상이므로 전역 검색은 영구 빨간불이 된다 — 실제로 그렇게 짰다가
       C6 이 수정본에서도 실패했다. 함수 본문으로 범위를 좁힌다.
    """
    fn = _fn_node(tree, "_fmp_price_history")
    if fn is None:
        return False
    # ⚠️ 원문(get_source_segment)을 보면 **독스트링과 주석까지 센다.** 전환 이력을
    #    설명하는 문장에 "limit=" 이 들어 있어 실제로 거짓 실패가 났다.
    #    ast.unparse 는 주석을 버리므로, 독스트링만 떼면 코드만 남는다.
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant) and \
            isinstance(body[0].value.value, str):
        body = body[1:]
    code = "\n".join(ast.unparse(st) for st in body)
    if "historical-price-eod" not in code:
        return False
    return ("from=" in code) and ("to=" in code) and ("limit=" not in code)


def classify_call_sites(tree) -> int:
    """classify_regime 호출 지점 수 (Attribute · Name 양쪽)."""
    c = 0
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == TARGET_FN) or \
               (isinstance(f, ast.Name) and f.id == TARGET_FN):
                c += 1
    return c


def excludes_on_full_metrics(tree, fn_name: str) -> bool:
    """그 함수 안에 `full_metrics` 를 검사하고 continue 하는 분기가 있는가.

    표시만 하고 넘어가면 weak 오답이 그대로 나간다. **제외까지** 확인한다.
    단순 문자열 검색으로는 '어느 함수에서'를 구분할 수 없어 AST 로 한다.
    """
    fn = _fn_node(tree, fn_name)
    if fn is None:
        return False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if "full_metrics" not in ast.unparse(node.test):
            continue
        if any(isinstance(s, ast.Continue) for s in ast.walk(node)):
            return True
    return False


def _hist(closes):
    idx = pd.date_range("2018-01-01", periods=len(closes), freq="B")
    c = pd.Series(closes, index=idx, dtype="float64")
    return pd.DataFrame({"Close": c, "Open": c, "High": c, "Low": c,
                         "Volume": pd.Series(1e6, index=idx)})


def length_invariance(mod, extra_bars=600, spike=9999.0, pit=0.01):
    """더 **오래된** 봉을 앞에 붙였을 때 52주 지표가 바뀌는가.

    핵심 성질: 앞에 무엇을 붙이든 최근 252봉은 그대로다. 따라서
    pct_from_high / pct_from_low / score / regime 이 전부 같아야 한다.

    앞부분에 극단적인 고점(spike)과 저점(pit)을 심는다 — 창이 새면 반드시
    티가 나도록. 심지 않으면 "우연히 같은 값"으로 통과할 수 있다.
    """
    recent = [50.0 + 20.0 * i / 599.0 for i in range(600)]   # 최근 600봉 단조 상승
    older = [30.0] * extra_bars
    older[extra_bars // 3] = spike        # 창 밖 초고점
    older[extra_bars // 2] = pit          # 창 밖 초저점
    a = mod.classify_regime(_hist(recent))
    b = mod.classify_regime(_hist(older + recent))
    return a, b


def _cmp(a, b, key):
    ca, cb = a.get("components", {}), b.get("components", {})
    x, y = ca.get(key), cb.get(key)
    if x is None or y is None:
        return None
    return abs(float(x) - float(y))


# ══════════════════════════════════════════════════════════════════════════
def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    path = os.path.join(root, "regime_core.py")
    if not os.path.exists(path):
        path = os.path.join(here, "regime_core.py")
    if not os.path.exists(path):
        print("[ERR] regime_core.py 를 찾을 수 없습니다")
        return 1

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    print("=" * 74)
    print("regime_core 52주 창 회귀 가드 — 네트워크·시트·FMP 콜 없음")
    print(f"대상: {path}")
    print("=" * 74)

    # ── R: 구조 ──────────────────────────────────────────────────────────
    print(f"\n[R] 구조 — {TARGET_FN} 안에서 창 없는 .max()/.min() 금지")
    hits = unscoped_aggs(src)
    for ln, what in hits:
        print(f"     {os.path.basename(path)}:{ln}  {what}()")
    check("R1 창 없는 집계 0곳", len(hits), 0)
    tree = ast.parse(src)
    check(f"R2 {TARGET_FN} 존재", _fn_node(tree, TARGET_FN) is not None, True)
    check("R3 _w52 가 창을 거친 이름으로 인식됨",
          "_w52" in windowed_names(_fn_node(tree, TARGET_FN)), True)

    # ── W: 상수 핀 ───────────────────────────────────────────────────────
    print("\n[W] 상수 핀 — 자기참조 테스트는 상수 변경을 못 잡는다")
    check(f"W1 W52_BARS == {EXPECT_W52_BARS}", module_const(src, "W52_BARS"),
          EXPECT_W52_BARS)

    mod = load_module(src, "regime_core_live")

    # ── B: 봉수 보고 (2026-08-27 신설) ───────────────────────────────────
    print("\n[B] 봉수 보고 — 창 고정은 봉이 252개 '있을 때'만 성립한다")
    check(f"B1 MIN_BARS_FULL == {EXPECT_MIN_BARS_FULL}",
          module_const(src, "MIN_BARS_FULL", allow_name=True), "W52_BARS")
    check("B2 MIN_BARS_FULL 이 W52_BARS 를 가리킨다",
          int(getattr(mod, "MIN_BARS_FULL", -1)), EXPECT_MIN_BARS_FULL)
    _b251 = mod.classify_regime(_hist([50.0 + 0.05 * i for i in range(251)]))
    _b252 = mod.classify_regime(_hist([50.0 + 0.05 * i for i in range(252)]))
    check("B3 251봉 → full_metrics False", _b251.get("full_metrics"), False)
    check("B4 252봉 → full_metrics True", _b252.get("full_metrics"), True)
    check("B5 bars 가 실제 봉수와 일치", (_b251.get("bars"), _b252.get("bars")),
          (251, 252))
    _empty = mod.classify_regime(pd.DataFrame())
    check("B6 빈 입력에도 필드가 존재한다 (None 과 0봉을 구분)",
          (_empty.get("bars"), _empty.get("full_metrics")), (0, False))
    # ⚠️ 핵심: 이 필드는 **보고용**이다. regime/score/enough_data 를 바꾸면
    #    regime_core:629(진입 시점 재구성 → 알림 억제)가 조용히 달라진다.
    check("B7 full_metrics=False 여도 enough_data 는 예전대로 True",
          _b251.get("enough_data"), True)
    check("B8 full_metrics=False 여도 regime 이 계산된다",
          _b251.get("regime") in ("strong", "sideways", "weak"), True)

    # ── C: 모듈 경계 — scanner_core 가 실제로 소비하는가 ──────────────────
    # §3-2 의 실패가 정확히 여기였다: diag_fmp_window [E] 는 파일 단위라
    # scanner_core → regime_core 소비를 구조적으로 못 봤고, 그래서 위험을
    # 엉뚱한 곳에 지목했다. "한계를 적는 것과 대비하는 것은 다르다."
    print("\n[C] 모듈 경계 — scanner_core 가 full_metrics 를 실제로 읽는가")
    sc_path = os.path.join(root, "scanner_core.py")
    if not os.path.exists(sc_path):
        sc_path = os.path.join(here, "scanner_core.py")
    if not os.path.exists(sc_path):
        check("C0 scanner_core.py 발견", False, True)
    else:
        sc_src = open(sc_path, "r", encoding="utf-8").read()
        sc_tree = ast.parse(sc_src)
        n_calls = classify_call_sites(sc_tree)
        check("C1 classify_regime 호출부를 찾았다", n_calls > 0, True)
        check("C2 full_metrics 를 읽는 곳이 있다",
              sc_src.count('"full_metrics"') > 0, True)
        # 라우터는 제외까지 해야 한다 — 표시만으로는 weak 오답이 그대로 나간다
        check("C3 route_candidates_by_regime 이 full_metrics 로 제외한다",
              excludes_on_full_metrics(sc_tree, "route_candidates_by_regime"), True)
        # 룩백 창이 요구치를 만족하는가 (거래일 ≈ 달력일 × 0.690)
        _lb = module_const(sc_src, "_REGIME_LOOKBACK_DAYS")
        check("C4 _REGIME_LOOKBACK_DAYS 상수 존재", isinstance(_lb, int), True)
        if isinstance(_lb, int):
            _bars = int(_lb * 252 / 365)
            print(f"     {_lb}일 → 약 {_bars}거래일 "
                  f"(요구 {EXPECT_MIN_BARS_FULL}봉 · 여유 {_bars - EXPECT_MIN_BARS_FULL}봉)")
            check("C5 룩백이 MIN_BARS_FULL + 30봉 이상을 확보한다",
                  _bars >= EXPECT_MIN_BARS_FULL + 30, True)
        # ⚠️ 파일 전역 "&limit=" 검색은 오탐이다. income-statement ·
        #    analyst-estimates · balance-sheet 는 재무제표라 limit 이 정상이다.
        #    **가격 이력 함수 본문으로 범위를 좁힌다.**
        check("C6 가격 이력이 from/to 로 전환됐다 (limit 잔재 없음)",
              price_window_ok(sc_src, sc_tree), True)

    # ── L: 길이 불변식 ───────────────────────────────────────────────────
    print("\n[L] 길이 불변식 — 창 밖에 초고점·초저점을 심고 앞에 600봉을 덧댄다")
    a, b = length_invariance(mod)
    check("L1 고점대비 동일", _cmp(a, b, "pct_from_high"), 0.0)
    check("L2 저점대비 동일", _cmp(a, b, "pct_from_low"), 0.0)
    check("L3 score 동일", a.get("score"), b.get("score"))
    check("L4 regime 동일", a.get("regime"), b.get("regime"))
    check("L5 stage 동일", a.get("stage"), b.get("stage"))
    short = mod.classify_regime(_hist([50.0 + 0.1 * i for i in range(100)]))
    check("L6 창보다 짧은 100봉도 정상 처리", short.get("enough_data"), True)

    # ── X: 역검증 ────────────────────────────────────────────────────────
    print("\n[X] 역검증 — 옛 코드를 되살려 L 이 **실패하는지** 확인")
    print("     L 이 통과하는데 X 도 통과하면 L 은 아무것도 검사하지 않는 것이다")
    n_new = src.count(NEW_BODY)
    check("X1 되돌릴 앵커가 정확히 1회", n_new, 1)
    if n_new == 1:
        legacy_src = src.replace(NEW_BODY, OLD_BODY)
        legacy = load_module(legacy_src, "regime_core_legacy")
        la, lb = length_invariance(legacy)
        d_hi = _cmp(la, lb, "pct_from_high") or 0.0
        d_lo = _cmp(la, lb, "pct_from_low") or 0.0
        check("X2 옛 코드는 고점대비가 달라진다", d_hi > 1.0, True)
        check("X3 옛 코드는 저점대비가 달라진다", d_lo > 1.0, True)
        check("X4 옛 코드는 구조 검사에 걸린다", len(unscoped_aggs(legacy_src)), 2)
        print(f"     실측 이탈폭 — 고점대비 {d_hi:.1f}%p · 저점대비 {d_lo:.1f}%p")
    else:
        check("X2 옛 코드는 고점대비가 달라진다", "앵커 없어 검증 불가", True)

    # ── S: 탐지기 자체 점검 ──────────────────────────────────────────────
    print("\n[S] 탐지기 자체 점검 — 오탐도 누락도 없어야 한다")
    check("S1 창 없는 호출을 잡는다",
          len(unscoped_aggs("def f(close):\n    x = close.max()\n", "f")), 1)
    check("S2 tail 체인은 안 잡는다",
          len(unscoped_aggs("def f(close):\n    x = close.tail(252).max()\n", "f")), 0)
    check("S3 창을 거쳐 대입된 이름은 안 잡는다",
          len(unscoped_aggs("def f(close):\n    w = close.tail(252)\n"
                            "    x = w.max()\n", "f")), 0)
    check("S4 슬라이스 대입도 안 잡는다",
          len(unscoped_aggs("def f(close):\n    p = close[close.index > d]\n"
                            "    x = p.max()\n", "f")), 0)
    check("S5 다른 함수의 위반은 안 잡는다 (스코프 한정)",
          len(unscoped_aggs("def f(close):\n    return 1\n"
                            "def g(close):\n    return close.max()\n", "f")), 0)
    check("S6 함수가 없으면 실패로 보고", len(unscoped_aggs("x = 1\n", "nope")), 1)
    check("S7 min 도 잡는다",
          len(unscoped_aggs("def f(close):\n    x = close.min()\n", "f")), 1)
    check("S8 무관한 메서드는 안 잡는다",
          len(unscoped_aggs("def f(close):\n    x = close.mean()\n", "f")), 0)
    check("S9 상수 탐지 — 없으면 None", module_const("y = 1\n", "W52_BARS"), None)
    check("S10 상수 탐지 — 있으면 값", module_const("W52_BARS = 252\n", "W52_BARS"), 252)
    check("S11 함수 안 상수는 모듈 상수가 아니다",
          module_const("def f():\n    W52_BARS = 252\n", "W52_BARS"), None)
    # ⚠️ 판별 케이스: 컬럼 선택을 창으로 오인하면 close 가 안전 목록에 들어가
    #    본체의 위반을 한 건도 못 잡는다. X4 가 이 결함을 실제로 발견했다.
    check("S12 컬럼 선택은 창이 아니다 — 위반을 여전히 잡는다",
          len(unscoped_aggs('def f(hist):\n    close = hist["Close"].dropna()\n'
                            "    x = close.max()\n", "f")), 1)
    _S_OLD = ('def _fmp_price_history(t, limit=252):\n'
              '    u = f"x/historical-price-eod/full?symbol={t}&limit={limit}"\n'
              '    return u\n')
    _S_NEW = ('def _fmp_price_history(t, lookback_days=460):\n'
              '    u = f"x/historical-price-eod/full?symbol={t}&from={a}&to={b}"\n'
              '    return u\n')
    check("S14 C6 탐지기가 limit 판을 잡는다",
          price_window_ok(_S_OLD, ast.parse(_S_OLD)), False)
    check("S15 C6 탐지기가 from/to 판을 오탐하지 않는다",
          price_window_ok(_S_NEW, ast.parse(_S_NEW)), True)
    # ⚠️ 판별 케이스: 독스트링에 limit= 이 있어도 코드가 from/to 면 통과여야 한다.
    #    실제로 이 오탐이 났다 — 전환 이력을 독스트링에 적었기 때문.
    _S_DOC = ('def _fmp_price_history(t, lookback_days=460):\n'
              '    """limit=500 을 보내도 1254봉이 왔다. 그래서 from/to 로 바꿨다."""\n'
              '    u = f"x/historical-price-eod/full?symbol={t}&from={a}&to={b}"\n'
              '    return u\n')
    check("S18 독스트링의 limit 언급은 잔재가 아니다",
          price_window_ok(_S_DOC, ast.parse(_S_DOC)), True)
    check("S16 C3 탐지기 — 표시만 하고 제외 안 하면 False",
          excludes_on_full_metrics(ast.parse(
              'def route_candidates_by_regime(x):\n'
              '    for t in x:\n'
              '        if not r.get("full_metrics"):\n'
              '            log(t)\n'), "route_candidates_by_regime"), False)
    check("S17 C3 탐지기 — 제외하면 True",
          excludes_on_full_metrics(ast.parse(
              'def route_candidates_by_regime(x):\n'
              '    for t in x:\n'
              '        if not r.get("full_metrics"):\n'
              '            out.append(t)\n'
              '            continue\n'), "route_candidates_by_regime"), True)
    check("S13 행 슬라이스는 창이다",
          len(unscoped_aggs("def f(close):\n    p = close[-252:]\n"
                            "    x = p.max()\n", "f")), 0)

    print("\n" + "=" * 74)
    if _fails:
        print(f"❌ {len(_ran)}건 중 {len(_fails)}건 실패: {', '.join(_fails)}")
        return 1
    print(f"✅ 전 항목 통과 ({len(_ran)}/{len(_ran)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
