"""diag_startup.py — 시작 페이지 + 실적 레이더 지연 계산 회귀.

app.py 를 **소스로 검사**한다(streamlit 없이 import 할 수 없으므로).
네트워크·시트 접근 없음.

지키려는 위험
─────────────
  1. 메뉴 인덱스 밀림
       _MAIN_NAV_OPTIONS 중간에 항목을 끼우면 _MAIN_NAV_OPTIONS[n] 분기가
       전부 한 칸씩 밀려 엉뚱한 페이지가 열린다. 새 항목은 **맨 뒤 append**,
       표시 순서만 _nav_opts 에서 재배치해야 한다.
  2. 시작 페이지가 무거워짐
       착지 지점을 옮긴 이유가 사라진다. FMP 호출·가격 이력·DRG 계산이
       _render_home 안에 들어오면 안 된다.
  3. 정의보다 앞선 사용
       중첩 함수였던 _pv_f/_pv_money 를 지연 게이트에서 쓰다 NameError 를
       낼 뻔했다. 모듈 레벨이어야 하고 정의가 사용보다 앞서야 한다.
  4. 지연 게이트가 캐시 키를 잘못 잡음
       종목을 추가하거나 날이 바뀌었는데 어제 판정을 그대로 보여주면
       낡은 판정이 된다 — 없는 판정보다 위험하다.

실행
────
    python automation/diag_startup.py
"""
import ast
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))

_APP = next((p for p in (os.path.join(_ROOT, "app.py"),
                         os.path.join(_HERE, "app.py"))
             if os.path.exists(p)), None)

_fail, _pass = [], 0


def check(name, cond, detail=""):
    global _pass
    if cond:
        _pass += 1
        print(f"  ✅ {name}")
    else:
        _fail.append(name)
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


if _APP is None:
    print("❌ app.py 를 찾지 못했다")
    sys.exit(1)

SRC = open(_APP, encoding="utf-8").read()
TREE = ast.parse(SRC)


def fn_src(name):
    """모듈 레벨 함수의 소스. 없으면 None."""
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n) or ""
    return None


TOPLEVEL = {n.name for n in TREE.body if isinstance(n, ast.FunctionDef)}

print("\n[1] 메뉴 구성 — 인덱스가 밀리면 안 된다")
_nav = next((n for n in TREE.body if isinstance(n, ast.Assign)
             and getattr(n.targets[0], "id", "") == "_MAIN_NAV_OPTIONS"), None)
check("_MAIN_NAV_OPTIONS 존재", _nav is not None)
if _nav is not None:
    opts = [e.value for e in _nav.value.elts]
    check("항목 17개", len(opts) == 17, f"got {len(opts)}")
    # 기존 인덱스 분기가 의존하는 자리들 — 하나라도 밀리면 오작동한다
    for idx, want in ((0, "Daily Risk Gauge"), (6, "포트폴리오 매도 레이더"),
                      (7, "Buy Watchlist"), (12, "사용 가이드"),
                      (13, "매매 복기"), (14, "내 설정"), (15, "실적 레이더")):
        check(f"index {idx} = {want}", want in opts[idx], f"got {opts[idx]!r}")
    check("시작 페이지가 맨 뒤(index 16)", "시작" in opts[16], f"got {opts[16]!r}")
    check("항목이 전부 유일", len(set(opts)) == len(opts))

print("\n[2] 표시 순서 재배치 — 시작이 맨 앞")
check("_nav_opts 에서 시작을 0번으로 삽입",
      "_nav_opts.insert(0, _home_label)" in SRC)
check("_MAIN_NAV_OPTIONS[16] 을 참조", "_MAIN_NAV_OPTIONS[16]" in SRC)
check("라우팅 분기 존재",
      re.search(r"if main_nav == _MAIN_NAV_OPTIONS\[16\]", SRC) is not None)
check("_render_home 호출", "_render_home()" in SRC)

print("\n[3] 시작 페이지는 가벼워야 한다")
home = fn_src("_render_home")
check("_render_home 이 모듈 레벨", home is not None)
if home:
    # 무거운 것들이 들어오면 착지 지점을 옮긴 의미가 없다
    for bad in ("compute_daily_risk_gauge", "_drg_get",
                "cached_timing_price_history", "fetch_latest_prices_for_tickers",
                "compute_account_context", "load_portfolio"):
        check(f"{bad} 미호출", bad not in home,
              "시작 화면이 무거워진다")
    check("사용 가이드 안내 포함", "사용 가이드" in home)
    check("가이드로 이동 index 12",
          'st.session_state["main_nav_idx"] = 12' in home)
    check("시트 읽기는 허용 — 실적 캘린더 사용",
          "load_earnings_calendar" in home)

