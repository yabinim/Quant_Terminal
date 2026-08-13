# -*- coding: utf-8 -*-
"""scanner_core.py — 3버킷 AI 종목 스캐너 SSOT.

app.py(1.6 스캐너 UI)와 automation/run_scanner_scan.py 가 **같은 이 파일**을 임포트한다.
점수 계산·프롬프트·상수·표시 포맷·70점 판정이 전부 여기 한 곳에만 존재하므로
"앱에서 본 숫자"와 "이메일로 받은 숫자"가 구조적으로 갈라질 수 없다.

설계 원칙
---------
1. **Streamlit 의존 없음.** 진행률/로그는 훅(set_progress_hook·set_log_hook)으로 주입한다.
   → app.py 는 st.progress/st.warning 을 연결하고, 자동화는 print 를 연결한다.
2. **캐시도 SSOT.** app.py 의 `@st.cache_data(ttl=...)` 를 모듈 레벨 TTL 캐시로 대체한다.
   Streamlit 리런 사이에도 모듈은 살아있으므로 동작이 동일하고, 자동화도 같은 캐시를 쓴다.
3. **Gemini 는 gemini_core 경유.** 재시도·폴백 정책이 양쪽에서 달라지지 않게 한다.
4. **절대 앵커링.** 모든 점수는 유니버스 구성과 무관하게 종목 고유값이다.
   (풀 상대정규화 금지 — 유니버스가 1종목만 달라져도 전 종목 점수가 흔들리기 때문)

⚠️ 이 파일을 수정하면 app.py 와 automation 을 **함께** 배포해야 한다(lockstep).
"""

import os
import re
import json
import time
import threading
import traceback
import concurrent.futures
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
import pytz

import regime_core as rc
import gemini_core
import fmp_extras as fx

# ── 공용 상수 ─────────────────────────────────────────────────────────────────
_FMP_BASE = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 7
_MARKET_ET_TZ = pytz.timezone("America/New_York")

# 점수표 스키마 버전 — 변경 시 Scanner_Last_Result 옛 캐시가 자동 무효화된다.
# v3-3bucket-auto: run_by/run_at 메타 추가 (자동화 스캔과 수동 스캔 구분).
# SSOT 버전 스탬프 — app.py 가 import 직후 대조한다.
# 배포 누락/재부팅 누락 시 AttributeError 대신 명확한 안내를 띄우기 위함.
SSOT_VERSION = "2026-08-12a"

SCANNER_SCHEMA_VERSION = "v3-3bucket-auto"

# 절대 컷오프 — Final Score 가 이 값 미만이면 "관망"(추천 없음). 절대 앵커링이라 의미 보존.
SCANNER_CUTOFF = {"leaders": 55.0, "setups": 50.0, "expansion": 50.0}

# ── 워치리스트 자동 편입 기준선 (엔진별) ────────────────────────────────────
# 세 엔진은 가중치 구조가 달라 **같은 70점이 같은 엄격도가 아니다.** 실측(2026-08-05):
#   주도주 중앙 57.26 · 최대 72.17 → 70점 이상 5.3%
#   대기주 중앙 62.72 · 최대 72.30 → 70점 이상 6.3%
#   확산주 중앙 66.76 · 최대 77.44 → 70점 이상 32.6% / 76점 이상 4.7%
# 확산주는 모멘텀·RS 를 의도적으로 뺀 자리에 Structural(Gemini) 0.40 이 들어가 하방이
# 막혀 있다(무특징 종목의 기본값 ≈ 66점). 그래서 확산주만 76 으로 올려 엄격도를 맞춘다.
_DEF_TH = {"leaders": 70.0, "emerging": 70.0, "expansion": 76.0}
WATCHLIST_AUTO_ADD_THRESHOLDS = {
    k: float(os.environ.get(f"WATCHLIST_TH_{k.upper()}", v) or v) for k, v in _DEF_TH.items()
}
# 하위 호환 — 엔진 미지정 호출부의 기본값
WATCHLIST_AUTO_ADD_THRESHOLD = WATCHLIST_AUTO_ADD_THRESHOLDS["leaders"]


def watchlist_threshold(engine: str) -> float:
    """엔진별 워치리스트 편입 기준선. 앱 배지·이메일·자동 추가가 전부 이 함수를 본다."""
    return float(WATCHLIST_AUTO_ADD_THRESHOLDS.get(str(engine).strip(),
                                                   WATCHLIST_AUTO_ADD_THRESHOLD))

# 자동 추가 종목의 기본 알림 플래그 (app.py `_WL_ALERT_DEFAULT` 와 동일해야 함)
WATCHLIST_AUTO_ADD_ALERT_STATES = "entry,risk,watch"

# 데이터 커버리지 하한 — 유니버스 대비 채점 성공률이 이 값 미만이면 "오염(degraded)" 으로
# 표시하고 **워치리스트 자동 편입에서 제외**한다.
# (Starter 플랜 300콜/분 초과로 확산주 76종목 중 3종목만 채점됐던 사고 재발 방지)
SCAN_MIN_COVERAGE = 0.60

ENGINE_LABELS = {"leaders": "주도주", "emerging": "대기주", "expansion": "확산주"}

# ── 누락 티커 사유 기록 ─────────────────────────────────────────────────────
# 커버리지 93% 같은 숫자만으로는 "어떤 3종목이 왜 빠졌는지"를 알 수 없다.
# API 호출은 성공했는데 데이터가 없는 경우(상장폐지·OTC·신규상장·환각 심볼)를
# 구분해야 내러티브 품질 문제인지 단순 데이터 공백인지 판단할 수 있다.
# 스캔은 순차 실행이므로 모듈 레벨 dict 로 충분하다.
_LAST_DROPS = {}


def _reset_drops(engine: str):
    _LAST_DROPS[engine] = []


def _record_drop(engine: str, ticker: str, reason: str):
    _LAST_DROPS.setdefault(engine, []).append({"ticker": ticker, "reason": reason})


def get_last_drops(engine: str) -> list:
    """직전 스캔에서 제외된 티커와 사유. [{"ticker","reason"}, ...]"""
    return list(_LAST_DROPS.get(engine) or [])


def _drop_reason_from_price(close_series, close_num, min_bars: int) -> str:
    """가격 데이터 상태로 제외 사유를 분류한다."""
    try:
        if close_series is None or (hasattr(close_series, "empty") and close_series.empty):
            return "가격 이력 없음 (FMP 응답 0건 — 상장폐지·OTC·미존재 심볼 가능)"
        n = len(close_num) if close_num is not None else 0
        if n < min_bars:
            return f"거래일 부족 ({n}일 / 최소 {min_bars}일 — 신규 상장 가능)"
        return "거래량 데이터 없음"
    except Exception:
        return "데이터 확인 불가"

# ── 스캐너 티커 검증 ──────────────────────────────────────────────────────────
_SCANNER_TICKER_BLACKLIST = frozenset(
    {
        "EXPANDING",
        "WINNER",
        "WINNERS",
        "WEEKLY",
        "THEME",
        "TOP",
        "AND",
        "PART",
    }
)
_SCANNER_TICKER_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9.-]*$")

# ── Gemini 배치 설정 ─────────────────────────────────────────────────────────
_SCANNER_NARRATIVE_BATCH_MODEL_ID = "gemini-2.5-flash"
# 티커를 한 번에 너무 많이 넘기면 응답이 잘려 JSONDecodeError 가 날 수 있어 상한을 둔다.
_SCANNER_NARRATIVE_TICKER_CHUNK_SIZE = 20

SCANNER_NARRATIVE_API_FAIL_MESSAGE = "API 응답 지연 (보정 점수 적용)"
# 아래 사유면 Narrative 원점수는 0이며 Final 에서 내러티브 가중치를 분모에서 제외
SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS = frozenset(
    {
        SCANNER_NARRATIVE_API_FAIL_MESSAGE,
        "내러티브 텍스트가 부족합니다.",
        "배치 응답에 누락됨",
        "배치 미응답",
        "Gemini 배치 평가 실패",
    }
)

_SCANNER_GEMINI_CHUNK_MAX_RETRIES = 3
_SCANNER_GEMINI_CHUNK_RETRY_SLEEP_SEC = 5


# ══════════════════════════════════════════════════════════════════════════════
# 1. 주입 훅 (Streamlit 분리 계층)
# ══════════════════════════════════════════════════════════════════════════════
_progress_hook = None      # callable(frac: float, text: str) -> None
_log_hook = None           # callable(level: str, msg: str) -> None
_genai_client_provider = None  # callable() -> genai.Client
_fmp_key_provider = None   # callable() -> str


def set_progress_hook(fn):
    """진행률 훅 등록. app.py 는 st.progress, 자동화는 print 를 연결한다."""
    global _progress_hook
    _progress_hook = fn


def set_log_hook(fn):
    """경고/오류 로그 훅 등록. fn(level, msg) — level ∈ {'info','warn','error'}."""
    global _log_hook
    _log_hook = fn


def set_genai_client_provider(fn):
    """genai.Client 를 돌려주는 콜러블 등록 (지연 생성)."""
    global _genai_client_provider
    _genai_client_provider = fn


def set_fmp_key_provider(fn):
    """FMP API 키를 돌려주는 콜러블 등록. 미등록 시 환경변수 FMP_API_KEY 사용."""
    global _fmp_key_provider
    _fmp_key_provider = fn


def _progress(frac, text=""):
    """진행률 통지. frac=None 은 '불확정'(구 st.spinner) — 훅에 None 그대로 전달한다.

    ⚠️ float(None) 이 TypeError 를 내면 except 가 삼켜서 스피너 상태가
       전부 유실되므로 None 을 분기해야 한다.
    """
    if _progress_hook is None:
        return
    try:
        _progress_hook(None if frac is None else float(frac), str(text))
    except Exception:
        pass


def _log(level, msg):
    if _log_hook is None:
        return
    try:
        _log_hook(str(level), str(msg))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# 2. TTL 캐시 (@st.cache_data 대체 — 앱·자동화 동일 동작)
# ══════════════════════════════════════════════════════════════════════════════
_cache_store = {}
_cache_lock = threading.Lock()


def _cache_key(name, args, kwargs):
    try:
        return (name, repr(args), repr(sorted(kwargs.items())))
    except Exception:
        return (name, str(args), str(kwargs))


def ttl_cache(ttl: int = 3600):
    """`@st.cache_data(ttl=...)` 대체. 프로세스 수명 동안 유지되는 스레드 안전 TTL 캐시.

    Streamlit 은 리런마다 스크립트를 다시 실행하지만 임포트된 모듈은 재사용하므로
    모듈 레벨 딕셔너리가 st.cache_data 와 동일한 수명을 갖는다.
    """
    def deco(fn):
        def wrapper(*args, **kwargs):
            key = _cache_key(fn.__name__, args, kwargs)
            now = time.time()
            with _cache_lock:
                hit = _cache_store.get(key)
                if hit is not None and (now - hit[0]) < ttl:
                    return hit[1]
            val = fn(*args, **kwargs)
            with _cache_lock:
                _cache_store[key] = (now, val)
            return val
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.clear = lambda: clear_cache(fn.__name__)
        return wrapper
    return deco


def clear_cache(prefix: str = None):
    """캐시 비우기. prefix 지정 시 해당 함수 캐시만."""
    with _cache_lock:
        if prefix is None:
            _cache_store.clear()
            return
        for k in [k for k in _cache_store if k[0] == prefix]:
            _cache_store.pop(k, None)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Gemini 브리지 (gemini_core 경유 — 재시도·폴백 정책 SSOT)
# ══════════════════════════════════════════════════════════════════════════════
class _GeminiResponse:
    """gemini_core.generate_text() 결과를 기존 호출부(`response.text`)와 호환시키는 얇은 래퍼."""

    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


