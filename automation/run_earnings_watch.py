# -*- coding: utf-8 -*-
"""
run_earnings_watch.py
─────────────────────
실적 레이더 자동화 — 평일 5PM ET 단일 실행, 3패스.

  1) 사전 (D-3 도달): 예상 변동폭 스냅샷 저장 → Earnings_Events
  2) 사후 (반응일 종가 확정): 갭 측정 · PEAD 판정 · (C층 도입 시) 예측 채점
  3) 지연 (D+5): D5_Return_Pct 채움

그리고 수신자별로 **본인 종목만** 담은 메일 1통을 보낸다.

왜 8AM job 이 없는가:
  반응 판정은 '종가'가 있어야 완결된다.
    · BMO 발표 → 당일이 반응일  → 당일 종가로 확정
    · AMC 발표 → 다음날이 반응일 → 다음날 종가로 확정
  둘 다 장 마감 후에만 확정되므로 5PM 단일 실행이 정확하다.

⚠️ lockstep: earnings_core / accounts_core / users_core / regime_core 와 함께 배포.
   Earnings_Events 시트는 티커 단위(관리자 소유·게스트 읽기 전용)이고,
   계좌별 축소 판정은 저장하지 않고 **수신자별 런타임 계산**한다.
   → 같은 티커를 여러 사용자가 보유해도 시트 행은 하나, FMP 조회도 1회.

실행: python automation/run_earnings_watch.py   (repo root 에서)
"""

import json
import os
import smtplib
import sys
import traceback
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import gspread
import numpy as np
import pandas as pd
import pytz
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import accounts_core as ac    # noqa: E402
import earnings_core as ec    # noqa: E402
# ⚠️ fmp_extras 는 선택이 아니다 — 모든 FMP 호출이 fx.fmp_get(레이트 리미터)을
#    거쳐야 429 가 조용히 빈 응답으로 사라지지 않는다(diag_fmp_ssot A1).
import fmp_extras as fx      # noqa: E402
import gs_retry as gsr  # noqa: E402  — Sheets 재시도 SSOT(503 등 일시 장애 방어)
import users_core as uc       # noqa: E402

# ── 환경변수 ──────────────────────────────────────────────────────────────
FMP_API_KEY        = os.environ["FMP_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]
_gcp_info = json.loads(os.environ["GSPREAD_KEY"])

# 유니버스 강제 재계산 (수동 워크플로 전용, 선택). 갱신일(월요일)이 아니어도
# ec.fetch_market_universe() 를 실제로 태워보고 싶을 때 쓴다.
# ⚠️ 정기 실행에서는 절대 켜지 말 것 — 매일 스크리너를 호출하게 된다.
FORCE_UNIVERSE = str(os.environ.get("FORCE_UNIVERSE", "") or "").strip().lower() \
    in ("1", "true", "y", "yes")

# 캘린더 전 종목 강제 재조회 (수동 워크플로 전용, 선택).
# needs_refresh 의 티어 주기(far=30일)를 무시한다.
#
# 필요한 이유: 조회 로직이 바뀌어도 기존 행은 Last_Checked 가 최근이라 far 티어면
# 30일간 재조회되지 않는다. 2026-08-13 의 earnings-calendar 엔드포인트 버그처럼
# **저장된 값 자체가 틀린 경우** 수정 코드를 배포해도 두 달간 반영되지 않는다.
# ⚠️ 정기 실행에서는 절대 켜지 말 것 — 매일 전 종목을 조회하게 된다.
FORCE_CALENDAR = str(os.environ.get("FORCE_CALENDAR", "") or "").strip().lower() \
    in ("1", "true", "y", "yes")

_ET = pytz.timezone("America/New_York")
_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 15
_SPREADSHEET_TITLE = "Quant_DB"
_WATCHLIST_WORKSHEET = "Watchlist"
_PF_WORKSHEET = "Portfolios"
_PROFILE_WORKSHEET = "Account_Profile"
_USERS_WORKSHEET = "Users"

# ── 가격 이력 조회 창 ─────────────────────────────────────────────────────
# [2026-08-28] `limit=900` → `from`/`to` 창. FMP 는 historical-price-eod 의
#   `limit` 을 **무시**하므로 실제로는 매 호출 약 1,254봉(281KB)을 받고 있었다.
#
# 옛 주석은 "8분기(≈2년)" 라고 적었지만 **소비 구문이 요구하는 것은 12분기**다:
#     ec.past_earnings_dates(limit=GAP_QUARTERS + 4)  → 최신 12개 이벤트
#     ec.gap_history(hist, past)                      → 성공 8건 모일 때까지 12개 순회
#   gap_history 는 측정 실패한 이벤트를 건너뛰고 계속 돈다. 8분기로 창을 자르면
#   9~12번째 이벤트가 창 밖으로 나가고, 앞 8개 중 하나라도 실패하면 표본이 8
#   아래로 떨어진다. 6 미만이면 expected_move 의 confidence 가 강등된다
#   (earnings_core: `len(vals) < GAP_QUARTERS - 2`).
#
#   → **옛 limit 숫자를 요구의 근거로 쓰지 않았다.** limit 은 무시돼 온 값이라
#     검증된 적이 없다. 요구는 소비 구문에서 역산했다(run_narrative 가 실증:
#     limit 130 / 실요구 200봉).
#
# ⚠️ 호출부별 `bars` 인자를 두지 않는 이유 — 6개 호출부가 **hist_cache 를 공유**한다.
#    먼저 부른 쪽의 창이 캐시에 박히므로 요구가 다른 창을 섞으면 나중 호출부가
#    자기 요구보다 얕은 이력을 받고도 그 사실을 모른다. 창은 최심 소비처 하나로
#    통일하는 것이 맞다. (§7 의 "창 함수에 기본값 금지" 는 호출부가 캐시를
#    공유하지 않는 경우의 규칙이다 — 여기서는 반대로 단일 창이 정답이다.)
_HIST_QUARTERS = ec.GAP_QUARTERS + 4       # = 12. past_earnings_dates 상한과 동일
_QUARTER_DAYS = 91.31                      # 365.25 / 4
_HIST_BARS = (fx.bars_for_calendar_days(_HIST_QUARTERS * _QUARTER_DAYS)
              + ec.VOLUME_BASELINE_BARS    # measure_reaction 의 거래량 기준 구간
              + 1)                         # 직전 종가(pre_close) 1봉
_HIST_DAYS = fx.hist_days_for_bars(_HIST_BARS)   # 1,133달력일 ≈ 778봉 (실측)
_HIST_EDGE_TOL_DAYS = 7
#   이력의 첫 봉이 창 하단에서 이 일수 안쪽이면 "창에 맞닿았다"고 본다.
#   긴 연휴(추수감사절 주간 등)를 흡수할 만큼만 잡는다.
_THESIS_WORKSHEET = "Thesis"     # app.py _THESIS_SHEET_COLS 와 lockstep
_CORE_CATEGORY = "core_dca"      # 코어/정기적립 마커 (app.py save_thesis_row 와 동일)

_NYSE_HOLIDAYS = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def is_market_open_today() -> bool:
    now = datetime.now(_ET)
    return now.weekday() < 5 and now.strftime("%Y-%m-%d") not in _NYSE_HOLIDAYS


# ── Sheets ────────────────────────────────────────────────────────────────
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    return gspread.authorize(Credentials.from_service_account_info(_gcp_info, scopes=scopes))


_SH = None


def _sheet():
    global _SH
    if _SH is None:
        _SH = gsr.call(get_gspread_client().open, _SPREADSHEET_TITLE,
                       _label="open(Quant_DB)")
    return _SH


def _ws(title: str, cols: list = None):
    """워크시트 조회. 없으면 헤더와 함께 생성(신규 배포 첫 실행 대비).

    ⚠️ 이미 있는 시트라도 스키마가 넓어졌으면 **그리드를 먼저 확장**해야 한다.
       Google Sheets 는 열 수가 고정이라 17열 시트에 20열을 쓰면
       'exceeds grid limits' APIError 가 난다(Users 시트에서 겪은 문제).
    """
    sh = _sheet()
    try:
        w = gsr.call(sh.worksheet, title, _label=f"worksheet({title})")
        if cols:
            try:
                if int(getattr(w, "col_count", 0) or 0) < len(cols):
                    gsr.call(w.add_cols, len(cols) - int(w.col_count),
                             _label=f"add_cols({title})")
                    print(f"[MIGRATE] '{title}' 열 확장 → {len(cols)}열")
                    _end = gspread.utils.rowcol_to_a1(1, len(cols))
                    gsr.call(w.update, [cols], range_name=f"A1:{_end}",
                             value_input_option="USER_ENTERED",
                             _label=f"header({title})")
            except Exception as e:
                print(f"[WARN] '{title}' 열 확장 실패: {e}")
        return w
    except Exception:
        if not cols:
            raise
        w = gsr.call(sh.add_worksheet, title=title, rows=2000,
                     cols=max(len(cols), 26), _label=f"add_worksheet({title})")
        gsr.call(w.update, [cols],
                 range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(cols))}",
                 value_input_option="USER_ENTERED", _label=f"header({title})")
        print(f"[INIT] '{title}' 시트 생성")
        return w


