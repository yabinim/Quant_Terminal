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


def call(fn, *args, _retries: int = None, _label: str = "", **kwargs):
    """gspread 호출을 재시도로 감싼다. 마지막 시도도 실패하면 예외를 그대로 올린다.

    _retries: 기본 GS_MAX_RETRIES. 0 이면 재시도 없음.
    _label  : 로그용 이름(생략 가능).
    """
    n = GS_MAX_RETRIES if _retries is None else max(0, int(_retries))
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
            wait = min(GS_BACKOFF_BASE * (2 ** attempt), GS_BACKOFF_CAP)
            wait += _random.uniform(0, 1.0)
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
