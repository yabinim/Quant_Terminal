"""
run_hidden_alpha.py
───────────────────
GitHub Actions 자동 실행: Hidden Alpha Radar 자동화 (주 1회)

흐름:
  [STEP 1] 신규 ETF 발견 → ETF_Universe 시트에 자동 추가
           (삭제/정리는 앱의 cleanup 기능으로 수동 실행 — 여기선 추가만)
  [STEP 2] ETF_Universe 시트에 '누적된 전체' 유니버스를 로드
  [STEP 3] 1주(5거래일)·1개월(21거래일) 수익률 계산
  [STEP 4] 각 지표를 백분위(0~100)로 정규화 후 가중합 점수화
           composite = 0.7 × 1개월백분위 + 0.3 × 1주백분위   (raw % 직접 합산 금지)
  [STEP 5] 점수 내림차순 Top 10 + 지난주 대비 순위 변화(Δ) 산출
  [STEP 6] 이메일 발송 (맨 위 액션 요약 → Top 10 표 → 규칙 리마인더)
  [STEP 7] 이번 주 순위 스냅샷 저장 (다음 주 Δ 계산용, 1셀 JSON 자동 덮어쓰기)

매매 규칙(사용자가 직접 로빈후드에서 집행):
  · Top 5 진입 종목 = 매수 후보
  · Top 5 밖으로 밀려난 보유 종목 = 매도
  · 집행은 월요일 10시(ET) 이후

실행 주기: 주 1회 (기존 '주말 5PM' 워크플로에 합류, 일요일에만 발송)
"""

import os
import sys
import json
import time
import smtplib
import traceback
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import pytz
import gspread
from google.oauth2.service_account import Credentials

# ── 🛰️ 위성 섹터 Top10 SSOT (fmp_extras) — 리포지토리 루트에서 임포트 ──
# 실행 위치와 무관하게 동작하도록 스크립트의 부모(리포 루트)와 자기 폴더를 sys.path 에 추가.
# fmp_extras(레이트 리미터) · users_core(수신자)는 필수 의존이다.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)
# ⚠️ fmp_extras 는 이제 선택이 아니다 — 모든 FMP 호출이 fx.fmp_get(레이트 리미터)을
#    경유하므로 import 가 실패하면 가격 조회 자체가 불가능하다. 조용히 넘어가면
#    "랭킹 산출 실패"만 남고 원인을 알 수 없으므로 즉시 중단한다.
import fmp_extras as fx
import users_core as uc
# rotation_core: 유동성·중복·크립토 캡 게이트 SSOT (app.py 와 공용).
# fmp_extras 와 같은 이유로 선택이 아니다 — 이게 없으면 게이트 없는 옛 동작으로
# 조용히 되돌아간다. 조용한 완화는 조용한 손실이 된다.
import rotation_core as rc

