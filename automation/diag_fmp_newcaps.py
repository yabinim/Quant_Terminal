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

  tierC (4콜) — 탐색적 신규 기능   ★ 2026-08-22 실행 완료 · 실행 그룹 삭제됨
  grades (3콜) — 등급 중복 확인    ★ 콜 없이 종결 · 실행 그룹 삭제됨
    두 그룹의 판정은 아래 §7 블록에 기록돼 있다. 다시 돌릴 이유가 없다.

  tierE (1콜) — tierC 의 유일한 잔여분
    historical-industry-pe             tierC 는 90일을 요청해 90일을 받았다.
                                       그건 깊이를 못 잰 것이다(tierB3 와 동일한
                                       요청값 종속 함정). 7년을 요청해 다시 잰다.

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
    PROBE_TIER=tierB4  3콜   ← tierB3 의 깊이 판정 재측정 (요청값 종속 착오 교정)
    PROBE_TIER=tierD   4콜   ← 공시 지연 실측 + grades 파라미터 지원
    PROBE_TIER=tierE   1콜   ← 업종 PER 이력 깊이 (tierC 잔여분)
    PROBE_TIER=all    30콜

⚠️ all 은 5+9+5+3+3+4+1 = 30 이다. **이 숫자를 손으로 세지 마라.** 실행하면
   헤더에 `호출 N콜` 이 찍히는데 그게 len() 으로 계산된 진짜 값이다. 과거에
   docstring 32 · yml 주석 32 · yml 입력설명 36 으로 세 군데가 갈렸다.

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
if _TIER not in ("tiera", "tierb", "tierb2", "tierb3", "tierb4",
                 "tierd", "tiere", "all"):
    # ⚠️ 낡은 yml 이 tierC / grades 를 보내면 여기서 조용히 tierA 로 떨어진다.
    #    그 상태로도 5콜이 나가고 로그 헤더에는 `티어: tiera` 가 찍힌다 —
    #    "왜 업종 PER 결과가 없지"의 원인이 되므로 헤더를 반드시 확인할 것.
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
# tierB4 — 깊이 재측정용. 3년(tierB3)은 **요청한 만큼만 돌아와서** 한도를 재지
# 못했다. 넉넉히 요청해야 min(date) 가 어디서 멈추는지 보인다.
# 가격 엔드포인트의 실측 한도가 5년(1255봉)이었으므로 그보다 넉넉한 7년을 쓴다.
_D_FROM_7Y = (_TODAY - timedelta(days=365 * 7)).isoformat()
# 불량 입력 검사용 좁은 창. from/to 가 응답을 실제로 자르는지 본다.
_D_NARROW_FROM = (_TODAY - timedelta(days=20)).isoformat()
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


