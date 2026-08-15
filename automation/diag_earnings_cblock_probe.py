"""diag_earnings_cblock_probe.py — 3단계 C블록 대체 후보 생존 확인 (3차 프로브).

앞선 프로브에서 이미 확정된 것 (재확인하지 않는다)
──────────────────────────────────────────────────
  ❌ earning-call-transcript-dates / earning-call-transcript  — HTTP 402
  ❌ analyst-estimates                                        — HTTP 402
  ✅ earnings?symbol= / price-target-consensus
     / grades-historical / news/stock?symbols=

C블록 원안(직전 어닝콜 트랜스크립트 요약)이 402 로 불가능해졌다.
이 스크립트는 **대체 후보가 이 요금제에서 실제로 살아 있는지**만 본다.
설계 확정 전이므로 되돌릴 것이 없다.

무엇을 보는가
─────────────
  다-1  news/press-releases?symbols=   회사 원문 보도자료.
                                       어닝 보도자료는 콜이 부연하는 원문서다.
                                       → **본문 필드 존재 여부와 길이**가 핵심
  다-2  insider-trading/search         실적 전 내부자 순매수/매도 (수치)
  다-2b insider-trade-statistics       위의 집계판. 콜을 아낄 수 있으면 이쪽
  다-3  sec-filings-search/symbol      실적 전 8-K 건수/유형 (수치)
  (나)  news/stock?symbols=            **본문(text) 필드가 정말 오는가**

마지막 항목이 중요하다. 핸드오프의 (나)안은 news/stock 에 본문이 있다고
전제하지만, 현재 earnings_core.fetch_stock_news 는 date/title/site/url 넷만
뽑는다. 본문 필드는 **아무도 확인한 적이 없다.**
("호출부가 있다 ≠ 이 플랜에서 동작한다" 로 이미 여러 번 틀렸다.)

판정 기준
─────────
  402                 → 이 플랜에서 불가능. 후보에서 제거
  200 + 0건           → 경로는 살아 있으나 해당 종목 데이터 없음.
                        전 종목 0건이면 사실상 사용 불가
  200 + 본문 없음     → 서술 후보(다-1 / 나)로는 탈락.
                        제목만으로는 트랜스크립트 대체가 안 된다
  200 + 본문 N자      → 서술 후보 성립. N 이 셀 예산(49,000자)과 요약 비용을 결정

비용: 종목당 5콜. 기본 3종목 = 15콜.
시트 접근·이메일·상태 머신 접촉 없음. 몇 번을 돌려도 부작용이 없다.

실행
────
    TICKERS=AAPL,NVDA,WMT python automation/diag_earnings_cblock_probe.py
"""
import os
import sys

# automation/ 에서 실행되든 루트에서 실행되든 공용 모듈을 찾게 한다(기존 diag 관례).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_http as fh  # noqa: E402  (sys.path 설정 후에 import 해야 한다)

from datetime import date, timedelta  # noqa: E402

_env = str(os.environ.get("TICKERS", "") or "").strip()
TICKERS = ([t.strip().upper() for t in _env.split(",") if t.strip()]
           if _env else ["AAPL", "NVDA", "WMT"])

ROWS_SHOWN = 4        # 종목당 표시할 행 수
BODY_HEAD = 160       # 본문 미리보기 길이
# 이 길이 미만이면 전문이 아니라 발췌(blurb)로 본다. 발췌를 요약해봐야
# 트랜스크립트 대체가 되지 않는다 — 이 프로브가 막아야 할 오독이 바로 이것이다.
BODY_MIN_USEFUL = 400
FILING_WINDOW = 120   # SEC 공시 조회 창(일)

# 본문일 가능성이 있는 키 — FMP 가 표기를 바꿔도 잡히도록 넓게 둔다.
BODY_KEYS = ("text", "content", "body", "article", "snippet", "description")

_TO = date.today()
_FROM = _TO - timedelta(days=FILING_WINDOW)


def _s(v, n=0):
    t = "" if v is None else str(v)
    return t[:n] if n else t


