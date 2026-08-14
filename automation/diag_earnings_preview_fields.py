"""diag_earnings_preview_fields.py — 실적 프리뷰 브리핑(2단계) 필드 스키마 프로브.

존재 이유
─────────
2단계 브리핑은 아직 이 코드베이스가 한 번도 읽어본 적 없는 필드에 의존한다.
특히 **매출 컨센서스**가 그렇다 — `analyst-estimates` 응답에서 매출 추정치가
어떤 키로 오는지(revenueAvg? estimatedRevenueAvg? 아예 없나?) 확인된 바 없다.

"코드에 호출부가 있다 ≠ 그 경로가 이 플랜에서 동작한다" 로 이미 세 번 틀렸다:
  · nasdaq-constituent      — stable 에 존재하지 않음
  · /etf/holdings           — 이 플랜에서 HTTP 402
  · earnings-calendar?symbol= — symbol 파라미터 자체가 없음(시장 전체 전용)

그래서 스키마를 **추정하지 않고 먼저 찍어본다.** 이 스크립트는 진단 전용이며
시트에 아무것도 쓰지 않고 상태 머신도 건드리지 않는다.

읽는 법
───────
엔드포인트마다 세 가지를 본다.
  1) kind   — ok / plan_limited(402) / http_error / no_key
  2) 키 목록 — 응답 첫 항목의 실제 키 (여기에 없는 필드는 존재하지 않는 것)
  3) 기대 필드표 — 2단계에서 쓰려는 필드가 실제로 값을 갖는지

⚠️ `값 없음`과 `키 없음`은 다르다. 키가 있는데 null 이면 그 종목만의 문제일 수
   있으니 티커를 늘려 재확인할 것. 키 자체가 없으면 설계를 바꿔야 한다.

실행
────
    TICKERS=AAPL,NVDA,WMT python automation/diag_earnings_preview_fields.py

TICKERS 미지정 시 기본값(대형주 3종목)을 쓴다. 종목당 6콜, 기본 18콜.
"""
import json
import os
import sys

# automation/ 에서 실행되든 루트에서 실행되든 공용 모듈을 찾게 한다(기존 diag 관례).
# python automation/xxx.py 로 실행하면 sys.path[0] 이 automation/ 이라
# repo 루트의 fmp_http 를 찾지 못한다 — diag_market_gate.py 와 동일한 처리.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_http as fh  # noqa: E402  (sys.path 설정 후에 import 해야 한다)

# ── 대상 ──────────────────────────────────────────────────────────────────
_env = str(os.environ.get("TICKERS", "") or "").strip()
TICKERS = ([t.strip().upper() for t in _env.split(",") if t.strip()]
           if _env else ["AAPL", "NVDA", "WMT"])

# ── 프로브 정의 ───────────────────────────────────────────────────────────
#   (라벨, 경로 템플릿, 블록, [기대 필드], 비고)
#   기대 필드는 "후보군"이다 — 하나라도 맞으면 그 항목은 확보된 것.
PROBES = [
    ("analyst-estimates (EPS·매출 컨센서스)",
     "analyst-estimates?symbol={tk}&period=quarter&limit=4", "A블록",
     ["epsAvg", "epsEstimated", "estimatedEpsAvg",
      "revenueAvg", "revenueLow", "revenueHigh",
      "estimatedRevenueAvg", "revenue"],
     "★ 매출 컨센서스 키가 이번 프로브의 핵심 미지수"),

    ("price-target-consensus (목표주가)",
     "price-target-consensus?symbol={tk}", "A블록",
     ["targetConsensus", "targetMean", "targetMedian", "targetHigh", "targetLow"],
     "app.py 6195행이 이미 사용 중 — 회귀 확인 목적"),

    ("grades-historical (매수의견 비율)",
     "grades-historical?symbol={tk}&limit=12", "B블록",
     ["date", "analystRatingsStrongBuy", "analystRatingsBuy",
      "analystRatingsHold", "analystRatingsSell", "analystRatingsStrongSell"],
     "월별 의견 '분포' 스냅샷. previousGrade/newGrade/action 은 없는 것이 정상"),

    ("earnings (과거 서프라이즈)",
     "earnings?symbol={tk}&limit=12", "B블록",
     ["date", "epsActual", "epsEstimated"],
     "beat율 + 평균 서프라이즈 폭(공매도 대체 요인)의 원천"),

    ("news/stock (뉴스 증분)",
     "news/stock?symbols={tk}&limit=10", "C블록",
     ["title", "publishedDate", "site", "symbol", "text", "url"],
     "종목별 뉴스. narrative_core 의 시장 전체 경로와는 다른 엔드포인트"),

    ("earning-call-transcript-dates (3단계 사전확인)",
     "earning-call-transcript-dates?symbol={tk}", "3단계",
     ["quarter", "year", "date"],
     "지금은 안 쓰지만 3단계가 여기 의존 — 플랜 제공 여부를 미리 확인"),
]