# ── 환경변수 (기존 run_*.py와 동일 시크릿) ────────────────────────────────────
FMP_API_KEY        = os.environ["FMP_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]
GSPREAD_KEY_JSON   = os.environ["GSPREAD_KEY"]
_gcp_info          = json.loads(GSPREAD_KEY_JSON)

# 수동 테스트(workflow_dispatch)에서 요일 가드를 무시하려면 1로 설정
_FORCE_RUN = str(os.environ.get("HIDDEN_ALPHA_FORCE", "")).strip() in ("1", "true", "TRUE")

# ── 상수 ───────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")

_SPREADSHEET_TITLE        = "Quant_DB"
_ETF_UNIVERSE_SHEET_TITLE = "ETF_Universe"
_ETF_UNIVERSE_SHEET_COLS  = ["Ticker", "Name", "Category", "AUM_M", "Added_Date", "Source"]
_SNAPSHOT_SHEET_TITLE     = "HiddenAlpha_Snapshot"   # 지난주 순위 스냅샷(1셀 JSON)

_FMP_BASE    = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 8

# 점수화 가중치 (app.py 'Ryan's Alpha Strategy'와 동일 철학: 1개월 우위)
_W_MONTH = 0.7
_W_WEEK  = 0.3

# 이메일 표는 **게이트가 판정한 범위와 같아야 한다**.
# [2026-09-02 실측] 판정 대상 15개 중 12개가 제외되어 슬롯 3개(GDXJ 6위 ·
#   USO · ARKB)가 남았는데, 뒤 둘은 11위 밖이라 Top 10 표에서 **사라졌다**.
#   실제 매수 대상이 이메일에 안 보이는 상태였다.
# 16위 밖으로 늘리지 않는 이유: 게이트가 판정하지 않은 구간이라 ⭐ 도 🚫 도
#   아닌 행이 생긴다 — 없는 판정을 있는 것처럼 보이게 한다.
# 그래서 숫자를 따로 두지 않고 _VERIFY_TOP_N 에 **묶는다**. 판정 범위가 바뀌면
#   표도 따라간다. 두 벌로 두면 오늘과 같은 어긋남이 다시 난다.
# 보유 슬롯 수는 rotation_core 가 소유한다 — 여기에 5 를 다시 쓰면 두 벌이 되고,
# 한쪽만 바뀌어도 아무도 모른다. 상수를 복사하지 않는다.
_HOLD_SLOTS  = rc.HOLD_SLOTS

# 발견(신규 ETF 추가) 파라미터 — app.run_etf_auto_update_if_needed와 동일값
_DISCOVERY_LOOKBACK_DAYS = 90
_DISCOVERY_MIN_AUM_M     = 50.0

# 주 1회 보장: '이번 ISO 주(월~일)'에 이미 발송했으면 스킵한다.
# 트리거 요일에 의존하지 않으므로 주말 워크플로가 토·일 모두 돌아도 1회만 발송되고,
# 다음 주(새 ISO 주)에는 정상 발송된다. (스냅샷 저장일 기준으로 판단)


# ── Google Sheets 클라이언트 (기존 스크립트와 동일) ───────────────────────────
def get_gspread_client():
    creds = Credentials.from_service_account_info(_gcp_info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)


def _safe_append_rows(ws, rows, value_input_option: str = "USER_ENTERED") -> None:
    """append_row의 '계단식 드리프트' 버그 회피 — 항상 A열 기준 마지막 다음 행에 기록.
    (app.py._safe_append_rows 동일 로직)"""
    if rows is None:
        return
    if len(rows) > 0 and not isinstance(rows[0], (list, tuple)):
        rows = [rows]
    rows = [list(r) for r in rows if r is not None]
    if not rows:
        return
    existing = ws.get_all_values() or []
    last_row = 0
    for idx, r in enumerate(existing, start=1):
        if any(str(c).strip() != "" for c in r):
            last_row = idx
    start_row = last_row + 1
    end_row = start_row + len(rows) - 1
    try:
        if end_row > ws.row_count:
            ws.add_rows(end_row - ws.row_count + 50)
    except Exception:
        pass
    last_col_letter = chr(ord("A") + max(0, len(_ETF_UNIVERSE_SHEET_COLS) - 1))
    ws.update(rows, range_name=f"A{start_row}:{last_col_letter}{end_row}",
              value_input_option=value_input_option)


def open_etf_universe_worksheet(gc):
    """Quant_DB의 ETF_Universe 탭. 없으면 생성. (app.py 동일)"""
    sh = gc.open(_SPREADSHEET_TITLE)
    titles = [ws.title for ws in sh.worksheets()]
    if _ETF_UNIVERSE_SHEET_TITLE in titles:
        return sh.worksheet(_ETF_UNIVERSE_SHEET_TITLE)
    ws = sh.add_worksheet(title=_ETF_UNIVERSE_SHEET_TITLE, rows=3000, cols=6)
    ws.update([_ETF_UNIVERSE_SHEET_COLS], range_name="A1:F1", value_input_option="USER_ENTERED")
    return ws


def load_universe_tickers(ws, etf_names: dict | None = None) -> tuple[list, dict]:
    """ETF_Universe 시트에서 누적 티커 로드 (헤더 제외, 중복 제거)
    + 로테이션 정책 필터 (fmp_extras SSOT: 인버스/레버리지 제외).

    필터 후 남는 티커로만 랭킹하므로 Top 5 는 자동으로 다음 순위가 채운다.

    반환: (유지 티커 목록, {티커: 이름})

    [2026-09-01] 반환에 이름 맵을 추가했다. 하류(A-2 재검증·크립토 판정)가
      이름을 필요로 하는데, 예전엔 이 함수가 이름을 버리고 티커만 돌려줬다.
    """
    try:
        vals = ws.get_all_values()
        if not vals or len(vals) < 2:
            return [], {}
        pairs, seen = [], set()
        for r in vals[1:]:
            row = r + ["", ""]
            tk = str(row[0]).strip().upper()
            nm = str(row[1]).strip()
            if tk and tk not in seen:
                seen.add(tk)
                pairs.append((tk, nm))
        # 이름 우선순위: etf-list > 시트 B열.
        # 시트 B열은 과거에 빈 값으로 저장된 행이 많다(SPAX 사고 경로).
        # etf-list 이름을 덮어씌워 **이름 없는 판정을 없앤다** — API 콜 추가 0.
        name_map = dict(etf_names or {})
        merged = []
        for tk, nm in pairs:
            merged.append((tk, name_map.get(tk, "") or nm))
        kept, excluded = fx.filter_rotation_universe(merged)
        if excluded:
            print(f"[INFO] 로테이션 정책 제외 {len(excluded)}개 (인버스/레버리지): "
                  + ", ".join(excluded[:12])
                  + (" ..." if len(excluded) > 12 else ""))
        names = {tk: nm for tk, nm in merged}
        return kept, names
    except Exception:
        return [], {}


# ── FMP 헬퍼 (app.py 로직에서 st.cache_data·st.secrets만 제거) ────────────────
def _fmp_price_history_ohlcv(ticker: str, bars: int) -> tuple[pd.Series, pd.Series]:
    """historical-price-eod → (Close, Volume) 시리즈 쌍.

    [2026-09-01] Close 단일 반환에서 (Close, Volume) 쌍으로 바꿨다. 유동성
      게이트가 달러거래대금(종가×거래량)을 요구하는데, 이 응답에 volume 이
      이미 들어 있다 — **API 콜 추가 0.**

    bars: 하류가 실제로 소비하는 꼬리 깊이. **기본값을 두지 않는다** —
      호출부가 자기 요구를 밝히지 않으면 TypeError 로 즉시 죽는 편이,
      어느 요구인지 모르는 창이 조용히 생기는 것보다 낫다.

    [2026-08-28] `limit=130` → `from`/`to` 창. FMP 는 이 엔드포인트의 `limit` 을
      **무시**하므로 실제로는 1,254봉을 받고 있었다. 옛 130 은 검증된 적 없는
      숫자라 요구의 근거로 쓰지 않았다 — `calculate_period_return(s, 21)` 이
      `iloc[-(21+1)]` 를 읽으므로 요구는 22봉이다.
    """
    try:
        r = fx.fmp_get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker}"
            + fx.hist_range_params(fx.hist_days_for_bars(bars))
            + f"&apikey={FMP_API_KEY}",
            timeout=_FMP_TIMEOUT,
        )
        if r is None:
            return pd.Series(dtype=float), pd.Series(dtype=float)
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.Series(dtype=float), pd.Series(dtype=float)
        df = pd.DataFrame(rows)
        if "date" not in df.columns or "close" not in df.columns:
            return pd.Series(dtype=float), pd.Series(dtype=float)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        # FMP가 간혹 같은 날짜를 중복 반환 — 중복 라벨은 DataFrame 조립 시
        # "cannot reindex on an axis with duplicate labels" 오류를 유발하므로 최신값만 유지
        df = df[~df.index.duplicated(keep="last")]
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if "volume" in df.columns:
            vol = pd.to_numeric(df["volume"], errors="coerce").dropna()
        else:
            # FMP 가 일부 ETF 에 volume 을 주지 않는다. 빈 시리즈를 돌려주면
            # rotation_core.avg_dollar_volume 이 None → '판정 불가' → 제외로 간다.
            # 0 으로 채우지 않는다 — 0 은 '거래 없음'이라는 다른 주장이다.
            vol = pd.Series(dtype=float)
        return close, vol
    except Exception:
        return pd.Series(dtype=float), pd.Series(dtype=float)


