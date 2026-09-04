#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_fmp_ssot.py — 저장소 전역 FMP SSOT·계약 가드 (2026-08-26).

왜 이 파일이 있나
─────────────────
2026-08-26 에 같은 결함이 두 번 다른 모습으로 터졌다.

  (1) `run_signal_backtest.py` 가 `fmp_http` 를 우회하고 원시 requests.get 으로
      474종목을 발사 → 분당 한도(300)를 넘긴 174종목이 429 → 빈 DataFrame 으로
      **무성 탈락** → 백테스트는 정상 완료되고 시트에 오염 행이 쌓였다.

  (2) 그걸 고치면서 `_fmp_price_history` 의 반환을 `(df, kind)` 로 바꿨는데,
      그 함수를 빌려 쓰는 `diag_fmp_depth.py`·`diag_trade_history.py` 의
      호출부를 안 고쳤다. 전자는 `len(tuple) == 2` 때문에 **예외 없이 전 종목이
      "2봉"으로 집계**됐다.

(1) 은 `fmp_extras.py` 70행 주석에 이미 한 번 기록돼 있던 사고다. 그때
"같은 패턴을 전 저장소에서 grep 한다"고 적었지만, grep 을 사람이 하기로 했고
하지 않았다. (2) 도 마찬가지로 사람 기억에 맡겼다가 놓쳤다.

**그래서 grep 을 도구로 옮긴다.** 이 파일이 그 도구다.

검사 구조
─────────
  A 저장소 전역 (대상 무관 · 영구)
    A1 원시 FMP 호출   requests.get 으로 FMP 를 직접 때리는 지점 (기준선 대비)
    A2 튜플 계약       튜플 반환 함수를 단일 이름으로 받는 크로스모듈 호출부
    A3 submit 언팩     ex.submit(mod.튜플함수, …) 후 .result() 를 언팩하는가

  B 위성 백테스트 게이트 (diag_satellite_backtest.py 락스텝 짝)
    B1 스로틀 · B2 분류 · B3 게이트 순서 · B4 지문 · B5 복제 대조 · B6 뮤테이션

A1 의 기준선(_RAW_GET_BASELINE)에 대하여
────────────────────────────────────────
저장소에는 이미 80여 곳의 원시 FMP 호출이 있다. 전부 하드 실패로 잡으면
이 스위트는 첫날부터 빨간불이고, 빨간불인 스위트는 아무도 안 본다.
그래서 **래칫**으로 만든다 — 기준선보다 늘면 실패, 줄면 통과하되 경고.

⚠️ 기준선은 '괜찮다'는 뜻이 아니라 '알고 있고 아직 안 고쳤다'는 뜻이다.
   각 항목에 왜 미뤘는지를 적어둔다.

안전성
──────
· 네트워크 접근 없음 · 시트 접근 없음 · FMP 호출 없음 (전부 스텁/정적 분석)
· 부작용 없다. 몇 번을 돌려도 같은 결과.

실행:  python3 automation/diag_fmp_ssot.py
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 모듈이 환경변수를 읽기 **전에** 비워둔다 — 실행 환경에 값이 남아 있으면
# 기본값 검사가 오염된다.
os.environ.pop("MIN_FETCH_RATE", None)

_fails, _passes, _notes = [], 0, []


def check(label, got, want):
    global _passes
    ok = (got == want)
    if ok:
        _passes += 1
        print("  ✅ " + label)
    else:
        _fails.append(label + "  (got=" + repr(got) + " want=" + repr(want) + ")")
        print("  ❌ " + label + "  got=" + repr(got) + " want=" + repr(want))
    return ok


# ══════════════════════════════════════════════════════════════════════════
# 저장소 수집 — 루트 + automation/ 양쪽을 본다 (배포 레이아웃이 갈라져 있다)
# ══════════════════════════════════════════════════════════════════════════
def repo_files() -> dict:
    """{모듈명: 절대경로}. 같은 이름이 양쪽에 있으면 sys.path 와 같게 루트 우선."""
    # ⚠️ 순서가 sys.path 와 같아야 한다. sys.path 는 위에서 [_ROOT, _HERE, …] 로
    #    끝나므로 import 는 _ROOT 를 먼저 집는다. 여기서 automation/ 을 우선하면
    #    **스캔한 파일과 import 한 모듈이 서로 다른 사본**이 되어, 정적 검사는
    #    통과하는데 실제로는 옛 코드가 도는 상황이 만들어진다.
    out = {}
    for d in (_HERE, os.path.join(_ROOT, "automation"), _ROOT):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".py") and not f.startswith("."):
                out[f[:-3]] = os.path.join(d, f)
    return out


FILES = repo_files()
SRCS = {}
TREES = {}
for _m, _p in FILES.items():
    try:
        _s = open(_p, encoding="utf-8").read()
        SRCS[_m] = _s
        TREES[_m] = ast.parse(_s)
    except (SyntaxError, UnicodeDecodeError, OSError):
        continue


def _parents(tree):
    """자식 → 부모 맵. ast 에는 부모 포인터가 없어 직접 만든다."""
    pm = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            pm[c] = n
    return pm


