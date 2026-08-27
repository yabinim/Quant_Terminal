#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FMP historical-price-eod — 날짜창 파라미터 검증 + GNOM 113.98 소재 추적.

목적
────
두 가지를 한 번에 묻는다. 같은 엔드포인트라 콜을 합칠 수 있다.

  [A] Trade_History 의 GNOM 2026-06-29 매수가 113.98 이 FMP 시리즈 안에
      존재하는 값인가? (실제 체결가는 Ryan 확인 결과 56.72)
  [B] `limit` 이 무시되는 것은 확정됐다(limit=500 → 1254봉). 그렇다면
      `from`/`to` 는 먹히는가? 먹히면 호출부 30여 곳의 페이로드를 줄일 수 있고,
      안 먹히면 "줄일 방법 없음"으로 확정하고 타임아웃을 그 전제로 맞춘다.

이 스크립트는 아무것도 수정하지 않는다. 읽기 전용이다.
  · 시트 접근 없음 · 파일 쓰기 없음 · FMP 읽기 7콜

════════════════════════════════════════════════════════════════════════
⚠️ 사전 확정 판정 기준 — 결과를 보기 전에 못 박는다
════════════════════════════════════════════════════════════════════════

[A] GNOM 113.98
  · 시리즈 전체(close/open/high/low)에서 113.98 의 ±0.5% 안을 찾는다.
  · 미발견            → FMP 데이터 기원설 **기각**. 수동 입력 오류로 확정.
                        코드 수정 불필요, 시트 수정만.
  · 발견 & 위치 상위 5 → "가장 오래된 봉" 가설 살아남음. 경로 추적 계속.
  · 발견 & 그 외       → 위치·날짜를 기록하고 별도 조사.

  ⚠️ app.py 매수 폼 두 곳(22030 `value=0.0`, 25077 `value=현재가`)을 확인한
     결과 과거 봉이 매수가 입력란에 들어갈 경로는 **없다**. 즉 이 검사의
     사전 기대값은 "미발견"이다. 발견되면 그게 새 정보다.

[B] from/to — **날짜로만 판정한다. 봉수·바이트로 판정하지 않는다.**
  근거: 봉수는 파라미터가 무시돼도 다른 이유로 달라질 수 있다. 반면
  "from 이전의 봉이 하나라도 왔다"는 파라미터가 무시됐을 때만 성립한다.
  (지난 세션 교훈: limit=500 요청에 1254봉 수신 → 한 방향으로 결정적)

  · from 먹힘 = 응답의 **최소 날짜 ≥ from**
  · to   먹힘 = 응답의 **최대 날짜 ≤ to**
  · 둘 다 지정한 두 창(B4·B5) **모두** 통과해야 "먹힘" 확정.
    하나만 통과하면 **불확정** — 한 창의 우연을 배제할 수 없다.
  · 빈 응답은 "먹힘"이 아니라 **불확정**이다. 범위를 좁혔더니 0건 온 것과
    엔드포인트가 죽은 것을 구분할 수 없기 때문.

[C] 페이로드 영향 — 콜 0회. 저장소 소스를 직접 읽어 호출부와 limit 값을
    열거한다. 기억이 아니라 소스가 근거다.

════════════════════════════════════════════════════════════════════════

설계 제약
─────────
· **원시 requests.get 을 쓰지 않는다.** 전부 fmp_http(SSOT) 경유.
  diag_fmp_ssot.py 의 A1 래칫은 기준선에 없는 파일에서 원시 호출이
  발견되면 실패한다. 새 파일은 0곳이어야 한다.
· **다른 모듈을 import 하지 않는다.** run_signal_backtest 를 빌려 쓰면
  gspread·pandas 의존이 딸려오고, 평면 디렉터리에서만 되는 경로 결함이
  숨는다(§6-2). URL 은 여기서 만들고, 호출부 조사는 **소스 텍스트**로 한다.

