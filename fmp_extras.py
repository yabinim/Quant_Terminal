# -*- coding: utf-8 -*-
"""
fmp_extras.py — app.py 보조 모듈.

기존 app.py가 아직 쓰지 않던 FMP `stable` 엔드포인트들을 한곳에 모았습니다.
모든 함수는 app.py와 동일한 규칙을 따릅니다:
  - 키: st.secrets["FMP_API_KEY"]
  - 캐시: @st.cache_data(ttl=3600)
  - 실패 시 절대 예외를 던지지 않고 빈 dict/list/None 반환 (앱 크래시 방지)

app.py 상단에 다음 한 줄만 추가하면 됩니다:
    import fmp_extras as fx
이후 fx.fmp_dcf("AAPL") 처럼 호출하세요.
"""

from __future__ import annotations

import datetime as _dt
import os as _os
import time as _time

import numpy as np
import pandas as pd
import pytz
import requests

# FMP HTTP 계층 SSOT — 레이트리밋/재시도/URL 조립. streamlit 무의존 모듈.
#   이 모듈의 모든 FMP 호출은 여기를 거친다(카운터가 프로세스당 하나여야 하므로).
import fmp_http as _fh

# streamlit 은 앱 환경에서만 존재 — 자동화(GitHub Actions)에서도 이 모듈을
# import 할 수 있도록 선택적 임포트 + 무해한 심(shim)으로 대체한다.
# (심: cache_data 는 no-op 데코레이터, secrets 는 빈 dict → _key 가 환경변수로 폴백)
try:
    import streamlit as st  # type: ignore
except Exception:  # pragma: no cover — 자동화 환경
    class _StShim:
        secrets: dict = {}
        @staticmethod
        def cache_data(*a, **k):
            def _wrap(fn):
                return fn
            return _wrap
    st = _StShim()  # type: ignore

_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 7
_ET_TZ = pytz.timezone("America/New_York")


def _key() -> str:
    """app.py의 _fmp_key()와 동일 + 자동화 폴백: st.secrets 미설정 시 환경변수 FMP_API_KEY."""
    try:
        k = str(st.secrets.get("FMP_API_KEY", "") or "").strip()
        if k:
            return k
    except Exception:
        pass
    return str(_os.environ.get("FMP_API_KEY", "") or "").strip()


# ══════════════════════════════════════════════════════════════════════════
# 일봉 조회 창 — 봉(거래일) ↔ 달력일 환산 SSOT
# ══════════════════════════════════════════════════════════════════════════
# FMP `historical-price-eod` 의 `limit` 은 **무시된다**(실측 확정). 창을 지정하는
# 유일한 수단은 `from`/`to` 이고 그 단위는 **달력일**이다. 반면 하류 지표 요구치는
# 전부 **봉 수**로 표현된다 — 두 단위가 코드 안에서 계속 섞인다. 환산을 여기
# 한 곳에만 둔다.
#
# ⚠️ 이 상수를 복제하지 말 것. 2026-08-28(2차)에 app.py 와 run_watchlist_alerts.py
#    가 각각 사본을 가질 뻔했다. 0.6871 이 두 벌이 되면 한쪽만 갱신되고, 창이
#    조용히 짧아지는 실패는 **에러 로그를 남기지 않는다**. diag_hist_window [X] 가
#    저장소 전역에서 이 리터럴의 중복을 금지한다.
HIST_TD_PER_CD = 0.6871
#   실측 비율(거래일 ÷ 달력일). 추정치 아님 — diag_fmp_window.py 기준선 응답.

HIST_WINDOW_DAYS = 460
#   기본 창(달력일). 최대 구속 요구는 regime_core.market_warnings 의 272봉이다
#   (vol 20창 → 252 중앙값 체인: 20 + 252 = 272). 272 ÷ 0.6871 = 395.9 달력일이
#   무마진 최소치이고, 460일 요청 시 실측 316봉(2026-08-28 probe) — 여유 44봉(16%).
#   마진을 이 방향으로 기울이는 이유는 비용이 비대칭이기 때문이다:
#     · 언더슈트 → 260봉 하드게이트 미달 → market_warnings=None →
#       market_gate_status.available=False → 게이트가 **조용히** 꺼진다(fail-open).
#     · 오버슈트 → 페이로드만 늘어난다. 관측 가능하고 되돌리기 쉽다.

HIST_MAX_DAYS = 1826
#   상한(약 5년 ≈ 1,254봉). limit 이 무시되던 시절의 사실상 동작과 같은 크기.

HIST_MIN_DAYS = 21
#   하한(달력일). 요구 봉수가 작을 때 순수 환산만 쓰면 창이 위험하게 짧아진다:
#   2봉 → ceil(2 ÷ 0.6871) = **3달력일**이다. 금요일 휴장(굿프라이데이·독립기념일
#   등)이면 목 종가에서 다음 월요일까지 간격만 4달력일이라 **0봉**이 온다.
#   비상 휴장은 더 길다 — 2001-09-11 은 7달력일 연속이었다.
#   21일이면 그 7일짜리 휴장이 창에 통째로 들어와도 14달력일 ≈ 9봉이 남는다.
#
#   비용은 여기서도 비대칭이다:
#     · 언더슈트 → `len(rows) >= 6` 같은 가드에 걸려 **신호가 조용히 사라진다**.
#       rolling(20, min_periods=10) 이면 더 나쁘다 — 에러 없이 더 짧은 창의
#       평균이 나와 값이 조용히 틀린다.
#     · 오버슈트 → 수 KB 페이로드. 관측 가능하고 되돌리기 쉽다.


def bars_for_calendar_days(days) -> int:
    """달력일 → 기대 거래일(봉) 수. 단위 혼동을 막기 위한 명시 변환기."""
    return int(float(days) * HIST_TD_PER_CD)


