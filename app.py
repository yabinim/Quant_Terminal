import sys

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import altair as alt
import feedparser
from google import genai
from google.genai import types as genai_types
import json
import html
import time
import traceback

import numpy as np
import pandas as pd
import pytz
import requests
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from fredapi import Fred


st.set_page_config(page_title="장기 투자 주식 분석", layout="wide")

if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
if "user_role" not in st.session_state:
    st.session_state["user_role"] = None
if "login_error" not in st.session_state:
    st.session_state["login_error"] = False
if "login_feedback" not in st.session_state:
    st.session_state["login_feedback"] = ""
if "login_user_id" not in st.session_state:
    st.session_state["login_user_id"] = ""
if "user_id" not in st.session_state:
    st.session_state["user_id"] = ""


_QUANT_DB_SPREADSHEET_TITLE = "Quant_DB"
_USERS_WORKSHEET_TITLE = "Users"
_USER_SHEET_COLS = ["ID", "Password", "Reason", "Source", "Status"]
_NARRATIVES_WORKSHEET_TITLE = "Narratives"
_NARRATIVES_SHEET_COLS = ["ID", "Date", "Category", "Title", "Content", "Winners", "Emerging"]
_PORTFOLIOS_WORKSHEET_TITLE = "Portfolios"
_PORTFOLIOS_SHEET_COLS = ["ID", "Account", "Ticker", "AvgPrice", "Quantity", "Date_Added"]
_PORTFOLIOS_LEGACY_HEADER = ["ID", "Ticker", "AvgPrice", "Quantity", "Date_Added"]
_TRADE_HISTORY_WORKSHEET_TITLE = "Trade_History"
_TRADE_HISTORY_SHEET_COLS = ["user_id", "account", "ticker", "action", "shares", "price", "date", "memo"]
_DRG_PREDICTIONS_WORKSHEET_TITLE = "DRG_Predictions"
_DRG_PREDICTIONS_SHEET_COLS = [
    "user_id", "pred_date", "direction", "sector_filter", "benchmark_etf",
    "spy_close_at_pred", "full_text", "actual_direction", "actual_return_pct",
    "is_correct", "review_comment"
]
_NAV_ADMIN_APPROVAL = "👑 [관리자] 유저 승인"
_THESIS_WORKSHEET_TITLE = "Thesis"
_WATCHLIST_SHEET_TITLE = "Watchlist"
_ETF_UNIVERSE_SHEET_TITLE = "ETF_Universe"
_EMERGING_TRACKER_SHEET_TITLE = "Emerging_Tracker"
_PORTFOLIO_HISTORY_SHEET_TITLE = "Portfolio_History"
_SCANNER_HISTORY_SHEET_TITLE = "Scanner_History"
_SCANNER_HISTORY_COLS = ["ID", "Date", "Engine", "Ticker", "Score", "Rank", "Verdict", "RS_Score", "Mom_1M"]
_SCANNER_LAST_RESULT_SHEET_TITLE = "Scanner_Last_Result"
_SCANNER_LAST_RESULT_COLS = ["ID", "Engine", "Saved_At", "Universe_Count", "Score_JSON"]
_PORTFOLIO_HISTORY_COLS = ["ID", "Date", "Total_Value", "Total_Cost", "Return_Pct", "SPY_Pct", "Alpha_Pct", "Positions"]
_EMERGING_TRACKER_COLS = ["ID", "Ticker", "Theme", "First_Seen", "Last_Seen", "Count", "Best_Verdict", "RS_Score", "Status"]
_ETF_UNIVERSE_SHEET_COLS = ["Ticker", "Name", "Category", "AUM_M", "Added_Date", "Source"]
_ETF_AUTO_UPDATE_INTERVAL_DAYS = 7  # 자동 업데이트 주기
_WATCHLIST_SHEET_COLS = ["ID", "Ticker", "Memo", "Alert_Price", "Alert_RSI", "Alert_MA200", "Saved_Price", "Date_Added", "Stop_Loss", "Target_Price"]
_WL_NCOL = len(_WATCHLIST_SHEET_COLS)  # =10. 과거 8열 시트는 읽을 때 빈 칸으로 패딩되어 호환됨.
_THESIS_SHEET_COLS = ["ID", "Ticker", "Account", "Thesis_Title", "Narrative_Category", "Narrative_Date", "Date_Added"]


@st.cache_resource
def get_gspread_client():
    """`st.secrets['gcp_service_account']` 서비스 계정으로 gspread 클라이언트 생성. 미설정 시 None.
    @st.cache_resource: 앱 전체 세션에서 OAuth 인증을 1회만 수행하고 클라이언트를 재사용.
    (기존: open_*_worksheet 10개가 각각 독립 인증 → 매 호출마다 새 auth 요청)
    """
    try:
        info = dict(st.secrets["gcp_service_account"])
    except (KeyError, FileNotFoundError, TypeError):
        return None
    scopes = (
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    )
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def _safe_append_rows(ws, rows, value_input_option: str = "USER_ENTERED") -> None:
    """gspread append_row/append_rows의 '계단식 드리프트' 버그를 회피하는 안전한 행 추가.

    원인: append_row/append_rows를 table_range 없이 호출하면 Google Sheets API가
    테이블의 시작 열을 잘못 감지해, 호출할 때마다 데이터가 오른쪽으로 밀려 기록된다
    (예: 1행 A열 → 2행 F열 → 3행 K열). 밀린 행은 A열(user_id)이 비어 있어
    A열 기준으로 읽는 로직에서 전부 누락된다.

    이 함수는 항상 A열을 기준으로 '마지막 데이터 행 다음'에 기록하므로 드리프트가 없다.
    rows: 단일 행(list) 또는 여러 행(list[list]) 모두 허용.
    """
    if rows is None:
        return
    # 단일 행을 2차원으로 정규화
    if len(rows) > 0 and not isinstance(rows[0], (list, tuple)):
        rows = [rows]
    rows = [list(r) for r in rows if r is not None]
    if not rows:
        return

    existing = ws.get_all_values() or []
    # 값이 하나라도 있는 마지막 행 번호 (중간 빈 행은 무시)
    last_row = 0
    for idx, r in enumerate(existing, start=1):
        if any(str(c).strip() != "" for c in r):
            last_row = idx
    start_row = last_row + 1
    end_row = start_row + len(rows) - 1

    # 그리드 용량 부족 시 행 확장 (values.update는 시트를 자동 확장하지 않음)
    try:
        if end_row > ws.row_count:
            ws.add_rows(end_row - ws.row_count + 50)
    except Exception:
        pass

    n_cols = max(len(r) for r in rows)
    end_cell = gspread.utils.rowcol_to_a1(end_row, n_cols)  # 예: "H3"
    ws.update(rows, range_name=f"A{start_row}:{end_cell}",
              value_input_option=value_input_option)


def open_users_worksheet():
    """Quant_DB 스프레드시트의 Users 탭. (worksheet | None, err_msg | None)"""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정(`gcp_service_account`)이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        ws = sh.worksheet(_USERS_WORKSHEET_TITLE)
        return ws, None
    except Exception as exc:
        return None, f"스프레드시트 `{_QUANT_DB_SPREADSHEET_TITLE}` / `{_USERS_WORKSHEET_TITLE}` 를 열 수 없습니다: {exc}"


def open_narratives_worksheet():
    """Quant_DB 스프레드시트의 Narratives 탭. (worksheet | None, err_msg | None)"""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정(`gcp_service_account`)이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        ws = sh.worksheet(_NARRATIVES_WORKSHEET_TITLE)
        return ws, None
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg or "unable to find" in msg:
            try:
                sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
                ws = sh.add_worksheet(title=_NARRATIVES_WORKSHEET_TITLE, rows=3000, cols=7)
                ensure_narratives_header_row(ws)
                return ws, None
            except Exception as exc2:
                return None, f"`{_NARRATIVES_WORKSHEET_TITLE}` 워크시트를 만들 수 없습니다: {exc2}"
        return None, f"스프레드시트 `{_QUANT_DB_SPREADSHEET_TITLE}` / `{_NARRATIVES_WORKSHEET_TITLE}` 를 열 수 없습니다: {exc}"


def open_portfolios_worksheet():
    """Quant_DB 스프레드시트의 Portfolios 탭. (worksheet | None, err_msg | None)"""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정(`gcp_service_account`)이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        ws = sh.worksheet(_PORTFOLIOS_WORKSHEET_TITLE)
        return ws, None
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg or "unable to find" in msg:
            try:
                sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
                ws = sh.add_worksheet(title=_PORTFOLIOS_WORKSHEET_TITLE, rows=3000, cols=6)
                ensure_portfolios_header_row(ws)
                return ws, None
            except Exception as exc2:
                return None, f"`{_PORTFOLIOS_WORKSHEET_TITLE}` 워크시트를 만들 수 없습니다: {exc2}"
        return None, f"스프레드시트 `{_QUANT_DB_SPREADSHEET_TITLE}` / `{_PORTFOLIOS_WORKSHEET_TITLE}` 를 열 수 없습니다: {exc}"


def open_thesis_worksheet():
    """Quant_DB 스프레드시트의 Thesis 탭. 없으면 자동 생성."""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정(`gcp_service_account`)이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        existing_titles = [ws.title for ws in sh.worksheets()]
        if _THESIS_WORKSHEET_TITLE in existing_titles:
            return sh.worksheet(_THESIS_WORKSHEET_TITLE), None
        ws = sh.add_worksheet(title=_THESIS_WORKSHEET_TITLE, rows=3000, cols=7)
        ws.update([_THESIS_SHEET_COLS], range_name="A1:G1", value_input_option="USER_ENTERED")
        return ws, None
    except Exception as exc:
        return None, f"Thesis 워크시트 열기/생성 실패: {exc}"


def save_thesis_row(user_id: str, ticker: str, account: str, thesis_title: str, narrative_category: str, narrative_date: str) -> tuple[bool, str]:
    """Thesis 시트에 한 행 저장."""
    ws, err = open_thesis_worksheet()
    if err:
        return False, err
    try:
        date_added = _narrative_now_et_string()
        row = [
            str(user_id).strip(),
            str(ticker).strip().upper(),
            str(account).strip(),
            str(thesis_title).strip(),
            str(narrative_category).strip(),
            str(narrative_date).strip(),
            date_added,
        ]
        _safe_append_rows(ws, row, value_input_option="USER_ENTERED")
        return True, ""
    except Exception as exc:
        return False, str(exc)


def load_thesis_records(user_id: str) -> pd.DataFrame:
    """현재 user_id의 Thesis 기록 전체를 DataFrame으로 반환."""
    empty_df = pd.DataFrame(columns=_THESIS_SHEET_COLS)
    ws, err = open_thesis_worksheet()
    if err:
        return empty_df
    try:
        vals = ws.get_all_values()
        if not vals or len(vals) < 2:
            return empty_df
        rows = []
        uid_u = str(user_id).strip().upper()
        for r in vals[1:]:
            r_padded = (r + [""] * 7)[:7]
            if str(r_padded[0]).strip().upper() != uid_u:
                continue
            rows.append({
                "ID": r_padded[0],
                "Ticker": r_padded[1],
                "Account": r_padded[2],
                "Thesis_Title": r_padded[3],
                "Narrative_Category": r_padded[4],
                "Narrative_Date": r_padded[5],
                "Date_Added": r_padded[6],
            })
        if not rows:
            return empty_df
        return pd.DataFrame(rows)
    except Exception:
        return empty_df


def delete_thesis_row(user_id: str, ticker: str, thesis_title: str) -> tuple[bool, str]:
    """Thesis 시트에서 특정 행 삭제."""
    ws, err = open_thesis_worksheet()
    if err:
        return False, err
    try:
        vals = ws.get_all_values()
        uid_u = str(user_id).strip().upper()
        tk_u = str(ticker).strip().upper()
        th_u = str(thesis_title).strip()
        rows_to_delete = []
        for i, r in enumerate(vals[1:], start=2):
            r_padded = (r + [""] * 7)[:7]
            if (str(r_padded[0]).strip().upper() == uid_u
                    and str(r_padded[1]).strip().upper() == tk_u
                    and str(r_padded[3]).strip() == th_u):
                rows_to_delete.append(i)
        for row_idx in reversed(rows_to_delete):
            ws.delete_rows(row_idx)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def get_thesis_options_from_narratives(user_id: str) -> list[dict]:
    """최근 내러티브에서 Thesis 선택 옵션 생성 (최신 10개 테마)."""
    records, _ = fetch_narrative_records_from_sheet()
    if not records:
        return []
    uid_u = str(user_id).strip().upper()
    options = []
    seen = set()
    for rec in reversed(records):
        if str(rec.get("_sheet_user_id", "")).strip().upper() != uid_u:
            continue
        analysis = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
        saved_at = rec.get("saved_at", "")
        date_str = ""
        try:
            dt = _narrative_parse_saved_at_utc(saved_at)
            if dt:
                date_str = dt.astimezone(_MARKET_ET_TZ).strftime("%m/%d")
        except Exception:
            pass
        category = str(analysis.get("source") or "market_narrative").strip()
        themes = analysis.get("themes", [])
        if not isinstance(themes, list):
            themes = []
        for theme in themes[:5]:
            if not isinstance(theme, dict):
                continue
            title = str(theme.get("title", "") or "").strip()
            if not title:
                continue
            key = f"{title}_{date_str}"
            if key in seen:
                continue
            seen.add(key)
            options.append({
                "label": f"[{date_str}] {title}",
                "thesis_title": title,
                "narrative_category": category,
                "narrative_date": date_str,
            })
            if len(options) >= 15:
                break
        if len(options) >= 15:
            break
    return options


def ensure_portfolios_header_row(ws):
    vals = ws.get_all_values()
    if not vals or not any(str(c).strip() for c in vals[0]):
        ws.update([_PORTFOLIOS_SHEET_COLS], range_name="A1:F1", value_input_option="USER_ENTERED")
        return
    if _portfolio_sheet_header_kind(vals[0]) != "new":
        ws.update([_PORTFOLIOS_SHEET_COLS], range_name="A1:F1", value_input_option="USER_ENTERED")


def _portfolio_sheet_header_kind(header_row: list) -> str:
    """'new' (ID+Account+…) | 'legacy' (Account 열 없음) | 'unknown'."""
    h = [str(x).strip() for x in (header_row or [])[:6]]
    if len(h) >= 6 and h[:6] == _PORTFOLIOS_SHEET_COLS:
        return "new"
    h5 = [str(x).strip() for x in (header_row or [])[:5]]
    if h5 == _PORTFOLIOS_LEGACY_HEADER:
        return "legacy"
    return "unknown"


def _portfolio_row_to_new_six_cells(header_kind: str, row: list) -> list | None:
    """데이터 행을 항상 [ID, Account, Ticker, AvgPrice, Quantity, Date_Added] 6칸으로 맞춘다."""
    row = list(row or [])
    effective = header_kind
    if header_kind == "new" and len(row) <= 5:
        t_candidate = str(row[1]).strip().upper() if len(row) > 1 else ""
        if len(row) >= 5 and is_valid_scanner_ticker(t_candidate):
            effective = "legacy"
    if effective == "legacy":
        row = row + [""] * 6
        rid = str(row[0]).strip()
        tk = str(row[1]).strip().upper()
        if not rid or not tk:
            return None
        return [
            rid,
            "Default",
            tk,
            row[2],
            row[3],
            str(row[4]).strip() if len(row) > 4 and str(row[4]).strip() else _narrative_now_et_string(),
        ]
    row = row + [""] * 6
    rid = str(row[0]).strip()
    acct = str(row[1]).strip()
    tk = str(row[2]).strip().upper()
    if not rid or not acct or not tk:
        return None
    dt = str(row[5]).strip() if len(row) > 5 and str(row[5]).strip() else _narrative_now_et_string()
    return [rid, acct, tk, row[3], row[4], dt]


def ensure_narratives_header_row(ws):
    vals = ws.get_all_values()
    if not vals or not any(str(c).strip() for c in vals[0]):
        ws.update([_NARRATIVES_SHEET_COLS], range_name="A1:G1", value_input_option="USER_ENTERED")
        return
    hdr = [str(h).strip() for h in vals[0][:7]]
    if hdr != _NARRATIVES_SHEET_COLS:
        ws.update([_NARRATIVES_SHEET_COLS], range_name="A1:G1", value_input_option="USER_ENTERED")


def _looks_local_timestamp(s: str) -> bool:
    s = str(s or "").strip()
    return bool(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:", s))


def _normalize_narrative_sheet_row(row: list) -> list:
    """레거시 4~6열 행을 7열 [ID..Emerging] 로 맞춘다."""
    row = list(row or [])
    if len(row) >= 7:
        return (row[:7] + [""] * 7)[:7]
    # 레거시 6열: ID, Date, Category, Title, Content, Tickers(단일)
    if len(row) == 6:
        s0 = str(row[0] or "").strip()
        if s0 and not _looks_local_timestamp(s0):
            return [
                str(row[0]).strip(),
                str(row[1]).strip(),
                str(row[2]).strip(),
                str(row[3]).strip(),
                str(row[4]).strip(),
                str(row[5]).strip(),
                "",
            ]
    s0 = str(row[0]).strip() if row else ""
    if len(row) >= 4 and _looks_local_timestamp(s0):
        c = str(row[3]).strip() if len(row) > 3 else ""
        if c.startswith("{") or c.startswith("["):
            if len(row) == 5:
                return ["", row[0], row[1], row[2], row[3], str(row[4]).strip(), ""]
            wleg = str(row[4]).strip() if len(row) > 4 else ""
            eleg = str(row[5]).strip() if len(row) > 5 else ""
            ex6 = str(row[6]).strip() if len(row) > 6 else ""
            if ex6:
                return ["", row[0], row[1], row[2], row[3], wleg, eleg or ex6]
            return ["", row[0], row[1], row[2], row[3], wleg, eleg]
    return (row + [""] * 7)[:7]


def _narrative_now_et_string(dt_utc=None) -> str:
    """UTC 시각을 ET(미 동부) 문자열 YYYY-MM-DD HH:MM:SS 로."""
    u = dt_utc or datetime.now(timezone.utc)
    if u.tzinfo is None:
        u = u.replace(tzinfo=timezone.utc)
    return u.astimezone(_MARKET_ET_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _narrative_et_date_string_to_utc_iso(date_kst_str: str) -> str | None:
    """시트 Date 열(ET, 미 동부) → UTC ISO 문자열."""
    if not date_kst_str or not str(date_kst_str).strip():
        return None
    try:
        naive = datetime.strptime(str(date_kst_str).strip(), "%Y-%m-%d %H:%M:%S")
        loc = _MARKET_ET_TZ.localize(naive)
        return loc.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _narrative_sheet_title_for_record(rec: dict) -> str:
    analysis = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
    base = _narrative_core_theme_display(analysis, max_chars=100)
    if analysis.get("source") == _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
        return "주간 트렌드(7일) 브리핑"
    if analysis.get("source") == "wow_trend_7d":
        return "⚖️ 트렌드 변곡점 (이번 주 vs 저번 주)"
    return base if base and base != "N/A" else "시장 내러티브 스냅샷"


def winners_tickers_from_theme_analysis(analysis):
    """themes[].winners 텍스트에서 티커만 추출 (strip·대문자·검증은 filter_scanner_ticker_list)."""
    if not isinstance(analysis, dict):
        return []
    themes = analysis.get("themes", [])
    if not isinstance(themes, list):
        themes = analysis.get("Themes", []) if isinstance(analysis.get("Themes"), list) else []
    if not isinstance(themes, list):
        themes = []
    out = []
    seen = set()
    for theme in themes:
        theme = theme if isinstance(theme, dict) else {}
        for ticker in parse_tickers_from_text(theme.get("winners", "") or ""):
            t = str(ticker).strip().upper()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return filter_scanner_ticker_list(out)


def emerging_tickers_from_theme_analysis(analysis):
    """themes[].expanding_to[].expected_tickers 에서 티커만 추출."""
    if not isinstance(analysis, dict):
        return []
    themes = analysis.get("themes", [])
    if not isinstance(themes, list):
        themes = analysis.get("Themes", []) if isinstance(analysis.get("Themes"), list) else []
    if not isinstance(themes, list):
        themes = []
    out = []
    seen = set()
    for theme in themes:
        theme = theme if isinstance(theme, dict) else {}
        expanding_to_data = theme.get("expanding_to", [])
        if not isinstance(expanding_to_data, list):
            continue
        for flow in expanding_to_data:
            flow = flow if isinstance(flow, dict) else {}
            for ticker in parse_tickers_from_text(flow.get("expected_tickers", "") or ""):
                t = str(ticker).strip().upper()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    return filter_scanner_ticker_list(out)


def _narrative_record_to_sheet_row(rec: dict, owner_id: str) -> list:
    """내부 레코드 dict → Narratives 시트 한 행 [ID, Date, Category, Title, Content, Winners, Emerging]."""
    rec_out = {k: v for k, v in rec.items() if not str(k).startswith("_sheet")}
    analysis = rec_out.get("analysis") if isinstance(rec_out.get("analysis"), dict) else {}
    dt_utc = _narrative_parse_saved_at_utc(rec_out.get("saved_at"))
    if dt_utc is None:
        dt_utc = datetime.now(timezone.utc)
    date_kst = _narrative_now_et_string(dt_utc)
    category = str(analysis.get("source") or "market_narrative").strip() or "market_narrative"
    title = _narrative_sheet_title_for_record(rec_out)
    if len(title) > 500:
        title = title[:497] + "..."

    # Google Sheets 셀 한도 50,000자 — weekly/wow 브리핑은 마크다운이 길어 초과할 수 있음
    # weekly_briefing_markdown 을 먼저 20,000자로 트리밍한 rec_out 을 직렬화
    _SHEET_CELL_LIMIT = 49000
    rec_for_json = rec_out
    md_key = "weekly_briefing_markdown"
    analysis_for_json = dict(analysis)
    if md_key in analysis_for_json and len(str(analysis_for_json.get(md_key) or "")) > 20000:
        analysis_for_json[md_key] = str(analysis_for_json[md_key])[:20000] + "\n\n…(truncated)"
        rec_for_json = dict(rec_out)
        rec_for_json["analysis"] = analysis_for_json
    content = json.dumps(rec_for_json, ensure_ascii=False)
    if len(content) > _SHEET_CELL_LIMIT:
        # 그래도 넘으면 summary 도 트리밍
        analysis_for_json["summary"] = str(analysis_for_json.get("summary") or "")[:5000]
        rec_for_json = dict(rec_for_json)
        rec_for_json["analysis"] = analysis_for_json
        content = json.dumps(rec_for_json, ensure_ascii=False)
    if len(content) > _SHEET_CELL_LIMIT:
        content = content[:_SHEET_CELL_LIMIT]

    w_list: list = []
    e_list: list = []
    try:
        w_list = winners_tickers_from_theme_analysis(analysis)
        e_list = emerging_tickers_from_theme_analysis(analysis)
    except Exception:
        pass
    if (not w_list and not e_list) and str(analysis.get("source") or "") == _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
        pre = analysis.get("precomputed_universe")
        if isinstance(pre, list):
            try:
                w_list = filter_scanner_ticker_list([str(x).strip().upper() for x in pre if str(x).strip()])
            except Exception:
                w_list = []
    w_csv = ",".join(w_list)
    e_csv = ",".join(e_list)
    return [str(owner_id).strip(), date_kst, category, title, content, w_csv, e_csv]


def _sheet_row_to_narrative_record(row: list) -> dict | None:
    """시트 데이터 행 → 내부 레코드 dict."""
    cells = _normalize_narrative_sheet_row(row)
    rid = cells[0]
    date_kst = cells[1]
    category = cells[2]
    title = cells[3]
    content = cells[4]
    winners_csv = str(cells[5]).strip() if len(cells) > 5 else ""
    emerging_csv = str(cells[6]).strip() if len(cells) > 6 else ""
    content = str(content or "").strip()
    if not content:
        return None
    try:
        envelope = json.loads(content)
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    if isinstance(envelope.get("analysis"), dict):
        if not envelope.get("saved_at") and date_kst:
            iso_guess = _narrative_et_date_string_to_utc_iso(date_kst)
            if iso_guess:
                envelope = dict(envelope)
                envelope["saved_at"] = iso_guess
        envelope = dict(envelope)
        envelope["_sheet_user_id"] = str(rid).strip()
        envelope["_sheet_winners_csv"] = winners_csv
        envelope["_sheet_emerging_csv"] = emerging_csv
        return envelope
    return None


@st.cache_data(ttl=60)
def _narratives_sheet_all_values_cached():
    try:
        ws, err = open_narratives_worksheet()
        if err or ws is None:
            return None, err
        return (ws.get_all_values(), None)
    except Exception as exc:
        return None, str(exc)


def _invalidate_narratives_sheet_cache():
    try:
        _narratives_sheet_all_values_cached.clear()
    except Exception:
        pass


def append_narrative_row_to_sheet(row_values: list) -> tuple[bool, str]:
    """Narratives 시트에 한 행 append. row_values = [ID, Date, Category, Title, Content, Winners, Emerging]."""
    ws, err = open_narratives_worksheet()
    if err:
        return False, err
    try:
        ensure_narratives_header_row(ws)
        _safe_append_rows(ws, list(row_values)[:7], value_input_option="USER_ENTERED")
        _invalidate_narratives_sheet_cache()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def fetch_narrative_records_from_sheet() -> tuple[list, str | None]:
    """Narratives 시트 전체를 읽어 내부 레코드 리스트(시간순 오래된 것 먼저)로 반환."""
    vals, err = _narratives_sheet_all_values_cached()
    if err:
        return [], err
    if not vals or len(vals) < 2:
        return [], None
    hdr = [str(h).strip() for h in vals[0][:7]]
    if hdr != _NARRATIVES_SHEET_COLS:
        try:
            ws2, err2 = open_narratives_worksheet()
            if err2:
                return [], err2
            ensure_narratives_header_row(ws2)
            _invalidate_narratives_sheet_cache()
            vals, err = _narratives_sheet_all_values_cached()
        except Exception as exc:
            return [], str(exc)
        if err or not vals or len(vals) < 2:
            return [], err
    records = []
    for r in vals[1:]:
        rec = _sheet_row_to_narrative_record(r)
        if rec and isinstance(rec.get("analysis"), dict):
            if not str(rec.get("session_label") or "").strip():
                dtu = _narrative_parse_saved_at_utc(rec.get("saved_at"))
                rec = dict(rec)
                rec["session_label"] = narrative_session_label_at_utc(dtu) if dtu else ""
            records.append(rec)
    records.sort(
        key=lambda r: _narrative_parse_saved_at_utc(r.get("saved_at")) or datetime.min.replace(tzinfo=timezone.utc)
    )
    return records, None


def save_narrative_history_records_merge_user(owner_id: str, records: list) -> tuple[bool, str]:
    """현재 사용자(owner_id) 행만 교체하고 나머지 사용자 행은 유지한다."""
    owner_id = str(owner_id or "").strip()
    if not owner_id:
        return False, "user_id 가 비어 있습니다."
    ws, err = open_narratives_worksheet()
    if err:
        return False, err
    try:
        _invalidate_narratives_sheet_cache()
        ensure_narratives_header_row(ws)
        vals = ws.get_all_values() or []
        header = _NARRATIVES_SHEET_COLS
        body = []
        oid_u = owner_id.upper()
        for r in vals[1:]:
            cells = _normalize_narrative_sheet_row(r)
            rid = str(cells[0]).strip().upper()
            if rid == oid_u:
                continue
            body.append(cells)
        rows = [header]
        rows.extend(body)
        for rec in records or []:
            if isinstance(rec, dict) and isinstance(rec.get("analysis"), dict):
                rows.append(_narrative_record_to_sheet_row(rec, owner_id))
        ws.clear()
        ws.update(rows, range_name=f"A1:G{len(rows)}", value_input_option="USER_ENTERED")
        _invalidate_narratives_sheet_cache()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def save_narrative_history_records(records):
    """현재 세션 user_id 기준으로 Narratives 시트를 머지 저장(prune 후 동기화용)."""
    uid = str(st.session_state.get("user_id") or "").strip()
    if not uid:
        return
    ok, err = save_narrative_history_records_merge_user(uid, records or [])
    if not ok and err:
        try:
            st.error(f"내러티브 시트 동기화 실패: {err}")
        except Exception:
            pass


def ensure_users_header_row(ws):
    vals = ws.get_all_values()
    if not vals or not any(str(c).strip() for c in vals[0]):
        ws.update([_USER_SHEET_COLS], range_name="A1:E1", value_input_option="USER_ENTERED")


def fetch_users_dataframe(ws) -> pd.DataFrame:
    vals = ws.get_all_values()
    if not vals:
        return pd.DataFrame(columns=_USER_SHEET_COLS)
    hdr = [str(h).strip() for h in vals[0][:5]]
    body = []
    for r in vals[1:]:
        row = (r + [""] * 5)[:5]
        body.append(row)
    if not hdr or not any(hdr):
        return pd.DataFrame(columns=_USER_SHEET_COLS)
    df = pd.DataFrame(body, columns=hdr[:5])
    for col in _USER_SHEET_COLS:
        if col not in df.columns:
            df[col] = ""
    return df[_USER_SHEET_COLS]


def register_pending_user(user_id: str, password: str, reason: str, source: str) -> tuple[bool, str]:
    if not user_id or not password:
        return False, "ID와 Password는 필수입니다."
    ws, err = open_users_worksheet()
    if err:
        return False, err
    ensure_users_header_row(ws)
    df = fetch_users_dataframe(ws)
    if not df.empty:
        existing = df["ID"].astype(str).str.strip().str.upper()
        if user_id.strip().upper() in set(existing):
            return False, "이미 등록된 ID입니다."
    _safe_append_rows(ws, [user_id.strip(), password, reason, source, "pending"], value_input_option="USER_ENTERED")
    return True, ""


def save_users_worksheet_from_df(df: pd.DataFrame) -> tuple[bool, str]:
    ws, err = open_users_worksheet()
    if err:
        return False, err
    df = df.copy()
    for col in _USER_SHEET_COLS:
        if col not in df.columns:
            df[col] = ""
    df = df[_USER_SHEET_COLS]
    values = [_USER_SHEET_COLS] + df.astype(str).values.tolist()
    ws.clear()
    rng = f"A1:E{len(values)}"
    ws.update(values, range_name=rng, value_input_option="USER_ENTERED")
    return True, ""


def append_row_to_google_sheet(_worksheet_name: str, _row_values: list) -> None:
    """레거시 호환 스텁(미사용). Users 가입은 `register_pending_user`를 사용."""
    _ = get_gspread_client()
    return


def sync_portfolio_snapshot_to_sheet(_payload: dict) -> None:
    """포트폴리오 스냅샷을 시트에 동기화(추후 구현)."""
    _ = get_gspread_client()
    return


def try_sheet_login():
    """로그인 탭 버튼 on_click: 시트 검증 또는 admin_pw 백도어."""
    st.session_state["login_feedback"] = ""
    st.session_state["login_error"] = False
    st.session_state["user_id"] = ""
    uid = str(st.session_state.get("login_id_input", "") or "").strip()
    pw = str(st.session_state.get("login_pw_input", "") or "").strip()
    if not uid or not pw:
        st.session_state["login_error"] = True
        st.session_state["login_feedback"] = "ID와 비밀번호를 모두 입력하세요."
        return
    try:
        admin_pw = str(st.secrets["passwords"]["admin_pw"])
    except (KeyError, FileNotFoundError, TypeError):
        admin_pw = ""
    if admin_pw and pw == admin_pw:
        st.session_state["logged_in"] = True
        st.session_state["user_role"] = "admin"
        st.session_state["login_user_id"] = uid
        st.session_state["user_id"] = uid
        st.session_state["login_error"] = False
        st.session_state["login_feedback"] = ""
        # 로그인 직후 DRG·VIX·거시지표 백그라운드 워밍업
        _trigger_background_warmup()
        return
    ws, err = open_users_worksheet()
    if err:
        st.session_state["login_error"] = True
        st.session_state["login_feedback"] = err
        return
    df = fetch_users_dataframe(ws)
    if df.empty:
        st.session_state["login_error"] = True
        st.session_state["login_feedback"] = "등록된 사용자가 없습니다. 먼저 가입 신청을 해 주세요."
        return
    key = uid.strip().upper()
    id_col = df["ID"].astype(str).str.strip().str.upper()
    match = df[id_col == key]
    if match.empty:
        st.session_state["login_error"] = True
        st.session_state["login_feedback"] = "등록되지 않은 ID입니다."
        return
    row = match.iloc[0]
    status = str(row.get("Status", "")).strip().lower()
    if status == "pending":
        st.session_state["login_error"] = True
        st.session_state["login_feedback"] = "관리자의 승인을 대기 중입니다."
        return
    if status == "rejected":
        st.session_state["login_error"] = True
        st.session_state["login_feedback"] = "접근이 거부되었습니다."
        return
    if status == "approved":
        if str(row.get("Password", "")) == pw:
            st.session_state["logged_in"] = True
            st.session_state["user_role"] = "guest"
            lid = str(row.get("ID", "")).strip()
            st.session_state["login_user_id"] = lid
            st.session_state["user_id"] = lid
            st.session_state["login_error"] = False
            st.session_state["login_feedback"] = ""
            # 로그인 직후 DRG·VIX·거시지표 백그라운드 워밍업
            _trigger_background_warmup()
        else:
            st.session_state["login_error"] = True
            st.session_state["login_feedback"] = "비밀번호가 올바르지 않습니다."
        return
    st.session_state["login_error"] = True
    st.session_state["login_feedback"] = f"알 수 없는 계정 상태입니다: {row.get('Status', '')}"


def _trigger_background_warmup():
    """로그인 직후 한 번만 실행: DRG·거시지표·VIX 캐시를 백그라운드 스레드에서 미리 워밍업.
    이미 캐시가 있으면 즉시 반환, 없으면 논블로킹으로 채워 둠.
    """
    import threading as _threading
    if st.session_state.get("_warmup_done"):
        return
    st.session_state["_warmup_done"] = True

    def _warmup():
        try:
            # VIX 캐시 (DRG·거시지표 공통)
            fetch_vix_latest_and_history()
        except Exception:
            pass
        try:
            # 거시지표 히스토리 캐시
            fetch_macro_history_series()
        except Exception:
            pass
        try:
            # DRG (전체 섹터 기본값으로 프리워밍)
            compute_daily_risk_gauge(sector_filter="전체")
        except Exception:
            pass
        try:
            # 거시경제 대시보드
            cached_analyze_us_macro_dashboard()
        except Exception:
            pass

    t = _threading.Thread(target=_warmup, daemon=True)
    t.start()


def _render_login_screen():
    st.title("Quant Stock Analyzer")
    st.caption("Google Sheets(Quant_DB / Users) 기반 회원 · 관리자 승인제입니다.")
    tab_login, tab_signup = st.tabs(["로그인", "Sign Up (가입 신청)"])

    with tab_login:
        st.text_input("ID", key="login_id_input")
        st.text_input("비밀번호", type="password", key="login_pw_input")
        st.button("로그인", key="login_submit_btn", type="primary", use_container_width=True, on_click=try_sheet_login)
        if st.session_state.get("login_feedback"):
            st.error(st.session_state["login_feedback"])

    with tab_signup:
        with st.form("signup_form", clear_on_submit=True):
            su_id = st.text_input("ID", key="signup_form_id")
            su_pw = st.text_input("Password", type="password", key="signup_form_pw")
            su_reason = st.text_area("이 앱을 쓰려는 이유", key="signup_form_reason")
            su_source = st.text_input("어떻게 알았는지 (Source)", key="signup_form_source")
            submitted = st.form_submit_button("가입 신청 제출", use_container_width=True, type="primary")
        if submitted:
            ok, msg = register_pending_user(
                str(st.session_state.get("signup_form_id", "") or "").strip(),
                str(st.session_state.get("signup_form_pw", "") or "").strip(),
                str(st.session_state.get("signup_form_reason", "") or "").strip(),
                str(st.session_state.get("signup_form_source", "") or "").strip(),
            )
            if ok:
                st.success("가입 신청이 완료되었습니다. 관리자 승인을 기다려주세요.")
            else:
                st.error(msg)


if not st.session_state.get("logged_in"):
    _render_login_screen()
    st.stop()

if "narrative_history" not in st.session_state:
    st.session_state["narrative_history"] = []
if "market_narrative_data" not in st.session_state:
    st.session_state["market_narrative_data"] = {}
if "current_view" not in st.session_state:
    st.session_state["current_view"] = {}
if "current_view_language" not in st.session_state:
    st.session_state["current_view_language"] = "ko"
if "current_view_source" not in st.session_state:
    # 분석 유형 추적: "market_narrative" | "weekly_trend_7d" | "wow_trend_7d"
    st.session_state["current_view_source"] = "market_narrative"
if "scanner_results" not in st.session_state:
    st.session_state["scanner_results"] = None
if "scanner_results_emerging" not in st.session_state:
    st.session_state["scanner_results_emerging"] = None
if "_scanner_last_result_restored" not in st.session_state:
    st.session_state["_scanner_last_result_restored"] = False
if "narrative_timeseries_briefing" not in st.session_state:
    st.session_state["narrative_timeseries_briefing"] = None

# 사이드바 메인 내비게이션 (탑다운: 단일 radio, 옵션 문자열로 분기)
_MAIN_NAV_OPTIONS = (
    # ── 시작하기 ───────────────────────────────────────────────────────────
    "🚨 Daily Risk Gauge",
    # ── 분석 도구 ──────────────────────────────────────────────────────────
    "🌐 [1단계] 거시경제 지표",
    "📰 [1단계] 시장 내러티브",
    "🎯 [2단계] 섹터 & 자금 흐름",
    "🚀 [2단계] AI 종목 스캐너",
    "🔬 [3단계] 개별 종목 정밀 검사",
    # ── 포트폴리오 관리 ────────────────────────────────────────────────────
    "🛡️ [4단계] 포트폴리오 매도 레이더",
    "🔔 Buy Watchlist & Alert",
    # ── AI 인사이트 ────────────────────────────────────────────────────────
    "📊 [AI] 내러티브 적중률 트래커",
    "💡 [AI] Idea-to-Portfolio 추적",
    "📋 [AI] 주간 포트폴리오 요약",
    "📡 [AI] Emerging 종목 추적기",
    # ── 도움말 ─────────────────────────────────────────────────────────────
    "📖 사용 가이드",
)


_NARRATIVE_RECORD_SOURCE_WEEKLY_7D = "weekly_trend_7d"

_OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS = (
    "🔥 오늘의 최신 내러티브 (단기 모멘텀)",
    "🏆 주간 메가 트렌드 (스윙/중기 투자용)",
    "🎯 수동 섹터/티커 입력",
)

_APP_DIR = Path(__file__).resolve().parent
_ETF_UNIVERSE_FILE = _APP_DIR / "etf_universe.txt"
_WATCHLIST_FILE = _APP_DIR / "watchlist.json"
_PORTFOLIO_FILE = _APP_DIR / "portfolio.csv"
_SAN_ANTONIO_TZ = pytz.timezone("America/Chicago")
_MARKET_ET_TZ = pytz.timezone("America/New_York")
_NARRATIVE_HISTORY_MAX_RECORDS = 40
_NARRATIVE_HISTORY_RETENTION_DAYS = 14
_DATA_CACHE_TTL = 3600

fred = Fred(api_key="5983699636e17c1ed733456106d51940")

# Google GenAI: 로그인 이후 첫 호출 시 secrets에서 키를 읽어 클라이언트를 생성한다.
_genai_client = None


def _ensure_genai_client():
    global _genai_client
    if _genai_client is not None:
        return _genai_client
    try:
        api_key = st.secrets["GOOGLE_API_KEY"]
    except (KeyError, FileNotFoundError):
        st.error(
            "GOOGLE_API_KEY가 설정되어 있지 않습니다. "
            "`.streamlit/secrets.toml` 또는 배포 Secrets에 등록해 주세요."
        )
        st.stop()
    _genai_client = genai.Client(api_key=api_key)
    return _genai_client


class _GenAIModel:
    """기존 google.generativeai.GenerativeModel과 동일한 호출 인터페이스를
    유지하기 위한 얇은 호환 래퍼.

    내부적으로는 새 SDK(google.genai)의 `client.models.generate_content`를
    사용한다. 호출부에서는 `model.generate_content(prompt)` 형태를 그대로
    사용할 수 있어 마이그레이션 위험을 최소화한다.
    """

    __slots__ = ("_model_id", "_config")

    def __init__(self, model_id, generation_config=None):
        self._model_id = model_id
        cfg = dict(generation_config or {})
        # Gemini 2.5 계열은 'thinking' 토큰이 max_output_tokens 예산을 함께 소비한다.
        # generation_config 에 thinking_budget(정수)이 있으면 ThinkingConfig 로 변환한다.
        #   0   → 사고 비활성화(전체 토큰 예산을 보이는 출력에 사용)
        #   -1  → 동적(모델이 알아서)
        #   N>0 → 사고에 최대 N 토큰만 허용
        if "thinking_budget" in cfg:
            _tb = cfg.pop("thinking_budget")
            try:
                cfg["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=int(_tb))
            except Exception:
                # 구버전 SDK 등에서 ThinkingConfig 미지원이면 조용히 무시
                pass
        self._config = genai_types.GenerateContentConfig(**cfg) if cfg else None

    def generate_content(self, prompt, max_retries: int = 3):
        """
        Gemini API 호출. 503/429 등 일시적 오류 시 최대 max_retries회 자동 재시도.
        재시도 간격: 1초 → 3초 → 7초 (지수 백오프)
        """
        import time as _time
        delays = [1, 3, 7]
        last_exc = None
        for attempt in range(max_retries):
            try:
                return _ensure_genai_client().models.generate_content(
                    model=self._model_id,
                    contents=prompt,
                    config=self._config,
                )
            except Exception as exc:
                last_exc = exc
                err_str = str(exc).lower()
                # 재시도 가능한 오류: 503(과부하), 429(rate limit), 500(서버오류)
                retryable = any(code in err_str for code in ["503", "500", "unavailable", "internal", "server", "overloaded", "resource"])
                if retryable and attempt < max_retries - 1:
                    wait = delays[attempt]
                    _time.sleep(wait)
                    continue
                # 재시도 불가 오류 또는 마지막 시도 실패 → 즉시 raise
                raise
        raise last_exc


model = _GenAIModel(
    "gemini-2.5-flash",  # 1.5가 아닌 가장 최근에 입력했던 2.5로 통일!
    generation_config={
        "temperature": 0.3,  # 내러티브 생성 — 매번 다른 인사이트를 위해 약간의 다양성 허용
        "top_p": 1,
        "top_k": 1,
        "max_output_tokens": 8192,
        "thinking_budget": 0,
        "response_mime_type": "application/json",
    },
)

# 1.6 스캐너 내러티브 전용: 티커 청크별 Gemini 호출 · JSON MIME + max_output_tokens(8192) + 세탁 후 파싱
_SCANNER_NARRATIVE_BATCH_MODEL_ID = "gemini-2.5-flash"
# 티커를 한 번에 너무 많이 넘기면 응답이 잘려 JSONDecodeError(Unterminated string)가 날 수 있어 상한을 둔다.
_SCANNER_NARRATIVE_TICKER_CHUNK_SIZE = 20

# 내러티브 가중치 제외·0점 처리 시 동일 문구 (Current Leaders / Emerging 공통)
SCANNER_NARRATIVE_API_FAIL_MESSAGE = "API 응답 지연 (보정 점수 적용)"
# 아래 사유면 Narrative 원점수는 0이며 Final에서 내러티브 가중치(35%)를 분모에서 제외
SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS = frozenset(
    {
        SCANNER_NARRATIVE_API_FAIL_MESSAGE,
        "내러티브 텍스트가 부족합니다.",
        "배치 응답에 누락됨",
        "배치 미응답",
        "Gemini 배치 평가 실패",
    }
)
_scanner_narrative_batch_model = _GenAIModel(
    _SCANNER_NARRATIVE_BATCH_MODEL_ID,
    generation_config={
        "temperature": 0.0,
        "top_p": 1,
        "top_k": 1,
        "max_output_tokens": 8192,
        "thinking_budget": 0,
        "response_mime_type": "application/json",
    },
)

# Gemini 청크 호출: 503/429 등 일시 오류 시 sleep 후 재시도 (토큰 낭비·즉시 0점 방지)
_SCANNER_GEMINI_CHUNK_MAX_RETRIES = 3
_SCANNER_GEMINI_CHUNK_RETRY_SLEEP_SEC = 5


def _scanner_narrative_batch_generate_chunk_with_retries(prompt: str):
    """청크 단위 `generate_content`. 최대 3회 시도, 사이에 고정 대기."""
    last_exc = None
    for attempt in range(1, _SCANNER_GEMINI_CHUNK_MAX_RETRIES + 1):
        try:
            return _scanner_narrative_batch_model.generate_content(prompt)
        except Exception as exc:
            last_exc = exc
            if attempt >= _SCANNER_GEMINI_CHUNK_MAX_RETRIES:
                break
            st.toast(
                f"구글 서버 과부하로 {_SCANNER_GEMINI_CHUNK_RETRY_SLEEP_SEC}초 대기 후 해당 청크 재시도 중... ({attempt}/{_SCANNER_GEMINI_CHUNK_MAX_RETRIES})",
                icon="⌛",
            )
            time.sleep(_SCANNER_GEMINI_CHUNK_RETRY_SLEEP_SEC)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Gemini 청크 호출 실패(원인 불명)")


MACRO_STATUS_PASS = "pass"
MACRO_STATUS_WARN = "warning"
MACRO_STATUS_FAIL = "fail"
MACRO_STATUS_NA = "na"
_FRED_TRANSIENT_ERROR_NOTE = "FRED 서버 응답 지연 (일시적 오류)"


def load_etf_universe_tickers():
    """
    Read ETF tickers from etf_universe.txt (same folder as app.py).
    Lines starting with # are comments. Tickers may be separated by commas, spaces, or newlines.
    """
    path = _ETF_UNIVERSE_FILE
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8").lstrip("\ufeff")
    except OSError:
        return []

    ordered = []
    seen = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        before_comment = line.split("#", 1)[0]
        for token in re.split(r"[,\s]+", before_comment):
            t = str(token).strip().upper()
            if not t:
                continue
            if t not in seen:
                seen.add(t)
                ordered.append(t)
    return ordered


def read_etf_universe_file_tickers():
    """
    etf_universe.txt + Google Sheets ETF_Universe 합산 티커 목록 반환.
    Sheets에 신규 ETF가 자동 추가되면 여기에도 자동 반영된다.
    """
    return load_etf_universe_tickers_merged()


def to_float(value):
    """Safely convert numeric values (e.g. from FMP) to float."""
    if value is None:
        return np.nan
    if isinstance(value, (list, tuple, dict, pd.Series, pd.DataFrame)):
        return np.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def pct_str(value, decimals=2):
    if pd.isna(value):
        return "N/A"
    return f"{value * 100:.{decimals}f}%"


def num_str(value, decimals=2):
    if pd.isna(value):
        return "N/A"
    return f"{value:,.{decimals}f}"


def pct_points_str(value, decimals=2):
    if pd.isna(value):
        return "N/A"
    return f"{value:.{decimals}f}%"


def won_str(value):
    if pd.isna(value):
        return "N/A"
    return f"{value:,.0f}"


def usd_short_str(value):
    if pd.isna(value):
        return "N/A"
    val = float(value)
    abs_val = abs(val)
    if abs_val >= 1_000_000_000_000:
        return f"${val / 1_000_000_000_000:.2f}T"
    if abs_val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    if abs_val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    return f"${val:,.0f}"


def pass_fail_badge(passed, no_data=False):
    if no_data:
        return "⚫ N/A"
    return "✅ Pass" if passed else "❌ Fail"


def get_latest_series_value(df, row_name):
    """
    Financial statements are often indexed by account name and columns by dates.
    Return the latest non-null value for a row.
    """
    if df is None or df.empty or row_name not in df.index:
        return np.nan
    row = df.loc[row_name]
    if isinstance(row, pd.Series):
        row = pd.to_numeric(row, errors="coerce").dropna()
        if row.empty:
            return np.nan
        return row.iloc[0]
    return np.nan


def get_momentum_values(history_df):
    if history_df is None or history_df.empty or "Close" not in history_df.columns:
        return np.nan, np.nan, np.nan

    close = pd.to_numeric(history_df["Close"], errors="coerce").dropna()
    if close.empty:
        return np.nan, np.nan, np.nan

    ma50 = close.rolling(window=50, min_periods=50).mean().iloc[-1]
    ma200 = close.rolling(window=200, min_periods=200).mean().iloc[-1]
    current_price = close.iloc[-1]
    return current_price, ma50, ma200


def get_latest_close_from_history(history_df):
    if history_df is None or history_df.empty or "Close" not in history_df.columns:
        return np.nan
    close = pd.to_numeric(history_df["Close"], errors="coerce").dropna()
    if close.empty:
        return np.nan
    return close.iloc[-1]


def get_price_and_ma200(history_df):
    if history_df is None or history_df.empty or "Close" not in history_df.columns:
        return np.nan, np.nan

    close = pd.to_numeric(history_df["Close"], errors="coerce").dropna()
    if close.empty:
        return np.nan, np.nan

    ma200 = close.rolling(window=200, min_periods=200).mean().iloc[-1]
    current_price = close.iloc[-1]
    return current_price, ma200


def macro_status_label(status):
    if status == MACRO_STATUS_PASS:
        return "✅ Pass"
    if status == MACRO_STATUS_WARN:
        return "🟡 Warning"
    if status == MACRO_STATUS_FAIL:
        return "🔴 Fail"
    return "N/A"


def macro_status_bad_count(status):
    return status in (MACRO_STATUS_WARN, MACRO_STATUS_FAIL)


def macro_traffic_light(bad_total, total_indicators: int = 11):
    """
    11개 지표 기준 신호등.
    - 빨간불: 전체의 60% 이상(7/11) 경고·실패  → 거시 리스크 전방위 확산
    - 노란불: 전체의 36% 이상(4/11) 경고·실패  → 경고 누적, 비중 축소
    - 초록불: 그 미만                           → 환경 안정적
    """
    red_threshold    = max(7, round(total_indicators * 0.60))   # ≥60% → 빨간불
    yellow_threshold = max(4, round(total_indicators * 0.36))   # ≥36% → 노란불

    if bad_total >= red_threshold:
        st.error(
            f"🔴 **[빨간불] 폭풍우 경보** — {total_indicators}개 지표 중 {bad_total}개 경고·실패 "
            f"({bad_total/total_indicators*100:.0f}%). "
            "복수의 거시 지표가 동시에 악화된 상태입니다. "
            "**현금 비중을 최우선으로 확대하고, 신규 매수를 전면 중단하세요.** "
            "개별 종목 분석(3단계)을 진행하기 전에 매크로 리스크가 해소되는지 먼저 확인하세요."
        )
    elif bad_total >= yellow_threshold:
        st.warning(
            f"🟡 **[노란불] 거시경제 경고 누적** — {total_indicators}개 지표 중 {bad_total}개 경고·실패 "
            f"({bad_total/total_indicators*100:.0f}%). "
            "개별 종목이 좋아 보여도 매크로 역풍이 있을 수 있습니다. "
            "포지션 비중을 줄이고 현금을 확보하며 분할 접근을 권장합니다."
        )
    else:
        st.success(
            f"🟢 **[초록불] 매크로 환경 안정적** — {total_indicators}개 지표 중 경고 {bad_total}개. "
            f"「{_MAIN_NAV_OPTIONS[4]}」에서 펀더멘털과 매수 타점을 확인한 뒤 투자를 진행하세요."
        )


def _yield_spread_note(spread: float, t10: float, t3m: float, source: str) -> tuple:
    """금리차 수치 → (status, note) 공통 판정 로직."""
    detail = f"10Y {t10:.2f}% / 3M {t3m:.2f}% [{source}]"
    if spread < 0:
        status = MACRO_STATUS_WARN
        note = f"수익률 곡선 역전(단기금리 > 장기): 침체·신용경색 우려 신호입니다. ({detail})"
    elif spread < 0.3:
        status = MACRO_STATUS_WARN
        note = f"금리차 플러스이나 매우 좁은 구간 — 역전 전단계 모니터링 필요. ({detail})"
    else:
        status = MACRO_STATUS_PASS
        note = f"장단기 금리차 플러스 구간입니다. 역전 신호 없음. ({detail})"
    return spread, status, note


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_yield_spread_latest():
    """10Y-3M 국채 금리차.
    1순위: FMP Treasury Rates API (당일 실시간)
    2순위: FRED DGS10 / DTB3 (전일 기준, 폴백)
    """
    k = _fmp_key()
    # ── FMP 실시간 시도 ─────────────────────────────────────────────────
    if k:
        try:
            r = requests.get(
                f"{_FMP_BASE}/treasury-rates?apikey={k}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    latest = data[0]
                    t10 = to_float(latest.get("year10") or latest.get("tenYear"))
                    t3m = to_float(latest.get("month3") or latest.get("threeMonth"))
                    if pd.notna(t10) and pd.notna(t3m):
                        spread = t10 - t3m
                        return _yield_spread_note(spread, t10, t3m, "FMP 실시간")
        except Exception:
            pass
    # ── FRED 폴백 ────────────────────────────────────────────────────────
    try:
        t10_s = fred.get_series("DGS10")
        t3m_s = fred.get_series("DTB3")
        t10 = float(pd.to_numeric(t10_s, errors="coerce").dropna().iloc[-1])
        t3m = float(pd.to_numeric(t3m_s, errors="coerce").dropna().iloc[-1])
        if pd.isna(t10) or pd.isna(t3m):
            return np.nan, MACRO_STATUS_NA, "데이터 부족 (N/A)"
        spread = t10 - t3m
        return _yield_spread_note(spread, t10, t3m, "FRED 전일")
    except Exception:
        return np.nan, MACRO_STATUS_NA, "연산 또는 다운로드 오류"


def evaluate_vix_status(vix_value):
    if pd.isna(vix_value):
        return MACRO_STATUS_NA, "데이터 부족 (N/A)."
    if vix_value >= 35:
        return MACRO_STATUS_FAIL, "극단적 불안/변동성. 리스크 자산 회피 신호입니다."
    if vix_value >= 25:
        return MACRO_STATUS_WARN, "공포 또는 변동성 확대 구간입니다. 레버리지·추격매수 금지."
    return MACRO_STATUS_PASS, "상대적으로 낮은 불안 구간입니다. 단, 역사적 평균 대비 고점 돌파 시 추세를 확인하세요."


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_vix_latest_and_history():
    """VIX 현재값 + 1년 히스토리.
    1순위: FRED VIXCLS  2순위: FMP ^VIX (full 엔드포인트)
    """
    # ── FRED ─────────────────────────────────────────────────────────
    try:
        s = fred.get_series("VIXCLS")
        s = pd.to_numeric(s, errors="coerce").dropna()
        if len(s) >= 10:
            s = s.tail(252)
            hist = pd.DataFrame({"Close": s})
            hist.index = pd.to_datetime(hist.index)
            return float(s.iloc[-1]), hist, None
    except Exception:
        pass
    # ── FMP 폴백 ─────────────────────────────────────────────────────
    try:
        k = _fmp_key()
        if k:
            r = requests.get(
                f"{_FMP_BASE}/historical-price-eod/full?symbol=%5EVIX&limit=252&apikey={k}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                rows = data.get("historical", data) if isinstance(data, dict) else data
                if isinstance(rows, list) and rows:
                    df = pd.DataFrame(rows)
                    if "date" in df.columns:
                        cc = "close" if "close" in df.columns else "Close"
                        df = df[["date", cc]].rename(columns={cc: "Close"})
                        df["date"] = pd.to_datetime(df["date"])
                        df = df.set_index("date").sort_index()
                        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
                        df = df.dropna()
                        if not df.empty:
                            return float(df["Close"].iloc[-1]), df, "FMP폴백"
    except Exception:
        pass
    return np.nan, None, "VIX 데이터 조회 실패 (FRED·FMP 모두 실패)"


def evaluate_wti_status(wti):
    if pd.isna(wti):
        return MACRO_STATUS_NA, "데이터 부족 (N/A)."
    if wti >= 110:
        return MACRO_STATUS_FAIL, "유가 급등권. 인플레·비용 압력이 성장주 밸류에이션을 압박할 수 있습니다."
    if wti >= 90:
        return MACRO_STATUS_WARN, "유가 상당히 높은 구간입니다. 에너지·비용 우려 모니터링."
    if wti <= 50:
        return MACRO_STATUS_WARN, "유가가 낮은 구간입니다. 글로벌 수요 둔화 가능성을 열어두세요."
    return MACRO_STATUS_PASS, "유가가 과도하게 붐비거나 붕괴한 구간은 아닙니다."


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_wti_latest():
    """WTI 유가 최신값.
    1순위: FRED DCOILWTICO  2순위: FMP USO ETF 근사
    """
    try:
        s = fred.get_series("DCOILWTICO")
        s = pd.to_numeric(s, errors="coerce").dropna()
        if not s.empty:
            return float(s.iloc[-1])
    except Exception:
        pass
    # FMP 폴백: USO (WTI ETF) — full 엔드포인트
    try:
        k = _fmp_key()
        if k:
            for sym in ("USO", "OIL"):
                try:
                    r = requests.get(
                        f"{_FMP_BASE}/historical-price-eod/full?symbol={sym}&limit=5&apikey={k}",
                        timeout=_FMP_TIMEOUT,
                    )
                    if r.status_code == 200:
                        d = r.json()
                        rows = d.get("historical", d) if isinstance(d, dict) else d
                        if isinstance(rows, list) and rows:
                            v = rows[0].get("close") or rows[0].get("Close")
                            if v is not None:
                                return float(v)
                except Exception:
                    continue
    except Exception:
        pass
    return np.nan


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_unrate_series() -> pd.Series:
    """UNRATE — FRED 1순위, FMP unemploymentRate 2순위."""
    # FRED
    try:
        s = fred.get_series("UNRATE")
        s = pd.to_numeric(s, errors="coerce").dropna()
        if len(s) >= 15:
            return s
    except Exception:
        pass
    # FMP economic-indicators
    try:
        k = _fmp_key()
        if k:
            r = requests.get(
                f"{_FMP_BASE}/economic-indicators?name=unemploymentRate&apikey={k}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    df = pd.DataFrame(data)
                    if "date" in df.columns and "value" in df.columns:
                        df = df[["date", "value"]].copy()
                        df["date"] = pd.to_datetime(df["date"])
                        df = df.set_index("date").sort_index()
                        s = pd.to_numeric(df["value"], errors="coerce").dropna()
                        if len(s) >= 5:
                            return s
    except Exception:
        pass
    return pd.Series(dtype=float)


def evaluate_unemployment_sahm_series():
    try:
        unrate_series = _fetch_unrate_series()
        if unrate_series.empty:
            return MACRO_STATUS_NA, np.nan, np.nan, np.nan, _FRED_TRANSIENT_ERROR_NOTE
        df_unrate = pd.DataFrame({"UNRATE": unrate_series})
        df_unrate["UNRATE"] = pd.to_numeric(df_unrate["UNRATE"], errors="coerce")
        df_unrate = df_unrate.dropna()
        if df_unrate.empty:
            return MACRO_STATUS_NA, np.nan, np.nan, np.nan, "UNRATE 데이터가 비어 있습니다."

        un = df_unrate["UNRATE"].sort_index()

        if len(un) < 15:
            return MACRO_STATUS_NA, np.nan, np.nan, np.nan, "실업률 표본이 부족합니다."

        last3 = float(un.iloc[-3:].mean())
        yr_min = float(un.iloc[-12:].min())
        margin = last3 - yr_min

        if margin >= 0.5:
            status = MACRO_STATUS_WARN
            msg = (
                f"샴 근접 신호: 최근 3개월 평균 {last3:.2f}% vs 12개월 최저 {yr_min:.2f}% (+{margin:.2f} pp)."
            )
        else:
            status = MACRO_STATUS_PASS
            msg = f"12개월 대비 평균 실업 악화 폭은 {margin:.2f} pp로 0.5 pp 미만입니다."

        return status, last3, yr_min, margin, msg
    except Exception:
        return MACRO_STATUS_NA, np.nan, np.nan, np.nan, _FRED_TRANSIENT_ERROR_NOTE


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_cpi_series() -> pd.Series:
    """CPIAUCSL — FRED 1순위, FMP CPI 2순위."""
    # FRED
    try:
        s = fred.get_series("CPIAUCSL")
        s = pd.to_numeric(s, errors="coerce").dropna()
        if len(s) >= 14:
            return s
    except Exception:
        pass
    # FMP economic-indicators (name=CPI)
    try:
        k = _fmp_key()
        if k:
            r = requests.get(
                f"{_FMP_BASE}/economic-indicators?name=CPI&apikey={k}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    df = pd.DataFrame(data)
                    if "date" in df.columns and "value" in df.columns:
                        df = df[["date", "value"]].copy()
                        df["date"] = pd.to_datetime(df["date"])
                        df = df.set_index("date").sort_index()
                        s = pd.to_numeric(df["value"], errors="coerce").dropna()
                        if len(s) >= 5:
                            return s
    except Exception:
        pass
    return pd.Series(dtype=float)


def evaluate_cpi_yoy():
    try:
        cpi_series = _fetch_cpi_series()
        if cpi_series.empty:
            return MACRO_STATUS_NA, np.nan, _FRED_TRANSIENT_ERROR_NOTE
        df_cpi = pd.DataFrame({"CPIAUCSL": cpi_series})
        df_cpi["CPIAUCSL"] = pd.to_numeric(df_cpi["CPIAUCSL"], errors="coerce")
        df_cpi = df_cpi.dropna()
        if df_cpi.empty:
            return MACRO_STATUS_NA, np.nan, "CPI 데이터가 비어 있습니다."

        cpi = df_cpi["CPIAUCSL"].sort_index()

        if len(cpi) < 14:
            return MACRO_STATUS_NA, np.nan, "CPI 표본이 부족합니다."

        yoy = float((cpi.iloc[-1] / cpi.iloc[-13] - 1.0) * 100)

        if yoy > 4.8:
            status = MACRO_STATUS_FAIL
            note = "물가 여전히 높은 구간입니다. 장기 높은 금리 레짐 우려가 성장주에 불리합니다."
        elif yoy > 3.2:
            status = MACRO_STATUS_WARN
            note = "연준의 금리·유동성 기조를 주시해야 하는 물가 수준입니다."
        else:
            status = MACRO_STATUS_PASS
            note = "완만한 디스인플레이션에 가까운 구간입니다(휴리스틱)."

        return status, yoy, note
    except Exception:
        return MACRO_STATUS_NA, np.nan, _FRED_TRANSIENT_ERROR_NOTE


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_dxy_latest_and_mean_deviation():
    """DXY 달러 인덱스.
    1순위: FRED DTWEXBGS  2순위: FMP UUP ETF (DXY 추적)
    """
    def _calc(closes_s: pd.Series):
        if closes_s.empty:
            return np.nan, np.nan, MACRO_STATUS_NA
        cur = float(closes_s.iloc[-1])
        ma252 = float(closes_s.rolling(window=252, min_periods=63).mean().iloc[-1])
        if pd.isna(cur) or pd.isna(ma252) or ma252 == 0:
            return cur, np.nan, MACRO_STATUS_NA
        dev_pct = (cur / ma252 - 1.0) * 100
        if dev_pct >= 5.5:
            return cur, dev_pct, MACRO_STATUS_FAIL
        if dev_pct >= 2.5:
            return cur, dev_pct, MACRO_STATUS_WARN
        return cur, dev_pct, MACRO_STATUS_PASS
    # FRED
    try:
        s = fred.get_series("DTWEXBGS")
        s = pd.to_numeric(s, errors="coerce").dropna()
        if not s.empty:
            return _calc(s.tail(400))
    except Exception:
        pass
    # FMP UUP (DXY ETF) — full 엔드포인트
    try:
        k = _fmp_key()
        if k:
            r = requests.get(
                f"{_FMP_BASE}/historical-price-eod/full?symbol=UUP&limit=400&apikey={k}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code == 200:
                d = r.json()
                rows = d.get("historical", d) if isinstance(d, dict) else d
                if isinstance(rows, list) and rows:
                    df = pd.DataFrame(rows)
                    if "date" in df.columns:
                        cc = "close" if "close" in df.columns else "Close"
                        df["date"] = pd.to_datetime(df["date"])
                        df = df.set_index("date").sort_index()
                        s = pd.to_numeric(df[cc], errors="coerce").dropna()
                        if not s.empty:
                            return _calc(s)
    except Exception:
        pass
    return np.nan, np.nan, MACRO_STATUS_NA


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_earnings_calendar(tickers: tuple) -> list[dict]:
    """보유 종목의 다음 실적 발표일.

    3-layer fallback 전략 (전부 FMP):
      1) FMP earnings-calendar  (from=오늘, to=오늘+90일, symbol=X)
      2) FMP earnings           (/stable/earnings?symbol=X)
      3) FMP quote              (earningsAnnouncement 필드)
    """
    k = _fmp_key()
    today = datetime.now(_MARKET_ET_TZ).date()  # 시장 기준일 = 미 동부시간
    tomorrow_dt = today + timedelta(days=1)
    from_str = today.strftime("%Y-%m-%d")
    to_str   = (today + timedelta(days=90)).strftime("%Y-%m-%d")
    results  = []

    def _parse_future(items: list, date_key: str = "date") -> tuple:
        """items 리스트에서 내일 이후 가장 이른 (days_until, date_str, item) 반환."""
        future = []
        for it in (items if isinstance(items, list) else []):
            ds = str(it.get(date_key) or "")[:10]
            if not ds or len(ds) < 10:
                continue
            try:
                dt = datetime.strptime(ds, "%Y-%m-%d").date()
                if dt >= tomorrow_dt:
                    future.append(((dt - today).days, ds, it))
            except Exception:
                continue
        return sorted(future)[0] if future else None

    def _parse_ann_field(ann_str: str) -> tuple | None:
        """단순 날짜 문자열(earningsAnnouncement)을 (days, date_str, {}) 형태로 변환."""
        ds = str(ann_str or "")[:10]
        if not ds or len(ds) < 10:
            return None
        try:
            dt = datetime.strptime(ds, "%Y-%m-%d").date()
            if dt >= tomorrow_dt:
                return (dt - today).days, ds, {}
        except Exception:
            pass
        return None

    for tk in tickers:
        best: tuple | None = None
        eps_estimate = None
        try:
            # ── Layer 1: earnings-calendar with date range ────────────────
            if k and best is None:
                try:
                    r = requests.get(
                        f"{_FMP_BASE}/earnings-calendar"
                        f"?symbol={tk}&from={from_str}&to={to_str}&apikey={k}",
                        timeout=_FMP_TIMEOUT,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        found = _parse_future(data if isinstance(data, list) else [])
                        if found:
                            best = found
                            eps_estimate = to_float(found[2].get("epsEstimated"))
                except Exception:
                    pass

            # ── Layer 2: /stable/earnings (includes upcoming) ─────────────
            if k and best is None:
                try:
                    r2 = requests.get(
                        f"{_FMP_BASE}/earnings?symbol={tk}&apikey={k}",
                        timeout=_FMP_TIMEOUT,
                    )
                    if r2.status_code == 200:
                        data2 = r2.json()
                        found2 = _parse_future(data2 if isinstance(data2, list) else [])
                        if found2:
                            best = found2
                            eps_estimate = to_float(found2[2].get("epsEstimated"))
                except Exception:
                    pass

            # ── Layer 3: quote earningsAnnouncement field ─────────────────
            if k and best is None:
                try:
                    rq = requests.get(
                        f"{_FMP_BASE}/quote?symbol={tk}&apikey={k}",
                        timeout=_FMP_TIMEOUT,
                    )
                    if rq.status_code == 200:
                        qd = rq.json()
                        q_item = qd[0] if isinstance(qd, list) and qd else {}
                        ann = str(q_item.get("earningsAnnouncement") or "")[:10]
                        parsed = _parse_ann_field(ann)
                        if parsed:
                            best = parsed
                except Exception:
                    pass

            if best:
                days_until, date_str, _ = best
                results.append({
                    "ticker":        tk,
                    "earnings_date": date_str,
                    "days_until":    days_until,
                    "eps_estimate":  eps_estimate,
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["days_until"])
    return results


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fed_funds_rate() -> tuple[float, str]:
    """현재 기준금리.
    1순위: FRED FEDFUNDS  2순위: FMP federalFundsRate
    """
    def _fed_note(rate: float) -> str:
        if rate >= 5.0:
            return "고금리 레짐. 성장주 밸류에이션 압박 지속."
        if rate >= 3.0:
            return "중립 이상 금리. 연준 기조 모니터링 필요."
        return "완화적 금리 환경. 유동성 우호적."
    # FRED
    try:
        series = fred.get_series("FEDFUNDS")
        if series is not None and not series.empty:
            rate = float(pd.to_numeric(series, errors="coerce").dropna().iloc[-1])
            if pd.notna(rate):
                return rate, _fed_note(rate)
    except Exception:
        pass
    # FMP federalFundsRate
    try:
        k = _fmp_key()
        if k:
            r = requests.get(
                f"{_FMP_BASE}/economic-indicators?name=federalFundsRate&apikey={k}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    rate = to_float(data[0].get("value"))
                    if pd.notna(rate):
                        return rate, _fed_note(rate) + " [FMP]"
    except Exception:
        pass
    return np.nan, _FRED_TRANSIENT_ERROR_NOTE


def _hist_fetch_fedfunds() -> pd.Series:
    """FEDFUNDS 히스토리 — FRED 1순위, FMP 폴백."""
    try:
        s = fred.get_series("FEDFUNDS")
        if s is not None and not s.empty:
            return pd.to_numeric(s, errors="coerce").dropna().tail(24)
    except Exception:
        pass
    try:
        k = _fmp_key()
        if k:
            r = requests.get(
                f"{_FMP_BASE}/economic-indicators?name=federalFundsRate&apikey={k}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    df = pd.DataFrame(data)[["date", "value"]].copy()
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.set_index("date").sort_index()
                    return pd.to_numeric(df["value"], errors="coerce").dropna().tail(24)
    except Exception:
        pass
    return pd.Series(dtype=float)


def _hist_fetch_dxy() -> pd.Series:
    """DXY 히스토리 — FRED 1순위, FMP UUP 폴백."""
    try:
        s = fred.get_series("DTWEXBGS")
        if s is not None and not s.empty:
            return pd.to_numeric(s, errors="coerce").dropna().tail(500)
    except Exception:
        pass
    try:
        k = _fmp_key()
        if k:
            r = requests.get(
                f"{_FMP_BASE}/historical-price-eod/full?symbol=UUP&limit=500&apikey={k}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code == 200:
                d = r.json()
                rows = d.get("historical", d) if isinstance(d, dict) else d
                if isinstance(rows, list) and rows:
                    df = pd.DataFrame(rows)
                    if "date" in df.columns:
                        cc = "close" if "close" in df.columns else "Close"
                        df["date"] = pd.to_datetime(df["date"])
                        df = df.set_index("date").sort_index()
                        return pd.to_numeric(df[cc], errors="coerce").dropna().tail(500)
    except Exception:
        pass
    return pd.Series(dtype=float)


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_macro_history_series() -> dict:
    """CPI·실업률·DXY·Fed Rate 1~2년 히스토리 시리즈.
    4개 소스를 ThreadPoolExecutor로 병렬 조회.
    """
    import concurrent.futures as _cf

    def _safe_cpi():
        try:
            s = _fetch_cpi_series()
            if not s.empty:
                df = pd.DataFrame({"CPI": s}).dropna()
                df["v"] = df["CPI"].pct_change(12) * 100
                return ("cpi", df["v"].dropna().tail(24))
        except Exception:
            pass
        return ("cpi", pd.Series(dtype=float))

    def _safe_un():
        try:
            s = _fetch_unrate_series()
            if not s.empty:
                return ("unrate", s.tail(24))
        except Exception:
            pass
        return ("unrate", pd.Series(dtype=float))

    def _safe_fed():
        return ("fedfunds", _hist_fetch_fedfunds())

    def _safe_dxy():
        return ("dxy", _hist_fetch_dxy())

    out = {}
    with _cf.ThreadPoolExecutor(max_workers=4) as exe:
        futs = [exe.submit(fn) for fn in (_safe_cpi, _safe_un, _safe_fed, _safe_dxy)]
        for fut in _cf.as_completed(futs, timeout=20):
            try:
                key, val = fut.result()
                if not val.empty:
                    out[key] = val
            except Exception:
                pass
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fmp_economic_indicator(name: str) -> tuple[float, float, str]:
    """FMP Economic Indicators API — (latest_val, prev_val, date_str).
    name 예: 'GDP', 'retailSales', 'unemploymentRate', 'consumerConfidence'
    """
    k = _fmp_key()
    if not k:
        return np.nan, np.nan, ""
    try:
        r = requests.get(
            f"{_FMP_BASE}/economic-indicators?name={name}&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code != 200:
            return np.nan, np.nan, ""
        data = r.json()
        if not isinstance(data, list) or not data:
            return np.nan, np.nan, ""
        latest_val = to_float(data[0].get("value"))
        prev_val = to_float(data[1].get("value")) if len(data) > 1 else np.nan
        date_str = str(data[0].get("date", ""))[:10]
        return latest_val, prev_val, date_str
    except Exception:
        return np.nan, np.nan, ""


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fmp_market_risk_premium() -> tuple[float, str]:
    """FMP Market Risk Premium API — (US ERP %, note)."""
    k = _fmp_key()
    if not k:
        return np.nan, "FMP API 키 없음"
    try:
        r = requests.get(
            f"{_FMP_BASE}/market-risk-premium?apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code != 200:
            return np.nan, "데이터 없음"
        data = r.json()
        if not isinstance(data, list) or not data:
            return np.nan, "데이터 없음"
        # US 항목 우선, 없으면 첫 번째
        us_item = next(
            (d for d in data if str(d.get("country", "")).upper() in ("US", "UNITED STATES", "USA")),
            data[0],
        )
        erp = to_float(us_item.get("totalEquityRiskPremium") or us_item.get("equityRiskPremium"))
        country = str(us_item.get("country", "US"))
        if pd.isna(erp):
            return np.nan, "데이터 없음"
        if erp >= 6.0:
            note = f"ERP {erp:.2f}% — 주식이 채권 대비 매우 매력적. 밸류에이션 저평가 구간."
        elif erp >= 4.0:
            note = f"ERP {erp:.2f}% — 주식 위험 프리미엄 적정 구간."
        else:
            note = f"ERP {erp:.2f}% — 주식 프리미엄 낮음. 채권 대비 주식 매력 저하."
        return erp, note
    except Exception:
        return np.nan, "조회 오류"


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fmp_economic_calendar_risk() -> tuple[list, bool]:
    """FMP Economic Calendar — 지난 2일~향후 3일 US 이벤트 감지.
    조회 창은 발표 직후(actual 확인)와 사전 경고를 함께 보기 위해 today-2 ~ today+3로 둔다.
    각 이벤트에 is_past(과거 발표 여부)를 태깅한다.
    반환: (events_list, has_high_impact)
      - events_list: [{"date","event","impact","actual","estimate","prior","is_past"}]
      - has_high_impact: **향후(오늘 포함) 고임팩트 이벤트만** 카운트(지난 발표는 더 이상 리스크 아님)
    """
    k = _fmp_key()
    if not k:
        return [], False
    try:
        today = datetime.now(_MARKET_ET_TZ).date()  # 시장 기준일 = 미 동부시간
        from_date = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        to_date = (today + timedelta(days=3)).strftime("%Y-%m-%d")
        r = requests.get(
            f"{_FMP_BASE}/economic-calendar?from={from_date}&to={to_date}&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code != 200:
            return [], False
        data = r.json()
        if not isinstance(data, list):
            return [], False
        events = []
        for ev in data:
            country = str(ev.get("country", "")).upper()
            if country not in ("US", "USD", "UNITED STATES", ""):
                continue
            impact = str(ev.get("impact", "") or ev.get("importance", "") or "").capitalize()
            event_name = str(ev.get("event", "") or ev.get("name", ""))
            if not event_name:
                continue
            date_str = str(ev.get("date", ""))[:10]
            actual_val = ev.get("actual")
            has_actual = actual_val is not None and str(actual_val).strip() not in ("", "None")
            # 발표완료 판정: 날짜가 오늘 이전이거나, 이미 actual 값이 채워졌으면 발표완료.
            # (actual이 있으면 ET 날짜경계와 무관하게 '발표 끝남'이 확실하다)
            is_past = False
            try:
                if date_str and datetime.strptime(date_str, "%Y-%m-%d").date() < today:
                    is_past = True
            except Exception:
                is_past = False
            if has_actual:
                is_past = True
            events.append({
                "date":     date_str,
                "event":    event_name,
                "impact":   impact,
                "actual":   actual_val,
                "estimate": ev.get("estimate"),
                "prior":    ev.get("previous") or ev.get("prior"),
                "is_past":  is_past,
            })
        # 정렬: 날짜 오름차순(과거 → 오늘 → 미래)
        events.sort(key=lambda e: e.get("date", ""))
        # 리스크 플래그는 '아직 발표되지 않은(오늘 포함)' 고임팩트만 카운트
        has_high = any(
            (not e["is_past"]) and e["impact"].lower() in ("high", "3")
            for e in events
        )
        return events, has_high
    except Exception:
        return [], False


def compute_macro_score(rows: list) -> tuple[float, str, str]:
    """
    6대 지표 + Fear&Greed + Fed Rate를 종합해 0~100 Macro Score 계산.
    반환: (score, grade, description)
    pass=100, warning=50, fail=0, na=50(중립)으로 환산 후 평균.
    """
    score_map = {MACRO_STATUS_PASS: 100, MACRO_STATUS_WARN: 40, MACRO_STATUS_FAIL: 0, MACRO_STATUS_NA: 50}
    scores = [score_map.get(r["_status"], 50) for r in rows]
    if not scores:
        return 50.0, "N/A", "데이터 없음"
    avg = float(np.mean(scores))
    if avg >= 75:
        grade, desc = "🟢 BULLISH", "매크로 환경 우호적. 적극적 포지션 가능."
    elif avg >= 50:
        grade, desc = "🟡 NEUTRAL", "혼재된 신호. 선별적 접근 권장."
    elif avg >= 25:
        grade, desc = "🟠 CAUTIOUS", "위험 신호 누적. 비중 축소 고려."
    else:
        grade, desc = "🔴 BEARISH", "전방위 경고. 현금 비중 확대 권장."
    return avg, grade, desc


def analyze_us_macro_dashboard():
    import concurrent.futures as _cf

    # ── 모든 외부 API를 병렬로 동시 호출 (순차 3분 → 병렬 ~20초) ─────────
    def _safe(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    _tasks = {
        "spread":   (fetch_yield_spread_latest,          []),
        "vix":      (fetch_vix_latest_and_history,       []),
        "wti":      (fetch_wti_latest,                   []),
        "un":       (evaluate_unemployment_sahm_series,  []),
        "cpi":      (evaluate_cpi_yoy,                   []),
        "dxy":      (fetch_dxy_latest_and_mean_deviation,[]),
        "fed":      (fetch_fed_funds_rate,               []),
        "fg":       (fetch_macro_history_series,         []),
        "gdp":      (fetch_fmp_economic_indicator,       ["GDP"]),
        "rs":       (fetch_fmp_economic_indicator,       ["retailSales"]),
        "erp":      (fetch_fmp_market_risk_premium,      []),
    }

    _results = {}
    with _cf.ThreadPoolExecutor(max_workers=11) as _exe:
        _futures = {
            name: _exe.submit(_safe, fn, *args)
            for name, (fn, args) in _tasks.items()
        }
        for name, fut in _futures.items():
            try:
                _results[name] = fut.result(timeout=25)
            except Exception:
                _results[name] = None

    # ── 결과 언팩 ─────────────────────────────────────────────────────
    _s = _results.get("spread") or (np.nan, MACRO_STATUS_NA, "조회 실패")
    spread_val, spread_st, spread_note = _s

    _v = _results.get("vix") or (np.nan, None, "조회 실패")
    vix_val, vix_hist, vix_err = _v
    vix_st, vix_note = evaluate_vix_status(vix_val)

    wti = _results.get("wti") or np.nan
    if wti is None:
        wti = np.nan
    wti_st, wti_note = evaluate_wti_status(wti)

    _u = _results.get("un") or (MACRO_STATUS_NA, np.nan, np.nan, np.nan, _FRED_TRANSIENT_ERROR_NOTE)
    un_st, un_avg3, un_yr_low, un_margin, un_note = _u

    _c = _results.get("cpi") or (MACRO_STATUS_NA, np.nan, _FRED_TRANSIENT_ERROR_NOTE)
    cpi_st, cpi_yoy, cpi_note = _c

    _d = _results.get("dxy") or (np.nan, np.nan, MACRO_STATUS_NA)
    dxy, dxy_dev, dxy_st_base = _d

    rows = []

    rows.append(
        {
            "지표": "장단기 금리차 (10Y-3M)",
            "현재값": f"{spread_val:.2f} pp (10Y TNX − 3M IRX)" if pd.notna(spread_val) else "N/A",
            "판정": macro_status_label(spread_st),
            "판독 요약": spread_note,
            "_status": spread_st,
        }
    )

    rows.append(
        {
            "지표": "VIX (공포 지수)",
            "현재값": num_str(vix_val),
            "판정": macro_status_label(vix_st),
            "판독 요약": vix_note + ("" if (not vix_err or vix_err in ("FMP폴백", "None", None)) else f" [{vix_err}]"),
            "_status": vix_st,
        }
    )

    rows.append(
        {
            "지표": "WTI 원유 ($/BBL)",
            "현재값": num_str(wti),
            "판정": macro_status_label(wti_st),
            "판독 요약": wti_note,
            "_status": wti_st,
        }
    )

    if pd.notna(un_avg3) and pd.notna(un_yr_low) and pd.notna(un_margin):
        un_display = (
            f"3M평균 {un_avg3:.2f}% · 12M최저 {un_yr_low:.2f}% · Δ +{un_margin:.2f} pp"
        )
    elif pd.notna(un_avg3) or pd.notna(un_yr_low):
        un_display = f"Avg/min: {num_str(un_avg3)} / {num_str(un_yr_low)}"
    else:
        un_display = "N/A"
    rows.append(
        {
            "지표": "실업률 UNRATE (샴 근접)",
            "현재값": "N/A" if un_note == _FRED_TRANSIENT_ERROR_NOTE else un_display,
            "판정": macro_status_label(un_st),
            "판독 요약": un_note,
            "_status": un_st,
        }
    )

    rows.append(
        {
            "지표": "CPI 전년 동월 대비 (YoY)",
            "현재값": "N/A" if cpi_note == _FRED_TRANSIENT_ERROR_NOTE else (pct_points_str(cpi_yoy) if pd.notna(cpi_yoy) else "N/A"),
            "판정": macro_status_label(cpi_st),
            "판독 요약": cpi_note if isinstance(cpi_note, str) else str(cpi_note),
            "_status": cpi_st,
        }
    )

    dxy_disp = (
        f"{num_str(dxy)} (vs MA252 편차 {pct_points_str(dxy_dev)})"
        if pd.notna(dxy) and pd.notna(dxy_dev)
        else num_str(dxy)
    )
    rows.append(
        {
            "지표": "달러 지수 DXY",
            "현재값": dxy_disp,
            "판정": macro_status_label(dxy_st_base),
            "판독 요약": (
                "252거래일 대비 과도한 달러 강세 가능성입니다. 다국적 빅테크 환차익 우려."
                if dxy_st_base == MACRO_STATUS_FAIL
                else (
                    "달러 중간적으로 강한 편입니다. 실적 번역 노이즈 확인."
                    if dxy_st_base == MACRO_STATUS_WARN
                    else (
                        "달러가 극단적 강세권이라고 보기 어렵습니다."
                        if dxy_st_base == MACRO_STATUS_PASS
                        else "데이터 부족 (N/A)."
                    )
                )
            ),
            "_status": dxy_st_base,
        }
    )

    # Market Sentiment Score — VIX 데이터 직접 인라인 계산 (별도 API 호출 없음)
    fg_score = np.nan
    fg_rating = "N/A"
    fg_status = MACRO_STATUS_NA
    try:
        if vix_hist is not None and not vix_hist.empty and "Close" in vix_hist.columns:
            _vix_s = pd.to_numeric(vix_hist["Close"], errors="coerce").dropna()
            if len(_vix_s) >= 20 and pd.notna(vix_val):
                # VIX 백분위 역산 → 낮을수록 탐욕
                _vix_pct = float((_vix_s < float(vix_val)).sum() / len(_vix_s) * 100)
                fg_score = float(np.clip(100.0 - _vix_pct, 0, 100))
                if fg_score >= 75:
                    fg_rating, fg_status = "Extreme Greed", MACRO_STATUS_FAIL
                elif fg_score >= 55:
                    fg_rating, fg_status = "Greed", MACRO_STATUS_WARN
                elif fg_score >= 45:
                    fg_rating, fg_status = "Neutral", MACRO_STATUS_PASS
                elif fg_score >= 25:
                    fg_rating, fg_status = "Fear", MACRO_STATUS_PASS
                else:
                    fg_rating, fg_status = "Extreme Fear", MACRO_STATUS_PASS
    except Exception:
        pass
    rows.append({
        "지표": "Market Sentiment (VIX 기반)",
        "현재값": f"{fg_score:.0f} / 100 ({fg_rating})" if pd.notna(fg_score) else "N/A",
        "판정": macro_status_label(fg_status),
        "판독 요약": (
            "극단적 탐욕 구간. 과매수 경고, 조정 가능성 높음." if pd.notna(fg_score) and fg_score >= 75
            else "탐욕 구간. 추격 매수 주의." if pd.notna(fg_score) and fg_score >= 55
            else "극단적 공포 구간. 역발상 매수 기회일 수 있습니다." if pd.notna(fg_score) and fg_score <= 25
            else "중립~공포 구간. 시장 심리 정상화 중." if pd.notna(fg_score)
            else "VIX 데이터 부족."
        ),
        "_status": fg_status,
    })

    # Fed Funds Rate
    _fr = _results.get("fed") or (np.nan, _FRED_TRANSIENT_ERROR_NOTE)
    fed_rate, fed_note = _fr
    fed_status = (
        MACRO_STATUS_FAIL if pd.notna(fed_rate) and fed_rate >= 5.0
        else MACRO_STATUS_WARN if pd.notna(fed_rate) and fed_rate >= 3.0
        else MACRO_STATUS_PASS if pd.notna(fed_rate)
        else MACRO_STATUS_NA
    )
    rows.append({
        "지표": "연준 기준금리 (Fed Funds Rate)",
        "현재값": f"{fed_rate:.2f}%" if pd.notna(fed_rate) else "N/A",
        "판정": macro_status_label(fed_status),
        "판독 요약": fed_note,
        "_status": fed_status,
    })

    # ── FMP GDP 성장률 (QoQ %) ──────────────────────────────────────────
    # FMP 'GDP' API는 절대값(십억 달러 단위 연율)을 반환하므로
    # 전분기 대비 변화율(%)을 직접 계산해야 한다.
    _gdp = _results.get("gdp") or (np.nan, np.nan, "")
    gdp_abs, gdp_abs_prev, gdp_date = _gdp
    if pd.notna(gdp_abs) and pd.notna(gdp_abs_prev) and gdp_abs_prev != 0:
        gdp_qoq = float((gdp_abs / gdp_abs_prev - 1.0) * 100)
    else:
        gdp_qoq = np.nan

    if pd.notna(gdp_qoq):
        if gdp_qoq < 0:
            gdp_st = MACRO_STATUS_FAIL
            gdp_note = f"GDP QoQ {gdp_qoq:+.2f}% ({gdp_date}) — 마이너스 성장, 기술적 침체 위험 구간."
        elif gdp_qoq < 0.5:
            gdp_st = MACRO_STATUS_WARN
            gdp_note = f"GDP QoQ {gdp_qoq:+.2f}% ({gdp_date}) — 성장 정체 구간. 경기 모멘텀 약화 신호."
        elif gdp_qoq < 1.0:
            gdp_st = MACRO_STATUS_WARN
            gdp_note = f"GDP QoQ {gdp_qoq:+.2f}% ({gdp_date}) — 저성장 구간. 선별적 접근 필요."
        else:
            gdp_st = MACRO_STATUS_PASS
            gdp_note = f"GDP QoQ {gdp_qoq:+.2f}% ({gdp_date}) — 실물 경기 안정권."
        gdp_display = f"{gdp_qoq:+.2f}% (QoQ)"
    elif pd.notna(gdp_abs):
        # prev 없이 절대값만 있을 때 — 표시만 하고 NA 판정
        gdp_st = MACRO_STATUS_NA
        gdp_note = f"GDP 절대값 ${gdp_abs:,.0f}B ({gdp_date}). 전분기 데이터 없어 성장률 계산 불가."
        gdp_display = f"${gdp_abs:,.0f}B"
    else:
        gdp_st = MACRO_STATUS_NA
        gdp_note = "GDP 데이터 없음 (FMP 조회 실패 또는 API 키 미설정)."
        gdp_display = "N/A"

    rows.append({
        "지표": "GDP 성장률 QoQ (FMP)",
        "현재값": gdp_display,
        "판정": macro_status_label(gdp_st),
        "판독 요약": gdp_note,
        "_status": gdp_st,
    })

    # ── FMP Retail Sales (소매판매 MoM %) ──────────────────────────────
    # FMP 'retailSales' API는 절대값(백만 달러)을 반환하므로
    # 전월 대비 변화율(%)을 직접 계산해야 한다.
    _rs = _results.get("rs") or (np.nan, np.nan, "")
    rs_abs, rs_abs_prev, rs_date = _rs
    if pd.notna(rs_abs) and pd.notna(rs_abs_prev) and rs_abs_prev != 0:
        rs_mom = float((rs_abs / rs_abs_prev - 1.0) * 100)
    else:
        rs_mom = np.nan

    if pd.notna(rs_mom):
        if rs_mom < -1.0:
            rs_st = MACRO_STATUS_FAIL
            rs_note = f"소매판매 MoM {rs_mom:+.2f}% ({rs_date}) — 소비 급감, 경기 침체 우려."
        elif rs_mom < 0:
            rs_st = MACRO_STATUS_WARN
            rs_note = f"소매판매 MoM {rs_mom:+.2f}% ({rs_date}) — 소비 모멘텀 둔화."
        elif rs_mom < 0.3:
            rs_st = MACRO_STATUS_WARN
            rs_note = f"소매판매 MoM {rs_mom:+.2f}% ({rs_date}) — 소비 정체 구간. 모니터링 필요."
        else:
            rs_st = MACRO_STATUS_PASS
            rs_note = f"소매판매 MoM {rs_mom:+.2f}% ({rs_date}) — 소비 체력 유지."
        rs_display = f"{rs_mom:+.2f}% (MoM)"
    elif pd.notna(rs_abs):
        rs_st = MACRO_STATUS_NA
        rs_note = f"소매판매 절대값 ${rs_abs:,.0f}M ({rs_date}). 전월 데이터 없어 변화율 계산 불가."
        rs_display = f"${rs_abs:,.0f}M"
    else:
        rs_st = MACRO_STATUS_NA
        rs_note = "소매판매 데이터 없음."
        rs_display = "N/A"

    rows.append({
        "지표": "소매판매 MoM (FMP)",
        "현재값": rs_display,
        "판정": macro_status_label(rs_st),
        "판독 요약": rs_note,
        "_status": rs_st,
    })

    # ── FMP Market Risk Premium (ERP) ──────────────────────────────────
    _erp = _results.get("erp") or (np.nan, "조회 실패")
    erp_val, erp_note = _erp
    if pd.notna(erp_val):
        if erp_val >= 5.5:
            erp_st = MACRO_STATUS_PASS
        elif erp_val >= 3.5:
            erp_st = MACRO_STATUS_WARN
        else:
            erp_st = MACRO_STATUS_FAIL
    else:
        erp_st = MACRO_STATUS_NA
    rows.append({
        "지표": "주식 위험 프리미엄 ERP (FMP)",
        "현재값": f"{erp_val:.2f}%" if pd.notna(erp_val) else "N/A",
        "판정": macro_status_label(erp_st),
        "판독 요약": erp_note,
        "_status": erp_st,
    })

    bad_total = sum(1 for r in rows if macro_status_bad_count(r["_status"]))
    na_total = sum(1 for r in rows if r["_status"] == MACRO_STATUS_NA)
    macro_score, macro_grade, macro_desc = compute_macro_score(rows)

    return {
        "rows": rows,
        "bad_total": bad_total,
        "vix_hist": vix_hist,
        "spread_val": spread_val,
        "vix_val": vix_val,
        "na_total": na_total,
        "macro_score": macro_score,
        "macro_grade": macro_grade,
        "macro_desc": macro_desc,
        "fg_score": fg_score,
        "fg_rating": fg_rating,
        "fed_rate": fed_rate,
    }


def calculate_rsi(close_series, window=14):
    """Calculate RSI from close price series."""
    if close_series is None:
        return pd.Series(dtype=float)

    close = pd.to_numeric(close_series, errors="coerce").dropna()
    if close.empty:
        return pd.Series(dtype=float)

    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.rolling(window=window, min_periods=window).mean()
    avg_loss = losses.rolling(window=window, min_periods=window).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_period_return(close_series, lookback_days):
    """
    Return percentage performance based on lookback trading days.
    Example: lookback_days=5 means return vs 5 trading days ago.
    """
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


def hidden_alpha_horizon_returns(close_series):
    """
    Trading-day horizons for Hidden Alpha: 5 / 10 / 21 / 63 over daily closes.
    """
    return {
        "1주(%)": calculate_period_return(close_series, 5),
        "2주(%)": calculate_period_return(close_series, 10),
        "1개월(%)": calculate_period_return(close_series, 21),
        "3개월(%)": calculate_period_return(close_series, 63),
    }


def get_close_prices_from_download(download_df):
    """
    Normalize price-download output to a close-price DataFrame with ticker columns.
    """
    if download_df is None or download_df.empty:
        return pd.DataFrame()

    if isinstance(download_df.columns, pd.MultiIndex):
        if "Close" in download_df.columns.get_level_values(0):
            close_df = download_df["Close"].copy()
        elif "Adj Close" in download_df.columns.get_level_values(0):
            close_df = download_df["Adj Close"].copy()
        else:
            return pd.DataFrame()
    else:
        # Single ticker fallback
        if "Close" in download_df.columns:
            close_df = download_df[["Close"]].copy()
            close_df.columns = ["SINGLE"]
        elif "Adj Close" in download_df.columns:
            close_df = download_df[["Adj Close"]].copy()
            close_df.columns = ["SINGLE"]
        else:
            return pd.DataFrame()

    return close_df


def build_sector_returns_table(close_df, sector_etfs):
    records = []
    for ticker, sector_name in sector_etfs:
        series = close_df[ticker] if ticker in close_df.columns else pd.Series(dtype=float)
        records.append(
            {
                "Ticker": ticker,
                "Sector": sector_name,
                "1-Week (%)": calculate_period_return(series, 5),
                "1-Month (%)": calculate_period_return(series, 21),
                "3-Month (%)": calculate_period_return(series, 63),
                "6-Month (%)": calculate_period_return(series, 126),
                "1-Year (%)": calculate_period_return(series, 252),
            }
        )
    ordered_cols = [
        "Sector",
        "Ticker",
        "1-Week (%)",
        "1-Month (%)",
        "3-Month (%)",
        "6-Month (%)",
        "1-Year (%)",
    ]
    return pd.DataFrame(records)[ordered_cols]


def build_etf_universe_returns_table(pool_tickers):
    """
    유니버스 티커별 수익률을 한 번에 계산한다. 신규 ETF 등 과거 일부 구간이 비어도 티커 행은 유지한다(NaN).
    """
    cols = ["Ticker", "1주(%)", "2주(%)", "1개월(%)", "3개월(%)", "1-Month (%)"]
    if not pool_tickers:
        return pd.DataFrame(columns=cols)

    unique_tickers = list(
        dict.fromkeys(str(t).strip().upper() for t in pool_tickers if str(t).strip())
    )
    if not unique_tickers:
        return pd.DataFrame(columns=cols)

    try:
        close_df = _fmp_batch_to_close_df(unique_tickers, limit=130)
    except Exception:
        close_df = pd.DataFrame()

    if close_df.empty:
        rows = [
            {"Ticker": t, "1주(%)": np.nan, "2주(%)": np.nan, "1개월(%)": np.nan, "3개월(%)": np.nan, "1-Month (%)": np.nan}
            for t in unique_tickers
        ]
        return pd.DataFrame(rows)

    if len(unique_tickers) == 1 and "SINGLE" in close_df.columns:
        close_df = close_df.copy()
        close_df.columns = [unique_tickers[0]]

    rows = []
    for ticker in unique_tickers:
        series = close_df[ticker] if ticker in close_df.columns else pd.Series(dtype=float)
        h = hidden_alpha_horizon_returns(series)
        one_m = h["1개월(%)"]
        rows.append(
            {
                "Ticker": ticker,
                "1주(%)": h["1주(%)"],
                "2주(%)": h["2주(%)"],
                "1개월(%)": one_m,
                "3개월(%)": h["3개월(%)"],
                "1-Month (%)": one_m,
            }
        )

    result_df = pd.DataFrame(rows)
    for c in ("1주(%)", "2주(%)", "1개월(%)", "3개월(%)", "1-Month (%)"):
        result_df[c] = pd.to_numeric(result_df[c], errors="coerce")
    return result_df


def compute_rs_score_weekly_change(pool_tickers: list, spy_series_now: pd.Series = None) -> pd.DataFrame:
    """
    1주 전 RS Score 대비 현재 RS Score 변화율 계산.
    - RS Score = 종목 3개월 수익률 - SPY 3개월 수익률
    - RS 변화 = (현재 RS) - (1주 전 RS) → 양수면 최근 1주간 시장 대비 강해짐
    반환: DataFrame [Ticker, RS_Now, RS_1W_Ago, RS_Change, RS_Signal]
    """
    if not pool_tickers:
        return pd.DataFrame()
    try:
        unique = list(dict.fromkeys(str(t).strip().upper() for t in pool_tickers if str(t).strip()))
        all_tickers = list(dict.fromkeys(unique + ["SPY"]))
        close_df = _fmp_batch_to_close_df(all_tickers, limit=130)
        if close_df.empty:
            return pd.DataFrame()

        spy = pd.to_numeric(close_df.get("SPY", pd.Series(dtype=float)), errors="coerce").dropna()
        if len(spy) < 70:
            return pd.DataFrame()

        rows = []
        for tk in unique:
            if tk not in close_df.columns:
                continue
            s = pd.to_numeric(close_df[tk], errors="coerce").dropna()
            if len(s) < 70:
                continue
            # 현재 RS (3개월 = 63거래일)
            rs_now = float((s.iloc[-1]/s.iloc[-64] - 1)*100 - (spy.iloc[-1]/spy.iloc[-64] - 1)*100) if len(s) >= 64 and len(spy) >= 64 else np.nan
            # 1주 전(5거래일 전) RS
            rs_1w = float((s.iloc[-6]/s.iloc[-69] - 1)*100 - (spy.iloc[-6]/spy.iloc[-69] - 1)*100) if len(s) >= 69 and len(spy) >= 69 else np.nan
            rs_change = float(rs_now - rs_1w) if pd.notna(rs_now) and pd.notna(rs_1w) else np.nan

            # 신호 분류
            if pd.notna(rs_change):
                if rs_now > 0 and rs_change > 2:
                    signal = "🚀 급부상"       # 강하면서 더 강해짐
                elif rs_now < 0 and rs_change > 2:
                    signal = "🌱 Early Signal"  # 약했는데 강해지기 시작 ← 핵심!
                elif rs_now > 0 and rs_change < -2:
                    signal = "⚠️ 모멘텀 약화"   # 강했는데 약해지기 시작 ← 매도 주의
                elif rs_change < -3:
                    signal = "🔴 급하락"
                else:
                    signal = "➡️ 유지"
            else:
                signal = "N/A"

            rows.append({
                "Ticker": tk,
                "RS_Now": round(rs_now, 2) if pd.notna(rs_now) else np.nan,
                "RS_1W_Ago": round(rs_1w, 2) if pd.notna(rs_1w) else np.nan,
                "RS_Change": round(rs_change, 2) if pd.notna(rs_change) else np.nan,
                "RS_Signal": signal,
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("RS_Change", ascending=False, na_position="last")
        return df
    except Exception:
        return pd.DataFrame()


def verify_emerging_with_quant(emerging_tickers: list, narrative_date: str = "") -> list[dict]:
    """
    내러티브 Emerging 종목을 정량 데이터로 교차 검증.
    "아직 안 오른 2차 수혜주"를 자동으로 분류.
    반환: [{"ticker", "rs_score", "mom_1m", "above_ma200", "vol_surge", "verdict", "verdict_emoji"}]
    """
    if not emerging_tickers:
        return []
    try:
        unique = list(dict.fromkeys(str(t).strip().upper() for t in emerging_tickers if str(t).strip()))
        all_tickers = list(dict.fromkeys(unique + ["SPY"]))
        close_df = _fmp_batch_to_close_df(all_tickers, limit=130)
        if close_df.empty:
            return []

        spy = pd.to_numeric(close_df.get("SPY", pd.Series(dtype=float)), errors="coerce").dropna()
        spy_3m = float((spy.iloc[-1]/spy.iloc[-64] - 1)*100) if len(spy) >= 64 else 0.0

        results = []
        for tk in unique:
            if tk not in close_df.columns:
                continue
            s = pd.to_numeric(close_df[tk], errors="coerce").dropna()
            if len(s) < 22:
                continue
            mom_1m = float((s.iloc[-1]/s.iloc[-22] - 1)*100) if len(s) >= 22 else np.nan
            mom_3m = float((s.iloc[-1]/s.iloc[-64] - 1)*100) if len(s) >= 64 else np.nan
            rs_score = float(mom_3m - spy_3m) if pd.notna(mom_3m) else np.nan
            ma200 = float(s.rolling(200, min_periods=150).mean().iloc[-1]) if len(s) >= 150 else np.nan
            above_ma200 = bool(s.iloc[-1] > ma200) if pd.notna(ma200) else None

            # 거래량 급증 (cached_timing_price_history 재사용)
            try:
                _hist_vol = cached_timing_price_history(tk)
                if not _hist_vol.empty and "Volume" in _hist_vol.columns:
                    vol = pd.to_numeric(_hist_vol["Volume"], errors="coerce").dropna()
                    vol_surge = float(vol.tail(5).mean() / vol.tail(21).mean()) if len(vol) >= 21 else np.nan
                else:
                    vol_surge = np.nan
            except Exception:
                vol_surge = np.nan

            # ── 핵심 판정 ─────────────────────────────────────────────────
            # 🎯 최적 매수 타이밍: RS 음수(아직 안 올랐음) + 거래량 급증(돈 들어오기 시작)
            # 🌱 얼리버드: RS 낮음 + 200일선 위 + 최근 모멘텀 상승
            # ✅ 이미 강세: RS 양수 + 모멘텀 확인 (늦지 않음)
            # ⏳ 대기: 아직 신호 없음
            # ❌ 하락 추세: RS 음수 + 200일선 아래

            if pd.notna(rs_score) and rs_score < 0 and pd.notna(vol_surge) and vol_surge >= 1.5:
                verdict = "🎯 최적 매수 타이밍"
                detail = f"RS {rs_score:+.1f}%p (아직 저평가) + 거래량 {vol_surge:.1f}x 급증"
            elif pd.notna(rs_score) and rs_score < 3 and above_ma200 and pd.notna(mom_1m) and mom_1m > 2:
                verdict = "🌱 얼리버드 기회"
                detail = f"RS {rs_score:+.1f}%p + 200일선 위 + 1개월 {mom_1m:+.1f}%"
            elif pd.notna(rs_score) and rs_score > 5 and above_ma200:
                verdict = "✅ 이미 강세 (진입 시 고점 주의)"
                detail = f"RS {rs_score:+.1f}%p + 200일선 위"
            elif above_ma200 is False:
                verdict = "❌ 하락 추세 (대기)"
                detail = "200일선 아래 — 아직 때가 아님"
            else:
                verdict = "⏳ 신호 대기"
                detail = f"RS {rs_score:+.1f}%p" if pd.notna(rs_score) else "데이터 부족"

            results.append({
                "ticker": tk,
                "rs_score": round(rs_score, 2) if pd.notna(rs_score) else None,
                "mom_1m": round(mom_1m, 2) if pd.notna(mom_1m) else None,
                "above_ma200": above_ma200,
                "vol_surge": round(vol_surge, 2) if pd.notna(vol_surge) else None,
                "verdict": verdict,
                "detail": detail,
                "current_price": round(float(s.iloc[-1]), 4) if not s.empty and pd.notna(s.iloc[-1]) else None,  # ✅ Watchlist saved_price용
            })

        results.sort(key=lambda x: (
            0 if "최적" in x["verdict"] else
            1 if "얼리" in x["verdict"] else
            2 if "이미" in x["verdict"] else
            3 if "대기" in x["verdict"] else 4
        ))
        return results
    except Exception:
        return []


def detect_sector_momentum_reversal(universe_tickers: list) -> list[dict]:
    """
    섹터 꺾임 감지: 연속 2주 RS 하락 + 거래량 감소 + 가격 유지 패턴.
    반환: [{"ticker", "signal", "rs_now", "rs_1w", "rs_change", "vol_change", "description"}]
    """
    if not universe_tickers:
        return []
    try:
        all_tickers = list(dict.fromkeys([t.strip().upper() for t in universe_tickers if t.strip()] + ["SPY"]))
        batch = _fmp_batch_price_history(all_tickers, limit=130)
        close_df = pd.DataFrame({tk: df["Close"] for tk, df in batch.items() if "Close" in df.columns}).sort_index()
        vol_raw = pd.DataFrame({tk: df["Volume"] for tk, df in batch.items() if "Volume" in df.columns}).sort_index()
        if close_df.empty:
            return []

        spy = pd.to_numeric(close_df.get("SPY", pd.Series(dtype=float)), errors="coerce").dropna()
        if len(spy) < 75:
            return []

        def calc_rs(s, spy, offset_start=0, offset_end=0):
            try:
                s_start = offset_start + 63
                s_end = offset_start
                spy_start = offset_start + 63
                spy_end = offset_start
                s_ret = float((s.iloc[-(1+s_end)] / s.iloc[-(1+s_start)] - 1) * 100)
                spy_ret = float((spy.iloc[-(1+spy_end)] / spy.iloc[-(1+spy_start)] - 1) * 100)
                return s_ret - spy_ret
            except Exception:
                return np.nan

        alerts = []
        for tk in all_tickers:
            if tk == "SPY" or tk not in close_df.columns:
                continue
            s = pd.to_numeric(close_df[tk], errors="coerce").dropna()
            if len(s) < 75:
                continue

            rs_now = calc_rs(s, spy, 0)        # 현재 RS
            rs_1w = calc_rs(s, spy, 5)         # 1주 전 RS
            rs_2w = calc_rs(s, spy, 10)        # 2주 전 RS

            if not (pd.notna(rs_now) and pd.notna(rs_1w) and pd.notna(rs_2w)):
                continue

            rs_change_1w = rs_now - rs_1w
            rs_change_2w = rs_1w - rs_2w
            consecutive_drop = rs_change_1w < -1.5 and rs_change_2w < -1.5

            # 거래량 변화
            vol_change = np.nan
            if not vol_raw.empty and tk in vol_raw.columns:
                vol = pd.to_numeric(vol_raw[tk], errors="coerce").dropna()
                if len(vol) >= 21:
                    recent_avg = float(vol.tail(5).mean())
                    baseline_avg = float(vol.tail(21).mean())
                    vol_change = (recent_avg / baseline_avg - 1) * 100 if baseline_avg > 0 else np.nan

            vol_declining = pd.notna(vol_change) and vol_change < -20

            # 신호 결정
            if consecutive_drop and rs_now > 0:
                signal = "⚠️ 매도 주의"
                desc = f"2주 연속 RS 하락 (현재 {rs_now:+.1f}%p → {rs_change_1w:+.1f}%p/주)"
                if vol_declining:
                    signal = "🔴 분산 매도 감지"
                    desc += f" + 거래량 {vol_change:+.0f}% 감소 (기관 매도 가능성)"
            elif rs_change_1w < -3 and rs_now < 0:
                signal = "🔴 급격한 약세 전환"
                desc = f"RS {rs_now:+.1f}%p + 주간 변화 {rs_change_1w:+.1f}%p"
            else:
                continue  # 정상 → 생략

            alerts.append({
                "ticker": tk,
                "signal": signal,
                "rs_now": round(rs_now, 2),
                "rs_1w": round(rs_1w, 2),
                "rs_change": round(rs_change_1w, 2),
                "vol_change": round(vol_change, 1) if pd.notna(vol_change) else None,
                "description": desc,
            })

        alerts.sort(key=lambda x: x["rs_change"])
        return alerts
    except Exception:
        return []


def build_pool_monthly_returns_table(pool_tickers):
    """Download pool prices once and calculate 1-month returns per ticker (공통 빌더 사용)."""
    df = build_etf_universe_returns_table(pool_tickers)
    if df.empty:
        return pd.DataFrame(columns=["Ticker", "1-Month (%)"])
    out = df[["Ticker", "1-Month (%)"]].copy()
    return out.sort_values("1-Month (%)", ascending=False, na_position="last").reset_index(drop=True)


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_analyze_us_macro_dashboard():
    return analyze_us_macro_dashboard()


def get_macro_dashboard_with_validation():
    """rows가 8개 미만이면 캐시를 자동 무효화하고 재호출."""
    pack = cached_analyze_us_macro_dashboard()
    if pack and len(pack.get("rows", [])) < 10:
        cached_analyze_us_macro_dashboard.clear()
        pack = cached_analyze_us_macro_dashboard()
    return pack


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_sector_etf_closes(tickers_tuple: tuple[str, ...]):
    if not tickers_tuple:
        return pd.DataFrame()
    return _fmp_batch_to_close_df(list(tickers_tuple), limit=500)


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_pool_monthly_returns(tickers_tuple: tuple[str, ...]):
    return build_pool_monthly_returns_table(list(tickers_tuple))


@st.cache_data(ttl=3600, show_spinner=False)
def cached_etf_universe_rankings_full(universe_tickers_tuple: tuple[str, ...]):
    """
    etf_universe 기준 Hidden Alpha·포트폴리오 유니버스 랭킹 단일 소스.
    1개월 수익률 내림차순으로 밀집 순위(1…N)를 부여하며, TTL 1시간 캐시.
    """
    if not universe_tickers_tuple:
        return pd.DataFrame()
    base = build_etf_universe_returns_table(list(universe_tickers_tuple))
    if base.empty:
        return pd.DataFrame()
    out = base.sort_values("1-Month (%)", ascending=False, na_position="last").reset_index(drop=True)
    out.insert(0, "순위", np.arange(1, len(out) + 1))
    out["Rank"] = out["순위"]
    out["rk_1w"] = out["1주(%)"].rank(method="min", ascending=False)
    out["rk_1m"] = out["1개월(%)"].rank(method="min", ascending=False)
    out["주도주"] = (
        (out["rk_1w"] <= 5)
        & (out["rk_1m"] <= 5)
        & (out["1주(%)"] > 0)
        & (out["1개월(%)"] > 0)
    )
    return out


def cached_etf_universe_momentum_rankings(universe_tickers_tuple: tuple[str, ...]):
    """포트폴리오 등 호환용: `cached_etf_universe_rankings_full`의 부분 열만 반환 (별도 캐시 없음)."""
    full = cached_etf_universe_rankings_full(universe_tickers_tuple)
    if full is None or full.empty:
        return pd.DataFrame(columns=["Ticker", "1-Month (%)", "Rank"])
    return full[["Ticker", "1-Month (%)", "Rank"]].copy()


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_quote_type(ticker_upper: str):
    """종목 타입 (EQUITY/ETF 등) — FMP profile 사용."""
    try:
        p = _fmp_profile(ticker_upper)
        if p:
            if p.get("isEtf") or p.get("isActivelyTrading") is False:
                return "ETF"
            qt = str(p.get("quoteType") or "").strip().upper()
            if qt:
                return qt
        return "EQUITY"
    except Exception:
        return ""


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_timing_price_history(ticker_upper: str):
    """1년 일봉 히스토리 — 기술적 분석 전용 (1Y 기간만)."""
    k = _fmp_key()
    if not k:
        return pd.DataFrame()
    try:
        r = requests.get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker_upper.strip().upper()}&limit=260&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        rename_map = {"close": "Close", "open": "Open", "high": "High",
                      "low": "Low", "volume": "Volume"}
        df = df.rename(columns=rename_map)
        for col in ["Close", "Open", "High", "Low", "Volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        # 정확히 1년치만 반환
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=1)
        df = df[df.index >= cutoff]
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)  # 10분 TTL — 지표 변화 빠른 반영
def cached_evaluate_kpis_snapshot(ticker_upper: str):
    return evaluate_kpis(str(ticker_upper).strip().upper())


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_earnings_history(ticker_upper: str) -> pd.DataFrame:
    """분기별 EPS 어닝 서프라이즈 — FMP earnings-surprises + income-statement 사용."""
    k = _fmp_key()
    if not k:
        return pd.DataFrame()

    # 방법 1: earnings-surprises (과거 EPS 실제 vs 예상)
    try:
        r = requests.get(
            f"{_FMP_BASE}/earnings-surprises?symbol={ticker_upper}&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        data = r.json() if r.status_code == 200 else []
        if isinstance(data, list) and len(data) >= 2:
            rows = []
            today_str = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
            for item in data[:8]:
                date_str = str(item.get("date") or item.get("fiscalDateEnding") or "")[:10]
                if not date_str or date_str >= today_str:
                    continue
                eps_actual = to_float(
                    item.get("actualEarningResult") or item.get("actualEPS") or item.get("eps")
                )
                eps_est = to_float(
                    item.get("estimatedEarning") or item.get("estimatedEPS") or item.get("epsEstimated")
                )
                surprise = "N/A"
                if pd.notna(eps_actual) and pd.notna(eps_est) and eps_est != 0:
                    surprise_pct = (eps_actual - eps_est) / abs(eps_est) * 100
                    surprise = f"{surprise_pct:+.1f}%"
                rows.append({
                    "분기": date_str,
                    "EPS 실제": f"${eps_actual:.3f}" if pd.notna(eps_actual) else "N/A",
                    "EPS 예상": f"${eps_est:.3f}" if pd.notna(eps_est) else "N/A",
                    "어닝 서프라이즈": surprise,
                })
            if rows:
                return pd.DataFrame(rows)
    except Exception:
        pass

    # 방법 2: income-statement (quarterly)
    try:
        r = requests.get(
            f"{_FMP_BASE}/income-statement?symbol={ticker_upper}&period=quarter&limit=8&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        data = r.json() if r.status_code == 200 else []
        if isinstance(data, list) and data:
            rows = []
            for item in data[:8]:
                date_str = str(item.get("date") or item.get("period") or "")[:10]
                eps = to_float(item.get("eps") or item.get("epsdiluted"))
                revenue = to_float(item.get("revenue"))
                net_income = to_float(item.get("netIncome"))
                if not date_str:
                    continue
                rows.append({
                    "분기": date_str,
                    "EPS 실제": f"${eps:.3f}" if pd.notna(eps) else "N/A",
                    "매출": f"${revenue/1e9:.2f}B" if pd.notna(revenue) else "N/A",
                    "순이익": f"${net_income/1e9:.2f}B" if pd.notna(net_income) else "N/A",
                })
            if rows:
                return pd.DataFrame(rows)
    except Exception:
        pass
    return pd.DataFrame()



@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_institutional_holders(ticker_upper: str) -> pd.DataFrame:
    """기관 보유 비중 — FMP stable/institutional-ownership/extract-analytics/holder 사용."""
    k = _fmp_key()
    if not k:
        return pd.DataFrame()

    # 최근 3개 분기 순서로 시도 (데이터가 있는 분기까지)
    now = datetime.now(_MARKET_ET_TZ)
    q_now = (now.month - 1) // 3
    year_now = now.year
    if q_now == 0:
        q_now = 4
        year_now -= 1

    # 최근 3개 분기 목록
    quarters_to_try = []
    y, q = year_now, q_now
    for _ in range(3):
        quarters_to_try.append((str(y), str(q)))
        q -= 1
        if q == 0:
            q = 4
            y -= 1

    for year, quarter in quarters_to_try:
        try:
            r = requests.get(
                f"{_FMP_BASE}/institutional-ownership/extract-analytics/holder"
                f"?symbol={ticker_upper}&year={year}&quarter={quarter}&limit=10&apikey={k}",
                timeout=_FMP_TIMEOUT
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    rows = []
                    for item in data[:10]:
                        investor = str(item.get("investorName") or item.get("name") or "")
                        weight = to_float(item.get("weight"))
                        last_weight = to_float(item.get("lastWeight"))
                        change_pct = to_float(item.get("changeInWeightPercentage") or item.get("changeInWeight"))
                        shares = to_float(item.get("sharesNumber") or item.get("shares"))
                        if not investor:
                            continue
                        rows.append({
                            "기관명": investor[:28],
                            "비중(%)": f"{float(weight):.2f}%" if pd.notna(weight) else "N/A",
                            "전분기 비중": f"{float(last_weight):.2f}%" if pd.notna(last_weight) else "N/A",
                            "비중 변화": f"{float(change_pct):+.2f}%" if pd.notna(change_pct) else "N/A",
                            "보유 주식수": f"{int(shares):,}" if pd.notna(shares) else "N/A",
                        })
                    if rows:
                        return pd.DataFrame(rows)
        except Exception:
            continue

    # fallback: symbol-positions-summary
    for year, quarter in quarters_to_try:
        try:
            r2 = requests.get(
                f"{_FMP_BASE}/institutional-ownership/symbol-positions-summary"
                f"?symbol={ticker_upper}&year={year}&quarter={quarter}&apikey={k}",
                timeout=_FMP_TIMEOUT
            )
            if r2.status_code == 200:
                data2 = r2.json()
                item = data2[0] if isinstance(data2, list) and data2 else {}
                if item:
                    rows = [{
                        "항목": "기관 투자자 수",
                        "값": f"{int(item.get('investorsHolding', 0)):,}",
                        "전분기 대비": f"{int(item.get('investorsHoldingChange', 0)):+,}",
                    }, {
                        "항목": "13F 보유 주식 수",
                        "값": f"{int(item.get('numberOfOf13Fshares', 0)):,}",
                        "전분기 대비": f"{int(item.get('numberOfOf13FsharesChange', 0)):+,}",
                    }, {
                        "항목": "기관 보유 비율",
                        "값": f"{float(item.get('ownershipPercent', 0)):.2f}%",
                        "전분기 대비": f"{float(item.get('ownershipPercentChange', 0)):+.2f}%",
                    }]
                    return pd.DataFrame(rows)
        except Exception:
            continue

    return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def compute_daily_risk_gauge(sector_filter: str = "전체") -> dict:
    """
    Daily Risk Gauge — 하락장 선행 신호 6가지 종합 점수.
    sector_filter: "전체" | "테크·반도체" | "에너지" | "금융" | "헬스케어" | "산업재"
    신호 6: FMP Economic Calendar 이벤트 리스크 (당일~3일 이내 고임팩트 이벤트)
    """
    import time as _time

    sector_etf_map = {
        "전체":        {"주도주": ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"], "sector_etf": "SPY"},
        "테크·반도체": {"주도주": ["NVDA", "AMD", "SOXX", "SMH", "XLK"],  "sector_etf": "XLK"},
        "에너지":      {"주도주": ["XLE", "XOP", "CVX", "XOM", "OIH"],    "sector_etf": "XLE"},
        "금융":        {"주도주": ["XLF", "KRE", "JPM", "GS", "BAC"],     "sector_etf": "XLF"},
        "헬스케어":    {"주도주": ["XLV", "IBB", "UNH", "JNJ", "ABBV"],   "sector_etf": "XLV"},
        "산업재":      {"주도주": ["XLI", "BA", "CAT", "GE", "HON"],      "sector_etf": "XLI"},
        "소비재":      {"주도주": ["XLY", "XLP", "AMZN", "HD", "MCD"],    "sector_etf": "XLY"},
        "부동산":      {"주도주": ["XLRE", "VNQ", "AMT", "PLD", "EQIX"],  "sector_etf": "XLRE"},
    }
    cfg = sector_etf_map.get(sector_filter, sector_etf_map["전체"])
    leaders = cfg["주도주"]
    sector_etf = cfg["sector_etf"]

    signals = {}
    warnings = []
    details = {}
    import concurrent.futures as _cf_drg

    k_drg = _fmp_key()

    # ══════════════════════════════════════════════════════════════════════════
    # 모든 외부 API 데이터를 병렬로 미리 fetch
    # ══════════════════════════════════════════════════════════════════════════
    def _fetch_vix_data():
        try:
            v, h, e = fetch_vix_latest_and_history()
            return ("vix", (v, h, e))
        except Exception:
            return ("vix", (np.nan, None, "오류"))

    def _fetch_credit_data():
        try:
            tickers = ["HYG", "LQD"]
            df = _fmp_batch_to_close_df(tickers, limit=30)
            return ("credit", df)
        except Exception:
            return ("credit", pd.DataFrame())

    def _fetch_leaders_data():
        try:
            tickers_dl = list(dict.fromkeys(leaders + ["SPY"]))
            df = _fmp_batch_to_close_df(tickers_dl, limit=30)
            return ("leaders", df)
        except Exception:
            return ("leaders", pd.DataFrame())

    def _fetch_futures_data():
        """프리마켓 방향 — SPY/QQQ stock-price-change API (1일 변화율).
        FMP는 ES=F/NQ=F 선물 심볼 미지원. stock-price-change가 가장 정확한 대체.
        프리마켓 시간대에는 quote의 price가 프리마켓 가격을 반영함.
        """
        results = {}
        if not k_drg:
            return ("futures", results)

        # 1차: stock-price-change API (1D 변화율 — 가장 신뢰도 높음)
        try:
            r_chg = requests.get(
                f"{_FMP_BASE}/stock-price-change?symbol=SPY,QQQ&apikey={k_drg}",
                timeout=_FMP_TIMEOUT,
            )
            if r_chg.status_code == 200:
                data_chg = r_chg.json()
                items = data_chg if isinstance(data_chg, list) else [data_chg]
                for item in items:
                    sym = str(item.get("symbol", "")).upper()
                    chg1d = to_float(item.get("1D") or item.get("1d") or item.get("day"))
                    if sym in ("SPY", "QQQ") and pd.notna(chg1d):
                        results[sym] = {"chg_pct": chg1d, "source": "price-change"}
        except Exception:
            pass

        # 2차: quote API 폴백 (price vs previousClose)
        for sym in ["SPY", "QQQ"]:
            if sym in results:
                continue
            try:
                r_q = requests.get(
                    f"{_FMP_BASE}/quote?symbol={sym}&apikey={k_drg}",
                    timeout=_FMP_TIMEOUT,
                )
                if r_q.status_code == 200:
                    d_q = r_q.json()
                    item = d_q[0] if isinstance(d_q, list) and d_q else d_q
                    price = to_float(item.get("price"))
                    prev  = to_float(item.get("previousClose") or item.get("prevClose"))
                    chg_pct = to_float(item.get("changesPercentage") or item.get("changePercent"))
                    if pd.notna(chg_pct):
                        results[sym] = {"chg_pct": chg_pct, "source": "quote"}
                    elif pd.notna(price) and pd.notna(prev) and prev > 0:
                        results[sym] = {"chg_pct": (price / prev - 1) * 100, "source": "quote"}
            except Exception:
                pass

        return ("futures", results)

    def _fetch_riskoff_data():
        results = {}
        if not k_drg:
            return ("riskoff", results)
        # UUP, GLD, IEF를 배치로 한 번에
        try:
            df = _fmp_batch_to_close_df(["UUP", "GLD", "IEF"], limit=10)
            name_map = {"UUP": "달러", "GLD": "금", "IEF": "국채(7-10Y)"}
            for sym, name in name_map.items():
                if sym in df.columns:
                    s = pd.to_numeric(df[sym], errors="coerce").dropna()
                    chg = float((s.iloc[-1] / s.iloc[-6] - 1) * 100) if len(s) >= 6 else np.nan
                    results[sym] = {"name": name, "chg_pct": chg}
        except Exception:
            pass
        return ("riskoff", results)

    def _fetch_calendar_data():
        try:
            return ("calendar", fetch_fmp_economic_calendar_risk())
        except Exception:
            return ("calendar", ([], False))

    def _fetch_news_data():
        news = []
        if not k_drg:
            return ("news", news)
        try:
            risk_tickers = ["SPY", sector_etf] + leaders[:2]
            for tk in list(dict.fromkeys(risk_tickers))[:3]:
                try:
                    r = requests.get(
                        f"{_FMP_BASE}/stock-news?symbols={tk}&limit=3&apikey={k_drg}",
                        timeout=_FMP_TIMEOUT
                    )
                    if r.status_code == 200:
                        tk_news = r.json()
                        if isinstance(tk_news, list):
                            for n in tk_news[:3]:
                                title = str(n.get("title") or "")
                                if title:
                                    news.append({
                                        "ticker": tk,
                                        "title": title,
                                        "publisher": str(n.get("site") or n.get("publisher") or ""),
                                        "link": str(n.get("url") or n.get("link") or ""),
                                        "pub_time": n.get("publishedDate") or n.get("date"),
                                    })
                except Exception:
                    pass
        except Exception:
            pass
        return ("news", news)

    # 7개 fetch 함수를 동시 실행
    _prefetch_tasks = [
        _fetch_vix_data, _fetch_credit_data, _fetch_leaders_data,
        _fetch_futures_data, _fetch_riskoff_data, _fetch_calendar_data,
        _fetch_news_data,
    ]
    _prefetch = {}
    with _cf_drg.ThreadPoolExecutor(max_workers=7) as _exe_drg:
        _futs_drg = {_exe_drg.submit(fn): fn.__name__ for fn in _prefetch_tasks}
        for fut in _cf_drg.as_completed(_futs_drg, timeout=20):
            try:
                key, val = fut.result()
                _prefetch[key] = val
            except Exception:
                pass

    # ── 신호 1: VIX 방향 전환 ────────────────────────────────────────────
    vix_close = pd.Series(dtype=float)
    try:
        _vix_val, _vix_hist, _vix_err = _prefetch.get("vix", (np.nan, None, "N/A"))
        if _vix_hist is not None and not _vix_hist.empty and "Close" in _vix_hist.columns:
            _s30 = pd.to_numeric(_vix_hist["Close"], errors="coerce").dropna().tail(30)
            if len(_s30) >= 10:
                vix_close = _s30
    except Exception:
        pass
    try:
        if len(vix_close) >= 10:
            vix_now = float(vix_close.iloc[-1])
            vix_20d_avg = float(vix_close.tail(20).mean())
            vix_trend = vix_now - float(vix_close.iloc[-6]) if len(vix_close) >= 6 else 0
            vix_alert = vix_trend > 2 or vix_now > vix_20d_avg * 1.15
            signals["vix"] = {"ok": not vix_alert, "value": f"{vix_now:.1f}", "trend": f"{vix_trend:+.1f}/5일"}
            details["VIX"] = {"현재값": f"{vix_now:.1f}", "5일 변화": f"{vix_trend:+.1f}", "20일 평균": f"{vix_20d_avg:.1f}"}
            if vix_alert:
                warnings.append(f"⚠️ VIX 상승 전환 감지 ({vix_now:.1f}, +{vix_trend:.1f}/5일)")
        else:
            signals["vix"] = {"ok": True, "value": "N/A", "trend": "N/A"}
    except Exception:
        signals["vix"] = {"ok": True, "value": "N/A", "trend": "N/A"}

    # ── 신호 2: 신용 스프레드 (HYG/LQD) ─────────────────────────────────
    try:
        _credit_df = _prefetch.get("credit", pd.DataFrame())
        hyg = pd.to_numeric(_credit_df.get("HYG", pd.Series(dtype=float)), errors="coerce").dropna()
        lqd = pd.to_numeric(_credit_df.get("LQD", pd.Series(dtype=float)), errors="coerce").dropna()
        if len(hyg) >= 10 and len(lqd) >= 10:
            spread_now = float(hyg.iloc[-1] / lqd.iloc[-1])
            spread_5d = float(hyg.iloc[-6] / lqd.iloc[-6]) if len(hyg) >= 6 else spread_now
            spread_chg = (spread_now / spread_5d - 1) * 100
            spread_alert = spread_chg < -0.5
            signals["credit"] = {"ok": not spread_alert, "value": f"{spread_chg:+.2f}%/5일", "trend": ""}
            details["HYG/LQD 스프레드"] = {"5일 변화": f"{spread_chg:+.2f}%", "판정": "⚠️ 축소(위험)" if spread_alert else "✅ 정상"}
            if spread_alert:
                warnings.append(f"⚠️ 신용 스프레드 축소 ({spread_chg:+.2f}%/5일) — 리스크오프 신호")
        else:
            signals["credit"] = {"ok": True, "value": "N/A", "trend": ""}
    except Exception:
        signals["credit"] = {"ok": True, "value": "N/A", "trend": ""}

    # ── 신호 3: 대장주 모멘텀 약화 ───────────────────────────────────────
    try:
        close_df = _prefetch.get("leaders", pd.DataFrame())
        leader_alerts = []
        leader_details = {}

        spy_s = pd.to_numeric(close_df.get("SPY", pd.Series(dtype=float)), errors="coerce").dropna()
        spy_5d = float((spy_s.iloc[-1]/spy_s.iloc[-6] - 1)*100) if len(spy_s) >= 6 else 0

        for ldr in leaders[:4]:
            if ldr == "SPY" or ldr not in close_df.columns:
                continue
            s = pd.to_numeric(close_df[ldr], errors="coerce").dropna()
            if len(s) < 10:
                continue
            ret_5d = float((s.iloc[-1]/s.iloc[-6] - 1)*100) if len(s) >= 6 else 0
            ma20 = float(s.rolling(20, min_periods=10).mean().iloc[-1])
            below_ma20 = float(s.iloc[-1]) < ma20
            rel_strength = ret_5d - spy_5d
            is_weak = below_ma20 or rel_strength < -3
            leader_alerts.append(is_weak)
            leader_details[ldr] = {
                "5일 수익률": f"{ret_5d:+.1f}%",
                "SPY 대비": f"{rel_strength:+.1f}%p",
                "MA20": "아래 ⚠️" if below_ma20 else "위 ✅",
            }

        weak_count = sum(leader_alerts)
        leader_alert = weak_count >= 2
        signals["leaders"] = {"ok": not leader_alert, "value": f"{weak_count}/{len(leader_alerts)}개 약세", "trend": ""}
        details["대장주 모멘텀"] = leader_details
        if leader_alert:
            warnings.append(f"⚠️ 대장주 {weak_count}개 동시 약세 — 섹터 전반 하락 가능성")
    except Exception:
        signals["leaders"] = {"ok": True, "value": "N/A", "trend": ""}

    # ── 신호 4: 프리마켓 방향 (SPY/QQQ 1일 변화율) — prefetch 결과 사용 ─
    # FMP는 ES=F/NQ=F 미지원 → stock-price-change + quote API로 SPY/QQQ 사용
    try:
        _fut_results = _prefetch.get("futures", {})
        if _fut_results:
            _fut_chgs = [v["chg_pct"] for v in _fut_results.values()
                         if pd.notna(v.get("chg_pct", np.nan))]
            _avg_chg = float(np.mean(_fut_chgs)) if _fut_chgs else np.nan
            if pd.notna(_avg_chg):
                fut_alert = _avg_chg <= -0.5
                if _avg_chg >= 0.3:
                    fut_label = f"📈 +{_avg_chg:.2f}%"
                elif _avg_chg <= -0.3:
                    fut_label = f"📉 {_avg_chg:.2f}%"
                else:
                    fut_label = f"➡️ {_avg_chg:+.2f}%"
                spy_chg = _fut_results.get("SPY", {}).get("chg_pct", np.nan)
                qqq_chg = _fut_results.get("QQQ", {}).get("chg_pct", np.nan)
                spy_str = f"SPY {spy_chg:+.2f}%" if pd.notna(spy_chg) else "N/A"
                qqq_str = f"QQQ {qqq_chg:+.2f}%" if pd.notna(qqq_chg) else "N/A"
                signals["futures"] = {
                    "ok": not fut_alert,
                    "value": fut_label,
                    "trend": f"{spy_str} / {qqq_str}",
                }
                details["프리마켓 방향"] = {
                    "SPY (S&P500)": spy_str,
                    "QQQ (나스닥100)": qqq_str,
                    "평균 변화율": f"{_avg_chg:+.2f}%",
                    "판정": "⚠️ 갭다운 우려" if fut_alert else "✅ 정상",
                    "데이터 소스": "FMP stock-price-change / quote",
                }
                if fut_alert:
                    warnings.append(
                        f"⚠️ 프리마켓 하락 ({spy_str} / {qqq_str}) — 갭다운 개장 가능성"
                    )
            else:
                signals["futures"] = {"ok": True, "value": "N/A", "trend": ""}
        else:
            signals["futures"] = {"ok": True, "value": "N/A", "trend": ""}
    except Exception:
        signals["futures"] = {"ok": True, "value": "N/A", "trend": ""}

    # ── 신호 5: 달러·금·국채 리스크오프 신호 — prefetch 결과 사용 ────────
    try:
        _safety_results = _prefetch.get("riskoff", {})
        if _safety_results:
            _rising = [sym for sym, v in _safety_results.items()
                       if pd.notna(v["chg_pct"]) and v["chg_pct"] > 0.3]
            _risk_off = len(_rising) >= 2
            # SPY도 하락 중이면 리스크오프 확증
            _spy_also_down = False
            if "leaders" in signals and len(leaders) > 0:
                try:
                    _spy_row = close_dict.get("SPY") if "close_dict" in dir() else None
                    if _spy_row is not None and len(_spy_row) >= 6:
                        _spy_5d = float((_spy_row.iloc[-1] / _spy_row.iloc[-6] - 1) * 100)
                        _spy_also_down = _spy_5d < -0.5
                except Exception:
                    pass
            riskoff_alert = _risk_off and _spy_also_down

            _parts = [f"{v['name']} {v['chg_pct']:+.1f}%" for v in _safety_results.values()
                      if pd.notna(v.get("chg_pct"))]
            val5 = " / ".join(_parts) if _parts else "N/A"
            signals["riskoff"] = {"ok": not riskoff_alert, "value": val5, "trend": ""}
            details["달러·금·국채"] = {
                k: f"{v['chg_pct']:+.1f}% (5일)" if pd.notna(v.get("chg_pct")) else "N/A"
                for k, v in _safety_results.items()
            }
            details["달러·금·국채"]["판정"] = "⚠️ 리스크오프 확증" if riskoff_alert else (
                "🟡 안전자산 강세 (SPY 확인 필요)" if _risk_off else "✅ 정상"
            )
            if riskoff_alert:
                warnings.append(f"⚠️ 달러·금·국채 동반 강세 + SPY 약세 — 리스크오프 본격화 신호")
            elif _risk_off:
                warnings.append(f"🟡 안전자산 {len(_rising)}개 동반 강세 — 리스크오프 경향 모니터링")
        else:
            signals["riskoff"] = {"ok": True, "value": "N/A (FMP키 없음)", "trend": ""}
    except Exception:
        signals["riskoff"] = {"ok": True, "value": "N/A", "trend": ""}

    # ── 신호 6: 이벤트 리스크 — prefetch 결과 사용 ──────────────────────
    cal_events = []
    try:
        cal_events, has_high_impact = _prefetch.get("calendar", ([], False))
        if cal_events:
            # 리스크 신호는 '아직 발표 전(오늘 포함)' 이벤트만 사용 (지난 발표는 리스크 아님)
            upcoming = [e for e in cal_events if not e.get("is_past")]
            high_events = [e for e in upcoming if e["impact"].lower() in ("high", "3")]
            med_events  = [e for e in upcoming if e["impact"].lower() in ("medium", "2")]
            event_alert = has_high_impact
            if high_events:
                ev_names = ", ".join(e["event"] for e in high_events[:3])
                ev_dates = high_events[0]["date"]
                signals["event_risk"] = {
                    "ok":    False,
                    "value": f"고임팩트 {len(high_events)}건",
                    "trend": ev_names[:60],
                }
                details["이벤트 리스크"] = {
                    "고임팩트": f"{len(high_events)}건 ({ev_dates}~)",
                    "주요 이벤트": ev_names[:80],
                    "판정": "⚠️ 변동성 확대 주의",
                }
                warnings.append(
                    f"⚠️ 고임팩트 경제 이벤트 {len(high_events)}건 예정 "
                    f"({ev_dates}): {ev_names[:60]}"
                )
            elif med_events:
                signals["event_risk"] = {
                    "ok":    True,
                    "value": f"중간 임팩트 {len(med_events)}건",
                    "trend": "",
                }
                details["이벤트 리스크"] = {
                    "중간임팩트": f"{len(med_events)}건",
                    "판정": "✅ 고임팩트 없음",
                }
            else:
                signals["event_risk"] = {"ok": True, "value": "고임팩트 이벤트 없음", "trend": ""}
                details["이벤트 리스크"] = {"판정": "✅ 3일 이내 고임팩트 없음"}
        else:
            signals["event_risk"] = {"ok": True, "value": "캘린더 조회 불가", "trend": ""}
    except Exception:
        signals["event_risk"] = {"ok": True, "value": "N/A", "trend": ""}

    # ── 뉴스 — prefetch 결과 사용 ────────────────────────────────────────
    news_items = _prefetch.get("news", [])

    # ── 종합 점수 계산 ─────────────────────────────────────────────────────
    ok_count = sum(1 for v in signals.values() if v["ok"])
    total = len(signals)
    risk_score = int((1 - ok_count / total) * 10) if total > 0 else 0

    if risk_score >= 7:
        risk_level = "🔴 HIGH RISK"
        risk_color = "#dc2626"
        risk_msg = "복수의 선행 지표에서 경고 신호. 신규 매수 자제, 포지션 축소 권장."
    elif risk_score >= 4:
        risk_level = "🟡 CAUTION"
        risk_color = "#f59e0b"
        risk_msg = "일부 경고 신호 감지. 종목 선별 신중히, 손절 라인 점검 권장."
    elif risk_score >= 2:
        risk_level = "🟢 MODERATE"
        risk_color = "#16a34a"
        risk_msg = "소수 경고 신호. 대체로 안전하나 모니터링 유지."
    else:
        risk_level = "🟢 LOW RISK"
        risk_color = "#16a34a"
        risk_msg = "선행 지표 이상 없음. 정상적인 투자 환경."

    return {
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_color": risk_color,
        "risk_msg": risk_msg,
        "signals": signals,
        "warnings": warnings,
        "details": details,
        "news_items": news_items,
        "sector_filter": sector_filter,
        "event_calendar": cal_events,
    }


@st.cache_data(ttl=3600, show_spinner=False)
def find_etfs_holding_stock(stock_ticker: str) -> list[dict]:
    """
    개별 주식이 어떤 ETF의 보유 종목에 포함되어 있는지 찾기.
    컬럼명에 의존하지 않고 row의 숫자값을 직접 순회해서 weight 추출.
    """
    if not stock_ticker:
        return []

    target = str(stock_ticker).strip().upper()
    results = []

    check_list = [
        "QQQ", "SPY", "VOO", "VGT", "XLK",
        "SOXX", "SMH", "XSD", "ARKK", "ARKW",
        "IWM", "MGK", "RSP", "XLF", "XLV",
        "XLE", "XLY", "XLI", "CIBR", "BOTZ",
    ]

    def _extract_weight_from_row(row) -> float:
        """row에서 0~100 범위의 숫자를 weight로 추출."""
        for val in row:
            num = pd.to_numeric(val, errors="coerce")
            if pd.isna(num):
                continue
            # 0~1 범위면 % 비중 (0.05 → 5%)
            if 0 < num <= 1:
                return round(float(num) * 100, 3)
            # 0~100 범위면 이미 %
            if 1 < num <= 100:
                return round(float(num), 3)
        return np.nan

    k = _fmp_key()
    for etf_tk in check_list:
        try:
            holdings_df = None
            # FMP ETF holdings
            if k:
                try:
                    r = requests.get(
                        f"{_FMP_BASE}/etf-holder/{etf_tk}?apikey={k}",
                        timeout=_FMP_TIMEOUT
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list) and data:
                            holdings_df = pd.DataFrame(data)
                except Exception:
                    pass

            if holdings_df is None or holdings_df.empty:
                continue

            found = False
            rank = -1
            weight = np.nan

            # Case A: index가 ticker (가장 흔한 패턴)
            idx_list = [str(i).strip().upper() for i in holdings_df.index]
            if target in idx_list:
                found = True
                rank = idx_list.index(target) + 1
                row = holdings_df.iloc[rank - 1]
                weight = _extract_weight_from_row(row.values)

            # Case B: 컬럼 중 하나가 ticker
            if not found:
                for col in holdings_df.columns:
                    col_vals = holdings_df[col].astype(str).str.strip().str.upper().tolist()
                    if target in col_vals:
                        found = True
                        rank = col_vals.index(target) + 1
                        row = holdings_df.iloc[rank - 1]
                        # ticker 컬럼 제외한 나머지 값에서 weight 추출
                        other_vals = [v for c, v in zip(holdings_df.columns, row.values) if c != col]
                        weight = _extract_weight_from_row(other_vals)
                        break

            if found:
                results.append({
                    "etf": etf_tk,
                    "weight": weight if pd.notna(weight) else None,
                    "rank": rank,
                })
        except Exception:
            continue

    results.sort(key=lambda x: (x.get("weight") or 0), reverse=True)
    return results

def fetch_short_interest(ticker_upper: str) -> dict:
    """공매도 비율 — FMP stable/shares-float + quote 사용."""
    k = _fmp_key()
    empty = {"short_pct": None, "days_to_cover": None, "shares_short": None, "squeeze_risk": "N/A"}
    if not k:
        return empty

    float_shares = None
    outstanding_shares = None
    free_float_pct = None

    # shares-float 엔드포인트 (이미지 17에서 확인)
    try:
        r = requests.get(
            f"{_FMP_BASE}/shares-float?symbol={ticker_upper}&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r.status_code == 200:
            data = r.json()
            item = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
            if item:
                free_float_pct = to_float(item.get("freeFloat"))  # % 형태 (0~100)
                float_shares = to_float(item.get("floatShares"))
                outstanding_shares = to_float(item.get("outstandingShares"))
    except Exception:
        pass

    # freeFloat는 유통 가능 비율 (Short %와 다름)
    # Short Interest는 별도 데이터 소스 필요 — FMP Starter에서 직접 제공 안 됨
    # quote에서 shortRatio 시도
    short_pct = None
    days_to_cover = None
    try:
        r2 = requests.get(
            f"{_FMP_BASE}/quote?symbol={ticker_upper}&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r2.status_code == 200:
            data2 = r2.json()
            item2 = data2[0] if isinstance(data2, list) and data2 else {}
            if item2:
                # quote에 shortRatio, sharesShort 있을 수 있음
                sr = to_float(item2.get("shortRatio") or item2.get("shortPercent"))
                if pd.notna(sr):
                    days_to_cover = sr if sr > 1 else None
                ss = to_float(item2.get("sharesShort") or item2.get("shortInterest"))
                if pd.notna(ss) and float_shares and float_shares > 0:
                    short_pct = round(float(ss / float_shares * 100), 2)
                elif pd.notna(ss) and outstanding_shares and outstanding_shares > 0:
                    short_pct = round(float(ss / outstanding_shares * 100), 2)
    except Exception:
        pass

    # freeFloat를 보조 정보로 활용 (유통주식 비율)
    squeeze_risk = "N/A"
    if short_pct is not None and pd.notna(short_pct):
        v = float(short_pct)
        squeeze_risk = "🔥 높음 (Short Squeeze 주의)" if v >= 20 else ("⚠️ 중간" if v >= 10 else "✅ 낮음")

    return {
        "short_pct": round(float(short_pct), 2) if short_pct is not None and pd.notna(short_pct) else None,
        "days_to_cover": round(float(days_to_cover), 1) if days_to_cover is not None and pd.notna(days_to_cover) else None,
        "shares_short": None,
        "squeeze_risk": squeeze_risk,
        "free_float_pct": round(float(free_float_pct), 2) if free_float_pct is not None and pd.notna(free_float_pct) else None,
        "float_shares": int(float_shares) if float_shares is not None and pd.notna(float_shares) else None,
    }


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_options_flow_summary(ticker_upper: str) -> dict:
    """Options Flow — 현재 무료 대안 없음. 추후 전용 API 연동 예정."""
    return {}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_insider_trading(ticker_upper: str) -> pd.DataFrame:
    """인사이더 트레이딩 — FMP stable/insider-trading/search + statistics 사용."""
    k = _fmp_key()
    if not k:
        return pd.DataFrame()

    rows = []
    # 정확한 엔드포인트: /stable/insider-trading/search?symbol=TSLA
    try:
        r = requests.get(
            f"{_FMP_BASE}/insider-trading/search?symbol={ticker_upper}&limit=15&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                for item in data[:15]:
                    name = str(item.get("reportingName") or item.get("name") or "")
                    title = str(item.get("typeOfOwner") or item.get("officerTitle") or "")
                    tx_type = str(item.get("transactionType") or item.get("acquistionOrDisposition") or "")
                    shares = to_float(item.get("securitiesTransacted") or item.get("shares"))
                    price = to_float(item.get("price") or item.get("transactionPrice"))
                    date_str = str(item.get("transactionDate") or item.get("filingDate") or "")[:10]
                    if not name or not date_str:
                        continue
                    is_buy = any(w in tx_type.upper() for w in ["P-", "PURCHASE", "BUY", "A-AWARD", "A-"])
                    is_sell = any(w in tx_type.upper() for w in ["S-", "SALE", "SELL", "D-", "DISPO"])
                    direction = "🟢 매수" if is_buy else ("🔴 매도" if is_sell else tx_type[:12])
                    value = shares * price if pd.notna(shares) and pd.notna(price) and price > 0 else None
                    rows.append({
                        "날짜": date_str,
                        "이름": name[:25],
                        "직책": title[:20],
                        "거래유형": direction,
                        "주식수": f"{int(shares):,}" if pd.notna(shares) else "N/A",
                        "거래가": f"${price:.2f}" if pd.notna(price) and price > 0 else "N/A",
                        "거래금액": f"${value/1e6:.2f}M" if value and value >= 1e6 else (f"${value:,.0f}" if value else "N/A"),
                    })
    except Exception:
        pass

    return pd.DataFrame(rows) if rows else pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_insider_statistics(ticker_upper: str) -> dict:
    """인사이더 트레이딩 통계 요약 — FMP stable/insider-trading/statistics 사용."""
    k = _fmp_key()
    if not k:
        return {}
    try:
        r = requests.get(
            f"{_FMP_BASE}/insider-trading/statistics?symbol={ticker_upper}&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r.status_code == 200:
            data = r.json()
            item = data[0] if isinstance(data, list) and data else {}
            if item:
                return {
                    "acquired": to_float(item.get("acquiredTransactions") or item.get("totalAcquired")),
                    "disposed": to_float(item.get("disposedTransactions") or item.get("totalDisposed")),
                    "total_purchases": to_float(item.get("totalPurchases")),
                    "total_sales": to_float(item.get("totalSales")),
                    "ratio": to_float(item.get("acquiredDisposedRatio")),
                }
    except Exception:
        pass
    return {}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_analyst_price_targets(ticker_upper: str) -> dict:
    """애널리스트 목표주가 — FMP stable/price-target-consensus + price-target 사용."""
    k = _fmp_key()
    if not k:
        return {}
    result = {}
    # 컨센서스 (평균/중간/고/저)
    try:
        r = requests.get(
            f"{_FMP_BASE}/price-target-consensus?symbol={ticker_upper}&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r.status_code == 200:
            data = r.json()
            item = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
            if item:
                result["target_high"] = to_float(item.get("targetHigh") or item.get("priceTargetHigh"))
                result["target_low"] = to_float(item.get("targetLow") or item.get("priceTargetLow"))
                result["target_mean"] = to_float(item.get("targetMean") or item.get("priceTargetMean") or item.get("targetConsensus"))
                result["target_median"] = to_float(item.get("targetMedian") or item.get("priceTargetMedian"))
    except Exception:
        pass
    # 최근 개별 애널리스트 추천
    try:
        r2 = requests.get(
            f"{_FMP_BASE}/price-target?symbol={ticker_upper}&limit=8&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r2.status_code == 200:
            data2 = r2.json()
            if isinstance(data2, list) and data2:
                recent = []
                for item in data2[:8]:
                    analyst = str(item.get("analystCompany") or item.get("publishedDate") or "")[:25]
                    target = to_float(item.get("adjPriceTarget") or item.get("priceTarget") or item.get("price"))
                    rating = str(item.get("priceGrade") or item.get("newGrade") or item.get("rating") or "")
                    date_s = str(item.get("publishedDate") or item.get("date") or "")[:10]
                    if not analyst:
                        continue
                    recent.append({
                        "날짜": date_s,
                        "기관": analyst,
                        "목표주가": f"${target:.2f}" if pd.notna(target) else "N/A",
                        "등급": rating or "N/A",
                    })
                result["recent"] = recent
    except Exception:
        pass
    return result


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_senate_house_trading(ticker_upper: str) -> pd.DataFrame:
    """상원/하원 의원 거래 — FMP stable/senate-trades + house-trades 사용."""
    k = _fmp_key()
    if not k:
        return pd.DataFrame()
    rows = []
    sources = [
        ("senate-trades", "상원"),
        ("house-trades", "하원"),
    ]
    for endpoint, source in sources:
        try:
            r = requests.get(
                f"{_FMP_BASE}/{endpoint}?symbol={ticker_upper}&apikey={k}",
                timeout=_FMP_TIMEOUT
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    for item in data[:8]:
                        first = str(item.get("firstName") or "")
                        last = str(item.get("lastName") or "")
                        office = str(item.get("office") or "")
                        name = office if office else f"{first} {last}".strip()
                        tx_type = str(item.get("type") or "")
                        amount = str(item.get("amount") or "")
                        asset = str(item.get("assetDescription") or ticker_upper)
                        date_s = str(item.get("transactionDate") or item.get("disclosureDate") or "")[:10]
                        if not name:
                            continue
                        is_buy = "purchase" in tx_type.lower() or "buy" in tx_type.lower()
                        is_sell = "sale" in tx_type.lower() or "sell" in tx_type.lower()
                        direction = "🟢 매수" if is_buy else ("🔴 매도" if is_sell else tx_type[:15])
                        rows.append({
                            "구분": source,
                            "의원명": name[:22],
                            "거래유형": direction,
                            "금액범위": amount[:20],
                            "종목": asset[:20],
                            "거래일": date_s,
                        })
        except Exception:
            continue
    return pd.DataFrame(rows) if rows else pd.DataFrame()

def calculate_narrative_consistency_score(user_id: str, lookback_days: int = 14) -> dict:
    """
    최근 N일간 내러티브에서 테마/종목이 얼마나 일관되게 등장했는지 점수화.

    [점수 계산 방식 — v2]
    - 테마 제목은 AI마다 표현이 달라 문자열 완전 일치로는 집계가 불가능.
    - 대신 Winners/Emerging 티커(정확히 일치)의 반복 등장 비율로 점수를 산정.
    - 점수 = 상위 5개 티커의 평균 등장 비율 × 100
      (예: 17회 분석 중 NVDA가 14회 등장 → 14/17 = 82점 기여)
    - 테마는 키워드 정규화(AI·반도체·클라우드 등)로 묶어서 표시.

    반환: {"top_themes": [(theme, count)], "top_tickers": [(ticker, count)],
           "consistency_score": 0~100}
    """
    # 테마 키워드 정규화 맵: 포함 키워드 → 대표 테마명
    _THEME_KEYWORD_MAP = [
        (["AI", "인공지능", "Artificial"],          "🤖 AI / 인공지능"),
        (["반도체", "Semiconductor", "Chip", "칩"],  "💾 반도체"),
        (["클라우드", "Cloud", "데이터센터"],         "☁️ 클라우드 / 데이터센터"),
        (["에너지", "Energy", "원자력", "Nuclear"],   "⚡ 에너지"),
        (["방산", "Defense", "항공우주", "Aerospace"],"🛡️ 방산 / 항공우주"),
        (["바이오", "Bio", "헬스", "Health", "제약"], "💊 바이오 / 헬스케어"),
        (["금융", "Finance", "Bank", "은행"],         "🏦 금융"),
        (["소비재", "Consumer", "리테일", "Retail"],  "🛒 소비재 / 리테일"),
        (["인프라", "Infra", "산업재", "Industrial"], "🏗️ 인프라 / 산업재"),
        (["지정학", "Geo", "무역", "Trade", "관세"],  "🌍 지정학 / 무역"),
    ]

    def _normalize_theme(title: str) -> str:
        t_upper = str(title).upper()
        for keywords, label in _THEME_KEYWORD_MAP:
            if any(kw.upper() in t_upper for kw in keywords):
                return label
        return str(title)[:30]  # 매칭 안 되면 원본 앞 30자

    try:
        records, _ = fetch_narrative_records_from_sheet()
        if not records:
            return {}
        uid_u = str(user_id).strip().upper()
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        user_recs = [
            r for r in records
            if str(r.get("_sheet_user_id", "")).strip().upper() == uid_u
            and (_narrative_parse_saved_at_utc(r.get("saved_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
        ]
        if not user_recs:
            return {}

        from collections import Counter
        theme_counter = Counter()
        ticker_counter = Counter()

        for rec in user_recs:
            analysis = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}

            # 테마: 키워드 정규화 후 카운트
            themes = analysis.get("themes", [])
            for t in themes:
                if isinstance(t, dict) and t.get("title"):
                    normalized = _normalize_theme(str(t["title"]))
                    theme_counter[normalized] += 1

            # 티커: Winners + Emerging 모두 카운트 (정확히 일치하므로 신뢰도 높음)
            winners_csv = str(rec.get("_sheet_winners_csv") or "").strip()
            emerging_csv = str(rec.get("_sheet_emerging_csv") or "").strip()
            for tk in filter_scanner_ticker_list(
                [x.strip().upper() for x in (winners_csv + "," + emerging_csv).split(",") if x.strip()]
            ):
                ticker_counter[tk] += 1

        total_recs = len(user_recs)
        top_themes  = theme_counter.most_common(5)
        top_tickers = ticker_counter.most_common(10)

        # ── 점수 계산: 티커 반복도 기반 ──────────────────────────────────
        # 상위 5개 티커의 평균 등장 비율 → 100점 만점
        # 예) 17회 분석에서 상위 5 티커가 [14,13,12,11,10]회 등장
        #     → avg = (14+13+12+11+10)/(5×17) = 60/85 ≈ 0.706 → 70점
        if top_tickers and total_recs > 0:
            top5_counts = [c for _, c in top_tickers[:5]]
            top5_avg = sum(top5_counts) / (len(top5_counts) * total_recs)
            consistency_score = min(100, int(top5_avg * 100))
        else:
            consistency_score = 0

        return {
            "top_themes":        top_themes,
            "top_tickers":       top_tickers,
            "total_records":     total_recs,
            "consistency_score": consistency_score,
            "lookback_days":     lookback_days,
        }
    except Exception:
        return {}



_SECTOR_KR = {
    "Technology": "기술", "Financial Services": "금융 서비스",
    "Healthcare": "헬스케어", "Consumer Cyclical": "경기소비재",
    "Consumer Defensive": "필수소비재", "Industrials": "산업재",
    "Basic Materials": "소재", "Energy": "에너지",
    "Real Estate": "부동산", "Utilities": "유틸리티",
    "Communication Services": "통신 서비스",
    "ETF": "ETF", "Mutual Fund": "펀드",
}

_INDUSTRY_KR = {
    "Semiconductors": "반도체", "Software—Application": "소프트웨어(응용)",
    "Software—Infrastructure": "소프트웨어(인프라)", "Consumer Electronics": "소비자 전자",
    "Internet Retail": "인터넷 소매", "Internet Content & Information": "인터넷 콘텐츠",
    "Auto Manufacturers": "자동차 제조", "Drug Manufacturers—General": "제약(대형)",
    "Biotechnology": "바이오테크", "Banks—Diversified": "은행(다각화)",
    "Oil & Gas E&P": "석유·가스 탐사", "Oil & Gas Integrated": "석유·가스 통합",
    "Aerospace & Defense": "항공우주·방산", "Capital Markets": "자본시장",
    "Asset Management": "자산운용", "Insurance—Diversified": "보험",
    "Specialty Retail": "전문 소매", "Restaurants": "외식업",
    "REITs": "리츠(부동산)", "Utilities—Regulated Electric": "전력(규제)",
    "Telecom Services": "통신 서비스",
}


def translate_ko(text: str, mapping: dict) -> str:
    """영문 섹터/산업명을 한글로 변환. 없으면 원문 반환."""
    return mapping.get(str(text).strip(), str(text).strip())


# ── FMP fallback helpers ──────────────────────────────────────────────────
_FMP_BASE    = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 7

def _fmp_key() -> str:
    try:
        return str(st.secrets.get("FMP_API_KEY", "") or "").strip()
    except Exception:
        return ""

@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_profile(ticker: str) -> dict:
    k = _fmp_key()
    if not k: return {}
    try:
        r = requests.get(f"{_FMP_BASE}/profile?symbol={ticker}&apikey={k}", timeout=_FMP_TIMEOUT)
        d = r.json()
        return d[0] if isinstance(d, list) and d else {}
    except Exception:
        return {}

@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_ratios(ticker: str) -> dict:
    """ratios-ttm — stable API only (v3 legacy 차단됨)"""
    k = _fmp_key()
    if not k: return {}
    try:
        r = requests.get(f"{_FMP_BASE}/ratios-ttm?symbol={ticker}&apikey={k}", timeout=_FMP_TIMEOUT)
        if r.status_code == 200:
            d = r.json()
            return d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    except Exception:
        pass
    return {}

@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_key_metrics(ticker: str) -> dict:
    """key-metrics-ttm — stable API only (v3 legacy 차단됨)"""
    k = _fmp_key()
    if not k: return {}
    try:
        r = requests.get(f"{_FMP_BASE}/key-metrics-ttm?symbol={ticker}&apikey={k}", timeout=_FMP_TIMEOUT)
        if r.status_code == 200:
            d = r.json()
            return d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    except Exception:
        pass
    return {}

@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_income(ticker: str) -> dict:
    """income-statement annual: revenue, operatingIncome, netIncome, epsdiluted"""
    k = _fmp_key()
    if not k: return {}
    try:
        r = requests.get(f"{_FMP_BASE}/income-statement?symbol={ticker}&period=annual&limit=2&apikey={k}", timeout=_FMP_TIMEOUT)
        d = r.json()
        return {"latest": d[0] if d else {}, "prev": d[1] if len(d) > 1 else {}}
    except Exception:
        return {}

@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_cashflow(ticker: str) -> dict:
    """cash-flow-statement annual: freeCashFlow, operatingCashFlow, capitalExpenditure"""
    k = _fmp_key()
    if not k: return {}
    try:
        r = requests.get(f"{_FMP_BASE}/cash-flow-statement?symbol={ticker}&period=annual&limit=1&apikey={k}", timeout=_FMP_TIMEOUT)
        d = r.json()
        return d[0] if isinstance(d, list) and d else {}
    except Exception:
        return {}

@st.cache_data(ttl=1800, show_spinner=False)
def _fmp_price_history(ticker: str, limit: int = 252) -> pd.DataFrame:
    """FMP historical-price-eod → Close/Open/High/Low/Volume DataFrame 반환."""
    k = _fmp_key()
    if not k:
        return pd.DataFrame()
    try:
        r = requests.get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker}&limit={limit}&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "date" not in df.columns:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        rename_map = {"close": "Close", "open": "Open", "high": "High",
                      "low": "Low", "volume": "Volume", "adjClose": "Adj Close"}
        df = df.rename(columns=rename_map)
        for col in ["Close", "Open", "High", "Low"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "Volume" in df.columns:
            df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def _fmp_batch_price_history(tickers: list, limit: int = 130) -> dict:
    """여러 티커를 ThreadPoolExecutor로 병렬 FMP 호출 → {ticker: DataFrame} 반환.
    순차 루프 대비 섹터 스캔(30~50종목) 기준 약 5~8배 속도 향상.
    max_workers=8: FMP Starter 레이트 리밋(300 req/min) 여유 있게 유지.
    """
    if not tickers:
        return {}

    import concurrent.futures

    _MAX_WORKERS = 8  # FMP 레이트 리밋 감안한 동시 요청 수

    def _fetch_one(tk):
        df = _fmp_price_history(tk, limit=limit)
        return tk, df

    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, tk): tk for tk in tickers}
        for future in concurrent.futures.as_completed(futures):
            try:
                tk, df = future.result()
                if not df.empty:
                    result[tk] = df
            except Exception:
                pass  # 개별 티커 실패가 전체 배치를 중단시키지 않음
    return result


def _fmp_batch_to_close_df(tickers: list, limit: int = 130) -> pd.DataFrame:
    """FMP 배치 조회 → Close 컬럼만 모은 DataFrame 반환."""
    batch = _fmp_batch_price_history(tickers, limit=limit)
    if not batch:
        return pd.DataFrame()
    close_dict = {}
    for tk, df in batch.items():
        if "Close" in df.columns:
            close_dict[tk] = df["Close"]
    if not close_dict:
        return pd.DataFrame()
    return pd.DataFrame(close_dict).sort_index()

def _fmp_fill_parallel_warmup(tickers: list, max_workers: int = 10) -> dict:
    """
    스캐너 루프 전에 _fmp_fill()을 종목별 병렬 실행해 캐시를 웜업한다.
    반환: {ticker: info_dict} — 루프 안에서 바로 조회해 HTTP 재호출 없이 사용.

    max_workers=10: FMP Starter 300 req/min 여유 감안.
    _fmp_fill() 내부 캐시 함수(_fmp_profile 등)는 @st.cache_data 공유이므로
    병렬 호출 시 첫 호출만 HTTP, 이후는 캐시 히트 → rate limit 안전.
    """
    import concurrent.futures as _cf

    if not tickers:
        return {}

    results = {}

    def _fill_one(tk):
        try:
            return tk, _fmp_fill({}, tk)
        except Exception:
            return tk, {}

    with _cf.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fill_one, tk): tk for tk in tickers}
        for future in _cf.as_completed(futures):
            try:
                tk, info = future.result()
                results[tk] = info
            except Exception:
                results[futures[future]] = {}

    return results




# ── _fmp_fill 내부 uncached requests.get 3개를 캐싱 래퍼로 분리 ──────────
# 기존: _fmp_fill() 호출마다 /quote, /analyst-estimates, /balance-sheet를
#        직접 requests.get → 스캐너 30~50 티커 루프에서 최대 150회 raw HTTP.
# 개선: 각각 @st.cache_data(ttl=3600) 래퍼로 감싸 첫 호출 이후 캐시 히트.

@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_quote(ticker: str) -> dict:
    """FMP /quote — MA50/MA200/52w고저/현재가/시총. _fmp_fill 전용 캐싱 래퍼."""
    k = _fmp_key()
    if not k:
        return {}
    try:
        r = requests.get(f"{_FMP_BASE}/quote?symbol={ticker}&apikey={k}", timeout=_FMP_TIMEOUT)
        if r.status_code == 200:
            d = r.json()
            return d[0] if isinstance(d, list) and d else {}
    except Exception:
        pass
    return {}


@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_analyst_estimates(ticker: str) -> list:
    """FMP /analyst-estimates (annual, limit=4) — Forward P/E 계산용. _fmp_fill 전용 캐싱 래퍼."""
    k = _fmp_key()
    if not k:
        return []
    try:
        r = requests.get(
            f"{_FMP_BASE}/analyst-estimates?symbol={ticker}&period=annual&limit=4&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_balance_sheet(ticker: str) -> dict:
    """FMP /balance-sheet-statement (limit=2) — ROE·D/E 직접 계산용. _fmp_fill 전용 캐싱 래퍼."""
    k = _fmp_key()
    if not k:
        return {}
    try:
        r = requests.get(
            f"{_FMP_BASE}/balance-sheet-statement?symbol={ticker}&limit=2&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            return d[0] if isinstance(d, list) and d else {}
    except Exception:
        pass
    return {}


# ── 신규 기능용 FMP 캐싱 래퍼 ────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_financial_scores(ticker: str) -> dict:
    """FMP /financial-scores — Altman Z-Score + Piotroski F-Score.
    반환 키: altmanZScore, piotroskiScore, workingCapital, retainedEarnings 등.
    """
    k = _fmp_key()
    if not k:
        return {}
    try:
        r = requests.get(
            f"{_FMP_BASE}/financial-scores?symbol={ticker}&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            return d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    except Exception:
        pass
    return {}


@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_sector_pe_snapshot() -> list:
    """FMP /sector-pe-snapshot — 전 섹터 현재 PER 스냅샷.
    반환: [{"sector": "Technology", "pe": 32.1, "date": "2024-02-01"}, ...]
    """
    k = _fmp_key()
    if not k:
        return []
    try:
        from datetime import date
        today = date.today().isoformat()
        r = requests.get(
            f"{_FMP_BASE}/sector-pe-snapshot?date={today}&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_earnings_transcript(ticker: str) -> dict:
    """FMP /earning-call-transcript — 직전 분기 어닝콜 원문.
    반환: {"symbol": ..., "quarter": ..., "year": ..., "content": ...}
    """
    k = _fmp_key()
    if not k:
        return {}
    try:
        # 날짜 목록 먼저 조회 → 가장 최근 분기/연도 파악
        rd = requests.get(
            f"{_FMP_BASE}/earning-call-transcript-dates?symbol={ticker}&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if rd.status_code != 200:
            return {}
        dates = rd.json()
        if not isinstance(dates, list) or not dates:
            return {}
        # 첫 번째 항목이 가장 최근 (FMP 응답 내림차순)
        latest = dates[0]
        year = latest.get("year") or latest[0] if isinstance(latest, list) else latest.get("year")
        quarter = latest.get("quarter") or latest[1] if isinstance(latest, list) else latest.get("quarter")
        if not year or not quarter:
            return {}
        rt = requests.get(
            f"{_FMP_BASE}/earning-call-transcript?symbol={ticker}&year={year}&quarter={quarter}&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if rt.status_code == 200:
            d = rt.json()
            item = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
            return item
    except Exception:
        pass
    return {}


@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_historical_ratings(ticker: str) -> list:
    """FMP /grades-historical — 애널리스트 등급 변경 히스토리 (최근 15건).
    반환: [{"date": ..., "company": ..., "analyst": ..., "previousGrade": ..., "newGrade": ..., "action": ...}, ...]
    """
    k = _fmp_key()
    if not k:
        return []
    try:
        r = requests.get(
            f"{_FMP_BASE}/grades-historical?symbol={ticker}&limit=15&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


@st.cache_data(ttl=3600, show_spinner=False)
def _fmp_company_screener(
    sector: str = "",
    market_cap_min: int = 1_000_000_000,
    volume_min: int = 500_000,
    limit: int = 50,
) -> list:
    """FMP /company-screener — 조건 기반 종목 자동 탐색.
    sector: 'Technology', 'Healthcare' 등 빈 문자열이면 전체.
    반환: [{"symbol": ..., "companyName": ..., "marketCap": ..., "sector": ..., "volume": ...}, ...]
    """
    k = _fmp_key()
    if not k:
        return []
    try:
        params = f"marketCapMoreThan={market_cap_min}&volumeMoreThan={volume_min}&limit={limit}&apikey={k}"
        if sector:
            params += f"&sector={sector.replace(' ', '%20')}"
        r = requests.get(
            f"{_FMP_BASE}/company-screener?{params}",
            timeout=_FMP_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


def _fmp_fill(info: dict, ticker: str) -> dict:
    """info dict의 빈 필드를 FMP로 채워 반환.
    ※ FMP Starter 플랜 실제 제공 필드만 사용.
    기존 값은 절대 덮어쓰지 않음.
    """
    info = dict(info)

    # ── profile: 회사명·섹터·설명·밸류에이션 기본값 ─────────────────
    # 밸류에이션 필드(P/E, EPS 등)가 없으면 항상 profile 조회
    _need_prof = not all([
        info.get("longName") or info.get("shortName"),
        info.get("sector"),
        info.get("longBusinessSummary"),
        info.get("trailingPE") or info.get("trailingEps"),  # 밸류에이션도 체크
    ])
    if _need_prof:
        p = _fmp_profile(ticker)
        if p:
            if not (info.get("longName") or info.get("shortName")):
                info["longName"] = str(p.get("companyName") or p.get("name") or ticker)
            if not info.get("sector"):
                info["sector"] = str(p.get("sector") or "")
            if not info.get("industry"):
                info["industry"] = str(p.get("industry") or "")
            if not info.get("longBusinessSummary"):
                info["longBusinessSummary"] = str(p.get("description") or "")
            if not info.get("website"):
                info["website"] = str(p.get("website") or "")
            if not info.get("country"):
                info["country"] = str(p.get("country") or "N/A")
            if not info.get("marketCap"):
                mkt = p.get("mktCap") or p.get("marketCap") or p.get("marketCapitalization")
                if mkt:
                    info["marketCap"] = to_float(mkt)
            if not info.get("fullTimeEmployees"):
                info["fullTimeEmployees"] = p.get("fullTimeEmployees") or p.get("employees")
            # 현재가
            if not info.get("currentPrice"):
                price = p.get("price") or p.get("lastPrice")
                if price:
                    info["currentPrice"] = to_float(price)
            # 52주 고저
            if not info.get("fiftyTwoWeekHigh"):
                v = to_float(p.get("range", "").split("-")[1] if "-" in str(p.get("range","")) else None)
                if pd.notna(v): info["fiftyTwoWeekHigh"] = v
            if not info.get("fiftyTwoWeekLow"):
                v = to_float(p.get("range", "").split("-")[0] if "-" in str(p.get("range","")) else None)
                if pd.notna(v): info["fiftyTwoWeekLow"] = v

    # ── quote: MA50/MA200/yearHigh/yearLow (기술적 분석용) ───────────
    _need_quote = not all([info.get("fiftyDayAverage"), info.get("twoHundredDayAverage")])
    if _need_quote:
        try:
            q_item = _fmp_quote(ticker)  # ← 캐싱 래퍼 사용 (기존: 직접 requests.get)
            if q_item:
                if not info.get("fiftyDayAverage"):
                    v = to_float(q_item.get("priceAvg50"))
                    if pd.notna(v): info["fiftyDayAverage"] = v
                if not info.get("twoHundredDayAverage"):
                    v = to_float(q_item.get("priceAvg200"))
                    if pd.notna(v): info["twoHundredDayAverage"] = v
                if not info.get("fiftyTwoWeekHigh"):
                    v = to_float(q_item.get("yearHigh"))
                    if pd.notna(v): info["fiftyTwoWeekHigh"] = v
                if not info.get("fiftyTwoWeekLow"):
                    v = to_float(q_item.get("yearLow"))
                    if pd.notna(v): info["fiftyTwoWeekLow"] = v
                if not info.get("currentPrice"):
                    v = to_float(q_item.get("price"))
                    if pd.notna(v): info["currentPrice"] = v
                if not info.get("marketCap"):
                    v = to_float(q_item.get("marketCap"))
                    if pd.notna(v): info["marketCap"] = v
        except Exception:
            pass

    # ── profile 보완 필드 (quote 블록과 독립) ──────────────────────────────
    try:
        _p = _fmp_profile(ticker)
        if _p:
            if not info.get("earningsDate") and _p.get("earningsAnnouncement"):
                info["earningsDate"] = [str(_p["earningsAnnouncement"])[:10]]
            if _p.get("isEtf") and not info.get("quoteType"):
                info["quoteType"] = "ETF"
            if not info.get("trailingPE"):
                v = to_float(_p.get("pe") or _p.get("peRatio"))
                if pd.notna(v) and v > 0: info["trailingPE"] = v
            if not info.get("trailingEps"):
                v = to_float(_p.get("eps") or _p.get("epsActual"))
                if pd.notna(v): info["trailingEps"] = v
            if not info.get("priceToBook"):
                v = to_float(_p.get("priceToBook") or _p.get("pbRatio"))
                if pd.notna(v) and v > 0: info["priceToBook"] = v
            if not info.get("beta"):
                v = to_float(_p.get("beta"))
                if pd.notna(v): info["beta"] = v
            if not info.get("dividendYield"):
                v = to_float(_p.get("lastDiv"))
                price_v = to_float(_p.get("price"))
                if pd.notna(v) and pd.notna(price_v) and price_v > 0:
                    info["dividendYield"] = v / price_v
            if not info.get("_fmp_dcf"):
                v = to_float(_p.get("dcf"))
                if pd.notna(v) and v > 0: info["_fmp_dcf"] = v
    except Exception:
        pass

    # ── analyst-estimates: Forward P/E 계산 ────────────────────────────────
    # 실제 API 응답: epsAvg 필드 사용, period=annual&limit=4로 가장 가까운 연도 우선
    if not info.get("forwardPE"):
        try:
            ae_data = _fmp_analyst_estimates(ticker)  # ← 캐싱 래퍼 사용 (기존: 직접 requests.get)
            if isinstance(ae_data, list) and ae_data:
                current_year = datetime.now(_MARKET_ET_TZ).year
                # 날짜 오름차순 정렬 → 가장 가까운 미래 연도 우선
                ae_sorted = sorted(
                    ae_data,
                    key=lambda x: str(x.get("date") or x.get("year") or "9999")
                )
                for ae in ae_sorted:
                    ae_year_str = str(ae.get("date") or ae.get("year") or "")[:4]
                    try:
                        ae_year = int(ae_year_str)
                    except Exception:
                        continue
                    if ae_year < current_year:
                        continue
                    # 실제 API 필드명: epsAvg (확인됨)
                    est_eps = to_float(
                        ae.get("epsAvg") or ae.get("estimatedEpsAvg") or
                        ae.get("estimatedEps") or ae.get("eps")
                    )
                    cur_price = to_float(
                        info.get("currentPrice") or info.get("price") or
                        info.get("regularMarketPrice") or info.get("_fmp_price")
                    )
                    # currentPrice 없으면 캐싱된 profile에서 가져오기 (기존: 별도 requests.get)
                    if (cur_price is None or pd.isna(cur_price)):
                        _p_cur = _fmp_profile(ticker)
                        cur_price = to_float(_p_cur.get("price")) if _p_cur else np.nan
                        if pd.notna(cur_price):
                            info["currentPrice"] = cur_price
                    if pd.notna(est_eps) and est_eps > 0 and pd.notna(cur_price) and cur_price > 0:
                        fwd_pe = round(float(cur_price) / float(est_eps), 2)
                        if 0 < fwd_pe < 2000:
                            info["forwardPE"] = fwd_pe
                            break
        except Exception:
            pass

    # ── key-metrics-ttm: EV/Sales, EV/FCF, EV/EBITDA, ROE (실제 확인 필드) ──
    if not info.get("_fmp_ev_to_sales") or not info.get("_fmp_ev_to_fcf") or not info.get("enterpriseToEbitda"):
        km = _fmp_key_metrics(ticker)
        if km:
            if not info.get("_fmp_ev_to_sales"):
                v = to_float(km.get("evToSalesTTM"))
                if pd.notna(v) and v > 0: info["_fmp_ev_to_sales"] = v
            if not info.get("_fmp_ev_to_fcf"):
                v = to_float(km.get("evToFreeCashFlowTTM"))
                if pd.notna(v) and v > 0: info["_fmp_ev_to_fcf"] = v
            # EV/EBITDA — 실제 필드명은 대문자 EBITDA
            if not info.get("enterpriseToEbitda"):
                v = to_float(km.get("evToEBITDATTM") or km.get("evToEbitdaTTM"))
                if pd.notna(v) and v > 0: info["enterpriseToEbitda"] = v
            if not info.get("marketCap"):
                v = to_float(km.get("marketCapTTM"))
                if pd.notna(v) and v > 0: info["marketCap"] = v
            if not info.get("returnOnEquity"):
                v = to_float(km.get("returnOnEquityTTM"))
                if pd.notna(v): info["returnOnEquity"] = v

    # ── ratios-ttm: 실제 API 응답에서 확인된 정확한 필드명 ────────────
    # operatingMargins만 채워져도 P/E 등이 없으면 다시 조회
    _need_ratios = not all([
        info.get("trailingPE"),
        info.get("priceToBook"),
        info.get("debtToEquity"),
        info.get("operatingMargins"),
        info.get("enterpriseToEbitda"),
    ])
    if _need_ratios:
        rat = _fmp_ratios(ticker)
        if rat:
            if not info.get("trailingPE"):
                v = to_float(rat.get("priceToEarningsRatioTTM"))
                if pd.notna(v) and v > 0: info["trailingPE"] = v
            # Forward P/E는 ratios-ttm에 전용 필드 없음 → profile에서 가져옴 (아래 profile 섹션에서 처리)
            if not info.get("priceToBook"):
                v = to_float(rat.get("priceToBookRatioTTM"))
                if pd.notna(v) and v > 0: info["priceToBook"] = v
            if not info.get("enterpriseToEbitda"):
                v = to_float(rat.get("enterpriseValueMultipleTTM"))
                if pd.notna(v) and v > 0: info["enterpriseToEbitda"] = v
            if not info.get("pegRatio"):
                v = to_float(rat.get("priceToEarningsGrowthRatioTTM"))
                if pd.notna(v): info["pegRatio"] = v
            if not info.get("debtToEquity"):
                v = to_float(rat.get("debtToEquityRatioTTM"))
                if pd.notna(v): info["debtToEquity"] = v
            if not info.get("operatingMargins"):
                v = to_float(rat.get("operatingProfitMarginTTM"))
                if pd.notna(v): info["operatingMargins"] = v
            if not info.get("grossMargins"):
                v = to_float(rat.get("grossProfitMarginTTM"))
                if pd.notna(v): info["grossMargins"] = v
            if not info.get("netMargins"):
                v = to_float(rat.get("netProfitMarginTTM"))
                if pd.notna(v): info["netMargins"] = v
            if not info.get("_fmp_ev_to_sales"):
                v = to_float(rat.get("priceToSalesRatioTTM"))
                if pd.notna(v) and v > 0: info["_fmp_ev_to_sales"] = v
            if not info.get("returnOnEquity"):
                v = to_float(rat.get("returnOnEquityTTM"))
                if pd.notna(v): info["returnOnEquity"] = v
            if not info.get("trailingEps"):
                v = to_float(rat.get("netIncomePerEBTTTM"))
                if pd.notna(v): info["trailingEps"] = v

    # ── income-statement + balance-sheet: ROE·D/E 직접 계산 ─────────
    _need_calc = not all([info.get("returnOnEquity"), info.get("debtToEquity")])
    if _need_calc:
        inc = _fmp_income(ticker)
        latest = inc.get("latest", {})
        prev   = inc.get("prev", {})
        if latest:
            if not info.get("operatingMargins"):
                _rev = to_float(latest.get("revenue"))
                _oi  = to_float(latest.get("operatingIncome"))
                if pd.notna(_rev) and _rev != 0 and pd.notna(_oi):
                    info["operatingMargins"] = float(_oi / _rev)
            if not info.get("trailingEps"):
                _eps = to_float(latest.get("epsdiluted") or latest.get("eps"))
                if pd.notna(_eps): info["trailingEps"] = _eps
            if not info.get("earningsGrowth") and prev:
                _rev_now  = to_float(latest.get("revenue"))
                _rev_prev = to_float(prev.get("revenue"))
                if pd.notna(_rev_now) and pd.notna(_rev_prev) and _rev_prev != 0:
                    info["earningsGrowth"] = float((_rev_now - _rev_prev) / abs(_rev_prev))
            _ni = to_float(latest.get("netIncome"))
            if pd.notna(_ni): info["_fmp_net_income"] = _ni

        # balance-sheet에서 직접 ROE, D/E 계산
        try:
            if not info.get("returnOnEquity") or not info.get("debtToEquity"):
                bs = _fmp_balance_sheet(ticker)  # ← 캐싱 래퍼 사용 (기존: 직접 requests.get)
                if bs:
                    equity = to_float(bs.get("totalStockholdersEquity") or bs.get("stockholdersEquity"))
                    total_debt = to_float(bs.get("totalDebt") or bs.get("longTermDebt"))
                    net_income = to_float(latest.get("netIncome")) if latest else np.nan
                    # ROE = Net Income / Equity
                    if not info.get("returnOnEquity") and pd.notna(net_income) and pd.notna(equity) and equity != 0:
                        info["returnOnEquity"] = float(net_income / equity)
                    # D/E = Total Debt / Equity
                    if not info.get("debtToEquity") and pd.notna(total_debt) and pd.notna(equity) and equity != 0:
                        info["debtToEquity"] = float(total_debt / equity)
        except Exception:
            pass

    return info


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_company_overview(ticker_upper: str) -> dict:
    """회사 기본 정보 조회 — FMP profile primary, _fmp_fill 보완."""
    try:
        info = {}
        info = _fmp_fill(info, ticker_upper)
        p = _fmp_profile(ticker_upper)
        name = str(info.get("longName") or info.get("shortName") or p.get("companyName") or ticker_upper)
        sector_en = str(info.get("sector") or p.get("sector") or "")
        industry_en = str(info.get("industry") or p.get("industry") or "")
        quote_type = str(info.get("quoteType") or "").upper()
        is_etf = quote_type in ("ETF", "MUTUALFUND") or bool(p.get("isEtf"))
        country = str(info.get("country") or p.get("country") or "N/A")
        summary_en = str(info.get("longBusinessSummary") or p.get("description") or "")
        employees = info.get("fullTimeEmployees") or p.get("fullTimeEmployees")
        market_cap = to_float(info.get("marketCap") or p.get("mktCap"))
        website = str(info.get("website") or p.get("website") or "")
        sector_kr = translate_ko(sector_en, _SECTOR_KR) if sector_en else "N/A"
        industry_kr = translate_ko(industry_en, _INDUSTRY_KR) if industry_en else "N/A"
        # 다음 실적 발표일 — FMP 4단계 순차 시도 (내일 이후 날짜만)
        next_earnings = None
        k = _fmp_key()
        tomorrow = (datetime.now(_MARKET_ET_TZ).date() + timedelta(days=1))  # 시장 기준 = 미 동부시간

        def _first_future_date(items):
            """리스트에서 내일 이후 가장 이른 날짜 반환."""
            future = []
            for it in (items if isinstance(items, list) else []):
                ds = str(it.get("date") or "")[:10]
                if not ds or len(ds) < 10:
                    continue
                try:
                    if datetime.strptime(ds, "%Y-%m-%d").date() >= tomorrow:
                        future.append(ds)
                except Exception:
                    continue
            return sorted(future)[0] if future else None

        if k:
            # 1) earnings-calendar
            try:
                r = requests.get(
                    f"{_FMP_BASE}/earnings-calendar?symbol={ticker_upper}&apikey={k}",
                    timeout=_FMP_TIMEOUT
                )
                if r.status_code == 200:
                    next_earnings = _first_future_date(r.json())
            except Exception:
                pass

            # 2) earnings (Earnings Report API — 미래 예정 포함)
            if not next_earnings:
                try:
                    r2 = requests.get(
                        f"{_FMP_BASE}/earnings?symbol={ticker_upper}&apikey={k}",
                        timeout=_FMP_TIMEOUT
                    )
                    if r2.status_code == 200:
                        next_earnings = _first_future_date(r2.json())
                except Exception:
                    pass

            # 3) analyst-estimates 분기별
            if not next_earnings:
                try:
                    r3 = requests.get(
                        f"{_FMP_BASE}/analyst-estimates?symbol={ticker_upper}&period=quarter&limit=4&apikey={k}",
                        timeout=_FMP_TIMEOUT
                    )
                    if r3.status_code == 200:
                        next_earnings = _first_future_date(r3.json())
                except Exception:
                    pass

            # 4) quote earningsAnnouncement
            if not next_earnings:
                try:
                    rq = requests.get(
                        f"{_FMP_BASE}/quote?symbol={ticker_upper}&apikey={k}",
                        timeout=_FMP_TIMEOUT
                    )
                    if rq.status_code == 200:
                        qd = rq.json()
                        q_item = qd[0] if isinstance(qd, list) and qd else {}
                        ann = str(q_item.get("earningsAnnouncement") or "")[:10]
                        if ann and len(ann) == 10:
                            try:
                                if datetime.strptime(ann, "%Y-%m-%d").date() >= tomorrow:
                                    next_earnings = ann
                            except Exception:
                                pass
                except Exception:
                    pass

        # profile earningsAnnouncement fallback
        if not next_earnings:
            ann = str(p.get("earningsAnnouncement") or "")[:10]
            if ann and len(ann) == 10:
                try:
                    if datetime.strptime(ann, "%Y-%m-%d").date() >= tomorrow:
                        next_earnings = ann
                except Exception:
                    pass
        return {
            "name": name,
            "sector": sector_kr, "sector_en": sector_en,
            "industry": industry_kr, "industry_en": industry_en,
            "is_etf": is_etf,
            "country": country,
            "summary_en": summary_en,
            "employees": employees, "market_cap": market_cap,
            "website": website, "next_earnings": next_earnings,
        }
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def fetch_price_history_by_period(ticker_upper: str, period: str) -> pd.DataFrame:
    """기간별 주가 히스토리 — FMP historical-price-eod 사용. period별 독립 캐시."""
    # period별 필요 limit (넉넉하게 요청 후 슬라이싱)
    period_limit_map = {
        "1D":  3,
        "1M":  25,
        "3M":  70,
        "YTD": 365,
        "1Y":  260,
        "5Y":  1300,
        "MAX": 6000,
    }
    limit = period_limit_map.get(period, 260)
    df = _fmp_price_history(ticker_upper, limit=limit)
    if df.empty:
        return df

    # 기간에 맞게 정확히 슬라이싱
    now = pd.Timestamp.now()
    if period == "1D":
        df = df.tail(2)
    elif period == "1M":
        cutoff = now - pd.DateOffset(months=1)
        df = df[df.index >= cutoff]
    elif period == "3M":
        cutoff = now - pd.DateOffset(months=3)
        df = df[df.index >= cutoff]
    elif period == "YTD":
        cutoff = pd.Timestamp(f"{now.year}-01-01")
        df = df[df.index >= cutoff]
    elif period == "1Y":
        cutoff = now - pd.DateOffset(years=1)
        df = df[df.index >= cutoff]
    elif period == "5Y":
        cutoff = now - pd.DateOffset(years=5)
        df = df[df.index >= cutoff]
    # MAX는 전체 반환

    return df if not df.empty else pd.DataFrame()


def calculate_macd(close_series: pd.Series, fast=12, slow=26, signal=9):
    """MACD 라인, 시그널 라인, 히스토그램 반환."""
    ema_fast = close_series.ewm(span=fast, adjust=False).mean()
    ema_slow = close_series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_bollinger_bands(close_series: pd.Series, window=20, num_std=2):
    """볼린저 밴드 상단/중단/하단 반환."""
    ma = close_series.rolling(window=window, min_periods=window).mean()
    std = close_series.rolling(window=window, min_periods=window).std()
    upper = ma + num_std * std
    lower = ma - num_std * std
    return upper, ma, lower


def _etf_holdings_perf_cache_key(holdings_df):
    pairs = []
    if holdings_df is None or holdings_df.empty:
        return ()
    df = holdings_df[["Ticker", "Weight(%)"]].copy()
    for _, row in df.iterrows():
        t = str(row.get("Ticker", "") or "").strip().upper()
        w = pd.to_numeric(row.get("Weight(%)"), errors="coerce")
        w_safe = round(float(w), 6) if pd.notna(w) else None
        pairs.append((t, w_safe))
    return tuple(pairs)


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_build_etf_holdings_performance_pairs(holding_pairs):
    if not holding_pairs:
        return pd.DataFrame(
            columns=["Ticker", "현재가", "1개월(%)", "3개월(%)", "6개월(%)", "12개월(%)", "비중(%)"]
        )
    holdings_df = pd.DataFrame([(p[0], p[1]) for p in holding_pairs], columns=["Ticker", "Weight(%)"])
    return build_etf_holdings_performance(holdings_df)


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_etf_holdings_universe_str(etf_ticker: str):
    t_clean = str(etf_ticker or "").strip().upper()
    k = _fmp_key()
    if not k:
        return []
    try:
        r = requests.get(f"{_FMP_BASE}/etf-holder/{t_clean}?apikey={k}", timeout=_FMP_TIMEOUT)
        data = r.json() if r.status_code == 200 else []
        if isinstance(data, list) and data:
            return [str(item.get("asset") or item.get("symbol") or "").strip().upper()
                    for item in data if item.get("asset") or item.get("symbol")]
    except Exception:
        pass
    return []


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_portfolio_yf_close_1y(tuple_tickers: tuple[str, ...]):
    tickers_list = list(dict.fromkeys([t for t in tuple_tickers if str(t).strip()]))
    if not tickers_list:
        return pd.DataFrame()
    close_df = _fmp_batch_to_close_df(tickers_list, limit=252)
    if close_df.empty:
        return close_df
    if len(tickers_list) == 1 and "SINGLE" in close_df.columns:
        close_df = close_df.copy()
        close_df.columns = [tickers_list[0]]
    return close_df


def render_sync_button(key: str, clear_funcs: list, caption: str = "캐시를 비우고 최신 데이터를 다시 가져옵니다."):
    """모든 탭 상단에 공통으로 사용하는 데이터 동기화 버튼."""
    _sc1, _sc2 = st.columns([1, 3])
    with _sc1:
        if st.button("🔄 현재 페이지 데이터 동기화", key=key, use_container_width=True):
            tab_sync_refresh(clear_funcs, rerun_after=True)
    with _sc2:
        st.caption(caption)


def tab_sync_refresh(clear_callbacks, rerun_after=True):
    """Invalidate tab-specific caches, toast notice, optionally rerun."""
    st.toast("데이터를 동기화 중입니다...", icon="🔄", duration="short")
    for cb in clear_callbacks:
        try:
            cb()
        except Exception:
            pass
    if rerun_after:
        st.rerun()


def evaluate_kpis(ticker_symbol):
    try:
        tk_upper = str(ticker_symbol).strip().upper()
        # FMP primary
        info = _fmp_fill({}, tk_upper)
        # 주가 히스토리 (MA200 등 계산용)
        history = _fmp_price_history(tk_upper, limit=252)
        # FMP 재무제표 활용
        inc = _fmp_income(tk_upper)
        cf = _fmp_cashflow(tk_upper)
        latest_inc = inc.get("latest", {})
        prev_inc = inc.get("prev", {})
        # FCF
        _fcf_val = to_float(cf.get("freeCashFlow"))
        if pd.isna(_fcf_val):
            _ocf = to_float(cf.get("operatingCashFlow"))
            _capex = to_float(cf.get("capitalExpenditure"))
            if pd.notna(_ocf) and pd.notna(_capex):
                _fcf_val = float(_ocf + _capex)
        if pd.notna(_fcf_val):
            info["_fmp_fcf"] = _fcf_val
    except Exception:
        tk_upper = str(ticker_symbol).strip().upper()
        info = _fmp_fill({}, tk_upper)
        history = pd.DataFrame()
        latest_inc, prev_inc = {}, {}

    # ── 지표 추출 ──────────────────────────────────────────────────────
    # ROE — FMP fill로 제공 (income_stmt/balance_sheet 분기 제거됨)
    roe = to_float(info.get("returnOnEquity"))

    # Operating Margin — FMP fill로 제공
    operating_margin = to_float(info.get("operatingMargins"))

    # Debt to Equity — FMP fill로 제공
    debt_to_equity = to_float(info.get("debtToEquity"))

    # PEG Ratio
    peg_ratio = to_float(info.get("pegRatio") or info.get("trailingPegRatio"))

    # Valuation 지표 — FMP 무료 플랜 실제 제공 필드 기준
    forward_pe    = to_float(info.get("forwardPE"))
    trailing_pe   = to_float(info.get("trailingPE"))
    price_to_book = to_float(info.get("priceToBook"))

    # Forward P/E fallback — 캐싱 래퍼 사용 (_fmp_fill 캐시 미스 대비)
    if pd.isna(forward_pe):
        try:
            _fp_data = _fmp_analyst_estimates(tk_upper)
            if isinstance(_fp_data, list) and _fp_data:
                _cur_year = datetime.now(_MARKET_ET_TZ).year
                _fp_sorted = sorted(_fp_data, key=lambda x: str(x.get("date") or "9999"))
                for _ae in _fp_sorted:
                    _ae_y = str(_ae.get("date") or "")[:4]
                    try:
                        if int(_ae_y) < _cur_year:
                            continue
                    except Exception:
                        continue
                    _eps = to_float(_ae.get("epsAvg") or _ae.get("estimatedEpsAvg") or _ae.get("estimatedEps"))
                    _cp  = to_float(info.get("currentPrice") or info.get("price"))
                    if pd.notna(_eps) and _eps > 0 and pd.notna(_cp) and _cp > 0:
                        _fwd = round(float(_cp) / float(_eps), 2)
                        if 0 < _fwd < 2000:
                            forward_pe = _fwd
                            info["forwardPE"] = _fwd
                            break
        except Exception:
            pass
    ev_to_ebitda  = to_float(info.get("enterpriseToEbitda"))
    # FMP 무료 대체 지표
    ev_to_sales   = to_float(info.get("_fmp_ev_to_sales"))
    ev_to_fcf     = to_float(info.get("_fmp_ev_to_fcf"))

    # EPS & Growth
    trailing_eps = to_float(info.get("trailingEps") or info.get("epsTrailingTwelveMonths"))
    earnings_growth = to_float(info.get("earningsGrowth") or info.get("revenueGrowth"))

    # Free Cash Flow — FMP cashflow에서 직접
    fcf = to_float(info.get("_fmp_fcf"))
    if pd.isna(fcf):
        cf_data = _fmp_cashflow(str(ticker_symbol).strip().upper())
        _fcf = to_float(cf_data.get("freeCashFlow"))
        if pd.isna(_fcf):
            _ocf = to_float(cf_data.get("operatingCashFlow"))
            _capex = to_float(cf_data.get("capitalExpenditure"))
            if pd.notna(_ocf) and pd.notna(_capex):
                _fcf = float(_ocf + _capex)
        if pd.notna(_fcf):
            fcf = _fcf

    # Momentum (가격 + MA)
    current_price, ma50, ma200 = get_momentum_values(history)
    if pd.isna(current_price):
        current_price = to_float(info.get("currentPrice") or info.get("regularMarketPrice"))
    if pd.isna(ma50):
        ma50 = to_float(info.get("fiftyDayAverage"))
    if pd.isna(ma200):
        ma200 = to_float(info.get("twoHundredDayAverage"))

    momentum_pass = (
        not pd.isna(current_price) and not pd.isna(ma50) and not pd.isna(ma200)
        and current_price > ma50 and current_price > ma200
    )
    growth_percent = earnings_growth * 100 if not pd.isna(earnings_growth) else np.nan
    intrinsic_value = np.nan
    # Graham 공식: EPS > 0 이고 성장률 > 0 일 때만 의미있음
    # 성장률 음수 or EPS 음수면 공식 결과가 왜곡됨 → 계산 생략
    if (not pd.isna(trailing_eps) and trailing_eps > 0
            and not pd.isna(growth_percent) and growth_percent > 0):
        intrinsic_value = trailing_eps * (8.5 + (2 * growth_percent))
        # 결과가 비현실적으로 크면(현재가 10배 이상) 신뢰도 낮으므로 표시만 경고
        if not pd.isna(current_price) and current_price > 0:
            if intrinsic_value > current_price * 10 or intrinsic_value < 0:
                intrinsic_value = np.nan  # 비현실적 값 제거

    margin_of_safety = np.nan
    if not pd.isna(intrinsic_value) and intrinsic_value != 0 and not pd.isna(current_price):
        margin_of_safety = ((intrinsic_value - current_price) / intrinsic_value) * 100
    core_fcf_pass = (not pd.isna(fcf)) and (fcf > 0)

    # ── 섹터 파악 ─────────────────────────────────────────────────────
    sector_en = str(info.get("sector_en") or info.get("sector") or "").lower()

    # 섹터별 밸류에이션 기준 (절대값)
    # 기술/성장주는 높은 P/E 허용, 가치주/금융은 엄격
    _is_tech   = any(w in sector_en for w in ["technology", "communication", "tech"])
    _is_growth = any(w in sector_en for w in ["consumer cyclical", "automotive", "discretionary"])
    _is_value  = any(w in sector_en for w in ["financial", "utilities", "real estate", "energy", "materials", "industrial"])
    _is_health = "health" in sector_en or "pharma" in sector_en or "biotech" in sector_en

    # 섹터별 P/E 상한선
    if _is_tech or _is_growth:
        pe_max = 120       # 기술/성장주: P/E 120까지 허용
        pb_max = 20        # P/B 20까지
        ev_sales_max = 20  # EV/Sales 20까지
        roe_min = 0.10     # ROE 10% 이상
    elif _is_health:
        pe_max = 80
        pb_max = 10
        ev_sales_max = 15
        roe_min = 0.10
    elif _is_value:
        pe_max = 20
        pb_max = 3
        ev_sales_max = 5
        roe_min = 0.12
    else:  # 기타
        pe_max = 60
        pb_max = 5
        ev_sales_max = 10
        roe_min = 0.15

    # ── 상대 평가: 성장률 대비 밸류에이션 ─────────────────────────────
    # PEG < 2 = 성장 대비 합리적, PEG < 1 = 저평가
    peg_pass_threshold = 2.0  # 섹터 관계없이 PEG 2 이하면 OK

    # EV/EBITDA 섹터별
    ev_ebitda_max = 60 if (_is_tech or _is_growth) else (40 if _is_health else 25)
    ev_fcf_max = 80 if (_is_tech or _is_growth) else (50 if _is_health else 40)

    # D/E 섹터별 (금융은 레버리지 높음)
    de_max = 300 if _is_value and "financial" in sector_en else 100

    # ── KPI 행 구성 ───────────────────────────────────────────────────
    # 섹터 라벨
    sector_label = "기술/성장주" if (_is_tech or _is_growth) else ("헬스케어" if _is_health else ("가치주" if _is_value else "일반"))

    rows = [
        {
            "Category": "수익성 (Profitability)",
            "KPI": "ROE",
            "Value": pct_str(roe),
            "Rule": f"{int(roe_min*100)}% 이상 [{sector_label}]",
            "Pass": pass_fail_badge(not pd.isna(roe) and roe >= roe_min, pd.isna(roe)),
        },
        {
            "Category": "수익성 (Profitability)",
            "KPI": "Operating Margin",
            "Value": pct_str(operating_margin),
            "Rule": "0 이상",
            "Pass": pass_fail_badge(not pd.isna(operating_margin) and operating_margin >= 0, pd.isna(operating_margin)),
        },
        {
            "Category": "건전성 (Financial Strength)",
            "KPI": "Debt-to-Equity",
            "Value": num_str(debt_to_equity),
            "Rule": f"{de_max} 미만 [{sector_label}]",
            "Pass": pass_fail_badge(not pd.isna(debt_to_equity) and debt_to_equity < de_max, pd.isna(debt_to_equity)),
        },
        {
            "Category": "건전성 (Financial Strength)",
            "KPI": "Free Cash Flow (최근연도)",
            "Value": won_str(fcf),
            "Rule": "0 초과",
            "Pass": pass_fail_badge(not pd.isna(fcf) and fcf > 0, pd.isna(fcf)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "Forward P/E",
            "Value": num_str(forward_pe) if pd.notna(forward_pe) else "N/A",
            "Rule": f"5~{pe_max} [{sector_label}]",
            "Pass": pass_fail_badge(pd.notna(forward_pe) and 0 < forward_pe <= pe_max, pd.isna(forward_pe)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "Trailing P/E",
            "Value": num_str(trailing_pe) if pd.notna(trailing_pe) else "N/A",
            "Rule": f"0 초과 & {pe_max} 이하 [{sector_label}]",
            "Pass": pass_fail_badge(pd.notna(trailing_pe) and 0 < trailing_pe <= pe_max, pd.isna(trailing_pe)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "P/B (Price-to-Book)",
            "Value": num_str(price_to_book) if pd.notna(price_to_book) else "N/A",
            "Rule": f"{pb_max:.0f} 이하 [{sector_label}]",
            "Pass": pass_fail_badge(pd.notna(price_to_book) and price_to_book <= pb_max, pd.isna(price_to_book)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "EV/EBITDA",
            "Value": num_str(ev_to_ebitda) if pd.notna(ev_to_ebitda) else "N/A",
            "Rule": f"{ev_ebitda_max} 이하 [{sector_label}]",
            "Pass": pass_fail_badge(pd.notna(ev_to_ebitda) and 0 < ev_to_ebitda <= ev_ebitda_max, pd.isna(ev_to_ebitda)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "EV/Sales",
            "Value": num_str(ev_to_sales) if pd.notna(ev_to_sales) else "N/A",
            "Rule": f"{ev_sales_max} 이하 [{sector_label}]",
            "Pass": pass_fail_badge(pd.notna(ev_to_sales) and 0 < ev_to_sales <= ev_sales_max, pd.isna(ev_to_sales)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "EV/FCF",
            "Value": num_str(ev_to_fcf) if pd.notna(ev_to_fcf) else "N/A",
            "Rule": f"{ev_fcf_max} 이하 [{sector_label}]",
            "Pass": pass_fail_badge(pd.notna(ev_to_fcf) and 0 < ev_to_fcf <= ev_fcf_max, pd.isna(ev_to_fcf)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "PEG Ratio (성장 대비)",
            "Value": num_str(peg_ratio),
            "Rule": f"{peg_pass_threshold} 미만 = 성장 대비 합리적",
            "Pass": pass_fail_badge(not pd.isna(peg_ratio) and peg_ratio < peg_pass_threshold, pd.isna(peg_ratio)),
        },
    ]

    # Margin of Safety — 가치주 조건 만족 시에만 KPI 행 추가
    # 조건: EPS > 0, 성장률 0~20%, P/E < 25
    _pe_for_check = trailing_pe if pd.notna(trailing_pe) else forward_pe
    _is_value_stock_kpi = (
        not pd.isna(trailing_eps) and trailing_eps > 0
        and not pd.isna(growth_percent) and 0 < growth_percent < 20
        and (pd.isna(_pe_for_check) or _pe_for_check < 25)
    )
    if _is_value_stock_kpi:
        rows.append({
            "Category": "밸류에이션 (Valuation)",
            "KPI": "Margin of Safety (가치주)",
            "Value": pct_points_str(margin_of_safety),
            "Rule": "20% 이상",
            "Pass": pass_fail_badge(not pd.isna(margin_of_safety) and margin_of_safety >= 20, pd.isna(margin_of_safety)),
        })
    rows.append({
        "Category": "모멘텀 (Momentum)",
        "KPI": "가격>MA50&MA200",
        "Value": f"${num_str(current_price)} / MA50 ${num_str(ma50)} / MA200 ${num_str(ma200)}",
        "Rule": "정배열",
        "Pass": pass_fail_badge(momentum_pass, pd.isna(current_price) or pd.isna(ma50) or pd.isna(ma200)),
    })

    kpi_df = pd.DataFrame(rows)
    _p = kpi_df["Pass"].astype(str)
    pass_count   = (_p.str.contains("Pass",  case=False) & ~_p.str.contains("Fail", case=False)).sum()
    fail_count   = _p.str.contains("Fail",   case=False).sum()
    nodata_count = (_p.str.contains("N/A|No Data", case=False) & ~_p.str.contains("Fail|Pass", case=False)).sum()

    margin_context = {
        "intrinsic_value": intrinsic_value,
        "margin_of_safety": margin_of_safety,
        "trailing_eps": trailing_eps,
        "growth_percent": growth_percent,
        "current_price": current_price,
        "core_fcf_pass": core_fcf_pass,
        "forward_pe": forward_pe,
        "trailing_pe": trailing_pe,
        "price_to_book": price_to_book,
        "ev_to_ebitda": ev_to_ebitda,
        "ev_to_sales": ev_to_sales,
        "ev_to_fcf": ev_to_fcf,
    }

    return kpi_df, pass_count, fail_count, nodata_count, margin_context


def calculate_style_scores(ticker_symbol: str, margin_context: dict, kpi_df) -> dict:
    """
    3가지 투자 스타일 점수 계산.
    CAN SLIM / 가치투자 / 장기 우량주 각 100점 만점.
    """
    ticker_upper = str(ticker_symbol).strip().upper()

    # ── 공통 데이터 수집 ──────────────────────────────────────────────
    try:
        info = _fmp_fill({}, ticker_upper)
        earn_df = cached_earnings_history(ticker_upper)
        inst_df = cached_institutional_holders(ticker_upper)
        hist = _fmp_price_history(ticker_upper, limit=65)
    except Exception:
        info = _fmp_fill({}, ticker_upper)
        earn_df = inst_df = hist = pd.DataFrame()

    # ── FMP income-statement에서 직접 성장률 계산 ─────────────────────
    _inc_annual = {}
    _cf_latest = {}
    k_val = _fmp_key()
    if k_val:
        try:
            r_inc = requests.get(
                f"{_FMP_BASE}/income-statement?symbol={ticker_upper}&period=annual&limit=3&apikey={k_val}",
                timeout=_FMP_TIMEOUT
            )
            inc_data = r_inc.json() if r_inc.status_code == 200 else []
            if isinstance(inc_data, list) and len(inc_data) >= 2:
                latest_inc = inc_data[0]
                prev_inc   = inc_data[1]
                rev_now  = to_float(latest_inc.get("revenue"))
                rev_prev = to_float(prev_inc.get("revenue"))
                ni_now   = to_float(latest_inc.get("netIncome"))
                ni_prev  = to_float(prev_inc.get("netIncome"))
                eps_now  = to_float(latest_inc.get("epsdiluted") or latest_inc.get("eps"))
                eps_prev = to_float(prev_inc.get("epsdiluted") or prev_inc.get("eps"))
                if pd.notna(rev_now) and pd.notna(rev_prev) and rev_prev != 0:
                    _inc_annual["revenue_gr"] = (rev_now - rev_prev) / abs(rev_prev)
                if pd.notna(ni_now) and pd.notna(ni_prev) and ni_prev != 0:
                    _inc_annual["earnings_gr"] = (ni_now - ni_prev) / abs(ni_prev)
                if pd.notna(eps_now):
                    _inc_annual["trailing_eps"] = eps_now
                if pd.notna(eps_now) and pd.notna(eps_prev) and eps_prev != 0:
                    _inc_annual["eps_gr"] = (eps_now - eps_prev) / abs(eps_prev)
                _inc_annual["op_margin"] = to_float(latest_inc.get("operatingIncomeRatio") or
                    ((to_float(latest_inc.get("operatingIncome")) or 0) / rev_now if rev_now else None))
        except Exception:
            pass
        try:
            r_cf = requests.get(
                f"{_FMP_BASE}/cash-flow-statement?symbol={ticker_upper}&period=annual&limit=1&apikey={k_val}",
                timeout=_FMP_TIMEOUT
            )
            cf_data = r_cf.json() if r_cf.status_code == 200 else []
            if isinstance(cf_data, list) and cf_data:
                cf = cf_data[0]
                fcf = to_float(cf.get("freeCashFlow"))
                if pd.isna(fcf):
                    ocf = to_float(cf.get("operatingCashFlow"))
                    capex = to_float(cf.get("capitalExpenditure"))
                    if pd.notna(ocf) and pd.notna(capex):
                        fcf = ocf + capex
                _cf_latest["fcf"] = fcf
        except Exception:
            pass

    # ── 지표 추출 ──────────────────────────────────────────────────────
    trailing_pe   = to_float(info.get("trailingPE"))
    forward_pe    = to_float(info.get("forwardPE"))
    price_to_book = to_float(info.get("priceToBook"))
    ev_to_ebitda  = to_float(info.get("enterpriseToEbitda"))
    ev_to_sales   = to_float(info.get("_fmp_ev_to_sales"))
    roe           = to_float(info.get("returnOnEquity"))
    op_margin     = to_float(_inc_annual.get("op_margin") or info.get("operatingMargins"))
    debt_to_eq    = to_float(info.get("debtToEquity"))
    trailing_eps  = to_float(_inc_annual.get("trailing_eps") or info.get("trailingEps"))
    earnings_gr   = to_float(_inc_annual.get("earnings_gr") or info.get("earningsGrowth"))
    revenue_gr    = to_float(_inc_annual.get("revenue_gr") or info.get("revenueGrowth"))
    eps_yoy_gr    = to_float(_inc_annual.get("eps_gr") or earnings_gr)
    dividend_rate = to_float(info.get("dividendRate") or info.get("trailingAnnualDividendRate"))
    dividend_yld  = to_float(info.get("dividendYield") or info.get("trailingAnnualDividendYield"))
    current_price = margin_context.get("current_price") or to_float(info.get("currentPrice") or info.get("price"))
    ma50          = to_float(info.get("fiftyDayAverage") or info.get("priceAvg50"))
    ma200         = to_float(info.get("twoHundredDayAverage") or info.get("priceAvg200"))
    week52_high   = to_float(info.get("fiftyTwoWeekHigh") or info.get("yearHigh"))
    fcf_val       = to_float(_cf_latest.get("fcf") or info.get("freeCashflow") or info.get("_fmp_fcf"))
    market_cap    = to_float(info.get("marketCap"))

    # 분기 EPS 성장 (QoQ) — Earnings 히스토리에서
    qoq_eps_growth = np.nan
    if not earn_df.empty:
        try:
            act_col = next((
                c for c in earn_df.columns
                if "actual" in c.lower() or "reported" in c.lower()
                   or "eps 실제" in c.lower() or "실제" in c.lower()
            ), None)
            if act_col and len(earn_df) >= 2:
                acts = pd.to_numeric(
                    earn_df[act_col].astype(str).str.replace(r"[$,]", "", regex=True),
                    errors="coerce"
                ).dropna()
                if len(acts) >= 2 and acts.iloc[1] != 0:
                    qoq_eps_growth = float((acts.iloc[0] - acts.iloc[1]) / abs(acts.iloc[1]) * 100)
        except Exception:
            pass

    # QoQ EPS fallback — FMP income-statement(quarter) 직접 계산
    if pd.isna(qoq_eps_growth) and k_val:
        try:
            r_q = requests.get(
                f"{_FMP_BASE}/income-statement?symbol={ticker_upper}&period=quarter&limit=4&apikey={k_val}",
                timeout=_FMP_TIMEOUT
            )
            if r_q.status_code == 200:
                q_data = r_q.json()
                if isinstance(q_data, list) and len(q_data) >= 2:
                    eps_now  = to_float(q_data[0].get("epsdiluted") or q_data[0].get("eps"))
                    eps_prev = to_float(q_data[1].get("epsdiluted") or q_data[1].get("eps"))
                    if pd.notna(eps_now) and pd.notna(eps_prev) and eps_prev != 0:
                        qoq_eps_growth = float((eps_now - eps_prev) / abs(eps_prev) * 100)
        except Exception:
            pass

    # I: 애널리스트 매수 컨센서스 (ratings-snapshot) — FMP Starter 제공
    # 기관 보유 API가 Starter 플랜 차단이므로 ratings-snapshot으로 대체
    analyst_buy_pct = np.nan
    analyst_label   = ""
    if k_val:
        try:
            r_rat = requests.get(
                f"{_FMP_BASE}/ratings-snapshot?symbol={ticker_upper}&apikey={k_val}",
                timeout=_FMP_TIMEOUT
            )
            if r_rat.status_code == 200:
                rat_data = r_rat.json()
                rat = rat_data[0] if isinstance(rat_data, list) and rat_data else (rat_data if isinstance(rat_data, dict) else {})
                if rat:
                    sb  = to_float(rat.get("strongBuy")  or 0)
                    b   = to_float(rat.get("buy")        or 0)
                    h   = to_float(rat.get("hold")       or 0)
                    s   = to_float(rat.get("sell")       or 0)
                    ss  = to_float(rat.get("strongSell") or 0)
                    tot = (sb or 0)+(b or 0)+(h or 0)+(s or 0)+(ss or 0)
                    if tot > 0:
                        bc = (sb or 0)+(b or 0)
                        analyst_buy_pct = round(float(bc / tot * 100), 1)
                        analyst_label = f"매수의견 {analyst_buy_pct:.0f}% ({int(bc)}/{int(tot)}명)"
        except Exception:
            pass

    # grades-consensus fallback
    if pd.isna(analyst_buy_pct) and k_val:
        try:
            r_gc = requests.get(
                f"{_FMP_BASE}/grades-consensus?symbol={ticker_upper}&apikey={k_val}",
                timeout=_FMP_TIMEOUT
            )
            if r_gc.status_code == 200:
                gc_data = r_gc.json()
                gc = gc_data[0] if isinstance(gc_data, list) and gc_data else (gc_data if isinstance(gc_data, dict) else {})
                if gc:
                    sb  = to_float(gc.get("strongBuy")  or 0)
                    b   = to_float(gc.get("buy")        or 0)
                    h   = to_float(gc.get("hold")       or 0)
                    s   = to_float(gc.get("sell")       or 0)
                    ss  = to_float(gc.get("strongSell") or 0)
                    tot = (sb or 0)+(b or 0)+(h or 0)+(s or 0)+(ss or 0)
                    if tot > 0:
                        bc = (sb or 0)+(b or 0)
                        analyst_buy_pct = round(float(bc / tot * 100), 1)
                        analyst_label = f"매수의견 {analyst_buy_pct:.0f}% ({int(bc)}/{int(tot)}명)"
        except Exception:
            pass

    # 거래량 비율 (최근 5일 / 3개월 평균)
    vol_ratio = np.nan
    if not hist.empty and "Volume" in hist.columns:
        try:
            vols = pd.to_numeric(hist["Volume"], errors="coerce").dropna()
            if len(vols) >= 10:
                vol_ratio = float(vols.tail(5).mean() / vols.mean())
        except Exception:
            pass

    # 52주 고점 근접도
    near_high_pct = np.nan
    if pd.notna(current_price) and pd.notna(week52_high) and week52_high > 0:
        near_high_pct = float(current_price / week52_high * 100)

    # FCF Yield
    fcf_yield = np.nan
    if pd.notna(fcf_val) and pd.notna(market_cap) and market_cap > 0:
        fcf_yield = float(fcf_val / market_cap * 100)

    # ══════════════════════════════════════════════════════════════════
    # 1. CAN SLIM 점수 (100점)
    # ══════════════════════════════════════════════════════════════════
    cs_score = 0
    cs_detail = {}

    # C: 현재 분기 EPS 성장 (25점)
    if pd.notna(qoq_eps_growth):
        if qoq_eps_growth >= 25:   pts = 25
        elif qoq_eps_growth >= 15: pts = 18
        elif qoq_eps_growth >= 5:  pts = 10
        elif qoq_eps_growth > 0:   pts = 5
        else:                      pts = 0
        cs_score += pts
        cs_detail["C_분기EPS성장"] = f"{qoq_eps_growth:.1f}% → {pts}점"
    else:
        cs_detail["C_분기EPS성장"] = "데이터 없음"

    # A: 연간 EPS 성장 (20점)
    _a_growth = eps_yoy_gr * 100 if pd.notna(eps_yoy_gr) else (earnings_gr * 100 if pd.notna(earnings_gr) else np.nan)
    if pd.notna(_a_growth):
        if _a_growth >= 25:   pts = 20
        elif _a_growth >= 15: pts = 14
        elif _a_growth >= 5:  pts = 8
        elif _a_growth > 0:   pts = 4
        else:                 pts = 0
        cs_score += pts
        cs_detail["A_연간EPS성장"] = f"{_a_growth:.1f}% → {pts}점"
    else:
        cs_detail["A_연간EPS성장"] = "데이터 없음"

    # A보조: 매출 성장 (10점)
    _r_growth = revenue_gr * 100 if pd.notna(revenue_gr) else np.nan
    if pd.notna(_r_growth):
        if _r_growth >= 20:   pts = 10
        elif _r_growth >= 10: pts = 7
        elif _r_growth >= 5:  pts = 4
        elif _r_growth > 0:   pts = 2
        else:                 pts = 0
        cs_score += pts
        cs_detail["A_매출성장"] = f"{_r_growth:.1f}% → {pts}점"
    else:
        cs_detail["A_매출성장"] = "데이터 없음"

    # L+N: 모멘텀 / 52주 고점 근접 (20점)
    momentum_pts = 0
    _pe_check = trailing_pe if pd.notna(trailing_pe) else forward_pe
    if pd.notna(current_price) and pd.notna(ma50) and pd.notna(ma200):
        if current_price > ma50 and current_price > ma200:
            momentum_pts += 12
    if pd.notna(near_high_pct):
        if near_high_pct >= 95:   momentum_pts += 8
        elif near_high_pct >= 85: momentum_pts += 5
        elif near_high_pct >= 75: momentum_pts += 2
    cs_score += momentum_pts
    cs_detail["L+N_모멘텀"] = f"MA정배열+고점근접 → {momentum_pts}점"

    # I: 애널리스트 매수 컨센서스 (15점) — 기관보유 대체
    if pd.notna(analyst_buy_pct):
        if analyst_buy_pct >= 70:   pts = 15
        elif analyst_buy_pct >= 55: pts = 10
        elif analyst_buy_pct >= 40: pts = 5
        else:                       pts = 2
        cs_score += pts
        cs_detail["I_기관보유"] = f"{analyst_label} → {pts}점"
    else:
        cs_detail["I_기관보유"] = "데이터 없음"

    # S: 거래량 (10점)
    if pd.notna(vol_ratio):
        if vol_ratio >= 1.5:   pts = 10
        elif vol_ratio >= 1.2: pts = 7
        elif vol_ratio >= 1.0: pts = 4
        else:                  pts = 0
        cs_score += pts
        cs_detail["S_거래량"] = f"평균대비 {vol_ratio:.2f}x → {pts}점"
    else:
        cs_detail["S_거래량"] = "데이터 없음"

    # ══════════════════════════════════════════════════════════════════
    # 2. 가치투자 점수 (100점)
    # ══════════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════════
    # 섹터 파악 (스타일 점수용)
    _sector_en = str(info.get("sector_en") or info.get("sector") or "").lower()
    _s_tech   = any(w in _sector_en for w in ["technology", "communication", "tech"])
    _s_growth = any(w in _sector_en for w in ["consumer cyclical", "automotive", "discretionary"])
    _s_value  = any(w in _sector_en for w in ["financial", "utilities", "real estate", "energy", "materials"])

    # 섹터별 P/E 점수 기준
    if _s_tech or _s_growth:
        _pe_tiers = [(30, 25), (50, 20), (80, 14), (120, 8), (200, 3)]   # 기술/성장주
    elif _s_value:
        _pe_tiers = [(10, 25), (15, 20), (20, 14), (25, 8), (35, 3)]     # 가치주
    else:
        _pe_tiers = [(15, 25), (20, 20), (30, 14), (50, 8), (80, 3)]     # 일반

    # 섹터별 P/B 기준
    if _s_tech or _s_growth:
        _pb_tiers = [(3, 20), (8, 15), (15, 10), (20, 5)]
    else:
        _pb_tiers = [(1, 20), (2, 15), (3, 10), (5, 5)]

    val_score = 0
    val_detail = {}

    # P/E (25점) — 섹터별 기준
    _pe = trailing_pe if pd.notna(trailing_pe) else forward_pe
    if pd.notna(_pe) and _pe > 0:
        pts = 0
        for threshold, score in _pe_tiers:
            if _pe <= threshold:
                pts = score
                break
        val_score += pts
        val_detail["P/E"] = f"{_pe:.1f} → {pts}점"
    else:
        val_detail["P/E"] = "데이터 없음"

    # P/B (20점) — 섹터별 기준
    if pd.notna(price_to_book) and price_to_book > 0:
        pts = 0
        for threshold, score in _pb_tiers:
            if price_to_book <= threshold:
                pts = score
                break
        val_score += pts
        val_detail["P/B"] = f"{price_to_book:.2f} → {pts}점"
    else:
        val_detail["P/B"] = "데이터 없음"

    # EV/EBITDA 또는 EV/Sales (20점) — 섹터별 기준
    _ev_ebitda_tiers = [(15, 20), (25, 15), (40, 10), (60, 5)] if (_s_tech or _s_growth) else [(8, 20), (12, 15), (18, 10), (25, 5)]
    if pd.notna(ev_to_ebitda) and ev_to_ebitda > 0:
        pts = 0
        for threshold, score in _ev_ebitda_tiers:
            if ev_to_ebitda <= threshold:
                pts = score
                break
        val_score += pts
        val_detail["EV/EBITDA"] = f"{ev_to_ebitda:.1f} → {pts}점"
    elif pd.notna(ev_to_sales) and ev_to_sales > 0:
        _ev_sales_tiers = [(5, 20), (10, 12), (20, 5)] if (_s_tech or _s_growth) else [(2, 20), (5, 12), (10, 5)]
        pts = 0
        for threshold, score in _ev_sales_tiers:
            if ev_to_sales <= threshold:
                pts = score
                break
        val_score += pts
        val_detail["EV/Sales"] = f"{ev_to_sales:.1f} → {pts}점"
    else:
        val_detail["EV/EBITDA"] = "데이터 없음"

    # Graham 안전마진 (20점)
    _mos = margin_context.get("margin_of_safety")
    if pd.notna(_mos):
        if _mos >= 40:   pts = 20
        elif _mos >= 25: pts = 15
        elif _mos >= 10: pts = 8
        elif _mos >= 0:  pts = 3
        else:            pts = 0
        val_score += pts
        val_detail["Graham안전마진"] = f"{_mos:.1f}% → {pts}점"
    else:
        val_detail["Graham안전마진"] = "해당없음(성장주)"

    # FCF Yield (15점)
    if pd.notna(fcf_yield):
        if fcf_yield >= 8:   pts = 15
        elif fcf_yield >= 5: pts = 10
        elif fcf_yield >= 3: pts = 6
        elif fcf_yield > 0:  pts = 3
        else:                pts = 0
        val_score += pts
        val_detail["FCF_Yield"] = f"{fcf_yield:.1f}% → {pts}점"
    else:
        val_detail["FCF_Yield"] = "데이터 없음"

    # ══════════════════════════════════════════════════════════════════
    # 3. 장기 우량주 점수 (100점)
    # ══════════════════════════════════════════════════════════════════
    quality_score = 0
    quality_detail = {}

    # ROE (25점)
    if pd.notna(roe):
        roe_pct = roe * 100 if abs(roe) <= 1 else roe
        if roe_pct >= 25:   pts = 25
        elif roe_pct >= 20: pts = 20
        elif roe_pct >= 15: pts = 14
        elif roe_pct >= 10: pts = 7
        elif roe_pct > 0:   pts = 3
        else:               pts = 0
        quality_score += pts
        quality_detail["ROE"] = f"{roe_pct:.1f}% → {pts}점"
    else:
        quality_detail["ROE"] = "데이터 없음"

    # Operating Margin (20점)
    if pd.notna(op_margin):
        om_pct = op_margin * 100 if abs(op_margin) <= 1 else op_margin
        if om_pct >= 25:   pts = 20
        elif om_pct >= 15: pts = 15
        elif om_pct >= 10: pts = 10
        elif om_pct >= 5:  pts = 5
        elif om_pct > 0:   pts = 2
        else:              pts = 0
        quality_score += pts
        quality_detail["영업이익률"] = f"{om_pct:.1f}% → {pts}점"
    else:
        quality_detail["영업이익률"] = "데이터 없음"

    # D/E 건전성 (20점)
    if pd.notna(debt_to_eq):
        if debt_to_eq <= 30:    pts = 20
        elif debt_to_eq <= 50:  pts = 15
        elif debt_to_eq <= 100: pts = 8
        elif debt_to_eq <= 200: pts = 3
        else:                   pts = 0
        quality_score += pts
        quality_detail["부채비율(D/E)"] = f"{debt_to_eq:.1f} → {pts}점"
    else:
        quality_detail["부채비율(D/E)"] = "데이터 없음"

    # FCF 양수 (20점)
    _fcf = to_float(margin_context.get("core_fcf_pass") and 1) or np.nan
    _fcf_raw = fcf_val if pd.notna(fcf_val) else np.nan
    if pd.notna(_fcf_raw):
        if _fcf_raw > 0:
            # FCF 규모에 따라 차등
            if pd.notna(market_cap) and market_cap > 0:
                fcf_ratio = _fcf_raw / market_cap * 100
                if fcf_ratio >= 5:   pts = 20
                elif fcf_ratio >= 3: pts = 15
                elif fcf_ratio >= 1: pts = 10
                else:                pts = 7
            else:
                pts = 10
        else:
            pts = 0
        quality_score += pts
        quality_detail["FCF"] = f"{'양수' if _fcf_raw > 0 else '음수'} → {pts}점"
    else:
        quality_detail["FCF"] = "데이터 없음"

    # 배당 지속성 (15점)
    if pd.notna(dividend_yld) and dividend_yld > 0:
        dy_pct = dividend_yld * 100 if dividend_yld <= 1 else dividend_yld
        if dy_pct >= 4:    pts = 15
        elif dy_pct >= 2:  pts = 10
        elif dy_pct >= 1:  pts = 6
        else:              pts = 3
        quality_score += pts
        quality_detail["배당수익률"] = f"{dy_pct:.2f}% → {pts}점"
    else:
        pts = 5  # 무배당이어도 성장 가능성으로 부분 점수
        quality_score += pts
        quality_detail["배당수익률"] = f"무배당 → {pts}점(성장주 보정)"

    # ── 등급 판정 ──────────────────────────────────────────────────────
    def _grade(score):
        if score >= 75:   return "🟢 우수", "#16a34a"
        elif score >= 55: return "🟡 양호", "#ca8a04"
        elif score >= 35: return "🟠 보통", "#ea580c"
        else:             return "🔴 미흡", "#dc2626"

    cs_grade,  cs_color  = _grade(cs_score)
    val_grade, val_color = _grade(val_score)
    q_grade,   q_color   = _grade(quality_score)

    # 주도 스타일 판정
    scores = {
        "CAN SLIM 성장주": cs_score,
        "가치투자": val_score,
        "장기 우량주": quality_score,
    }
    dominant = max(scores, key=scores.get)

    return {
        "canslim": {"score": cs_score, "grade": cs_grade, "color": cs_color, "detail": cs_detail},
        "value":   {"score": val_score, "grade": val_grade, "color": val_color, "detail": val_detail},
        "quality": {"score": quality_score, "grade": q_grade, "color": q_color, "detail": quality_detail},
        "dominant": dominant,
        "scores": scores,
    }


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_style_scores(ticker_upper: str, _margin_context_key: str, _kpi_hash: str):
    """style score 캐싱용 래퍼 — margin_context는 JSON key로 전달."""
    return None  # 실제 계산은 렌더링 시점에 직접 호출


def detect_quote_type(ticker_symbol):
    try:
        tk_upper = str(ticker_symbol).strip().upper()
        p = _fmp_profile(tk_upper)
        quote_type = "ETF" if p.get("isEtf") else str(p.get("quoteType") or "EQUITY").upper()
        return quote_type, None, p
    except Exception:
        return "", None, {}


def get_etf_top_holdings(ticker_obj):
    try:
        funds_data = getattr(ticker_obj, "funds_data", None)
        if funds_data is not None:
            top_holdings = getattr(funds_data, "top_holdings", None)
            if isinstance(top_holdings, pd.DataFrame) and not top_holdings.empty:
                return top_holdings.copy()
    except Exception:
        pass
    return pd.DataFrame()


def _extract_holdings_table(top_holdings_df):
    if top_holdings_df is None or top_holdings_df.empty:
        return pd.DataFrame(columns=["Ticker", "Weight(%)"])

    df = top_holdings_df.copy()
    lower_cols = {str(c).strip().lower(): c for c in df.columns}

    ticker_col = None
    for key in ["symbol", "ticker", "holding", "holdings"]:
        if key in lower_cols:
            ticker_col = lower_cols[key]
            break

    if ticker_col is None:
        tickers = pd.Series(df.index, index=df.index)
    else:
        tickers = df[ticker_col]

    weight_col = None
    for key in lower_cols:
        if "weight" in key or "percent" in key or "%" in key:
            weight_col = lower_cols[key]
            break

    if weight_col is None and len(df.columns) > 0:
        weight_col = df.columns[-1]

    weights_raw = df[weight_col] if weight_col is not None else pd.Series(np.nan, index=df.index)
    weights = pd.to_numeric(weights_raw.astype(str).str.replace("%", "", regex=False), errors="coerce")

    # source may be ratio(0~1) or already percent(0~100)
    if weights.notna().any() and weights.dropna().max() <= 1.5:
        weights = weights * 100.0

    out = pd.DataFrame({"Ticker": tickers, "Weight(%)": weights})
    out["Ticker"] = out["Ticker"].astype(str).str.strip().str.upper()
    out = out[out["Ticker"].ne("") & out["Ticker"].ne("NAN")]
    out = out.drop_duplicates(subset=["Ticker"], keep="first").reset_index(drop=True)
    return out


def build_etf_holdings_universe(ticker_obj):
    top_holdings_df = get_etf_top_holdings(ticker_obj)
    return _extract_holdings_table(top_holdings_df)


def build_etf_holdings_performance(holdings_df):
    if holdings_df is None or holdings_df.empty:
        return pd.DataFrame(columns=["Ticker", "현재가", "1개월(%)", "3개월(%)", "6개월(%)", "12개월(%)", "비중(%)"])

    tickers = holdings_df["Ticker"].dropna().astype(str).str.upper().tolist()
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return pd.DataFrame(columns=["Ticker", "현재가", "1개월(%)", "3개월(%)", "6개월(%)", "12개월(%)", "비중(%)"])

    try:
        close_df = _fmp_batch_to_close_df(tickers, limit=500)
    except Exception:
        close_df = pd.DataFrame()
    if close_df.empty:
        return pd.DataFrame(columns=["Ticker", "현재가", "1개월(%)", "3개월(%)", "6개월(%)", "12개월(%)", "비중(%)"])

    if len(tickers) == 1 and "SINGLE" in close_df.columns:
        close_df.columns = [tickers[0]]

    weight_map = holdings_df.set_index("Ticker")["Weight(%)"].to_dict()
    rows = []
    for t in tickers:
        series = close_df[t] if t in close_df.columns else pd.Series(dtype=float)
        clean = pd.to_numeric(series, errors="coerce").dropna()
        cur = float(clean.iloc[-1]) if not clean.empty else np.nan
        rows.append(
            {
                "Ticker": t,
                "현재가": cur,
                "1개월(%)": calculate_period_return(series, 21),
                "3개월(%)": calculate_period_return(series, 63),
                "6개월(%)": calculate_period_return(series, 126),
                "12개월(%)": calculate_period_return(series, 252),
                "비중(%)": pd.to_numeric(weight_map.get(t, np.nan), errors="coerce"),
            }
        )

    return pd.DataFrame(rows)


def one_month_total_return_pct(close_series):
    """Return % over the span of a downloaded history (e.g. period='1mo')."""
    clean = pd.to_numeric(close_series, errors="coerce").dropna()
    if len(clean) < 2:
        return np.nan
    return (float(clean.iloc[-1]) / float(clean.iloc[0]) - 1.0) * 100.0


@st.cache_data(ttl=60)
def _portfolio_sheet_all_values_cached():
    try:
        ws, err = open_portfolios_worksheet()
        if err or ws is None:
            return None, err
        return (ws.get_all_values(), None)
    except Exception as exc:
        return None, str(exc)


def _invalidate_portfolio_sheet_cache():
    try:
        _portfolio_sheet_all_values_cached.clear()
    except Exception:
        pass


def distinct_portfolio_accounts_for_user_id(user_id: str) -> list[str]:
    """시트에서 해당 ID의 고유 Account 이름 목록(정렬)."""
    uid = str(user_id or "").strip()
    if not uid:
        return []
    vals, err = _portfolio_sheet_all_values_cached()
    if err or not vals or len(vals) < 2:
        return []
    hk = _portfolio_sheet_header_kind(vals[0])
    if hk == "unknown":
        hk = "new"
    uid_u = uid.upper()
    seen = set()
    out = []
    for r in vals[1:]:
        cells = _portfolio_row_to_new_six_cells(hk, r)
        if not cells:
            continue
        if str(cells[0]).strip().upper() != uid_u:
            continue
        a = str(cells[1]).strip()
        if not a:
            continue
        ak = a.lower()
        if ak not in seen:
            seen.add(ak)
            out.append(a)
    return sorted(out, key=lambda s: s.lower())


def delete_portfolio_sheet_row(user_id: str, account: str, ticker: str) -> tuple[bool, str]:
    """Portfolios 시트에서 ID·Account·Ticker가 모두 일치하는 행을 삭제한다."""
    uid = str(user_id or "").strip()
    acct = str(account or "").strip()
    tku = str(ticker or "").strip().upper()
    if not uid or not acct or not tku:
        return False, "ID, 계좌명, 티커를 모두 확인해 주세요."
    ws, err = open_portfolios_worksheet()
    if err:
        return False, err
    try:
        _invalidate_portfolio_sheet_cache()
        vals = ws.get_all_values() or []
        hk = _portfolio_sheet_header_kind(vals[0])
        if hk == "unknown":
            hk = "new"
        for i, r in enumerate(vals[1:], start=2):
            cells = _portfolio_row_to_new_six_cells(hk, r)
            if not cells:
                continue
            if str(cells[0]).strip().upper() != uid.upper():
                continue
            if str(cells[1]).strip().lower() != acct.lower():
                continue
            if str(cells[2]).strip().upper() != tku:
                continue
            ws.delete_rows(i)
            _invalidate_portfolio_sheet_cache()
            return True, ""
        return False, "시트에서 해당 행을 찾을 수 없습니다."
    except Exception as exc:
        return False, str(exc)


def replace_user_portfolio_sheet_rows(user_id: str, df: pd.DataFrame) -> tuple[bool, str]:
    """해당 user_id 행만 시트에서 교체하고 다른 사용자 행은 유지한다."""
    uid = str(user_id or "").strip()
    if not uid:
        return False, "user_id 가 비어 있습니다."
    ws, err = open_portfolios_worksheet()
    if err:
        return False, err
    try:
        _invalidate_portfolio_sheet_cache()
        ensure_portfolios_header_row(ws)
        vals = ws.get_all_values() or []
        header = _PORTFOLIOS_SHEET_COLS
        others = []
        uid_u = uid.upper()
        hk = _portfolio_sheet_header_kind(vals[0])
        if hk == "unknown":
            hk = "new"
        for r in vals[1:]:
            cells = _portfolio_row_to_new_six_cells(hk, r)
            if not cells:
                continue
            if str(cells[0]).strip().upper() == uid_u:
                continue
            others.append(cells)
        rows = [header] + others
        now_s = _narrative_now_et_string()
        for _, row in df.iterrows():
            acct = str(row.get("Account", "") or "").strip()
            tk = str(row.get("Ticker", "")).strip().upper()
            if not acct or not tk:
                continue
            pp = pd.to_numeric(row.get("Purchase_Price"), errors="coerce")
            qq = pd.to_numeric(row.get("Quantity"), errors="coerce")
            if pd.isna(pp) or pd.isna(qq):
                continue
            rows.append([uid, acct, tk, float(pp), float(qq), now_s])
        ws.clear()
        ws.update(rows, range_name=f"A1:F{len(rows)}", value_input_option="USER_ENTERED")
        _invalidate_portfolio_sheet_cache()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def load_portfolio():
    base_columns = ["Account", "Ticker", "Purchase_Price", "Quantity"]
    uid = str(st.session_state.get("user_id") or "").strip()
    if not uid:
        return pd.DataFrame(columns=base_columns)
    st.session_state.pop("_portfolio_last_sheet_error", None)
    vals, err = _portfolio_sheet_all_values_cached()
    if err:
        st.session_state["_portfolio_last_sheet_error"] = err
        return pd.DataFrame(columns=base_columns)
    if not vals or len(vals) < 2:
        return pd.DataFrame(columns=base_columns)
    hk0 = _portfolio_sheet_header_kind(vals[0])
    if hk0 not in ("new", "legacy"):
        try:
            ws2, err2 = open_portfolios_worksheet()
            if not err2 and ws2:
                ensure_portfolios_header_row(ws2)
                _invalidate_portfolio_sheet_cache()
                vals, err = _portfolio_sheet_all_values_cached()
        except Exception:
            pass
        if err or not vals or len(vals) < 2:
            return pd.DataFrame(columns=base_columns)
    hk = _portfolio_sheet_header_kind(vals[0])
    if hk == "unknown":
        hk = "new"
    rows = []
    uid_u = uid.upper()
    for r in vals[1:]:
        cells = _portfolio_row_to_new_six_cells(hk, r)
        if not cells:
            continue
        if str(cells[0]).strip().upper() != uid_u:
            continue
        rows.append(
            {
                "Account": str(cells[1]).strip(),
                "Ticker": str(cells[2]).strip().upper(),
                "Purchase_Price": pd.to_numeric(cells[3], errors="coerce"),
                "Quantity": pd.to_numeric(cells[4], errors="coerce"),
                "Date_Added": str(cells[5]).strip() if len(cells) > 5 else "",
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=base_columns + ["Date_Added"])
    df["Quantity"] = df["Quantity"].fillna(1.0)
    df = df[df["Ticker"].ne("") & df["Ticker"].ne("NAN") & df["Account"].astype(str).str.strip().ne("")]
    df = df.drop_duplicates(subset=["Account", "Ticker"], keep="last").reset_index(drop=True)
    _out_cols = base_columns + (["Date_Added"] if "Date_Added" in df.columns else [])
    return df[_out_cols].copy()


def save_portfolio(df):
    uid = str(st.session_state.get("user_id") or "").strip()
    if not uid:
        return
    base_columns = ["Account", "Ticker", "Purchase_Price", "Quantity"]
    safe_df = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame(columns=base_columns)
    for col in base_columns:
        if col not in safe_df.columns:
            safe_df[col] = np.nan
    safe_df = safe_df[base_columns].copy()
    safe_df["Account"] = safe_df["Account"].astype(str).str.strip()
    safe_df["Ticker"] = safe_df["Ticker"].astype(str).str.strip().str.upper()
    safe_df["Purchase_Price"] = pd.to_numeric(safe_df["Purchase_Price"], errors="coerce")
    safe_df["Quantity"] = pd.to_numeric(safe_df["Quantity"], errors="coerce")
    safe_df["Quantity"] = safe_df["Quantity"].fillna(1.0)
    if safe_df["Account"].eq("").any():
        try:
            st.error("계좌명(Account)이 비어 있는 행이 있습니다. 계좌명을 입력한 뒤 다시 저장해 주세요.")
        except Exception:
            pass
        return
    safe_df = safe_df[safe_df["Ticker"].ne("") & safe_df["Ticker"].ne("NAN")]
    safe_df = safe_df.drop_duplicates(subset=["Account", "Ticker"], keep="last").reset_index(drop=True)
    ok, msg = replace_user_portfolio_sheet_rows(uid, safe_df)
    if not ok:
        try:
            st.error(f"Portfolios 시트 저장 실패: {msg}")
        except Exception:
            pass


def _validate_positive_portfolio_number(label_kr, value):
    """수량·가격 검증: 숫자이며 0보다 커야 함."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return False, None, f"{label_kr}은(는) 숫자여야 합니다."
    if not np.isfinite(x):
        return False, None, f"{label_kr}은(는) 유효한 숫자여야 합니다."
    if x <= 0:
        return False, None, f"{label_kr}은(는) 0보다 커야 합니다."
    return True, x, ""


# ──────────────────────────────────────────────────────────────────────────────
# DRG_Predictions 시트 — AI 예측 저장 & 검증
# ──────────────────────────────────────────────────────────────────────────────

# 섹터 필터 → 벤치마크 ETF 매핑
_SECTOR_BENCHMARK_ETF = {
    "전체": "SPY",
    "테크/반도체": "SOXX",
    "에너지": "XLE",
    "금융": "XLF",
    "헬스케어": "XLV",
    "산업재": "XLI",
    "소비재": "XLY",
    "부동산": "XLRE",
}


def open_drg_predictions_worksheet():
    """Quant_DB / DRG_Predictions 탭. 없으면 자동 생성."""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
    except Exception as exc:
        return None, f"스프레드시트 접근 실패: {exc}"
    # 탭 열기 시도 → 실패하면 무조건 생성
    try:
        ws = sh.worksheet(_DRG_PREDICTIONS_WORKSHEET_TITLE)
        return ws, None
    except Exception:
        pass
    try:
        ncols = len(_DRG_PREDICTIONS_SHEET_COLS)
        ws = sh.add_worksheet(title=_DRG_PREDICTIONS_WORKSHEET_TITLE, rows=2000, cols=ncols)
        ws.update([_DRG_PREDICTIONS_SHEET_COLS],
                  range_name=f"A1:{chr(64+ncols)}1",
                  value_input_option="USER_ENTERED")
        return ws, None
    except Exception as exc2:
        return None, f"DRG_Predictions 시트 생성 실패: {exc2}"


@st.cache_data(ttl=300)
def _drg_predictions_all_values_cached():
    ws, err = open_drg_predictions_worksheet()
    if err or ws is None:
        return [], err
    try:
        return ws.get_all_values(), None
    except Exception as exc:
        return [], str(exc)


def _invalidate_drg_predictions_cache():
    _drg_predictions_all_values_cached.clear()


def load_drg_predictions(user_id: str) -> pd.DataFrame:
    """DRG_Predictions 시트에서 해당 user_id 행만 로드. 캐시 우회, 시트 직접 읽기."""
    empty = pd.DataFrame(columns=_DRG_PREDICTIONS_SHEET_COLS)
    if not user_id:
        return empty
    # 캐시 우회 — 항상 시트 직접 읽기
    try:
        ws, err = open_drg_predictions_worksheet()
        if err or ws is None:
            return empty
        rows = ws.get_all_values()
    except Exception:
        return empty
    if not rows or len(rows) < 2:
        return empty
    try:
        header = [str(c).strip().lower() for c in rows[0]]
        df = pd.DataFrame(rows[1:], columns=header)
    except Exception:
        return empty
    col_map = {c: s for c in df.columns for s in _DRG_PREDICTIONS_SHEET_COLS if c == s.lower()}
    df = df.rename(columns=col_map)
    for c in _DRG_PREDICTIONS_SHEET_COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[df["user_id"].astype(str).str.strip() == str(user_id).strip()]
    return df[_DRG_PREDICTIONS_SHEET_COLS].copy().reset_index(drop=True)


def save_drg_prediction(user_id: str, pred_date: str, direction: str,
                         sector_filter: str, benchmark_etf: str,
                         spy_close: float, full_text: str) -> tuple[bool, str]:
    """예측 결과를 DRG_Predictions 시트에 저장."""
    ws, err = open_drg_predictions_worksheet()
    if err or ws is None:
        return False, err or "시트를 열 수 없습니다."
    try:
        row = [
            str(user_id).strip(),
            str(pred_date),
            str(direction),
            str(sector_filter),
            str(benchmark_etf),
            str(round(float(spy_close), 4)) if spy_close and not np.isnan(float(spy_close)) else "",
            str(full_text).strip(),
            "",   # actual_direction (나중에 업데이트)
            "",   # actual_return_pct
            "",   # is_correct
            "",   # review_comment
        ]
        _safe_append_rows(ws, row, value_input_option="USER_ENTERED")
        _invalidate_drg_predictions_cache()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def update_drg_prediction_result(user_id: str, pred_date: str,
                                  actual_direction: str, actual_return_pct: float,
                                  is_correct: str, review_comment: str) -> tuple[bool, str]:
    """예측 행의 실제 결과 컬럼을 업데이트."""
    ws, err = open_drg_predictions_worksheet()
    if err or ws is None:
        return False, err or "시트를 열 수 없습니다."
    try:
        rows = ws.get_all_values()
        if not rows or len(rows) < 2:
            return False, "데이터 없음"
        header = [str(c).strip().lower() for c in rows[0]]
        uid_idx = header.index("user_id") if "user_id" in header else 0
        date_idx = header.index("pred_date") if "pred_date" in header else 1
        actual_dir_idx = header.index("actual_direction") if "actual_direction" in header else 7
        actual_ret_idx = header.index("actual_return_pct") if "actual_return_pct" in header else 8
        correct_idx = header.index("is_correct") if "is_correct" in header else 9
        comment_idx = header.index("review_comment") if "review_comment" in header else 10

        for i, row in enumerate(rows[1:], start=2):
            if (len(row) > uid_idx and str(row[uid_idx]).strip() == str(user_id).strip() and
                    len(row) > date_idx and str(row[date_idx]).strip() == str(pred_date).strip()):
                # 4번 개별 호출 → batch_update 1회로 합산 (API 호출 4→1)
                def _col(idx):
                    return chr(64 + idx + 1)
                ws.batch_update([
                    {"range": f"{_col(actual_dir_idx)}{i}",  "values": [[actual_direction]]},
                    {"range": f"{_col(actual_ret_idx)}{i}",  "values": [[str(round(actual_return_pct, 4))]]},
                    {"range": f"{_col(correct_idx)}{i}",     "values": [[is_correct]]},
                    {"range": f"{_col(comment_idx)}{i}",     "values": [[review_comment]]},
                ], value_input_option="USER_ENTERED")
                _invalidate_drg_predictions_cache()
                return True, ""
        return False, f"{pred_date} 예측 행을 찾을 수 없습니다."
    except Exception as exc:
        return False, str(exc)


def verify_drg_prediction(pred_row: pd.Series) -> tuple[str, float, str]:
    """
    예측 행에서 실제 결과를 계산.
    로직: 예측일(pred_date) 당일 종가 vs 직전 거래일 종가 비교.
    → "내일 시장 방향" 예측이므로 예측일 당일 장이 마감되면 즉시 검증 가능.
    반환: (actual_direction, actual_return_pct, is_correct)
    """
    try:
        bench_etf = str(pred_row.get("benchmark_etf", "SPY") or "SPY").strip().upper()
        pred_date_str = str(pred_row.get("pred_date", "")).strip()
        pred_date = pd.to_datetime(pred_date_str, errors="coerce")
        if pd.isna(pred_date):
            return "", np.nan, ""

        pred_date_naive = pred_date.date()

        # FMP로 예측일 전후 20일치 데이터 조회
        hist = _fmp_price_history(bench_etf, limit=20)
        if hist is None or hist.empty:
            return "", np.nan, ""

        # timezone 제거 후 date 비교
        hist.index = pd.to_datetime(hist.index).tz_localize(None)
        hist_dates = hist.index.normalize()

        pred_ts = pd.Timestamp(pred_date_naive)

        hist_on_pred = hist[hist_dates == pred_ts]
        hist_before  = hist[hist_dates < pred_ts]

        if hist_on_pred.empty:
            # 예측일 데이터 없음 = 휴장일이거나 아직 장 마감 전
            return "", np.nan, ""
        if hist_before.empty:
            return "", np.nan, ""

        # Close 컬럼 처리 (MultiIndex 대응)
        def _get_close(df):
            if isinstance(df.columns, pd.MultiIndex):
                return pd.to_numeric(df["Close"].iloc[:, 0], errors="coerce").dropna()
            return pd.to_numeric(df["Close"], errors="coerce").dropna()

        pred_close_s = _get_close(hist_on_pred)
        prev_close_s = _get_close(hist_before)

        if pred_close_s.empty or prev_close_s.empty:
            return "", np.nan, ""

        pred_day_close = float(pred_close_s.iloc[-1])
        prev_close     = float(prev_close_s.iloc[-1])

        if prev_close <= 0:
            return "", np.nan, ""

        ret_pct = (pred_day_close / prev_close - 1.0) * 100.0

        # 방향 판정 (±0.7% 기준 — 그 안쪽은 사실상 보합이므로 '중립')
        if ret_pct >= 0.7:
            actual_dir = "상승"
        elif ret_pct <= -0.7:
            actual_dir = "하락"
        else:
            actual_dir = "중립"

        # 예측 방향 추출
        pred_dir = str(pred_row.get("direction", "")).strip()
        pred_dir_norm = "상승" if "상승" in pred_dir else ("하락" if "하락" in pred_dir else "중립")

        is_correct = "✅ 적중" if pred_dir_norm == actual_dir else "❌ 빗나감"
        return actual_dir, ret_pct, is_correct
    except Exception:
        return "", np.nan, ""


# ──────────────────────────────────────────────────────────────────────────────
# Trade_History 시트 — 매수/매도 기록 CRUD
# ──────────────────────────────────────────────────────────────────────────────

def open_trade_history_worksheet():
    """Quant_DB / Trade_History 탭. 없으면 자동 생성."""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        ws = sh.worksheet(_TRADE_HISTORY_WORKSHEET_TITLE)
        return ws, None
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg or "unable to find" in msg:
            try:
                sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
                ws = sh.add_worksheet(title=_TRADE_HISTORY_WORKSHEET_TITLE, rows=5000, cols=8)
                ws.update([_TRADE_HISTORY_SHEET_COLS], range_name="A1:H1", value_input_option="USER_ENTERED")
                return ws, None
            except Exception as exc2:
                return None, f"Trade_History 시트를 만들 수 없습니다: {exc2}"
        return None, f"Trade_History 시트를 열 수 없습니다: {exc}"


@st.cache_data(ttl=60)
def _trade_history_all_values_cached():
    ws, err = open_trade_history_worksheet()
    if err or ws is None:
        return [], err
    try:
        return ws.get_all_values(), None
    except Exception as exc:
        return [], str(exc)


def _invalidate_trade_history_cache():
    _trade_history_all_values_cached.clear()


def load_trade_history(user_id: str) -> pd.DataFrame:
    """Trade_History 시트에서 해당 user_id 행만 로드."""
    empty = pd.DataFrame(columns=_TRADE_HISTORY_SHEET_COLS)
    if not user_id:
        return empty
    rows, err = _trade_history_all_values_cached()
    if err or not rows:
        return empty

    # 헤더가 없거나 비어있으면 자동으로 표준 헤더로 간주
    first_row_lower = [str(c).strip().lower() for c in rows[0]]
    has_valid_header = any(c in _TRADE_HISTORY_SHEET_COLS for c in first_row_lower)

    if not has_valid_header or len(rows) < 2:
        # 헤더 없이 데이터만 있는 경우 — 컬럼 수로 맞추기
        data_rows = rows if not has_valid_header else rows[1:]
        if not data_rows:
            return empty
        try:
            ncols = max(len(r) for r in data_rows)
            cols = _TRADE_HISTORY_SHEET_COLS[:ncols]
            df = pd.DataFrame([r[:ncols] for r in data_rows], columns=cols)
        except Exception:
            return empty
    else:
        try:
            df = pd.DataFrame(rows[1:], columns=first_row_lower)
        except Exception:
            return empty
        # 컬럼 정규화 (대소문자 불일치 보정)
        col_map = {c: s for c in df.columns for s in _TRADE_HISTORY_SHEET_COLS if c == s.lower()}
        df = df.rename(columns=col_map)

    for c in _TRADE_HISTORY_SHEET_COLS:
        if c not in df.columns:
            df[c] = ""

    df = df[df["user_id"].astype(str).str.strip() == str(user_id).strip()]
    df = df[_TRADE_HISTORY_SHEET_COLS].copy().reset_index(drop=True)
    df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    return df


def append_trade_history_row(user_id: str, account: str, ticker: str, action: str,
                              shares: float, price: float, date: str, memo: str = "") -> tuple[bool, str]:
    """Trade_History 시트에 한 행 추가."""
    ws, err = open_trade_history_worksheet()
    if err or ws is None:
        return False, err or "시트를 열 수 없습니다."
    try:
        row = [
            str(user_id).strip(),
            str(account).strip(),
            str(ticker).strip().upper(),
            str(action).strip().upper(),
            str(round(float(shares), 6)),
            str(round(float(price), 6)),
            str(date).strip(),
            str(memo).strip(),
        ]
        _safe_append_rows(ws, row, value_input_option="USER_ENTERED")
        _invalidate_trade_history_cache()
        return True, ""
    except Exception as exc:
        return False, str(exc)


# ──────────────────────────────────────────────────────────────────────────────
# 실현 손익 계산 (FIFO + 평균단가)
# ──────────────────────────────────────────────────────────────────────────────

def compute_realized_pnl(trade_df: pd.DataFrame) -> pd.DataFrame:
    """
    BUY/SELL 기록에서 종목·계좌별 실현 손익 계산.
    - FIFO: 먼저 매수한 lot부터 소진
    - 평균단가: 전체 매수 평균가 기준
    """
    _cols = ["ticker", "account", "sell_date", "shares_sold", "sell_price",
             "fifo_cost", "avg_cost", "fifo_pnl", "avg_pnl", "fifo_pnl_pct", "avg_pnl_pct", "memo"]
    if trade_df is None or trade_df.empty:
        return pd.DataFrame(columns=_cols)

    results = []
    for (ticker, account), grp in trade_df.groupby(["ticker", "account"]):
        grp = grp.copy()
        grp["date"] = pd.to_datetime(grp["date"], errors="coerce")
        grp = grp.sort_values("date").reset_index(drop=True)
        fifo_queue = []          # [[shares, price], ...]
        avg_total_shares = 0.0
        avg_total_cost = 0.0

        for _, row in grp.iterrows():
            act = str(row.get("action", "")).strip().upper()
            sh = float(row.get("shares") or 0)
            pr = float(row.get("price") or 0)
            dt = row.get("date")
            memo = str(row.get("memo") or "")

            if act == "BUY" and sh > 0 and pr > 0:
                fifo_queue.append([sh, pr])
                avg_total_cost += sh * pr
                avg_total_shares += sh

            elif act == "SELL" and sh > 0 and pr > 0:
                # FIFO 비용
                fifo_cost_total = 0.0
                remaining = sh
                temp_q = [list(q) for q in fifo_queue]
                for lot in temp_q:
                    if remaining <= 0:
                        break
                    use = min(lot[0], remaining)
                    fifo_cost_total += use * lot[1]
                    lot[0] -= use
                    remaining -= use
                fifo_queue = [[q[0], q[1]] for q in temp_q if q[0] > 1e-9]

                # 평균단가 비용
                avg_per = (avg_total_cost / avg_total_shares) if avg_total_shares > 1e-9 else 0.0
                avg_cost_total = sh * avg_per
                avg_total_cost = max(0.0, avg_total_cost - avg_cost_total)
                avg_total_shares = max(0.0, avg_total_shares - sh)
                if avg_total_shares < 1e-9:
                    avg_total_shares = avg_total_cost = 0.0

                proceeds = sh * pr
                fifo_pnl = proceeds - fifo_cost_total
                avg_pnl = proceeds - avg_cost_total
                fifo_cost_per = fifo_cost_total / sh if sh > 0 else 0
                avg_cost_per = avg_cost_total / sh if sh > 0 else 0
                fifo_pct = ((pr / fifo_cost_per) - 1.0) * 100.0 if fifo_cost_per > 0 else np.nan
                avg_pct = ((pr / avg_cost_per) - 1.0) * 100.0 if avg_cost_per > 0 else np.nan

                results.append({
                    "ticker": ticker, "account": account,
                    "sell_date": dt.strftime("%Y-%m-%d") if pd.notna(dt) else "",
                    "shares_sold": sh, "sell_price": pr,
                    "fifo_cost": fifo_cost_per, "avg_cost": avg_cost_per,
                    "fifo_pnl": fifo_pnl, "avg_pnl": avg_pnl,
                    "fifo_pnl_pct": fifo_pct, "avg_pnl_pct": avg_pct, "memo": memo,
                })

    if not results:
        return pd.DataFrame(columns=_cols)
    return pd.DataFrame(results).sort_values("sell_date", ascending=False).reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────────────────
# 매도 타이밍 기술적 신호 계산
# ──────────────────────────────────────────────────────────────────────────────

# 트레일링 스톱 기본값: 보유 기간 고점 대비 이 % 이상 하락 시 경보
_DEFAULT_TRAILING_STOP_PCT = 15.0


def integrated_sell_verdict(
    *,
    above_ma200,
    one_month_return,
    rsi,
    macd_signal,
    pct_from_52w_high,
    drawdown_from_high_pct,
    trailing_stop_pct: float = _DEFAULT_TRAILING_STOP_PCT,
) -> tuple[str, str]:
    """200일선 + 선행신호(RSI·MACD·52주 이격) + 트레일링 스톱을 종합한 매도 판정.

    기존 매도 레이더는 '현재가 < 200일선'만 봐서 신호가 늦었다.
    여기서는 추세 붕괴(후행)와 과열·꺾임(선행), 그리고 고점 대비 하락(트레일링)을
    한 점수로 합쳐 🟢보유 / 🟡일부 익절 검토 / 🔴매도 3단계로 반환한다.
    반환: (status_label, reason_text)
    """
    reasons = []
    score = 0

    # ── 후행: 추세 붕괴 (가장 무거움) ───────────────────────────────
    if above_ma200 is False:
        score += 4
        reasons.append("200일선 이탈(추세 붕괴)")

    # ── 트레일링 스톱: 보유 고점 대비 하락 ──────────────────────────
    if pd.notna(drawdown_from_high_pct) and drawdown_from_high_pct <= -abs(trailing_stop_pct):
        score += 3
        reasons.append(f"고점 대비 {drawdown_from_high_pct:.0f}% 하락(트레일링 스톱 -{abs(trailing_stop_pct):.0f}%)")

    # ── 선행: 모멘텀 꺾임 ──────────────────────────────────────────
    if macd_signal == "DEAD_CROSS":
        score += 2
        reasons.append("MACD 데드크로스(추세 꺾임)")
    elif macd_signal == "BELOW_SIGNAL":
        score += 1

    if pd.notna(one_month_return) and one_month_return < 0:
        score += 1
        reasons.append(f"1개월 {one_month_return:+.1f}%")

    # ── 선행: 과열 (신고가 부근 + RSI 과매수) ──────────────────────
    overheated = (
        pd.notna(rsi) and rsi > 70
        and pd.notna(pct_from_52w_high) and pct_from_52w_high > -3
    )
    if overheated:
        score += 1
        reasons.append(f"RSI {rsi:.0f} 과열 + 신고가 부근(단기 조정 위험)")

    # ── 판정 ───────────────────────────────────────────────────────
    if score >= 4:
        label = "🔴 매도 (SELL)"
    elif score >= 2:
        label = "🟡 일부 익절 검토"
    else:
        label = "✅ 보유 (HOLD)"
        if not reasons:
            reasons.append("추세 유지 · 이상 신호 없음")

    return label, " · ".join(reasons)


def suggest_stop_and_target(ticker: str, entry_price: float = None) -> dict:
    """진입가 기준 손절·목표가 '제안값'을 계산해 반환(확정은 사용자 몫).

    두 가지 손절 기준을 함께 제시한다:
      - ATR 기반: entry - 2×ATR(14)  (변동성 큰 종목일수록 손절을 멀리)
      - 200일선 기반: 200일 이동평균 바로 아래 2%
    목표가는 ATR 손절 거리의 2배(손익비 1:2)를 기본 제안.
    계산 불가 항목은 np.nan.
    """
    out = {
        "entry": np.nan, "atr": np.nan,
        "stop_atr": np.nan, "stop_atr_pct": np.nan,
        "stop_ma200": np.nan, "stop_ma200_pct": np.nan,
        "target_2r": np.nan, "target_2r_pct": np.nan,
    }
    try:
        hist = cached_timing_price_history(str(ticker).strip().upper())
        if hist is None or hist.empty or "Close" not in hist.columns:
            return out
        close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
        if close.empty:
            return out
        entry = float(entry_price) if (entry_price is not None and pd.notna(entry_price)) else float(close.iloc[-1])
        out["entry"] = entry

        # ATR(14): True Range의 14일 평균
        atr = np.nan
        if all(c in hist.columns for c in ("High", "Low", "Close")):
            high = pd.to_numeric(hist["High"], errors="coerce")
            low = pd.to_numeric(hist["Low"], errors="coerce")
            prev_close = pd.to_numeric(hist["Close"], errors="coerce").shift(1)
            tr = pd.concat([
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ], axis=1).max(axis=1)
            if tr.notna().sum() >= 10:
                atr = float(tr.rolling(14, min_periods=10).mean().dropna().iloc[-1])
        out["atr"] = atr

        if pd.notna(atr) and atr > 0 and entry > 0:
            stop_atr = entry - 2.0 * atr
            out["stop_atr"] = stop_atr
            out["stop_atr_pct"] = (stop_atr / entry - 1.0) * 100.0
            risk = entry - stop_atr
            target = entry + 2.0 * risk  # 손익비 1:2
            out["target_2r"] = target
            out["target_2r_pct"] = (target / entry - 1.0) * 100.0

        # 200일선 기반 손절
        if len(close) >= 150:
            ma_ser = close.rolling(200, min_periods=150).mean().dropna()
            if not ma_ser.empty:
                ma200 = float(ma_ser.iloc[-1])
                if pd.notna(ma200) and ma200 > 0:
                    stop_ma = ma200 * 0.98
                    out["stop_ma200"] = stop_ma
                    if entry > 0:
                        out["stop_ma200_pct"] = (stop_ma / entry - 1.0) * 100.0
    except Exception:
        return out
    return out


def compute_position_drawdown(close_series, purchase_price, current_price, date_added: str = ""):
    """포지션의 '고점 대비 하락'을 보수적으로 계산 + 데이터 오류 플래그.

    선택 1C: (a) 매수일 이후 고점, (b) 매수가를 바닥선으로 본 고점 중
    더 보수적인(=덜 깊은 하락) 쪽을 채택해 허수 낙폭을 줄인다.
    선택 2B: 비현실적 낙폭(스플릿 미조정·스파이크)으로 의심되면 data_error=True.

    반환: (drawdown_pct, data_error: bool)
      - drawdown_pct: 0 이하의 % (예: -12.3). 계산 불가 시 np.nan
      - data_error:   True면 트레일링 경보/판정에서 낙폭을 신뢰하지 않음
    """
    try:
        if close_series is None or len(close_series) == 0 or pd.isna(current_price) or current_price <= 0:
            return np.nan, False

        # (a) 매수일 이후 고점
        high_since_buy = np.nan
        if date_added:
            try:
                buy_dt = pd.to_datetime(str(date_added)[:10], errors="coerce")
                if pd.notna(buy_dt) and isinstance(close_series.index, pd.DatetimeIndex):
                    idx = close_series.index
                    tz = getattr(idx, "tz", None)
                    if tz is not None and buy_dt.tzinfo is None:
                        buy_dt = buy_dt.tz_localize(tz)
                    sliced = close_series[close_series.index >= buy_dt]
                    if not sliced.empty:
                        high_since_buy = float(sliced.max())
            except Exception:
                high_since_buy = np.nan

        # (b) 1년 고점 (폴백/비교용)
        high_1y = float(close_series.max()) if len(close_series) else np.nan

        # 매수가 바닥선: 고점이 적어도 매수가보다는 높아야 '하락'이 의미 있음
        pp = float(purchase_price) if (purchase_price is not None and pd.notna(purchase_price) and purchase_price > 0) else np.nan

        # 채택할 고점 후보들: 매수일 이후 고점 우선, 없으면 1년 고점
        ref_high = high_since_buy if pd.notna(high_since_buy) else high_1y
        if pd.isna(ref_high) or ref_high <= 0:
            return np.nan, False

        # 1C: '매수일 이후 고점' 대비 낙폭을 기본으로 사용(트레일링 스톱의 본래 의미).
        #     단, 고점이 비현실적으로 높으면(스플릿 미조정 등) 매수가 기준으로 보정.
        dd_from_high = (current_price / ref_high - 1.0) * 100.0
        drawdown_pct = dd_from_high

        # 2B: 데이터 오류 의심 — 낙폭이 -70%보다 깊은데 현재 수익이 플러스면 모순,
        #     또는 참조고점이 현재가의 5배를 초과하면(스플릿 미조정 전형) 오류로 간주
        data_error = False
        if pd.notna(pp):
            in_profit = current_price >= pp
            if dd_from_high <= -70.0 and in_profit:
                data_error = True
        if ref_high > current_price * 5:
            data_error = True

        if data_error:
            # 오류 시 고점을 신뢰하지 않고 매수가 기준 낙폭만 사용(없으면 NaN)
            if pd.notna(pp):
                drawdown_pct = min(0.0, (current_price / pp - 1.0) * 100.0)
            else:
                drawdown_pct = np.nan

        return drawdown_pct, data_error
    except Exception:
        return np.nan, False


def _empty_sell_signal():
    return {
        "rsi": np.nan, "macd_signal": "N/A", "pct_from_52w_high": np.nan,
        "above_ma200": None, "ma200": np.nan, "current_price": np.nan,
        "signal_score": 0, "signal_label": "⚪ 데이터 없음",
    }


def compute_sell_signal_indicators(tickers: list) -> dict:
    """
    종목별 매도 타이밍 기술적 신호 계산.
    반환: {ticker: {rsi, macd_signal, pct_from_52w_high, above_ma200, signal_score, signal_label, ...}}
    """
    if not tickers:
        return {}
    tickers_tuple = tuple(sorted(set(str(t).upper() for t in tickers)))
    close_df = cached_portfolio_yf_close_1y(tickers_tuple)
    results = {}
    for ticker in tickers:
        try:
            if close_df is None or ticker not in close_df.columns:
                results[ticker] = _empty_sell_signal()
                continue
            series = pd.to_numeric(close_df[ticker], errors="coerce").dropna()
            if len(series) < 30:
                results[ticker] = _empty_sell_signal()
                continue

            # RSI (14일)
            delta = series.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = float((100 - 100 / (1 + rs)).iloc[-1])

            # MACD (12, 26, 9)
            ema12 = series.ewm(span=12, adjust=False).mean()
            ema26 = series.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            m_now, m_prev = float(macd_line.iloc[-1]), float(macd_line.iloc[-2]) if len(macd_line) > 1 else float(macd_line.iloc[-1])
            s_now, s_prev = float(signal_line.iloc[-1]), float(signal_line.iloc[-2]) if len(signal_line) > 1 else float(signal_line.iloc[-1])
            if m_prev >= s_prev and m_now < s_now:
                macd_signal = "DEAD_CROSS"
            elif m_now < s_now:
                macd_signal = "BELOW_SIGNAL"
            elif m_prev <= s_prev and m_now > s_now:
                macd_signal = "GOLDEN_CROSS"
            else:
                macd_signal = "ABOVE_SIGNAL"

            # 52주 고점 대비
            high_52w = float(series.rolling(252, min_periods=1).max().iloc[-1])
            current = float(series.iloc[-1])
            pct_from_high = ((current / high_52w) - 1.0) * 100.0 if high_52w > 0 else np.nan

            # 200일선
            ma200 = float(series.rolling(200, min_periods=200).mean().iloc[-1]) if len(series) >= 200 else np.nan
            above_ma200 = (current > ma200) if pd.notna(ma200) else None

            # 위험 점수 (높을수록 매도 위험)
            score = 0
            if pd.notna(rsi):
                if rsi > 75: score += 3
                elif rsi > 70: score += 2
                elif rsi > 65: score += 1
            if macd_signal == "DEAD_CROSS": score += 3
            elif macd_signal == "BELOW_SIGNAL": score += 1
            if pd.notna(pct_from_high):
                if pct_from_high > -3: score += 2
                elif pct_from_high > -7: score += 1
            if above_ma200 is False: score += 3

            label = "🔴 매도 검토" if score >= 6 else ("🟡 주의" if score >= 3 else "🟢 보유")
            results[ticker] = {
                "rsi": rsi, "macd_signal": macd_signal,
                "pct_from_52w_high": pct_from_high, "above_ma200": above_ma200,
                "ma200": ma200, "current_price": current,
                "signal_score": score, "signal_label": label,
            }
        except Exception:
            results[ticker] = _empty_sell_signal()
    return results


def build_portfolio_sell_radar_df(portfolio_df):
    _sell_radar_cols = [
        "계좌",
        "티커",
        "수량",
        "매수가",
        "현재가",
        "투자 손익($)",
        "수익률(%)",
        "SPY Alpha(%)",
        "Drawdown(%)",
        "200일선",
        "1개월 수익률",
        "자산 비중(%)",
        "유니버스 랭킹(Universe Rank)",
        "상태(Status)",
        "판정 근거(Why)",
    ]
    if portfolio_df is None or portfolio_df.empty:
        return pd.DataFrame(columns=_sell_radar_cols)

    clean_portfolio = portfolio_df.copy()
    clean_portfolio["Account"] = clean_portfolio["Account"].fillna("Default Account").astype(str).str.strip()
    clean_portfolio["Ticker"] = clean_portfolio["Ticker"].astype(str).str.strip().str.upper()
    clean_portfolio["Purchase_Price"] = pd.to_numeric(clean_portfolio["Purchase_Price"], errors="coerce")
    clean_portfolio["Quantity"] = pd.to_numeric(clean_portfolio["Quantity"], errors="coerce")
    clean_portfolio["Quantity"] = clean_portfolio["Quantity"].fillna(1.0)
    clean_portfolio = clean_portfolio.drop_duplicates(subset=["Account", "Ticker"], keep="last")

    clean_tickers = clean_portfolio["Ticker"].dropna().astype(str).tolist()
    clean_tickers = [t for t in clean_tickers if t]
    if not clean_tickers:
        return pd.DataFrame(columns=_sell_radar_cols)

    tickers_sorted_tuple = tuple(sorted(dict.fromkeys(clean_tickers)))
    # SPY 포함해서 한 번에 다운로드 (Alpha 계산용)
    tickers_with_spy = tuple(sorted(dict.fromkeys(list(clean_tickers) + ["SPY"])))
    close_df_full = cached_portfolio_yf_close_1y(tickers_with_spy)
    if close_df_full is None or close_df_full.empty:
        return pd.DataFrame(columns=_sell_radar_cols)
    close_df = close_df_full

    # SPY 1개월 수익률 (Alpha 기준선)
    spy_1m_return = np.nan
    try:
        if "SPY" in close_df_full.columns:
            spy_series = pd.to_numeric(close_df_full["SPY"], errors="coerce").dropna()
            spy_1m_return = calculate_period_return(spy_series, 21)
    except Exception:
        spy_1m_return = np.nan

    universe_list = read_etf_universe_file_tickers()
    universe_set = set(str(x).strip().upper() for x in universe_list if str(x).strip())
    universe_tuple = tuple(sorted(universe_set))
    rank_df = cached_etf_universe_rankings_full(universe_tuple)
    ticker_to_rank = {}
    if rank_df is not None and not rank_df.empty and "Ticker" in rank_df.columns and "Rank" in rank_df.columns:
        for _, rr in rank_df.iterrows():
            tk = str(rr.get("Ticker", "")).strip().upper()
            rk = pd.to_numeric(rr.get("Rank"), errors="coerce")
            if tk and pd.notna(rk):
                ticker_to_rank[tk] = int(rk)

    unique_holdings = sorted(dict.fromkeys(clean_tickers))
    quote_by_ticker = {t: cached_quote_type(t) for t in unique_holdings}

    # 선행 신호(RSI·MACD·52주 이격)를 매도 판정에 통합하기 위해 미리 계산
    sell_sig_map = compute_sell_signal_indicators(unique_holdings)

    symbol_stats = {}
    for ticker in clean_tickers:
        series = close_df[ticker] if ticker in close_df.columns else pd.Series(dtype=float)
        clean_series = pd.to_numeric(series, errors="coerce").dropna()
        current_price = float(clean_series.iloc[-1]) if not clean_series.empty else np.nan
        ma200 = clean_series.rolling(window=200, min_periods=200).mean().iloc[-1] if not clean_series.empty else np.nan
        one_month_return = calculate_period_return(clean_series, 21)

        sig = sell_sig_map.get(ticker, {}) if isinstance(sell_sig_map, dict) else {}
        above_ma200_flag = (
            bool(current_price > ma200) if (pd.notna(current_price) and pd.notna(ma200)) else None
        )

        symbol_stats[ticker] = {
            "current_price": current_price,
            "ma200": ma200,
            "one_month_return": one_month_return,
            "above_ma200": above_ma200_flag,
            "sig": sig,
            "close_series": clean_series,  # 매수일 기준 고점 슬라이싱용
        }

    total_market_value = 0.0
    for _, row in clean_portfolio.iterrows():
        ticker = str(row.get("Ticker", "")).strip().upper()
        qty = pd.to_numeric(row.get("Quantity"), errors="coerce")
        cur = pd.to_numeric(symbol_stats.get(ticker, {}).get("current_price"), errors="coerce")
        if pd.notna(qty) and qty > 0 and pd.notna(cur):
            total_market_value += float(cur) * float(qty)

    rows = []
    for _, row in clean_portfolio.iterrows():
        account = str(row.get("Account", "Default Account") or "Default Account").strip()
        ticker = str(row.get("Ticker", "")).strip().upper()
        quantity = pd.to_numeric(row.get("Quantity"), errors="coerce")
        purchase_price = pd.to_numeric(row.get("Purchase_Price"), errors="coerce")
        stat = symbol_stats.get(ticker, {})
        current_price = pd.to_numeric(stat.get("current_price"), errors="coerce")
        ma200 = pd.to_numeric(stat.get("ma200"), errors="coerce")
        one_month_return = pd.to_numeric(stat.get("one_month_return"), errors="coerce")
        date_added = str(row.get("Date_Added", "") or "")

        # 포지션별 보수적 낙폭 + 데이터오류 플래그 (선택 1C/2B)
        drawdown_pct, dd_data_error = compute_position_drawdown(
            stat.get("close_series"), purchase_price, current_price, date_added
        )

        sig = stat.get("sig", {}) if isinstance(stat.get("sig"), dict) else {}
        above_ma200_flag = stat.get("above_ma200")
        if pd.isna(current_price) or pd.isna(ma200) or pd.isna(one_month_return):
            status = "N/A"
            status_reason = "데이터 부족"
        else:
            # 데이터 오류면 낙폭을 판정에 넣지 않는다(허수 매도신호 방지)
            dd_for_verdict = np.nan if dd_data_error else drawdown_pct
            status, status_reason = integrated_sell_verdict(
                above_ma200=above_ma200_flag,
                one_month_return=one_month_return,
                rsi=sig.get("rsi", np.nan),
                macd_signal=sig.get("macd_signal", "N/A"),
                pct_from_52w_high=sig.get("pct_from_52w_high", np.nan),
                drawdown_from_high_pct=dd_for_verdict,
            )
            if dd_data_error:
                status_reason = (status_reason + " · ⚠️ 가격데이터 확인 필요(스플릿/이상치 의심)").strip(" ·")

        return_pct = np.nan
        gain_loss = np.nan
        market_value = np.nan
        if pd.notna(quantity) and quantity > 0 and pd.notna(current_price):
            market_value = float(current_price) * float(quantity)
        if pd.notna(purchase_price) and purchase_price > 0 and pd.notna(current_price):
            return_pct = (float(current_price) / float(purchase_price) - 1.0) * 100.0
        if (
            pd.notna(quantity)
            and quantity > 0
            and pd.notna(current_price)
            and pd.notna(purchase_price)
        ):
            gain_loss = (float(current_price) - float(purchase_price)) * float(quantity)
        weight_pct = np.nan
        if pd.notna(market_value) and total_market_value > 0:
            weight_pct = (float(market_value) / float(total_market_value)) * 100.0

        qt = str(quote_by_ticker.get(ticker, "") or "").strip()
        universe_rank_cell = "-"
        if qt == "ETF":
            if ticker not in universe_set:
                universe_rank_cell = "⚠️ 리스트 없음(추가 요망)"
            else:
                rk = ticker_to_rank.get(ticker)
                if rk is None:
                    universe_rank_cell = "순위 산출 불가"
                else:
                    universe_rank_cell = f"Top {int(rk)}위"
                    if int(rk) > 5:
                        universe_rank_cell = f"🔴 {universe_rank_cell}"

        # SPY Alpha = 종목 1개월 수익률 - SPY 1개월 수익률
        spy_alpha = np.nan
        if pd.notna(one_month_return) and pd.notna(spy_1m_return):
            spy_alpha = float(one_month_return) - float(spy_1m_return)

        rows.append(
            {
                "계좌": account,
                "티커": ticker,
                "수량": quantity,
                "매수가": purchase_price,
                "현재가": current_price,
                "투자 손익($)": gain_loss,
                "수익률(%)": return_pct,
                "SPY Alpha(%)": spy_alpha,
                "Drawdown(%)": drawdown_pct,
                "200일선": ma200,
                "1개월 수익률": one_month_return,
                "자산 비중(%)": weight_pct,
                "유니버스 랭킹(Universe Rank)": universe_rank_cell,
                "상태(Status)": status,
                "판정 근거(Why)": status_reason,
                "_dd_data_error": bool(dd_data_error),
            }
        )

    out_df = pd.DataFrame(rows)
    return out_df


def open_etf_universe_worksheet():
    """Quant_DB 스프레드시트의 ETF_Universe 탭. 없으면 자동 생성."""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        existing_titles = [ws.title for ws in sh.worksheets()]
        if _ETF_UNIVERSE_SHEET_TITLE in existing_titles:
            return sh.worksheet(_ETF_UNIVERSE_SHEET_TITLE), None
        ws = sh.add_worksheet(title=_ETF_UNIVERSE_SHEET_TITLE, rows=3000, cols=6)
        ws.update([_ETF_UNIVERSE_SHEET_COLS], range_name="A1:F1", value_input_option="USER_ENTERED")
        return ws, None
    except Exception as exc:
        return None, f"ETF_Universe 워크시트 열기/생성 실패: {exc}"


@st.cache_data(ttl=3600, show_spinner=False)
def load_etf_universe_from_sheet() -> list[str]:
    """Google Sheets ETF_Universe 탭에서 ticker 목록 로드. 1시간 캐시."""
    ws, err = open_etf_universe_worksheet()
    if err or ws is None:
        return []
    try:
        vals = ws.get_all_values()
        if not vals or len(vals) < 2:
            return []
        tickers = []
        seen = set()
        for r in vals[1:]:
            tk = str((r + [""])[0]).strip().upper()
            if tk and tk not in seen:
                seen.add(tk)
                tickers.append(tk)
        return tickers
    except Exception:
        return []


def save_new_etfs_to_sheet(new_etfs: list[dict]) -> tuple[int, str]:
    """신규 ETF를 ETF_Universe 시트에 추가. 반환: (추가된 수, 에러메시지)"""
    if not new_etfs:
        return 0, ""
    ws, err = open_etf_universe_worksheet()
    if err or ws is None:
        return 0, err or "워크시트 열기 실패"
    try:
        existing_vals = ws.get_all_values()
        existing_tickers = set(
            str(r[0]).strip().upper()
            for r in existing_vals[1:]
            if r and r[0].strip()
        )
        rows_to_add = []
        added_date = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
        for etf in new_etfs:
            tk = str(etf.get("ticker", "")).strip().upper()
            if not tk or tk in existing_tickers:
                continue
            rows_to_add.append([
                tk,
                str(etf.get("name", ""))[:80],
                str(etf.get("category", ""))[:50],
                str(etf.get("aum_m", "")),
                added_date,
                "FMP_AUTO",
            ])
            existing_tickers.add(tk)
        if rows_to_add:
            _safe_append_rows(ws, rows_to_add, value_input_option="USER_ENTERED")
        return len(rows_to_add), ""
    except Exception as exc:
        return 0, str(exc)


@st.cache_data(ttl=86400, show_spinner=False)
def _fmp_etf_symbol_name_map() -> dict:
    """
    /stable/etf-list 로 'ETF인 심볼 → 이름' 맵을 만든다.
    주의: 이 엔드포인트는 심볼·이름만 제공한다(상장일·거래소·AUM 없음).
    그래서 '어떤 심볼이 ETF인지'를 가리는 멤버십 집합 용도로만 쓴다. 1일 캐시.
    """
    k = _fmp_key()
    if not k:
        return {}
    try:
        r = requests.get(f"{_FMP_BASE}/etf-list?apikey={k}", timeout=15)
        if r.status_code != 200:
            return {}
        data = r.json()
        if not isinstance(data, list):
            return {}
        out = {}
        for it in data:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol", "") or "").strip().upper()
            if not sym:
                continue
            out[sym] = str(it.get("name", "") or "").strip()
        return out
    except Exception:
        return {}


def fetch_new_etfs_from_fmp(days_lookback: int = 90, min_aum_m: float = 50.0) -> list[dict]:
    """
    최근 N일 내 '신규 상장'된 ETF를 스캔.

    [수정 이유] 기존 구현은 /stable/etf-list 응답에서 ipoDate·exchange 를 읽으려 했으나,
    이 엔드포인트는 심볼·이름만 반환하므로 모든 항목이 필터에서 탈락 → 항상 빈 결과였다.

    [새 방식]
      1) /stable/ipos-calendar 로 최근 N일 신규 상장(상장일·거래소 포함) 목록을 받고
      2) /stable/etf-list(심볼 집합)와 교집합해 'ETF인 것'만 남긴 뒤
      3) /stable/profile 로 AUM(totalAssets)을 확인한다.
    - days_lookback: 최근 N일 이내 상장
    - min_aum_m: 최소 AUM (백만달러). AUM 데이터가 없으면(신규라 미집계) 일단 통과시킨다.
    """
    k = _fmp_key()
    if not k:
        return []
    try:
        # 1) ETF 심볼 집합 (멤버십 판별용)
        etf_map = _fmp_etf_symbol_name_map()
        if not etf_map:
            return []

        # 2) 최근 N일 IPO/신규 상장 캘린더
        today_et = datetime.now(_MARKET_ET_TZ)
        cutoff_date = today_et - timedelta(days=days_lookback)
        from_str = cutoff_date.strftime("%Y-%m-%d")
        to_str = today_et.strftime("%Y-%m-%d")
        # from/to 를 서버가 무시해도 아래에서 클라이언트측으로 한 번 더 거른다.
        url = f"{_FMP_BASE}/ipos-calendar?from={from_str}&to={to_str}&apikey={k}"
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return []
        ipos = resp.json()
        if not isinstance(ipos, list):
            return []

        _US_EXCH = {
            "NYSE ARCA", "NYSEARCA", "ARCA", "NASDAQ", "NASDAQ GLOBAL MARKET",
            "BATS", "CBOE", "CBOE BZX", "NYSE", "AMEX", "NYSE AMERICAN",
        }

        candidates = []
        seen = set()
        for it in ipos:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol", "") or "").strip().upper()
            if not sym or sym in seen:
                continue
            # ETF 목록에 있는 심볼만 (= 신규 상장 중 ETF인 것)
            if sym not in etf_map:
                continue
            # 미국 거래소만 (거래소 정보가 있을 때만 필터, 없으면 통과)
            exch = str(it.get("exchange", "") or it.get("exchangeShortName", "") or "").upper().strip()
            if exch and exch not in _US_EXCH:
                continue
            # 상장일 클라이언트측 재확인
            ipo_date_str = str(it.get("date", "") or it.get("ipoDate", "") or "")[:10]
            if ipo_date_str:
                try:
                    ipo_dt = datetime.strptime(ipo_date_str, "%Y-%m-%d").replace(tzinfo=_MARKET_ET_TZ)
                    if ipo_dt < cutoff_date:
                        continue
                except Exception:
                    pass
            seen.add(sym)
            candidates.append({
                "ticker": sym,
                "name": (etf_map.get(sym) or str(it.get("company", "") or it.get("name", "") or ""))[:80],
                "category": "",
                "aum_m": "",
                "ipo_date": ipo_date_str,
            })

        # 3) AUM 확인 (profile) — 레이트리밋 보호를 위해 호출 수 제한
        filtered = []
        for etf in candidates[:60]:
            try:
                p = _fmp_profile(etf["ticker"])
                aum = float(p.get("totalAssets") or p.get("mktCap") or 0) / 1_000_000
                # AUM 집계가 안 된 신규 ETF는 일단 통과(빈 값), 집계됐는데 기준 미달이면 제외
                if aum and aum < min_aum_m:
                    continue
                if aum:
                    etf["aum_m"] = f"{aum:.0f}"
                if not etf["category"]:
                    etf["category"] = str(p.get("sector") or p.get("industry") or "")[:50]
                filtered.append(etf)
            except Exception:
                filtered.append(etf)

        return filtered

    except Exception:
        return []


def cleanup_low_quality_etfs_from_sheet(min_avg_volume_m: float = 1.0, min_aum_m: float = 100.0) -> tuple[int, str]:
    """
    Google Sheets ETF_Universe에서 유동성/AUM 미달 ETF 자동 제거.
    - 30일 평균 거래대금 < $1M
    - AUM < $100M (상장 6개월 이상된 ETF만)
    보호 목록: etf_universe.txt에 있는 티커는 절대 삭제하지 않음.
    """
    ws, err = open_etf_universe_worksheet()
    if err or ws is None:
        return 0, err or "워크시트 열기 실패"
    try:
        vals = ws.get_all_values()
        if not vals or len(vals) < 2:
            return 0, ""

        # etf_universe.txt 티커는 보호 (삭제 대상에서 제외)
        protected = set(load_etf_universe_tickers())
        cutoff_date = datetime.now(_MARKET_ET_TZ) - timedelta(days=180)

        rows_to_delete = []
        for i, r in enumerate(vals[1:], start=2):
            r = (r + [""] * 6)[:6]
            ticker = str(r[0]).strip().upper()
            source = str(r[5]).strip()
            added_date_str = str(r[4]).strip()

            # txt 보호 목록은 스킵
            if ticker in protected or source != "FMP_AUTO":
                continue

            # 상장 6개월 미만이면 스킵 (아직 AUM 성장 중)
            try:
                added_dt = datetime.strptime(added_date_str[:10], "%Y-%m-%d").replace(tzinfo=_MARKET_ET_TZ)
                if added_dt > cutoff_date:
                    continue
            except Exception:
                continue

            # FMP로 AUM + 거래량 체크
            try:
                p = _fmp_profile(ticker)
                aum = float(p.get("totalAssets") or p.get("mktCap") or 0) / 1_000_000
                avg_vol = float(p.get("volAvg") or p.get("averageVolume") or 0)
                price = float(p.get("price") or p.get("regularMarketPrice") or 0)
                avg_vol_m = avg_vol * price / 1_000_000

                if aum < min_aum_m or avg_vol_m < min_avg_volume_m:
                    rows_to_delete.append(i)
            except Exception:
                continue

        # 역순으로 삭제 (인덱스 밀림 방지)
        for row_idx in reversed(rows_to_delete):
            ws.delete_rows(row_idx)

        return len(rows_to_delete), ""
    except Exception as exc:
        return 0, str(exc)


def should_run_etf_auto_update() -> bool:
    """마지막 ETF 자동 업데이트로부터 7일이 지났는지 확인."""
    last_update = st.session_state.get("_etf_auto_update_last_run")
    if last_update is None:
        return True
    elapsed = (datetime.now(timezone.utc) - last_update).total_seconds()
    return elapsed > (_ETF_AUTO_UPDATE_INTERVAL_DAYS * 86400)


def run_etf_auto_update_if_needed(silent: bool = True) -> tuple[int, str]:
    """
    필요 시 ETF 자동 업데이트 실행.
    1) FMP로 신규 ETF 추가
    2) 유동성 낮은 ETF 자동 정리 (상장 6개월+ & AUM<$100M & 거래대금<$1M)
    silent=True: 백그라운드 실행
    반환: (새로 추가된 ETF 수, 에러 메시지)
    """
    if not should_run_etf_auto_update():
        return 0, ""
    st.session_state["_etf_auto_update_last_run"] = datetime.now(timezone.utc)
    try:
        # 1. 신규 ETF 추가
        new_etfs = fetch_new_etfs_from_fmp(days_lookback=90, min_aum_m=50.0)
        added = 0
        err = ""
        if new_etfs:
            added, err = save_new_etfs_to_sheet(new_etfs)
        # 2. 저품질 ETF 자동 정리 (silent 모드에서만 - 백그라운드)
        if silent:
            try:
                cleanup_low_quality_etfs_from_sheet(min_avg_volume_m=1.0, min_aum_m=100.0)
            except Exception:
                pass
        return added, err
    except Exception as exc:
        return 0, str(exc)


def load_etf_universe_tickers_merged() -> list[str]:
    """
    etf_universe.txt + Google Sheets ETF_Universe 합산 ticker 목록.
    중복 제거 후 반환.
    """
    file_tickers = load_etf_universe_tickers()
    sheet_tickers = load_etf_universe_from_sheet()
    seen = set()
    merged = []
    for tk in file_tickers + sheet_tickers:
        tk = str(tk).strip().upper()
        if tk and tk not in seen:
            seen.add(tk)
            merged.append(tk)
    return merged


def import_txt_tickers_to_sheet() -> tuple[int, str]:
    """
    etf_universe.txt 의 티커를 Google Sheets `ETF_Universe` 탭으로 1회성 통합한다.
    - 이미 시트에 있는 티커는 건너뛴다.
    - source='TXT_IMPORT' 로 저장하므로, 저품질 자동정리(cleanup, source=='FMP_AUTO'만 삭제)
      대상에서 자동으로 보호된다.
    통합 후에는 etf_universe.txt 가 없어도 동작한다.
    반환: (추가된 수, 에러메시지)
    """
    txt_tickers = load_etf_universe_tickers()
    if not txt_tickers:
        return 0, "etf_universe.txt에서 읽은 티커가 없습니다."
    ws, err = open_etf_universe_worksheet()
    if err or ws is None:
        return 0, err or "워크시트 열기 실패"
    try:
        existing_vals = ws.get_all_values()
        existing = set(
            str(r[0]).strip().upper()
            for r in existing_vals[1:]
            if r and r[0].strip()
        )
        added_date = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
        rows_to_add = []
        for tk in txt_tickers:
            tk = str(tk).strip().upper()
            if not tk or tk in existing:
                continue
            # [Ticker, Name, Category, AUM_M, Added_Date, Source]
            rows_to_add.append([tk, "", "", "", added_date, "TXT_IMPORT"])
            existing.add(tk)
        if rows_to_add:
            _safe_append_rows(ws, rows_to_add, value_input_option="USER_ENTERED")
        return len(rows_to_add), ""
    except Exception as exc:
        return 0, str(exc)


# 알려진 레버리지/인버스 ETF → 배수 매핑. 양수=레버리지, 음수=인버스.
# 유니버스(미국 상장 ETF) 위주라 이 매핑이 이름 파싱보다 정확하다.
_LEVERAGED_ETF_MAP = {
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
    "UCO": 2, "QID": -2,
    # ── 1.5x ──
    "TSLR": 1.5,
    # ── 인버스 ──
    "SQQQ": -3, "SPXU": -3, "SOXS": -3, "TECS": -3, "FAZ": -3,
    "TZA": -3, "LABD": -3, "SDOW": -3, "WEBS": -3, "YANG": -3,
    "DRV": -3, "ERY": -3, "JDST": -3, "DGAZ": -3, "KOLD": -3, "SCO": -3,
    "SDS": -2, "QID": -2, "DXD": -2, "TWM": -2, "SKF": -2,
    "SH": -1, "PSQ": -1, "DOG": -1, "RWM": -1, "EUM": -1,
}

# 이름 문자열 보조 추정용 (매핑에 없을 때만). (정규식, 배수)
# 이름 문자열 보조 추정용 (매핑에 없을 때만): 배수 크기와 인버스 방향을 따로 판별.
_LEV_MAG_3X = re.compile(r"\b3X\b|ULTRAPRO|TRIPLE", re.I)
_LEV_MAG_2X = re.compile(r"\b2X\b|\bULTRA\b|DOUBLE", re.I)
_LEV_MAG_15 = re.compile(r"\b1\.5X\b", re.I)
_LEV_INVERSE = re.compile(r"INVERSE|SHORT|BEAR", re.I)


def get_leverage_multiplier(ticker: str, name: str = "") -> float | None:
    """티커(필요시 이름)로 레버리지 배수를 반환. 일반 ETF/주식이면 None.
    양수=레버리지 롱, 음수=인버스."""
    t = str(ticker).strip().upper()
    if t in _LEVERAGED_ETF_MAP:
        return _LEVERAGED_ETF_MAP[t]
    if name:
        n = str(name)
        # 배수 크기와 인버스 방향을 독립적으로 판별한 뒤 결합
        if _LEV_MAG_3X.search(n):
            mag = 3.0
        elif _LEV_MAG_2X.search(n):
            mag = 2.0
        elif _LEV_MAG_15.search(n):
            mag = 1.5
        else:
            mag = None
        is_inverse = bool(_LEV_INVERSE.search(n))
        if mag is not None:
            return -mag if is_inverse else mag
        if is_inverse:
            return -1.0  # 배수 명시 없는 단순 인버스(-1x)
    return None


def leverage_badge(ticker: str, name: str = "") -> str:
    """티커 옆에 붙일 배지 문자열. 일반 종목이면 빈 문자열.
    매핑에 없고 이름도 안 넘어오면, FMP etf-list 이름맵(캐시)으로 한 번 더 추정한다."""
    m = get_leverage_multiplier(ticker, name)
    if m is None and not name:
        try:
            _nm = _fmp_etf_symbol_name_map().get(str(ticker).strip().upper(), "")
        except Exception:
            _nm = ""
        if _nm:
            m = get_leverage_multiplier(ticker, _nm)
    if m is None:
        return ""
    if m < 0:
        return f" ⚡{abs(int(m)) if float(m).is_integer() else abs(m)}x 인버스"
    mult_str = str(int(m)) if float(m).is_integer() else str(m)
    return f" ⚡{mult_str}x 레버리지"


def save_scanner_result_history(user_id: str, score_df: pd.DataFrame, engine: str = "leaders") -> tuple[bool, str]:
    """AI 스캐너 TOP 결과를 Sheets에 저장. engine: 'leaders' | 'emerging'"""
    gc = get_gspread_client()
    if gc is None:
        return False, "Google 서비스 계정이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        existing = [ws.title for ws in sh.worksheets()]
        if _SCANNER_HISTORY_SHEET_TITLE not in existing:
            ws = sh.add_worksheet(title=_SCANNER_HISTORY_SHEET_TITLE, rows=5000, cols=9)
            ws.update([_SCANNER_HISTORY_COLS], range_name="A1:I1", value_input_option="USER_ENTERED")
        else:
            ws = sh.worksheet(_SCANNER_HISTORY_SHEET_TITLE)
            # ── 헤더 마이그레이션: Engine 컬럼 없으면 자동 추가 ──────────
            cur_header = ws.row_values(1)
            if "Engine" not in cur_header:
                # 기존 데이터 전체 읽기
                all_vals = ws.get_all_values()
                # 시트 초기화 후 새 헤더로 재작성
                ws.clear()
                ws.update([_SCANNER_HISTORY_COLS], range_name="A1:I1", value_input_option="USER_ENTERED")
                # 기존 데이터를 새 포맷으로 변환 (Engine="Leaders" 삽입)
                if len(all_vals) > 1:
                    migrated = []
                    for old_row in all_vals[1:]:
                        old_row = (old_row + [""] * 8)[:8]
                        # ID, Date, [Engine삽입], Ticker, Score, Rank, Verdict, RS_Score, Mom_1M
                        migrated.append([old_row[0], old_row[1], "Leaders"] + old_row[2:])
                    _safe_append_rows(ws, migrated, value_input_option="USER_ENTERED")

        today_str = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
        uid_u = str(user_id).strip().upper()
        engine_label = "Emerging" if engine == "emerging" else "Leaders"
        rows = []
        for rank_idx, (_, row) in enumerate(score_df.head(10).iterrows(), start=1):
            _ticker = str(row.get("Ticker", ""))
            _score = str(round(float(row.get("Final Score", 0) or 0), 2))
            _verdict = str(row.get("Narrative Why", row.get("Verdict", "")))[:50]
            _rs = ""
            for _rs_col in ["RS Score", "Early RS Score", "RS"]:
                if _rs_col in row and pd.notna(row[_rs_col]):
                    try:
                        _rs = str(round(float(row[_rs_col]), 2))
                    except Exception:
                        pass
                    break
            _mom = ""
            for _mom_col in ["1M Return", "Momentum Score", "Vol Accel Score"]:
                if _mom_col in row and pd.notna(row[_mom_col]):
                    try:
                        _mom = str(round(float(row[_mom_col]), 2))
                    except Exception:
                        pass
                    break
            rows.append([uid_u, today_str, engine_label, _ticker, _score, str(rank_idx), _verdict, _rs, _mom])
        if rows:
            _safe_append_rows(ws, rows, value_input_option="USER_ENTERED")
        return True, ""
    except Exception as exc:
        return False, str(exc)


def save_scanner_last_result(user_id: str, snap: dict, engine: str) -> tuple[bool, str]:
    """
    마지막 스캔 결과 전체(score_df + meta)를 Scanner_Last_Result 탭에 JSON으로 저장.
    세션이 끊겨도 다음 로그인 시 복원 가능.
    user_id + engine 기준으로 덮어쓰기 (최신 1건만 유지).
    """
    gc = get_gspread_client()
    if gc is None:
        return False, "Google 서비스 계정 미설정"
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        existing = [w.title for w in sh.worksheets()]
        if _SCANNER_LAST_RESULT_SHEET_TITLE not in existing:
            ws = sh.add_worksheet(title=_SCANNER_LAST_RESULT_SHEET_TITLE, rows=100, cols=5)
            ws.update([_SCANNER_LAST_RESULT_COLS], range_name="A1:E1", value_input_option="USER_ENTERED")
        else:
            ws = sh.worksheet(_SCANNER_LAST_RESULT_SHEET_TITLE)

        uid_u = str(user_id).strip().upper()
        engine_label = str(engine).strip()
        saved_at = datetime.now(timezone.utc).isoformat()

        # score_df → JSON (상위 50행만, 용량 절약)
        score_df = snap.get("score_df")
        if not isinstance(score_df, pd.DataFrame) or score_df.empty:
            return False, "score_df 없음"
        score_json = score_df.head(50).to_json(orient="records", force_ascii=False)
        universe_count = str(len(snap.get("universe") or []))

        # 메타 정보도 JSON에 포함
        meta = {
            "mode_note": snap.get("mode_note", ""),
            "scanner_mode": snap.get("scanner_mode", ""),
            "universe": snap.get("universe", []),
            "completed_at": snap.get("completed_at", saved_at),
        }
        payload = json.dumps({"meta": meta, "rows": json.loads(score_json)}, ensure_ascii=False)

        # 기존 행 탐색 (해당 user+engine)
        all_vals = ws.get_all_values()
        target_row = None
        for ri, row in enumerate(all_vals[1:], start=2):
            row = (row + [""] * 5)[:5]
            if str(row[0]).strip().upper() == uid_u and str(row[1]).strip() == engine_label:
                target_row = ri
                break

        new_row = [uid_u, engine_label, saved_at, universe_count, payload]
        if target_row:
            ws.update([new_row], range_name=f"A{target_row}:E{target_row}", value_input_option="USER_ENTERED")
        else:
            _safe_append_rows(ws, [new_row], value_input_option="USER_ENTERED")
        return True, ""
    except Exception as exc:
        return False, str(exc)


def load_scanner_last_result(user_id: str, engine: str) -> dict | None:
    """
    Scanner_Last_Result 탭에서 마지막 스캔 결과를 복원.
    반환: snap dict (score_df 포함) 또는 None.
    """
    gc = get_gspread_client()
    if gc is None:
        return None
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        existing = [w.title for w in sh.worksheets()]
        if _SCANNER_LAST_RESULT_SHEET_TITLE not in existing:
            return None
        ws = sh.worksheet(_SCANNER_LAST_RESULT_SHEET_TITLE)
        all_vals = ws.get_all_values()
        if not all_vals or len(all_vals) < 2:
            return None
        uid_u = str(user_id).strip().upper()
        engine_label = str(engine).strip()
        for row in all_vals[1:]:
            row = (row + [""] * 5)[:5]
            if str(row[0]).strip().upper() == uid_u and str(row[1]).strip() == engine_label:
                payload_str = str(row[4]).strip()
                if not payload_str:
                    return None
                try:
                    payload = json.loads(payload_str)
                    meta = payload.get("meta", {})
                    rows_data = payload.get("rows", [])
                    if not rows_data:
                        return None
                    score_df = pd.DataFrame(rows_data)
                    return {
                        "score_df": score_df,
                        "mode_note": meta.get("mode_note", engine_label),
                        "scanner_mode": meta.get("scanner_mode", ""),
                        "universe": meta.get("universe", []),
                        "completed_at": meta.get("completed_at", ""),
                        "_restored_from_sheet": True,
                    }
                except Exception:
                    return None
        return None
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def load_scanner_history(user_id: str, engine: str = "all") -> pd.DataFrame:
    """스캐너 히스토리 로드. engine: 'all' | 'leaders' | 'emerging'"""
    gc = get_gspread_client()
    if gc is None:
        return pd.DataFrame()
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        existing = [ws.title for ws in sh.worksheets()]
        if _SCANNER_HISTORY_SHEET_TITLE not in existing:
            return pd.DataFrame()
        ws = sh.worksheet(_SCANNER_HISTORY_SHEET_TITLE)
        vals = ws.get_all_values()
        if not vals or len(vals) < 2:
            return pd.DataFrame()
        uid_u = str(user_id).strip().upper()
        # 헤더로 컬럼 인덱스 파악 (Engine 컬럼 있을 수도 없을 수도)
        header = [str(h).strip() for h in vals[0]]
        has_engine = "Engine" in header
        rows = []
        for r in vals[1:]:
            r = (r + [""] * 9)[:9]
            if str(r[0]).strip().upper() != uid_u:
                continue
            if has_engine:
                row_engine = str(r[2]).strip()
                date_v, eng_v, tk_v, sc_v, rk_v, vd_v, rs_v, mom_v = r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]
            else:
                # 구버전 (Engine 컬럼 없음) → Leaders로 간주
                row_engine = "Leaders"
                date_v, eng_v, tk_v, sc_v, rk_v, vd_v, rs_v, mom_v = r[1], "Leaders", r[2], r[3], r[4], r[5], r[6], r[7]
            # 엔진 필터
            if engine != "all":
                if engine == "leaders" and row_engine.lower() != "leaders":
                    continue
                if engine == "emerging" and row_engine.lower() != "emerging":
                    continue
            rows.append({
                "Date": date_v, "Engine": eng_v, "Ticker": tk_v,
                "Score": pd.to_numeric(sc_v, errors="coerce"),
                "Rank": pd.to_numeric(rk_v, errors="coerce"),
                "Verdict": vd_v,
                "RS_Score": pd.to_numeric(rs_v, errors="coerce"),
                "Mom_1M": pd.to_numeric(mom_v, errors="coerce"),
            })
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def open_portfolio_history_worksheet():
    """Quant_DB 스프레드시트의 Portfolio_History 탭. 없으면 자동 생성."""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        existing = [ws.title for ws in sh.worksheets()]
        if _PORTFOLIO_HISTORY_SHEET_TITLE in existing:
            return sh.worksheet(_PORTFOLIO_HISTORY_SHEET_TITLE), None
        ws = sh.add_worksheet(title=_PORTFOLIO_HISTORY_SHEET_TITLE, rows=5000, cols=8)
        ws.update([_PORTFOLIO_HISTORY_COLS], range_name="A1:H1", value_input_option="USER_ENTERED")
        return ws, None
    except Exception as exc:
        return None, f"Portfolio_History 워크시트 열기/생성 실패: {exc}"


def save_portfolio_snapshot(user_id: str, sell_radar_df: pd.DataFrame) -> tuple[bool, str]:
    """
    현재 포트폴리오 상태를 스냅샷으로 저장.
    - 일 1회 중복 저장 방지 (오늘 이미 저장된 기록 있으면 업데이트)
    """
    ws, err = open_portfolio_history_worksheet()
    if err or ws is None:
        return False, err or "워크시트 열기 실패"
    if sell_radar_df is None or sell_radar_df.empty:
        return False, "포트폴리오 데이터 없음"
    try:
        today_str = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
        uid_u = str(user_id).strip().upper()

        # 총 평가금액, 총 매수금액, 수익률 계산
        current_vals = pd.to_numeric(sell_radar_df["현재가"], errors="coerce")
        avg_prices = pd.to_numeric(sell_radar_df["매수가"], errors="coerce")
        quantities = pd.to_numeric(sell_radar_df["수량"], errors="coerce").fillna(1)
        gain_loss = pd.to_numeric(sell_radar_df["투자 손익($)"], errors="coerce")

        total_cost = float((avg_prices * quantities).sum()) if not avg_prices.empty else 0
        total_value = float(total_cost + gain_loss.sum()) if not gain_loss.empty else total_cost
        ret_pct = float((total_value / total_cost - 1) * 100) if total_cost > 0 else 0.0

        # SPY 수익률 (1개월)
        spy_pct = np.nan
        try:
            spy_col = next((c for c in sell_radar_df.columns if "SPY" in str(c)), None)
            if spy_col:
                spy_vals = pd.to_numeric(sell_radar_df[spy_col], errors="coerce").dropna()
                spy_pct = float(spy_vals.mean()) if not spy_vals.empty else np.nan
        except Exception:
            pass

        alpha_pct = float(ret_pct - spy_pct) if pd.notna(spy_pct) else np.nan
        positions_csv = ",".join(sell_radar_df["티커"].dropna().astype(str).tolist())

        new_row = [
            uid_u, today_str,
            f"{total_value:.2f}", f"{total_cost:.2f}",
            f"{ret_pct:.3f}",
            f"{spy_pct:.3f}" if pd.notna(spy_pct) else "",
            f"{alpha_pct:.3f}" if pd.notna(alpha_pct) else "",
            positions_csv[:200],
        ]

        # 오늘 기록이 이미 있으면 업데이트, 없으면 추가
        vals = ws.get_all_values()
        today_row = None
        for i, r in enumerate(vals[1:], start=2):
            if (r + [""])[0].strip().upper() == uid_u and (r + [""])[1].strip() == today_str:
                today_row = i
                break

        if today_row:
            ws.update([new_row], range_name=f"A{today_row}:H{today_row}", value_input_option="USER_ENTERED")
        else:
            _safe_append_rows(ws, new_row, value_input_option="USER_ENTERED")
        return True, ""
    except Exception as exc:
        return False, str(exc)


@st.cache_data(ttl=300, show_spinner=False)
def load_portfolio_history(user_id: str) -> pd.DataFrame:
    """포트폴리오 히스토리 로드."""
    ws, err = open_portfolio_history_worksheet()
    if err or ws is None:
        return pd.DataFrame()
    try:
        vals = ws.get_all_values()
        if not vals or len(vals) < 2:
            return pd.DataFrame()
        uid_u = str(user_id).strip().upper()
        rows = []
        for r in vals[1:]:
            r = (r + [""] * 8)[:8]
            if str(r[0]).strip().upper() != uid_u:
                continue
            rows.append({
                "Date": r[1],
                "Total_Value": pd.to_numeric(r[2], errors="coerce"),
                "Total_Cost": pd.to_numeric(r[3], errors="coerce"),
                "Return_Pct": pd.to_numeric(r[4], errors="coerce"),
                "SPY_Pct": pd.to_numeric(r[5], errors="coerce"),
                "Alpha_Pct": pd.to_numeric(r[6], errors="coerce"),
                "Positions": r[7],
            })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        return df.sort_values("Date").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def open_emerging_tracker_worksheet():
    """Quant_DB 스프레드시트의 Emerging_Tracker 탭. 없으면 자동 생성."""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        existing = [ws.title for ws in sh.worksheets()]
        if _EMERGING_TRACKER_SHEET_TITLE in existing:
            return sh.worksheet(_EMERGING_TRACKER_SHEET_TITLE), None
        ws = sh.add_worksheet(title=_EMERGING_TRACKER_SHEET_TITLE, rows=2000, cols=9)
        ws.update([_EMERGING_TRACKER_COLS], range_name="A1:I1", value_input_option="USER_ENTERED")
        return ws, None
    except Exception as exc:
        return None, f"Emerging_Tracker 워크시트 열기/생성 실패: {exc}"


def load_emerging_tracker(user_id: str) -> pd.DataFrame:
    """현재 유저의 Emerging 추적 기록 전체 로드."""
    ws, err = open_emerging_tracker_worksheet()
    if err or ws is None:
        return pd.DataFrame(columns=_EMERGING_TRACKER_COLS)
    try:
        vals = ws.get_all_values()
        if not vals or len(vals) < 2:
            return pd.DataFrame(columns=_EMERGING_TRACKER_COLS)
        uid_u = str(user_id).strip().upper()
        rows = []
        for r in vals[1:]:
            r = (r + [""] * 9)[:9]
            if str(r[0]).strip().upper() != uid_u:
                continue
            rows.append({
                "ID": r[0], "Ticker": r[1], "Theme": r[2],
                "First_Seen": r[3], "Last_Seen": r[4],
                "Count": int(r[5]) if r[5].isdigit() else 1,
                "Best_Verdict": r[6], "RS_Score": r[7], "Status": r[8],
            })
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=_EMERGING_TRACKER_COLS)
    except Exception:
        return pd.DataFrame(columns=_EMERGING_TRACKER_COLS)


# Emerging 신선도: Last_Seen이 이 일수를 넘으면 '오래된 신호'로 간주(매수신호 제외, 기록은 보존)
_EMERGING_STALE_DAYS = 10


def _emerging_days_since_last_seen(last_seen_str) -> float:
    """Last_Seen(ET 'YYYY-MM-DD HH:MM' 또는 'YYYY-MM-DD')로부터 경과 일수. 파싱 실패 시 NaN."""
    s = str(last_seen_str or "").strip()
    if not s:
        return np.nan
    try:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
                break
            except ValueError:
                continue
        if dt is None:
            dt = pd.to_datetime(s, errors="coerce").to_pydatetime()
        if dt is None or pd.isna(dt):
            return np.nan
        # ET 기준 현재와 비교 (naive끼리 비교)
        now_et = datetime.now(_MARKET_ET_TZ).replace(tzinfo=None)
        return max(0.0, (now_et - dt).total_seconds() / 86400.0)
    except Exception:
        return np.nan


def _augment_emerging_with_freshness(et_df):
    """et_df에 _days_old(경과일)와 _is_stale(오래됨 여부) 컬럼을 추가해 반환."""
    if et_df is None or et_df.empty:
        return et_df
    out = et_df.copy()
    out["_days_old"] = out["Last_Seen"].apply(_emerging_days_since_last_seen)
    out["_is_stale"] = out["_days_old"].apply(
        lambda d: bool(pd.notna(d) and d > _EMERGING_STALE_DAYS)
    )
    return out


def upsert_emerging_tracker(user_id: str, ticker: str, theme: str, verdict: str, rs_score) -> tuple[bool, str]:
    """Emerging 종목 기록 추가/업데이트. 같은 티커면 Count++, Last_Seen 갱신."""
    ws, err = open_emerging_tracker_worksheet()
    if err or ws is None:
        return False, err or "워크시트 열기 실패"
    try:
        vals = ws.get_all_values()
        uid_u = str(user_id).strip().upper()
        tk_u = str(ticker).strip().upper()
        now_str = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d %H:%M")
        rs_str = f"{float(rs_score):.2f}" if rs_score is not None and rs_score == rs_score else ""

        # 기존 행 찾기
        found_row = None
        for i, r in enumerate(vals[1:], start=2):
            r = (r + [""] * 9)[:9]
            if str(r[0]).strip().upper() == uid_u and str(r[1]).strip().upper() == tk_u:
                found_row = (i, r)
                break

        if found_row:
            row_idx, old_r = found_row
            old_count = int(old_r[5]) if old_r[5].isdigit() else 1
            new_count = old_count + 1
            # Best_Verdict: 최적 > 얼리버드 > 이미강세 > 신호대기 우선순위
            verdict_priority = {"🎯 최적 매수 타이밍": 0, "🌱 얼리버드 기회": 1, "✅ 이미 강세 (진입 시 고점 주의)": 2}
            old_priority = verdict_priority.get(old_r[6], 99)
            new_priority = verdict_priority.get(verdict, 99)
            best_verdict = verdict if new_priority < old_priority else old_r[6]

            # Status 결정
            if new_count >= 5:
                status = "🔥 지속 등장 (강한 신호)"
            elif new_count >= 3:
                status = "📌 반복 등장"
            else:
                status = "🆕 신규"

            ws.update(
                [[uid_u, tk_u, theme, old_r[3], now_str, str(new_count), best_verdict, rs_str, status]],
                range_name=f"A{row_idx}:I{row_idx}",
                value_input_option="USER_ENTERED"
            )
        else:
            _safe_append_rows(ws, 
                [uid_u, tk_u, str(theme)[:60], now_str, now_str, "1", verdict, rs_str, "🆕 신규"],
                value_input_option="USER_ENTERED"
            )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def delete_emerging_tracker_row(user_id: str, ticker: str) -> tuple[bool, str]:
    """Emerging Tracker에서 특정 티커 삭제."""
    ws, err = open_emerging_tracker_worksheet()
    if err or ws is None:
        return False, err or "워크시트 열기 실패"
    try:
        vals = ws.get_all_values()
        uid_u = str(user_id).strip().upper()
        tk_u = str(ticker).strip().upper()
        to_del = [i for i, r in enumerate(vals[1:], start=2)
                  if (r + [""])[0].strip().upper() == uid_u and (r + [""])[1].strip().upper() == tk_u]
        for idx in reversed(to_del):
            ws.delete_rows(idx)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _repair_watchlist_layout(ws) -> bool:
    """헤더 누락 또는 계단식 드리프트로 어긋난 Watchlist 행을 A열 기준 8칸 레코드로 재정렬.

    과거에 table_range 없이 append_row가 호출되어 행이 오른쪽으로 밀려 저장됐거나
    (1행 A열 → 2행 F열 → 3행 K열), 헤더 행이 없어 첫 데이터가 1행에 들어가 읽기에서
    누락되던 데이터를 모두 복구한다. 이미 정상이면 아무 것도 하지 않는다(멱등).
    반환: 시트를 변경했으면 True, 변경할 필요 없었으면 False.
    """
    try:
        vals = ws.get_all_values() or []
    except Exception:
        return False

    if not vals:
        _hdr_end = gspread.utils.rowcol_to_a1(1, _WL_NCOL)
        ws.update([_WATCHLIST_SHEET_COLS], range_name=f"A1:{_hdr_end}", value_input_option="USER_ENTERED")
        return True

    header_ok = [str(c).strip() for c in (vals[0] + [""] * _WL_NCOL)[:_WL_NCOL]] == _WATCHLIST_SHEET_COLS

    # 데이터 행 중 A열이 비어(=오른쪽으로 밀려) 시작하는 행이 있으면 드리프트
    drift = False
    for r in (vals[1:] if header_ok else vals):
        if not any(str(c).strip() for c in r):
            continue
        first_nonempty = next((i for i, c in enumerate(r) if str(c).strip()), 0)
        if first_nonempty > 0:
            drift = True
            break

    if header_ok and not drift:
        return False  # 정상 → 재작성 불필요

    # 모든 비어있지 않은 행을 '첫 비어있지 않은 셀'부터 _WL_NCOL칸으로 정규화
    # (과거 8열 레코드는 Stop_Loss/Target_Price 자리가 빈 칸으로 채워져 자동 마이그레이션됨)
    cleaned = []
    for r in vals:
        if not any(str(c).strip() for c in r):
            continue
        start = next((i for i, c in enumerate(r) if str(c).strip()), None)
        if start is None:
            continue
        rec = (r[start:start + _WL_NCOL] + [""] * _WL_NCOL)[:_WL_NCOL]
        # 헤더로 보이는 행은 건너뛰고(아래에서 다시 부착) — 신/구 헤더 모두 인식
        _rec_head = [str(c).strip() for c in rec]
        if _rec_head == _WATCHLIST_SHEET_COLS \
                or _rec_head[:8] == _WATCHLIST_SHEET_COLS[:8] \
                or str(rec[0]).strip().upper() in ("ID", "USER_ID", "USERID"):
            continue
        # user_id, ticker 둘 다 있어야 유효 레코드
        if not str(rec[0]).strip() or not str(rec[1]).strip():
            continue
        cleaned.append(rec)

    new_rows = [_WATCHLIST_SHEET_COLS] + cleaned
    try:
        if len(new_rows) > ws.row_count:
            ws.add_rows(len(new_rows) - ws.row_count + 50)
    except Exception:
        pass
    ws.clear()
    end_cell = gspread.utils.rowcol_to_a1(len(new_rows), _WL_NCOL)
    ws.update(new_rows, range_name=f"A1:{end_cell}", value_input_option="USER_ENTERED")
    return True


def open_watchlist_worksheet():
    """Quant_DB 스프레드시트의 Watchlist 탭. 없으면 자동 생성."""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정(`gcp_service_account`)이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        # 먼저 기존 탭 목록에서 찾기
        existing_titles = [ws.title for ws in sh.worksheets()]
        if _WATCHLIST_SHEET_TITLE in existing_titles:
            ws = sh.worksheet(_WATCHLIST_SHEET_TITLE)
            # 과거 드리프트/헤더 누락 데이터 자동 복구 (세션당 1회)
            if not st.session_state.get("_wl_layout_repaired"):
                try:
                    if _repair_watchlist_layout(ws):
                        load_watchlist_sheet.clear()
                        st.session_state.pop("_sidebar_wl_count", None)
                except Exception:
                    pass
                st.session_state["_wl_layout_repaired"] = True
            return ws, None
        # 없으면 새로 생성
        ws = sh.add_worksheet(title=_WATCHLIST_SHEET_TITLE, rows=1000, cols=max(_WL_NCOL, 10))
        _hdr_end = gspread.utils.rowcol_to_a1(1, _WL_NCOL)
        ws.update([_WATCHLIST_SHEET_COLS], range_name=f"A1:{_hdr_end}", value_input_option="USER_ENTERED")
        st.session_state["_wl_layout_repaired"] = True
        return ws, None
    except Exception as exc:
        return None, f"Watchlist 워크시트를 열거나 생성할 수 없습니다: {exc}"


@st.cache_data(ttl=60, show_spinner=False)
def load_watchlist_sheet(user_id: str) -> list[dict]:
    """Watchlist 시트에서 현재 user_id 기록 로드. 60초 캐시."""
    ws, err = open_watchlist_worksheet()
    if err or ws is None:
        return []
    try:
        vals = ws.get_all_values()
        if not vals or len(vals) < 2:
            return []
        uid_u = str(user_id).strip().upper()
        items = []
        for r in vals[1:]:
            r = (r + [""] * _WL_NCOL)[:_WL_NCOL]
            if str(r[0]).strip().upper() != uid_u:
                continue
            items.append({
                "ticker": str(r[1]).strip().upper(),
                "memo": str(r[2]).strip(),
                "alert_price": pd.to_numeric(r[3], errors="coerce") if r[3] else np.nan,
                "alert_rsi": pd.to_numeric(r[4], errors="coerce") if r[4] else np.nan,
                "alert_ma200": str(r[5]).strip().lower() == "true",
                "saved_price": pd.to_numeric(r[6], errors="coerce") if r[6] else np.nan,
                "date_added": str(r[7]).strip(),
                "stop_loss": pd.to_numeric(r[8], errors="coerce") if r[8] else np.nan,
                "target_price": pd.to_numeric(r[9], errors="coerce") if r[9] else np.nan,
            })
        return items
    except Exception:
        return []


def save_watchlist_sheet(user_id: str, items: list[dict]) -> tuple[bool, str]:
    """현재 user_id의 Watchlist 전체를 시트에 덮어쓰기."""
    ws, err = open_watchlist_worksheet()
    if err or ws is None:
        return False, err or "워크시트 열기 실패"
    try:
        vals = ws.get_all_values() or []
        uid_u = str(user_id).strip().upper()
        # 다른 유저 행 보존
        other_rows = [
            r for r in vals[1:]
            if str((r + [""])[0]).strip().upper() != uid_u
        ]
        new_rows = []
        for item in items:
            ticker = str(item.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            alert_price = item.get("alert_price", "")
            alert_price_str = str(round(float(alert_price), 4)) if pd.notna(alert_price) and alert_price != "" else ""
            alert_rsi = item.get("alert_rsi", "")
            alert_rsi_str = str(round(float(alert_rsi), 1)) if pd.notna(alert_rsi) and alert_rsi != "" else ""
            alert_ma200_str = "true" if item.get("alert_ma200") else "false"
            saved_price = item.get("saved_price", "")
            saved_price_str = str(round(float(saved_price), 4)) if pd.notna(saved_price) and saved_price != "" else ""
            stop_loss = item.get("stop_loss", "")
            stop_loss_str = str(round(float(stop_loss), 4)) if pd.notna(stop_loss) and stop_loss != "" else ""
            target_price = item.get("target_price", "")
            target_price_str = str(round(float(target_price), 4)) if pd.notna(target_price) and target_price != "" else ""
            new_rows.append([
                str(user_id).strip(),
                ticker,
                str(item.get("memo", "")).strip(),
                alert_price_str,
                alert_rsi_str,
                alert_ma200_str,
                saved_price_str,
                str(item.get("date_added", _narrative_now_et_string())).strip(),
                stop_loss_str,
                target_price_str,
            ])
        all_rows = [_WATCHLIST_SHEET_COLS] + other_rows + new_rows
        ws.clear()
        _end_cell = gspread.utils.rowcol_to_a1(len(all_rows), _WL_NCOL)
        ws.update(all_rows, range_name=f"A1:{_end_cell}", value_input_option="USER_ENTERED")
        return True, ""
    except Exception as exc:
        return False, str(exc)


def delete_from_watchlist(user_id: str, ticker: str) -> tuple[bool, str]:
    """
    Watchlist에서 특정 종목 삭제 - 해당 행만 직접 삭제 (빠름).
    """
    uid_u = str(user_id).strip().upper()
    tk_u = str(ticker).strip().upper()
    try:
        ws, err = open_watchlist_worksheet()
        if err or ws is None:
            return False, err or "워크시트 열기 실패"
        vals = ws.get_all_values() or []
        # 삭제할 행 번호 찾기 (역순으로 삭제해야 인덱스 안 밀림)
        to_del = []
        for i, r in enumerate(vals[1:], start=2):
            r = (r + [""] * _WL_NCOL)[:_WL_NCOL]
            if str(r[0]).strip().upper() == uid_u and str(r[1]).strip().upper() == tk_u:
                to_del.append(i)
        for row_idx in reversed(to_del):
            ws.delete_rows(row_idx)
        # 캐시 초기화
        load_watchlist_sheet.clear()
        st.session_state.pop("_sidebar_wl_count", None)
        st.session_state["_watchlist_alert_checked"] = False
        return True, ""
    except Exception as exc:
        return False, str(exc)


def add_to_watchlist(user_id: str, ticker: str, memo: str = "",
                     alert_rsi: float = None, alert_ma200: bool = False,
                     alert_price: float = None,
                     saved_price: float = None,
                     stop_loss: float = None,
                     target_price: float = None) -> tuple[bool, str]:
    """Watchlist에 단일 종목 추가. append_row 방식.
    saved_price를 직접 넘기면 가격 재조회 생략 (빠름).
    없으면 @st.cache_data 캐시 활용 조회.
    stop_loss/target_price는 진입 시 손절·목표가(선택).
    """
    uid = str(user_id).strip()
    tk = str(ticker).strip().upper()
    if not uid or not tk:
        return False, "유저ID 또는 티커 없음"

    # 현재가: 직접 넘긴 값 우선, 없으면 캐시 조회
    if saved_price is not None and pd.notna(saved_price):
        cur_price = float(saved_price)
    else:
        try:
            cur_price = fetch_latest_prices_for_tickers((tk,)).get(tk, np.nan)
        except Exception:
            cur_price = np.nan

    try:
        ws, err = open_watchlist_worksheet()
        if err or ws is None:
            return False, err or "워크시트 열기 실패"
        # 기존 중복 행 삭제
        vals = ws.get_all_values() or []
        uid_u = uid.upper()
        to_del = [
            i for i, r in enumerate(vals[1:], start=2)
            if (r + [""] * 2)[0].strip().upper() == uid_u
            and (r + [""] * 2)[1].strip().upper() == tk
        ]
        for row_idx in reversed(to_del):
            ws.delete_rows(row_idx)
        # 새 행 추가 (10열: ... Date_Added, Stop_Loss, Target_Price)
        _safe_append_rows(ws, [
            uid, tk,
            str(memo).strip(),
            str(round(float(alert_price), 4)) if alert_price is not None else "",
            str(round(float(alert_rsi), 1)) if alert_rsi is not None else "",
            "true" if alert_ma200 else "false",
            str(round(float(cur_price), 4)) if pd.notna(cur_price) else "",
            _narrative_now_et_string(),
            str(round(float(stop_loss), 4)) if (stop_loss is not None and pd.notna(stop_loss)) else "",
            str(round(float(target_price), 4)) if (target_price is not None and pd.notna(target_price)) else "",
        ], value_input_option="USER_ENTERED")
        # 캐시 초기화
        load_watchlist_sheet.clear()
        st.session_state.pop("_sidebar_wl_count", None)
        st.session_state["_watchlist_alert_checked"] = False
        st.session_state[f"_wl_added_{tk}"] = True
        return True, ""
    except Exception as exc:
        return False, str(exc)


def check_watchlist_alerts(items: list[dict], price_map: dict, rsi_map: dict, ma200_map: dict) -> list[dict]:
    """Watchlist 각 종목의 Alert 조건 체크. 발동된 Alert만 반환."""
    triggered = []
    for item in items:
        tk = str(item.get("ticker", "")).strip().upper()
        current_price = pd.to_numeric(price_map.get(tk), errors="coerce")
        current_rsi = pd.to_numeric(rsi_map.get(tk), errors="coerce")
        ma200 = pd.to_numeric(ma200_map.get(tk), errors="coerce")
        alert_price = pd.to_numeric(item.get("alert_price"), errors="coerce")
        alert_rsi = pd.to_numeric(item.get("alert_rsi"), errors="coerce")
        alert_ma200 = bool(item.get("alert_ma200"))

        alerts = []
        if pd.notna(alert_price) and pd.notna(current_price) and current_price <= alert_price:
            alerts.append(f"💰 목표가 도달: 현재 ${current_price:.2f} ≤ 설정 ${alert_price:.2f}")
        if pd.notna(alert_rsi) and pd.notna(current_rsi) and current_rsi <= alert_rsi:
            alerts.append(f"📉 RSI 과매도: 현재 RSI {current_rsi:.1f} ≤ 설정 {alert_rsi:.1f}")
        if alert_ma200 and pd.notna(current_price) and pd.notna(ma200):
            gap_pct = (current_price / ma200 - 1.0) * 100
            if abs(gap_pct) <= 3.0:
                alerts.append(f"📊 200일선 근접: 현재가 ${current_price:.2f} / 200일선 ${ma200:.2f} (괴리 {gap_pct:+.1f}%)")
        if alerts:
            triggered.append({"ticker": tk, "alerts": alerts, "current_price": current_price})
    return triggered


def load_watchlist():
    if not _WATCHLIST_FILE.exists():
        save_watchlist([])
        return []
    try:
        with open(_WATCHLIST_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return raw
        return []
    except Exception:
        return []


def save_watchlist(items):
    try:
        with open(_WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _narrative_parse_saved_at_utc(saved_at_str):
    if not saved_at_str:
        return None
    try:
        ts = str(saved_at_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_market_status(et_now):
    """Return current US market session label based on ET."""
    if et_now is None:
        return "🌑 Market Closed"
    if et_now.weekday() >= 5:
        return "🌑 Market Closed (Weekend)"

    m = et_now.hour * 60 + et_now.minute
    if 240 <= m <= 569:  # 04:00 ~ 09:29
        return "🌅 Pre-market"
    if 570 <= m <= 959:  # 09:30 ~ 15:59
        return "🟢 Regular Market (Open)"
    if 960 <= m <= 1199:  # 16:00 ~ 19:59
        return "🌙 After-hours"
    return "🌑 Market Closed"


def render_global_market_watch_header():
    now_utc = datetime.now(timezone.utc)
    san_antonio_now = now_utc.astimezone(_SAN_ANTONIO_TZ)
    et_now = now_utc.astimezone(_MARKET_ET_TZ)
    market_status = get_market_status(et_now)

    c1, c2, c3 = st.columns([1.3, 1.3, 1.8])
    with c1:
        st.markdown(f"**🏠 San Antonio:** `{san_antonio_now.strftime('%H:%M')}`")
    with c2:
        st.markdown(f"**🗽 New York (ET):** `{et_now.strftime('%H:%M')}`")
    with c3:
        if market_status.startswith("🟢"):
            st.markdown(f"**📊 Status:** :green[{market_status}]")
        else:
            st.markdown(f"**📊 Status:** {market_status}")

    st.divider()


def _narrative_session_label_for_et_dt(dt_et):
    """ET 기준 Ryan 루틴 세션 라벨."""
    if dt_et is None:
        return "⏱️ Unknown session"
    m = dt_et.hour * 60 + dt_et.minute
    # 04:00 ~ 09:29 / 09:30 ~ 16:00 / 16:01 ~ 20:00 / 20:01 ~ 03:59
    if 240 <= m <= 569:
        return "🌅 Pre-market Prep"
    if 570 <= m <= 960:
        return "🟢 Market Hours Analysis"
    if 961 <= m <= 1200:
        return "🔔 Daily Recap (Post-Market)"
    return "🌙 Overnight Strategy"


def narrative_session_label_at_utc(dt_utc):
    if dt_utc is None:
        return "⏱️ Unknown session"
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return _narrative_session_label_for_et_dt(dt_utc.astimezone(_MARKET_ET_TZ))


def _narrative_regime_risk_display(analysis):
    if not isinstance(analysis, dict):
        return "N/A"
    regime = analysis.get("regime")
    if isinstance(regime, dict):
        r = str(regime.get("risk", "") or "").strip()
        if r:
            return r
    return "N/A"


def _narrative_core_theme_display(analysis, max_chars=40):
    """Expander 제목용: 첫 테마 제목, 없으면 요약 앞부분."""
    if not isinstance(analysis, dict):
        return "N/A"
    themes = analysis.get("themes")
    if isinstance(themes, list) and themes:
        th0 = themes[0] if isinstance(themes[0], dict) else {}
        title = str(th0.get("title", "") or "").strip()
        if title:
            return title if len(title) <= max_chars else title[: max_chars - 1] + "…"
    summary = str(analysis.get("summary", "") or "").strip().replace("\n", " ")
    if not summary:
        return "N/A"
    return summary if len(summary) <= max_chars else summary[: max_chars - 1] + "…"


def narrative_history_expander_title(rec):
    """[MM-DD] 세션라벨 | 레짐 | 핵심테마"""
    rec = rec if isinstance(rec, dict) else {}
    dt_utc = _narrative_parse_saved_at_utc(rec.get("saved_at"))
    if dt_utc is None:
        date_part = "--"
    else:
        date_part = dt_utc.astimezone(_MARKET_ET_TZ).strftime("%m-%d")
    session_lbl = str(rec.get("session_label") or "").strip()
    if not session_lbl and dt_utc is not None:
        session_lbl = narrative_session_label_at_utc(dt_utc)
    elif not session_lbl:
        session_lbl = "⏱️ Unknown session"
    analysis = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
    src = str(analysis.get("source") or "").strip()

    if src == _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
        return f"[{date_part}] 📊 주간트렌드(7일) | 브리핑 | 티커 {len(analysis.get('precomputed_universe') or [])}개"
    if src == "wow_trend_7d":
        # 브리핑 첫 줄(제목)을 미리보기로 추출
        md = str(analysis.get("weekly_briefing_markdown") or analysis.get("wow_briefing_markdown") or "").strip()
        preview = ""
        if md:
            first_line = md.lstrip("#").split("\n")[0].strip()
            preview = first_line[:50] + ("…" if len(first_line) > 50 else "")
        return f"[{date_part}] ⚖️ 트렌드 변곡점 (이번 주 vs 저번 주)" + (f" | {preview}" if preview else "")

    regime_part = _narrative_regime_risk_display(analysis)
    theme_part = _narrative_core_theme_display(analysis)
    return f"[{date_part}] {session_lbl} | {regime_part} | {theme_part}"


def prune_narrative_history_records(records):
    """최근 14일 이내 + 최대 40건(시간순 보존 후 최신만 유지).
    weekly_portfolio_summary는 별도 탭에서 관리하므로 prune 대상에서 제외.
    """
    if not records:
        return []
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=_NARRATIVE_HISTORY_RETENTION_DAYS)
    migrated = []
    for rec in records:
        if not isinstance(rec, dict) or not isinstance(rec.get("analysis"), dict):
            continue
        # weekly_portfolio_summary는 prune/sync 대상 제외 (Weekly Summary 탭에서 별도 관리)
        if rec.get("analysis", {}).get("source") == "weekly_portfolio_summary":
            continue
        dt_utc = _narrative_parse_saved_at_utc(rec.get("saved_at"))
        if dt_utc is None:
            continue
        if dt_utc < cutoff:
            continue
        if not str(rec.get("session_label") or "").strip():
            rec = dict(rec)
            rec["session_label"] = narrative_session_label_at_utc(dt_utc)
        migrated.append(rec)
    migrated.sort(key=lambda r: _narrative_parse_saved_at_utc(r.get("saved_at")) or datetime.min.replace(tzinfo=timezone.utc))
    if len(migrated) > _NARRATIVE_HISTORY_MAX_RECORDS:
        migrated = migrated[-_NARRATIVE_HISTORY_MAX_RECORDS :]
    return migrated


def load_narrative_history_records():
    """Narratives 시트에서 현재 user_id 행만 로드. prune 후 해당 사용자 구간만 시트와 동기화."""
    st.session_state.pop("_narratives_last_sheet_error", None)
    base, err = fetch_narrative_records_from_sheet()
    if err:
        st.session_state["_narratives_last_sheet_error"] = err
        return []
    uid = str(st.session_state.get("user_id") or "").strip()
    uid_u = uid.upper()
    mine = []
    for rec in base:
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("_sheet_user_id") or "").strip().upper()
        if uid_u and rid == uid_u:
            mine.append(rec)
    if not uid_u:
        return []
    pruned = prune_narrative_history_records(mine)
    if pruned != mine:
        ok, merr = save_narrative_history_records_merge_user(uid, pruned)
        if not ok and merr:
            try:
                st.warning(merr)
            except Exception:
                pass
    return pruned


def append_narrative_history_record(analysis_dict, language: str):
    if not isinstance(analysis_dict, dict) or not analysis_dict:
        return
    uid = str(st.session_state.get("user_id") or "").strip()
    if not uid:
        st.error("로그인 user_id 가 없습니다. 다시 로그인해 주세요.")
        return
    now_utc = datetime.now(timezone.utc)
    session_label = narrative_session_label_at_utc(now_utc)
    record = {
        "saved_at": now_utc.isoformat(),
        "session_label": session_label,
        "language": str(language or "ko"),
        "analysis": analysis_dict,
    }
    ok, err = append_narrative_row_to_sheet(_narrative_record_to_sheet_row(record, uid))
    if not ok:
        st.error(f"Google 시트 `Quant_DB` / `Narratives` 저장에 실패했습니다: {err}")
        return
    st.session_state.pop("_narratives_cached_records", None)
    st.session_state.pop("_narratives_cache_time", None)
    load_narrative_history_records()


def append_weekly_trend_narrative_record(briefing_markdown: str, language: str, week_recs: list):
    """
    1.5 주간 트렌드 Gemini 결과를 `Quant_DB` / `Narratives` 시트에 남겨 1.6 스캐너가 동일 기록에서 조회할 수 있게 한다.
    유니버스 = 최근 7일 스냅샷 테마 티커 합집합 + 브리핑 본문에서 파싱한 티커.
    """
    md = str(briefing_markdown or "").strip()
    if not md:
        return
    ordered = []
    seen = set()
    for rec in week_recs or []:
        if not isinstance(rec, dict):
            continue
        a = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
        for tk in universe_tickers_from_theme_analysis(a):
            if tk not in seen:
                seen.add(tk)
                ordered.append(tk)
    for tk in parse_tickers_from_text(md):
        if tk not in seen:
            seen.add(tk)
            ordered.append(tk)

    now_utc = datetime.now(timezone.utc)
    analysis_dict = {
        "source": _NARRATIVE_RECORD_SOURCE_WEEKLY_7D,
        "themes": [],
        "regime": {},
        "rotation": f"최근 7일 롤링 윈도우 · 스냅샷 {len(week_recs or [])}건 기반 주간 트렌드 브리핑",
        "summary": md,
        "weekly_briefing_markdown": md,
        "precomputed_universe": filter_scanner_ticker_list(ordered),
    }
    record = {
        "saved_at": now_utc.isoformat(),
        "session_label": "📊 주간 트렌드 (최근 7일 교집합)",
        "language": str(language or "ko"),
        "analysis": analysis_dict,
    }
    uid = str(st.session_state.get("user_id") or "").strip()
    if not uid:
        st.error("로그인 user_id 가 없습니다. 다시 로그인해 주세요.")
        return
    ok, err = append_narrative_row_to_sheet(_narrative_record_to_sheet_row(record, uid))
    if not ok:
        st.error(f"Google 시트 `Quant_DB` / `Narratives` 저장에 실패했습니다: {err}")
        return
    st.session_state.pop("_narratives_cached_records", None)
    st.session_state.pop("_narratives_cache_time", None)
    load_narrative_history_records()


def append_wow_trend_narrative_record(briefing_markdown: str, language: str,
                                       this_week_recs: list, last_week_recs: list):
    """
    WoW(트렌드 변곡점) Gemini 결과를 Quant_DB / Narratives 시트에 저장.
    source = 'wow_trend_7d', session_label = '⚖️ 트렌드 변곡점 (이번 주 vs 저번 주)'
    """
    md = str(briefing_markdown or "").strip()
    if not md:
        return

    # 이번 주 + 저번 주 티커 합집합 추출
    ordered, seen = [], set()
    for rec in (this_week_recs or []) + (last_week_recs or []):
        if not isinstance(rec, dict):
            continue
        a = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
        for tk in universe_tickers_from_theme_analysis(a):
            if tk not in seen:
                seen.add(tk)
                ordered.append(tk)
    for tk in parse_tickers_from_text(md):
        if tk not in seen:
            seen.add(tk)
            ordered.append(tk)

    now_utc = datetime.now(timezone.utc)
    analysis_dict = {
        "source": "wow_trend_7d",
        "themes": [],
        "regime": {},
        "rotation": (
            f"WoW 변곡점 분석 · 이번 주 {len(this_week_recs or [])}건 "
            f"vs 저번 주 {len(last_week_recs or [])}건 기반"
        ),
        "summary": md,
        "weekly_briefing_markdown": md,
        "precomputed_universe": filter_scanner_ticker_list(ordered),
    }
    record = {
        "saved_at": now_utc.isoformat(),
        "session_label": "⚖️ 트렌드 변곡점 (이번 주 vs 저번 주)",
        "language": str(language or "ko"),
        "analysis": analysis_dict,
    }
    uid = str(st.session_state.get("user_id") or "").strip()
    if not uid:
        st.error("로그인 user_id 가 없습니다. 다시 로그인해 주세요.")
        return
    ok, err = append_narrative_row_to_sheet(_narrative_record_to_sheet_row(record, uid))
    if not ok:
        st.error(f"Google 시트 `Quant_DB` / `Narratives` 저장에 실패했습니다: {err}")
        return
    st.session_state.pop("_narratives_cached_records", None)
    st.session_state.pop("_narratives_cache_time", None)
    load_narrative_history_records()
    st.success("✅ 트렌드 변곡점 분석이 Narratives 시트에 저장되었습니다.")


def clear_narrative_history_file_and_session():
    save_narrative_history_records([])
    st.session_state["narrative_history"] = []
    st.session_state["narrative_history_disk_records"] = []
    st.session_state["market_narrative_data"] = {}
    st.session_state["current_view"] = {}
    for k in ("_narratives_cached_records", "_narratives_cache_time", "_narratives_force_sheet_refresh", "_narrative_past_show_n"):
        st.session_state.pop(k, None)
    if "_narrative_persist_loaded_v2" in st.session_state:
        del st.session_state["_narrative_persist_loaded_v2"]


def render_narrative_history_compact(analysis: dict):
    """Render a concise past-analysis block inside expanders."""
    if not isinstance(analysis, dict):
        st.write("_데이터 형식 오류_")
        return

    src = str(analysis.get("source") or "").strip()

    # ── 주간 트렌드 (weekly_trend_7d) ────────────────────────────────────
    if src == _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
        st.markdown("**유형:** 주간 트렌드 브리핑 (최근 7일 교집합)")
        uni = analysis.get("precomputed_universe")
        if isinstance(uni, list) and uni:
            st.caption("스캐너 연동 티커 풀: " + ", ".join(str(x) for x in uni[:40]) + (" …" if len(uni) > 40 else ""))
        md = str(analysis.get("weekly_briefing_markdown") or analysis.get("summary") or "").strip()
        if md:
            with st.expander("브리핑 본문", expanded=False):
                st.markdown(md[:12000])
        return

    # ── 트렌드 변곡점 (wow_trend_7d) ─────────────────────────────────────
    if src == "wow_trend_7d":
        st.markdown("**유형:** ⚖️ 트렌드 변곡점 분석 (이번 주 vs 저번 주)")
        md = str(
            analysis.get("weekly_briefing_markdown")
            or analysis.get("wow_briefing_markdown")
            or analysis.get("summary")
            or ""
        ).strip()
        if md:
            with st.expander("변곡점 브리핑 본문 보기", expanded=False):
                st.markdown(md[:12000])
        else:
            st.caption("저장된 브리핑 본문이 없습니다.")
        return

    # ── 일반 시장 내러티브 (market_narrative) ────────────────────────────
    regime = analysis.get("regime") if isinstance(analysis.get("regime"), dict) else {}
    summary = analysis.get("summary") or ""
    themes = analysis.get("themes") if isinstance(analysis.get("themes"), list) else []

    lines = []
    if regime:
        lines.append(
            f"- **Risk / G-V / Liquidity**: {regime.get('risk', 'N/A')} · "
            f"{regime.get('growth_value', 'N/A')} · {regime.get('liquidity', 'N/A')}"
        )
    if isinstance(themes, list) and themes:
        titles = []
        for th in themes[:5]:
            th = th if isinstance(th, dict) else {}
            t = str(th.get("title", "") or "").strip()
            if t:
                titles.append(t)
        if titles:
            lines.append("- **Themes**: " + ", ".join(titles))
    st.markdown("\n".join(lines) if lines else "_요약 정보 없음_")
    if summary:
        with st.expander("요약 전문 보기", expanded=False):
            st.markdown(str(summary))


def hydrate_narrative_from_disk_once():
    """Quant_DB / Narratives 시트에서 기록을 세션으로 불러옵니다. 짧은 간격으로 API를 재호출하지 않도록 캐시합니다."""
    import time as _time

    now_ts = _time.time()
    cache_ttl = 45.0
    last_ts = float(st.session_state.get("_narratives_cache_time") or 0)
    force = st.session_state.pop("_narratives_force_sheet_refresh", None)
    if (
        not force
        and st.session_state.get("_narratives_cached_records") is not None
        and (now_ts - last_ts) < cache_ttl
    ):
        records = list(st.session_state["_narratives_cached_records"])
    else:
        records = load_narrative_history_records()
        err = st.session_state.pop("_narratives_last_sheet_error", None)
        if err:
            st.error(f"내러티브 기록을 Google 시트(`Quant_DB` / `Narratives`)에서 불러오지 못했습니다: {err}")
            records = []
        st.session_state["_narratives_cached_records"] = list(records)
        st.session_state["_narratives_cache_time"] = now_ts

    st.session_state["narrative_history_disk_records"] = records
    st.session_state["narrative_history"] = [r["analysis"] for r in records if isinstance(r.get("analysis"), dict)]

    if not st.session_state.get("_narrative_persist_loaded_v2"):
        cv = st.session_state.get("current_view")
        # 가장 최근 저장 레코드를 source 종류 무관하게 선택
        pick = records[-1] if records else None
        if (not isinstance(cv, dict) or not cv) and pick:
            picked_analysis = pick.get("analysis", {})
            st.session_state["current_view"] = picked_analysis
            st.session_state["current_view_language"] = pick.get("language", "ko")
            # source 타입 복원
            src = str(picked_analysis.get("source") or "market_narrative")
            if src not in ("weekly_trend_7d", "wow_trend_7d"):
                src = "market_narrative"
            st.session_state["current_view_source"] = src
            # wow/weekly 는 narrative_timeseries_briefing 도 복원
            if src in ("weekly_trend_7d", "wow_trend_7d"):
                briefing_md = str(
                    picked_analysis.get("weekly_briefing_markdown")
                    or picked_analysis.get("wow_briefing_markdown")
                    or picked_analysis.get("summary")
                    or ""
                ).strip()
                if briefing_md:
                    title = (
                        "📊 주간 트렌드 추출 (최근 7일)" if src == "weekly_trend_7d"
                        else "⚖️ 트렌드 변곡점 (이번 주 vs 저번 주)"
                    )
                    existing_ts = st.session_state.get("narrative_timeseries_briefing")
                    if not (isinstance(existing_ts, dict) and existing_ts.get("markdown")):
                        fc_kind = "wow" if src == "wow_trend_7d" else "weekly"
                        # 복원 시 팩트체크 재계산
                        factcheck_df = pd.DataFrame()
                        try:
                            tickers_by_cat = extract_factcheck_tickers_from_briefing(briefing_md, kind=fc_kind)
                            if tickers_by_cat.get("all"):
                                if fc_kind == "wow":
                                    factcheck_df = compute_narrative_factcheck_wow_returns(tickers_by_cat)
                                else:
                                    factcheck_df = compute_narrative_factcheck_weekly_returns(tickers_by_cat)
                        except Exception:
                            factcheck_df = pd.DataFrame()
                        st.session_state["narrative_timeseries_briefing"] = {
                            "title": title,
                            "markdown": briefing_md,
                            "factcheck_kind": fc_kind,
                            "factcheck_df": factcheck_df,
                        }
        st.session_state["_narrative_persist_loaded_v2"] = True


@st.cache_data(ttl=60, show_spinner=False)
def fetch_latest_prices_for_tickers(tickers: tuple) -> dict:
    """현재가 조회 — FMP stable batch-quote 우선, 누락분은 단일 quote, 그래도 없으면 historical 마지막 종가로 fallback.

    stable API는 여러 종목을 /batch-quote?symbols=A,B,C 형식으로 받는다.
    (구형 v3의 /quote/A,B,C 경로 형식은 /stable 에서 동작하지 않아 빈 값이 온다.)
    """
    clean_tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]
    clean_tickers = list(dict.fromkeys(clean_tickers))
    if not clean_tickers:
        return {}
    k = _fmp_key()
    if not k:
        return {}

    price_map = {}

    def _extract(item) -> None:
        if not isinstance(item, dict):
            return
        sym = str(item.get("symbol") or "").strip().upper()
        price = to_float(item.get("price") or item.get("previousClose"))
        if sym and pd.notna(price):
            price_map[sym] = price

    # ── 1차: stable batch-quote (실시간 호가, 한 번에 여러 종목) ─────────────
    try:
        symbols = ",".join(clean_tickers)
        r = requests.get(
            f"{_FMP_BASE}/batch-quote?symbols={symbols}&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                for item in data:
                    _extract(item)
    except Exception:
        pass

    # ── 2차: 누락 종목은 단일 quote?symbol= 로 폴백 ─────────────────────────
    missing = [tk for tk in clean_tickers if tk not in price_map or pd.isna(price_map.get(tk))]
    for tk in missing:
        try:
            r = requests.get(
                f"{_FMP_BASE}/quote?symbol={tk}&apikey={k}",
                timeout=_FMP_TIMEOUT
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    _extract(data[0])
        except Exception:
            pass

    # ── 3차 fallback: 그래도 없으면 historical 마지막 종가 ──────────────────
    missing = [tk for tk in clean_tickers if tk not in price_map or pd.isna(price_map.get(tk))]
    for tk in missing:
        try:
            hist = _fmp_price_history(tk, limit=5)
            if not hist.empty and "Close" in hist.columns:
                last_close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
                if not last_close.empty:
                    price_map[tk] = float(last_close.iloc[-1])
                    continue
        except Exception:
            pass
        price_map[tk] = np.nan  # 최종 실패

    return price_map


# 1.5 / 1.6 공통: 마크다운·설명문에서 잘못 딴 가짜 티커(EXPANDING 등) 원천 차단
_SCANNER_TICKER_BLACKLIST = frozenset(
    {
        "EXPANDING",
        "WINNER",
        "WINNERS",
        "WEEKLY",
        "THEME",
        "TOP",
        "AND",
        "PART",
    }
)
_SCANNER_TICKER_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9.-]*$")


def is_valid_scanner_ticker(ticker: str) -> bool:
    """스캐너·내러티브 유니버스에 올릴 수 있는 티커 심볼만 허용."""
    t = str(ticker or "").strip().upper()
    if not t or t in _SCANNER_TICKER_BLACKLIST:
        return False
    if len(t) <= 1 or len(t) > 5:
        return False
    if not _SCANNER_TICKER_TOKEN_RE.fullmatch(t):
        return False
    core = re.sub(r"[^A-Z]", "", t)
    if not core or not any(ch.isalpha() for ch in core):
        return False
    return True


def filter_scanner_ticker_list(tickers):
    """순서 유지·중복 제거·검증 통과 심볼만 반환."""
    out = []
    seen = set()
    for x in tickers or []:
        t = str(x or "").strip().upper()
        if not is_valid_scanner_ticker(t):
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _parse_narrative_tickers_csv(csv: str) -> list:
    """쉼표/줄바꿈 구분 문자열 → strip·대문자·검증된 티커 리스트."""
    parts = []
    for piece in str(csv or "").replace("\n", ",").split(","):
        t = str(piece).strip().upper()
        if t:
            parts.append(t)
    return filter_scanner_ticker_list(parts)


def parse_tickers_from_text(text):
    raw_text = str(text or "").upper()
    found = re.findall(r"\b[A-Z][A-Z0-9\.\-]{0,9}\b", raw_text)
    return filter_scanner_ticker_list(found)


# =============================================================================
# 내러티브 팩트 체크 (Price Action Check)
# 1.5 주간 트렌드 / 변곡점 분석 결과에 실제 FMP 수익률을 덧붙여 검증.
# =============================================================================

# 1.5 주간 트렌드 프롬프트가 강제하는 두 섹션 헤더 매처
_FACTCHECK_WINNERS_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*🏆\s*Weekly\s+Winners"          # 고정 헤더
    r"|#{1,6}.*(?:winners?|대장주|지속\s*티커|주간\s*대장)"  # 한국어 헤더
    r"|지속\s*티커\s*[\(（]?[Ww]inners?[\)）]?"           # '지속 티커 (Winners):' 형식
    r").*$",
    re.MULTILINE | re.IGNORECASE,
)
_FACTCHECK_EXPANDING_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*🚀\s*Weekly\s+Expanding\s+To"    # 고정 헤더
    r"|#{1,6}.*(?:expanding|후발|확장\s*수혜)"           # 한국어 헤더
    r").*$",
    re.MULTILINE | re.IGNORECASE,
)
# WoW 고정 헤더 + 한국어 숫자형 헤더 모두 허용
_FACTCHECK_EMERGING_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*🌱\s*Emerging"                   # 고정 헤더
    r"|#{1,6}.*(?:emerging|부상|새로\s*나)"
    r"|\d+[\)）\.]\s*.*(?:emerging|부상|새로\s*나)"      # '3) Emerging' 형식
    r").*$",
    re.MULTILINE | re.IGNORECASE,
)
_FACTCHECK_FADING_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*🥀\s*Fading"                     # 고정 헤더
    r"|#{1,6}.*(?:fading|사그|약화|소멸)"
    r"|🥀\s*Fading"                                     # 이모지만 있는 소제목
    r"|\d+[\)）\.]\s*.*(?:fading|사그|약화|소멸)"
    r").*$",
    re.MULTILINE | re.IGNORECASE,
)
_FACTCHECK_INTERSECTION_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}.*(?:교집합|intersection|살아남은)"
    r"|\d+[\)）\.]\s*.*(?:교집합|intersection|살아남은)"  # '1) 교집합' 형식
    r").*$",
    re.MULTILINE | re.IGNORECASE,
)

_FACTCHECK_BOLD_TICKER_RE = re.compile(r"\*\*\s*([A-Z][A-Z0-9.\-]{0,9})\s*\*\*")
_FACTCHECK_PLAIN_TICKER_RE = re.compile(r"\b([A-Z][A-Z0-9.\-]{1,9})\b")

# 본문에서 티커처럼 보이지만 실제로는 일반 약어인 단어들 — 잡음 제거용
_FACTCHECK_NON_TICKER_WORDS = frozenset(
    {
        "AI", "AGI", "ML", "API", "SDK", "ETF", "ETFS", "US", "USA", "UK", "EU",
        "ASIA", "FED", "FOMC", "CPI", "PPI", "PCE", "GDP", "ECB", "BOJ", "OPEC",
        "WTI", "ROI", "ROE", "EPS", "PER", "PBR", "PEG", "YTD", "MTD", "QOQ",
        "YOY", "MOM", "HBM", "ASIC", "CEO", "CFO", "CTO", "COO", "IPO", "SPO",
        "DCF", "ESG", "DRAM", "DDR", "LPDDR", "TAM", "SAM", "FY", "CY",
        "Q1", "Q2", "Q3", "Q4", "H1", "H2",
        "TO", "FROM", "AT", "ON", "OFF", "BY", "FOR", "AND", "OR", "BUT",
        "WITH", "VS", "THE", "TBD", "TBA", "NA", "NAN",
        "WOW", "DOD", "WEEKLY", "WINNERS", "EXPANDING", "COPILOT", "GEMINI",
        # WoW 한국어 브리핑에서 자주 잡히는 비티커 대문자
        "RISK", "TIGHTENING", "DRIFT", "NARRATIVE", "WEEK", "SECTOR",
        "ROTATION", "FADING", "EMERGING", "INTERSECTION", "PART", "SUMMARY",
        "EXECUTIVE", "AR", "VR", "DX", "AR",
    }
)


def _factcheck_slice_section(markdown: str, header_re: "re.Pattern") -> str:
    """주어진 헤더 정규식 매치 위치부터 다음 섹션 헤더 전까지 본문 반환.
    ## 마크다운 헤더 + 숫자형 섹션(1), 2) 등) 모두 인식.
    """
    md = str(markdown or "")
    m = header_re.search(md)
    if not m:
        return ""
    start = m.end()
    # 다음 섹션 구분자: ## 헤더 또는 숫자형 섹션 시작 또는 🥀/🌱 이모지 소제목
    nxt = re.search(
        r"(?:^\s*#{1,6}\s+\S+|^\s*\d+[\)）\.]\s+\S|^\s*[🥀🌱🚀🏆]\s+\S)",
        md[start:],
        re.MULTILINE,
    )
    end = start + (nxt.start() if nxt else len(md) - start)
    return md[start:end]


def _factcheck_pick_tickers_from_section(section_md: str) -> list:
    """섹션 본문에서 티커를 추출.
    1순위: `**TICKER**` 볼드 매치 (신규 프롬프트 고정 형식)
    2순위: 라인 내 모든 대문자 토큰 — 쉼표/공백 구분 나열 형식도 전부 수집
    """
    out = []
    seen = set()
    for raw_line in str(section_md or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 1순위: 볼드 티커 — 한 라인에 여러 개 있어도 모두 수집
        bold_tickers = []
        for bm in _FACTCHECK_BOLD_TICKER_RE.finditer(line):
            tk = bm.group(1).upper().strip(".-")
            if tk and tk not in _FACTCHECK_NON_TICKER_WORDS and tk not in seen:
                seen.add(tk)
                bold_tickers.append(tk)
        if bold_tickers:
            out.extend(bold_tickers)
            continue
        # 2순위: 일반 대문자 토큰 — 라인 내 모든 후보 수집 (쉼표 구분 나열 대응)
        for tok in _FACTCHECK_PLAIN_TICKER_RE.findall(line):
            tk = tok.upper().strip(".-")
            if not (2 <= len(tk) <= 6):
                continue
            if tk in _FACTCHECK_NON_TICKER_WORDS:
                continue
            if tk in seen:
                continue
            seen.add(tk)
            out.append(tk)
    return out


def extract_factcheck_tickers_from_briefing(markdown: str, kind: str = "weekly") -> dict:
    """주간 트렌드(weekly) 또는 변곡점 분석(wow) 브리핑에서 카테고리별 티커 리스트를 추출.

    반환 dict 키:
      - 'weekly': {'winners': [...], 'expanding': [...], 'all': [...]}
      - 'wow'   : {'winners': [...], 'expanding': [...],
                   'emerging': [...], 'fading': [...], 'intersection': [...], 'all': [...]}
    """
    md = str(markdown or "")
    winners = _factcheck_pick_tickers_from_section(
        _factcheck_slice_section(md, _FACTCHECK_WINNERS_HEADER_RE)
    )
    expanding = _factcheck_pick_tickers_from_section(
        _factcheck_slice_section(md, _FACTCHECK_EXPANDING_HEADER_RE)
    )

    extra = {}
    if kind == "wow":
        extra["emerging"] = _factcheck_pick_tickers_from_section(
            _factcheck_slice_section(md, _FACTCHECK_EMERGING_HEADER_RE)
        )
        extra["fading"] = _factcheck_pick_tickers_from_section(
            _factcheck_slice_section(md, _FACTCHECK_FADING_HEADER_RE)
        )
        extra["intersection"] = _factcheck_pick_tickers_from_section(
            _factcheck_slice_section(md, _FACTCHECK_INTERSECTION_HEADER_RE)
        )

    # 두 핵심 섹션이 모두 비었으면 본문 전체에서 볼드 티커라도 긁어와 폴백
    if not winners and not expanding and not any(extra.values()):
        seen = set()
        for m in _FACTCHECK_BOLD_TICKER_RE.finditer(md):
            tk = m.group(1).upper().strip(".-")
            if 2 <= len(tk) <= 6 and tk not in _FACTCHECK_NON_TICKER_WORDS and tk not in seen:
                seen.add(tk)
                winners.append(tk)

    all_t = []
    seen = set()
    for bucket in (winners, expanding, *(extra.get(k, []) for k in ("emerging", "fading", "intersection"))):
        for t in bucket:
            if t not in seen:
                seen.add(t)
                all_t.append(t)

    out = {"winners": winners, "expanding": expanding, "all": all_t}
    out.update(extra)
    return out


def _factcheck_to_yahoo_symbol(ticker: str) -> str:
    """야후 파이낸스 심볼 표기로 변환.
    SEC/거래소 표기의 클래스 구분자 `.`(예: BRK.B, BF.B)는 야후에서 `-`(BRK-B, BF-B).
    이미 `-`인 종목(MOG-A 등)은 그대로 유지."""
    t = str(ticker or "").strip().upper()
    if "." in t and "-" not in t:
        return t.replace(".", "-")
    return t


def _factcheck_download_closes(tickers, period: str = "60d") -> pd.DataFrame:
    """팩트 체크용 종가 시계열 — FMP 사용. 누락 티커는 NaN 컬럼 보존."""
    clean = [str(t).strip().upper() for t in tickers if str(t).strip()]
    clean = list(dict.fromkeys(clean))
    if not clean:
        return pd.DataFrame()
    # period 문자열 → limit 변환
    period_limit = {"30d": 30, "60d": 65, "90d": 95, "6mo": 130, "1y": 252}
    limit = period_limit.get(period, 65)
    try:
        close_df = _fmp_batch_to_close_df(clean, limit=limit)
        if close_df is None or close_df.empty:
            return pd.DataFrame({t: pd.Series(dtype=float) for t in clean})
        for t in clean:
            if t not in close_df.columns:
                close_df[t] = np.nan
        return close_df[clean]
    except Exception:
        return pd.DataFrame({t: pd.Series(dtype=float) for t in clean})


def _factcheck_return_over_window(series, latest_offset: int, base_offset: int):
    """series.iloc[latest_offset] / series.iloc[base_offset] - 1, %.
    오프셋은 음수(거래일 인덱스 from end). 데이터 부족 시 NaN."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    need = max(abs(latest_offset), abs(base_offset)) + 1
    if len(s) < need:
        return np.nan
    a = float(s.iloc[latest_offset])
    b = float(s.iloc[base_offset])
    if not np.isfinite(a) or not np.isfinite(b) or b == 0:
        return np.nan
    return (a / b - 1.0) * 100.0


_FACTCHECK_CATEGORY_LABELS = {
    "winners": "🏆 Weekly Winner",
    "expanding": "🚀 Expanding To",
    "emerging": "🌱 Emerging",
    "fading": "🥀 Fading",
    "intersection": "🔁 Intersection",
}


def _factcheck_flatten_buckets(tickers_by_cat: dict, categories: tuple) -> list:
    flat = []
    for cat in categories:
        for t in tickers_by_cat.get(cat) or []:
            flat.append((cat, t))
    return flat


def compute_narrative_factcheck_weekly_returns(tickers_by_cat: dict) -> pd.DataFrame:
    """주간 트렌드 팩트 체크: 최근 5거래일 수익률(%) 한 컬럼."""
    flat = _factcheck_flatten_buckets(tickers_by_cat, ("winners", "expanding"))
    if not flat:
        return pd.DataFrame()
    unique_tickers = list(dict.fromkeys([t for _, t in flat]))
    closes = _factcheck_download_closes(unique_tickers, period="30d")
    rows = []
    for cat, t in flat:
        if closes.empty or t not in closes.columns:
            r1w = np.nan
        else:
            r1w = calculate_period_return(closes[t], 5)
        rows.append(
            {
                "Category": _FACTCHECK_CATEGORY_LABELS.get(cat, cat),
                "Ticker": t,
                "1W Return (%)": r1w,
            }
        )
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["Ticker"], keep="first").reset_index(drop=True)
    return df


def compute_narrative_factcheck_wow_returns(tickers_by_cat: dict) -> pd.DataFrame:
    """변곡점(WoW) 팩트 체크: 저번 주(-10→-5), 이번 주(-5→0) 수익률 + Δ(%p)."""
    flat = _factcheck_flatten_buckets(
        tickers_by_cat,
        ("intersection", "emerging", "fading", "winners", "expanding"),
    )
    if not flat:
        return pd.DataFrame()
    unique_tickers = list(dict.fromkeys([t for _, t in flat]))
    closes = _factcheck_download_closes(unique_tickers, period="60d")
    rows = []
    for cat, t in flat:
        if closes.empty or t not in closes.columns:
            last_w = this_w = delta = np.nan
        else:
            s = closes[t]
            this_w = _factcheck_return_over_window(s, latest_offset=-1, base_offset=-6)
            last_w = _factcheck_return_over_window(s, latest_offset=-6, base_offset=-11)
            if np.isfinite(this_w) and np.isfinite(last_w):
                delta = this_w - last_w
            else:
                delta = np.nan
        rows.append(
            {
                "Category": _FACTCHECK_CATEGORY_LABELS.get(cat, cat),
                "Ticker": t,
                "Last Week (%)": last_w,
                "This Week (%)": this_w,
                "Δ (%p)": delta,
            }
        )
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["Ticker"], keep="first").reset_index(drop=True)
    return df


def _factcheck_color_pos_neg(value):
    """양수=초록, 음수=빨강, 0/NaN=중립."""
    if value is None:
        return "color: #999"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return ""
    if not np.isfinite(x):
        return "color: #999"
    if x > 0:
        return "color: #16a34a; font-weight: 600"
    if x < 0:
        return "color: #dc2626; font-weight: 600"
    return ""


def render_narrative_factcheck_table(df: pd.DataFrame, kind: str = "weekly") -> None:
    """팩트 체크 DataFrame을 Streamlit에 색상 포함 표로 렌더링.
    수치 결측은 'N/A'로 표시. kind: 'weekly' | 'wow'."""
    if df is None or df.empty:
        st.caption("팩트 체크 대상 티커가 추출되지 않았습니다.")
        return

    pct_cols = [c for c in df.columns if c.endswith("(%)") or c.endswith("(%p)")]
    fmt = {}
    for c in pct_cols:
        suffix = "%p" if c.endswith("(%p)") else "%"
        fmt[c] = lambda v, s=suffix: ("N/A" if pd.isna(v) else f"{v:+.2f}{s}")

    styler = df.style.format(fmt, na_rep="N/A")
    # pandas 2.1+의 Styler.map 우선 사용, 구버전이면 applymap으로 폴백
    color_apply = getattr(styler, "map", None) or styler.applymap
    color_apply(_factcheck_color_pos_neg, subset=pct_cols)

    miss = int(df[pct_cols].isna().any(axis=1).sum()) if pct_cols else 0
    note_kind = "주간(최근 5거래일)" if kind == "weekly" else "WoW(저번 주 vs 이번 주, 각 5거래일)"
    note = f"기간: **{note_kind}** · 종가 기준(자동 조정) · 데이터 부족 티커는 N/A."
    if miss:
        note += f" (결측 {miss}건)"
    st.caption(note)
    st.dataframe(styler, width="stretch", hide_index=True)


def universe_tickers_from_theme_analysis(analysis):
    """themes 기준 Winners + Emerging 합집합(순서: Winners 먼저, 중복 제거)."""
    w = winners_tickers_from_theme_analysis(analysis)
    e = emerging_tickers_from_theme_analysis(analysis)
    out = []
    seen = set()
    for t in w + e:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _non_weekly_narratives_latest_first(records):
    """주간 전용 레코드 제외 후 saved_at 기준 최신이 앞."""
    pool = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        a = r.get("analysis") if isinstance(r.get("analysis"), dict) else {}
        if a.get("source") == _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
            continue
        pool.append(r)
    return sorted(
        pool,
        key=lambda r: _narrative_parse_saved_at_utc(r.get("saved_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def get_latest_narrative_sheet_winners_tickers_only():
    """현재 유저의 최신 일반 내러티브에서 시트 `Winners` 열(또는 JSON themes의 winners)만."""
    records = load_narrative_history_records()
    if not records:
        return [], {}
    for rec in _non_weekly_narratives_latest_first(records):
        analysis = rec.get("analysis", {}) if isinstance(rec, dict) else {}
        if not isinstance(analysis, dict):
            continue
        wc = str(rec.get("_sheet_winners_csv") or "").strip()
        if wc:
            parsed = _parse_narrative_tickers_csv(wc)
            if parsed:
                return parsed, analysis
        wj = winners_tickers_from_theme_analysis(analysis)
        if wj:
            return wj, analysis
    return [], {}


def get_latest_narrative_sheet_emerging_tickers_only():
    """현재 유저의 최신 일반 내러티브에서 시트 `Emerging` 열(또는 JSON expanding_to)만."""
    records = load_narrative_history_records()
    if not records:
        return [], {}
    for rec in _non_weekly_narratives_latest_first(records):
        analysis = rec.get("analysis", {}) if isinstance(rec, dict) else {}
        if not isinstance(analysis, dict):
            continue
        ec = str(rec.get("_sheet_emerging_csv") or "").strip()
        if ec:
            parsed = _parse_narrative_tickers_csv(ec)
            if parsed:
                return parsed, analysis
        ej = emerging_tickers_from_theme_analysis(analysis)
        if ej:
            return ej, analysis
    return [], {}


def get_latest_narrative_target_universe():
    """
    가장 최근 일반 내러티브에서 시트 `Winners`·`Emerging` 합집합을 우선 사용하고,
    없으면 JSON themes에서 Winners+Expanding 전체를 사용한다.
    """
    records = load_narrative_history_records()
    if not records:
        return [], {}

    for rec in _non_weekly_narratives_latest_first(records):
        analysis = rec.get("analysis", {}) if isinstance(rec, dict) else {}
        if not isinstance(analysis, dict):
            continue
        wc = str(rec.get("_sheet_winners_csv") or "").strip()
        ec = str(rec.get("_sheet_emerging_csv") or "").strip()
        merged = ",".join(x for x in [wc, ec] if x)
        if merged:
            parsed = _parse_narrative_tickers_csv(merged)
            if parsed:
                return parsed, analysis
        u = universe_tickers_from_theme_analysis(analysis)
        if u:
            return u, analysis

    for rec in _non_weekly_narratives_latest_first(records):
        analysis = rec.get("analysis", {}) if isinstance(rec, dict) else {}
        if not isinstance(analysis, dict):
            continue
        return universe_tickers_from_theme_analysis(analysis), analysis

    return [], {}


def get_latest_weekly_trend_scan_universe_and_analysis():
    """
    `Quant_DB` / `Narratives` 시트에서 가장 최근 `weekly_trend_7d` 레코드를 찾아 유니버스·분석 dict를 반환.
    (레거시: source 없이 weekly_briefing_markdown만 있는 경우도 마크다운에서 티커 추출 시도)
    """
    records = load_narrative_history_records()
    if not records:
        return [], {}

    sorted_recs = sorted(
        records,
        key=lambda r: _narrative_parse_saved_at_utc(r.get("saved_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for rec in sorted_recs:
        analysis = rec.get("analysis", {}) if isinstance(rec, dict) else {}
        if not isinstance(analysis, dict):
            continue
        if analysis.get("source") != _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
            continue

        wc = str(rec.get("_sheet_winners_csv") or "").strip()
        ec = str(rec.get("_sheet_emerging_csv") or "").strip()
        leg = str(rec.get("_sheet_tickers_csv") or "").strip()
        merged_csv = ",".join(x for x in [wc, ec, leg] if x)
        if merged_csv:
            parsed = _parse_narrative_tickers_csv(merged_csv)
            if parsed:
                return parsed, analysis

        ordered = []
        seen = set()
        pre = analysis.get("precomputed_universe")
        if isinstance(pre, list):
            for t in pre:
                tk = str(t).strip().upper()
                if not is_valid_scanner_ticker(tk) or tk in seen:
                    continue
                seen.add(tk)
                ordered.append(tk)
        md = str(analysis.get("weekly_briefing_markdown") or analysis.get("summary") or "")
        for tk in parse_tickers_from_text(md):
            if tk not in seen:
                seen.add(tk)
                ordered.append(tk)
        return ordered, analysis

    return [], {}


_OPPORTUNITY_SCANNER_SECTOR_ETFS = [
    ("기술 (Tech · XLK)", "XLK"),
    ("바이오 (Bio · XBI)", "XBI"),
    ("에너지 (Energy · XLE)", "XLE"),
    ("금융 (Finance · XLF)", "XLF"),
    ("헬스케어 (Healthcare · XLV)", "XLV"),
    ("반도체 (Semis · SOXX)", "SOXX"),
    ("통신 (Comm · XLC)", "XLC"),
    ("임의소비재 (Discretionary · XLY)", "XLY"),
    ("유틸리티 (Utilities · XLU)", "XLU"),
    ("소재 (Materials · XLB)", "XLB"),
    ("부동산 (REIT · XLRE)", "XLRE"),
    ("산업재 (Industrials · XLI)", "XLI"),
]


def build_opportunity_scanner_universe_from_direct_selection(sector_display_labels, manual_ticker_text):
    """
    섹터별 대표 ETF의 top holdings + 사용자 직접 입력 티커를 합쳐 유니버스를 만든다.
    """
    ordered = []
    seen = set()
    label_to_etf = dict(_OPPORTUNITY_SCANNER_SECTOR_ETFS)

    for label in sector_display_labels or []:
        etf = label_to_etf.get(label)
        if not etf:
            continue
        hdf = cached_etf_holdings_universe_str(etf)
        if hdf is None or hdf.empty or "Ticker" not in hdf.columns:
            continue
        for t in hdf["Ticker"].astype(str).str.strip().str.upper().tolist():
            if not t or not is_valid_scanner_ticker(t) or t in seen:
                continue
            seen.add(t)
            ordered.append(t)

    for t in parse_tickers_from_text(manual_ticker_text or ""):
        if t not in seen:
            seen.add(t)
            ordered.append(t)

    return filter_scanner_ticker_list(ordered)


def resolve_opportunity_scanner_narrative_for_direct_mode():
    """직접 스캔 시 Narrative 팩터에 넣을 JSON 컨텍스트 (세션 내러티브 또는 안내 문구)."""
    cv = st.session_state.get("current_view")
    if isinstance(cv, dict) and cv:
        return cv
    return {
        "summary": (
            "사용자 지정 섹터/티커 스캔입니다. "
            f"「{_MAIN_NAV_OPTIONS[1]}」 메뉴에서 분석을 실행해 두면 같은 유니버스에 대해 Narrative Alignment 점수가 더 잘 맞습니다."
        ),
        "themes": [],
    }


def render_opportunity_scanner_snapshot(snap):
    """
    1.6 스캐너 결과 UI. `snap`은 st.session_state['scanner_results'] 형식(dict).
    사이드바 등과 무관하게 세션에 저장된 score_df만 사용한다.
    """
    if not isinstance(snap, dict):
        return
    score_df = snap.get("score_df")
    if not isinstance(score_df, pd.DataFrame) or score_df.empty:
        return

    score_df = _scanner_score_df_format_for_display(score_df.copy(), "leaders")

    mode_note = str(snap.get("mode_note") or "스캔").strip()
    universe = snap.get("universe") or []
    completed_at = str(snap.get("completed_at") or "").strip()

    st.divider()
    st.markdown("##### 📌 저장된 스캔 결과")
    if snap.get("_restored_from_sheet"):
        st.info("🔄 이전 세션의 마지막 스캔 결과를 자동 복원했습니다. 최신 시장 데이터 반영을 위해 재스캔을 권장합니다.")
    if completed_at:
        try:
            _ca_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            _now_utc = datetime.now(timezone.utc)
            _elapsed_min = int((_now_utc - _ca_dt).total_seconds() / 60)
            _elapsed_str = f"{_elapsed_min}분 전" if _elapsed_min < 60 else f"{_elapsed_min // 60}시간 {_elapsed_min % 60}분 전"
            _ca_kst = _ca_dt.astimezone(_MARKET_ET_TZ).strftime("%Y-%m-%d %H:%M ET")
            st.caption(f"⏱️ 완료 시각: `{_ca_kst}` ({_elapsed_str})")
            if _elapsed_min > 360:  # 6시간 이상 경과
                st.warning("⚠️ 스캔 결과가 6시간 이상 지났습니다. 최신 시장 상황 반영을 위해 재스캔을 권장합니다.")
        except Exception:
            st.caption(f"완료 시각 (UTC): `{completed_at}`")
    st.info(f"[{mode_note}] 유니버스 **{len(universe)}**개 티커 · 리런·메뉴 이동 후에도 세션에 유지됩니다.")
    with st.expander("스캔 당시 유니버스 보기", expanded=False):
        st.code(", ".join(universe) if universe else "(기록 없음)")

    _scanner_factor_defs = [
        ("Narrative\n(35%)", "Narrative Score"),
        ("Momentum\n(20%)", "Momentum Score"),
        ("RS\n(15%)", "RS Score"),
        ("Fundamentals\n(15%)", "Fundamentals Score"),
        ("Inst. Interest\n(10%)", "Institutional Score"),
        ("Valuation\n(5%)", "Valuation Score"),
    ]

    top3 = score_df.head(3).copy()
    for rank_idx, row in top3.iterrows():
        rank = rank_idx + 1
        tk_scan = str(row["Ticker"]).strip().upper()
        st.error(
            f"🏆 TOP {rank} | {tk_scan} ({row['Name']}) | "
            f"Final {_scanner_ui_fmt_2f(row['Final Score'])} / 100"
        )
        fac_cols = st.columns(6)
        for i, (label, key) in enumerate(_scanner_factor_defs):
            with fac_cols[i]:
                st.metric(label, _scanner_ui_fmt_2f(row[key]))
        st.markdown(f"**Narrative Why:** {row['Narrative Why']}")
        st.markdown(f"**Risk:** {row['Risk']}")
        # Watchlist 추가 버튼 (on_click 방식)
        def _add_sc_top3_wl(tk=tk_scan, r=rank, _row=row):
            _uid_s = str(st.session_state.get("user_id") or "").strip()
            # 스캐너 결과에서 현재가 추출 (있으면 가격 재조회 불필요)
            _sp = pd.to_numeric(_row.get("Price", _row.get("Close", np.nan)), errors="coerce")
            _ok_s, _err_s = add_to_watchlist(
                _uid_s, tk,
                memo=f"AI 스캐너 TOP{r} - Score: {_scanner_ui_fmt_2f(_row['Final Score'])}",
                saved_price=float(_sp) if pd.notna(_sp) else None,
            )
            if _ok_s:
                st.session_state[f"_sc_wl_added_{tk}"] = True
        btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 2])
        with btn_col1:
            if st.session_state.get(f"_sc_wl_added_{tk_scan}"):
                st.success(f"✅ {tk_scan} 추가됨!")
            else:
                st.button(f"🔔 {tk_scan} Watchlist 추가",
                    key=f"scanner_wl_add_{rank_idx}_{tk_scan}",
                    on_click=_add_sc_top3_wl,
                    use_container_width=True)
        with btn_col2:
            # 아이디어A: 1클릭으로 3단계 정밀검사 탭으로 이동
            def _goto_micro(tk=tk_scan):
                st.session_state["selected_ticker"] = tk
                st.session_state["main_sidebar_nav"] = _MAIN_NAV_OPTIONS[5]
            st.button(f"🔬 {tk_scan} 정밀검사",
                key=f"scanner_goto_micro_{rank_idx}_{tk_scan}",
                on_click=_goto_micro,
                use_container_width=True,
                help="3단계 개별 종목 정밀 검사 탭으로 이동합니다.")
        st.divider()

    remain_df = score_df.iloc[3:].copy()
    if not remain_df.empty:
        st.markdown("### 4위 이하 종목")
        show_cols = [
            "Ticker", "Name", "Final Score", "Narrative Score",
            "Momentum Score", "RS Score", "Fundamentals Score",
            "Institutional Score", "Valuation Score",
        ]
        num_fmt = st.column_config.NumberColumn(format="%.2f")
        st.dataframe(
            remain_df[show_cols], use_container_width=True, hide_index=True,
            column_config={c: num_fmt for c in show_cols if c not in ("Ticker", "Name")},
        )
        # 4위 이하 Watchlist 추가
        st.markdown("**🔔 Watchlist에 추가하기:**")
        _rem_cols = st.columns(min(len(remain_df), 5))
        for _ri, (_, _rrow) in enumerate(remain_df.head(5).iterrows()):
            _rtk = str(_rrow["Ticker"]).strip().upper()
            with _rem_cols[_ri]:
                def _add_rem_wl(tk=_rtk, row=_rrow):
                    _uid_r = str(st.session_state.get("user_id") or "").strip()
                    _sp_r = pd.to_numeric(row.get("Price", row.get("Close", np.nan)), errors="coerce")
                    _ok_r, _ = add_to_watchlist(
                        _uid_r, tk,
                        memo=f"AI 스캐너 - Score: {_scanner_ui_fmt_2f(row['Final Score'])}",
                        saved_price=float(_sp_r) if pd.notna(_sp_r) else None,
                    )
                    if _ok_r:
                        st.session_state[f"_sc_wl_added_{tk}"] = True
                if st.session_state.get(f"_sc_wl_added_{_rtk}"):
                    st.success(f"✅ {_rtk}")
                else:
                    st.button(f"🔔 {_rtk}", key=f"rem_wl_{_ri}_{_rtk}", on_click=_add_rem_wl, use_container_width=True)
                def _goto_micro_rem(tk=_rtk):
                    st.session_state["selected_ticker"] = tk
                    st.session_state["main_sidebar_nav"] = _MAIN_NAV_OPTIONS[5]
                st.button(f"🔬 {_rtk}", key=f"rem_micro_{_ri}_{_rtk}", on_click=_goto_micro_rem, use_container_width=True, help="3단계 정밀검사로 이동")
    else:
        st.caption("유니버스 종목 수가 3개 이하라 추가 표시는 없습니다.")

    # ── 아이디어D: 팩터 레이더 차트 ──────────────────────────────────────
    with st.expander("📊 팩터 레이더 차트 (상위 종목 팩터 프로파일 비교)", expanded=False):
        _radar_df = score_df.head(5).copy()
        _factor_cols = ["Narrative Score", "Momentum Score", "RS Score", "Fundamentals Score", "Institutional Score", "Valuation Score"]
        _factor_short = ["Narrative", "Momentum", "RS", "Fundamen.", "Inst.", "Valuation"]
        _available_cols = [c for c in _factor_cols if c in _radar_df.columns]
        if not _radar_df.empty and len(_available_cols) >= 3:
            _rows = []
            for _, _rdr in _radar_df.iterrows():
                _tk = str(_rdr["Ticker"])
                for _col, _short in zip(_factor_cols, _factor_short):
                    if _col in _rdr.index:
                        _val = pd.to_numeric(_rdr[_col], errors="coerce")
                        _rows.append({"Ticker": _tk, "Factor": _short, "Score": float(_val) if pd.notna(_val) else 0.0})
            _chart_df = pd.DataFrame(_rows)
            if not _chart_df.empty:
                try:
                    _radar_chart = (
                        alt.Chart(_chart_df)
                        .mark_bar()
                        .encode(
                            x=alt.X("Factor:N", sort=_factor_short, title=None),
                            y=alt.Y("Score:Q", scale=alt.Scale(domain=[0, 100]), title="Score"),
                            color=alt.Color("Ticker:N", legend=alt.Legend(title="종목")),
                            xOffset="Ticker:N",
                            tooltip=["Ticker", "Factor", alt.Tooltip("Score:Q", format=".1f")],
                        )
                        .properties(height=300, title="Top 5 종목 팩터 점수 비교")
                    )
                    st.altair_chart(_radar_chart, use_container_width=True)
                    st.caption("막대가 길수록 해당 팩터에서 강세. 같은 Factor 내 색깔별로 종목 비교 가능.")
                except Exception as _chart_err:
                    st.caption(f"차트 렌더링 실패: {_chart_err}")
        else:
            st.caption("레이더 차트를 표시할 점수 데이터가 부족합니다.")

    with st.expander("📐 알파 스캐너 점수 계산 공식 (가중치·의미·계산 방법)", expanded=False):
        st.markdown(
            """
**가중치 (합계 100%)**

| 항목 | 비중 | 한 줄 의미 |
|------|------|------------|
| Narrative | **35%** | 최신 시장 내러티브와 해당 종목의 연결·수혜 강도 |
| Momentum | **20%** | 최근 1개월 가격 모멘텀 (상대적 강약) |
| RS (Relative Strength) | **15%** | 3개월 수익률 대비 SPY 초과수익 |
| Fundamentals | **15%** | 매출 성장 또는 EPS 등 재무 지표 기반 가점 |
| Inst. Interest | **10%** | 최근 3일 평균 거래량 vs 3개월 평균 거래량 비율 |
| Valuation | **5%** | Forward P/E가 합리적 구간인지 여부 |

**초보자용 설명**

1. **Narrative**  
   저장된 내러티브 JSON을 Gemini에 넘겨, “이 테마와 이 기업이 얼마나 맞물리는지”를 0~100으로 채점합니다. 같은 날 유니버스 전체에 대해 한 번씩 정규화해 상대 순위가 나옵니다.

2. **Momentum**  
   일봉 종가 기준으로 약 21거래일(1개월) 수익률을 구한 뒤, 이번 스캔에 포함된 종목들 사이에서 0~100으로 다시 스케일링합니다.

3. **RS**  
   종목의 약 63거래일(3개월) 수익률에서 SPY의 동일 기간 수익률을 뺀 값(초과수익)을 쓰고, 역시 이번 스캔 풀 안에서 0~100으로 정규화합니다.

4. **Fundamentals**  
   `revenueGrowth`를 0~100 연속 점수로 정규화합니다 (성장률 -100%=0점, +200%=100점 선형 맵핑). `trailingEps > 0`이면 최대 30점 추가 보너스, 음수이면 15점 패널티. 단순 이진 판단 대신 연속 점수로 종목 구별력을 높였습니다. **두 지표가 모두 없으면** 이 항목은 가중치에서 빼고 나머지 항목만으로 100점 환산합니다.

5. **Inst. Interest**  
   `거래량 급증(3일/63일 ≥ 1.2)` OR `기관보유비율(institutionPercentHeld) ≥ 50%` 중 하나라도 충족하면 신호로 인정합니다. **두 조건 모두 충족 시 100점, 하나만 충족 시 65점**으로 신호 강도를 구분합니다.

6. **Valuation**  
   `forwardPE`가 양수이고 **50 이하**이면 가점(원시 100), 그렇지 않으면 0 후 정규화합니다. **P/E가 없거나 0 이하**(적자 등으로 해석 어려움)면 역시 가중치에서 제외하고 재환산합니다.

**최종 점수 (Final Score)**

- 위 6개 점수는 각각 0~100 스케일에서 유니버스 내 상대 정규화된 값입니다.  
- 이론상 가중합은 `0.35×N + 0.20×M + …` 형태이며, **펀더멘털·밸류에이션 데이터가 없는 종목**은 해당 비중(15% 또는 5%)을 **분모에서 빼고** `(가중합 ÷ 적용된 가중치 합)`으로 **100점 만점**으로 맞춥니다.  
- 동일 점수일 때는 티커 알파벳 순으로 순위를 안정적으로 정렬합니다.

**면책**  
점수는 데이터 가용성·시장 상황에 따라 변동하며, 투자 권유가 아닙니다.
"""
        )


def render_opportunity_emerging_snapshot(snap):
    """Emerging 엔진 세션 결과 UI (`scanner_results_emerging`)."""
    if not isinstance(snap, dict):
        return
    score_df = snap.get("score_df")
    if not isinstance(score_df, pd.DataFrame) or score_df.empty:
        return

    score_df = _scanner_score_df_format_for_display(score_df.copy(), "emerging")

    mode_note = str(snap.get("mode_note") or "Emerging").strip()
    universe = snap.get("universe") or []
    completed_at = str(snap.get("completed_at") or "").strip()

    st.divider()
    st.markdown("##### 🚀 저장된 Emerging 스캔 결과")
    if snap.get("_restored_from_sheet"):
        st.info("🔄 이전 세션의 마지막 Emerging 스캔 결과를 자동 복원했습니다. 최신 시장 데이터 반영을 위해 재스캔을 권장합니다.")
    if completed_at:
        try:
            _ca_dt_em = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            _now_utc_em = datetime.now(timezone.utc)
            _elapsed_min_em = int((_now_utc_em - _ca_dt_em).total_seconds() / 60)
            _elapsed_str_em = f"{_elapsed_min_em}분 전" if _elapsed_min_em < 60 else f"{_elapsed_min_em // 60}시간 {_elapsed_min_em % 60}분 전"
            _ca_kst_em = _ca_dt_em.astimezone(_MARKET_ET_TZ).strftime("%Y-%m-%d %H:%M ET")
            st.caption(f"⏱️ 완료 시각: `{_ca_kst_em}` ({_elapsed_str_em})")
            if _elapsed_min_em > 360:
                st.warning("⚠️ 스캔 결과가 6시간 이상 지났습니다. 최신 시장 상황 반영을 위해 재스캔을 권장합니다.")
        except Exception:
            st.caption(f"완료 시각 (UTC): `{completed_at}`")
    st.info(f"[{mode_note}] 유니버스 **{len(universe)}**개 · 후발·2차 수혜 관점 브리핑")
    with st.expander("스캔 당시 유니버스", expanded=False):
        st.code(", ".join(universe) if universe else "(기록 없음)")

    _em_factor_defs = [
        ("Narrative\nExpansion (35%)", "Narrative Score"),
        ("Early RS\n(20%)", "Early RS Score"),
        ("Vol Accel\n(20%)", "Vol Accel Score"),
        ("Fund.\nReadiness (15%)", "Fundamentals Score"),
        ("Overext.\n(10%)", "Overextension Score"),
    ]

    top3 = score_df.head(3).copy()
    for rank_idx, row in top3.iterrows():
        rank = rank_idx + 1
        tk_em = str(row["Ticker"]).strip().upper()
        volx = row.get("Vol5/30x")
        volx_s = f"{float(volx):.2f}x" if pd.notna(volx) else "N/A"
        rsi_s = f"{float(row.get('RSI(14)')):.1f}" if pd.notna(row.get("RSI(14)")) else "N/A"
        st.success(
            f"🌱 TOP {rank} | {tk_em} ({row['Name']}) | Final {_scanner_ui_fmt_2f(row['Final Score'])} / 100\n"
            f"RSI(14): {rsi_s} · 5일/30일 거래량 비: {volx_s}"
        )
        fac_cols = st.columns(5)
        for i, (label, key) in enumerate(_em_factor_defs):
            with fac_cols[i]:
                st.metric(label, _scanner_ui_fmt_2f(row[key]))
        st.markdown(f"**다음 타자 AI 코멘트:** {row['Narrative Why']}")
        st.markdown(f"**리스크:** {row['Risk']}")

        # ── Watchlist 추가 버튼 ───────────────────────────────────────────
        _em_wl_key = f"_em_snap_wl_{tk_em}"
        def _do_add_em(tk=tk_em, r=rank, sc=row['Final Score'], _row=row):
            _uid = str(st.session_state.get("user_id") or "").strip()
            _sp_e = pd.to_numeric(_row.get("Price", _row.get("Close", np.nan)), errors="coerce")
            _ok, _ = add_to_watchlist(
                _uid, tk,
                memo=f"Emerging TOP{r} - Score: {_scanner_ui_fmt_2f(sc)}",
                alert_rsi=35.0,
                saved_price=float(_sp_e) if pd.notna(_sp_e) else None,
            )
            if _ok:
                st.session_state[f"_em_snap_wl_{tk}"] = True
        _em_btn_c1, _em_btn_c2, _em_btn_c3 = st.columns([1, 1, 2])
        with _em_btn_c1:
            if st.session_state.get(_em_wl_key):
                st.success(f"✅ {tk_em} Watchlist 추가됨!")
            else:
                st.button(
                    f"🔔 {tk_em} Watchlist 추가",
                    key=f"em_snap_wl_{rank_idx}_{tk_em}",
                    on_click=_do_add_em,
                    use_container_width=True,
                )
        with _em_btn_c2:
            def _goto_micro_em(tk=tk_em):
                st.session_state["selected_ticker"] = tk
                st.session_state["main_sidebar_nav"] = _MAIN_NAV_OPTIONS[5]
            st.button(
                f"🔬 {tk_em} 정밀검사",
                key=f"em_snap_micro_{rank_idx}_{tk_em}",
                on_click=_goto_micro_em,
                use_container_width=True,
                help="3단계 개별 종목 정밀 검사 탭으로 이동합니다.",
            )
        st.divider()

    remain_df = score_df.iloc[3:].copy()
    if not remain_df.empty:
        st.markdown("### 4위 이하 (지표 포함)")
        show_cols = [
            "Ticker",
            "Name",
            "Final Score",
            "Narrative Score",
            "Early RS Score",
            "Vol Accel Score",
            "Fundamentals Score",
            "Overextension Score",
            "RSI(14)",
            "Vol5/30x",
            "Narrative Why",
        ]
        num_fmt = st.column_config.NumberColumn(format="%.2f")
        st.dataframe(
            remain_df[show_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "Final Score": num_fmt,
                "Narrative Score": num_fmt,
                "Early RS Score": num_fmt,
                "Vol Accel Score": num_fmt,
                "Fundamentals Score": num_fmt,
                "Overextension Score": num_fmt,
                "RSI(14)": st.column_config.NumberColumn(format="%.1f"),
                "Vol5/30x": st.column_config.NumberColumn(format="%.2f"),
            },
        )
    else:
        st.caption("유니버스가 3개 이하입니다.")

    # ── Emerging 팩터 비교 차트 ────────────────────────────────────────
    with st.expander("📊 Emerging 팩터 비교 차트 (Top 5)", expanded=False):
        _em_radar_df = score_df.head(5).copy()
        _em_factor_cols = ["Narrative Score", "Early RS Score", "Vol Accel Score", "Fundamentals Score", "Overextension Score"]
        _em_factor_short = ["Narrative", "Early RS", "Vol Accel", "Fundamen.", "Overext."]
        _em_avail_cols = [c for c in _em_factor_cols if c in _em_radar_df.columns]
        if not _em_radar_df.empty and len(_em_avail_cols) >= 3:
            _em_rows = []
            for _, _edr in _em_radar_df.iterrows():
                _etk = str(_edr["Ticker"])
                for _ec, _es in zip(_em_factor_cols, _em_factor_short):
                    if _ec in _edr.index:
                        _ev = pd.to_numeric(_edr[_ec], errors="coerce")
                        _em_rows.append({"Ticker": _etk, "Factor": _es, "Score": float(_ev) if pd.notna(_ev) else 0.0})
            _em_chart_df = pd.DataFrame(_em_rows)
            if not _em_chart_df.empty:
                try:
                    _em_chart = (
                        alt.Chart(_em_chart_df)
                        .mark_bar()
                        .encode(
                            x=alt.X("Factor:N", sort=_em_factor_short, title=None),
                            y=alt.Y("Score:Q", scale=alt.Scale(domain=[0, 100]), title="Score"),
                            color=alt.Color("Ticker:N", legend=alt.Legend(title="종목")),
                            xOffset="Ticker:N",
                            tooltip=["Ticker", "Factor", alt.Tooltip("Score:Q", format=".1f")],
                        )
                        .properties(height=300, title="Emerging Top 5 팩터 비교")
                    )
                    st.altair_chart(_em_chart, use_container_width=True)
                    st.caption("Overextension은 낮을수록(과열 없음) 좋은 신호입니다.")
                except Exception as _ec_err:
                    st.caption(f"차트 렌더링 실패: {_ec_err}")

    with st.expander("📐 Emerging 엔진 공식 (가중치·의미)", expanded=False):
        st.markdown(
            """
| 항목 | 비중 | 요약 |
|------|------|------|
| Narrative Expansion | **35%** | 대장주 제외, 2차 수혜·인프라·공급망 관점 Gemini 점수 |
| Early Relative Strength | **20%** | 1개월 수익률 기반 원시점수(0~100)를 가중합에 직접 반영 |
| Volume Acceleration | **20%** | 최근 5일 / 30일 평균 거래량 비율(≥1.5 고득점) |
| Fundamental Readiness | **15%** | EPS·매출 성장 등 `FMP` 펀더멘털 기반 |
| Overextension Penalty | **10%** | RSI(14)>70·50일선 대비 과도 이격 시 감점 반영(높을수록 덜 과열) |

결측 팩터는 가중치 분모에서 제외하고 `(가중합 ÷ 유효 가중치 합)`으로 **100점 만점** 환산합니다.
"""
        )


def normalize_series_to_100(series):
    values = pd.to_numeric(series, errors="coerce")
    valid = values.dropna()
    out = pd.Series(0.0, index=series.index)
    if valid.empty:
        return out
    v_min = float(valid.min())
    v_max = float(valid.max())
    if v_max == v_min:
        out.loc[valid.index] = 100.0
        return out
    out.loc[valid.index] = ((valid - v_min) / (v_max - v_min) * 100.0).clip(0, 100)
    return out


def clip_series_0_100(series):
    """스캐너 원시 점수(이론상 0~100)를 열 단위로 [0,100]으로 자름 — 동일값 전 시계열→100 부작용 없음."""
    v = pd.to_numeric(series, errors="coerce")
    return v.clip(lower=0.0, upper=100.0)


def _scanner_score_df_format_for_display(score_df: pd.DataFrame, mode: str) -> pd.DataFrame:
    """보조 점수 결측은 50(중립), 모든 Score·Final은 소수 둘째 자리로 정리."""
    df = score_df.copy()
    if mode == "leaders":
        for c in ("Fundamentals Score", "Institutional Score", "Valuation Score"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(50.0).clip(0.0, 100.0)
    elif mode == "emerging":
        for c in ("Fundamentals Score", "Overextension Score"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(50.0).clip(0.0, 100.0)
    for c in df.columns:
        if c == "Final Score" or str(c).endswith(" Score"):
            df[c] = pd.to_numeric(df[c], errors="coerce").round(2)
    return df


def _scanner_ui_fmt_2f(val) -> str:
    """표시용 숫자 포맷. 결측은 대시(구형 세션은 format_for_display로 먼저 보정)."""
    v = pd.to_numeric(val, errors="coerce")
    if pd.isna(v):
        return "—"
    return f"{float(v):.2f}"


def emerging_final_weighted_score(score_df):
    """
    Emerging Final Score: 원시 0~100 점수에 가중치를 곱해 합산. 결측/비가용 팩터는 분자·분모 모두에서 제외.
    (Narrative×0.35 + Early RS×0.20 + Vol Accel×0.20 + Fundamentals×0.15 + Overextension×0.10),
    합이 항상 ≤100이 되도록 가용 가중치 합으로 나눔.
    Overextension 항은 '덜 과열일수록 높은' 원시점수(0~100)로, 패널티는 낮은 원시점수로 반영됨.
    """
    w_n, w_e, w_v, w_f, w_o = 0.35, 0.20, 0.20, 0.15, 0.10

    n = clip_series_0_100(score_df["Narrative Raw"]).fillna(0.0)
    e = clip_series_0_100(score_df["Early RS Raw"])
    v = clip_series_0_100(score_df["Vol Accel Raw"])
    f = clip_series_0_100(score_df["Fundamentals Raw"])
    o = clip_series_0_100(score_df["Overextension Raw"])

    na = score_df["Narrative Available"].astype(float)
    ea = score_df["Early RS Available"].astype(float)
    va = score_df["Vol Accel Available"].astype(float)
    fa = score_df["Fundamentals Available"].astype(float)
    oa = score_df["Overextension Available"].astype(float)

    numer = (
        na * n * w_n
        + ea * e.fillna(0.0) * w_e
        + va * v.fillna(0.0) * w_v
        + fa * f.fillna(0.0) * w_f
        + oa * o.fillna(0.0) * w_o
    )
    denom = na * w_n + ea * w_e + va * w_v + fa * w_f + oa * w_o
    denom = denom.replace(0.0, np.nan)
    out = (numer / denom).fillna(0.0).clip(upper=100.0)
    return out, n, e, v, f, o


def leaders_final_weighted_score(score_df):
    """
    Current Leaders Final Score: 원시/정규화 점수(각 0~100)에 가중치를 곱해 합산.
    Narrative 가용 시 0.35N + 0.20M + 0.15RS + 0.15F + 0.10I + 0.05V ≤ 100.
    Narrative 비가용 시 내러티브 항 제외 후 나머지 가중치 합으로 나누어 100점 만점(동적 분모, 전체 비가용 시 약 0.65).
    """
    w_n, w_m, w_rs, w_f, w_i, w_v = 0.35, 0.20, 0.15, 0.15, 0.10, 0.05

    n = clip_series_0_100(score_df["Narrative Raw"]).fillna(0.0)
    m = normalize_series_to_100(score_df["Momentum 1M Raw"])
    rs = normalize_series_to_100(score_df["RS Raw"])
    f = clip_series_0_100(score_df["Fundamentals Raw"]).fillna(0.0)
    inst = clip_series_0_100(score_df["Institutional Raw"]).fillna(0.0)
    val = clip_series_0_100(score_df["Valuation Raw"]).fillna(0.0)

    na = score_df["Narrative Available"].astype(float)
    fa = score_df["Fundamentals Available"].astype(float)
    va = score_df["Valuation Available"].astype(float)

    numer = (
        na * n * w_n
        + m * w_m
        + rs * w_rs
        + fa * f * w_f
        + inst * w_i
        + va * val * w_v
    )
    denom = na * w_n + w_m + w_rs + fa * w_f + w_i + va * w_v
    denom = denom.replace(0.0, np.nan)
    out = (numer / denom).fillna(0.0).clip(upper=100.0)
    return out, n, m, rs, f, inst, val


def _parse_gemini_ticker_score_json_array(raw_text):
    """Gemini 응답에서 JSON 배열([{{...}},...]) 추출·파싱. 마크다운 제거 + UTF-8 세탁 후 loads."""
    raw = str(raw_text or "")
    raw = raw.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    raw = raw.encode("utf-8", "ignore").decode("utf-8")
    if not raw:
        return []

    candidates = []
    if raw.lstrip().startswith("["):
        candidates.append(raw.strip())
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        g = match.group(0)
        if g not in candidates:
            candidates.append(g)

    last_tb = None
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            # 잘린 JSON·Unterminated string 등: 해당 후보만 포기하고 다음 후보 또는 빈 결과
            continue
        except Exception:
            last_tb = traceback.format_exc()

    if last_tb:
        st.code(last_tb, language="bash")
    elif not candidates:
        try:
            raise ValueError("응답에서 JSON 배열 패턴을 찾지 못했습니다.")
        except Exception:
            st.code(traceback.format_exc(), language="bash")

    return []


def batch_narrative_alignment_scores(tickers, narrative_text):
    """
    Current Leaders: 유니버스 티커에 대해 Narrative Alignment를 Gemini **청크(최대 20티커)별** 호출로 평가.
    반환: ticker -> (score 0~100, reason str)
    """
    clean = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not clean:
        return {}
    nt = str(narrative_text or "").strip()
    if not nt:
        return {t: (0.0, "내러티브 텍스트가 부족합니다.") for t in clean}

    out = {}
    n_chunk = max(1, (len(clean) + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE - 1) // _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE)
    for ci, i in enumerate(range(0, len(clean), _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE), start=1):
        chunk = clean[i : i + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE]
        tickers_json = json.dumps(chunk, ensure_ascii=False)
        prompt = f"""
당신은 월가 탑다운-바텀업 전략가입니다.

[Market Narrative JSON]
{nt}

[Tickers to score (이번 청크에서 모두 평가)]
{tickers_json}

작업:
- 주어진 **모든** 티커에 대해 Narrative Alignment 점수(0~100 정수)와 그 이유(reason, **한국어 15단어 이내**)를 평가하라.
- 반드시 **JSON 배열 형식만** 응답하라. 마크다운·설명 문장·코드펜스 금지.
- 배열 원소 형식: {{"ticker": "VRT", "score": 85, "reason": "AI 전력망 핵심 수혜"}}
- 배열 길이는 반드시 {len(chunk)}이며, 위 티커 리스트의 각 심볼을 **정확히 한 번씩** 포함할 것.

예시 형식:
[{{"ticker":"VRT","score":85,"reason":"AI 전력망 핵심 수혜"}},{{"ticker":"ANET","score":72,"reason":"클라우드 네트워크 수혜"}}]
"""
        try:
            response = _scanner_narrative_batch_generate_chunk_with_retries(prompt)
            raw_text = str(getattr(response, "text", "") or "").strip()
            items = _parse_gemini_ticker_score_json_array(raw_text)
            if not items:
                st.warning(
                    f"내러티브 배치: 청크 {ci}/{n_chunk} JSON 배열 파싱 결과가 비어 있습니다. "
                    f"({len(chunk)}티커)"
                )
                for t in chunk:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                tk = str(it.get("ticker") or it.get("Ticker") or "").strip().upper()
                if not tk:
                    continue
                sc = to_float(it.get("score"))
                if pd.isna(sc):
                    sc = 0.0
                sc = float(np.clip(sc, 0.0, 100.0))
                reason = str(it.get("reason") or it.get("why") or "").strip() or "N/A"
                out[tk] = (sc, reason)
            for t in chunk:
                if t not in out:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
        except Exception as exc:
            st.warning(
                f"내러티브 배치 Gemini 호출 실패 ({_SCANNER_GEMINI_CHUNK_MAX_RETRIES}회 재시도 후, 청크 {ci}/{n_chunk}): {exc}"
            )
            st.code(traceback.format_exc(), language="bash")
            for t in chunk:
                out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
    return out


def batch_narrative_emerging_second_order_scores(tickers, narrative_text):
    """
    Emerging: 2차 수혜 관점 내러티브를 Gemini **청크(최대 20티커)별** 호출로 일괄 평가.
    """
    clean = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not clean:
        return {}
    nt = str(narrative_text or "").strip()
    if not nt:
        return {t: (0.0, "내러티브 텍스트가 부족합니다.") for t in clean}

    out = {}
    n_chunk = max(1, (len(clean) + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE - 1) // _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE)
    for ci, i in enumerate(range(0, len(clean), _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE), start=1):
        chunk = clean[i : i + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE]
        tickers_json = json.dumps(chunk, ensure_ascii=False)
        prompt = f"""
당신은 크로스에셋·공급망 투자 전문가입니다.

[Market Narrative JSON]
{nt}

[Tickers to score (이번 청크)]
{tickers_json}

작업:
- **이미 과도하게 오른 1차 대장주·직접 수혜주는 제외**하는 관점에서,
  인프라/공급망/후속 기술 등 **2차 수혜(Second-order)** 로서 각 티커의 기대도를 0~100 정수로 채점하라.
- 이유(reason)는 **한국어 15단어 이내**.
- 반드시 **JSON 배열만** 출력. 마크다운 금지.
- 원소 형식: {{"ticker": "VRT", "score": 82, "reason": "데이터센터 전력 인프라 연쇄"}}
- 배열 길이는 반드시 {len(chunk)}이며 모든 티커를 빠짐없이 포함할 것.

예시:
[{{"ticker":"VRT","score":82,"reason":"데이터센터 전력 인프라 연쇄"}},{{"ticker":"MU","score":70,"reason":"메모리 공급망 후방"}}]
"""
        try:
            response = _scanner_narrative_batch_generate_chunk_with_retries(prompt)
            raw_text = str(getattr(response, "text", "") or "").strip()
            items = _parse_gemini_ticker_score_json_array(raw_text)
            if not items:
                st.warning(
                    f"Emerging 내러티브 배치: 청크 {ci}/{n_chunk} JSON 배열 파싱 결과가 비어 있습니다. "
                    f"({len(chunk)}티커)"
                )
                for t in chunk:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                tk = str(it.get("ticker") or it.get("Ticker") or "").strip().upper()
                if not tk:
                    continue
                sc = to_float(it.get("score"))
                if pd.isna(sc):
                    sc = 0.0
                sc = float(np.clip(sc, 0.0, 100.0))
                reason = str(it.get("reason") or it.get("why") or "").strip() or "N/A"
                out[tk] = (sc, reason)
            for t in chunk:
                if t not in out:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
        except Exception as exc:
            st.warning(
                f"Emerging 내러티브 배치 Gemini 호출 실패 ({_SCANNER_GEMINI_CHUNK_MAX_RETRIES}회 재시도 후, 청크 {ci}/{n_chunk}): {exc}"
            )
            st.code(traceback.format_exc(), language="bash")
            for t in chunk:
                out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
    return out


def score_opportunity_universe(universe_tickers, latest_analysis):
    if not universe_tickers:
        return pd.DataFrame()

    tickers = list(dict.fromkeys([str(t).strip().upper() for t in universe_tickers if str(t).strip()]))
    tickers = filter_scanner_ticker_list(tickers)
    if not tickers:
        return pd.DataFrame()

    narrative_text = json.dumps(latest_analysis or {}, ensure_ascii=False)
    progress = st.progress(0.0, text="스캐너 준비 중...")

    with st.spinner("Gemini 내러티브 배치 평가 중 (티커 청크별, gemini-2.5-flash)..."):
        narrative_map = batch_narrative_alignment_scores(tickers, narrative_text)

    with st.spinner("가격/거래량 데이터 다운로드 중..."):
        try:
            batch = _fmp_batch_price_history(tickers + ["SPY"], limit=130)
            close_df = pd.DataFrame({tk: df["Close"] for tk, df in batch.items() if "Close" in df.columns}).sort_index()
            volume_df = pd.DataFrame({tk: df["Volume"] for tk, df in batch.items() if "Volume" in df.columns}).sort_index()
            spy_hist = batch.get("SPY", pd.DataFrame())
        except Exception:
            close_df = pd.DataFrame()
            volume_df = pd.DataFrame()
            spy_hist = pd.DataFrame()

    # ── 병렬 웜업: _fmp_fill() HTTP 호출을 루프 전에 한꺼번에 병렬 처리 ──
    # 캐시 콜드(첫 스캔)일 때 180종목 × 순차 7회 HTTP → 병렬화로 대폭 단축
    with st.spinner(f"펀더멘털 데이터 병렬 수집 중... ({len(tickers)}종목)"):
        _info_cache = _fmp_fill_parallel_warmup(tickers)

    spy_3m = calculate_period_return(spy_hist["Close"], 63) if "Close" in spy_hist.columns else np.nan
    spy_3m = to_float(spy_3m)
    if pd.isna(spy_3m):
        spy_3m = 0.0

    rows = []
    total = len(tickers)

    for idx, ticker in enumerate(tickers, start=1):
        progress.progress(idx / total, text=f"[{idx}/{total}] {ticker} 멀티팩터 계산 중...")

        close_series = close_df[ticker] if ticker in close_df.columns else pd.Series(dtype=float)
        vol_series = volume_df[ticker] if ticker in volume_df.columns else pd.Series(dtype=float)
        _close_num_l = pd.to_numeric(close_series, errors="coerce").dropna()
        last_px_l = to_float(_close_num_l.iloc[-1]) if not _close_num_l.empty else np.nan  # ✅ 현재가

        m1_ret = calculate_period_return(close_series, 21)
        m3_ret = calculate_period_return(close_series, 63)
        rs_raw = to_float(m3_ret) - to_float(spy_3m)

        # 병렬 웜업 딕셔너리에서 조회 (캐시 히트) — 없으면 fallback으로 직접 호출
        info = _info_cache.get(ticker) or _fmp_fill({}, ticker)

        revenue_growth = to_float(info.get("revenueGrowth") or info.get("earningsGrowth"))
        trailing_eps = to_float(info.get("trailingEps"))
        forward_pe = to_float(info.get("forwardPE"))
        long_name = str(info.get("longName") or info.get("shortName") or ticker).strip()

        vol_3d_avg = pd.to_numeric(vol_series, errors="coerce").tail(3).mean() if not vol_series.empty else np.nan
        vol_3m_avg = pd.to_numeric(vol_series, errors="coerce").tail(63).mean() if not vol_series.empty else np.nan
        vol_ratio = np.nan
        if pd.notna(vol_3d_avg) and pd.notna(vol_3m_avg) and vol_3m_avg > 0:
            vol_ratio = float(vol_3d_avg / vol_3m_avg)

        fundamentals_data_available = pd.notna(revenue_growth) or pd.notna(trailing_eps)
        # 이진(0/100) 대신 연속 점수화: revenue_growth·trailing_eps를 각각 0~100으로 정규화 후 합산
        # revenue_growth: clip(-1, 2) → 0~100 선형 맵핑 (200% 성장 = 100점)
        # trailing_eps > 0 이면 최대 30점 보너스 (펀더멘털 안정성 가점)
        _fund_raw = 0.0
        if pd.notna(revenue_growth):
            _rev_score = float(np.clip((revenue_growth + 1.0) / 3.0 * 100.0, 0.0, 100.0))
            _fund_raw = max(_fund_raw, _rev_score)
        if pd.notna(trailing_eps) and trailing_eps > 0:
            _fund_raw = min(100.0, _fund_raw + 30.0)
        elif pd.notna(trailing_eps) and trailing_eps <= 0:
            _fund_raw = max(0.0, _fund_raw - 15.0)
        fundamentals_pass = _fund_raw >= 30.0  # 리스크 판단용 임계값
        # Institutional: 거래량 급증(vol_ratio>=1.2) OR 기관 보유비율 높음 (OR 조건으로 신호 품질 향상)
        inst_ownership = to_float(info.get("institutionPercentHeld") or info.get("institutionalOwnershipPercentage"))
        inst_via_vol = pd.notna(vol_ratio) and vol_ratio >= 1.2
        inst_via_ownership = pd.notna(inst_ownership) and inst_ownership >= 0.5  # 50% 이상 기관 보유
        inst_pass = inst_via_vol or inst_via_ownership
        # Institutional Raw: 두 신호 모두 충족 시 100, 하나만 충족 시 65, 둘 다 미충족 시 0
        if inst_via_vol and inst_via_ownership:
            _inst_raw = 100.0
        elif inst_via_vol or inst_via_ownership:
            _inst_raw = 65.0
        else:
            _inst_raw = 0.0
        # forwardPE가 None/NaN이거나 0 이하(적자 등으로 PE 해석 불가)면 결측으로 간주
        valuation_data_available = pd.notna(forward_pe) and forward_pe > 0
        valuation_pass = valuation_data_available and forward_pe <= 50

        narrative_score, narrative_why = narrative_map.get(ticker, (0.0, "배치 미응답"))
        # API 실패 사유면 narrative_available=False로 표기 → 동적 분모에서 완전 제외 (불이익 방지)
        narrative_available = narrative_why not in SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS
        if not narrative_available:
            narrative_score = 0.0  # 분자에서도 0으로 보장

        risk_bits = []
        if not valuation_data_available:
            risk_bits.append("밸류에이션 데이터 결측/해석 불가")
        elif not valuation_pass:
            risk_bits.append("밸류에이션 부담")
        if not fundamentals_data_available:
            risk_bits.append("펀더멘털 데이터 결측")
        elif not fundamentals_pass:
            risk_bits.append("펀더멘털 모멘텀 약함")
        if not inst_pass:
            risk_bits.append("기관 수급 가속 신호 부족")
        risk_text = ", ".join(risk_bits) if risk_bits else "특이 리스크 신호 제한적"

        rows.append(
            {
                "Ticker": ticker,
                "Name": long_name,
                "Price": round(float(last_px_l), 4) if pd.notna(last_px_l) else np.nan,  # ✅ Watchlist saved_price용
                "Narrative Raw": narrative_score,
                "Narrative Why": narrative_why,
                "Narrative Available": bool(narrative_available),
                "Momentum 1M Raw": to_float(m1_ret),
                "RS Raw": to_float(rs_raw),
                "Fundamentals Raw": float(_fund_raw),
                "Fundamentals Available": bool(fundamentals_data_available),
                "Institutional Raw": float(_inst_raw),
                "Valuation Raw": 100.0 if valuation_pass else 0.0,
                "Valuation Available": bool(valuation_data_available),
                "Risk": risk_text,
            }
        )

    score_df = pd.DataFrame(rows)
    if score_df.empty:
        progress.empty()
        return score_df

    # 가격 시계열이 없거나 핵심 모멘텀/RS가 NaN인 종목은 랭킹에서 제외 (내러티브만으로 상위 노출 방지)
    score_df = score_df.dropna(subset=["Momentum 1M Raw", "RS Raw"])
    if score_df.empty:
        progress.empty()
        return score_df

    _nw = score_df["Narrative Why"].isin(SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS)
    score_df.loc[_nw, "Narrative Raw"] = 0.0

    final_s, n_s, m_s, rs_s, f_s, inst_s, val_s = leaders_final_weighted_score(score_df)
    score_df["Final Score"] = final_s
    score_df["Narrative Score"] = n_s
    score_df["Momentum Score"] = m_s
    score_df["RS Score"] = rs_s
    score_df["Fundamentals Score"] = f_s
    score_df["Institutional Score"] = inst_s
    score_df["Valuation Score"] = val_s
    score_df = _scanner_score_df_format_for_display(score_df, "leaders")
    score_df = score_df.sort_values(
        ["Final Score", "Ticker"], ascending=[False, True], na_position="last"
    ).reset_index(drop=True)

    progress.progress(1.0, text="AI Opportunity Scanner 계산 완료")
    return score_df


def score_emerging_opportunity_universe(universe_tickers, latest_analysis):
    """
    Emerging Opportunities 스코어링 (총 100점, 결측 시 가중치 재분배).
    가중치: Narrative Expansion 35%, Early RS 20%, Vol Accel 20%, Fund Readiness 15%, Overextension 10%.
    """
    if not universe_tickers:
        return pd.DataFrame()

    tickers = list(dict.fromkeys([str(t).strip().upper() for t in universe_tickers if str(t).strip()]))
    tickers = filter_scanner_ticker_list(tickers)
    if not tickers:
        return pd.DataFrame()

    narrative_text = json.dumps(latest_analysis or {}, ensure_ascii=False)
    progress = st.progress(0.0, text="Emerging 스캐너 준비 중...")

    with st.spinner("Emerging: 가격·거래량 데이터 다운로드 중..."):
        try:
            batch_em = _fmp_batch_price_history(tickers, limit=130)
            close_df = pd.DataFrame({tk: df["Close"] for tk, df in batch_em.items() if "Close" in df.columns}).sort_index()
            volume_df = pd.DataFrame({tk: df["Volume"] for tk, df in batch_em.items() if "Volume" in df.columns}).sort_index()
        except Exception:
            close_df = pd.DataFrame()
            volume_df = pd.DataFrame()

    with st.spinner("Emerging Gemini 내러티브 배치 평가 중 (티커 청크별, gemini-2.5-flash)..."):
        narrative_map_em = batch_narrative_emerging_second_order_scores(tickers, narrative_text)

    # ── 병렬 웜업: _fmp_fill() HTTP 호출을 루프 전에 한꺼번에 병렬 처리 ──
    with st.spinner(f"Emerging: 펀더멘털 데이터 병렬 수집 중... ({len(tickers)}종목)"):
        _em_info_cache = _fmp_fill_parallel_warmup(tickers)

    rows = []
    total = len(tickers)

    for idx, ticker in enumerate(tickers, start=1):
        progress.progress(idx / total, text=f"[Emerging {idx}/{total}] {ticker} 계산 중...")

        close_series = close_df[ticker] if ticker in close_df.columns else pd.Series(dtype=float)
        vol_series = volume_df[ticker] if ticker in volume_df.columns else pd.Series(dtype=float)
        close_num = pd.to_numeric(close_series, errors="coerce").dropna()

        m1_ret = calculate_period_return(close_series, 21)
        m1 = to_float(m1_ret)
        early_available = pd.notna(m1)
        if not early_available:
            early_raw = np.nan
        elif m1 < 0:
            early_raw = 0.0
        elif m1 <= 15.0:
            early_raw = 100.0
        else:
            early_raw = max(0.0, 100.0 - (m1 - 15.0) * 6.0)

        vol_num = pd.to_numeric(vol_series, errors="coerce")
        v5 = float(vol_num.tail(5).mean()) if not vol_num.empty else np.nan
        v30 = float(vol_num.tail(30).mean()) if not vol_num.empty else np.nan
        vol_ratio = np.nan
        vol_available = False
        if pd.notna(v5) and pd.notna(v30) and v30 > 0:
            vol_ratio = v5 / v30
            vol_available = True
        if vol_available:
            if vol_ratio >= 1.5:
                vol_raw = 100.0
            elif vol_ratio >= 1.2:
                vol_raw = 72.0
            elif vol_ratio >= 1.0:
                vol_raw = 45.0
            else:
                vol_raw = 15.0
        else:
            vol_raw = np.nan

        rsi_last = np.nan
        if len(close_num) >= 15:
            rsi_series = calculate_rsi(close_series, 14)
            rsi_last = to_float(rsi_series.iloc[-1]) if rsi_series is not None and not rsi_series.empty else np.nan

        ma50 = np.nan
        if len(close_num) >= 50:
            ma50 = to_float(close_num.rolling(window=50, min_periods=50).mean().iloc[-1])
        last_px = to_float(close_num.iloc[-1]) if not close_num.empty else np.nan

        stretch_pct = np.nan
        if pd.notna(last_px) and pd.notna(ma50) and ma50 > 0:
            stretch_pct = (last_px / ma50 - 1.0) * 100.0

        overext_available = pd.notna(rsi_last) and pd.notna(ma50) and pd.notna(last_px) and ma50 > 0
        if overext_available:
            over_raw = 100.0
            if rsi_last > 70.0:
                over_raw -= min(55.0, (rsi_last - 70.0) * 2.2)
            if pd.notna(stretch_pct) and stretch_pct > 12.0:
                over_raw -= min(40.0, (stretch_pct - 12.0) * 1.8)
            over_raw = float(np.clip(over_raw, 0.0, 100.0))
        else:
            over_raw = np.nan

        # 병렬 웜업 딕셔너리에서 조회 — 없으면 fallback으로 직접 호출
        info = _em_info_cache.get(ticker) or _fmp_fill({}, ticker)

        eg = to_float(info.get("earningsGrowth"))
        if pd.isna(eg):
            eg = to_float(info.get("epsGrowth"))
        if pd.isna(eg):
            eg = to_float(info.get("earningsQuarterlyGrowth"))
        rev_g = to_float(info.get("revenueGrowth"))
        trail_eps = to_float(info.get("trailingEps"))

        fund_data_available = any(pd.notna(x) for x in (eg, rev_g, trail_eps))
        fund_raw = np.nan
        if fund_data_available:
            if pd.notna(eg) and eg > 0:
                fund_raw = 100.0
            elif pd.notna(rev_g) and rev_g > 0 and pd.notna(trail_eps) and trail_eps > 0:
                fund_raw = 85.0
            elif pd.notna(trail_eps) and trail_eps > 0:
                fund_raw = 55.0
            else:
                fund_raw = 20.0

        narrative_score, narrative_why = narrative_map_em.get(ticker, (0.0, "배치 미응답"))
        # API 실패 사유면 narrative_available=False → 동적 분모에서 완전 제외 (불이익 방지)
        narrative_available = narrative_why not in SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS
        if not narrative_available:
            narrative_score = 0.0

        risk_bits = []
        if pd.notna(rsi_last) and rsi_last > 70:
            risk_bits.append(f"RSI 과열({rsi_last:.1f})")
        if pd.notna(stretch_pct) and stretch_pct > 12:
            risk_bits.append(f"50일선 대비 과도 이격({stretch_pct:.1f}%)")
        if pd.notna(m1) and m1 > 15:
            risk_bits.append("1M 급등 구간(후발주 관점 부담)")
        risk_text = ", ".join(risk_bits) if risk_bits else "과열·이격 리스크 상대적으로 제한적"

        long_name = str(info.get("longName") or info.get("shortName") or ticker).strip()

        rows.append(
            {
                "Ticker": ticker,
                "Name": long_name,
                "Price": round(float(last_px), 4) if pd.notna(last_px) else np.nan,  # ✅ Watchlist saved_price용
                "Narrative Raw": narrative_score,
                "Narrative Why": narrative_why,
                "Narrative Available": bool(narrative_available),
                "Early RS Raw": early_raw,
                "Early RS Available": bool(early_available),
                "Vol Accel Raw": vol_raw,
                "Vol Accel Available": bool(vol_available),
                "Vol5/30x": vol_ratio,
                "Fundamentals Raw": fund_raw,
                "Fundamentals Available": bool(fund_data_available),
                "Overextension Raw": over_raw,
                "Overextension Available": bool(overext_available),
                "RSI(14)": rsi_last,
                "Stretch vs MA50(%)": stretch_pct,
                "1M Return(%)": m1,
                "Risk": risk_text,
            }
        )

    score_df = pd.DataFrame(rows)
    if score_df.empty:
        progress.empty()
        return score_df

    # 기술적 팩터(Early RS·거래량 가속·RSI) 중 하나라도 계산 불가면 최종 랭크에서 제외
    score_df = score_df.dropna(subset=["Early RS Raw", "Vol Accel Raw", "RSI(14)"])
    if score_df.empty:
        progress.empty()
        return score_df

    _nw_em = score_df["Narrative Why"].isin(SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS)
    score_df.loc[_nw_em, "Narrative Raw"] = 0.0

    final_s, n_s, e_s, v_s, f_s, o_s = emerging_final_weighted_score(score_df)
    score_df["Final Score"] = final_s
    score_df["Narrative Score"] = n_s
    score_df["Early RS Score"] = e_s
    score_df["Vol Accel Score"] = v_s
    score_df["Fundamentals Score"] = f_s
    score_df["Overextension Score"] = o_s
    score_df = _scanner_score_df_format_for_display(score_df, "emerging")
    score_df = score_df.sort_values(["Final Score", "Ticker"], ascending=[False, True], na_position="last").reset_index(
        drop=True
    )

    progress.progress(1.0, text="Emerging 스캐너 계산 완료")
    return score_df


def fetch_market_news():
    """
    FMP Stock News API 기반 뉴스 수집 (레거시 호환용 단순 텍스트 반환).
    fetch_global_market_news()의 경량 버전으로, 일부 서브 프롬프트에서 사용.
    FMP 키 없을 시 Yahoo Finance RSS fallback.
    """
    k = _fmp_key()
    chunks = []

    if k:
        try:
            r = requests.get(
                f"{_FMP_BASE}/news/stock-latest?page=0&limit=20&apikey={k}",
                timeout=_FMP_TIMEOUT,
            )
            if r.status_code == 200:
                items = r.json() if isinstance(r.json(), list) else []
                for idx, item in enumerate(items[:20], start=1):
                    title = _clean_news_text(item.get("title", ""))
                    text = _clean_news_text(item.get("text", "") or item.get("summary", ""))[:200]
                    tickers = str(item.get("tickers", "") or "").strip()
                    if title:
                        line = f"[{idx}] Title: {title}"
                        if tickers:
                            line += f" | Tickers: {tickers}"
                        if text:
                            line += f"\nSummary: {text}"
                        chunks.append(line)
                if chunks:
                    return "\n\n".join(chunks).strip()
        except Exception:
            pass

    # Fallback: Yahoo Finance RSS
    try:
        feed = feedparser.parse("https://finance.yahoo.com/news/rssindex")
        entries = getattr(feed, "entries", [])[:20]
        for idx, entry in enumerate(entries, start=1):
            title = _clean_news_text(getattr(entry, "title", ""))
            summary = _clean_news_text(getattr(entry, "summary", "") or "")
            if title or summary:
                chunks.append(f"[{idx}] Title: {title}\nSummary: {summary}")
        return "\n\n".join(chunks).strip()
    except Exception:
        return ""


def _to_utc_datetime(struct_time_obj):
    if not struct_time_obj:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime(*struct_time_obj[:6], tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _clean_news_text(raw_text, max_len: int = 0):
    """HTML 태그 제거 + 공백 정규화. max_len > 0 이면 해당 길이로 truncate."""
    text = str(raw_text or "").strip()
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_len > 0:
        text = text[:max_len]
    return text


# ---------------------------------------------------------------------------
# FMP 뉴스 파이프라인 헬퍼
# ---------------------------------------------------------------------------

def _fmp_news_fetch(endpoint: str, params: dict, timeout: int = 8) -> list:
    """
    FMP 뉴스 API 단일 호출 래퍼.
    성공 시 list[dict] 반환, 실패 시 [] 반환.
    """
    k = _fmp_key()
    if not k:
        return []
    try:
        p = {**params, "apikey": k}
        r = requests.get(f"{_FMP_BASE}/{endpoint}", params=p, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _fmp_news_to_unified(items: list, source_label: str, weight: float,
                         body_key: str = "text", max_body: int = 300) -> list:
    """
    FMP 뉴스 응답 → 통합 dict 포맷 변환.
    통합 포맷: {title, summary, tickers, published, published_dt, source, weight}
    """
    result = []
    for item in items:
        title = _clean_news_text(item.get("title", ""))
        body  = _clean_news_text(item.get(body_key, "") or item.get("text", "") or item.get("content", ""), max_len=max_body)
        pub_raw = str(item.get("publishedDate", "") or item.get("date", "") or "").strip()
        tickers = _clean_news_text(item.get("tickers", "") or item.get("symbol", ""))

        # publishedDate → datetime (FMP: "2025-05-28 14:30:00" 형식)
        pub_dt = datetime.min.replace(tzinfo=timezone.utc)
        if pub_raw:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    pub_dt = datetime.strptime(pub_raw[:19], fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue

        if not title:
            continue
        result.append({
            "title":        title,
            "summary":      body,
            "tickers":      tickers,
            "published":    pub_raw if pub_raw else "N/A",
            "published_dt": pub_dt,
            "source":       source_label,
            "weight":       weight,
        })
    return result


def _rss_news_fallback() -> list:
    """
    FMP API 키 없거나 모든 레이어 실패 시 RSS fallback.
    기존 4개 소스 유지.
    """
    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    rss_sources = {
        "Yahoo Finance":      ("https://finance.yahoo.com/news/rssindex", 1.0),
        "CNBC":               ("https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000", 0.9),
        "Google News Finance":("https://news.google.com/rss/search?q=finance+market+economy&hl=en-US&gl=US&ceid=US:en", 0.8),
        "MarketWatch":        ("http://feeds.marketwatch.com/marketwatch/marketpulse/", 0.7),
    }
    all_news = []
    for source_name, (feed_url, weight) in rss_sources.items():
        try:
            response = requests.get(feed_url, headers=browser_headers, timeout=8)
            if response.status_code != 200:
                continue
            feed = feedparser.parse(response.content)
            for entry in getattr(feed, "entries", []) or []:
                title   = _clean_news_text(getattr(entry, "title", ""))
                summary = _clean_news_text(getattr(entry, "summary", "") or getattr(entry, "description", ""), max_len=300)
                pub_raw = str(getattr(entry, "published", "") or getattr(entry, "updated", "") or "").strip()
                pub_dt  = _to_utc_datetime(
                    getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
                )
                if not (title or summary):
                    continue
                all_news.append({
                    "title": title, "summary": summary, "tickers": "",
                    "published": pub_raw or "N/A", "published_dt": pub_dt,
                    "source": f"RSS/{source_name}", "weight": weight,
                })
        except Exception:
            continue
    return all_news


def _dedup_and_rank(all_news: list, top_n: int = 60) -> list:
    """제목 유사도 기반 중복 제거 → (weight DESC, published_dt DESC) 정렬 → top_n 반환."""
    similarity_threshold = 0.7
    deduped = []
    for news in all_news:
        dup_idx = None
        for idx, kept in enumerate(deduped):
            if SequenceMatcher(None, news["title"].lower(), kept["title"].lower()).ratio() >= similarity_threshold:
                dup_idx = idx
                break
        if dup_idx is None:
            deduped.append(news)
        else:
            kept = deduped[dup_idx]
            if news["weight"] > kept["weight"]:
                deduped[dup_idx] = news
            elif news["weight"] == kept["weight"] and news["published_dt"] > kept["published_dt"]:
                deduped[dup_idx] = news

    ranked = sorted(
        deduped,
        key=lambda x: (x.get("weight", 0.0), x.get("published_dt", datetime.min.replace(tzinfo=timezone.utc))),
        reverse=True,
    )
    return ranked[:top_n]


def _build_news_context_text(top_news: list) -> str:
    """
    LLM 프롬프트용 뉴스 컨텍스트 문자열 생성.
    소스 유형별로 섹션을 분리해서 Gemini가 Press Release / Stock News / General을
    다른 가중치로 처리할 수 있도록 구조화.
    """

    # 소스 유형 분류 기준 및 섹션 정의
    # (매칭 키워드, 섹션 헤더, 설명)
    section_defs = [
        (
            "FMP Press Release",
            "━━━ [SECTION A] Press Releases — 고임팩트 기업 공시 (최우선 참고) ━━━",
            "어닝 발표·M&A·가이던스 등 주가에 직접 영향을 주는 공식 기업 공시입니다.\n"
            "이 섹션의 Tickers 종목은 즉각적인 가격 반응이 확인된 최우선 winners 후보입니다.",
        ),
        (
            "FMP Stock News",
            "━━━ [SECTION B] Stock News — 종목 티커 태그 포함 뉴스 ━━━",
            "각 기사에 관련 종목 티커가 태그되어 있습니다.\n"
            "Tickers 필드에 명시된 종목을 해당 테마의 직접 수혜주 후보로 간주하세요.",
        ),
        (
            "FMP Article",
            "━━━ [SECTION C] FMP Articles — 전문 애널리스트 분석 기사 ━━━",
            "FMP 에디터의 심층 분석 기사입니다. 테마의 구조적 배경과 expanding_to 단계 설정에 활용하세요.",
        ),
        (
            "FMP General News",
            "━━━ [SECTION D] General / Macro News — 거시경제 맥락 ━━━",
            "금리·환율·정책 등 매크로 뉴스입니다. regime(Risk On/Off, liquidity) 판단에 활용하세요.",
        ),
        (
            "RSS",
            "━━━ [SECTION E] RSS Fallback News ━━━",
            "RSS 수집 뉴스입니다. FMP 데이터가 없을 때 사용됩니다.",
        ),
    ]

    # 소스별 분류
    buckets: dict[str, list] = {key: [] for key, _, _ in section_defs}
    buckets["__etc__"] = []

    for item in top_news:
        src = str(item.get("source", "") or "")
        matched = False
        for key, _, _ in section_defs:
            if key in src:
                buckets[key].append(item)
                matched = True
                break
        if not matched:
            buckets["__etc__"].append(item)

    sections = []
    global_idx = 1

    for key, header, desc in section_defs:
        items = buckets.get(key, [])
        if not items:
            continue
        chunks = []
        for item in items:
            lines = [f"[{global_idx}] Published: {item.get('published', 'N/A')}"]
            tickers = str(item.get("tickers", "") or "").strip()
            if tickers:
                lines.append(f"  Tickers: {tickers}")
            lines.append(f"  Title: {item.get('title', '')}")
            content = str(item.get("summary", "") or "").strip()
            if content:
                lines.append(f"  Content: {content}")
            chunks.append("\n".join(lines))
            global_idx += 1
        sections.append(f"{header}\n{desc}\n\n" + "\n\n".join(chunks))

    # 분류 안 된 기타 항목
    if buckets["__etc__"]:
        chunks = []
        for item in buckets["__etc__"]:
            lines = [f"[{global_idx}] Source: {item.get('source','Unknown')} | Published: {item.get('published','N/A')}"]
            tickers = str(item.get("tickers", "") or "").strip()
            if tickers:
                lines.append(f"  Tickers: {tickers}")
            lines.append(f"  Title: {item.get('title', '')}")
            content = str(item.get("summary", "") or "").strip()
            if content:
                lines.append(f"  Content: {content}")
            chunks.append("\n".join(lines))
            global_idx += 1
        sections.append("━━━ [SECTION F] Other News ━━━\n\n" + "\n\n".join(chunks))

    return "\n\n\n".join(sections).strip()


def fetch_global_market_news():
    """
    FMP 4-레이어 뉴스 파이프라인 (RSS fallback 포함).

    레이어 구조 및 가중치:
      Layer A  Stock News       weight=1.2  티커 태그 포함 → winners 직결
      Layer B  Press Releases   weight=1.3  어닝/M&A/가이던스 등 고임팩트
      Layer C  FMP Articles     weight=1.0  FMP 에디터 분석 기사
      Layer D  General News     weight=0.8  매크로/경제 맥락

    FMP 키 없거나 전체 레이어 수집 0건 → RSS fallback 자동 전환.

    Returns:
      top_news:     List[dict]  (Top 60, published_dt 제거됨)
      context_text: str         (LLM 프롬프트용 텍스트)
      raw_count:    int         (중복 제거 전 수집 건수)
    """
    k = _fmp_key()
    all_news: list = []
    source_log: list[str] = []

    if k:
        # ── Layer A: Stock News (티커 태그 포함, 가장 중요) ──────────────────
        stock_items = _fmp_news_fetch("news/stock-latest", {"page": 0, "limit": 50})
        layer_a = _fmp_news_to_unified(stock_items, "FMP Stock News", weight=1.2, body_key="text", max_body=300)
        all_news.extend(layer_a)
        source_log.append(f"Stock News {len(layer_a)}건")

        # ── Layer B: Press Releases (고임팩트 기업 이벤트) ───────────────────
        press_items = _fmp_news_fetch("press-releases-latest", {"page": 0, "limit": 30})
        layer_b = _fmp_news_to_unified(press_items, "FMP Press Release", weight=1.3, body_key="text", max_body=300)
        all_news.extend(layer_b)
        source_log.append(f"Press Releases {len(layer_b)}건")

        # ── Layer C: FMP Articles (분석 깊이) ────────────────────────────────
        article_items = _fmp_news_fetch("fmp-articles", {"page": 0, "limit": 20})
        layer_c = _fmp_news_to_unified(article_items, "FMP Article", weight=1.0, body_key="content", max_body=300)
        all_news.extend(layer_c)
        source_log.append(f"FMP Articles {len(layer_c)}건")

        # ── Layer D: General News (매크로 맥락) ──────────────────────────────
        general_items = _fmp_news_fetch("news/general-latest", {"page": 0, "limit": 20})
        layer_d = _fmp_news_to_unified(general_items, "FMP General News", weight=0.8, body_key="text", max_body=200)
        all_news.extend(layer_d)
        source_log.append(f"General News {len(layer_d)}건")

    raw_count = len(all_news)

    # FMP 키 없거나 전체 수집 0건 → RSS fallback
    if raw_count == 0:
        all_news = _rss_news_fallback()
        raw_count = len(all_news)
        source_log = [f"RSS Fallback {raw_count}건"]

    top_news = _dedup_and_rank(all_news, top_n=60)
    context_text = _build_news_context_text(top_news)

    # session_state에 소스 수집 현황 기록 (UI 디버그용)
    st.session_state["_news_source_log"] = " | ".join(source_log)

    # published_dt는 내부용이므로 최종 반환 전 제거
    for item in top_news:
        item.pop("published_dt", None)

    return top_news, context_text, raw_count


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_fetch_global_market_news_pack():
    return fetch_global_market_news()


def fetch_targeted_news(query):
    """
    Fetch targeted Google News RSS results for a ticker/theme query.
    Returns top 20 entries as list[dict].
    """
    cleaned_query = str(query or "").strip()
    if not cleaned_query:
        return []

    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    encoded_query = requests.utils.quote(cleaned_query)
    url = (
        f"https://news.google.com/rss/search?q={encoded_query}+stock+market"
        "&hl=en-US&gl=US&ceid=US:en"
    )

    try:
        response = requests.get(url, headers=browser_headers, timeout=8)
        if response.status_code != 200:
            return []
        feed = feedparser.parse(response.content)
    except Exception:
        return []

    targeted_news = []
    for entry in (getattr(feed, "entries", []) or []):
        title = _clean_news_text(getattr(entry, "title", ""))
        summary = _clean_news_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
        published_raw = str(getattr(entry, "published", "") or getattr(entry, "updated", "") or "").strip()
        if not (title or summary):
            continue
        targeted_news.append(
            {
                "title": title,
                "summary": summary,
                "published": published_raw if published_raw else "N/A",
                "source": "Google News",
            }
        )

    return targeted_news[:20]


def _gemini_response_text_utf8_safe(response) -> str:
    """google-genai `response.text`를 UTF-8로 정규화한 뒤 strip. `json.loads` 직전에 사용."""
    raw_text = getattr(response, "text", None)
    if isinstance(raw_text, str):
        return raw_text.encode("utf-8", "ignore").decode("utf-8").strip()
    return str(raw_text or "").strip()


def analyze_deep_dive(query, news_data, language):
    if not query or not news_data:
        return {}

    chunks = []
    for idx, item in enumerate(news_data[:20], start=1):
        chunks.append(
            "\n".join(
                [
                    f"[{idx}] Source: {item.get('source', 'Unknown')}",
                    f"Published: {item.get('published', 'N/A')}",
                    f"Title: {item.get('title', '')}",
                    f"Summary: {item.get('summary', '')}",
                ]
            )
        )
    news_text = "\n\n".join(chunks).strip()

    deep_dive_model = _GenAIModel(
        "gemini-2.5-flash",
        generation_config={
            "temperature": 0.0,
            "top_p": 1,
            "top_k": 1,
            "max_output_tokens": 8192,
            "thinking_budget": 0,
            "response_mime_type": "application/json",
        },
    )
    target_language = "Korean" if language == "ko" else "English"
    prompt = f"""
You are a rigorous Wall Street equity research analyst.
Analyze ONLY the provided news about "{query}".

Hard rules:
1) Use only facts inferable from the provided news. Do not hallucinate.
2) If evidence is weak or conflicting, explicitly state uncertainty.
3) Return ONLY valid JSON. No markdown, no extra text.
4) Keep each field concise but specific.
5) You MUST translate and write the entire response strictly in {target_language}. Do not mix languages. If the target language is Korean, write in natural, professional financial Korean. If English, write in Wall Street analyst style.

[News Data]
{news_text}

[Output JSON schema]
{{
  "company_overview": "이 기업(또는 테마)이 정확히 어떤 비즈니스를 하는지 간략한 요약",
  "recent_catalyst": "최근 주가가 급등하거나 급락한 핵심적인 이유 (최근 뉴스 기반 팩트체크)",
  "market_sentiment": "현재 월가 및 시장의 반응 (Bullish/Bearish/Neutral 및 이유)",
  "forward_outlook": "향후 전망 및 관전 포인트 (실적 발표, 신제품, 거시경제 영향 등)"
}}
"""
    try:
        response = deep_dive_model.generate_content(prompt)
        raw_text = _gemini_response_text_utf8_safe(response)
        if not raw_text:
            return {}
        return json.loads(raw_text)
    except Exception:
        return {}


@st.cache_data(ttl=3600, show_spinner=False)
def compute_quantitative_sector_leaders(top_n: int = 30) -> dict:
    """
    ETF Universe에서 정량 지표로 유망 섹터/종목을 선별.
    반환: {
        "leaders": [{"ticker", "rs_score", "mom_1m", "mom_3m", "vol_surge", "above_ma200"}],
        "top_sectors": [섹터 ETF 티커],
        "summary_text": "Gemini 입력용 요약 텍스트"
    }
    """
    try:
        universe = read_etf_universe_file_tickers()
        if not universe:
            return {"leaders": [], "top_sectors": [], "summary_text": ""}

        # 핵심 섹터 ETF (RS 계산용)
        sector_etfs = ["XLK", "XLV", "XLF", "XLE", "XLY", "XLI", "XLC", "XLU", "XLRE", "XLB"]
        spy_ticker = "SPY"

        # 전체 유니버스 + SPY 가격 다운로드
        all_tickers = list(dict.fromkeys(universe + [spy_ticker] + sector_etfs))

        try:
            batch_ql = _fmp_batch_price_history(all_tickers, limit=252)
            closes = pd.DataFrame({tk: df["Close"] for tk, df in batch_ql.items() if "Close" in df.columns}).sort_index()
            volumes = pd.DataFrame({tk: df["Volume"] for tk, df in batch_ql.items() if "Volume" in df.columns}).sort_index()
        except Exception:
            return {"leaders": [], "top_sectors": [], "summary_text": ""}

        if closes.empty:
            return {"leaders": [], "top_sectors": [], "summary_text": ""}

        # SPY 기준 RS 계산
        spy_series = pd.to_numeric(closes.get(spy_ticker, pd.Series(dtype=float)), errors="coerce").dropna()

        leaders = []
        for tk in universe:
            if tk not in closes.columns:
                continue
            try:
                price = pd.to_numeric(closes[tk], errors="coerce").dropna()
                if len(price) < 63:
                    continue

                # 1개월·3개월 수익률
                mom_1m = float((price.iloc[-1] / price.iloc[-22] - 1.0) * 100) if len(price) >= 22 else np.nan
                mom_3m = float((price.iloc[-1] / price.iloc[-63] - 1.0) * 100) if len(price) >= 63 else np.nan

                # RS Score (SPY 대비 3개월 수익률)
                if len(spy_series) >= 63:
                    spy_3m = float((spy_series.iloc[-1] / spy_series.iloc[-63] - 1.0) * 100)
                    rs_score = float(mom_3m - spy_3m) if pd.notna(mom_3m) else np.nan
                else:
                    rs_score = np.nan

                # 200일선 위/아래
                ma200 = float(price.rolling(200, min_periods=150).mean().iloc[-1]) if len(price) >= 150 else np.nan
                above_ma200 = bool(price.iloc[-1] > ma200) if pd.notna(ma200) else None

                # 거래량 급증 (최근 5일 vs 21일 평균)
                vol_surge = np.nan
                if not volumes.empty and tk in volumes.columns:
                    vol = pd.to_numeric(volumes[tk], errors="coerce").dropna()
                    if len(vol) >= 21:
                        recent_vol = float(vol.tail(5).mean())
                        baseline_vol = float(vol.tail(21).mean())
                        vol_surge = float(recent_vol / baseline_vol) if baseline_vol > 0 else np.nan

                leaders.append({
                    "ticker": tk,
                    "rs_score": round(rs_score, 2) if pd.notna(rs_score) else None,
                    "mom_1m": round(mom_1m, 2) if pd.notna(mom_1m) else None,
                    "mom_3m": round(mom_3m, 2) if pd.notna(mom_3m) else None,
                    "vol_surge": round(vol_surge, 2) if pd.notna(vol_surge) else None,
                    "above_ma200": above_ma200,
                })
            except Exception:
                continue

        # RS Score 기준 정렬 → Top N
        leaders_sorted = sorted(
            [l for l in leaders if l["rs_score"] is not None],
            key=lambda x: x["rs_score"],
            reverse=True
        )[:top_n]

        # 섹터 ETF RS 랭킹
        sector_rs = []
        for se in sector_etfs:
            found = next((l for l in leaders if l["ticker"] == se), None)
            if found and found["rs_score"] is not None:
                sector_rs.append((se, found["rs_score"]))
        sector_rs.sort(key=lambda x: x[1], reverse=True)
        top_sectors = [s[0] for s in sector_rs[:5]]

        # Gemini 입력용 요약 텍스트 생성
        lines = ["[정량 스크리닝 결과 — Gemini 참고용]"]
        lines.append(f"SPY 대비 RS 상위 {len(leaders_sorted)}개 ETF/종목:")
        for l in leaders_sorted[:15]:
            ma_str = "✅200일선 위" if l["above_ma200"] else "❌200일선 아래" if l["above_ma200"] is False else ""
            vol_str = f"거래량 급증 {l['vol_surge']:.1f}x" if l["vol_surge"] and l["vol_surge"] >= 1.3 else ""
            lines.append(
                f"  {l['ticker']}: RS={l['rs_score']:+.1f}%p | 1M={l['mom_1m']:+.1f}% | 3M={l['mom_3m']:+.1f}% {ma_str} {vol_str}"
            )
        if top_sectors:
            lines.append(f"강세 섹터 ETF 상위 5: {', '.join(top_sectors)}")

        return {
            "leaders": leaders_sorted,
            "top_sectors": top_sectors,
            "summary_text": "\n".join(lines),
        }

    except Exception:
        return {"leaders": [], "top_sectors": [], "summary_text": ""}


def generate_market_narrative(news_text, target_language, quant_data: dict = None):
    if not news_text:
        return {}

    st.session_state["last_gemini_raw_text"] = ""
    language_label = "한국어" if target_language == "ko" else "English"

    # 정량 데이터 섹션 구성
    quant_section = ""
    if quant_data and quant_data.get("summary_text"):
        quant_section = f"""
[정량 스크리닝 데이터 — 실제 가격/거래량 기반]
{quant_data['summary_text']}

중요: winners 선정 시 위 정량 데이터에서 RS Score가 양수(+)이고 200일선 위에 있는 종목을 우선적으로 포함하세요.
뉴스 테마와 정량 모멘텀이 일치하는 종목이 가장 신뢰도 높은 Winners입니다.
"""

    prompt = f"""
당신은 월가 수석 퀀트 전략가입니다.
아래 뉴스와 정량 스크리닝 데이터를 종합 분석하여 지정된 JSON 스키마 그대로만 응답하세요.

핵심 원칙:
- 뉴스(정성) + 가격 모멘텀(정량)이 동시에 확인된 종목만 Winners로 선정
- 정량 데이터에서 RS Score 양수 + 200일선 위 종목을 우선 포함
- 뉴스만 좋고 가격이 안 받쳐주는 종목은 emerging으로만 분류
- 각 theme의 winners는 반드시 3~6개 티커

뉴스 소스 유형별 활용 지침 (매우 중요):
- [SECTION A] Press Releases: 기업이 직접 발표한 공식 공시입니다.
  해당 섹션의 "Tickers:" 필드에 태그된 종목은 실적·M&A·가이던스 등 고임팩트 이벤트가 확인된 종목이므로,
  정량 데이터와 방향이 일치하면 반드시 winners의 최우선 후보로 선정하세요.
- [SECTION B] Stock News: 각 기사의 "Tickers:" 필드는 해당 뉴스와 직접 연관된 종목입니다.
  동일 티커가 여러 기사에 반복 등장할수록 모멘텀이 강한 종목으로 간주하세요.
- [SECTION C] FMP Articles: 테마의 구조적 맥락과 expanding_to 단계 설정에 활용하세요.
- [SECTION D] General/Macro News: regime(Risk On/Off, liquidity 방향) 판단에 주로 활용하세요.

중요 규칙:
1) 반드시 순수 JSON 텍스트만 출력 (```json 같은 마크다운 금지)
2) 모든 키를 빠짐없이 포함
3) themes는 최소 3개 이상 생성
4) winners/emerging은 티커를 쉼표로 구분한 문자열
5) 각 theme의 expanding_to는 반드시 객체 배열(list)이어야 함
6) expanding_to의 각 객체는 반드시 "stage"와 "expected_tickers" 키를 포함
7) expected_tickers는 각 stage마다 반드시 2~4개 티커를 쉼표 구분 문자열로 작성
8) momentum_note: 반드시 "강함", "보통", "약함" 셋 중 하나만 출력 (설명 금지, 큰따옴표 사용 금지)
9) 결과는 반드시 {language_label}로, 금융 전문 용어를 사용하여 가장 자연스럽게 작성
10) winners/emerging 선정 근거: 뉴스의 Tickers 태그 + 반복 등장 횟수 + 정량 RS Score를 종합해 판단하세요.
    근거 없이 유명 대형주를 임의로 채워 넣는 것을 금지합니다.
{quant_section}
[뉴스 데이터 — 소스 유형별 섹션 구분]
{news_text}

[출력 JSON 스키마]
{{
  "regime": {{
    "risk": "Risk On 또는 Risk Off",
    "growth_value": "Growth 선호 또는 Value 선호",
    "liquidity": "Expanding 또는 Tightening"
  }},
  "themes": [
    {{
      "title": "테마명 (예: AI Capex Expansion)",
      "driver": "무엇이 이 테마를 촉발했는가? Press Release·Stock News 기반 구체적 근거 포함",
      "winners": "정량+정성 모두 확인된 수혜주 (예: NVDA, MSFT, SOXX)",
      "emerging": "뉴스 Tickers 태그는 있으나 정량 가격 확인 필요 종목 (예: ARM, MRVL)",
      "momentum_note": "강함/보통/약함 중 하나만 선택 (예: 강함)",
      "expanding_to": [
        {{"stage": "기업용 AI 솔루션", "expected_tickers": "CRM, NOW, WDAY"}},
        {{"stage": "AI 기반 사이버 보안", "expected_tickers": "CRWD, PANW, FTNT"}}
      ],
      "risk": "이 테마가 무너질 수 있는 위험 요인"
    }}
  ],
  "rotation": "과열 섹터 -> 수혜 섹터 플로우 요약 (예: Tech -> Industrials)",
  "top_quant_picks": "정량 스크리닝 상위 종목 중 내러티브와 일치하는 최우선 종목 3~5개 (쉼표 구분)",
  "summary": "월가 퀀트 리포트 스타일 전체 시장 핵심 요약 (뉴스+모멘텀 종합, 기관 vs 개인 뷰 차이 포함)"
}}
You MUST respond ONLY with a valid JSON object. No markdown tags, no greetings.
"""
    try:
        response = model.generate_content(prompt)
        raw_text = _gemini_response_text_utf8_safe(response)
        st.session_state["last_gemini_raw_text"] = raw_text
        if not raw_text:
            return {}

        # JSON 자동 복구: 3단계 파싱 시도
        def _try_parse_json(text):
            try:
                return json.loads(text)
            except Exception:
                pass
            try:
                c = re.sub(r"^```json", "", text.strip(), flags=re.IGNORECASE)
                c = re.sub(r"^```", "", c.strip())
                c = re.sub(r"```$", "", c.strip())
                return json.loads(c.strip())
            except Exception:
                pass
            return None

        result_data = _try_parse_json(raw_text)
        if result_data is None:
            err_lower = ""
            try:
                json.loads(raw_text)
            except Exception as _pe:
                err_lower = str(_pe).lower()
            if any(t in err_lower for t in ["429", "resourceexhausted", "quota"]):
                st.warning("⚠️ API 요청 한도를 초과했습니다. 약 1분 후 다시 시도해주세요.")
            elif any(t in err_lower for t in ["safety", "blocked"]):
                st.warning("⚠️ AI 안전 필터에 의해 분석이 차단되었습니다.")
            else:
                st.error("❌ Gemini 응답 파싱에 실패했습니다.")
                st.error(f"🤖 Gemini 실제 답변 원문:\n\n{raw_text}")
            result_data = {}
        return result_data
    except Exception as e:
        st.error("❌ JSON 파싱 에러가 발생했습니다.")
        st.error(f"에러 메시지: {e}")
        return {}

def translate_narrative_json(json_data, target_language):
    if not isinstance(json_data, dict) or not json_data:
        return {}

    language_label = "한국어" if target_language == "ko" else "English"
    source_json = json.dumps(json_data, ensure_ascii=False)
    prompt = f"""
아래 JSON은 시장 내러티브 분석 결과입니다.
요청사항:
1) JSON 구조/키 이름은 절대 변경하지 마세요.
2) 값(value) 내용만 {language_label}로 번역하세요.
3) 직역이 아닌 금융/투자 실무에서 자연스러운 표현으로 번역하세요.
4) 티커, 수치, 방향성은 보존하세요.
5) 반드시 순수 JSON 텍스트만 출력 (```json 같은 마크다운 금지)

[원본 JSON]
{source_json}
"""
    try:
        response = model.generate_content(prompt)
        response_text = _gemini_response_text_utf8_safe(response)
        if not response_text:
            return {}
        return json.loads(response_text)
    except Exception:
        return {}


def _compact_narrative_record_for_timeseries(rec):
    """`Narratives` 시트에서 읽은 한 건을 시계열 LLM 입력용으로 축약."""
    if not isinstance(rec, dict):
        return None
    a = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
    themes = a.get("themes") if isinstance(a.get("themes"), list) else []
    theme_rows = []
    for th in themes[:12]:
        th = th if isinstance(th, dict) else {}
        theme_rows.append(
            {
                "title": str(th.get("title", "") or "")[:200],
                "winners": str(th.get("winners", "") or "")[:400],
                "risk": str(th.get("risk", "") or "")[:300],
            }
        )
    regime = a.get("regime") if isinstance(a.get("regime"), dict) else {}
    return {
        "saved_at": rec.get("saved_at"),
        "session_label": rec.get("session_label"),
        "language": rec.get("language"),
        "regime": regime,
        "themes": theme_rows,
        "rotation": str(a.get("rotation") or "")[:800],
        "summary": str(a.get("summary") or "")[:2000],
    }


def _filter_narrative_records_utc_range(records, start_utc, end_utc, end_inclusive=True):
    """saved_at 기준 UTC 구간 필터. end_inclusive=True면 end_utc까지 포함."""
    if not records:
        return []
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        dt = _narrative_parse_saved_at_utc(rec.get("saved_at"))
        if dt is None:
            continue
        if dt < start_utc:
            continue
        if end_inclusive:
            if dt > end_utc:
                continue
        else:
            if dt >= end_utc:
                continue
        out.append(rec)
    out.sort(
        key=lambda r: _narrative_parse_saved_at_utc(r.get("saved_at"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return out


def _filter_narrative_records_today_et(records):
    """미국 동부(ET) 달력 기준 '오늘'에 저장된 기록만."""
    now_et = datetime.now(_MARKET_ET_TZ)
    day_start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_et = now_et.replace(hour=23, minute=59, second=59, microsecond=999999)
    start_utc = day_start_et.astimezone(timezone.utc)
    end_utc = day_end_et.astimezone(timezone.utc)
    return _filter_narrative_records_utc_range(records, start_utc, end_utc, end_inclusive=True)


def _filter_narrative_records_last_days(records, days, anchor_utc=None):
    """anchor_utc 기준 최근 days일(당일 포함 롤링 윈도)."""
    anchor = anchor_utc or datetime.now(timezone.utc)
    start = anchor - timedelta(days=days)
    return _filter_narrative_records_utc_range(records, start, anchor, end_inclusive=True)


def _split_narrative_records_wow(records, anchor_utc=None):
    """
    이번 주: 최근 7일 [anchor-7d, anchor]
    저번 주: 그 이전 7일 [anchor-14d, anchor-7d)  (경계 중복 없음)
    """
    anchor = anchor_utc or datetime.now(timezone.utc)
    this_start = anchor - timedelta(days=7)
    last_start = anchor - timedelta(days=14)
    this_week = _filter_narrative_records_utc_range(records, this_start, anchor, end_inclusive=True)
    last_week = _filter_narrative_records_utc_range(records, last_start, this_start, end_inclusive=False)
    return this_week, last_week


def _narrative_timeseries_briefing_model():
    return _GenAIModel(
        "gemini-2.5-flash",
        generation_config={
            "temperature": 0.0,
            "top_p": 0.95,
            "top_k": 40,
            # 변곡점/주간 리포트: 본문 + 자동파싱 4개 섹션 + 티커 리스트가 길어
            # 4096 으로는 잘린다. 넉넉히 확보한다.
            "max_output_tokens": 16384,
            # Gemini 2.5 Flash 는 thinking 이 기본 ON 이고 사고 토큰이
            # max_output_tokens 예산을 함께 깎는다. 사고를 비활성화(0)하여
            # 전체 토큰 예산을 '보이는 출력'에 사용하도록 한다.
            "thinking_budget": 0,
        },
    )


def generate_weekly_portfolio_summary(portfolio_context: dict, narrative_context: list, macro_context: dict) -> str:
    """
    포트폴리오 현황 + 최근 내러티브 + Macro 지표를 묶어 Gemini로 주간 요약 리포트 생성.
    반환: 마크다운 문자열
    """
    portfolio_json = json.dumps(portfolio_context, ensure_ascii=False)
    narrative_json = json.dumps(narrative_context[:10], ensure_ascii=False)
    macro_json = json.dumps(macro_context, ensure_ascii=False)

    prompt = f"""
당신은 개인 투자자의 전담 퀀트 애널리스트입니다.
아래 세 가지 데이터를 종합해 **주간 포트폴리오 리뷰 리포트**를 한국어 마크다운으로 작성하세요.

[1] 포트폴리오 현황
{portfolio_json}

[2] 최근 AI 내러티브 요약 (최신 10개)
{narrative_json}

[3] 거시경제 지표 현황
{macro_json}

---
작성 규칙:
1) 데이터에 없는 내용은 추측하지 마세요.
2) 반드시 아래 섹션 구조를 지켜 마크다운으로 작성하세요.
3) 각 섹션은 간결하고 실전적으로, 투자 판단에 바로 쓸 수 있는 내용으로.

## 📊 이번 주 포트폴리오 요약
- 전체 수익률, 최고/최저 종목, 주목할 변화 한 줄씩

## 🌐 거시경제 환경 점검
- 현재 Macro 지표가 포트폴리오에 미치는 영향 (긍정/부정 요인)

## 🧠 AI 내러티브와 포트폴리오 연결
- 이번 주 AI가 강조한 테마와 내 포트폴리오 종목의 연관성
- 내러티브 상 수혜가 예상되는 보유 종목 vs 위험 종목

## ⚠️ 리스크 점검
- 고상관 종목 쌍, 섹터 편중, Drawdown 주의 종목

## 🎯 다음 주 액션 플랜
- 모니터링 우선순위 3가지 (매수/매도/관망 판단 기준 포함)
"""

    try:
        summary_model = _GenAIModel(
            "gemini-2.5-flash",
            generation_config={
                "temperature": 0.3,  # 주간 요약 — 매주 다른 인사이트를 위해 약간의 다양성 허용
                "top_p": 1,
                "top_k": 1,
                # 5개 섹션 마크다운 리포트 — 4096 으로는 잘릴 수 있어 확대 + 사고 비활성화
                "max_output_tokens": 16384,
                "thinking_budget": 0,
            },
        )
        response = summary_model.generate_content(prompt)
        raw = _gemini_response_text_utf8_safe(response)
        return raw if raw else ""
    except Exception as exc:
        return f"Weekly Summary 생성 실패: {exc}"


def _build_portfolio_context_for_summary(sell_radar_df: pd.DataFrame) -> dict:
    """sell_radar_df에서 Gemini 입력용 포트폴리오 context 생성."""
    if sell_radar_df is None or sell_radar_df.empty:
        return {}
    rows = []
    for _, r in sell_radar_df.iterrows():
        ret = pd.to_numeric(r.get("수익률(%)"), errors="coerce")
        alpha = pd.to_numeric(r.get("SPY Alpha(%)"), errors="coerce")
        dd = pd.to_numeric(r.get("Drawdown(%)"), errors="coerce")
        rows.append({
            "ticker": str(r.get("티커", "")),
            "account": str(r.get("계좌", "")),
            "return_pct": round(float(ret), 2) if pd.notna(ret) else None,
            "spy_alpha_pct": round(float(alpha), 2) if pd.notna(alpha) else None,
            "drawdown_pct": round(float(dd), 2) if pd.notna(dd) else None,
            "status": str(r.get("상태(Status)", "")),
        })
    total_gl = pd.to_numeric(sell_radar_df["투자 손익($)"], errors="coerce").sum()
    return {
        "total_gain_loss_usd": round(float(total_gl), 2) if pd.notna(total_gl) else None,
        "positions": rows,
    }


def _build_narrative_context_for_summary(user_id: str) -> list:
    """최근 내러티브에서 Gemini 입력용 context 생성 (최신 10개)."""
    records, _ = fetch_narrative_records_from_sheet()
    if not records:
        return []
    uid_u = str(user_id).strip().upper()
    user_recs = [
        r for r in reversed(records)
        if str(r.get("_sheet_user_id", "")).strip().upper() == uid_u
    ][:10]
    out = []
    for rec in user_recs:
        analysis = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
        saved_at = rec.get("saved_at", "")
        themes = analysis.get("themes", [])
        theme_titles = [str(t.get("title", "")) for t in themes if isinstance(t, dict)]
        out.append({
            "saved_at": saved_at,
            "session": rec.get("session_label", ""),
            "regime": analysis.get("regime", {}),
            "themes": theme_titles[:5],
            "rotation": str(analysis.get("rotation", ""))[:300],
            "winners_csv": str(rec.get("_sheet_winners_csv", ""))[:200],
        })
    return out


def _build_macro_context_for_summary() -> dict:
    """cached_analyze_us_macro_dashboard에서 Gemini 입력용 macro context 생성."""
    try:
        macro_rows = cached_analyze_us_macro_dashboard()
        if not macro_rows:
            return {}
        out = {}
        for row in macro_rows:
            label = str(row.get("지표", ""))
            val = str(row.get("현재값", ""))
            status = str(row.get("_status", ""))
            note = str(row.get("판독 요약", ""))[:100]
            out[label] = {"value": val, "status": status, "note": note}
        return out
    except Exception:
        return {}


def _save_weekly_summary_to_narratives(user_id: str, summary_text: str) -> tuple[bool, str]:
    """주간 요약을 Narratives 시트에 저장."""
    if not summary_text or not user_id:
        return False, "내용 또는 user_id가 없습니다."
    rec = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "language": "ko",
        "session_label": "📋 Weekly Portfolio Summary",
        "analysis": {
            "source": "weekly_portfolio_summary",
            "summary": summary_text,
            "themes": [],
            "regime": {},
            "rotation": "",
        },
    }
    row = _narrative_record_to_sheet_row(rec, user_id)
    ok, err = append_narrative_row_to_sheet(row)
    return ok, err


def run_narrative_timeseries_gemini(kind, records_payload, target_language):
    """
    kind: 'daily' | 'weekly' | 'wow'
    records_payload: LLM에 넣을 dict (이미 구간 필터·압축된 스냅샷 리스트 등)
    반환: 마크다운 문자열 또는 빈 문자열
    """
    language_label = "한국어" if target_language == "ko" else "English"
    payload_json = json.dumps(records_payload, ensure_ascii=False)
    if kind == "daily":
        task = """
당신은 월가 매크로 스트래티지스트입니다.
입력은 **미국 동부(ET) 달력 기준 오늘 하루**에 저장된 시장 내러티브 스냅샷들입니다(시간순).

다음을 **마크다운**으로 간결하지만 밀도 있게 작성하세요:
1) 오늘 하루 내러티브가 어떻게 **흘러갔는지**(시간대/세션 라벨이 있으면 활용)
2) 반복 등장한 **공통 승자(티커)·테마**
3) 레짐(regime) 변화가 있었다면 한 줄로
4) 투자자가 오늘 밤 준비할 **한 가지 체크포인트**

데이터에 없는 내용은 추측하지 마세요. 출력은 반드시 {lang}입니다.
""".format(
            lang=language_label
        )
    elif kind == "weekly":
        task = """
당신은 월가 매크로 스트래티지스트이자 액티브 트레이딩 데스크의 운영 파트너입니다.
입력은 **최근 7일(롤링)** 동안 저장된 시장 내러티브 스냅샷입니다(시간순).

[Part 1] 시장 요약 본문 (서술부)
다음을 **마크다운**으로 작성하세요:
1) 일주일 동안 **가장 강하게 유지된 메가 트렌드** 2~4개
2) **지속적으로 언급된 티커**(빈도·일관성 관점에서 상위)
3) rotation / 테마 확장 흐름에서 보이는 **자본 이동 가설**(보수적으로)
4) 다음 주 초반 **모니터링 우선순위** 3개

근거 없는 확정은 피하고, 데이터에 기반해 서술하세요. 본문은 반드시 {lang}로 작성합니다.

[Part 2] 실전 매매 연동 섹션 — 매우 중요 (1.6 Opportunity Scanner 자동 연동용)
전체적인 시장 요약 외에, 리포트 **하단에 반드시 다음 두 가지 섹션을 별도로 분리하여 작성**하라.
이 두 섹션은 1.6 스캐너가 정규식으로 자동 파싱하여 유니버스로 사용하므로,
아래 포맷을 **단 한 글자도** 어기지 마라.

A) 두 섹션의 **헤더 문자열은 고정**이며 그대로 사용한다(번역 금지, 이모지 포함, ## 레벨):
   ## 🏆 Weekly Winners (주간 대장주)
   ## 🚀 Weekly Expanding To (주간 후발/확장 수혜주)

B) 각 섹션은 아래 구성을 따른다:
   - 첫 줄: `테마: ...` — 해당 섹션의 핵심 테마/카테고리 1~3개를 쉼표로 구분
   - 그 다음 줄부터 **불릿 리스트**. 각 라인은 **정확히 티커 1개**만 담는다.
     라인 포맷(엄격):
       - **TICKER** — 한 줄 이유(약 20자, {lang})
     예: `- **NVDA** — AI 가속기 수요 가속`

C) 🏆 Weekly Winners (주간 대장주):
   - 일주일 내내 강한 모멘텀을 유지한 **핵심 주도 테마와 그 대표 티커**.
   - 5~10개 종목, **강한 순서**로 정렬.

D) 🚀 Weekly Expanding To (주간 후발/확장 수혜주):
   - 위 1차 대장주에서 **자금이 이동(Sector Rotation)** 중이거나, 공급망/인프라/2차 파생 수혜로
     다음 주 초반에 부상할 가능성이 높은 **순환매 후보 티커**.
   - 5~10개 종목, **기대도 높은 순**으로 정렬.
   - 🏆 Weekly Winners와의 **중복은 최소화**(가급적 0~1개).

[티커 표기 규칙 — 절대 준수]
- 본문 서술부와 두 섹션 모두에서, **모든 티커는 반드시 영문 대문자(UPPERCASE)** 로 표기한다.
  허용 문자: `[A-Z0-9.-]`, 길이 1~10자. 예) NVDA, MSFT, AVGO, BRK.B, MOG-A
- 두 섹션의 불릿 라인에는 **티커 1개만** 둔다.
  · 잘못된 예: `- **NVDA, AMD** — AI 칩 수혜`
  · 올바른 예: `- **NVDA** — AI 가속기 1위`  /  `- **AMD** — MI300 점유율 확대`
- 회사명·괄호·여러 티커 나열·소문자·풀네임은 금지(서술부에서도 동일).
- 일반 영어 약어(AI, ETF, US, FED, GDP, CEO 등 — 실제 상장 티커가 아닌 단어)는
  티커로 오인되지 않도록 **가급적 한국어로 풀어 쓰거나 소문자**로 적어라.
  (예: `AI` → `인공지능`, `FED` → `연준`)

[환각 방지]
- 입력 7일 데이터에 한 번도 등장하지 않은 **새 티커를 도입하지 말 것**.
- 근거가 약하면 해당 섹션을 비우지 말고, 가장 보수적인 후보 1~2개라도 제시하되
  이유 칸에 `데이터 부족 — 모니터링 후보` 와 같이 명시한다.
""".format(
            lang=language_label
        )
    else:
        task = """
당신은 월가 매크로 스트래티지스트입니다.
입력에는 **이번 주(최근 7일)** 스냅샷과 **저번 주(그 이전 7일)** 스냅샷이 구분되어 있습니다.

다음을 **마크다운**으로 작성하세요 (WoW = week-over-week):
1) **교집합**: 두 주 모두에서 살아남은 테마·티커
2) **차집합 / Narrative Drift**: 저번 주에는 A였는데 이번 주에는 B로 **돈·관심이 이동**한 흔적
3) **Fading**(사그라지는 내러티브)과 **Emerging**(부상하는 내러티브)을 명시적으로 구분
4) 한 문단 **Executive Summary** (투자 회의 브리핑 톤)

데이터 밖 환각 금지. 출력은 반드시 {lang}입니다.

[필수 — 자동 파싱 섹션: 아래 4개 헤더는 번역·수정 금지, 이모지 포함, ## 레벨 그대로 유지]
리포트 **하단**에 반드시 다음 4개 섹션을 추가하라. 시스템이 정규식으로 파싱하므로 헤더 문자열을 단 한 글자도 바꾸지 마라.

## 🏆 Weekly Winners (주간 대장주)
- 이번 주와 저번 주 **모두에서 강했던** 핵심 티커 (교집합 + 지속 모멘텀)
- 각 라인: `- **TICKER** — 한 줄 이유` (티커 1개/라인, 5~10개)

## 🚀 Weekly Expanding To (주간 후발/확장 수혜주)
- 이번 주 새로 부상하거나 다음 주 초반 순환매 수혜 예상 티커
- 각 라인: `- **TICKER** — 한 줄 이유` (티커 1개/라인, 5~10개)

## 🌱 Emerging (이번 주 새로 부상)
- 저번 주에는 없었고 이번 주에 새로 등장한 티커·테마
- 각 라인: `- **TICKER** — 한 줄 이유` (티커 1개/라인, 3~7개)

## 🥀 Fading (저번 주 대비 약화)
- 저번 주에는 강했지만 이번 주에 사그라든 티커·테마
- 각 라인: `- **TICKER** — 한 줄 이유` (티커 1개/라인, 3~7개)

[티커 표기 규칙 — 절대 준수]
- 모든 티커는 반드시 영문 대문자(UPPERCASE). 예) NVDA, MSFT, AVGO
- 각 불릿 라인에 티커 1개만. 잘못된 예: `- **NVDA, AMD** — ...`
- 일반 약어(AI, ETF, FED, GDP 등)는 티커로 표기 금지.
""".format(
            lang=language_label
        )

    prompt = f"""
{task}

[입력 JSON]
{payload_json}
"""
    try:
        briefing_model = _narrative_timeseries_briefing_model()
        response = briefing_model.generate_content(prompt)
        raw = str(getattr(response, "text", "") or "").strip()
        # text 가 비어도 candidates.parts 에 본문이 들어오는 경우가 있어 회수한다.
        if not raw and hasattr(response, "candidates"):
            try:
                for _c in (response.candidates or []):
                    for _p in getattr(getattr(_c, "content", None), "parts", []) or []:
                        raw += getattr(_p, "text", "") or ""
                raw = raw.strip()
            except Exception:
                pass
        # 토큰 한도로 잘렸는지(finish_reason == MAX_TOKENS) 확인하여 경고한다.
        try:
            _fr = ""
            for _c in (getattr(response, "candidates", None) or []):
                _fr = str(getattr(_c, "finish_reason", "") or "")
                break
            if "MAX_TOKENS" in _fr.upper():
                st.warning(
                    "⚠️ 분석 본문이 모델 출력 한도에 도달해 일부가 잘렸을 수 있습니다. "
                    "다시 시도하면 더 완전한 결과를 받을 수 있습니다."
                )
        except Exception:
            pass
        return raw
    except Exception as exc:
        err = str(exc).lower()
        if any(token in err for token in ["429", "resourceexhausted", "quota"]):
            st.warning("⚠️ API 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요.")
        elif any(token in err for token in ["safety", "blocked"]):
            st.warning("⚠️ 안전 필터에 의해 응답이 제한되었을 수 있습니다.")
        else:
            st.error("시계열 분석 중 오류가 발생했습니다.")
        return ""


if st.session_state.get("logged_in"):
    st.title("장기 투자 주식 분석 대시보드")
    st.caption(
        "[1단계] 거시·내러티브 → [2단계] 섹터·스캐너 → [3단계] 종목 정밀 → [4단계] 포트폴리오. "
        "왼쪽 사이드바 라디오에서 단계를 선택하세요."
    )
    
    render_global_market_watch_header()

    # ── ETF Universe 자동 업데이트 (주 1회) ─────────────────────────────────
    if str(st.session_state.get("user_id") or "").strip():
        if not st.session_state.get("_etf_auto_update_done_this_session"):
            st.session_state["_etf_auto_update_done_this_session"] = True
            try:
                _added_cnt, _add_err = run_etf_auto_update_if_needed(silent=True)
                if _added_cnt > 0:
                    st.session_state["_etf_new_added_count"] = _added_cnt
            except Exception:
                pass

    # ── Watchlist Alert 자동 체크 (매 세션 1회) ───────────────────────────
    _uid_alert = str(st.session_state.get("user_id") or "").strip()
    if _uid_alert and not st.session_state.get("_watchlist_alert_checked"):
        st.session_state["_watchlist_alert_checked"] = True
        try:
            _wl_items = load_watchlist_sheet(_uid_alert)
            if _wl_items:
                _wl_tickers = [i["ticker"] for i in _wl_items]
                _price_map = fetch_latest_prices_for_tickers(tuple(_wl_tickers))
                _rsi_map, _ma200_map = {}, {}
                for _tk in _wl_tickers:
                    try:
                        _hist = _fmp_price_history(_tk, limit=252)
                        _close = pd.to_numeric(_hist["Close"], errors="coerce").dropna() if not _hist.empty else pd.Series(dtype=float)
                        _rsi_map[_tk] = float(calculate_rsi(_close).dropna().iloc[-1]) if not calculate_rsi(_close).dropna().empty else np.nan
                        _ma200_map[_tk] = float(_close.rolling(200, min_periods=200).mean().iloc[-1]) if len(_close) >= 200 else np.nan
                    except Exception:
                        _rsi_map[_tk] = np.nan
                        _ma200_map[_tk] = np.nan
                _triggered = check_watchlist_alerts(_wl_items, _price_map, _rsi_map, _ma200_map)
                if _triggered:
                    st.session_state["_watchlist_triggered_alerts"] = _triggered
        except Exception:
            pass

    # Alert 발동 시 상단에 배너 표시
    _triggered_alerts = st.session_state.get("_watchlist_triggered_alerts", [])
    if _triggered_alerts:
        with st.container():
            st.warning(f"🔔 **Watchlist Alert** — {len(_triggered_alerts)}개 종목에서 매수 조건이 발동됐어요! `🔔 Buy Watchlist & Alert` 메뉴에서 확인하세요.")

    # 신규 ETF 자동 발견 알림
    _etf_new_cnt = st.session_state.pop("_etf_new_added_count", 0)
    if _etf_new_cnt > 0:
        st.success(f"🆕 **ETF Universe 자동 업데이트** — 최근 90일 내 신규 상장 ETF **{_etf_new_cnt}개**가 자동으로 추가됐어요! `[2단계] 섹터 & 자금 흐름`에서 확인하세요.")

    _nav_key = "main_sidebar_nav"
    _nav_opts = list(_MAIN_NAV_OPTIONS)
    if st.session_state.get("user_role") == "admin":
        _nav_opts.append(_NAV_ADMIN_APPROVAL)
    if "main_nav_idx" in st.session_state:
        try:
            _omi = int(st.session_state.pop("main_nav_idx"))
            if 0 <= _omi < len(_MAIN_NAV_OPTIONS):
                st.session_state[_nav_key] = _MAIN_NAV_OPTIONS[_omi]
        except (TypeError, ValueError):
            st.session_state.pop("main_nav_idx", None)
    _cur_nav = st.session_state.get(_nav_key)
    if _cur_nav not in _nav_opts:
        st.session_state[_nav_key] = _nav_opts[0]
    
    main_nav = st.sidebar.radio(
        "탑다운 단계",
        _nav_opts,
        key=_nav_key,
        label_visibility="collapsed",
    )
    
    default_ticker = st.session_state.get("selected_ticker", "TSLA")
    selected_ticker = st.sidebar.text_input(
        "분석할 주식 티커 (Ticker)",
        value=default_ticker,
        help="예: TSLA, AAPL, MSFT, 005930.KS",
    ).strip().upper()
    
    if not selected_ticker:
        selected_ticker = "TSLA"
        st.sidebar.warning("티커가 비어 있어 기본값 TSLA를 사용합니다.")
    
    st.session_state["selected_ticker"] = selected_ticker

    # 사이드바 투자 메모 + Alert 조건 (Quick Save)
    watch_note = st.sidebar.text_area(
        "투자 메모",
        key="watch_note_input",
        placeholder="매수 근거, 리스크, 체크포인트를 기록하세요."
    )

    # Alert 조건 (간단 버전)
    st.sidebar.caption("⚡ Alert 조건 (선택)")
    _alert_rsi_val = st.sidebar.number_input(
        "📉 RSI 이하 시 알림 (0=사용 안 함)",
        min_value=0.0, max_value=100.0,
        value=0.0, step=5.0, format="%.0f",
        key="sidebar_alert_rsi",
        help="예) 30 입력 → RSI 30 이하로 내려오면 알림"
    )
    _alert_ma200_val = st.sidebar.checkbox(
        "📊 200일선 ±3% 근접 시 알림",
        key="sidebar_alert_ma200",
        help="현재가가 200일 이동평균 ±3% 이내 진입 시 알림"
    )
    st.sidebar.caption("💡 목표가 등 상세 설정은 🔔 Watchlist 탭에서")

    if st.sidebar.button("현재 티커 저장", use_container_width=True, type="primary"):
        _uid_memo = str(st.session_state.get("user_id") or "").strip()
        _tk_save = str(selected_ticker).strip().upper()
        # ✅ 저장 시점 현재가를 캐시 우회하여 신선하게 조회
        try:
            fetch_latest_prices_for_tickers.clear()
        except Exception:
            pass
        _saved_p = fetch_latest_prices_for_tickers((_tk_save,)).get(_tk_save, np.nan)
        _ok_wl_save, _err_wl_save = add_to_watchlist(
            _uid_memo, _tk_save,
            memo=watch_note.strip(),
            alert_rsi=float(_alert_rsi_val) if _alert_rsi_val > 0 else None,
            alert_ma200=bool(_alert_ma200_val),
            saved_price=float(_saved_p) if pd.notna(_saved_p) else None,  # ✅ 명시적 전달
        )
        if _ok_wl_save:
            _alert_desc = []
            if _alert_rsi_val > 0:
                _alert_desc.append(f"RSI {_alert_rsi_val:.0f} 이하")
            if _alert_ma200_val:
                _alert_desc.append("200일선 근접")
            _alert_str = " + ".join(_alert_desc) if _alert_desc else "Alert 없음"
            st.sidebar.success(f"{_tk_save} 저장 완료 ({_alert_str})")
            st.rerun()
        else:
            st.sidebar.error(f"저장 실패: {_err_wl_save}")

    def _goto_watchlist():
        st.session_state["main_sidebar_nav"] = _MAIN_NAV_OPTIONS[7]

    st.sidebar.button(
        "🔔 Watchlist 전체 보기",
        key="sidebar_wl_goto",
        use_container_width=True,
        on_click=_goto_watchlist,
    )

    # Watchlist 종목 수 표시 (매 렌더링마다 API 호출 방지 - session_state 캐시 활용)
    _uid_sidebar = str(st.session_state.get("user_id") or "").strip()
    if "_sidebar_wl_count" not in st.session_state:
        try:
            _temp_wl = load_watchlist_sheet(_uid_sidebar)
            st.session_state["_sidebar_wl_count"] = len(_temp_wl)
        except Exception:
            st.session_state["_sidebar_wl_count"] = 0
    _wl_count = st.session_state.get("_sidebar_wl_count", 0)
    if _wl_count > 0:
        st.sidebar.caption(f"📋 Watchlist: {_wl_count}개 종목 저장됨")
    else:
        st.sidebar.caption("저장된 Watchlist 메모가 없습니다.")
    
    if st.sidebar.button("로그아웃", key="sidebar_logout_btn", use_container_width=True):
        st.session_state["logged_in"] = False
        st.session_state["user_role"] = None
        st.session_state["login_error"] = False
        st.session_state["login_feedback"] = ""
        st.session_state["login_user_id"] = ""
        st.session_state["user_id"] = ""
        st.rerun()
    
    quote_type, selected_ticker_obj, selected_ticker_info = detect_quote_type(selected_ticker)
    is_etf_mode = quote_type == "ETF"
    
    if main_nav == _MAIN_NAV_OPTIONS[0]:
        # ─────────────────────────────────────────────────────────────────────
        # 🚨 Daily Risk Gauge
        # ─────────────────────────────────────────────────────────────────────
        st.subheader("🚨 Daily Risk Gauge")
        st.caption("매일 접속 시 시장 선행 신호 6가지를 자동 스캔합니다. 선물 프리마켓·이벤트 캘린더로 당일 장 방향을 장 열기 전에 파악하세요.")

        drg_col1, drg_col2 = st.columns([2, 3])
        with drg_col1:
            sector_choice = st.selectbox(
                "📊 분석 섹터",
                options=["전체", "테크·반도체", "에너지", "금융", "헬스케어", "산업재", "소비재", "부동산"],
                key="drg_sector_choice",
            )
        with drg_col2:
            st.caption(f"선택 섹터: **{sector_choice}** | 30분 캐시")

        if st.button("🔄 지금 스캔", key="drg_refresh_btn", use_container_width=True):
            compute_daily_risk_gauge.clear()
            fetch_vix_latest_and_history.clear()
            fetch_fmp_economic_calendar_risk.clear()

        with st.spinner("📡 선행 신호 6가지 동시 분석 중... (약 5~10초)"):
            drg = compute_daily_risk_gauge(sector_filter=sector_choice)

        risk_score = drg["risk_score"]
        st.markdown(
            f"<div style='background:{drg['risk_color']}22;border:2px solid {drg['risk_color']};"
            f"border-radius:12px;padding:20px;margin:12px 0;text-align:center;'>"
            f"<div style='font-size:32px;font-weight:900;color:{drg['risk_color']}'>{drg['risk_level']}</div>"
            f"<div style='font-size:16px;margin-top:6px;'>{drg['risk_msg']}</div>"
            f"</div>",
            unsafe_allow_html=True
        )
        bar_width = int(risk_score / 10 * 100)
        st.markdown(
            f"<div style='background:#1e293b;border-radius:8px;padding:4px;margin:4px 0 8px 0;'>"
            f"<div style='background:{drg['risk_color']};width:{bar_width}%;height:20px;border-radius:6px;"
            f"display:flex;align-items:center;justify-content:center;color:white;font-size:12px;font-weight:700;'>"
            f"Risk {risk_score}/10"
            f"</div></div>",
            unsafe_allow_html=True
        )

        st.markdown("### 📡 선행 신호 6가지")
        sig = drg["signals"]
        s_col1, s_col2, s_col3, s_col4, s_col5, s_col6 = st.columns(6)
        signal_defs = [
            ("vix",        "VIX 방향",        "🌡️"),
            ("credit",     "신용 스프레드",    "💳"),
            ("leaders",    "대장주 모멘텀",    "🏆"),
            ("futures",    "프리마켓 방향",    "📈"),
            ("riskoff",    "달러·금·국채",     "🛡️"),
            ("event_risk", "이벤트 리스크",    "📅"),
        ]
        for col, (key, label, emoji) in zip([s_col1, s_col2, s_col3, s_col4, s_col5, s_col6], signal_defs):
            with col:
                s = sig.get(key, {})
                ok = s.get("ok", True)
                val = s.get("value", "N/A")
                trend = s.get("trend", "")
                status = "✅ 정상" if ok else "⚠️ 경고"
                color = "#16a34a" if ok else "#dc2626"
                st.markdown(
                    f"<div style='text-align:center;padding:10px;background:#1e293b;border-radius:8px;"
                    f"border:1px solid {color};'>"
                    f"<div style='font-size:20px'>{emoji}</div>"
                    f"<div style='font-size:11px;color:#94a3b8'>{label}</div>"
                    f"<div style='font-weight:700;color:{color}'>{status}</div>"
                    f"<div style='font-size:11px;color:#cbd5e1'>{val}</div>"
                    + (f"<div style='font-size:10px;color:#64748b;margin-top:2px'>{trend}</div>" if trend else "")
                    + f"</div>",
                    unsafe_allow_html=True
                )

        warnings_drg = drg.get("warnings", [])
        if warnings_drg:
            st.divider()
            st.markdown("### ⚠️ 감지된 경고 신호")
            for w in warnings_drg:
                st.warning(w)
        else:
            st.success("✅ 현재 감지된 선행 경고 신호 없음 — 시장 환경 정상")

        # ── 이벤트 캘린더 — 행동 지침 포함 ──────────────────────────────
        event_cal = drg.get("event_calendar", [])
        if event_cal:
            st.divider()
            st.markdown("### 📅 경제 이벤트 캘린더 · 지난 2일~향후 3일 (FMP)")

            # 이벤트별 행동 지침 자동 생성
            _ACTION_GUIDE = {
                "fomc": ("🏦 FOMC", "발표 전후 변동성 2배 구간. 신규 매수 자제, 기존 포지션 절반 이하 유지 권장."),
                "federal reserve": ("🏦 연준 발언", "예상치 못한 매파 발언 시 기술주 급락 가능. 포지션 축소 준비."),
                "cpi": ("📊 CPI", "예상치 상회 시 금리 인상 우려 → 성장주 매도 압력. 예상치 하회 시 랠리 가능."),
                "nonfarm": ("👷 NFP", "고용 호조 = 금리 인상 우려(기술주 악재). 고용 부진 = 경기 침체 우려."),
                "payroll": ("👷 NFP", "고용 호조 = 금리 인상 우려(기술주 악재). 고용 부진 = 경기 침체 우려."),
                "pce": ("💰 PCE", "연준 선호 인플레 지표. 예상 상회 시 금리 인하 지연 → 성장주 부담."),
                "ppi": ("🏭 PPI", "생산자물가. CPI 선행지표. 상승 시 향후 CPI 상승 압력 예고."),
                "gdp": ("📈 GDP", "예상 하회 시 경기 침체 우려. 예상 상회 시 과열 인플레 우려 공존."),
                "retail sales": ("🛒 소매판매", "소비 강도 지표. 급감 시 경기 둔화 신호."),
                "ism": ("🏗️ ISM PMI", "50 이하 = 경기 수축. 제조업 PMI 하회 시 산업재·소재 섹터 주의."),
                "initial claims": ("📋 실업수당", "급증 시 고용 악화 신호. 연속 급증이면 경기 침체 선행 지표."),
                "pmi": ("📉 PMI", "50 이하 수축 신호. 예상 하회 시 경기 우려 확대."),
            }

            def _get_action(event_name: str) -> tuple:
                name_lower = event_name.lower()
                for kw, guide in _ACTION_GUIDE.items():
                    if kw in name_lower:
                        return guide
                return ("", "")

            def _fmt_ev_val(v):
                return str(v) if v is not None and str(v).strip() not in ("", "None") else "—"

            def _ev_num(v):
                try:
                    return float(str(v).replace(",", "").replace("%", "").strip())
                except Exception:
                    return None

            def _surprise_text(actual, estimate):
                """발표된 이벤트의 '실제 vs 예상' 서프라이즈 한 줄."""
                a, e = _ev_num(actual), _ev_num(estimate)
                if a is None:
                    return ""
                if e is None:
                    return f"실제 **{actual}** (예상치 없음)"
                if a > e:
                    return f"실제 **{actual}** / 예상 {estimate} → 📈 예상 상회"
                if a < e:
                    return f"실제 **{actual}** / 예상 {estimate} → 📉 예상 하회"
                return f"실제 **{actual}** / 예상 {estimate} → ➖ 예상 부합"

            # 고임팩트 이벤트: 과거(발표완료 결과) / 향후(사전 행동 지침) 분리
            high_ev = [e for e in event_cal if e.get("impact","").lower() in ("high","3")]
            past_high = [e for e in high_ev if e.get("is_past")]
            upcoming_high = [e for e in high_ev if not e.get("is_past")]

            # ① 발표 완료된 고임팩트 — 결과(실제 vs 예상) 표시
            if past_high:
                for ev in past_high[-3:]:
                    ev_icon, _ = _get_action(ev.get("event",""))
                    surprise = _surprise_text(ev.get("actual"), ev.get("estimate"))
                    body = (
                        f"**{ev_icon or '📅'} {ev.get('date','')} · {ev.get('event','')}** (발표 완료)  \n"
                        f"{surprise}" if surprise
                        else f"**{ev_icon or '📅'} {ev.get('date','')} · {ev.get('event','')}** (발표 완료)"
                    )
                    st.info(body)

            # ② 향후 고임팩트 — 사전 행동 지침 표시
            if upcoming_high:
                for ev in upcoming_high[:3]:
                    ev_icon, ev_action = _get_action(ev.get("event",""))
                    if ev_action:
                        est = ev.get("estimate")
                        prior = ev.get("prior")
                        context = ""
                        if est is not None and str(est).strip() not in ("", "None"):
                            context += f" 예상: **{est}**"
                        if prior is not None and str(prior).strip() not in ("", "None"):
                            context += f" / 이전: **{prior}**"
                        st.warning(
                            f"**{ev_icon} {ev.get('date','')} · {ev.get('event','')}**{context}  \n"
                            f"→ {ev_action}"
                        )

            # 전체 캘린더 테이블
            with st.expander("📋 전체 이벤트 목록 보기", expanded=False):
                ev_cols_header = st.columns([1.2, 3.3, 1.2, 1.0, 1.2, 1.2, 1.2])
                for hdr, txt in zip(ev_cols_header, ["날짜", "이벤트", "임팩트", "상태", "실제", "예상", "이전"]):
                    hdr.markdown(f"**{txt}**")
                for ev in event_cal[:20]:
                    impact = ev.get("impact", "")
                    is_past = bool(ev.get("is_past"))
                    impact_color = (
                        "#dc2626" if impact.lower() in ("high", "3")
                        else "#f59e0b" if impact.lower() in ("medium", "2")
                        else "#64748b"
                    )
                    impact_emoji = (
                        "🔴 High" if impact.lower() in ("high", "3")
                        else "🟡 Medium" if impact.lower() in ("medium", "2")
                        else "⚪ Low"
                    )
                    status_txt = "✅ 발표완료" if is_past else "⏳ 예정"
                    # 과거 행은 흐리게
                    date_md = (
                        f"<span style='color:#64748b'>`{ev.get('date','')}`</span>" if is_past
                        else f"`{ev.get('date','')}`"
                    )
                    name_md = (
                        f"<span style='color:#94a3b8'>{ev.get('event','')}</span>" if is_past
                        else ev.get("event", "")
                    )
                    ev_cols = st.columns([1.2, 3.3, 1.2, 1.0, 1.2, 1.2, 1.2])
                    ev_cols[0].markdown(date_md, unsafe_allow_html=True)
                    ev_cols[1].markdown(name_md, unsafe_allow_html=True)
                    ev_cols[2].markdown(f"<span style='color:{impact_color};font-weight:700'>{impact_emoji}</span>", unsafe_allow_html=True)
                    ev_cols[3].markdown(status_txt)
                    ev_cols[4].markdown(_fmt_ev_val(ev.get("actual")))
                    ev_cols[5].markdown(_fmt_ev_val(ev.get("estimate")))
                    ev_cols[6].markdown(_fmt_ev_val(ev.get("prior")))
            st.caption("📌 FMP Economic Calendar 기준. 발표 완료 이벤트는 2일간 결과(실제값) 확인용으로 표시됩니다.")

        with st.expander("🔍 신호 상세 데이터", expanded=False):
            for key, data in drg.get("details", {}).items():
                st.markdown(f"**{key}**")
                if isinstance(data, dict):
                    for k2, v2 in data.items():
                        if isinstance(v2, dict):
                            st.markdown("  - `" + k2 + "`: " + " / ".join(f"{kk}={vv}" for kk, vv in v2.items()))
                        else:
                            st.markdown(f"  - {k2}: **{v2}**")

        news_items = drg.get("news_items", [])
        if news_items:
            st.divider()
            st.markdown("### 📰 관련 최신 뉴스")
            for n in news_items[:8]:
                time_str = ""
                if n.get("time"):
                    try:
                        import datetime as _dt
                        dt = _dt.datetime.fromtimestamp(n["time"], tz=_dt.timezone.utc).astimezone(_MARKET_ET_TZ)
                        time_str = dt.strftime("%m/%d %H:%M")
                    except Exception:
                        pass
                link = n.get("link", "")
                title = n.get("title", "")
                publisher = n.get("publisher", "")
                ticker = n.get("ticker", "")
                if link:
                    st.markdown(f"**{ticker}** | [{title}]({link}) — _{publisher}_ {time_str}")
                else:
                    st.markdown(f"**{ticker}** | {title} — _{publisher}_ {time_str}")

        st.divider()
        st.markdown("### 🤖 AI 내일 시장 예측")
        st.caption("선행 신호 6가지(선물·달러·금 포함) + RSS 최신 뉴스(최대 30개) + 거시지표를 종합해 분석합니다. 장 열리기 전(8AM ET / 한국 밤 10시) 실행 권장.")

        if st.button("🤖 AI 내일 시장 예측 실행", key="drg_ai_btn", type="primary", use_container_width=True):
            with st.spinner("최신 데이터 수집 중..."):
                # 1) DRG 캐시 강제 갱신 → 신호/뉴스 최신화
                compute_daily_risk_gauge.clear()
                fresh_drg = compute_daily_risk_gauge(sector_filter=sector_choice)
                fresh_sig = fresh_drg.get("signals", sig)
                fresh_warnings = fresh_drg.get("warnings", [])

                # 2) FMP 멀티레이어 뉴스 수집 (Stock News·Press Releases·Articles·General, 최대 30개)
                try:
                    fmp_news_list, _, _ = fetch_global_market_news()
                    rss_news_text = "\n".join([
                        f"- [{item.get('source','?')}] {item.get('title','')}"
                        + (f" | Tickers: {item.get('tickers','')}" if item.get('tickers') else "")
                        + (f" | {item.get('summary','')[:80]}" if item.get('summary') else "")
                        for item in fmp_news_list[:30]
                    ]) if fmp_news_list else "뉴스 없음"
                    rss_news_count = len(fmp_news_list)
                except Exception:
                    rss_news_text = "\n".join([
                        f"- [{n['ticker']}] {n['title']} ({n['publisher']})"
                        for n in fresh_drg.get("news_items", [])[:10]
                    ]) or "최신 뉴스 없음"
                    rss_news_count = 0

                # 3) 거시경제 지표
                try:
                    _macro_ctx = _build_macro_context_for_summary()
                    macro_summary = "\n".join([
                        f"- {k}: {v.get('value','N/A')} ({v.get('status','N/A')})"
                        for k, v in list(_macro_ctx.items())[:8]
                    ]) if _macro_ctx else "데이터 없음"
                except Exception:
                    macro_summary = "데이터 없음"

            # 4) 이벤트 캘린더 요약
            fresh_events = fresh_drg.get("event_calendar", [])
            if fresh_events:
                # 예측은 '향후' 이벤트만 사용. 발표 완료분은 결과(실제값)로 별도 표기.
                up_ev = [e for e in fresh_events if not e.get("is_past")]
                done_ev = [e for e in fresh_events if e.get("is_past") and e.get("actual") is not None]
                lines = []
                for e in up_ev[:6]:
                    lines.append(
                        f"- (예정) {e['date']} [{e['impact']}] {e['event']}"
                        + (f" (예상: {e['estimate']})" if e.get("estimate") is not None else "")
                    )
                for e in done_ev[-3:]:
                    lines.append(
                        f"- (발표완료) {e['date']} [{e['impact']}] {e['event']}"
                        f" (실제: {e['actual']}"
                        + (f" / 예상: {e['estimate']}" if e.get("estimate") is not None else "")
                        + ")"
                    )
                event_summary = "\n".join(lines) if lines else "이벤트 없음 또는 조회 불가"
            else:
                event_summary = "이벤트 없음 또는 조회 불가"

            signal_summary = "\n".join([
                f"- {label}: {'정상' if fresh_sig.get(key, {}).get('ok', True) else '경고'} | {fresh_sig.get(key, {}).get('value', 'N/A')}"
                + (f" ({fresh_sig.get(key, {}).get('trend','')})" if fresh_sig.get(key, {}).get('trend','') else "")
                for key, label, _ in signal_defs
            ])
            warning_text = "\n".join(fresh_warnings) if fresh_warnings else "없음"
            now_kst = datetime.now(_MARKET_ET_TZ)
            now_et_for_session = datetime.now(_MARKET_ET_TZ)
            # 장 세션 판단은 ET 기준 (9:30~16:00 ET)
            et_hour = now_et_for_session.hour
            et_minute = now_et_for_session.minute
            market_session = (
                "장 전" if (et_hour < 9 or (et_hour == 9 and et_minute < 30))
                else ("장 중" if et_hour < 16 else "장 후")
            )

            # 선물·리스크오프 신호 별도 강조
            _futures_sig = fresh_sig.get("futures", {})
            _riskoff_sig = fresh_sig.get("riskoff", {})
            _futures_line = f"선물 프리마켓: {_futures_sig.get('value','N/A')} ({_futures_sig.get('trend','')})"
            _riskoff_line = f"달러·금·국채: {_riskoff_sig.get('value','N/A')}"

            drg_prompt = (
                "당신은 월가 수석 퀀트 전략가입니다. "
                "아래 실시간 데이터를 바탕으로 내일 미국 주식시장을 예측하세요.\n\n"
                f"[현재 시각] {now_kst.strftime('%Y-%m-%d %H:%M')} ET ({market_session})\n"
                f"[분석 섹터] {sector_choice}\n\n"
                f"[선행 신호 6가지]\n{signal_summary}\n\n"
                f"[선물·안전자산 핵심 데이터]\n"
                f"- {_futures_line}\n"
                f"- {_riskoff_line}\n\n"
                f"[감지된 경고]\n{warning_text}\n\n"
                f"[3일 이내 주요 경제 이벤트]\n{event_summary}\n\n"
                f"[거시경제 지표]\n{macro_summary}\n\n"
                f"[오늘의 주요 뉴스 ({rss_news_count}개 소스)]\n{rss_news_text}\n\n"
                "---\n"
                "아래 4개 항목을 각각 작성하세요. "
                "반드시 위 수치(선물 %, VIX 값, 이벤트명/예상치, 뉴스 종목명)를 직접 인용해 근거를 만드세요. "
                "일반론·빈말 금지. 선물과 이벤트 데이터를 반드시 활용하세요.\n\n"
                "**[방향 판단 규칙 — 중요]** 다음날 단기 시장 방향은 본질적으로 예측이 어렵습니다. "
                "선물·VIX·이벤트가 **한 방향을 강하게(예: 선물 ±0.7% 이상, 또는 고임팩트 이벤트와 뉴스가 같은 방향) 가리킬 때만** "
                "'상승 우세' 또는 '하락 우세'로 판단하세요. 신호가 혼재되거나 약하거나 보합권이면 **'중립'을 선택**하세요. "
                "억지로 방향을 단정하지 말고, 확신이 없으면 '중립'이 정답입니다.\n\n"
                "## 내일 시장 방향 판단: [상승 우세 / 중립 / 하락 우세]\n\n"
                "**📊 핵심 근거** (선물 방향·VIX·이벤트 수치 직접 인용, 2~3문장):\n\n"
                "**📰 뉴스 영향** (오버나잇 뉴스 중 내일 시장에 영향줄 종목/이슈, 2문장):\n\n"
                "**⚠️ 내일 주목할 리스크** (구체적 수치/종목/이벤트 기반, 2가지):\n"
                "1. \n"
                "2. \n\n"
                "**🎯 실전 대응** (지금 상황에 맞는 구체적 행동, 보유/매수/현금 각 1문장):\n"
                "- 보유 중: \n"
                "- 매수 타이밍 보는 중: \n"
                "- 현금 대기 중: \n\n"
                "*본 분석은 AI 참고용이며 투자 권유가 아닙니다.*"
            )
            with st.spinner("Gemini AI 분석 중... (약 15~20초)"):
                _drg_model = _GenAIModel(
                    "gemini-2.5-flash",
                    generation_config={"temperature": 0.7, "max_output_tokens": 8192, "thinking_budget": 0}
                )
                _drg_response = _drg_model.generate_content(drg_prompt)
                _drg_text = _gemini_response_text_utf8_safe(_drg_response)
            if _drg_text:
                # 방향 추출
                _pred_dir = "중립"
                if "상승 우세" in _drg_text:
                    _pred_dir = "상승 우세"
                elif "하락 우세" in _drg_text:
                    _pred_dir = "하락 우세"

                # 벤치마크 ETF 결정
                _bench_etf = _SECTOR_BENCHMARK_ETF.get(sector_choice, "SPY")

                # 현재 벤치마크 ETF 종가 가져오기
                try:
                    _spy_df = _fmp_price_history(_bench_etf, limit=3)
                    _spy_close = float(_spy_df["Close"].iloc[-1]) if _spy_df is not None and not _spy_df.empty else np.nan
                except Exception:
                    _spy_close = np.nan

                # DRG_Predictions 시트에 저장 (ET 기준 — 자동화 스크립트와 동일)
                _pred_date_str = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
                _puid_drg = str(st.session_state.get("user_id") or "").strip()
                save_drg_prediction(
                    _puid_drg, _pred_date_str, _pred_dir,
                    sector_choice, _bench_etf, _spy_close, _drg_text
                )

                st.session_state["_drg_ai_result"] = _drg_text
                st.session_state["_drg_ai_time"] = datetime.now(_MARKET_ET_TZ).strftime("%m/%d %H:%M")
                st.session_state["_drg_ai_news_count"] = rss_news_count
                st.session_state["_drg_pred_dir"] = _pred_dir
                st.session_state["_drg_bench_etf"] = _bench_etf
                st.rerun()

        if st.session_state.get("_drg_ai_result"):
            _news_cnt = st.session_state.get("_drg_ai_news_count", 0)
            _src_str = f"RSS {_news_cnt}개 뉴스 반영" if _news_cnt > 0 else "FMP 뉴스 반영"
            st.info(f"🕐 분석 시각: {st.session_state.get('_drg_ai_time', '')} ET · {_src_str}")
            _txt = st.session_state["_drg_ai_result"]
            if "하락 우세" in _txt:
                st.error(_txt)
            elif "상승 우세" in _txt:
                st.success(_txt)
            else:
                st.warning(_txt)

        # ═══════════════════════════════════════════════════════════════════
        # AI 예측 히스토리 & 적중률
        # ═══════════════════════════════════════════════════════════════════
        st.divider()
        st.markdown("## 📊 AI 예측 히스토리 & 적중률")
        st.caption("과거 AI 예측과 실제 시장 결과를 비교합니다. 예측 다음날 이후 '결과 검증' 버튼으로 실제 결과를 확인하세요.")

        _puid_hist = str(st.session_state.get("user_id") or "").strip()

        if st.button("🔄 히스토리 새로고침", key="drg_hist_refresh_btn"):
            st.rerun()

        pred_hist_df = load_drg_predictions(_puid_hist)

        if pred_hist_df.empty:
            _ws_diag, _err_diag = open_drg_predictions_worksheet()
            if _err_diag:
                st.error(f"시트 연결 실패: {_err_diag}")
            else:
                try:
                    _rows_diag = _ws_diag.get_all_values()
                    _total = len(_rows_diag) - 1 if _rows_diag and len(_rows_diag) > 1 else 0
                    if _total > 0:
                        _first_uid = _rows_diag[1][0] if _rows_diag[1] else "?"
                        st.warning(f"시트에 {_total}개 행이 있지만 현재 user_id(`{_puid_hist}`)와 일치하는 행이 없습니다. 시트의 첫 데이터 user_id: `{_first_uid}`")
                    else:
                        st.info("아직 AI 예측 기록이 없습니다. 'AI 내일 시장 예측 실행' 버튼을 누르면 자동으로 기록됩니다.")
                except Exception as _de:
                    st.error(f"진단 오류: {_de}")
        else:
            verified = pred_hist_df[pred_hist_df["is_correct"].astype(str).str.strip() != ""]
            total_v = len(verified)
            correct_v = (verified["is_correct"] == "✅ 적중").sum() if total_v else 0
            hit_rate = (correct_v / total_v * 100) if total_v else np.nan
            hm1, hm2, hm3, hm4 = st.columns(4)
            hm1.metric("총 예측 횟수", len(pred_hist_df))
            hm2.metric("검증 완료", total_v)
            hm3.metric("적중 횟수", correct_v)
            hm4.metric("적중률", f"{hit_rate:.0f}%" if pd.notna(hit_rate) else "N/A")
            st.divider()

            unverified = pred_hist_df[pred_hist_df["is_correct"].astype(str).str.strip() == ""].copy()
            if not unverified.empty:
                st.markdown("#### 🔍 결과 검증 대기 중")
                for _uidx, (_, urow) in enumerate(unverified.iterrows()):
                    uc1, uc2, uc3 = st.columns([2, 2, 1])
                    uc1.write(f"📅 **{urow.get('pred_date','')}** | {urow.get('direction','')}")
                    uc2.write(f"벤치마크: {urow.get('benchmark_etf','SPY')} | 섹터: {urow.get('sector_filter','전체')}")
                    with uc3:
                        if st.button("결과 검증", key=f"verify_{urow.get('pred_date','')}_{_uidx}"):
                            with st.spinner("실제 결과 조회 중..."):
                                _ad, _ar, _ic = verify_drg_prediction(urow)
                            if _ad:
                                with st.spinner("AI 리뷰 작성 중..."):
                                    try:
                                        _rp = (
                                            f"예측방향: {urow.get('direction','')} / 실제: {_ad} ({_ar:+.2f}%) / {_ic}\n"
                                            f"예측 전문(앞 500자): {str(urow.get('full_text',''))[:500]}\n"
                                            "3~4문장 한국어 리뷰: 맞았다면 어떤 근거가 적중했는지, 틀렸다면 무엇을 놓쳤는지, 다음 예측 시 참고할 인사이트."
                                        )
                                        _rm = _GenAIModel("gemini-2.5-flash",
                                            generation_config={"temperature": 0.3, "max_output_tokens": 1024, "thinking_budget": 0})
                                        _rv = _gemini_response_text_utf8_safe(_rm.generate_content(_rp)) or ""
                                    except Exception:
                                        _rv = ""
                                ok_u, err_u = update_drg_prediction_result(
                                    _puid_hist, str(urow.get("pred_date", "")),
                                    _ad, _ar, _ic, _rv)
                                if ok_u:
                                    st.success(f"{_ic} | 실제: {_ad} ({_ar:+.2f}%)")
                                    st.rerun()
                                else:
                                    st.error(f"저장 실패: {err_u}")
                            else:
                                st.warning("예측일 당일 장이 아직 마감되지 않았거나 휴장일입니다. 장 마감(오후 4시 ET) 후 다시 시도해주세요.")
                st.divider()

            st.markdown("#### 📋 전체 예측 기록")
            bench_opts = ["전체 보기"] + [f"{v} ({k})" for k, v in _SECTOR_BENCHMARK_ETF.items()]
            _hist_bench = st.selectbox("벤치마크 ETF 필터", options=bench_opts, key="drg_hist_bench_filter")
            show_hist = pred_hist_df.copy()
            if _hist_bench != "전체 보기":
                _fetf = _hist_bench.split(" ")[0]
                show_hist = show_hist[show_hist["benchmark_etf"] == _fetf]
            show_hist = show_hist.sort_values("pred_date", ascending=False).reset_index(drop=True)
            show_hist["actual_return_pct"] = pd.to_numeric(show_hist["actual_return_pct"], errors="coerce")

            def _cd(v):
                if "상승" in str(v): return "color:#16a34a;font-weight:600;"
                if "하락" in str(v): return "color:#dc2626;font-weight:600;"
                return ""
            def _cc(v):
                if "✅" in str(v): return "color:#16a34a;font-weight:700;"
                if "❌" in str(v): return "color:#dc2626;font-weight:700;"
                return "color:#94a3b8;"

            st.dataframe(
                show_hist[["pred_date","direction","sector_filter","benchmark_etf",
                            "actual_direction","actual_return_pct","is_correct","review_comment"]]
                .rename(columns={"pred_date":"예측일","direction":"예측방향","sector_filter":"섹터",
                                  "benchmark_etf":"ETF","actual_direction":"실제방향",
                                  "actual_return_pct":"실제수익률(%)","is_correct":"적중여부","review_comment":"AI리뷰"})
                .style
                .format({"실제수익률(%)": "{:+.2f}%"}, na_rep="대기중")
                .map(_cd, subset=["예측방향","실제방향"])
                .map(_cc, subset=["적중여부"]),
                use_container_width=True, hide_index=True)

            if not show_hist.empty:
                st.markdown("#### 📄 예측 전문 보기")
                _sel = st.selectbox("날짜 선택", options=show_hist["pred_date"].tolist(), key="drg_fulltext_sel")
                _sel_row = show_hist[show_hist["pred_date"] == _sel]
                if not _sel_row.empty:
                    _r = _sel_row.iloc[0]
                    _ft = str(_r.get("full_text", ""))
                    _corr = str(_r.get("is_correct", ""))
                    if "✅" in _corr: st.success(_ft)
                    elif "❌" in _corr: st.error(_ft)
                    else: st.info(_ft)
                    if str(_r.get("review_comment", "")).strip():
                        st.markdown("**🤖 AI 리뷰:**")
                        st.markdown(str(_r.get("review_comment", "")))

        # ── 🎯 오늘 투자해도 될까? — 실시간 투자 판단 브리핑 ────────────────
        st.divider()
        st.markdown("### 🎯 오늘 투자해도 될까?")
        st.caption(
            "아침 시장 예측 + 현재 거시경제 지표 + DRG 선행 신호를 종합해 "
            "Gemini가 지금 이 순간 투자 판단을 내립니다."
        )

        _itb_session_key = "invest_today_briefing"
        _itb_ts_key = "invest_today_briefing_ts"
        _itb_uid = str(st.session_state.get("user_id") or "").strip()

        # ── 입력 데이터 수집 ────────────────────────────────────────────────
        # 1) 오늘 아침 예측
        _itb_today_str = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
        _itb_pred_df = load_drg_predictions(_itb_uid)
        _itb_today_pred = None
        if not _itb_pred_df.empty:
            _itb_match = _itb_pred_df[
                _itb_pred_df["pred_date"].astype(str).str[:10] == _itb_today_str
            ]
            if not _itb_match.empty:
                _itb_today_pred = _itb_match.iloc[-1]

        # 2) DRG 현재 점수/신호 (이미 위에서 계산된 drg 재사용)
        # drg 변수는 이 페이지 상단에서 이미 compute_daily_risk_gauge()로 계산됨

        # ── 상태 표시 ────────────────────────────────────────────────────────
        _itb_info_c1, _itb_info_c2, _itb_info_c3 = st.columns(3)
        with _itb_info_c1:
            if _itb_today_pred is not None:
                st.success(f"✅ 오늘 예측 있음\n\n**{_itb_today_pred.get('direction', '')}**")
            else:
                st.warning("⚠️ 오늘 아침 예측 없음\n\n(예측 없이도 분석 가능)")
        with _itb_info_c2:
            st.info(
                f"**DRG:** {drg.get('risk_level', 'N/A')}\n\n"
                f"Risk Score: {drg.get('risk_score', 'N/A')}/10"
            )
        with _itb_info_c3:
            if st.session_state.get(_itb_ts_key):
                st.info(f"⏱️ 마지막 분석\n\n{st.session_state[_itb_ts_key]}")
            else:
                st.info("📊 거시 지표 자동 로드됨\n\n버튼을 눌러 분석 시작")

        # ── 실행 버튼 ────────────────────────────────────────────────────────
        _itb_btn_col, _itb_reset_col = st.columns([3, 1])
        with _itb_btn_col:
            _run_itb = st.button(
                "🎯 지금 투자해도 될지 분석",
                key="invest_today_briefing_btn",
                use_container_width=True,
                type="primary",
            )
        with _itb_reset_col:
            if st.button("🗑️ 초기화", key="invest_today_briefing_reset", use_container_width=True):
                st.session_state.pop(_itb_session_key, None)
                st.session_state.pop(_itb_ts_key, None)
                st.rerun()

        if _run_itb:
            # ── 거시 지표 로드 ────────────────────────────────────────────
            with st.spinner("거시경제 지표 로드 중..."):
                _itb_macro = get_macro_dashboard_with_validation()

            # ── 프롬프트 구성 ─────────────────────────────────────────────
            # 1) 아침 예측 섹션
            if _itb_today_pred is not None:
                _pred_section = (
                    f"[오늘 아침 시장 예측 — {_itb_today_pred.get('pred_date', '')}]\n"
                    f"예측 방향: {_itb_today_pred.get('direction', '')}\n"
                    f"섹터 필터: {_itb_today_pred.get('sector_filter', '전체')}\n"
                    f"벤치마크: {_itb_today_pred.get('benchmark_etf', 'SPY')}\n"
                    f"예측 전문 (앞 800자):\n{str(_itb_today_pred.get('full_text', ''))[:800]}\n"
                )
            else:
                _pred_section = "[오늘 아침 시장 예측]\n예측 기록 없음 (아직 실행 전이거나 휴장일)\n"

            # 2) DRG 신호 섹션
            _sig = drg.get("signals", {})
            _sig_lines = []
            _sig_label_map = {
                "vix": "VIX 방향",
                "credit": "신용 스프레드",
                "leaders": "대장주 모멘텀",
                "volume": "거래량 패턴",
                "vix_vxn": "VIX/VXN 비율",
                "event_risk": "이벤트 리스크",
                "futures": "선물 방향",
                "riskoff": "리스크오프 신호",
            }
            for _sk, _sl in _sig_label_map.items():
                _sv = _sig.get(_sk)
                if _sv:
                    _ok_str = "✅ 정상" if _sv.get("ok") else "❌ 경고"
                    _sig_lines.append(f"  - {_sl}: {_ok_str} | {_sv.get('value', 'N/A')}")
            _drg_section = (
                f"[DRG (Daily Risk Gauge) 현재 상태]\n"
                f"종합 Risk Score: {drg.get('risk_score', 'N/A')}/10 — {drg.get('risk_level', '')}\n"
                f"요약: {drg.get('risk_msg', '')}\n"
                f"선행 신호 상세:\n" + "\n".join(_sig_lines) + "\n"
            )

            # 3) 거시 지표 섹션
            _macro_lines = []
            for _mr in (_itb_macro.get("rows") or [])[:12]:
                _macro_lines.append(
                    f"  - {_mr.get('지표', '')}: {_mr.get('현재값', 'N/A')} → {_mr.get('판정', '')} | {_mr.get('판독 요약', '')[:60]}"
                )
            _macro_score = _itb_macro.get("macro_score", "N/A")
            _macro_grade = _itb_macro.get("macro_grade", "N/A")
            _fg_rating = _itb_macro.get("fg_rating", "N/A")
            _macro_section = (
                f"[거시경제 지표 현황]\n"
                f"종합 Macro Score: {_macro_score}/100 — {_macro_grade}\n"
                f"Fear & Greed: {_fg_rating}\n"
                f"주요 지표:\n" + "\n".join(_macro_lines) + "\n"
            )

            # 4) 최종 프롬프트
            _itb_prompt = f"""당신은 월가 퀀트 스트래티지스트입니다.
아래 세 가지 데이터를 종합해서 **지금 이 순간** 투자 판단을 내려주세요.

{_pred_section}
{_drg_section}
{_macro_section}

---
다음 형식으로 한국어로 답변하세요:

## 📊 신호 일치도 분석
아침 예측 방향과 현재 DRG·거시 지표가 일치하는지 불일치하는지 2~3문장으로 설명.

## 🎯 투자 판단 (세 가지)
- 🟢 **신규 진입**: [가능 / 자제 / 불가] — 이유 1문장
- 🟡 **기존 포지션 유지**: [유지 / 일부 축소 / 전량 축소] — 이유 1문장
- 🔴 **헷지·현금 비중**: [현수준 유지 / 확대 권장 / 긴급 확대] — 이유 1문장

## 💡 확신도
[0~100 숫자]% — 이유 1문장 (신호들이 일관되면 높음, 혼조세면 낮음)

## ⚠️ 오늘 가장 주목할 리스크 1가지
한 문장으로.

## ✅ 오늘 가장 긍정적인 신호 1가지
한 문장으로.
"""

            with st.spinner("Gemini 투자 판단 분석 중... (10~20초)"):
                try:
                    _itb_model = _GenAIModel(
                        "gemini-2.5-flash",
                        generation_config={"temperature": 0.2, "max_output_tokens": 4096, "thinking_budget": 0},
                    )
                    _itb_resp = _itb_model.generate_content(_itb_prompt)
                    _itb_text = (_gemini_response_text_utf8_safe(_itb_resp) or "").strip()
                    if _itb_text:
                        _now_et = datetime.now(_MARKET_ET_TZ)
                        _ts_str = _now_et.strftime("%m/%d %H:%M ET")
                        # 장 마감까지 남은 시간
                        _market_close = _now_et.replace(hour=16, minute=0, second=0, microsecond=0)
                        _mins_left = int((_market_close - _now_et).total_seconds() / 60)
                        _validity = (
                            f"장 마감까지 약 {_mins_left}분" if 0 < _mins_left < 480
                            else ("장 마감 후" if _mins_left <= 0 else "프리마켓")
                        )
                        st.session_state[_itb_session_key] = {
                            "text": _itb_text,
                            "timestamp": _ts_str,
                            "validity": _validity,
                            "pred_direction": _itb_today_pred.get("direction", "없음") if _itb_today_pred is not None else "없음",
                            "drg_score": drg.get("risk_score", "N/A"),
                            "macro_score": _macro_score,
                        }
                        st.session_state[_itb_ts_key] = _ts_str
                    else:
                        st.warning("분석 결과를 받지 못했습니다. 다시 시도해주세요.")
                except Exception as _itb_err:
                    st.error(f"Gemini 분석 실패: {_itb_err}")

        # ── 저장된 결과 표시 ────────────────────────────────────────────────
        _itb_saved = st.session_state.get(_itb_session_key)
        if _itb_saved and _itb_saved.get("text"):
            _itb_meta_c1, _itb_meta_c2, _itb_meta_c3, _itb_meta_c4 = st.columns(4)
            _itb_meta_c1.metric("분석 시각", _itb_saved.get("timestamp", "N/A"))
            _itb_meta_c2.metric("유효 시간", _itb_saved.get("validity", "N/A"))
            _itb_meta_c3.metric("DRG 점수", f"{_itb_saved.get('drg_score', 'N/A')}/10")
            _itb_meta_c4.metric("Macro Score", f"{_itb_saved.get('macro_score', 'N/A')}/100")
            st.markdown("---")
            st.markdown(_itb_saved["text"])
            st.caption(
                "⚠️ 이 분석은 투자 참고용입니다. 최종 투자 결정은 본인의 판단으로 하세요. "
                "새로운 뉴스·이벤트 발생 시 '🗑️ 초기화' 후 재분석을 권장합니다."
            )

    elif main_nav == _MAIN_NAV_OPTIONS[1]:
        render_sync_button("sync_tab_macro", [cached_analyze_us_macro_dashboard.clear], "불러온 지표는 세션 동안 캐시됩니다.")
        st.subheader(f"{_MAIN_NAV_OPTIONS[1]} · 미국 거시경제 대시보드")
        st.caption("FRED API + FMP Treasury/Economics API 기준. 금리차는 FMP 실시간, 월간 지표는 FRED. 판단은 참고용 휴리스틱입니다.")

        try:
            with st.spinner("매크로 지표를 불러오는 중..."):
                macro_pack = get_macro_dashboard_with_validation()

            if macro_pack.get("na_total", 0) >= 4:
                st.warning("일부 거시경제 데이터를 불러오지 못했습니다. FRED 또는 FMP API 상태를 확인해주세요.")

            # ── 1. Macro Score 게이지 ──────────────────────────────────────
            macro_score = macro_pack.get("macro_score", 50.0)
            macro_grade = macro_pack.get("macro_grade", "N/A")
            macro_desc = macro_pack.get("macro_desc", "")
            fg_score = macro_pack.get("fg_score", np.nan)
            fg_rating = macro_pack.get("fg_rating", "N/A")
            fed_rate = macro_pack.get("fed_rate", np.nan)

            st.markdown("### 🎯 종합 Macro Score")
            score_col1, score_col2, score_col3, score_col4 = st.columns(4)
            with score_col1:
                st.metric("Macro Score", f"{macro_score:.0f} / 100", delta=macro_grade)
            with score_col2:
                fg_str = f"{fg_score:.0f} ({fg_rating})" if pd.notna(fg_score) else "N/A"
                st.metric("Fear & Greed", fg_str)
            with score_col3:
                st.metric("Fed Funds Rate", f"{fed_rate:.2f}%" if pd.notna(fed_rate) else "N/A")
            with score_col4:
                bad_n = macro_pack["bad_total"]
                total_n = len(macro_pack["rows"])
                st.metric("경고 지표", f"{bad_n} / {total_n}")

            # Macro Score 프로그레스 바
            score_pct = int(macro_score)
            bar_color = "#16a34a" if macro_score >= 75 else "#f59e0b" if macro_score >= 50 else "#f97316" if macro_score >= 25 else "#dc2626"
            st.markdown(
                f"""
                <div style="background:#1e293b;border-radius:8px;padding:4px;margin:4px 0 8px 0;">
                  <div style="background:{bar_color};width:{score_pct}%;height:18px;border-radius:6px;
                              display:flex;align-items:center;justify-content:center;
                              color:white;font-size:12px;font-weight:700;">
                    {macro_grade}
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.caption(macro_desc)
            macro_traffic_light(macro_pack["bad_total"], total_indicators=len(macro_pack["rows"]))

            st.divider()

            # ── 2. 지표별 히스토리 차트 ───────────────────────────────────
            st.markdown("### 📈 주요 지표 트렌드 (2년 히스토리)")
            with st.spinner("히스토리 데이터 로딩 중..."):
                hist_data = fetch_macro_history_series()

            chart_tab1, chart_tab2, chart_tab3, chart_tab4, chart_tab5, chart_tab6, chart_tab7 = st.tabs(
                ["VIX", "CPI YoY", "실업률", "Fed Rate", "DXY", "GDP", "소매판매"]
            )
            with chart_tab1:
                vix_hist = macro_pack.get("vix_hist")
                if vix_hist is not None and not vix_hist.empty and "Close" in vix_hist.columns:
                    vix_chart = pd.DataFrame({"VIX": pd.to_numeric(vix_hist["Close"], errors="coerce")}).dropna()
                    st.line_chart(vix_chart, use_container_width=True)
                    st.caption("💡 20 돌파 여부가 핵심. 바닥에서 고개를 들면 조정 신호.")
                else:
                    st.warning("VIX 데이터를 불러오지 못했습니다.")

            with chart_tab2:
                if "cpi" in hist_data and not hist_data["cpi"].empty:
                    cpi_df = pd.DataFrame({"CPI YoY(%)": hist_data["cpi"]})
                    st.line_chart(cpi_df, use_container_width=True)
                    st.caption("💡 연준 목표 2%. 3% 이상이면 고금리 유지 압력.")
                else:
                    st.warning("CPI 데이터를 불러오지 못했습니다.")

            with chart_tab3:
                if "unrate" in hist_data and not hist_data["unrate"].empty:
                    un_df = pd.DataFrame({"실업률(%)": hist_data["unrate"]})
                    st.line_chart(un_df, use_container_width=True)
                    st.caption("💡 우상향 전환 시 샴의 법칙 근접. 소비 둔화 선행 지표.")
                else:
                    st.warning("실업률 데이터를 불러오지 못했습니다.")

            with chart_tab4:
                if "fedfunds" in hist_data and not hist_data["fedfunds"].empty:
                    fed_df = pd.DataFrame({"Fed Rate(%)": hist_data["fedfunds"]})
                    st.line_chart(fed_df, use_container_width=True)
                    st.caption("💡 금리 인하 사이클 시작 여부가 성장주 밸류에이션의 핵심 트리거.")
                else:
                    st.warning("Fed Rate 데이터를 불러오지 못했습니다.")

            with chart_tab5:
                if "dxy" in hist_data and not hist_data["dxy"].empty:
                    dxy_df = pd.DataFrame({"DXY": hist_data["dxy"]})
                    st.line_chart(dxy_df, use_container_width=True)
                    st.caption("💡 달러 강세 시 다국적 빅테크 해외 실적 환차손 우려.")
                else:
                    st.warning("DXY 데이터를 불러오지 못했습니다.")

            with chart_tab6:
                try:
                    gdp_row = next((r for r in macro_pack.get("rows", []) if "GDP" in r.get("지표", "")), None)
                    if gdp_row and gdp_row.get("현재값", "N/A") != "N/A":
                        st.metric("GDP 성장률 QoQ (최신)", gdp_row["현재값"])
                        st.info(gdp_row["판독 요약"])
                        st.caption("💡 전분기 대비 성장률(%). 마이너스 2분기 연속 = 기술적 침체. 0.5% 미만이면 정체 신호.")
                    else:
                        st.warning("GDP 데이터를 불러오지 못했습니다. FMP API 키를 확인하세요.")
                except Exception:
                    st.warning("GDP 데이터 조회 중 오류가 발생했습니다.")

            with chart_tab7:
                try:
                    rs_row = next((r for r in macro_pack.get("rows", []) if "소매판매" in r.get("지표", "")), None)
                    if rs_row and rs_row.get("현재값", "N/A") != "N/A":
                        st.metric("소매판매 MoM (최신)", rs_row["현재값"])
                        st.info(rs_row["판독 요약"])
                        st.caption("💡 전월 대비 변화율(%). 마이너스 전환 시 소비 위축·경기 둔화 선행 신호.")
                    else:
                        st.warning("소매판매 데이터를 불러오지 못했습니다. FMP API 키를 확인하세요.")
                except Exception:
                    st.warning("소매판매 데이터 조회 중 오류가 발생했습니다.")

            st.divider()

            # ── 3. 전체 지표 카드 ─────────────────────────────────────────
            highlights = macro_pack["rows"]
            st.markdown("### 🗂️ 전체 지표 판독 카드")
            for row_idx in range(0, len(highlights), 4):
                card_cols = st.columns(4)
                for col_idx in range(4):
                    ix = row_idx + col_idx
                    if ix >= len(highlights):
                        break
                    row = highlights[ix]
                    with card_cols[col_idx]:
                        st.metric(label=row["지표"], value=row["판정"])
                        st.caption(row["현재값"])
                        st.info(row["판독 요약"])

            show_df = pd.DataFrame(highlights).drop(columns=["_status"], errors="ignore")
            with st.expander("📋 원본 표 (복사용)", expanded=False):
                st.dataframe(show_df, use_container_width=True, hide_index=True)

            with st.expander("📖 거시경제 지표 해석 가이드", expanded=False):
                st.markdown("""
| 지표 | 소스 | 의미 | 주식 시장 영향 |
|---|---|---|---|
| 장단기 금리차 | FMP 실시간 / FRED 폴백 | 경기 침체 예고 신호 | 역전 시 침체 우려, 좁아질수록 위험 |
| VIX | FRED | 투자자 공포·탐욕 수준 | 20 이하 안정, 35 이상 극단적 공포 |
| WTI 유가 | FRED | 인플레이션·비용 압력 | 급등 시 성장주 밸류에이션 타격 |
| 실업률 (샴) | FRED | 소비 체력·침체 진입 | 샴 발동 시 실물 위기 시작 |
| CPI YoY | FRED | 연준 금리 결정 방향 | 3% 이상 시 고금리 유지 (성장주 악재) |
| DXY | FRED | 글로벌 달러 위상 | 강세 시 다국적 기업 해외 실적 악화 |
| Fear & Greed | VIX 백분위 근사 | 시장 심리 종합 | 75+ 극단적 탐욕(매도 고려), 25- 극단적 공포(매수 고려) |
| Fed Funds Rate | FRED | 현재 기준금리 | 5% 이상 고금리 레짐, 인하 시 성장주 재평가 |
| GDP 성장률 QoQ | FMP Economics | 전분기 대비 실질 성장률 | 마이너스 2분기 연속 = 기술적 침체, 0.5% 미만 = 정체 경보 |
| 소매판매 MoM | FMP Economics | 전월 대비 소비 변화율 | 마이너스 전환 시 소비 위축·경기 둔화 선행 신호 |
| 주식 위험 프리미엄 ERP | FMP | 주식 vs 채권 매력도 | ERP 높을수록 주식 저평가, 낮을수록 채권 선호 국면 |
""")
                st.caption("📌 FMP Economics API 항목은 FMP_API_KEY 설정 시에만 조회됩니다.")

        except Exception as e:
            st.error("거시경제 데이터를 불러오거나 계산하는 중 오류가 발생했습니다.")
            st.exception(e)
    
    elif main_nav == _MAIN_NAV_OPTIONS[2]:
        hydrate_narrative_from_disk_once()
    
        nrow_1, nrow_2 = st.columns([1, 3])
        with nrow_1:
            if st.button("🔄 현재 페이지 데이터 동기화", key="sync_tab_narrative", use_container_width=True):
                st.session_state["_narratives_force_sheet_refresh"] = True
                tab_sync_refresh(
                    [
                        cached_fetch_global_market_news_pack.clear,
                        _narratives_sheet_all_values_cached.clear,
                    ],
                    rerun_after=True,
                )
        with nrow_2:
            st.caption("동기화 시 RSS·뉴스 캐시를 비우고, `Narratives` 시트 기록도 다시 불러옵니다.")
    
        header_col_1, header_col_2 = st.columns([3, 1])
        with header_col_1:
            st.subheader(f"{_MAIN_NAV_OPTIONS[2]} · AI 시장 내러티브 분석")
        with header_col_2:
            language_option = st.radio(
                "언어",
                ["🇰🇷 한국어", "🇺🇸 English"],
                horizontal=True,
                key="narrative_language_selector",
            )
        selected_language = "ko" if language_option == "🇰🇷 한국어" else "en"
    
        if st.button("🚀 AI 내러티브 엔진 가동 (실시간 뉴스 분석)"):
            try:
                with st.status("🚀 AI 내러티브 엔진 가동 프로세스 시작...", expanded=True) as status:
                    st.write("📥 1단계: FMP 멀티레이어 뉴스 파이프라인 수집 중... (Stock News · Press Releases · FMP Articles · General News)")
                    top_news, news_text, raw_news_count = cached_fetch_global_market_news_pack()
                    _source_log = st.session_state.get("_news_source_log", "")
                    if raw_news_count == 0:
                        st.write(":red[뉴스 수집 에러: 유효한 뉴스 데이터를 확보하지 못했습니다. (FMP API 키 확인 또는 RSS Fallback도 실패)]")
                        status.update(label="❌ 분석 중단 (에러 발생)", state="error", expanded=True)
                        st.error("❌ 실시간 뉴스 수집에 실패했습니다.")
                    else:
                        st.write(f"✔️ 수집 완료: 총 {raw_news_count}개 원본 뉴스 확보 ({_source_log})")
    
                        st.write("⚙️ 2단계: 뉴스 중복 제거(Deduplication) 및 가중치 기반 랭킹 산출 중...")
                        st.write(f"✔️ 중복 제거/랭킹 완료: Top {len(top_news)}개 핵심 뉴스 선별 (티커 태그 포함)")
                        st.session_state["latest_top_news"] = top_news
    
                        st.write("📊 3단계: ETF Universe 정량 스크리닝 (RS·모멘텀·거래량 분석) 중...")
                        quant_result = compute_quantitative_sector_leaders(top_n=30)
                        if quant_result.get("leaders"):
                            st.write(f"✔️ 정량 스크리닝 완료: RS 상위 {len(quant_result['leaders'])}개 종목 선별 | 강세 섹터: {', '.join(quant_result.get('top_sectors', []))}")
                        else:
                            st.write("⚠️ 정량 스크리닝 데이터 없음 — 뉴스 분석만으로 진행")

                        st.write("🧠 4단계: 뉴스(티커 태그 포함) + 정량 데이터를 Gemini 2.5 Flash 엔진으로 전송하여 내러티브 분석 중...")
                        narrative_data = generate_market_narrative(news_text, selected_language, quant_data=quant_result)
                        st.write("🔍 5단계: AI 응답 수신 완료. JSON 데이터 파싱 및 필터링 중...")
    
                        if narrative_data:
                            st.session_state["narrative_history"].append(narrative_data)
                            st.session_state["current_view"] = narrative_data
                            st.session_state["current_view_language"] = selected_language
                            st.session_state["current_view_source"] = "market_narrative"
                            append_narrative_history_record(narrative_data, selected_language)
                            st.session_state["narrative_history_disk_records"] = load_narrative_history_records()
                            status.update(label="✅ 분석 완료! (성공)", state="complete", expanded=False)
                            st.success(
                                "분석 결과를 Google 스프레드시트 `Quant_DB`의 **`Narratives`** 탭에 한 행 추가했습니다. "
                                f"(수집 {raw_news_count}건 / LLM 투입 Top {len(top_news)} | {_source_log})"
                            )
                        else:
                            st.write(":red[AI 파싱 에러 발생: Gemini 응답에서 유효한 JSON을 추출하지 못했습니다.]")
                            raw_text = st.session_state.get("last_gemini_raw_text", "")
                            if raw_text:
                                st.error("❌ JSON 파싱 에러가 발생했습니다.")
                                st.error(f"🤖 Gemini 실제 답변 원문:\n\n{raw_text}")
                            status.update(label="❌ 분석 중단 (에러 발생)", state="error", expanded=True)
                            st.error("Gemini 응답 파싱에 실패했습니다. 다시 시도해주세요.")
            except Exception as e:
                tb = traceback.format_exc()
                st.error("🚨 내러티브 엔진 가동 중 치명적 에러 발생!")
                st.error(f"요약: {type(e).__name__}: {e}")
                st.error(tb)
                st.markdown(
                    "<p style='color:#b71c1c;font-weight:700;'>Traceback (파일·줄 번호, 붉은 강조)</p>"
                    "<pre style='color:#7f0000;background:#ffebee;padding:0.75rem 1rem;border-radius:6px;"
                    "border:1px solid #ef9a9a;overflow:auto;max-height:28rem;font-size:0.78rem;"
                    "white-space:pre-wrap;word-break:break-word;'>"
                    f"{html.escape(tb)}</pre>",
                    unsafe_allow_html=True,
                )
                with st.expander("Traceback 전문 (코드 블록)", expanded=True):
                    st.code(tb, language="python")
                raw_text = st.session_state.get("last_gemini_raw_text", "")
                if raw_text:
                    st.error(f"🤖 Gemini 실제 답변 원문:\n\n{raw_text}")
    
        history_len = len(st.session_state.get("narrative_history", []))
        st.caption(f"📊 현재 누적된 분석 횟수: {history_len} 회")
    
        # ── 언어 전환 번역 (market_narrative 전용) ──────────────────────────
        _cv_source = st.session_state.get("current_view_source", "market_narrative")
        if (
            _cv_source == "market_narrative"
            and st.session_state.get("current_view")
            and st.session_state.get("current_view_language") != selected_language
        ):
            with st.spinner("선택한 언어로 결과를 자연스럽게 번역 중입니다..."):
                translated = translate_narrative_json(st.session_state["current_view"], selected_language)
                if translated:
                    st.session_state["current_view"] = translated
                    st.session_state["current_view_language"] = selected_language
                else:
                    st.warning("언어 전환 번역에 실패하여 기존 결과를 유지합니다.")

        narrative_data = st.session_state.get("current_view", {})
        _cv_source = st.session_state.get("current_view_source", "market_narrative")

        # ── 최근 분석 유형 배지 ────────────────────────────────────────────
        if narrative_data:
            _badge_map = {
                "market_narrative": ("🚀 AI 시장 내러티브", "info"),
                "weekly_trend_7d":  ("📊 주간 트렌드 추출 (최근 7일)", "success"),
                "wow_trend_7d":     ("⚖️ 트렌드 변곡점 (이번 주 vs 저번 주)", "success"),
            }
            _badge_label, _badge_type = _badge_map.get(_cv_source, ("최근 분석 결과", "info"))
            if _badge_type == "success":
                st.success(f"📌 현재 표시: **{_badge_label}** — 가장 최근 실행 결과입니다.")
            else:
                st.info(f"📌 현재 표시: **{_badge_label}** — 가장 최근 실행 결과입니다.")

        # ════════════════════════════════════════════════════════════════════
        # CASE A: weekly_trend_7d / wow_trend_7d → 마크다운 브리핑 렌더
        # ════════════════════════════════════════════════════════════════════
        if _cv_source in ("weekly_trend_7d", "wow_trend_7d") and isinstance(narrative_data, dict):
            briefing_md = str(
                narrative_data.get("weekly_briefing_markdown")
                or narrative_data.get("wow_briefing_markdown")
                or narrative_data.get("summary")
                or ""
            ).strip()
            nb_ts = st.session_state.get("narrative_timeseries_briefing")
            # session_state의 briefing이 더 최신이면 그쪽 우선
            if isinstance(nb_ts, dict) and nb_ts.get("markdown"):
                briefing_md = str(nb_ts.get("markdown") or briefing_md).strip()

            if briefing_md:
                st.markdown(briefing_md)
                # 팩트 체크 테이블
                fc_kind = "wow" if _cv_source == "wow_trend_7d" else "weekly"
                fc_df = nb_ts.get("factcheck_df") if isinstance(nb_ts, dict) else None

                # nb_ts 없거나 factcheck_df 비어있으면 briefing_md 로 즉석 재계산
                if not isinstance(fc_df, pd.DataFrame) or fc_df.empty:
                    try:
                        tickers_by_cat = extract_factcheck_tickers_from_briefing(briefing_md, kind=fc_kind)
                        if tickers_by_cat.get("all"):
                            if fc_kind == "wow":
                                fc_df = compute_narrative_factcheck_wow_returns(tickers_by_cat)
                            else:
                                fc_df = compute_narrative_factcheck_weekly_returns(tickers_by_cat)
                            # nb_ts 에 캐싱
                            if isinstance(nb_ts, dict):
                                nb_ts["factcheck_df"] = fc_df
                                st.session_state["narrative_timeseries_briefing"] = nb_ts
                    except Exception:
                        fc_df = pd.DataFrame()

                if isinstance(fc_df, pd.DataFrame) and not fc_df.empty:
                    st.subheader("📊 내러티브 팩트 체크 (실제 수익률)")
                    render_narrative_factcheck_table(fc_df, kind=fc_kind)
                else:
                    st.caption("팩트 체크 대상 티커가 추출되지 않았습니다.")
            else:
                st.warning("저장된 브리핑 마크다운이 없습니다. 다시 실행해주세요.")

        # ════════════════════════════════════════════════════════════════════
        # CASE B: market_narrative (기본) → JSON 기반 풀 렌더
        # ════════════════════════════════════════════════════════════════════
        else:
            regime_data     = narrative_data.get("regime", {}) if isinstance(narrative_data, dict) else {}
            themes_data     = narrative_data.get("themes", []) if isinstance(narrative_data, dict) else []
            rotation_data   = narrative_data.get("rotation", "") if isinstance(narrative_data, dict) else ""
            summary_data    = narrative_data.get("summary", "") if isinstance(narrative_data, dict) else ""
            top_quant_picks = narrative_data.get("top_quant_picks", "") if isinstance(narrative_data, dict) else ""

            if not narrative_data:
                st.warning("아직 AI 분석 결과가 없습니다. 상단 버튼을 눌러 실시간 내러티브를 생성하세요.")

            st.markdown("### 🧭 Market Regime Indicator")
            regime_col_1, regime_col_2, regime_col_3 = st.columns(3)
            with regime_col_1:
                st.metric("Risk On / Risk Off", regime_data.get("risk", "N/A"))
            with regime_col_2:
                st.metric("Growth vs Value", regime_data.get("growth_value", "N/A"))
            with regime_col_3:
                st.metric("Liquidity", regime_data.get("liquidity", "N/A"))

            # ── Top Quant Picks 배너 ─────────────────────────────────────────
            if top_quant_picks:
                st.divider()
                st.markdown("### 🏆 Top Quant Picks (정량+정성 동시 확인)")
                st.success(
                    f"**정량 모멘텀 + 뉴스 내러티브가 동시에 확인된 최우선 종목:** `{top_quant_picks}`  "
                    "RS Score 양수 + 200일선 위 + 내러티브 테마 일치 종목입니다."
                )

            st.divider()
            st.markdown("### 🔥 Current Market Themes & 📊 Narrative Breakdown")
            if isinstance(themes_data, list) and themes_data:
                for idx, theme in enumerate(themes_data, start=1):
                    theme = theme if isinstance(theme, dict) else {}
                    title = theme.get("title", f"Theme {idx}")
                    expanding_to_data = theme.get("expanding_to", [])
                    if isinstance(expanding_to_data, list):
                        expanding_lines = []
                        for flow in expanding_to_data:
                            flow = flow if isinstance(flow, dict) else {}
                            stage = str(flow.get("stage", "") or "").strip()
                            expected_tickers = str(flow.get("expected_tickers", "") or "").strip()
                            if stage or expected_tickers:
                                expanding_lines.append(f"- ➔ **{stage if stage else 'N/A'}**: `{expected_tickers if expected_tickers else 'N/A'}`")
                        expanding_to_display = "\n".join(expanding_lines) if expanding_lines else "- ➔ **N/A**: `N/A`"
                    else:
                        fallback_text = str(expanding_to_data or "N/A").strip()
                        expanding_to_display = f"- ➔ **Legacy Flow**: `{fallback_text}`"

                    momentum_note   = str(theme.get("momentum_note", "") or "").strip()
                    emerging_tickers = str(theme.get("emerging", "") or "").strip()
                    winners_str     = str(theme.get("winners", "") or "N/A").strip()

                    if "강함" in momentum_note or "strong" in momentum_note.lower():
                        mom_emoji = "🔥"
                    elif "약함" in momentum_note or "weak" in momentum_note.lower():
                        mom_emoji = "❄️"
                    else:
                        mom_emoji = "📊"

                    with st.expander(f"Theme {idx}: {title} {mom_emoji}", expanded=(idx == 1)):
                        st.markdown(f"**🎯 Winners (정량+정성 확인):** `{winners_str}`")
                        if emerging_tickers:
                            st.markdown(f"**🌱 Emerging (추적 필요):** `{emerging_tickers}`")
                        if momentum_note:
                            st.caption(f"📈 모멘텀: {momentum_note}")
                        st.markdown(
                            f"""
- **Driver (원인):** {theme.get("driver", "N/A")}
- **Expanding to (확장 흐름):**
{expanding_to_display}
- **Risk (위험 요인):** {theme.get("risk", "N/A")}
"""
                        )
            else:
                st.info("AI가 추출한 테마 데이터가 아직 없습니다.")

            st.divider()
            st.markdown("### 📈 Sector Rotation Map (자금 이동 지도)")
            st.markdown(rotation_data if rotation_data else "N/A")

            st.divider()
            st.markdown("### 🧠 Smart AI Summary")
            st.info(summary_data if summary_data else "N/A")

        # ── 내러티브 일관성 점수 ─────────────────────────────────────────
        st.divider()
        st.markdown("### 🎯 내러티브 일관성 점수")
        st.caption("최근 14일간 같은 테마/종목이 반복 등장할수록 신뢰도가 높아요. 일관성이 높은 테마에 집중 투자하세요.")
        _nc_uid = str(st.session_state.get("user_id") or "").strip()
        nc_data = calculate_narrative_consistency_score(_nc_uid, lookback_days=14)
        if not nc_data:
            st.info("아직 내러티브 기록이 부족해요. 분석을 더 진행하면 일관성 점수가 계산됩니다.")
        else:
            nc_score = nc_data.get("consistency_score", 0)
            nc_c1, nc_c2, nc_c3 = st.columns(3)
            with nc_c1:
                st.metric("일관성 점수", f"{nc_score} / 100",
                          delta="🔥 신뢰도 높음" if nc_score >= 60 else ("⚠️ 보통" if nc_score >= 30 else "❄️ 낮음"))
            with nc_c2:
                st.metric("분석 기록 수", f"{nc_data.get('total_records', 0)}개 (최근 14일)")
            with nc_c3:
                top_t = nc_data.get("top_themes", [])
                st.metric("가장 일관된 테마", top_t[0][0][:20] if top_t else "N/A",
                          delta=f"{top_t[0][1]}회 등장" if top_t else "")

            if nc_score >= 60:
                st.success(f"✅ 내러티브 일관성 높음 — 상위 테마를 집중 추적하세요.")
            elif nc_score >= 30:
                st.warning("🟡 내러티브 일관성 보통 — 분석을 더 쌓아야 신뢰도가 올라가요.")
            else:
                st.info("ℹ️ 아직 패턴이 불명확해요. 매일 꾸준히 분석하세요.")

            top_themes = nc_data.get("top_themes", [])
            top_tickers = nc_data.get("top_tickers", [])
            t_col1, t_col2 = st.columns(2)
            with t_col1:
                if top_themes:
                    st.markdown("**🔥 반복 등장 테마 Top 5:**")
                    max_theme_cnt = max(c for _, c in top_themes) if top_themes else 1
                    for theme, cnt in top_themes:
                        filled = round(cnt / max_theme_cnt * 8)
                        bar = "🟩" * filled + "⬜" * (8 - filled)
                        st.markdown(f"`{theme}` — {cnt}회  \n{bar}")
            with t_col2:
                if top_tickers:
                    st.markdown("**📌 반복 등장 종목 Top 10:**")
                    max_ticker_cnt = max(c for _, c in top_tickers[:10]) if top_tickers else 1
                    for tk, cnt in top_tickers[:10]:
                        filled = round(cnt / max_ticker_cnt * 8)
                        bar = "🟦" * filled + "⬜" * (8 - filled)
                        st.markdown(f"**{tk}** — {cnt}회  \n{bar}")

        # ── 기능 2: Emerging 종목 정량 교차 검증 ─────────────────────────
        # themes_data는 CASE B(market_narrative)에서만 정의되므로
        # wow/weekly source일 때 NameError 방지를 위해 여기서 안전하게 추출
        _cv_data_for_emerging = st.session_state.get("current_view", {})
        themes_data = (
            _cv_data_for_emerging.get("themes", [])
            if isinstance(_cv_data_for_emerging, dict)
            else []
        )
        if themes_data and isinstance(themes_data, list):
            all_emerging = []
            for theme in themes_data:
                if not isinstance(theme, dict):
                    continue
                em_str = str(theme.get("emerging", "") or "").strip()
                if em_str:
                    tks = [t.strip().upper() for t in em_str.replace(",", " ").split() if t.strip()]
                    all_emerging.extend(tks)
            all_emerging = list(dict.fromkeys(all_emerging))

            if all_emerging:
                st.divider()
                st.markdown("### 🔬 Emerging 종목 정량 교차 검증")
                st.caption(
                    f"AI가 제시한 **Emerging 종목 {len(all_emerging)}개**를 실제 가격/거래량 데이터로 검증합니다. "
                    "'아직 안 오른 2차 수혜주'를 자동으로 분류해요."
                )
                if st.button("🔍 Emerging 종목 검증 실행", key="emerging_verify_btn", type="primary", use_container_width=True):
                    with st.spinner(f"Emerging {len(all_emerging)}개 종목 정량 검증 중..."):
                        _v_result = verify_emerging_with_quant(all_emerging)
                    if _v_result:
                        st.session_state["_emerging_verified"] = _v_result
                        st.session_state["_emerging_themes_data"] = themes_data
                        # 자동 저장
                        _em_uid2 = str(st.session_state.get("user_id") or "").strip()
                        for _v in _v_result:
                            _tstr = ", ".join([str(t.get("title","")) for t in themes_data if isinstance(t,dict) and _v["ticker"] in str(t.get("emerging",""))])
                            upsert_emerging_tracker(_em_uid2, _v["ticker"], _tstr or "내러티브", _v["verdict"], _v["rs_score"])
                        st.rerun()
                    else:
                        st.warning("Emerging 종목 검증 데이터를 가져오지 못했습니다.")

                # 검증 결과 표시 (session_state에서 복원 → 버튼 클릭 후에도 유지)
                _verified_cache = st.session_state.get("_emerging_verified", [])
                if _verified_cache:
                    best = [v for v in _verified_cache if "최적" in v["verdict"]]
                    early = [v for v in _verified_cache if "얼리" in v["verdict"]]

                    if best:
                        st.success(f"🎯 **최적 매수 타이밍 {len(best)}개** — 아직 저평가 + 거래량 급증!")
                        for v in best:
                            vol_str = f"거래량 {v['vol_surge']:.1f}x" if v["vol_surge"] else ""
                            st.markdown(
                                f"**{v['ticker']}** — RS {v['rs_score']:+.1f}%p / "
                                f"1개월 {v['mom_1m']:+.1f}% / {vol_str} | _{v['detail']}_"
                            )
                            def _add_to_wl_em(tk=v["ticker"], vdict=v):
                                _uid_em = str(st.session_state.get("user_id") or "").strip()
                                # ✅ verify_emerging_with_quant 결과의 current_price 우선 사용
                                _preset = vdict.get("current_price")
                                if _preset and pd.notna(_preset):
                                    _cur_p = float(_preset)
                                else:
                                    try:
                                        fetch_latest_prices_for_tickers.clear()
                                    except Exception:
                                        pass
                                    _cur_p = fetch_latest_prices_for_tickers((tk,)).get(tk, np.nan)
                                _em_item = {
                                    "ticker": tk,
                                    "memo": f"Emerging 검증 - {vdict['verdict']}",
                                    "alert_price": np.nan,
                                    "alert_rsi": 30.0,
                                    "alert_ma200": True,
                                    "saved_price": float(_cur_p) if pd.notna(_cur_p) else np.nan,
                                    "date_added": _narrative_now_et_string(),
                                }
                                _em_wl = load_watchlist_sheet(_uid_em)
                                _em_wl = [x for x in _em_wl if x["ticker"] != tk]
                                _em_wl.append(_em_item)
                                _ok_em, _ = save_watchlist_sheet(_uid_em, _em_wl)
                                if _ok_em:
                                    st.session_state[f"_em_wl_added_{tk}"] = True
                                    st.session_state.pop("_sidebar_wl_count", None)
                            tk_key = v["ticker"]
                            if st.session_state.get(f"_em_wl_added_{tk_key}"):
                                st.success(f"✅ {tk_key} Watchlist 추가됨!")
                            else:
                                st.button(
                                    f"🔔 {tk_key} Watchlist 추가",
                                    key=f"em_wl_{tk_key}",
                                    on_click=_add_to_wl_em,
                                    use_container_width=False,
                                )

                    if early:
                        st.info(f"🌱 **얼리버드 기회 {len(early)}개** — 아직 초기, 200일선 위")
                        for v in early:
                            st.markdown(f"**{v['ticker']}** — {v['detail']}")
                            def _add_early_wl(tk=v["ticker"], vd=v):
                                _uid_el = str(st.session_state.get("user_id") or "").strip()
                                # ✅ verify_emerging_with_quant 결과의 current_price 우선 사용
                                _preset2 = vd.get("current_price")
                                if _preset2 and pd.notna(_preset2):
                                    _cur_p2 = float(_preset2)
                                else:
                                    try:
                                        fetch_latest_prices_for_tickers.clear()
                                    except Exception:
                                        pass
                                    _cur_p2 = fetch_latest_prices_for_tickers((tk,)).get(tk, np.nan)
                                _el_item = {
                                    "ticker": tk, "memo": f"Emerging 얼리버드 - {vd['detail']}",
                                    "alert_price": np.nan, "alert_rsi": 35.0, "alert_ma200": True,
                                    "saved_price": float(_cur_p2) if pd.notna(_cur_p2) else np.nan,
                                    "date_added": _narrative_now_et_string(),
                                }
                                _el_wl = load_watchlist_sheet(_uid_el)
                                _el_wl = [x for x in _el_wl if x["ticker"] != tk]
                                _el_wl.append(_el_item)
                                _ok_el, _ = save_watchlist_sheet(_uid_el, _el_wl)
                                if _ok_el:
                                    st.session_state[f"_em_wl_added_{tk}"] = True
                                    st.session_state.pop("_sidebar_wl_count", None)
                            tk_e = v["ticker"]
                            if st.session_state.get(f"_em_wl_added_{tk_e}"):
                                st.success(f"✅ {tk_e} Watchlist 추가됨!")
                            else:
                                st.button(f"🔔 {tk_e} Watchlist 추가", key=f"em_wl_e_{tk_e}", on_click=_add_early_wl)

                    with st.expander("📋 전체 Emerging 검증 결과", expanded=False):
                        em_rows = []
                        for v in _verified_cache:
                            em_rows.append({
                                "티커": v["ticker"], "판정": v["verdict"],
                                "RS Score": f"{v['rs_score']:+.1f}%p" if v["rs_score"] is not None else "N/A",
                                "1개월 수익률": f"{v['mom_1m']:+.1f}%" if v["mom_1m"] is not None else "N/A",
                                "200일선": "위 ✅" if v["above_ma200"] else ("아래 ❌" if v["above_ma200"] is False else "N/A"),
                                "거래량 배율": f"{v['vol_surge']:.1f}x" if v["vol_surge"] else "N/A",
                            })
                        st.dataframe(pd.DataFrame(em_rows), use_container_width=True, hide_index=True)
                    
                    st.caption(f"💾 {len(_verified_cache)}개 Emerging 종목 추적 시트에 자동 저장됨.")
    
        st.markdown("### 🕒 시계열 분석 엔진")
        st.caption(
            "`Quant_DB` → `Narratives` 시트에 저장된 `saved_at`(UTC)을 기준으로 구간을 나눈 뒤 Gemini로 브리핑합니다. "
            "**오늘**은 미국 동부(ET) 달력 기준입니다."
        )
        ts_b1, ts_b2, ts_b3 = st.columns(3)
        with ts_b1:
            btn_daily = st.button("📅 일일 분석 요약 (오늘)", key="narrative_ts_daily", use_container_width=True)
        with ts_b2:
            btn_weekly = st.button("📊 주간 트렌드 추출 (최근 7일)", key="narrative_ts_weekly", use_container_width=True)
        with ts_b3:
            btn_wow = st.button("⚖️ 트렌드 변곡점 분석 (이번 주 vs 저번 주)", key="narrative_ts_wow", use_container_width=True)
    
        def _pack_ts_records(recs, cap=30):
            out = []
            for r in recs[-cap:] if len(recs) > cap else recs:
                c = _compact_narrative_record_for_timeseries(r)
                if c:
                    out.append(c)
            return out
    
        disk_for_ts = list(st.session_state.get("narrative_history_disk_records") or [])
        anchor_utc = datetime.now(timezone.utc)
    
        if btn_daily:
            with st.spinner("오늘(ET) 기록을 모아 일일 브리핑을 작성하는 중입니다..."):
                today_recs = _filter_narrative_disk_recs_today_et(disk_for_ts)
                if not today_recs:
                    st.warning("오늘(미국 동부 달력 기준) 저장된 분석 기록이 없습니다.")
                else:
                    payload = {
                        "window": "today_et",
                        "count": len(today_recs),
                        "snapshots": _pack_ts_records(today_recs, cap=40),
                    }
                    text = run_narrative_timeseries_gemini("daily", payload, selected_language)
                    if text:
                        st.session_state["narrative_timeseries_briefing"] = {
                            "title": "📅 일일 분석 요약 (오늘 · ET)",
                            "markdown": text,
                        }
    
        if btn_weekly:
            with st.spinner("최근 7일 롤링 윈도우 기록을 집계하는 중입니다..."):
                week_recs = _filter_narrative_records_last_days(disk_for_ts, 7, anchor_utc=anchor_utc)
                if not week_recs:
                    st.warning("최근 7일 이내 저장된 분석 기록이 없습니다.")
                else:
                    payload = {
                        "window": "rolling_7d",
                        "anchor_utc": anchor_utc.isoformat(),
                        "count": len(week_recs),
                        "snapshots": _pack_ts_records(week_recs, cap=40),
                    }
                    text = run_narrative_timeseries_gemini("weekly", payload, selected_language)
                    if text:
                        factcheck_df = pd.DataFrame()
                        try:
                            tickers_by_cat = extract_factcheck_tickers_from_briefing(text, kind="weekly")
                            if tickers_by_cat.get("all"):
                                factcheck_df = compute_narrative_factcheck_weekly_returns(tickers_by_cat)
                        except Exception:
                            factcheck_df = pd.DataFrame()
                        st.session_state["narrative_timeseries_briefing"] = {
                            "title": "📊 주간 트렌드 추출 (최근 7일)",
                            "markdown": text,
                            "factcheck_kind": "weekly",
                            "factcheck_df": factcheck_df,
                        }
                        # current_view 갱신 — 분석 유형: weekly_trend_7d
                        weekly_analysis = {"source": "weekly_trend_7d", "weekly_briefing_markdown": text}
                        st.session_state["current_view"] = weekly_analysis
                        st.session_state["current_view_language"] = selected_language
                        st.session_state["current_view_source"] = "weekly_trend_7d"
                        append_weekly_trend_narrative_record(text, selected_language, week_recs)
                        st.session_state["narrative_history_disk_records"] = load_narrative_history_records()
                        st.session_state["narrative_history"] = [
                            r["analysis"] for r in st.session_state["narrative_history_disk_records"]
                        ]
                        st.info(
                            f"주간 트렌드 결과를 Google 시트 **`Narratives`**에 저장했습니다. "
                            f"「{_MAIN_NAV_OPTIONS[3]}」의 「주간 메가 트렌드」 소스에서 사용할 수 있습니다."
                        )
    
        if btn_wow:
            with st.spinner("이번 주 vs 저번 주 스냅샷을 비교하는 중입니다..."):
                this_w, last_w = _split_narrative_records_wow(disk_for_ts, anchor_utc=anchor_utc)
                if not this_w and not last_w:
                    st.warning("비교할 만큼 충분한 기록이 없습니다. 며칠 더 누적한 뒤 다시 시도해주세요.")
                else:
                    payload = {
                        "anchor_utc": anchor_utc.isoformat(),
                        "this_week_count": len(this_w),
                        "last_week_count": len(last_w),
                        "this_week_snapshots": _pack_ts_records(this_w, cap=28),
                        "last_week_snapshots": _pack_ts_records(last_w, cap=28),
                    }
                    text = run_narrative_timeseries_gemini("wow", payload, selected_language)
                    if text:
                        factcheck_df = pd.DataFrame()
                        try:
                            tickers_by_cat = extract_factcheck_tickers_from_briefing(text, kind="wow")
                            if tickers_by_cat.get("all"):
                                factcheck_df = compute_narrative_factcheck_wow_returns(tickers_by_cat)
                        except Exception:
                            factcheck_df = pd.DataFrame()
                        st.session_state["narrative_timeseries_briefing"] = {
                            "title": "⚖️ 트렌드 변곡점 (이번 주 vs 저번 주)",
                            "markdown": text,
                            "factcheck_kind": "wow",
                            "factcheck_df": factcheck_df,
                        }
                        # current_view 갱신 — 분석 유형: wow_trend_7d
                        wow_analysis = {"source": "wow_trend_7d", "weekly_briefing_markdown": text}
                        st.session_state["current_view"] = wow_analysis
                        st.session_state["current_view_language"] = selected_language
                        st.session_state["current_view_source"] = "wow_trend_7d"
                        # ── Sheets 자동 저장 (주간 트렌드와 동일 방식) ──
                        append_wow_trend_narrative_record(text, selected_language, this_w, last_w)
                        st.session_state["narrative_history_disk_records"] = load_narrative_history_records()
                        st.session_state["narrative_history"] = [
                            r["analysis"] for r in st.session_state["narrative_history_disk_records"]
                        ]
                        st.info(
                            "트렌드 변곡점 분석 결과를 Google 시트 **`Narratives`**에 저장했습니다. "
                            "과거 분석 기록에서 확인할 수 있습니다."
                        )
    
        nb_ts = st.session_state.get("narrative_timeseries_briefing")
        if isinstance(nb_ts, dict) and str(nb_ts.get("markdown") or "").strip():
            st.markdown("---")
            st.success(str(nb_ts.get("title") or "시계열 브리핑"))
            st.markdown(str(nb_ts.get("markdown") or "").strip())
    
            fc_kind = nb_ts.get("factcheck_kind")
            fc_df = nb_ts.get("factcheck_df")
            if fc_kind in ("weekly", "wow") and isinstance(fc_df, pd.DataFrame):
                st.subheader("📊 내러티브 팩트 체크 (실제 수익률)")
                render_narrative_factcheck_table(fc_df, kind=fc_kind)
    
        st.markdown("### 📚 과거 분석 기록 (`Quant_DB` / `Narratives`)")
        st.caption(
            f"Google 시트에서 최신순으로 불러옵니다. 기본 **10**건만 펼치며, "
            f"최대 **{_NARRATIVE_HISTORY_MAX_RECORDS}**건 · 최근 **{_NARRATIVE_HISTORY_RETENTION_DAYS}**일 이내만 유지합니다."
        )
        disk_recs = st.session_state.get("narrative_history_disk_records") or []
        if not disk_recs:
            st.caption("`Narratives` 시트에 아직 저장된 스냅샷이 없습니다.")
        else:
            sorted_disk = sorted(
                disk_recs,
                key=lambda r: _narrative_parse_saved_at_utc(r.get("saved_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            n_default = min(10, len(sorted_disk))
            n_show = int(st.session_state.get("_narrative_past_show_n", n_default))
            n_show = min(max(n_show, 1), len(sorted_disk))
            st.session_state["_narrative_past_show_n"] = n_show
            b_more, b_reset = st.columns(2)
            with b_more:
                if st.button("과거 기록 더 보기 (+10)", key="narrative_past_more_btn", disabled=n_show >= len(sorted_disk)):
                    st.session_state["_narrative_past_show_n"] = min(n_show + 10, len(sorted_disk))
            with b_reset:
                cap10 = min(10, len(sorted_disk))
                if st.button("최근 10건으로", key="narrative_past_reset_btn", disabled=n_show <= cap10):
                    st.session_state["_narrative_past_show_n"] = cap10
            st.caption(f"표시 중: **{n_show}** / {len(sorted_disk)}건 (최신순)")
            for rec in sorted_disk[:n_show]:
                exp_title = narrative_history_expander_title(rec)
                with st.expander(exp_title, expanded=False):
                    ts_raw = rec.get("saved_at", "") or ""
                    lang_r = rec.get("language", "")
                    sess = rec.get("session_label") or ""
                    st.caption(f"{ts_raw} (UTC) · 언어: {lang_r}" + (f" · {sess}" if sess else ""))
                    render_narrative_history_compact(rec.get("analysis") or {})
    
        st.divider()
        st.subheader("🔬 종목/테마 X-Ray 심층 분석")
        deep_dive_query = st.text_input(
            "심층 분석할 티커 또는 테마를 입력하세요 (예: PLTR, AMD, AI Infrastructure)",
            key="deep_dive_query_input",
        )
        if st.button("X-Ray 가동", key="deep_dive_run_button"):
            query = str(deep_dive_query or "").strip()
            if not query:
                st.warning("티커 또는 키워드를 입력해주세요.")
            else:
                with st.status("뉴스를 스캐닝 중...", expanded=True) as deep_status:
                    deep_news = fetch_targeted_news(query)
                    if not deep_news:
                        deep_status.update(label="❌ 뉴스 수집 실패", state="error", expanded=True)
                        st.error("유효한 타겟 뉴스 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해주세요.")
                    else:
                        st.write(f"✔️ {len(deep_news)}개 타겟 뉴스 확보")
                        deep_status.update(label="AI 심층 분석 중...", state="running", expanded=True)
                        deep_result = analyze_deep_dive(query, deep_news, selected_language)
                        if not deep_result:
                            deep_status.update(label="❌ AI 심층 분석 실패", state="error", expanded=True)
                            st.error("AI 심층 분석 결과를 파싱하지 못했습니다. 다시 시도해주세요.")
                        else:
                            deep_status.update(label="✅ X-Ray 심층 분석 완료", state="complete", expanded=False)
                            st.success(f"`{query}` X-Ray 분석이 완료되었습니다.")
    
                            labels = {
                                "ko": {
                                    "overview": "🏢 기업/테마 개요",
                                    "catalyst": "🔥 최근 주가 변동 촉매제",
                                    "sentiment": "📊 시장 심리 및 반응",
                                    "outlook": "🔭 향후 전망 및 관전 포인트",
                                    "expander": "🧬 Deep Dive 결과 보기",
                                },
                                "en": {
                                    "overview": "🏢 Company/Theme Overview",
                                    "catalyst": "🔥 Recent Catalyst",
                                    "sentiment": "📊 Market Sentiment",
                                    "outlook": "🔭 Forward Outlook",
                                    "expander": "🧬 View Deep Dive Results",
                                },
                            }
                            ui_text = labels["ko"] if selected_language == "ko" else labels["en"]
                            with st.expander(ui_text["expander"], expanded=True):
                                st.markdown(f"**{ui_text['overview']}**\n\n{deep_result.get('company_overview', 'N/A')}")
                                st.markdown(f"**{ui_text['catalyst']}**\n\n{deep_result.get('recent_catalyst', 'N/A')}")
                                st.markdown(f"**{ui_text['sentiment']}**\n\n{deep_result.get('market_sentiment', 'N/A')}")
                                st.markdown(f"**{ui_text['outlook']}**\n\n{deep_result.get('forward_outlook', 'N/A')}")
    
    elif main_nav == _MAIN_NAV_OPTIONS[4]:
        render_sync_button("sync_tab_scanner", [], "스캐너 결과 캐시를 초기화합니다.")

        # ── 세션 최초 진입 시 Sheets에서 마지막 스캔 결과 복원 ──────────────
        # (함수 정의 이후 시점이므로 NameError 없음)
        if not st.session_state.get("_scanner_last_result_restored"):
            _uid_restore = str(st.session_state.get("user_id") or "").strip()
            if _uid_restore:
                if st.session_state.get("scanner_results") is None:
                    _restored_l = load_scanner_last_result(_uid_restore, "leaders")
                    if _restored_l:
                        st.session_state["scanner_results"] = _restored_l
                if st.session_state.get("scanner_results_emerging") is None:
                    _restored_e = load_scanner_last_result(_uid_restore, "emerging")
                    if _restored_e:
                        st.session_state["scanner_results_emerging"] = _restored_e
            st.session_state["_scanner_last_result_restored"] = True

        st.subheader(f"{_MAIN_NAV_OPTIONS[4]} · 듀얼 엔진")
        st.caption(
            "**Current Leaders**는 기존 6대 팩터로 대장·테마 정렬을, **Emerging**은 2차 수혜·초기 모멘텀·거래량 가속·과열 회피 관점으로 같은 유니버스를 재스코어링합니다."
        )
    
        scanner_data_src = st.radio(
            "스캔 기반 데이터 (Data Source)",
            list(_OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS),
            key="opportunity_scanner_data_source",
        )
    
        tab_scan_leaders, tab_scan_emerge = st.tabs(
            ["🔥 Current Leaders (대장주 추종)", "🚀 Emerging Opportunities (후발주 선점)"]
        )

        # ── 아이디어C: 두 엔진 결과가 모두 있을 때 교집합/차집합 요약 ──────────
        _snap_l = st.session_state.get("scanner_results")
        _snap_e = st.session_state.get("scanner_results_emerging")
        if (
            isinstance(_snap_l, dict) and isinstance(_snap_l.get("score_df"), pd.DataFrame) and not _snap_l["score_df"].empty
            and isinstance(_snap_e, dict) and isinstance(_snap_e.get("score_df"), pd.DataFrame) and not _snap_e["score_df"].empty
        ):
            _l_df = _snap_l["score_df"]
            _e_df = _snap_e["score_df"]
            _top_n = 10
            _l_top = set(_l_df.head(_top_n)["Ticker"].str.upper().tolist())
            _e_top = set(_e_df.head(_top_n)["Ticker"].str.upper().tolist())
            _both = sorted(_l_top & _e_top)
            _only_l = sorted(_l_top - _e_top)
            _only_e = sorted(_e_top - _l_top)
            with st.expander("🔀 듀얼 엔진 크로스 분석 (두 결과가 모두 있을 때 자동 표시)", expanded=True):
                st.caption(f"양쪽 엔진 TOP{_top_n} 기준 비교")
                _cx1, _cx2, _cx3 = st.columns(3)
                with _cx1:
                    st.markdown("**✅ 양쪽 모두 상위권** (핵심 신호)")
                    if _both:
                        for _bt in _both:
                            _l_sc = _l_df[_l_df["Ticker"] == _bt]["Final Score"].values
                            _e_sc = _e_df[_e_df["Ticker"] == _bt]["Final Score"].values
                            _l_s = f"{float(_l_sc[0]):.1f}" if len(_l_sc) else "—"
                            _e_s = f"{float(_e_sc[0]):.1f}" if len(_e_sc) else "—"
                            st.markdown(f"- **{_bt}** (L:{_l_s} / E:{_e_s})")
                    else:
                        st.caption("교집합 없음")
                with _cx2:
                    st.markdown("**🔥 Leaders만 상위권** (현재 대장주)")
                    if _only_l:
                        for _lt in _only_l:
                            _l_sc = _l_df[_l_df["Ticker"] == _lt]["Final Score"].values
                            _l_s = f"{float(_l_sc[0]):.1f}" if len(_l_sc) else "—"
                            st.markdown(f"- **{_lt}** (L:{_l_s})")
                    else:
                        st.caption("Leaders 전용 종목 없음")
                with _cx3:
                    st.markdown("**🚀 Emerging만 상위권** (후발주 선점 후보)")
                    if _only_e:
                        for _et in _only_e:
                            _e_sc = _e_df[_e_df["Ticker"] == _et]["Final Score"].values
                            _e_s = f"{float(_e_sc[0]):.1f}" if len(_e_sc) else "—"
                            st.markdown(f"- **{_et}** (E:{_e_s})")
                    else:
                        st.caption("Emerging 전용 종목 없음")
    
        with tab_scan_leaders:
            st.markdown("##### 🔥 Current Leaders")

            # ── 아이디어E: 내러티브 미등장 상위 종목 피드백 분석 ──────────────
            _snap_fb = st.session_state.get("scanner_results")
            if isinstance(_snap_fb, dict) and isinstance(_snap_fb.get("score_df"), pd.DataFrame) and not _snap_fb["score_df"].empty:
                _fb_df = _snap_fb["score_df"].head(10)
                # 내러티브 정렬에 없었던(점수 낮은) 종목 중 Final Score 상위권
                _fb_exclude = frozenset(SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS)
                _blind_spots = _fb_df[
                    (_fb_df["Narrative Why"].isin(_fb_exclude) | (_fb_df.get("Narrative Score", pd.Series(dtype=float)) < 30))
                    & (_fb_df["Final Score"] >= 55)
                ]["Ticker"].tolist() if "Narrative Score" in _fb_df.columns else []
                if _blind_spots:
                    with st.expander(f"🧠 내러티브 피드백 루프 — 스캐너가 발견한 내러티브 사각지대 {len(_blind_spots)}종목", expanded=False):
                        st.caption(
                            "스캐너 Final Score 55↑이지만 내러티브 정렬 점수가 낮은 종목입니다. "
                            "다음 내러티브 생성 시 이 섹터/테마를 추가로 점검하세요."
                        )
                        st.code(", ".join(_blind_spots))
                        if st.button("🤖 Gemini로 사각지대 분석 요청", key="blind_spot_gemini_btn"):
                            _genai_client = _get_genai_client()
                            if _genai_client is None:
                                st.error("Gemini API 키가 설정되지 않았습니다.")
                            else:
                                _narrative_ctx = json.dumps(_snap_fb.get("scanner_data_source", ""), ensure_ascii=False)
                                _bs_prompt = f"""
당신은 월가 탑다운 스트래티지스트입니다.
아래 종목들은 퀀트 스캐너(모멘텀·RS·펀더멘털)에서 상위 점수를 받았지만,
최신 시장 내러티브 분석에서는 낮은 Narrative Alignment를 받은 종목들입니다.

종목: {', '.join(_blind_spots)}

다음을 간결하게 분석해주세요:
1) 이 종목들이 공유하는 테마/섹터/공급망 관계는 무엇인가?
2) 내러티브에서 놓친 이유(데이터 지연, 2차 수혜 구간, 섹터 rotation 초기 등)는?
3) 다음 내러티브 분석 시 추가로 점검해야 할 키워드 2~3개

한국어로, 간결하게 (총 200자 이내)."""
                                try:
                                    with st.spinner("Gemini 사각지대 분석 중..."):
                                        _bs_model = _genai_client.models
                                        _bs_resp = _bs_model.generate_content(
                                            model=_SCANNER_NARRATIVE_BATCH_MODEL_ID,
                                            contents=_bs_prompt,
                                        )
                                        _bs_text = str(getattr(_bs_resp, "text", "") or "").strip()
                                    if _bs_text:
                                        st.info(_bs_text)
                                    else:
                                        st.warning("분석 결과를 받지 못했습니다.")
                                except Exception as _bs_err:
                                    st.error(f"Gemini 분석 실패: {_bs_err}")
    
            selected_sector_labels = []
            manual_tickers_input = ""
            if scanner_data_src == _OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS[2]:
                st.caption(
                    "선택한 섹터는 각각 대표 ETF(XLK, XBI 등)의 공개 보유종목을 유니버스로 가져옵니다. "
                    "아래 티커 입력과 합쳐 중복 없이 스캔합니다."
                )
                selected_sector_labels = st.multiselect(
                    "주요 섹터 (복수 선택)",
                    options=[lbl for lbl, _ in _OPPORTUNITY_SCANNER_SECTOR_ETFS],
                    default=[],
                    help="Yahoo Finance가 제공하는 해당 ETF의 top holdings 기준입니다.",
                    key="opportunity_scanner_sector_ms",
                )
                manual_tickers_input = st.text_input(
                    "직접 티커 입력 (쉼표·공백 구분)",
                    placeholder="예: NVDA, MSFT, SMCI",
                    key="opportunity_scanner_manual_tickers",
                )

                # ── FMP Screener 자동 탐색 ───────────────────────────────
                with st.expander("🔍 FMP Screener 자동 탐색 (유니버스 보완)", expanded=False):
                    st.caption("시총 $10B↑ · 거래량 100만↑ 조건으로 FMP가 자동 필터링한 종목을 유니버스에 추가합니다.")
                    _scr_sector_map = {
                        "전체 (섹터 무관)": "",
                        "Technology": "Technology",
                        "Healthcare": "Healthcare",
                        "Financials": "Financials",
                        "Energy": "Energy",
                        "Consumer Cyclical": "Consumer Cyclical",
                        "Industrials": "Industrials",
                        "Communication Services": "Communication Services",
                        "Utilities": "Utilities",
                        "Real Estate": "Real Estate",
                        "Materials": "Materials",
                    }
                    _scr_sel = st.selectbox(
                        "스크리너 섹터 필터",
                        options=list(_scr_sector_map.keys()),
                        key="screener_sector_select",
                    )
                    _scr_cap_min = st.number_input(
                        "최소 시가총액 ($B)",
                        min_value=1, max_value=500, value=10, step=5,
                        key="screener_cap_min",
                    )
                    if st.button("🔍 Screener 탐색 실행", key="run_screener_btn", use_container_width=True):
                        with st.spinner("FMP Screener 조회 중..."):
                            _scr_results = _fmp_company_screener(
                                sector=_scr_sector_map[_scr_sel],
                                market_cap_min=int(_scr_cap_min * 1_000_000_000),
                                volume_min=1_000_000,
                                limit=50,
                            )
                        if _scr_results:
                            _scr_tickers = [str(r.get("symbol") or "").strip().upper() for r in _scr_results if r.get("symbol")]
                            _scr_tickers = filter_scanner_ticker_list(_scr_tickers)
                            st.session_state["_screener_extra_tickers"] = _scr_tickers
                            st.success(f"✅ {len(_scr_tickers)}개 종목 탐색 완료 — 스캔 실행 시 유니버스에 자동 포함됩니다.")
                            st.code(", ".join(_scr_tickers[:30]) + ("..." if len(_scr_tickers) > 30 else ""))
                        else:
                            st.warning("스크리너 결과를 가져올 수 없습니다.")
                    _scr_extra = st.session_state.get("_screener_extra_tickers", [])
                    if _scr_extra:
                        st.caption(f"📌 현재 탐색된 추가 종목: {len(_scr_extra)}개 (스캔 실행 시 자동 병합)")

            elif scanner_data_src == _OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS[0]:
                st.caption(
                    "「Current Leaders 스캔」은 `Narratives` 시트 **Winners** 열(또는 JSON의 winners)만 사용합니다. "
                    "「Emerging Opportunities 스캔」은 **Emerging** 열만 사용합니다."
                )
            else:
                st.caption(
                    "`Quant_DB` / `Narratives`에서 **가장 최근 주간 트렌드(최근 7일)** 저장분의 티커 풀·브리핑을 사용합니다. "
                    f"「{_MAIN_NAV_OPTIONS[1]}」 메뉴에서 「📊 주간 트렌드 추출」 실행 시 `Narratives` 시트에 자동 저장됩니다."
                )
    
            run_scanner = st.button("Current Leaders 스캔", key="run_ai_opportunity_scanner", use_container_width=True, type="primary")
    
            if run_scanner:
                target_universe = []
                latest_analysis = {}
                src_note = ""
    
                if scanner_data_src == _OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS[2]:
                    target_universe = build_opportunity_scanner_universe_from_direct_selection(
                        selected_sector_labels, manual_tickers_input
                    )
                    # FMP Screener 자동 탐색 결과 병합
                    _scr_extra_l = st.session_state.get("_screener_extra_tickers", [])
                    if _scr_extra_l:
                        target_universe = list(dict.fromkeys(target_universe + _scr_extra_l))
                    latest_analysis = resolve_opportunity_scanner_narrative_for_direct_mode()
                    src_note = "수동 섹터/티커"
                    scanner_mode_saved = "섹터/티커 직접 스캔"
                    if not target_universe:
                        st.warning("섹터를 하나 이상 선택하거나, 티커를 입력한 뒤 다시 실행해주세요.")
                elif scanner_data_src == _OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS[0]:
                    target_universe, latest_analysis = get_latest_narrative_sheet_winners_tickers_only()
                    src_note = "최신 내러티브 · Winners"
                    scanner_mode_saved = "내러티브 Winners 스캔"
                    if not target_universe:
                        st.info("분석된 티커가 없습니다.")
                else:
                    target_universe, latest_analysis = get_latest_weekly_trend_scan_universe_and_analysis()
                    src_note = "주간 메가 트렌드"
                    scanner_mode_saved = "내러티브 기반 스캔"
                    if not isinstance(latest_analysis, dict) or latest_analysis.get("source") != _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
                        st.warning(
                            f"저장된 주간 트렌드(최근 7일) 분석이 없습니다. 「{_MAIN_NAV_OPTIONS[1]}」에서 "
                            "「📊 주간 트렌드 추출 (최근 7일)」을 먼저 실행해 주세요."
                        )
                    elif not target_universe:
                        st.warning(
                            "주간 트렌드 기록은 있으나 추출된 티커가 없습니다. 7일 구간 스냅샷 또는 브리핑 본문을 확인해 주세요."
                        )
    
                if target_universe:
                    mode_note = f"Current Leaders · {src_note}"
                    score_df = score_opportunity_universe(target_universe, latest_analysis)
                    if score_df.empty:
                        st.error("스코어링 결과가 비어 있습니다. 데이터 소스 상태를 확인한 뒤 다시 시도해주세요.")
                    else:
                        narrative_summary_rows = score_df[["Ticker", "Narrative Why", "Risk"]].copy()
                        st.session_state["scanner_results"] = {
                            "score_df": score_df.copy(),
                            "narrative_ai_summary": narrative_summary_rows.to_dict("records"),
                            "mode_note": mode_note,
                            "scanner_mode": scanner_mode_saved,
                            "scanner_data_source": scanner_data_src,
                            "universe": list(target_universe),
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                        }
                        st.success("Current Leaders 스캔 완료 — 결과가 세션에 저장되었습니다.")
                        # 세션 재시작 후 복원을 위해 Sheets에 마지막 결과 저장
                        _lr_uid = str(st.session_state.get("user_id") or "").strip()
                        save_scanner_last_result(_lr_uid, st.session_state["scanner_results"], engine="leaders")
    
            snap = st.session_state.get("scanner_results")
            if isinstance(snap, dict) and isinstance(snap.get("score_df"), pd.DataFrame) and not snap["score_df"].empty:
                render_opportunity_scanner_snapshot(snap)

                # 스캐너 결과 자동 저장
                _sc_uid = str(st.session_state.get("user_id") or "").strip()
                _sc_last = st.session_state.get("_scanner_saved_date")
                _sc_today = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
                if _sc_last != _sc_today:
                    _ok_sc, _ = save_scanner_result_history(_sc_uid, snap["score_df"], engine="leaders")
                    if _ok_sc:
                        st.session_state["_scanner_saved_date"] = _sc_today
                        load_scanner_history.clear()
                        st.caption("💾 스캐너 결과가 히스토리에 저장됐어요.")

            elif not run_scanner:
                st.caption("스캔을 실행하면 결과가 세션에 저장되며, 사이드바·다른 메뉴로 이동해도 유지됩니다.")

            # ── 스캐너 히스토리 ────────────────────────────────────────────
            _sc_uid2 = str(st.session_state.get("user_id") or "").strip()
            with st.expander("📚 Current Leaders 히스토리 (과거 TOP 결과 추적)", expanded=False):
                sc_hist = load_scanner_history(_sc_uid2, engine="leaders")
                if sc_hist.empty:
                    st.info("아직 히스토리가 없어요. 스캔 실행 시 자동 저장됩니다.")
                else:
                    # 종목별 등장 빈도
                    ticker_freq = sc_hist.groupby("Ticker").agg(
                        등장횟수=("Ticker", "count"),
                        평균점수=("Score", "mean"),
                        최근날짜=("Date", "max"),
                        최고순위=("Rank", "min"),
                    ).reset_index().sort_values("등장횟수", ascending=False)
                    ticker_freq["평균점수"] = ticker_freq["평균점수"].round(1)

                    st.markdown("**🏆 자주 등장한 종목 (신뢰도 높은 신호)**")
                    st.dataframe(ticker_freq.head(15), use_container_width=True, hide_index=True)

                    # ── 아이디어B: 연속 상위권 자동 Watchlist 추가 제안 ──────
                    _total_scans = sc_hist["Date"].nunique()
                    if _total_scans >= 3:
                        _strong = ticker_freq[
                            (ticker_freq["등장횟수"] >= max(3, int(_total_scans * 0.5))) &
                            (ticker_freq["평균점수"] >= 60.0)
                        ].head(5)
                        if not _strong.empty:
                            st.markdown("**🎯 반복 상위권 종목 — Watchlist 추가 추천**")
                            st.caption(f"전체 스캔 {_total_scans}회 중 50% 이상 등장 + 평균점수 60↑ 조건")
                            _strong_cols = st.columns(min(len(_strong), 5))
                            for _si, (_, _sr) in enumerate(_strong.iterrows()):
                                _stk = str(_sr["Ticker"]).strip().upper()
                                with _strong_cols[_si]:
                                    def _add_strong_wl(tk=_stk, avg_sc=_sr["평균점수"], cnt=_sr["등장횟수"]):
                                        _uid_st = str(st.session_state.get("user_id") or "").strip()
                                        add_to_watchlist(
                                            _uid_st, tk,
                                            memo=f"반복 상위권({cnt}회 등장, 평균{avg_sc:.1f}점)",
                                        )
                                        st.session_state[f"_hist_wl_{tk}"] = True
                                    if st.session_state.get(f"_hist_wl_{_stk}"):
                                        st.success(f"✅ {_stk}")
                                    else:
                                        st.button(f"🔔 {_stk} 추가", key=f"hist_wl_{_si}_{_stk}",
                                            on_click=_add_strong_wl, use_container_width=True)

                    # 최근 스캔 결과
                    st.markdown("**📅 최근 스캔 기록**")
                    recent_sc = sc_hist.sort_values("Date", ascending=False).head(20)
                    st.dataframe(recent_sc, use_container_width=True, hide_index=True)
    
        with tab_scan_emerge:
            st.markdown("##### 🚀 Emerging Opportunities")
    
            selected_sector_labels_em = []
            manual_tickers_input_em = ""
            if scanner_data_src == _OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS[2]:
                st.caption(
                    "Data Source가 수동일 때만 아래에서 유니버스를 구성합니다. "
                    "섹터 선택값은 Leaders 탭과 **독립**으로 저장됩니다 — 여기서 따로 선택해주세요."
                )
                selected_sector_labels_em = st.multiselect(
                    "주요 섹터 (복수 선택)",
                    options=[lbl for lbl, _ in _OPPORTUNITY_SCANNER_SECTOR_ETFS],
                    default=[],
                    help="Yahoo Finance가 제공하는 해당 ETF의 top holdings 기준입니다.",
                    key="opportunity_scanner_sector_ms_em",
                )
                manual_tickers_input_em = st.text_input(
                    "직접 티커 입력 (쉼표·공백 구분)",
                    placeholder="예: NVDA, MSFT, SMCI",
                    key="opportunity_scanner_manual_tickers_em",
                )
            elif scanner_data_src == _OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS[0]:
                st.caption(
                    "「Emerging Opportunities 스캔」은 `Narratives` 시트 **Emerging** 열(또는 JSON expanding_to)만 사용합니다."
                )
            else:
                st.caption("Current Leaders와 동일한 **주간 메가 트렌드** 소스로 유니버스를 구성합니다.")
    
            run_emerge = st.button(
                "Emerging Opportunities 스캔",
                key="run_emerging_opportunity_scanner",
                use_container_width=True,
            )
    
            if run_emerge:
                target_u_em = []
                latest_a_em = {}
                src_note_em = ""
    
                if scanner_data_src == _OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS[2]:
                    target_u_em = build_opportunity_scanner_universe_from_direct_selection(
                        selected_sector_labels_em, manual_tickers_input_em
                    )
                    latest_a_em = resolve_opportunity_scanner_narrative_for_direct_mode()
                    src_note_em = "수동 섹터/티커"
                    scanner_mode_saved_em = "섹터/티커 직접 스캔"
                    if not target_u_em:
                        st.warning("섹터를 하나 이상 선택하거나, 티커를 입력한 뒤 다시 실행해주세요.")
                elif scanner_data_src == _OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS[0]:
                    target_u_em, latest_a_em = get_latest_narrative_sheet_emerging_tickers_only()
                    src_note_em = "최신 내러티브 · Emerging"
                    scanner_mode_saved_em = "내러티브 Emerging 스캔"
                    if not target_u_em:
                        st.info("분석된 티커가 없습니다.")
                else:
                    target_u_em, latest_a_em = get_latest_weekly_trend_scan_universe_and_analysis()
                    src_note_em = "주간 메가 트렌드"
                    scanner_mode_saved_em = "내러티브 기반 스캔"
                    if not isinstance(latest_a_em, dict) or latest_a_em.get("source") != _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
                        st.warning(
                            f"저장된 주간 트렌드(최근 7일) 분석이 없습니다. 「{_MAIN_NAV_OPTIONS[1]}」에서 "
                            "「📊 주간 트렌드 추출 (최근 7일)」을 먼저 실행해 주세요."
                        )
                    elif not target_u_em:
                        st.warning(
                            "주간 트렌드 기록은 있으나 추출된 티커가 없습니다. 7일 구간 스냅샷 또는 브리핑 본문을 확인해 주세요."
                        )
    
                if target_u_em:
                    mode_note_em = f"Emerging · {src_note_em}"
                    em_df = score_emerging_opportunity_universe(target_u_em, latest_a_em)
                    if em_df.empty:
                        st.error("Emerging 스코어링 결과가 비어 있습니다. 데이터 소스를 확인해주세요.")
                    else:
                        st.session_state["scanner_results_emerging"] = {
                            "score_df": em_df.copy(),
                            "mode_note": mode_note_em,
                            "scanner_mode": scanner_mode_saved_em,
                            "scanner_data_source": scanner_data_src,
                            "universe": list(target_u_em),
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                        }
                        # 히스토리 저장은 아래 자동저장 로직(_em_sc_last != _em_sc_today)에서 일괄 처리
                        st.success("Emerging Opportunities 스캔 완료 — 결과가 세션에 저장되었습니다.")
                        # 세션 재시작 후 복원을 위해 Sheets에 마지막 결과 저장
                        _em_lr_uid = str(st.session_state.get("user_id") or "").strip()
                        save_scanner_last_result(_em_lr_uid, st.session_state["scanner_results_emerging"], engine="emerging")
    
            snap_em = st.session_state.get("scanner_results_emerging")
            if isinstance(snap_em, dict) and isinstance(snap_em.get("score_df"), pd.DataFrame) and not snap_em["score_df"].empty:
                render_opportunity_emerging_snapshot(snap_em)

                # Emerging 자동 저장 (스캔 직후 session에 있을 때)
                _em_sc_uid = str(st.session_state.get("user_id") or "").strip()
                _em_sc_last = st.session_state.get("_em_scanner_saved_date")
                _em_sc_today = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
                if _em_sc_last != _em_sc_today:
                    _ok_em_sc, _ = save_scanner_result_history(_em_sc_uid, snap_em["score_df"], engine="emerging")
                    if _ok_em_sc:
                        st.session_state["_em_scanner_saved_date"] = _em_sc_today
                        load_scanner_history.clear()

            elif not run_emerge:
                st.caption("Emerging 엔진 스캔을 실행하면 RSI·거래량 가속 지표와 함께 결과가 세션에 유지됩니다.")

            # ── Emerging 히스토리 ──────────────────────────────────────────
            _em_uid3 = str(st.session_state.get("user_id") or "").strip()
            with st.expander("📚 Emerging 히스토리 (과거 TOP 결과 추적)", expanded=False):
                em_sc_hist = load_scanner_history(_em_uid3, engine="emerging")
                if em_sc_hist.empty:
                    st.info("아직 히스토리가 없어요. Emerging 스캔 실행 시 자동 저장됩니다.")
                else:
                    # 종목별 등장 빈도
                    em_freq = em_sc_hist.groupby("Ticker").agg(
                        등장횟수=("Ticker", "count"),
                        평균점수=("Score", "mean"),
                        최근날짜=("Date", "max"),
                        최고순위=("Rank", "min"),
                    ).reset_index().sort_values("등장횟수", ascending=False)
                    em_freq["평균점수"] = em_freq["평균점수"].round(1)

                    st.markdown("**🌱 자주 등장한 Emerging 종목 (신뢰도 높은 신호)**")
                    st.dataframe(em_freq.head(15), use_container_width=True, hide_index=True)

                    # ── 아이디어B: 연속 상위권 자동 Watchlist 추가 제안 ──────
                    _em_total_scans = em_sc_hist["Date"].nunique()
                    if _em_total_scans >= 3:
                        _em_strong = em_freq[
                            (em_freq["등장횟수"] >= max(3, int(_em_total_scans * 0.5))) &
                            (em_freq["평균점수"] >= 55.0)
                        ].head(5)
                        if not _em_strong.empty:
                            st.markdown("**🎯 반복 상위권 Emerging 종목 — Watchlist 추가 추천**")
                            st.caption(f"전체 스캔 {_em_total_scans}회 중 50% 이상 등장 + 평균점수 55↑ 조건")
                            _em_strong_cols = st.columns(min(len(_em_strong), 5))
                            for _esi, (_, _esr) in enumerate(_em_strong.iterrows()):
                                _estk = str(_esr["Ticker"]).strip().upper()
                                with _em_strong_cols[_esi]:
                                    def _add_em_strong_wl(tk=_estk, avg_sc=_esr["평균점수"], cnt=_esr["등장횟수"]):
                                        _uid_est = str(st.session_state.get("user_id") or "").strip()
                                        add_to_watchlist(
                                            _uid_est, tk,
                                            memo=f"Emerging 반복({cnt}회, 평균{avg_sc:.1f}점)",
                                            alert_rsi=35.0,
                                        )
                                        st.session_state[f"_em_hist_wl_{tk}"] = True
                                    if st.session_state.get(f"_em_hist_wl_{_estk}"):
                                        st.success(f"✅ {_estk}")
                                    else:
                                        st.button(f"🔔 {_estk}", key=f"em_hist_wl_{_esi}_{_estk}",
                                            on_click=_add_em_strong_wl, use_container_width=True)

                    # 최근 스캔 기록
                    st.markdown("**📅 최근 Emerging 스캔 기록**")
                    recent_em = em_sc_hist.sort_values("Date", ascending=False).head(20)
                    st.dataframe(recent_em, use_container_width=True, hide_index=True)
    
    elif main_nav == _MAIN_NAV_OPTIONS[3]:
        syn_s1, syn_s2 = st.columns([1, 3])
        with syn_s1:
            if st.button("🔄 현재 페이지 데이터 동기화", key="sync_tab_sector", use_container_width=True):
                tab_sync_refresh(
                    [
                        cached_sector_etf_closes.clear,
                        cached_pool_monthly_returns.clear,
                        cached_etf_universe_rankings_full.clear,
                    ],
                    rerun_after=True,
                )
        with syn_s2:
            st.caption("섹터 ETF·세부 종목 풀 데이터 캐시를 비우고 해당 화면을 다시 열 때 최신 데이터를 사용합니다.")
    
        # ── ETF Universe 관리 expander ────────────────────────────────────
        with st.expander("🆕 ETF Universe 관리 (자동 업데이트)", expanded=False):
            st.caption(
                "FMP API로 최근 90일 내 신규 상장 ETF를 자동 스캔해 Google Sheets `ETF_Universe` 탭에 추가합니다. "
                f"업데이트 주기: **{_ETF_AUTO_UPDATE_INTERVAL_DAYS}일마다 자동 실행**. 수동으로도 실행할 수 있어요."
            )

            eu_col1, eu_col2 = st.columns(2)
            with eu_col1:
                current_merged = load_etf_universe_tickers_merged()
                file_only = load_etf_universe_tickers()
                sheet_only = load_etf_universe_from_sheet()
                st.metric("전체 ETF Universe", f"{len(current_merged)}개")
                st.caption(f"📄 etf_universe.txt: {len(file_only)}개 | ☁️ Sheets 자동추가: {len(sheet_only)}개")

            with eu_col2:
                last_run = st.session_state.get("_etf_auto_update_last_run")
                last_run_str = last_run.astimezone(_MARKET_ET_TZ).strftime("%m/%d %H:%M") if last_run else "이번 세션 미실행"
                st.metric("마지막 자동 업데이트", last_run_str)

            manual_col1, manual_col2 = st.columns(2)
            with manual_col1:
                if st.button("🔍 신규 ETF 지금 스캔", key="etf_manual_scan_btn", use_container_width=True, type="primary"):
                    with st.spinner("FMP API로 신규 ETF 스캔 중... (약 30초 소요)"):
                        st.session_state["_etf_auto_update_last_run"] = None  # 강제 재실행
                        _m_added, _m_err = run_etf_auto_update_if_needed(silent=False)
                    if _m_err:
                        st.error(f"스캔 오류: {_m_err}")
                    elif _m_added > 0:
                        st.success(f"✅ 신규 ETF {_m_added}개가 추가됐어요!")
                        cached_etf_universe_rankings_full.clear()
                        st.rerun()
                    else:
                        st.info("최근 90일 내 신규 상장된 ETF(AUM $50M 이상)가 없거나 이미 모두 등록되어 있어요.")

            with manual_col2:
                if st.button("📋 Sheets 추가 목록 보기", key="etf_sheet_list_btn", use_container_width=True):
                    if sheet_only:
                        st.dataframe(
                            pd.DataFrame({"자동추가 ETF": sheet_only}),
                            use_container_width=True,
                            hide_index=True,
                        )
                    else:
                        st.info("Sheets에 자동 추가된 ETF가 없어요.")

            if st.button("📥 txt 티커를 시트로 통합 (1회 실행)", key="etf_txt_import_btn", use_container_width=True):
                with st.spinner("etf_universe.txt 티커를 ETF_Universe 시트로 통합 중..."):
                    _imp_added, _imp_err = import_txt_tickers_to_sheet()
                if _imp_err:
                    st.error(f"통합 오류: {_imp_err}")
                elif _imp_added > 0:
                    st.success(
                        f"✅ txt 티커 {_imp_added}개를 시트로 통합했어요! "
                        "이제 `etf_universe.txt` 없이도 동작합니다. "
                        "(source=TXT_IMPORT → 저품질 자동정리에서 보호됨)"
                    )
                    load_etf_universe_from_sheet.clear()
                    cached_etf_universe_rankings_full.clear()
                    st.rerun()
                else:
                    st.info("시트에 이미 모든 txt 티커가 들어 있어요. 통합할 게 없습니다.")
            st.caption(
                "📥 txt → 시트 통합: `etf_universe.txt`의 티커를 시트로 옮깁니다. "
                "통합 후에는 txt 파일이 없어도 됩니다."
            )

            st.caption("🧹 저품질 ETF 자동 정리 조건: 상장 6개월 이상 & AUM $100M 미만 & 30일 평균 거래대금 $1M 미만")
            if st.button("🧹 저품질 ETF 정리 실행", key="etf_cleanup_btn", use_container_width=True):
                with st.spinner("유동성/AUM 기준으로 저품질 ETF 정리 중..."):
                    removed_n, clean_err = cleanup_low_quality_etfs_from_sheet(
                        min_avg_volume_m=1.0, min_aum_m=100.0
                    )
                if clean_err:
                    st.error(f"정리 오류: {clean_err}")
                elif removed_n > 0:
                    st.success(f"✅ 저품질 ETF {removed_n}개를 정리했어요!")
                    cached_etf_universe_rankings_full.clear()
                    st.rerun()
                else:
                    st.info("정리 대상 ETF가 없어요. 리스트 품질이 좋은 상태예요.")

        st.subheader(f"{_MAIN_NAV_OPTIONS[3]} · 섹터 ETF 상대 강도")
        st.caption("주요 섹터/테마 ETF 상대 강도 점검 (2년 데이터 기반)")
    
        sector_etfs = [
            ("XLK", "기술"),
            ("XLV", "헬스케어"),
            ("XLF", "금융"),
            ("XLE", "에너지"),
            ("XLY", "자유소비재"),
            ("XLI", "산업재"),
            ("XLC", "통신"),
            ("XLU", "유틸리티"),
            ("XLRE", "부동산"),
            ("XLB", "소재"),
            ("UFO", "우주항공"),
            ("SOXX", "반도체"),
            ("URA", "원자력/우라늄"),
            ("BOTZ", "AI 및 로봇"),
            ("XBI", "바이오테크"),
            ("IWM", "중소형주/러셀2000"),
            ("GLD", "금/안전자산"),
            ("ITA", "방산/항공"),
            ("CIBR", "사이버보안"),
            ("INDA", "인도 시장"),
            ("IBIT", "비트코인 현물"),
        ]
        sector_tickers = [ticker for ticker, _ in sector_etfs]
        sector_holdings_map = {
            "XLK": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "ADBE", "CRM", "AMD", "CSCO", "INTU", "QCOM", "AMAT", "TXN", "NOW", "IBM"],
            "XLV": ["LLY", "UNH", "JNJ", "MRK", "ABBV", "PFE", "TMO", "DHR", "AMGN", "GILD", "BMY", "ISRG", "VRTX", "SYK", "CVS"],
            "XLF": ["JPM", "BRK-B", "V", "MA", "BAC", "WFC", "GS", "MS", "SCHW", "BLK", "AXP", "C", "PGR", "AIG", "USB"],
            "XLE": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "KMI", "HAL", "BKR", "DVN", "FANG", "WMB"],
            "XLY": ["AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "NKE", "SBUX", "TJX", "CMG", "RCL", "MAR", "GM", "F", "ORLY"],
            "XLI": ["GE", "CAT", "RTX", "HON", "UNP", "LMT", "DE", "ETN", "BA", "NOC", "UPS", "FDX", "WM", "EMR", "ITW"],
            "XLC": ["GOOGL", "META", "NFLX", "TMUS", "DIS", "VZ", "T", "CHTR", "CMCSA", "EA", "TTWO", "FOXA", "WBD", "DASH", "SPOT"],
            "XLU": ["NEE", "SO", "DUK", "CEG", "AEP", "EXC", "SRE", "XEL", "D", "PCG", "PEG", "ED", "EIX", "WEC", "ETR"],
            "XLRE": ["AMT", "PLD", "EQIX", "SPG", "O", "WELL", "PSA", "DLR", "CCI", "CBRE", "VICI", "AVB", "EQR", "ESS", "EXR"],
            "XLB": ["LIN", "APD", "SHW", "ECL", "NUE", "FCX", "DOW", "DD", "CTVA", "NEM", "MLM", "VMC", "PPG", "LYB", "MOS"],
            "UFO": ["PLTR", "RKLB", "LHX", "RTX", "NOC", "BA", "AJRD", "IRDM", "SPIR", "SATL", "ASTS", "MAXR", "MDA", "BKSY", "VSAT"],
            "SOXX": ["NVDA", "AVGO", "AMD", "MU", "TXN", "AMAT", "QCOM", "INTC", "ASML", "LRCX", "KLAC", "ADI", "MCHP", "ON", "NXPI"],
            "URA": ["CCJ", "BWXT", "UUUU", "SMR", "NXE", "LEU", "LTBR", "DNN", "UEC", "URNM", "CVI", "RYCEY", "FLR", "GEV", "CEG"],
            "BOTZ": ["NVDA", "ABB", "ISRG", "PATH", "SYM", "ROK", "FANUY", "TER", "OMCL", "CGNX", "IRBT", "UI", "ROBO", "NARI", "MDT"],
            "XBI": ["GILD", "BIIB", "REGN", "VRTX", "ALNY", "MRNA", "BNTX", "AMGN", "ILMN", "SRPT", "CRSP", "EXEL", "NBIX", "INCY", "ARGX"],
            "IWM": ["SMCI", "CRWD", "INSM", "CELH", "DKNG", "PLTR", "RKLB", "APP", "SFM", "ONTO", "TMDX", "NXT", "FSLY", "CVNA", "UPST"],
            "GLD": ["GLD", "IAU", "GDX", "NEM", "AEM", "GOLD", "WPM", "RGLD", "FNV", "KGC", "AU", "BTG", "SSRM", "PAAS", "AGI"],
            "ITA": ["RTX", "LMT", "NOC", "GD", "BA", "HII", "TXT", "TDG", "HEI", "KTOS", "AVAV", "LDOS", "LHX", "CW", "MRCY"],
            "CIBR": ["CRWD", "PANW", "FTNT", "ZS", "CYBR", "OKTA", "GEN", "CHKP", "TENB", "RPD", "S", "NET", "DDOG", "AKAM", "QLYS"],
            "INDA": ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", "LT.NS", "BHARTIARTL.NS", "SBIN.NS", "ITC.NS", "HINDUNILVR.NS", "AXISBANK.NS", "MARUTI.NS", "SUNPHARMA.NS", "KOTAKBANK.NS", "BAJFINANCE.NS"],
            "IBIT": ["IBIT", "COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT", "BITF", "BTDR", "CIFR", "WULF", "IREN", "CORZ", "GLXY.TO", "HIVE"],
        }
    
        try:
            with st.spinner("섹터 ETF 데이터를 불러오는 중..."):
                close_df = cached_sector_etf_closes(tuple(sector_tickers))
            sector_returns_df = build_sector_returns_table(close_df, sector_etfs)
    
            if sector_returns_df.empty:
                st.warning("섹터 수익률 테이블을 생성할 수 없습니다. 데이터 제공 상태를 확인해주세요.")
            else:
                perf_cols = ["1-Week (%)", "1-Month (%)", "3-Month (%)", "6-Month (%)", "1-Year (%)"]
                st.info(
                    "🟩 진한 초록: 거대 자본 유입 (강한 매수세/대세 상승)\n"
                    "🟨 노랑/연두: 중립 및 관망 (시장 평균 수준)\n"
                    "🟥 진한 빨강: 거대 자본 유출 (강한 매도세/하락 추세)\n"
                    "💡 [Tip] 1개월과 3개월이 모두 진한 초록색인 섹터가 진짜 '장기 주도 섹터'입니다. "
                    "이번 주(1-Week)만 빨간색이라면 훌륭한 분할 매수(조정) 기회일 수 있습니다."
                )
                styled = (
                    sector_returns_df.style.format(
                        {
                            "1-Week (%)": "{:.2f}%",
                            "1-Month (%)": "{:.2f}%",
                            "3-Month (%)": "{:.2f}%",
                            "6-Month (%)": "{:.2f}%",
                            "1-Year (%)": "{:.2f}%",
                        },
                        na_rep="N/A",
                    )
                    .background_gradient(
                        cmap="RdYlGn",
                        subset=perf_cols,
                        axis=0,
                    )
                )
                st.dataframe(styled, use_container_width=True, hide_index=True, height=560)
    
                missing_cells = int(sector_returns_df[perf_cols].isna().sum().sum())
                if missing_cells > 0:
                    st.info("일부 ETF는 거래일 부족 또는 데이터 누락으로 일부 기간 수익률이 N/A로 표시됩니다.")

                # ── 섹터 PER 스냅샷 ──────────────────────────────────────────
                st.divider()
                st.markdown("### 💰 섹터별 현재 PER (Sector P/E Snapshot)")
                st.caption("FMP sector-pe-snapshot 기준. RS 강한 섹터라도 PER이 지나치게 높으면 신중하게 접근하세요.")
                with st.spinner("섹터 PER 데이터 조회 중..."):
                    _spe_data = _fmp_sector_pe_snapshot()
                if _spe_data:
                    _spe_rows = []
                    for _sp in _spe_data:
                        _sp_pe = to_float(_sp.get("pe") or _sp.get("pe_ratio") or _sp.get("peRatio"))
                        _sp_sec = str(_sp.get("sector") or "")
                        if _sp_sec and pd.notna(_sp_pe) and _sp_pe > 0:
                            _spe_rows.append({"섹터": _sp_sec, "현재 PER": round(_sp_pe, 1)})
                    if _spe_rows:
                        _spe_df = pd.DataFrame(_spe_rows).sort_values("현재 PER", ascending=True).reset_index(drop=True)
                        _spe_styled = (
                            _spe_df.style
                            .format({"현재 PER": "{:.1f}x"})
                            .background_gradient(cmap="RdYlGn_r", subset=["현재 PER"], vmin=10, vmax=50)
                        )
                        st.dataframe(_spe_styled, use_container_width=True, hide_index=True)
                        _median_pe = _spe_df["현재 PER"].median()
                        st.caption(f"📊 섹터 중간 PER: {_median_pe:.1f}x  |  낮을수록 밸류에이션 매력 높음 (초록), 높을수록 부담 (빨강)")
                    else:
                        st.caption("PER 데이터를 파싱할 수 없습니다.")
                else:
                    st.caption("섹터 PER 데이터를 조회할 수 없습니다.")

                st.divider()
                selected_sector_etf = st.selectbox(
                    "세부 종목을 확인하고 싶은 섹터를 선택하세요",
                    options=sector_tickers,
                    index=0,
                    key="sector_holdings_select",
                )
    
                selected_row = sector_returns_df[sector_returns_df["Ticker"] == selected_sector_etf]
                selected_1m_return = (
                    pd.to_numeric(selected_row["1-Month (%)"], errors="coerce").iloc[0]
                    if not selected_row.empty
                    else np.nan
                )
    
                selected_holdings = sector_holdings_map.get(selected_sector_etf, [])
                if not selected_holdings:
                    st.warning("선택한 섹터 ETF에 대한 세부 종목 데이터가 없습니다.")
                else:
                    if pd.isna(selected_1m_return):
                        st.markdown("### 세부 종목 (섹터 1개월 수익률 데이터 없음)")
                    else:
                        st.markdown(f"선택 섹터 ETF `{selected_sector_etf}` 1-Month 수익률: **{selected_1m_return:.2f}%**")
    
                    with st.spinner(f"{selected_sector_etf} 세부 종목 1개월 수익률 계산 중..."):
                        pool_returns_df = cached_pool_monthly_returns(tuple(dict.fromkeys(selected_holdings)))
    
                    if pool_returns_df.empty or pool_returns_df["1-Month (%)"].isna().all():
                        st.warning("선택한 섹터의 세부 종목 수익률을 계산할 수 없습니다.")
                    else:
                        leaders_df = pool_returns_df.head(5).copy()
                        laggards_df = (
                            pool_returns_df.sort_values("1-Month (%)", ascending=True, na_position="last")
                            .head(5)
                            .copy()
                        )
    
                        leaders_df["1-Month (%)"] = leaders_df["1-Month (%)"].map(
                            lambda x: f"{x:.2f}%" if pd.notna(x) else "N/A"
                        )
                        laggards_df["1-Month (%)"] = laggards_df["1-Month (%)"].map(
                            lambda x: f"{x:.2f}%" if pd.notna(x) else "N/A"
                        )
    
                        leader_col, laggard_col = st.columns(2)
                        with leader_col:
                            st.markdown("### 현재 섹터의 진짜 주도주 (Alpha Leaders)")
                            st.dataframe(leaders_df, use_container_width=True, hide_index=True)
                        with laggard_col:
                            st.markdown("### 현재 섹터의 낙폭 과대주 (Laggards)")
                            st.dataframe(laggards_df, use_container_width=True, hide_index=True)
    
                        top_ticker = leaders_df.iloc[0]["Ticker"] if not leaders_df.empty else None
                        top_return = leaders_df.iloc[0]["1-Month (%)"] if not leaders_df.empty else None
                        if top_ticker and top_return:
                            st.success(
                                f"이 종목들 중 1-Month 수익률이 가장 높은 종목이 Ryan님의 2단계 전략인 "
                                f"'진짜 1등(Leader)'입니다. (현재: {top_ticker}, {top_return})"
                            )
                        else:
                            st.info("이 종목들 중 1-Month 수익률이 가장 높은 종목이 Ryan님의 2단계 전략인 '진짜 1등(Leader)'입니다.")
    
                    st.info(
                        "여기서 확인한 종목 티커를 왼쪽 사이드바에 입력한 뒤, "
                        f"「{_MAIN_NAV_OPTIONS[4]}」에서 펀더멘털과 매수 타점을 순서대로 확인하세요."
                    )
    
        except Exception as e:
            st.error("섹터 데이터를 불러오거나 계산하는 중 오류가 발생했습니다.")
            st.exception(e)
    
        st.divider()
        st.subheader("🚀 Hidden Alpha Radar (새로운 주도 테마 발굴)")
        etf_radar_universe = load_etf_universe_tickers_merged()
        st.caption(
            f"ETF Universe **{len(etf_radar_universe)}개**(`etf_universe.txt` + Google Sheets `ETF_Universe` 통합)를 사용합니다. "
            "최근 **5·10·21·63 거래일** 관점으로 1주~3개월 수익률을 일괄 계산합니다."
        )
        st.info(
            "⚙️ 추적 ETF 추가·정리·txt 통합은 위의 「🆕 ETF Universe 관리」에서 하세요. "
            "Google Sheets `ETF_Universe` 탭이 기준 소스입니다."
        )
        if st.button("지금 돈이 몰리는 미지의 ETF 찾기", key="hidden_alpha_radar_btn"):
            if not etf_radar_universe:
                st.warning(
                    f"`{_ETF_UNIVERSE_FILE}` 파일이 없거나 티커가 비어 있습니다. "
                    "프로젝트 폴더에 파일을 두고 티커를 입력해주세요."
                )
            else:
                etf_universe_sorted_tuple = tuple(sorted(set(etf_radar_universe)))
                with st.spinner("Hidden Alpha Radar: 유니버스 수익률·순위 계산 중 (포트폴리오와 동일 캐시)..."):
                    _ha_result = cached_etf_universe_rankings_full(etf_universe_sorted_tuple)
                if _ha_result is None or _ha_result.empty:
                    st.session_state["_hidden_alpha_df"] = None
                    st.warning("유니버스 랭킹을 계산하지 못했습니다. `etf_universe.txt` 티커·네트워크를 확인해주세요.")
                else:
                    st.session_state["_hidden_alpha_df"] = _ha_result

        # 결과를 session_state에서 표시 (rerun 후에도 유지)
        _ha_df = st.session_state.get("_hidden_alpha_df")
        if _ha_df is not None and not _ha_df.empty:
            ret_cols = ["1주(%)", "2주(%)", "1개월(%)", "3개월(%)"]
            tmp_show = _ha_df.copy()
            tmp_show["티커"] = tmp_show.apply(
                lambda r: f"{r['Ticker']} 🔥{leverage_badge(r['Ticker'])}" if bool(r["주도주"])
                else f"{r['Ticker']}{leverage_badge(r['Ticker'])}", axis=1
            )
            display_cols = ["순위", "티커", "1주(%)", "2주(%)", "1개월(%)", "3개월(%)"]
            display_df = tmp_show[display_cols].copy()
            fmt = {c: "{:.2f}%" for c in ret_cols}
            styled_radar = display_df.style.format(fmt, na_rep="N/A").background_gradient(
                cmap="RdYlGn", subset=ret_cols, axis=None
            )
            st.dataframe(styled_radar, use_container_width=True, hide_index=True)
            st.caption(
                "과거 구간 데이터가 부족한 ETF는 해당 칸이 N/A일 수 있으나, 유니버스 순위는 포트폴리오 화면과 동일한 1개월 기준입니다."
            )
            st.markdown(
                "💡 [Ryan's Alpha Strategy] 1주와 1개월 수익률이 모두 초록색인 상위 3~5개 ETF에 분할 투자하는 방식은 "
                f"'추세 추종(Momentum)'의 정석입니다. 단, 매수 전 「{_MAIN_NAV_OPTIONS[4]}」의 매수 타점에서 RSI가 과열(70 이상)인지 반드시 확인하세요."
            )

        st.info(
            "💡 이 레이더에 잡힌 생소한 티커를 왼쪽 사이드바에 입력하여 해당 테마를 이끄는 개별 주도주를 찾아보세요."
        )

        # ── 기능 1: RS Score 주간 변화율 (Early Signal) ───────────────────
        st.divider()
        st.subheader("🌱 Early Signal Radar — 지금 막 강해지기 시작한 섹터")
        st.caption(
            "1주 전 RS Score 대비 현재 RS Score 변화율을 계산합니다. "
            "**아직 RS가 낮지만 변화가 급격히 플러스**인 종목이 Early Entry 기회예요."
        )

        # ── 필터 설정 ──────────────────────────────────────────────────
        _uid_filter = str(st.session_state.get("user_id") or "").strip()
        filter_col1, filter_col2 = st.columns([2, 3])
        with filter_col1:
            scan_universe_choice = st.selectbox(
                "📋 스캔 대상 선택",
                options=[
                    "전체 ETF Universe",
                    "포트폴리오 보유 종목만",
                    "Watchlist 종목만",
                    "포트폴리오 + Watchlist",
                    "주요 섹터 ETF만 (SPDR XL*)",
                    "테크·반도체만",
                    "직접 입력",
                ],
                key="early_signal_universe_choice",
            )
        with filter_col2:
            if scan_universe_choice == "직접 입력":
                custom_tickers_input = st.text_input(
                    "티커 직접 입력 (쉼표 구분)",
                    placeholder="NVDA, SMH, SOXX, XLK",
                    key="early_signal_custom_tickers",
                )
            else:
                custom_tickers_input = ""

        # 스캔 대상 결정
        def _resolve_scan_universe(choice, uid, custom_input, full_universe):
            if choice == "포트폴리오 보유 종목만":
                try:
                    pf = load_portfolio()
                    return list(pf["Ticker"].dropna().astype(str).str.upper().unique()) if not pf.empty else []
                except Exception:
                    return []
            elif choice == "Watchlist 종목만":
                try:
                    wl = load_watchlist_sheet(uid)
                    return [i["ticker"] for i in wl] if wl else []
                except Exception:
                    return []
            elif choice == "포트폴리오 + Watchlist":
                tickers = []
                try:
                    pf = load_portfolio()
                    if not pf.empty:
                        tickers += list(pf["Ticker"].dropna().astype(str).str.upper().unique())
                except Exception:
                    pass
                try:
                    wl = load_watchlist_sheet(uid)
                    tickers += [i["ticker"] for i in wl] if wl else []
                except Exception:
                    pass
                return list(dict.fromkeys(tickers))
            elif choice == "주요 섹터 ETF만 (SPDR XL*)":
                return [t for t in full_universe if t.startswith("XL")]
            elif choice == "테크·반도체만":
                tech = {"VGT","SOXX","SMH","XSD","SOXQ","DRAM","XLK","FTEC","CHAT","AIQ","IGPT"}
                return [t for t in full_universe if t in tech]
            elif choice == "직접 입력":
                return [t.strip().upper() for t in custom_input.replace(",", " ").split() if t.strip()]
            else:
                return full_universe  # 전체

        scan_target = _resolve_scan_universe(
            scan_universe_choice, _uid_filter, custom_tickers_input, etf_radar_universe
        )
        st.caption(f"스캔 대상: **{len(scan_target)}개** 티커")

        if st.button("🔍 Early Signal 스캔", key="early_signal_btn", type="primary", use_container_width=True):
            if not scan_target:
                st.warning("스캔 대상 티커가 없어요. 필터를 변경하거나 포트폴리오/Watchlist에 종목을 추가해주세요.")
            else:
                with st.spinner(f"RS Score 주간 변화율 계산 중... ({len(scan_target)}개 티커)"):
                    _es_result = compute_rs_score_weekly_change(scan_target)
                if _es_result.empty:
                    st.session_state["_early_signal_df"] = None
                    st.warning("RS 변화율 데이터를 가져오지 못했습니다.")
                else:
                    st.session_state["_early_signal_df"] = _es_result

        # 결과를 session_state에서 표시 (rerun 후에도 유지)
        _es_df = st.session_state.get("_early_signal_df")
        if _es_df is not None and not _es_df.empty:
            rs_change_df = _es_df
            early_df = rs_change_df[rs_change_df["RS_Signal"] == "🌱 Early Signal"]
            surge_df = rs_change_df[rs_change_df["RS_Signal"] == "🚀 급부상"]
            weak_df = rs_change_df[rs_change_df["RS_Signal"] == "⚠️ 모멘텀 약화"]

            if not early_df.empty:
                st.success(f"🌱 **Early Signal {len(early_df)}개** — 아직 안 올랐지만 강해지기 시작한 섹터!")
                for _, row in early_df.iterrows():
                    st.markdown(
                        f"**{row['Ticker']}**{leverage_badge(row['Ticker'])} — RS {row['RS_Now']:+.1f}%p "
                        f"(1주 변화: {row['RS_Change']:+.1f}%p) "
                        f"→ Watchlist 등록 고려"
                    )

            es_col1, es_col2 = st.columns(2)
            with es_col1:
                if not surge_df.empty:
                    st.info(f"🚀 **급부상 {len(surge_df)}개** — 강하면서 더 강해지는 중")
                    for _, row in surge_df.iterrows():
                        st.markdown(f"**{row['Ticker']}**{leverage_badge(row['Ticker'])} RS {row['RS_Now']:+.1f}%p / 주간 +{row['RS_Change']:.1f}%p")
            with es_col2:
                if not weak_df.empty:
                    st.warning(f"⚠️ **모멘텀 약화 {len(weak_df)}개** — 주의 필요")
                    for _, row in weak_df.iterrows():
                        st.markdown(f"**{row['Ticker']}**{leverage_badge(row['Ticker'])} RS {row['RS_Now']:+.1f}%p / 주간 {row['RS_Change']:.1f}%p")

            with st.expander("📋 전체 RS 변화율 테이블", expanded=False):
                def _style_rs_change(val):
                    v = pd.to_numeric(val, errors="coerce")
                    if pd.isna(v): return ""
                    return "color:#16a34a;font-weight:600" if v > 2 else "color:#dc2626;font-weight:600" if v < -2 else ""
                rs_table = rs_change_df.copy()
                if "Ticker" in rs_table.columns:
                    rs_table.insert(
                        1, "레버리지",
                        rs_table["Ticker"].map(
                            lambda t: (leverage_badge(t).replace(" ⚡", "").strip() or "—")
                        ),
                    )
                styled_rs = rs_table.style.map(_style_rs_change, subset=["RS_Change"])
                st.dataframe(styled_rs, use_container_width=True, hide_index=True)

        # ── 기능 3: 섹터 꺾임 감지 ───────────────────────────────────────
        st.divider()
        st.subheader("🛡️ 섹터 꺾임 감지 — 매도 타이밍 선제 포착")
        st.caption(
            "2주 연속 RS Score 하락 + 거래량 감소 패턴을 자동으로 감지합니다. "
            "보유 종목의 섹터 ETF가 여기 뜨면 **선제적 매도를 고려**하세요."
        )

        if st.button("🔍 섹터 꺾임 스캔", key="sector_reversal_btn", use_container_width=True):
            if not scan_target:
                st.warning("스캔 대상 티커가 없어요. 위 Early Signal 필터를 먼저 설정하세요.")
            else:
                with st.spinner(f"섹터 모멘텀 반전 신호 분석 중... ({len(scan_target)}개)"):
                    reversal_alerts = detect_sector_momentum_reversal(scan_target)

            if not reversal_alerts:
                st.success("✅ 현재 감지된 꺾임 신호 없음 — 전반적으로 모멘텀 유지 중입니다.")
            else:
                st.warning(f"⚠️ **{len(reversal_alerts)}개 섹터에서 꺾임 신호 감지!**")
                for alert in reversal_alerts:
                    is_critical = "분산 매도" in alert["signal"] or "급격" in alert["signal"]
                    with st.expander(
                        f"{alert['signal']} **{alert['ticker']}**{leverage_badge(alert['ticker'])} — RS {alert['rs_now']:+.1f}%p / 주간 변화 {alert['rs_change']:+.1f}%p",
                        expanded=is_critical,
                    ):
                        st.markdown(f"**신호:** {alert['description']}")
                        detail_col1, detail_col2 = st.columns(2)
                        with detail_col1:
                            st.metric("현재 RS", f"{alert['rs_now']:+.1f}%p")
                            st.metric("1주 전 RS", f"{alert['rs_1w']:+.1f}%p")
                        with detail_col2:
                            st.metric("주간 RS 변화", f"{alert['rs_change']:+.1f}%p")
                            if alert["vol_change"] is not None:
                                st.metric("거래량 변화", f"{alert['vol_change']:+.0f}%")
                        if "분산 매도" in alert["signal"]:
                            st.error("🔴 기관 분산 매도 패턴 — 포트폴리오에 이 ETF 관련 종목이 있다면 즉시 점검하세요!")
                        else:
                            st.warning("⚠️ 모멘텀 약화 초기 신호 — 매도 준비 또는 손절 라인 설정 권장")
    
    elif main_nav == _MAIN_NAV_OPTIONS[5]:
        render_sync_button(
            "sync_tab_stock",
            [cached_evaluate_kpis_snapshot.clear, cached_etf_holdings_universe_str.clear,
             cached_build_etf_holdings_performance_pairs.clear, cached_timing_price_history.clear,
             fetch_company_overview.clear, fetch_price_history_by_period.clear,
             _fmp_profile.clear, _fmp_ratios.clear, _fmp_key_metrics.clear, _fmp_cashflow.clear],
            "종목별 재무·차트·회사정보 캐시를 비우고 최신 데이터를 받습니다.",
        )
        st.subheader(_MAIN_NAV_OPTIONS[5])
        st.caption(
            "사이드바의 분석 티커 기준입니다. **상단**에서 펀더멘털·KPI(또는 ETF 건전성)를 확인한 뒤, **하단**에서 RSI·이동평균으로 매수 타점을 점검하세요."
        )
        st.markdown(f"**분석 티커:** `{selected_ticker}`")

        # ── 회사 기본 정보 ────────────────────────────────────────────────
        try:
            with st.spinner(f"{selected_ticker} 기본 정보 불러오는 중..."):
                co = fetch_company_overview(str(selected_ticker).strip().upper())
        except Exception as _co_err:
            co = {}
            st.warning(f"회사 기본정보 조회 실패: {_co_err}")

        if co:
            name_str = co.get("name", selected_ticker)
            website = co.get("website", "")
            is_etf_co = co.get("is_etf", False)
            etf_badge = " 🏷️ ETF" if is_etf_co else ""
            st.markdown(
                f"## {name_str}{etf_badge}"
                + (f"  [{website.replace('https://','').replace('http://','')[:35]}]({website})" if website else "")
            )
            info_c1, info_c2, info_c3, info_c4 = st.columns(4)
            with info_c1:
                sector_display = co.get("sector", "N/A")
                sector_en = co.get("sector_en", "")
                st.metric("섹터", sector_display)
                if sector_en and sector_en != sector_display:
                    st.caption(sector_en)
            with info_c2:
                industry_display = co.get("industry", "N/A")
                industry_en = co.get("industry_en", "")
                st.metric("산업", industry_display[:18] if industry_display else "N/A")
                if industry_en and industry_en != industry_display:
                    st.caption(industry_en[:25])
            with info_c3:
                mc = co.get("market_cap")
                st.metric("시가총액", usd_short_str(mc) if mc else "N/A")
            with info_c4:
                ne = co.get("next_earnings")
                if ne:
                    try:
                        days_to_earn = (datetime.strptime(ne[:10], "%Y-%m-%d") - datetime.now(_MARKET_ET_TZ).replace(tzinfo=None)).days
                        earn_label = f"D-{days_to_earn}일" if days_to_earn >= 0 else f"{abs(days_to_earn)}일 전"
                        st.metric("다음 실적 발표", ne[:10], delta=earn_label)
                        st.caption("※ FMP 제공 예정일. 미확인 상태일 수 있습니다.")
                    except Exception:
                        st.metric("다음 실적 발표", ne[:10])
                else:
                    st.metric("다음 실적 발표", "N/A")

            # ── 회사 소개: 항상 한글로 표시 (자동 번역) ─────────────────
            summary_en = co.get("summary_en", "")
            if summary_en:
                _sum_key = f"_co_summary_kr_{selected_ticker}"
                with st.expander("📋 회사 소개", expanded=True):
                    if st.session_state.get(_sum_key):
                        st.markdown(st.session_state[_sum_key])
                        if st.button("🔄 다시 번역", key=f"retranslate_{selected_ticker}"):
                            del st.session_state[_sum_key]
                            st.rerun()
                    else:
                        # 한글 번역본 없으면 자동 번역 실행
                        with st.spinner("회사 소개 번역 중..."):
                            try:
                                _words_list = summary_en.split()
                                _capped = " ".join(_words_list[:800]) + ("..." if len(_words_list) > 800 else "")
                                _tr_model = _GenAIModel(
                                    "gemini-2.5-flash",
                                    generation_config={"temperature": 0.0, "max_output_tokens": 8192, "thinking_budget": 0}
                                )
                                _tr_resp = _tr_model.generate_content(
                                    "다음 영문 회사 소개를 한국어로 번역하세요. "
                                    "모든 문장을 빠짐없이 번역하고, 번역문만 출력하세요.\n\n" + _capped
                                )
                                _tr_text = (_gemini_response_text_utf8_safe(_tr_resp) or "").strip()
                                if _tr_text:
                                    st.session_state[_sum_key] = _tr_text
                                    st.markdown(_tr_text)
                                else:
                                    st.markdown(summary_en)
                            except Exception:
                                st.markdown(summary_en)

        # ── 이 종목을 보유한 ETF 목록 (기본 접힘) ────────────────────────
        if not is_etf_mode:
            with st.expander("📊 이 종목을 보유한 ETF 목록", expanded=False):
                st.caption("주요 ETF 20개 중 이 종목을 보유 중인 ETF를 자동으로 찾습니다.")
                _etf_list_key = f"_etf_holding_{selected_ticker}"
                if st.session_state.get(_etf_list_key) is None:
                    try:
                        with st.spinner(f"{selected_ticker} 보유 ETF 검색 중... (약 10초)"):
                            _etf_list = find_etfs_holding_stock(selected_ticker)
                        st.session_state[_etf_list_key] = _etf_list
                    except Exception as _etf_err:
                        st.session_state[_etf_list_key] = []
                        st.warning(f"ETF 보유 목록 조회 실패: {_etf_err}")
                _etf_result = st.session_state.get(_etf_list_key, [])
                if _etf_result:
                    st.success(f"**{selected_ticker}** 를 보유한 ETF **{len(_etf_result)}개** 발견!")
                    _etf_rows = []
                    for e in _etf_result:
                        _etf_rows.append({
                            "ETF": e["etf"],
                            "보유 비중": f"{e['weight']:.2f}%" if e.get("weight") else "N/A",
                            "보유 순위": f"Top {e['rank']}" if e.get("rank") else "N/A",
                        })
                    st.dataframe(pd.DataFrame(_etf_rows), use_container_width=True, hide_index=True)
                else:
                    st.info(f"조회한 주요 ETF 20개 중 {selected_ticker}를 보유한 ETF를 찾지 못했습니다.")
                if st.button("🔄 다시 조회", key=f"re_find_etf_{selected_ticker}"):
                    st.session_state.pop(_etf_list_key, None)
                    find_etfs_holding_stock.clear()
                    st.rerun()

        st.divider()

        # ── 기간별 주가 차트 ──────────────────────────────────────────────
        st.markdown("### 📈 주가 차트")
        period_options = ["1D", "1M", "3M", "YTD", "1Y", "5Y", "MAX"]
        selected_period = st.radio(
            "기간 선택",
            period_options,
            index=4,  # 기본값 1Y
            horizontal=True,
            key="stock_chart_period",
            label_visibility="collapsed",
        )
        with st.spinner(f"{selected_ticker} {selected_period} 차트 로딩 중..."):
            period_hist = fetch_price_history_by_period(str(selected_ticker).strip().upper(), selected_period)

        if period_hist is not None and not period_hist.empty and "Close" in period_hist.columns:
            close_p = pd.to_numeric(period_hist["Close"], errors="coerce").dropna()
            if not close_p.empty:
                first_p = float(close_p.iloc[0])
                last_p = float(close_p.iloc[-1])
                change_pct = (last_p / first_p - 1) * 100 if first_p > 0 else 0
                chg_col1, chg_col2, chg_col3 = st.columns(3)
                with chg_col1:
                    st.metric("현재가", f"${last_p:.2f}")
                with chg_col2:
                    st.metric(f"{selected_period} 수익률", f"{change_pct:+.2f}%")
                with chg_col3:
                    high_p = float(close_p.max())
                    low_p = float(close_p.min())
                    st.metric("기간 고점/저점", f"${high_p:.2f} / ${low_p:.2f}")

                chart_pdf = pd.DataFrame({"Close": close_p})
                st.line_chart(chart_pdf, use_container_width=True, height=260)
        else:
            st.warning("차트 데이터를 가져오지 못했습니다.")

        st.divider()
        st.markdown("### 체력 검사 (Fundamentals)")
        st.caption("FMP 기반 KPI 점검 (Pass/Fail)")
    
        try:
            if is_etf_mode:
                st.info("📊 ETF 분석 모드입니다. 개별 기업의 재무제표 대신 펀드 건전성 지표를 제공합니다.")
    
                total_assets = to_float(selected_ticker_info.get("totalAssets"))
                expense_ratio_raw = to_float(selected_ticker_info.get("expenseRatio"))
                etf_yield_raw = to_float(selected_ticker_info.get("yield"))
    
                expense_ratio_pct = expense_ratio_raw * 100 if not pd.isna(expense_ratio_raw) else np.nan
                etf_yield_pct = etf_yield_raw * 100 if not pd.isna(etf_yield_raw) else np.nan
    
                asset_col, fee_col, yield_col = st.columns(3)
                with asset_col:
                    st.metric("순자산 규모 (AUM)", usd_short_str(total_assets))
                with fee_col:
                    fee_delta = "✅ Pass (<0.5%)" if (pd.notna(expense_ratio_pct) and expense_ratio_pct < 0.5) else "🟡 Warning (>=0.5%)"
                    if pd.isna(expense_ratio_pct):
                        fee_delta = "N/A"
                    st.metric("운용 보수", pct_points_str(expense_ratio_pct), delta=fee_delta)
                with yield_col:
                    st.metric("배당 수익률", pct_points_str(etf_yield_pct))
    
                st.success("✅ [ETF 패스] 분산 투자된 펀드이므로 개별 재무 건전성 킬 스위치를 면제합니다.")
    
                with st.spinner(f"{selected_ticker} 보유 종목(Top Holdings) 불러오는 중..."):
                    holdings_universe = cached_etf_holdings_universe_str(selected_ticker)
                    if holdings_universe is None or holdings_universe.empty:
                        holdings_top10 = pd.DataFrame(columns=["Ticker", "Weight(%)"])
                        etf_perf_df = pd.DataFrame()
                    else:
                        holdings_top10 = holdings_universe.head(10).reset_index(drop=True)
                        perf_key = _etf_holdings_perf_cache_key(holdings_top10)
                        etf_perf_df = cached_build_etf_holdings_performance_pairs(perf_key)
    
                if holdings_universe is None or holdings_universe.empty:
                    st.info("해당 ETF의 세부 종목 데이터를 야후 파이낸스에서 불러올 수 없습니다.")
                elif etf_perf_df.empty:
                    st.markdown("### 보유 종목 TOP 10 (Top Holdings)")
                    st.dataframe(holdings_top10, use_container_width=True, hide_index=True)
                    st.warning("보유 종목 가격·수익률 데이터를 불러오지 못했습니다. 위 표는 티커·비중만 표시합니다.")
                else:
                    order = holdings_top10["Ticker"].tolist()
                    etf_perf_df = etf_perf_df.set_index("Ticker").reindex(order).reset_index()
    
                    return_cols = ["1개월(%)", "3개월(%)", "6개월(%)", "12개월(%)"]
                    styled_top10 = (
                        etf_perf_df.style.format(
                            {
                                "현재가": "${:,.2f}",
                                "1개월(%)": "{:.2f}%",
                                "3개월(%)": "{:.2f}%",
                                "6개월(%)": "{:.2f}%",
                                "12개월(%)": "{:.2f}%",
                                "비중(%)": "{:.2f}%",
                            },
                            na_rep="N/A",
                        )
                        .background_gradient(cmap="RdYlGn", subset=return_cols, axis=0)
                    )
    
                    st.divider()
                    st.markdown("### 보유 종목 TOP 10 (Top Holdings)")
                    st.dataframe(styled_top10, use_container_width=True, hide_index=True)
            else:
                with st.spinner(f"{selected_ticker} 데이터 불러오는 중..."):
                    kpi_df, _c_pass, _c_fail, _c_nodata, margin_context = cached_evaluate_kpis_snapshot(
                        selected_ticker
                    )
                # 캐시된 카운트 무시 — kpi_df에서 직접 계산 (캐시 형식 불일치 방지)
                _pp = kpi_df["Pass"].astype(str)
                pass_count   = int((_pp.str.contains("Pass",  case=False) & ~_pp.str.contains("Fail", case=False)).sum())
                fail_count   = int(_pp.str.contains("Fail",   case=False).sum())
                nodata_count = int((_pp.str.contains("N/A|No Data", case=False) & ~_pp.str.contains("Fail|Pass", case=False)).sum())

                c1, c2, c3, c4 = st.columns(4)
                total_kpis = len(kpi_df)
                c1.metric("총 KPI", total_kpis)
                c2.metric("Pass", pass_count)
                c3.metric("Fail", fail_count)
                c4.metric("No Data", nodata_count)
    
                with st.container():
                    final_core_pass = bool(margin_context.get("core_fcf_pass"))
                    if final_core_pass:
                        st.success("✅ [최종 합격] 필수 재무 건전성(현금창출력)이 검증된 종목입니다.")
                    else:
                        st.error("🚨 [최종 불합격] 잉여현금흐름 적자 기업. 투자를 권장하지 않습니다.")
    
                st.divider()
                st.subheader(f"{selected_ticker} KPI 대시보드")

                # ── 투자 스타일 점수 (KPI 위) ─────────────────────────────
                st.markdown("#### 🎯 투자 스타일 적합도")
                st.caption("CAN SLIM · 가치투자 · 장기 우량주 3가지 관점에서 이 종목을 평가합니다.")
                with st.spinner("투자 스타일 점수 계산 중..."):
                    try:
                        _style = calculate_style_scores(
                            str(selected_ticker).strip().upper(),
                            margin_context,
                            kpi_df,
                        )
                        _sc1, _sc2, _sc3 = st.columns(3)
                        for _col, _key, _label, _desc in [
                            (_sc1, "canslim",  "📈 CAN SLIM",    "고성장 모멘텀"),
                            (_sc2, "value",    "💰 가치투자",    "저평가 발굴"),
                            (_sc3, "quality",  "🏆 장기 우량주", "퀄리티 투자"),
                        ]:
                            _d = _style[_key]
                            with _col:
                                st.markdown(
                                    f"<div style='background:#1e293b;border-radius:12px;padding:16px;"
                                    f"border-top:4px solid {_d['color']};text-align:center;'>"
                                    f"<div style='font-size:14px;color:#94a3b8;'>{_label}</div>"
                                    f"<div style='font-size:36px;font-weight:800;color:{_d['color']};'>{_d['score']}</div>"
                                    f"<div style='font-size:13px;color:#cbd5e1;'>/ 100점</div>"
                                    f"<div style='font-size:15px;margin-top:4px;'>{_d['grade']}</div>"
                                    f"<div style='font-size:11px;color:#64748b;margin-top:2px;'>{_desc}</div>"
                                    f"</div>",
                                    unsafe_allow_html=True,
                                )

                        # 주도 스타일 배너
                        _dom = _style["dominant"]
                        _dom_score = _style["scores"][_dom]
                        st.markdown(
                            f"<div style='background:#0f172a;border:1px solid #334155;"
                            f"border-radius:8px;padding:10px 16px;margin-top:12px;'>"
                            f"<span style='color:#94a3b8;'>💡 이 종목의 주도 스타일: </span>"
                            f"<strong style='color:#f1f5f9;font-size:16px;'>{_dom}</strong>"
                            f"<span style='color:#64748b;font-size:13px;'> ({_dom_score}점)</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                        # 세부 점수 expander
                        with st.expander("📊 세부 점수 보기", expanded=False):
                            _det_c1, _det_c2, _det_c3 = st.columns(3)
                            for _dc, _key, _label in [
                                (_det_c1, "canslim",  "📈 CAN SLIM"),
                                (_det_c2, "value",    "💰 가치투자"),
                                (_det_c3, "quality",  "🏆 장기 우량주"),
                            ]:
                                with _dc:
                                    st.markdown(f"**{_label}**")
                                    for _k, _v in _style[_key]["detail"].items():
                                        _kname = _k.replace("_", " ")
                                        st.caption(f"{_kname}: {_v}")

                    except Exception as _se:
                        st.warning(f"스타일 점수 계산 오류: {_se}")

                st.divider()
    
                category_order = [
                    "수익성 (Profitability)",
                    "건전성 (Financial Strength)",
                    "밸류에이션 (Valuation)",
                    "모멘텀 (Momentum)",
                ]
    
                for category in category_order:
                    st.markdown(f"### {category}")
                    cat_df = kpi_df[kpi_df["Category"] == category].reset_index(drop=True)

                    if category == "모멘텀 (Momentum)":
                        # 모멘텀은 값이 길어서 카드 형태로 별도 표시
                        for _, row in cat_df.iterrows():
                            _pass_str = str(row["Pass"])
                            _is_pass = "Pass" in _pass_str and "Fail" not in _pass_str
                            _is_fail = "Fail" in _pass_str
                            _is_nodata = "No Data" in _pass_str
                            _card_color = "#16a34a" if _is_pass else ("#dc2626" if _is_fail else "#94a3b8")
                            _pass_label = "✅ Pass" if _is_pass else ("❌ Fail" if _is_fail else "⚫ 데이터 없음")
                            _val = str(row["Value"])
                            _parts = [p.strip() for p in _val.split("/") if p.strip()]
                            _html = (
                                f"<div style='background:#1e293b;border-radius:10px;padding:14px 16px;"
                                f"border-left:4px solid {_card_color};margin:6px 0;'>"
                                f"<div style='color:{_card_color};font-weight:700;font-size:15px;margin-bottom:8px;'>"
                                f"{_pass_label} — {row['Rule']}</div>"
                            )
                            for _p in _parts:
                                _html += f"<div style='font-size:14px;color:#e2e8f0;margin:2px 0;'>• {_p}</div>"
                            _html += "</div>"
                            st.markdown(_html, unsafe_allow_html=True)
                    else:
                        for idx in range(0, len(cat_df), 2):
                            cols = st.columns(2)
                            for j in range(2):
                                row_idx = idx + j
                                if row_idx >= len(cat_df):
                                    continue
                                row = cat_df.iloc[row_idx]
                                with cols[j]:
                                    st.metric(label=row["KPI"], value=row["Value"], delta=row["Rule"])
                                    st.markdown(f"판정: {row['Pass']}")

                    if category == "밸류에이션 (Valuation)":
                        _fpe  = margin_context.get("forward_pe")
                        _tpe  = margin_context.get("trailing_pe")
                        _pb   = margin_context.get("price_to_book")
                        _ev   = margin_context.get("ev_to_ebitda")
                        _evs  = margin_context.get("ev_to_sales")
                        _evf  = margin_context.get("ev_to_fcf")
                        _iv   = margin_context.get("intrinsic_value")
                        _mos  = margin_context.get("margin_of_safety")
                        _cp   = margin_context.get("current_price")

                        # Valuation 지표는 위 카드에서 이미 표시됨 — 중복 제거

                        # Graham 적정주가 — 가치주 조건 만족 시에만 표시
                        # 조건: EPS > 0, 성장률 0~20%, P/E < 25 (성장주·적자기업 제외)
                        _eps_val    = margin_context.get("trailing_eps")
                        _growth_val = margin_context.get("growth_percent")
                        _pe_val     = _tpe if pd.notna(_tpe) else _fpe

                        _is_value_stock = (
                            pd.notna(_eps_val) and _eps_val > 0
                            and pd.notna(_growth_val) and 0 < _growth_val < 20
                            and (pd.isna(_pe_val) or _pe_val < 25)
                        )

                        if _is_value_stock and pd.notna(_iv) and pd.notna(_mos):
                            st.divider()
                            st.caption(
                                "📌 **Graham 안전마진** — 저PER·저성장 가치주 전용 지표입니다. "
                                "성장주(P/E ≥ 25 또는 성장률 ≥ 20%)에는 표시되지 않습니다."
                            )
                            intrinsic_col, mos_col = st.columns(2)
                            with intrinsic_col:
                                st.metric(
                                    "적정 주가 (Graham)",
                                    f"${num_str(_iv)}",
                                    delta=(
                                        f"EPS {num_str(_eps_val)} / "
                                        f"성장률 {pct_points_str(_growth_val)}"
                                    ),
                                )
                            with mos_col:
                                _mos_color = "normal" if _mos >= 20 else "inverse"
                                st.metric(
                                    "안전마진 (Margin of Safety %)",
                                    pct_points_str(_mos),
                                    delta=f"현재가 ${num_str(_cp)}",
                                    delta_color=_mos_color,
                                )
                            if _mos < 0:
                                st.warning("⚠️ 현재가가 Graham 적정가보다 높습니다. 고평가 구간일 수 있습니다.")
                            elif _mos >= 20:
                                st.success(f"✅ 안전마진 {_mos:.1f}% — Graham 기준 매력적인 가격대입니다.")

                    st.divider()

    
                # ── Altman Z-Score + Piotroski F-Score ──────────────────────
                with st.expander("🏦 재무 건전성 점수 (Altman Z / Piotroski F)", expanded=True):
                    st.caption("FMP `/financial-scores` 기반. Altman Z는 파산 위험, Piotroski F는 재무 종합 점수(0~9).")
                    with st.spinner(f"{selected_ticker} 재무 건전성 점수 조회 중..."):
                        _fs = _fmp_financial_scores(str(selected_ticker).strip().upper())
                    if _fs:
                        _az = to_float(_fs.get("altmanZScore"))
                        _ps = to_float(_fs.get("piotroskiScore"))
                        _az_col, _ps_col = st.columns(2)
                        with _az_col:
                            if pd.notna(_az):
                                if _az >= 3.0:
                                    _az_label = "✅ 안전 구간 (≥3.0)"
                                    _az_color = "#16a34a"
                                elif _az >= 1.81:
                                    _az_label = "⚠️ 회색 구간 (1.81~3.0)"
                                    _az_color = "#d97706"
                                else:
                                    _az_label = "🚨 위험 구간 (<1.81)"
                                    _az_color = "#dc2626"
                                st.markdown(
                                    f"<div style='background:#1e293b;border-radius:10px;padding:14px;"
                                    f"border-left:4px solid {_az_color};'>"
                                    f"<div style='color:#94a3b8;font-size:12px;'>Altman Z-Score</div>"
                                    f"<div style='color:{_az_color};font-size:26px;font-weight:900;'>{_az:.2f}</div>"
                                    f"<div style='color:#e2e8f0;font-size:13px;margin-top:4px;'>{_az_label}</div>"
                                    f"<div style='color:#64748b;font-size:11px;margin-top:6px;'>"
                                    f"≥3.0 안전 · 1.81~3.0 주의 · &lt;1.81 파산위험</div>"
                                    f"</div>",
                                    unsafe_allow_html=True,
                                )
                            else:
                                st.metric("Altman Z-Score", "N/A")
                        with _ps_col:
                            if pd.notna(_ps):
                                _ps_int = int(_ps)
                                if _ps_int >= 7:
                                    _ps_label = "✅ 강한 재무 (7~9)"
                                    _ps_color = "#16a34a"
                                elif _ps_int >= 4:
                                    _ps_label = "🟡 중립 (4~6)"
                                    _ps_color = "#d97706"
                                else:
                                    _ps_label = "🚨 약한 재무 (0~3)"
                                    _ps_color = "#dc2626"
                                _ps_bar = "█" * _ps_int + "░" * (9 - _ps_int)
                                st.markdown(
                                    f"<div style='background:#1e293b;border-radius:10px;padding:14px;"
                                    f"border-left:4px solid {_ps_color};'>"
                                    f"<div style='color:#94a3b8;font-size:12px;'>Piotroski F-Score</div>"
                                    f"<div style='color:{_ps_color};font-size:26px;font-weight:900;'>{_ps_int} / 9</div>"
                                    f"<div style='color:{_ps_color};font-size:14px;font-family:monospace;'>{_ps_bar}</div>"
                                    f"<div style='color:#e2e8f0;font-size:13px;margin-top:4px;'>{_ps_label}</div>"
                                    f"</div>",
                                    unsafe_allow_html=True,
                                )
                            else:
                                st.metric("Piotroski F-Score", "N/A")
                    else:
                        st.caption("재무 건전성 데이터를 조회할 수 없습니다.")

                with st.expander("원본 KPI 테이블 보기"):
                    st.dataframe(
                        kpi_df[["Category", "KPI", "Value", "Rule", "Pass"]],
                        use_container_width=True,
                        hide_index=True,
                    )
    
        except Exception as e:
            st.error("펀더멘털(체력 검사) 데이터를 불러오거나 계산하는 중 오류가 발생했습니다.")
            st.exception(e)

        # ── 어닝콜 트랜스크립트 AI 요약 ───────────────────────────────────
        st.divider()
        st.markdown("### 📞 어닝콜 트랜스크립트 AI 요약")
        st.caption("직전 분기 실적발표 원문 → Gemini 3줄 요약 (가이던스·경영진 톤·주요 리스크)")
        _tc_key = f"_transcript_summary_{selected_ticker}"
        _tc_raw_key = f"_transcript_raw_{selected_ticker}"
        _tc_col1, _tc_col2 = st.columns([1, 1])
        with _tc_col1:
            if st.button("🤖 트랜스크립트 AI 요약 실행", key=f"tc_btn_{selected_ticker}", use_container_width=True):
                with st.spinner("어닝콜 원문 조회 중..."):
                    _tc_data = _fmp_earnings_transcript(str(selected_ticker).strip().upper())
                st.session_state[_tc_raw_key] = _tc_data
                if _tc_data and _tc_data.get("content"):
                    _tc_text = str(_tc_data["content"])[:6000]
                    _tc_q = _tc_data.get("quarter", "")
                    _tc_y = _tc_data.get("year", "")
                    _tc_prompt = (
                        f"{selected_ticker} {_tc_y}년 Q{_tc_q} 어닝콜 트랜스크립트를 분석하세요.\n\n"
                        "다음 3가지를 각각 2~3문장으로 요약하세요:\n"
                        "1) 📈 가이던스 (상향/하향/유지 여부, 구체 수치)\n"
                        "2) 🎙️ 경영진 톤 (자신감/우려/방어적 등 분위기)\n"
                        "3) ⚠️ 주요 리스크 (경영진이 언급한 위험 요인)\n\n"
                        "한국어로 답변하세요.\n\n"
                        f"[트랜스크립트 원문]\n{_tc_text}"
                    )
                    with st.spinner("Gemini 요약 중..."):
                        try:
                            _tc_model = _GenAIModel(
                                "gemini-2.5-flash",
                                generation_config={"temperature": 0.1, "max_output_tokens": 4096, "thinking_budget": 0},
                            )
                            _tc_resp = _tc_model.generate_content(_tc_prompt)
                            _tc_summary = (_gemini_response_text_utf8_safe(_tc_resp) or "").strip()
                            st.session_state[_tc_key] = {
                                "summary": _tc_summary,
                                "quarter": _tc_q,
                                "year": _tc_y,
                            }
                        except Exception as _tc_err:
                            st.error(f"Gemini 요약 실패: {_tc_err}")
                else:
                    st.warning("어닝콜 트랜스크립트 데이터를 찾을 수 없습니다.")
        with _tc_col2:
            if st.session_state.get(_tc_key):
                _tc_sum = st.session_state[_tc_key]
                st.caption(f"📅 {_tc_sum.get('year')}년 Q{_tc_sum.get('quarter')} 어닝콜")
        if st.session_state.get(_tc_key) and st.session_state[_tc_key].get("summary"):
            st.markdown(st.session_state[_tc_key]["summary"])
            _tc_raw = st.session_state.get(_tc_raw_key, {})
            if _tc_raw and _tc_raw.get("content"):
                with st.expander("📄 원문 보기 (영문)", expanded=False):
                    st.text(str(_tc_raw["content"])[:3000] + ("..." if len(str(_tc_raw.get("content",""))) > 3000 else ""))

        st.divider()
        st.markdown("### 📊 기술적 분석 (Technical Analysis)")
        st.caption("RSI · MACD · 볼린저밴드 · 거래량으로 진입 구간 점검")
        # 기술적 분석 sync는 탭 상단 버튼과 통합됨

        try:
            with st.spinner(f"{selected_ticker} 타이밍 데이터를 불러오는 중..."):
                timing_history = cached_timing_price_history(str(selected_ticker).strip())

            if timing_history is None or timing_history.empty or "Close" not in timing_history.columns:
                st.warning("가격 데이터를 불러오지 못했습니다. 티커를 확인해주세요.")
            else:
                close = pd.to_numeric(timing_history["Close"], errors="coerce")
                volume = pd.to_numeric(timing_history.get("Volume", pd.Series(dtype=float)), errors="coerce")
                current_price = float(close.dropna().iloc[-1]) if not close.dropna().empty else np.nan
                high_52w = float(close.dropna().max())
                low_52w = float(close.dropna().min())

                ma20 = close.rolling(window=20, min_periods=20).mean()
                ma50 = close.rolling(window=50, min_periods=50).mean()
                ma200 = close.rolling(window=200, min_periods=200).mean()
                rsi_series = calculate_rsi(close, window=14)
                current_rsi = float(rsi_series.dropna().iloc[-1]) if not rsi_series.dropna().empty else np.nan
                current_ma20 = float(ma20.dropna().iloc[-1]) if not ma20.dropna().empty else np.nan
                current_ma50 = float(ma50.dropna().iloc[-1]) if not ma50.dropna().empty else np.nan
                current_ma200 = float(ma200.dropna().iloc[-1]) if not ma200.dropna().empty else np.nan

                # MACD
                macd_line, signal_line, histogram = calculate_macd(close.dropna())
                current_macd = float(macd_line.iloc[-1]) if not macd_line.empty else np.nan
                current_signal = float(signal_line.iloc[-1]) if not signal_line.empty else np.nan
                current_hist = float(histogram.iloc[-1]) if not histogram.empty else np.nan
                macd_bullish = pd.notna(current_hist) and current_hist > 0

                # 볼린저밴드
                bb_upper, bb_mid, bb_lower = calculate_bollinger_bands(close)
                current_bb_upper = float(bb_upper.dropna().iloc[-1]) if not bb_upper.dropna().empty else np.nan
                current_bb_lower = float(bb_lower.dropna().iloc[-1]) if not bb_lower.dropna().empty else np.nan

                # 52주 위치
                pct_from_high = ((current_price / high_52w) - 1) * 100 if pd.notna(current_price) and high_52w > 0 else np.nan
                pct_from_low = ((current_price / low_52w) - 1) * 100 if pd.notna(current_price) and low_52w > 0 else np.nan

                # 거래량 급증
                vol_surge = np.nan
                if not volume.dropna().empty and len(volume.dropna()) >= 21:
                    recent_vol = float(volume.dropna().tail(5).mean())
                    baseline_vol = float(volume.dropna().tail(21).mean())
                    vol_surge = recent_vol / baseline_vol if baseline_vol > 0 else np.nan

                # 눌림목 포착
                is_buy_on_dip = (
                    pd.notna(current_price) and pd.notna(current_ma200)
                    and current_price > current_ma200
                    and pd.notna(current_rsi) and current_rsi < 50
                    and (
                        (pd.notna(current_ma20) and current_price <= current_ma20 * 1.02)
                        or (pd.notna(current_ma50) and current_price <= current_ma50 * 1.02)
                    )
                )

                # ── 지표 요약 메트릭 ──────────────────────────────────────
                m1, m2, m3, m4, m5 = st.columns(5)
                with m1:
                    rsi_delta = "과매수🚨" if pd.notna(current_rsi) and current_rsi >= 70 else ("조정구간✅" if pd.notna(current_rsi) and current_rsi < 50 else "중립🟡")
                    st.metric("RSI (14)", f"{current_rsi:.1f}" if pd.notna(current_rsi) else "N/A", delta=rsi_delta)
                with m2:
                    macd_delta = "상승전환🟢" if macd_bullish else "하락전환🔴"
                    st.metric("MACD 히스토그램", f"{current_hist:+.3f}" if pd.notna(current_hist) else "N/A", delta=macd_delta)
                with m3:
                    ma200_delta = "200일선 위✅" if pd.notna(current_price) and pd.notna(current_ma200) and current_price > current_ma200 else "200일선 아래❌"
                    st.metric("200일선", f"${current_ma200:.2f}" if pd.notna(current_ma200) else "N/A", delta=ma200_delta)
                with m4:
                    st.metric("52주 고점 대비", f"{pct_from_high:+.1f}%" if pd.notna(pct_from_high) else "N/A",
                              delta="신고가 근접🔥" if pd.notna(pct_from_high) and pct_from_high >= -5 else "조정구간" if pd.notna(pct_from_high) and pct_from_high <= -20 else "")
                with m5:
                    vol_str = f"{vol_surge:.1f}x" if pd.notna(vol_surge) else "N/A"
                    vol_delta = "거래량 급증🔥" if pd.notna(vol_surge) and vol_surge >= 1.5 else ("평균 이하" if pd.notna(vol_surge) and vol_surge < 0.8 else "")
                    st.metric("거래량 (5일/21일)", vol_str, delta=vol_delta)

                # RSI 종합 판정
                if pd.isna(current_rsi):
                    st.warning("RSI 계산에 필요한 데이터가 부족합니다.")
                elif current_rsi >= 70:
                    st.error("🚨 과매수 구간 — 추격 매수 금지. 인내심을 가지세요.")
                elif current_rsi >= 50:
                    st.warning("🟡 중립 구간 — 관망. 조정을 기다리세요.")
                else:
                    st.success("✅ 조정 구간 — 분할 매수 고려.")

                if is_buy_on_dip:
                    st.warning("🎯 [눌림목 포착] 장기 우상향 중인 종목의 단기 조정 구간입니다. 매수를 검토하세요!")

                # ── 매수 타이밍 게이트: "지금 사도 되나" 종합 판정 ──────────────
                # RSI 과매수 + 신고가 부근(과열) 여부를 합쳐 추격 매수를 한 박자 늦춘다.
                # 좋은 종목이라도 '나쁜 가격(고점)'에 사는 실수를 줄이는 장치.
                _bg_overheated = (
                    pd.notna(current_rsi) and current_rsi >= 70
                    and pd.notna(pct_from_high) and pct_from_high >= -3
                )
                _bg_hot_rsi = pd.notna(current_rsi) and current_rsi >= 75
                _bg_uptrend = (
                    pd.notna(current_price) and pd.notna(current_ma200)
                    and current_price > current_ma200
                )
                if _bg_overheated or _bg_hot_rsi:
                    _reason_bits = []
                    if pd.notna(current_rsi):
                        _reason_bits.append(f"RSI {current_rsi:.0f}(과매수)")
                    if pd.notna(pct_from_high) and pct_from_high >= -3:
                        _reason_bits.append("52주 신고가 부근")
                    if pd.notna(vol_surge) and vol_surge >= 1.5:
                        _reason_bits.append(f"거래량 {vol_surge:.1f}x 급증")
                    st.error(
                        "⛔ **지금은 매수 보류 (단기 과열)** — "
                        + ", ".join(_reason_bits)
                        + ".\n\n추세 자체는 살아있을 수 있으나 **지금 진입하면 고점 매수 위험**이 큽니다. "
                        "RSI가 60 아래로 식거나 20일선/50일선 눌림목까지 기다렸다가 분할 매수하는 편이 유리합니다."
                    )
                elif is_buy_on_dip:
                    st.success("🟢 **매수 타이밍 양호** — 우상향 추세 + 눌림목 구간. 분할 매수 고려 가능 구간입니다.")
                elif _bg_uptrend and pd.notna(current_rsi) and current_rsi < 60:
                    st.info("🟢 **진입 가능 구간** — 추세 유지 + 과열 아님. 다만 분할 매수로 리스크를 나누세요.")

                # 볼린저밴드 위치 알림
                if pd.notna(current_price) and pd.notna(current_bb_lower) and pd.notna(current_bb_upper):
                    if current_price <= current_bb_lower:
                        st.success(f"📊 볼린저밴드 하단 터치 (${current_bb_lower:.2f}) — 단기 반등 가능성.")
                    elif current_price >= current_bb_upper:
                        st.error(f"📊 볼린저밴드 상단 터치 (${current_bb_upper:.2f}) — 단기 과열 주의.")

                # ── 차트 탭 ───────────────────────────────────────────────
                chart_tab1, chart_tab2, chart_tab3 = st.tabs(["📈 가격 + 이평선 + 볼린저밴드", "📊 MACD", "📦 거래량"])

                with chart_tab1:
                    chart_df = pd.DataFrame({
                        "Close": close,
                        "MA20": ma20,
                        "MA50": ma50,
                        "MA200": ma200,
                        "BB_Upper": bb_upper,
                        "BB_Lower": bb_lower,
                    }).dropna(how="all")
                    if not chart_df.empty:
                        st.line_chart(chart_df[["Close", "MA20", "MA50", "MA200", "BB_Upper", "BB_Lower"]], use_container_width=True)
                        st.caption("파란선=Close, MA20/50/200, BB_Upper/Lower (볼린저밴드)")
                    else:
                        st.warning("차트 데이터 부족")

                with chart_tab2:
                    macd_df = pd.DataFrame({
                        "MACD": macd_line,
                        "Signal": signal_line,
                        "Histogram": histogram,
                    }).dropna(how="all")
                    if not macd_df.empty:
                        st.line_chart(macd_df[["MACD", "Signal"]], use_container_width=True)
                        st.bar_chart(macd_df[["Histogram"]], use_container_width=True)
                        st.caption("MACD가 Signal 위로 올라오면 상승 전환 신호. Histogram이 양수(+)면 상승 모멘텀.")
                    else:
                        st.warning("MACD 데이터 부족")

                with chart_tab3:
                    if not volume.dropna().empty:
                        vol_df = pd.DataFrame({"Volume": volume}).dropna()
                        st.bar_chart(vol_df, use_container_width=True)
                        st.caption("거래량 급증 + 가격 상승 = 강한 신호. 거래량 감소 + 가격 상승 = 약한 신호.")
                    else:
                        st.warning("거래량 데이터 없음")

        except Exception as e:
            st.error("매수 타점 데이터를 불러오거나 계산하는 중 오류가 발생했습니다.")
            st.exception(e)

        # ── Earnings Surprise 히스토리 ────────────────────────────────────
        if not is_etf_mode:
            st.divider()
            st.markdown("### 📅 Earnings Surprise 히스토리")
            st.caption("최근 분기별 EPS 예상 대비 실제 Beat/Miss 현황입니다.")
            try:
                with st.spinner("어닝 데이터 불러오는 중..."):
                    earn_df = cached_earnings_history(str(selected_ticker).strip().upper())

                if earn_df.empty:
                    st.info("어닝 히스토리 데이터를 가져오지 못했습니다.")
                else:
                    earn_df.columns = [str(c).strip() for c in earn_df.columns]

                    # ── Beat/Miss 판정 ──────────────────────────────────────
                    # 방법 1: "어닝 서프라이즈" 컬럼이 이미 있으면 바로 사용
                    _surp_col = next((c for c in earn_df.columns if "서프라이즈" in c or "surprise" in c.lower()), None)
                    if _surp_col:
                        # "+1.2%" 형태 → 수치 파싱
                        _sv = pd.to_numeric(
                            earn_df[_surp_col].astype(str).str.replace(r"[%+]","",regex=True),
                            errors="coerce"
                        )
                        earn_df["판정"] = _sv.apply(
                            lambda x: "✅ Beat" if pd.notna(x) and x > 0 else ("❌ Miss" if pd.notna(x) and x < 0 else "—")
                        )
                    else:
                        # 방법 2: EPS 예상/실제 컬럼에서 직접 계산
                        est_col = next((c for c in earn_df.columns
                            if "estimate" in c.lower() or "eps 예상" in c.lower() or "예상" in c.lower()), None)
                        act_col = next((c for c in earn_df.columns
                            if "actual" in c.lower() or "reported" in c.lower()
                               or "eps 실제" in c.lower() or "실제" in c.lower()), None)
                        if est_col and act_col:
                            _en = pd.to_numeric(earn_df[est_col].astype(str).str.replace(r"[$,]","",regex=True), errors="coerce")
                            _an = pd.to_numeric(earn_df[act_col].astype(str).str.replace(r"[$,]","",regex=True), errors="coerce")
                            _diff = ((_an - _en) / _en.abs() * 100).round(1)
                            earn_df["어닝 서프라이즈"] = _diff.apply(
                                lambda x: f"{x:+.1f}%" if pd.notna(x) else "N/A"
                            )
                            earn_df["판정"] = _diff.apply(
                                lambda x: "✅ Beat" if pd.notna(x) and x > 0 else ("❌ Miss" if pd.notna(x) and x < 0 else "—")
                            )

                    # ── Beat 비율 요약 ──────────────────────────────────────
                    if "판정" in earn_df.columns:
                        beat_count  = (earn_df["판정"] == "✅ Beat").sum()
                        total_count = len(earn_df[earn_df["판정"] != "—"])
                        if total_count > 0:
                            beat_rate = beat_count / total_count * 100
                            msg = f"어닝 Beat 비율: {beat_rate:.0f}% ({beat_count}/{total_count}분기)"
                            if beat_rate >= 75:   st.success(f"🏆 {msg} — 실적 퀄리티 우수")
                            elif beat_rate >= 50: st.warning(f"🟡 {msg} — 보통")
                            else:                 st.error(f"🔴 {msg} — 실적 부진")

                    # ── 색상 스타일 적용 ────────────────────────────────────
                    def _style_surp(val):
                        v = pd.to_numeric(str(val).replace("%","").replace("+",""), errors="coerce")
                        if pd.isna(v): return ""
                        return "color:#16a34a;font-weight:600" if v > 0 else "color:#dc2626;font-weight:600"

                    _style_cols = [c for c in earn_df.columns if "서프라이즈" in c or "surprise" in c.lower()]
                    if _style_cols:
                        st.dataframe(earn_df.style.map(_style_surp, subset=_style_cols), use_container_width=True, hide_index=True)
                    else:
                        st.dataframe(earn_df, use_container_width=True, hide_index=True)
            except Exception as _ee:
                st.warning(f"어닝 히스토리 로드 오류: {_ee}")

            # ── 기관 보유 비중 ────────────────────────────────────────────
            st.divider()
            st.markdown("### 🏦 기관 투자자 보유 현황")
            st.caption("상위 기관의 보유 비중. 기관 보유 비중이 높고 증가 추세이면 스마트머니 유입 신호입니다.")
            try:
                _tk_inst = str(selected_ticker).strip().upper()
                _k_inst  = _fmp_key()
                _shown   = False

                with st.spinner("애널리스트 데이터 불러오는 중..."):
                    if _k_inst:
                        # 1) ratings-snapshot 시도
                        try:
                            _r1 = requests.get(
                                f"{_FMP_BASE}/ratings-snapshot?symbol={_tk_inst}&apikey={_k_inst}",
                                timeout=_FMP_TIMEOUT
                            )
                            if _r1.status_code == 200:
                                _rd1 = _r1.json()
                                _ri  = _rd1[0] if isinstance(_rd1, list) and _rd1 else (
                                       _rd1 if isinstance(_rd1, dict) else {})
                                if _ri:
                                    # FMP ratings-snapshot 실제 필드명 모두 시도
                                    _sb  = int(to_float(_ri.get("strongBuy")  or _ri.get("ratingDetailsStrongBuyCount") or _ri.get("ratingStrongBuy")  or 0) or 0)
                                    _b   = int(to_float(_ri.get("buy")        or _ri.get("ratingDetailsBuyCount")       or _ri.get("ratingBuy")        or 0) or 0)
                                    _h   = int(to_float(_ri.get("hold")       or _ri.get("ratingDetailsHoldCount")      or _ri.get("ratingHold")       or 0) or 0)
                                    _s   = int(to_float(_ri.get("sell")       or _ri.get("ratingDetailsSellCount")      or _ri.get("ratingSell")       or 0) or 0)
                                    _ss  = int(to_float(_ri.get("strongSell") or _ri.get("ratingDetailsStrongSellCount")or _ri.get("ratingStrongSell") or 0) or 0)
                                    _tot = _sb + _b + _h + _s + _ss
                                    if _tot > 0:
                                        _buy_pct = round((_sb + _b) / _tot * 100, 1)
                                        ic1, ic2, ic3 = st.columns(3)
                                        ic1.metric("매수 의견 비율", f"{_buy_pct:.0f}%", f"Strong Buy {_sb} + Buy {_b}명")
                                        ic2.metric("Hold", f"{_h}명")
                                        ic3.metric("Sell / Strong Sell", f"{_s+_ss}명", f"총 {_tot}명")
                                        _rlabel = (_ri.get("ratingRecommendation") or _ri.get("rating") or
                                                   _ri.get("ratingScore") or "")
                                        if _rlabel:
                                            _col = "#16a34a" if _buy_pct >= 60 else ("#f59e0b" if _buy_pct >= 40 else "#dc2626")
                                            st.markdown(
                                                f"<div style='background:#1e293b;border-radius:8px;padding:10px 16px;"
                                                f"border-left:4px solid {_col};margin-top:8px;'>"
                                                f"<span style='color:{_col};font-weight:700;'>📊 종합 의견: {_rlabel}</span></div>",
                                                unsafe_allow_html=True)
                                        _shown = True
                        except Exception:
                            pass

                        # 2) grades-consensus fallback
                        if not _shown:
                            try:
                                _r2 = requests.get(
                                    f"{_FMP_BASE}/grades-consensus?symbol={_tk_inst}&apikey={_k_inst}",
                                    timeout=_FMP_TIMEOUT
                                )
                                if _r2.status_code == 200:
                                    _rd2 = _r2.json()
                                    _gc  = _rd2[0] if isinstance(_rd2, list) and _rd2 else (
                                           _rd2 if isinstance(_rd2, dict) else {})
                                    if _gc:
                                        _sb2 = int(to_float(_gc.get("strongBuy")  or 0) or 0)
                                        _b2  = int(to_float(_gc.get("buy")        or 0) or 0)
                                        _h2  = int(to_float(_gc.get("hold")       or 0) or 0)
                                        _s2  = int(to_float(_gc.get("sell")       or 0) or 0)
                                        _ss2 = int(to_float(_gc.get("strongSell") or 0) or 0)
                                        _tot2 = _sb2+_b2+_h2+_s2+_ss2
                                        if _tot2 > 0:
                                            _bp2 = round((_sb2+_b2)/_tot2*100, 1)
                                            ic1, ic2, ic3 = st.columns(3)
                                            ic1.metric("매수 의견 비율", f"{_bp2:.0f}%", f"Strong Buy {_sb2} + Buy {_b2}명")
                                            ic2.metric("Hold", f"{_h2}명")
                                            ic3.metric("Sell / Strong Sell", f"{_s2+_ss2}명", f"총 {_tot2}명")
                                            _shown = True
                            except Exception:
                                pass

                if not _shown:
                    # FMP API에서 데이터 없음 — 직접 응답 내용 디버그
                    st.info("애널리스트 컨센서스 데이터를 가져오지 못했습니다. (FMP API 응답 없음)")
            except Exception as _ie:
                st.warning(f"데이터 로드 오류: {_ie}")



            # ── 인사이더 트레이딩 ─────────────────────────────────────────
            st.divider()
            st.markdown("### 🕵️ 인사이더 트레이딩")
            st.caption("CEO·CFO 등 내부자의 실제 매수/매도 거래. 대량 매수는 강한 내부 확신 신호입니다.")
            try:
                with st.spinner("인사이더 거래 데이터 불러오는 중..."):
                    _tk_ins = str(selected_ticker).strip().upper()
                    insider_df = fetch_insider_trading(_tk_ins)
                    insider_stats = fetch_insider_statistics(_tk_ins)

                # 통계 요약 먼저 표시
                if insider_stats or not insider_df.empty:
                    purchases = to_float(insider_stats.get("total_purchases") or insider_stats.get("acquired")) if insider_stats else None
                    sales     = to_float(insider_stats.get("total_sales")     or insider_stats.get("disposed"))  if insider_stats else None
                    ins_c1, ins_c2, ins_c3 = st.columns(3)
                    with ins_c1:
                        p_n = int(purchases) if purchases is not None and pd.notna(purchases) else 0
                        st.metric("내부자 매수 건수", f"{p_n}건")
                    with ins_c2:
                        s_n = int(sales) if sales is not None and pd.notna(sales) else 0
                        st.metric("내부자 매도 건수", f"{s_n}건")
                    with ins_c3:
                        p_v = float(purchases) if purchases is not None and pd.notna(purchases) else 0.0
                        s_v = float(sales)     if sales     is not None and pd.notna(sales)     else 0.0
                        if p_v > 0 or s_v > 0:
                            signal = "🟢 매수 우세" if p_v > s_v else ("🔴 매도 우세" if s_v > p_v else "⚪ 중립")
                        elif not insider_df.empty and "거래유형" in insider_df.columns:
                            buy_cnt  = insider_df["거래유형"].str.contains("매수", na=False).sum()
                            sell_cnt = insider_df["거래유형"].str.contains("매도", na=False).sum()
                            signal   = "🟢 매수 우세" if buy_cnt > sell_cnt else ("🔴 매도 우세" if sell_cnt > buy_cnt else "⚪ 중립")
                        else:
                            signal = "집계 중"
                        st.metric("내부자 방향성", signal)

                if insider_df.empty:
                    st.info("인사이더 트레이딩 상세 내역이 없습니다.")
                else:
                    st.dataframe(insider_df, use_container_width=True, hide_index=True)
            except Exception as _ins_e:
                st.warning(f"인사이더 트레이딩 로드 오류: {_ins_e}")

            # ── 애널리스트 목표주가 ──────────────────────────────────────────
            st.divider()
            st.markdown("### 🎯 애널리스트 목표주가")
            st.caption("월가 애널리스트들의 목표주가 컨센서스. 현재가 대비 상승여력을 확인합니다.")
            try:
                with st.spinner("애널리스트 데이터 불러오는 중..."):
                    analyst_data = fetch_analyst_price_targets(str(selected_ticker).strip().upper())
                if not analyst_data:
                    st.info("애널리스트 목표주가 데이터를 가져오지 못했습니다.")
                else:
                    cur_price = to_float(
                        selected_ticker_info.get("price") or
                        selected_ticker_info.get("currentPrice") or
                        selected_ticker_info.get("regularMarketPrice") or
                        selected_ticker_info.get("lastPrice")
                    )
                    tgt_mean = analyst_data.get("target_mean")
                    tgt_high = analyst_data.get("target_high")
                    tgt_low = analyst_data.get("target_low")
                    tgt_med = analyst_data.get("target_median")

                    at_c1, at_c2, at_c3, at_c4 = st.columns(4)
                    with at_c1:
                        upside = f"{((tgt_mean / cur_price - 1) * 100):+.1f}%" if tgt_mean and cur_price else "N/A"
                        st.metric("평균 목표가", f"${tgt_mean:.2f}" if tgt_mean else "N/A", delta=upside)
                    with at_c2:
                        st.metric("중간값 목표가", f"${tgt_med:.2f}" if tgt_med else "N/A")
                    with at_c3:
                        st.metric("최고 목표가", f"${tgt_high:.2f}" if tgt_high else "N/A")
                    with at_c4:
                        st.metric("최저 목표가", f"${tgt_low:.2f}" if tgt_low else "N/A")

                    recent = analyst_data.get("recent", [])
                    if recent:
                        st.markdown("**최근 애널리스트 추천**")
                        st.dataframe(pd.DataFrame(recent), use_container_width=True, hide_index=True)
            except Exception as _at_e:
                st.warning(f"애널리스트 데이터 로드 오류: {_at_e}")

            # ── 상원/하원 의원 거래 ──────────────────────────────────────────
            st.divider()
            st.markdown("### 🏛️ 상원/하원 의원 거래")
            st.caption("미국 의회 의원들의 주식 거래 공시. 정책 방향 선행 지표로 활용됩니다.")
            try:
                with st.spinner("의회 거래 데이터 불러오는 중..."):
                    congress_df = fetch_senate_house_trading(str(selected_ticker).strip().upper())
                if congress_df.empty:
                    st.info("의회 거래 데이터를 가져오지 못했습니다. (FMP Starter 플랜 제공 범위 외)")
                else:
                    buy_cnt = congress_df[congress_df["거래유형"].str.upper().str.contains("PURCHASE|BUY|매수", na=False)]
                    sell_cnt = congress_df[congress_df["거래유형"].str.upper().str.contains("SALE|SELL|매도", na=False)]
                    cg_c1, cg_c2 = st.columns(2)
                    with cg_c1:
                        st.metric("의회 매수 건수", f"{len(buy_cnt)}건")
                    with cg_c2:
                        st.metric("의회 매도 건수", f"{len(sell_cnt)}건")
                    st.dataframe(congress_df, use_container_width=True, hide_index=True)
            except Exception as _cg_e:
                st.warning(f"의회 거래 데이터 로드 오류: {_cg_e}")

        # ── AI 종합 진단 ─────────────────────────────────────────────────
        st.divider()
        st.markdown("### 🤖 AI 종합 진단")
        st.caption("단순 정리가 아닌 시그널 충돌 분석·핵심 투자포인트·구체적 액션을 제시합니다. 결과는 탭 이동 후에도 유지됩니다.")

        # session_state로 결과 유지 (종목 바뀌면 초기화)
        _diag_sk = f"ai_diag_{str(selected_ticker).strip().upper()}"
        _diag_tk = "ai_diag_last_ticker"
        if st.session_state.get(_diag_tk) != str(selected_ticker).strip().upper():
            st.session_state[_diag_tk] = str(selected_ticker).strip().upper()
            st.session_state.pop(_diag_sk, None)

        if st.button("🤖 AI 종합 진단 실행", key="ai_diagnosis_btn", type="primary", use_container_width=True):
            try:
                # 이미 위에서 계산된 값들을 최대한 재사용
                _diag_timing = cached_timing_price_history(str(selected_ticker).strip())
                _diag_close = pd.to_numeric(_diag_timing["Close"], errors="coerce").dropna() if not _diag_timing.empty else pd.Series(dtype=float)
                _diag_rsi = float(calculate_rsi(_diag_close).dropna().iloc[-1]) if len(_diag_close) > 14 else np.nan
                _diag_ma200 = float(_diag_close.rolling(200, min_periods=150).mean().iloc[-1]) if len(_diag_close) >= 150 else np.nan
                _diag_ma50 = float(_diag_close.rolling(50, min_periods=50).mean().iloc[-1]) if len(_diag_close) >= 50 else np.nan
                _diag_price = float(_diag_close.iloc[-1]) if not _diag_close.empty else np.nan
                _diag_52w_high = float(_diag_close.max()) if not _diag_close.empty else np.nan
                _diag_pct_from_high = ((_diag_price / _diag_52w_high) - 1) * 100 if pd.notna(_diag_price) and pd.notna(_diag_52w_high) and _diag_52w_high > 0 else np.nan

                # MACD - 명시적으로 양수/음수 체크
                _diag_macd_line, _diag_sig_line, _diag_hist_series = calculate_macd(_diag_close)
                _diag_hist_val = float(_diag_hist_series.iloc[-1]) if not _diag_hist_series.empty else np.nan
                _macd_direction = "상승모멘텀 (양수)" if (pd.notna(_diag_hist_val) and _diag_hist_val > 0) else "하락모멘텀 (음수)" if (pd.notna(_diag_hist_val) and _diag_hist_val < 0) else "중립"

                # 볼린저밴드
                _bb_upper, _bb_mid, _bb_lower = calculate_bollinger_bands(_diag_close)
                _bb_upper_val = float(_bb_upper.dropna().iloc[-1]) if not _bb_upper.dropna().empty else np.nan
                _bb_lower_val = float(_bb_lower.dropna().iloc[-1]) if not _bb_lower.dropna().empty else np.nan
                _bb_position = "하단 터치 (반등 가능)" if pd.notna(_diag_price) and pd.notna(_bb_lower_val) and _diag_price <= _bb_lower_val * 1.01 else \
                               "상단 터치 (과열)" if pd.notna(_diag_price) and pd.notna(_bb_upper_val) and _diag_price >= _bb_upper_val * 0.99 else "밴드 내부"

                # KPI 요약
                _kpi_summary = ""
                _kpi_details = ""
                try:
                    _kpi_df, _pass_n, _fail_n, _, _margin = cached_evaluate_kpis_snapshot(str(selected_ticker).strip().upper())
                    _kpi_summary = f"KPI: {_pass_n}개 통과 / {_fail_n}개 실패"
                    _safety = _margin.get('margin_of_safety')
                    _intrinsic = _margin.get('intrinsic_value')
                    _kpi_details = (
                        f"안전마진: {_safety:.1f}% / 적정주가: ${_intrinsic:.2f}"
                        if pd.notna(_safety) and pd.notna(_intrinsic) else "밸류에이션 데이터 없음"
                    )
                    # 실패한 KPI 목록
                    _fail_items = _kpi_df[_kpi_df["Pass"].str.contains("Fail", na=False)]["KPI"].tolist() if not _kpi_df.empty else []
                    if _fail_items:
                        _kpi_details += f" / 실패 항목: {', '.join(_fail_items[:3])}"
                except Exception:
                    _kpi_summary = "KPI 데이터 없음"
                    _kpi_details = ""

                # 기관 보유
                _inst_pct = "데이터 없음"
                try:
                    _inst_df = cached_institutional_holders(str(selected_ticker).strip().upper())
                    if not _inst_df.empty:
                        _pct_col = next((c for c in _inst_df.columns if "%" in c or "pct" in c.lower() or "held" in c.lower()), None)
                        if _pct_col:
                            _total = _inst_df[_pct_col].sum()
                            _total_pct = _total * 100 if _total <= 1 else _total
                            _inst_pct = f"{_total_pct:.1f}%"
                except Exception:
                    pass

                # ── 기술적 지표 추가 수집 ──────────────────────────────────────
                # KPI 데이터 재사용
                _diag_pass_items, _diag_fail_items = [], []
                _diag_fpe = _diag_tpe = _diag_pb = _diag_ev = _diag_evs = _diag_evf = np.nan
                _diag_mos = _diag_iv = np.nan
                _diag_cs = _diag_vs = _diag_qs = 0
                _diag_cs_det = _diag_vs_det = _diag_qs_det = _diag_dom = ""
                try:
                    _dk, _, _, _, _dm = cached_evaluate_kpis_snapshot(str(selected_ticker).strip().upper())
                    _dpp = _dk["Pass"].astype(str)
                    _diag_pass_items = _dk[_dpp.str.contains("Pass", case=False) & ~_dpp.str.contains("Fail", case=False)]["KPI"].tolist()
                    _diag_fail_items = _dk[_dpp.str.contains("Fail", case=False)]["KPI"].tolist()
                    _diag_fpe  = _dm.get("forward_pe");    _diag_tpe = _dm.get("trailing_pe")
                    _diag_pb   = _dm.get("price_to_book"); _diag_ev  = _dm.get("ev_to_ebitda")
                    _diag_evs  = _dm.get("ev_to_sales");   _diag_evf = _dm.get("ev_to_fcf")
                    _diag_mos  = _dm.get("margin_of_safety"); _diag_iv = _dm.get("intrinsic_value")
                    _diag_style = calculate_style_scores(str(selected_ticker).strip().upper(), _dm, _dk)
                    _diag_cs  = _diag_style["canslim"]["score"]
                    _diag_vs  = _diag_style["value"]["score"]
                    _diag_qs  = _diag_style["quality"]["score"]
                    _diag_dom = _diag_style["dominant"]
                    _diag_cs_det = " / ".join(f"{k}:{v}" for k,v in _diag_style["canslim"]["detail"].items())
                    _diag_vs_det = " / ".join(f"{k}:{v}" for k,v in _diag_style["value"]["detail"].items())
                    _diag_qs_det = " / ".join(f"{k}:{v}" for k,v in _diag_style["quality"]["detail"].items())
                except Exception:
                    pass

                # 인사이더 방향
                _diag_ins_buy = _diag_ins_sell = 0
                try:
                    _dis = fetch_insider_statistics(str(selected_ticker).strip().upper())
                    _diag_ins_buy  = int(to_float(_dis.get("total_purchases") or _dis.get("acquired") or 0) or 0)
                    _diag_ins_sell = int(to_float(_dis.get("total_sales")     or _dis.get("disposed")  or 0) or 0)
                except Exception:
                    pass

                # 어닝 Beat 비율
                _diag_beat_rate = _diag_beat_n = _diag_earn_n = 0
                _diag_earn_rows = []
                try:
                    _dea = cached_earnings_history(str(selected_ticker).strip().upper())
                    if not _dea.empty and "판정" in _dea.columns:
                        _diag_beat_n  = int((_dea["판정"] == "✅ Beat").sum())
                        _diag_earn_n  = len(_dea[_dea["판정"] != "—"])
                        _diag_beat_rate = int(_diag_beat_n / _diag_earn_n * 100) if _diag_earn_n else 0
                    _sc_e = next((c for c in _dea.columns if "서프라이즈" in c), None)
                    _ac_e = next((c for c in _dea.columns if "eps 실제" in c.lower() or "실제" in c.lower()), None)
                    for _, _er in _dea.head(4).iterrows():
                        _rs = str(_er.get("분기",""))
                        if _ac_e: _rs += f" EPS={_er.get(_ac_e,'')}"
                        if _sc_e: _rs += f" 서프라이즈={_er.get(_sc_e,'')}"
                        _diag_earn_rows.append(_rs)
                except Exception:
                    pass

                # 애널리스트 목표가 & 매수비율
                _diag_tgt_mean = _diag_tgt_high = _diag_tgt_low = None
                _diag_buy_pct = _diag_rat_label = _diag_rat_tot = ""
                _diag_rec_list = []
                try:
                    _dan = fetch_analyst_price_targets(str(selected_ticker).strip().upper())
                    if _dan:
                        _diag_tgt_mean = _dan.get("target_mean")
                        _diag_tgt_high = _dan.get("target_high")
                        _diag_tgt_low  = _dan.get("target_low")
                        _diag_rec_list = [f"{r.get('기관','')}: {r.get('등급','')}" for r in _dan.get("recent",[])[:3]]
                    _k_r = _fmp_key()
                    if _k_r:
                        _r_r = requests.get(f"{_FMP_BASE}/ratings-snapshot?symbol={str(selected_ticker).strip().upper()}&apikey={_k_r}", timeout=_FMP_TIMEOUT)
                        if _r_r.status_code == 200:
                            _rrd = _r_r.json()
                            _rri = _rrd[0] if isinstance(_rrd, list) and _rrd else {}
                            if _rri:
                                _rsb=int(to_float(_rri.get("strongBuy") or 0) or 0); _rb=int(to_float(_rri.get("buy") or 0) or 0)
                                _rh=int(to_float(_rri.get("hold") or 0) or 0); _rs=int(to_float(_rri.get("sell") or 0) or 0); _rss=int(to_float(_rri.get("strongSell") or 0) or 0)
                                _rt=_rsb+_rb+_rh+_rs+_rss
                                if _rt > 0:
                                    _diag_buy_pct   = f"{round((_rsb+_rb)/_rt*100,1):.0f}%"
                                    _diag_rat_label = _rri.get("ratingRecommendation") or _rri.get("rating") or ""
                                    _diag_rat_tot   = f"{_rsb+_rb}/{_rt}명"
                except Exception:
                    pass

                # 의회 거래
                _diag_cg_buy = _diag_cg_sell = 0
                try:
                    _dcg = fetch_senate_house_trading(str(selected_ticker).strip().upper())
                    if not _dcg.empty:
                        _diag_cg_buy  = len(_dcg[_dcg["거래유형"].str.upper().str.contains("PURCHASE|BUY|매수", na=False)])
                        _diag_cg_sell = len(_dcg[_dcg["거래유형"].str.upper().str.contains("SALE|SELL|매도", na=False)])
                except Exception:
                    pass

                def _nv(v, fmt=".2f", pre="", suf=""):
                    return f"{pre}{v:{fmt}}{suf}" if pd.notna(v) else "N/A"

                # ── 프롬프트: 정리 말고 판단을 요청 ──────────────────────────
                _diag_prompt = f"""당신은 15년 경력의 헤지펀드 포트폴리오 매니저입니다.
아래는 {selected_ticker}에 대한 실제 정량 데이터입니다.

당신의 역할:
- 데이터를 단순 나열하지 마세요. 이미 사용자가 알고 있습니다.
- 시그널들이 서로 일치하는지, 충돌하는지를 분석하세요.
- 충돌하는 시그널이 있다면 어느 쪽을 더 신뢰해야 하는지 근거를 대세요.
- 최종적으로 지금 이 종목을 어떻게 다뤄야 하는지 명확한 의견을 내세요.

--- 데이터 ---
[기술적]
현재가 {_nv(_diag_price,".2f","$")} | 52주 고점 대비 {_nv(_diag_pct_from_high,"+.1f","","%")}
RSI {_nv(_diag_rsi,".1f")} | MACD 히스토그램 {_nv(_diag_hist_val,"+.4f")} ({_macd_direction}) | 볼린저밴드 {_bb_position}
MA50 {_nv(_diag_ma50,".2f","$")} / MA200 {_nv(_diag_ma200,".2f","$")} → {"정배열" if pd.notna(_diag_price) and pd.notna(_diag_ma50) and pd.notna(_diag_ma200) and _diag_price>_diag_ma50>_diag_ma200 else "역배열" if pd.notna(_diag_price) and pd.notna(_diag_ma200) and _diag_price<_diag_ma200 else "부분배열"}

[투자 스타일 점수]
CAN SLIM {_diag_cs}점 | 가치투자 {_diag_vs}점 | 장기우량주 {_diag_qs}점 → 주도스타일: {_diag_dom}
CAN SLIM 세부: {_diag_cs_det}
가치투자 세부: {_diag_vs_det}
장기우량주 세부: {_diag_qs_det}

[체력검사 KPI]
통과: {', '.join(_diag_pass_items) if _diag_pass_items else '없음'}
실패: {', '.join(_diag_fail_items) if _diag_fail_items else '없음'}
밸류에이션: Forward P/E={_nv(_diag_fpe)} Trailing P/E={_nv(_diag_tpe)} P/B={_nv(_diag_pb)} EV/EBITDA={_nv(_diag_ev)} EV/Sales={_nv(_diag_evs)} EV/FCF={_nv(_diag_evf)}
Graham 안전마진={_nv(_diag_mos,".1f","","%")} | {_kpi_details}

[Earnings Surprise 최근 {_diag_earn_n}분기]
Beat {_diag_beat_n}회 ({_diag_beat_rate}%) | {" / ".join(_diag_earn_rows)}

[수급 시그널]
인사이더: 매수 {_diag_ins_buy}건 vs 매도 {_diag_ins_sell}건 → {"매수 우세" if _diag_ins_buy > _diag_ins_sell else "매도 우세" if _diag_ins_sell > _diag_ins_buy else "중립"}
애널리스트 매수의견: {_diag_buy_pct} ({_diag_rat_tot}) | 종합의견: {_diag_rat_label}
애널리스트 목표가: 평균 {_nv(_diag_tgt_mean,".2f","$")} / 최고 {_nv(_diag_tgt_high,".2f","$")} / 최저 {_nv(_diag_tgt_low,".2f","$")}
최근 추천: {" | ".join(_diag_rec_list) if _diag_rec_list else "N/A"}
의회 거래: 매수 {_diag_cg_buy}건 vs 매도 {_diag_cg_sell}건

--- 작성 지침 ---
아래 형식으로 작성하되, 각 섹션에서 데이터를 나열하지 말고 해석과 판단만 쓰세요.

## 🎯 종합 판단: [매수 고려 / 관망 / 매도 고려]
한 줄 핵심 근거 (가장 결정적인 이유 하나)

**📡 시그널 충돌 분석:**
기술적·펀더멘털·수급 시그널이 같은 방향을 가리키는지, 엇갈리는지 분석하세요.
예: "기술적으로는 강하지만 인사이더가 대규모 매도 중" 같은 충돌을 짚고, 어느 쪽에 무게를 두어야 하는지 판단하세요.

**💡 이 종목의 핵심 투자 포인트:**
지금 이 종목을 매수할 만한 가장 강력한 이유 (데이터 기반). 없다면 "현재 없음"이라고 쓰세요.

**💣 가장 큰 리스크:**
지금 이 종목을 피해야 할 가장 강력한 이유 (데이터 기반). 숫자로 근거를 대세요.

**🎯 구체적 액션 플랜:**
지금 당장 어떻게 해야 하는지. 매수라면 어떤 조건이 추가로 확인되어야 하는지. 관망이라면 어떤 시그널이 나올 때 진입/회피할지. 막연한 표현 금지.

*본 분석은 참고용이며 투자 권유가 아닙니다.*"""

                with st.spinner("Gemini AI가 종합 진단 중... (약 15초 소요)"):
                    _diag_model = _GenAIModel(
                        "gemini-2.5-flash",
                        generation_config={"temperature": 0.4, "max_output_tokens": 16384, "thinking_budget": 0}
                    )
                    _diag_response = _diag_model.generate_content(_diag_prompt)
                    _diag_text = _gemini_response_text_utf8_safe(_diag_response)

                if _diag_text:
                    st.session_state[_diag_sk] = _diag_text
                else:
                    st.warning("AI 진단 결과를 받지 못했습니다.")
            except Exception as _ae:
                st.error(f"AI 진단 오류: {_ae}")

        # 저장된 결과 표시 (버튼 눌러도, 탭 이동해도 유지)
        if st.session_state.get(_diag_sk):
            _saved_diag = st.session_state[_diag_sk]
            if "매수 고려" in _saved_diag:
                st.success(_saved_diag)
            elif "매도 고려" in _saved_diag:
                st.error(_saved_diag)
            else:
                st.warning(_saved_diag)
            if st.button("🗑️ 진단 초기화", key="ai_diag_reset"):
                st.session_state.pop(_diag_sk, None)
                st.rerun()

        # ── 애널리스트 등급 변경 히스토리 ────────────────────────────────
        st.divider()
        st.markdown("### 📊 애널리스트 등급 변경 히스토리")
        st.caption("주요 기관의 상향/하향/유지 이력 (FMP grades-historical, 최근 15건)")
        with st.spinner(f"{selected_ticker} 등급 히스토리 조회 중..."):
            _rating_hist = _fmp_historical_ratings(str(selected_ticker).strip().upper())
        if _rating_hist:
            _rh_rows = []
            for _rh in _rating_hist:
                _action = str(_rh.get("action") or _rh.get("ratingChange") or "").strip()
                _action_emoji = (
                    "🟢 상향" if any(w in _action.lower() for w in ["upgrade", "initiated", "initiated at"]) else
                    "🔴 하향" if "downgrade" in _action.lower() else
                    "⚪ 유지"
                )
                _rh_rows.append({
                    "날짜": str(_rh.get("date") or "")[:10],
                    "기관": str(_rh.get("gradingCompany") or _rh.get("company") or ""),
                    "변경": _action_emoji,
                    "이전": str(_rh.get("previousGrade") or ""),
                    "현재": str(_rh.get("newGrade") or ""),
                })
            _rh_df = pd.DataFrame(_rh_rows)
            st.dataframe(_rh_df, use_container_width=True, hide_index=True)
        else:
            st.caption("등급 변경 히스토리 데이터를 찾을 수 없습니다.")

    elif main_nav == _MAIN_NAV_OPTIONS[6]:
        syn_p1, syn_p2 = st.columns([1, 3])
        with syn_p1:
            if st.button("🔄 현재 페이지 데이터 동기화", key="sync_tab_portfolio", use_container_width=True):
                tab_sync_refresh(
                    [
                        cached_portfolio_yf_close_1y.clear,
                        cached_etf_universe_rankings_full.clear,
                        cached_quote_type.clear,
                        _portfolio_sheet_all_values_cached.clear,
                        _trade_history_all_values_cached.clear,
                    ],
                    rerun_after=True,
                )
        with syn_p2:
            st.caption(
                "시세·ETF 유니버스 모멘텀 랭킹(1시간 TTL)·종목 유형(quoteType) 캐시를 비우고 레이더를 다시 계산합니다."
            )
    
        st.subheader(_MAIN_NAV_OPTIONS[6])
        st.caption(
            "Google 시트 `Quant_DB` / **Portfolios** 한 줄은 "
            "`[ID, Account, Ticker, AvgPrice, Quantity, Date_Added]` 입니다. "
            "**ID** 열에는 항상 현재 로그인 `user_id` 만 기록하고, 증권사·계좌 구분 이름은 **Account** 열에만 저장합니다."
        )

        portfolio_df = load_portfolio()
        puid = str(st.session_state.get("user_id") or "").strip()
        if st.session_state.get("_portfolio_last_sheet_error"):
            st.warning(f"Portfolios 시트: {st.session_state['_portfolio_last_sheet_error']}")

        st.markdown("### 보유 계좌별 요약")
        st.caption(f"시트에서 **ID = `{puid or '—'}`** 인 행만 표시합니다. (Account는 사용자가 지정한 계좌명입니다.)")
        if portfolio_df.empty:
            st.info("등록된 포트폴리오가 없습니다. 하단에서 종목을 추가해 주세요.")
        else:
            for acct in sorted(portfolio_df["Account"].astype(str).unique(), key=lambda x: str(x).lower()):
                sub = portfolio_df[portfolio_df["Account"] == acct].sort_values("Ticker")
                with st.expander(f"**{acct}** · {len(sub)}종목", expanded=False):
                    st.dataframe(
                        sub[["Ticker", "Purchase_Price", "Quantity"]].rename(
                            columns={"Purchase_Price": "평단가(AvgPrice)", "Quantity": "수량"}
                        ),
                        width="stretch",
                        hide_index=True,
                    )

        st.markdown("### 계좌 필터 (매도 레이더)")
        account_list = sorted(portfolio_df["Account"].dropna().astype(str).unique().tolist()) if not portfolio_df.empty else []
        selected_accounts = st.multiselect(
            "조회할 계좌를 선택하세요",
            options=account_list,
            default=account_list,
            help="선택한 계좌만 아래 매도 레이더·차트에 반영됩니다.",
        )

        filtered_portfolio_df = portfolio_df.copy()
        if selected_accounts:
            filtered_portfolio_df = filtered_portfolio_df[filtered_portfolio_df["Account"].isin(selected_accounts)].copy()

        sheet_accounts = distinct_portfolio_accounts_for_user_id(puid) if puid else []

        st.markdown("### 포트폴리오 관리")

        with st.expander("종목 추가", expanded=False):
            add_account_options = ["직접 입력"] + sheet_accounts
            selected_account_option = st.selectbox(
                "계좌명 (시트 Account 열)",
                options=add_account_options,
                index=0,
                key="portfolio_add_account_selector",
            )
            # Thesis 옵션 (폼 바깥에서 미리 로드)
            thesis_options = get_thesis_options_from_narratives(puid)
            thesis_labels = ["(Thesis 없음 - 일반 매수)"] + [o["label"] for o in thesis_options]

            with st.form("portfolio_add_form", clear_on_submit=False):
                if selected_account_option == "직접 입력":
                    custom_account_input = st.text_input(
                        "계좌명 직접 입력",
                        value="",
                        placeholder="예: robinhood, Fidelity Roth",
                        key="form_portfolio_add_custom_account",
                    )
                else:
                    custom_account_input = ""
                    st.caption(f"선택한 계좌: **{selected_account_option}**")
                new_ticker = st.text_input(
                    "종목 티커 입력",
                    value="",
                    placeholder="예: QQQ (저장 시 대문자)",
                    key="form_portfolio_add_ticker",
                ).strip().upper()
                new_purchase_price = st.number_input(
                    "평균 매수가 (추가 매수 시 해당 매수 단가)",
                    min_value=0.0,
                    value=0.0,
                    step=0.01,
                    format="%.2f",
                    key="form_portfolio_add_purchase_price",
                )
                new_quantity = st.number_input(
                    "수량 (추가 매수 수량)",
                    min_value=0.0,
                    value=0.0,
                    step=1.0,
                    format="%.4f",
                    key="form_portfolio_add_quantity",
                )
                selected_thesis_label = st.selectbox(
                    "📌 투자 Thesis (어떤 내러티브 테마에서 매수했나요?)",
                    options=thesis_labels,
                    index=0,
                    key="form_portfolio_add_thesis",
                    help="최근 내러티브에서 추출한 테마 목록입니다. 선택하면 Thesis 탭에서 추적할 수 있어요.",
                )
                submitted_add = st.form_submit_button("포트폴리오에 추가", use_container_width=True, type="primary")
                if submitted_add:
                    if not puid:
                        st.error("로그인 user_id 가 없습니다. 다시 로그인해 주세요.")
                    else:
                        account_name = (
                            (custom_account_input or "").strip()
                            if selected_account_option == "직접 입력"
                            else str(selected_account_option or "").strip()
                        )
                        if not account_name:
                            st.warning("계좌명을 선택하거나 직접 입력해주세요.")
                        elif not new_ticker:
                            st.warning("티커를 입력해주세요.")
                        else:
                            ok_p, price_v, err_p = _validate_positive_portfolio_number("매수가", new_purchase_price)
                            ok_q, qty_v, err_q = _validate_positive_portfolio_number("수량", new_quantity)
                            if not ok_p:
                                st.error(err_p)
                            elif not ok_q:
                                st.error(err_q)
                            else:
                                updated_df = portfolio_df.copy()
                                mask = (updated_df["Account"] == account_name) & (updated_df["Ticker"] == new_ticker)
                                if mask.any():
                                    idx = updated_df.index[mask][0]
                                    old_qty = float(updated_df.loc[idx, "Quantity"])
                                    old_price = float(updated_df.loc[idx, "Purchase_Price"])
                                    ok_old_q, _, err_old_q = _validate_positive_portfolio_number("기존 수량", old_qty)
                                    ok_old_p, _, err_old_p = _validate_positive_portfolio_number("기존 평단가", old_price)
                                    if not ok_old_q:
                                        st.error(f"저장된 데이터 오류: {err_old_q} [데이터 수정하기]에서 바로잡아 주세요.")
                                    elif not ok_old_p:
                                        st.error(f"저장된 데이터 오류: {err_old_p} [데이터 수정하기]에서 바로잡아 주세요.")
                                    else:
                                        new_qty_total = old_qty + qty_v
                                        new_avg = ((old_price * old_qty) + (price_v * qty_v)) / new_qty_total
                                        updated_df.loc[idx, "Quantity"] = new_qty_total
                                        updated_df.loc[idx, "Purchase_Price"] = new_avg
                                        save_portfolio(updated_df)
                                        # Trade_History에 BUY 기록
                                        _buy_date = _narrative_now_et_string()[:10]
                                        append_trade_history_row(puid, account_name, new_ticker, "BUY", qty_v, price_v, _buy_date, "추가 매수")
                                        st.success(
                                            f"{account_name} / {new_ticker}: 추가 매수를 반영했습니다. "
                                            f"합산 수량 {new_qty_total:g}, 새 평단가 {new_avg:.4f}."
                                        )
                                        st.rerun()
                                else:
                                    updated_df = pd.concat(
                                        [
                                            updated_df,
                                            pd.DataFrame(
                                                [
                                                    {
                                                        "Account": account_name,
                                                        "Ticker": new_ticker,
                                                        "Purchase_Price": price_v,
                                                        "Quantity": qty_v,
                                                    }
                                                ]
                                            ),
                                        ],
                                        ignore_index=True,
                                    )
                                    save_portfolio(updated_df)
                                    # Trade_History에 BUY 기록
                                    _buy_date = _narrative_now_et_string()[:10]
                                    append_trade_history_row(puid, account_name, new_ticker, "BUY", qty_v, price_v, _buy_date, "신규 매수")
                                    # Thesis 선택 시 Thesis 시트에도 저장
                                    if selected_thesis_label != "(Thesis 없음 - 일반 매수)":
                                        matched = next((o for o in thesis_options if o["label"] == selected_thesis_label), None)
                                        if matched:
                                            save_thesis_row(
                                                puid,
                                                new_ticker,
                                                account_name,
                                                matched["thesis_title"],
                                                matched["narrative_category"],
                                                matched["narrative_date"],
                                            )
                                    st.success(f"{account_name} / {new_ticker} 종목을 추가했습니다. (시트 ID={puid})")
                                    st.rerun()

        with st.expander("데이터 수정하기", expanded=False):
            if portfolio_df.empty:
                st.info("수정할 포트폴리오 데이터가 없습니다.")
            else:
                edit_accounts = sorted(portfolio_df["Account"].dropna().astype(str).unique().tolist())
                edit_account = st.selectbox(
                    "수정할 계좌 선택",
                    options=edit_accounts,
                    index=0,
                    key="portfolio_edit_account_selector",
                )
                edit_candidates = portfolio_df[portfolio_df["Account"] == edit_account].copy()
                edit_tickers = sorted(edit_candidates["Ticker"].dropna().astype(str).unique().tolist())
                if not edit_tickers:
                    st.info("해당 계좌에 등록된 티커가 없습니다.")
                else:
                    edit_ticker = st.selectbox(
                        "수정할 티커 선택",
                        options=edit_tickers,
                        index=0,
                        key="portfolio_edit_ticker_selector",
                    )
                    pair_key = f"{edit_account}__{edit_ticker}".replace(" ", "_")
                    row_match = portfolio_df[
                        (portfolio_df["Account"] == edit_account) & (portfolio_df["Ticker"] == edit_ticker)
                    ]
                    if row_match.empty:
                        st.warning("선택한 종목을 찾을 수 없습니다.")
                    else:
                        cur_row = row_match.iloc[0]
                        q_raw = pd.to_numeric(cur_row["Quantity"], errors="coerce")
                        p_raw = pd.to_numeric(cur_row["Purchase_Price"], errors="coerce")
                        cur_q = float(q_raw) if pd.notna(q_raw) else 0.0
                        cur_p = float(p_raw) if pd.notna(p_raw) else 0.0
                        with st.form("portfolio_edit_form", clear_on_submit=False):
                            ed_col1, ed_col2 = st.columns(2)
                            with ed_col1:
                                edit_quantity = st.number_input(
                                    "수량",
                                    min_value=0.0,
                                    value=float(max(cur_q, 0.0)),
                                    step=1.0,
                                    format="%.4f",
                                    key=f"form_portfolio_edit_qty__{pair_key}",
                                )
                            with ed_col2:
                                edit_price = st.number_input(
                                    "평단가",
                                    min_value=0.0,
                                    value=float(max(cur_p, 0.0)),
                                    step=0.01,
                                    format="%.4f",
                                    key=f"form_portfolio_edit_price__{pair_key}",
                                )
                            submitted_edit = st.form_submit_button("수정 완료", use_container_width=True, type="primary")
                            if submitted_edit:
                                ok_eq, qty_ev, err_eq = _validate_positive_portfolio_number("수량", edit_quantity)
                                ok_ep, price_ev, err_ep = _validate_positive_portfolio_number("평단가", edit_price)
                                if not ok_eq:
                                    st.error(err_eq)
                                elif not ok_ep:
                                    st.error(err_ep)
                                else:
                                    upd = portfolio_df.copy()
                                    m = (upd["Account"] == edit_account) & (upd["Ticker"] == edit_ticker)
                                    if not m.any():
                                        st.error("해당 행이 더 이상 존재하지 않습니다. 화면을 새로고침했는지 확인해 주세요.")
                                    else:
                                        ix = upd.index[m][0]
                                        upd = upd.copy()
                                        upd["Quantity"] = upd["Quantity"].astype(object)
                                        upd["Purchase_Price"] = upd["Purchase_Price"].astype(object)
                                        upd.at[ix, "Quantity"] = float(qty_ev)
                                        upd.at[ix, "Purchase_Price"] = float(price_ev)
                                        save_portfolio(upd)
                                        st.success(
                                            f"{edit_account} / {edit_ticker} 수량·평단가를 수정해 저장했습니다."
                                        )
                                        st.rerun()

        with st.expander("🗑️ 종목 삭제 (매도 기록 없이 포지션 제거)", expanded=False):
            st.caption("매도 기록 없이 포지션만 제거합니다. 실현 손익 추적이 필요하면 아래 '매도 기록'을 이용하세요.")
            if portfolio_df.empty:
                st.info("삭제할 종목이 없습니다.")
            else:
                del_col1, del_col2, del_col3 = st.columns([1.4, 1.4, 1.0])
                with del_col1:
                    del_acct_opts = sorted(portfolio_df["Account"].dropna().astype(str).unique().tolist())
                    del_account = st.selectbox(
                        "삭제할 계좌 선택",
                        options=del_acct_opts if del_acct_opts else ["(등록된 계좌 없음)"],
                        key="portfolio_delete_account_select",
                    )
                with del_col2:
                    del_cand = portfolio_df[portfolio_df["Account"] == del_account].copy() if del_acct_opts else pd.DataFrame()
                    del_ticker_opts = del_cand["Ticker"].dropna().astype(str).tolist() if not del_cand.empty else []
                    del_target = st.selectbox(
                        "삭제할 티커 선택",
                        options=del_ticker_opts if del_ticker_opts else ["(등록된 티커 없음)"],
                        key="portfolio_delete_select",
                    )
                with del_col3:
                    st.write("")
                    st.write("")
                    if st.button("선택 종목 삭제", use_container_width=True, type="primary"):
                        if del_account == "(등록된 계좌 없음)" or del_target == "(등록된 티커 없음)":
                            st.info("삭제할 계좌/종목을 선택해주세요.")
                        else:
                            uid_del = str(st.session_state.get("user_id") or "").strip()
                            ok_del, derr = delete_portfolio_sheet_row(uid_del, del_account, del_target)
                            if not ok_del:
                                st.error(derr)
                            else:
                                st.success(f"{del_account} / {del_target} 종목을 삭제했습니다.")
                            st.rerun()

        st.markdown("### 💸 매도 기록")
        with st.expander("매도 기록 (부분 매도 포함)", expanded=False):
            if portfolio_df.empty:
                st.info("포트폴리오에 종목이 없습니다.")
            else:
                sell_accounts = sorted(portfolio_df["Account"].dropna().astype(str).unique().tolist())
                sell_acct_sel = st.selectbox("계좌 선택", options=sell_accounts, key="sell_form_account_sel")
                sell_tickers_avail = (
                    portfolio_df[portfolio_df["Account"] == sell_acct_sel]["Ticker"]
                    .dropna().astype(str).tolist()
                ) if sell_acct_sel else []
                with st.form("portfolio_sell_form", clear_on_submit=False):
                    sell_ticker_sel = st.selectbox(
                        "매도할 종목",
                        options=sell_tickers_avail if sell_tickers_avail else ["(종목 없음)"],
                        key="sell_form_ticker_sel",
                    )
                    hold_row = portfolio_df[
                        (portfolio_df["Account"] == sell_acct_sel) &
                        (portfolio_df["Ticker"] == sell_ticker_sel)
                    ]
                    cur_hold_qty = float(pd.to_numeric(hold_row["Quantity"].values[0], errors="coerce") or 0) if not hold_row.empty else 0.0
                    cur_avg_price = float(pd.to_numeric(hold_row["Purchase_Price"].values[0], errors="coerce") or 0) if not hold_row.empty else 0.0
                    st.caption(f"현재 보유 수량: **{cur_hold_qty:g}주** | 평균 매수가: **${cur_avg_price:,.4f}**")
                    sell_price_input = st.number_input("매도가 (1주당 $)", min_value=0.0, value=0.0, step=0.01, format="%.4f", key="sell_form_price")
                    sell_qty_input = st.number_input(
                        f"매도 수량 (최대 {cur_hold_qty:g}주)",
                        min_value=0.0,
                        max_value=float(max(cur_hold_qty, 0.0001)),
                        value=0.0, step=1.0, format="%.4f", key="sell_form_qty",
                    )
                    sell_date_input = st.date_input("매도 날짜", value=datetime.now(pytz.timezone("US/Eastern")).date(), key="sell_form_date")
                    sell_memo_input = st.text_input("메모 (선택)", placeholder="예: 목표가 도달, 섹터 약세", key="sell_form_memo")
                    submitted_sell = st.form_submit_button("매도 기록 저장", use_container_width=True, type="primary")

                    if submitted_sell:
                        if not puid:
                            st.error("로그인 user_id가 없습니다.")
                        elif sell_ticker_sel == "(종목 없음)":
                            st.warning("종목을 선택해주세요.")
                        elif sell_price_input <= 0:
                            st.error("매도가를 입력해주세요.")
                        elif sell_qty_input <= 0:
                            st.error("매도 수량을 입력해주세요.")
                        elif sell_qty_input > cur_hold_qty + 1e-9:
                            st.error(f"매도 수량({sell_qty_input:g})이 보유 수량({cur_hold_qty:g})을 초과합니다.")
                        else:
                            sell_date_str = sell_date_input.strftime("%Y-%m-%d")
                            ok_th, err_th = append_trade_history_row(
                                puid, sell_acct_sel, sell_ticker_sel, "SELL",
                                sell_qty_input, sell_price_input, sell_date_str, sell_memo_input
                            )
                            if not ok_th:
                                st.error(f"매도 기록 저장 실패: {err_th}")
                            else:
                                _invalidate_trade_history_cache()  # 명시적 캐시 초기화
                                upd_sell = portfolio_df.copy()
                                m_sell = (upd_sell["Account"] == sell_acct_sel) & (upd_sell["Ticker"] == sell_ticker_sel)
                                if m_sell.any():
                                    ix_sell = upd_sell.index[m_sell][0]
                                    new_qty = cur_hold_qty - sell_qty_input
                                    if new_qty < 1e-9:
                                        upd_sell = upd_sell.drop(index=ix_sell).reset_index(drop=True)
                                        save_portfolio(upd_sell)
                                        realized = (sell_price_input - cur_avg_price) * sell_qty_input
                                        pnl_pct = ((sell_price_input / cur_avg_price) - 1.0) * 100.0 if cur_avg_price > 0 else 0
                                        pnl_emoji = "🟢" if realized >= 0 else "🔴"
                                        st.success(f"{pnl_emoji} {sell_acct_sel}/{sell_ticker_sel} 전량 매도. 평균단가 기준 손익: ${realized:+,.2f} ({pnl_pct:+.2f}%). 포트폴리오에서 제거됩니다.")
                                    else:
                                        upd_sell.loc[ix_sell, "Quantity"] = new_qty
                                        save_portfolio(upd_sell)
                                        realized = (sell_price_input - cur_avg_price) * sell_qty_input
                                        pnl_pct = ((sell_price_input / cur_avg_price) - 1.0) * 100.0 if cur_avg_price > 0 else 0
                                        pnl_emoji = "🟢" if realized >= 0 else "🔴"
                                        st.success(f"{pnl_emoji} {sell_acct_sel}/{sell_ticker_sel} {sell_qty_input:g}주 매도. 잔여 {new_qty:g}주 | 손익: ${realized:+,.2f} ({pnl_pct:+.2f}%)")
                                st.rerun()

        st.divider()
        if filtered_portfolio_df.empty:
            st.info("조건에 맞는 포트폴리오가 없습니다. 계좌 필터를 변경하거나 상단에서 종목을 추가해주세요.")
        else:
            st.caption(
                "ETF 종목은 `etf_universe.txt` 전체와 비교한 **1개월 모멘텀 순위**를 표시합니다. "
                "유니버스 **Top 5 밖**이면 셀 앞에 🔴가 붙습니다. (랭킹은 1시간 캐시)"
            )
            with st.spinner("기관급 포트폴리오 레이더를 계산하는 중..."):
                sell_radar_df = build_portfolio_sell_radar_df(filtered_portfolio_df)

                # 포트폴리오 스냅샷 자동 저장 (일 1회)
                _ph_uid = str(st.session_state.get("user_id") or "").strip()
                _ph_last = st.session_state.get("_portfolio_snapshot_saved_date")
                _today_str = datetime.now(_MARKET_ET_TZ).strftime("%Y-%m-%d")
                if _ph_last != _today_str and not sell_radar_df.empty:
                    _ok_snap, _ = save_portfolio_snapshot(_ph_uid, sell_radar_df)
                    if _ok_snap:
                        st.session_state["_portfolio_snapshot_saved_date"] = _today_str
                        load_portfolio_history.clear()
    
            if sell_radar_df.empty:
                st.warning("실시간 데이터를 불러오지 못했습니다. 네트워크 또는 티커를 확인해주세요.")
            else:
                total_gain_loss = pd.to_numeric(sell_radar_df["투자 손익($)"], errors="coerce").sum(min_count=1)
                total_market_value = (
                    pd.to_numeric(sell_radar_df["현재가"], errors="coerce")
                    * pd.to_numeric(sell_radar_df["수량"], errors="coerce")
                ).sum(min_count=1)
                total_cost_basis = (
                    pd.to_numeric(sell_radar_df["매수가"], errors="coerce")
                    * pd.to_numeric(sell_radar_df["수량"], errors="coerce")
                ).sum(min_count=1)
                overall_return_pct = np.nan
                if pd.notna(total_cost_basis) and total_cost_basis != 0 and pd.notna(total_market_value):
                    overall_return_pct = (float(total_market_value) / float(total_cost_basis) - 1.0) * 100.0
    
                max_dd_idx = pd.to_numeric(sell_radar_df["Drawdown(%)"], errors="coerce").idxmin()
                worst_name = "N/A"
                worst_dd = np.nan
                if pd.notna(max_dd_idx):
                    worst_row = sell_radar_df.loc[max_dd_idx]
                    worst_name = f"{worst_row['계좌']} / {worst_row['티커']}"
                    worst_dd = pd.to_numeric(worst_row["Drawdown(%)"], errors="coerce")
    
                metric_col1, metric_col2, metric_col3 = st.columns(3)
                with metric_col1:
                    st.metric("총 포트폴리오 투자 손익($)", f"${0.0 if pd.isna(total_gain_loss) else total_gain_loss:,.2f}")
                with metric_col2:
                    st.metric("전체 수익률(%)", "N/A" if pd.isna(overall_return_pct) else f"{overall_return_pct:.2f}%")
                with metric_col3:
                    if pd.isna(worst_dd):
                        st.metric("최대 하락 종목", worst_name)
                    else:
                        st.metric("최대 하락 종목", worst_name, delta=f"{worst_dd:.2f}%")
    
                st.divider()
                pie_mode = st.radio(
                    "비중 차트 기준",
                    options=["계좌별", "종목별"],
                    horizontal=True,
                    key="portfolio_pie_mode",
                )
                chart_df = sell_radar_df.copy()
                chart_df["평가액"] = pd.to_numeric(chart_df["현재가"], errors="coerce") * pd.to_numeric(
                    chart_df["수량"], errors="coerce"
                )
                chart_df = chart_df.dropna(subset=["평가액"])
                chart_df = chart_df[chart_df["평가액"] > 0]
                if chart_df.empty:
                    st.info("비중 차트를 그릴 평가액 데이터가 없습니다.")
                else:
                    group_col = "계좌" if pie_mode == "계좌별" else "티커"
                    pie_df = chart_df.groupby(group_col, as_index=False)["평가액"].sum()
                    pie_chart = (
                        alt.Chart(pie_df)
                        .mark_arc()
                        .encode(
                            theta=alt.Theta(field="평가액", type="quantitative"),
                            color=alt.Color(field=group_col, type="nominal"),
                            tooltip=[
                                alt.Tooltip(group_col, title=group_col),
                                alt.Tooltip("평가액:Q", title="평가액", format=",.2f"),
                            ],
                        )
                    )
                    st.altair_chart(pie_chart, use_container_width=True)
    
                def style_status(cell_value):
                    s = str(cell_value)
                    if "SELL" in s or "매도" in s:
                        return "color: #d62728; font-weight: 700;"
                    if "익절" in s or "WARNING" in s or "주의" in s:
                        return "color: #f59e0b; font-weight: 700;"
                    if "HOLD" in s or "보유" in s:
                        return "color: #16a34a; font-weight: 700;"
                    return "color: #6b7280;"
    
                def highlight_deep_drawdown(cell_value):
                    val = pd.to_numeric(cell_value, errors="coerce")
                    if pd.notna(val) and val <= -10:
                        return "background-color: #ffd6d6; color: #b91c1c; font-weight: 700;"
                    return ""
    
                def style_universe_rank_cell(cell_value):
                    s = str(cell_value or "")
                    if "리스트 없음" in s:
                        return "color: #dc2626; font-weight: 700;"
                    if "🔴" in s:
                        return "color: #dc2626; font-weight: 700;"
                    return ""
    
                def style_spy_alpha(val):
                    v = pd.to_numeric(val, errors="coerce")
                    if pd.isna(v):
                        return "color: #9ca3af;"
                    if v > 0:
                        return "color: #16a34a; font-weight: 600;"
                    if v < 0:
                        return "color: #dc2626; font-weight: 600;"
                    return ""

                # ── 트레일링 스톱 경보 (고점 대비 하락폭) ───────────────────
                try:
                    _ts_pct = _DEFAULT_TRAILING_STOP_PCT
                    _dd_col = pd.to_numeric(sell_radar_df.get("Drawdown(%)"), errors="coerce")
                    # 데이터 오류로 의심되는 행은 경보에서 제외 (선택 2B)
                    _err_col = sell_radar_df.get("_dd_data_error")
                    if _err_col is None:
                        _err_mask = pd.Series(False, index=sell_radar_df.index)
                    else:
                        _err_mask = _err_col.fillna(False).astype(bool)
                    _breach_mask = (_dd_col <= -_ts_pct) & (~_err_mask)
                    _breached = sell_radar_df[_breach_mask] if _dd_col is not None else None
                    if _breached is not None and not _breached.empty:
                        _msg_lines = []
                        for _, _br in _breached.iterrows():
                            _msg_lines.append(
                                f"- **{_br['티커']}** ({_br['계좌']}): 고점 대비 "
                                f"**{pd.to_numeric(_br['Drawdown(%)'], errors='coerce'):.0f}%** 하락"
                            )
                        st.warning(
                            f"🔔 **트레일링 스톱 경보** — 매수 이후 고점 대비 -{_ts_pct:.0f}% 이상 하락한 종목:\n\n"
                            + "\n".join(_msg_lines)
                            + "\n\n번 수익을 지키려면 매도 또는 일부 익절을 검토하세요. "
                            "(이미 오른 종목이라면 본전까지 토해내기 전에 차익을 확정하는 장치입니다.)"
                        )
                    # 데이터 오류 종목이 있으면 별도 안내
                    if _err_mask.any():
                        _err_tks = ", ".join(sell_radar_df[_err_mask]["티커"].astype(str).unique())
                        st.info(
                            f"ℹ️ {_err_tks} — 가격 데이터에 스플릿 미조정/이상치가 의심되어 "
                            "고점 대비 낙폭 경보에서 제외했습니다. (매수가 대비 수익률은 정상 표시)"
                        )
                except Exception:
                    pass

                # 표시용에서 내부 플래그 컬럼 제거
                _display_radar_df = sell_radar_df.drop(columns=["_dd_data_error"], errors="ignore")

                styled_sell_radar = (
                    _display_radar_df.style.format(
                        {
                            "수량": "{:,.4f}",
                            "매수가": "{:,.2f}",
                            "현재가": "{:,.2f}",
                            "투자 손익($)": "${:,.2f}",
                            "수익률(%)": "{:.2f}%",
                            "SPY Alpha(%)": "{:+.2f}%",
                            "Drawdown(%)": "{:.2f}%",
                            "200일선": "{:,.2f}",
                            "1개월 수익률": "{:.2f}%",
                            "자산 비중(%)": "{:.2f}%",
                        },
                        na_rep="N/A",
                    )
                    .background_gradient(cmap="RdYlGn", subset=["수익률(%)", "1개월 수익률"], axis=0)
                    .background_gradient(cmap="RdYlGn", subset=["투자 손익($)"], axis=0)
                    .map(highlight_deep_drawdown, subset=["Drawdown(%)"])
                    .map(style_status, subset=["상태(Status)"])
                    .map(style_spy_alpha, subset=["SPY Alpha(%)"])
                    .map(style_universe_rank_cell, subset=["유니버스 랭킹(Universe Rank)"])
                )
                st.dataframe(styled_sell_radar, use_container_width=True, hide_index=True)

                # ── Correlation Matrix ─────────────────────────────────────
                st.divider()
                st.markdown("### 🔗 보유 종목 Correlation Matrix")
                st.caption(
                    "최근 1년 일별 수익률 기준 종목 간 상관계수(-1 ~ +1). "
                    "**1.0(진한 초록)** 완전 동행 · **0(노랑)** 무관 · **-1.0(진한 빨강)** 완전 역행. "
                    "상관계수가 높은 종목끼리는 실질적으로 분산 효과가 없어요."
                )
                try:
                    corr_tickers = sell_radar_df["티커"].dropna().astype(str).unique().tolist()
                    corr_tickers = [t for t in corr_tickers if t and t != "SPY"]
                    if len(corr_tickers) < 2:
                        st.info("Correlation Matrix는 보유 종목이 2개 이상일 때 표시됩니다.")
                    else:
                        # close_df_full은 build_portfolio_sell_radar_df 내부에서만 쓰이므로
                        # 여기선 cached_portfolio_yf_close_1y 재사용
                        corr_tuple = tuple(sorted(dict.fromkeys(corr_tickers)))
                        corr_close = cached_portfolio_yf_close_1y(corr_tuple)
                        if corr_close is None or corr_close.empty:
                            st.warning("Correlation Matrix 데이터를 불러오지 못했습니다.")
                        else:
                            # 유효한 ticker만 추출
                            valid_cols = [t for t in corr_tickers if t in corr_close.columns]
                            if len(valid_cols) < 2:
                                st.warning("유효한 가격 데이터가 있는 종목이 2개 미만입니다.")
                            else:
                                price_df = corr_close[valid_cols].copy()
                                # 일별 수익률로 변환 후 상관계수 계산
                                ret_df = price_df.pct_change().dropna(how="all")
                                corr_matrix = ret_df.corr()

                                # Altair heatmap 렌더링
                                corr_reset = corr_matrix.reset_index()
                                corr_reset.columns.name = None
                                index_col = corr_reset.columns[0]
                                corr_long = (
                                    corr_reset
                                    .melt(id_vars=index_col, var_name="종목2", value_name="상관계수")
                                    .rename(columns={index_col: "종목1"})
                                )
                                corr_long["상관계수_표시"] = corr_long["상관계수"].map(
                                    lambda x: f"{x:.2f}" if pd.notna(x) else "N/A"
                                )
                                n_tickers = len(valid_cols)
                                cell_size = max(40, min(70, 500 // n_tickers))
                                heatmap = (
                                    alt.Chart(corr_long)
                                    .mark_rect()
                                    .encode(
                                        x=alt.X("종목1:N", sort=valid_cols, title=None),
                                        y=alt.Y("종목2:N", sort=valid_cols, title=None),
                                        color=alt.Color(
                                            "상관계수:Q",
                                            scale=alt.Scale(scheme="redyellowgreen", domain=[-1, 1]),
                                            title="상관계수",
                                        ),
                                        tooltip=["종목1:N", "종목2:N", "상관계수_표시:N"],
                                    )
                                    .properties(width=cell_size * n_tickers, height=cell_size * n_tickers)
                                )
                                text_layer = (
                                    alt.Chart(corr_long)
                                    .mark_text(fontSize=11, fontWeight="bold")
                                    .encode(
                                        x=alt.X("종목1:N", sort=valid_cols),
                                        y=alt.Y("종목2:N", sort=valid_cols),
                                        text="상관계수_표시:N",
                                        color=alt.condition(
                                            "datum.상관계수 > 0.5 || datum.상관계수 < -0.5",
                                            alt.value("white"),
                                            alt.value("black"),
                                        ),
                                    )
                                )
                                st.altair_chart(heatmap + text_layer, use_container_width=True)

                                # 고상관 경고 (0.85 이상)
                                high_corr_pairs = []
                                for i, t1 in enumerate(valid_cols):
                                    for t2 in valid_cols[i+1:]:
                                        val = corr_matrix.loc[t1, t2]
                                        if pd.notna(val) and abs(val) >= 0.85:
                                            high_corr_pairs.append((t1, t2, val))
                                if high_corr_pairs:
                                    pair_lines = "\n".join(
                                        f"- **{t1}** & **{t2}**: {v:.2f}"
                                        for t1, t2, v in high_corr_pairs
                                    )
                                    st.warning(
                                        "⚠️ **고상관 종목 쌍 감지** (상관계수 ≥ 0.85) — "
                                        "실질적인 분산 효과가 낮을 수 있어요:\n" + pair_lines
                                    )
                                else:
                                    st.success("✅ 고상관 종목 쌍 없음 — 포트폴리오가 적절히 분산되어 있어요.")
                except Exception as e:
                    st.warning(f"Correlation Matrix 계산 중 오류가 발생했습니다: {e}")

                # ── 포트폴리오 수익률 히스토리 ────────────────────────────
                st.divider()
                st.markdown("### 📈 포트폴리오 수익률 히스토리")
                st.caption("포트폴리오 탭 방문 시 일 1회 자동으로 스냅샷이 저장돼요. 데이터가 쌓일수록 더 의미있는 차트가 됩니다.")

                _ph_uid2 = str(st.session_state.get("user_id") or "").strip()
                ph_df = load_portfolio_history(_ph_uid2)

                if ph_df.empty or len(ph_df) < 2:
                    st.info("📊 아직 히스토리 데이터가 부족해요. 매일 이 탭을 방문하면 자동으로 기록이 쌓입니다.")
                else:
                    # 요약 메트릭
                    first_ret = float(ph_df["Return_Pct"].dropna().iloc[0]) if not ph_df["Return_Pct"].dropna().empty else 0
                    last_ret = float(ph_df["Return_Pct"].dropna().iloc[-1]) if not ph_df["Return_Pct"].dropna().empty else 0
                    max_ret = float(ph_df["Return_Pct"].dropna().max())
                    min_ret = float(ph_df["Return_Pct"].dropna().min())
                    last_alpha = float(ph_df["Alpha_Pct"].dropna().iloc[-1]) if not ph_df["Alpha_Pct"].dropna().empty else np.nan
                    days = len(ph_df)

                    ph_m1, ph_m2, ph_m3, ph_m4 = st.columns(4)
                    with ph_m1:
                        st.metric("현재 수익률", f"{last_ret:+.2f}%", delta=f"{last_ret - first_ret:+.2f}%p (시작 대비)")
                    with ph_m2:
                        st.metric("SPY Alpha", f"{last_alpha:+.2f}%p" if pd.notna(last_alpha) else "N/A")
                    with ph_m3:
                        st.metric("역대 최고", f"{max_ret:+.2f}%")
                    with ph_m4:
                        st.metric("기록 기간", f"{days}일")

                    # 수익률 곡선 차트
                    ph_chart_df = ph_df[["Date", "Return_Pct", "SPY_Pct", "Alpha_Pct"]].copy()
                    ph_chart_df = ph_chart_df.dropna(subset=["Return_Pct"])
                    ph_chart_df["Date"] = pd.to_datetime(ph_chart_df["Date"])

                    ph_long = ph_chart_df.melt(
                        id_vars="Date",
                        value_vars=[c for c in ["Return_Pct", "SPY_Pct"] if c in ph_chart_df.columns],
                        var_name="구분", value_name="수익률(%)"
                    )
                    ph_long["구분"] = ph_long["구분"].map({"Return_Pct": "내 포트폴리오", "SPY_Pct": "SPY"})

                    ph_color = alt.Scale(
                        domain=["내 포트폴리오", "SPY"],
                        range=["#3b82f6", "#f59e0b"]
                    )
                    ph_line = (
                        alt.Chart(ph_long.dropna())
                        .mark_line(strokeWidth=2)
                        .encode(
                            x=alt.X("Date:T", title="날짜"),
                            y=alt.Y("수익률(%):Q", title="수익률 (%)", scale=alt.Scale(zero=False)),
                            color=alt.Color("구분:N", scale=ph_color, title=""),
                            tooltip=[
                                alt.Tooltip("Date:T", format="%Y-%m-%d"),
                                alt.Tooltip("구분:N"),
                                alt.Tooltip("수익률(%):Q", format="+.2f"),
                            ]
                        )
                        .properties(height=280)
                        .interactive()
                    )
                    zero_line = (
                        alt.Chart(pd.DataFrame({"y": [0]}))
                        .mark_rule(color="gray", strokeDash=[4, 4], strokeWidth=1)
                        .encode(y="y:Q")
                    )
                    st.altair_chart(ph_line + zero_line, use_container_width=True)
                    st.caption("🔵 내 포트폴리오 수익률 · 🟡 SPY 1개월 수익률 | 0% 기준선 점선")

                    # Alpha 히스토리
                    if "Alpha_Pct" in ph_chart_df.columns and not ph_chart_df["Alpha_Pct"].dropna().empty:
                        with st.expander("📊 Alpha 히스토리 (포트폴리오 - SPY)", expanded=False):
                            alpha_df = ph_chart_df[["Date", "Alpha_Pct"]].dropna()
                            alpha_chart = (
                                alt.Chart(alpha_df)
                                .mark_bar()
                                .encode(
                                    x=alt.X("Date:T", title="날짜"),
                                    y=alt.Y("Alpha_Pct:Q", title="Alpha (%p)"),
                                    color=alt.condition(
                                        "datum.Alpha_Pct > 0",
                                        alt.value("#16a34a"),
                                        alt.value("#dc2626")
                                    ),
                                    tooltip=["Date:T", alt.Tooltip("Alpha_Pct:Q", format="+.2f")]
                                )
                                .properties(height=180)
                            )
                            st.altair_chart(alpha_chart, use_container_width=True)

                # ── Personal Benchmark 비교 ────────────────────────────────
                st.divider()
                st.markdown("### 📈 Personal Benchmark 비교")
                st.caption(
                    "보유 종목의 **현재 비중(자산 비중)** 기준으로 가중 수익률을 계산해 "
                    "SPY · QQQ와 동일 시작점(100)으로 비교합니다."
                )

                bench_period = st.radio(
                    "비교 기간",
                    options=["1개월", "3개월", "6개월", "1년"],
                    index=1,
                    horizontal=True,
                    key="benchmark_period_radio",
                )
                period_td_map = {"1개월": 21, "3개월": 63, "6개월": 126, "1년": 252}
                bench_lookback = period_td_map[bench_period]

                try:
                    # 포트폴리오 종목 + SPY + QQQ 한 번에 다운로드
                    bench_tickers_raw = sell_radar_df["티커"].dropna().astype(str).unique().tolist()
                    bench_tickers_raw = [t for t in bench_tickers_raw if t]
                    if not bench_tickers_raw:
                        st.info("Benchmark 비교를 위한 포트폴리오 종목이 없습니다.")
                    else:
                        all_bench_tickers = tuple(sorted(set(bench_tickers_raw + ["SPY", "QQQ"])))
                        bench_close_df = cached_portfolio_yf_close_1y(all_bench_tickers)

                        if bench_close_df is None or bench_close_df.empty:
                            st.warning("Benchmark 비교 데이터를 불러오지 못했습니다.")
                        else:
                            # 최근 bench_lookback 거래일만 슬라이싱
                            bench_close_df = bench_close_df.dropna(how="all")
                            if len(bench_close_df) > bench_lookback:
                                bench_close_df = bench_close_df.iloc[-bench_lookback:]

                            # 자산 비중 계산 (sell_radar_df의 자산 비중 컬럼 활용)
                            weight_series = pd.to_numeric(
                                sell_radar_df.set_index("티커")["자산 비중(%)"], errors="coerce"
                            ).dropna()
                            weight_series = weight_series[weight_series > 0]
                            total_w = weight_series.sum()
                            if total_w > 0:
                                weight_series = weight_series / total_w  # 합계 1로 정규화

                            # 포트폴리오 가중 일별 수익률 계산
                            valid_bench_tickers = [
                                t for t in weight_series.index
                                if t in bench_close_df.columns
                            ]
                            if not valid_bench_tickers:
                                st.warning("비중 데이터가 있는 종목의 가격 데이터를 불러오지 못했습니다.")
                            else:
                                price_df = bench_close_df[valid_bench_tickers].copy()
                                price_df = price_df.ffill().dropna(how="all")
                                ret_df = price_df.pct_change().fillna(0)

                                # 각 종목 비중으로 weighted return 계산
                                weights = weight_series.reindex(valid_bench_tickers).fillna(0)
                                weights = weights / weights.sum()
                                portfolio_daily_ret = (ret_df * weights.values).sum(axis=1)

                                # 100 기준 누적 수익률 (normalized)
                                portfolio_cum = (1 + portfolio_daily_ret).cumprod() * 100

                                # SPY, QQQ normalized
                                chart_data = pd.DataFrame({"내 포트폴리오": portfolio_cum})
                                for bm in ["SPY", "QQQ"]:
                                    if bm in bench_close_df.columns:
                                        bm_series = pd.to_numeric(
                                            bench_close_df[bm], errors="coerce"
                                        ).ffill().dropna()
                                        if len(bm_series) > bench_lookback:
                                            bm_series = bm_series.iloc[-bench_lookback:]
                                        bm_ret = bm_series.pct_change().fillna(0)
                                        bm_cum = (1 + bm_ret).cumprod() * 100
                                        bm_cum.index = portfolio_cum.index[:len(bm_cum)]
                                        chart_data[bm] = bm_cum

                                chart_data = chart_data.dropna(how="all")

                                if chart_data.empty:
                                    st.warning("차트를 그릴 데이터가 부족합니다.")
                                else:
                                    # 최종 수익률 요약
                                    final_vals = chart_data.iloc[-1]
                                    mc1, mc2, mc3 = st.columns(3)
                                    port_ret_final = float(final_vals.get("내 포트폴리오", 100)) - 100
                                    spy_ret_final = float(final_vals.get("SPY", 100)) - 100
                                    qqq_ret_final = float(final_vals.get("QQQ", 100)) - 100
                                    with mc1:
                                        st.metric(
                                            f"내 포트폴리오 ({bench_period})",
                                            f"{port_ret_final:+.2f}%",
                                        )
                                    with mc2:
                                        alpha_spy = port_ret_final - spy_ret_final
                                        st.metric(
                                            f"SPY vs 포트폴리오 Alpha",
                                            f"{spy_ret_final:+.2f}%",
                                            delta=f"Alpha {alpha_spy:+.2f}%p",
                                        )
                                    with mc3:
                                        alpha_qqq = port_ret_final - qqq_ret_final
                                        st.metric(
                                            f"QQQ vs 포트폴리오 Alpha",
                                            f"{qqq_ret_final:+.2f}%",
                                            delta=f"Alpha {alpha_qqq:+.2f}%p",
                                        )

                                    # Rolling 차트 (Altair line chart)
                                    chart_data.index = pd.to_datetime(chart_data.index)
                                    chart_data = chart_data.reset_index()
                                    date_col = chart_data.columns[0]
                                    chart_data = chart_data.rename(columns={date_col: "날짜"})
                                    chart_long = chart_data.melt(
                                        id_vars="날짜", var_name="종류", value_name="누적수익률(100=시작)"
                                    )
                                    color_scale = alt.Scale(
                                        domain=["내 포트폴리오", "SPY", "QQQ"],
                                        range=["#3b82f6", "#f59e0b", "#10b981"],
                                    )
                                    line_chart = (
                                        alt.Chart(chart_long)
                                        .mark_line(strokeWidth=2)
                                        .encode(
                                            x=alt.X("날짜:T", title="날짜"),
                                            y=alt.Y(
                                                "누적수익률(100=시작):Q",
                                                title="누적 수익률 (시작=100)",
                                                scale=alt.Scale(zero=False),
                                            ),
                                            color=alt.Color("종류:N", scale=color_scale, title=""),
                                            tooltip=[
                                                alt.Tooltip("날짜:T", format="%Y-%m-%d"),
                                                alt.Tooltip("종류:N"),
                                                alt.Tooltip("누적수익률(100=시작):Q", format=".2f"),
                                            ],
                                        )
                                        .properties(height=320)
                                        .interactive()
                                    )
                                    rule = (
                                        alt.Chart(pd.DataFrame({"y": [100]}))
                                        .mark_rule(color="gray", strokeDash=[4, 4], strokeWidth=1)
                                        .encode(y="y:Q")
                                    )
                                    st.altair_chart(line_chart + rule, use_container_width=True)
                                    st.caption(
                                        "🔵 내 포트폴리오 · 🟡 SPY · 🟢 QQQ | "
                                        "시작점 100 기준 normalized. 포트폴리오는 현재 자산 비중으로 계산되며, "
                                        "실제 매수 시점과 다를 수 있습니다."
                                    )
                except Exception as e:
                    st.warning(f"Benchmark 비교 차트 계산 중 오류가 발생했습니다: {e}")

                # ── Earnings Calendar ──────────────────────────────────────
                st.divider()
                st.markdown("### 📅 보유 종목 실적 발표 캘린더")
                st.caption("보유 종목의 다음 실적 발표일을 자동으로 조회합니다.")
                try:
                    earn_tickers = tuple(sorted(set(
                        sell_radar_df["티커"].dropna().astype(str).unique().tolist()
                    )))
                    if earn_tickers:
                        with st.spinner("실적 발표일 조회 중..."):
                            earn_data = fetch_earnings_calendar(earn_tickers)

                        if not earn_data:
                            st.info("실적 발표일 데이터를 가져오지 못했습니다.")
                        else:
                            # 30일 이내 발표 예정 강조
                            upcoming = [e for e in earn_data if 0 <= e["days_until"] <= 30]
                            future = [e for e in earn_data if e["days_until"] > 30]
                            past = [e for e in earn_data if e["days_until"] < 0]

                            if upcoming:
                                st.warning(f"⚠️ **30일 이내 실적 발표 예정: {len(upcoming)}개 종목**")
                                for e in upcoming:
                                    eps_str = f"EPS 예상: ${e['eps_estimate']}" if e['eps_estimate'] else ""
                                    st.markdown(
                                        f"🔴 **{e['ticker']}** — {e['earnings_date']} "
                                        f"(**D-{e['days_until']}일**) {eps_str}"
                                    )

                            earn_rows = []
                            for e in earn_data:
                                if e["days_until"] >= 0:
                                    label = f"D-{e['days_until']}일"
                                    status = "🔴 30일 이내" if e["days_until"] <= 30 else "🟡 예정"
                                else:
                                    label = f"{abs(e['days_until'])}일 전"
                                    status = "✅ 완료"
                                earn_rows.append({
                                    "티커": e["ticker"],
                                    "실적 발표일": e["earnings_date"],
                                    "D-Day": label,
                                    "상태": status,
                                    "EPS 예상": f"${e['eps_estimate']}" if e["eps_estimate"] else "N/A",
                                })
                            earn_df = pd.DataFrame(earn_rows)
                            st.dataframe(earn_df, use_container_width=True, hide_index=True)
                except Exception as _earn_e:
                    st.warning(f"실적 캘린더 로드 중 오류: {_earn_e}")

        # ═══════════════════════════════════════════════════════════════════
        # 섹션 B: 🎯 매도 타이밍 레이더 (기술적 신호 + AI)
        # ═══════════════════════════════════════════════════════════════════
        st.divider()
        st.markdown("## 🎯 매도 타이밍 레이더")
        st.caption("RSI·MACD·52주 고점·200일선 기반 위험점수와 AI 분석으로 매도 우선순위를 제시합니다.")

        if not filtered_portfolio_df.empty:
            radar_tickers = filtered_portfolio_df["Ticker"].dropna().astype(str).str.upper().unique().tolist()
            with st.spinner("매도 신호 분석 중..."):
                sell_signals = compute_sell_signal_indicators(radar_tickers)

            sig_rows = []
            for tk in radar_tickers:
                sig = sell_signals.get(tk, _empty_sell_signal())
                rsi_v = sig.get("rsi", np.nan)
                pct_high = sig.get("pct_from_52w_high", np.nan)
                above200 = sig.get("above_ma200")
                macd_s = sig.get("macd_signal", "N/A")
                macd_display = {"DEAD_CROSS": "💀 데드크로스", "BELOW_SIGNAL": "📉 시그널 하회",
                                "GOLDEN_CROSS": "✨ 골든크로스", "ABOVE_SIGNAL": "📈 시그널 상회"}.get(macd_s, macd_s)
                sig_rows.append({
                    "신호": sig.get("signal_label", "⚪"),
                    "티커": tk,
                    "위험점수": sig.get("signal_score", 0),
                    "RSI(14)": f"{rsi_v:.1f}" if pd.notna(rsi_v) else "N/A",
                    "MACD": macd_display,
                    "52주고점대비": f"{pct_high:+.1f}%" if pd.notna(pct_high) else "N/A",
                    "200일선": "✅ 위" if above200 is True else ("🔴 아래" if above200 is False else "N/A"),
                    "200일선가격": f"${sig.get('ma200', np.nan):,.2f}" if pd.notna(sig.get("ma200")) else "N/A",
                })

            if sig_rows:
                sig_df = pd.DataFrame(sig_rows).sort_values("위험점수", ascending=False).reset_index(drop=True)

                def _c_signal(v):
                    if "🔴" in str(v): return "color:#dc2626;font-weight:700;"
                    if "🟡" in str(v): return "color:#d97706;font-weight:700;"
                    if "🟢" in str(v): return "color:#16a34a;font-weight:700;"
                    return ""

                def _c_rsi(v):
                    try:
                        f = float(str(v))
                        if f > 75: return "color:#dc2626;font-weight:700;"
                        if f > 70: return "color:#d97706;font-weight:700;"
                    except Exception: pass
                    return ""

                st.dataframe(
                    sig_df.drop(columns=["위험점수"]).style
                    .map(_c_signal, subset=["신호"]).map(_c_rsi, subset=["RSI(14)"]),
                    use_container_width=True, hide_index=True,
                )

            # AI 매도 분석
            st.markdown("#### 🤖 AI 매도 우선순위 분석")
            if st.button("AI 매도 분석 실행", key="btn_ai_sell_analysis", type="primary"):
                if not filtered_portfolio_df.empty and sell_signals:
                    with st.spinner("Gemini가 포트폴리오를 분석하는 중..."):
                        try:
                            port_lines = []
                            for _, pr in filtered_portfolio_df.iterrows():
                                tk = str(pr.get("Ticker", "")).upper()
                                avg_p = float(pr.get("Purchase_Price") or 0)
                                sig = sell_signals.get(tk, {})
                                cur_p = sig.get("current_price", np.nan)
                                rsi_v = sig.get("rsi", np.nan)
                                pnl = ((float(cur_p) / avg_p) - 1.0) * 100.0 if pd.notna(cur_p) and avg_p > 0 else np.nan
                                _cur_str = "N/A" if pd.isna(cur_p) else f"{cur_p:.2f}"
                                _pnl_str = "N/A" if pd.isna(pnl) else f"{pnl:.1f}pct"
                                _rsi_str = "N/A" if pd.isna(rsi_v) else f"{rsi_v:.1f}"
                                _high_str = "N/A" if pd.isna(sig.get("pct_from_52w_high", np.nan)) else f"{sig.get('pct_from_52w_high'):.1f}pct"
                                _ma200_str = "위" if sig.get("above_ma200") else ("아래" if sig.get("above_ma200") is False else "N/A")
                                _macd_str = sig.get("macd_signal", "N/A")
                                port_lines.append(
                                    f"- {tk}: 평단가${avg_p:.2f} 현재가${_cur_str} "
                                    f"수익률={_pnl_str} RSI={_rsi_str} MACD={_macd_str} "
                                    f"52주고점대비={_high_str} 200일선={_ma200_str}"
                                )
                            port_text = "\n".join(port_lines)
                            ai_prompt = (
                                "You are a quant investment expert. Analyze the portfolio below and respond ONLY with a valid JSON array. "
                                "No explanation, no markdown, no extra text. Pure JSON only.\n"
                                "Each item MUST have exactly these 5 fields: "
                                "{\"ticker\":\"XXX\",\"priority\":1,\"action\":\"SELL NOW or WATCH or HOLD\","
                                "\"reason\":\"한국어로 20자 이내\",\"target_price\":\"$000.00 or N/A\"}\n"
                                "priority 1 = most urgent to sell. Include ALL tickers. "
                                "reason must be in Korean under 20 chars. "
                                "target_price = suggested sell price in USD (e.g. $150.00), or N/A if no clear target.\n"
                                "[PORTFOLIO]\n" + port_text
                            )
                            _ai_m = _GenAIModel("gemini-2.5-flash", generation_config={
                                "temperature": 0.0,
                                "max_output_tokens": 8192,
                                "thinking_budget": 0,
                                "response_mime_type": "application/json",
                            })
                            _resp = _ai_m.generate_content(ai_prompt)
                            raw = getattr(_resp, "text", "") or ""
                            if not raw and hasattr(_resp, "candidates"):
                                for _c in _resp.candidates:
                                    for _p in getattr(_c.content, "parts", []):
                                        raw += getattr(_p, "text", "")

                            # 안전한 JSON 파싱: 코드블록 제거 → 배열 추출 → 불완전 JSON 복구
                            raw = raw.strip()
                            for _pfx in ["```json", "```"]:
                                if raw.startswith(_pfx):
                                    raw = raw[len(_pfx):]
                            if raw.endswith("```"):
                                raw = raw[:-3]
                            raw = raw.strip()

                            # JSON 배열 구간 추출
                            _arr_start = raw.find("[")
                            _arr_end = raw.rfind("]")
                            if _arr_start != -1 and _arr_end != -1 and _arr_end > _arr_start:
                                raw = raw[_arr_start:_arr_end + 1]

                            # 파싱 시도 — 실패 시 완전한 객체만 추출 (잘린 JSON 복구)
                            ai_data = None
                            try:
                                ai_data = json.loads(raw)
                            except json.JSONDecodeError:
                                # 완전한 JSON 객체만 추출 (마지막 불완전 객체 제거)
                                _safe = []
                                _depth = 0
                                _obj_start = None
                                for _ci, _ch in enumerate(raw):
                                    if _ch == "{":
                                        if _depth == 0:
                                            _obj_start = _ci
                                        _depth += 1
                                    elif _ch == "}":
                                        _depth -= 1
                                        if _depth == 0 and _obj_start is not None:
                                            try:
                                                _safe.append(json.loads(raw[_obj_start:_ci + 1]))
                                            except Exception:
                                                pass
                                            _obj_start = None
                                if _safe:
                                    ai_data = _safe

                            if isinstance(ai_data, list) and ai_data:
                                ai_df = pd.DataFrame(ai_data).sort_values("priority")
                                def _c_act(v):
                                    if "SELL NOW" in str(v): return "color:#dc2626;font-weight:700;"
                                    if "WATCH" in str(v): return "color:#d97706;font-weight:700;"
                                    if "HOLD" in str(v): return "color:#16a34a;font-weight:700;"
                                    return ""
                                st.dataframe(ai_df.style.map(_c_act, subset=["action"]), use_container_width=True, hide_index=True)
                                _total = len(filtered_portfolio_df)
                                if len(ai_df) < _total:
                                    st.caption(f"⚠️ 응답이 잘려 {len(ai_df)}/{_total}개 종목만 표시됩니다. 종목 수를 줄이거나 계좌 필터를 좁혀주세요.")
                            else:
                                st.warning("AI 응답을 파싱할 수 없습니다. 잠시 후 다시 시도해주세요.")
                        except Exception as _e_ai:
                            st.error(f"AI 분석 오류: {_e_ai}")
                else:
                    st.info("포트폴리오가 비어있습니다.")
        else:
            st.info("포트폴리오가 비어있거나 계좌 필터 조건에 맞는 종목이 없습니다.")

        # ═══════════════════════════════════════════════════════════════════
        # 섹션 C: 📒 매매 히스토리 & 실현 손익
        # ═══════════════════════════════════════════════════════════════════
        st.divider()
        st.markdown("## 📒 매매 히스토리 & 실현 손익")

        trade_hist_df = load_trade_history(puid)

        if trade_hist_df.empty:
            # 시트 연결 상태 진단
            _ws_check, _ws_err = open_trade_history_worksheet()
            if _ws_err:
                st.error(f"⚠️ Trade_History 시트 연결 실패: {_ws_err}")
                st.caption("Google Sheets에 'Trade_History' 탭이 없거나 접근 권한이 없습니다. 매도 기록을 한 번 저장하면 자동 생성됩니다.")
            else:
                _raw_rows, _ = _trade_history_all_values_cached()
                _total_rows = len(_raw_rows) - 1 if _raw_rows and len(_raw_rows) > 1 else 0
                if _total_rows > 0:
                    st.warning(f"⚠️ Trade_History 시트에 총 {_total_rows}개 행이 있지만 현재 계정({puid})의 기록이 없습니다. 저장 시 user_id가 일치하는지 확인해주세요.")
                else:
                    st.info("아직 매매 기록이 없습니다. '종목 추가' 또는 '매도 기록'을 통해 거래를 기록하면 여기에 표시됩니다.")
                    st.caption("💡 동기화 버튼을 눌러도 안 나타나면 Google Sheets Quant_DB에 'Trade_History' 탭이 생성됐는지 확인해주세요.")
        else:
            th_tab1, th_tab2, th_tab3 = st.tabs(["📋 전체 거래 내역", "💰 실현 손익 분석", "📈 누적 손익 차트"])

            with th_tab1:
                show_df = trade_hist_df.copy()
                show_df["거래금액"] = show_df["shares"] * show_df["price"]
                disp = show_df.rename(columns={
                    "account": "계좌", "ticker": "티커", "action": "매수/매도",
                    "shares": "수량", "price": "가격", "date": "날짜", "memo": "메모"
                })[["날짜", "계좌", "티커", "매수/매도", "수량", "가격", "거래금액", "메모"]]

                def _c_act2(v):
                    if str(v).upper() == "BUY": return "color:#2563eb;font-weight:700;"
                    if str(v).upper() == "SELL": return "color:#dc2626;font-weight:700;"
                    return ""

                st.dataframe(
                    disp.style
                    .format({"수량": "{:,.4f}", "가격": "${:,.4f}", "거래금액": "${:,.2f}"}, na_rep="N/A")
                    .map(_c_act2, subset=["매수/매도"]),
                    use_container_width=True, hide_index=True,
                )

            with th_tab2:
                pnl_df = compute_realized_pnl(trade_hist_df)
                if pnl_df.empty:
                    st.info("실현된 손익이 없습니다. 매도 기록이 있어야 계산됩니다.")
                else:
                    total_fifo = pnl_df["fifo_pnl"].sum()
                    total_avg = pnl_df["avg_pnl"].sum()
                    total_trades = len(pnl_df)
                    win_trades = (pnl_df["fifo_pnl"] > 0).sum()
                    pm1, pm2, pm3 = st.columns(3)
                    with pm1:
                        st.metric("총 실현 손익 (FIFO)", f"${total_fifo:+,.2f}")
                    with pm2:
                        st.metric("총 실현 손익 (평균단가)", f"${total_avg:+,.2f}")
                    with pm3:
                        st.metric("승률 (FIFO 기준)", f"{win_trades}/{total_trades} ({win_trades/total_trades*100:.0f}%)" if total_trades else "N/A")
                    st.divider()

                    def _c_pnl(v):
                        try:
                            f = float(str(v).replace("$","").replace("+","").replace(",",""))
                            if f > 0: return "color:#16a34a;font-weight:700;"
                            if f < 0: return "color:#dc2626;font-weight:700;"
                        except Exception: pass
                        return ""

                    disp_pnl = pnl_df.rename(columns={
                        "ticker": "티커", "account": "계좌", "sell_date": "매도일",
                        "shares_sold": "매도수량", "sell_price": "매도가",
                        "fifo_cost": "FIFO매수가", "avg_cost": "평균매수가",
                        "fifo_pnl": "FIFO손익($)", "avg_pnl": "평균단가손익($)",
                        "fifo_pnl_pct": "FIFO손익(%)", "avg_pnl_pct": "평균단가손익(%)", "memo": "메모",
                    })
                    st.dataframe(
                        disp_pnl.style
                        .format({"매도수량": "{:,.4f}", "매도가": "${:,.4f}", "FIFO매수가": "${:,.4f}",
                                 "평균매수가": "${:,.4f}", "FIFO손익($)": "${:+,.2f}", "평균단가손익($)": "${:+,.2f}",
                                 "FIFO손익(%)": "{:+.2f}%", "평균단가손익(%)": "{:+.2f}%"}, na_rep="N/A")
                        .map(_c_pnl, subset=["FIFO손익($)", "평균단가손익($)"]),
                        use_container_width=True, hide_index=True,
                    )

            with th_tab3:
                pnl_c = compute_realized_pnl(trade_hist_df)
                if pnl_c.empty:
                    st.info("실현 손익 데이터가 없습니다.")
                else:
                    pnl_c["sell_date"] = pd.to_datetime(pnl_c["sell_date"], errors="coerce")
                    pnl_c = pnl_c.dropna(subset=["sell_date"]).sort_values("sell_date")
                    pnl_c["cum_fifo"] = pnl_c["fifo_pnl"].cumsum()
                    pnl_c["cum_avg"] = pnl_c["avg_pnl"].cumsum()

                    cum_long = pd.concat([
                        pnl_c[["sell_date", "cum_fifo"]].rename(columns={"cum_fifo": "손익"}).assign(방식="FIFO"),
                        pnl_c[["sell_date", "cum_avg"]].rename(columns={"cum_avg": "손익"}).assign(방식="평균단가"),
                    ])
                    cum_chart = (
                        alt.Chart(cum_long).mark_line(point=True)
                        .encode(
                            x=alt.X("sell_date:T", title="매도일"),
                            y=alt.Y("손익:Q", title="누적 실현 손익 ($)"),
                            color=alt.Color("방식:N"),
                            tooltip=[alt.Tooltip("sell_date:T", title="날짜"), alt.Tooltip("방식:N"), alt.Tooltip("손익:Q", format="$,.2f")],
                        ).properties(title="누적 실현 손익 추이")
                    )
                    zero_rule = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="gray", strokeDash=[4,4]).encode(y="y:Q")
                    st.altair_chart(cum_chart + zero_rule, use_container_width=True)

                    # 종목별 바 차트
                    tk_pnl = pnl_c.groupby("ticker")[["fifo_pnl","avg_pnl"]].sum().reset_index()
                    tk_long = pd.concat([
                        tk_pnl[["ticker","fifo_pnl"]].rename(columns={"fifo_pnl":"손익"}).assign(방식="FIFO"),
                        tk_pnl[["ticker","avg_pnl"]].rename(columns={"avg_pnl":"손익"}).assign(방식="평균단가"),
                    ])
                    bar_chart = (
                        alt.Chart(tk_long).mark_bar()
                        .encode(
                            x=alt.X("ticker:N", title="티커"),
                            y=alt.Y("손익:Q", title="실현 손익 ($)"),
                            color=alt.Color("방식:N"),
                            xOffset="방식:N",
                            tooltip=[alt.Tooltip("ticker:N"), alt.Tooltip("방식:N"), alt.Tooltip("손익:Q", format="$,.2f")],
                        ).properties(title="종목별 실현 손익 (FIFO vs 평균단가)")
                    )
                    st.altair_chart(bar_chart, use_container_width=True)

    elif main_nav == _MAIN_NAV_OPTIONS[8]:
        # ─────────────────────────────────────────────────────────────────────
        # 🎯 AI 내러티브 적중률 트래커
        # Narratives 시트에 저장된 Winners/Emerging 티커의 실제 수익률을 역산해
        # AI의 예측 품질을 정량적으로 평가합니다.
        # ─────────────────────────────────────────────────────────────────────
        st.subheader("🎯 AI 내러티브 적중률 트래커")
        st.caption(
            "`Narratives` 시트의 **Winners / Emerging** 티커를 기준으로, "
            "내러티브 생성 시점 이후 **실제 주가 수익률**을 역산해 AI의 예측 품질을 정량 평가합니다. "
            "적중 기준: 내러티브 생성 후 해당 기간 내 **+5% 이상** 상승."
        )

        uid_tracker = str(st.session_state.get("user_id") or "").strip()

        # ── 설정 컨트롤 ────────────────────────────────────────────────────
        ctrl_col1, ctrl_col2, ctrl_col3 = st.columns(3)
        with ctrl_col1:
            lookback_days = st.selectbox(
                "분석 기간 (최근 N일 내러티브)",
                options=[7, 14, 30, 60, 90],
                index=1,
                key="tracker_lookback_days",
                help="몇 일 이내에 생성된 내러티브를 평가 대상으로 할지 설정합니다.",
            )
        with ctrl_col2:
            eval_horizon = st.selectbox(
                "수익률 측정 기간",
                options=["1주(5거래일)", "2주(10거래일)", "1개월(21거래일)"],
                index=0,
                key="tracker_eval_horizon",
                help="내러티브 생성 시점 이후 몇 거래일 후 수익률을 측정할지 설정합니다.",
            )
        with ctrl_col3:
            hit_threshold = st.number_input(
                "적중 기준 (%)",
                min_value=1.0,
                max_value=20.0,
                value=5.0,
                step=0.5,
                format="%.1f",
                key="tracker_hit_threshold",
                help="이 수익률(%) 이상이면 '적중'으로 판정합니다.",
            )

        horizon_map = {
            "1주(5거래일)": 5,
            "2주(10거래일)": 10,
            "1개월(21거래일)": 21,
        }
        horizon_td = horizon_map[eval_horizon]

        run_tracker = st.button(
            "📊 적중률 분석 실행",
            key="run_accuracy_tracker_btn",
            type="primary",
            use_container_width=True,
        )

        if run_tracker:
            with st.spinner("Narratives 시트에서 기록을 불러오는 중..."):
                all_records, sheet_err = fetch_narrative_records_from_sheet()

            if sheet_err:
                st.error(f"시트 로드 실패: {sheet_err}")
            elif not all_records:
                st.info("분석 가능한 내러티브 기록이 없습니다. 먼저 내러티브를 생성하고 저장해주세요.")
            else:
                # user_id 필터링
                user_records = [
                    r for r in all_records
                    if str(r.get("_sheet_user_id") or "").strip().upper() == uid_tracker.upper()
                ]

                if not user_records:
                    st.info(f"현재 계정(`{uid_tracker}`)으로 저장된 내러티브가 없습니다.")
                else:
                    # lookback 필터링
                    now_utc = datetime.now(timezone.utc)
                    cutoff_utc = now_utc - timedelta(days=lookback_days)
                    filtered_records = [
                        r for r in user_records
                        if (_narrative_parse_saved_at_utc(r.get("saved_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff_utc
                    ]

                    if not filtered_records:
                        st.warning(f"최근 {lookback_days}일 이내 저장된 내러티브가 없습니다. 분석 기간을 늘려보세요.")
                    else:
                        # ── 티커 수집 ──────────────────────────────────────────
                        rows_to_evaluate = []
                        for rec in filtered_records:
                            saved_at_utc = _narrative_parse_saved_at_utc(rec.get("saved_at"))
                            if saved_at_utc is None:
                                continue
                            saved_at_kst = saved_at_utc.astimezone(_MARKET_ET_TZ)
                            date_label = saved_at_kst.strftime("%m/%d %H:%M")
                            session_label = rec.get("session_label") or ""

                            # Winners
                            winners_csv = str(rec.get("_sheet_winners_csv") or "").strip()
                            for tk in filter_scanner_ticker_list(
                                [t.strip().upper() for t in winners_csv.split(",") if t.strip()]
                            ):
                                rows_to_evaluate.append({
                                    "saved_at_utc": saved_at_utc,
                                    "date_label": date_label,
                                    "session": session_label,
                                    "type": "Winners",
                                    "ticker": tk,
                                })

                            # Emerging
                            emerging_csv = str(rec.get("_sheet_emerging_csv") or "").strip()
                            for tk in filter_scanner_ticker_list(
                                [t.strip().upper() for t in emerging_csv.split(",") if t.strip()]
                            ):
                                rows_to_evaluate.append({
                                    "saved_at_utc": saved_at_utc,
                                    "date_label": date_label,
                                    "session": session_label,
                                    "type": "Emerging",
                                    "ticker": tk,
                                })

                        if not rows_to_evaluate:
                            st.warning("해당 기간 내러티브에 Winners/Emerging 티커가 기록되어 있지 않습니다.")
                        else:
                            unique_tickers = list(dict.fromkeys(r["ticker"] for r in rows_to_evaluate))
                            st.info(
                                f"**{len(filtered_records)}**개 내러티브 · **{len(unique_tickers)}**개 고유 티커 분석 중... "
                                f"(측정 기간: {eval_horizon})"
                            )

                            # ── 가격 데이터 다운로드 ───────────────────────────
                            with st.spinner(f"FMP에서 {len(unique_tickers)}개 티커 가격 데이터를 받는 중..."):
                                dl_period = "3mo" if horizon_td <= 21 else "6mo"
                                closes = _factcheck_download_closes(unique_tickers, period=dl_period)

                            # ── 수익률 계산 ────────────────────────────────────
                            result_rows = []
                            for row in rows_to_evaluate:
                                tk = row["ticker"]
                                saved_at_utc = row["saved_at_utc"]

                                if closes.empty or tk not in closes.columns:
                                    ret = np.nan
                                else:
                                    series = closes[tk].dropna()
                                    # 내러티브 생성 날짜 이후 데이터만 슬라이싱
                                    # FMP 종가 인덱스가 tz-aware일 수 있으므로(방어적 처리)
                                    # tz_convert(None)로 naive로 변환 후 비교
                                    try:
                                        naive_cutoff = pd.Timestamp(saved_at_utc.replace(tzinfo=None))
                                        idx = series.index
                                        if hasattr(idx, 'tz') and idx.tz is not None:
                                            idx_naive = idx.tz_convert(None)  # tz aware → naive
                                        else:
                                            idx_naive = idx  # 이미 naive
                                        mask = idx_naive >= naive_cutoff
                                        series_after = series.iloc[mask.values]
                                        # 슬라이싱 결과가 비어있으면 전체 series 사용
                                        if series_after.empty:
                                            series_after = series
                                    except Exception:
                                        series_after = series

                                    if len(series_after) >= horizon_td:
                                        base = float(series_after.iloc[0])
                                        end_ = float(series_after.iloc[min(horizon_td - 1, len(series_after) - 1)])
                                        ret = (end_ / base - 1.0) * 100.0 if base != 0 else np.nan
                                    elif len(series_after) >= 2:
                                        # 데이터가 horizon보다 짧으면 현재까지 수익률로 표시
                                        base = float(series_after.iloc[0])
                                        end_ = float(series_after.iloc[-1])
                                        ret = (end_ / base - 1.0) * 100.0 if base != 0 else np.nan
                                    else:
                                        ret = np.nan

                                hit = (pd.notna(ret) and ret >= hit_threshold)
                                result_rows.append({
                                    "날짜": row["date_label"],
                                    "세션": row["session"],
                                    "타입": row["type"],
                                    "티커": tk,
                                    f"수익률(%, {eval_horizon})": ret,
                                    "적중 여부": "✅ 적중" if hit else ("N/A" if pd.isna(ret) else "❌ 미적중"),
                                })

                            result_df = pd.DataFrame(result_rows)

                            # ── 요약 지표 ──────────────────────────────────────
                            st.divider()
                            st.markdown("### 📈 종합 적중률 요약")

                            valid_df = result_df[result_df["적중 여부"] != "N/A"].copy()
                            total_valid = len(valid_df)
                            hit_count = (valid_df["적중 여부"] == "✅ 적중").sum()
                            hit_rate = (hit_count / total_valid * 100) if total_valid > 0 else 0.0

                            winners_df = valid_df[valid_df["타입"] == "Winners"]
                            emerging_df = valid_df[valid_df["타입"] == "Emerging"]
                            w_hit = (winners_df["적중 여부"] == "✅ 적중").sum()
                            e_hit = (emerging_df["적중 여부"] == "✅ 적중").sum()
                            w_rate = (w_hit / len(winners_df) * 100) if len(winners_df) > 0 else 0.0
                            e_rate = (e_hit / len(emerging_df) * 100) if len(emerging_df) > 0 else 0.0

                            avg_ret_winners = pd.to_numeric(
                                winners_df[f"수익률(%, {eval_horizon})"], errors="coerce"
                            ).mean()
                            avg_ret_emerging = pd.to_numeric(
                                emerging_df[f"수익률(%, {eval_horizon})"], errors="coerce"
                            ).mean()

                            m1, m2, m3, m4 = st.columns(4)
                            with m1:
                                st.metric(
                                    "전체 적중률",
                                    f"{hit_rate:.1f}%",
                                    delta=f"{hit_count}/{total_valid}건",
                                )
                            with m2:
                                st.metric(
                                    "Winners 적중률",
                                    f"{w_rate:.1f}%",
                                    delta=f"평균 {avg_ret_winners:+.1f}%" if pd.notna(avg_ret_winners) else "N/A",
                                )
                            with m3:
                                st.metric(
                                    "Emerging 적중률",
                                    f"{e_rate:.1f}%",
                                    delta=f"평균 {avg_ret_emerging:+.1f}%" if pd.notna(avg_ret_emerging) else "N/A",
                                )
                            with m4:
                                na_count = (result_df["적중 여부"] == "N/A").sum()
                                st.metric(
                                    "데이터 없음",
                                    f"{na_count}건",
                                    delta="데이터 미수신 티커",
                                )

                            # 적중률 수준별 평가 메시지
                            if total_valid > 0:
                                if hit_rate >= 60:
                                    st.success(
                                        f"🏆 AI 예측 품질 우수: 적중률 **{hit_rate:.1f}%** — "
                                        f"내러티브 신호를 적극 활용할 수 있는 수준입니다."
                                    )
                                elif hit_rate >= 40:
                                    st.warning(
                                        f"🟡 AI 예측 품질 보통: 적중률 **{hit_rate:.1f}%** — "
                                        f"내러티브를 참고 지표로만 활용하고, 다른 기준과 병행하세요."
                                    )
                                else:
                                    st.error(
                                        f"🔴 AI 예측 품질 저조: 적중률 **{hit_rate:.1f}%** — "
                                        f"현재 시장 환경이 내러티브 패턴과 맞지 않을 수 있습니다."
                                    )

                            # ── 티커별 베스트/워스트 ──────────────────────────
                            st.divider()
                            st.markdown("### 🏅 티커별 수익률 순위")

                            ticker_summary = (
                                result_df.groupby("티커")
                                .agg(
                                    타입=("타입", "first"),
                                    평균수익률=(f"수익률(%, {eval_horizon})", lambda x: pd.to_numeric(x, errors="coerce").mean()),
                                    등장횟수=("티커", "count"),
                                )
                                .reset_index()
                                .sort_values("평균수익률", ascending=False, na_position="last")
                            )

                            best5 = ticker_summary.head(5)
                            worst5 = ticker_summary.tail(5).sort_values("평균수익률", ascending=True)

                            rank_col1, rank_col2 = st.columns(2)
                            with rank_col1:
                                st.markdown("#### 🟢 Best 5 티커")
                                for _, r in best5.iterrows():
                                    val = r["평균수익률"]
                                    val_str = f"{val:+.2f}%" if pd.notna(val) else "N/A"
                                    st.markdown(
                                        f"**{r['티커']}** `{r['타입']}` — {val_str} "
                                        f"_(등장 {int(r['등장횟수'])}회)_"
                                    )

                            with rank_col2:
                                st.markdown("#### 🔴 Worst 5 티커")
                                for _, r in worst5.iterrows():
                                    val = r["평균수익률"]
                                    val_str = f"{val:+.2f}%" if pd.notna(val) else "N/A"
                                    st.markdown(
                                        f"**{r['티커']}** `{r['타입']}` — {val_str} "
                                        f"_(등장 {int(r['등장횟수'])}회)_"
                                    )

                            # ── 상세 결과 테이블 ──────────────────────────────
                            st.divider()
                            st.markdown("### 📋 상세 결과 테이블")
                            ret_col = f"수익률(%, {eval_horizon})"

                            def _style_hit(val):
                                if "적중" in str(val):
                                    return "color: #16a34a; font-weight: 700;"
                                if "미적중" in str(val):
                                    return "color: #dc2626; font-weight: 600;"
                                return "color: #9ca3af;"

                            def _style_return(val):
                                v = pd.to_numeric(val, errors="coerce")
                                if pd.isna(v):
                                    return "color: #9ca3af;"
                                if v >= hit_threshold:
                                    return "color: #16a34a; font-weight: 600;"
                                if v < 0:
                                    return "color: #dc2626;"
                                return ""

                            styled_result = (
                                result_df.style
                                .format(
                                    {ret_col: lambda x: f"{x:+.2f}%" if pd.notna(x) else "N/A"},
                                    na_rep="N/A",
                                )
                                .map(_style_hit, subset=["적중 여부"])
                                .map(_style_return, subset=[ret_col])
                                .background_gradient(
                                    cmap="RdYlGn",
                                    subset=[ret_col],
                                    axis=0,
                                )
                            )
                            st.dataframe(styled_result, use_container_width=True, hide_index=True)

                            st.caption(
                                f"⚠️ 적중 기준: 내러티브 생성 시점 이후 {eval_horizon} 기준 **+{hit_threshold:.1f}%** 이상 상승. "
                                "데이터가 horizon보다 짧은 경우 현재까지 수익률로 대체합니다. 투자 권유가 아닙니다."
                            )

                            # ── 월별 적중률 트렌드 차트 ──────────────────
                            st.divider()
                            st.markdown("### 📅 월별 적중률 트렌드")
                            try:
                                result_df["날짜_dt"] = pd.to_datetime(
                                    result_df["날짜"].apply(
                                        lambda x: f"2026/{x}" if "/" in str(x) else str(x)
                                    ), errors="coerce"
                                )
                                valid_monthly = result_df[result_df["적중 여부"] != "N/A"].copy()
                                valid_monthly["월"] = valid_monthly["날짜_dt"].dt.to_period("M").astype(str)
                                monthly_stats = (
                                    valid_monthly.groupby("월")
                                    .apply(lambda g: round((g["적중 여부"] == "✅ 적중").sum() / len(g) * 100, 1))
                                    .reset_index()
                                    .rename(columns={0: "적중률(%)"})
                                )
                                if len(monthly_stats) >= 2:
                                    monthly_chart = (
                                        alt.Chart(monthly_stats)
                                        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                                        .encode(
                                            x=alt.X("월:N", title="월"),
                                            y=alt.Y("적중률(%):Q", scale=alt.Scale(domain=[0, 100]), title="적중률(%)"),
                                            color=alt.condition(
                                                "datum['적중률(%)'] >= 50",
                                                alt.value("#16a34a"),
                                                alt.value("#dc2626"),
                                            ),
                                            tooltip=["월:N", "적중률(%):Q"],
                                        )
                                        .properties(height=200)
                                    )
                                    rule_50 = (
                                        alt.Chart(pd.DataFrame({"y": [50]}))
                                        .mark_rule(color="gray", strokeDash=[4, 4])
                                        .encode(y="y:Q")
                                    )
                                    st.altair_chart(monthly_chart + rule_50, use_container_width=True)
                                    st.caption("초록 = 50% 이상 적중 · 빨강 = 50% 미만 | 점선 = 50% 기준선")
                                else:
                                    st.info("월별 트렌드를 표시하려면 2개월 이상의 내러티브 기록이 필요해요.")
                            except Exception as _me:
                                st.warning(f"월별 트렌드 차트 오류: {_me}")

        else:
            # 버튼 미클릭 상태 안내
            st.info(
                "설정을 완료한 후 **📊 적중률 분석 실행** 버튼을 클릭하면 분석이 시작됩니다.\n\n"
                "**분석 방식:**\n"
                "- `Narratives` 시트에서 현재 계정의 내러티브 기록을 불러옵니다.\n"
                "- 각 내러티브의 **Winners**와 **Emerging** 티커에 대해 생성 시점 이후 수익률을 계산합니다.\n"
                "- 설정한 적중 기준(%) 이상 상승한 티커를 '적중'으로 판정하고 AI 예측 품질을 평가합니다."
            )

    elif main_nav == _MAIN_NAV_OPTIONS[9]:
        render_sync_button("sync_tab_idea", [], "Idea-to-Portfolio 데이터를 다시 불러옵니다.")
        # ─────────────────────────────────────────────────────────────────────
        # 💡 Idea-to-Portfolio 추적
        # 내러티브 테마 → 종목 발굴 → 포트폴리오 편입 흐름을 Thesis ID로 연결
        # ─────────────────────────────────────────────────────────────────────
        st.subheader("💡 Idea-to-Portfolio 추적")
        st.caption(
            "AI 내러티브 테마(Thesis)에서 시작해 포트폴리오에 편입한 종목들을 추적합니다. "
            "**[4단계] 포트폴리오 매도 레이더**에서 종목 추가 시 Thesis를 선택하면 여기에 자동으로 기록돼요."
        )

        uid_thesis = str(st.session_state.get("user_id") or "").strip()

        with st.spinner("Thesis 기록 불러오는 중..."):
            thesis_df = load_thesis_records(uid_thesis)

        if thesis_df.empty:
            st.info(
                "아직 Thesis 기록이 없어요. "
                "[4단계] 포트폴리오 매도 레이더 → 종목 추가 시 "
                "📌 투자 Thesis 드롭다운에서 내러티브 테마를 선택하면 여기에 자동으로 기록됩니다."
            )
        else:
            # ── 요약 지표 ──────────────────────────────────────────────────
            total_positions = len(thesis_df)
            unique_thesis = thesis_df["Thesis_Title"].nunique()
            unique_tickers = thesis_df["Ticker"].nunique()

            m1, m2, m3 = st.columns(3)
            with m1:
                st.metric("총 Thesis 연결 포지션", f"{total_positions}건")
            with m2:
                st.metric("고유 Thesis 수", f"{unique_thesis}개")
            with m3:
                st.metric("추적 중인 티커", f"{unique_tickers}개")

            st.divider()

            # ── Thesis별 그룹 뷰 ───────────────────────────────────────────
            st.markdown("### 📋 Thesis별 포지션 현황")

            # 매수가/수량 조회용 포트폴리오 (루프 밖에서 1회 로드)
            try:
                portfolio_df_thesis = load_portfolio()
            except Exception:
                portfolio_df_thesis = pd.DataFrame()
            if portfolio_df_thesis is None:
                portfolio_df_thesis = pd.DataFrame()

            thesis_groups = thesis_df.groupby("Thesis_Title")

            for thesis_title, group in thesis_groups:
                tickers_in_thesis = group["Ticker"].tolist()
                narrative_date = group["Narrative_Date"].iloc[0]
                narrative_cat = group["Narrative_Category"].iloc[0]

                # 현재가 및 수익률 계산
                ticker_tuple = tuple(sorted(set(tickers_in_thesis)))
                try:
                    close_df_thesis = cached_portfolio_yf_close_1y(ticker_tuple)
                except Exception:
                    close_df_thesis = pd.DataFrame()

                perf_rows = []
                for _, row in group.iterrows():
                    tk = str(row["Ticker"]).strip().upper()
                    acct = str(row["Account"]).strip()
                    date_added = str(row["Date_Added"]).strip()

                    # 포트폴리오에서 매수가/수량 조회
                    # portfolio_df_thesis 는 루프 밖에서 이미 로드됨
                    port_row = portfolio_df_thesis[
                        (portfolio_df_thesis["Ticker"] == tk) &
                        (portfolio_df_thesis["Account"] == acct)
                    ] if not portfolio_df_thesis.empty else pd.DataFrame()

                    purchase_price = np.nan
                    current_price = np.nan
                    ret_pct = np.nan
                    if not port_row.empty:
                        purchase_price = pd.to_numeric(port_row.iloc[0].get("Purchase_Price"), errors="coerce")
                    if not close_df_thesis.empty and tk in close_df_thesis.columns:
                        series = pd.to_numeric(close_df_thesis[tk], errors="coerce").dropna()
                        current_price = float(series.iloc[-1]) if not series.empty else np.nan
                    if pd.notna(purchase_price) and pd.notna(current_price) and purchase_price > 0:
                        ret_pct = (current_price / purchase_price - 1.0) * 100.0

                    perf_rows.append({
                        "계좌": acct,
                        "티커": tk,
                        "매수가": purchase_price,
                        "현재가": current_price,
                        "수익률(%)": ret_pct,
                        "Thesis 편입일": date_added,
                    })

                perf_df = pd.DataFrame(perf_rows)
                avg_ret = pd.to_numeric(perf_df["수익률(%)"], errors="coerce").mean()
                avg_ret_str = f"{avg_ret:+.2f}%" if pd.notna(avg_ret) else "N/A"

                with st.expander(
                    f"**{thesis_title}** [{narrative_date}] — {len(tickers_in_thesis)}개 종목 · 평균 수익률 {avg_ret_str}",
                    expanded=True,
                ):
                    st.caption(f"카테고리: `{narrative_cat}` · 내러티브 날짜: `{narrative_date}`")

                    def _style_ret(val):
                        v = pd.to_numeric(val, errors="coerce")
                        if pd.isna(v): return "color: #9ca3af;"
                        if v > 0: return "color: #16a34a; font-weight: 600;"
                        if v < 0: return "color: #dc2626; font-weight: 600;"
                        return ""

                    styled_perf = (
                        perf_df.style
                        .format({
                            "매수가": lambda x: f"${x:,.2f}" if pd.notna(x) else "N/A",
                            "현재가": lambda x: f"${x:,.2f}" if pd.notna(x) else "N/A",
                            "수익률(%)": lambda x: f"{x:+.2f}%" if pd.notna(x) else "N/A",
                        }, na_rep="N/A")
                        .map(_style_ret, subset=["수익률(%)"])
                    )
                    st.dataframe(styled_perf, use_container_width=True, hide_index=True)

                    # Thesis 삭제 버튼
                    if st.button(
                        f"🗑️ '{thesis_title}' Thesis 기록 삭제",
                        key=f"del_thesis_{thesis_title[:20]}",
                        help="Thesis 연결 기록만 삭제합니다. 포트폴리오 종목은 삭제되지 않아요.",
                    ):
                        for tk in tickers_in_thesis:
                            delete_thesis_row(uid_thesis, tk, thesis_title)
                        st.success(f"'{thesis_title}' Thesis 기록을 삭제했습니다.")
                        st.rerun()

            st.divider()
            st.markdown("### 📊 티커별 Thesis 연결 현황")
            st.caption("한 종목이 여러 Thesis에 연결되어 있을 수 있어요.")
            ticker_thesis_summary = (
                thesis_df.groupby("Ticker")["Thesis_Title"]
                .apply(lambda x: " / ".join(x.unique()))
                .reset_index()
                .rename(columns={"Thesis_Title": "연결된 Thesis"})
            )
            st.dataframe(ticker_thesis_summary, use_container_width=True, hide_index=True)

    elif main_nav == _MAIN_NAV_OPTIONS[10]:
        render_sync_button("sync_tab_weekly", [], "주간 요약 데이터를 다시 불러옵니다.")
        # ─────────────────────────────────────────────────────────────────────
        # 📋 주간 포트폴리오 AI 요약
        # 포트폴리오 현황 + 최근 내러티브 + Macro를 묶어 Gemini로 주간 리포트 생성
        # ─────────────────────────────────────────────────────────────────────
        st.subheader("📋 주간 포트폴리오 AI 요약")
        st.caption(
            "현재 포트폴리오 상태 · 최근 AI 내러티브 · 거시경제 지표를 종합해 "
            "Gemini가 **주간 투자 리뷰 리포트**를 자동 생성합니다. "
            "생성된 리포트는 `Narratives` 시트에 자동 저장돼요."
        )

        uid_weekly = str(st.session_state.get("user_id") or "").strip()

        # ── 이전 저장된 요약 불러오기 ──────────────────────────────────────
        with st.expander("📚 이전 주간 요약 기록 보기", expanded=False):
            prev_records, _ = fetch_narrative_records_from_sheet()
            prev_summaries = [
                r for r in (prev_records or [])
                if str(r.get("_sheet_user_id", "")).strip().upper() == uid_weekly.upper()
                and isinstance(r.get("analysis"), dict)
                and r["analysis"].get("source") == "weekly_portfolio_summary"
            ]
            if not prev_summaries:
                st.caption("저장된 주간 요약이 없습니다.")
            else:
                prev_summaries_sorted = sorted(
                    prev_summaries,
                    key=lambda r: _narrative_parse_saved_at_utc(r.get("saved_at"))
                    or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True,
                )
                for prev in prev_summaries_sorted[:5]:
                    dt = _narrative_parse_saved_at_utc(prev.get("saved_at"))
                    dt_str = dt.astimezone(_MARKET_ET_TZ).strftime("%Y-%m-%d %H:%M") if dt else "날짜 불명"
                    summary_text = prev["analysis"].get("summary", "")
                    with st.expander(f"📋 {dt_str} (ET)", expanded=False):
                        st.markdown(summary_text)

        st.divider()

        # ── 데이터 미리보기 ────────────────────────────────────────────────
        with st.expander("📊 분석에 사용될 데이터 미리보기", expanded=False):
            col_p, col_m = st.columns(2)
            with col_p:
                st.markdown("**포트폴리오 종목**")
                preview_portfolio = load_portfolio()
                if preview_portfolio.empty:
                    st.caption("포트폴리오가 없습니다.")
                else:
                    st.dataframe(
                        preview_portfolio[["Account", "Ticker"]],
                        use_container_width=True,
                        hide_index=True,
                    )
            with col_m:
                st.markdown("**거시경제 지표**")
                macro_preview = _build_macro_context_for_summary()
                if not macro_preview:
                    st.caption("Macro 데이터 없음")
                else:
                    for k, v in list(macro_preview.items())[:5]:
                        status_icon = "✅" if v["status"] == "pass" else ("🟡" if v["status"] == "warning" else "🔴")
                        st.caption(f"{status_icon} {k}: {v['value']}")

            st.markdown("**최근 내러티브 테마**")
            narr_preview = _build_narrative_context_for_summary(uid_weekly)
            if not narr_preview:
                st.caption("내러티브 기록 없음")
            else:
                for n in narr_preview[:3]:
                    themes_str = " · ".join(n.get("themes", [])[:3])
                    st.caption(f"- {n.get('session', '')} | {themes_str}")

        st.divider()

        # ── 생성 버튼 ──────────────────────────────────────────────────────
        st.info(
            "💡 리포트 생성 전 **[4단계] 포트폴리오 매도 레이더**를 한 번 열어두면 "
            "포트폴리오 수익률 데이터가 더 정확하게 반영됩니다."
        )

        if st.button(
            "🤖 주간 AI 리포트 생성",
            key="generate_weekly_summary_btn",
            type="primary",
            use_container_width=True,
        ):
            with st.status("주간 리포트를 생성하는 중...", expanded=True) as weekly_status:
                st.write("📊 포트폴리오 데이터 수집 중...")
                portfolio_df_weekly = load_portfolio()
                if portfolio_df_weekly.empty:
                    weekly_status.update(label="❌ 포트폴리오 데이터 없음", state="error")
                    st.error("포트폴리오에 종목을 먼저 추가해주세요.")
                else:
                    # sell_radar_df 계산
                    sell_radar_weekly = build_portfolio_sell_radar_df(portfolio_df_weekly)
                    portfolio_ctx = _build_portfolio_context_for_summary(sell_radar_weekly)

                    st.write("🧠 내러티브 데이터 수집 중...")
                    narrative_ctx = _build_narrative_context_for_summary(uid_weekly)

                    st.write("🌐 거시경제 지표 수집 중...")
                    macro_ctx = _build_macro_context_for_summary()

                    st.write("✨ Gemini가 리포트를 작성하는 중... (약 20~30초 소요)")
                    summary_md = generate_weekly_portfolio_summary(
                        portfolio_ctx, narrative_ctx, macro_ctx
                    )

                    if not summary_md or summary_md.startswith("Weekly Summary 생성 실패"):
                        weekly_status.update(label="❌ 리포트 생성 실패", state="error")
                        st.error(summary_md or "알 수 없는 오류가 발생했습니다.")
                    else:
                        st.write("💾 Narratives 시트에 저장 중...")
                        ok_save, err_save = _save_weekly_summary_to_narratives(uid_weekly, summary_md)
                        if not ok_save:
                            st.warning(f"시트 저장 실패 (리포트는 아래에서 확인 가능): {err_save}")
                        weekly_status.update(label="✅ 주간 리포트 생성 완료!", state="complete", expanded=False)
                        st.session_state["_weekly_summary_latest"] = summary_md
                        st.rerun()

        # ── 생성된 리포트 표시 ─────────────────────────────────────────────
        # 1순위: 이번 세션에서 방금 생성한 것. 없으면 시트에서 가장 최근 저장본을 복원.
        latest_summary = st.session_state.get("_weekly_summary_latest", "")
        latest_saved_at = None
        if not latest_summary:
            try:
                _all_recs, _ = fetch_narrative_records_from_sheet()
                _mine = [
                    r for r in (_all_recs or [])
                    if str(r.get("_sheet_user_id", "")).strip().upper() == uid_weekly.upper()
                    and isinstance(r.get("analysis"), dict)
                    and r["analysis"].get("source") == "weekly_portfolio_summary"
                ]
                if _mine:
                    _latest_rec = max(
                        _mine,
                        key=lambda r: _narrative_parse_saved_at_utc(r.get("saved_at"))
                        or datetime.min.replace(tzinfo=timezone.utc),
                    )
                    latest_summary = _latest_rec["analysis"].get("summary", "")
                    latest_saved_at = _narrative_parse_saved_at_utc(_latest_rec.get("saved_at"))
            except Exception:
                latest_summary = ""

        if latest_summary:
            st.divider()
            if latest_saved_at is not None:
                _saved_str = latest_saved_at.astimezone(_MARKET_ET_TZ).strftime("%Y-%m-%d %H:%M")
                st.success(f"✅ 가장 최근 저장된 AI 포트폴리오 리포트 ({_saved_str} ET)")
            else:
                st.success("✅ 이번 주 AI 포트폴리오 리포트")
            st.markdown(latest_summary)
            st.caption("이 리포트는 `Narratives` 시트에 자동 저장됩니다. 과거 기록은 위의 '📚 이전 주간 요약 기록 보기'에서 볼 수 있어요. 투자 권유가 아닙니다.")

    elif main_nav == _MAIN_NAV_OPTIONS[7]:
        # ─────────────────────────────────────────────────────────────────────
        # 🔔 Buy Watchlist & Alert
        # 관심 종목 등록 + 매수 조건(목표가/RSI/200일선) 자동 체크
        # ─────────────────────────────────────────────────────────────────────
        st.subheader(_MAIN_NAV_OPTIONS[7])
        st.caption(
            "관심 종목을 등록하고 **매수 조건**을 설정하세요. "
            "앱 접속 시 조건이 자동으로 체크되며, 발동 시 상단에 알림이 표시됩니다."
        )

        uid_wl = str(st.session_state.get("user_id") or "").strip()

        # ── Alert 발동 현황 ────────────────────────────────────────────────
        triggered = st.session_state.get("_watchlist_triggered_alerts", [])
        if triggered:
            st.error(f"🔔 현재 **{len(triggered)}개 종목**에서 매수 조건이 발동됐어요!")
            for t in triggered:
                with st.expander(f"⚡ {t['ticker']} — 현재가 ${t['current_price']:.2f}" if pd.notna(t.get('current_price')) else f"⚡ {t['ticker']}", expanded=True):
                    for a in t["alerts"]:
                        st.markdown(f"- {a}")
            st.divider()

        # ── Watchlist 로드 ─────────────────────────────────────────────────
        wl_items = load_watchlist_sheet(uid_wl)

        # ── 종목 추가 폼 ───────────────────────────────────────────────────
        with st.expander("➕ 관심 종목 추가", expanded=not wl_items):
            # 손절/목표가 제안 미리보기 (폼 바깥 — 폼은 제출 시 초기화되므로)
            st.caption("💡 티커를 넣고 아래 **제안값 계산**을 누르면 ATR·200일선 기준 손절/목표가가 표시됩니다. 그 값을 참고해 폼에 직접 입력하세요.")
            _sug_c1, _sug_c2 = st.columns([2, 1])
            with _sug_c1:
                _sug_ticker = st.text_input(
                    "제안 계산용 티커",
                    placeholder="예: NVDA",
                    key="wl_suggest_ticker",
                ).strip().upper()
            with _sug_c2:
                _sug_go = st.button("📐 제안값 계산", key="wl_suggest_btn", use_container_width=True)
            if _sug_go and _sug_ticker:
                with st.spinner(f"{_sug_ticker} 손절/목표가 제안 계산 중..."):
                    _sug = suggest_stop_and_target(_sug_ticker)
                st.session_state["_wl_suggestion"] = {"ticker": _sug_ticker, **_sug}
            _sug_state = st.session_state.get("_wl_suggestion")
            if _sug_state and _sug_state.get("ticker"):
                _e = _sug_state.get("entry")
                if pd.notna(_e):
                    st.markdown(f"**{_sug_state['ticker']}** 현재가 기준 제안 (진입가 ${_e:.2f})")
                    _r1, _r2, _r3 = st.columns(3)
                    with _r1:
                        if pd.notna(_sug_state.get("stop_atr")):
                            st.metric(
                                "손절 (ATR 기준)",
                                f"${_sug_state['stop_atr']:.2f}",
                                delta=f"{_sug_state['stop_atr_pct']:+.1f}%",
                                delta_color="off",
                            )
                            st.caption("변동성(ATR×2) 기반. 변동 큰 종목일수록 멀게.")
                        else:
                            st.metric("손절 (ATR 기준)", "N/A")
                    with _r2:
                        if pd.notna(_sug_state.get("stop_ma200")):
                            st.metric(
                                "손절 (200일선 기준)",
                                f"${_sug_state['stop_ma200']:.2f}",
                                delta=f"{_sug_state['stop_ma200_pct']:+.1f}%" if pd.notna(_sug_state.get("stop_ma200_pct")) else None,
                                delta_color="off",
                            )
                            st.caption("추세선 바로 아래. 추세 붕괴 시 손절.")
                        else:
                            st.metric("손절 (200일선 기준)", "N/A")
                    with _r3:
                        if pd.notna(_sug_state.get("target_2r")):
                            st.metric(
                                "목표가 (손익비 1:2)",
                                f"${_sug_state['target_2r']:.2f}",
                                delta=f"{_sug_state['target_2r_pct']:+.1f}%",
                                delta_color="off",
                            )
                            st.caption("ATR 손절 거리의 2배.")
                        else:
                            st.metric("목표가 (손익비 1:2)", "N/A")
                    st.caption("⚠️ 제안값은 참고용입니다. 종목 특성을 보고 본인이 최종 손절/목표를 정하세요.")
                else:
                    st.warning(f"{_sug_state['ticker']} 가격 데이터를 불러오지 못해 제안값을 계산할 수 없습니다.")

            with st.form("watchlist_add_form", clear_on_submit=True):
                wl_col1, wl_col2 = st.columns([1, 2])
                with wl_col1:
                    wl_ticker = st.text_input(
                        "티커",
                        placeholder="예: NVDA",
                        key="wl_form_ticker",
                    ).strip().upper()
                with wl_col2:
                    wl_memo = st.text_input(
                        "메모 (매수 근거)",
                        placeholder="예: AI Capex 수혜, 어닝 모멘텀",
                        key="wl_form_memo",
                    )
                st.markdown("**Alert 조건 설정** (하나 이상 설정하세요)")
                al_col1, al_col2, al_col3 = st.columns(3)
                with al_col1:
                    wl_alert_price = st.number_input(
                        "💰 목표 매수가 ($) 이하",
                        min_value=0.0,
                        value=0.0,
                        step=0.5,
                        format="%.2f",
                        key="wl_form_alert_price",
                        help="현재가가 이 가격 이하로 내려오면 알림",
                    )
                with al_col2:
                    wl_alert_rsi = st.number_input(
                        "📉 RSI(14) 이하",
                        min_value=0.0,
                        max_value=100.0,
                        value=0.0,
                        step=1.0,
                        format="%.0f",
                        key="wl_form_alert_rsi",
                        help="RSI가 이 값 이하로 내려오면 알림 (예: 30 = 과매도)",
                    )
                with al_col3:
                    wl_alert_ma200 = st.checkbox(
                        "📊 200일선 ±3% 근접 시",
                        key="wl_form_alert_ma200",
                        help="현재가가 200일 이동평균선의 ±3% 이내에 들어오면 알림",
                    )

                st.markdown("**손절 / 목표가** (진입 전 미리 정하면 감정적 보유를 막아줍니다)")
                sl_col1, sl_col2 = st.columns(2)
                with sl_col1:
                    wl_stop_loss = st.number_input(
                        "🛑 손절가 ($)",
                        min_value=0.0,
                        value=0.0,
                        step=0.5,
                        format="%.2f",
                        key="wl_form_stop_loss",
                        help="여기까지 내려오면 자른다. 위 제안값을 참고하세요.",
                    )
                with sl_col2:
                    wl_target_price = st.number_input(
                        "🎯 목표가 ($)",
                        min_value=0.0,
                        value=0.0,
                        step=0.5,
                        format="%.2f",
                        key="wl_form_target_price",
                        help="익절 목표. 손익비 1:2 이상이면 기대값이 좋습니다.",
                    )
                submitted_wl = st.form_submit_button("Watchlist에 추가", type="primary", use_container_width=True)

            if submitted_wl:
                if not wl_ticker:
                    st.warning("티커를 입력해주세요.")
                else:
                    with st.spinner(f"{wl_ticker} 저장 중..."):
                        # ✅ 저장 시점 현재가를 캐시 우회하여 신선하게 조회
                        try:
                            fetch_latest_prices_for_tickers.clear()
                        except Exception:
                            pass
                        _wl_form_price = fetch_latest_prices_for_tickers((wl_ticker,)).get(wl_ticker, np.nan)
                        _sl_val = float(wl_stop_loss) if wl_stop_loss > 0 else None
                        _tp_val = float(wl_target_price) if wl_target_price > 0 else None
                        ok_wl, err_wl = add_to_watchlist(
                            uid_wl, wl_ticker,
                            memo=wl_memo.strip(),
                            alert_price=float(wl_alert_price) if wl_alert_price > 0 else None,
                            alert_rsi=float(wl_alert_rsi) if wl_alert_rsi > 0 else None,
                            alert_ma200=wl_alert_ma200,
                            saved_price=float(_wl_form_price) if pd.notna(_wl_form_price) else None,  # ✅ 명시적 전달
                            stop_loss=_sl_val,
                            target_price=_tp_val,
                        )
                    if ok_wl:
                        # 손익비(R:R) 피드백
                        _rr_msg = ""
                        if _sl_val and _tp_val and pd.notna(_wl_form_price):
                            _entry = float(_wl_form_price)
                            _risk = _entry - _sl_val
                            _reward = _tp_val - _entry
                            if _risk > 0 and _reward > 0:
                                _rr = _reward / _risk
                                if _rr >= 2:
                                    _rr_msg = f" · 손익비 1:{_rr:.1f} ✅ (기대값 양호)"
                                elif _rr >= 1:
                                    _rr_msg = f" · 손익비 1:{_rr:.1f} ⚠️ (1:2 이상 권장)"
                                else:
                                    _rr_msg = f" · 손익비 1:{_rr:.1f} 🔴 (위험이 보상보다 큼 — 재검토)"
                        st.success(f"✅ {wl_ticker} Watchlist에 추가했습니다!{_rr_msg}")
                        st.session_state.pop("_wl_suggestion", None)
                        st.rerun()
                    else:
                        st.error(f"저장 실패: {err_wl}")

        st.divider()

        # ── Watchlist 현황 ─────────────────────────────────────────────────
        if not wl_items:
            st.info("등록된 관심 종목이 없어요. 위에서 종목을 추가해주세요.")
        else:
            st.markdown(f"### 📋 관심 종목 현황 ({len(wl_items)}개)")
            wl_tickers = [i["ticker"] for i in wl_items]

            # ── 💰 등록가 일괄 복구 버튼 (saved_price 없는 종목 대상) ────────
            _missing_sp = [i for i in wl_items if pd.isna(pd.to_numeric(i.get("saved_price"), errors="coerce"))]
            if _missing_sp:
                st.warning(f"⚠️ **{len(_missing_sp)}개 종목**의 등록가(Saved Price)가 없어 '등록 시 대비'가 N/A로 표시됩니다.")
                if st.button("💰 등록가 현재가로 일괄 복구", key="bulk_fix_saved_price", type="secondary"):
                    with st.spinner("등록가 복구 중..."):
                        try:
                            fetch_latest_prices_for_tickers.clear()
                        except Exception:
                            pass
                        _fix_tks = tuple(i["ticker"] for i in _missing_sp)
                        _fix_prices = fetch_latest_prices_for_tickers(_fix_tks)
                        _fix_count = 0
                        for i in wl_items:
                            if pd.isna(pd.to_numeric(i.get("saved_price"), errors="coerce")):
                                _fp = _fix_prices.get(i["ticker"], np.nan)
                                if pd.notna(_fp):
                                    i["saved_price"] = float(_fp)
                                    _fix_count += 1
                        if _fix_count > 0:
                            _ok_fix, _err_fix = save_watchlist_sheet(uid_wl, wl_items)
                            if _ok_fix:
                                load_watchlist_sheet.clear()
                                st.success(f"✅ {_fix_count}개 종목 등록가 복구 완료!")
                                st.rerun()
                            else:
                                st.error(f"저장 실패: {_err_fix}")
                        else:
                            st.warning("복구할 가격 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해주세요.")

            with st.spinner("실시간 가격 및 지표 계산 중..."):
                price_map_wl = fetch_latest_prices_for_tickers(tuple(wl_tickers))
                rsi_map_wl, ma200_map_wl = {}, {}
                for tk in wl_tickers:
                    try:
                        hist = _fmp_price_history(tk, limit=252)
                        close = pd.to_numeric(hist["Close"], errors="coerce").dropna() if not hist.empty else pd.Series(dtype=float)
                        rsi_series = calculate_rsi(close).dropna()
                        rsi_map_wl[tk] = float(rsi_series.iloc[-1]) if not rsi_series.empty else np.nan
                        ma200_map_wl[tk] = float(close.rolling(200, min_periods=200).mean().iloc[-1]) if len(close) >= 200 else np.nan
                    except Exception:
                        rsi_map_wl[tk] = np.nan
                        ma200_map_wl[tk] = np.nan

            for idx, item in enumerate(wl_items):
                tk = item["ticker"]
                current_price = pd.to_numeric(price_map_wl.get(tk), errors="coerce")
                saved_price = pd.to_numeric(item.get("saved_price"), errors="coerce")
                rsi_val = pd.to_numeric(rsi_map_wl.get(tk), errors="coerce")
                ma200_val = pd.to_numeric(ma200_map_wl.get(tk), errors="coerce")
                pnl = (float(current_price) / float(saved_price) - 1.0) * 100.0 if pd.notna(current_price) and pd.notna(saved_price) and saved_price > 0 else np.nan

                # Alert 조건 체크
                item_alerts = check_watchlist_alerts([item], price_map_wl, rsi_map_wl, ma200_map_wl)
                alert_badge = "⚡ Alert 발동!" if item_alerts else ""

                with st.expander(
                    f"**{tk}** {alert_badge} | "
                    f"현재가 {'${:.2f}'.format(float(current_price)) if pd.notna(current_price) else 'N/A'} | "
                    f"RSI {'{:.1f}'.format(float(rsi_val)) if pd.notna(rsi_val) else 'N/A'} | "
                    f"등록 시 대비 {'{:+.1f}%'.format(pnl) if pd.notna(pnl) else 'N/A'}",
                    expanded=bool(item_alerts),
                ):
                    info_col, alert_col = st.columns([3, 2])
                    with info_col:
                        st.markdown(f"**메모:** {item.get('memo', '') or '없음'}")
                        st.markdown(f"**등록일:** {item.get('date_added', 'N/A')}")
                        st.markdown(f"**200일선:** {'${:.2f}'.format(float(ma200_val)) if pd.notna(ma200_val) else 'N/A'}")
                        _sl = pd.to_numeric(item.get("stop_loss"), errors="coerce")
                        _tp = pd.to_numeric(item.get("target_price"), errors="coerce")
                        if pd.notna(_sl) or pd.notna(_tp):
                            _sl_txt = f"🛑 손절 ${_sl:.2f}" if pd.notna(_sl) else "🛑 손절 미설정"
                            _tp_txt = f"🎯 목표 ${_tp:.2f}" if pd.notna(_tp) else "🎯 목표 미설정"
                            st.markdown(f"**{_sl_txt} · {_tp_txt}**")
                            # 손익비 표시
                            if pd.notna(_sl) and pd.notna(_tp) and pd.notna(saved_price) and saved_price > 0:
                                _rk = float(saved_price) - float(_sl)
                                _rw = float(_tp) - float(saved_price)
                                if _rk > 0 and _rw > 0:
                                    st.caption(f"손익비 1:{_rw/_rk:.1f} (등록가 기준)")
                            # 실시간 도달 경보
                            if pd.notna(current_price):
                                if pd.notna(_sl) and float(current_price) <= float(_sl):
                                    st.error(f"🛑 손절가 도달! 현재가 ${float(current_price):.2f} ≤ 손절 ${_sl:.2f} — 매도 검토")
                                elif pd.notna(_tp) and float(current_price) >= float(_tp):
                                    st.success(f"🎯 목표가 도달! 현재가 ${float(current_price):.2f} ≥ 목표 ${_tp:.2f} — 익절 검토")
                        else:
                            st.caption("🛑🎯 손절/목표가 미설정 — 편집에서 추가하면 도달 시 알림을 줍니다.")

                    with alert_col:
                        st.markdown("**설정된 Alert 조건:**")
                        ap = pd.to_numeric(item.get("alert_price"), errors="coerce")
                        ar = pd.to_numeric(item.get("alert_rsi"), errors="coerce")
                        am = item.get("alert_ma200", False)
                        if pd.notna(ap):
                            st.caption(f"💰 목표가: ${ap:.2f} 이하")
                        if pd.notna(ar):
                            st.caption(f"📉 RSI: {ar:.0f} 이하")
                        if am:
                            st.caption("📊 200일선 ±3% 근접")
                        if not pd.notna(ap) and not pd.notna(ar) and not am:
                            st.caption("조건 없음 (메모만)")

                    if item_alerts:
                        for a in item_alerts[0]["alerts"]:
                            st.success(a)

                    # Alert 조건 편집
                    with st.expander("✏️ Alert 조건 편집", expanded=False):
                        _edit_key = f"_wl_edit_{idx}_{tk}"
                        _ap_cur = float(ap) if pd.notna(ap) else 0.0
                        _ar_cur = float(ar) if pd.notna(ar) else 0.0
                        _am_cur = bool(am)

                        edit_c1, edit_c2 = st.columns(2)
                        with edit_c1:
                            new_ap = st.number_input(
                                "💰 목표 매수가 (0=사용 안 함)",
                                min_value=0.0, value=_ap_cur, step=1.0, format="%.2f",
                                key=f"edit_ap_{idx}_{tk}",
                            )
                            new_ar = st.number_input(
                                "📉 RSI 이하 시 알림 (0=사용 안 함)",
                                min_value=0.0, max_value=100.0, value=_ar_cur, step=5.0, format="%.0f",
                                key=f"edit_ar_{idx}_{tk}",
                            )
                        with edit_c2:
                            new_am = st.checkbox(
                                "📊 200일선 ±3% 근접 시 알림",
                                value=_am_cur,
                                key=f"edit_am_{idx}_{tk}",
                            )
                            new_memo = st.text_input(
                                "📝 메모 수정",
                                value=str(item.get("memo", "") or ""),
                                key=f"edit_memo_{idx}_{tk}",
                            )

                        _sl_cur = float(_sl) if pd.notna(_sl) else 0.0
                        _tp_cur = float(_tp) if pd.notna(_tp) else 0.0
                        edit_sl_c1, edit_sl_c2 = st.columns(2)
                        with edit_sl_c1:
                            new_sl = st.number_input(
                                "🛑 손절가 (0=미설정)",
                                min_value=0.0, value=_sl_cur, step=0.5, format="%.2f",
                                key=f"edit_sl_{idx}_{tk}",
                            )
                        with edit_sl_c2:
                            new_tp = st.number_input(
                                "🎯 목표가 (0=미설정)",
                                min_value=0.0, value=_tp_cur, step=0.5, format="%.2f",
                                key=f"edit_tp_{idx}_{tk}",
                            )

                        if st.button("💾 저장", key=f"wl_edit_save_{idx}_{tk}", type="primary", use_container_width=True):
                            # 기존 항목 삭제 후 새 항목 추가 (행 단위 처리)
                            # ⚠️ saved_price/stop/target은 재추가 시 보존해야 함
                            _keep_saved = pd.to_numeric(item.get("saved_price"), errors="coerce")
                            _ok_del, _ = delete_from_watchlist(uid_wl, tk)
                            if _ok_del:
                                _ok_add, _err_add = add_to_watchlist(
                                    uid_wl, tk,
                                    memo=new_memo.strip(),
                                    alert_price=float(new_ap) if new_ap > 0 else None,
                                    alert_rsi=float(new_ar) if new_ar > 0 else None,
                                    alert_ma200=bool(new_am),
                                    saved_price=float(_keep_saved) if pd.notna(_keep_saved) else None,
                                    stop_loss=float(new_sl) if new_sl > 0 else None,
                                    target_price=float(new_tp) if new_tp > 0 else None,
                                )
                                if _ok_add:
                                    st.rerun()
                                else:
                                    st.error(f"저장 실패: {_err_add}")
                            else:
                                st.error("기존 항목 삭제 실패")

                    btn_col1, btn_col2 = st.columns(2)
                    with btn_col1:
                        def _goto_analysis(ticker=tk):
                            st.session_state["selected_ticker"] = ticker
                            st.session_state["main_sidebar_nav"] = _MAIN_NAV_OPTIONS[5]
                        st.button(
                            f"📌 {tk} 분석하기",
                            key=f"wl_pick_{idx}",
                            use_container_width=True,
                            on_click=_goto_analysis,
                        )
                    with btn_col2:
                        def _do_wl_delete(tk_del=tk, uid_del=uid_wl):
                            _ok_d, _err_d = delete_from_watchlist(uid_del, tk_del)
                        st.button(
                            f"🗑️ 삭제",
                            key=f"wl_del_{idx}",
                            use_container_width=True,
                            on_click=_do_wl_delete,
                        )


            # Alert 재체크 버튼
            st.divider()
            if st.button("🔄 Alert 조건 다시 체크", key="wl_recheck_btn", use_container_width=True):
                st.session_state["_watchlist_alert_checked"] = False
                st.session_state.pop("_watchlist_triggered_alerts", None)
                st.rerun()

            st.divider()
            st.markdown("### 🗑️ Watchlist 전체 초기화")
            st.caption("등록된 모든 종목을 삭제합니다. Alert 조건은 🔔 Buy Watchlist & Alert 탭에서 새로 추가하세요.")
            if st.button("🗑️ Watchlist 전체 삭제", key="wl_clear_all_btn", use_container_width=True):
                st.session_state["_wl_clear_confirm"] = True

            if st.session_state.get("_wl_clear_confirm"):
                st.error("⚠️ 정말 모두 삭제할까요? 이 작업은 되돌릴 수 없어요.")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("✅ 네, 전부 삭제", key="wl_clear_yes", use_container_width=True, type="primary"):
                        ok_clear, err_clear = save_watchlist_sheet(uid_wl, [])
                        if ok_clear:
                            st.session_state.pop("_wl_clear_confirm", None)
                            load_watchlist_sheet.clear()
                            st.session_state.pop("_sidebar_wl_count", None)
                            st.session_state["_watchlist_alert_checked"] = False
                            st.success("✅ Watchlist를 모두 삭제했어요!")
                            st.rerun()
                        else:
                            st.error(f"삭제 실패: {err_clear}")
                with c2:
                    if st.button("❌ 취소", key="wl_clear_no", use_container_width=True):
                        st.session_state.pop("_wl_clear_confirm", None)
                        st.rerun()

            # ── saved_price 일괄 복구 ──────────────────────────────────────
            missing_price = [i for i in wl_items if pd.isna(pd.to_numeric(i.get("saved_price"), errors="coerce"))]
            if missing_price:
                st.warning(f"⚠️ **{len(missing_price)}개 종목**의 등록 시 가격이 없어서 수익률이 0%로 표시돼요.")
                if st.button("💰 현재가로 등록 가격 일괄 복구", key="wl_fix_price_btn", type="primary", use_container_width=True):
                    with st.spinner("현재가 조회 중..."):
                        fix_tickers = tuple(i["ticker"] for i in missing_price)
                        fix_price_map = fetch_latest_prices_for_tickers(fix_tickers)
                    fixed_count = 0
                    updated_items = []
                    for item in wl_items:
                        tk = item["ticker"]
                        if pd.isna(pd.to_numeric(item.get("saved_price"), errors="coerce")):
                            new_price = fix_price_map.get(tk, np.nan)
                            if pd.notna(new_price):
                                item = dict(item)
                                item["saved_price"] = float(new_price)
                                fixed_count += 1
                        updated_items.append(item)
                    ok_fix, err_fix = save_watchlist_sheet(uid_wl, updated_items)
                    if ok_fix:
                        st.success(f"✅ {fixed_count}개 종목의 등록 가격을 현재가로 복구했어요!")
                        st.session_state.pop("_sidebar_wl_count", None)
                        st.rerun()
                    else:
                        st.error(f"저장 실패: {err_fix}")

    elif main_nav == _MAIN_NAV_OPTIONS[11]:
        render_sync_button("sync_tab_emerging", [], "Emerging 추적 데이터를 다시 불러옵니다.")
        # ─────────────────────────────────────────────────────────────────────
        # 📡 Emerging 종목 추적기
        # ─────────────────────────────────────────────────────────────────────
        st.subheader("📡 Emerging 종목 추적기")
        st.caption(
            "AI 내러티브 분석 후 Emerging 종목 검증을 실행할 때마다 자동으로 기록이 쌓입니다. "
            "같은 종목이 반복 등장할수록 신뢰도 높은 신호예요."
        )

        uid_et = str(st.session_state.get("user_id") or "").strip()

        with st.spinner("Emerging 추적 기록 불러오는 중..."):
            et_df = load_emerging_tracker(uid_et)
            et_df = _augment_emerging_with_freshness(et_df)

        if et_df.empty:
            st.info(
                "아직 추적 기록이 없어요. "
                "[1단계] 시장 내러티브에서 AI 분석 후 **Emerging 종목 검증 실행** 버튼을 누르면 "
                "자동으로 기록이 쌓입니다."
            )
        else:
            # ── 요약 지표 ──────────────────────────────────────────────────
            total = len(et_df)
            hot = (et_df["Count"].astype(int) >= 3).sum() if "Count" in et_df.columns else 0
            new_ones = (et_df["Status"].str.contains("신규", na=False)).sum()
            # 매수 신호: 검증 라벨이 최적/얼리버드 AND 최근 10일 내 등장(신선)만
            _is_stale_col = et_df["_is_stale"] if "_is_stale" in et_df.columns else pd.Series(False, index=et_df.index)
            best_ones = et_df[
                et_df["Best_Verdict"].str.contains("최적|얼리버드", na=False)
                & (~_is_stale_col.fillna(False).astype(bool))
            ]
            stale_count = int(_is_stale_col.fillna(False).astype(bool).sum())

            m1, m2, m3, m4 = st.columns(4)
            with m1:
                st.metric("전체 추적 종목", f"{total}개")
            with m2:
                st.metric("🔥 반복 등장 (3회+)", f"{hot}개")
            with m3:
                st.metric("🆕 신규 등장", f"{new_ones}개")
            with m4:
                st.metric("🎯 매수 신호 (신선)", f"{len(best_ones)}개")

            if stale_count > 0:
                st.caption(
                    f"⏳ {stale_count}개 종목은 마지막 등장이 {_EMERGING_STALE_DAYS}일을 넘어 '오래된 신호'로 분류됐어요. "
                    "매수 신호 섹션에서는 제외되며, 전체 목록에는 배지와 함께 남습니다."
                )

            # ── 최우선 관심 종목 ───────────────────────────────────────────
            if not best_ones.empty:
                st.divider()
                st.markdown("### 🎯 매수 신호 종목 (최적/얼리버드 · 최근 10일 내)")
                st.caption("정량 검증에서 '최적 매수 타이밍' 또는 '얼리버드 기회'로 분류된 **신선한** 종목들이에요. RS 값은 등장 당시 검증값입니다.")
                for _, row in best_ones.sort_values("Count", ascending=False).iterrows():
                    count = int(row["Count"]) if str(row["Count"]).isdigit() else 1
                    rs_str = f"RS {float(row['RS_Score']):.1f}%p(당시)" if row["RS_Score"] else ""
                    _days_old = row.get("_days_old", np.nan)
                    _fresh = f"{_days_old:.0f}일 전" if pd.notna(_days_old) else row["Last_Seen"]
                    st.markdown(
                        f"**{row['Ticker']}** — {row['Best_Verdict']} | "
                        f"등장 **{count}회** | {rs_str} | 최근: {_fresh}"
                    )
                    col_a, col_b = st.columns([1, 4])
                    with col_a:
                        def _goto_stock(tk=row["Ticker"]):
                            st.session_state["selected_ticker"] = tk
                            st.session_state["main_sidebar_nav"] = _MAIN_NAV_OPTIONS[5]
                        st.button(f"🔬 {row['Ticker']} 분석", key=f"et_goto_{row['Ticker']}", on_click=_goto_stock)

            # ── 반복 등장 종목 ─────────────────────────────────────────────
            st.divider()
            st.markdown("### 🔥 반복 등장 종목 (관심 집중)")

            hot_df = et_df[et_df["Count"].astype(int) >= 2].sort_values("Count", ascending=False)
            if hot_df.empty:
                st.info("아직 2회 이상 등장한 종목이 없어요. 내러티브 분석을 더 진행하면 쌓입니다.")
            else:
                def _style_count(val):
                    v = int(val) if str(val).isdigit() else 0
                    if v >= 5: return "color:#dc2626;font-weight:700"
                    if v >= 3: return "color:#f97316;font-weight:600"
                    return "color:#16a34a"

                def _style_verdict(val):
                    if "최적" in str(val): return "color:#16a34a;font-weight:700"
                    if "얼리" in str(val): return "color:#0ea5e9;font-weight:600"
                    return ""

                def _freshness_badge(days_old):
                    if pd.isna(days_old):
                        return "—"
                    d = float(days_old)
                    if d > _EMERGING_STALE_DAYS:
                        return f"⏳ 오래됨 ({d:.0f}일 전)"
                    if d <= 2:
                        return f"🟢 최신 ({d:.0f}일 전)"
                    return f"🟡 {d:.0f}일 전"

                hot_disp = hot_df.copy()
                hot_disp["신선도"] = hot_disp["_days_old"].apply(_freshness_badge)

                def _style_freshness(val):
                    if "오래됨" in str(val): return "color:#9ca3af;font-style:italic"
                    if "최신" in str(val): return "color:#16a34a;font-weight:600"
                    return "color:#f59e0b"

                styled_hot = (
                    hot_disp[["Ticker", "Theme", "First_Seen", "Last_Seen", "신선도", "Count", "Best_Verdict", "RS_Score", "Status"]]
                    .style
                    .map(_style_count, subset=["Count"])
                    .map(_style_verdict, subset=["Best_Verdict"])
                    .map(_style_freshness, subset=["신선도"])
                )
                st.dataframe(styled_hot, use_container_width=True, hide_index=True)
                st.caption("RS_Score는 종목이 마지막으로 검증된 **당시 값**입니다(실시간 아님). '오래됨' 종목은 매수 판단에서 제외하세요.")

            # ── 전체 목록 ──────────────────────────────────────────────────
            st.divider()
            st.markdown("### 📋 전체 추적 목록")
            with st.expander("전체 보기", expanded=False):
                _full = et_df.sort_values("Count", ascending=False).copy()
                if "_days_old" in _full.columns:
                    _full["경과일"] = _full["_days_old"].apply(
                        lambda d: f"{d:.0f}일 전" if pd.notna(d) else "—"
                    )
                _drop = [c for c in ("_days_old", "_is_stale") if c in _full.columns]
                st.dataframe(
                    _full.drop(columns=_drop),
                    use_container_width=True, hide_index=True
                )

            # ── 개별 삭제 ──────────────────────────────────────────────────
            st.divider()
            st.markdown("### 🗑️ 종목 삭제")
            del_ticker = st.selectbox(
                "삭제할 종목 선택",
                options=[""] + et_df["Ticker"].tolist(),
                key="et_del_select"
            )
            if del_ticker and st.button("🗑️ 선택 종목 삭제", key="et_del_btn"):
                ok_del, err_del = delete_emerging_tracker_row(uid_et, del_ticker)
                if ok_del:
                    st.success(f"✅ {del_ticker} 삭제 완료")
                    st.rerun()
                else:
                    st.error(f"삭제 실패: {err_del}")

    elif main_nav == _MAIN_NAV_OPTIONS[12]:
        # ─────────────────────────────────────────────────────────────────────
        # 📖 사용 가이드
        # ─────────────────────────────────────────────────────────────────────
        st.title("📖 사용 가이드")
        st.caption("Quant Investment Terminal — 전체 기능 및 워크플로우 안내")

        _uid_guide = str(st.session_state.get("user_id") or "").strip()
        _role_guide = st.session_state.get("user_role", "guest")

        # ── 전체 워크플로우 개요 ──────────────────────────────────────────────
        st.markdown("## 🗺️ 투자 의사결정 워크플로우 (Top-Down)")
        st.info(
            "**권장 순서:** 🚨 Daily Risk → 🌐 거시경제 → 📰 내러티브 → 🎯 섹터 → "
            "🚀 종목 스캐너 → 🔬 정밀 검사 → 🛡️ 포트폴리오 관리\n\n"
            "매일 아침 ① Daily Risk Gauge 먼저 확인 → 위험이 낮으면 ② 내러티브 분석 실행 "
            "→ AI가 추천한 섹터·종목을 ③ 스캐너로 검증 → ④ 정밀 검사 후 투자 결정"
        )

        # ── 섹션별 탭 구성 ────────────────────────────────────────────────────
        _g_tab0, _g_tab1, _g_tab2, _g_tab3, _g_tab4 = st.tabs([
            "🚀 시작하기",
            "📊 분석 도구",
            "💼 포트폴리오",
            "🤖 AI 인사이트",
            "❓ FAQ",
        ])

        # ════════════════════════════════════════════════════════════════════
        with _g_tab0:
            st.markdown("### 🚀 처음 시작하는 분들을 위한 2단계 셋업")

            with st.expander("**STEP 1 — 포트폴리오 종목 등록**", expanded=False):
                st.markdown("""
**[4단계] 포트폴리오 매도 레이더** 탭에서:
1. `계좌명(Account)` 입력 (예: Robinhood, Fidelity, ISA)
2. `티커(Ticker)` 입력 (예: AAPL, TSLA, 005930.KS)
3. `매수가`, `수량` 입력 후 **"종목 추가"** 클릭
4. Google Sheets `Portfolio` 탭에 자동 저장 — 모든 기기에서 실시간 동기화

> 💡 국내 주식도 `.KS` 접미사로 지원합니다 (예: `005930.KS` = 삼성전자)
""")

            with st.expander("**STEP 2 — 첫 AI 내러티브 분석 실행**", expanded=False):
                st.markdown("""
**[1단계] 시장 내러티브** 탭에서:
1. 분석 타입 선택: `오늘의 최신 내러티브` 또는 `주간 메가 트렌드`
2. **"🤖 AI 내러티브 분석 실행"** 버튼 클릭
3. AI가 Winners(주도주)·Emerging(급부상 종목)·Sector Rotation을 자동 분석
4. 결과가 `Narratives` 시트에 자동 저장 → 이후 모든 AI 탭에서 활용

> ⏱️ 최초 분석 소요 시간: 약 30~60초
""")

            st.divider()
            with st.expander("### 📅 일일 루틴 추천", expanded=False):
                st.markdown("""
| 시간 | 탭 | 행동 |
|---|---|---|
| 장 열기 전 (오전 9시 이전) | 🚨 Daily Risk Gauge | 선행 신호 6개 + 시장 위험도 확인 |
| 장 중 | 📰 [1단계] 시장 내러티브 | AI 내러티브 분석 → Winners/Emerging 파악 |
| 장 중 | 🎯 [2단계] 섹터 & 자금 흐름 | 자금이 몰리는 섹터 확인 |
| 관심 종목 발생 시 | 🔬 [3단계] 정밀 검사 | 개별 종목 펀더멘털 + 기술적 분석 |
| 주 1회 | 📋 [AI] 주간 포트폴리오 요약 | 전체 포트폴리오 AI 리뷰 |
""")

        # ════════════════════════════════════════════════════════════════════
        with _g_tab1:
            st.markdown("### 📊 분석 도구 탭 상세 가이드")

            with st.expander("🚨 **Daily Risk Gauge** — 매일 첫 확인", expanded=False):
                st.markdown("""
**목적:** 당일 시장 진입 가능 여부를 빠르게 판단

**선행 신호 6가지:**
- 🌙 **VIX 공포지수** — 20 이하: 안전 / 30 이상: 위험
- 📈 **선물 프리마켓** (S&P500·나스닥) — 장 열기 전 방향성 확인
- 💵 **달러 인덱스(DXY)** — 강달러 = 신흥국·원자재 압박
- 📉 **국채 수익률** — 10Y·2Y 스프레드로 경기침체 선행 신호 포착
- 🌐 **글로벌 시장** — 전일 아시아·유럽 마감 분위기
- 📅 **경제 이벤트 캘린더** — FOMC·CPI 등 고임팩트 발표 감지

**오늘 투자해도 될까?** 버튼으로 AI가 6가지 신호를 종합해 매매 가능 여부 판단

> ⚡ 데이터는 1시간 캐시. 강제 새로고침은 화면 상단 **🔄 데이터 새로고침** 버튼
""")

            with st.expander("🌐 **[1단계] 거시경제 지표**", expanded=False):
                st.markdown("""
**미국 거시경제 8대 지표 자동 수집·판독**

| 지표 | 데이터 소스 | 판독 기준 |
|---|---|---|
| 기준금리 (Fed Funds) | FRED / FMP | 5% 이상 = 고금리 레짐 |
| 국채 수익률 (10Y, 2Y) | FRED / FMP | 역전 = 침체 선행 신호 |
| CPI 인플레이션 | FRED / FMP | 3% 이상 = 위험 |
| 실업률 | FRED / FMP | 4.5% 이상 = 경기 둔화 |
| GDP 성장률 | FMP | 2% 이하 = 경기 둔화 |
| 소매판매 | FMP | 전월 대비 감소 = 소비 위축 |
| VIX 공포지수 | FRED / FMP | 25 이상 = 위험 선호 저하 |
| 달러 인덱스 | FRED / FMP(UUP) | 52주 평균 대비 +5.5% 이상 = 달러 과강세 |

**📊 Macro Score:** 8개 지표 합산 점수 (Pass/Warning/Fail)  
**📈 2년 히스토리 차트:** 지표 추이를 시각화해 트렌드 파악
""")

            with st.expander("📰 **[1단계] 시장 내러티브**", expanded=False):
                st.markdown("""
**AI가 뉴스·시장 데이터를 분석해 투자 테마를 추출**

**분석 타입:**
- `오늘의 최신 내러티브` — 당일 뉴스 기반 단기 모멘텀 분석
- `주간 메가 트렌드` — 최근 7일 흐름 기반 스윙/중기 투자용

**분석 결과 구성:**
1. **Market Regime** — 현재 시장 국면 (Risk-On / Risk-Off / Neutral)
2. **테마별 내러티브** — AI가 추출한 핵심 투자 테마 (2~5개)
3. **Winners** — 각 테마에서 주목할 선도 종목
4. **Emerging** — 급부상 중인 초기 단계 종목
5. **Sector Rotation Map** — 자금이 이동하는 섹터 시각화
6. **내러티브 일관성 점수** — 이전 분석과의 연속성 평가

**Emerging 종목 정량 교차 검증:** Winners/Emerging 종목을 RSI·RS·거래량으로 자동 검증  
**시계열 분석 엔진:** 특정 내러티브가 얼마나 지속됐는지 추적  

> 💾 분석 결과는 `Narratives` 시트에 자동 저장 (최근 40개 보관)
""")

            with st.expander("🎯 **[2단계] 섹터 & 자금 흐름**", expanded=False):
                st.markdown("""
**11개 GICS 섹터 ETF 상대 강도 분석**

- **섹터 ETF 강도 순위** — XLK·XLV·XLE 등 섹터별 1개월 수익률 비교
- **섹터별 P/E Snapshot** — 고평가/저평가 섹터 한눈에 파악
- **Alpha Leaders** — 선택 섹터 내 실제 주도주 발굴
- **Laggards** — 낙폭과대 반등 후보

**추가 레이더:**
- 🚀 **Hidden Alpha Radar** — 새로운 주도 테마 발굴 (섹터 이탈 강세주)
- 🌱 **Early Signal Radar** — 막 강해지기 시작한 섹터 선제 감지
- 🛡️ **섹터 꺾임 감지** — 고점 이탈 섹터 → 매도 타이밍 포착
""")

            with st.expander("🚀 **[2단계] AI 종목 스캐너**", expanded=False):
                st.markdown("""
**내러티브 DB의 Winners·Emerging 종목을 자동 스캔**

**데이터 소스 선택:**
- `오늘의 최신 내러티브` — 당일 분석 결과의 Winners/Emerging
- `주간 메가 트렌드` — 7일 누적 상위 종목
- `수동 섹터/티커 입력` — 직접 종목 지정

**듀얼 엔진 스캔 결과:**
- **Current Leaders** — RSI·모멘텀·거래량 기반 현재 주도주
- **Emerging Opportunities** — 초기 부상 중인 기회 종목

**🔬 종목/테마 X-Ray:** 특정 종목·섹터에 대한 심층 AI 분석  
**📊 내러티브 팩트 체크:** 과거 AI 추천 종목의 실제 수익률 검증
""")

            with st.expander("🔬 **[3단계] 개별 종목 정밀 검사**", expanded=False):
                st.markdown("""
**사이드바에서 티커 입력 후 분석 실행**

**펀더멘털 분석:**
- 시가총액·P/E·P/B·PEG·ROE·부채비율 등 핵심 KPI
- **투자 스타일 적합도** — 성장주/가치주/배당주/모멘텀 중 어디에 해당하는지
- **DCF 내재가치 계산** — 현재가 대비 안전마진 산출
- **어닝 서프라이즈 히스토리** — 최근 8분기 EPS 실제 vs 예상

**기술적 분석:**
- RSI(14), MACD, 볼린저밴드, 52주 고점/저점 대비
- 200일선·50일선 위치 확인
- 거래량 이상 감지

**추가 데이터:**
- 📞 어닝콜 트랜스크립트 AI 요약 (FMP)
- 🏦 기관 투자자 보유 현황 변화
- 🕵️ 인사이더 트레이딩 내역
- 🎯 애널리스트 목표주가 컨센서스
- 🏛️ 상원/하원 의원 거래 내역

**🤖 AI 종합 진단:** 펀더멘털+기술적 신호를 종합한 매수/관망/매도 의견
""")

        # ════════════════════════════════════════════════════════════════════
        with _g_tab2:
            st.markdown("### 💼 포트폴리오 관리 탭 상세 가이드")

            with st.expander("🛡️ **[4단계] 포트폴리오 매도 레이더**", expanded=False):
                st.markdown("""
**포트폴리오 전체 현황 + 매도 신호 자동 감지**

#### 📋 포트폴리오 관리
- **계좌별 등록** — Robinhood·Fidelity 등 여러 계좌를 분리 관리
- **종목 추가/삭제** — 티커·매수가·수량 입력
- **실시간 손익** — 현재가 자동 조회 → 투자 손익($) 및 수익률(%) 계산
- **매도 기록** — 매도한 종목의 실현 손익 추적

#### 📊 포트폴리오 분석 (고급)
- **Correlation Matrix** — 보유 종목 간 상관관계 (분산투자 체크)
- **수익률 히스토리** — 매수가 기준 누적 수익 추이
- **Personal Benchmark 비교** — 내 포트폴리오 vs SPY·QQQ 성과 비교 (100 normalized)

#### 📅 보유 종목 실적 발표 캘린더
보유 종목의 다음 실적 발표일 자동 조회:
- **빨간 🔴**: D-30일 이내 임박 → 변동성 대비 필요
- **노란 🟡**: 예정 (30일 이후)
- **초록 ✅**: 발표 완료
- EPS 예상치 표시 (FMP 제공 시)

#### 🎯 매도 타이밍 레이더
RSI·MACD·52주 고점·200일선 4가지 기술적 신호로 **위험점수** 산출:

| 신호 등급 | 기준 | 의미 |
|---|---|---|
| 🔴 매도 검토 | 위험점수 3점+ | RSI 과매수 + 복수 기술적 경고 |
| 🟡 주의 | 위험점수 1~2점 | 일부 신호 경고 |
| 🟢 유지 | 위험점수 0점 | 이상 신호 없음 |

**🤖 AI 매도 우선순위:** 기술적 신호 + 펀더멘털을 AI가 종합해 매도 우선순위 제시
""")

            with st.expander("🔔 **Buy Watchlist & Alert**", expanded=False):
                st.markdown("""
**관심 종목 등록 + 매수 조건 자동 체크**

#### 등록 방법
1. 사이드바 티커 입력창에 종목 입력
2. 투자 메모 작성 (선택)
3. Alert 조건 설정:
   - **목표 RSI** — RSI가 지정값 이하로 하락 시 알림 (예: 30 이하 → 과매도 매수 기회)
   - **200일선 하향 돌파** — 200일선 아래로 내려갈 때 알림
4. **"현재 티커 저장"** 클릭

#### Watchlist 탭에서
- **Alert 조건 충족 시** — 🔴 자동 하이라이트
- 각 종목 클릭 → 바로 [3단계] 정밀 검사로 이동
- 목표가·메모 직접 편집 가능

> 💡 정기적으로 Watchlist를 관리해 관심 종목이 매수 조건에 들어올 때 빠르게 대응하세요.
""")

        # ════════════════════════════════════════════════════════════════════
        with _g_tab3:
            st.markdown("### 🤖 AI 인사이트 탭 상세 가이드")

            with st.expander("📊 **[AI] 내러티브 적중률 트래커**", expanded=False):
                st.markdown("""
**AI 내러티브 분석의 실제 성과를 검증하는 팩트체크 시스템**

#### 작동 방식
1. 과거 내러티브 분석 시 기록된 Winners·Emerging 종목 불러오기
2. 분석 시점으로부터 현재까지 실제 수익률 계산
3. 종목별·테마별·월별 적중률 통계 제공

#### 주요 지표
- **평균 수익률** — AI가 추천한 종목들의 평균 성과
- **적중률(%)** — 플러스 수익률을 기록한 비율
- **Best 5 / Worst 5** — 가장 성공한/실패한 AI 추천 종목

> 이 탭을 통해 어떤 내러티브 타입이 더 신뢰할 만한지, 어떤 섹터에서 AI가 강한지 파악할 수 있습니다.
""")

            with st.expander("💡 **[AI] Idea-to-Portfolio 추적**", expanded=False):
                st.markdown("""
**내러티브 분석에서 발굴한 아이디어가 실제 포트폴리오에 편입됐는지 추적**

AI가 추천한 Winners·Emerging 종목 중:
- 포트폴리오에 **이미 보유 중**인 종목 → ✅ 표시
- 아직 **미편입** 종목 → 빠른 검사 버튼으로 바로 분석
- **실현 손익**과 AI 추천 이후 수익률 비교

> 💡 AI의 인사이트를 실제 투자로 연결하는 파이프라인을 관리합니다.
""")

            with st.expander("📋 **[AI] 주간 포트폴리오 요약**", expanded=False):
                st.markdown("""
**주 1회 실행 — 포트폴리오 전체에 대한 AI 주간 리뷰 리포트 자동 생성**

#### 리포트 포함 내용
- 현재 포트폴리오 전체 손익 현황
- 최근 AI 내러티브와 보유 종목의 연관성 분석
- 현재 거시경제 환경에서의 포트폴리오 리스크 평가
- 주간 투자 전략 제언 (매도 검토 / 추가 매수 관심 / 유지)

#### 사용 방법
1. **[4단계] 포트폴리오 매도 레이더**를 먼저 열어 수익률 데이터 동기화
2. **"🤖 주간 AI 리포트 생성"** 버튼 클릭
3. 리포트가 `Narratives` 시트에 자동 저장 (이전 기록도 보관)

> ⏱️ 생성 소요 시간: 약 20~30초
""")

            with st.expander("📡 **[AI] Emerging 종목 추적기**", expanded=False):
                st.markdown("""
**Emerging 종목이 반복 등장할수록 신뢰도 높은 매수 신호**

#### 작동 방식
- [1단계] 시장 내러티브 분석 후 **"Emerging 종목 검증 실행"** 클릭 시 자동 기록
- 같은 종목이 여러 분석에서 반복 등장할 때 추적

#### 신호 등급
| 등장 횟수 | 의미 | 색상 |
|---|---|---|
| 1회 | 관심 후보 | 초록 |
| 2~4회 | 반복 등장 — 관심 집중 | 주황 |
| 5회+ | 강한 모멘텀 신호 | 빨강 |

#### Best Verdict 분류
- **최적 매수 타이밍** — RSI·RS 지표가 매수 구간에 있는 종목
- **얼리버드 기회** — 아직 초기 단계지만 선취매 가능한 종목

> 💡 [3단계] 개별 종목 정밀 검사 버튼으로 바로 심층 분석 진행 가능
""")

        # ════════════════════════════════════════════════════════════════════
        with _g_tab4:
            st.markdown("### ❓ 자주 묻는 질문 (FAQ)")

            _faqs = [
                (
                    "Q. 실적 발표 캘린더에 데이터가 안 나와요",
                    """**원인:** FMP API 키가 없거나 해당 종목의 다음 실적 발표일이 아직 확정되지 않은 경우입니다.

**해결:**
1. `FMP_API_KEY`가 Secrets에 설정되어 있는지 확인
2. 실적 발표일이 확정된 종목만 표시됩니다 (보통 발표 3~4주 전부터 데이터 생성)
3. 🔄 새로고침 후 재시도""",
                ),
                (
                    "Q. AI 분석이 '생성 실패'로 뜹니다",
                    """**원인:** Anthropic API 키 오류 또는 일시적 서버 부하

**해결:**
1. `ANTHROPIC_API_KEY`가 유효한지 확인
2. 잠시 후(30초~1분) 재시도
3. 분석 소요 시간은 최대 60초 — 네트워크 상태 확인""",
                ),
                (
                    "Q. 포트폴리오에 추가했는데 다음에 들어오면 사라져요",
                    """**원인:** Google Sheets 연결 오류로 저장 실패

**해결:**
1. 종목 추가 시 "✅ 저장 완료" 메시지가 나오는지 확인
2. `SPREADSHEET_ID`와 서비스 계정 권한 확인
3. Google Sheet에서 `Portfolio` 탭을 직접 열어 데이터 존재 여부 확인""",
                ),
                (
                    "Q. Daily Risk Gauge의 선물 가격이 안 나와요",
                    """**원인:** 데이터 소스 Rate Limit 또는 장외 시간(데이터 없음)

**해결:**
1. 🔄 데이터 새로고침 버튼 클릭
2. 미국 프리마켓 시간(오후 4시~오전 4시 ET) 이외에는 선물 데이터가 제한될 수 있습니다
3. FMP_API_KEY 설정 시 FMP 선물 데이터로 자동 대체""",
                ),
                (
                    "Q. 한국 주식도 분석 가능한가요?",
                    """**가능합니다.** 티커 형식: `종목코드.KS`

예시:
- 삼성전자: `005930.KS`
- SK하이닉스: `000660.KS`
- NAVER: `035420.KS`

단, FMP 기반 데이터(기관 투자자·인사이더·어닝콜)는 한국 주식을 지원하지 않습니다.  
FMP 기반 기술적 분석·차트는 정상 작동합니다.""",
                ),
                (
                    "Q. ETF 분석도 되나요?",
                    """**됩니다.** ETF 티커를 [3단계] 정밀 검사에서 직접 입력하면:
- 섹터 배분·보유 종목 Top 10 자동 표시 (FMP `etf-holder` API)
- 기술적 분석 (RSI·MACD·차트) 정상 작동
- 펀더멘털(P/E 등)은 ETF 특성상 N/A로 표시될 수 있음

[2단계] AI 종목 스캐너에서도 ETF 유니버스를 기반으로 스캔합니다.""",
                ),
                (
                    "Q. 데이터는 얼마나 자주 업데이트되나요?",
                    """**실시간 데이터가 아닙니다.** 캐시 기반 운영:
- 주가 데이터: 1시간마다 갱신 (🔄 버튼으로 강제 갱신 가능)
- AI 내러티브: 버튼 클릭 시마다 새로 생성
- Google Sheets (포트폴리오·Watchlist): 즉시 동기화

Streamlit Community Cloud 환경의 특성상 동시 접속자가 많으면 응답이 느려질 수 있습니다.""",
                ),
            ]

            for _q, _a in _faqs:
                with st.expander(_q, expanded=False):
                    st.markdown(_a)

            st.divider()
            st.markdown("### 📬 피드백 & 버그 리포트")
            st.info(
                "기능 요청이나 버그는 포트폴리오 탭 하단 메모 기능 또는 "
                "관리자에게 직접 문의해 주세요.\n\n"
                "**앱 버전:** v2.0 (2025) | "
                "**Tech Stack:** Streamlit · Google Sheets · FMP · Claude AI"
            )

    elif main_nav == _NAV_ADMIN_APPROVAL:
        st.subheader(_NAV_ADMIN_APPROVAL)
        st.caption(
            "`Quant_DB` → `Users` 탭과 동기화됩니다. **Status**는 아래 목록만 선택 가능하며, 저장 시 구글 시트 전체가 덮어씌워집니다."
        )
        ws, err = open_users_worksheet()
        if err:
            st.error(err)
        else:
            df_users = fetch_users_dataframe(ws)
            _status_opts = ["pending", "approved", "rejected"]
            if "Status" in df_users.columns:
                df_users["Status"] = (
                    df_users["Status"]
                    .astype(str)
                    .str.strip()
                    .str.lower()
                    .apply(lambda s: s if s in _status_opts else "pending")
                )
            _pending_n = (
                (df_users["Status"] == "pending").sum()
                if "Status" in df_users.columns and not df_users.empty
                else 0
            )
            st.caption(f"현재 **{len(df_users)}**명 행 · pending **{_pending_n}**")
            edited_df = st.data_editor(
                df_users,
                num_rows="fixed",
                use_container_width=True,
                hide_index=True,
                key="admin_users_data_editor",
                column_config={
                    "Status": st.column_config.SelectboxColumn(
                        "Status",
                        help="승인 상태를 선택하세요.",
                        options=_status_opts,
                        required=True,
                    ),
                },
            )
            if st.button("변경사항 DB에 저장", key="admin_save_users_sheet_btn", type="primary", use_container_width=True):
                ok_save, msg_save = save_users_worksheet_from_df(edited_df)
                if ok_save:
                    st.success("Users 시트가 업데이트되었습니다.")
                    st.rerun()
                else:
                    st.error(msg_save)
