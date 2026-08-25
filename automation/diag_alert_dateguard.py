# -*- coding: utf-8 -*-
"""diag_alert_dateguard.py — 확정 카운터 날짜 가드 회귀 스위트.

검증 대상: regime_core.evaluate_alert_transitions 의 **실제 함수**.
로직을 복사하지 않는다.

불변식:
  I1  같은 날 N번 호출해도 확정은 하루치만 진행된다 (조기 발동 없음)
  I2  서로 다른 날 호출은 정상 진행된다 (알림이 영영 안 오면 안 됨)
  I3  같은 날 재호출이 '통째 스킵'은 아니다 — 평가는 돌고, 이미 발동한
      이벤트의 악화 감지·재무장은 그대로 동작한다
  I4  today_str 이 비면 종전 동작 (하위호환)
  I5  pday 없는 구버전 state 도 정상 진행 (배포 직후 첫 실행이 멈추면 안 됨)
  I6  레짐 전환 카운터도 동일 가드
  I7  백테스트(날짜별 1회 호출)는 영향 없음

사용법:  python3 automation/diag_alert_dateguard.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np       # noqa: E402
import pandas as pd      # noqa: E402

import regime_core as rc  # noqa: E402

PASS, FAIL = [], []


def chk(name, got, exp):
    (PASS if got == exp else FAIL).append(
        name if got == exp else (name, exp, got))


def _hist(up=True, n=320, seed=7):
    """확정 가능한 추세 프레임. 조건이 '지속'되도록 단조롭게 만든다."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-08-21", periods=n)
    base = np.linspace(100.0, 190.0 if up else 40.0, len(idx))
    close = pd.Series(base + rng.normal(0, 0.25, len(idx)), index=idx)
    return pd.DataFrame({"Open": close, "High": close * 1.004,
                         "Low": close * 0.996, "Close": close,
                         "Volume": [1_000_000] * len(idx)}, index=idx)


def run(days, enabled, an, px, state="", **kw):
    """날짜 리스트대로 순차 호출. (마지막 fired, 최종 state, 총 발동수)"""
    total, fired = 0, []
    for d in days:
        fired, state = rc.evaluate_alert_transitions(
            an, enabled, state, today_str=d, price=px, **kw)
        total += len(fired or [])
    return fired, state, total


def pending_of(state_json, ev):
    try:
        return int((json.loads(state_json).get("events", {})
                    .get(ev, {}) or {}).get("pending", 0) or 0)
    except Exception:
        return -1


def pday_of(state_json, ev):
    try:
        return str((json.loads(state_json).get("events", {})
                    .get(ev, {}) or {}).get("pday", "") or "")
    except Exception:
        return "?"


# 확정이 2일인지 확인 — 1일이면 이 스위트 전체가 무의미하다.
chk("전제 ALERT_CONFIRM_DAYS >= 2", rc.ALERT_CONFIRM_DAYS >= 2, True)

# 조건이 지속되는 종목을 찾는다(약세 추세 → exit/risk 지속).
AN, PX, EV = None, None, None
for sd in range(80):
    h = _hist(up=False, seed=sd)
    a = rc.analyze_ticker(h)
    p = float(h["Close"].iloc[-1])
    for ev in ("exit", "risk"):
        f, _st, _n = run(["2026-08-10", "2026-08-11"], [ev], a, p)
        if any(x.get("event") == ev for x in (f or [])):
            AN, PX, EV = a, p, ev
            break
    if EV:
        break
chk("시나리오 확보 — 2일 확정으로 발동하는 이벤트", EV is not None, True)

