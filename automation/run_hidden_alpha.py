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

import requests
import numpy as np
import pandas as pd
import pytz
import gspread
from google.oauth2.service_account import Credentials

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

_TOP_N_EMAIL = 10   # 이메일에 표시할 순위 수
_HOLD_SLOTS  = 5    # 실제 보유 슬롯 수 (Top 5)

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


def load_universe_tickers(ws) -> list[str]:
    """ETF_Universe 시트 A열에서 누적 티커 전체 로드 (헤더 제외, 중복 제거)."""
    try:
        vals = ws.get_all_values()
        if not vals or len(vals) < 2:
            return []
        tickers, seen = [], set()
        for r in vals[1:]:
            tk = str((r + [""])[0]).strip().upper()
            if tk and tk not in seen:
                seen.add(tk)
                tickers.append(tk)
        return tickers
    except Exception:
        return []


# ── FMP 헬퍼 (app.py 로직에서 st.cache_data·st.secrets만 제거) ────────────────
def _fmp_price_history_close(ticker: str, limit: int = 130) -> pd.Series:
    """historical-price-eod → Close 시리즈."""
    try:
        r = requests.get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker}&limit={limit}&apikey={FMP_API_KEY}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code != 200:
            return pd.Series(dtype=float)
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.Series(dtype=float)
        df = pd.DataFrame(rows)
        if "date" not in df.columns or "close" not in df.columns:
            return pd.Series(dtype=float)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        return pd.to_numeric(df["close"], errors="coerce").dropna()
    except Exception:
        return pd.Series(dtype=float)


def _fmp_batch_close_df(tickers: list, limit: int = 130) -> pd.DataFrame:
    """여러 티커 병렬 Close 조회 → DataFrame. (app._fmp_batch_to_close_df 동일 패턴)"""
    if not tickers:
        return pd.DataFrame()
    import concurrent.futures

    def _one(tk):
        return tk, _fmp_price_history_close(tk, limit=limit)

    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_one, tk): tk for tk in tickers}
        for fut in concurrent.futures.as_completed(futs):
            try:
                tk, s = fut.result()
                if s is not None and not s.empty:
                    out[tk] = s
            except Exception:
                pass
    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).sort_index()


def _fmp_profile(ticker: str) -> dict:
    try:
        r = requests.get(f"{_FMP_BASE}/profile?symbol={ticker}&apikey={FMP_API_KEY}", timeout=_FMP_TIMEOUT)
        d = r.json()
        return d[0] if isinstance(d, list) and d else {}
    except Exception:
        return {}


def _fmp_etf_symbol_set() -> set:
    """/stable/etf-list → ETF 심볼 집합 (멤버십 판별용)."""
    try:
        r = requests.get(f"{_FMP_BASE}/etf-list?apikey={FMP_API_KEY}", timeout=15)
        if r.status_code != 200:
            return set()
        data = r.json()
        if not isinstance(data, list):
            return set()
        return {str(it.get("symbol", "")).strip().upper() for it in data if it.get("symbol")}
    except Exception:
        return set()


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
        etf_set = _fmp_etf_symbol_set()
        if not etf_set:
            print("[WARN] ETF 심볼 목록 조회 실패 — 발견 단계 스킵")
            return 0

        today_et = datetime.now(_ET)
        cutoff = today_et - timedelta(days=_DISCOVERY_LOOKBACK_DAYS)
        from_str, to_str = cutoff.strftime("%Y-%m-%d"), today_et.strftime("%Y-%m-%d")
        r = requests.get(f"{_FMP_BASE}/ipos-calendar?from={from_str}&to={to_str}&apikey={FMP_API_KEY}",
                         timeout=15)
        if r.status_code != 200:
            print(f"[WARN] ipos-calendar status {r.status_code} — 발견 단계 스킵")
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
            candidates.append({"ticker": sym,
                               "name": str(it.get("company", "") or it.get("name", "") or "")[:80]})

        # AUM 확인 (profile) — 호출 수 제한
        filtered = []
        for etf in candidates[:60]:
            try:
                p = _fmp_profile(etf["ticker"])
                aum = float(p.get("totalAssets") or p.get("mktCap") or 0) / 1_000_000
                if aum and aum < _DISCOVERY_MIN_AUM_M:
                    continue
                etf["aum_m"] = f"{aum:.0f}" if aum else ""
                etf["category"] = str(p.get("sector") or p.get("industry") or "")[:50]
                filtered.append(etf)
            except Exception:
                etf["aum_m"], etf["category"] = "", ""
                filtered.append(etf)

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
def build_ranked_table(tickers: list) -> pd.DataFrame:
    """티커별 1주·1개월 수익률 → 백분위 정규화 → 가중 점수 → 순위.
    반환 컬럼: rank, Ticker, week_pct, month_pct, score"""
    close_df = _fmp_batch_close_df(tickers, limit=130)
    if close_df.empty:
        return pd.DataFrame()

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
        return pd.DataFrame()

    # 백분위 정규화(0~100, 높을수록 강함) 후 가중합 — raw % 직접 합산 금지
    df["pr_week"]  = df["week_pct"].rank(pct=True) * 100.0
    df["pr_month"] = df["month_pct"].rank(pct=True) * 100.0
    df["score"] = _W_MONTH * df["pr_month"] + _W_WEEK * df["pr_week"]

    df = df.sort_values("score", ascending=False, kind="mergesort").reset_index(drop=True)
    df.insert(0, "rank", np.arange(1, len(df) + 1))
    return df[["rank", "Ticker", "week_pct", "month_pct", "score"]]


