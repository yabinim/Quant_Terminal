#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FMP historical-price-eod — 날짜창 파라미터 검증 + 전환 안전성 감사.

v2 (2026-08-26): [A] 종결 후 [D] 완전성 · [E] 하류 소요 봉수 감사 추가.

배경
────
`limit` 은 무시된다(limit=500 → 1254봉). v1 프로브로 **`from`/`to` 는 먹힌다**가
확정됐다 — 지금까지 확인된 FMP 파라미터 중 유일하게 작동하는 것이다.
[A] GNOM 113.98 은 시리즈에 없었다(not_found) → 수동 입력 오류로 종결.

그런데 "먹힌다"만으로는 전환할 수 없다. v1 이 확인한 것은 **경계**뿐이다:
from 이전 봉이 안 온다는 것. **창 안의 봉이 전부 오는지(완전성)는 확인하지
않았다.** B4·B5 가 20봉짜리 좁은 창이라 중간 절삭이 있어도 드러나지 않는다.

⚠️ 전환의 진짜 위험 — limit 은 한 번도 강제된 적이 없다
─────────────────────────────────────────────────────
현재 호출부의 `limit` 값들은 **전부 무의미했다.** 뭘 적든 1254봉이 왔다.
즉 그 숫자는 "필요한 최소 봉수"가 아니라 **"누가 언젠가 적어둔 값"** 이고
한 번도 검증된 적이 없다.

`from` 으로 전환하는 순간 그 숫자가 **처음으로 실제 상한이 된다.**
`limit=130` 인데 하류에서 200일선을 계산하는 곳이 하나라도 있으면, 오늘까지
멀쩡하던 것이 전환 당일 조용히 NaN 이 된다. [E] 가 그걸 미리 찾는다.

단위도 다르다. `limit` 은 **거래일**, `from` 은 **달력일**이다. 비율은 추정하지
않고 기준선 응답에서 **실측**한다.

이 스크립트는 아무것도 수정하지 않는다.
  · 시트 접근 없음 · 파일 쓰기 없음 · FMP 읽기 7콜(기본) / 8콜(--with-a)

════════════════════════════════════════════════════════════════════════
⚠️ 사전 확정 판정 기준 — 결과를 보기 전에 못 박는다
════════════════════════════════════════════════════════════════════════

[B]·[D] 공통 — **기준선 시리즈를 정답지로 쓴다**

  v1 은 경계(최소·최대 날짜)만 봤다. v2 는 B1(무파라미터) 응답을 먼저 받아
  **그 시리즈에서 창 안의 봉 수를 직접 세어 기대값**으로 삼는다. 추가 콜 0회로
  판정력이 올라간다 — 기대값이 외부 가정이 아니라 같은 엔드포인트의 실측이다.

  · 경계 위반(from 이전 / to 이후 봉 존재)        → **ignored**
  · 경계 OK · 실제 == 기대 (±1봉)                 → **honored_complete**
  · 경계 OK · 실제 <  기대                        → **honored_truncated**
      경계는 지키지만 봉이 빠진다. 넓은 창에 쓸 수 없다
  · 경계 OK · 실제 >  기대                        → **anomaly** (조사 필요)
  · 빈 응답                                       → **inconclusive**
      범위 축소 결과인지 엔드포인트 장애인지 구분할 수 없다

  ±1봉 허용 이유: 기준선과 창 요청 사이 수 초 간격에 당일 봉이 생길 수 있다.
  2봉 이상 차이는 허용하지 않는다.

  **최종 "전환 가능" 판정 = B2~B5 · D1 · D2 가 전부 honored_complete.**
  하나라도 truncated 면 전환 불가. 하나라도 inconclusive 면 재실행.

[D] 완전성 — 좁은 창만으로는 못 본다

  D1 중간폭 창: from = 기준선의 중앙 날짜 (약 절반 ≈ 600봉대)
  D2 전폭 창  : from = 기준선의 최초 날짜 (전량 ≈ 1254봉)

  D2 가 complete 면 "날짜창으로 전량 확보 가능" 이 확정된다 — 이게 되어야
  `run_signal_backtest` 처럼 최대 깊이가 필요한 곳도 전환할 수 있다.

