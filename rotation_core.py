"""rotation_core.py — Hidden Alpha 로테이션 게이트 SSOT

app.py 와 automation/run_hidden_alpha.py 가 공유한다.
이 모듈은 **순수 로직만** 담는다 — streamlit·gspread·requests 를 import 하지 않는다.
네트워크 조회는 호출부가 하고, 여기에는 판정만 들어온다. (테스트 가능성 불변식)

━━ 왜 이 모듈이 생겼나 (2026-09-01) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2026-08-29 주간 이메일이 Top 5 로 다음을 내보냈다:

    1 SPAX  (T-REX **2X Long** SPCX Daily Target ETF — 레버리지)
    2 THYP  (21Shares Hyperliquid  — HYPE 현물)
    3 HYPG  (Grayscale  Hyperliquid — HYPE 현물)
    4 AIQU
    5 BHYP  (Bitwise    Hyperliquid — HYPE 현물)

세 가지 서로 다른 고장이 동시에 드러났다:

  ① 레버리지가 뚫렸다. `fmp_extras.is_rotation_excluded()` 는 티커 매핑과 **이름
     정규식** 두 경로뿐인데, run_hidden_alpha 가 이름을 빈 문자열로 넘겨서
     정규식이 볼 문자열이 없었다. → 이 모듈이 아니라 호출부/fmp_extras 에서 수정.

  ② 같은 기초자산의 래퍼 3개가 슬롯 3개를 먹었다. THYP·BHYP·HYPG 는 전부 HYPE
     현물이고 1개월 수익률이 44.33 / 44.06 / 44.21 로 사실상 동일했다.
     점수가 수익률 백분위만 보므로 **래퍼들은 구조적으로 나란히 상위권에 붙는다.**
     $250 분산인 척하면서 실제로는 $150 이 한 토큰이었다.  → `dedup_by_correlation`

  ③ 유동성 게이트가 아예 없었다. AUM 게이트는 발견 시점 1회뿐이고 그마저
     `if aum and aum < 50` 이라 **aum 이 0/None 이면 통과**했다 — 신규 상장 직후엔
     totalAssets 가 거의 항상 비므로, 막으려던 대상에게만 정확히 무력했다.
     → `passes_liquidity` / `passes_aum` (둘 다 "모르면 제외")

━━ 게이트 적용 순서 (파이프라인) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    유니버스 로드
     → A. 레버리지/인버스 배제      (fmp_extras SSOT)
     → 수익률·점수·정렬
     → C. 유동성 + AUM 게이트        (passes_liquidity / passes_aum)
     → B-1. 상관 중복 제거           (dedup_by_correlation)
     → B-2. 크립토 캡 2슬롯          (select_top_slots)
     → Top 5 확정
게이트에 걸린 종목은 드롭하고 **다음 순위가 슬롯을 승계**한다.

━━ 사전 확정 임계값 (2026-09-01, 결과 보기 전에 잠갔다) ━━━━━━━━━━━━━━━━━━━━━
분포를 보고 나서 조정하는 것은 금지한다. 프로브는 사후 기록용으로만 돌린다.
"""

from __future__ import annotations

import re as _re

import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════════════════════════
# 사전 확정 임계값 — 재협상 금지
# ══════════════════════════════════════════════════════════════════════════════
MIN_DOLLAR_VOLUME: float = 3_000_000.0   # 20일 평균 달러거래대금 하한
DOLLAR_VOLUME_WINDOW: int = 20           # 평균 구간(거래일)
MIN_AUM_M: float = 50.0                  # 순자산 하한(백만 달러)

CORR_THRESHOLD: float = 0.90             # 이 이상이면 같은 베팅으로 본다
CORR_LOOKBACK: int = 60                  # 상관 계산 구간(거래일)
CORR_MIN_OVERLAP: int = 40               # 공통 관측일이 이보다 적으면 '판정 불가'

