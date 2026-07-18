# -*- coding: utf-8 -*-
"""
users_core.py — Users 시트 SSOT 모듈 (app.py + automation 공용)
────────────────────────────────────────────────────────────────
스키마 v2 (8열): ID, Password, Reason, Source, Status, Email, Alert_Radar, Alert_Global

- Password: "pbkdf2$<iterations>$<salt_hex>$<hash_hex>" 형식 해시.
  접두어(pbkdf2$)가 없으면 레거시 평문으로 간주 → 로그인 성공 시 호출측이 해시로 승격.
- Alert_Radar : 매매 레이더(개인 메일) 수신 여부 ("Y"/"N")
- Alert_Global: 시장 브리핑(내러티브·DRG 전역 메일) 수신 여부 ("Y"/"N")

설계 원칙:
- gspread Worksheet 객체를 인자로 받아 동작한다. 클라이언트 생성은 호출측 책임
  (app.py = st.secrets 서비스 계정, automation = GSPREAD_KEY 환경변수).
- 이 모듈은 streamlit / gspread 를 import 하지 않는다 (양쪽 환경 공용).

⚠️ lockstep: 이 파일 변경 시 app.py + automation 4개
   (run_watchlist_alerts / run_narrative / run_drg_predict / run_drg_verify)
   를 함께 배포하고 Streamlit 앱을 리부트한다.
"""

import hashlib
import hmac
import secrets as _pysecrets

import pandas as pd

# ── 스키마 ─────────────────────────────────────────────────────────────────────
USER_SHEET_COLS = [
    "ID", "Password", "Reason", "Source", "Status",
    "Email", "Alert_Radar", "Alert_Global",
]
NCOL = len(USER_SHEET_COLS)               # 8
LAST_COL = chr(ord("A") + NCOL - 1)       # "H"

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
def ensure_users_header_v2(ws) -> None:
    """헤더가 없거나 구버전(5열)이면 v2(8열) 헤더로 확장. 기존 데이터 행은 보존."""
    vals = ws.get_all_values()
    if not vals or not any(str(c).strip() for c in vals[0]):
        ws.update([USER_SHEET_COLS], range_name=f"A1:{LAST_COL}1",
                  value_input_option="USER_ENTERED")
        return
    hdr = [str(c).strip() for c in (vals[0] + [""] * NCOL)[:NCOL]]
    if hdr != USER_SHEET_COLS:
        ws.update([USER_SHEET_COLS], range_name=f"A1:{LAST_COL}1",
                  value_input_option="USER_ENTERED")


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


def get_recipients(ws, kind: str) -> list[tuple[str, str]]:
    """수신자 목록 조회. kind: 'radar'(매매 레이더) | 'global'(시장 브리핑).

    조건: Status == approved AND 해당 토글 truthy AND Email 비어있지 않음.
    반환: [(user_id, email), ...]  (중복 이메일 제거, 시트 순서 유지)
    """
    col = "Alert_Radar" if str(kind).strip().lower() == "radar" else "Alert_Global"
    try:
        df = fetch_users_df(ws)
    except Exception:
        return []
    out, seen = [], set()
    for _, r in df.iterrows():
        if str(r.get("Status", "")).strip().lower() != "approved":
            continue
        if not _truthy(r.get(col, "")):
            continue
        email = str(r.get("Email", "")).strip()
        if not email or "@" not in email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((str(r.get("ID", "")).strip(), email))
    return out
