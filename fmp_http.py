"""fmp_http.py — FMP HTTP 계층 SSOT (streamlit 무의존)

목적
────
FMP 호출 경로가 그동안 셋으로 갈라져 있었다:

  1) fmp_extras.fmp_get()      — 레이트리밋 O, 재시도 O   (일부 함수만 사용)
  2) fmp_extras._get_json()    — 레이트리밋 X, 재시도 X   (대부분의 함수가 사용)
  3) earnings_core._get()      — 레이트리밋 X, 재시도 X

2)·3) 은 429 를 만나면 재시도 없이 None 을 돌려주고, 호출부는 그것을
"데이터 없음"으로 처리한다 → **조용히 틀린 값**이 나온다. 종목 수가 적어
지금까지 안 터졌을 뿐이다. 실적 레이더 Tier 2(유니버스 ~140종목)를 붙이면
반드시 터진다.

이 모듈은 1) 의 구현을 그대로 옮겨와 **단일 카운터**로 만든다. 세 경로가
모두 여기를 거치므로 한 프로세스의 분당 호출량이 실제로 한도 안에 들어온다.
(카운터를 모듈별로 두면 합산이 한도의 2~3배가 된다 — 이 모듈이 존재하는 이유)

streamlit 무의존 이유
─────────────────────
earnings_core.py 는 "streamlit 을 import 하지 않는다 → app.py 와 automation 이
동일 코드를 공유한다"를 파일 상단에 명시한 설계 불변식으로 갖고 있다.
그 모듈이 이 모듈을 import 해야 하므로 여기에도 streamlit 이 있으면 안 된다.

API 키는 import 하지 않고 **주입**받는다(scanner_core.set_fmp_key_provider 와
동일한 패턴). 기본 제공자는 환경변수 FMP_API_KEY 이고, app.py 는 st.secrets
우선 제공자를 설치한다.

사용
────
    import fmp_http as fh

    data = fh.fmp_get_json("profile?symbol=AAPL")      # dict | list | None
    r    = fh.fmp_get("https://.../custom?apikey=...")  # Response | None
    print(fh.fmp_stats_line())
"""

from __future__ import annotations

import os as _os
import random as _random
import threading as _threading
import time as _time
from collections import deque as _deque
from typing import Any, Callable, Optional

import requests

SSOT_VERSION = "2026-08-13a"

# ══════════════════════════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════════════════════════
FMP_BASE = "https://financialmodelingprep.com/stable"
FMP_TIMEOUT = 7

# 한도의 약 83% 로 보수적 설정. 워크플로에서 환경변수로 조절 가능.
FMP_RATE_LIMIT_PER_MIN = max(30, int(_os.environ.get("FMP_RATE_LIMIT_PER_MIN", "200") or 200))

# 429/402 를 만났을 때 재시도 횟수. 재시도가 없으면 한 번 밀린 호출이 그대로
# 유실되어 해당 티커가 dropna 로 탈락한다(대기주 15종목 중 13종목 유실 사고의 원인).
FMP_MAX_RETRIES = max(0, int(_os.environ.get("FMP_MAX_RETRIES", "3") or 3))

_FMP_WINDOW_SEC = 60.0

_fmp_rate_lock = _threading.Lock()
_fmp_call_times: _deque = _deque()
_fmp_stats = {"ok": 0, "rate_limited": 0, "http_error": 0, "exception": 0,
              "throttle_waits": 0, "throttle_sec": 0.0,
              "retries": 0, "recovered": 0, "gave_up": 0}


# ══════════════════════════════════════════════════════════════════════════
# API 키 — 주입식 (streamlit 의존 회피)
# ══════════════════════════════════════════════════════════════════════════
_key_provider: Optional[Callable[[], str]] = None


def set_key_provider(fn: Optional[Callable[[], str]]) -> None:
    """API 키 제공자 설치. app.py 는 st.secrets 우선 제공자를 넣는다.

    fn 이 None 이면 기본(환경변수)으로 되돌린다.
    """
    global _key_provider
    _key_provider = fn


def fmp_key() -> str:
    """현재 API 키. 제공자가 없거나 실패하면 환경변수 FMP_API_KEY 로 폴백."""
    if _key_provider is not None:
        try:
            k = str(_key_provider() or "").strip()
            if k:
                return k
        except Exception:
            pass
    return str(_os.environ.get("FMP_API_KEY", "") or "").strip()


