"""diag_market_calendar.py — calendar_core 회귀 검증 + 뮤테이션 테스트.

무엇을 지키려는 검사인가
────────────────────────
휴장일 판정이 틀리면 **조용히** 틀린다. 개장일을 휴장으로 오판하면 그날 알림이
통째로 안 나가고, 반대로 오판하면 알림 상태머신의 확정 카운터가 헛돌아 진행된다.
둘 다 로그에 에러가 남지 않는다. 그래서 배포 전에 기계로 확인해야 한다.

네트워크·시트 접근이 전혀 없다. 언제 어디서 돌려도 같은 결과가 나온다.

    python automation/diag_market_calendar.py

검사 항목
─────────
  A. 골든 재현    — 교체 전 5개 파일에 하드코딩돼 있던 2025·2026 휴장일 20개를
                    규칙 계산이 **정확히** 재현하는가. 이게 깨지면 교체가 회귀다.
  B. 대체 휴일    — 토요일→전날 금요일, 일요일→다음 월요일, 신정 토요일 예외
  C. 부활절       — 굿프라이데이 산출의 기준. 외부 검증값과 대조
  D. 판정 계약    — 주말/휴장/개장/임시휴장 보강/폴백
  E. 시트 파싱    — 헤더 결손·깨진 행에도 예외 없이 빈 결과
  F. FMP 대조     — 임시 휴장(국장일) 검출 경로가 실제로 동작하는가
  H. 반일장       — 조기 마감(13:00) 규칙 골든 7년 + 마감시각 계약
  I. 소비자 배선  — automation 파일이 calendar_core 를 **실제로** 부르는가.
                    하드코딩 날짜 집합 재발 금지(AST 래칫) + 역검증 픽스처
  J. 세션 구간    — session_phase / narrative_session_label 경계 계약.
                    반일장·휴장이 구간 판정에 반영되는가
  K. 라벨 배선    — app.py · run_narrative.py 가 밴드를 **다시 적지 않는가**,
                    DRG market_session 이 마감 시각을 캘린더에서 받는가,
                    반일장 불일치가 Reminders 로 배달되는가 + 역검증 픽스처
  G. 뮤테이션     — 규칙 엔진에 의도적 버그를 심어 검사가 **잡아내는지** 확인

G 와 I·K 가 핵심이다. 통과만 하는 테스트는 통과만 하는 코드를 보증하지 않고,
모듈만 검사하는 스위트는 소비자가 그 모듈을 안 쓰는 것을 보증하지 않는다
(I 군은 실제로 그 사고 — run_earnings_watch 미이관 — 뒤에 추가됐다).

K 군이 J 군과 짝이다. J 만 있으면 calendar_core 는 완벽한데 app.py 가 자기 안의
낡은 밴드 표를 계속 쓰는 상태를 통과시킨다 — 그게 정확히 이 변경 **직전**의
상태였다.
"""
import os
import sys
from datetime import date, datetime, timedelta   # datetime: J절 세션 구간

# automation/ 에서 실행돼도 repo root 의 calendar_core 를 찾도록 한다
# (diag_reminders.py / diag_watchlist_metrics.py 와 동일한 관용구).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import calendar_core as cc  # noqa: E402

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  — " + detail) if detail and not cond else ""))
    return cond


# ══════════════════════════════════════════════════════════════════════════
# A. 골든 재현 — 교체 전 하드코딩 값과 완전 일치
# ══════════════════════════════════════════════════════════════════════════
GOLDEN = {
    2025: {"2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
           "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25"},
    2026: {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
           "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"},
}


def test_golden():
    print("\n── A. 골든 재현 (교체 전 하드코딩 20개)")
    for y, gold in sorted(GOLDEN.items()):
        got = set(cc.nyse_regular_holidays(y))
        ok = got == gold
        detail = ""
        if not ok:
            detail = "누락=" + str(sorted(gold - got)) + " 초과=" + str(sorted(got - gold))
        check(str(y) + " 휴장일 10개 완전 일치", ok, detail)


# ══════════════════════════════════════════════════════════════════════════
# B. 대체 휴일 규칙
# ══════════════════════════════════════════════════════════════════════════
def test_observed():
    print("\n── B. 대체 휴일 규칙")
    # 2026-07-04 는 토요일 → 7/3(금) 관측
    check("토요일 휴일 → 전날 금요일",
          "2026-07-03" in cc.nyse_regular_holidays(2026)
          and "2026-07-04" not in cc.nyse_regular_holidays(2026))
    # 2027-07-04 는 일요일 → 7/5(월) 관측
    h27 = cc.nyse_regular_holidays(2027)
    check("일요일 휴일 → 다음 월요일",
          "2027-07-05" in h27 and "2027-07-04" not in h27)
    # 2027-06-19 는 토요일 → 6/18(금)
    check("준틴스 토요일 → 6/18 금요일", "2027-06-18" in h27)
    # 2027-12-25 는 토요일 → 12/24(금)
    check("크리스마스 토요일 → 12/24 금요일", "2027-12-24" in h27)
    # 신정 예외: 2028-01-01 은 토요일 → 2027-12-31 을 쉬지 않는다
    check("신정 토요일 예외 — 전년 12/31 안 쉼",
          "2027-12-31" not in cc.nyse_regular_holidays(2027))
    # 2028-01-01(토) 은 2028년 목록에도 없어야 한다
    check("신정 토요일 — 당해 목록에도 없음",
          "2028-01-01" not in cc.nyse_regular_holidays(2028))
    # 2023-01-01 은 일요일 → 2023-01-02(월) 관측
    check("신정 일요일 → 1/2 월요일",
          "2023-01-02" in cc.nyse_regular_holidays(2023))


# ══════════════════════════════════════════════════════════════════════════
# C. 부활절 (굿프라이데이 기준)
# ══════════════════════════════════════════════════════════════════════════
EASTER = {2024: date(2024, 3, 31), 2025: date(2025, 4, 20), 2026: date(2026, 4, 5),
          2027: date(2027, 3, 28), 2028: date(2028, 4, 16), 2030: date(2030, 4, 21)}


def test_easter():
    print("\n── C. 부활절 산출")
    for y, exp in sorted(EASTER.items()):
        got = cc.easter_sunday(y)
        check(str(y) + " 부활절 " + exp.isoformat(), got == exp, "실제=" + got.isoformat())


# ══════════════════════════════════════════════════════════════════════════
# D. 판정 계약
# ══════════════════════════════════════════════════════════════════════════
def test_contract():
    print("\n── D. 판정 계약")
    check("토요일 → 휴장", cc.is_market_open("2026-08-22") is False)
    check("일요일 → 휴장", cc.is_market_open("2026-08-23") is False)
    check("평일 → 개장", cc.is_market_open("2026-08-19") is True)
    check("정규 휴장일 → 휴장", cc.is_market_open("2027-01-01") is False)
    check("2027 MLK → 휴장 (기존 하드코딩은 놓쳤던 날)",
          cc.is_market_open("2027-01-18") is False)
    check("2030 추수감사절 → 휴장", cc.is_market_open("2030-11-28") is False)
    # 임시 휴장 보강
    check("extra_closed 보강 — 국장일 차단",
          cc.is_market_open("2025-01-09", extra_closed={"2025-01-09"}) is False)
    check("extra_closed 없으면 개장 (규칙은 못 잡음)",
          cc.is_market_open("2025-01-09") is True)
    # 폴백 방향 — 판정 불가는 '개장'
    check("파싱 불가 입력 → 개장으로 폴백", cc.is_market_open("쓰레기") is True)
    # 이름 조회
    check("holiday_name — 굿프라이데이",
          cc.holiday_name("2026-04-03") == "Good Friday",
          "실제=" + cc.holiday_name("2026-04-03"))
    check("holiday_name — 개장일은 빈 문자열", cc.holiday_name("2026-08-19") == "")
    # 거래일 이동
    check("next_trading_day — 금 다음은 월",
          cc.next_trading_day("2026-08-21") == date(2026, 8, 24))
    check("next_trading_day — 휴장일 건너뜀",
          cc.next_trading_day("2026-12-24") == date(2026, 12, 28),
          "실제=" + str(cc.next_trading_day("2026-12-24")))
    check("prev_trading_day — 월 이전은 금",
          cc.prev_trading_day("2026-08-24") == date(2026, 8, 21))