실행
────
    python automation/diag_fmp_window.py              # 실제 프로브 (7콜)
    python automation/diag_fmp_window.py --selftest   # 네트워크 없음, 판정기 역검증
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fmp_http as fh  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════
# 상수 — 사전 확정값
# ══════════════════════════════════════════════════════════════════════════
GNOM_SYMBOL = "GNOM"
GNOM_RECORDED = 113.98     # Trade_History 에 기록된 값
GNOM_ACTUAL = 56.72        # Ryan 확인 실제 체결가
GNOM_TRADE_DATE = "2026-06-29"
MATCH_TOL = 0.005          # ±0.5%
HEAD_N = 5                 # "가장 오래된 봉" 판정 폭

PROBE_SYMBOL = "SPY"
ENDPOINT = "historical-price-eod/full"

# B4·B5 두 창. 서로 다른 시기를 고른다 — 한 창의 우연을 배제한다.
WIN_A = ("2026-06-01", "2026-06-30")
WIN_B = ("2026-08-01", "2026-08-25")
FROM_ONLY = "2026-06-01"
TO_ONLY = "2022-12-31"

TIMEOUT = 20.0             # limit 무시 확정 이후 항상 1254봉이 온다


# ══════════════════════════════════════════════════════════════════════════
# 순수 함수 — selftest 대상
# ══════════════════════════════════════════════════════════════════════════
def extract_rows(data):
    """FMP 응답 → 레코드 리스트. run_signal_backtest._fmp_price_history 와 동일 규칙."""
    if isinstance(data, dict):
        data = data.get("historical", data)
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def row_dates(rows):
    """레코드 리스트 → (유효날짜 리스트, 불량건수). 날짜는 'YYYY-MM-DD' 문자열."""
    out, bad = [], 0
    for r in rows:
        d = str(r.get("date") or "").strip()[:10]
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            out.append(d)
        else:
            bad += 1
    return sorted(out), bad


def verdict_range(rows, d_from, d_to):
    """날짜창 파라미터가 먹혔는지 판정.

    Returns: (verdict, detail)
      verdict ∈ {"honored", "ignored", "inconclusive"}

    ⚠️ 판정은 **날짜 경계**로만 한다. 봉수·바이트는 참고 지표일 뿐이다.
       빈 응답은 honored 가 아니라 inconclusive — 범위 축소의 결과인지
       엔드포인트 장애인지 구분할 수 없다.
    """
    dates, bad = row_dates(rows)
    if not dates:
        return "inconclusive", "유효 날짜 0건 (빈 응답 — 범위 축소인지 장애인지 구분 불가)"
    lo, hi = dates[0], dates[-1]
    bits = []
    violated = False
    if d_from:
        if lo < d_from:
            violated = True
            bits.append(f"최소날짜 {lo} < from {d_from} → from 무시됨")
        else:
            bits.append(f"최소날짜 {lo} ≥ from {d_from} ✓")
    if d_to:
        if hi > d_to:
            violated = True
            bits.append(f"최대날짜 {hi} > to {d_to} → to 무시됨")
        else:
            bits.append(f"최대날짜 {hi} ≤ to {d_to} ✓")
    if bad:
        bits.append(f"날짜 파싱 실패 {bad}건")
    if not d_from and not d_to:
        return "inconclusive", "기준 날짜가 지정되지 않음"
    return ("ignored" if violated else "honored"), " · ".join(bits)


def find_value(rows, target, tol=MATCH_TOL):
    """시리즈 안에서 target 의 ±tol 안에 드는 값을 모두 찾는다.

    close 만 보지 않는다 — open/high/low/adjClose 를 잘못 집는 것도
    가능한 메커니즘이므로 전부 훑는다.

    Returns: [(index, date, field, value), ...]  (index 는 과거→현재 정렬 기준)
    """
    dated = []
    for r in rows:
        d = str(r.get("date") or "").strip()[:10]
        dated.append((d, r))
    dated.sort(key=lambda t: t[0])

    hits = []
    lo, hi = target * (1.0 - tol), target * (1.0 + tol)
    for i, (d, r) in enumerate(dated):
        for field in ("close", "adjClose", "open", "high", "low"):
            v = r.get(field)
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if lo <= fv <= hi:
                hits.append((i, d, field, fv))
    return hits


