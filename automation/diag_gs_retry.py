"""diag_gs_retry.py — Sheets 재시도 SSOT 통합 가드 (2026-09-04)

무엇을 지키는가
──────────────
2026-09-04 이전, Google Sheets 재시도 로직이 **세 벌** 있었다.

  1) gs_retry.call                        — 5시도 · 1.5→3→6→12초 · 예산 ~22초
  2) run_signal_backtest._gs              — 6시도 · 2→4→8→16→32초 · 예산 ~62초
  3) diag_satellite_backtest._gs          — 2) 와 거의 동일한 복사본

2)·3) 을 지우고 1) 로 기계적으로 치환하면 **재시도 예산이 62 → 22초로 조용히
줄어든다.** 그 62초는 우연이 아니다 — 두 백테스트는 몇 시간치 FMP 수집이
끝난 맨 마지막에 시트에 쓰고, 그 한 번의 실패가 런 전체를 무효로 만든다.

그래서 구현만 합치고 정책은 남겼다(gs_retry.PROFILE_BATCH). 이 진단은
**합치는 과정에서 조용히 바뀔 수 있는 것들**을 초 단위로 못 박는다:

  G1  기본 경로(_profile=None)가 통합 이전과 **완전히 동일한** 대기열을 낸다
      → 기존 소비자 7개(run_earnings_watch · refresh_market_calendar ·
        refresh_industry_perf · backfill_insider_stats · diag_earnings_batchwrite
        · seed_reminders · run_scanner_scan 계열)의 동작이 한 톨도 안 바뀐다
  G2  PROFILE_BATCH 가 옛 `_gs` 의 값을 그대로 갖는다 (2/4/8/16/32 · 지터 25% · 6시도)
  G3  두 예산이 설계대로 갈라져 있다 (22초 ≠ 62초)
  G4  _retries 명시가 프로파일보다 우선한다
  G5  WorksheetNotFound / SpreadsheetNotFound 는 즉시 raise (정상 흐름 — 호출부가 생성)
  G6  4xx 는 재시도하지 않는다
  G7  429 · 5xx 는 재시도한다
  G8  HTTP 상태를 못 읽는 예외를 **재시도한다** ← 통합하며 의도적으로 바꾼 유일한 항목
  G9  성공 경로는 재시도 0회이고 통계가 맞는다
  G10 두 백테스트 파일에 중복 구현이 되살아나지 않았다 (래칫)
  G11 두 백테스트 파일의 `_gs` 가 실제로 gs_retry 로 위임한다
  M   위 검사들이 결함을 실제로 잡는지 뮤테이션으로 역검증

왜 상수를 하드코딩하는가
────────────────────────
G1/G2 는 gs_retry 의 상수를 읽어와 자기 자신과 비교하지 **않는다.** 그러면
누가 1.5 를 3.0 으로 바꿔도 검사는 통과한다(자기참조 테스트의 고전적 실패).
기대값을 이 파일에 직접 박아두고, 값이 바뀌면 사람이 이 파일도 같이 고치게 한다.

안전성
──────
· 네트워크 없음 · 시트 접근 없음 · 실제 sleep 없음(전부 몽키패치로 가로챈다)
· 부작용 없다. 몇 번을 돌려도 같은 결과.

실행:  python3 automation/diag_gs_retry.py
"""
import ast
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 실행 환경에 값이 남아 있으면 기본값 검사가 오염된다.
os.environ.pop("GS_MAX_RETRIES", None)

import gs_retry as gsr  # noqa: E402

