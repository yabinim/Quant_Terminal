"""diag_fmp_endpoints.py — 코드에 박힌 FMP 엔드포인트 경로 실측 프로브.

배경
────
FMP 공식 문서(api-docs.md, 278개 엔드포인트)와 코드를 대조한 결과,
현재 코드가 쓰는 경로 6개가 공식 문서에 존재하지 않는다.

  1) fmp_extras.py:452    etf/sector-weighting          → etf/sector-weightings
  2) fmp_extras.py:389    batch-market-capitalization   → market-capitalization-batch
  3) narrative_core.py:220 press-releases-latest        → news/press-releases-latest
  4) app.py:5507, 14834   stock-news?symbols=           → news/stock?symbols=
  5) app.py:6012, 7123    etf-holder/{sym}              → etf/holdings?symbol=
  6) earnings_core.py:339 earnings-surprises?symbol=    → (개별 심볼용 없음)
     app.py:5165
     diag_earnings_preview_backtest.py:91

여섯 곳 전부 try-except 로 예외를 삼킨다. 즉 죽어 있어도 "데이터 없음"으로
보이지 조용히 틀린 값이 나온다 — fmp_http.py 상단에 적어둔 실패 모드 그대로다.

다만 FMP 가 일부 레거시 경로에 별칭을 유지할 수도 있으므로, 코드를 고치기 전에
**실제로 한 번씩 때려보는 것**이 이 스크립트의 전부다.

왜 fmp_http 를 안 쓰는가
────────────────────────
fmp_http.fmp_get 은 429/402 에서 재시도한 뒤 None 을 돌려준다. 프로브는
**첫 응답의 원본 상태 코드**가 필요하다(402 인지 404 인지가 판정을 가른다).
그래서 여기서는 requests 를 직접 쓴다.

부수 효과로 이 스크립트는 프로젝트 모듈을 하나도 import 하지 않는다.
사본 신선도와 무관하게 언제 돌려도 같은 결과가 나온다.

"문서에 있다" ≠ "이 플랜에서 쓸 수 있다"
────────────────────────────────────────
1차 프로브에서 analyst-estimates 와 earning-call-transcript-* 가 공식 문서에
버젓이 있으면서도 이 플랜에서 402 로 죽는 것이 이미 확인됐다. 그래서 판정을
둘로 나눈다.

  · 402          → 🔒 플랜 미포함 (경로는 맞음 — 코드 수정으로 해결 안 됨)
  · 404 / 에러메시지 → ❌ 경로 자체가 틀림 (코드 수정으로 해결됨)

이 둘을 뭉개면 엉뚱한 수정을 하게 된다.

시트 접근 없음 · 이메일 없음 · 알림 상태 머신 미접촉. 재실행 부작용 없다.

비용
────
기본 12콜 (6쌍 × 2). PROBE_EXTRA=1 이면 +3콜.

실행
────
    FMP_API_KEY=xxx python automation/diag_fmp_endpoints.py
    FMP_API_KEY=xxx PROBE_EXTRA=1 python automation/diag_fmp_endpoints.py
"""
import json
import os
import sys
import time

import requests

FMP_BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 12
SLEEP_SEC = 0.35  # 12콜이라 레이트리밋 걱정은 없지만 예의상

_KEY = str(os.environ.get("FMP_API_KEY", "") or "").strip()
_EXTRA = str(os.environ.get("PROBE_EXTRA", "") or "").strip() in ("1", "true", "TRUE", "yes")


# ══════════════════════════════════════════════════════════════════════════
# 프로브 대상
#   (그룹명, 현재 코드 경로, 공식 문서 경로, 코드 위치)
#   공식 경로가 None 이면 "공식 문서에 대응물이 없음"을 뜻한다.
# ══════════════════════════════════════════════════════════════════════════
PAIRS = [
    (
        "ETF 섹터 비중",
        "etf/sector-weighting?symbol=SPY",
        "etf/sector-weightings?symbol=SPY",
        "fmp_extras.py:452",
    ),
    (
        "시가총액 배치",
        "batch-market-capitalization?symbols=AAPL,MSFT",
        "market-capitalization-batch?symbols=AAPL,MSFT",
        "fmp_extras.py:389",
    ),
    (
        "보도자료 최신 (내러티브 Layer B, weight 1.3)",
        "press-releases-latest?page=0&limit=5",
        "news/press-releases-latest?page=0&limit=5",
        "narrative_core.py:220",
    ),
    (
        "종목 뉴스 검색",
        "stock-news?symbols=AAPL&limit=3",
        "news/stock?symbols=AAPL&limit=3",
        "app.py:5507, app.py:14834",
    ),
    (
        "ETF 보유종목",
        "etf-holder/SPY",
        "etf/holdings?symbol=SPY",
        "app.py:6012, app.py:7123",
    ),
    (
        "어닝 서프라이즈",
        "earnings-surprises?symbol=AAPL&limit=8",
        "earnings?symbol=AAPL&limit=8",
        "earnings_core.py:339, app.py:5165, diag_earnings_preview_backtest.py:91",
    ),
]