def _rows(data):
    """리스트든 {data:[...]} 든 행 목록으로 정규화."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("data", "results", "content"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        return [data]
    return []


def _body_field(item):
    """(키, 길이) — 본문으로 쓸 만한 가장 긴 문자열 필드. 없으면 (None, 0)."""
    best, best_n = None, 0
    for k in BODY_KEYS:
        v = item.get(k)
        if isinstance(v, str) and len(v) > best_n:
            best, best_n = k, len(v)
    return best, best_n


def _preview(t):
    one = " ".join(_s(t).split())
    return one[:BODY_HEAD] + ("…" if len(one) > BODY_HEAD else "")


# ── 후보 정의 ─────────────────────────────────────────────────────────────
# (코드, 이름, 경로 템플릿, 본문을 봐야 하는가)
PROBES = [
    ("다-1", "보도자료 news/press-releases",
     "news/press-releases?symbols={tk}&limit=5", True),
    ("다-2", "내부자거래 insider-trading/search",
     "insider-trading/search?symbol={tk}&page=0&limit=20", False),
    ("다-2b", "내부자거래 집계 insider-trade-statistics",
     "insider-trade-statistics?symbol={tk}", False),
    ("다-3", "SEC 공시 sec-filings-search/symbol",
     "sec-filings-search/symbol?symbol={tk}&from=" + _FROM.strftime("%Y-%m-%d")
     + "&to=" + _TO.strftime("%Y-%m-%d") + "&page=0&limit=20", False),
    ("(나)", "뉴스 본문 news/stock",
     "news/stock?symbols={tk}&limit=3", True),
]

if not fh.fmp_key():
    print("❌ FMP_API_KEY 없음 — 종료")
    sys.exit(1)

print("=" * 78)
print("3단계 C블록 대체 후보 생존 확인")
print(f"대상 {', '.join(TICKERS)} · 종목당 5콜 · 총 {len(TICKERS) * len(PROBES)}콜")
print(f"SEC 공시 창 {_FROM} ~ {_TO}")
print("=" * 78)

# 집계: code → {ok, rows, body_key, body_max, kinds}
tally = {c: {"ok": 0, "rows": 0, "body_max": 0, "body_key": None, "kinds": {}}
         for c, _n, _p, _b in PROBES}

for tk in TICKERS:
    print(f"\n{'=' * 78}")
    print(f"▶ {tk}")
    print("=" * 78)

    for code, name, tmpl, want_body in PROBES:
        path = tmpl.format(tk=tk)
        data, status, kind = fh.fmp_get_json_ex(path)
        agg = tally[code]
        agg["kinds"][kind] = agg["kinds"].get(kind, 0) + 1

        print(f"\n  [{code}] {name}")
        print(f"       {path}")

        if kind != "ok":
            mark = "❌ 플랜 제한(402)" if kind == "plan_limited" else f"❌ {kind}"
            print(f"       {mark}  status={status}")
            continue

        rows = _rows(data)
        agg["ok"] += 1
        agg["rows"] += len(rows)

        if not rows:
            print("       ⚠️ 200 인데 0건 — 경로는 살아 있으나 이 종목 데이터 없음")
            continue

        print(f"       ✅ {len(rows)}건")
        print(f"       키: {', '.join(sorted(rows[0].keys()))}")

        bk, bn = _body_field(rows[0])
        if bn > agg["body_max"]:
            agg["body_max"] = bn
            agg["body_key"] = bk

        if want_body:
            if bk is None:
                print("       ⚠️ 본문 후보 필드 없음 — 제목만 옴")
            else:
                print(f"       본문 필드 '{bk}' — {bn:,}자")
                print(f"       미리보기: {_preview(rows[0].get(bk))}")

        # 행 요약 — 계열별로 볼 것이 다르다
        for it in rows[:ROWS_SHOWN]:
            if code in ("다-1", "(나)"):
                d = _s(it.get("date") or it.get("publishedDate"), 10)
                print(f"         · {d:10}  {_s(it.get('title'), 62)}")
            elif code.startswith("다-2"):
                d = _s(it.get("filingDate") or it.get("transactionDate")
                       or it.get("date"), 10)
                who = _s(it.get("reportingName") or it.get("symbol"), 22)
                typ = _s(it.get("transactionType") or it.get("acquisitionOrDisposition")
                         or "", 12)
                qty = it.get("securitiesTransacted")
                qty_s = "" if qty is None else f"{qty:,}" if isinstance(qty, (int, float)) else _s(qty, 12)
                print(f"         · {d:10}  {who:22} {typ:12} {qty_s}")
            else:  # 다-3
                d = _s(it.get("filedDate") or it.get("acceptedDate")
                       or it.get("date"), 10)
                ft = _s(it.get("formType") or it.get("type"), 10)
                print(f"         · {d:10}  {ft}")

# ── 판정 ──────────────────────────────────────────────────────────────────
n = len(TICKERS)
print("\n" + "=" * 78)
print("판정")
print("=" * 78)
print(f"  {'후보':6} {'이름':34} {'성공':>6} {'총건수':>7}  본문")
print("  " + "-" * 74)
for code, name, _t, want_body in PROBES:
    a = tally[code]
    if want_body:
        body = "없음" if a["body_max"] == 0 else f"{a['body_key']} {a['body_max']:,}자"
    else:
        body = "—"
    print(f"  {code:6} {name[:34]:34} {a['ok']:>4}/{n} {a['rows']:>7}  {body}")

print()
for code, name, _t, want_body in PROBES:
    a = tally[code]
    dead = a["kinds"].get("plan_limited", 0)
    if dead == n:
        print(f"  {code} ❌ 전 종목 402 — 이 플랜에서 불가능. 후보에서 제거.")
    elif a["ok"] == 0:
        ks = ", ".join(f"{k}({v})" for k, v in sorted(a["kinds"].items()))
        print(f"  {code} ❌ 호출 실패 ({ks}) — 경로·파라미터 규약 재확인 필요.")
    elif a["rows"] == 0:
        print(f"  {code} ⚠️ 200 이나 전 종목 0건 — 사실상 사용 불가.")
    elif want_body and a["body_max"] == 0:
        print(f"  {code} ⚠️ 본문 없음 — 서술 후보로는 탈락. "
              f"제목만으로 트랜스크립트를 대체할 수 없다.")
    elif want_body and a["body_max"] < BODY_MIN_USEFUL:
        print(f"  {code} ⚠️ 본문이 {a['body_max']:,}자뿐 — 전문이 아니라 발췌다. "
              f"요약해도 트랜스크립트 대체가 안 된다. 사실상 탈락.")
    elif want_body:
        print(f"  {code} ✅ 본문 {a['body_max']:,}자 확보 — 서술 후보 성립. "
              f"Gemini 요약 + GOOGLE_API_KEY 추가가 전제.")
    else:
        print(f"  {code} ✅ 수치 확보 — Gemini 불필요. 셀 예산 압박 없음. "
              f"Pre_Ret_D1/D3/D7 로 백테스트 가능.")

if tally["다-2"]["ok"] and tally["다-2b"]["ok"]:
    print("\n  · 다-2 와 다-2b 가 둘 다 살아 있다 — 집계판(2b)이 1콜로 끝나면 그쪽이 싸다.")

print("\n" + fh.fmp_stats_line())
print("(진단 스크립트 — 종료 코드는 항상 0)")