# ══════════════════════════════════════════════════════════════════════════
# Tier B4 — tierB3 의 깊이 판정을 다시 잰다 (3콜)
#
# 왜 다시 재나
# ────────────
# tierB3 는 `from = today - 365*3` 을 보냈고 `2023-08-21 ~ 2026-08-20 · 3.00년`
# 을 받았다. 그 결과가 "이 플랜은 업종 이력 3년까지"로 기록됐다.
#
# **그건 한도의 증거가 아니다.** 요청한 날짜부터 데이터가 왔다는 것은
# `from` 이 먹었다는 뜻일 뿐, 4년을 요청하면 어땠는지는 아무도 안 봤다.
# 판별력 없는 지표를 판별자로 쓴 것이다 — 이 프로젝트에서 반복된 실수다
# (무필터 대비 건수, 시드 포함 여부에 이어 세 번째).
#
# 반례가 이미 있다: `historical-price-eod/full` 의 진짜 한도는 실측 결과
# **5년(1255봉, limit 무관 롤링)** 이었다(diag_fmp_depth → run_signal_backtest:152).
# 계정 플랜 차원의 한도라면 업종도 3년이 아닐 수 있다.
#
# 3년과 5년의 차이가 판정을 가른다
# ────────────────────────────────
#   3년 → 롤링 워크포워드 6창이 한 국면(상승장) 연속 6분기에 갇힌다
#   5년 → 학습창에 2022 하락장이 들어온다. 질문의 질이 달라진다
#
# 설계
# ────
#   ① 좁은 창(20일)  — **불량 입력 검사**. from/to 가 응답을 자르는가.
#                       판별자는 건수가 아니라 응답 날짜 **범위 폭**이다.
#                       여기가 🔴 면 ②③ 판정은 전부 무효다.
#   ② 업종 7년 요청  — min(date) 가 요청 from 에서 멈추는가, 그 뒤에서 멈추는가
#   ③ 섹터 7년 요청  — 동일
#
# ⚠️ TIER_B3 는 그대로 둔다. "3년 요청" 기록이 이 착오의 증거다.
# ══════════════════════════════════════════════════════════════════════════
TIER_B4 = [
    (
        "① 불량 입력 검사 — 좁은 창(20일) 요청",
        "historical-industry-performance?industry=Semiconductors&from="
        + _D_NARROW_FROM + "&to=" + _D_TO,
        "20일을 요청했는데 3년이 돌아오면 from/to 가 무시된다는 뜻이고, "
        "그러면 ②③ 의 깊이 판정은 전부 무효다. 진단을 믿기 전에 알려진 "
        "불량 입력에서 실패하는지부터 확인한다.",
        ["date", "industry", "averageChange"],
        None,
        "range_check",
    ),
    (
        "② 업종 성과 이력 깊이 — 7년 요청",
        "historical-industry-performance?industry=Semiconductors&from="
        + _D_FROM_7Y + "&to=" + _D_TO,
        "tierB3 는 3년을 요청해 3년을 받았고 그게 '플랜 한도 3년'으로 "
        "기록됐다. 요청한 날짜부터 왔다는 건 한도를 못 쟀다는 뜻이다. "
        "넉넉히 요청해 min(date) 가 어디서 멈추는지 본다.",
        ["date", "industry", "averageChange"],
        None,
        "depth_req",
    ),
    (
        "③ 섹터 성과 이력 깊이 — 7년 요청",
        "historical-sector-performance?sector=Technology&from="
        + _D_FROM_7Y + "&to=" + _D_TO,
        "위와 동일. 업종과 섹터가 다른 깊이를 줄 수 있다 — tierB3 에서도 "
        "같은 구간에 777 vs 772 로 건수가 달랐다(생성 규칙이 다르다는 신호).",
        ["date", "sector", "averageChange"],
        None,
        "depth_req",
    ),
]

# ══════════════════════════════════════════════════════════════════════════
# §7 Tier C — 2026-08-22 00:28 UTC 실행 완료. **실행 그룹을 삭제했다.**
# ══════════════════════════════════════════════════════════════════════════
# 4콜 전부 판정이 났다. 드롭다운에 남겨두면 닫힌 질문에 콜이 나간다.
# 판정만 남긴다 — 재검토 금지.
#
#   senate-latest?page=0&limit=10          ✅ 200 · 10건 · symbol 있음
#   house-latest?page=0&limit=10           ✅ 200 · 10건 · symbol 있음
#     → 경로는 살아 있지만 **tierD 가 전제를 무너뜨렸다.**
#        상원 지연 중앙값 572일 / 하원 27일 · 7일 이내 3%.
#        VERDICT_2026-08-22_congress_trades_terminated.md · app.py:21546.
#        "최신 피드가 온다"는 판별력이 없었다 — 45일 늦은 것도 최신 피드다.
#
#   institutional-ownership/holder-performance-summary   🔒 402
#     → 경로는 맞고 플랜에 미포함. 코드로 해결되지 않는다. 영구 제외.
#
#   historical-industry-pe                 ✅ 200 · 65건
#     응답 키: date, exchange, industry, pe
#     from=today-90d 로 65건 → 90 캘린더일 ≈ 62 거래일이므로 **from/to 가
#     실제로 먹는다**(grades 와 정반대다). 범위 통제는 여기서 통과했다.
#     ⚠️ 그러나 **깊이는 재지 못했다.** 90일을 요청해 90일을 받은 것뿐이다.
#        → tierE 로 이월. 이 파일에서 유일하게 열려 있는 질문이다.
#
# ══════════════════════════════════════════════════════════════════════════
# §7 grades 그룹 — **콜 없이 종결.** 실행 그룹을 삭제했다.
# ══════════════════════════════════════════════════════════════════════════
# 원래 질문: "grades?symbol= 가 기존 등급 엔드포인트와 겹치는가" (3콜 예정).
# 답이 이미 손에 있었다. 콜을 쓸 필요가 없었다.
#
#   grades            키: action, date, gradingCompany, newGrade,
#                         previousGrade, symbol      ← tierD 로그(2026-08-22)
#   grades-consensus  키: strongBuy, buy, hold, sell, strongSell,
#                         consensus, symbol          ← app.py:8428 / 21200 파싱부
#   ratings-snapshot  이미 **제거 완료**. 호출 0건이고 diag_analyst_congress
#                     A-1 이 래칫으로 잠그고 있다. 비교 대상 자체가 없다.
#
# 판정: 겹치는 필드는 `symbol` 뿐이다. **중복이 아니다** —
#   grades 는 등급 변경 **이벤트 로그**, grades-consensus 는 **현재 스냅샷
#   집계**다. 축이 다르다.
#
# 실사용 제약(tierD 실측): grades 는 limit 도 from/to 도 무시한다. 종목당
#   전체 이력이 통으로 온다(AAPL 1787건). **워치리스트 전체 적용 불가.**
#   단일 종목 온디맨드 1콜은 기술적으로 가능하나 그건 신규 기능이지
#   이 프로브의 잔여 과제가 아니다.


