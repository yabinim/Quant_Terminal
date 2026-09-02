# -*- coding: utf-8 -*-
"""diag_rotation_policy.py — Hidden Alpha 로테이션 게이트 회귀 스위트.

검증 대상: fmp_extras 의 레버리지 판별과 rotation_core 의 **실제 함수**.
로직을 복사하지 않는다 — 복사하면 두 벌이 되고, 둘이 어긋나도 초록불이 뜬다.

불변식:
  I1  레버리지/인버스는 배수 종류와 무관하게 걸린다 (열거가 아니라 일반형 캡처).
  I2  이름이 없으면 이름 정규식은 아무것도 주장하지 않는다 — 그래서 이름 배관
      (etf-list → profile.companyName)이 필수다. 이 사실을 테스트가 못 박는다.
  I3  '모르면 제외' — 유동성·AUM 은 None 에서 False. 0.0 과 None 을 구분한다.
  I4  '모르면 통과' — 상관 판정 불가(공통 관측일 부족)는 통과. 크립토 분류
      미상도 통과. 위험 크기 판정과 분류 판정은 규칙이 다르다.
  I5  게이트에 걸린 종목은 슬롯을 비우지 않고 **다음 순위가 승계**한다.
  I6  크립토 캡은 정확히 CRYPTO_SLOT_CAP 개까지만 통과시킨다.
  I7  상관 제거는 점수 높은 쪽을 남긴다 (입력 순서 의존 — 정렬 책임은 호출부).
  I8  **소비자 배선** — app.py 와 run_hidden_alpha.py 가 둘 다 rotation_core 를
      실제로 호출하고, 임계값·슬롯·창 깊이를 로컬에 다시 쓰지 않는다.
      순수 함수만 검사하면 "게이트는 완벽한데 아무도 안 부른다"를 못 잡는다 —
      2026-09-02 에 app.py 버튼 경로가 정확히 그 상태였다.

⚠️ 양성 대조(G)는 **알려진 불량 입력**에 대해 진단이 실제로 실패하는지 본다.
   초록불이 옳은 이유로 켜졌는지 확인하지 않으면 초록불은 정보가 아니다.

사용법:  python3 automation/diag_rotation_policy.py
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np                # noqa: E402
import pandas as pd               # noqa: E402

import fmp_extras as fx           # noqa: E402
import rotation_core as rc        # noqa: E402

PASS, FAIL = [], []


def chk(name, got, exp):
    (PASS if got == exp else FAIL).append(
        name if got == exp else (name, exp, got))


def _series(vals, start="2026-01-01"):
    idx = pd.bdate_range(start=start, periods=len(vals))
    return pd.Series(vals, index=idx, dtype=float)


# ══════════════════════════════════════════════════════════════════════════════
# A. 레버리지/인버스 판별 (I1, I2)
# ══════════════════════════════════════════════════════════════════════════════
_LEV_CASES = [
    # (이름, 기대 제외 여부, 설명)
    ("T-REX 2X Long SPCX Daily Target ETF", True,  "A-1 2026-08-29 사고 당사자"),
    ("Direxion Daily 4X Long Something",    True,  "A-2 옛 열거가 몰랐던 4X"),
    ("Leverage Shares 5X Long NVDA",        True,  "A-3 옛 열거가 몰랐던 5X"),
    ("Defiance Daily Target 1.75X Long",    True,  "A-4 소수 배수"),
    ("21Shares 2x Long HYPE ETF",           True,  "A-5 소문자 x"),
    ("ProShares UltraShort S&P500",         True,  "A-6 인버스(단어형)"),
    ("ProShares UltraPro QQQ",              True,  "A-7 3배(단어형)"),
    ("Some Leveraged Bond Fund",            True,  "A-8 배수 미상·의심 키워드"),
    ("Invesco QQQ Trust",                   False, "A-9 일반 ETF"),
    ("SPDR S&P 500 ETF Trust",              False, "A-10 일반 ETF"),
    ("iShares 1-3 Year Short-Term Treasury Bond ETF", False, "A-11 SHORT 오탐 방지"),
    ("21Shares Hyperliquid Staking ETF",    False, "A-12 HYPE 현물은 레버리지 아님"),
    ("Bitwise Hyperliquid ETF",             False, "A-13 동상"),
    ("Grayscale Hyperliquid Staking ETF",   False, "A-14 동상"),
    ("iShares MSCI Mexico 2026X",           False, "A-15 연도 꼬리를 배수로 오독 금지"),
    ("Global X Artificial Intelligence UCITS", False, "A-16 일반 ETF"),
]
for _nm, _exp, _desc in _LEV_CASES:
    chk(f"{_desc}", fx.is_rotation_excluded("TEST", _nm), _exp)

# I2 — 이름이 없으면 판정 불가. 이건 결함이 아니라 명시된 한계다.
#      A-1(이름 배관)이 존재해야 하는 이유를 테스트가 박아둔다.
chk("A-17 이름 없으면 티커만으로는 못 잡는다(한계 명시)",
    fx.is_rotation_excluded("SPAX", ""), False)
# 다만 티커 매핑에 있는 것은 이름 없이도 잡힌다.
chk("A-18 매핑된 티커는 이름 없이도 제외", fx.is_rotation_excluded("SQQQ", ""), True)

# M15 대응 — 배수 뒤 단어경계(\b)가 하는 일을 실제로 가르는 케이스.
#   '2XU' 는 의류 브랜드다. 경계를 빼면 '2X' 로 잘려 배수 2 로 오독한다.
#   앞선 A-15(2026X)는 범위검사(1<v<=20)가 대신 막아서 경계를 검증하지 못했다.
chk("A-19 배수 뒤 단어경계 — '2XU' 를 2배로 오독하지 않는다",
    fx.is_rotation_excluded("TEST", "2XU Compression Apparel Fund"), False)

# M16 대응 — 배수 '크기'까지 본다. 제외 여부만 보면 인버스 경로·3배 경로가
#   2배 단어형의 구멍을 가린다 (UltraShort→SHORT, UltraPro→ULTRAPRO).
chk("A-20 'Ultra' 단독은 2배", fx.get_leverage_multiplier("TEST", "ProShares Ultra QQQ"), 2.0)
chk("A-21 'UltraShort' 는 -2배 (인버스 -1 로 뭉개지 않는다)",
    fx.get_leverage_multiplier("TEST", "ProShares UltraShort S&P500"), -2.0)
chk("A-22 'UltraPro' 는 3배",
    fx.get_leverage_multiplier("TEST", "ProShares UltraPro QQQ"), 3.0)
chk("A-23 'Double' 은 2배",
    fx.get_leverage_multiplier("TEST", "Double Long Gold Fund"), 2.0)

# ══════════════════════════════════════════════════════════════════════════════
# B. 유동성 (I3)
# ══════════════════════════════════════════════════════════════════════════════
_c = _series([10.0] * 25)
_v = _series([1_000_000.0] * 25)          # 10 × 1M = $10M/일
chk("B-1 달러거래대금 계산", round(rc.avg_dollar_volume(_c, _v) / 1e6, 3), 10.0)
chk("B-2 임계 통과", rc.passes_liquidity(rc.avg_dollar_volume(_c, _v)), True)

_v_low = _series([100_000.0] * 25)        # 10 × 100k = $1M/일 < $3M
chk("B-3 임계 미달 차단", rc.passes_liquidity(rc.avg_dollar_volume(_c, _v_low)), False)

chk("B-4 거래량 없음 → 판정 불가(None)", rc.avg_dollar_volume(_c, pd.Series(dtype=float)), None)
chk("B-5 판정 불가 → 제외 ('모르면 안 산다')", rc.passes_liquidity(None), False)
chk("B-6 0.0 은 None 과 다르다 — 관측된 0 도 제외",
    (rc.avg_dollar_volume(_c, _series([0.0] * 25)), rc.passes_liquidity(0.0)), (0.0, False))
chk("B-7 NaN 방어", rc.passes_liquidity(float("nan")), False)

# 날짜 정렬 — 길이만 맞추면 다른 날 종가×거래량을 곱하는 조용한 오류가 난다.
_c_off = _series([10.0] * 25, start="2026-01-01")
_v_off = _series([1_000_000.0] * 25, start="2026-03-02")   # 겹치는 날 없음
chk("B-8 인덱스 교집합 없음 → None (길이 맞추기 금지)",
    rc.avg_dollar_volume(_c_off, _v_off), None)

# 창 경계: 최근 20일만 본다.
_c_mix = _series([10.0] * 40)
_v_mix = _series([100_000.0] * 20 + [1_000_000.0] * 20)   # 앞 저조·뒤 활발
chk("B-9 최근 20일만 평균", round(rc.avg_dollar_volume(_c_mix, _v_mix) / 1e6, 3), 10.0)

# ══════════════════════════════════════════════════════════════════════════════
# C. AUM (I3) — falsy 통과 결함의 회귀 방지
# ══════════════════════════════════════════════════════════════════════════════
chk("C-1 충분한 AUM 통과", rc.passes_aum(120.0), True)
chk("C-2 미달 차단", rc.passes_aum(10.0), False)
chk("C-3 None → 제외", rc.passes_aum(None), False)
chk("C-4 0 → 제외 (옛 `if aum and aum < MIN` 은 여기서 통과시켰다)",
    rc.passes_aum(0), False)
chk("C-5 빈 문자열 → 제외", rc.passes_aum(""), False)
chk("C-6 경계값 정확히 임계 → 통과", rc.passes_aum(rc.MIN_AUM_M), True)
# M3 대응 — 임계가 0 이어도 '순자산 0' 은 매수 대상이 아니다.
#   최종 비교(a >= minimum)만으로는 이 경우를 못 막는다. 명시 가드가 필요하다.
chk("C-7 임계 0 이어도 AUM 0 은 제외", rc.passes_aum(0.0, 0.0), False)
chk("C-8 임계 0 이어도 음수는 제외", rc.passes_aum(-5.0, 0.0), False)
chk("C-9 임계 0 · 양수는 통과", rc.passes_aum(1.0, 0.0), True)

# ══════════════════════════════════════════════════════════════════════════════
# D. 크립토 판정 (I4)
# ══════════════════════════════════════════════════════════════════════════════
chk("D-1 HYPE 현물", rc.is_crypto("THYP", "21Shares Hyperliquid Staking ETF"), True)
chk("D-2 비트코인", rc.is_crypto("XXX", "iShares Bitcoin Trust"), True)
chk("D-3 솔라나", rc.is_crypto("MSOL", "Bitwise Solana Staking ETF"), True)
chk("D-4 SOLAR 오탐 금지", rc.is_crypto("TAN", "Invesco Solar ETF"), False)
chk("D-5 섹터 보조 판정", rc.is_crypto("ZZZ", "", "Digital Assets"), True)
chk("D-6 이름·섹터 둘 다 미상 → 비크립토('모르면 통과')", rc.is_crypto("ZZZ", "", ""), False)
chk("D-7 블록체인", rc.is_crypto("BKCH", "Global X Blockchain ETF"), True)
chk("D-8 일반 ETF", rc.is_crypto("QQQ", "Invesco QQQ Trust"), False)

# ══════════════════════════════════════════════════════════════════════════════
# E. 상관 중복 제거 (I4, I7)
# ══════════════════════════════════════════════════════════════════════════════
_rng = np.random.default_rng(20260901)
_n = 60
_base = _rng.normal(0, 0.02, _n)
_idx = pd.bdate_range("2026-01-01", periods=_n)
_rets = pd.DataFrame({
    "AAA": _base,                                   # 기준
    "BBB": _base + _rng.normal(0, 0.0004, _n),      # 사실상 같은 자산 (ρ≈0.999)
    "CCC": _base + _rng.normal(0, 0.0004, _n),      # 사실상 같은 자산
    "DDD": _rng.normal(0, 0.02, _n),                # 무관
}, index=_idx)

_kept, _dropped = rc.dedup_by_correlation(["AAA", "BBB", "CCC", "DDD"], _rets)
chk("E-1 래퍼 3형제 중 1개만 잔류", _kept, ["AAA", "DDD"])
chk("E-2 드롭 사유에 기준 티커 기록", sorted(_dropped.keys()), ["BBB", "CCC"])
chk("E-3 드롭된 쪽은 AAA 를 가리킨다",
    {k: v[0] for k, v in _dropped.items()}, {"BBB": "AAA", "CCC": "AAA"})

# I7 — 입력 순서를 뒤집으면 남는 쪽이 바뀐다. 정렬 책임이 호출부에 있다는 계약.
_kept_rev, _ = rc.dedup_by_correlation(["CCC", "BBB", "AAA", "DDD"], _rets)
chk("E-4 순서 의존 — 먼저 온 쪽이 남는다", _kept_rev, ["CCC", "DDD"])

# I4 — 공통 관측일 부족은 '판정 불가' → 통과
_short = pd.DataFrame({
    "AAA": _base,
    "EEE": [np.nan] * (_n - 10) + list(_base[-10:]),   # 겹치는 날 10개뿐
}, index=_idx)
_k2, _d2 = rc.dedup_by_correlation(["AAA", "EEE"], _short)
chk("E-5 공통 관측일 부족 → 통과 (신규 상장 보호)", _k2, ["AAA", "EEE"])
chk("E-6 그때 드롭 기록 없음", _d2, {})

chk("E-7 수익률 프레임 자체가 비면 전원 통과",
    rc.dedup_by_correlation(["AAA", "BBB"], pd.DataFrame())[0], ["AAA", "BBB"])
chk("E-8 빈 입력", rc.dedup_by_correlation([], _rets), ([], {}))

# ══════════════════════════════════════════════════════════════════════════════
# F. 크립토 캡 · 슬롯 승계 (I5, I6)
# ══════════════════════════════════════════════════════════════════════════════
_order = ["C1", "C2", "C3", "N1", "N2", "N3"]
_cmap = {"C1": True, "C2": True, "C3": True, "N1": False, "N2": False, "N3": False}
_sel, _skip = rc.select_top_slots(_order, _cmap, slots=5, cap=2)
chk("F-1 크립토 2개까지만", _sel, ["C1", "C2", "N1", "N2", "N3"])
chk("F-2 초과분은 스킵 기록", _skip, {"C3": "crypto_cap"})
chk("F-3 슬롯은 비지 않는다 (다음 순위 승계)", len(_sel), 5)

chk("F-4 크립토 전무하면 캡은 무해",
    rc.select_top_slots(["A", "B", "C", "D", "E", "F"], {}, slots=5, cap=2)[0],
    ["A", "B", "C", "D", "E", "F"][:5])
chk("F-5 전부 크립토면 캡 개수만 선정",
    rc.select_top_slots(["C1", "C2", "C3"], {"C1": 1, "C2": 1, "C3": 1}, slots=5, cap=2)[0],
    ["C1", "C2"])
chk("F-6 후보가 슬롯보다 적으면 있는 만큼",
    rc.select_top_slots(["A", "B"], {}, slots=5, cap=2)[0], ["A", "B"])
chk("F-7 캡 상수는 2 (2026-09-01 확정)", rc.CRYPTO_SLOT_CAP, 2)

# ══════════════════════════════════════════════════════════════════════════════
# G. 통합 — 2026-08-29 실제 사고 재현 (I5)
# ══════════════════════════════════════════════════════════════════════════════
# 그 주 Top 5: SPAX(2x 레버리지) · THYP · HYPG · AIQU · BHYP (HYPE 3형제)
_n2 = 60
_hype = _rng.normal(0.006, 0.02, _n2)          # HYPE 공통 성분
_idx2 = pd.bdate_range("2026-06-06", periods=_n2)


def _px(rets):
    return pd.Series(100.0 * np.cumprod(1.0 + np.asarray(rets)), index=_idx2)


_close = pd.DataFrame({
    "SPAX": _px(2.0 * _hype),
    "THYP": _px(_hype + _rng.normal(0, 0.0004, _n2)),
    "HYPG": _px(_hype + _rng.normal(0, 0.0004, _n2)),
    "BHYP": _px(_hype + _rng.normal(0, 0.0004, _n2)),
    "AIQU": _px(_rng.normal(0.004, 0.015, _n2)),
    "KMCA": _px(_rng.normal(0.003, 0.015, _n2)),   # 유동성 미달
    "WCLD": _px(_rng.normal(0.002, 0.015, _n2)),
    "SPY":  _px(_rng.normal(0.001, 0.008, _n2)),
})
_big = pd.DataFrame({t: pd.Series([5_000_000.0] * _n2, index=_idx2) for t in _close.columns})
_big["KMCA"] = pd.Series([1_000.0] * _n2, index=_idx2)     # 거래대금 $0.1M 수준

_ranked = pd.DataFrame({
    "rank": range(1, 9),
    "Ticker": ["SPAX", "THYP", "HYPG", "AIQU", "BHYP", "KMCA", "WCLD", "SPY"],
})
_meta = {
    "SPAX": {"name": "T-REX 2X Long SPCX Daily Target ETF", "aum_m": 42.0,
             "leveraged": fx.is_rotation_excluded("SPAX", "T-REX 2X Long SPCX Daily Target ETF")},
    "THYP": {"name": "21Shares Hyperliquid Staking ETF", "aum_m": 300.0, "leveraged": False},
    "HYPG": {"name": "Grayscale Hyperliquid Staking ETF", "aum_m": 200.0, "leveraged": False},
    "BHYP": {"name": "Bitwise Hyperliquid ETF", "aum_m": 150.0, "leveraged": False},
    "AIQU": {"name": "Global X Artificial Intelligence UCITS", "aum_m": 800.0, "leveraged": False},
    "KMCA": {"name": "Some Small Cap ETF", "aum_m": 90.0, "leveraged": False},
    "WCLD": {"name": "WisdomTree Cloud Computing Fund", "aum_m": 400.0, "leveraged": False},
    "SPY":  {"name": "SPDR S&P 500 ETF Trust", "aum_m": 500_000.0, "leveraged": False},
}
_g = rc.apply_rotation_gates(_ranked, close_df=_close, volume_df=_big, meta=_meta, slots=5)

chk("G-1 SPAX 는 레버리지로 제외", _g["excluded"].get("SPAX"), "leverage")
chk("G-2 KMCA 는 거래대금으로 제외", _g["excluded"].get("KMCA"), "liquidity")
chk("G-3 HYPE 래퍼는 1개만 남는다",
    len([t for t in _g["selected"] if t in ("THYP", "HYPG", "BHYP")]), 1)
chk("G-4 HYPE 중복 사유가 기록된다",
    sorted([t for t, w in _g["excluded"].items() if w == "duplicate"]), ["BHYP", "HYPG"])
chk("G-5 최종 슬롯에 SPAX·KMCA 없음",
    ("SPAX" in _g["selected"]) or ("KMCA" in _g["selected"]), False)
chk("G-6 크립토는 캡 이하", sum(1 for t in _g["selected"] if _g["crypto"].get(t)) <= 2, True)
chk("G-7 남은 후보로 슬롯을 최대한 채운다", _g["selected"], ["THYP", "AIQU", "WCLD", "SPY"])

# AUM 게이트 통합
_meta_lowaum = dict(_meta)
_meta_lowaum["WCLD"] = {**_meta["WCLD"], "aum_m": None}
_g2 = rc.apply_rotation_gates(_ranked, close_df=_close, volume_df=_big,
                              meta=_meta_lowaum, slots=5)
chk("G-8 AUM 미상 → 제외", _g2["excluded"].get("WCLD"), "aum")

chk("G-9 빈 랭킹은 조용히 빈 결과", rc.apply_rotation_gates(pd.DataFrame())["selected"], [])
chk("G-10 거래량 프레임 없음 → 전원 유동성 제외 ('모르면 안 산다')",
    rc.apply_rotation_gates(_ranked, close_df=_close, volume_df=None,
                            meta=_meta, slots=5)["selected"], [])

# ══════════════════════════════════════════════════════════════════════════════
# H. 양성 대조 — 알려진 불량 입력에서 진단이 실제로 실패하는가
# ══════════════════════════════════════════════════════════════════════════════
# 진단이 "옳은 이유로" 통과했는지 본다. 아래 주장이 참이면 위 검사들은
# 무력하다는 뜻이므로 즉시 실패로 뒤집는다.
chk("H-1 [양성대조] 임계 미달이 통과로 뒤집히지 않았는가",
    rc.passes_liquidity(rc.MIN_DOLLAR_VOLUME - 1.0), False)
chk("H-2 [양성대조] 캡을 0 으로 주면 크립토가 전부 빠지는가",
    rc.select_top_slots(["C1", "N1"], {"C1": True}, slots=5, cap=0)[0], ["N1"])
chk("H-3 [양성대조] 임계 1.0 이면 무관 종목도 안 걸리는가",
    rc.dedup_by_correlation(["AAA", "DDD"], _rets, threshold=1.0)[0], ["AAA", "DDD"])
chk("H-4 [양성대조] 임계 0.0 이면 전부 중복 처리되는가",
    rc.dedup_by_correlation(["AAA", "BBB", "CCC", "DDD"], _rets, threshold=0.0)[0], ["AAA"])
chk("H-5 [양성대조] REQUIRED_BARS 가 상관 요구를 실제로 덮는가",
    rc.REQUIRED_BARS >= rc.CORR_LOOKBACK + 1, True)
chk("H-6 [양성대조] REQUIRED_BARS 가 1개월 수익률 요구(22봉)를 덮는가",
    rc.REQUIRED_BARS >= 22, True)


# ══════════════════════════════════════════════════════════════════════════════
# W. 소비자 배선 (정적 분석 + 스텁 실행)
# ══════════════════════════════════════════════════════════════════════════════
# 왜 필요한가: A~H 는 rotation_core 의 순수 함수만 본다. 그 함수들이 아무리
# 옳아도 **호출부가 안 부르면** 화면은 게이트 없는 순위표다. 2026-09-02 에
# app.py 의 「지금 돈이 몰리는 미지의 ETF 찾기」 버튼이 정확히 그랬고, 이 스위트는
# 초록불이었다. 정적 분석으로 배선을 못 박고, 함수 본문은 스텁으로 실제 실행한다.
_APP = os.path.join(_ROOT, "app.py")
_RHA = os.path.join(_ROOT, "run_hidden_alpha.py")
_RHA_ALT = os.path.join(_ROOT, "automation", "run_hidden_alpha.py")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _alias_of(src_text, module):
    """`import <module> as X` 의 X. 없으면 None. (from-import 는 별칭 개념이 없어 제외)"""
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return None
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == module:
                    return a.asname or a.name
    return None


def _calls_attr(src_text, alias, attr):
    """`alias.attr(...)` 호출이 있는가. 문자열 검색이 아니라 AST 다 —
    주석·문서열의 같은 글자에 속지 않는다."""
    if not alias:
        return False
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return False
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == alias):
            return True
    return False


def _kwarg_src(src_text, alias, attr, kw):
    """`alias.attr(..., kw=<expr>)` 의 <expr> 소스. 없으면 None."""
    if not alias:
        return None
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == alias):
            for k in n.keywords:
                if k.arg == kw:
                    return ast.unparse(k.value)
    return None


def _module_level_names(src_text):
    """모듈 최상위에 **대입된** 이름 집합."""
    out = set()
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return out
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(n, (ast.AnnAssign,)) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
    return out


def _func_node(src_text, name):
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return None
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


_app_src = _read(_APP)
_rha_src = _read(_RHA) or _read(_RHA_ALT)

# 사전 확정 임계값 — 호출부가 로컬에 다시 쓰면 두 벌이 되고 조용히 갈라진다.
_LOCKED = ("MIN_DOLLAR_VOLUME", "MIN_AUM_M", "CORR_THRESHOLD",
           "CORR_LOOKBACK", "HOLD_SLOTS", "CRYPTO_SLOT_CAP", "REQUIRED_BARS")

chk("W-0a app.py 사본 존재", _app_src is not None, True)
chk("W-0b run_hidden_alpha.py 사본 존재", _rha_src is not None, True)

if _app_src and _rha_src:
    _app_alias = _alias_of(_app_src, "rotation_core")
    _rha_alias = _alias_of(_rha_src, "rotation_core")

    chk("W-1 app.py 가 rotation_core 를 import 한다", _app_alias is not None, True)
    chk("W-2 run_hidden_alpha 가 rotation_core 를 import 한다", _rha_alias is not None, True)

    # 별칭 충돌: app.py 의 rc 는 regime_core 다. rotation_core 를 rc 로 두면
    # 나중에 rc.classify_regime 이 AttributeError 로 죽는다.
    chk("W-3 app.py 의 rotation_core 별칭이 regime_core 별칭과 다르다",
        (_app_alias is not None
         and _app_alias != _alias_of(_app_src, "regime_core")), True)

    chk("W-4 app.py 가 apply_rotation_gates 를 실제 호출한다",
        _calls_attr(_app_src, _app_alias, "apply_rotation_gates"), True)
    chk("W-5 run_hidden_alpha 가 apply_rotation_gates 를 실제 호출한다",
        _calls_attr(_rha_src, _rha_alias, "apply_rotation_gates"), True)

    # 슬롯 수를 리터럴로 쓰면 rotation_core 와 갈라진다.
    _slots_app = _kwarg_src(_app_src, _app_alias, "apply_rotation_gates", "slots")
    chk("W-6 app.py 의 slots 인자가 rotation_core 를 참조한다 (리터럴 금지)",
        bool(_slots_app) and _app_alias in str(_slots_app), True)

    # 창 깊이도 마찬가지 — REQUIRED_BARS 가 소유한다.
    chk("W-7 app.py 가 창 깊이를 REQUIRED_BARS 로 잡는다",
        f"{_app_alias}.REQUIRED_BARS" in _app_src, True)

    # 제외 사유를 코드 그대로 노출하면 사용자가 못 읽는다.
    chk("W-8 app.py 가 reason_label 로 사유를 번역한다",
        _calls_attr(_app_src, _app_alias, "reason_label"), True)

    # 임계값 로컬 재정의 금지 (run_hidden_alpha 의 `_HOLD_SLOTS = rc.HOLD_SLOTS`
    # 같은 위임 대입은 이름이 달라 걸리지 않는다 — 금지 대상은 동명 재정의다)
    for _f, _s, _lbl in ((_APP, _app_src, "app.py"), (_RHA, _rha_src, "run_hidden_alpha")):
        _dup = sorted(_module_level_names(_s) & set(_LOCKED))
        chk(f"W-9 {_lbl} 가 사전확정 임계값을 로컬 재정의하지 않는다", _dup, [])

    # 랭킹 표는 전원 유지 — 랭킹 캐시 안에서 게이트를 걸면 다른 소비자
    # (build_pool_monthly_returns_table)의 목록이 조용히 줄어든다.
    _rank_fn = _func_node(_app_src, "cached_etf_universe_rankings_full")
    chk("W-10 랭킹 캐시 함수가 존재한다", _rank_fn is not None, True)
    if _rank_fn is not None:
        _inside = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "apply_rotation_gates"
            for n in ast.walk(_rank_fn)
        )
        chk("W-11 랭킹 캐시는 게이트로 걸러내지 않는다 (표는 전원 유지)", _inside, False)

    # 버튼 경로가 게이트 함수를 실제로 부르는가
    chk("W-12 버튼 경로가 cached_hidden_alpha_gates 를 호출한다",
        any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "cached_hidden_alpha_gates"
            for n in ast.walk(ast.parse(_app_src))), True)

# ══════════════════════════════════════════════════════════════════════════════
# W-R. app.py 게이트 함수 **본문 실행** (네트워크 0 · 스텁)
# ══════════════════════════════════════════════════════════════════════════════
# 정적 분석은 "부르긴 부른다"까지만 증명한다. 넘기는 자료의 모양이 틀리면
# (거래량 프레임이 비어 있다든지, 메타 키 이름이 다르다든지) 게이트는 조용히
# 전원 제외를 돌려주고 화면은 "슬롯 0" 이 된다. 그래서 함수 본문을 떼어내
# 스텁 위에서 실제로 돌린다. app.py 전체를 import 하지 않는 이유는 Streamlit
# 의존 때문이다 — 필요한 것은 함수 하나뿐이다.
_gatefn = _func_node(_app_src, "cached_hidden_alpha_gates") if _app_src else None
chk("W-R0 게이트 함수가 app.py 에 정의돼 있다", _gatefn is not None, True)

if _gatefn is not None:
    _gatefn.decorator_list = []          # st.cache_data 제거
    _mod = ast.Module(body=[_gatefn], type_ignores=[])
    ast.fix_missing_locations(_mod)

    _px = pd.DataFrame(
        {"d": pd.date_range("2026-01-01", periods=90, freq="B")}).set_index("d")

    def _mk(seed, n=90):
        rng = np.random.default_rng(seed)
        return pd.Series(100 + np.cumsum(rng.normal(0, 1, n)),
                         index=_px.index[:n])

    _base = _mk(1)
    _SERIES = {
        "AAA": _base,
        "BBB": _base * 1.5 + 0.001,      # AAA 와 ρ≈1 → 중복
        "CCC": _mk(7),
        "DDD": _mk(9),
        "EEE": _mk(11),
        "FFF": _mk(13),
    }
    _BIGVOL = pd.Series(5_000_000.0, index=_px.index[:90])   # 종가×거래량 ≫ 임계
    _TINYVOL = pd.Series(1.0, index=_px.index[:90])

    def _make_ns(*, vol_for=None, profiles=None, names=None, raise_batch=False,
                 tickers=None):
        tickers = tickers or list(_SERIES)
        vol_for = list(_SERIES) if vol_for is None else vol_for

        def _batch(tks, *, lookback_days):
            if raise_batch:
                raise RuntimeError("네트워크 실패 시뮬레이션")
            out = {}
            for t in tks:
                if t not in _SERIES:
                    continue
                d = pd.DataFrame({"Close": _SERIES[t]})
                if t in vol_for:
                    d["Volume"] = (_BIGVOL if vol_for is not True else _BIGVOL)
                out[t] = d
            return out

        def _prof(t):
            return (profiles or {}).get(t, {"companyName": t + " ETF",
                                            "sector": "Tech",
                                            "totalAssets": 500_000_000})

        return {
            "pd": pd, "np": np, "fx": fx, "rot": rc,
            "_HA_VERIFY_TOP_N": 15,
            "_fmp_batch_price_history": _batch,
            "_fmp_etf_symbol_name_map": lambda: (names or {}),
            "_fmp_profile": _prof,
        }

    def _run(ns, order):
        exec(compile(_mod, "<app_gate>", "exec"), ns)          # noqa: S102
        return ns["cached_hidden_alpha_gates"](tuple(order))

    # ⚠️ 알파벳 순이 **아니어야** 한다. 정렬된 픽스처를 쓰면 "호출부가 순서를
    #    다시 정렬한다"는 변이가 무동작이 되어 W-R3 가 통과해 버린다(실측).
    _ORDER = ["EEE", "AAA", "BBB", "CCC", "DDD", "FFF"]

    # 정상 경로 — AAA/BBB 는 ρ≈1 이라 하나만 남고 나머지가 승계한다.
    _g = _run(_make_ns(), _ORDER)
    chk("W-R1 정상 입력에서 슬롯이 찬다", len(_g["selected"]), rc.HOLD_SLOTS)
    chk("W-R2 상관 중복이 실제로 제거된다", _g["excluded"].get("BBB"), "duplicate")
    chk("W-R3 입력 순서(=랭킹)를 재정렬하지 않는다", _g["selected"][0], "EEE")

    # 거래량이 없는 티커 → '판정 불가' → 제외 (모르면 안 산다)
    _g = _run(_make_ns(vol_for=["AAA", "CCC", "DDD", "EEE", "FFF"]), _ORDER)
    chk("W-R4 거래량 없는 티커는 유동성 제외", _g["excluded"].get("BBB"), "liquidity")

    # AUM 미상 → 제외
    _g = _run(_make_ns(profiles={"CCC": {"companyName": "CCC ETF", "sector": "Tech"}}),
              _ORDER)
    chk("W-R5 AUM 미상은 제외", _g["excluded"].get("CCC"), "aum")

    # 레버리지 — 이름은 profile.companyName 으로 처음 채워진다 (SPAX 경로)
    # names 에 **무해한 이름**을 미리 넣는다. 비워 두면 `if cn and not nm` 같은
    # 변이(= profile 이름이 시트 이름을 못 이김)가 보이지 않는다(실측). 이 경로가
    # SPAX 가 뚫렸던 자리이므로 '이긴다'를 반드시 못 박아야 한다.
    _g = _run(_make_ns(names={"DDD": "Boring Broad Index Fund"},
                       profiles={"DDD": {"companyName": "Direxion Daily 2X Shares",
                                         "sector": "Tech",
                                         "totalAssets": 500_000_000}}), _ORDER)
    chk("W-R6 profile 이름이 시트 이름을 이기고 레버리지가 잡힌다",
        _g["excluded"].get("DDD"), "leverage")

    # 배치 실패 → 크래시 없이 전원 유동성 제외
    _g = _run(_make_ns(raise_batch=True), _ORDER)
    chk("W-R7 가격 조회 실패 시 크래시하지 않는다", isinstance(_g, dict), True)
    chk("W-R8 가격 조회 실패 시 슬롯을 채우지 않는다", _g["selected"], [])

    # 빈 입력
    chk("W-R9 빈 입력은 조용히 빈 결과", _run(_make_ns(), [])["selected"], [])

    # 상위 N 개만 판정한다
    _ns = _make_ns()
    _ns["_HA_VERIFY_TOP_N"] = 2
    _g = _run(_ns, _ORDER)
    chk("W-R10 상위 N 개만 판정 대상에 들어간다",
        sorted(set(_g["selected"]) | set(_g["excluded"])), ["AAA", "EEE"])

    # meta 를 함께 돌려준다 (화면이 이름·AUM 을 쓴다)
    chk("W-R11 meta 를 함께 반환한다",
        bool(_run(_make_ns(), _ORDER).get("meta")), True)

# ── 양성 대조: 위 AST 헬퍼가 알려진 불량 소스에서 실제로 실패하는가 ──────────
_BAD = "import rotation_core as rot\nx = rot.something_else()\nMIN_AUM_M = 1.0\n"
chk("W-P1 [양성대조] 호출이 없으면 _calls_attr 가 False 를 낸다",
    _calls_attr(_BAD, "rot", "apply_rotation_gates"), False)
chk("W-P2 [양성대조] 로컬 재정의를 실제로 잡는다",
    sorted(_module_level_names(_BAD) & set(_LOCKED)), ["MIN_AUM_M"])
chk("W-P3 [양성대조] 없는 모듈에는 별칭이 안 잡힌다",
    _alias_of(_BAD, "regime_core"), None)
chk("W-P4 [양성대조] 문서열 속 글자에 속지 않는다",
    _calls_attr('"""rot.apply_rotation_gates(x)"""\n', "rot", "apply_rotation_gates"),
    False)

# ══════════════════════════════════════════════════════════════════════════════
print("=" * 74)
print(f"diag_rotation_policy — 통과 {len(PASS)} / 실패 {len(FAIL)}  (총 {len(PASS) + len(FAIL)})")
print("=" * 74)
if FAIL:
    for item in FAIL:
        name, exp, got = item
        print(f"  ❌ {name}\n       기대: {exp!r}\n       실제: {got!r}")
    sys.exit(1)
print("  ✅ 전 항목 통과")
