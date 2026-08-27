#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""매도 판정(포지션·스윙) 진단 — 읽기 전용.

목적
────
매매레이더에서 NEE/MRNA/LRCX 가 매수 직후 청산 알림을 받은 건에 대해,
(a) 입력 데이터가 맞는지, (b) 어떤 항목이 결정타인지, (c) 제안된 문턱 변경안이
실제로 판정을 바꾸는지를 숫자로 확인한다.

이 스크립트는 아무것도 수정하지 않는다.
  - 시트 기록 없음 (읽기 전용)
  - 이메일 발송 없음
  - regime_core / app.py 로직 변경 없음

⚠️ 3D·3E 는 채택되어 사라졌다 (2026-08-26) — 다시 추가하지 말 것
─────────────────────────────────────────────────────────────
  · 3D(1개월 <= -5% 일 때만 +1) → `rc.POSITION_MONTH_DROP_PCT = -5.0` 으로 채택됨
  · 3E(200일선 이격 비례 배점)  → `rc.MA200_GAP_BASE_SCORE/MA200_GAP_FULL_PCT` 로
      채택됨. 단 SSOT 는 **선형 램프**이지 이 파일이 쓰던 계단식이 아니다.
      app.py:10834 와 regime_core:2407 이 `gap_ma200_pct` 를 넘겨 이미 라이브다.

  둘 다 프로덕션이 된 뒤에도 이 파일의 '현재' 열이 갱신되지 않아, 시나리오 열이
  **프로덕션 대 프로덕션**을 비교하고 있었다. 그 상태로 PNC·CSCO 에서 SSOT 대조가
  깨졌다(진단 🟡 줄이기 vs SSOT ✅ 보유). 원인은 '현재' 열이 `om < 0` 로 채점한
  것 — SSOT 는 `om <= -5.0` 이다.

  ⚠️ 재정의하지 말 것. 새 임계값을 넣는 것은 **새 질문을 발명하는 것**이고,
     그럴 사전 기준도 근거도 없다.

출력 블록
─────────
  [S] SSOT 격자 대조     — 합성 입력 전수로 복제본 == regime_core 검증 (콜 0회)
  [T] block0 자기검증    — 스텁 주입으로 [0] 의 분기 전수 실행 (콜 0회)
  [0] MA200 격차 원인 규명 — 중복날짜 · 창 길이 · min_periods · 배당조정 (3콜)
       └ 0-F 과거 MA200 스냅샷 추적 — 외부값이 '언제의 값'인지 (추가 콜 0회)
  [1] 보유 종목 스냅샷   — MA200 이격 / MACD / 샹들리에 등 판정 입력 전체
  [2] 진입 시점 재구성   — Date_Added 당시 코드 vs 오늘 코드 (2A 억제 효과 판정)
  [3] 점수 시나리오 비교 — 현재 / 문턱5
  [4] MA200 이격 분포    — 보유·유니버스가 SSOT 램프의 어디에 있는지

실행:  python automation/diag_sell_verdict.py   (repo root 에서)
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fmp_http as fh  # noqa: E402
import regime_core as rc  # noqa: E402

try:
    import fmp_extras as fx  # noqa: E402
except Exception:  # pragma: no cover - 선택 의존
    fx = None

# ── 환경 ──────────────────────────────────────────────────────────────────────
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")

_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 20
_SPREADSHEET_TITLE = "Quant_DB"
_PF_WORKSHEET = "Portfolios"
_WL_WORKSHEET = "Watchlist"

# Portfolios 열 인덱스 — run_watchlist_alerts.py 와 동일(0-base)
_PF_COL_AVG = 3
_PF_COL_DATE_ADDED = 5

# 보유 종목은 Date_Added 시점의 MA200 을 계산해야 하므로 그 이전 200봉이 더 필요하다.
HOLD_LIMIT = 600
SCAN_LIMIT = 300          # [4] 분포용 — MA200 만 있으면 되므로 짧게
_FETCH_WORKERS = 6

PROBE_TICKER = os.environ.get("DIAG_PROBE_TICKER", "NEE")
ONLY_UID = os.environ.get("DIAG_ONLY_UID", "").strip()   # 빈 값 = 전체 사용자

# ── 문턱 (미채택 시나리오만 남긴다) ──────────────────────────────────────────
# ⚠️ D_MONTH_THRESHOLD / E_LADDER 는 삭제됐다. 채택되어 SSOT 로 옮겨졌기 때문이다.
#    상수를 여기 복제하면 SSOT 가 바뀔 때 조용히 갈라진다 — 실제로 그렇게 갈라졌다.
#    임계값이 필요하면 rc.POSITION_MONTH_DROP_PCT 처럼 **rc 에서 읽어 쓸 것**.
SELL_TH_DEFAULT, TRIM_TH_DEFAULT = 4, 2
SELL_TH_ALT = 5                   # 미채택: 청산 문턱을 4 → 5 로

# ── [0-F] 스냅샷 가설 사전 확정 기준 (2026-08-28 확정 · 결과를 보기 전에 못 박음) ─
# 배당조정 · 중복날짜 · 창 길이 · 봉수 가설이 전부 기각된 뒤 남은 마지막 후보:
#   "외부값(89.77 / 91.64)은 **다른 날짜의 우리 MA200 스냅샷**이다."
#
# ⚠️ 원안("최근 252봉 안에 두 값이 등장하면 채택")을 글자 그대로 쓰면 이 검정은
#    거의 자동 통과한다. 우리 값 87.87 기준으로 두 목표는 **둘 다 위쪽**이고,
#    MA200 이 하락 추세로 87 → 92 를 훑고 지나갔다면 그 사이의 **모든 값**이
#    '등장'한다. 그때 통과는 스냅샷의 증거가 아니라 'MA200 이 그 구간을
#    지나갔다'는 사실만 증명한다. 그래서 실행 전에 셋을 확정했다:
#      (a) 허용오차 ±0.05        — 외부 표시가 소수 2자리이므로 그 언저리
#      (b) 두 날짜 모두 최근 60봉(~3개월) 이내 — 데이터 사이트가 1년 낡을 리 없다
#      (c) 판정 시리즈는 raw close 단독 — 배당조정본은 참고 출력이며 판정에
#          관여하지 않는다(둘 다 허용하면 기회가 두 배가 되어 기준이 느슨해진다)
#
# ⚠️ 결과를 본 뒤 이 상수들을 손대지 말 것. 손대는 순간 이 검정은 증거가 아니라
#    사후 맞춤이 된다. 값을 바꾸고 싶으면 **새 질문**으로 새 기준을 먼저 세운다.
MA200_SNAP_TOL = 0.05          # (a)
MA200_SNAP_RECENT = 60         # (b) 봉 단위
MA200_SNAP_SCAN = 252          # 되짚을 과거 구간(봉)
MA200_EXT_TARGETS = [("TickerReport", 89.77), ("Investing.com", 91.64)]


# ══════════════════════════════════════════════════════════════════════════════
# FMP
# ══════════════════════════════════════════════════════════════════════════════
def _fmp_rows_ex(ticker: str, limit: int | None = None,
                 path: str = "historical-price-eod/full",
                 day_span: int | None = None):
    """(행 리스트 | None, kind) — 실패 사유를 보존한다.

    day_span 을 주면 `limit` 대신 `from`/`to` 창으로 호출한다.
    §7 에서 `historical-price-eod` 의 `limit` 은 **무효 확정**이고 `from`/`to` 는
    **먹힘 확정**이므로, 두 방식을 나란히 비교할 수 있어야 원인을 가른다.

    kind ∈ {"ok","empty","no_key","rate_limited","plan_limited","http_error",
            "exception","bad_json"}

    왜 사유를 남기는가: 이전 구현은 non-200 을 전부 None 으로 뭉갰다. 그러면
    402(플랜 미제공)와 429(레이트리밋)와 '200 인데 행이 짧음'이 화면상 똑같이
    보인다. **이번 조사(배당조정 봉 부족)의 원인 후보가 정확히 그 셋**이라
    뭉개면 조사 자체가 성립하지 않는다.
    """
    if not FMP_API_KEY:
        return None, "no_key"
    url = f"{_FMP_BASE}/{path}?symbol={ticker}"
    if day_span is not None:
        today = datetime.utcnow().date()
        url += (f"&from={today - timedelta(days=int(day_span))}"
                f"&to={today}")
    elif limit is not None:
        url += f"&limit={int(limit)}"
    url += f"&apikey={FMP_API_KEY}"

    r, _status, kind = fh.fmp_get_ex(url, timeout=_FMP_TIMEOUT)
    if r is None or kind != "ok":
        return None, kind
    try:
        data = r.json()
    except Exception:
        return None, "bad_json"
    rows = data.get("historical", data) if isinstance(data, dict) else data
    if not (isinstance(rows, list) and rows):
        return None, "empty"
    return rows, "ok"


def _fmp_rows(ticker: str, limit: int, path: str = "historical-price-eod/full"):
    """원본 행 리스트 반환. 실패 시 None (키 점검용으로 가공 전 상태를 준다).

    ⚠️ 반환 계약을 일부러 바꾸지 않았다 — `_price_history` 를 거쳐 [1]~[4] 전
       블록이 이 모양에 의존한다. 사유가 필요하면 `_fmp_rows_ex` 를 쓴다.
    """
    return _fmp_rows_ex(ticker, limit=limit, path=path)[0]


