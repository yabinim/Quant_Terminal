# -*- coding: utf-8 -*-
"""diag_satellite_mandate.py — SATELLITE_MANDATE.md ↔ 코드 드리프트 가드.

왜 필요한가
───────────
md §6 은 스스로 이렇게 선언한다: *"정직하게 적는다. 문서에 썼다고 작동하는 게
아니다."* 그런데 그 선언을 지킬 장치가 없었다. 문서가 "✅ 작동 중"이라고 적어도
아무도 확인하지 않는다.

이 진단은 그 선언을 **검사로 강제한다.**

  · 문서의 숫자가 코드 상수와 같은가 (계좌명·개시일·슬롯·봉수·−30%)
  · 문서의 공식이 코드의 실제 계산과 같은가 (blend 가중치를 **실행해서** 확인)
  · 문서의 §6 상태표가 실제 구축 상태와 같은가 ← 이 진단의 본체
  · 소비자가 실제로 SSOT 를 부르는가 (앱·주간 메일·러너)
  · 앱이 문안을 복사하지 않았는가 (두 벌이 되면 −30% 날 어느 쪽이 진짜인지 모른다)

불변식:
  A1  §1 표의 두 장부 이름 = fmp_extras.SATELLITE_BOOKS
  A2  §1 개시일 = SATELLITE_START_DATE, §4③ 시드 문구와도 일치
  A3  §1 "최대 5종목" = SATELLITE_SLOTS
  B1  §2 A반 룰 리터럴 = SATELLITE_RANK_RULE, MOM_RULES 에 실재
  B2  §2 "소요 이력 N봉" = SATELLITE_BARS (리터럴 아닌 파생값과 대조)
  B3  §2 B반 공식의 가중치가 score_blend 의 **실제 출력**과 일치
  C1  §4③ "−30%" = SATELLITE_DRAWDOWN_TRIGGER, 부호까지 음수
  D   낙폭 판정 회귀 — 특히 기록 누락·빈 장부가 가짜 낙폭을 만들지 않는가
  E1  §6 ③ 이 ✅ 라면 현금이 실제로 기록돼야 한다. 러너가 Cash 를 공란으로
      두는 한 ✅ 는 거짓이다 → ⚠️ 여야 한다
  E2  §6 ③ 이 ❌ 미구축이면 안 된다 (스냅샷은 구축됐다)
  F   소비자 배선 — app.py · run_hidden_alpha.py 가 fmp_extras 판정 함수를
      실제로 호출하고, 러너 파일과 워크플로 스텝이 존재하는가
  G   app.py 가 md 문안을 복사하지 않았는가
  J   §4① 깊은 창 참고 실행 사전 약정 ↔ 러너 상수 (임계·반등·룰·필터·기록 탭),
      §2 약점 각주의 '바꾸지 않는다' 유지
  H   양성 대조 — 알려진 불량 입력에서 각 검사가 실제로 실패하는가

⚠️ 로직을 복사하지 않는다. fmp_extras 의 실제 함수를 부른다.

사용법:  python3 automation/diag_satellite_mandate.py
안전성:  네트워크·시트·FMP·메일 접근 없음. 시크릿 불필요. 부작용 없음.
"""
import ast
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE) if os.path.basename(_HERE) == "automation" else _HERE
sys.path.insert(0, _ROOT)

import fmp_extras as fx  # noqa: E402

PASS, FAIL = [], []


def chk(name, got, exp):
    (PASS if got == exp else FAIL).append(f"{name}: got={got!r} exp={exp!r}")


def fx_snapshot_modes():
    """러너 소스에서 SNAPSHOT_MODES 키만 뽑는다.

    임포트하지 않는 이유: 러너는 gspread·google-auth 를 최상단에서 요구한다.
    진단은 네트워크·시크릿 없이 돌아야 하므로 AST 로 읽는다.
    """
    src = SNAP or ""
    for n in ast.walk(ast.parse(src)) if src else []:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == "SNAPSHOT_MODES":
                    if isinstance(n.value, ast.Dict):
                        return [k.value for k in n.value.keys
                                if isinstance(k, ast.Constant)]
    return []


def _read(*parts):
    p = os.path.join(_ROOT, *parts)
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


MD = _read("SATELLITE_MANDATE.md")
APP = _read("app.py")
HA = _read("automation", "run_hidden_alpha.py") or _read("run_hidden_alpha.py")
SNAP = (_read("automation", "run_satellite_snapshot.py")
        or _read("run_satellite_snapshot.py"))
WF = _read(".github", "workflows", "market_5pm_weekday.yml") or _read("market_5pm_weekday.yml")
WF_SEED = (_read(".github", "workflows", "seed_satellite_snapshot.yml")
           or _read("seed_satellite_snapshot.yml"))

chk("MD-0 문서 존재", MD is not None, True)
if MD is None:
    print("SATELLITE_MANDATE.md 를 읽지 못해 중단합니다.")
    sys.exit(1)


# ══ [A] §1 확정 숫자 ↔ 코드 상수 ══════════════════════════════════════════
def md_books(text):
    """§1 표의 `백틱 안 계좌명` 을 등장 순서대로."""
    out = []
    for line in text.splitlines():
        if "반 (" in line and line.strip().startswith("|"):
            m = re.search(r"`([^`]+)`", line)
            if m:
                out.append(m.group(1))
    return out


chk("A1 §1 장부 이름 = SATELLITE_BOOKS",
    tuple(md_books(MD)), tuple(fx.SATELLITE_BOOKS))

m = re.search(r"A/B 실험 개시일\s*\|\s*\*\*(\d{4}-\d{2}-\d{2})\*\*", MD)
chk("A2 §1 개시일 = SATELLITE_START_DATE",
    m.group(1) if m else None, fx.SATELLITE_START_DATE)
chk("A2b §4③ 시드 문구가 같은 개시일을 쓴다",
    f"개시일 시드({fx.SATELLITE_START_DATE})" in MD, True)

