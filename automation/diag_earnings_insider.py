"""diag_earnings_insider.py — C블록(내부자 거래) 회귀 스위트.

네트워크·시트 접근 없음. FMP 호출을 스텁으로 갈아끼워 순수 로직만 본다.
몇 번을 돌려도 부작용이 없다.

여기서 지키려는 위험 4가지 — 전부 실제로 밟았거나 밟을 뻔한 것이다
──────────────────────────────────────────────────────────────────
  1. 유형 오염
       A-Award / F-InKind / M-Exempt / G-Gift 를 재량 거래로 세면
       베스팅일에 가짜 신호가 뜬다. 실측에서 WMT 는 보이는 4건이 전부
       F-InKind 였고 NVDA 는 G-Gift 가 500,000주였다.
  2. 창 오염
       90일 밖 행이 합계에 들어가면 종목마다 다른 기간을 더하게 된다.
  3. 열 정렬 붕괴
       내부자 4열을 **맨 뒤가 아닌 곳**에 끼우면 기존 29열 행의
       Data_Flags 가 숫자로, Notes 가 플래그로 읽힌다.
  4. 결측과 0의 혼동
       조회 실패를 0 으로 쓰면 "봤는데 없었다"와 "못 봤다"가 섞이고
       백테스트에서 매도 0건 종목이 조회 실패 종목과 같은 취급을 받는다.

실행
────
    python automation/diag_earnings_insider.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

import earnings_core as ec  # noqa: E402

TODAY = pd.Timestamp("2026-08-15")

_fail = []
_pass = 0


def check(name, cond, detail=""):
    global _pass
    if cond:
        _pass += 1
        print(f"  ✅ {name}")
    else:
        _fail.append(name)
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def row(days_ago, typ, qty, px, tdate=True):
    d = (TODAY - pd.Timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return {"symbol": "X", "transactionType": typ,
            "securitiesTransacted": qty, "price": px,
            "transactionDate": (d if tdate else ""),
            "filingDate": d, "reportingName": "DOE JANE"}


def stub(items):
    """ec._get 을 고정 응답으로 바꾼다."""
    ec._get = lambda path, key="": items


_ORIG_GET = ec._get


# ══════════════════════════════════════════════════════════════════════════
print("\n[1] 유형 필터 — 자동·스케줄 거래가 섞이면 안 된다")
# ══════════════════════════════════════════════════════════════════════════
stub([
    row(10, "S-Sale", 100, 10.0),        # 재량 매도 1,000
    row(20, "P-Purchase", 50, 20.0),     # 재량 매수 1,000
    row(30, "A-Award", 9999, 500.0),     # 부여 — 제외
    row(31, "F-InKind", 9999, 500.0),    # 세금원천징수 — 제외
    row(32, "M-Exempt", 9999, 500.0),    # 옵션행사 — 제외
    row(33, "G-Gift", 9999, 500.0),      # 증여 — 제외
    row(34, "I-Discretionary", 9999, 500.0),   # 계획 내 재량 — 제외
])
r = ec.fetch_insider_90d("X", key="k", today=TODAY)
check("재량 매도만 합산", r["sale_val"] == 1000.0, f"got {r['sale_val']}")
check("재량 매수만 합산", r["buy_val"] == 1000.0, f"got {r['buy_val']}")
check("건수도 재량만", (r["sale_n"], r["buy_n"]) == (1, 1),
      f"got {r['sale_n']}/{r['buy_n']}")

# 변이 — 필터를 빼면 잡히는가
_saved = ec.INSIDER_SELL_TYPES
ec.INSIDER_SELL_TYPES = ("S-Sale", "F-InKind", "A-Award")
r_mut = ec.fetch_insider_90d("X", key="k", today=TODAY)
ec.INSIDER_SELL_TYPES = _saved
check("[변이] 필터를 넓히면 값이 달라진다(테스트가 살아 있음)",
      r_mut["sale_val"] != r["sale_val"])

# ══════════════════════════════════════════════════════════════════════════
print("\n[2] 창 경계 — 90일 밖은 들어오면 안 된다")
# ══════════════════════════════════════════════════════════════════════════
stub([
    row(89, "S-Sale", 100, 10.0),    # 창 안 → 1,000
    row(90, "S-Sale", 100, 10.0),    # 경계 = cutoff. 포함
    row(91, "S-Sale", 100, 10.0),    # 창 밖 → 제외
    row(400, "S-Sale", 999, 999.0),  # 한참 밖 → 제외
])
r = ec.fetch_insider_90d("X", key="k", today=TODAY)
check("90일 밖 제외", r["sale_val"] == 2000.0, f"got {r['sale_val']}")
check("cov_d 는 가장 과거 행 기준", r["cov_d"] == 400, f"got {r['cov_d']}")

# ══════════════════════════════════════════════════════════════════════════
print("\n[3] cov_d — 창이 잘렸는지 알 수 있어야 한다")
# ══════════════════════════════════════════════════════════════════════════
stub([row(i, "S-Sale", 10, 10.0) for i in (1, 2, 3, 30)])
r = ec.fetch_insider_90d("X", key="k", today=TODAY)
check("cov_d=30 (90일 미달)", r["cov_d"] == 30, f"got {r['cov_d']}")
check("잘림 판정이 가능", r["cov_d"] < ec.PREVIEW_INSIDER_WINDOW)

# ══════════════════════════════════════════════════════════════════════════
print("\n[4] price 결측 — 건수는 세되 금액에는 넣지 않는다")
# ══════════════════════════════════════════════════════════════════════════
stub([
    row(5, "S-Sale", 100, 10.0),     # 1,000
    row(6, "S-Sale", 100, None),     # 금액 불가
    row(7, "S-Sale", 100, 0),        # 0 도 불가
])
r = ec.fetch_insider_90d("X", key="k", today=TODAY)
check("금액은 정상 건만", r["sale_val"] == 1000.0, f"got {r['sale_val']}")
check("건수는 전부", r["sale_n"] == 3, f"got {r['sale_n']}")
check("결측 건수 기록", r["price_missing"] == 2, f"got {r['price_missing']}")

# ══════════════════════════════════════════════════════════════════════════
print("\n[5] 결측 vs 0 — 섞이면 안 된다")
# ══════════════════════════════════════════════════════════════════════════
stub(None)
r_none = ec.fetch_insider_90d("X", key="k", today=TODAY)
check("조회 실패 → ok=False", r_none["ok"] is False)
check("조회 실패 → 금액 None (0 아님)", r_none["sale_val"] is None)

stub([])
r_empty = ec.fetch_insider_90d("X", key="k", today=TODAY)
check("빈 응답 → ok=False", r_empty["ok"] is False)

stub([row(10, "A-Award", 100, 10.0)])   # 행은 있으나 재량 0건
r_zero = ec.fetch_insider_90d("X", key="k", today=TODAY)
check("행은 있고 재량만 0 → ok=True", r_zero["ok"] is True)
check("이때는 금액이 진짜 0", r_zero["sale_val"] == 0.0)

# ══════════════════════════════════════════════════════════════════════════
print("\n[6] transactionDate 없는 행 — 창 판정 불가라 버린다")
# ══════════════════════════════════════════════════════════════════════════
stub([
    row(10, "S-Sale", 100, 10.0),
    row(11, "S-Sale", 100, 10.0, tdate=False),   # 날짜 없음 → 제외
])
r = ec.fetch_insider_90d("X", key="k", today=TODAY)
check("날짜 없는 행 제외", r["sale_val"] == 1000.0, f"got {r['sale_val']}")

ec._get = _ORIG_GET

# ══════════════════════════════════════════════════════════════════════════
print("\n[7] 열 정렬 — 기존 29열 행이 깨지면 안 된다")
# ══════════════════════════════════════════════════════════════════════════
check("PREVIEW_NCOL == 33", ec.PREVIEW_NCOL == 33, f"got {ec.PREVIEW_NCOL}")
check("내부자 4열이 맨 뒤",
      ec.PREVIEW_COLS[-4:] == ["Insider_Sale_Val_90d", "Insider_Sale_N_90d",
                               "Insider_Buy_Val_90d", "Insider_Cov_D"],
      f"got {ec.PREVIEW_COLS[-4:]}")
check("Data_Flags 는 28번째 그대로", ec.PREVIEW_COLS[27] == "Data_Flags",
      f"got {ec.PREVIEW_COLS[27]}")
check("Notes 는 29번째 그대로", ec.PREVIEW_COLS[28] == "Notes",
      f"got {ec.PREVIEW_COLS[28]}")

# 실제 시트에 있는 WMT 1행을 흉내낸다 — 29열짜리 옛 행
old29 = ["WMT_2026-08-20_D7", "WMT_2026-08-20", "WMT", "2026-08-20", "bmo",
         "D7", 6, "2026-08-14 22:00", 100.5, 4.2, -6.1,
         1.2, 3.0, 0.5, 1.7e11, 4.0, 110.0, 9.4,
         2.1, 75.0, 3.3, 60.0, -2.0, 8,
         3, "[]", "", "no_revision", ""]
parsed = ec.parse_preview([ec.PREVIEW_COLS, old29])
check("옛 29열 행 파싱 성공", len(parsed) == 1)
d0 = parsed[0]
check("Data_Flags 가 그대로 읽힌다", d0["Data_Flags"] == "no_revision",
      f"got {d0['Data_Flags']!r}")
check("Ticker 가 그대로 읽힌다", d0["Ticker"] == "WMT")
check("새 열은 공란", d0["Insider_Cov_D"] == "", f"got {d0['Insider_Cov_D']!r}")

# 변이 — 4열을 앞에 끼우면 정렬이 실제로 깨지는가
_savedcols = list(ec.PREVIEW_COLS)
_savedn = ec.PREVIEW_NCOL
ec.PREVIEW_COLS = _savedcols[:27] + _savedcols[-4:] + _savedcols[27:29]
ec.PREVIEW_NCOL = len(ec.PREVIEW_COLS)
d_mut = ec.parse_preview([ec.PREVIEW_COLS, old29])[0]
ec.PREVIEW_COLS, ec.PREVIEW_NCOL = _savedcols, _savedn
check("[변이] 중간 삽입하면 Data_Flags 가 깨진다(테스트가 살아 있음)",
      d_mut["Data_Flags"] != "no_revision",
      f"got {d_mut['Data_Flags']!r}")

# ══════════════════════════════════════════════════════════════════════════
print("\n[8] preview_row — 값이 올바른 자리에 들어가는가")
# ══════════════════════════════════════════════════════════════════════════
ev = {"ticker": "AAPL", "earnings_date": "2026-08-20",
      "days_until": 5, "timing": "amc"}
r_full = ec.preview_row(ev, "D7", {
    "price": 200.0, "ins_sale_val": 16028088.0, "ins_sale_n": 3,
    "ins_buy_val": 0.0, "ins_cov_d": 314, "flags": ["x"],
}, now_et="2026-08-15 17:00")
check("행 길이 33", len(r_full) == 33, f"got {len(r_full)}")
m = {c: r_full[i] for i, c in enumerate(ec.PREVIEW_COLS)}
check("매도 금액 위치", m["Insider_Sale_Val_90d"] == 16028088.0)
check("커버 일수 위치", m["Insider_Cov_D"] == 314)
check("매수 0 은 0 으로 저장", m["Insider_Buy_Val_90d"] == 0)
check("Data_Flags 자리 유지", m["Data_Flags"] == "x")

r_miss = ec.preview_row(ev, "D7", {"price": 200.0, "flags": ["no_insider"]},
                        now_et="2026-08-15 17:00")
m2 = {c: r_miss[i] for i, c in enumerate(ec.PREVIEW_COLS)}
check("조회 실패 시 공란(0 아님)", m2["Insider_Sale_Val_90d"] == "",
      f"got {m2['Insider_Sale_Val_90d']!r}")
check("커버 일수도 공란", m2["Insider_Cov_D"] == "")

# ══════════════════════════════════════════════════════════════════════════
print("\n[9] 백필 — 미래 정보 누출과 미완결 분기를 막는가")
# ══════════════════════════════════════════════════════════════════════════
try:
    import backfill_insider_stats as bf

    qs = [
        {"end": pd.Timestamp("2026-09-30"), "y": 2026, "q": 3,
         "sales": 1, "purch": 0},    # 발표일 이후 + 진행 중 → 금지
        {"end": pd.Timestamp("2026-06-30"), "y": 2026, "q": 2,
         "sales": 14, "purch": 0},   # 정답
        {"end": pd.Timestamp("2026-03-31"), "y": 2026, "q": 1,
         "sales": 0, "purch": 0},
        {"end": pd.Timestamp("2025-12-31"), "y": 2025, "q": 4,
         "sales": 15, "purch": 0},
        {"end": pd.Timestamp("2025-09-30"), "y": 2025, "q": 3,
         "sales": 20, "purch": 0},
    ]
    pick, ttm4 = bf.pick_closed_quarter(qs, "2026-08-20")
    check("발표일 이전 마감 분기 선택", pick is not None and pick["q"] == 2,
          f"got {pick}")
    check("미래 분기 미선택", pick is None or pick["end"] < pd.Timestamp("2026-08-20"))
    check("TTM4 = 4개 마감 분기 합", ttm4 == 14 + 0 + 15 + 20, f"got {ttm4}")

    pick_old, ttm_old = bf.pick_closed_quarter(qs, "2025-01-01")
    check("마감 분기가 없으면 None", pick_old is None)

    qs3 = qs[:3]
    _p, t3 = bf.pick_closed_quarter(qs3, "2026-08-20")
    check("4분기 미만이면 TTM4 는 None(부분합 금지)", t3 is None, f"got {t3}")
except Exception as e:
    check("백필 모듈 로드", False, str(e))

# ══════════════════════════════════════════════════════════════════════════
print("\n[10] 락스텝 자기점검 — 구버전 호출부 감지")
import contextlib as _ctx
import io as _io
_ev = {"ticker": "WMT", "earnings_date": "2026-08-20",
       "days_until": 3, "timing": "bmo"}


def _cap(metrics):
    b = _io.StringIO()
    with _ctx.redirect_stdout(b):
        r = ec.preview_row(_ev, "D3", metrics, now_et="2026-08-17 17:02")
    return r, b.getvalue()


# 스키마에는 내부자 열이 있는데 호출부가 값도 플래그도 안 주면 구버전이다.
# 2026-08-17 에 실제로 이 상태로 돌아 30~33열이 조용히 공란 저장됐다.
_r, _o = _cap({"price": 100.0, "flags": ["no_revision"]})
check("구버전 호출부에 FATAL 출력", "[FATAL] 락스텝 불일치" in _o)
_mm = {c: _r[i] for i, c in enumerate(ec.PREVIEW_COLS)}
check("stale_caller 플래그 기록", "stale_caller" in _mm["Data_Flags"],
      _mm["Data_Flags"])
check("기존 플래그 보존", "no_revision" in _mm["Data_Flags"])

_r2, _o2 = _cap({"price": 100.0, "ins_cov_d": 151, "ins_sale_val": 1.05e9,
                 "flags": []})
check("정상 호출부는 조용", "FATAL" not in _o2)

# 조회 실패(no_insider)는 구버전이 아니다 — 오탐이면 매번 FATAL 이 떠서 무뎌진다
_r3, _o3 = _cap({"price": 100.0, "flags": ["no_insider"]})
check("조회 실패는 오탐 아님", "FATAL" not in _o3)

print("\n" + "=" * 70)
if _fail:
    print(f"❌ 실패 {len(_fail)}건 / 통과 {_pass}건")
    for n in _fail:
        print(f"   · {n}")
    sys.exit(1)
print(f"✅ 전체 통과 — {_pass}건")
sys.exit(0)