def _events_ws():
    return _ws(ec.EVENTS_WORKSHEET, ec.EVENTS_COLS)


def _calendar_ws():
    return _ws(ec.CALENDAR_WORKSHEET, ec.CALENDAR_COLS)


def _preview_ws():
    return _ws(ec.PREVIEW_WORKSHEET, ec.PREVIEW_COLS)


def _col_a1(n: int) -> str:
    """1-base 열 번호 → A1 문자 (AA 이상 대응)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _safe_update(ws, values: list, first_row: int, ncol: int):
    """A열 앵커 명시 범위 기록 — append_row 계단 드리프트 방지(프로젝트 원칙 #3)."""
    if not values:
        return
    last = _col_a1(ncol)
    gsr.call(ws.update, values,
             range_name=f"A{first_row}:{last}{first_row + len(values) - 1}",
             value_input_option="USER_ENTERED", _label="safe_update")


def _merge_runs(updates) -> list:
    """[(row_i, vals)] → [(first_row, [vals...])] 로 **연속 행 병합**.

    행 번호를 정렬해 인접 구간을 하나의 range 로 합친다. 전 종목 갱신
    (FORCE_CALENDAR)처럼 행이 촘촘하면 169개가 1~2구간으로 줄어든다.
    """
    out = []
    for row_i, vals in sorted(updates, key=lambda x: int(x[0])):
        row_i = int(row_i)
        if out and out[-1][0] + len(out[-1][1]) == row_i:
            out[-1][1].append(vals)
        else:
            out.append((row_i, [vals]))
    return [(r, v) for r, v in out]


def _batch_update(ws, updates, ncol: int, label: str = "batch") -> int:
    """행 단위 갱신 목록을 **최소 API 호출**로 기록. 반환: 실제 API 호출 수.

    2026-08-14 도입 — 429 다발의 근본 원인 해결.
      이전에는 행마다 ws.update() 를 불렀다. FORCE_CALENDAR 로 169행을 갱신하니
      쓰기 API 169콜이 되었고, Google Sheets 한도는 **분당 60회**라 구조적으로
      초과였다. gs_retry 의 4회 백오프로도 3건이 최종 실패했고, 쓰기가 실패하면
      Last_Checked 도 안 써져 해당 행이 far 티어 30일 주기에 다시 갇힌다.

      1) 연속 행을 하나의 range 로 병합 (_merge_runs)
      2) 남은 비연속 range 들을 batch_update 로 **단일 호출**에 전송

    batch_update 가 실패하면 구간별 개별 기록으로 폴백한다 — gspread 버전에
    따라 시그니처 차이가 있을 수 있고, 부분 기록이라도 남기는 편이 낫다.
    """
    runs = _merge_runs(updates)
    if not runs:
        return 0
    last = _col_a1(ncol)
    body = [{"range": f"A{r}:{last}{r + len(v) - 1}", "values": v} for r, v in runs]

    # 메서드 부재는 네트워크 문제가 아니다 — 호출하면 gs_retry 가 '상태를 못 읽는
    # 예외'로 보고 4회 백오프(약 25초)를 낭비한다. 먼저 존재 여부만 확인한다.
    if not hasattr(ws, "batch_update"):
        print("  [WARN] batch_update 미지원 gspread — 구간별 기록으로 폴백")
    else:
        try:
            gsr.call(ws.batch_update, body, value_input_option="USER_ENTERED",
                     _label=f"{label}.batch({len(body)}구간)")
            return 1
        except Exception as e:
            print(f"  [WARN] batch_update 실패({e}) — 구간별 기록으로 폴백")

    n = 0
    for r, v in runs:
        try:
            _safe_update(ws, v, r, ncol)
            n += 1
        except Exception as e:
            print(f"[ERROR] 행 {r}~{r + len(v) - 1} 갱신 실패: {e}")
    return n


# ── FMP ───────────────────────────────────────────────────────────────────
def fmp_price_history(ticker: str) -> pd.DataFrame:
    """historical-price-eod → OHLCV DataFrame(오름차순, Close 결측 제거).

    창은 모듈 상수 `_HIST_DAYS` 하나다. 인자로 받지 않는 이유는 위 상수 블록
    참조 — 호출부 6곳이 `hist_cache` 를 공유하기 때문이다.

    `fx.fmp_get` 을 쓰는 이유는 스타일이 아니다: 원시 `requests.get` 은 분당
    한도를 우회해 초과분이 429 → 빈 DataFrame 으로 조용히 사라진다. 그러면
    실적 갭 표본이 줄어든 것이 "종목 이력이 짧다"와 구분되지 않는다.
    """
    try:
        r = fx.fmp_get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker}"
            + fx.hist_range_params(_HIST_DAYS)
            + f"&apikey={FMP_API_KEY}", timeout=_FMP_TIMEOUT)
        if r is None or r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "date" not in df.columns or "close" not in df.columns:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        out = pd.DataFrame(index=df.index)
        for src, dst in [("open", "Open"), ("high", "High"), ("low", "Low"),
                         ("close", "Close"), ("volume", "Volume")]:
            if src in df.columns:
                out[dst] = pd.to_numeric(df[src], errors="coerce")
        return out.dropna(subset=["Close"])
    except Exception:
        return pd.DataFrame()