m = re.search(r"각 반 보유 종목\s*\|\s*최대 \*\*(\d+)종목", MD)
chk("A3 §1 최대 종목수 = SATELLITE_SLOTS",
    int(m.group(1)) if m else None, fx.SATELLITE_SLOTS)


# ══ [B] §2 랭킹 규칙 ↔ 코드 ═══════════════════════════════════════════════
m = re.search(r"\*\*A반\*\*\s*—\s*`([^`]+)`", MD)
chk("B1 §2 A반 룰 = SATELLITE_RANK_RULE",
    m.group(1) if m else None, fx.SATELLITE_RANK_RULE)
chk("B1b A반 룰이 MOM_RULES 에 실재", fx.SATELLITE_RANK_RULE in fx.MOM_RULES, True)
chk("B1c B반 룰이 MOM_RULES 에 실재", fx.SATELLITE_ALT_RULE in fx.MOM_RULES, True)
chk("B1d B반 장부 이름이 B반 룰명을 담는다",
    fx.SATELLITE_BOOK_B.endswith(fx.SATELLITE_ALT_RULE), True)

m = re.search(r"소요 이력\s*(\d+)\s*봉", MD)
chk("B2 §2 소요 봉수 = SATELLITE_BARS (파생값)",
    int(m.group(1)) if m else None, fx.SATELLITE_BARS)

# B3 — 문서의 가중치를 **실행해서** 확인한다. 텍스트끼리 비교하면 문서가
#      코드를 따라간 게 아니라 서로 다른 두 문자열이 우연히 같은 것뿐이다.
m = re.search(r"\*\*B반\*\*\s*—\s*`1M×(\d+)%\s*\+\s*3M×(\d+)%\s*\+\s*6M×(\d+)%`", MD)
if not m:
    chk("B3 §2 B반 공식 파싱", False, True)
else:
    w1, w3, w6 = (int(x) / 100.0 for x in m.groups())
    chk("B3a 가중치 합 = 1.0", round(w1 + w3 + w6, 6), 1.0)
    import numpy as _np
    _rng = _np.random.default_rng(20260907)
    _ok = True
    for _ in range(5):
        vals = _np.cumprod(1 + _rng.normal(0.0004, 0.011, fx.SATELLITE_BARS))
        c = fx.mom_components(vals)
        want = w1 * c["r1m"] + w3 * c["r3m"] + w6 * c["r6m"]
        got = fx.mom_score(vals, fx.SATELLITE_ALT_RULE)
        if not (abs(got - want) < 1e-9):
            _ok = False
    chk("B3b 문서 가중치가 score_blend 실제 출력과 일치", _ok, True)


# ══ [C] §4③ 트리거 ═══════════════════════════════════════════════════════
m = re.search(r"\*\*트리거\*\*\s*고점 대비\s*\*\*[−-](\d+)%\*\*", MD)
chk("C1 §4③ 트리거 크기 = SATELLITE_DRAWDOWN_TRIGGER",
    -int(m.group(1)) / 100.0 if m else None, fx.SATELLITE_DRAWDOWN_TRIGGER)
chk("C1b 트리거는 음수 — abs() 비교로 되돌리면 +30% 상승도 통과한다",
    fx.SATELLITE_DRAWDOWN_TRIGGER < 0, True)


# ══ [D] 낙폭 판정 회귀 (실제 함수 호출) ═══════════════════════════════════
_A, _B = fx.SATELLITE_BOOK_A, fx.SATELLITE_BOOK_B
_H = fx.SATELLITE_SNAPSHOT_COLS


def _row(d, b, close, n=None, src=fx.SATELLITE_SNAP_MONTHLY, cash=None):
    n = fx.SATELLITE_SLOTS if n is None else n
    hold = {f"X{i}": {"qty": 10, "close": close} for i in range(n)}
    return fx.satellite_snapshot_row(d, b, hold, cash=cash, source=src)


def _dd(vals):
    return fx.satellite_drawdown(fx.parse_satellite_snapshots(vals))


_seed = [_row("2026-09-07", _A, 100.0, src=fx.SATELLITE_SNAP_SEED),
         _row("2026-09-07", _B, 100.0, src=fx.SATELLITE_SNAP_SEED)]

d = _dd([_H] + _seed + [_row("2026-10-30", _A, 60.0), _row("2026-10-30", _B, 60.0)])
chk("D1 −40% 는 발동", (round(d["drawdown"], 6), d["triggered"]), (-0.4, True))

d = _dd([_H] + _seed + [_row("2026-10-30", _A, 70.0), _row("2026-10-30", _B, 70.0)])
chk("D2 정확히 −30% 경계는 발동(이하 비교)",
    (round(d["drawdown"], 6), d["triggered"]), (-0.3, True))

d = _dd([_H] + _seed + [_row("2026-10-30", _A, 71.0), _row("2026-10-30", _B, 71.0)])
chk("D3 −29% 는 발동하지 않는다", d["triggered"], False)

d = _dd([_H] + _seed + [_row("2026-10-30", _A, 200.0), _row("2026-10-30", _B, 200.0)])
chk("D4 +100% 상승은 발동하지 않고 낙폭 0", (d["triggered"], d["drawdown"]), (False, 0.0))

# ⚠️ D5·D6 이 이 진단에서 가장 값비싼 두 줄이다. 둘 다 **기록의 결함이
#    손실로 둔갑하는** 경로를 막는다. 지우면 다음이 조용히 통과한다:
#      · 한 장부 기록 누락 → 슬리브가 반토막으로 찍혀 −50% 가짜 발동
#      · 시장 필터로 전량 현금화 → 빈 장부를 0 으로 합산해 가짜 발동
#    즉 **규칙을 지킨 행동이 트리거를 당긴다.**
d = _dd([_H] + _seed + [_row("2026-10-30", _A, 100.0)])
chk("D5 한 장부만 기록된 날짜는 합산 제외 (가짜 −50% 방지)",
    (d["drawdown"], d["triggered"], d["incomplete_dates"]),
    (0.0, False, ["2026-10-30"]))

