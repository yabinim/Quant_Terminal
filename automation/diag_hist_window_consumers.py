#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_hist_window_consumers.py — historical-price-eod 소비처 4곳의 조회 창 회귀.

무엇을 지키는가
---------------
`fmp_extras._closes` · `run_hidden_alpha._fmp_price_history_close` ·
`run_narrative._fmp_close_series` · `run_drg_verify.verify_prediction` 이
`limit`(무시되는 파라미터) 대신 `from`/`to` 창을 쓰고, 그 창이 **하류가 실제로
소비하는 꼬리 깊이**를 덮는지 본다.

왜 별도 스위트인가
------------------
`diag_hist_window` 의 `[D]` 군은 `run_drg_predict` 전용이다. 하네스(스텁 FMP·
AST 헬퍼)는 그대로 쓸 수 있으므로 **복제하지 않고 import 한다.**
`diag_hist_window` 는 모듈 최상단에서 환경변수 기본값을 세팅하고 함수/클래스만
정의하므로 임포트 부작용이 없다(스위트 실행은 `if __name__ == "__main__"` 안).

이 파일의 실패 모드는 전부 **조용한 값 오류**다 — 셋 다 정적 분석만으로는 안 보인다.

  1. run_narrative 의 MA200. `s.rolling(200, min_periods=150)` 이
     `if len(s) >= 150` 가드로 감싸여 있다. 창이 130봉이면 가드가 실패해
     `above_ma200=None` 이 되고 verdict 분기 "❌ 하락 추세 (대기)" 가 통째로
     도달 불가가 된다. 150~199봉이면 더 나쁘다 — **에러 없이** 더 짧은 창의
     평균이 나온다. 그래서 E2 는 창 길이를 문자열로 재지 않고
     **`above_ma200` 이 None 이 아님을 관측**한다.
  2. run_drg_verify 의 앵커. 이 창만 기준점이 오늘이 아니라 **pred_date** 다.
     미검증 예측이 며칠 밀려 있으면(주말·휴장·워크플로 실패) 오늘 기준 창은
     pred_date 봉을 안 담고 `hist_on_pred.empty` 로 빠져 그 예측은 **영구
     미검증**으로 남는다. 예외도 로그도 없다.
  3. 요구 봉수를 옛 `limit` 숫자에서 가져오는 것. `limit` 은 무시돼 왔으므로
     그 숫자는 **아무도 검증한 적이 없다.** run_narrative 가 실증이다 —
     limit 은 130 인데 실요구는 200봉이었다.

요구 봉수는 어디서 오는가
-------------------------
저장소 어디에도 상수로 적지 않는다. **소비 구문에서 AST 로 역산**한다
(`diag_hist_window` `[D] D3` 가 표를 하드코딩한 한계를 여기서는 반복하지 않는다).
`R` 군이 역산치와 호출부 선언치(`bars=`)를 대조한다.

사용법
------
    python diag_hist_window_consumers.py

네트워크 접근 없음 — 런타임 군은 전부 스텁 FMP 위에서 돈다.
단, 원시 `requests.get` 이 한 곳이라도 남아 있으면 스텁이 그 경로를 **가로채지
못한다.** 그래서 `C-STOP` 이 정적 배선 실패 시 런타임 군을 조기 종료한다.
"""

import os
import sys
import ast
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# diag_hist_window 가 환경변수 기본값을 세팅한다(run_* 모듈들이 모듈 최상단에서
# os.environ[...] 로 읽으므로 없으면 임포트가 KeyError 로 죽는다).
import diag_hist_window as dhw

import fmp_http as fh
import fmp_extras as fx
import run_narrative as nrt
import run_drg_verify as drv
import run_hidden_alpha as hid
import run_earnings_watch as rew
import earnings_core as ec

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
_TREES = {}


def tree(mod) -> ast.Module:
    if mod.__name__ not in _TREES:
        with open(mod.__file__, encoding="utf-8") as f:
            _TREES[mod.__name__] = ast.parse(f.read())
    return _TREES[mod.__name__]


def find_func(t: ast.Module, name: str):
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def local_funcs(t: ast.Module) -> dict:
    return {n.name: n for n in ast.walk(t)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _const_int(node, consts: dict):
    """정수로 접을 수 있으면 접는다. 못 접으면 None.

    `iloc[-(lookback_days + 1)]` 처럼 첨자가 식인 경우를 위해 필요하다.
    consts 는 이름 → 정수 리터럴(호출부에서 넘어온 인자값).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Name):
        v = consts.get(node.id)
        return v if isinstance(v, int) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = _const_int(node.operand, consts)
        return None if v is None else -v
    if isinstance(node, ast.BinOp):
        l, r = _const_int(node.left, consts), _const_int(node.right, consts)
        if l is None or r is None:
            return None
        if isinstance(node.op, ast.Add):
            return l + r
        if isinstance(node.op, ast.Sub):
            return l - r
        if isinstance(node.op, ast.Mult):
            return l * r
    return None


def depth(node, env: dict, consts: dict) -> int:
    """표현식이 요구하는 꼬리 **오프셋**(요구 봉수 = 오프셋 + 1).

    `dhw.offset_of` 를 감싸서 `.iloc[-N]` 층만 얹는다. dhw 쪽은 Subscript 를
    만나면 첨자를 버리고 base 로 내려간다(`return offset_of(node.value, ...)`).
    rolling/tail 만 보면 됐던 run_drg_predict 에는 충분했지만, 여기 소비처들은
    **첨자 안에 요구 깊이가 들어 있다**(`spy_close.iloc[-64]`).
    환산 로직(rolling·tail·shift·pct_change)은 복제하지 않고 dhw 것을 그대로 쓴다.
    """
    best = dhw.offset_of(node, env, consts)
    for n in ast.walk(node):
        if (isinstance(n, ast.Subscript)
                and isinstance(n.value, ast.Attribute)
                and n.value.attr == "iloc"):
            v = _const_int(n.slice, consts)
            if v is not None and v < 0:
                best = max(best, -v - 1)      # iloc[-1] → 오프셋 0 (1봉)
    return best


