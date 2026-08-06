# -*- coding: utf-8 -*-
"""
users_core.py — Users 시트 SSOT 모듈 (app.py + automation 공용)
────────────────────────────────────────────────────────────────
스키마 v3 (12열): ID, Password, Reason, Source, Status, Email, Alert_Radar,
                  Alert_Global, Alert_DRG, Alert_Weekly, Alert_HiddenAlpha, Auto_Watchlist

- Password: "pbkdf2$<iterations>$<salt_hex>$<hash_hex>" 형식 해시.
  접두어(pbkdf2$)가 없으면 레거시 평문으로 간주 → 로그인 성공 시 호출측이 해시로 승격.
- Alert_Radar  : 매매 레이더(개인 메일) 수신 여부 ("Y"/"N")
- Alert_Global : 시장 내러티브 브리핑 수신 여부 ("Y"/"N")   ← v3에서 DRG 분리
- Alert_DRG    : DRG 예측·검증 메일 수신 여부 ("Y"/"N")
- Alert_Weekly : 주간 3버킷 스캐너 메일 수신 여부 ("Y"/"N")
- Alert_HiddenAlpha: Hidden Alpha 주간 ETF 로테이션 메일 수신 여부 ("Y"/"N")
- Auto_Watchlist: 주간 스캐너 결과를 **본인 워치리스트에 자동 편입**할지 ("Y"/"N")
                  Alert_Weekly 와 독립 — 메일 없이 편입만, 편입 없이 메일만 모두 가능.

⚠️ 관리자(yab)도 예외가 아니다. 모든 토글은 관리자에게도 동일하게 적용되어,
   코드 수정 없이 특정 알림만 일시 중지할 수 있어야 한다.
   단 yab 행 자체가 없거나 Email 이 비어 있으면 GMAIL_TO 로 폴백한다
   (시트 사고로 관리자가 알림을 통째로 잃는 것을 방지). 명시적 "N" 은 폴백하지 않는다.

설계 원칙:
- gspread Worksheet 객체를 인자로 받아 동작한다. 클라이언트 생성은 호출측 책임
  (app.py = st.secrets 서비스 계정, automation = GSPREAD_KEY 환경변수).
- 이 모듈은 streamlit / gspread 를 import 하지 않는다 (양쪽 환경 공용).

⚠️ lockstep: 이 파일 변경 시 app.py + automation 6개
   (run_watchlist_alerts / run_narrative / run_drg_predict / run_drg_verify /
    run_hidden_alpha / run_scanner_scan) 를 함께 배포하고 Streamlit 앱을 리부트한다.
"""

import hashlib
import hmac
import secrets as _pysecrets

import pandas as pd

# ── 스키마 ─────────────────────────────────────────────────────────────────────
SSOT_VERSION = "2026-08-06a"

USER_SHEET_COLS = [
    "ID", "Password", "Reason", "Source", "Status",
    "Email", "Alert_Radar", "Alert_Global",
    "Alert_DRG", "Alert_Weekly", "Alert_HiddenAlpha", "Auto_Watchlist",
]
NCOL = len(USER_SHEET_COLS)               # 12
LAST_COL = chr(ord("A") + NCOL - 1)       # "L"

# v2(8열) 스키마 — 마이그레이션 판별용
_USER_SHEET_COLS_V2 = [
    "ID", "Password", "Reason", "Source", "Status",
    "Email", "Alert_Radar", "Alert_Global",
]

# 알림 종류 → Users 시트 컬럼
ALERT_KINDS = {
    "radar":  "Alert_Radar",     # 매매 레이더 (개인)
    "global": "Alert_Global",    # 시장 내러티브 브리핑
    "drg":    "Alert_DRG",       # DRG 예측·검증
    "weekly": "Alert_Weekly",    # 주간 3버킷 스캐너
    "hidden": "Alert_HiddenAlpha",  # Hidden Alpha 주간 ETF 로테이션
    "autowl": "Auto_Watchlist",  # 워치리스트 자동 편입(메일 아님)
}

# v2 → v3 마이그레이션 기본값.
#   기존 알림(DRG)은 현행 유지를 위해 Y, 신규 기능은 동의 없이 켜지 않도록 N.
#   관리자(yab)만 전부 Y 로 채운다.
_NEW_V3_COLS = ["Alert_DRG", "Alert_Weekly", "Alert_HiddenAlpha", "Auto_Watchlist"]
_V3_DEFAULTS_GUEST = {"Alert_DRG": "Y", "Alert_Weekly": "N",
                      "Alert_HiddenAlpha": "N", "Auto_Watchlist": "N"}
_V3_DEFAULTS_ADMIN = {"Alert_DRG": "Y", "Alert_Weekly": "Y",
                      "Alert_HiddenAlpha": "Y", "Auto_Watchlist": "Y"}

ADMIN_CONTENT_OWNER_ID = "yab"            # 전역(자동화) 콘텐츠 소유자 uid