def bar_on(rows, date_s):
    """특정 날짜의 레코드. 없으면 None."""
    for r in rows:
        if str(r.get("date") or "").strip()[:10] == date_s:
            return r
    return None


# ══════════════════════════════════════════════════════════════════════════
# 네트워크
# ══════════════════════════════════════════════════════════════════════════
def fetch(path):
    """(rows, status, kind, nbytes). fmp_http 경유 — 원시 requests 를 쓰지 않는다."""
    url = fh.fmp_url(path)
    r, status, kind = fh.fmp_get_ex(url, timeout=TIMEOUT)
    if r is None or kind != "ok":
        return [], status, kind, 0
    nbytes = len(r.content or b"")
    try:
        return extract_rows(r.json()), status, "ok", nbytes
    except Exception:
        return [], status, "bad_json", nbytes


def _q(symbol, d_from="", d_to="", limit=None):
    p = f"{ENDPOINT}?symbol={symbol}"
    if limit is not None:
        p += f"&limit={limit}"
    if d_from:
        p += f"&from={d_from}"
    if d_to:
        p += f"&to={d_to}"
    return p


# ══════════════════════════════════════════════════════════════════════════
# [A] GNOM 113.98 소재
# ══════════════════════════════════════════════════════════════════════════
def part_a():
    print("\n" + "=" * 74)
    print("[A] GNOM 113.98 소재 추적  (기록값 113.98 · 실제 체결가 56.72)")
    print("=" * 74)

    rows, status, kind, nbytes = fetch(_q(GNOM_SYMBOL))
    if not rows:
        print(f"  ❌ 응답 없음 (status={status} kind={kind}) — 판정 불가")
        return "inconclusive"

    dates, bad = row_dates(rows)
    print(f"  응답: {len(rows)}봉 · {dates[0]} ~ {dates[-1]} · "
          f"{nbytes:,}바이트" + (f" · 날짜불량 {bad}건" if bad else ""))

    # 거래일 실제 봉 — FMP 데이터 자체가 맞는지 독립 확인
    b = bar_on(rows, GNOM_TRADE_DATE)
    if b is None:
        print(f"  · {GNOM_TRADE_DATE} 봉 없음 (휴장일이거나 범위 밖)")
    else:
        try:
            c = float(b.get("close"))
            gap = (c - GNOM_ACTUAL) / GNOM_ACTUAL * 100.0
            mark = "✓ 일치" if abs(gap) <= 2.0 else "⚠️ 불일치"
            print(f"  · {GNOM_TRADE_DATE} 종가 ${c:.2f}  vs 실제 체결가 "
                  f"${GNOM_ACTUAL:.2f} ({gap:+.1f}%) {mark}")
        except (TypeError, ValueError):
            print(f"  · {GNOM_TRADE_DATE} 봉의 close 를 읽을 수 없음")

    print(f"\n  가장 오래된 {HEAD_N}봉:")
    dated = sorted(((str(r.get('date') or '')[:10], r) for r in rows),
                   key=lambda t: t[0])
    for i, (d, r) in enumerate(dated[:HEAD_N]):
        try:
            print(f"    [{i}] {d}  close ${float(r.get('close')):>8.2f}")
        except (TypeError, ValueError):
            print(f"    [{i}] {d}  close 파싱 실패")

    hits = find_value(rows, GNOM_RECORDED)
    print(f"\n  113.98 ±{MATCH_TOL * 100:.1f}% 탐색 (close/adjClose/open/high/low): "
          f"{len(hits)}건")

    if not hits:
        print("\n  ✅ 판정: **미발견** → FMP 데이터 기원설 기각.")
        print("     113.98 은 GNOM 시리즈 어디에도 없다. 수동 입력 오류로 확정.")
        print("     조치: 시트 F열 수정만. 코드 변경 불필요.")
        return "not_found"

    for i, d, field, v in hits[:12]:
        print(f"    idx {i:>5}  {d}  {field:>8} ${v:.2f}")
    if len(hits) > 12:
        print(f"    … 외 {len(hits) - 12}건")

    min_idx = min(h[0] for h in hits)
    if min_idx < HEAD_N:
        print(f"\n  ⚠️ 판정: **최고참 봉 근처에서 발견** (idx {min_idx}).")
        print("     '가장 오래된 봉을 집었다' 가설이 살아남았다.")
        print("     → app.py 원시 호출 58곳에서 iloc[0]/head()/[0] 패턴을 찾을 것.")
        return "head_hit"

    print(f"\n  판정: **발견되었으나 최고참 봉이 아님** (최소 idx {min_idx}).")
    print("     단순 우연일 수 있다 — 5년 시계열에 특정 값이 한 번 나오는 것은 흔하다.")
    print("     날짜가 매수일과 무관하면 우연으로 본다.")
    return "mid_hit"


