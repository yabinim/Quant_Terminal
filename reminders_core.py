"""reminders_core.py — 개발 로드맵 리마인더 SSOT.

왜 필요한가
───────────
  "3개월 뒤 다시 본다"로 미뤄둔 항목이 여러 개다. 사람은 잊는다.
  이미 매일 도는 메일 경로가 있으니 거기에 얹는다.

  ⚠️ 이건 **투자 알림이 아니라 개발 로드맵 관리**다. 관리자 전용이며
     게스트에게 보내지 않는다. Users 시트에 토글을 만들지 않는 이유도 같다.
     나중에 사용자별 개인 리마인더가 필요해지면 그때 Alert_Reminder 열을
     추가해 확장한다(현 스키마는 그 확장을 막지 않는다).

설계 원칙
─────────
  · 순수 모듈 — gspread 를 import 하지 않는다. 시트 접근은 호출부 책임이다.
    그래야 회귀 스위트가 시트 없이 로직을 검증할 수 있다.
  · app.py 와 run_narrative.py 가 **같은 함수**로 만기를 판정한다.
    앱에서 '오늘 만기'인데 메일엔 안 오는 식의 불일치가 나면 안 된다.
  · Snoozed_Until 이 있으면 그것이 Due_Date 를 이긴다.
    "봤지만 아직 못 함"을 done 으로 처리하면 항목이 영영 사라진다.

상태
────
  open  — 살아 있음
  done  — 완료. 목록에서 빠지지만 행은 남긴다(언제 무엇을 했는지 기록)

분류 (classify)
───────────────
  overdue  만기가 지났다        → 계속 뜬다. 지날수록 위로 올라온다
  due      오늘이 만기다
  soon     PREVIEW_DAYS 이내다  → 미리 보고 준비하라는 뜻
  later    아직 멀었다          → 메일에 넣지 않는다
  done     완료
"""
from datetime import date, timedelta

REMINDERS_WORKSHEET = "Reminders"

REMINDER_COLS = [
    "ID",             # 안정 키. 완료·연기 시 이 값으로 행을 찾는다
    "Due_Date",       # YYYY-MM-DD
    "Title",          # 한 줄 제목
    "Category",       # 백테스트 / 검증 / 정리 / 기타
    "What_To_Check",  # ⚠️ 핵심. 3개월 뒤의 나에게 주는 실행 지시
    "Why",            # 왜 미뤘는지 — 판단 근거를 잊지 않기 위해
    "Status",         # open / done
    "Snoozed_Until",  # 비어 있으면 Due_Date 를 쓴다
    "Created",        # YYYY-MM-DD
    "Source",         # 어느 결정에서 나왔나
]
REMINDER_NCOL = len(REMINDER_COLS)

STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUSES = (STATUS_OPEN, STATUS_DONE)

PREVIEW_DAYS = 7          # 만기 며칠 전부터 알릴 것인가
SNOOZE_CHOICES = (7, 14, 30)   # 앱 UI 연기 버튼

CATEGORIES = ("백테스트", "검증", "정리", "기타")

_KIND_ORDER = {"overdue": 0, "due": 1, "soon": 2, "later": 3, "done": 4}
_KIND_LABEL = {
    "overdue": "🔴 기한 지남",
    "due": "🟠 오늘 만기",
    "soon": "🟡 곧 만기",
    "later": "⚪ 예정",
    "done": "✅ 완료",
}


def _pd(v):
    """'YYYY-MM-DD' → date. 실패하면 None (추측해서 채우지 않는다)."""
    s = str(v or "").strip()[:10]
    if len(s) != 10:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def new_id(title: str, due: str) -> str:
    """제목+만기로 안정 ID. 같은 항목을 두 번 넣으면 같은 ID 가 나온다."""
    base = "".join(ch for ch in str(title or "") if ch.isalnum())[:16]
    return f"{str(due or '')[:10]}_{base or 'item'}"


def parse_reminders(values) -> list:
    """시트 2차원 값 → dict 목록. 헤더 행은 건너뛴다.

    열이 모자란 행은 빈 문자열로 채운다. 남는 열은 버린다.
    스키마가 늘어나도 옛 행이 죽지 않게 하기 위함이다.
    """
    out = []
    if not values or len(values) < 2:
        return out
    for r in values[1:]:
        row = (list(r) + [""] * REMINDER_NCOL)[:REMINDER_NCOL]
        d = {c: str(row[i]).strip() for i, c in enumerate(REMINDER_COLS)}
        if not d.get("ID") and not d.get("Title"):
            continue          # 완전 빈 행
        out.append(d)
    return out


