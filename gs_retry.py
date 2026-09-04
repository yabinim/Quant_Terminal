"""gs_retry.py — Google Sheets(gspread) 호출 재시도 SSOT. streamlit 무의존.

배경
────
2026-08-13, `earnings_only` 실행이 첫 줄에서 죽었다.

    gspread.exceptions.APIError: [503]: The service is currently unavailable.

스프레드시트를 여는 `get_gspread_client().open()` 한 번이 실패했을 뿐인데
실행 전체가 종료됐다. 확인해보니 자동화 5개(run_earnings_watch,
run_watchlist_alerts, run_narrative, run_scanner_scan, run_hidden_alpha)
**전부 gspread 재시도가 없었다.**

FMP 쪽은 200/분 스로틀 + 3회 백오프로 방어돼 있는데(fmp_http), 정작 모든
데이터가 오가는 Sheets 는 무방비였다. 5PM 정기 실행이 같은 이유로 죽으면
그날 알림이 통째로 사라진다.

재시도 정책
──────────
재시도함 : 429(쿼터) · 500 · 502 · 503 · 504 · 네트워크 예외
재시도 안 함:
  · 4xx(400/403/404 등) — 권한·잘못된 범위·없는 시트. 재시도해도 같다.
  · WorksheetNotFound — **정상 흐름**이다. `_ws()` 는 이 예외를 잡아 시트를
    생성한다. 여기서 재시도하면 신규 시트 생성이 3회 백오프만큼 늦어진다.

사용
────
    import gs_retry as gsr

    sh   = gsr.call(get_gspread_client().open, _SPREADSHEET_TITLE)
    vals = gsr.call(ws.get_all_values) or []
    gsr.call(ws.update, values, range_name="A2:C9", value_input_option="USER_ENTERED")

    print(gsr.stats_line())
"""

from __future__ import annotations

import os as _os
import random as _random
import threading as _threading
import time as _time
from typing import NamedTuple as _NamedTuple

SSOT_VERSION = "2026-08-13a"

# gspread 는 자동화 환경에만 있으면 된다. 없으면 예외 분류를 상태코드 문자열
# 파싱으로만 수행한다(동작은 유지).
try:  # pragma: no cover
    import gspread as _gspread
    _APIError = _gspread.exceptions.APIError
    _WorksheetNotFound = _gspread.exceptions.WorksheetNotFound
    _SpreadsheetNotFound = _gspread.exceptions.SpreadsheetNotFound
except Exception:  # pragma: no cover
    _gspread = None

    class _APIError(Exception):
        pass

    class _WorksheetNotFound(Exception):
        pass

    class _SpreadsheetNotFound(Exception):
        pass


GS_MAX_RETRIES = max(0, int(_os.environ.get("GS_MAX_RETRIES", "4") or 4))
GS_BACKOFF_BASE = 1.5     # 초. 1.5 → 3 → 6 → 12 (+지터)
GS_BACKOFF_CAP = 20.0

RETRYABLE_STATUS = (429, 500, 502, 503, 504)


# ══════════════════════════════════════════════════════════════════════════
# 재시도 프로파일 (2026-09-04)
# ══════════════════════════════════════════════════════════════════════════
# 왜 프로파일이 필요한가
# ──────────────────────
# run_signal_backtest.py 와 diag_satellite_backtest.py 는 이 모듈과 **거의 같은**
# 재시도 래퍼(`_gs` / `_gs_is_transient`)를 각자 복사해 갖고 있었다. 지우고
# gsr.call 로 기계적으로 치환하면 재시도 예산이 **62초 → 22초로 조용히 줄어든다.**
#
# 그 62초는 우연이 아니다. 백테스트는 몇 시간치 FMP 수집이 끝난 **맨 마지막에**
# 시트에 쓴다. 그 한 번의 쓰기가 실패하면 런 전체가 날아간다. 반대로 이 모듈의
# 기본 22초는 5PM 알림 경로용이고, 거기서는 늦는 것 자체가 실패다.
# **예산 차이는 유지해야 할 설계 결정이지 정리 대상이 아니다.**
#
# 그래서 구현을 하나로 합치되 정책은 호출부가 고르게 한다. 중복은 사라지고
# 차이는 한 줄 선언으로 눈에 보이게 남는다.
#
# 통합하면서 **의도적으로 바꾼 것 하나**
# ────────────────────────────────────
# 백테스트 쪽 `_gs_is_transient` 는 HTTP 상태를 못 읽는 예외를 **재시도하지 않았다**
# (`code is None → False`). requests 의 ConnectionError/Timeout/ChunkedEncodingError
# 세 종류만 이름으로 잡았다. 그래서 `google.auth.exceptions.TransportError`,
# `ssl.SSLError`, `http.client.RemoteDisconnected` 같은 것들이 오면
# **재시도 없이 런이 죽었다.** 이 모듈의 `_retryable` 은 반대로 보수적 재시도를
# 택한다(실패 방향이 안전하다). 통합 후에는 이 모듈 정책을 따른다 —
# 정책 선택이 아니라 저쪽 버그를 고치는 것이다.
class RetryProfile(_NamedTuple):
    """재시도 정책 묶음. 스케줄과 지터가 따로 놀지 않도록 한 덩어리로 묶는다."""
    retries: int            # None 이면 GS_MAX_RETRIES
    backoff: tuple          # 고정 대기 스케줄(초). 비어 있으면 기하 백오프
    jitter_frac: float      # 0 이면 균등 0~1.0초, >0 이면 대기의 그 비율만큼
    name: str