# ══════════════════════════════════════════════════════════════════════════
# [B] from / to 파라미터
# ══════════════════════════════════════════════════════════════════════════
def part_b():
    print("\n" + "=" * 74)
    print(f"[B] from/to 파라미터 검증  (심볼 {PROBE_SYMBOL})")
    print("=" * 74)

    cases = [
        ("B1 기준선 (파라미터 없음)", "", "", None),
        ("B2 from 단독", FROM_ONLY, "", None),
        ("B3 to 단독", "", TO_ONLY, None),
        (f"B4 창A {WIN_A[0]}~{WIN_A[1]}", WIN_A[0], WIN_A[1], None),
        (f"B5 창B {WIN_B[0]}~{WIN_B[1]}", WIN_B[0], WIN_B[1], None),
    ]

    results = {}
    base_bytes, base_bars = 0, 0

    print(f"\n  {'케이스':<32}{'봉수':>7}{'바이트':>11}  {'최소~최대':<24}판정")
    print("  " + "-" * 88)
    for label, d_from, d_to, lim in cases:
        rows, status, kind, nbytes = fetch(_q(PROBE_SYMBOL, d_from, d_to, lim))
        if not rows:
            print(f"  {label:<32}{'—':>7}{'—':>11}  응답 없음 "
                  f"(status={status} kind={kind})")
            results[label[:2]] = ("inconclusive", f"응답 없음 ({kind})")
            continue
        dates, _bad = row_dates(rows)
        v, detail = verdict_range(rows, d_from, d_to)
        if label.startswith("B1"):
            base_bytes, base_bars = nbytes, len(rows)
            v, detail = "baseline", f"{dates[0]} ~ {dates[-1]}"
        span = f"{dates[0]}~{dates[-1]}"
        print(f"  {label:<32}{len(rows):>7}{nbytes:>11,}  {span:<24}{v}")
        results[label[:2]] = (v, detail)

    print("\n  상세:")
    for k in sorted(results):
        v, detail = results[k]
        print(f"    {k}: {v} — {detail}")

    # ── 사전 확정 기준 적용 ────────────────────────────────────────────
    b4 = results.get("B4", ("inconclusive", ""))[0]
    b5 = results.get("B5", ("inconclusive", ""))[0]
    b2 = results.get("B2", ("inconclusive", ""))[0]
    b3 = results.get("B3", ("inconclusive", ""))[0]

    print("\n  " + "─" * 70)
    if b4 == "honored" and b5 == "honored":
        final = "honored"
        print("  ✅ 판정: from/to **먹힘** — 두 창 모두 경계를 지켰다.")
        if base_bytes and base_bars:
            print(f"     기준선 {base_bars}봉 {base_bytes:,}바이트 "
                  f"(봉당 약 {base_bytes / base_bars:.0f}바이트)")
            print("     → [C] 의 호출부들에 날짜창을 적용하면 페이로드가 줄어든다.")
    elif "ignored" in (b4, b5):
        final = "ignored"
        print("  ❌ 판정: from/to **무시됨** — 범위 밖 날짜가 왔다.")
        print("     limit·delisted-companies 심볼필터·symbol-change 심볼필터에 이은")
        print("     **네 번째 '조용히 무시되는 FMP 파라미터'**.")
        print("     → 페이로드 축소 방법 없음으로 확정. 타임아웃을 1254봉 전제로 유지.")
    else:
        final = "inconclusive"
        print("  ⚠️ 판정: **불확정** — 두 창이 엇갈리거나 응답이 비었다.")
        print("     한 창의 결과만으로 결론내지 않는다. 재실행하거나 창을 바꿔 볼 것.")

    print(f"     (단독 파라미터: from={b2} · to={b3})")
    return final, base_bytes, base_bars


