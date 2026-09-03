# -*- coding: utf-8 -*-
"""diag_universe_funnel.py — 백테스트 유니버스 퍼널 회귀 스위트 (v2.8).

무엇을 지키나
─────────────
`run_signal_backtest.py` 는 474종목의 이력을 병렬로 받아온다. 여기서 가장
위험한 고장은 **일부만 받아오고도 정상 완료하는 것**이다.

    분당 한도(300)를 넘긴 요청이 429 로 되돌아온다
      → status_code != 200 → 빈 DataFrame → 그 종목이 조용히 사라진다
      → 백테스트는 정상 완료되고 시트에 정상처럼 보이는 행이 쌓인다
      → 크기만 다를 뿐이라 사후에 골라낼 수 없다
      → run 간 비교가 오염된 줄 모르고 결론을 낸다

2026-08-26 진단에서 실제로 발생했다. 로그에 `299/474` 로만 남았고
(299 = 300/분 − SPY 1콜) 몇 주간 발견되지 않았다. 8-01 의 `227→159` 도
`TEST_LOOKBACK` 탓으로 잘못 기록돼 있었으나 같은 원인이었다.

검사 구조
─────────
    1 스로틀    _fmp_price_history 가 fmp_http SSOT 를 거친다 (AST)
    2 분류      실패를 사유별로 구분해 반환한다 (스텁 주입 → 실제 함수 호출)
    3 집계      _batch_fetch_history 가 사유·탈락 목록을 보존한다
    4 게이트    부분 유니버스가 **시트 쓰기 전에** 차단된다 (AST 순서 검사)
    5 지문      세그먼트별 Universe_Size/Hash 가 실제 평가 집합을 반영한다
    6 기록      결과 행에 지문이 실린다
    7 뮤테이션  위 검사가 결함을 실제로 잡는지 역검증

⚠️ 4절의 **순서** 검사가 핵심이다. 게이트가 존재하는지만 보면, 게이트를
   시트 쓰기 뒤로 옮긴 변경을 못 잡는다 — 그러면 오염 행은 그대로 들어가고
   종료 코드만 1이 된다. 로그에도 이상이 없다.

⚠️ 5절도 존재 검사로는 부족하다. 해시를 `sorted()` 없이 계산하면 구성이
   같은데도 run 마다 값이 달라지는데, 겉보기에는 '해시가 잘 찍히는' 정상
   동작으로 보인다. 그래서 순서를 뒤집은 입력으로 직접 비교한다.

⚠️ 로직을 복사하지 않는다 — run_signal_backtest 의 실제 함수를 import 해서
   호출하고, 배선은 실제 소스를 AST 로 읽는다.

안전성
──────
· 네트워크 접근 없음 · 시트 접근 없음 · FMP 호출 없음 (전부 스텁)
· 부작용 없다. 몇 번을 돌려도 같은 결과.

실행:  python3 automation/diag_universe_funnel.py
"""
import ast
import inspect
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
os.environ.pop("CONFIRM_DAYS", None)

import numpy as np      # noqa: E402
import pandas as pd     # noqa: E402

import run_signal_backtest as bt   # noqa: E402

_fails, _passes = [], 0


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


SRC = open(bt.__file__, encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════════════
# AST 배선 분석 — 뮤테이션 때 같은 함수를 변형 소스에 다시 돌린다
# ══════════════════════════════════════════════════════════════════════════
def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _calls_attr(node, attr):
    """node 하위에 `*.attr(...)` 호출이 있는가."""
    for c in ast.walk(node):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == attr):
            return True
    return False


def _calls_name(node, name):
    for c in ast.walk(node):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == name:
            return True
    return False


def _mentions(node, ident):
    for c in ast.walk(node):
        if isinstance(c, ast.Name) and c.id == ident:
            return True
    return False