def _hist_window_bound(hist) -> bool:
    """이력의 시작이 조회 창 하단 경계에 맞닿아 있는가.

    갭 표본이 모자랄 때 원인이 **창**인지 **종목의 짧은 이력**인지 가르는
    판별자다. 개수만 보면 두 원인이 구분되지 않는다 — 상장 1년 된 종목의
    n=4 는 정상이고, 상장 10년 된 종목의 n=4 는 창이 짧다는 뜻이다.

    창을 줄일 때 유일하게 위험한 실패 모드가 이것이다: measure_reaction 은
    이벤트가 창 밖이면 값을 틀리게 주는 게 아니라 **측정을 조용히 건너뛴다**
    (resolve_reaction_index 가 cand<=0 에서 None). 로그가 없으면 아무도 모른다.
    """
    try:
        if hist is None or hist.empty:
            return False
        first = pd.Timestamp(hist.index[0]).date()
        edge = datetime.now(_ET).date() - timedelta(days=_HIST_DAYS)
        return (first - edge).days <= _HIST_EDGE_TOL_DAYS
    except Exception:
        return False


# ── 대상 수집 ─────────────────────────────────────────────────────────────
def load_universe():
    """수신자별 관심 종목 수집.

    반환: (holdings, watch, all_tickers)
      holdings  : {uid: {account: [{ticker, qty, avg}]}}
      watch     : {uid: {ticker: account}}
      wl_stops  : {(uid, TICKER): Stop_Loss}  — 게이트 손절폭 산출용
    """
    holdings, watch, tickers, wl_stops = {}, {}, set(), {}

    try:
        vals = gsr.call(_ws(_PF_WORKSHEET).get_all_values, _label="Portfolios") or []
        for r in vals[1:]:
            r = (list(r) + [""] * 8)[:8]
            uid, acct, tk = str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip().upper()
            if not uid or not tk:
                continue
            avg = pd.to_numeric(r[3], errors="coerce")
            qty = pd.to_numeric(r[4], errors="coerce")
            if pd.isna(qty) or float(qty) <= 0:
                continue
            holdings.setdefault(uid, {}).setdefault(acct, []).append({
                "ticker": tk, "qty": float(qty),
                "avg": (float(avg) if pd.notna(avg) else None),
            })
            tickers.add(tk)
    except Exception as e:
        print(f"[WARN] Portfolios 로드 실패: {e}")

    try:
        vals = gsr.call(_ws(_WATCHLIST_WORKSHEET).get_all_values,
                        _label="Watchlist") or []
        for r in vals[1:]:
            r = (list(r) + [""] * 13)[:13]
            uid, tk = str(r[0]).strip(), str(r[1]).strip().upper()
            if not uid or not tk:
                continue
            watch.setdefault(uid, {})[tk] = str(r[12]).strip()
            _sl = pd.to_numeric(r[8], errors="coerce")   # I열 = Stop_Loss
            if pd.notna(_sl) and float(_sl) > 0:
                wl_stops[(uid, tk)] = float(_sl)
            tickers.add(tk)
    except Exception as e:
        print(f"[WARN] Watchlist 로드 실패: {e}")

    return holdings, watch, sorted(tickers), wl_stops


def _universe_ws():
    return _ws(ec.UNIVERSE_WORKSHEET, ec.UNIVERSE_COLS)


def load_market_universe() -> dict:
    """Earnings_Universe 시트 → {TICKER: dict}. 없거나 실패 시 빈 dict."""
    try:
        rows = ec.parse_universe(
            gsr.call(_universe_ws().get_all_values, _label="Earnings_Universe") or [])
        return {str(r.get("Ticker") or "").strip().upper(): r for r in rows
                if str(r.get("Ticker") or "").strip()}
    except Exception as e:
        print(f"[WARN] Earnings_Universe 로드 실패: {e}")
        return {}


def pass_universe(today, now_et, force: bool = False) -> dict:
    """Tier 2 유니버스 주 1회 갱신. 반환: {TICKER: dict} (갱신 여부와 무관).

    전체 덮어쓰기다. 부분 결과로 덮으면 멤버십이 조용히 반토막 나므로,
    ec.fetch_market_universe 가 ok=False 를 주면 **기존 시트를 그대로 둔다.**
    """
    cur = load_market_universe()
    is_refresh_day = (int(pd.Timestamp(today).weekday()) == ec.UNIVERSE_REFRESH_WEEKDAY)
    if not (force or is_refresh_day or not cur):
        print(f"[UNIV] 갱신일 아님 — 기존 {len(cur)}종목 사용")
        return cur
    if force and not is_refresh_day:
        print("[UNIV] 강제 재계산(FORCE_UNIVERSE)")

    res = ec.fetch_market_universe(key=FMP_API_KEY)
    print(f"[UNIV] {res.get('diag') or ''}")
    if not res.get("ok"):
        print(f"[UNIV] 조회 실패 — 기존 {len(cur)}종목 유지")
        return cur

    rows = ec.merge_universe_sources(res["ndx"], res["sp"], now_et=now_et,
                                     labels=res.get("labels"))
    if not rows:
        print("[UNIV] 병합 결과 0종목 — 기존 유지")
        return cur

    try:
        ws = _universe_ws()
        gsr.call(ws.clear, _label="Earnings_Universe.clear")
        gsr.call(ws.update, [ec.UNIVERSE_COLS] + rows,
                 range_name=f"A1:{_col_a1(ec.UNIVERSE_NCOL)}{len(rows) + 1}",
                 value_input_option="USER_ENTERED", _label="Earnings_Universe.write")
        _n_cap = sum(1 for r in rows if str(r[3] or "").strip())
        _by_src = {}
        for r in rows:
            _by_src[r[4]] = _by_src.get(r[4], 0) + 1
        _brk = " · ".join(f"{k} {v}" for k, v in sorted(_by_src.items()))
        print(f"[UNIV] 갱신 완료 — {len(rows)}종목 (출처 {res.get('source')}) "
              f"· {_brk} · 시총채움 {_n_cap}")
    except Exception as e:
        print(f"[ERROR] Earnings_Universe 기록 실패: {e} — 기존 유지")
        return cur

    return {r[0]: dict(zip(ec.UNIVERSE_COLS, r)) for r in rows}


def load_profiles(uid: str, cache: dict):
    if "_vals" not in cache:
        try:
            cache["_vals"] = gsr.call(_ws(_PROFILE_WORKSHEET).get_all_values,
                                      _label="Account_Profile") or []
        except Exception:
            cache["_vals"] = []
    if uid not in cache:
        cache[uid] = ac.parse_profiles(cache["_vals"], uid)
    return cache[uid]


def load_core_keys() -> set:
    """코어/정기적립 보유 키 {(uid_lower, account_lower, TICKER)}.

    Portfolios 시트에는 메모 컬럼이 없다. 코어 여부는 Thesis 시트의
    Narrative_Category == 'core_dca' 가 SSOT (app.py save_thesis_row 와 동일).
    """
    keys = set()
    try:
        vals = gsr.call(_ws(_THESIS_WORKSHEET).get_all_values, _label="Thesis") or []
        for r in vals[1:]:
            r = (list(r) + [""] * 7)[:7]
            if str(r[4]).strip().lower() != _CORE_CATEGORY:
                continue
            uid = str(r[0]).strip().lower()
            tk = str(r[1]).strip().upper()
            acct = str(r[2]).strip().lower()
            if uid and tk:
                keys.add((uid, acct, tk))
    except Exception as e:
        print(f"[INFO] Thesis 조회 생략(코어 면제 미적용): {e}")
    return keys


# ── 패스 0: 캘린더 갱신 (계단식 1A) ───────────────────────────────────────
#   앱 탭이 FMP 를 한 번도 부르지 않도록, 실적일과 예상 변동폭을 여기서 시트에
#   적재한다. 전 종목을 매일 3콜씩 검증하면 자동화가 느려지므로 D-Day 에 따라
#   갱신 주기를 나눈다(far 30일 / mid 7일 / near 매일).
# ──────────────────────────────────────────────────────────────────────────

