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
    # ⚠️ 2026-08-21 실측 형태: 100건 · 심볼 100종 · **시드도 그 안에 있다.**
    #    시드를 같은 피드에서 뽑았으니 포함은 구조적으로 보장된다 —
    #    그래서 '시드 포함 여부' 판별자는 이걸 놓쳤다(2차 오판). 회귀로 고정한다.
    #    판별력이 있는 건 '심볼이 몇 종인가' 하나뿐이다.
    "symbol-change?symbol=OLD0": ("OK", sc_feed(100), "100건"),
    "delisted-companies?symbol=DEAD0": ("OK", dl_feed(100), "100건"),
    "__from_to__": ("OK", sc_feed(100), "100건"),
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


# ══════════════════════════════════════════════════════════════════════════
# 멤버십 모드 (PROBE_MODE=membership) 사전 검증
# ══════════════════════════════════════════════════════════════════════════
# 여기서 가장 중요한 건 M2 다. **알려진 불량 입력**(지상진실 사망인데 리스트에
# 존재)을 물렸을 때 판정이 '폐기'로 뒤집히지 않으면, 나머지 통과는 전부 의미가
# 없다. 통과만 확인하는 스위트는 이 프로젝트에서 다섯 번 틀렸다.
_ETF = ["SPY", "QQQ", "IWM", "XLK", "SMH"]
_FX = ["005930.KS", "7203.T", "NB2.F"]


def atl(symbols, as_dict=True):
    if as_dict:
        return [{"symbol": s, "companyName": f"Co {s}", "exchange": "NASDAQ"}
                for s in symbols]
    return list(symbols)


def dl20(sym="DEADX"):
    return [{"symbol": sym, "companyName": "Gone", "delistedDate": "2026-08-01",
             "exchange": "NASDAQ"}]


MEM = {}

MEM["M1 채택"] = {
    "atl": ("OK", atl(["AAPL"] + _ETF + _FX + [f"T{i}" for i in range(50)]), "58건"),
    "dl": ("OK", dl20(), "1건"),
    "profile:AAPL": ("OK", [{"symbol": "AAPL", "companyName": "Apple", "isActivelyTrading": True}], "1건"),
    "profile:SPY": ("OK", [{"symbol": "SPY", "companyName": "SPDR", "isActivelyTrading": True}], "1건"),
    "profile:NB2.F": ("OK", [{"symbol": "NB2.F", "companyName": "Foreign", "isActivelyTrading": True}], "1건"),
    "profile:DEADX": ("OK", [{"symbol": "DEADX", "companyName": "Gone", "isActivelyTrading": False}], "1건"),
}

# ★ 핵심 회귀 — 죽은 티커가 '거래 중' 목록에 들어 있는 세계.
#   이 경우 리스트는 생사 판별자가 아니다. 반드시 '폐기'가 나와야 한다.
MEM["M2 사망포함→폐기"] = dict(MEM["M1 채택"])
MEM["M2 사망포함→폐기"]["atl"] = (
    "OK", atl(["AAPL", "DEADX"] + _ETF + _FX), "10건")

MEM["M3 ETF해외전멸→부분"] = dict(MEM["M1 채택"])
MEM["M3 ETF해외전멸→부분"]["atl"] = (
    "OK", atl(["AAPL"] + [f"T{i}" for i in range(50)]), "51건")
MEM["M3 ETF해외전멸→부분"]["profile:SPY"] = ("PLAN", None, "플랜 미포함")
MEM["M3 ETF해외전멸→부분"]["profile:NB2.F"] = ("PLAN", None, "플랜 미포함")

MEM["M4 리스트미수신"] = {"atl": ("PLAN", None, "플랜 미포함")}

MEM["M5 이상값"] = {
    "atl": ("OK", atl(["AAPL", "SPY"], as_dict=False), "2건"),   # 원소가 문자열
    "dl": ("EMPTY", [], "빈 배열"),                              # 시드 실패
    "profile:AAPL": ("ODD", None, "예상 밖 타입"),
    "profile:SPY": ("OK", [{"symbol": "SPY"}], "1건"),           # 필드 결손
    "profile:NB2.F": ("RATE", None, "레이트리밋"),
}