# 참고용 — 문서에는 있으나 플랜 가용성이 불확실한 것들.
EXTRAS = [
    ("신규 발견 — 애널리스트 등급 스냅샷", "grades?symbol=AAPL"),
    ("Stage 2 재확인 — 분기 컨센서스", "analyst-estimates?symbol=AAPL&period=quarter&limit=4"),
    ("Stage 3 재확인 — 트랜스크립트 날짜", "earning-call-transcript-dates?symbol=AAPL"),
]


# ══════════════════════════════════════════════════════════════════════════
# 호출 · 판정
# ══════════════════════════════════════════════════════════════════════════
def _mask(url):
    """로그에 API 키가 남지 않게 한다."""
    if not _KEY:
        return url
    return url.replace(_KEY, "***")


def probe(path):
    """단일 엔드포인트 호출. 판정 dict 를 돌려준다."""
    sep = "&" if "?" in path else "?"
    url = FMP_BASE + "/" + path + sep + "apikey=" + _KEY

    out = {
        "path": path,
        "status": None,
        "verdict": "",
        "detail": "",
        "n": None,
        "keys": [],
    }

    try:
        r = requests.get(url, timeout=TIMEOUT)
    except Exception as e:
        out["verdict"] = "EXC"
        out["detail"] = type(e).__name__ + ": " + str(e)[:80]
        return out

    out["status"] = r.status_code

    if r.status_code == 402:
        out["verdict"] = "PLAN"
        out["detail"] = "경로는 맞음 — 이 플랜에 미포함"
        return out
    if r.status_code in (401, 403):
        out["verdict"] = "AUTH"
        out["detail"] = "키 문제 또는 권한 없음"
        return out
    if r.status_code == 429:
        out["verdict"] = "RATE"
        out["detail"] = "레이트리밋 — 잠시 후 재실행"
        return out
    if r.status_code == 404:
        out["verdict"] = "404"
        out["detail"] = "경로 없음"
        return out
    if r.status_code != 200:
        out["verdict"] = "HTTP"
        out["detail"] = "HTTP " + str(r.status_code)
        return out

    # 200 이어도 안심할 수 없다. FMP 는 잘못된 경로에
    # 200 + {"Error Message": ...} 또는 200 + [] 를 돌려주기도 한다.
    try:
        data = r.json()
    except Exception:
        out["verdict"] = "NOJSON"
        out["detail"] = "본문 앞부분: " + r.text[:60].replace("\n", " ")
        return out

    if isinstance(data, dict):
        dkeys = list(data.keys())
        lowered = [str(x).lower() for x in dkeys]
        if "error message" in lowered or "error" in lowered:
            msg = ""
            for kk in dkeys:
                if str(kk).lower().startswith("error"):
                    msg = str(data.get(kk))[:80]
                    break
            out["verdict"] = "ERRMSG"
            out["detail"] = msg
            return out
        out["verdict"] = "LIVE"
        out["n"] = 1
        out["keys"] = sorted(dkeys)[:10]
        out["detail"] = "dict 응답"
        return out

    if isinstance(data, list):
        out["n"] = len(data)
        if len(data) == 0:
            # 가장 위험한 케이스. 코드가 이걸 '데이터 없음'으로 오해한다.
            out["verdict"] = "EMPTY"
            out["detail"] = "200 인데 빈 배열 — 코드가 '데이터 없음'으로 오독"
            return out
        first = data[0]
        if isinstance(first, dict):
            out["keys"] = sorted(first.keys())[:10]
        out["verdict"] = "LIVE"
        out["detail"] = str(len(data)) + "건"
        return out

    out["verdict"] = "ODD"
    out["detail"] = "예상 밖 타입: " + type(data).__name__
    return out


_ICON = {
    "LIVE": "✅",
    "EMPTY": "⚠️",
    "404": "❌",
    "ERRMSG": "❌",
    "PLAN": "🔒",
    "AUTH": "🔑",
    "RATE": "⏳",
    "HTTP": "❌",
    "NOJSON": "❌",
    "EXC": "💥",
    "ODD": "❓",
}


def show(label, res):
    icon = _ICON.get(res["verdict"], "❓")
    st = res["status"] if res["status"] is not None else "-"
    line = "  " + icon + " " + label.ljust(6) + " " + str(st).ljust(4) + " " + res["path"]
    print(line)
    if res["detail"]:
        print("        └ " + res["detail"])
    if res["keys"]:
        print("        └ 키: " + ", ".join(res["keys"]))