def pass_calendar(tickers, cal, hist_cache, today, now_et, user_set=None,
                  force: bool = False, market_map=None):
    """캘린더 갱신. 반환: (updates, appends, cal) — cal 은 갱신 반영된 dict.

    user_set: 보유/워치리스트 티커 집합. 여기 없으면 Tier 2(universe)로 기록해
      스냅샷/이메일/축소 판정에서 제외된다. None 이면 전부 user(구 동작).
    """
    updates, appends = [], []
    n_skip = n_light = n_full = n_move = n_map = 0
    n_timing = n_infer = 0

    for tk in tickers:
        try:
            prev = cal.get(tk)
            if prev is not None and not (force or ec.needs_refresh(prev, today)):
                n_skip += 1
                continue

            dd_prev = ec.days_until_from_row(prev, today) if prev else None
            tier = ec.tier_of(dd_prev)

            # near 만 3콜 교차 확인 — 그 밖은 확정일이 어차피 안 나온다
            _in_map = tk in (market_map or {})
            if tier == ec.TIER_NEAR:
                ev = ec.fetch_next_earnings(tk, today=today, key=FMP_API_KEY,
                                            market_map=market_map)
                n_full += 1
            else:
                ev = ec.fetch_next_earnings_light(tk, today=today, key=FMP_API_KEY,
                                                  market_map=market_map)
                if _in_map:
                    n_map += 1          # 맵에서 바로 해결 — FMP 콜 0
                else:
                    n_light += 1
                # 경량 조회 결과가 D-10 안이면 즉시 정밀(2콜)로 승격
                if ev and ev["days_until"] <= ec.SCAN_HORIZON_DAYS:
                    ev = ec.fetch_next_earnings(tk, today=today, key=FMP_API_KEY,
                                                market_map=market_map) or ev
                    n_full += 1

            dd = ev["days_until"] if ev else None
            row_for_move = dict(prev or {})
            if ev:
                row_for_move["Earnings_Date"] = ev["earnings_date"]

            # ⚠️ force 는 needs_move 도 우회해야 한다.
            #   FORCE_CALENDAR 는 "캐시 무시 전면 재조회"인데 변동폭 캐시만 남겨두면
            #   이 블록이 통째로 건너뛰어진다. 2026-08-14 실측에서 `변동폭 0 → 추론 0`
            #   이 나온 원인이 이것이다(7종목이 이미 Move_For_Date 를 갖고 있었다).
            #
            #   timing 추론을 별도 조건으로 빼지 않는 이유: 추론이 보류되면 timing 이
            #   계속 비어 있어 **매 실행마다 hist·past 를 다시 부르는 루프**가 된다.
            #   과거 실적 반응은 분기 내내 변하지 않으므로 재시도는 무의미하다.
            #   needs_move 는 분기가 바뀔 때 True 가 되므로 추론 시점으로 정확하다.
            move = None
            if ev is not None and (force or ec.needs_move(row_for_move, days_until=dd)):
                hist = hist_cache.get(tk)
                if hist is None:
                    hist = hist_cache[tk] = fmp_price_history(tk)
                if hist is not None and not hist.empty:
                    past = ec.past_earnings_dates(tk, today=today, key=FMP_API_KEY)
                    _do_move = force or ec.needs_move(row_for_move, days_until=dd)
                    if _do_move:
                        move = ec.expected_move(ec.gap_history(hist, past),
                                                atr_pct=ec.atr_pct_of(hist))
                        n_move += 1
                    # BMO/AMC 추론 — FMP stable 이 발표 시각을 안 주므로 과거
                    # 거래량 패턴에서 역산한다. hist·past 가 이미 여기 있어 콜 0.
                    if ev is not None and not str(ev.get("timing") or ""):
                        _inf = ec.infer_timing(hist, past)
                        if _inf.get("ok"):
                            ev["timing"] = _inf["timing"]
                            n_infer += 1
                            print(f"  [TIME] {tk} → {_inf['timing']} "
                                  f"({_inf['votes']['bmo']}b/{_inf['votes']['amc']}a "
                                  f"n={_inf['n']} {_inf['ratio']:.0%})")
                    if move is not None:
                        _sn = int(move.get("sample_n") or 0)
                        print(f"  [MOVE] {tk} D-{dd} ±{move.get('median_pct')}% "
                              f"n={_sn} ({move.get('confidence')})")
                        if _sn < ec.GAP_QUARTERS and _hist_window_bound(hist):
                            print(f"  [WARN] {tk} 갭 표본 {_sn}/{ec.GAP_QUARTERS} "
                                  f"— 이력 시작이 조회 창 하단({_HIST_DAYS}일)과 "
                                  f"맞닿음. 창 제약 의심 → _HIST_QUARTERS 상향 검토")

            _src = (ec.SOURCE_USER if (user_set is None or tk in user_set)
                    else ec.SOURCE_UNIVERSE)
            if ev and str(ev.get("timing") or ""):
                n_timing += 1
            row = ec.calendar_row(tk, ev, move, today=today, now_et=now_et,
                                  prev=prev, source=_src)
            if prev is not None:
                updates.append((prev["_row"], row))
            else:
                appends.append(row)
            cal[tk] = {c: row[i] for i, c in enumerate(ec.CALENDAR_COLS)}
            cal[tk]["_row"] = (prev or {}).get("_row", 0)
        except Exception as e:
            print(f"  [WARN] {tk} 캘린더 갱신 실패: {e}")

    # 정밀 조회는 earnings?symbol= + quote?symbol= 2콜이다.
    # (2026-08-13 이전에는 earnings-calendar 를 포함해 3콜이었으나, 그 엔드포인트가
    #  시장 전체용이라 제거했다 — 로그 문구도 함께 정정)
    print(f"[CAL] 생략 {n_skip} · 맵적중 {n_map}(0콜) · 경량 {n_light}콜 · "
          f"정밀 {n_full}×2콜 · 변동폭 {n_move} · "
          f"timing확보 {n_timing}(추론 {n_infer})")
    return updates, appends, cal


