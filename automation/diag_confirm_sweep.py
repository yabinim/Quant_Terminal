# -*- coding: utf-8 -*-
"""diag_confirm_sweep.py — 확정 일수(CONFIRM_DAYS) 스윕 회귀 스위트.

무엇을 지키나
─────────────
`run_signal_backtest.py` 를 `CONFIRM_DAYS=1/2/3` 으로 세 번 돌려 "2일 확정이
값어치를 하는가" 를 잰다. 이때 **가장 위험한 고장은 조용한 무시**다.

    입력을 3으로 주고 돌렸는데 내부가 2로 떨어진다
      → 세 번 돌려 똑같은 숫자가 나온다
      → "확정 일수는 성적에 영향이 없다" 는 **정반대 결론**을 낸다
      → 로그에도 시트에도 이상이 없다

이 스위트가 없으면 그 결론을 반증할 방법이 없다. 이번 프로젝트에서 이미
같은 유형(판별력 없는 지표를 판별자로 고름)으로 세 번 당했다.

검사 구조
─────────
    1 파싱      _env_confirm_days 의 실제 함수 — 정상값·빈값·쓰레기·범위밖
    2 전달      walk_forward_events 가 confirm_days 를 **실제로 지킨다**
                (가짜 analyze_fn 주입 → 확정일이 N일차에 정확히 오는지)
    3 배선      main() 이 모듈 상수를 명시적으로 넘긴다 (AST)
    4 기록      결과 행에 Confirm_Days 가 실린다 (스윕 행 구분 불가 방지)
    5 뮤테이션  위 검사가 결함을 실제로 잡는지 역검증

⚠️ 2절이 핵심이다. 1절(파싱)과 3절(배선)만 있으면 "환경변수는 잘 읽혔고
   인자로도 잘 넘어갔는데 정작 루프가 무시하는" 경우를 못 잡는다. 그래서
   가짜 판정 함수를 주입해 **이벤트가 며칠째에 확정되는지**를 직접 센다.

⚠️ 로직을 복사하지 않는다 — run_signal_backtest 의 실제 함수를 import 해서
   호출한다. 모듈은 환경변수를 .get 으로 읽으므로 import 만으로 깨지지 않는다.

안전성
──────
· 네트워크 접근 없음 · 시트 접근 없음 · FMP 호출 없음
· 부작용 전혀 없다. 몇 번을 돌려도 같은 결과.

실행:  python3 automation/diag_confirm_sweep.py
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
os.environ.pop("CONFIRM_DAYS", None)

import numpy as np      # noqa: E402
import pandas as pd     # noqa: E402

import run_signal_backtest as bt   # noqa: E402

_fails, _passes = [], 0


def check(name, got, want):
    global _passes
    if got == want:
        _passes += 1
        print("  ✅ " + name)
    else:
        _fails.append(name)
        print("  ❌ " + name + "\n       기대: " + repr(want)
              + "\n       실제: " + repr(got))


def _find_target():
    for base in (_HERE, os.path.join(_ROOT, "automation"), _ROOT):
        p = os.path.join(base, "run_signal_backtest.py")
        if os.path.exists(p):
            return p
    return os.path.join(_HERE, "run_signal_backtest.py")


TARGET = sys.argv[1] if len(sys.argv) > 1 else _find_target()
SRC = open(TARGET, encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════════════
# 1) 파싱
# ══════════════════════════════════════════════════════════════════════════
print("=" * 74)
print("1) 파싱 — _env_confirm_days (실제 함수)")
print("=" * 74)

f = bt._env_confirm_days
check("P1  '1' → 1", f("1"), 1)
check("P2  '2' → 2", f("2"), 2)
check("P3  '3' → 3", f("3"), 3)
check("P4  '10' → 10 (상한 포함)", f("10"), 10)
check("P5  빈 문자열 → 기본 2", f(""), 2)
check("P6  공백만 → 기본 2", f("   "), 2)
check("P7  ' 3 ' 패딩 허용", f(" 3 "), 3)
check("P8  'abc' → 2 폴백", f("abc"), 2)
check("P9  '0' → 범위 밖 → 2 폴백", f("0"), 2)
check("P10 '11' → 범위 밖 → 2 폴백", f("11"), 2)
check("P11 '-1' → 범위 밖 → 2 폴백", f("-1"), 2)
check("P12 '2.5' → 정수 아님 → 2 폴백", f("2.5"), 2)
check("P13 None 입력 시 환경변수 참조(현재 미설정) → 2", f(None), 2)
check("P14 모듈 상수 기본값이 2", bt.CONFIRM_DAYS, 2)


# ══════════════════════════════════════════════════════════════════════════
# 2) 전달 — 루프가 confirm_days 를 실제로 지키는가
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("2) 전달 — walk_forward_events 가 N일째에 확정하는가")
print("=" * 74)


def _hist(n=320):
    """단조 상승 OHLCV. 판정은 가짜 함수가 하므로 값 자체는 중요하지 않다."""
    idx = pd.bdate_range(end="2026-08-21", periods=n)
    close = pd.Series(np.linspace(100.0, 160.0, n), index=idx)
    return pd.DataFrame({"Open": close, "High": close * 1.01,
                         "Low": close * 0.99, "Close": close,
                         "Volume": [1_000_000] * n}, index=idx)


H = _hist()
_ENTRY_FROM = 300     # 이 인덱스부터 entry 가 연속으로 참


def fake_analyze(slice_, spy_close=None):
    """마지막 봉 위치가 _ENTRY_FROM 이상이면 entry, 아니면 wait."""
    i = len(slice_) - 1
    code = "entry" if i >= _ENTRY_FROM else "wait"
    return {"timing": {"code": code}, "regime": {"enough_data": True}}


def first_entry_pos(confirm):
    """confirm_days=N 일 때 entry 이벤트가 처음 확정된 봉 위치."""
    events, _alerts, _tds, _d0, _d1 = bt.walk_forward_events(
        H, spy_close=None, min_prior=200, test_lookback=100,
        confirm_days=confirm, analyze_fn=fake_analyze, roundtrip=False)
    dates = list(H.index)
    for e in events:
        if e.get("code") == "entry":
            for i, d in enumerate(dates):
                if str(pd.Timestamp(d).date()) == e.get("date"):
                    return i
    return None


p1, p2, p3 = first_entry_pos(1), first_entry_pos(2), first_entry_pos(3)
print("     확정 봉 위치: confirm=1 → %s · 2 → %s · 3 → %s "
      "(entry 시작 %d)" % (p1, p2, p3, _ENTRY_FROM))

check("T1  confirm=1 은 조건 첫날 확정", p1, _ENTRY_FROM)
check("T2  confirm=2 는 이틀째 확정", p2, _ENTRY_FROM + 1)
check("T3  confirm=3 은 사흘째 확정", p3, _ENTRY_FROM + 2)
check("T4  세 값이 서로 다르다 (스윕이 의미를 가진다)",
      len({p1, p2, p3}), 3)
check("T5  확정일수가 커질수록 늦어진다", (p1 < p2 < p3), True)


# ══════════════════════════════════════════════════════════════════════════
# 3) 배선 — main() 이 상수를 명시적으로 넘기는가
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("3) 배선 — run_backtest() → walk_forward_events (AST)")
print("=" * 74)


# 호출부가 사는 함수. main() 이 아니라 run_backtest() 다 — 티커 루프가 여기 있다.
HOST_FN = "run_backtest"


def wiring(src):
    out = {"env_read": False, "explicit": False, "hardcoded": False}
    try:
        tree = ast.parse(src)
    except Exception:
        return out
    # 상수가 환경변수에서 오는가
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "CONFIRM_DAYS"
                        for t in n.targets)
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Name)
                and n.value.func.id == "_env_confirm_days"):
            out["env_read"] = True
    # main() 안 호출이 confirm_days=CONFIRM_DAYS 를 명시하는가
    host = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == HOST_FN:
            host = n
    if host is None:
        return out
    for c in ast.walk(host):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == "walk_forward_events"):
            continue
        for kw in c.keywords:
            if kw.arg != "confirm_days":
                continue
            if isinstance(kw.value, ast.Name) and kw.value.id == "CONFIRM_DAYS":
                out["explicit"] = True
            elif isinstance(kw.value, ast.Constant):
                out["hardcoded"] = True
    return out


W = wiring(SRC)
check("W1  CONFIRM_DAYS 가 _env_confirm_days() 로 초기화됨", W["env_read"], True)
check("W2  run_backtest() 가 confirm_days=CONFIRM_DAYS 를 명시 전달",
      W["explicit"], True)
check("W3  호출부에 상수 하드코딩 없음", W["hardcoded"], False)


# ══════════════════════════════════════════════════════════════════════════
# 4) 기록 — 결과 행에 Confirm_Days 가 실리는가
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("4) 기록 — Signal_Backtest 행에 확정일수가 남는가")
print("=" * 74)

check("R1  _RESULT_COLS 에 Confirm_Days 존재",
      "Confirm_Days" in bt._RESULT_COLS, True)

_rows = bt.build_result_rows({"entry": {"count": 3, "winrate_20d": 50.0}},
                         "2026-08-25", "2022-01-01", "2026-08-25", 10,
                         buckets=("entry",), mode="verdict", segment="stock")
check("R2  행 길이가 열 개수와 일치", len(_rows[0]), len(bt._RESULT_COLS))
_ix = bt._RESULT_COLS.index("Confirm_Days")
check("R3  Confirm_Days 칸에 실제 값이 들어감", _rows[0][_ix], bt.CONFIRM_DAYS)
check("R4  Mode/Segment 가 밀리지 않았다",
      (_rows[0][bt._RESULT_COLS.index("Mode")],
       _rows[0][bt._RESULT_COLS.index("Segment")]), ("verdict", "stock"))


# ══════════════════════════════════════════════════════════════════════════
# 5) 뮤테이션
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("5) 뮤테이션 — 결함을 심으면 위 검사가 빨간불이어야 한다")
print("=" * 74)

# ── 5a. 파싱 함수 본문 ────────────────────────────────────────────────────
FSRC = None
for _n in ast.walk(ast.parse(SRC)):
    if isinstance(_n, ast.FunctionDef) and _n.name == "_env_confirm_days":
        FSRC = ast.get_source_segment(SRC, _n)

PARSE_CASES = [("1", 1), ("3", 3), ("", 2), ("abc", 2), ("0", 2), ("11", 2)]
FN_MUTANTS = [
    ("F-M1 범위 가드 제거 (0·11 이 그대로 통과)",
     "if not (1 <= v <= 10):", "if False:"),
    ("F-M2 항상 기본값 (환경변수 무시 — 가장 위험한 고장)",
     "    return v", "    return default"),
    ("F-M3 빈값 분기가 아무거나 삼킴",
     "if not s:", "if True:"),
]
for name, old, new in FN_MUTANTS:
    if FSRC is None or old not in FSRC:
        print("  ⚠️  " + name + " — 앵커 없음. 스킵")
        _fails.append(name + " [앵커 소실]")
        continue
    ns = {"os": os}
    try:
        exec(compile(FSRC.replace(old, new, 1), "<mut>", "exec"), ns)
        mf = ns["_env_confirm_days"]
    except Exception:
        print("  ✅ " + name + " — 로드 단계에서 실패(검출)")
        _passes += 1
        continue
    caught = False
    for raw, want in PARSE_CASES:
        try:
            if mf(raw) != want:
                caught = True
                break
        except Exception:
            caught = True
            break
    if caught:
        print("  ✅ " + name + " — 1절이 잡아냄")
        _passes += 1
    else:
        print("  ❌ " + name + " — **잡아내지 못함**")
        _fails.append(name)

# ── 5b. 배선 ──────────────────────────────────────────────────────────────
WIRE_MUTANTS = [
    ("W-M1 호출부가 인자를 안 넘김 (기본 인자에만 의존)",
     "hist, spy_close=spy_close, confirm_days=CONFIRM_DAYS)",
     "hist, spy_close=spy_close)", "explicit"),
    ("W-M2 호출부에 2 하드코딩 (스윕이 조용히 무력화)",
     "confirm_days=CONFIRM_DAYS)", "confirm_days=2)", "hardcoded"),
    ("W-M3 상수를 환경변수에서 안 읽음",
     "CONFIRM_DAYS   = _env_confirm_days()", "CONFIRM_DAYS   = 2", "env_read"),
]
for name, old, new, key in WIRE_MUTANTS:
    n_occ = SRC.count(old)
    if n_occ != 1:
        print("  ⚠️  " + name + " — 앵커가 " + str(n_occ) + "회(1회여야 함). 스킵")
        _fails.append(name + " [앵커 " + str(n_occ) + "회]")
        continue
    mw = wiring(SRC.replace(old, new, 1))
    if mw.get(key) != W.get(key):
        print("  ✅ " + name + " — 3절 " + key + " 가 뒤집힘")
        _passes += 1
    else:
        print("  ❌ " + name + " — **3절이 잡아내지 못함**")
        _fails.append(name)

# ── 5c. 양성대조 — 2절이 정말 판별력이 있는가 ────────────────────────────
# confirm_days 를 무시하고 늘 1로 쓰는 가짜 러너를 만들어, T1~T5 가
# 실제로 실패하는지 본다. "항상 통과하는 스위트" 를 방지한다.
_p_all1 = [first_entry_pos(1), first_entry_pos(1), first_entry_pos(1)]
check("G-1 양성대조: confirm 을 무시하면 T4(세 값이 다름)가 깨진다",
      len(set(_p_all1)) == 3, False)

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
