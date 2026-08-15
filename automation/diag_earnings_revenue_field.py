"""diag_earnings_revenue_field.py — 매출 컨센서스 실값 확인 (2차 프로브).

1차 프로브(diag_earnings_preview_fields.py)에서 확정된 것
────────────────────────────────────────────────────────
  ❌ analyst-estimates            — 이 플랜에서 HTTP 402
  ❌ earning-call-transcript-*    — 이 플랜에서 HTTP 402 (3단계 설계 변경 필요)
  ✅ price-target-consensus / grades-historical / news/stock

그때 `earnings?symbol=` 응답의 키 목록에서 매출 필드를 발견했다.

    date, epsActual, epsEstimated, lastUpdated,
    revenueActual, revenueEstimated, symbol

즉 A블록 매출 컨센서스는 analyst-estimates 없이도 살아날 수 있고,
EPS 와 매출을 **한 콜로** 받게 되어 스냅샷당 5콜 → 4콜로 줄어든다.

다만 1차에서는 **키의 존재만 봤지 값을 보지 않았다.** 키가 있어도 전부 null 이면
쓸 수 없다. 그 하나를 확인하는 것이 이 스크립트의 전부다.

1차와 달라진 점
───────────────
  · 402 확정된 두 엔드포인트 제거 → 종목당 1콜 (총 3콜)
  · 매출 필드를 기대 목록에 추가
  · **첫 항목이 아니라 앞 3개 행**을 표로 출력
    1차에서 epsActual 이 "없음"으로 뜬 건 첫 항목이 미래 분기(실적 미발표)라
    당연히 null 이었기 때문이다. 미래·과거 행을 같이 봐야 오독하지 않는다.

판정 기준
─────────
  A. 미래 행에 revenueEstimated 값이 있다  → A블록 매출 컨센서스 확보 ✅
  B. 키는 있는데 전 종목·전 행 null       → 매출 항목 제외하고 진행
  C. 과거 행에만 값이 있다                 → 컨센서스가 아니라 실적 확정치.
                                            사전 브리핑에는 쓸 수 없음

시트 접근·이메일·상태 머신 접촉 없음. 재실행 부작용 없다.

실행
────
    TICKERS=AAPL,NVDA,WMT python automation/diag_earnings_revenue_field.py
"""
import json
import os
import sys

# automation/ 에서 실행되든 루트에서 실행되든 공용 모듈을 찾게 한다(기존 diag 관례).
# python automation/xxx.py 는 sys.path[0] 이 automation/ 이라 루트 모듈을 못 찾는다.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_http as fh  # noqa: E402  (sys.path 설정 후에 import 해야 한다)

try:
    import pandas as pd
except Exception:
    pd = None

_env = str(os.environ.get("TICKERS", "") or "").strip()
TICKERS = ([t.strip().upper() for t in _env.split(",") if t.strip()]
           if _env else ["AAPL", "NVDA", "WMT"])

ROWS_SHOWN = 3          # 앞 3개 행 (보통 미래 1~2 + 과거)
LIMIT = 12              # past_earnings_dates 와 같은 창

# 후보 키 — FMP 가 표기를 바꿔도 잡히도록 넓게 둔다.
EPS_EST = ["epsEstimated", "epsEstimate", "estimatedEps"]
EPS_ACT = ["epsActual", "eps"]
REV_EST = ["revenueEstimated", "revenueEstimate", "estimatedRevenue"]
REV_ACT = ["revenueActual", "revenue"]


def _pick(item, names):
    """후보 키 중 실제로 존재하는 첫 키의 (키, 값). 없으면 (None, None)."""
    for n in names:
        if isinstance(item, dict) and n in item:
            return n, item.get(n)
    return None, None


def _today():
    if pd is not None:
        return pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
    from datetime import date
    return date.today().strftime("%Y-%m-%d")


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _money(v):
    n = _num(v)
    if n is None:
        return "null"
    a = abs(n)
    if a >= 1e9:
        return f"{n / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{n / 1e6:,.1f}M"
    return f"{n:,.2f}"


if not fh.fmp_key():
    print("❌ FMP_API_KEY 없음 — 종료")
    sys.exit(1)

TODAY = _today()
print("=" * 76)
print(f"매출 컨센서스 실값 확인 — earnings?symbol=  대상 {', '.join(TICKERS)}")
print(f"기준일 {TODAY} · 종목당 1콜")
print("=" * 76)

fut_rev = 0      # 미래 행에서 매출 추정치를 받은 종목 수
fut_eps = 0      # 미래 행에서 EPS 추정치를 받은 종목 수
past_rev = 0     # 과거 행에서만 매출값을 받은 종목 수
key_seen = {}    # 실제 응답에서 관측된 키 이름
ok_n = 0

