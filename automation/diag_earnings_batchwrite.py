"""diag_earnings_batchwrite.py — 캘린더/이벤트 배치 쓰기 회귀 검사.

run_earnings_watch.py 의 쓰기 헬퍼를 **소스에서 AST 로 추출**해 검증한다
(로직 사본이 아니라 실제 코드). Sheets 없이 스텁 워크시트로 API 콜 수만 센다.

배경: 2026-08-14, FORCE_CALENDAR 로 169행을 갱신하다 행마다 ws.update() 를
불러 쓰기 API 169콜이 됐다. Google Sheets 한도는 분당 60회라 구조적 초과였고,
gs_retry 4회 백오프로도 3건이 최종 실패했다. 쓰기가 실패하면 Last_Checked 도
안 써져 그 행이 far 티어 30일 주기에 다시 갇힌다.

가장 위험한 버그: 병합이 행을 **잘못된 위치**에 쓰는 것. 한 칸만 밀려도
전 종목 데이터가 뒤섞인다. [2][3] 이 그것을 잡는다.
"""
import ast
import sys

SRC = "run_earnings_watch.py"
fail = []


def chk(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        fail.append(msg)


src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)
WANT = {"_col_a1", "_safe_update", "_merge_runs", "_batch_update"}
mod = ast.Module(body=[n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name in WANT],
                 type_ignores=[])
ns = {}
import gs_retry as gsr                                    # noqa: E402
gsr.GS_BACKOFF_BASE, gsr.GS_BACKOFF_CAP = 0.01, 0.02
ns["gsr"] = gsr
exec(compile(mod, "<extract>", "exec"), ns)
_merge_runs, _batch_update = ns["_merge_runs"], ns["_batch_update"]

print("[1] 추출")
chk(WANT <= set(ns), f"헬퍼 {len(WANT)}개 추출")

print("\n[2] 연속 행 병합 — 169행 → 1구간")
u = [(i, [f"v{i}"]) for i in range(2, 171)]
runs = _merge_runs(u)
chk(len(runs) == 1, f"{len(runs)}구간")
chk(runs[0][0] == 2 and len(runs[0][1]) == 169, f"시작 {runs[0][0]} 길이 {len(runs[0][1])}")
chk([r[0] for r in runs[0][1]] == [f"v{i}" for i in range(2, 171)],
    "행 내용이 행번호 순서 그대로 (밀림 없음)")

print("\n[3] 비연속·역순 입력")
u2 = [(50, ["z"]), (10, ["j"]), (2, ["a"]), (11, ["k"]), (3, ["b"]), (4, ["c"])]
runs2 = _merge_runs(u2)
chk([(r, len(v)) for r, v in runs2] == [(2, 3), (10, 2), (50, 1)],
    f"구간 = {[(r, len(v)) for r, v in runs2]}")
chk(runs2[0][1] == [["a"], ["b"], ["c"]], "정렬 후 내용 일치")
chk(runs2[1][1] == [["j"], ["k"]], "두 번째 구간 내용 일치")


class WS:
    def __init__(self):
        self.bodies, self.updates = [], []

    def batch_update(self, body, value_input_option=None):
        self.bodies.append(body)
        return {"ok": True}

    def update(self, values, range_name=None, value_input_option=None):
        self.updates.append(range_name)


print("\n[4] batch_update — 행 수와 무관하게 API 1콜")
ws = WS()
chk(_batch_update(ws, u, 21, label="cal") == 1, "169행 → API 1콜")
ws2 = WS()
chk(_batch_update(ws2, u2, 21, label="cal") == 1, "비연속 6행 → API 1콜")
chk(not ws.updates and not ws2.updates, "개별 update() 미호출")

print("\n[5] range 문자열 정확성 (한 칸 밀리면 전 종목 오염)")
b = ws2.bodies[0]
chk([x["range"] for x in b] == ["A2:U4", "A10:U11", "A50:U50"],
    f"range = {[x['range'] for x in b]}")
chk(b[0]["values"] == [["a"], ["b"], ["c"]], "range 와 values 대응")
chk(sum(len(x["values"]) for x in b) == len(u2), "총 행 수 보존")

print("\n[6] 21열/30열 경계 (CALENDAR_NCOL / EVENTS_NCOL)")
ws3 = WS()
_batch_update(ws3, [(5, ["x"])], 30, label="ev")
chk(ws3.bodies[0][0]["range"] == "A5:AD5", f"30열 → {ws3.bodies[0][0]['range']}")

print("\n[7] batch 실패 → 구간별 폴백 (데이터 보존)")


class WSfail(WS):
    def batch_update(self, body, value_input_option=None):
        raise RuntimeError("simulated 500")


wf = WSfail()
chk(_batch_update(wf, u2, 21, label="cal") == 3, f"폴백 {len(wf.updates)}구간 기록")
chk(wf.updates == ["A2:U4", "A10:U11", "A50:U50"], f"폴백 range = {wf.updates}")

print("\n[8] batch_update 미지원 gspread → 재시도 낭비 없이 즉시 폴백")


class WSold:
    def __init__(self):
        self.updates = []

    def update(self, values, range_name=None, value_input_option=None):
        self.updates.append(range_name)


import time                                               # noqa: E402
wo = WSold()
t0 = time.perf_counter()
n = _batch_update(wo, u2, 21, label="cal")
el = time.perf_counter() - t0
chk(n == 3 and el < 0.5, f"즉시 폴백 {n}구간, {el:.3f}초 (백오프 낭비 없음)")

print("\n[9] 빈 입력")
chk(_batch_update(WS(), [], 21) == 0, "빈 목록 → 0콜, 예외 없음")

print("\n[10] main 이 배치 경로를 쓰는지")
chk("_batch_update(cws, c_updates" in src, "캘린더 갱신이 배치 경로")
chk("_batch_update(ews, _ev_updates" in src, "이벤트 갱신이 배치 경로")
chk("_safe_update(cws, [vals], row_i" not in src, "행 단위 캘린더 쓰기 루프 제거됨")
chk("정밀 {n_full}×2콜" in src, "정밀 조회 콜 수 로그 정정(3→2)")

print("\n" + "=" * 52)
print(f"❌ 실패 {len(fail)}건" if fail else "✅ 전부 통과")
sys.exit(1 if fail else 0)