def _scanner_narrative_batch_generate_chunk_with_retries(prompt: str):
    """청크 단위 Gemini 호출. gemini_core 의 지터 백오프 + 모델 폴백을 그대로 사용한다.

    스캐너는 JSON 배열만 받아야 하므로 response_mime_type=application/json 을 강제하고,
    thinking_budget=0 으로 사고 토큰이 출력 예산을 깎지 않게 한다.
    """
    if _genai_client_provider is None:
        raise RuntimeError("genai 클라이언트 프로바이더가 등록되지 않았습니다 "
                           "(set_genai_client_provider 호출 필요).")
    client = _genai_client_provider()
    text = gemini_core.generate_text(
        client,
        prompt,
        temperature=0.0,
        top_p=1,
        max_output_tokens=8192,
        thinking_budget=0,
        response_mime_type="application/json",
        primary_attempts=_SCANNER_GEMINI_CHUNK_MAX_RETRIES,
        fallback_attempts=_SCANNER_GEMINI_CHUNK_MAX_RETRIES,
        log=lambda m: _log("warn", m),
        label="스캐너 내러티브 배치",
    )
    return _GeminiResponse(text)


def _log_warn(*parts):
    """구 st.warning(...) 호출부 호환 — 인자를 이어붙여 warn 로그로 보낸다."""
    _log("warn", " ".join(str(p) for p in parts))


class _ProgressProxy:
    """구 `st.progress(...)` 반환 객체 호환 셔틀. 실제 표시는 진행률 훅이 담당한다."""

    __slots__ = ()

    def __init__(self, text=""):
        _progress(0.0, text)

    def progress(self, frac, text=""):
        _progress(frac, text)

    def empty(self):
        return None


class _spinner:
    """구 `with st.spinner("..."):` 호환. 진입 시 상태 텍스트만 전달한다."""

    __slots__ = ("_text",)

    def __init__(self, text):
        self._text = str(text or "")

    def __enter__(self):
        _progress(None, self._text)
        return self

    def __exit__(self, *exc):
        return False


# ══════════════════════════════════════════════════════════════════════════════
# 4. 스캐너 본체 (app.py 에서 이관 — 로직 무변경)
# ══════════════════════════════════════════════════════════════════════════════
def to_float(value):
    """Safely convert numeric values (e.g. from FMP) to float."""
    if value is None:
        return np.nan
    if isinstance(value, (list, tuple, dict, pd.Series, pd.DataFrame)):
        return np.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan

def calculate_rsi(close_series, window=14):
    """Calculate RSI from close price series."""
    if close_series is None:
        return pd.Series(dtype=float)

    close = pd.to_numeric(close_series, errors="coerce").dropna()
    if close.empty:
        return pd.Series(dtype=float)

    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.rolling(window=window, min_periods=window).mean()
    avg_loss = losses.rolling(window=window, min_periods=window).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_period_return(close_series, lookback_days):
    """
    Return percentage performance based on lookback trading days.
    Example: lookback_days=5 means return vs 5 trading days ago.
    """
    if close_series is None:
        return np.nan

    clean = pd.to_numeric(close_series, errors="coerce").dropna()
    if len(clean) <= lookback_days:
        return np.nan

    latest = clean.iloc[-1]
    past = clean.iloc[-(lookback_days + 1)]

    if pd.isna(latest) or pd.isna(past) or past == 0:
        return np.nan
    return (latest / past - 1.0) * 100

def is_valid_scanner_ticker(ticker: str) -> bool:
    """스캐너·내러티브 유니버스에 올릴 수 있는 티커 심볼만 허용."""
    t = str(ticker or "").strip().upper()
    if not t or t in _SCANNER_TICKER_BLACKLIST:
        return False
    if len(t) <= 1 or len(t) > 5:
        return False
    if not _SCANNER_TICKER_TOKEN_RE.fullmatch(t):
        return False
    core = re.sub(r"[^A-Z]", "", t)
    if not core or not any(ch.isalpha() for ch in core):
        return False
    return True

def filter_scanner_ticker_list(tickers):
    """순서 유지·중복 제거·검증 통과 심볼만 반환."""
    out = []
    seen = set()
    for x in tickers or []:
        t = str(x or "").strip().upper()
        if not is_valid_scanner_ticker(t):
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out

def _fmp_key() -> str:
    """FMP API 키. 프로바이더 우선(app.py=st.secrets), 없으면 환경변수(자동화)."""
    if _fmp_key_provider is not None:
        try:
            k = str(_fmp_key_provider() or "").strip()
            if k:
                return k
        except Exception:
            pass
    return str(os.environ.get("FMP_API_KEY", "") or "").strip()

@ttl_cache(ttl=3600)
def _fmp_profile(ticker: str) -> dict:
    k = _fmp_key()
    if not k: return {}
    try:
        r = fx.fmp_get(f"{_FMP_BASE}/profile?symbol={ticker}&apikey={k}", timeout=_FMP_TIMEOUT)
        d = r.json()
        return d[0] if isinstance(d, list) and d else {}
    except Exception:
        return {}

@ttl_cache(ttl=3600)
def _fmp_ratios(ticker: str) -> dict:
    """ratios-ttm — stable API only (v3 legacy 차단됨)"""
    k = _fmp_key()
    if not k: return {}
    try:
        r = fx.fmp_get(f"{_FMP_BASE}/ratios-ttm?symbol={ticker}&apikey={k}", timeout=_FMP_TIMEOUT)
        if r is not None:
            d = r.json()
            return d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    except Exception:
        pass
    return {}

@ttl_cache(ttl=3600)
def _fmp_key_metrics(ticker: str) -> dict:
    """key-metrics-ttm — stable API only (v3 legacy 차단됨)"""
    k = _fmp_key()
    if not k: return {}
    try:
        r = fx.fmp_get(f"{_FMP_BASE}/key-metrics-ttm?symbol={ticker}&apikey={k}", timeout=_FMP_TIMEOUT)
        if r is not None:
            d = r.json()
            return d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    except Exception:
        pass
    return {}

@ttl_cache(ttl=3600)
def _fmp_income(ticker: str) -> dict:
    """income-statement annual: revenue, operatingIncome, netIncome, epsdiluted"""
    k = _fmp_key()
    if not k: return {}
    try:
        r = fx.fmp_get(f"{_FMP_BASE}/income-statement?symbol={ticker}&period=annual&limit=2&apikey={k}", timeout=_FMP_TIMEOUT)
        d = r.json()
        return {"latest": d[0] if d else {}, "prev": d[1] if len(d) > 1 else {}}
    except Exception:
        return {}

@ttl_cache(ttl=1800)
def _fmp_price_history(ticker: str, limit: int = 252) -> pd.DataFrame:
    """FMP historical-price-eod → Close/Open/High/Low/Volume DataFrame 반환."""
    k = _fmp_key()
    if not k:
        return pd.DataFrame()
    try:
        r = fx.fmp_get(
            f"{_FMP_BASE}/historical-price-eod/full?symbol={ticker}&limit={limit}&apikey={k}",
            timeout=_FMP_TIMEOUT
        )
        if r is None:
            return pd.DataFrame()
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "date" not in df.columns:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        rename_map = {"close": "Close", "open": "Open", "high": "High",
                      "low": "Low", "volume": "Volume", "adjClose": "Adj Close"}
        df = df.rename(columns=rename_map)
        for col in ["Close", "Open", "High", "Low"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "Volume" in df.columns:
            df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()

@ttl_cache(ttl=1800)
def _fmp_batch_price_history(tickers: list, limit: int = 130) -> dict:
    """여러 티커를 ThreadPoolExecutor로 병렬 FMP 호출 → {ticker: DataFrame} 반환.
    순차 루프 대비 섹터 스캔(30~50종목) 기준 약 5~8배 속도 향상.
    max_workers=8: FMP Starter 레이트 리밋(300 req/min) 여유 있게 유지.
    """
    if not tickers:
        return {}

    import concurrent.futures

    _MAX_WORKERS = 8  # FMP 레이트 리밋 감안한 동시 요청 수

    def _fetch_one(tk):
        df = _fmp_price_history(tk, limit=limit)
        return tk, df

    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, tk): tk for tk in tickers}
        for future in concurrent.futures.as_completed(futures):
            try:
                tk, df = future.result()
                if not df.empty:
                    result[tk] = df
            except Exception:
                pass  # 개별 티커 실패가 전체 배치를 중단시키지 않음
    return result

@ttl_cache(ttl=3600)
def _fmp_quote(ticker: str) -> dict:
    """FMP /quote — MA50/MA200/52w고저/현재가/시총. _fmp_fill 전용 캐싱 래퍼."""
    k = _fmp_key()
    if not k:
        return {}
    try:
        r = fx.fmp_get(f"{_FMP_BASE}/quote?symbol={ticker}&apikey={k}", timeout=_FMP_TIMEOUT)
        if r is not None:
            d = r.json()
            return d[0] if isinstance(d, list) and d else {}
    except Exception:
        pass
    return {}

