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
    """Quant_DB 스프레드시트의 Thesis 탭. (worksheet | None, err_msg | None)"""
    gc = get_gspread_client()
    if gc is None:
        return None, "Google 서비스 계정(`gcp_service_account`)이 설정되지 않았습니다."
    try:
        sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
        ws = sh.worksheet(_THESIS_WORKSHEET_TITLE)
        return ws, None
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg or "unable to find" in msg:
            try:
                sh = gc.open(_QUANT_DB_SPREADSHEET_TITLE)
                ws = sh.add_worksheet(title=_THESIS_WORKSHEET_TITLE, rows=3000, cols=7)
                ws.update([_THESIS_SHEET_COLS], range_name="A1:G1", value_input_option="USER_ENTERED")
                return ws, None
            except Exception as exc2:
                return None, f"`{_THESIS_WORKSHEET_TITLE}` 워크시트를 만들 수 없습니다: {exc2}"
        return None, f"스프레드시트 `{_QUANT_DB_SPREADSHEET_TITLE}` / `{_THESIS_WORKSHEET_TITLE}` 를 열 수 없습니다: {exc}"


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
    "🌐 [1단계] 거시경제 지표",
    "📰 [1단계] 시장 내러티브",
    "🎯 [2단계] 섹터 & 자금 흐름",
    "🚀 [2단계] AI 종목 스캐너",
    "🔬 [3단계] 개별 종목 정밀 검사",
    "🛡️ [4단계] 포트폴리오 매도 레이더",
    "🎯 [AI] 내러티브 적중률 트래커",
    "💡 [AI] Idea-to-Portfolio 추적",
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

    def generate_content(self, prompt):
        return _ensure_genai_client().models.generate_content(
            model=self._model_id,
            contents=prompt,
            config=self._config,
        )


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
    `etf_universe.txt`에서 ETF 티커 목록을 읽는다 (줄바꿈·쉼표·공백 구분, # 주석 지원).
    내부적으로 `load_etf_universe_tickers()`와 동일한 파서를 사용한다.
    """
    return load_etf_universe_tickers()


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

    bad_total = sum(1 for r in rows if macro_status_bad_count(r["_status"]))
    na_total = sum(1 for r in rows if r["_status"] == MACRO_STATUS_NA)

    return {
        "rows": rows,
        "bad_total": bad_total,
        "vix_hist": vix_hist,
        "spread_val": spread_val,
        "vix_val": vix_val,
        "na_total": na_total,
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
        info = ticker.info if ticker.info else {}
        cashflow = ticker.cashflow
        history = ticker.history(period="1y", auto_adjust=False)
    except Exception:
        info, cashflow, history = {}, None, pd.DataFrame()

    roe = to_float(info.get("returnOnEquity"))
    operating_margin = to_float(info.get("operatingMargins"))
    debt_to_equity = to_float(info.get("debtToEquity"))
    peg_ratio = to_float(info.get("pegRatio"))
    trailing_eps = to_float(info.get("trailingEps"))
    earnings_growth = to_float(info.get("earningsGrowth"))

    fcf = get_latest_series_value(cashflow, "Free Cash Flow")

    current_price, ma50, ma200 = get_momentum_values(history)
    momentum_pass = (
        not pd.isna(current_price)
        and not pd.isna(ma50)
        and not pd.isna(ma200)
        and current_price > ma50
        and current_price > ma200
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
            "Pass": pass_fail_badge(roe >= 0.15, pd.isna(roe)),
        },
        {
            "Category": "수익성 (Profitability)",
            "KPI": "Operating Margin",
            "Value": pct_str(operating_margin),
            "Rule": "0 이상",
            "Pass": pass_fail_badge(operating_margin >= 0, pd.isna(operating_margin)),
        },
        {
            "Category": "건전성 (Financial Strength)",
            "KPI": "Debt-to-Equity",
            "Value": num_str(debt_to_equity),
            "Rule": "100 미만",
            "Pass": pass_fail_badge(debt_to_equity < 100, pd.isna(debt_to_equity)),
        },
        {
            "Category": "건전성 (Financial Strength)",
            "KPI": "Free Cash Flow (최근연도)",
            "Value": won_str(fcf),
            "Rule": "0 초과",
            "Pass": pass_fail_badge(fcf > 0, pd.isna(fcf)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "PEG Ratio",
            "Value": num_str(peg_ratio),
            "Rule": "1.0 미만",
            "Pass": pass_fail_badge(peg_ratio < 1.0, pd.isna(peg_ratio)),
        },
        {
            "Category": "밸류에이션 (Valuation)",
            "KPI": "Margin of Safety",
            "Value": pct_points_str(margin_of_safety),
            "Rule": "20% 이상",
            "Pass": pass_fail_badge(margin_of_safety >= 20, pd.isna(margin_of_safety)),
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
    """최근 14일 이내 + 최대 40건(시간순 보존 후 최신만 유지)."""
    if not records:
        return []
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=_NARRATIVE_HISTORY_RETENTION_DAYS)
    migrated = []
    for rec in records:
        if not isinstance(rec, dict) or not isinstance(rec.get("analysis"), dict):
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
        st.error(
            f"🏆 TOP {rank} | {row['Ticker']} ({row['Name']}) | "
            f"Final {_scanner_ui_fmt_2f(row['Final Score'])} / 100"
        )
        fac_cols = st.columns(6)
        for i, (label, key) in enumerate(_scanner_factor_defs):
            with fac_cols[i]:
                st.metric(label, _scanner_ui_fmt_2f(row[key]))
        st.markdown(f"**Narrative Why:** {row['Narrative Why']}")
        st.markdown(f"**Risk:** {row['Risk']}")
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


def generate_market_narrative(news_text, target_language):
    if not news_text:
        return {}

    st.session_state["last_gemini_raw_text"] = ""
    language_label = "한국어" if target_language == "ko" else "English"
    prompt = f"""
당신은 월가 수석 전략가입니다.
아래 뉴스 묶음을 분석하여 지정된 JSON 스키마 그대로만 응답하세요.
중요 규칙:
1) 반드시 순수 JSON 텍스트만 출력 (```json 같은 마크다운 금지)
2) 모든 키를 빠짐없이 포함
3) themes는 최소 2개 이상 생성
4) winners는 티커를 쉼표로 구분한 문자열
5) 각 theme의 expanding_to는 반드시 객체 배열(list)이어야 함 (문자열 금지)
6) expanding_to의 각 객체는 반드시 "stage"와 "expected_tickers" 키를 포함
7) expected_tickers는 각 stage마다 반드시 2~4개 티커를 쉼표 구분 문자열로 작성
8) 결과는 반드시 {language_label}로, 금융 전문 용어를 사용하여 가장 자연스럽게 작성

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
      "winners": "수혜주 티커들 (예: NVDA, MSFT)",
      "expanding_to": [
        {{"stage": "기업용 AI 솔루션", "expected_tickers": "CRM, NOW, WDAY"}},
        {{"stage": "AI 기반 사이버 보안", "expected_tickers": "CRWD, PANW, FTNT"}}
      ],
      "risk": "이 테마가 무너질 수 있는 위험 요인"
    }}
  ],
  "rotation": "과열 섹터 -> 수혜 섹터 플로우 요약 (예: Tech -> Industrials)",
  "summary": "월가 리포트 스타일의 전체 시장 핵심 요약 (기관과 개인의 뷰 차이 포함)"
}}
You MUST respond ONLY with a valid JSON object. No markdown tags, no greetings.
"""
    try:
        response = model.generate_content(prompt)
        raw_text = _gemini_response_text_utf8_safe(response)
        st.session_state["last_gemini_raw_text"] = raw_text
        if not raw_text:
            return {}

        try:
            result_data = json.loads(raw_text)
        except Exception as e:
            import traceback

            err_text = str(e) or ""
            err_lower = err_text.lower()
            if any(token in err_lower for token in ["429", "resourceexhausted", "quota"]):
                st.warning(
                    "⚠️ API 요청 한도를 초과했습니다. 약 1분 정도 기다리신 후 다시 시도해주세요. "
                    "(무료 API는 분당 호출 횟수 제한이 있습니다)"
                )
                result_data = {}
            elif any(token in err_lower for token in ["safety", "blocked"]):
                st.warning(
                    "⚠️ 뉴스 내용 중 민감한 키워드가 포함되어 AI 안전 필터에 의해 분석이 차단되었습니다."
                )
                result_data = {}
            else:
                st.error("❌ Gemini 응답 파싱에 실패했습니다.")
                st.code(traceback.format_exc(), language="bash")
                st.error(f"🤖 Gemini 실제 답변 원문:\n\n{raw_text}")
                st.error(f"에러 메시지: {e}")
                result_data = {}

        return result_data
    except Exception as e:
        import traceback

        st.error("❌ JSON 파싱 에러가 발생했습니다.")
        st.code(traceback.format_exc(), language="bash")
        st.error(f"🤖 Gemini 실제 답변 원문:\n\n{st.session_state.get('last_gemini_raw_text', '')}")
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
            "temperature": 0.2,
            "top_p": 0.95,
            "top_k": 40,
            "max_output_tokens": 4096,
        },
    )


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
    
    watchlist_items = load_watchlist()
    
    watch_note = st.sidebar.text_area("투자 메모", key="watch_note_input", placeholder="매수 근거, 리스크, 체크포인트를 기록하세요.")
    if st.sidebar.button("현재 티커 저장", use_container_width=True):
        current_price_map = fetch_latest_prices_for_tickers([selected_ticker])
        current_price = current_price_map.get(selected_ticker, np.nan)
        new_item = {
            "ticker": selected_ticker,
            "memo": watch_note.strip(),
            "saved_price": None if pd.isna(current_price) else float(current_price),
        }
        watchlist_items = [i for i in watchlist_items if str(i.get("ticker", "")).upper() != selected_ticker]
        watchlist_items.append(new_item)
        save_watchlist(watchlist_items)
        st.sidebar.success(f"{selected_ticker} 메모를 저장했습니다.")
    
    watchlist_items = load_watchlist()
    if watchlist_items:
        tickers = [str(item.get("ticker", "")).strip().upper() for item in watchlist_items]
        latest_price_map = fetch_latest_prices_for_tickers(tickers)
    
        rows = []
        for item in watchlist_items:
            ticker = str(item.get("ticker", "")).strip().upper()
            memo = str(item.get("memo", "") or "")
            saved_price = pd.to_numeric(item.get("saved_price"), errors="coerce")
            current_price = pd.to_numeric(latest_price_map.get(ticker), errors="coerce")
            pnl_pct = np.nan
            if pd.notna(saved_price) and saved_price > 0 and pd.notna(current_price):
                pnl_pct = (float(current_price) / float(saved_price) - 1.0) * 100.0
            rows.append(
                {
                    "Ticker": ticker,
                    "메모 내용": memo,
                    "저장 시점 가격": saved_price,
                    "현재가": current_price,
                    "수익률(%)": pnl_pct,
                }
            )
    
        watch_df = pd.DataFrame(rows)
        st.sidebar.dataframe(
            watch_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "저장 시점 가격": st.column_config.NumberColumn(format="%.2f"),
                "현재가": st.column_config.NumberColumn(format="%.2f"),
                "수익률(%)": st.column_config.NumberColumn(format="%.2f"),
            },
        )
    
        for idx, item in enumerate(watchlist_items):
            ticker = str(item.get("ticker", "")).strip().upper()
            action_cols = st.sidebar.columns([2, 1])
            with action_cols[0]:
                if st.button(f"📌 {ticker} 분석", key=f"watch_pick_{idx}", use_container_width=True):
                    st.session_state["selected_ticker"] = ticker
                    st.rerun()
            with action_cols[1]:
                if st.button("삭제", key=f"watch_del_{idx}", use_container_width=True):
                    updated = [x for j, x in enumerate(watchlist_items) if j != idx]
                    save_watchlist(updated)
                    st.rerun()
    else:
        st.sidebar.caption("저장된 메모가 없습니다.")
    
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
        sync_m1, sync_m2 = st.columns([1, 3])
        with sync_m1:
            if st.button("🔄 현재 페이지 데이터 동기화", key="sync_tab_macro", use_container_width=True):
                tab_sync_refresh(
                    [cached_analyze_us_macro_dashboard.clear],
                    rerun_after=True,
                )
        with sync_m2:
            st.caption("불러온 지표는 세션 동안 캐시됩니다. 동기화 시 캐시를 비우고 최신 데이터를 다시 가져옵니다.")
    
        st.subheader(f"{_MAIN_NAV_OPTIONS[0]} · 미국 6대 지표 대시보드")
        st.caption("yfinance + FRED API fredapi(UNRATE·CPI) 기준. 판단은 참고용 휴리스틱입니다.")
    
        try:
            with st.spinner("6대 매크로 지표를 불러오는 중..."):
                macro_pack = cached_analyze_us_macro_dashboard()

            if macro_pack.get("na_total", 0) >= 4:
                _notify_yfinance_fetch_failed()

            macro_traffic_light(macro_pack["bad_total"])
    
            st.markdown(
                f"**종합 신호등 요약**: Warning 또는 Fail 상태인 지표 개수는 **{macro_pack['bad_total']} / 6** 입니다. "
                f"데이터를 가져오지 못한 지표는 **{macro_pack.get('na_total', 0)}**건입니다 (신호등 집계에는 포함하지 않음)."
            )
    
            st.divider()
    
            vix_hist = macro_pack.get("vix_hist")
            st.markdown("#### VIX (1년 추세)")
            if vix_hist is None or vix_hist.empty or "Close" not in vix_hist.columns:
                st.warning("VIX 1년 차트 데이터를 불러오지 못했습니다.")
            else:
                vix_chart = pd.DataFrame({"VIX Close": pd.to_numeric(vix_hist["Close"], errors="coerce")}).dropna(
                    how="all"
                )
                st.line_chart(vix_chart, use_container_width=True)
            st.caption(
                "💡 [VIX 판독법] 하루의 등락보다 '바닥권에서 고개를 들며 20을 돌파하는지' 추세가 중요합니다. "
                "평온한 장세에서는 주 1~2회, 시장이 출렁일 때는 매일 전고점 돌파 여부를 모니터링하세요. "
                "(6개월~1년 차트로 과거 평균 대비 현재 위치를 파악하세요.)"
            )
    
            st.divider()
    
            highlights = macro_pack["rows"]
            macro_df = pd.DataFrame(highlights)
            show_df = macro_df.drop(columns=["_status"], errors="ignore")
    
            st.markdown("###### 6대 지표 종합 카드 · 판독 결과")
            for row_idx in range(2):
                card_cols = st.columns(3)
                for col_idx in range(3):
                    ix = row_idx * 3 + col_idx
                    if ix >= len(highlights):
                        continue
                    row = highlights[ix]
                    with card_cols[col_idx]:
                        st.metric(label="판정", value=row["판정"])
                        st.caption(row["지표"])
                        st.write(f"**현재값:** {row['현재값']}")
                        st.info(row["판독 요약"])
    
            st.markdown("###### 원본 표 (복사용)")
            st.dataframe(show_df, use_container_width=True, hide_index=True, height=min(520, 140 + len(show_df) * 36))
    
            with st.expander("📖 6대 거시경제 지표 해석 가이드", expanded=False):
                st.markdown(
                    """
    | 지표 | 의미 (무엇을 나타내는가?) | 주식 시장 영향 |
    |---|---|---|
    | 장단기 금리차 | 시장이 예상하는 경기 침체의 유효성 | 마이너스(-)일 때 침체 예고, 정상화될 때 폭락 가능성 |
    | Sentiment (VIX) | 투자자들의 광기와 공포의 수준 | 80 이상 과매수 시 하락 주의, 20 이하 공포 시 매수 기회 |
    | WTI 유가 | 인플레이션 압력과 기업 생산 비용 | 급등 시 기술주(대장주) 밸류에이션에 타격 |
    | 실업률 (샴의 법칙) | 소비 체력과 경기 침체 진입 여부 | 법칙 발동 시 실물 경제 위기 시작 |
    | CPI (물가) | 연방준비제도(Fed)의 금리 결정 방향 | 예상 상회 시 고금리 유지 (성장주에 악재) |
    | 달러 지수 (DXY) | 글로벌 자산 중 달러의 위상 | 달러 강세 시 다국적 빅테크 해외 실적 악화 |
    
    *본 앱의 Pass/Warning/Fail은 위 표를 완전 반영하지 않으며, 간단한 임계치 휴리스틱을 사용합니다.*
    """
                )
    
        except Exception as e:
            st.error("거시경제 데이터를 불러오거나 계산하는 중 오류가 발생했습니다.")
            st.exception(e)
    
    elif main_nav == _MAIN_NAV_OPTIONS[1]:
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
    
                        st.write("🧠 3단계: 추출된 핵심 뉴스를 Gemini 2.5 Flash 엔진으로 전송하여 내러티브 분석 중...")
                        narrative_data = generate_market_narrative(news_text, selected_language)
                        st.write("🔍 4단계: AI 응답 수신 완료. JSON 데이터 파싱 및 필터링 중...")
    
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
    
                with st.expander(f"Theme {idx}: {title}", expanded=(idx == 1)):
                    st.markdown(
                        f"""
    - **Driver (원인):** {theme.get("driver", "N/A")}
    - **Winners (수혜주):** {theme.get("winners", "N/A")}
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
    
    elif main_nav == _MAIN_NAV_OPTIONS[3]:
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
    
    elif main_nav == _MAIN_NAV_OPTIONS[2]:
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
    
    elif main_nav == _MAIN_NAV_OPTIONS[4]:
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
        st.markdown("### 매수 타점 (Timing)")
        st.caption("RSI(14)와 장기 이동평균으로 진입 구간 점검")
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
                current_price = float(close.dropna().iloc[-1]) if not close.dropna().empty else np.nan
                ma20 = close.rolling(window=20, min_periods=20).mean()
                ma50 = close.rolling(window=50, min_periods=50).mean()
                ma200 = close.rolling(window=200, min_periods=200).mean()
                rsi_series = calculate_rsi(close, window=14)
                current_rsi = rsi_series.dropna().iloc[-1] if not rsi_series.dropna().empty else np.nan
                current_ma20 = ma20.dropna().iloc[-1] if not ma20.dropna().empty else np.nan
                current_ma50 = ma50.dropna().iloc[-1] if not ma50.dropna().empty else np.nan
                current_ma200 = ma200.dropna().iloc[-1] if not ma200.dropna().empty else np.nan
    
                is_buy_on_dip = (
                    pd.notna(current_price)
                    and pd.notna(current_ma200)
                    and current_price > current_ma200
                    and pd.notna(current_rsi)
                    and current_rsi < 50
                    and (
                        (pd.notna(current_ma20) and current_price <= current_ma20 * 1.02)
                        or (pd.notna(current_ma50) and current_price <= current_ma50 * 1.02)
                    )
                )
    
                rsi_col, info_col = st.columns([1, 2])
                with rsi_col:
                    st.metric("현재 RSI (14)", num_str(current_rsi))
    
                with info_col:
                    if pd.isna(current_rsi):
                        st.warning("RSI 계산에 필요한 데이터가 부족합니다.")
                    elif current_rsi >= 70:
                        st.error("🚨 과매수 구간 (추격 매수 금지 - 인내심을 가지세요)")
                    elif current_rsi >= 50:
                        st.warning("🟡 중립 구간 (관망)")
                    else:
                        st.success("✅ 조정 구간 (분할 매수 고려)")
    
                if is_buy_on_dip:
                    st.warning("🎯 [눌림목 포착] 장기 우상향 중인 종목의 단기 조정 구간입니다. 매수를 검토하세요!")
    
                chart_df = pd.DataFrame(
                    {
                        "Close": close,
                        "MA20": ma20,
                        "MA50": ma50,
                        "MA200": ma200,
                    }
                ).dropna(how="all")
                if chart_df.empty:
                    st.warning("차트를 표시할 데이터가 부족합니다.")
                else:
                    st.line_chart(chart_df, use_container_width=True)
    
        except Exception as e:
            st.error("매수 타점 데이터를 불러오거나 계산하는 중 오류가 발생했습니다.")
            st.exception(e)
    
    elif main_nav == _MAIN_NAV_OPTIONS[5]:
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
    
    elif main_nav == _MAIN_NAV_OPTIONS[6]:
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

        else:
            # 버튼 미클릭 상태 안내
            st.info(
                "설정을 완료한 후 **📊 적중률 분석 실행** 버튼을 클릭하면 분석이 시작됩니다.\n\n"
                "**분석 방식:**\n"
                "- `Narratives` 시트에서 현재 계정의 내러티브 기록을 불러옵니다.\n"
                "- 각 내러티브의 **Winners**와 **Emerging** 티커에 대해 생성 시점 이후 수익률을 계산합니다.\n"
                "- 설정한 적중 기준(%) 이상 상승한 티커를 '적중'으로 판정하고 AI 예측 품질을 평가합니다."
            )

    elif main_nav == _MAIN_NAV_OPTIONS[7]:
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
                    portfolio_df_cur = load_portfolio()
                    port_row = portfolio_df_cur[
                        (portfolio_df_cur["Ticker"] == tk) &
                        (portfolio_df_cur["Account"] == acct)
                    ] if not portfolio_df_cur.empty else pd.DataFrame()

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