d = _dd([_H] + _seed + [_row("2026-10-30", _A, 100.0),
                        _row("2026-10-30", _B, 100.0, n=0)])
chk("D6 빈 장부 + 현금 미추적 날짜는 평가 불가로 제외",
    (d["drawdown"], d["triggered"], d["unvaluable_dates"]),
    (0.0, False, ["2026-10-30"]))

d = _dd([_H] + _seed + [_row("2026-10-30", _A, 100.0),
                        _row("2026-10-30", _B, 100.0, n=0, cash=5000.0)])
chk("D7 현금을 기록하면 빈 장부도 평가 가능", len(d["series"]), 2)

chk("D8 Cash 공란과 0 은 다르다 (미추적 vs 재서 0)",
    (_row("2026-11-30", _A, 100.0)[_H.index("Cash")],
     _row("2026-11-30", _A, 100.0, cash=0.0)[_H.index("Cash")]), ("", 0.0))

_perm = [_H[1], _H[0]] + _H[2:]
_base = [_H] + _seed + [_row("2026-10-30", _A, 60.0), _row("2026-10-30", _B, 60.0)]
_pm = [_perm] + [[r[1], r[0]] + r[2:] for r in _base[1:]]
chk("D9 열 순서를 바꿔도 이름으로 읽는다",
    round(_dd(_pm)["drawdown"], 6), round(_dd(_base)["drawdown"], 6))

chk("D10 스냅샷 0건은 ok=False (0% 로 위장하지 않는다)",
    fx.satellite_drawdown([])["ok"], False)

chk("D11 고점은 시드를 포함한다 (첫 달 공백 방지)",
    _dd(_base)["peak_date"], "2026-09-07")

# ── D12·D13: 트리거 비교식의 **부호 안전성** ──────────────────────────────
# 왜 두 줄이나 쓰나: 돌연변이 시험에서 `dd <= TRIGGER` 를
# `abs(dd) >= abs(TRIGGER)` 로 바꿔도 아무 검사가 안 걸렸다(2026-09-07, M9).
# 원인을 따져보니 **등가 돌연변이**였다 — 고점을 '현재 포함 최댓값'으로 잡으므로
# dd 는 항상 0 이하이고, 그 조건에서 두 식은 같다.
#
# 지금은 같지만 영원히 같지는 않다. 고점 정의를 '직전까지의 최댓값'으로 바꾸면
# dd 가 양수가 될 수 있고, 그 순간 abs() 형태는 **+30% 상승에서 축소를
# 발동시킨다.** D12 는 그 전제(dd ≤ 0)를 불변식으로 못 박고, D13 은 전제가
# 깨진 날 비교식이 반드시 함께 검토되도록 소스를 잠근다.
import random as _rnd

_rr = _rnd.Random(20260907)
_max_dd = -1.0
for _ in range(200):
    _vals = [_H]
    _prev_dates = []
    for _i in range(_rr.randint(2, 8)):
        _d = f"2026-{_rr.randint(1, 12):02d}-{_rr.randint(1, 28):02d}"
        if _d in _prev_dates:
            continue
        _prev_dates.append(_d)
        _c = _rr.uniform(1.0, 500.0)
        _vals.append(_row(_d, _A, _c))
        _vals.append(_row(_d, _B, _rr.uniform(1.0, 500.0)))
    _o = _dd(_vals)
    if _o.get("ok") and _o.get("drawdown") is not None:
        _max_dd = max(_max_dd, _o["drawdown"])
chk("D12 낙폭은 어떤 입력에서도 양수가 되지 않는다 (고점 = 현재 포함 최댓값)",
    _max_dd <= 0.0, True)

_FX_SRC = _read("fmp_extras.py") or ""
_trig_lines = [ln.strip() for ln in _FX_SRC.splitlines()
               if 'out["triggered"]' in ln and "=" in ln]
chk("D13a 트리거 판정 대입은 정확히 한 곳", len(_trig_lines), 1)
chk("D13b 비교에 abs() 를 쓰지 않는다 — dd ≤ 0 전제가 깨지면 +30% 상승이 발동한다",
    "abs(" in (_trig_lines[0] if _trig_lines else "abs("), False)
chk("D13c 비교는 '이하'(<=) 다 — '미만'이면 정확히 −30%가 빠져나간다",
    "<=" in (_trig_lines[0] if _trig_lines else ""), True)


# ══ [E] §6 상태표 ↔ 실제 구축 상태 ═══════════════════════════════════════
def md_row6(text, key):
    for line in text.splitlines():
        if line.strip().startswith("|") and key in line:
            return line
    return ""


_r3 = md_row6(MD, "③ 자본 손상")
chk("E0 §6 ③ 행 존재", bool(_r3), True)


def status_mark(row_line):
    """상태 셀의 **선두 마커**만 본다.

    ⚠️ 행 전체에서 이모지를 찾으면 안 된다. 상태 셀 본문에 "현금을 관리하면
       ✅ 로 올린다" 같은 안내가 있으면 그것까지 상태로 읽혀 오탐한다
       (2026-09-07 이 진단을 처음 돌렸을 때 실제로 걸렸다).
    """
    cells = [c.strip() for c in (row_line or "").split("|")]
    cell = cells[2] if len(cells) > 2 else ""
    for mk in ("✅", "⚠️", "❌"):
        if cell.startswith(mk):
            return mk
    return ""


# 러너가 Cash 를 실제로 채우는가 — 채우지 않으면 ✅ 는 거짓이다.
_cash_written = bool(SNAP) and "cash=None" not in (SNAP or "")
chk("E1 ✅ 는 현금을 실제로 기록할 때만 (지금은 공란이므로 ⚠️ 여야 한다)",
    (status_mark(_r3) == "✅"), _cash_written)