def env_names(src: str) -> set:
    """모듈이 **실제로 읽는** 환경변수 이름 집합.

    ⚠️ 문자열 검색으로는 안 된다. 초안에서 `"SKIP_FETCH_GATE" in SRC` 로 검사했다가
    '우회 스위치는 두지 않는다' 고 적어둔 **주석 자체**에 걸려 오탐이 났다.
    주석의 언급과 실제 os.environ 접근을 구분하려면 AST 로 읽어야 한다.
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
        if c.args and isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str):
            out.add(c.args[0].value)
    return out


def _body_mentions(fn, needle: str) -> bool:
    """함수 **코드**에 문자열이 있는가. 독스트링·주석은 제외한다.

    ast.unparse 는 주석을 애초에 버린다. 남는 것은 독스트링뿐이라 그것만 뗀다.
    get_source_segment 를 쓰면 주석까지 세어 거짓 실패가 난다 —
    diag_regime_window.price_window_ok 가 같은 함정을 밟고 고친 이력이 있다.
    """
    if fn is None:
        return False
    body = fn.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    try:
        code = "\n".join(ast.unparse(st) for st in body)
    except Exception:
        return False
    return needle in code


def _is_kwonly(fn, arg: str) -> bool:
    """`arg` 가 키워드 전용(*, arg)인가. 위치 인자면 False."""
    if fn is None:
        return False
    if any(a.arg == arg for a in fn.args.args):
        return False        # 위치로도 받힌다 — 옛 호출이 조용히 통과한다
    return any(a.arg == arg for a in fn.args.kwonlyargs)


def _has_kwdefault(fn, arg: str) -> bool:
    """키워드 전용 인자에 기본값이 달려 있는가."""
    if fn is None:
        return False
    for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        if a.arg == arg:
            return d is not None
    return False


def wiring(src: str) -> dict:
    """실제 소스에서 배선 사실만 뽑는다. 값 판정은 하지 않는다."""
    out = {
        "import_fh": False,      # fmp_http 를 임포트하는가
        "ssot_call": False,      # _fmp_price_history 가 fmp_get_ex 를 쓰는가
        "raw_get": True,         # _fmp_price_history 가 requests.get 을 쓰는가(써선 안 됨)
        "gate_exists": False,    # main() 에 MIN_FETCH_RATE 비교 분기가 있는가
        "gate_returns": False,   # 그 분기가 실제로 빠져나가는가
        "gate_before_write": False,   # 그 분기가 시트 쓰기 **앞**인가
        "gate_op": None,         # 그 분기의 비교 연산자 (Lt 여야 한다)
        "hash_sorted": False,    # universe_hash 가 sorted 를 거치는가
        "ok_guarded": False,     # ok_by_seg 수집이 d0 통과 분기 안에 있는가
        "reason_counted": False,  # _batch_fetch_history 가 사유를 센다
        # ── v2.9 창 정책 (limit → from/to) ──
        "import_fx": False,      # fmp_extras 를 임포트하는가
        "url_has_limit": True,   # _fmp_price_history 본문이 limit 을 URL 에 넣는가(넣으면 안 됨)
        "uses_range": False,     # fmp_extras 의 창 헬퍼로 from/to 를 만드는가
        "kwonly_bars": False,    # _fmp_price_history(*, bars) 키워드 전용인가
        "batch_kwonly_bars": False,   # _batch_fetch_history(*, bars) 키워드 전용인가
        "bars_defaults": True,   # 두 함수에 bars 기본값이 있는가(있으면 안 됨)
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
                if a.name == "fmp_extras":
                    out["import_fx"] = True

    f = _fn(tree, "_fmp_price_history")
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

        # ── v2.9 창 정책 ──
        # ⚠️ 파일 전역 문자열 검색으로 하면 안 된다. 이 파일의 독스트링·주석에는
        #    전환 이력을 설명하는 "limit" 이 잔뜩 들어 있어 영구 빨간불이 된다
        #    (diag_regime_window 가 실제로 그 함정을 밟았다). 함수 본문으로
        #    범위를 좁히고, ast.unparse 로 **주석을 버린 뒤** 독스트링도 뗀다.
        out["url_has_limit"] = _body_mentions(f, "limit=")
        out["uses_range"] = _calls_attr(f, "hist_range_params")
        out["kwonly_bars"] = _is_kwonly(f, "bars")

    b = _fn(tree, "_batch_fetch_history")
    if b is not None:
        for c in ast.walk(b):
            if (isinstance(c, ast.Subscript) and isinstance(c.value, ast.Name)
                    and c.value.id == "reasons"):
                out["reason_counted"] = True
        out["batch_kwonly_bars"] = _is_kwonly(b, "bars")

    # bars 기본값 금지 — 두 함수 중 **하나라도** 기본값이 있으면 True
    out["bars_defaults"] = any(
        _has_kwdefault(_fn(tree, nm), "bars")
        for nm in ("_fmp_price_history", "_batch_fetch_history"))

    h = _fn(tree, "universe_hash")
    if h is not None:
        out["hash_sorted"] = _calls_name(h, "sorted")

    r = _fn(tree, "run_backtest")
    if r is not None:
        for c in ast.walk(r):
            if not (isinstance(c, ast.If) and isinstance(c.test, ast.Compare)):
                continue
            t = c.test
            if not (isinstance(t.left, ast.Name) and t.left.id == "d0"
                    and t.ops and isinstance(t.ops[0], ast.IsNot)):
                continue
            for x in c.body:
                if _calls_attr(x, "setdefault") and _mentions(x, "ok_by_seg"):
                    out["ok_guarded"] = True

    m = _fn(tree, "main")
    if m is not None:
        gate_ln, write_ln = None, None
        for c in ast.walk(m):
            if isinstance(c, ast.If) and _mentions(c.test, "MIN_FETCH_RATE"):
                out["gate_exists"] = True
                if gate_ln is None:
                    gate_ln = c.lineno
                    if isinstance(c.test, ast.Compare) and c.test.ops:
                        out["gate_op"] = type(c.test.ops[0]).__name__
                for x in ast.walk(c):
                    if (isinstance(x, ast.Return) and isinstance(x.value, ast.Constant)
                            and x.value.value == 1):
                        out["gate_returns"] = True
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == "open_result_worksheet"):
                if write_ln is None or c.lineno < write_ln:
                    write_ln = c.lineno
        if gate_ln is not None and write_ln is not None:
            out["gate_before_write"] = gate_ln < write_ln
    return out


W = wiring(SRC)


# ══════════════════════════════════════════════════════════════════════════
print("=" * 74)
print("1) 스로틀 — FMP 호출이 fmp_http SSOT 를 거치는가")
print("=" * 74)

check("S1  fmp_http 를 임포트한다", W["import_fh"], True)
check("S2  _fmp_price_history 가 fmp_get_ex 를 호출한다", W["ssot_call"], True)
check("S3  _fmp_price_history 에 원시 requests.get 이 없다", W["raw_get"], False)
check("S4  타임아웃이 15초 이상 (1,255봉 페이로드)", bt._FMP_TIMEOUT >= 15, True)

# ── v2.9 창 정책 (limit → from/to) ─────────────────────────────────────
# 왜 여기에 두는가: 이 파일이 이미 _fmp_price_history 를 AST 로 들여다보는
# 유일한 관문이다. run_signal_backtest 는 diag_hist_window[S](run_watchlist_alerts
# 전용)·diag_hist_window_consumers[H](rotation_core 전용) 어디에도 안 들어가
# **창 정책을 지키는 관문이 하나도 없었다.** 새 파일을 만드는 대신 여기 붙인다.
#
# S6 이 왜 중요한가: FMP 는 limit 을 조용히 무시한다. 되살아나도 URL 만 바뀌고
# 데이터는 그대로다 — 에러도 경고도 없다. HISTORY_BARS 를 올렸는데 평가 구간이
# 안 늘어나는 형태로만 드러나고, 그건 사람이 알아채기 어렵다(v2.4 에서 실제로
# 놓쳤고 v2.8 에서 원인을 오진했다).
check("S5  fmp_extras(창 환산 SSOT)를 임포트한다", W["import_fx"], True)
check("S6  _fmp_price_history 코드에 limit= 이 없다", W["url_has_limit"], False)
check("S7  fmp_extras.hist_range_params 로 from/to 를 만든다", W["uses_range"], True)
check("S8  _fmp_price_history(*, bars) 가 키워드 전용", W["kwonly_bars"], True)
check("S8b _batch_fetch_history(*, bars) 가 키워드 전용", W["batch_kwonly_bars"], True)
# S9: 기본값이 있으면 호출부가 창 요구를 빠뜨려도 '그럴듯한 값'으로 메워진다.
#     SPY 와 유니버스가 서로 다른 창을 받는 갈림이 그렇게 생긴다.
check("S9  bars 인자에 기본값이 없다 (§7)", W["bars_defaults"], False)
check("S10 HISTORY_BARS 로 개명됐다 (옛 HISTORY_LIMIT 부재)",
      (hasattr(bt, "HISTORY_BARS"), hasattr(bt, "HISTORY_LIMIT")), (True, False))

# ── 선행 조건 — 2절 이하는 v2.8 심볼이 있어야 돌아간다 ────────────────────
# 없으면 AttributeError 로 죽는데, 그러면 "몇 건이 왜 실패했는지" 가 안 보인다.
# 부분 롤백 상태에서 돌렸을 때 스택 트레이스 대신 진단을 내놓아야 한다.
_NEED = ("fh", "fx", "_fmp_price_history", "_batch_fetch_history", "universe_hash",
         "_env_fetch_rate", "MIN_FETCH_RATE", "build_rt_rows", "build_result_rows",
         "HISTORY_BARS")
_MISSING = [a for a in _NEED if not hasattr(bt, a)]
if _MISSING:
    print()
    print("=" * 74)
    print("❌ 중단 — run_signal_backtest 가 v2.9 이전 버전이다")
    print("=" * 74)
    print("   없는 심볼: " + ", ".join(_MISSING))
    print("   2절 이하(분류·집계·게이트·지문·기록)는 실행할 수 없다.")
    print("   → run_signal_backtest.py 를 v2.9 로 올린 뒤 다시 돌릴 것."
          " 락스텝 쌍이다.")
    print("   실패 " + str(len(_fails) + len(_MISSING)) + "건 / 통과 "
          + str(_passes) + "건")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("2) 분류 — 실패를 사유별로 구분하는가")
print("=" * 74)


class _Resp:
    def __init__(self, payload, boom=False):
        self._p, self._boom = payload, boom

    def json(self):
        if self._boom:
            raise ValueError("broken json")
        return self._p


def _bars(n=30):
    return [{"date": f"2026-01-{(i % 28) + 1:02d}", "close": 100 + i,
             "open": 100, "high": 101 + i, "low": 99, "volume": 1000}
            for i in range(n)]


_real_get_ex = bt.fh.fmp_get_ex
_real_key = bt.FMP_API_KEY


def _probe(stub_ret, key="TESTKEY"):
    bt.FMP_API_KEY = key
    bt.fh.fmp_get_ex = lambda url, timeout=None, retries=None: stub_ret
    try:
        return bt._fmp_price_history("XYZ", bars=bt.HISTORY_BARS)
    finally:
        bt.fh.fmp_get_ex = _real_get_ex
        bt.FMP_API_KEY = _real_key


_df, _k = _probe((_Resp(_bars()), 200, "ok"))
check("C1  정상 200 → kind='ok'", _k, "ok")
check("C2  정상 200 → DataFrame 이 비어있지 않다", (not _df.empty), True)
check("C3  정상 200 → Close 열로 정규화된다", "Close" in _df.columns, True)

check("C4  429 → 'rate_limited' (이전엔 빈 DF 로 뭉개짐)",
      _probe((None, 429, "rate_limited"))[1], "rate_limited")
check("C5  402 → 'plan_limited' (재시도 무의미 — 429 와 구분 필수)",
      _probe((None, 402, "plan_limited"))[1], "plan_limited")
check("C6  4xx → 'http_error'", _probe((None, 404, "http_error"))[1], "http_error")
check("C7  네트워크 예외 → 'exception'",
      _probe((None, None, "exception"))[1], "exception")
check("C8  200 이지만 빈 배열 → 'empty'",
      _probe((_Resp([]), 200, "ok"))[1], "empty")
check("C9  200 이지만 date 열 없음 → 'empty'",
      _probe((_Resp([{"close": 1}]), 200, "ok"))[1], "empty")
check("C10 json 파싱 실패 → 'exception'",
      _probe((_Resp(None, boom=True), 200, "ok"))[1], "exception")
check("C11 키 없음 → 'no_key' (조용한 0건과 구분)",
      _probe((_Resp(_bars()), 200, "ok"), key="")[1], "no_key")
check("C12 실패 시 반환 DataFrame 은 비어 있다",
      _probe((None, 429, "rate_limited"))[0].empty, True)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("3) 집계 — 배치 페치가 사유·탈락 목록을 보존하는가")
print("=" * 74)

check("B0  사유 카운터가 실제로 존재한다 (AST)", W["reason_counted"], True)

_real_hist = bt._fmp_price_history
_TK = [f"T{i:02d}" for i in range(10)]
_PLAN = {"T00": "rate_limited", "T01": "rate_limited", "T02": "plan_limited"}


# ⚠️ 스텁 시그니처는 v2.9 실물과 **같아야** 한다. 옛 `(tk, limit=None)` 으로
#    두면 _batch_fetch_history 가 bars= 로 넘길 때 TypeError 가 나는데,
#    그 예외를 배치 루프의 `except Exception` 이 삼켜 전부 "exception" 으로
#    집계된다. 크래시가 아니라 **틀린 숫자로 조용히 실패**한다.
def _stub_hist(tk, *, bars=None):
    k = _PLAN.get(tk)
    if k:
        return pd.DataFrame(), k
    return pd.DataFrame({"Close": [1.0, 2.0]}), "ok"


bt._fmp_price_history = _stub_hist
try:
    _cache, _reasons, _failed = bt._batch_fetch_history(_TK,
                                                        bars=bt.HISTORY_BARS)
finally:
    bt._fmp_price_history = _real_hist

check("B1  성공 종목만 캐시에 담긴다", len(_cache), 7)
check("B2  사유별 카운트가 정확하다",
      (_reasons.get("ok"), _reasons.get("rate_limited"), _reasons.get("plan_limited")),
      (7, 2, 1))
check("B3  카운트 합계 = 입력 종목수 (누락 없음)", sum(_reasons.values()), 10)
check("B4  탈락 목록에 티커와 사유가 함께 남는다",
      sorted(_failed), [("T00", "rate_limited"), ("T01", "rate_limited"),
                       ("T02", "plan_limited")])
check("B5  빈 입력도 3-튜플을 돌려준다",
      len(bt._batch_fetch_history([], bars=bt.HISTORY_BARS)), 3)


# ⚠️ 여기도 v2.9 시그니처여야 한다. 옛 `(tk, limit=None)` 이면 bars= 를 받다가
#    TypeError 가 나는데, 이 스텁은 어차피 예외를 던지므로 B6 는 **통과한다** —
#    의도한 RuntimeError 가 아니라 시그니처 불일치로 통과하는 가짜 초록불이다.
def _boom_hist(tk, *, bars=None):
    raise RuntimeError("worker died")


bt._fmp_price_history = _boom_hist
try:
    _c2, _r2, _f2 = bt._batch_fetch_history(["A", "B"], bars=bt.HISTORY_BARS)
finally:
    bt._fmp_price_history = _real_hist
check("B6  워커 예외도 삼키지 않고 'exception' 으로 센다",
      (_r2.get("exception"), len(_f2)), (2, 2))



# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("4) 게이트 — 부분 유니버스가 시트 쓰기 전에 차단되는가")
print("=" * 74)

check("G1  기본 임계 0.98", bt._env_fetch_rate(""), 0.98)
check("G2  정상 입력을 그대로 쓴다", bt._env_fetch_rate("0.5"), 0.5)
check("G3  쓰레기 입력 → 기본값 폴백", bt._env_fetch_rate("abc"), 0.98)
check("G4  범위 밖(>1) → 기본값 폴백", bt._env_fetch_rate("1.5"), 0.98)
check("G5  범위 밖(<0) → 기본값 폴백", bt._env_fetch_rate("-0.1"), 0.98)
check("G6  0.0 은 유효값이다 (명시적으로 끄는 경우)", bt._env_fetch_rate("0"), 0.0)
check("G7  게이트 분기가 main() 에 존재한다", W["gate_exists"], True)
check("G8  게이트가 실제로 return 1 한다", W["gate_returns"], True)
check("G9  게이트가 시트 쓰기 **앞**에 있다 (순서가 핵심)",
      W["gate_before_write"], True)
check("G10 게이트 비교 연산자가 '<' 이다 (뒤집히면 정상 run 만 중단)",
      W["gate_op"], "Lt")
check("G11 모듈이 읽는 환경변수가 화이트리스트와 정확히 일치 "
      "(우회 스위치가 몰래 추가되면 여기서 걸린다)",
      env_names(SRC),
      {"FMP_API_KEY", "GSPREAD_KEY", "CONFIRM_DAYS", "MIN_FETCH_RATE"})


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("5) 지문 — 세그먼트별 유니버스가 실제 평가 집합을 반영하는가")
print("=" * 74)

check("H0  universe_hash 가 sorted 를 거친다 (AST)", W["hash_sorted"], True)
check("H1  입력 순서가 달라도 같은 값",
      bt.universe_hash(["B", "A", "C"]), bt.universe_hash(["C", "B", "A"]))
check("H2  set 순회 순서에도 안정",
      bt.universe_hash({"B", "A", "C"}), bt.universe_hash(["A", "B", "C"]))
check("H3  대소문자·공백 정규화",
      bt.universe_hash([" a ", "b"]), bt.universe_hash(["A", "B"]))
check("H4  구성이 다르면 값이 다르다",
      bt.universe_hash(["A", "B"]) != bt.universe_hash(["A", "B", "C"]), True)
check("H5  빈 목록은 빈 문자열", bt.universe_hash([]), "")
check("H6  길이 9 (접두어 1 + 16진수 8)", len(bt.universe_hash(["A"])), 9)
# ⚠️ USER_ENTERED 쓰기에서 전부 숫자인 해시는 수로 해석돼 앞자리 0 이 날아간다.
#    16진수 8자가 전부 숫자일 확률은 (10/16)^8 ≈ 2.3% — 드물어서 더 위험하다.
#    무작위 표본으로 '어떤 입력에도 숫자로 안 보이는가' 를 확인한다.
_samples = [bt.universe_hash([f"T{i}", f"Z{i * 7}"]) for i in range(400)]
check("H13 어떤 해시도 숫자로 해석되지 않는다 (앞자리 0 소실 방지)",
      any(x.lstrip("+-").replace(".", "", 1).isdigit() for x in _samples), False)
check("H14 표본이 실제로 다양하다 (양성대조 — 상수 반환이면 여기서 걸린다)",
      len(set(_samples)) > 390, True)


def _synth(n, seed):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    step = rng.normal(0.0006, 0.012, n)
    close = 100 * np.exp(np.cumsum(step))
    return pd.DataFrame({"Close": close, "Open": close,
                         "High": close * 1.01, "Low": close * 0.99,
                         "Volume": rng.integers(1e6, 5e6, n)}, index=idx)


_hc = {"AAA": _synth(260, 1), "BBB": _synth(260, 2),
       "EEE": _synth(260, 3), "SHORT": _synth(100, 4)}
_segmap = {"AAA": "stock", "BBB": "stock", "EEE": "etf", "SHORT": "stock"}
_aggs, _rt, _meta = bt.run_backtest(sorted(_hc), _synth(260, 9), _hc,
                                    segment_map=_segmap)

check("H7  d0 미달 종목(봉 부족)은 유니버스에서 빠진다",
      _meta["seg_size"]["all"], 3)
check("H8  세그먼트별 크기가 분리된다",
      (_meta["seg_size"]["stock"], _meta["seg_size"]["etf"]), (2, 1))
check("H9  stock 해시는 개별주 집합만 반영 (ETF 가 섞이지 않는다)",
      _meta["seg_hash"]["stock"], bt.universe_hash(["AAA", "BBB"]))
check("H10 all 해시는 stock 해시와 다르다 (전역 해시로 퉁치지 않음)",
      _meta["seg_hash"]["all"] != _meta["seg_hash"]["stock"], True)
check("H11 탈락 종목이 해시에 안 들어간다",
      _meta["seg_hash"]["stock"] != bt.universe_hash(["AAA", "BBB", "SHORT"]), True)
check("H12 ok_by_seg 수집이 d0 통과 분기 안에 있다 (AST)", W["ok_guarded"], True)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("6) 기록 — 결과 행에 지문이 실리는가")
print("=" * 74)

check("R1  _RESULT_COLS 에 Universe_Hash 존재",
      "Universe_Hash" in bt._RESULT_COLS, True)
check("R2  _RT_COLS 에 Universe_Hash 존재", "Universe_Hash" in bt._RT_COLS, True)

_rows = bt.build_result_rows({"entry": {"count": 3, "winrate_20d": 50.0}},
                             "2026-08-26", "2022-01-01", "2026-08-26", 85,
                             buckets=("entry",), mode="verdict", segment="stock",
                             uni_hash="deadbeef")
check("R3  행 길이가 열 개수와 일치", len(_rows[0]), len(bt._RESULT_COLS))
check("R4  Universe_Hash 칸에 값이 들어감",
      _rows[0][bt._RESULT_COLS.index("Universe_Hash")], "deadbeef")
check("R5  Universe_Size 칸에 세그먼트 값이 들어감",
      _rows[0][bt._RESULT_COLS.index("Universe_Size")], 85)
check("R6  Mode/Segment/Confirm_Days 가 밀리지 않았다",
      (_rows[0][bt._RESULT_COLS.index("Mode")],
       _rows[0][bt._RESULT_COLS.index("Segment")],
       _rows[0][bt._RESULT_COLS.index("Confirm_Days")]),
      ("verdict", "stock", bt.CONFIRM_DAYS))

_rtr = bt.build_rt_rows({("swing", "stock", "all"): {"trades": 5},
                         ("swing", "etf", "all"): {"trades": 2}},
                        "2026-08-26", "2022-01-01", "2026-08-26", 999,
                        seg_size={"stock": 85, "etf": 389},
                        seg_hash={"stock": "aaaa1111", "etf": "bbbb2222"})
check("R7  RT 행 길이가 열 개수와 일치", len(_rtr[0]), len(bt._RT_COLS))
_ix_seg, _ix_sz = bt._RT_COLS.index("Segment"), bt._RT_COLS.index("Universe_Size")
_ix_hs = bt._RT_COLS.index("Universe_Hash")
_by_seg = {r[_ix_seg]: (r[_ix_sz], r[_ix_hs]) for r in _rtr}
check("R8  RT 행이 세그먼트별 크기/해시를 쓴다 (전역값 999 가 아님)",
      _by_seg, {"stock": (85, "aaaa1111"), "etf": (389, "bbbb2222")})
check("R9  seg_size 미제공 시 전역값으로 폴백 (구 호출부 호환)",
      bt.build_rt_rows({("swing", "stock", "all"): {"trades": 1}},
                       "d", "s", "e", 777)[0][_ix_sz], 777)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("7) 뮤테이션 — 결함을 심으면 위 검사가 빨간불이어야 한다")
print("=" * 74)

# 게이트 블록 원문 — M3(return 제거)·M8(순서 이동)이 공유한다.
_GATE = """    if _rate < MIN_FETCH_RATE:
        print(f"[ABORT] 페치 성공률 {_rate * 100:.1f}% < 임계 "
              f"{MIN_FETCH_RATE * 100:.1f}% — 시트에 기록하지 않고 중단한다.")
        print("[ABORT] 위 \'사유별\' 을 볼 것. rate_limited 가 많으면 "
              "FMP_RATE_LIMIT_PER_MIN 을 낮추고, exception 이 많으면 "
              "_FMP_TIMEOUT 을 늘린다.")
        return 1