def _fmp_batch_ohlcv_df(tickers: list, bars: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """여러 티커 병렬 조회 → (Close DataFrame, Volume DataFrame).

    bars 는 그대로 하류에 전달된다. 여기에도 기본값을 두지 않는다 — 중간 계층이
    기본값을 가지면 최종 호출부의 요구가 가려진다.

    거래량이 빈 티커는 volume 쪽 열에서 빠진다. 그 티커는 유동성 '판정 불가'가
    되어 제외되는데, 이는 의도된 동작이다 (모르면 안 산다).
    """
    if not tickers:
        return pd.DataFrame(), pd.DataFrame()
    import concurrent.futures

    def _one(tk):
        return tk, _fmp_price_history_ohlcv(tk, bars=bars)

    closes, vols = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_one, tk): tk for tk in tickers}
        for fut in concurrent.futures.as_completed(futs):
            try:
                tk, pair = fut.result()
                c, v = pair
                if c is not None and not c.empty:
                    closes[tk] = c
                if v is not None and not v.empty:
                    vols[tk] = v
            except Exception:
                pass
    cdf = pd.DataFrame(closes).sort_index() if closes else pd.DataFrame()
    vdf = pd.DataFrame(vols).sort_index() if vols else pd.DataFrame()
    return cdf, vdf


def _fmp_profile(ticker: str) -> dict:
    try:
        r = fx.fmp_get(f"{_FMP_BASE}/profile?symbol={ticker}&apikey={FMP_API_KEY}", timeout=_FMP_TIMEOUT)
        d = r.json()
        return d[0] if isinstance(d, list) and d else {}
    except Exception:
        return {}


def _fmp_etf_symbol_name_map() -> dict:
    """/stable/etf-list → {심볼: 이름} 맵.

    [2026-09-01] 옛 `_fmp_etf_symbol_set()` 은 같은 응답에서 **이름을 버리고**
      심볼 집합만 만들었다. 그 결과 `is_rotation_excluded(tk, "")` 로 호출되어
      이름 정규식이 볼 문자열이 없었고, SPAX(T-REX 2X Long SPCX Daily Target ETF)가
      레버리지 필터를 통과해 Top 1·매수로 나갔다.

      app.py 는 같은 엔드포인트에서 `_fmp_etf_symbol_name_map()` 으로 이름을
      살려 쓰고 있었다. 즉 같은 함수·다른 입력이었고, "이메일과 앱 화면이 항상
      일치"라던 주석은 거짓이었다. 여기서 입력을 맞춘다 — **API 콜 추가 0.**
    """
    try:
        r = fx.fmp_get(f"{_FMP_BASE}/etf-list?apikey={FMP_API_KEY}", timeout=15)
        if r is None:
            return {}
        data = r.json()
        if not isinstance(data, list):
            return {}
        out = {}
        for it in data:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol", "") or "").strip().upper()
            if sym:
                out[sym] = str(it.get("name", "") or "").strip()
        return out
    except Exception:
        return {}


def calculate_period_return(close_series, lookback_days: int):
    """lookback_days 거래일 전 대비 수익률(%). (app.py 동일)"""
    if close_series is None:
        return np.nan
    clean = pd.to_numeric(close_series, errors="coerce").dropna()
    if len(clean) <= lookback_days:
        return np.nan
    latest = clean.iloc[-1]
    past = clean.iloc[-(lookback_days + 1)]
    if pd.isna(latest) or pd.isna(past) or past == 0:
        return np.nan
    return (latest / past - 1.0) * 100