# ── 패스 1: 사전 스냅샷 ───────────────────────────────────────────────────
def pass_snapshot(cal, existing, hist_cache, today, now_et):
    """D-SNAPSHOT_DAYS 도달 종목의 스냅샷. **캘린더에서 읽으므로 FMP 재조회 없음.**"""
    new_rows, snapshots = [], {}
    n_univ = 0
    for tk, row in (cal or {}).items():
        try:
            # Tier 2(유니버스)는 일정/예상갭만 보여준다 — 스냅샷·이메일·축소 제외
            if ec.is_universe_only(row):
                n_univ += 1
                continue
            ed = str(row.get("Earnings_Date") or "")
            dd = ec.days_until_from_row(row, today)
            if dd is None or dd < 0 or dd > ec.SCAN_HORIZON_DAYS:
                continue
            move = ec.move_from_row(row)
            ev = {"ticker": tk, "earnings_date": ed, "days_until": dd,
                  "timing": str(row.get("Timing") or ""),
                  "date_source": str(row.get("Date_Source") or "")}

            eid = ec.event_id(tk, ed)
            if eid in existing:
                rec = dict(existing[eid]); rec["_move"] = move; rec["_event"] = ev
                snapshots[tk] = rec
                continue
            if dd > ec.SNAPSHOT_DAYS:
                # 아직 스냅샷 시점은 아니지만 앱/메일 표시용으로는 캘린더 값을 쓴다
                snapshots[tk] = {"Ticker": tk, "Earnings_Date": ed,
                                 "Date_Source": ev["date_source"], "Timing": ev["timing"],
                                 "Exp_Median_Pct": row.get("Exp_Median_Pct", ""),
                                 "Exp_Worst_Pct": row.get("Exp_Worst_Pct", ""),
                                 "Sample_N": row.get("Sample_N", ""),
                                 "_move": move, "_event": ev, "_pending": True}
                continue

            hist = hist_cache.get(tk)
            px = float(hist["Close"].iloc[-1]) if (hist is not None and not hist.empty) else None
            if px is None:
                hist = hist_cache[tk] = fmp_price_history(tk)
                px = float(hist["Close"].iloc[-1]) if (hist is not None and not hist.empty) else None
            srow = ec.snapshot_row(ev, move, price=px, now_et=now_et)
            new_rows.append(srow)
            rec = {c: srow[i] for i, c in enumerate(ec.EVENTS_COLS)}
            rec["_move"] = move; rec["_event"] = ev
            snapshots[tk] = rec
            print(f"  [SNAP] {tk} D-{dd} {ed}({ev['date_source']}) "
                  f"±{move.get('median_pct')}% n={move.get('sample_n')}")
        except Exception as e:
            print(f"  [WARN] {tk} 스냅샷 실패: {e}")
    if n_univ:
        print(f"  [SNAP] Tier 2 유니버스 {n_univ}종목 제외(일정 전용)")
    return new_rows, snapshots


# ── 패스 2·3: 사후 측정 / 지연 ────────────────────────────────────────────
def pass_verify(rows, hist_cache, today):
    """반응일 종가가 확정된 행의 갭·PEAD 측정. 반환: (updates, results)
       updates: [(row_index, {col: value})]"""
    updates, results = [], []
    for r in rows:
        try:
            if str(r.get("Gap_Pct") or "").strip():
                continue                      # 이미 측정 완료
            d = ec._d(r.get("Earnings_Date"))
            if d is None or d > pd.Timestamp(today):
                continue
            tk = str(r.get("Ticker") or "").strip().upper()
            if not tk:
                continue
            hist = hist_cache.get(tk)
            if hist is None:
                hist = hist_cache[tk] = fmp_price_history(tk)
            if hist is None or hist.empty:
                continue

            m = ec.measure_reaction(hist, r.get("Earnings_Date"), r.get("Timing", ""))
            if not m["ok"]:
                continue
            # 반응일이 아직 오늘 이후면 종가 미확정 → 다음 실행으로 미룬다
            rd = ec._d(m["reaction_date"])
            if rd is None or rd > pd.Timestamp(today):
                continue

            move = {"ok": True, "median_pct": ec._num(r.get("Exp_Median_Pct")),
                    "worst_down_pct": ec._num(r.get("Exp_Worst_Pct"))}
            if move["median_pct"] is None:
                move["ok"] = False
            pead = ec.evaluate_pead(m, move)
            hit = ec.score_prediction(r.get("Pred_Direction"), m["gap_pct"])

            patch = {
                "Gap_Pct": m["gap_pct"],
                "Volume_Ratio": ("" if m["volume_ratio"] is None else m["volume_ratio"]),
                "Gap_Held": ("" if m["gap_held"] is None else ("TRUE" if m["gap_held"] else "FALSE")),
                "PEAD_Verdict": pead["code"],
                "Pred_Hit": hit,
                "Verified_At": datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET"),
            }
            updates.append((r["_row"], patch))
            results.append({**r, **patch, "_pead": pead, "_reaction": m})
            print(f"  [VERIFY] {tk} 갭 {m['gap_pct']:+.1f}% → {pead['code']}")
        except Exception as e:
            print(f"  [WARN] 측정 실패({r.get('Ticker')}): {e}")
    return updates, results


def pass_delayed(rows, hist_cache, today):
    """발표 D+5 도달 행의 D5_Return_Pct + 사전 종가 기준 수익률 3열.

    두 축을 함께 채운다. 콜 추가는 없다 — 같은 hist 와 같은 반응일 인덱스를
    이미 손에 쥐고 있다.

      · D5_Return_Pct   기준 = **반응일 종가** (기존)
      · Pre_Ret_DN_Pct  기준 = **발표 전날 종가** = close[i-1]
                        (갭 계산의 분모와 동일 — 발표 전에 들고 들어간 사람의 손익)

    D+N 은 '반응일부터 N거래일 보유'로 센다. 따라서 Pre_Ret_D1 은 정의상
    Gap_Pct 와 같은 값이 된다. 중복이 아니라 **무료 정합성 검사**로 쓴다 —
    두 값이 어긋나면 반응일 판정이나 가격 이력 중 하나가 틀린 것이다.
    """
    updates = []
    for r in rows:
        try:
            has_d5 = bool(str(r.get("D5_Return_Pct") or "").strip())
            has_pre = bool(str(r.get("Pre_Ret_D7_Pct") or "").strip())
            if has_d5 and has_pre:
                continue
            if not str(r.get("Gap_Pct") or "").strip():
                continue
            d = ec._d(r.get("Earnings_Date"))
            if d is None or (pd.Timestamp(today) - d).days < 7:
                continue
            tk = str(r.get("Ticker") or "").strip().upper()
            hist = hist_cache.get(tk)
            if hist is None:
                hist = hist_cache[tk] = fmp_price_history(tk)
            if hist is None or hist.empty:
                continue
            i = ec.resolve_reaction_index(hist, r.get("Earnings_Date"), r.get("Timing", ""))
            if i is None or i < 1:
                continue
            patch = {}

            if not has_d5 and i + 5 < len(hist):
                base = float(hist["Close"].iloc[i])
                if base > 0:
                    end = float(hist["Close"].iloc[i + 5])
                    patch["D5_Return_Pct"] = round((end - base) / base * 100.0, 2)

            if not has_pre:
                pre = float(hist["Close"].iloc[i - 1])
                if pre > 0:
                    # D+7 까지 전부 확정된 뒤에 한 번에 쓴다. 부분 기록을 남기면
                    # has_pre 검사(D7 열 기준)가 통과해 D1/D3 만 채운 채 굳는다.
                    if i + 6 < len(hist):
                        for col, off in (("Pre_Ret_D1_Pct", 0),
                                         ("Pre_Ret_D3_Pct", 2),
                                         ("Pre_Ret_D7_Pct", 6)):
                            end = float(hist["Close"].iloc[i + off])
                            patch[col] = round((end - pre) / pre * 100.0, 2)

            if patch:
                updates.append((r["_row"], patch))
        except Exception as e:
            print(f"  [WARN] D+5 실패({r.get('Ticker')}): {e}")
    return updates


