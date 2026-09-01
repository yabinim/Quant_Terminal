#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""교체 허들(Replacement Hurdle) 진단 — 읽기 전용 · FMP 콜 0회.

목적
────
슬롯 만석 시 "최약 보유와 교체해도 되는가"를 판정하는 regime_core 9-b) 블록
(`is_weak_status` · `rank_weakest` · `replacement_hurdle`)이 설계대로 동작하는지,
그리고 app.py 소비처가 그 SSOT 를 실제로 호출하는지를 확인한다.

이 스크립트는 아무것도 수정하지 않는다.
  - 시트 기록 없음 · 이메일 발송 없음 · FMP 호출 없음
  - 네트워크 없이 돈다 (시크릿 불필요)

출력 블록
─────────
  [A] is_weak_status 격자        — 라벨 → 정리대상 여부 전수
  [B] rank_weakest 규칙          — NaN·봉부족 제외 / 정렬 / 타이브레이크 / limit
  [C] replacement_hurdle 진리표  — 두 조건 AND · 경계값 · 산출불가
  [D] SSOT 대조                  — integrated_sell_verdict 가 VERDICT_* 를 돌려주는가
  [E] 소비처 lockstep (AST)      — app.py 가 SSOT 를 호출하는가
  [M] 뮤테이션 테스트            — 고의 결함이 [A][B][C] 에 잡히는가

실행:  python automation/diag_replace_hurdle.py   (repo root 에서)

⚠️ 사전 확정 기준 (결과를 보고 재협상하지 않는다)
   · [A][B][C][D][E] 전부 통과해야 한다 — 하나라도 실패하면 종료 코드 1.
   · [M] 은 준비한 뮤턴트를 **전부** 죽여야 한다. 하나라도 살아남으면 테스트가
     그 경로를 검사하지 않는다는 뜻이므로 역시 종료 코드 1.