def calendar_days_for_bars(bars) -> int:
    """요구 봉수 → 필요한 최소 달력일(올림). 마진은 호출부가 따로 얹는다."""
    return int(-(-float(bars) // HIST_TD_PER_CD))


def hist_days_for_bars(bars, pad_bars: int = 5) -> int:
    """요구 **봉수** → 실제로 요청할 조회 창(달력일). 마진·하한·상한 포함.

    `calendar_days_for_bars()` 와 역할이 다르다. 둘을 헷갈리면 안 된다:
      · calendar_days_for_bars — **순수 환산**. 마진도 하한도 없다.
        무마진 최소치를 계산해 마진의 존재를 검증하는 용도(diag T6).
      · hist_days_for_bars     — **호출부용**. 여유 봉수를 얹고 HIST_MIN_DAYS
        바닥과 HIST_MAX_DAYS 상한을 적용한 최종 값.

    호출부가 마진 정책을 몰라도 되게 하는 것이 요점이다. 호출부마다 직접
    `calendar_days_for_bars(n + 5)` 를 쓰면 0.6871 은 한 벌이어도 **정책이
    여러 벌**이 되고, 나중에 한쪽만 갱신된다. 그 실패는 로그를 남기지 않는다.

    [2026-08-28] run_drg_predict.py 의 limit=2 / limit=10 호출을 from/to 로
      옮기며 신설. 봉수를 달력일에 그대로 넣으면 연휴 한 번에 0봉이 온다.

    bars     : 하류가 실제로 소비하는 꼬리 깊이(iloc[-n] · rolling(n) 의 n).
    pad_bars : 그 위에 얹는 여유 봉수. 기본 5.
    """
    try:
        need = max(1, int(bars)) + max(0, int(pad_bars))
    except Exception:
        return HIST_MAX_DAYS      # 요구치를 못 읽었다 → 짧게 틀리느니 넓게 받는다
    return int(min(max(calendar_days_for_bars(need), HIST_MIN_DAYS),
                   HIST_MAX_DAYS))


def hist_range_params(calendar_days, today=None) -> str:
    """`&from=...&to=...` 쿼리 조각 — historical-price-eod 창 지정의 유일한 수단.

    인자 단위는 **달력일**이다. 봉 수로 생각하고 싶으면 `calendar_days_for_bars()`
    로 명시 변환할 것. `limit` 을 되살리는 대신 이 함수를 쓰라는 것이 요점이다.

    ⚠️ `to` 는 오늘이 아니라 **오늘+1일**이다. 상한 경계가 포함인지 배제인지,
       서버 타임존이 무엇인지에 최신 봉을 걸지 않기 위한 방어다. 하루를 더 줘도
       미래 봉은 존재하지 않으므로 결과는 달라지지 않지만, 만약 `to` 가
       배타적이었다면 전 종목이 하루 낡은 봉으로 평가됐을 것이다.
       룩백 기준점은 어디까지나 오늘이다(`from` = 오늘 − calendar_days).
    """
    d = today or _dt.datetime.now(_ET_TZ).date()
    _from = (d - _dt.timedelta(days=int(calendar_days))).strftime("%Y-%m-%d")
    _to = (d + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return f"&from={_from}&to={_to}"


def hist_days_for_holding(date_added, today=None, ticker: str = "",
                          base: int = None, warn=print) -> int:
    """보유 종목 1건에 필요한 조회 창(달력일).

    `regime_core.compute_position_drawdown` 의 보유 고점은 `close[index >= Date_Added]`
    의 최대값이다 — **상수가 아니라 보유 기간에 비례한다.** 창이 매수일에 못 미치면
    트레일링 스톱 기준 고점이 조용히 낮아져 **매도 신호가 덜 뜬다**(놓치는 방향이라
    더 위험하다). 고정 창으로는 절대 못 푸는 요구다.

    반환 규칙
      · Date_Added 없음      → 기본 창. 하류가 252봉 폴백을 쓰므로 깊은 조회 불필요.
      · Date_Added 파싱 실패 → **최대 창**. 값은 있는데 못 읽었다면 보유 기간이
                               미상이다. 짧은 창으로 조용히 틀리는 것보다 페이로드를
                               더 쓰는 편이 낫다(관측 가능한 비용).
      · 정상                 → min(기본 + 보유 달력일, 상한)

    [2026-08-28] run_watchlist_alerts._hist_days_for_holding 에서 승격했다.
      app.py 의 포트폴리오 경로는 고정 600봉(≈limit)을 쓰고 있어서, **같은 보유
      종목을 두고 메일과 화면의 트레일링 고점이 갈렸다.** 자동화만 고쳐두면
      화면이 조용히 틀린다 — 두 소비자가 같은 함수를 부르게 한다.

    warn: 경고 출력 훅. 자동화는 print(기본), 앱은 로그로 흘려도 무해하다.
    """
    _base = HIST_WINDOW_DAYS if base is None else int(base)
    try:
        s = str(date_added or "").strip()[:10]
        if not s:
            return _base
        d = pd.to_datetime(s, errors="coerce")
        if pd.isna(d):
            if warn:
                warn(f"[WARN] {ticker or '?'} Date_Added 파싱 실패({s!r}) — 최대 창 적용")
            return HIST_MAX_DAYS
        t = pd.to_datetime(str(today or "")[:10], errors="coerce")
        if pd.isna(t):
            t = pd.Timestamp(_dt.datetime.now(_ET_TZ).date())
        held = int((t - d).days)
        if held <= 0:
            return _base          # 미래 날짜/당일 매수 → 깊은 조회 불필요
        want = _base + held
        if want > HIST_MAX_DAYS:
            if warn:
                warn(f"[WARN] {ticker or '?'} 보유 {held}일 — 조회 창 상한 "
                     f"{HIST_MAX_DAYS}일 적용(보유 고점 절삭 가능)")
            return HIST_MAX_DAYS
        return want
    except Exception:
        return HIST_MAX_DAYS


def hist_days_for_target_date(target, today=None, base: int = None,
                              pad: int = 10) -> int:
    """특정 과거 날짜(target)를 반드시 포함하는 조회 창(달력일).

    배당 재투자 체결가처럼 **요구가 '봉 수'가 아니라 '이 날짜가 창 안에 있을 것'**
    인 경우에 쓴다. 고정 창을 쓰면 오래된 미기록 배당이 조용히 "가격 이력 조회
    실패"로 떨어지고, 실패는 기록되지 않으므로 **다음 접속에도 똑같이 실패한다**
    — 영구 미기록이 된다.

    pad: target 이후 첫 거래일을 찾아야 하므로 하한이 아니라 상한 쪽 여유가 아니라,
         연휴로 target 직후 거래일이 밀리는 경우를 위한 며칠의 여유다.
    """
    _base = HIST_WINDOW_DAYS if base is None else int(base)
    try:
        ts = pd.to_datetime(str(target or "")[:10], errors="coerce")
        if pd.isna(ts):
            return _base
        t = pd.to_datetime(str(today or "")[:10], errors="coerce")
        if pd.isna(t):
            t = pd.Timestamp(_dt.datetime.now(_ET_TZ).date())
        back = int((t - ts).days)
        if back <= 0:
            return _base          # 미래 지급일 → 어차피 데이터 없음
        return int(min(max(_base, back + int(pad)), HIST_MAX_DAYS))
    except Exception:
        return HIST_MAX_DAYS


# st.secrets 우선 키 제공자를 fmp_http 에 설치한다(자동화에서는 _key 가 환경변수로 폴백).
_fh.set_key_provider(_key)


def _get_json(path: str):
    """공통 GET → JSON. 실패 시 None. (path 예: 'profile?symbol=AAPL')

    2026-08-13: 맨 requests.get → fmp_http.fmp_get_json 으로 전환.
      이전에는 이 경로만 레이트리밋/재시도를 건너뛰어, 429 를 만나면 재시도 없이
      None 을 돌려줬다. 호출부는 그걸 '데이터 없음'으로 처리하므로 **조용히 틀린
      값**이 나왔다. 반환 계약(비200 → None)은 동일하다.
    """
    return _fh.fmp_get_json(path)


def _f(v, default=np.nan) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════
# 3단계 — 밸류에이션 / 정밀 검사 보강
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=3600, show_spinner=False)
def fmp_dcf(ticker: str) -> dict:
    """DCF 적정주가 vs 현재가 괴리율.
    반환: {"dcf": float, "price": float, "gap_pct": float, "verdict": str} 또는 {}."""
    data = _get_json(f"discounted-cash-flow?symbol={ticker}")
    if not data:
        return {}
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return {}
    dcf = _f(row.get("dcf"))
    price = _f(row.get("Stock Price", row.get("stockPrice", row.get("price"))))
    if np.isnan(dcf) or np.isnan(price) or price <= 0:
        return {}
    gap = (dcf / price - 1) * 100
    if gap >= 15:
        verdict = "🟢 저평가 (DCF가 현재가보다 높음)"
    elif gap <= -15:
        verdict = "🔴 고평가 (DCF가 현재가보다 낮음)"
    else:
        verdict = "🟡 적정 범위"
    return {"dcf": round(dcf, 2), "price": round(price, 2),
            "gap_pct": round(gap, 1), "verdict": verdict}


@st.cache_data(ttl=3600, show_spinner=False)
def fmp_stock_peers(ticker: str) -> list[str]:
    """동종업계 peer 티커 목록."""
    data = _get_json(f"stock-peers?symbol={ticker}")
    if not data:
        return []
    out = []
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if isinstance(row, dict):
            sym = row.get("symbol") or row.get("peersList")
            if isinstance(sym, list):
                out.extend(sym)
            elif sym:
                out.append(sym)
        elif isinstance(row, str):
            out.append(row)
    # 자기 자신 제외, 중복 제거
    seen, clean = set(), []
    for s in out:
        s = str(s).upper().strip()
        if s and s != str(ticker).upper() and s not in seen:
            seen.add(s)
            clean.append(s)
    return clean[:10]


@st.cache_data(ttl=3600, show_spinner=False)
def fmp_revenue_product_segmentation(ticker: str) -> dict:
    """가장 최근 기간의 제품별 매출 비중. 반환: {제품명: 비중%}."""
    data = _get_json(f"revenue-product-segmentation?symbol={ticker}")
    return _segmentation_latest(data)


@st.cache_data(ttl=3600, show_spinner=False)
def fmp_revenue_geographic_segmentation(ticker: str) -> dict:
    """가장 최근 기간의 지역별 매출 비중. 반환: {지역명: 비중%}."""
    data = _get_json(f"revenue-geographic-segmentation?symbol={ticker}")
    return _segmentation_latest(data)


def _segmentation_latest(data) -> dict:
    """FMP segmentation 응답에서 최신 기간의 {세그먼트: 비중%} 추출."""
    if not isinstance(data, list) or not data:
        return {}
    try:
        # 최신 기간(보통 첫 항목). data[*] = {"date":..., "data":{seg: amount}}
        latest = data[0]
        seg = latest.get("data", latest) if isinstance(latest, dict) else {}
        nums = {k: _f(v) for k, v in seg.items() if not np.isnan(_f(v))}
        total = sum(v for v in nums.values() if v > 0)
        if total <= 0:
            return {}
        return {k: round(v / total * 100, 1) for k, v in nums.items()}
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════
# 4단계 — 포트폴리오 매도 레이더 보강
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=3600, show_spinner=False)
def fmp_price_target_summary(ticker: str) -> dict:
    """애널리스트 목표가 요약(평균/최저/최고). price-target-consensus보다 분포가 상세."""
    data = _get_json(f"price-target-summary?symbol={ticker}")
    if not data:
        return {}
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return {}
    out = {}
    for k_out, keys in {
        "avg": ("lastMonthAvgPriceTarget", "allTimeAvgPriceTarget", "lastQuarterAvgPriceTarget"),
        "high": ("allTimeHighPriceTarget", "lastMonthHighPriceTarget"),
        "low": ("allTimeLowPriceTarget", "lastMonthLowPriceTarget"),
        "count": ("allTimeCount", "lastMonthCount"),
    }.items():
        for kk in keys:
            v = _f(row.get(kk))
            if not np.isnan(v):
                out[k_out] = round(v, 2)
                break
    return out


def _iso_date_or_blank(v) -> str:
    """'YYYY-MM-DD' 로 파싱 가능하면 그대로, 아니면 빈 문자열."""
    s = str(v or "")[:10]
    if len(s) != 10:
        return ""
    try:
        _dt.date.fromisoformat(s)
        return s
    except Exception:
        return ""


@st.cache_data(ttl=21600, show_spinner=False)
def fmp_dividend_history(ticker: str) -> list[dict]:
    """배당 이력(과거 + 발표된 예정분) 전체 — **ex_date 오름차순**.

    반환: [{"ex_date","record_date","pay_date","declaration_date",
            "amount","frequency"}]  · 실패 시 [].

    ⚠️ 배당 수령 자격은 **ex_date(배당락일) 개장 시점 보유** 기준이다.
       즉 체결일이 ex_date **미만**이어야 받는다(ex_date 당일 매수는 못 받음).
       실제 현금 입금·DRIP 체결은 pay_date(지급일) 기준이므로 두 날짜를 모두 돌려준다.

    ⚠️ ROC(원금환급) 구분은 FMP 가 제공하지 않는다. QQQI·JEPQ 같은 커버드콜 ETF는
       배당의 상당분이 ROC 라서 세무상 평단가를 '낮춰야' 하지만 여기서는 알 수 없다.
       → 재투자 결과는 어디까지나 **장부 근사치**다.
    """
    data = _get_json(f"dividends?symbol={ticker}")
    if not isinstance(data, list) or not data:
        return []
    out, seen = [], set()
    for row in data:
        if not isinstance(row, dict):
            continue
        ex = _iso_date_or_blank(row.get("date"))
        if not ex or ex in seen:
            continue
        amt = _f(row.get("dividend"))
        if np.isnan(amt) or amt <= 0:
            amt = _f(row.get("adjDividend"))
        if np.isnan(amt) or amt <= 0:
            continue
        seen.add(ex)
        out.append({
            "ex_date": ex,
            "record_date": _iso_date_or_blank(row.get("recordDate")),
            "pay_date": _iso_date_or_blank(row.get("paymentDate")),
            "declaration_date": _iso_date_or_blank(row.get("declarationDate")),
            "amount": round(float(amt), 6),
            "frequency": str(row.get("frequency") or "").strip(),
        })
    out.sort(key=lambda d: d["ex_date"])
    return out


@st.cache_data(ttl=21600, show_spinner=False)
def fmp_dividends(ticker: str) -> dict:
    """다가오는/최근 배당 1건. 반환: {"ex_date","amount","is_upcoming",...} 또는 {}.

    ⚠️ 기존 호출부 호환 유지 — 반환 키(ex_date/amount/is_upcoming)와 선택 규칙
       (미래 건이 있으면 가장 가까운 미래, 없으면 가장 최근 과거)은 그대로다.
       내부 파싱만 fmp_dividend_history(SSOT)로 위임했다.
    """
    rows = fmp_dividend_history(ticker)
    if not rows:
        return {}
    today = _dt.date.today()
    upcoming = None
    for r in rows:  # 오름차순 → 첫 미래 건이 가장 가까운 미래
        try:
            if _dt.date.fromisoformat(r["ex_date"]) >= today:
                upcoming = r
                break
        except Exception:
            continue
    target = upcoming or rows[-1]  # 미래가 없으면 가장 최근 과거
    return {
        "ex_date": target["ex_date"],
        "amount": target["amount"],
        "is_upcoming": upcoming is not None,
        "pay_date": target.get("pay_date", ""),
        "frequency": target.get("frequency", ""),
    }


@st.cache_data(ttl=21600, show_spinner=False)
def fmp_splits_calendar(days_ahead: int = 30) -> list[dict]:
    """향후 days_ahead일 이내 주식 분할 일정. 반환: [{symbol, date, ratio}]."""
    today = _dt.date.today()
    to = today + _dt.timedelta(days=days_ahead)
    data = _get_json(f"splits-calendar?from={today.isoformat()}&to={to.isoformat()}")
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        out.append({
            "symbol": str(row.get("symbol", "")).upper(),
            "date": str(row.get("date", ""))[:10],
            "ratio": f"{row.get('numerator', '?')}:{row.get('denominator', '?')}",
        })
    return out


@st.cache_data(ttl=21600, show_spinner=False)
def fmp_senate_trades(ticker: str) -> list[dict]:
    """특정 종목에 대한 미 상원의원 거래 내역(최근)."""
    return _congress_trades("senate-trades", ticker)


@st.cache_data(ttl=21600, show_spinner=False)
def fmp_house_trades(ticker: str) -> list[dict]:
    """특정 종목에 대한 미 하원의원 거래 내역(최근)."""
    return _congress_trades("house-trades", ticker)


def _congress_trades(endpoint: str, ticker: str) -> list[dict]:
    data = _get_json(f"{endpoint}?symbol={ticker}")
    if not isinstance(data, list):
        return []
    out = []
    for row in data[:20]:
        if not isinstance(row, dict):
            continue
        typ = str(row.get("type", row.get("transactionType", ""))).lower()
        action = "매도" if "sale" in typ or "sell" in typ else ("매수" if "purchase" in typ or "buy" in typ else typ)
        out.append({
            "name": row.get("firstName", "") and f"{row.get('firstName','')} {row.get('lastName','')}".strip()
                    or row.get("representative", row.get("office", "")),
            "action": action,
            "date": str(row.get("transactionDate", row.get("dateRecieved", "")))[:10],
            "amount": row.get("amount", ""),
        })
    return out


# ══════════════════════════════════════════════════════════════════════════
# 배치 호출 (레이트리밋 대응) — 포트폴리오/워치리스트 N종목을 1콜로
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=900, show_spinner=False)
def fmp_batch_quote_short(tickers: tuple) -> dict:
    """여러 종목 현재가/변동률/거래량을 한 번에. 반환: {ticker: {price, change_pct, volume}}.
    ⚠️ 캐시 키 안정성을 위해 tickers는 tuple로 전달하세요."""
    syms = ",".join(sorted({str(t).upper().strip() for t in tickers if t}))
    if not syms:
        return {}
    data = _get_json(f"batch-quote-short?symbols={syms}")
    if not isinstance(data, list):
        return {}
    out = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        out[sym] = {
            "price": _f(row.get("price")),
            "change_pct": _f(row.get("changesPercentage", row.get("change"))),
            "volume": _f(row.get("volume")),
        }
    return out