[E] 하류 소요 봉수 감사 — 콜 0회, AST

  각 호출부 **파일**을 훑어 긴 윈도우 연산의 최대 N 을 찾는다:
  `rolling(N)` · `ewm(span=N)` · `.tail(N)` · `.iloc[-N:]` ·
  `*_BARS/LOOKBACK/WINDOW/PERIOD/MIN_PRIOR* = N` 상수.

  판정: limit < 관측최대N → **위험**(전환 시 데이터 부족)

  ⚠️ **이 감사는 파일 단위 과대근사다.** 같은 파일의 다른 함수가 쓰는 윈도우도
     함께 잡힌다. 즉 **오탐은 나지만 누락은 나지 않는다** — 전환 전 감사로는
     안전한 방향이다. 여기서 '안전'으로 나온 것만 자동으로 믿어도 되고,
     '위험'은 사람이 한 번 봐야 한다.
  ⚠️ df 가 다른 모듈로 넘어가 거기서 200일선을 계산하면 이 감사는 못 본다.
     파일 경계를 넘는 소비는 범위 밖이다.

════════════════════════════════════════════════════════════════════════

설계 제약
─────────
· **원시 requests.get 을 쓰지 않는다.** 전부 fmp_http(SSOT) 경유.
  diag_fmp_ssot A1 래칫은 기준선에 없는 파일의 원시 호출을 실패 처리한다.
· **다른 저장소 모듈을 import 하지 않는다.** 의존성 딸림·경로 결함 은폐 방지
  (§6-2). URL 은 여기서 만들고 호출부 조사는 소스 AST 로 한다.
· 의존성은 `requests` 하나(그마저 fmp_http 경유).

실행
────
    python automation/diag_fmp_window.py              # 기본 7콜 (B·D·C·E)
    python automation/diag_fmp_window.py --with-a     # + GNOM 재확인 (8콜)
    python automation/diag_fmp_window.py --selftest   # 네트워크 없음