# ══════════════════════════════════════════════════════════════════════════
# [C] 호출부 조사 — 콜 0회, 소스 텍스트 기반
# ══════════════════════════════════════════════════════════════════════════
_CALLSITE_RE = re.compile(
    r"historical-price-eod/(?:full|light)[^\"'\n]*?limit=(\{?[A-Za-z_0-9]+\}?)")


def scan_callsites(root):
    """저장소에서 historical-price-eod 호출부와 limit 값을 열거한다.

    기억이 아니라 소스가 근거다. 새 호출부가 생기면 자동으로 표에 뜬다.
    """
    found = []
    for d in (root, os.path.join(root, "automation")):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(d, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    src = f.read()
            except Exception:
                continue
            for i, line in enumerate(src.splitlines(), 1):
                m = _CALLSITE_RE.search(line)
                if m:
                    found.append((fn, i, m.group(1)))
    return found


def part_c(root, bytes_per_bar):
    print("\n" + "=" * 74)
    print("[C] 호출부 · 페이로드 영향  (FMP 콜 0회 — 소스 스캔)")
    print("=" * 74)

    sites = scan_callsites(root)
    if not sites:
        print("  호출부를 찾지 못했습니다 (경로 확인 필요).")
        return

    print(f"\n  {'파일':<34}{'행':>6}{'요청 limit':>12}{'실제 수신':>10}{'배율':>8}")
    print("  " + "-" * 72)
    ACTUAL = 1254  # limit 무시 확정값
    waste = 0
    for fn, ln, lim in sites:
        try:
            n = int(lim)
            ratio = f"{ACTUAL / n:.0f}x"
            waste += max(0, ACTUAL - n)
        except ValueError:
            n, ratio = lim, "—"      # 변수명(HISTORY_LIMIT 등)
        print(f"  {fn:<34}{ln:>6}{str(lim):>12}{ACTUAL:>10}{ratio:>8}")

    print(f"\n  호출부 {len(sites)}곳 · 상수 limit 기준 낭비 봉수 합계 약 {waste:,}봉")
    if bytes_per_bar:
        print(f"  봉당 {bytes_per_bar:.0f}바이트 → 회당 절감 가능 약 "
              f"{waste * bytes_per_bar / 1024 / 1024:.1f}MB")
        print("  ⚠️ [B] 가 'honored' 일 때만 실현 가능한 수치다.")
    else:
        print("  (봉당 바이트를 재지 못해 용량 환산 생략)")


# ══════════════════════════════════════════════════════════════════════════
# selftest — 네트워크 없음. 판정기가 **틀린 입력에서 실패하는지** 확인한다.
# ══════════════════════════════════════════════════════════════════════════
def _rows(dates, close=100.0):
    return [{"date": d, "close": close, "open": close,
             "high": close, "low": close, "adjClose": close} for d in dates]


def _raw_get_calls(path=None, src=None):
    """원시 requests.get(FMP) 호출 지점 목록 — diag_fmp_ssot.raw_fmp_gets 와 동일 규칙.

    Attribute(attr='get', value=Name(id='requests')) 인 Call 중, 첫 인자에
    financialmodelingprep 또는 apikey 가 있는 것만 센다. 규칙을 A1 과 맞춰
    두어야 "여기선 통과, A1 에선 실패"가 생기지 않는다.
    """
    import ast
    if src is None:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    hits = []
    for c in ast.walk(tree):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "get"
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "requests"):
            continue
        try:
            arg = ast.unparse(c.args[0]) if c.args else ""
        except Exception:
            arg = ""
        if "financialmodelingprep" in arg or "apikey" in arg:
            hits.append((c.lineno, arg[:60]))
    return hits


