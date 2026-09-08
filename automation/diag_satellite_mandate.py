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
  H   양성 대조 — 알려진 불량 입력에서 각 검사가 실제로 실패하는가

⚠️ 로직을 복사하지 않는다. fmp_extras 의 실제 함수를 부른다.

사용법:  python3 automation/diag_satellite_mandate.py
안전성:  네트워크·시트·FMP·메일 접근 없음. 시크릿 불필요. 부작용 없음.
"""
import ast
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

chk("H6 배수만 바꾼 봉수는 B2 를 통과하지 못한다",
    _would_fail(lambda: int(re.search(
        r"소요 이력\s*(\d+)\s*봉",
        MD.replace(f"소요 이력 {fx.SATELLITE_BARS}봉", "소요 이력 127봉", 1)
    ).group(1)) == fx.SATELLITE_BARS), True)


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
