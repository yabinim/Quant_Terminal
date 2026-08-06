"""
run_drg_verify.py
─────────────────
GitHub Actions 자동 실행: DRG 예측 결과 검증 + Google Sheets 업데이트 + 이메일 발송
실행 시간: 평일 5PM ET (장 마감 후)
"""

import os
import sys
import json
import re
import smtplib
import traceback
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import pytz
import requests
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types as genai_types

# ── repo root 를 sys.path 에 추가 → 공유 SSOT 모듈(gemini_core) import ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gemini_core
import users_core as uc  # Users 시트/수신자 SSOT

# ── 환경변수 ──────────────────────────────────────────────────────────────────
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_TO           = os.environ["GMAIL_TO"]
GSPREAD_KEY_JSON   = os.environ["GSPREAD_KEY"]
# AI 리뷰 생성용(검증 일관성). 워크플로에 시크릿이 없으면 리뷰만 생략(기존 동작 유지).
GOOGLE_API_KEY     = os.environ.get("GOOGLE_API_KEY", "")
_gcp_info          = json.loads(GSPREAD_KEY_JSON)

# ── 상수 ───────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")
_SPREADSHEET_TITLE         = "Quant_DB"
_DRG_PREDICTIONS_WORKSHEET = "DRG_Predictions"
_DRG_SHEET_COLS = [
    "user_id", "pred_date", "direction", "sector_filter", "benchmark_etf",
    "spy_close_at_pred", "full_text", "actual_direction", "actual_return_pct",
    "is_correct", "review_comment",
    "revised_direction", "revised_full_text", "revision_reason", "revised_at", "is_revised"
]
_ADMIN_USER_ID = "yab"

_NYSE_HOLIDAYS_2025 = {
    "2025-01-01","2025-01-20","2025-02-17","2025-04-18",
    "2025-05-26","2025-06-19","2025-07-04","2025-09-01",
    "2025-11-27","2025-12-25",
}
_NYSE_HOLIDAYS_2026 = {
    "2026-01-01","2026-01-19","2026-02-16","2026-04-03",
    "2026-05-25","2026-06-19","2026-07-03","2026-09-07",
    "2026-11-26","2026-12-25",
}
_NYSE_HOLIDAYS = _NYSE_HOLIDAYS_2025 | _NYSE_HOLIDAYS_2026


def is_market_open_today() -> bool:
    et_now = datetime.now(_ET)
    return et_now.weekday() < 5 and et_now.strftime("%Y-%m-%d") not in _NYSE_HOLIDAYS


# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_gspread_client():
    creds = Credentials.from_service_account_info(_gcp_info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)


def load_drg_predictions() -> pd.DataFrame:
    """DRG_Predictions 시트 전체 로드."""
    empty = pd.DataFrame(columns=_DRG_SHEET_COLS)
    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        ws = sh.worksheet(_DRG_PREDICTIONS_WORKSHEET)
        rows = ws.get_all_values()
        if len(rows) < 2:
            return empty
        header = [str(c).strip() for c in rows[0]]
        df = pd.DataFrame(rows[1:], columns=header)
        for col in _DRG_SHEET_COLS:
            if col not in df.columns:
                df[col] = ""
        return df[_DRG_SHEET_COLS].copy()
    except Exception as e:
        print(f"[ERROR] Sheets 로드 실패: {e}")
        return empty


