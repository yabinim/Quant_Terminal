"""diag_earnings_timing.py — BMO/AMC 판정 + 시장 전체 캘린더 맵 회귀 검사.

배경 (2026-08-14): Earnings_Calendar 264행이 전부 `Timing` 공란,
`Date_Source` 전부 `estimated` 였다. 원인 세 가지:

  1) per-symbol earnings?symbol= 응답에 time 필드가 없다
  2) quote.earningsAnnouncement 는 UTC 인데 ET 개장 기준(hh<12)으로 판정했다
     → 장전 08:30 ET = 12:30 UTC → hh=12 → 'amc' 로 뒤집힘
  3) 확정 규약을 (earnings, quote) 로 좁혔는데 quote 가 stable 에서 이 필드를
     주지 않아 agree 가 2에 영영 도달 못 함

가장 위험한 버그: BMO 를 AMC 로 오판하는 것. resolve_reaction_index 가 반응일을
하루 밀려 잡아 갭·PEAD 측정이 통째로 어긋난다. [1] 이 그것을 잡는다.
"""
import sys

import pandas as pd

import earnings_core as ec
import fmp_http as fh

fail = []


def chk(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        fail.append(msg)


class R:
    def __init__(self, d, c=200):
        self._d, self.status_code = d, c

    def json(self):
        return self._d


_orig = fh.fmp_get_ex
TODAY = pd.Timestamp("2026-08-14")

print("[1] UTC → ET 변환 (BMO/AMC 오판 방지)")
for v, exp, desc in [
    ("2026-08-20T12:30:00.000+0000", "bmo", "08:30 ET 장전 — 구버전은 amc 로 오판"),
    ("2026-08-20T13:29:00.000+0000", "bmo", "09:29 ET 개장 직전"),
    ("2026-08-20T13:30:00.000+0000", "amc", "09:30 ET 개장 이후"),
    ("2026-08-20T20:30:00.000+0000", "amc", "16:30 ET 장후"),
    ("2026-08-20T21:05:00.000+0000", "amc", "17:05 ET"),
    ("2026-08-20", "", "날짜만 → 판정 불가"),
    ("2026-08-20T00:00:00.000+0000", "", "자정 정각 → 판정 불가"),
    ("", "", "빈 값"),
    (None, "", "None"),
]:
    got = ec._timing_from_utc(v)
    chk(got == exp, f"{str(v)[:30]:31} → {got!r:6} {desc}")

print("\n[2] ET 라벨 파싱 (_timing_of — earnings-calendar 의 time 필드)")
for item, exp in [({"time": "bmo"}, "bmo"), ({"time": "amc"}, "amc"),
                  ({"time": "08:30"}, "bmo"), ({"time": "16:30"}, "amc"),
                  ({"when": "Before Market Open"}, "bmo"),
                  ({"when": "After Market Close"}, "amc"),
                  ({}, ""), ({"time": ""}, "")]:
    got = ec._timing_of(item)
    chk(got == exp, f"{str(item)[:34]:35} → {got!r}")

print("\n[3] 시장 전체 캘린더 맵")
CAL = [
    {"symbol": "WMT", "date": "2026-08-20", "time": "bmo"},
    {"symbol": "HD", "date": "2026-08-18", "time": "06:00"},
    {"symbol": "TJX", "date": "2026-08-19", "time": "amc"},
    {"symbol": "NODATE", "date": "2026-08-19"},
    {"symbol": "WMT", "date": "2026-11-20", "time": "amc"},   # 나중 분기 → 무시
    {"symbol": "", "date": "2026-08-19"},                     # 빈 티커
    {"symbol": "PAST", "date": "2026-08-01"},                 # 창 밖(과거)
    {"symbol": "FAR", "date": "2026-12-01"},                  # 창 밖(미래)
]
fh.fmp_get_ex = lambda url, timeout=None, retries=None: (
    (R(CAL), 200, "ok") if "earnings-calendar" in url else (None, 404, "http_error"))
m, diag = ec.fetch_market_calendar_map(today=TODAY, key="K")
chk(sorted(m) == ["HD", "NODATE", "TJX", "WMT"], f"맵 = {sorted(m)}")
chk(m["WMT"]["date"] == "2026-08-20", f"같은 티커 중복 → 이른 날짜 = {m['WMT']['date']}")
chk(m["WMT"]["timing"] == "bmo" and m["TJX"]["timing"] == "amc", "ET 라벨 파싱")
chk(m["HD"]["timing"] == "bmo", f"06:00 → {m['HD']['timing']}")
chk(m["NODATE"]["timing"] == "", "time 없는 항목 → 빈 timing (행은 유지)")
chk("PAST" not in m and "FAR" not in m, "창 밖 제외")
chk("timing 3" in diag, f"진단 문자열 = {diag}")

print("\n[4] 경량 조회 — 맵 적중 시 FMP 콜 0")
_calls = []


def _count(url, timeout=None, retries=None):
    _calls.append(url)
    return None, 404, "http_error"


fh.fmp_get_ex = _count
ev = ec.fetch_next_earnings_light("WMT", today=TODAY, key="K", market_map=m)
chk(ev is not None and ev["earnings_date"] == "2026-08-20", f"맵에서 날짜 = {ev and ev['earnings_date']}")
chk(ev and ev["timing"] == "bmo", f"맵에서 timing = {ev and ev['timing']}")
chk(ev and ev["days_until"] == 6, f"D-Day = {ev and ev['days_until']}")
chk(len(_calls) == 0, f"FMP 콜 {len(_calls)}회 (맵 적중 시 0이어야 함)")

print("\n[5] 맵에 없으면 폴백 (14일 창이라 '없음'≠'실적 없음')")
_calls.clear()
fh.fmp_get_ex = lambda url, timeout=None, retries=None: (
    (R([{"date": "2026-10-05", "epsEstimated": 3.1}]), 200, "ok")
    if "earnings?symbol" in url else (None, 404, "http_error"))
ev2 = ec.fetch_next_earnings_light("UNSEEN", today=TODAY, key="K", market_map=m)
chk(ev2 is not None and ev2["earnings_date"] == "2026-10-05",
    f"폴백 조회 = {ev2 and ev2['earnings_date']}")

print("\n[6] 정밀 조회 — 맵이 확정 판정의 두 번째 소스")


def _full(url, timeout=None, retries=None):
    if "earnings?symbol" in url:
        return R([{"date": "2026-08-20", "epsEstimated": 1.55}]), 200, "ok"
    return None, 404, "http_error"      # quote 는 응답 없음(stable 실측 가정)


fh.fmp_get_ex = _full
full = ec.fetch_next_earnings("WMT", today=TODAY, key="K", market_map=m)
chk(full is not None, "정밀 조회 성공")
chk(full and full["date_source"] == "confirmed",
    f"date_source = {full and full['date_source']} (earnings+calendar 일치)")
chk(full and full["timing"] == "bmo", f"timing = {full and full['timing']} (맵에서 공급)")
chk(full and full["eps_estimate"] == 1.55, f"EPS = {full and full['eps_estimate']}")

print("\n[7] 맵 없이는 confirmed 불가 (quote 미응답 시 = 수정 전 상태)")
full2 = ec.fetch_next_earnings("WMT", today=TODAY, key="K", market_map=None)
chk(full2 and full2["date_source"] == "estimated",
    f"맵 없음 → {full2 and full2['date_source']} (단일 소스)")
chk(full2 and full2["timing"] == "", "맵 없음 → timing 공란 (버그 재현)")

print("\n[8] 날짜 불일치 시 confirmed 아님")
m_bad = {"WMT": {"date": "2026-08-25", "timing": "amc"}}
full3 = ec.fetch_next_earnings("WMT", today=TODAY, key="K", market_map=m_bad)
chk(full3 and full3["date_source"] == "estimated",
    f"소스 불일치 → {full3 and full3['date_source']}")
chk(full3 and full3["conflict"], "conflict 플래그")
chk(full3 and full3["earnings_date"] == "2026-08-20", "이른 날짜 채택")

print("\n[9] 자동화 배선")
src = open("run_earnings_watch.py", encoding="utf-8").read()
chk("fetch_market_calendar_map" in src, "main 에서 맵 1회 조회")
chk("market_map=market_map" in src, "pass_calendar 로 전달")
chk("맵적중" in src and "timing확보" in src, "[CAL] 로그에 커버리지 지표")

fh.fmp_get_ex = _orig
print("\n" + "=" * 52)
print(f"❌ 실패 {len(fail)}건" if fail else "✅ 전부 통과")
sys.exit(1 if fail else 0)
