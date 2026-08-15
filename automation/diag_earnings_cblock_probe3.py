"""diag_earnings_cblock_probe3.py — insider-trading/search 커버리지 확인 (3차).

왜 또 찌르는가
──────────────
  2차에서 insider-trading/statistics 가 3/3 생존했다. 다만 한계 둘이 남았다.
      · 분기 지연 — 8월 이벤트에 6월말 마감 분기를 붙인다 (46~138일 묵음)
      · 건수지 금액이 아니다 — totalSales 는 거래 '건수'다

  둘 다 insider-trading/search 로 풀린다. 행 단위라 날짜가 있고 price 가 있다.
  2차에서 확보한 18종 코드 목록으로 유형 필터도 해결됐다.

  남은 의문은 하나 — **한 번의 호출이 실적 전 90일을 덮는가.**
  1차 프로브에서 limit=20 이 AAPL 은 약 2개월, WMT 는 하루밖에 못 덮었다.
  베스팅일에 신고가 몰리는 종목에서 재량 거래가 창 밖으로 밀려난다.

무엇을 보는가 (6콜)
───────────────────
  종목당 2콜 × 3종목
      page=0&limit=100   기본 커버리지
      page=1&limit=100   페이징이 실제로 더 과거를 주는가 (중복이 아닌가)

  종목별로 판정하는 것:
    1. 커버리지   page=0 이 90일을 덮는가. 못 덮으면 몇 페이지가 필요한가
    2. 재량 비중  창 안에서 P-Purchase / S-Sale 이 몇 %인가
                  95%가 F-InKind 잡음이면 limit 을 올려도 실효 커버는 작다
    3. price 건전성  S-Sale 행의 price 가 채워져 있는가
                  비어 있으면 달러 환산이 불가능하고 건수로 후퇴해야 한다
    4. 페이징 동작  page=1 이 page=0 보다 과거인가, 아니면 같은 행인가

  이 넷이 다 통과해야 '실적 전 90일 달러 금액'이 성립한다.
  하나라도 깨지면 2차의 통계형 3컬럼(Insider_Sales_Q / TTM4 / Q)으로 확정한다.

날짜 기준
─────────
  transactionDate 를 쓴다. filingDate 는 신고일이라 거래일보다 늦다(Form 4 는
  2영업일 내 신고). 실적 전 창을 재려면 실제 거래일이어야 한다.
  transactionDate 가 비어 있으면 그 행은 filingDate 로 대체하고 별도로 센다.

비용: 총 6콜. 시트·이메일·상태 머신 미접촉. 부작용 없음.

실행
────
    TICKERS=AAPL,NVDA,WMT python automation/diag_earnings_cblock_probe3.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_http as fh  # noqa: E402  (sys.path 설정 후에 import)

from datetime import date  # noqa: E402

_env = str(os.environ.get("TICKERS", "") or "").strip()
TICKERS = ([t.strip().upper() for t in _env.split(",") if t.strip()]
           if _env else ["AAPL", "NVDA", "WMT"])

WINDOW_D = 90     # 실적 전 목표 창
LIMIT = 100       # 문서 예시 최대치

# 2차에서 확보한 18종 중 실제 재량 거래.
# I-Discretionary 는 16b-3(f) 계획 내 재량이라 성격이 달라 따로 센다.
DISCRETIONARY = {"P-Purchase", "S-Sale"}
SEMI = {"I-Discretionary"}

TODAY = date.today()
CUTOFF = date.fromordinal(TODAY.toordinal() - WINDOW_D)


def _rows(data):
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("data", "results"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        return [data]
    return []


def _d(v):
    """'2026-08-13...' → date. 실패하면 None."""
    s = str(v or "")[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _row_date(it):
    """(날짜, 대체여부). transactionDate 우선, 없으면 filingDate."""
    d = _d(it.get("transactionDate"))
    if d:
        return d, False
    return _d(it.get("filingDate")), True


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _key(it):
    """행 동일성 판정용. 페이징 중복 검사에 쓴다."""
    return (str(it.get("transactionDate")), str(it.get("reportingName")),
            str(it.get("transactionType")), str(it.get("securitiesTransacted")),
            str(it.get("price")))


if not fh.fmp_key():
    print("❌ FMP_API_KEY 없음 — 종료")
    sys.exit(1)

print("=" * 78)
print("C블록 프로브 3차 — insider-trading/search 커버리지")
print(f"대상 {', '.join(TICKERS)} · 총 {len(TICKERS) * 2}콜 · 기준일 {TODAY}")
print(f"목표 창 최근 {WINDOW_D}일 (>= {CUTOFF}) · limit={LIMIT}")
print("=" * 78)

verdicts = {}   # tk → dict

for tk in TICKERS:
    print(f"\n{'=' * 78}")
    print(f"▶ {tk}")
    print("=" * 78)

    v = {"ok": False, "covers": False, "disc_n": 0, "semi_n": 0,
         "other_n": 0, "sale_val": 0.0, "buy_val": 0.0,
         "price_bad": 0, "fallback_d": 0, "span": None,
         "page1": "미확인", "oldest": None}
    verdicts[tk] = v

    # ── page=0 ────────────────────────────────────────────────────────────
    p0_path = f"insider-trading/search?symbol={tk}&page=0&limit={LIMIT}"
    data, status, kind = fh.fmp_get_json_ex(p0_path)
    print(f"\n  {p0_path}")
    if kind != "ok":
        mark = "플랜 제한(402)" if kind == "plan_limited" else kind
        print(f"    ❌ {mark}  status={status}")
        continue

    r0 = _rows(data)
    if not r0:
        print("    ⚠️ 200 인데 0건")
        continue

    v["ok"] = True
    dates = []
    for it in r0:
        d, fb = _row_date(it)
        if d:
            dates.append(d)
        if fb:
            v["fallback_d"] += 1

    if dates:
        oldest, newest = min(dates), max(dates)
        v["oldest"] = oldest
        v["span"] = (newest - oldest).days
        v["covers"] = oldest <= CUTOFF
        cov = "✅ 90일 덮음" if v["covers"] else "❌ 90일 못 덮음"
        print(f"    ✅ {len(r0)}건 · {oldest} ~ {newest} "
              f"({v['span']}일) · {cov}")
    else:
        print(f"    ✅ {len(r0)}건 · ⚠️ 날짜 해석 불가")

    if v["fallback_d"]:
        print(f"    ⚠️ transactionDate 비어 filingDate 로 대체: "
              f"{v['fallback_d']}건")

    # ── 창 안의 유형 분포 ─────────────────────────────────────────────────
    tally = {}
    for it in r0:
        d, _fb = _row_date(it)
        if not d or d < CUTOFF:
            continue
        t = str(it.get("transactionType") or "?")
        tally[t] = tally.get(t, 0) + 1

        qty = _f(it.get("securitiesTransacted")) or 0.0
        px = _f(it.get("price"))

        if t in DISCRETIONARY:
            v["disc_n"] += 1
            if px is None or px <= 0:
                v["price_bad"] += 1
            else:
                if t == "S-Sale":
                    v["sale_val"] += px * qty
                else:
                    v["buy_val"] += px * qty
        elif t in SEMI:
            v["semi_n"] += 1
        else:
            v["other_n"] += 1

    in_win = v["disc_n"] + v["semi_n"] + v["other_n"]
    print(f"\n    최근 {WINDOW_D}일 안 {in_win}건 유형 분포:")
    for t, c in sorted(tally.items(), key=lambda x: -x[1]):
        star = " ←재량" if t in DISCRETIONARY else (
            " ←준재량" if t in SEMI else "")
        print(f"      {t:<18} {c:>4}{star}")

    if in_win:
        pct = 100.0 * v["disc_n"] / in_win
        print(f"    재량 비중 {v['disc_n']}/{in_win} ({pct:.0f}%)")
    if v["disc_n"]:
        print(f"    S-Sale 달러 {v['sale_val']:>18,.0f}")
        print(f"    P-Purchase 달러 {v['buy_val']:>14,.0f}")
        if v["price_bad"]:
            print(f"    ⚠️ price 결측·0 인 재량 행 {v['price_bad']}건 "
                  "— 달러 합계가 과소계상된다")

    # ── page=1 ────────────────────────────────────────────────────────────
    p1_path = f"insider-trading/search?symbol={tk}&page=1&limit={LIMIT}"
    data1, status1, kind1 = fh.fmp_get_json_ex(p1_path)
    print(f"\n  {p1_path}")
    if kind1 != "ok":
        v["page1"] = "실패"
        print(f"    ❌ {kind1}  status={status1}")
    else:
        r1 = _rows(data1)
        if not r1:
            v["page1"] = "빈 응답"
            print("    ⚠️ 0건 — 페이징으로 더 과거를 얻을 수 없다")
        else:
            k0 = {_key(x) for x in r0}
            dup = sum(1 for x in r1 if _key(x) in k0)
            d1 = [d for d, _f2 in (_row_date(x) for x in r1) if d]
            o1 = min(d1) if d1 else None
            if dup >= len(r1) * 0.9:
                v["page1"] = "중복"
                print(f"    ❌ {len(r1)}건 중 {dup}건이 page=0 과 동일 "
                      "— 페이징이 동작하지 않는다")
            elif o1 and v["oldest"] and o1 < v["oldest"]:
                v["page1"] = "정상"
                print(f"    ✅ {len(r1)}건 · 가장 과거 {o1} "
                      f"— page=0({v['oldest']})보다 과거. 페이징 정상")
            else:
                v["page1"] = "불명"
                print(f"    ⚠️ {len(r1)}건 · 중복 {dup} · 가장 과거 {o1} "
                      "— 판단 보류")

# ── 판정 ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 78)
print("판정")
print("=" * 78)
print(f"  {'종목':6} {'생존':>4} {'90일':>5} {'재량':>5} {'잡음':>5} "
      f"{'price결측':>9}  {'페이징':<6}")
print("  " + "-" * 60)
for tk in TICKERS:
    v = verdicts[tk]
    print(f"  {tk:6} {'✅' if v['ok'] else '❌':>4} "
          f"{'✅' if v['covers'] else '❌':>5} "
          f"{v['disc_n']:>5} {v['other_n'] + v['semi_n']:>5} "
          f"{v['price_bad']:>9}  {v['page1']:<6}")

alive = [t for t in TICKERS if verdicts[t]["ok"]]
covers = [t for t in alive if verdicts[t]["covers"]]
no_disc = [t for t in alive if verdicts[t]["disc_n"] == 0]
bad_px = [t for t in alive if verdicts[t]["price_bad"]]

print()
if not alive:
    print("  ❌ 전 종목 실패 — 2차 통계형 3컬럼으로 확정한다.")
else:
    if len(covers) == len(alive):
        print(f"  ✅ 커버리지 — {LIMIT}건 1콜로 전 종목 {WINDOW_D}일을 덮는다. "
              "스냅샷당 +1콜.")
    else:
        miss = [t for t in alive if t not in covers]
        print(f"  ❌ 커버리지 — {', '.join(miss)} 가 1콜로 {WINDOW_D}일을 못 덮는다.")
        if any(verdicts[t]["page1"] == "정상" for t in miss):
            print("     페이징은 동작한다. 종목마다 콜 수가 달라지고 "
                  "상한을 못 정한다는 뜻이다.")
        else:
            print("     페이징도 안 되면 창을 완성할 방법이 없다 — 달러 환산 포기.")

    if no_disc:
        print(f"  ❌ 재량 거래 0건 — {', '.join(no_disc)}. "
              f"최근 {WINDOW_D}일에 P/S 가 없으면 이 창에서는 신호가 나오지 않는다.")

    if bad_px:
        allbad = [t for t in bad_px
                  if verdicts[t]["price_bad"] >= verdicts[t]["disc_n"]]
        part = [t for t in bad_px if t not in allbad]
        if allbad:
            print(f"  ❌ price 전량 결측 — {', '.join(allbad)}. "
                  "달러 환산 자체가 불가능하다. 건수로 후퇴할 것.")
        if part:
            print(f"  ⚠️ price 일부 결측 — {', '.join(part)}. "
                  "달러 합계가 과소계상된다.")

    if len(covers) == len(alive) and not no_disc and not bad_px:
        print("  ✅ 네 조건 모두 통과 — '실적 전 90일 달러 금액'이 성립한다.")
    else:
        print("  → 하나라도 깨졌다. 2차 통계형 3컬럼"
              "(Insider_Sales_Q / TTM4 / Q)으로 확정하는 쪽이 안전하다.")

print("\n" + fh.fmp_stats_line())
print("(진단 스크립트 — 종료 코드는 항상 0)")
