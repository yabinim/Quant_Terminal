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

print("\n[10] BMO/AMC 추론 — 과거 거래량 패턴 역산")


def mk_hist(dates, vols):
    return pd.DataFrame({"Close": [100.0] * len(dates), "Volume": vols},
                        index=pd.DatetimeIndex(dates))


# 거래일 40개 (주말 무시 — 인덱스 위치만 쓴다)
_days = pd.bdate_range("2026-01-05", periods=40)
_ev_dates = [_days[10], _days[20], _days[30]]


def _vols(spike_offset):
    """실적일 + offset 세션에 거래량 5배."""
    v = [1_000_000] * len(_days)
    for d in _ev_dates:
        i = list(_days).index(d) + spike_offset
        v[i] = 5_000_000
    return v


_events = [{"date": d.strftime("%Y-%m-%d")} for d in reversed(_ev_dates)]

r_bmo = ec.infer_timing(mk_hist(_days, _vols(0)), _events)
chk(r_bmo["timing"] == "bmo" and r_bmo["ok"],
    f"발표일 당일 급증 → {r_bmo['timing']} (표 {r_bmo['votes']})")

r_amc = ec.infer_timing(mk_hist(_days, _vols(1)), _events)
chk(r_amc["timing"] == "amc" and r_amc["ok"],
    f"다음날 급증 → {r_amc['timing']} (표 {r_amc['votes']})")

print("\n[11] 기권 규칙 — 틀린 timing 은 미상보다 나쁘다")
_flat = mk_hist(_days, [1_000_000] * len(_days))
r_flat = ec.infer_timing(_flat, _events)
chk(r_flat["timing"] == "" and not r_flat["ok"],
    f"거래량 차이 없음 → 판정 보류 (유효표 {r_flat['n']})")

r_few = ec.infer_timing(mk_hist(_days, _vols(0)), _events[:2])
chk(r_few["timing"] == "" and r_few["n"] == 2,
    f"표본 2분기(<{ec.TIMING_INFER_MIN_VOTES}) → 보류")

# 3:2 로 갈리면 우세 비율 60% < 70% → 보류
_mixed_days = pd.bdate_range("2026-01-05", periods=70)
_mev = [_mixed_days[i] for i in (10, 20, 30, 40, 50)]
_mv = [1_000_000] * len(_mixed_days)
for i, d in enumerate(_mev):
    _mv[list(_mixed_days).index(d) + (0 if i < 3 else 1)] = 5_000_000
r_mix = ec.infer_timing(mk_hist(_mixed_days, _mv),
                        [{"date": d.strftime("%Y-%m-%d")} for d in reversed(_mev)])
chk(r_mix["timing"] == "" and r_mix["ratio"] == 0.6,
    f"3:2 분열(60% < 70%) → 보류 (표 {r_mix['votes']}, 비율 {r_mix['ratio']})")

print("\n[12] 빈 입력 안전")
chk(ec.infer_timing(None, _events)["timing"] == "", "hist None")
chk(ec.infer_timing(mk_hist(_days, _vols(0)), [])["timing"] == "", "events 없음")
chk(ec.infer_timing(pd.DataFrame({"Close": []}), _events)["timing"] == "",
    "Volume 열 없음")

print("\n[13] 추정 라벨 — 확정 사실로 표시하지 않는다")
chk(ec.TIMING_LABELS_INFERRED["bmo"].endswith("(추정)"), ec.TIMING_LABELS_INFERRED["bmo"])
chk(ec.TIMING_LABELS_INFERRED["amc"].endswith("(추정)"), ec.TIMING_LABELS_INFERRED["amc"])
chk(ec.TIMING_LABELS_INFERRED[""] == "시각 미상", "미상은 그대로")
_app = open("app.py", encoding="utf-8").read()
chk("TIMING_LABELS_INFERRED" in _app and "ec.TIMING_LABELS.get" not in _app,
    "app.py 가 추정 라벨만 사용")
chk("시점은 추정입니다" in _app, "지형표 캡션에 근거 명시")

print("\n[14] 자동화 추론 배선")
_rw = open("run_earnings_watch.py", encoding="utf-8").read()
chk("ec.infer_timing(hist, past)" in _rw, "move 계산 자리에서 추론(콜 0)")
chk('not str(ev.get("timing") or "")' in _rw, "FMP 가 준 timing 이 있으면 덮어쓰지 않음")
chk("추론 {n_infer}" in _rw, "[CAL] 로그에 추론 건수")

print("\n[15] 게이트 회귀 — force 가 needs_move 도 우회하는가")
_rw2 = open("run_earnings_watch.py", encoding="utf-8").read()
_blk = _rw2[_rw2.index("            move = None"):_rw2.index("            _src = (ec.SOURCE_USER")]
chk("(force or ec.needs_move(" in _blk,
    "블록 진입 조건이 force 를 포함 (변동폭 캐시가 추론을 막지 않음)")
chk("_do_move = force or ec.needs_move(" in _blk, "변동폭 재계산은 별도 판정")
chk(_blk.index("infer_timing") > _blk.index("past = ec.past_earnings_dates"),
    "추론이 past 로드 이후에 위치")
chk("if move is not None:" in _blk, "[MOVE] 로그가 None 가드 뒤에 있음")
chk("_need_timing" not in _rw2,
    "매 실행 재시도 루프를 만드는 별도 조건이 없음 (분기 내 재추론 무의미)")

fh.fmp_get_ex = _orig
print("\n" + "=" * 52)
print(f"❌ 실패 {len(fail)}건" if fail else "✅ 전부 통과")
sys.exit(1 if fail else 0)