# ══════════════════════════════════════════════════════════════════════════
# E. 시트 파싱 — 깨진 입력에도 예외 없이
# ══════════════════════════════════════════════════════════════════════════
def test_parse():
    print("\n── E. 시트 파싱 내성")
    hdr = cc.CAL_COLS
    good = [hdr,
            ["2027-01-01", "NASDAQ", "New Year's Day", "Y", "", "", "FMP", ""],
            ["2027-11-26", "NASDAQ", "Thanksgiving", "N", "09:30", "13:00", "FMP", ""]]
    r = cc.parse_calendar_values(good)
    check("휴장일 파싱", r["closed"] == {"2027-01-01"}, str(r["closed"]))
    check("반일장 파싱", r["half"] == {"2027-11-26": "13:00"}, str(r["half"]))
    check("연도 수집", r["years"] == {2027}, str(r["years"]))

    check("빈 입력 → 빈 결과", cc.parse_calendar_values([])["closed"] == set())
    check("헤더만 → 빈 결과", cc.parse_calendar_values([hdr])["closed"] == set())
    check("헤더 결손 → 빈 결과 (예외 아님)",
          cc.parse_calendar_values([["A", "B"], ["x", "y"]])["closed"] == set())
    broken = [hdr, ["", "", "", "", "", "", "", ""], ["짧은행"],
              ["2027-01-01", "NASDAQ", "N", "Y", "", "", "", ""]]
    check("깨진 행 혼재 → 유효분만 수집",
          cc.parse_calendar_values(broken)["closed"] == {"2027-01-01"})
    check("truthy 변형 인식",
          cc.parse_calendar_values(
              [hdr, ["2027-01-01", "", "", "TRUE", "", "", "", ""]])["closed"]
          == {"2027-01-01"})


# ══════════════════════════════════════════════════════════════════════════
# F. FMP 대조 — 임시 휴장 검출 경로
# ══════════════════════════════════════════════════════════════════════════
def test_diff():
    print("\n── F. FMP 대조 (임시 휴장 검출)")
    recs = [{"exchange": "NASDAQ", "date": "2025-01-01", "name": "New Year's Day",
             "isClosed": True, "adjOpenTime": None, "adjCloseTime": None},
            {"exchange": "NASDAQ", "date": "2025-01-09", "name": "Day of Mourning",
             "isClosed": True, "adjOpenTime": None, "adjCloseTime": None},
            {"exchange": "NASDAQ", "date": "2025-11-28", "name": "Day after Thanksgiving",
             "isClosed": False, "adjOpenTime": "09:30", "adjCloseTime": "13:00"}]
    d = cc.diff_against_rules(recs, years=[2025])
    check("국장일을 임시 휴장으로 검출",
          "2025-01-09" in d["extra_closed"], str(d["extra_closed"]))
    check("정규 휴일은 임시로 오검출 안 함",
          "2025-01-01" not in d["extra_closed"])
    check("반일장 분리", d["half_days"] == {"2025-11-28": "13:00"}, str(d["half_days"]))
    check("응답 누락분 보고", "2025-07-04" in d["missing"])

    # 빈 입력 / 잡음 입력
    check("빈 응답 → 예외 없음", cc.diff_against_rules([], years=[2025])["extra_closed"] == {})
    check("잡음 섞인 응답 → 무시",
          cc.diff_against_rules([None, "x", {"date": "bad"}, {}], years=[2025])
          ["extra_closed"] == {})

    rows = cc.rows_from_fmp(recs, source="FMP", now_str="t")
    check("rows_from_fmp 행 수", len(rows) == 3, str(len(rows)))
    check("rows_from_fmp 열 수 = CAL_COLS", all(len(r) == len(cc.CAL_COLS) for r in rows))
    check("rows_from_fmp 날짜 정렬", [r[0] for r in rows] == sorted(r[0] for r in rows))
    check("rows_from_fmp isClosed → Y/N", rows[0][3] == "Y" and rows[2][3] == "N")


# ══════════════════════════════════════════════════════════════════════════
# H. 반일장(조기 마감) — 규칙 골든 + 마감시각 계약
#
# 왜 여기 있어야 하나
# ───────────────────
# 반일장 판정은 **규칙 계산**이다(app.py 핫 패스라 시트를 못 읽는다).
# refresh_market_calendar 가 배포 전 게이트로 이 스위트를 돈다. 골든이 여기
# 없으면 calendar_core 를 고쳐 반일장 규칙을 깨뜨려도 게이트를 통과한다.
#
# 골든 출처
# ─────────
#   2020~2024  NYSE 공개 이력
#   2025~2026  FMP 실측 (diag_halfday, adjCloseTime='13:00')
#
# 2020~2030 구간에서 7/4 와 12/25 의 요일 7가지가 전부 등장하므로 규칙의
# 모든 분기가 이 골든으로 검증된다.
# ══════════════════════════════════════════════════════════════════════════
GOLDEN_HALF = {
    2020: {"2020-11-27", "2020-12-24"},
    2021: {"2021-11-26"},
    2022: {"2022-11-25"},
    2023: {"2023-07-03", "2023-11-24"},
    2024: {"2024-07-03", "2024-11-29", "2024-12-24"},
    2025: {"2025-07-03", "2025-11-28", "2025-12-24"},
    2026: {"2026-11-27", "2026-12-24"},
}


def test_halfday():
    print("\n── H. 반일장 (조기 마감 13:00)")

    for y, gold in sorted(GOLDEN_HALF.items()):
        got = set(cc.nyse_early_close_days(y))
        ok = got == gold
        detail = ""
        if not ok:
            detail = "누락=" + str(sorted(gold - got)) + " 초과=" + str(sorted(got - gold))
        check("골든 " + str(y), ok, detail)

    # 요일 조건 — 규칙의 핵심. 7/4·12/25 가 화~금일 때만 전날이 반일장이다.
    # 토요일이면 전날이 **관측 휴일**(전휴장)이지 반일장이 아니다.
    check("7/4 토요일(2026) → 7/3 은 전휴장, 반일장 아님",
          "2026-07-03" not in cc.nyse_early_close_days(2026)
          and "2026-07-03" in cc.nyse_regular_holidays(2026))
    check("7/4 일요일(2027) → 7/3 은 토요일, 반일장 아님",
          "2027-07-03" not in cc.nyse_early_close_days(2027))
    check("12/25 토요일(2027) → 12/24 는 전휴장, 반일장 아님",
          "2027-12-24" not in cc.nyse_early_close_days(2027))
    check("추수감사절 다음 금요일은 매년 반일장",
          all(any(d.startswith(str(y) + "-11") for d in cc.nyse_early_close_days(y))
              for y in range(2020, 2031)))
    # 전휴장과 반일장이 동시에 참인 날이 있으면 두 판정이 모순된다
    check("반일장이 전휴장과 겹치지 않는다 (2020~2030)",
          all(not (set(cc.nyse_early_close_days(y))
                   & set(cc.nyse_regular_holidays(y)))
              for y in range(2020, 2031)))

    # ── 마감시각 계약 ────────────────────────────────────────────────────
    # ⚠️ 이 파일의 check() 는 (name, cond, detail) 이다 — (name, got, want) 가
    #    아니다. 값을 그대로 넘기면 truthy 인 한 무조건 통과한다(판별력 0).
    #    반드시 == 비교 결과를 넘긴다.
    def eq(name, got, want):
        check(name, got == want, "기대=" + repr(want) + " 실제=" + repr(got))

    eq("session_close_time — 반일장", cc.session_close_time("2026-11-27"), "13:00")
    eq("session_close_time — 평일", cc.session_close_time("2026-11-25"), "16:00")
    eq("session_close_time — 휴장", cc.session_close_time("2026-11-26"), None)
    eq("session_close_time — 주말", cc.session_close_time("2026-08-22"), None)
    eq("session_close_time — 임시휴장 보강이 우선",
       cc.session_close_time("2026-11-27", extra_closed={"2026-11-27"}), None)
    eq("session_close_time — half_map 이 규칙보다 우선",
       cc.session_close_time("2026-11-25", half_map={"2026-11-25": "12:00"}), "12:00")
    eq("session_close_time — half_map 은 휴장을 뒤집지 못한다",
       cc.session_close_time("2026-11-26", half_map={"2026-11-26": "13:00"}), None)
    eq("is_early_close — 반일장", cc.is_early_close("2026-11-27"), True)
    eq("is_early_close — 평일", cc.is_early_close("2026-11-25"), False)
    eq("is_early_close — 휴장일은 False (열려야 조기마감이 의미)",
       cc.is_early_close("2026-11-26"), False)
    eq("close_minutes — 13:00", cc.close_minutes("13:00"), 780)
    eq("close_minutes — 16:00", cc.close_minutes("16:00"), 960)
    eq("close_minutes — 쓰레기 입력은 정규 마감으로 폴백",
       cc.close_minutes("nope"), 960)
    eq("close_minutes — None 폴백", cc.close_minutes(None), 960)
    eq("close_minutes — 범위 밖(25:00)은 폴백", cc.close_minutes("25:00"), 960)


