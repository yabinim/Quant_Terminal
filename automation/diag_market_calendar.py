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
  G. 뮤테이션     — 규칙 엔진에 의도적 버그를 심어 검사가 **잡아내는지** 확인

G 가 핵심이다. 통과만 하는 테스트는 통과만 하는 코드를 보증하지 않는다.
"""
import os
import sys
from datetime import date, timedelta   # timedelta: 반일장 뮤테이션에서 사용

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

    # 원복 확인 — 뮤테이션 뒤에도 골든이 통과해야 한다
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
