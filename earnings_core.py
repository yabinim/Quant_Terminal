"""earnings_core.py — 실적 이벤트 리스크 SSOT.

설계 철학 (프로젝트 원칙 #4 — 손실 방지 우선):
  이 모듈은 **새로운 매수/매도 신호를 만들지 않는다.** 하는 일은 셋뿐:
    1) 차단 — 실적 직전 신규 진입 게이트를 닫는다      (evaluate_entry_gate)
    2) 축소 — 갭 리스크가 한도를 넘는 보유를 줄인다     (evaluate_trim)
    3) 관찰 — 발표 후 반응을 측정·기록한다             (measure_reaction / evaluate_pead)
  매수는 여전히 regime_core 의 레짐·타이밍·R:R 게이트가, 매도는 스윙/포지션
  이중 신호가 결정한다. 여기서 나온 판정은 그 위에 얹히는 '제약'이지 신호가 아니다.

옵션 데이터 미사용 (의도적):
  FMP 는 옵션 체인을 제공하지 않고, 내재 변동폭은 정의상 대칭이라 방향 정보가 없다.
  대신 **과거 8분기 실적 반응일의 close-to-close 변동률**을 쓴다. 실현 움직임을
  직접 재는 값이라 사이징·손절 유효성 판단에는 오히려 더 잘 맞고, 소급 계산이
  가능해 배포 첫날부터 완전 동작한다(내재 변동폭은 이력 축적이 불가능).

IO 경계:
  FMP 조회는 이 모듈이 담당(narrative_core 와 동일 패턴). Sheets IO 는 소비처가 담당.
  streamlit 을 import 하지 않는다 → app.py 와 automation 이 동일 코드를 공유한다.
  app.py 는 @st.cache_data 래퍼만 씌운다.

lockstep 대상:
  - 이 파일 변경 시: app.py + automation/run_earnings_watch.py 동시 배포.
  - 계좌 프리셋 해석은 accounts_core 가 SSOT. 이 모듈은 해석 끝난 trim_cap_pct 만 받는다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

# FMP HTTP 계층 SSOT. **streamlit 무의존 모듈**이므로 이 파일의 설계 불변식
# (상단 주석 참조: streamlit 을 import 하지 않는다)을 깨지 않는다.
import fmp_http as _fh

_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 7


# ──────────────────────────────────────────────────────────────────────────
# 튜닝 상수 (한 곳에서 관리)
# ──────────────────────────────────────────────────────────────────────────

ENTRY_BLOCK_DAYS = 3          # D-N 이내 신규 진입 차단
SNAPSHOT_DAYS = 3             # 사전 스냅샷/이메일 발송 시점 (D-N)
SCAN_HORIZON_DAYS = 10        # 상세 조회 대상 (API 호출 절감)
CALENDAR_HORIZON_DAYS = 90    # 캘린더 조회 창

GAP_QUARTERS = 8              # 예상 변동폭 표본 분기 수
MIN_SAMPLE = 4                # 이 미만이면 산출 거부 (추정치로 지시 금지)

TAIL_ALERT_PCT = 5.0          # 최악 시 계좌 타격 임계 (%) — 계좌 무관 고정
TRIM_HARD_RATIO = 2.0         # 목표 대비 이 배수 초과 → 대폭 축소

PEAD_VOLUME_MIN = 2.0         # PEAD 인정 최소 거래량 배수
PEAD_SURPRISE_RATIO = 0.8     # 실제 갭 / 예상 중앙값 — 이 이상이면 '유의미한 반응'
VOLUME_BASELINE_BARS = 20     # 거래량 배수 기준 구간

MOVE_CONF_LOW_IQR = 1.0       # IQR/중앙값 이 값 초과 → 신뢰 낮음
MOVE_CONF_MID_IQR = 0.5


# ──────────────────────────────────────────────────────────────────────────
# 표시 라벨 (앱 탭 · 이메일이 공유하는 SSOT)
# ──────────────────────────────────────────────────────────────────────────

TRIM_LABELS = {
    "hold":      "🟢 그대로 두세요",
    "trim":      "🟠 줄이세요",
    "trim_hard": "🔴 절반 이하로 줄이세요",
    "core":      "🔵 코어 — 대상 아님",
    "disabled":  "⚪ 축소 판정 미사용 계좌",
    "na":        "⚪ 표본 부족 — 수동 판단",
}

GATE_LABELS = {
    "open":    "✅ 진입 가능",
    "blocked": "⛔ 매수 보류",
    "na":      "⚪ 판정 보류",
}

PEAD_LABELS = {
    "up_continue":   "🟢 상승 지속 후보",
    "up_faded":      "🟡 상승 갭 되돌림 — 관망",
    "down_break":    "🔴 하락 이탈 — 근거 훼손",
    "down_recovered": "🟡 하락 갭 회복 — 관망",
    "muted":         "⚪ 반응 미미",
    "na":            "⚪ 측정 불가",
}

TIMING_LABELS = {
    "bmo": "장 시작 전",
    "amc": "장 마감 후",
    "":    "시각 미상",
}

# FMP stable 은 발표 시각을 제공하지 않는다(2026-08-14 실측). 캘린더에 들어오는
# timing 은 **전부 과거 거래량 패턴에서 역산한 추정치**이므로, 확정 사실처럼
# 표시하면 과신을 부른다. 화면에는 이 라벨을 쓴다.
TIMING_LABELS_INFERRED = {
    "bmo": "장 시작 전(추정)",
    "amc": "장 마감 후(추정)",
    "":    "시각 미상",
}

DATE_SOURCE_LABELS = {
    "confirmed": "확정",
    "estimated": "추정",
    "":          "미상",
}

MOVE_CONF_LABELS = {
    "high":         "신뢰 높음",
    "medium":       "신뢰 보통",
    "low":          "신뢰 낮음",
    "insufficient": "표본 부족",
}


# ──────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────

def _fmp_key() -> str:
    return str(os.environ.get("FMP_API_KEY", "") or "").strip()


def _num(x):
    """안전한 float 변환. 실패/비유한 → None."""
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _get(path: str, key: str = "") -> list | dict | None:
    """FMP stable GET. 실패 시 None (예외 전파 금지).

    2026-08-13: 맨 requests.get → fmp_http(공용 레이트리미터)로 전환.
      이전에는 429 를 만나면 재시도 없이 None 을 돌려줬고, 호출부는 그걸
      '데이터 없음'으로 처리했다 → **조용히 틀린 값**. 종목 수가 적어 드러나지
      않았을 뿐이며, 유니버스(Tier 2, ~140종목)를 붙이면 반드시 터진다.
      반환 계약(비200 → None)은 동일하다.
    """
    k = key or _fmp_key()
    if not k:
        return None
    return _fh.fmp_get_json(path, timeout=_FMP_TIMEOUT, key=k)


def _d(s) -> pd.Timestamp | None:
    """'YYYY-MM-DD...' → Timestamp(날짜만). 실패 → None."""
    ds = str(s or "")[:10]
    if len(ds) != 10:
        return None
    try:
        return pd.Timestamp(ds)
    except Exception:
        return None


ET_TZ_NAME = "America/New_York"


def _timing_from_utc(ts) -> str:
    """UTC 타임스탬프 → ET 기준 bmo/amc.

    2026-08-14 수정 — **버그였다.**
      quote.earningsAnnouncement 는 `2026-08-20T20:30:00.000+0000` 형태의 UTC 인데,
      이전 코드는 문자열 [11:16] 을 잘라 _timing_of 에 넘겼고 _timing_of 는 hh<12 를
      **ET 개장 기준**으로 판정했다. 장전 08:30 ET 발표는 12:30 UTC 이므로 hh=12 →
      'amc' 로 뒤집힌다. BMO 를 AMC 로 오판하면 resolve_reaction_index 가 반응일을
      하루 밀려 잡아 갭·PEAD 측정이 통째로 어긋난다.
    """
    try:
        t = pd.Timestamp(ts)
    except Exception:
        return ""
    if t is None or pd.isna(t):
        return ""
    # ⚠️ 자정 판정은 **변환 전**에 해야 한다. 변환 후에 보면 00:00 UTC 가
    #    ET 로 전날 20:00 이 되어 'amc' 로 새어 나간다("2026-08-20" 처럼 날짜만
    #    온 응답이 전부 장후로 오판됐다).
    if t.hour == 0 and t.minute == 0 and t.second == 0:
        return ""
    try:
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        t = t.tz_convert(ET_TZ_NAME)
    except Exception:
        return ""
    return "bmo" if (t.hour, t.minute) < (9, 30) else "amc"


def _timing_of(item: dict) -> str:
    """FMP 실적 항목에서 BMO/AMC 추출. 필드명이 판올림마다 달라 다중 키 탐색.

    ⚠️ 여기 오는 시각은 **ET 라벨**을 전제한다(earnings-calendar 의 time 필드).
       UTC 타임스탬프는 _timing_from_utc 를 쓸 것.
    """
    for key in ("time", "when", "timing", "hour", "announcementTime"):
        v = str(item.get(key) or "").strip().lower()
        if not v:
            continue
        if "bmo" in v or "before" in v or "pre" in v:
            return "bmo"
        if "amc" in v or "after" in v or "post" in v:
            return "amc"
        # "08:30" 형태 — 09:30 ET 개장 기준
        if ":" in v:
            try:
                hh = int(v.split(":")[0])
                return "bmo" if hh < 12 else "amc"
            except (TypeError, ValueError):
                continue
    return ""


# ──────────────────────────────────────────────────────────────────────────
# 1) 실적 캘린더 — 확정/추정 구분 필수
#    FMP 실적일 상당수가 '추정일'(회사 공식 공지 전 전년 동기 기준 추정).
#    D-3 알림을 보냈는데 실제론 D-9 이면 신뢰가 무너지므로 2개 소스를 교차
#    확인하고, 불일치하면 **더 이른 날짜를 채택**한다(보수적).
# ──────────────────────────────────────────────────────────────────────────

def fetch_next_earnings(ticker: str, today=None, horizon_days: int = CALENDAR_HORIZON_DAYS,
                        key: str = "", market_map: dict = None) -> dict | None:
    """다음 실적 발표 예정 1건. 없으면 None.

    반환: {ticker, earnings_date, days_until, timing, date_source, eps_estimate,
           sources:[...], conflict:bool}
      date_source : 'confirmed' (2개 소스 일치) / 'estimated' (단일 소스 or 불일치)
    """
    tk = str(ticker or "").strip().upper()
    if not tk:
        return None
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    hi = t0 + pd.Timedelta(days=int(horizon_days))
    k = key or _fmp_key()

    cands = []   # (date, timing, eps_est, source)

    def _scan(items, source):
        for it in (items if isinstance(items, list) else []):
            if not isinstance(it, dict):
                continue
            d = _d(it.get("date") or it.get("fiscalDateEnding"))
            if d is None or d < t0 or d > hi:
                continue
            eps = _num(it.get("epsEstimated") or it.get("estimatedEPS")
                       or it.get("epsEstimate") or it.get("estimatedEarning"))
            cands.append((d, _timing_of(it), eps, source))

    # ⚠️ earnings-calendar 는 **시장 전체** 엔드포인트다(FMP 문서상 symbol 파라미터
    #    없음). symbol 을 붙여도 무시되어 모든 티커가 같은 응답을 받았고, 그 결과
    #    캘린더의 Earnings_Date 가 전 종목 동일해졌다(2026-08-13 시트 실측:
    #    AAPL·NVDA·WMT 가 모두 2026-10-13). per-symbol 은 earnings?symbol= 이다.
    _scan(_get(f"earnings?symbol={tk}", k), "earnings")

    # 시장 전체 캘린더 맵 — 실행당 1회 조회한 것을 티커별로 꺼내 쓴다(추가 콜 0).
    #   per-symbol earnings?symbol= 에 없는 timing 의 주 공급원이며,
    #   확정 판정(2개 소스 일치)의 두 번째 소스이기도 하다.
    _mc = (market_map or {}).get(tk)
    if isinstance(_mc, dict):
        _md = _d(_mc.get("date"))
        if _md is not None and t0 <= _md <= hi:
            cands.append((_md, str(_mc.get("timing") or ""), None, "calendar"))

    # quote.earningsAnnouncement — 단일 날짜 폴백
    q = _get(f"quote?symbol={tk}", k)
    qi = q[0] if isinstance(q, list) and q else (q if isinstance(q, dict) else {})
    if isinstance(qi, dict):
        _ann = qi.get("earningsAnnouncement")
        d = _d(_ann)
        if d is not None and t0 <= d <= hi:
            cands.append((d, _timing_from_utc(_ann), None, "quote"))

    if not cands:
        return None

    cands.sort(key=lambda x: x[0])
    best_d = cands[0][0]
    srcs = sorted({c[3] for c in cands})
    dates = {c[0] for c in cands}
    conflict = len(dates) > 1
    timing = next((c[1] for c in cands if c[0] == best_d and c[1]), "")
    eps = next((c[2] for c in cands if c[0] == best_d and c[2] is not None), None)

    # 2개 이상 소스가 같은 날짜를 가리키면 '확정'으로 본다.
    #   2026-08-13: calendar 를 티커별로 잘못 호출하고 있어 (earnings, quote) 로
    #     좁혔으나, quote 가 stable 에서 earningsAnnouncement 를 안 주는 것으로
    #     보여 agree 가 2에 영영 도달하지 못했다(시트 264행 전부 estimated).
    #   2026-08-14: calendar 를 **시장 전체 맵**으로 올바르게 공급하여 복구.
    #   date_source 는 표시 전용이며 어떤 판정도 게이트하지 않는다.
    agree = sum(1 for c in cands if c[0] == best_d
                and c[3] in ("earnings", "calendar", "quote"))
    return {
        "ticker": tk,
        "earnings_date": best_d.strftime("%Y-%m-%d"),
        "days_until": int((best_d - t0).days),
        "timing": timing,
        "date_source": "confirmed" if (agree >= 2 and not conflict) else "estimated",
        "eps_estimate": eps,
        "sources": srcs,
        "conflict": conflict,
    }


def fetch_upcoming(tickers, today=None, horizon_days: int = SCAN_HORIZON_DAYS,
                   key: str = "") -> list[dict]:
    """여러 티커의 다가오는 실적 — horizon_days 이내만. 날짜순 정렬."""
    out = []
    seen = set()
    for tk in (tickers or []):
        t = str(tk or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        ev = fetch_next_earnings(t, today=today, key=key)
        if ev and 0 <= ev["days_until"] <= int(horizon_days):
            out.append(ev)
    out.sort(key=lambda x: (x["days_until"], x["ticker"]))
    return out


def past_earnings_dates(ticker: str, today=None, limit: int = GAP_QUARTERS + 4,
                        key: str = "") -> list[dict]:
    """과거 실적 발표일 목록 (최신순). [{date, timing, surprise_pct}]"""
    tk = str(ticker or "").strip().upper()
    if not tk:
        return []
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    rows, seen = [], set()
    # earnings-surprises 는 stable 에 개별 심볼용이 없다(-bulk 만 존재). 실측에서
    # 404 확인됨 — 티커마다 1콜씩 버려지고 있었다. earnings?symbol= 하나로 충분하다.
    for path in (f"earnings?symbol={tk}",):
        for it in (_get(path, key) or []):
            if not isinstance(it, dict):
                continue
            d = _d(it.get("date") or it.get("fiscalDateEnding"))
            if d is None or d >= t0:
                continue
            ds = d.strftime("%Y-%m-%d")
            if ds in seen:
                continue
            seen.add(ds)
            act = _num(it.get("epsActual") or it.get("actualEarningResult") or it.get("eps"))
            est = _num(it.get("epsEstimated") or it.get("estimatedEarning") or it.get("epsEstimated"))
            sp = None
            if act is not None and est is not None and abs(est) > 1e-9:
                sp = (act - est) / abs(est) * 100.0
            rows.append({"date": ds, "timing": _timing_of(it), "surprise_pct": sp})
        if len(rows) >= limit:
            break
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows[:limit]


# ──────────────────────────────────────────────────────────────────────────
# 2) 반응 측정 — 갭은 '반응일 종가 vs 직전 종가'(close-to-close)
#    BMO 발표 → 당일이 반응일 / AMC 발표 → 다음 거래일이 반응일.
#    timing 미상이면 D·D+1 중 거래량이 큰 쪽을 반응일로 추정한다.
# ──────────────────────────────────────────────────────────────────────────

def resolve_reaction_index(hist: pd.DataFrame, event_date, timing: str = "") -> int | None:
    """hist(DatetimeIndex, 오름차순)에서 반응 세션의 위치 인덱스. 없으면 None."""
    if hist is None or hist.empty:
        return None
    d = _d(event_date)
    if d is None:
        return None
    idx = hist.index
    # 발표일 이후(포함) 첫 거래일
    pos = int(idx.searchsorted(d, side="left"))
    if pos >= len(idx):
        return None
    t = str(timing or "").strip().lower()
    if t == "bmo":
        cand = pos
    elif t == "amc":
        cand = pos + 1 if idx[pos] == d else pos
    else:
        # 미상 — D 와 D+1 중 거래량이 큰 세션
        a, b = pos, min(pos + 1, len(idx) - 1)
        if "Volume" in hist.columns and a != b:
            va = _num(hist["Volume"].iloc[a]) or 0.0
            vb = _num(hist["Volume"].iloc[b]) or 0.0
            cand = b if vb > va else a
        else:
            cand = pos
    if cand <= 0 or cand >= len(idx):
        return None
    return cand


def measure_reaction(hist: pd.DataFrame, event_date, timing: str = "") -> dict:
    """실적 반응 측정 (관찰만 — 예측 없음).

    반환: {ok, reaction_date, gap_pct, open_gap_pct, volume_ratio, gap_held, reason}
      gap_pct     : (반응일 종가 − 직전 종가) / 직전 종가 × 100
      gap_held    : 상승 갭이면 종가 ≥ 시가, 하락 갭이면 종가 ≤ 시가
    """
    out = {"ok": False, "reaction_date": "", "gap_pct": None, "open_gap_pct": None,
           "volume_ratio": None, "gap_held": None, "reason": ""}
    i = resolve_reaction_index(hist, event_date, timing)
    if i is None:
        out["reason"] = "반응일 확인 불가"
        return out
    try:
        pre_close = _num(hist["Close"].iloc[i - 1])
        rc = _num(hist["Close"].iloc[i])
        ro = _num(hist["Open"].iloc[i]) if "Open" in hist.columns else None
    except Exception:
        out["reason"] = "가격 데이터 부족"
        return out
    if pre_close is None or rc is None or pre_close <= 0:
        out["reason"] = "가격 데이터 부족"
        return out

    gap = (rc - pre_close) / pre_close * 100.0
    out["gap_pct"] = round(gap, 2)
    out["reaction_date"] = pd.Timestamp(hist.index[i]).strftime("%Y-%m-%d")
    if ro is not None and ro > 0:
        out["open_gap_pct"] = round((ro - pre_close) / pre_close * 100.0, 2)
        out["gap_held"] = bool(rc >= ro) if gap >= 0 else bool(rc <= ro)

    if "Volume" in hist.columns and i >= 2:
        base = pd.to_numeric(
            hist["Volume"].iloc[max(0, i - VOLUME_BASELINE_BARS):i], errors="coerce"
        ).dropna()
        v = _num(hist["Volume"].iloc[i])
        if v is not None and not base.empty:
            med = float(base.median())
            if med > 0:
                out["volume_ratio"] = round(v / med, 2)

    out["ok"] = True
    return out


TIMING_INFER_MIN_VOTES = 3       # 최소 표본 (분기)
TIMING_INFER_MIN_RATIO = 0.7     # 다수결 우세 비율 — 미달이면 판정 보류
TIMING_INFER_MIN_VOL_EDGE = 1.2  # 두 세션 거래량 비. 이 미만이면 그 분기는 기권


def infer_timing(hist: pd.DataFrame, events: list,
                 quarters: int = GAP_QUARTERS) -> dict:
    """과거 실적 반응 위치로 BMO/AMC 추론. 반환: {timing, votes, n, ratio, ok}

    FMP stable 은 발표 시각(BMO/AMC)을 **어떤 엔드포인트로도 제공하지 않는다**
    (2026-08-14 실측: earnings-calendar 1,834행 전부 time 필드 없음,
     quote.earningsAnnouncement 도 미제공). 문서 전체에도 해당 필드가 없다.

    대신 과거 반응에서 역산한다. 발표 다음 세션에 거래량이 터지므로:
      · 발표일(D) 당일에 터졌다 → 장 시작 전 발표(BMO)
      · 다음 거래일(D+1)에 터졌다 → 장 마감 후 발표(AMC)
    기업은 발표 시각을 거의 바꾸지 않으므로 8분기 다수결이면 충분하다.

    **새로운 가정을 도입하지 않는다** — resolve_reaction_index 가 timing 미상일 때
    이미 쓰는 것과 동일한 신호(거래량 집중)를 반대 방향으로 읽을 뿐이다.

    기권 규칙 (틀린 timing 은 미상보다 나쁘다 — 반응일이 하루 밀린다):
      · 두 세션 거래량 차이가 TIMING_INFER_MIN_VOL_EDGE 미만이면 그 분기는 무효표
      · 유효표가 TIMING_INFER_MIN_VOTES 미만이면 판정 보류
      · 우세 비율이 TIMING_INFER_MIN_RATIO 미만이면 판정 보류(시각을 바꾼 기업)
    """
    out = {"timing": "", "votes": {"bmo": 0, "amc": 0}, "n": 0,
           "ratio": None, "ok": False}
    if hist is None or hist.empty or "Volume" not in hist.columns:
        return out
    idx = hist.index
    seen = 0
    for ev in (events or []):
        if seen >= int(quarters):
            break
        d = _d(ev.get("date"))
        if d is None:
            continue
        pos = int(idx.searchsorted(d, side="left"))
        if pos <= 0 or pos + 1 >= len(idx):
            continue
        seen += 1
        va = _num(hist["Volume"].iloc[pos]) or 0.0
        vb = _num(hist["Volume"].iloc[pos + 1]) or 0.0
        if va <= 0 or vb <= 0:
            continue
        hi_, lo_ = (va, vb) if va >= vb else (vb, va)
        if lo_ <= 0 or (hi_ / lo_) < TIMING_INFER_MIN_VOL_EDGE:
            continue                      # 차이가 미미 → 기권
        out["votes"]["bmo" if va > vb else "amc"] += 1

    n = out["votes"]["bmo"] + out["votes"]["amc"]
    out["n"] = n
    if n < TIMING_INFER_MIN_VOTES:
        return out
    top = "bmo" if out["votes"]["bmo"] >= out["votes"]["amc"] else "amc"
    ratio = out["votes"][top] / float(n)
    out["ratio"] = round(ratio, 3)
    if ratio < TIMING_INFER_MIN_RATIO:
        return out                        # 우세하지 않음 → 보류
    out["timing"] = top
    out["ok"] = True
    return out


def gap_history(hist: pd.DataFrame, events: list, quarters: int = GAP_QUARTERS) -> list[dict]:
    """과거 실적 반응 목록 (최신순). events: past_earnings_dates() 결과."""
    rows = []
    for ev in (events or []):
        m = measure_reaction(hist, ev.get("date"), ev.get("timing", ""))
        if m["ok"] and m["gap_pct"] is not None:
            rows.append({"date": ev.get("date"), "gap_pct": m["gap_pct"],
                         "volume_ratio": m.get("volume_ratio")})
        if len(rows) >= int(quarters):
            break
    return rows


# ──────────────────────────────────────────────────────────────────────────
# 3) 예상 변동폭 — 중앙값이 메인, 최악 하락폭은 테일 경보용
#    평균이 아니라 중앙값을 쓰는 이유: 실적 갭 분포는 꼬리가 두꺼워
#    한 번의 -22% 가 평균을 오염시킨다.
# ──────────────────────────────────────────────────────────────────────────

def expected_move(gaps: list, atr_pct=None, min_sample: int = MIN_SAMPLE) -> dict:
    """과거 갭 목록 → 예상 변동폭 통계.

    gaps: gap_history() 결과 또는 [숫자, ...]
    반환: {ok, median_pct, worst_down_pct, worst_abs_pct, p25, p75,
           atr_multiple, sample_n, confidence, confidence_label, note}
      worst_down_pct 는 **양수 크기**로 반환 (예: -23% → 23.0)
    """
    out = {"ok": False, "median_pct": None, "worst_down_pct": None, "worst_abs_pct": None,
           "p25": None, "p75": None, "atr_multiple": None, "sample_n": 0,
           "confidence": "insufficient", "confidence_label": MOVE_CONF_LABELS["insufficient"],
           "note": ""}

    vals = []
    for g in (gaps or []):
        v = _num(g.get("gap_pct")) if isinstance(g, dict) else _num(g)
        if v is not None:
            vals.append(float(v))
    out["sample_n"] = len(vals)
    if len(vals) < int(min_sample):
        out["note"] = f"표본 {len(vals)}분기 — 최소 {int(min_sample)}분기 필요"
        return out

    arr = np.array(vals, dtype=float)
    absa = np.abs(arr)
    med = float(np.median(absa))
    p25, p75 = float(np.percentile(absa, 25)), float(np.percentile(absa, 75))
    downs = arr[arr < 0]
    worst_down = float(abs(downs.min())) if downs.size else 0.0

    out.update({
        "ok": True,
        "median_pct": round(med, 2),
        "worst_down_pct": round(worst_down, 2),
        "worst_abs_pct": round(float(absa.max()), 2),
        "p25": round(p25, 2), "p75": round(p75, 2),
    })

    a = _num(atr_pct)
    if a is not None and a > 0:
        out["atr_multiple"] = round(med / a, 1)

    # 신뢰도 = 표본 수 + 분산(IQR/중앙값)
    iqr_ratio = ((p75 - p25) / med) if med > 0 else 99.0
    if len(vals) < GAP_QUARTERS - 2 or iqr_ratio > MOVE_CONF_LOW_IQR:
        conf = "low"
    elif iqr_ratio > MOVE_CONF_MID_IQR:
        conf = "medium"
    else:
        conf = "high"
    out["confidence"] = conf
    out["confidence_label"] = MOVE_CONF_LABELS[conf]
    if conf == "low":
        out["note"] = f"분기별 편차 큼 ({p25:.1f}~{p75:.1f}%) — 참고용"
    return out


def atr_pct_of(hist: pd.DataFrame, window: int = 22) -> float | None:
    """평소 일간 변동성(ATR%) — '평소의 몇 배' 환산용."""
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    try:
        c = pd.to_numeric(hist["Close"], errors="coerce")
        h = pd.to_numeric(hist["High"], errors="coerce") if "High" in hist.columns else c
        lo = pd.to_numeric(hist["Low"], errors="coerce") if "Low" in hist.columns else c
        pc = c.shift(1)
        tr = pd.concat([(h - lo).abs(), (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(int(window)).mean().iloc[-1])
        last = float(c.iloc[-1])
        if not (np.isfinite(atr) and np.isfinite(last) and last > 0):
            return None
        return round(atr / last * 100.0, 3)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────
# 4) 진입 게이트 — "손절이 작동하는가"만 본다. 비중과 무관.
# ──────────────────────────────────────────────────────────────────────────

def derived_stop_pct(hist, price=None, atr_mult=None) -> float | None:
    """수동 손절이 없을 때 ATR 기반 손절폭(%) 추정 — R:R 게이트와 동일 규약.

    워치리스트에 Stop_Loss 를 넣지 않는 종목이 많은데, 손절 미상이라고 무조건
    차단하면 '예상 갭 vs 손절폭' 비교라는 이 게이트의 핵심이 아예 작동하지 않는다
    (±3% 종목과 ±19% 종목의 판정이 같아진다). regime_core.build_watchlist_plan 이
    수동 손절 없을 때 쓰는 것과 같은 ATR 손절을 추정치로 쓴다.

    반환: 양수 % (예: 6.4). 산출 불가 시 None.
    """
    try:
        import regime_core as _rc
    except Exception:
        return None
    if hist is None or getattr(hist, "empty", True) or "Close" not in hist.columns:
        return None
    try:
        px = _num(price)
        if px is None:
            px = _num(pd.to_numeric(hist["Close"], errors="coerce").dropna().iloc[-1])
        if px is None or px <= 0:
            return None
        mult = _num(atr_mult)
        if mult is None:
            mult = float(_rc.regime_params({})["atr_mult"])
        atr = _num(_rc.compute_atr(hist, _rc.ATR_WINDOW))
        if atr is None or atr <= 0:
            return None
        return round(atr * mult / px * 100.0, 2)
    except Exception:
        return None


MARKET_CAL_DAYS = 14      # 시장 전체 캘린더 조회 창 (timing 이 실제로 쓰이는 D-10 을 덮는다)


def fetch_market_calendar_map(today=None, days: int = None, key: str = "") -> tuple:
    """시장 전체 실적 캘린더 → ({TICKER: {date, timing}}, diag)

    2026-08-14 도입. earnings-calendar 는 symbol 파라미터가 없는 **시장 전체**
    엔드포인트다. 지금까지 이걸 티커별 조회에 잘못 쓰다 전 종목 동일 날짜 버그를
    냈는데, **원래 용도대로 실행당 1회** 호출하면 세 가지가 한꺼번에 해결된다:

      1) timing(bmo/amc) 확보 — per-symbol earnings?symbol= 에는 time 필드가 없고,
         quote.earningsAnnouncement 는 stable 스키마에서 오지 않는 것으로 보인다
         (시트 264행 전부 Timing 공란·Date_Source 전부 estimated).
      2) 확정 규약 복구 — 티커별로 정확한 두 번째 소스가 생겨 (earnings, calendar)
         교차 확인이 실제로 성립한다.
      3) 경량 조회 절감 — 맵에 있는 종목은 추가 콜 없이 날짜를 얻는다.

    창을 14일로 좁힌 이유: 응답이 페이지네이션되는 정황이 있었다(90일 창에서
    이상 동작). timing 은 D-10 이내에서만 실제로 쓰이므로 14일이면 충분하다.
    커버리지는 diag 로 남겨 실측한다.
    """
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    n = int(days if days is not None else MARKET_CAL_DAYS)
    hi = t0 + pd.Timedelta(days=n)
    frm, to = t0.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")

    data, status, kind = _fh.fmp_get_json_ex(
        f"earnings-calendar?from={frm}&to={to}", timeout=20, key=key)
    out = {}
    n_raw = 0
    if isinstance(data, list):
        for it in data:
            if not isinstance(it, dict):
                continue
            n_raw += 1
            tk = str(it.get("symbol") or "").strip().upper()
            d = _d(it.get("date"))
            if not tk or d is None or d < t0 or d > hi:
                continue
            prev = out.get(tk)
            # 같은 티커가 여러 번 나오면 가장 이른 날짜를 쓴다
            if prev is not None and _d(prev["date"]) <= d:
                continue
            out[tk] = {"date": d.strftime("%Y-%m-%d"), "timing": _timing_of(it)}

    n_tim = sum(1 for v in out.values() if v.get("timing"))
    diag = (f"시장캘린더 {len(out)}종목/{n_raw}행 · timing {n_tim} "
            f"({frm}~{to}, HTTP {status}/{kind})")
    return out, diag


def fetch_next_earnings_light(ticker: str, today=None,
                              horizon_days: int = CALENDAR_HORIZON_DAYS,
                              key: str = "", market_map: dict = None) -> dict | None:
    """다음 실적 예정일 — **1콜 경량 조회** (교차 확인 없음).

    D-10 밖 종목은 어차피 FMP 가 확정일을 안 내놓는다(보통 발표 2~4주 전에 확정).
    그 구간에서 교차 확인은 낭비이므로 1콜만 쓰고 date_source 는 항상
    'estimated' 로 둔다. D-10 이내로 들어오면 fetch_next_earnings 가 승격시킨다.

    2026-08-13 수정 — **치명적 버그였다.**
      이전에는 earnings-calendar?symbol={tk} 를 썼는데, 이 엔드포인트는 FMP 문서상
      **시장 전체용이며 symbol 파라미터가 없다.** symbol 을 붙여도 무시되어 모든
      티커가 동일한 응답을 받았다. 시트 실측: AAPL·NVDA·WMT·MSFT 가 전부 같은
      Earnings_Date(2026-10-13), Est_EPS 도 전부 동일(0.01814).

      더 나쁜 것은 그 값이 항상 ~70일 뒤여서 **D-10 안으로 승격되는 일이 없었다**
      는 점이다. 신규 편입 종목은 저장된 날짜가 실제로 지나(ed < t0) near 로
      떨어질 때까지 최대 두 달간 틀린 날짜로 방치됐다.

      timing 은 이 엔드포인트에 없으므로 빈 문자열이 된다 — D-10 승격 시
      fetch_next_earnings 의 quote?symbol= 이 채운다. 정상 동작이다.
    """
    tk = str(ticker or "").strip().upper()
    if not tk:
        return None
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    hi = t0 + pd.Timedelta(days=int(horizon_days))
    frm, to = t0.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")

    # 시장 전체 캘린더 맵에 있으면 **추가 콜 없이** 날짜+timing 을 얻는다.
    #   맵은 14일 창이므로 '없음'이 '실적 없음'을 뜻하지 않는다 → 폴백 필수.
    _mc = (market_map or {}).get(tk)
    if isinstance(_mc, dict):
        _md = _d(_mc.get("date"))
        if _md is not None and t0 <= _md <= hi:
            return {
                "ticker": tk,
                "earnings_date": _md.strftime("%Y-%m-%d"),
                "days_until": int((_md - t0).days),
                "timing": str(_mc.get("timing") or ""),
                "date_source": "estimated",
                "eps_estimate": None,
                "sources": ["calendar"],
                "conflict": False,
            }

    best = None
    for it in (_get(f"earnings?symbol={tk}", key) or []):
        if not isinstance(it, dict):
            continue
        d = _d(it.get("date") or it.get("fiscalDateEnding"))
        if d is None or d < t0 or d > hi:
            continue
        if best is None or d < best[0]:
            best = (d, _timing_of(it),
                    _num(it.get("epsEstimated") or it.get("estimatedEPS")
                         or it.get("epsEstimate")))
    if best is None:
        return None
    return {
        "ticker": tk,
        "earnings_date": best[0].strftime("%Y-%m-%d"),
        "days_until": int((best[0] - t0).days),
        "timing": best[1],
        "date_source": "estimated",
        "eps_estimate": best[2],
        "sources": ["calendar"],
        "conflict": False,
    }


# ──────────────────────────────────────────────────────────────────────────
# 1-b) Earnings_Calendar — 종목당 1행을 계속 갱신하는 캐시 시트
#
#   왜 시트에 두는가:
#     앱 탭이 매번 종목별 3콜(확정/추정 교차 확인)을 돌면 30종목에 90콜이 든다.
#     실적일은 몇 주에 한 번 바뀌는 정보인데 화면 열 때마다 재조회할 이유가 없다.
#     → 5PM 자동화가 계단식 주기로 갱신하고, 앱은 **FMP 호출 0회**로 시트만 읽는다.
#
#   Earnings_Events 와 분리하는 이유:
#     캘린더는 '종목당 1행이 갱신'되고 이벤트는 '분기마다 1행이 추가'된다.
#     성격이 달라 한 시트에 섞으면 차단 판정 필터가 꼬인다.
#
#   계단식 갱신 (1A):
#     D-30 초과 → 30일에 1회 (경량 1콜)
#     D-30~D-11 → 7일에 1회  (경량 1콜)
#     D-10 이하 → 매일       (3콜 교차 확인 + 변동폭 산출)
# ──────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────
# Tier 구분 (2026-08-13)
#
# Tier 1 (user)     — 보유/워치리스트. 풀 브리핑·스냅샷·이메일·축소 판정 대상.
# Tier 2 (universe) — 대형주 유니버스. **일정 + 예상 갭까지만.**
#                     스냅샷·이메일·축소 판정에서 제외한다.
#
# Tier 2 를 브리핑 대상에 넣지 않는 이유: 관심 표명이 없는 130여 종목에 매일
# 카드를 띄우면 발굴 피드가 되는데, 실적 방향 예측은 백테스트에서 엣지가
# 확인되지 않았다(diag_earnings_preview_backtest). 유니버스의 용도는 "이번 주
# 어떤 대형주가 발표하는가" 라는 이벤트 지형 파악이며, 그건 일정만 있으면 된다.
# ──────────────────────────────────────────────────────────────────────────

SOURCE_USER = "user"
SOURCE_UNIVERSE = "universe"


def normalize_source(v) -> str:
    """Source 값 정규화. 미상/구 행은 SOURCE_USER 로 본다.

    구 행(Source 열이 없던 시절)은 전부 보유/워치리스트 종목이었으므로
    빈 값을 user 로 해석하는 것이 안전하다 — universe 로 오인하면 기존
    종목의 스냅샷·이메일이 조용히 끊긴다(실패 방향이 나쁜 쪽).
    """
    s = str(v or "").strip().lower()
    return SOURCE_UNIVERSE if s == SOURCE_UNIVERSE else SOURCE_USER


def is_universe_only(row) -> bool:
    """이 행이 Tier 2 전용인가(= 스냅샷/이메일/축소 대상에서 제외)."""
    if isinstance(row, dict):
        return normalize_source(row.get("Source")) == SOURCE_UNIVERSE
    return normalize_source(row) == SOURCE_UNIVERSE


CALENDAR_WORKSHEET = "Earnings_Calendar"
CALENDAR_COLS = [
    "Ticker", "Earnings_Date", "Date_Source", "Timing", "Days_Until",
    "Last_Checked", "Check_Tier",
    "Exp_Median_Pct", "Exp_Worst_Pct", "Exp_P25_Pct", "Exp_P75_Pct",
    "ATR_Multiple", "Sample_N", "Move_Confidence",
    "Move_Computed_At", "Move_For_Date",
    # ── EPS 추정치 리비전 축적 (전방 전용) ──────────────────────────────
    # FMP analyst-estimates 는 '현재 시점 전망'만 준다 — 과거에 추정치가 어떻게
    # 변해왔는지의 시계열이 없어 백테스트가 불가능하다. 유일한 방법은 오늘부터
    # 매일 스냅샷을 남기는 것. 2~3 실적 시즌 뒤 리비전 요인으로 쓸 수 있다.
    "Est_EPS", "Est_History_JSON", "Est_Revision_Pct",
    "Notes",
    # ── Tier 구분 (2026-08-13 추가) ────────────────────────────────────
    # "user"     = 보유/워치리스트 → 풀 브리핑·스냅샷·이메일·축소 판정 대상
    # "universe" = 대형주 유니버스 → 일정+예상갭만. 스냅샷/이메일/축소 제외
    # 둘 다면 "user" 가 이긴다(사용자 관심이 우선).
    # ⚠️ 반드시 **맨 끝**에 둔다. Notes 앞에 끼우면 기존 행의 Notes 값이
    #    Source 로 오독된다(마이그레이션 불필요하게 만드는 것이 목적).
    "Source",
]
CALENDAR_NCOL = len(CALENDAR_COLS)


# ──────────────────────────────────────────────────────────────────────────
# Earnings_Est_Archive — EPS 추정치 이력의 분기 아카이브
#
# 왜 필요한가 (2026-09-05 발견):
#   `Est_History_JSON` 은 Earnings_Events 의 (Ticker, Earnings_Date) **행 안**
#   에 사는 JSON 문자열이다. 그런데 calendar_row() 가 분기 전환을 감지하면
#
#       prev_json = "" if date_changed else …
#
#   으로 **이력을 버린다.** 실적이 지나가 Earnings_Date 가 다음 분기로 넘어가는
#   순간 그 티커의 추정치 시계열이 사라진다. 티커당 행은 하나뿐이라 과거 분기
#   행도 남지 않는다. 지워지기 전 어디로도 복사되지 않았다.
#
#   CALENDAR_COLS 주석은 "오늘부터 매일 스냅샷을 남긴다 → 2~3 실적 시즌 뒤
#   리비전 요인으로 쓸 수 있다" 고 적어놨지만, **구현이 그걸 못 했다.**
#   6개월을 기다려도 매 분기 초기화된 3개월치 롤링 버퍼만 있었다.
#   diag_earnings_preview_backtest 의 F1(EPS 리비전)이 영원히 비는 이유였다.
#
# ⚠️ 이 아카이브는 **오늘 이후 분기 전환분만** 잡는다. 이전 분기들은 이미
#    영구히 사라졌고 되살릴 방법이 없다. 재평가 가능 시점은 2~3 실적 시즌 뒤다.
#
# 소유권: Earnings_Events 와 같다 — 티커 단위, 관리자 소유·게스트 읽기 전용.
#   사용자별 행이 아니므로 uid 열이 없다.
# 쓰기: append 전용. 과거 분기는 불변이라 갱신할 일이 없다.
# ──────────────────────────────────────────────────────────────────────────
EST_ARCHIVE_SHEET = "Earnings_Est_Archive"
EST_ARCHIVE_COLS = [
    "Ticker", "Earnings_Date", "Est_EPS", "Est_History_JSON",
    "Est_Revision_Pct",
    # Snapshot_N 을 따로 두는 이유: JSON 을 파싱하지 않고도 "몇 점 쌓였나" 를
    # 시트에서 바로 볼 수 있어야 한다. 재평가 가능 시점을 판단하는 지표다.
    "Snapshot_N", "Archived_At",
]
EST_ARCHIVE_NCOL = len(EST_ARCHIVE_COLS)

# 스냅샷이 이보다 적으면 아카이브하지 않는다. 리비전 계산에 최소 2점이
# 필요하고(estimate_revision_pct), 1점짜리 행은 잡음이다.
EST_ARCHIVE_MIN_SNAPSHOTS = 2


def est_archive_row(ticker: str, prev: dict, now_et=None):
    """분기 전환 직전의 prev 행 → 아카이브 행. 남길 게 없으면 None.

    ⚠️ **calendar_row() 를 부르기 전에** 호출해야 한다. 그 함수가
       date_changed 를 보고 Est_History_JSON 을 버린다. 순서가 뒤집히면
       빈 문자열을 아카이브하게 된다.

    prev : Earnings_Events 의 **옛** 행(dict). CALENDAR_COLS 키를 가진다.
    """
    if not isinstance(prev, dict):
        return None
    raw = str(prev.get("Est_History_JSON", "") or "").strip()
    if not raw:
        return None
    try:
        hist = json.loads(raw)
    except Exception:
        return None
    if not isinstance(hist, list) or len(hist) < EST_ARCHIVE_MIN_SNAPSHOTS:
        return None
    ed = str(prev.get("Earnings_Date", "") or "").strip()
    if not ed:
        return None
    return [
        str(ticker or "").strip().upper(), ed,
        _blank(prev.get("Est_EPS")), raw,
        _blank(prev.get("Est_Revision_Pct")), len(hist),
        str(now_et or ""),
    ]


# ──────────────────────────────────────────────────────────────────────────
# Earnings_Universe — Tier 2 대형주 유니버스 (주 1회 전체 덮어쓰기)
#
# 정의: QQQ 보유종목(≈나스닥 100) ∪ SPY 보유종목 비중 상위 N(≈S&P500 시총 상위 N)
#
#   왜 지수 구성종목 엔드포인트를 안 쓰나 (2026-08-13 실측):
#     · FMP stable 에 nasdaq-constituent 가 **없다**. historical- 만 존재하며
#       그건 현재 명단이 아니라 편입/편출 이력이다.
#     · sp500-constituent 는 경로가 맞는데도 이 계정에서 빈 응답이었다.
#     · /etf/holdings 는 app.py 의 ETF 유니버스·Hidden Alpha 중복도에서 이미
#       프로덕션으로 돌고 있어 **동작이 확인된** 유일한 멤버십 경로다.
#   SPY 비중은 시총 가중이므로 '비중 상위 N' ≈ '시총 상위 N' 이다.
#
#   "S&P 100" 이라는 라벨은 쓰지 않는다 — 실제 OEX 는 위원회가 섹터 균형·옵션
#   유동성을 보고 고르는 별개 지수다.
#
# append 가 아니라 **전체 덮어쓰기**다. 멤버십 스냅샷이라 이전 행을 남기면
# 편출 종목이 영원히 유니버스에 남는다.
# ──────────────────────────────────────────────────────────────────────────

UNIVERSE_WORKSHEET = "Earnings_Universe"
UNIVERSE_COLS = [
    "Ticker", "Name", "Sector", "Market_Cap", "Source", "Updated_At",
]
UNIVERSE_NCOL = len(UNIVERSE_COLS)

# ETF 보유종목에 섞여 오는 비주식 항목. 티커 형태(3~5자 알파벳)라 문자 규칙으로는
# 걸러지지 않아 명시 차단이 필요하다 — QQQ 응답에서 USD 가 실제로 통과했다.
UNIVERSE_EXCLUDE_TICKERS = {
    "USD", "CASH", "USDOLLAR", "XTSLA", "MCASH", "FGXXX", "GOVXX",
    "N/A", "NA", "OTHER", "NONE", "TBILL", "MMF",
}

# SPY 보유종목 비중 상위 N (= S&P 500 시총 상위 N 근사).
UNIVERSE_SPY_TOP_N = 100

# 스크리너 시총 하한 — **유니버스 정의가 아니라 보강용**이다.
# SPY 100위권 시총이 대략 900억 달러대이지만, QQQ 하위권은 그보다 낮다.
# 유니버스 전원의 Market_Cap/섹터가 채워지도록 넉넉히 아래까지 훑는다.
UNIVERSE_SCREENER_MIN_CAP = 25_000_000_000

# ETF 두 경로가 모두 실패했을 때만 쓰는 폴백 유니버스의 시총 하한.
UNIVERSE_MIN_MARKET_CAP = 150_000_000_000

# 유니버스 재계산 주기 (요일: 0=월). 주 1회면 편입/편출 반영에 충분하다.
UNIVERSE_REFRESH_WEEKDAY = 0

# Source 값 — 캘린더의 SOURCE_* 와 다른 축이다(여긴 '어디서 왔나')
UNIV_SRC_NDX = "NDX100"
UNIV_SRC_SP = "SP500_LARGE"     # ETF 경로 전용 (현재 플랜에서 비활성)
UNIV_SRC_BOTH = "BOTH"
UNIV_SRC_LARGE = "US_LARGE"     # 스크리너 경로 — S&P500 한정이 아니다(ADR 포함)

# /etf/holdings 멤버십 경로 사용 여부.
#
# **현재 False** — 2026-08-13 실측에서 QQQ·SPY 둘 다 HTTP 402(Payment Required)를
# 반환했다. 엔드포인트 단위 플랜 제한이며(같은 실행에서 스크리너는 200),
# 요금제가 바뀌기 전까지 절대 변하지 않는다. app.py 6697행에도 같은 취지의
# 주석이 이미 있었다: "대표종목 폴백 (요금제에 holdings 엔드포인트가 없을 때)".
#
# 코드를 남겨둔 이유: 상위 플랜으로 올리면 QQQ = 나스닥 100 정확한 멤버십을
# 얻을 수 있고, 그때는 이 플래그만 True 로 돌리면 된다.
UNIVERSE_USE_ETF_MEMBERSHIP = False


def _etf_membership(etf: str, key: str = "", top_n: int = 0) -> tuple:
    """ETF 보유종목 → ([(TICKER, weight_pct, name)], status, kind)

    지수 구성종목 엔드포인트 대신 ETF 보유종목을 쓰는 이유:
      FMP stable 에 nasdaq-constituent 가 **없고**(historical- 만 존재),
      sp500-constituent 는 이 계정에서 빈 응답이었다(2026-08-13 로그).
      반면 /etf/holdings 는 app.py 의 ETF 유니버스·Hidden Alpha 중복도에서
      이미 프로덕션으로 돌고 있어 동작이 확인된 경로다.
      QQQ ≈ 나스닥 100, SPY ≈ S&P 500(비중 = 시총 가중이므로 비중 상위 N
      ≈ 시총 상위 N).

    top_n: 0 이면 전량. 비중 내림차순 정렬 후 상위 N.
    """
    data, status, kind = _fh.fmp_get_json_ex(
        f"etf/holdings?symbol={str(etf or '').strip().upper()}",
        timeout=15, key=key)
    if not isinstance(data, list):
        return [], status, kind
    rows = []
    for it in data:
        if not isinstance(it, dict):
            continue
        tk = str(it.get("asset") or it.get("symbol") or "").strip().upper()
        # 현금·채권·복수클래스 티커 제외 (BRK.B 등은 실적 조회가 안 된다)
        if not tk or "." in tk or len(tk) > 5 or not tk.isalpha():
            continue
        if tk in UNIVERSE_EXCLUDE_TICKERS:
            continue
        w = _num(it.get("weightPercentage"))
        if w is None:
            w = _num(it.get("weight"))
        if w is None:
            w = _num(it.get("pctVal"))
        if w is not None and 0 < w <= 1:
            w *= 100.0                      # 비율(0~1) → %
        rows.append((tk, float(w or 0.0), str(it.get("name") or "").strip()))
    rows.sort(key=lambda x: -x[1])
    # 같은 티커가 여러 번 나오는 경우 첫(최대 비중) 것만
    seen, uniq = set(), []
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        uniq.append(r)
    return (uniq[:top_n] if top_n else uniq), status, kind


def _is_fund_ticker(tk: str) -> bool:
    """미국 뮤추얼 펀드 티커 관례: 5글자이며 X 로 끝난다(NASDAQ 배정 규칙).

    최후의 방어선이다. 스크리너 파라미터·응답 필드로 먼저 걸러지지 않은 것만
    여기서 잡는다. 오탐 위험이 있으므로 응답 필드 판정이 우선한다.
    """
    return len(tk) == 5 and tk.endswith("X")


def _screener_map(min_cap: float, key: str = "") -> tuple:
    """시총 하한 이상 **보통주** → ({TICKER: {...}}, status, kind, 제외통계).

    2026-08-13 수정 — 유니버스의 절반이 뮤추얼 펀드였다.
      isEtf=false 만으로는 뮤추얼 펀드가 걸러지지 않는데, FMP 가 펀드의 AUM 을
      marketCap 으로 보고하므로 1,500억 하한을 그대로 통과한다. 실측 결과 167종목
      중 약 82개가 VFIAX·VTSAX·FXAIX·AGTHX 류 펀드였고, MER-PK(우선주)와
      BRK-A/BRK-B 중복도 섞였다(하이픈 티커가 필터를 통과).

      파라미터(isFund=false)가 이 플랜에서 실제로 먹는지 문서에 명시가 없으므로
      **응답 필드로도 거른다.** 어느 쪽이 실제로 작동했는지는 제외 통계로 남긴다.
    """
    data, status, kind = _fh.fmp_get_json_ex(
        f"company-screener?marketCapMoreThan={int(min_cap)}"
        f"&isEtf=false&isFund=false&isActivelyTrading=true"
        f"&exchange=NASDAQ,NYSE&limit=2000", timeout=20, key=key)
    out = {}
    drop = {"etf": 0, "fund": 0, "ticker_form": 0, "fund_naming": 0, "excluded": 0}
    if not isinstance(data, list):
        return out, status, kind, drop
    for it in data:
        if not isinstance(it, dict):
            continue
        tk = str(it.get("symbol") or "").strip().upper()
        if not tk:
            continue
        # 복수클래스(BRK-B)·우선주(MER-PK)·해외 접미(XXXX.L) — 실적 조회가 안 된다
        if "." in tk or "-" in tk:
            drop["ticker_form"] += 1
            continue
        if tk in UNIVERSE_EXCLUDE_TICKERS:
            drop["excluded"] += 1
            continue
        if bool(it.get("isEtf")):
            drop["etf"] += 1
            continue
        if bool(it.get("isFund")):
            drop["fund"] += 1
            continue
        if _is_fund_ticker(tk):
            drop["fund_naming"] += 1
            continue
        out[tk] = {
            "name": str(it.get("companyName") or "").strip(),
            "sector": str(it.get("sector") or "").strip(),
            "market_cap": _num(it.get("marketCap")),
        }
    return out, status, kind, drop


def fetch_market_universe(key: str = "", spy_top_n: int = None,
                          use_etf: bool = None) -> dict:
    """Tier 2 유니버스 조회.

    현재 유효 경로는 **스크리너 단독**이다 (UNIVERSE_USE_ETF_MEMBERSHIP=False).
      · company-screener 로 시총 하한 이상 미국 상장주를 받는다.
      · S&P500 한정이 아니므로 ADR(TSM/ASML/NVO 등)도 포함된다 — 이들도 실적을
        발표하고 섹터를 흔들기 때문에 '이벤트 지형' 목적에는 포함이 맞다.
      · 지수 멤버십이 아니라 시총 하한이므로 편입/편출 이벤트에 흔들리지 않고
        자기 유지된다.

    use_etf=True 로 켜면 QQQ/SPY 보유종목 멤버십을 우선 시도하고, 실패 시
    스크리너로 폴백한다. 현재 플랜에서는 402 라 기본 비활성.

    반환: {"ndx": {...}, "sp": {...}, "ok": bool, "diag": str, "source": str,
           "labels": (label_a, label_b, label_both)}
    """
    _use_etf = UNIVERSE_USE_ETF_MEMBERSHIP if use_etf is None else bool(use_etf)
    n = int(spy_top_n if spy_top_n is not None else UNIVERSE_SPY_TOP_N)

    parts = []
    qqq, spy = [], []
    if _use_etf:
        qqq, q_st, q_kind = _etf_membership("QQQ", key)
        spy, s_st, s_kind = _etf_membership("SPY", key, top_n=n)
        parts.append(f"QQQ {len(qqq)}종목(HTTP {q_st}/{q_kind})")
        parts.append(f"SPY상위{n} {len(spy)}종목(HTTP {s_st}/{s_kind})")

    screen, c_st, c_kind, c_drop = _screener_map(UNIVERSE_SCREENER_MIN_CAP, key)
    _dropped = ", ".join(f"{k} {v}" for k, v in c_drop.items() if v) or "0"
    parts.append(f"스크리너 {len(screen)}종목(HTTP {c_st}/{c_kind}, 제외 {_dropped})")
    diag = " · ".join(parts)

    def _meta(tk, name=""):
        m = screen.get(tk) or {}
        return {"name": m.get("name") or name or "",
                "sector": m.get("sector") or "",
                "market_cap": m.get("market_cap")}

    if qqq or spy:
        return {"ndx": {tk: _meta(tk, nm) for tk, _w, nm in qqq},
                "sp": {tk: _meta(tk, nm) for tk, _w, nm in spy},
                "ok": True, "diag": diag, "source": "etf",
                "labels": (UNIV_SRC_NDX, UNIV_SRC_SP, UNIV_SRC_BOTH)}

    # 스크리너 경로 — 시총 하한 상향 적용
    universe = {tk: m for tk, m in screen.items()
                if (m.get("market_cap") or 0) >= UNIVERSE_MIN_MARKET_CAP}
    if universe:
        if _use_etf:
            diag += " → ETF 실패, 스크리너 폴백"
        return {"ndx": {}, "sp": universe, "ok": True, "diag": diag,
                "source": "screener",
                "labels": (UNIV_SRC_NDX, UNIV_SRC_LARGE, UNIV_SRC_BOTH)}

    return {"ndx": {}, "sp": {}, "ok": False, "diag": diag, "source": "none",
            "labels": (UNIV_SRC_NDX, UNIV_SRC_LARGE, UNIV_SRC_BOTH)}


def universe_row(ticker: str, name: str = "", sector: str = "",
                 market_cap=None, source: str = "", now_et: str = "") -> list:
    """Earnings_Universe 1행."""
    row = [
        str(ticker or "").strip().upper(),
        str(name or "").strip(),
        str(sector or "").strip(),
        _blank(_num(market_cap)),
        str(source or "").strip(),
        str(now_et or ""),
    ]
    return (row + [""] * UNIVERSE_NCOL)[:UNIVERSE_NCOL]


def parse_universe(values: list) -> list[dict]:
    """Earnings_Universe 시트 values → dict 목록 (헤더 행 제외)."""
    out = []
    for r in (values or [])[1:]:
        r = (list(r) + [""] * UNIVERSE_NCOL)[:UNIVERSE_NCOL]
        d = dict(zip(UNIVERSE_COLS, r))
        if str(d.get("Ticker") or "").strip():
            out.append(d)
    return out


def merge_universe_sources(ndx: dict, sp: dict, now_et: str = "",
                           labels: tuple = None) -> list[list]:
    """두 출처 dict 를 합쳐 시트 행 목록으로.

    ndx/sp: {TICKER: {"name":..., "sector":..., "market_cap":...}}
    labels: (label_a, label_b, label_both). fetch_market_universe 가 돌려주는
      값을 그대로 넘긴다. 생략하면 ETF 경로 기본값.
      ※ 스크리너 결과에 SP500_LARGE 를 붙이면 **거짓 라벨**이다 — 스크리너는
        S&P500 한정이 아니라 미국 상장 대형주 전체(ADR 포함)다.
    반환: 시총 내림차순 행 목록 (헤더 미포함)
    """
    label_a, label_b, label_both = (labels or
                                    (UNIV_SRC_NDX, UNIV_SRC_SP, UNIV_SRC_BOTH))
    ndx = ndx or {}
    sp = sp or {}
    rows = []
    for tk in sorted(set(ndx) | set(sp)):
        a = ndx.get(tk) or {}
        b = sp.get(tk) or {}
        if tk in ndx and tk in sp:
            src = label_both
        elif tk in ndx:
            src = label_a
        else:
            src = label_b
        mc = _num(b.get("market_cap")) or _num(a.get("market_cap"))
        rows.append((mc or 0.0, universe_row(
            tk,
            b.get("name") or a.get("name") or "",
            b.get("sector") or a.get("sector") or "",
            mc, src, now_et)))
    rows.sort(key=lambda x: -x[0])
    return [r for _, r in rows]
EST_HISTORY_MAX = 24          # 분기당 약 3개월치 일별 스냅샷 상한
EST_REVISION_WINDOW = 30      # 리비전 산출 기준 일수

TIER_NEAR, TIER_MID, TIER_FAR = "near", "mid", "far"
TIER_REFRESH_DAYS = {TIER_NEAR: 1, TIER_MID: 7, TIER_FAR: 30}
TIER_MID_MAX = 30          # D-30 이하부터 mid


def tier_of(days_until) -> str:
    """D-Day → 갱신 등급. 날짜 미상이면 far(30일에 1회만 확인)."""
    d = _num(days_until)
    if d is None or d > TIER_MID_MAX:
        return TIER_FAR
    if d > SCAN_HORIZON_DAYS:
        return TIER_MID
    return TIER_NEAR


def needs_refresh(row: dict, today=None) -> bool:
    """이 행을 오늘 다시 조회해야 하는가."""
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    if not isinstance(row, dict):
        return True
    last = _d(row.get("Last_Checked"))
    if last is None:
        return True
    ed = _d(row.get("Earnings_Date"))
    # 저장된 날짜가 이미 지났으면 다음 분기를 찾아야 한다 → 무조건 갱신
    if ed is not None and ed < t0:
        return True
    days = int((ed - t0).days) if ed is not None else None
    interval = TIER_REFRESH_DAYS[tier_of(days)]
    return int((t0 - last).days) >= interval


def needs_move(row: dict, days_until=None) -> bool:
    """예상 변동폭을 (재)산출해야 하는가.

    2A: 확정일을 기다리지 않고 **D-10 도달 시 무조건** 산출한다. 확정일 없이
        발표되는 종목이 있어 확정을 기다리면 게이트를 놓친다. 산출 자체는
        과거 8분기 갭이라 발표일 확정 여부와 무관하다.
    """
    d = _num(days_until if days_until is not None else (row or {}).get("Days_Until"))
    if d is None or d > SCAN_HORIZON_DAYS or d < 0:
        return False
    if not isinstance(row, dict):
        return True
    # 분기가 바뀌면(=산출 기준 날짜가 다르면) 다시 계산
    if str(row.get("Move_For_Date") or "") != str(row.get("Earnings_Date") or ""):
        return True
    return not str(row.get("Move_Computed_At") or "").strip()


def calendar_row(ticker: str, ev: dict = None, move: dict = None,
                 today=None, now_et: str = "", prev: dict = None,
                 source: str = "") -> list:
    """캘린더 1행 생성. ev/move 가 없으면 prev 값을 보존한다.

    source: SOURCE_USER / SOURCE_UNIVERSE. 빈 문자열이면 prev 값을 보존하고,
      그것도 없으면 SOURCE_USER 로 본다(구 행 = 전부 사용자 종목이었음).
    """
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    p = prev or {}
    e = ev or {}
    ed = str(e.get("earnings_date") or p.get("Earnings_Date") or "")
    d = _d(ed)
    days = int((d - t0).days) if d is not None else ""
    # 날짜가 바뀌었으면 이전 분기의 변동폭은 버린다
    date_changed = bool(ed) and str(p.get("Earnings_Date") or "") not in ("", ed)
    m = move if (isinstance(move, dict) and move.get("ok")) else (None if date_changed else None)

    def _keep(col, val=None):
        return val if val is not None else (p.get(col, "") if not date_changed else "")

    if isinstance(move, dict) and move.get("ok"):
        mv = [_blank(move.get("median_pct")), _blank(move.get("worst_down_pct")),
              _blank(move.get("p25")), _blank(move.get("p75")),
              _blank(move.get("atr_multiple")), int(move.get("sample_n") or 0),
              str(move.get("confidence") or ""), str(now_et or ""), ed]
    else:
        mv = [_keep("Exp_Median_Pct"), _keep("Exp_Worst_Pct"), _keep("Exp_P25_Pct"),
              _keep("Exp_P75_Pct"), _keep("ATR_Multiple"), _keep("Sample_N"),
              _keep("Move_Confidence"), _keep("Move_Computed_At"), _keep("Move_For_Date")]

    # EPS 추정치 스냅샷 — 분기가 바뀌면 이력도 새로 시작한다
    est = _num(e.get("eps_estimate"))
    prev_json = "" if date_changed else str(p.get("Est_History_JSON", "") or "")
    est_json = push_estimate(prev_json, est, today=t0) if (ev is not None) else prev_json
    est_cur = est if est is not None else (
        "" if date_changed else _blank(p.get("Est_EPS")))
    rev = estimate_revision_pct(est_json, today=t0)

    row = [
        str(ticker or "").strip().upper(), ed,
        str(e.get("date_source") or (p.get("Date_Source", "") if not date_changed else "")),
        str(e.get("timing") or (p.get("Timing", "") if not date_changed else "")),
        days, t0.strftime("%Y-%m-%d"), tier_of(days if days != "" else None),
    ] + mv + [_blank(est_cur), est_json, _blank(rev),
              str(p.get("Notes", "") or ""),
              normalize_source(source or p.get("Source", ""))]
    return (row + [""] * CALENDAR_NCOL)[:CALENDAR_NCOL]


def push_estimate(prev_json: str, eps, today=None, keep: int = EST_HISTORY_MAX) -> str:
    """EPS 추정치 스냅샷을 누적. 값이 같으면 날짜만 갱신(중복 방지)."""
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    ds = t0.strftime("%Y-%m-%d")
    v = _num(eps)
    try:
        hist = json.loads(prev_json) if str(prev_json or "").strip() else []
        if not isinstance(hist, list):
            hist = []
    except Exception:
        hist = []
    if v is None:
        return json.dumps(hist[-keep:], separators=(",", ":"))
    if hist and _num(hist[-1].get("eps")) == v:
        hist[-1]["d"] = ds                      # 값 동일 → 마지막 관측일만 갱신
    else:
        hist.append({"d": ds, "eps": round(v, 4)})
    return json.dumps(hist[-keep:], separators=(",", ":"))


def estimate_revision_pct(hist_json: str, today=None,
                          window: int = EST_REVISION_WINDOW):
    """최근 window 일 EPS 추정치 변화율(%). 표본 부족 시 None.

    양수 = 상향(발표 전 애널리스트가 눈높이를 올리는 중).
    """
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    try:
        hist = json.loads(hist_json) if str(hist_json or "").strip() else []
    except Exception:
        return None
    if not isinstance(hist, list) or len(hist) < 2:
        return None
    cutoff = t0 - pd.Timedelta(days=int(window))
    older = [h for h in hist if (_d(h.get("d")) is not None and _d(h.get("d")) <= cutoff)]
    base = _num((older[-1] if older else hist[0]).get("eps"))
    cur = _num(hist[-1].get("eps"))
    if base is None or cur is None or abs(base) < 1e-9:
        return None
    return round((cur - base) / abs(base) * 100.0, 2)


def parse_calendar(values: list) -> dict:
    """Earnings_Calendar get_all_values → {TICKER: {col: val, _row: n}}"""
    out = {}
    if not values or len(values) < 2:
        return out
    for i, r in enumerate(values[1:], start=2):
        r = (list(r) + [""] * CALENDAR_NCOL)[:CALENDAR_NCOL]
        tk = str(r[0]).strip().upper()
        if not tk:
            continue
        d = {c: r[j] for j, c in enumerate(CALENDAR_COLS)}
        d["_row"] = i
        out[tk] = d
    return out


def move_from_row(row: dict) -> dict:
    """캘린더 행 → expected_move 형태 dict (게이트·축소 판정 입력용)."""
    r = row or {}
    med = _num(r.get("Exp_Median_Pct"))
    if med is None:
        return {"ok": False, "sample_n": int(_num(r.get("Sample_N")) or 0),
                "note": "예상 변동폭 미산출 (D-10 도달 시 계산)"}
    conf = str(r.get("Move_Confidence") or "")
    return {
        "ok": True, "median_pct": med,
        "worst_down_pct": _num(r.get("Exp_Worst_Pct")),
        "p25": _num(r.get("Exp_P25_Pct")), "p75": _num(r.get("Exp_P75_Pct")),
        "atr_multiple": _num(r.get("ATR_Multiple")),
        "sample_n": int(_num(r.get("Sample_N")) or 0),
        "confidence": conf,
        "confidence_label": MOVE_CONF_LABELS.get(conf, conf),
        "note": "",
    }


def days_until_from_row(row: dict, today=None):
    """저장된 Days_Until 은 어제 값일 수 있으므로 **항상 재계산**한다."""
    d = _d((row or {}).get("Earnings_Date"))
    if d is None:
        return None
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    return int((d - t0).days)


def blocked_from_calendar(cal: dict, today=None,
                          block_days: int = ENTRY_BLOCK_DAYS) -> dict:
    """캘린더 → {TICKER: 사유} (현재 진입 차단 중인 종목).

    4A: run_watchlist_alerts 와 앱이 **같은 시트 한 곳**을 본다.
        행이 없으면 차단하지 않는다(fail-open) — 캘린더가 아직 안 쌓였다고
        매수 알림을 통째로 막는 쪽이 더 나쁘다.
    """
    out = {}
    for tk, row in (cal or {}).items():
        dd = days_until_from_row(row, today)
        if dd is None or not (0 <= dd <= int(block_days)):
            continue
        mv = _num(row.get("Exp_Median_Pct"))
        why = f"실적 D-{dd}"
        if mv is not None:
            why += f" · 예상 갭 ±{mv:.1f}%"
        out[str(tk).strip().upper()] = why
    return out


def evaluate_entry_gate(move: dict, planned_stop_pct=None, days_until=None,
                        block_days: int = ENTRY_BLOCK_DAYS,
                        earnings_date: str = "", stop_source: str = "") -> dict:
    """실적 직전 신규 진입 차단 판정.

    planned_stop_pct: 계획 손절폭 (양수 %, 예: -7% 손절 → 7.0). None 이면 보수적 차단.
    stop_source     : 'manual' | 'atr'(추정) — 사유 문구에만 반영.
    반환: {blocked, code, label, reason, unblock_after, stop_pct, stop_source}
    """
    out = {"blocked": False, "code": "open", "label": GATE_LABELS["open"],
           "reason": "", "unblock_after": "",
           "stop_pct": _num(planned_stop_pct), "stop_source": str(stop_source or ""),
           "median_pct": None, "gate_move_pct": None, "gate_basis": ""}
    d = _num(days_until)
    if d is None or d < 0 or d > int(block_days):
        return out

    out["unblock_after"] = str(earnings_date or "")
    # 게이트 임계는 **P75**(상위 25% 갭). 중앙값을 쓰면 "절반 이상의 확률로 손절이
    # 뚫릴 때만 차단"이 되는데, 손절이 뚫릴 확률 50%는 이미 받아들이기 어려운 수준이다.
    # P75 = "4번 중 1번 뚫리면 차단". 축소 판정(상시 중앙값)과 달리 게이트는 대상이
    # 워치리스트 몇 종목뿐이고 창도 3일이라 더 보수적으로 가도 알림 피로가 작다.
    # 표시용 통상 변동폭은 여전히 중앙값이다(gate_basis 로 구분해 반환).
    ok = bool((move or {}).get("ok")) if isinstance(move, dict) else False
    med = (move or {}).get("median_pct") if isinstance(move, dict) else None
    p75 = (move or {}).get("p75") if isinstance(move, dict) else None
    mv = _num(p75)
    basis = "p75"
    if mv is None:
        mv, basis = _num(med), "median"
    out["median_pct"] = _num(med)
    out["gate_move_pct"] = mv
    out["gate_basis"] = basis
    _bl = "상위 25% 갭" if basis == "p75" else "예상 갭"
    sp = _num(planned_stop_pct)
    _sfx = " (추정)" if str(stop_source or "").lower() == "atr" else ""

    if not ok or mv is None:
        # 측정 불가 + 실적 임박 → 보수적 차단 (D1 원칙: 미상이면 엄격)
        out.update({"blocked": True, "code": "blocked", "label": GATE_LABELS["blocked"],
                    "reason": f"실적 D-{int(d)} · 예상 변동폭 산출 불가 — 발표 후 재평가"})
        return out

    if sp is None or sp <= 0:
        out.update({"blocked": True, "code": "blocked", "label": GATE_LABELS["blocked"],
                    "reason": f"실적 D-{int(d)} · {_bl} ±{mv:.1f}% · 손절 산출 불가 — 발표 후 재평가"})
        return out

    if mv > sp:
        out.update({"blocked": True, "code": "blocked", "label": GATE_LABELS["blocked"],
                    "reason": (f"실적 D-{int(d)} · {_bl} ±{mv:.1f}% > 손절 {sp:.1f}%{_sfx} "
                               f"— 손절이 작동하지 않는 구간")})
        return out

    out["reason"] = f"실적 D-{int(d)} · {_bl} ±{mv:.1f}% ≤ 손절 {sp:.1f}%{_sfx}"
    return out


# ──────────────────────────────────────────────────────────────────────────
# 5) 축소 판정 — 두 제약을 동시에 만족하는 최대 금액이 목표
#      cap  : 비중 × 중앙 갭 ≤ trim_cap_pct        (상시 판정)
#      tail : 비중 × 최악 하락폭 ≤ TAIL_ALERT_PCT   (테일 경보, 계좌 무관 고정)
#    목표 = min(두 제약). 현재 금액이 목표의 몇 배인지로 3단 판정.
# ──────────────────────────────────────────────────────────────────────────

def evaluate_trim(position_value, equity, move: dict, trim_cap_pct=None,
                  min_trade_dollars: float = 0.0, is_core: bool = False) -> dict:
    """보유 포지션의 실적 갭 노출 진단.

    trim_cap_pct: accounts_core 가 해석한 계좌별 한도(%). None → 축소 판정 미사용(dca_only).
    is_core     : 코어/정기적립 thesis → 축소 대상 제외.
    반환: {code, label, position_value, position_pct, target_value, target_pct,
           sell_dollars, impact_pct, impact_dollars, cap_multiple,
           tail_pct, tail_flag, binding, reason, below_min}
    """
    out = {"code": "na", "label": TRIM_LABELS["na"],
           "position_value": 0.0, "position_pct": None,
           "target_value": None, "target_pct": None, "sell_dollars": 0.0,
           "impact_pct": None, "impact_dollars": None, "cap_multiple": None,
           "tail_pct": None, "tail_flag": False, "binding": "",
           "reason": "", "below_min": False}

    pv, eq = _num(position_value), _num(equity)
    if pv is None or eq is None or pv <= 0 or eq <= 0:
        out["reason"] = "포지션/자본 데이터 없음"
        return out
    out["position_value"] = round(pv, 2)
    out["position_pct"] = round(pv / eq * 100.0, 2)

    if is_core:
        out.update({"code": "core", "label": TRIM_LABELS["core"],
                    "reason": "코어/정기적립 — 실적으로 흔들지 않음"})
        return out

    cap = _num(trim_cap_pct)
    if cap is None or cap <= 0:
        out.update({"code": "disabled", "label": TRIM_LABELS["disabled"],
                    "reason": "장기적립 전용 계좌 — 축소 제안 없음"})
        return out

    if not (isinstance(move, dict) and move.get("ok")):
        out["reason"] = (move or {}).get("note") or "예상 변동폭 산출 불가"
        return out

    med = _num(move.get("median_pct"))
    worst = _num(move.get("worst_down_pct")) or 0.0
    if med is None or med <= 0:
        out["reason"] = "예상 변동폭 산출 불가"
        return out

    # 현재 노출
    impact_d = pv * med / 100.0
    out["impact_dollars"] = round(impact_d, 2)
    out["impact_pct"] = round(impact_d / eq * 100.0, 2)
    out["cap_multiple"] = round(out["impact_pct"] / cap, 2) if cap > 0 else None
    if worst > 0:
        out["tail_pct"] = round(pv * worst / 100.0 / eq * 100.0, 2)
        out["tail_flag"] = bool(out["tail_pct"] > TAIL_ALERT_PCT)

    # 목표 금액 = 두 제약의 교집합
    cap_target = eq * cap / med
    tail_target = (eq * TAIL_ALERT_PCT / worst) if worst > 0 else float("inf")
    if tail_target < cap_target:
        target, binding = tail_target, "tail"
    else:
        target, binding = cap_target, "cap"
    out["binding"] = binding
    out["target_value"] = round(target, 2)
    out["target_pct"] = round(target / eq * 100.0, 2)

    ratio = pv / target if target > 0 else float("inf")
    if ratio <= 1.0:
        out.update({"code": "hold", "label": TRIM_LABELS["hold"],
                    "sell_dollars": 0.0,
                    "reason": (f"예상 타격 {out['impact_pct']:.1f}% — 한도 {cap:.2f}% 이내")})
        return out

    sell = max(pv - target, 0.0)
    _min = _num(min_trade_dollars) or 0.0
    if _min > 0 and sell < _min:
        out.update({"code": "hold", "label": TRIM_LABELS["hold"], "sell_dollars": 0.0,
                    "below_min": True,
                    "reason": f"축소 필요액 ${sell:,.0f} < 최소 거래금액 ${_min:,.0f} — 제안 생략"})
        return out

    code = "trim_hard" if ratio > TRIM_HARD_RATIO else "trim"
    if binding == "tail":
        reason = (f"최악 시 계좌 타격 {out['tail_pct']:.1f}% "
                  f"— 테일 경보 기준 {TAIL_ALERT_PCT:.0f}% 초과")
    else:
        reason = (f"예상 타격 {out['impact_pct']:.1f}% "
                  f"— 한도 {cap:.2f}% 의 {out['cap_multiple']:.1f}배")
    out.update({"code": code, "label": TRIM_LABELS[code],
                "sell_dollars": round(sell, 2), "reason": reason})
    return out


# ──────────────────────────────────────────────────────────────────────────
# 6) PEAD — 예측이 아니라 관찰. 갭 방향 · 거래량 · 갭 유지 3요소만.
#    상승 갭만 '후보'가 되고, 하락 갭은 신규 진입 대상이 아니라
#    기존 근거 훼손 신호로만 쓴다(모멘텀 철학).
# ──────────────────────────────────────────────────────────────────────────

def evaluate_pead(reaction: dict, move: dict = None, regime: str = "") -> dict:
    """발표 후 반응 판정. 행동을 지시하지 않고 '상태'만 돌려준다.

    반환: {code, label, reasons[], is_candidate, is_damage}
    """
    out = {"code": "na", "label": PEAD_LABELS["na"], "reasons": [],
           "is_candidate": False, "is_damage": False}
    if not (isinstance(reaction, dict) and reaction.get("ok")):
        out["reasons"].append((reaction or {}).get("reason") or "측정 불가")
        return out

    gap = _num(reaction.get("gap_pct"))
    if gap is None:
        out["reasons"].append("갭 산출 불가")
        return out

    vr = _num(reaction.get("volume_ratio"))
    held = reaction.get("gap_held")
    med = _num((move or {}).get("median_pct")) if isinstance(move, dict) else None

    # 반응 크기: 예상 대비 유의미한가
    significant = True
    if med is not None and med > 0:
        significant = abs(gap) >= med * PEAD_SURPRISE_RATIO
    reasons = [f"갭 {gap:+.1f}%"]
    if med is not None:
        reasons.append(f"예상 ±{med:.1f}%")
    if vr is not None:
        reasons.append(f"거래량 {vr:.1f}배")
    if held is not None:
        reasons.append("갭 유지" if held else "갭 되돌림")

    vol_ok = (vr is None) or (vr >= PEAD_VOLUME_MIN)

    if not significant:
        out.update({"code": "muted", "label": PEAD_LABELS["muted"], "reasons": reasons})
        return out

    if gap > 0:
        if held is not False and vol_ok:
            out.update({"code": "up_continue", "label": PEAD_LABELS["up_continue"],
                        "is_candidate": True})
            if str(regime or "") == "weak":
                out["reasons"] = reasons + ["단 레짐 약세 — 진입 근거로는 부족"]
                out["is_candidate"] = False
                return out
        else:
            out.update({"code": "up_faded", "label": PEAD_LABELS["up_faded"]})
    else:
        if held is not False and vol_ok:
            out.update({"code": "down_break", "label": PEAD_LABELS["down_break"],
                        "is_damage": True})
        else:
            out.update({"code": "down_recovered", "label": PEAD_LABELS["down_recovered"]})
    out["reasons"] = reasons
    return out


# ──────────────────────────────────────────────────────────────────────────
# 7) 방향 예측 채점 (C층 — 2차 도입). 1차에는 예측 생성을 하지 않지만
#    채점·집계 함수는 미리 둔다: 탭의 성적표 섹션이 '표본 0건'을 정상 렌더링하고,
#    시트 컬럼도 처음부터 확정되어 나중에 중간 삽입이 필요 없다.
#    ※ 기준선은 50% 가 아니다. 시장이 우상향하므로 '무조건 상승' 대조군이
#      진짜 기준선이며, accuracy_summary 는 그 값을 항상 함께 돌려준다.
# ──────────────────────────────────────────────────────────────────────────

PRED_MIN_SAMPLE = 30
PRED_MIN_ACCURACY = 55.0

DIRECTION_UP = "상승 우세"
DIRECTION_DOWN = "하락 우세"
DIRECTION_NEUTRAL = "중립"


def score_prediction(pred_direction: str, gap_pct) -> str:
    """예측 채점. '중립'은 채점 제외(표본에 넣지 않는다)."""
    p = str(pred_direction or "").strip()
    g = _num(gap_pct)
    if g is None or p not in (DIRECTION_UP, DIRECTION_DOWN):
        return ""      # 미채점
    if g > 0:
        return "hit" if p == DIRECTION_UP else "miss"
    if g < 0:
        return "hit" if p == DIRECTION_DOWN else "miss"
    return ""


def accuracy_summary(rows: list) -> dict:
    """[{Pred_Direction, Gap_Pct}, ...] → 적중률 + 대조군 + 유효성.

    반환: {n, hits, accuracy, baseline_accuracy, edge, valid, banner}
      baseline_accuracy: '무조건 상승 예측' 대조군 성적 (진짜 기준선)
    """
    scored, ups, total = 0, 0, 0
    hits = 0
    for r in (rows or []):
        g = _num((r or {}).get("Gap_Pct") if isinstance(r, dict) else None)
        if g is None or g == 0:
            continue
        total += 1
        if g > 0:
            ups += 1
        s = score_prediction((r or {}).get("Pred_Direction"), g)
        if s == "hit":
            scored += 1
            hits += 1
        elif s == "miss":
            scored += 1

    acc = round(hits / scored * 100.0, 1) if scored else None
    base = round(ups / total * 100.0, 1) if total else None
    out = {"n": scored, "hits": hits, "accuracy": acc,
           "baseline_accuracy": base, "edge": None, "valid": False, "banner": ""}
    if acc is not None and base is not None:
        out["edge"] = round(acc - base, 1)

    if scored < PRED_MIN_SAMPLE:
        out["banner"] = (f"표본 {scored}건 (최소 {PRED_MIN_SAMPLE}건) — "
                         f"참고 불가. 성적이 안정되기 전까지 판단에 쓰지 마세요.")
    elif acc is None or acc < PRED_MIN_ACCURACY:
        out["banner"] = (f"적중률 {acc:.1f}% — 현재 이 신호는 유의미하지 않습니다. "
                         f"판단에 반영하지 마세요.")
    elif base is not None and acc <= base:
        out["banner"] = (f"적중률 {acc:.1f}% ≤ 무조건상승 대조군 {base:.1f}% — "
                         f"엣지 없음. 판단에 반영하지 마세요.")
    else:
        out["valid"] = True
        out["banner"] = ""
    return out


# ──────────────────────────────────────────────────────────────────────────
# 8) Earnings_Events 시트 스키마 (SSOT)
#    C층 컬럼을 1차부터 확정해 둔다 — 나중 중간 삽입은 automation 의
#    범위 지정을 깨뜨린다(Watchlist Account 컬럼 사례).
# ──────────────────────────────────────────────────────────────────────────

EVENTS_WORKSHEET = "Earnings_Events"
EVENTS_COLS = [
    # 사전 (D-3 스냅샷)
    "Event_ID", "Ticker", "Earnings_Date", "Date_Source", "Timing",
    "Snapshot_At", "Price_At_Snapshot",
    "Exp_Median_Pct", "Exp_Worst_Pct", "Exp_P25_Pct", "Exp_P75_Pct",
    "ATR_Multiple", "Sample_N", "Move_Confidence",
    # 예측 (C층 — 1차 공란)
    "Pred_Direction", "Pred_Score", "Pred_Confidence", "Feature_JSON", "Pred_Narrative",
    # 사후 (반응일 5pm)
    "Actual_EPS", "Est_EPS", "Surprise_Pct", "Gap_Pct", "Volume_Ratio",
    "Gap_Held", "PEAD_Verdict", "Pred_Hit", "Verified_At",
    # 지연 (D+5)
    "D5_Return_Pct", "Notes",
    # 사전 종가 기준 수익률 (2026-08-15 추가 — 2단계 프리뷰 대조용)
    #   기존 D5_Return_Pct 는 '반응일 종가' 기준이라 축이 다르다. 이쪽은
    #   **발표 전날 종가**(= 갭 계산의 분모와 동일) 기준이라, 발표 전에 들고
    #   들어갔을 때의 실제 손익을 잰다. 둘 다 유지한다.
    #   ⚠️ 중간 삽입 금지 — 반드시 꼬리에 추가한다(범위 지정이 깨진다).
    "Pre_Ret_D1_Pct", "Pre_Ret_D3_Pct", "Pre_Ret_D7_Pct",
]
EVENTS_NCOL = len(EVENTS_COLS)


def event_id(ticker: str, earnings_date: str) -> str:
    return f"{str(ticker or '').strip().upper()}_{str(earnings_date or '')[:10]}"


def snapshot_row(ev: dict, move: dict, price=None, now_et: str = "") -> list:
    """사전 스냅샷 → 시트 행. C층 컬럼과 사후 컬럼은 공란으로 자리만 확보."""
    m = move or {}
    row = [
        event_id(ev.get("ticker"), ev.get("earnings_date")),
        str(ev.get("ticker") or "").upper(),
        str(ev.get("earnings_date") or ""),
        str(ev.get("date_source") or ""),
        str(ev.get("timing") or ""),
        str(now_et or ""),
        ("" if _num(price) is None else round(float(price), 4)),
        _blank(m.get("median_pct")), _blank(m.get("worst_down_pct")),
        _blank(m.get("p25")), _blank(m.get("p75")),
        _blank(m.get("atr_multiple")), int(m.get("sample_n") or 0),
        str(m.get("confidence") or ""),
    ]
    row += [""] * 5                       # 예측 (C층)
    row += [""] * 8                       # 사후
    row += [""] * 2                       # 지연
    return (row + [""] * EVENTS_NCOL)[:EVENTS_NCOL]


def _blank(v):
    n = _num(v)
    return "" if n is None else n


def blocked_tickers(rows: list, today=None, block_days: int = ENTRY_BLOCK_DAYS) -> dict:
    """Earnings_Events 행 목록 → {TICKER: 사유} (현재 진입 차단 중인 종목).

    run_watchlist_alerts 가 시트 1회 조회로 게이트를 참조하기 위한 순수 함수.
    스크립트 간 실행 순서에 의존하지 않도록 **시트를 공유 상태로** 쓴다.
      · 아직 발표 전(Gap_Pct 공란)이고 D-block_days 이내인 행만 차단 대상.
      · 행이 없으면 차단하지 않는다(fail-open) — 실적 워치가 한 번도 안 돌았거나
        실패한 상황에서 매수 알림을 통째로 막아버리는 쪽이 더 나쁘다.
    """
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    out = {}
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        if str(r.get("Gap_Pct") or "").strip():
            continue                       # 이미 발표·측정 완료
        d = _d(r.get("Earnings_Date"))
        if d is None:
            continue
        dd = int((d - t0).days)
        if not (0 <= dd <= int(block_days)):
            continue
        tk = str(r.get("Ticker") or "").strip().upper()
        if not tk:
            continue
        mv = _num(r.get("Exp_Median_Pct"))
        why = f"실적 D-{dd}"
        if mv is not None:
            why += f" · 예상 갭 ±{mv:.1f}%"
        prev = out.get(tk)
        if prev is None or dd < prev[0]:
            out[tk] = (dd, why)
    return {k: v[1] for k, v in out.items()}


def parse_events(values: list) -> list[dict]:
    """Earnings_Events get_all_values → dict 목록 (헤더 제외)."""
    out = []
    if not values or len(values) < 2:
        return out
    for i, r in enumerate(values[1:], start=2):
        r = (list(r) + [""] * EVENTS_NCOL)[:EVENTS_NCOL]
        d = {c: r[j] for j, c in enumerate(EVENTS_COLS)}
        d["_row"] = i
        out.append(d)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 실적 프리뷰 브리핑 (2단계) — Earnings_Preview
# ══════════════════════════════════════════════════════════════════════════
#
# 목적
#   실적 발표 전에 "기대치가 어디 잡혀 있고, 내가 틀리면 얼마 잃나"를 보여준다.
#   **방향 예측은 하지 않는다.** 1,209건 백테스트에서 5개 요인 전부 방향 예측
#   엣지가 없음이 확인됐다(최고 점수 구간의 상승 갭 비율 45.2% — 무조건부
#   기준선보다 낮음). 여기 저장하는 요인들은 '예측'이 아니라
#   **'시장이 이미 얼마나 반영했나'** 를 서술하는 용도다.
#
# 왜 별도 시트인가
#   Earnings_Events 는 1행 = 1이벤트다. 프리뷰는 1행 = 1스냅샷(D-7/D-3/최종)이라
#   축이 다르다. 같은 시트에 넣으면 이벤트당 3행이 되어 기존 조회가 전부 깨진다.
#   append-only 로만 쓴다 — 과거 스냅샷은 절대 수정하지 않는다.
#   "그때 내가 본 숫자"가 보존되어야 사후 대조가 의미를 갖는다.
#
# 이 플랜에서 불가능한 것 (2026-08-15 실측)
#   · analyst-estimates          → HTTP 402. EPS·매출 컨센서스는
#                                  earnings?symbol= 의 미래 행에서 얻는다
#   · earning-call-transcript-*  → HTTP 402. 트랜스크립트 요약 불가.
#                                  Transcript_Summary 열은 **영구 사표**다.
#                                  기존 행 정렬을 지키려고 남겨둘 뿐 채우지 않는다.
#   · news/press-releases        → HTTP 402 (2026-08-15 3종목 실측).
#                                  보도자료 원문도 불가. 서술 노선은 전부 막혔다
#   · news/stock 의 text 필드    → 존재하지만 138~255자 리드 문단이다.
#                                  전문이 아니라 발췌라 요약해도 트랜스크립트
#                                  대체가 안 된다. 제목만 쓴다
#   · 공매도 비율                → 엔드포인트 자체가 플랜에 없음.
#                                  백테스트에 있던 Surprise_Avg_Pct 로 대체
#
# C블록 확정 (2026-08-15) — 서술 대신 내부자 거래
#   서술이 전부 막혀서 C블록을 수치로 바꿨다. insider-trading/search 로
#   최근 90일 **재량** 거래를 달러로 집계한다. Gemini 불필요.
#   3종목 실측: 100건 1콜이 AAPL 314일 / NVDA 187일 / WMT 151일을 덮었고
#   price 결측 0건, 페이징 정상.
# ──────────────────────────────────────────────────────────────────────────

PREVIEW_WORKSHEET = "Earnings_Preview"
PREVIEW_COLS = [
    # ── 식별 ──
    "Snapshot_ID", "Event_ID", "Ticker", "Earnings_Date", "Timing",
    "Phase", "Days_Until", "Snapshot_At",
    # ── 가격·리스크 (그 시점에 본 최악치를 박아둔다) ──
    "Price_At_Snapshot", "Exp_Median_Pct", "Exp_Worst_Pct",
    # ── A블록: 기대치가 어디 잡혀 있나 ──
    "Est_EPS", "Est_EPS_YoY_Pct", "Est_Revision_Pct",
    "Est_Revenue", "Est_Revenue_YoY_Pct",
    "Target_Mean", "Target_Upside_Pct",
    # ── B블록: 얼마나 이미 반영됐나 (전부 백테스트 검증 산식) ──
    "RS_20d_Pct", "Beat_Rate_Pct", "Surprise_Avg_Pct",
    "Grade_Buy_Pct", "Grade_Drift_90d", "Sample_N_Q",
    # ── C블록: 서술 — Transcript_Summary 는 402 로 영구 사표(항상 공란) ──
    "News_Count", "News_JSON", "Transcript_Summary",
    # ── 메타 ──
    "Data_Flags", "Notes",
    # ── C블록(수치): 내부자 거래 — 3단계 추가분 ──
    #   ⚠️ 반드시 **맨 뒤**에 붙인다. 중간에 끼우면 기존 29열 행의 값이
    #      한 칸씩 밀려 Data_Flags 가 숫자로, Notes 가 플래그로 읽힌다.
    #      기존 행은 30~33열이 공란이 되고 parse_preview 가 패딩으로 메운다.
    "Insider_Sale_Val_90d", "Insider_Sale_N_90d",
    "Insider_Buy_Val_90d", "Insider_Cov_D",
]
PREVIEW_NCOL = len(PREVIEW_COLS)

# 백테스트(diag_earnings_preview_backtest.py)와 **같은 값을 써야 한다.**
# 여기서 창을 바꾸면 저장되는 숫자가 검증된 산식과 달라진다.
PREVIEW_RS_WINDOW = 20            # 상대강도 관측 창 (거래일)
PREVIEW_GRADE_DRIFT_DAYS = 90     # 의견 변화 관측 창. 월별 데이터라 60일은 관측점 1~2개
PREVIEW_SURPRISE_LOOKBACK = 8     # beat율·평균 서프라이즈 표본 분기 수
PREVIEW_MIN_QUARTERS = 4          # 이 미만이면 B블록 산출 거부 (추정치로 지시 금지)
PREVIEW_YOY_TOL_DAYS = 45         # 전년 동기 매칭 허용 오차 — 분기 날짜가 밀리는 종목 대비
PREVIEW_NEWS_MAX = 5              # 스냅샷당 저장 헤드라인 수
PREVIEW_NEWS_TITLE_MAX = 140      # 제목 절단 길이 (셀 용량 방어)

# ── 내부자 거래 (C블록 수치) ──
PREVIEW_INSIDER_WINDOW = 90       # 관측 창(일). 실적 전 분기와 대략 맞춘다
PREVIEW_INSIDER_LIMIT = 100       # 1콜 행 수. 실측상 90일을 여유 있게 덮는다

# 18종 코드 중 **시장에서 스스로 사고판** 것만. 나머지는 부여·세금·행사·증여라
# 임원의 판단이 들어가지 않는다. 이걸 안 거르면 베스팅일에 가짜 신호가 뜬다.
#   실측 반례 — AAPL 2026Q1 은 재량 매수·매도가 둘 다 0인데
#   acquiredDisposedRatio 는 1.500 이었다. 그 비율은 전체 주식 수 기준이라
#   A-Award·F-InKind·M-Exempt 가 그대로 섞인다. 그래서 비율은 쓰지 않는다.
INSIDER_BUY_TYPES = ("P-Purchase",)
INSIDER_SELL_TYPES = ("S-Sale",)

# 스냅샷 발동 창.
#   창을 두는 이유: 휴장일에 5PM 실행이 건너뛰면 그날 dd 가 그냥 지나가버려
#   스냅샷이 통째로 유실된다. 창끼리 겹치지 않으므로 이중 발동은 없고,
#   (Event_ID, Phase) 중복 검사로 한 번 더 막는다.
PREVIEW_PHASE_WINDOWS = (("D7", 6, 8), ("D3", 3, 4))
PREVIEW_PHASES = ("D7", "D3", "FINAL")

PREVIEW_PHASE_LABELS = {
    "D7": "D-7 초기",
    "D3": "D-3 중간",
    "FINAL": "최종",
}


def preview_final_dd(timing: str = "") -> int:
    """최종 스냅샷의 목표 D-N.

    AMC(장 마감 후 발표) → D-1 종가까지 반영 가능.
    BMO(장 시작 전 발표) → D-1 아침에 이미 발표되므로 D-2 가 마지막 안전 시점.
    미상 → AMC 로 간주(보수적: 더 늦게 찍는다).
    """
    t = str(timing or "").strip().lower()
    return 2 if t == "bmo" else 1


def preview_phase(days_until, timing: str = "") -> str:
    """D-N → 스냅샷 Phase. 해당 없으면 "".

    dd == 0 에서는 절대 발동하지 않는다. 발표 당일 5PM 실행은 AMC 라면
    **이미 발표된 뒤**라 '사전' 스냅샷이 아니게 된다.
    """
    dd = _num(days_until)
    if dd is None:
        return ""
    dd = int(dd)
    if dd < 1:
        return ""
    for name, lo, hi in PREVIEW_PHASE_WINDOWS:
        if lo <= dd <= hi:
            return name
    if 1 <= dd <= preview_final_dd(timing):
        return "FINAL"
    return ""


def preview_snapshot_id(eid: str, phase: str) -> str:
    return f"{str(eid or '')}_{str(phase or '')}"


# ── FMP 조회 ──────────────────────────────────────────────────────────────

def fetch_earnings_records(ticker: str, key: str = "", limit: int = 16) -> list[dict]:
    """earnings?symbol= 원본 → 정규화 레코드 (최신순).

    **이 한 콜이 A블록과 B블록을 동시에 먹인다.**
      · 미래 행 → EPS·매출 컨센서스 (A블록)
      · 과거 행 → beat율·평균 서프라이즈 폭 (B블록) + 전년 동기 실적 (YoY)

    2026-08-15 실측 확인 필드:
        date, epsActual, epsEstimated, lastUpdated,
        revenueActual, revenueEstimated, symbol
    """
    tk = str(ticker or "").strip().upper()
    if not tk:
        return []
    out = []
    for it in (_get(f"earnings?symbol={tk}&limit={int(limit)}", key) or []):
        if not isinstance(it, dict):
            continue
        d = _d(it.get("date"))
        if d is None:
            continue
        out.append({
            "date": d.strftime("%Y-%m-%d"),
            "_dt": d,
            "eps_est": _num(it.get("epsEstimated")),
            "eps_act": _num(it.get("epsActual")),
            "rev_est": _num(it.get("revenueEstimated")),
            "rev_act": _num(it.get("revenueActual")),
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def split_future_past(records: list, today=None) -> tuple:
    """레코드 → (다가오는 분기 1건 | None, 과거 분기 목록 최신순).

    '미래'의 기준은 오늘이 아니라 **실적 발표일**이다. 오늘 이후 발표 행 중
    가장 가까운 것이 이번 이벤트다.
    """
    t0 = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    fut = [r for r in (records or []) if r.get("_dt") is not None and r["_dt"] >= t0]
    past = [r for r in (records or []) if r.get("_dt") is not None and r["_dt"] < t0]
    fut.sort(key=lambda x: x["date"])
    return (fut[0] if fut else None), past


def fetch_price_target(ticker: str, key: str = "") -> dict:
    """price-target-consensus → {mean, median, high, low}. 없으면 빈 dict."""
    tk = str(ticker or "").strip().upper()
    if not tk:
        return {}
    data = _get(f"price-target-consensus?symbol={tk}", key)
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for it in items:
        if not isinstance(it, dict):
            continue
        mean = _num(it.get("targetConsensus") or it.get("targetMean"))
        med = _num(it.get("targetMedian"))
        if mean is None and med is None:
            continue
        return {"mean": mean, "median": med,
                "high": _num(it.get("targetHigh")), "low": _num(it.get("targetLow"))}
    return {}


def fetch_grade_series(ticker: str, key: str = "", limit: int = 400) -> list:
    """grades-historical → [(date, 매수의견 비율%)] 오름차순.

    매수의견 비율 = (StrongBuy + Buy) / 전체 × 100.
    diag_earnings_preview_backtest.grade_series 와 **동일 산식**이어야 한다.

    이 엔드포인트는 previousGrade/newGrade/action 을 주지 않는다. 월별
    '의견 분포 스냅샷'이지 '변경 이력'이 아니다.
    """
    tk = str(ticker or "").strip().upper()
    if not tk:
        return []
    out = []
    for it in (_get(f"grades-historical?symbol={tk}&limit={int(limit)}", key) or []):
        if not isinstance(it, dict):
            continue
        d = _d(it.get("date") or it.get("publishedDate"))
        if d is None:
            continue
        sb = _num(it.get("analystRatingsStrongBuy")) or 0.0
        b = _num(it.get("analystRatingsBuy")) or 0.0
        h = _num(it.get("analystRatingsHold")) or 0.0
        sl = _num(it.get("analystRatingsSell")) or 0.0
        ss = _num(it.get("analystRatingsStrongSell")) or 0.0
        tot = sb + b + h + sl + ss
        if tot <= 0:
            continue
        out.append((d, (sb + b) / tot * 100.0))
    out.sort(key=lambda x: x[0])
    return out


def fetch_stock_news(ticker: str, key: str = "", limit: int = 10) -> list[dict]:
    """news/stock?symbols= → [{date, title, site, url}] 최신순.

    narrative_core 의 뉴스 경로는 **시장 전체** 전용이라 재사용할 수 없다.
    """
    tk = str(ticker or "").strip().upper()
    if not tk:
        return []
    out = []
    for it in (_get(f"news/stock?symbols={tk}&limit={int(limit)}", key) or []):
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        d = _d(str(it.get("publishedDate") or "")[:10])
        out.append({
            "date": ("" if d is None else d.strftime("%Y-%m-%d")),
            "title": title[:PREVIEW_NEWS_TITLE_MAX],
            "site": str(it.get("site") or "").strip()[:40],
            "url": str(it.get("url") or "").strip()[:300],
        })
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out


def fetch_insider_90d(ticker: str, key: str = "", today=None,
                      window: int = PREVIEW_INSIDER_WINDOW,
                      limit: int = PREVIEW_INSIDER_LIMIT) -> dict:
    """insider-trading/search → 최근 window 일 **재량** 거래 달러 집계.

    반환 {ok, sale_val, sale_n, buy_val, buy_n, cov_d, price_missing}
      ok=False 면 나머지는 의미 없음 — 호출부가 공란으로 남겨야 한다.
      0 을 쓰면 "봤는데 없었다"와 "못 봤다"가 구분되지 않는다.

    왜 statistics 가 아니라 search 인가
      insider-trading/statistics 는 1콜에 24년치 분기를 주지만
      (a) 분기 집계라 8월 이벤트에 6월말 마감분을 붙이게 되고(46~138일 지연)
      (b) 금액이 아니라 **건수**다.
      search 는 행 단위라 날짜와 price 가 있어 둘 다 해결된다.
      대신 100행 ≈ 1년치뿐이라 소급 백테스트는 statistics 쪽을 쓴다
      (automation/backfill_insider_stats.py).

    날짜는 transactionDate 를 쓴다. filingDate 는 신고일이라 거래일보다
    늦다(Form 4 는 2영업일 내 신고). 실적 전 창을 재려면 실제 거래일이어야 한다.

    cov_d 는 이 응답이 실제로 덮은 일수다. window 보다 작으면 그 행의 달러값은
    **잘린 값**이라 다른 행과 비교하면 안 된다. 신고가 잦은 종목에서 100행이
    90일에 못 미칠 수 있고, 그걸 기록해 두지 않으면 백테스트가 조용히 오염된다.
    """
    out = {"ok": False, "sale_val": None, "sale_n": 0,
           "buy_val": None, "buy_n": 0, "cov_d": None, "price_missing": 0}
    tk = str(ticker or "").strip().upper()
    if not tk:
        return out

    items = _get(f"insider-trading/search?symbol={tk}"
                 f"&page=0&limit={int(limit)}", key)
    if not isinstance(items, list) or not items:
        return out

    t0 = _d(today) if today is not None else _d(pd.Timestamp.today())
    if t0 is None:
        t0 = pd.Timestamp.today().normalize()
    cutoff = t0 - pd.Timedelta(days=int(window))

    sale_val = buy_val = 0.0
    sale_n = buy_n = miss = 0
    oldest = None

    for it in items:
        if not isinstance(it, dict):
            continue
        # transactionDate 우선. 비어 있으면 그 행은 창 판정을 할 수 없으므로 버린다.
        d = _d(it.get("transactionDate"))
        if d is None:
            continue
        if oldest is None or d < oldest:
            oldest = d
        if d < cutoff:
            continue

        t = str(it.get("transactionType") or "").strip()
        if t not in INSIDER_BUY_TYPES and t not in INSIDER_SELL_TYPES:
            continue

        qty = _num(it.get("securitiesTransacted")) or 0.0
        px = _num(it.get("price"))
        if px is None or px <= 0:
            # 건수는 세되 금액에는 넣지 않는다. 몇 건이 빠졌는지 남긴다.
            miss += 1
            if t in INSIDER_SELL_TYPES:
                sale_n += 1
            else:
                buy_n += 1
            continue

        if t in INSIDER_SELL_TYPES:
            sale_val += px * abs(qty)
            sale_n += 1
        else:
            buy_val += px * abs(qty)
            buy_n += 1

    if oldest is None:
        return out

    out.update({
        "ok": True,
        "sale_val": sale_val, "sale_n": sale_n,
        "buy_val": buy_val, "buy_n": buy_n,
        "cov_d": int((t0 - oldest).days),
        "price_missing": miss,
    })
    return out


# ── 순수 계산 ─────────────────────────────────────────────────────────────

def yoy_pct(cur, prior):
    """전년 대비 증감률(%). 분모가 0에 가까우면 None.

    prior 가 음수(적자)면 부호 때문에 증감률이 무의미해진다 — None 을 준다.
    '적자에서 흑자'를 +300% 같은 숫자로 표시하면 오독을 부른다.
    """
    c, p = _num(cur), _num(prior)
    if c is None or p is None or p <= 0:
        return None
    return (c - p) / p * 100.0


def prior_year_quarter(past: list, target_date, tol_days: int = PREVIEW_YOY_TOL_DAYS):
    """전년 동기 레코드. 인덱스가 아니라 **날짜 매칭**으로 찾는다.

    '4개 전 행'으로 세면 분기 발표가 밀리거나 빠진 종목에서 엉뚱한 분기와
    비교하게 된다. 목표일(= 이번 발표일 - 365일)에 가장 가까운 행을 쓰되,
    tol_days 를 넘으면 비교를 포기한다.
    """
    t = _d(target_date)
    if t is None or not past:
        return None
    goal = t - pd.Timedelta(days=365)
    best, best_gap = None, None
    for r in past:
        d = r.get("_dt")
        if d is None:
            continue
        gap = abs(int((d - goal).days))
        if best_gap is None or gap < best_gap:
            best, best_gap = r, gap
    if best is None or best_gap > int(tol_days):
        return None
    return best


def beat_stats(past: list, lookback: int = PREVIEW_SURPRISE_LOOKBACK,
               min_sample: int = PREVIEW_MIN_QUARTERS) -> dict:
    """과거 분기 → beat율 + 평균 서프라이즈 폭.

    공매도 비율이 이 플랜에 없어 그 자리를 **평균 서프라이즈 폭**이 대신한다.
    임의 대체가 아니라 백테스트 FACTORS 에 원래 있던 요인(F2_surp)이다.

    표본이 min_sample 미만이면 산출을 거부한다 — 2~3분기로 낸 beat율은
    노이즈이고, 그걸 카드에 띄우면 없느니만 못하다.
    """
    vals = []
    for r in (past or [])[:int(lookback)]:
        act, est = _num(r.get("eps_act")), _num(r.get("eps_est"))
        if act is None or est is None or abs(est) < 1e-9:
            continue
        vals.append((act - est) / abs(est) * 100.0)
    if len(vals) < int(min_sample):
        return {"ok": False, "beat_rate_pct": None, "surprise_avg_pct": None,
                "sample_n": len(vals)}
    beat = sum(1 for v in vals if v > 0) / len(vals) * 100.0
    return {"ok": True, "beat_rate_pct": beat,
            "surprise_avg_pct": sum(vals) / len(vals), "sample_n": len(vals)}


def rel_strength_pct(hist, spy_hist, window: int = PREVIEW_RS_WINDOW, asof=None):
    """20일 상대강도 = 종목 수익률 − SPY 수익률 (%p).

    diag_earnings_preview_backtest.build_events 의 F3_rs 와 동일 산식.
    SPY 를 못 구하면 절대 수익률을 그대로 준다(부분 정보라도 남긴다).
    """
    if hist is None or getattr(hist, "empty", True) or "Close" not in hist.columns:
        return None
    s = hist["Close"]
    if asof is not None:
        a = _d(asof)
        if a is not None:
            s = s.loc[:a]
    w = int(window)
    if len(s) < w + 1:
        return None
    c0, c1 = _num(s.iloc[-w - 1]), _num(s.iloc[-1])
    if c0 is None or c1 is None or c0 <= 0:
        return None
    out = (c1 - c0) / c0 * 100.0
    try:
        if spy_hist is not None and not spy_hist.empty and "Close" in spy_hist.columns:
            ss = spy_hist["Close"]
            if asof is not None:
                a = _d(asof)
                if a is not None:
                    ss = ss.loc[:a]
            if len(ss) > w:
                s0, s1 = _num(ss.iloc[-w - 1]), _num(ss.iloc[-1])
                if s0 is not None and s1 is not None and s0 > 0:
                    out -= (s1 - s0) / s0 * 100.0
    except Exception:
        pass
    return out


def grade_factors(series: list, base_dt, drift_days: int = PREVIEW_GRADE_DRIFT_DAYS):
    """(변화폭, 현재 수준). base_dt 이전 관측만 사용 — look-ahead 차단.

    diag_earnings_preview_backtest.grade_factors 와 동일.
    """
    if not series:
        return None, None
    b = _d(base_dt)
    if b is None:
        return None, None
    past = [(d, v) for d, v in series if d <= b]
    if not past:
        return None, None
    level = past[-1][1]
    cutoff = b - pd.Timedelta(days=int(drift_days))
    older = [v for d, v in past if d <= cutoff]
    if not older:
        return None, level
    return level - older[-1], level


def news_digest(items: list, since=None, limit: int = PREVIEW_NEWS_MAX) -> tuple:
    """(건수, JSON 문자열). since 이후 기사만 — 스냅샷 간 '증분'이 되게 한다.

    since 가 None 이면(=D-7 첫 스냅샷) 최근 limit 건을 담는다.
    """
    s = _d(since) if since is not None else None
    picked = []
    for it in (items or []):
        if s is not None:
            d = _d(it.get("date"))
            if d is None or d < s:
                continue
        picked.append({"d": it.get("date", ""), "t": it.get("title", ""),
                       "s": it.get("site", ""), "u": it.get("url", "")})
        if len(picked) >= int(limit):
            break
    if not picked:
        return 0, ""
    try:
        return len(picked), json.dumps(picked, ensure_ascii=False)
    except Exception:
        return len(picked), ""


def parse_news_json(s: str) -> list[dict]:
    """News_JSON → [{d,t,s,u}]. 깨져 있으면 빈 목록 (표시가 죽지 않게)."""
    txt = str(s or "").strip()
    if not txt:
        return []
    try:
        v = json.loads(txt)
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    except Exception:
        return []


def target_upside_pct(target_mean, price):
    """목표주가 대비 상승 여력(%). 음수면 이미 목표가를 넘어선 상태."""
    t, p = _num(target_mean), _num(price)
    if t is None or p is None or p <= 0:
        return None
    return (t - p) / p * 100.0


# ── 행 조립 ───────────────────────────────────────────────────────────────

def preview_row(ev: dict, phase: str, metrics: dict, now_et: str = "") -> list:
    """스냅샷 1건 → 시트 행. 결측은 공란으로 두고 Data_Flags 에 사유를 남긴다.

    ⚠️ 여기서 계산하지 않는다. 계산은 automation 이 한 번만 하고, 앱은 저장된
       스냅샷을 그대로 읽는다 — 메일 숫자와 앱 숫자가 구조적으로 같아야 한다.
    """
    m = metrics or {}
    eid = event_id(ev.get("ticker"), ev.get("earnings_date"))
    flags = [f for f in (m.get("flags") or []) if f]

    # ── 락스텝 자기점검 ────────────────────────────────────────────────────
    #   스키마에는 내부자 열이 있는데 호출부가 값도 안 주고 no_insider 플래그도
    #   안 달았다면, 그 호출부는 내부자 블록을 모르는 **구버전**이다.
    #   실제로 2026-08-17 에 이 상태로 돌았다 — earnings_core 만 배포되고
    #   run_earnings_watch 가 구버전이라 30~33열이 조용히 공란으로 저장됐다.
    #   나쁜 데이터가 들어가진 않지만 수집이 통째로 누락되고, 로그만 봐서는
    #   정상 실행과 구분되지 않는다. 그래서 여기서 크게 알린다.
    if ("Insider_Cov_D" in PREVIEW_COLS
            and "ins_cov_d" not in m and "no_insider" not in flags):
        print("[FATAL] 락스텝 불일치 — Earnings_Preview 스키마에는 내부자 열이 "
              "있는데 호출부가 값을 주지 않았습니다. run_earnings_watch.py 가 "
              "구버전일 가능성이 높습니다. 30~33열이 공란으로 저장됩니다.")
        flags.append("stale_caller")

    row = [
        preview_snapshot_id(eid, phase),
        eid,
        str(ev.get("ticker") or "").upper(),
        str(ev.get("earnings_date") or ""),
        str(ev.get("timing") or ""),
        str(phase or ""),
        ("" if _num(ev.get("days_until")) is None else int(ev.get("days_until"))),
        str(now_et or ""),

        _blank(m.get("price")),
        _blank(m.get("exp_median_pct")), _blank(m.get("exp_worst_pct")),

        _blank(m.get("est_eps")), _blank(m.get("est_eps_yoy_pct")),
        _blank(m.get("est_revision_pct")),
        _blank(m.get("est_revenue")), _blank(m.get("est_revenue_yoy_pct")),
        _blank(m.get("target_mean")), _blank(m.get("target_upside_pct")),

        _blank(m.get("rs_20d_pct")), _blank(m.get("beat_rate_pct")),
        _blank(m.get("surprise_avg_pct")), _blank(m.get("grade_buy_pct")),
        _blank(m.get("grade_drift_90d")), int(m.get("sample_n_q") or 0),

        int(m.get("news_count") or 0), str(m.get("news_json") or ""),
        "",                                    # Transcript_Summary — 3단계 예약

        ",".join(flags), str(m.get("notes") or ""),

        # 내부자 — 조회 실패 시 전부 공란. 0 을 쓰면 "없었다"와 "못 봤다"가 섞인다.
        _blank(m.get("ins_sale_val")), _blank(m.get("ins_sale_n")),
        _blank(m.get("ins_buy_val")), _blank(m.get("ins_cov_d")),
    ]
    return (row + [""] * PREVIEW_NCOL)[:PREVIEW_NCOL]


def parse_preview(values: list) -> list[dict]:
    """Earnings_Preview get_all_values → dict 목록 (헤더 제외)."""
    out = []
    if not values or len(values) < 2:
        return out
    for i, r in enumerate(values[1:], start=2):
        r = (list(r) + [""] * PREVIEW_NCOL)[:PREVIEW_NCOL]
        d = {c: r[j] for j, c in enumerate(PREVIEW_COLS)}
        d["_row"] = i
        out.append(d)
    return out


def preview_index(rows: list) -> dict:
    """[{...}] → {(Event_ID, Phase): row}. 중복 발동 차단용."""
    out = {}
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        k = (str(r.get("Event_ID") or ""), str(r.get("Phase") or ""))
        if k[0] and k[1]:
            out[k] = r
    return out


def last_snapshot_at(rows: list, eid: str):
    """해당 이벤트의 **가장 최근 스냅샷 시각**. 뉴스 증분의 기준점.

    없으면 None → 첫 스냅샷이므로 최근 기사를 그대로 담는다.
    """
    best = None
    for r in (rows or []):
        if str(r.get("Event_ID") or "") != str(eid or ""):
            continue
        d = _d(str(r.get("Snapshot_At") or "")[:10])
        if d is None:
            continue
        if best is None or d > best:
            best = d
    return best
