"""diag_market_status.py — app.py get_market_status() 휴장일 연결 회귀 검증 + 뮤테이션 테스트.

목적
────
app.py 의 시장 상태 라벨이 calendar_core 를 통해
  (1) 휴장일  (2) 반일장(13:00 조기 마감)
을 인식하는지 검증한다.

원래는 `weekday() >= 5` 만 봤다 → 추수감사절 10:00 에 "Open" 이 떴다(A-1 잔여).
그 다음 단계가 반일장이다 → 11/27 14:00 에 여전히 "Open" 이 떴다. 장은 13:00 에
끝나 있었다.

⚠️ 설계 원칙: **로직을 복사하지 않는다.**
   app.py 를 AST 로 파싱해 get_market_status 의 **실제 소스**를 꺼내 exec 한다.
   복사본을 테스트하면 app.py 를 안 고쳐도 통과하는 가짜 초록불이 된다
   (과거 M1/M2 클래스 실패). 소스가 바뀌면 테스트도 같이 바뀐다.

실행: python3 diag_market_status.py [app.py 경로]
"""
import ast
import sys
import os
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

# automation/ 에서 실행돼도 repo root 의 calendar_core·app.py 를 찾도록 한다
# (diag_market_calendar.py / diag_reminders.py 와 동일한 관용구).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import calendar_core as cc  # noqa: E402


def _find_app():
    for base in (_ROOT, _HERE):
        p = os.path.join(base, "app.py")
        if os.path.exists(p):
            return p
    return os.path.join(_ROOT, "app.py")


APP = sys.argv[1] if len(sys.argv) > 1 else _find_app()
FUNC = "get_market_status"

_fails = []
_passes = 0


def check(name, got, want):
    global _passes
    if got == want:
        _passes += 1
        print(f"  ✅ {name}")
    else:
        _fails.append(name)
        print(f"  ❌ {name}\n       기대: {want!r}\n       실제: {got!r}")


def et(y, mo, d, h, mi):
    dt = datetime(y, mo, d, h, mi)
    return dt.replace(tzinfo=_ET) if _ET else dt


# ══════════════════════════════════════════════════════════════════════════
# 0) AST 추출 — app.py 의 진짜 소스를 꺼낸다
# ══════════════════════════════════════════════════════════════════════════
SRC = open(APP, encoding="utf-8").read()
TREE = ast.parse(SRC)


def extract_func(src, tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node), node
    return None, None


def load(func_src):
    """함수 소스를 실제 calendar_core 를 mcal 로 묶어 exec."""
    ns = {"mcal": cc}
    exec(compile(func_src, "<extracted>", "exec"), ns)
    return ns[FUNC]


# ══════════════════════════════════════════════════════════════════════════
# 1) 정적 검사 — 배선이 실제로 됐는지
# ══════════════════════════════════════════════════════════════════════════
print("=" * 74)
print("1) 정적 검사 — import 배선 / 별칭 충돌")
print("=" * 74)

alias = None
for node in TREE.body:
    if isinstance(node, ast.Import):
        for a in node.names:
            if a.name == "calendar_core":
                alias = a.asname or a.name
check("S1  app.py 가 calendar_core 를 모듈 레벨에서 import", alias is not None, True)
check("S2  별칭이 mcal", alias, "mcal")

# 별칭 섀도잉 — mcal 이 어디선가 재대입되면 그 스코프에서 모듈이 죽는다.
shadow = []
for node in ast.walk(TREE):
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == "mcal":
        shadow.append(node.lineno)
    if isinstance(node, ast.arg) and node.arg == "mcal":
        shadow.append(node.lineno)
check("S3  mcal 이 어디서도 재대입되지 않음", shadow, [])

