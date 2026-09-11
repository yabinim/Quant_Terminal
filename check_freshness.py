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
    # cached_satellite_drawdown: 2026-09-07 위성 슬리브 낙폭(md §4③) 표시.
    #   이 마커가 없는데 fmp_extras 만 올리면 앱 위성 블록이 NameError 로 죽는다.
    "app.py": ["_SSOT_NEEDS", "load_earnings_universe", "TIMING_LABELS_INFERRED",
               "_open_quant_db", "update_watchlist_row", "시장 이벤트 지형",
               "cached_satellite_drawdown"],
    # est_archive_row: 2026-09-05 EPS 추정치 분기 아카이브. 이 마커가 없는데
    #   run_earnings_watch 만 올리면, 분기 전환 종목마다
    #   "[WARN] {tk} 추정치 아카이브 실패" 만 찍히고 **그 분기 시계열이 영구히
    #   사라진다.** try/except 안이라 러너는 초록불로 끝난다 — 조용한 유실이다.
    "earnings_core.py": ["infer_timing", "fetch_market_calendar_map", "SOURCE_UNIVERSE",
                         "UNIVERSE_WORKSHEET", "_timing_from_utc", "TIMING_LABELS_INFERRED",
                         "est_archive_row", "EST_ARCHIVE_COLS"],
    # est_archive_row: 위 earnings_core 마커와 **짝**이다. 반대 방향 유실을
    #   잡는다 — earnings_core 만 올리면 함수는 있는데 아무도 안 부른다.
    "run_earnings_watch.py": ["pass_universe", "_batch_update", "_merge_runs",
                              "FORCE_CALENDAR", "FORCE_UNIVERSE", "gsr.call",
                              "est_archive_row"],
    "fmp_http.py": ["fmp_get_json_ex", "plan_limited", "set_key_provider"],
    # PROFILE_BATCH: 2026-09-04 백테스트 2개의 `_gs` 중복 흡수. 이 마커가 없으면
    #   run_signal_backtest / diag_satellite_backtest 가 import 단계에서 죽는다
    #   (AttributeError: module 'gs_retry' has no attribute 'PROFILE_BATCH').
    "gs_retry.py": ["_retryable", "GS_MAX_RETRIES", "PROFILE_BATCH"],
    # satellite_drawdown / SATELLITE_SNAPSHOT_COLS: 2026-09-07 위성 스냅샷 SSOT.
    #   세 소비자(app · run_hidden_alpha · run_satellite_snapshot)가 전부 여기를
    #   부른다. 이 마커가 없으면 셋 다 임포트 단계에서 죽는다 — 시끄럽게 죽는
    #   편이 낫지만, 관문에서 먼저 잡는 게 더 낫다.
    #   MOM_MKT_RULES(2026-09-11): 시장 룰 레지스트리. 없는 fmp_extras 에 새 bt 를
    #   올리면 RankEngine 이 fx.is_mkt_rule 에서 AttributeError — 시끄러운 실패다.
    "fmp_extras.py": ["import fmp_http", "fmp_stats_line",
                      "SATELLITE_SNAPSHOT_COLS", "satellite_drawdown", "MOM_MKT_RULES"],
    "regime_core.py": ["_market_warnings", "ALERT_CONFIRM_DAYS"],
    "users_core.py": ["Gate_Market"],
    "watchlist_metrics_core.py": ["completed_bars_only"],
    "scanner_core.py": [],
    # import fmp_http: 2026-09-04 A1 전환(원시 requests 2곳 → fmp_http).
    #   fmp_get_json_ex: _fmp_validate_symbols_ex 의 3상태 판정이 kind 에 걸려 있다.
    "narrative_core.py": ["import fmp_http", "fmp_get_json_ex"],
    # session_phase / narrative_session_label: 2026-09-09 세션 구간 SSOT.
    #   app.py 와 run_narrative.py 가 **둘 다** 이 두 함수를 부른다. 마커가
    #   없는 calendar_core 를 올리면 양쪽이 AttributeError 로 죽는다 —
    #   시끄럽게 죽는 편이 낫지만 관문에서 먼저 잡는 게 더 낫다.
    # nyse_early_close_days / session_close_time: 반일장 규칙 본체.
    #   이게 없으면 2PM 가드와 앱 시장 상태 헤더가 동시에 무력화된다.
    "calendar_core.py": ["session_phase", "narrative_session_label",
                         "nyse_early_close_days", "session_close_time"],
    # narrative_session_label: 위 calendar_core 마커와 **짝**이다. 반대 방향
    #   유실을 잡는다 — calendar_core 만 올리면 함수는 있는데 run_narrative 가
    #   여전히 자기 안의 낡은 밴드 표를 쓴다. 그 실패는 조용하다: 메일도 시트
    #   저장도 정상이고 반일장 13:30 이 "Market Hours Analysis" 로 남는다.
    "run_narrative.py": ["narrative_session_label"],
    # 반일장 규칙 불일치: 2026-09-09 추가된 리마인더 배달 경로.
    #   이 마커가 없으면 규칙이 FMP 와 어긋나도 Actions 로그에만 남고 아무도
    #   모른다. HALFDAY-ALERT 는 그 이전 차수(대조 자체)의 마커다.
    "refresh_market_calendar.py": ["HALFDAY-ALERT", "반일장 규칙 불일치"],
    "portfolio_core.py": [],
    "accounts_core.py": [], "gemini_core.py": [],
    # _intraday_close_passed: 반일장 2PM 가드의 판정 본체.
    #   이 마커가 없는 사본을 올리면 **반일장 14:00 에 2PM 잡이 그대로 돈다.**
    #   /quote 가 돌려주는 13:00 종가를 "장중 잠정" 봉으로 주입해 헤드업 메일을
    #   보낸다 — 숫자는 맞고 라벨만 틀리며, 3시간 뒤 5PM 이 같은 숫자로 확정
    #   메일을 또 보낸다. 예외도 에러 로그도 없다. 조용한 실패다.
    # session_close_time: 마감 시각을 calendar_core 에서 받는다는 증거.
    #   함수만 있고 이게 없으면 마감 시각이 하드코딩됐다는 뜻이다.
    "run_watchlist_alerts.py": ["_intraday_close_passed", "session_close_time"],
    # build_drawdown_html: fmp_extras 위성 마커와 **짝**이다. 반대 방향 유실을
    #   잡는다 — fmp_extras 만 올리면 함수는 있는데 주간 메일이 안 부른다.
    #   그 실패는 조용하다: 이메일은 정상 발송되고 낙폭 섹션만 없다.
    # SATELLITE_INSTRUCTION_SHEET: md §4② 층 1 의 유일한 기록 지점이다. 이 마커가
    #   없으면 주말 메일은 멀쩡히 나가고 지시만 조용히 안 쌓인다 — 낙폭과 같은
    #   종류의 조용한 실패다. 층 2 를 만드는 날에야 빈 시트를 발견하게 된다.
    "run_hidden_alpha.py": ["build_drawdown_html", "satellite_drawdown",
                            "SATELLITE_INSTRUCTION_SHEET"],
    # 월별 스냅샷 러너. 없으면 md §4③ 의 시계열이 아예 안 쌓인다.
    # SNAPSHOT_MODES: seed_satellite_snapshot.yml 과 **짝**이다. yml 만 올리고
    #   이 마커가 없으면, mode=seed 를 눌러도 스크립트가 그 값을 모른 채 기본
    #   monthly 로 돌아 "[SKIP] 마지막 거래일이 아닙니다" 만 찍고 **exit 0** 으로
    #   끝난다. Actions 는 초록불이고 시드는 만들어지지 않는다 — 조용한 실패다.
    "run_satellite_snapshot.py": ["is_last_trading_day_of_month",
                                  "load_satellite_holdings",
                                  "SATELLITE_SNAP_SEED", "SNAPSHOT_MODES"],
    # ── §4① 판정 · 깊은 창 참고 실행 (2026-09-10 작업 A) ─────────────────────
    # 넷이 락스텝이다. 이전까지 이 표에 없어서, A 착수 때 세션 시작 지문으로는
    # 판정 엔진의 사본이 최신인지 알 수 없었다(따로 세어야 했다).
    # WINDOW_DAYS_OVERRIDE: 참고 실행 전용 창 지정. 이게 없는 bt 에 러너만 올리면
    #   러너는 AttributeError 로 시끄럽게 죽는다 — 괜찮은 실패다.
    # _OV_ALLOWED: 판정 파일이 그 값을 대입하지 못하게 막는 B4s 의 허용 목록.
    #   이 마커가 없는 ssot 는 판정 경로가 깊어져도 초록불이다 — 조용한 실패다.
    "diag_satellite_backtest.py": ["WINDOW_DAYS_PIN", "WINDOW_DAYS_OVERRIDE",
                                   "_env_as_of", "mom_score_mkt"],
    "diag_momentum_rule_compare.py": ["VERDICT_RULES", "_env_as_of"],
    # Meas_Start: 2026-09-10 측정 결함 수정. 없는 사본이면 절삭 구간이 다시 n/a 다.
    "diag_momentum_deep_ref.py": ["EPISODE_DD", "REBOUND_BARS",
                                  "WINDOW_DAYS_OVERRIDE", "Meas_Start"],
    "diag_fmp_ssot.py": ["_OV_ALLOWED", "B4s", "B4t", "B4u"],
    # ── §4① β중립 12-0 참고 실행 (2026-09-11 · C-1) ─────────────────────────
    # 러너 · 약정 문서 · 두 드리프트 가드가 락스텝이다. 약정 숫자는 md 와 러너에
    # 두 번 적혀 있고 K 그룹이 그 둘을 묶는다 — 한쪽만 낡으면 K 가 빨간불이다.
    # 이전까지 md · mandate 가드 · consumers 가드가 이 표에 없어서, 세션 시작 지문으로
    # 그 셋의 사본이 최신인지 알 수 없었다.
    "diag_beta_mom_ref.py": ["TARGET_LEGS", "R0_TOL_PP", "run_core"],
    "diag_hist_window_consumers.py": ["S3m"],
    # C-1 결과 기록(2026-09-11 · 미관찰 → 종료). 위 두 마커는 약정 블록에만 있어서
    # 결과 기록 **전** 사본과 **후** 사본을 가르지 못한다 — 셋째 마커가 그 구분이다.
    # md 가 2/3 이면 종료 기록 전 사본이다. 그 사본으로 §2 를 고치면 종료 기록이 날아간다.
    "diag_satellite_mandate.py": ["J8", "K11", "K12"],
    "SATELLITE_MANDATE.md": ["β중립 C1", "β중립 결과", "대응 시도 1 — β중립 12-0"],
}