def _names_in(node) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def required_bars_for(func, seeds: set, t: ast.Module,
                      consts: dict | None = None, _seen=None) -> int:
    """`seeds` 로 흘러 들어간 시리즈가 요구하는 봉 수.

    누산 골격은 `dhw.required_bars_in` 과 동형이다 — 대입을 따라가며 별칭에
    오프셋을 전파하고 최댓값을 취한다. 다만 두 가지가 달라 그대로 못 쓴다:

      · 오프셋 계산기를 `depth()` 로 갈아끼워야 한다(iloc 첨자).
      · **변수별**로 봐야 한다. 한 함수가 요구가 다른 두 시리즈를 함께 쓴다
        (verify_emerging_with_quant 의 `spy_close` 64봉 vs `s` 200봉).
        함수 전체 최댓값을 쓰면 SPY 호출부가 과다 선언으로 오판된다.

    dhw 쪽에 계산기 훅이 없어서 골격만 재현했다. 두 구현이 갈리는 것을 막으려고
    `R0` 이 iloc 없는 함수에서 둘의 답을 대조한다.

    seeds 로부터의 **오염 전파**: 대입의 우변이 오염된 이름을 참조하면 좌변도
    오염된다. 지역 함수에 오염된 인자가 들어가면 그 함수 안으로 따라 들어간다
    (`calculate_period_return(s, 21)` · `_trailing_return(s, 126)`).
    """
    consts = dict(consts or {})
    _seen = _seen or set()
    # ⚠️ consts 를 키에 넣어야 한다. 빼면 같은 함수를 다른 인자로 두 번 부르는
    #    경우(`calculate_period_return(s, 5)` 와 `(s, 21)`)에 **뒤 호출이 단락**되고,
    #    walk 순서에 따라 5봉짜리 답이 남는다. 1차 작성에서 실제로 22 대신 6 이
    #    나왔다 — 스위트가 자기 버그를 먼저 잡은 사례.
    key = (id(func), tuple(sorted(seeds)), tuple(sorted(consts.items())))
    if key in _seen:
        return 1
    _seen.add(key)

    funcs = local_funcs(t)
    env, tainted, worst = {}, set(seeds), 1

    for stmt in ast.walk(func):
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)):
            off = depth(stmt.value, env, consts)
            env[stmt.targets[0].id] = off
            if _names_in(stmt.value) & tainted:
                tainted.add(stmt.targets[0].id)
                worst = max(worst, off + 1)

    # 오염된 이름이 등장하는 모든 표현식 + 지역 함수로의 전달
    for n in ast.walk(func):
        if isinstance(n, ast.Call):
            fn = n.func
            fname = fn.id if isinstance(fn, ast.Name) else None
            if fname in funcs:
                sub = funcs[fname]
                params = [a.arg for a in sub.args.args]
                sub_seeds, sub_consts = set(), {}
                for i, a in enumerate(n.args):
                    if i >= len(params):
                        break
                    if isinstance(a, ast.Name) and a.id in tainted:
                        sub_seeds.add(params[i])
                    v = _const_int(a, consts)
                    if v is not None:
                        sub_consts[params[i]] = v
                for kw in n.keywords:
                    if kw.arg is None:
                        continue
                    if isinstance(kw.value, ast.Name) and kw.value.id in tainted:
                        sub_seeds.add(kw.arg)
                    v = _const_int(kw.value, consts)
                    if v is not None:
                        sub_consts[kw.arg] = v
                if sub_seeds:
                    worst = max(worst, required_bars_for(
                        sub, sub_seeds, t, sub_consts, _seen))
                continue
        if isinstance(n, (ast.Subscript, ast.Call, ast.Attribute, ast.Compare)):
            if _names_in(n) & tainted:
                worst = max(worst, depth(n, env, consts) + 1)

    return worst


def _docstring_ids(t: ast.Module) -> set:
    """독스트링 노드의 id 집합.

    ⚠️ 작성 중 실제로 이것 때문에 오탐했다. 이 커밋의 독스트링들이
       "`limit=130` → from/to 창" 같은 **변경 이력**을 적고 있어서, 문자열
       상수를 무차별로 훑으면 주석이 코드로 잡힌다. 검사기가 자기가 만든
       문서를 결함으로 신고하는 셈이라, 통과시키려면 문서를 지워야 한다 —
       정확히 반대 방향의 압력이다.
    """
    out = set()
    for n in ast.walk(t):
        body = getattr(n, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def _manual_from_stmts(t: ast.Module) -> set:
    """historical-price-eod URL 을 품은 **문 전체**에 손으로 쓴 from= 이 있는가.

    `hist_url_nodes` 는 문자열 노드 단위라 연결 체인의 옆 항을 못 본다.
    여기서는 URL 조각을 포함하는 최상위 문을 통째로 unparse 해서 검사한다.
    `hist_range_params` 가 만든 from 은 소스에 리터럴로 나타나지 않으므로
    오탐하지 않는다 — 리터럴 `from=` 이 보이면 그건 손으로 쓴 것이다.
    """
    docs = _docstring_ids(t)
    hits = set()
    for n in ast.walk(t):
        if not isinstance(n, (ast.Assign, ast.Expr, ast.Return, ast.AugAssign)):
            continue
        try:
            src = ast.unparse(n)
        except Exception:
            continue
        # 독스트링 배제. `docs` 는 **Constant 노드**의 id 라 Expr 노드와는 안 맞는다
        # (1차 작성에서 fmp_extras.hist_range_params 의 독스트링이 오탐했다 —
        #  그 독스트링에 "historical-price-eod" 와 "&from=" 이 둘 다 들어 있다).
        # 문자열만 있는 Expr 은 URL 조립일 수 없으므로 통째로 뺀다.
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str):
            continue
        if id(n) in docs or "historical-price-eod" not in src:
            continue
        if "&from=" in src or "?from=" in src:
            hits.add(n.lineno)
    return hits


