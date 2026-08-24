# -*- coding: utf-8 -*-
"""diag_sheet_numfmt.py — 셀 서식이 숫자를 파괴하는 칸을 찾는다 (읽기 전용).

## 배경 — 왜 이게 필요한가

`Account_Profile` 의 `Earn_Trim_Cap_Pct` 는 몇 달 동안 조용히 죽어 있었다.
셀에 날짜 서식이 걸려 있어서 저장한 `0` 이 시트에 `1899-12-30` 으로 굳었고,
`get_all_values()` 는 **표시 문자열**을 돌려주므로 `pd.to_numeric` 이 NaN 을
냈다. 오류도 경고도 없었다. 0 이 "프리셋 기본값 사용" 이라 동작이 같아서
아무도 몰랐고, `Swing_Weight_Pct` 를 같은 자리에 얹다가 우연히 걸렸다.

같은 함정이 `Portfolios.AvgPrice/Quantity`, `Portfolio_Alert_State.Stop_Loss`
에 걸리면 평단·수량·손절가가 조용히 틀린다. 손절가가 틀리는 것은 이 시스템의
존재 이유(손실 방지)가 실패하는 것이다.

## 무엇을 하나

모든 워크시트를 **두 번** 읽는다.
  · FORMATTED   = get_all_values()                      ← 지금 코드가 쓰는 방식
  · UNFORMATTED = get_values(UNFORMATTED_VALUE)         ← 셀 서식 무시, 원값

두 결과가 갈리는 칸을 찾아 심각도로 분류한다.

  🔴 LOSS   원값은 숫자인데 표시 문자열은 숫자로 안 읽힘 → 코드가 NaN 을 받는다
  🟠 WRONG  둘 다 숫자로 읽히는데 값이 다름 → 코드가 **틀린 숫자**를 받는다
  ⬜ TEXT   원값이 숫자가 아님 → 무해 (날짜 문자열 컬럼 등 정상 케이스)

## 안전성

**읽기 전용이다.** update/clear/format/append 를 단 한 번도 호출하지 않는다.
쓰기 API 는 import 조차 하지 않는다. 몇 번을 돌려도 시트는 바뀌지 않는다.

## 사용법

    python3 automation/diag_sheet_numfmt.py            # 전체 스캔
    python3 automation/diag_sheet_numfmt.py --selftest # 네트워크 없이 분류기 검증
    python3 automation/diag_sheet_numfmt.py --only Portfolios,Account_Profile
"""
import os
import sys
import time

import pandas as pd

# ── 읽기 스로틀 — Sheets 읽기 쿼터는 분당 60회. 시트당 2회 읽으므로 여유를 둔다.
_THROTTLE_SEC = 1.2

# 코드가 실제로 숫자로 파싱하는 열. 여기에 LOSS/WRONG 이 뜨면 즉시 조치 대상.
# (없는 시트·열은 그냥 무시된다 — 목록이 낡아도 스캔 자체는 전체를 본다.)
_CRITICAL = {
    "Portfolios": {"AvgPrice", "Quantity"},
    "Portfolio_Alert_State": {"Stop_Loss", "Target_Price", "Swing_Weight_Pct"},
    "Account_Profile": {"Cash", "Risk_Pct", "Max_Position_Pct", "Max_Positions",
                        "Cash_Reserve_Pct", "Min_Trade_Dollars",
                        "Earn_Trim_Cap_Pct", "Swing_Weight_Pct", "Trim_Ratio_Pct"},
    "Trade_History": {"Price", "Quantity", "Realized_PnL"},
    "Watchlist": {"Alert_Price", "Alert_RSI", "Target_Price"},
    "Dividend_Log": {"Amount", "Shares", "Price"},
}

_MAX_SAMPLES = 4        # 열당 보고할 예시 칸 수