MEM["M6 음성대조실패"] = dict(MEM["M1 채택"])
MEM["M6 음성대조실패"]["atl"] = (
    "OK", atl(["AAPL", "ZZZZQQ9"] + _ETF + _FX), "10건")


def make_fake_mem(table):
    def fake_call(path, keep=False):
        P._CALLS += 1
        if path.startswith("actively-trading-list"):
            return table.get("atl", ("EMPTY", [], "빈 배열"))
        if path.startswith("delisted-companies"):
            return table.get("dl", ("EMPTY", [], "빈 배열"))
        if path.startswith("profile?symbol="):
            sym = path.split("=", 1)[1]
            return table.get("profile:" + sym, ("EMPTY", [], "빈 배열"))
        return ("EMPTY", [], "미정의 경로 — 빈 배열")
    return fake_call


print()
print("── 멤버십 모드 (PROBE_MODE=membership) ──")
mem_fails = []
mem_verdict = {}
for name, table in MEM.items():
    P._CALLS = 0
    P.FIND = {}
    P._MODE = "membership"
    P.call = make_fake_mem(table)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = P.main()
        v = P.FIND.get("mem_verdict", "(없음)")
        mem_verdict[name] = (v, P._CALLS)
        print(f"{name:18} ✅ rc={rc} · {P._CALLS}콜 · {v}")
    except Exception as e:
        mem_fails.append((name, e))
        print(f"{name:18} ❌ 예외 {type(e).__name__}: {e}")
        print(buf.getvalue()[-800:])

if mem_fails:
    print(f"❌ {len(mem_fails)}개 멤버십 시나리오에서 예외")
    sys.exit(1)

# 판별력 검사 — 통과만으로는 부족하다. 불량 입력에서 뒤집히는지 본다.
checks = [
    ("M1 채택", lambda v: v.startswith("채택")),
    ("M2 사망포함→폐기", lambda v: v.startswith("폐기")),
    ("M3 ETF해외전멸→부분", lambda v: v.startswith("부분 채택")),
    ("M4 리스트미수신", lambda v: v.startswith("판정불가")),
    ("M6 음성대조실패", lambda v: v.startswith("폐기")),
]
bad = [n for n, fn in checks if not fn(mem_verdict.get(n, ("",))[0])]

# M4 는 리스트를 못 받은 순간 잔여 콜을 쓰지 않아야 한다(재실행 비용 절약).
if mem_verdict.get("M4 리스트미수신", ("", 99))[1] != 1:
    bad.append("M4 조기중단(1콜)")

print()
print(("✅" if not bad else "❌") + " 판별력 검사 — "
      + repr({n: mem_verdict.get(n, ("(없음)",))[0][:34] for n, _ in checks}))
if bad:
    print("❌ 판별에 실패한 케이스: " + ", ".join(bad))
    sys.exit(1)
print("✅ 멤버십 6시나리오 — 사망포함·음성대조 두 불량 입력에서 정상적으로 뒤집힘")


# ══════════════════════════════════════════════════════════════════════════
# 멤버십 모드 2 (PROBE_MODE=membership2) 사전 검증
# ══════════════════════════════════════════════════════════════════════════
# 1차의 실패는 "불량입력이 없는데 통과처럼 보인 것" 이었다. 그래서 여기서 가장
# 중요한 케이스는 N3 다 — 후보가 전부 생존이라 **불량입력을 하나도 못 구한**
# 세계에서 판정이 '판정불가'로 떨어져야 한다. '채택'이 나오면 1차 실패의 재발이다.


def atl2(symbols):
    return [{"symbol": s, "name": f"Co {s}"} for s in symbols]


def scfeed(olds):
    return [{"date": "2026-08-01", "oldSymbol": o, "newSymbol": o + "N"} for o in olds]


MEM2 = {}

# 깨끗한 미국 표기(r1) 사망 티커 DEAD 가 리스트에 없다 → 강한 불량입력 확인
MEM2["N1 강한불량→채택"] = {
    "atl": ("OK", atl2(["AAPL", "MSFT"] + [f"T{i}" for i in range(40)]), "42건"),
    "dl": ("OK", atl2(["DEAD", "ADWPF", "AAA.KS"]), "3건"),
    "sc": ("OK", scfeed(["OLDX"]), "1건"),
    "profile:DEAD": ("OK", [{"symbol": "DEAD", "name": "Gone", "isActivelyTrading": False}], "1건"),
    "profile:OLDX": ("EMPTY", [], "빈 배열"),
    "profile:ADWPF": ("OK", [{"symbol": "ADWPF", "isActivelyTrading": True}], "1건"),
}