chk("E1b §6 ③ 이 ⚠️ 로 한계를 밝힌다", status_mark(_r3), "⚠️")
chk("E2 §6 ③ 이 '❌ 미구축' 이 아니다 (스냅샷은 구축됐다)",
    status_mark(_r3) == "❌", False)
chk("E3 §4③ 이 현금 미추적 한계를 문서에 남긴다",
    ("현금은 추적하지 않는다" in MD) and ("과대" in MD) and ("늦게" in MD), True)
chk("E4 한계를 이유로 트리거를 완화하지 않았다",
    "−30% 는 그대로 둔다" in MD, True)


# ══ [F] 소비자 배선 ═══════════════════════════════════════════════════════
def _alias_of(src, module):
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == module:
                    return a.asname or a.name
    return None


def _call_count(src, alias, attr):
    """실제 **호출** 횟수. 문자열 카운트로 세면 안 된다 — 같은 이름을 언급한
    주석까지 세어져, 주석 한 줄을 늘린 것만으로 진단이 빨갛게 된다."""
    if not (src and alias):
        return 0
    n = 0
    for node in ast.walk(ast.parse(src)):
        f = getattr(node, "func", None)
        if (isinstance(node, ast.Call) and isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name) and f.value.id == alias
                and f.attr == attr):
            n += 1
    return n


def _calls(src, alias, attr):
    if not (src and alias):
        return False
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == alias and n.attr == attr):
            return True
    return False


chk("F0 러너 파일 존재 (automation/run_satellite_snapshot.py)", SNAP is not None, True)
chk("F1 app.py 가 fmp_extras.satellite_drawdown 을 호출",
    _calls(APP, _alias_of(APP, "fmp_extras"), "satellite_drawdown"), True)
chk("F2 run_hidden_alpha 가 fmp_extras.satellite_drawdown 을 호출",
    _calls(HA, _alias_of(HA, "fmp_extras"), "satellite_drawdown"), True)
chk("F3 러너가 fmp_extras.satellite_snapshot_row 를 호출",
    _calls(SNAP, _alias_of(SNAP, "fmp_extras"), "satellite_snapshot_row"), True)
chk("F4 러너가 계좌명을 로컬에 다시 쓰지 않는다",
    (fx.SATELLITE_BOOK_A in (SNAP or "")), False)
# app.py 의 expander 제목과 md 읽기 실패 시 폴백 문구에는 −30% 가 **문장으로**
# 들어 있다. 그 자체는 정당하다(파일을 못 읽었을 때 요약이라도 보여야 한다).
# 문제는 그 숫자가 md 와 따로 늙는 것이다 — 존재를 금지하는 대신 값을 맞춘다.
_app_pcts = {int(x) for x in re.findall(r"−(\d+)%", APP or "")}
chk("F5 app.py 안의 −NN% 문구가 전부 md 트리거와 같은 값",
    _app_pcts, {int(abs(fx.SATELLITE_DRAWDOWN_TRIGGER * 100))})
chk("F6 워크플로에 스냅샷 스텝이 배선돼 있다",
    "run_satellite_snapshot.py" in (WF or ""), True)
chk("F7 주간 메일에 낙폭 섹션 빌더가 있다",
    "build_drawdown_html" in (HA or ""), True)
chk("F8 낙폭 섹션이 Top10 성공에 묶여 있지 않다 (독립 호출)",
    "drawdown_html = build_drawdown_html" in (HA or ""), True)

# F9·F10 — 수동 워크플로와 스크립트의 **모드 어휘**가 같은가.
# yml 이 고른 문자열을 그대로 넘기므로, 두 목록이 갈라지면 사용자는 초록불을
# 보면서 아무 일도 안 일어난 것을 모른다(스크립트가 죽도록 만들어 뒀지만,
# 애초에 갈라지지 않게 여기서 잠근다).
_wf_modes = set()
if WF_SEED:
    _in_opts = False
    for _ln in WF_SEED.splitlines():
        _t = _ln.strip()
        if _t.startswith("options:"):
            _in_opts = True
            continue
        if _in_opts:
            if _t.startswith("- ") and not _t.startswith("- name"):
                _wf_modes.add(_t[2:].strip().strip("'\""))
            elif _t and not _t.startswith("#"):
                _in_opts = False

chk("F9 수동 워크플로 존재 (seed_satellite_snapshot.yml)", WF_SEED is not None, True)
chk("F10a 워크플로 선택지가 전부 스크립트가 아는 모드",
    bool(_wf_modes) and _wf_modes <= set(fx_snapshot_modes()), True)
chk("F10b 시드 모드가 선택지에 있다 (없으면 기준선을 만들 방법이 없다)",
    {"seed", "seed_dryrun"} <= _wf_modes, True)
chk("F10c yml 이 표현식이 아니라 입력값을 그대로 넘긴다 (2026-08-22 사고 방지)",
    "SNAPSHOT_MODE: ${{ inputs.mode }}" in (WF_SEED or ""), True)


# ══ [G] 문안 중복 금지 ════════════════════════════════════════════════════
# md 의 특징적인 문장이 app.py 에 복사돼 있으면 두 벌이 된다. 앱은 파일을
# 읽어서 렌더해야 한다 — 문안이 갈라지면 −30% 날 어느 쪽이 진짜인지 모른다.
_FINGERPRINTS = [
    "이 문서는 **−30% 구간의 나**를 위해 미리 적은 것이다",
    "손실을 본 뒤에 이 문서를 고치는 것은 규칙을 지킨 것이 아니라 없앤 것이다",
    "한쪽이 앞서면 옮기고 싶어진다",
]
for i, fp in enumerate(_FINGERPRINTS, 1):
    chk(f"G{i} md 문장이 md 에 실재", fp in MD, True)
    chk(f"G{i}b app.py 가 그 문장을 복사하지 않았다", fp in (APP or ""), False)
chk("G4 app.py 는 md 를 **파일로 읽는다**",
    "SATELLITE_MANDATE_FILE" in (APP or ""), True)