for tk in TICKERS:
    data, status, kind = fh.fmp_get_json_ex(f"earnings?symbol={tk}&limit={LIMIT}")
    print(f"\n{'─' * 76}")
    if kind != "ok":
        print(f"▶ {tk}  ❌ {kind} (status={status})")
        continue
    if not isinstance(data, list) or not data:
        print(f"▶ {tk}  ⚠️ 200 인데 0건 — 경로는 살아 있으나 데이터 없음")
        continue

    ok_n += 1
    print(f"▶ {tk}  ✅ {len(data)}건")
    print(f"   실제 키: {', '.join(sorted(data[0].keys()))}")
    print()
    print(f"   {'날짜':12} {'시점':6} {'EPS추정':>10} {'EPS실적':>10} "
          f"{'매출추정':>12} {'매출실적':>12}")
    print(f"   {'-' * 66}")

    tk_fut_rev = tk_fut_eps = tk_past_rev = False

    for it in data[:ROWS_SHOWN]:
        if not isinstance(it, dict):
            continue
        d = str(it.get("date") or "")[:10]
        when = "미래" if d > TODAY else "과거"
        ke, ve = _pick(it, EPS_EST)
        ka, va = _pick(it, EPS_ACT)
        kre, vre = _pick(it, REV_EST)
        kra, vra = _pick(it, REV_ACT)
        for k in (ke, ka, kre, kra):
            if k:
                key_seen[k] = key_seen.get(k, 0) + 1

        if when == "미래":
            if _num(vre) is not None:
                tk_fut_rev = True
            if _num(ve) is not None:
                tk_fut_eps = True
        else:
            if _num(vre) is not None or _num(vra) is not None:
                tk_past_rev = True

        ve_s = "null" if _num(ve) is None else f"{_num(ve):,.4f}"
        va_s = "null" if _num(va) is None else f"{_num(va):,.4f}"
        print(f"   {d:12} {when:6} {ve_s:>10} {va_s:>10} "
              f"{_money(vre):>12} {_money(vra):>12}")

    fut_rev += 1 if tk_fut_rev else 0
    fut_eps += 1 if tk_fut_eps else 0
    past_rev += 1 if tk_past_rev else 0

    if not tk_fut_rev and not tk_fut_eps:
        nxt = [str(x.get("date") or "")[:10] for x in data[:ROWS_SHOWN]]
        print(f"   ⚠️ 앞 {ROWS_SHOWN}행에 미래 분기가 없음 (날짜 {nxt}) — "
              f"limit 을 늘리거나 다른 종목으로 재확인 필요")

# ── 판정 ──────────────────────────────────────────────────────────────────
n = len(TICKERS)
print("\n" + "=" * 76)
print("판정")
print("=" * 76)
print(f"  호출 성공                        {ok_n}/{n}")
print(f"  미래 분기에 EPS 추정치 존재      {fut_eps}/{n}")
print(f"  미래 분기에 매출 추정치 존재     {fut_rev}/{n}")
print(f"  과거 분기에 매출값 존재          {past_rev}/{n}")
if key_seen:
    print(f"\n  관측된 키: " + ", ".join(f"{k}({v})" for k, v in sorted(key_seen.items())))

print()
_rev_key_seen = any(k in key_seen for k in (REV_EST + REV_ACT))
if ok_n == 0:
    print("  ❌ earnings 경로 자체가 실패. B블록(beat율·서프라이즈)까지 재설계 필요")
elif fut_eps == 0 and fut_rev == 0:
    print("  ⚠️ D: 응답에 미래 분기 행이 하나도 없다 — 판정 불가.")
    print(f"     limit({LIMIT})을 늘리거나 실적이 임박한 종목으로 재확인할 것.")
elif not _rev_key_seen:
    print("  ⚠️ E: 매출 관련 키가 응답에 아예 없다. A블록에서 매출 항목을 뺀다.")
    print("     (1차 프로브가 본 키 목록과 다르다 — FMP 응답 스키마가 바뀐 것)")
elif fut_rev == ok_n:
    print("  ✅ A: 매출 컨센서스 확보. analyst-estimates 없이 진행한다.")
    print("     EPS·매출을 한 콜로 받아 스냅샷당 4콜.")
elif fut_rev > 0:
    print(f"  ⚠️ 부분 확보 ({fut_rev}/{ok_n}) — 종목에 따라 결측.")
    print("     Data_Flags 에 결측을 표기하고 카드에서 '자료 없음'으로 처리한다.")
elif past_rev > 0:
    print("  ⚠️ C: 과거 행에만 값이 있음 = 컨센서스가 아니라 실적 확정치.")
    print("     사전 브리핑에는 쓸 수 없다. A블록에서 매출 항목을 뺀다.")
else:
    print("  ⚠️ B: 키는 있으나 전부 null. A블록에서 매출 항목을 뺀다.")
    print("     (목표주가·EPS·수정률만으로 A블록 구성)")

print("\n" + fh.fmp_stats_line())
print("(진단 스크립트 — 종료 코드는 항상 0)")