HOLD_SLOTS: int = 5                      # 실제 보유 슬롯
CRYPTO_SLOT_CAP: int = 2                 # Top 5 중 크립토 최대 슬롯

# 상관 계산에 필요한 봉 수 — 호출부가 이 값으로 창을 잡는다.
# CORR_LOOKBACK 은 *수익률* 개수라 종가는 +1 봉이 필요하고,
# 1개월 수익률(calculate_period_return(s, 21) → iloc[-22])도 함께 만족해야 한다.
REQUIRED_BARS: int = max(CORR_LOOKBACK + 1, DOLLAR_VOLUME_WINDOW, 22)  # = 61


# ══════════════════════════════════════════════════════════════════════════════
# 제외 사유 코드 — 이메일·화면이 같은 문자열을 쓴다
# ══════════════════════════════════════════════════════════════════════════════
REASON_LABELS: dict[str, str] = {
    "leverage":   "레버리지/인버스",
    "liquidity":  "거래대금 미달",
    "aum":        "순자산 미달",
    "duplicate":  "기초자산 중복",
    "crypto_cap": "크립토 슬롯 한도",
}


def reason_label(code: str) -> str:
    """사유 코드 → 한국어 라벨. 모르는 코드는 코드 자체를 돌려준다."""
    return REASON_LABELS.get(str(code), str(code))


# ══════════════════════════════════════════════════════════════════════════════
# C. 유동성 · 순자산 게이트
# ══════════════════════════════════════════════════════════════════════════════
def avg_dollar_volume(close, volume, window: int = DOLLAR_VOLUME_WINDOW) -> float | None:
    """최근 `window` 거래일의 평균 달러거래대금(종가 × 거래량).

    반환 None = **판정 불가**(데이터 없음/부족). 0.0 과 구별해야 한다 —
    0.0 은 "거래가 없었다"는 관측이고 None 은 "관측이 없다"이며,
    호출부에서 둘 다 제외로 처리되지만 사유 표시가 달라진다.

    거래량이 통째로 비는 종목이 있으므로(FMP 가 ETF 일부에 volume 을 안 준다)
    종가·거래량을 **날짜 인덱스로 정렬해서 교집합**만 쓴다. 길이만 맞춰
    자르면 서로 다른 날의 종가와 거래량을 곱하는 조용한 오류가 난다.
    """
    if close is None or volume is None:
        return None
    c = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    v = pd.to_numeric(pd.Series(volume), errors="coerce").dropna()
    if c.empty or v.empty:
        return None
    pair = pd.concat([c.rename("c"), v.rename("v")], axis=1).dropna()
    if pair.empty:
        return None
    tail = pair.tail(int(window))
    if tail.empty:
        return None
    dv = (tail["c"] * tail["v"]).astype(float)
    if dv.empty or not np.isfinite(dv.to_numpy()).any():
        return None
    val = float(np.nanmean(dv.to_numpy()))
    return val if np.isfinite(val) else None


def passes_liquidity(adv: float | None, minimum: float = MIN_DOLLAR_VOLUME) -> bool:
    """달러거래대금 게이트. **모르면 제외** — AUM 게이트와 같은 규칙."""
    if adv is None:
        return False
    try:
        a = float(adv)
    except (TypeError, ValueError):
        return False
    if not np.isfinite(a):
        return False
    return a >= float(minimum)


def passes_aum(aum_m: float | None, minimum: float = MIN_AUM_M) -> bool:
    """순자산(백만 달러) 게이트. **모르면 제외.**

    옛 `if aum and aum < MIN: continue` 는 falsy(0/None) 를 통과시켜서
    신규 상장 ETF 에게만 정확히 무력했다. 그 결함을 여기서 뒤집는다.
    """
    if aum_m is None:
        return False
    try:
        a = float(aum_m)
    except (TypeError, ValueError):
        return False
    if not np.isfinite(a) or a <= 0:
        return False
    return a >= float(minimum)


