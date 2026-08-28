# -*- coding: utf-8 -*-
"""diag_hist_window.py — 가격 히스토리 조회 창(limit → from/to) 회귀 스위트.

무엇을 지키는가
───────────────
`run_watchlist_alerts._fmp_price_history` 는 `limit`(거래일, FMP 가 무시)에서
`from`/`to`(달력일, 실제로 존중됨)로 바뀌었다. 그 순간 창 크기가 **처음으로 진짜
상한**이 된다. 너무 좁으면 하류 지표가 조용히 NaN 이 되거나 더 짧은 창의 값으로
바뀐다 — 어느 쪽도 에러 로그를 남기지 않는다.

특히 위험한 두 지점:

  1) regime_core.market_warnings 의 **260봉 하드게이트**
     `if c.notna().sum() < 260: return None` → market_gate_status.available=False
     → fail-open 으로 **시장 진입 게이트가 통째로 꺼진다.** 조용히.

  2) 같은 함수의 **rolling 체인**
         vol20   = c.pct_change().rolling(20).std()
         vol_med = vol20.rolling(252, min_periods=60).median()
     마지막 행이 진짜 252창 중앙값이 되려면 272봉이 필요하다. 개별 rolling
     인자의 최대값(252)만 보면 20봉이 모자란다. 기존 diag_fmp_window.py [E] 는
     **파일 단위 과대근사**이고 독스트링에 스스로 "모듈 경계를 넘는 소비는 범위
     밖"이라 적어두었다 — 이 스위트가 그 경계를 넘는다.

어떻게 지키는가
───────────────
요구 봉수를 이 파일에 **적어두지 않는다.** regime_core / watchlist_metrics_core 의
소스를 AST 로 읽어 rolling·shift·pct_change 체인을 따라 요구 봉수를 계산하고,
하드게이트 리터럴도 소스에서 뽑는다. 상수를 복사하면 원본이 바뀌어도 통과하는
가짜 초록불이 된다. [P] 양성대조가 추출기 자체의 부패를 잡는다.

검사 묶음
─────────
  [S] 구조   — limit 제거·from/to 존재·호출부 배선 (AST, run_watchlist_alerts)
  [R] 요구치 — 크로스 모듈 요구 봉수 역산 후 창이 덮는지 (AST, regime_core 외)
  [P] 양성대조 — 추출기가 알려진 체인에서 정답을 내는지
  [T] 단위   — 달력일 ↔ 봉 변환기
  [H] 보유창 — _hist_days_for_holding 경계 표
  [G] 런타임 — 실제 URL 문자열과 파싱 계약 (fmp_get_ex 스텁)
  [C] 캐시   — 조회 깊이 기록과 FMP 콜 수 보존
  [A] app.py — historical-price-eod 창 + **시그니처 결합** (정적 AST, 임포트 없음)
  [W] 충분성 — app.py 각 창이 하류 요구 봉수를 덮는지 (크로스 모듈 역산)
  [X] SSOT   — 0.6871·환산기·보유창이 fmp_extras 한 곳에만 존재하는지
  [D] DRG    — run_drg_predict 의 창·정렬·봉수 충족 (런타임, 스텁 FMP)

⚠️ [A][W] 가 app.py 를 **임포트하지 않고 소스 AST 만 읽는 이유**: app.py 는
   streamlit 위에서만 임포트되고 최상단부터 UI 를 그린다. 임포트하는 순간 이
   스위트는 시트·FMP·Gemini 를 건드리게 된다. 정적 분석이 유일하게 안전한 방법이고,
   [A4] 시그니처 결합은 정적으로도 충분히 잡힌다(2026-08-28 실증: 18/18 검출).

안전성: 네트워크 0 · FMP 콜 0 · 시트 접근 0 · 메일 0 · 파일 쓰기 0.

사용법:
  FMP_API_KEY=x GSPREAD_KEY='{}' GMAIL_USER=a GMAIL_APP_PASSWORD=b \\
  GMAIL_TO=c python3 diag_hist_window.py
"""
import os
import sys
import ast

# GOOGLE_API_KEY·FRED_API_KEY 는 run_drg_predict 가 **모듈 최상단에서**
# os.environ[...] 로 읽는다(get 이 아니다) — 없으면 임포트가 KeyError 로 죽는다.
for _k, _v in (("FMP_API_KEY", "x"), ("GSPREAD_KEY", "{}"), ("GMAIL_USER", "a"),
               ("GMAIL_APP_PASSWORD", "b"), ("GMAIL_TO", "c"),
               ("GOOGLE_API_KEY", "x"), ("FRED_API_KEY", "x")):
    os.environ.setdefault(_k, _v)

from datetime import datetime, timedelta

import pandas as pd

import run_watchlist_alerts as m
import run_drg_predict as drg
import fmp_http as fh
import regime_core as rc
import watchlist_metrics_core as wm
import fmp_extras as fx

PASS, FAIL = [], []

def _find_repo_root(start: str) -> str:
    """app.py 가 있는 디렉터리를 위로 올라가며 찾는다.

    ⚠️ `os.path.dirname(__file__)` 을 저장소 루트로 가정하면 안 된다. 이 스위트는
       `automation/` 에 있고 app.py·regime_core·fmp_extras 는 레포 루트에 있다.
       (2026-08-28 실측: 그 가정 때문에 [A] 가 FileNotFoundError 로 죽었고,
        예외가 main() 을 통째로 끊어 요약조차 안 나왔다.)
       배치가 바뀌어도 따라가도록 앵커 파일로 탐색한다.
    """
    d = os.path.abspath(start)
    for _ in range(5):
        if os.path.exists(os.path.join(d, "app.py")):
            return d
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return os.path.abspath(start)


_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = _find_repo_root(_HERE)


def src_file(name: str) -> str:
    """레포 루트의 파일 소스. 임포트하지 않는다(app.py 는 임포트 불가)."""
    with open(os.path.join(_REPO, name), encoding="utf-8") as fh:
        return fh.read()


def tree_file(name: str) -> ast.Module:
    return ast.parse(src_file(name))


def sig_from_ast(tree: ast.Module, name: str):
    """소스에서 함수 시그니처를 복원한다 — 임포트 없이 결합 검사를 하기 위해.

    기본값의 '값'은 필요 없다(결합만 본다). 있으면 None, 없으면 empty 로 둔다.
    """
    import inspect as _insp
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            a, P = n.args, __import__("inspect").Parameter
            ps, nd = [], len(a.defaults)
            for i, x in enumerate(a.args):
                ps.append(P(x.arg, P.POSITIONAL_OR_KEYWORD,
                            default=(P.empty if i < len(a.args) - nd else None)))
            if a.vararg:
                ps.append(P(a.vararg.arg, P.VAR_POSITIONAL))
            for x, dv in zip(a.kwonlyargs, a.kw_defaults):
                ps.append(P(x.arg, P.KEYWORD_ONLY,
                            default=(P.empty if dv is None else None)))
            if a.kwarg:
                ps.append(P(a.kwarg.arg, P.VAR_KEYWORD))
            return _insp.Signature(ps)
    return None


def hist_urls(tree: ast.Module):
    """historical-price-eod 를 담은 f-string 노드들.

    인접한 문자열 리터럴은 파서가 하나의 JoinedStr 로 합치므로, 여러 줄로 쪼갠
    f-string 도 한 노드로 잡힌다(그래서 줄 단위 grep 보다 정확하다).
    반환: [(lineno, 상수부 합친 문자열, [FormattedValue 노드]), ...]
    """
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.JoinedStr):
            continue
        lit = "".join(v.value for v in n.values
                      if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if "historical-price-eod" in lit:
            fvs = [v for v in n.values if isinstance(v, ast.FormattedValue)]
            out.append((n.lineno, lit, fvs))
    return out


def calls_named(tree, dotted: str):
    """`fx.hist_range_params` 같은 점 표기 호출 노드들."""
    mod, _, attr = dotted.partition(".")
    hits = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr
                and isinstance(n.func.value, ast.Name) and n.func.value.id == mod):
            hits.append(n)
    return hits


def float_literals(tree) -> set:
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, float)}


def ok(tag, msg):
    PASS.append(f"{tag} {msg}")
    print(f"  ✅ {tag} {msg}")