def _num(v):
    """코드가 숫자로 읽어낼 수 있는가? 못 읽으면 None."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if pd.notna(v) else None
    n = pd.to_numeric(str(v).strip(), errors="coerce")
    return float(n) if pd.notna(n) else None


def classify(fmt_v, raw_v):
    """(표시값, 원값) → 'LOSS' | 'WRONG' | 'TEXT' | None(정상)."""
    if str(fmt_v) == str(raw_v):
        return None
    r = _num(raw_v)
    if r is None:
        return "TEXT"                      # 원값이 숫자가 아니면 무해
    f = _num(fmt_v)
    if f is None:
        return "LOSS"                      # 원값은 숫자인데 표시로는 못 읽는다
    if abs(f - r) > 1e-9:
        return "WRONG"                     # 둘 다 읽히는데 값이 다르다
    return None                            # 표기만 다르고 값은 같다


# ══════════════════════════════════════════════════════════════════════
# 자기검증 — 네트워크 없이 분류기가 맞는지 확인한다.
#   ⚠️ 실데이터를 돌리기 전에 반드시 통과해야 한다. 분류기가 틀리면
#      "이상 없음" 이라는 거짓 안심을 주고, 그게 지금까지의 상태였다.
# ══════════════════════════════════════════════════════════════════════
def selftest():
    cases = [
        # (표시값, 원값, 기대)
        ("1899-12-30 0:00", 0, "LOSS"),        # Earn_Trim_Cap_Pct 실제 사례
        ("1900-02-01 0:00", 33, "LOSS"),       # Trim_Ratio_Pct 실제 사례
        ("1,000", 1000, "LOSS"),               # 천단위 쉼표도 to_numeric 실패
        ("$20.00", 20, "LOSS"),                # 통화 서식
        ("50%", 0.5, "LOSS"),                  # 퍼센트 서식 — to_numeric 이 못 읽는다
        # 🟠 WRONG 은 '둘 다 읽히는데 값이 다른' 경우다. 표시 반올림이 대표적이고
        #    가장 위험하다 — 코드가 그럴듯한 틀린 숫자를 받아 아무도 의심하지 않는다.
        ("33", 33.456, "WRONG"),               # 소수점 0자리 서식이 값을 자름
        ("0.06", 0.0611, "WRONG"),             # 소수점 주식이 표시 반올림에 깎임
        ("0.5", 0.5, None),                    # 정상
        ("50", 50, None),                      # 정상
        ("50.0", 50, None),                    # 표기만 다름
        ("2026-08-23 17:00:00", "2026-08-23 17:00:00", None),   # 문자열 그대로
        ("risk_based", "risk_based", None),    # 텍스트
        ("", "", None),                        # 빈칸
        ("tax_free", "tax_free", None),
        ("Sheet1!A1", "=Sheet1!A1", "TEXT"),   # 수식 — 원값이 숫자가 아님
    ]
    ok, bad = 0, []
    for fmt_v, raw_v, exp in cases:
        got = classify(fmt_v, raw_v)
        if got == exp:
            ok += 1
        else:
            bad.append((fmt_v, raw_v, exp, got))
    # 양성 대조: 분류기를 '항상 None' 으로 고장내면 아래 개수만큼 실패해야 한다.
    # 이 값이 0 이면 케이스 집합에 이상 사례가 없다는 뜻 → 하네스가 무의미하다.
    broken = sum(1 for c in cases if c[2] is not None)
    kinds = {c[2] for c in cases if c[2] is not None}
    print("=" * 70)
    print(f"자기검증  통과 {ok} / 실패 {len(bad)}  (총 {len(cases)})")
    print(f"양성대조  이상으로 분류돼야 하는 케이스 {broken}건 "
          f"({', '.join(sorted(kinds))}) → 'always None' 구현이면 {broken}건 실패")
    print("=" * 70)
    for f, r, e, g in bad:
        print(f"  ❌ 표시={f!r} 원값={r!r}  기대={e} 실제={g}")
    # LOSS·WRONG 두 종류가 모두 케이스에 있어야 분류기 전체가 검증된다.
    return 1 if (bad or broken == 0 or kinds != {"LOSS", "WRONG", "TEXT"}) else 0


# ══════════════════════════════════════════════════════════════════════
# 실스캔
# ══════════════════════════════════════════════════════════════════════
def scan(only=None):
    import gspread
    from google.oauth2.service_account import Credentials

    key = os.environ.get("GSPREAD_KEY", "")
    if not key:
        print("[ERR] GSPREAD_KEY 환경변수가 없습니다.")
        return 2
    import json
    creds = Credentials.from_service_account_info(
        json.loads(key),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open("Quant_DB")

    wss = sh.worksheets()
    if only:
        want = {t.strip().lower() for t in only.split(",") if t.strip()}
        wss = [w for w in wss if w.title.lower() in want]
    print(f"[INFO] 워크시트 {len(wss)}개 스캔 (시트당 읽기 2회, "
          f"스로틀 {_THROTTLE_SEC}s)\n")

    findings = []       # (sheet, col_name, col_letter, kind, count, samples)
    errors = []

    for i, ws in enumerate(wss, 1):
        title = ws.title
        try:
            fmt = ws.get_all_values() or []
            time.sleep(_THROTTLE_SEC)
            raw = ws.get_values(value_render_option="UNFORMATTED_VALUE") or []
            time.sleep(_THROTTLE_SEC)
        except Exception as e:
            errors.append((title, str(e)))
            print(f"  [{i}/{len(wss)}] {title:28s} ⚠️ 읽기 실패: {e}")
            continue

        if not fmt or len(fmt) < 2:
            print(f"  [{i}/{len(wss)}] {title:28s} — 데이터 없음")
            continue

        header = [str(h).strip() for h in fmt[0]]
        # 열 단위로 집계한다. 셀 하나하나 나열하면 읽을 수가 없다.
        buckets = {}
        for r_ix in range(1, min(len(fmt), len(raw))):
            frow, rrow = fmt[r_ix], raw[r_ix]
            for c_ix in range(min(len(frow), len(rrow))):
                kind = classify(frow[c_ix], rrow[c_ix])
                if kind in (None, "TEXT"):
                    continue
                name = header[c_ix] if c_ix < len(header) else f"?{c_ix}"
                b = buckets.setdefault((c_ix, name, kind),
                                       {"n": 0, "samples": []})
                b["n"] += 1
                if len(b["samples"]) < _MAX_SAMPLES:
                    b["samples"].append(
                        (f"{chr(ord('A') + c_ix) if c_ix < 26 else '?'}{r_ix + 1}",
                         frow[c_ix], rrow[c_ix]))

        if not buckets:
            print(f"  [{i}/{len(wss)}] {title:28s} ✅ 이상 없음 "
                  f"({len(fmt) - 1}행)")
            continue

        print(f"  [{i}/{len(wss)}] {title:28s} 🔎 {len(buckets)}개 열에서 이상")
        for (c_ix, name, kind), b in sorted(buckets.items()):
            letter = chr(ord("A") + c_ix) if c_ix < 26 else f"col{c_ix}"
            crit = name in _CRITICAL.get(title, set())
            findings.append((title, name, letter, kind, b["n"],
                             b["samples"], crit))

    # ── 보고 ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    if not findings:
        print("✅ 서식이 숫자를 파괴하는 칸이 없습니다.")
    else:
        crit = [f for f in findings if f[6]]
        rest = [f for f in findings if not f[6]]
        print(f"발견 {len(findings)}건 — 즉시 조치 {len(crit)} · 검토 {len(rest)}")
        print("=" * 70)
        for group, label in ((crit, "🚨 즉시 조치 (코드가 숫자로 파싱하는 열)"),
                             (rest, "🔎 검토 (파싱 대상인지 확인 필요)")):
            if not group:
                continue
            print(f"\n{label}")
            for title, name, letter, kind, n, samples, _ in group:
                mark = "🔴" if kind == "LOSS" else "🟠"
                print(f"  {mark} {kind:5s} {title}.{name} ({letter}열) — {n}칸")
                for addr, fv, rv in samples:
                    print(f"        {addr}: 표시={fv!r}  원값={rv!r}")
        print("\n" + "-" * 70)
        print("조치: 해당 열을 시트에서 선택 → 서식 → 숫자 → 자동 으로 되돌리고,")
        print("      그 시트를 읽는 코드를 UNFORMATTED_VALUE 로 바꾼다.")
        print("      🔴 LOSS 는 코드가 NaN 을 받는다(값이 사라진 것과 같다).")
        print("      🟠 WRONG 은 코드가 틀린 숫자를 받는다(더 위험하다).")

    if errors:
        print(f"\n⚠️ 읽기 실패 {len(errors)}건")
        for t, e in errors:
            print(f"   {t}: {e}")

    return 1 if any(f[6] for f in findings) else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    rc_self = selftest()
    if rc_self != 0:
        print("\n[STOP] 분류기 자기검증 실패 — 실스캔을 하지 않습니다.")
        sys.exit(rc_self)
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    print()
    sys.exit(scan(only))
