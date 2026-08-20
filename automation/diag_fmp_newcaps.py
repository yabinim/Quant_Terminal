"""diag_fmp_newcaps.py — 미사용 FMP 엔드포인트 실측 프로브 (신규 기능 후보).

배경
────
2026-08-19 감사: 공식 api-docs 의 고유 경로 242개 중 코드가 쓰는 것은 48개다.
남은 194개 가운데 이 터미널의 철학(손실방지 · 모멘텀 · 초기 수혜주 발굴)에
직결되는 후보를 골라 **쓸 수 있는지 먼저 확인**한다.

diag_fmp_endpoints.py 와 무엇이 다른가
──────────────────────────────────────
그쪽은 "이미 코드에 박힌 경로가 살아 있나"를 본다(쌍 비교).
이쪽은 "아직 안 쓰는 경로를 쓸 수 있나 + **필요한 필드가 실제로 오나**"를 본다.

그래서 판정에 한 단계를 더 넣었다.

    LIVE      200 + 데이터 있음 + 필요한 필드 전부 존재    → 바로 설계 가능
    FIELDS    200 + 데이터 있음 + **필요한 필드 일부 없음** → 설계 전제 재검토
    EMPTY     200 + 빈 배열                                 → 파라미터/시점 문제일 수 있음
    PLAN      402                                           → 코드로 해결 안 됨
    404 등    경로 실패

"200 이니까 된다"로 넘어가면 구현 도중에 필드가 없어서 되돌아오게 된다.
필드까지 확인하는 것이 이 스크립트의 존재 이유다.

프로브 대상 (개발 계획서 Tier 분류와 1:1)
─────────────────────────────────────────
  tierA (5콜) — 실제 결함 해결
    holidays-by-exchange      🔴 휴장일 하드코딩이 2026-12-25 에서 끝남
                                 (run_watchlist_alerts:165 / run_drg_predict:58
                                  / run_drg_verify:54 — 3중 중복)
                                 ★ from/to 를 **내년**으로 넣어 내년 데이터가
                                   실제로 오는지까지 본다. 이게 핵심 질문이다.
    symbol-change             🔴 티커 변경 시 run_watchlist_alerts:823 에서
    delisted-companies        🔴 hist.empty → 조용히 skip → 영구 침묵
    actively-trading-list         상장 여부 판정용 멤버십 집합 (대안 경로)
    etf/asset-exposure        🟡 app.py:6450 find_etfs_holding_stock 스텁의 정답
                                 후보. etf/holdings 가 402 였으므로 의심은 있다.

  tierB (9콜) — 기존 기능 강화
    sector-performance-snapshot        섹터/업종 **성과**. 현재는 sector-pe-snapshot
    historical-sector-performance      (밸류에이션)만 있고 성과 데이터가 없다.
    industry-performance-snapshot      averageChange 는 동일가중 브레드스라
    historical-industry-performance    시총가중 ETF 가격과 구조적으로 다른 신호.
    price-target-summary               목표주가 리비전 모멘텀(월/분기/연 카운트)
    stock-peers                        동종업계 대비 RS (현재는 SPY 대비만)
    sec-filings-8k                     중대사건 공시 — 매도 레이더 손실방지
    dividends-calendar                 DRIP 예정 배당 사전 파악
    mergers-acquisitions-latest        보유·워치 종목 M&A 이벤트

  tierC (4콜) — 탐색적 신규 기능
    senate-latest / house-latest       역방향 조기 발굴 (현재는 종목별 조회만)
    institutional-ownership/holder-performance-summary
    historical-industry-pe

  grades (3콜) — 미결 과제 정리
    grades / grades-consensus / ratings-snapshot 의 **필드 집합을 나란히 출력**한다.
    "grades?symbol= 가 기존 등급 엔드포인트와 겹치는가"는 지난 감사의 미검토
    잔여 건이다. 응답 키를 직접 비교하는 것이 가장 빠른 답이다.

안전성
──────
  · 시트 접근 없음 — 읽지도 쓰지도 않는다
  · 이메일 없음 · 알림 상태 머신 미접촉
  · 프로젝트 모듈 import 없음 (requests 만 사용)
  → 사본 신선도와 무관하다. 몇 번을 돌려도 부작용이 없다.

fmp_http 를 쓰지 않는 이유는 diag_fmp_endpoints.py 와 같다.
fmp_get 은 402/429 를 삼키고 None 을 돌려주는데, 프로브는 **원본 상태 코드**가
필요하다(402 인지 404 인지가 판정을 가른다).

비용
────
    PROBE_TIER=tierA   5콜  (기본)
    PROBE_TIER=tierB   9콜
    PROBE_TIER=tierC   4콜
    PROBE_TIER=grades  3콜
    PROBE_TIER=all    21콜

실행
────
    FMP_API_KEY=xxx python automation/diag_fmp_newcaps.py
    FMP_API_KEY=xxx PROBE_TIER=all python automation/diag_fmp_newcaps.py
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

FMP_BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 15
SLEEP_SEC = 0.35

_KEY = str(os.environ.get("FMP_API_KEY", "") or "").strip()

_TIER = str(os.environ.get("PROBE_TIER", "") or "tierA").strip().lower()
if _TIER not in ("tiera", "tierb", "tierc", "grades", "all"):
    _TIER = "tiera"


# ══════════════════════════════════════════════════════════════════════════
# 날짜 — 하드코딩하면 내년에 이 프로브 자체가 낡는다. 실행 시점에서 만든다.
# ══════════════════════════════════════════════════════════════════════════
_TODAY = datetime.now(timezone.utc).date()
_NEXT_YEAR = _TODAY.year + 1


def _recent_weekday(days_back=3):
    """최근 평일 날짜 문자열. 스냅샷 계열은 거래일이어야 데이터가 온다.

    휴장일까지는 피하지 못한다 — 그 경우 EMPTY 로 나오고, 그건 '경로가 죽었다'가
    아니라 '그날 데이터가 없다'는 뜻이므로 판정에서 별도 버킷으로 뺀다.
    """
    d = _TODAY - timedelta(days=days_back)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d.isoformat()


_D_RECENT = _recent_weekday(3)
_D_FROM_30 = (_TODAY - timedelta(days=30)).isoformat()
_D_FROM_90 = (_TODAY - timedelta(days=90)).isoformat()
_D_TO = _TODAY.isoformat()
_Y_FROM = str(_NEXT_YEAR) + "-01-01"
_Y_TO = str(_NEXT_YEAR) + "-12-31"


# ══════════════════════════════════════════════════════════════════════════
# 프로브 대상
#   (라벨, 경로, 용도/영향, 필요 필드, 본문에 반드시 있어야 할 문자열 or None)
#
#   '필요 필드' 는 설계가 실제로 의존하는 키다. 200 이어도 이게 없으면 설계를
#   다시 해야 하므로 LIVE 와 구분한다.
# ══════════════════════════════════════════════════════════════════════════
TIER_A = [
    (
        "🔴 휴장일 캘린더 — 하드코딩 대체",
        "holidays-by-exchange?exchange=NASDAQ&from=" + _Y_FROM + "&to=" + _Y_TO,
        "run_watchlist_alerts:165 / run_drg_predict:58 / run_drg_verify:54 의 "
        "_NYSE_HOLIDAYS 가 2026-12-25 에서 끝난다. "
        + str(_NEXT_YEAR) + "-01-01 부터 모든 휴장일을 거래일로 오판한다.",
        ["exchange", "date", "isClosed"],
        str(_NEXT_YEAR),      # ★ 내년 데이터가 실제로 오는가 — 이게 핵심
    ),
    (
        "🔴 티커 변경 이력",
        "symbol-change?limit=10",
        "티커가 바뀌면 historical-price-eod 가 빈 배열을 주고 "
        "run_watchlist_alerts:823 이 조용히 skip 한다 → 영구 침묵.",
        ["date", "oldSymbol", "newSymbol"],
        None,
    ),
    (
        "🔴 상장폐지 목록",
        "delisted-companies?page=0&limit=10",
        "보유 종목이 상폐되면 매도 신호가 아예 오지 않는다. 손실방지 직격.",
        ["symbol", "delistedDate"],
        None,
    ),
    (
        "상장 종목 멤버십 집합 (대안 경로)",
        "actively-trading-list",
        "delisted-companies 가 막히면 이쪽으로 '아직 거래되는가'를 판정한다. "
        "응답이 매우 클 수 있어 건수도 함께 본다.",
        ["symbol"],
        None,
    ),
    (
        "🟡 ETF 역방향 보유 조회",
        "etf/asset-exposure?symbol=AAPL",
        "app.py:6450 find_etfs_holding_stock 은 현재 [] 반환 스텁이다. "
        "etf/holdings 가 402 였으므로 같은 계열일 가능성이 있다. "
        "살아 있으면 weightPercentage 까지 와서 가중치 표시 설계도 함께 풀린다.",
        ["symbol", "asset", "weightPercentage"],
        None,
    ),
]

TIER_B = [
    (
        "섹터 성과 스냅샷",
        "sector-performance-snapshot?date=" + _D_RECENT,
        "현재는 sector-pe-snapshot(밸류에이션)만 쓴다. 실제 성과 데이터가 없다.",
        ["date", "sector", "averageChange"],
        None,
    ),
    (
        "섹터 성과 시계열",
        "historical-sector-performance?sector=Technology&from=" + _D_FROM_90 + "&to=" + _D_TO,
        "위성 섹터 로테이션 백테스트의 신규 입력 후보. averageChange 는 "
        "동일가중 브레드스라 시총가중 ETF 가격과 구조적으로 다른 신호다.",
        ["date", "sector", "averageChange"],
        None,
    ),
    (
        "업종 성과 스냅샷",
        "industry-performance-snapshot?date=" + _D_RECENT,
        "섹터보다 세분화된 단위. '초기 수혜주 발굴' 철학에 직결.",
        ["date", "industry", "averageChange"],
        None,
    ),
    (
        "업종 성과 시계열",
        "historical-industry-performance?industry=Semiconductors&from="
        + _D_FROM_90 + "&to=" + _D_TO,
        "업종 모멘텀 순위의 시계열 기반.",
        ["date", "industry", "averageChange"],
        None,
    ),
    (
        "목표주가 리비전 요약",
        "price-target-summary?symbol=AAPL",
        "현재 price-target-consensus 는 스냅샷뿐. 월/분기/연 카운트가 오면 "
        "애널리스트 상향 추세를 조기 신호로 쓸 수 있다.",
        ["symbol", "lastMonthCount", "lastMonthAvgPriceTarget", "lastQuarterCount"],
        None,
    ),
    (
        "동종업계 비교군",
        "stock-peers?symbol=AAPL",
        "현재 RS 는 SPY 대비만. 동종업계 대비 RS 는 '섹터 전체가 올라서 "
        "따라 오른 종목'을 걸러낸다.",
        ["symbol"],
        None,
    ),
    (
        "8-K 중대사건 공시",
        "sec-filings-8k?from=" + _D_FROM_30 + "&to=" + _D_TO + "&page=0&limit=10",
        "매도 레이더 손실방지. 가격이 반응하기 전에 잡히는 구조적 경로.",
        ["symbol", "filingDate", "acceptedDate"],
        None,
    ),
    (
        "배당 캘린더",
        "dividends-calendar?from=" + _D_FROM_30 + "&to=" + _D_TO,
        "DRIP 이 현재 종목별 배당 이력을 순회한다. 캘린더 1콜로 대체 가능한지.",
        ["symbol", "date", "adjDividend"],
        None,
    ),
    (
        "M&A 최신",
        "mergers-acquisitions-latest?page=0&limit=10",
        "피인수 발표는 포지션 판단을 통째로 바꾼다.",
        ["symbol", "targetedCompanyName"],
        None,
    ),
]

TIER_C = [
    (
        "상원 거래 최신 피드",
        "senate-latest?page=0&limit=10",
        "현재는 종목별 조회만 쓴다. 최신 피드는 역방향 조기 발굴 입력이 된다.",
        ["symbol"],
        None,
    ),
    (
        "하원 거래 최신 피드",
        "house-latest?page=0&limit=10",
        "위와 동일.",
        ["symbol"],
        None,
    ),
    (
        "기관 보유자 성과 요약",
        "institutional-ownership/holder-performance-summary?cik=0001067983&page=0",
        "'성과 좋은 기관이 사는가'로 필터링. cik 은 버크셔(예시).",
        ["cik"],
        None,
    ),
    (
        "업종 PER 시계열",
        "historical-industry-pe?industry=Semiconductors&from=" + _D_FROM_90 + "&to=" + _D_TO,
        "업종 밸류에이션의 시계열 위치. 현재는 섹터 스냅샷만 있다.",
        ["date", "industry"],
        None,
    ),
]

# 미결 과제 — 등급 계열 3종의 필드 집합을 나란히 본다.
GRADES = [
    (
        "등급 (미검토 잔여 건)",
        "grades?symbol=AAPL",
        "지난 감사에서 200(1786건) 확인됐으나 기존 등급 엔드포인트와의 "
        "중복 여부가 미검토 상태다.",
        ["symbol"],
        None,
    ),
    (
        "등급 컨센서스 (현재 사용 중)",
        "grades-consensus?symbol=AAPL",
        "app.py 에서 이미 사용. 비교 기준.",
        ["symbol"],
        None,
    ),
    (
        "등급 스냅샷 (현재 사용 중)",
        "ratings-snapshot?symbol=AAPL",
        "app.py 에서 이미 사용. 비교 기준.",
        ["symbol"],
        None,
    ),
]


# ══════════════════════════════════════════════════════════════════════════
# 호출 · 판정
# ══════════════════════════════════════════════════════════════════════════
def _mask(text):
    """로그에 API 키가 남지 않게 한다."""
    if not _KEY:
        return text
    return str(text).replace(_KEY, "***")


def probe(path, need=None, contains=None):
    """단일 엔드포인트 호출 + 필드 검증. 판정 dict 를 돌려준다."""
    sep = "&" if "?" in path else "?"
    url = FMP_BASE + "/" + path + sep + "apikey=" + _KEY

    out = {
        "path": path,
        "status": None,
        "verdict": "",
        "detail": "",
        "n": None,
        "keys": [],
        "missing": [],
    }

    try:
        r = requests.get(url, timeout=TIMEOUT)
    except Exception as e:
        out["verdict"] = "EXC"
        out["detail"] = type(e).__name__ + ": " + _mask(str(e))[:90]
        return out

    out["status"] = r.status_code

    if r.status_code == 402:
        out["verdict"] = "PLAN"
        out["detail"] = "경로는 맞음 — 이 플랜에 미포함. 코드로 해결 안 됨"
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

    body_text = r.text or ""

    try:
        data = r.json()
    except Exception:
        out["verdict"] = "NOJSON"
        out["detail"] = "본문 앞부분: " + _mask(body_text[:70]).replace("\n", " ")
        return out

    # FMP 는 잘못된 요청에 200 + {"Error Message": ...} 를 주기도 한다.
    if isinstance(data, dict):
        dkeys = list(data.keys())
        lowered = [str(x).lower() for x in dkeys]
        if "error message" in lowered or "error" in lowered:
            msg = ""
            for kk in dkeys:
                if str(kk).lower().startswith("error"):
                    msg = _mask(str(data.get(kk)))[:90]
                    break
            out["verdict"] = "ERRMSG"
            out["detail"] = msg
            return out
        sample_keys = sorted(dkeys)
        out["n"] = 1
        out["detail"] = "dict 응답"
    elif isinstance(data, list):
        out["n"] = len(data)
        if len(data) == 0:
            out["verdict"] = "EMPTY"
            out["detail"] = "200 인데 빈 배열 — 파라미터/시점 문제일 수 있음"
            return out
        first = data[0]
        sample_keys = sorted(first.keys()) if isinstance(first, dict) else []
        out["detail"] = str(len(data)) + "건"
    else:
        out["verdict"] = "ODD"
        out["detail"] = "예상 밖 타입: " + type(data).__name__
        return out

    out["keys"] = sample_keys[:14]

    # ── 필드 검증 ────────────────────────────────────────────────────────
    # 200 + 데이터가 있어도 설계가 의존하는 키가 없으면 그대로 못 쓴다.
    missing = [k for k in (need or []) if k not in sample_keys]
    out["missing"] = missing

    # ── 본문 내용 검증 (holidays 의 내년 데이터처럼 '무엇이 왔는가'가 중요할 때)
    if contains is not None and str(contains) not in body_text:
        out["verdict"] = "FIELDS"
        out["detail"] = (out["detail"] + " — 그러나 본문에 '" + str(contains)
                         + "' 가 없다. 요청한 범위의 데이터가 오지 않았다")
        return out

    if missing:
        out["verdict"] = "FIELDS"
        out["detail"] = out["detail"] + " — 그러나 필요 필드 누락: " + ", ".join(missing)
        return out

    out["verdict"] = "LIVE"
    return out


_ICON = {
    "LIVE": "✅",
    "FIELDS": "🟠",
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


def show(res):
    icon = _ICON.get(res["verdict"], "❓")
    st = res["status"] if res["status"] is not None else "-"
    print("  " + icon + " " + str(st).ljust(4) + " " + res["path"][:96])
    if res["detail"]:
        print("        └ " + res["detail"])
    if res["keys"]:
        print("        └ 응답 키: " + ", ".join(res["keys"]))


# ══════════════════════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════════════════════
def run_group(title, targets, results):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)
    for label, path, impact, need, contains in targets:
        print("")
        print("── " + label)
        print("   용도: " + impact)
        if need:
            print("   필요 필드: " + ", ".join(need))
        r = probe(path, need=need, contains=contains)
        show(r)
        results.append((label, path, impact, r))
        time.sleep(SLEEP_SEC)


def main():
    if not _KEY:
        print("❌ FMP_API_KEY 가 비어 있습니다. 중단.")
        return 2

    run_a = _TIER in ("tiera", "all")
    run_b = _TIER in ("tierb", "all")
    run_c = _TIER in ("tierc", "all")
    run_g = _TIER in ("grades", "all")

    ncalls = (len(TIER_A) if run_a else 0) + (len(TIER_B) if run_b else 0) \
        + (len(TIER_C) if run_c else 0) + (len(GRADES) if run_g else 0)

    print("=" * 78)
    print("FMP 미사용 엔드포인트 실측 프로브 — 신규 기능 후보")
    print("  기준: 공식 api-docs 242 경로 중 코드 미사용분")
    print("  티어: " + _TIER + " · 호출 " + str(ncalls) + "콜 · 시트/이메일 접촉 없음")
    print("  기준일: " + _D_TO + " · 스냅샷 조회일 " + _D_RECENT
          + " · 휴장일 조회 " + str(_NEXT_YEAR) + "년")
    print("=" * 78)

    results = []
    if run_a:
        run_group("Tier A — 실제 결함 해결", TIER_A, results)
    if run_b:
        run_group("Tier B — 기존 기능 강화", TIER_B, results)
    if run_c:
        run_group("Tier C — 탐색적 신규 기능", TIER_C, results)
    if run_g:
        run_group("등급 계열 중복 확인 — 필드 집합 비교", GRADES, results)

    # ── 최종 판정 ────────────────────────────────────────────────────────
    live, fields, empty, plan, dead, unk = [], [], [], [], [], []
    for label, path, impact, r in results:
        v = r["verdict"]
        if v == "LIVE":
            live.append((label, path, impact, r))
        elif v == "FIELDS":
            fields.append((label, path, impact, r))
        elif v == "EMPTY":
            empty.append((label, path, impact, r))
        elif v == "PLAN":
            plan.append((label, path, impact, r))
        elif v in ("RATE", "EXC", "AUTH"):
            unk.append((label, path, impact, r))
        else:
            dead.append((label, path, impact, r))

    print("")
    print("=" * 78)
    print("최종 판정")
    print("=" * 78)

    if live:
        print("")
        print("✅ 사용 가능 — 필요 필드까지 확인됨. 설계 진행 가능")
        for label, path, impact, r in live:
            print("   · " + label + "  (" + str(r["detail"]) + ")")
            print("     " + path[:92])

    if fields:
        print("")
        print("🟠 응답은 오지만 전제가 어긋남 — 설계 재검토 필요")
        print("   200 이라고 넘어가면 구현 도중에 되돌아온다. 여기부터 다시 본다.")
        for label, path, impact, r in fields:
            print("   · " + label)
            print("     " + path[:92])
            print("     " + str(r["detail"]))
            if r["keys"]:
                print("     실제 키: " + ", ".join(r["keys"]))

    if empty:
        print("")
        print("⚠️ 200 + 빈 배열 — 판단 보류")
        print("   경로가 맞고 그 시점/파라미터에 데이터가 없을 뿐일 수 있다.")
        print("   스냅샷 계열은 휴장일이면 비어 온다. 날짜를 바꿔 재실행할 것.")
        for label, path, impact, r in empty:
            print("   · " + label)
            print("     " + path[:92])

    if plan:
        print("")
        print("🔒 플랜 미포함(402) — 코드 수정으로 해결되지 않는다. 후보에서 제외")
        for label, path, impact, r in plan:
            print("   · " + label)
            print("     " + path[:92])

    if dead:
        print("")
        print("❌ 경로 실패 — 공식 문서에서 경로 재확인 필요")
        for label, path, impact, r in dead:
            print("   · " + label + "  [" + r["verdict"] + "]")
            print("     " + path[:92])
            if r["detail"]:
                print("     " + str(r["detail"]))

    if unk:
        print("")
        print("⏳ 판정 불가 — 레이트리밋/네트워크/인증. 재실행 필요")
        for label, path, impact, r in unk:
            print("   · " + label + "  [" + r["verdict"] + "]")

    print("")
    print("=" * 78)
    print("요약: 총 " + str(len(results))
          + " (사용가능 " + str(len(live))
          + " · 전제어긋남 " + str(len(fields))
          + " · 빈배열 " + str(len(empty))
          + " · 플랜 " + str(len(plan))
          + " · 경로실패 " + str(len(dead))
          + " · 판정불가 " + str(len(unk)) + ")")
    print("=" * 78)

    # 기계 판독용 — 워크플로 로그에서 grep 하기 쉽게 한 줄 JSON.
    summary = {
        "tier": _TIER,
        "date": _D_TO,
        "live": [p for _, p, _, _ in live],
        "fields": [p for _, p, _, _ in fields],
        "empty": [p for _, p, _, _ in empty],
        "plan": [p for _, p, _, _ in plan],
        "dead": [p for _, p, _, _ in dead],
        "unknown": [p for _, p, _, _ in unk],
    }
    print("NEWCAPS_JSON " + json.dumps(summary, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