def effective_due(rem: dict):
    """실제로 적용되는 만기. Snoozed_Until 이 Due_Date 를 이긴다."""
    return _pd(rem.get("Snoozed_Until")) or _pd(rem.get("Due_Date"))


def classify(rem: dict, today=None) -> str:
    if str(rem.get("Status") or "").strip().lower() == STATUS_DONE:
        return "done"
    t = today or date.today()
    due = effective_due(rem)
    if due is None:
        # 만기를 해석하지 못했다. 조용히 숨기면 영영 안 보이므로 띄운다.
        return "overdue"
    if due < t:
        return "overdue"
    if due == t:
        return "due"
    if due <= t + timedelta(days=PREVIEW_DAYS):
        return "soon"
    return "later"


def kind_label(kind: str) -> str:
    return _KIND_LABEL.get(kind, kind)


def days_left(rem: dict, today=None):
    """남은 일수. 음수면 지난 것. 해석 불가면 None."""
    due = effective_due(rem)
    if due is None:
        return None
    return (due - (today or date.today())).days


def sort_key(rem: dict, today=None):
    """급한 것부터. 같은 등급이면 만기가 이른 것부터."""
    k = classify(rem, today)
    d = days_left(rem, today)
    return (_KIND_ORDER.get(k, 9), 99999 if d is None else d,
            str(rem.get("Title") or ""))


def active_reminders(rows, today=None) -> list:
    """메일·배지에 띄울 것만. later 와 done 은 뺀다."""
    t = today or date.today()
    live = [r for r in rows if classify(r, t) in ("overdue", "due", "soon")]
    live.sort(key=lambda r: sort_key(r, t))
    return live


def to_row(rem: dict) -> list:
    return [str(rem.get(c, "") or "") for c in REMINDER_COLS]


def make(title, due, what_to_check, why="", category="기타",
         source="", created=None) -> dict:
    return {
        "ID": new_id(title, due),
        "Due_Date": str(due)[:10],
        "Title": str(title or ""),
        "Category": str(category or "기타"),
        "What_To_Check": str(what_to_check or ""),
        "Why": str(why or ""),
        "Status": STATUS_OPEN,
        "Snoozed_Until": "",
        "Created": str(created or date.today())[:10],
        "Source": str(source or ""),
    }


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def build_email_html(rows, today=None) -> str:
    """관리자 메일에 붙일 리마인더 블록. 띄울 게 없으면 빈 문자열.

    빈 문자열을 반환하는 것이 중요하다 — 매일 "리마인더 없음"이 붙으면
    블록 자체를 안 읽게 되고, 정작 만기가 왔을 때도 안 읽는다.
    """
    t = today or date.today()
    live = active_reminders(rows, t)
    if not live:
        return ""

    items = []
    for r in live:
        kind = classify(r, t)
        d = days_left(r, t)
        if d is None:
            when = "만기 해석 불가"
        elif d < 0:
            when = f"{-d}일 지남"
        elif d == 0:
            when = "오늘"
        else:
            when = f"{d}일 남음"

        snz = str(r.get("Snoozed_Until") or "").strip()
        due_txt = _esc(r.get("Due_Date"))
        if snz:
            due_txt += f" → 연기 {_esc(snz)}"

        why = str(r.get("Why") or "").strip()
        items.append(
            "<li style='margin:0 0 14px 0;'>"
            f"<div><b>{_esc(kind_label(kind))} {_esc(r.get('Title'))}</b>"
            f" <span style='color:#888;'>({when} · {due_txt})</span></div>"
            f"<div style='margin-top:4px;'>{_esc(r.get('What_To_Check'))}</div>"
            + (f"<div style='margin-top:2px;color:#888;font-size:13px;'>"
               f"미뤘던 이유 — {_esc(why)}</div>" if why else "")
            + "</li>"
        )

    return (
        "<div style='margin-top:28px;padding:16px;border:1px solid #ddd;"
        "border-radius:8px;background:#fafafa;'>"
        "<div style='font-size:16px;font-weight:700;margin-bottom:10px;'>"
        f"🔔 개발 리마인더 {len(live)}건</div>"
        "<ul style='margin:0;padding-left:18px;'>" + "".join(items) + "</ul>"
        "<div style='margin-top:10px;color:#888;font-size:12px;'>"
        "앱 → 👑 [관리자] 유저 승인 탭 하단에서 완료·연기 처리할 수 있습니다."
        "</div></div>"
    )
