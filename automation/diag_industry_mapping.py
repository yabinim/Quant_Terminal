"""diag_industry_mapping.py — `profile.industry` ↔ `available-industries` 매칭 검증.

무엇을 확인하나
───────────────
Phase 3 화면은 **종목의 업종명으로 순위표를 조회**한다.

    app.py:19278   industry_en = co.get("industry_en")     ← profile 원본값
                   ↓  이 문자열로
    Industry_Rank  {업종명: 백분위}                         ← available-industries 기준

두 문자열 체계가 **같은지 검증된 적이 없다.** 다르면 화면에 영원히 아무것도 안
뜨는데, **에러도 안 난다.** 조용히 빈 칸이 될 뿐이다.

이 프로젝트에서 가장 많이 당한 실패 유형이라(402 vs 404, 필드명 대소문자,
열 정렬 붕괴) 구현 전에 실측한다.

값의 출처 (추적 완료)
─────────────────────
    app.py:19278            co.get("industry_en")
    app.py:7333             info.get("industry") or p.get("industry")
    scanner_core.py:611-612 info["industry"] = p.get("industry")
    p = _fmp_profile(ticker) → /stable/profile

즉 `industry_en` 은 **profile 엔드포인트의 industry 원본값**이다. 이걸
`available-industries` 목록과 대조하면 된다.

판정
────
    exact     완전 일치                         → 그대로 조회 가능
    case      대소문자만 다름                   → .lower() 정규화 필요
    space     공백/하이픈 주변만 다름           → 정규화 필요
    none      대응 없음                         → 매핑 테이블 필요

    exact 100%          → 정규화 계층 불필요. 바로 구현
    case/space 섞임     → industry_core 에 정규화 함수 추가
    none 다수           → 설계 재검토 (자동 매핑 불가)

비용
────
    기본        available-industries 1콜 + 티커당 1콜 (기본 표본 15개) = 16콜
    --watchlist Watchlist·Portfolios 시트를 읽어 실제 보유/관심 종목으로 검증
                (티커 수만큼 콜. 진짜 매칭률은 이쪽이 답이다)

시트에 아무것도 쓰지 않는다. 이메일 없음.

실행
────
    FMP_API_KEY=.. python automation/diag_industry_mapping.py
    FMP_API_KEY=.. GSPREAD_KEY=.. python automation/diag_industry_mapping.py --watchlist
    FMP_API_KEY=.. python automation/diag_industry_mapping.py --tickers AAPL,MO,XOM
"""
import argparse
import json
import os
import sys
import time

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import industry_core as ic  # noqa: E402

FMP_BASE = "https://financialmodelingprep.com/stable"
_KEY = str(os.environ.get("FMP_API_KEY", "") or "").strip()
GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")
TIMEOUT = 15
SLEEP_SEC = 0.20

# 기본 표본 — 섹터를 골고루 덮되, **의심스러운 업종을 일부러 포함**한다.
#   MO/PM  : Tobacco — 실측에서 값이 극단이었던 버킷
#   XOM/CVX: Oil & Gas — 120일에서 유일하게 불안정 판정을 받은 계열
#   NVDA   : Semiconductors — 가장 자주 조회될 업종
_DEFAULT = ["AAPL", "NVDA", "MSFT", "MO", "PM", "XOM", "CVX", "JPM",
            "JNJ", "WMT", "TSLA", "CAT", "NEE", "AMT", "GLD"]


def _get(path):
    sep = "&" if "?" in path else "?"
    try:
        r = requests.get(FMP_BASE + "/" + path + sep + "apikey=" + _KEY,
                         timeout=TIMEOUT)
        if r.status_code != 200:
            return None, "HTTP " + str(r.status_code)
        d = r.json()
    except Exception as e:
        return None, type(e).__name__
    return d, ""


def fetch_profile_industry(ticker):
    d, err = _get("profile?symbol=" + str(ticker))
    if err:
        return None, err
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return str(d[0].get("industry") or "").strip(), ""
    if isinstance(d, dict):
        return str(d.get("industry") or "").strip(), ""
    return None, "ODD"


def _norm_space(s):
    """공백·하이픈 주변을 정규화. 'Oil & Gas - Energy' vs 'Oil & Gas Energy'."""
    t = " ".join(str(s).split())
    t = t.replace(" - ", " ").replace("-", " ")
    return " ".join(t.split())


def classify(name, universe):
    """profile 업종명이 순위표 키와 어떻게 대응되는지."""
    if not name:
        return "empty", ""
    if name in universe:
        return "exact", name
    low = {u.lower(): u for u in universe}
    if name.lower() in low:
        return "case", low[name.lower()]
    sp = {_norm_space(u).lower(): u for u in universe}
    if _norm_space(name).lower() in sp:
        return "space", sp[_norm_space(name).lower()]
    return "none", ""


def closest(name, universe, k=3):
    """대응이 없을 때 후보 제시 — 매핑 테이블 설계용."""
    import difflib
    return difflib.get_close_matches(name, universe, n=k, cutoff=0.55)