_HASH_PREFIX = "pbkdf2"
_HASH_ITERATIONS = 120_000
_TRUTHY = {"y", "yes", "true", "1", "on"}


# ── 비밀번호 해시 ──────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    """평문 비밀번호 → 'pbkdf2$<iter>$<salt_hex>$<hash_hex>' 해시 문자열."""
    pw = str(password or "")
    salt = _pysecrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _HASH_ITERATIONS)
    return f"{_HASH_PREFIX}${_HASH_ITERATIONS}${salt.hex()}${dk.hex()}"


def is_hashed(stored: str) -> bool:
    """저장된 값이 해시 포맷인지 여부."""
    return str(stored or "").startswith(_HASH_PREFIX + "$")


def verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """비밀번호 검증. 반환: (일치 여부, 평문→해시 승격 필요 여부).

    - stored 가 해시 포맷이면 PBKDF2 재계산 후 상수시간 비교. (승격 불필요)
    - stored 가 레거시 평문이면 문자열 비교. 일치 시 승격 필요=True.
    """
    pw = str(password or "")
    st_val = str(stored or "")
    if is_hashed(st_val):
        try:
            _, iter_s, salt_hex, hash_hex = st_val.split("$", 3)
            dk = hashlib.pbkdf2_hmac(
                "sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_s)
            )
            return hmac.compare_digest(dk.hex(), hash_hex), False
        except Exception:
            return False, False
    # 레거시 평문
    ok = hmac.compare_digest(pw, st_val) and bool(st_val)
    return ok, ok


