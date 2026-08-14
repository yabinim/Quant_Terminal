# -*- coding: utf-8 -*-
"""diag_market_gate.py — 시장 진입 게이트 회귀 검증 (수동 전용).

검증 대상
  1) market_warnings — 백테스트 경로가 이관 전과 **비트 단위로 동일**한가
  2) market_warnings — 라이브 경로가 워밍업 구간을 NaN 으로 되살리는가
  3) market_gate_status — 임계값 판정 / fail-open
  4) evaluate_alert_transitions — 동결이 entry 만 막고 보유 관리는 통과시키는가
  5) 동결 재개 — 상태 보존 후 게이트가 풀리면 이어서 발동하는가
  6) users_core — Gate_Market 스키마 / 기본값 / 폴백 오염 없음
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

# automation/ 에서 실행되든 루트에서 실행되든 공용 모듈을 찾게 한다(기존 diag 관례).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _find(name: str) -> str:
    """소스 파일 실경로. 루트 → automation/ 순으로 찾는다(실행 위치 무관)."""
    for base in (_ROOT, _HERE, os.getcwd()):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(name)


import regime_core as rc  # noqa: E402
import users_core as uc  # noqa: E402

FAIL: list[str] = []


def ck(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAIL.append(msg)


def spy(n=900, drift=0.0004, vol=0.010, seed=1, tail_crash=0.0):
    r = np.random.default_rng(seed)
    px = 100 * np.exp(np.cumsum(r.normal(drift, vol, n)))
    if tail_crash:
        px[-60:] = px[-60] * np.linspace(1.0, 1.0 - tail_crash, 60)
    return pd.Series(px)


def legacy_market_warnings(spy_arr):
    """이관 전 run_signal_backtest._market_warnings 원본 (참조 구현)."""
    c = pd.Series(spy_arr, dtype=float)
    if c.notna().sum() < 260:
        return None
    ma200 = c.rolling(200, min_periods=200).mean()
    ma50 = c.rolling(50, min_periods=50).mean()
    ret20 = c / c.shift(20) - 1.0
    dd = c / c.rolling(252, min_periods=60).max() - 1.0
    vol20 = c.pct_change().rolling(20).std()
    vol_med = vol20.rolling(252, min_periods=60).median()
    w = ((c < ma200).astype(float) + (c < ma50).astype(float)
         + (ret20 < 0).astype(float) + (dd < -0.10).astype(float)
         + (vol20 > vol_med * 1.5).astype(float))
    return w.fillna(2.0).to_numpy(dtype=float)


print("=" * 68)
print("1) market_warnings — 백테스트 경로 비트 단위 동일성")
print("=" * 68)
same_all = True
for seed in range(8):
    r = np.random.default_rng(seed)
    n = int(r.integers(300, 1600))
    px = 100 * np.exp(np.cumsum(r.normal(0.0003, 0.011, n)))
    if seed % 3 == 1:
        px[:9] = np.nan
    a = legacy_market_warnings(px)
    b = rc.market_warnings(px, fill_neutral=2.0)
    same_all &= np.array_equal(a, b, equal_nan=True)
ck(same_all, "8개 시나리오에서 이관 전 구현과 완전 일치 (과거 백테스트 비교 가능)")
ck(rc.market_warnings(pd.Series([1.0] * 100)) is None, "표본 부족(<260) → None")
ck(rc.market_warnings(None) is None, "입력 None → None")

print()
print("=" * 68)
print("2) market_warnings — 라이브 경로 NaN 보존")
print("=" * 68)
live = rc.market_warnings(spy())
bt = rc.market_warnings(spy(), fill_neutral=2.0)
ck(np.isnan(live[:150]).all(), "워밍업 구간이 NaN 으로 남음 (fail-open 감지 가능)")
ck(not np.isnan(bt).any(), "백테스트 경로는 NaN 없음 (기존 동작 유지)")
ck(np.isfinite(live[-1]), "충분한 데이터에서 최신값은 유한")
_fin = np.isfinite(live)
ck(np.array_equal(live[_fin], bt[_fin]), "유효 구간의 값은 두 경로가 동일")

print()
print("=" * 68)
print("3) market_gate_status — 판정 / fail-open")
print("=" * 68)
# 결정론적 단조 상승 — 5개 신호가 모두 거짓이 되는 유일하게 확실한 구성.
#   랜덤워크는 우상향이어도 50일선 하회 + 20일 수익률 마이너스로 경고 2개가 쉽게 뜬다
#   (임계값 2 의 차단률이 21% 로 나온 것과 정합).
_calm_px = pd.Series(100 * np.power(1.0005, np.arange(900)))
calm = rc.market_gate_status(_calm_px)
crash = rc.market_gate_status(spy(seed=4, drift=0.0009, vol=0.007, tail_crash=0.28))
print(f"     상승장: {calm['reason']}")
print(f"     급락장: {crash['reason']}")
ck(calm["available"] and crash["available"], "두 시나리오 모두 판정 가능")
ck(crash["count"] > calm["count"], "급락 시 경고 개수가 증가")
ck(calm["count"] == 0, f"단조 상승 → 경고 0개 (실제 {calm['count']})")
ck(crash["blocked"] is True, "급락장 → 동결")
ck(calm["blocked"] is False, "상승장 → 통과")

short = rc.market_gate_status(pd.Series([1.0] * 50))
ck(short["available"] is False and short["blocked"] is False,
   "데이터 부족 → available=False, blocked=False (fail-open)")
none_g = rc.market_gate_status(None)
ck(none_g["blocked"] is False, "SPY 없음 → 차단하지 않음 (조용한 알림 소실 방지)")
ck(rc.market_gate_status(spy(), threshold=99)["blocked"] is False, "임계값 인자 반영")
ck(rc.market_gate_status(spy(), threshold=0)["blocked"] is True, "임계 0 → 항상 동결")
ck(rc.MARKET_GATE_THRESHOLD == 2, "기본 임계값 2 (결정 1)")

# 경계 고정: 경고가 **정확히 임계값과 같을 때** 동결되어야 한다.
#   >= 를 > 로 바꾸면 임계 2 가 사실상 임계 3 이 된다 — 백테스트에서 두 모드는
#   초과수익 +8.86% vs +6.55% 로 다른 결과였다. 조용히 다른 모드가 되면 안 된다.
_exact2 = None
for _sd in range(400):
    _st = rc.market_gate_status(spy(seed=_sd), threshold=2)
    if _st.get("available") and _st.get("count") == 2.0:
        _exact2 = _st
        break
ck(_exact2 is not None, "경고 정확히 2개인 시나리오 확보")
if _exact2:
    ck(_exact2["blocked"] is True, "경고 == 임계값 → 동결 (>= 경계, > 아님)")
    _one = None
    for _sd in range(400):
        _st = rc.market_gate_status(spy(seed=_sd), threshold=3)
        if _st.get("available") and _st.get("count") == 2.0:
            _one = _st
            break
    ck(_one is not None and _one["blocked"] is False,
       "경고 2개 · 임계 3 → 통과 (임계값이 실제로 반영됨)")
ck(len(rc.MARKET_WARNING_LABELS) == rc.MARKET_WARNING_MAX, "라벨 개수 = 신호 개수")

print()
print("=" * 68)
print("4) 동결 — entry 만 막고 보유 관리는 통과")
print("=" * 68)


def trend(n=320, seed=11, up=True):
    r = np.random.default_rng(seed)
    d = 0.0016 if up else -0.0022
    px = 60 * np.exp(np.cumsum(r.normal(d, 0.013, n)))
    idx = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame({"Close": px, "High": px * 1.012, "Low": px * 0.988,
                         "Open": px, "Volume": 1e6}, index=idx)


ENTRY_EVENTS = {"entry", "entry_invalid"}
found_entry = found_hold = False
for seed in range(60):
    for up in (True, False):
        h = trend(seed=seed, up=up)
        an = rc.analyze_ticker(h)
        px = float(h["Close"].iloc[-1])
        enabled = ["entry", "entry_invalid", "risk", "exit"]
        # 2일 확정을 채우기 위해 동일 조건으로 두 번 평가
        st1 = ""
        for day in ("2026-08-10", "2026-08-11"):
            fired, st1 = rc.evaluate_alert_transitions(
                an, enabled, st1, today_str=day, price=px)
        if not fired:
            continue
        kinds = {e.get("event") for e in fired}
        st0 = ""
        for day in ("2026-08-10", "2026-08-11"):
            fired_b, st0 = rc.evaluate_alert_transitions(
                an, enabled, st0, today_str=day, price=px, entry_blocked=True)
        kinds_b = {e.get("event") for e in fired_b}
        if kinds & ENTRY_EVENTS and not found_entry:
            ck(not (kinds_b & ENTRY_EVENTS),
               f"entry 계열 동결됨 (미동결 {sorted(kinds & ENTRY_EVENTS)} → 동결 시 없음)")
            found_entry = True
        if kinds - ENTRY_EVENTS and not found_hold:
            ck(bool(kinds_b & (kinds - ENTRY_EVENTS)),
               f"보유 관리 이벤트는 동결과 무관하게 발동 {sorted(kinds - ENTRY_EVENTS)}")
            found_hold = True
    if found_entry and found_hold:
        break
ck(found_entry, "entry 계열 발동 시나리오 확보")
ck(found_hold, "보유 관리 이벤트 발동 시나리오 확보")

print()
print("=" * 68)
print("5) 동결 재개 — 상태 보존")
print("=" * 68)
resumed = False
for seed in range(60):
    h = trend(seed=seed, up=True)
    an = rc.analyze_ticker(h)
    px = float(h["Close"].iloc[-1])
    enabled = ["entry"]
    # 동결 상태로 3일 → 해제 후 2일
    stt = ""
    for day in ("2026-08-05", "2026-08-06", "2026-08-07"):
        f_blk, stt = rc.evaluate_alert_transitions(
            an, enabled, stt, today_str=day, price=px, entry_blocked=True)
        if f_blk:
            break
    else:
        for day in ("2026-08-10", "2026-08-11"):
            f_open, stt = rc.evaluate_alert_transitions(
                an, enabled, stt, today_str=day, price=px)
            if any(e.get("event") == "entry" for e in f_open):
                resumed = True
                break
    if resumed:
        break
ck(resumed, "동결 중 무발동 → 해제 후 정상 발동 (신호 영구 소실 없음)")

print()
print("=" * 68)
print("6) users_core — Gate_Market 스키마")
print("=" * 68)
ck("Gate_Market" in uc.USER_SHEET_COLS, "USER_SHEET_COLS 에 존재")
ck(uc.USER_SHEET_COLS[-1] == "Gate_Market", "맨 뒤 열 (기존 열 위치 불변)")
ck(uc.NCOL == 14 and uc.LAST_COL == "N", f"NCOL=14 / LAST_COL=N (실제 {uc.NCOL}/{uc.LAST_COL})")
ck(uc.alert_column("gate_market") == "Gate_Market",
   "ALERT_KINDS 매핑됨 (누락 시 Alert_Global 로 조용히 폴백)")
ck(uc.alert_column("bogus") == "Alert_Global", "미지 kind 폴백 회귀 없음")
ck("Gate_Market" in uc._MIGRATED_COLS, "마이그레이션 대상에 포함")
ck(uc._DEFAULTS_GUEST["Gate_Market"] == "N", "게스트 기본 N")
ck(uc._DEFAULTS_ADMIN["Gate_Market"] == "N", "관리자도 기본 N (차단형 기능)")
ck(uc.USER_SHEET_COLS[:13][-1] == "Alert_Earnings", "기존 13열 순서 불변")

print()
print("=" * 68)
print("7) run_watchlist_alerts — 게이트 결선 (구조 검증)")
print("=" * 68)
# gspread 미설치 환경에서도 돌아야 하므로 AST 로 결선을 확인한다.
#   이 결선이 빠지면 게이트가 **조용히 아무것도 하지 않는다** — 뮤테이션 테스트에서
#   실제로 미검출된 유일한 항목이었다(2026-08-12). 반드시 고정한다.
import ast

try:
    _wa_src = open(_find("automation/run_watchlist_alerts.py"), encoding="utf-8").read()
    _tree = ast.parse(_wa_src)
    _fns = {n.name: n for n in ast.walk(_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    ck("load_market_gate_users" in _fns, "load_market_gate_users 정의됨")
    ck("eval_watchlist_eod" in _fns, "eval_watchlist_eod 정의됨")

    _ev = _fns.get("eval_watchlist_eod")
    if _ev:
        _args = [a.arg for a in _ev.args.args]
        ck("mkt_gate" in _args and "mkt_users" in _args,
           f"eval_watchlist_eod 가 게이트 인자를 받음 ({_args[-2:]})")
        _ev_src = ast.get_source_segment(_wa_src, _ev) or ""
        # entry_blocked 키워드에 _mblk 가 실제로 결합돼 있는지
        _wired = False
        for _n in ast.walk(_ev):
            if isinstance(_n, ast.Call):
                for _kw in _n.keywords:
                    if _kw.arg == "entry_blocked":
                        _txt = ast.get_source_segment(_wa_src, _kw.value) or ""
                        _wired = "_mblk" in _txt
        ck(_wired, "entry_blocked 에 _mblk 가 결합됨 (게이트 실제 결선)")
        # _mblk 조건식 안에 _mkt_users 검사가 실제로 들어있는지 (토글 무시 방지).
        #   단순 문자열 검사로는 상단 대입문 때문에 통과해버린다 — 조건식만 본다.
        _tog = False
        for _n in ast.walk(_ev):
            if (isinstance(_n, ast.Assign) and _n.targets
                    and isinstance(_n.targets[0], ast.Name)
                    and _n.targets[0].id == "_mblk"):
                _v = _n.value
                _cond = ast.get_source_segment(_wa_src, _v.test) if isinstance(_v, ast.IfExp) else ""
                _tog = "_mkt_users" in (_cond or "") and "_mkt_blocked" in (_cond or "")
        ck(_tog, "_mblk 조건식이 _mkt_blocked AND _mkt_users 를 함께 검사 (토글 무시 방지)")

    # 호출부가 실제로 게이트를 만들어 넘기는지
    # _mkt_blocked 가 게이트 결과에서 파생되는지 (상수로 못박히면 기능이 죽는다)
    _derived = False
    for _n in ast.walk(_fns.get("eval_watchlist_eod") or ast.Module(body=[], type_ignores=[])):
        if (isinstance(_n, ast.Assign) and _n.targets
                and isinstance(_n.targets[0], ast.Name)
                and _n.targets[0].id == "_mkt_blocked"):
            _txt = ast.get_source_segment(_wa_src, _n.value) or ""
            _derived = "_mkt" in _txt and "blocked" in _txt
    ck(_derived, "_mkt_blocked 가 mkt_gate 결과에서 파생됨 (상수 무력화 방지)")
    ck("load_market_gate_users()" in _wa_src, "메인 흐름에서 대상 사용자 조회")
    ck("rc.market_gate_status(" in _wa_src, "메인 흐름에서 SSOT 게이트 판정 호출")
    # 호출부가 **실제 변수**를 넘기는지. mkt_gate=None 으로 바꿔치기해도
    #   단순 문자열 검사는 통과한다 — 인자 값이 Name 노드인지까지 확인한다.
    _passed = False
    for _n in ast.walk(_tree):
        if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name)
                and _n.func.id == "eval_watchlist_eod"):
            _kw = {k.arg: k.value for k in _n.keywords}
            _g, _u = _kw.get("mkt_gate"), _kw.get("mkt_users")
            if (isinstance(_g, ast.Name) and isinstance(_u, ast.Name)
                    and _g.id != "None" and _u.id != "None"):
                _passed = True
    ck(_passed, "eval 호출이 게이트 변수를 실제로 전달 (None 하드코딩 아님)")
    # 2pm 장중 경로에는 적용하지 않는다(결정 3-A)
    _iv = _fns.get("eval_watchlist_intraday")
    if _iv:
        _iv_src = ast.get_source_segment(_wa_src, _iv) or ""
        ck("mkt_gate" not in _iv_src and "_mblk" not in _iv_src,
           "2pm 장중 경로에는 게이트 미적용 (결정 3-A)")
except Exception as e:  # noqa: BLE001
    ck(False, f"구조 검증 실패: {type(e).__name__}: {e}")

print()
print("=" * 68)
print("7-b) 5pm 관찰 배너 (Gate_Market=N 이어도 기록이 남는가)")
print("=" * 68)
# 게이트를 켤지 판단하려면 '켜져 있었다면 막혔을 날'의 기록이 필요하다.
#   판정이 토글에 묶여 있으면(_m_users 가 비면 판정 자체를 안 하면) 관찰이 불가능하다.
try:
    _wa_tree = ast.parse(_wa_src)
    _fns2 = {n.name: n for n in _wa_tree.body if isinstance(n, ast.FunctionDef)}
    ck("build_market_gate_banner" in _fns2, "build_market_gate_banner 정의됨")

    # 판정이 토글과 무관하게 항상 수행되는지 (구버전: `if _m_users else None`)
    _mg = next((n for n in ast.walk(_wa_tree)
                if isinstance(n, ast.Assign) and n.targets
                and getattr(n.targets[0], "id", "") == "_m_gate"), None)
    ck(_mg is not None, "_m_gate 대입 존재")
    if _mg is not None:
        ck(not isinstance(_mg.value, ast.IfExp),
           "_m_gate 판정이 토글에 조건부가 아님 (관찰 모드에서도 기록됨)")

    # EOD 발송에 배너가 실제로 전달되는지
    _wired2 = False
    for _n in ast.walk(_wa_tree):
        if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name)
                and _n.func.id == "dispatch_radar_emails"):
            for _kw in _n.keywords:
                if _kw.arg == "banner_html":
                    _t = ast.get_source_segment(_wa_src, _kw.value) or ""
                    if "build_market_gate_banner" in _t:
                        _wired2 = True
    ck(_wired2, "EOD 발송에 관찰 배너가 전달됨")

    # 배너 동작 — 실제 실행
    _bn = [_fns2["build_market_gate_banner"]]
    _bn += [n for n in _wa_tree.body if isinstance(n, ast.Assign)
            and getattr(n.targets[0], "id", "") == "MARKET_GATE_MAIL_TAG"]
    _bns = {"rc": rc}
    exec(compile(ast.Module(body=_bn, type_ignores=[]), "wa", "exec"), _bns)  # noqa: S102
    _build = _bns["build_market_gate_banner"]
    _TAG = _bns["MARKET_GATE_MAIL_TAG"]

    ck(_build({"blocked": False, "count": 1.0, "threshold": 2}, set()) == "",
       "경고 임계 미만 → 배너 없음 (매일 뜨는 잡음 방지)")
    ck(_build(None, set()) == "", "게이트 None → 배너 없음")
    # 빈 태그면 `"" in text` 가 항상 참이라 검사가 통과해버린다 — 태그 자체를 먼저 본다.
    ck(isinstance(_TAG, str) and len(_TAG.strip()) >= 4,
       f"검색 태그가 실질적인 문자열 ({_TAG!r})")
    _obs = _build({"blocked": True, "count": 3.0, "threshold": 2}, set())
    ck(bool(_TAG.strip()) and _TAG in _obs,
       f"관찰 모드 배너에 검색 태그('{_TAG}') 포함")
    ck("않았을" in _obs, "관찰 모드는 반사실 문구 (실제 동결 아님)")
    _live = _build({"blocked": True, "count": 3.0, "threshold": 2}, {"yab"})
    ck(bool(_TAG.strip()) and _TAG in _live and "동결됐습니다" in _live,
       "적용 모드는 실제 동결 문구")
    ck(_obs != _live, "관찰/적용 문구가 구분됨")
except Exception as e:  # noqa: BLE001
    ck(False, f"배너 검증 실패: {type(e).__name__}: {e}")

print()
print("=" * 68)
print("8) app.py ↔ 공용 모듈 심볼 정합성 (전수)")
print("=" * 68)
# v2(2026-08-13): 버전 문자열 일치 검사를 폐기하고 **실제 심볼 존재**를 본다.
#   구 방식은 6개 모듈이 같은 SSOT_VERSION 을 갖도록 요구해 무관한 파일까지
#   배포하게 만들었고, 하나만 빠뜨려도 앱이 멈췄다(오경보 반복).
#
#   여기서는 app.py 가 `rc.foo` / `uc.BAR` 식으로 참조하는 **모든** 속성을 AST 로
#   뽑아, 대상 모듈의 최상위 정의에 실제로 있는지 대조한다. app.py 런타임 검사는
#   속도 때문에 핵심 심볼만 보므로, 전수 확인은 배포 **전인** 여기서 한다.
import ast


def _toplevel_names(path):
    """import 없이 모듈 최상위 정의 이름 집합 (외부 의존성 불필요)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            for tg in n.targets:
                if isinstance(tg, ast.Name):
                    out.add(tg.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add(a.asname or a.name.split(".")[0])
    return out


try:
    _app_src = open(_find("app.py"), encoding="utf-8").read()
    _app_tree = ast.parse(_app_src)

    _alias = {}
    for _n in ast.walk(_app_tree):
        if isinstance(_n, ast.Import):
            for _a in _n.names:
                _alias[_a.asname or _a.name] = _a.name
    _TARGETS = {"scanner_core", "narrative_core", "users_core", "fmp_extras",
                "portfolio_core", "watchlist_metrics_core", "regime_core",
                "earnings_core"}   # 2026-08-13: 실적 레이더 Tier 2(Source 열) 추가
    _al = {k: v for k, v in _alias.items() if v in _TARGETS}

    _used = {}
    for _n in ast.walk(_app_tree):
        if (isinstance(_n, ast.Attribute) and isinstance(_n.value, ast.Name)
                and _n.value.id in _al):
            _used.setdefault(_al[_n.value.id], set()).add(_n.attr)

    ck(bool(_used), "app.py 의 공용 모듈 참조를 추출함")
    for _mod in sorted(_used):
        _syms = _used[_mod]
        try:
            _have = _toplevel_names(_find(f"{_mod}.py"))
        except FileNotFoundError:
            print(f"  ⏭️  {_mod}.py 미포함 — 배포 시 함께 확인할 것")
            continue
        _miss = sorted(s for s in _syms if s not in _have)
        ck(not _miss,
           f"{_mod}: app.py 가 쓰는 {len(_syms)}개 심볼 모두 존재"
           + (f" — 없음 {_miss}" if _miss else ""))

    # 런타임 매니페스트(_SSOT_NEEDS)도 실제 모듈과 맞는지 확인
    _mani = next((n for n in ast.walk(_app_tree)
                  if isinstance(n, ast.Assign)
                  and getattr(n.targets[0], "id", "") == "_SSOT_NEEDS"), None)
    ck(_mani is not None, "app.py 에 _SSOT_NEEDS 매니페스트 존재")
    if _mani is not None:
        _bad = 0
        for _elt in _mani.value.elts:
            _name = _elt.elts[0].value
            _syms = [x.value for x in _elt.elts[2].elts]
            # 오타난 모듈명을 조용히 건너뛰면 검사가 통째로 무력화된다.
            # 매니페스트에 적힌 모듈은 반드시 실재해야 한다.
            try:
                _have = _toplevel_names(_find(f"{_name}.py"))
            except FileNotFoundError:
                ck(False, f"매니페스트의 '{_name}' 파일이 없음 — 모듈명 오타 의심")
                _bad += 1
                continue
            _bad += sum(1 for s in _syms if s not in _have)
        ck(_bad == 0, f"_SSOT_NEEDS 매니페스트가 실제 모듈과 일치 (불일치 {_bad}건)")
        # 매니페스트가 비어 있으면 검사 자체가 무력화된다
        ck(len(_mani.value.elts) >= 5,
           f"매니페스트에 모듈 {len(_mani.value.elts)}개 등록 (5개 이상)")
        _names = {e.elts[0].value for e in _mani.value.elts}
        _unknown = sorted(_names - _TARGETS)
        ck(not _unknown, f"매니페스트 모듈명이 모두 알려진 공용 모듈"
                         + (f" — 미상 {_unknown}" if _unknown else ""))

    # 구 방식 잔재가 남아 있으면 오경보가 다시 시작된다
    ck("_SSOT_REQUIRED" not in _app_src,
       "구 버전일치 검사(_SSOT_REQUIRED) 제거됨")
except Exception as e:  # noqa: BLE001
    ck(False, f"심볼 정합성 검사 실패: {type(e).__name__}: {e}")

print()
print("=" * 68)
if FAIL:
    print(f"❌ 실패 {len(FAIL)}건")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("✅ 전체 통과")