# ══ [I] §4② 층 1 — 지시 기록 · 주기 정합 ═════════════════════════════════
# 이 섹션이 막는 사고는 둘이다.
#   (1) 문서의 주기와 백테스트 축이 갈라지는 것. 2026-09-08 에 실제로 벌어져
#       있었다 — 근거가 된 워크포워드는 주간으로 돌았는데 §1 은 "월간"이라고
#       적혀 있었다. 조용히 갈라지면 6개월 뒤 ① 점검이 다른 축을 재게 된다.
#   (2) 층 2(판정)를 근거 없이 앞당겨 만드는 것. 기록이 없는 판정은 없느니만
#       못하다.
MRC = (_read("automation", "diag_momentum_rule_compare.py")
       or _read("diag_momentum_rule_compare.py"))
SEED = _read("automation", "seed_reminders.py") or _read("seed_reminders.py")

_m_freq = re.search(r'FIXED_FREQ\s*=\s*["\'](\w+)["\']', MRC or "")
_m_cyc = re.search(r"리밸런싱 주기\s*\|\s*\*\*(\S+?)\*\*", MD)
_FREQ_KO = {"weekly": "주간", "monthly": "월간", "daily": "일간"}

chk("I1 §1 리밸런싱 주기 행이 읽힌다", bool(_m_cyc), True)
chk("I2 §1 주기가 백테스트 축(FIXED_FREQ)과 같다 — 2026-09-08 재발 방지",
    _m_cyc.group(1) if _m_cyc else None,
    _FREQ_KO.get(_m_freq.group(1) if _m_freq else "", "?"))
chk("I3 §4③ 스냅샷은 **월간 그대로** (§1 정정이 번지지 않았다)",
    "매월 **마지막 거래일**" in MD, True)

chk("I4 §4② 가 시트명을 코드 상수와 같게 적는다",
    fx.SATELLITE_INSTRUCTION_SHEET in MD, True)
_m_cols = re.search(r"`(Date \| Book \|[^`]+)`", MD)
chk("I5 §4② 열 목록 = SATELLITE_INSTRUCTION_COLS (순서까지)",
    [c.strip() for c in _m_cols.group(1).split("|")] if _m_cols else None,
    list(fx.SATELLITE_INSTRUCTION_COLS))
chk("I6a Risk_On 열이 있다", "Risk_On" in fx.SATELLITE_INSTRUCTION_COLS, True)
chk("I6b §4② 의 §3 시장 필터 참조가 살아 있다",
    ("§3 시장 필터" in MD) and ("신규 매수 중단" in MD), True)

# 실동작 회귀 — 로직을 복사하지 않고 fmp_extras 의 함수를 실제로 부른다.
# 공유 dict 를 일부러 심는다: A반 1위이면서 B반 6위인 종목이다.
_shared = {"ticker": "XLK", "score": 9.9, "score_alt": 1.1, "rank": 1, "alt_rank": 6}
_iA = [_shared] + [{"ticker": f"A{i}", "score": 9.0 - i, "score_alt": None,
                    "rank": i + 1} for i in range(1, 10)]
_iB = ([{"ticker": f"B{i}", "score": None, "score_alt": 5.0 - i} for i in range(1, 6)]
       + [_shared]
       + [{"ticker": f"B{i}", "score": None, "score_alt": -i} for i in range(6, 10)])
_iout = {"rows": _iA, "alt": {"rows": _iB, "pool_n": 11}, "pool_n": 11,
         "market_filter": {"risk_on": False}, "skipped": [("X", "y"), ("Z", "w")]}
_ir = fx.satellite_instruction_rows(_iout, "2026-09-11")

chk("I7a 행은 2개 (A반·B반)", len(_ir), 2)
chk("I7b 행 폭 = 헤더 폭", [len(r) for r in _ir],
    [len(fx.SATELLITE_INSTRUCTION_COLS)] * 2)
chk("I7c 장부명이 SSOT 상수", [_ir[0][1], _ir[1][1]], list(fx.SATELLITE_BOOKS))
chk("I7d 룰이 SSOT 상수", [_ir[0][2], _ir[1][2]],
    [fx.SATELLITE_RANK_RULE, fx.SATELLITE_ALT_RULE])

# ⚠️ 열 이름 조회는 **죽지 않게** 한다. KeyError 로 죽으면 진단이 중간에서
#    멈춰 "어느 불변식이 깨졌는지"가 안 보이고 뒤 항목이 아예 안 돌아간다.
#    없는 열은 -1 로 두어 해당 검사만 조용히 실패하게 둔다.
_ci = {c: i for i, c in enumerate(fx.SATELLITE_INSTRUCTION_COLS)}


def _cix(name):
    return _ci.get(name, -1)
_bT = json.loads(_ir[1][_cix("Top_JSON")])
_bB = json.loads(_ir[1][_cix("Bench_JSON")])
chk("I8a B반 Top 은 1위부터 시작 (A반 rank 가 새지 않았다)",
    [e["rank"] for e in _bT], list(range(1, fx.SATELLITE_SLOTS + 1)))
chk("I8b B반 Bench 는 6위부터 이어진다",
    [e["rank"] for e in _bB],
    list(range(fx.SATELLITE_SLOTS + 1, fx.SATELLITE_SLOTS * 2 + 1)))
chk("I8c 공유 dict 는 B반에서 **B반 점수**로 기록된다 (score 9.9 가 아니라 1.1)",
    [(e["ticker"], e["score"]) for e in _bB if e["ticker"] == "XLK"],
    [("XLK", 1.1)])

chk("I9a market_filter 가 없으면 Risk_On 은 **공란** ('안 쟀다')",
    fx.satellite_instruction_rows(
        dict(_iout, market_filter=None), "d")[0][_cix("Risk_On")], "")
chk("I9b risk_on=False 는 False 로 남는다 ('재서 위험 구간')",
    _ir[0][_cix("Risk_On")], False)