def hist_url_nodes(t: ast.Module):
    """historical-price-eod **URL** 을 담은 문자열 노드들 — f-string 과 상수 양쪽.

    `dhw.hist_urls` 는 JoinedStr(f-string)만 본다. run_drg_verify 는 전환 후
    URL 조각이 순수 상수("historical-price-eod/full?symbol=")라 그쪽 탐지기로는
    통째로 안 보인다. 그래서 Constant 도 함께 본다.

    산문과 URL 을 가르는 기준은 **쿼리 시작(`/full?`)** 이다. 독스트링은
    별도로 배제한다(위 `_docstring_ids` 주석 참조).
    반환: [(lineno, 상수부 합친 문자열)]
    """
    docs = _docstring_ids(t)
    # ⚠️ f-string 안의 조각도 Constant 다. 배제하지 않으면 URL 하나가 **두 번**
    #    세어진다(JoinedStr 1 + 내부 Constant 1). 1차 작성에서 C2a 가 그렇게
    #    "URL 2건 vs hist_range_params 1회" 로 오탐했다.
    inner = {id(v) for n in ast.walk(t) if isinstance(n, ast.JoinedStr)
             for v in ast.walk(n) if isinstance(v, ast.Constant)}
    out = []
    for n in ast.walk(t):
        if isinstance(n, ast.JoinedStr):
            lit = "".join(v.value for v in n.values
                          if isinstance(v, ast.Constant) and isinstance(v.value, str))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            if id(n) in docs or id(n) in inner:
                continue
            lit = n.value
        else:
            continue
        if "historical-price-eod/full?" in lit:
            out.append((n.lineno, lit))
    return out


def has_import(t: ast.Module, name: str) -> bool:
    for n in ast.walk(t):
        if isinstance(n, ast.Import):
            if any(a.name.split(".")[0] == name for a in n.names):
                return True
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == name:
            return True
    return False


def calls_to(t: ast.Module, name: str):
    """`foo(...)` · `mod.foo(...)` 양쪽을 잡는다."""
    hits = []
    for n in ast.walk(t):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == name:
            hits.append(n)
        elif isinstance(f, ast.Attribute) and f.attr == name:
            hits.append(n)
    return hits


