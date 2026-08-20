"""diag_nodata_radar.py — 데이터 미수신 감지(A-2a) 회귀 검증 + 뮤테이션 테스트.

무엇을 지키려는 검사인가
────────────────────────
가격 이력이 빈 배열로 오면 평가 루프가 `continue` 로 조용히 건너뛴다. 티커
변경·상장폐지가 대표적 원인이고, **보유 종목에서 발생하면 매도 신호가 영구히
오지 않는다.** 이 기능은 그 침묵을 사용자에게 알리는 것이다.

그런데 이 기능 자체가 조용히 고장 날 수 있다. 특히 위험한 세 가지:

  1. 미수신을 기록해도 **발송 게이트가 걸러 버리는** 경우
     (`if not _n_u: return` — 침묵을 알리는 메일이 같은 이유로 침묵)
  2. 광범위 장애일 때 티커 수만큼 세어 **제목이 부풀어 실제 신호가 묻히는** 경우
  3. 같은 티커가 워치리스트·보유 양쪽에 있을 때 **보유 표시가 사라지는** 경우
     (심각도 정보 손실)

셋 다 로그에 에러를 남기지 않는다. 기계로 확인해야 한다.

`run_watchlist_alerts.py` 를 import 하지 않는다 — 그 모듈은 최상단에서
`os.environ[...]` 로 시크릿을 요구하고 gspread/pandas 를 끌어온다. 대신 필요한
함수만 **소스에서 추출해 실행**한다(AST 로 함수 정의를 떼어낸다). 그래서 검사
대상이 실제 배포 코드와 같은 소스임이 보장된다.

네트워크·시트·시크릿 없이 돈다.

    python automation/diag_nodata_radar.py
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name
          + (("  — " + detail) if detail and not cond else ""))
    return cond


# ══════════════════════════════════════════════════════════════════════════
# 대상 함수를 실제 소스에서 추출 (import 하지 않는다)
# ══════════════════════════════════════════════════════════════════════════
_WANT_FUNCS = ["reset_nodata", "record_attempt", "record_nodata",
               "nodata_is_systemic", "nodata_for_user", "nodata_total",
               "nodata_weight", "nodata_log_summary", "render_nodata_html"]
_WANT_CONSTS = ["_NODATA_RATIO_ALERT", "_NODATA_MIN_SAMPLE", "_nodata"]


def load_module_slice():
    """run_watchlist_alerts.py 에서 미수신 관련 정의만 떼어 실행한다."""
    path = None
    for cand in (os.path.join(_HERE, "run_watchlist_alerts.py"),
                 os.path.join(_ROOT, "automation", "run_watchlist_alerts.py"),
                 os.path.join(_ROOT, "run_watchlist_alerts.py")):
        if os.path.exists(cand):
            path = cand
            break
    if path is None:
        print("❌ run_watchlist_alerts.py 를 찾을 수 없습니다.")
        sys.exit(2)

    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    picked, found_f, found_c = [], set(), set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            picked.append(node)
            found_f.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in _WANT_CONSTS:
                    picked.append(node)
                    found_c.add(t.id)

    missing = (set(_WANT_FUNCS) - found_f) | (set(_WANT_CONSTS) - found_c)
    if missing:
        print("❌ 소스에 없는 정의: " + ", ".join(sorted(missing)))
        print("   run_watchlist_alerts.py 가 A-2a 이전 버전일 수 있습니다.")
        sys.exit(2)

    picked.sort(key=lambda n: n.lineno)
    mod = ast.Module(body=picked, type_ignores=[])
    ns = {"print": print}
    exec(compile(ast.fix_missing_locations(mod), "<slice>", "exec"), ns)
    print("  소스: " + os.path.relpath(path, _ROOT))
    print("  추출: 함수 " + str(len(found_f)) + " · 상수 " + str(len(found_c)))
    return ns


M = load_module_slice()


def _fresh():
    M["reset_nodata"]()


# ══════════════════════════════════════════════════════════════════════════
# A. 기록 · 집계
# ══════════════════════════════════════════════════════════════════════════
def test_record():
    print("\n── A. 기록 · 집계")
    _fresh()
    check("초기화 후 총계 0", M["nodata_total"]() == 0)
    check("초기화 후 광범위 아님", M["nodata_is_systemic"]() is False)

    for _ in range(10):
        M["record_attempt"]()
    M["record_nodata"]("yab", "aapl", "워치리스트")
    check("티커 대문자 정규화", M["nodata_for_user"]("yab") == [("AAPL", "워치리스트")],
          str(M["nodata_for_user"]("yab")))
    check("총계 1", M["nodata_total"]() == 1)

    M["record_nodata"]("yab", "AAPL", "보유")
    got = M["nodata_for_user"]("yab")
    check("같은 티커 중복 → 1건으로 합침", len(got) == 1, str(got))
    check("발생 위치는 둘 다 보존 (심각도 정보)",
          got[0][1] == "워치리스트·보유", str(got))

    M["record_nodata"]("yab", "AAPL", "보유")
    check("동일 위치 재기록 → 라벨 중복 안 됨",
          M["nodata_for_user"]("yab")[0][1] == "워치리스트·보유")

    M["record_nodata"]("guest1", "TSLA", "보유")
    check("사용자 격리 — yab 목록에 guest1 없음",
          [t for t, _ in M["nodata_for_user"]("yab")] == ["AAPL"])
    check("사용자 격리 — guest1 목록에 yab 없음",
          [t for t, _ in M["nodata_for_user"]("guest1")] == ["TSLA"])
    check("모르는 사용자 → 빈 목록", M["nodata_for_user"]("nobody") == [])

    check("빈 uid 무시", (M["record_nodata"]("", "X", "보유"),
                          M["nodata_for_user"]("") == [])[1])
    check("빈 티커 무시", (M["record_nodata"]("yab", "", "보유"),
                           len(M["nodata_for_user"]("yab")) == 1)[1])
    check("None 입력에 예외 없음",
          (M["record_nodata"](None, None, "보유"), True)[1])


# ══════════════════════════════════════════════════════════════════════════
# B. 광범위 판정 (비율 임계치)
# ══════════════════════════════════════════════════════════════════════════
def test_systemic():
    print("\n── B. 광범위 판정 (비율 임계치)")
    ratio = M["_NODATA_RATIO_ALERT"]
    minn = M["_NODATA_MIN_SAMPLE"]
    check("임계치 30%", abs(ratio - 0.30) < 1e-9, str(ratio))
    check("최소 표본 5", minn == 5, str(minn))

    # 표본 부족 — 비율이 100%여도 광범위로 보지 않는다
    _fresh()
    for i in range(3):
        M["record_attempt"]()
        M["record_nodata"]("yab", "T" + str(i), "보유")
    check("표본 3건 전부 미수신 → 광범위 아님 (표본 부족)",
          M["nodata_is_systemic"]() is False)
    check("표본 부족 시 개별 보고 (가중치 = 티커 수)",
          M["nodata_weight"]("yab") == 3, str(M["nodata_weight"]("yab")))

    # 20개 중 2개 = 10% → 종목 문제
    _fresh()
    for i in range(20):
        M["record_attempt"]()
    for i in range(2):
        M["record_nodata"]("yab", "T" + str(i), "보유")
    check("20건 중 2건(10%) → 종목 문제", M["nodata_is_systemic"]() is False)
    check("종목 문제 시 가중치 = 티커 수", M["nodata_weight"]("yab") == 2)

    # 20개 중 6개 = 30% → 경계값, 초과 아님
    _fresh()
    for i in range(20):
        M["record_attempt"]()
    for i in range(6):
        M["record_nodata"]("yab", "T" + str(i), "보유")
    check("경계값 정확히 30% → 광범위 아님 (초과 조건)",
          M["nodata_is_systemic"]() is False)

    # 20개 중 7개 = 35% → 광범위
    _fresh()
    for i in range(20):
        M["record_attempt"]()
    for i in range(7):
        M["record_nodata"]("yab", "T" + str(i), "보유")
    check("20건 중 7건(35%) → 광범위", M["nodata_is_systemic"]() is True)
    check("광범위 시 가중치 1 (제목 부풀림 방지)",
          M["nodata_weight"]("yab") == 1, str(M["nodata_weight"]("yab")))
    check("광범위여도 총계는 실제 건수", M["nodata_total"]() == 7)

    check("미수신 없는 사용자 가중치 0", M["nodata_weight"]("nobody") == 0)

    # 분모 0 — 0으로 나누기 방어
    _fresh()
    check("조회 0건 → 예외 없이 광범위 아님", M["nodata_is_systemic"]() is False)


# ══════════════════════════════════════════════════════════════════════════
# C. HTML 렌더링
# ══════════════════════════════════════════════════════════════════════════
def test_render():
    print("\n── C. HTML 렌더링")
    _fresh()
    check("미수신 없으면 빈 문자열", M["render_nodata_html"]("yab") == "")

    # 개별 보고
    _fresh()
    for i in range(20):
        M["record_attempt"]()
    M["record_nodata"]("yab", "VYNE", "보유")
    M["record_nodata"]("yab", "CCIX", "워치리스트")
    h = M["render_nodata_html"]("yab")
    check("개별 보고 — 티커가 본문에 있다", "VYNE" in h and "CCIX" in h)
    check("개별 보고 — 보유 경고 문구 포함", "매도 신호가 나오지 않습니다" in h)
    check("개별 보고 — 원인 안내 포함", "상장폐지" in h)
    check("개별 보고 — 광범위 문구 없음", "광범위" not in h)
    check("타 사용자 티커 누출 없음", "TSLA" not in h)

    # 광범위
    _fresh()
    for i in range(20):
        M["record_attempt"]()
    for i in range(10):
        M["record_nodata"]("yab", "T" + str(i), "보유")
    h = M["render_nodata_html"]("yab")
    check("광범위 — 전용 문구", "광범위" in h)
    check("광범위 — 개별 티커 나열 안 함", "T0" not in h and "T9" not in h)
    check("광범위 — 건수는 표시", "10개 종목" in h)
    check("광범위 — '신호 없음' 오독 경고 포함",
          "'신호 없음'을 뜻하지 않습니다" in h)


# ══════════════════════════════════════════════════════════════════════════
# D. 소스 계약 — 실제 코드가 이 기능을 호출하는가
# ══════════════════════════════════════════════════════════════════════════
def test_source_contract():
    print("\n── D. 소스 계약 (AST 검사)")
    path = None
    for cand in (os.path.join(_HERE, "run_watchlist_alerts.py"),
                 os.path.join(_ROOT, "automation", "run_watchlist_alerts.py"),
                 os.path.join(_ROOT, "run_watchlist_alerts.py")):
        if os.path.exists(cand):
            path = cand
            break
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)

    def calls_in(fname):
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == fname:
                return {c.func.id for c in ast.walk(n)
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        return set()

    wl = calls_in("eval_watchlist_eod")
    pf = calls_in("eval_portfolio_eod")
    mn = calls_in("main")
    dp = calls_in("dispatch_radar_emails")

    check("워치리스트 EOD 가 record_nodata 호출", "record_nodata" in wl)
    check("워치리스트 EOD 가 record_attempt 호출", "record_attempt" in wl)
    check("보유 EOD 가 record_nodata 호출", "record_nodata" in pf)
    check("보유 EOD 가 record_attempt 호출", "record_attempt" in pf)
    check("main 이 reset_nodata 호출 (실행 간 오염 방지)", "reset_nodata" in mn)
    check("main 이 nodata_log_summary 호출", "nodata_log_summary" in mn)
    check("main 게이트가 nodata_total 을 본다 (침묵의 침묵 방지)",
          "nodata_total" in mn)
    check("발송부가 nodata_weight 를 본다", "nodata_weight" in dp)
    check("발송부가 render_nodata_html 을 본다", "render_nodata_html" in dp)

    # build_email_html 이 nodata_html 를 받고 쓰는가
    ok_sig = ok_use = False
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "build_email_html":
            ok_sig = any(a.arg == "nodata_html" for a in n.args.args)
            ok_use = any(isinstance(x, ast.Name) and x.id == "nodata_html"
                         for x in ast.walk(n))
    check("build_email_html 이 nodata_html 인자를 받는다", ok_sig)
    check("build_email_html 이 nodata_html 을 본문에 쓴다", ok_use)

    # 기존 호출 호환 — 기본값이 있어야 한다
    dflt = False
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "build_email_html":
            dflt = len(n.args.defaults) >= 1
    check("nodata_html 기본값 존재 (기존 호출부 무손상)", dflt)


# ══════════════════════════════════════════════════════════════════════════
# E. 뮤테이션 — 버그를 심어 검사가 잡는가
# ══════════════════════════════════════════════════════════════════════════
def test_mutation():
    print("\n── E. 뮤테이션")

    def systemic_check_works():
        """35% 상황에서 광범위 판정 + 가중치 1 이 유지되는가."""
        _fresh()
        for i in range(20):
            M["record_attempt"]()
        for i in range(7):
            M["record_nodata"]("yab", "T" + str(i), "보유")
        return M["nodata_is_systemic"]() and M["nodata_weight"]("yab") == 1

    def merge_check_works():
        """워치리스트·보유 양쪽 기록 시 보유 라벨이 남는가."""
        _fresh()
        M["record_attempt"]()
        M["record_nodata"]("yab", "AAPL", "워치리스트")
        M["record_nodata"]("yab", "AAPL", "보유")
        got = M["nodata_for_user"]("yab")
        # ⚠️ "보유 in 라벨" 만 보면 덮어쓰기 버그를 못 잡는다 — 마지막 호출이
        #    "보유" 라서 덮어써도 그 조건은 통과한다(실제로 M4 가 처음에
        #    검출되지 않은 원인). **두 라벨이 모두** 남았는지 봐야 한다.
        return (len(got) == 1
                and "보유" in got[0][1]
                and "워치리스트" in got[0][1])

    check("기준 상태 — 광범위 검사 정상", systemic_check_works())
    check("기준 상태 — 라벨 병합 정상", merge_check_works())

    # M1: 임계치를 1.0 으로 → 광범위가 절대 발동하지 않는다
    orig = M["_NODATA_RATIO_ALERT"]
    M["_NODATA_RATIO_ALERT"] = 1.0
    caught = not systemic_check_works()
    M["_NODATA_RATIO_ALERT"] = orig
    check("M1 임계치 1.0 (광범위 무력화) → 검출됨", caught)

    # M2: 최소 표본을 1000 으로 → 광범위 판정 불가
    orig_m = M["_NODATA_MIN_SAMPLE"]
    M["_NODATA_MIN_SAMPLE"] = 1000
    caught = not systemic_check_works()
    M["_NODATA_MIN_SAMPLE"] = orig_m
    check("M2 최소 표본 1000 → 검출됨", caught)

    # M3: 가중치가 항상 티커 수 → 광범위에서 제목 부풀림
    orig_w = M["nodata_weight"]
    M["nodata_weight"] = lambda uid: len(M["nodata_for_user"](uid))
    caught = not systemic_check_works()
    M["nodata_weight"] = orig_w
    check("M3 가중치 항상 티커 수 → 검출됨", caught)

    # M4: 라벨을 덮어써 보유 정보 소실
    orig_r = M["record_nodata"]

    def _overwrite(uid, ticker, where):
        u = str(uid or "").strip()
        tk = str(ticker or "").strip().upper()
        if not u or not tk:
            return
        M["_nodata"]["missing"] += 1
        M["_nodata"]["by_user"].setdefault(u, {})[tk] = [where]   # 덮어쓰기 버그

    M["record_nodata"] = _overwrite
    caught = not merge_check_works()
    M["record_nodata"] = orig_r
    check("M4 라벨 덮어쓰기 (보유 표시 소실) → 검출됨", caught)

    check("뮤테이션 원복 후 정상",
          systemic_check_works() and merge_check_works())


# ══════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 70)
    print("데이터 미수신 감지(A-2a) 회귀 검증")
    print("  네트워크·시트·시크릿 없음 · 부작용 없음")
    print("=" * 70)

    test_record()
    test_systemic()
    test_render()
    test_source_contract()
    test_mutation()

    print("")
    print("=" * 70)
    print("결과: 통과 " + str(len(_PASS)) + " · 실패 " + str(len(_FAIL)))
    if _FAIL:
        print("")
        print("❌ 실패 항목:")
        for f in _FAIL:
            print("   · " + f)
        print("")
        print("⚠️ 배포하지 말 것. 미수신 감지가 고장 나면 침묵을 알리는 경로가")
        print("   같은 이유로 침묵한다 — 로그에 에러가 남지 않는다.")
    else:
        print("✅ 전 항목 통과 — 배포 가능")
    print("=" * 70)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
