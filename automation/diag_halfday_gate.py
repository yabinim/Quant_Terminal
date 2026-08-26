# -*- coding: utf-8 -*-
"""diag_halfday_gate.py — 반일장 2PM 장중 가드 회귀 스위트.

무엇을 지키나
─────────────
반일장(13:00 조기 마감)에 2PM 잡(`--mode intraday`)이 돌면, /quote 가 돌려주는
13:00 **종가**가 "잠정 봉"으로 주입돼 "장중 헤드업" 메일이 나간다. 숫자는 맞고
라벨이 틀린다 — 장은 이미 한 시간 전에 끝났다. 3시간 뒤 5PM EOD 가 같은 숫자로
확정 메일을 또 보내므로 중복이기도 하다.

`run_watchlist_alerts.py` 의 `_intraday_close_passed()` + main() 배선이 그걸
막는다. 이 스위트는 **그 두 가지를 함께** 검사한다.

⚠️ 왜 순수 함수 테스트만으로는 부족한가 — 이 스위트의 존재 이유
──────────────────────────────────────────────────────────────
가드를 무력화하는 가장 위험한 두 변이는 **함수 안이 아니라 호출부에** 있다.

    ① datetime.now(_ET) → datetime.now(_KST)
       14:00 ET = 03:00 KST(익일) → 180 < 780 → 가드가 통째로 통과한다.
       함수는 정상 동작하고 있으므로 함수 테스트는 전부 초록불이다.

    ② `args.mode == "intraday"` 조건 삭제
       17:00 EOD 실행까지 스킵된다 → **확정 알림 유실.** 장중 메일이 한 통
       잘못 나가는 것보다 훨씬 나쁜데, 함수 테스트로는 절대 안 잡힌다.

그래서 2절(동작)과 1절(AST 배선)이 **둘 다** 있어야 한다. 어느 한쪽만 있으면
판별력이 없다. 5절이 그 사실 자체를 기계로 확인한다(배선을 망가뜨리고 1절이
실제로 빨간불이 되는지).

⚠️ 로직을 복사하지 않는다
─────────────────────────
`run_watchlist_alerts.py` 를 AST 로 파싱해 `_intraday_close_passed` 의 **실제
소스**를 꺼내 exec 한다. 복사본을 테스트하면 원본을 안 고쳐도 통과하는 가짜
초록불이 된다(이 프로젝트에서 이미 겪은 실패 유형).

import 가 아니라 AST 추출을 쓰는 이유: `run_watchlist_alerts` 는 모듈 레벨에서
필수 환경변수 5개를 읽고 gspread/pandas/numpy/requests 를 끌어온다. 이 가드는
그중 아무것도 필요 없으므로, 워크플로 의존성을 pytz 하나로 유지한다.

안전성
──────
· 네트워크 접근 없음 · 시트 접근 없음 · FMP 호출 없음 · 이메일 없음
· 상태머신 미접촉. 몇 번을 돌려도 같은 결과.

실행:  python3 automation/diag_halfday_gate.py [run_watchlist_alerts.py 경로]
"""
import ast
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import calendar_core as cc  # noqa: E402

GUARD_FN = "_intraday_close_passed"


def _find_target():
    for base in (_HERE, os.path.join(_ROOT, "automation"), _ROOT):
        p = os.path.join(base, "run_watchlist_alerts.py")
        if os.path.exists(p):
            return p
    return os.path.join(_HERE, "run_watchlist_alerts.py")


TARGET = sys.argv[1] if len(sys.argv) > 1 else _find_target()

_fails = []
_passes = 0


def check(name, got, want):
    global _passes
    if got == want:
        _passes += 1
        print("  ✅ " + name)
    else:
        _fails.append(name)
        print("  ❌ " + name + "\n       기대: " + repr(want)
              + "\n       실제: " + repr(got))


def et(h, mi, y=2026, mo=11, d=27):
    """ET 시각. 함수는 .hour/.minute 만 읽으므로 tz 는 불필요하다."""
    return datetime(y, mo, d, h, mi)


# ══════════════════════════════════════════════════════════════════════════
# 0) AST 추출
# ══════════════════════════════════════════════════════════════════════════
SRC = open(TARGET, encoding="utf-8").read()