@st.cache_data(ttl=900, show_spinner=False)
def fmp_batch_etf_quotes(tickers: tuple) -> dict:
    """여러 ETF 현재가/변동률을 한 번에. 반환: {ticker: {price, change_pct}}."""
    syms = ",".join(sorted({str(t).upper().strip() for t in tickers if t}))
    if not syms:
        return {}
    data = _get_json(f"batch-etf-quotes?symbols={syms}")
    if not isinstance(data, list):
        # 일부 플랜은 symbols 파라미터 미지원 → batch-quote-short로 폴백
        return fmp_batch_quote_short(tickers)
    out = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper()
        if sym:
            out[sym] = {"price": _f(row.get("price")),
                        "change_pct": _f(row.get("changesPercentage", row.get("change")))}
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def fmp_batch_market_cap(tickers: tuple) -> dict:
    """여러 종목 시가총액을 한 번에. 반환: {ticker: market_cap(float)}."""
    syms = ",".join(sorted({str(t).upper().strip() for t in tickers if t}))
    if not syms:
        return {}
    data = _get_json(f"market-capitalization-batch?symbols={syms}")
    if not isinstance(data, list):
        return {}
    out = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper()
        mc = _f(row.get("marketCap", row.get("marketCapitalization")))
        if sym and not np.isnan(mc):
            out[sym] = mc
    return out


