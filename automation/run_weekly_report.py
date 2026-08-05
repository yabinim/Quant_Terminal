"""
run_weekly_report.py
────────────────────
주간 트렌드 리포트(1.5) 자동 생성 — 토요일 5pm ET.

app.py 의 「📊 주간 트렌드 추출(최근 7일)」 버튼과 **완전히 동일한 결과**를 만든다.
프롬프트·병합·트리밍이 전부 narrative_core 의 SSOT 함수이므로 갈라질 수 없다.

파이프라인:
  1) Narratives 시트에서 최근 7일 일일 스냅샷 로드 (주간 레코드 제외)
  2) narrative_core.compact_record_for_timeseries 로 축약
  3) narrative_core.build_timeseries_prompt('weekly', ...) → Gemini
  4) narrative_core.build_weekly_trend_record 로 **7일 병합 themes** 포함 레코드 생성 (2C)
  5) trim_weekly_analysis_for_cell 로 셀 한도 방어 후 Narratives 시트에 저장

⚠️ 이 레코드의 themes 가 run_scanner_scan.py 의 3버킷 입력 전부를 결정한다.
   실패하면 스캔 단계는 실행되지 않아야 하므로 실패 시 exit code 1 을 반환한다.

실행 환경: GitHub Actions (Ubuntu), Python 3.11+
"""

import os
import sys
import json
import smtplib
import traceback
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytz
import gspread
from google.oauth2.service_account import Credentials
from google import genai

# ── repo root → SSOT 모듈 import ─────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import narrative_core
import gemini_core

