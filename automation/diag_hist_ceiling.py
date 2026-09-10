#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_hist_ceiling.py — FMP 이력 **상한**의 정체 규명 (읽기 전용)

왜 지금 이걸 묻나
─────────────────
`diag_credit_gate_probe` v2 가 부수적으로 잡았다: `historical-price-eod/full` 에
`from=2007-01-01&to=오늘` 단일 호출로 **4,952봉 · 19.7년**이 왔다. 그런데
저장소 여섯 곳이 "FMP 자체 상한 ~1,255봉"을 **사실로** 적고 있다.

⚠️ 이것은 §7 재조사 금지 항목에 대한 도전이 **아니다.**
   §7 에 박힌 명제는 "`limit` 은 무시된다"이고 그건 여전히 참이다. 이 프로브는
   `limit` 을 아예 보내지 않는다. 도전 대상은 거기서 **유도된** 명제 —
   "따라서 이력 상한은 ~1,255봉이다" — 이고, 그건 `from`/`to` 전환 이후
   **한 번도 검증된 적이 없다.**

   왜 검증되지 않았나가 이 건의 핵심이다. `hist_days_for_bars()` 가
   `HIST_MAX_DAYS=1826` 에서 클램프하므로, `diag_fmp_window` 가 창을
   아무리 흔들어도 **1826일 넘는 요청은 저장소 어디에서도 나가지 않는다.**
   정책 상수가 자기를 반증할 기회를 구조적으로 막고 있었다. 상한이
   API 사실인지 정책인지는 그래서 지금까지 알 수 없었다.

왜 기존 파일에 안 붙이나
────────────────────────
  · `diag_fmp_depth.py` — '유니버스가 실제로 몇 봉을 확보하는가'(신규 상장·
    이력 짧은 종목)를 묻는다. `run_signal_backtest` 에 결합돼 있고 질문이 다르다.
  · `diag_fmp_window.py` — '`from`/`to` 가 먹히는가'. 종결된 질문이고, 그
    파일의 판정법(B1 무파라미터 응답을 정답지로 삼기)은 여기 쓸 수 없다 —
    2021년 이전 구간에는 대조할 기준선이 없다.
  → 종결된 프로브에 열린 질문을 섞으면 다음 사람이 어느 쪽이 살아 있는지
    구분하지 못한다. 새 파일로 둔다.

  실행: python automation/diag_hist_ceiling.py
        python automation/diag_hist_ceiling.py --selftest   # 네트워크 불필요

아무것도 수정하지 않는다. 시트 접근 0 · 파일 쓰기 0 · FMP 6콜.


═══════════════════════════════════════════════════════════════════════════
사전 커밋 판정 기준 — 2026-09-10 확정. 결과를 본 뒤 재협상하지 않는다.
═══════════════════════════════════════════════════════════════════════════

  D1  **상한의 유형.** 상장일이 크게 다른 4종목에 같은 최심 창을 던지고
      응답의 최소일·봉수 분포로 판정한다.
        · 최소일이 전부 같다(±5일)            → **고정 시작일** 상한
        · 최소일은 다른데 봉수가 서로 ±5% 이내 → **고정 봉수** 상한
        · 최소일이 각자 다르고 참고 상장일에 닿음 → **상한 없음**
        · 그 외                                 → **불명**(숫자를 정하지 않는다)
      ⚠️ 판정 통계를 '봉수'가 아니라 **종목 간 분산**으로 잡은 이유: 한 종목만
         보면 세 유형이 전부 같은 그림을 낸다. 파라미터가 작동했을 때만
         성립하는 통계를 고르라는 것이 `symbol-change` 때 배운 규율이다.

  D2  **완전성.** 최심 응답의 **연도별 봉 수**가 240~260 밖인 연도가 전체
      연도의 **10% 초과**면 `truncated` — 깊은 창은 쓸 수 없다고 판정한다.
      경계(최소·최대일)만 맞고 중간이 비는 실패가 실재한다(diag_fmp_window
      [D] 의 교훈). 부분 연도(첫 해·올해)는 분모에서 제외한다.

  D3  **새 상한 숫자는 이 프로브에서 정하지 않는다.** 여기서는 관측만 하고,
      상수는 Step 1 에서 **가장 얕은 종목**을 제약으로 삼아 정한다.
      ⚠️ 이 조항이 있는 이유: 이번 라운드에서 단일 관측에 앵커를 박는 실수를
         두 번 했다(R1 의 off 36%, P5 의 5~15%). 세 번은 하지 않는다.

  D4  **동결 영향은 판정이 아니라 고지다.** Q4 는 상한을 올렸을 때
      `diag_satellite_backtest` 의 구간 수가 어떻게 변하는지 계산만 한다.
      T1~T4 는 판정 완료·동결 항목이므로, 데이터 깊이가 바뀌면 재실행 결과가
      달라진다는 사실을 **숫자로** 남기는 것이 목적이다.