# ══════════════════════════════════════════════════════════════════════════
# 레이트 리밋 — 슬라이딩 윈도우
# ══════════════════════════════════════════════════════════════════════════
def fmp_rate_limit_acquire() -> float:
    """슬라이딩 윈도우 토큰 확보. 한도 초과 시 여유가 생길 때까지 대기.

    Returns: 실제 대기한 초(0.0 이면 즉시 통과).
    """
    waited = 0.0
    while True:
        with _fmp_rate_lock:
            now = _time.time()
            while _fmp_call_times and (now - _fmp_call_times[0]) >= _FMP_WINDOW_SEC:
                _fmp_call_times.popleft()
            if len(_fmp_call_times) < FMP_RATE_LIMIT_PER_MIN:
                _fmp_call_times.append(now)
                if waited > 0:
                    _fmp_stats["throttle_waits"] += 1
                    _fmp_stats["throttle_sec"] += waited
                return waited
            sleep_for = _FMP_WINDOW_SEC - (now - _fmp_call_times[0]) + 0.01
        sleep_for = min(max(sleep_for, 0.01), 5.0)
        _time.sleep(sleep_for)
        waited += sleep_for


# ══════════════════════════════════════════════════════════════════════════
# GET
# ══════════════════════════════════════════════════════════════════════════
def fmp_get(url: str, timeout: float = None, retries: int = None):
    """레이트 리밋 + 429 백오프 재시도를 적용한 GET.

    429(레이트리밋)·402(쿼터)·5xx 는 지수 백오프로 재시도한다. 4xx(잘못된 심볼 등)는
    재시도해도 소용없으므로 즉시 포기한다.

    Returns: requests.Response | None
    """
    n = FMP_MAX_RETRIES if retries is None else max(0, int(retries))
    last_kind = None
    for attempt in range(n + 1):
        fmp_rate_limit_acquire()
        try:
            r = requests.get(url, timeout=(timeout or FMP_TIMEOUT))
        except Exception:
            last_kind = "exception"
            r = None
        else:
            if r.status_code == 200:
                with _fmp_rate_lock:
                    _fmp_stats["ok"] += 1
                    if attempt > 0:
                        _fmp_stats["recovered"] += 1
                return r
            if r.status_code in (429, 402):
                last_kind = "rate_limited"
            elif r.status_code >= 500:
                last_kind = "http_error"
            else:
                with _fmp_rate_lock:
                    _fmp_stats["http_error"] += 1
                return None  # 4xx 는 재시도 무의미

        if attempt < n:
            with _fmp_rate_lock:
                _fmp_stats["retries"] += 1
            # 지수 백오프 + 지터 (스레드가 동시에 몰려 재차 429 나는 것 방지)
            _time.sleep(min(2.0 * (2 ** attempt), 12.0) + _random.uniform(0, 1.5))

    with _fmp_rate_lock:
        _fmp_stats[last_kind or "exception"] += 1
        _fmp_stats["gave_up"] += 1
    return None


def fmp_url(path: str, key: str = "") -> str:
    """'profile?symbol=AAPL' → 전체 URL(apikey 부착).

    path 는 /stable/ 이후 부분. 앞의 '/' 는 있어도 없어도 된다.
    """
    p = str(path or "").lstrip("/")
    k = key or fmp_key()
    sep = "&" if "?" in p else "?"
    return f"{FMP_BASE}/{p}{sep}apikey={k}"


def fmp_get_json(path: str, timeout: float = None, retries: int = None,
                 key: str = "") -> Any:
    """공통 GET → JSON. 실패 시 None. (path 예: 'profile?symbol=AAPL')

    fmp_extras._get_json / earnings_core._get 의 대체품. 동작 계약은 동일하되
    레이트리밋과 429 재시도가 적용된다.

    key: 명시 키 우선. 비우면 제공자 → 환경변수 순으로 폴백.
      (app.py 는 st.secrets 키를 earnings_core._get 에 직접 넘긴다 — 이 경로가
       살아 있어야 앱에서 실적 조회가 동작한다.)
    """
    k = str(key or "").strip() or fmp_key()
    if not k:
        return None
    r = fmp_get(fmp_url(path, k), timeout=timeout, retries=retries)
    if r is None:
        return None
    try:
        return r.json()
    except Exception:
        with _fmp_rate_lock:
            _fmp_stats["exception"] += 1
        return None


# ══════════════════════════════════════════════════════════════════════════
# 통계
# ══════════════════════════════════════════════════════════════════════════
def fmp_stats() -> dict:
    with _fmp_rate_lock:
        return dict(_fmp_stats)


def fmp_reset_stats() -> None:
    with _fmp_rate_lock:
        for k in _fmp_stats:
            _fmp_stats[k] = 0 if k != "throttle_sec" else 0.0


def fmp_stats_line() -> str:
    s = fmp_stats()
    total = s["ok"] + s["rate_limited"] + s["http_error"] + s["exception"]
    return (f"FMP {total}콜 — 성공 {s['ok']}(재시도 회복 {s['recovered']}) · "
            f"레이트리밋 {s['rate_limited']} · HTTP오류 {s['http_error']} · "
            f"예외 {s['exception']} · 최종포기 {s['gave_up']} · "
            f"재시도 {s['retries']}회 · 스로틀 {s['throttle_sec']:.0f}초 "
            f"(한도 {FMP_RATE_LIMIT_PER_MIN}/분, 재시도 {FMP_MAX_RETRIES}회)")