# ══════════════════════════════════════════════════════════════════════════
# 2단계 — ETF 분석 (stable 엔드포인트로 교체/보강)
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=86400, show_spinner=False)
def fmp_etf_holdings(symbol: str) -> pd.DataFrame:
    """ETF 보유 종목(stable: etf/holdings). legacy etf-holder/ 대체.
    반환: columns=[asset, name, weight_pct]."""
    data = _get_json(f"etf/holdings?symbol={symbol}")
    if not isinstance(data, list) or not data:
        return pd.DataFrame()
    rows = []
    for row in data:
        if not isinstance(row, dict):
            continue
        w = _f(row.get("weightPercentage", row.get("weight")))
        if 0 < w <= 1:  # 비율(0~1)이면 %로
            w *= 100
        rows.append({
            "asset": str(row.get("asset", row.get("symbol", ""))).upper(),
            "name": row.get("name", ""),
            "weight_pct": round(w, 3) if not np.isnan(w) else np.nan,
        })
    df = pd.DataFrame(rows)
    if not df.empty and "weight_pct" in df.columns:
        df = df.sort_values("weight_pct", ascending=False, na_position="last")
    return df


@st.cache_data(ttl=86400, show_spinner=False)
def fmp_etf_info(symbol: str) -> dict:
    """ETF 기초 정보: 운용보수(expense ratio), AUM, 보유종목 수 등."""
    data = _get_json(f"etf/info?symbol={symbol}")
    if not data:
        return {}
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return {}
    return {
        "name": row.get("name", ""),
        "expense_ratio": _f(row.get("expenseRatio")),
        "aum": _f(row.get("aum", row.get("assetsUnderManagement"))),
        "holdings_count": _f(row.get("holdingsCount", row.get("numberOfHoldings"))),
        "domicile": row.get("domicile", ""),
    }


@st.cache_data(ttl=86400, show_spinner=False)
def fmp_etf_sector_weighting(symbol: str) -> dict:
    """ETF 섹터 비중. 반환: {섹터: 비중%}."""
    data = _get_json(f"etf/sector-weightings?symbol={symbol}")
    if not isinstance(data, list):
        return {}
    out = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sec = row.get("sector", row.get("industry"))
        w = _f(row.get("weightPercentage", row.get("weight")))
        if 0 < w <= 1:
            w *= 100
        if sec and not np.isnan(w):
            out[str(sec)] = round(w, 2)
    return out


# ══════════════════════════════════════════════════════════════════════════
# Emerging 종목 추적기 보강 — 시장 모멘텀 발굴
# ══════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=1800, show_spinner=False)
def fmp_biggest_gainers(limit: int = 20) -> list[dict]:
    """장중 급등 상위 종목."""
    return _movers("biggest-gainers", limit)


@st.cache_data(ttl=1800, show_spinner=False)
def fmp_biggest_losers(limit: int = 20) -> list[dict]:
    """장중 급락 상위 종목."""
    return _movers("biggest-losers", limit)


def _movers(endpoint: str, limit: int) -> list[dict]:
    data = _get_json(endpoint)
    if not isinstance(data, list):
        return []
    out = []
    for row in data[:limit]:
        if not isinstance(row, dict):
            continue
        out.append({
            "symbol": str(row.get("symbol", "")).upper(),
            "name": row.get("name", ""),
            "change_pct": _f(row.get("changesPercentage", row.get("change"))),
            "price": _f(row.get("price")),
        })
    return out


# ══════════════════════════════════════════════════════════════════════════
# DRG 신호 7·8 공용 데이터 — sector-performance-snapshot 1콜로 두 신호 산출
#   app.py(compute_daily_risk_gauge)와 run_drg_predict.py가 동일 로직을 쓰도록
#   계산부를 여기로 분리. (run_drg_predict.py는 streamlit 미사용이라 inline 복제)
# ══════════════════════════════════════════════════════════════════════════
_DEFENSIVE_SECTORS = {"Utilities", "Consumer Staples", "Consumer Defensive",
                      "Healthcare", "Health Care", "Real Estate"}
_CYCLICAL_SECTORS = {"Technology", "Consumer Cyclical", "Consumer Discretionary",
                     "Industrials", "Financial Services", "Financials",
                     "Energy", "Basic Materials", "Materials",
                     "Communication Services"}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sector_snapshot() -> list:
    """가장 최근 거래일의 섹터 퍼포먼스 스냅샷(원본 리스트)."""
    today = _dt.datetime.now(_ET_TZ).date()
    for back in range(0, 6):
        d = today - _dt.timedelta(days=back)
        if d.weekday() >= 5:  # 토/일 건너뜀
            continue
        data = _get_json(f"sector-performance-snapshot?date={d.isoformat()}")
        if isinstance(data, list) and data:
            return data
    return []