# ── [STEP 1] 신규 ETF 발견 → 시트 추가 ────────────────────────────────────────
def discover_and_add_new_etfs(ws) -> int:
    """최근 N일 신규 상장 ETF를 찾아 ETF_Universe에 추가. 반환: 추가된 수.
    (app.fetch_new_etfs_from_fmp + save_new_etfs_to_sheet 동일 로직)"""
    try:
        etf_names = _fmp_etf_symbol_name_map()
        if not etf_names:
            print("[WARN] ETF 심볼 목록 조회 실패 — 발견 단계 스킵")
            return 0
        etf_set = set(etf_names.keys())

        today_et = datetime.now(_ET)
        cutoff = today_et - timedelta(days=_DISCOVERY_LOOKBACK_DAYS)
        from_str, to_str = cutoff.strftime("%Y-%m-%d"), today_et.strftime("%Y-%m-%d")
        r = fx.fmp_get(f"{_FMP_BASE}/ipos-calendar?from={from_str}&to={to_str}&apikey={FMP_API_KEY}",
                         timeout=15)
        if r is None:
            print("[WARN] ipos-calendar 조회 실패(레이트리밋/HTTP 오류) — 발견 단계 스킵")
            return 0
        ipos = r.json()
        if not isinstance(ipos, list):
            return 0

        _US_EXCH = {"NYSE ARCA", "NYSEARCA", "ARCA", "NASDAQ", "NASDAQ GLOBAL MARKET",
                    "BATS", "CBOE", "CBOE BZX", "NYSE", "AMEX", "NYSE AMERICAN"}

        candidates, seen = [], set()
        for it in ipos:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol", "") or "").strip().upper()
            if not sym or sym in seen or sym not in etf_set:
                continue
            exch = str(it.get("exchange", "") or it.get("exchangeShortName", "") or "").upper().strip()
            if exch and exch not in _US_EXCH:
                continue
            ipo_date_str = str(it.get("date", "") or it.get("ipoDate", "") or "")[:10]
            if ipo_date_str:
                try:
                    ipo_dt = datetime.strptime(ipo_date_str, "%Y-%m-%d").replace(tzinfo=_ET)
                    if ipo_dt < cutoff:
                        continue
                except Exception:
                    pass
            seen.add(sym)
            # 이름 우선순위: etf-list > ipos-calendar.
            # ipos-calendar 의 company/name 은 ETF 항목에서 자주 비는데, 그게
            # SPAX 가 이름 없이 시트에 들어간 경로였다. 아래에서 profile.companyName
            # 으로 한 번 더 덮어쓴다(콜 추가 없음 — AUM 때문에 어차피 호출한다).
            _nm = etf_names.get(sym, "") or str(it.get("company", "") or it.get("name", "") or "")
            candidates.append({"ticker": sym, "name": _nm[:80]})

        # AUM 확인 (profile) — 호출 수 제한
        filtered = []
        for etf in candidates[:60]:
            try:
                p = _fmp_profile(etf["ticker"])
                # profile.companyName 이 가장 정확한 정식명이다. 이 호출은 원래
                # AUM 때문에 하던 것이고, 옛 코드는 companyName 을 손에 쥐고도
                # 버렸다 (sector/industry 만 category 로 저장). 이제 살려 쓴다.
                _cn = str(p.get("companyName") or "").strip()
                if _cn:
                    etf["name"] = _cn[:80]
                # [2026-09-02] totalAssets/mktCap → marketCap. 앞의 두 키는
                #   /stable/profile 에 **존재하지 않는다** (mktCap 은 레거시 v3,
                #   totalAssets 는 재무제표 필드). 그래서 aum 이 항상 None 이었고
                #   '모르면 제외'가 전 종목에 걸렸다. 프로브 diag_aum_field 로
                #   marketCap 확정 (GDX profile/etf-info 비 = 1.014).
                _raw = p.get("marketCap")
                aum = (float(_raw) / 1_000_000) if _raw not in (None, "", 0) else None
                # ⚠️ 옛 조건은 `if aum and aum < MIN: continue` 였다. falsy(0/None)가
                #    통과했고, 신규 상장 직후엔 totalAssets 가 거의 항상 비므로
                #    **막으려던 대상에게만 정확히 무력했다.** 이제 '모르면 제외'.
                if not rc.passes_aum(aum, _DISCOVERY_MIN_AUM_M):
                    print(f"[INFO] 신규 발견 스킵 (순자산 미달/미상): {etf['ticker']} — "
                          f"{'데이터 없음' if aum is None else f'${aum:.0f}M'}")
                    continue
                etf["aum_m"] = f"{aum:.0f}"
                etf["category"] = str(p.get("sector") or p.get("industry") or "")[:50]
                filtered.append(etf)
            except Exception:
                # profile 조회 자체가 실패하면 AUM 을 모른다 → 제외 (같은 원칙).
                print(f"[INFO] 신규 발견 스킵 (profile 조회 실패): {etf['ticker']}")
                continue

        if not filtered:
            print("[INFO] 신규 ETF 후보 없음")
            return 0

        # 시트에 없는 것만 추가
        existing = ws.get_all_values()
        existing_tickers = {str(r[0]).strip().upper() for r in existing[1:] if r and r[0].strip()}
        added_date = today_et.strftime("%Y-%m-%d")
        rows_to_add = []
        for etf in filtered:
            tk = etf["ticker"]
            if tk in existing_tickers:
                continue
            # 로테이션 정책 (fmp_extras SSOT): 인버스/레버리지는 애초에 유니버스에 추가하지 않음
            if fx is not None and fx.is_rotation_excluded(tk, etf.get("name", "")):
                print(f"[INFO] 신규 발견 스킵 (인버스/레버리지): {tk} — {etf.get('name','')[:50]}")
                continue
            rows_to_add.append([tk, etf.get("name", ""), etf.get("category", ""),
                                etf.get("aum_m", ""), added_date, "FMP_AUTO"])
            existing_tickers.add(tk)

        if rows_to_add:
            _safe_append_rows(ws, rows_to_add, value_input_option="USER_ENTERED")
            print(f"[OK] 신규 ETF {len(rows_to_add)}개 추가: {[r[0] for r in rows_to_add]}")
        else:
            print("[INFO] 신규 ETF 모두 기존 유니버스에 존재 — 추가 없음")
        return len(rows_to_add)
    except Exception as e:
        print(f"[WARN] 발견 단계 실패(랭킹은 계속 진행): {e}")
        return 0


# ── [STEP 3·4] 수익률 계산 + 점수화 + 랭킹 ────────────────────────────────────
def build_ranked_table(tickers: list) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """티커별 1주·1개월 수익률 → 백분위 정규화 → 가중 점수 → 순위.

    반환: (랭킹 DataFrame[rank, Ticker, week_pct, month_pct, score],
           종가 DataFrame, 거래량 DataFrame)

    가격·거래량 프레임을 함께 돌려주는 이유: 하류 게이트(유동성·상관)가 같은
    데이터를 요구하는데, 다시 조회하면 API 콜이 두 배가 되고 두 조회 사이에
    시점이 어긋난다. 한 번 받은 것을 흘려보낸다.
    """
    # 소요 봉 수는 rotation_core 가 정한다 (REQUIRED_BARS = 61).
    #   · calculate_period_return(s, 21) → iloc[-22]           → 22봉
    #   · 상관 60일 수익률                → 종가 61봉
    #   · 달러거래대금 20일 평균                                → 20봉
    # 셋 중 최대. 여기에 숫자를 직접 쓰지 않는다 — 임계값이 바뀌면 창도 같이
    # 따라와야 하는데, 상수를 복사해 두면 조용히 어긋난다.
    close_df, volume_df = _fmp_batch_ohlcv_df(tickers, bars=rc.REQUIRED_BARS)
    if close_df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    rows = []
    for tk in tickers:
        s = close_df[tk] if tk in close_df.columns else pd.Series(dtype=float)
        rows.append({
            "Ticker": tk,
            "week_pct":  calculate_period_return(s, 5),
            "month_pct": calculate_period_return(s, 21),
        })
    df = pd.DataFrame(rows)
    df["week_pct"]  = pd.to_numeric(df["week_pct"], errors="coerce")
    df["month_pct"] = pd.to_numeric(df["month_pct"], errors="coerce")

    # 두 수익률 모두 있어야 점수화 (데이터 부족 ETF는 순위 제외)
    df = df.dropna(subset=["week_pct", "month_pct"]).copy()
    if df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    # 백분위 정규화(0~100, 높을수록 강함) 후 가중합 — raw % 직접 합산 금지
    df["pr_week"]  = df["week_pct"].rank(pct=True) * 100.0
    df["pr_month"] = df["month_pct"].rank(pct=True) * 100.0
    df["score"] = _W_MONTH * df["pr_month"] + _W_WEEK * df["pr_week"]

    df = df.sort_values("score", ascending=False, kind="mergesort").reset_index(drop=True)
    df.insert(0, "rank", np.arange(1, len(df) + 1))
    return df[["rank", "Ticker", "week_pct", "month_pct", "score"]], close_df, volume_df


# ── [STEP 4.5] A-2 상위 후보 재검증 + 게이트 적용 ─────────────────────────────
_VERIFY_TOP_N = 15   # profile 재조회 대상 (콜 15개)
_TOP_N_EMAIL = _VERIFY_TOP_N   # 이메일 표 = 판정 범위 (위 주석 참조)