def kw_int(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return _const_int(kw.value, {})
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 검사 대상 — (모듈, 정의 함수, 호출부 함수, [(선언 bars, 결과 변수들)])
# ══════════════════════════════════════════════════════════════════════════════
_MODULES = {"fmp_extras": fx, "run_hidden_alpha": hid,
            "run_narrative": nrt, "run_drg_verify": drv,
            "run_earnings_watch": rew}
# run_earnings_watch 는 `bars=` 인자가 아니라 **모듈 상수 하나**로 창을 정한다
# (호출부 6곳이 hist_cache 를 공유하므로 창이 여러 벌이면 캐시 선점 순서에 따라
#  이력 깊이가 달라진다). 그래서 _DEFS/_SITES 에는 넣지 않고, 정적 배선(C)과
# 전용 군 [Q] 로 본다.

# 창을 만드는 정의부 — 여기에 기본값이 있으면 호출부가 요구를 안 밝혀도 통과한다.
_DEFS = [
    ("fmp_extras", "_closes", "bars"),
    ("run_hidden_alpha", "_fmp_price_history_close", "bars"),
    ("run_hidden_alpha", "_fmp_batch_close_df", "bars"),   # 중간 계층도 마찬가지
    ("run_narrative", "_fmp_close_series", "bars"),
]

# 요구 봉수를 역산할 지점 — (모듈, 소비 함수, 창을 만드는 함수, 결과 변수들)
#
# ⚠️ 선언치(`bars=` 숫자)를 여기 적지 않는다. 적으면 진단이 정답을 들고 있는
#    셈이라, 소스의 `bars=200` 을 130 으로 바꿔도 진단은 여전히 200 과 대조해
#    **통과한다.** 그게 `diag_hist_window [D] D3` 가 남긴 한계였고(§9-3),
#    여기서는 반복하지 않는다. 양쪽 다 소스에서 뽑는다:
#      · 요구치 → 소비 구문(rolling·tail·iloc)에서 역산
#      · 선언치 → 호출부의 `bars=` 키워드에서 판독
#    두 경로는 서로 독립이므로 대조가 공허해지지 않는다.
_SITES = [
    ("fmp_extras", "compute_satellite_top10", "_closes", {"spy"}),
    ("fmp_extras", "compute_satellite_top10", "_closes", {"s"}),
    ("run_narrative", "verify_emerging_with_quant", "_fmp_close_series", {"spy_close"}),
    ("run_narrative", "verify_emerging_with_quant", "_fmp_close_series", {"s", "vol"}),
    ("run_hidden_alpha", "build_ranked_table", "_fmp_batch_close_df", {"close_df"}),
]


def _assign_targets(stmt) -> set:
    """대입문의 좌변 이름들. `s, vol = f(...)` 같은 튜플 언패킹도 본다."""
    out = set()
    for t in stmt.targets:
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            out |= {e.id for e in t.elts if isinstance(e, ast.Name)}
    # `spy_close, _ = _fmp_close_series(...)` — 버리는 자리는 요구를 만들지
    # 않으므로 대조 대상에서 뺀다. 빼지 않으면 좌변 집합이 seeds 와 안 맞아
    # 선언치 판독이 통째로 실패한다(1차 작성에서 실제로 그랬다).
    out.discard("_")
    return out


def declared_bars(func, defname: str, seeds: set):
    """호출부 소스에서 `bars=` 선언치를 읽는다. 못 찾으면 None."""
    for stmt in ast.walk(func):
        if not isinstance(stmt, ast.Assign):
            continue
        if _assign_targets(stmt) != set(seeds):
            continue
        v = stmt.value
        if not isinstance(v, ast.Call):
            continue
        f = v.func
        nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if nm != defname:
            continue
        return kw_int(v, "bars")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# [C] 정적 배선 — limit 부재 · from/to 결합 · 기본값 금지 · 정책 우회 금지
# ══════════════════════════════════════════════════════════════════════════════
def group_C():
    print("\n[C] 정적 배선")

    for name, mod in _MODULES.items():
        t = tree(mod)
        urls = hist_url_nodes(t)
        chk(bool(urls), "C1a", f"{name}: historical-price-eod URL 을 찾았다 ({len(urls)}건)")
        bad_limit = [ln for ln, lit in urls if "limit=" in lit]
        chk(not bad_limit, "C1b",
            f"{name}: URL 에 limit= 없음"
            + (f" — 잔존 {bad_limit}" if bad_limit else ""))

    # from/to 는 fx.hist_range_params 가 유일하게 만든다. 손으로 &from= 을 쓰면
    # 마진 정책이 그 자리에 복제된 것이므로 잡아야 한다.
    for name, mod in _MODULES.items():
        t = tree(mod)
        n_rp = len(calls_to(t, "hist_range_params"))
        n_url = len(hist_url_nodes(t))
        chk(n_rp >= n_url, "C2a",
            f"{name}: hist_range_params {n_rp}회 ≥ URL {n_url}건")
        # ⚠️ URL 노드 하나만 보면 안 된다. `f"...?symbol={t}" + "&from=..."` 처럼
        #    **연결 체인의 다른 항**에 붙이면 URL 노드의 리터럴에는 from= 이 없다.
        #    변이 M4 가 이 사각지대를 실증했다(C2a 가 대신 잡아 가려져 있었다).
        #    URL 노드를 품은 대입/호출문 전체를 unparse 해서 본다.
        manual = sorted({ln for ln, lit in hist_url_nodes(t)
                         if "&from=" in lit or "?from=" in lit}
                        | _manual_from_stmts(t))
        chk(not manual, "C2b",
            f"{name}: 손으로 쓴 &from= 없음" + (f" — {manual}" if manual else ""))

    # 원시 requests 가 남아 있으면 아래 런타임 군의 스텁이 그 경로를 못 가로챈다.
    for name in ("run_narrative", "run_drg_verify", "run_hidden_alpha",
                 "run_earnings_watch"):
        t = tree(_MODULES[name])
        chk(not has_import(t, "requests"), "C3",
            f"{name}: import requests 없음 (래칫 — 되살리려면 임포트부터 다시)")

    # 기본값 금지. 기본값이 있으면 호출부가 자기 요구를 안 밝혀도 통과하고
    # '어느 요구인지 모르는 창'이 생긴다. 단일 문자 변이라 파일 크기도 안 변한다.
    for name, fname, arg in _DEFS:
        t = tree(_MODULES[name])
        f = find_func(t, fname)
        if f is None:
            bad("C4a", f"{name}.{fname}: 정의를 못 찾았다")
            continue
        args = [a.arg for a in f.args.args]
        n_def = len(f.args.defaults)
        pos = args.index(arg) if arg in args else -1
        chk(pos >= 0, "C4a", f"{name}.{fname}: 인자 {arg} 존재")
        if pos >= 0:
            has_default = pos >= len(args) - n_def
            chk(not has_default, "C4b",
                f"{name}.{fname}({arg}): 기본값 없음")
        chk("limit" not in args, "C4c",
            f"{name}.{fname}: 옛 인자 limit 제거됨")

    # 호출부는 bars 를 키워드로 명시한다(위치 인자면 요구가 안 보인다).
    for name in ("fmp_extras", "run_hidden_alpha", "run_narrative"):
        t = tree(_MODULES[name])
        targets = {d[1] for d in _DEFS if d[0] == name}
        miss = []
        for tgt in targets:
            for c in calls_to(t, tgt):
                if not any(kw.arg == "bars" for kw in c.keywords):
                    miss.append((tgt, c.lineno))
        chk(not miss, "C5",
            f"{name}: 호출부 전부 bars= 명시" + (f" — 누락 {miss}" if miss else ""))

    # 순수 환산기 직접 호출 금지 — 마진·하한 정책을 우회하는 경로다(X5b 와 동형).
    # fmp_extras 는 정책 **소유자**이므로 면제한다(fmp_http 가 A1 에서 면제되는 것과 같다).
    for name, mod in _MODULES.items():
        if name == "fmp_extras":
            continue
        t = tree(mod)
        direct = [c.lineno for c in calls_to(t, "calendar_days_for_bars")]
        chk(not direct, "C6",
            f"{name}: 순수 환산기 직접 호출 없음" + (f" — {direct}" if direct else ""))
        floats = {n.value for n in ast.walk(t)
                  if isinstance(n, ast.Constant) and isinstance(n.value, float)}
        chk(fx.HIST_TD_PER_CD not in floats, "C7",
            f"{name}: {fx.HIST_TD_PER_CD} 리터럴 복제 없음")

    # run_drg_verify 만 앵커가 다르다 — 정적으로도 못 박는다.
    t = tree(drv)
    anchored = [c for c in calls_to(t, "hist_range_params")
                if any(kw.arg == "today" for kw in c.keywords)]
    chk(len(anchored) == 1, "C8a",
        f"run_drg_verify: hist_range_params(today=) 앵커 {len(anchored)}건 (1이어야 함)")
    f = find_func(t, "verify_prediction")
    dead = [n.targets[0].id for n in ast.walk(f)
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in ("start_date", "end_date")] if f else []
    chk(not dead, "C8b",
        "run_drg_verify: 죽은 start_date/end_date 제거됨" + (f" — {dead}" if dead else ""))


# ══════════════════════════════════════════════════════════════════════════════
# [Q] run_earnings_watch 의 분기 요구 — earnings_core 와의 락스텝
# ══════════════════════════════════════════════════════════════════════════════
def group_Q():
    """창이 `past_earnings_dates` 가 실제로 돌려주는 이벤트 수를 덮는가.

    ⚠️ 여기서 `12` 를 리터럴로 쓰지 않는다. 쓰면 진단이 정답을 들고 있는 셈이라
       run_earnings_watch 의 `GAP_QUARTERS + 4` 를 `GAP_QUARTERS` 로 바꿔도
       진단은 여전히 12 와 대조해 **통과한다**. 요구치는 earnings_core 의
       시그니처 기본값에서, 선언치는 run_earnings_watch 의 상수에서 —
       서로 다른 파일에서 독립으로 뽑아 대조한다.
    """
    print("\n[Q] 실적 레이더 분기 요구")

    # 요구치: past_earnings_dates(limit=...) 의 기본값을 AST 로 판독
    t_ec = tree(ec)
    f = find_func(t_ec, "past_earnings_dates")
    want = None
    if f is not None:
        names = [a.arg for a in f.args.args]
        if "limit" in names:
            pos = names.index("limit")
            n_def = len(f.args.defaults)
            if pos >= len(names) - n_def:
                want = _const_int(f.args.defaults[pos - (len(names) - n_def)],
                                  {"GAP_QUARTERS": ec.GAP_QUARTERS})
    chk(want is not None, "Q1a",
        f"earnings_core.past_earnings_dates: limit 기본값 판독 ({want})")

    # 선언치: run_earnings_watch 의 모듈 상수
    got = getattr(rew, "_HIST_QUARTERS", None)
    chk(isinstance(got, int), "Q1b", f"run_earnings_watch._HIST_QUARTERS 존재 ({got})")

    if want is not None and isinstance(got, int):
        chk(got >= want, "Q2",
            f"창 분기수 {got} ≥ past_earnings_dates 이벤트 수 {want}")

    # 호출부가 limit 을 직접 넘기지 않는가 — 넘기면 위 대조가 공허해진다.
    t_rew = tree(rew)
    overridden = [c.lineno for c in calls_to(t_rew, "past_earnings_dates")
                  if any(kw.arg == "limit" for kw in c.keywords)]
    chk(not overridden, "Q3",
        "run_earnings_watch: past_earnings_dates(limit=) 미지정(기본값 사용)"
        + (f" — 지정됨 {overridden}" if overridden else ""))

    # 공급 봉수가 요구를 덮는가. 요구 = 12분기 도달 + 거래량 기준 + 직전 종가.
    days = getattr(rew, "_HIST_DAYS", None)
    chk(isinstance(days, int) and days > 0,
        "Q4a", f"run_earnings_watch._HIST_DAYS 존재 ({days})")
    if isinstance(days, int) and want:
        supply = fx.bars_for_calendar_days(days)
        need_hard = fx.bars_for_calendar_days(want * 91.31) + 1
        need_soft = need_hard + ec.VOLUME_BASELINE_BARS
        chk(supply >= need_hard, "Q4b",
            f"공급 {supply}봉 ≥ 하드 요구 {need_hard}봉"
            f"({want}분기 도달 + 직전 종가)")
        chk(supply >= need_soft, "Q4c",
            f"공급 {supply}봉 ≥ 소프트 요구 {need_soft}봉"
            f"(+거래량 기준 {ec.VOLUME_BASELINE_BARS}봉)")
        chk(days <= fx.HIST_MAX_DAYS, "Q4d",
            f"창 {days}일 ≤ 상한 {fx.HIST_MAX_DAYS}일")

    # 창 제약 판별자가 살아 있는가. 개수만 보면 '창이 짧다'와 '종목 이력이
    # 짧다'가 구분되지 않는다 — 이 함수가 유일한 판별자다.
    wb = getattr(rew, "_hist_window_bound", None)
    chk(callable(wb), "Q5a", "run_earnings_watch._hist_window_bound 존재")
    if callable(wb):
        now = datetime.now(rew._ET).date()
        edge = pd.Timestamp(now - timedelta(days=rew._HIST_DAYS))
        near = pd.DataFrame({"Close": [1.0, 2.0]},
                            index=[edge, edge + timedelta(days=1)])
        far = pd.DataFrame({"Close": [1.0, 2.0]},
                           index=[edge + timedelta(days=400),
                                  edge + timedelta(days=401)])
        chk(wb(near) is True, "Q5b", "창 하단에 맞닿은 이력 → True")
        chk(wb(far) is False, "Q5c", "훨씬 뒤에서 시작하는 이력 → False (종목 이력이 짧은 것)")
        chk(wb(pd.DataFrame()) is False, "Q5d", "빈 이력 → False (단정하지 않는다)")


def c_stop() -> bool:
    """런타임 군을 돌려도 되는가 — '네트워크 0' 약속을 지키는 장치.

    원시 requests 가 남아 있거나 URL 에 limit 이 남아 있으면 스텁이 경로를
    가로채지 못하고 **진짜 FMP 로 나간다.** 그러면 통과/실패와 무관하게
    이 스위트의 전제가 깨진다.
    """
    blockers = [f for f in FAIL if f.startswith(("C1b", "C3"))]
    if blockers:
        print("\n[C-STOP] 정적 배선이 깨졌다 — 스텁이 경로를 못 가로챈다.")
        print("         런타임 군을 건너뛴다(진짜 FMP 호출 방지).")
        for b in blockers:
            print(f"           · {b}")
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# [R] 요구 봉수 역산 — 소비 구문에서 뽑아 호출부 선언치와 대조
# ══════════════════════════════════════════════════════════════════════════════
def group_R():
    print("\n[R] 요구 봉수 역산 (AST) — 선언치와 대조")

    # ── R0 포지티브 컨트롤 — 포크한 누산기 자체를 검증한다 ──
    #    이 스위트는 `required_bars_for` 의 답을 근거로 판정한다. 그 누산기가
    #    조용히 0 을 내면 **모든 R1 이 공허하게 통과**한다. 그래서 답을 아는
    #    합성 입력을 먼저 통과시킨다.
    #
    #    R0a: iloc 이 없는 코드에서는 dhw 원본과 답이 같아야 한다(포크가
    #         rolling/tail 환산을 건드리지 않았다는 증거).
    #    R0b: iloc 이 있는 코드에서는 포크가 **더 크게** 나와야 한다.
    #         같다면 iloc 층이 죽은 것이고, 그러면 R1 이 요구를 과소평가한다.
    #    R0c: 함수 경계를 넘는 전달(요구가 인자로 들어오는 경우)을 따라가는가.
    _CTRL_NO_ILOC = """
def f(src):
    a = src.rolling(30).mean()
    return a
"""
    _CTRL_ILOC = """
def f(src):
    a = src.dropna()
    return a.iloc[-77]
"""
    _CTRL_CROSS = """
def g(series, back):
    return series.iloc[-1 - back]

def f(src):
    return g(src, 41)
"""
    t0 = ast.parse(_CTRL_NO_ILOC)
    f0 = find_func(t0, "f")
    a0, b0 = dhw.required_bars_in(f0), required_bars_for(f0, {"src"}, t0)
    chk(a0 == b0 == 30, "R0a",
        f"포지티브 컨트롤(rolling 30): dhw={a0}, 포크={b0} — 둘 다 30이어야 한다")

    t1 = ast.parse(_CTRL_ILOC)
    f1 = find_func(t1, "f")
    a1, b1 = dhw.required_bars_in(f1), required_bars_for(f1, {"src"}, t1)
    chk(b1 == 77 and a1 < b1, "R0b",
        f"포지티브 컨트롤(iloc[-77]): 포크={b1} (dhw={a1}) — iloc 층이 살아 있다")

    t2 = ast.parse(_CTRL_CROSS)
    f2 = find_func(t2, "f")
    b2 = required_bars_for(f2, {"src"}, t2)
    chk(b2 == 42, "R0c",
        f"포지티브 컨트롤(함수 경계 넘김 g(src,41)): 포크={b2} — 42여야 한다")

    for name, fname, defname, seeds in _SITES:
        t = tree(_MODULES[name])
        f = find_func(t, fname)
        if f is None:
            bad("R1", f"{name}.{fname}: 함수를 못 찾았다")
            continue
        derived = required_bars_for(f, set(seeds), t)
        declared = declared_bars(f, defname, seeds)
        chk(declared is not None, "R1a",
            f"{name}.{fname} {sorted(seeds)}: 호출부에서 bars= 판독됨 ({declared})")
        chk(derived == declared, "R1b",
            f"{name}.{fname} {sorted(seeds)}: 역산 {derived}봉 == 선언 {declared}봉")

    # R2 — 선언치가 소비 구문과 무관하게 굳어버리는 것을 막는다.
    #      run_narrative 종목 창은 rolling(200) 이 유일한 근거다. 그 리터럴이
    #      바뀌면 선언치도 따라 바뀌어야 한다.
    t = tree(nrt)
    f = find_func(t, "verify_emerging_with_quant")
    rollings = [_const_int(c.args[0], {}) for c in calls_to(t, "rolling")
                if c.args] if f else []
    chk(200 in rollings, "R2a",
        f"run_narrative: rolling 창 리터럴에 200 존재 — {rollings}")
    gates = [n.comparators[0].value for n in ast.walk(f)
             if isinstance(n, ast.Compare) and isinstance(n.comparators[0], ast.Constant)
             and isinstance(n.comparators[0].value, int)] if f else []
    chk(150 in gates, "R2b",
        "run_narrative: len(s) >= 150 하드게이트 존재 (이게 있어서 130봉이면 조용히 None)")


# ══════════════════════════════════════════════════════════════════════════════
# [E] 런타임 — 스텁 FMP 위에서 실제로 실행하고 결과를 관측한다
# ══════════════════════════════════════════════════════════════════════════════
def _win_of(path: str):
    """호출 경로에서 (from, to) 를 뽑아 (창 달력일, to 날짜) 반환.

    ⚠️ dhw._drg_stub 의 supply["days"] 를 쓰지 않는다. 그쪽은 `today - from` 이라
       **오늘 기준**인데, run_drg_verify 의 창은 pred_date 앵커라 값이 어긋난다.
       경로 문자열에서 직접 뽑으면 앵커와 무관하게 정확하다.
       (창 = to - 1 - from. hist_range_params 가 to 를 앵커+1일로 주기 때문이다.)
    """
    _, _, qs = str(path).partition("?")
    q = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv)
    if "from" not in q or "to" not in q:
        return None, None
    d0 = datetime.strptime(q["from"], "%Y-%m-%d").date()
    d1 = datetime.strptime(q["to"], "%Y-%m-%d").date()
    return (d1 - timedelta(days=1) - d0).days, d1


