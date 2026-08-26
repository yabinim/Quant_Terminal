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
import inspect
import os
import sys
import textwrap

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
               "nodata_weight", "nodata_log_summary", "render_nodata_html",
               # A-2b — 원인 판정. _fmp_profile_status 는 **일부러 뺀다**:
               # 그 함수만 네트워크를 만지고, classify 는 probe 주입으로 테스트한다.
               "classify_nodata_causes", "nodata_cause", "nodata_cause_counts",
               # A-2c — 선제 상장상태 점검. _fmp_actively_trading_symbols 와
               # _collect_user_tickers 는 **일부러 뺀다**: 그 둘만 네트워크/시트를
               # 만지고, run_liveness_scan 은 주입으로 테스트한다.
               "reset_liveness", "liveness_due", "run_liveness_scan",
               "liveness_for_user", "liveness_total", "liveness_weight",
               "liveness_log_summary", "render_liveness_html",
               # 발송 게이트 자체를 실제로 돌려본다(계약 검사만으로는 부족했다)
               "dispatch_radar_emails"]
_WANT_CONSTS = ["_NODATA_RATIO_ALERT", "_NODATA_MIN_SAMPLE", "_nodata",
                "_NODATA_CAUSE_LABEL", "_NODATA_CAUSE_MAX",
                "_LIVENESS_WEEKDAY", "_LIVENESS_MIN_UNIVERSE",
                "_LIVENESS_ABSENT_RATIO", "_LIVENESS_MIN_SAMPLE",
                "_LIVENESS_PROFILE_MAX",
                "_LIVENESS_LABEL", "_liveness"]


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
    # dispatch_radar_emails 를 **실제로 실행**하기 위한 최소 스텁.
    # 계약(AST) 검사만으로는 부족하다 — 함수 안의 조기 반환 가드는 호출 여부가
    # 아니라 조건식이라 AST 로는 안 잡힌다(실제로 그 결함이 있었다).
    class _UC:
        ADMIN_CONTENT_OWNER_ID = "yab"

    _SENT = []

    ns = {
        "print": print,
        "datetime": __import__("datetime").datetime,
        "uc": _UC(),
        "GMAIL_TO": "admin@example.com",
        "_ET": None,
        "SENT": _SENT,
        "send_email": lambda subj, html, to_addr=None: (
            _SENT.append({"subject": subj, "html": html, "to": to_addr}) or True),
        "build_email_html": (lambda wl, pf, today, nodata_html="", liveness_html="":
                             "WL" * sum(len(v) for v in (wl or {}).values())
                             + "PF" * sum(len(v) for v in (pf or {}).values())
                             + nodata_html + liveness_html),
        "_radar_recipients": lambda: [("yab", None)],
    }
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
    # A-2b 이후 이 경로는 '원인 미판정(unknown)' 분기를 탄다. 판정이 실패해도
    # 보유 심각도 경고와 가능 원인 안내가 **A-2a 수준 밑으로 내려가면 안 된다.**
    check("개별 보고(미판정) — 보유 경고 문구 유지", "매도 신호가 나오지 않습니다" in h)
    check("개별 보고(미판정) — 가능 원인 안내 유지", "상장폐지" in h)
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
    check("main 이 classify_nodata_causes 호출 (A-2b 훅 배선)",
          "classify_nodata_causes" in mn)
    _order_src = src[src.find("def main("):]
    _i_cls = _order_src.find("classify_nodata_causes()")
    _i_log = _order_src.find("nodata_log_summary()")
    check("원인 판정이 로그 요약보다 먼저 (요약에 원인이 찍히도록)",
          _i_cls != -1 and _i_log != -1 and _i_cls < _i_log)
    check("classify 가 _fmp_profile_status 를 쓴다 (경로 A)",
          "_fmp_profile_status" in calls_in("classify_nodata_causes")
          or "_fmp_profile_status" in src)
    # ⚠️ src 전체를 grep 하면 **'이 경로를 쓰지 않는다'고 설명한 주석**에 반응한다
    #    (실제로 초판이 그렇게 오탐했다). 주석은 AST 에 남지 않으므로 문자열
    #    상수만 본다. 독스트링도 설명문이라 제외한다.
    _docs = set()
    for _n in ast.walk(tree):
        if isinstance(_n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _d = ast.get_docstring(_n, clean=False)
            if _d:
                _docs.add(_d)
    _lits = [n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and n.value not in _docs]
    check("전역 목록 대조 경로를 실제로 호출하지 않는다 (프로브: 402·US 커버리지 없음)",
          not any(("delisted-companies" in s) or ("symbol-change" in s) for s in _lits))
    check("profile 엔드포인트를 실제로 호출한다 (경로 A)",
          any("profile?symbol=" in s for s in _lits))
    check("main 게이트가 nodata_total 을 본다 (침묵의 침묵 방지)",
          "nodata_total" in mn)
    check("발송부가 nodata_weight 를 본다", "nodata_weight" in dp)
    # ── A-2c 배선 계약 ────────────────────────────────────────────────────
    check("main 이 reset_liveness 호출 (실행 간 오염 방지)", "reset_liveness" in mn)
    check("main 이 liveness_due 로 요일 게이트 (매 실행 1.5MB 방지)",
          "liveness_due" in mn)
    check("main 이 run_liveness_scan 호출", "run_liveness_scan" in mn)
    check("main 이 liveness_log_summary 호출", "liveness_log_summary" in mn)
    check("main 게이트가 liveness_total 을 본다 (경고의 침묵 방지)",
          "liveness_total" in mn)
    check("발송부가 liveness_weight 를 본다", "liveness_weight" in dp)
    check("발송부가 render_liveness_html 을 본다", "render_liveness_html" in dp)
    check("발송 게이트가 liveness_total/nodata_total 을 함께 본다",
          "liveness_total" in dp and "nodata_total" in dp)
    # A-2c 는 부가 기능이다. 예외가 레이더 본체를 죽이면 안 된다.
    _lv_wrapped = False
    for _n in ast.walk(tree):
        if isinstance(_n, ast.FunctionDef) and _n.name == "main":
            for _t in ast.walk(_n):
                if isinstance(_t, ast.Try):
                    _names = {c.func.id for c in ast.walk(_t)
                              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
                    if "run_liveness_scan" in _names and _t.handlers:
                        _lv_wrapped = True
    check("run_liveness_scan 이 try/except 로 감싸져 있다 (fail-open)", _lv_wrapped)
    check("actively-trading-list 를 실제로 호출한다",
          any("actively-trading-list" in s2 for s2 in _lits))
    check("발송부가 render_nodata_html 을 본다", "render_nodata_html" in dp)

    # build_email_html 이 nodata_html 를 받고 쓰는가
    ok_sig = ok_use = False
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "build_email_html":
            ok_sig = any(a.arg == "nodata_html" for a in n.args.args)
            ok_use = any(isinstance(x, ast.Name) and x.id == "nodata_html"
                         for x in ast.walk(n))
    _lv_sig = _lv_use = False
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "build_email_html":
            _lv_sig = any(a.arg == "liveness_html" for a in n.args.args)
            _lv_use = any(isinstance(x, ast.Name) and x.id == "liveness_html"
                          for x in ast.walk(n))
    check("build_email_html 이 liveness_html 인자를 받는다", _lv_sig)
    check("build_email_html 이 liveness_html 을 본문에 쓴다", _lv_use)
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

    # ── A-2b 뮤테이션 ─────────────────────────────────────────────────────
    orig_cls = M["classify_nodata_causes"]
    orig_render = M["render_nodata_html"]

    def _setup(causes, attempted=60):
        _fresh()
        for _ in range(attempted):
            M["record_attempt"]()
        for tk in causes:
            M["record_nodata"]("yab", tk, "보유")

    def budget_gate_works():
        """광범위 장애면 0콜이어야 한다."""
        _fresh()
        for _ in range(10):
            M["record_attempt"]()
        for i in range(6):
            M["record_nodata"]("yab", f"T{i}", "보유")
        calls = []

        def _p(tk):
            calls.append(tk)
            return ("delisted", "", "")
        M["classify_nodata_causes"](probe=_p)
        return calls == []

    def dedupe_works():
        """같은 티커를 두 사용자가 가져도 1콜."""
        _fresh()
        for _ in range(60):
            M["record_attempt"]()
        M["record_nodata"]("yab", "AAPL", "보유")
        M["record_nodata"]("guest1", "AAPL", "워치리스트")
        calls = []

        def _p(tk):
            calls.append(tk)
            return ("transient", "", "")
        M["classify_nodata_causes"](probe=_p)
        return len(calls) == 1

    def loss_guard_works():
        """상폐가 섞였는데 '유지해도 됩니다'가 뜨면 안 된다 — 매도 시점 상실."""
        tbl = {"OK1": ("transient", "", ""), "DEAD": ("delisted", "", "")}
        _setup(tbl)
        M["classify_nodata_causes"](probe=lambda tk: tbl[tk])
        h = M["render_nodata_html"]("yab")
        return ("유지해도 됩니다" not in h) and ("확인이 필요합니다" in h)

    check("A-2b 기준 상태 — 예산 게이트 정상", budget_gate_works())
    check("A-2b 기준 상태 — 중복 제거 정상", dedupe_works())
    check("A-2b 기준 상태 — 손실 가드 정상", loss_guard_works())

    # M5: systemic 게이트 무시 → 장애 중 API 를 티커 수만큼 재타격
    def _no_gate(probe=None):
        fn = probe
        for tk in sorted({t for s in M["_nodata"]["by_user"].values() for t in s}):
            M["_nodata"]["cause"][tk] = fn(tk)
        return 0
    M["classify_nodata_causes"] = _no_gate
    caught = not budget_gate_works()
    M["classify_nodata_causes"] = orig_cls
    check("M5 systemic 게이트 제거 (장애 중 재타격) → 검출됨", caught)

    # M6: 사용자별로 돌아 중복 티커를 두 번 조회
    def _dupe(probe=None):
        fn = probe
        for slot in M["_nodata"]["by_user"].values():
            for tk in sorted(slot):
                M["_nodata"]["cause"][tk] = fn(tk)
        return 0
    M["classify_nodata_causes"] = _dupe
    caught = not dedupe_works()
    M["classify_nodata_causes"] = orig_cls
    check("M6 사용자별 중복 조회 (콜 낭비) → 검출됨", caught)

    # M7: 안심 문구를 무조건 붙임 — 상폐인데 '유지해도 됩니다'
    def _always_calm(uid):
        return orig_render(uid) + "보유는 유지해도 됩니다."
    M["render_nodata_html"] = _always_calm
    caught = not loss_guard_works()
    M["render_nodata_html"] = orig_render
    check("M7 상폐에도 안심 문구 (매도 시점 상실) → 검출됨", caught)

    # M8: 호출 상한 제거
    def _no_cap(probe=None):
        fn = probe
        n = 0
        for tk in sorted({t for s in M["_nodata"]["by_user"].values() for t in s}):
            M["_nodata"]["cause"][tk] = fn(tk)
            n += 1
        return n

    def cap_works():
        _fresh()
        for _ in range(400):
            M["record_attempt"]()
        for i in range(M["_NODATA_CAUSE_MAX"] + 9):
            M["record_nodata"]("yab", f"S{i:03d}", "보유")
        return M["classify_nodata_causes"](
            probe=lambda tk: ("unknown", "", "")) <= M["_NODATA_CAUSE_MAX"]

    check("A-2b 기준 상태 — 호출 상한 정상", cap_works())
    M["classify_nodata_causes"] = _no_cap
    caught = not cap_works()
    M["classify_nodata_causes"] = orig_cls
    check("M8 호출 상한 제거 (예산이 입력에 좌우됨) → 검출됨", caught)

    check("A-2b 뮤테이션 원복 후 정상",
          budget_gate_works() and dedupe_works() and loss_guard_works() and cap_works())


# ══════════════════════════════════════════════════════════════════════════
# C-2. 원인 판정 (A-2b)
# ══════════════════════════════════════════════════════════════════════════
def test_cause():
    print("\n── C-2. 원인 판정 (A-2b)")

    def fake(table, log=None):
        def _f(tk):
            if log is not None:
                log.append(tk)
            return table.get(tk, ("unknown", "", "미정의"))
        return _f

    # --- 호출 예산: 해피 패스는 반드시 0콜이어야 한다 ---
    _fresh()
    for _ in range(20):
        M["record_attempt"]()
    calls = []
    n = M["classify_nodata_causes"](probe=fake({}, calls))
    check("미수신 0건 → 0콜 (해피 패스 비용 없음)", n == 0 and calls == [], str(n))

    # --- 광범위 장애면 판정 자체를 건너뛴다 ---
    _fresh()
    for _ in range(10):
        M["record_attempt"]()
    for i in range(5):                     # 5/10 = 50% > 30% → systemic
        M["record_nodata"]("yab", f"T{i}", "보유")
    calls = []
    n = M["classify_nodata_causes"](probe=fake({}, calls))
    check("광범위 장애 → 0콜 (장애 중 API 재타격 방지)", n == 0 and calls == [], str(n))

    # --- 사용자 간 중복 티커는 1콜 ---
    _fresh()
    for _ in range(50):
        M["record_attempt"]()
    M["record_nodata"]("yab", "AAPL", "보유")
    M["record_nodata"]("guest1", "AAPL", "워치리스트")
    M["record_nodata"]("yab", "TSLA", "워치리스트")
    calls = []
    M["classify_nodata_causes"](probe=fake(
        {"AAPL": ("transient", "Apple Inc.", ""),
         "TSLA": ("delisted", "Tesla", "")}, calls))
    check("사용자 간 중복 티커 → 1콜로 합침", sorted(calls) == ["AAPL", "TSLA"], str(calls))
    check("판정 결과 저장", M["nodata_cause"]("AAPL")[0] == "transient")
    check("회사명 보존", M["nodata_cause"]("AAPL")[1] == "Apple Inc.")
    check("미판정 티커 → unknown", M["nodata_cause"]("ZZZZ")[0] == "unknown")

    # --- 호출 상한 ---
    _fresh()
    for _ in range(400):
        M["record_attempt"]()
    for i in range(M["_NODATA_CAUSE_MAX"] + 7):
        M["record_nodata"]("yab", f"S{i:03d}", "워치리스트")
    calls = []
    n = M["classify_nodata_causes"](probe=fake({}, calls))
    check("호출 상한 준수 (예산이 입력에 좌우되지 않음)",
          n == M["_NODATA_CAUSE_MAX"], f"{n} vs {M['_NODATA_CAUSE_MAX']}")

    # --- probe 가 예외를 던져도 판정이 멈추지 않아야 한다 ---
    _fresh()
    for _ in range(50):
        M["record_attempt"]()
    M["record_nodata"]("yab", "AAA", "보유")
    M["record_nodata"]("yab", "BBB", "보유")

    def _boom(tk):
        if tk == "AAA":
            raise RuntimeError("네트워크 폭발")
        return ("delisted", "B Corp", "")

    M["classify_nodata_causes"](probe=_boom)
    check("probe 예외 → unknown 으로 격리, 다음 종목 계속",
          M["nodata_cause"]("AAA")[0] == "unknown"
          and M["nodata_cause"]("BBB")[0] == "delisted")

    # --- 알 수 없는 원인 문자열은 unknown 으로 정규화 ---
    _fresh()
    for _ in range(50):
        M["record_attempt"]()
    M["record_nodata"]("yab", "CCC", "보유")
    M["classify_nodata_causes"](probe=lambda tk: ("좀비", "", ""))
    check("미정의 원인 라벨 → unknown 정규화", M["nodata_cause"]("CCC")[0] == "unknown")

    # ── 문안 분기 — 여기서 틀리면 손실로 이어진다 ────────────────────────
    # 전부 일시적인데 "즉시 확인"을 띄우면 늑대소년이 되고,
    # 상폐가 섞였는데 "유지해도 된다"를 띄우면 매도 시점을 놓친다.
    def html_for(causes):
        _fresh()
        for _ in range(60):
            M["record_attempt"]()
        for tk in causes:
            M["record_nodata"]("yab", tk, "보유")
        M["classify_nodata_causes"](probe=lambda tk: causes[tk])
        return M["render_nodata_html"]("yab")

    h = html_for({"AAPL": ("transient", "Apple Inc.", "")})
    check("전부 정상거래 → '유지해도 됩니다' 안내", "유지해도 됩니다" in h)
    check("전부 정상거래 → '확인이 필요합니다' 없음", "확인이 필요합니다" not in h)
    check("정상거래 라벨 표시", "일시적 데이터 공백" in h)
    check("회사명 렌더", "Apple Inc." in h)

    h = html_for({"AAPL": ("transient", "Apple Inc.", ""),
                  "DEAD": ("delisted", "Dead Co", "")})
    check("상폐 1건 섞이면 → '확인이 필요합니다'", "확인이 필요합니다" in h)
    check("상폐 섞이면 '유지해도 됩니다' 억제 (손실 방지)", "유지해도 됩니다" not in h)
    check("상폐 라벨 표시", "상장폐지·거래중지" in h)

    h = html_for({"OLDX": ("gone", "", "profile 빈 배열")})
    check("티커 소멸 → '확인이 필요합니다'", "확인이 필요합니다" in h)
    check("티커 소멸 라벨 표시", "티커 소멸" in h)
    check("비고 렌더", "profile 빈 배열" in h)

    h = html_for({"XXXX": ("unknown", "", "플랜 미포함(해외 상장 가능성)")})
    check("원인 미확인 → 단정하지 않고 확인 요청", "확인하세요" in h)
    check("원인 미확인 → '유지해도 됩니다' 없음", "유지해도 됩니다" not in h)

    h = html_for({"AAPL": ("transient", "Apple Inc.", ""),
                  "XXXX": ("unknown", "", "HTTP 500")})
    check("정상거래+미확인 혼재 → 안심 문구 억제", "유지해도 됩니다" not in h)

    # --- 광범위 장애 렌더는 원인 없이도 동작해야 한다 ---
    _fresh()
    for _ in range(10):
        M["record_attempt"]()
    for i in range(6):
        M["record_nodata"]("yab", f"G{i}", "보유")
    h = M["render_nodata_html"]("yab")
    check("광범위 장애 렌더 정상 (원인 판정 생략 상태)", "광범위" in h and h != "")

    # --- 원인 집계 ---
    _fresh()
    for _ in range(60):
        M["record_attempt"]()
    for tk, c in [("A", "delisted"), ("B", "delisted"), ("C", "transient")]:
        M["record_nodata"]("yab", tk, "보유")
    M["classify_nodata_causes"](
        probe=lambda tk: ({"A": "delisted", "B": "delisted", "C": "transient"}[tk], "", ""))
    cnt = M["nodata_cause_counts"]()
    check("원인별 집계", cnt.get("delisted") == 2 and cnt.get("transient") == 1, str(cnt))



# ══════════════════════════════════════════════════════════════════════════
# F. A-2c 선제 상장상태 점검
# ══════════════════════════════════════════════════════════════════════════
# 이 기능이 조용히 고장 나는 방식은 미수신과 다르다. 미수신은 "알려야 할 걸 안
# 알리는" 쪽이지만, 선제 점검은 **"알리지 말아야 할 걸 알리는"** 쪽이 더 위험하다.
# 거래 종목 목록이 잘려 오거나 커버리지 구멍(해외·OTC)에 걸리면 정상 보유
# 종목에 상장폐지 경고가 나간다 — 그 메일은 사용자를 실제로 잘못 움직이게 한다.
def _lv_fresh():
    M["reset_liveness"]()


def _tk(mapping):
    return lambda: mapping


def _uni(symbols):
    return lambda: set(symbols)


def _probe_map(m):
    def fn(t):
        return m.get(t, ("transient", "", ""))
    return fn


_BIG = {f"T{i}" for i in range(6000)}


def test_liveness():
    print("\n── F. A-2c 선제 상장상태 점검")

    # F-1 정상 — 부재 1종이 profile 로 사망 확정
    _lv_fresh()
    calls = M["run_liveness_scan"](
        universe_fn=_uni(_BIG | {"AAPL", "MSFT"}),
        tickers_fn=_tk({"yab": {"AAPL": "워치리스트", "DEAD": "보유"}}),
        probe=_probe_map({"DEAD": ("delisted", "Dead Co", "")}))
    check("F-1 사망 1종 확정 · 2콜(목록1+profile1)", calls == 2, f"calls={calls}")
    check("F-1 경고 1건", M["liveness_total"]() == 1)
    check("F-1 보유 표기 유지",
          M["liveness_for_user"]("yab")[0][3] == "보유")

    # F-2 ★ 최악 — 목록 미수신. 전 종목이 사망으로 뒤집히면 절대 안 된다
    _lv_fresh()
    calls = M["run_liveness_scan"](
        universe_fn=lambda: None,
        tickers_fn=_tk({"yab": {"AAPL": "보유", "MSFT": "보유"}}),
        probe=_probe_map({}))
    check("F-2 목록 미수신 → 경고 0건 (오탐 없음)", M["liveness_total"]() == 0)
    check("F-2 목록 미수신 → profile 미호출(1콜)", calls == 1, f"calls={calls}")

    # F-3 ★ 목록이 잘려 옴 — 하한선 가드
    _lv_fresh()
    calls = M["run_liveness_scan"](
        universe_fn=_uni({"AAPL"}),
        tickers_fn=_tk({"yab": {"AAPL": "보유", "MSFT": "보유", "TSLA": "보유"}}),
        probe=_probe_map({"MSFT": ("delisted", "", ""), "TSLA": ("delisted", "", "")}))
    check("F-3 목록 이상(하한 미달) → 경고 0건", M["liveness_total"]() == 0)
    check("F-3 목록 이상 → profile 미호출", calls == 1, f"calls={calls}")

    # F-4 부재 과다 — 커버리지 구멍이지 떼죽음이 아니다
    _lv_fresh()
    many = {f"X{i}": "보유" for i in range(12)}   # 표본 ≥ _LIVENESS_MIN_SAMPLE
    calls = M["run_liveness_scan"](
        universe_fn=_uni(_BIG),
        tickers_fn=_tk({"yab": many}),
        probe=_probe_map({f"X{i}": ("delisted", "", "") for i in range(12)}))
    check("F-4 부재 100% → 경고 0건 (커버리지로 판단)", M["liveness_total"]() == 0)
    check("F-4 부재 과다 → profile 미호출", calls == 1, f"calls={calls}")

    # F-4b ★ 작은 표본 — 비율이 높아도 게이트가 진짜 상폐를 삼키면 안 된다
    _lv_fresh()
    M["run_liveness_scan"](
        universe_fn=_uni(_BIG | {"AAPL"}),
        tickers_fn=_tk({"yab": {"AAPL": "보유", "DEAD": "보유"}}),
        probe=_probe_map({"DEAD": ("delisted", "Dead Co", "")}))
    check("F-4b 표본 2종(부재 50%) → 비율 게이트 미적용, 경고 1건",
          M["liveness_total"]() == 1)

    # F-5 ★ 부재인데 profile 이 '거래 중' — 부재만으로 단정하지 않는다
    _lv_fresh()
    M["run_liveness_scan"](
        universe_fn=_uni(_BIG),
        tickers_fn=_tk({"yab": {"OTCX": "보유"}}),
        probe=_probe_map({"OTCX": ("transient", "Live Co", "")}))
    check("F-5 부재+거래중 → 경고 0건 (한 방향 판정)", M["liveness_total"]() == 0)

    # F-6 unknown(402 등)도 알리지 않는다
    _lv_fresh()
    M["run_liveness_scan"](
        universe_fn=_uni(_BIG),
        tickers_fn=_tk({"yab": {"AAA.KS": "보유"}}),
        probe=_probe_map({"AAA.KS": ("unknown", "", "플랜 미포함")}))
    check("F-6 원인 불명 → 경고 0건", M["liveness_total"]() == 0)

    # F-7 호출 상한
    _lv_fresh()
    lots = {f"D{i}": "보유" for i in range(40)}
    calls = M["run_liveness_scan"](
        universe_fn=_uni(_BIG | {f"L{i}" for i in range(200)}),
        tickers_fn=_tk({"yab": dict(lots, **{f"L{i}": "보유" for i in range(200)})}),
        probe=_probe_map({f"D{i}": ("delisted", "", "") for i in range(40)}))
    check("F-7 profile 상한 25 준수", calls <= 1 + M["_LIVENESS_PROFILE_MAX"],
          f"calls={calls}")

    # F-8 사용자 격리
    _lv_fresh()
    M["run_liveness_scan"](
        universe_fn=_uni(_BIG),
        tickers_fn=_tk({"yab": {"DEAD": "보유"}, "guest": {"GONE": "워치리스트"}}),
        probe=_probe_map({"DEAD": ("delisted", "", ""), "GONE": ("gone", "", "")}))
    check("F-8 유저 격리 — yab 1건", M["liveness_weight"]("yab") == 1)
    check("F-8 유저 격리 — guest 1건", M["liveness_weight"]("guest") == 1)
    check("F-8 교차 오염 없음",
          M["liveness_for_user"]("yab")[0][0] == "DEAD"
          and M["liveness_for_user"]("guest")[0][0] == "GONE")

    # F-9 요일 게이트
    import datetime as _dt
    fri = _dt.datetime(2026, 8, 21)   # 금
    mon = _dt.datetime(2026, 8, 24)   # 월
    check("F-9 금요일에 실행", M["liveness_due"](fri) is True)
    check("F-9 월요일엔 미실행", M["liveness_due"](mon) is False)
    check("F-9 강제 플래그는 요일 무시", M["liveness_due"](mon, forced=True) is True)

    # F-10 렌더 — 보유 포함 여부로 문구가 갈린다
    _lv_fresh()
    M["run_liveness_scan"](
        universe_fn=_uni(_BIG),
        tickers_fn=_tk({"yab": {"DEAD": "보유"}}),
        probe=_probe_map({"DEAD": ("delisted", "Dead Co", "")}))
    html = M["render_liveness_html"]("yab")
    check("F-10 보유 경고 문구", "매도 신호가 영구히" in html)
    check("F-10 경고 없는 유저는 빈 문자열", M["render_liveness_html"]("nobody") == "")


# ══════════════════════════════════════════════════════════════════════════
# F2. 목록 조회 함수의 계약 — None 과 빈 집합을 구분하는가
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ 이 검사는 run_liveness_scan 의 하한선 가드(_LIVENESS_MIN_UNIVERSE)와
#    **의도적으로 중복**이다. 목록 조회가 실패했을 때 빈 집합을 돌려주면
#    호출부가 '전 종목 부재'로 읽어 모든 보유에 사망 경고를 보낸다.
#    지금은 하한선이 그걸 막지만, 하한선을 '중복이니 지우자'고 없애는 순간
#    이 결함이 되살아난다. 둘 다 못으로 박아둔다.
#
#    이 함수만 네트워크를 만지므로 가짜 fetcher 를 물려 따로 실행한다.
#
#    2026-08-26 락스텝: run_watchlist_alerts 가 requests.get → fh.fmp_get_ex 로
#    전환됐다. 하네스도 같이 바꾸지 않으면 exec 네임스페이스에 fh 가 없어
#    **NameError 로 F2 전체가 깨진다.** 두 파일은 반드시 함께 배포한다.
def _extract_fetcher():
    path = None
    for cand in (os.path.join(_HERE, "run_watchlist_alerts.py"),
                 os.path.join(_ROOT, "automation", "run_watchlist_alerts.py"),
                 os.path.join(_ROOT, "run_watchlist_alerts.py")):
        if os.path.exists(cand):
            path = cand
            break
    tree = ast.parse(open(path, encoding="utf-8").read())
    node = None
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_fmp_actively_trading_symbols":
            node = n
    if node is None:
        print("❌ _fmp_actively_trading_symbols 를 찾을 수 없습니다.")
        sys.exit(2)
    return node, path


class _FakeResp:
    def __init__(self, status=200, payload=None, raise_json=False):
        self.status_code = status
        self._p = payload
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("bad json")
        return self._p


class _FakeFh:
    """fmp_http 대역. fmp_get_ex 는 (응답|None, status, kind) 3-튜플을 돌려준다.

    ⚠️ 실물과 계약이 어긋나면 이 스위트는 통과하는데 실운용은 깨진다. 아래
    _assert_fh_contract() 가 실제 fmp_http 를 import 해 반환 길이를 대조한다.
    """

    def __init__(self, resp=None, status=200, kind="ok", exc=None):
        self._resp, self._status, self._kind, self._exc = resp, status, kind, exc

    def fmp_get_ex(self, *a, **k):
        if self._exc:
            raise self._exc
        return self._resp, self._status, self._kind


def test_fetcher_contract():
    print("\n── F2. 목록 조회 계약 (None ≠ 빈 집합)")
    node, _path = _extract_fetcher()

    def run(fake):
        ns = {"print": lambda *a, **k: None, "fh": fake,
              "_FMP_BASE": "https://x", "FMP_API_KEY": "k", "_FMP_TIMEOUT": 5}
        mod = ast.Module(body=[node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(mod), "<f>", "exec"), ns)
        return ns["_fmp_actively_trading_symbols"]()

    # SSOT 전환 자체를 못으로 박는다. 원시 requests 로 되돌리면 여기서 걸린다.
    _names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    check("F2-0 원시 requests 를 쓰지 않는다 (fmp_http 경유)",
          "requests" not in _names and "fh" in _names,
          repr(sorted(_names & {"requests", "fh"})))

    check("F2-1 fetcher 자체가 예외를 던져도 → None (빈 집합 아님)",
          run(_FakeFh(exc=RuntimeError("boom"))) is None)
    check("F2-1b 네트워크 예외를 kind 로 받아도 → None",
          run(_FakeFh(resp=None, status=0, kind="exception")) is None)
    check("F2-2 HTTP 402 → None",
          run(_FakeFh(resp=None, status=402, kind="plan_limited")) is None)
    # 재시도까지 소진한 429. 전환 전에는 존재하지 않던 실패 모드다.
    check("F2-2b 레이트리밋 소진 → None",
          run(_FakeFh(resp=None, status=429, kind="rate_limited")) is None)
    check("F2-3 JSON 파싱 실패 → None",
          run(_FakeFh(resp=_FakeResp(200, raise_json=True))) is None)
    check("F2-4 응답 타입 이상(dict) → None",
          run(_FakeFh(resp=_FakeResp(200, {"a": 1}))) is None)
    got = run(_FakeFh(resp=_FakeResp(200, [{"symbol": "aapl"}, {"symbol": "MSFT"}])))
    check("F2-5 정상 → 대문자 심볼 집합", got == {"AAPL", "MSFT"}, repr(got))
    got2 = run(_FakeFh(resp=_FakeResp(200, ["aapl", "msft"])))
    check("F2-6 원소가 문자열이어도 견딤", got2 == {"AAPL", "MSFT"}, repr(got2))
    _assert_fh_contract()


def _assert_fh_contract():
    """가짜 fh 가 실물 fmp_http 와 같은 계약인지 대조.

    대역(mock)은 반드시 실물에서 멀어진다. 실물 fmp_get_ex 의 반환 원소 수가
    바뀌면 이 스위트는 계속 초록불인데 실운용만 깨진다 — 그 침묵을 막는다.
    """
    try:
        import fmp_http as _real
    except Exception as e:
        check("F2-7 실물 fmp_http 계약 대조", False, f"import 실패: {e}")
        return
    src = inspect.getsource(_real.fmp_get_ex)
    arities = {len(n.value.elts) for n in ast.walk(ast.parse(textwrap.dedent(src)))
               if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)}
    check("F2-7 실물 fmp_get_ex 가 3-튜플만 반환 (가짜 fh 와 동일 계약)",
          arities == {3}, repr(sorted(arities)))


# ══════════════════════════════════════════════════════════════════════════
# G. 발송 게이트 — 실제로 돌려본다
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ 이 검사가 없어서 **기존 결함을 92/92 통과 상태로 놓쳤다.**
#    dispatch_radar_emails 최상단의 `if not total: return` 는 발동 0건이면
#    즉시 반환한다. main 이 `if total or nodata_total():` 로 불러도 여기서
#    잘린다 — 미수신만 있는 날의 메일이 나가지 않았다.
#    계약(AST) 검사는 '호출 여부'만 보므로 조건식을 못 잡는다. 실행해야 잡힌다.
def test_dispatch_gate():
    print("\n── G. 발송 게이트 (실제 실행)")
    _fresh()
    _lv_fresh()
    M["SENT"].clear()

    # G-1 미수신만 있고 발동 0건 → 메일이 나가야 한다
    M["record_attempt"]()
    M["record_nodata"]("yab", "DEAD", "보유")
    M["dispatch_radar_emails"]({}, {}, "2026-08-22", "🔔", "매매 레이더")
    check("G-1 발동 0건 + 미수신 1건 → 발송됨", len(M["SENT"]) == 1,
          f"sent={len(M['SENT'])}")

    # G-2 선제 경고만 있고 발동·미수신 0건 → 메일이 나가야 한다
    _fresh()
    _lv_fresh()
    M["SENT"].clear()
    M["run_liveness_scan"](
        universe_fn=_uni(_BIG),
        tickers_fn=_tk({"yab": {"DEAD": "보유"}}),
        probe=_probe_map({"DEAD": ("delisted", "Dead Co", "")}))
    M["dispatch_radar_emails"]({}, {}, "2026-08-22", "🔔", "매매 레이더")
    check("G-2 선제 경고만 → 발송됨", len(M["SENT"]) == 1, f"sent={len(M['SENT'])}")
    check("G-2 본문에 경고 섹션 포함",
          bool(M["SENT"]) and "상장 상태 경고" in M["SENT"][0]["html"])

    # G-3 아무것도 없으면 발송하지 않는다 (반대 방향 — 늑대소년 방지)
    _fresh()
    _lv_fresh()
    M["SENT"].clear()
    M["dispatch_radar_emails"]({}, {}, "2026-08-22", "🔔", "매매 레이더")
    check("G-3 전부 0건 → 발송 안 함", len(M["SENT"]) == 0, f"sent={len(M['SENT'])}")


# ══════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 70)
    print("데이터 미수신 감지(A-2a) + 원인 판정(A-2b) 회귀 검증")
    print("  네트워크·시트·시크릿 없음 · 부작용 없음")
    print("=" * 70)

    test_record()
    test_systemic()
    test_render()
    test_cause()
    test_liveness()
    test_fetcher_contract()
    test_dispatch_gate()
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