def _first_item(data):
    """응답에서 대표 항목 1개를 꺼낸다. list / dict / list[list] 모두 대응."""
    if isinstance(data, list):
        if not data:
            return None, 0
        return data[0], len(data)
    if isinstance(data, dict):
        for k in ("historical", "data", "results"):
            v = data.get(k)
            if isinstance(v, list) and v:
                return v[0], len(v)
        return data, 1
    return None, 0


def _keys_of(item):
    if isinstance(item, dict):
        return sorted(item.keys())
    if isinstance(item, list):
        return [f"<list[{len(item)}]>"]
    return [f"<{type(item).__name__}>"]


def _preview(v, width: int = 48):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = " ".join(str(s).split())
    return s if len(s) <= width else s[:width - 1] + "…"


# ── 실행 ──────────────────────────────────────────────────────────────────
if not fh.fmp_key():
    print("❌ FMP_API_KEY 없음 — 종료")
    sys.exit(1)

print("=" * 78)
print(f"실적 프리뷰 필드 프로브 — 대상 {', '.join(TICKERS)}")
print("=" * 78)

# {(라벨, 필드): 값을 실제로 받은 티커 수}
seen_field = {}
# {라벨: [(티커, kind, 건수)]}
seen_call = {}

for label, path_t, block, expect, note in PROBES:
    print(f"\n{'─' * 78}")
    print(f"▶ [{block}] {label}")
    if note:
        print(f"  {note}")
    print(f"{'─' * 78}")

    for tk in TICKERS:
        path = path_t.format(tk=tk)
        data, status, kind = fh.fmp_get_json_ex(path)
        seen_call.setdefault(label, []).append((tk, kind, 0))

        if kind != "ok":
            print(f"  {tk:6} ❌ {kind} (status={status})  {path}")
            continue

        item, n = _first_item(data)
        seen_call[label][-1] = (tk, kind, n)
        if item is None:
            print(f"  {tk:6} ⚠️  응답은 200 인데 항목 0건 — 경로는 살아 있으나 데이터 없음")
            continue

        print(f"  {tk:6} ✅ {n}건")
        print(f"         키: {', '.join(_keys_of(item))}")

        if isinstance(item, dict):
            for f in expect:
                if f in item:
                    v = item.get(f)
                    mark = "·" if v is None else "✓"
                    if v is not None:
                        seen_field[(label, f)] = seen_field.get((label, f), 0) + 1
                    print(f"         {mark} {f:26} = {_preview(v)}")

# ── 요약 ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 78)
print("요약 — 기대 필드별 확보 상황 (값을 실제로 받은 티커 수 / 전체)")
print("=" * 78)

n_tk = len(TICKERS)
blocking = []
for label, path_t, block, expect, note in PROBES:
    calls = seen_call.get(label, [])
    ok_n = sum(1 for _, k, _ in calls if k == "ok")
    head = f"[{block}] {label}"
    if ok_n == 0:
        print(f"\n❌ {head} — 전 종목 호출 실패. 이 블록은 설계 변경 필요")
        blocking.append(head)
        continue

    print(f"\n{head}  (호출 성공 {ok_n}/{n_tk})")
    got_any = False
    for f in expect:
        c = seen_field.get((label, f), 0)
        if c:
            got_any = True
            print(f"   ✓ {f:28} {c}/{n_tk}")
        else:
            print(f"   ✗ {f:28} 없음")
    if not got_any:
        print("   ⚠️  기대 필드 중 값을 받은 것이 하나도 없음 — 키 목록을 직접 확인할 것")
        blocking.append(head)

print("\n" + "=" * 78)
print(fh.fmp_stats_line())
print("=" * 78)

if blocking:
    print("\n⚠️ 설계 재검토가 필요한 항목:")
    for b in blocking:
        print(f"   · {b}")
    print("\n(진단 스크립트이므로 종료 코드는 0 — 워크플로를 실패로 만들지 않는다)")
else:
    print("\n✅ 모든 블록에서 최소 한 개 이상의 기대 필드를 확보했습니다.")
