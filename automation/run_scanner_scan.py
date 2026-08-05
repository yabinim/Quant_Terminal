"""
run_scanner_scan.py
───────────────────
주간 3버킷 AI 종목 스캐너 자동 실행 — 토요일 5pm ET (run_weekly_report.py 직후).

앱의 「🚀 주간 3버킷 일괄 스캔」 버튼과 **완전히 동일한 함수**(scanner_core.run_three_bucket_scan)
를 호출한다. 계산은 여기서 딱 한 번만 일어나고, 그 **같은 score_df 객체**가
  ① 이메일 HTML  ② 워치리스트 70점 편입  ③ Scanner_Last_Result 시트 저장
세 곳을 모두 먹인다. 앱은 시트에 저장된 스냅샷을 **재계산 없이** 복원해 표시하므로
"이메일 숫자 ≠ 앱 숫자" 가 구조적으로 발생할 수 없다.

파이프라인:
  1) Narratives 시트에서 최신 주간 레코드(weekly_trend_7d) 로드
  2) narrative_core.weekly_scan_pools 로 winners / expanding 풀 분리 (2C)
  3) scanner_core.run_three_bucket_scan 으로 주도주·대기주·확산주 스캔
  4) Scanner_Last_Result 에 3버킷 스냅샷 저장 (run_by="auto")
  5) Scanner_History 에 기록 (엔진 라벨 정상화)
  6) 70점 이상 → Watchlist 신규 편입 (기존 행은 절대 건드리지 않음)
  7) 결과 메일 발송 (주간 브리핑 요약 + 3버킷 + 편입 내역)

⚠️ 시트 쓰기 순서: 파괴적이지 않은 순서(스냅샷 → 히스토리 → 워치리스트) 로 진행하고
   워치리스트는 **append 전용**이라 부분 실패 시 재시도해도 안전하다.

실행 환경: GitHub Actions (Ubuntu), Python 3.11+
"""

import os
import sys
import json
import html
import smtplib
import traceback
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import pytz
import gspread
from google.oauth2.service_account import Credentials
from google import genai

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import narrative_core
import scanner_core as sc
import fmp_extras as fx

