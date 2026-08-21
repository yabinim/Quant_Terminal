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


def main():
    if not _KEY:
        print("❌ FMP_API_KEY 없음 — 중단")
        return 1

    print("=" * 78)
    print("A-2b 프로브 — 미수신 원인 판정 경로 실측")
    print(f"실행일 {_D_TO} · HEAVY={'ON' if _HEAVY else 'OFF'}")
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
        """필터 결과가 요청 심볼을 담고 있는지로 판정한다.

        ⚠️ 초판은 **건수 비교**를 판별자로 썼다(무필터와 건수가 같으면 IGNORED).
           그게 틀렸다. 무필터 호출은 limit=20 이고 필터 호출은 limit 이 없어
           기본 100건이 온다 — 건수가 달라 IGNORED 분기를 그냥 빠져나갔고,
           심볼이 하나도 안 맞는 전역 피드가 'PARTIAL(판단 보류)' 로 찍혔다.
           제가 잡으려던 함정에 판별자가 걸렸다.

           올바른 판별자는 **요청한 심볼이 결과에 들어 있는가**다. 건수는 무관하다.
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

        if seed_u not in syms:
            show(tag, "IGNORED",
                 f"{len(d)}건 — **요청 심볼 {seed_u} 가 결과에 없다.** "
                 f"파라미터가 무시되고 전역 피드가 왔다. 샘플: {sorted(syms)[:5]}")
            return "IGNORED"
        if syms <= {seed_u}:
            show(tag, "OK", f"{len(d)}건 — 전부 {seed_u}. 필터 정상 동작")
            return "FILTER_OK"
        show(tag, "PARTIAL",
             f"{len(d)}건 — {seed_u} 포함이나 다른 심볼 {len(syms) - 1}종 혼재. "
             f"샘플: {sorted(syms - {seed_u})[:5]}")
        return "PARTIAL"

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
        v, d, det = call(f"historical-price-eod/full?symbol={sym}&limit=250")
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