class _StubJson:
    """fh.fmp_get_json 을 dhw._drg_stub 으로 교체. 네트워크 접근 없음."""

    def __init__(self, blackout=None):
        self.log, self.supply = [], []
        self._blackout = blackout
        self._saved = None

    def __enter__(self):
        self._saved = fh.fmp_get_json
        fh.fmp_get_json = dhw._drg_stub(order="oldest", blackout=self._blackout,
                                        log=self.log, supply=self.supply)
        return self

    def __exit__(self, *a):
        fh.fmp_get_json = self._saved
        return False


class _StubResp:
    """fx.fmp_get(절대 URL) → 응답 객체 경로. run_hidden_alpha 가 이 모양이다.

    fmp_extras 는 임포트 시점에 `fmp_get = _fh.fmp_get` 로 **값을 바인딩**한다.
    그래서 fh.fmp_get 만 갈아끼우면 run_hidden_alpha 는 여전히 원본을 부른다 —
    fx.fmp_get 쪽을 갈아야 한다.
    """

    def __init__(self, blackout=None):
        self.log, self.supply = [], []
        self._inner = dhw._drg_stub(order="oldest", blackout=blackout,
                                    log=self.log, supply=self.supply)
        self._saved = None

    def __enter__(self):
        self._saved = fx.fmp_get

        def fake(url, timeout=None, **kw):
            path = str(url).split("/stable/", 1)[-1]
            return dhw.FakeResp(self._inner(path))

        fx.fmp_get = fake
        return self

    def __exit__(self, *a):
        fx.fmp_get = self._saved
        return False