@ttl_cache(ttl=3600)
def _fmp_analyst_estimates(ticker: str) -> list:
    """FMP /analyst-estimates (annual, limit=4) — Forward P/E 계산용. _fmp_fill 전용 캐싱 래퍼."""
    k = _fmp_key()
    if not k:
        return []
    try:
        r = fx.fmp_get(
            f"{_FMP_BASE}/analyst-estimates?symbol={ticker}&period=annual&limit=4&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r is not None:
            d = r.json()
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []

@ttl_cache(ttl=3600)
def _fmp_balance_sheet(ticker: str) -> dict:
    """FMP /balance-sheet-statement (limit=2) — ROE·D/E 직접 계산용. _fmp_fill 전용 캐싱 래퍼."""
    k = _fmp_key()
    if not k:
        return {}
    try:
        r = fx.fmp_get(
            f"{_FMP_BASE}/balance-sheet-statement?symbol={ticker}&limit=2&apikey={k}",
            timeout=_FMP_TIMEOUT,
        )
        if r is not None:
            d = r.json()
            return d[0] if isinstance(d, list) and d else {}
    except Exception:
        pass
    return {}

def _fmp_fill(info: dict, ticker: str) -> dict:
    """info dict의 빈 필드를 FMP로 채워 반환.
    ※ FMP Starter 플랜 실제 제공 필드만 사용.
    기존 값은 절대 덮어쓰지 않음.
    """
    info = dict(info)

    # ── profile: 회사명·섹터·설명·밸류에이션 기본값 ─────────────────
    # 밸류에이션 필드(P/E, EPS 등)가 없으면 항상 profile 조회
    _need_prof = not all([
        info.get("longName") or info.get("shortName"),
        info.get("sector"),
        info.get("longBusinessSummary"),
        info.get("trailingPE") or info.get("trailingEps"),  # 밸류에이션도 체크
    ])
    if _need_prof:
        p = _fmp_profile(ticker)
        if p:
            if not (info.get("longName") or info.get("shortName")):
                info["longName"] = str(p.get("companyName") or p.get("name") or ticker)
            if not info.get("sector"):
                info["sector"] = str(p.get("sector") or "")
            if not info.get("industry"):
                info["industry"] = str(p.get("industry") or "")
            if not info.get("longBusinessSummary"):
                info["longBusinessSummary"] = str(p.get("description") or "")
            if not info.get("website"):
                info["website"] = str(p.get("website") or "")
            if not info.get("country"):
                info["country"] = str(p.get("country") or "N/A")
            if not info.get("marketCap"):
                mkt = p.get("mktCap") or p.get("marketCap") or p.get("marketCapitalization")
                if mkt:
                    info["marketCap"] = to_float(mkt)
            if not info.get("fullTimeEmployees"):
                info["fullTimeEmployees"] = p.get("fullTimeEmployees") or p.get("employees")
            # 현재가
            if not info.get("currentPrice"):
                price = p.get("price") or p.get("lastPrice")
                if price:
                    info["currentPrice"] = to_float(price)
            # 52주 고저
            if not info.get("fiftyTwoWeekHigh"):
                v = to_float(p.get("range", "").split("-")[1] if "-" in str(p.get("range","")) else None)
                if pd.notna(v): info["fiftyTwoWeekHigh"] = v
            if not info.get("fiftyTwoWeekLow"):
                v = to_float(p.get("range", "").split("-")[0] if "-" in str(p.get("range","")) else None)
                if pd.notna(v): info["fiftyTwoWeekLow"] = v

    # ── quote: MA50/MA200/yearHigh/yearLow (기술적 분석용) ───────────
    _need_quote = not all([info.get("fiftyDayAverage"), info.get("twoHundredDayAverage")])
    if _need_quote:
        try:
            q_item = _fmp_quote(ticker)  # ← 캐싱 래퍼 사용 (기존: 직접 requests.get)
            if q_item:
                if not info.get("fiftyDayAverage"):
                    v = to_float(q_item.get("priceAvg50"))
                    if pd.notna(v): info["fiftyDayAverage"] = v
                if not info.get("twoHundredDayAverage"):
                    v = to_float(q_item.get("priceAvg200"))
                    if pd.notna(v): info["twoHundredDayAverage"] = v
                if not info.get("fiftyTwoWeekHigh"):
                    v = to_float(q_item.get("yearHigh"))
                    if pd.notna(v): info["fiftyTwoWeekHigh"] = v
                if not info.get("fiftyTwoWeekLow"):
                    v = to_float(q_item.get("yearLow"))
                    if pd.notna(v): info["fiftyTwoWeekLow"] = v
                if not info.get("currentPrice"):
                    v = to_float(q_item.get("price"))
                    if pd.notna(v): info["currentPrice"] = v
                if not info.get("marketCap"):
                    v = to_float(q_item.get("marketCap"))
                    if pd.notna(v): info["marketCap"] = v
        except Exception:
            pass

    # ── profile 보완 필드 (quote 블록과 독립) ──────────────────────────────
    try:
        _p = _fmp_profile(ticker)
        if _p:
            if not info.get("earningsDate") and _p.get("earningsAnnouncement"):
                info["earningsDate"] = [str(_p["earningsAnnouncement"])[:10]]
            if _p.get("isEtf") and not info.get("quoteType"):
                info["quoteType"] = "ETF"
            if not info.get("trailingPE"):
                v = to_float(_p.get("pe") or _p.get("peRatio"))
                if pd.notna(v) and v > 0: info["trailingPE"] = v
            if not info.get("trailingEps"):
                v = to_float(_p.get("eps") or _p.get("epsActual"))
                if pd.notna(v): info["trailingEps"] = v
            if not info.get("priceToBook"):
                v = to_float(_p.get("priceToBook") or _p.get("pbRatio"))
                if pd.notna(v) and v > 0: info["priceToBook"] = v
            if not info.get("beta"):
                v = to_float(_p.get("beta"))
                if pd.notna(v): info["beta"] = v
            if not info.get("dividendYield"):
                v = to_float(_p.get("lastDiv"))
                price_v = to_float(_p.get("price"))
                if pd.notna(v) and pd.notna(price_v) and price_v > 0:
                    info["dividendYield"] = v / price_v
            if not info.get("_fmp_dcf"):
                v = to_float(_p.get("dcf"))
                if pd.notna(v) and v > 0: info["_fmp_dcf"] = v
    except Exception:
        pass

    # ── analyst-estimates: Forward P/E 계산 ────────────────────────────────
    # 실제 API 응답: epsAvg 필드 사용, period=annual&limit=4로 가장 가까운 연도 우선
    if not info.get("forwardPE"):
        try:
            ae_data = _fmp_analyst_estimates(ticker)  # ← 캐싱 래퍼 사용 (기존: 직접 requests.get)
            if isinstance(ae_data, list) and ae_data:
                current_year = datetime.now(_MARKET_ET_TZ).year
                # 날짜 오름차순 정렬 → 가장 가까운 미래 연도 우선
                ae_sorted = sorted(
                    ae_data,
                    key=lambda x: str(x.get("date") or x.get("year") or "9999")
                )
                for ae in ae_sorted:
                    ae_year_str = str(ae.get("date") or ae.get("year") or "")[:4]
                    try:
                        ae_year = int(ae_year_str)
                    except Exception:
                        continue
                    if ae_year < current_year:
                        continue
                    # 실제 API 필드명: epsAvg (확인됨)
                    est_eps = to_float(
                        ae.get("epsAvg") or ae.get("estimatedEpsAvg") or
                        ae.get("estimatedEps") or ae.get("eps")
                    )
                    cur_price = to_float(
                        info.get("currentPrice") or info.get("price") or
                        info.get("regularMarketPrice") or info.get("_fmp_price")
                    )
                    # currentPrice 없으면 캐싱된 profile에서 가져오기 (기존: 별도 requests.get)
                    if (cur_price is None or pd.isna(cur_price)):
                        _p_cur = _fmp_profile(ticker)
                        cur_price = to_float(_p_cur.get("price")) if _p_cur else np.nan
                        if pd.notna(cur_price):
                            info["currentPrice"] = cur_price
                    if pd.notna(est_eps) and est_eps > 0 and pd.notna(cur_price) and cur_price > 0:
                        fwd_pe = round(float(cur_price) / float(est_eps), 2)
                        if 0 < fwd_pe < 2000:
                            info["forwardPE"] = fwd_pe
                            break
        except Exception:
            pass

    # ── key-metrics-ttm: EV/Sales, EV/FCF, EV/EBITDA, ROE (실제 확인 필드) ──
    if not info.get("_fmp_ev_to_sales") or not info.get("_fmp_ev_to_fcf") or not info.get("enterpriseToEbitda"):
        km = _fmp_key_metrics(ticker)
        if km:
            if not info.get("_fmp_ev_to_sales"):
                v = to_float(km.get("evToSalesTTM"))
                if pd.notna(v) and v > 0: info["_fmp_ev_to_sales"] = v
            if not info.get("_fmp_ev_to_fcf"):
                v = to_float(km.get("evToFreeCashFlowTTM"))
                if pd.notna(v) and v > 0: info["_fmp_ev_to_fcf"] = v
            # EV/EBITDA — 실제 필드명은 대문자 EBITDA
            if not info.get("enterpriseToEbitda"):
                v = to_float(km.get("evToEBITDATTM") or km.get("evToEbitdaTTM"))
                if pd.notna(v) and v > 0: info["enterpriseToEbitda"] = v
            if not info.get("marketCap"):
                v = to_float(km.get("marketCapTTM"))
                if pd.notna(v) and v > 0: info["marketCap"] = v
            if not info.get("returnOnEquity"):
                v = to_float(km.get("returnOnEquityTTM"))
                if pd.notna(v): info["returnOnEquity"] = v

    # ── ratios-ttm: 실제 API 응답에서 확인된 정확한 필드명 ────────────
    # operatingMargins만 채워져도 P/E 등이 없으면 다시 조회
    _need_ratios = not all([
        info.get("trailingPE"),
        info.get("priceToBook"),
        info.get("debtToEquity"),
        info.get("operatingMargins"),
        info.get("enterpriseToEbitda"),
    ])
    if _need_ratios:
        rat = _fmp_ratios(ticker)
        if rat:
            if not info.get("trailingPE"):
                v = to_float(rat.get("priceToEarningsRatioTTM"))
                if pd.notna(v) and v > 0: info["trailingPE"] = v
            # Forward P/E는 ratios-ttm에 전용 필드 없음 → profile에서 가져옴 (아래 profile 섹션에서 처리)
            if not info.get("priceToBook"):
                v = to_float(rat.get("priceToBookRatioTTM"))
                if pd.notna(v) and v > 0: info["priceToBook"] = v
            if not info.get("enterpriseToEbitda"):
                v = to_float(rat.get("enterpriseValueMultipleTTM"))
                if pd.notna(v) and v > 0: info["enterpriseToEbitda"] = v
            if not info.get("pegRatio"):
                v = to_float(rat.get("priceToEarningsGrowthRatioTTM"))
                if pd.notna(v): info["pegRatio"] = v
            if not info.get("debtToEquity"):
                v = to_float(rat.get("debtToEquityRatioTTM"))
                if pd.notna(v): info["debtToEquity"] = v
            if not info.get("operatingMargins"):
                v = to_float(rat.get("operatingProfitMarginTTM"))
                if pd.notna(v): info["operatingMargins"] = v
            if not info.get("grossMargins"):
                v = to_float(rat.get("grossProfitMarginTTM"))
                if pd.notna(v): info["grossMargins"] = v
            if not info.get("netMargins"):
                v = to_float(rat.get("netProfitMarginTTM"))
                if pd.notna(v): info["netMargins"] = v
            if not info.get("_fmp_ev_to_sales"):
                v = to_float(rat.get("priceToSalesRatioTTM"))
                if pd.notna(v) and v > 0: info["_fmp_ev_to_sales"] = v
            if not info.get("returnOnEquity"):
                v = to_float(rat.get("returnOnEquityTTM"))
                if pd.notna(v): info["returnOnEquity"] = v
            if not info.get("trailingEps"):
                v = to_float(rat.get("netIncomePerEBTTTM"))
                if pd.notna(v): info["trailingEps"] = v

    # ── income-statement + balance-sheet: ROE·D/E 직접 계산 ─────────
    _need_calc = not all([info.get("returnOnEquity"), info.get("debtToEquity")])
    if _need_calc:
        inc = _fmp_income(ticker)
        latest = inc.get("latest", {})
        prev   = inc.get("prev", {})
        if latest:
            if not info.get("operatingMargins"):
                _rev = to_float(latest.get("revenue"))
                _oi  = to_float(latest.get("operatingIncome"))
                if pd.notna(_rev) and _rev != 0 and pd.notna(_oi):
                    info["operatingMargins"] = float(_oi / _rev)
            if not info.get("trailingEps"):
                _eps = to_float(latest.get("epsdiluted") or latest.get("eps"))
                if pd.notna(_eps): info["trailingEps"] = _eps
            if not info.get("earningsGrowth") and prev:
                _rev_now  = to_float(latest.get("revenue"))
                _rev_prev = to_float(prev.get("revenue"))
                if pd.notna(_rev_now) and pd.notna(_rev_prev) and _rev_prev != 0:
                    info["earningsGrowth"] = float((_rev_now - _rev_prev) / abs(_rev_prev))
            _ni = to_float(latest.get("netIncome"))
            if pd.notna(_ni): info["_fmp_net_income"] = _ni

        # balance-sheet에서 직접 ROE, D/E 계산
        try:
            if not info.get("returnOnEquity") or not info.get("debtToEquity"):
                bs = _fmp_balance_sheet(ticker)  # ← 캐싱 래퍼 사용 (기존: 직접 requests.get)
                if bs:
                    equity = to_float(bs.get("totalStockholdersEquity") or bs.get("stockholdersEquity"))
                    total_debt = to_float(bs.get("totalDebt") or bs.get("longTermDebt"))
                    net_income = to_float(latest.get("netIncome")) if latest else np.nan
                    # ROE = Net Income / Equity
                    if not info.get("returnOnEquity") and pd.notna(net_income) and pd.notna(equity) and equity != 0:
                        info["returnOnEquity"] = float(net_income / equity)
                    # D/E = Total Debt / Equity
                    if not info.get("debtToEquity") and pd.notna(total_debt) and pd.notna(equity) and equity != 0:
                        info["debtToEquity"] = float(total_debt / equity)
        except Exception:
            pass

    return info

def _fmp_fill_parallel_warmup(tickers: list, max_workers: int = 10) -> dict:
    """
    스캐너 루프 전에 _fmp_fill()을 종목별 병렬 실행해 캐시를 웜업한다.
    반환: {ticker: info_dict} — 루프 안에서 바로 조회해 HTTP 재호출 없이 사용.

    max_workers=10: FMP Starter 300 req/min 여유 감안.
    _fmp_fill() 내부 캐시 함수(_fmp_profile 등)는 ttl_cache 공유이므로
    병렬 호출 시 첫 호출만 HTTP, 이후는 캐시 히트 → rate limit 안전.
    """
    import concurrent.futures as _cf

    if not tickers:
        return {}

    results = {}

    def _fill_one(tk):
        try:
            return tk, _fmp_fill({}, tk)
        except Exception:
            return tk, {}

    with _cf.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fill_one, tk): tk for tk in tickers}
        for future in _cf.as_completed(futures):
            try:
                tk, info = future.result()
                results[tk] = info
            except Exception:
                results[futures[future]] = {}

    return results