def _unparse(n) -> str:
    try:
        return ast.unparse(n)
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════
# A1 — 원시 FMP 호출
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ 값은 '허용치'가 아니라 '현재 남아 있는 부채'다. 줄이면 이 숫자를 낮춘다.
_RAW_GET_BASELINE = {
    # 대화형 경로 + @st.cache_data. 자동화와 위험도가 다르고 68곳을 한 번에
    # 손대면 회귀 위험이 크다 → 별도 검토 사안(인수인계 §6-B5).
    # 2026-08-27: 58 → 63. 코드가 늘어난 게 아니라 **탐지기가 눈을 떴다**
    # (`_fmp_url_names` 가 변수에 담긴 URL 까지 추적). 실제 부채는 처음부터 63 이었다.
    # 2026-08-28: 63 → 62. _fmp_price_history_robust 를 fh.fmp_get_ex 로 위임했다.
    # 2026-09-04: 62 → 50. **매크로 지표 블록 12곳**을 `_fmp_macro_get` 으로
    #   전환했다(treasury-rates · ^VIX · WTI · unemploymentRate · CPI · UUP×2 ·
    #   federalFundsRate×2 · economic-indicators · market-risk-premium ·
    #   economic-calendar). 함수당 정확히 1곳뿐인 것만 골라 호출 간 상호작용을
    #   피했다 — compute_daily_risk_gauge(6곳) 같은 다중 호출은 남겼다.
    #   ⚠️ 전환 이유는 스타일이 아니다. run_drg_predict 는 8-28 에 같은 매크로
    #   지표들을 이미 전환했고 사유가 이랬다: "429 를 조용히 삼키면 '조회 실패'
    #   한 줄만 남고 DRG 프롬프트에서 신호가 통째로 빠진다." **app.py 는 그
    #   지표를 화면에 그리는 반쪽인데 여태 생 호출이었다.** SSOT 방향이
    #   app.py → automation 인데 automation 만 고쳐져 있던 자리다.
    #   FRED 폴백이 없는 함수(_fetch_cpi_series 등)는 429 가 곧 N/A 였다.
    #   ⚠️ 재시도 예산은 `_FMP_MACRO_RETRIES = 1` 한 곳에만 있다. 대화형 경로라
    #   fmp_http 기본값 3회를 쓰면 화면이 멈춘다 — 호출부 12곳에 복제 금지.
    #   나머지 50곳은 @st.cache_data 대화형 경로다 — 한 번에 손대면 회귀 위험이
    #   크므로 계속 래칫으로 조인다(인수인계 §6-B5).
    "app.py": 50,
    # run_drg_predict.py 는 2026-08-28 에 11곳 전부 fmp_http 로 전환됐다(11 → 0).
    #   requests 임포트 자체를 지웠다 — 되살리려면 임포트를 다시 추가해야 한다.
    #   기준선에서 제거 = 이제 한 곳이라도 생기면 '신규 우회'로 실패한다.
    #   전환 이유는 스타일이 아니다: 8AM/9AM 예측이 429 를 조용히 삼키면
    #   "- 신용 스프레드: 조회 실패" 같은 한 줄만 남고 **DRG 프롬프트에서 신호가
    #   통째로 빠진다.** 프롬프트가 짧아진 것을 사람이 알아채기 어렵다.
    # run_watchlist_alerts.py 는 2026-08-26 에 4곳 전부 fmp_http 로 전환됐다.
    # requests 임포트 자체를 지워 되살리려면 임포트를 다시 추가해야 한다.
    # 기준선에서 제거 = 이제 한 곳이라도 생기면 '신규 우회'로 실패한다.
    # narrative_core.py 는 2026-09-04 에 2곳 전부 fmp_http 로 전환됐다(2 → 0).
    #   기준선에서 제거 = 이제 한 곳이라도 생기면 '신규 우회'로 실패한다.
    #   ⚠️ 다른 전환들과 달리 **`import requests` 를 지우지 못했다** — 89행대의
    #      _rss_news_fallback 이 RSS 피드를 가져오는 데 실제로 쓴다. 그래서
    #      임포트 삭제로 재발을 막는 수법을 여기서는 쓸 수 없다.
    #      대신 `_FMP_BASE` 상수를 삭제했다: 원시 호출을 되살리려면 상수를
    #      다시 만들거나 URL 을 하드코딩해야 하고 둘 다 이 탐지기가 잡는다.
    #      **이 항목이 기준선에서 빠진 것이 유일한 방어선이다.**
    #   전환 이유는 스타일이 아니다: _fmp_validate_symbols_ex 는 429 를 받으면
    #      '보류'로 처리하는데 보류는 곧 '유지'다 → **환각 티커가 그대로 출력에
    #      남는다.** 이 함수가 존재하는 목적 자체가 레이트리밋 한 번에 무력화됐다.
    # run_narrative.py 는 2026-08-28 에 2곳 전부 fmp_http 로 전환됐다(2 → 0).
    #   requests 임포트 자체를 지웠다 — 되살리려면 임포트를 다시 추가해야 한다.
    #   기준선에서 제거 = 이제 한 곳이라도 생기면 '신규 우회'로 실패한다.
    #   전환 이유는 스타일이 아니다: _fmp_close_series 가 429 를 조용히 삼키면
    #   빈 Series 가 돌아가고 호출부는 그걸 '데이터 부족'으로 읽어 종목을
    #   continue 로 건너뛴다. **Emerging 검증 결과에서 종목이 통째로 사라지는데
    #   로그에는 아무것도 안 남는다.**
    # run_drg_verify.py 도 같은 날 1곳 전환됐다(1 → 0). 이쪽은 더 나빴다 —
    #   429 면 fmp_r.json() 이 예외로 튀어 verify_prediction 이 빈 결과를
    #   돌려주고, 그날 DRG 검증이 통째로 사라진다.
    # run_earnings_watch.py 는 2026-08-28 에 1곳 전환됐다(1 → 0). requests 임포트
    #   자체를 지웠다 — 되살리려면 임포트를 다시 추가해야 한다.
    #   기준선에서 제거 = 이제 한 곳이라도 생기면 '신규 우회'로 실패한다.
    #   전환 이유는 스타일이 아니다: fmp_price_history 가 429 를 조용히 삼키면
    #   빈 DataFrame 이 돌아가고, 그러면 실적 갭 표본이 줄어든 것이 "종목 이력이
    #   짧다"와 **구분되지 않는다.** 예상 변동폭 confidence 가 조용히 강등된다.
    # industry_core.py 는 2026-09-04 에 1곳 전환됐다(1 → 0). `_BASE` 상수와
    #   `import requests` 를 **둘 다** 지웠다 — fetch_history 가 쓰던
    #   requests.utils.quote 도 urllib.parse.quote 로 바꿨다. 되살리려면
    #   임포트를 다시 추가하거나 URL 을 하드코딩해야 하고 둘 다 이 탐지기가 잡는다.
    #   기준선에서 제거 = 이제 한 곳이라도 생기면 '신규 우회'로 실패한다.
    #   ⚠️ 전환 이유는 스타일이 아니다. 이건 **부하 결함**이었다:
    #   refresh_industry_perf --backfill 이 `for nm in names` 로 업종 수(159)
    #   만큼 **슬립 없이** _get 을 부르는데 그 전부가 레이트리미터 밖이었다.
    #   그리고 429 가 뜨면 즉시 "HTTP 429" 를 돌려줘 그 업종이 empty 로 빠진다
    #   — **Industry_Perf 시트의 열 하나가 통째로 유실되는데 로그엔 경고 한 줄
    #   뿐이다.** 와이드 포맷이라 열 유실은 그 업종의 전 기간 이력 손실이다.
    "diag_earnings_preview_backtest.py": 1,
    "diag_industry_mapping.py": 1,
    # ── 2026-08-27 신규 노출 (강화된 탐지기가 처음 본 것들 · 코드 변경 아님) ──
    # 전부 `url = FMP_BASE + … + "apikey=" + KEY` → `requests.get(url)` 모양이라
    # 이전 탐지기가 통째로 놓쳤다. 이제 보이므로 기준선에 명시해 부채로 남긴다.
    #
    # calendar_core.py 는 2026-09-04 에 1곳 전환됐다(1 → 0). 위 주석이
    #   "우선 정리 대상"이라고 적어둔 그 항목이다. URL 하드코딩과
    #   `import requests` 를 둘 다 지웠다 — 되살리려면 임포트를 다시 추가하거나
    #   URL 을 하드코딩해야 하고 둘 다 이 탐지기가 잡는다.
    #   기준선에서 제거 = 이제 한 곳이라도 생기면 '신규 우회'로 실패한다.
    #   ⚠️ 부하는 주 1회 1콜이라 위험이 없었다. 그런데도 고친 이유는 이 파일이
    #   **코어 모듈**이기 때문이다 — 예외를 허용하는 SSOT 는 지킬 수 없는
    #   SSOT 다. 실패 시 조용히 [] 를 돌려주는 계약은 그대로 유지했다.
    #   ⚠️ import 는 **함수 안**에 있다. calendar_core 는 프로젝트 모듈을 모듈
    #   레벨에서 하나도 import 하지 않는다 — app.py 의 시장 상태 헤더가 매
    #   rerun 마다 이 모듈을 타는 핫 패스라 임포트 비용이 0 이어야 한다.
    #   모듈 상단으로 올리면 그 제약이 깨진다.
    "diag_fmp_endpoints.py": 1,
    "diag_fmp_newcaps.py": 1,
    "diag_industry_momentum.py": 1,
    "diag_nodata_cause.py": 1,
    # ── 2026-09-03 신규 등재 ──
    # ⚠️ 이 파일은 **의도적으로** 원시 requests 를 쓴다. 63행이 사유를 밝힌다:
    #    "프로젝트 모듈 import 없음 (requests 만 사용) → 사본 신선도와 무관".
    #    fmp_extras/fmp_http 를 임포트하는 순간 그 독립성이 깨진다 — 사본이
    #    낡았을 때 API 사실을 확인할 수단이 없어진다(9-02 marketCap 프로브가
    #    정확히 그 상황에서 쓰였다). 그래서 SSOT 전환 대상이 아니라 **예외**다.
    #    위 diag_* 여섯 항목과 같은 성격이다.
    # ⚠️ 등재를 빠뜨린 대가가 컸다. A1 이 빨간불이면 워크플로가 exit 1 로
    #    끊겨 **뒤 스텝이 통째로 스킵된다** — diag_hist_window_consumers.yml
    #    에서 check_freshness.py(락스텝 업로드 누락 탐지)와 check_py311.py 가
    #    한 번도 돌지 못했다(2026-09-03 실측). 부채 한 건이 관문 둘을 실명시켰다.
    "diag_aum_field.py": 1,
    # diag_sell_verdict.py 는 2026-08-27 에 fmp_http 로 전환됐다(1 → 0).
    # 기준선에서 제거 = 이제 한 곳이라도 생기면 '신규 우회'로 실패한다.
    # 전환 이유는 스타일이 아니었다: 이 진단은 6워커 병렬로 유니버스를 훑는데
    # 429 를 조용히 삼키면 '봉 부족'과 구분되지 않아 [0] 블록의 원인 규명 자체가
    # 오염된다.
}

# fmp_http 자체는 SSOT 구현체이므로 원시 호출이 있어야 정상이다.
_RAW_GET_EXEMPT = {"fmp_http"}


def _fmp_url_names(tree) -> set:
    """모듈 안에서 FMP 주소가 바인딩된 이름들 (_FMP_BASE · url · 그 파생).

    ⚠️ 2026-08-27: 이전 구현은 **문자열 상수 직접 대입만** 봤다. 그래서

        url = f"{_FMP_BASE}/…?apikey={KEY}"
        requests.get(url, timeout=…)

    이 패턴을 통째로 놓쳤다 — 호출식의 인자가 `url` 뿐이라 그 안에
    "financialmodelingprep" 도 "apikey" 도 `_FMP_BASE` 도 없기 때문이다.
    뮤테이션으로 실증했다(M9). 저장소에 이 모양이 10곳 있었고 그중 하나는
    **코어 모듈**(`calendar_core`)이었다.

    그래서 **고정점까지 전파**한다: 값의 소스에 FMP 표식이나 이미 알려진
    FMP 이름이 등장하면 그 대입 대상도 FMP 이름이다. 증강 대입(`url += …`)도
    같이 본다 — 조각을 나눠 붙이면 표식이 첫 줄에만 있다.
    """
    out = set()
    for _ in range(6):          # 고정점. 실측 2회면 수렴하나 여유를 둔다.
        before = len(out)
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                targets, val = n.targets, n.value
            elif isinstance(n, ast.AugAssign):
                targets, val = [n.target], n.value
            else:
                continue
            src = _unparse(val)
            if not src:
                continue
            hit = ("financialmodelingprep" in src or "apikey" in src
                   or any(nm in src for nm in out))
            if not hit:
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        if len(out) == before:
            break
    return out