def verify_and_gate(ranked: pd.DataFrame, close_df: pd.DataFrame,
                    volume_df: pd.DataFrame, name_map: dict) -> dict:
    """상위 후보만 profile 로 재검증한 뒤 rotation_core 게이트를 적용한다.

    왜 상위 N개만 하는가: 유니버스 전체(수백 종목)에 profile 을 돌리면 콜이
    폭발한다. 게이트는 **최종 슬롯 선정에만** 영향을 주므로 상위권만 정확하면
    된다. N=15 는 Top 5 를 채우는 데 여유가 있는 값이다 — 게이트가 10개를
    떨궈도 슬롯이 찬다.

    이 단계가 A-1(이름 배관)의 **최종 방어선**이다. 시트에도 etf-list 에도
    이름이 없는 신규 상장 ETF 는 여기서 profile.companyName 으로 처음 이름을
    얻는다. SPAX 는 두 겹 중 하나만 뚫려도 막힌다.
    """
    head = ranked.head(_VERIFY_TOP_N)["Ticker"].tolist() if not ranked.empty else []
    meta: dict = {}
    for tk in head:
        nm = (name_map or {}).get(tk, "")
        sector, aum_m = "", None
        try:
            prof = _fmp_profile(tk)
        except Exception:
            prof = {}
        if prof:
            cn = str(prof.get("companyName") or "").strip()
            if cn:
                nm = cn            # profile 이름이 가장 정확 — 있으면 이긴다
            sector = str(prof.get("sector") or prof.get("industry") or "")
            # [2026-09-02] marketCap 이 정답 — diag_aum_field 프로브 참조.
            raw = prof.get("marketCap")
            if raw not in (None, "", 0):
                try:
                    aum_m = float(raw) / 1_000_000
                except (TypeError, ValueError):
                    aum_m = None
        lev = fx.is_rotation_excluded(tk, nm)
        if lev:
            print(f"[GATE] 레버리지/인버스 재검출: {tk} — {nm[:60]}")
        meta[tk] = {"name": nm, "sector": sector, "aum_m": aum_m, "leveraged": lev}

    gates = rc.apply_rotation_gates(
        ranked.head(_VERIFY_TOP_N), close_df=close_df, volume_df=volume_df,
        meta=meta, slots=_HOLD_SLOTS,
    )
    gates["meta"] = meta
    for tk, why in gates["excluded"].items():
        print(f"[GATE] 제외 {tk}: {rc.reason_label(why)} — {gates['detail'].get(tk, '')}")
    print(f"[GATE] 최종 슬롯: {gates['selected']}")
    return gates


# ── [STEP 7] 스냅샷 로드/저장 (1셀 JSON, 드리프트 없음) ───────────────────────
def load_prev_snapshot(gc) -> tuple[dict, str, list]:
    """지난주 {ticker: rank} 맵 · 날짜 · **실제 보유 목록**을 반환.

    [2026-09-01] 세 번째 반환값을 추가했다. 게이트 도입 전에는 '순위 ≤ 5 = 보유'가
      참이라 순위만 저장해도 됐지만, 이제 유동성·중복·크립토 캡이 순위와 보유를
      분리한다. 순위에서 보유를 역산하면 **매도 신호가 조용히 틀린다** —
      예: 캡에 걸려 안 산 종목을 다음 주에 '매도'하라고 지시한다.
    """
    try:
        sh = gc.open(_SPREADSHEET_TITLE)
        try:
            ws = sh.worksheet(_SNAPSHOT_SHEET_TITLE)
        except gspread.exceptions.WorksheetNotFound:
            return {}, "", []
        raw = ws.acell("A1").value
        if not raw:
            return {}, "", []
        obj = json.loads(raw)
        ranks = {str(k).upper(): int(v) for k, v in (obj.get("ranks") or {}).items()}
        sel = [str(t).upper() for t in (obj.get("selected") or []) if str(t).strip()]
        if not sel:
            # 하위호환: 게이트 도입(2026-09-01) 이전 스냅샷에는 selected 가 없다.
            # 그때는 '순위 ≤ 5 = 보유'가 참이었으므로 그 규칙으로 복원한다.
            sel = sorted([t for t, r in ranks.items() if r <= _HOLD_SLOTS],
                         key=lambda t: ranks[t])
            print("[INFO] 옛 스냅샷 형식 — 순위≤5 로 보유 목록 복원")
        return ranks, str(obj.get("date", "")), sel
    except Exception as e:
        print(f"[WARN] 스냅샷 로드 실패: {e}")
        return {}, "", []


def save_snapshot(gc, rank_map: dict, date_str: str, selected: list | None = None) -> None:
    """이번 주 순위 + 보유 슬롯 스냅샷 저장(A1 셀 JSON 덮어쓰기)."""
    try:
        sh = gc.open(_SPREADSHEET_TITLE)
        try:
            ws = sh.worksheet(_SNAPSHOT_SHEET_TITLE)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=_SNAPSHOT_SHEET_TITLE, rows=10, cols=2)
        payload = json.dumps({"date": date_str, "ranks": rank_map,
                              "selected": list(selected or [])}, ensure_ascii=False)
        ws.update([[payload]], range_name="A1", value_input_option="RAW")
        print(f"[OK] 스냅샷 저장 완료 ({len(rank_map)}종목)")
    except Exception as e:
        print(f"[WARN] 스냅샷 저장 실패: {e}")


def _same_iso_week(date_str: str, ref_dt: datetime) -> bool:
    """date_str(YYYY-MM-DD)가 ref_dt와 같은 ISO 주(월~일)인지."""
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return d.isocalendar()[:2] == ref_dt.date().isocalendar()[:2]
    except Exception:
        return False


# ── 액션 요약 계산 ─────────────────────────────────────────────────────────────
def compute_actions(ranked: pd.DataFrame, prev_map: dict,
                    selected: list | None = None,
                    prev_selected: list | None = None) -> dict:
    """보유 슬롯 기준 매수/매도 신호 산출.

    [2026-09-01] 비교 기준을 '순위 ≤ 5' 에서 **실제 선정 결과**로 바꿨다.
      게이트가 순위와 보유를 분리한 이상, 순위로 매매를 지시하면 사지도 않은
      종목에 매도 신호가 나간다.
    """
    cur_rank = dict(zip(ranked["Ticker"], ranked["rank"]))
    cur_top5 = list(selected if selected is not None
                    else ranked.head(_HOLD_SLOTS)["Ticker"].tolist())
    prev_top5 = list(prev_selected or [])

    # 신규 매수: 이번 Top5 중 지난주 Top5에 없던 것
    buys = []
    for tk in cur_top5:
        was_in = tk in prev_top5
        if not was_in:
            buys.append((tk, int(cur_rank[tk]), tk not in prev_map))

    # 매도: 지난주 Top5였는데 지금 Top5 밖(또는 순위권 이탈)
    sells = []
    for tk in prev_top5:
        if tk not in cur_top5:
            now_rank = cur_rank.get(tk)  # None이면 순위권 밖
            sells.append((tk, int(now_rank) if now_rank is not None else None))
    return {"buys": buys, "sells": sells, "has_prev": bool(prev_map)}