# ── 패스 P: 실적 프리뷰 브리핑 (2단계) ────────────────────────────────────
def pass_preview(cal, prev_rows, hist_cache, today, now_et, spy_hist=None):
    """D-7 / D-3 / 최종 스냅샷. Tier 1(보유+워치리스트)만.

    스냅샷당 FMP 4콜:
        earnings?symbol=        A블록(EPS·매출 컨센서스) + B블록(beat·서프라이즈)
        price-target-consensus  목표주가
        grades-historical       의견 수준·90일 변화
        news/stock?symbols=     뉴스 증분
    가격 이력은 hist_cache 재사용(0콜), SPY 는 실행당 1회.

    append-only. 이미 있는 (Event_ID, Phase) 는 절대 다시 쓰지 않는다 —
    "그때 본 숫자"가 보존돼야 사후 대조가 의미를 갖는다.
    """
    new_rows = []
    idx = ec.preview_index(prev_rows)
    n_univ = n_skip = 0

    for tk, row in (cal or {}).items():
        try:
            if ec.is_universe_only(row):
                n_univ += 1
                continue
            ed = str(row.get("Earnings_Date") or "")
            dd = ec.days_until_from_row(row, today)
            timing = str(row.get("Timing") or "")
            phase = ec.preview_phase(dd, timing)
            if not phase:
                continue
            eid = ec.event_id(tk, ed)
            if (eid, phase) in idx:
                n_skip += 1
                continue

            hist = hist_cache.get(tk)
            if hist is None or hist.empty:
                hist = hist_cache[tk] = fmp_price_history(tk)
            px = (float(hist["Close"].iloc[-1])
                  if (hist is not None and not hist.empty) else None)

            flags = []
            m = {"price": px}
            mv = ec.move_from_row(row)
            m["exp_median_pct"] = mv.get("median_pct")
            m["exp_worst_pct"] = mv.get("worst_down_pct")
            if m["exp_median_pct"] is None:
                flags.append("no_expected_move")

            # ── 콜 1: A블록 + B블록 원천 ──
            recs = ec.fetch_earnings_records(tk, key=FMP_API_KEY)
            fut, past = ec.split_future_past(recs, today)
            if fut is None:
                flags.append("no_estimate")
            else:
                m["est_eps"] = fut.get("eps_est")
                m["est_revenue"] = fut.get("rev_est")
                if m["est_revenue"] is None:
                    flags.append("no_revenue_est")
                py = ec.prior_year_quarter(past, fut.get("date"))
                if py is None:
                    flags.append("no_yoy_base")
                else:
                    m["est_eps_yoy_pct"] = ec.yoy_pct(fut.get("eps_est"), py.get("eps_act"))
                    m["est_revenue_yoy_pct"] = ec.yoy_pct(fut.get("rev_est"), py.get("rev_act"))

            bs = ec.beat_stats(past)
            m["sample_n_q"] = bs.get("sample_n") or 0
            if bs.get("ok"):
                m["beat_rate_pct"] = bs.get("beat_rate_pct")
                m["surprise_avg_pct"] = bs.get("surprise_avg_pct")
            else:
                flags.append("few_quarters")

            # 리비전은 캘린더가 이미 축적 중이다 — 재계산하지 않는다(SSOT).
            rev = ec._num(row.get("Est_Revision_Pct"))
            if rev is None:
                flags.append("no_revision")
            else:
                m["est_revision_pct"] = rev

            # ── 콜 2: 목표주가 ──
            tgt = ec.fetch_price_target(tk, key=FMP_API_KEY)
            if tgt.get("mean") is None:
                flags.append("no_target")
            else:
                m["target_mean"] = tgt.get("mean")
                m["target_upside_pct"] = ec.target_upside_pct(tgt.get("mean"), px)

            # ── 콜 3: 매수의견 ──
            gs = ec.fetch_grade_series(tk, key=FMP_API_KEY)
            drift, level = ec.grade_factors(gs, today)
            if level is None:
                flags.append("no_grades")
            else:
                m["grade_buy_pct"] = level
                if drift is None:
                    flags.append("no_grade_drift")
                else:
                    m["grade_drift_90d"] = drift

            # ── 상대강도 (0콜) ──
            rs = ec.rel_strength_pct(hist, spy_hist)
            if rs is None:
                flags.append("no_rs")
            else:
                m["rs_20d_pct"] = rs
                if spy_hist is None or spy_hist.empty:
                    flags.append("rs_absolute")   # SPY 미확보 → 절대 수익률

            # ── 콜 4: 뉴스 증분 ──
            since = ec.last_snapshot_at(prev_rows, eid)
            news = ec.fetch_stock_news(tk, key=FMP_API_KEY)
            cnt, njson = ec.news_digest(news, since=since)
            m["news_count"], m["news_json"] = cnt, njson

            m["flags"] = flags
            ev = {"ticker": tk, "earnings_date": ed, "days_until": dd, "timing": timing}
            new_rows.append(ec.preview_row(ev, phase, m, now_et=now_et))
            print(f"  [PREVIEW] {tk} {phase} D-{dd} {ed} "
                  f"EPS={m.get('est_eps')} 목표대비={_fmt1(m.get('target_upside_pct'))}% "
                  f"RS={_fmt1(m.get('rs_20d_pct'))}%p 뉴스{cnt}건"
                  + (f" ⚠️{','.join(flags)}" if flags else ""))
        except Exception as e:
            print(f"  [WARN] {tk} 프리뷰 실패: {e}")
    if n_univ or n_skip:
        print(f"  [PREVIEW] 유니버스 제외 {n_univ} · 이미 기록됨 {n_skip}")
    return new_rows


def _fmt1(v):
    n = ec._num(v)
    return "—" if n is None else f"{n:+.1f}"


# ── 수신자별 판정 (계좌 프로필 반영) ──────────────────────────────────────
def build_user_report(uid, holdings, watch, snapshots, results, prof_cache, hist_cache,
                      core_keys=None, wl_stops=None):
    """수신자 1명분 리포트. 계좌별 축소 판정은 여기서 런타임 계산한다."""
    profiles = load_profiles(uid, prof_cache)
    pre, blocked, post = [], [], []

    # 보유 — 축소 판정
    for acct, rows in (holdings.get(uid) or {}).items():
        prof = ac.get_profile(profiles, acct)
        cap = ac.resolve_earn_trim_cap(prof)
        price_map = {}
        for h in rows:
            hist = hist_cache.get(h["ticker"])
            if hist is not None and not hist.empty:
                price_map[h["ticker"]] = float(hist["Close"].iloc[-1])
        eqc = ac.compute_equity(
            [(h["ticker"], h["qty"], h["avg"]) for h in rows], price_map, prof["Cash"])
        equity = eqc["equity"]
        for h in rows:
            snap = snapshots.get(h["ticker"])
            if not snap:
                continue
            px = price_map.get(h["ticker"]) or h["avg"]
            if not px:
                continue
            move = snap.get("_move") or {
                "ok": ec._num(snap.get("Exp_Median_Pct")) is not None,
                "median_pct": ec._num(snap.get("Exp_Median_Pct")),
                "worst_down_pct": ec._num(snap.get("Exp_Worst_Pct")),
            }
            t = ec.evaluate_trim(px * h["qty"], equity, move,
                                 trim_cap_pct=cap["cap_pct"],
                                 min_trade_dollars=prof["Min_Trade_Dollars"],
                                 is_core=((str(uid).lower(), acct.lower(), h["ticker"])
                                          in (core_keys or set())))
            pre.append({"ticker": h["ticker"], "account": acct, "snap": snap,
                        "trim": t, "cap": cap, "move": move})

    # 워치리스트 — 진입 차단
    wl_stops = wl_stops or {}
    for tk, acct in (watch.get(uid) or {}).items():
        snap = snapshots.get(tk)
        if not snap:
            continue
        move = snap.get("_move") or {
            "ok": ec._num(snap.get("Exp_Median_Pct")) is not None,
            "median_pct": ec._num(snap.get("Exp_Median_Pct")),
        }
        ev = snap.get("_event") or {}
        dd = ev.get("days_until")
        if dd is None:
            d = ec._d(snap.get("Earnings_Date"))
            dd = int((d - pd.Timestamp.today().normalize()).days) if d is not None else None
        # 손절폭: 워치리스트 수동 손절 우선, 없으면 ATR 추정(app.py 와 동일 규약)
        stop_pct, stop_src = None, ""
        hist = hist_cache.get(tk)
        px = None
        if hist is not None and not hist.empty:
            px = float(hist["Close"].iloc[-1])
        try:
            sl = float(wl_stops.get((uid, tk)) or 0.0)
            if sl > 0 and px and px > 0:
                stop_pct, stop_src = abs((px - sl) / px * 100.0), "manual"
        except (TypeError, ValueError):
            stop_pct = None
        if stop_pct is None and hist is not None and not hist.empty:
            stop_pct = ec.derived_stop_pct(hist, price=px)
            stop_src = "atr" if stop_pct is not None else ""
        g = ec.evaluate_entry_gate(move, planned_stop_pct=stop_pct, days_until=dd,
                                   earnings_date=snap.get("Earnings_Date"),
                                   stop_source=stop_src)
        if g["blocked"]:
            blocked.append({"ticker": tk, "account": acct, "gate": g, "snap": snap})

    # 사후 — 본인 관심 종목만
    mine = set((watch.get(uid) or {}).keys())
    for acct_rows in (holdings.get(uid) or {}).values():
        mine |= {h["ticker"] for h in acct_rows}
    for r in results:
        if str(r.get("Ticker") or "").upper() in mine:
            post.append(r)

    order = {"trim_hard": 0, "trim": 1, "core": 3, "disabled": 4, "na": 5, "hold": 6}
    pre.sort(key=lambda x: (order.get(x["trim"]["code"], 9), -(x["trim"]["position_value"] or 0)))
    return {"pre": pre, "blocked": blocked, "post": post}