# 반대 방향 확인 — cc 를 안 쓴 게 맞는지(지역변수 cc="close" 3곳과의 충돌 회피)
cc_local = sorted({n.lineno for n in ast.walk(TREE)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == "cc"})
print(f"  ℹ️  지역변수 cc 대입 위치: {cc_local}  (별칭을 cc 로 뒀다면 이 스코프에서 모듈이 가려짐)")

func_src, func_node = extract_func(SRC, TREE, FUNC)
check("S4  get_market_status 소스 추출 성공", func_src is not None, True)

# 휴장/마감시각 판정이 시간대 분기 **앞**에 있어야 한다(순서 뒤집히면 무력화됨)
if func_src:
    i_cal = func_src.find("session_close_time")
    i_minute = func_src.find("et_now.hour * 60")
    check("S5  마감시각 판정이 시간대 분기보다 앞", (i_cal != -1 and i_cal < i_minute), True)
    # 마감 밴드가 하드코딩 959/960 으로 되돌아가면 반일장이 다시 죽는다.
    check("S6  정규장 종료 경계가 하드코딩이 아니다(_close_m 사용)",
          ("_close_m" in func_src) and ("<= 959" not in func_src), True)

# ══════════════════════════════════════════════════════════════════════════
# 2) 동작 검사
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("2) 동작 검사 — 휴장일 / 주말 / 평일 / 시간대")
print("=" * 74)

f = load(func_src)

OPEN = "🟢 Regular Market (Open)"
PRE = "🌅 Pre-market"
AFT = "🌙 After-hours"
CLS = "🌑 Market Closed"
WKD = "🌑 Market Closed (Weekend)"
OPEN_H = "🟢 Regular Market (반일장 · 13:00 마감)"
PRE_H = "🌅 Pre-market (반일장)"
AFT_H = "🌙 After-hours (반일장)"

# --- 결함 재현 케이스: 이 4건이 수정 전에는 전부 OPEN 이었다 ---
check("T1  추수감사절 2026-11-26(목) 10:00", f(et(2026, 11, 26, 10, 0)), f"{CLS} (Thanksgiving Day)")
check("T2  굿프라이데이 2026-04-03(금) 11:30", f(et(2026, 4, 3, 11, 30)), f"{CLS} (Good Friday)")
check("T3  독립기념일 대체 2026-07-03(금) 14:00", f(et(2026, 7, 3, 14, 0)), f"{CLS} (Independence Day)")
check("T4  성탄절 2026-12-25(금) 09:45", f(et(2026, 12, 25, 9, 45)), f"{CLS} (Christmas Day)")

# --- 하드코딩 만료 구간(2027) — A-1 이 고친 바로 그 경계 ---
check("T5  2027-01-01(금) 신정 10:00", f(et(2027, 1, 1, 10, 0)), f"{CLS} (New Year's Day)")
check("T6  2027-07-05(월) 독립기념일 대체 10:00", f(et(2027, 7, 5, 10, 0)), f"{CLS} (Independence Day)")
check("T7  2027-11-25(목) 추수감사절 10:00", f(et(2027, 11, 25, 10, 0)), f"{CLS} (Thanksgiving Day)")

# --- 휴장일에는 프리마켓/애프터도 없다 ---
check("T8  휴장일 프리마켓 시간 08:00", f(et(2026, 11, 26, 8, 0)), f"{CLS} (Thanksgiving Day)")
check("T9  휴장일 애프터 시간 17:00", f(et(2026, 11, 26, 17, 0)), f"{CLS} (Thanksgiving Day)")