def _expect_window(bars: int) -> int:
    """정책 변환기가 산출하는 창. 진단이 정책을 복제하지 않는다."""
    return fx.hist_days_for_bars(bars)


def _bars_at(modname, consumer, defname, seeds):
    """호출부 소스의 `bars=` 값. 런타임 군도 이 값으로 구동한다 —
    진단이 든 숫자로 부르면 소스가 바뀌어도 런타임 검사가 옛 값을 계속 본다."""
    t = tree(_MODULES[modname])
    f = find_func(t, consumer)
    return declared_bars(f, defname, seeds) if f else None


def group_E(blackout=None, tag_suffix=""):
    label = "정상" if blackout is None else "7일 비상휴장 주입"
    print(f"\n[E{tag_suffix}] 런타임 — 스텁 FMP ({label})")

    B_SPY_N = _bars_at("run_narrative", "verify_emerging_with_quant",
                       "_fmp_close_series", {"spy_close"})
    B_TKR_N = _bars_at("run_narrative", "verify_emerging_with_quant",
                       "_fmp_close_series", {"s", "vol"})
    B_SPY_F = _bars_at("fmp_extras", "compute_satellite_top10", "_closes", {"spy"})
    B_CND_F = _bars_at("fmp_extras", "compute_satellite_top10", "_closes", {"s"})
    B_HID = _bars_at("run_hidden_alpha", "build_ranked_table",
                     "_fmp_batch_close_df", {"close_df"})
    if None in (B_SPY_N, B_TKR_N, B_SPY_F, B_CND_F, B_HID):
        bad(f"E0{tag_suffix}", "호출부 bars= 판독 실패 — 런타임 군을 구동할 수 없다")
        return

    # ── E1 run_narrative: 관측 창 == 정책 산출값, 공급 봉수 ≥ 요구 ──
    with _StubJson(blackout) as st:
        spy, _ = nrt._fmp_close_series("SPY", bars=B_SPY_N)
        s, vol = nrt._fmp_close_series("AAA", bars=B_TKR_N)
    wins = [_win_of(p)[0] for p in st.log if "historical-price-eod" in p]
    chk(_expect_window(B_SPY_N) in wins, f"E1a{tag_suffix}",
        f"run_narrative SPY: 관측 창 {wins} 에 정책값 {_expect_window(B_SPY_N)}일 포함")
    chk(_expect_window(B_TKR_N) in wins, f"E1b{tag_suffix}",
        f"run_narrative 종목: 관측 창 {wins} 에 정책값 {_expect_window(B_TKR_N)}일 포함")
    chk(len(spy) >= B_SPY_N, f"E1c{tag_suffix}",
        f"run_narrative SPY: 공급 {len(spy)}봉 ≥ 요구 {B_SPY_N}봉")
    chk(len(s) >= B_TKR_N, f"E1d{tag_suffix}",
        f"run_narrative 종목: 공급 {len(s)}봉 ≥ 요구 {B_TKR_N}봉")

    # ── E2 ★결정적★ MA200 이 실제로 산출되는가 ──
    #    창 길이를 문자열로 재는 것으로는 이 결함을 못 잡는다. 130봉 창이면
    #    len(s) >= 150 가드가 실패해 above_ma200 이 None 이 되고, verdict 분기
    #    "❌ 하락 추세 (대기)" 가 도달 불가가 된다. 결과를 관측해야 한다.
    with _StubJson(blackout):
        res = nrt.verify_emerging_with_quant(["AAA"])
    chk(len(res) == 1, f"E2a{tag_suffix}",
        f"run_narrative: 종목이 데이터 부족으로 탈락하지 않았다 ({len(res)}건)")
    if res:
        av = res[0].get("above_ma200")
        chk(av is not None, f"E2b{tag_suffix}",
            f"run_narrative: above_ma200 산출됨 ({av}) — None 이면 창이 200봉 미만")
        chk(res[0].get("verdict"), f"E2c{tag_suffix}",
            f"run_narrative: verdict 산출됨 ({res[0].get('verdict')})")

    # ── E3 run_drg_verify: 창 앵커가 pred_date ──
    #    미검증 예측이 밀려 있는 상황을 모사한다(10일 전 예측을 오늘 검증).
    pred_date = (datetime.now().date() - timedelta(days=10))
    while pred_date.weekday() >= 5:                 # 주말이면 봉이 없다
        pred_date -= timedelta(days=1)
    row = pd.Series({"benchmark_etf": "SPY",
                     "pred_date": pred_date.strftime("%Y-%m-%d"),
                     "direction": "상승", "is_revised": "", "revised_direction": ""})
    with _StubJson(blackout) as st:
        actual, ret, correct = drv.verify_prediction(row)
    hp = [p for p in st.log if "historical-price-eod" in p]
    chk(len(hp) == 1, f"E3a{tag_suffix}", f"run_drg_verify: 일봉 호출 1건 ({len(hp)})")
    if hp:
        w, to = _win_of(hp[0])
        chk(to == pred_date + timedelta(days=1), f"E3b{tag_suffix}",
            f"run_drg_verify: to={to} == pred_date+1 ({pred_date + timedelta(days=1)}) — 오늘 앵커가 아니다")
        chk(w == _expect_window(2), f"E3c{tag_suffix}",
            f"run_drg_verify: 창 {w}일 == 정책값 {_expect_window(2)}일")
    chk(actual in ("상승", "하락", "중립"), f"E3d{tag_suffix}",
        f"run_drg_verify: 방향 판정 산출됨 ('{actual}') — 빈 문자열이면 검증이 사라진 것")
    chk(not (isinstance(ret, float) and np.isnan(ret)), f"E3e{tag_suffix}",
        f"run_drg_verify: 수익률 산출됨 ({ret})")

    # ── E4 fmp_extras._closes ──
    with _StubJson(blackout) as st:
        spy2 = fx._closes("SPY", bars=B_SPY_F)
        s2 = fx._closes("BBB", bars=B_CND_F)
    wins = [_win_of(p)[0] for p in st.log if "historical-price-eod" in p]
    chk(_expect_window(B_SPY_F) in wins, f"E4a{tag_suffix}",
        f"fmp_extras 시장필터: 관측 창 {wins} 에 {_expect_window(B_SPY_F)}일 포함")
    chk(_expect_window(B_CND_F) in wins, f"E4b{tag_suffix}",
        f"fmp_extras 챔피언: 관측 창 {wins} 에 {_expect_window(B_CND_F)}일 포함")
    chk(len(spy2) >= B_SPY_F, f"E4c{tag_suffix}", f"fmp_extras SPY: {len(spy2)}봉 ≥ {B_SPY_F}")
    chk(len(s2) >= B_CND_F, f"E4d{tag_suffix}", f"fmp_extras 후보: {len(s2)}봉 ≥ {B_CND_F}")

    # ── E5 run_hidden_alpha (응답 객체 경로) ──
    with _StubResp(blackout) as st:
        ser = hid._fmp_price_history_close("CCC", bars=B_HID)
    wins = [_win_of(p)[0] for p in st.log if "historical-price-eod" in p]
    chk(_expect_window(B_HID) in wins, f"E5a{tag_suffix}",
        f"run_hidden_alpha: 관측 창 {wins} 에 {_expect_window(B_HID)}일 포함")
    chk(len(ser) >= B_HID, f"E5b{tag_suffix}",
        f"run_hidden_alpha: 공급 {len(ser)}봉 ≥ 요구 {B_HID}봉")
    ret = hid.calculate_period_return(ser, 21)
    chk(ret is not None and not pd.isna(ret), f"E5c{tag_suffix}",
        f"run_hidden_alpha: 1개월 수익률 산출됨 ({ret}) — NaN 이면 창이 22봉 미만")