# ══════════════════════════════════════════════════════════════════════════
# Tier E — tierC 의 유일한 잔여분 (2026-09-03, 1콜)
# ══════════════════════════════════════════════════════════════════════════
# tierC 가 90일을 요청해 90일을 받았다. tierB3 와 **같은 함정**이다:
# 요청한 날짜부터 데이터가 왔다는 것은 한도를 못 쟀다는 뜻이지 한도가 그
# 값이라는 뜻이 아니다. 형제 엔드포인트(historical-industry-performance)가
# 7.0년인 것도 근거가 안 된다 — 형제로 추론하지 않는다는 게 §7 교훈이다.
#
# 판별자: min(date) − 요청 from  (render_depth_req)
#   ≈ 0  → 한도 미도달. 확보된 폭이 얼마인지만 알 수 있다
#   ≫ 0  → 한도 발견. min(date) 가 실제 상한이다
#
# 사전 확정 기준 (결과를 보고 고치지 않는다):
#   확보 폭 5년 이상 → 데이터는 쓸 만하다. **기록 후 보류**
#   5년 미만        → 종결. 학습창에 2022 하락장이 안 들어온다
#
# ⚠️ ✅ 가 나와도 자동 착공이 아니다. 기능화하려면 industry_core 와 같은
#    구조가 필요하다: 일회성 백필 149콜 + 새 와이드 시트 + 일일 유지콜
#    (`industry-pe-snapshot` 존재 여부 **미확인** — 없으면 매일 149콜이라
#    유지 불가). 게다가 형제 신호인 업종 모멘텀은 2026-09-01 롤링
#    워크포워드에서 6창 중 1창으로 부결됐다. 착공은 별도 판단이다.
TIER_E = [
    (
        "업종 PER 시계열 — 이력 깊이 실측",
        "historical-industry-pe?industry=Semiconductors&from="
        + _D_FROM_7Y + "&to=" + _D_TO,
        "tierC 의 90일 요청은 깊이를 못 쟀다. 7년을 요청해 상한을 찾는다.",
        ["date", "industry", "pe"],
        None,
        "depth_req",
    ),
]