"""
from __future__ import annotations

import ast
import os
import py_compile
import sys
import tempfile
import types

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import regime_core as rc  # noqa: E402

_FAILS: list[str] = []


def chk(name: str, got, want) -> bool:
    ok = (got == want)
    print(f"  {'✅' if ok else '❌'} {name}")
    if not ok:
        print(f"      기대={want!r}  실제={got!r}")
        _FAILS.append(name)
    return ok


# ══════════════════════════════════════════════════════════════════════════
# 테스트 본체 — 모듈을 인자로 받는다. 뮤테이션 테스트가 같은 배터리를
#   변형된 모듈에 그대로 돌리기 위해서다(테스트를 두 벌 쓰면 어긋난다).
# ══════════════════════════════════════════════════════════════════════════

def battery_A(m) -> list[str]:
    """is_weak_status — 라벨 → 정리대상 여부."""
    bad = []
    cases = [
        (m.VERDICT_SELL, True,  "🔴 청산 = 정리대상"),
        (m.VERDICT_TRIM, True,  "🟡 줄이기 = 정리대상"),
        (m.VERDICT_HOLD, False, "✅ 보유 = 정리대상 아님"),
        ("", False, "빈 문자열 = 불명 → False"),
        (None, False, "None = 불명 → False"),
        ("보유 중", False, "이모지 없는 임의 문자열 = 불명 → False"),
        # 판별 케이스: 문구가 바뀌어도 신호등 색으로 판정해야 한다.
        ("🟡 줄이기 (일부 청산)", True, "문구 변형(일부 청산)도 🟡 → 정리대상"),
        ("🔴 전량 정리", True, "문구 변형(전량 정리)도 🔴 → 정리대상"),
        ("🟢 보유", False, "🟢 = 정리대상 아님"),
    ]
    for label, want, desc in cases:
        if m.is_weak_status(label) != want:
            bad.append(f"A:{desc}")
    return bad


def battery_B(m) -> list[str]:
    """rank_weakest — 제외 규칙 · 정렬 · 타이브레이크."""
    bad = []
    rows = [
        {"ticker": "AAA", "score": 70.0, "status": m.VERDICT_HOLD, "full_metrics": True},
        {"ticker": "BBB", "score": 30.0, "status": m.VERDICT_SELL, "full_metrics": True},
        {"ticker": "CCC", "score": 50.0, "status": m.VERDICT_TRIM, "full_metrics": True},
        {"ticker": "DDD", "score": float("nan"), "status": m.VERDICT_SELL, "full_metrics": True},
        {"ticker": "EEE", "score": None, "status": m.VERDICT_SELL, "full_metrics": True},
        {"ticker": "FFF", "score": 5.0, "status": m.VERDICT_SELL, "full_metrics": False},
    ]
    got = m.rank_weakest(rows)
    if [r["ticker"] for r in got] != ["BBB", "CCC", "AAA"]:
        bad.append("B:강도 오름차순 정렬")
    # 판별 케이스 — 제외가 실제로 일어나야 한다. FFF(강도 5)가 살아 있으면
    # 정렬 결과의 선두가 바뀌므로 위 테스트가 이미 잡지만, 명시적으로 한 번 더.
    if any(r["ticker"] in ("DDD", "EEE") for r in got):
        bad.append("B:NaN/None 강도 제외")
    if any(r["ticker"] == "FFF" for r in got):
        bad.append("B:봉 부족(full_metrics=False) 제외")
    # 봉 부족을 허용하면 FFF 가 최약이 되어야 한다 — 플래그가 살아 있는지 확인
    got2 = m.rank_weakest(rows, require_full_metrics=False)
    if not got2 or got2[0]["ticker"] != "FFF":
        bad.append("B:require_full_metrics=False 시 FFF 가 최약")
    # weak 파생 필드
    if not all(r.get("weak") is m.is_weak_status(r.get("status")) for r in got):
        bad.append("B:weak 파생 필드가 is_weak_status 와 일치")
    # 동점 타이브레이크 — 티커 오름차순 (같은 입력 → 같은 출력)
    tie = [
        {"ticker": "ZZZ", "score": 40.0, "status": m.VERDICT_HOLD, "full_metrics": True},
        {"ticker": "MMM", "score": 40.0, "status": m.VERDICT_HOLD, "full_metrics": True},
    ]
    if [r["ticker"] for r in m.rank_weakest(tie)] != ["MMM", "ZZZ"]:
        bad.append("B:동점 시 티커 오름차순")
    # limit
    if len(m.rank_weakest(rows, limit=2)) != 2:
        bad.append("B:limit 적용")
    # 입력 비파괴
    if not (pd.isna(rows[3]["score"]) and rows[0]["score"] == 70.0):
        bad.append("B:입력 dict 를 변형하지 않음")
    # 방어 입력
    if m.rank_weakest(None) != [] or m.rank_weakest([None, 3, "x"]) != []:
        bad.append("B:잘못된 입력은 빈 리스트")
    return bad


def battery_C(m) -> list[str]:
    """replacement_hurdle — 두 조건 AND · 경계값 · 산출불가."""
    bad = []
    M = m.REPLACE_SCORE_MARGIN

    # 진리표 4조합
    r = m.replacement_hurdle(60.0, 30.0, m.VERDICT_SELL)          # 강도 O · 상태 O
    if not (r["passed"] and r["score_ok"] and r["status_ok"]):
        bad.append("C:강도O·상태O → 성립")
    r = m.replacement_hurdle(60.0, 30.0, m.VERDICT_HOLD)          # 강도 O · 상태 X
    if r["passed"] or not r["score_ok"] or r["status_ok"]:
        bad.append("C:강도O·상태X → 불성립")
    r = m.replacement_hurdle(35.0, 30.0, m.VERDICT_SELL)          # 강도 X · 상태 O
    if r["passed"] or r["score_ok"] or not r["status_ok"]:
        bad.append("C:강도X·상태O → 불성립")
    r = m.replacement_hurdle(35.0, 30.0, m.VERDICT_HOLD)          # 강도 X · 상태 X
    if r["passed"] or r["score_ok"] or r["status_ok"]:
        bad.append("C:강도X·상태X → 불성립")

    # 경계값 — 마진과 '정확히 같으면' 통과해야 한다(>= 이지 > 가 아니다)
    r = m.replacement_hurdle(30.0 + M, 30.0, m.VERDICT_SELL)
    if not r["score_ok"]:
        bad.append("C:마진 정확히 일치 → 강도 조건 통과(>=)")
    r = m.replacement_hurdle(30.0 + M - 0.1, 30.0, m.VERDICT_SELL)
    if r["score_ok"]:
        bad.append("C:마진 0.1 부족 → 강도 조건 실패")

    # 마진 자체가 0 이 아니어야 한다 — 규칙이 무력화되면 이 테스트가 잡는다
    r = m.replacement_hurdle(30.5, 30.0, m.VERDICT_SELL)
    if r["score_ok"]:
        bad.append("C:마진 +0.5 로는 교체 불가(허들이 살아 있는가)")

    # 산출 불가 — 모르면 교체하지 않는다
    for cand, weak in ((float("nan"), 30.0), (60.0, float("nan")),
                       (None, 30.0), ("-", 30.0)):
        r = m.replacement_hurdle(cand, weak, m.VERDICT_SELL)
        if r["passed"]:
            bad.append("C:강도 산출불가 → 불성립")
            break

    # margin_actual 부호
    r = m.replacement_hurdle(60.0, 30.0, m.VERDICT_SELL)
    if abs(float(r["margin_actual"]) - 30.0) > 1e-9:
        bad.append("C:margin_actual = 후보 - 최약")

    # reason 은 언제나 비어 있지 않다(화면에 그대로 나간다)
    if not str(m.replacement_hurdle(1.0, 2.0, m.VERDICT_HOLD)["reason"]).strip():
        bad.append("C:reason 비어 있지 않음")
    return bad


def battery_F(m) -> list[str]:
    """position_size 의 block_reason 문구 — 슬롯 만석이 교체 검토의 입구다.

    이 경로는 지금까지 어떤 진단도 덮지 않았다. 슬롯 만석 문구가 조용히
    사라지면 '교체를 검토하라'는 신호 자체가 없어지므로 여기서 함께 지킨다.
    """
    bad = []

    # (1) 부족 사유 문구 — 라벨을 그대로 이어 붙이던 중복 결함의 회귀 가드
    if m.shortage_message("reserve") != "투자 여유 없음":
        bad.append("F:reserve 부족 문구('투자 여유 여유 없음' 중복 방지)")
    if "여유 여유" in " ".join(m.shortage_message(k) for k in m.BINDING_LABELS):
        bad.append("F:어떤 binding 에서도 '여유'가 두 번 나오지 않음")
    # 미등록 코드는 폴백해야 한다(빈 문자열이 나가면 사용자가 이유를 못 본다)
    if not str(m.shortage_message("nosuchcode")).strip():
        bad.append("F:미등록 binding 폴백 문구 비어 있지 않음")

    # (2) 슬롯 만석 → blocked + 교체 안내. 금액은 깎지 않는다.
    full = m.position_size(equity=10000.0, risk_pct=1.0, entry=100.0, stop=95.0,
                           slots_used=12, max_positions=12)
    if not full.get("slots_full"):
        bad.append("F:슬롯 만석 플래그")
    if not full.get("blocked"):
        bad.append("F:슬롯 만석이면 blocked")
    if "교체" not in str(full.get("block_reason", "")):
        bad.append("F:슬롯 만석 사유에 교체 안내 포함")
    if not (float(full.get("dollars", 0)) > 0):
        bad.append("F:슬롯 만석이어도 금액은 그대로 산출(깎지 않음)")

    # 판별 케이스 — 여유가 있으면 막히지 않아야 한다(위 검사가 상시 참이 아님)
    room = m.position_size(equity=10000.0, risk_pct=1.0, entry=100.0, stop=95.0,
                           slots_used=3, max_positions=12)
    if room.get("slots_full") or room.get("blocked"):
        bad.append("F:슬롯 여유 시 통과")

    # (3) 현금 0 → 부족 사유가 '가용 현금 없음'
    broke = m.position_size(equity=10000.0, risk_pct=1.0, entry=100.0, stop=95.0,
                            cash=0.0)
    if broke.get("binding") != "cash":
        bad.append("F:현금 0 이면 binding='cash'")
    if broke.get("block_reason") != "가용 현금 없음":
        bad.append("F:현금 0 부족 문구")

    # (4) reserve 가 결정 제약일 때의 **실제 경로** — 보고된 결함 그 자체.
    #     예비현금 50% 인데 이미 60% 를 투자해 여유가 0 인 상태.
    #     문구 단위 테스트(1)만으로는 position_size 가 그 함수를 부르는지 모른다.
    resv = m.position_size(equity=10000.0, risk_pct=1.0, entry=100.0, stop=95.0,
                           reserve_pct=50.0, invested_value=6000.0)
    if resv.get("binding") != "reserve":
        bad.append("F:예비현금 소진 시 binding='reserve'")
    if resv.get("block_reason") != "투자 여유 없음":
        bad.append("F:reserve 차단 문구가 '투자 여유 없음' (중복 '여유' 회귀)")
    return bad


def run_batteries(m) -> list[str]:
    return battery_A(m) + battery_B(m) + battery_C(m) + battery_F(m)


# ══════════════════════════════════════════════════════════════════════════
# [D] SSOT 대조 — integrated_sell_verdict 의 실제 반환이 VERDICT_* 인가
# ══════════════════════════════════════════════════════════════════════════

def block_D() -> None:
    print("\n[D] SSOT 대조 — integrated_sell_verdict → VERDICT_* 상수")
    base = dict(above_ma200=True, one_month_return=0.0, rsi=50.0,
                macd_signal="NONE", pct_from_52w_high=-10.0,
                drawdown_from_high_pct=0.0)

    lab_hold, _ = rc.integrated_sell_verdict(**base)
    chk("D-1 무신호 → VERDICT_HOLD", lab_hold, rc.VERDICT_HOLD)

    # 점수 2점 = MACD 데드크로스 단독 → 줄이기
    lab_trim, _ = rc.integrated_sell_verdict(**{**base, "macd_signal": "DEAD_CROSS"})
    chk("D-2 MACD 데드크로스 → VERDICT_TRIM", lab_trim, rc.VERDICT_TRIM)

    # 200일선 대폭 이탈(4점) → 청산
    lab_sell, _ = rc.integrated_sell_verdict(
        **{**base, "above_ma200": False, "gap_ma200_pct": -20.0})
    chk("D-3 200일선 대폭 이탈 → VERDICT_SELL", lab_sell, rc.VERDICT_SELL)

    # 세 라벨이 서로 달라야 한다(상수를 잘못 묶으면 여기서 걸린다)
    chk("D-4 세 라벨이 서로 구별됨",
        len({rc.VERDICT_SELL, rc.VERDICT_TRIM, rc.VERDICT_HOLD}), 3)
    chk("D-5 VERDICT_WEAK_LABELS = (SELL, TRIM)",
        tuple(rc.VERDICT_WEAK_LABELS), (rc.VERDICT_SELL, rc.VERDICT_TRIM))

    # 실제 판정 라벨은 전부 is_weak_status 가 해석할 수 있어야 한다
    chk("D-6 판정 라벨 3종이 모두 해석됨",
        [rc.is_weak_status(x) for x in (lab_sell, lab_trim, lab_hold)],
        [True, True, False])


# ══════════════════════════════════════════════════════════════════════════
# [E] 소비처 lockstep (AST) — app.py 가 SSOT 를 호출하는가
#     문자열 검색이 아니라 AST 로 본다: 주석·독스트링 오탐을 피하고,
#     별칭 import 도 실제 호출 노드로 확인한다.
# ══════════════════════════════════════════════════════════════════════════

def block_E() -> None:
    print("\n[E] 소비처 lockstep — app.py (AST)")
    path = os.path.join(_ROOT, "app.py")
    if not os.path.exists(path):
        print("  ⚠️ app.py 를 찾을 수 없습니다 — 이 블록을 건너뜁니다.")
        _FAILS.append("E:app.py 없음")
        return
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)

    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name) and base.id == "rc":
                calls.add(node.func.attr)

    chk("E-1 rc.rank_weakest 호출", "rank_weakest" in calls, True)
    chk("E-2 rc.replacement_hurdle 호출", "replacement_hurdle" in calls, True)
    chk("E-3 rc.classify_regime 호출(강도 산출)", "classify_regime" in calls, True)

    # 강도 컬럼이 스키마와 행 양쪽에 있어야 한다. 한쪽만 있으면 표가 비거나
    # KeyError 가 난다 — 실제로 겪은 실패 모드(컬럼 목록만 고치고 rows 를 빠뜨림).
    consts = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    chk("E-4 '강도(Score)' 리터럴이 2회 이상(스키마+행)",
        sum(1 for c in consts if c == "강도(Score)") >= 2, True)

    # 내부 플래그가 표시용에서 제거되는가
    chk("E-5 '_score_full' 내부 플래그 사용", "_score_full" in src, True)


# ══════════════════════════════════════════════════════════════════════════
# [M] 뮤테이션 테스트
#     고의로 결함을 심은 regime_core 를 만들어 [A][B][C] 배터리를 돌린다.
#     ⚠️ 뮤턴트마다 py_compile 로 먼저 검증한다 — 문법이 깨진 뮤턴트가 죽는 것은
#        "테스트가 잡았다"가 아니라 "애초에 실행이 안 됐다"이므로 구별해야 한다.
# ══════════════════════════════════════════════════════════════════════════

_MUTANTS = [
    ("M1 마진 무력화 (15.0 → 0.0)",
     "REPLACE_SCORE_MARGIN = 15.0", "REPLACE_SCORE_MARGIN = 0.0"),
    ("M2 AND → OR (한 조건만으로 교체)",
     'out["passed"] = bool(out["score_ok"] and out["status_ok"])',
     'out["passed"] = bool(out["score_ok"] or out["status_ok"])'),
    ("M3 불명 라벨을 정리대상으로 오판",
     "    if head in _VERDICT_WEAK_MARKS:\n        return True\n    return False",
     "    if head in _VERDICT_WEAK_MARKS:\n        return True\n    return True"),
    ("M4 HOLD 를 정리대상으로 오판",
     "    if s == VERDICT_HOLD:\n        return False",
     "    if s == VERDICT_HOLD:\n        return True"),
    ("M5 봉 부족 제외 규칙 삭제",
     "        if require_full_metrics and not bool(r.get(\"full_metrics\", False)):",
     "        if False and not bool(r.get(\"full_metrics\", False)):"),
    ("M6 정렬 방향 반전 (최약 → 최강)",
     'out.sort(key=lambda x: (x["score"], x["ticker"]))',
     'out.sort(key=lambda x: (x["score"], x["ticker"]), reverse=True)'),
    ("M7 경계 부등호 >= → > (마진 정확히 일치 시 탈락)",
     'out["score_ok"] = bool(gap >= m)', 'out["score_ok"] = bool(gap > m)'),
    ("M8 NaN 강도 제외 규칙 삭제",
     "        if not np.isfinite(sc):\n            continue",
     "        if False:\n            continue"),
    ("M9 부족 문구 중복 재발 (라벨 이어붙이기)",
     "        reasons.append(shortage_message(binding))",
     '        reasons.append(f"{BINDING_LABELS.get(binding, binding)} 여유 없음")'),
    ("M10 슬롯 만석 안내에서 '교체' 제거",
     'reasons.append(f"슬롯 만석 ({slots_used}/{max_positions}) — 최약 보유와 교체 검토")',
     'reasons.append(f"슬롯 만석 ({slots_used}/{max_positions})")'),
    ("M11 슬롯 만석 판정 무력화",
     "            if _mx > 0 and _us >= _mx:\n                slots_full = True",
     "            if False:\n                slots_full = True"),
]


def _load_mutant(src: str, name: str):
    """변형 소스를 임시 모듈로 적재. 문법 오류면 (None, 사유)."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        tmp = fh.name
    try:
        py_compile.compile(tmp, doraise=True)
    except Exception as e:
        os.unlink(tmp)
        return None, f"문법 오류({e.__class__.__name__})"
    mod = types.ModuleType(f"_mut_{name}")
    mod.__file__ = tmp
    try:
        exec(compile(src, tmp, "exec"), mod.__dict__)
    except Exception as e:
        os.unlink(tmp)
        return None, f"임포트 실패({e.__class__.__name__}: {e})"
    os.unlink(tmp)
    return mod, ""