# ══════════════════════════════════════════════════════════════════════════
# I. 소비자 배선 — calendar_core 를 **실제로** 쓰는가
#
# 왜 이 검사군이 필요한가 (이 스위트가 4개월간 놓친 것)
# ─────────────────────────────────────────────────────
# A~H 는 calendar_core **자체**만 본다. 규칙 엔진이 완벽해도 소비자가 그걸 안
# 부르면 아무 의미가 없는데, 그 확인이 어디에도 없었다.
#
# 실제로 그렇게 됐다. calendar_core 는 5개 자동화 파일의 하드코딩 휴장일을
# 대체하려고 만들었는데 `run_earnings_watch.py` 하나가 이관되지 않은 채로
# 남았다(2026-12-25 에서 끝나는 집합 그대로). 스위트는 그동안 계속 초록불이었다.
# 게이트가 **공허하게 통과**한 것이다 — 0건 검사는 0건 실패다.
#
# 두 갈래로 나눈 이유
# ───────────────────
#   ① 하드코딩 금지 → automation 전 파일에 적용. 고정 목록이 아니라 **탐색**이라
#      나중에 추가되는 파일도 자동으로 걸린다. 이게 래칫이다.
#   ② import + 호출 존재 → 게이트 소비자 5개에만 적용. 새 스크립트가 캘린더를
#      쓸 이유가 없을 수도 있으므로 전 파일에 강제하지 않는다.
#
# ⚠️ 탐색이 0건이면 조용히 전부 통과한다. I-0 이 하한을 못 박는 이유다.
# ⚠️ 문자열 검색이 아니라 AST 다. 주석·독스트링의 날짜(이 파일 위쪽 골든 설명,
#    calendar_core 독스트링 등)를 오탐하지 않으려면 그래야 한다.
# ══════════════════════════════════════════════════════════════════════════
import ast   # noqa: E402  — I군 전용. 상단에 두면 A~H 가 쓰지 않는 의존이 된다.
import re    # noqa: E402

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 개장 여부로 실행을 멈추는 파일. import 와 호출이 **둘 다** 있어야 한다.
GATE_CONSUMERS = [
    "run_watchlist_alerts.py",
    "run_drg_predict.py",
    "run_drg_verify.py",
    "run_narrative.py",
    "run_earnings_watch.py",
]

# 하드코딩 금지 대상 탐색 접두사. diag_* 는 제외한다 — 골든 픽스처가 정당하게
# 날짜 집합을 갖는다(이 파일의 GOLDEN 이 바로 그것이다).
SCAN_PREFIXES = ("run_", "refresh_", "backfill_", "seed_")

_MIN_SCAN = 8        # 탐색 하한. 실측 13개이므로 여유가 있다.
_HARDCODE_MIN = 3    # 컬렉션 안 날짜 리터럴이 이 개수 이상이면 '휴장일표'로 본다


def _scan_dirs():
    """automation/ 우선, 없으면 평면 배치도 받아준다 (diag_halfday_gate 관용구)."""
    return [_HERE, os.path.join(_ROOT, "automation"), _ROOT]


def _discover():
    """{파일명: 경로}. 같은 이름은 먼저 찾은 쪽이 이긴다."""
    found = {}
    for base in _scan_dirs():
        if not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for nm in names:
            if not nm.endswith(".py") or nm in found:
                continue
            if nm.startswith(SCAN_PREFIXES):
                found[nm] = os.path.join(base, nm)
    return found


def _parse(path):
    try:
        with open(path, encoding="utf-8") as f:
            return ast.parse(f.read())
    except Exception:
        return None


# ── 무엇이 '휴장일표'인가 — 이름이 아니라 **형상**으로 판정한다 ────────────
# 처음엔 (파일, 변수명) 면제 목록으로 갔다가 버렸다. 두 가지가 걸렸다.
#
#   ① `_HARDCODED_CALENDAR_2026` (run_drg_predict / run_narrative)
#      FOMC·CPI·NFP **발표 일정** 32일. 휴장일이 아니다.
#   ② `SEEDS` (seed_reminders)
#      리마인더 설정 구조 안에 흩어진 due 날짜 4개.
#
# 면제 목록으로 막으면 새 파일이 생길 때마다 목록을 늘려야 하고, 무엇보다
# **`_NYSE_HOLIDAYS` 를 면제된 이름으로 바꾸는 것만으로 래칫을 빠져나간다.**
#
# 판별자는 따로 있다 — 휴장일표는 정의상 NYSE 휴장일과 겹친다.
#
#     _NYSE_HOLIDAYS (이관 전)   20일 중 20일 겹침   100%
#     _HARDCODED_CALENDAR_2026   32일 중  1일 겹침     3%   ← 2026-04-03
#     SEEDS                       4일 중  0일 겹침     0%       굿프라이데이에도
#                                                              BLS 는 NFP 를 낸다
#
# 이 기준은 이름·파일·변수 구조와 무관하다. 면제 목록도 필요 없다.
#
# ⚠️ 이 판정은 cc.nyse_regular_holidays 에 의존한다. 그게 빈 값을 돌려주면
#    겹침이 0 이 되어 **전부 조용히 통과한다.** I-R0 이 그 전제를 먼저 못 박는다.
_HOLIDAY_OVERLAP_MIN = 3      # 겹침 절대 개수 하한
_HOLIDAY_OVERLAP_RATIO = 0.5  # 겹침 비율 하한


def _holiday_overlap(dates):
    """날짜 목록 중 실제 NYSE 정규 휴장일인 것의 개수."""
    hol = set()
    for d in dates:
        try:
            hol |= set(cc.nyse_regular_holidays(int(d[:4])))
        except Exception:
            pass
    return len(set(dates) & hol)