# ══════════════════════════════════════════════════════════════════════════
# Tier D — 설계 전에 재야 하는 두 가지 (2026-08-22, 4콜)
# ══════════════════════════════════════════════════════════════════════════
# D-1/D-2 의회 거래 피드 — **지연을 재기 전에는 설계하지 않는다.**
#   tierC 에서 senate-latest / house-latest 가 살아 있고 symbol 도 온다는 건
#   확인했다. 그런데 '역방향 조기 발굴 입력'이라는 용도는 **공시가 빨라야**
#   성립한다. 미 의회는 법정 공시 기한이 최대 45일이다. 응답에
#   transactionDate 와 disclosureDate 가 **둘 다** 오므로 실측이 가능하다.
#
#   판별자: 중앙값(disclosureDate − transactionDate)
#     건수나 "최신 피드가 온다"는 판별력이 없다 — 45일 늦은 것도 최신 피드다.
#
#   사전 확정 기준 (결과를 보고 고치지 않는다):
#     중앙값 14일 이하 → 조기 발굴 입력으로 유효. 설계 진행
#     15~30일          → 조건부. '조기 발굴'이 아니라 '누적 관찰'로만
#     30일 초과        → **종결.** 이 터미널의 모멘텀 철학에 맞지 않는다
#
# D-3/D-4 grades 파라미터 — 쓸 수 있느냐가 아니라 **감당되느냐**다.
#   grades?symbol=AAPL 은 200 이지만 **1787건**이다. 워치리스트 전체에 쓰면
#   종목당 수백 KB 다. limit 또는 from/to 가 먹어야 실사용이 가능하다.
#
#   판별자: 무필터 1787건이라는 **측정된 기준선**이 있으므로 여기서는 건수
#     비교가 유효하다(기준선 없이 건수를 판별자로 쓴 게 1차 오판이었다).
#     from/to 는 건수만으로 부족하다 — 응답 날짜가 요청 구간 안인지도 본다.
_GRADES_BASELINE = 1787       # grades?symbol=AAPL 무필터 실측 (2026-08-22)
_D_FROM_365 = (_TODAY - timedelta(days=365)).isoformat()

TIER_D = [
    (
        "상원 거래 — 공시 지연 실측",
        "senate-latest?page=0&limit=100",
        "'조기 발굴 입력' 전제 검정. transactionDate → disclosureDate 간격.",
        ["symbol", "transactionDate", "disclosureDate"],
        None,
        "cong_lag",
    ),
    (
        "하원 거래 — 공시 지연 실측",
        "house-latest?page=0&limit=100",
        "위와 동일. 상·하원이 다를 수 있어 따로 잰다.",
        ["symbol", "transactionDate", "disclosureDate"],
        None,
        "cong_lag",
    ),
    (
        "등급 이력 — limit 지원 여부",
        "grades?symbol=AAPL&limit=10",
        "무필터 1787건(측정됨)이 10건으로 줄면 limit 이 먹는 것이다.",
        ["symbol", "date"],
        None,
        "grades_limit",
    ),
    (
        "등급 이력 — from/to 지원 여부",
        "grades?symbol=AAPL&from=" + _D_FROM_365 + "&to=" + _D_TO,
        "최근 1년만 받을 수 있으면 '30일 상향−하향'이 실사용 가능해진다.",
        ["symbol", "date"],
        None,
        "grades_range",
    ),
]


# GRADES 그룹은 2026-09-03 에 삭제했다. 판정은 위 §7 블록에 있다(콜 0으로 종결).


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


def _req_param(path, key):
    """요청 경로에서 쿼리 파라미터 값을 꺼낸다. 없으면 빈 문자열.

    렌더러가 **자기 요청**을 알아야 응답과 대조할 수 있다. res["path"] 에
    요청 경로가 그대로 들어 있으므로 스키마 변경 없이 된다.
    """
    tail = path.split("?", 1)[1] if "?" in path else ""
    for part in tail.split("&"):
        if part.startswith(key + "="):
            return part[len(key) + 1:]
    return ""


def _date_span(res, date_key="date"):
    """응답 → (정렬된 고유 날짜, 첫 date, 끝 date, 폭). 못 재면 None."""
    data = res.get("data")
    if not isinstance(data, list) or not data:
        return None
    dates = []
    for rec in data:
        if isinstance(rec, dict):
            d = str(rec.get(date_key) or "").strip()[:10]
            if len(d) == 10:
                dates.append(d)
    if not dates:
        return None
    dates = sorted(set(dates))
    try:
        d0 = datetime.strptime(dates[0], "%Y-%m-%d").date()
        d1 = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    except Exception:
        return None
    return dates, d0, d1, (d1 - d0).days