def block_M() -> bool:
    print("\n[M] 뮤테이션 테스트 — 고의 결함이 잡히는가")
    path = os.path.join(_ROOT, "regime_core.py")
    base_src = open(path, encoding="utf-8").read()

    # 먼저 원본이 통과하는지 확인 — 원본이 실패하면 뮤테이션 결과가 무의미하다.
    live_base = run_batteries(rc)
    if live_base:
        print("  ❌ 원본이 이미 실패 — 뮤테이션 테스트를 신뢰할 수 없습니다.")
        for b in live_base:
            print(f"      · {b}")
        _FAILS.append("M:원본 배터리 실패")
        return False

    all_killed = True
    for name, old, new in _MUTANTS:
        n = base_src.count(old)
        if n != 1:
            print(f"  ❌ {name} — 앵커가 {n}회 발견됨(1회여야 함). 코드가 바뀌었습니다.")
            _FAILS.append(f"M:{name} 앵커 불일치")
            all_killed = False
            continue
        mod, why = _load_mutant(base_src.replace(old, new), name.split()[0])
        if mod is None:
            print(f"  ⚠️ {name} — 유효하지 않은 뮤턴트({why}). 테스트 공백이 아닙니다.")
            continue
        caught = run_batteries(mod)
        if caught:
            print(f"  ✅ {name} — 사망 ({len(caught)}건 검출: {caught[0]})")
        else:
            print(f"  ❌ {name} — **생존**. 이 결함을 잡는 테스트가 없습니다.")
            _FAILS.append(f"M:{name} 생존")
            all_killed = False
    return all_killed


