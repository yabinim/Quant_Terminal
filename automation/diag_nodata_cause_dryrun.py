"""diag_nodata_cause_dryrun.py — diag_nodata_cause.py 오프라인 사전 검증.

프로브가 **콜을 태우다가 중간에 터지는 것**이 최악이다. 실행하려면 FMP 키가
필요하고 재실행에 또 콜이 든다. 그래서 네트워크 없이 가짜 응답을 물려
모든 분기를 미리 밟아본다.

시나리오 5종:
  S1 정상      — 필터 동작, 필드 정상
  S2 무시됨    — 필터가 조용히 무시되는 경우 (가장 위험한 함정)
  S3 시드없음  — P1/P2 가 죽어 후속이 전부 스킵
  S4 필드결손  — profile 에 isActivelyTrading 없음
  S5 이상값    — 날짜 None/깨짐, 정렬 불규칙, 빈 배열 혼재
"""
import importlib
import io
import os
import sys
import contextlib

os.environ["FMP_API_KEY"] = "DUMMYKEY"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import diag_nodata_cause as P  # noqa: E402


def sc_feed(n=20, start=0):
    return [{"date": f"2026-{((i % 8) + 1):02d}-{((i % 27) + 1):02d}",
             "oldSymbol": f"OLD{i}", "newSymbol": f"NEW{i}",
             "companyName": f"Co {i}"} for i in range(start, start + n)]


def dl_feed(n=20, base_month=8, day0=19, start=0):
    return [{"symbol": f"DEAD{i}", "companyName": f"Gone {i}",
             "delistedDate": f"2026-{base_month:02d}-{max(1, day0 - (i % 27)):02d}",
             "exchange": "NASDAQ"} for i in range(start, start + n)]


SCEN = {}

SCEN["S1 정상"] = {
    "symbol-change?limit=20": ("OK", sc_feed(), "20건"),
    "delisted-companies?page=0&limit=20": ("OK", dl_feed(), "20건"),
    "symbol-change?symbol=OLD0": ("OK", [sc_feed()[0]], "1건"),
    "__from_to__": ("OK", [{"date": "2026-07-01", "oldSymbol": "A", "newSymbol": "B"}], "1건"),
    "delisted-companies?symbol=DEAD0": ("OK", [dl_feed()[0]], "1건"),
    "delisted-companies?page=5&limit=20": ("OK", dl_feed(20, 5, 28, start=200), "20건"),
    "__profile_dead__": ("OK", [{"symbol": "DEAD0", "companyName": "Gone 0",
                                 "isActivelyTrading": False}], "1건"),
    "__profile_old__": ("OK", [{"symbol": "OLD0", "companyName": "Co 0",
                                "isActivelyTrading": False}], "1건"),
    "__hist_old__": ("EMPTY", [], "빈 배열"),
    "__hist_dead__": ("EMPTY", [], "빈 배열"),
    "__search__": ("OK", [{"symbol": "NEW0"}], "1건"),
}

# 2026-08-21 실측에서 실제로 나온 형태를 재현한다.
# 필터 호출은 limit 이 없어 **기본 100건**이 오고 요청 심볼은 들어 있지도 않다.
# 초판 판별자(건수 비교)는 20 != 100 이라 이걸 놓쳤다 — 회귀로 고정한다.
SCEN["S2 필터무시"] = dict(SCEN["S1 정상"])
SCEN["S2 필터무시"].update({
    # ⚠️ 핵심: 요청 심볼(OLD0/DEAD0)이 결과에 **없다.** 실측이 그랬다
    #    (USGX 를 요청했는데 AAUM·ADIG.V·AGGI… 가 왔다).
    "symbol-change?symbol=OLD0": ("OK", sc_feed(100, start=500), "100건"),
    "delisted-companies?symbol=DEAD0": ("OK", dl_feed(100, start=500), "100건"),
    "__from_to__": ("OK", sc_feed(100, start=500), "100건"),
})

SCEN["S3 시드없음"] = {
    "symbol-change?limit=20": ("PLAN", None, "플랜 미포함"),
    "delisted-companies?page=0&limit=20": ("EMPTY", [], "빈 배열"),
    "__from_to__": ("404", None, "경로 없음"),
    "delisted-companies?page=5&limit=20": ("EMPTY", [], "빈 배열"),
}

SCEN["S4 필드결손"] = dict(SCEN["S1 정상"])
SCEN["S4 필드결손"].update({
    "__profile_dead__": ("OK", [{"symbol": "DEAD0", "companyName": "Gone 0"}], "1건"),
    "__profile_old__": ("EMPTY", [], "빈 배열"),
    "__hist_old__": ("OK", [{"date": "2026-08-19", "close": 1.0}] * 250, "250건"),
})