"""
from __future__ import annotations

import ast
import datetime as dt
import math
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
GNOM_RECORDED = 113.98     # Trade_History 에 기록됐던 값 (2026-08-26 56.72 로 정정)
GNOM_ACTUAL = 56.72        # Ryan 확인 실제 체결가
GNOM_TRADE_DATE = "2026-06-29"
MATCH_TOL = 0.005          # ±0.5%
HEAD_N = 5

PROBE_SYMBOL = "SPY"
ENDPOINT = "historical-price-eod/full"

WIN_A = ("2026-06-01", "2026-06-30")
WIN_B = ("2026-08-01", "2026-08-25")
FROM_ONLY = "2026-06-01"
TO_ONLY = "2022-12-31"

COUNT_TOL = 1              # 기준선 대비 허용 봉수 차 (당일 봉 타이밍)
SAFETY_MARGIN = 1.10       # 전환 권고 오프셋 안전계수
SAFETY_PAD_DAYS = 10       # 연휴 구간 대비 고정 여유

TIMEOUT = 20.0

# [E] 가 훑는 긴 윈도우 신호
_WINDOW_CALLS = {"rolling", "ewm"}
_TAIL_CALLS = {"tail", "head"}
_CONST_RE = re.compile(r"(BARS|LOOKBACK|WINDOW|PERIOD|MIN_PRIOR|_MA\b|HISTORY)",
                       re.IGNORECASE)
_CONST_MAX = 5000          # 이보다 큰 상수는 봉수가 아니라 다른 것(포트·바이트 등)


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
    """레코드 리스트 → (정렬된 유효날짜 리스트, 불량건수)."""
    out, bad = [], 0
    for r in rows:
        d = str(r.get("date") or "").strip()[:10]
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            out.append(d)
        else:
            bad += 1
    return sorted(out), bad


def expected_in_window(baseline_dates, d_from, d_to):
    """기준선 시리즈에서 [d_from, d_to] 안의 봉 수 — 창 요청의 기대값.

    기대값을 외부 가정이 아니라 **같은 엔드포인트의 실측**에서 뽑는다.
    빈 경계는 무제한으로 본다.
    """
    lo = d_from or "0000-00-00"
    hi = d_to or "9999-99-99"
    return sum(1 for d in baseline_dates if lo <= d <= hi)


def verdict_range(rows, d_from, d_to):
    """경계 판정만. Returns: (verdict, detail). v1 호환 유지."""
    dates, bad = row_dates(rows)
    if not dates:
        return "inconclusive", "유효 날짜 0건 (빈 응답 — 범위 축소인지 장애인지 구분 불가)"
    lo, hi = dates[0], dates[-1]
    bits, violated = [], False
    if d_from:
        if lo < d_from:
            violated = True
            bits.append(f"최소 {lo} < from {d_from} → from 무시")
        else:
            bits.append(f"최소 {lo} ≥ from ✓")
    if d_to:
        if hi > d_to:
            violated = True
            bits.append(f"최대 {hi} > to {d_to} → to 무시")
        else:
            bits.append(f"최대 {hi} ≤ to ✓")
    if bad:
        bits.append(f"날짜 파싱 실패 {bad}건")
    if not d_from and not d_to:
        return "inconclusive", "기준 날짜가 지정되지 않음"
    return ("ignored" if violated else "honored"), " · ".join(bits)


def verdict_window(rows, d_from, d_to, expected):
    """경계 + **완전성** 판정.

    경계만 보면 "from 이후만 왔다"는 알 수 있어도 "창 안 봉이 다 왔는가"는
    모른다. 좁은 창에서는 중간 절삭이 드러나지 않기 때문에 반드시 기대값과
    개수를 맞춰야 한다.
    """
    base_v, base_d = verdict_range(rows, d_from, d_to)
    if base_v != "honored":
        return base_v, base_d
    dates, _bad = row_dates(rows)
    n = len(dates)
    if expected is None:
        return "honored", base_d + " · 기대값 없음(완전성 미판정)"
    diff = n - expected
    tail = f"실제 {n}봉 / 기대 {expected}봉 ({diff:+d})"
    if abs(diff) <= COUNT_TOL:
        return "honored_complete", base_d + " · " + tail
    if diff < 0:
        return "honored_truncated", base_d + " · " + tail + " ← 봉 누락"
    return "anomaly", base_d + " · " + tail + " ← 기대 초과"


def bars_per_calendar_day(dates):
    """기준선에서 실측한 거래일/달력일 비율. 추정하지 않는다."""
    if len(dates) < 2:
        return 0.0
    try:
        a = dt.date.fromisoformat(dates[0])
        b = dt.date.fromisoformat(dates[-1])
    except ValueError:
        return 0.0
    span = (b - a).days
    return (len(dates) / span) if span > 0 else 0.0


def calendar_days_for(bars, ratio):
    """N 거래일을 확보하는 데 필요한 달력일 (안전계수 + 고정 여유 포함)."""
    if not ratio or bars <= 0:
        return 0
    return int(math.ceil(bars / ratio * SAFETY_MARGIN)) + SAFETY_PAD_DAYS


def find_value(rows, target, tol=MATCH_TOL):
    """시리즈에서 target 의 ±tol 안에 드는 값 전부. close 외 필드도 훑는다."""
    dated = sorted(((str(r.get("date") or "")[:10], r) for r in rows),
                   key=lambda t: t[0])
    hits = []
    lo, hi = target * (1.0 - tol), target * (1.0 + tol)
    for i, (d, r) in enumerate(dated):
        for field in ("close", "adjClose", "open", "high", "low"):
            try:
                fv = float(r.get(field))
            except (TypeError, ValueError):
                continue
            if lo <= fv <= hi:
                hits.append((i, d, field, fv))
    return hits


def bar_on(rows, date_s):
    for r in rows:
        if str(r.get("date") or "").strip()[:10] == date_s:
            return r
    return None


# ══════════════════════════════════════════════════════════════════════════
# [E] 하류 윈도우 스캐너 — AST
# ══════════════════════════════════════════════════════════════════════════
def _int_of(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    return None


def scan_windows(src):
    """소스에서 '긴 윈도우를 요구하는' 신호를 모두 찾는다.

    Returns: [(lineno, kind, N), ...]

    구조적 신호만 본다. "200 이라는 숫자가 근처에 있다" 같은 텍스트 휴리스틱은
    쓰지 않는다 — 오탐이 나오는 가드는 곧 무시당한다(§6-3).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        # rolling(N) / rolling(window=N) / ewm(span=N)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr in _WINDOW_CALLS:
                v = None
                for a in n.args:
                    v = v or _int_of(a)
                for kw in n.keywords:
                    if kw.arg in ("window", "span", "periods", "min_periods"):
                        v = v or _int_of(kw.value)
                if v and 0 < v <= _CONST_MAX:
                    out.append((n.lineno, n.func.attr, v))
            elif n.func.attr in _TAIL_CALLS and n.args:
                v = _int_of(n.args[0])
                if v and 0 < v <= _CONST_MAX:
                    out.append((n.lineno, n.func.attr, v))
        # .iloc[-N:]  /  [-N:]
        elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice):
            lo = n.slice.lower
            if isinstance(lo, ast.UnaryOp) and isinstance(lo.op, ast.USub):
                v = _int_of(lo.operand)
                if v and 0 < v <= _CONST_MAX:
                    out.append((n.lineno, "slice", v))
        # 이름이 봉수를 뜻하는 정수 상수
        elif isinstance(n, ast.Assign):
            v = _int_of(n.value)
            if v and 0 < v <= _CONST_MAX:
                for t in n.targets:
                    if isinstance(t, ast.Name) and _CONST_RE.search(t.id):
                        out.append((n.lineno, f"const {t.id}", v))
    return out