def extract_func(src, name):
    try:
        tree = ast.parse(src)
    except Exception:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    return None


def load(func_src):
    """실제 calendar_core 를 cc 로 묶어 exec — 스텁을 쓰지 않는다."""
    ns = {"cc": cc}
    exec(compile(func_src, "<extracted>", "exec"), ns)
    return ns[GUARD_FN]


# ══════════════════════════════════════════════════════════════════════════
# 배선 정적 분석 — 소스 문자열을 받아 불변식 dict 를 돌려준다.
# 5절에서 같은 함수를 변이 소스에 다시 돌려 판별력을 확인한다.
# ══════════════════════════════════════════════════════════════════════════
def wiring(src):
    out = {"fn_exists": False, "calls": 0, "first_arg_et": False,
           "mode_guarded": False, "returns": False,
           "close_from_core": False, "after_open_gate": False}
    try:
        tree = ast.parse(src)
    except Exception:
        return out

    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == GUARD_FN:
            out["fn_exists"] = True

    main_node = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "main":
            main_node = n
    if main_node is None:
        return out

    calls = [c for c in ast.walk(main_node)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
             and c.func.id == GUARD_FN]
    out["calls"] = len(calls)
    if not calls:
        return out
    call = calls[0]

    # ① 첫 인자가 datetime.now(_ET) 인가 — _KST 변이 검출 지점
    if call.args:
        a = call.args[0]
        out["first_arg_et"] = bool(
            isinstance(a, ast.Call)
            and isinstance(a.func, ast.Attribute) and a.func.attr == "now"
            and isinstance(a.func.value, ast.Name) and a.func.value.id == "datetime"
            and len(a.args) == 1
            and isinstance(a.args[0], ast.Name) and a.args[0].id == "_ET")

    # ② 호출을 감싼 if 에 args.mode == "intraday" 가 있는가 — EOD 유실 방지
    for n in ast.walk(main_node):
        if not isinstance(n, ast.If):
            continue
        if not any(x is call for x in ast.walk(n)):
            continue
        for cmp_ in ast.walk(n.test):
            if (isinstance(cmp_, ast.Compare)
                    and isinstance(cmp_.left, ast.Attribute)
                    and cmp_.left.attr == "mode"
                    and isinstance(cmp_.left.value, ast.Name)
                    and cmp_.left.value.id == "args"
                    and any(isinstance(o, ast.Eq) for o in cmp_.ops)
                    and any(isinstance(v, ast.Constant) and v.value == "intraday"
                            for v in cmp_.comparators)):
                out["mode_guarded"] = True
        # ③ 마감 시각을 calendar_core 에서 받는가 (하드코딩 방지)
        for c2 in ast.walk(n):
            if (isinstance(c2, ast.Call) and isinstance(c2.func, ast.Attribute)
                    and c2.func.attr == "session_close_time"):
                out["close_from_core"] = True

    # ④ 판정이 참일 때 실제로 return 하는가 — pass 변이 검출
    for n in ast.walk(main_node):
        if isinstance(n, ast.If) and any(x is call for x in ast.walk(n.test)):
            out["returns"] = any(isinstance(x, ast.Return) for x in ast.walk(n))
            break

    # ⑤ 휴장일 게이트보다 뒤에 있는가 (순서가 뒤집히면 휴장일에 헛돈다)
    gate_ln = None
    for c2 in ast.walk(main_node):
        if (isinstance(c2, ast.Call) and isinstance(c2.func, ast.Name)
                and c2.func.id == "is_market_open_today"):
            gate_ln = c2.lineno if gate_ln is None else min(gate_ln, c2.lineno)
    out["after_open_gate"] = bool(gate_ln is not None and gate_ln < call.lineno)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 1) 정적 검사 — 배선
# ══════════════════════════════════════════════════════════════════════════
print("=" * 74)
print("1) 정적 검사 — main() 배선 (순수 함수 테스트로는 못 잡는 자리)")
print("=" * 74)
print("  대상: " + TARGET)