def _requests_names(tree):
    """(requests 모듈을 가리키는 이름들, requests.get 자체를 가리키는 이름들).

    ⚠️ 2026-08-28: 이전 구현은 `c.func.value.id == "requests"` 하드코딩이었다.
       즉 **별칭 한 줄이면 래칫이 통째로 우회된다**:

           import requests as _rq
           _rq.get(url, timeout=8)          # ← A1 이 못 봤다

       변이 P10 으로 실증했다. 기준선에서 파일을 제거하며 "이제 한 곳이라도
       생기면 실패한다"고 적어 온 문장이, 별칭 앞에서는 사실이 아니었다.
       `from requests import get` 과 `_g = requests.get` 도 같은 구멍이다.
    """
    mods, gets = {"requests"}, set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] == "requests":
                    mods.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "requests":
            for a in n.names:
                (gets if a.name == "get" else mods).add(a.asname or a.name)
        elif (isinstance(n, ast.Assign) and len(n.targets) == 1
              and isinstance(n.targets[0], ast.Name)):
            v, t = n.value, n.targets[0].id
            if (isinstance(v, ast.Attribute) and v.attr == "get"
                    and isinstance(v.value, ast.Name) and v.value.id in mods):
                gets.add(t)
            elif isinstance(v, ast.Name) and v.id in mods:
                mods.add(t)
    return mods, gets


def raw_fmp_gets(mod: str) -> list:
    """[(lineno, 호출식 요약)] — requests.get 으로 FMP 를 직접 때리는 지점."""
    tree = TREES.get(mod)
    if tree is None or mod in _RAW_GET_EXEMPT:
        return []
    names = _fmp_url_names(tree)
    rq_mods, rq_gets = _requests_names(tree)
    hits = []
    for c in ast.walk(tree):
        _is_get = (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                   and c.func.attr == "get" and isinstance(c.func.value, ast.Name)
                   and c.func.value.id in rq_mods)
        _is_bare = (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id in rq_gets)
        if not (_is_get or _is_bare):
            continue
        arg = _unparse(c.args[0]) if c.args else ""
        # ⚠️ 이름 매칭은 **AST 이름 노드**로 한다. 부분 문자열로 하면
        #    `url` 이라는 이름이 `cfg['url']`(RSS 피드 주소)에 걸리고,
        #    `a` 같은 한 글자 이름은 거의 모든 인자에 걸린다. 실제로 그렇게
        #    narrative_core 가 2 → 3 으로 부풀었다.
        argn = {x.id for x in ast.walk(c.args[0])
                if isinstance(x, ast.Name)} if c.args else set()
        if ("financialmodelingprep" in arg or "apikey" in arg
                or (argn & names)):
            hits.append((c.lineno, arg[:60]))
    return hits


# ══════════════════════════════════════════════════════════════════════════
# A2/A3 — 튜플 반환 계약
# ══════════════════════════════════════════════════════════════════════════
def tuple_returning(tree) -> dict:
    """{함수명: {가능한 튜플 길이}} — return 문이 튜플인 최상위/중첩 함수."""
    out = {}
    for n in ast.walk(tree):
        if not isinstance(n, ast.FunctionDef):
            continue
        ar = set()
        for r in ast.walk(n):
            if isinstance(r, ast.Return) and isinstance(r.value, ast.Tuple):
                if len(r.value.elts) >= 2:
                    ar.add(len(r.value.elts))
        if ar:
            out[n.name] = ar
    return out


REG = {m: tuple_returning(t) for m, t in TREES.items()}