# ── 락스텝 관문 ──────────────────────────────────────────────────────────────
# gs_retry.py 를 같이 올리지 않으면 아래 검사 전체가 AttributeError 로 튄다.
# 트레이스백보다 원인을 말해주는 편이 낫다. 이 관문이 곧 배포 누락 탐지기다.
for _need in ("PROFILE_BATCH", "BATCH_BACKOFF", "RetryProfile", "_wait_for"):
    if not hasattr(gsr, _need):
        print("=" * 76)
        print(f"❌ 락스텝 실패 — gs_retry.{_need} 가 없다.")
        print("   gs_retry.py(레포 루트)가 2026-09-04 판으로 올라가지 않았다.")
        print("   run_signal_backtest.py · diag_satellite_backtest.py 도")
        print("   같은 커밋에서 함께 올라가야 한다.")
        print("=" * 76)
        sys.exit(1)

_fails, _passes = [], 0


def check(label, got, want):
    global _passes
    if got == want:
        _passes += 1
        print("  ✅ " + label)
        return True
    _fails.append(f"{label}  (got={got!r} want={want!r})")
    print(f"  ❌ {label}  got={got!r} want={want!r}")
    return False


# ══════════════════════════════════════════════════════════════════════════
# 기대값 — 여기에 **직접** 박는다 (자기참조 금지)
# ══════════════════════════════════════════════════════════════════════════
EXPECT_DEFAULT_RETRIES = 4          # 시도 5회
EXPECT_DEFAULT_BASE = 1.5           # 초
EXPECT_DEFAULT_CAP = 20.0           # 초
EXPECT_DEFAULT_SCHEDULE = [1.5, 3.0, 6.0, 12.0]      # 지터 제외

EXPECT_BATCH_RETRIES = 5            # 시도 6회
EXPECT_BATCH_SCHEDULE = [2.0, 4.0, 8.0, 16.0, 32.0]  # 지터 제외
EXPECT_BATCH_JITTER_FRAC = 0.25
EXPECT_BATCH_FULL = (2, 4, 8, 16, 32, 60)