def _uses(path, attr):
    """소스에 `<something>.attr(...)` 호출이 실제로 있는가 (AST — 주석/산문 제외)."""
    import ast
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
               and c.func.attr == attr for c in ast.walk(tree))


def selftest():
    fails = []
    ran = []

    def check(name, got, want):
        ran.append(name)
        ok = (got == want)
        print(f"  {'✅' if ok else '❌'} {name}: {got}" +
              ("" if ok else f"  (기대 {want})"))
        if not ok:
            fails.append(name)

    print("\n" + "=" * 74)
    print("selftest — 판정기 역검증 (네트워크·시트 접근 없음)")
    print("=" * 74)

    print("\n verdict_range — 정상 경로")
    check("S1 범위 안",
          verdict_range(_rows(["2026-06-02", "2026-06-15", "2026-06-30"]),
                        "2026-06-01", "2026-06-30")[0], "honored")
    check("S2 경계 정확히",
          verdict_range(_rows(["2026-06-01", "2026-06-30"]),
                        "2026-06-01", "2026-06-30")[0], "honored")

    print("\n verdict_range — 역검증 (여기가 판별력이다)")
    # 실물 실패 모양: 창을 좁혔는데 5년치가 그대로 온다
    check("S3 from 이전 봉 존재 → ignored",
          verdict_range(_rows(["2021-08-27", "2026-06-15"]),
                        "2026-06-01", "2026-06-30")[0], "ignored")
    check("S4 to 이후 봉 존재 → ignored",
          verdict_range(_rows(["2026-06-15", "2026-08-25"]),
                        "2026-06-01", "2026-06-30")[0], "ignored")
    check("S5 from 단독 위반 → ignored",
          verdict_range(_rows(["2020-01-02"]), "2026-06-01", "")[0], "ignored")
    check("S6 to 단독 위반 → ignored",
          verdict_range(_rows(["2026-08-25"]), "", "2022-12-31")[0], "ignored")

    print("\n verdict_range — 침묵 금지 (빈 응답을 통과로 읽지 않는다)")
    check("S7 빈 응답 → inconclusive",
          verdict_range([], "2026-06-01", "2026-06-30")[0], "inconclusive")
    check("S8 날짜 전부 불량 → inconclusive",
          verdict_range([{"date": "bad", "close": 1}], "2026-06-01", "")[0],
          "inconclusive")
    check("S9 기준 날짜 미지정 → inconclusive",
          verdict_range(_rows(["2026-06-15"]), "", "")[0], "inconclusive")

    print("\n find_value")
    hits = find_value(_rows(["2021-06-30"], close=113.98)
                      + _rows(["2026-06-29"], close=56.72), 113.98)
    check("S10 정확값 발견", len(hits) > 0, True)
    check("S11 발견 위치가 최고참", min(h[0] for h in hits) if hits else -1, 0)
    check("S12 허용오차 밖은 미발견",
          len(find_value(_rows(["2021-06-30"], close=120.0), 113.98)), 0)
    check("S13 허용오차 안은 발견",
          len(find_value(_rows(["2021-06-30"], close=114.3), 113.98)) > 0, True)

    print("\n extract_rows / row_dates")
    check("S14 dict 래핑 해제",
          len(extract_rows({"historical": _rows(["2026-01-02"])})), 1)
    check("S15 리스트 그대로", len(extract_rows(_rows(["2026-01-02"]))), 1)
    check("S16 비리스트 → 빈 결과", extract_rows("nope"), [])
    check("S17 날짜 정렬",
          row_dates(_rows(["2026-06-30", "2021-08-27"]))[0][0], "2021-08-27")

    print("\n 구조 — 원시 FMP 호출이 이 파일에 없는가 (diag_fmp_ssot A1 래칫)")
    # ⚠️ 텍스트 매칭을 쓰면 안 된다. 이 파일의 독스트링에 "원시 requests.get 을
    #    쓰지 않는다"라는 **산문**이 있어서 초안이 자기 자신을 오탐했다.
    #    판정은 diag_fmp_ssot.raw_fmp_gets 와 **동일한 AST 규칙**으로 한다 —
    #    규칙이 다르면 여기선 통과하고 A1 에서 터진다.
    raw_calls = _raw_get_calls(os.path.abspath(__file__))
    check("S18 원시 requests.get 호출 0곳 (AST)", len(raw_calls), 0)
    check("S19 fmp_http 경유", _uses(os.path.abspath(__file__), "fmp_get_ex"), True)
    # 역검증: 같은 탐지기가 **진짜 원시 호출을 잡는지** 확인한다.
    # 이게 없으면 탐지기가 항상 0 을 돌려줘도 S18 은 초록불이다.
    check("S20 탐지기 역검증 — 원시 호출 샘플을 잡는다",
          len(_raw_get_calls(src=(
              "import requests\n"
              "r = requests.get(f'{BASE}/quote?symbol=X&apikey={k}')\n"))), 1)
    check("S21 탐지기 오탐 없음 — 무관한 .get 은 안 잡는다",
          len(_raw_get_calls(src=(
              "d = {}\n"
              "v = d.get('apikey')\n"
              "s.get('https://financialmodelingprep.com/x')\n"))), 0)

    print("\n" + "=" * 74)
    if fails:
        print(f"❌ {len(ran)}건 중 {len(fails)}건 실패: {', '.join(fails)}")
        return 1
    print(f"✅ 전 항목 통과 ({len(ran)}/{len(ran)})")
    return 0


# ══════════════════════════════════════════════════════════════════════════
def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if "--selftest" in sys.argv:
        return selftest()

    if not fh.fmp_key():
        print("[ERR] FMP_API_KEY 없음")
        return 1

    print("=" * 74)
    print("FMP 날짜창 프로브 — 읽기 전용 · 시트 기록 없음 · 예상 7콜")
    print("=" * 74)

    a_verdict = part_a()
    b_verdict, base_bytes, base_bars = part_b()
    part_c(root, (base_bytes / base_bars) if base_bars else 0.0)

    print("\n" + "=" * 74)
    print("요약")
    print("=" * 74)
    print(f"  [A] GNOM 113.98 : {a_verdict}")
    print(f"  [B] from/to     : {b_verdict}")
    print(f"  {fh.fmp_stats_line()}")

    # 종료코드는 '프로브가 제대로 돌았는가'만 나타낸다.
    # 파라미터가 무시된다는 결과 자체는 실패가 아니라 **답**이다.
    if b_verdict == "inconclusive" or a_verdict == "inconclusive":
        print("\n  ⚠️ 불확정 항목이 있습니다 — 위 상세를 보고 재실행 여부를 판단하세요.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
