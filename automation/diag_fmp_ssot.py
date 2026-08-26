#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_fmp_ssot.py — 저장소 전역 FMP SSOT·계약 가드 (2026-08-26).

왜 이 파일이 있나
─────────────────
2026-08-26 에 같은 결함이 두 번 다른 모습으로 터졌다.

  (1) `run_signal_backtest.py` 가 `fmp_http` 를 우회하고 원시 requests.get 으로
      474종목을 발사 → 분당 한도(300)를 넘긴 174종목이 429 → 빈 DataFrame 으로
      **무성 탈락** → 백테스트는 정상 완료되고 시트에 오염 행이 쌓였다.

  (2) 그걸 고치면서 `_fmp_price_history` 의 반환을 `(df, kind)` 로 바꿨는데,
      그 함수를 빌려 쓰는 `diag_fmp_depth.py`·`diag_trade_history.py` 의
      호출부를 안 고쳤다. 전자는 `len(tuple) == 2` 때문에 **예외 없이 전 종목이
      "2봉"으로 집계**됐다.

(1) 은 `fmp_extras.py` 70행 주석에 이미 한 번 기록돼 있던 사고다. 그때
"같은 패턴을 전 저장소에서 grep 한다"고 적었지만, grep 을 사람이 하기로 했고
하지 않았다. (2) 도 마찬가지로 사람 기억에 맡겼다가 놓쳤다.

**그래서 grep 을 도구로 옮긴다.** 이 파일이 그 도구다.

검사 구조
─────────
  A 저장소 전역 (대상 무관 · 영구)
    A1 원시 FMP 호출   requests.get 으로 FMP 를 직접 때리는 지점 (기준선 대비)
    A2 튜플 계약       튜플 반환 함수를 단일 이름으로 받는 크로스모듈 호출부
    A3 submit 언팩     ex.submit(mod.튜플함수, …) 후 .result() 를 언팩하는가

  B 위성 백테스트 게이트 (diag_satellite_backtest.py 락스텝 짝)
    B1 스로틀 · B2 분류 · B3 게이트 순서 · B4 지문 · B5 복제 대조 · B6 뮤테이션

A1 의 기준선(_RAW_GET_BASELINE)에 대하여
────────────────────────────────────────
저장소에는 이미 80여 곳의 원시 FMP 호출이 있다. 전부 하드 실패로 잡으면
이 스위트는 첫날부터 빨간불이고, 빨간불인 스위트는 아무도 안 본다.
그래서 **래칫**으로 만든다 — 기준선보다 늘면 실패, 줄면 통과하되 경고.

⚠️ 기준선은 '괜찮다'는 뜻이 아니라 '알고 있고 아직 안 고쳤다'는 뜻이다.
   각 항목에 왜 미뤘는지를 적어둔다.

안전성
──────
· 네트워크 접근 없음 · 시트 접근 없음 · FMP 호출 없음 (전부 스텁/정적 분석)
· 부작용 없다. 몇 번을 돌려도 같은 결과.

실행:  python3 automation/diag_fmp_ssot.py
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 모듈이 환경변수를 읽기 **전에** 비워둔다 — 실행 환경에 값이 남아 있으면
# 기본값 검사가 오염된다.
os.environ.pop("MIN_FETCH_RATE", None)

_fails, _passes, _notes = [], 0, []


def check(label, got, want):
    global _passes
    ok = (got == want)
    if ok:
        _passes += 1
        print("  ✅ " + label)
    else:
        _fails.append(label + "  (got=" + repr(got) + " want=" + repr(want) + ")")
        print("  ❌ " + label + "  got=" + repr(got) + " want=" + repr(want))
    return ok


