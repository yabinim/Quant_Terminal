"""diag_earnings_tier2.py — Tier 1/Tier 2 구분 회귀 검사.

네트워크·Sheets 없이 순수 로직만 본다. workflow_dispatch 전용으로 붙일 것.
가장 위험한 버그: 구 20열 행의 Notes 를 Source 로 오독 → 기존 종목이
universe 로 강등되어 스냅샷·이메일이 조용히 끊기는 것. [1][3] 이 그것을 잡는다.
"""
import sys
import pandas as pd
import earnings_core as ec

TODAY = pd.Timestamp("2026-08-13")
NOW = "2026-08-13 17:00 ET"
fail = []


def chk(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        fail.append(msg)


print("\n[1] 구 20열 행 → Source 공란 → user 로 해석 (기존 종목 침묵 방지)")
old_row = ["AAPL", "2026-08-20", "confirmed", "amc", "7", "2026-08-13", "near"] \
    + [""] * 9 + ["", "", "", "메모"]          # 20열 (Source 없음)
parsed = ec.parse_calendar([ec.CALENDAR_COLS, old_row])
row = parsed["AAPL"]
chk(row.get("Notes") == "메모", f"Notes 보존 = {row.get('Notes')!r} (Source 로 오독 안 됨)")
chk(not ec.is_universe_only(row), "구 행 → user 로 해석")

print("\n[2] calendar_row 의 source 인자")
r_user = ec.calendar_row("AAPL", {"earnings_date": "2026-08-20", "timing": "amc"},
                         None, today=TODAY, now_et=NOW, source=ec.SOURCE_USER)
r_univ = ec.calendar_row("XOM", {"earnings_date": "2026-08-21", "timing": "bmo"},
                         None, today=TODAY, now_et=NOW, source=ec.SOURCE_UNIVERSE)
chk(len(r_user) == ec.CALENDAR_NCOL == 21, f"행 길이 {len(r_user)} == 21")
chk(r_user[-1] == "user" and r_univ[-1] == "universe", "Source 기록 정확")

print("\n[3] source 미지정 시 prev 보존 (승격/강등이 사고로 안 일어남)")
prev = dict(zip(ec.CALENDAR_COLS, r_univ))
r_keep = ec.calendar_row("XOM", {"earnings_date": "2026-08-21"}, None,
                         today=TODAY, now_et=NOW, prev=prev)
chk(r_keep[-1] == "universe", "prev 의 universe 유지")

print("\n[4] pass_snapshot — Tier 2 제외 / Tier 1 통과")
sys.modules.setdefault("gspread", type(sys)("gspread"))
cal = {
    "AAPL": dict(zip(ec.CALENDAR_COLS, r_user)),
    "XOM": dict(zip(ec.CALENDAR_COLS, r_univ)),
}
for tk in cal:
    cal[tk]["Exp_Median_Pct"] = "4.0"
    cal[tk]["Exp_Worst_Pct"] = "-8.0"
    cal[tk]["Sample_N"] = "8"
    cal[tk]["Move_Confidence"] = "medium"


def fake_snapshot(cal, today):
    """pass_snapshot 의 Tier 분기만 재현(시트 IO 제거)."""
    kept, skipped = [], []
    for tk, row in cal.items():
        if ec.is_universe_only(row):
            skipped.append(tk)
            continue
        dd = ec.days_until_from_row(row, today)
        if dd is None or dd < 0 or dd > ec.SCAN_HORIZON_DAYS:
            continue
        kept.append(tk)
    return kept, skipped


kept, skipped = fake_snapshot(cal, TODAY)
chk(kept == ["AAPL"], f"스냅샷 대상 = {kept}")
chk(skipped == ["XOM"], f"유니버스 제외 = {skipped}")

print("\n[5] merge_universe_sources — 출처 라벨/정렬")
rows = ec.merge_universe_sources(
    {"NVDA": {"name": "NVIDIA", "market_cap": 3e12},
     "AMD": {"name": "AMD", "market_cap": 3e11}},
    {"NVDA": {"sector": "Technology", "market_cap": 3e12},
     "JPM": {"name": "JPMorgan", "market_cap": 6e11}},
    now_et=NOW)
srcs = {r[0]: r[4] for r in rows}
chk(srcs.get("NVDA") == "BOTH", f"NVDA={srcs.get('NVDA')}")
chk(srcs.get("AMD") == "NDX100", f"AMD={srcs.get('AMD')}")
chk(srcs.get("JPM") == "SP500_LARGE", f"JPM={srcs.get('JPM')}")
chk([r[0] for r in rows] == ["NVDA", "JPM", "AMD"], "시총 내림차순 정렬")
chk(all(len(r) == ec.UNIVERSE_NCOL for r in rows), "유니버스 행 길이 일치")

print("\n[6] parse_universe 왕복")
back = ec.parse_universe([ec.UNIVERSE_COLS] + rows)
chk(len(back) == 3 and back[0]["Ticker"] == "NVDA", f"왕복 {len(back)}건")

print("\n[7] ETF 멤버십 파싱 — 현금/복수클래스/중복 제거 (스텁)")
import fmp_http as fh


class _R:
    def __init__(self, d, c=200):
        self._d, self.status_code = d, c

    def json(self):
        return self._d


_QQQ = [{"asset": "NVDA", "weightPercentage": 9.1, "name": "NVIDIA"},
        {"asset": "AAPL", "weightPercentage": 8.0, "name": "Apple"},
        {"asset": "BRK.B", "weightPercentage": 1.0},
        {"asset": "USD", "weightPercentage": 0.2},
        {"asset": "XTSLA", "weightPercentage": 0.1},
        {"asset": "", "weightPercentage": 0.5},
        {"asset": "AAPL", "weightPercentage": 0.1}]
_SPY = [{"asset": "AAPL", "weightPercentage": 7.0},
        {"asset": "MSFT", "weightPercentage": 6.5},
        {"asset": "JPM", "weightPercentage": 1.4},
        {"asset": "XOM", "weightPercentage": 1.1}]
_SCR = [{"symbol": "NVDA", "companyName": "NVIDIA Corp", "sector": "Technology",
         "marketCap": 3.1e12},
        {"symbol": "AAPL", "companyName": "Apple Inc", "sector": "Technology",
         "marketCap": 3.4e12},
        {"symbol": "MSFT", "companyName": "Microsoft", "sector": "Technology",
         "marketCap": 3.0e12},
        {"symbol": "JPM", "companyName": "JPMorgan", "sector": "Financials",
         "marketCap": 6.0e11}]
_orig_get_ex = fh.fmp_get_ex


def _fake(url, timeout=None, retries=None):
    if "QQQ" in url:
        return _R(_QQQ), 200, "ok"
    if "SPY" in url:
        return _R(_SPY), 200, "ok"
    if "screener" in url:
        return _R(_SCR), 200, "ok"
    return None, 404, "http_error"


fh.fmp_get_ex = _fake
fh.set_key_provider(lambda: "K")
res = ec.fetch_market_universe(key="K", spy_top_n=3, use_etf=True)
chk(res["source"] == "etf" and res["ok"], f"출처 etf / ok={res['ok']}")
chk("USD" not in res["ndx"] and "XTSLA" not in res["ndx"], "현금 항목 제외")
chk("BRK" not in str(res["ndx"]), "복수클래스(BRK.B) 제외")
chk(sorted(res["ndx"]) == ["AAPL", "NVDA"], f"QQQ 중복 제거 = {sorted(res['ndx'])}")
u = ec.merge_universe_sources(res["ndx"], res["sp"], now_et=NOW,
                             labels=res.get("labels"))
chk(u[0][0] == "AAPL" and u[0][4] == "BOTH", "교집합 BOTH + 시총 최상위")
chk(all(str(r[3]).strip() for r in u), "Market_Cap 스크리너 보강 완료")

print("\n[8] ETF 전멸 → 스크리너 폴백")


def _fake_etf_dead(url, timeout=None, retries=None):
    if "screener" in url:
        return _R(_SCR + [{"symbol": "TINY", "companyName": "Tiny",
                           "marketCap": 3.0e10}]), 200, "ok"
    return None, 403, "http_error"


fh.fmp_get_ex = _fake_etf_dead
res2 = ec.fetch_market_universe(key="K", spy_top_n=3, use_etf=True)
chk(res2["source"] == "screener" and res2["ok"], f"폴백 승격 = {res2['source']}")
chk("TINY" not in res2["sp"], "폴백은 1,500억 하한 적용 (TINY 300억 제외)")
chk("403" in res2["diag"], f"HTTP 상태 진단 노출: {res2['diag'][:40]}...")

print("\n[9] 스크리너 단독(기본) — ETF 호출 자체를 안 함")
_calls = []


def _fake_count(url, timeout=None, retries=None):
    _calls.append(url)
    if "screener" in url:
        return _R(_SCR), 200, "ok"
    return None, 402, "plan_limited"


fh.fmp_get_ex = _fake_count
res4 = ec.fetch_market_universe(key="K")
chk(not any("holdings" in u for u in _calls), f"ETF 호출 0회 (총 {len(_calls)}콜)")
chk(res4["source"] == "screener", f"출처 = {res4['source']}")
u4 = ec.merge_universe_sources(res4["ndx"], res4["sp"], now_et=NOW,
                               labels=res4.get("labels"))
_labels = {r[4] for r in u4}
chk(_labels == {"US_LARGE"}, f"라벨 US_LARGE (거짓 SP500_LARGE 아님) = {_labels}")

print("\n[10] 402 는 재시도하지 않는다 (플랜 제한)")
_n = {"c": 0}


def _fake_402(url, timeout=None, retries=None):
    _n["c"] += 1
    return None, 402, "plan_limited"


fh.fmp_get_ex = _fake_402
ec._etf_membership("QQQ", "K")
chk(_n["c"] == 1, f"402 호출 1회로 종료 (실제 {_n['c']}회)")

print("\n[11] 전멸 → 안전 실패")
fh.fmp_get_ex = lambda url, timeout=None, retries=None: (None, 404, "http_error")
res3 = ec.fetch_market_universe(key="K")
chk(not res3["ok"] and res3["source"] == "none", "ok=False → 시트 미갱신 경로")
fh.fmp_get_ex = _orig_get_ex


print("\n" + ("=" * 52))
print(f"❌ 실패 {len(fail)}건" if fail else "✅ 전부 통과")
sys.exit(1 if fail else 0)