print("\n[4] 표시 헬퍼 — 모듈 레벨 + 정의가 사용보다 앞")
for f in ("_pv_num", "_pv_f", "_pv_money", "_boot_mark", "_render_home"):
    check(f"{f} 모듈 레벨", f in TOPLEVEL)
for f in ("_pv_f", "_pv_money", "_render_home", "_boot_mark"):
    d = SRC.find(f"def {f}")
    u = SRC.find(f"{f}(", d + len(f) + 5)
    check(f"{f} 정의 < 첫 사용", d != -1 and (u == -1 or d < u))
# 중첩 정의가 남아 있으면 안쪽 것이 그림자를 만든다
check("_pv_f 중첩 정의 잔재 없음",
      SRC.count("def _pv_f(") == 1, f"{SRC.count('def _pv_f(')}개")
check("_pv_money 중첩 정의 잔재 없음", SRC.count("def _pv_money(") == 1)

print("\n[5] 실적 레이더 지연 게이트")
check("게이트 존재", "_earn_calc_key" in SRC and "_earn_calc_now" in SRC)
check("계산 버튼 존재", "⚡ 조치 판정 계산" in SRC)
# 캐시 키에 날짜와 종목집합이 들어가야 낡은 판정이 남지 않는다
m = re.search(r"_e_ck = \((.*?)\)\n", SRC, re.S)
check("캐시 키 구성 발견", m is not None)
if m:
    key = m.group(1)
    check("캐시 키에 사용자", "_euid" in key)
    check("캐시 키에 날짜", "_e_today" in key)
    check("캐시 키에 종목집합", "_uni" in key)

# 게이트가 무거운 계산보다 앞에 있어야 의미가 있다.
# ⚠️ 전역 find 를 쓰면 안 된다 — fetch_latest_prices_for_tickers 는 다른 탭에도
#    있어서 앞쪽 것을 잡고 오판한다. 실적 레이더 섹션 안으로 범위를 좁힌다.
i_sec = SRC.find("elif main_nav == _MAIN_NAV_OPTIONS[15]:")
check("실적 레이더 섹션 발견", i_sec != -1)
SEC = SRC[i_sec:]
i_gate = SEC.find("_e_ck = (")
for later in ("# ── 계좌별 한도 해석 ──", "fetch_latest_prices_for_tickers(tuple",
              "cached_timing_price_history(_tk)"):
    j = SEC.find(later)
    check(f"게이트가 '{later[:24]}...' 보다 앞",
          i_gate != -1 and j != -1 and i_gate < j)

# _uni/_cal 은 게이트보다 앞에서 만들어져야 한다(게이트가 참조하므로)
for earlier in ("_cal = load_earnings_calendar()", "_uni = [u for u in _uni"):
    j = SEC.find(earlier)
    check(f"'{earlier[:26]}...' 가 게이트보다 앞",
          j != -1 and j < i_gate)

print("\n[6] 지연 화면이 판정을 사칭하지 않는가")
seg = SEC[i_gate:SEC.find('st.session_state["_earn_calc_key"] = _e_ck')]
check("판정 미계산 고지", "판정은 아직 계산하지 않았습니다" in seg)
check("예상 변동폭 오해 방지 문구", "과거 실적 반응의 중앙값" in seg)
for bad in ("evaluate_entry_gate", "evaluate_trim", "compute_account_context"):
    check(f"지연 화면에서 {bad} 미호출", bad not in seg)

print("\n[7] 시작 화면 무거운 작업 지연")
# 계측 결과: 로그인 직후 28.67초 중 워치리스트 알림 점검이 24.34초(85%)였다.
# ETF 자동 갱신은 0.003초로 무죄. 시작 화면에서만 건너뛰고 버튼으로 실행한다.
check("_on_home 판정 존재", "_on_home = (" in SRC)
check("_on_home 이 index 16 을 본다",
      re.search(r"_on_home = \(.*_MAIN_NAV_OPTIONS\[16\]", SRC, re.S) is not None)