def import_aliases(tree) -> dict:
    """{별칭: 저장소모듈명} — 저장소 안의 모듈만."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in TREES:
                    out[a.asname or a.name] = a.name
    return out


# 튜플에 실제로 있는 속성 — 이건 접근해도 정상이다.
_TUPLE_ATTRS = {"count", "index"}


def _enclosing_fn(node, pm):
    cur = pm.get(node)
    while cur is not None:
        if isinstance(cur, ast.FunctionDef):
            return cur
        cur = pm.get(cur)
    return None


def contract_violations(mod: str) -> list:
    """[(lineno, 설명)] — 튜플 반환 함수를 단일 값으로 소비하는 지점."""
    tree = TREES.get(mod)
    if tree is None:
        return []
    pm = _parents(tree)
    alias = import_aliases(tree)
    bad = []

    def _arity(al, fn):
        tgt = alias.get(al)
        if not tgt:
            return None
        return REG.get(tgt, {}).get(fn)

    for c in ast.walk(tree):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
            continue
        ar = _arity(f.value.id, f.attr)
        if not ar:
            continue
        ref = f"{f.value.id}.{f.attr}()"
        p = pm.get(c)
        # (1) X = mod.f(...)
        #   단일 이름으로 받는 것 자체는 죄가 아니다 — 튜플을 통째로 다음 함수에
        #   넘기는 정당한 패턴이 있다(run_watchlist_alerts 의 `_posv` 가 그렇다).
        #   진짜 결함은 그렇게 받아놓고 **첫 원소인 척 쓰는 것**이다.
        #   그래서 이후 `X.<속성>` 접근이 있을 때만 잡는다.
        if isinstance(p, ast.Assign):
            if len(p.targets) == 1 and isinstance(p.targets[0], ast.Name):
                nm = p.targets[0].id
                scope = _enclosing_fn(c, pm) or tree
                hit = None
                for u in ast.walk(scope):
                    if (isinstance(u, ast.Attribute) and isinstance(u.value, ast.Name)
                            and u.value.id == nm and u.attr not in _TUPLE_ATTRS):
                        hit = u.attr
                        break
                if hit:
                    bad.append((c.lineno, f"{ref} 를 단일 이름 '{nm}' 로 받고 "
                                          f"'{nm}.{hit}' 로 쓴다 — 튜플에는 없다"))
            continue
        # (2) mod.f(...).attr — 튜플에 곧바로 속성 접근
        if isinstance(p, ast.Attribute) and p.attr not in _TUPLE_ATTRS:
            bad.append((c.lineno, f"{ref} 반환값에 '.{p.attr}' 접근 "
                                  f"— 튜플에는 없다"))
            continue
        # ⚠️ len(mod.f(...)) 은 검사하지 않는다. 스위트가 반환 길이를 일부러
        #    확인하는 정당한 용법이 있고(diag_universe_funnel B5), 결함 쪽은
        #    아래 (3) submit 규칙이 이미 덮는다. 구분할 방법이 없으면 잡지 않는다
        #    — 오탐이 나오는 가드는 곧 무시당한다.

    # (3) ex.submit(mod.f, ...) — 결과는 .result() 로 온다. 같은 함수 안에
    #     튜플 언팩이 있는지 본다. 없으면 (1)~(3) 과 같은 사고가 지연 발생한다.
    for c in ast.walk(tree):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "submit" and c.args):
            continue
        a0 = c.args[0]
        if not (isinstance(a0, ast.Attribute) and isinstance(a0.value, ast.Name)):
            continue
        tgt = alias.get(a0.value.id)
        if not tgt or a0.attr not in REG.get(tgt, {}):
            continue
        fn = _enclosing_fn(c, pm)
        ok = False
        for x in ast.walk(fn if fn is not None else tree):
            if (isinstance(x, ast.Assign) and isinstance(x.value, ast.Call)
                    and isinstance(x.value.func, ast.Attribute)
                    and x.value.func.attr == "result"
                    and len(x.targets) == 1
                    and isinstance(x.targets[0], ast.Tuple)):
                ok = True
        if not ok:
            bad.append((c.lineno, f"submit({a0.value.id}.{a0.attr}) 인데 "
                                  f"이 함수에 '.result()' 튜플 언팩이 없다"))
    return sorted(set(bad))


# ══════════════════════════════════════════════════════════════════════════
print("=" * 76)
print("A) 저장소 전역 — 원시 FMP 호출 래칫")
print("=" * 76)

_raw_now = {}
for m in sorted(TREES):
    h = raw_fmp_gets(m)
    if h:
        _raw_now[m + ".py"] = len(h)

_new, _shrunk = [], []
for fn, n in sorted(_raw_now.items()):
    base = _RAW_GET_BASELINE.get(fn)
    if base is None:
        _new.append(f"{fn}: {n}곳 — 기준선에 없는 **신규 우회**")
    elif n > base:
        _new.append(f"{fn}: {n}곳 (기준선 {base}) — 늘었다")
    elif n < base:
        _shrunk.append(f"{fn}: {n}곳 (기준선 {base}) — 줄었다, 기준선을 낮출 것")

for fn in sorted(_RAW_GET_BASELINE):
    if fn not in _raw_now:
        _shrunk.append(f"{fn}: 0곳 (기준선 {_RAW_GET_BASELINE[fn]}) — "
                       f"전부 정리됨, 기준선에서 제거할 것")

check("A1  기준선을 넘는 원시 FMP 호출이 없다", _new, [])
if _new:
    for x in _new:
        print("        · " + x)
print(f"      (현재 부채 총 {sum(_raw_now.values())}곳 / "
      f"{len(_raw_now)}개 파일 — 0 이 목표)")
if _shrunk:
    # 실패로 만들지 않는다. 고쳤더니 스위트가 빨개지면 아무도 안 고친다.
    print("  ⚠️  기준선 갱신 필요:")
    for x in _shrunk:
        print("        · " + x)
        _notes.append(x)

print()
print("=" * 76)
print("A) 저장소 전역 — 튜플 반환 계약 (허용목록 없음 · 0 이어야 한다)")
print("=" * 76)

_viol = []
for m in sorted(TREES):
    for ln, why in contract_violations(m):
        _viol.append(f"{m}.py:{ln}  {why}")

check("A2  튜플 반환 함수를 단일 값으로 받는 곳이 없다", _viol, [])
for x in _viol:
    print("        · " + x)

_n_tuple_fns = sum(len(v) for v in REG.values())
print(f"      (튜플 반환 함수 {_n_tuple_fns}개를 추적 중)")


# ══════════════════════════════════════════════════════════════════════════
# A4 — 필수 키워드 인자 계약 (2026-08-27 신설)
# ══════════════════════════════════════════════════════════════════════════
# 왜 A2 로 안 되나
# ────────────────
# A2 는 '반환 모양'이 바뀐 계약을 본다. A4 는 그 반대 방향 — **인자를 빼먹어도
# 예외가 안 나고 조용히 다른 답이 나오는** 계약이다. 이쪽이 더 조용하다.
#
#   rc.integrated_sell_verdict(..., gap_ma200_pct=gap)   ← 이격 비례 2.0~4.0
#   rc.integrated_sell_verdict(...)                      ← 일괄 4.0 (폴백)
#
# 둘 다 정상 실행되고 둘 다 라벨을 반환한다. 다른 것은 **점수**뿐이고, 4.0 은
# 🔴 청산 문턱이라 판정이 갈린다. 타입 오류도, 예외도, 로그도 없다.
#
# 2026-08-27 에 실제로 터졌다: run_signal_backtest._pos_label_at 이 이 인자를
# 빼고 있었고, 그 함수의 독스트링에는 "(SSOT 그대로)" 라고 **적혀만** 있었다.
# 주석은 계약을 강제하지 않는다. 그래서 강제를 도구로 옮긴다.
#
# 래칫이 아니라 하드 실패인 이유: 위반이 1곳뿐이었고 그 1곳을 같은 커밋에서
# 고쳤다. 기준선 0 에서 출발할 수 있으면 래칫을 둘 이유가 없다.
_A4_CONTRACTS = [
    # (소유 모듈, 함수명, 필수 키워드, 빼먹었을 때 무슨 일이 나는가)
    ("regime_core", "integrated_sell_verdict", "gap_ma200_pct",
     "200일선 이탈 점수가 이격 비례(2.0~4.0)가 아닌 일괄 4.0 폴백이 된다"),
]


def _def_node(tree, func_name):
    """모듈 최상위 FunctionDef 를 찾는다. 없으면 None."""
    if tree is None:
        return None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == func_name:
            return n
    return None


def declares_kwarg(tree, func_name, kwarg) -> bool:
    """정의부가 그 키워드를 **실제로** 받는가 (명시 핀).

    ⚠️ 이 검사가 없으면 SSOT 쪽에서 인자 이름만 바꿔도 A4 는 초록불이다 —
       호출부는 옛 이름을 그대로 넘기고 있고, 탐지기도 옛 이름을 찾으니까.
       그러면 옛 이름은 **kwargs 도 없이 TypeError 가 나거나(그나마 나음),
       시그니처가 유연하면 조용히 무시된다. 정의부를 직접 못 박는다.
    """
    fn = _def_node(tree, func_name)
    if fn is None:
        return False
    names = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    return kwarg in names


def required_kw_violations(tree, func_name, kwarg) -> list:
    """[(lineno, 설명)] — 그 함수를 호출하면서 필수 키워드를 안 넘긴 지점.

    호출 지점 인정 범위:
      · rc.integrated_sell_verdict(...)      → Attribute
      · integrated_sell_verdict(...)         → Name (from … import 경로)
    `**kwargs` 전달이 있으면 정적으로는 증명할 수 없으므로 통과시킨다
    (누락을 만들지 않기 위해서가 아니라, 거짓 빨간불을 만들지 않기 위해서다 —
     이 저장소에 그런 호출부는 현재 없고, 생기면 아래 목록 개수로 드러난다).
    """
    if tree is None:
        return []
    bad = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute):
            hit = (f.attr == func_name)
        elif isinstance(f, ast.Name):
            hit = (f.id == func_name)
        else:
            hit = False
        if not hit:
            continue
        kws = [k.arg for k in n.keywords]
        if None in kws:          # **kwargs 전달 — 정적 판정 불가
            continue
        if kwarg not in kws:
            bad.append((getattr(n, "lineno", 0),
                        _unparse(f) + "(…) 에 " + kwarg + " 없음"))
    return bad


def _count_call_sites(tree, func_name) -> int:
    if tree is None:
        return 0
    c = 0
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if (isinstance(f, ast.Attribute) and f.attr == func_name) or \
           (isinstance(f, ast.Name) and f.id == func_name):
            c += 1
    return c


print()
print("=" * 76)
print("A) 저장소 전역 — 필수 키워드 인자 계약 (허용목록 없음 · 0 이어야 한다)")
print("=" * 76)

for _own, _fn, _kw, _why in _A4_CONTRACTS:
    # A4a 정의부 명시 핀 — 인자 이름이 바뀌면 여기서 먼저 빨간불
    check(f"A4a {_own}.{_fn} 정의부가 {_kw} 를 받는다",
          declares_kwarg(TREES.get(_own), _fn, _kw), True)

    # A4b 호출부 전수
    # 소유 모듈(regime_core) 안의 내부 호출도 검사 대상에 넣는다 —
    # position_sell_verdict 가 실제 호출부다. 정의(FunctionDef)는 Call 이
    # 아니므로 자기 자신을 위반으로 잡는 오탐은 나지 않는다(M8c 가 확인).
    _v4, _sites = [], 0
    for m in sorted(TREES):
        _t = TREES[m]
        _sites += _count_call_sites(_t, _fn)
        for ln, why in required_kw_violations(_t, _fn, _kw):
            _v4.append(f"{m}.py:{ln}  {why}")

    check(f"A4b {_fn} 호출부 전부가 {_kw} 를 넘긴다", _v4, [])
    for x in _v4:
        print("        · " + x)
    if _v4:
        print("        ↳ 빼먹으면: " + _why)
    print(f"      (호출부 {_sites}곳을 추적 중 · 소유 {_own}.py)")


# ══════════════════════════════════════════════════════════════════════════
# B) 위성 백테스트 게이트 — diag_satellite_backtest.py 락스텝 짝
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ 로직을 복사하지 않는다. 실제 모듈을 import 해서 실제 함수를 호출하고,
#    배선은 실제 소스를 AST 로 읽는다. 사본을 시험하면 항상 초록불이 나온다.
import pandas as pd   # noqa: E402

import diag_satellite_backtest as sb   # noqa: E402
import run_signal_backtest as bt       # noqa: E402
# 창 환산 SSOT. `sb.fx` 로 우회하지 않는다 — 옛 사본이면 그쪽이 AttributeError 를
# 내면서 아래 _NEED 의 친절한 중단 메시지를 삼켜버린다.
import fmp_extras as fx                # noqa: E402

SB_SRC = SRCS.get("diag_satellite_backtest", "")
SB_TREE = TREES.get("diag_satellite_backtest")


def env_names(src: str) -> set:
    """모듈이 **실제로 읽는** 환경변수 이름 집합.

    ⚠️ 문자열 검색으로는 안 된다. '우회 스위치는 두지 않는다' 고 적어둔 **주석
    자체**에 걸려 오탐이 난다(2026-08-26 에 실제로 겪었다). 주석의 언급과 실제
    os.environ 접근을 구분하려면 AST 로 읽어야 한다.
    """
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for c in ast.walk(tree):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)):
            continue
        if c.func.attr not in ("get", "pop"):
            continue
        v = c.func.value
        if not (isinstance(v, ast.Attribute) and v.attr == "environ"):
            continue
        if c.args and isinstance(c.args[0], ast.Constant) \
                and isinstance(c.args[0].value, str):
            out.add(c.args[0].value)
    return out


def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _calls_attr(node, attr):
    for c in ast.walk(node):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == attr):
            return True
    return False


def _mentions(node, ident):
    for c in ast.walk(node):
        if isinstance(c, ast.Name) and c.id == ident:
            return True
    return False


def _body_mentions(fn, needle: str) -> bool:
    """함수 **코드**에 문자열이 있는가. 독스트링·주석은 제외한다.

    ⚠️ 파일 전역 문자열 검색으로 하면 안 된다. `_fmp_eod` 의 독스트링은 전환
       이력을 설명하느라 "limit" 을 세 번 언급하고, 같은 파일 `build_panels`
       에는 pandas 의 `ffill(limit=3)` 이 두 줄 있다. 전자는 주석이고 후자는
       FMP 와 무관한데, 전역 검색은 셋 다 걸어 **영구 빨간불**을 만든다.

    ast.unparse 는 주석을 애초에 버린다. 남는 것은 독스트링뿐이라 그것만 뗀다.
    get_source_segment 를 쓰면 주석까지 세어 거짓 실패가 난다 —
    diag_regime_window.price_window_ok 와 diag_universe_funnel.S6 이 같은
    함정을 밟고 고친 이력이 있어 그 기법을 그대로 가져왔다.
    """
    if fn is None:
        return False
    body = fn.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    try:
        code = "\n".join(ast.unparse(st) for st in body)
    except Exception:
        return False
    return needle in code


def _is_kwonly(fn, arg: str) -> bool:
    """`arg` 가 키워드 전용(*, arg)인가. 위치로도 받히면 False."""
    if fn is None:
        return False
    if any(a.arg == arg for a in fn.args.args):
        return False        # 위치로도 받힌다 — 옛 호출이 조용히 통과한다
    return any(a.arg == arg for a in fn.args.kwonlyargs)


def _has_default(fn, arg: str) -> bool:
    """`arg` 에 기본값이 달려 있는가(§7 — 달려 있으면 안 된다).

    ⚠️ 키워드 전용만 보면 안 된다. 초안은 `kwonlyargs` 만 훑었는데, 뮤테이션
       M-d/M-e(`*, bars` → `bars=1300`)에서 인자가 **위치로 내려가는 순간**
       kwonlyargs 에서 사라져 "기본값 없음"으로 통과했다(2026-09-03 실측).
       그 뮤테이션은 마침 B4d/B4e 가 잡았지만, 그러면 방어가 한 겹뿐이다.
       두 검사가 독립적이어야 한쪽을 완화해도 다른 쪽이 남는다.
    """
    if fn is None:
        return False
    a = fn.args
    # 위치 인자: defaults 는 **뒤에서부터** 대응한다 (앞쪽 인자는 기본값 없음)
    pos = list(a.posonlyargs) + list(a.args)
    off = len(pos) - len(a.defaults)
    for i, p in enumerate(pos):
        if p.arg == arg:
            return i >= off
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        if p.arg == arg:
            return d is not None
    return False


def sb_wiring(src: str) -> dict:
    """실제 소스에서 배선 사실만 뽑는다. 값 판정은 하지 않는다."""
    out = {
        "import_fh": False,
        "import_fx": False,          # fmp_extras(창 환산 SSOT)를 임포트하는가
        "ssot_call": False,          # _fmp_eod 가 fmp_get_ex 를 쓰는가
        "raw_get": True,             # _fmp_eod 에 원시 requests.get 이 있는가(있으면 안 됨)
        # ── v2.9 창 정책 (limit → from/to) ──────────────────────────────
        "eod_limit_code": True,      # _fmp_eod **코드**에 limit= 이 있는가(있으면 안 됨)
        "eod_range_params": False,   # _fmp_eod 가 hist_range_params 로 창을 만드는가
        "eod_bars_kwonly": False,    # _fmp_eod 의 bars 가 키워드 전용인가
        "eod_bars_default": True,    # 그 bars 에 기본값이 있는가(있으면 안 됨 §7)
        "batch_bars_kwonly": False,  # _batch_fetch 의 bars 가 키워드 전용인가
        "batch_bars_default": True,  # 그 bars 에 기본값이 있는가(있으면 안 됨 §7)
        "reason_counted": False,     # _batch_fetch 가 사유를 세는가
        "gate_exists": False,
        "gate_returns": False,
        "gate_before_write": False,  # 게이트가 시트 쓰기 **앞**인가
        "gate_op": None,
        "hash_sorted": False,
        "n_gates": 0,
    }
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out

    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "fmp_http":
                    out["import_fh"] = True
                if a.name == "fmp_extras":
                    out["import_fx"] = True

    f = _fn(tree, "_fmp_eod")
    if f is not None:
        out["ssot_call"] = _calls_attr(f, "fmp_get_ex")
        raw = False
        for c in ast.walk(f):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "get"
                    and isinstance(c.func.value, ast.Name)
                    and c.func.value.id == "requests"):
                raw = True
        out["raw_get"] = raw
        # 창 정책 — 본문 코드만 본다(독스트링·주석 제외). _body_mentions 참조.
        out["eod_limit_code"] = _body_mentions(f, "limit=")
        # 언급이 아니라 **호출**을 본다. 문자열로 보면 "hist_range_params 로
        # 바꿨다" 는 주석만 남기고 실제 호출을 지운 변경을 놓친다.
        out["eod_range_params"] = _calls_attr(f, "hist_range_params")
        out["eod_bars_kwonly"] = _is_kwonly(f, "bars")
        out["eod_bars_default"] = _has_default(f, "bars")

    b = _fn(tree, "_batch_fetch")
    if b is not None:
        out["batch_bars_kwonly"] = _is_kwonly(b, "bars")
        out["batch_bars_default"] = _has_default(b, "bars")
        for c in ast.walk(b):
            if (isinstance(c, ast.Subscript) and isinstance(c.value, ast.Name)
                    and c.value.id == "reasons"):
                out["reason_counted"] = True

    h = _fn(tree, "universe_hash")
    if h is not None:
        for c in ast.walk(h):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) \
                    and c.func.id == "sorted":
                out["hash_sorted"] = True

    m = _fn(tree, "main")
    if m is not None:
        gates, write_ln = [], None
        for c in ast.walk(m):
            if isinstance(c, ast.If) and _mentions(c.test, "MIN_FETCH_RATE"):
                gates.append(c)
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == "write_results"):
                if write_ln is None or c.lineno < write_ln:
                    write_ln = c.lineno
        gates.sort(key=lambda x: x.lineno)
        out["n_gates"] = len(gates)
        if gates:
            g = gates[0]
            out["gate_exists"] = True
            if isinstance(g.test, ast.Compare) and g.test.ops:
                out["gate_op"] = type(g.test.ops[0]).__name__
            out["gate_returns"] = any(
                isinstance(x, ast.Return) and isinstance(x.value, ast.Constant)
                and x.value.value == 1 for x in ast.walk(g))
            if write_ln is not None:
                # **모든** 게이트가 쓰기 앞이어야 한다. 첫 게이트만 보면 뒤쪽
                # 게이트를 쓰기 뒤로 옮긴 변경을 놓친다.
                out["gate_before_write"] = max(x.lineno for x in gates) < write_ln
    return out


SW = sb_wiring(SB_SRC)

print()
print("=" * 76)
print("B) 위성 백테스트 — 스로틀 배선")
print("=" * 76)

check("B1  fmp_http 를 임포트한다", SW["import_fh"], True)
check("B2  _fmp_eod 가 fmp_get_ex 를 호출한다", SW["ssot_call"], True)
check("B3  _fmp_eod 에 원시 requests.get 이 없다", SW["raw_get"], False)
check("B4  타임아웃이 15초 이상 (HISTORY_BARS 급 페이로드)", sb._FMP_TIMEOUT >= 15, True)

# ── v2.9: limit → from/to 창 정책 래칫 ────────────────────────────────────
# ⚠️ 이 6개가 없으면 고쳐도 되돌아온다. run_signal_backtest 가 정확히 그랬다 —
#    diag_hist_window[S] 는 run_watchlist_alerts 전용, diag_hist_window_consumers[H]
#    는 rotation_core 전용이라 어느 래칫에도 없는 파일이었다.
check("B4a fmp_extras(창 환산 SSOT)를 임포트한다", SW["import_fx"], True)
check("B4b _fmp_eod **코드**에 limit= 이 없다", SW["eod_limit_code"], False)
check("B4c _fmp_eod 가 hist_range_params 로 from/to 를 만든다",
      SW["eod_range_params"], True)
check("B4d _fmp_eod 의 bars 가 키워드 전용이다", SW["eod_bars_kwonly"], True)
check("B4e _batch_fetch 의 bars 도 키워드 전용이다 (중간층 포함)",
      SW["batch_bars_kwonly"], True)
check("B4f bars 에 기본값이 없다 (§7 — 두 층 모두)",
      (SW["eod_bars_default"], SW["batch_bars_default"]), (False, False))

# ── v2.9: 개명 래칫 ──────────────────────────────────────────────────────
# 개명은 장식이 아니다. 옛 이름을 남겨두면 이 상수를 빌려 쓰는 파일이 조용히
# 다른 값을 쓴다. 지워야 AttributeError 로 **크게** 죽는다.
check("B4g HISTORY_BARS 로 개명 · 옛 HISTORY_LIMIT 부재",
      (hasattr(sb, "HISTORY_BARS"), hasattr(sb, "HISTORY_LIMIT")), (True, False))

# 창 환산 정책을 이 파일이 복제하지 않는가 — 복제하면 비율은 한 벌이어도
# 정책이 여러 벌이 되고 나중에 한쪽만 갱신된다.
#
# ⚠️ 문자열 검색으로 하면 안 된다. 초안에서 `"0.6871" in SB_SRC` 로 짰다가
#    `_window_days_for` 독스트링의 **"여기서 0.6871 을 복제하지 않는다"** 라는
#    문장에 걸려 곧바로 오탐이 났다(2026-09-03 실측). 규칙을 적어둔 주석이
#    규칙 위반으로 집계되는 것이 이 계열 검사의 고질적 실패다.
#    숫자 리터럴만 보면 주석(파서가 버림)과 독스트링(문자열 상수)이 둘 다 빠진다.
def _has_float_literal(tree, value: float) -> bool:
    if tree is None:
        return False
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, float) \
                and abs(n.value - value) < 1e-12:
            return True
    return False


check("B4h 환산 비율을 숫자 리터럴로 복제하지 않는다 (fmp_extras 소유)",
      _has_float_literal(SB_TREE, fx.HIST_TD_PER_CD), False)

# ⚠️ 상한 경고의 **런타임** 검증(B4i~B4k)은 아래 _NEED 관문 뒤에 있다.
#    여기서 sb._window_days_for 를 만지면 옛 사본일 때 AttributeError 로
#    터져서, 아래의 "v2.8 이전 버전이다" 라는 친절한 중단 메시지가 영영
#    안 나온다. 진단이 왜 죽었는지 읽을 수 없는 것이 가장 나쁜 실패다.

# ── 선행 조건 — 이하는 v2.8 심볼이 있어야 돌아간다 ────────────────────────
_NEED = ("fh", "fx", "_fmp_eod", "_batch_fetch", "universe_hash",
         "_env_fetch_rate", "MIN_FETCH_RATE", "_INFRA_KINDS",
         # v2.9 — 옛 사본이면 여기서 크게 죽는다. 조용히 통과하는 것보다 낫다.
         "HISTORY_BARS", "_window_days_for", "_WARNED_CEILING")
_MISSING = [a for a in _NEED if not hasattr(sb, a)]
if _MISSING:
    print()
    print("=" * 76)
    print("❌ 중단 — diag_satellite_backtest 가 v2.9 이전 버전이다")
    print("   없는 심볼: " + ", ".join(_MISSING))
    print("   두 파일을 함께 배포할 것 (락스텝)")
    print("=" * 76)
    sys.exit(1)

# ── v2.9: 상한 경고 런타임 검증 ──────────────────────────────────────────
# limit 을 from/to 로 바꾸는 것만으로는 원래 위험이 안 없어진다. "숫자를 올려도
# 아무 일 없음"이 FMP 의 미공개 quirk 에서 HIST_MAX_DAYS 상한으로 **자리만
# 옮긴다.** 경고가 그 이동을 눈에 보이게 하는 유일한 장치이므로 래칫으로 조인다.
_warned, _quiet = [], []
sb._WARNED_CEILING.clear()
sb._window_days_for(99_999, warn=_warned.append)      # 반드시 상한에 걸린다
sb._WARNED_CEILING.clear()
sb._window_days_for(50, warn=_quiet.append)           # 상한과 무관
check("B4i 상한에 걸리면 경고한다", len(_warned), 1)
check("B4j 상한 미만이면 조용하다", len(_quiet), 0)
sb._WARNED_CEILING.clear()
_dup = []
sb._window_days_for(99_999, warn=_dup.append)
sb._window_days_for(99_999, warn=_dup.append)         # 같은 bars 재호출
check("B4k 같은 요구는 1회만 경고한다 (8워커 동시호출 대비)", len(_dup), 1)
sb._WARNED_CEILING.clear()


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("B) 위성 백테스트 — 실패 분류 (스텁 주입 · 네트워크 없음)")
print("=" * 76)

_TK = [f"T{i:02d}" for i in range(10)]


def _mk_px():
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    return pd.DataFrame({"px": [100.0 + i for i in range(30)]}, index=idx)


def _stub_eod(tk, ep, *, bars=None):
    """티커별로 서로 다른 실패 사유를 낸다 — 사유가 보존되는지 보기 위함.

    v2.9: 시그니처를 `(tk, ep, *, bars=None)` 으로 맞췄다. 옛 `limit=None` 으로
    두면 `_batch_fetch` 가 `bars=` 로 부르는 순간 TypeError 가 나고, 그것을
    워커의 `except Exception` 이 삼켜 **전 항목이 조용히 "exception" 으로
    집계된다.** B5~B9 가 전부 실패하는데 원인은 스텁 쪽이라, 진단이 대상
    코드를 잘못 고발하게 된다. 아래 P2 가 이 상황을 명시적으로 재현한다.
    """
    i = int(tk[1:])
    if ep == "full":
        if i == 8:
            return pd.DataFrame(), "rate_limited"
        if i == 9:
            return pd.DataFrame(), "empty"
        return _mk_px(), "ok"                       # T00~T07 = 8건
    if i == 7:
        return pd.DataFrame(), "rate_limited"
    if i in (6, 8, 9):
        return pd.DataFrame(), "empty"
    return _mk_px(), "ok"                           # T00~T05 = 6건


_real_eod = sb._fmp_eod
sb._fmp_eod = _stub_eod
try:
    _raw, _adj, _fb, _rs, _fd = sb._batch_fetch(_TK, bars=sb.HISTORY_BARS)
finally:
    sb._fmp_eod = _real_eod

check("B5  원종가 확보 = 8종목", len(_raw), 8)
check("B6  배당조정 폴백 = 2종목 (T06 empty · T07 rate_limited)",
      sorted(_fb), ["T06", "T07"])
check("B7  사유가 (엔드포인트, kind) 로 보존된다",
      (_rs.get(("full", "ok")), _rs.get(("full", "rate_limited")),
       _rs.get(("dividend-adjusted", "rate_limited"))), (8, 1, 1))
check("B8  카운트 합계 = 티커 × 엔드포인트 (누락 없음)",
      sum(_rs.values()), len(_TK) * 2)
check("B9  탈락 목록에 티커·엔드포인트·사유가 함께 남는다", len(_fd), 6)


def _boom_eod(tk, ep, *, bars=None):
    raise RuntimeError("worker died")


sb._fmp_eod = _boom_eod
try:
    _r2, _a2, _f2, _rs2, _fd2 = sb._batch_fetch(_TK, bars=sb.HISTORY_BARS)
finally:
    sb._fmp_eod = _real_eod

check("B10 워커 예외가 삼켜지지 않고 'exception' 으로 집계된다",
      _rs2.get(("full", "exception")), 10)
check("B11 빈 입력도 5-튜플을 돌려준다",
      len(sb._batch_fetch([], bars=sb.HISTORY_BARS)), 5)


# ── 양성대조 — 옛 계약으로 되돌리면 실제로 깨지는가 ────────────────────────
def _old_style_eod(tk, ep, *, bars=None):
    return pd.DataFrame()          # v2.7 이하: kind 없이 DataFrame 만


sb._fmp_eod = _old_style_eod
try:
    _r3, _a3, _f3, _rs3, _fd3 = sb._batch_fetch(_TK, bars=sb.HISTORY_BARS)
finally:
    sb._fmp_eod = _real_eod

check("P1  양성대조: 옛 계약으로 되돌리면 전량 exception 으로 잡힌다",
      (len(_r3), _rs3.get(("full", "exception"))), (0, 10))


# ── P2 하네스 자기검증 — 스텁 시그니처가 낡으면 어떻게 보이는가 ────────────
# 위 B5~B9 는 "스텁이 제대로 불린다"는 전제 위에 서 있다. 그 전제가 깨지면
# 어떻게 되는지를 여기서 **직접 재현**한다: `_batch_fetch` 는 `bars=` 로 부르는데
# 스텁이 옛 `limit=` 만 받으면 TypeError 가 나고, 워커의 `except Exception` 이
# 그것을 삼켜 전 항목이 "exception" 으로 집계된다.
#
# 이게 왜 중요한가 — 그 모습은 **대상 코드가 고장난 것과 구별되지 않는다.**
# 2026-09-03 diag_universe_funnel 에서 실제로 같은 함정을 밟았다. P2 가 있으면
# B5~B9 가 무더기로 빨간불일 때 "스텁을 안 고쳤나?" 를 먼저 의심할 수 있다.
def _unmigrated_stub(tk, ep, limit=None):        # 일부러 옛 시그니처
    return _mk_px(), "ok"


sb._fmp_eod = _unmigrated_stub
try:
    _r4, _a4, _f4, _rs4, _fd4 = sb._batch_fetch(_TK, bars=sb.HISTORY_BARS)
finally:
    sb._fmp_eod = _real_eod

check("P2  하네스 자기검증: 스텁이 옛 시그니처면 전량 exception 이 된다 "
      "(대상 코드 고장과 구별 불가 — 스텁부터 의심할 것)",
      (len(_r4), _rs4.get(("full", "exception"))), (0, 10))


# ── P3 양성대조 — bars 가 실제로 URL 까지 도달하는가 ──────────────────────
# B4c 는 `hist_range_params` 를 **호출**하는지만 본다. 호출해놓고 결과를 URL 에
# 안 붙이면 통과한다. 실제 URL 을 잡아 눈으로 확인하는 것이 유일한 확인이다.
_urls = []


def _capture(url, timeout=None):
    _urls.append(url)
    return None, 429, "rate_limited"


_real_get, _real_key = sb.fh.fmp_get_ex, sb.FMP_API_KEY
sb.fh.fmp_get_ex, sb.FMP_API_KEY = _capture, "TESTKEY"
try:
    sb._fmp_eod("SPY", "full", bars=400)
finally:
    sb.fh.fmp_get_ex, sb.FMP_API_KEY = _real_get, _real_key

def _from_of(url: str) -> str:
    """URL 의 `from=` 값. 없으면 빈 문자열.

    ⚠️ `url.split("from=")[1]` 로 짰다가 뮤테이션 M-l(창을 만들되 URL 에 안
       붙임)에서 IndexError 로 **진단 전체가 죽었다**(2026-09-03 실측).
       요약 줄조차 못 찍어서, 몇 건이 실패했는지도 알 수 없었다.
       검사는 빨간불이 되어야지 크래시하면 안 된다 — 크래시는 빨간불보다 나쁘다.
    """
    if "from=" not in url:
        return ""
    return url.split("from=", 1)[1].split("&", 1)[0]


_u = _urls[0] if _urls else ""
check("P3  실제 URL 에 limit= 이 없다", "limit=" in _u, False)
check("P4  실제 URL 에 from=/to= 가 있다",
      ("from=" in _u and "to=" in _u), True)
# 창이 bars 를 따라 움직이는가 — 상수를 URL 에 박아둔 변경을 잡는다.
_urls.clear()
sb.fh.fmp_get_ex, sb.FMP_API_KEY = _capture, "TESTKEY"
try:
    sb._fmp_eod("SPY", "full", bars=40)
finally:
    sb.fh.fmp_get_ex, sb.FMP_API_KEY = _real_get, _real_key
_u2 = _urls[0] if _urls else ""
check("P5  bars 를 줄이면 from= 도 따라 움직인다 (창이 고정값이 아니다)",
      (_from_of(_u2) != "" and _from_of(_u2) != _from_of(_u)), True)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("B) 위성 백테스트 — 게이트 · 지문 · 결과 열")
print("=" * 76)

check("B12 게이트가 존재한다", SW["gate_exists"], True)
check("B13 게이트 비교가 '<' 이다 (>= 로 뒤집히면 정상 run 이 막힌다)",
      SW["gate_op"], "Lt")
check("B14 게이트가 실제로 빠져나간다 (return 1)", SW["gate_returns"], True)
# ⚠️ 존재 검사만으로는 부족하다. 게이트를 시트 쓰기 뒤로 옮기면 오염 행은
#    그대로 들어가고 종료 코드만 1이 된다 — 로그에도 이상이 없다.
check("B15 **모든** 게이트가 시트 쓰기보다 앞이다", SW["gate_before_write"], True)
check("B16 게이트가 둘이다 (원종가 · 배당조정 인프라성 실패)", SW["n_gates"], 2)
check("B17 기본 임계 0.98", sb.MIN_FETCH_RATE, 0.98)
# 'empty'(그 시리즈가 원래 없음)를 인프라성으로 세면 게이트가 영구 빨간불이 되고,
# 반대로 빼먹으면 진짜 오염을 놓친다.
check("B18 인프라성 사유에 'empty' 가 없다", "empty" in sb._INFRA_KINDS, False)
check("B19 인프라성 사유에 'rate_limited' 가 있다",
      "rate_limited" in sb._INFRA_KINDS, True)

check("B20 결과 열 마지막이 Universe_Hash", sb._RESULT_COLS[-1], "Universe_Hash")

# 해시: 존재 검사로는 부족하다. sorted() 없이 계산하면 구성이 같아도 run 마다
# 값이 달라지는데, 겉보기에는 '해시가 잘 찍히는' 정상 동작으로 보인다.
_H1 = sb.universe_hash(["AAPL", "MSFT", "NVDA"])
_H2 = sb.universe_hash(["NVDA", "aapl", " MSFT "])
check("B21 순서·대소문자·공백이 달라도 같은 지문", _H1, _H2)
check("B22 구성이 다르면 지문도 다르다",
      sb.universe_hash(["AAPL", "MSFT"]) != _H1, True)
# USER_ENTERED 로 쓰기 때문에 16진수 8자가 전부 숫자면(≈2.3%) 앞자리 0 이 날아간다.
check("B23 지문에 'u' 접두어가 있다 (USER_ENTERED 숫자 해석 방지)",
      _H1.startswith("u") and len(_H1) == 9, True)
check("B24 빈 유니버스는 빈 문자열", sb.universe_hash([]), "")

# ── 복제 대조 — run_signal_backtest 와 같은 구현이어야 한다 ────────────────
# 두 파일이 같은 함수를 각자 갖고 있다(공유 모듈로 빼면 diag_universe_funnel
# 까지 손대야 해서 미뤘다). 복제는 반드시 어긋난다 — 그래서 여기서 대조한다.
_SAMPLES = (["AAPL", "MSFT"], ["nvda", "AAPL ", "MSFT"], [], ["SPY"])
check("B25 universe_hash 가 run_signal_backtest 와 완전히 동일",
      [sb.universe_hash(x) for x in _SAMPLES],
      [bt.universe_hash(x) for x in _SAMPLES])
_RATES = ("", "0.5", "1.0", "abc", "1.5", "-0.1", " 0.98 ")
check("B26 _env_fetch_rate 가 run_signal_backtest 와 완전히 동일",
      [sb._env_fetch_rate(x) for x in _RATES],
      [bt._env_fetch_rate(x) for x in _RATES])

# ── 우회 스위치 부재 ──────────────────────────────────────────────────────
_ENV = env_names(SB_SRC)
_BYPASS = sorted(n for n in _ENV
                 if n.startswith("SKIP_") or n.startswith("FORCE_")
                 or n.startswith("IGNORE_"))
check("B27 우회 스위치(SKIP_*/FORCE_*/IGNORE_*)가 없다", _BYPASS, [])
check("B28 MIN_FETCH_RATE 를 환경변수로 읽는다", "MIN_FETCH_RATE" in _ENV, True)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
print("B) 뮤테이션 역검증 — 위 검사가 결함을 실제로 잡는가")
print("=" * 76)

# ⚠️ 뮤테이션이 레이블대로 동작하는지 확인할 것. '무검출'은 코드가 아니라
#    뮤테이션이 틀렸다는 신호일 수 있다(2026-08-26 에 실제로 겪었다).
MUTANTS = [
    ("M1 SSOT 우회 — fh.fmp_get_ex 를 requests.get 으로",
     "r, _status, kind = fh.fmp_get_ex(url, timeout=_FMP_TIMEOUT)",
     "r = requests.get(url, timeout=_FMP_TIMEOUT); kind = 'ok'",
     ["ssot_call", "raw_get"]),
    ("M2 사유 집계 제거",
     "            reasons[(ep, kind)] = reasons.get((ep, kind), 0) + 1",
     "            pass",
     ["reason_counted"]),
    ("M3 게이트 비교 뒤집기 (< → >)",
     "    if _rate < MIN_FETCH_RATE:",
     "    if _rate > MIN_FETCH_RATE:",
     ["gate_op"]),
    ("M4 게이트 이탈 제거 (return 1 → pass)",
     '              "_FMP_TIMEOUT 을 늘린다.")\n        return 1',
     '              "_FMP_TIMEOUT 을 늘린다.")\n        pass',
     ["gate_returns"]),
    ("M5 해시 sorted() 제거",
     "    seq = sorted(str(t).strip().upper() for t in (tickers or [])",
     "    seq = list(str(t).strip().upper() for t in (tickers or [])",
     ["hash_sorted"]),
]

for name, old, new, keys in MUTANTS:
    n_occ = SB_SRC.count(old)
    if n_occ != 1:
        print("  ⚠️  " + name + " — 앵커가 " + str(n_occ) + "회(1회여야 함). 스킵")
        _fails.append(name + " [앵커 " + str(n_occ) + "회]")
        continue
    mw = sb_wiring(SB_SRC.replace(old, new, 1))
    flipped = [k for k in keys if mw.get(k) != SW.get(k)]
    if flipped:
        print("  ✅ " + name + " — " + ", ".join(flipped) + " 가 뒤집힘")
        _passes += 1
    else:
        print("  ❌ " + name + " — **배선 검사가 잡아내지 못함**")
        _fails.append(name)

# ── M6 게이트 순서 — 존재 검사로는 절대 못 잡는 유형 ──────────────────────
_W = "    write_results(all_rows)\n"
_GH = "    if _rate < MIN_FETCH_RATE:\n"
if SB_SRC.count(_W) == 1 and SB_SRC.count(_GH) == 1:
    _moved = SB_SRC.replace(_W, "", 1).replace(_GH, _W + _GH, 1)
    _mw = sb_wiring(_moved)
    if _mw.get("gate_exists") and not _mw.get("gate_before_write"):
        print("  ✅ M6 시트 쓰기를 게이트 앞으로 이동 — 순서 검사가 잡아냄")
        _passes += 1
    else:
        print("  ❌ M6 시트 쓰기를 게이트 앞으로 이동 — **잡아내지 못함**")
        _fails.append("M6 게이트 순서")
else:
    print("  ⚠️  M6 — 앵커 없음. 스킵")
    _fails.append("M6 [앵커 소실]")

# ── M7 A2 스캐너 자체의 판별력 ────────────────────────────────────────────
# 가드가 계약 위반을 정말 잡는지, 위반을 인공으로 만들어 확인한다.
_probe = '''
import run_signal_backtest as bt
def f():
    df = bt._fmp_price_history("SPY")
    return df.empty
'''
_pm = "___contract_probe___"
TREES[_pm] = ast.parse(_probe)
SRCS[_pm] = _probe
REG[_pm] = tuple_returning(TREES[_pm])
try:
    _hits = contract_violations(_pm)
finally:
    for _d in (TREES, SRCS, REG):
        _d.pop(_pm, None)
if _hits:
    print("  ✅ M7 인공 계약 위반을 A2 스캐너가 잡아냄 — " + _hits[0][1][:52])
    _passes += 1
else:
    print("  ❌ M7 인공 계약 위반을 **잡아내지 못함** — A2 는 판별력이 없다")
    _fails.append("M7 A2 판별력")

# ── M8 A4 스캐너 자체의 판별력 ────────────────────────────────────────────
# 초록불인 가드는 두 가지 이유로 초록불일 수 있다: 위반이 없거나, 못 잡거나.
# 셋을 인공으로 만들어 구분한다 — 누락 검출 · 오탐 없음 · 정의부 핀.
#   ⚠️ A4 는 지금 위반 0곳에서 출발한다. 판별력 증명이 없으면 이 가드는
#      영원히 초록불인 채 아무것도 안 지킨다(2026-08-27 diag_regime_window
#      X4 에서 실제로 겪은 실패 모드).
_A4_BAD = '''
import regime_core as rc
def f(feats, price, i):
    return rc.integrated_sell_verdict(
        above_ma200=True, one_month_return=0.0, rsi=50.0,
        macd_signal="ABOVE_SIGNAL", pct_from_52w_high=-1.0,
        drawdown_from_high_pct=-2.0,
    )
'''
_A4_GOOD = '''
import regime_core as rc
def f(feats, price, i):
    return rc.integrated_sell_verdict(
        above_ma200=True, one_month_return=0.0, rsi=50.0,
        macd_signal="ABOVE_SIGNAL", pct_from_52w_high=-1.0,
        drawdown_from_high_pct=-2.0, gap_ma200_pct=-3.0,
    )
'''
_A4_NODECL = '''
def integrated_sell_verdict(*, above_ma200, one_month_return, rsi,
                            macd_signal, pct_from_52w_high,
                            drawdown_from_high_pct):
    return ("보유", "")
'''

_m8_bad = required_kw_violations(ast.parse(_A4_BAD),
                                 "integrated_sell_verdict", "gap_ma200_pct")
if _m8_bad:
    print("  ✅ M8a 인공 인자 누락을 A4 가 잡아냄 — " + _m8_bad[0][1][:52])
    _passes += 1
else:
    print("  ❌ M8a 인공 인자 누락을 **잡아내지 못함** — A4 는 판별력이 없다")
    _fails.append("M8a A4 판별력")

_m8_good = required_kw_violations(ast.parse(_A4_GOOD),
                                  "integrated_sell_verdict", "gap_ma200_pct")
if not _m8_good:
    print("  ✅ M8b 정상 호출을 A4 가 오탐하지 않음")
    _passes += 1
else:
    print("  ❌ M8b 정상 호출을 위반으로 **오탐** — " + _m8_good[0][1][:52])
    _fails.append("M8b A4 오탐")

_m8_tree = ast.parse(_A4_NODECL)
_m8_decl_bad = declares_kwarg(_m8_tree, "integrated_sell_verdict", "gap_ma200_pct")
_m8_self = required_kw_violations(_m8_tree, "integrated_sell_verdict", "gap_ma200_pct")
if (not _m8_decl_bad) and (not _m8_self):
    print("  ✅ M8c 정의부 핀이 인자 소실을 잡고, 정의 자체를 호출로 오탐하지 않음")
    _passes += 1
else:
    print("  ❌ M8c 정의부 핀 실패 — "
          + ("인자 없는 정의를 통과시킴" if _m8_decl_bad else "")
          + ("정의를 호출 위반으로 오탐" if _m8_self else ""))
    _fails.append("M8c A4 정의부 핀")

# ── M9 — 변수에 담긴 FMP URL 을 A1 이 보는가 ───────────────────────────────
# 2026-08-27 실패에서 나왔다. `_fmp_url_names` 가 문자열 상수 직접 대입만 보던
# 시절, 아래 M9a 모양은 A1 이 초록불을 냈다. 저장소에 이 모양이 10곳 있었고
# 그중 하나는 코어 모듈(calendar_core)이었다.
#
# M9b 는 반대편을 지킨다: 이름 매칭을 부분 문자열로 하면 `url` 이라는 이름이
# `cfg['url']`(RSS 피드 주소)에 걸려 narrative_core 가 2 → 3 으로 부풀었다.
# 두 방향을 같이 걸어야 '엄격해지기'와 '정확해지기'가 구분된다.
_M9A = """
import requests
FMP_BASE = "https://financialmodelingprep.com/stable"
def go(sym):
    url = FMP_BASE + "/quote?symbol=" + sym + "&apikey=" + KEY
    return requests.get(url, timeout=10)