# ★ 핵심 — 죽은 티커가 '거래 중' 목록에 있다 → 폐기
MEM2["N2 사망포함→폐기"] = dict(MEM2["N1 강한불량→채택"])
MEM2["N2 사망포함→폐기"]["atl"] = ("OK", atl2(["AAPL", "DEAD", "OLDX"]), "3건")

# ★ 핵심 — 2026-08-22 재현. 후보가 전부 생존이라 불량입력 0개.
#   반드시 '판정불가'. '채택'이 나오면 1차 실패의 재발이다.
MEM2["N3 불량입력0→판정불가"] = {
    "atl": ("OK", atl2(["AAPL", "ADWPF", "BBBBF", "CCCCY"]), "4건"),
    "dl": ("OK", atl2(["ADWPF", "BBBBF", "CCCCY"]), "3건"),
    "sc": ("EMPTY", [], "빈 배열"),
    "profile:ADWPF": ("OK", [{"symbol": "ADWPF", "isActivelyTrading": True}], "1건"),
    "profile:BBBBF": ("OK", [{"symbol": "BBBBF", "isActivelyTrading": True}], "1건"),
    "profile:CCCCY": ("OK", [{"symbol": "CCCCY", "isActivelyTrading": True}], "1건"),
}

MEM2["N4 리스트미수신"] = {"atl": ("PLAN", None, "플랜 미포함")}

# 소멸(빈 배열)만 잡히고 사망은 없음 → 조건부 채택
MEM2["N5 약한불량만→조건부"] = {
    "atl": ("OK", atl2(["AAPL", "MSFT"]), "2건"),
    "dl": ("EMPTY", [], "빈 배열"),
    "sc": ("OK", scfeed(["OLDX", "OLDY"]), "2건"),
    "profile:OLDX": ("EMPTY", [], "빈 배열"),
    "profile:OLDY": ("EMPTY", [], "빈 배열"),
}

MEM2["N6 이상값"] = {
    "atl": ("OK", atl2(["AAPL"]), "1건"),
    "dl": ("OK", [{"noSymbolField": 1}], "1건"),
    "sc": ("OK", [{"date": None}], "1건"),
}

# 사망이지만 5자 + F 끝(ADWPF 부류) → 교락 위험. '강한'으로 세면 안 된다.
MEM2["N8 F끝사망→조건부"] = {
    "atl": ("OK", atl2(["AAPL", "MSFT"]), "2건"),
    "dl": ("OK", atl2(["XYZQF"]), "1건"),
    "sc": ("EMPTY", [], "빈 배열"),
    "profile:XYZQF": ("OK", [{"symbol": "XYZQF", "isActivelyTrading": False}], "1건"),
}

MEM2["N7 음성대조실패"] = dict(MEM2["N1 강한불량→채택"])
MEM2["N7 음성대조실패"]["atl"] = ("OK", atl2(["AAPL", "ZZZZQQ9"]), "2건")


def make_fake_mem2(table):
    def fake_call(path, keep=False):
        P._CALLS += 1
        if path.startswith("actively-trading-list"):
            return table.get("atl", ("EMPTY", [], "빈 배열"))
        if path.startswith("delisted-companies"):
            return table.get("dl", ("EMPTY", [], "빈 배열"))
        if path.startswith("symbol-change"):
            return table.get("sc", ("EMPTY", [], "빈 배열"))
        if path.startswith("profile?symbol="):
            sym = path.split("=", 1)[1]
            return table.get("profile:" + sym, ("EMPTY", [], "빈 배열"))
        return ("EMPTY", [], "미정의 경로 — 빈 배열")
    return fake_call


print()
print("── 멤버십 모드 2 (PROBE_MODE=membership2) ──")
m2_fails, m2_v = [], {}
for name, table in MEM2.items():
    P._CALLS = 0
    P.FIND = {}
    P._MODE = "membership2"
    P.call = make_fake_mem2(table)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = P.main()
        v = P.FIND.get("mem2_verdict", "(없음)")
        m2_v[name] = (v, P._CALLS, P.FIND.get("mem2_neg_case_exercised"))
        print(f"{name:22} ✅ rc={rc} · {P._CALLS}콜 · {v}")
    except Exception as e:
        m2_fails.append((name, e))
        print(f"{name:22} ❌ 예외 {type(e).__name__}: {e}")
        print(buf.getvalue()[-900:])