def clip_series_0_100(series):
    """스캐너 원시 점수(이론상 0~100)를 열 단위로 [0,100]으로 자름 — 동일값 전 시계열→100 부작용 없음."""
    v = pd.to_numeric(series, errors="coerce")
    return v.clip(lower=0.0, upper=100.0)

def anchor_momentum_1m(series):
    """1개월 수익률(%, calculate_period_return 단위) → 0~100 절대 앵커.
    -10%→0 · 0%→50 · +20%→100 (구간 선형, 범위 밖은 양끝값으로 클램프)."""
    v = pd.to_numeric(series, errors="coerce")
    out = pd.Series(np.nan, index=v.index)
    valid = v.notna()
    if valid.any():
        out[valid] = np.interp(
            v[valid].astype(float),
            [-100.0, -10.0, 0.0, 20.0, 100.0],
            [0.0, 0.0, 50.0, 100.0, 100.0],
        )
    return out

def anchor_valuation_pe(series):
    """forwardPE → 0~100 절대 앵커 (재평가 여지). PE 15→100 · 30→60 · 50→20 · 60↑→0.
    PE 결측/0 이하는 NaN(해석 불가) → Available=False 처리용."""
    v = pd.to_numeric(series, errors="coerce")
    out = pd.Series(np.nan, index=v.index)
    valid = v.notna() & (v > 0)
    if valid.any():
        out[valid] = np.interp(
            v[valid].astype(float),
            [0.0, 15.0, 30.0, 50.0, 60.0, 1.0e9],
            [100.0, 100.0, 60.0, 20.0, 0.0, 0.0],
        )
    return out