# ── [STEP 7] 스냅샷 로드/저장 (1셀 JSON, 드리프트 없음) ───────────────────────
def load_prev_snapshot(gc) -> tuple[dict, str]:
    """지난주 {ticker: rank} 맵과 날짜 반환. 없으면 ({}, "")."""
    try:
        sh = gc.open(_SPREADSHEET_TITLE)
        try:
            ws = sh.worksheet(_SNAPSHOT_SHEET_TITLE)
        except gspread.exceptions.WorksheetNotFound:
            return {}, ""
        raw = ws.acell("A1").value
        if not raw:
            return {}, ""
        obj = json.loads(raw)
        ranks = {str(k).upper(): int(v) for k, v in (obj.get("ranks") or {}).items()}
        return ranks, str(obj.get("date", ""))
    except Exception as e:
        print(f"[WARN] 스냅샷 로드 실패: {e}")
        return {}, ""


def save_snapshot(gc, rank_map: dict, date_str: str) -> None:
    """이번 주 순위 스냅샷 저장(A1 셀 JSON 덮어쓰기)."""
    try:
        sh = gc.open(_SPREADSHEET_TITLE)
        try:
            ws = sh.worksheet(_SNAPSHOT_SHEET_TITLE)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=_SNAPSHOT_SHEET_TITLE, rows=10, cols=2)
        payload = json.dumps({"date": date_str, "ranks": rank_map}, ensure_ascii=False)
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
def compute_actions(ranked: pd.DataFrame, prev_map: dict) -> dict:
    """Top 5 기준 매수/매도 신호 산출."""
    cur_top5 = ranked.head(_HOLD_SLOTS)["Ticker"].tolist()
    cur_rank = dict(zip(ranked["Ticker"], ranked["rank"]))
    prev_top5 = [tk for tk, r in prev_map.items() if r <= _HOLD_SLOTS]

    # 신규 매수: 이번 Top5 중 지난주 Top5에 없던 것
    buys = []
    for tk in cur_top5:
        was_in = tk in prev_map and prev_map[tk] <= _HOLD_SLOTS
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