i_on = SRC.find("_on_home = (")
# _on_home 은 이제 ETF 자동 갱신 스킵에만 쓴다.
# 워치리스트 게이트는 소비 탭 판정(_wl_needed)으로 옮겼다 — [9] 참고.
for user in ("and not _on_home",):
    j = SRC.find(user)
    check(f"'{user[:22]}...' 가 _on_home 정의보다 뒤", j != -1 and i_on < j)

# 건너뛸 때 _watchlist_alert_checked 를 세우면 안 된다 —
# 세우면 다른 탭으로 가도 영영 점검이 안 돌아 알림을 놓친다.
i_if = SRC.find("if (_wl_full or _wl_part) and not _wl_defer:")
check("워치리스트 지연 게이트 존재", i_if != -1)
if i_if != -1:
    i_flag = SRC.find('st.session_state["_watchlist_alert_checked"] = True')
    check("체크 플래그 설정이 게이트 안쪽", i_flag > i_if)

home7 = fn_src("_render_home") or ""
check("시작 화면에 알림 확인 버튼", "_wl_check_now" in home7)
check("소요 시간 사전 고지", "20초" in home7)
check("놓치지 않는다는 안내", "5PM" in home7 or "이메일" in home7)

print("\n[8] 사용 가이드 — 신규 기능이 문서화됐는가")
# 시작 화면이 "가이드부터 보라"고 안내하므로 가이드가 낡으면 안내가 해가 된다.
i_g = SRC.find("elif main_nav == _MAIN_NAV_OPTIONS[12]:")
j_g = SRC.find("elif main_nav == _MAIN_NAV_OPTIONS[14]:", i_g)
GUIDE = SRC[i_g:j_g] if (i_g != -1 and j_g > i_g) else ""
check("가이드 구간 발견", len(GUIDE) > 1000)
for topic, needle in (("🏠 시작", "**시작** — 로그인 직후 착지"),
                      ("📅 실적 레이더", "**실적 레이더** — 방향 맞히기"),
                      ("⚙️ 내 설정", "**내 설정** — 알림 토글"),
                      ("👑 관리자·리마인더", "개발 리마인더"),
                      ("매도 사유 태그", "매도 기록 — 사유 태그")):
    check(f"{topic} 문서화", needle in GUIDE)

# 자동 이메일 스케줄 표가 실제 워크플로와 맞는가 — 5PM 에 실적, 주말에 스캐너
check("5PM 스케줄에 실적 레이더", "run_earnings_watch" in GUIDE)
check("주말 스케줄에 스캐너", "run_scanner_scan" in GUIDE)
check("주말 스케줄에 주간요약", "run_weekly_report" in GUIDE)
check("가이드 버전 v4 표기", "v4" in GUIDE)

# 게스트 가이드도 같이 갱신됐는가
GUEST = fn_src("_render_guest_guide") or ""
check("게스트 가이드에 시작 화면", "🏠 시작" in GUEST)
check("게스트 가이드에 실적 레이더", "실적 레이더" in GUEST)
check("게스트 가이드에 매도 사유", "매도 사유" in GUEST)

print("\n[9] 알림 점검 — 소비 탭에서만 자동 실행")
# 2026-08-18 정정: 시작 화면에서만 건너뛰었더니 24초가 사라진 게 아니라
# '첫 탭 이동' 시점으로 옮겨졌다. 계산이 전혀 없는 사용 가이드에서 20초를 맞았다.
# 알림 결과를 쓰는 곳은 index 7(Buy Watchlist) 하나뿐이다.
check("소비 탭 판정 존재", "_wl_needed" in SRC)
check("index 7 을 본다",
      re.search(r"_wl_needed = \(_nav_now == _MAIN_NAV_OPTIONS\[7\]\)", SRC)
      is not None)
check("게이트가 _wl_needed 를 쓴다",
      re.search(r"_wl_defer = \(not _wl_needed\)", SRC) is not None)
i_need = SRC.find("_wl_needed = (")
i_use = SRC.find("_wl_defer = (not _wl_needed)")
check("정의가 사용보다 앞", i_need != -1 and i_use != -1 and i_need < i_use)

home9 = fn_src("_render_home") or ""
check("시작 화면 안내가 소비 탭을 지목", "Buy Watchlist & Alert` 탭" in home9)