# ── 이메일 ────────────────────────────────────────────────────────────────
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_email(rep: dict, today_str: str) -> tuple:
    act = [p for p in rep["pre"] if p["trim"]["code"] in ("trim", "trim_hard")]
    hold = [p for p in rep["pre"] if p["trim"]["code"] not in ("trim", "trim_hard")]
    n_act = len(act) + len(rep["blocked"])
    subj = f"📅 실적 레이더 {today_str} — 조치 {n_act}건"

    h = ["<div style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
         "max-width:640px;margin:0 auto;color:#1a1a1a'>",
         f"<h2 style='margin:0 0 4px'>📅 실적 레이더</h2>"
         f"<div style='color:#666;font-size:13px;margin-bottom:16px'>{today_str} · "
         f"D-{ec.SNAPSHOT_DAYS} 사전 경고 + 발표 후 결과</div>"]

    if not (act or rep["blocked"] or rep["post"] or hold):
        h.append("<p>오늘 조치가 필요한 실적 이벤트가 없습니다.</p></div>")
        return subj, "".join(h)

    for p in act:
        t, s = p["trim"], p["snap"]
        color = "#c0392b" if t["code"] == "trim_hard" else "#e67e22"
        mv = ec._num(s.get("Exp_Median_Pct"))
        wv = ec._num(s.get("Exp_Worst_Pct"))
        h.append(
            f"<div style='border-left:4px solid {color};background:#fdf6f3;padding:12px 14px;"
            f"margin:0 0 12px;border-radius:4px'>"
            f"<div style='font-size:17px;font-weight:700'>{_esc(t['label'])} — "
            f"{_esc(p['ticker'])}</div>"
            f"<div style='color:#555;font-size:13px;margin:2px 0 10px'>"
            f"{_esc(s.get('Earnings_Date'))}"
            f"({_esc(ec.DATE_SOURCE_LABELS.get(s.get('Date_Source'), ''))}) · "
            f"{_esc(ec.TIMING_LABELS.get(s.get('Timing'), ''))} · {_esc(p['account'])}</div>"
            f"<div style='font-size:15px'><b>약 ${t['sell_dollars']:,.0f} 매도</b> "
            f"— 비중 {t['position_pct']:.1f}% → {t['target_pct']:.1f}%</div>"
            f"<div style='color:#444;font-size:13px;margin-top:8px'>"
            f"<b>왜:</b> {_esc(t['reason'])}<br>"
            f"통상 ±{mv:.1f}%" + (f" · 최악 −{wv:.1f}%" if wv else "") +
            f" · 표본 {_esc(s.get('Sample_N'))}분기</div></div>")

    for b in rep["blocked"]:
        h.append(
            f"<div style='border-left:4px solid #7f8c8d;background:#f4f6f6;padding:12px 14px;"
            f"margin:0 0 12px;border-radius:4px'>"
            f"<div style='font-size:17px;font-weight:700'>⛔ {_esc(b['ticker'])} — 매수 보류</div>"
            f"<div style='color:#555;font-size:13px;margin:2px 0 8px'>"
            f"{_esc(b['snap'].get('Earnings_Date'))} · 워치리스트</div>"
            f"<div style='color:#444;font-size:13px'>{_esc(b['gate']['reason'])}<br>"
            f"발표 결과 확인 후 재평가합니다.</div></div>")

    if rep["post"]:
        h.append("<h3 style='margin:20px 0 8px;font-size:15px'>발표 완료</h3>")
        for r in rep["post"]:
            pead = r.get("_pead") or {}
            gp = ec._num(r.get("Gap_Pct")) or 0.0
            h.append(
                f"<div style='border:1px solid #e0e0e0;padding:10px 12px;margin:0 0 8px;"
                f"border-radius:4px'><b>{_esc(r.get('Ticker'))}</b> "
                f"갭 {gp:+.1f}% · {_esc(pead.get('label', ''))}"
                f"<div style='color:#666;font-size:12px;margin-top:4px'>"
                f"{_esc(' · '.join(pead.get('reasons', [])))}</div></div>")

    if hold:
        names = ", ".join(f"{p['ticker']}({p['trim']['label'].split()[-1]})" for p in hold)
        h.append(f"<div style='color:#666;font-size:12px;margin-top:14px'>"
                 f"조치 없음: {_esc(names)}</div>")

    h.append("<div style='color:#999;font-size:11px;margin-top:20px;border-top:1px solid #eee;"
             "padding-top:10px'>이 메일은 매수·매도 신호가 아니라 <b>이벤트 리스크 제약</b>입니다. "
             "매수는 레짐·타이밍·R:R 게이트가, 매도는 스윙/포지션 신호가 결정합니다.<br>"
             "본 정보는 참고용이며 투자 권유가 아닙니다.</div></div>")
    return subj, "".join(h)


def send_mail(to_addr, subject, html) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = str(to_addr or GMAIL_TO).strip()
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"[ERROR] 메일 발송 실패({to_addr}): {e}")
        return False


