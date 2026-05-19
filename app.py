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
import yfinance as yf
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

if "_yf_cloud_limit_warn_shown" not in st.session_state:
    st.session_state["_yf_cloud_limit_warn_shown"] = False


def _notify_yfinance_fetch_failed() -> None:
    """클라우드 등에서 Yahoo 제한 시 한 세션당 한 번만 안내."""
    if st.session_state.get("_yf_cloud_limit_warn_shown"):
        return
    st.session_state["_yf_cloud_limit_warn_shown"] = True
    st.warning("야후 파이낸스 접속 제한으로 일부 데이터를 불러오지 못했습니다.")


_QUANT_DB_SPREADSHEET_TITLE = "Quant_DB"
_USERS_WORKSHEET_TITLE = "Users"
_USER_SHEET_COLS = ["ID", "Password", "Reason", "Source", "Status"]
_NARRATIVES_WORKSHEET_TITLE = "Narratives"
_NARRATIVES_SHEET_COLS = ["ID", "Date", "Category", "Title", "Content", "Winners", "Emerging"]
_PORTFOLIOS_WORKSHEET_TITLE = "Portfolios"
_PORTFOLIOS_SHEET_COLS = ["ID", "Account", "Ticker", "AvgPrice", "Quantity", "Date_Added"]
_PORTFOLIOS_LEGACY_HEADER = ["ID", "Ticker", "AvgPrice", "Quantity", "Date_Added"]
_NAV_ADMIN_APPROVAL = "👑 [관리자] 유저 승인"
_THESIS_WORKSHEET_TITLE = "Thesis"
_WATCHLIST_SHEET_TITLE = "Watchlist"
_ETF_UNIVERSE_SHEET_TITLE = "ETF_Universe"
_ETF_UNIVERSE_SHEET_COLS = ["Ticker", "Name", "Category", "AUM_M", "Added_Date", "Source"]
_ETF_AUTO_UPDATE_INTERVAL_DAYS = 7  # 자동 업데이트 주기
_WATCHLIST_SHEET_COLS = ["ID", "Ticker", "Memo", "Alert_Price", "Alert_RSI", "Alert_MA200", "Saved_Price", "Date_Added"]
_THESIS_SHEET_COLS = ["ID", "Ticker", "Account", "Thesis_Title", "Narrative_Category", "Narrative_Date", "Date_Added"]


def get_gspread_client():
    """`st.secrets['gcp_service_account']` 서비스 계정으로 gspread 클라이언트 생성. 미설정 시 None."""
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
        date_added = _narrative_now_kst_string()
        row = [
            str(user_id).strip(),
            str(ticker).strip().upper(),
            str(account).strip(),
            str(thesis_title).strip(),
            str(narrative_category).strip(),
            str(narrative_date).strip(),
            date_added,
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
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
                date_str = dt.astimezone(_KST_TZ).strftime("%m/%d")
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
            str(row[4]).strip() if len(row) > 4 and str(row[4]).strip() else _narrative_now_kst_string(),
        ]
    row = row + [""] * 6
    rid = str(row[0]).strip()
    acct = str(row[1]).strip()
    tk = str(row[2]).strip().upper()
    if not rid or not acct or not tk:
        return None
    dt = str(row[5]).strip() if len(row) > 5 and str(row[5]).strip() else _narrative_now_kst_string()
    return [rid, acct, tk, row[3], row[4], dt]


def ensure_narratives_header_row(ws):
    vals = ws.get_all_values()
    if not vals or not any(str(c).strip() for c in vals[0]):
        ws.update([_NARRATIVES_SHEET_COLS], range_name="A1:G1", value_input_option="USER_ENTERED")
        return
    hdr = [str(h).strip() for h in vals[0][:7]]
    if hdr != _NARRATIVES_SHEET_COLS:
        ws.update([_NARRATIVES_SHEET_COLS], range_name="A1:G1", value_input_option="USER_ENTERED")


def _looks_kst_timestamp(s: str) -> bool:
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
        if s0 and not _looks_kst_timestamp(s0):
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
    if len(row) >= 4 and _looks_kst_timestamp(s0):
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