# --- 회귀 방지: 기존 동작이 그대로여야 한다 ---
check("T10 평일 2026-08-20(목) 10:00", f(et(2026, 8, 20, 10, 0)), OPEN)
check("T11 평일 개장 직전 09:29", f(et(2026, 8, 20, 9, 29)), PRE)
check("T12 평일 개장 09:30", f(et(2026, 8, 20, 9, 30)), OPEN)
check("T13 평일 마감 15:59", f(et(2026, 8, 20, 15, 59)), OPEN)
check("T14 평일 16:00 애프터", f(et(2026, 8, 20, 16, 0)), AFT)
check("T15 평일 20:00 마감", f(et(2026, 8, 20, 20, 0)), CLS)
check("T16 평일 03:00 마감", f(et(2026, 8, 20, 3, 0)), CLS)
check("T17 토요일 2026-08-22 10:00", f(et(2026, 8, 22, 10, 0)), WKD)
check("T18 일요일 2026-08-23 10:00", f(et(2026, 8, 23, 10, 0)), WKD)
check("T19 None 입력", f(None), CLS)

# --- 휴장 전날/다음날은 정상 개장이어야 한다(경계 오프바이원) ---
check("T20 추수감사절 전날 2026-11-25(수) 10:00", f(et(2026, 11, 25, 10, 0)), OPEN)
check("T21 추수감사절 다음날 2026-11-27(금) 10:00 — 반일장 장중",
      f(et(2026, 11, 27, 10, 0)), OPEN_H)

# ══════════════════════════════════════════════════════════════════════════
# 2-b) 반일장(조기 마감) — 이전에는 "알려진 한계"로 남겨뒀던 자리
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("2-b) 반일장 — 13:00 조기 마감")
print("=" * 74)

# ★ 결함 재현: 수정 전에는 이 3건이 전부 OPEN 이었다
check("H1  2026-11-27 14:00 — 마감 1시간 후",
      f(et(2026, 11, 27, 14, 0)), AFT_H)
check("H2  2026-12-24 14:30 — 성탄절 전날",
      f(et(2026, 12, 24, 14, 30)), AFT_H)
check("H3  2025-07-03 15:00 — 독립기념일 전날(2025)",
      f(et(2025, 7, 3, 15, 0)), AFT_H)

# --- 경계 (오프바이원이 가장 잘 숨는 자리) ---
check("H4  12:59 — 마감 1분 전은 아직 장중", f(et(2026, 11, 27, 12, 59)), OPEN_H)
check("H5  13:00 — 마감 정각은 애프터", f(et(2026, 11, 27, 13, 0)), AFT_H)
check("H6  09:29 — 개장 1분 전 프리마켓", f(et(2026, 11, 27, 9, 29)), PRE_H)
check("H7  09:30 — 개장 정각", f(et(2026, 11, 27, 9, 30)), OPEN_H)
check("H8  16:59 — 애프터 마지막(13:00+4h)", f(et(2026, 11, 27, 16, 59)), AFT_H)
check("H9  17:00 — 애프터 종료", f(et(2026, 11, 27, 17, 0)), CLS)

# --- 반일장이 아닌 날은 그대로여야 한다(과대적용 방지) ---
check("H10 2026-11-25(수) 14:00 — 평범한 날은 여전히 장중",
      f(et(2026, 11, 25, 14, 0)), OPEN)
check("H11 2026-11-25(수) 15:59 — 정규 마감 1분 전", f(et(2026, 11, 25, 15, 59)), OPEN)
check("H12 2026-11-26(목) 추수감사절은 전휴장(반일장 아님)",
      f(et(2026, 11, 26, 10, 0)), f"{CLS} (Thanksgiving Day)")
# 2026-07-03 은 7/4 가 토요일이라 **전휴장**이다. 반일장으로 오판하면 안 된다.
check("H13 2026-07-03 — 7/4 토요일이라 전휴장(반일장 아님)",
      f(et(2026, 7, 3, 11, 0)), f"{CLS} (Independence Day)")

