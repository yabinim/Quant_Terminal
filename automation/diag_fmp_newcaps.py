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
    PROBE_TIER=tierB2  5콜   ← tierB 후속 확인
    PROBE_TIER=tierB3  3콜   ← B-1 설계 직전 (이력 깊이 + 분류명 전체)
    PROBE_TIER=tierC   4콜
    PROBE_TIER=grades  3콜
    PROBE_TIER=all    29콜

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
if _TIER not in ("tiera", "tierb", "tierb2", "tierb3", "tierc", "grades", "all"):
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
_D_FROM_3Y = (_TODAY - timedelta(days=365 * 3)).isoformat()
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

# ══════════════════════════════════════════════════════════════════════════
# TIER B2 — tierB 실측 후 드러난 후속 확인 (2026-08-20)
#
# tierB 는 9/9 전부 LIVE 였지만, 응답을 보고 나서야 **실사용이 불가능한 것**이
# 두 개 드러났다.
#
#   sec-filings-8k               symbol 파라미터가 없다 → 시장 전체 페이징
#   mergers-acquisitions-latest  동일
#
# 내 종목 10~100개의 공시를 보려고 시장 전체를 페이징할 수는 없다. 문서에
# symbol/이름 필터가 있는 대체 경로가 있어서 그걸 확인한다.
#
# 함께 확인하는 것: B-1(섹터/업종 성과) 설계에 반드시 필요한 **분류명 목록**.
# historical-*-performance 는 sector/industry 파라미터에 FMP 가 정한 정확한
# 문자열을 요구한다. "Semiconductors" 처럼 추측으로 맞춘 값으로 구현하면
# 다른 업종에서 조용히 빈 배열이 온다.
# ══════════════════════════════════════════════════════════════════════════
TIER_B2 = [
    (
        "8-K 대체 경로 — 심볼 필터형",
        "sec-filings-search/symbol?symbol=AAPL&from=" + _D_FROM_90 + "&to=" + _D_TO
        + "&page=0&limit=10",
        "sec-filings-8k 는 symbol 파라미터가 없어 시장 전체를 페이징해야 한다. "
        "이쪽이 살아 있으면 종목당 1콜로 끝난다. formType 이 와야 8-K 만 "
        "골라낼 수 있다.",
        ["symbol", "formType", "filingDate", "acceptedDate"],
        None,
    ),
    (
        "8-K 대체 경로 — 폼타입 필터형",
        "sec-filings-search/form-type?formType=8-K&from=" + _D_FROM_30 + "&to=" + _D_TO
        + "&page=0&limit=10",
        "심볼형이 종목당 1콜인 반면 이쪽은 기간당 N페이지다. 워치리스트가 "
        "커질수록 어느 쪽이 싼지 갈리므로 둘 다 재서 비교한다.",
        ["symbol", "formType", "filingDate"],
        None,
    ),
    (
        "M&A 대체 경로 — 이름 검색형",
        "mergers-acquisitions-search?name=Apple",
        "mergers-acquisitions-latest 도 symbol 필터가 없다. 다만 이쪽은 "
        "**심볼이 아니라 회사명** 검색이라 티커→회사명 매핑이 필요하다. "
        "응답에 symbol 이 와야 역매칭이 가능하다.",
        ["symbol", "companyName", "targetedCompanyName"],
        None,
    ),
    (
        "B-1 전제 — 섹터 분류명 목록",
        "available-sectors",
        "historical-sector-performance 의 sector 파라미터에 넣을 정확한 문자열. "
        "추측으로 맞추면 다른 섹터에서 조용히 빈 배열이 온다.",
        ["sector"],
        None,
    ),
    (
        "B-1 전제 — 업종 분류명 목록",
        "available-industries",
        "industry-performance-snapshot 이 127건을 준 그 127개의 정확한 이름. "
        "B-1 설계의 필수 입력이다.",
        ["industry"],
        None,
    ),
]