chk("I10 Skipped_N 은 A/B 공용 루프의 제외 수",
    [_ir[0][_cix("Skipped_N")], _ir[1][_cix("Skipped_N")]], [2, 2])

# 숙주와 순서 — 소스 위치로 강제한다.
_p_dd = (HA or "").find("[STEP 5.6] 슬리브 낙폭 판정 중")
_p_in = (HA or "").find("satellite_instruction_rows")
_p_se = (HA or "").find("[STEP 6] 이메일 발송 중")
chk("I11a 세 지점이 모두 run_hidden_alpha 에 있다",
    min(_p_dd, _p_in, _p_se) > 0, True)
chk("I11b 지시 기록이 [STEP 5.6] 낙폭 **뒤**다 (③이 ②에 죽지 않는다)",
    _p_dd < _p_in, True)
chk("I11c 지시 기록이 [STEP 6] 발송 **앞**이다 (발송 실패한 주도 지시는 남는다)",
    _p_in < _p_se, True)
chk("I12 run_hidden_alpha 가 SSOT 함수를 부른다",
    _calls(HA, _alias_of(HA, "fmp_extras"), "satellite_instruction_rows"), True)
chk("I13 랭킹을 **다시 계산하지 않는다** (compute_satellite_top10 호출 1회)",
    _call_count(HA, _alias_of(HA, "fmp_extras"), "compute_satellite_top10"), 1)
chk("I14 Date 는 실행일이 아니라 데이터 기준일(data_date)이다",
    "data_date" in (HA or ""), True)
chk("I15 숙주 오배치 가드 — 월말 러너에는 지시 기록이 없다",
    ("satellite_instruction" in (SNAP or "").lower()
     or "SATELLITE_INSTRUCTION" in (SNAP or "")), False)
chk("I16 층 2 조기 구현 가드 — satellite_execution_gap 은 아직 없다",
    hasattr(fx, "satellite_execution_gap"), False)

# I18 — 주기 문구가 소비자에 복사돼 있다. §G 가 잡는 "문안 복사"의 얇은 판이다.
# app.py 는 md 를 파일로 읽어 렌더하지만, 그 **위에** 자기 캡션으로 주기를 또
# 적는다(md 를 못 읽었을 때의 폴백 요약에도 적는다). 그래서 §1 만 고치면 같은
# 화면에서 캡션은 "월간", 만다트는 "주간"이 된다. 값을 맞춰 잠근다.
_CYC = _m_cyc.group(1) if _m_cyc else "?"
_CYC_OTHER = {"주간": "월간", "월간": "주간"}.get(_CYC, "?")
# seed_reminders 도 소비자다. 11-03 점검 절차가 "월간"으로 남아 있으면, 그날
# 나는 있지도 않은 월 단위 지시를 찾게 된다 — 기록은 주 단위로 쌓여 있는데.
_CYC_SRC = (APP, HA, SEED)
chk("I18a 소비자 문구가 §1 주기를 그대로 쓴다 (app.py · 주간 메일 · 리마인더)",
    [f"{_CYC} 리밸런싱" in (src or "") for src in _CYC_SRC], [True] * 3)
chk("I18b 소비자에 반대 주기가 남아 있지 않다",
    [f"{_CYC_OTHER} 리밸런싱" in (src or "") for src in _CYC_SRC], [False] * 3)

# I19 — 리마인더가 **읽을 대상**을 지목하는가. 시트는 만들었는데 점검 절차가
# 그 시트를 모르면, 11-03 에 나는 Portfolios 만 들여다보고 넘어간다.
chk("I19a 11-03 점검이 Satellite_Instruction 을 지목한다",
    fx.SATELLITE_INSTRUCTION_SHEET in (SEED or ""), True)
chk("I19b 실행 쪽 입력(Trade_History) 배선 확인 항목이 있다",
    ("Trade_History" in (SEED or "")) and ("2026-09-21" in (SEED or "")), True)
chk("I19c 층 2 착수는 사전 확약 규율을 달고 있다",
    ("유예 기간" in (SEED or "")) and ("결과를 보고 바꾸지 않는다" in (SEED or "")),
    True)


_r2 = md_row6(MD, "② 실행 실패")
chk("I17a §6 ② 행 존재", bool(_r2), True)
chk("I17b §6 ② 가 ⚠️ (층 1 만 구축)", status_mark(_r2), "⚠️")
chk("I17c §6 ② 를 ✅ 로 올리지 않았다 (층 2 미구축)",
    status_mark(_r2) == "✅", False)


# ══ [J] §4① 깊은 창 참고 실행 — 사전 약정 ↔ 러너 상수 ═══════════════════
# 사전 약정의 숫자가 문서에만 있으면, 결과를 본 뒤 러너 상수만 슬쩍 바꿔 다시
# 돌려도 아무것도 울리지 않는다. 문서와 코드를 묶어 두면 한쪽만 바꾸는 순간
# 이 가드가 빨간불이 되고, 둘 다 바꾸면 §7 에 행이 남는다.
# 임포트하지 않는다 — 러너는 bt·rc 를 끌고 오고, 이 진단은 의존성 없이 돌아야 한다.
DEEP = (_read("automation", "diag_momentum_deep_ref.py")
        or _read("diag_momentum_deep_ref.py"))
RCMP = (_read("automation", "diag_momentum_rule_compare.py")
        or _read("diag_momentum_rule_compare.py"))
WF_DEEP = (_read(".github", "workflows", "diag_momentum_deep_ref.yml")
           or _read("diag_momentum_deep_ref.yml"))


def _module_assign(src, name):
    """모듈 최상위 `name = <식>` 의 식 노드. 없으면 None."""
    if not src:
        return None
    for n in ast.parse(src).body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name
                                             for t in n.targets):
            return n.value
    return None


def _lit(src, name):
    v = _module_assign(src, name)
    try:
        return ast.literal_eval(v) if v is not None else None
    except ValueError:
        return None


