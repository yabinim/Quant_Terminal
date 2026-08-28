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

안전성: 네트워크 0 · FMP 콜 0 · 시트 접근 0 · 메일 0 · 파일 쓰기 0.

사용법:
  FMP_API_KEY=x GSPREAD_KEY='{}' GMAIL_USER=a GMAIL_APP_PASSWORD=b \\
  GMAIL_TO=c python3 diag_hist_window.py
"""
import os
import sys
import ast

for _k, _v in (("FMP_API_KEY", "x"), ("GSPREAD_KEY", "{}"), ("GMAIL_USER", "a"),
               ("GMAIL_APP_PASSWORD", "b"), ("GMAIL_TO", "c")):
    os.environ.setdefault(_k, _v)

from datetime import datetime, timedelta

import pandas as pd

import run_watchlist_alerts as m
import regime_core as rc
import watchlist_metrics_core as wm

PASS, FAIL = [], []


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
def main():
    print("=" * 78)
    print("diag_hist_window — 가격 히스토리 조회 창(from/to) 회귀 스위트")
    print(f"  run_watchlist_alerts: {m.__file__}")
    print(f"  regime_core        : {rc.__file__}")
    print("=" * 78)

    group_S()
    group_R()
    group_P()
    group_T()
    group_H()
    group_G()
    group_C()

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