# ── 환경변수 ─────────────────────────────────────────────────────────────────
GOOGLE_API_KEY     = os.environ["GOOGLE_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]
GSPREAD_KEY_JSON   = os.environ["GSPREAD_KEY"]

_gcp_info = json.loads(GSPREAD_KEY_JSON)

# ── 상수 ─────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")
_SPREADSHEET_TITLE    = "Quant_DB"
_NARRATIVES_WORKSHEET = "Narratives"
_ADMIN_USER_ID        = "yab"

_LOOKBACK_DAYS = 7
_MAX_SNAPSHOTS = 40       # app.py 주간 버튼과 동일한 상한

# 토요일 전용. 주말 잡이 토·일 모두 돌기 때문에 요일 가드가 필요하다.
_TARGET_WEEKDAY = 5       # 0=월 … 5=토
_FORCE_RUN = str(os.environ.get("WEEKLY_REPORT_FORCE", "")).strip() in ("1", "true", "TRUE")


def log(msg):
    print(msg, flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# Sheets
# ══════════════════════════════════════════════════════════════════════════════
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(_gcp_info, scopes=scopes)
    return gspread.authorize(creds)


def _safe_append_rows(ws, rows, value_input_option: str = "USER_ENTERED") -> None:
    """gspread append_row 의 '계단식 드리프트' 회피 — 항상 A열 기준 마지막 데이터 다음에 기록."""
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


def _parse_saved_at(value):
    """app.py `_narrative_parse_saved_at_utc` 와 동일한 관용적 파싱."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def load_recent_narrative_records(ws, days: int = _LOOKBACK_DAYS) -> list:
    """최근 N일 일일 내러티브 레코드를 **시간 오름차순**으로 반환.

    ⚠️ merge_weekly_themes 는 뒤에 오는 레코드를 '최신'으로 간주해 driver/linkage 를
       채택하므로 반드시 오름차순이어야 한다.
    """
    values = ws.get_all_values() or []
    if len(values) <= 1:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for row in values[1:]:
        if not row or len(row) < 5:
            continue
        content = str(row[4] or "").strip()
        if not content:
            continue
        try:
            env = json.loads(content)
        except Exception:
            continue
        if not isinstance(env, dict) or not isinstance(env.get("analysis"), dict):
            continue
        a = env["analysis"]
        if str(a.get("source") or "") == narrative_core.NARRATIVE_SOURCE_WEEKLY_7D:
            continue  # 주간 레코드는 입력에서 제외 (재귀 방지)
        dt = _parse_saved_at(env.get("saved_at"))
        if dt is None and len(row) > 1:
            dt = _parse_saved_at(row[1])
        if dt is None or dt < cutoff:
            continue
        env["_dt"] = dt
        out.append(env)
    out.sort(key=lambda r: r["_dt"])
    for r in out:
        r.pop("_dt", None)
    return out[-_MAX_SNAPSHOTS:]


def weekly_record_exists_this_week(ws) -> bool:
    """이번 ISO 주에 이미 주간 레코드가 저장됐는지 (중복 실행 방지)."""
    values = ws.get_all_values() or []
    now_iso = datetime.now(_ET).isocalendar()
    for row in reversed(values[1:] if len(values) > 1 else []):
        if not row or len(row) < 5:
            continue
        if str(row[2] or "").strip() != narrative_core.NARRATIVE_SOURCE_WEEKLY_7D:
            continue
        try:
            env = json.loads(str(row[4] or ""))
        except Exception:
            continue
        dt = _parse_saved_at(env.get("saved_at")) or _parse_saved_at(row[1])
        if dt is None:
            continue
        c = dt.astimezone(_ET).isocalendar()
        if (c[0], c[1]) == (now_iso[0], now_iso[1]):
            return True
    return False


def record_to_sheet_row(record: dict, owner_id: str) -> list:
    """app.py `_narrative_record_to_sheet_row` 와 동일한 7열 변환.

    ⚠️ 하드컷 전에 narrative_core.trim_weekly_analysis_for_cell 로 줄여야 한다.
       7일 병합 themes 는 무압축 시 20만 자를 넘겨 JSON 이 깨지고,
       그러면 앱이 레코드를 파싱하지 못해 **주간 리포트가 통째로 사라진다.**
    """
    rec = dict(record)
    analysis = dict(rec.get("analysis") or {})
    analysis = narrative_core.trim_weekly_analysis_for_cell(analysis)
    rec["analysis"] = analysis

    dt = _parse_saved_at(rec.get("saved_at")) or datetime.now(timezone.utc)
    date_kst = dt.astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S")
    category = str(analysis.get("source") or narrative_core.NARRATIVE_SOURCE_WEEKLY_7D)

    themes = analysis.get("themes") or []
    title = "📊 주간 트렌드 (최근 7일)"
    if themes and isinstance(themes[0], dict):
        t0 = str(themes[0].get("title", "") or "").strip()
        if t0:
            title = f"📊 주간 트렌드 — {t0}"
    if len(title) > 500:
        title = title[:497] + "..."

    content = json.dumps(rec, ensure_ascii=False)
    if len(content) > narrative_core.SHEET_CELL_BUDGET:
        # trim 이 한 번 더 필요한 극단 케이스 — 그래도 넘으면 요약을 잘라낸다.
        analysis["summary"] = str(analysis.get("summary") or "")[:3000]
        analysis["weekly_briefing_markdown"] = str(
            analysis.get("weekly_briefing_markdown") or "")[:10000]
        rec["analysis"] = analysis
        content = json.dumps(rec, ensure_ascii=False)

    pools = narrative_core.weekly_scan_pools(analysis)
    w_csv = ",".join(pools["winners"])
    e_csv = ",".join(pools["expanding"])
    return [str(owner_id).strip(), date_kst, category, title, content, w_csv, e_csv]


# ══════════════════════════════════════════════════════════════════════════════
# 메일 (실패 알림 전용 — 정상 결과는 run_scanner_scan.py 가 하나의 메일로 보낸다)
# ══════════════════════════════════════════════════════════════════════════════
def send_failure_email(reason: str, detail: str = "") -> None:
    subject = f"⚠️ [주간 리포트 실패] {datetime.now(_ET).strftime('%m/%d')} — 스캔 건너뜀"
    html = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      background:#0f1116;color:#e6e6e6;padding:24px;">
      <h2 style="color:#ff6b6b;margin:0 0 12px;">주간 트렌드 리포트 생성 실패</h2>
      <p style="margin:0 0 16px;color:#b8b8b8;">주간 레코드가 없어 3버킷 스캐너도 실행하지 않았습니다.</p>
      <div style="background:#1a1d24;border-left:3px solid #ff6b6b;padding:12px 16px;border-radius:4px;">
        <div style="font-weight:600;margin-bottom:6px;">사유</div>
        <div style="color:#d0d0d0;">{reason}</div>
      </div>
      {f'<pre style="background:#14171d;padding:12px;border-radius:4px;overflow-x:auto;font-size:12px;color:#9aa0a6;margin-top:14px;">{detail[:2000]}</pre>' if detail else ''}
      <p style="margin-top:20px;font-size:13px;color:#7a7f87;">
        수동 재실행: GitHub Actions → market_5pm_weekend → Run workflow (WEEKLY_REPORT_FORCE=1)
      </p></body></html>"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_TO
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        log("[OK] 실패 알림 메일 발송")
    except Exception as e:
        log(f"[WARN] 실패 알림 메일 발송 실패: {e}")


# ══════════════════════════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log(f"[START] 주간 트렌드 리포트: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    et_now = datetime.now(_ET)
    if et_now.weekday() != _TARGET_WEEKDAY and not _FORCE_RUN:
        log(f"[SKIP] 오늘은 {et_now.strftime('%A')} (ET). 토요일에만 실행합니다. "
            f"(강제: WEEKLY_REPORT_FORCE=1)")
        return 0

    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        ws = sh.worksheet(_NARRATIVES_WORKSHEET)
    except Exception as e:
        log(f"[ERROR] 시트 열기 실패: {e}")
        traceback.print_exc()
        send_failure_email("Google Sheets 접근 실패", traceback.format_exc())
        return 1

    if weekly_record_exists_this_week(ws) and not _FORCE_RUN:
        log("[SKIP] 이번 주 주간 레코드가 이미 존재합니다. (강제: WEEKLY_REPORT_FORCE=1)")
        return 0

    # ── 1) 최근 7일 스냅샷 ────────────────────────────────────────────────
    log("[STEP 1] 최근 7일 내러티브 스냅샷 로드...")
    try:
        recs = load_recent_narrative_records(ws, days=_LOOKBACK_DAYS)
    except Exception as e:
        log(f"[ERROR] 스냅샷 로드 실패: {e}")
        traceback.print_exc()
        send_failure_email("Narratives 시트 로드 실패", traceback.format_exc())
        return 1

    log(f"[INFO] 스냅샷 {len(recs)}건 (최대 {_MAX_SNAPSHOTS})")
    if len(recs) < 2:
        msg = f"최근 {_LOOKBACK_DAYS}일 스냅샷이 {len(recs)}건뿐이라 주간 집계가 불가능합니다."
        log(f"[ERROR] {msg}")
        send_failure_email(msg)
        return 1

    # ── 2) 축약 → 3) 프롬프트 → Gemini ────────────────────────────────────
    log("[STEP 2] 시계열 페이로드 축약...")
    payload_records = [narrative_core.compact_record_for_timeseries(r) for r in recs]
    payload_records = [p for p in payload_records if p]
    payload = {"kind": "weekly", "window_days": _LOOKBACK_DAYS,
               "count": len(payload_records), "records": payload_records}

    log("[STEP 3] Gemini 주간 브리핑 생성...")
    try:
        client = genai.Client(api_key=GOOGLE_API_KEY)
        prompt = narrative_core.build_timeseries_prompt("weekly", payload, "ko")
        briefing = gemini_core.generate_text(
            client, prompt,
            temperature=0.3,
            max_output_tokens=8192,
            thinking_budget=0,
            validate=lambda t: len(str(t).strip()) > 200,
            log=log,
            label="주간 브리핑",
        )
    except Exception as e:
        log(f"[ERROR] Gemini 생성 실패: {e}")
        traceback.print_exc()
        send_failure_email("Gemini 주간 브리핑 생성 실패", traceback.format_exc())
        return 1

    log(f"[INFO] 브리핑 {len(briefing):,}자 생성")
    has_w = "## 🏆 Weekly Winners" in briefing
    has_x = "## 🚀 Weekly Expanding To" in briefing
    log(f"[INFO] 고정 헤더 — Winners:{'✅' if has_w else '❌'} Expanding:{'✅' if has_x else '❌'}")

    # ── 4) 2C 레코드 생성 (7일 병합 themes 포함) ──────────────────────────
    log("[STEP 4] 7일 themes 병합 (2C)...")
    record = narrative_core.build_weekly_trend_record(briefing, "ko", recs)
    analysis = record["analysis"]
    merged = analysis.get("themes") or []
    pools = narrative_core.weekly_scan_pools(analysis)
    log(f"[INFO] 병합 테마 {len(merged)}개 "
        f"(상한 {narrative_core.WEEKLY_THEME_MERGE_LIMIT})")
    log(f"[INFO] 스캔 풀 — winners {len(pools['winners'])}종목 · "
        f"expanding {len(pools['expanding'])}종목")
    if merged:
        top = ", ".join(f"{t.get('title','')}({t.get('occurrences',0)}회)" for t in merged[:5])
        log(f"[INFO] 상위 테마: {top}")

    if not pools["winners"] and not pools["expanding"]:
        msg = "병합 결과 스캔 가능한 티커가 하나도 없습니다."
        log(f"[ERROR] {msg}")
        send_failure_email(msg)
        return 1

    # ── 5) 저장 ────────────────────────────────────────────────────────────
    log("[STEP 5] Narratives 시트 저장...")
    try:
        row = record_to_sheet_row(record, _ADMIN_USER_ID)
        log(f"[INFO] Content 셀 {len(row[4]):,}자 (예산 {narrative_core.SHEET_CELL_BUDGET:,})")
        json.loads(row[4])  # 저장 전 JSON 무결성 확인 — 깨진 셀은 앱이 못 읽는다
        _safe_append_rows(ws, row, value_input_option="USER_ENTERED")
    except json.JSONDecodeError as e:
        log(f"[ERROR] Content JSON 무결성 실패(저장 중단): {e}")
        send_failure_email("주간 레코드 JSON 무결성 실패", traceback.format_exc())
        return 1
    except Exception as e:
        log(f"[ERROR] 시트 저장 실패: {e}")
        traceback.print_exc()
        send_failure_email("Narratives 시트 저장 실패", traceback.format_exc())
        return 1

    log(f"[OK] 주간 리포트 저장 완료 — 테마 {len(merged)}개 · "
        f"winners {len(pools['winners'])} · expanding {len(pools['expanding'])}")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        try:
            send_failure_email("예상치 못한 오류", traceback.format_exc())
        except Exception:
            pass
        sys.exit(1)