# ══════════════════════════════════════════════════════════════════════════
# 저장소 수집 — 루트 + automation/ 양쪽을 본다 (배포 레이아웃이 갈라져 있다)
# ══════════════════════════════════════════════════════════════════════════
def repo_files() -> dict:
    """{모듈명: 절대경로}. 같은 이름이 양쪽에 있으면 sys.path 와 같게 루트 우선."""
    # ⚠️ 순서가 sys.path 와 같아야 한다. sys.path 는 위에서 [_ROOT, _HERE, …] 로
    #    끝나므로 import 는 _ROOT 를 먼저 집는다. 여기서 automation/ 을 우선하면
    #    **스캔한 파일과 import 한 모듈이 서로 다른 사본**이 되어, 정적 검사는
    #    통과하는데 실제로는 옛 코드가 도는 상황이 만들어진다.
    out = {}
    for d in (_HERE, os.path.join(_ROOT, "automation"), _ROOT):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".py") and not f.startswith("."):
                out[f[:-3]] = os.path.join(d, f)
    return out


FILES = repo_files()
SRCS = {}
TREES = {}
for _m, _p in FILES.items():
    try:
        _s = open(_p, encoding="utf-8").read()
        SRCS[_m] = _s
        TREES[_m] = ast.parse(_s)
    except (SyntaxError, UnicodeDecodeError, OSError):
        continue


def _parents(tree):
    """자식 → 부모 맵. ast 에는 부모 포인터가 없어 직접 만든다."""
    pm = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            pm[c] = n
    return pm


def _unparse(n) -> str:
    try:
        return ast.unparse(n)
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════
# A1 — 원시 FMP 호출
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ 값은 '허용치'가 아니라 '현재 남아 있는 부채'다. 줄이면 이 숫자를 낮춘다.
_RAW_GET_BASELINE = {
    # 대화형 경로 + @st.cache_data. 자동화와 위험도가 다르고 68곳을 한 번에
    # 손대면 회귀 위험이 크다 → 별도 검토 사안(인수인계 §6-B5).
    "app.py": 58,
    # 8AM/9AM DRG 예측. 종목 수가 적어(매크로 지표 중심) 한도에 닿지 않는다.
    "run_drg_predict.py": 11,
    # run_watchlist_alerts.py 는 2026-08-26 에 4곳 전부 fmp_http 로 전환됐다.
    # requests 임포트 자체를 지워 되살리려면 임포트를 다시 추가해야 한다.
    # 기준선에서 제거 = 이제 한 곳이라도 생기면 '신규 우회'로 실패한다.
    "narrative_core.py": 2,
    "run_narrative.py": 2,
    "run_drg_verify.py": 1,
    "run_earnings_watch.py": 1,
    "industry_core.py": 1,
    "diag_earnings_preview_backtest.py": 1,
    "diag_industry_mapping.py": 1,
    # P2 로 미룬 항목 — 읽기 전용 진단이고 시트에 쓰지 않는다.
    "diag_sell_verdict.py": 1,
}

# fmp_http 자체는 SSOT 구현체이므로 원시 호출이 있어야 정상이다.
_RAW_GET_EXEMPT = {"fmp_http"}


def _fmp_url_names(tree) -> set:
    """모듈 안에서 FMP 주소 문자열이 바인딩된 이름들 (_FMP_BASE 등)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str) \
                and "financialmodelingprep" in n.value.value:
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def raw_fmp_gets(mod: str) -> list:
    """[(lineno, 호출식 요약)] — requests.get 으로 FMP 를 직접 때리는 지점."""
    tree = TREES.get(mod)
    if tree is None or mod in _RAW_GET_EXEMPT:
        return []
    names = _fmp_url_names(tree)
    hits = []
    for c in ast.walk(tree):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "get"
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "requests"):
            continue
        arg = _unparse(c.args[0]) if c.args else ""
        if ("financialmodelingprep" in arg or "apikey" in arg
                or any(nm in arg for nm in names)):
            hits.append((c.lineno, arg[:60]))
    return hits


# ══════════════════════════════════════════════════════════════════════════
# A2/A3 — 튜플 반환 계약
# ══════════════════════════════════════════════════════════════════════════
def tuple_returning(tree) -> dict:
    """{함수명: {가능한 튜플 길이}} — return 문이 튜플인 최상위/중첩 함수."""
    out = {}
    for n in ast.walk(tree):
        if not isinstance(n, ast.FunctionDef):
            continue
        ar = set()
        for r in ast.walk(n):
            if isinstance(r, ast.Return) and isinstance(r.value, ast.Tuple):
                if len(r.value.elts) >= 2:
                    ar.add(len(r.value.elts))
        if ar:
            out[n.name] = ar
    return out


REG = {m: tuple_returning(t) for m, t in TREES.items()}


def import_aliases(tree) -> dict:
    """{별칭: 저장소모듈명} — 저장소 안의 모듈만."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in TREES:
                    out[a.asname or a.name] = a.name
    return out