# ══════════════════════════════════════════════════════════════════════════════
# B-2. 크립토 판정
# ══════════════════════════════════════════════════════════════════════════════
# 이름 기반이 1순위. 토큰 이름은 일반 영단어와 충돌하는 게 많아서(SOL↔SOLAR,
# ETHER↔AETHER) 전부 단어 경계를 건다. 'COIN' 은 Coinbase 파생상품도 잡는데,
# 그건 오탐이 아니라 의도다 — 크립토 베타를 공유하므로 같은 슬롯을 두고 다툰다.
_CRYPTO_NAME = _re.compile(
    r"\bCRYPTO\w*\b|\bBITCOIN\b|\bBTC\b|\bETHER(?:EUM)?\b|\bETH\b"
    r"|\bSOLANA\b|\bHYPERLIQUID\b|\bHYPE\b|\bXRP\b|\bRIPPLE\b"
    r"|\bDOGE\w*\b|\bLITECOIN\b|\bLTC\b|\bCARDANO\b|\bADA\b"
    r"|\bAVALANCHE\b|\bAVAX\b|\bCHAINLINK\b|\bPOLKADOT\b|\bSUI\b"
    r"|\bBLOCKCHAIN\b|\bDIGITAL\s+ASSET\w*\b|\bTOKEN\w*\b|\bCOIN\w*\b"
    r"|\bWEB3\b|\bSTABLECOIN\b|\bMINER\w*\b",
    _re.I,
)
# 섹터/산업 필드 보조 — FMP 가 ETF 에 이걸 잘 안 채우므로 2순위다.
_CRYPTO_SECTOR = _re.compile(r"CRYPTO|DIGITAL\s+ASSET|BLOCKCHAIN", _re.I)


def is_crypto(ticker: str, name: str = "", sector: str = "") -> bool:
    """크립토 익스포저 종목인지.

    판정 우선순위: ① 이름 ② 섹터/산업 ③ **둘 다 미상이면 비크립토로 본다.**

    여기서만 "모르면 통과"인 이유: 유동성·AUM 은 위험 *크기* 판정이라 모르면
    사는 게 위험하지만, 크립토 캡은 *분류* 판정이다. 분류를 모른다고 배제하면
    이름 없는 신규 ETF 가 전부 캡에 걸려 신규 발굴이라는 존재 이유가 무너진다.
    """
    n = str(name or "")
    if n and _CRYPTO_NAME.search(n):
        return True
    s = str(sector or "")
    if s and _CRYPTO_SECTOR.search(s):
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# B-1. 상관 기반 중복 제거
# ══════════════════════════════════════════════════════════════════════════════
def daily_returns_frame(close_df: pd.DataFrame, lookback: int = CORR_LOOKBACK) -> pd.DataFrame:
    """종가 DataFrame → 최근 `lookback` 개 일간수익률 DataFrame."""
    if close_df is None or not isinstance(close_df, pd.DataFrame) or close_df.empty:
        return pd.DataFrame()
    num = close_df.apply(pd.to_numeric, errors="coerce")
    rets = num.pct_change()
    if len(rets) > lookback:
        rets = rets.tail(int(lookback))
    return rets


