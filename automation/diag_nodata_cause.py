"""diag_nodata_cause.py — A-2b 필드 프로브. 미수신 '원인 판정' 경로 실측.

무엇을 결정하려는 프로브인가
────────────────────────────
A-2a 가 만든 미수신 감지는 지금 사용자에게 이렇게 말한다:

    "티커 변경·상장폐지가 흔한 원인이니 확인하세요."

원인을 **추측해서 사용자에게 떠넘기고** 있다. 보유 종목이 상폐된 건지 FMP 가
하루 삐끗한 건지 사람이 직접 확인해야 한다. A-2b 는 이걸 기계가 판정하게 한다.

경로가 두 갈래고 비용이 완전히 다르다:

  경로 A (종목별) : profile?symbol=X 의 isActivelyTrading 으로 직접 판정.
                    미수신 티커 수만큼 N 콜. 정밀. 회사명까지 온다.
                    ⚠️ 상폐 종목에 profile 이 빈 배열을 주면 이 경로는 죽는다.

  경로 B (전역)   : symbol-change / delisted-companies 를 받아 로컬 매칭.
                    미수신이 몇 개든 1~2 콜 고정.
                    ⚠️ 심볼 필터가 없으면 최근 얼마를 받아야 커버되는지 모른다.
                       모르는 채 구현하면 "3개월 전 상폐는 못 잡는" 조용한
                       구멍이 생긴다 — A-2a 가 없애려던 침묵과 같은 종류다.

가장 위험한 함정 — 파라미터 무시
────────────────────────────────
FMP 는 모르는 파라미터를 **에러 없이 무시하고 전체 피드를 200 으로** 돌려주는
경우가 있다. 그걸 '필터 지원'으로 오독하면 엉뚱한 티커에 엉뚱한 원인이 붙는다.
**원인을 모르는 것보다 틀린 원인을 붙이는 게 나쁘다.** 그래서 이 프로브는
필터 결과를 무필터 결과와 대조해 '무시됨'을 별도 판정으로 뽑는다.

자기 시드 방식
──────────────
상폐·개명 티커를 하드코딩하지 않는다(TWTR·ATVI 같은 기억은 낡는다).
피드에서 **실제 최근 사례를 뽑아** 그 티커로 후속 검사를 건다.

호출 예산: 기본 11 콜. PROBE_HEAVY=1 이면 +1 (actively-trading-list, 2.6만건).

    FMP_API_KEY=... python automation/diag_nodata_cause.py

네트워크만 쓴다. 시트 접근·쓰기 없음. 부작용 없음.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

FMP_BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 20
SLEEP_SEC = 0.35

_KEY = str(os.environ.get("FMP_API_KEY", "") or "").strip()
_HEAVY = str(os.environ.get("PROBE_HEAVY", "") or "").strip() in ("1", "true", "yes")

# PROBE_MODE=membership 이면 아래 '멤버십 판별' 블록만 돈다(6콜).
# 빈 값이면 기존 11콜 프로브가 **한 글자도 바뀌지 않은 채** 그대로 돈다.
# 기존 경로를 건드리지 않는 이유: 그 실측 결과가 A-2b 설계의 근거 기록이다.
_MODE = str(os.environ.get("PROBE_MODE", "") or "").strip().lower()
# 모르는 값이면 **조용히 기본 모드로 떨어지지 않는다.** 2026-08-22 에 워크플로가
# membership2 를 빈 문자열로 매핑해 기본 11콜을 태웠고, 로그만 봐서는 다른 게
# 돌았다는 걸 알기 어려웠다. 조용한 폴백은 이 프로젝트가 내내 싸워온 실패 유형이다.
_VALID_MODES = ("", "membership", "membership2")

_TODAY = datetime.now(timezone.utc).date()
_D_TO = _TODAY.isoformat()
_D_FROM_90 = (_TODAY - timedelta(days=90)).isoformat()
_D_FROM_365 = (_TODAY - timedelta(days=365)).isoformat()

_CALLS = 0


def _mask(text):
    """로그에 API 키가 남지 않게 한다."""
    if not _KEY:
        return str(text)
    return str(text).replace(_KEY, "***")


def call(path, keep=False):
    """단일 호출. (verdict, data, detail) 반환. 예외를 밖으로 내보내지 않는다."""
    global _CALLS
    _CALLS += 1
    sep = "&" if "?" in path else "?"
    url = FMP_BASE + "/" + path + sep + "apikey=" + _KEY
    try:
        r = requests.get(url, timeout=TIMEOUT)
    except Exception as e:
        return "EXC", None, type(e).__name__ + ": " + _mask(str(e))[:90]
    finally:
        time.sleep(SLEEP_SEC)

    sc = r.status_code
    if sc == 402:
        return "PLAN", None, "플랜 미포함 — 코드로 해결 안 됨"
    if sc in (401, 403):
        return "AUTH", None, "키 문제 또는 권한 없음"
    if sc == 429:
        return "RATE", None, "레이트리밋 — 잠시 후 재실행"
    if sc == 404:
        return "404", None, "경로 없음"
    if sc != 200:
        return "HTTP", None, "HTTP " + str(sc)

    try:
        data = r.json()
    except Exception:
        return "NOJSON", None, "본문 앞: " + _mask((r.text or "")[:70]).replace("\n", " ")

    if isinstance(data, dict):
        low = [str(k).lower() for k in data.keys()]
        if any(k.startswith("error") for k in low):
            return "ERRMSG", None, _mask(json.dumps(data, ensure_ascii=False))[:110]
        return "OK", data, "dict 응답"
    if isinstance(data, list):
        if not data:
            return "EMPTY", [], "200 인데 빈 배열"
        return "OK", data, str(len(data)) + "건"
    return "ODD", None, "예상 밖 타입: " + type(data).__name__


def show(tag, verdict, detail, extra=""):
    icon = {"OK": "✅", "EMPTY": "⬜", "PLAN": "🔒", "AUTH": "🔑",
            "404": "❌", "RATE": "⏳"}.get(verdict, "⚠️")
    print(f"  {icon} [{verdict}] {tag}")
    print(f"       {detail}")
    if extra:
        for line in str(extra).splitlines():
            print("       " + line)


def keys_of(data, n=16):
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return sorted(data[0].keys())[:n]
    if isinstance(data, dict):
        return sorted(data.keys())[:n]
    return []


def pick(d, *names):
    """대소문자·표기 흔들림에 견디는 필드 추출."""
    if not isinstance(d, dict):
        return None
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def dates_in(data, *fields):
    out = []
    for row in (data or []):
        v = pick(row, *fields)
        if v:
            out.append(str(v)[:10])
    return sorted(x for x in out if len(x) == 10)


FIND = {}   # 최종 판정 누적


# ══════════════════════════════════════════════════════════════════════════
# 멤버십 판별 모드 (PROBE_MODE=membership · 6콜)
# ══════════════════════════════════════════════════════════════════════════
# 무엇을 결정하나
#   actively-trading-list(2.6만건, 1콜)를 **생사 판별자**로 쓸 수 있는가.
#   쓸 수 있다면 A-2b 의 종목당 profile N콜을 "리스트 1콜 + 잔여만 profile"
#   로 줄이고, 나아가 데이터가 비기를 기다리지 않는 **선제 점검**이 가능해진다.
#
# ⚠️ 이 프로브가 답해야 하는 건 "리스트가 오는가" 가 아니다. 그건 이미 안다
#    (2026-08-21 실측 26,247건 · 1.5MB · 0.8초). 답해야 하는 건 **부재가 사망을
#    뜻하는가** 다. 부재의 이유가 사망 말고도 있으면(해외 미커버·ETF 미포함)
#    정상 보유 종목에 사망 딱지가 붙는다 — 원인을 모르는 것보다 나쁘다.
#
# 판별력 설계 (같은 함정을 다섯 번 밟았으므로 명시한다)
#   · 통과만 보면 안 된다. **알려진 불량 입력에서 실패해야** 판별자다.
#     → 지상진실이 '사망'인 티커가 리스트에 **있으면** 이 경로는 죽는다.
#   · 지상진실을 내 기억에서 가져오지 않는다. TWTR·ATVI 류 기억은 낡는다.
#     사망 픽스처는 delisted-companies 피드에서 **뽑고**, 생사는 리스트가 아니라
#     profile.isActivelyTrading 으로 **따로** 확정한다. 판별자와 정답지를 분리한다.
#   · 음성 대조군: 존재할 수 없는 심볼이 집합에 잡히면 조회 자체가 틀린 것이다.
#     이건 0콜이고, 없으면 "전부 통과" 가 구현 결함을 숨긴다.
#   · 단일 픽스처는 약하다. 해외·ETF 는 **패널**로 보고, 추가로 집합 전체의
#     점(.) 포함 심볼 수라는 **총계 통계**를 본다. 점 심볼이 0이면 픽스처가
#     무엇이든 해외는 커버되지 않는다.
#
# 사전 확정 채택 기준 (결과를 보기 전에 못 박는다 — 사후 재협상 금지)
#   · 음성 대조 실패                      → 판정 무효. 구현부터 고친다
#   · 지상진실 '사망'이 리스트에 존재      → 전면 폐기. 재실험 금지
#   · 지상진실 '생존'인 미국 보통주가 부재 → 전면 폐기
#   · ETF 패널 전멸                        → ETF 제외하고 부분 채택
#   · 해외 패널 전멸 또는 점 심볼 0        → 해외 제외하고 부분 채택
#   · 그 외                                → 채택. (a)하이브리드 + (b)선제 점검 설계로
_MEM_LIVE_US = "AAPL"                                  # 지상진실 1콜
_MEM_ETF_TRUTH = "SPY"                                 # 지상진실 1콜
_MEM_ETF_PANEL = ["SPY", "QQQ", "IWM", "XLK", "SMH"]   # 멤버십만 — 0콜
_MEM_FOREIGN_TRUTH = "NB2.F"                           # 지상진실 1콜
_MEM_FOREIGN_PANEL = ["005930.KS", "7203.T", "NB2.F"]  # 멤버십만 — 0콜
_MEM_NEG = "ZZZZQQ9"                                   # 음성 대조군 — 0콜

# ⚠️ 2026-08-22 실측 후 기록 — 이 모드의 `mem_neg_case_exercised` 는 **믿으면 안 된다.**
#    그날 결과: 점(.) 포함 심볼이 리스트에 0종 → 해외는 통째로 미커버.
#    그런데 유일한 '사망' 픽스처가 NB2.F 였다. 사망이면서 동시에 해외다.
#    부재의 이유가 '해외라서'로 완전히 설명되므로 **'사망이면 부재' 조항은 한 번도
#    시험되지 않았다.** 그런데도 플래그는 True 를 찍었다 — 판별력 실패 6차.
#    **교락된 픽스처는 픽스처가 아니다.**
#
#    이 모드는 **그 착오의 기록**으로 동작을 바꾸지 않고 그대로 둔다(tierB3 와 동일).
#    교정된 판정은 PROBE_MODE=membership2 에 있다. 새로 판단할 때는 그쪽을 쓴다.


def truth_of(sym):
    """profile 1콜 → ('생존'|'사망'|'소멸'|'판정불가', 회사명, 비고).

    리스트와 **독립된** 정답지다. 이 함수가 리스트를 참조하면 판별이 자기순환이
    된다. 절대 섞지 않는다.
    """
    v, d, det = call("profile?symbol=" + str(sym))
    if v == "EMPTY":
        return "소멸", "", "200 + 빈 배열 — FMP 가 이 심볼을 모른다"
    if v != "OK":
        return "판정불가", "", det
    row = None
    if isinstance(d, list) and d:
        row = d[0]
    elif isinstance(d, dict):
        row = d
    if not isinstance(row, dict):
        return "판정불가", "", "예상 밖 행 타입"
    name = str(pick(row, "companyName", "name") or "").strip()
    act = pick(row, "isActivelyTrading")
    if isinstance(act, str):
        s = act.strip().lower()
        act = True if s in ("true", "1", "yes") else (False if s in ("false", "0", "no") else None)
    if act is True:
        return "생존", name, ""
    if act is False:
        return "사망", name, ""
    return "판정불가", name, "isActivelyTrading 필드 없음"


def symbols_of(data):
    """리스트 응답 → 대문자 심볼 집합. 원소가 dict 이든 문자열이든 견딘다."""
    out = set()
    for row in (data or []):
        if isinstance(row, dict):
            s = pick(row, "symbol", "ticker")
        else:
            s = row
        s = str(s or "").strip().upper()
        if s:
            out.add(s)
    return out


def run_membership():
    """actively-trading-list 를 생사 판별자로 쓸 수 있는지 판정한다. 6콜."""
    print("=" * 78)
    print("멤버십 판별 프로브 — actively-trading-list 가 생사 판별자가 되는가")
    print(f"실행일 {_D_TO} · MODE=membership · 예산 6콜 · 시트/이메일 접촉 없음")
    print("=" * 78)

    # ── 1단계 — 집합 확보 (1콜) ──────────────────────────────────────────
    print("\n[1단계] actively-trading-list — 구조·크기·해외 커버리지")
    t0 = time.time()
    v1, d1, det1 = call("actively-trading-list")
    el = time.time() - t0
    size_mb = len(json.dumps(d1, default=str)) / 1048576.0 if d1 else 0.0
    shape = "dict" if (isinstance(d1, list) and d1 and isinstance(d1[0], dict)) else "scalar"
    show("M1 actively-trading-list", v1,
         det1 + f" · 약 {size_mb:.1f}MB · {el:.1f}초 · 원소={shape}",
         ("필드: " + ", ".join(keys_of(d1))) if shape == "dict" else "")

    if v1 != "OK":
        FIND["mem_verdict"] = "판정불가(리스트 미수신)"
        print("  ⛔ 리스트를 못 받았다 — 나머지 검사는 의미가 없어 중단한다(잔여 콜 미사용)")
        return

    universe = symbols_of(d1)
    dotted = sorted(s for s in universe if "." in s)
    FIND["mem_count"] = len(universe)
    FIND["mem_shape"] = shape
    FIND["mem_dotted"] = len(dotted)
    print(f"       심볼 {len(universe)}종 · 점(.) 포함 {len(dotted)}종"
          + (" · 예: " + ", ".join(dotted[:5]) if dotted else " ← 해외 표기 전무"))

    # 음성 대조군 — 0콜. 이게 잡히면 조회가 틀린 것이고 다른 통과는 전부 무의미하다.
    neg_hit = _MEM_NEG in universe
    FIND["mem_neg_control"] = "FAIL(존재)" if neg_hit else "OK(부재)"
    print(f"       음성 대조 {_MEM_NEG}: " + ("❌ 존재 — 조회 결함" if neg_hit else "✅ 부재"))

    # ── 2단계 — 사망 픽스처 자기 시드 (1콜) ──────────────────────────────
    print("\n[2단계] 사망 픽스처 확보 — 기억이 아니라 피드에서 뽑는다")
    v2, d2, det2 = call("delisted-companies?page=0&limit=20")
    show("M2 delisted-companies?page=0", v2, det2)
    seed_dead = None
    if v2 == "OK" and d2:
        cands = [s for s in symbols_of(d2) if s]
        us_like = sorted(s for s in cands if "." not in s)
        seed_dead = (us_like or sorted(cands) or [None])[0]
        print(f"       후보 {len(cands)}종 · 미국형(점 없음) {len(us_like)}종"
              f" → 시드 = {seed_dead!r}")
    if not seed_dead:
        print("       ⚠️ 시드 확보 실패 — '사망이 리스트에 있는가' 검정은 판정불가로 남는다")

    # ── 3단계 — 지상진실 확립 (최대 4콜) ────────────────────────────────
    print("\n[3단계] 지상진실 — profile.isActivelyTrading (리스트와 독립)")
    fixtures = [("미국 생존", _MEM_LIVE_US), ("ETF", _MEM_ETF_TRUTH),
                ("해외", _MEM_FOREIGN_TRUTH)]
    if seed_dead:
        fixtures.append(("시드 사망", seed_dead))

    rows = []
    for label, sym in fixtures:
        st, name, note = truth_of(sym)
        inlist = sym.upper() in universe
        expect = {"생존": "존재", "사망": "부재", "소멸": "부재"}.get(st)
        if expect is None:
            mark = "—"
        else:
            ok = (inlist and expect == "존재") or ((not inlist) and expect == "부재")
            mark = "✅" if ok else "❌"
        rows.append((label, sym, st, inlist, mark, name, note))
        print(f"  {mark} {label:9} {sym:12} 지상진실={st:5} 리스트={'있음' if inlist else '없음'}"
              + (f" · {name[:28]}" if name else "") + (f" · {note}" if note else ""))
        FIND["mem_truth_" + label.replace(" ", "_")] = (sym, st, inlist)

    # ── 4단계 — 패널 (0콜) ──────────────────────────────────────────────
    print("\n[4단계] 패널 대조 — 단일 픽스처의 우연을 배제한다 (0콜)")
    etf_hit = [s for s in _MEM_ETF_PANEL if s.upper() in universe]
    fx_hit = [s for s in _MEM_FOREIGN_PANEL if s.upper() in universe]
    FIND["mem_etf_hit"] = f"{len(etf_hit)}/{len(_MEM_ETF_PANEL)}"
    FIND["mem_foreign_hit"] = f"{len(fx_hit)}/{len(_MEM_FOREIGN_PANEL)}"
    print(f"  ETF  {len(etf_hit)}/{len(_MEM_ETF_PANEL)} 존재"
          + (" — " + ", ".join(etf_hit) if etf_hit else " ← 전멸"))
    print(f"  해외 {len(fx_hit)}/{len(_MEM_FOREIGN_PANEL)} 존재"
          + (" — " + ", ".join(fx_hit) if fx_hit else " ← 전멸"))

    # ── 5단계 — 사전 확정 기준 적용 ─────────────────────────────────────
    print("\n[5단계] 사전 확정 기준 적용 (결과를 보고 고치지 않는다)")
    fails, partial = [], []
    if neg_hit:
        fails.append("음성 대조 실패 — 구현 결함")
    for label, sym, st, inlist, mark, _n, _o in rows:
        if st in ("사망", "소멸") and inlist:
            fails.append(f"{label}({sym}) 지상진실 {st} 인데 리스트에 존재")
        if st == "생존" and not inlist and label == "미국 생존":
            fails.append(f"미국 보통주 {sym} 이 리스트에 부재")
    if not etf_hit:
        partial.append("ETF 제외")
    if not fx_hit or len(dotted) == 0:
        partial.append("해외 제외")

    if fails:
        verdict = "폐기 — " + " · ".join(fails)
    elif partial:
        verdict = "부분 채택 — " + " · ".join(partial)
    else:
        verdict = "채택 — (a)하이브리드 + (b)선제 점검 설계 진행"
    FIND["mem_verdict"] = verdict
    print("  → " + verdict)

    if not any(st in ("사망", "소멸") for _l, _s, st, _i, _m, _n, _o in rows):
        print("  ⚠️ 지상진실 '사망' 픽스처가 하나도 없었다. 통과처럼 보여도 이 실행은")
        print("     **알려진 불량 입력을 밟지 못했다** — 판별력이 검증되지 않았다.")
        FIND["mem_neg_case_exercised"] = False
    else:
        FIND["mem_neg_case_exercised"] = True


# ══════════════════════════════════════════════════════════════════════════
# 멤버십 판별 모드 2 (PROBE_MODE=membership2 · 최대 12콜)
# ══════════════════════════════════════════════════════════════════════════
# 왜 2가 필요한가 — 1차(2026-08-22)가 무엇에 실패했나
#
#   1차는 '부분 채택 — 해외 제외' 를 냈다. 확인된 건 맞다:
#       리스트 26,252건 · 필드는 name/symbol 뿐 · ETF 5/5 존재
#       점(.) 포함 심볼 **0종** → 해외 표기 통째로 미커버
#   그런데 **핵심 조항이 미시험이었다.** 유일한 '사망' 픽스처 NB2.F 가
#   사망이면서 동시에 해외였다. 점 심볼이 0종인 이상 그게 부재한 건 '해외라서'
#   로 완전히 설명된다 — '사망이라서 부재' 라는 증거는 하나도 없었다.
#   **교락된 픽스처는 픽스처가 아니다.**
#
#   그리고 더 큰 게 딸려 나왔다. 시드 ADWPF 는
#       delisted-companies 에 있고 · profile 은 생존이라 하고 · 리스트에도 있다.
#   **delisted-companies 는 내용 자체가 신뢰 불가다.** 이미 페이지네이션 402 ·
#   심볼필터 무시 · 비US 위주로 감점돼 있었는데 여기에 '있다고 죽은 게 아님'
#   이 추가됐다. "점 없음 = 미국형" 휴리스틱도 틀렸다 — ADWPF 는 5자 + F 끝,
#   외국 보통주의 OTC 표기 관행이다.
#
# 그래서 2차가 하는 일
#   **해외 교락과 분리된** 미국 표기 + 지상진실 사망/소멸 티커를 실제로 확보하고,
#   그것이 리스트에 부재하는지 본다. 확보하지 못하면 **'판정불가'** 로 끝난다 —
#   1차처럼 '통과처럼 보이는 미시험'을 만들지 않는다. 이게 이 모드의 존재 이유다.
#
# 후보 풀
#   delisted-companies 가 신뢰를 잃었으므로 symbol-change 의 oldSymbol(개명 전
#   티커 — 소멸했을 것)을 합친다. 어느 피드에서 왔든 **생사는 profile 로 다시
#   확정한다.** 피드는 후보를 주는 역할만 한다.
#
# 순위 (외국 OTC 표기를 뒤로 미룬다)
#   1) 점 없음 · 4자 이하                ← 가장 깨끗한 미국 표기
#   2) 점 없음 · 5자 · F/Y 로 안 끝남
#   3) 점 없음 · 5자 · F/Y 로 끝남       ← ADWPF 부류. 교락 위험
#   4) 점 포함                            ← 해외 확정. 교락되므로 사실상 무의미
#
# 사전 확정 기준 (결과를 보고 고치지 않는다)
#   · 확보한 불량입력이 하나라도 리스트에 **존재** → 전면 폐기 · 재실험 금지
#   · 강한 불량입력(사망 · 점 없음 · F/Y 아님) ≥1 확보 + 전부 부재
#                                          → 판별력 확인. 채택(해외 제외)
#   · 약한 것(소멸, 또는 F/Y 표기)만 확보  → 조건부 채택. 한계를 명시해 기록
#   · 하나도 확보 못 함                    → **판정불가.** 채택 아님
_MEM2_PROFILE_MAX = 9        # 1(리스트) + 2(피드) + 9 = 12콜 상한
_MEM2_NEED = 2               # 불량입력 2개 확보하면 즉시 중단(콜 절약)


def us_rank(sym):
    """미국 표기 순위. 낮을수록 교락이 적다."""
    s = str(sym or "").upper()
    if "." in s:
        return 4
    if len(s) <= 4:
        return 1
    if len(s) == 5 and s.endswith(("F", "Y")):
        return 3
    return 2


def run_membership2():
    """해외 교락과 분리된 불량입력으로 리스트의 판별력을 실제로 시험한다."""
    print("=" * 78)
    print("멤버십 판별 프로브 2 — 교락 없는 불량입력으로 재시험")
    print(f"실행일 {_D_TO} · MODE=membership2 · 예산 최대 12콜 · 시트/이메일 접촉 없음")
    print("=" * 78)

    # ── 1단계 — 집합 (1콜) ──────────────────────────────────────────────
    print("\n[1단계] actively-trading-list")
    v1, d1, det1 = call("actively-trading-list")
    show("N1 actively-trading-list", v1, det1)
    if v1 != "OK":
        FIND["mem2_verdict"] = "판정불가(리스트 미수신)"
        print("  ⛔ 리스트 미수신 — 잔여 콜 미사용으로 중단")
        return
    universe = symbols_of(d1)
    dotted = [s for s in universe if "." in s]
    neg_hit = _MEM_NEG in universe
    FIND["mem2_count"] = len(universe)
    FIND["mem2_dotted"] = len(dotted)
    FIND["mem2_neg_control"] = "FAIL(존재)" if neg_hit else "OK(부재)"
    print(f"       심볼 {len(universe)}종 · 점 포함 {len(dotted)}종 · "
          f"음성대조 {_MEM_NEG}: " + ("❌ 존재" if neg_hit else "✅ 부재"))

    # ── 2단계 — 후보 풀 (2콜) ───────────────────────────────────────────
    print("\n[2단계] 후보 풀 — 두 피드를 합치되 생사는 여기서 판단하지 않는다")
    pool = {}
    v2, d2, det2 = call("delisted-companies?page=0&limit=20")
    show("N2 delisted-companies?page=0", v2, det2)
    if v2 == "OK":
        for s in symbols_of(d2):
            pool.setdefault(s, "delisted")
    v3, d3, det3 = call("symbol-change?limit=20")
    show("N3 symbol-change?limit=20", v3, det3)
    if v3 == "OK":
        for row in (d3 or []):
            s = str(pick(row, "oldSymbol", "old_symbol") or "").strip().upper()
            if s:
                pool.setdefault(s, "renamed-old")

    cands = sorted(pool.keys(), key=lambda s: (us_rank(s), s))
    FIND["mem2_pool"] = len(cands)
    by_rank = {}
    for s in cands:
        by_rank[us_rank(s)] = by_rank.get(us_rank(s), 0) + 1
    print(f"       후보 {len(cands)}종 · 순위분포 {by_rank}")
    if not cands:
        FIND["mem2_verdict"] = "판정불가(후보 풀 없음)"
        print("  ⛔ 후보를 못 만들었다 — 잔여 콜 미사용으로 중단")
        return

    # ── 3단계 — 지상진실 확정 (최대 9콜) ────────────────────────────────
    print("\n[3단계] 지상진실 — profile 로 다시 확정. 피드 주장은 근거가 아니다")
    bad, alive_in_feed, used = [], [], 0
    for sym in cands:
        if used >= _MEM2_PROFILE_MAX or len(bad) >= _MEM2_NEED:
            break
        st, name, note = truth_of(sym)
        used += 1
        inlist = sym in universe
        src = pool.get(sym, "")
        if st in ("사망", "소멸"):
            bad.append((sym, st, inlist, src))
            mark = "✅" if not inlist else "❌"
            print(f"  {mark} {sym:10} r{us_rank(sym)} {src:11} 지상진실={st:4} "
                  f"리스트={'있음' if inlist else '없음'}"
                  + (f" · {name[:24]}" if name else ""))
        else:
            if st == "생존":
                alive_in_feed.append((sym, src))
            print(f"  ·  {sym:10} r{us_rank(sym)} {src:11} 지상진실={st:4}"
                  + (f" · {note}" if note else "")
                  + ("   ← 피드와 모순" if st == "생존" and src == "delisted" else ""))

    FIND["mem2_profile_calls"] = used
    FIND["mem2_bad_found"] = len(bad)
    FIND["mem2_feed_contradiction"] = len([1 for _s, src in alive_in_feed
                                           if src == "delisted"])
    print(f"       profile {used}콜 · 불량입력 {len(bad)}개 확보 · "
          f"delisted 인데 생존 {FIND['mem2_feed_contradiction']}건")

    # ── 4단계 — 사전 확정 기준 ──────────────────────────────────────────
    print("\n[4단계] 사전 확정 기준 적용 (결과를 보고 고치지 않는다)")
    present = [b for b in bad if b[2]]
    strong = [b for b in bad if b[1] == "사망" and us_rank(b[0]) <= 2]
    weak = [b for b in bad if b not in strong]

    if neg_hit:
        verdict = "폐기 — 음성 대조 실패(구현 결함)"
    elif present:
        verdict = "폐기 — " + ", ".join(f"{s}({st}) 가 리스트에 존재"
                                       for s, st, _i, _r in present)
    elif strong:
        verdict = "채택(해외 제외) — 강한 불량입력 " + str(len(strong)) + "개가 부재로 확인"
    elif weak:
        verdict = "조건부 채택(해외 제외) — 약한 불량입력만 확보(" + \
                  ", ".join(f"{s}:{st}" for s, st, _i, _r in weak) + ")"
    else:
        verdict = "판정불가 — 교락 없는 불량입력을 확보하지 못함(채택 아님)"

    FIND["mem2_strong"] = len(strong)
    FIND["mem2_weak"] = len(weak)
    FIND["mem2_verdict"] = verdict
    FIND["mem2_neg_case_exercised"] = bool(strong or weak)
    print("  → " + verdict)
    if not strong:
        print("  ⚠️ 강한 불량입력(사망 · 점 없음 · F/Y 아님)이 없다. "
              "'사망이면 부재' 조항은 아직 완전히 시험되지 않았다.")


def main():
    # 정규화를 여기서 한다. import 시점에 하면 검증 계층 밖이라 테스트가 닿지
    # 않고, 모듈을 먼저 import 한 뒤 값을 넣는 경로에서도 안 먹는다.
    mode = "" if _MODE == "default" else _MODE
    if mode not in _VALID_MODES:
        print(f"❌ PROBE_MODE={_MODE!r} — 모르는 모드다. 콜을 태우지 않고 중단한다.")
        print("   가능한 값: default(=빈값) · membership · membership2")
        return 2

    if not _KEY:
        print("❌ FMP_API_KEY 없음 — 중단")
        return 1

    if mode in ("membership", "membership2"):
        if mode == "membership2":
            run_membership2()
            key = "MEMBERSHIP2_JSON "
        else:
            run_membership()
            key = "MEMBERSHIP_JSON "
        print("\n" + "=" * 78)
        print(f"총 {_CALLS} 콜 소비")
        print("=" * 78)
        for k, v in FIND.items():
            print(f"  {k:26} = {v!r}")
        print("")
        print(key + json.dumps(FIND, ensure_ascii=False, default=str))
        return 0

    print("=" * 78)
    print("A-2b 프로브 — 미수신 원인 판정 경로 실측")
    print(f"실행일 {_D_TO} · MODE=default · HEAVY={'ON' if _HEAVY else 'OFF'}")
    print("=" * 78)

    # ══════════════════════════════════════════════════════════════════
    # 0단계 — 시드 확보. 이후 모든 검사가 여기서 나온 실제 티커를 쓴다.
    # ══════════════════════════════════════════════════════════════════
    print("\n[0단계] 시드 확보 — 실제 개명·상폐 사례를 피드에서 뽑는다")

    v1, d1, det1 = call("symbol-change?limit=20")
    show("P1  symbol-change?limit=20", v1, det1,
         "필드: " + ", ".join(keys_of(d1)) if d1 else "")
    seed_old = seed_new = None
    if v1 == "OK" and d1:
        sc_dates = dates_in(d1, "date")
        raw1 = [str(pick(r, "date") or "")[:10] for r in d1 if pick(r, "date")]
        if sc_dates:
            order = ("최신먼저" if raw1 == sorted(raw1, reverse=True)
                     else ("오래된먼저" if raw1 == sorted(raw1) else "불규칙"))
            print(f"       날짜 범위: {sc_dates[0]} ~ {sc_dates[-1]}  (정렬: {order})")
        for row in d1:
            o, n = pick(row, "oldSymbol"), pick(row, "newSymbol")
            if o and n:
                seed_old, seed_new = str(o).strip().upper(), str(n).strip().upper()
                break
        print(f"       🌱 시드(개명): {seed_old} → {seed_new}")
    FIND["symbol_change_live"] = (v1 == "OK")

    v2, d2, det2 = call("delisted-companies?page=0&limit=20")
    show("P2  delisted-companies?page=0&limit=20", v2, det2,
         "필드: " + ", ".join(keys_of(d2)) if d2 else "")
    seed_del = None
    p0_dates = []
    if v2 == "OK" and d2:
        p0_dates = dates_in(d2, "delistedDate", "date")
        if p0_dates:
            print(f"       날짜 범위: {p0_dates[0]} ~ {p0_dates[-1]}")
        s = pick(d2[0], "symbol")
        seed_del = str(s).strip().upper() if s else None
        print(f"       🌱 시드(상폐): {seed_del}")
    FIND["delisted_live"] = (v2 == "OK")

    # ══════════════════════════════════════════════════════════════════
    # 1단계 — 심볼 필터가 진짜 먹는가. **무시됨을 반드시 구분한다.**
    # ══════════════════════════════════════════════════════════════════
    print("\n[1단계] 심볼 필터 — '지원'과 '조용히 무시됨'을 구분한다")

    def filter_check(tag, path, seed, unfiltered, sym_fields):
        """결과 심볼이 **한 종류인가**로 판정한다.

        ⚠️ 판별자를 두 번 틀렸다. 기록해 둔다:
          1차 — 건수 비교(무필터와 같으면 IGNORED). 무필터는 limit=20, 필터는
                limit 없이 기본 100건이라 건수가 달랐고 분기를 그냥 빠져나갔다.
          2차 — 시드 포함 여부. **시드를 그 피드에서 뽑았으니 포함은 구조적으로
                보장된다.** 아무것도 증명하지 못하는 지표였다.

        판별력이 있는 건 하나뿐이다: 심볼 필터가 동작하면 결과 심볼은
        **한 종류**여야 한다. 100건에 99종이 섞였으면 그건 전역 피드다.
        """
        if not seed:
            print(f"  ⏭️  {tag} — 시드 없음, 건너뜀")
            return "NOSEED"
        v, d, det = call(path)
        if v != "OK":
            show(tag, v, det)
            return v
        syms = {str(pick(r, *sym_fields) or "").strip().upper() for r in d}
        syms.discard("")
        seed_u = str(seed).strip().upper()

        if len(syms) > 1:
            _others = sorted(syms - {seed_u})
            show(tag, "IGNORED",
                 f"{len(d)}건 · 심볼 {len(syms)}종 — **필터가 먹으면 1종이어야 한다. "
                 f"전역 피드가 왔다.** "
                 f"시드 {seed_u} {'포함' if seed_u in syms else '미포함'}"
                 f"(피드에서 뽑았으니 포함 자체는 무의미). 타 심볼 샘플: {_others[:5]}")
            return "IGNORED"
        if syms == {seed_u}:
            show(tag, "OK", f"{len(d)}건 — 전부 {seed_u}. 필터 정상 동작")
            return "FILTER_OK"
        if not syms:
            show(tag, "ODD", f"{len(d)}건 — 심볼 필드를 읽지 못했다")
            return "ODD"
        show(tag, "ODD",
             f"{len(d)}건 — 1종이지만 시드가 아니다: {sorted(syms)}")
        return "ODD"

    FIND["sc_symbol_filter"] = filter_check(
        f"P3  symbol-change?symbol={seed_old}",
        f"symbol-change?symbol={seed_old}", seed_old, d1, ("oldSymbol", "symbol"))

    v4, d4, det4 = call(f"symbol-change?from={_D_FROM_90}&to={_D_TO}")
    if v4 == "OK" and d4:
        r4 = dates_in(d4, "date")
        inside = [x for x in r4 if _D_FROM_90 <= x <= _D_TO]
        ok = len(r4) > 0 and len(inside) == len(r4)
        show("P4  symbol-change?from&to (90일)", "OK" if ok else "IGNORED",
             f"{len(d4)}건 · 범위 {r4[0] if r4 else '?'}~{r4[-1] if r4 else '?'} "
             + ("— 요청 구간 내" if ok else "— **구간 밖 데이터 포함. 무시된 듯**"))
        FIND["sc_date_filter"] = "OK" if ok else "IGNORED"
    else:
        show("P4  symbol-change?from&to (90일)", v4, det4)
        FIND["sc_date_filter"] = v4

    FIND["dl_symbol_filter"] = filter_check(
        f"P5  delisted-companies?symbol={seed_del}",
        f"delisted-companies?symbol={seed_del}", seed_del, d2, ("symbol",))

    # ══════════════════════════════════════════════════════════════════
    # 2단계 — 페이지당 며칠씩 뒤로 가는가 → 필요 깊이 산출
    # ══════════════════════════════════════════════════════════════════
    print("\n[2단계] 페이지 깊이 — 1년치를 덮으려면 몇 콜이 드는가")

    v6, d6, det6 = call("delisted-companies?page=5&limit=20")
    if v6 == "OK" and d6:
        p5_dates = dates_in(d6, "delistedDate", "date")
        show("P6  delisted-companies?page=5", "OK",
             f"{len(d6)}건 · 범위 {p5_dates[0] if p5_dates else '?'}"
             f"~{p5_dates[-1] if p5_dates else '?'}")
        if p0_dates and p5_dates:
            try:
                newest = datetime.strptime(p0_dates[-1], "%Y-%m-%d").date()
                older = datetime.strptime(p5_dates[0], "%Y-%m-%d").date()
                span = (newest - older).days
                per_page = span / 5.0 if span > 0 else 0
                print(f"       0→5 페이지 후퇴폭: {span}일  (페이지당 약 {per_page:.1f}일)")
                if per_page > 0:
                    need = int(365.0 / per_page) + 1
                    print(f"       ⇒ **1년 커버에 약 {need}페이지 = {need}콜**")
                    FIND["dl_pages_for_1y"] = need
                else:
                    print("       ⚠️ 후퇴폭 0 이하 — 정렬이 날짜순이 아닐 수 있다")
                    FIND["dl_pages_for_1y"] = None
            except Exception as e:
                print(f"       ⚠️ 날짜 파싱 실패: {type(e).__name__}")
                FIND["dl_pages_for_1y"] = None
    else:
        show("P6  delisted-companies?page=5", v6, det6)
        FIND["dl_pages_for_1y"] = None

    # ══════════════════════════════════════════════════════════════════
    # 3단계 — 경로 A 생사. profile 이 상폐/개명 티커에 뭘 주는가.
    # ══════════════════════════════════════════════════════════════════
    print("\n[3단계] 경로 A 생사 — profile.isActivelyTrading")

    def profile_check(tag, sym):
        if not sym:
            print(f"  ⏭️  {tag} — 시드 없음, 건너뜀")
            return None
        v, d, det = call(f"profile?symbol={sym}")
        if v != "OK":
            show(tag, v, det)
            return None
        row = d[0] if isinstance(d, list) and d else d
        act = pick(row, "isActivelyTrading")
        nm = pick(row, "companyName", "name")
        has = act is not None
        show(tag, "OK" if has else "FIELDS",
             f"isActivelyTrading={act!r} · companyName={str(nm)[:40]!r}"
             + ("" if has else "  ← **필드 없음. 경로 A 불가**"))
        return act

    FIND["profile_delisted"] = profile_check(f"P7  profile?symbol={seed_del} (상폐)", seed_del)
    FIND["profile_old"] = profile_check(f"P8  profile?symbol={seed_old} (구티커)", seed_old)

    # ══════════════════════════════════════════════════════════════════
    # 4단계 — A-2a 트리거 재현. 이게 안 비면 그 분기는 아예 불필요하다.
    # ══════════════════════════════════════════════════════════════════
    print("\n[4단계] 트리거 재현 — 실제로 이력이 비는가 (run_watchlist_alerts:191 과 동일 경로)")

    def hist_check(tag, sym):
        if not sym:
            print(f"  ⏭️  {tag} — 시드 없음, 건너뜀")
            return None
        # ⚠️ `limit=250` 이었다(2026-09-04 수정). 두 가지가 틀려 있었다:
        #   ① FMP `historical-price-eod` 는 `limit=` 을 조용히 무시한다.
        #      250 을 요청하든 말든 ~1,254봉이 온다.
        #   ② 위 주석이 "run_watchlist_alerts:191 과 동일 경로"라고 주장하는데
        #      **그쪽은 이미 from/to 창으로 옮겼다.** 경로 동일성을 주장하는
        #      진단이 다른 경로를 타고 있었다. 판정이 "비었나/왔나" 뿐이라
        #      결과는 안 바뀌었지만, 재현한다고 말하는 걸 재현하지 않았다.
        # ⚠️ 여기서 `fmp_extras` 를 import 하지 않는다. 이 파일이 A1 기준선에
        #    남아 있는 사유가 **프로젝트 모듈 무의존**이다 — 사본이 낡아도
        #    진단 결과가 흔들리지 않아야 한다. 그래서 창을 인라인으로 만든다.
        #    ⚠️ 대신 이 날짜 계산은 fmp_extras 의 정책과 **다를 수 있다**.
        #       여기 판정은 "0건인가 아닌가" 뿐이라 정밀도가 필요 없다.
        #       봉수를 세는 검사를 여기 추가하려면 그때는 fx 를 써야 한다.
        _to = datetime.now(timezone.utc).date()
        _from = _to - timedelta(days=520)     # ≈ 250봉 + 넉넉한 여유
        v, d, det = call(f"historical-price-eod/full?symbol={sym}"
                         f"&from={_from.isoformat()}&to={_to.isoformat()}")
        n = len(d) if isinstance(d, list) else (1 if d else 0)
        empty = (v == "EMPTY") or (v == "OK" and n == 0)
        show(tag, v, det + ("  ← 비었다 = A-2a 발동" if empty
                            else f"  ← **데이터가 온다({n}건). 이 분기는 미수신이 안 난다**"))
        return "EMPTY" if empty else ("DATA" if v == "OK" else v)

    FIND["hist_old"] = hist_check(f"P9  구티커 {seed_old} 이력", seed_old)
    FIND["hist_delisted"] = hist_check(f"P10 상폐 {seed_del} 이력", seed_del)

    v11, d11, det11 = call(f"search-symbol?query={seed_old}") if seed_old else ("NOSEED", None, "")
    if seed_old:
        hits = [str(pick(r, "symbol") or "") for r in (d11 or [])][:5]
        show(f"P11 search-symbol?query={seed_old}", v11, det11,
             ("후보: " + ", ".join(hits)) if hits else "")
        FIND["search_symbol"] = v11

    # ══════════════════════════════════════════════════════════════════
    # 선택 — 멤버십 집합 크기 실측
    # ══════════════════════════════════════════════════════════════════
    if _HEAVY:
        print("\n[선택] actively-trading-list — 실제 크기 실측")
        t0 = time.time()
        v12, d12, det12 = call("actively-trading-list")
        el = time.time() - t0
        size_mb = len(json.dumps(d12)) / 1048576.0 if d12 else 0
        show("P12 actively-trading-list", v12,
             det12 + f" · 약 {size_mb:.1f}MB · {el:.1f}초")
        FIND["atl_count"] = len(d12) if isinstance(d12, list) else None
    else:
        print("\n[선택] actively-trading-list — 건너뜀 (PROBE_HEAVY=1 로 켜기)")

    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 78)
    print(f"총 {_CALLS} 콜 소비")
    print("=" * 78)
    print("판정 요약 — 이 값들로 A-2b 설계를 확정한다:")
    for k, v in FIND.items():
        print(f"  {k:22} = {v!r}")
    print("")
    print("NODATA_CAUSE_JSON " + json.dumps(FIND, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
