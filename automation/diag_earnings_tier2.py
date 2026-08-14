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

print("\n" + ("=" * 52))
print(f"❌ 실패 {len(fail)}건" if fail else "✅ 전부 통과")
sys.exit(1 if fail else 0)