def _narrative_now_kst_string(dt_utc=None) -> str:
    """UTC 시각을 KST 문자열 YYYY-MM-DD HH:MM:SS 로."""
    u = dt_utc or datetime.now(timezone.utc)
    if u.tzinfo is None:
        u = u.replace(tzinfo=timezone.utc)
    return u.astimezone(_KST_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _narrative_kst_date_string_to_utc_iso(date_kst_str: str) -> str | None:
    """시트 Date 열(KST) → UTC ISO 문자열."""
    if not date_kst_str or not str(date_kst_str).strip():
        return None
    try:
        naive = datetime.strptime(str(date_kst_str).strip(), "%Y-%m-%d %H:%M:%S")
        loc = _KST_TZ.localize(naive)
        return loc.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _narrative_sheet_title_for_record(rec: dict) -> str:
    analysis = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
    base = _narrative_core_theme_display(analysis, max_chars=100)
    if analysis.get("source") == _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
        return "주간 트렌드(7일) 브리핑"
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
    date_kst = _narrative_now_kst_string(dt_utc)
    category = str(analysis.get("source") or "market_narrative").strip() or "market_narrative"
    title = _narrative_sheet_title_for_record(rec_out)
    if len(title) > 500:
        title = title[:497] + "..."
    content = json.dumps(rec_out, ensure_ascii=False)
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
            iso_guess = _narrative_kst_date_string_to_utc_iso(date_kst)
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
        ws.append_row(list(row_values)[:7], value_input_option="USER_ENTERED")
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
    ws.append_row([user_id.strip(), password, reason, source, "pending"], value_input_option="USER_ENTERED")
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
        else:
            st.session_state["login_error"] = True
            st.session_state["login_feedback"] = "비밀번호가 올바르지 않습니다."
        return
    st.session_state["login_error"] = True
    st.session_state["login_feedback"] = f"알 수 없는 계정 상태입니다: {row.get('Status', '')}"


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
if "scanner_results" not in st.session_state:
    st.session_state["scanner_results"] = None
if "scanner_results_emerging" not in st.session_state:
    st.session_state["scanner_results_emerging"] = None
if "narrative_timeseries_briefing" not in st.session_state:
    st.session_state["narrative_timeseries_briefing"] = None

# 사이드바 메인 내비게이션 (탑다운: 단일 radio, 옵션 문자열로 분기)
_MAIN_NAV_OPTIONS = (
    # ── 시작하기 ───────────────────────────────────────────────────────────
    "📖 사용 가이드 (처음이라면 여기부터)",
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
)

# 구버전 라디오/버튼 라벨 → 동일 인덱스 (세션 마이그레이션용)
_LEGACY_NAV_STR_TO_INDEX = {
    "📊 거시경제 지표": 0,
    "🧠 1.5 시장 내러티브": 1,
    "🚀 1.6 AI Opportunity Scanner": 3,
    "📈 2. 섹터 분석": 2,
    "🔬 3. 체력 검사": 4,
    "⏱️ 4. 매수 타점": 4,
    "🛡️ 5.0 포트폴리오 매도 레이더": 5,
    "1. 거시경제 지표 (Macro)": 0,
    "2. 시장 내러티브 (Narrative)": 1,
    "3. 섹터 & 자금 흐름 (Sector Flow)": 2,
    "4. AI 종목 스캐너 (Scanner)": 3,
    "5. 개별 종목 정밀 검사 (Fundamentals & Timing)": 4,
    "6. 포트폴리오 매도 레이더 (Portfolio)": 5,
}

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
_KST_TZ = pytz.timezone("Asia/Seoul")
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
                retryable = any(code in err_str for code in ["503", "500", "unavailable", "internal"])
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
        "temperature": 0.0,  # AI의 상상력을 0으로 통제 (일관성 극대화)
        "top_p": 1,
        "top_k": 1,
        "max_output_tokens": 8192,
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
    """Safely convert values from yfinance to float."""
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
        return ":gray[No Data]"
    return ":green[Pass]" if passed else ":red[Fail]"


def get_latest_series_value(df, row_name):
    """
    yfinance financial statements are often indexed by account name and columns by dates.
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


def macro_traffic_light(bad_total):
    if bad_total >= 5:
        st.error(
            "🔴 [빨간불] 폭풍우 경보 (Full Alignment). 시스템 붕괴 위험. "
            "제3원칙에 따라 시장에서 도망치세요."
        )
    elif bad_total >= 3:
        st.warning(
            "🟡 [노란불] 거시경제 경고 누적. 개별 종목이 좋아도 비중을 줄이고 현금을 확보하세요."
        )
    else:
        st.success(
            f"🟢 [초록불] 매크로 환경 안정적. 「{_MAIN_NAV_OPTIONS[4]}」에서 펀더멘털과 매수 타점을 확인한 뒤 투자를 진행하세요."
        )


def fetch_yield_spread_latest():
    try:
        tnx = yf.Ticker("^TNX").history(period="10d", auto_adjust=False)
        irx = yf.Ticker("^IRX").history(period="10d", auto_adjust=False)
        t10 = get_latest_close_from_history(tnx)
        t3m = get_latest_close_from_history(irx)
        if pd.isna(t10) or pd.isna(t3m):
            return np.nan, MACRO_STATUS_NA, "데이터 부족 (N/A)"
        spread = float(t10) - float(t3m)
        if spread < 0:
            status = MACRO_STATUS_WARN
            note = "수익률 곡선 역전(단기금리 > 장기): 침체·신용경색 우려 신호입니다."
        else:
            status = MACRO_STATUS_PASS
            note = "장단기 금리차 플러스 구간입니다. 역전 신호 없음으로 해석합니다."
        return spread, status, note
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


def fetch_vix_latest_and_history():
    try:
        hist = yf.Ticker("^VIX").history(period="1y", auto_adjust=False)
        cur = get_latest_close_from_history(hist)
        return cur, hist, None
    except Exception as exc:
        return np.nan, None, str(exc)


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


def fetch_wti_latest():
    try:
        o = yf.Ticker("CL=F").history(period="14d", auto_adjust=False)
        return get_latest_close_from_history(o)
    except Exception:
        return np.nan


def evaluate_unemployment_sahm_series():
    try:
        try:
            unrate_series = fred.get_series("UNRATE")
        except Exception:
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


def evaluate_cpi_yoy():
    try:
        try:
            cpi_series = fred.get_series("CPIAUCSL")
        except Exception:
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


def fetch_dxy_latest_and_mean_deviation():
    try:
        dxy_hist = yf.Ticker("DX-Y.NYB").history(period="400d", auto_adjust=False)
        cur = get_latest_close_from_history(dxy_hist)
        if dxy_hist is None or "Close" not in dxy_hist.columns:
            return np.nan, np.nan, MACRO_STATUS_NA

        closes = pd.to_numeric(dxy_hist["Close"], errors="coerce").dropna()
        ma252 = closes.rolling(window=252, min_periods=126).mean().iloc[-1]
        dev_pct = np.nan if pd.isna(cur) or pd.isna(ma252) or ma252 == 0 else (cur / ma252 - 1.0) * 100

        if pd.isna(cur) or pd.isna(ma252):
            return cur, np.nan, MACRO_STATUS_NA

        if dev_pct >= 5.5:
            return cur, dev_pct, MACRO_STATUS_FAIL
        if dev_pct >= 2.5:
            return cur, dev_pct, MACRO_STATUS_WARN
        return cur, dev_pct, MACRO_STATUS_PASS
    except Exception:
        return np.nan, np.nan, MACRO_STATUS_NA


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_earnings_calendar(tickers: tuple) -> list[dict]:
    """
    보유 종목의 다음 실적 발표일을 yfinance로 조회.
    반환: [{"ticker", "earnings_date", "days_until", "eps_estimate"}]
    """
    results = []
    for tk in tickers:
        try:
            info = yf.Ticker(tk).info
            # yfinance earnings date
            earnings_ts = info.get("earningsTimestamp") or info.get("earningsDate")
            eps_fwd = info.get("forwardEps")

            earnings_dt = None
            if earnings_ts:
                try:
                    if isinstance(earnings_ts, (int, float)):
                        earnings_dt = datetime.fromtimestamp(earnings_ts, tz=timezone.utc)
                    elif isinstance(earnings_ts, list) and earnings_ts:
                        earnings_dt = datetime.fromtimestamp(earnings_ts[0], tz=timezone.utc)
                except Exception:
                    pass

            if earnings_dt:
                now_utc = datetime.now(timezone.utc)
                days_until = (earnings_dt.date() - now_utc.date()).days
                results.append({
                    "ticker": tk,
                    "earnings_date": earnings_dt.strftime("%Y-%m-%d"),
                    "days_until": days_until,
                    "eps_estimate": round(float(eps_fwd), 2) if eps_fwd else None,
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["days_until"])
    return results


def fetch_fed_funds_rate() -> tuple[float, str]:
    """FRED FEDFUNDS 시리즈에서 현재 기준금리 반환. (rate, note)"""
    try:
        series = fred.get_series("FEDFUNDS")
        if series is None or series.empty:
            return np.nan, "FRED 데이터 없음"
        rate = float(pd.to_numeric(series, errors="coerce").dropna().iloc[-1])
        if rate >= 5.0:
            note = "고금리 레짐. 성장주 밸류에이션 압박 지속."
        elif rate >= 3.0:
            note = "중립 이상 금리. 연준 기조 모니터링 필요."
        else:
            note = "완화적 금리 환경. 유동성 우호적."
        return rate, note
    except Exception:
        return np.nan, _FRED_TRANSIENT_ERROR_NOTE


def fetch_macro_history_series() -> dict:
    """CPI·실업률·DXY·Fed Rate 1~2년 히스토리 시리즈 반환."""
    out = {}
    try:
        cpi_s = fred.get_series("CPIAUCSL")
        if cpi_s is not None and not cpi_s.empty:
            cpi_df = pd.DataFrame({"CPI": pd.to_numeric(cpi_s, errors="coerce")}).dropna()
            cpi_df["CPI YoY(%)"] = cpi_df["CPI"].pct_change(12) * 100
            out["cpi"] = cpi_df["CPI YoY(%)"].dropna().tail(24)
    except Exception:
        pass
    try:
        un_s = fred.get_series("UNRATE")
        if un_s is not None and not un_s.empty:
            out["unrate"] = pd.to_numeric(un_s, errors="coerce").dropna().tail(24)
    except Exception:
        pass
    try:
        fed_s = fred.get_series("FEDFUNDS")
        if fed_s is not None and not fed_s.empty:
            out["fedfunds"] = pd.to_numeric(fed_s, errors="coerce").dropna().tail(24)
    except Exception:
        pass
    try:
        dxy_hist = yf.Ticker("DX-Y.NYB").history(period="2y", auto_adjust=False)
        if dxy_hist is not None and not dxy_hist.empty and "Close" in dxy_hist.columns:
            out["dxy"] = pd.to_numeric(dxy_hist["Close"], errors="coerce").dropna()
    except Exception:
        pass
    return out


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
    spread_val, spread_st, spread_note = fetch_yield_spread_latest()

    vix_val, vix_hist, vix_err = fetch_vix_latest_and_history()
    vix_st, vix_note = evaluate_vix_status(vix_val)

    wti = fetch_wti_latest()
    wti_st, wti_note = evaluate_wti_status(wti)

    un_st, un_avg3, un_yr_low, un_margin, un_note = evaluate_unemployment_sahm_series()

    cpi_st, cpi_yoy, cpi_note = evaluate_cpi_yoy()

    dxy, dxy_dev, dxy_st_base = fetch_dxy_latest_and_mean_deviation()

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
            "판독 요약": vix_note + ("" if not vix_err else f" (데이터 참고: {vix_err})"),
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
    fed_rate, fed_note = fetch_fed_funds_rate()
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
    Trading-day horizons for Hidden Alpha: 5 / 10 / 21 / 63 vs yfinance-style daily closes.
    """
    return {
        "1주(%)": calculate_period_return(close_series, 5),
        "2주(%)": calculate_period_return(close_series, 10),
        "1개월(%)": calculate_period_return(close_series, 21),
        "3개월(%)": calculate_period_return(close_series, 63),
    }


def get_close_prices_from_download(download_df):
    """
    Normalize yfinance download output to a close-price DataFrame with ticker columns.
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
        raw = yf.download(
            tickers=unique_tickers,
            period="6mo",
            interval="1d",
            auto_adjust=False,
            group_by="column",
            progress=False,
            threads=True,
        )
    except Exception:
        raw = pd.DataFrame()
    close_df = get_close_prices_from_download(raw)

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
        raw = yf.download(
            tickers=all_tickers, period="6mo", interval="1d",
            auto_adjust=False, group_by="column", progress=False, threads=True
        )
        close_df = get_close_prices_from_download(raw)
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
        raw = yf.download(
            tickers=all_tickers, period="6mo", interval="1d",
            auto_adjust=False, group_by="column", progress=False, threads=True
        )
        close_df = get_close_prices_from_download(raw)
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

            # 거래량 급증
            try:
                raw_vol = yf.download(tk, period="2mo", interval="1d", auto_adjust=False, progress=False)
                vol = pd.to_numeric(raw_vol.get("Volume", pd.Series(dtype=float)), errors="coerce").dropna()
                vol_surge = float(vol.tail(5).mean() / vol.tail(21).mean()) if len(vol) >= 21 else np.nan
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
        raw = yf.download(
            tickers=all_tickers, period="6mo", interval="1d",
            auto_adjust=False, group_by="column", progress=False, threads=True
        )
        close_df = get_close_prices_from_download(raw)
        vol_raw = pd.DataFrame()
        try:
            if isinstance(raw.columns, pd.MultiIndex) and "Volume" in raw.columns.get_level_values(0):
                vol_raw = raw["Volume"]
        except Exception:
            pass
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


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_sector_etf_closes(tickers_tuple: tuple[str, ...]):
    if not tickers_tuple:
        return pd.DataFrame()
    try:
        raw = yf.download(
            tickers=list(tickers_tuple),
            period="2y",
            interval="1d",
            auto_adjust=False,
            group_by="column",
            progress=False,
        )
    except Exception:
        raw = pd.DataFrame()
    return get_close_prices_from_download(raw)


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
def cached_yfinance_quote_type(ticker_upper: str):
    try:
        info = yf.Ticker(str(ticker_upper).strip().upper()).info or {}
        return str(info.get("quoteType") or "").strip()
    except Exception:
        return ""


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_timing_price_history(ticker_upper: str):
    try:
        return yf.Ticker(str(ticker_upper).strip().upper()).history(
            period="1y", auto_adjust=False
        )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_evaluate_kpis_snapshot(ticker_upper: str):
    return evaluate_kpis(str(ticker_upper).strip().upper())


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_earnings_history(ticker_upper: str) -> pd.DataFrame:
    """최근 분기별 EPS 예상 vs 실제 (Earnings Surprise)."""
    try:
        tk = yf.Ticker(str(ticker_upper).strip().upper())
        # quarterly earnings
        qe = tk.quarterly_earnings
        if qe is not None and not qe.empty:
            qe = qe.reset_index()
            return qe
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_institutional_holders(ticker_upper: str) -> pd.DataFrame:
    """기관 보유 비중 상위 목록."""
    try:
        tk = yf.Ticker(str(ticker_upper).strip().upper())
        ih = tk.institutional_holders
        if ih is not None and not ih.empty:
            return ih.head(10)
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


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
    return build_etf_holdings_universe(yf.Ticker(t_clean))


@st.cache_data(ttl=_DATA_CACHE_TTL, show_spinner=False)
def cached_portfolio_yf_close_1y(tuple_tickers: tuple[str, ...]):
    tickers_list = list(dict.fromkeys([t for t in tuple_tickers if str(t).strip()]))
    if not tickers_list:
        return pd.DataFrame()
    try:
        raw = yf.download(
            tickers=tickers_list,
            period="1y",
            interval="1d",
            auto_adjust=False,
            group_by="column",
            progress=False,
            threads=True,
        )
    except Exception:
        raw = pd.DataFrame()
    close_df = get_close_prices_from_download(raw)
    if close_df.empty:
        return close_df
    if len(tickers_list) == 1 and "SINGLE" in close_df.columns:
        close_df = close_df.copy()
        close_df.columns = [tickers_list[0]]
    return close_df


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
        ticker = yf.Ticker(ticker_symbol)

        # ── info 수집 (여러 방법 시도) ──────────────────────────────────
        info = {}
        try:
            raw_info = ticker.info
            if isinstance(raw_info, dict) and len(raw_info) > 5:
                info = raw_info
        except Exception:
            pass

        # info가 비어있으면 fast_info로 보완
        if not info:
            try:
                fi = ticker.fast_info
                info = {
                    "currentPrice": getattr(fi, "last_price", None),
                    "fiftyDayAverage": getattr(fi, "fifty_day_average", None),
                    "twoHundredDayAverage": getattr(fi, "two_hundred_day_average", None),
                    "marketCap": getattr(fi, "market_cap", None),
                }
            except Exception:
                pass

        cashflow = None
        try:
            cashflow = ticker.cashflow
        except Exception:
            pass
        if cashflow is None or cashflow.empty:
            try:
                cashflow = ticker.get_cashflow()
            except Exception:
                cashflow = None

        history = pd.DataFrame()
        try:
            history = ticker.history(period="1y", auto_adjust=False)
        except Exception:
            pass

        # ── 재무제표에서 직접 추출 시도 ─────────────────────────────────
        income_stmt = None
        try:
            income_stmt = ticker.income_stmt
        except Exception:
            try:
                income_stmt = ticker.get_income_stmt()
            except Exception:
                pass

        balance_sheet = None
        try:
            balance_sheet = ticker.balance_sheet
        except Exception:
            try:
                balance_sheet = ticker.get_balance_sheet()
            except Exception:
                pass

    except Exception:
        info, cashflow, history = {}, None, pd.DataFrame()
        income_stmt, balance_sheet = None, None

    # ── 지표 추출 ──────────────────────────────────────────────────────
    # ROE
    roe = to_float(info.get("returnOnEquity"))
    if pd.isna(roe) and income_stmt is not None and balance_sheet is not None:
        try:
            net_income = income_stmt.loc["Net Income"].iloc[0] if "Net Income" in income_stmt.index else np.nan
            equity = balance_sheet.loc["Stockholders Equity"].iloc[0] if "Stockholders Equity" in balance_sheet.index else np.nan
            if pd.notna(net_income) and pd.notna(equity) and equity != 0:
                roe = float(net_income / equity)
        except Exception:
            pass

    # Operating Margin
    operating_margin = to_float(info.get("operatingMargins"))
    if pd.isna(operating_margin) and income_stmt is not None:
        try:
            op_income = income_stmt.loc["Operating Income"].iloc[0] if "Operating Income" in income_stmt.index else np.nan
            total_rev = income_stmt.loc["Total Revenue"].iloc[0] if "Total Revenue" in income_stmt.index else np.nan
            if pd.notna(op_income) and pd.notna(total_rev) and total_rev != 0:
                operating_margin = float(op_income / total_rev)
        except Exception:
            pass

    # Debt to Equity
    debt_to_equity = to_float(info.get("debtToEquity"))
    if pd.isna(debt_to_equity) and balance_sheet is not None:
        try:
            total_debt = balance_sheet.loc["Total Debt"].iloc[0] if "Total Debt" in balance_sheet.index else np.nan
            equity = balance_sheet.loc["Stockholders Equity"].iloc[0] if "Stockholders Equity" in balance_sheet.index else np.nan
            if pd.notna(total_debt) and pd.notna(equity) and equity != 0:
                debt_to_equity = float(total_debt / equity * 100)
        except Exception:
            pass

    # PEG Ratio
    peg_ratio = to_float(info.get("pegRatio") or info.get("trailingPegRatio"))

    # EPS & Growth
    trailing_eps = to_float(info.get("trailingEps") or info.get("epsTrailingTwelveMonths"))
    earnings_growth = to_float(info.get("earningsGrowth") or info.get("revenueGrowth"))

    # Free Cash Flow
    fcf = get_latest_series_value(cashflow, "Free Cash Flow")
    if pd.isna(fcf) and cashflow is not None:
        for key in ["FreeCashFlow", "Free Cash Flow", "Operating Cash Flow"]:
            fcf = get_latest_series_value(cashflow, key)
            if not pd.isna(fcf):
                break

    # Momentum (가격 + MA)
    current_price, ma50, ma200 = get_momentum_values(history)
    # info에서 보완
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
    if not pd.isna(trailing_eps) and not pd.isna(growth_percent):
        intrinsic_value = trailing_eps * (8.5 + (2 * growth_percent))

    margin_of_safety = np.nan
    if not pd.isna(intrinsic_value) and intrinsic_value != 0 and not pd.isna(current_price):
        margin_of_safety = ((intrinsic_value - current_price) / intrinsic_value) * 100
    core_fcf_pass = (not pd.isna(fcf)) and (fcf > 0)

    rows = [
        {
            "Category": "수익성 (Profitability)",
            "KPI": "ROE",
            "Value": pct_str(roe),
            "Rule": "15% 이상",
            "Pass": pass_fail_badge(not pd.isna(roe) and roe >= 0.15, pd.isna(roe)),
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
            "Rule": "100 미만",
            "Pass": pass_fail_badge(not pd.isna(debt_to_equity) and debt_to_equity < 100, pd.isna(debt_to_equity)),
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
            "KPI": "PEG Ratio",
            "Value": num_str(peg_ratio),
            "Rule": "1.0 미만",
            "Pass": pass_fail_badge(not pd.isna(peg_ratio) and peg_ratio < 1.0, pd.isna(peg_ratio)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "Margin of Safety",
            "Value": pct_points_str(margin_of_safety),
            "Rule": "20% 이상",
            "Pass": pass_fail_badge(not pd.isna(margin_of_safety) and margin_of_safety >= 20, pd.isna(margin_of_safety)),
        },
        {
            "Category": "모멘텀 (Momentum)",
            "KPI": "가격 > MA50 & MA200",
            "Value": (
                f"현재가 {num_str(current_price)} / MA50 {num_str(ma50)} / "
                f"MA200 {num_str(ma200)}"
            ),
            "Rule": "정배열(상향)",
            "Pass": pass_fail_badge(momentum_pass, pd.isna(current_price) or pd.isna(ma50) or pd.isna(ma200)),
        },
    ]

    kpi_df = pd.DataFrame(rows)
    pass_count = (kpi_df["Pass"] == ":green[Pass]").sum()
    fail_count = (kpi_df["Pass"] == ":red[Fail]").sum()
    nodata_count = (kpi_df["Pass"] == ":gray[No Data]").sum()

    margin_context = {
        "intrinsic_value": intrinsic_value,
        "margin_of_safety": margin_of_safety,
        "trailing_eps": trailing_eps,
        "growth_percent": growth_percent,
        "current_price": current_price,
        "core_fcf_pass": core_fcf_pass,
    }

    return kpi_df, pass_count, fail_count, nodata_count, margin_context


def detect_quote_type(ticker_symbol):
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info if ticker.info else {}
        quote_type = str(info.get("quoteType") or "").upper()
        return quote_type, ticker, info
    except Exception:
        _notify_yfinance_fetch_failed()
        return "", yf.Ticker(ticker_symbol), {}


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

    # yfinance source may be ratio(0~1) or already percent(0~100)
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
        raw = yf.download(
            tickers=tickers,
            period="2y",
            interval="1d",
            auto_adjust=False,
            group_by="column",
            progress=False,
            threads=True,
        )
    except Exception:
        raw = pd.DataFrame()
    close_df = get_close_prices_from_download(raw)
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
        now_s = _narrative_now_kst_string()
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
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=base_columns)
    df["Quantity"] = df["Quantity"].fillna(1.0)
    df = df[df["Ticker"].ne("") & df["Ticker"].ne("NAN") & df["Account"].astype(str).str.strip().ne("")]
    df = df.drop_duplicates(subset=["Account", "Ticker"], keep="last").reset_index(drop=True)
    return df[base_columns].copy()


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
    quote_by_ticker = {t: cached_yfinance_quote_type(t) for t in unique_holdings}

    symbol_stats = {}
    for ticker in clean_tickers:
        series = close_df[ticker] if ticker in close_df.columns else pd.Series(dtype=float)
        clean_series = pd.to_numeric(series, errors="coerce").dropna()
        current_price = float(clean_series.iloc[-1]) if not clean_series.empty else np.nan
        ma200 = clean_series.rolling(window=200, min_periods=200).mean().iloc[-1] if not clean_series.empty else np.nan
        one_month_return = calculate_period_return(clean_series, 21)
        high_52w = float(clean_series.max()) if not clean_series.empty else np.nan
        drawdown_pct = np.nan
        if pd.notna(current_price) and pd.notna(high_52w) and high_52w > 0:
            drawdown_pct = ((current_price - high_52w) / high_52w) * 100.0

        if pd.isna(current_price) or pd.isna(ma200) or pd.isna(one_month_return):
            status = "N/A"
        elif current_price < ma200:
            status = "🚨 매도 (SELL)"
        elif current_price > ma200 and one_month_return < 0:
            status = "⚠️ 주의 (WARNING)"
        elif current_price > ma200 and one_month_return > 0:
            status = "✅ 보유 (HOLD)"
        else:
            status = "N/A"

        symbol_stats[ticker] = {
            "current_price": current_price,
            "ma200": ma200,
            "one_month_return": one_month_return,
            "drawdown_pct": drawdown_pct,
            "status": status,
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
        drawdown_pct = pd.to_numeric(stat.get("drawdown_pct"), errors="coerce")
        status = stat.get("status", "N/A")

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
            }
        )

    return pd.DataFrame(rows)


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
        added_date = datetime.now(_KST_TZ).strftime("%Y-%m-%d")
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
            ws.append_rows(rows_to_add, value_input_option="USER_ENTERED")
        return len(rows_to_add), ""
    except Exception as exc:
        return 0, str(exc)


def fetch_new_etfs_from_fmp(days_lookback: int = 90, min_aum_m: float = 50.0) -> list[dict]:
    """
    FMP API로 최근 상장된 ETF를 스캔.
    - days_lookback: 최근 N일 이내 상장된 ETF
    - min_aum_m: 최소 AUM (백만달러)
    """
    try:
        fmp_key = st.secrets.get("FMP_API_KEY", "")
        if not fmp_key:
            return []

        # FMP ETF 전체 목록
        url = f"https://financialmodelingprep.com/stable/etf-list?apikey={fmp_key}"
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return []
        all_etfs = resp.json()
        if not isinstance(all_etfs, list):
            return []

        cutoff_date = datetime.now(_KST_TZ) - timedelta(days=days_lookback)
        new_etfs = []

        for etf in all_etfs:
            if not isinstance(etf, dict):
                continue
            ticker = str(etf.get("symbol", "") or "").strip().upper()
            if not ticker:
                continue
            # 미국 ETF만 필터링
            exchange = str(etf.get("exchange", "") or "").upper()
            if exchange not in ("NYSE ARCA", "NASDAQ", "BATS", "NYSEARCA", "NYSEArca"):
                continue
            # 상장일 체크
            ipo_date_str = str(etf.get("ipoDate", "") or "")
            if ipo_date_str:
                try:
                    ipo_dt = datetime.strptime(ipo_date_str[:10], "%Y-%m-%d").replace(tzinfo=_KST_TZ)
                    if ipo_dt < cutoff_date:
                        continue
                except Exception:
                    continue
            else:
                continue
            new_etfs.append({
                "ticker": ticker,
                "name": str(etf.get("name", "") or "")[:80],
                "category": str(etf.get("assetClass", "") or "")[:50],
                "aum_m": "",
                "ipo_date": ipo_date_str[:10],
            })

        # AUM 필터링 (yfinance로 빠르게 체크)
        filtered = []
        for etf in new_etfs[:50]:  # 최대 50개만 체크 (rate limit 방지)
            try:
                info = yf.Ticker(etf["ticker"]).info
                aum = float(info.get("totalAssets", 0) or 0) / 1_000_000
                if aum >= min_aum_m:
                    etf["aum_m"] = f"{aum:.0f}"
                    filtered.append(etf)
            except Exception:
                # AUM 확인 실패해도 포함 (보수적으로)
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
        cutoff_date = datetime.now(_KST_TZ) - timedelta(days=180)

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
                added_dt = datetime.strptime(added_date_str[:10], "%Y-%m-%d").replace(tzinfo=_KST_TZ)
                if added_dt > cutoff_date:
                    continue
            except Exception:
                continue

            # yfinance로 AUM + 거래량 체크
            try:
                info = yf.Ticker(ticker).info
                aum = float(info.get("totalAssets", 0) or 0) / 1_000_000
                avg_vol = float(info.get("averageVolume", 0) or 0)
                price = float(info.get("regularMarketPrice", 0) or info.get("previousClose", 0) or 0)
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
            return sh.worksheet(_WATCHLIST_SHEET_TITLE), None
        # 없으면 새로 생성
        ws = sh.add_worksheet(title=_WATCHLIST_SHEET_TITLE, rows=1000, cols=8)
        ws.update([_WATCHLIST_SHEET_COLS], range_name="A1:H1", value_input_option="USER_ENTERED")
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
            r = (r + [""] * 8)[:8]
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
            new_rows.append([
                str(user_id).strip(),
                ticker,
                str(item.get("memo", "")).strip(),
                alert_price_str,
                alert_rsi_str,
                alert_ma200_str,
                saved_price_str,
                str(item.get("date_added", _narrative_now_kst_string())).strip(),
            ])
        all_rows = [_WATCHLIST_SHEET_COLS] + other_rows + new_rows
        ws.clear()
        ws.update(all_rows, range_name=f"A1:H{len(all_rows)}", value_input_option="USER_ENTERED")
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
    if analysis.get("source") == _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
        return f"[{date_part}] 📊 주간트렌드(7일) | 브리핑 | 티커 {len(analysis.get('precomputed_universe') or [])}개"
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
    if analysis.get("source") == _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
        st.markdown("**유형:** 주간 트렌드 브리핑 (최근 7일 교집합)")
        uni = analysis.get("precomputed_universe")
        if isinstance(uni, list) and uni:
            st.caption("스캐너 연동 티커 풀: " + ", ".join(str(x) for x in uni[:40]) + (" …" if len(uni) > 40 else ""))
        md = str(analysis.get("weekly_briefing_markdown") or analysis.get("summary") or "").strip()
        if md:
            with st.expander("브리핑 본문", expanded=False):
                st.markdown(md[:12000])
        return

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
        last_non_weekly = None
        for r in reversed(records):
            if not isinstance(r, dict):
                continue
            a = r.get("analysis") if isinstance(r.get("analysis"), dict) else {}
            if a.get("source") == _NARRATIVE_RECORD_SOURCE_WEEKLY_7D:
                continue
            last_non_weekly = r
            break
        pick = last_non_weekly or (records[-1] if records else None)
        if (not isinstance(cv, dict) or not cv) and pick:
            st.session_state["current_view"] = pick.get("analysis", {})
            st.session_state["current_view_language"] = pick.get("language", "ko")
        st.session_state["_narrative_persist_loaded_v2"] = True


def fetch_latest_prices_for_tickers(tickers):
    clean_tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]
    clean_tickers = list(dict.fromkeys(clean_tickers))
    if not clean_tickers:
        return {}
    try:
        raw = yf.download(
            tickers=clean_tickers,
            period="10d",
            interval="1d",
            auto_adjust=False,
            group_by="column",
            progress=False,
            threads=True,
        )
        close_df = get_close_prices_from_download(raw)
        if close_df.empty:
            return {}
        if len(clean_tickers) == 1 and "SINGLE" in close_df.columns:
            close_df.columns = [clean_tickers[0]]
        price_map = {}
        for ticker in clean_tickers:
            if ticker not in close_df.columns:
                price_map[ticker] = np.nan
                continue
            series = pd.to_numeric(close_df[ticker], errors="coerce").dropna()
            price_map[ticker] = float(series.iloc[-1]) if not series.empty else np.nan
        return price_map
    except Exception:
        _notify_yfinance_fetch_failed()
        return {}


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
# 1.5 주간 트렌드 / 변곡점 분석 결과에 실제 yfinance 수익률을 덧붙여 검증.
# =============================================================================

# 1.5 주간 트렌드 프롬프트가 강제하는 두 섹션 헤더 매처
_FACTCHECK_WINNERS_HEADER_RE = re.compile(
    r"^\s*#{1,6}\s*🏆\s*Weekly\s+Winners.*$", re.MULTILINE | re.IGNORECASE
)
_FACTCHECK_EXPANDING_HEADER_RE = re.compile(
    r"^\s*#{1,6}\s*🚀\s*Weekly\s+Expanding\s+To.*$", re.MULTILINE | re.IGNORECASE
)
# WoW(트렌드 변곡점) 응답에서 자주 등장하는 Fading/Emerging 헤더(영문·한국어 혼용 모두 허용)
_FACTCHECK_EMERGING_HEADER_RE = re.compile(
    r"^\s*#{1,6}.*(emerging|부상|새로\s*나).*$", re.MULTILINE | re.IGNORECASE
)
_FACTCHECK_FADING_HEADER_RE = re.compile(
    r"^\s*#{1,6}.*(fading|사그|약화|소멸).*$", re.MULTILINE | re.IGNORECASE
)
_FACTCHECK_INTERSECTION_HEADER_RE = re.compile(
    r"^\s*#{1,6}.*(교집합|intersection|살아남은).*$", re.MULTILINE | re.IGNORECASE
)

_FACTCHECK_BOLD_TICKER_RE = re.compile(r"\*\*\s*([A-Z][A-Z0-9.\-]{0,9})\s*\*\*")
_FACTCHECK_PLAIN_TICKER_RE = re.compile(r"\b([A-Z][A-Z0-9.\-]{0,9})\b")

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
        "WITH", "VS", "THE", "TBD", "TBA", "NA", "NAN", "TBA",
        "WOW", "DOD", "WEEKLY", "WINNERS", "EXPANDING", "COPILOT", "GEMINI",
    }
)


def _factcheck_slice_section(markdown: str, header_re: "re.Pattern") -> str:
    """주어진 헤더 정규식 매치 위치부터 다음 ## 헤더 전까지의 본문을 반환."""
    md = str(markdown or "")
    m = header_re.search(md)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"^\s*#{1,6}\s+\S+", md[start:], re.MULTILINE)
    end = start + (nxt.start() if nxt else len(md) - start)
    return md[start:end]