# 튜플에 실제로 있는 속성 — 이건 접근해도 정상이다.
_TUPLE_ATTRS = {"count", "index"}


def _enclosing_fn(node, pm):
    cur = pm.get(node)
    while cur is not None:
        if isinstance(cur, ast.FunctionDef):
            return cur
        cur = pm.get(cur)
    return None


def contract_violations(mod: str) -> list:
    """[(lineno, 설명)] — 튜플 반환 함수를 단일 값으로 소비하는 지점."""
    tree = TREES.get(mod)
    if tree is None:
        return []
    pm = _parents(tree)
    alias = import_aliases(tree)
    bad = []

    def _arity(al, fn):
        tgt = alias.get(al)
        if not tgt:
            return None
        return REG.get(tgt, {}).get(fn)

    for c in ast.walk(tree):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
            continue
        ar = _arity(f.value.id, f.attr)
        if not ar:
            continue
        ref = f"{f.value.id}.{f.attr}()"
        p = pm.get(c)
        # (1) X = mod.f(...)
        #   단일 이름으로 받는 것 자체는 죄가 아니다 — 튜플을 통째로 다음 함수에
        #   넘기는 정당한 패턴이 있다(run_watchlist_alerts 의 `_posv` 가 그렇다).
        #   진짜 결함은 그렇게 받아놓고 **첫 원소인 척 쓰는 것**이다.
        #   그래서 이후 `X.<속성>` 접근이 있을 때만 잡는다.
        if isinstance(p, ast.Assign):
            if len(p.targets) == 1 and isinstance(p.targets[0], ast.Name):
                nm = p.targets[0].id
                scope = _enclosing_fn(c, pm) or tree
                hit = None
                for u in ast.walk(scope):
                    if (isinstance(u, ast.Attribute) and isinstance(u.value, ast.Name)
                            and u.value.id == nm and u.attr not in _TUPLE_ATTRS):
                        hit = u.attr
                        break
                if hit:
                    bad.append((c.lineno, f"{ref} 를 단일 이름 '{nm}' 로 받고 "
                                          f"'{nm}.{hit}' 로 쓴다 — 튜플에는 없다"))
            continue
        # (2) mod.f(...).attr — 튜플에 곧바로 속성 접근
        if isinstance(p, ast.Attribute) and p.attr not in _TUPLE_ATTRS:
            bad.append((c.lineno, f"{ref} 반환값에 '.{p.attr}' 접근 "
                                  f"— 튜플에는 없다"))
            continue
        # ⚠️ len(mod.f(...)) 은 검사하지 않는다. 스위트가 반환 길이를 일부러
        #    확인하는 정당한 용법이 있고(diag_universe_funnel B5), 결함 쪽은
        #    아래 (3) submit 규칙이 이미 덮는다. 구분할 방법이 없으면 잡지 않는다
        #    — 오탐이 나오는 가드는 곧 무시당한다.

    # (3) ex.submit(mod.f, ...) — 결과는 .result() 로 온다. 같은 함수 안에
    #     튜플 언팩이 있는지 본다. 없으면 (1)~(3) 과 같은 사고가 지연 발생한다.
    for c in ast.walk(tree):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "submit" and c.args):
            continue
        a0 = c.args[0]
        if not (isinstance(a0, ast.Attribute) and isinstance(a0.value, ast.Name)):
            continue
        tgt = alias.get(a0.value.id)
        if not tgt or a0.attr not in REG.get(tgt, {}):
            continue
        fn = _enclosing_fn(c, pm)
        ok = False
        for x in ast.walk(fn if fn is not None else tree):
            if (isinstance(x, ast.Assign) and isinstance(x.value, ast.Call)
                    and isinstance(x.value.func, ast.Attribute)
                    and x.value.func.attr == "result"
                    and len(x.targets) == 1
                    and isinstance(x.targets[0], ast.Tuple)):
                ok = True
        if not ok:
            bad.append((c.lineno, f"submit({a0.value.id}.{a0.attr}) 인데 "
                                  f"이 함수에 '.result()' 튜플 언팩이 없다"))
    return sorted(set(bad))