# ── 이메일 HTML ────────────────────────────────────────────────────────────────
def _fmt_pct(v) -> str:
    return f"{v:+.2f}%" if pd.notna(v) else "N/A"


def _delta_badge(tk: str, cur_rank: int, prev_map: dict) -> str:
    if tk not in prev_map:
        return '<span style="color:#fbbf24;font-weight:700;">NEW</span>'
    d = prev_map[tk] - cur_rank  # 양수=상승
    if d > 0:
        return f'<span style="color:#16a34a;">▲{d}</span>'
    if d < 0:
        return f'<span style="color:#dc2626;">▼{abs(d)}</span>'
    return '<span style="color:#64748b;">=</span>'


def build_satellite_html(sat: dict | None) -> str:
    """🛰️ 위성 섹터 Top10 이메일 섹션 — app.py 탭과 동일 데이터(SSOT: fmp_extras).
    행마다 중복 상대 전부(10%↑) 표시 — 이메일만으로 위성 리밸런싱이 가능해야 한다."""
    if not sat or not sat.get("rows"):
        return ""
    mf = sat.get("market_filter")
    if mf and mf.get("risk_on"):
        mf_html = (f'<div style="background:#0b1f17;border:1px solid #16a34a;border-radius:8px;'
                   f'padding:10px 14px;margin-bottom:12px;font-size:13px;color:#86efac;">'
                   f'🚦 SPY ${mf["spy"]:,} &gt; 200일선 ${mf["ma200"]:,} — <b>위성 정상 운용 구간</b></div>')
    elif mf:
        mf_html = (f'<div style="background:#2a1214;border:1px solid #dc2626;border-radius:8px;'
                   f'padding:10px 14px;margin-bottom:12px;font-size:13px;color:#fca5a5;">'
                   f'🚦 SPY ${mf["spy"]:,} &lt; 200일선 ${mf["ma200"]:,} — '
                   f'<b>⛔ 위성 신규 매수 중단·비중 축소 룰 발동</b></div>')
    else:
        mf_html = ('<div style="font-size:12px;color:#94a3b8;margin-bottom:12px;">'
                   '🚦 시장 필터: SPY 데이터 미확보 — 판단 유보</div>')

    def _grade(p):
        return "🔴" if p >= 40 else ("🟡" if p >= 25 else "🟢")

    rows_html = ""
    for r in sat["rows"]:
        theme = f" · {r['theme_label']}" if r.get("theme_label") else ""
        r1w = f" (1W {r['r1w']:+.1f}%)" if r.get("r1w") is not None else ""
        if r.get("overlaps"):
            ov = " &nbsp;·&nbsp; ".join(f"{_grade(p)} {t} {p:.0f}%" for t, p in r["overlaps"])
        else:
            ov = "🟢 없음 (10%↑ 기준)"
        rows_html += (
            f'<div style="border-bottom:1px solid #1f2937;padding:8px 0;">'
            f'<div style="font-size:14px;color:#e2e8f0;"><b>{r["rank"]}위 {r["ticker"]}</b>'
            f' <span style="color:#94a3b8;">— {r["sector_label"]}{theme}</span>'
            f' &nbsp;<span style="color:#fbbf24;font-weight:700;">점수 {r["score"]:+.1f}</span></div>'
            f'<div style="font-size:12px;color:#94a3b8;margin-top:2px;">'
            f'1M {r["r1m"]:+.1f}% · 3M {r["r3m"]:+.1f}% · 6M {r["r6m"]:+.1f}%{r1w}</div>'
            f'<div style="font-size:12px;color:#cbd5e1;margin-top:2px;">중복: {ov}</div>'
            f'</div>'
        )
    return (
        '<div style="background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:16px;">'
        '<div style="font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:4px;">'
        '🛰️ 위성 섹터 Top10 — 월간 리밸런싱 후보</div>'
        '<div style="font-size:11px;color:#64748b;margin-bottom:10px;">'
        '점수 = 1M×40% + 3M×40% + 6M×20% (1주는 표시만) · GICS 섹터당 1개 · '
        '중복 % = 구성종목 상위 15개 교집합 · 🟢&lt;25 🟡25~40 🔴40+</div>'
        f'{mf_html}{rows_html}'
        f'<div style="font-size:11px;color:#64748b;margin-top:8px;">기준: {sat.get("as_of", "")}'
        ' · 상세 매트릭스는 앱 [2단계] 섹터 &amp; 자금 흐름 탭</div>'
        '</div>'
    )