"""
_M9B = """
import requests
FMP_BASE = "https://financialmodelingprep.com/stable"
def go(cfg):
    url = FMP_BASE + "/quote?apikey=" + KEY
    return requests.get(cfg['url'], timeout=10)
"""


# M9c — 고정점 반복이 실제로 필요한 모양. `b = a + …` 가 `a = …` 보다 **먼저**
# 순회되므로 1회 통과로는 b 를 못 잡는다(모듈 말미에 상수를 두는 흔한 배치다).
# 이 케이스가 없으면 `for _ in range(6)` 루프가 1회로 잘려도 아무도 모른다.
# ⚠️ `ast.walk` 은 BFS 다(소스 순서가 아니다). 그래서 사용처를 정의보다 앞에,
#    **같은 깊이**에 둬야 1회 통과로 못 잡는다. 이름은 `_q1` 처럼 길게 쓴다 —
#    `a` 같은 한 글자로 두면 `_fmp_url_names` 의 부분 문자열 매칭에 우연히
#    걸려 통과해 버려서, 정작 고정점을 시험하지 못한다.
_M9C = """
import requests
_q2 = _q1 + "&limit=5"
def go():
    return requests.get(_q2, timeout=10)
_q1 = "https://financialmodelingprep.com/stable/quote?apikey=" + KEY
"""

# M9d — 증강 대입(`url += …`)으로 조각을 이어 붙이는 모양. 시작이 빈 문자열이면
# 표식이 첫 줄에 없어 `ast.Assign` 만 보는 구현은 통째로 놓친다.
_M9D = """
import requests
def go(sym):
    url = ""
    url += "https://financialmodelingprep.com/stable/quote?symbol=" + sym
    url += "&apikey=" + KEY
    return requests.get(url, timeout=10)