def update_drg_result_in_sheet(pred_date: str, actual_dir: str,
                                 actual_ret: float, is_correct: str,
                                 review_comment: str = "") -> bool:
    """pred_date 행의 검증 결과 업데이트 (review_comment 포함)."""
    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        ws = sh.worksheet(_DRG_PREDICTIONS_WORKSHEET)
        rows = ws.get_all_values()
        if len(rows) < 2:
            return False

        header = [str(c).strip() for c in rows[0]]
        try:
            pred_date_col = header.index("pred_date") + 1
            user_id_col   = header.index("user_id") + 1
            actual_dir_col = header.index("actual_direction") + 1
            actual_ret_col = header.index("actual_return_pct") + 1
            is_correct_col = header.index("is_correct") + 1
        except ValueError as e:
            print(f"[ERROR] 컬럼 찾기 실패: {e}")
            return False
        review_col = (header.index("review_comment") + 1) if "review_comment" in header else None

        updated = False
        for row_idx, row in enumerate(rows[1:], start=2):
            row_date    = str(row[pred_date_col - 1]).strip() if len(row) >= pred_date_col else ""
            row_user_id = str(row[user_id_col - 1]).strip() if len(row) >= user_id_col else ""
            row_correct = str(row[is_correct_col - 1]).strip() if len(row) >= is_correct_col else ""

            if row_date == pred_date and row_user_id == _ADMIN_USER_ID and not row_correct:
                ret_str = f"{actual_ret:.2f}" if not np.isnan(actual_ret) else ""
                ws.update_cell(row_idx, actual_dir_col, actual_dir)
                ws.update_cell(row_idx, actual_ret_col, ret_str)
                ws.update_cell(row_idx, is_correct_col, is_correct)
                if review_col and review_comment:
                    ws.update_cell(row_idx, review_col, review_comment)
                print(f"[OK] 검증 업데이트: {pred_date} → {actual_dir} | {is_correct}"
                      f"{' | 리뷰✓' if (review_col and review_comment) else ''}")
                updated = True

        return updated
    except Exception as e:
        print(f"[ERROR] Sheets 업데이트 실패: {e}")
        traceback.print_exc()
        return False


# ── AI 리뷰 생성 (검증 일관성) ─────────────────────────────────────────────────
def generate_review_comment(direction: str, actual_dir: str, actual_ret: float,
                            is_correct: str, full_text: str) -> str:
    """app.py의 '결과 검증' AI 리뷰와 동일한 review_comment 생성.
    ⚠️ 프롬프트는 app.py의 결과 검증 리뷰와 반드시 동일하게 유지할 것 (검증 일관성).
       full_text 앞부분에 발표일 가드레일 배너가 있으면 모델이 자연히
       '발표 전 예측이었음'을 근거로 빗나간 이유를 귀속한다."""
    if not GOOGLE_API_KEY:
        return ""
    try:
        ret_str = f"{actual_ret:+.2f}%" if not np.isnan(actual_ret) else "N/A"
        prompt = (
            f"예측방향: {direction} / 실제: {actual_dir} ({ret_str}) / {is_correct}\n"
            f"예측 전문(앞 500자): {str(full_text)[:500]}\n"
            "3~4문장 한국어 리뷰: 맞았다면 어떤 근거가 적중했는지, 틀렸다면 무엇을 놓쳤는지, 다음 예측 시 참고할 인사이트."
        )
        client = genai.Client(api_key=GOOGLE_API_KEY)
        # 탄력적 생성(SSOT): 재시도+지터+폴백, thinking_budget=0. 리뷰는 비핵심 →
        # 최종 실패해도 "" 반환(검증·스코어링은 계속 진행).
        text = gemini_core.generate_text(
            client, prompt,
            temperature=0.3, max_output_tokens=4096, thinking_budget=0,
            primary_attempts=5, fallback_attempts=3,
            label="AI리뷰",
        )
        return text
    except Exception as e:
        print(f"[WARN] AI 리뷰 생성 실패: {e}")
        return ""