_CALLSITE_RE = re.compile(
    r"historical-price-eod/(?:full|light)[^\"'\n]*?limit=(\{?[A-Za-z_0-9]+\}?)")


def scan_callsites(root):
    """저장소에서 historical-price-eod 호출부와 limit 값을 열거한다.

    기억이 아니라 소스가 근거다. 새 호출부가 생기면 자동으로 표에 뜬다.
    Returns: [(파일명, 절대경로, 행, limit문자열), ...]
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
                    found.append((fn, p, i, m.group(1)))
    return found


# ══════════════════════════════════════════════════════════════════════════
# 네트워크
# ══════════════════════════════════════════════════════════════════════════
def fetch(path):
    """(rows, status, kind, nbytes). fmp_http 경유 — 원시 requests 를 쓰지 않는다."""
    r, status, kind = fh.fmp_get_ex(fh.fmp_url(path), timeout=TIMEOUT)
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
# [A] GNOM 113.98 소재 — v1 에서 not_found 로 종결. --with-a 로만 재확인.
# ══════════════════════════════════════════════════════════════════════════
def part_a():
    print("\n" + "=" * 74)
    print("[A] GNOM 113.98 소재 재확인  (v1 결과: not_found — 종결됨)")
    print("=" * 74)

    rows, status, kind, nbytes = fetch(_q(GNOM_SYMBOL))
    if not rows:
        print(f"  ❌ 응답 없음 (status={status} kind={kind}) — 판정 불가")
        return "inconclusive"

    dates, bad = row_dates(rows)
    print(f"  응답: {len(rows)}봉 · {dates[0]} ~ {dates[-1]} · {nbytes:,}바이트")

    b = bar_on(rows, GNOM_TRADE_DATE)
    if b is not None:
        try:
            c = float(b.get("close"))
            gap = (c - GNOM_ACTUAL) / GNOM_ACTUAL * 100.0
            print(f"  · {GNOM_TRADE_DATE} 종가 ${c:.2f} vs 실제 체결가 "
                  f"${GNOM_ACTUAL:.2f} ({gap:+.1f}%) "
                  f"{'✓' if abs(gap) <= 2.0 else '⚠️'}")
        except (TypeError, ValueError):
            print(f"  · {GNOM_TRADE_DATE} close 파싱 실패")

    hits = find_value(rows, GNOM_RECORDED)
    print(f"  113.98 ±{MATCH_TOL * 100:.1f}% 탐색: {len(hits)}건")
    if not hits:
        print("  ✅ 미발견 — FMP 데이터 기원설 기각 유지. 수동 입력 오류 확정.")
        return "not_found"
    for i, d, field, v in hits[:8]:
        print(f"    idx {i:>5}  {d}  {field:>8} ${v:.2f}")
    min_idx = min(h[0] for h in hits)
    return "head_hit" if min_idx < HEAD_N else "mid_hit"


# ══════════════════════════════════════════════════════════════════════════
# [B] from / to — 경계 + 완전성 (기준선을 정답지로)
# ══════════════════════════════════════════════════════════════════════════
def part_b():
    print("\n" + "=" * 74)
    print(f"[B] from/to 경계·완전성  (심볼 {PROBE_SYMBOL})")
    print("=" * 74)

    # B1 기준선을 **먼저** 받는다 — 이후 모든 창의 기대값이 여기서 나온다.
    base_rows, status, kind, base_bytes = fetch(_q(PROBE_SYMBOL))
    base_dates, _bad = row_dates(base_rows)
    if not base_dates:
        print(f"  ❌ 기준선 응답 없음 (status={status} kind={kind}) — 전체 판정 불가")
        return "inconclusive", {}, [], 0.0

    ratio = bars_per_calendar_day(base_dates)
    print(f"\n  B1 기준선(무파라미터): {len(base_dates)}봉 · "
          f"{base_dates[0]} ~ {base_dates[-1]} · {base_bytes:,}바이트")
    if ratio:
        print(f"     봉당 {base_bytes / len(base_dates):.0f}바이트 · "
              f"거래일/달력일 실측 비율 {ratio:.4f} "
              f"(1봉당 달력 {1 / ratio:.2f}일)")

    cases = [
        ("B2 from 단독", FROM_ONLY, ""),
        ("B3 to 단독", "", TO_ONLY),
        (f"B4 창A {WIN_A[0]}~{WIN_A[1]}", WIN_A[0], WIN_A[1]),
        (f"B5 창B {WIN_B[0]}~{WIN_B[1]}", WIN_B[0], WIN_B[1]),
    ]

    results = {}
    print(f"\n  {'케이스':<30}{'실제':>7}{'기대':>7}{'바이트':>10}  판정")
    print("  " + "-" * 74)
    for label, d_from, d_to in cases:
        rows, st, kd, nb = fetch(_q(PROBE_SYMBOL, d_from, d_to))
        exp = expected_in_window(base_dates, d_from, d_to)
        v, detail = verdict_window(rows, d_from, d_to, exp)
        n = len(row_dates(rows)[0])
        print(f"  {label:<30}{n:>7}{exp:>7}{nb:>10,}  {v}")
        results[label[:2]] = (v, detail)

    print("\n  상세:")
    for k in sorted(results):
        print(f"    {k}: {results[k][1]}")

    return "ok", results, base_dates, (base_bytes / len(base_dates))


# ══════════════════════════════════════════════════════════════════════════
# [D] 완전성 — 넓은 창에서도 전량 오는가 (2콜)
# ══════════════════════════════════════════════════════════════════════════
def part_d(base_dates):
    print("\n" + "=" * 74)
    print("[D] 완전성 — 넓은 창 (좁은 창만으로는 중간 절삭이 안 보인다)")
    print("=" * 74)

    if not base_dates:
        print("  기준선이 없어 판정 불가")
        return {}

    mid = base_dates[len(base_dates) // 2]
    cases = [
        (f"D1 중간폭 from={mid}", mid, ""),
        (f"D2 전폭   from={base_dates[0]}", base_dates[0], ""),
    ]

    results = {}
    print(f"\n  {'케이스':<34}{'실제':>7}{'기대':>7}  판정")
    print("  " + "-" * 70)
    for label, d_from, d_to in cases:
        rows, st, kd, nb = fetch(_q(PROBE_SYMBOL, d_from, d_to))
        exp = expected_in_window(base_dates, d_from, d_to)
        v, detail = verdict_window(rows, d_from, d_to, exp)
        n = len(row_dates(rows)[0])
        print(f"  {label:<34}{n:>7}{exp:>7}  {v}")
        results[label[:2]] = (v, detail)

    print("\n  상세:")
    for k in sorted(results):
        print(f"    {k}: {results[k][1]}")
    return results


# ══════════════════════════════════════════════════════════════════════════
# [C]+[E] 호출부 · 하류 소요 봉수 감사 (콜 0회)
# ══════════════════════════════════════════════════════════════════════════
def part_ce(root, bytes_per_bar, ratio):
    print("\n" + "=" * 74)
    print("[C/E] 호출부 · 하류 소요 봉수 감사  (FMP 콜 0회 — 소스 AST)")
    print("=" * 74)

    sites = scan_callsites(root)
    if not sites:
        print("  호출부를 찾지 못했습니다 (경로 확인 필요).")
        return

    ACTUAL = 1254
    win_cache = {}
    risky, unknown, safe = [], [], []

    print(f"\n  {'파일':<32}{'행':>6}{'limit':>9}{'하류최대':>9}{'판정':>8}"
          f"{'권고 from':>11}")
    print("  " + "-" * 78)
    waste = 0
    for fn, path, ln, lim in sites:
        if path not in win_cache:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    win_cache[path] = scan_windows(f.read())
            except Exception:
                win_cache[path] = []
        wins = win_cache[path]
        wmax = max((w[2] for w in wins), default=0)

        try:
            n = int(lim)
            waste += max(0, ACTUAL - n)
        except ValueError:
            n = None

        if n is None:
            verdict, rec = "불명", "—"
            unknown.append((fn, ln, lim, wmax))
        elif wmax == 0:
            verdict, rec = "신호없음", f"{calendar_days_for(n, ratio)}일"
            unknown.append((fn, ln, lim, wmax))
        elif n < wmax:
            verdict = "⚠️위험"
            rec = f"{calendar_days_for(wmax, ratio)}일"
            risky.append((fn, ln, n, wmax))
        else:
            verdict = "안전"
            rec = f"{calendar_days_for(n, ratio)}일"
            safe.append((fn, ln, n, wmax))

        print(f"  {fn:<32}{ln:>6}{str(lim):>9}{(wmax or '—'):>9}{verdict:>8}{rec:>11}")

    print(f"\n  호출부 {len(sites)}곳 — 안전 {len(safe)} · 위험 {len(risky)} · "
          f"판정보류 {len(unknown)}")
    if bytes_per_bar and waste:
        print(f"  상수 limit 기준 낭비 {waste:,}봉 · 봉당 {bytes_per_bar:.0f}바이트 "
              f"→ 회당 약 {waste * bytes_per_bar / 1024 / 1024:.1f}MB")

    if risky:
        print("\n  ⚠️ 위험 — 지금 limit 으로 전환하면 데이터가 부족해진다:")
        for fn, ln, n, wmax in risky:
            print(f"     {fn}:{ln}  limit={n} < 하류 최대 윈도우 {wmax}")
        print("     → 전환 전에 limit 이 아니라 **하류 요구치**로 from 을 잡을 것.")

    if unknown:
        print(f"\n  판정보류 {len(unknown)}곳 — 변수 limit 이거나 윈도우 신호 없음.")
        print("     사람이 직접 확인해야 한다. 자동 전환 대상에서 제외할 것.")

    print("\n  ⚠️ 이 감사는 **파일 단위 과대근사**다. 같은 파일의 다른 함수가 쓰는")
    print("     윈도우도 잡힌다 — 오탐은 나지만 누락은 나지 않는다.")
    print("     df 가 다른 모듈로 넘어가 거기서 계산되면 이 감사는 못 본다.")


# ══════════════════════════════════════════════════════════════════════════
# selftest
# ══════════════════════════════════════════════════════════════════════════
def _rows(dates, close=100.0):
    return [{"date": d, "close": close, "open": close,
             "high": close, "low": close, "adjClose": close} for d in dates]


def _raw_get_calls(path=None, src=None):
    """원시 requests.get(FMP) 호출 지점 — diag_fmp_ssot.raw_fmp_gets 와 동일 규칙.

    규칙을 A1 과 맞춰 두어야 "여기선 통과, A1 에선 실패"가 생기지 않는다.
    """
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
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
               and c.func.attr == attr for c in ast.walk(tree))


def selftest():
    fails, ran = [], []

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

    print("\n verdict_range — 경계 정상")
    check("S1 범위 안", verdict_range(_rows(["2026-06-02", "2026-06-30"]),
                                      "2026-06-01", "2026-06-30")[0], "honored")
    check("S2 경계 정확히", verdict_range(_rows(["2026-06-01", "2026-06-30"]),
                                          "2026-06-01", "2026-06-30")[0], "honored")

    print("\n verdict_range — 역검증")
    check("S3 from 이전 봉 → ignored",
          verdict_range(_rows(["2021-08-27", "2026-06-15"]),
                        "2026-06-01", "2026-06-30")[0], "ignored")
    check("S4 to 이후 봉 → ignored",
          verdict_range(_rows(["2026-06-15", "2026-08-25"]),
                        "2026-06-01", "2026-06-30")[0], "ignored")
    check("S5 from 단독 위반", verdict_range(_rows(["2020-01-02"]),
                                             "2026-06-01", "")[0], "ignored")
    check("S6 to 단독 위반", verdict_range(_rows(["2026-08-25"]),
                                           "", "2022-12-31")[0], "ignored")

    print("\n verdict_range — 침묵 금지")
    check("S7 빈 응답 → inconclusive",
          verdict_range([], "2026-06-01", "2026-06-30")[0], "inconclusive")
    check("S8 날짜 전부 불량 → inconclusive",
          verdict_range([{"date": "bad", "close": 1}], "2026-06-01", "")[0],
          "inconclusive")
    check("S9 기준 미지정 → inconclusive",
          verdict_range(_rows(["2026-06-15"]), "", "")[0], "inconclusive")

    print("\n expected_in_window — 기대값 산출")
    BASE = ["2026-06-01", "2026-06-15", "2026-06-30", "2026-07-15"]
    check("S10 창 안 개수", expected_in_window(BASE, "2026-06-01", "2026-06-30"), 3)
    check("S11 from 단독", expected_in_window(BASE, "2026-06-15", ""), 3)
    check("S12 to 단독", expected_in_window(BASE, "", "2026-06-15"), 2)
    check("S13 경계 없음 = 전량", expected_in_window(BASE, "", ""), 4)
    check("S14 빈 교집합", expected_in_window(BASE, "2027-01-01", ""), 0)

    print("\n verdict_window — 완전성 (v2 의 핵심 · 여기가 판별력이다)")
    check("S15 완전",
          verdict_window(_rows(["2026-06-01", "2026-06-15", "2026-06-30"]),
                         "2026-06-01", "2026-06-30", 3)[0], "honored_complete")
    # 실물 실패 모양: 경계는 지키는데 중간 봉이 빠진다 — 좁은 창에선 안 보인다
    check("S16 절삭 → truncated (2봉 이상 부족)",
          verdict_window(_rows(["2026-06-01", "2026-06-30"]),
                         "2026-06-01", "2026-06-30", 4)[0], "honored_truncated")
    check("S17 초과 → anomaly",
          verdict_window(_rows(["2026-06-01", "2026-06-15", "2026-06-30"]),
                         "2026-06-01", "2026-06-30", 1)[0], "anomaly")
    # 경계 케이스: 기준선과 창 요청 사이 수 초에 당일 봉이 생길 수 있다.
    # 1봉 차이는 통과, 2봉부터는 실패 — 이 경계가 흐려지면 절삭을 놓친다.
    check("S18 -1봉은 완전으로 본다 (당일 봉 타이밍)",
          verdict_window(_rows(["2026-06-01", "2026-06-30"]),
                         "2026-06-01", "2026-06-30", 3)[0], "honored_complete")
    check("S18b +1봉도 완전으로 본다",
          verdict_window(_rows(["2026-06-01", "2026-06-15", "2026-06-30"]),
                         "2026-06-01", "2026-06-30", 2)[0], "honored_complete")
    check("S19 경계 위반이 완전성보다 우선",
          verdict_window(_rows(["2021-01-01", "2026-06-15", "2026-06-30"]),
                         "2026-06-01", "2026-06-30", 3)[0], "ignored")
    check("S20 빈 응답은 complete 가 아니다",
          verdict_window([], "2026-06-01", "2026-06-30", 0)[0], "inconclusive")

    print("\n bars_per_calendar_day / calendar_days_for")
    r = bars_per_calendar_day(["2021-08-27", "2026-08-26"])
    check("S21 비율 계산됨", 0.0 < r < 1.0, True)
    check("S22 1봉 시리즈는 0", bars_per_calendar_day(["2026-01-01"]), 0.0)
    check("S23 달력일 환산 (252봉·비율0.69)",
          calendar_days_for(252, 0.69) > 252, True)
    check("S24 비율 0 이면 0", calendar_days_for(252, 0.0), 0)

    print("\n scan_windows — 하류 윈도우 탐지")
    # ⚠️ 판별 케이스: 이름 필터를 지웠을 때 **결과가 달라져야** 한다.
    #    PORT=8080 만으로는 안 된다 — 8080 은 크기 필터(_CONST_MAX)에 먼저
    #    걸려서, 이름 필터를 통째로 제거해도 여전히 미검출이다.
    #    5000 미만이면서 이름이 봉수와 무관한 상수가 있어야 판별력이 생긴다.
    SRC = ("df['x'].rolling(200).mean()\n"
           "df['y'].ewm(span=26).mean()\n"
           "z = df.tail(60)\n"
           "w = arr[-252:]\n"
           "MIN_PRIOR_BARS = 220\n"
           "MAX_WORKERS = 8\n"
           "TIMEOUT_SEC = 20\n"
           "PORT = 8080\n")
    got = scan_windows(SRC)
    check("S25 rolling 잡힘", any(k == "rolling" and v == 200 for _l, k, v in got), True)
    check("S26 ewm span 잡힘", any(k == "ewm" and v == 26 for _l, k, v in got), True)
    check("S27 tail 잡힘", any(k == "tail" and v == 60 for _l, k, v in got), True)
    check("S28 음수 슬라이스 잡힘", any(k == "slice" and v == 252 for _l, k, v in got), True)
    check("S29 봉수 상수 잡힘", any(v == 220 for _l, _k, v in got), True)
    # 역검증: 이름이 봉수와 무관한 상수는 잡으면 안 된다 (오탐 = 무시당함)
    check("S30 큰 무관 상수 미검출", any(v == 8080 for _l, _k, v in got), False)
    check("S30b 작은 무관 상수 미검출 (MAX_WORKERS=8)",
          any(v == 8 for _l, _k, v in got), False)
    check("S30c 작은 무관 상수 미검출 (TIMEOUT_SEC=20)",
          any(k.startswith("const") and v == 20 for _l, k, v in got), False)
    check("S31 최대 윈도우", max(v for _l, _k, v in got), 252)
    check("S32 구문오류는 빈 결과", scan_windows("def ("), [])

    print("\n extract_rows / row_dates")
    check("S33 dict 래핑 해제",
          len(extract_rows({"historical": _rows(["2026-01-02"])})), 1)
    check("S34 비리스트 → 빈 결과", extract_rows("nope"), [])
    check("S35 날짜 정렬",
          row_dates(_rows(["2026-06-30", "2021-08-27"]))[0][0], "2021-08-27")

    print("\n find_value")
    hits = find_value(_rows(["2021-06-30"], 113.98) + _rows(["2026-06-29"], 56.72),
                      113.98)
    check("S36 정확값 발견", len(hits) > 0, True)
    check("S37 위치가 최고참", min(h[0] for h in hits) if hits else -1, 0)
    check("S38 허용오차 밖 미발견",
          len(find_value(_rows(["2021-06-30"], 120.0), 113.98)), 0)

    print("\n 구조 — 원시 FMP 호출 (diag_fmp_ssot A1 래칫과 동일 규칙)")
    me = os.path.abspath(__file__)
    check("S39 원시 requests.get 0곳", len(_raw_get_calls(me)), 0)
    check("S40 fmp_http 경유", _uses(me, "fmp_get_ex"), True)
    check("S41 탐지기 역검증 — 진짜 원시 호출을 잡는다",
          len(_raw_get_calls(src="import requests\n"
                                 "r = requests.get(f'{B}/quote?apikey={k}')\n")), 1)
    check("S42 탐지기 오탐 없음 — 무관한 .get 은 안 잡는다",
          len(_raw_get_calls(src="d={}\nv=d.get('apikey')\n"
                                 "s.get('https://financialmodelingprep.com/x')\n")), 0)

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

    with_a = "--with-a" in sys.argv
    print("=" * 74)
    print(f"FMP 날짜창 프로브 v2 — 읽기 전용 · 시트 기록 없음 · "
          f"예상 {8 if with_a else 7}콜")
    print("=" * 74)

    a_verdict = part_a() if with_a else "skipped(v1 에서 not_found 로 종결)"
    b_ok, b_res, base_dates, bytes_per_bar = part_b()
    d_res = part_d(base_dates) if b_ok == "ok" else {}
    ratio = bars_per_calendar_day(base_dates)
    part_ce(root, bytes_per_bar, ratio)

    print("\n" + "=" * 74)
    print("요약")
    print("=" * 74)
    print(f"  [A] GNOM 113.98 : {a_verdict}")
    all_v = {**{k: v[0] for k, v in b_res.items()},
             **{k: v[0] for k, v in d_res.items()}}
    for k in sorted(all_v):
        print(f"  [{k}] {all_v[k]}")

    # ── 사전 확정 최종 판정 ────────────────────────────────────────────
    need = ["B2", "B3", "B4", "B5", "D1", "D2"]
    have = [all_v.get(k) for k in need]
    print("\n  " + "─" * 70)
    if not all_v or any(v is None for v in have):
        final, code = "불완전 — 일부 케이스가 실행되지 않음", 2
    elif all(v == "honored_complete" for v in have):
        final, code = ("✅ 전환 가능 — 6개 케이스 전부 경계·완전성 통과. "
                       "단 [E] 의 '위험'·'판정보류' 호출부는 개별 확인 필요", 0)
    elif any(v == "ignored" for v in have):
        final, code = "❌ 전환 불가 — 파라미터가 무시되는 케이스가 있다", 0
    elif any(v == "honored_truncated" for v in have):
        final, code = ("❌ 전환 불가 — 경계는 지키나 봉이 누락된다. "
                       "어느 창에서 절삭되는지 위 상세를 볼 것", 0)
    else:
        final, code = "⚠️ 불확정 — 재실행하거나 창을 바꿔 볼 것", 2
    print(f"  {final}")
    print(f"\n  {fh.fmp_stats_line()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