def build_email_html(ranked: pd.DataFrame, actions: dict, prev_map: dict,
                     prev_date: str, new_added: int, satellite: dict | None = None,
                     gates: dict | None = None) -> str:
    now_et  = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
    now_kst = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")

    # ── 액션 요약 카드 ──
    if not actions["has_prev"]:
        action_html = (
            '<div style="background:#1e293b;border-radius:8px;padding:14px 16px;margin-bottom:16px;'
            'border:1px solid #334155;color:#94a3b8;font-size:13px;">'
            '첫 실행입니다. 지난주 스냅샷이 없어 변화(Δ)·매수/매도 신호는 다음 주부터 표시됩니다. '
            '아래 <b style="color:#e2e8f0;">Top 5</b>로 시작하세요.</div>'
        )
    else:
        buy_lines = ""
        for tk, rk, is_new in actions["buys"]:
            tag = " (신규 진입)" if is_new else ""
            buy_lines += f'<div style="color:#86efac;font-size:14px;margin:3px 0;">🟢 <b>{tk}</b> — 현재 {rk}위{tag} · 매수</div>'
        if not buy_lines:
            buy_lines = '<div style="color:#64748b;font-size:13px;">신규 매수 없음 (Top 5 유지)</div>'

        sell_lines = ""
        for tk, now_rk in actions["sells"]:
            where = f"현재 {now_rk}위" if now_rk is not None else "순위권 밖"
            sell_lines += f'<div style="color:#fca5a5;font-size:14px;margin:3px 0;">🔴 <b>{tk}</b> — {where} (Top 5 이탈) · 매도</div>'
        if not sell_lines:
            sell_lines = '<div style="color:#64748b;font-size:13px;">매도 없음 (보유 5종목 전부 Top 5 유지)</div>'

        action_html = (
            '<div style="background:#0b1f17;border:1px solid #16a34a;border-radius:10px;padding:16px;margin-bottom:16px;">'
            '<div style="font-weight:800;color:#4ade80;font-size:15px;margin-bottom:10px;">📌 이번 주 액션</div>'
            f'{buy_lines}'
            '<div style="height:8px;"></div>'
            f'{sell_lines}'
            '</div>'
        )

    # ── Top 10 표 ──
    rows_html = ""
    _selected = list((gates or {}).get("selected") or [])
    _excluded = dict((gates or {}).get("excluded") or {})
    _detail   = dict((gates or {}).get("detail") or {})

    for _, r in ranked.head(_TOP_N_EMAIL).iterrows():
        rk = int(r["rank"])
        tk = r["Ticker"]
        in_top5 = (tk in _selected) if _selected else (rk <= _HOLD_SLOTS)
        row_bg = "#13243b" if in_top5 else "#0f172a"
        rk_color = "#60a5fa" if in_top5 else "#64748b"
        _why = _excluded.get(tk)
        if _why:
            rk_label = f'<b style="color:#64748b;">{rk}</b> 🚫'
        else:
            rk_label = f'<b style="color:{rk_color};">{rk}</b>' + (" ⭐" if in_top5 else "")
        delta = _delta_badge(tk, rk, prev_map)
        # 제외 사유는 티커 밑에 작게 — 왜 상위인데 안 사는지 이메일만 보고 알아야 한다
        tk_cell = f'{tk}'
        if _why:
            _d = _detail.get(tk, "")
            tk_cell += (f'<div style="font-size:11px;font-weight:400;color:#f59e0b;'
                        f'margin-top:2px;">{rc.reason_label(_why)}'
                        + (f' · {_d}' if _d else '') + '</div>')
        rows_html += (
            f'<tr style="background:{row_bg};">'
            f'<td style="padding:8px 10px;text-align:center;">{rk_label}</td>'
            f'<td style="padding:8px 10px;font-weight:700;color:#e2e8f0;">{tk_cell}</td>'
            f'<td style="padding:8px 10px;text-align:right;color:#cbd5e1;">{_fmt_pct(r["week_pct"])}</td>'
            f'<td style="padding:8px 10px;text-align:right;color:#cbd5e1;">{_fmt_pct(r["month_pct"])}</td>'
            f'<td style="padding:8px 10px;text-align:center;color:#94a3b8;">{r["score"]:.1f}</td>'
            f'<td style="padding:8px 10px;text-align:center;">{delta}</td>'
            f'</tr>'
        )

    discovery_note = (f' · 신규 ETF {new_added}개 추가됨' if new_added else "")
    prev_note = f"지난주 스냅샷: {prev_date}" if prev_date else "지난주 스냅샷 없음(첫 실행)"
    satellite_html = build_satellite_html(satellite)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;">

  <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #334155;">
    <div style="font-size:22px;font-weight:800;color:#60a5fa;">💰 Hidden Alpha Radar</div>
    <div style="font-size:14px;color:#94a3b8;margin-top:4px;">$50 × Top 5 로테이션 · 주간 리포트</div>
    <div style="font-size:13px;color:#64748b;margin-top:6px;">{now_et} &nbsp;|&nbsp; {now_kst}</div>
    <div style="font-size:12px;color:#475569;margin-top:4px;">{prev_note}{discovery_note}</div>
  </div>

  {action_html}

  <div style="background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:16px;">
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:10px;">📊 Top {_TOP_N_EMAIL} 랭킹 (⭐ = 실제 보유 대상 · 🚫 = 게이트 제외)</div>
    <div style="font-size:11px;color:#64748b;margin:-6px 0 8px;">게이트 판정 대상은 상위 {_TOP_N_EMAIL}개입니다. 그 아래 순위는 판정하지 않으므로 표시하지 않습니다.</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="color:#94a3b8;border-bottom:1px solid #334155;">
          <th style="padding:6px 10px;text-align:center;">순위</th>
          <th style="padding:6px 10px;text-align:left;">Ticker</th>
          <th style="padding:6px 10px;text-align:right;">1주%</th>
          <th style="padding:6px 10px;text-align:right;">1개월%</th>
          <th style="padding:6px 10px;text-align:center;">점수</th>
          <th style="padding:6px 10px;text-align:center;">Δ</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    <div style="font-size:11px;color:#64748b;margin-top:8px;">
      점수 = 0.7×(1개월 백분위) + 0.3×(1주 백분위) · 데이터 부족 ETF는 순위 제외
    </div>
  </div>

  {satellite_html}

  <div style="background:#1c1917;border:1px solid #44403c;border-radius:8px;padding:12px 16px;margin-bottom:16px;">
    <div style="font-size:12px;color:#d6d3d1;line-height:1.7;">
      <b style="color:#fbbf24;">규칙</b> · Top 5 진입 = 매수 &nbsp;|&nbsp; Top 5 이탈 = 매도 &nbsp;|&nbsp; 월요일 10시(ET) 이후 집행
    </div>
  </div>

  <div style="text-align:center;padding:16px;">
    <a href="https://stocker.streamlit.app"
       style="background:#2563eb;color:#fff;text-decoration:none;padding:12px 32px;border-radius:8px;font-weight:700;font-size:14px;">
      🚀 Quant Terminal 열기
    </a>
  </div>

  <div style="text-align:center;font-size:11px;color:#475569;margin-top:16px;">
    본 리포트는 AI 참고용이며 투자 권유가 아닙니다. · Quant Terminal Auto Report
  </div>