def compute_sector_signals(snapshot: list) -> dict:
    """스냅샷 → 방어/경기민감 로테이션 + 시장 폭(breadth) 지표.
    app.py와 run_drg_predict.py 양쪽에서 공통 호출 가능(streamlit 비의존)."""
    if not isinstance(snapshot, list) or not snapshot:
        return {}
    vals = {}
    for row in snapshot:
        if not isinstance(row, dict):
            continue
        sec = row.get("sector")
        chg = row.get("averageChange", row.get("changesPercentage", row.get("change")))
        try:
            chg = float(chg)
        except (TypeError, ValueError):
            continue
        if sec:
            vals[str(sec)] = chg
    if not vals:
        return {}
    defs = [v for s, v in vals.items() if s in _DEFENSIVE_SECTORS]
    cycs = [v for s, v in vals.items() if s in _CYCLICAL_SECTORS]
    neg_frac = sum(1 for v in vals.values() if v < 0) / len(vals)
    def_avg = float(np.mean(defs)) if defs else float("nan")
    cyc_avg = float(np.mean(cycs)) if cycs else float("nan")
    rotation = (def_avg - cyc_avg) if (defs and cycs) else float("nan")
    return {
        "vals": vals,
        "neg_frac": neg_frac,
        "def_avg": def_avg,
        "cyc_avg": cyc_avg,
        "rotation": rotation,  # 양수 = 방어주가 경기민감주보다 강함 = 리스크오프 경향
    }


# ══════════════════════════════════════════════════════════════════════════
# 🛰️ 위성 섹터 Top10 — SSOT (app.py 탭 표시 + run_hidden_alpha 주간 이메일 공유)
# ══════════════════════════════════════════════════════════════════════════

SECTOR_THEME_ETFS = {
    "XLK":  [("SOXX", "반도체"), ("IGV", "소프트웨어"), ("CIBR", "사이버보안"), ("SKYY", "클라우드"), ("BOTZ", "AI·로봇")],
    "XLV":  [("XBI", "바이오테크"), ("IHI", "의료기기"), ("IHF", "의료서비스"), ("PPH", "제약"), ("GNOM", "유전체")],
    "XLE":  [("XOP", "탐사·생산"), ("OIH", "오일서비스"), ("URA", "우라늄/원자력"), ("TAN", "태양광"), ("AMLP", "미드스트림")],
    "XLI":  [("ITA", "방산·항공"), ("JETS", "항공"), ("IYT", "운송"), ("PAVE", "인프라"), ("UFO", "우주")],
    "XLF":  [("KRE", "지방은행"), ("KBE", "은행"), ("KIE", "보험"), ("IAI", "증권·브로커"), ("FINX", "핀테크")],
    "XLY":  [("XRT", "소매"), ("ITB", "주택건설"), ("IBUY", "온라인소매"), ("PEJ", "레저·여행"), ("BETZ", "게이밍·베팅")],
    "XLB":  [("GDX", "금광"), ("COPX", "구리광"), ("LIT", "리튬·배터리"), ("SLX", "철강"), ("WOOD", "목재")],
    "XLRE": [("VNQ", "리츠 광범위"), ("REZ", "주거 리츠"), ("SRVR", "데이터센터 리츠"), ("INDS", "산업 리츠"), ("REM", "모기지 리츠")],
    "XLC":  [("FDN", "인터넷"), ("SOCL", "소셜미디어"), ("ESPO", "게임·e스포츠")],
    "XLU":  [("GRID", "스마트그리드"), ("NLR", "원자력")],
    "XLP":  [],
}

