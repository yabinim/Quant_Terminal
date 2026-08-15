"""diag_earnings_cblock_probe2.py — C블록 후보 프로브 2차 (경로 정정판).

1차 프로브에서 정정된 것
────────────────────────
  1차에서 insider-trade-statistics?symbol= 로 찔러 404 를 받고
  "플랜 제한"으로 오판했다. 공식 문서 확인 결과 **경로 오류**였다.
      틀림  insider-trade-statistics?symbol=
      정식  insider-trading/statistics?symbol=

  반면 아래 셋은 경로가 정확했고 402 도 진짜였다 — 되살릴 여지 없음.
      news/press-releases?symbols=      402
      earning-call-transcript-dates     402
      analyst-estimates                 402

무엇을 보는가 (4콜)
───────────────────
  A  insider-trading/statistics?symbol=   × 3종목
  B  insider-trading-transaction-type     × 1 (전역, 종목 무관)

A 가 살아 있으면 1차에서 지적한 함정 두 개가 자동으로 풀린다.
  · 함정 1 (A-Award / F-InKind 오염)
      → totalPurchases / totalSales 가 이미 재량 매수·매도(P/S)만 센 값
  · 함정 2 (limit=20 이 종목마다 다른 기간을 덮음)
      → 분기 버킷으로 나오므로 창 길이가 고정된다

이 프로브가 반드시 판정해야 할 것 — **신선도**
────────────────────────────────────────────
  분기 집계는 지연된다. 최신 분기가 이미 몇 달 지난 것이면
  실적 이벤트 직전 스냅샷에 붙여봐야 의미가 없다.
  경로가 살아 있다(200)는 것과 쓸모가 있다는 것은 다른 문제다.
  → 최신 분기 종료일 기준 경과일수를 계산해 직접 판정한다.

    경과 ≤  45일   신선. 그대로 사용
    경과 ≤ 120일   1개 분기 지연. 사용 가능하나 Insider_Q 기록 필수
    경과 >  120일  2개 분기 이상 지연. 실적 이벤트용으로 부적합

  또한 응답이 **여러 분기를 주는지 최신 1건만 주는지**도 본다.
  여러 분기가 오면 실적일에 가장 가까운 분기를 고를 수 있다.

비용: 총 4콜. 시트·이메일·상태 머신 미접촉. 몇 번을 돌려도 부작용 없음.

실행
────
    TICKERS=AAPL,NVDA,WMT python automation/diag_earnings_cblock_probe2.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_http as fh  # noqa: E402  (sys.path 설정 후에 import)

from calendar import monthrange  # noqa: E402
from datetime import date  # noqa: E402

_env = str(os.environ.get("TICKERS", "") or "").strip()
TICKERS = ([t.strip().upper() for t in _env.split(",") if t.strip()]
           if _env else ["AAPL", "NVDA", "WMT"])

FRESH_D = 45     # 이 이하면 신선
USABLE_D = 120   # 이 이하면 1분기 지연 — 사용 가능

# 설계안이 실제로 읽을 필드. 하나라도 없으면 컬럼 매핑을 다시 짜야 한다.
NEEDED = ("year", "quarter", "acquiredDisposedRatio",
          "totalPurchases", "totalSales")

TODAY = date.today()


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


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _q_end(y, q):
    """분기 종료일. 값이 이상하면 None."""
    y, q = _num(y), _num(q)
    if not y or q not in (1, 2, 3, 4):
        return None
    m = q * 3
    return date(y, m, monthrange(y, m)[1])


def _lag(y, q):
    e = _q_end(y, q)
    return None if e is None else (TODAY - e).days


if not fh.fmp_key():
    print("❌ FMP_API_KEY 없음 — 종료")
    sys.exit(1)

print("=" * 78)
print("C블록 후보 프로브 2차 — 내부자 통계 (경로 정정판)")
print(f"대상 {', '.join(TICKERS)} · 총 {len(TICKERS) + 1}콜 · 기준일 {TODAY}")
print("=" * 78)

# ── B. 거래유형 코드 목록 (전역 1콜) ──────────────────────────────────────
print("\n[B] 거래유형 코드 목록  insider-trading-transaction-type")
data, status, kind = fh.fmp_get_json_ex("insider-trading-transaction-type")
if kind != "ok":
    mark = "플랜 제한(402)" if kind == "plan_limited" else kind
    print(f"    ❌ {mark}  status={status}")
    print("    → 유형 코드를 문서에 적힌 값으로 하드코딩해야 한다.")
else:
    codes = []
    for it in _rows(data):
        c = it.get("transactionType")
        if c:
            codes.append(str(c))
    print(f"    ✅ {len(codes)}종")
    for i in range(0, len(codes), 6):
        print("       " + "  ".join(f"{c:<14}" for c in codes[i:i + 6]))
    print("    → 재량 거래는 P-Purchase / S-Sale. 나머지는 자동·스케줄 거래다.")

# ── A. 내부자 통계 (종목당 1콜) ───────────────────────────────────────────
ok_n = 0
multi_q = 0          # 여러 분기를 준 종목 수
worst_lag = None     # 가장 오래된 지연 (보수적으로 최악값을 본다)
best_lag = None
missing_all = set()

for tk in TICKERS:
    path = f"insider-trading/statistics?symbol={tk}"
    print(f"\n[A] {tk}  {path}")
    data, status, kind = fh.fmp_get_json_ex(path)

    if kind != "ok":
        mark = "플랜 제한(402)" if kind == "plan_limited" else kind
        print(f"    ❌ {mark}  status={status}")
        continue

    rows = _rows(data)
    if not rows:
        print("    ⚠️ 200 인데 0건 — 이 종목 데이터 없음")
        continue

    ok_n += 1
    if len(rows) > 1:
        multi_q += 1

    top = rows[0]
    print(f"    ✅ {len(rows)}건")
    print(f"    키: {', '.join(sorted(top.keys()))}")

    miss = [k for k in NEEDED if k not in top]
    if miss:
        missing_all.update(miss)
        print(f"    ❌ 설계안이 쓸 필드 누락: {', '.join(miss)}")

    lag = _lag(top.get("year"), top.get("quarter"))
    qs = f"{top.get('year')}Q{top.get('quarter')}"
    if lag is None:
        print(f"    ⚠️ 분기 표기 해석 불가: year={top.get('year')} "
              f"quarter={top.get('quarter')}")
    else:
        if worst_lag is None or lag > worst_lag:
            worst_lag = lag
        if best_lag is None or lag < best_lag:
            best_lag = lag
        if lag < 0:
            print(f"    최신 분기 {qs} — 아직 {-lag}일 남음 (**진행 중·미완결**)")
        else:
            tag = ("신선" if lag <= FRESH_D
                   else "1분기 지연" if lag <= USABLE_D else "2분기 이상 지연")
            print(f"    최신 분기 {qs} — 종료 후 {lag}일 경과 ({tag})")

    # 분기별 실제 값 — 설계안 컬럼에 그대로 들어갈 숫자다
    for it in rows[:4]:
        r = it.get("acquiredDisposedRatio")
        r_s = f"{r:.3f}" if isinstance(r, (int, float)) else str(r)
        print(f"      · {it.get('year')}Q{it.get('quarter')}  "
              f"비율 {r_s:>8}  매수 {it.get('totalPurchases')}  "
              f"매도 {it.get('totalSales')}")

# ── 판정 ──────────────────────────────────────────────────────────────────
n = len(TICKERS)
print("\n" + "=" * 78)
print("판정")
print("=" * 78)

if ok_n == 0:
    print("  ❌ insider-trading/statistics 사용 불가.")
    print("     → 1차에서 3/3 생존이 확인된 insider-trading/search (20행 파싱) 로 후퇴.")
    print("       그 경우 유형 필터와 창 길이 보정을 직접 짜야 한다(함정 1·2 부활).")
else:
    print(f"  ✅ 경로 생존 {ok_n}/{n} — 1차의 404 는 경로 오류였음이 확정됐다.")

    if missing_all:
        print(f"  ❌ 필드 누락: {', '.join(sorted(missing_all))} — 컬럼 매핑 재설계 필요.")
    else:
        print("  ✅ 설계안 컬럼 4개가 쓸 필드 모두 존재 "
              "(Insider_AD_Ratio / Sales_N / Purchases_N / Insider_Q).")

    if multi_q:
        print(f"  ✅ {multi_q}/{ok_n} 종목이 복수 분기 반환 — "
              "실적일에 가장 가까운 분기를 고를 수 있다.")
    else:
        print("  ⚠️ 전 종목 1건만 반환 — 최신 분기만 쓸 수 있다. "
              "과거 스냅샷 소급 계산은 불가.")

    if best_lag is not None and best_lag < 0:
        print(f"  ⚠️ 진행 중인 분기를 반환한다 (마감까지 {-best_lag}일). "
              "값이 미완결이다.")
        print("     같은 분기라도 스냅샷 시점마다 totalSales 가 달라진다 — "
              "Insider_Q 만으로는 부족하고")
        print("     '이 분기가 마감됐는가'를 함께 기록해야 백테스트 비교가 성립한다.")

    if worst_lag is None:
        print("  ⚠️ 신선도 판정 불가 — 분기 표기를 해석하지 못했다.")
    elif worst_lag < 0:
        print("  ✅ 지연 없음 — 다만 위의 미완결 문제를 설계에 반영할 것.")
    elif worst_lag <= FRESH_D:
        print(f"  ✅ 신선도 양호 (최악 {worst_lag}일) — 그대로 사용.")
    elif worst_lag <= USABLE_D:
        print(f"  ⚠️ 최대 {worst_lag}일 지연 (최소 {best_lag}일). "
              "사용 가능하나 Insider_Q 컬럼 기록이 필수다.")
    else:
        print(f"  ❌ 최대 {worst_lag}일 지연 — 2개 분기 이상 묵은 자료다.")
        print("     실적 이벤트 직전 스냅샷에 붙일 근거가 약하다. C블록 재검토 대상.")

print("\n" + fh.fmp_stats_line())
print("(진단 스크립트 — 종료 코드는 항상 0)")