# ══════════════════════════════════════════════════════════════════════════

_REQUIRED = ("VERDICT_SELL", "VERDICT_TRIM", "VERDICT_HOLD", "VERDICT_WEAK_LABELS",
             "REPLACE_SCORE_MARGIN", "is_weak_status", "rank_weakest",
             "replacement_hurdle", "shortage_message")


def preflight() -> bool:
    """regime_core 에 9-b) 블록이 있는가. 없으면 **lockstep 위반**이다 —
    app.py 만 올리고 regime_core.py 를 빠뜨린 배포에서 정확히 이 상태가 된다.
    트레이스백 대신 원인을 이름으로 말해준다."""
    missing = [a for a in _REQUIRED if not hasattr(rc, a)]
    if not missing:
        return True
    print("\n❌ regime_core.py 에 교체 허들 블록(9-b)이 없습니다 — lockstep 위반")
    print(f"   누락 심볼: {', '.join(missing)}")
    print("   app.py 와 regime_core.py 는 **함께** 배포해야 합니다.")
    return False


def main() -> int:
    print("=" * 78)
    print("교체 허들 진단 — 읽기 전용 · FMP 콜 0회")
    print("=" * 78)

    if not preflight():
        print("=" * 78)
        return 1

    print("\n[A] is_weak_status 격자")
    aa = battery_A(rc)
    for b in aa:
        print(f"  ❌ {b}")
        _FAILS.append(b)
    if not aa:
        print("  ✅ 9개 케이스 전부 통과 (문구 변형 2건 포함)")

    print("\n[B] rank_weakest 규칙")
    bb = battery_B(rc)
    for b in bb:
        print(f"  ❌ {b}")
        _FAILS.append(b)
    if not bb:
        print("  ✅ 제외(NaN·봉부족) · 정렬 · 타이브레이크 · limit · 비파괴 전부 통과")

    print("\n[C] replacement_hurdle 진리표")
    cc = battery_C(rc)
    for b in cc:
        print(f"  ❌ {b}")
        _FAILS.append(b)
    if not cc:
        print(f"  ✅ 진리표 4조합 · 경계값(마진 {rc.REPLACE_SCORE_MARGIN:.0f}) · 산출불가 전부 통과")

    print("\n[F] position_size 차단 문구 · 슬롯 만석")
    ff = battery_F(rc)
    for b in ff:
        print(f"  ❌ {b}")
        _FAILS.append(b)
    if not ff:
        print(f"  ✅ 부족 문구(reserve='{rc.shortage_message('reserve')}') · "
              "슬롯 만석 blocked · 금액 비절삭 · 판별 케이스 전부 통과")

    block_D()
    block_E()
    ok_m = block_M()

    print("\n" + "=" * 78)
    if _FAILS:
        print(f"❌ 실패 {len(_FAILS)}건")
        for f in _FAILS:
            print(f"   · {f}")
        print("=" * 78)
        return 1
    print(f"✅ 전부 통과 — 뮤턴트 {len(_MUTANTS)}종 모두 사망" if ok_m else "✅ 통과")
    print("진단 종료 — 아무것도 수정하지 않았습니다.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
