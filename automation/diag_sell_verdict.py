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
def _t_rows(n, start_px=80.0, dup=0, adj=True, step=0.05, adj_mult=0.985):
    """합성 일봉 n개(거래일만). dup>0 이면 앞쪽 dup개 날짜를 중복시킨다."""
    out, d, got = [], datetime(2026, 8, 27).date(), 0
    while got < n:
        if d.weekday() < 5:
            px = start_px + got * step
            r = {"date": str(d), "open": px, "high": px, "low": px,
                 "close": round(px, 4), "volume": 1000}
            if adj:
                r["adjClose"] = round(px * adj_mult, 4)
            out.append(r)
            got += 1
        d -= timedelta(days=1)
    for i in range(dup):
        out.insert(0, dict(out[i]))
    return out


def _t_stub(full_n, dup=0, adj=True, adj_mult=0.985, div_lim=None,
            div_win=None, div_kind="empty", full_kind="ok"):
    """block0 이 부르는 `_fmp_rows_ex` 를 대신할 스텁을 만든다."""
    def stub(t, limit=None, path="historical-price-eod/full", day_span=None):
        if "dividend-adjusted" in path:
            n = div_lim if day_span is None else div_win
            return (_t_rows(n, 78.8, adj=False), "ok") if n else (None, div_kind)
        if full_kind != "ok":
            return None, full_kind
        return _t_rows(full_n, dup=dup, adj=adj, adj_mult=adj_mult), "ok"
    return stub


# (이름, block0 입력, 반드시 나와야 할 문자열, 절대 나오면 안 될 문자열)
_T_CASES = [
    ("T1 정상 600봉", dict(full_n=600, div_lim=100, div_win=330),
     ["중복 날짜                      : 0 건", "✅ 정상",
      "원인 확정: `limit` 이 무시되고", "close (중복제거)",
      "adjClose", "dividend-adjusted", "✅ 배당조정 가설 기각"],
     ["❗ 배당조정 MA200 이 미조정보다 높다"]),
    ("T2 중복 7건", dict(full_n=600, dup=7, div_kind="plan_limited"),
     ["중복 날짜                      : 7 건",
      "⚠️ 200봉 창이 200거래일보다 길어진다",
      "엔드포인트 자체를 못 쓴다 (kind=plan_limited/plan_limited)"], []),
    ("T3 170봉", dict(full_n=170),
     ["고유 봉이 170개뿐 — 200봉 창을 만들 수 없다",
      "200봉 평균이 아니다 (170봉 · min_periods=150)",
      "그대로 gap_ma200_pct 가 되어 3E 램프에 들어간다",
      "봉 부족", "(170봉 / 200봉 필요)"],
     ["프로덕션에서도 ma200 이 NaN"]),
    ("T3b 130봉", dict(full_n=130),
     ["봉이 130개뿐 — 프로덕션에서도 ma200 이 NaN"],
     ["그대로 gap_ma200_pct 가 되어"]),
    # ⚠️ T3c 는 2026-08-27 추가. 200~251봉 구간에서 "200봉 평균이 아니다"라는
    #    **거짓 문장**이 나가던 것을 잡는다. 이 구간은 ma200 자체는 정상이고
    #    52주 창만 짧다. 이 케이스가 없으면 그 오보를 아무도 못 잡는다.
    ("T3c 220봉", dict(full_n=220),
     ["은 정상(200봉 충족)이나 봉이 220개로 52주(252봉)에 미달한다",
      "고점대비·낙폭 입력이 짧은 창에서 나온다"],
     ["200봉 평균이 아니다", "프로덕션에서도 ma200 이 NaN"]),
    ("T4 조정치가 더 높음", dict(full_n=600, adj_mult=1.05),
     ["❗ 배당조정 MA200 이 미조정보다 높다"], ["✅ 배당조정 가설 기각"]),
    ("T5 첫 호출 실패", dict(full_n=600, full_kind="rate_limited"),
     ["응답 없음 (kind=rate_limited)"], ["0-D. 소스별 MA200"]),
    ("T6 adjClose 없음", dict(full_n=600, adj=False),
     ["adjClose 필드                  : 없음 ❌",
      "비교 대상 소스가 하나도 안 나왔다"], []),
]


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

    if fails:
        print(f"  ❌ block0 자기검증 실패 {len(fails)}건 — [0] 판정을 믿지 말 것")
        for f in fails:
            print("     - " + f)
        return False
    print(f"  ✅ {len(_T_CASES) + 1} 케이스 전부 통과 "
          "(T1·T2·T3·T3b·T3c·T4·T5·T6·T7)")
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
    if alt_lim and alt_win and len(alt_win) > len(alt_lim):
        print("  → 원인 확정: `limit` 이 무시되고 기본 창이 짧았다 (§7 과 일치)")
    elif not alt_lim and not alt_win:
        print(f"  → 엔드포인트 자체를 못 쓴다 (kind={k_lim}/{k_win})")

    alt = alt_win or alt_lim

    # ── 0-D. 소스별 MA200 ───────────────────────────────────────────────────
    print("\n  ── 0-D. 소스별 MA200 ──")
    print(f"  {'소스':<24}{'종가':>10}{'MA50':>10}{'MA200':>10}"
          f"{'MA200이격':>12}{'기준대비':>10}")
    print("  " + "-" * 76)

    base = _ma200_row("close (현재 사용)", raw, None)
    variants = [("close (중복제거)", ded)]
    if has_adj:
        variants.append(("adjClose", _dedupe(_hist_from_rows(rows, "adjClose"))))
    if alt:
        variants.append(("dividend-adjusted", _dedupe(_hist_from_rows(alt, "close"))))
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