def load_my_tickers():
    """Watchlist + Portfolios 의 실제 티커. 진짜 매칭률은 이쪽이 답이다."""
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(GSPREAD_KEY_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    sh = gspread.authorize(creds).open("Quant_DB")
    out = set()
    for title in ("Watchlist", "Portfolios"):
        try:
            vals = sh.worksheet(title).get_all_values() or []
        except Exception as e:
            print("  (" + title + " 읽기 실패: " + str(e)[:50] + ")")
            continue
        if len(vals) < 2:
            continue
        hdr = [str(c).strip() for c in vals[0]]
        idx = None
        for cand in ("Ticker", "Symbol", "티커"):
            if cand in hdr:
                idx = hdr.index(cand)
                break
        if idx is None:
            print("  (" + title + ": 티커 열을 못 찾음 — 헤더 "
                  + ", ".join(hdr[:6]) + ")")
            continue
        for r in vals[1:]:
            if idx < len(r):
                t = str(r[idx]).strip().upper()
                if t and t.isascii() and 1 <= len(t) <= 6:
                    out.add(t)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="")
    ap.add_argument("--watchlist", action="store_true")
    args = ap.parse_args()

    if not _KEY:
        print("❌ FMP_API_KEY 없음.")
        return 2

    print("=" * 78)
    print("profile.industry ↔ available-industries 매칭 검증")
    print("  industry_en 의 출처: profile 엔드포인트 원본값")
    print("  (app.py:19278 → 7333 → scanner_core.py:611 → _fmp_profile)")
    print("=" * 78)

    universe = ic.fetch_industries(_KEY)
    time.sleep(SLEEP_SEC)
    if not universe:
        print("❌ available-industries 조회 실패. 중단.")
        return 1
    print("  순위표 키: " + str(len(universe)) + "개")

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        src = "직접 지정"
    elif args.watchlist:
        if not GSPREAD_KEY_JSON:
            print("❌ --watchlist 에는 GSPREAD_KEY 가 필요합니다.")
            return 2
        tickers = load_my_tickers()
        src = "Watchlist + Portfolios"
    else:
        tickers = list(_DEFAULT)
        src = "기본 표본"
    if not tickers:
        print("❌ 검증할 티커가 없습니다.")
        return 1
    print("  대상: " + src + " " + str(len(tickers)) + "종목 · 호출 "
          + str(len(tickers) + 1) + "콜")

    buckets = {"exact": [], "case": [], "space": [], "none": [], "empty": [],
               "error": []}
    print("")
    for t in tickers:
        name, err = fetch_profile_industry(t)
        if err:
            buckets["error"].append((t, err))
            print("  ❌ %-6s  조회 실패 (%s)" % (t, err))
        else:
            kind, mapped = classify(name, universe)
            buckets[kind].append((t, name, mapped))
            icon = {"exact": "✅", "case": "🔡", "space": "␣",
                    "none": "❌", "empty": "∅"}[kind]
            extra = ""
            if kind in ("case", "space"):
                extra = "  → " + mapped
            elif kind == "none":
                cands = closest(name, universe)
                extra = ("  후보: " + ", ".join(cands)) if cands else "  (후보 없음)"
            print("  %s %-6s  %-42s%s" % (icon, t, (name or "(빈값)")[:42], extra))
        time.sleep(SLEEP_SEC)

    n = len(tickers)
    ok = len(buckets["exact"])
    norm = len(buckets["case"]) + len(buckets["space"])
    bad = len(buckets["none"]) + len(buckets["empty"])

    print("")
    print("=" * 78)
    print("판정")
    print("=" * 78)
    print("  완전 일치        %3d/%d" % (ok, n))
    print("  정규화하면 일치  %3d      (대소문자 %d · 공백 %d)"
          % (norm, len(buckets["case"]), len(buckets["space"])))
    print("  대응 없음        %3d" % bad)
    if buckets["error"]:
        print("  조회 실패        %3d" % len(buckets["error"]))

    print("")
    if bad == 0 and norm == 0 and ok == n:
        print("  ✅ 완전 일치 — 정규화 계층 불필요. 바로 구현 가능")
        rc = 0
    elif bad == 0:
        print("  🟠 정규화하면 전부 맞는다 — industry_core 에 정규화 함수 추가 필요")
        print("     (조회 실패를 조용히 빈 칸으로 두면 원인을 못 찾는다)")
        rc = 0
    else:
        print("  🔴 대응 없는 업종이 있다 — 아래 목록은 매핑 테이블이 필요하다")
        for t, name, _m in buckets["none"] + buckets["empty"]:
            print("     " + t + ": " + (name or "(빈값)"))
        print("     매칭률이 낮으면 화면 설계를 다시 봐야 한다 —")
        print("     '조용히 빈 칸'이 대부분이면 기능이 있으나 마나다.")
        rc = 1

    # 역방향 참고 — 순위표에만 있고 종목에서 안 나오는 업종은 정상이다
    # (그 업종에 해당하는 종목을 이번에 안 봤을 뿐).
    print("")
    print("  참고: 순위표 키 " + str(len(universe)) + "개 중 이번 표본이 덮은 것 "
          + str(len({m for _, _, m in buckets["exact"]}
                    | {m for _, _, m in buckets["case"]}
                    | {m for _, _, m in buckets["space"]})) + "개")
    print("=" * 78)
    return rc


if __name__ == "__main__":
    sys.exit(main())