def _hist_from_rows(rows, price_field: str = "close") -> pd.DataFrame:
    """원본 행 → OHLCV DataFrame. price_field 로 Close 를 무엇으로 삼을지 선택."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "date" not in df.columns or price_field not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    out = pd.DataFrame(index=df.index)
    for src, dst in [("open", "Open"), ("high", "High"), ("low", "Low"),
                     ("volume", "Volume")]:
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src], errors="coerce")
    out["Close"] = pd.to_numeric(df[price_field], errors="coerce")
    return out.dropna(subset=["Close"])


def _price_history(ticker: str, limit: int = 252) -> pd.DataFrame:
    """run_watchlist_alerts._fmp_price_history 와 동일 동작(미조정 close)."""
    return _hist_from_rows(_fmp_rows(ticker, limit), "close")


# ══════════════════════════════════════════════════════════════════════════════
# 판정 입력 추출 + 점수 분해
# ══════════════════════════════════════════════════════════════════════════════
def verdict_inputs(hist: pd.DataFrame, entry_price=None, entry_date: str = "") -> dict:
    """integrated_sell_verdict 가 보는 입력값을 그대로 재현해 반환.

    position_sell_verdict 내부와 동일한 순서·동일한 SSOT 헬퍼를 쓴다.
    """
    close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
    if len(close) < 60:
        return {}
    price = float(close.iloc[-1])
    ma50 = rc._ma_last(close, 50)
    ma200 = rc._ma_last(close, 200)
    above_ma200 = bool(price > ma200) if np.isfinite(ma200) else None
    gap200 = (price / ma200 - 1.0) * 100.0 if np.isfinite(ma200) and ma200 > 0 else np.nan
    gap50 = (price / ma50 - 1.0) * 100.0 if np.isfinite(ma50) and ma50 > 0 else np.nan

    one_month = np.nan
    if len(close) > 22:
        p0 = float(close.iloc[-22])
        if p0 > 0:
            one_month = (price / p0 - 1.0) * 100.0

    r = rc.compute_rsi(close).dropna()
    rsi = float(r.iloc[-1]) if not r.empty else np.nan
    macd = rc._macd_state(close)
    hi52 = float(close.tail(252).max())
    pct52 = (price / hi52 - 1.0) * 100.0 if hi52 > 0 else np.nan

    dd, dd_err = rc.compute_position_drawdown(close, entry_price, price, entry_date)
    atr = rc.compute_atr(hist, rc.ATR_WINDOW)
    chand = np.nan
    if pd.notna(atr) and atr > 0:
        hh = float(close.tail(max(rc.CHANDELIER_LOOKBACK, 22)).max())
        ref = max(hh, float(entry_price)) if (entry_price and pd.notna(entry_price)) else hh
        chand = ref - rc.CHANDELIER_ATR_MULT * atr

    return {
        "price": price, "ma50": ma50, "ma200": ma200, "gap50": gap50, "gap200": gap200,
        "above_ma200": above_ma200, "one_month": one_month, "rsi": rsi, "macd": macd,
        "pct52": pct52, "drawdown": (np.nan if dd_err else dd), "dd_raw": dd,
        "dd_error": dd_err, "atr": atr, "chandelier": chand,
    }


def score_components(inp: dict, *,
                     trailing_stop_pct: float = rc._DEFAULT_TRAILING_STOP_PCT) -> tuple:
    """점수 분해. ⚠️ SSOT 아님 — 항목별 기여도를 보여주기 위한 복제본이다.

    `integrated_sell_verdict` 는 합계만 돌려주므로 분해를 얻으려면 복제가 필요하다.
    **반드시 rc 와 같은 결과를 내야 한다** — [S] 블록이 합성 격자 전수로 대조한다.

    ⚠️ 임계값을 이 파일에 복제하지 말 것. 전부 rc 에서 읽는다.
       예전에 `om < 0` 을 하드코딩해 두었는데 SSOT 가 `om <= -5.0` 으로 바뀌면서
       조용히 갈라졌고, 보유 종목의 1개월 수익률이 우연히 -5%~0% 에 들어온
       날에야 발각됐다.
    """
    c = {"ma200": 0.0, "trailing": 0.0, "macd": 0.0, "month": 0.0, "overheat": 0.0}

    if inp.get("above_ma200") is False:
        # SSOT 는 이격 비례 **선형 램프**다(계단식이 아니다).
        # app.py·regime_core 가 gap_ma200_pct 를 넘기므로 이 경로가 라이브다.
        gap = inp.get("gap200")
        if pd.notna(gap):
            _g = abs(float(gap))
            c["ma200"] = rc.MA200_GAP_BASE_SCORE + (4.0 - rc.MA200_GAP_BASE_SCORE) * min(
                1.0, _g / rc.MA200_GAP_FULL_PCT)
        else:
            c["ma200"] = 4.0

    dd = inp.get("drawdown")
    if pd.notna(dd) and dd <= -abs(trailing_stop_pct):
        c["trailing"] = 3.0

    macd = inp.get("macd")
    if macd == "DEAD_CROSS":
        c["macd"] = 2.0
    elif macd == "BELOW_SIGNAL":
        c["macd"] = 1.0

    om = inp.get("one_month")
    if pd.notna(om) and om <= rc.POSITION_MONTH_DROP_PCT:
        c["month"] = 1.0

    rsi, p52 = inp.get("rsi"), inp.get("pct52")
    if pd.notna(rsi) and rsi > 70 and pd.notna(p52) and p52 > -3:
        c["overheat"] = 1.0

    return sum(c.values()), c


def label_for(score: int, sell_th: int = SELL_TH_DEFAULT,
              trim_th: int = TRIM_TH_DEFAULT) -> str:
    if score >= sell_th:
        return "🔴 청산"
    if score >= trim_th:
        return "🟡 줄이기"
    return "✅ 보유"


# ══════════════════════════════════════════════════════════════════════════════
# [S] SSOT 격자 대조 — 합성 입력 전수. FMP·시트 접근 없음.
# ══════════════════════════════════════════════════════════════════════════════
# 실보유 종목만으로 대조하면 **그날 우연히 판별 구간에 들어야만** 드리프트가
# 잡힌다. 실제로 `om < 0` vs `om <= -5.0` 드리프트는 보유 종목의 1개월 수익률이
# -5%~0% 에 들어온 날에야 발각됐다. 그 사이 [3] 은 계속 초록불이었다.
#   → 경계값을 직접 만들어 전수로 돌린다. 매 실행마다 확정적으로 잡힌다.
_G_MONTH = [float("nan"), -10.0, -5.1, -5.0, -4.9, -2.8, 0.0, 3.0]
_G_GAP = [float("nan"), 0.0, -0.5, -3.0, -4.0, -7.9, -8.0, -8.1, -20.0]
_G_MACD = ["DEAD_CROSS", "BELOW_SIGNAL", "ABOVE_SIGNAL", "N/A"]
_G_RSI = [float("nan"), 50.0, 70.0, 70.1]
_G_P52 = [float("nan"), -10.0, -3.0, -2.9, 0.0]
_G_DD = [float("nan"), 0.0, -14.9, -15.0, -15.1]
_G_ABOVE = [True, False]


def block_s_ssot_grid() -> bool:
    """복제본 score_components 가 regime_core 와 **모든 경계 조합**에서 일치하는가."""
    print("\n" + "=" * 78)
    print("[S] SSOT 격자 대조 — 합성 입력 전수 (FMP 콜 0 · 시트 접근 없음)")
    print("=" * 78)

    total, bad = 0, []
    for above in _G_ABOVE:
        for gap in _G_GAP:
            for om in _G_MONTH:
                for macd in _G_MACD:
                    for rsi in _G_RSI:
                        for p52 in _G_P52:
                            for dd in _G_DD:
                                inp = {"above_ma200": above, "gap200": gap,
                                       "one_month": om, "macd": macd, "rsi": rsi,
                                       "pct52": p52, "drawdown": dd}
                                total += 1
                                sc, _c = score_components(inp)
                                mine = label_for(sc)
                                ssot, _r = rc.integrated_sell_verdict(
                                    above_ma200=above, one_month_return=om, rsi=rsi,
                                    macd_signal=macd, pct_from_52w_high=p52,
                                    drawdown_from_high_pct=dd, gap_ma200_pct=gap)
                                if mine[0] != ssot[0]:
                                    bad.append((inp, mine, ssot, sc))

    print(f"  조합 {total:,}건 대조")
    if bad:
        print(f"\n  ❌ 불일치 {len(bad):,}건 — 진단 복제본이 SSOT 와 갈라졌다.")
        for inp, mine, ssot, sc in bad[:8]:
            print(f"     above_ma200={inp['above_ma200']} gap={inp['gap200']} "
                  f"1개월={inp['one_month']} MACD={inp['macd']} RSI={inp['rsi']} "
                  f"고점대비={inp['pct52']} 낙폭={inp['drawdown']}")
            print(f"       진단 {sc:g} {mine}  vs  regime_core {ssot}")
        if len(bad) > 8:
            print(f"     … 외 {len(bad) - 8:,}건")
        print("\n     → [1][2][3] 전부 신뢰하지 말 것. score_components 를 먼저 고쳐야 한다.")
        print("     → 임계값을 이 파일에 복제하지 말고 rc 에서 읽을 것.")
        return False
    print("  ✅ 전 조합 일치 — 복제본이 regime_core.integrated_sell_verdict 와 동일")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# 시트
# ══════════════════════════════════════════════════════════════════════════════
def _sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(GSPREAD_KEY_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    return gspread.authorize(Credentials.from_service_account_info(info, scopes=scopes)) \
        .open(_SPREADSHEET_TITLE)


def read_holdings(sh) -> list:
    """[(uid, account, ticker, avg, date_added)] — 읽기 전용."""
    try:
        vals = sh.worksheet(_PF_WORKSHEET).get_all_values() or []
    except Exception as e:
        print(f"  [WARN] Portfolios 읽기 실패: {e}")
        return []
    out = []
    for r in vals[1:]:
        r = (list(r) + [""] * 6)[:6]
        uid, acct, tk = str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip().upper()
        if not uid or not tk:
            continue
        if ONLY_UID and uid != ONLY_UID:
            continue
        avg = pd.to_numeric(r[_PF_COL_AVG], errors="coerce")
        out.append((uid, acct, tk, float(avg) if pd.notna(avg) else None,
                    str(r[_PF_COL_DATE_ADDED]).strip()))
    return out


def read_watchlist_tickers(sh) -> list:
    try:
        vals = sh.worksheet(_WL_WORKSHEET).get_all_values() or []
    except Exception:
        return []
    return sorted({str(r[1]).strip().upper() for r in vals[1:]
                   if len(r) > 1 and str(r[1]).strip()})


def satellite_tickers() -> list:
    if fx is None:
        return []
    try:
        pool = fx.satellite_candidate_pool() or {}
        return sorted({str(t).strip().upper() for v in pool.values() for t in v if str(t).strip()})
    except Exception as e:
        print(f"  [WARN] satellite_candidate_pool 실패: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# [T] block0 자기검증 — 스텁 주입 · FMP 콜 0회 · 네트워크 없음
# ══════════════════════════════════════════════════════════════════════════════
# [S] 격자 대조와 같은 취지다: 이 블록의 판정을 믿기 전에 블록 자신을 검증한다.
# 개발 중 뮤테이션 7건(중복제거 무력화 · from/to 분기 제거 · 배당조정 부등호
# 반전 · min_periods 문턱 · 창 크기 · 봉수 게이트 · 중복 카운트)을 걸어 전부
# 잡히는 것을 확인했고, **그 과정에서 죽은 조건절 하나를 발견해 제거**했다
# (두 값 비교는 봉수 검사에 이미 가려져 판정에 관여하지 못했다).
def _t_rows(n, start_px=80.0, dup=0, adj=True, step=0.05, adj_mult=0.985,
            with_close=True):
    """합성 일봉 n개(거래일만). dup>0 이면 앞쪽 dup개 날짜를 중복시킨다.

    with_close=False 는 **`close` 없이 `adjClose` 만 오는 스키마**를 재현한다.
    `dividend-adjusted` 가 실제로 그 모양이며(2026-08-27 실측), 이 옵션이 없던
    동안 T1 은 `close` 가 있는 가상의 응답만 먹여서 결함을 통과시켰다.
    """
    out, d, got = [], datetime(2026, 8, 27).date(), 0
    while got < n:
        if d.weekday() < 5:
            px = start_px + got * step
            r = {"date": str(d), "open": px, "high": px, "low": px,
                 "volume": 1000}
            if with_close:
                r["close"] = round(px, 4)
            if adj:
                r["adjClose"] = round(px * adj_mult, 4)
            out.append(r)
            got += 1
        d -= timedelta(days=1)
    for i in range(dup):
        out.insert(0, dict(out[i]))
    return out


def _t_stub(full_n, dup=0, adj=True, adj_mult=0.985, div_lim=None,
            div_win=None, div_kind="empty", full_kind="ok",
            div_schema="adjClose"):
    """block0 이 부르는 `_fmp_rows_ex` 를 대신할 스텁을 만든다.

    div_schema ∈ {"adjClose", "close", "none"} — dividend-adjusted 응답의
    가격 필드. 기본을 "adjClose" 로 둔 것은 그것이 **실측된 모양**이기 때문이다.
    """
    def stub(t, limit=None, path="historical-price-eod/full", day_span=None):
        if "dividend-adjusted" in path:
            n = div_lim if day_span is None else div_win
            if not n:
                return None, div_kind
            if div_schema == "adjClose":
                r = _t_rows(n, 78.8, adj=True, adj_mult=1.0, with_close=False)
            elif div_schema == "close":
                r = _t_rows(n, 78.8, adj=False)
            else:                       # 가격 필드가 아예 없는 응답
                r = [{"date": x["date"], "volume": 1000} for x in
                     _t_rows(n, 78.8, adj=False)]
            return r, "ok"
        if full_kind != "ok":
            return None, full_kind
        return _t_rows(full_n, dup=dup, adj=adj, adj_mult=adj_mult), "ok"
    return stub


# (이름, block0 입력, 반드시 나와야 할 문자열, 절대 나오면 안 될 문자열)
_T_CASES = [
    # ⚠️ 픽스처는 **실측 비율**이다(2026-08-27 NEE): limit 요청은 무시되어
    #    전체(1254행)가 오고, from/to 460일은 316행이 온다. 이전 픽스처는
    #    div_lim=100 < div_win=330 으로 **정반대 세계**를 고정하고 있었고,
    #    그래서 "짧은 쪽을 고른다"는 실제 결함을 통과시켰다.
    ("T1 정상 600봉", dict(full_n=600, div_lim=600, div_win=316),
     ["중복 날짜                      : 0 건", "✅ 정상",
      "`limit` 이 무시되고 전체 이력이 왔다 (600행)",
      "사용 시리즈                    : limit 경로 600행",
      "사용 필드                      : adjClose",
      "close (중복제거)", "dividend-adjusted (adjClose)",
      "✅ 배당조정 가설 기각",
      # 0-F. 픽스처는 0.05/봉 로 단조 하락하므로 스윕 범위가 두 목표를 통째로
      # 삼킨다 → '등장'은 하되 날짜 게이트에서 걸려야 한다. 이 조합이 곧
      # "통과가 보장되는 검정"의 실물이며, 경고와 기각이 함께 나와야 옳다.
      "── 0-F. 과거 MA200 스냅샷 추적 (FMP 콜 0회) ──",
      "⚠️ 검정력 경고: 두 목표값이 모두 스윕 범위 안에 있다",
      "❌ 스냅샷 가설 기각", "60봉 게이트 밖", "MA200 격차 건 종료",
      "[참고] dividend-adjusted"],
     ["❗ 배당조정 MA200 이 미조정보다 높다", "응답 없음",
      "✅ 스냅샷 가설 채택", "⛔ 판정 보류"]),
    # T8 — 이번 실측에서 드러난 결함 그 자체. `kind=ok` 로 행이 왔는데 가격
    # 필드명이 달라 0-D 가 "응답 없음"을 찍고 0-E 가 판정을 포기했다.
    # 이 케이스가 없으면 같은 결함이 또 조용히 통과한다.
    ("T8 dividend-adjusted 가 adjClose 로만 옴",
     dict(full_n=600, div_lim=600, div_win=316, div_schema="adjClose"),
     ["dividend-adjusted (adjClose)"],
     ["응답 없음", "비교 대상 소스가 하나도 안 나왔다",
      "dividend-adjusted            가격 필드 없음"]),
    # T9 — 반대편. 행은 왔는데 쓸 가격 필드가 정말 없는 경우는 '응답 없음'이
    # 아니라 **받은 필드 목록**을 보여줘야 다음 사람이 원인을 안다.
    ("T9 가격 필드가 아예 없는 응답",
     dict(full_n=600, div_lim=600, div_win=316, div_schema="none"),
     ["가격 필드 없음", "받은 필드: date, volume"],
     ["dividend-adjusted (adjClose)", "dividend-adjusted (close)"]),
    ("T2 중복 7건", dict(full_n=600, dup=7, div_kind="plan_limited"),
     ["중복 날짜                      : 7 건",
      "⚠️ 200봉 창이 200거래일보다 길어진다",
      "엔드포인트 자체를 못 쓴다 (kind=plan_limited/plan_limited)"], []),
    ("T3 170봉", dict(full_n=170),
     ["고유 봉이 170개뿐 — 200봉 창을 만들 수 없다",
      "200봉 평균이 아니다 (170봉 · min_periods=150)",
      "그대로 gap_ma200_pct 가 되어 3E 램프에 들어간다",
      "봉 부족", "(170봉 / 200봉 필요)",
      # 0-F: 200봉이 안 되면 MA200 시계열 자체가 없다. 이때 '기각'을 찍으면
      # 데이터 부족을 가설 기각으로 둔갑시키는 것이라 '판정 불가'여야 한다.
      "⚠️ MA200 시계열을 만들 수 없다 (170봉 < 200봉)",
      "⛔ 판정 불가"],
     ["프로덕션에서도 ma200 이 NaN", "❌ 스냅샷 가설 기각",
      "✅ 스냅샷 가설 채택"]),
    ("T3b 130봉", dict(full_n=130),
     ["봉이 130개뿐 — 프로덕션에서도 ma200 이 NaN"],
     ["그대로 gap_ma200_pct 가 되어"]),
    # ⚠️ T3c 는 2026-08-27 추가. 200~251봉 구간에서 "200봉 평균이 아니다"라는
    #    **거짓 문장**이 나가던 것을 잡는다. 이 구간은 ma200 자체는 정상이고
    #    52주 창만 짧다. 이 케이스가 없으면 그 오보를 아무도 못 잡는다.
    ("T3c 220봉", dict(full_n=220),
     ["은 정상(200봉 충족)이나 봉이 220개로 52주(252봉)에 미달한다",
      "고점대비·낙폭 입력이 짧은 창에서 나온다",
      # 0-F: MA200 점이 21개뿐이라 252봉 창을 못 덮는다. 접촉이 없는 목표가
      # 있으면 '기각'이 아니라 '판정 보류'다 — 못 본 구간이 있기 때문이다.
      "⚠️ 창 부족: MA200 점이 21개로 252봉에 미달한다",
      "⛔ 판정 보류"],
     ["200봉 평균이 아니다", "프로덕션에서도 ma200 이 NaN",
      "❌ 스냅샷 가설 기각"]),
    ("T4 조정치가 더 높음", dict(full_n=600, adj_mult=1.05),
     ["❗ 배당조정 MA200 이 미조정보다 높다"], ["✅ 배당조정 가설 기각"]),
    ("T5 첫 호출 실패", dict(full_n=600, full_kind="rate_limited"),
     ["응답 없음 (kind=rate_limited)"], ["0-D. 소스별 MA200"]),
    ("T6 adjClose 없음", dict(full_n=600, adj=False),
     ["adjClose 필드                  : 없음 ❌",
      "비교 대상 소스가 하나도 안 나왔다"], []),
]


def _t_snap_frame(n=700, step=0.25) -> pd.DataFrame:
    """단조 상승 합성 종가 n봉. 기울기를 크게 둬 봉마다 MA200 이 확실히 갈린다.

    step 을 크게 잡은 이유: MA200 이 봉당 step 만큼 움직이므로 서로 다른 age 의
    값이 허용오차(±0.05) 안에서 겹치지 않는다. 겹치면 '어느 날짜냐'를 묻는
    검정 자체가 성립하지 않는다.
    """
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    return pd.DataFrame({"Close": [50.0 + i * step for i in range(n)]},
                        index=idx)


def _t_snapshot_controls() -> list:
    """0-F 의 대조군 T10~T14. 실패 메시지 리스트를 반환(빈 리스트 = 통과)."""
    import io
    from contextlib import redirect_stdout

    out, g = [], globals()
    df = _t_snap_frame()
    trk = _ma200_track(df)                      # 스캔 창(252) 적용
    full = _ma200_track(df, scan=0)             # 전체 — 창 게이트 검증용
    # full 은 age 300 을 뽑을 수 있어야 한다(창 게이트 검증용) → 최소 301점.
    if len(trk) != MA200_SNAP_SCAN or len(full) < 301:
        return [f"T10~T14: 픽스처 이상 (trk={len(trk)}, full={len(full)})"]

    def at(ser, age):
        return float(ser.iloc[len(ser) - 1 - age])

    v10, v20, v100 = at(trk, 10), at(trk, 20), at(trk, 100)
    v300 = at(full, 300)                        # 252봉 창 **밖**

    cases = [
        # (이름, 목표쌍, 반드시 나올 것, 나오면 안 될 것)
        ("T10 양성대조 (오차 0.04 · 최근 10/20봉)",
         [("A", v10 + 0.04), ("B", v20 - 0.04)],
         ["✅ 스냅샷 가설 채택"], ["❌ 스냅샷 가설 기각", "⛔ 판정 보류"]),
        ("T11 음성대조 (스윕 범위 밖)",
         [("A", v10), ("B", float(trk.max()) + 50.0)],
         ["❌ 스냅샷 가설 기각", "한 번도 닿지 않았다"], ["✅ 스냅샷 가설 채택"]),
        ("T12 날짜 게이트 (100봉 전 접촉)",
         [("A", v10), ("B", v100)],
         ["❌ 스냅샷 가설 기각", "100봉 전 — 60봉 게이트 밖"],
         ["✅ 스냅샷 가설 채택"]),
        ("T13 창 게이트 (252봉 밖 300봉 전 값)",
         [("A", v10), ("B", v300)],
         ["❌ 스냅샷 가설 기각", "한 번도 닿지 않았다"], ["✅ 스냅샷 가설 채택"]),
        ("T14 허용오차 상한 (오차 0.10)",
         [("A", v10 + 0.10), ("B", v20)],
         ["❌ 스냅샷 가설 기각", "한 번도 닿지 않았다"], ["✅ 스냅샷 가설 채택"]),
    ]

    # T16 — 게이트 경계 + '가장 최근 접촉' 선택을 한 케이스로 못 박는다.
    # step 을 0.02 로 낮춰 한 목표에 접촉이 5개(60~64봉 전) 생기게 만든다.
    #   · 가장 최근 접촉(60봉)을 고르고 `<= 60` 이어야 채택이다
    #   · 가장 오래된 접촉(64봉)을 고르면 기각으로 뒤집힌다  ← hits[-1] 뮤테이션
    #   · 게이트를 `< 60` 으로 바꿔도 기각으로 뒤집힌다      ← 경계 뮤테이션
    df2 = _t_snap_frame(700, step=0.02)
    t2 = _ma200_track(df2)
    cases.append(("T16 게이트 경계 (접촉 60~64봉 전)",
                  [("A", at(t2, 62)), ("B", at(t2, 10))],
                  ["✅ 스냅샷 가설 채택", "(60봉 전)"],
                  ["❌ 스냅샷 가설 기각", "(64봉 전)"]))

    orig = g["MA200_EXT_TARGETS"]
    try:
        for name, targets, want, unwant in cases:
            frame = df2 if name.startswith("T16") else df
            g["MA200_EXT_TARGETS"] = targets
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    block0f_snapshot(frame, None)
            except Exception as e:
                out.append(f"{name}: 예외 {type(e).__name__}: {e}")
                continue
            o = buf.getvalue()
            out += [f"{name}: 기대 문자열 없음 → {w!r}" for w in want if w not in o]
            out += [f"{name}: 나오면 안 되는 문자열 → {u!r}" for u in unwant
                    if u in o]
    finally:
        g["MA200_EXT_TARGETS"] = orig
    return out


def block_t_selftest() -> bool:
    """block0 의 분기를 스텁으로 전수 실행한다. True = 전부 통과."""
    import io
    from contextlib import redirect_stdout

    print("\n" + "=" * 78)
    print("[T] block0 자기검증 (스텁 · FMP 콜 0회)")
    print("=" * 78)

    g = globals()
    orig, fails = g["_fmp_rows_ex"], []
    try:
        for name, kw, want, unwant in _T_CASES:
            g["_fmp_rows_ex"] = _t_stub(**kw)
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    block0_datasource()
            except Exception as e:
                fails.append(f"{name}: 예외 {type(e).__name__}: {e}")
                continue
            o = buf.getvalue()
            fails += [f"{name}: 기대 문자열 없음 → {w!r}" for w in want if w not in o]
            fails += [f"{name}: 나오면 안 되는 문자열 → {u!r}" for u in unwant if u in o]
    finally:
        g["_fmp_rows_ex"] = orig

    # T7 — URL 조립. from/to 가 실제로 붙고 limit 과 섞이지 않는지.
    # ⚠️ 스텁이 아니라 진짜 `_fmp_rows_ex` 를 부른다(위 finally 로 복원된 뒤).
    #    fh.fmp_get_ex 만 가로채므로 네트워크는 나가지 않는다.
    seen, real = [], fh.fmp_get_ex
    fh.fmp_get_ex = lambda url, timeout=None: (seen.append(url),
                                               (None, None, "empty"))[1]
    try:
        _fmp_rows_ex("TEST", limit=600)
        _fmp_rows_ex("TEST", path="historical-price-eod/dividend-adjusted",
                     day_span=460)
    finally:
        fh.fmp_get_ex = real
    if len(seen) != 2:
        fails.append(f"T7: 호출 {len(seen)}회 (2여야 함)")
    else:
        if "&limit=600" not in seen[0] or "from=" in seen[0]:
            fails.append(f"T7: limit 경로 URL 이상 → {seen[0][:80]}")
        elif ("from=" not in seen[1] or "&to=" not in seen[1]
                or "limit=" in seen[1]):
            fails.append(f"T7: from/to 경로 URL 이상 → {seen[1][:80]}")
        else:
            _d = datetime.fromisoformat
            span = (_d(seen[1].split("&to=")[1].split("&")[0])
                    - _d(seen[1].split("from=")[1].split("&")[0])).days
            if span != 460:
                fails.append(f"T7: 창 폭 {span}일 (460이어야 함)")

    # ── T10~T15. 0-F 사전 기준 대조군 ────────────────────────────────────────
    # ⚠️ 심어놓은 값을 못 찾는 양성대조가 없으면, 0-F 가 그냥 아무것도 못 찾는
    #    깡통이어도 "기각"이 나가고 그것이 결론처럼 읽힌다. 그래서 **먼저**
    #    "있으면 반드시 찾는다"를 증명한 뒤에야 "없다"는 말에 무게가 생긴다.
    #    허용오차는 안쪽(0.04)과 바깥쪽(0.10) 양쪽에서 못 박는다 — 목표를 정확히
    #    맞춰 심으면(Δ=0) 오차 상수를 어떻게 바꿔도 통과해 뮤테이션을 놓친다.
    fails += _t_snapshot_controls()

    # T15. 0-F 는 "추가 FMP 콜 0회"가 계약이다. 나중에 누가 안에서 데이터를 더
    # 받아오면 그 순간 조용히 깨진다 — AST 로 호출 자체를 막는다.
    import ast as _ast
    import inspect as _inspect
    _banned = {"_fmp_rows", "_fmp_rows_ex", "_price_history", "fmp_get_ex",
               "fmp_get"}
    for fn in (block0f_snapshot, _snap_table, _snap_hits, _ma200_track):
        try:
            tree = _ast.parse(_inspect.getsource(fn))
        except Exception as e:                      # pragma: no cover
            fails.append(f"T15: {fn.__name__} 소스 파싱 실패 {e}")
            continue
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            f = node.func
            nm = (f.attr if isinstance(f, _ast.Attribute)
                  else getattr(f, "id", ""))
            if nm in _banned:
                fails.append(f"T15: {fn.__name__} 안에서 {nm}() 호출 — "
                             "0-F 는 콜 0회여야 한다")

    if fails:
        print(f"  ❌ block0 자기검증 실패 {len(fails)}건 — [0] 판정을 믿지 말 것")
        for f in fails:
            print("     - " + f)
        return False
    print(f"  ✅ {len(_T_CASES) + 8} 케이스 전부 통과 "
          "(T1·T2·T3·T3b·T3c·T4·T5·T6·T7·T8·T9 / "
          "0-F: T10·T11·T12·T13·T14·T15·T16)")
    return True

# ══════════════════════════════════════════════════════════════════════════════
# [0] MA200 격차 원인 규명
# ══════════════════════════════════════════════════════════════════════════════
def _dedupe(h: pd.DataFrame) -> pd.DataFrame:
    """같은 날짜가 두 번 오면 마지막 것만 남긴다.

    `app.py:6997` · `fmp_extras:669` · `run_hidden_alpha:193` ·
    `diag_satellite_backtest:231` 이 전부 하는 처리다. 하지 않으면 '최근 200봉'이
    200 거래일보다 **긴 구간**을 덮어 MA200 이 과거로 끌려간다(= 낮아진다).
    """
    if h.empty:
        return h
    return h[~h.index.duplicated(keep="last")]


def _price_field(rows, prefer=("adjClose", "close")) -> str | None:
    """행에 실제로 있는 가격 필드를 고른다. 없으면 None.

    ⚠️ 2026-08-27 실측에서 나왔다. `dividend-adjusted` 는 `kind=ok` 로 1,254행을
    돌려주는데도 0-D 가 "응답 없음"을 찍었다. 원인은 필드명이었다 — 이 경로는
    `adjClose` 로 오는데 `close` 를 하드코딩해 `_hist_from_rows` 가 빈
    DataFrame 을 냈고, 그것이 '응답 없음'과 구분되지 않았다.

    `diag_satellite_backtest:226` 이 이미 같은 처리를 한다
    (`col = "adjClose" if "adjClose" in df.columns else "close"`). 그 사실이
    레포에 있었는데도 여기서 다시 틀렸다 — 그래서 헬퍼로 뽑아 한 곳에 둔다.
    """
    if not rows or not isinstance(rows[0], dict):
        return None
    keys = rows[0].keys()
    for f in prefer:
        if f in keys:
            return f
    return None


def _ma200_row(name: str, h: pd.DataFrame, base: float | None) -> float:
    """소스 하나의 MA200 을 한 줄로 출력하고 MA200 값을 반환(실패 시 nan)."""
    if h.empty:
        print(f"  {name:<24}{'응답 없음':>10}")
        return np.nan
    if len(h) < 200:
        # ⚠️ '봉 부족'을 여기서 끝내지 않는다 — 몇 봉인지가 원인 판별의 핵심이다.
        print(f"  {name:<24}{'봉 부족':>10}{'':>10}{'':>10}{'':>12}"
              f"   ({len(h)}봉 / 200봉 필요)")
        return np.nan
    c = h["Close"]
    px, m50, m200 = float(c.iloc[-1]), rc._ma_last(c, 50), rc._ma_last(c, 200)
    gap = (px / m200 - 1.0) * 100.0 if np.isfinite(m200) and m200 > 0 else np.nan
    delta = ""
    if base is not None and np.isfinite(base) and np.isfinite(m200):
        delta = f"{m200 - base:+.2f}"
    print(f"  {name:<24}{px:>10.2f}{m50:>10.2f}{m200:>10.2f}{gap:>11.2f}%{delta:>10}")
    return m200


def _ma200_track(h: pd.DataFrame, scan: int = MA200_SNAP_SCAN) -> pd.Series:
    """과거 시점별 MA200 시계열(최근 `scan` 봉). 계산 불가면 빈 시리즈.

    ⚠️ `min_periods=200` 을 **엄격하게** 쓴다. 프로덕션(`rc._ma_last(...,
       min_p=150)`)과 일부러 다르다. 여기서 하는 일은 외부 사이트가 표시한
       'MA200' 과 같은 자를 대는 것이고, 150봉 평균을 MA200 이라 부르며 대조하면
       비교 자체가 성립하지 않는다.

    반환 시리즈의 인덱스는 **그 MA200 이 성립한 날짜**(창의 마지막 봉)다.
    """
    if h is None or h.empty or "Close" not in h.columns or len(h) < 200:
        return pd.Series(dtype=float)
    ser = h["Close"].rolling(200, min_periods=200).mean().dropna()
    if scan and len(ser) > scan:
        ser = ser.iloc[-scan:]
    return ser


def _snap_hits(ser: pd.Series, target: float,
               tol: float = MA200_SNAP_TOL) -> dict:
    """`target` 에 tol 안으로 닿은 지점을 찾는다.

    age = 시리즈 마지막 봉으로부터 몇 봉 전인가(0 = 최신). 날짜가 아니라 봉으로
    세는 이유: 사전 기준(b)이 봉 단위이고, 휴장일이 섞이면 캘린더 일수와 봉 수가
    어긋나 같은 기준이 종목마다 다른 뜻이 된다.
    """
    out = {"n": 0, "hit_age": None, "hit_date": None, "hit_val": None,
           "near_age": None, "near_date": None, "near_val": None,
           "near_delta": None, "lo": np.nan, "hi": np.nan}
    if ser is None or ser.empty:
        return out
    last = len(ser) - 1
    d = (ser - float(target)).abs().values
    out["lo"], out["hi"] = float(ser.min()), float(ser.max())
    hits = [i for i, v in enumerate(d) if v <= tol]
    out["n"] = len(hits)
    if hits:
        i = hits[-1]                      # 가장 최근 접촉 = age 가 가장 작은 것
        out["hit_age"] = last - i
        out["hit_date"] = ser.index[i]
        out["hit_val"] = float(ser.iloc[i])
    j = int(np.argmin(d))
    out["near_age"] = last - j
    out["near_date"] = ser.index[j]
    out["near_val"] = float(ser.iloc[j])
    out["near_delta"] = float(ser.iloc[j] - float(target))
    return out


def _snap_table(label: str, ser: pd.Series, n_bars: int, *,
                decides: bool) -> list:
    """한 시리즈에 대한 목표별 접촉 표를 출력하고 결과 dict 리스트를 반환."""
    tag = "판정" if decides else "참고"
    print(f"\n  [{tag}] {label} — 원본 {n_bars}봉 → MA200 점 {len(ser)}개"
          f" (스캔 최근 {MA200_SNAP_SCAN}봉)")
    if ser.empty:
        print(f"    ⚠️ MA200 시계열을 만들 수 없다 ({n_bars}봉 < 200봉)")
        return []
    print(f"    스윕 범위                    : {float(ser.min()):.2f}"
          f" ~ {float(ser.max()):.2f}"
          f"   ({ser.index[0].date()} ~ {ser.index[-1].date()})")

    res = []
    for name, tgt in MA200_EXT_TARGETS:
        r = _snap_hits(ser, tgt)
        r["name"], r["target"] = name, tgt
        res.append(r)
        head = f"    {name} {tgt:.2f}"
        if r["hit_age"] is None:
            print(f"{head:<34}: 접촉 없음 (±{MA200_SNAP_TOL})"
                  f"   최근접 {r['near_val']:.2f}"
                  f" ({r['near_delta']:+.2f}, {r['near_date'].date()},"
                  f" {r['near_age']}봉 전)")
        else:
            gate = "✅ 60봉 이내" if r["hit_age"] <= MA200_SNAP_RECENT \
                   else f"❌ {MA200_SNAP_RECENT}봉 초과"
            print(f"{head:<34}: 접촉 {r['n']}개"
                  f"   최근 접촉 {r['hit_val']:.2f}"
                  f" ({r['hit_date'].date()}, {r['hit_age']}봉 전)  {gate}")
    return res


def block0f_snapshot(ded: pd.DataFrame, alt_h: pd.DataFrame | None) -> None:
    """0-F. 과거 시점별 MA200 을 되짚어 외부값이 '언제의 값'인지 찾는다.

    FMP 콜 0회 — 0-A/0-C 에서 이미 받은 행을 그대로 재사용한다.
    사전 확정 기준은 `MA200_SNAP_*` 상수에 있으며 결과를 본 뒤 바꾸지 않는다.
    """
    print("\n  ── 0-F. 과거 MA200 스냅샷 추적 (FMP 콜 0회) ──")
    print(f"  사전 기준: raw close 단독 판정 · |Δ| ≤ {MA200_SNAP_TOL}"
          f" · 두 날짜 모두 최근 {MA200_SNAP_RECENT}봉 이내")

    ser = _ma200_track(ded)
    res = _snap_table("close (중복제거)", ser, len(ded), decides=True)

    # 참고용. (c) 에 따라 판정에 관여하지 않는다 — 여기 결과로 판정을 뒤집으면
    # 기회가 두 배가 되어 사전 기준이 느슨해진다.
    if alt_h is not None and not alt_h.empty:
        _snap_table("dividend-adjusted", _ma200_track(alt_h), len(alt_h),
                    decides=False)
        print("    ↑ 참고 전용 — (c)에 따라 판정에 관여하지 않는다")

    # ── 검정력 자기고발 ─────────────────────────────────────────────────────
    # 판정보다 **먼저** 찍는다. 두 목표가 스윕 범위 안에 통째로 들어가면 '등장'은
    # 사실상 보장되고, 그때 판정을 가르는 것은 오직 날짜 게이트다. 이 줄이 없으면
    # 다음 사람이 통과를 곧바로 '스냅샷 확인'으로 오독한다.
    print("\n    ── 검정력 ──")
    if ser.empty:
        print("    ⚠️ 판정 시리즈가 비어 검정력을 말할 수 없다")
    else:
        lo, hi = float(ser.min()), float(ser.max())
        inside = [f"{t:.2f}" for _, t in MA200_EXT_TARGETS if lo <= t <= hi]
        if len(inside) == len(MA200_EXT_TARGETS):
            print("    ⚠️ 검정력 경고: 두 목표값이 모두 스윕 범위 안에 있다 —"
                  " '등장' 자체는 거의 보장된다.")
            print("       통과해도 그것만으로는 스냅샷의 증거가 아니다."
                  " 판정을 가르는 것은 날짜 게이트다.")
        elif inside:
            print(f"    스윕 범위 안: {', '.join(inside)} / 밖: "
                  + ", ".join(f"{t:.2f}" for _, t in MA200_EXT_TARGETS
                              if not (lo <= t <= hi)))
        else:
            print("    ✅ 두 목표값 모두 스윕 범위 밖 — 이 검정은 변별력이 있다")

    # ── 사전 기준 대입 ──────────────────────────────────────────────────────
    print("\n    ── 사전 기준 대입 ──")
    if not res:
        print("    ⛔ 판정 불가 — 판정 시리즈에서 MA200 을 만들 수 없다")
        print("       (데이터 부족이지 가설의 기각이 아니다)")
        return

    short = len(ser) < MA200_SNAP_SCAN
    if short:
        print(f"    ⚠️ 창 부족: MA200 점이 {len(ser)}개로 "
              f"{MA200_SNAP_SCAN}봉에 미달한다")

    passed = [r for r in res
              if r["hit_age"] is not None and r["hit_age"] <= MA200_SNAP_RECENT]
    missing = [r for r in res if r["hit_age"] is None]

    if len(passed) == len(res):
        print("    ✅ 스냅샷 가설 채택 — 두 값 모두 ±"
              f"{MA200_SNAP_TOL} 안에서, 최근 {MA200_SNAP_RECENT}봉 이내에 나왔다")
        for r in passed:
            print(f"       {r['name']} {r['target']:.2f}"
                  f" → {r['hit_date'].date()} ({r['hit_age']}봉 전)")
        print("       → 외부값은 다른 날짜의 우리 MA200 이다. 우리 계산은 틀리지 않았다.")
    elif missing and short:
        # 창이 짧아 못 본 구간이 있는데 '없다'고 단정하면 데이터 부족을 가설
        # 기각으로 둔갑시키는 것이다. 여기서만 판정을 보류한다.
        print("    ⛔ 판정 보류 — 창이 짧고 접촉이 없는 목표가 있다: "
              + ", ".join(r["name"] for r in missing))
        print(f"       {MA200_SNAP_SCAN}봉을 덮는 시리즈를 확보한 뒤 다시 돌릴 것")
    else:
        print("    ❌ 스냅샷 가설 기각 — 사전 기준을 충족하지 못했다")
        for r in res:
            if r["hit_age"] is None:
                print(f"       {r['name']} {r['target']:.2f}: "
                      f"±{MA200_SNAP_TOL} 안에 한 번도 닿지 않았다"
                      f" (최근접 {r['near_delta']:+.2f})")
            elif r["hit_age"] > MA200_SNAP_RECENT:
                print(f"       {r['name']} {r['target']:.2f}: 닿은 것은 "
                      f"{r['hit_age']}봉 전 — {MA200_SNAP_RECENT}봉 게이트 밖")
        print("    → MA200 격차 건 종료. 남은 후보 가설이 없다.")
        print("       (외부 두 값이 서로 2.1% 다르다는 사실 자체가"
              " '외부 = 단일 정답' 전제를 이미 무너뜨린다)")


def block0_datasource() -> None:
    """MA200 격차 원인 규명 — FMP 3콜, 읽기 전용.

    배경: 우리 NEE MA200 = 87.87 인데 외부는 89.77(TickerReport) /
    91.64(Investing.com) 이다. 인수인계는 이것을 '배당조정 미반영'으로 진단했으나
    **부호가 반대다** — 배당조정(후방조정)은 과거 가격을 낮추므로 배당조정 MA200 은
    미조정보다 **작아진다.** 외부가 우리보다 높은 것을 설명할 수 없다.

    그래서 이 블록은 배당조정을 '도입'하는 게 아니라 **가설을 기각하거나 채택**하고,
    동시에 다른 후보(중복 날짜 · 창 길이 · min_periods 완화)를 같은 화면에서 가른다.
    """
    print("\n" + "=" * 78)
    print(f"[0] MA200 격차 원인 규명 ({PROBE_TICKER})")
    print("=" * 78)

    rows, kind = _fmp_rows_ex(PROBE_TICKER, limit=HOLD_LIMIT)
    if not rows:
        print(f"  ❌ {PROBE_TICKER} 응답 없음 (kind={kind}) — 이후 블록도 신뢰 불가")
        return

    keys = sorted(rows[0].keys()) if isinstance(rows[0], dict) else []
    has_adj = "adjClose" in keys

    # ── 0-A. 원본 시리즈 상태 ────────────────────────────────────────────────
    print("\n  ── 0-A. 원본 시리즈 상태 ──")
    print(f"  historical-price-eod/full 필드 : {', '.join(keys)}")
    print(f"  adjClose 필드                  : {'있음 ✅' if has_adj else '없음 ❌'}")

    raw = _hist_from_rows(rows, "close")
    ded = _dedupe(raw)
    n_dup = len(raw) - len(ded)
    print(f"  총 행 수                       : {len(raw)}")
    print(f"  고유 날짜 수                   : {len(ded)}")
    print(f"  중복 날짜                      : {n_dup} 건"
          + ("   ⚠️ 200봉 창이 200거래일보다 길어진다" if n_dup else "   ✅"))
    if not raw.empty:
        print(f"  최신 봉 날짜                   : {raw.index[-1].date()}")

    # ── 0-B. MA200 창 검증 ──────────────────────────────────────────────────
    # 외부 사이트와 '같은 200봉'을 보고 있는지 확인한다. 창의 시작일이 다르면
    # 배당조정이 아니라 창 자체가 원인이다.
    print("\n  ── 0-B. MA200 창 검증 ──")
    if len(ded) >= 200:
        win = ded.index[-200:]
        span = (win[-1] - win[0]).days
        # 거래일 → 캘린더일 근사 (§8: × 0.690). 200거래일 ≈ 290일.
        expect = int(round(200 / 0.690))
        print(f"  최근 200봉 구간                : {win[0].date()} ~ {win[-1].date()}")
        print(f"  구간 캘린더 일수               : {span}일  (기대 ≈{expect}일)")
        verdict = ("✅ 정상" if abs(span - expect) <= 20
                   else "⚠️ 창이 어긋난다 — 결측/중복 의심")
        print(f"  판정                           : {verdict}")
    else:
        print(f"  ⚠️ 고유 봉이 {len(ded)}개뿐 — 200봉 창을 만들 수 없다")

    # 프로덕션(`classify_regime`)은 `_ma_last(close, 200, min_p=150)` 을 쓴다.
    # 봉이 150~199개면 200봉이 아닌 평균이 'ma200' 이라는 이름으로 나가고,
    # 그 값이 `gap_ma200_pct` 가 되어 3E 램프에 그대로 들어간다.
    #
    # ⚠️ 여기서 두 값을 비교하지 않는다. 봉이 200 이상이면 둘은 **항상** 같고
    #    미만이면 엄격 쪽이 NaN 이라, 비교식은 실행돼도 판정에 관여하지 못하는
    #    죽은 코드가 된다(뮤테이션 M-d 로 실증). 판정하는 것은 봉 수뿐이다.
    n_uniq = len(ded)
    if 0 < n_uniq < rc.W52_BARS:
        loose = rc._ma_last(ded["Close"], 200, min_p=150)
        if not np.isfinite(loose):
            print(f"  ⚠️ 봉이 {n_uniq}개뿐 — 프로덕션에서도 ma200 이 NaN 이다 "
                  "(§3-1: regime 이 weak 로 떨어진다)")
        elif n_uniq < 200:
            print(f"  ⚠️ 프로덕션 ma200 = {loose:.2f} 이지만 200봉 평균이 아니다 "
                  f"({n_uniq}봉 · min_periods=150)")
            print("     → 이 값이 그대로 gap_ma200_pct 가 되어 3E 램프에 들어간다")
        else:
            # 200~251봉. ma200 자체는 진짜 200봉 평균이라 정확하다. 그러나 52주
            # 창(rc.W52_BARS=252)이 짧아 고점대비·낙폭 입력이 함께 degraded 다.
            # ⚠️ 여기서 "200봉 평균이 아니다"라고 쓰면 **거짓말**이 된다.
            print(f"  ⚠️ ma200 = {loose:.2f} 은 정상(200봉 충족)이나 봉이 "
                  f"{n_uniq}개로 52주({rc.W52_BARS}봉)에 미달한다")
            print("     → 고점대비·낙폭 입력이 짧은 창에서 나온다")

    # ── 0-C. dividend-adjusted 엔드포인트 진단 ──────────────────────────────
    # '봉 부족'의 원인이 플랜 제한인지, 레이트리밋인지, 파라미터인지 가른다.
    print("\n  ── 0-C. dividend-adjusted 엔드포인트 ──")
    _DIV = "historical-price-eod/dividend-adjusted"
    alt_lim, k_lim = _fmp_rows_ex(PROBE_TICKER, limit=HOLD_LIMIT, path=_DIV)
    print(f"  limit={HOLD_LIMIT} 호출            : kind={k_lim}, "
          f"{len(alt_lim) if alt_lim else 0}행")
    alt_win, k_win = _fmp_rows_ex(PROBE_TICKER, path=_DIV, day_span=460)
    print(f"  from/to 460일 호출             : kind={k_win}, "
          f"{len(alt_win) if alt_win else 0}행")
    n_lim = len(alt_lim) if alt_lim else 0
    n_win = len(alt_win) if alt_win else 0
    if not alt_lim and not alt_win:
        print(f"  → 엔드포인트 자체를 못 쓴다 (kind={k_lim}/{k_win})")
    elif n_lim > n_win:
        # 2026-08-27 실측: limit=600 을 요청했는데 1,254행이 왔다. `limit` 이
        # 무시되면 창이 짧아지는 게 아니라 **전체가 온다.** 이전 서술은 반대로
        # 적혀 있었고, [T] 픽스처도 그 반대 세계를 고정하고 있었다.
        print(f"  → `limit` 이 무시되고 전체 이력이 왔다 ({n_lim}행) — §7 과 일치")
    elif n_win > n_lim:
        print(f"  → from/to 가 더 긴 시리즈를 준다 ({n_win} > {n_lim}행)")

    # 더 긴 쪽을 쓴다. 짧은 쪽을 고르면 MA200 표본이 이유 없이 줄어든다.
    alt = alt_lim if n_lim >= n_win else alt_win
    alt_field = _price_field(alt)
    if alt:
        which = "limit" if alt is alt_lim else "from/to"
        print(f"  사용 시리즈                    : {which} 경로 {len(alt)}행")
        print(f"  사용 필드                      : {alt_field or '없음 ❌'}"
              f"  ({', '.join(sorted(alt[0].keys()))})")

    # ── 0-D. 소스별 MA200 ───────────────────────────────────────────────────
    print("\n  ── 0-D. 소스별 MA200 ──")
    print(f"  {'소스':<24}{'종가':>10}{'MA50':>10}{'MA200':>10}"
          f"{'MA200이격':>12}{'기준대비':>10}")
    print("  " + "-" * 76)

    base = _ma200_row("close (현재 사용)", raw, None)
    variants = [("close (중복제거)", ded)]
    if has_adj:
        variants.append(("adjClose", _dedupe(_hist_from_rows(rows, "adjClose"))))
    # 0-F 가 재사용한다 — 다시 부르면 콜이 늘어난다. 여기서 만든 것을 넘긴다.
    alt_h = None
    if alt and alt_field:
        alt_h = _dedupe(_hist_from_rows(alt, alt_field))
        variants.append((f"dividend-adjusted ({alt_field})", alt_h))
    elif alt:
        # 행은 왔는데 쓸 가격 필드가 없다. '응답 없음'과 반드시 구분한다.
        print(f"  {'dividend-adjusted':<24}{'가격 필드 없음':>10}"
              f"   (받은 필드: {', '.join(sorted(alt[0].keys()))})")
    adj_vals = []
    for name, h in variants:
        v = _ma200_row(name, h, base if np.isfinite(base) else None)
        if name != "close (중복제거)":
            adj_vals.append(v)

    # ── 0-E. 판정 ───────────────────────────────────────────────────────────
    print("\n  ── 0-E. 외부 대조 및 가설 판정 ──")
    print("  외부 보고치: 89.77(TickerReport) / 91.64(Investing.com)")
    print("  ⚠️ 외부 두 값이 1.87(2.1%) 다르다 — 단일 정답이 아니며 자로 쓸 수 없다.")
    fin = [v for v in adj_vals if np.isfinite(v)]
    if np.isfinite(base) and fin:
        if all(v <= base + 1e-9 for v in fin):
            print("  ✅ 배당조정 가설 기각 — 배당조정 MA200 이 미조정보다 낮거나 같다.")
            print("     외부가 우리보다 높은 것을 배당조정으로는 설명할 수 없다.")
        else:
            print("  ❗ 배당조정 MA200 이 미조정보다 높다 — 예상 밖. 조정 방향을 재확인할 것.")
    elif not fin:
        print("  ⚠️ 비교 대상 소스가 하나도 안 나왔다 — 위 0-C 의 kind 를 볼 것.")

    # ── 0-F. 마지막 후보: 외부값이 다른 날짜의 스냅샷인가 ───────────────────
    block0f_snapshot(ded, alt_h)


# ══════════════════════════════════════════════════════════════════════════════
# [1] 보유 종목 스냅샷
# ══════════════════════════════════════════════════════════════════════════════
def block1_snapshot(holds: list, hist: dict) -> dict:
    print("\n" + "=" * 78)
    print("[1] 보유 종목 현재 스냅샷 — 판정 입력 전체")
    print("=" * 78)
    print(f"  {'종목':<7}{'계좌':<14}{'종가':>9}{'MA50':>9}{'MA200':>9}"
          f"{'이격200':>9}{'1개월':>8}{'RSI':>6}{'MACD':>13}{'보유낙폭':>9}{'샹들리에':>10}")
    print("  " + "-" * 105)

    inputs = {}
    for uid, acct, tk, avg, dadd in holds:
        h = hist.get(tk)
        if h is None or h.empty:
            print(f"  {tk:<7}{acct[:13]:<14}{'데이터 없음':>9}")
            continue
        inp = verdict_inputs(h, avg, dadd)
        if not inp:
            print(f"  {tk:<7}{acct[:13]:<14}{'봉 부족':>9}")
            continue
        inputs[(uid, acct, tk)] = (inp, avg, dadd)
        dd = inp["drawdown"]
        print(f"  {tk:<7}{acct[:13]:<14}{inp['price']:>9.2f}{inp['ma50']:>9.2f}"
              f"{inp['ma200']:>9.2f}{inp['gap200']:>8.1f}%{inp['one_month']:>7.1f}%"
              f"{inp['rsi']:>6.0f}{inp['macd']:>13}"
              f"{(f'{dd:.1f}%' if pd.notna(dd) else 'N/A'):>9}"
              f"{inp['chandelier']:>10.2f}")
        if inp["dd_error"]:
            print(f"  {'':<7}⚠️ 낙폭 데이터 오류 의심 (원값 {inp['dd_raw']:.1f}%)")

    print("\n  ※ 샹들리에는 '최근 22일 고점 vs 평단 중 큰 값' 기준 — 종가가 이보다 낮으면")
    print("    스윙 청산이 발동한다. 눌린 종목을 매수하면 진입 즉시 위반 상태가 된다.")
    return inputs


# ══════════════════════════════════════════════════════════════════════════════
# [2] 진입 시점 재구성
# ══════════════════════════════════════════════════════════════════════════════
def block2_entry_baseline(inputs: dict, hist: dict, spy_hist: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print("[2] 진입 시점 재구성 — 2A(진입 후 신규 발생분만 발동) 효과 판정")
    print("=" * 78)

    spy_close = spy_hist["Close"] if (spy_hist is not None and not spy_hist.empty) else None

    for (uid, acct, tk), (inp_now, avg, dadd) in inputs.items():
        h = hist.get(tk)
        print(f"\n  ── {tk} ({acct}) · 평단 {avg if avg else 'N/A'} · Date_Added {dadd or '미기록'}")
        if not dadd:
            print("     ⚠️ Date_Added 없음 → 재구성 불가 (2A 적용 대상에서 제외됨)")
            continue
        buy_dt = pd.to_datetime(str(dadd)[:10], errors="coerce")
        if pd.isna(buy_dt):
            print(f"     ⚠️ Date_Added 파싱 실패: {dadd!r}")
            continue

        sliced = h[h.index <= buy_dt]
        if len(sliced) < 210:
            print(f"     ⚠️ 진입일 이전 봉 {len(sliced)}개 — MA200 산출 불가(210봉 필요)")
            continue

        ex_then = rc.compute_exit_signals(sliced, entry_price=avg)
        sc = spy_close[spy_close.index <= buy_dt] if spy_close is not None else None
        try:
            reg_then = rc.classify_regime(sliced, spy_close=sc)
            tim_then = rc.evaluate_timing(sliced, reg_then)
        except Exception as e:
            reg_then, tim_then = {}, {}
            print(f"     [WARN] 레짐/타이밍 재구성 실패: {e}")

        c_then = sliced["Close"]
        px_t, m50_t, m200_t = float(c_then.iloc[-1]), rc._ma_last(c_then, 50), rc._ma_last(c_then, 200)
        codes_then = set(ex_then.get("codes") or [])
        if str(tim_then.get("code") or "") == "trend_break":
            codes_then.add("trend_break")

        ex_now = rc.compute_exit_signals(h, entry_price=avg)
        reg_now = rc.classify_regime(h, spy_close=spy_close)
        tim_now = rc.evaluate_timing(h, reg_now)
        codes_now = set(ex_now.get("codes") or [])
        if str(tim_now.get("code") or "") == "trend_break":
            codes_now.add("trend_break")

        print(f"     진입일 종가 {px_t:.2f} · MA50 {m50_t:.2f} · MA200 {m200_t:.2f}"
              f"  → 50일선 {'아래' if px_t < m50_t else '위'} / 200일선 {'아래' if px_t < m200_t else '위'}")
        print(f"     진입 시점 코드: {sorted(codes_then) or '없음'}")
        print(f"     오늘   코드: {sorted(codes_now) or '없음'}")

        new = codes_now - codes_then
        pre = codes_now & codes_then
        if pre:
            print(f"     🔇 2A 억제 대상(진입 시 이미 참): {sorted(pre)}")
        if new:
            print(f"     🔔 2A 적용 후에도 발동: {sorted(new)}")
        else:
            print("     ✅ 2A 적용 시 이 종목의 스윙/risk 알림은 발동하지 않음")


# ══════════════════════════════════════════════════════════════════════════════
# [3] 점수 시나리오 비교
# ══════════════════════════════════════════════════════════════════════════════
def block3_scenarios(inputs: dict) -> bool:
    print("\n" + "=" * 78)
    print("[3] 포지션 점수 시나리오 비교")
    print("=" * 78)
    print(f"  현재  : SSOT 그대로 (청산 문턱 {SELL_TH_DEFAULT} · 줄이기 {TRIM_TH_DEFAULT})")
    print(f"  문턱5 : 미채택 시나리오 — 청산 문턱만 {SELL_TH_ALT} 로")
    print()
    print(f"  {'종목':<7}{'현재':>16}{'문턱5':>16}")
    print("  " + "-" * 45)

    mismatch = []
    for (uid, acct, tk), (inp, avg, dadd) in inputs.items():
        s_cur, comp = score_components(inp)
        print(f"  {tk:<7}{f'{s_cur:g} ' + label_for(s_cur):>16}"
              f"{f'{s_cur:g} ' + label_for(s_cur, sell_th=SELL_TH_ALT):>16}")
        print(f"  {'':<7}└ 분해: 200일선 {comp['ma200']:g} · 트레일링 {comp['trailing']:g}"
              f" · MACD {comp['macd']:g} · 1개월 {comp['month']:g} · 과열 {comp['overheat']:g}")

        # SSOT 대조 — 실보유 데이터로도 확인한다.
        # ⚠️ gap_ma200_pct 를 반드시 넘긴다. 안 넘기면 SSOT 가 일괄 4점 폴백으로
        #    가는데, 프로덕션(app.py:10834 · regime_core:2407)은 넘긴다.
        #    즉 안 넘기면 **프로덕션이 아닌 설정과 대조**하게 된다.
        ssot_label, _ = rc.integrated_sell_verdict(
            above_ma200=inp["above_ma200"], one_month_return=inp["one_month"],
            rsi=inp["rsi"], macd_signal=inp["macd"], pct_from_52w_high=inp["pct52"],
            drawdown_from_high_pct=inp["drawdown"], gap_ma200_pct=inp.get("gap200"),
        )
        mine = label_for(s_cur)
        if mine[0] != ssot_label[0]:
            mismatch.append((tk, mine, ssot_label))

    if mismatch:
        print("\n  ❌ SSOT 대조 실패(실보유) — 진단 배점이 regime_core 와 다르다:")
        for tk, mine, ssot in mismatch:
            print(f"     {tk}: 진단 {mine} vs regime_core {ssot}")
        print("     → [3] 결과를 신뢰하지 말 것. 진단 스크립트를 먼저 고쳐야 한다.")
        return False
    print("\n  ✅ SSOT 대조 통과(실보유) — regime_core.integrated_sell_verdict 와 일치")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# [4] MA200 이격 분포
# ══════════════════════════════════════════════════════════════════════════════
def block4_distribution(tickers: list) -> None:
    print("\n" + "=" * 78)
    print(f"[4] MA200 이격 분포 — 표본 {len(tickers)}종목 "
          f"(SSOT 램프: 이격 0% → {rc.MA200_GAP_BASE_SCORE:g}점, "
          f"{rc.MA200_GAP_FULL_PCT:g}% 이상 → 4점)")
    print("=" * 78)
    if not tickers:
        print("  표본 없음")
        return

    gaps = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
        futs = {ex.submit(_price_history, tk, SCAN_LIMIT): tk for tk in tickers}
        for fut in concurrent.futures.as_completed(futs):
            tk = futs[fut]
            try:
                h = fut.result()
                if h is None or h.empty or len(h) < 200:
                    continue
                c = h["Close"]
                m200 = rc._ma_last(c, 200)
                if np.isfinite(m200) and m200 > 0:
                    gaps[tk] = (float(c.iloc[-1]) / m200 - 1.0) * 100.0
            except Exception:
                continue

    if not gaps:
        print("  유효 응답 없음")
        return

    s = pd.Series(gaps).sort_values()
    below = s[s < 0]
    print(f"  유효 {len(s)}/{len(tickers)}종목 · 200일선 아래 {len(below)}종목 "
          f"({len(below) / len(s) * 100:.0f}%)")

    print(f"\n  전체 분위:  " + "  ".join(
        f"{int(q * 100)}%={s.quantile(q):+.1f}" for q in (0.05, 0.25, 0.50, 0.75, 0.95)))

    if len(below):
        print(f"  이탈분 분위: " + "  ".join(
            f"{int(q * 100)}%={below.quantile(q):+.1f}" for q in (0.05, 0.25, 0.50, 0.75, 0.95)))
        # ⚠️ 구간 경계를 하드코딩하지 않는다. 삭제된 3E 계단(-3/-8)이 여기
        #    남아 "제안 계단별 분포"로 계속 출력되고 있었다. SSOT 램프 상수에서
        #    유도해 상수가 바뀌면 표도 따라오게 한다.
        _full = float(rc.MA200_GAP_FULL_PCT)
        _half = _full / 2.0
        buckets = [
            (f"0 ~ -{_half:g}%  (2.0~3.0점)", ((below > -_half).sum())),
            (f"-{_half:g} ~ -{_full:g}% (3.0~4.0점)",
             (((below <= -_half) & (below > -_full)).sum())),
            (f"-{_full:g}% 초과 (4.0점 포화)", ((below <= -_full).sum()))]
        print("\n  SSOT 램프 구간별 분포(200일선 아래 종목만):")
        for name, n in buckets:
            pct = n / len(below) * 100
            print(f"    {name:<24}{n:>4}종목 ({pct:>5.1f}%)  {'█' * int(pct / 2)}")
        print(f"\n  → -{_full:g}% 초과분은 이격이 더 벌어져도 점수가 안 변한다(포화).")
        print("    거기 표본이 몰려 있으면 램프가 사실상 일괄 4점처럼 작동한다는 뜻.")
        print("    현재 단면은 시장 국면에 따라 통째로 이동하므로 과적합에 주의.")


# ══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    print("=" * 78)
    print("매도 판정 진단 (읽기 전용 · 시트 기록 없음 · 수정 없음)")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 78)

    # ⚠️ 네트워크·시트보다 **먼저** 돌린다. 복제본이 SSOT 와 갈라져 있으면
    #    아래 블록의 숫자가 전부 무의미하므로, 키가 없어 중단되는 경우에도
    #    이 대조 결과만은 남아야 한다.
    ok_grid = block_s_ssot_grid()

    # [T] 도 콜 0회다. [0] 의 판정을 읽기 전에 [0] 자신을 먼저 검증한다 —
    # 순서가 반대면 이미 화면에 뿌려진 숫자를 나중에 부정하게 된다.
    ok_t = block_t_selftest()

    if not FMP_API_KEY:
        print("❌ FMP_API_KEY 없음 — 중단")
        return 1

    block0_datasource()

    if not GSPREAD_KEY_JSON:
        print("\n⚠️ GSPREAD_KEY 없음 — [1]~[3] 건너뜀 (보유 종목을 읽을 수 없음)")
        # ⚠️ 무조건 0 을 내던 자리다. 시트 키가 없는 실행에서는 [S]/[T] 가
        #    깨져도 초록불이 나갔다 — 자기검증을 켜 둔 의미가 없어진다.
        if not (ok_grid and ok_t):
            # 종료 코드만 1 로 내보내면 로그에서 'GSPREAD_KEY 없음' 이 원인처럼
            # 읽힌다. 진짜 원인을 한 줄로 못 박는다.
            bad = [n for n, v in (("[S] SSOT 격자", ok_grid),
                                  ("[T] block0 자기검증", ok_t)) if not v]
            print("⚠️ 자기검증 실패: " + " · ".join(bad)
                  + " — 시트 키 부재가 아니라 이것이 실패 원인이다.")
        return 0 if (ok_grid and ok_t) else 1

    try:
        sh = _sheet()
    except Exception as e:
        print(f"\n❌ Quant_DB 접속 실패: {e}")
        return 1

    holds = read_holdings(sh)
    print(f"\n[INFO] 보유 종목 {len(holds)}건" + (f" (uid={ONLY_UID} 필터)" if ONLY_UID else ""))

    hist = {}
    hold_tickers = sorted({tk for _, _, tk, _, _ in holds})
    with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
        futs = {ex.submit(_price_history, tk, HOLD_LIMIT): tk for tk in hold_tickers}
        for fut in concurrent.futures.as_completed(futs):
            tk = futs[fut]
            try:
                hist[tk] = fut.result()
            except Exception:
                hist[tk] = pd.DataFrame()

    spy_hist = _price_history("SPY", HOLD_LIMIT)

    inputs = block1_snapshot(holds, hist)
    block2_entry_baseline(inputs, hist, spy_hist)
    ok3 = block3_scenarios(inputs)

    universe = sorted(set(hold_tickers) | set(read_watchlist_tickers(sh)) | set(satellite_tickers()))
    block4_distribution(universe)

    print("\n" + "=" * 78)
    print("진단 종료 — 아무것도 수정하지 않았습니다.")
    if not (ok_grid and ok_t and ok3):
        bad = [n for n, v in (("[S] SSOT 격자", ok_grid),
                              ("[T] block0 자기검증", ok_t),
                              ("[3] 시나리오 SSOT", ok3)) if not v]
        print("⚠️ 자기검증 실패: " + " · ".join(bad))
        print("   위 결과를 신뢰하지 마십시오.")
    print("=" * 78)
    # SSOT 가 갈라진 채로 초록불을 내면 다음에도 못 잡는다. 빨간불로 남긴다.
    return 0 if (ok_grid and ok_t and ok3) else 1


if __name__ == "__main__":
    sys.exit(main())