# ══════════════════════════════════════════════════════════════════════════
print("=" * 76)
print("A) 저장소 전역 — 원시 FMP 호출 래칫")
print("=" * 76)

_raw_now = {}
for m in sorted(TREES):
    h = raw_fmp_gets(m)
    if h:
        _raw_now[m + ".py"] = len(h)

_new, _shrunk = [], []
for fn, n in sorted(_raw_now.items()):
    base = _RAW_GET_BASELINE.get(fn)
    if base is None:
        _new.append(f"{fn}: {n}곳 — 기준선에 없는 **신규 우회**")
    elif n > base:
        _new.append(f"{fn}: {n}곳 (기준선 {base}) — 늘었다")
    elif n < base:
        _shrunk.append(f"{fn}: {n}곳 (기준선 {base}) — 줄었다, 기준선을 낮출 것")

for fn in sorted(_RAW_GET_BASELINE):
    if fn not in _raw_now:
        _shrunk.append(f"{fn}: 0곳 (기준선 {_RAW_GET_BASELINE[fn]}) — "
                       f"전부 정리됨, 기준선에서 제거할 것")

check("A1  기준선을 넘는 원시 FMP 호출이 없다", _new, [])
if _new:
    for x in _new:
        print("        · " + x)
print(f"      (현재 부채 총 {sum(_raw_now.values())}곳 / "
      f"{len(_raw_now)}개 파일 — 0 이 목표)")
if _shrunk:
    # 실패로 만들지 않는다. 고쳤더니 스위트가 빨개지면 아무도 안 고친다.
    print("  ⚠️  기준선 갱신 필요:")
    for x in _shrunk:
        print("        · " + x)
        _notes.append(x)

print()
print("=" * 76)
print("A) 저장소 전역 — 튜플 반환 계약 (허용목록 없음 · 0 이어야 한다)")
print("=" * 76)

_viol = []
for m in sorted(TREES):
    for ln, why in contract_violations(m):
        _viol.append(f"{m}.py:{ln}  {why}")

check("A2  튜플 반환 함수를 단일 값으로 받는 곳이 없다", _viol, [])
for x in _viol:
    print("        · " + x)

_n_tuple_fns = sum(len(v) for v in REG.values())
print(f"      (튜플 반환 함수 {_n_tuple_fns}개를 추적 중)")


# ══════════════════════════════════════════════════════════════════════════
# B) 위성 백테스트 게이트 — diag_satellite_backtest.py 락스텝 짝
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ 로직을 복사하지 않는다. 실제 모듈을 import 해서 실제 함수를 호출하고,
#    배선은 실제 소스를 AST 로 읽는다. 사본을 시험하면 항상 초록불이 나온다.
import pandas as pd   # noqa: E402

import diag_satellite_backtest as sb   # noqa: E402
import run_signal_backtest as bt       # noqa: E402

SB_SRC = SRCS.get("diag_satellite_backtest", "")
SB_TREE = TREES.get("diag_satellite_backtest")


def env_names(src: str) -> set:
    """모듈이 **실제로 읽는** 환경변수 이름 집합.

    ⚠️ 문자열 검색으로는 안 된다. '우회 스위치는 두지 않는다' 고 적어둔 **주석
    자체**에 걸려 오탐이 난다(2026-08-26 에 실제로 겪었다). 주석의 언급과 실제
    os.environ 접근을 구분하려면 AST 로 읽어야 한다.
    """
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for c in ast.walk(tree):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)):
            continue
        if c.func.attr not in ("get", "pop"):
            continue
        v = c.func.value
        if not (isinstance(v, ast.Attribute) and v.attr == "environ"):
            continue
        if c.args and isinstance(c.args[0], ast.Constant) \
                and isinstance(c.args[0].value, str):
            out.add(c.args[0].value)
    return out