# app.py 가 `별칭.심볼` 로 참조하는 공용 모듈 (import 별칭은 자동 추출)
CROSS_TARGETS = {"regime_core", "users_core", "narrative_core", "scanner_core",
                 "fmp_extras", "portfolio_core", "watchlist_metrics_core",
                 "earnings_core", "accounts_core",
                 # calendar_core: app.py 가 `mcal.` 로 부르고 자동화 6개가 `cc.`
                 #   로 부른다. 지금까지 교차 검사 대상이 아니었는데, 세션 구간
                 #   SSOT 가 들어오면서 소비자가 늘었다 — 딱 이 검사가 필요한
                 #   자리다. 별칭은 아래에서 자동 추출하므로 mcal/cc 를 가리지
                 #   않는다.
                 "calendar_core"}


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
# run_narrative.py / refresh_market_calendar.py 를 넣은 이유: 둘 다 이번에
# calendar_core 의 새 심볼에 의존하게 됐다. 여기 없으면 낡은 calendar_core 와
# 새 소비자를 섞어 올려도 관문이 통과한다.
for auto in ("run_earnings_watch.py", "run_watchlist_alerts.py",
             "run_hidden_alpha.py", "run_satellite_snapshot.py",
             "run_narrative.py", "refresh_market_calendar.py"):
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