"""


def _a1_hits_on(src_text: str) -> int:
    """합성 소스에 **진짜 A1 탐지기**(`raw_fmp_gets`)를 적용한다.

    ⚠️ 판정 규칙을 여기 복제하지 않는다. 복제하면 `raw_fmp_gets` 를 무력화하는
       변경(`argn & names` 제거 등)에도 이 검사가 초록불을 내준다 — 실제로
       2026-08-27 에 복제본으로 짰다가 뮤테이션 MX-b 에서 그 구멍이 드러났다.
       TREES 에 임시로 밀어 넣고 원본을 호출한 뒤 반드시 되돌린다.
    """
    key = "__m9_probe__.py"
    TREES[key] = ast.parse(src_text)
    SRCS[key] = src_text
    try:
        return len(raw_fmp_gets(key))
    finally:
        TREES.pop(key, None)
        SRCS.pop(key, None)


# M10 — 별칭으로 래칫을 우회할 수 있는가. 변이 P10 이 이 구멍을 드러냈다.
#   a: `import requests as _rq` → `_rq.get(...)`
#   b: `from requests import get` → `get(...)`
#   c: `_g = requests.get` → `_g(...)`
#   d: **오탐 대조** — requests 와 무관한 객체의 `.get()`(dict 조회 등)은 잡히면 안 된다.
#      이게 없으면 "attr == 'get' 이면 전부 히트" 로 바꿔도 a~c 는 통과한다.
_M10A = """
import requests as _rq
def go(sym):
    return _rq.get("https://financialmodelingprep.com/stable/quote?symbol=" + sym, timeout=8)