def dedup_by_correlation(
    ordered: list,
    rets: pd.DataFrame,
    threshold: float = CORR_THRESHOLD,
    min_overlap: int = CORR_MIN_OVERLAP,
) -> tuple[list, dict]:
    """점수 내림차순 목록에서 서로 같은 베팅인 것을 제거한다 (그리디).

    `ordered` 는 **반드시 점수 내림차순**이어야 한다 — 먼저 채택된 쪽이 남는다.
    반환: (남은 티커 목록, {드롭된 티커: (남긴 티커, 상관계수)})

    ρ 판정이 불가능하면(공통 관측일 부족 / 데이터 없음) **통과**시킨다.
    이력이 짧다는 것은 중복이라는 증거가 아니라 증거가 없다는 뜻이다.
    신규 상장 ETF 를 여기서 떨구면 Hidden Alpha 의 존재 이유가 사라진다.
    유동성·AUM·크립토 캡이 이미 앞뒤에서 막고 있다.
    """
    kept: list = []
    dropped: dict = {}
    if not ordered:
        return kept, dropped
    have = (
        isinstance(rets, pd.DataFrame)
        and not rets.empty
        and len(getattr(rets, "columns", [])) > 0
    )
    for tk in ordered:
        t = str(tk).strip().upper()
        if not t or t in kept or t in dropped:
            continue
        if not have or t not in rets.columns:
            kept.append(t)          # 판정 불가 → 통과
            continue
        s = pd.to_numeric(rets[t], errors="coerce")
        if s.dropna().empty:
            kept.append(t)          # 판정 불가 → 통과
            continue
        hit = None
        for k in kept:
            if k not in rets.columns:
                continue
            s2 = pd.to_numeric(rets[k], errors="coerce")
            pair = pd.concat([s, s2], axis=1).dropna()
            if len(pair) < int(min_overlap):
                continue            # 판정 불가 → 이 쌍은 건너뛴다
            a = pair.iloc[:, 0]
            b = pair.iloc[:, 1]
            # 한쪽이 상수면 상관이 정의되지 않는다 (분모 0) — NaN 이 나오므로
            # 아래 notna 검사에서 걸러진다. 미리 죽이지 않는다.
            rho = a.corr(b)
            if pd.notna(rho) and float(rho) >= float(threshold):
                hit = (k, float(rho))
                break
        if hit is None:
            kept.append(t)
        else:
            dropped[t] = hit
    return kept, dropped


# ══════════════════════════════════════════════════════════════════════════════
# B-2. 슬롯 선정 (크립토 캡 적용)
# ══════════════════════════════════════════════════════════════════════════════
def select_top_slots(
    ordered: list,
    crypto_map: dict | None = None,
    slots: int = HOLD_SLOTS,
    cap: int = CRYPTO_SLOT_CAP,
) -> tuple[list, dict]:
    """점수 내림차순 목록 → 최종 보유 슬롯. 크립토는 `cap` 개까지만.

    캡에 걸린 종목은 드롭하고 **다음 순위가 승계**한다 (슬롯을 비우지 않는다).
    반환: (선정 티커 목록, {스킵된 티커: "crypto_cap"})

    B-1 이 래퍼 중복(THYP/BHYP/HYPG)을 없애도 HYPE 1 + SOL 1 + BTC 1 조합은
    ρ 가 0.9 를 밑돌아 통과한다. 서로 다른 토큰이지만 위험회피 국면에는 한 몸으로
    무너지므로 손실 방지 우선 원칙에서 별도 캡이 필요하다. (2026-09-01 확정)
    """
    crypto_map = crypto_map or {}
    selected: list = []
    skipped: dict = {}
    n_crypto = 0
    for tk in ordered:
        if len(selected) >= int(slots):
            break
        t = str(tk).strip().upper()
        if not t or t in selected:
            continue
        if bool(crypto_map.get(t)):
            if n_crypto >= int(cap):
                skipped[t] = "crypto_cap"
                continue
            n_crypto += 1
        selected.append(t)
    return selected, skipped


