"""check_freshness.py — 프로젝트 사본 신선도 지문 + 모듈 간 정합성 검사.

왜 필요한가
───────────
프로젝트 폴더 사본이 GitHub 배포본보다 뒤처져 있는 채로 편집을 시작해 회귀가
난 적이 있다(~300줄 손실). Claude 는 비공개 저장소를 직접 읽을 수 없으므로
"최신인지"를 스스로 판정하지 못한다. 대신 세 가지를 한다:

  1) 지문 표 — 사용자가 GitHub 과 대조할 수 있는 형태로 출력
  2) 분할 배치 드리프트 — 루트와 automation/ 에 같은 파일이 다른 줄 수로
     있으면 락스텝이 깨진 것이다
  3) 모듈 간 정합성 — 사본끼리 앞뒤가 안 맞으면(A 가 부르는 심볼이 B 에 없음)
     **버전이 섞였다는 사실이 자동으로 드러난다.**

2026-09-04 개편 — 기반 및 무엇이 뚫려 있었나
────────────────────────────────────────────
기반은 배포본 203줄이다. 프로젝트 사본(187줄)은 16줄 낡아 있었고, 그걸 기준으로
덮어썼다면 `rotation_core`·`run_hidden_alpha` 마커, `CROSS_TARGETS` 의
`rotation_core`, `path()` 의 automation/ 폴백이 전부 소실됐다. 마커는 배포본을
**덮지 않고 합집합**으로 병합했다(배포본 마커 38개 → 95개, 소실 0).
① **커버리지.** 지문 표가 하드코딩 15개 파일만 출력했다. 사본 82개 중 67개가
   표에 없어 **줄 수 대조 자체가 불가능**했다(`run_signal_backtest.py` 1597,
   `diag_fmp_ssot.py` 1429, `run_hidden_alpha.py` 1036, `calendar_core.py` 546,
   그리고 이 파일 자신까지). 이제 디렉터리를 스캔한다 — 파일이 표에서 빠지는
   일은 구조적으로 불가능하다.

②-0 **자기 자신 부재.** 이 파일이 지문 표에 자기를 안 넣어, 정작 이 파일이
   낡았다는 사실을 이 파일이 못 알렸다. 위 16줄 격차가 그렇게 숨어 있었다.

② **마커 표류.** `run_earnings_watch.py` 의 마커 6개가 전부 창 이행·캘린더
   이행 **이전** 심볼이었다. 실제로 37줄 낡은 사본이 6/6 초록으로 통과했다.
   마커는 "그 시점 이후 버전"을 증명해야 하므로 **가장 최근 이행 심볼**로
   갱신했다. 이건 자동화가 안 된다 — 이행할 때마다 손으로 갱신해야 한다.

③ **정합성 검사 대상.** 자동화 루프가 `run_earnings_watch` ·
   `run_watchlist_alerts` 둘뿐이었다. 이제 `import` 로 공용 모듈을 끌어쓰는
   **모든 파일**을 검사한다(사본 기준 51개).

남아 있는 한계 — 알고 두는 것
─────────────────────────────
정합성 검사는 **방향이 하나뿐**이다. "소비자가 부르는 심볼이 모듈에 없다"만
잡는다. 소비자가 낡아서 **새 SSOT 심볼을 아직 안 부르는** 경우는 원리상 못
잡는다(run_earnings_watch 가 정확히 그 케이스였다). 그 방향은 마커(②)와
Actions 의 diag 스위트(`diag_market_calendar` I군, `diag_hist_window_consumers`)
가 맡는다. 여기에 같은 규칙을 복제하지 않는다 — 소유권이 갈리면 표류한다.

사용:
    python3 check_freshness.py [프로젝트경로]
    python3 check_freshness.py [프로젝트경로] regime_core diag_fmp_ssot
        ← 인자를 더 주면 지문 표만 그 이름들로 걸러 출력한다(정합성은 항상 전체).
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/mnt/project"
FILTERS = [a for a in sys.argv[2:] if a.strip()]

# ── 마커 ─────────────────────────────────────────────────────────────────────
# 규칙: **그 파일의 가장 최근 이행/기능 심볼**을 넣는다. 오래된 심볼만 넣으면
# 낡은 사본이 초록으로 통과한다(2026-09-02 실제 사고). 이행을 끝낼 때 여기도
# 같이 갱신하는 것이 이행 작업의 일부다.
# 선언이 없는 파일은 "-" 로 나오며 **줄 수만으로 대조**해야 한다.
MARKERS = {
    # 코어
    "app.py": ["_SSOT_NEEDS", "load_earnings_universe", "TIMING_LABELS_INFERRED",
               "_open_quant_db", "update_watchlist_row", "시장 이벤트 지형",
               "cached_hidden_alpha_gates", "import rotation_core",
               "import calendar_core", "_shared_owner_uid"],
    "earnings_core.py": ["infer_timing", "fetch_market_calendar_map", "SOURCE_UNIVERSE",
                         "UNIVERSE_WORKSHEET", "_timing_from_utc", "TIMING_LABELS_INFERRED"],
    "fmp_http.py": ["fmp_get_json_ex", "plan_limited", "set_key_provider"],
    "gs_retry.py": ["_retryable", "GS_MAX_RETRIES"],
    # 창 수학 SSOT — 이 셋이 없으면 limit= 이전 버전이다
    "fmp_extras.py": ["import fmp_http", "fmp_stats_line",
                      "hist_range_params", "hist_days_for_bars", "HIST_MAX_DAYS"],
    # 슬롯 교체 A단계 이후
    "regime_core.py": ["_market_warnings", "ALERT_CONFIRM_DAYS",
                       "replacement_hurdle", "rank_weakest", "is_weak_status"],
    "rotation_core.py": ["apply_rotation_gates", "dedup_by_correlation",
                         "select_top_slots", "CRYPTO_SLOT_CAP", "REQUIRED_BARS",
                         "promote_by_tradability", "TIE_ADV_MARGIN"],
    # calendar_core v1.1.0 (반장 지원)
    "calendar_core.py": ["nyse_early_close_days", "session_close_time",
                         "is_early_close", "close_minutes", "is_market_open_today"],
    "accounts_core.py": ["Trim_Size_Show"],
    "users_core.py": ["Gate_Market"],
    "watchlist_metrics_core.py": ["completed_bars_only"],
    "narrative_core.py": ["build_narrative_prompt", "parse_narrative_json"],
    "gemini_core.py": ["thinking_budget"],
    # 배포본에 빈 목록([])으로만 있던 항목들 — 실제 심볼로 채운다.
    # 빈 목록은 "-" 로 표시돼 줄 수 대조 외에는 아무것도 증명하지 못했다.
    "scanner_core.py": ["SCANNER_SCHEMA_VERSION", "SCANNER_CUTOFF",
                        "WATCHLIST_AUTO_ADD_THRESHOLDS", "set_fmp_key_provider"],
    "portfolio_core.py": ["PORTFOLIOS_SHEET_COLS", "holdings_by_user", "held_tickers"],
    "industry_core.py": ["INDUSTRY_CORE_VERSION", "rank_percentile", "is_stable"],
    "reminders_core.py": ["REMINDERS_WORKSHEET", "effective_due", "active_reminders"],

    # 자동화 러너 — 창 이행 + 캘린더 이행 심볼이 핵심이다
    "run_earnings_watch.py": ["pass_universe", "_batch_update", "_merge_runs",
                              "FORCE_CALENDAR", "FORCE_UNIVERSE", "gsr.call",
                              "hist_range_params", "cc.is_market_open_today"],
    # 이 파일은 hist_range_params 가 아니라 보유기간 계열을 쓴다(정상)
    "run_watchlist_alerts.py": ["cc.is_market_open_today", "fmp_get_ex",
                                "hist_days_for_holding"],
    "run_hidden_alpha.py": ["verify_and_gate", "_fmp_batch_ohlcv_df",
                            "import rotation_core", "hist_range_params",
                            "email_table_rows", "_VERIFY_TOP_N", "rc.REQUIRED_BARS"],
    "run_signal_backtest.py": ["HISTORY_BARS", "hist_days_for_bars",
                               "fmp_get_ex", "MIN_PRIOR_BARS"],
    "run_narrative.py": ["build_narrative_prompt", "cc.is_market_open_today",
                         "hist_range_params"],
    "run_drg_predict.py": ["cc.is_market_open_today", "hist_range_params"],
    "run_drg_verify.py": ["cc.is_market_open_today", "hist_range_params"],
    "refresh_market_calendar.py": ["Market_Calendar", "Adj_Close"],
    "refresh_industry_perf.py": ["industry_core", "Industry_Rank"],
}

# `별칭.심볼` 참조를 검사할 공용 모듈
CROSS_TARGETS = {
    "regime_core", "users_core", "narrative_core", "scanner_core", "fmp_extras",
    "portfolio_core", "watchlist_metrics_core", "earnings_core", "accounts_core",
    "calendar_core", "rotation_core", "industry_core", "reminders_core",
    "gemini_core", "fmp_http", "gs_retry",
}

# 정합성 불일치를 **실패(exit 1)** 로 볼 파일. 나머지는 경고만 낸다.
# 진단·일회성 스크립트는 스텁·합성 모듈을 다루므로 빨간불이 정상일 수 있고,
# 여기서 exit 1 이 나면 워크플로 후속 단계가 조용히 건너뛰어진다(실제 사고).
# 접두가 우선한다 — `diag_industry_core.py` 는 이름이 `_core.py` 로 끝나지만
# 진단이지 코어가 아니다. 이 순서를 뒤집으면 진단 파일이 실패 판정 대상이 된다.
def _is_tooling(name: str) -> bool:
    return name.startswith(("diag_", "check_"))


def is_hard(name: str) -> bool:
    if _is_tooling(name):
        return False
    return (name == "app.py"
            or name.endswith("_core.py")
            or name in ("fmp_http.py", "gs_retry.py", "fmp_extras.py")
            or name.startswith(("run_", "refresh_")))


def group_of(name: str) -> str:
    if _is_tooling(name):
        return "diag"
    if name == "app.py" or name.endswith("_core.py") or name in ("fmp_http.py",
                                                                 "gs_retry.py",
                                                                 "fmp_extras.py"):
        return "core"
    if name.startswith(("run_", "refresh_", "backfill_", "seed_")):
        return "runner"
    return "diag"


# ── AST 헬퍼 ────────────────────────────────────────────────────────────────
def _target_names(t) -> set:
    out = set()
    if isinstance(t, ast.Name):
        out.add(t.id)
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            out |= _target_names(e)
    elif isinstance(t, ast.Starred):
        out |= _target_names(t.value)
    return out


def top_level_names(src: str) -> set:
    """모듈 최상위에 정의/바인딩된 이름.

    튜플 대입(`A, B = 1, 2`)을 풀지 않으면 멀쩡한 모듈을 '심볼 없음'으로
    오탐한다(실제 발생). 최상위 `try:` / `if:` 안의 정의도 최상위 바인딩이므로
    한 겹 들어가서 걷는다 — 조건부 import 관용구가 여기 걸린다.
    """
    out = set()

    def walk_body(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(n.name)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    out.update(_target_names(t))
            elif isinstance(n, ast.AnnAssign):
                out.update(_target_names(n.target))
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    if a.name != "*":
                        out.add(a.asname or a.name.split(".")[0])
            elif isinstance(n, ast.If):
                walk_body(n.body)
                walk_body(n.orelse)
            elif isinstance(n, ast.Try):
                walk_body(n.body)
                walk_body(n.orelse)
                walk_body(n.finalbody)
                for h in n.handlers:
                    walk_body(h.body)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                walk_body(n.body)

    walk_body(ast.parse(src).body)
    return out


def _locally_bound(fn) -> set:
    """함수 안에서 지역으로 묶이는 이름.

    `with open(...) as fh:` 가 모듈 별칭 `fh`(fmp_http)를 가리는 실제 사례가
    있다(diag_hist_window.py:120). 이걸 처리 안 하면 그 함수 안의 `fh.read()`
    가 'fmp_http 에 read 없음' 오탐으로 뜬다.

    중첩 함수까지 함께 걷으므로 다소 과하게 수집한다 — 과수집은 검사를 건너뛰는
    쪽이라 오탐 대신 미탐이 된다. 섀도잉이 드물어 감수한다.
    """
    b = set()
    a = fn.args
    for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
        b.add(x.arg)
    if a.vararg:
        b.add(a.vararg.arg)
    if a.kwarg:
        b.add(a.kwarg.arg)
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                b |= _target_names(t)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            b |= _target_names(n.target)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            b |= _target_names(n.target)
        elif isinstance(n, ast.withitem):
            if n.optional_vars is not None:
                b |= _target_names(n.optional_vars)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            b.add(n.name)
        elif isinstance(n, ast.comprehension):
            b |= _target_names(n.target)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b.add(n.name)
    return b


def alias_map(tree) -> dict:
    al = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in CROSS_TARGETS:
                    al[a.asname or a.name] = a.name
    return al


def collect_uses(tree, al: dict) -> dict:
    """`별칭.속성` 사용처 수집. 섀도잉된 스코프와 던더는 뺀다."""
    uses: dict = {}

    def visit(node, shadowed: frozenset):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, shadowed | (_locally_bound(child) & set(al)))
                continue
            if (isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and child.value.id in al
                    and child.value.id not in shadowed
                    and not child.attr.startswith("__")):
                uses.setdefault(al[child.value.id], set()).add(child.attr)
            visit(child, shadowed)

    visit(tree, frozenset())
    return uses


def read(p):
    try:
        return open(p, encoding="utf-8").read()
    except Exception:
        return None


# ── 수집 ────────────────────────────────────────────────────────────────────
# 저장소에서 자동화 스크립트는 `automation/` 하위에 있고 프로젝트 사본은 평평하다.
# 양쪽 배치를 모두 걷지 않으면 한쪽에서 영구히 '사본 없음'이 뜬다(배포본 `path()`
# 폴백이 하던 일 — 여기서는 스캔 자체를 두 곳으로 넓혀 대체한다).
AUTO = os.path.join(ROOT, "automation")
SCAN = [("", ROOT)] + ([("automation/", AUTO)] if os.path.isdir(AUTO) else [])

files = {}      # 표시경로 → (파일명, 절대경로, 원문)
for prefix, d in SCAN:
    try:
        names = sorted(os.listdir(d))
    except Exception:
        continue
    for nm in names:
        if not nm.endswith(".py"):
            continue
        full = os.path.join(d, nm)
        src = read(full)
        if src is None:
            continue
        files[prefix + nm] = (nm, full, src)

print(f"프로젝트 경로: {ROOT}")
if len(SCAN) > 1:
    print("automation/ 하위 디렉터리 감지 — 함께 스캔합니다.")
print()
print("=" * 78)
print("1) 지문 — GitHub 과 대조하세요 (줄 수가 다르면 사본이 낡은 것)")
print("   줄 수 규약: count('\\n')+1 — GitHub 표시는 이보다 1 작습니다")
print("=" * 78)


def lines_of(src):
    return src.count("\n") + 1


def marker_tag(nm, src):
    marks = MARKERS.get(nm)
    if not marks:
        return "-", False
    have = [m for m in marks if m in src]
    tag = f"{len(have)}/{len(marks)}"
    if len(have) < len(marks):
        miss = ", ".join(m for m in marks if m not in src)
        return tag + "  ⚠ 누락: " + miss[:44], True
    return tag, False


shown = 0
marker_gaps = []
buckets = {"core": [], "runner": [], "diag": []}
for disp, (nm, full, src) in files.items():
    buckets[group_of(nm)].append((disp, nm, src))

TITLES = {"core": "코어 모듈 (app + *_core + http/retry/extras)",
          "runner": "자동화 러너 (run_ / refresh_ / backfill_ / seed_)",
          "diag": "진단 · 유틸"}

for g in ("core", "runner", "diag"):
    rows = buckets[g]
    if FILTERS:
        rows = [r for r in rows if any(f.lower() in r[0].lower() for f in FILTERS)]
    if not rows:
        continue
    print(f"\n── {TITLES[g]} ({len(rows)}개)")
    # 폭은 실제 이름 길이로 잡는다. 분할 배치에서는 `automation/` 접두가 붙어
    # 고정 폭이면 열이 어긋난다(실측: 최장 44자).
    w = max(len(r[0]) for r in rows) + 1
    if g == "diag":
        # 마커 선언이 있는 진단 파일만 전체 폭, 나머지는 2열로 압축.
        # 이름이 길면(분할 배치) 2열이 화면을 넘기므로 1열로 떨어뜨린다.
        wide = [r for r in rows if MARKERS.get(r[1])]
        slim = [r for r in rows if not MARKERS.get(r[1])]
        for disp, nm, src in wide:
            tag, gap = marker_tag(nm, src)
            if gap:
                marker_gaps.append(disp)
            print(f"  {disp:{w}} {lines_of(src):>6}  {tag}")
            shown += 1
        cols = 2 if w <= 36 else 1
        half = (len(slim) + cols - 1) // cols
        left, right = slim[:half], slim[half:]
        for i in range(half):
            line = f"  {left[i][0]:{w}} {lines_of(left[i][2]):>6}"
            if cols == 2 and i < len(right):
                line += f"   {right[i][0]:{w}} {lines_of(right[i][2]):>6}"
            print(line)
        shown += len(slim)
    else:
        for disp, nm, src in rows:
            tag, gap = marker_tag(nm, src)
            if gap:
                marker_gaps.append(disp)
            print(f"  {disp:{w}} {lines_of(src):>6}  {tag}")
            shown += 1

no_marker = sum(1 for _, (nm, _, _) in files.items() if not MARKERS.get(nm))
print(f"\n합계 {len(files)}개 파일" + (f" · 이 표에 {shown}개 표시(필터 적용)"
                                    if FILTERS else "")
      + f" · 마커 선언 {len(files) - no_marker}개 · 줄 수만 {no_marker}개")
if marker_gaps:
    print(f"⚠ 마커 누락 {len(marker_gaps)}건: {', '.join(marker_gaps)}")
    print("   → 그 기능이 들어가기 **이전** 사본일 가능성이 높습니다.")

orphan = [k for k in MARKERS if not any(nm == k for nm, _, _ in files.values())]
if orphan:
    print(f"⚠ 마커는 선언됐는데 사본이 없는 파일: {', '.join(orphan)}")

# ── 2) 분할 배치 드리프트 ───────────────────────────────────────────────────
drift = []
if len(SCAN) > 1:
    print("\n" + "=" * 78)
    print("2) 분할 배치 드리프트 — 루트와 automation/ 에 같은 파일이 있는가")
    print("=" * 78)
    root_names = {nm for d, (nm, _, _) in files.items() if not d.startswith("automation/")}
    both = [nm for d, (nm, _, _) in files.items()
            if d.startswith("automation/") and nm in root_names]
    if not both:
        print("  중복 없음.")
    for nm in sorted(set(both)):
        a = lines_of(files[nm][2])
        b = lines_of(files["automation/" + nm][2])
        if a == b:
            print(f"  ✅ {nm:30} 양쪽 {a}줄 — 일치")
        else:
            drift.append(f"{nm}: 루트 {a}줄 ≠ automation/ {b}줄")
            print(f"  ❌ {nm:30} 루트 {a} ≠ automation/ {b} — 락스텝 깨짐")

# ── 3) 모듈 간 정합성 ───────────────────────────────────────────────────────
sec = 3 if len(SCAN) > 1 else 2
print("\n" + "=" * 78)
print(f"{sec}) 모듈 간 정합성 — 여기서 실패하면 사본 버전이 섞인 것")
print("=" * 78)

_tops: dict = {}


def module_tops(mod):
    if mod not in _tops:
        src = None
        for prefix, _ in SCAN:
            key = prefix + mod + ".py"
            if key in files:
                src = files[key][2]
                break
        try:
            _tops[mod] = top_level_names(src) if src is not None else None
        except SyntaxError:
            _tops[mod] = None
    return _tops[mod]


problems, warnings, checked = [], [], 0
for disp in sorted(files):
    nm, full, src = files[disp]
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        (problems if is_hard(nm) else warnings).append(f"{disp}: 구문 오류 {e}")
        continue
    al = alias_map(tree)
    if not al:
        continue
    uses = collect_uses(tree, al)
    if not uses:
        continue
    checked += 1
    hard = is_hard(nm)
    bad = []
    for mod in sorted(uses):
        mt = module_tops(mod)
        if mt is None:
            bad.append(f"{mod}.py 사본 없음")
            continue
        miss = sorted(uses[mod] - mt)
        if miss:
            bad.append(f"{mod}: {len(miss)}개 없음 → {miss[:6]}")
    if bad:
        for b in bad:
            line = f"{disp} → {b}"
            (problems if hard else warnings).append(line)
            print(f"  {'❌' if hard else '⚠️ '} {line}")
    elif hard:
        tot = sum(len(v) for v in uses.values())
        print(f"  ✅ {disp:30} {len(uses)}개 모듈 · {tot}개 심볼 모두 존재")

print(f"\n  검사한 임포터 {checked}개 "
      f"(실패 판정 대상 {sum(1 for d in files if is_hard(files[d][0]))}개 중)")

# ── 판정 ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 78)
fail = problems + drift
if fail:
    print(f"❌ 정합성 문제 {len(fail)}건 — **사본 버전이 섞였을 가능성이 높습니다.**")
    print("   편집 전에 GitHub 현재 버전을 받으세요.")
    for p in fail:
        print(f"   · {p}")
else:
    print("✅ 사본끼리는 앞뒤가 맞습니다.")
    print("   다만 이것이 'GitHub 최신'을 보장하지는 않습니다 —")
    print("   전부 같은 시점의 낡은 버전이면 정합성은 통과합니다.")
    print("   위 지문 표의 줄 수를 GitHub 과 대조해 주세요.")
if warnings:
    print(f"\n⚠ 진단·일회성 스크립트 경고 {len(warnings)}건 (exit 코드에는 반영 안 함)")
    for w in warnings[:8]:
        print(f"   · {w}")
    if len(warnings) > 8:
        print(f"   · … 외 {len(warnings) - 8}건")
if marker_gaps:
    print(f"\n⚠ 마커 누락 {len(marker_gaps)}건 — 위 1) 참고. exit 코드에는 반영 안 함")
print("=" * 78)
sys.exit(1 if fail else 0)