"""
_GATE_NORET = _GATE.replace("        return 1\n", "")

WIRE_MUTANTS = [
    ("M1 스로틀 우회 — 원시 requests.get 으로 되돌림",
     "    r, _status, kind = fh.fmp_get_ex(url, timeout=_FMP_TIMEOUT)",
     "    import requests\n"
     "    r = requests.get(url, timeout=_FMP_TIMEOUT)\n"
     "    kind = 'ok' if r.status_code == 200 else 'http_error'",
     ("ssot_call", "raw_get")),
    ("M2 게이트 부등호 뒤집기 — 정상 run 만 중단",
     "    if _rate < MIN_FETCH_RATE:",
     "    if _rate > MIN_FETCH_RATE:",
     ("gate_op",)),
    ("M3 게이트가 빠져나가지 않음 — 경고만 찍고 계속 진행",
     _GATE, _GATE_NORET, ("gate_returns",)),
    ("M4 사유 집계 삭제 — 탈락 이유가 사라짐",
     "            reasons[kind] = reasons.get(kind, 0) + 1",
     "            pass",
     ("reason_counted",)),
    ("M5 해시 정렬 제거 — run 마다 값이 달라짐",
     "    seq = sorted(str(t).strip().upper() for t in (tickers or [])",
     "    seq = list(str(t).strip().upper() for t in (tickers or [])",
     ("hash_sorted",)),
    ("M6 d0 가드 제거 — 탈락 종목까지 유니버스에 포함",
     "            ok_by_seg.setdefault(_seg, []).append(tk)",
     "        ok_by_seg.setdefault(_seg, []).append(tk)",
     ("ok_guarded",)),
    ("M7 fmp_http 임포트 제거",
     "import fmp_http as fh     # noqa: E402",
     "fh = None",
     ("import_fh",)),
]

for name, old, new, keys in WIRE_MUTANTS:
    n_occ = SRC.count(old)
    if n_occ != 1:
        print("  ⚠️  " + name + " — 앵커가 " + str(n_occ) + "회(1회여야 함). 스킵")
        _fails.append(name + " [앵커 " + str(n_occ) + "회]")
        continue
    mw = wiring(SRC.replace(old, new, 1))
    flipped = [k for k in (keys or W.keys()) if mw.get(k) != W.get(k)]
    if flipped:
        print("  ✅ " + name + " — " + ", ".join(flipped) + " 가 뒤집힘")
        _passes += 1
    else:
        print("  ❌ " + name + " — **배선 검사가 잡아내지 못함**")
        _fails.append(name)

# ── 7b. 게이트 순서 — 시트 쓰기 뒤로 옮기면 잡히는가 ──────────────────────
# 존재 검사만으로는 못 잡는 유형. 게이트 블록을 통째로 잘라 쓰기 뒤에 붙인다.
_ANCHOR = '    print(f"[DONE] {time.time() - t0:.1f}s")'
if SRC.count(_GATE) == 1 and SRC.count(_ANCHOR) == 1:
    _moved = SRC.replace(_GATE, "", 1).replace(_ANCHOR, _GATE + _ANCHOR, 1)
    _mw = wiring(_moved)
    if _mw.get("gate_exists") and not _mw.get("gate_before_write"):
        print("  ✅ M8 게이트를 시트 쓰기 뒤로 이동 — 순서 검사가 잡아냄")
        _passes += 1
    else:
        print("  ❌ M8 게이트를 시트 쓰기 뒤로 이동 — **잡아내지 못함**")
        _fails.append("M8 게이트 순서")
else:
    print("  ⚠️  M8 — 앵커 없음. 스킵")
    _fails.append("M8 [앵커 소실]")

# ── 7c. 양성대조 — 분류/집계 검사가 정말 판별력이 있는가 ─────────────────
# 모든 실패를 빈 DataFrame 하나로 뭉개는 옛 동작을 재현해, 2·3절이 실제로
# 깨지는지 본다. "항상 통과하는 스위트" 방지.
def _old_style(tk, *, bars=None):
    return pd.DataFrame(), "ok"      # 옛 코드: 실패해도 사유가 남지 않음


bt._fmp_price_history = _old_style
try:
    _c3, _r3, _f3 = bt._batch_fetch_history(_TK, bars=bt.HISTORY_BARS)
finally:
    bt._fmp_price_history = _real_hist
check("P1  양성대조: 사유를 뭉개면 B2(사유별 카운트)가 깨진다",
      _r3.get("rate_limited"), None)
check("P2  양성대조: 그때 캐시는 0건인데 사유는 전부 ok 로 보인다",
      (len(_c3), _r3.get("ok")), (0, 10))


# ── P3 하네스 자기검증 — 스텁이 실물 시그니처와 호환인가 ───────────────────
# 왜 필요한가: 스텁이 옛 `(tk, limit=None)` 이면 _batch_fetch_history 가 bars= 로
# 부를 때 TypeError 가 나는데, 배치 루프의 `except Exception` 이 그것을 삼켜
# 전부 "exception" 으로 집계한다. B1/B2 는 틀린 숫자로 실패하고(그건 잡힌다),
# **B6 는 오히려 통과한다** — 의도한 RuntimeError 가 아니라 시그니처 불일치로
# 통과하는 가짜 초록불이다. 2026-09-03 뮤테이션 N9 에서 실측 확인했다.
# 검사가 아니라 검사 도구를 검사하는 항목이다.
def _stub_sig_ok(fn) -> bool:
    try:
        inspect.signature(fn).bind("X", bars=1)
        return True
    except TypeError:
        return False


check("P3  스텁 3종이 실물 (tk, *, bars) 시그니처와 호환",
      tuple(_stub_sig_ok(f) for f in (_stub_hist, _boom_hist, _old_style)),
      (True, True, True))


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
if _fails:
    print("❌ 실패 " + str(len(_fails)) + "건 / 통과 " + str(_passes) + "건")
    for x in _fails:
        print("   - " + str(x))
    sys.exit(1)
print("✅ 전부 통과 — " + str(_passes) + "건")
print("=" * 74)