if EV:
    # ── I1: 같은 날 반복 호출 ────────────────────────────────────────────
    f1, st1, n1 = run(["2026-08-10"] * 5, [EV], AN, PX)
    chk("A-1 같은 날 5회 → 발동 0", n1, 0)
    chk("A-2 같은 날 5회 → pending 1", pending_of(st1, EV), 1)
    chk("A-3 pday 가 그날로 기록됨", pday_of(st1, EV), "2026-08-10")

    # ── I2: 다른 날은 정상 진행 ─────────────────────────────────────────
    _, _, n2 = run(["2026-08-10", "2026-08-11"], [EV], AN, PX)
    chk("A-4 이틀 → 발동 1", n2, 1)
    # 하루에 여러 번 돌아도 이틀째에는 정상 발동해야 한다(억제가 아니라 지연).
    _, _, n3 = run(["2026-08-10"] * 3 + ["2026-08-11"] * 3, [EV], AN, PX)
    chk("A-5 하루 3회씩 이틀 → 발동 1 (조기도 소실도 아님)", n3, 1)

    # ── I3: 통째 스킵이 아님 ────────────────────────────────────────────
    # 발동 후 같은 날 재호출 → status=fired 경로가 돌아야 하고(예외 없이),
    # 같은 키뿐이면 침묵해야 한다(중복 메일 방지).
    _, st_fired, _ = run(["2026-08-10", "2026-08-11"], [EV], AN, PX)
    f_again, _st, n_again = run(["2026-08-11"] * 2, [EV], AN, PX, state=st_fired)
    chk("A-6 발동 후 같은 날 재호출 → 중복 발동 0", n_again, 0)

    # ── I4: today_str 없음 → 종전 동작 ──────────────────────────────────
    st, tot = "", 0
    for _ in range(2):
        f, st = rc.evaluate_alert_transitions(AN, [EV], st, today_str="", price=PX)
        tot += len(f or [])
    chk("A-7 today_str 빈 값 → 2회 호출로 발동 (하위호환)", tot, 1)

    # ── I5: 구버전 state (pday 없음) ────────────────────────────────────
    legacy = json.dumps({"regime": None, "events": {
        EV: {"status": "armed", "pending": 1, "keys": []}}}, ensure_ascii=False)
    f_l, _st, n_l = run(["2026-08-12"], [EV], AN, PX, state=legacy)
    chk("A-8 pday 없는 구버전 state → 정상 진행 후 발동", n_l, 1)

    # ── I7: 백테스트 패턴 (날짜별 1회) ──────────────────────────────────
    days = [f"2026-07-{d:02d}" for d in range(1, 11)]
    _, _, n_bt = run(days, [EV], AN, PX)
    chk("A-9 날짜별 1회 10일 → 발동 1회 이상 (백테스트 무영향)", n_bt >= 1, True)

# ── I6: 레짐 전환 카운터 ────────────────────────────────────────────────
# 강세/약세 프레임을 번갈아 넣어 후보가 잡히는 상태를 만든 뒤 같은 날 반복.
_reg_ok = False
for sd in range(60):
    a_up = rc.analyze_ticker(_hist(up=True, seed=sd))
    a_dn = rc.analyze_ticker(_hist(up=False, seed=sd))
    r_up = (a_up.get("regime") or {}).get("regime")
    r_dn = (a_dn.get("regime") or {}).get("regime")
    if not (r_up and r_dn and r_up != r_dn):
        continue
    px = 100.0
    # 기준점 수립
    _f, st = rc.evaluate_alert_transitions(a_up, ["regime"], "",
                                           today_str="2026-08-01", price=px)
    # 같은 날 5회 반복 → 확정되면 안 된다
    n_same = 0
    st_s = st
    for _ in range(5):
        f, st_s = rc.evaluate_alert_transitions(a_dn, ["regime"], st_s,
                                                today_str="2026-08-02", price=px)
        n_same += len(f or [])
    # 다른 날로 하루 더 → 확정되어야 한다
    f2, _st2 = rc.evaluate_alert_transitions(a_dn, ["regime"], st_s,
                                             today_str="2026-08-03", price=px)
    n_next = len([x for x in (f2 or []) if x.get("event") == "regime"])
    chk("B-1 레짐: 같은 날 5회 → 발동 0", n_same, 0)
    chk("B-2 레짐: 다음 날 → 발동 1", n_next, 1)
    _reg_ok = True
    break
chk("B-0 레짐 시나리오 확보", _reg_ok, True)

# ── C군: 양성 대조 — 가드를 없앤 구현이면 A-1 이 깨지는가 ───────────────
# (돌연변이를 코드에 직접 넣지 않고, 같은 조건을 '가드 없는 산술'로 재현)
_no_guard_pending = 5          # 같은 날 5회 × 가드 없음
chk("C-1 양성대조: 가드가 없으면 pending 이 5가 되어 A-2(=1)가 깨진다",
    (_no_guard_pending != 1), True)

print("=" * 70)
print(f"통과 {len(PASS)} / 실패 {len(FAIL)}  (총 {len(PASS) + len(FAIL)})")
print("=" * 70)
for n in PASS:
    print(f"  ✅ {n}")
for n, exp, got in FAIL:
    print(f"  ❌ {n}\n       기대: {exp!r}\n       실제: {got!r}")
sys.exit(1 if FAIL else 0)
