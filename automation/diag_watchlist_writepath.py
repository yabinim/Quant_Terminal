# -*- coding: utf-8 -*-
"""diag_watchlist_writepath.py — Watchlist 쓰기 경로 드리프트 방지 회귀 테스트.

app.py 에서 대상 함수만 AST 로 떼어내 가짜 워크시트 위에서 실행한다.
Streamlit·gspread·네트워크 없이 돌아가므로 GitHub Actions 에서 안전하다.

검증 대상 (드리프트 4중 가드):
  G1. 행 번호는 '방금 읽은 스냅샷'에서만 온다        → stale row 차단
  G2. 기록 직전 A열(ID)·B열(Ticker) 재대조           → 타인 행 파손 차단
  G3. 기록 셀 수는 항상 정확히 13칸                  → 옆 열 밀림 차단
  G4. 기록 range 는 항상 A{r}:M{r} (시작열 A 고정)   → 계단식 드리프트 차단

마지막에 각 가드를 하나씩 고의로 부순 뒤(뮤테이션) 테스트가 실제로 실패하는지
확인한다. '항상 통과하는 무력한 테스트'를 걸러내기 위한 절차다.

실행: python diag_watchlist_writepath.py
"""
from __future__ import annotations

import ast
import re
import sys
import types

def _find_app_py() -> str:
    """automation/ 에서 실행하든 저장소 루트에서 실행하든 app.py 를 찾는다."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "app.py"),
                 os.path.join(here, "..", "app.py"),
                 "app.py"):
        if os.path.isfile(cand):
            return os.path.normpath(cand)
    raise SystemExit("[치명] app.py 를 찾을 수 없습니다.")


APP_PATH = _find_app_py()

# app.py 에서 떼어낼 함수들 (정의 순서 유지)
TARGET_FUNCS = [
    "_wl_row_to_item",
    "_wl_item_to_row",
    "_wl_session_key",
    "_wl_session_store",
    "_wl_session_invalidate",
    "_wl_session_set",
    "_wl_session_upsert",
    "_wl_session_remove",
    "update_watchlist_row",
]

_WATCHLIST_SHEET_COLS = ["ID", "Ticker", "Memo", "Alert_Price", "Alert_RSI",
                         "Alert_MA200", "Saved_Price", "Date_Added", "Stop_Loss",
                         "Target_Price", "Alert_States", "Alert_LastState", "Account"]
_WL_NCOL = len(_WATCHLIST_SHEET_COLS)
_WL_COL_ACCOUNT = 12
_EDITABLE = (2, 3, 4, 5, 6, 7, 8, 9, 10, 12)


# ══════════════════════════════════════════════════════════════════════════════
# 가짜 환경
# ══════════════════════════════════════════════════════════════════════════════
class FakeWorksheet:
    """ws.update() 호출을 기록만 하고 실제 반영도 하는 가짜 시트."""

    def __init__(self, rows):
        self.rows = [list(r) for r in rows]
        self.updates = []          # [(range_name, values)]  — 전체행 덮어쓰기
        self.writes = []           # [(cell, value)]         — 셀 단위 기록
        self.probes = []           # 좌표 재확인 읽기 이력

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def get(self, range_name):
        """A{r}:B{r} 형태의 좌표 재확인 읽기."""
        self.probes.append(range_name)
        m = re.fullmatch(r"A(\d+):B(\d+)", str(range_name))
        if not m:
            raise AssertionError(f"예상치 못한 probe range: {range_name}")
        r0 = int(m.group(1))
        if r0 - 1 >= len(self.rows):
            return []
        return [list(self.rows[r0 - 1])[:2]]

    def batch_update(self, data, value_input_option=None):
        for req in data:
            rng = req["range"]
            m = re.fullmatch(r"([A-Z]+)(\d+)", str(rng))
            if not m:
                raise AssertionError(f"단일 셀이 아닌 range: {rng}")
            col = 0
            for ch in m.group(1):
                col = col * 26 + (ord(ch) - 64)
            row = int(m.group(2))
            vals = req["values"]
            if len(vals) != 1 or len(vals[0]) != 1:
                raise AssertionError(f"셀 1칸이 아님: {rng} -> {vals}")
            while len(self.rows) < row:
                self.rows.append([""] * _WL_NCOL)
            r = list(self.rows[row - 1]) + [""] * _WL_NCOL
            r = r[:max(_WL_NCOL, col)]
            r[col - 1] = vals[0][0]
            self.rows[row - 1] = r[:_WL_NCOL]
            self.writes.append((rng, vals[0][0]))

    def update(self, values, range_name=None, value_input_option=None):
        self.updates.append((range_name, [list(v) for v in values]))
        m = re.fullmatch(r"A(\d+):([A-Z]+)(\d+)", str(range_name or ""))
        if not m:
            raise AssertionError(f"예상치 못한 range 형식: {range_name}")
        r0 = int(m.group(1))
        while len(self.rows) < r0 + len(values) - 1:
            self.rows.append([""] * _WL_NCOL)
        for k, v in enumerate(values):
            self.rows[r0 - 1 + k] = list(v)


class FakeSessionState(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

    def pop(self, k, d=None):
        return dict.pop(self, k, d)


class FakeSt:
    def __init__(self):
        self.session_state = FakeSessionState()


class FakeUtils:
    @staticmethod
    def rowcol_to_a1(row, col):
        s = ""
        c = col
        while c:
            c, r = divmod(c - 1, 26)
            s = chr(65 + r) + s
        return f"{s}{row}"


class FakeGspread:
    utils = FakeUtils()


class FakePd:
    @staticmethod
    def isna(v):
        return v is None or (isinstance(v, float) and v != v)

    @staticmethod
    def to_numeric(v, errors=None):
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("nan")


def load_namespace(src: str, opened_ws):
    """app.py 소스에서 대상 함수만 뽑아 격리 네임스페이스에 심는다."""
    tree = ast.parse(src)
    picked, found = [], set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in TARGET_FUNCS:
            picked.append(node)
            found.add(node.name)
    missing = set(TARGET_FUNCS) - found
    if missing:
        raise SystemExit(f"[치명] app.py 에서 함수를 찾지 못함: {sorted(missing)}")
    picked.sort(key=lambda n: TARGET_FUNCS.index(n.name))

    # 모듈 상수는 app.py 에서 직접 읽어 값이 갈리지 않게 한다
    _sess_key = None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and node.targets
                and getattr(node.targets[0], "id", "") == "_WL_SESSION_KEY"):
            _sess_key = ast.literal_eval(node.value)
    if _sess_key is None:
        raise SystemExit("[치명] app.py 에서 _WL_SESSION_KEY 를 찾지 못함")

    ns = {
        "st": FakeSt(), "pd": FakePd(), "np": types.SimpleNamespace(nan=float("nan")),
        "gspread": FakeGspread(), "_WL_SESSION_KEY": _sess_key,
        "_WL_EDITABLE_COL_IDX": _EDITABLE,
        "_WL_NCOL": _WL_NCOL, "_WL_COL_ACCOUNT": _WL_COL_ACCOUNT,
        "_WATCHLIST_SHEET_COLS": _WATCHLIST_SHEET_COLS,
        "_narrative_now_et_string": lambda: "2026-08-10 09:00",
        "open_watchlist_worksheet": lambda: (opened_ws, None),
        "load_watchlist_sheet": types.SimpleNamespace(clear=lambda: None),
    }
    exec(compile(ast.Module(body=picked, type_ignores=[]), "<extract>", "exec"), ns)
    return ns


def base_rows():
    """admin(yab) 2종목 + 게스트(guest1) 1종목. 게스트 행은 절대 변하면 안 된다."""
    return [
        _WATCHLIST_SHEET_COLS,
        ["yab", "NVDA", "메모A", "", "", "false", "100.5", "2026-01-02",
         "90", "150", "entry,risk", "", "Roth"],
        ["guest1", "NVDA", "게스트메모", "", "", "false", "101", "2026-01-03",
         "", "", "entry", "", "HSA"],
        ["yab", "AVGO", "메모B", "", "", "false", "200", "2026-01-04",
         "", "", "watch", "", ""],
    ]


# ══════════════════════════════════════════════════════════════════════════════
# 테스트
# ══════════════════════════════════════════════════════════════════════════════
def run_tests(src: str) -> list:
    fails = []

    def ck(cond, name, detail=""):
        if not cond:
            fails.append(f"{name}{(' — ' + detail) if detail else ''}")

    # ── G3: 직렬화는 어떤 입력에도 항상 13칸 ────────────────────────────────
    ws0 = FakeWorksheet(base_rows())
    ns = load_namespace(src, ws0)
    to_row = ns["_wl_item_to_row"]
    weird = [
        {}, {"ticker": "X"}, {"ticker": "X", "memo": "a,b,c"},
        {"ticker": "X", "alert_states": ["entry", "risk"]},
        {"ticker": "X", "alert_states": "entry"},
        {"ticker": "X", "stop_loss": None, "target_price": float("nan")},
        {"ticker": "X", "alert_price": "쓰레기값", "alert_rsi": ""},
        {"ticker": "X", "memo": "탭\t줄바꿈\n포함", "account": None},
    ]
    for w in weird:
        r = to_row("yab", w)
        ck(len(r) == _WL_NCOL, "G3 셀수", f"{w} → {len(r)}칸")
        ck(all(isinstance(c, str) for c in r), "G3 타입", f"{w} → {r}")

    # ── G3/G4 + 정상 갱신: 변경 칸만, 게스트 행 무손상 ─────────────────────
    ws = FakeWorksheet(base_rows())
    ns = load_namespace(src, ws)
    upd = ns["update_watchlist_row"]
    before_guest = list(ws.rows[2])
    ok, err, item = upd("yab", "NVDA", {"memo": "수정됨", "stop_loss": 95.0})
    ck(ok, "정상 수정 성공", err)
    ck(len(ws.updates) == 0, "전체행 덮어쓰기 없음", f"{ws.updates}")
    ck(len(ws.probes) == 1, "좌표 재확인 1회", f"{ws.probes}")
    ck(ws.probes[0] == "A2:B2", "재확인 좌표", f"{ws.probes[0]}")
    # 바꾼 건 memo(C) + stop_loss(I) 둘뿐 — 나머지는 손대지 않아야 한다
    ck(sorted(c for c, _ in ws.writes) == ["C2", "I2"],
       "G3 변경 칸만 기록", f"{ws.writes}")
    ck(ws.rows[2] == before_guest, "게스트 행 무손상", f"{ws.rows[2]}")
    ck(ws.rows[1][2] == "수정됨", "memo 반영", f"{ws.rows[1][2]}")

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    ck(num(ws.rows[1][8]) == 95.0, "stop_loss 반영", f"{ws.rows[1][8]}")
    # 미지정 필드는 '원문 그대로' 남아야 한다 (표기 정규화조차 없음)
    ck(ws.rows[1][9] == "150", "target_price 원문 보존", f"{ws.rows[1][9]}")
    ck(ws.rows[1][10] == "entry,risk", "alert_states 원문 보존", f"{ws.rows[1][10]}")
    ck(ws.rows[1][12] == "Roth", "account 원문 보존", f"{ws.rows[1][12]}")
    ck(ws.rows[1][6] == "100.5", "saved_price 원문 보존", f"{ws.rows[1][6]}")
    ck(ws.rows[1][7] == "2026-01-02", "date_added 원문 보존", f"{ws.rows[1][7]}")

    # ── L열(Alert_LastState)은 자동화 소유 — 앱이 절대 쓰면 안 된다 ─────────
    rows_ls = base_rows()
    rows_ls[1][11] = "AUTO_STATE_2026"
    ws = FakeWorksheet(rows_ls)
    ns = load_namespace(src, ws)
    ok, err, _ = ns["update_watchlist_row"](
        "yab", "NVDA", {"memo": "m", "alert_last_state": "앱이덮어쓰기시도"})
    ck(ok, "L열 보호 케이스 성공", err)
    ck(all(not c.startswith("L") for c, _ in ws.writes),
       "L열 미기록", f"{ws.writes}")
    ck(ws.rows[1][11] == "AUTO_STATE_2026", "자동화 상태 보존", f"{ws.rows[1][11]}")

    # ── 변경 없음이면 쓰기 자체를 생략 ──────────────────────────────────────
    ws = FakeWorksheet(base_rows())
    ns = load_namespace(src, ws)
    ok, err, _ = ns["update_watchlist_row"]("yab", "NVDA", {"memo": "메모A"})
    ck(ok, "무변경 성공", err)
    ck(len(ws.writes) == 0 and len(ws.probes) == 0,
       "무변경 시 쓰기·재확인 생략", f"writes={ws.writes} probes={ws.probes}")

    # ── 같은 티커라도 소유자가 다르면 admin 행만 잡아야 한다 ────────────────
    ws = FakeWorksheet(base_rows())
    ns = load_namespace(src, ws)
    ok, err, _ = ns["update_watchlist_row"]("guest1", "NVDA", {"memo": "G"})
    ck(ok, "게스트 수정 성공", err)
    ck(all(c.endswith("3") for c, _ in ws.writes), "소유자별 행 선택", f"{ws.writes}")
    ck(ws.rows[1][2] == "메모A", "admin 행 무손상", f"{ws.rows[1][2]}")

    # ── 행이 없으면 not_found (호출부가 add 로 폴백) ────────────────────────
    ws = FakeWorksheet(base_rows())
    ns = load_namespace(src, ws)
    ok, err, _ = ns["update_watchlist_row"]("yab", "TSLA", {"memo": "X"})
    ck((not ok) and err == "not_found", "미존재 → not_found", f"ok={ok} err={err}")
    ck(len(ws.writes) == 0, "미존재 시 무기록", f"{ws.writes}")

    # ── 빈 시트 / 헤더만 있는 시트에서도 기록하지 않는다 ────────────────────
    for rows, label in [([], "빈 시트"), ([_WATCHLIST_SHEET_COLS], "헤더만")]:
        ws = FakeWorksheet(rows)
        ns = load_namespace(src, ws)
        ok, err, _ = ns["update_watchlist_row"]("yab", "NVDA", {"memo": "X"})
        ck(not ok, f"{label} → 실패 반환", f"ok={ok}")
        ck(len(ws.writes) == 0, f"{label} → 무기록", f"{ws.writes}")

    # ── 구 8열 레코드도 13칸으로 정규화되어야 한다 (열 밀림 방지) ───────────
    legacy = [_WATCHLIST_SHEET_COLS,
              ["yab", "AMD", "구메모", "", "", "false", "50", "2026-01-05"]]
    ws = FakeWorksheet(legacy)
    ns = load_namespace(src, ws)
    ok, err, _ = ns["update_watchlist_row"]("yab", "AMD", {"memo": "새메모"})
    ck(ok, "구 8열 수정 성공", err)
    ck([c for c, _ in ws.writes] == ["C2"], "구 8열 — 변경 칸만", f"{ws.writes}")
    ck(len(ws.rows[1]) == _WL_NCOL, "구 8열 → 13칸 정규화", f"{len(ws.rows[1])}칸")

    # ── G2: 스냅샷 이후 행이 밀리면 반드시 거부해야 한다 ────────────────────
    class ShiftingWorksheet(FakeWorksheet):
        """get_all_values() 직후 다른 프로세스가 행을 지운 상황을 흉내낸다."""

        def __init__(self, rows):
            super().__init__(rows)
            self._served = False

        def get_all_values(self):
            snap = [list(r) for r in self.rows]
            if not self._served:
                self._served = True
                del self.rows[1]      # 자동화가 2행 삭제 → 이후 행 번호가 한 칸 밀림
            return snap

    ws = ShiftingWorksheet(base_rows())
    ns = load_namespace(src, ws)
    ok, err, _ = ns["update_watchlist_row"]("yab", "AVGO", {"memo": "위험"})
    ck(not ok, "G2 행 이동 시 거부", f"ok={ok} err={err}")
    ck(len(ws.writes) == 0, "G2 오좌표 무기록", f"{ws.writes}")
    # 밀린 자리에 있던 게스트 행이 손상되지 않았는지
    ck(any(r[0] == "guest1" and r[2] == "게스트메모" for r in ws.rows),
       "G2 타인 행 무손상", f"{ws.rows}")

    # ── 세션 레이어: 미적재 사용자에게 부분 목록을 굳히지 않는다 ────────────
    ws = FakeWorksheet(base_rows())
    ns = load_namespace(src, ws)
    ns["_wl_session_upsert"]("yab", {"ticker": "NVDA"})
    store = ns["st"].session_state.get(ns["_WL_SESSION_KEY"])
    ck(store is None or "YAB" not in (store or {}),
       "미적재 사용자 upsert 무시", f"store={store}")
    ns["_wl_session_set"]("yab", [{"ticker": "NVDA"}, {"ticker": "AVGO"}])
    ns["_wl_session_upsert"]("yab", {"ticker": "NVDA", "memo": "M"})
    got = ns["st"].session_state[ns["_WL_SESSION_KEY"]]["YAB"]
    ck(len(got) == 2, "upsert 중복 없음", f"{len(got)}건")
    ns["_wl_session_remove"]("yab", "NVDA")
    got = ns["st"].session_state[ns["_WL_SESSION_KEY"]]["YAB"]
    ck([g["ticker"] for g in got] == ["AVGO"], "remove 동작", f"{got}")
    # 사용자 분리
    ns["_wl_session_set"]("guest1", [{"ticker": "SPY"}])
    ck(ns["st"].session_state[ns["_WL_SESSION_KEY"]]["YAB"] !=
       ns["st"].session_state[ns["_WL_SESSION_KEY"]]["GUEST1"],
       "사용자별 목록 분리")

    return fails


# ══════════════════════════════════════════════════════════════════════════════
# 뮤테이션 — 가드를 부수면 테스트가 실제로 실패하는가?
# ══════════════════════════════════════════════════════════════════════════════
MUTATIONS = [
    ("G3 직렬화 정규화 제거",
     'return (row + [""] * _WL_NCOL)[:_WL_NCOL]',
     "return row + ['DRIFT']"),
    ("G4 화이트리스트를 전 컬럼으로 확대",
     "for _idx in _WL_EDITABLE_COL_IDX:",
     "for _idx in range(_WL_NCOL):"),
    ("G2 좌표 재확인 제거",
     'if str(_pr[0]).strip().upper() != uid_u or str(_pr[1]).strip().upper() != tk:',
     "if False:"),
    ("G2 재확인을 옛 스냅샷으로 되돌림(죽은 가드 재발)",
     '_probe = ws.get(f"A{target_row}:B{target_row}") or []',
     "_probe = [vals[target_row - 1]] if target_row - 1 < len(vals) else []"),
    ("행 탐색에서 소유자 조건 제거",
     'if str(rr[0]).strip().upper() == uid_u and str(rr[1]).strip().upper() == tk:',
     "if str(rr[1]).strip().upper() == tk:"),
    ("변경 감지 무력화(전 필드 재기록)",
     "if str(old_cells[_idx]) == str(new_cells[_idx]):",
     "if False:"),
]


def main():
    src = open(APP_PATH, encoding="utf-8").read()

    print("=" * 74)
    print("1) 원본 검증")
    print("=" * 74)
    fails = run_tests(src)
    if fails:
        for f in fails:
            print(f"  [NG] {f}")
        print(f"\n>>> 원본에서 {len(fails)}건 실패 — 중단")
        return 1
    print("  [OK] 전 항목 통과")

    print()
    print("=" * 74)
    print("2) 뮤테이션 검증 (가드를 부수면 반드시 실패해야 함)")
    print("=" * 74)
    weak = 0
    for name, old, new in MUTATIONS:
        if src.count(old) != 1:
            print(f"  [SKIP] {name}: 앵커 {src.count(old)}회 — 코드 변경됨, 뮤테이션 갱신 필요")
            weak += 1
            continue
        try:
            mf = run_tests(src.replace(old, new, 1))
        except Exception as e:
            mf = [f"예외 발생: {type(e).__name__}"]
        if mf:
            print(f"  [OK]   {name}: {len(mf)}건 탐지 (예: {mf[0][:52]})")
        else:
            print(f"  [WEAK] {name}: 부쉈는데도 전부 통과 — 테스트가 무력함")
            weak += 1

    print()
    print("=" * 74)
    if weak:
        print(f">>> 결과: 뮤테이션 {weak}건이 탐지되지 않음 — 테스트 보강 필요")
        return 1
    print(">>> 결과: 원본 통과 + 뮤테이션 전건 탐지. 드리프트 가드 유효.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