# --- 규칙 자체 검증 (calendar_core) — 실측·이력 7년과 대조 ---
GOLD = {
    2020: {"2020-11-27", "2020-12-24"},          # NYSE 이력
    2021: {"2021-11-26"},
    2022: {"2022-11-25"},
    2023: {"2023-07-03", "2023-11-24"},
    2024: {"2024-07-03", "2024-11-29", "2024-12-24"},
    2025: {"2025-07-03", "2025-11-28", "2025-12-24"},   # FMP 실측(diag_halfday)
    2026: {"2026-11-27", "2026-12-24"},                 # FMP 실측
}
for _y in sorted(GOLD):
    check("H%d  규칙 반일장 %d" % (13 + list(sorted(GOLD)).index(_y) + 1, _y),
          set(cc.nyse_early_close_days(_y)), GOLD[_y])

# ══════════════════════════════════════════════════════════════════════════
# 3) 뮤테이션 테스트 — 결함을 심었을 때 위 검사가 잡아내는가
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
print("3) 뮤테이션 테스트 — 결함을 심으면 반드시 실패해야 한다")
print("=" * 74)

MUTANTS = [
    ("M1 휴장 판정 제거 — 마감시각을 항상 16:00 으로(반일장 무시, 수정 전 상태)",
     "_ct = mcal.session_close_time(et_now)", '_ct = "16:00"'),
    ("M2 휴장(None) 분기 제거 — 휴장일에도 시간대 로직으로 진행",
     "if _ct is None:", "if False:"),
    ("M3 휴장일에 개장 라벨 반환",
     'return f"🌑 Market Closed ({_hn})" if _hn else "🌑 Market Closed (Holiday)"',
     'return "🟢 Regular Market (Open)"'),
    ("M4 주말 분기를 휴장 분기가 삼킴(Weekend 라벨 소실)",
     'return "🌑 Market Closed (Weekend)"', 'pass'),
    # ★ 반일장 전용 — 경계 오프바이원. 13:00 정각을 장중으로 만든다.
    ("M5 정규장 종료 경계 < 를 <= 로 (마감 정각이 장중으로 샌다)",
     "if 570 <= m < _close_m:", "if 570 <= m <= _close_m:"),
    # ★ 반일장 판정만 죽인다 — 라벨은 정상인데 밴드가 16:00 로 남는 경우
    ("M6 _early 를 항상 False 로 (반일장 라벨만 소실)",
     "_early = (_ct != mcal.REGULAR_CLOSE_TIME)", "_early = False"),
    ("M7 마감 분 계산을 상수로 고정(close_minutes 무력화)",
     "_close_m = mcal.close_minutes(_ct)", "_close_m = 960"),
]

for name, old, new in MUTANTS:
    if old not in func_src:
        print(f"  ⚠️  {name} — 대상 문자열 없음(소스 변경?). 스킵")
        continue
    mf = load(func_src.replace(old, new, 1))
    caught = False
    for dt_, want in [
        (et(2026, 11, 26, 10, 0), f"{CLS} (Thanksgiving Day)"),
        (et(2026, 4, 3, 11, 30), f"{CLS} (Good Friday)"),
        (et(2026, 8, 20, 10, 0), OPEN),
        (et(2026, 8, 22, 10, 0), WKD),
        # 반일장 — M1/M5/M6/M7 은 여기서만 잡힌다
        (et(2026, 11, 27, 14, 0), AFT_H),
        (et(2026, 11, 27, 13, 0), AFT_H),
        (et(2026, 11, 27, 10, 0), OPEN_H),
        (et(2026, 11, 25, 14, 0), OPEN),
    ]:
        try:
            if mf(dt_) != want:
                caught = True
                break
        except Exception:
            caught = True
            break
    if caught:
        print(f"  ✅ {name} — 검사가 잡아냄")
        _passes += 1
    else:
        print(f"  ❌ {name} — **잡아내지 못함. 테스트가 부실하다**")
        _fails.append(name)

# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 74)
if _fails:
    print(f"❌ 실패 {len(_fails)}건 / 통과 {_passes}건")
    for x in _fails:
        print("   -", x)
    sys.exit(1)
print(f"✅ 전부 통과 — {_passes}건")
print("=" * 74)