def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _calls_attr(node, attr):
    for c in ast.walk(node):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == attr):
            return True
    return False


def _mentions(node, ident):
    for c in ast.walk(node):
        if isinstance(c, ast.Name) and c.id == ident:
            return True
    return False


def sb_wiring(src: str) -> dict:
    """실제 소스에서 배선 사실만 뽑는다. 값 판정은 하지 않는다."""
    out = {
        "import_fh": False,
        "ssot_call": False,          # _fmp_eod 가 fmp_get_ex 를 쓰는가
        "raw_get": True,             # _fmp_eod 에 원시 requests.get 이 있는가(있으면 안 됨)
        "reason_counted": False,     # _batch_fetch 가 사유를 세는가
        "gate_exists": False,
        "gate_returns": False,
        "gate_before_write": False,  # 게이트가 시트 쓰기 **앞**인가
        "gate_op": None,
        "hash_sorted": False,
        "n_gates": 0,
    }
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out

    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "fmp_http":
                    out["import_fh"] = True

    f = _fn(tree, "_fmp_eod")
    if f is not None:
        out["ssot_call"] = _calls_attr(f, "fmp_get_ex")
        raw = False
        for c in ast.walk(f):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "get"
                    and isinstance(c.func.value, ast.Name)
                    and c.func.value.id == "requests"):
                raw = True
        out["raw_get"] = raw

    b = _fn(tree, "_batch_fetch")
    if b is not None:
        for c in ast.walk(b):
            if (isinstance(c, ast.Subscript) and isinstance(c.value, ast.Name)
                    and c.value.id == "reasons"):
                out["reason_counted"] = True

    h = _fn(tree, "universe_hash")
    if h is not None:
        for c in ast.walk(h):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) \
                    and c.func.id == "sorted":
                out["hash_sorted"] = True

    m = _fn(tree, "main")
    if m is not None:
        gates, write_ln = [], None
        for c in ast.walk(m):
            if isinstance(c, ast.If) and _mentions(c.test, "MIN_FETCH_RATE"):
                gates.append(c)
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == "write_results"):
                if write_ln is None or c.lineno < write_ln:
                    write_ln = c.lineno
        gates.sort(key=lambda x: x.lineno)
        out["n_gates"] = len(gates)
        if gates:
            g = gates[0]
            out["gate_exists"] = True
            if isinstance(g.test, ast.Compare) and g.test.ops:
                out["gate_op"] = type(g.test.ops[0]).__name__
            out["gate_returns"] = any(
                isinstance(x, ast.Return) and isinstance(x.value, ast.Constant)
                and x.value.value == 1 for x in ast.walk(g))
            if write_ln is not None:
                # **모든** 게이트가 쓰기 앞이어야 한다. 첫 게이트만 보면 뒤쪽
                # 게이트를 쓰기 뒤로 옮긴 변경을 놓친다.
                out["gate_before_write"] = max(x.lineno for x in gates) < write_ln
    return out


SW = sb_wiring(SB_SRC)

print()
print("=" * 76)
print("B) 위성 백테스트 — 스로틀 배선")
print("=" * 76)

check("B1  fmp_http 를 임포트한다", SW["import_fh"], True)
check("B2  _fmp_eod 가 fmp_get_ex 를 호출한다", SW["ssot_call"], True)
check("B3  _fmp_eod 에 원시 requests.get 이 없다", SW["raw_get"], False)
check("B4  타임아웃이 15초 이상 (1,300봉 페이로드)", sb._FMP_TIMEOUT >= 15, True)

# ── 선행 조건 — 이하는 v2.8 심볼이 있어야 돌아간다 ────────────────────────
_NEED = ("fh", "_fmp_eod", "_batch_fetch", "universe_hash",
         "_env_fetch_rate", "MIN_FETCH_RATE", "_INFRA_KINDS")
