"""FMP economic-indicators 실측 프로브 — 창(from/to)과 지표 이름을 확정한다.

풀려는 문제
───────────
app.py 매크로 대시보드에서 `economic-indicators` 를 쓰는 지표 둘만 낡았다.
2026-09-04 화면 실측:

    GDP 성장률 QoQ    N/A   $31,423B (2025-10-01)   ← 3분기 낡음, prev 없음
    소매판매 MoM      +0.06%      (2025-12-01)      ← 8개월 낡음, prev 있음

같은 엔드포인트인데 **한쪽은 prev 가 있고 한쪽은 없다.** 나머지 지표
(금리차·VIX·WTI·DXY·ERP)는 전부 다른 엔드포인트이고 값이 현재다.

app.py 는 `name` 만 보내고 `from`/`to` 를 안 보낸다. api-docs 는 둘 다
지원한다고 적어놨다. 하지만 **"창을 안 보내서 그렇다"는 아직 가설이다.**

왜 프로브를 먼저 하나
────────────────────
`historical-price-eod` 의 `limit=` 이 조용히 무시되던 사고에서 배운 게 있다:
추측으로 창 크기를 정하면 같은 실수를 반복한다. 그때 얻은 규칙이

    "파라미터가 먹었는지 확인할 땐, **파라미터가 작동해야만 성립하는
     통계**를 고른다. 레코드 수와 시드 포함 여부는 이 시험을 통과하지 못한다."

레코드 수는 이 시험을 통과하지 못한다 — 창을 넣었더니 늘어난 것처럼 보여도
기본 응답이 원래 그만큼이었을 수 있다. 그래서 D2 는 **기본 응답과 겹칠 수
없는 과거 창**(2023년)을 요청하고, 돌아온 날짜가 실제로 그 창 안인지 본다.
2023년 날짜가 오면 파라미터는 확실히 먹은 것이다. 그것 말고는 설명이 없다.

FMP 콜 예산
───────────
    W (창 검증)     6콜   GDP·retailSales 각각 무창/넓은창/과거창
    N (이름 검증)   4콜   federalFunds vs federalFundsRate + 무효 이름 대조
    S (나머지 지표) 3콜   CPI · unemploymentRate · consumerSentiment 무창
    ─────────────────────
    all            13콜

의존성
──────
`fmp_http` 를 경유한다. 다른 diag_* 프로브들은 프로젝트 모듈을 일부러
import 하지 않지만(사본 신선도와 무관하게 만들려고), 이 프로브는 성격이
다르다 — fmp_http 를 시험하는 게 아니라 **FMP 의 응답 의미**를 시험한다.
경유하면 레이트리밋이 공짜로 붙고 A1 부채도 늘지 않는다.
⚠️ `retries=0` 이다. 재시도가 붙으면 429/4xx 원본 상태를 관측할 수 없다.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fmp_http as fh  # noqa: E402

BASE = "https://financialmodelingprep.com/stable"
KEY = os.environ.get("FMP_API_KEY", "").strip()
TIER = (os.environ.get("PROBE_TIER") or "all").strip()

TODAY = date.today()


def _get(url_tail: str):
    """(레코드리스트|None, status, kind) — 200 이 아니면 첫 원소가 None."""
    sep = "&" if "?" in url_tail else "?"
    r, status, kind = fh.fmp_get_ex(
        BASE + "/" + url_tail + sep + "apikey=" + KEY, timeout=20, retries=0)
    if r is None:
        return None, status, kind
    try:
        d = r.json()
    except Exception as e:
        return None, status, "bad_json:" + type(e).__name__
    return d, status, kind


def _dates(recs):
    out = []
    for x in recs or []:
        if isinstance(x, dict):
            ds = str(x.get("date") or "")[:10]
            if len(ds) == 10:
                out.append(ds)
    return out


def _report(label, recs, status, kind, *, expect_window=None):
    if recs is None:
        print(f"  {label:<44} ❌ status={status} kind={kind}")
        return None
    if not isinstance(recs, list):
        print(f"  {label:<44} ⚠️  리스트 아님: {type(recs).__name__} "
              f"{str(recs)[:80]}")
        return None
    ds = _dates(recs)
    if not ds:
        print(f"  {label:<44} ⚠️  {len(recs)}건인데 date 필드 없음")
        return None
    lo, hi = min(ds), max(ds)
    v0 = None
    if isinstance(recs[0], dict):
        v0 = recs[0].get("value")
    line = (f"  {label:<44} {len(recs):>4}건  최신 {hi}  최고(古) {lo}"
            f"  [0]={v0}")
    if expect_window:
        w_from, w_to = expect_window
        inside = [d for d in ds if w_from <= d <= w_to]
        ok = len(inside) == len(ds) and len(ds) > 0
        line += f"\n      └ 창 {w_from}~{w_to} 안: {len(inside)}/{len(ds)} " \
                f"{'✅ 파라미터 먹음' if ok else '❌ 창 밖 날짜 있음 — 무시된 것'}"
    print(line)
    return {"n": len(recs), "latest": hi, "oldest": lo, "dates": ds}


def tier_W():
    """창 검증 — 이 프로브의 핵심."""
    print("\n" + "=" * 78)
    print("W) from/to 가 실제로 먹는가 — GDP · retailSales")
    print("=" * 78)
    print("  ⚠️ W3/W6 이 판정 기준이다. 기본 응답과 **겹칠 수 없는** 2023년 창을")
    print("     요청한다. 2023년 날짜가 돌아오면 파라미터는 확실히 먹은 것이다.")
    print("     레코드 수만으로는 판정하지 않는다 — 우연히 늘어난 것과 구별 못 한다.\n")

    wide_from = (TODAY - timedelta(days=365 * 3)).isoformat()
    wide_to = TODAY.isoformat()
    past = ("2023-01-01", "2023-12-31")

    res = {}
    for name in ("GDP", "retailSales"):
        print(f"  ── {name} " + "─" * (60 - len(name)))
        d, s, k = _get(f"economic-indicators?name={name}")
        res[(name, "none")] = _report(f"{name} · 파라미터 없음 (지금 app.py)",
                                      d, s, k)
        d, s, k = _get(f"economic-indicators?name={name}"
                       f"&from={wide_from}&to={wide_to}")
        res[(name, "wide")] = _report(f"{name} · 넓은 창 3년", d, s, k)
        d, s, k = _get(f"economic-indicators?name={name}"
                       f"&from={past[0]}&to={past[1]}")
        res[(name, "past")] = _report(f"{name} · 과거 창 2023년 ★판정", d, s, k,
                                      expect_window=past)
        print()
    return res


def tier_N():
    """이름 검증 — federalFunds vs federalFundsRate."""
    print("\n" + "=" * 78)
    print("N) 지표 이름 — app.py 는 federalFundsRate 를 쓰는데 문서에는 없다")
    print("=" * 78)
    print("  ⚠️ 양성대조 포함. 무효 이름을 넣었을 때 FMP 가 무엇을 하는지 먼저")
    print("     봐야 federalFundsRate 결과를 해석할 수 있다. 빈 배열 200 을")
    print("     돌려주는 API 라면 '200 이니까 유효'는 틀린 추론이다.\n")
    out = {}
    for nm, note in (("federalFunds", "문서에 있는 이름"),
                     ("federalFundsRate", "app.py:4086·4110 이 쓰는 이름"),
                     ("__NOPE__", "양성대조 — 확실히 무효"),
                     ("consumerSentiment", "app.py 독스트링은 consumerConfidence")):
        d, s, k = _get(f"economic-indicators?name={nm}")
        out[nm] = _report(f"{nm}  ({note})", d, s, k)
    return out


def tier_S():
    """나머지 지표 — FRED 폴백에 가려 안 보이던 것들."""
    print("\n" + "=" * 78)
    print("S) FRED 폴백이 가리고 있는 지표들")
    print("=" * 78)
    print("  _fetch_cpi_series 는 14건, _fetch_unrate_series 는 15건,")
    print("  _hist_fetch_fedfunds 는 24건이 있어야 FMP 경로를 채택한다.")
    print("  모자라면 조용히 FRED 로 떨어진다 — 화면에 아무 표시가 없다.\n")
    out = {}
    for nm, need in (("CPI", 14), ("unemploymentRate", 15),
                     ("totalNonfarmPayroll", 0)):
        d, s, k = _get(f"economic-indicators?name={nm}")
        r = _report(f"{nm}  (필요 {need}건)" if need else f"{nm}", d, s, k)
        if r and need:
            ok = r["n"] >= need
            print(f"      └ {r['n']}건 vs 필요 {need}건 — "
                  f"{'✅ FMP 경로 채택 가능' if ok else '❌ 부족 → 항상 FRED 폴백'}")
        out[nm] = r
    return out


def main() -> int:
    print("=" * 78)
    print("FMP economic-indicators 실측 프로브")
    print(f"티어: {TIER}   오늘: {TODAY.isoformat()}")
    print("=" * 78)
    if not KEY:
        print("\n❌ FMP_API_KEY 없음 — 네트워크를 만지지 않고 종료한다.")
        return 1

    W = N = S = None
    if TIER in ("all", "W"):
        W = tier_W()
    if TIER in ("all", "N"):
        N = tier_N()
    if TIER in ("all", "S"):
        S = tier_S()

    print("\n" + "=" * 78)
    print("판정")
    print("=" * 78)
    if W:
        for name in ("GDP", "retailSales"):
            none_r, wide_r, past_r = (W.get((name, "none")),
                                      W.get((name, "wide")),
                                      W.get((name, "past")))
            print(f"\n  ── {name}")
            if none_r:
                print(f"     무창    {none_r['n']}건 · 최신 {none_r['latest']}")
            if wide_r:
                print(f"     3년창   {wide_r['n']}건 · 최신 {wide_r['latest']}")
            if past_r:
                inside = all("2023-01-01" <= d <= "2023-12-31"
                             for d in past_r["dates"])
                print(f"     2023창  {past_r['n']}건 · {past_r['oldest']}"
                      f"~{past_r['latest']}")
                print(f"     ➜ from/to 파라미터: "
                      f"{'✅ 먹는다 (2023 날짜만 돌아왔다)' if inside else '❌ 무시된다'}")
            else:
                print("     ➜ 2023창 응답 없음 — 판정 불가")
            if none_r and wide_r and none_r["latest"] == wide_r["latest"]:
                print(f"     ⚠️ 무창과 3년창의 최신 날짜가 같다({none_r['latest']}).")
                print("        창은 과거를 넓힐 뿐 최신을 앞당기지 못한다는 뜻 —")
                print("        **FMP 쪽 데이터가 낡은 것**이고 창 추가로는 못 고친다.")
    if N:
        a, b, ctl = N.get("federalFunds"), N.get("federalFundsRate"), N.get("__NOPE__")
        print("\n  ── 지표 이름")
        if ctl is None:
            print("     양성대조(__NOPE__): 응답 없음 → 무효 이름은 비200. "
                  "따라서 200 = 유효로 읽어도 된다.")
        else:
            print(f"     ⚠️ 양성대조(__NOPE__)가 {ctl['n']}건을 돌려줬다 — "
                  "무효 이름도 200 이 온다.")
            print("        'federalFundsRate 가 200 이니 유효' 라는 추론은 무효다.")
        for nm, r in (("federalFunds", a), ("federalFundsRate", b)):
            print(f"     {nm:<20} " + ("응답 없음" if r is None
                                       else f"{r['n']}건 · 최신 {r['latest']}"))
        if a and b and a["latest"] == b["latest"] and a["n"] == b["n"]:
            print("     ➜ 둘이 동일한 응답 — 별칭이다. app.py 수정 불필요.")
        elif a and not b:
            print("     ➜ federalFundsRate 는 무효. app.py:4086·4110 을 "
                  "federalFunds 로 고쳐야 한다.")
    print("\n" + "=" * 78)
    print("이 프로브는 판정하지 않는다 — 사실만 찍는다. 설계는 결과를 보고 한다.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