SCEN["S5 이상값"] = {
    "symbol-change?limit=20": ("OK", [{"oldSymbol": "X", "newSymbol": "Y"},
                                      {"date": None, "oldSymbol": "P", "newSymbol": "Q"},
                                      {"date": "bad", "oldSymbol": "M", "newSymbol": "N"}], "3건"),
    "delisted-companies?page=0&limit=20": ("OK", [{"symbol": "Z"}], "1건"),
    "symbol-change?symbol=X": ("EXC", None, "Timeout"),
    "__from_to__": ("EMPTY", [], "빈 배열"),
    "delisted-companies?symbol=Z": ("ERRMSG", None, "Error Message: bad param"),
    "delisted-companies?page=5&limit=20": ("OK", [{"symbol": "W", "delistedDate": "2027-01-01"}], "1건"),
    "__profile_dead__": ("ODD", None, "예상 밖 타입"),
    "__profile_old__": ("RATE", None, "레이트리밋"),
    "__hist_old__": ("HTTP", None, "HTTP 500"),
    "__hist_dead__": ("NOJSON", None, "본문 앞: <html>"),
    "__search__": ("EMPTY", [], "빈 배열"),
}


def make_fake(table):
    def fake_call(path, keep=False):
        P._CALLS += 1
        if path in table:
            return table[path]
        if path.startswith("symbol-change?from="):
            return table.get("__from_to__", ("EMPTY", [], "빈 배열"))
        if path.startswith("profile?symbol="):
            sym = path.split("=", 1)[1]
            key = "__profile_dead__" if sym.startswith("DEAD") or sym == "Z" else "__profile_old__"
            return table.get(key, ("EMPTY", [], "빈 배열"))
        if path.startswith("historical-price-eod/full?symbol="):
            sym = path.split("=", 1)[1].split("&", 1)[0]
            key = "__hist_dead__" if sym.startswith("DEAD") or sym == "Z" else "__hist_old__"
            return table.get(key, ("EMPTY", [], "빈 배열"))
        if path.startswith("search-symbol?"):
            return table.get("__search__", ("EMPTY", [], "빈 배열"))
        if path.startswith("actively-trading-list"):
            return ("OK", [{"symbol": f"T{i}"} for i in range(100)], "100건")
        return ("EMPTY", [], "미정의 경로 — 빈 배열")
    return fake_call


fails = []
for name, table in SCEN.items():
    for heavy in (False, True):
        P._CALLS = 0
        P.FIND = {}
        P._HEAVY = heavy
        P.call = make_fake(table)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = P.main()
            status = f"✅ 정상종료(rc={rc}) · {P._CALLS}콜"
        except Exception as e:
            status = f"❌ 예외 {type(e).__name__}: {e}"
            fails.append((name, heavy, e))
        print(f"{name:12} HEAVY={'ON ' if heavy else 'OFF'}  {status}")
        if fails and fails[-1][0] == name and fails[-1][1] == heavy:
            print(buf.getvalue()[-800:])

print()
if fails:
    print(f"❌ {len(fails)}개 시나리오에서 예외 — 프로브가 콜을 태우다 죽는다")
    sys.exit(1)
print("✅ 5개 시나리오 × HEAVY 2종 = 10회 전부 예외 없이 완주")

# 함정 탐지가 실제로 되는지 확인
P._CALLS = 0
P.FIND = {}
P._HEAVY = False
P.call = make_fake(SCEN["S2 필터무시"])
with contextlib.redirect_stdout(io.StringIO()):
    P.main()
ok = (P.FIND.get("sc_symbol_filter") == "IGNORED"
      and P.FIND.get("dl_symbol_filter") == "IGNORED"
      and P.FIND.get("sc_date_filter") == "IGNORED")
print(("✅" if ok else "❌") + " 파라미터 무시 함정 탐지 — "
      + repr({k: P.FIND.get(k) for k in
              ("sc_symbol_filter", "dl_symbol_filter", "sc_date_filter")}))
if not ok:
    sys.exit(1)

# 정상 시나리오에서 페이지 깊이 산출이 되는지
P._CALLS = 0
P.FIND = {}
P.call = make_fake(SCEN["S1 정상"])
with contextlib.redirect_stdout(io.StringIO()):
    P.main()
print(f"✅ 정상 시나리오 판정: sc_filter={P.FIND.get('sc_symbol_filter')} "
      f"· 1년커버={P.FIND.get('dl_pages_for_1y')}페이지 "
      f"· profile상폐={P.FIND.get('profile_delisted')!r} "
      f"· 구티커이력={P.FIND.get('hist_old')}")