def _factcheck_pick_tickers_from_section(section_md: str) -> list:
    """섹션 본문에서 라인 단위로 티커를 추출.
    1순위: `**TICKER**` 볼드 매치 (프롬프트가 강제하는 형식)
    2순위: 라인 첫 대문자 토큰 (잡음 단어 제외, 길이 2~6)
    """
    out = []
    seen = set()
    for raw_line in str(section_md or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        bold_match = _FACTCHECK_BOLD_TICKER_RE.search(line)
        if bold_match:
            tk = bold_match.group(1).upper().strip(".-")
            if tk and tk not in _FACTCHECK_NON_TICKER_WORDS and tk not in seen:
                seen.add(tk)
                out.append(tk)
            continue
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
            break  # 라인당 1개만
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
    """팩트 체크용 종가 시계열. 누락 티커는 NaN 컬럼으로 보존하여 호출부 'N/A' 처리.
    내부적으로 yfinance용 심볼 매핑을 적용하되, 반환 컬럼은 사용자 라벨(원본)으로 유지."""
    clean = [str(t).strip().upper() for t in tickers if str(t).strip()]
    clean = list(dict.fromkeys(clean))
    if not clean:
        return pd.DataFrame()

    # 사용자 라벨 → 야후 심볼 매핑 (역매핑도 함께 구성)
    yf_map = {t: _factcheck_to_yahoo_symbol(t) for t in clean}
    yf_to_user = {}
    for user_t, yf_t in yf_map.items():
        yf_to_user.setdefault(yf_t, user_t)
    yf_symbols = list(yf_to_user.keys())

    try:
        raw = yf.download(
            tickers=yf_symbols,
            period=period,
            interval="1d",
            auto_adjust=True,
            group_by="column",
            progress=False,
            threads=True,
        )
    except Exception:
        _notify_yfinance_fetch_failed()
        return pd.DataFrame({t: pd.Series(dtype=float) for t in clean})
    close_df = get_close_prices_from_download(raw)
    if close_df is None or close_df.empty:
        # 다운로드 실패 — 빈 데이터로라도 컬럼은 보존해 호출부가 N/A 처리할 수 있게 한다
        return pd.DataFrame({t: pd.Series(dtype=float) for t in clean})
    if len(yf_symbols) == 1 and "SINGLE" in close_df.columns:
        close_df = close_df.rename(columns={"SINGLE": yf_symbols[0]})
    # 야후 심볼 컬럼을 사용자 라벨로 되돌리기
    rename_back = {yf_t: user_t for yf_t, user_t in yf_to_user.items() if yf_t in close_df.columns}
    if rename_back:
        close_df = close_df.rename(columns=rename_back)
    # 누락된 사용자 라벨 컬럼은 NaN으로 보존
    for t in clean:
        if t not in close_df.columns:
            close_df[t] = np.nan
    return close_df[clean]


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
    if completed_at:
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
        # Watchlist 추가 버튼
        btn_col1, btn_col2 = st.columns([1, 3])
        with btn_col1:
            if st.button(f"🔔 {tk_scan} Watchlist 추가", key=f"scanner_wl_add_{rank_idx}_{tk_scan}", use_container_width=True):
                _uid_scan = str(st.session_state.get("user_id") or "").strip()
                _cur_price_scan = fetch_latest_prices_for_tickers([tk_scan]).get(tk_scan, np.nan)
                _scan_wl_item = {
                    "ticker": tk_scan,
                    "memo": f"AI 스캐너 TOP{rank} - Final Score: {_scanner_ui_fmt_2f(row['Final Score'])}",
                    "alert_price": np.nan,
                    "alert_rsi": np.nan,
                    "alert_ma200": False,
                    "saved_price": float(_cur_price_scan) if pd.notna(_cur_price_scan) else np.nan,
                    "date_added": _narrative_now_kst_string(),
                }
                _scan_wl_cur = load_watchlist_sheet(_uid_scan)
                _scan_wl_cur = [x for x in _scan_wl_cur if x["ticker"] != tk_scan]
                _scan_wl_cur.append(_scan_wl_item)
                _ok_scan, _err_scan = save_watchlist_sheet(_uid_scan, _scan_wl_cur)
                if _ok_scan:
                    st.success(f"✅ {tk_scan}을 Watchlist에 추가했어요!")
                    st.session_state["_watchlist_alert_checked"] = False
                else:
                    st.error(f"저장 실패: {_err_scan}")
        st.divider()

    remain_df = score_df.iloc[3:].copy()
    if not remain_df.empty:
        st.markdown("### 4위 이하 종목")
        show_cols = [
            "Ticker",
            "Name",
            "Final Score",
            "Narrative Score",
            "Momentum Score",
            "RS Score",
            "Fundamentals Score",
            "Institutional Score",
            "Valuation Score",
        ]
        num_fmt = st.column_config.NumberColumn(format="%.2f")
        st.dataframe(
            remain_df[show_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "Final Score": num_fmt,
                "Narrative Score": num_fmt,
                "Momentum Score": num_fmt,
                "RS Score": num_fmt,
                "Fundamentals Score": num_fmt,
                "Institutional Score": num_fmt,
                "Valuation Score": num_fmt,
            },
        )
    else:
        st.caption("유니버스 종목 수가 3개 이하라 추가 표시는 없습니다.")

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
   Yahoo Finance `info`에서 `revenueGrowth` 또는 `trailingEps`가 양수면 가점(원시 100), 아니면 0으로 두고 풀 전체에서 정규화합니다. **두 지표가 모두 없으면** 이 항목은 “데이터 없음”으로 보아 가중치에서 빼고, 나머지 항목만으로 **100점 만점에 맞게 다시 환산**합니다.

5. **Inst. Interest**  
   최근 3거래일 평균 거래량 ÷ 최근 63거래일 평균 거래량이 **1.2 이상**이면 가점(원시 100), 미만이면 0 후 정규화합니다.

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
    if completed_at:
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
        volx = row.get("Vol5/30x")
        volx_s = f"{float(volx):.2f}x" if pd.notna(volx) else "N/A"
        rsi_s = f"{float(row.get('RSI(14)')):.1f}" if pd.notna(row.get("RSI(14)")) else "N/A"
        st.success(
            f"🌱 TOP {rank} | {row['Ticker']} ({row['Name']}) | Final {_scanner_ui_fmt_2f(row['Final Score'])} / 100\n"
            f"RSI(14): {rsi_s} · 5일/30일 거래량 비: {volx_s}"
        )
        fac_cols = st.columns(5)
        for i, (label, key) in enumerate(_em_factor_defs):
            with fac_cols[i]:
                st.metric(label, _scanner_ui_fmt_2f(row[key]))
        st.markdown(f"**다음 타자 AI 코멘트:** {row['Narrative Why']}")
        st.markdown(f"**리스크:** {row['Risk']}")
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

    with st.expander("📐 Emerging 엔진 공식 (가중치·의미)", expanded=False):
        st.markdown(
            """
| 항목 | 비중 | 요약 |
|------|------|------|
| Narrative Expansion | **35%** | 대장주 제외, 2차 수혜·인프라·공급망 관점 Gemini 점수 |
| Early Relative Strength | **20%** | 1개월 수익률 기반 원시점수(0~100)를 가중합에 직접 반영 |
| Volume Acceleration | **20%** | 최근 5일 / 30일 평균 거래량 비율(≥1.5 고득점) |
| Fundamental Readiness | **15%** | EPS·매출 성장 등 `yfinance` info 기반 |
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
            raw = yf.download(
                tickers=tickers,
                period="6mo",
                interval="1d",
                auto_adjust=False,
                group_by="column",
                progress=False,
                threads=True,
            )
            spy_hist = yf.Ticker("SPY").history(period="6mo", auto_adjust=False)
        except Exception:
            _notify_yfinance_fetch_failed()
            raw = pd.DataFrame()
            spy_hist = pd.DataFrame()

    close_df = get_close_prices_from_download(raw)
    if len(tickers) == 1 and "SINGLE" in close_df.columns:
        close_df.columns = [tickers[0]]

    volume_df = pd.DataFrame()
    if raw is not None and not raw.empty:
        if isinstance(raw.columns, pd.MultiIndex):
            if "Volume" in raw.columns.get_level_values(0):
                volume_df = raw["Volume"].copy()
        elif "Volume" in raw.columns:
            volume_df = raw[["Volume"]].copy()
            volume_df.columns = ["SINGLE"]
    if len(tickers) == 1 and "SINGLE" in volume_df.columns:
        volume_df.columns = [tickers[0]]

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

        m1_ret = calculate_period_return(close_series, 21)
        m3_ret = calculate_period_return(close_series, 63)
        rs_raw = to_float(m3_ret) - to_float(spy_3m)

        info = {}
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception:
            _notify_yfinance_fetch_failed()
            info = {}

        revenue_growth = to_float(info.get("revenueGrowth"))
        trailing_eps = to_float(info.get("trailingEps"))
        forward_pe = to_float(info.get("forwardPE"))
        long_name = str(info.get("longName") or info.get("shortName") or ticker).strip()

        vol_3d_avg = pd.to_numeric(vol_series, errors="coerce").tail(3).mean() if not vol_series.empty else np.nan
        vol_3m_avg = pd.to_numeric(vol_series, errors="coerce").tail(63).mean() if not vol_series.empty else np.nan
        vol_ratio = np.nan
        if pd.notna(vol_3d_avg) and pd.notna(vol_3m_avg) and vol_3m_avg > 0:
            vol_ratio = float(vol_3d_avg / vol_3m_avg)

        fundamentals_data_available = pd.notna(revenue_growth) or pd.notna(trailing_eps)
        fundamentals_pass = (
            (pd.notna(revenue_growth) and revenue_growth > 0)
            or (pd.notna(trailing_eps) and trailing_eps > 0)
        )
        inst_pass = pd.notna(vol_ratio) and vol_ratio >= 1.2
        # forwardPE가 None/NaN이거나 0 이하(적자 등으로 PE 해석 불가)면 결측으로 간주
        valuation_data_available = pd.notna(forward_pe) and forward_pe > 0
        valuation_pass = valuation_data_available and forward_pe <= 50

        narrative_score, narrative_why = narrative_map.get(ticker, (0.0, "배치 미응답"))
        narrative_available = narrative_why not in SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS

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
                "Narrative Raw": narrative_score,
                "Narrative Why": narrative_why,
                "Narrative Available": bool(narrative_available),
                "Momentum 1M Raw": to_float(m1_ret),
                "RS Raw": to_float(rs_raw),
                "Fundamentals Raw": 100.0 if fundamentals_pass else 0.0,
                "Fundamentals Available": bool(fundamentals_data_available),
                "Institutional Raw": 100.0 if inst_pass else 0.0,
                "Valuation Raw": 100.0 if valuation_pass else 0.0,
                "Valuation Available": bool(valuation_data_available),
                "Risk": risk_text,
            }
        )

    score_df = pd.DataFrame(rows)
    if score_df.empty:
        progress.empty()
        return score_df

    # yfinance 가격 시계열이 없거나 핵심 모멘텀/RS가 NaN인 종목은 랭킹에서 제외 (내러티브만으로 상위 노출 방지)
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
            raw = yf.download(
                tickers=tickers,
                period="6mo",
                interval="1d",
                auto_adjust=False,
                group_by="column",
                progress=False,
                threads=True,
            )
        except Exception:
            _notify_yfinance_fetch_failed()
            raw = pd.DataFrame()

    close_df = get_close_prices_from_download(raw)
    if len(tickers) == 1 and "SINGLE" in close_df.columns:
        close_df.columns = [tickers[0]]

    volume_df = pd.DataFrame()
    if raw is not None and not raw.empty:
        if isinstance(raw.columns, pd.MultiIndex):
            if "Volume" in raw.columns.get_level_values(0):
                volume_df = raw["Volume"].copy()
        elif "Volume" in raw.columns:
            volume_df = raw[["Volume"]].copy()
            volume_df.columns = ["SINGLE"]
    if len(tickers) == 1 and "SINGLE" in volume_df.columns:
        volume_df.columns = [tickers[0]]

    with st.spinner("Emerging Gemini 내러티브 배치 평가 중 (티커 청크별, gemini-2.5-flash)..."):
        narrative_map_em = batch_narrative_emerging_second_order_scores(tickers, narrative_text)

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

        info = {}
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception:
            _notify_yfinance_fetch_failed()
            info = {}

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
        narrative_available = narrative_why not in SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS

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
    try:
        feed = feedparser.parse("https://finance.yahoo.com/news/rssindex")
        entries = getattr(feed, "entries", [])[:20]
        if not entries:
            return ""

        chunks = []
        for idx, entry in enumerate(entries, start=1):
            title = str(getattr(entry, "title", "") or "").strip()
            summary = str(getattr(entry, "summary", "") or "").strip()
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = re.sub(r"\s+", " ", summary).strip()
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


def _clean_news_text(raw_text):
    text = str(raw_text or "").strip()
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_global_market_news():
    """
    Multi-source RSS ingestion + title similarity dedup + weighted ranking.
    Returns:
      top_news: List[dict] (Top 50)
      context_text: str (LLM 입력 텍스트)
      raw_count: int (중복 제거 전 원본 수집 건수)
    """
    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    rss_sources = {
        "Yahoo Finance": {
            "url": "https://finance.yahoo.com/news/rssindex",
            "weight": 1.0,
        },
        "CNBC": {
            "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000",
            "weight": 0.9,
        },
        "Google News Finance": {
            "url": "https://news.google.com/rss/search?q=finance+market+economy&hl=en-US&gl=US&ceid=US:en",
            "weight": 0.8,
        },
        "MarketWatch": {
            "url": "http://feeds.marketwatch.com/marketwatch/marketpulse/",
            "weight": 0.7,
        },
    }

    all_news = []
    for source_name, cfg in rss_sources.items():
        feed_url = cfg.get("url", "")
        weight = float(cfg.get("weight", 0.0))
        try:
            response = requests.get(feed_url, headers=browser_headers, timeout=8)
            if response.status_code != 200:
                continue

            feed = feedparser.parse(response.content)
            entries = getattr(feed, "entries", []) or []
            for entry in entries:
                title = _clean_news_text(getattr(entry, "title", ""))
                summary = _clean_news_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
                published_raw = str(getattr(entry, "published", "") or getattr(entry, "updated", "") or "").strip()
                published_dt = _to_utc_datetime(
                    getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
                )
                if not (title or summary):
                    continue
                all_news.append(
                    {
                        "title": title,
                        "summary": summary,
                        "published": published_raw if published_raw else "N/A",
                        "published_dt": published_dt,
                        "source": source_name,
                        "weight": weight,
                    }
                )
        except Exception:
            # 개별 피드 실패는 전체 파이프라인을 중단시키지 않음
            continue

    deduped_news = []
    similarity_threshold = 0.7
    for news in all_news:
        duplicate_idx = None
        for idx, kept in enumerate(deduped_news):
            title_sim = SequenceMatcher(None, news["title"].lower(), kept["title"].lower()).ratio()
            if title_sim >= similarity_threshold:
                duplicate_idx = idx
                break

        if duplicate_idx is None:
            deduped_news.append(news)
            continue

        kept_news = deduped_news[duplicate_idx]
        if news["weight"] > kept_news["weight"]:
            deduped_news[duplicate_idx] = news
        elif news["weight"] == kept_news["weight"] and news["published_dt"] > kept_news["published_dt"]:
            deduped_news[duplicate_idx] = news

    ranked_news = sorted(
        deduped_news,
        key=lambda x: (x.get("weight", 0.0), x.get("published_dt", datetime.min.replace(tzinfo=timezone.utc))),
        reverse=True,
    )
    top_news = ranked_news[:50]

    chunks = []
    for idx, item in enumerate(top_news, start=1):
        chunks.append(
            "\n".join(
                [
                    f"[{idx}] Source: {item.get('source', 'Unknown')} (Weight: {item.get('weight', 0.0):.1f})",
                    f"Published: {item.get('published', 'N/A')}",
                    f"Title: {item.get('title', '')}",
                    f"Summary: {item.get('summary', '')}",
                ]
            )
        )
    context_text = "\n\n".join(chunks).strip()

    for item in top_news:
        item.pop("published_dt", None)
    return top_news, context_text, len(all_news)


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
            "max_output_tokens": 4096,
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
        all_tuple = tuple(sorted(set(all_tickers)))

        try:
            raw = yf.download(
                list(all_tuple), period="1y", interval="1d",
                auto_adjust=True, progress=False, threads=True
            )
            if isinstance(raw.columns, pd.MultiIndex):
                closes = raw["Close"] if "Close" in raw.columns.get_level_values(0) else pd.DataFrame()
                volumes = raw["Volume"] if "Volume" in raw.columns.get_level_values(0) else pd.DataFrame()
            else:
                closes = raw
                volumes = pd.DataFrame()
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
{quant_section}
[뉴스 데이터]
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
      "driver": "무엇이 이 테마를 촉발했는가?",
      "winners": "정량+정성 모두 확인된 수혜주 (예: NVDA, MSFT, SOXX)",
      "emerging": "뉴스 모멘텀은 있으나 가격 확인 필요 종목 (예: ARM, MRVL)",
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
            "max_output_tokens": 4096,
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
                "temperature": 0.0,
                "top_p": 1,
                "top_k": 1,
                "max_output_tokens": 4096,
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
                _price_map = fetch_latest_prices_for_tickers(_wl_tickers)
                _rsi_map, _ma200_map = {}, {}
                for _tk in _wl_tickers:
                    try:
                        _hist = yf.Ticker(_tk).history(period="1y", auto_adjust=False)
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
        if isinstance(_cur_nav, str) and _cur_nav in _LEGACY_NAV_STR_TO_INDEX:
            st.session_state[_nav_key] = _MAIN_NAV_OPTIONS[_LEGACY_NAV_STR_TO_INDEX[_cur_nav]]
        else:
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
    
    # 사이드바 투자 메모 (간단 버전) - 전체 기능은 🔔 Buy Watchlist & Alert 탭에서
    watch_note = st.sidebar.text_area("투자 메모", key="watch_note_input", placeholder="매수 근거, 리스크, 체크포인트를 기록하세요.")
    if st.sidebar.button("현재 티커 저장", use_container_width=True):
        _uid_memo = str(st.session_state.get("user_id") or "").strip()
        _cur_price = fetch_latest_prices_for_tickers([selected_ticker]).get(selected_ticker, np.nan)
        _new_wl_item = {
            "ticker": selected_ticker,
            "memo": watch_note.strip(),
            "alert_price": np.nan,
            "alert_rsi": np.nan,
            "alert_ma200": False,
            "saved_price": float(_cur_price) if pd.notna(_cur_price) else np.nan,
            "date_added": _narrative_now_kst_string(),
        }
        _wl_cur = load_watchlist_sheet(_uid_memo)
        _wl_cur = [x for x in _wl_cur if x["ticker"] != selected_ticker]
        _wl_cur.append(_new_wl_item)
        _ok_wl_save, _err_wl_save = save_watchlist_sheet(_uid_memo, _wl_cur)
        if _ok_wl_save:
            st.sidebar.success(f"{selected_ticker} Watchlist에 저장했습니다.")
            st.session_state["_watchlist_alert_checked"] = False
            st.session_state.pop("_sidebar_wl_count", None)  # 카운트 캐시 초기화
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
        st.title("📖 Quant Terminal 사용 가이드")
        st.markdown("처음 오셨나요? **실제 매수 결정부터 매도까지** 앱의 모든 탭을 어떤 순서로, 어떻게 활용하는지 단계별로 설명합니다.")
        st.info("💡 **핵심 원칙:** 이 앱은 단 하나의 신호로 매수 결정을 내리지 않습니다. Macro → Sector → Stock 3개 레이어가 모두 같은 방향을 가리킬 때만 높은 확신으로 진입할 수 있습니다.")

        st.divider()
        st.markdown("## 🗺️ 전체 투자 흐름")
        flow_cols = st.columns(5)
        flow_data = [
            ("🌐", "Step 1", "거시 환경 확인"),
            ("📰", "Step 2", "테마 발굴"),
            ("🔬", "Step 3", "종목 검증"),
            ("💼", "Step 4", "매수 & 관리"),
            ("🛡️", "Step 5", "매도 타이밍"),
        ]
        for col, (emoji, title, sub) in zip(flow_cols, flow_data):
            with col:
                st.markdown(
                    "<div style='text-align:center;padding:12px;background:#1e293b;border-radius:8px;'>"
                    f"<div style='font-size:28px'>{emoji}</div>"
                    f"<div style='font-weight:700;margin:4px 0;font-size:12px'>{title}</div>"
                    f"<div style='color:#94a3b8;font-size:11px'>{sub}</div></div>",
                    unsafe_allow_html=True,
                )

        st.divider()

        # Step 1
        with st.expander("🌐 Step 1 — 거시경제 환경 확인 (탭: 거시경제 지표)", expanded=True):
            g1c1, g1c2 = st.columns(2)
            with g1c1:
                st.success(
                    "✅ 진입 가능 신호\n\n"
                    "- Macro Score 60점 이상\n"
                    "- VIX 20 이하\n"
                    "- Fear & Greed 25~74\n"
                    "- 장단기 금리차 플러스(+)"
                )
            with g1c2:
                st.error(
                    "❌ 주의/대기 신호\n\n"
                    "- Macro Score 50점 미만\n"
                    "- VIX 30 이상\n"
                    "- Fear & Greed 10 이하\n"
                    "- 경고 지표 5개 이상"
                )
            st.markdown("**💡 팁:** VIX 히스토리에서 추세를 보세요. 숫자 하나보다 방향이 중요해요. Fear & Greed 25 이하는 역발상 매수 기회일 수 있어요.")
            st.markdown("**📌 예시:** Macro Score 74 / NEUTRAL, VIX 17.3 → 선별적 진입 가능")

        # Step 2
        with st.expander("📰 Step 2 — AI 내러티브로 테마 발굴 (탭: 시장 내러티브)", expanded=False):
            g2c1, g2c2, g2c3 = st.columns(3)
            with g2c1:
                st.info("🏆 Top Quant Picks\n\n정량 모멘텀 + 뉴스 테마 동시 확인. 가장 신뢰도 높음.")
            with g2c2:
                st.success("🎯 Winners\n\n테마별 주요 수혜주. 가격 + 내러티브 모두 확인.")
            with g2c3:
                st.warning("🌱 Emerging\n\n뉴스는 있지만 가격 미확인. Watchlist에 등록 후 대기.")
            st.markdown("**💡 팁:** Emerging 종목을 놓치지 마세요. 초기 발굴해서 Watchlist에 등록하면 타이밍을 잡을 수 있어요.")
            st.markdown("**📌 예시:** Theme: AI 인프라 Capex / Winners: NVDA, SMH / Emerging: VST, CEG → NVDA는 스캐너 검증, VST는 Watchlist 등록")

        # Step 3
        with st.expander("🎯 Step 3 — 섹터 흐름 검증 (탭: 섹터 & 자금 흐름)", expanded=False):
            st.markdown("Hidden Alpha Radar에서 내러티브 테마 ETF가 RS Score 상위에 있는지 확인하세요.")
            st.markdown("- **RS Score 상위 + 거래량 급증** = 기관 자금 유입 신호\n- 내러티브와 섹터 RS가 일치할 때 진입 확신이 높아집니다.")
            st.markdown("**📌 예시:** SOXX RS +18.3%p 거래량 1.9x → 반도체 섹터 강세. 내러티브와 완전 일치.")

        # Step 4
        with st.expander("🚀 Step 4 — AI 스캐너로 최종 종목 선별 (탭: AI 종목 스캐너)", expanded=False):
            g4c1, g4c2, g4c3 = st.columns(3)
            with g4c1:
                st.success("80점 이상\n\n강력 매수 후보.")
            with g4c2:
                st.warning("60~79점\n\n조건부 매수. 타이밍 추가 확인.")
            with g4c3:
                st.error("60점 미만\n\n관망. 다음 기회 탐색.")
            st.markdown("**💡 팁:** TOP3에 🔔 Watchlist 추가 버튼으로 바로 등록하세요. Narrative Score 높아도 Momentum Score 낮으면 시장이 아직 안 따라오는 신호예요.")

        # Step 5
        with st.expander("🔬 Step 5 — 개별 종목 진입 타이밍 (탭: 개별 종목 정밀 검사)", expanded=False):
            g5c1, g5c2 = st.columns(2)
            with g5c1:
                st.success(
                    "✅ 진입 조건\n\n"
                    "- 현재가 200일선 위\n"
                    "- RSI 40~65 사이\n"
                    "- 최근 고점 대비 5~15% 조정\n"
                    "- 실적 발표 2주 이상 남음"
                )
            with g5c2:
                st.warning(
                    "⚠️ 주의 신호\n\n"
                    "- RSI 75 이상 (과매수)\n"
                    "- 52주 신고가 바로 위\n"
                    "- 실적 발표 1주 이내\n"
                    "- 200일선 아래"
                )
            st.markdown("**📌 예시:** NVDA 현재가 $875 / 200일선 $812 ✅ / RSI 58 ✅ / 최근 고점 -8% ✅ → 진입 타이밍 양호")

        # Step 6
        with st.expander("🔔 Step 6 — Watchlist Alert 설정 (탭: Buy Watchlist & Alert)", expanded=False):
            g6c1, g6c2, g6c3 = st.columns(3)
            with g6c1:
                st.info("💰 목표 매수가\n\n원하는 가격 이하 도달 시 알림.")
            with g6c2:
                st.info("📉 RSI 과매도\n\nRSI 30 이하 설정 권장.")
            with g6c3:
                st.info("📊 200일선 근접\n\n장기 지지선 테스트 시 알림.")
            st.markdown("**💡 팁:** Alert 발동 시 바로 매수하지 말고 [3단계] 개별 종목 검사를 다시 확인하세요.")

        # Step 7
        with st.expander("💼 Step 7 — 포트폴리오 등록 & Thesis 기록 (탭: 포트폴리오 매도 레이더)", expanded=False):
            st.markdown("매수 후 등록 시 **📌 투자 Thesis를 반드시 선택**하세요. 나중에 어떤 테마로 샀는지 추적할 수 있어요.")
            st.markdown("- **Correlation Matrix:** 기존 보유 종목과 0.85 이상이면 분산 효과 없음\n- **Personal Benchmark:** 내 포트폴리오가 SPY를 이기고 있는지 확인\n- **Earnings Calendar:** 보유 종목 실적 발표일 확인")

        # Step 8
        with st.expander("🛡️ Step 8 — 매도 타이밍 잡기 (탭: 포트폴리오 매도 레이더)", expanded=False):
            g8c1, g8c2 = st.columns(2)
            with g8c1:
                st.error(
                    "🔴 즉시 매도 검토\n\n"
                    "- 상태: SELL 표시\n"
                    "- Drawdown -30% 이상\n"
                    "- 200일선 아래\n"
                    "- SPY Alpha -20%p 이하\n"
                    "- Macro Score 45점 미만 급락"
                )
            with g8c2:
                st.warning(
                    "🟡 부분 매도 / 관망\n\n"
                    "- SPY Alpha 0%p 이하\n"
                    "- VIX 30 이상 급등\n"
                    "- 내러티브 테마 약화\n"
                    "- 실적 발표 1주 전\n"
                    "- Correlation 종목 동시 하락"
                )
            st.markdown("**📌 주간 루틴:** 월요일 거시 확인 → 화요일 내러티브 실행 → 목요일 포트폴리오 점검 → 금요일 주간 AI 리포트")
            st.markdown("**📌 매도 예시:** Macro Score 급락 + VIX 급등 + 주간 리포트 축소 권장 + Drawdown 18% → 4가지 동시 발생 시 보유 물량 50% 이상 매도")

        st.divider()
        st.markdown("## ✅ 매수 전 최종 체크리스트 (5개 이상 충족 시 진입)")
        checklist = [
            ("🌐", "Macro Score 60점 이상", "거시 환경 우호적"),
            ("📰", "AI 내러티브 Winners 또는 Top Quant Picks 포함", "테마 확인"),
            ("🎯", "Hidden Alpha Radar 상위 20% 이내", "섹터 강세"),
            ("🚀", "AI 스캐너 Final Score 70점 이상", "종목 경쟁력"),
            ("🔬", "RSI 40~70 사이", "과매수/과매도 아님"),
            ("📊", "현재가 200일선 위", "장기 우상향 추세"),
            ("📅", "실적 발표 2주 이상 남음", "이벤트 리스크 없음"),
        ]
        for emoji, item, desc in checklist:
            st.markdown(f"{emoji} **{item}** — _{desc}_")

        st.divider()
        st.warning("⚠️ 면책 조항: 이 앱은 투자 참고 도구이며 투자 권유가 아닙니다. 모든 투자 결정과 결과는 투자자 본인의 책임입니다.")

    elif main_nav == _MAIN_NAV_OPTIONS[1]:
        sync_m1, sync_m2 = st.columns([1, 3])
        with sync_m1:
            if st.button("🔄 현재 페이지 데이터 동기화", key="sync_tab_macro", use_container_width=True):
                tab_sync_refresh(
                    [cached_analyze_us_macro_dashboard.clear],
                    rerun_after=True,
                )
        with sync_m2:
            st.caption("불러온 지표는 세션 동안 캐시됩니다. 동기화 시 캐시를 비우고 최신 데이터를 다시 가져옵니다.")
    
        st.subheader(f"{_MAIN_NAV_OPTIONS[0]} · 미국 거시경제 대시보드")
        st.caption("yfinance + FRED API + CNN Fear & Greed 기준. 판단은 참고용 휴리스틱입니다.")

        try:
            with st.spinner("매크로 지표를 불러오는 중..."):
                macro_pack = cached_analyze_us_macro_dashboard()

            if macro_pack.get("na_total", 0) >= 4:
                _notify_yfinance_fetch_failed()

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
            macro_traffic_light(macro_pack["bad_total"])

            st.divider()

            # ── 2. 지표별 히스토리 차트 ───────────────────────────────────
            st.markdown("### 📈 주요 지표 트렌드 (2년 히스토리)")
            with st.spinner("히스토리 데이터 로딩 중..."):
                hist_data = fetch_macro_history_series()

            chart_tab1, chart_tab2, chart_tab3, chart_tab4, chart_tab5 = st.tabs(
                ["VIX", "CPI YoY", "실업률", "Fed Rate", "DXY"]
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
| 지표 | 의미 | 주식 시장 영향 |
|---|---|---|
| 장단기 금리차 | 경기 침체 예고 신호 | 역전 시 침체 우려, 정상화 시 폭락 가능 |
| VIX | 투자자 공포·탐욕 수준 | 20 이하 안정, 35 이상 극단적 공포 |
| WTI 유가 | 인플레이션·비용 압력 | 급등 시 성장주 밸류에이션 타격 |
| 실업률 (샴) | 소비 체력·침체 진입 | 샴 발동 시 실물 위기 시작 |
| CPI YoY | 연준 금리 결정 방향 | 3% 이상 시 고금리 유지 (성장주 악재) |
| DXY | 글로벌 달러 위상 | 강세 시 다국적 기업 해외 실적 악화 |
| Fear & Greed | 시장 심리 종합 | 75+ 극단적 탐욕(매도 고려), 25- 극단적 공포(매수 고려) |
| Fed Funds Rate | 현재 기준금리 | 5% 이상 고금리 레짐, 인하 시 성장주 재평가 |
""")

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
            st.subheader(f"{_MAIN_NAV_OPTIONS[1]} · 테마와 자본 이동 관제탑")
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
                    st.write("📥 1단계: 글로벌 RSS 뉴스 피드 접속 및 데이터 수집 중...")
                    top_news, news_text, raw_news_count = cached_fetch_global_market_news_pack()
                    if raw_news_count == 0:
                        st.write(":red[RSS 수집 에러 발생: 유효한 뉴스 데이터를 확보하지 못했습니다.]")
                        status.update(label="❌ 분석 중단 (에러 발생)", state="error", expanded=True)
                        st.error("❌ 실시간 뉴스 수집에 실패했습니다.")
                    else:
                        st.write(f"✔️ 수집 완료: 총 {raw_news_count}개 뉴스 확보")
    
                        st.write("⚙️ 2단계: 뉴스 중복 제거(Deduplication) 및 중요도 랭킹 산출 중...")
                        st.write(f"✔️ 중복 제거/랭킹 완료: Top {len(top_news)}개 핵심 뉴스 선별")
                        st.session_state["latest_top_news"] = top_news
    
                        st.write("📊 3단계: ETF Universe 정량 스크리닝 (RS·모멘텀·거래량 분석) 중...")
                        quant_result = compute_quantitative_sector_leaders(top_n=30)
                        if quant_result.get("leaders"):
                            st.write(f"✔️ 정량 스크리닝 완료: RS 상위 {len(quant_result['leaders'])}개 종목 선별 | 강세 섹터: {', '.join(quant_result.get('top_sectors', []))}")
                        else:
                            st.write("⚠️ 정량 스크리닝 데이터 없음 — 뉴스 분석만으로 진행")

                        st.write("🧠 4단계: 뉴스 + 정량 데이터를 Gemini 2.5 Flash 엔진으로 전송하여 내러티브 분석 중...")
                        narrative_data = generate_market_narrative(news_text, selected_language, quant_data=quant_result)
                        st.write("🔍 5단계: AI 응답 수신 완료. JSON 데이터 파싱 및 필터링 중...")
    
                        if narrative_data:
                            st.session_state["narrative_history"].append(narrative_data)
                            st.session_state["current_view"] = narrative_data
                            st.session_state["current_view_language"] = selected_language
                            append_narrative_history_record(narrative_data, selected_language)
                            st.session_state["narrative_history_disk_records"] = load_narrative_history_records()
                            status.update(label="✅ 분석 완료! (성공)", state="complete", expanded=False)
                            st.success(
                                "분석 결과를 Google 스프레드시트 `Quant_DB`의 **`Narratives`** 탭에 한 행 추가했습니다. "
                                f"(수집 {raw_news_count}건 / LLM 투입 Top {len(top_news)})"
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
    
        if st.session_state.get("current_view") and st.session_state.get("current_view_language") != selected_language:
            with st.spinner("선택한 언어로 결과를 자연스럽게 번역 중입니다..."):
                translated = translate_narrative_json(st.session_state["current_view"], selected_language)
                if translated:
                    st.session_state["current_view"] = translated
                    st.session_state["current_view_language"] = selected_language
                else:
                    st.warning("언어 전환 번역에 실패하여 기존 결과를 유지합니다.")
    
        narrative_data = st.session_state.get("current_view", {})
        regime_data = narrative_data.get("regime", {}) if isinstance(narrative_data, dict) else {}
        themes_data = narrative_data.get("themes", []) if isinstance(narrative_data, dict) else []
        rotation_data = narrative_data.get("rotation", "") if isinstance(narrative_data, dict) else ""
        summary_data = narrative_data.get("summary", "") if isinstance(narrative_data, dict) else ""
        top_quant_picks = narrative_data.get("top_quant_picks", "") if isinstance(narrative_data, dict) else ""
    
        if not narrative_data:
            st.warning("아직 AI 분석 결과가 없습니다. 상단 버튼을 눌러 실시간 내러티브를 생성하세요.")
        else:
            st.info(
                "현재 화면에 표시되는 것은 **가장 최근 내러티브 스냅샷**입니다. "
                "저장된 기록을 날짜별로 묶어 보려면 하단 **시계열 분석 엔진**을 사용하세요."
            )
    
        st.markdown("### 🧭 Market Regime Indicator")
        regime_col_1, regime_col_2, regime_col_3 = st.columns(3)
        with regime_col_1:
            st.metric(
                "Risk On / Risk Off",
                regime_data.get("risk", "N/A"),
            )
        with regime_col_2:
            st.metric(
                "Growth vs Value",
                regime_data.get("growth_value", "N/A"),
            )
        with regime_col_3:
            st.metric(
                "Liquidity",
                regime_data.get("liquidity", "N/A"),
            )
    
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
                    # 과거 누적 데이터(문자열 포맷)와의 하위 호환
                    fallback_text = str(expanding_to_data or "N/A").strip()
                    expanding_to_display = f"- ➔ **Legacy Flow**: `{fallback_text}`"
    
                momentum_note = str(theme.get("momentum_note", "") or "").strip()
                emerging_tickers = str(theme.get("emerging", "") or "").strip()
                winners_str = str(theme.get("winners", "") or "N/A").strip()

                # 모멘텀 강도 이모지
                if "강함" in momentum_note or "strong" in momentum_note.lower():
                    mom_emoji = "🔥"
                elif "약함" in momentum_note or "weak" in momentum_note.lower():
                    mom_emoji = "❄️"
                else:
                    mom_emoji = "📊"

                with st.expander(f"Theme {idx}: {title} {mom_emoji}", expanded=(idx == 1)):
                    # Winners + 모멘텀 점수 강조
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

        # ── 기능 2: Emerging 종목 정량 교차 검증 ─────────────────────────
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
                        verified = verify_emerging_with_quant(all_emerging)

                    if not verified:
                        st.warning("Emerging 종목 검증 데이터를 가져오지 못했습니다.")
                    else:
                        # 최적 매수 타이밍 종목 강조
                        best = [v for v in verified if "최적" in v["verdict"]]
                        early = [v for v in verified if "얼리" in v["verdict"]]

                        if best:
                            st.success(f"🎯 **최적 매수 타이밍 {len(best)}개** — 아직 저평가 + 거래량 급증!")
                            for v in best:
                                vol_str = f"거래량 {v['vol_surge']:.1f}x" if v["vol_surge"] else ""
                                st.markdown(
                                    f"**{v['ticker']}** — RS {v['rs_score']:+.1f}%p / "
                                    f"1개월 {v['mom_1m']:+.1f}% / {vol_str} | _{v['detail']}_"
                                )
                                if st.button(f"🔔 {v['ticker']} Watchlist 추가", key=f"em_wl_{v['ticker']}", use_container_width=False):
                                    _uid_em = str(st.session_state.get("user_id") or "").strip()
                                    _em_item = {
                                        "ticker": v["ticker"],
                                        "memo": f"Emerging 검증 - {v['verdict']}",
                                        "alert_price": np.nan,
                                        "alert_rsi": 30.0,
                                        "alert_ma200": True,
                                        "saved_price": np.nan,
                                        "date_added": _narrative_now_kst_string(),
                                    }
                                    _em_wl = load_watchlist_sheet(_uid_em)
                                    _em_wl = [x for x in _em_wl if x["ticker"] != v["ticker"]]
                                    _em_wl.append(_em_item)
                                    save_watchlist_sheet(_uid_em, _em_wl)
                                    st.success(f"✅ {v['ticker']} Watchlist 추가!")
                                    st.session_state.pop("_sidebar_wl_count", None)

                        if early:
                            st.info(f"🌱 **얼리버드 기회 {len(early)}개** — 아직 초기, 200일선 위")
                            for v in early:
                                st.markdown(f"**{v['ticker']}** — {v['detail']}")

                        # 전체 결과 테이블
                        with st.expander("📋 전체 Emerging 검증 결과", expanded=False):
                            em_rows = []
                            for v in verified:
                                em_rows.append({
                                    "티커": v["ticker"],
                                    "판정": v["verdict"],
                                    "RS Score": f"{v['rs_score']:+.1f}%p" if v["rs_score"] is not None else "N/A",
                                    "1개월 수익률": f"{v['mom_1m']:+.1f}%" if v["mom_1m"] is not None else "N/A",
                                    "200일선": "위 ✅" if v["above_ma200"] else ("아래 ❌" if v["above_ma200"] is False else "N/A"),
                                    "거래량 배율": f"{v['vol_surge']:.1f}x" if v["vol_surge"] else "N/A",
                                })
                            st.dataframe(pd.DataFrame(em_rows), use_container_width=True, hide_index=True)
    
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
        st.subheader(f"{_MAIN_NAV_OPTIONS[3]} · 듀얼 엔진")
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
    
        with tab_scan_leaders:
            st.markdown("##### 🔥 Current Leaders")
    
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
    
            snap = st.session_state.get("scanner_results")
            if isinstance(snap, dict) and isinstance(snap.get("score_df"), pd.DataFrame) and not snap["score_df"].empty:
                render_opportunity_scanner_snapshot(snap)
            elif not run_scanner:
                st.caption("스캔을 실행하면 결과가 세션에 저장되며, 사이드바·다른 메뉴로 이동해도 유지됩니다.")
    
        with tab_scan_emerge:
            st.markdown("##### 🚀 Emerging Opportunities")
    
            selected_sector_labels_em = []
            manual_tickers_input_em = ""
            if scanner_data_src == _OPPORTUNITY_SCANNER_DATA_SOURCE_OPTIONS[2]:
                st.caption(
                    "Data Source가 수동일 때만 아래에서 유니버스를 구성합니다. "
                    "(Current Leaders 서브탭과 동일한 옵션 구조이며, 선택 값은 서브탭별로 독립입니다.)"
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
                        st.success("Emerging Opportunities 스캔 완료 — 결과가 세션에 저장되었습니다.")
    
            snap_em = st.session_state.get("scanner_results_emerging")
            if isinstance(snap_em, dict) and isinstance(snap_em.get("score_df"), pd.DataFrame) and not snap_em["score_df"].empty:
                render_opportunity_emerging_snapshot(snap_em)
            elif not run_emerge:
                st.caption("Emerging 엔진 스캔을 실행하면 RSI·거래량 가속 지표와 함께 결과가 세션에 유지됩니다.")
    
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
                last_run_str = last_run.astimezone(_KST_TZ).strftime("%m/%d %H:%M") if last_run else "이번 세션 미실행"
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

        st.subheader(f"{_MAIN_NAV_OPTIONS[2]} · 섹터 ETF 상대 강도")
        st.caption("주요 섹터/테마 ETF 상대 강도 점검 (2년 데이터 기반)")
        st.markdown(f"기준 티커: `{selected_ticker}`")
    
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
        etf_radar_universe = load_etf_universe_tickers()
        st.caption(
            f"`{_ETF_UNIVERSE_FILE.name}`에서 **{len(etf_radar_universe)}**개 티커를 읽었습니다. "
            "최근 **5·10·21·63 거래일** 관점으로 1주~3개월 수익률을 일괄 계산합니다."
        )
        st.info(
            "⚙️ 추적하는 ETF 리스트를 변경하거나 추가하고 싶다면, 코드 수정 없이 폴더 안의 "
            "`etf_universe.txt` 파일만 수정하시면 앱에 즉시 반영됩니다."
        )
        if st.button("지금 돈이 몰리는 미지의 ETF 찾기", key="hidden_alpha_radar_btn"):
            if not etf_radar_universe:
                st.warning(
                    f"`{_ETF_UNIVERSE_FILE}` 파일이 없거나 티커가 비어 있습니다. "
                    "프로젝트 폴더에 파일을 두고 티커를 입력해주세요."
                )
            else:
                etf_universe_sorted_tuple = tuple(sorted(set(etf_radar_universe)))
                ret_cols = ["1주(%)", "2주(%)", "1개월(%)", "3개월(%)"]
                with st.spinner("Hidden Alpha Radar: 유니버스 수익률·순위 계산 중 (포트폴리오와 동일 캐시)..."):
                    radar_df = cached_etf_universe_rankings_full(etf_universe_sorted_tuple)
    
                if radar_df is None or radar_df.empty:
                    st.warning(
                        "유니버스 랭킹을 계산하지 못했습니다. `etf_universe.txt` 티커·네트워크를 확인해주세요."
                    )
                else:
                    tmp_show = radar_df.copy()
                    tmp_show["티커"] = tmp_show.apply(
                        lambda r: f"{r['Ticker']} 🔥" if bool(r["주도주"]) else r["Ticker"], axis=1
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

        if st.button("🔍 Early Signal 스캔", key="early_signal_btn", type="primary", use_container_width=True):
            with st.spinner("RS Score 주간 변화율 계산 중... (약 30초 소요)"):
                rs_change_df = compute_rs_score_weekly_change(etf_radar_universe)

            if rs_change_df.empty:
                st.warning("RS 변화율 데이터를 가져오지 못했습니다.")
            else:
                # 신호별 분류
                early_df = rs_change_df[rs_change_df["RS_Signal"] == "🌱 Early Signal"]
                surge_df = rs_change_df[rs_change_df["RS_Signal"] == "🚀 급부상"]
                weak_df = rs_change_df[rs_change_df["RS_Signal"] == "⚠️ 모멘텀 약화"]

                if not early_df.empty:
                    st.success(f"🌱 **Early Signal {len(early_df)}개** — 아직 안 올랐지만 강해지기 시작한 섹터!")
                    for _, row in early_df.iterrows():
                        st.markdown(
                            f"**{row['Ticker']}** — RS {row['RS_Now']:+.1f}%p "
                            f"(1주 변화: {row['RS_Change']:+.1f}%p) "
                            f"→ Watchlist 등록 고려"
                        )

                es_col1, es_col2 = st.columns(2)
                with es_col1:
                    if not surge_df.empty:
                        st.info(f"🚀 **급부상 {len(surge_df)}개** — 강하면서 더 강해지는 중")
                        for _, row in surge_df.iterrows():
                            st.markdown(f"**{row['Ticker']}** RS {row['RS_Now']:+.1f}%p / 주간 +{row['RS_Change']:.1f}%p")
                with es_col2:
                    if not weak_df.empty:
                        st.warning(f"⚠️ **모멘텀 약화 {len(weak_df)}개** — 주의 필요")
                        for _, row in weak_df.iterrows():
                            st.markdown(f"**{row['Ticker']}** RS {row['RS_Now']:+.1f}%p / 주간 {row['RS_Change']:.1f}%p")

                with st.expander("📋 전체 RS 변화율 테이블", expanded=False):
                    def _style_rs_change(val):
                        v = pd.to_numeric(val, errors="coerce")
                        if pd.isna(v): return ""
                        return "color:#16a34a;font-weight:600" if v > 2 else "color:#dc2626;font-weight:600" if v < -2 else ""
                    styled_rs = rs_change_df.style.map(_style_rs_change, subset=["RS_Change"])
                    st.dataframe(styled_rs, use_container_width=True, hide_index=True)

        # ── 기능 3: 섹터 꺾임 감지 ───────────────────────────────────────
        st.divider()
        st.subheader("🛡️ 섹터 꺾임 감지 — 매도 타이밍 선제 포착")
        st.caption(
            "2주 연속 RS Score 하락 + 거래량 감소 패턴을 자동으로 감지합니다. "
            "보유 종목의 섹터 ETF가 여기 뜨면 **선제적 매도를 고려**하세요."
        )

        if st.button("🔍 섹터 꺾임 스캔", key="sector_reversal_btn", use_container_width=True):
            with st.spinner("섹터 모멘텀 반전 신호 분석 중..."):
                reversal_alerts = detect_sector_momentum_reversal(etf_radar_universe)

            if not reversal_alerts:
                st.success("✅ 현재 감지된 꺾임 신호 없음 — 전반적으로 모멘텀 유지 중입니다.")
            else:
                st.warning(f"⚠️ **{len(reversal_alerts)}개 섹터에서 꺾임 신호 감지!**")
                for alert in reversal_alerts:
                    is_critical = "분산 매도" in alert["signal"] or "급격" in alert["signal"]
                    with st.expander(
                        f"{alert['signal']} **{alert['ticker']}** — RS {alert['rs_now']:+.1f}%p / 주간 변화 {alert['rs_change']:+.1f}%p",
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
        st.subheader(_MAIN_NAV_OPTIONS[4])
        st.caption(
            "사이드바의 분석 티커 기준입니다. **상단**에서 펀더멘털·KPI(또는 ETF 건전성)를 확인한 뒤, **하단**에서 RSI·이동평균으로 매수 타점을 점검하세요."
        )
        st.markdown(f"**분석 티커:** `{selected_ticker}`")
    
        st.markdown("### 체력 검사 (Fundamentals)")
        st.caption("yfinance 기반 KPI 점검 (Pass/Fail)")
        syn_f1, syn_f2 = st.columns([1, 3])
        with syn_f1:
            if st.button("🔄 현재 페이지 데이터 동기화", key="sync_tab_fund", use_container_width=True):
                tab_sync_refresh(
                    [
                        cached_evaluate_kpis_snapshot.clear,
                        cached_etf_holdings_universe_str.clear,
                        cached_build_etf_holdings_performance_pairs.clear,
                    ],
                    rerun_after=True,
                )
        with syn_f2:
            st.caption("종목별 재무·ETF 보유 데이터 캐시를 비워 최신 재조회 결과를 받습니다.")
    
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
                    kpi_df, pass_count, fail_count, nodata_count, margin_context = cached_evaluate_kpis_snapshot(
                        selected_ticker
                    )
    
                c1, c2, c3, c4 = st.columns(4)
                total_kpis = len(kpi_df)
                c1.metric("총 KPI", total_kpis)
                c2.metric("Pass", int(pass_count))
                c3.metric("Fail", int(fail_count))
                c4.metric("No Data", int(nodata_count))
    
                with st.container():
                    final_core_pass = bool(margin_context.get("core_fcf_pass"))
                    if final_core_pass:
                        st.success("✅ [최종 합격] 필수 재무 건전성(현금창출력)이 검증된 종목입니다.")
                    else:
                        st.error("🚨 [최종 불합격] 잉여현금흐름 적자 기업. 투자를 권장하지 않습니다.")
    
                st.divider()
                st.subheader(f"{selected_ticker} KPI 대시보드")
    
                category_order = [
                    "수익성 (Profitability)",
                    "건전성 (Financial Strength)",
                    "밸류에이션 (Valuation)",
                    "모멘텀 (Momentum)",
                ]
    
                for category in category_order:
                    st.markdown(f"### {category}")
                    cat_df = kpi_df[kpi_df["Category"] == category].reset_index(drop=True)
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
                        intrinsic_col, mos_col = st.columns(2)
                        with intrinsic_col:
                            st.metric(
                                "적정 주가 (Intrinsic Value)",
                                num_str(margin_context.get("intrinsic_value")),
                                delta=(
                                    f"EPS {num_str(margin_context.get('trailing_eps'))} / "
                                    f"성장률 {pct_points_str(margin_context.get('growth_percent'))}"
                                ),
                            )
                        with mos_col:
                            st.metric(
                                "안전마진 (Margin of Safety %)",
                                pct_points_str(margin_context.get("margin_of_safety")),
                                delta=f"현재가 {num_str(margin_context.get('current_price'))}",
                            )
    
                    st.divider()
    
                with st.expander("원본 KPI 테이블 보기"):
                    st.dataframe(
                        kpi_df[["Category", "KPI", "Value", "Rule", "Pass"]],
                        use_container_width=True,
                        hide_index=True,
                    )
    
        except Exception as e:
            st.error("펀더멘털(체력 검사) 데이터를 불러오거나 계산하는 중 오류가 발생했습니다.")
            st.exception(e)
    
        st.divider()
        st.markdown("### 📊 기술적 분석 (Technical Analysis)")
        st.caption("RSI · MACD · 볼린저밴드 · 거래량으로 진입 구간 점검")
        syn_t1, syn_t2 = st.columns([1, 3])
        with syn_t1:
            if st.button("🔄 현재 페이지 데이터 동기화", key="sync_tab_timing", use_container_width=True):
                tab_sync_refresh(
                    [cached_timing_price_history.clear],
                    rerun_after=True,
                )
        with syn_t2:
            st.caption("가격 이력 캐시를 비워 다음 로드부터 최근 1년 OHLC를 다시 받습니다.")

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
            st.caption("최근 분기별 EPS 예상 vs 실제. 어닝 서프라이즈가 꾸준히 양수면 실적 퀄리티가 높은 종목입니다.")
            try:
                with st.spinner("어닝 데이터 불러오는 중..."):
                    earn_df = cached_earnings_history(str(selected_ticker).strip().upper())

                if earn_df.empty:
                    st.info("어닝 히스토리 데이터를 가져오지 못했습니다.")
                else:
                    # 컬럼 정규화
                    earn_df.columns = [str(c).strip() for c in earn_df.columns]
                    # Estimate, Actual, Surprise 컬럼 찾기
                    est_col = next((c for c in earn_df.columns if "estimate" in c.lower()), None)
                    act_col = next((c for c in earn_df.columns if "actual" in c.lower() or "reported" in c.lower()), None)

                    if est_col and act_col:
                        earn_df[est_col] = pd.to_numeric(earn_df[est_col], errors="coerce")
                        earn_df[act_col] = pd.to_numeric(earn_df[act_col], errors="coerce")
                        earn_df["Surprise(%)"] = ((earn_df[act_col] - earn_df[est_col]) / earn_df[est_col].abs() * 100).round(1)
                        earn_df["판정"] = earn_df["Surprise(%)"].apply(
                            lambda x: "✅ Beat" if pd.notna(x) and x > 0 else ("❌ Miss" if pd.notna(x) and x < 0 else "N/A")
                        )

                        beat_count = (earn_df["판정"] == "✅ Beat").sum()
                        total_count = len(earn_df[earn_df["판정"] != "N/A"])
                        if total_count > 0:
                            beat_rate = beat_count / total_count * 100
                            if beat_rate >= 75:
                                st.success(f"🏆 어닝 Beat 비율: {beat_rate:.0f}% ({beat_count}/{total_count}) — 실적 퀄리티 우수")
                            elif beat_rate >= 50:
                                st.warning(f"🟡 어닝 Beat 비율: {beat_rate:.0f}% ({beat_count}/{total_count}) — 보통")
                            else:
                                st.error(f"🔴 어닝 Beat 비율: {beat_rate:.0f}% ({beat_count}/{total_count}) — 실적 부진")

                    def _style_surprise(val):
                        v = pd.to_numeric(str(val).replace("%",""), errors="coerce")
                        if pd.isna(v): return ""
                        return "color:#16a34a;font-weight:600" if v > 0 else "color:#dc2626;font-weight:600"

                    display_cols = [c for c in earn_df.columns if c not in ["판정"]]
                    if "Surprise(%)" in earn_df.columns:
                        styled_earn = earn_df.style.map(_style_surprise, subset=["Surprise(%)"])
                        st.dataframe(styled_earn, use_container_width=True, hide_index=True)
                    else:
                        st.dataframe(earn_df, use_container_width=True, hide_index=True)
            except Exception as _ee:
                st.warning(f"어닝 히스토리 로드 오류: {_ee}")

            # ── 기관 보유 비중 ────────────────────────────────────────────
            st.divider()
            st.markdown("### 🏦 기관 투자자 보유 현황")
            st.caption("상위 기관의 보유 비중. 기관 보유 비중이 높고 증가 추세이면 스마트머니 유입 신호입니다.")
            try:
                with st.spinner("기관 보유 데이터 불러오는 중..."):
                    inst_df = cached_institutional_holders(str(selected_ticker).strip().upper())

                if inst_df.empty:
                    st.info("기관 보유 데이터를 가져오지 못했습니다.")
                else:
                    inst_df.columns = [str(c).strip() for c in inst_df.columns]
                    # % Held 컬럼 포맷
                    pct_col = next((c for c in inst_df.columns if "%" in c or "pct" in c.lower() or "held" in c.lower()), None)
                    if pct_col:
                        inst_df[pct_col] = pd.to_numeric(inst_df[pct_col], errors="coerce")
                        total_inst = inst_df[pct_col].sum() * 100 if inst_df[pct_col].max() <= 1 else inst_df[pct_col].sum()
                        st.metric("상위 10개 기관 합산 보유 비중", f"{total_inst:.1f}%")
                    st.dataframe(inst_df, use_container_width=True, hide_index=True)
            except Exception as _ie:
                st.warning(f"기관 보유 데이터 로드 오류: {_ie}")

        # ── AI 종합 진단 ─────────────────────────────────────────────────
        st.divider()
        st.markdown("### 🤖 AI 종합 진단")
        st.caption("펀더멘털 + 기술적 지표를 Gemini AI가 종합 분석해서 투자 판단을 제공합니다.")

        if st.button("🤖 AI 진단 실행", key="ai_diagnosis_btn", type="primary", use_container_width=True):
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

                _diag_prompt = f"""당신은 월가 수석 퀀트 애널리스트입니다.
아래 실제 데이터를 바탕으로 **{selected_ticker}** 종목에 대한 투자 진단을 한국어로 작성하세요.
데이터에 없는 내용은 추측하지 마세요.

[기술적 지표 — 실제 계산값]
- 현재가: ${f'{_diag_price:.2f}' if pd.notna(_diag_price) else 'N/A'}
- RSI(14): {f'{_diag_rsi:.1f}' if pd.notna(_diag_rsi) else 'N/A'} ({'과매수' if pd.notna(_diag_rsi) and _diag_rsi >= 70 else '조정구간' if pd.notna(_diag_rsi) and _diag_rsi < 50 else '중립'})
- MACD 히스토그램: {f'{_diag_hist_val:+.4f}' if pd.notna(_diag_hist_val) else 'N/A'} → {_macd_direction}
- 200일선: ${f'{_diag_ma200:.2f}' if pd.notna(_diag_ma200) else 'N/A'} → 현재가 {'위 (장기 우상향)' if pd.notna(_diag_price) and pd.notna(_diag_ma200) and _diag_price > _diag_ma200 else '아래 (장기 하락추세)'}
- 50일선: ${f'{_diag_ma50:.2f}' if pd.notna(_diag_ma50) else 'N/A'} → 현재가 {'위' if pd.notna(_diag_price) and pd.notna(_diag_ma50) and _diag_price > _diag_ma50 else '아래'}
- 52주 고점 대비: {f'{_diag_pct_from_high:+.1f}%' if pd.notna(_diag_pct_from_high) else 'N/A'}
- 볼린저밴드: {_bb_position}

[펀더멘털]
- {_kpi_summary}
- {_kpi_details}
- 기관 보유 비중 (상위 10개): {_inst_pct}

[출력 규칙]
1. 반드시 아래 형식 그대로 작성 (마크다운 사용)
2. 각 항목을 구체적 수치와 함께 설명 (데이터 기반)
3. "관망" 판단 시에도 구체적인 매수 진입 조건을 명시
4. 전체 200~300자 이상 작성

## 종합 판단: [매수 고려 / 관망 / 매도 고려]

**📊 기술적 분석:**
(RSI, MACD, 이동평균, 볼린저밴드 각각 언급)

**💼 펀더멘털 분석:**
(KPI 통과/실패, 안전마진, 기관 보유 언급)

**⚠️ 주요 리스크:**
(구체적으로 2가지)

**🎯 액션 플랜:**
(관망이면 구체적 진입 조건 명시. 예: RSI XX 이하 또는 MACD 양전환 시)

*본 분석은 참고용이며 투자 권유가 아닙니다.*"""

                with st.spinner("Gemini AI가 종합 진단 중... (약 15초 소요)"):
                    _diag_model = _GenAIModel(
                        "gemini-2.5-flash",
                        generation_config={"temperature": 0.0, "max_output_tokens": 4096}
                    )
                    _diag_response = _diag_model.generate_content(_diag_prompt)
                    _diag_text = _gemini_response_text_utf8_safe(_diag_response)

                if _diag_text:
                    if "매수 고려" in _diag_text:
                        st.success(_diag_text)
                    elif "매도 고려" in _diag_text:
                        st.error(_diag_text)
                    else:
                        st.warning(_diag_text)
                else:
                    st.warning("AI 진단 결과를 받지 못했습니다.")
            except Exception as _ae:
                st.error(f"AI 진단 오류: {_ae}")
    
    elif main_nav == _MAIN_NAV_OPTIONS[6]:
        syn_p1, syn_p2 = st.columns([1, 3])
        with syn_p1:
            if st.button("🔄 현재 페이지 데이터 동기화", key="sync_tab_portfolio", use_container_width=True):
                tab_sync_refresh(
                    [
                        cached_portfolio_yf_close_1y.clear,
                        cached_etf_universe_rankings_full.clear,
                        cached_yfinance_quote_type.clear,
                        _portfolio_sheet_all_values_cached.clear,
                    ],
                    rerun_after=True,
                )
        with syn_p2:
            st.caption(
                "시세·ETF 유니버스 모멘텀 랭킹(1시간 TTL)·종목 유형(quoteType) 캐시를 비우고 레이더를 다시 계산합니다."
            )
    
        st.subheader(_MAIN_NAV_OPTIONS[5])
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
                with st.expander(f"**{acct}** · {len(sub)}종목", expanded=len(sub) <= 8):
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

        with st.expander("종목 추가", expanded=True):
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
                                        upd.loc[ix, "Quantity"] = qty_ev
                                        upd.loc[ix, "Purchase_Price"] = price_ev
                                        save_portfolio(upd)
                                        st.success(
                                            f"{edit_account} / {edit_ticker} 수량·평단가를 수정해 저장했습니다."
                                        )
                                        st.rerun()

        st.markdown("### 포트폴리오 종목 삭제")
        delete_col1, delete_col2, delete_col3 = st.columns([1.4, 1.4, 1.0])
        with delete_col1:
            delete_account_options = sorted(portfolio_df["Account"].dropna().astype(str).unique().tolist()) if not portfolio_df.empty else []
            delete_account = st.selectbox(
                "삭제할 계좌 선택",
                options=delete_account_options if delete_account_options else ["(등록된 계좌 없음)"],
                index=0,
                key="portfolio_delete_account_select",
            )
        with delete_col2:
            candidate_df = portfolio_df[portfolio_df["Account"] == delete_account].copy() if not portfolio_df.empty else pd.DataFrame()
            ticker_options = candidate_df["Ticker"].dropna().astype(str).tolist() if not candidate_df.empty else []
            delete_target = st.selectbox(
                "삭제할 티커 선택",
                options=ticker_options if ticker_options else ["(등록된 티커 없음)"],
                index=0,
                key="portfolio_delete_select",
            )
        with delete_col3:
            st.write("")
            st.write("")
            if st.button("선택 종목 삭제", use_container_width=True):
                if portfolio_df.empty:
                    st.info("삭제할 종목이 없습니다.")
                elif delete_account == "(등록된 계좌 없음)":
                    st.info("삭제할 계좌가 없습니다.")
                elif delete_target == "(등록된 티커 없음)":
                    st.info("삭제할 종목이 없습니다.")
                else:
                    uid_del = str(st.session_state.get("user_id") or "").strip()
                    ok_del, derr = delete_portfolio_sheet_row(uid_del, delete_account, delete_target)
                    if not ok_del:
                        st.error(derr)
                    else:
                        st.success(f"{delete_account} / {delete_target} 종목을 삭제했습니다.")
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
                    if "SELL" in str(cell_value):
                        return "color: #d62728; font-weight: 700;"
                    if "WARNING" in str(cell_value):
                        return "color: #f59e0b; font-weight: 700;"
                    if "HOLD" in str(cell_value):
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

                styled_sell_radar = (
                    sell_radar_df.style.format(
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
                            saved_at_kst = saved_at_utc.astimezone(_KST_TZ)
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
                            with st.spinner(f"yfinance에서 {len(unique_tickers)}개 티커 가격 데이터를 받는 중..."):
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
                                    # yfinance auto_adjust=True 시 index에 tz(America/New_York)가 붙으므로
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
                                    delta="yfinance 미수신 티커",
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
                    # portfolio_df_cur 는 루프 밖에서 이미 로드됨
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
                    dt_str = dt.astimezone(_KST_TZ).strftime("%Y-%m-%d %H:%M") if dt else "날짜 불명"
                    summary_text = prev["analysis"].get("summary", "")
                    with st.expander(f"📋 {dt_str} (KST)", expanded=False):
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
        latest_summary = st.session_state.get("_weekly_summary_latest", "")
        if latest_summary:
            st.divider()
            st.success("✅ 이번 주 AI 포트폴리오 리포트")
            st.markdown(latest_summary)
            st.caption("이 리포트는 `Narratives` 시트에 자동 저장되었습니다. 투자 권유가 아닙니다.")

    elif main_nav == _MAIN_NAV_OPTIONS[7]:
        # ─────────────────────────────────────────────────────────────────────
        # 🔔 Buy Watchlist & Alert
        # 관심 종목 등록 + 매수 조건(목표가/RSI/200일선) 자동 체크
        # ─────────────────────────────────────────────────────────────────────
        st.subheader("🔔 Buy Watchlist & Alert")
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
                submitted_wl = st.form_submit_button("Watchlist에 추가", type="primary", use_container_width=True)

            if submitted_wl:
                if not wl_ticker:
                    st.warning("티커를 입력해주세요.")
                else:
                    price_now = fetch_latest_prices_for_tickers([wl_ticker])
                    new_wl_item = {
                        "ticker": wl_ticker,
                        "memo": wl_memo.strip(),
                        "alert_price": float(wl_alert_price) if wl_alert_price > 0 else np.nan,
                        "alert_rsi": float(wl_alert_rsi) if wl_alert_rsi > 0 else np.nan,
                        "alert_ma200": wl_alert_ma200,
                        "saved_price": price_now.get(wl_ticker, np.nan),
                        "date_added": _narrative_now_kst_string(),
                    }
                    wl_items = [i for i in wl_items if i["ticker"] != wl_ticker]
                    wl_items.append(new_wl_item)
                    ok_wl, err_wl = save_watchlist_sheet(uid_wl, wl_items)
                    if ok_wl:
                        st.success(f"✅ {wl_ticker} Watchlist에 추가했습니다!")
                        st.session_state["_watchlist_alert_checked"] = False
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

            with st.spinner("실시간 가격 및 지표 계산 중..."):
                price_map_wl = fetch_latest_prices_for_tickers(wl_tickers)
                rsi_map_wl, ma200_map_wl = {}, {}
                for tk in wl_tickers:
                    try:
                        hist = yf.Ticker(tk).history(period="1y", auto_adjust=False)
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
                        if st.button(f"🗑️ 삭제", key=f"wl_del_{idx}", use_container_width=True):
                            updated_wl = [x for j, x in enumerate(wl_items) if j != idx]
                            save_watchlist_sheet(uid_wl, updated_wl)
                            st.session_state["_watchlist_alert_checked"] = False
                            st.session_state.pop("_sidebar_wl_count", None)
                            st.rerun()

            # Alert 재체크 버튼
            st.divider()
            if st.button("🔄 Alert 조건 다시 체크", key="wl_recheck_btn", use_container_width=True):
                st.session_state["_watchlist_alert_checked"] = False
                st.session_state.pop("_watchlist_triggered_alerts", None)
                st.rerun()

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