# ══════════════════════════════════════════════════════════════════════════════
# [B] 비상 휴장 — 7달력일 연속 휴장을 주입해도 요구를 덮는가
# ══════════════════════════════════════════════════════════════════════════════
def group_B():
    """2001-09-11 은 7달력일 연속 휴장이었다. 창의 마진이 그걸 견디는지 본다.

    ⚠️ 알려진 한계: `hist_days_for_bars` 의 기본 마진 pad_bars=5 는 7달력일
       휴장으로 잃는 봉수(≈5봉)를 **정확히 상쇄만** 한다. 요구가 15봉을 넘으면
       HIST_MIN_DAYS=21 바닥도 안 걸리므로 여유가 0 이다. 요구 충족은 되지만
       슬랙이 없다는 뜻이고, 이 검사가 그 사실을 매번 다시 확인한다.
    """
    end = datetime.now().date() - timedelta(days=3)
    group_E(blackout=(end - timedelta(days=6), end), tag_suffix="-BLACKOUT")


def main():
    print("=" * 78)
    print("diag_hist_window_consumers — historical-price-eod 소비처 창 회귀")
    for name, mod in _MODULES.items():
        print(f"  {name:18s}: {mod.__file__}")
    print(f"  earnings_core     : {ec.__file__}")
    print(f"  하네스             : {dhw.__file__} (스텁·AST 헬퍼 재사용)")
    print("=" * 78)

    group_C()
    group_R()
    group_Q()
    if c_stop():
        group_E()
        group_B()

    print("\n" + "=" * 78)
    print(f"결과: {len(PASS)}/{len(PASS) + len(FAIL)} 통과")
    if FAIL:
        print("\n실패 항목:")
        for f in FAIL:
            print(f"  ❌ {f}")
        print("=" * 78)
        return 1
    print("✅ 전 항목 통과 — 소비처 5곳의 창이 하류 요구치를 덮는다.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