_MISSING = [a for a in _NEED if not hasattr(sb, a)]
if _MISSING:
    print()
    print("=" * 76)
    print("❌ 중단 — diag_satellite_backtest 가 v2.8 이전 버전이다")
    print("   없는 심볼: " + ", ".join(_MISSING))
    print("   두 파일을 함께 배포할 것 (락스텝)")
    print("=" * 76)
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("B) 위성 백테스트 — 실패 분류 (스텁 주입 · 네트워크 없음)")
print("=" * 76)

_TK = [f"T{i:02d}" for i in range(10)]


def _mk_px():
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    return pd.DataFrame({"px": [100.0 + i for i in range(30)]}, index=idx)


def _stub_eod(tk, ep, limit=None):
    """티커별로 서로 다른 실패 사유를 낸다 — 사유가 보존되는지 보기 위함."""
    i = int(tk[1:])
    if ep == "full":
        if i == 8:
            return pd.DataFrame(), "rate_limited"
        if i == 9:
            return pd.DataFrame(), "empty"
        return _mk_px(), "ok"                       # T00~T07 = 8건
    if i == 7:
        return pd.DataFrame(), "rate_limited"
    if i in (6, 8, 9):
        return pd.DataFrame(), "empty"
    return _mk_px(), "ok"                           # T00~T05 = 6건


_real_eod = sb._fmp_eod
sb._fmp_eod = _stub_eod
try:
    _raw, _adj, _fb, _rs, _fd = sb._batch_fetch(_TK)
finally:
    sb._fmp_eod = _real_eod

check("B5  원종가 확보 = 8종목", len(_raw), 8)
check("B6  배당조정 폴백 = 2종목 (T06 empty · T07 rate_limited)",
      sorted(_fb), ["T06", "T07"])
check("B7  사유가 (엔드포인트, kind) 로 보존된다",
      (_rs.get(("full", "ok")), _rs.get(("full", "rate_limited")),
       _rs.get(("dividend-adjusted", "rate_limited"))), (8, 1, 1))
check("B8  카운트 합계 = 티커 × 엔드포인트 (누락 없음)",
      sum(_rs.values()), len(_TK) * 2)
check("B9  탈락 목록에 티커·엔드포인트·사유가 함께 남는다", len(_fd), 6)


def _boom_eod(tk, ep, limit=None):
    raise RuntimeError("worker died")


sb._fmp_eod = _boom_eod
try:
    _r2, _a2, _f2, _rs2, _fd2 = sb._batch_fetch(_TK)
finally:
    sb._fmp_eod = _real_eod

check("B10 워커 예외가 삼켜지지 않고 'exception' 으로 집계된다",
      _rs2.get(("full", "exception")), 10)
check("B11 빈 입력도 5-튜플을 돌려준다", len(sb._batch_fetch([])), 5)


# ── 양성대조 — 옛 계약으로 되돌리면 실제로 깨지는가 ────────────────────────
def _old_style_eod(tk, ep, limit=None):
    return pd.DataFrame()          # v2.7 이하: kind 없이 DataFrame 만


sb._fmp_eod = _old_style_eod
try:
    _r3, _a3, _f3, _rs3, _fd3 = sb._batch_fetch(_TK)
finally:
    sb._fmp_eod = _real_eod

check("P1  양성대조: 옛 계약으로 되돌리면 전량 exception 으로 잡힌다",
      (len(_r3), _rs3.get(("full", "exception"))), (0, 10))


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("B) 위성 백테스트 — 게이트 · 지문 · 결과 열")
print("=" * 76)

check("B12 게이트가 존재한다", SW["gate_exists"], True)
check("B13 게이트 비교가 '<' 이다 (>= 로 뒤집히면 정상 run 이 막힌다)",
      SW["gate_op"], "Lt")