W = wiring(SRC)
check("S1  _intraday_close_passed 가 모듈 레벨 함수로 존재", W["fn_exists"], True)
check("S2  main() 이 정확히 1회 호출", W["calls"], 1)
check("S3  첫 인자가 datetime.now(_ET)  ← _KST 변이 검출", W["first_arg_et"], True)
check("S4  args.mode == \"intraday\" 로 감싸짐  ← EOD 유실 방지", W["mode_guarded"], True)
check("S5  판정이 참이면 return 한다  ← pass 변이 검출", W["returns"], True)
check("S6  마감시각을 session_close_time 에서 받는다(하드코딩 아님)",
      W["close_from_core"], True)
check("S7  휴장일 게이트(is_market_open_today)보다 뒤에 있다",
      W["after_open_gate"], True)

FSRC = extract_func(SRC, GUARD_FN)
check("S8  함수 소스 추출 성공", FSRC is not None, True)
if FSRC is None:
    print("\n❌ 함수를 못 찾아 이후 검사를 진행할 수 없다.")
    sys.exit(1)

f = load(FSRC)

# ══════════════════════════════════════════════════════════════════════════
# 2) 동작 검사 — 추출된 실제 함수
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("2) 동작 검사 — True=스킵(장 마감) / False=정상 진행")
print("=" * 74)

check("H1  반일장 13:00 · 14:00 (2PM 잡의 실제 조건) → 스킵", f(et(14, 0), "13:00"), True)
check("H2  반일장 13:00 · 12:59 → 아직 장중",              f(et(12, 59), "13:00"), False)
check("H3  반일장 13:00 · 13:00 정각 → 스킵 (경계)",        f(et(13, 0), "13:00"), True)
check("H4  반일장 13:00 · 09:30 개장 직후 → 진행",          f(et(9, 30), "13:00"), False)
check("H5  정규장 16:00 · 14:00 → 이 가드 대상 아님",       f(et(14, 0), "16:00"), False)
check("H6  정규장 16:00 · 16:30 → 여전히 대상 아님 "
      "(정규장 단축반환 검증)",                              f(et(16, 30), "16:00"), False)
check("H7  휴장(None) → 진행 (앞단 게이트가 이미 처리)",     f(et(14, 0), None), False)
check("H8  빈 문자열 → 진행",                               f(et(14, 0), ""), False)
check("H9  공백 패딩 ' 13:00 ' 도 인식",                    f(et(14, 0), " 13:00 "), True)
check("H10 쓰레기 시각 'nope' · 14:00 → 진행 (fail-open, 960 폴백)",
      f(et(14, 0), "nope"), False)
check("H11 now_et=None → 진행 (fail-open)",                 f(None, "13:00"), False)
check("H12 KST 오배선 재현: 03:00 을 넘기면 통과해 버린다 "
      "(← S3 가 막는 자리)",                                 f(et(3, 0), "13:00"), False)

# ══════════════════════════════════════════════════════════════════════════
# 3) 실제 캘린더 연동 — 규칙 계산 결과를 그대로 물린다
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("3) calendar_core 연동 — 실제 날짜로 종단 확인")
print("=" * 74)

_t1127 = cc.session_close_time("2026-11-27")     # 추수감사절 다음 금요일
_t1125 = cc.session_close_time("2026-11-25")     # 평범한 수요일
_t1126 = cc.session_close_time("2026-11-26")     # 추수감사절 = 전휴장
_t1224 = cc.session_close_time("2026-12-24")     # 12/25 가 금요일 → 반일장

check("E1  2026-11-27 은 반일장", _t1127, "13:00")
check("E2  2026-11-27 14:00 → 스킵", f(et(14, 0), _t1127), True)
check("E3  2026-11-25(수)는 정규장", _t1125, "16:00")
check("E4  2026-11-25 14:00 → 진행 (과대적용 방지)", f(et(14, 0), _t1125), False)
check("E5  2026-11-26 추수감사절은 휴장(None)", _t1126, None)
check("E6  2026-12-24 도 반일장 · 14:00 → 스킵", f(et(14, 0), _t1224), True)

# ══════════════════════════════════════════════════════════════════════════
# 4) 뮤테이션 — 함수 본문에 결함을 심는다
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("4) 뮤테이션(함수 본문) — 결함을 심으면 2·3절이 반드시 빨간불이어야 한다")
print("=" * 74)