def bad(tag, msg):
    FAIL.append(f"{tag} {msg}")
    print(f"  ❌ {tag} {msg}")


def chk(cond, tag, msg):
    (ok if cond else bad)(tag, msg)


# ══════════════════════════════════════════════════════════════════════════════
# 공용 — 소스/AST
# ══════════════════════════════════════════════════════════════════════════════
def src_of(mod) -> str:
    with open(mod.__file__, encoding="utf-8") as f:
        return f.read()


def tree_of(mod) -> ast.Module:
    return ast.parse(src_of(mod))


def find_func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _int_arg(call, pos: int, kw: str, consts: dict | None = None):
    """rolling(N) / rolling(window=N) 에서 정수를 뽑는다.

    리터럴뿐 아니라 **모듈 상수 이름**(`close.tail(W52_BARS)`)도 해석한다.
    이름을 못 읽으면 그 창은 0으로 취급돼 요구치가 과소평가되기 때문이다.
    """
    consts = consts or {}

    def val(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return int(node.value)
        if isinstance(node, ast.Name) and isinstance(consts.get(node.id), int):
            return int(consts[node.id])
        return None

    if len(call.args) > pos:
        v = val(call.args[pos])
        if v is not None:
            return v
    for k in call.keywords:
        if k.arg == kw:
            v = val(k.value)
            if v is not None:
                return v
    return None


def module_ints(tree: ast.Module) -> dict:
    """모듈 최상위의 정수 상수 전체 (창 인자 이름 해석용)."""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, int):
            out[n.targets[0].id] = int(n.value.value)
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.Name):
            # MIN_BARS_FULL = W52_BARS 같은 별칭
            v = out.get(n.value.id)
            if isinstance(v, int):
                out[n.targets[0].id] = v
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 요구 봉수 추출기 — rolling/shift/pct_change 체인을 따라간다
# ══════════════════════════════════════════════════════════════════════════════
# 반환값은 "첫 유효 인덱스"(0-base). 필요한 봉 수 = 그 값 + 1.
#
# ⚠️ min_periods 는 일부러 무시한다. min_periods 는 값이 **나오게** 할 뿐이고,
#    그때 나오는 값은 더 짧은 창의 값이다 — 우리가 막으려는 바로 그 침묵의
#    오답이다. 여기서 재는 것은 '전체 창이 채워지는 시점'이다.
_ZERO_OFFSET_METHODS = {"std", "mean", "median", "max", "min", "sum", "var",
                        "astype", "dropna", "notna", "isna", "fillna", "mask",
                        "to_numpy", "reset_index", "abs", "diff_noop"}


def offset_of(node, env: dict, consts: dict | None = None) -> int:
    consts = consts or {}
    if isinstance(node, ast.Name):
        return int(env.get(node.id, 0))
    if isinstance(node, ast.Constant):
        return 0
    if isinstance(node, ast.BinOp):
        return max(offset_of(node.left, env, consts),
                   offset_of(node.right, env, consts))
    if isinstance(node, ast.UnaryOp):
        return offset_of(node.operand, env, consts)
    if isinstance(node, ast.IfExp):
        # `float(x.rolling(200).mean().iloc[-1]) if len(c) >= 200 else np.nan`
        # 형태를 놓치면 요구치가 통째로 0 이 된다(실제로 compute_metrics 가 그랬다).
        return max(offset_of(node.body, env, consts),
                   offset_of(node.orelse, env, consts),
                   offset_of(node.test, env, consts))
    if isinstance(node, ast.Compare):
        return max([offset_of(node.left, env, consts)]
                   + [offset_of(c, env, consts) for c in node.comparators])
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Attribute):
            base = offset_of(fn.value, env, consts)
            name = fn.attr
            if name == "rolling":
                w = _int_arg(node, 0, "window", consts)
                return base + (w - 1 if w else 0)
            if name == "shift":
                w = _int_arg(node, 0, "periods", consts)
                return base + (w if w else 0)
            if name == "pct_change":
                w = _int_arg(node, 0, "periods", consts)
                return base + (w if w else 1)
            if name in ("tail", "head"):
                w = _int_arg(node, 0, "n", consts)
                return max(base, (w - 1) if w else 0)
            if name in ("ewm",):
                w = _int_arg(node, 0, "span", consts)
                return base + (w - 1 if w else 0)
            if name in _ZERO_OFFSET_METHODS:
                return base
            return base
        return max([offset_of(a, env, consts) for a in node.args] or [0])
    if isinstance(node, ast.Attribute):
        return offset_of(node.value, env, consts)
    if isinstance(node, ast.Subscript):
        return offset_of(node.value, env, consts)
    return 0


def required_bars_in(func: ast.AST, consts: dict | None = None) -> int:
    """함수 본문의 시리즈 대입을 따라가 필요한 최대 봉 수를 구한다."""
    env, worst = {}, 1
    for stmt in ast.walk(func):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name):
            off = offset_of(stmt.value, env, consts)
            env[stmt.targets[0].id] = off
            worst = max(worst, off + 1)
        elif isinstance(stmt, (ast.Return, ast.Expr)) and stmt.value is not None:
            worst = max(worst, offset_of(stmt.value, env, consts) + 1)
    return worst


def hard_gate_in(func: ast.AST) -> int:
    """`... .sum() < N` 형태의 하드게이트 리터럴을 소스에서 뽑는다."""
    best = 0
    for n in ast.walk(func):
        if isinstance(n, ast.Compare) and len(n.ops) == 1 \
                and isinstance(n.ops[0], (ast.Lt, ast.LtE)) \
                and isinstance(n.comparators[0], ast.Constant) \
                and isinstance(n.comparators[0].value, int) \
                and n.comparators[0].value >= 50:
            best = max(best, int(n.comparators[0].value))
    return best


def window_consts_in(tree: ast.Module) -> dict:
    """모듈 상단의 창 길이 상수 (이름에 BARS/LOOKBACK/WINDOW 포함, 값 >= 50)."""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, int) and n.value.value >= 50:
            nm = n.targets[0].id
            if any(t in nm.upper() for t in ("BARS", "LOOKBACK", "WINDOW")):
                out[nm] = int(n.value.value)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# [S] 구조 — 단위가 섞여 돌아오는 경로를 막는다