출력 전용: 페이로드 크기·소요 시간(Q3) · 낡은 주석 목록(Q5)


이 프로브가 답할 수 없는 것
───────────────────────────
 1) **플랜 의존성.** 상한이 없다는 결과가 나와도 그것은 이 계정·이 시점의
    관측이다. FMP 가 조용히 바꿀 수 있다. 상수를 올리면 그 위험을 떠안는
    것이고, 그래서 Step 1 은 상수 변경과 함께 **얕아졌을 때 시끄럽게 죽는
    가드**를 같이 넣어야 한다.
 2) **정확성을 재지 않는다.** 2008년 봉이 온다는 것과 그 값이 맞다는 것은
    다른 문제다. 액면분할 조정·배당 조정의 과거 정확성은 별건이다.
 3) **비용의 상한을 재지 않는다.** Q3 는 SPY 1종목 기준이다. 유니버스 60종목
    × 20년의 실제 부하는 Step 1 에서 별도로 봐야 한다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date

import pandas as pd

# ── 리포 루트 + 자기 폴더를 sys.path 에 (실행 위치 무관) ─────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_extras as fx      # noqa: E402  — 창/봉 정책 SSOT (읽기만 한다)
import fmp_http as fh        # noqa: E402  — FMP 호출 SSOT (A1: 생 requests 금지)

DEEP_FROM = "1970-01-01"     # 최심 요청. 어떤 미국 상장 종목보다 앞선다.
CONTROL_FROM = "2015-01-01"  # 대조군 — 창이 실제로 경계를 지키는지 확인용

# 참고 상장일. **판정에 직접 쓰지 않는다** — D1 은 종목 간 분산으로 판정하고
# 이 값들은 '상한 없음' 가지에서 보조 확인으로만 쓴다. 외부 기억은 틀릴 수
# 있으므로 어긋나도 그 자체를 실패로 만들지 않는다(출력에 같이 찍어 사람이 본다).
PROBE_SYMBOLS = [
    ("AAPL", "1980-12-12"),
    ("MSFT", "1986-03-13"),
    ("SPY", "1993-01-22"),
    ("QQQ", "1999-03-10"),
]

D1_SAME_DATE_TOL_DAYS = 5
D1_SAME_BARS_TOL = 0.05
D2_YEAR_LO, D2_YEAR_HI = 240, 260
D2_MAX_BAD_FRAC = 0.10

# Q5 — 낡은 주석 스캔 패턴
STALE_PATTERNS = [
    (r"1,?255\s*봉", "~1,255봉 상한 주장"),
    (r"1,?254\s*봉", "~1,254봉 상한 주장"),
    (r"FMP\s*자체\s*상한", "'FMP 자체 상한' 단정"),
    (r"≈\s*4\s*년", "'평가 가능 구간 ≈4년' 전제"),
    (r"계정\s*플랜의\s*이력\s*한도", "'플랜 이력 한도' 가설"),
]

_SEP = "=" * 76


# ══════════════════════════════════════════════════════════════════════════
# 순수 로직 (selftest 대상)
# ══════════════════════════════════════════════════════════════════════════
def year_counts(dates: pd.DatetimeIndex) -> dict:
    """연도 → 봉 수."""
    s = pd.Series(1, index=pd.DatetimeIndex(dates))
    return {int(y): int(v) for y, v in s.groupby(s.index.year).sum().items()}