# ══════════════════════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════════════════════
def main():
    if not _KEY:
        print("❌ FMP_API_KEY 가 비어 있습니다. 중단.")
        return 2

    ncalls = len(PAIRS) * 2 + (len(EXTRAS) if _EXTRA else 0)
    print("=" * 78)
    print("FMP 엔드포인트 실측 프로브")
    print("  기준: 공식 api-docs (278 엔드포인트) vs 현재 코드")
    print("  호출 수: " + str(ncalls) + "콜 · 시트/이메일 접촉 없음")
    print("=" * 78)

    results = []
    for name, cur, off, where in PAIRS:
        print("")
        print("── " + name)
        print("   코드 위치: " + where)

        r_cur = probe(cur)
        show("현재", r_cur)
        time.sleep(SLEEP_SEC)

        if off is None:
            r_off = None
            print("  ➖ 공식   -    (공식 문서에 대응 엔드포인트 없음)")
        else:
            r_off = probe(off)
            show("공식", r_off)
            time.sleep(SLEEP_SEC)

        results.append((name, where, cur, off, r_cur, r_off))

    if _EXTRA:
        print("")
        print("=" * 78)
        print("참고 확인 — 문서에는 있으나 플랜 가용성이 불확실한 것들")
        print("=" * 78)
        for name, path in EXTRAS:
            print("")
            print("── " + name)
            show("확인", probe(path))
            time.sleep(SLEEP_SEC)

    # ── 최종 판정 ────────────────────────────────────────────────────────
    print("")
    print("=" * 78)
    print("최종 판정")
    print("=" * 78)

    must_fix = []
    keep = []
    both_dead = []
    inconclusive = []

    for name, where, cur, off, r_cur, r_off in results:
        vc = r_cur["verdict"]
        vo = r_off["verdict"] if r_off else "NONE"

        # 레이트리밋/네트워크 오류는 판정 불가로 분리한다.
        if vc in ("RATE", "EXC", "AUTH") or vo in ("RATE", "EXC", "AUTH"):
            inconclusive.append((name, where, cur, off, vc, vo))
            continue

        cur_ok = vc == "LIVE"
        off_ok = vo == "LIVE"

        if not cur_ok and off_ok:
            must_fix.append((name, where, cur, off, vc))
        elif cur_ok and not off_ok:
            keep.append((name, where, cur, off, vo))
        elif cur_ok and off_ok:
            keep.append((name, where, cur, off, "둘 다 응답 — 별칭 유지 중"))
        else:
            both_dead.append((name, where, cur, off, vc, vo))

    if must_fix:
        print("")
        print("🔴 수정 필수 — 현재 경로가 죽었고 공식 경로가 살아 있음")
        for name, where, cur, off, vc in must_fix:
            print("   · " + name)
            print("     " + where)
            print("     " + cur + "  [" + vc + "]")
            print("       → " + off)

    if both_dead:
        print("")
        print("⚫ 둘 다 실패 — 경로 교체로 해결 안 됨. 기능 제거/대체 검토")
        for name, where, cur, off, vc, vo in both_dead:
            offs = off if off else "(대응물 없음)"
            print("   · " + name + "  현재[" + vc + "] / 공식[" + vo + "]")
            print("     " + where)
            print("     현재: " + cur)
            print("     공식: " + offs)
            if vc == "PLAN" or vo == "PLAN":
                print("     ⚠️ 402 = 플랜 미포함. 코드를 고쳐도 살아나지 않는다.")

    if keep:
        print("")
        print("🟡 현행 유지 가능 — 다만 공식 경로로 통일 권장(우선순위 낮음)")
        for name, where, cur, off, note in keep:
            print("   · " + name + "  (" + str(note) + ")")
            print("     " + where)

    if inconclusive:
        print("")
        print("⏳ 판정 불가 — 레이트리밋/네트워크/인증. 재실행 필요")
        for name, where, cur, off, vc, vo in inconclusive:
            print("   · " + name + "  현재[" + vc + "] / 공식[" + vo + "]")

    print("")
    print("=" * 78)
    total = len(results)
    print("요약: 총 " + str(total) + "쌍 · 수정필수 " + str(len(must_fix))
          + " · 둘다실패 " + str(len(both_dead))
          + " · 현행유지 " + str(len(keep))
          + " · 판정불가 " + str(len(inconclusive)))
    print("=" * 78)

    # 기계 판독용 — 워크플로 로그에서 grep 하기 쉽게 한 줄 JSON 으로도 남긴다.
    summary = {
        "must_fix": [w for _, w, _, _, _ in must_fix],
        "both_dead": [w for _, w, _, _, _, _ in both_dead],
        "keep": [w for _, w, _, _, _ in keep],
        "inconclusive": [w for _, w, _, _, _, _ in inconclusive],
    }
    print("PROBE_JSON " + json.dumps(summary, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