# ── 예측 검증 로직 ────────────────────────────────────────────────────────────
def verify_prediction(pred_row: pd.Series) -> tuple[str, float, str]:
    """예측일 당일 실제 결과 계산. app.py의 verify_drg_prediction과 동일 로직."""
    try:
        bench_etf    = str(pred_row.get("benchmark_etf", "SPY") or "SPY").strip().upper()
        pred_date_str = str(pred_row.get("pred_date", "")).strip()
        pred_date    = pd.to_datetime(pred_date_str, errors="coerce")
        if pd.isna(pred_date):
            return "", np.nan, ""

        start_date = pred_date - pd.Timedelta(days=14)
        end_date   = pred_date + pd.Timedelta(days=2)

        FMP_KEY_V = os.environ.get("FMP_API_KEY", "")
        fmp_r = requests.get(
            f"https://financialmodelingprep.com/stable/historical-price-eod/full?symbol={bench_etf}&limit=20&apikey={FMP_KEY_V}",
            timeout=8
        )
        fmp_data = fmp_r.json()
        fmp_rows = fmp_data.get("historical", fmp_data) if isinstance(fmp_data, dict) else fmp_data
        if not isinstance(fmp_rows, list) or not fmp_rows:
            return "", np.nan, ""
        hist_df = pd.DataFrame(fmp_rows)
        hist_df["date"] = pd.to_datetime(hist_df["date"])
        hist_df = hist_df.set_index("date").sort_index()
        hist_df["Close"] = pd.to_numeric(hist_df["close"], errors="coerce")
        hist = hist_df[["Close"]]
        if hist is None or hist.empty:
            return "", np.nan, ""

        hist.index = pd.to_datetime(hist.index).tz_localize(None)
        hist_dates = hist.index.normalize()
        pred_ts    = pd.Timestamp(pred_date.date())

        hist_on_pred = hist[hist_dates == pred_ts]
        hist_before  = hist[hist_dates < pred_ts]

        if hist_on_pred.empty or hist_before.empty:
            return "", np.nan, ""

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
        # ⚠️ app.py의 verify_drg_prediction과 반드시 동일하게 유지할 것.
        if ret_pct >= 0.7:
            actual_dir = "상승"
        elif ret_pct <= -0.7:
            actual_dir = "하락"
        else:
            actual_dir = "중립"

        # 발표 후 갱신본이 있으면 갱신 방향으로 채점(일관성 — app.py와 동일)
        _is_rev = str(pred_row.get("is_revised", "")).strip().upper() == "TRUE"
        _rev_dir = str(pred_row.get("revised_direction", "")).strip()
        pred_dir = _rev_dir if (_is_rev and _rev_dir) else str(pred_row.get("direction", "")).strip()
        pred_dir_norm = "상승" if "상승" in pred_dir else ("하락" if "하락" in pred_dir else "중립")
        is_correct = "✅ 적중" if pred_dir_norm == actual_dir else "❌ 빗나감"

        return actual_dir, ret_pct, is_correct
    except Exception as e:
        print(f"[WARN] 검증 계산 실패: {e}")
        return "", np.nan, ""