def render_range_check(res, date_key="date"):
    """좁은 창을 요청해 from/to 가 실제로 응답을 자르는지 본다 — 불량 입력 검사.

    판별자가 왜 '건수'가 아니라 '범위 폭'인가
    ─────────────────────────────────────────
    건수는 다른 이유로도 달라진다(휴장일 행 생성, 결측, 기본 limit).
    20일을 요청했는데 3년 폭이 돌아오면 필터가 안 먹은 것 말고 해석의 여지가
    없다. 이 프로젝트에서 건수를 판별자로 썼다가 두 번 틀린 전례가 있다.

    이 검사가 🔴 면 depth_req 판정은 전부 무효다 — 진단을 믿기 전에
    알려진 불량 입력에서 실패하는지부터 확인한다는 규칙의 적용이다.
    """
    got = _date_span(res, date_key)
    if not got:
        print("        └ (날짜 없음 — 범위 검사 불가)")
        return
    dates, _d0, _d1, span = got
    f_s = _req_param(res.get("path", ""), "from")
    t_s = _req_param(res.get("path", ""), "to")
    try:
        req_span = (datetime.strptime(t_s, "%Y-%m-%d").date()
                    - datetime.strptime(f_s, "%Y-%m-%d").date()).days
    except Exception:
        print("        └ (요청 from/to 파싱 실패 — 범위 검사 불가)")
        return
    print("        └ 요청 " + f_s + " ~ " + t_s
          + "  (폭 " + str(req_span) + "일)")
    print("           응답 " + dates[0] + " ~ " + dates[-1]
          + "  (폭 " + str(span) + "일 · " + str(len(dates)) + "건)")
    if req_span <= 0:
        print("           🟠 요청 폭이 0 이하 — 판정 불가")
    elif span > req_span * 3:
        print("           🔴 from/to 무시됨 — 요청 폭의 "
              + ("%.1f" % (span / float(req_span))) + "배가 돌아왔다")
        print("              ⛔ 아래 ②③ 이력 깊이 판정은 전부 무효다.")
        print("                 (tierB3 의 '3년' 기록도 같이 무효가 된다)")
    else:
        print("           ✅ from/to 존중됨 — ②③ 깊이 판정의 전제가 성립한다")


def render_depth_req(res, date_key="date"):
    """이력 깊이 — **요청한 from 과 실제 min(date) 를 대조한다.**

    render_depth 와 분리한 이유
    ───────────────────────────
    render_depth 는 판정 문구에 요청값이 박혀 있다:

        if span_days >= 1000:
            print("✅ 3년 백필 가능 — ...")

    7년을 요청해서 3년만 와도 이 문구가 나온다. **한도에 걸린 상황을 성공으로
    표시한다.** 실제로 그 착오가 났다 — tierB3 가 3년을 요청해 3년을 받았고,
    그게 "플랜 한도 3년"으로 기록됐다.

    요청한 날짜부터 데이터가 왔다는 것은 **한도를 재지 못했다**는 뜻이지
    한도가 그 값이라는 뜻이 아니다. 반례가 있다: historical-price-eod 의 진짜
    한도는 5년(1255봉, limit 무관)이었다.

    판별자: min(date) − 요청 from
        ≈ 0  → 한도 미도달. 이 요청으로는 상한을 못 잰다
        ≫ 0  → 한도 발견. min(date) 가 실제 상한이다
    """
    got = _date_span(res, date_key)
    if not got:
        print("        └ (깊이 측정 불가 — 날짜 없음)")
        return
    dates, d0, d1, span = got
    f_s = _req_param(res.get("path", ""), "from")
    try:
        req_from = datetime.strptime(f_s, "%Y-%m-%d").date()
    except Exception:
        print("        └ (요청 from 파싱 실패 — 대조 불가)")
        return
    gap = (d0 - req_from).days
    print("        └ 요청 from : " + f_s)
    print("           실제 최초 : " + dates[0] + "   (요청 대비 "
          + ("%+d" % gap) + "일)")
    print("           실제 최종 : " + dates[-1] + " · 고유 날짜 "
          + str(len(dates)) + "일 · 폭 " + str(span) + "일")

    if gap <= 7:
        print("           🟢 한도 미도달 — 요청한 만큼 전부 왔다")
        print("              상한은 여전히 미측정이다. 다만 "
              + ("%.1f" % (span / 365.0)) + "년 확보되므로")
        print("              롤링 워크포워드에는 충분하다")
    elif gap > 30:
        roll = (d1 - d0).days / 365.0
        print("           🔵 한도 발견 — " + dates[0] + " 에서 멈춘다")
        print("              실제 상한 ≈ " + ("%.1f" % roll)
              + "년 (롤링). 요청을 더 늘려도 이보다 앞은 안 온다")
    else:
        print("           🟠 판정 보류 — 차이가 " + str(gap)
              + "일. 휴일·데이터 시작일 경계일 수 있다")
        print("              from 을 더 앞으로 밀어 재확인 필요")

    # 밀도 — 1년이 와도 주 1건씩만 오면 백테스트가 왜곡된다
    if span >= 7:
        dens = len(dates) / (span / 7.0 * 5.0)
        if dens < 0.7:
            print("           ⚠️ 거래일 대비 밀도 " + ("%.0f" % (dens * 100))
                  + "% — 결측 구간이 있다. 백테스트 전 확인 필요")