# ── 환경변수 ─────────────────────────────────────────────────────────────────
GOOGLE_API_KEY     = os.environ["GOOGLE_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]
GSPREAD_KEY_JSON   = os.environ["GSPREAD_KEY"]
FMP_API_KEY        = os.environ.get("FMP_API_KEY", "").strip()

_gcp_info = json.loads(GSPREAD_KEY_JSON)

# ── 상수 ─────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")
_SPREADSHEET_TITLE = "Quant_DB"
_ADMIN_USER_ID     = "yab"

_WS_NARRATIVES  = "Narratives"
_WS_WATCHLIST   = "Watchlist"
_WS_SCAN_LAST   = "Scanner_Last_Result"
_WS_SCAN_HIST   = "Scanner_History"

# app.py `_WATCHLIST_SHEET_COLS` 와 동일. Account 는 반드시 13번째(M열).
_WL_COLS = ["ID", "Ticker", "Memo", "Alert_Price", "Alert_RSI", "Alert_MA200",
            "Saved_Price", "Date_Added", "Stop_Loss", "Target_Price",
            "Alert_States", "Alert_LastState", "Account"]
_WL_NCOL = len(_WL_COLS)
_SCAN_LAST_COLS = ["User_ID", "Engine", "Saved_At", "Universe_Count", "Payload"]

_TARGET_WEEKDAY = 5  # 토요일
_FORCE_RUN = str(os.environ.get("SCANNER_SCAN_FORCE", "")).strip() in ("1", "true", "TRUE")

_ENGINES = ("leaders", "emerging", "expansion")
_ENGINE_EMOJI = {"leaders": "🏆", "emerging": "🌱", "expansion": "🚀"}


def log(msg):
    print(msg, flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# Sheets 공통
# ══════════════════════════════════════════════════════════════════════════════
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(_gcp_info, scopes=scopes)
    return gspread.authorize(creds)


def _safe_append_rows(ws, rows, value_input_option: str = "USER_ENTERED") -> None:
    """append_row 계단식 드리프트 회피 — A열 기준 마지막 데이터 다음 행에 명시적 range 로 기록."""
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
    n_cols = max(len(r) for r in rows)
    end_cell = gspread.utils.rowcol_to_a1(end_row, n_cols)
    ws.update(rows, range_name=f"A{start_row}:{end_cell}",
              value_input_option=value_input_option)


def _get_or_create_ws(sh, title, cols, rows=1000):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=max(len(cols), 5))
        end = gspread.utils.rowcol_to_a1(1, len(cols))
        ws.update([cols], range_name=f"A1:{end}", value_input_option="USER_ENTERED")
        return ws


def _parse_saved_at(value):
    s = str(value or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 1) 최신 주간 레코드 로드
# ══════════════════════════════════════════════════════════════════════════════
def load_latest_weekly_record(ws):
    """가장 최근 weekly_trend_7d 레코드의 analysis dict 를 반환. 없으면 None."""
    values = ws.get_all_values() or []
    best, best_dt = None, None
    for row in (values[1:] if len(values) > 1 else []):
        if not row or len(row) < 5:
            continue
        if str(row[2] or "").strip() != narrative_core.NARRATIVE_SOURCE_WEEKLY_7D:
            continue
        try:
            env = json.loads(str(row[4] or ""))
        except Exception:
            continue
        if not isinstance(env, dict) or not isinstance(env.get("analysis"), dict):
            continue
        dt = _parse_saved_at(env.get("saved_at"))
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best, best_dt = env, dt
    if best is None:
        return None, None
    return best["analysis"], best_dt


# ══════════════════════════════════════════════════════════════════════════════
# 4) Scanner_Last_Result 저장 — 앱이 이 스냅샷을 재계산 없이 복원한다
# ══════════════════════════════════════════════════════════════════════════════
def save_scanner_snapshot(sh, engine: str, snap: dict) -> bool:
    """app.py `save_scanner_last_result` 와 동일 스키마로 user+engine 덮어쓰기."""
    try:
        ws = _get_or_create_ws(sh, _WS_SCAN_LAST, _SCAN_LAST_COLS, rows=100)
        score_df = snap.get("score_df")
        if not isinstance(score_df, pd.DataFrame) or score_df.empty:
            return False
        score_json = score_df.head(50).to_json(orient="records", force_ascii=False)
        saved_at = datetime.now(timezone.utc).isoformat()
        meta = {
            "mode_note": snap.get("mode_note", ""),
            "scanner_mode": snap.get("scanner_mode", ""),
            "scanner_data_source": snap.get("scanner_data_source", ""),
            "universe": snap.get("universe", []),
            "completed_at": snap.get("completed_at", saved_at),
            "schema_version": sc.SCANNER_SCHEMA_VERSION,
            # 앱 배너용 — "🤖 자동화 스캔 (토요일 이메일과 동일)" 표시 근거
            "run_by": snap.get("run_by", "auto"),
        }
        payload = json.dumps({"meta": meta, "rows": json.loads(score_json)},
                             ensure_ascii=False)
        row = [_ADMIN_USER_ID.upper(), engine, saved_at,
               str(len(snap.get("universe") or [])), payload]

        all_vals = ws.get_all_values() or []
        target = None
        for i, r in enumerate(all_vals[1:], start=2):
            if len(r) >= 2 and str(r[0]).strip().upper() == _ADMIN_USER_ID.upper() \
                    and str(r[1]).strip() == engine:
                target = i
                break
        if target:
            end = gspread.utils.rowcol_to_a1(target, len(row))
            ws.update([row], range_name=f"A{target}:{end}", value_input_option="USER_ENTERED")
        else:
            _safe_append_rows(ws, row)
        return True
    except Exception as e:
        log(f"[WARN] {engine} 스냅샷 저장 실패: {e}")
        traceback.print_exc()
        return False


def save_scanner_history(sh, engine: str, snap: dict) -> bool:
    """Scanner_History 기록.

    ⚠️ app.py `save_scanner_result_history` 는 'emerging' 만 분기해서
       expansion 결과가 "Leaders" 라벨로 저장되는 버그가 있다.
       여기서는 ENGINE_LABELS 로 정상 라벨링한다 (app.py 도 동일 수정 필요).
    """
    try:
        score_df = snap.get("score_df")
        if not isinstance(score_df, pd.DataFrame) or score_df.empty:
            return False
        ws = _get_or_create_ws(
            sh, _WS_SCAN_HIST,
            ["ID", "Date", "Engine", "Ticker", "Final_Score", "Universe_Count", "Run_By"],
            rows=5000)
        today = datetime.now(_ET).strftime("%Y-%m-%d")
        label = sc.ENGINE_LABELS.get(engine, engine)
        run_by = snap.get("run_by", "auto")
        ucount = str(len(snap.get("universe") or []))
        rows = []
        disp = sc._scanner_score_df_format_for_display(score_df.copy(), engine)
        for _, r in disp.head(20).iterrows():
            fs = pd.to_numeric(r.get("Final Score"), errors="coerce")
            rows.append([_ADMIN_USER_ID.upper(), today, label,
                         str(r.get("Ticker", "")).strip().upper(),
                         "" if pd.isna(fs) else round(float(fs), 2),
                         ucount, run_by])
        if rows:
            _safe_append_rows(ws, rows)
        return True
    except Exception as e:
        log(f"[WARN] {engine} 히스토리 저장 실패: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# 6) 워치리스트 70점 편입 — 신규만 추가, 기존 행은 절대 손대지 않음 (4A)
# ══════════════════════════════════════════════════════════════════════════════
def load_existing_watchlist_tickers(ws) -> set:
    vals = ws.get_all_values() or []
    out = set()
    for r in (vals[1:] if len(vals) > 1 else []):
        if len(r) >= 2 and str(r[0]).strip().upper() == _ADMIN_USER_ID.upper():
            tk = str(r[1]).strip().upper()
            if tk:
                out.add(tk)
    return out


def add_watchlist_rows(ws, candidates: list, existing: set) -> tuple:
    """70점 이상 신규 종목만 append.

    ⚠️ app.py `add_to_watchlist` 는 동일 티커의 기존 행을 **삭제 후 재추가**하므로
       손절가·목표가·Alert_States·Account 가 초기화된다. 자동화는 그 경로를 쓰지 않고
       신규 티커만 append 한다 (기존 행 무손상 보장).

    Returns: (추가된 목록, 건너뛴 목록)
    """
    today = datetime.now(_ET).strftime("%Y-%m-%d")
    added, skipped, rows = [], [], []
    seen = set(existing)
    for c in candidates:
        tk = c["ticker"]
        if tk in seen:
            skipped.append(c)
            continue
        seen.add(tk)
        row = [""] * _WL_NCOL
        row[0] = _ADMIN_USER_ID.upper()                              # ID
        row[1] = tk                                                  # Ticker
        row[2] = sc.build_auto_memo(c["engine"], c["score"], today)   # Memo
        row[6] = "" if c.get("price") is None else round(float(c["price"]), 4)  # Saved_Price
        row[7] = today                                               # Date_Added
        row[10] = sc.WATCHLIST_AUTO_ADD_ALERT_STATES                 # Alert_States
        row[12] = "미지정"                                            # Account (M열 고정)
        rows.append(row)
        added.append(c)
    if rows:
        _safe_append_rows(ws, rows)
    return added, skipped


# ══════════════════════════════════════════════════════════════════════════════
# 7) 이메일 — score_df 와 **같은 객체**를 표시 포맷 함수에 통과시켜 만든다
# ══════════════════════════════════════════════════════════════════════════════
def _esc(s):
    return html.escape(str(s or ""))


def _bucket_table(engine: str, snap: dict) -> str:
    if snap is None:
        return (f'<div style="color:#7a7f87;padding:10px 0;">'
                f'{_ENGINE_EMOJI[engine]} <b>{sc.ENGINE_LABELS[engine]}</b> — 후보 없음</div>')
    # 앱과 동일한 표시 포맷 함수를 통과 — 이걸 건너뛰면 앱은 50.00, 메일은 공란이 된다
    df = sc._scanner_score_df_format_for_display(snap["score_df"].copy(), engine)
    cutoff = sc.SCANNER_CUTOFF.get(engine, 50.0)
    rows = []
    for _, r in df.head(15).iterrows():
        fs = pd.to_numeric(r.get("Final Score"), errors="coerce")
        fsv = 0.0 if pd.isna(fs) else float(fs)
        if fsv >= sc.WATCHLIST_AUTO_ADD_THRESHOLD:
            badge, color = "🔔 편입", "#4ade80"
        elif fsv >= cutoff:
            badge, color = "관심", "#fbbf24"
        else:
            badge, color = "관망", "#6b7280"
        why = str(r.get("Narrative Why", r.get("Structural Why", "")) or "")[:60]
        rows.append(
            f'<tr>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #232733;font-weight:600;">'
            f'{_esc(r.get("Ticker",""))}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #232733;color:#b8b8b8;">'
            f'{_esc(str(r.get("Name",""))[:22])}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #232733;text-align:right;'
            f'font-weight:700;color:{color};">{fsv:.2f}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #232733;color:{color};'
            f'font-size:12px;">{badge}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #232733;color:#9aa0a6;'
            f'font-size:12px;">{_esc(why)}</td>'
            f'</tr>')
    _uni_n = len(snap.get("universe") or [])
    _cov = float(snap.get("coverage", 0.0)) * 100
    _deg = bool(snap.get("degraded"))
    _cov_html = (f'<span style="color:#ff6b6b;font-weight:700;"> · ⚠️ 커버리지 '
                 f'{_cov:.0f}% — 데이터 수집 실패, 편입 제외</span>') if _deg else \
                f'<span> · 커버리지 {_cov:.0f}%</span>'
    return f"""
    <div style="margin:22px 0 8px;font-size:16px;font-weight:700;">
      {_ENGINE_EMOJI[engine]} {sc.ENGINE_LABELS[engine]}
      <span style="font-size:12px;color:#7a7f87;font-weight:400;">
        · 유니버스 {_uni_n}종목 · 채점 {len(snap["score_df"])}종목 · 컷오프 {cutoff:.0f}
        {_cov_html}</span>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <tr style="color:#7a7f87;font-size:12px;text-align:left;">
        <th style="padding:4px 10px;">티커</th><th style="padding:4px 10px;">종목명</th>
        <th style="padding:4px 10px;text-align:right;">점수</th>
        <th style="padding:4px 10px;">판정</th><th style="padding:4px 10px;">근거</th>
      </tr>
      {''.join(rows)}
    </table>"""


def build_email(result: dict, added: list, skipped: list,
                weekly_dt, briefing_md: str) -> tuple:
    counts = {e: (0 if result.get(e) is None else len(result[e]["score_df"]))
              for e in _ENGINES}
    subject = (f"🚀 [주간 스캐너] 주도주 {counts['leaders']} · 대기주 {counts['emerging']} · "
               f"확산주 {counts['expansion']} · 워치리스트 +{len(added)} · "
               f"{datetime.now(_ET).strftime('%m/%d')}")

    # 주간 브리핑 요약 — 고정 파싱 섹션 앞부분(서술부)만
    head_md = briefing_md.split("## 🏆")[0].strip()
    brief_lines = [l for l in head_md.split("\n") if l.strip()][:14]
    brief_html = "<br>".join(_esc(l) for l in brief_lines)

    if added:
        rows = "".join(
            f'<tr><td style="padding:6px 10px;font-weight:600;">{_esc(c["ticker"])}</td>'
            f'<td style="padding:6px 10px;color:#b8b8b8;">{_esc(c["engine_label"])}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:#4ade80;font-weight:700;">'
            f'{c["score"]:.2f}</td></tr>' for c in added)
        added_html = f"""
        <div style="background:#132018;border-left:3px solid #4ade80;padding:12px 16px;
             border-radius:4px;margin:18px 0;">
          <div style="font-weight:700;margin-bottom:8px;color:#4ade80;">
            🔔 워치리스트 신규 편입 {len(added)}종목
            <span style="font-weight:400;font-size:12px;color:#7a9a85;">
              (Final Score ≥ {sc.WATCHLIST_AUTO_ADD_THRESHOLD:.0f})</span>
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:13px;">{rows}</table>
          <div style="margin-top:10px;font-size:12px;color:#7a9a85;">
            알림 플래그 {sc.WATCHLIST_AUTO_ADD_ALERT_STATES} · 계좌 미지정 ·
            첫 매수/리스크 알림은 월요일 5pm ET
          </div>
        </div>"""
    else:
        added_html = ('<div style="color:#7a7f87;margin:18px 0;padding:12px 16px;'
                      'background:#1a1d24;border-radius:4px;">'
                      f'70점 이상 신규 종목이 없습니다.</div>')

    skip_html = ""
    if skipped:
        names = ", ".join(f'{c["ticker"]}({c["score"]:.1f})' for c in skipped[:20])
        skip_html = (f'<div style="font-size:12px;color:#7a7f87;margin:8px 0 0;">'
                     f'이미 워치리스트에 있어 건너뜀 {len(skipped)}종목 — {_esc(names)}'
                     f'<br>기존 손절가·목표가·알림 설정은 그대로 유지됩니다.</div>')

    _deg_names = [sc.ENGINE_LABELS[e] for e in _ENGINES
                  if result.get(e) is not None and result[e].get("degraded")]
    _deg_names += [f"{sc.ENGINE_LABELS.get(e, e)}(전량 실패)"
                   for e in (result.get("failed") or []) if e in sc.ENGINE_LABELS]
    if "routing" in (result.get("failed") or []):
        _deg_names.append("regime 라우팅(전량 제외)")
    _degraded_banner = ""
    if _deg_names:
        _degraded_banner = (
            '<div style="background:#2a1416;border-left:3px solid #ff6b6b;padding:12px 16px;'
            'border-radius:4px;margin-bottom:16px;">'
            '<div style="font-weight:700;color:#ff6b6b;margin-bottom:6px;">'
            f'⚠️ 데이터 수집 실패 — {", ".join(_deg_names)}</div>'
            '<div style="color:#d0a0a0;font-size:13px;line-height:1.6;">'
            'FMP 응답 부족으로 해당 버킷의 점수를 신뢰할 수 없습니다. '
            '워치리스트 자동 편입에서 제외했습니다. '
            f'({fx.fmp_stats_line()})</div></div>')

    wk = weekly_dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET") if weekly_dt else "-"
    body = f"""<html><body style="margin:0;padding:0;background:#0f1116;">
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#0f1116;color:#e6e6e6;padding:24px;max-width:760px;margin:0 auto;">
      <h1 style="margin:0 0 4px;font-size:20px;">🚀 주간 3버킷 스캐너</h1>
      <div style="color:#7a7f87;font-size:13px;margin-bottom:18px;">
        {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')} ·
        주간 리포트 기준 {wk} · 자동 실행
      </div>

      {_degraded_banner}
      <div style="background:#1a1d24;padding:14px 16px;border-radius:6px;
           border-left:3px solid #60a5fa;">
        <div style="font-weight:700;margin-bottom:8px;">📊 주간 메가 트렌드</div>
        <div style="color:#c8ccd4;font-size:13px;line-height:1.65;">{brief_html}</div>
      </div>

      {_bucket_table('leaders', result.get('leaders'))}
      {_bucket_table('emerging', result.get('emerging'))}
      {_bucket_table('expansion', result.get('expansion'))}

      {added_html}
      {skip_html}

      <div style="margin-top:26px;padding-top:14px;border-top:1px solid #232733;
           font-size:12px;color:#6b7280;line-height:1.7;">
        이 표의 점수는 앱 「1.6 AI 종목 스캐너」에 저장된 스냅샷과 <b>동일한 값</b>입니다
        (재계산 없이 같은 결과를 복원해 표시).<br>
        앱에서 수동 재스캔하면 스냅샷이 덮어써져 이 메일과 달라질 수 있습니다.
      </div>
    </div></body></html>"""
    return subject, body


def send_email(subject: str, html_body: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_TO
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        log("[OK] 스캐너 메일 발송")
        return True
    except Exception as e:
        log(f"[ERROR] 메일 발송 실패: {e}")
        traceback.print_exc()
        return False


# ══════════════════════════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log(f"[START] 주간 3버킷 스캐너: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    et_now = datetime.now(_ET)
    if et_now.weekday() != _TARGET_WEEKDAY and not _FORCE_RUN:
        log(f"[SKIP] 오늘은 {et_now.strftime('%A')} (ET). 토요일 전용. "
            f"(강제: SCANNER_SCAN_FORCE=1)")
        return 0

    # scanner_core 훅 연결 — 자동화는 로그를 stdout 으로
    sc.set_log_hook(lambda lv, m: log(f"[{lv.upper()}] {m}"))
    sc.set_progress_hook(lambda f, t: log(f"  … {t}" if f is None else f"  … {t} ({f*100:.0f}%)"))
    sc.set_fmp_key_provider(lambda: FMP_API_KEY)
    sc.set_genai_client_provider(lambda: genai.Client(api_key=GOOGLE_API_KEY))

    gc = get_gspread_client()
    sh = gc.open(_SPREADSHEET_TITLE)

    # ── 1) 주간 레코드 ────────────────────────────────────────────────────
    log("[STEP 1] 최신 주간 레코드 로드...")
    ws_nar = sh.worksheet(_WS_NARRATIVES)
    analysis, weekly_dt = load_latest_weekly_record(ws_nar)
    if analysis is None:
        log("[ERROR] 주간 레코드(weekly_trend_7d)가 없습니다. run_weekly_report.py 선행 필요.")
        return 1
    log(f"[INFO] 주간 레코드 {weekly_dt.astimezone(_ET).strftime('%Y-%m-%d %H:%M ET')} · "
        f"테마 {len(analysis.get('themes') or [])}개")

    # ── 2) 3버킷 입력 풀 ──────────────────────────────────────────────────
    pools = narrative_core.weekly_scan_pools(analysis, with_stats=True)
    _ps = pools.get("stats") or {}
    log(f"[STEP 2] 풀 분리 — winners {len(pools['winners'])} · "
        f"expanding {len(pools['expanding'])}")
    if _ps:
        log(f"[INFO] 확산주 정제: 원본 {_ps.get('raw', 0)}종목 → {_ps.get('kept', 0)}종목")
        _ov = _ps.get("dropped_overlap") or []
        if _ov:
            log(f"[INFO]   ① winners 교집합 제거 {len(_ov)}종목: "
                f"{', '.join(_ov[:20])}{' …' if len(_ov) > 20 else ''}")
        _rare = _ps.get("dropped_rare") or []
        if _rare:
            log(f"[INFO]   ② 등장 {_ps.get('min_days')}일 미만 제거 {len(_rare)}종목: "
                f"{', '.join(_rare[:20])}{' …' if len(_rare) > 20 else ''}")
        if not _ps.get("freq_available"):
            log("[WARN]   빈도 정보 없는 구 레코드 — 등장일수 필터 미적용")
        _h = _ps.get("freq_hist") or {}
        if _h:
            log("[INFO]   등장 일수 분포: "
                + " · ".join(f"{d}일 {n}종목" for d, n in _h.items()))
    if not pools["winners"] and not pools["expanding"]:
        log("[ERROR] 스캔 가능한 티커가 없습니다.")
        return 1

    # ── 3) 스캔 (앱 버튼과 동일 함수) ─────────────────────────────────────
    log("[STEP 3] 3버킷 스캔 시작...")
    result = sc.run_three_bucket_scan(
        pools["winners"], pools["expanding"], analysis, run_by="auto")

    _degraded = []
    for e in _ENGINES:
        snap = result.get(e)
        if snap is None:
            log(f"[INFO] {sc.ENGINE_LABELS[e]}: 후보 없음")
            continue
        n = len(snap["score_df"])
        cov = snap.get("coverage", 0.0) * 100
        flag = " ⚠️ 커버리지 부족" if snap.get("degraded") else ""
        log(f"[INFO] {sc.ENGINE_LABELS[e]}: {n}/{len(snap.get('universe') or [])}종목 채점 "
            f"(커버리지 {cov:.0f}%){flag}")
        if snap.get("degraded"):
            _degraded.append(sc.ENGINE_LABELS[e])
    for e in _ENGINES:
        snap = result.get(e)
        if snap is None or snap["score_df"].empty:
            continue
        _fs = pd.to_numeric(
            sc._scanner_score_df_format_for_display(snap["score_df"].copy(), e)["Final Score"],
            errors="coerce").dropna()
        if _fs.empty:
            continue
        _q = _fs.quantile([.25, .5, .75, .9]).round(2)
        log(f"[DIST] {sc.ENGINE_LABELS[e]} 점수 — 최소 {_fs.min():.2f} · "
            f"Q1 {_q.iloc[0]} · 중앙 {_q.iloc[1]} · Q3 {_q.iloc[2]} · "
            f"P90 {_q.iloc[3]} · 최대 {_fs.max():.2f}")
        _cuts = " · ".join(f"{c}점 {int((_fs >= c).sum())}종목" for c in (70, 72, 74, 76, 78, 80))
        log(f"[DIST] {sc.ENGINE_LABELS[e]} 커트라인별 — {_cuts}")
    log(f"[INFO] {fx.fmp_stats_line()}")
    if _degraded:
        log(f"[WARN] 커버리지 부족 버킷: {', '.join(_degraded)} — 워치리스트 편입 제외")

    if all(result.get(e) is None for e in _ENGINES):
        log("[ERROR] 3버킷 모두 결과 없음.")
        return 1

    # ── 4·5) 스냅샷 + 히스토리 저장 ───────────────────────────────────────
    log("[STEP 4] Scanner_Last_Result 저장...")
    for e in _ENGINES:
        if result.get(e) is not None:
            ok = save_scanner_snapshot(sh, e, result[e])
            log(f"  {sc.ENGINE_LABELS[e]}: {'✅' if ok else '❌'}")

    log("[STEP 5] Scanner_History 기록...")
    for e in _ENGINES:
        if result.get(e) is not None:
            save_scanner_history(sh, e, result[e])

    # ── 6) 70점 워치리스트 편입 (3버킷 전부 · 상한 없음) ──────────────────
    log(f"[STEP 6] 워치리스트 편입 (≥{sc.WATCHLIST_AUTO_ADD_THRESHOLD:.0f}점)...")
    candidates = []
    for e in _ENGINES:
        if result.get(e) is None:
            continue
        picks = sc.pick_watchlist_candidates(result[e]["score_df"], e, snap=result[e])
        log(f"  {sc.ENGINE_LABELS[e]}: {len(picks)}종목 "
            f"{[(p['ticker'], p['score']) for p in picks]}")
        candidates.extend(picks)
    # 여러 버킷에 동시 등장하면 높은 점수 하나만
    candidates.sort(key=lambda c: c["score"], reverse=True)
    dedup, seen = [], set()
    for c in candidates:
        if c["ticker"] not in seen:
            seen.add(c["ticker"])
            dedup.append(c)

    added, skipped = [], []
    try:
        ws_wl = _get_or_create_ws(sh, _WS_WATCHLIST, _WL_COLS, rows=2000)
        existing = load_existing_watchlist_tickers(ws_wl)
        log(f"[INFO] 기존 워치리스트 {len(existing)}종목")
        added, skipped = add_watchlist_rows(ws_wl, dedup, existing)
        log(f"[OK] 신규 편입 {len(added)} · 기존 보유로 건너뜀 {len(skipped)}")
    except Exception as e:
        log(f"[ERROR] 워치리스트 반영 실패(메일은 계속 발송): {e}")
        traceback.print_exc()

    # ── 7) 메일 ───────────────────────────────────────────────────────────
    log("[STEP 7] 메일 발송...")
    briefing = str(analysis.get("weekly_briefing_markdown") or analysis.get("summary") or "")
    subject, body = build_email(result, added, skipped, weekly_dt, briefing)
    send_email(subject, body)

    log("=" * 60)
    log(f"[DONE] 주도주/대기주/확산주 스캔 완료 · 워치리스트 +{len(added)}")
    if result.get("failed") or _degraded:
        # 메일은 이미 보냈다(사용자가 상황을 봐야 하므로). 다만 워크플로에는 실패로 알린다.
        log(f"[FAIL] 데이터 수집 문제 — 전량실패 {result.get('failed')} · "
            f"커버리지부족 {_degraded}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