def _md_ticks(text, label):
    m = re.search(r"\*\*" + label + r"\*\*([^\n]*)", text or "")
    return tuple(re.findall(r"`([a-z0-9_]+)`", m.group(1))) if m else ()


def md_deep_dd(text):
    m = re.search(r"\*\*사건 임계\*\*[^\n]*?\*\*−(\d+(?:\.\d+)?)%\*\*", text or "")
    return -float(m.group(1)) / 100.0 if m else None


def md_deep_rebound(text):
    m = re.search(r"\*\*반등 구간\*\*[^\n]*?\*\*(\d+)봉\*\*", text or "")
    return int(m.group(1)) if m else None


_deep_rules_expr = _module_assign(DEEP, "RULES")
chk("J0 러너 파일 존재 (automation/diag_momentum_deep_ref.py)", DEEP is not None, True)
chk("J1 §4① 사건 임계 = EPISODE_DD", md_deep_dd(MD), _lit(DEEP, "EPISODE_DD"))
chk("J1b 사건 임계는 음수 (부호를 잃으면 상승이 사건이 된다)",
    (_lit(DEEP, "EPISODE_DD") or 0) < 0, True)
chk("J2 §4① 반등 구간 봉수 = REBOUND_BARS", md_deep_rebound(MD), _lit(DEEP, "REBOUND_BARS"))
chk("J3 §4① 대상 룰 = 판정 파일 VERDICT_RULES (동결 튜플)",
    _md_ticks(MD, "대상 룰"), _lit(RCMP, "VERDICT_RULES"))
chk("J3b 러너 RULES 는 rc.VERDICT_RULES 를 그대로 가리킨다 (사본 금지)",
    ast.unparse(_deep_rules_expr) if _deep_rules_expr is not None else None,
    "rc.VERDICT_RULES")
chk("J4 §4① 대상 필터 = FILTERS", _md_ticks(MD, "대상 필터"), _lit(DEEP, "FILTERS"))
chk("J5 기록 탭이 판정 탭과 다르다 (Momentum_Rule_Deep)",
    (_lit(DEEP, "_RESULT_WORKSHEET"), _lit(DEEP, "_RESULT_WORKSHEET") != _lit(RCMP, "_RESULT_WORKSHEET")),
    ("Momentum_Rule_Deep", True))
chk("J6 §4① 가 러너와 탭을 지목한다",
    ("diag_momentum_deep_ref.py" in (MD or ""), "Momentum_Rule_Deep" in (MD or "")), (True, True))
# J8 — 결과를 기록한 §2 각주가 "바꾸지 않는다"를 잃지 않았는가. 약점 목록은 읽기 좋은
#      근거라, 나중에 이 문장만 지우고 "그래서 필터를 바꿨다"로 이어 쓰기 쉽다.
_J8_HEAD = "**깊은 창 참고 실행에서 드러난 알려진 약점"
_J8_KEEP = "**이 사실로 규칙·필터·트리거를 바꾸지 않는다.**"


def md_deep_note_ok(text):
    t = text or ""
    i = t.find(_J8_HEAD)
    j = t.find("### 3.", i if i >= 0 else 0)
    return i >= 0 and j > i and _J8_KEEP in t[i:j]


chk("J8 §2 약점 각주가 '규칙·필터·트리거를 바꾸지 않는다'를 §3 앞에서 유지한다",
    md_deep_note_ok(MD), True)
chk("J7 수동 워크플로가 러너를 실행한다 (자체검증 → 본 실행)",
    (WF_DEEP is not None
     and "diag_momentum_deep_ref.py --selftest" in WF_DEEP
     and re.search(r"python automation/diag_momentum_deep_ref\.py\s*$", WF_DEEP, re.M) is not None),
    True)


# ══ [H] 양성 대조 — 알려진 불량 입력에서 실제로 실패하는가 ═══════════════
# 초록불이 옳은 이유로 켜졌는지 확인하지 않으면 초록불은 정보가 아니다.
def _would_fail(fn):
    try:
        return not fn()
    except Exception:
        return True


_bad_md = MD.replace(f"**{fx.SATELLITE_START_DATE}**", "**2020-01-01**", 1)
chk("H1 개시일이 어긋난 md 는 A2 를 통과하지 못한다",
    _would_fail(lambda: (re.search(
        r"A/B 실험 개시일\s*\|\s*\*\*(\d{4}-\d{2}-\d{2})\*\*", _bad_md).group(1)
        == fx.SATELLITE_START_DATE)), True)

_bad_md2 = MD.replace("고점 대비 **−30%**", "고점 대비 **−50%**", 1)
chk("H2 트리거가 어긋난 md 는 C1 을 통과하지 못한다",
    _would_fail(lambda: (-int(re.search(
        r"\*\*트리거\*\*\s*고점 대비\s*\*\*[−-](\d+)%\*\*", _bad_md2).group(1)) / 100.0
        == fx.SATELLITE_DRAWDOWN_TRIGGER)), True)

_bad_r3 = _r3.replace("| ⚠️", "| ✅", 1)
chk("H3 ③ 을 ✅ 로 올린 md 는 E1 을 통과하지 못한다",
    _would_fail(lambda: (status_mark(_bad_r3) == "✅") == _cash_written), True)
chk("H3b 상태 셀 본문의 '✅ 로 올린다' 안내는 상태로 읽히지 않는다",
    status_mark("| ③ 자본 손상 | ⚠️ 부분 — 현금을 관리하면 ✅ 로 올린다 |"), "⚠️")

_bad_app_pct = (APP or "").replace("−30% 시 절반", "−50% 시 절반", 1)
chk("H7 app.py 폴백 문구의 %가 어긋나면 F5 를 통과하지 못한다",
    _would_fail(lambda: {int(x) for x in re.findall(r"−(\d+)%", _bad_app_pct)}
                == {int(abs(fx.SATELLITE_DRAWDOWN_TRIGGER * 100))}), True)