# ── main ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"실적 레이더 시작 — {datetime.now(_ET):%Y-%m-%d %H:%M ET}")
    print("=" * 60)
    if not is_market_open_today():
        print("[SKIP] 휴장일 — 종료")
        return

    today = pd.Timestamp(datetime.now(_ET).date())
    today_str = today.strftime("%Y-%m-%d")
    now_et = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")

    ews = _events_ws()
    rows = ec.parse_events(gsr.call(ews.get_all_values, _label="Earnings_Events") or [])
    existing = {str(r.get("Event_ID") or ""): r for r in rows}
    cws = _calendar_ws()
    _cal_vals = gsr.call(cws.get_all_values, _label="Earnings_Calendar") or []
    cal = ec.parse_calendar(_cal_vals)
    _cal_next_row = max(len(_cal_vals), 1) + 1   # 헤더 포함 마지막 행 다음
    print(f"[INFO] 기존 이벤트 {len(rows)}건 · 캘린더 {len(cal)}종목")

    holdings, watch, tickers, wl_stops = load_universe()
    user_set = set(tickers)
    print(f"[INFO] Tier 1 티커 {len(user_set)}개 "
          f"(보유 {len(holdings)}명 · 워치 {len(watch)}명)")

    # ── Tier 2: 대형주 유니버스 (일정 지형 전용) ──
    print("\n▶ 패스 U: 유니버스 갱신")
    univ = pass_universe(today, now_et, force=FORCE_UNIVERSE)
    univ_only = sorted(set(univ) - user_set)
    # Tier 1 을 먼저 처리한다 — 타임아웃이 나더라도 사용자 종목은 갱신되게.
    scan_tickers = sorted(user_set) + univ_only
    print(f"[INFO] Tier 2 유니버스 {len(univ)}종목 (Tier 1 중복 제외 {len(univ_only)})"
          f" → 캘린더 대상 총 {len(scan_tickers)}종목")

    hist_cache = {}

    # ── 시장 전체 캘린더 1회 조회 ──
    #   timing(bmo/amc) 의 주 공급원. per-symbol earnings?symbol= 에는 time 필드가
    #   없고 quote.earningsAnnouncement 도 stable 에서 오지 않는 것으로 보인다.
    market_map, _mc_diag = ec.fetch_market_calendar_map(today=today, key=FMP_API_KEY)
    print(f"[TIME] {_mc_diag}")

    print("\n▶ 패스 0: 캘린더 갱신")
    if FORCE_CALENDAR:
        print("[CAL] 강제 재조회(FORCE_CALENDAR) — 티어 주기 무시")
    c_updates, c_appends, cal = pass_calendar(scan_tickers, cal, hist_cache,
                                              today, now_et, user_set=user_set,
                                              force=FORCE_CALENDAR,
                                              market_map=market_map)
    print("\n▶ 패스 2: 사후 측정")
    v_updates, results = pass_verify(rows, hist_cache, today)
    print("\n▶ 패스 3: D+5 지연")
    d_updates = pass_delayed(rows, hist_cache, today)
    print("\n▶ 패스 1: 사전 스냅샷")
    new_rows, snapshots = pass_snapshot(cal, existing, hist_cache, today, now_et)

    # ── 패스 P: 프리뷰 브리핑 ──
    #   패스 0 다음에 둔다 — 캘린더가 갱신된 뒤라야 D-N 판정이 오늘 기준이 된다.
    #   메일 발송보다 앞에 두는 이유는, 여기서 터져도 기존 사전 경고 메일은
    #   나가야 하기 때문이다(프리뷰는 부가 기능이지 게이트가 아니다).
    print("\n▶ 패스 P: 프리뷰 브리핑")
    p_rows, prev_rows, _pws = [], [], None
    try:
        _pws = _preview_ws()
        _pv_vals = gsr.call(_pws.get_all_values, _label="Earnings_Preview") or []
        prev_rows = ec.parse_preview(_pv_vals)
        _pv_next_row = max(len(_pv_vals), 1) + 1
        spy_hist = hist_cache.get("SPY")
        if spy_hist is None or spy_hist.empty:
            spy_hist = hist_cache["SPY"] = fmp_price_history("SPY")
        p_rows = pass_preview(cal, prev_rows, hist_cache, today, now_et,
                              spy_hist=spy_hist)
        if p_rows:
            _safe_update(_pws, p_rows, _pv_next_row, ec.PREVIEW_NCOL)
            print(f"[OK] 프리뷰 스냅샷 {len(p_rows)}건 저장")
    except Exception as e:
        print(f"[ERROR] 프리뷰 실패(사전 경고는 계속 진행): {e}")
        traceback.print_exc()

    # ── 캘린더 기록: 갱신 먼저, 추가는 마지막 ──
    if c_updates:
        _n_api = _batch_update(cws, c_updates, ec.CALENDAR_NCOL, label="calendar")
        print(f"[OK] 캘린더 {len(c_updates)}행 갱신 "
              f"({len(_merge_runs(c_updates))}구간 · API {_n_api}콜)")
    if c_appends:
        try:
            _safe_update(cws, c_appends, _cal_next_row, ec.CALENDAR_NCOL)
            print(f"[OK] 캘린더 신규 {len(c_appends)}종목 추가")
        except Exception as e:
            print(f"[ERROR] 캘린더 추가 실패: {e}")

    # ── 시트 기록: 갱신 먼저, 추가는 마지막 (부분 실패 시 재시도 가능) ──
    idx = {c: i for i, c in enumerate(ec.EVENTS_COLS)}
    _ev_updates = []
    for row_i, patch in (v_updates + d_updates):
        try:
            cur = next((r for r in rows if r["_row"] == row_i), None)
            if cur is None:
                continue
            vals = [cur.get(c, "") for c in ec.EVENTS_COLS]
            for c, v in patch.items():
                vals[idx[c]] = v
            _ev_updates.append((row_i, vals))
        except Exception as e:
            print(f"[ERROR] 행 {row_i} 조립 실패: {e}")
    if _ev_updates:
        _n_api = _batch_update(ews, _ev_updates, ec.EVENTS_NCOL, label="events")
        print(f"[OK] 이벤트 {len(_ev_updates)}행 갱신 (API {_n_api}콜)")
    if new_rows:
        try:
            _safe_update(ews, new_rows, len(rows) + 2, ec.EVENTS_NCOL)
            print(f"[OK] 신규 스냅샷 {len(new_rows)}건 저장")
        except Exception as e:
            print(f"[ERROR] 스냅샷 저장 실패: {e}")

    # ── 메일 ──
    print("\n▶ 발송")
    try:
        uws = _ws(_USERS_WORKSHEET, uc.USER_SHEET_COLS)
        uc.ensure_users_header_v4(uws)
        rcpts = uc.get_recipients(uws, "earnings", admin_fallback_email=GMAIL_TO)
    except Exception as e:
        print(f"[WARN] 수신자 조회 실패 — 관리자에게만 발송: {e}")
        rcpts = [(uc.ADMIN_CONTENT_OWNER_ID, GMAIL_TO)]

    core_keys = load_core_keys()
    prof_cache, sent = {}, 0
    for uid, email in (rcpts or []):
        try:
            rep = build_user_report(uid, holdings, watch, snapshots, results,
                                    prof_cache, hist_cache, core_keys=core_keys,
                                    wl_stops=wl_stops)
            if not (rep["pre"] or rep["blocked"] or rep["post"]):
                print(f"  [SKIP] {uid} — 해당 이벤트 없음")
                continue
            subj, html = render_email(rep, today_str)
            to = None if str(uid).lower() == uc.ADMIN_CONTENT_OWNER_ID else email
            if send_mail(to or GMAIL_TO, subj, html):
                sent += 1
                print(f"  [SENT] {uid} → {to or GMAIL_TO}")
        except Exception as e:
            print(f"  [ERROR] {uid} 리포트 실패: {e}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"완료 — 캘린더 {len(c_updates)}갱신/{len(c_appends)}신규 · "
          f"스냅샷 {len(new_rows)} · 프리뷰 {len(p_rows)} · 측정 {len(v_updates)} · "
          f"D+5 {len(d_updates)} · 메일 {sent}통")
    print(gsr.stats_line())
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