if m2_fails:
    print(f"❌ {len(m2_fails)}개 시나리오에서 예외")
    sys.exit(1)

checks2 = [
    ("N1 강한불량→채택", lambda v: v.startswith("채택")),
    ("N2 사망포함→폐기", lambda v: v.startswith("폐기")),
    ("N3 불량입력0→판정불가", lambda v: v.startswith("판정불가")),
    ("N4 리스트미수신", lambda v: v.startswith("판정불가")),
    ("N5 약한불량만→조건부", lambda v: v.startswith("조건부 채택")),
    ("N7 음성대조실패", lambda v: v.startswith("폐기")),
    ("N8 F끝사망→조건부", lambda v: v.startswith("조건부 채택")),
]
bad2 = [n for n, fn in checks2 if not fn(m2_v.get(n, ("",))[0])]

# 예산 상한 — 어떤 시나리오도 12콜을 넘으면 안 된다
over = [n for n, (_v, c, _e) in m2_v.items() if c > 12]
if over:
    bad2 += ["예산초과:" + ",".join(over)]
# 리스트 미수신은 1콜에서 끊어야 한다
if m2_v.get("N4 리스트미수신", ("", 99, None))[1] != 1:
    bad2.append("N4 조기중단(1콜)")
# 불량입력 0개면 '시험됨' 플래그가 False 여야 한다 — 1차 실패의 정확한 재발 방지
if m2_v.get("N3 불량입력0→판정불가", ("", 0, True))[2] is not False:
    bad2.append("N3 exercised 플래그가 False 가 아님")

print()
print(("✅" if not bad2 else "❌") + " 판별력 검사 2 — "
      + repr({n: m2_v.get(n, ("(없음)",))[0][:30] for n, _ in checks2}))
if bad2:
    print("❌ 실패: " + ", ".join(bad2))
    sys.exit(1)
print("✅ 멤버십2 8시나리오 — 불량입력 0개 세계에서 '판정불가'로 떨어짐(1차 실패 재발 방지)")


# ══════════════════════════════════════════════════════════════════════════
# 모드 유효성 — 조용한 폴백 방지 (2026-08-22 회귀)
# ══════════════════════════════════════════════════════════════════════════
# 워크플로가 membership2 를 빈 문자열로 매핑해 **기본 11콜이 조용히 돌았다.**
# 로그만 봐서는 다른 게 실행됐다는 걸 알기 어려웠다. 다시는 조용히 떨어지지 않게
# 모르는 모드는 콜 0으로 즉시 죽어야 한다. 그리고 'default' 는 정상 통과해야 한다.
print()
print("── 모드 유효성 (조용한 폴백 방지) ──")
mode_bad = []


def _run_mode(mode, table):
    P._CALLS = 0
    P.FIND = {}
    P._MODE = mode
    P.call = make_fake_mem2(table)
    with contextlib.redirect_stdout(io.StringIO()) as b:
        rc = P.main()
    return rc, P._CALLS, b.getvalue()


for mode, want_rc, want_calls in [("membershp2", 2, 0),   # 오타
                                  ("MEMBERSHIP2", 2, 0),  # 대문자(정규화는 env 단계)
                                  ("", None, None),       # 기본(빈값) — 통과해야 함
                                  ("default", 0, 11),     # 워크플로가 실제로 보내는 값
                                  ("membership2", 0, None)]:
    rc, calls, _out = _run_mode(mode, MEM2["N1 강한불량→채택"])
    ok = True
    if want_rc is not None and rc != want_rc:
        ok = False
    if want_calls is not None and calls != want_calls:
        ok = False
    print(f"  PROBE_MODE={mode!r:14} rc={rc} · {calls}콜 " + ("✅" if ok else "❌"))
    if not ok:
        mode_bad.append(mode)

if mode_bad:
    print("❌ 모드 유효성 실패: " + ", ".join(repr(m) for m in mode_bad))
    sys.exit(1)
print("✅ 모르는 모드는 콜 0으로 즉시 중단 · 유효 모드는 정상 실행")