def date_literal_groups(tree, fname=""):
    """대입문 안의 **휴장일표로 보이는** 날짜 뭉치 → [(줄번호, 개수, 이름, 겹침)].

    set / list / tuple / dict 를 전부 본다. dict 는 키·값 양쪽 —
    {"2025-01-01": "New Year's Day", ...} 형태가 실제로 쓰이는 모양이다.

    ⚠️ **대입문 단위**로 본다. 익명 리터럴이 아니라 이름을 알아야 실패 메시지가
       쓸모 있다("줄 116" 보다 "_NYSE_HOLIDAYS" 가 낫다).

    낱개 날짜(창 경계, 상수 1~2개)는 _HARDCODE_MIN 미만이라 애초에 통과한다.

    ⚠️ fname 은 **판정에 쓰이지 않는다.** 초안에서 (파일, 변수명) 면제 목록을
       운용하려고 받았는데, 형상 기준으로 바꾸면서 불필요해졌다. 인자를 남긴
       이유는 하나다 — 파일별 예외가 필요해지는 순간 호출부를 안 고치고 여기서만
       분기할 수 있게. 그런 예외가 끝내 안 생기면 지워도 된다.
    """
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", "") if node.targets else ""
            body = node.value
        elif isinstance(node, ast.AnnAssign):
            name = getattr(node.target, "id", "")
            body = node.value
        else:
            continue
        if body is None:
            continue
        dates = [v.value for v in ast.walk(body)
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)
                 and _DATE_RE.match(v.value)]
        if len(dates) < _HARDCODE_MIN:
            continue
        ov = _holiday_overlap(dates)
        if ov < _HOLIDAY_OVERLAP_MIN:
            continue
        if ov / float(len(set(dates))) < _HOLIDAY_OVERLAP_RATIO:
            continue
        hits.append((getattr(node, "lineno", 0), len(dates),
                     name or "<익명>", ov))
    return hits


