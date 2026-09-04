"""FMP economic-indicators 실측 프로브 v2 — 창 모양과 대체 이름을 확정한다.

v1(2026-09-04 18:38) 이 알아낸 것
─────────────────────────────────
  ✅ from/to 는 **먹는다**. retailSales 에 2023년 창을 주니 2023-11·12 만
     돌아왔다(2/2 창 안). 기본 응답과 겹칠 수 없는 구간이라 우연이 아니다.
  ✅ 그런데 응답이 1~3건으로 잘린다. 창 길이와 건수가 비례하지 않는다:
         retailSales 무창    3건  2025-10 ~ 2025-12
         retailSales 3년창   1건  2026-07-01      ← 최신인데 1건
         retailSales 2023창  2건  2023-11 ~ 2023-12
  ✅ **무창 응답이 낡았다.** 3년창은 2026-07-01(현재)을 줬는데 무창은
     2025-12 에서 멈춘다. FMP 데이터가 낡은 게 아니다.
  ✅ GDP 는 정반대다. 무창 1건(2025-10-01) / 3년창 0건 / 2023창 0건.
  ✅ federalFundsRate 는 무효 — __NOPE__ 와 완전히 같이 행동한다.
     문서에 있는 federalFunds 는 3건 정상(최신 2025-12-01, 3.72).
  ✅ CPI 2건 · unemploymentRate 2건 — app.py 가 요구하는 14·15건에 한참
     못 미친다. **두 함수의 FMP 분기는 한 번도 채택된 적이 없는 죽은 코드**다.

v1 의 결함 — 이 파일이 고치는 것
────────────────────────────────
  ⚠️ v1 의 판정 한 줄이 틀렸다:
         "양성대조(__NOPE__): 응답 없음 → 무효 이름은 비200"
     실제 status 는 **200** 이었다(본문이 JSON 이 아니라 파싱만 실패).
     `_report` 가 bad_json 에 None 을 돌려줬고 판정부가 그 None 을
     "비200" 으로 읽었다. 양성대조를 넣어놓고 결과를 잘못 해석한 것이다.
     → v2 는 status 를 **직접** 인용하고, 빈 배열 / 파싱 실패 / 비200 을
       절대 한 값으로 뭉개지 않는다. `_probe()` 는 항상 dict 를 돌려주고
       `ok` 필드로 성공을 표시한다. 실패 신호로 None 을 쓰지 않는다.
     → 200 + 빈 배열은 `ok=True, n=0, kind='empty'` 다. 실패가 아니라
       "그 창에 데이터가 없다" 는 **사실**이고, 둘은 다른 이야기다.

v2 가 푸는 질문
───────────────
  G) GDP 를 살릴 창이 있는가? 없으면 FRED 폴백을 붙여야 한다.   7콜
  R) 창 길이 ↔ 건수 관계는? MoM 에 필요한 연속 2개월은 어떤 창?  5콜
  C) CPI·실업률·federalFunds 도 최근 창을 주면 현재값이 오는가?  3콜
  ─────────────────────────────────────────────────────────────
  all                                                          15콜

의존성
──────
`fmp_http` 경유(레이트리밋 SSOT). `retries=0` — 재시도가 붙으면 원본
상태코드를 관측할 수 없다.
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


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _probe(name: str, *, d_from: str = "", d_to: str = "") -> dict:
    """항상 dict 를 돌려준다. 실패 신호로 None 을 쓰지 않는 것이 v2 의 핵심이다.

    반환 키:
      ok      200 + JSON 리스트를 받았는가 (빈 배열도 True — 그건 사실이다)
      status  관측된 HTTP 코드 (네트워크 예외면 None)
      kind    fmp_http 분류 또는 'empty' / 'bad_json:*' / 'not_list:*'
      n       레코드 수
      dates   YYYY-MM-DD 리스트
    """
    tail = "economic-indicators?name=" + name
    if d_from:
        tail += "&from=" + d_from
    if d_to:
        tail += "&to=" + d_to
    r, status, kind = fh.fmp_get_ex(BASE + "/" + tail + "&apikey=" + KEY,
                                    timeout=20, retries=0)
    out = {"name": name, "from": d_from, "to": d_to, "ok": False,
           "status": status, "kind": kind, "n": 0, "dates": [], "v0": None}
    if r is None:
        return out
    try:
        d = r.json()
    except Exception as e:
        out["kind"] = "bad_json:" + type(e).__name__
        return out
    if not isinstance(d, list):
        out["kind"] = "not_list:" + type(d).__name__
        out["v0"] = str(d)[:70]
        return out
    ds = [str(x.get("date") or "")[:10] for x in d if isinstance(x, dict)]
    out["dates"] = [x for x in ds if len(x) == 10]
    out["n"] = len(d)
    out["ok"] = True
    if not d:
        out["kind"] = "empty"          # 200 + 빈 배열. 실패가 아니다.
        return out
    if isinstance(d[0], dict):
        out["v0"] = d[0].get("value")
    return out


def _line(label: str, p: dict) -> None:
    win = f"{p['from'] or '·'}~{p['to'] or '·'}"
    if not p["ok"]:
        print(f"  {label:<38} {win:<24} ❌ status={p['status']} kind={p['kind']}")
        return
    if p["n"] == 0:
        print(f"  {label:<38} {win:<24} ⚠️  status=200 · **빈 배열 0건**")
        return
    ds = p["dates"]
    span = f"{min(ds)} ~ {max(ds)}" if ds else "날짜없음"
    print(f"  {label:<38} {win:<24} {p['n']:>2}건  {span}  [0]={p['v0']}")


def _consecutive_months(dates) -> bool:
    """연속 2개월이 있는가 — MoM 계산 가능 여부."""
    ms = sorted({d[:7] for d in dates})
    for i in range(len(ms) - 1):
        a = int(ms[i][:4]) * 12 + int(ms[i][5:7])
        b = int(ms[i + 1][:4]) * 12 + int(ms[i + 1][5:7])
        if b - a == 1:
            return True
    return False


def tier_G():
    """GDP 구제 — 살릴 창이 있는가."""
    print("\n" + "=" * 84)
    print("G) GDP — 3년창·2023창 모두 0건이었다. 살릴 창 모양이 있는가?")
    print("=" * 84)
    print("  창을 여러 모양으로 흔든다: from 만 / to 만 / 짧게 / 길게 / 분기 경계.")
    print("  대체 이름 realGDP 도 같이 본다.")
    print("  전부 1건 이하면 결론은 '창으로는 못 고친다' 이고 FRED 폴백이 답이다.\n")
    out = {}
    cases = [
        ("무창 (v1 재현)",        dict()),
        ("최근 1년",              dict(d_from=_ago(365), d_to=TODAY.isoformat())),
        ("최근 5년",              dict(d_from=_ago(365 * 5), d_to=TODAY.isoformat())),
        ("from 만 (to 없음)",     dict(d_from=_ago(365 * 2))),
        ("to 만 (from 없음)",     dict(d_to=TODAY.isoformat())),
        ("2025-01-01~오늘",       dict(d_from="2025-01-01", d_to=TODAY.isoformat())),
    ]
    for label, kw in cases:
        p = _probe("GDP", **kw)
        _line("GDP · " + label, p)
        out["GDP/" + label] = p
    print()
    p = _probe("realGDP")
    _line("realGDP · 무창", p)
    out["realGDP/무창"] = p
    return out


def tier_R():
    """창 길이 ↔ 건수 관계 — MoM 에 필요한 연속 2개월을 찾는다."""
    print("\n" + "=" * 84)
    print("R) retailSales — 창 길이와 건수의 관계 (MoM 은 연속 2개월이 필요)")
    print("=" * 84)
    print("  v1 실측: 무창 3건(낡음) / 3년창 1건(최신) / 2023창 2건.")
    print("  창이 길수록 건수가 주는 것처럼 보인다 — 계단식으로 확인한다.\n")
    out = {}
    for label, days in (("60일", 60), ("120일", 120), ("365일", 365),
                        ("1095일(3년)", 1095)):
        p = _probe("retailSales", d_from=_ago(days), d_to=TODAY.isoformat())
        _line(f"retailSales · 최근 {label}", p)
        out["최근 " + label] = p
    p = _probe("retailSales", d_from="2026-01-01", d_to=TODAY.isoformat())
    _line("retailSales · 2026-01-01~오늘", p)
    out["2026-01-01~오늘"] = p
    print()
    for label, p in out.items():
        if p["ok"] and p["n"]:
            c = _consecutive_months(p["dates"])
            print(f"      {label:<20} 연속 2개월 "
                  f"{'✅ 있음 — MoM 계산 가능' if c else '❌ 없음'}")
    return out


def tier_C():
    """CPI·실업률·federalFunds 도 최근 창을 주면 현재값이 오는가."""
    print("\n" + "=" * 84)
    print("C) CPI · unemploymentRate · federalFunds — 최근 3년창 대조")
    print("=" * 84)
    print("  v1 은 전부 무창으로만 쟀고 2~3건에 그쳤다. retailSales 처럼")
    print("  '무창만 낡은' 것이라면 창을 주면 현재값이 와야 한다.")
    print("  ⚠️ app.py 요구: CPI 14건 · unemploymentRate 15건 · fedfunds 24건.\n")
    out = {}
    for nm, need in (("CPI", 14), ("unemploymentRate", 15), ("federalFunds", 24)):
        p = _probe(nm, d_from=_ago(365 * 3), d_to=TODAY.isoformat())
        _line(f"{nm} · 최근 3년", p)
        p["need"] = need
        out[nm] = p
        if p["ok"] and p["n"]:
            print(f"      └ {p['n']}건 vs app.py 요구 {need}건 — "
                  f"{'✅ 충족' if p['n'] >= need else '❌ 부족 → FMP 분기는 여전히 죽은 코드'}")
    return out


def main() -> int:
    print("=" * 84)
    print("FMP economic-indicators 실측 프로브 v2")
    print(f"티어: {TIER}   오늘: {TODAY.isoformat()}")
    print("=" * 84)
    if not KEY:
        print("\n❌ FMP_API_KEY 없음 — 네트워크를 만지지 않고 종료한다.")
        return 1

    G = R = C = None
    if TIER in ("all", "G"):
        G = tier_G()
    if TIER in ("all", "R"):
        R = tier_R()
    if TIER in ("all", "C"):
        C = tier_C()

    print("\n" + "=" * 84)
    print("판정")
    print("=" * 84)
    print("⚠️ status 를 직접 인용한다. v1 은 '응답 없음' 을 '비200' 으로 읽어")
    print("   틀린 결론을 냈다. 200+빈배열 / 200+파싱실패 / 비200 은 다 다르다.\n")

    if G:
        print("  ── GDP")
        for k, p in G.items():
            mark = "○" if (p["ok"] and p["n"]) else ("·" if p["ok"] else "✗")
            print(f"     {mark} {k:<26} status={p['status']} "
                  f"kind={p['kind']} n={p['n']}")
        gdp = {k: p for k, p in G.items()
               if k.startswith("GDP/") and p["ok"] and p["n"] >= 2}
        if gdp:
            best = sorted(gdp, key=lambda x: -gdp[x]["n"])[0]
            print(f"     ➜ ✅ QoQ 가능한 창이 있다: {best} ({gdp[best]['n']}건)")
        elif any(p["ok"] and p["n"] for k, p in G.items() if k.startswith("GDP/")):
            print("     ➜ ⚠️ 응답은 오지만 전부 1건 이하 — QoQ 계산 불가.")
            print("        창으로는 못 고친다. FRED(GDPC1) 폴백을 붙여야 한다.")
        else:
            print("     ➜ ❌ 어떤 창에서도 데이터가 없다. FRED 폴백이 유일한 답이다.")
        rg = G.get("realGDP/무창")
        if rg:
            tail = "  ← 대체 이름 후보" if (rg["ok"] and rg["n"] >= 2) else ""
            print(f"     realGDP 무창: status={rg['status']} kind={rg['kind']} "
                  f"n={rg['n']}{tail}")

    if R:
        print("\n  ── retailSales 창↔건수")
        for k, p in R.items():
            ds = p["dates"]
            span = f"{min(ds)}~{max(ds)}" if ds else "-"
            print(f"     {k:<20} n={p['n']:<3} {span:<26}"
                  f" 연속2개월={'예' if (ds and _consecutive_months(ds)) else '아니오'}")
        good = [k for k, p in R.items()
                if p["ok"] and p["dates"] and _consecutive_months(p["dates"])]
        print("     ➜ " + (f"✅ MoM 가능한 창: {', '.join(good)}" if good
                          else "❌ 어떤 창에서도 연속 2개월이 안 온다 — MoM 포기 또는 FRED."))

    if C:
        print("\n  ── CPI · unemploymentRate · federalFunds (최근 3년창)")
        for nm, p in C.items():
            ds = p["dates"]
            span = f"{min(ds)}~{max(ds)}" if ds else "-"
            enough = "✅" if p["n"] >= p.get("need", 0) else "❌"
            print(f"     {nm:<20} n={p['n']:<3}/{p.get('need','?'):<3} {enough} "
                  f"{span:<26} kind={p['kind']}")

    print("\n" + "=" * 84)
    print("이 프로브는 설계하지 않는다 — 사실만 찍는다. 설계는 결과를 보고 한다.")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