# ══════════════════════════════════════════════════════════════════════════════
def group_S():
    print("\n[S] 구조 — limit 제거 · from/to 존재 · 호출부 배선")
    t = tree_of(m)
    src = src_of(m)

    f = find_func(t, "_fmp_price_history")
    chk(f is not None, "S0", "_fmp_price_history 존재")
    if f is None:
        return

    names = [a.arg for a in f.args.args]
    kwonly = [a.arg for a in f.args.kwonlyargs]
    chk("limit" not in names + kwonly, "S1",
        f"limit 파라미터 제거됨 (현재 인자: {names + kwonly})")
    chk("calendar_days" in kwonly, "S2",
        "calendar_days 는 키워드 전용 — 위치 인자로 옛 단위가 못 들어온다")

    lit = "".join(v.value for v in ast.walk(f)
                  if isinstance(v, ast.Constant) and isinstance(v.value, str))
    chk("from=" in lit, "S3a", "요청 URL 에 from= 존재")
    chk("to=" in lit, "S3b", "요청 URL 에 to= 존재")
    chk("limit=" not in lit, "S3c", "요청 URL 에 limit= 부재")

    pos_abuse = [n.lineno for n in ast.walk(t)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_fmp_price_history" and len(n.args) > 1]
    chk(not pos_abuse, "S4",
        f"_fmp_price_history 위치 인자 2개 이상 호출 없음 {pos_abuse or ''}")

    # 주석에는 남아 있다(왜 폐기했는지가 기록이다). 코드 심볼로만 없으면 된다.
    live = [n for n in ast.walk(t)
            if isinstance(n, ast.Name) and n.id == "_PF_HIST_LIMIT"]
    chk(not live, "S5", f"_PF_HIST_LIMIT 코드 심볼 제거 (주석 언급은 허용) "
                        f"{[n.lineno for n in live] or ''}")
    chk("_PF_HIST_LIMIT" in src, "S5b",
        "폐기 사유가 주석으로 남아 있다 — 같은 상수가 되살아나는 것을 막는다")

    pf_calls = [n for n in ast.walk(t)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_pf_hist"]
    chk(len(pf_calls) >= 2, "S6a", f"_pf_hist 호출부 {len(pf_calls)}곳 발견")
    thin = [n.lineno for n in pf_calls if len(n.args) + len(n.keywords) < 4]
    chk(not thin, "S6b",
        f"모든 _pf_hist 호출이 date_added·today 를 넘긴다 {thin or ''}")

    chk(any(isinstance(n, ast.ImportFrom) and n.module == "datetime"
            and any(a.name == "timedelta" for a in n.names) for n in ast.walk(t)),
        "S7", "timedelta 임포트 존재")


# ══════════════════════════════════════════════════════════════════════════════
# [R] 요구치 — 모듈 경계를 넘는 하류 소비처에서 역산
# ══════════════════════════════════════════════════════════════════════════════
def group_R():
    print("\n[R] 요구치 — 크로스 모듈 역산 후 창이 덮는지")
    rt, wt = tree_of(rc), tree_of(wm)
    rconst, wconst = module_ints(rt), module_ints(wt)

    mw = find_func(rt, "market_warnings")
    chk(mw is not None, "R0", "regime_core.market_warnings 존재")
    if mw is None:
        return

    chain = required_bars_in(mw, rconst)
    gate = hard_gate_in(mw)
    print(f"      · market_warnings 체인 요구 = {chain}봉 · 하드게이트 = {gate}봉")
    chk(chain > 252, "R1",
        f"체인 합성이 개별 rolling 최대값(252)보다 크게 잡힘 → {chain}봉")
    chk(gate >= 200, "R2", f"하드게이트 리터럴 추출 성공 → {gate}봉")

    consts = {}
    consts.update(window_consts_in(rt))
    consts.update(window_consts_in(wt))
    print(f"      · 창 상수: {consts}")

    fn_reqs = {}
    for fname in ("classify_regime", "evaluate_timing", "compute_exit_signals",
                  "position_sell_verdict", "compute_position_drawdown",
                  "build_watchlist_plan"):
        fnode = find_func(rt, fname)
        if fnode is not None:
            fn_reqs[fname] = required_bars_in(fnode, rconst)
    cm = find_func(wt, "compute_metrics")
    if cm is not None:
        fn_reqs["compute_metrics"] = required_bars_in(cm, wconst)
    print(f"      · 함수별 체인 요구: {fn_reqs}")

    # +1: watchlist_metrics_core.completed_bars_only 가 당일 봉 1개를 절삭한다.
    required = max([chain, gate] + list(consts.values()) + list(fn_reqs.values())) + 1
    have = m._bars_for_calendar_days(m._HIST_WINDOW_DAYS)
    print(f"      · 최종 요구 {required}봉  vs  창 {m._HIST_WINDOW_DAYS}달력일 "
          f"= 약 {have}봉")

    chk(have >= required, "R3",
        f"기본 창이 요구 봉수를 덮는다 ({have} >= {required})")
    chk(have >= int(gate * 1.10), "R4",
        f"하드게이트 대비 10% 이상 여유 ({have} >= {int(gate * 1.10)})")
    chk(m._HIST_MAX_DAYS >= m._HIST_WINDOW_DAYS, "R5",
        f"상한({m._HIST_MAX_DAYS})이 기본 창({m._HIST_WINDOW_DAYS}) 이상")
    chk(m._bars_for_calendar_days(m._HIST_MAX_DAYS) >= 1200, "R6",
        f"상한이 limit 무효 시절 실측(약 1,254봉)에 준한다 "
        f"→ {m._bars_for_calendar_days(m._HIST_MAX_DAYS)}봉")


# ══════════════════════════════════════════════════════════════════════════════
# [P] 양성대조 — 추출기가 부패하면 [R] 이 무의미해진다
# ══════════════════════════════════════════════════════════════════════════════
_FIXTURE = """
def f(c):
    a = c.rolling(200, min_periods=200).mean()
    b = c / c.shift(20) - 1.0
    d = c / c.rolling(252, min_periods=60).max() - 1.0
    v = c.pct_change().rolling(20).std()
    vm = v.rolling(252, min_periods=60).median()
    if c.notna().sum() < 260:
        return None
"""


def group_P():
    print("\n[P] 양성대조 — 알려진 체인에서 추출기가 정답을 내는가")
    ftree = ast.parse(_FIXTURE)
    f = find_func(ftree, "f")
    fconst = module_ints(ftree)
    env = {}
    body = {}
    for stmt in ast.walk(f):
        if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name):
            off = offset_of(stmt.value, env, fconst)
            env[stmt.targets[0].id] = off
            body[stmt.targets[0].id] = off + 1

    for var, want in (("a", 200), ("b", 21), ("d", 252), ("v", 21), ("vm", 272)):
        chk(body.get(var) == want, f"P-{var}",
            f"{var} 요구 {body.get(var)}봉 (기대 {want})")
    chk(required_bars_in(f, fconst) == 272, "P1",
        f"함수 전체 요구 {required_bars_in(f, fconst)}봉 (기대 272 — 체인 합성)")
    chk(hard_gate_in(f) == 260, "P2",
        f"하드게이트 추출 {hard_gate_in(f)} (기대 260)")

    # 음성대조: min_periods 만 있고 rolling 이 없으면 요구가 커지면 안 된다.
    g = find_func(ast.parse("def g(c):\n    x = c.mean()\n"), "g")
    chk(required_bars_in(g) == 1, "P3",
        f"롤링 없는 함수는 1봉 요구 (실제 {required_bars_in(g)})")


# ══════════════════════════════════════════════════════════════════════════════
# [T] 단위 변환
# ══════════════════════════════════════════════════════════════════════════════
def group_T():
    print("\n[T] 단위 — 달력일 ↔ 봉")
    chk(0.60 < m._HIST_TD_PER_CD < 0.75, "T1",
        f"비율이 실측 범위 안 ({m._HIST_TD_PER_CD})")
    b460 = m._bars_for_calendar_days(460)
    chk(310 <= b460 <= 322, "T2",
        f"460달력일 → {b460}봉 (2026-08-28 실측 316봉과 정합)")
    d272 = m._calendar_days_for_bars(272)
    chk(390 <= d272 <= 400, "T3", f"272봉 → {d272}달력일")
    chk(m._bars_for_calendar_days(d272) >= 272, "T4",
        "왕복 변환이 요구 봉수를 밑돌지 않는다(올림 방향)")
    chk(m._bars_for_calendar_days(100) < m._bars_for_calendar_days(200), "T5",
        "변환기 단조 증가")
    chk(m._HIST_WINDOW_DAYS > m._calendar_days_for_bars(272), "T6",
        f"기본 창({m._HIST_WINDOW_DAYS})이 무마진 최소치({d272})보다 크다 — 마진 존재")


    # ── T7~T9 — hist_days_for_bars (요구 봉수 → 창, 마진·하한 포함) ────────
    # calendar_days_for_bars 는 **순수 환산**이라 작은 요구에서 위험하다:
    # 2봉 → 3달력일. 금요일 휴장이면 목→월 간격만 4달력일이라 0봉이 온다.
    # hist_days_for_bars 가 그 함정을 막는 정책 계층이다.
    tbl = [(1, 21), (2, 21), (6, 21), (20, 37), (30, 51), (272, 404)]
    bad7 = [(b, fx.hist_days_for_bars(b), w)
            for b, w in tbl if fx.hist_days_for_bars(b) != w]
    chk(not bad7, "T7a", f"요구 봉수 → 창 경계표 6종 {bad7 or ''}")
    chk(fx.hist_days_for_bars(10 ** 6) == fx.HIST_MAX_DAYS, "T7b",
        "거대 요구는 상한으로 클램프")
    chk(fx.hist_days_for_bars("깨진값") == fx.HIST_MAX_DAYS, "T7c",
        "요구치를 못 읽으면 최대 창 — 짧게 조용히 틀리는 것보다 낫다")

    # T8 — 마진이 실제로 존재하는가(무마진 환산보다 항상 넓다).
    #   이 검사가 없으면 pad_bars 를 0 으로 바꿔도 초록불이 난다.
    nomargin = [b for b in (1, 2, 6, 20, 30, 100, 272)
                if fx.hist_days_for_bars(b) <= fx.calendar_days_for_bars(b)]
    chk(not nomargin, "T8", f"모든 요구치에서 무마진 환산보다 넓다 {nomargin or ''}")

    # T9 — 하한이 '최장 비상 휴장 + 최대 소요'를 견디는가.
    #   2001-09-11 은 7달력일 연속 휴장이었다. 그 7일이 창에 통째로 들어와도
    #   6봉(이 저장소의 소형 요구 중 최대)이 남아야 한다.
    _EMERGENCY_CLOSURE_CD = 7
    survive = fx.bars_for_calendar_days(fx.HIST_MIN_DAYS - _EMERGENCY_CLOSURE_CD)
    print(f"      · 하한 {fx.HIST_MIN_DAYS}일 − 비상휴장 {_EMERGENCY_CLOSURE_CD}일 "
          f"→ 잔여 약 {survive}봉")
    chk(survive >= 6, "T9",
        f"하한이 7일 연속 휴장을 견딘다(잔여 {survive}봉 ≥ 6)")


# ══════════════════════════════════════════════════════════════════════════════
# [H] 보유 창
# ══════════════════════════════════════════════════════════════════════════════
def group_H():
    print("\n[H] 보유 창 — _hist_days_for_holding 경계표")
    TODAY = "2026-08-28"
    t = pd.Timestamp(TODAY)
    B, MX = m._HIST_WINDOW_DAYS, m._HIST_MAX_DAYS

    def days_ago(n):
        return (t - pd.Timedelta(days=n)).strftime("%Y-%m-%d")

    cases = [
        ("H1", "", B, "Date_Added 없음 → 기본 창(하류 252봉 폴백 경로)"),
        ("H2", None, B, "None → 기본 창"),
        ("H3", days_ago(5), B + 5, "최근 매수 → 기본 + 보유일"),
        ("H4", days_ago(30), B + 30, "1개월 보유"),
        ("H5", days_ago(730), B + 730, "2년 보유"),
        ("H6", days_ago(2200), MX, "5년 초과 → 상한"),
        ("H7", days_ago(MX - B), MX, "상한 경계 정확히"),
        ("H8", "not-a-date", MX, "파싱 실패 → 최대 창(짧게 틀리는 것보다 낫다)"),
        ("H9", days_ago(-10), B, "미래 날짜 → 기본 창"),
        ("H10", TODAY, B, "당일 매수 → 기본 창"),
    ]
    for tag, da, want, why in cases:
        got = m._hist_days_for_holding(da, TODAY, ticker="TEST")
        chk(got == want, tag, f"{why} → {got} (기대 {want})")

    chk(m._hist_days_for_holding(days_ago(30), TODAY)
        > m._hist_days_for_holding(days_ago(5), TODAY), "H11",
        "오래 보유할수록 창이 넓어진다(단조)")
    deep = m._hist_days_for_holding(days_ago(730), TODAY)
    chk(m._bars_for_calendar_days(deep) >= m._bars_for_calendar_days(B) + 730 * 0.6,
        "H12", "2년 보유 창이 매수일 이전 구간까지 실제로 덮는다")


# ══════════════════════════════════════════════════════════════════════════════
# [G] 런타임 — 실제 URL 과 파싱 계약
# ══════════════════════════════════════════════════════════════════════════════
class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _payload(n=30):
    end = datetime(2026, 8, 28)
    rows = []
    for i in range(n):
        d = end - timedelta(days=i)
        rows.append({"date": d.strftime("%Y-%m-%d"), "open": 10.0 + i,
                     "high": 11.0 + i, "low": 9.0 + i, "close": 10.5 + i,
                     "volume": 1000 + i})
    return rows


class Stub:
    """fmp_get_ex 를 가로채 URL 을 기록한다. 네트워크 접근 없음."""

    def __init__(self, kind="ok", payload=None):
        self.urls = []
        self.kind = kind
        self.payload = _payload() if payload is None else payload
        self._orig = None

    def __enter__(self):
        self._orig = m.fh.fmp_get_ex

        def fake(url, timeout=None, **kw):
            self.urls.append(url)
            if self.kind != "ok":
                return None, 429, self.kind
            return FakeResp(self.payload), 200, "ok"

        m.fh.fmp_get_ex = fake
        return self

    def __exit__(self, *a):
        m.fh.fmp_get_ex = self._orig
        return False


def group_G():
    print("\n[G] 런타임 — URL 문자열과 파싱 계약")
    today = datetime.now(m._ET).date()

    with Stub() as s:
        df = m._fmp_price_history("AAPL")
    chk(len(s.urls) == 1, "G0", f"콜 1회 (실제 {len(s.urls)})")
    u = s.urls[0] if s.urls else ""
    chk("&limit=" not in u, "G1", "URL 에 limit 파라미터 없음")
    exp_from = (today - timedelta(days=m._HIST_WINDOW_DAYS)).strftime("%Y-%m-%d")
    exp_to = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    chk(f"from={exp_from}" in u, "G2", f"from={exp_from} (오늘 − 기본 창)")
    chk(f"to={exp_to}" in u, "G3", f"to={exp_to} (오늘 + 1일 — 경계 방어)")
    chk("symbol=AAPL" in u, "G4", "symbol 파라미터 유지")
    chk("/stable/" in u, "G5", "FMP /stable/ 경로 유지")

    with Stub() as s:
        m._fmp_price_history("MSFT", calendar_days=m._HIST_MAX_DAYS)
    exp_deep = (today - timedelta(days=m._HIST_MAX_DAYS)).strftime("%Y-%m-%d")
    chk(f"from={exp_deep}" in (s.urls[0] if s.urls else ""), "G6",
        f"깊은 창 요청이 from 에 반영 ({exp_deep})")

    chk(list(df.columns) == ["Open", "High", "Low", "Close", "Volume"], "G7",
        f"열 계약 불변 (실제 {list(df.columns)})")
    chk(df.index.is_monotonic_increasing, "G8", "오름차순 정렬 유지")
    chk(len(df) == 30, "G9", f"응답 행 수 보존 (실제 {len(df)})")

    with Stub(kind="ratelimit") as s:
        df2 = m._fmp_price_history("NVDA")
    chk(df2 is not None and df2.empty, "G10", "실패 응답 → 빈 DataFrame(계약 불변)")

    # 옛 단위 호출은 구조적으로 막혀 있어야 한다.
    try:
        m._fmp_price_history("AAPL", 600)
        bad("G11", "위치 인자 600 이 통과했다 — 키워드 전용 방어 실패")
    except TypeError:
        ok("G11", "위치 인자 호출은 TypeError (옛 단위 유입 차단)")


# ══════════════════════════════════════════════════════════════════════════════
# [C] 캐시 깊이 — 콜 수 보존과 재조회 조건
# ══════════════════════════════════════════════════════════════════════════════
def group_C():
    print("\n[C] 캐시 — 조회 깊이 기록과 FMP 콜 수")
    TODAY = "2026-08-28"
    t = pd.Timestamp(TODAY)

    def days_ago(n):
        return (t - pd.Timedelta(days=n)).strftime("%Y-%m-%d")

    cache = {}
    with Stub() as s:
        m._pf_hist("AAA", cache, "", TODAY)
        n1 = len(s.urls)
        m._pf_hist("AAA", cache, "", TODAY)
        n2 = len(s.urls)
        m._pf_hist("AAA", cache, days_ago(730), TODAY)
        n3 = len(s.urls)
        m._pf_hist("AAA", cache, days_ago(5), TODAY)
        n4 = len(s.urls)
    chk(n1 == 1, "C1", f"최초 조회 1콜 (실제 {n1})")
    chk(n2 == 1, "C2", f"같은 깊이 재요청은 추가 콜 없음 (실제 {n2})")
    chk(n3 == 2, "C3", f"더 깊은 요구는 1회 재조회 (실제 {n3})")
    chk(n4 == 3 - 1, "C4", f"더 얕은 요구는 추가 콜 없음 (실제 {n4})")

    # 워치리스트 루프가 기본 창으로 먼저 채운 캐시를 깊은 요구가 덮는가
    cache2 = {}
    with Stub() as s0:
        cache2["BBB"] = m._fmp_price_history("BBB")
    with Stub() as s:
        m._pf_hist("BBB", cache2, "", TODAY)
        n_shallow = len(s.urls)
        m._pf_hist("BBB", cache2, days_ago(900), TODAY)
        n_deep = len(s.urls)
    chk(n_shallow == 0, "C5",
        "얕은 캐시가 이미 기본 창이면 재조회하지 않는다(콜 절약)")
    chk(n_deep == 1, "C6", f"깊은 요구가 오면 덮어쓴다 (실제 {n_deep}콜)")

    # 조회 실패는 같은 실행 안에서 재시도하지 않는다(옛 동작 보존)
    cache3 = {}
    with Stub(kind="timeout") as s:
        r1 = m._pf_hist("CCC", cache3, "", TODAY)
        r2 = m._pf_hist("CCC", cache3, "", TODAY)
        n_fail = len(s.urls)
    chk(n_fail == 1, "C7", f"실패 후 같은 깊이 재시도 없음 (실제 {n_fail}콜)")
    chk(r1 is None and r2 is None, "C8", "실패 시 None 반환(호출부 계약 불변)")

    chk(isinstance(cache.get(m._DEEP_KEY), dict), "C9",
        "깊이 기록이 dict (예전 set 이면 깊이 비교가 불가능하다)")


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# [A] app.py — 조회 창 구조 + 시그니처 결합 (정적 AST · 임포트 없음)
# ══════════════════════════════════════════════════════════════════════════════
#   app.py 유예 호출부 기준선. **'괜찮다'가 아니라 '알고 있고 아직 안 고쳤다'**는 뜻.
#     · cached_earnings_price_history(limit=900) — run_earnings_watch.py:242 와
#       락스텝 쌍이다. 한쪽만 from/to 로 바꾸면 실적 갭 표본 수가 갈려서 앱 화면과
#       이메일의 예상 변동폭이 달라진다. 같은 커밋에서 함께 전환할 것(백로그).
_APP_LIMIT_BASELINE = 1


def group_A():
    print("\n[A] app.py — historical-price-eod 창 · 시그니처 결합")

    # A-0: 경로 해석을 **검사 항목으로** 만든다. 예외로 죽으면 main() 이 끊겨
    #      나머지 검사군의 결과조차 못 본다(2026-08-28 실측). 못 찾으면 실패로
    #      기록하고 진행한다 — 조용히 건너뛰면 가짜 초록불이 된다.
    missing_src = [n for n in ("app.py", "scanner_core.py")
                   if not os.path.exists(os.path.join(_REPO, n))]
    chk(not missing_src, "A-0",
        f"레포 루트 해석 성공: {_REPO} "
        f"{'— 없는 파일 ' + str(missing_src) if missing_src else ''}")
    if missing_src:
        print(f"        · 스위트 위치: {_HERE}")
        print("        · app.py 를 앵커로 위로 5단계까지 탐색했으나 못 찾았다.")
        return

    at = tree_file("app.py")
    asrc = src_file("app.py")
    urls = hist_urls(at)

    chk(len(urls) >= 6, "A0", f"historical-price-eod URL {len(urls)}곳 발견")

    with_limit = [(ln, lit) for ln, lit, _ in urls if "limit=" in lit]
    chk(len(with_limit) <= _APP_LIMIT_BASELINE, "A1",
        f"limit= 잔존 {len(with_limit)}곳 (기준선 {_APP_LIMIT_BASELINE}) "
        f"{[ln for ln, _ in with_limit]}")
    if len(with_limit) < _APP_LIMIT_BASELINE:
        print("      ⚠️ 기준선보다 줄었다 — _APP_LIMIT_BASELINE 을 낮출 것")
    chk(all("limit=900" in lit for _, lit in with_limit), "A1b",
        "유예된 곳은 실적 심층 캐시(limit=900) 뿐이다 "
        f"{[lit[-40:] for _, lit in with_limit] or ''}")

    # 전환된 곳은 전부 fx.hist_range_params 를 통과해야 한다.
    conv = [(ln, lit, fvs) for ln, lit, fvs in urls if "limit=" not in lit]
    missing = []
    for ln, lit, fvs in conv:
        has = any(calls_named(ast.Module(body=[ast.Expr(v.value)], type_ignores=[]),
                              "fx.hist_range_params") for v in fvs)
        if not has:
            missing.append(ln)
    chk(not missing, "A2",
        f"전환된 {len(conv)}곳 전부 fx.hist_range_params 경유 {missing or ''}")

    chk(all(("from=" not in lit and "to=" not in lit) for _, lit, _ in conv), "A2b",
        "from/to 를 호출부에서 직접 조립하지 않는다 — 조립은 fmp_extras 한 곳")

    # ── A4: 시그니처 결합. 이 스위트가 존재하는 가장 큰 이유다. ──────────────
    #   scanner_core 가 limit → lookback_days 로 바뀌었는데 app.py 호출부가
    #   하나도 안 따라와 18곳 전부 TypeError 였다(2026-08-27~28). 전부 try/except
    #   안이라 **조용히** 빈 DataFrame 이 됐고, check_freshness 정합성 검사는
    #   '심볼 존재'만 보므로 초록불이었다. 그 사각지대를 여기서 덮는다.
    sct = tree_file("scanner_core.py")
    sigs = {}
    for nm in ("_fmp_price_history", "_fmp_batch_price_history"):
        sg = sig_from_ast(sct, nm)
        if sg:
            sigs[nm] = sg
    for nm in ("_fmp_batch_to_close_df", "_wl_prefetch_histories",
               "_pf_prefetch_histories", "_fmp_price_history_robust",
               "_fmp_robust_batch_close", "_fmp_robust_batch_history_report"):
        sg = sig_from_ast(at, nm)
        if sg:
            sigs[nm] = sg
    chk(len(sigs) >= 8, "A3", f"검사 대상 함수 {len(sigs)}개 시그니처 복원")

    ok, bad = 0, []
    for n in ast.walk(at):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in sigs:
            try:
                sigs[n.func.id].bind(*[object()] * len(n.args),
                                     **{k.arg: object() for k in n.keywords if k.arg})
                ok += 1
            except TypeError as e:
                bad.append(f"{n.lineno}:{n.func.id} — {e}")
    chk(not bad, "A4", f"app.py 호출부 {ok}곳 전부 실제 시그니처에 결합")
    for b in bad:
        print("        ❌ " + b)

    # ── A5: 옛 단위가 위치 인자로 스며드는 경로 차단 ────────────────────────
    kwonly_required = ("_fmp_batch_to_close_df", "_wl_prefetch_histories",
                       "_pf_prefetch_histories", "_fmp_price_history_robust",
                       "_fmp_robust_batch_close", "_fmp_robust_batch_history_report")
    leaks = []
    for nm in kwonly_required:
        sg = sigs.get(nm)
        if sg is None:
            leaks.append(f"{nm}(없음)")
            continue
        if "limit" in sg.parameters:
            leaks.append(f"{nm}: limit 파라미터 잔존")
        cd = sg.parameters.get("calendar_days") or sg.parameters.get("date_added_map")
        if cd is None or cd.kind is not cd.KEYWORD_ONLY:
            leaks.append(f"{nm}: 창 인자가 키워드 전용이 아니다")
    chk(not leaks, "A5", f"래퍼 6개 모두 limit 제거 + 키워드 전용 {leaks or ''}")

    # 주석에는 남아 있어도 된다(왜 폐기했는지가 기록이다). **코드 심볼**로만 없으면 된다.
    live = []
    for nm in kwonly_required:
        fn = find_func(at, nm)
        if fn is None:
            continue
        live += [f"{nm}:{n.lineno}" for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and n.id == "limit"]
    chk(not live, "A5b",
        f"래퍼 6개 본문에 limit 코드 심볼 부재(주석 언급은 허용) {live or ''}")
    chk("limit" in asrc, "A5c",
        "폐기 사유가 주석으로 남아 있다 — 같은 파라미터가 되살아나는 것을 막는다")


# ══════════════════════════════════════════════════════════════════════════════
# [W] app.py 창 충분성 — 하류 요구 봉수를 실제로 덮는가
# ══════════════════════════════════════════════════════════════════════════════
def group_W():
    print("\n[W] app.py 창 충분성 — 하류 요구 역산")
    if not os.path.exists(os.path.join(_REPO, "app.py")):
        chk(False, "W-0", f"app.py 를 찾지 못했다 ({_REPO}) — [A-0] 참조")
        return
    at = tree_file("app.py")
    aconst = module_ints(at)
    rt = tree_of(rc)
    rconst = module_ints(rt)

    # W1/W2 — 기술적 분석 캐시(SPY 포함)가 market_warnings 를 먹인다
    mw = find_func(rt, "market_warnings")
    chain = required_bars_in(mw, rconst)
    gate = hard_gate_in(mw)
    have = fx.bars_for_calendar_days(fx.HIST_WINDOW_DAYS)
    print(f"      · 기본 창 {fx.HIST_WINDOW_DAYS}달력일 → 약 {have}봉 "
          f"(체인 요구 {chain} · 하드게이트 {gate})")
    chk(have >= chain, "W1", f"{have}봉 ≥ vol 체인 요구 {chain}봉")
    chk(have >= gate, "W2", f"{have}봉 ≥ 하드게이트 {gate}봉")

    # W3 — 진짜 범인은 limit 이 아니라 이 후처리였다. 부활 금지.
    ctph = find_func(at, "cached_timing_price_history")
    chk(ctph is not None, "W3a", "cached_timing_price_history 존재")
    if ctph is not None:
        offs = [n for n in ast.walk(ctph)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "DateOffset"]
        chk(not offs, "W3",
            f"1년 cutoff 후처리 부재 — 창은 from/to 만이 정한다 "
            f"{[n.lineno for n in offs] or ''}")
        lit = "".join(v for _, v, _ in hist_urls(ast.Module(body=[ctph], type_ignores=[])))
        chk("limit=" not in lit, "W3b", "요청 URL 에 limit= 부재")

    # W4 — MA200 경로(_fmp_robust_batch_close 기본 창)
    ve = find_func(at, "verify_emerging_with_quant")
    need_ma = required_bars_in(ve, aconst) if ve else 0
    bc = sig_from_ast(at, "_fmp_robust_batch_close")
    bc_days = 0
    if bc and bc.parameters.get("calendar_days") is not None:
        for n in ast.walk(at):
            if isinstance(n, ast.FunctionDef) and n.name == "_fmp_robust_batch_close":
                for x, dv in zip(n.args.kwonlyargs, n.args.kw_defaults):
                    if x.arg == "calendar_days" and isinstance(dv, ast.Constant):
                        bc_days = int(dv.value)
    bc_bars = fx.bars_for_calendar_days(bc_days)
    chk(bc_bars >= need_ma > 0, "W4",
        f"batch_close 기본 {bc_days}일(≈{bc_bars}봉) ≥ MA200 요구 {need_ma}봉")

    # W5 — RS 경로(_fmp_robust_batch_history_report 기본 창)
    ds = find_func(at, "detect_sector_momentum_reversal")
    need_rs = hard_gate_in(ds) if ds else 0
    hr_days = 0
    for n in ast.walk(at):
        if isinstance(n, ast.FunctionDef) and n.name == "_fmp_robust_batch_history_report":
            for x, dv in zip(n.args.kwonlyargs, n.args.kw_defaults):
                if x.arg == "calendar_days" and isinstance(dv, ast.Constant):
                    hr_days = int(dv.value)
    hr_bars = fx.bars_for_calendar_days(hr_days)
    chk(hr_bars >= need_rs > 0, "W5",
        f"batch_history_report 기본 {hr_days}일(≈{hr_bars}봉) ≥ RS 게이트 {need_rs}봉")

    # W6/W7 — 분모가 시리즈 길이인 곳은 tail() 로 못 박혀 있어야 한다.
    #   창을 넓히는 순간 '값'이 바뀌는 부류다. VIX 백분위와 거래량 비율이 그렇다.
    #   ⚠️ any() 로 쓰면 안 된다. 이 함수에는 **FRED 경로에도** s.tail(252) 가 있어서
    #      FMP 쪽 tail 을 지워도 any() 는 계속 참이다(2026-08-28 뮤테이션에서 실제로
    #      놓쳤다). 판별력이 있는 통계는 '개수'다 — 두 경로 각각 하나씩, 총 2개.
    vix = find_func(at, "fetch_vix_latest_and_history")
    n252 = len([a for n in (ast.walk(vix) if vix else [])
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "tail"
                for a in n.args
                if isinstance(a, ast.Constant) and a.value == 252])
    chk(n252 >= 2, "W6",
        f"tail(252) 가 FRED·FMP 두 경로에 각각 존재 (발견 {n252}개, 기대 2) — "
        f"백분위 분모가 소스에 따라 갈리지 않는다")

    css = find_func(at, "calculate_style_scores")
    tails2 = [n for n in ast.walk(css)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "tail"] if css else []
    chk(any(isinstance(a, ast.Constant) and a.value == 65
            for n in tails2 for a in n.args), "W7",
        "calculate_style_scores 에 tail(65) 고정 — vols.mean() 분모 안정")

    # W8 — 요구가 가변인 3곳은 상수 리터럴이면 안 된다.
    for fname, tag in (("_dividend_reinvest_price", "W8a"),
                       ("_pf_prefetch_histories", "W8b")):
        fn = find_func(at, fname)
        dyn = False
        if fn is not None:
            dyn = bool(calls_named(ast.Module(body=[fn], type_ignores=[]),
                                   "fx.hist_days_for_holding")
                       or calls_named(ast.Module(body=[fn], type_ignores=[]),
                                      "fx.hist_days_for_target_date"))
        chk(dyn, tag, f"{fname} 이 동적 창 헬퍼를 쓴다(상수 아님)")


# ══════════════════════════════════════════════════════════════════════════════
# [X] SSOT — 환산 상수·보유창이 한 곳에만 존재하는가
# ══════════════════════════════════════════════════════════════════════════════
def group_X():
    print("\n[X] SSOT — 0.6871 · 환산기 · 보유창의 단일 출처")
    ratio = fx.HIST_TD_PER_CD

    # X1 — 코드 리터럴 기준(주석은 허용). 복제가 드리프트의 시작이다.
    dupes = []
    for name in ("app.py", "run_watchlist_alerts.py", "scanner_core.py",
                 "automation/run_drg_predict.py"):
        try:
            if ratio in float_literals(tree_file(name)):
                dupes.append(name)
        except FileNotFoundError:
            pass
    chk(not dupes, "X1",
        f"{ratio} 코드 리터럴이 fmp_extras 밖에 없다 {dupes or ''}")
    chk(ratio in float_literals(tree_file("fmp_extras.py")), "X1b",
        "fmp_extras 에는 실제로 존재한다(검사기가 헛도는 것 방지)")

    # X2 — 재수출이 값·객체 동일성까지 유지하는가
    chk(m._HIST_TD_PER_CD == ratio, "X2a", "run_watchlist_alerts 재수출 값 동일")
    chk(m._HIST_WINDOW_DAYS == fx.HIST_WINDOW_DAYS, "X2b", "기본 창 동일")
    chk(m._HIST_MAX_DAYS == fx.HIST_MAX_DAYS, "X2c", "상한 동일")
    chk(m._bars_for_calendar_days is fx.bars_for_calendar_days, "X2d",
        "변환기가 같은 객체(사본이 아니라 재수출)")
    chk(m._hist_days_for_holding is fx.hist_days_for_holding, "X2e",
        "보유창 함수가 같은 객체 — 메일과 화면이 같은 규칙을 쓴다")

    # X3 — app.py 가 실제로 SSOT 를 임포트하는가
    if not os.path.exists(os.path.join(_REPO, "app.py")):
        chk(False, "X3-0", f"app.py 를 찾지 못했다 ({_REPO}) — [A-0] 참조")
        return
    at = tree_file("app.py")
    aliases = {a.asname or a.name for n in ast.walk(at)
               if isinstance(n, ast.Import) for a in n.names}
    chk("fx" in aliases, "X3a", "app.py 가 fmp_extras 를 fx 로 임포트")
    chk("fh" in aliases, "X3b", "app.py 가 fmp_http 를 fh 로 임포트")

    # X4 — 승격된 함수가 옛 동작을 그대로 재현하는가(경계 4종)
    T = "2026-08-27"
    cases = [("", fx.HIST_WINDOW_DAYS), ("2026-09-01", fx.HIST_WINDOW_DAYS),
             ("2026-07-28", fx.HIST_WINDOW_DAYS + 30), ("1990-01-01", fx.HIST_MAX_DAYS)]
    bad = [(d, fx.hist_days_for_holding(d, today=T), w)
           for d, w in cases if fx.hist_days_for_holding(d, today=T) != w]
    chk(not bad, "X4", f"보유창 경계 4종 재현 {bad or ''}")

    # X5 — run_drg_predict 가 창을 **직접 조립하지 않는가**.
    #   `&from=` 을 손으로 쓰기 시작하면 to=오늘+1 규약과 마진 정책이 조용히
    #   두 벌이 된다. 창 조립은 fx.hist_range_params 하나만 통과해야 한다.
    dsrc = src_file("automation/run_drg_predict.py")
    chk("&from=" not in dsrc, "X5a", "run_drg_predict 에 직접 조립한 &from= 없음")
    chk("calendar_days_for_bars" not in dsrc, "X5b",
        "순수 환산기를 직접 부르지 않는다(마진 정책 우회 차단)")
    chk("hist_days_for_bars" in dsrc, "X5c",
        "정책 변환기를 실제로 쓴다(검사기가 헛도는 것 방지)")


# ══════════════════════════════════════════════════════════════════════════════
# [D] run_drg_predict — 창·정렬·봉수 충족 (런타임)
# ══════════════════════════════════════════════════════════════════════════════
# 정적 분석만으로는 부족하다. 이 파일의 실패 모드는 전부 **조용한 값 오류**다:
#   · 창이 짧으면 `len(rows) >= 6` 가드에 걸려 신호 한 줄이 통째로 사라진다.
#   · rolling(20, min_periods=10) 은 20봉 미만이어도 **에러 없이** 더 짧은 창의
#     평균을 낸다 — below_ma20 이 조용히 틀린다.
#   · 응답 정렬을 가정하면 '직전 종가' 자리에 창에서 가장 오래된 종가가 들어간다.
# 셋 다 AST 로는 안 보인다. 스텁 FMP 를 물리고 **실제로 실행**한다.
_DRG_FAIL_MARK = "조회 실패"


def _drg_stub(order="oldest", blackout=None, log=None, supply=None):
    """요청된 from/to 창 안의 평일을 봉으로 만들어 주는 가짜 FMP.

    order    : "oldest" | "newest" — 응답 정렬. 코드가 정렬에 걸리면 둘이 갈린다.
    blackout : (시작, 끝) 달력일 구간을 휴장으로 지운다(연휴/비상휴장 모사).
    supply   : 호출 **1건당 1개** dict 를 append 할 리스트.

    ⚠️ supply 를 심볼 키 dict 로 두면 안 된다. 같은 심볼이 서로 다른 창으로
       여러 번 불린다(SPY 는 51일 창과 21일 창 양쪽에서). 심볼 키로 기록하면
       뒤 호출이 앞 호출을 덮어써서 **대장주 51일 창의 공급량을 21일 창 값으로
       착각**한다. 2026-08-28 1차 작성에서 실제로 그렇게 틀렸다.
    """
    def _f(path, timeout=None, retries=None, key=""):
        p = str(path)
        if log is not None:
            log.append(p)
        head, _, qs = p.partition("?")
        q = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv)
        if head.startswith("historical-price-eod"):
            today = datetime.now().date()
            d0 = datetime.strptime(q["from"], "%Y-%m-%d").date()
            d1 = min(datetime.strptime(q["to"], "%Y-%m-%d").date(), today)
            rows, d, px = [], d0, 100.0
            while d <= d1:
                closed = d.weekday() >= 5 or (
                    blackout and blackout[0] <= d <= blackout[1])
                if not closed:
                    rows.append({"date": d.isoformat(), "close": round(px, 2),
                                 "open": round(px, 2), "high": round(px, 2),
                                 "low": round(px, 2), "volume": 1000})
                    px += 1.0
                d += timedelta(days=1)
            if supply is not None:
                supply.append({
                    "symbol": q.get("symbol", "?"),
                    "days": (today - d0).days,
                    "bars": len(rows),
                    "last_close": rows[-1]["close"] if rows else None,
                })
            return rows if order == "oldest" else list(reversed(rows))
        if head.startswith("quote"):
            return [{"previousClose": 0}]      # → 반드시 일봉 폴백을 태운다
        if head.startswith("aftermarket-trade"):
            return [{"price": 200.0}]          # → 실시간 경로를 살려 prev 를 쓰게 한다
        if head.startswith("aftermarket-quote"):
            return []
        if head.startswith("stock-price-change"):
            return [{"symbol": "SPY", "1D": 0.5}, {"symbol": "QQQ", "1D": -0.3}]
        if head.startswith("sector-performance-snapshot"):
            return [{"sector": "Technology", "averageChange": 1.0},
                    {"sector": "Utilities", "averageChange": -0.5}]
        if head.startswith("economic-calendar"):
            return [{"country": "US", "impact": "High", "event": "CPI",
                     "date": datetime.now().strftime("%Y-%m-%d")}]
        return []
    return _f


