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


def module_const(src, name):
    """모듈 최상위 정수 상수 값. 없으면 None."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, int):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return n.value.value
    return None


def load_module(src, tag):
    m = types.ModuleType(tag)
    exec(compile(src, tag, "exec"), m.__dict__)
    return m


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