# ══════════════════════════════════════════════════════════════════════════
# TIER B3 — B-1 설계 직전 확인 (2026-08-20)
#
# tierB2 에서 available-industries 가 **159건**을 줬는데
# industry-performance-snapshot 은 같은 날 **127건**이었다. 32개 차이다.
# 그리고 이력을 API 에서 받을 수 있는지가 아직 미확인이다.
#
# 이 세 콜로 B-1 설계의 미결 사항이 전부 닫힌다.
#
#   1) 백필이 가능한가            → historical-* 를 3년 요청해 실측
#        3년 오면  → 일회성 백필 → 즉시 백테스트 (대기 0)
#        단기만    → 스냅샷 매일 누적이 유일 경로 (몇 달 대기)
#      historical-price-eod 때도 같은 이유로 깊이를 따로 쟀다(diag_fmp_depth).
#
#   2) 어떤 이름을 쓰나           → 159개 전체 목록을 눈으로 확보
#      파라미터 문자열이 틀리면 402 도 404 도 아니고 **조용히 빈 배열**이
#      온다. 가장 찾기 어려운 실패다.
#
#   3) 159 vs 127 차이           → 목록을 받아야 어느 32개가 빠졌는지 대조 가능
#
# 상세 출력 렌더러를 붙인 6요소 튜플이다(다른 티어는 5요소).
# ══════════════════════════════════════════════════════════════════════════
TIER_B3 = [
    (
        "업종 분류명 — 159개 전체 목록",
        "available-industries",
        "historical-industry-performance 의 industry 파라미터에 넣을 정확한 "
        "문자열 전부. 스냅샷 127건과 대조해 어느 32개가 비는지 확인한다.",
        ["industry"],
        None,
        "names_industry",
    ),
    (
        "업종 성과 이력 깊이 — 3년 요청",
        "historical-industry-performance?industry=Semiconductors&from="
        + _D_FROM_3Y + "&to=" + _D_TO,
        "3년이 오면 159콜 일회성 백필 후 즉시 백테스트가 가능하다. "
        "단기만 오면 스냅샷을 몇 달 쌓는 수밖에 없다. 이 한 줄이 "
        "'지금 검증'과 '3개월 대기'를 가른다.",
        ["date", "industry", "averageChange"],
        None,
        "depth",
    ),
    (
        "섹터 성과 이력 깊이 — 3년 요청",
        "historical-sector-performance?sector=Technology&from="
        + _D_FROM_3Y + "&to=" + _D_TO,
        "위 항목과 동일. 섹터는 11개뿐이라 백필 비용이 11콜로 싸다 — "
        "업종이 단기만 주더라도 섹터는 백테스트가 가능할 수 있다.",
        ["date", "sector", "averageChange"],
        None,
        "depth",
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


def probe(path, need=None, contains=None, keep_data=False):
    """단일 엔드포인트 호출 + 필드 검증. 판정 dict 를 돌려준다.

    keep_data: True 면 파싱된 원자료를 out["data"] 에 담는다. 이름 목록 덤프나
      이력 깊이 측정처럼 **건수만으로는 답이 안 나오는** 확인에만 켠다.
      기본값이 False 인 이유는 응답이 수천 건일 수 있어서다(dividends-calendar
      는 30일치가 4000건이었다).
    """
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
        "data": None,
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
    if keep_data:
        out["data"] = data

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
# ══════════════════════════════════════════════════════════════════════════
# 상세 출력 — 건수만으로는 답이 안 나오는 확인들
# ══════════════════════════════════════════════════════════════════════════
def render_names(res, key):
    """분류명 전체를 줄바꿈해서 찍는다.

    왜 필요한가: historical-sector-performance / historical-industry-performance
    는 sector/industry 파라미터에 **FMP 가 정한 정확한 문자열**을 요구한다.
    틀리면 402 도 404 도 아니고 **조용히 빈 배열**이 온다 — 가장 찾기 어려운
    실패다. 이름을 눈으로 확보해야 구현에 들어갈 수 있다.
    """
    data = res.get("data")
    if not isinstance(data, list) or not data:
        print("        └ (목록 없음)")
        return
    names = []
    for rec in data:
        if isinstance(rec, dict) and rec.get(key):
            names.append(str(rec[key]))
    names = sorted(set(names))
    print("        └ 고유 " + str(len(names)) + "개:")
    line = "           "
    for nm in names:
        if len(line) + len(nm) + 3 > 96:
            print(line)
            line = "           "
        line += nm + " | "
    if line.strip():
        print(line.rstrip(" |"))


def render_depth(res, date_key="date"):
    """이력 깊이 — 최초/최종 날짜, 커버 기간, 요청 대비 충족 여부.

    왜 필요한가: 3년을 요청해도 플랜이 90일만 줄 수 있다. 그러면 백필 전략이
    통째로 달라진다(일회성 백필 vs 매일 스냅샷 누적 몇 달). historical-price-eod
    때도 같은 이유로 깊이를 따로 쟀다.
    """
    data = res.get("data")
    if not isinstance(data, list) or not data:
        print("        └ (깊이 측정 불가)")
        return
    dates = []
    for rec in data:
        if isinstance(rec, dict):
            d = str(rec.get(date_key) or "").strip()[:10]
            if len(d) == 10:
                dates.append(d)
    if not dates:
        print("        └ (날짜 필드 없음 — key=" + date_key + ")")
        return
    dates = sorted(set(dates))
    first, last = dates[0], dates[-1]
    try:
        d0 = datetime.strptime(first, "%Y-%m-%d").date()
        d1 = datetime.strptime(last, "%Y-%m-%d").date()
        span_days = (d1 - d0).days
    except Exception:
        span_days = 0
    yrs = span_days / 365.0
    print("        └ 고유 날짜 " + str(len(dates)) + "일 · "
          + first + " ~ " + last
          + " · 커버 " + str(span_days) + "일(" + ("%.2f" % yrs) + "년)")
    # 요청은 3년(1095일). 실제로 얼마나 왔는지가 백필 전략을 가른다.
    if span_days >= 1000:
        print("           ✅ 3년 백필 가능 — 일회성 백필 후 즉시 백테스트")
    elif span_days >= 300:
        print("           🟠 1년 안팎 — 백테스트 표본 제한적. 스냅샷 누적 병행 권장")
    else:
        print("           🔴 단기만 제공 — 일회성 백필 불가. 스냅샷 매일 누적이 유일 경로")
    # 거래일 밀도 — 빈 구간이 있으면 백테스트가 왜곡된다
    if span_days > 0:
        dens = len(dates) / (span_days / 7.0 * 5.0) if span_days >= 7 else 0
        if dens and dens < 0.7:
            print("           ⚠️ 거래일 대비 밀도 " + ("%.0f" % (dens * 100))
                  + "% — 결측 구간이 있다. 백테스트 전 확인 필요")


_DETAIL = {
    "names_sector": lambda r: render_names(r, "sector"),
    "names_industry": lambda r: render_names(r, "industry"),
    "depth": lambda r: render_depth(r, "date"),
}


def run_group(title, targets, results):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)
    for t in targets:
        # 기존 티어는 5요소, B3 는 상세 렌더러를 붙인 6요소다. 둘 다 받는다.
        label, path, impact, need, contains = t[0], t[1], t[2], t[3], t[4]
        detail_kind = t[5] if len(t) > 5 else None
        print("")
        print("── " + label)
        print("   용도: " + impact)
        if need:
            print("   필요 필드: " + ", ".join(need))
        r = probe(path, need=need, contains=contains,
                  keep_data=bool(detail_kind))
        show(r)
        if detail_kind and r["verdict"] in ("LIVE", "FIELDS"):
            fn_d = _DETAIL.get(detail_kind)
            if fn_d:
                try:
                    fn_d(r)
                except Exception as e:
                    print("        └ (상세 출력 실패: " + str(e)[:60] + ")")
        results.append((label, path, impact, r))
        time.sleep(SLEEP_SEC)


def main():
    if not _KEY:
        print("❌ FMP_API_KEY 가 비어 있습니다. 중단.")
        return 2

    run_a = _TIER in ("tiera", "all")
    run_b = _TIER in ("tierb", "all")
    run_b2 = _TIER in ("tierb2", "all")
    run_b3 = _TIER in ("tierb3", "all")
    run_c = _TIER in ("tierc", "all")
    run_g = _TIER in ("grades", "all")

    ncalls = (len(TIER_A) if run_a else 0) + (len(TIER_B) if run_b else 0) \
        + (len(TIER_B2) if run_b2 else 0) + (len(TIER_B3) if run_b3 else 0) \
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
    if run_b2:
        run_group("Tier B2 — tierB 후속 확인 (심볼 필터 대체 경로 + 분류명)",
                  TIER_B2, results)
    if run_b3:
        run_group("Tier B3 — B-1 설계 직전 확인 (이력 깊이 + 분류명 전체)",
                  TIER_B3, results)
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