def _pick(d, *names):
    """대소문자 흔들림에 견디는 필드 추출."""
    if not isinstance(d, dict):
        return None
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def render_cong_lag(res):
    """의회 거래 공시 지연 — 중앙값(disclosureDate − transactionDate).

    왜 중앙값인가
    ─────────────
    평균은 한 건의 극단값(몇 년 전 거래를 뒤늦게 정정 공시)에 끌려간다.
    판정은 '보통 며칠 늦는가'라서 중앙값이 맞다. p90 은 꼬리 확인용으로만 찍는다.

    ⚠️ 건수·최신 여부는 판별자가 아니다. 45일 늦은 것도 '최신 피드'다.
       이 프로젝트에서 판별력 없는 판별자를 고른 게 여섯 번이다.
    """
    rows = res.get("data") or []
    if not isinstance(rows, list) or not rows:
        print("        └ (레코드 없음 — 지연 측정 불가)")
        return
    lags, bad = [], 0
    for r in rows:
        t = str(_pick(r, "transactionDate", "transaction_date") or "")[:10]
        d = str(_pick(r, "disclosureDate", "disclosure_date") or "")[:10]
        try:
            dt_t = datetime.strptime(t, "%Y-%m-%d").date()
            dt_d = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            bad += 1
            continue
        lags.append((dt_d - dt_t).days)
    if not lags:
        print("        └ (날짜 파싱 전멸 " + str(bad) + "건 — 측정 불가)")
        return
    lags.sort()
    n = len(lags)
    med = lags[n // 2] if n % 2 else (lags[n // 2 - 1] + lags[n // 2]) / 2.0
    p90 = lags[min(n - 1, int(n * 0.9))]
    within7 = 100.0 * sum(1 for x in lags if x <= 7) / n
    neg = sum(1 for x in lags if x < 0)
    print("        └ 표본 " + str(n) + "건"
          + (" (날짜 불량 " + str(bad) + "건 제외)" if bad else ""))
    print("           지연 중앙값 " + ("%.0f" % med) + "일 · p90 "
          + str(p90) + "일 · 최소 " + str(lags[0]) + "일 · 최대 "
          + str(lags[-1]) + "일")
    print("           7일 이내 " + ("%.0f" % within7) + "%"
          + (" · 음수(공시가 거래보다 빠름) " + str(neg) + "건" if neg else ""))
    # 사전 확정 기준 — 결과를 보고 고치지 않는다.
    if med <= 14:
        print("           🟢 조기 발굴 입력으로 유효 — 설계 진행 가능")
    elif med <= 30:
        print("           🟡 조건부 — '조기 발굴'이 아니라 '누적 관찰'로만")
    else:
        print("           🔴 종결 — 중앙값 " + ("%.0f" % med) + "일은 모멘텀 "
              "철학에 맞지 않는다")
        print("              (이 판정은 사전 확정이다. 재협상하지 않는다)")


def render_grades_limit(res):
    """limit 이 먹는가. 무필터 기준선 1787건과 대조한다."""
    rows = res.get("data") or []
    n = len(rows) if isinstance(rows, list) else 0
    want = _req_param(res.get("path", ""), "limit")
    print("        └ 요청 limit=" + str(want) + " · 응답 " + str(n) + "건"
          + " · 무필터 기준선 " + str(_GRADES_BASELINE) + "건")
    try:
        want_n = int(want)
    except Exception:
        print("           (limit 파싱 실패 — 판정 불가)")
        return
    if n == want_n:
        print("           ✅ limit 존중됨 — 워치리스트 실사용 가능")
    elif n >= _GRADES_BASELINE * 0.9:
        print("           🔴 limit 무시됨 — 전체 이력이 그대로 왔다")
        print("              종목당 수백 KB. 워치리스트 전체 적용 불가.")
    else:
        print("           🟠 요청과도 기준선과도 다르다(" + str(n) + "건) — "
              "다른 기본 상한이 걸렸을 수 있다. 단정하지 않는다.")


def render_grades_range(res):
    """from/to 가 먹는가. **건수만으로 판정하지 않는다** — 날짜가 구간 안인지 본다."""
    rows = res.get("data") or []
    n = len(rows) if isinstance(rows, list) else 0
    f_s = _req_param(res.get("path", ""), "from")
    t_s = _req_param(res.get("path", ""), "to")
    ds = sorted(str(_pick(r, "date") or "")[:10] for r in rows
                if isinstance(r, dict) and _pick(r, "date"))
    ds = [d for d in ds if len(d) == 10]
    print("        └ 요청 " + str(f_s) + " ~ " + str(t_s)
          + " · 응답 " + str(n) + "건 · 무필터 기준선 "
          + str(_GRADES_BASELINE) + "건")
    if not ds:
        print("           (날짜 없음 — 판정 불가)")
        return
    print("           응답 날짜 " + ds[0] + " ~ " + ds[-1])
    outside = sum(1 for d in ds if d < str(f_s) or d > str(t_s))
    if outside:
        print("           🔴 from/to 무시됨 — 구간 밖 " + str(outside) + "건")
        print("              건수가 줄었더라도 무시된 것이다(다른 상한일 뿐).")
    elif n >= _GRADES_BASELINE * 0.9:
        print("           🟠 구간 밖은 없는데 건수가 기준선과 같다 — "
              "우연히 전부 구간 안일 수 있다. 단정하지 않는다.")
    else:
        print("           ✅ from/to 존중됨 — '최근 30일 상향−하향' 실사용 가능")


_DETAIL = {
    "names_sector": lambda r: render_names(r, "sector"),
    "names_industry": lambda r: render_names(r, "industry"),
    "depth": lambda r: render_depth(r, "date"),
    "range_check": lambda r: render_range_check(r, "date"),
    "depth_req": lambda r: render_depth_req(r, "date"),
    # ⚠️ lambda 로 감싼다. 위 기존 항목들과 같은 형태이고, 그게 우연이 아니다 —
    #    함수 객체를 직접 넣으면 **이 dict 를 만드는 시점에** 이름을 찾는다.
    #    2026-08-22 에 렌더러를 이 dict 아래에 정의했다가 NameError 로 죽었다.
    #    ast.parse 도 check_py311 도 통과한다(문법은 멀쩡하다). 임포트만 깨진다.
    #    lambda 면 호출 시점에 찾으므로 정의 순서가 바뀌어도 안 깨진다.
    "cong_lag": lambda r: render_cong_lag(r),
    "grades_limit": lambda r: render_grades_limit(r),
    "grades_range": lambda r: render_grades_range(r),
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
    run_b4 = _TIER in ("tierb4", "all")
    run_d = _TIER in ("tierd", "all")
    run_e = _TIER in ("tiere", "all")

    # ⚠️ 콜 수는 len() 으로만 센다. 주석에 적어둔 숫자를 믿지 말 것 —
    #    2026-09-03 이전에 docstring 32 · yml 주석 32 · yml 설명 36 이 갈렸다.
    ncalls = (len(TIER_A) if run_a else 0) + (len(TIER_B) if run_b else 0) \
        + (len(TIER_B2) if run_b2 else 0) + (len(TIER_B3) if run_b3 else 0) \
        + (len(TIER_B4) if run_b4 else 0) \
        + (len(TIER_D) if run_d else 0) + (len(TIER_E) if run_e else 0)

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
    if run_b4:
        run_group("Tier B4 — 이력 깊이 재측정 (tierB3 의 요청값 종속 판정 교정)",
                  TIER_B4, results)
    if run_d:
        run_group("Tier D — 공시 지연 실측 + grades 파라미터 지원",
                  TIER_D, results)
    if run_e:
        run_group("Tier E — 업종 PER 이력 깊이 (tierC 잔여분)",
                  TIER_E, results)

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