"""
_M10B = """
from requests import get
def go(sym):
    return get("https://financialmodelingprep.com/stable/quote?symbol=" + sym, timeout=8)
"""
_M10C = """
import requests
_g = requests.get
def go(sym):
    return _g("https://financialmodelingprep.com/stable/quote?symbol=" + sym, timeout=8)
"""
_M10D = """
import fmp_http as fh
def go(cfg, sym):
    url = "https://financialmodelingprep.com/stable/quote?symbol=" + sym
    _ = cfg.get(url)
    return fh.fmp_get(url, timeout=8)
"""
_m10 = [_a1_hits_on(s) for s in (_M10A, _M10B, _M10C, _M10D)]
if _m10 == [1, 1, 1, 0]:
    _passes += 1
    print("  ✅ M10 별칭 우회 탐지 — import as(a) · from import(b) · "
          "변수 바인딩(c) · dict.get 오탐 없음(d)")
else:
    _fails.append(f"M10 별칭 우회 탐지 — 기대 [1,1,1,0], 실제 {_m10}")
    print(f"  ❌ M10 별칭 우회 탐지 — 기대 [1,1,1,0], 실제 {_m10}")

_m9a, _m9b = _a1_hits_on(_M9A), _a1_hits_on(_M9B)
_m9c, _m9d = _a1_hits_on(_M9C), _a1_hits_on(_M9D)
if _m9a == 1 and _m9b == 0 and _m9c == 1 and _m9d == 1:
    _passes += 1
    print("  ✅ M9 변수 URL(a) · 겹친 이름 오탐 없음(b) · 다단 전파(c) · "
          "증강 대입(d) 전부 정상")
else:
    print("  ❌ M9 실패 — 변수URL=" + str(_m9a) + "(1) · 오탐=" + str(_m9b)
          + "(0) · 다단전파=" + str(_m9c) + "(1) · 증강대입=" + str(_m9d) + "(1)")
    _fails.append("M9 A1 변수 URL 탐지")


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 76)
if _fails:
    print("❌ 실패 " + str(len(_fails)) + "건 / 통과 " + str(_passes) + "건")
    for x in _fails:
        print("   - " + str(x))
    sys.exit(1)
print("✅ 전부 통과 — " + str(_passes) + "건")
if _notes:
    print()
    print("⚠️  기준선 갱신 권고 " + str(len(_notes)) + "건 (실패 아님):")
    for x in _notes:
        print("   - " + x)
print("=" * 76)