def completeness(dates: pd.DatetimeIndex) -> tuple:
    """D2 — 연도별 봉 수로 중간 절삭을 잡는다. 반환 (판정, 나쁜연도, 총연도).

    ⚠️ 첫 해와 마지막 해는 **부분 연도**라 분모에서 뺀다. 안 빼면 어떤 응답도
       두 해가 자동으로 '나쁜 연도'가 되어 짧은 시리즈에서 판정이 뒤집힌다.
    """
    yc = year_counts(dates)
    if len(yc) <= 2:
        return "판정불가", [], 0
    ys = sorted(yc)
    full = ys[1:-1]
    bad = [y for y in full if not (D2_YEAR_LO <= yc[y] <= D2_YEAR_HI)]
    frac = len(bad) / len(full) if full else 0.0
    return ("truncated" if frac > D2_MAX_BAD_FRAC else "complete"), bad, len(full)


def classify_ceiling(obs: dict) -> tuple:
    """D1 — 종목 간 분산으로 상한 유형을 판정. 반환 (유형, 근거문자열).

    obs: {심볼: {"min": Timestamp, "bars": int, "ref": "YYYY-MM-DD"}}

    ⚠️ 검사 순서에 의미가 있다. '고정 시작일'과 '고정 봉수'를 먼저 배제해야
       '상한 없음'을 주장할 수 있다. 순서를 뒤집으면 우연히 상장일 근처에서
       잘린 경우를 '상한 없음'으로 오판한다.
    """
    if len(obs) < 3:
        return "불명", f"종목 {len(obs)}개 — 분산을 볼 수 없다(최소 3개 필요)"

    mins = [v["min"] for v in obs.values()]
    spread = (max(mins) - min(mins)).days
    if spread <= D1_SAME_DATE_TOL_DAYS:
        return "고정 시작일", (f"최소일이 전부 {min(mins).date()} 부근 "
                            f"(분산 {spread}일 ≤ {D1_SAME_DATE_TOL_DAYS})")

    bars = [v["bars"] for v in obs.values()]
    lo, hi = min(bars), max(bars)
    if lo > 0 and (hi - lo) / lo <= D1_SAME_BARS_TOL:
        return "고정 봉수", (f"최소일은 다른데 봉수가 {lo}~{hi} "
                          f"(편차 {(hi - lo) / lo * 100:.1f}% ≤ "
                          f"{D1_SAME_BARS_TOL * 100:.0f}%)")

    reached = []
    for sym, v in obs.items():
        ref = pd.to_datetime(v["ref"], errors="coerce")
        reached.append(pd.notna(ref) and v["min"] <= ref + pd.Timedelta(days=45))
    if all(reached):
        return "상한 없음", f"{len(obs)}종목 모두 참고 상장일에 도달 (최소일 분산 {spread}일)"
    n_ok = sum(1 for x in reached if x)
    return "불명", (f"최소일 분산 {spread}일 · 봉수 {lo}~{hi} · "
                  f"상장일 도달 {n_ok}/{len(obs)} — 유형을 특정할 수 없다")