check("B14 게이트가 실제로 빠져나간다 (return 1)", SW["gate_returns"], True)
# ⚠️ 존재 검사만으로는 부족하다. 게이트를 시트 쓰기 뒤로 옮기면 오염 행은
#    그대로 들어가고 종료 코드만 1이 된다 — 로그에도 이상이 없다.
check("B15 **모든** 게이트가 시트 쓰기보다 앞이다", SW["gate_before_write"], True)
check("B16 게이트가 둘이다 (원종가 · 배당조정 인프라성 실패)", SW["n_gates"], 2)
check("B17 기본 임계 0.98", sb.MIN_FETCH_RATE, 0.98)
# 'empty'(그 시리즈가 원래 없음)를 인프라성으로 세면 게이트가 영구 빨간불이 되고,
# 반대로 빼먹으면 진짜 오염을 놓친다.
check("B18 인프라성 사유에 'empty' 가 없다", "empty" in sb._INFRA_KINDS, False)
check("B19 인프라성 사유에 'rate_limited' 가 있다",
      "rate_limited" in sb._INFRA_KINDS, True)

check("B20 결과 열 마지막이 Universe_Hash", sb._RESULT_COLS[-1], "Universe_Hash")

# 해시: 존재 검사로는 부족하다. sorted() 없이 계산하면 구성이 같아도 run 마다
# 값이 달라지는데, 겉보기에는 '해시가 잘 찍히는' 정상 동작으로 보인다.
_H1 = sb.universe_hash(["AAPL", "MSFT", "NVDA"])
_H2 = sb.universe_hash(["NVDA", "aapl", " MSFT "])
check("B21 순서·대소문자·공백이 달라도 같은 지문", _H1, _H2)
check("B22 구성이 다르면 지문도 다르다",
      sb.universe_hash(["AAPL", "MSFT"]) != _H1, True)
# USER_ENTERED 로 쓰기 때문에 16진수 8자가 전부 숫자면(≈2.3%) 앞자리 0 이 날아간다.
check("B23 지문에 'u' 접두어가 있다 (USER_ENTERED 숫자 해석 방지)",
      _H1.startswith("u") and len(_H1) == 9, True)
check("B24 빈 유니버스는 빈 문자열", sb.universe_hash([]), "")

# ── 복제 대조 — run_signal_backtest 와 같은 구현이어야 한다 ────────────────
# 두 파일이 같은 함수를 각자 갖고 있다(공유 모듈로 빼면 diag_universe_funnel
# 까지 손대야 해서 미뤘다). 복제는 반드시 어긋난다 — 그래서 여기서 대조한다.
_SAMPLES = (["AAPL", "MSFT"], ["nvda", "AAPL ", "MSFT"], [], ["SPY"])
check("B25 universe_hash 가 run_signal_backtest 와 완전히 동일",
      [sb.universe_hash(x) for x in _SAMPLES],
      [bt.universe_hash(x) for x in _SAMPLES])
_RATES = ("", "0.5", "1.0", "abc", "1.5", "-0.1", " 0.98 ")
check("B26 _env_fetch_rate 가 run_signal_backtest 와 완전히 동일",
      [sb._env_fetch_rate(x) for x in _RATES],
      [bt._env_fetch_rate(x) for x in _RATES])

# ── 우회 스위치 부재 ──────────────────────────────────────────────────────
_ENV = env_names(SB_SRC)
_BYPASS = sorted(n for n in _ENV
                 if n.startswith("SKIP_") or n.startswith("FORCE_")
                 or n.startswith("IGNORE_"))
check("B27 우회 스위치(SKIP_*/FORCE_*/IGNORE_*)가 없다", _BYPASS, [])
check("B28 MIN_FETCH_RATE 를 환경변수로 읽는다", "MIN_FETCH_RATE" in _ENV, True)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("B) 뮤테이션 역검증 — 위 검사가 결함을 실제로 잡는가")
print("=" * 76)