def _scanner_score_df_format_for_display(score_df: pd.DataFrame, mode: str) -> pd.DataFrame:
    """보조 점수 결측은 50(중립), 모든 Score·Final은 소수 둘째 자리로 정리."""
    df = score_df.copy()
    if mode == "leaders":
        for c in ("Fundamentals Score", "Institutional Score"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(50.0).clip(0.0, 100.0)
    elif mode == "emerging":
        for c in ("Fundamentals Score", "Overextension Score", "Base Maturity Score"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(50.0).clip(0.0, 100.0)
    for c in df.columns:
        if c == "Final Score" or str(c).endswith(" Score"):
            df[c] = pd.to_numeric(df[c], errors="coerce").round(2)
    return df

def emerging_final_weighted_score(score_df):
    """
    대기주(Setups) Final Score — 절대 앵커링. '출발 직전' 신호 중심.
    Vol Accel 0.25 + Narrative 0.20 + Base Maturity 0.20 + Early RS 0.15 + Overextension 0.10 + Fundamentals 0.10.
    Base Maturity: 50MA 근접 + RSI 회복 구간(베이스 성숙=곧 돌파). Overextension은 '덜 과열일수록 높은' 점수.
    결측/비가용 팩터는 분자·분모 모두에서 제외(동적 분모).
    """
    w_v, w_n, w_b, w_e, w_o, w_f = 0.25, 0.20, 0.20, 0.15, 0.10, 0.10

    n = clip_series_0_100(score_df["Narrative Raw"]).fillna(0.0)
    e = clip_series_0_100(score_df["Early RS Raw"])
    v = clip_series_0_100(score_df["Vol Accel Raw"])
    f = clip_series_0_100(score_df["Fundamentals Raw"])
    o = clip_series_0_100(score_df["Overextension Raw"])
    b = clip_series_0_100(score_df["Base Maturity Raw"])

    na = score_df["Narrative Available"].astype(float)
    ea = score_df["Early RS Available"].astype(float)
    va = score_df["Vol Accel Available"].astype(float)
    fa = score_df["Fundamentals Available"].astype(float)
    oa = score_df["Overextension Available"].astype(float)
    ba = score_df["Base Maturity Available"].astype(float)

    numer = (
        va * v.fillna(0.0) * w_v
        + na * n * w_n
        + ba * b.fillna(0.0) * w_b
        + ea * e.fillna(0.0) * w_e
        + oa * o.fillna(0.0) * w_o
        + fa * f.fillna(0.0) * w_f
    )
    denom = va * w_v + na * w_n + ba * w_b + ea * w_e + oa * w_o + fa * w_f
    denom = denom.replace(0.0, np.nan)
    out = (numer / denom).fillna(0.0).clip(upper=100.0)
    return out, n, e, v, f, o, b

def leaders_final_weighted_score(score_df):
    """
    주도주(Leaders) Final Score — 절대 앵커링.
    Regime 0.35 + Momentum(절대) 0.15 + Fundamentals 0.20 + Institutional 0.15 + Narrative 0.15.
    추세·RS는 regime_core.score(0~100, 이미 절대)가 백본으로 흡수 → 별도 RS 항 없음.
    Valuation은 주도주 가중에서 제외(리스크 텍스트용으로만 계산). 결측 팩터는 동적 분모로 제외.
    """
    w_reg, w_m, w_f, w_i, w_n = 0.35, 0.15, 0.20, 0.15, 0.15

    reg = clip_series_0_100(score_df["Regime Score Raw"]).fillna(0.0)
    m = anchor_momentum_1m(score_df["Momentum 1M Raw"]).fillna(0.0)
    f = clip_series_0_100(score_df["Fundamentals Raw"]).fillna(0.0)
    inst = clip_series_0_100(score_df["Institutional Raw"]).fillna(0.0)
    n = clip_series_0_100(score_df["Narrative Raw"]).fillna(0.0)

    rega = score_df["Regime Available"].astype(float)
    fa = score_df["Fundamentals Available"].astype(float)
    na = score_df["Narrative Available"].astype(float)

    numer = (
        rega * reg * w_reg
        + m * w_m
        + fa * f * w_f
        + inst * w_i
        + na * n * w_n
    )
    denom = rega * w_reg + w_m + fa * w_f + w_i + na * w_n
    denom = denom.replace(0.0, np.nan)
    out = (numer / denom).fillna(0.0).clip(upper=100.0)
    return out, n, m, reg, f, inst

def expansion_final_weighted_score(score_df):
    """
    확산주(Next Wave) Final Score — 절대 앵커링. 가격 신호가 없는 2·3차 후발주.
    Structural 0.40 + Accumulation 0.25 + Fundamentals 0.20 + Valuation 0.15.
    모멘텀·RS는 의도적으로 제외(아직 안 움직여 0으로 깔리므로 노이즈). 결측은 동적 분모로 제외.
    """
    w_s, w_a, w_f, w_v = 0.40, 0.25, 0.20, 0.15

    s = clip_series_0_100(score_df["Structural Raw"]).fillna(0.0)
    a = clip_series_0_100(score_df["Accumulation Raw"])
    f = clip_series_0_100(score_df["Fundamentals Raw"])
    val = anchor_valuation_pe(score_df["Valuation PE Raw"])

    sa = score_df["Structural Available"].astype(float)
    aa = score_df["Accumulation Available"].astype(float)
    fa = score_df["Fundamentals Available"].astype(float)
    va = score_df["Valuation Available"].astype(float)

    numer = (
        sa * s * w_s
        + aa * a.fillna(0.0) * w_a
        + fa * f.fillna(0.0) * w_f
        + va * val.fillna(0.0) * w_v
    )
    denom = sa * w_s + aa * w_a + fa * w_f + va * w_v
    denom = denom.replace(0.0, np.nan)
    out = (numer / denom).fillna(0.0).clip(upper=100.0)
    return out, s, a, f, val

def _parse_gemini_ticker_score_json_array(raw_text):
    """Gemini 응답에서 JSON 배열([{{...}},...]) 추출·파싱. 마크다운 제거 + UTF-8 세탁 후 loads."""
    raw = str(raw_text or "")
    raw = raw.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    raw = raw.encode("utf-8", "ignore").decode("utf-8")
    if not raw:
        return []

    candidates = []
    if raw.lstrip().startswith("["):
        candidates.append(raw.strip())
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        g = match.group(0)
        if g not in candidates:
            candidates.append(g)

    last_tb = None
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            # 잘린 JSON·Unterminated string 등: 해당 후보만 포기하고 다음 후보 또는 빈 결과
            continue
        except Exception:
            last_tb = traceback.format_exc()

    if last_tb:
        _log("error", last_tb)
    elif not candidates:
        try:
            raise ValueError("응답에서 JSON 배열 패턴을 찾지 못했습니다.")
        except Exception:
            _log("error", traceback.format_exc())

    return []

def batch_narrative_alignment_scores(tickers, narrative_text):
    """
    Current Leaders: 유니버스 티커에 대해 Narrative Alignment를 Gemini **청크(최대 20티커)별** 호출로 평가.
    반환: ticker -> (score 0~100, reason str)
    """
    clean = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not clean:
        return {}
    nt = str(narrative_text or "").strip()
    if not nt:
        return {t: (0.0, "내러티브 텍스트가 부족합니다.") for t in clean}

    out = {}
    n_chunk = max(1, (len(clean) + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE - 1) // _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE)
    for ci, i in enumerate(range(0, len(clean), _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE), start=1):
        chunk = clean[i : i + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE]
        tickers_json = json.dumps(chunk, ensure_ascii=False)
        prompt = f"""
당신은 월가 탑다운-바텀업 전략가입니다.

[Market Narrative JSON]
{nt}

[Tickers to score (이번 청크에서 모두 평가)]
{tickers_json}

작업:
- 각 티커에 "주도주 적합도"를 0~100 정수로 채점하라. (reason: 한국어 15단어 이내)
- 채점 기준(이 순서로 비중):
  1) 테마 중심성: 테마의 핵심 드라이버(대장)인가, 단순 주변 언급인가. 중심일수록 고점.
  2) 테마 강화 여부: 이 종목이 속한 테마가 지금 강화·확대 중인가, 식어가는가. 강화 중일수록 고점.
  3) 내러티브 일치: winners/top_quant_picks에 명시될수록 고점, 근거 없으면 저점.
- "유명하다/자주 언급된다"는 이유만으로 고점을 주지 말 것.
- 반드시 **JSON 배열 형식만** 응답하라. 마크다운·설명 문장·코드펜스 금지.
- 배열 길이는 반드시 {len(chunk)}이며, 위 티커 리스트의 각 심볼을 **정확히 한 번씩** 포함할 것.

예시 형식:
[{{"ticker":"NVDA","score":88,"reason":"AI칩 테마 핵심 드라이버, 확대 중"}},{{"ticker":"ANET","score":72,"reason":"네트워크 수혜, 테마 주변부"}}]
"""
        try:
            response = _scanner_narrative_batch_generate_chunk_with_retries(prompt)
            raw_text = str(getattr(response, "text", "") or "").strip()
            items = _parse_gemini_ticker_score_json_array(raw_text)
            if not items:
                _log_warn(
                    f"내러티브 배치: 청크 {ci}/{n_chunk} JSON 배열 파싱 결과가 비어 있습니다. "
                    f"({len(chunk)}티커)"
                )
                for t in chunk:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                tk = str(it.get("ticker") or it.get("Ticker") or "").strip().upper()
                if not tk:
                    continue
                sc = to_float(it.get("score"))
                if pd.isna(sc):
                    sc = 0.0
                sc = float(np.clip(sc, 0.0, 100.0))
                reason = str(it.get("reason") or it.get("why") or "").strip() or "N/A"
                out[tk] = (sc, reason)
            for t in chunk:
                if t not in out:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
        except Exception as exc:
            _log_warn(
                f"내러티브 배치 Gemini 호출 실패 ({_SCANNER_GEMINI_CHUNK_MAX_RETRIES}회 재시도 후, 청크 {ci}/{n_chunk}): {exc}"
            )
            _log("error", traceback.format_exc())
            for t in chunk:
                out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
    return out

def batch_narrative_expansion_scores(tickers, narrative_text):
    """
    확산주(Next Wave): 2·3차 공급망 구조 연결 강도를 Gemini **청크(최대 20티커)별** 호출로 채점.
    (구 batch_narrative_emerging_second_order_scores — 3-버킷 재설계에서 확산주 전용으로 이전·강화)
    """
    clean = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not clean:
        return {}
    nt = str(narrative_text or "").strip()
    if not nt:
        return {t: (0.0, "내러티브 텍스트가 부족합니다.") for t in clean}

    out = {}
    n_chunk = max(1, (len(clean) + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE - 1) // _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE)
    for ci, i in enumerate(range(0, len(clean), _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE), start=1):
        chunk = clean[i : i + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE]
        tickers_json = json.dumps(chunk, ensure_ascii=False)
        prompt = f"""
당신은 크로스에셋·공급망 투자 전문가입니다.

[Market Narrative JSON]
{nt}

[Tickers to score (이번 청크)]
{tickers_json}

작업:
- 각 티커를 테마가 옆 섹터로 번질 때의 '2·3차 확산 수혜(Next Wave)'로서 0~100 정수로 채점하라.
- 채점 기준:
  1) 공급망 연결 강도: 테마 driver에서 이 종목까지의 인과 고리가 구체적이고 짧은가. 강할수록 고점.
  2) 미발화 프리미엄: 아직 시장이 주목하지 않은 종목일수록 고점.
  3) 대형주 남발 배제: 단지 유명해서/인접 섹터라서 고른 종목은 감점.
- 인과 고리를 설명할 수 없으면 낮게 채점하라.
- reason은 **인과 고리를 담아 한국어 15단어 이내**. 반드시 **JSON 배열만** 출력, 마크다운 금지.
- 원소 형식: {{"ticker": "VRT", "score": 82, "reason": "데이터센터 전력 폭증→정밀냉각 연쇄"}}
- 배열 길이는 반드시 {len(chunk)}이며 모든 티커를 빠짐없이 포함할 것.

예시:
[{{"ticker":"VRT","score":82,"reason":"데이터센터 전력 폭증→정밀냉각 연쇄"}},{{"ticker":"VST","score":74,"reason":"AI 전력수요→발전사업자 수혜"}}]
"""
        try:
            response = _scanner_narrative_batch_generate_chunk_with_retries(prompt)
            raw_text = str(getattr(response, "text", "") or "").strip()
            items = _parse_gemini_ticker_score_json_array(raw_text)
            if not items:
                _log_warn(
                    f"Emerging 내러티브 배치: 청크 {ci}/{n_chunk} JSON 배열 파싱 결과가 비어 있습니다. "
                    f"({len(chunk)}티커)"
                )
                for t in chunk:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                tk = str(it.get("ticker") or it.get("Ticker") or "").strip().upper()
                if not tk:
                    continue
                sc = to_float(it.get("score"))
                if pd.isna(sc):
                    sc = 0.0
                sc = float(np.clip(sc, 0.0, 100.0))
                reason = str(it.get("reason") or it.get("why") or "").strip() or "N/A"
                out[tk] = (sc, reason)
            for t in chunk:
                if t not in out:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
        except Exception as exc:
            _log_warn(
                f"Emerging 내러티브 배치 Gemini 호출 실패 ({_SCANNER_GEMINI_CHUNK_MAX_RETRIES}회 재시도 후, 청크 {ci}/{n_chunk}): {exc}"
            )
            _log("error", traceback.format_exc())
            for t in chunk:
                out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
    return out

def batch_narrative_setup_scores(tickers, narrative_text):
    """
    대기주(Setups): 테마의 직접(1차) 수혜주이지만 아직 본격 상승 전 종목의 적합도를
    Gemini **청크(최대 20티커)별** 호출로 채점. 반환: ticker -> (score 0~100, reason str)
    """
    clean = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not clean:
        return {}
    nt = str(narrative_text or "").strip()
    if not nt:
        return {t: (0.0, "내러티브 텍스트가 부족합니다.") for t in clean}

    out = {}
    n_chunk = max(1, (len(clean) + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE - 1) // _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE)
    for ci, i in enumerate(range(0, len(clean), _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE), start=1):
        chunk = clean[i : i + _SCANNER_NARRATIVE_TICKER_CHUNK_SIZE]
        tickers_json = json.dumps(chunk, ensure_ascii=False)
        prompt = f"""
당신은 진입 타이밍 중심의 종목 발굴 전략가입니다.

[Market Narrative JSON]
{nt}

[Tickers to score (이번 청크)]
{tickers_json}

작업:
- 각 티커는 테마의 '직접(1차) 수혜주이지만 아직 본격 상승 전' 후보다.
- "대기주 적합도"를 0~100 정수로 채점하라.
- 채점 기준:
  1) 테마 직접성: 테마의 직접 수혜 라인인가(멀리 떨어진 후방 공급망이 아니라). 직접일수록 고점.
  2) 촉매 임박도: 곧 가격을 움직일 구체적 촉매(실적·제품·정책·수주)가 가까운가.
  3) 미상승 프리미엄: 아직 크게 안 오른 종목일수록 고점, 이미 급등한 종목은 감점.
- 이미 테마 대장으로 크게 오른 종목은 여기 대상이 아니므로 낮게 채점.
- reason은 **한국어 15단어 이내**. 반드시 **JSON 배열만** 출력, 마크다운 금지.
- 원소 형식: {{"ticker": "MRVL", "score": 78, "reason": "AI칩 직접 라인, 실적 임박, 아직 횡보"}}
- 배열 길이는 반드시 {len(chunk)}이며 모든 티커를 빠짐없이 포함할 것.

예시:
[{{"ticker":"MRVL","score":78,"reason":"AI칩 직접 라인, 실적 임박, 아직 횡보"}},{{"ticker":"ARM","score":70,"reason":"IP 직접 수혜, 베이스 형성 중"}}]
"""
        try:
            response = _scanner_narrative_batch_generate_chunk_with_retries(prompt)
            raw_text = str(getattr(response, "text", "") or "").strip()
            items = _parse_gemini_ticker_score_json_array(raw_text)
            if not items:
                _log_warn(
                    f"대기주 내러티브 배치: 청크 {ci}/{n_chunk} JSON 배열 파싱 결과가 비어 있습니다. "
                    f"({len(chunk)}티커)"
                )
                for t in chunk:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                tk = str(it.get("ticker") or it.get("Ticker") or "").strip().upper()
                if not tk:
                    continue
                sc = to_float(it.get("score"))
                if pd.isna(sc):
                    sc = 0.0
                sc = float(np.clip(sc, 0.0, 100.0))
                reason = str(it.get("reason") or it.get("why") or "").strip() or "N/A"
                out[tk] = (sc, reason)
            for t in chunk:
                if t not in out:
                    out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
        except Exception as exc:
            _log_warn(
                f"대기주 내러티브 배치 Gemini 호출 실패 ({_SCANNER_GEMINI_CHUNK_MAX_RETRIES}회 재시도 후, 청크 {ci}/{n_chunk}): {exc}"
            )
            _log("error", traceback.format_exc())
            for t in chunk:
                out[t] = (0.0, SCANNER_NARRATIVE_API_FAIL_MESSAGE)
    return out

def route_candidates_by_regime(candidate_tickers, limit: int = 252):
    """후보 풀 각 종목을 regime_core.classify_regime 으로 분기.
    반환: {
      "leaders":  [티커...],          # regime strong (Stage 2), 천장 아님
      "setups":   [티커...],          # regime sideways (Stage 1), 천장 아님
      "excluded": [(티커, 사유)...],   # weak(Stage4)/topping(Stage3)/데이터부족 → 매도 레이더
      "detail":   {티커: classify_regime dict},
    }
    """
    result = {"leaders": [], "setups": [], "excluded": [], "detail": {}}
    clean = filter_scanner_ticker_list(
        [str(t).strip().upper() for t in (candidate_tickers or []) if str(t).strip()]
    )
    if not clean:
        return result
    try:
        batch = _fmp_batch_price_history(clean + ["SPY"], limit=limit)
    except Exception:
        batch = {}
    spy_df = batch.get("SPY", pd.DataFrame())
    spy_close = (
        spy_df["Close"]
        if isinstance(spy_df, pd.DataFrame) and "Close" in spy_df.columns
        else None
    )
    for tk in clean:
        hist = batch.get(tk)
        if not isinstance(hist, pd.DataFrame) or hist.empty or "Close" not in hist.columns:
            result["excluded"].append((tk, "가격 데이터 없음"))
            continue
        try:
            reg = rc.classify_regime(hist, spy_close=spy_close)
        except Exception:
            result["excluded"].append((tk, "레짐 분류 실패"))
            continue
        result["detail"][tk] = reg
        if not reg.get("enough_data", False):
            result["excluded"].append((tk, "데이터 부족"))
            continue
        regime = str(reg.get("regime", "unknown"))
        topping = bool(reg.get("topping", False))
        if topping:
            result["excluded"].append((tk, "천장(Stage3)"))
        elif regime == "strong":
            result["leaders"].append(tk)
        elif regime == "sideways":
            result["setups"].append(tk)
        elif regime == "weak":
            result["excluded"].append((tk, "약추세(Stage4)"))
        else:
            result["excluded"].append((tk, "분류 불가"))
    return result

def score_opportunity_universe(universe_tickers, latest_analysis, regime_detail=None):
    if not universe_tickers:
        return pd.DataFrame()

    tickers = list(dict.fromkeys([str(t).strip().upper() for t in universe_tickers if str(t).strip()]))
    tickers = filter_scanner_ticker_list(tickers)
    if not tickers:
        return pd.DataFrame()

    narrative_text = json.dumps(latest_analysis or {}, ensure_ascii=False)
    regime_detail = regime_detail if isinstance(regime_detail, dict) else {}
    progress = _ProgressProxy("스캐너 준비 중...")

    with _spinner("Gemini 내러티브 배치 평가 중 (티커 청크별, gemini-2.5-flash)..."):
        narrative_map = batch_narrative_alignment_scores(tickers, narrative_text)

    with _spinner("가격/거래량 데이터 다운로드 중..."):
        try:
            batch = _fmp_batch_price_history(tickers + ["SPY"], limit=252)
            close_df = pd.DataFrame({tk: df["Close"] for tk, df in batch.items() if "Close" in df.columns}).sort_index()
            volume_df = pd.DataFrame({tk: df["Volume"] for tk, df in batch.items() if "Volume" in df.columns}).sort_index()
            spy_hist = batch.get("SPY", pd.DataFrame())
        except Exception:
            close_df = pd.DataFrame()
            volume_df = pd.DataFrame()
            spy_hist = pd.DataFrame()
    _spy_close_for_regime = spy_hist["Close"] if "Close" in spy_hist.columns else None

    # ── 병렬 웜업: _fmp_fill() HTTP 호출을 루프 전에 한꺼번에 병렬 처리 ──
    # 캐시 콜드(첫 스캔)일 때 180종목 × 순차 7회 HTTP → 병렬화로 대폭 단축
    with _spinner(f"펀더멘털 데이터 병렬 수집 중... ({len(tickers)}종목)"):
        _info_cache = _fmp_fill_parallel_warmup(tickers)

    spy_3m = calculate_period_return(spy_hist["Close"], 63) if "Close" in spy_hist.columns else np.nan
    spy_3m = to_float(spy_3m)
    if pd.isna(spy_3m):
        spy_3m = 0.0

    rows = []
    total = len(tickers)

    for idx, ticker in enumerate(tickers, start=1):
        progress.progress(idx / total, text=f"[{idx}/{total}] {ticker} 멀티팩터 계산 중...")

        close_series = close_df[ticker] if ticker in close_df.columns else pd.Series(dtype=float)
        vol_series = volume_df[ticker] if ticker in volume_df.columns else pd.Series(dtype=float)
        _close_num_l = pd.to_numeric(close_series, errors="coerce").dropna()
        last_px_l = to_float(_close_num_l.iloc[-1]) if not _close_num_l.empty else np.nan  # ✅ 현재가

        m1_ret = calculate_period_return(close_series, 21)
        m3_ret = calculate_period_return(close_series, 63)
        rs_raw = to_float(m3_ret) - to_float(spy_3m)

        # Regime Score: 라우터가 계산한 결과(regime_detail) 재사용, 없으면 인라인 분류 (fallback)
        _reg = regime_detail.get(ticker)
        if not isinstance(_reg, dict):
            try:
                _reg = rc.classify_regime(
                    pd.DataFrame({"Close": close_series}), spy_close=_spy_close_for_regime
                )
            except Exception:
                _reg = {}
        regime_score_raw = to_float(_reg.get("score")) if isinstance(_reg, dict) else np.nan
        regime_available = bool(isinstance(_reg, dict) and _reg.get("enough_data", False))

        # 병렬 웜업 딕셔너리에서 조회 (캐시 히트) — 없으면 fallback으로 직접 호출
        info = _info_cache.get(ticker) or _fmp_fill({}, ticker)

        revenue_growth = to_float(info.get("revenueGrowth") or info.get("earningsGrowth"))
        trailing_eps = to_float(info.get("trailingEps"))
        forward_pe = to_float(info.get("forwardPE"))
        long_name = str(info.get("longName") or info.get("shortName") or ticker).strip()

        vol_3d_avg = pd.to_numeric(vol_series, errors="coerce").tail(3).mean() if not vol_series.empty else np.nan
        vol_3m_avg = pd.to_numeric(vol_series, errors="coerce").tail(63).mean() if not vol_series.empty else np.nan
        vol_ratio = np.nan
        if pd.notna(vol_3d_avg) and pd.notna(vol_3m_avg) and vol_3m_avg > 0:
            vol_ratio = float(vol_3d_avg / vol_3m_avg)

        fundamentals_data_available = pd.notna(revenue_growth) or pd.notna(trailing_eps)
        # 이진(0/100) 대신 연속 점수화: revenue_growth·trailing_eps를 각각 0~100으로 정규화 후 합산
        # revenue_growth: clip(-1, 2) → 0~100 선형 맵핑 (200% 성장 = 100점)
        # trailing_eps > 0 이면 최대 30점 보너스 (펀더멘털 안정성 가점)
        _fund_raw = 0.0
        if pd.notna(revenue_growth):
            _rev_score = float(np.clip((revenue_growth + 1.0) / 3.0 * 100.0, 0.0, 100.0))
            _fund_raw = max(_fund_raw, _rev_score)
        if pd.notna(trailing_eps) and trailing_eps > 0:
            _fund_raw = min(100.0, _fund_raw + 30.0)
        elif pd.notna(trailing_eps) and trailing_eps <= 0:
            _fund_raw = max(0.0, _fund_raw - 15.0)
        fundamentals_pass = _fund_raw >= 30.0  # 리스크 판단용 임계값
        # Institutional: 거래량 급증(vol_ratio>=1.2) OR 기관 보유비율 높음 (OR 조건으로 신호 품질 향상)
        inst_ownership = to_float(info.get("institutionPercentHeld") or info.get("institutionalOwnershipPercentage"))
        inst_via_vol = pd.notna(vol_ratio) and vol_ratio >= 1.2
        inst_via_ownership = pd.notna(inst_ownership) and inst_ownership >= 0.5  # 50% 이상 기관 보유
        inst_pass = inst_via_vol or inst_via_ownership
        # Institutional Raw: 두 신호 모두 충족 시 100, 하나만 충족 시 65, 둘 다 미충족 시 0
        if inst_via_vol and inst_via_ownership:
            _inst_raw = 100.0
        elif inst_via_vol or inst_via_ownership:
            _inst_raw = 65.0
        else:
            _inst_raw = 0.0
        # forwardPE가 None/NaN이거나 0 이하(적자 등으로 PE 해석 불가)면 결측으로 간주
        valuation_data_available = pd.notna(forward_pe) and forward_pe > 0
        valuation_pass = valuation_data_available and forward_pe <= 50

        narrative_score, narrative_why = narrative_map.get(ticker, (0.0, "배치 미응답"))
        # API 실패 사유면 narrative_available=False로 표기 → 동적 분모에서 완전 제외 (불이익 방지)
        narrative_available = narrative_why not in SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS
        if not narrative_available:
            narrative_score = 0.0  # 분자에서도 0으로 보장

        risk_bits = []
        if not valuation_data_available:
            risk_bits.append("밸류에이션 데이터 결측/해석 불가")
        elif not valuation_pass:
            risk_bits.append("밸류에이션 부담")
        if not fundamentals_data_available:
            risk_bits.append("펀더멘털 데이터 결측")
        elif not fundamentals_pass:
            risk_bits.append("펀더멘털 모멘텀 약함")
        if not inst_pass:
            risk_bits.append("기관 수급 가속 신호 부족")
        risk_text = ", ".join(risk_bits) if risk_bits else "특이 리스크 신호 제한적"

        rows.append(
            {
                "Ticker": ticker,
                "Name": long_name,
                "Price": round(float(last_px_l), 4) if pd.notna(last_px_l) else np.nan,  # ✅ Watchlist saved_price용
                "Narrative Raw": narrative_score,
                "Narrative Why": narrative_why,
                "Narrative Available": bool(narrative_available),
                "Momentum 1M Raw": to_float(m1_ret),
                "RS Raw": to_float(rs_raw),
                "Regime Score Raw": float(regime_score_raw) if pd.notna(regime_score_raw) else np.nan,
                "Regime Available": bool(regime_available),
                "Fundamentals Raw": float(_fund_raw),
                "Fundamentals Available": bool(fundamentals_data_available),
                "Institutional Raw": float(_inst_raw),
                "Valuation Raw": 100.0 if valuation_pass else 0.0,
                "Valuation Available": bool(valuation_data_available),
                "Risk": risk_text,
            }
        )

    score_df = pd.DataFrame(rows)
    if score_df.empty:
        progress.empty()
        return score_df

    # 가격 시계열이 없거나 핵심 모멘텀/RS가 NaN인 종목은 랭킹에서 제외 (내러티브만으로 상위 노출 방지)
    _reset_drops("leaders")
    for _, _r in score_df[score_df[["Momentum 1M Raw", "RS Raw"]].isna().any(axis=1)].iterrows():
        _record_drop("leaders", str(_r.get("Ticker", "")),
                     "가격 이력 없음/부족 (모멘텀·RS 계산 불가)")
    score_df = score_df.dropna(subset=["Momentum 1M Raw", "RS Raw"])
    if score_df.empty:
        progress.empty()
        return score_df

    _nw = score_df["Narrative Why"].isin(SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS)
    score_df.loc[_nw, "Narrative Raw"] = 0.0

    final_s, n_s, m_s, reg_s, f_s, inst_s = leaders_final_weighted_score(score_df)
    score_df["Final Score"] = final_s
    score_df["Narrative Score"] = n_s
    score_df["Momentum Score"] = m_s
    score_df["Regime Score"] = reg_s
    score_df["Fundamentals Score"] = f_s
    score_df["Institutional Score"] = inst_s
    score_df = _scanner_score_df_format_for_display(score_df, "leaders")
    score_df = score_df.sort_values(
        ["Final Score", "Ticker"], ascending=[False, True], na_position="last"
    ).reset_index(drop=True)

    progress.progress(1.0, text="AI Opportunity Scanner 계산 완료")
    return score_df

def score_emerging_opportunity_universe(universe_tickers, latest_analysis):
    """
    Emerging Opportunities 스코어링 (총 100점, 결측 시 가중치 재분배).
    가중치: Narrative Expansion 35%, Early RS 20%, Vol Accel 20%, Fund Readiness 15%, Overextension 10%.
    """
    if not universe_tickers:
        return pd.DataFrame()

    tickers = list(dict.fromkeys([str(t).strip().upper() for t in universe_tickers if str(t).strip()]))
    tickers = filter_scanner_ticker_list(tickers)
    if not tickers:
        return pd.DataFrame()

    narrative_text = json.dumps(latest_analysis or {}, ensure_ascii=False)
    progress = _ProgressProxy("Emerging 스캐너 준비 중...")

    with _spinner("Emerging: 가격·거래량 데이터 다운로드 중..."):
        try:
            batch_em = _fmp_batch_price_history(tickers, limit=130)
            close_df = pd.DataFrame({tk: df["Close"] for tk, df in batch_em.items() if "Close" in df.columns}).sort_index()
            volume_df = pd.DataFrame({tk: df["Volume"] for tk, df in batch_em.items() if "Volume" in df.columns}).sort_index()
        except Exception:
            close_df = pd.DataFrame()
            volume_df = pd.DataFrame()

    with _spinner("대기주 Gemini 내러티브 배치 평가 중 (티커 청크별, gemini-2.5-flash)..."):
        narrative_map_em = batch_narrative_setup_scores(tickers, narrative_text)

    # ── 병렬 웜업: _fmp_fill() HTTP 호출을 루프 전에 한꺼번에 병렬 처리 ──
    with _spinner(f"Emerging: 펀더멘털 데이터 병렬 수집 중... ({len(tickers)}종목)"):
        _em_info_cache = _fmp_fill_parallel_warmup(tickers)

    rows = []
    total = len(tickers)

    for idx, ticker in enumerate(tickers, start=1):
        progress.progress(idx / total, text=f"[Emerging {idx}/{total}] {ticker} 계산 중...")

        close_series = close_df[ticker] if ticker in close_df.columns else pd.Series(dtype=float)
        vol_series = volume_df[ticker] if ticker in volume_df.columns else pd.Series(dtype=float)
        close_num = pd.to_numeric(close_series, errors="coerce").dropna()

        m1_ret = calculate_period_return(close_series, 21)
        m1 = to_float(m1_ret)
        early_available = pd.notna(m1)
        if not early_available:
            early_raw = np.nan
        elif m1 < 0:
            early_raw = 0.0
        elif m1 <= 15.0:
            early_raw = 100.0
        else:
            early_raw = max(0.0, 100.0 - (m1 - 15.0) * 6.0)

        vol_num = pd.to_numeric(vol_series, errors="coerce")
        v5 = float(vol_num.tail(5).mean()) if not vol_num.empty else np.nan
        v30 = float(vol_num.tail(30).mean()) if not vol_num.empty else np.nan
        vol_ratio = np.nan
        vol_available = False
        if pd.notna(v5) and pd.notna(v30) and v30 > 0:
            vol_ratio = v5 / v30
            vol_available = True
        if vol_available:
            if vol_ratio >= 1.5:
                vol_raw = 100.0
            elif vol_ratio >= 1.2:
                vol_raw = 72.0
            elif vol_ratio >= 1.0:
                vol_raw = 45.0
            else:
                vol_raw = 15.0
        else:
            vol_raw = np.nan

        rsi_last = np.nan
        if len(close_num) >= 15:
            rsi_series = calculate_rsi(close_series, 14)
            rsi_last = to_float(rsi_series.iloc[-1]) if rsi_series is not None and not rsi_series.empty else np.nan

        ma50 = np.nan
        if len(close_num) >= 50:
            ma50 = to_float(close_num.rolling(window=50, min_periods=50).mean().iloc[-1])
        last_px = to_float(close_num.iloc[-1]) if not close_num.empty else np.nan

        stretch_pct = np.nan
        if pd.notna(last_px) and pd.notna(ma50) and ma50 > 0:
            stretch_pct = (last_px / ma50 - 1.0) * 100.0

        overext_available = pd.notna(rsi_last) and pd.notna(ma50) and pd.notna(last_px) and ma50 > 0
        if overext_available:
            over_raw = 100.0
            if rsi_last > 70.0:
                over_raw -= min(55.0, (rsi_last - 70.0) * 2.2)
            if pd.notna(stretch_pct) and stretch_pct > 12.0:
                over_raw -= min(40.0, (stretch_pct - 12.0) * 1.8)
            over_raw = float(np.clip(over_raw, 0.0, 100.0))
        else:
            over_raw = np.nan

        # Base Maturity: 50MA 근접(-8%~+3%) + RSI 회복 구간(45~58) → 베이스 성숙=곧 돌파 신호
        base_available = pd.notna(rsi_last) and pd.notna(stretch_pct)
        if base_available:
            if -8.0 <= stretch_pct <= 3.0:
                prox = 100.0
            elif stretch_pct < -8.0:
                prox = max(0.0, 100.0 - (abs(stretch_pct) - 8.0) * 5.0)
            else:  # 50MA 위로 이미 이격
                prox = max(0.0, 100.0 - (stretch_pct - 3.0) * 6.0)
            if 45.0 <= rsi_last <= 58.0:
                rsi_band = 100.0
            elif rsi_last < 45.0:
                rsi_band = max(0.0, 100.0 - (45.0 - rsi_last) * 5.0)
            else:  # 58 초과 = 회복 구간 상단 이탈(과열 방향)
                rsi_band = max(0.0, 100.0 - (rsi_last - 58.0) * 5.0)
            base_raw = round((prox + rsi_band) / 2.0, 2)
        else:
            base_raw = np.nan

        # 병렬 웜업 딕셔너리에서 조회 — 없으면 fallback으로 직접 호출
        info = _em_info_cache.get(ticker) or _fmp_fill({}, ticker)

        eg = to_float(info.get("earningsGrowth"))
        if pd.isna(eg):
            eg = to_float(info.get("epsGrowth"))
        if pd.isna(eg):
            eg = to_float(info.get("earningsQuarterlyGrowth"))
        rev_g = to_float(info.get("revenueGrowth"))
        trail_eps = to_float(info.get("trailingEps"))

        fund_data_available = any(pd.notna(x) for x in (eg, rev_g, trail_eps))
        fund_raw = np.nan
        if fund_data_available:
            if pd.notna(eg) and eg > 0:
                fund_raw = 100.0
            elif pd.notna(rev_g) and rev_g > 0 and pd.notna(trail_eps) and trail_eps > 0:
                fund_raw = 85.0
            elif pd.notna(trail_eps) and trail_eps > 0:
                fund_raw = 55.0
            else:
                fund_raw = 20.0

        narrative_score, narrative_why = narrative_map_em.get(ticker, (0.0, "배치 미응답"))
        # API 실패 사유면 narrative_available=False → 동적 분모에서 완전 제외 (불이익 방지)
        narrative_available = narrative_why not in SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS
        if not narrative_available:
            narrative_score = 0.0

        risk_bits = []
        if pd.notna(rsi_last) and rsi_last > 70:
            risk_bits.append(f"RSI 과열({rsi_last:.1f})")
        if pd.notna(stretch_pct) and stretch_pct > 12:
            risk_bits.append(f"50일선 대비 과도 이격({stretch_pct:.1f}%)")
        if pd.notna(m1) and m1 > 15:
            risk_bits.append("1M 급등 구간(후발주 관점 부담)")
        risk_text = ", ".join(risk_bits) if risk_bits else "과열·이격 리스크 상대적으로 제한적"

        long_name = str(info.get("longName") or info.get("shortName") or ticker).strip()

        rows.append(
            {
                "Ticker": ticker,
                "Name": long_name,
                "Price": round(float(last_px), 4) if pd.notna(last_px) else np.nan,  # ✅ Watchlist saved_price용
                "Narrative Raw": narrative_score,
                "Narrative Why": narrative_why,
                "Narrative Available": bool(narrative_available),
                "Early RS Raw": early_raw,
                "Early RS Available": bool(early_available),
                "Vol Accel Raw": vol_raw,
                "Vol Accel Available": bool(vol_available),
                "Vol5/30x": vol_ratio,
                "Fundamentals Raw": fund_raw,
                "Fundamentals Available": bool(fund_data_available),
                "Overextension Raw": over_raw,
                "Overextension Available": bool(overext_available),
                "Base Maturity Raw": base_raw,
                "Base Maturity Available": bool(base_available),
                "RSI(14)": rsi_last,
                "Stretch vs MA50(%)": stretch_pct,
                "1M Return(%)": m1,
                "Risk": risk_text,
            }
        )

    score_df = pd.DataFrame(rows)
    if score_df.empty:
        progress.empty()
        return score_df

    # 기술적 팩터(Early RS·거래량 가속·RSI) 중 하나라도 계산 불가면 최종 랭크에서 제외
    _reset_drops("emerging")
    _miss = score_df[score_df[["Early RS Raw", "Vol Accel Raw", "RSI(14)"]].isna().any(axis=1)]
    for _, _r in _miss.iterrows():
        _record_drop("emerging", str(_r.get("Ticker", "")),
                     "가격 이력 없음/부족 (Early RS·거래량·RSI 계산 불가)")
    score_df = score_df.dropna(subset=["Early RS Raw", "Vol Accel Raw", "RSI(14)"])
    if score_df.empty:
        progress.empty()
        return score_df

    _nw_em = score_df["Narrative Why"].isin(SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS)
    score_df.loc[_nw_em, "Narrative Raw"] = 0.0

    final_s, n_s, e_s, v_s, f_s, o_s, b_s = emerging_final_weighted_score(score_df)
    score_df["Final Score"] = final_s
    score_df["Narrative Score"] = n_s
    score_df["Early RS Score"] = e_s
    score_df["Vol Accel Score"] = v_s
    score_df["Fundamentals Score"] = f_s
    score_df["Overextension Score"] = o_s
    score_df["Base Maturity Score"] = b_s
    score_df = _scanner_score_df_format_for_display(score_df, "emerging")
    score_df = score_df.sort_values(["Final Score", "Ticker"], ascending=[False, True], na_position="last").reset_index(
        drop=True
    )

    progress.progress(1.0, text="Emerging 스캐너 계산 완료")
    return score_df

def score_expansion_opportunity_universe(universe_tickers, latest_analysis):
    """
    확산주(Next Wave) 엔진 — expanding_to(2·3차 후발주) 풀 대상.
    가격 신호가 없어 모멘텀/RS 제외. Structural(공급망 연결강도) 0.40 + Accumulation(초기 축적) 0.25
    + Fundamentals 0.20 + Valuation(재평가 여지) 0.15. 절대 앵커링.
    """
    _drop_detail = {}
    _expansion_ctx = {}
    try:
        import narrative_core as _nc
        _expansion_ctx = _nc.expansion_ticker_context(latest_analysis)
    except Exception:
        _expansion_ctx = {}
    if not universe_tickers:
        return pd.DataFrame()

    tickers = list(dict.fromkeys([str(t).strip().upper() for t in universe_tickers if str(t).strip()]))
    tickers = filter_scanner_ticker_list(tickers)
    if not tickers:
        return pd.DataFrame()

    narrative_text = json.dumps(latest_analysis or {}, ensure_ascii=False)
    progress = _ProgressProxy("확산주 스캐너 준비 중...")

    with _spinner("확산주: 가격·거래량 데이터 다운로드 중..."):
        try:
            batch_x = _fmp_batch_price_history(tickers, limit=130)
            close_df = pd.DataFrame({tk: df["Close"] for tk, df in batch_x.items() if "Close" in df.columns}).sort_index()
            volume_df = pd.DataFrame({tk: df["Volume"] for tk, df in batch_x.items() if "Volume" in df.columns}).sort_index()
        except Exception:
            close_df = pd.DataFrame()
            volume_df = pd.DataFrame()

    # 구조 연결 강도: second-order/공급망 프롬프트 재사용 (step4에서 batch_narrative_expansion_scores로 이전 예정)
    with _spinner("확산주 Gemini 구조 연결 강도 평가 중 (gemini-2.5-flash)..."):
        narrative_map_x = batch_narrative_expansion_scores(tickers, narrative_text)

    with _spinner(f"확산주: 펀더멘털 데이터 병렬 수집 중... ({len(tickers)}종목)"):
        _x_info_cache = _fmp_fill_parallel_warmup(tickers)

    rows = []
    total = len(tickers)
    for idx, ticker in enumerate(tickers, start=1):
        progress.progress(idx / total, text=f"[확산주 {idx}/{total}] {ticker} 계산 중...")

        close_series = close_df[ticker] if ticker in close_df.columns else pd.Series(dtype=float)
        vol_series = volume_df[ticker] if ticker in volume_df.columns else pd.Series(dtype=float)
        close_num = pd.to_numeric(close_series, errors="coerce").dropna()
        last_px = to_float(close_num.iloc[-1]) if not close_num.empty else np.nan

        # 거래량 비율 (저밴드도 가점)
        vol_num = pd.to_numeric(vol_series, errors="coerce")
        v5 = float(vol_num.tail(5).mean()) if not vol_num.empty else np.nan
        v30 = float(vol_num.tail(30).mean()) if not vol_num.empty else np.nan
        vol_ratio = (v5 / v30) if (pd.notna(v5) and pd.notna(v30) and v30 > 0) else np.nan

        # 초기 축적: 조용한 베이스에서 거래량이 막 붙기 시작(저밴드 가점) + 저변동성 베이스
        accum_available = pd.notna(vol_ratio) and len(close_num) >= 20
        if not accum_available:
            _drop_detail[ticker] = _drop_reason_from_price(close_series, close_num, 20)
        if accum_available:
            if vol_ratio >= 1.3:
                vol_acc = 100.0
            elif vol_ratio >= 1.1:
                vol_acc = 70.0
            elif vol_ratio >= 1.0:
                vol_acc = 50.0
            elif vol_ratio >= 0.9:
                vol_acc = 30.0
            else:
                vol_acc = 15.0
            recent = close_num.tail(20)
            _mean = float(recent.mean())
            cv = float(recent.std() / _mean) if _mean > 0 else np.nan
            if pd.notna(cv):
                base_tight = float(np.interp(cv, [0.0, 0.02, 0.10, 1.0], [100.0, 100.0, 20.0, 0.0]))
            else:
                base_tight = 50.0
            accum_raw = round((vol_acc + base_tight) / 2.0, 2)
        else:
            accum_raw = np.nan

        info = _x_info_cache.get(ticker) or _fmp_fill({}, ticker)

        # 펀더멘털 (대기주와 동일 기준)
        eg = to_float(info.get("earningsGrowth"))
        if pd.isna(eg):
            eg = to_float(info.get("epsGrowth"))
        if pd.isna(eg):
            eg = to_float(info.get("earningsQuarterlyGrowth"))
        rev_g = to_float(info.get("revenueGrowth"))
        trail_eps = to_float(info.get("trailingEps"))
        fund_data_available = any(pd.notna(x) for x in (eg, rev_g, trail_eps))
        fund_raw = np.nan
        if fund_data_available:
            if pd.notna(eg) and eg > 0:
                fund_raw = 100.0
            elif pd.notna(rev_g) and rev_g > 0 and pd.notna(trail_eps) and trail_eps > 0:
                fund_raw = 85.0
            elif pd.notna(trail_eps) and trail_eps > 0:
                fund_raw = 55.0
            else:
                fund_raw = 20.0

        # 밸류에이션 여유 (forward PE → anchor_valuation_pe)
        pe_val = to_float(info.get("forwardPE"))
        if pd.isna(pe_val):
            pe_val = to_float(info.get("forwardPe"))
        if pd.isna(pe_val):
            pe_val = to_float(info.get("trailingPE"))
        if pd.isna(pe_val):
            pe_val = to_float(info.get("pe"))
        valuation_available = pd.notna(pe_val) and pe_val > 0

        structural_score, structural_why = narrative_map_x.get(ticker, (0.0, "배치 미응답"))
        structural_available = structural_why not in SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS
        if not structural_available:
            structural_score = 0.0

        long_name = str(info.get("longName") or info.get("shortName") or ticker).strip()

        _ctx = _expansion_ctx.get(ticker, {})
        rows.append(
            {
                "Ticker": ticker,
                "Name": long_name,
                # 어느 테마의 몇 차 확산인지 — 인과 고리 타당성을 눈으로 검증하기 위함
                "Theme": str(_ctx.get("theme", "") or ""),
                "Stage": str(_ctx.get("stage", "") or ""),
                "Price": round(float(last_px), 4) if pd.notna(last_px) else np.nan,
                "Structural Raw": structural_score,
                "Structural Available": bool(structural_available),
                "Narrative Why": structural_why,
                "Accumulation Raw": accum_raw,
                "Accumulation Available": bool(accum_available),
                "Vol5/30x": vol_ratio,
                "Fundamentals Raw": fund_raw,
                "Fundamentals Available": bool(fund_data_available),
                "Valuation PE Raw": float(pe_val) if pd.notna(pe_val) else np.nan,
                "Valuation Available": bool(valuation_available),
                "Risk": "감시 티어 — 진입은 정량 신호(거래량 베이스·돌파) 확인 후",
            }
        )

    score_df = pd.DataFrame(rows)
    if score_df.empty:
        progress.empty()
        return score_df

    # 가격 데이터 자체가 없는 종목(초기 축적 계산 불가)은 제외
    _reset_drops("expansion")
    for _, _r in score_df[score_df["Accumulation Raw"].isna()].iterrows():
        _tk = str(_r.get("Ticker", ""))
        _record_drop("expansion", _tk, _drop_detail.get(_tk, "가격/거래량 데이터 없음"))
    score_df = score_df.dropna(subset=["Accumulation Raw"])
    if score_df.empty:
        progress.empty()
        return score_df

    _nw_x = score_df["Narrative Why"].isin(SCANNER_NARRATIVE_EXCLUDE_WEIGHT_REASONS)
    score_df.loc[_nw_x, "Structural Raw"] = 0.0

    final_s, s_s, a_s, f_s, v_s = expansion_final_weighted_score(score_df)
    score_df["Final Score"] = final_s
    score_df["Structural Score"] = s_s
    score_df["Accumulation Score"] = a_s
    score_df["Fundamentals Score"] = f_s
    score_df["Valuation Score"] = v_s
    score_df = score_df.sort_values(["Final Score", "Ticker"], ascending=[False, True], na_position="last").reset_index(
        drop=True
    )

    progress.progress(1.0, text="확산주 스캐너 계산 완료")
    return score_df

# ══════════════════════════════════════════════════════════════════════════════
# 5. 워치리스트 자동 편입 판정 (SSOT)
#    앱 배지 · 이메일 표시 · 자동 추가가 **전부 이 함수 하나**를 본다.
# ══════════════════════════════════════════════════════════════════════════════
def pick_watchlist_candidates(score_df, engine: str,
                              threshold: float = None,
                              snap: dict = None) -> list:
    """Final Score 가 기준선 이상인 종목을 점수 내림차순으로 반환.

    Args:
        snap: 해당 버킷 스냅샷. degraded=True 면 **빈 리스트**를 반환한다.
              데이터 수집이 깨진 상태에서 나온 고득점은 신뢰할 수 없기 때문.

    Returns:
        [{"ticker","score","engine","engine_label","name","price","why","risk"}, ...]
        상한 없음(설계 확정) — 70점을 넘긴 종목은 전부 편입 대상이다.
    """
    if threshold is None:
        threshold = watchlist_threshold(engine)
    if isinstance(snap, dict) and snap.get("degraded"):
        _log("warn", f"{ENGINE_LABELS.get(engine, engine)}: 커버리지 부족으로 "
                     f"워치리스트 편입을 건너뜁니다.")
        return []
    if not isinstance(score_df, pd.DataFrame) or score_df.empty:
        return []
    if "Final Score" not in score_df.columns or "Ticker" not in score_df.columns:
        return []

    df = _scanner_score_df_format_for_display(score_df.copy(), engine)
    fs = pd.to_numeric(df["Final Score"], errors="coerce")
    df = df.loc[fs.notna() & (fs >= float(threshold))].copy()
    if df.empty:
        return []
    df["_fs"] = pd.to_numeric(df["Final Score"], errors="coerce")
    df = df.sort_values("_fs", ascending=False)

    label = ENGINE_LABELS.get(engine, engine)
    out = []
    for _, row in df.iterrows():
        tk = str(row.get("Ticker", "")).strip().upper()
        if not tk or not is_valid_scanner_ticker(tk):
            continue
        price = pd.to_numeric(row.get("Price", row.get("Close", np.nan)), errors="coerce")
        why = str(row.get("Narrative Why", row.get("Structural Why", "")) or "").strip()
        out.append({
            "ticker": tk,
            "score": round(float(row["_fs"]), 2),
            "engine": engine,
            "engine_label": label,
            "name": str(row.get("Name", "") or "").strip(),
            "price": float(price) if pd.notna(price) else None,
            "why": why,
            "risk": str(row.get("Risk", "") or "").strip(),
        })
    return out


def build_auto_memo(engine: str, score: float, run_date: str) -> str:
    """자동 추가분 식별용 Memo. 나중에 자동 편입분만 골라내거나 정리할 수 있게 한다.

    ⚠️ 점수는 반드시 소수 2자리 — 이메일·앱 표시(`Final Score` 2자리)와 같은 값이어야
       워치리스트 메모와 스캐너 화면의 숫자가 어긋나지 않는다.
    """
    return f"AUTO|{ENGINE_LABELS.get(engine, engine)}|{float(score):.2f}|{run_date}"


def is_auto_memo(memo: str) -> bool:
    return str(memo or "").strip().upper().startswith("AUTO|")


# ══════════════════════════════════════════════════════════════════════════════
# 6. 3버킷 일괄 스캔 (앱 버튼 · 자동화가 **동일하게** 호출하는 단일 진입점)
# ══════════════════════════════════════════════════════════════════════════════
def run_three_bucket_scan(winners_pool, expansion_pool, latest_analysis,
                          run_by: str = "manual") -> dict:
    """주간 레코드 하나에서 3버킷을 모두 스캔한다.

    Args:
        winners_pool:   주도주·대기주 라우팅 후보 (주간 themes[].winners 합집합)
        expansion_pool: 확산주 유니버스 (주간 themes[].expanding_to 합집합)
        latest_analysis: 채점 근거로 넘길 분석 dict. **7일 병합 themes(driver/stage/linkage)**
                         가 들어 있어야 확산주 Structural(40%) 채점이 제대로 된다.
        run_by: "auto" | "manual" — 스냅샷 메타에 기록되어 앱 배너에 표시된다.

    Returns:
        {"leaders": snap|None, "emerging": snap|None, "expansion": snap|None,
         "routed": {...}, "run_by":..., "completed_at":...}
        각 snap 은 app.py `st.session_state["scanner_results*"]` 와 동일한 형식이다.
    """
    completed_at = datetime.now(timezone.utc).isoformat()
    winners_pool = filter_scanner_ticker_list(
        [str(t).strip().upper() for t in (winners_pool or []) if str(t).strip()]
    )
    expansion_pool = filter_scanner_ticker_list(
        [str(t).strip().upper() for t in (expansion_pool or []) if str(t).strip()]
    )
    result = {
        "leaders": None, "emerging": None, "expansion": None,
        "routed": None, "run_by": run_by, "completed_at": completed_at,
        # 후보 풀은 있었는데 결과가 비어버린 버킷 — "후보 없음"과 구분해야 한다.
        # (전량 데이터 실패가 정상처럼 보이는 조용한 실패를 막는다)
        "failed": [],
    }

    # ── 정량 라우팅: 후보 풀을 regime 으로 분기 ────────────────────────────
    _log("info", f"[라우팅] 후보 {len(winners_pool)}종목 regime 분기 중...")
    routed = route_candidates_by_regime(winners_pool)
    result["routed"] = {
        "leaders": routed["leaders"],
        "setups": routed["setups"],
        "excluded": routed["excluded"],
    }
    _log("info", f"[라우팅] 주도주 {len(routed['leaders'])} · 대기주 {len(routed['setups'])} "
                 f"· 제외 {len(routed['excluded'])}")
    if winners_pool and not routed["leaders"] and not routed["setups"]:
        result["failed"].append("routing")
        _log("error", f"라우팅: 후보 {len(winners_pool)}종목이 전부 제외됐습니다. "
                      f"가격 데이터 수집 실패 가능성이 높습니다.")

    def _snap(df, mode_note, universe):
        uni = list(universe)
        scored = int(len(df)) if isinstance(df, pd.DataFrame) else 0
        coverage = (scored / len(uni)) if uni else 0.0
        degraded = bool(uni) and coverage < SCAN_MIN_COVERAGE
        if degraded:
            _log("error",
                 f"{mode_note}: 커버리지 {coverage*100:.0f}% ({scored}/{len(uni)}) — "
                 f"데이터 수집 실패로 판단. 워치리스트 자동 편입에서 제외합니다.")
        return {
            "score_df": df.copy(),
            "mode_note": mode_note,
            "scanner_mode": "주간 3버킷 일괄 스캔",
            "scanner_data_source": "주간 메가 트렌드",
            "universe": uni,
            "scored_count": scored,
            "coverage": round(coverage, 4),
            "degraded": degraded,
            "completed_at": completed_at,
            "run_by": run_by,
        }

    # ── 주도주 ─────────────────────────────────────────────────────────────
    if routed["leaders"]:
        try:
            df_l = score_opportunity_universe(
                routed["leaders"], latest_analysis, regime_detail=routed["detail"]
            )
            if isinstance(df_l, pd.DataFrame) and not df_l.empty:
                df_l = df_l.copy()
                df_l["Narrative Why"] = df_l.get("Narrative Why", "")
                result["leaders"] = _snap(df_l, "주도주 · 주간 메가 트렌드", routed["leaders"])
            else:
                result["failed"].append("leaders")
                _log("error", f"주도주: 후보 {len(routed['leaders'])}종목인데 채점 결과가 "
                              f"비었습니다. 데이터 수집 실패로 판단합니다.")
        except Exception as exc:
            result["failed"].append("leaders")
            _log("error", f"주도주 스캔 실패: {exc}\n{traceback.format_exc()}")
    else:
        _log("warn", "주도주(강세 추세) 후보가 없습니다.")

    # ── 대기주 ─────────────────────────────────────────────────────────────
    if routed["setups"]:
        try:
            df_e = score_emerging_opportunity_universe(routed["setups"], latest_analysis)
            if isinstance(df_e, pd.DataFrame) and not df_e.empty:
                result["emerging"] = _snap(df_e, "대기주 · 주간 메가 트렌드", routed["setups"])
            else:
                result["failed"].append("emerging")
                _log("error", f"대기주: 후보 {len(routed['setups'])}종목인데 채점 결과가 "
                              f"비었습니다. 데이터 수집 실패로 판단합니다.")
        except Exception as exc:
            result["failed"].append("emerging")
            _log("error", f"대기주 스캔 실패: {exc}\n{traceback.format_exc()}")
    else:
        _log("warn", "대기주(베이스 단계) 후보가 없습니다.")

    # ── 확산주 ─────────────────────────────────────────────────────────────
    if expansion_pool:
        try:
            df_x = score_expansion_opportunity_universe(expansion_pool, latest_analysis)
            if isinstance(df_x, pd.DataFrame) and not df_x.empty:
                result["expansion"] = _snap(df_x, "확산주 · 주간 expanding_to", expansion_pool)
            else:
                result["failed"].append("expansion")
                _log("error", f"확산주: 후보 {len(expansion_pool)}종목인데 채점 결과가 "
                              f"비었습니다. 데이터 수집 실패로 판단합니다.")
        except Exception as exc:
            result["failed"].append("expansion")
            _log("error", f"확산주 스캔 실패: {exc}\n{traceback.format_exc()}")
    else:
        _log("warn", "확산주(expanding_to) 후보가 없습니다.")

    try:
        _log("info", fx.fmp_stats_line())
    except Exception:
        pass
    return result