def segments_for(bars: int, warmup: int, seg_bars: int, seg_max: int) -> int:
    """diag_satellite_backtest 의 구간 수. 파일 주석의 일반식과 같아야 한다:
    (받는봉수 - WARMUP_BARS) // SEG_BARS, 단 SEG_MAX 로 캡."""
    if bars <= warmup or seg_bars <= 0:
        return 0
    return int(min(seg_max, (bars - warmup) // seg_bars))


def scan_stale(root: str) -> list:
    """Q5 — 낡은 상한 주장을 담은 줄을 찾는다. **고치지 않는다. 목록만.**"""
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", ".github", "node_modules")]
        for fn in sorted(filenames):
            if not fn.endswith((".py", ".md", ".yml")):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as fp:
                    lines = fp.read().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(lines, 1):
                for pat, why in STALE_PATTERNS:
                    if re.search(pat, line):
                        rel = os.path.relpath(path, root)
                        hits.append((rel, i, why, line.strip()[:70]))
                        break
    return hits


# ══════════════════════════════════════════════════════════════════════════
# 실측
# ══════════════════════════════════════════════════════════════════════════
def fetch(symbol: str, from_date: str, to_date: str) -> tuple:
    """(DatetimeIndex, 바이트, 초, kind). 실패 시 (빈 인덱스, 0, 초, kind).

    ⚠️ `fx.hist_range_params()` 를 쓰지 않는다 — 그것은 **오늘 기준 룩백**
       정책이고, 여기서 묻는 것은 **절대 구간**이다. 게다가 그 함수는
       `HIST_MAX_DAYS` 에서 클램프하므로, 그걸 쓰면 이 프로브는
       **자기가 검증하려는 상한 안에 갇힌다.** 그 예외는 이 함수 하나에 갇혀
       있고 프로덕션 코드는 계속 `hist_range_params` 만 쓴다.
    """
    path = (f"historical-price-eod/full?symbol={symbol}"
            f"&from={from_date}&to={to_date}")
    t0 = time.perf_counter()
    data, _status, kind = fh.fmp_get_json_ex(path)
    dt = time.perf_counter() - t0
    rows = data.get("historical", data) if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        return pd.DatetimeIndex([]), 0, dt, (kind if kind != "ok" else "empty")
    nbytes = len(json.dumps(rows))
    idx = pd.to_datetime(pd.DataFrame(rows)["date"], errors="coerce").dropna()
    return pd.DatetimeIndex(sorted(idx)), nbytes, dt, "ok"


def probe_ceiling() -> tuple:
    print(f"\n{_SEP}\n■ Q1 — 상한의 유형 (D1)\n{_SEP}")
    print(f"  같은 최심 창(from={DEEP_FROM})을 상장일이 크게 다른 4종목에 던진다.")
    print("  판정 통계는 한 종목의 봉수가 아니라 **종목 간 분산**이다.\n")
    print(f"  {'종목':<7}{'봉수':>8}{'최소일':>13}{'최대일':>13}{'참고상장일':>13}"
          f"{'KB':>8}{'초':>7}")
    print("  " + "-" * 72)

    obs, deepest, deepest_sym = {}, pd.DatetimeIndex([]), None
    for sym, ref in PROBE_SYMBOLS:
        idx, nb, dt, kind = fetch(sym, DEEP_FROM, date.today().strftime("%Y-%m-%d"))
        if len(idx) == 0:
            print(f"  {sym:<7}{'—':>8}{'—':>13}{'—':>13}{ref:>13}"
                  f"{'—':>8}{dt:>7.2f}   ⚠️ {kind}")
            continue
        print(f"  {sym:<7}{len(idx):>8}{str(idx[0].date()):>13}{str(idx[-1].date()):>13}"
              f"{ref:>13}{nb / 1024:>8.0f}{dt:>7.2f}")
        obs[sym] = {"min": idx[0], "bars": len(idx), "ref": ref, "kb": nb / 1024, "sec": dt}
        if len(idx) > len(deepest):
            deepest, deepest_sym = idx, sym

    kind_, why = classify_ceiling(obs)
    print(f"\n  ★ D1 판정: **{kind_}**  — {why}")
    if kind_ == "상한 없음":
        print("     → fx.HIST_MAX_DAYS=1826 은 순수 정책 상수다. Step 1 로 간다.")
    elif kind_ in ("고정 시작일", "고정 봉수"):
        print("     → 상한은 실재한다. 다만 위치가 1,255봉이 아니다 — 그 숫자를")
        print("        적은 주석들은 여전히 틀렸고 정정 대상이다(Q5).")
    else:
        print("     → **숫자를 정하지 않는다**(D3). 유형이 불명이면 상수를 못 고친다.")
    return obs, deepest, deepest_sym


def probe_completeness(idx: pd.DatetimeIndex, sym: str) -> str:
    print(f"\n{_SEP}\n■ Q2 — 완전성 (D2)\n{_SEP}")
    print("  경계만 맞고 중간이 비는 실패가 실재한다. 연도별 봉 수로 잡는다.\n")
    if len(idx) == 0:
        print("  [X] 데이터 없음 — 판정 불가")
        return "판정불가"
    verdict, bad, n_full = completeness(idx)
    yc = year_counts(idx)
    ys = sorted(yc)
    row, out = [], []
    for y in ys:
        row.append(f"{y}:{yc[y]}")
        if len(row) == 8:
            out.append("  " + "  ".join(row))
            row = []
    if row:
        out.append("  " + "  ".join(row))
    print(f"  {sym} 연도별 봉 수 (첫 해·올해는 부분 연도라 분모에서 제외)")
    print("\n".join(out))
    print(f"\n  ★ D2 판정: **{verdict}** — 완전 연도 {n_full}개 중 "
          f"{D2_YEAR_LO}~{D2_YEAR_HI} 밖 {len(bad)}개"
          + (f" ({', '.join(map(str, bad[:8]))}{' …' if len(bad) > 8 else ''})" if bad else ""))
    if verdict == "truncated":
        print("     → 깊은 창은 쓸 수 없다. 상한을 올려도 데이터가 성기다.")

    dup = len(idx) - len(pd.Index(idx).unique())
    mono = bool(pd.Index(idx).is_monotonic_increasing)
    print(f"     [보조] 중복 날짜 {dup}개 · 정렬 단조 {'O' if mono else 'X'}")
    return verdict


def probe_control() -> None:
    """대조군 — 가까운 창이 실제로 경계를 지키는가. 이게 깨지면 Q1 도 못 믿는다."""
    print(f"\n{_SEP}\n■ Q3 — 대조군 + 페이로드 비용 (출력 전용)\n{_SEP}")
    today = date.today().strftime("%Y-%m-%d")
    idx, nb, dt, kind = fetch("SPY", CONTROL_FROM, today)
    if len(idx) == 0:
        print(f"  [X] 대조군 실패 ({kind})")
        return
    ok = idx[0] >= pd.Timestamp(CONTROL_FROM)
    print(f"  SPY from={CONTROL_FROM}: {len(idx)}봉 · 최소일 {idx[0].date()} · "
          f"{nb / 1024:.0f}KB · {dt:.2f}초  → 경계 {'O' if ok else 'X'}")
    if not ok:
        print("  ⚠️ 대조군이 경계를 어겼다 — Q1 의 최소일도 신뢰할 수 없다.")
    print("\n  비용 참고: 대화형(Streamlit) 경로에 20년 창을 물리면 종목당 이만큼이다.")
    print("  옵션 B(대화형은 1826 유지 · 딥 소비자만 확장)의 판단 재료다.")


def probe_freeze_impact(obs: dict) -> None:
    print(f"\n{_SEP}\n■ Q4 — 동결 백테스트 영향 (D4 · 판정 아님 · 고지)\n{_SEP}")
    try:
        import diag_satellite_backtest as bt
        HB, SB, SM, WB = bt.HISTORY_BARS, bt.SEG_BARS, bt.SEG_MAX, bt.WARMUP_BARS
        src = "diag_satellite_backtest 에서 읽음"
    except Exception as e:
        HB, SB, SM, WB = 1300, 252, 6, 127
        src = f"임포트 실패({type(e).__name__}) — 리터럴 사용, 값 확인 필요"
    print(f"  상수: HISTORY_BARS={HB} · SEG_BARS={SB} · SEG_MAX={SM} · "
          f"WARMUP_BARS={WB}  ({src})")

    deepest_bars = max([v["bars"] for v in obs.values()], default=0)
    print(f"\n  {'상한(달력일)':>14}{'요청 가능 봉':>14}{'실제 받는 봉':>14}{'구간 수':>10}")
    print("  " + "-" * 52)
    for cap in (fx.HIST_MAX_DAYS, 3652, 5478, 7305, 18262):
        askable = int(cap * fx.HIST_TD_PER_CD)
        got = min(HB, askable, deepest_bars) if deepest_bars else min(HB, askable)
        segs = segments_for(got, WB, SB, SM)
        mark = "  ← 현행" if cap == fx.HIST_MAX_DAYS else ""
        print(f"  {cap:>14}{askable:>14}{got:>14}{segs:>10}{mark}")

    print(f"\n  ⚠️ HISTORY_BARS={HB} 가 실질 상한이라 구간 수는 곧 포화한다. 즉 상수를")
    print("     올리는 것만으로는 백테스트가 깊어지지 않는다 — HISTORY_BARS 도 같이")
    print("     올려야 한다. **그 순간 T1~T4 의 재실행 결과가 달라진다.**")
    print("     T1~T4 는 판정 완료·동결 항목이다. 옵션 C(동결 백테스트를 명시적")
    print("     as-of 창으로 고정)가 필요한 이유가 이 표다.")


def probe_stale() -> None:
    print(f"\n{_SEP}\n■ Q5 — 낡은 상한 주장 감사 (출력 전용 · 고치지 않는다)\n{_SEP}")
    hits = scan_stale(_ROOT)
    if not hits:
        print("  (해당 없음)")
        return
    print(f"  {len(hits)}곳. Step 1 의 정정 목록이 된다.\n")
    cur = None
    for rel, ln, why, text in hits:
        if rel != cur:
            print(f"  {rel}")
            cur = rel
        print(f"    L{ln:<6} {why:<22} {text}")


# ══════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    fails = []

    # 1) D1 세 유형을 각각 만들어 낸다. **하나라도 못 내면 판정기가 죽은 것이다.**
    def _mk(d, b, r):
        return {"min": pd.Timestamp(d), "bars": b, "ref": r}
    fixed_start = {"A": _mk("1996-01-02", 7600, "1980-12-12"),
                   "B": _mk("1996-01-03", 7599, "1986-03-13"),
                   "C": _mk("1996-01-02", 7601, "1993-01-22")}
    if classify_ceiling(fixed_start)[0] != "고정 시작일":
        fails.append("D1: 최소일이 같은데 '고정 시작일'로 판정 안 됨")
    fixed_bars = {"A": _mk("2006-01-02", 5000, "1980-12-12"),
                  "B": _mk("2006-06-02", 5010, "1986-03-13"),
                  "C": _mk("2007-01-02", 4990, "1993-01-22")}
    if classify_ceiling(fixed_bars)[0] != "고정 봉수":
        fails.append("D1: 봉수가 같은데 '고정 봉수'로 판정 안 됨")
    unlimited = {"A": _mk("1980-12-12", 11400, "1980-12-12"),
                 "B": _mk("1986-03-13", 10100, "1986-03-13"),
                 "C": _mk("1993-01-22", 8300, "1993-01-22")}
    if classify_ceiling(unlimited)[0] != "상한 없음":
        fails.append("D1: 상장일에 닿았는데 '상한 없음'으로 판정 안 됨")

    # 2) 순서 함정 — 봉수가 우연히 비슷한 '상한 없음' 후보를 '고정 봉수'로
    #    오판하지 않는가. 이 검사가 없으면 검사 순서를 바꿔도 1)이 통과한다.
    tricky = {"A": _mk("1980-12-12", 11400, "1980-12-12"),
              "B": _mk("1986-03-13", 11390, "1986-03-13"),
              "C": _mk("1993-01-22", 11395, "1993-01-22")}
    if classify_ceiling(tricky)[0] != "고정 봉수":
        fails.append("D1 순서: 봉수가 같으면 상장일 도달보다 '고정 봉수'가 우선이어야 한다")

    # 3) 종목 부족 → 불명 (분산을 볼 수 없으면 판정하지 않는다)
    if classify_ceiling({"A": _mk("1993-01-22", 8300, "1993-01-22")})[0] != "불명":
        fails.append("D1: 종목 2개 미만인데 판정을 냈다")

    # 4) D2 완전성 — 양방향. 부분 연도 제외가 실제로 되는가까지.
    full = pd.DatetimeIndex(sorted(
        [pd.Timestamp(f"{y}-01-01") + pd.Timedelta(days=i)
         for y in range(2010, 2016) for i in range(250)]))
    if completeness(full)[0] != "complete":
        fails.append("D2: 정상 시리즈가 truncated 로 나왔다")
    holed = [d for d in full if d.year != 2013] + \
            [pd.Timestamp("2013-06-01") + pd.Timedelta(days=i) for i in range(30)]
    if completeness(pd.DatetimeIndex(sorted(holed)))[0] != "truncated":
        fails.append("D2: 한 해가 30봉뿐인데 complete 로 나왔다")
    # 부분 연도 제외가 **실제로 판정을 바꾸는** 픽스처. 첫 해·마지막 해가
    # 짧은 것은 정상인데(시작·오늘), 분모에 넣으면 짧은 시리즈에서 그 둘만으로
    # 임계를 넘겨 멀쩡한 응답이 truncated 가 된다.
    partial = []
    for y, n in ((2010, 100), (2011, 250), (2012, 250),
                 (2013, 250), (2014, 250), (2015, 100)):
        partial += [pd.Timestamp(f"{y}-01-01") + pd.Timedelta(days=i) for i in range(n)]
    if completeness(pd.DatetimeIndex(sorted(partial)))[0] != "complete":
        fails.append("D2: 첫 해·마지막 해가 부분 연도인데 truncated 로 나왔다 "
                     "— 분모에서 제외되지 않는다")
    # 부분 연도만으로는 판정하지 않는다
    if completeness(pd.DatetimeIndex(
            [pd.Timestamp("2020-01-01") + pd.Timedelta(days=i) for i in range(300)]))[0] != "판정불가":
        fails.append("D2: 연도 2개 이하인데 판정을 냈다")

    # 5) 구간 공식 — diag_satellite_backtest 주석의 일반식과 일치해야 한다.
    #    주석 실측: 받는봉 1254 · WARMUP 127 · SEG 252 → 4구간
    if segments_for(1254, 127, 252, 6) != 4:
        fails.append("구간 공식: 현행 조건에서 4가 안 나온다 — 주석과 어긋난다")
    if segments_for(756, 127, 252, 6) != 2:
        fails.append("구간 공식: 756봉에서 2가 안 나온다")
    if segments_for(4952, 127, 252, 6) != 6:
        fails.append("구간 공식: 깊은 데이터에서 SEG_MAX 캡이 안 걸린다")
    if segments_for(100, 127, 252, 6) != 0:
        fails.append("구간 공식: 워밍업 미달인데 0이 아니다")

    # 6) 낡은 주석 스캐너 — 잡아야 할 것과 잡으면 안 될 것
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "a.py"), "w", encoding="utf-8") as f:
            f.write("# FMP 자체 상한(~1,255봉)이 또 있다\n"      # L1 hit
                    "x = 1\n"                                  # L2
                    "# 평가 가능 구간은 ≈1,000봉(≈4년)\n"        # L3 hit
                    "# 실측 1,254봉\n"                          # L4 hit
                    "# 무해한 줄 — 봉수 1300 요청\n")            # L5 매치 없어야
        os.makedirs(os.path.join(td, "__pycache__"), exist_ok=True)
        with open(os.path.join(td, "__pycache__", "b.py"), "w", encoding="utf-8") as f:
            f.write("# 1,255봉\n")
        hits = scan_stale(td)
        # 줄 번호까지 못박는다. 개수만 재면 '엉뚱한 줄을 잡고 진짜를 놓친'
        # 경우가 통과한다 — L5(무해한 줄)를 잡으면 여기서 걸린다.
        if sorted(h[1] for h in hits) != [1, 3, 4]:
            fails.append(f"Q5 스캐너: L1·L3·L4 여야 하는데 "
                         f"{sorted(h[1] for h in hits)}")
        if any("__pycache__" in h[0] for h in hits):
            fails.append("Q5 스캐너: __pycache__ 를 건너뛰지 않았다")

    print(f"\n{_SEP}\n■ selftest — 순수 로직 (네트워크 불필요)\n{_SEP}")
    if fails:
        for x in fails:
            print(f"  [X] {x}")
        print(f"\n  {len(fails)}건 실패")
        return 1
    print("  [O] 14개 검사 통과 — D1 유형 3 · D1 순서/부족 2 · "
          "D2 양방향+부분연도 4 · 구간 공식 4 · Q5 스캐너 2")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

    print(f"\n{_SEP}")
    print("FMP 이력 상한 규명 프로브 (Step 0) — 읽기 전용")
    print("상수를 고치지 않는다. 숫자는 Step 1 에서 정한다(D3).")
    print(_SEP)

    if _selftest() != 0:
        print("\n[중단] selftest 실패 상태에서 실측을 읽지 않는다.")
        return 1

    obs, deepest, sym = probe_ceiling()
    if not obs:
        print("\n[중단] 실측을 하나도 받지 못했다.")
        return 1
    probe_completeness(deepest, sym)
    probe_control()
    probe_freeze_impact(obs)
    probe_stale()

    print(f"\n{_SEP}")
    print(f"FMP 통계 — {fh.fmp_stats_line()}")
    print("이 프로브는 아무것도 수정하지 않았다. 시트 접촉 0 · 파일 쓰기 0.")
    print(_SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