# 백테스트 프로파일 — 이전 `_gs` 의 값을 **그대로** 옮겼다.
#   시도 6회(재시도 5) · 2→4→8→16→32초 · 지터 대기의 0~25%  ≒ 총 예산 62초
BATCH_BACKOFF = (2, 4, 8, 16, 32, 60)
PROFILE_BATCH = RetryProfile(retries=5, backoff=BATCH_BACKOFF,
                             jitter_frac=0.25, name="batch")


def _wait_for(attempt: int, backoff: tuple, jitter_frac: float) -> float:
    """attempt(0-based) 회차의 대기 시간(초).

    ⚠️ backoff 가 비고 jitter_frac 이 0 이면 **2026-09-04 이전 공식과 완전히
       동일하다** — 같은 식, 같은 난수 호출 횟수. 기존 소비자 7개
       (run_earnings_watch · refresh_market_calendar · refresh_industry_perf ·
       backfill_insider_stats · diag_earnings_batchwrite 등)의 동작이
       한 톨도 바뀌면 안 된다.
    """
    if backoff:
        wait = float(backoff[min(attempt, len(backoff) - 1)])
    else:
        wait = min(GS_BACKOFF_BASE * (2 ** attempt), GS_BACKOFF_CAP)
    if jitter_frac and jitter_frac > 0:
        wait += _random.uniform(0, wait * jitter_frac)
    else:
        wait += _random.uniform(0, 1.0)
    return wait

_lock = _threading.Lock()
_stats = {"ok": 0, "retries": 0, "recovered": 0, "gave_up": 0,
          "not_found": 0, "fatal": 0, "wait_sec": 0.0}


def _status_of(exc) -> int | None:
    """예외에서 HTTP 상태코드 추출. 못 찾으면 None.

    gspread 버전에 따라 APIError 가 상태를 담는 위치가 다르다
    (`.response.status_code` / `.code` / 메시지의 '[503]'). 셋 다 본다.
    """
    r = getattr(exc, "response", None)
    sc = getattr(r, "status_code", None)
    if isinstance(sc, int):
        return sc
    sc = getattr(exc, "code", None)
    if isinstance(sc, int):
        return sc
    s = str(exc)
    for code in RETRYABLE_STATUS + (400, 401, 403, 404):
        if f"[{code}]" in s:
            return code
    return None


def _retryable(exc) -> bool:
    # 시트/스프레드시트 없음은 정상 흐름 — 호출부가 잡아서 생성한다.
    if isinstance(exc, (_WorksheetNotFound, _SpreadsheetNotFound)):
        return False
    st = _status_of(exc)
    if st is None:
        # 상태를 못 읽는 예외 = 네트워크/타임아웃/커넥션 리셋 계열로 본다.
        # APIError 인데 상태가 안 읽히면 보수적으로 재시도한다(실패 방향이 안전).
        return True
    return st in RETRYABLE_STATUS


def call(fn, *args, _retries: int = None, _label: str = "",
         _profile: "RetryProfile" = None, **kwargs):
    """gspread 호출을 재시도로 감싼다. 마지막 시도도 실패하면 예외를 그대로 올린다.

    _retries: 기본 GS_MAX_RETRIES(또는 _profile.retries). 0 이면 재시도 없음.
              명시하면 프로파일보다 우선한다.
    _label  : 로그용 이름(생략 가능).
    _profile: RetryProfile. None 이면 기존 기본 정책(기하 백오프 · 22초 예산).
              장시간 배치는 PROFILE_BATCH 를 넘긴다.

    ⚠️ _profile=None 경로는 2026-09-04 이전과 **동작이 동일해야 한다.**
       diag_gs_retry.py 의 G1~G3 가 대기열을 초 단위로 대조해 이를 못 박는다.
    """
    prof = _profile
    backoff = tuple(prof.backoff) if prof is not None else ()
    jitter = float(prof.jitter_frac) if prof is not None else 0.0
    if _retries is not None:
        n = max(0, int(_retries))
    elif prof is not None and prof.retries is not None:
        n = max(0, int(prof.retries))
    else:
        n = GS_MAX_RETRIES
    name = _label or getattr(fn, "__name__", "gs_call")

    for attempt in range(n + 1):
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:
            if isinstance(exc, (_WorksheetNotFound, _SpreadsheetNotFound)):
                with _lock:
                    _stats["not_found"] += 1
                raise
            if not _retryable(exc) or attempt >= n:
                with _lock:
                    if _retryable(exc):
                        _stats["gave_up"] += 1
                    else:
                        _stats["fatal"] += 1
                raise
            wait = _wait_for(attempt, backoff, jitter)
            with _lock:
                _stats["retries"] += 1
                _stats["wait_sec"] += wait
            print(f"  [GS-RETRY] {name} — {_status_of(exc) or type(exc).__name__} "
                  f"({attempt + 1}/{n}) {wait:.1f}초 후 재시도")
            _time.sleep(wait)
        else:
            with _lock:
                _stats["ok"] += 1
                if attempt > 0:
                    _stats["recovered"] += 1
            return out


def stats() -> dict:
    with _lock:
        return dict(_stats)


def reset_stats() -> None:
    with _lock:
        for k in _stats:
            _stats[k] = 0.0 if k == "wait_sec" else 0


def stats_line() -> str:
    s = stats()
    return (f"Sheets {s['ok']}콜 — 재시도 {s['retries']}회"
            f"(회복 {s['recovered']}) · 최종포기 {s['gave_up']} · "
            f"치명 {s['fatal']} · 대기 {s['wait_sec']:.0f}초 "
            f"(재시도 한도 {GS_MAX_RETRIES})")