# ══════════════════════════════════════════════════════════════════════════════
# 오케스트레이터 — 호출부는 이것만 부르면 된다
# ══════════════════════════════════════════════════════════════════════════════
def apply_rotation_gates(
    ranked: pd.DataFrame,
    close_df: pd.DataFrame | None = None,
    volume_df: pd.DataFrame | None = None,
    meta: dict | None = None,
    slots: int = HOLD_SLOTS,
    ticker_col: str = "Ticker",
) -> dict:
    """랭킹 표 + 가격/거래량/메타 → 최종 슬롯과 제외 내역.

    ranked     : 점수 내림차순 정렬이 끝난 DataFrame (`ticker_col` 필수)
    close_df   : 열 = 티커, 행 = 날짜인 종가 DataFrame
    volume_df  : 같은 모양의 거래량 DataFrame (없으면 유동성 판정 불가 → 전원 제외)
    meta       : {티커: {"name":…, "sector":…, "aum_m":…, "leveraged":bool}}

    반환 dict:
      selected  : 최종 Top N 티커 목록
      excluded  : {티커: 사유코드}
      detail    : {티커: 사람이 읽을 부가정보 문자열}
      adv       : {티커: 평균 달러거래대금 or None}
      crypto    : {티커: bool}

    ⚠️ `ranked` 의 정렬을 여기서 다시 하지 않는다. 정렬 책임은 호출부에 있고,
       이 함수는 주어진 순서를 신뢰한다. 두 곳에서 정렬하면 어느 쪽이 이겼는지
       모르는 조용한 실패가 난다.
    """
    out = {"selected": [], "excluded": {}, "detail": {}, "adv": {}, "crypto": {}}
    if ranked is None or not isinstance(ranked, pd.DataFrame) or ranked.empty:
        return out
    if ticker_col not in ranked.columns:
        return out

    meta = meta or {}
    ordered = [str(t).strip().upper() for t in ranked[ticker_col].tolist() if str(t).strip()]

    # ── A. 레버리지 (판정은 fmp_extras 가 이미 했고 meta 로 들어온다) ──────────
    survivors: list = []
    for t in ordered:
        m = meta.get(t) or {}
        if bool(m.get("leveraged")):
            out["excluded"][t] = "leverage"
            out["detail"][t] = str(m.get("name", ""))[:60]
            continue
        survivors.append(t)

    # ── C. 유동성 + 순자산 ────────────────────────────────────────────────────
    after_c: list = []
    for t in survivors:
        c = close_df[t] if (close_df is not None and t in getattr(close_df, "columns", [])) else None
        v = volume_df[t] if (volume_df is not None and t in getattr(volume_df, "columns", [])) else None
        adv = avg_dollar_volume(c, v)
        out["adv"][t] = adv
        if not passes_liquidity(adv):
            out["excluded"][t] = "liquidity"
            out["detail"][t] = ("데이터 없음" if adv is None
                                else f"20일 평균 ${adv / 1_000_000:.2f}M")
            continue
        aum = (meta.get(t) or {}).get("aum_m")
        if not passes_aum(aum):
            out["excluded"][t] = "aum"
            out["detail"][t] = ("데이터 없음" if aum in (None, "")
                                else f"AUM ${float(aum):.0f}M")
            continue
        after_c.append(t)

    # ── B-1. 상관 중복 제거 ───────────────────────────────────────────────────
    sub = None
    if close_df is not None and isinstance(close_df, pd.DataFrame) and not close_df.empty:
        cols = [t for t in after_c if t in close_df.columns]
        if cols:
            sub = daily_returns_frame(close_df[cols])
    kept, dropped = dedup_by_correlation(after_c, sub if sub is not None else pd.DataFrame())
    for t, (keeper, rho) in dropped.items():
        out["excluded"][t] = "duplicate"
        out["detail"][t] = f"{keeper} 와 ρ={rho:.2f}"

    # ── B-2. 크립토 캡 ────────────────────────────────────────────────────────
    for t in kept:
        m = meta.get(t) or {}
        out["crypto"][t] = is_crypto(t, m.get("name", ""), m.get("sector", ""))
    selected, skipped = select_top_slots(kept, out["crypto"], slots=slots)
    for t, why in skipped.items():
        out["excluded"][t] = why
        out["detail"][t] = f"크립토 {CRYPTO_SLOT_CAP}슬롯 초과"

    out["selected"] = selected
    return out