# 라이브(FMP /etf/holdings)가 비어 있을 때 사용하는 대표 보유종목 폴백.
# (요금제에 ETF Holdings 엔드포인트가 없을 때를 대비 — 라이브가 오면 라이브 우선)
ETF_CONSTITUENTS = {
    # GICS 11개 대형 섹터
    "XLK": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "ADBE", "CRM", "AMD", "CSCO", "INTU", "QCOM", "AMAT", "TXN", "NOW", "IBM"],
    "XLC": ["GOOGL", "META", "NFLX", "TMUS", "DIS", "VZ", "T", "CHTR", "CMCSA", "EA", "TTWO", "FOXA", "WBD", "DASH", "SPOT"],
    "XLY": ["AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "NKE", "SBUX", "TJX", "CMG", "RCL", "MAR", "GM", "F", "ORLY"],
    "XLP": ["COST", "WMT", "PG", "KO", "PEP", "PM", "MO", "MDLZ", "CL", "TGT", "KMB", "GIS", "KVUE", "KHC", "STZ"],
    "XLV": ["LLY", "UNH", "JNJ", "MRK", "ABBV", "PFE", "TMO", "DHR", "AMGN", "GILD", "BMY", "ISRG", "VRTX", "SYK", "CVS"],
    "XLF": ["JPM", "BRK-B", "V", "MA", "BAC", "WFC", "GS", "MS", "SCHW", "BLK", "AXP", "C", "PGR", "AIG", "USB"],
    "XLE": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "KMI", "HAL", "BKR", "DVN", "FANG", "WMB"],
    "XLI": ["GE", "CAT", "RTX", "HON", "UNP", "LMT", "DE", "ETN", "BA", "NOC", "UPS", "FDX", "WM", "EMR", "ITW"],
    "XLB": ["LIN", "APD", "SHW", "ECL", "NUE", "FCX", "DOW", "DD", "CTVA", "NEM", "MLM", "VMC", "PPG", "LYB", "MOS"],
    "XLRE": ["AMT", "PLD", "EQIX", "SPG", "O", "WELL", "PSA", "DLR", "CCI", "CBRE", "VICI", "AVB", "EQR", "ESS", "EXR"],
    "XLU": ["NEE", "SO", "DUK", "CEG", "AEP", "EXC", "SRE", "XEL", "D", "PCG", "PEG", "ED", "EIX", "WEC", "ETR"],
    # 테마/세부산업 ETF
    "SOXX": ["NVDA", "AVGO", "AMD", "MU", "TXN", "AMAT", "QCOM", "INTC", "LRCX", "KLAC", "ADI", "MCHP", "MRVL", "NXPI", "ON"],
    "IGV": ["MSFT", "ORCL", "CRM", "ADBE", "NOW", "PLTR", "PANW", "CRWD", "SNOW", "INTU", "FTNT", "DDOG", "WDAY", "TEAM", "ANSS"],
    "CIBR": ["CRWD", "PANW", "FTNT", "ZS", "CSCO", "GEN", "CYBR", "OKTA", "CHKP", "NET", "TENB", "S", "QLYS", "RPD", "AKAM"],
    "SKYY": ["ORCL", "MSFT", "GOOGL", "AMZN", "NOW", "CRM", "SNOW", "NET", "DDOG", "MDB", "ANET", "IBM", "ZS", "AKAM", "FTNT"],
    "BOTZ": ["NVDA", "ABB", "ISRG", "PATH", "SYM", "ROK", "FANUY", "TER", "OMCL", "CGNX", "IRBT", "UI", "NARI", "MDT", "KEYS"],
    "XBI": ["GILD", "BIIB", "REGN", "VRTX", "ALNY", "MRNA", "BNTX", "AMGN", "ILMN", "SRPT", "CRSP", "EXEL", "NBIX", "INCY", "ARGX"],
    "IHI": ["ISRG", "ABT", "BSX", "MDT", "SYK", "BDX", "EW", "DXCM", "ZBH", "RMD", "STE", "GEHC", "PODD", "BAX", "COO"],
    "IHF": ["UNH", "ELV", "CI", "HCA", "CVS", "CNC", "HUM", "MCK", "COR", "DVA", "MOH", "EHC", "UHS", "THC", "ENSG"],
    "PPH": ["LLY", "JNJ", "ABBV", "MRK", "NVS", "NVO", "AZN", "PFE", "BMY", "ZTS", "GSK", "AMGN", "TAK", "HLN", "VTRS"],
    "GNOM": ["CRSP", "NTLA", "BEAM", "TWST", "EXAS", "ARWR", "IONS", "PACB", "EDIT", "FATE", "RXRX", "DNA", "VCYT", "SDGR", "NVCR"],
    "XOP": ["COP", "EOG", "FANG", "DVN", "OXY", "HES", "MPC", "VLO", "PSX", "APA", "CTRA", "OVV", "EQT", "AR", "MUR"],
    "OIH": ["SLB", "HAL", "BKR", "TS", "FTI", "NOV", "CHX", "WFRD", "RIG", "LBRT", "HP", "NBR", "OII", "PTEN", "VAL"],
    "URA": ["CCJ", "BWXT", "NXE", "UEC", "DNN", "LEU", "UUUU", "SMR", "OKLO", "LTBR", "URG", "EU", "UROY"],
    "TAN": ["FSLR", "ENPH", "NXT", "SEDG", "RUN", "SHLS", "ARRY", "MAXN", "CSIQ", "FLNC", "NOVA", "DQ", "JKS", "SPWR", "CSLR"],
    "AMLP": ["MPLX", "ET", "EPD", "PAA", "WES", "ENLC", "HESM", "DTM", "SUN", "NS", "CQP", "DMLP", "GLP"],
    "ITA": ["RTX", "LMT", "NOC", "GD", "BA", "HII", "TXT", "TDG", "HEI", "KTOS", "AVAV", "LDOS", "LHX", "CW", "MRCY"],
    "JETS": ["DAL", "UAL", "LUV", "AAL", "ALK", "BA", "RYAAY", "ALGT", "SKYW", "HA", "GD", "CPA", "JBLU", "MESA", "HXL"],
    "IYT": ["UBER", "UPS", "UNP", "FDX", "CSX", "NSC", "ODFL", "JBHT", "CHRW", "EXPD", "R", "KNX", "LSTR", "WERN", "SAIA"],
    "PAVE": ["PWR", "ETN", "URI", "NUE", "EMR", "JCI", "FAST", "DE", "CARR", "MLM", "VMC", "HUBB", "AME", "TT", "WAB"],
    "UFO": ["PLTR", "RKLB", "LHX", "RTX", "NOC", "BA", "IRDM", "ASTS", "SPIR", "SATL", "VSAT", "GSAT", "TDY", "HEI", "CACI"],
    "KRE": ["TFC", "USB", "FITB", "RF", "HBAN", "MTB", "KEY", "CFG", "FCNCA", "ZION", "CMA", "WAL", "WBS", "EWBC", "SNV"],
    "KBE": ["COF", "GS", "MS", "BK", "STT", "JPM", "BAC", "WFC", "C", "USB", "PNC", "TFC", "FITB", "RF", "HBAN"],
    "KIE": ["PGR", "ALL", "MET", "PRU", "TRV", "AIG", "HIG", "CB", "AFL", "CINF", "L", "GL", "AIZ", "ACGL", "RGA"],
    "IAI": ["GS", "MS", "SCHW", "ICE", "CME", "SPGI", "MCO", "COIN", "IBKR", "MKTX", "NDAQ", "CBOE", "RJF", "LPLA", "TROW"],
    "FINX": ["PYPL", "COIN", "FI", "GPN", "AFRM", "SOFI", "NU", "FIS", "MELI", "TOST", "BILL", "HOOD", "FOUR", "FLYW", "MQ"],
    "XRT": ["ANF", "GAP", "W", "RH", "CVNA", "BBY", "DKS", "ULTA", "ROST", "TJX", "BURL", "FL", "KSS", "M", "GME"],
    "ITB": ["DHI", "LEN", "PHM", "NVR", "TOL", "KBH", "TPH", "MTH", "BLDR", "MAS", "SHW", "LOW", "HD", "MHK", "FBIN"],
    "IBUY": ["CHWY", "CVNA", "ETSY", "W", "AMZN", "EBAY", "MELI", "SE", "SHOP", "DASH", "ABNB", "EXPE", "PINS", "RVLV", "WSM"],
    "PEJ": ["BKNG", "MAR", "HLT", "RCL", "CCL", "NCLH", "LVS", "WYNN", "MGM", "DAL", "UAL", "CMG", "SBUX", "YUM", "DPZ"],
    "BETZ": ["DKNG", "FLUT", "PENN", "CZR", "MGM", "LVS", "WYNN", "BYD", "RSI", "GENI", "SGHC", "LNW", "GDEN", "CHDN", "ACEL"],
    "GDX": ["NEM", "AEM", "GOLD", "WPM", "FNV", "KGC", "GFI", "AU", "RGLD", "PAAS", "BVN", "HMY", "SSRM", "EGO", "OR"],
    "COPX": ["FCX", "SCCO", "TECK", "ERO", "HBM", "IVN", "FM", "LUN", "TGB", "CMMC", "AGI", "WRN", "CS", "ANTO", "GLEN"],
    "LIT": ["ALB", "SQM", "TSLA", "BYDDY", "PCRFY", "LAC", "PLL", "SLI", "MP", "FREY", "ENVX", "AMPX", "QS", "SES", "LICY"],
    "SLX": ["VALE", "NUE", "RIO", "STLD", "RS", "TX", "CLF", "MT", "GGB", "X", "CMC", "ATI", "WOR", "TMST", "SID"],
    "WOOD": ["WY", "PCH", "RYN", "WFG", "IP", "PKG", "SW", "SON", "LPX", "UFPI", "BCC", "OSB", "MERC", "SLVM", "DTC"],
    "VNQ": ["PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "DLR", "CCI", "CBRE", "EXR", "AVB", "VICI", "IRM", "EQR"],
    "REZ": ["WELL", "AVB", "EQR", "INVH", "VTR", "ESS", "MAA", "UDR", "AMH", "ELS", "SUI", "CPT", "DOC", "NNN", "STAG"],
    "SRVR": ["EQIX", "DLR", "AMT", "CCI", "SBAC", "IRM", "UNIT", "DBRG", "GLPI", "LAMR", "FYBR", "T", "VZ", "CSGP", "WY"],
    "INDS": ["PLD", "EXR", "PSA", "CUBE", "EGP", "FR", "REXR", "STAG", "TRNO", "NSA", "ILPT", "PLYM", "LXP", "GTY", "COLD"],
    "REM": ["AGNC", "NLY", "STWD", "RITM", "BXMT", "ABR", "CIM", "TWO", "RC", "ARI", "PMT", "NYMT", "DX", "EFC", "MFA"],
    "FDN": ["AMZN", "META", "GOOGL", "NFLX", "CRM", "UBER", "ABNB", "PYPL", "SHOP", "SNOW", "COIN", "DASH", "PINS", "SPOT", "Z"],
    "SOCL": ["META", "GOOGL", "PINS", "SNAP", "RDDT", "MTCH", "BIDU", "YELP", "BMBL", "NTES", "Z", "CARG", "DJT", "CARS", "TTGT"],
    "ESPO": ["NVDA", "NTES", "EA", "RBLX", "TTWO", "SE", "BILI", "U", "AMD", "LOGI", "CRSR", "PLTK", "SCPL", "GRVY", "SLGG"],
    "GRID": ["ABB", "ETN", "GEV", "PWR", "AME", "HUBB", "JCI", "EMR", "SU", "APH", "GLW", "ENPH", "BMI", "ITRI", "POWL"],
    "NLR": ["CEG", "CCJ", "BWXT", "PEG", "DUK", "SO", "EXC", "PWR", "GEV", "OKLO", "SMR", "NRG", "VST", "TLN", "LEU"],
}