class _FredStub:
    def get_series(self, name):
        idx = pd.date_range("2026-01-01", periods=60, freq="D")
        return pd.Series([18.0 + i * 0.05 for i in range(60)], index=idx)


def _run_macro(order="oldest", blackout=None):
    """fetch_macro_context 를 스텁 위에서 실행. (산출텍스트, 호출로그, 공급봉수)"""
    log, supply = [], []
    saved = fh.fmp_get_json
    fh.fmp_get_json = _drg_stub(order=order, blackout=blackout,
                                log=log, supply=supply)
    try:
        out = drg.fetch_macro_context(_FredStub(), [])
    finally:
        fh.fmp_get_json = saved
    return out, log, supply


def group_D():
    print("\n[D] run_drg_predict — 조회 창·정렬·봉수 (런타임 · FMP 콜 0)")

    # ── D0 — 배선과 A1 래칫 ────────────────────────────────────────────────
    dsrc = src_file("automation/run_drg_predict.py")
    dtree = ast.parse(dsrc)
    aliases = {a.asname or a.name for n in ast.walk(dtree)
               if isinstance(n, ast.Import) for a in n.names}
    chk("fh" in aliases, "D0a", "fmp_http 를 fh 로 임포트")
    chk("fx" in aliases, "D0b", "fmp_extras 를 fx 로 임포트")
    # requests 임포트가 없어야 원시 GET 이 '되살리려면 임포트부터'가 된다.
    _no_req_import = "requests" not in aliases
    _no_raw_get = "requests.get" not in dsrc
    chk(_no_req_import, "D0c",
        "requests 를 임포트하지 않는다 — 원시 FMP 호출 래칫이 잠긴다")
    chk(_no_raw_get, "D0d", "소스에 requests.get 이 0곳")

    # D0e/D0f — `_fmp_hist(sym, bars)` 는 기본값을 가지면 안 된다.
    #   소비처 요구가 서로 다르다(신용 스프레드 6봉 · 대장주 20봉). 기본값이
    #   있으면 호출부가 자기 요구를 말하지 않아도 통과하고, **어느 요구인지 모르는
    #   창**이 생긴다. 기본값 부활은 그 자체로는 동작을 안 바꾸므로 런타임 검사에
    #   안 잡힌다 — 정적으로 못 박는다.
    _fd = find_func(dtree, "_fmp_hist")
    if _fd is None:
        chk(False, "D0e-0", "_fmp_hist 정의를 찾지 못했다")
    else:
        chk(not _fd.args.defaults and not _fd.args.kw_defaults, "D0e",
            "_fmp_hist 인자에 기본값이 없다 — 호출부가 요구 봉수를 반드시 밝힌다")
        _calls = [n for n in ast.walk(dtree)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == "_fmp_hist"]
        _naked = [n.lineno for n in _calls
                  if len(n.args) < 2 and not any(k.arg == "bars" for k in n.keywords)]
        chk(_calls and not _naked, "D0f",
            f"_fmp_hist 호출 {len(_calls)}곳 전부가 bars 를 명시 {_naked or ''}")

    # ⚠️ 여기서 반드시 멈춘다. 원시 requests.get 이 남아 있으면 fh 스텁이 그
    #    경로를 **가로채지 못하고**, 아래 런타임 검사가 진짜 FMP 로 나간다.
    #    이 스위트는 '네트워크 0'을 약속한다 — 약속을 지키려면 조기 종료해야 한다.
    #    (역검증에서 실제로 확인: 패치 전 파일로 돌리면 여기서 걸린다.)
    if not (_no_req_import and _no_raw_get):
        chk(False, "D0-STOP",
            "원시 requests 경로 잔존 — 런타임 검사를 건너뛴다(네트워크 보호)")
        return

    # ── D1 — URL 계약 ──────────────────────────────────────────────────────
    out, log, supply = _run_macro()
    hist = [p for p in log if p.startswith("historical-price-eod")]
    chk(len(hist) >= 5, "D1a", f"일봉 호출 관측 {len(hist)}건")
    chk(all("&from=" in p and "&to=" in p for p in hist), "D1b",
        "일봉 호출 전건에 from/to 존재")
    chk(not any("limit=" in p for p in log), "D1c",
        f"전 호출 URL 에 limit= 부재 ({len(log)}건)")
    tom = (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")
    chk(all(f"&to={tom}" in p for p in hist), "D1d",
        f"to 가 오늘+1 ({tom}) — hist_range_params 규약")

    # ── D2 — 창 폭이 정책 변환기 산출과 정확히 일치 ────────────────────────
    #   호출부가 fx 를 우회해 제 손으로 달력일을 정하면 여기서 갈린다.
    allowed = {fx.hist_days_for_bars(1), fx.hist_days_for_bars(2),
               fx.hist_days_for_bars(6), fx.hist_days_for_bars(30)}
    widths = set()
    for p in hist:
        q = dict(kv.split("=", 1) for kv in p.partition("?")[2].split("&") if "=" in kv)
        f = datetime.strptime(q["from"], "%Y-%m-%d").date()
        widths.add((datetime.now().date() - f).days)
    chk(widths <= allowed, "D2a",
        f"관측 창 {sorted(widths)} ⊆ 정책 산출 {sorted(allowed)}")
    chk(max(widths) == fx.hist_days_for_bars(30), "D2b",
        f"최대 창이 대장주 요구(30봉={fx.hist_days_for_bars(30)}일)와 일치")

    # ── D3 — 봉수 충족: 각 창이 **그 창이 존재하는 이유**를 덮는가 ─────────
    #   자기참조를 피하려고 요구치를 창에서 되읽지 않는다. 아래 표는 소스의
    #   소비 깊이(rolling(20) · iloc[-6] · 직전봉 · 최신봉)에서 직접 온 값이다.
    #   창이 여러 요구를 공유하면(21일 창은 1·2·6봉 공용) 최댓값으로 조인다.
    need = {}
    for _req_bars, _consume, _why in ((30, 20, "_fmp_hist rolling(20)"),
                                      (6, 6, "안전자산·SPY비교 iloc[-6]"),
                                      (2, 2, "직전 종가 폴백"),
                                      (1, 1, "SPY 현재가")):
        _w = fx.hist_days_for_bars(_req_bars)
        need[_w] = max(need.get(_w, 0), _consume)
    print(f"      · 창별 최소 요구 봉수: {dict(sorted(need.items()))}")

    def _shortfall(sup):
        return [(e["symbol"], e["days"], e["bars"], need.get(e["days"]))
                for e in sup if e["bars"] < need.get(e["days"], 0)]

    print(f"      · 공급 봉수: "
          f"{sorted({(e['days'], e['bars']) for e in supply})} (달력일, 봉)")
    chk(not _shortfall(supply), "D3a",
        f"전 호출이 자기 창의 요구 봉수를 덮는다 {_shortfall(supply) or ''}")
    chk(any(e["days"] == fx.hist_days_for_bars(30) for e in supply), "D3b",
        "대장주 51일 창 호출이 실제로 관측된다(검사기가 헛도는 것 방지)")
    chk(_DRG_FAIL_MARK not in out, "D3c",
        "정상 창에서 '조회 실패' 문구가 하나도 없다")

    # ── D4 — 최악 휴장: 7달력일 연속 휴장을 견디는가 ───────────────────────
    #   HIST_MIN_DAYS 의 존재 이유를 직접 검증한다. 하한을 지우면 여기서 깨진다.
    _t = datetime.now().date()
    out_bo, _, sup_bo = _run_macro(blackout=(_t - timedelta(days=8),
                                             _t - timedelta(days=2)))
    print(f"      · 7일 휴장 시 공급: "
          f"{sorted({(e['days'], e['bars']) for e in sup_bo})}")
    chk(_DRG_FAIL_MARK not in out_bo, "D4a",
        "7달력일 연속 휴장에도 신호가 사라지지 않는다")
    chk(not _shortfall(sup_bo), "D4b",
        f"7일 휴장에도 전 창이 요구를 덮는다 {_shortfall(sup_bo) or ''}")

    # ── D5 — 정렬 무의존성 ────────────────────────────────────────────────
    #   FMP 는 지금 newest-first 로 준다. 그 순서를 코드가 **가정하면**
    #   순서가 바뀌는 날 '직전 종가' 자리에 창의 가장 오래된 종가가 들어가고
    #   에러는 나지 않는다. 두 정렬에서 산출이 동일해야 한다.
    out_new, _, _ = _run_macro(order="newest")
    chk(out == out_new, "D5",
        "oldest-first / newest-first 응답에서 산출 텍스트 동일")
    if out != out_new:
        for a, b in zip(out.splitlines(), out_new.splitlines()):
            if a != b:
                print(f"      · oldest: {a}")
                print(f"      · newest: {b}")

    # ── D6 — main() 의 SPY 현재가 블록 (AST 추출 후 실제 실행) ─────────────
    #   main() 은 시트·메일을 건드리므로 통째로 부를 수 없다. 해당 try 블록만
    #   소스에서 떼어내 실행한다 — **사본이 아니라 배포될 코드 자체**다.
    mfn = find_func(dtree, "main")
    blk = None
    for n in ast.walk(mfn) if mfn else []:
        if isinstance(n, ast.Try) and "spy_data = fh.fmp_get_json" in ast.unparse(n):
            blk = n
            break
    if blk is None:
        chk(False, "D6-0", "main() 에서 SPY 현재가 블록을 찾지 못했다")
    else:
        ns = {"fh": fh, "fx": fx, "np": __import__("numpy")}
        log6, sup6 = [], []
        saved = fh.fmp_get_json
        # 일부러 oldest-first 로 준다 — rows[0] 을 쓰면 여기서 갈린다.
        fh.fmp_get_json = _drg_stub(order="oldest", log=log6, supply=sup6)
        try:
            exec(compile(ast.Module(body=[blk], type_ignores=[]),
                         "<spy_block>", "exec"), ns)
        finally:
            fh.fmp_get_json = saved
        got = ns.get("spy_close")
        want = sup6[-1]["last_close"] if sup6 else None    # 스텁이 만든 최신 봉
        oldest = 100.0                                      # 스텁의 첫 봉 종가
        chk(not any("limit=" in p for p in log6), "D6a",
            "SPY 현재가 호출에 limit= 부재")
        chk(got == want, "D6b",
            f"oldest-first 응답에서도 **최신** 종가를 고른다 (got={got}, want={want})")
        chk(got != oldest, "D6c",
            f"창의 **가장 오래된** 종가({oldest})가 아니다 — 정렬 가정 부활 검출")


def main():
    print("=" * 78)
    print("diag_hist_window — 가격 히스토리 조회 창(from/to) 회귀 스위트")
    print(f"  run_watchlist_alerts: {m.__file__}")
    print(f"  regime_core        : {rc.__file__}")
    print(f"  fmp_extras         : {fx.__file__}")
    print(f"  레포 루트           : {_REPO}  (스위트 위치: {_HERE})")
    print(f"  app.py             : {os.path.join(_REPO, 'app.py')} (정적 분석)")
    print("=" * 78)

    group_S()
    group_R()
    group_P()
    group_T()
    group_H()
    group_G()
    group_C()
    group_A()
    group_W()
    group_X()
    group_D()

    print("\n" + "=" * 78)
    print(f"결과: {len(PASS)}/{len(PASS) + len(FAIL)} 통과")
    if FAIL:
        print("\n실패 항목:")
        for f in FAIL:
            print(f"  ❌ {f}")
        print("=" * 78)
        return 1
    print("✅ 전 항목 통과 — 조회 창이 하류 요구치를 덮는다.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