# ── HTML 이메일 ───────────────────────────────────────────────────────────────
def build_verify_email_html(results: list[dict]) -> str:
    now_et  = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
    now_kst = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")

    if not results:
        rows_html = "<tr><td colspan='5' style='text-align:center;color:#64748b;padding:20px;'>검증할 예측 없음</td></tr>"
    else:
        rows_html = ""
        for r in results:
            correct = r.get("is_correct", "")
            correct_color = "#16a34a" if "적중" in correct else ("#dc2626" if "빗나감" in correct else "#6b7280")
            ret_val = r.get("actual_return_pct", np.nan)
            ret_str = f"{ret_val:+.2f}%" if not np.isnan(ret_val) else "N/A"
            ret_color = "#16a34a" if (not np.isnan(ret_val) and ret_val >= 0) else "#dc2626"
            rows_html += f"""
            <tr>
              <td style="padding:10px;color:#94a3b8;font-size:13px;">{r.get('pred_date','')}</td>
              <td style="padding:10px;font-size:13px;">{r.get('direction','')}</td>
              <td style="padding:10px;font-size:13px;">{r.get('actual_direction','')}</td>
              <td style="padding:10px;font-weight:700;color:{ret_color};font-size:13px;">{ret_str}</td>
              <td style="padding:10px;font-weight:700;color:{correct_color};font-size:13px;">{correct}</td>
            </tr>"""

    # 적중률 계산
    total   = len([r for r in results if r.get("is_correct")])
    correct = len([r for r in results if "적중" in str(r.get("is_correct", ""))])
    accuracy_str = f"{correct/total*100:.0f}% ({correct}/{total})" if total > 0 else "N/A"
    acc_color = "#16a34a" if total > 0 and correct/total >= 0.6 else "#d97706"

    # 최근 30일 누적 적중률
    all_df = load_drg_predictions()
    cutoff = (datetime.now(_ET) - timedelta(days=30)).strftime("%Y-%m-%d")
    recent = all_df[
        (all_df["user_id"] == _ADMIN_USER_ID) &
        (all_df["pred_date"] >= cutoff) &
        (all_df["is_correct"] != "")
    ]
    r_total   = len(recent)
    r_correct = len(recent[recent["is_correct"].str.contains("적중", na=False)])
    r_acc_str = f"{r_correct/r_total*100:.0f}% ({r_correct}/{r_total})" if r_total > 0 else "N/A"

    # AI 리뷰 블록 (검증 일관성 — 앱 '예측 전문 보기'의 AI 리뷰와 동일 내용)
    reviews_html = ""
    for r in results:
        rev = str(r.get("review_comment", "") or "").strip()
        if not rev:
            continue
        rc = r.get("is_correct", "")
        bar = "#16a34a" if "적중" in rc else ("#dc2626" if "빗나감" in rc else "#6b7280")
        rev_html = rev.replace("\n", "<br>")
        reviews_html += f"""
        <div style="background:#1e293b;border-left:3px solid {bar};border-radius:8px;padding:14px;margin-bottom:10px;">
          <div style="font-size:12px;color:#64748b;margin-bottom:6px;">🤖 AI 리뷰 · {r.get('pred_date','')} · {r.get('direction','')} → {r.get('actual_direction','')} {rc}</div>
          <div style="font-size:13px;color:#cbd5e1;line-height:1.6;">{rev_html}</div>
        </div>"""
    reviews_section = (
        f'<div style="margin-bottom:16px;">{reviews_html}</div>' if reviews_html else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;">

  <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #334155;">
    <div style="font-size:22px;font-weight:800;color:#60a5fa;">✅ Quant Terminal</div>
    <div style="font-size:14px;color:#94a3b8;margin-top:4px;">DRG 예측 결과 검증 리포트 · After-Close 5PM</div>
    <div style="font-size:13px;color:#64748b;margin-top:6px;">{now_et} &nbsp;|&nbsp; {now_kst}</div>
  </div>

  <!-- 적중률 요약 -->
  <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;">
    <div style="flex:1;background:#1e293b;border-radius:10px;padding:16px;text-align:center;">
      <div style="font-size:11px;color:#64748b;text-transform:uppercase;">오늘 검증</div>
      <div style="font-size:22px;font-weight:800;color:{acc_color};margin-top:6px;">{accuracy_str}</div>
    </div>
    <div style="flex:1;background:#1e293b;border-radius:10px;padding:16px;text-align:center;">
      <div style="font-size:11px;color:#64748b;text-transform:uppercase;">최근 30일 누적</div>
      <div style="font-size:22px;font-weight:800;color:#60a5fa;margin-top:6px;">{r_acc_str}</div>
    </div>
  </div>

  <!-- 검증 테이블 -->
  <div style="background:#1e293b;border-radius:10px;overflow:hidden;margin-bottom:16px;">
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#0f172a;">
          <th style="padding:10px;text-align:left;font-size:12px;color:#64748b;font-weight:600;">예측일</th>
          <th style="padding:10px;text-align:left;font-size:12px;color:#64748b;font-weight:600;">예측 방향</th>
          <th style="padding:10px;text-align:left;font-size:12px;color:#64748b;font-weight:600;">실제 방향</th>
          <th style="padding:10px;text-align:left;font-size:12px;color:#64748b;font-weight:600;">실제 수익률</th>
          <th style="padding:10px;text-align:left;font-size:12px;color:#64748b;font-weight:600;">결과</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  {reviews_section}

  <div style="text-align:center;padding:16px;">
    <a href="https://quantdb.streamlit.app"
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
    """단일 수신자 발송."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = to_addr
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, to_addr, msg.as_string())
        print(f"[OK] 이메일 발송 완료 → {to_addr}")
        return True
    except Exception as e:
        print(f"[ERROR] 이메일 발송 실패({to_addr}): {e}")
        return False


def _global_recipients() -> list[tuple[str, str]]:
    """Users 시트에서 시장 브리핑(Alert_Global) 수신자 조회. 실패 시 빈 목록."""
    try:
        gc = get_gspread_client()
        ws = gc.open(_SPREADSHEET_TITLE).worksheet("Users")
        return uc.get_recipients(ws, "global")
    except Exception as e:
        print(f"[WARN] Users 수신자 조회 실패 — 관리자 메일만 발송: {e}")
        return []


def _kind_recipients(kind: str) -> list:
    """Users 시트 수신자 조회 + v3 헤더 방어. 실패 시 관리자 폴백."""
    try:
        gc = get_gspread_client()
        ws = gc.open(_SPREADSHEET_TITLE).worksheet("Users")
        uc.ensure_users_header_v3(ws)
        return uc.get_recipients(ws, kind, admin_fallback_email=GMAIL_TO)
    except Exception as e:
        print(f"[WARN] Users 수신자 조회 실패 — 관리자 폴백: {e}")
        return [(uc.ADMIN_CONTENT_OWNER_ID, GMAIL_TO)]


def _broadcast(subject: str, html_body: str, kind: str, send_one) -> bool:
    """토글 ON 인 승인 사용자 전원에게 동일 본문 발송.

    ⚠️ 관리자도 예외가 아니다. 예전에는 GMAIL_TO 로 무조건 먼저 보내서 관리자가
       토글로 알림을 끌 수 없었다. 이제 yab 행의 토글을 그대로 따른다.
    반환: 관리자 발송 성공 여부(관리자가 대상일 때). 게스트 실패는 격리.
    """
    rcpts = _kind_recipients(kind)
    if not rcpts:
        print(f"[INFO] {kind} 수신자가 없습니다 — 발송 생략")
        return True
    admin_u = str(uc.ADMIN_CONTENT_OWNER_ID).strip().upper()
    ok_admin, admin_targeted = True, False
    for _uid, _email in rcpts:
        is_admin = str(_uid).strip().upper() == admin_u
        try:
            ok = send_one(subject, html_body, str(_email).strip())
        except Exception as e:
            print(f"[WARN] {_uid} 발송 실패(다음 수신자 진행): {e}")
            ok = False
        if is_admin:
            admin_targeted, ok_admin = True, ok
        elif not ok:
            print(f"[WARN] 게스트 {_uid} 발송 실패 — 계속 진행")
    return ok_admin if admin_targeted else True


def send_email(subject: str, html_body: str) -> bool:
    """DRG 브리핑 브로드캐스트 — Alert_DRG ON 인 승인 사용자 전원."""
    return _broadcast(subject, html_body, "drg", _send_email_one)

def main():
    print("=" * 60)
    print(f"[START] DRG 검증 시작: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    if not is_market_open_today():
        print("[SKIP] 오늘은 NYSE 휴장일. DRG 검증 스킵.")
        sys.exit(0)

    print("[STEP 1] DRG 예측 기록 로드 중...")
    df = load_drg_predictions()
    if df.empty:
        print("[WARN] 검증할 예측 데이터 없음.")
        sys.exit(0)

    # 미검증 행만 필터 (admin 유저, is_correct 비어있음)
    today_str = datetime.now(_ET).strftime("%Y-%m-%d")
    unverified = df[
        (df["user_id"] == _ADMIN_USER_ID) &
        (df["is_correct"] == "") &
        (df["pred_date"] <= today_str)
    ]
    print(f"[INFO] 미검증 예측: {len(unverified)}건")

    verified_results = []
    for _, row in unverified.iterrows():
        pred_date = str(row.get("pred_date", "")).strip()
        if not pred_date:
            continue

        print(f"[STEP 2] 검증 중: {pred_date}")
        actual_dir, actual_ret, is_correct = verify_prediction(row)

        if not actual_dir:
            print(f"[WARN] {pred_date} 검증 불가 (장 미마감 또는 데이터 없음)")
            continue

        # 채점·리뷰는 발표 후 갱신본이 있으면 그 기준으로(일관성)
        _is_rev = str(row.get("is_revised", "")).strip().upper() == "TRUE"
        _rev_dir = str(row.get("revised_direction", "")).strip()
        _revised = _is_rev and bool(_rev_dir)
        eff_dir = _rev_dir if _revised else str(row.get("direction", ""))
        eff_text = str(row.get("revised_full_text", "")).strip() if _is_rev else ""
        if not eff_text:
            eff_text = str(row.get("full_text", ""))

        print(f"[INFO] {pred_date}: {eff_dir}{' (갱신)' if _revised else ''} → 실제 {actual_dir} | {is_correct} ({actual_ret:+.2f}%)")
        review = generate_review_comment(eff_dir, actual_dir, actual_ret, is_correct, eff_text)
        update_drg_result_in_sheet(pred_date, actual_dir, actual_ret, is_correct, review)

        verified_results.append({
            "pred_date":         pred_date,
            "direction":         eff_dir + (" 🔄" if _revised else ""),
            "actual_direction":  actual_dir,
            "actual_return_pct": actual_ret,
            "is_correct":        is_correct,
            "review_comment":    review,
        })

    print(f"[STEP 3] 이메일 발송 중... (검증 완료 {len(verified_results)}건)")
    today_label = datetime.now(_ET).strftime("%m/%d")
    subject = f"✅ [DRG 검증] {today_label} · {len(verified_results)}건 검증 완료"
    html_body = build_verify_email_html(verified_results)
    send_email(subject, html_body)

    print(f"[DONE] 완료: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