</div>
</body></html>"""


def _send_email_one(subject: str, html_body: str, to_addr: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = str(to_addr)
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, str(to_addr), msg.as_string())
        print(f"[OK] 이메일 발송 완료 → {to_addr}")
        return True
    except Exception as e:
        print(f"[ERROR] 이메일 발송 실패({to_addr}): {e}")
        return False


def send_email(subject: str, html_body: str) -> bool:
    """Hidden Alpha 브로드캐스트 — Alert_HiddenAlpha ON 인 승인 사용자 전원.

    ⚠️ 관리자도 Users 시트 토글을 따른다(예전엔 GMAIL_TO 하드코딩이라 끌 수 없었다).
    """
    try:
        gc = get_gspread_client()
        ws = gc.open(_SPREADSHEET_TITLE).worksheet("Users")
        uc.ensure_users_header_v3(ws)
        rcpts = uc.get_recipients(ws, "hidden", admin_fallback_email=GMAIL_TO)
    except Exception as e:
        print(f"[WARN] Users 수신자 조회 실패 — 관리자 폴백: {e}")
        rcpts = [(uc.ADMIN_CONTENT_OWNER_ID, GMAIL_TO)]
    if not rcpts:
        print("[INFO] Hidden Alpha 수신자가 없습니다 — 발송 생략")
        return True
    admin_u = str(uc.ADMIN_CONTENT_OWNER_ID).strip().upper()
    ok_admin, admin_targeted = True, False
    for _uid, _email in rcpts:
        is_admin = str(_uid).strip().upper() == admin_u
        ok = _send_email_one(subject, html_body, str(_email).strip())
        if is_admin:
            admin_targeted, ok_admin = True, ok
        elif not ok:
            print(f"[WARN] 게스트 {_uid} 발송 실패 — 계속 진행")
    return ok_admin if admin_targeted else True

def main():
    print("=" * 60)
    print(f"[START] Hidden Alpha 자동화: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    # 주 1회 보장: 이번 ISO 주에 이미 발송(스냅샷 저장)했으면 스킵
    gc = get_gspread_client()
    prev_map, prev_date, prev_selected = load_prev_snapshot(gc)
    if not _FORCE_RUN and _same_iso_week(prev_date, datetime.now(_ET)):
        print(f"[SKIP] 이번 주(스냅샷 {prev_date})에 이미 발송됨. 종료. (강제 실행은 HIDDEN_ALPHA_FORCE=1)")
        sys.exit(0)

    uni_ws = open_etf_universe_worksheet(gc)

    # [STEP 1] 신규 ETF 발견 → 추가 (삭제/정리는 앱에서 수동)
    print("[STEP 1] 신규 ETF 발견 중...")
    new_added = discover_and_add_new_etfs(uni_ws)

    # [STEP 2] 누적 유니버스 전체 로드
    print("[STEP 2] ETF_Universe 전체 로드 중...")
    # etf-list 이름맵을 한 번만 받아 발견·로드가 공유한다 (콜 1회).
    _etf_names = _fmp_etf_symbol_name_map()
    tickers, name_map = load_universe_tickers(uni_ws, _etf_names)
    print(f"[INFO] 유니버스 {len(tickers)}개 티커")
    if not tickers:
        print("[ERROR] 유니버스가 비어 있음. 종료.")
        sys.exit(1)

    # [STEP 3·4] 수익률 → 점수 → 랭킹
    print("[STEP 3-4] 수익률 계산·점수화·랭킹 중...")
    ranked, close_df, volume_df = build_ranked_table(tickers)
    if ranked.empty:
        print("[ERROR] 랭킹 산출 실패(데이터 부족/네트워크). 종료.")
        sys.exit(1)
    print(f"[INFO] 랭킹 산출 {len(ranked)}개. 점수 상위 5: {ranked.head(5)['Ticker'].tolist()}")

    # [STEP 4.5] 상위 후보 재검증 + 게이트 (레버리지·유동성·AUM·중복·크립토 캡)
    print(f"[STEP 4.5] 상위 {_VERIFY_TOP_N}개 재검증·게이트 적용 중...")
    gates = verify_and_gate(ranked, close_df, volume_df, name_map)
    selected = gates.get("selected") or []
    if not selected:
        print("[WARN] 게이트 통과 종목 0개 — 이번 주는 신규 매수 없음으로 발송한다.")

    # [STEP 5] 지난주 스냅샷(상단에서 이미 로드) → 액션·Δ
    print("[STEP 5] 스냅샷 비교 중...")
    actions = compute_actions(ranked, prev_map, selected=selected,
                              prev_selected=prev_selected)

    # [STEP 5.5] 🛰️ 위성 섹터 Top10 (SSOT: fmp_extras · 실패 시 섹션 생략, 발송은 계속)
    satellite = None
    if fx is not None:
        print("[STEP 5.5] 위성 섹터 Top10 계산 중... (약 60개 ETF)")
        try:
            satellite = fx.compute_satellite_top10()
            print(f"[INFO] 위성 Top10: {[r['ticker'] for r in satellite.get('rows', [])]}")
            mf = satellite.get("market_filter")
            if mf:
                print(f"[INFO] 시장 필터: SPY {mf['spy']} vs MA200 {mf['ma200']} → "
                      f"{'정상 운용' if mf['risk_on'] else '⛔ 위성 축소 룰'}")
        except Exception as exc:
            print(f"[WARN] 위성 Top10 계산 실패 — 섹션 생략: {exc}")
            satellite = None

    # [STEP 6] 이메일 발송
    print("[STEP 6] 이메일 발송 중...")
    top5_str = ", ".join(selected) if selected else "해당 없음"
    n_buy, n_sell = len(actions["buys"]), len(actions["sells"])
    tag = ""
    if actions["has_prev"] and (n_buy or n_sell):
        tag = f" · 🔁 매수{n_buy}/매도{n_sell}"
    subject = f"💰 [Hidden Alpha] Top5: {top5_str}{tag} · {datetime.now(_ET).strftime('%m/%d')}"
    html_body = build_email_html(ranked, actions, prev_map, prev_date, new_added,
                                 satellite=satellite, gates=gates)
    send_email(subject, html_body)

    # [STEP 7] 이번 주 스냅샷 저장 (Top 30만 — Δ 계산엔 충분)
    print("[STEP 7] 스냅샷 저장 중...")
    snap = {row["Ticker"]: int(row["rank"]) for _, row in ranked.head(30).iterrows()}
    save_snapshot(gc, snap, datetime.now(_ET).strftime("%Y-%m-%d"), selected=selected)

    print(f"[DONE] 완료: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
