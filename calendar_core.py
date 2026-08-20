"""calendar_core.py — NYSE/NASDAQ 시장 캘린더 SSOT.

왜 만들었나
───────────
휴장일이 **5개 자동화 파일에 하드코딩 중복**돼 있었고, 전부 2026-12-25 에서 끝난다.

    run_watchlist_alerts.py:165   _NYSE_HOLIDAYS
    run_drg_predict.py:58/63/68   _NYSE_HOLIDAYS_2025 | _2026
    run_drg_verify.py:54/59/64    _NYSE_HOLIDAYS_2025 | _2026
    run_earnings_watch.py:85      _NYSE_HOLIDAYS
    run_narrative.py:57/62/64     _NYSE_FIXED_HOLIDAYS_2025 | _2026

2027-01-01 부터 다섯 곳 전부 **모든 휴장일을 거래일로 오판**한다. 신정에 알림
자동화가 돌고, DRG 가 존재하지 않는 종가를 검증하려 들고, 알림 상태머신의
확정 카운터가 헛돌아 진행된다(그 카운터를 진행시키지 않으려고 휴장일에 멈추는
설계인데 그 전제가 깨진다).

설계 — 왜 규칙 계산이 1차인가
─────────────────────────────
FMP `holidays-by-exchange` 는 실측 결과 정상이다(2027년 10건, `isClosed` +
`adjOpenTime`/`adjCloseTime` 포함). 그런데 **판정 경로에 네트워크나 시트를
끼워 넣으면 실행마다 비용이 붙는다.** 다섯 개 자동화 전부 개장 여부 가드가
**시트를 열기 전 최상단**에 있어서, 시트 조회로 바꾸면 "휴장일이라 즉시 종료"
하던 실행에까지 시트 왕복이 생긴다.

그래서 비용 순으로 판정한다.

    1) 주말이면              → 휴장       [FMP 0 · 시트 0]
    2) 규칙 계산 정규 휴일   → 휴장       [FMP 0 · 시트 0]   ← 연 10일 전부 여기
    3) 그 외                 → 개장       [FMP 0 · 시트 0]   ← 나머지 250일
    4) extra_closed 가 주어지면 3) 앞에서 한 번 더 본다 (임시 휴장 보강)

**핫 패스 비용이 기존 하드코딩 집합 조회와 동일하게 0 이다.** 로딩 시간에
영향을 주지 않는 것이 이 모듈의 설계 제약이었다.

NYSE 정규 휴일 10개는 전부 규칙으로 결정된다 — 굿프라이데이만 부활절 기반이라
계산이 필요하고(익명 그레고리력 알고리즘), 나머지는 고정일 또는 n번째 요일이다.
2025·2026 하드코딩 값 20개를 그대로 재현하는지 `diag_market_calendar.py` 가
검증한다.

그럼 FMP 는 왜 쓰나 — 규칙으로 잡을 수 없는 것
─────────────────────────────────────────────
**임시 휴장.** 대통령 국장일에 NYSE 는 하루 닫는다.

    2025-01-09  카터 전 대통령 국장일   ← 다섯 파일 어디에도 없다
    2018-12-05  부시 전 대통령 국장일

즉 **2025-01-09 에 이 시스템의 자동화는 전부 헛돌았다.** 규칙 계산으로도 못
잡는다. 이건 사람이 알려주거나 API 가 알려주는 수밖에 없다.

그래서 FMP 조회는 **주 1회 갱신 잡**(`refresh_market_calendar.py`)으로 분리했다.
갱신 잡이 하는 일:

    · FMP 에서 올해+내년 캘린더를 받아 `Market_Calendar` 시트에 저장
    · 규칙 계산 결과와 **대조** → 불일치를 로그로 경고
      (규칙에 없는 휴장 = 임시 휴장 후보. 이게 조기 발견 경로다)
    · 반일장 시각(`adjCloseTime`)도 함께 저장 — 이번 차수에서는 **저장만** 하고
      판정에는 쓰지 않는다. 2PM 워크플로 가드는 별건으로 분리했다.

의존성
──────
`reminders_core.py` 패턴을 따른다 — **모듈 레벨에서 gspread 를 import 하지
않는다.** 순수 로직만 있어서 오프라인 테스트가 되고, app.py 가 나중에 흡수해도
임포트 비용이 붙지 않는다. requests 도 함수 안에서 지연 import 한다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

try:
    import pytz
    _ET = pytz.timezone("US/Eastern")
except Exception:                                    # pragma: no cover
    _ET = None

# SSOT 버전 스탬프 — 소비자가 기동 시 확인한다.
CALENDAR_CORE_VERSION = "1.0.0"

CAL_SHEET = "Market_Calendar"
CAL_COLS = ["Date", "Exchange", "Name", "Is_Closed",
            "Adj_Open", "Adj_Close", "Source", "Updated_At"]

DEFAULT_EXCHANGE = "NASDAQ"


# ══════════════════════════════════════════════════════════════════════════
# 규칙 계산 — 핫 패스. FMP·시트 접근 없음.
# ══════════════════════════════════════════════════════════════════════════
def easter_sunday(year: int) -> date:
    """익명 그레고리력 알고리즘. 굿프라이데이는 이 날짜 -2일."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """그 달의 n번째 <weekday>. weekday 는 월=0 … 일=6."""
    d = date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    return d + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """그 달의 마지막 <weekday>."""
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date, is_new_year: bool = False):
    """NYSE 대체 휴일 규칙.

    토요일에 걸리면 **전날 금요일**을 쉰다. 단 신정만 예외로 쉬지 않는다
    (전년도 12/31 은 정상 개장). 일요일이면 다음 월요일.

    반환이 None 이면 '그 해에는 관측하지 않음'을 뜻한다.
    """
    wd = d.weekday()
    if wd == 5:                       # 토
        return None if is_new_year else d - timedelta(days=1)
    if wd == 6:                       # 일
        return d + timedelta(days=1)
    return d


def nyse_regular_holidays(year: int) -> dict:
    """그 해의 NYSE 정규 휴장일 → {"YYYY-MM-DD": 이름}.

    임시 휴장(국장일 등)은 **포함하지 않는다** — 규칙으로 계산할 수 없다.
    그쪽은 Market_Calendar 시트의 extra_closed 로 보강한다.
    """
    out = {}

    def _put(d, name, is_new_year=False):
        od = _observed(d, is_new_year=is_new_year)
        if od is not None and od.year == year:
            out[od.isoformat()] = name

    _put(date(year, 1, 1), "New Year's Day", is_new_year=True)
    _put(_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day")
    _put(_nth_weekday(year, 2, 0, 3), "Washington's Birthday")

    # ⚠️ 굿프라이데이에는 _observed 를 적용하지 않는다.
    #    정의상 항상 금요일이라 대체 휴일 규칙이 걸릴 일이 없는데, 그래도
    #    적용해 두면 **부활절 계산이 하루 틀렸을 때 토요일→전날 금요일 보정이
    #    틀린 값을 정답으로 되돌려 버린다.** 오류를 숨기는 코드가 된다
    #    (실제로 뮤테이션 테스트 M3 가 이걸 잡아냈다).
    #    금요일이 아니면 계산이 틀린 것이므로 넣지 않고 넘어간다.
    gf = easter_sunday(year) - timedelta(days=2)
    if gf.weekday() == 4 and gf.year == year:
        out[gf.isoformat()] = "Good Friday"

    _put(_last_weekday(year, 5, 0), "Memorial Day")
    _put(date(year, 6, 19), "Juneteenth National Independence Day")
    _put(date(year, 7, 4), "Independence Day")
    _put(_nth_weekday(year, 9, 0, 1), "Labor Day")
    _put(_nth_weekday(year, 11, 3, 4), "Thanksgiving Day")
    _put(date(year, 12, 25), "Christmas Day")
    return out


# 프로세스 캐시 — 같은 실행 안에서 두 번 이상 물어보는 스크립트가 있다
# (run_drg_predict 는 909 와 1160 두 곳). 연도당 한 번만 계산한다.
_RULE_CACHE: dict = {}


def regular_holidays_cached(year: int) -> dict:
    if year not in _RULE_CACHE:
        _RULE_CACHE[year] = nyse_regular_holidays(year)
    return _RULE_CACHE[year]


def is_market_open(d=None, extra_closed=None) -> bool:
    """개장일 여부. FMP·시트 접근 없음.

    d            : date / datetime / "YYYY-MM-DD" / None(=오늘 ET)
    extra_closed : 임시 휴장 날짜 문자열 집합(선택). Market_Calendar 보강분.

    ⚠️ 판정 불가 상황에서는 **개장(True)** 으로 폴백한다. 알림을 통째로
       놓치는 것보다 헛도는 쪽이 덜 위험하다는 판단이다.
    """
    dd = _coerce_date(d)
    if dd is None:
        return True
    if dd.weekday() >= 5:
        return False
    key = dd.isoformat()
    if extra_closed and key in extra_closed:
        return False
    return key not in regular_holidays_cached(dd.year)


def is_market_open_today(extra_closed=None) -> bool:
    """오늘(ET 기준) 개장 여부 — 자동화 가드용 진입점."""
    return is_market_open(None, extra_closed=extra_closed)


def _coerce_date(d):
    if d is None:
        if _ET is not None:
            return datetime.now(_ET).date()
        return datetime.now().date()
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    s = str(d).strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def holiday_name(d=None) -> str:
    """휴장일이면 이름, 아니면 빈 문자열. 로그 메시지용."""
    dd = _coerce_date(d)
    if dd is None:
        return ""
    return regular_holidays_cached(dd.year).get(dd.isoformat(), "")


def next_trading_day(d=None, extra_closed=None) -> date:
    """d 이후(d 제외) 첫 개장일."""
    dd = _coerce_date(d) or datetime.now().date()
    for _ in range(20):
        dd = dd + timedelta(days=1)
        if is_market_open(dd, extra_closed=extra_closed):
            return dd
    return dd


def prev_trading_day(d=None, extra_closed=None) -> date:
    """d 이전(d 제외) 마지막 개장일."""
    dd = _coerce_date(d) or datetime.now().date()
    for _ in range(20):
        dd = dd - timedelta(days=1)
        if is_market_open(dd, extra_closed=extra_closed):
            return dd
    return dd


# ══════════════════════════════════════════════════════════════════════════
# 시트 파싱 — 순수 함수. gspread 를 모르는 채로 값 배열만 받는다.
# ══════════════════════════════════════════════════════════════════════════
def _truthy(v) -> bool:
    return str(v).strip().lower() in ("y", "yes", "true", "1", "t")


def parse_calendar_values(values) -> dict:
    """Market_Calendar 시트의 get_all_values() 결과를 파싱.

    반환: {"closed": set(날짜), "half": {날짜: 마감시각}, "years": set(연도)}

    헤더가 없거나 깨져 있으면 빈 결과를 돌려준다 — 예외를 던지지 않는다.
    이 값은 **보강용**이라 없어도 규칙 계산으로 정상 동작해야 한다.
    """
    out = {"closed": set(), "half": {}, "years": set()}
    if not values or len(values) < 2:
        return out
    hdr = [str(c).strip() for c in values[0]]
    try:
        i_date = hdr.index("Date")
        i_closed = hdr.index("Is_Closed")
    except ValueError:
        return out
    i_close_t = hdr.index("Adj_Close") if "Adj_Close" in hdr else None

    for row in values[1:]:
        r = list(row)
        ds = str(r[i_date]).strip()[:10] if i_date < len(r) else ""
        if len(ds) != 10:
            continue
        try:
            out["years"].add(int(ds[:4]))
        except Exception:
            continue
        closed = _truthy(r[i_closed]) if i_closed < len(r) else False
        if closed:
            out["closed"].add(ds)
        elif i_close_t is not None and i_close_t < len(r):
            t = str(r[i_close_t]).strip()
            if t:
                out["half"][ds] = t
    return out


def rows_from_fmp(records, source: str = "FMP", now_str: str = "") -> list:
    """FMP holidays-by-exchange 응답 → 시트 행 배열(헤더 제외).

    FMP 응답 키: exchange, date, name, isClosed, adjOpenTime, adjCloseTime
    """
    rows = []
    for rec in (records or []):
        if not isinstance(rec, dict):
            continue
        ds = str(rec.get("date") or "").strip()[:10]
        if len(ds) != 10:
            continue
        rows.append([
            ds,
            str(rec.get("exchange") or DEFAULT_EXCHANGE),
            str(rec.get("name") or ""),
            "Y" if rec.get("isClosed") else "N",
            str(rec.get("adjOpenTime") or ""),
            str(rec.get("adjCloseTime") or ""),
            source,
            now_str,
        ])
    rows.sort(key=lambda r: r[0])
    return rows


def diff_against_rules(records, years=None) -> dict:
    """FMP 응답을 규칙 계산과 대조한다.

    반환:
      extra_closed : FMP 가 닫혔다는데 규칙에는 없는 날 → **임시 휴장 후보**
      missing      : 규칙에는 있는데 FMP 응답에 없는 날 → 규칙 오류 또는 응답 누락
      half_days    : 마감 시각이 조정된 날(반일장)

    `extra_closed` 가 이 대조의 존재 이유다. 2025-01-09(카터 국장일) 같은 날은
    규칙으로 절대 못 잡고, 이 대조에서만 드러난다.
    """
    fmp_closed, half = {}, {}
    seen_years = set()
    for rec in (records or []):
        if not isinstance(rec, dict):
            continue
        ds = str(rec.get("date") or "").strip()[:10]
        if len(ds) != 10:
            continue
        try:
            seen_years.add(int(ds[:4]))
        except Exception:
            continue
        if rec.get("isClosed"):
            fmp_closed[ds] = str(rec.get("name") or "")
        else:
            t = str(rec.get("adjCloseTime") or "").strip()
            if t:
                half[ds] = t

    target_years = set(years) if years else seen_years
    rule = {}
    for y in target_years:
        rule.update(nyse_regular_holidays(y))

    extra = {d: n for d, n in fmp_closed.items()
             if d not in rule and int(d[:4]) in target_years}
    missing = {d: n for d, n in rule.items() if d not in fmp_closed}
    return {"extra_closed": extra, "missing": missing, "half_days": half}


# ══════════════════════════════════════════════════════════════════════════
# FMP 조회 — 주간 갱신 잡 전용. 판정 경로에서는 절대 호출하지 않는다.
# ══════════════════════════════════════════════════════════════════════════
def fetch_calendar_fmp(api_key: str, years, exchange: str = DEFAULT_EXCHANGE,
                       timeout: float = 15.0) -> list:
    """holidays-by-exchange 조회. 실패하면 빈 리스트 — 예외를 올리지 않는다.

    판정 경로가 이 함수에 의존하지 않으므로, 실패해도 시스템은 규칙 계산으로
    정상 동작한다. 그래서 조용히 실패해도 안전하다.
    """
    import requests                                  # 지연 import — 임포트 비용 회피

    key = str(api_key or "").strip()
    if not key or not years:
        return []
    ys = sorted({int(y) for y in years})
    url = ("https://financialmodelingprep.com/stable/holidays-by-exchange"
           "?exchange=" + str(exchange)
           + "&from=" + str(ys[0]) + "-01-01"
           + "&to=" + str(ys[-1]) + "-12-31"
           + "&apikey=" + key)
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]