class _FakeAPIError(Exception):
    """gspread APIError 모사 — 메시지에 '[503]' 형태로 상태를 담는다."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"[{code}] synthetic")


class _Opaque(Exception):
    """HTTP 상태를 읽을 수 없는 예외 (ssl.SSLError · TransportError 계열 모사)."""


def _capture(profile=None, retries=None, exc=None, no_jitter=True, seed=11):
    """재시도 대기열을 초 단위로 가로챈다. 실제 sleep 은 일어나지 않는다.

    Returns: (waits, raised_type | None)
    """
    waits = []
    orig_sleep, orig_random = gsr._time.sleep, gsr._random
    gsr._time.sleep = waits.append
    if no_jitter:
        class _Z:
            @staticmethod
            def uniform(a, b):
                return 0.0
        gsr._random = _Z
    else:
        r = random.Random(seed)
        gsr._random = r
    try:
        def boom():
            raise (exc or _FakeAPIError(503))
        try:
            gsr.call(boom, _profile=profile, _retries=retries)
            raised = None
        except Exception as e:                     # noqa: BLE001
            raised = type(e)
    finally:
        gsr._time.sleep, gsr._random = orig_sleep, orig_random
    return [round(float(w), 4) for w in waits], raised


def _legacy_schedule(n, base, cap):
    """2026-09-04 이전 gs_retry.call 의 대기 공식 (지터 제외) — 여기 복제해 둔다."""
    return [round(min(base * (2 ** a), cap), 4) for a in range(n)]


# ══════════════════════════════════════════════════════════════════════════
# G1 — 기본 경로가 통합 이전과 동일한가
# ══════════════════════════════════════════════════════════════════════════
print("=" * 76)
print("G) 재시도 정책 — 기본 경로 무변경 + 배치 프로파일 보존")
print("=" * 76)

check("G1a 기본 재시도 횟수가 4 (시도 5회)", gsr.GS_MAX_RETRIES, EXPECT_DEFAULT_RETRIES)
check("G1b 기본 백오프 base=1.5 · cap=20.0",
      (gsr.GS_BACKOFF_BASE, gsr.GS_BACKOFF_CAP), (EXPECT_DEFAULT_BASE, EXPECT_DEFAULT_CAP))

_w_def, _ = _capture(profile=None)
check("G1c 기본 대기열이 통합 이전 공식과 동일", _w_def, EXPECT_DEFAULT_SCHEDULE)
check("G1d 기본 대기열이 재계산한 legacy 공식과도 동일",
      _w_def, _legacy_schedule(EXPECT_DEFAULT_RETRIES, EXPECT_DEFAULT_BASE, EXPECT_DEFAULT_CAP))

_w_jit, _ = _capture(profile=None, no_jitter=False)
check("G1e 기본 지터는 균등 0~1.0초 (대기 비례 아님)",
      all(b <= w <= b + 1.0 for b, w in zip(EXPECT_DEFAULT_SCHEDULE, _w_jit))
      and _w_jit != EXPECT_DEFAULT_SCHEDULE, True)

# ══════════════════════════════════════════════════════════════════════════
# G2 — 배치 프로파일이 옛 _gs 값을 그대로 갖는가
# ══════════════════════════════════════════════════════════════════════════
check("G2a BATCH_BACKOFF 가 (2,4,8,16,32,60)", tuple(gsr.BATCH_BACKOFF), EXPECT_BATCH_FULL)
check("G2b PROFILE_BATCH.retries = 5 (시도 6회)", gsr.PROFILE_BATCH.retries, EXPECT_BATCH_RETRIES)
check("G2c PROFILE_BATCH.jitter_frac = 0.25", gsr.PROFILE_BATCH.jitter_frac, EXPECT_BATCH_JITTER_FRAC)

_w_bat, _ = _capture(profile=gsr.PROFILE_BATCH)
check("G2d BATCH 대기열이 옛 _gs 스케줄과 동일", _w_bat, EXPECT_BATCH_SCHEDULE)

_w_bj, _ = _capture(profile=gsr.PROFILE_BATCH, no_jitter=False)
check("G2e BATCH 지터는 대기의 0~25% (비례)",
      all(b <= w <= b * 1.25 for b, w in zip(EXPECT_BATCH_SCHEDULE, _w_bj))
      and _w_bj != EXPECT_BATCH_SCHEDULE, True)

# ══════════════════════════════════════════════════════════════════════════
# G3 — 두 예산이 설계대로 갈라져 있는가
# ══════════════════════════════════════════════════════════════════════════
_budget_def, _budget_bat = sum(EXPECT_DEFAULT_SCHEDULE), sum(EXPECT_BATCH_SCHEDULE)
check(f"G3a 기본 예산 {_budget_def:.0f}초 · 배치 예산 {_budget_bat:.0f}초 — 다르다",
      _budget_bat > _budget_def * 2.5, True)
check("G3b 실측 예산이 기대와 일치", (sum(_w_def), sum(_w_bat)), (_budget_def, _budget_bat))

# ══════════════════════════════════════════════════════════════════════════
# G4 — _retries 우선순위
# ══════════════════════════════════════════════════════════════════════════
_w_ov, _ = _capture(profile=gsr.PROFILE_BATCH, retries=1)
check("G4a _retries=1 이 프로파일(5)보다 우선", len(_w_ov), 1)
check("G4b 그래도 스케줄은 프로파일 것을 쓴다", _w_ov, EXPECT_BATCH_SCHEDULE[:1])
check("G4c _retries=0 이면 재시도 없음", _capture(profile=gsr.PROFILE_BATCH, retries=0)[0], [])

# ══════════════════════════════════════════════════════════════════════════
# G5~G8 — 예외 분류
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("G) 예외 분류 — 무엇을 재시도하고 무엇을 즉시 포기하는가")
print("=" * 76)

# ⚠️ 클래스 **이름**으로 비교하면 안 된다. gspread 가 설치된 환경(자동화)에서는
#    진짜 WorksheetNotFound 가, 없는 환경(로컬 문법검사)에서는 gs_retry 내부
#    스텁이 쓰여 이름이 달라진다. 검증하려는 것은 이름이 아니라
#    "대기 0회 + 그 예외가 그대로 올라옴" 이다.
_w, _r = _capture(profile=gsr.PROFILE_BATCH, exc=gsr._WorksheetNotFound("nope"))
check("G5a WorksheetNotFound 는 대기 없이 즉시 raise",
      (_w, _r is gsr._WorksheetNotFound), ([], True))
_w, _r = _capture(profile=gsr.PROFILE_BATCH, exc=gsr._SpreadsheetNotFound("nope"))
check("G5b SpreadsheetNotFound 도 즉시 raise",
      (_w, _r is gsr._SpreadsheetNotFound), ([], True))
check("G5c not_found 통계가 잡힌다", gsr.stats()["not_found"] >= 2, True)
gsr.reset_stats()

for _code in (400, 401, 403, 404):
    check(f"G6  {_code} 는 재시도하지 않는다",
          _capture(profile=gsr.PROFILE_BATCH, exc=_FakeAPIError(_code))[0], [])

for _code in (429, 500, 502, 503, 504):
    check(f"G7  {_code} 는 5회 재시도한다",
          len(_capture(profile=gsr.PROFILE_BATCH, exc=_FakeAPIError(_code))[0]),
          EXPECT_BATCH_RETRIES)

# ⚠️ 통합하며 **의도적으로 바꾼 유일한 항목.** 이전 백테스트의 `_gs_is_transient`
#    는 `code is None → False` 라서 상태를 못 읽는 예외에 재시도하지 않고 죽었다.
#    google.auth.exceptions.TransportError · ssl.SSLError ·
#    http.client.RemoteDisconnected 가 정확히 그 부류다.
check("G8  상태를 못 읽는 예외는 **재시도한다** (통합 시 의도적 변경)",
      len(_capture(profile=gsr.PROFILE_BATCH, exc=_Opaque("ssl handshake failed"))[0]),
      EXPECT_BATCH_RETRIES)
check("G8b _retryable 이 상태 미판독을 True 로 본다", gsr._retryable(_Opaque("x")), True)
check("G8c _retryable 이 404 를 False 로 본다", gsr._retryable(_FakeAPIError(404)), False)

# ══════════════════════════════════════════════════════════════════════════
# G9 — 성공 경로
# ══════════════════════════════════════════════════════════════════════════
gsr.reset_stats()
check("G9a 성공은 값을 그대로 돌려준다", gsr.call(lambda x: x * 2, 21), 42)
_s = gsr.stats()
check("G9b 성공 1건 · 재시도 0", (_s["ok"], _s["retries"]), (1, 0))
gsr.reset_stats()

# 재시도 후 회복
_state = {"n": 0}


def _flaky():
    _state["n"] += 1
    if _state["n"] < 3:
        raise _FakeAPIError(503)
    return "recovered"


_orig_sleep = gsr._time.sleep
gsr._time.sleep = lambda w: None
try:
    _out = gsr.call(_flaky, _profile=gsr.PROFILE_BATCH)
finally:
    gsr._time.sleep = _orig_sleep
_s = gsr.stats()
check("G9c 2회 재시도 후 회복", (_out, _s["retries"], _s["recovered"]), ("recovered", 2, 1))
gsr.reset_stats()

# ══════════════════════════════════════════════════════════════════════════
# G10/G11 — 중복 재발 래칫 (정적 분석)
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("G) 중복 재발 래칫 — 백테스트 2개가 정말 위임하고 있는가")
print("=" * 76)

_TARGETS = ("run_signal_backtest.py", "diag_satellite_backtest.py")
# 되살아나면 안 되는 이름들. **AST 로만** 본다 — 주석·독스트링에 등장하는 것은
# 설명이지 구현이 아니다(문자열 검색으로 하면 이 파일의 설명 주석까지 잡힌다).
_FORBIDDEN = ("_gs_is_transient", "_GS_MAX_ATTEMPTS", "_GS_BACKOFF", "_GS_RETRY_STATUS")


def _find(fname):
    for d in (_HERE, _ROOT):
        p = os.path.join(d, fname)
        if os.path.isfile(p):
            return p
    return None


def _toplevel_defs(tree):
    """모듈 최상위에 바인딩된 이름 (함수·클래스·대입)."""
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
    return out


def _imports_gs_retry(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            if any(a.name == "gs_retry" for a in n.names):
                return True
        elif isinstance(n, ast.ImportFrom) and n.module == "gs_retry":
            return True
    return False


def _gs_delegates(tree):
    """`def _gs(...)` 의 본문이 gs_retry 로 위임하는가 (별칭 무관)."""
    aliases = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "gs_retry":
                    aliases.add(a.asname or a.name)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_gs":
            for sub in ast.walk(n):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "call"
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id in aliases):
                    return True
            return False
    return False


def _profile_is_batch(tree):
    """`_gs` 가 PROFILE_BATCH 를 넘기는가 — 기본 프로파일로 조용히 강등되면 잡는다."""
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_gs":
            for sub in ast.walk(n):
                if isinstance(sub, ast.Call):
                    for kw in sub.keywords:
                        if (kw.arg == "_profile" and isinstance(kw.value, ast.Attribute)
                                and kw.value.attr == "PROFILE_BATCH"):
                            return True
            return False
    return False


for _fn in _TARGETS:
    _path = _find(_fn)
    if _path is None:
        check(f"G10 {_fn} 을 찾았다", False, True)
        continue
    _tree = ast.parse(open(_path, encoding="utf-8").read(), filename=_fn)
    _defs = _toplevel_defs(_tree)
    _leftover = sorted(n for n in _FORBIDDEN if n in _defs)
    check(f"G10a {_fn} 에 중복 구현이 없다", _leftover, [])
    check(f"G10b {_fn} 에 미사용 `import random` 이 없다",
          any(isinstance(n, ast.Import) and any(a.name == "random" for a in n.names)
              for n in _tree.body), False)
    check(f"G11a {_fn} 이 gs_retry 를 import 한다", _imports_gs_retry(_tree), True)
    check(f"G11b {_fn} 의 _gs 가 gs_retry.call 로 위임한다", _gs_delegates(_tree), True)
    check(f"G11c {_fn} 의 _gs 가 PROFILE_BATCH 를 넘긴다", _profile_is_batch(_tree), True)

# ══════════════════════════════════════════════════════════════════════════
# M — 뮤테이션 역검증
# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("M) 뮤테이션 역검증 — 위 검사가 결함을 실제로 잡는가")
print("=" * 76)


def _mutate(label, apply_fn, restore_fn, probe_fn):
    """apply → probe 가 뒤집히는지 확인 → 반드시 restore."""
    try:
        apply_fn()
        flipped = not probe_fn()
    finally:
        restore_fn()
    check(f"M  {label}", flipped, True)


# M1 — 기본 백오프 base 를 바꾸면 G1 이 잡아야 한다
_orig_base = gsr.GS_BACKOFF_BASE
_mutate("기본 base 1.5 → 3.0 을 G1 이 잡는다",
        lambda: setattr(gsr, "GS_BACKOFF_BASE", 3.0),
        lambda: setattr(gsr, "GS_BACKOFF_BASE", _orig_base),
        lambda: _capture(profile=None)[0] == EXPECT_DEFAULT_SCHEDULE)

# M2 — 배치 스케줄을 한 칸 바꾸면 G2 가 잡아야 한다
_orig_prof = gsr.PROFILE_BATCH
_mutate("BATCH 스케줄 32 → 8 을 G2 가 잡는다",
        lambda: setattr(gsr, "PROFILE_BATCH",
                        _orig_prof._replace(backoff=(2, 4, 8, 16, 8, 60))),
        lambda: setattr(gsr, "PROFILE_BATCH", _orig_prof),
        lambda: _capture(profile=gsr.PROFILE_BATCH)[0] == EXPECT_BATCH_SCHEDULE)

# M3 — 배치 재시도 횟수를 줄이면 G2b/G7 이 잡아야 한다
_mutate("BATCH retries 5 → 2 를 G7 이 잡는다",
        lambda: setattr(gsr, "PROFILE_BATCH", _orig_prof._replace(retries=2)),
        lambda: setattr(gsr, "PROFILE_BATCH", _orig_prof),
        lambda: len(_capture(profile=gsr.PROFILE_BATCH,
                             exc=_FakeAPIError(503))[0]) == EXPECT_BATCH_RETRIES)

# M4 — 상태 미판독을 재시도 안 함으로 되돌리면(= 통합 전 백테스트 버그) G8 이 잡아야 한다
_orig_retryable = gsr._retryable
_mutate("상태 미판독 → 재시도 안 함(옛 버그 복원)을 G8 이 잡는다",
        lambda: setattr(gsr, "_retryable",
                        lambda exc: (gsr._status_of(exc) or 0) in gsr.RETRYABLE_STATUS),
        lambda: setattr(gsr, "_retryable", _orig_retryable),
        lambda: len(_capture(profile=gsr.PROFILE_BATCH,
                             exc=_Opaque("x"))[0]) == EXPECT_BATCH_RETRIES)

# M5 — 프로파일을 무시하고 기본으로 강등하면 G2d 가 잡아야 한다
#      (안 A '기계적 치환'이 정확히 이 모양이다 — 예산이 62 → 22초로 준다)
_orig_call = gsr.call
_mutate("프로파일 무시(기계적 치환 시나리오)를 G2 가 잡는다",
        lambda: setattr(gsr, "call",
                        lambda fn, *a, _profile=None, **k: _orig_call(fn, *a, **k)),
        lambda: setattr(gsr, "call", _orig_call),
        lambda: _capture(profile=gsr.PROFILE_BATCH)[0] == EXPECT_BATCH_SCHEDULE)

# M6 — 래칫 역검증: 금지 이름이 최상위에 정의된 소스를 넣으면 G10a 가 잡아야 한다
_bad = ast.parse("_GS_MAX_ATTEMPTS = 6\n\n\ndef _gs_is_transient(e):\n    return True\n")
check("M  G10a 가 중복 구현 부활을 잡는다",
      sorted(n for n in _FORBIDDEN if n in _toplevel_defs(_bad)),
      ["_GS_MAX_ATTEMPTS", "_gs_is_transient"])
# 주석/독스트링에만 등장하는 경우는 오탐하지 않아야 한다 (이 파일 자체가 그 예다)
_okdoc = ast.parse('"""_gs_is_transient 를 지웠다"""\n# _GS_BACKOFF 도 지웠다\nX = 1\n')
check("M  G10a 가 주석·독스트링 언급을 오탐하지 않는다",
      sorted(n for n in _FORBIDDEN if n in _toplevel_defs(_okdoc)), [])

# 복원 확인 — 뮤테이션이 새어나가면 이후 실행이 조용히 오염된다
print()
check("M  복원 확인 — 기본 대기열", _capture(profile=None)[0], EXPECT_DEFAULT_SCHEDULE)
check("M  복원 확인 — BATCH 대기열", _capture(profile=gsr.PROFILE_BATCH)[0], EXPECT_BATCH_SCHEDULE)
check("M  복원 확인 — _retryable", gsr._retryable(_Opaque("x")), True)

# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
if _fails:
    print(f"❌ 실패 {len(_fails)}건 / 통과 {_passes}건")
    for f in _fails:
        print("   · " + f)
    print("=" * 76)
    sys.exit(1)
print(f"✅ 전부 통과 — {_passes}건")
print("=" * 76)
sys.exit(0)