def gen_temp_password(length: int = 10) -> str:
    """임시 비밀번호 생성 (혼동 문자 제외 영숫자)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(_pysecrets.choice(alphabet) for _ in range(length))


# ── 시트 스키마/조회 ───────────────────────────────────────────────────────────
def ensure_users_header_v3(ws) -> bool:
    """헤더를 v3(11열)로 보장하고, v2 행에는 신규 3열 기본값을 채운다.

    ⚠️ 앱과 자동화 **양쪽 진입부에서** 호출해야 한다. 자동화가 앱보다 먼저 돌면
       구 스키마를 읽게 되고, 신규 토글이 빈 문자열이라 전부 꺼진 것으로 오인된다.

    기본값(A1): 게스트 Alert_DRG=Y · Alert_Weekly=N · Auto_Watchlist=N
                관리자(yab) 전부 Y
    반환: 마이그레이션을 실제로 수행했으면 True.
    """
    vals = ws.get_all_values()
    if not vals or not any(str(c).strip() for c in (vals[0] if vals else [])):
        ws.update([USER_SHEET_COLS], range_name=f"A1:{LAST_COL}1",
                  value_input_option="USER_ENTERED")
        return True

    hdr = [str(c).strip() for c in vals[0]]
    if hdr[:NCOL] == USER_SHEET_COLS and len(hdr) >= NCOL:
        return False  # 이미 v3

    # 헤더 갱신
    ws.update([USER_SHEET_COLS], range_name=f"A1:{LAST_COL}1",
              value_input_option="USER_ENTERED")

    # 본문 행의 신규 3열 채우기 (기존 8열은 손대지 않는다)
    body = vals[1:]
    if not body:
        return True
    new_cells = []
    for r in body:
        uid = str((list(r) + [""])[0]).strip().lower()
        defaults = _V3_DEFAULTS_ADMIN if uid == ADMIN_CONTENT_OWNER_ID else _V3_DEFAULTS_GUEST
        row = (list(r) + [""] * NCOL)[:NCOL]
        vals3 = []
        for i, col in enumerate(_NEW_V3_COLS, start=8):
            cur = str(row[i] or "").strip()
            vals3.append(cur if cur else defaults[col])
        new_cells.append(vals3)
    start_col = chr(ord("A") + 8)          # "I"
    end_row = 1 + len(new_cells)
    ws.update(new_cells, range_name=f"{start_col}2:{LAST_COL}{end_row}",
              value_input_option="USER_ENTERED")
    return True


# 하위 호환 별칭 — 기존 호출부(app.py 등)가 깨지지 않도록 유지
def ensure_users_header_v2(ws) -> None:
    """(레거시 별칭) v3 마이그레이션을 수행한다."""
    ensure_users_header_v3(ws)


def fetch_users_df(ws) -> pd.DataFrame:
    """Users 시트 전체를 v2 스키마(8열) DataFrame 으로 반환. 부족한 열은 빈값 패딩."""
    vals = ws.get_all_values()
    if not vals or len(vals) < 2:
        return pd.DataFrame(columns=USER_SHEET_COLS)
    body = [(r + [""] * NCOL)[:NCOL] for r in vals[1:]]
    df = pd.DataFrame(body, columns=USER_SHEET_COLS)
    # 완전 빈 행 제거
    df = df[df["ID"].astype(str).str.strip().ne("")].reset_index(drop=True)
    return df


def find_user_row_index(ws, user_id: str) -> int | None:
    """user_id 가 위치한 시트 행 번호(1-based) 반환. 없으면 None."""
    uid_u = str(user_id or "").strip().upper()
    if not uid_u:
        return None
    vals = ws.get_all_values()
    for ri, r in enumerate(vals[1:], start=2):
        if str((r + [""])[0]).strip().upper() == uid_u:
            return ri
    return None


def update_user_fields(ws, user_id: str, fields: dict) -> tuple[bool, str]:
    """해당 user_id 행의 특정 컬럼들만 in-place 업데이트.

    fields: {"Email": "...", "Alert_Radar": "Y", ...} — USER_SHEET_COLS 내 컬럼만 허용.
    """
    bad = [k for k in fields if k not in USER_SHEET_COLS]
    if bad:
        return False, f"허용되지 않은 컬럼: {bad}"
    ri = find_user_row_index(ws, user_id)
    if ri is None:
        return False, f"Users 시트에서 ID '{user_id}' 를 찾을 수 없습니다."
    try:
        vals = ws.get_all_values()
        row = (vals[ri - 1] + [""] * NCOL)[:NCOL]
        for k, v in fields.items():
            row[USER_SHEET_COLS.index(k)] = str(v)
        ws.update([row], range_name=f"A{ri}:{LAST_COL}{ri}",
                  value_input_option="USER_ENTERED")
        return True, ""
    except Exception as exc:
        return False, f"Users 시트 업데이트 실패: {exc}"


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in _TRUTHY


def alert_column(kind: str) -> str:
    """알림 종류 → Users 시트 컬럼명. 미지의 kind 는 Alert_Global 로 폴백."""
    return ALERT_KINDS.get(str(kind).strip().lower(), "Alert_Global")


def get_recipients(ws, kind: str, admin_fallback_email: str = None) -> list[tuple[str, str]]:
    """수신자 목록 조회.

    Args:
        kind: 'radar' | 'global' | 'drg' | 'weekly' | 'autowl'
        admin_fallback_email: GMAIL_TO. **yab 행이 없거나 Email 이 무효일 때만**
            관리자를 이 주소로 보충한다(시트 사고 방어).
            ⚠️ yab 행이 존재하고 토글이 명시적 "N" 이면 폴백하지 않는다 —
               그래야 관리자가 실제로 알림을 끌 수 있다.

    조건: Status == approved AND 해당 토글 truthy AND Email 유효.
    반환: [(user_id, email), ...]  (중복 이메일 제거, 시트 순서 유지)
    """
    col = alert_column(kind)
    try:
        df = fetch_users_df(ws)
    except Exception:
        df = pd.DataFrame(columns=USER_SHEET_COLS)

    out, seen = [], set()
    admin_row_ok = False          # yab 행이 존재하고 Email 도 유효한가
    for _, r in df.iterrows():
        uid = str(r.get("ID", "")).strip()
        email = str(r.get("Email", "")).strip()
        is_admin = uid.lower() == ADMIN_CONTENT_OWNER_ID
        if is_admin and email and "@" in email:
            admin_row_ok = True
        if str(r.get("Status", "")).strip().lower() != "approved":
            continue
        if not _truthy(r.get(col, "")):
            continue
        if not email or "@" not in email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((uid, email))

    # 관리자 행이 없거나 Email 이 비어 데이터로 판단 불가한 경우에만 폴백
    if admin_fallback_email and not admin_row_ok:
        fb = str(admin_fallback_email).strip()
        if fb and "@" in fb and fb.lower() not in seen:
            out.append((ADMIN_CONTENT_OWNER_ID, fb))
    return out


def user_flag(ws, user_id: str, kind: str, default: bool = False) -> bool:
    """특정 사용자의 토글 값 조회 (autowl 처럼 메일이 아닌 플래그용)."""
    col = alert_column(kind)
    try:
        df = fetch_users_df(ws)
    except Exception:
        return default
    uid_u = str(user_id or "").strip().upper()
    for _, r in df.iterrows():
        if str(r.get("ID", "")).strip().upper() != uid_u:
            continue
        if str(r.get("Status", "")).strip().lower() != "approved":
            return False
        return _truthy(r.get(col, ""))
    return default


def get_flagged_users(ws, kind: str) -> list[str]:
    """해당 토글이 켜진 승인 사용자 ID 목록 (이메일 불필요한 플래그용)."""
    col = alert_column(kind)
    try:
        df = fetch_users_df(ws)
    except Exception:
        return []
    out = []
    for _, r in df.iterrows():
        if str(r.get("Status", "")).strip().lower() != "approved":
            continue
        if not _truthy(r.get(col, "")):
            continue
        uid = str(r.get("ID", "")).strip()
        if uid:
            out.append(uid)
    return out