# ⚠️ 뮤테이션이 레이블대로 동작하는지 확인할 것. '무검출'은 코드가 아니라
#    뮤테이션이 틀렸다는 신호일 수 있다(2026-08-26 에 실제로 겪었다).
MUTANTS = [
    ("M1 SSOT 우회 — fh.fmp_get_ex 를 requests.get 으로",
     "r, _status, kind = fh.fmp_get_ex(url, timeout=_FMP_TIMEOUT)",
     "r = requests.get(url, timeout=_FMP_TIMEOUT); kind = 'ok'",
     ["ssot_call", "raw_get"]),
    ("M2 사유 집계 제거",
     "            reasons[(ep, kind)] = reasons.get((ep, kind), 0) + 1",
     "            pass",
     ["reason_counted"]),
    ("M3 게이트 비교 뒤집기 (< → >)",
     "    if _rate < MIN_FETCH_RATE:",
     "    if _rate > MIN_FETCH_RATE:",
     ["gate_op"]),
    ("M4 게이트 이탈 제거 (return 1 → pass)",
     '              "_FMP_TIMEOUT 을 늘린다.")\n        return 1',
     '              "_FMP_TIMEOUT 을 늘린다.")\n        pass',
     ["gate_returns"]),
    ("M5 해시 sorted() 제거",
     "    seq = sorted(str(t).strip().upper() for t in (tickers or [])",
     "    seq = list(str(t).strip().upper() for t in (tickers or [])",
     ["hash_sorted"]),
]

for name, old, new, keys in MUTANTS:
    n_occ = SB_SRC.count(old)
    if n_occ != 1:
        print("  ⚠️  " + name + " — 앵커가 " + str(n_occ) + "회(1회여야 함). 스킵")
        _fails.append(name + " [앵커 " + str(n_occ) + "회]")
        continue
    mw = sb_wiring(SB_SRC.replace(old, new, 1))
    flipped = [k for k in keys if mw.get(k) != SW.get(k)]
    if flipped:
        print("  ✅ " + name + " — " + ", ".join(flipped) + " 가 뒤집힘")
        _passes += 1
    else:
        print("  ❌ " + name + " — **배선 검사가 잡아내지 못함**")
        _fails.append(name)

# ── M6 게이트 순서 — 존재 검사로는 절대 못 잡는 유형 ──────────────────────
_W = "    write_results(all_rows)\n"
_GH = "    if _rate < MIN_FETCH_RATE:\n"
if SB_SRC.count(_W) == 1 and SB_SRC.count(_GH) == 1:
    _moved = SB_SRC.replace(_W, "", 1).replace(_GH, _W + _GH, 1)
    _mw = sb_wiring(_moved)
    if _mw.get("gate_exists") and not _mw.get("gate_before_write"):
        print("  ✅ M6 시트 쓰기를 게이트 앞으로 이동 — 순서 검사가 잡아냄")
        _passes += 1
    else:
        print("  ❌ M6 시트 쓰기를 게이트 앞으로 이동 — **잡아내지 못함**")
        _fails.append("M6 게이트 순서")
else:
    print("  ⚠️  M6 — 앵커 없음. 스킵")
    _fails.append("M6 [앵커 소실]")

# ── M7 A2 스캐너 자체의 판별력 ────────────────────────────────────────────
# 가드가 계약 위반을 정말 잡는지, 위반을 인공으로 만들어 확인한다.
_probe = '''
import run_signal_backtest as bt
def f():
    df = bt._fmp_price_history("SPY")
    return df.empty
'''
_pm = "___contract_probe___"
TREES[_pm] = ast.parse(_probe)
SRCS[_pm] = _probe
REG[_pm] = tuple_returning(TREES[_pm])
try:
    _hits = contract_violations(_pm)
finally:
    for _d in (TREES, SRCS, REG):
        _d.pop(_pm, None)
if _hits:
    print("  ✅ M7 인공 계약 위반을 A2 스캐너가 잡아냄 — " + _hits[0][1][:52])
    _passes += 1
else:
    print("  ❌ M7 인공 계약 위반을 **잡아내지 못함** — A2 는 판별력이 없다")
    _fails.append("M7 A2 판별력")


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
if _fails:
    print("❌ 실패 " + str(len(_fails)) + "건 / 통과 " + str(_passes) + "건")
    for x in _fails:
        print("   - " + str(x))
    sys.exit(1)
print("✅ 전부 통과 — " + str(_passes) + "건")
if _notes:
    print()
    print("⚠️  기준선 갱신 권고 " + str(len(_notes)) + "건 (실패 아님):")
    for x in _notes:
        print("   - " + x)
print("=" * 76)