def build_email_html(ranked: pd.DataFrame, actions: dict, prev_map: dict,
                     prev_date: str, new_added: int) -> str:
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
    for _, r in ranked.head(_TOP_N_EMAIL).iterrows():
        rk = int(r["rank"])
        tk = r["Ticker"]
        in_top5 = rk <= _HOLD_SLOTS
        row_bg = "#13243b" if in_top5 else "#0f172a"
        rk_color = "#60a5fa" if in_top5 else "#64748b"
        rk_label = f'<b style="color:{rk_color};">{rk}</b>' + (" ⭐" if in_top5 else "")
        delta = _delta_badge(tk, rk, prev_map)
        rows_html += (
            f'<tr style="background:{row_bg};">'
            f'<td style="padding:8px 10px;text-align:center;">{rk_label}</td>'
            f'<td style="padding:8px 10px;font-weight:700;color:#e2e8f0;">{tk}</td>'
            f'<td style="padding:8px 10px;text-align:right;color:#cbd5e1;">{_fmt_pct(r["week_pct"])}</td>'
            f'<td style="padding:8px 10px;text-align:right;color:#cbd5e1;">{_fmt_pct(r["month_pct"])}</td>'
            f'<td style="padding:8px 10px;text-align:center;color:#94a3b8;">{r["score"]:.1f}</td>'
            f'<td style="padding:8px 10px;text-align:center;">{delta}</td>'
            f'</tr>'
        )

    discovery_note = (f' · 신규 ETF {new_added}개 추가됨' if new_added else "")
    prev_note = f"지난주 스냅샷: {prev_date}" if prev_date else "지난주 스냅샷 없음(첫 실행)"

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
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:10px;">📊 Top {_TOP_N_EMAIL} 랭킹 (⭐ = Top 5 보유 대상)</div>
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


def send_email(subject: str, html_body: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_TO
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())
        print(f"[OK] 이메일 발송 완료 → {GMAIL_TO}")
        return True
    except Exception as e:
        print(f"[ERROR] 이메일 발송 실패: {e}")
        return False


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"[START] Hidden Alpha 자동화: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    # 주 1회 보장: 이번 ISO 주에 이미 발송(스냅샷 저장)했으면 스킵
    gc = get_gspread_client()
    prev_map, prev_date = load_prev_snapshot(gc)
    if not _FORCE_RUN and _same_iso_week(prev_date, datetime.now(_ET)):
        print(f"[SKIP] 이번 주(스냅샷 {prev_date})에 이미 발송됨. 종료. (강제 실행은 HIDDEN_ALPHA_FORCE=1)")
        sys.exit(0)

    uni_ws = open_etf_universe_worksheet(gc)

    # [STEP 1] 신규 ETF 발견 → 추가 (삭제/정리는 앱에서 수동)
    print("[STEP 1] 신규 ETF 발견 중...")
    new_added = discover_and_add_new_etfs(uni_ws)

    # [STEP 2] 누적 유니버스 전체 로드
    print("[STEP 2] ETF_Universe 전체 로드 중...")
    tickers = load_universe_tickers(uni_ws)
    print(f"[INFO] 유니버스 {len(tickers)}개 티커")
    if not tickers:
        print("[ERROR] 유니버스가 비어 있음. 종료.")
        sys.exit(1)

    # [STEP 3·4] 수익률 → 점수 → 랭킹
    print("[STEP 3-4] 수익률 계산·점수화·랭킹 중...")
    ranked = build_ranked_table(tickers)
    if ranked.empty:
        print("[ERROR] 랭킹 산출 실패(데이터 부족/네트워크). 종료.")
        sys.exit(1)
    print(f"[INFO] 랭킹 산출 {len(ranked)}개. Top 5: {ranked.head(5)['Ticker'].tolist()}")

    # [STEP 5] 지난주 스냅샷(상단에서 이미 로드) → 액션·Δ
    print("[STEP 5] 스냅샷 비교 중...")
    actions = compute_actions(ranked, prev_map)

    # [STEP 6] 이메일 발송
    print("[STEP 6] 이메일 발송 중...")
    top5_str = ", ".join(ranked.head(5)["Ticker"].tolist())
    n_buy, n_sell = len(actions["buys"]), len(actions["sells"])
    tag = ""
    if actions["has_prev"] and (n_buy or n_sell):
        tag = f" · 🔁 매수{n_buy}/매도{n_sell}"
    subject = f"💰 [Hidden Alpha] Top5: {top5_str}{tag} · {datetime.now(_ET).strftime('%m/%d')}"
    html_body = build_email_html(ranked, actions, prev_map, prev_date, new_added)
    send_email(subject, html_body)

    # [STEP 7] 이번 주 스냅샷 저장 (Top 30만 — Δ 계산엔 충분)
    print("[STEP 7] 스냅샷 저장 중...")
    snap = {row["Ticker"]: int(row["rank"]) for _, row in ranked.head(30).iterrows()}
    save_snapshot(gc, snap, datetime.now(_ET).strftime("%Y-%m-%d"))

    print(f"[DONE] 완료: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