BEHAVIOR_CASES = [
    (et(14, 0), "13:00", True),
    (et(12, 59), "13:00", False),
    (et(13, 0), "13:00", True),
    (et(14, 0), "16:00", False),
    (et(16, 30), "16:00", False),
    (et(14, 0), None, False),
    (et(14, 0), "nope", False),
    (et(3, 0), "13:00", False),
]

FN_MUTANTS = [
    ("F-M1 경계 >= 를 > 로 (마감 정각이 장중으로 샌다)",
     "return _now_m >= _close_m", "return _now_m > _close_m"),
    ("F-M2 정규장 단축반환 제거 (16:30 에도 스킵이 걸린다)",
     "if not t or t == cc.REGULAR_CLOSE_TIME:", "if not t:"),
    ("F-M3 마감 분을 780 으로 하드코딩 (close_minutes 무력화)",
     "_close_m = cc.close_minutes(t)", "_close_m = 780"),
    ("F-M4 항상 스킵 (장중에도 헤드업이 죽는다)",
     "return _now_m >= _close_m", "return True"),
    ("F-M5 항상 진행 = 가드 무력화 (수정 전 상태)",
     "return _now_m >= _close_m", "return False"),
]

for name, old, new in FN_MUTANTS:
    if old not in FSRC:
        print("  ⚠️  " + name + " — 대상 문자열 없음(소스 변경?). 스킵")
        _fails.append(name + " [앵커 소실]")
        continue
    try:
        mf = load(FSRC.replace(old, new, 1))
    except Exception:
        print("  ✅ " + name + " — 변이가 로드 단계에서 실패(검출)")
        _passes += 1
        continue
    caught = False
    for now_, t_, want in BEHAVIOR_CASES:
        try:
            if mf(now_, t_) != want:
                caught = True
                break
        except Exception:
            caught = True
            break
    if caught:
        print("  ✅ " + name + " — 검사가 잡아냄")
        _passes += 1
    else:
        print("  ❌ " + name + " — **잡아내지 못함. 테스트가 부실하다**")
        _fails.append(name)

# ══════════════════════════════════════════════════════════════════════════
# 5) 뮤테이션 — 배선을 망가뜨린다 (이 스위트의 핵심)
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("5) 뮤테이션(배선) — 함수는 멀쩡한데 호출부만 틀린 경우")
print("=" * 74)

WIRE_MUTANTS = [
    ("W-M1 _ET → _KST (14:00 ET = 03:00 KST 라 가드가 통째로 통과)",
     "_intraday_close_passed(datetime.now(_ET), _ct)",
     "_intraday_close_passed(datetime.now(_KST), _ct)",
     "first_arg_et"),
    ("W-M2 mode 조건 삭제 (EOD 까지 스킵 → 확정 알림 유실)",
     'if args.mode == "intraday" and args.scope != "metrics":',
     'if args.scope != "metrics":',
     "mode_guarded"),
    ("W-M3 return → pass (스킵 판정 후 그대로 진행)",
     '확정 결과는 5PM EOD 실행이 보낸다.")\n                    return',
     '확정 결과는 5PM EOD 실행이 보낸다.")\n                    pass',
     "returns"),
    ("W-M4 호출부 제거 (if False 로 무력화)",
     "if _intraday_close_passed(datetime.now(_ET), _ct):",
     "if False:",
     "calls"),
    ("W-M5 마감시각을 '13:00' 으로 하드코딩 (캘린더 이탈)",
     "_ct = cc.session_close_time(None)",
     '_ct = "13:00"',
     "close_from_core"),
]

for name, old, new, key in WIRE_MUTANTS:
    n_occ = SRC.count(old)
    if n_occ != 1:
        print("  ⚠️  " + name + " — 앵커가 " + str(n_occ) + "회(1회여야 함). 스킵")
        _fails.append(name + " [앵커 " + str(n_occ) + "회]")
        continue
    mw = wiring(SRC.replace(old, new, 1))
    if mw.get(key) != W.get(key):
        print("  ✅ " + name + " — 1절 " + key + " 가 빨간불로 뒤집힘")
        _passes += 1
    else:
        print("  ❌ " + name + " — **1절이 잡아내지 못함. 배선 검사가 부실하다**")
        _fails.append(name)

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
