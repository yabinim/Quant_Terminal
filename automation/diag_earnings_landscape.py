"""diag_earnings_landscape.py — 1-B 시장 이벤트 지형 섹션 회귀 검사.

app.py 의 섹션 6 렌더 로직을 **소스에서 직접 추출**해 검증한다(로직 사본이
아니라 실제 코드). Streamlit·Sheets 없이 순수 데이터 변환만 본다.

가장 위험한 버그:
  1) 캘린더 고아 행(과거 보유/워치에서 빠진 종목)이 표에 새는 것
  2) Tier 1(내 종목)이 ⭐ 없이 섞여 시장 종목처럼 보이는 것
  3) D-10 초과/음수가 새는 것
"""
import ast
import re
import sys

import pandas as pd

APP = "/home/claude/app.py"
fail = []


def chk(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        fail.append(msg)


# ── 1. 소스에서 섹션 6 블록 추출 ────────────────────────────────────────
src = open(APP, encoding="utf-8").read()
start = src.index("# ── 섹션 6: 시장 이벤트 지형")
end = src.index("elif main_nav == _NAV_ADMIN_APPROVAL:", start)
block = src[start:end]

print("[1] 소스 추출 및 구조")
chk(len(block) > 500, f"섹션 6 블록 추출 {len(block)}자")
chk("expanded=False" in block, "기본 접힘(expanded=False)")
chk("load_earnings_universe()" in block, "유니버스 시트 로드 호출")
chk("hide_index=True" in block and "use_container_width=True" in block,
    "기존 표 렌더 규약 준수")

# 루프 본문만 떼어 실행 가능한 함수로 재구성 (실제 소스 라인 사용)
loop_start = block.index("for _tk_u, _crow_u in")
loop_end = block.index("if not _land:")
loop_src = block[loop_start:loop_end].rstrip()
# 12칸 들여쓰기(with > else > for) → 4칸으로 정규화
lines = [ln[12:] if ln.startswith(" " * 12) else ln for ln in loop_src.split("\n")]
body = "\n".join("    " + ln for ln in lines)
fn_src = ("def _build(_cal, _univ, _mine, _e_today, ec):\n"
          "    _land = []\n" + body + "\n    return _land\n")
ast.parse(fn_src)          # 구문 검증
ns = {}
exec(compile(fn_src, "<sec6>", "exec"), ns)
_build = ns["_build"]

import earnings_core as ec  # noqa: E402

# ── 2. 픽스처 ───────────────────────────────────────────────────────────
TODAY = pd.Timestamp("2026-08-13")


def cal_row(tk, date, timing="amc", source="universe", median=None):
    r = ec.calendar_row(tk, {"earnings_date": date, "timing": timing},
                        None, today=TODAY, source=source)
    d = dict(zip(ec.CALENDAR_COLS, r))
    if median is not None:
        d.update({"Exp_Median_Pct": str(median), "Exp_Worst_Pct": "-8.0",
                  "Sample_N": "8", "Move_Confidence": "medium",
                  "Move_For_Date": date, "Move_Computed_At": "2026-08-13"})
    return d


_cal = {
    "NVDA": cal_row("NVDA", "2026-08-20", "amc", "universe", 6.5),
    "WMT":  cal_row("WMT",  "2026-08-19", "bmo", "universe", 4.0),
    "AAPL": cal_row("AAPL", "2026-08-18", "amc", "user", 3.2),   # 내 종목
    "XOM":  cal_row("XOM",  "2026-09-30", "bmo", "universe"),    # D-48 → 제외
    "OLD":  cal_row("OLD",  "2026-08-17", "amc", "user"),        # 고아 행
    "PAST": cal_row("PAST", "2026-08-10", "amc", "universe"),    # 과거 → 제외
}
_univ = {
    "NVDA": {"Ticker": "NVDA", "Name": "NVIDIA Corporation", "Sector": "Technology"},
    "WMT":  {"Ticker": "WMT", "Name": "Walmart Inc.", "Sector": "Consumer Defensive"},
    "XOM":  {"Ticker": "XOM", "Name": "Exxon Mobil", "Sector": "Energy"},
    "PAST": {"Ticker": "PAST", "Name": "Past Co", "Sector": "X"},
}
_mine = {"AAPL"}

land = _build(_cal, _univ, _mine, TODAY, ec)
tks = {re.sub(r"^⭐ ", "", r["티커"]) for r in land}

print("\n[2] 필터링")
chk("OLD" not in tks, "캘린더 고아 행 제외 (유니버스에도 없고 현재 내 종목도 아님)")
chk("XOM" not in tks, "D-10 초과 제외 (D-48)")
chk("PAST" not in tks, "과거 실적일 제외")
chk(tks == {"NVDA", "WMT", "AAPL"}, f"표시 대상 = {sorted(tks)}")

print("\n[3] 내 종목 표시")
star = [r["티커"] for r in land if r["티커"].startswith("⭐")]
chk(star == ["⭐ AAPL"], f"⭐ 표시 = {star}")
chk(all(not r["티커"].startswith("⭐") for r in land
        if r["티커"] in ("NVDA", "WMT")), "시장 종목엔 ⭐ 없음")

print("\n[4] 필드")
by = {re.sub(r"^⭐ ", "", r["티커"]): r for r in land}
chk(by["NVDA"]["회사"] == "NVIDIA Corporation", f"회사명 조인 = {by['NVDA']['회사']}")
chk(by["NVDA"]["섹터"] == "Technology", f"섹터 조인 = {by['NVDA']['섹터']}")
# FMP stable 이 발표 시각을 안 주므로 timing 은 전부 추론값이다 →
# 확정 사실처럼 보이는 라벨을 쓰면 안 된다(2026-08-14).
chk(by["WMT"]["시점"] == "장 시작 전(추정)", f"bmo 라벨 = {by['WMT']['시점']}")
chk(by["NVDA"]["시점"] == "장 마감 후(추정)", f"amc 라벨 = {by['NVDA']['시점']}")
chk("(추정)" in by["WMT"]["시점"], "추정임이 라벨에 드러남")
chk(by["NVDA"]["예상 갭"] == "±6.5%", f"예상 갭 = {by['NVDA']['예상 갭']}")
chk(by["AAPL"]["회사"] == "-", "유니버스에 없는 내 종목 → 회사명 '-' (예외 아님)")

print("\n[5] D-10 이지만 갭 미산출 → '-'")
_cal2 = {"NOGAP": cal_row("NOGAP", "2026-08-21", "amc", "universe")}
_u2 = {"NOGAP": {"Name": "No Gap Co", "Sector": "Test"}}
l2 = _build(_cal2, _u2, set(), TODAY, ec)
chk(l2 and l2[0]["예상 갭"] == "-", f"갭 없음 → {l2[0]['예상 갭'] if l2 else 'N/A'}")

print("\n[6] 정렬 (D 오름차순)")
land.sort(key=lambda x: (x["D"], x["티커"]))
chk([r["D"] for r in land] == sorted(r["D"] for r in land), "D 오름차순")
chk(land[0]["D"] == 5, f"최근접 = D-{land[0]['D']} (AAPL 8/18)")

print("\n[7] 유니버스 시트 부재 → 안전")
chk("if not _univ:" in block, "빈 유니버스 안내 분기 존재")
l3 = _build(_cal, {}, {"AAPL"}, TODAY, ec)
chk({re.sub(r'^⭐ ', '', r['티커']) for r in l3} == {"AAPL"},
    "유니버스 비어도 내 종목만 안전하게 표시")

print("\n[8] app.py SSOT 매니페스트")
chk('"earnings_core", ec' in src, "_SSOT_NEEDS 에 earnings_core 등록")
chk('"Source" in m.CALENDAR_COLS' in src, "Source 열 존재 추가조건")
chk("def load_earnings_universe" in src, "load_earnings_universe 정의")
chk("add_worksheet" not in src[src.index("def load_earnings_universe"):
                               src.index("def load_earnings_universe") + 900],
    "유니버스 로더는 시트를 생성하지 않음(읽기 전용)")

print("\n" + "=" * 52)
print(f"❌ 실패 {len(fail)}건" if fail else "✅ 전부 통과")
sys.exit(1 if fail else 0)