print("\n[10] 매도 레이더 — 계측 누락 구간 없음")
i_pf = SRC.find("elif main_nav == _MAIN_NAV_OPTIONS[6]:")
j_pf = SRC.find("elif main_nav == _MAIN_NAV_OPTIONS[7]:", i_pf)
PF = SRC[i_pf:j_pf] if (i_pf != -1 and j_pf > i_pf) else ""
check("매도 레이더 구간 발견", len(PF) > 1000)
for span in ("PF 배당 스캔", "PF 일봉 프리페치", "PF 매도레이더 계산",
             "PF 실적 캘린더"):
    check(f"'{span}' 계측됨", f'_timed("{span}")' in PF)

# 사이드바는 모든 탭에서 그려진다 — 여기서 FMP 를 부르면 문서 페이지까지 느려진다.
check("사이드바 계좌 컨텍스트 계측", '_timed("사이드바 계좌 컨텍스트")' in SRC)

print("\n[11] 실적 캘린더 — 종목별 캐시")
# 티커 튜플 전체가 캐시 키면 계좌 필터를 하나 켜고 끌 때마다 전체를 다시 받는다.
check("종목 단위 캐시 함수 존재", "def _earnings_cal_one" in SRC)
check("종목별 캐시에 ttl", re.search(
    r"@st\.cache_data\(ttl=\d+[^)]*\)\s*\ndef _earnings_cal_one", SRC) is not None)
check("smart 래퍼 존재", "def fetch_earnings_calendar_smart" in SRC)
check("매도 레이더가 smart 를 쓴다",
      "fetch_earnings_calendar_smart(earn_tickers)" in PF)
check("옛 일괄 호출 잔재 없음",
      "fetch_earnings_calendar(earn_tickers)" not in PF)

print("\n[12] 워치리스트 알림 점검 — 구간 계측")
# 24초가 어디서 나는지 모른 채 손대지 않는다. 후보 셋을 나눠 잰다.
for span in ("WL 가격 조회", "WL 일봉 프리페치"):
    check(f"'{span}' 계측됨", f'_timed("{span}")' in SRC)
check("'WL 레짐 평가' 계측됨", '"WL 레짐 평가"' in SRC)
check("analyze_ticker 횟수 기록", "_wl_an_n" in SRC)
# _wl_items 가 비어도 계측 블록이 참조하므로 미리 초기화돼야 한다
i_init = SRC.find("_wl_an_n, _wl_t0 = 0")
i_use = SRC.find('_b_wl["WL 레짐 평가"]')
check("계측 변수 사전 초기화", i_init != -1 and i_use != -1 and i_init < i_use)

print("\n[13] 현재가 조회 — 배치 청크 + 폴백 병렬")
# 실측: 워치리스트 59종목에서 60콜 17.1초. 배치 1회 실패 + 단일 quote 59회 '순차'.
# 같은 함수를 사이드바도 쓴다(13콜 3.8초) — 여기를 고치면 양쪽이 같이 빨라진다.
i_q = SRC.find("def fetch_latest_prices_for_tickers")
j_q = SRC.find("\ndef ", i_q + 10)
Q = SRC[i_q:j_q] if i_q != -1 else ""
check("함수 구간 발견", len(Q) > 500)
check("배치를 청크로 나눈다", "_FMP_BATCH_CHUNK" in Q)
check("배치 실패 사유를 남긴다", "_batch_fail" in Q)
check("실패 시 경고 출력", "batch-quote 실패" in Q)
check("단일 폴백이 병렬", "_cf.ThreadPoolExecutor(max_workers=_FMP_QUOTE_WORKERS)" in Q)
check("스레드풀 실패 시 순차 폴백", "for tk in missing:" in Q)
# 상수가 정의돼 있고 사용보다 앞서야 한다
for c in ("_FMP_BATCH_CHUNK", "_FMP_QUOTE_WORKERS"):
    d = SRC.find(f"{c} = ")
    check(f"{c} 정의가 사용보다 앞", d != -1 and d < i_q)

print("\n" + "=" * 66)
if _fail:
    print(f"❌ 실패 {len(_fail)}건 / 통과 {_pass}건")
    for n in _fail:
        print(f"   · {n}")
    sys.exit(1)
print(f"✅ 전체 통과 — {_pass}건")
sys.exit(0)