def calendar_alias(tree):
    """`import calendar_core as X` 의 X. 없으면 None."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "calendar_core":
                    return a.asname or "calendar_core"
        elif isinstance(node, ast.ImportFrom):
            if node.module == "calendar_core":
                return ""      # from calendar_core import ... — 별칭 없음
    return None


def calls_open_check(tree, alias):
    """alias.is_market_open* 를 실제로 부르는가.

    import 만 하고 배선을 잊는 것이 가장 그럴듯한 회귀다 — 그러면 파일은
    '이관 완료'처럼 보이면서 게이트는 사라진다. import 존재만으로는 못 잡는다.
    """
    if alias is None:
        return False
    if alias == "":                       # from-import 형태
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id.startswith("is_market_open")):
                return True
        return False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == alias
                and node.attr.startswith("is_market_open")):
            return True
    return False


# ── 역검증 픽스처 — 이관 **전** run_earnings_watch.py 의 실제 모양 ──────────
# 검사를 믿기 전에 '알려진 나쁜 입력'에서 반드시 빨간불이 나는지 확인한다.
# 이 픽스처가 곧 회귀 케이스다(이 결함이 재발하면 여기서 먼저 걸린다).
_BAD_FIXTURE = '''
_NYSE_HOLIDAYS = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def is_market_open_today() -> bool:
    now = datetime.now(_ET)
    return now.weekday() < 5 and now.strftime("%Y-%m-%d") not in _NYSE_HOLIDAYS
'''

# 이관 **후** 의 모양. 통과해야 한다 — 아니면 검사가 과민한 것이다.
_GOOD_FIXTURE = '''
import calendar_core as cc


def is_market_open_today() -> bool:
    return cc.is_market_open_today()
'''

# import 만 하고 배선을 잊은 모양. import 검사는 통과하고 호출 검사는 실패해야
# 한다 — 두 검사가 서로 다른 것을 본다는 증거다.
_LIMP_FIXTURE = '''
import calendar_core as cc

_CUTOFF = 5


def is_market_open_today() -> bool:
    return True
'''


def test_wiring():
    print("\n── I. 소비자 배선 (calendar_core 를 실제로 쓰는가)")

    files = _discover()
    # I-0 · I-1: 탐색이 살아 있는가. 이게 없으면 아래 전부가 공허하게 통과한다.
    check("I-0  automation 파일 탐색 " + str(len(files)) + "개 (하한 "
          + str(_MIN_SCAN) + ")", len(files) >= _MIN_SCAN,
          "탐색 실패 — 아래 검사가 전부 무의미해진다. 실행 경로 확인 필요")
    missing = [f for f in GATE_CONSUMERS if f not in files]
    check("I-1  게이트 소비자 5개 전원 탐색됨", not missing,
          "누락=" + str(missing))

    # I-2군: 하드코딩 금지 — 탐색된 **전 파일**. 새 파일도 자동으로 걸린다.
    for nm in sorted(files):
        tree = _parse(files[nm])
        if tree is None:
            check("I-2  " + nm + " 파싱", False, "AST 파싱 실패")
            continue
        hits = date_literal_groups(tree, nm)
        check("I-2  " + nm + " 하드코딩 휴장일표 0건", not hits,
              ", ".join(who + " 줄" + str(ln) + " (날짜 " + str(n)
                        + "개 중 휴장일 " + str(ov) + "개 일치)"
                        for ln, n, who, ov in hits))

    # I-3군: 게이트 소비자는 import + 호출이 둘 다 있어야 한다.
    for nm in GATE_CONSUMERS:
        p = files.get(nm)
        if not p:
            continue                       # I-1 이 이미 실패로 보고했다
        tree = _parse(p)
        if tree is None:
            continue
        alias = calendar_alias(tree)
        check("I-3  " + nm + " calendar_core import", alias is not None,
              "import 없음")
        check("I-4  " + nm + " is_market_open* 호출",
              calls_open_check(tree, alias),
              "import 만 있고 배선이 없다")

    # ── 역검증 — 검사가 알려진 나쁜 입력에서 실제로 빨간불이 나는가 ──────
    bad = ast.parse(_BAD_FIXTURE)
    good = ast.parse(_GOOD_FIXTURE)
    limp = ast.parse(_LIMP_FIXTURE)

    check("I-R1 이관 전 픽스처 → 날짜 집합 검출됨",
          bool(date_literal_groups(bad, "run_earnings_watch.py")),
          "검출 못 함 — 이 검사는 판별력이 없다")
    check("I-R2 이관 전 픽스처 → import 없음으로 판정",
          calendar_alias(bad) is None)
    check("I-R3 이관 후 픽스처 → 날짜 집합 0건 (과민하지 않음)",
          not date_literal_groups(good, "run_earnings_watch.py"))
    check("I-R4 이관 후 픽스처 → import + 호출 둘 다 인식",
          calendar_alias(good) == "cc"
          and calls_open_check(good, calendar_alias(good)))
    check("I-R5 import 만 있고 배선 없는 픽스처 → 호출 검사만 실패",
          calendar_alias(limp) == "cc"
          and not calls_open_check(limp, calendar_alias(limp)),
          "두 검사가 같은 것을 보고 있다 — 하나는 잉여다")
    check("I-R6 낱개 날짜 2개는 통과 (문턱 " + str(_HARDCODE_MIN) + ")",
          not date_literal_groups(ast.parse('X = ["2025-01-01", "2025-02-01"]')))
    check("I-R7 dict 형태 휴장일표도 검출",
          bool(date_literal_groups(ast.parse(
              'H = {"2025-01-01": "a", "2025-01-20": "b", "2025-02-17": "c"}'))))

    # ── 형상 기준이 실제로 판별하는가 ────────────────────────────────────
    # 이름 면제 목록을 버리고 '휴장일과 겹치는가'로 판정한다. R8~R11 이 그
    # 기준의 양쪽 끝을 고정한다 — 이름을 바꿔도 잡히고, 다른 날짜표는 통과한다.
    _hol26 = ", ".join('"' + d + '"' for d in
                       sorted(cc.nyse_regular_holidays(2026)))
    check("I-R8 이름을 바꿔도 휴장일표는 검출 (이름 우회 불가)",
          bool(date_literal_groups(
              ast.parse("_MY_HARMLESS_DATES = {" + _hol26 + "}"),
              "run_earnings_watch.py")),
          "이름만 바꾸면 래칫을 빠져나간다")
    check("I-R9 경제지표 발표 일정은 통과 (겹침 없음)",
          not date_literal_groups(ast.parse(
              '_C = {"CPI": ["2026-01-14", "2026-02-11", "2026-03-11", '
              '"2026-04-10", "2026-05-13"]}')))
    check("I-R10 설정 구조에 흩어진 날짜는 통과 (seed_reminders 형태)",
          not date_literal_groups(ast.parse(
              '_S = [D(due="2026-10-15"), D(due="2026-11-03"), '
              'D(due="2026-11-15"), D(due="2027-02-02")]')))
    check("I-R11 휴장일 절반 섞인 표도 검출 (비율 "
          + str(_HOLIDAY_OVERLAP_RATIO) + ")",
          bool(date_literal_groups(ast.parse(
              '_M = ["2026-01-01", "2026-07-03", "2026-12-25", '
              '"2026-03-11", "2026-05-13"]'))))

    # ⚠️ 위 판정은 전부 cc.nyse_regular_holidays 에 기댄다. 그게 비면 겹침이
    #    0 이 되어 I-2 가 **조용히 전부 통과**한다. 전제를 여기서 못 박는다.
    check("I-R0 겹침 판정의 전제 — 2026 휴장일 10개가 실제로 나온다",
          len(cc.nyse_regular_holidays(2026)) == 10,
          "calendar_core 가 비었다면 I-2 는 전부 공허한 통과다")


# ══════════════════════════════════════════════════════════════════════════
# J. 세션 구간 — session_phase / narrative_session_label
# ══════════════════════════════════════════════════════════════════════════
# 무엇을 지키나
# ─────────────
# 이 두 함수가 틀리면 **숫자는 맞고 라벨만 틀린다.** 반일장 13:30 을
# "🟢 Market Hours Analysis" 라고 시트에 저장해도 예외가 안 나고, 추수감사절
# 정오에 "장중"이라고 표시해도 에러 로그가 없다. 기계로 확인하는 수밖에 없다.
#
# 경계를 정각 단위로 못 박는 이유
# ───────────────────────────────
# 통합 전에는 밴드가 `570 <= m <= 960` (마감 정각 포함) 이었고 app.py 시장 상태
# 헤더는 `m < close_m` (마감 정각 제외) 였다. 그래서 16:00 정각 1분 동안 한
# 화면에 "🌙 After-hours" 와 "🟢 Market Hours Analysis" 가 **동시에** 떴다.
# 20:00 정각에도 같은 일이 있었다. 헤더 쪽(제외)으로 통일했고, 그 통일이
# 유지되는지를 J-B 군이 정각으로 고정한다.
def _phase(y, mo, d, hh, mi, **kw):
    return cc.session_phase(datetime(y, mo, d, hh, mi), **kw)


def test_session_phase():
    print("\n── J. 세션 구간 (session_phase · narrative_session_label)")

    # ── J-A: 판정 불가는 open 이 아니라 unknown ──────────────────────────
    # is_market_open 은 개장으로 fail-open 한다(알림을 놓치는 것보다 헛도는
    # 쪽이 낫다). 이 함수의 산출물은 **사람이 읽는 라벨**이라 판단 근거가
    # 아니므로, 모를 때는 모른다고 말하는 쪽이 정직하다. 방향이 다른 게
    # 의도라는 것을 여기서 못 박는다.
    check("J-A1 None → unknown", cc.session_phase(None) == cc.PHASE_UNKNOWN,
          str(cc.session_phase(None)))
    check("J-A2 date 객체(시각 없음) → unknown",
          cc.session_phase(date(2026, 11, 25)) == cc.PHASE_UNKNOWN)
    check("J-A3 문자열 → unknown",
          cc.session_phase("2026-11-25") == cc.PHASE_UNKNOWN)
    check("J-A4 unknown 라벨",
          cc.narrative_session_label(None) == "⏱️ Unknown session")

    # ── J-B: 정규장 경계 (2026-11-25 수요일) ────────────────────────────
    check("J-B1 03:59 → overnight", _phase(2026, 11, 25, 3, 59) == "overnight")
    check("J-B2 04:00 → pre (경계)", _phase(2026, 11, 25, 4, 0) == "pre")
    check("J-B3 09:29 → pre", _phase(2026, 11, 25, 9, 29) == "pre")
    check("J-B4 09:30 → open (경계)", _phase(2026, 11, 25, 9, 30) == "open")
    check("J-B5 15:59 → open", _phase(2026, 11, 25, 15, 59) == "open")
    check("J-B6 16:00 → post (마감 정각은 open 이 아니다)",
          _phase(2026, 11, 25, 16, 0) == "post",
          "헤더와 어긋나면 한 화면에 두 상태가 뜬다")
    check("J-B7 19:59 → post", _phase(2026, 11, 25, 19, 59) == "post")
    check("J-B8 20:00 → overnight (마감+4h 정각도 경계)",
          _phase(2026, 11, 25, 20, 0) == "overnight")
    check("J-B9 23:59 → overnight", _phase(2026, 11, 25, 23, 59) == "overnight")

    # ── J-C: 반일장 경계 (2026-11-27 추수감사절 다음 금요일) ────────────
    # 이 세 줄이 이번 작업의 존재 이유다. 통합 전에는 전부 open 이었다.
    check("J-C1 반일장 12:59 → open", _phase(2026, 11, 27, 12, 59) == "open")
    check("J-C2 반일장 13:00 → post (여기가 예전에 open 이었다)",
          _phase(2026, 11, 27, 13, 0) == "post")
    check("J-C3 반일장 13:30 → post", _phase(2026, 11, 27, 13, 30) == "post")
    check("J-C4 반일장 16:59 → post (마감+4h 는 17:00)",
          _phase(2026, 11, 27, 16, 59) == "post")
    check("J-C5 반일장 17:00 → overnight",
          _phase(2026, 11, 27, 17, 0) == "overnight")
    check("J-C6 반일장 09:30 → open (개장은 정규장과 같다)",
          _phase(2026, 11, 27, 9, 30) == "open")
    check("J-C7 12/24 도 반일장 13:30 → post",
          _phase(2026, 12, 24, 13, 30) == "post")

    # ── J-D: 휴장 ────────────────────────────────────────────────────────
    check("J-D1 추수감사절 정오 → closed", _phase(2026, 11, 26, 12, 0) == "closed")
    check("J-D2 주말 정오 → closed", _phase(2026, 11, 28, 12, 0) == "closed")
    check("J-D3 휴장 라벨",
          cc.narrative_session_label(datetime(2026, 11, 26, 12, 0))
          == "🌑 Market Closed")

    # ── J-E: 보강분이 규칙을 이긴다 (extra_closed · half_map) ───────────
    # 시트 보강 경로는 아직 아무도 안 쓰지만, 계약은 지금 고정해 둔다.
    # 나중에 연결할 때 이 검사가 있으면 방향(보강 > 규칙)을 다시 안 정한다.
    check("J-E1 extra_closed 로 임시 휴장 주입 → closed",
          _phase(2026, 11, 25, 12, 0,
                 extra_closed={"2026-11-25"}) == "closed")
    check("J-E2 half_map 으로 조기 마감 주입 → 13:30 이 post",
          _phase(2026, 11, 25, 13, 30,
                 half_map={"2026-11-25": "13:00"}) == "post")
    check("J-E3 half_map 은 그 날짜에만 적용",
          _phase(2026, 11, 24, 13, 30,
                 half_map={"2026-11-25": "13:00"}) == "open")

    # ── J-F: 내림차순 판정 — 망가진 half_map 에도 구간이 안 뒤집힌다 ────
    # 오름차순(pre 를 먼저 보는 방식)이면 이미 닫힌 장을 "pre" 라고 부른다.
    # 시트의 Adj_Close 는 사람이 손댈 수 있는 값이라 이 방어가 필요하다.
    check("J-F1 half_map='05:00' · 07:00 → pre 가 아니라 post",
          _phase(2026, 11, 25, 7, 0,
                 half_map={"2026-11-25": "05:00"}) == "post")
    check("J-F2 half_map 쓰레기값 → close_minutes 폴백(16:00)으로 open",
          _phase(2026, 11, 25, 13, 30,
                 half_map={"2026-11-25": "nope"}) == "open")

    # ── J-G: 라벨 표 계약 ────────────────────────────────────────────────
    lbl = cc.NARRATIVE_SESSION_LABELS
    phases = {cc.PHASE_UNKNOWN, cc.PHASE_CLOSED, cc.PHASE_PRE,
              cc.PHASE_OPEN, cc.PHASE_POST, cc.PHASE_OVERNIGHT}
    check("J-G1 6개 구간 전부 라벨이 있다", set(lbl) == phases,
          "누락=" + str(sorted(phases - set(lbl))))
    check("J-G2 라벨 6개가 전부 서로 다르다", len(set(lbl.values())) == 6)
    check("J-G3 session_phase 는 이 6개 외의 값을 내지 않는다",
          all(_phase(2026, 11, 25, h, mm) in phases
              for h in range(24) for mm in (0, 30, 59)))
    # 통합 전 app.py / run_narrative 가 쓰던 문자열 4개가 그대로여야 한다.
    # 바뀌면 Narratives 시트에 그 시점 이후로 다른 문자열이 쌓여 히스토리가
    # 두 갈래로 쪼개진다.
    for ph, want in (("pre", "🌅 Pre-market Prep"),
                     ("open", "🟢 Market Hours Analysis"),
                     ("post", "🔔 Daily Recap (Post-Market)"),
                     ("overnight", "🌙 Overnight Strategy")):
        check("J-G4 기존 라벨 보존 — " + ph, lbl.get(ph) == want,
              "시트 히스토리가 쪼개진다: " + str(lbl.get(ph)))


# ══════════════════════════════════════════════════════════════════════════
# K. 세션 라벨 소비자 배선 — 밴드를 **다시 적지 않는가**
# ══════════════════════════════════════════════════════════════════════════
# 왜 J 만으로는 부족한가
# ──────────────────────
# calendar_core 가 완벽해도 app.py 가 자기 안의 낡은 밴드 표를 계속 쓰면
# 아무것도 안 바뀐다. 그게 정확히 이 변경 **직전**의 상태였다 — 반일장 규칙은
# 이미 v1.1.0 에 있었는데 세션 라벨은 그걸 안 봤다.
#
# I 군과 같은 구조이되 대상이 다르다. I 는 "휴장일 게이트를 부르는가", K 는
# "밴드를 다시 적지 않는가". 후자는 호출 유무가 아니라 **부재**를 봐야 해서
# 리터럴 래칫이 필요하다.
_LABEL_BANDS = {240, 569, 570, 960, 961, 1200}

# app.py 의 라벨 함수 · run_narrative.py 의 라벨 함수
_LABEL_FNS = (("app.py", "_narrative_session_label_for_et_dt"),
              ("run_narrative.py", "_session_label_for_utc"))


def _find_file(name):
    """_scan_dirs() 순서로 찾는다. app.py 는 repo root 에 있다."""
    for base in _scan_dirs():
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return None


def _read(name):
    p = _find_file(name)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def label_wiring(src, fn_name):
    """라벨 함수의 배선 불변식. 소스 문자열을 받아 dict 를 돌려준다.

    같은 함수를 뮤테이션된 소스에 다시 돌려 판별력을 확인한다(K-R 군).
    """
    out = {"exists": False, "no_band_literal": False, "delegates": False}
    try:
        tree = ast.parse(src)
    except Exception:
        return out
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == fn_name:
            fn = n
    if fn is None:
        return out
    out["exists"] = True
    # 밴드 리터럴이 함수 안에 되살아났는가
    lits = {v.value for v in ast.walk(fn)
            if isinstance(v, ast.Constant) and isinstance(v.value, int)}
    out["no_band_literal"] = not (lits & _LABEL_BANDS)
    # calendar_core 의 라벨 함수를 부르는가 (별칭 무관 — mcal / cc 둘 다)
    for c in ast.walk(fn):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "narrative_session_label"):
            out["delegates"] = True
    return out


def market_session_wiring(src):
    """app.py DRG 프롬프트의 market_session 배선.

    옛 모양은 IfExp 한 줄 대입(`"장 중" if et_hour < 16 else ...`)이고,
    새 모양은 _sess_ct/_sess_m 를 보는 if/elif 체인이다. 둘은 구조가 달라서
    AST 로 확실히 갈린다.
    """
    out = {"ct_from_core": False, "guarded_by_ct": False,
           "has_closed_branch": False, "no_hour_name": False}
    try:
        tree = ast.parse(src)
    except Exception:
        return out

    # ① _sess_ct 를 calendar_core.session_close_time 에서 받는가
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                and getattr(n.targets[0], "id", "") == "_sess_ct"):
            continue
        v = n.value
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and v.func.attr == "session_close_time"):
            out["ct_from_core"] = True

    # ② market_session 대입을 감싼 if 체인이 _sess_ct/_sess_m 를 보는가
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "market_session"
                       for t in n.targets)]
    if not assigns:
        return out
    test_names = set()
    covered = False
    for n in ast.walk(tree):
        if not isinstance(n, ast.If):
            continue
        inner = list(ast.walk(n))
        if not any(a is x for a in assigns for x in inner):
            continue
        covered = True
        for t in [n.test] + [e.test for e in ast.walk(n)
                             if isinstance(e, ast.If)]:
            test_names |= {x.id for x in ast.walk(t)
                           if isinstance(x, ast.Name)}
    out["guarded_by_ct"] = bool(covered
                                and {"_sess_ct", "_sess_m"} <= test_names)

    # ③ et_hour / et_minute 가 판정에 되살아나지 않았는가
    #
    # ⚠️ 대입식(IfExp)의 이름도 함께 본다. 옛 모양은 if **문**이 아니라 한 줄
    #    삼항식이라 위 루프에 아예 안 걸린다 — test_names 만 보면 옛 코드에서
    #    이름 집합이 비어 있어 이 검사가 **조용히 통과**한다(K-R7 이 이걸
    #    잡아냈다). guarded_by_ct 와는 다른 집합을 봐야 두 검사가 서로 다른
    #    것을 본다.
    value_names = set()
    for a in assigns:
        value_names |= {x.id for x in ast.walk(a.value)
                        if isinstance(x, ast.Name)}
    out["no_hour_name"] = not ({"et_hour", "et_minute"}
                               & (test_names | value_names))
    # ④ 휴장 분기가 있는가 — 예전엔 휴장이 "장 전/장 후" 로 뭉개졌다
    out["has_closed_branch"] = any(
        isinstance(a.value, ast.Constant) and a.value.value == "휴장"
        for a in assigns)
    return out


def half_reminder_wiring(src):
    """반일장 불일치가 add_reminder 로 배달되는가."""
    out = {"delivers": False}
    try:
        tree = ast.parse(src)
    except Exception:
        return out
    for n in ast.walk(tree):
        if not isinstance(n, ast.If):
            continue
        tn = {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}
        if not ({"_fo", "_ro", "_td"} & tn):
            continue
        for c in ast.walk(n):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == "add_reminder"):
                out["delivers"] = True
    return out


# ── 역검증 픽스처 — 변경 **전** 의 실제 소스다. 곧 회귀 케이스다 ──────────
_OLD_LABEL_FIXTURE = '''
def _narrative_session_label_for_et_dt(dt_et):
    """ET 기준 Ryan 루틴 세션 라벨."""
    if dt_et is None:
        return "⏱️ Unknown session"
    m = dt_et.hour * 60 + dt_et.minute
    if 240 <= m <= 569:
        return "🌅 Pre-market Prep"
    if 570 <= m <= 960:
        return "🟢 Market Hours Analysis"
    if 961 <= m <= 1200:
        return "🔔 Daily Recap (Post-Market)"
    return "🌙 Overnight Strategy"
'''

_NEW_LABEL_FIXTURE = '''
def _narrative_session_label_for_et_dt(dt_et):
    """위임."""
    return mcal.narrative_session_label(dt_et)
'''

# 위임은 하는데 밴드가 남아 있는 모양. 두 검사가 서로 다른 것을 본다는 증거
# (_LIMP_FIXTURE 와 같은 역할이다).
_LIMP_LABEL_FIXTURE = '''
def _narrative_session_label_for_et_dt(dt_et):
    m = dt_et.hour * 60 + dt_et.minute
    if 570 <= m <= 960:
        return "🟢 Market Hours Analysis"
    return mcal.narrative_session_label(dt_et)
'''

_OLD_MS_FIXTURE = '''
def f():
    et_hour = now_et_for_session.hour
    et_minute = now_et_for_session.minute
    market_session = (
        "장 전" if (et_hour < 9 or (et_hour == 9 and et_minute < 30))
        else ("장 중" if et_hour < 16 else "장 후")
    )
'''

# 마감 시각을 캘린더에서 안 받고 하드코딩한 모양 — 배선은 살아 있고
# ct_from_core 만 죽어야 한다.
_HARDCODE_MS_FIXTURE = '''
def f():
    _sess_ct = "13:00"
    _sess_m = now_et_for_session.hour * 60 + now_et_for_session.minute
    if _sess_ct is None:
        market_session = "휴장"
    elif _sess_m < 570:
        market_session = "장 전"
    elif _sess_m < mcal.close_minutes(_sess_ct):
        market_session = "장 중"
    else:
        market_session = "장 후"
'''

_OLD_REMINDER_FIXTURE = '''
def main():
    if extra:
        add_reminder(sh, title="임시 휴장일 확인", what="", why="", source="")
    print("[END]")
'''


def test_label_wiring():
    print("\n── K. 세션 라벨 배선 (밴드를 다시 적지 않는가)")

    srcs = {}
    for nm in ("app.py", "run_narrative.py", "refresh_market_calendar.py"):
        srcs[nm] = _read(nm)
        check("K-0  " + nm + " 사본 탐색", srcs[nm] is not None,
              "못 찾으면 아래 검사가 전부 공허하게 통과한다")

    # ── K-1군: 라벨 함수 2개 ─────────────────────────────────────────────
    for nm, fn in _LABEL_FNS:
        if not srcs.get(nm):
            continue
        w = label_wiring(srcs[nm], fn)
        check("K-1  " + nm + ":" + fn + " 존재", w["exists"])
        check("K-2  " + nm + " 밴드 리터럴 0건 (240/570/960/1200)",
              w["no_band_literal"],
              "밴드가 되살아났다 — 복사본이 다시 생겼다는 뜻")
        check("K-3  " + nm + " narrative_session_label 위임",
              w["delegates"], "import 만 있고 배선이 없다")

    # ── K-4군: DRG 프롬프트 market_session ───────────────────────────────
    if srcs.get("app.py"):
        m = market_session_wiring(srcs["app.py"])
        check("K-4  app.py _sess_ct 를 session_close_time 에서 받는다",
              m["ct_from_core"], "마감 시각 하드코딩")
        check("K-5  app.py market_session 이 _sess_ct/_sess_m 로 갈린다",
              m["guarded_by_ct"], "옛 IfExp 한 줄 대입으로 되돌아갔다")
        check("K-6  app.py et_hour/et_minute 판정 재발 없음",
              m["no_hour_name"])
        check("K-7  app.py 휴장 분기 존재",
              m["has_closed_branch"],
              "휴장이 '장 전/장 후' 로 뭉개진다")

    # ── K-8: 반일장 불일치 배달 ──────────────────────────────────────────
    if srcs.get("refresh_market_calendar.py"):
        check("K-8  반일장 불일치 → add_reminder 배달",
              half_reminder_wiring(srcs["refresh_market_calendar.py"])["delivers"],
              "로그에만 남으면 아무도 안 본다")

    # ── 역검증 — 알려진 나쁜 입력에서 실제로 빨간불이 나는가 ────────────
    ol = label_wiring(_OLD_LABEL_FIXTURE, "_narrative_session_label_for_et_dt")
    nl = label_wiring(_NEW_LABEL_FIXTURE, "_narrative_session_label_for_et_dt")
    ll = label_wiring(_LIMP_LABEL_FIXTURE, "_narrative_session_label_for_et_dt")
    check("K-R1 변경 전 픽스처 → 밴드 리터럴 검출됨",
          ol["exists"] and not ol["no_band_literal"],
          "검출 못 함 — 이 검사는 판별력이 없다")
    check("K-R2 변경 전 픽스처 → 위임 없음으로 판정", not ol["delegates"])
    check("K-R3 변경 후 픽스처 → 둘 다 통과 (과민하지 않음)",
          nl["no_band_literal"] and nl["delegates"])
    check("K-R4 위임하면서 밴드도 남긴 픽스처 → 리터럴 검사만 실패",
          ll["delegates"] and not ll["no_band_literal"],
          "두 검사가 같은 것을 보고 있다 — 하나는 잉여다")
    check("K-R5 함수 이름이 사라지면 exists 로 잡힌다",
          not label_wiring("x = 1", "_narrative_session_label_for_et_dt")["exists"])

    om = market_session_wiring(_OLD_MS_FIXTURE)
    hm = market_session_wiring(_HARDCODE_MS_FIXTURE)
    check("K-R6 변경 전 market_session 픽스처 → 배선 검사 실패",
          not om["guarded_by_ct"] and not om["ct_from_core"])
    check("K-R7 변경 전 픽스처 → et_hour 재발로 검출",
          not om["no_hour_name"],
          "et_hour 가 판정에 있는데 통과했다")
    check("K-R8 변경 전 픽스처 → 휴장 분기 없음으로 검출",
          not om["has_closed_branch"])
    check("K-R9 마감시각만 하드코딩한 픽스처 → ct_from_core 만 실패",
          hm["guarded_by_ct"] and hm["has_closed_branch"]
          and not hm["ct_from_core"],
          "네 검사가 같은 것을 보고 있다")
    check("K-R10 변경 전 리마인더 픽스처 → 배달 없음으로 검출",
          not half_reminder_wiring(_OLD_REMINDER_FIXTURE)["delivers"])


# ══════════════════════════════════════════════════════════════════════════
# G. 뮤테이션 — 의도적 버그를 검사가 잡는가
# ══════════════════════════════════════════════════════════════════════════
def test_mutation():
    print("\n── G. 뮤테이션 (버그를 심어 검사가 잡는지 확인)")
    orig_nth, orig_last = cc._nth_weekday, cc._last_weekday
    orig_easter, orig_obs = cc.easter_sunday, cc._observed

    def golden_fails():
        """골든 검사가 실패하면 True. 캐시를 비우고 다시 계산한다."""
        cc._RULE_CACHE.clear()
        for y, gold in GOLDEN.items():
            if set(cc.nyse_regular_holidays(y)) != gold:
                return True
        return False

    muts = []

    # M1: n번째 요일 off-by-one (MLK/프레지던트/노동절/추수감사절 전멸)
    cc._nth_weekday = lambda y, m, w, n: orig_nth(y, m, w, n + 1)
    muts.append(("M1 n번째 요일 +1주", golden_fails()))
    cc._nth_weekday = orig_nth

    # M2: 마지막 요일 → 첫 요일 (메모리얼데이)
    cc._last_weekday = lambda y, m, w: orig_nth(y, m, w, 1)
    muts.append(("M2 메모리얼데이 마지막→첫째", golden_fails()))
    cc._last_weekday = orig_last

    # M3: 굿프라이데이 오프셋 -2 → -1 (부활절 토요일)
    cc.easter_sunday = lambda y: orig_easter(y) + __import__("datetime").timedelta(days=1)
    muts.append(("M3 부활절 +1일", golden_fails()))
    cc.easter_sunday = orig_easter

    # M4: 대체 휴일 규칙 무력화 (2026-07-03 이 7/04 로 남음)
    cc._observed = lambda d, is_new_year=False: d
    muts.append(("M4 대체 휴일 규칙 제거", golden_fails()))
    cc._observed = orig_obs

    cc._RULE_CACHE.clear()

    for name, caught in muts:
        check(name + " → 검출됨", caught, "뮤테이션을 골든 검사가 못 잡았다")

    # M5: 판정 계약 — extra_closed 를 무시하면 국장일이 새는가
    leaked = cc.is_market_open("2025-01-09", extra_closed=set()) is True
    check("M5 extra_closed 비었을 때 개장 (설계대로)", leaked)

    # ── 반일장 뮤테이션 — 골든 H 가 실제로 잡는가 ────────────────────────
    _orig_nth = cc._nth_weekday
    _orig_early = cc.nyse_early_close_days

    def _half_fails():
        # ⚠️ 두 캐시를 **모두** 비운다.
        #    nyse_early_close_days 는 내부에서 regular_holidays_cached 를 부른다
        #    (전휴장 충돌 가드). _EARLY_CACHE 만 비우면, 변이된 _nth_weekday 로
        #    계산된 휴일이 _RULE_CACHE 에 남아 원복 후에도 오염이 이어진다.
        #    실제로 이 원복 검사가 그 누수를 잡아냈다.
        cc._EARLY_CACHE.clear()
        cc._RULE_CACHE.clear()
        for y, gold in GOLDEN_HALF.items():
            if set(cc.nyse_early_close_days(y)) != gold:
                return True
        return False

    # H-M1: 추수감사절 다음날을 +2일로 (금요일 → 토요일)
    cc._nth_weekday = lambda y, m, w, n: _orig_nth(y, m, w, n) + timedelta(days=1)
    check("H-M1 추수감사절 +1일 → 검출됨", _half_fails(),
          "반일장 골든이 추수감사절 이동을 못 잡았다")
    cc._nth_weekday = _orig_nth
    cc._EARLY_CACHE.clear()
    cc._RULE_CACHE.clear()

    # ⚠️ 여기서 배운 것 — 삼중 중복은 단일 지점 뮤테이션으로 판별할 수 없다
    # ────────────────────────────────────────────────────────────────────
    # nyse_early_close_days 의 방어는 셋이다:
    #   (a) 요일 조건 — 다음날이 화~금
    #   (b) 주말 가드 — 결과가 토·일이면 제외
    #   (c) 휴일 가드 — 결과가 전휴장이면 제외
    #
    # 셋이 **상호 중복**이다. 실측 결과 골든 7년 기준:
    #   (a)만 제거 → 일치   (b)만 제거 → 일치   (c)만 제거 → 일치
    #   (a)+(c) 제거 → 불일치 [2020, 2021, 2026]
    #   셋 다 제거   → 불일치 [2020, 2021, 2022, 2023, 2026]
    #
    # 처음엔 (a) 하나만 넓히는 뮤턴트를 썼는데 검출되지 않았다. 골든이 부실한
    # 게 아니라 **뮤턴트가 판별력이 없었다** — (b)(c)가 덮어버린다.
    # 그래서 2개 이상을 동시에 제거하는 뮤턴트를 쓴다.
    def _mk_broken(weekday_cond, guard_weekend, guard_holiday):
        def _f(year):
            out = {}
            tg = _orig_nth(year, 11, 3, 4)
            out[(tg + timedelta(days=1)).isoformat()] = cc.EARLY_CLOSE_TIME
            for mm, dd_ in ((7, 3), (12, 24)):
                nxt = date(year, mm, dd_) + timedelta(days=1)
                if (not weekday_cond) or nxt.weekday() in (1, 2, 3, 4):
                    out[date(year, mm, dd_).isoformat()] = cc.EARLY_CLOSE_TIME
            hol = cc.nyse_regular_holidays(year)
            res = {}
            for ds, t in out.items():
                if guard_holiday and ds in hol:
                    continue
                if guard_weekend and date.fromisoformat(ds).weekday() >= 5:
                    continue
                res[ds] = t
            return res
        return _f

    cc.nyse_early_close_days = _mk_broken(False, True, False)
    check("H-M2 요일조건 + 휴일가드 동시 제거 → 검출됨", _half_fails(),
          "2020·2021·2026 이 어긋나야 한다")
    cc.nyse_early_close_days = _orig_early
    cc._EARLY_CACHE.clear()
    cc._RULE_CACHE.clear()

    cc.nyse_early_close_days = _mk_broken(False, False, False)
    check("H-M3 세 방어 전부 제거 → 검출됨", _half_fails())
    cc.nyse_early_close_days = _orig_early
    cc._EARLY_CACHE.clear()
    cc._RULE_CACHE.clear()

    # 중복성 자체를 기록해 둔다 — 이게 '검출 실패'가 아니라 '설계'임을 명시.
    # 미래에 누구든 "요일 조건이 중복이니 지우자" 고 생각할 수 있는데, 하나만
    # 남기면 그 하나가 틀렸을 때 잡을 게 없어진다.
    _single = [cc.nyse_early_close_days]
    for nm, args_ in (("요일조건만 제거", (False, True, True)),
                      ("주말가드만 제거", (True, False, True)),
                      ("휴일가드만 제거", (True, True, False))):
        cc.nyse_early_close_days = _mk_broken(*args_)
        _same = not _half_fails()
        cc.nyse_early_close_days = _orig_early
        cc._EARLY_CACHE.clear()
        cc._RULE_CACHE.clear()
        check("H-R " + nm + " → 골든 여전히 통과 (삼중 중복, 설계대로)", _same,
              "중복이 깨졌다면 방어 하나가 실제로 사라진 것이다 — 확인 필요")

    # ── J절 뮤테이션 — 세션 구간이 반일장을 실제로 반영하는가 ──────────
    # session_phase 는 session_close_time 을 통해서만 반일장을 안다. 그 연결이
    # 끊기면(마감을 늘 960 으로 보면) J-C 군이 전부 빨간불이어야 한다.
    _orig_close = cc.session_close_time

    def _sp_fails():
        """반일장 13:30 이 post 가 아니면 True."""
        return cc.session_phase(datetime(2026, 11, 27, 13, 30)) != "post"

    # ⚠️ muts.append 를 쓰지 말 것 — 소비 루프(`for name, caught in muts`)가
    #    **이 지점보다 위에** 있다. 여기서 append 하면 항목이 영원히 소비되지
    #    않고, 실패 수에는 아무 변화가 없어 **초록불 속에서 죽은 게이트**가
    #    된다(초안에서 실제로 그렇게 썼다가 잡았다). check() 를 직접 부른다.
    cc.session_close_time = lambda *a, **k: cc.REGULAR_CLOSE_TIME
    check("J-M1 마감을 항상 16:00 으로 (반일장 무력화) → 검출됨", _sp_fails(),
          "session_phase 가 반일장을 안 본다")
    cc.session_close_time = _orig_close

    _orig_cm = cc.close_minutes
    cc.close_minutes = lambda t: 960
    check("J-M2 close_minutes 를 960 하드코딩 → 검출됨", _sp_fails(),
          "마감 분 계산을 우회해도 통과한다")
    cc.close_minutes = _orig_cm

    # 원복 확인 — 뮤테이션 뒤에도 골든이 통과해야 한다
    check("세션 구간 뮤테이션 원복 후 정상", not _sp_fails())
    check("반일장 뮤테이션 원복 후 골든 정상", not _half_fails())
    check("뮤테이션 원복 후 골든 정상", not golden_fails())


# ══════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 70)
    print("calendar_core 회귀 검증 — v" + cc.CALENDAR_CORE_VERSION)
    print("  네트워크·시트 접근 없음 · 부작용 없음")
    print("=" * 70)

    test_golden()
    test_observed()
    test_easter()
    test_contract()
    test_parse()
    test_diff()
    test_halfday()
    test_wiring()
    test_session_phase()
    test_label_wiring()
    test_mutation()

    print("")
    print("=" * 70)
    print("결과: 통과 " + str(len(_PASS)) + " · 실패 " + str(len(_FAIL)))
    if _FAIL:
        print("")
        print("❌ 실패 항목:")
        for f in _FAIL:
            print("   · " + f)
        print("")
        print("⚠️ 배포하지 말 것. 휴장일 오판은 로그에 에러를 남기지 않는다.")
    else:
        print("✅ 전 항목 통과 — 배포 가능")
    print("=" * 70)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
