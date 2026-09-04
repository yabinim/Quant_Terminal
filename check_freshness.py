"""check_freshness.py — 프로젝트 사본 신선도 지문 + 모듈 간 정합성 검사.

왜 필요한가
───────────
프로젝트 폴더 사본이 GitHub 배포본보다 뒤처져 있는 채로 편집을 시작해 회귀가
난 적이 있다(~300줄 손실). Claude 는 비공개 저장소를 직접 읽을 수 없으므로
"최신인지"를 스스로 판정하지 못한다. 대신 두 가지를 한다:

  1) 지문 표 — 사용자가 GitHub 과 30초 안에 대조할 수 있는 형태로 출력
  2) 모듈 간 정합성 — 사본끼리 앞뒤가 안 맞으면(A 가 부르는 심볼이 B 에 없음)
     **버전이 섞였다는 사실이 자동으로 드러난다.** 이쪽이 실제 방어선이다.

사용: python3 check_freshness.py [프로젝트경로]   (기본 /mnt/project)
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/mnt/project"

# 모듈: 존재를 확인할 기능 마커 (있으면 그 시점 이후 버전)
MARKERS = {
    "app.py": ["_SSOT_NEEDS", "load_earnings_universe", "TIMING_LABELS_INFERRED",
               "_open_quant_db", "update_watchlist_row", "시장 이벤트 지형"],
    "earnings_core.py": ["infer_timing", "fetch_market_calendar_map", "SOURCE_UNIVERSE",
                         "UNIVERSE_WORKSHEET", "_timing_from_utc", "TIMING_LABELS_INFERRED"],
    "run_earnings_watch.py": ["pass_universe", "_batch_update", "_merge_runs",
                              "FORCE_CALENDAR", "FORCE_UNIVERSE", "gsr.call"],
    "fmp_http.py": ["fmp_get_json_ex", "plan_limited", "set_key_provider"],
    # PROFILE_BATCH: 2026-09-04 백테스트 2개의 `_gs` 중복 흡수. 이 마커가 없으면
    #   run_signal_backtest / diag_satellite_backtest 가 import 단계에서 죽는다
    #   (AttributeError: module 'gs_retry' has no attribute 'PROFILE_BATCH').
    "gs_retry.py": ["_retryable", "GS_MAX_RETRIES", "PROFILE_BATCH"],
    "fmp_extras.py": ["import fmp_http", "fmp_stats_line"],
    "regime_core.py": ["_market_warnings", "ALERT_CONFIRM_DAYS"],
    "users_core.py": ["Gate_Market"],
    "watchlist_metrics_core.py": ["completed_bars_only"],
    "scanner_core.py": [],
    # import fmp_http: 2026-09-04 A1 전환(원시 requests 2곳 → fmp_http).
    #   fmp_get_json_ex: _fmp_validate_symbols_ex 의 3상태 판정이 kind 에 걸려 있다.
    "narrative_core.py": ["import fmp_http", "fmp_get_json_ex"],
    "portfolio_core.py": [],
    "accounts_core.py": [], "gemini_core.py": [], "run_watchlist_alerts.py": [],
}

# app.py 가 `별칭.심볼` 로 참조하는 공용 모듈 (import 별칭은 자동 추출)
CROSS_TARGETS = {"regime_core", "users_core", "narrative_core", "scanner_core",
                 "fmp_extras", "portfolio_core", "watchlist_metrics_core",
                 "earnings_core", "accounts_core"}


def top_level_names(src: str) -> set:
    """모듈 최상위에 정의된 이름. 튜플 대입도 푼다.

    `TIER_NEAR, TIER_MID, TIER_FAR = "near", "mid", "far"` 같은 형태를 놓치면
    멀쩡한 모듈을 '심볼 없음'으로 오탐한다(실제로 발생).
    """
    out = set()

    def _add(t):
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                _add(e)

    for n in ast.parse(src).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                _add(t)
        elif isinstance(n, ast.AnnAssign):
            _add(n.target)
    return out


def path(name):
    """ROOT 평면 → automation/ 순으로 찾는다.

    2026-09-04: 이전에는 ROOT 평면만 봤다. 프로젝트 사본은 평면이라 문제가
    없었지만, **레포 루트에서 돌리면** run_earnings_watch.py ·
    run_watchlist_alerts.py 가 automation/ 에 있어 "사본 없음"으로 빠졌다.
    지문 표를 GitHub Actions 로그에 남기려면 배포 레이아웃 그대로 찾아야 한다.
    (평면 사본에서는 첫 번째 후보가 바로 맞으므로 동작이 바뀌지 않는다.)
    """
    flat = os.path.join(ROOT, name)
    if os.path.isfile(flat):
        return flat
    nested = os.path.join(ROOT, "automation", name)
    if os.path.isfile(nested):
        return nested
    return flat


def read(name):
    try:
        return open(path(name), encoding="utf-8").read()
    except Exception:
        return None


print(f"프로젝트 경로: {ROOT}\n")
print("=" * 78)
print("1) 지문 — GitHub 과 대조하세요 (줄 수가 다르면 사본이 낡은 것)")
print("=" * 78)
print(f"{'파일':30} {'줄수':>7}  마커(기능 존재 여부)")
print("-" * 78)

srcs = {}
missing = []
for name, marks in MARKERS.items():
    src = read(name)
    if src is None:
        missing.append(name)
        continue
    srcs[name] = src
    n = src.count("\n") + 1
    if marks:
        have = [m for m in marks if m in src]
        tag = f"{len(have)}/{len(marks)}"
        if len(have) < len(marks):
            tag += "  ⚠ 누락: " + ", ".join(m for m in marks if m not in src)[:40]
    else:
        tag = "-"
    print(f"{name:30} {n:>7}  {tag}")

if missing:
    print(f"\n⚠ 사본 없음: {', '.join(missing)}")

print("\n" + "=" * 78)
print("2) 모듈 간 정합성 — 여기서 실패하면 사본 버전이 섞인 것")
print("=" * 78)

problems = []
app = srcs.get("app.py")
if not app:
    print("app.py 사본이 없어 교차 검사를 건너뜁니다.")
else:
    tree = ast.parse(app)
    alias = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in CROSS_TARGETS:
                    alias[a.asname or a.name] = a.name

    used = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id in alias):
            used.setdefault(alias[n.value.id], set()).add(n.attr)

    for mod in sorted(used):
        msrc = read(mod + ".py")
        if msrc is None:
            problems.append(f"{mod}.py 사본 없음 (app.py 가 참조 중)")
            print(f"  ❌ {mod:26} 사본 없음")
            continue
        miss = sorted(used[mod] - top_level_names(msrc))
        if miss:
            problems.append(f"{mod}: app.py 가 쓰는 {miss} 없음")
            print(f"  ❌ {mod:26} app.py 가 쓰는 {len(miss)}개 없음 → {miss[:6]}")
        else:
            print(f"  ✅ {mod:26} app.py 가 쓰는 {len(used[mod])}개 심볼 모두 존재")

# 자동화 ↔ 공용 모듈
for auto in ("run_earnings_watch.py", "run_watchlist_alerts.py"):
    asrc = srcs.get(auto)
    if not asrc:
        continue
    at = ast.parse(asrc)
    al = {}
    for n in ast.walk(at):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in CROSS_TARGETS:
                    al[a.asname or a.name] = a.name
    u = {}
    for n in ast.walk(at):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id in al):
            u.setdefault(al[n.value.id], set()).add(n.attr)
    for mod in sorted(u):
        msrc = read(mod + ".py")
        if msrc is None:
            continue
        miss = sorted(u[mod] - top_level_names(msrc))
        if miss:
            problems.append(f"{auto} → {mod}: {miss} 없음")
            print(f"  ❌ {auto} → {mod:14} {len(miss)}개 없음 → {miss[:6]}")

print("\n" + "=" * 78)
if problems:
    print(f"❌ 정합성 문제 {len(problems)}건 — **사본 버전이 섞였을 가능성이 높습니다.**")
    print("   편집 전에 GitHub 현재 버전을 받으세요.")
    for p in problems:
        print(f"   · {p}")
else:
    print("✅ 사본끼리는 앞뒤가 맞습니다.")
    print("   다만 이것이 'GitHub 최신'을 보장하지는 않습니다 —")
    print("   전부 같은 시점의 낡은 버전이면 정합성은 통과합니다.")
    print("   위 지문 표의 줄 수를 GitHub 과 대조해 주세요.")
print("=" * 78)
sys.exit(1 if problems else 0)
