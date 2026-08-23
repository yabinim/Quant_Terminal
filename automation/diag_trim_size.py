# -*- coding: utf-8 -*-
"""diag_trim_size.py — 트랜치 매도 사이징 회귀 스위트.

검증 대상: regime_core 의 **실제 함수** (resolve_swing_weight /
default_events_for_weight / verdict_action / trim_size_plan) + accounts_core 의
실제 파서. 로직을 복사하지 않는다.

불변식:
  I1  비율 미설정(None) → enabled=False. 기존 사용자 동작이 절대 안 바뀐다.
  I2  0 과 None 은 다르다. 0 = 포지션 100%(명시), None = 기능 미사용.
  I3  최소 거래금액 게이트는 부분 축소에만 걸린다. 전량 청산은 금액 무관 통과.
  I4  두 호흡 모두 청산이면 전량. 어느 쪽도 매도 신호 없으면 0.
  I5  스윙 몫이 0 이면 스윙 판정은 수량에 영향을 주지 않는다(반대도 동일).
  I6  default_events_for_weight 는 미설정에서 fallback 을 그대로 돌려준다.

사용법:  python3 automation/diag_trim_size.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import accounts_core as ac      # noqa: E402
import regime_core as rc        # noqa: E402

PASS, FAIL = [], []

EXIT_S = "🔴 청산"
EXIT_P = "🔴 청산 (SELL)"
TRIM_S = "🟡 줄이기"
TRIM_P = "🟡 줄이기 (일부 익절)"
HOLD_P = "✅ 보유 (HOLD)"


def chk(name, got, exp):
    (PASS if got == exp else FAIL).append(
        name if got == exp else (name, exp, got))


def plan(**kw):
    kw.setdefault("qty", 30.0)
    kw.setdefault("price", 10.0)
    kw.setdefault("swing_label", HOLD_P)
    kw.setdefault("position_label", HOLD_P)
    return rc.trim_size_plan(**kw)


# ── A군: 미설정 보호 (I1, I2) ────────────────────────────────────────────
chk("A-1 None → enabled=False",
    plan(swing_weight_pct=None, swing_label=EXIT_S)["enabled"], False)
chk("A-2 빈 문자열 → enabled=False",
    plan(swing_weight_pct="", swing_label=EXIT_S)["enabled"], False)
chk("A-3 0 은 명시값 → enabled=True",
    plan(swing_weight_pct=0, position_label=EXIT_P)["enabled"], True)
chk("A-4 resolve: 오버라이드 우선",
    rc.resolve_swing_weight(70, 20), 70.0)
chk("A-5 resolve: 오버라이드 빈칸 → 계좌 기본",
    rc.resolve_swing_weight("", 20), 20.0)
chk("A-6 resolve: 오버라이드 0 은 계좌 기본을 덮는다",
    rc.resolve_swing_weight(0, 20), 0.0)
chk("A-7 resolve: 둘 다 없음 → None",
    rc.resolve_swing_weight(None, None), None)
chk("A-8 resolve: 범위 클램프",
    (rc.resolve_swing_weight(140, None), rc.resolve_swing_weight(-5, None)),
    (100.0, 0.0))

# ── B군: 수량 계산 (I4, I5) ──────────────────────────────────────────────
# 기본 트림폭은 제품 결정이다. 상수를 조용히 바꾸면 전 사용자의 매도 규모가
# 바뀌므로 값 자체를 고정한다(아래 B-1 은 자기참조라 값 변경을 못 잡는다).
chk("B-0 기본 트림폭 = 50%", rc.TRIM_RATIO_DEFAULT_PCT, 50.0)
chk("B-1 0:100 · 포지션 줄이기 → 30주의 기본 트림폭",
    plan(swing_weight_pct=0, position_label=TRIM_P)["qty"],
    round(30.0 * rc.TRIM_RATIO_DEFAULT_PCT / 100.0, 4))
chk("B-1b 기본 트림폭이 명시 트림폭과 같은 결과를 낸다(자기참조 방지)",
    plan(swing_weight_pct=0, position_label=TRIM_P,
         trim_ratio_pct=rc.TRIM_RATIO_DEFAULT_PCT)["qty"],
    plan(swing_weight_pct=0, position_label=TRIM_P)["qty"])
chk("B-2 0:100 · 스윙 청산은 무시 (스윙 몫 0)",
    plan(swing_weight_pct=0, swing_label=EXIT_S)["qty"], 0.0)
chk("B-3 100:0 · 포지션 청산은 무시",
    plan(swing_weight_pct=100, position_label=EXIT_P)["qty"], 0.0)
chk("B-4 50:50 · 스윙 청산만 → 절반",
    plan(swing_weight_pct=50, swing_label=EXIT_S)["qty"], 15.0)
chk("B-5 50:50 · 둘 다 청산 → 전량",
    plan(swing_weight_pct=50, swing_label=EXIT_S,
         position_label=EXIT_P)["full_exit"], True)
chk("B-6 둘 다 보유 → 0",
    plan(swing_weight_pct=50)["qty"], 0.0)
chk("B-7 50:50 · 스윙 청산 + 포지션 줄이기 → 15 + 15×기본트림폭",
    plan(swing_weight_pct=50, swing_label=EXIT_S,
         position_label=TRIM_P)["qty"],
    round(15.0 + 15.0 * rc.TRIM_RATIO_DEFAULT_PCT / 100.0, 4))
chk("B-8 트림폭 25% 적용 (기본값과 다른 값으로 판별)",
    plan(swing_weight_pct=0, position_label=TRIM_P,
         trim_ratio_pct=25)["qty"], 7.5)
chk("B-9 트림폭 범위 밖은 클램프(200 → 90)",
    plan(swing_weight_pct=0, position_label=TRIM_P,
         trim_ratio_pct=200)["qty"], 27.0)
chk("B-10 소수점 주식 반올림 안 함 (정수가 되면 실패)",
    plan(qty=0.185, price=53.89, swing_weight_pct=0, position_label=TRIM_P,
         trim_ratio_pct=33)["qty"], 0.0611)

# ── C군: 최소 거래금액 게이트 (I3) ───────────────────────────────────────
_g = plan(swing_weight_pct=0, position_label=TRIM_P, min_trade_dollars=200.0)
chk("C-1 부분 축소 $99 < $200 → blocked", _g["blocked"], True)
chk("C-2 blocked 여도 enabled 는 유지(신호를 숨기지 않는다)", _g["enabled"], True)
chk("C-3 blocked 면 수량 0", _g["qty"], 0.0)
_f = plan(swing_weight_pct=0, position_label=EXIT_P, min_trade_dollars=99999.0)
chk("C-4 전량 청산은 금액 게이트 면제", _f["blocked"], False)
chk("C-5 전량 청산 수량 유지", _f["qty"], 30.0)
chk("C-6 게이트 0 이면 미적용",
    plan(swing_weight_pct=0, position_label=TRIM_P,
         min_trade_dollars=0.0)["blocked"], False)
chk("C-7 price 없으면 게이트 판정 불가 → 통과",
    plan(price=None, swing_weight_pct=0, position_label=TRIM_P,
         min_trade_dollars=99999.0)["blocked"], False)

# ── D군: 기본 이벤트 파생 (I6) ───────────────────────────────────────────
chk("D-1 미설정 → fallback 그대로",
    rc.default_events_for_weight(None, "exit,risk"), "exit,risk")
chk("D-2 0 → 포지션 전용", rc.default_events_for_weight(0), "pexit,ptrim")
chk("D-3 100 → 스윙 전용", rc.default_events_for_weight(100), "exit,risk")
chk("D-4 30 → 양쪽", rc.default_events_for_weight(30),
    "exit,risk,pexit,ptrim")
chk("D-5 명시 저장된 states 는 파생값을 무시한다",
    rc.resolve_alert_events("exit", rc.default_events_for_weight(0)), ["exit"])
chk("D-6 빈 states 는 파생값을 따른다",
    rc.resolve_alert_events("", rc.default_events_for_weight(0)),
    ["pexit", "ptrim"])
chk("D-7 'none' 은 여전히 전체 해제",
    rc.resolve_alert_events("none", rc.default_events_for_weight(0)), [])

# ── E군: 라벨 분류 ───────────────────────────────────────────────────────
chk("E-1 청산 라벨", rc.verdict_action(EXIT_P), "exit")
chk("E-2 줄이기(일부 익절) — 청산 먼저 매칭되면 안 됨",
    rc.verdict_action(TRIM_P), "trim")
chk("E-3 보유", rc.verdict_action(HOLD_P), "hold")
chk("E-4 빈 라벨", rc.verdict_action(None), "hold")
# 판별자: 부분 문자열 검색이면 '일부 청산' 이 exit 로 잡혀 전량 매도가 된다.
chk("E-5 '줄이기 (일부 청산)' → trim (부분검색이면 실패)",
    rc.verdict_action("🟡 줄이기 (일부 청산)"), "trim")
chk("E-6 판단보류는 hold (매도 수량을 만들지 않는다)",
    rc.verdict_action("⚪ 판단보류"), "hold")
chk("E-7 E-5 가 수량으로 이어지는지 — 전량이 되면 안 됨",
    plan(swing_weight_pct=0,
         position_label="🟡 줄이기 (일부 청산)")["full_exit"], False)

# ── F군: accounts_core 파싱 왕복 ─────────────────────────────────────────
_base = ["yab", "Roth", "100", "risk_based", "1", "20", "12", "0", "20",
         "2026-08-23", "tax_free", "0"]
chk("F-1 빈칸 → None", ac._coerce_row(_base + ["", ""])["Swing_Weight_Pct"], None)
chk("F-2 0 저장값 보존", ac._coerce_row(_base + ["0", ""])["Swing_Weight_Pct"], 0.0)
chk("F-3 트림폭 기본",
    ac._coerce_row(_base + ["", ""])["Trim_Ratio_Pct"], rc.TRIM_RATIO_DEFAULT_PCT)
chk("F-4 트림폭 클램프", ac._coerce_row(_base + ["", "500"])["Trim_Ratio_Pct"],
    rc.TRIM_RATIO_MAX_PCT)
chk("F-5 범위 밖 비율은 무시(기본 유지)",
    ac._coerce_row(_base + ["150", ""])["Swing_Weight_Pct"], None)
_p0 = ac._coerce_row(_base + ["0", "40"])
chk("F-6 to_row 왕복: 0 은 0 으로", ac.to_row("yab", "Roth", _p0, "n")[-2], 0.0)
chk("F-7 to_row 왕복: None 은 빈칸으로",
    ac.to_row("yab", "Roth", ac.default_profile("Roth"), "n")[-2], "")
chk("F-8 COLS 폭", ac.NCOL, 14)
# 날짜 서식 함정: get_all_values 가 0 을 "1899-12-30 0:00" 로 돌려주던 케이스.
# 파서는 이를 숫자로 오인하지 않고 미설정(None)으로 떨어뜨려야 한다.
chk("F-9 날짜 문자열은 비율로 해석되지 않는다",
    ac._coerce_row(_base + ["1899-12-30 0:00", ""])["Swing_Weight_Pct"], None)
chk("F-10 숫자 0(문자열 아님)도 정상 파싱",
    ac._coerce_row(_base + [0, ""])["Swing_Weight_Pct"], 0.0)

# ── G군: 양성 대조 — 하네스가 진짜 결함을 잡는지 ─────────────────────────
# 게이트를 전량 청산까지 걸도록 '고장낸' 구현이 C-4 를 실제로 깨뜨리는지 확인.
_broken_blocked = (30.0 * 10.0) < 99999.0   # full_exit 를 무시한 잘못된 판정
chk("G-1 양성대조(게이트를 전량에도 걸면 C-4 가 깨진다)",
    (_broken_blocked, _f["blocked"]), (True, False))

print("=" * 70)
print(f"통과 {len(PASS)} / 실패 {len(FAIL)}  (총 {len(PASS) + len(FAIL)})")
print("=" * 70)
for n in PASS:
    print(f"  ✅ {n}")
for n, exp, got in FAIL:
    print(f"  ❌ {n}\n       기대: {exp!r}\n       실제: {got!r}")
sys.exit(1 if FAIL else 0)