_bad_app = (APP or "") + "\n# " + _FINGERPRINTS[0] + "\n"
chk("H4 문안을 복사한 app.py 는 G1b 를 통과하지 못한다",
    _would_fail(lambda: _FINGERPRINTS[0] not in _bad_app), True)

_bad_ha = (HA or "").replace("fx.satellite_drawdown", "_local_drawdown")
chk("H5 SSOT 호출을 뗀 run_hidden_alpha 는 F2 를 통과하지 못한다",
    _would_fail(lambda: _calls(_bad_ha, _alias_of(_bad_ha, "fmp_extras"),
                               "satellite_drawdown")), True)

_bad_order = (HA or "")
if _p_dd > 0 and _p_in > 0:      # 지시 블록을 낙폭 **앞**으로 옮긴 소스를 흉내낸다
    _bad_order = ((HA or "")[:_p_dd] + "fx.satellite_instruction_rows(  # moved\n"
                  + (HA or "")[_p_dd:])
chk("H8 지시 기록을 낙폭 앞으로 옮긴 소스는 I11b 를 통과하지 못한다",
    _would_fail(lambda: _bad_order.find("[STEP 5.6] 슬리브 낙폭 판정 중")
                < _bad_order.find("satellite_instruction_rows")), True)

_bad_cyc = MD.replace("| 리밸런싱 주기 | **주간**", "| 리밸런싱 주기 | **월간**", 1)
chk("H9 §1 을 '월간'으로 되돌린 md 는 I2 를 통과하지 못한다",
    _would_fail(lambda: re.search(r"리밸런싱 주기\s*\|\s*\*\*(\S+?)\*\*",
                                  _bad_cyc).group(1)
                == _FREQ_KO.get(_m_freq.group(1))), True)

_bad_cols = [c for c in fx.SATELLITE_INSTRUCTION_COLS if c != "Risk_On"]
chk("H10 Risk_On 을 뺀 열 목록은 I5·I6a 를 통과하지 못한다",
    _would_fail(lambda: ([c.strip() for c in _m_cols.group(1).split("|")]
                         == _bad_cols) and ("Risk_On" in _bad_cols)), True)

chk("H11 층 2 함수가 생기면 I16 이 이를 잡는다",
    _would_fail(lambda: not hasattr(fx, "satellite_drawdown")), True)

_bad_cap = (APP or "").replace(f"{_CYC} 리밸런싱", f"{_CYC_OTHER} 리밸런싱", 1)
chk("H12 앱 캡션 하나만 옛 주기로 되돌려도 I18b 가 잡는다",
    _would_fail(lambda: f"{_CYC_OTHER} 리밸런싱" not in _bad_cap), True)

# ⚠️ **전량** 치환한다. 1건만 지우면 다른 언급이 남아 가드가 살아남는다 —
#    처음 이렇게 썼다가 H13 이 빨갛게 떠서 알았다. 양성 대조가 없었으면
#    "시트를 지목한다"는 검사가 실제로는 아무것도 안 지키고 있었을 것이다.
_bad_seed = (SEED or "").replace(fx.SATELLITE_INSTRUCTION_SHEET, "Portfolios")
chk("H13 리마인더에서 시트 지목을 지우면 I19a 가 잡는다",
    _would_fail(lambda: fx.SATELLITE_INSTRUCTION_SHEET in _bad_seed), True)

chk("H6 배수만 바꾼 봉수는 B2 를 통과하지 못한다",
    _would_fail(lambda: int(re.search(
        r"소요 이력\s*(\d+)\s*봉",
        MD.replace(f"소요 이력 {fx.SATELLITE_BARS}봉", "소요 이력 127봉", 1)
    ).group(1)) == fx.SATELLITE_BARS), True)

# ── J 양성 대조 ─────────────────────────────────────────────────────────
chk("H14 md 사건 임계만 −15% 로 바꾸면 J1 이 잡는다",
    _would_fail(lambda: md_deep_dd(MD.replace("**−12%**", "**−15%**", 1))
                == _lit(DEEP, "EPISODE_DD")), True)
chk("H15 md 대상 룰에 위험조정 룰을 끼우면 J3 이 잡는다",
    # ⚠️ 치환 앵커는 `mom12_1` 까지 포함해야 한다. "`mom12_0` (" 만 쓰면 §2 의
    #    "A반 — `mom12_0` (12개월…" 에 먼저 걸려 §4① 은 그대로 남고, 이 대조가
    #    아무것도 안 지키면서 초록불이 된다(작성 중 실측).
    _would_fail(lambda: _md_ticks(MD.replace("`mom12_1` · `mom12_0` (",
                                             "`mom12_1` · `mom12_0` · `mom12_0_ra` (", 1),
                                  "대상 룰") == _lit(RCMP, "VERDICT_RULES")), True)
chk("H17 약점 각주에서 '바꾸지 않는다' 문장만 지우면 J8 이 잡는다",
    _would_fail(lambda: md_deep_note_ok(MD.replace(_J8_KEEP, "", 1))), True)
chk("H16 러너 반등 봉수만 63 으로 바꾸면 J2 가 잡는다",
    _would_fail(lambda: md_deep_rebound(MD) == _lit(
        (DEEP or "").replace("REBOUND_BARS = 126", "REBOUND_BARS = 63", 1), "REBOUND_BARS")), True)


print("=" * 74)
print(f"diag_satellite_mandate — 통과 {len(PASS)} / 실패 {len(FAIL)}  "
      f"(총 {len(PASS) + len(FAIL)})")
print("=" * 74)
if FAIL:
    print("\n❌ 실패 항목")
    for item in FAIL:
        print(f"   · {item}")
    print("\n문서와 코드가 갈라졌습니다. 어느 쪽이 맞는지 정한 뒤 양쪽을 맞추고,")
    print("md §7 개정 이력에 날짜·근거·변경 내용을 남기세요.")
    sys.exit(1)
print("\n✅ 문서와 코드가 일치합니다.")
sys.exit(0)