GICS_SECTOR_LABELS = {
    "XLK": "기술", "XLC": "통신", "XLY": "자유소비재", "XLP": "필수소비재",
    "XLV": "헬스케어", "XLF": "금융", "XLE": "에너지", "XLI": "산업재",
    "XLB": "소재", "XLRE": "부동산", "XLU": "유틸리티",
}

# 위성 후보 풀: GICS 섹터 대표 ETF + 각 섹터의 테마 ETF (레버리지·인버스 없음)
def satellite_candidate_pool() -> dict:
    """{GICS 섹터티커: [후보 ETF들(섹터 대표 포함)]}"""
    pool = {}
    for sec in GICS_SECTOR_LABELS:
        pool[sec] = [sec] + [t for t, _ in SECTOR_THEME_ETFS.get(sec, [])]
    return pool


def _closes(ticker: str, limit: int = 170) -> pd.Series:
    """일별 종가 시리즈(오름차순·중복 날짜 제거). 일시 실패 2회 재시도."""
    for attempt in range(3):
        data = _get_json(f"historical-price-eod/full?symbol={ticker}&limit={limit}")
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if isinstance(rows, list) and rows:
            df = pd.DataFrame(rows)
            if "date" in df.columns and "close" in df.columns:
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                s = (pd.Series(pd.to_numeric(df["close"], errors="coerce").values, index=df["date"])
                     .dropna().sort_index())
                return s[~s.index.duplicated(keep="last")]
            return pd.Series(dtype=float)  # 형식 이상 = 확정 실패
        if data is not None:
            return pd.Series(dtype=float)  # 200·빈 응답 = 데이터 없음(재시도 무의미)
        _time.sleep(1.5 * (attempt + 1))   # None = 네트워크/429 → 재시도
    return pd.Series(dtype=float)


def _trailing_return(s: pd.Series, bars: int):
    if len(s) <= bars:
        return np.nan
    try:
        return float((s.iloc[-1] / s.iloc[-1 - bars] - 1) * 100)
    except Exception:
        return np.nan


def _top_holdings_set(ticker: str, fallback_map: dict, top_n: int = 15) -> set:
    """중복 계산용 구성종목 집합 — 라이브(/etf/holdings) 우선, 폴백 맵 차선."""
    try:
        live = fmp_etf_holdings(ticker)
        if live is not None and not live.empty and "symbol" in live.columns:
            syms = [str(x).strip().upper() for x in live["symbol"].head(top_n) if str(x).strip()]
            syms = [x for x in syms if "." not in x]
            if len(syms) >= 3:
                return set(syms)
    except Exception:
        pass
    fb = [str(x).strip().upper() for x in (fallback_map or {}).get(ticker, []) if "." not in str(x)]
    return set(fb[:top_n])


def compute_satellite_top10(top_n: int = 10, overlap_floor: float = 10.0,
                            pause_sec: float = 0.12) -> dict:
    """🛰️ 위성 섹터 Top10 — 월간 리밸런싱 후보 리스트 (SSOT).

    점수 = 1M×0.40 + 3M×0.40 + 6M×0.20  (1주 수익률은 노이즈 — 점수 제외, 표시만)
    GICS 섹터당 최고점 후보 1개만 → 상위 top_n. 같은 섹터 중복이 구조적으로 차단된다.

    시장 필터: SPY 종가 vs 200일선 — 아래면 '위성 신규 중단·축소' 수동 룰 발동 신호.
    후보 간 중복: 구성종목 상위 15개 집합의 교집합 비율(작은 쪽 기준 %),
                 overlap_floor 미만은 생략(전체는 matrix 에 보존).

    반환 dict:
      as_of, market_filter {spy, ma200, risk_on} | None,
      rows [{rank, ticker, sector, sector_label, theme_label, score, r1w, r1m, r3m, r6m,
             overlaps [(other, pct)]}],
      matrix {"A|B": pct}(전체 쌍), skipped [(ticker, 이유)]
    """
    out = {"as_of": _dt.datetime.now(_ET_TZ).strftime("%Y-%m-%d %H:%M ET"),
           "market_filter": None, "rows": [], "matrix": {}, "skipped": []}

    # ── 🚦 시장 필터 (SPY vs 200일선) ──
    spy = _closes("SPY", limit=260)
    if len(spy) >= 200:
        ma200 = float(spy.tail(200).mean())
        last = float(spy.iloc[-1])
        out["market_filter"] = {"spy": round(last, 2), "ma200": round(ma200, 2),
                                "risk_on": bool(last > ma200)}

    # ── 섹터별 챔피언 선발 ──
    theme_label_map = {t: lbl for lst in SECTOR_THEME_ETFS.values() for t, lbl in lst}
    champions = []
    for sec, cands in satellite_candidate_pool().items():
        best = None
        for tk in cands:
            s = _closes(tk, limit=170)
            _time.sleep(pause_sec)
            if len(s) < 127:  # 6M(126봉) 계산 불가
                out["skipped"].append((tk, f"히스토리 부족({len(s)}봉)"))
                continue
            r1w, r1m = _trailing_return(s, 5), _trailing_return(s, 21)
            r3m, r6m = _trailing_return(s, 63), _trailing_return(s, 126)
            if any(pd.isna(x) for x in (r1m, r3m, r6m)):
                out["skipped"].append((tk, "수익률 계산 실패"))
                continue
            score = 0.40 * r1m + 0.40 * r3m + 0.20 * r6m
            row = {"ticker": tk, "sector": sec,
                   "sector_label": GICS_SECTOR_LABELS.get(sec, sec),
                   "theme_label": ("섹터 대표" if tk == sec else theme_label_map.get(tk, "")),
                   "score": round(score, 2),
                   "r1w": round(r1w, 2) if pd.notna(r1w) else None,
                   "r1m": round(r1m, 2), "r3m": round(r3m, 2), "r6m": round(r6m, 2)}
            if best is None or row["score"] > best["score"]:
                best = row
        if best is not None:
            champions.append(best)

    champions.sort(key=lambda r: r["score"], reverse=True)
    rows = champions[:max(1, int(top_n))]
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # ── 후보 간 구성종목 중복 (전체 쌍 matrix + 행별 10%↑ 나열) ──
    hold = {r["ticker"]: _top_holdings_set(r["ticker"], ETF_CONSTITUENTS) for r in rows}
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            sa, sb = hold.get(a["ticker"], set()), hold.get(b["ticker"], set())
            pct = (len(sa & sb) / min(len(sa), len(sb)) * 100.0) if (sa and sb) else 0.0
            out["matrix"][f"{a['ticker']}|{b['ticker']}"] = round(pct, 1)
    for r in rows:
        partners = []
        for key, pct in out["matrix"].items():
            x, y = key.split("|")
            if r["ticker"] in (x, y) and pct >= overlap_floor:
                partners.append((y if x == r["ticker"] else x, pct))
        partners.sort(key=lambda p: -p[1])
        r["overlaps"] = partners

    out["rows"] = rows
    return out


def overlap_grade(pct: float) -> str:
    """중복 % → 색 등급 이모지 (🟢<25 · 🟡25~40 · 🔴40+)."""
    if pct >= 40:
        return "🔴"
    if pct >= 25:
        return "🟡"
    return "🟢"


# ═══════════════════════════════════════════════════════════════════════════════
# 레버리지/인버스 ETF 판별 SSOT — app.py · run_hidden_alpha.py 공용
# ═══════════════════════════════════════════════════════════════════════════════
# Hidden Alpha 로테이션 유니버스 필터 정책 (2026-07 확정):
#   - 인버스(음수 배수)  : 제외 확정 — 롱 모멘텀 로테이션과 구조적으로 상충
#   - 레버리지 롱(>1배)  : 제외가 디폴트 — 실전 운용 관행 코드화.
#                          Signal_Backtest 플래그 on/off 비교로 최종 판정 예정.
# 두 플래그는 app.py 랭킹과 run_hidden_alpha.py 이메일이 반드시 같은 값을 공유한다.
EXCLUDE_INVERSE: bool = True          # 인버스(-1x 포함) 로테이션 제외
EXCLUDE_LEVERAGED_LONG: bool = True   # 레버리지 롱(2x/3x/1.5x) 로테이션 제외

import re as _re

# 알려진 레버리지/인버스 ETF → 배수 매핑. 양수=레버리지 롱, 음수=인버스.
# 유니버스(미국 상장 ETF) 위주라 이 매핑이 이름 파싱보다 정확하다.
LEVERAGED_ETF_MAP = {
    # ── 3x 롱 ──
    "TQQQ": 3, "UPRO": 3, "SPXL": 3, "SOXL": 3, "TECL": 3, "FAS": 3,
    "TNA": 3, "LABU": 3, "UDOW": 3, "WEBL": 3, "FNGU": 3, "BULZ": 3,
    "NAIL": 3, "DPST": 3, "RETL": 3, "DFEN": 3, "CURE": 3, "DRN": 3,
    "GUSH": 3, "ERX": 3, "YINN": 3, "TPOR": 3, "UTSL": 3, "MIDU": 3,
    "URTY": 3, "TMF": 3, "DUST": 3, "JNUG": 3, "NUGT": 3, "USD": 3,
    # ── 2x 롱 ──
    "QLD": 2, "SSO": 2, "DDM": 2, "ROM": 2, "UWM": 2, "SAA": 2,
    "USD2X": 2, "NVDL": 2, "TSLL": 2, "AAPU": 2, "MSFU": 2, "GGLL": 2,
    "AMZU": 2, "FBL": 2, "NVDU": 2, "CONL": 2, "UYG": 2, "ROKT": 2,
    "BITX": 2, "BITU": 2, "ETHT": 2, "AGQ": 2, "UGL": 2, "BOIL": 2,
    "UCO": 2,
    # ── 1.5x ──
    "TSLR": 1.5,
    # ── 인버스 ──
    "SQQQ": -3, "SPXU": -3, "SOXS": -3, "TECS": -3, "FAZ": -3,
    "TZA": -3, "LABD": -3, "SDOW": -3, "WEBS": -3, "YANG": -3,
    "DRV": -3, "ERY": -3, "JDST": -3, "DGAZ": -3, "KOLD": -3, "SCO": -3,
    "SDS": -2, "QID": -2, "DXD": -2, "TWM": -2, "SKF": -2,
    "SH": -1, "PSQ": -1, "DOG": -1, "RWM": -1, "EUM": -1,
}

# 이름 문자열 보조 추정용 (매핑에 없을 때만): 배수 크기와 인버스 방향을 따로 판별.
_LEV_MAG_3X = _re.compile(r"\b3X\b|ULTRAPRO|TRIPLE", _re.I)
_LEV_MAG_2X = _re.compile(r"\b2X\b|\bULTRA\b|DOUBLE", _re.I)
_LEV_MAG_15 = _re.compile(r"\b1\.5X\b", _re.I)
_LEV_INVERSE = _re.compile(r"INVERSE|SHORT|BEAR", _re.I)
# 'SHORT'의 채권 만기 표현 오탐 방지 (Short-Term Treasury 등)
_LEV_SHORT_FALSE_POS = _re.compile(r"SHORT[\s\-]?(TERM|DURATION|MATURITY)", _re.I)


def get_leverage_multiplier(ticker: str, name: str = "") -> float | None:
    """티커(필요시 이름)로 레버리지 배수를 반환. 일반 ETF/주식이면 None.
    양수=레버리지 롱, 음수=인버스."""
    t = str(ticker).strip().upper()
    if t in LEVERAGED_ETF_MAP:
        return LEVERAGED_ETF_MAP[t]
    if name:
        n = str(name)
        if _LEV_MAG_3X.search(n):
            mag = 3.0
        elif _LEV_MAG_2X.search(n):
            mag = 2.0
        elif _LEV_MAG_15.search(n):
            mag = 1.5
        else:
            mag = None
        is_inverse = bool(_LEV_INVERSE.search(n)) and not _LEV_SHORT_FALSE_POS.search(n)
        if mag is not None:
            return -mag if is_inverse else mag
        if is_inverse:
            return -1.0  # 배수 명시 없는 단순 인버스(-1x)
    return None


def is_rotation_excluded(ticker: str, name: str = "") -> bool:
    """Hidden Alpha 로테이션 유니버스에서 제외 대상인지 (플래그 정책 적용)."""
    m = get_leverage_multiplier(ticker, name)
    if m is None:
        return False
    if m < 0:
        return EXCLUDE_INVERSE
    if m > 1:
        return EXCLUDE_LEVERAGED_LONG
    return False


def filter_rotation_universe(pairs: list) -> tuple[list, list]:
    """(ticker, name) 쌍 목록 → (유지 티커 목록, 제외 티커 목록).

    name 이 없는 항목은 빈 문자열로 취급 (티커 매핑만으로 판별).
    """
    kept, excluded = [], []
    for item in pairs:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            tk, nm = str(item[0]).strip().upper(), str(item[1] or "")
        else:
            tk, nm = str(item).strip().upper(), ""
        if not tk:
            continue
        (excluded if is_rotation_excluded(tk, nm) else kept).append(tk)
    return kept, excluded


# ══════════════════════════════════════════════════════════════════════════
# FMP 레이트 리미터 — 구현은 fmp_http.py(SSOT)로 이관
#
# 배경: 주간 3버킷 스캐너가 61초에 약 950콜을 쏴서 한도를 3배 초과했고,
#       그 직후 실행된 Hidden Alpha 는 324티커 전부 조회에 실패해 종료됐다.
#       확산주도 76종목 중 73종목이 가격 데이터를 못 받아 결과가 오염됐다.
#
# 2026-08-13 이관 이유: earnings_core 도 같은 리미터를 써야 하는데, 그 모듈은
#   "streamlit 을 import 하지 않는다"를 설계 불변식으로 갖는다(파일 상단 명시).
#   fmp_extras 는 streamlit 심(shim)을 갖고 있어 import 자체는 되지만, 불변식을
#   지키고 **카운터를 프로세스당 하나로** 유지하기 위해 무의존 모듈로 분리했다.
#   카운터를 모듈별로 두면 합산 호출량이 한도의 2~3배가 된다.
#
# 아래는 기존 호출부(scanner_core / run_hidden_alpha / run_scanner_scan / app.py)
# 호환을 위한 재노출이다. 구현은 fmp_http 단일 정의.
# ══════════════════════════════════════════════════════════════════════════
FMP_RATE_LIMIT_PER_MIN = _fh.FMP_RATE_LIMIT_PER_MIN
FMP_MAX_RETRIES        = _fh.FMP_MAX_RETRIES

fmp_rate_limit_acquire = _fh.fmp_rate_limit_acquire
fmp_get                = _fh.fmp_get
fmp_stats              = _fh.fmp_stats
fmp_reset_stats        = _fh.fmp_reset_stats
fmp_stats_line         = _fh.fmp_stats_line
