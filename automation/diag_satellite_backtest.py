#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_satellite_backtest.py — 🛰️ HSA 위성 섹터 로테이션 백테스트 (읽기 전용 진단)

목적
────
주말 Hidden Alpha 이메일의 '위성 섹터 Top10'을 보고 Top5 를 보유하다가
순위가 바뀌면 즉시 교체하는 실제 운용 방식의 과거 성과를 재현한다.

  자본금 $5,000 · 최초 1~5위에 $1,000 씩 · 추가 납입 없음 · 소수점 매수

랭킹 로직은 fmp_extras.compute_satellite_top10 을 **재구현하지 않고 그대로 복제**한다
(SSOT 원칙 — 후보 풀·섹터 라벨은 fmp_extras 에서 import, 점수식만 과거 시점 슬라이스로 반복 계산).

  점수 = 1M×0.40 + 3M×0.40 + 6M×0.20   (1주는 노이즈 — 점수 제외)
  GICS 섹터당 최고점 1개만 챔피언 → 점수 내림차순
  히스토리 127봉 미만 티커 제외 (라이브와 동일)
  시장 필터 = SPY 종가 vs 최근 200봉 평균 (라이브의 spy.tail(200).mean() 과 동일)

검증 설계
─────────
· 미래 훔쳐보기 차단: 랭킹은 신호일 t 까지의 데이터만 사용.
· 체결 지연: 신호일(금요일 종가) → **다음 거래일(월요일) 종가** 체결.
  이메일이 주말에 오고 월요일 10시 이후 집행하는 실제 흐름과 동일.
· 슬리피지: 편도 0.05% (매수·매도 각각). 수수료 0 (Fidelity ETF·소수점 매수).
· 성과는 adjClose(배당 재투자) 기준, 랭킹은 close 기준 — 라이브 랭킹과 정합.
  adjClose 가 close 와 사실상 동일하면 배당 미반영으로 판단해 경고를 출력한다.

⚠️ 결과 해석 시 반드시 감안할 편향 (이 스크립트로 제거 불가)
────────────────────────────────────────────────────────────
 1) **후보 풀 선택 편향**: fmp_extras.SECTOR_THEME_ETFS 56개는 2026년 현재 시점에
    사람이 고른 목록이다. 과거로 돌리면 '미래를 아는 풀'이라 결과가 실제보다 좋게 나온다.
 2) **구간 편향**: FMP 계정 이력 한도가 1255봉(5년 롤링)이라 2020 코로나·2022 초입
    하락장이 데이터에 없다. 커버 구간은 대체로 강세장이다.
 → 따라서 절대 수익률은 '상한선'으로 보고, **조건 간 상대 비교**(주간 vs 월간,
   시장필터 유무, 밴드 룰 유무)에만 신뢰를 두는 것이 맞다.

실행
────
  python automation/diag_satellite_backtest.py            # 전체 백테스트
  python automation/diag_satellite_backtest.py --selftest  # 엔진 자체검증(네트워크 불필요)

아무것도 수정하지 않는다. Google Sheets `Satellite_Backtest` 탭에 결과 행만 append.
(GSPREAD_KEY 가 없으면 시트 기록은 건너뛰고 콘솔 출력만 한다.)
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime

import numpy as np
import pandas as pd
import pytz

# ── 리포 루트 + 자기 폴더를 sys.path 에 (실행 위치 무관) ─────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import fmp_extras as fx  # noqa: E402  — 후보 풀 SSOT
import fmp_http as fh    # noqa: E402  — FMP 레이트리밋/429 SSOT
import gs_retry as gsr   # noqa: E402  — Sheets 재시도 SSOT

# ── 환경 ──────────────────────────────────────────────────────────────────────
FMP_API_KEY      = os.environ.get("FMP_API_KEY", "")
GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")

_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")

_FMP_BASE      = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT   = 20           # v2.8: 12 는 1,300봉 페이로드에 짧다. 레이트리밋만
                              #   고치면 탈락 사유가 timeout 으로 옮겨갈 뿐이다.
_FETCH_WORKERS = 8            # 스로틀은 fh.fmp_get_ex 안에 있다. 워커는 한도에
                              #   걸리면 대기할 뿐 요청을 잃지 않으므로 그대로 둔다.

_SPREADSHEET_TITLE = "Quant_DB"
_RESULT_WORKSHEET  = "Satellite_Backtest"


# ── v2.8(2026-08-26): 부분 유니버스 차단 ────────────────────────────────────
# 후보 풀 56개 중 일부만 받아와도 백테스트는 정상 완료되고 결과가 시트에 들어간다.
# Top5 를 고르는 모집단이 달라졌을 뿐 행 모양은 똑같아서 사후에 골라낼 수 없다.
#
# ⚠️ 우회 스위치(SKIP_FETCH_GATE=1 류)는 두지 않는다. '이번만 넘기자' 용 플래그는
#    반드시 기본값이 된다. 낮추려면 이 값을 명시적으로 내려야 하고 로그에 남는다.
#
# ⚠️ 이 두 함수는 run_signal_backtest.py 와 **의도적으로 같은 구현**이다.
#    공유 모듈로 빼지 않은 이유: 그러려면 run_signal_backtest 와 그 락스텝 짝인
#    diag_universe_funnel(68/68 통과 중)까지 함께 손대야 한다. 대신 복제가
#    어긋나지 않는지를 diag_fmp_ssot.py 가 두 모듈을 직접 호출해 대조한다.
def _env_fetch_rate(raw=None, default: float = 0.98) -> float:
    """MIN_FETCH_RATE 파싱. 0.0~1.0 만 허용, 그 외는 경고 후 default."""
    if raw is None:
        raw = os.environ.get("MIN_FETCH_RATE", "")
    t = str(raw).strip()
    if not t:
        return default
    try:
        v = float(t)
    except (TypeError, ValueError):
        print("[WARN] MIN_FETCH_RATE=" + repr(t) + " 는 실수가 아니다 → "
              + str(default) + " 로 폴백")
        return default
    if not (0.0 <= v <= 1.0):
        print("[WARN] MIN_FETCH_RATE=" + str(v) + " 는 범위(0.0~1.0) 밖 → "
              + str(default) + " 로 폴백")
        return default
    if v < default:
        print("[WARN] MIN_FETCH_RATE=" + str(v) + " — 기본 " + str(default)
              + " 보다 낮다. 부분 유니버스 결과가 시트에 들어갈 수 있다.")
    return v


MIN_FETCH_RATE = _env_fetch_rate()

# 인프라성 실패 — 데이터가 원래 없는 것("empty")과 구분한다. 전자는 다시 돌리면
# 달라지고 후자는 안 달라진다. 섞어서 세면 게이트가 영구 빨간불이 된다.
_INFRA_KINDS = ("no_key", "rate_limited", "plan_limited", "http_error", "exception")


def universe_hash(tickers) -> str:
    """정렬된 티커 목록의 SHA1 앞 8자 — run 간 '같은 후보 풀이었나' 를 시트만 보고 판정.

    ⚠️ 반드시 sorted() 를 거쳐야 한다. 집합(set) 순회 순서로 계산하면 구성이
    같은데도 run 마다 값이 달라져 열 자체가 무의미해진다 — 그런데 겉보기에는
    '해시가 잘 찍히는' 정상 동작으로 보인다.
    """
    seq = sorted(str(t).strip().upper() for t in (tickers or [])
                 if str(t).strip())
    if not seq:
        return ""
    # ⚠️ 접두어 'u' 는 장식이 아니다. 시트 쓰기가 USER_ENTERED 라, 16진수 8자가
    #    우연히 전부 숫자면(확률 (10/16)^8 ≈ 2.3%) 구글 시트가 수로 해석해 앞자리
    #    0 을 날린다("00123456" → 123456). 그러면 같은 풀인데 해시가 달라 보이고,
    #    그게 바로 이 열이 막으려던 사고다.
    return "u" + hashlib.sha1("\n".join(seq).encode("utf-8")).hexdigest()[:8]

# ── 백테스트 파라미터 (튜닝 한 곳) ───────────────────────────────────────────
CAPITAL        = 5_000.0      # 시작 자본
SLOTS          = 5            # 보유 슬롯 (Top5)
BAND_SLOTS     = 7            # 3B 밴드 룰: Top7 안이면 계속 보유
SLIPPAGE       = 0.0005       # 편도 0.05%
COMMISSION_PER_TRADE = 0.0    # Fidelity HSA: 미국 주식·ETF 온라인 매매 $0 (확인 완료).
#   ⚠️ 단, 일부 소수 ETF 는 건당 $100 서비스 수수료 대상 — 후보 풀에 해당 종목이
#   있는지는 Fidelity 목록에서 직접 확인해야 한다. 민감도 테스트용 노브로 남겨둔다.
SELL_ASSESSMENT = 0.00002     # 매도 시 SEC 부과금 ≈ 원금 $1,000당 $0.02
ENTRY_LAG_DAYS = 1            # 신호일 → 체결일 (금 종가 신호 → 월 종가 체결)
HISTORY_BARS   = 1300         # 요구 **봉수**. 창 환산은 fmp_extras 가 한다.
#   ⚠️ v2.9 개명: 옛 이름은 HISTORY_LIMIT 이었다. 단위는 처음부터 봉수였고
#      이름만 FMP 의 `limit` 파라미터를 따라갔다. 개명은 장식이 아니라 락스텝
#      장치다 — 이 상수를 빌려 쓰는 파일이 옛 이름으로 남아 있으면
#      AttributeError 로 **크게** 죽는다. 조용히 다른 값을 쓰는 것보다 낫다.
#   ⚠️ 이 값을 올려도 받는 봉수는 안 늘어난다. fmp_extras.HIST_MAX_DAYS(1826일
#      ≈ 1,254봉)가 상한이고, 그 위에 FMP 자체 상한(~1,255봉)이 또 있다.
#      상한에 걸리면 _window_days_for() 가 경고를 한 줄 찍는다 — 조용히 잘리지
#      않게 하려는 것이 그 경고의 유일한 목적이다.

SEG_BARS       = 252          # 연도별 분해 단위(12개월)
SEG_MAX        = 6            # 분해 구간 수 **안전 캡** — 실제 도달값은 4다.
#   ⚠️ 6 을 보고 "6구간이 나온다"고 읽으면 안 된다. build_segments 는 데이터가
#      떨어지면 SEG_MAX 에 닿기 전에 break 한다. 현재 상한으로 계산하면:
#        받는 봉수 ≈ 1,254 (fmp_extras.HIST_MAX_DAYS 1826일 · FMP 자체 상한)
#        end_i ≈ 1253 에서 시작해 lo 가 1002 → 750 → 498 → 246 으로 내려가고
#        5번째는 lo = -6 < WARMUP_BARS(127) → break.
#      즉 **4구간에서 멈춘다**. 일반식: (받는봉수 - WARMUP_BARS) // SEG_BARS.
#      데이터가 짧아지면 더 줄어든다 — 756봉이면 2구간, 504봉이면 1구간이다.
#   ⚠️ 그래도 4 로 낮추지 않는다. 오늘 동작이 똑같은데(이미 4에서 멈춘다),
#      FMP 가 상한을 올리면 4 는 데이터를 버리게 된다. 캡은 캡으로 둔다.
#      진짜 제약은 SEG_MAX 가 아니라 WARMUP_BARS 와 받는 봉수다.

WARMUP_BARS    = 127          # 6M(126봉) 계산 최소 — compute_satellite_top10 과 동일
MA200_BARS     = 200          # 시장 필터
WINDOWS        = {"1년": 252, "2년": 504, "3년": 756}   # 거래일 기준

BENCH_TICKERS  = ("SPY", "QQQ")

# 점수식은 fmp_extras 의 룰 SSOT(fx.MOM_RULES / fx.mom_score)가 유일한 출처다.
# 여기에 가중치 숫자를 다시 적어두지 않는다 — 예전에는 _W_1M/_W_3M/_W_6M 사본이
# 있었고, 라이브가 바뀌어도 이 파일은 조용히 옛 식으로 계속 돌 수 있었다.

# ── 집중도·가중 스킴 (룰 비교 전용 축) ───────────────────────────────────────
# 차등 가중은 **rebal 에서만** 정의된다. swap 은 매도한 슬롯의 현금만 재투입하므로
# 목표 비중이라는 개념 자체가 없다 — swap + 차등을 허용하면 의도와 다른 드리프트
# 포트폴리오를 '차등 가중'이라고 부르며 비교하게 된다. 그래서 예외로 막는다.
WEIGHT_SCHEMES = {
    "eq1":   (1.00,),
    "eq3":   (1 / 3, 1 / 3, 1 / 3),
    "eq5":   (0.20, 0.20, 0.20, 0.20, 0.20),
    "tier3": (0.50, 0.30, 0.20),
    "tier5": (0.30, 0.25, 0.20, 0.15, 0.10),
}

# ── 설정 그리드 ───────────────────────────────────────────────────────────────
FREQS      = ("weekly", "monthly")           # 1A/1B
SWAPS      = ("swap", "rebal")               # 2A(교체분만) / 2B(매주 균등 재조정)
SELLRULES  = ("top5", "top7")                # 3A / 3B
MKTFILTERS = ("none", "no_new", "all_cash")  # 4A / 4B / 4C

BASELINE = ("weekly", "swap", "top5", "none")   # 실제 운용 방식

_RESULT_COLS = [
    "Run_Date", "Window", "Freq", "Swap", "SellRule", "MktFilter",
    "Start", "End", "Capital", "Final_Equity", "Total_Ret_Pct", "CAGR_Pct",
    "MDD_Pct", "Sharpe", "Trades", "WinRate_Pct", "Turnover_x",
    "Slippage_Cost", "vs_SPY_pp", "Div_Basis",
    "Universe_Hash",              # v2.8: 이 run 의 실제 랭킹 후보 집합 지문
]


# ══════════════════════════════════════════════════════════════════════════════
# 데이터 수집
# ══════════════════════════════════════════════════════════════════════════════
_WARNED_CEILING = set()          # 경고 1회 제한 — 8워커가 동시에 찍으면 못 읽는다
_WARN_LOCK = threading.Lock()


def _window_days_for(bars: int, warn=print) -> int:
    """요구 봉수 → 조회 창(달력일). 상한에 걸리면 **한 번** 경고한다.

    환산 정책은 `fmp_extras` 가 유일 소유자다. 여기서 0.6871 이나 창 상수를
    복제하지 않는다 — 복제하면 0.6871 은 한 벌이어도 정책이 여러 벌이 되고,
    나중에 한쪽만 갱신된다. 그 실패는 로그를 남기지 않는다.

    경고가 왜 필요한가
    ──────────────────
    `limit` 을 `from`/`to` 로 바꾸는 것만으로는 원래 위험이 사라지지 않는다.
    "숫자를 올려도 아무 일 없음"이 FMP 의 미공개 quirk 에서 `HIST_MAX_DAYS`
    상한으로 **자리만 옮길** 뿐이다. HISTORY_BARS 를 1,500 으로 올려도 창은
    여전히 1,826일(≈1,254봉)이고, 에러도 경고도 없이 같은 데이터가 온다.

    이게 실제로 사고를 냈다. run_signal_backtest v2.4 에서 TEST_LOOKBACK 을
    올렸을 때 URL 은 바뀌고 데이터는 안 바뀌었는데, 원인을 "종목이 조건 미달로
    탈락했다"로 오진해 v2.8 에서 정정하는 데 두 번을 돌았다. 그 오진의 비용이
    이 한 줄보다 훨씬 컸다.

    warn: 주입 가능 — 진단이 경고 발생 여부를 관찰하기 위함.
    """
    days = fx.hist_days_for_bars(bars)
    if days >= fx.HIST_MAX_DAYS:
        with _WARN_LOCK:
            if bars not in _WARNED_CEILING:
                _WARNED_CEILING.add(bars)
                warn(f"[WARN] 요구 {bars}봉은 조회 상한에 잘린다 — "
                     f"창 {days}달력일 ≈ {int(days * fx.HIST_TD_PER_CD)}봉 "
                     f"(fmp_extras.HIST_MAX_DAYS={fx.HIST_MAX_DAYS}). "
                     f"이 값을 더 올려도 받는 봉수는 늘지 않는다.")
    return days


def _fmp_eod(ticker: str, endpoint: str, *, bars: int) -> tuple:
    """/stable/historical-price-eod/{endpoint} → DatetimeIndex + 'px' 컬럼.

    endpoint='full'              : 원 종가 (랭킹용 — 라이브 compute_satellite_top10 과 동일)
    endpoint='dividend-adjusted' : 배당 재투자 반영 종가 (성과 측정용)

    반환: (DataFrame, kind). kind ∈ {"ok","empty","no_key","rate_limited",
    "plan_limited","http_error","exception"}

    v2.9: `&limit=` 을 `from`/`to` 창으로 바꿨다.
      FMP 는 `historical-price-eod` 의 `limit` 을 **조용히 무시**한다(§7 확정
      사실). 지금까지 무해했던 이유는 우연이다 — HISTORY_LIMIT=1,300 이 FMP 가
      항상 돌려주는 봉수(~1,255)보다 커서 늘 전량을 받았을 뿐이다.
      위험은 값이 아니라 구조에 있었다. 자세한 것은 `_window_days_for` 참조.

      `bars` 는 **키워드 전용 · 기본값 없음**이다(§7). 기본값을 두면 호출부가
      요구를 빠뜨려도 그럴듯한 값으로 메워져, 요구가 바뀔 때 한쪽만 갱신되는
      사고를 만든다.

    v2.8: 두 가지를 고쳤다.
      1) requests.get → fh.fmp_get_ex. 이 경로만 SSOT 를 우회하고 있었다.
         슬라이딩 윈도우 스로틀 + 429 지수 백오프 + 지터가 딸려온다. 이전의
         자체 재시도(1.5초·3초)는 분당 한도 앞에서 무력했다 — 같은 1분 안에
         다시 쏘기 때문이다.
      2) 실패를 빈 DataFrame 하나로 뭉개지 않는다. 402(플랜 제한)·429(레이트
         리밋)·빈 200 응답은 원인도 대응도 전혀 다르다. 특히 이 파일에서는
         dividend-adjusted 실패가 **원 종가로 조용히 대체**되므로, 사유를
         남기지 않으면 성과 기준이 언제 오염됐는지 영원히 알 수 없다.
    """
    if not FMP_API_KEY:
        return pd.DataFrame(), "no_key"
    url = (f"{_FMP_BASE}/historical-price-eod/{endpoint}"
           f"?symbol={ticker}&apikey={FMP_API_KEY}"
           + fx.hist_range_params(_window_days_for(bars)))
    r, _status, kind = fh.fmp_get_ex(url, timeout=_FMP_TIMEOUT)
    if r is None or kind != "ok":
        return pd.DataFrame(), kind
    try:
        data = r.json()
        rows = data.get("historical", data) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return pd.DataFrame(), "empty"
        df = pd.DataFrame(rows)
        if "date" not in df.columns:
            return pd.DataFrame(), "empty"
        # dividend-adjusted 는 adjClose 로 오기도 하고 close 가 이미 조정치이기도 하다
        col = "adjClose" if "adjClose" in df.columns else "close"
        if col not in df.columns:
            return pd.DataFrame(), "empty"
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).set_index("date").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        out = pd.DataFrame(index=df.index)
        out["px"] = pd.to_numeric(df[col], errors="coerce")
        out = out.dropna(subset=["px"])
        return (out, "ok") if not out.empty else (out, "empty")
    except Exception:
        return pd.DataFrame(), "exception"


def _batch_fetch(tickers: list, *, bars: int) -> tuple:
    """(raw_close{}, div_adj{}, fallback_list, reasons{}, failed[]) — 병렬 수집.

    reasons : {(endpoint, kind): count} — 엔드포인트별 성공/실패 사유 분포
    failed  : [(ticker, endpoint, kind), ...] — 탈락 항목과 그 이유

    v2.9: `bars` 를 **중간층까지 키워드 전용 · 기본값 없이** 받는다(§7).
      중간층에 기본값을 두면 상위가 요구를 빠뜨려도 조용히 메워진다. 여기서는
      특히 나쁘다 — 랭킹용(full)과 성과측정용(dividend-adjusted)이 같은 창을
      받아야 하는데, 한쪽만 다른 창을 받으면 **수익률 기준이 서로 다른 기간**이
      되고 결과 행 모양은 똑같아서 사후에 골라낼 수 없다.

    v2.8: 이전에는 `except Exception: df = pd.DataFrame()` 로 예외까지 삼켜
    탈락 사유가 하나도 남지 않았다. 특히 dividend-adjusted 실패는 바로 아래에서
    원 종가로 대체되므로, 사유가 없으면 **성과 기준이 종목마다 뒤섞인 채로
    정상처럼 보이는 결과**가 나온다.
    """
    raw, adj = {}, {}
    reasons: dict = {}
    failed: list = []
    if not tickers:
        return raw, adj, [], reasons, failed
    jobs = [(tk, "full") for tk in tickers] + [(tk, "dividend-adjusted") for tk in tickers]
    with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
        futs = {ex.submit(_fmp_eod, tk, ep, bars=bars): (tk, ep) for tk, ep in jobs}
        for fut in concurrent.futures.as_completed(futs):
            tk, ep = futs[fut]
            try:
                df, kind = fut.result()
            except Exception:
                df, kind = pd.DataFrame(), "exception"
            reasons[(ep, kind)] = reasons.get((ep, kind), 0) + 1
            if df is None or df.empty:
                failed.append((tk, ep, kind))
                continue
            (raw if ep == "full" else adj)[tk] = df["px"]
    fallback = []
    for tk in list(raw):
        if tk not in adj:
            adj[tk] = raw[tk]          # 배당조정 실패 → 원 종가로 폴백
            fallback.append(tk)
    return raw, adj, fallback, reasons, failed


def build_panels(raw: dict, adj: dict, calendar_ticker: str = "SPY"):
    """{ticker: Series} 2벌 → (close_df, adj_df) — SPY 거래일 캘린더에 정렬."""
    if calendar_ticker not in raw:
        raise RuntimeError(f"{calendar_ticker} 히스토리 확보 실패 — 캘린더 기준을 만들 수 없다")
    cal = raw[calendar_ticker].index
    close = pd.DataFrame(index=cal)
    adjp = pd.DataFrame(index=cal)
    for tk, s in raw.items():
        close[tk] = s.reindex(cal)
        a = adj.get(tk)
        adjp[tk] = (a.reindex(cal) if a is not None else s.reindex(cal))
    # 산발적 결측(휴장 차이 등)만 최대 3봉 보간 — 상장 이전 구간은 NaN 유지
    close = close.ffill(limit=3)
    adjp = adjp.ffill(limit=3)
    adjp = adjp.where(adjp > 0)
    return close, adjp


# ══════════════════════════════════════════════════════════════════════════════
# 랭킹 엔진 — compute_satellite_top10 의 과거 시점 복제
# ══════════════════════════════════════════════════════════════════════════════
def _trailing_return(vals: np.ndarray, bars: int):
    if len(vals) <= bars:
        return np.nan
    prev = vals[-1 - bars]
    if not np.isfinite(prev) or prev == 0:
        return np.nan
    return float((vals[-1] / prev - 1.0) * 100.0)


class RankEngine:
    """티커별 유효 종가 배열을 미리 잘라두고, 날짜별 챔피언 랭킹을 계산한다.

    rank_rule : fx.MOM_RULES 의 키. 기본 "blend" = 라이브 compute_satellite_top10.
    warmup    : 랭킹에 필요한 최소 봉수. None 이면 룰의 필요 봉수를 쓴다.
      ⚠️ 룰을 **비교**할 때는 반드시 세 룰 공통값(fx.mom_warmup_bars())을 넘겨라.
         룰마다 다른 워밍업을 쓰면 시작일이 달라져 서로 다른 구간을 비교하게 된다.
    """

    def __init__(self, close_df: pd.DataFrame, rank_rule: str = "blend",
                 warmup: int | None = None):
        if rank_rule not in fx.MOM_RULES:
            raise KeyError(f"알 수 없는 랭킹 룰: {rank_rule!r}")
        self.rank_rule = rank_rule
        self.warmup = int(warmup) if warmup else int(fx.MOM_RULES[rank_rule][1])
        self.pool = fx.satellite_candidate_pool()          # {섹터: [후보들]}
        self.sector_of = {}
        self.series = {}
        for sec, cands in self.pool.items():
            for tk in cands:
                self.sector_of[tk] = sec
                if tk in close_df.columns:
                    s = close_df[tk].dropna()
                    if not s.empty:
                        self.series[tk] = (s.index.values, s.to_numpy(dtype=float))
        self._cache = {}

    def rank_at(self, date) -> list:
        """date(포함) 까지 데이터만으로 산출한 챔피언 랭킹 [{ticker, sector, score}, ...]."""
        key = pd.Timestamp(date)
        if key in self._cache:
            return self._cache[key]
        champions = []
        for sec, cands in self.pool.items():
            best = None
            for tk in cands:
                ser = self.series.get(tk)
                if ser is None:
                    continue
                idx, vals = ser
                n = int(np.searchsorted(idx, np.datetime64(key), side="right"))
                if n < self.warmup:                     # 라이브의 len(s) < 127 스킵과 동일
                    continue
                # 점수식은 fmp_extras 룰 SSOT. 여기서 다시 쓰지 않는다.
                score = fx.mom_score(vals[:n], self.rank_rule)
                if not np.isfinite(score):
                    continue
                if best is None or score > best["score"]:
                    best = {"ticker": tk, "sector": sec, "score": score}
            if best is not None:
                champions.append(best)
        champions.sort(key=lambda r: r["score"], reverse=True)
        self._cache[key] = champions
        return champions


def market_risk_on(spy_close: pd.Series, date) -> bool:
    """SPY 종가 vs 최근 200봉 평균 — 라이브 compute_satellite_top10 과 동일 정의."""
    s = spy_close.loc[:date].dropna()
    if len(s) < MA200_BARS:
        return True                                   # 판단 불가 시 정상 운용
    return bool(float(s.iloc[-1]) > float(s.tail(MA200_BARS).mean()))


# ══════════════════════════════════════════════════════════════════════════════
# 리밸런싱 날짜
# ══════════════════════════════════════════════════════════════════════════════
def signal_dates(index: pd.DatetimeIndex, freq: str) -> list:
    """freq 별 신호일 = 각 주/월의 마지막 거래일."""
    s = pd.Series(index, index=index)
    if freq == "weekly":
        grouped = s.groupby([index.isocalendar().year, index.isocalendar().week])
    else:
        grouped = s.groupby([index.year, index.month])
    return sorted(grouped.max().tolist())


# ══════════════════════════════════════════════════════════════════════════════
# 시뮬레이터
# ══════════════════════════════════════════════════════════════════════════════
def simulate(cfg: tuple, engine: RankEngine, close_df: pd.DataFrame, adj_df: pd.DataFrame,
             start_i: int, end_i: int, capital: float = CAPITAL,
             slots: int | None = None, weights: tuple | None = None) -> dict:
    """cfg=(freq, swap, sellrule, mktfilter) 로 [start_i, end_i] 구간을 시뮬레이션.

    slots   : 보유 슬롯 수. None 이면 모듈 기본 SLOTS(5) — 기존 그리드와 동일.
    weights : 순위별 목표 비중 튜플(합 1.0). None 이면 균등.
              rebal 에서만 유효하며, len(weights) == slots 이어야 한다.

    ⚠️ sellrule="top5" 는 '5' 가 아니라 '슬롯 수만큼' 을 뜻한다. slots=3 이면
       Top3 밖으로 밀린 종목을 판다. 이름을 안 바꾼 이유는 기존 시트 행의
       SellRule 값과 대조가 끊기기 때문이다.
    """
    freq, swap, sellrule, mktfilter = cfg
    n_slots = int(slots) if slots else SLOTS
    if weights is not None:
        w = tuple(float(x) for x in weights)
        if len(w) != n_slots:
            raise ValueError(f"weights 길이 {len(w)} != slots {n_slots}")
        if abs(sum(w) - 1.0) > 1e-9:
            raise ValueError(f"weights 합이 1.0 이 아님: {sum(w)}")
        if swap != "rebal":
            raise ValueError("차등 가중은 rebal 에서만 정의된다 (swap 은 목표 비중이 없다)")
    else:
        w = None
    index = adj_df.index
    keep_thresh = n_slots if sellrule == "top5" else BAND_SLOTS

    all_sig = signal_dates(index, freq)
    pos_of = {d: i for i, d in enumerate(index)}
    # 신호일 + 체결일(신호일 + ENTRY_LAG_DAYS)이 모두 구간 안에 들어오는 것만
    plan = []
    for d in all_sig:
        i = pos_of.get(pd.Timestamp(d))
        if i is None:
            continue
        j = i + ENTRY_LAG_DAYS
        if j > end_i or i < start_i:
            continue
        plan.append((i, j))
    if not plan:
        return {}

    cash = float(capital)
    shares: dict = {}          # {ticker: 주식수}
    basis: dict = {}           # {ticker: 취득원가($)}
    trades: list = []
    slip_cost = 0.0
    traded_notional = 0.0
    exec_map = {j: i for i, j in plan}
    rebal_log: list = []

    equity_dates, equity_vals = [], []
    sim_start = plan[0][1]

    def px(tk, i):
        v = adj_df[tk].iloc[i] if tk in adj_df.columns else np.nan
        return float(v) if pd.notna(v) and v > 0 else np.nan

    def sell(tk, i, frac=1.0):
        nonlocal cash, slip_cost, traded_notional
        p = px(tk, i)
        if not np.isfinite(p) or tk not in shares:
            return
        qty = shares[tk] * frac
        gross = qty * p
        if gross <= 0:
            return
        cost = gross * (SLIPPAGE + SELL_ASSESSMENT) + COMMISSION_PER_TRADE
        cash += gross - cost
        slip_cost += cost
        traded_notional += gross
        shares[tk] -= qty
        if frac >= 1.0 or shares[tk] <= 1e-9:
            b = basis.pop(tk, 0.0)
            ret_pct = ((gross - cost) / b - 1.0) * 100.0 if b > 1e-9 else float("nan")
            trades.append({"ticker": tk, "ret_pct": ret_pct, "exit": index[i]})
            shares.pop(tk, None)
        else:
            basis[tk] = basis.get(tk, 0.0) * (1 - frac)

    def buy(tk, i, dollars):
        nonlocal cash, slip_cost, traded_notional
        p = px(tk, i)
        if not np.isfinite(p) or dollars <= 0.01:
            return
        dollars = min(dollars, cash)
        if dollars <= COMMISSION_PER_TRADE:
            return
        cost = dollars * SLIPPAGE + COMMISSION_PER_TRADE
        qty = (dollars - cost) / p
        if qty <= 0:
            return
        cash -= dollars
        slip_cost += cost
        traded_notional += dollars
        shares[tk] = shares.get(tk, 0.0) + qty
        basis[tk] = basis.get(tk, 0.0) + dollars

    prev_held: set = set()
    maxw_track: list = []
    risk_on = True
    for i in range(sim_start, end_i + 1):
        if i in exec_map:
            prev_held = set(shares)
            sig_i = exec_map[i]
            sig_date = index[sig_i]
            champs = engine.rank_at(sig_date)
            rank_of = {c["ticker"]: n for n, c in enumerate(champs, 1)}
            risk_on = True
            if mktfilter != "none" and "SPY" in close_df.columns:
                risk_on = market_risk_on(close_df["SPY"], sig_date)

            if mktfilter == "all_cash" and not risk_on:
                keep, buys = [], []
            else:
                keep = [tk for tk in list(shares) if rank_of.get(tk, 10**6) <= keep_thresh]
                free = n_slots - len(keep)
                if mktfilter == "no_new" and not risk_on:
                    buys = []
                else:
                    top = [c["ticker"] for c in champs[:n_slots]]
                    buys = [tk for tk in top if tk not in keep][:max(0, free)]

            for tk in [t for t in list(shares) if t not in keep]:
                sell(tk, i)

            if swap == "swap":
                if buys:
                    each = cash / len(buys)
                    for tk in buys:
                        buy(tk, i, each)
            else:  # rebal — 슬롯 목표 비중 재조정
                targets = keep + buys
                if targets:
                    held_val = sum(shares.get(t, 0.0) * px(t, i) for t in targets
                                   if np.isfinite(px(t, i)))
                    equity = cash + held_val
                    # 분모는 '생존 종목 수'가 아니라 항상 슬롯 수.
                    # risk-off 로 슬롯이 비면 그만큼 현금으로 남아야 한다.
                    # (len(targets) 로 나누면 남은 종목에 몰빵되어 필터 의도가 뒤집힌다)
                    #
                    # 차등 가중도 같은 원칙이다 — 비중은 **순위 순서**로 배정하고,
                    # 슬롯이 비면 그 슬롯 몫은 재분배하지 않고 현금으로 둔다.
                    # ⚠️ 균등일 때는 **모든** 대상에 equity/n_slots 를 준다.
                    #    top7 밴드(sellrule="top7")에서는 보유가 슬롯 수를 넘을 수
                    #    있고, 그때 뒤쪽을 목표 0 으로 잘라내면 밴드 룰의 의미가
                    #    사라진다(밴드는 '순위가 좀 밀려도 안 판다' 는 규칙이다).
                    #    현금 부족은 buy() 의 min(..., cash) 가 이미 처리한다.
                    #    차등 가중은 w 가 슬롯 수만큼만 있으므로 그 뒤는 0 이 맞다.
                    ordered = sorted(targets, key=lambda t: rank_of.get(t, 10**6))
                    if w is None:
                        tgt_of = {t: equity / n_slots for t in ordered}
                    else:
                        tgt_of = {t: equity * w[k] for k, t in enumerate(ordered)
                                  if k < n_slots}
                    # 순회 순서도 동결한다. 현금이 모자랄 때 **누가 먼저 사느냐**로
                    # 결과가 갈리므로, 균등 경로는 기존 순서(keep+buys)를 그대로 쓴다.
                    loop_order = targets if w is None else ordered
                    for tk in loop_order:                   # 초과분 먼저 매도
                        p = px(tk, i)
                        if not np.isfinite(p):
                            continue
                        tgt = tgt_of.get(tk, 0.0)
                        cur = shares.get(tk, 0.0) * p
                        if cur > tgt * 1.005:
                            sell(tk, i, frac=min(1.0, (cur - tgt) / cur))
                    for tk in loop_order:                   # 부족분 매수
                        p = px(tk, i)
                        if not np.isfinite(p):
                            continue
                        tgt = tgt_of.get(tk, 0.0)
                        cur = shares.get(tk, 0.0) * p
                        if cur < tgt * 0.995:
                            buy(tk, i, min(tgt - cur, cash))

        val = cash + sum(q * px(tk, i) for tk, q in shares.items()
                         if np.isfinite(px(tk, i)))
        equity_dates.append(index[i])
        equity_vals.append(val)
        if val > 0 and shares:
            top_w = max((q * px(tk, i) for tk, q in shares.items()
                         if np.isfinite(px(tk, i))), default=0.0) / val
            maxw_track.append(top_w)

        if i in exec_map:
            rebal_log.append({
                "sig": index[exec_map[i]], "exec": index[i],
                "risk_on": risk_on,
                "sold": sorted(t for t in prev_held if t not in shares),
                "bought": sorted(t for t in shares if t not in prev_held),
                "held": sorted(shares), "cash": cash, "equity": val,
            })

    curve = pd.Series(equity_vals, index=pd.DatetimeIndex(equity_dates))
    m = _metrics(curve, trades, slip_cost, traded_notional, capital)
    if m:
        m["log"] = rebal_log
        m["maxw_mean"] = float(np.mean(maxw_track)) * 100.0 if maxw_track else float("nan")
        m["maxw_peak"] = float(np.max(maxw_track)) * 100.0 if maxw_track else float("nan")
    return m


def _metrics(curve: pd.Series, trades: list, slip_cost: float,
             traded_notional: float, capital: float) -> dict:
    if curve.empty or len(curve) < 5:
        return {}
    final = float(curve.iloc[-1])
    total_ret = (final / capital - 1.0) * 100.0
    years = max(len(curve) / 252.0, 1e-9)
    cagr = ((final / capital) ** (1 / years) - 1.0) * 100.0
    dd = (curve / curve.cummax() - 1.0)
    mdd = float(dd.min()) * 100.0
    rets = curve.pct_change().dropna()
    sharpe = (float(rets.mean()) / float(rets.std()) * np.sqrt(252)
              if len(rets) > 5 and float(rets.std()) > 0 else float("nan"))
    closed = [t for t in trades if np.isfinite(t.get("ret_pct", np.nan))]
    win = (sum(1 for t in closed if t["ret_pct"] > 0) / len(closed) * 100.0) if closed else float("nan")
    avg_eq = float(curve.mean())
    turnover = (traded_notional / 2.0) / avg_eq / years if avg_eq > 0 else float("nan")
    return {"final": final, "total_ret": total_ret, "cagr": cagr, "mdd": mdd,
            "sharpe": sharpe, "trades": len(closed), "win": win,
            "turnover": turnover, "slip": slip_cost,
            "start": curve.index[0], "end": curve.index[-1], "curve": curve}


def buy_hold(tickers: list, adj_df: pd.DataFrame, start_i: int, end_i: int,
             capital: float = CAPITAL) -> dict:
    """벤치마크 — 시작일에 균등 매수 후 만기까지 보유(슬리피지 편도 1회)."""
    usable = [t for t in tickers if t in adj_df.columns
              and pd.notna(adj_df[t].iloc[start_i]) and adj_df[t].iloc[start_i] > 0]
    if not usable:
        return {}
    each = capital / len(usable)
    qty = {t: (each * (1 - SLIPPAGE)) / float(adj_df[t].iloc[start_i]) for t in usable}
    sub = adj_df.iloc[start_i:end_i + 1][usable]
    curve = (sub * pd.Series(qty)).sum(axis=1)
    return _metrics(curve, [], capital * SLIPPAGE, capital, capital)


# ══════════════════════════════════════════════════════════════════════════════
# 출력
# ══════════════════════════════════════════════════════════════════════════════
def _fmt(v, nd=2, suffix=""):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "N/A"
    return f"{v:,.{nd}f}{suffix}"


_LBL = {"weekly": "주간", "monthly": "월간", "swap": "교체분만", "rebal": "균등재조정",
        "top5": "Top5이탈", "top7": "Top7밴드", "none": "필터없음",
        "no_new": "신규중단", "all_cash": "전량현금"}


def print_window_table(win_label: str, results: dict, benches: dict) -> None:
    print(f"\n{'=' * 108}")
    print(f"■ {win_label} 백테스트 — 자본 ${CAPITAL:,.0f} · CAGR 내림차순")
    print("=" * 108)
    print(f"{'주기':<5}{'교체':<11}{'매도룰':<10}{'시장필터':<10}"
          f"{'최종$':>10}{'총수익%':>9}{'CAGR%':>8}{'MDD%':>8}{'샤프':>7}"
          f"{'거래':>6}{'승률%':>7}{'회전x':>7}{'vsSPY':>8}")
    print("-" * 108)
    spy_cagr = benches.get("SPY", {}).get("cagr", float("nan"))
    for cfg, m in sorted(results.items(), key=lambda kv: -(kv[1].get("cagr") or -1e9)):
        if not m:
            continue
        mark = " ◀ 실제운용" if cfg == BASELINE else ""
        vs = m["cagr"] - spy_cagr if np.isfinite(spy_cagr) else float("nan")
        print(f"{_LBL[cfg[0]]:<5}{_LBL[cfg[1]]:<11}{_LBL[cfg[2]]:<10}{_LBL[cfg[3]]:<10}"
              f"{_fmt(m['final'], 0):>10}{_fmt(m['total_ret'], 1):>9}{_fmt(m['cagr'], 1):>8}"
              f"{_fmt(m['mdd'], 1):>8}{_fmt(m['sharpe'], 2):>7}{m['trades']:>6}"
              f"{_fmt(m['win'], 0):>7}{_fmt(m['turnover'], 1):>7}{_fmt(vs, 1, 'pp'):>8}{mark}")
    print("-" * 108)
    for name, m in benches.items():
        if not m:
            continue
        print(f"{'[벤치]':<5}{name:<31}"
              f"{_fmt(m['final'], 0):>10}{_fmt(m['total_ret'], 1):>9}{_fmt(m['cagr'], 1):>8}"
              f"{_fmt(m['mdd'], 1):>8}{_fmt(m['sharpe'], 2):>7}{'-':>6}{'-':>7}{'-':>7}")


def build_segments(idx: pd.DatetimeIndex, end_i: int) -> list:
    """끝에서부터 252봉(12개월) 단위로 자른다. 앞쪽 자투리(<252봉)는 버린다."""
    segs = []
    hi = end_i
    while len(segs) < SEG_MAX:
        lo = hi - SEG_BARS + 1
        if lo < WARMUP_BARS:
            break
        segs.append((lo, hi))
        hi = lo - 1
    return list(reversed(segs))


def run_segments(engine: RankEngine, close_df: pd.DataFrame, adj_df: pd.DataFrame,
                 segs: list) -> list:
    """구간별 기준선 vs SPY vs QQQ — 3년 성과가 어느 해에서 갈렸는지 격리한다."""
    idx = adj_df.index
    out = []
    for lo, hi in segs:
        m = simulate(BASELINE, engine, close_df, adj_df, lo, hi)
        if not m:
            continue
        row = {"lo": lo, "hi": hi, "from": idx[lo], "to": idx[hi], "m": m}
        for b in BENCH_TICKERS:
            row[b] = buy_hold([b], adj_df, lo, hi)
        spy_ret = (row.get("SPY") or {}).get("total_ret", float("nan"))
        row["excess"] = m["total_ret"] - spy_ret
        out.append(row)
    return out


def print_segments(rows: list) -> None:
    if not rows:
        return
    print(f"\n{'=' * 108}")
    print("■ 12개월 구간별 분해 — 기준선(실제 운용 방식) vs 벤치마크")
    print("=" * 108)
    print(f"{'구간':<26}{'기준선%':>9}{'MDD%':>8}{'거래':>6}{'승률%':>7}"
          f"{'SPY%':>9}{'QQQ%':>9}{'vsSPY':>10}")
    print("-" * 108)
    for r in rows:
        m = r["m"]
        spy = (r.get("SPY") or {}).get("total_ret", float("nan"))
        qqq = (r.get("QQQ") or {}).get("total_ret", float("nan"))
        label = f"{r['from'].date()} ~ {r['to'].date()}"
        print(f"{label:<26}{_fmt(m['total_ret'], 1):>9}{_fmt(m['mdd'], 1):>8}"
              f"{m['trades']:>6}{_fmt(m['win'], 0):>7}"
              f"{_fmt(spy, 1):>9}{_fmt(qqq, 1):>9}{_fmt(r['excess'], 1, 'pp'):>10}")
    print("-" * 108)
    worst = min(rows, key=lambda r: r["excess"] if np.isfinite(r["excess"]) else 1e9)
    best = max(rows, key=lambda r: r["excess"] if np.isfinite(r["excess"]) else -1e9)
    print(f"최악 구간: {worst['from'].date()} ~ {worst['to'].date()} "
          f"(SPY 대비 {_fmt(worst['excess'], 1, 'pp')})")
    print(f"최고 구간: {best['from'].date()} ~ {best['to'].date()} "
          f"(SPY 대비 {_fmt(best['excess'], 1, 'pp')})")
    print("→ 구간별 편차가 크면 3년 평균은 '전략의 실력'이 아니라 '구간 운'에 가깝다.")
    return worst


def run_segment_matrix(engine: RankEngine, close_df: pd.DataFrame, adj_df: pd.DataFrame,
                       segs: list) -> dict:
    """구간 × 설정 전체 매트릭스 — 나쁜 구간을 방어한 설정이 무엇인지 격리한다."""
    out = {}
    for freq in FREQS:
        for swap in SWAPS:
            for sr in SELLRULES:
                for mf in MKTFILTERS:
                    cfg = (freq, swap, sr, mf)
                    row = []
                    for lo, hi in segs:
                        try:
                            row.append(simulate(cfg, engine, close_df, adj_df, lo, hi))
                        except Exception:
                            row.append({})
                    out[cfg] = row
    return out


def print_segment_matrix(matrix: dict, seg_rows: list, bad_k: int = 2) -> list:
    """설정별 구간 수익률 표. 최악 구간 성과 순으로 정렬 — 손실 방지 관점의 랭킹."""
    if not matrix or not seg_rows:
        return []
    labels = [f"{r['from'].strftime('%y/%m')}~{r['to'].strftime('%y/%m')}" for r in seg_rows]
    spy_rets = [(r.get("SPY") or {}).get("total_ret", float("nan")) for r in seg_rows]
    # SPY 대비 열위가 큰 구간 = '나쁜 구간'
    order = sorted(range(len(seg_rows)),
                   key=lambda k: seg_rows[k]["excess"] if np.isfinite(seg_rows[k]["excess"])
                   else 1e9)
    bad_idx = order[:bad_k]

    rows = []
    for cfg, ms in matrix.items():
        rets = [(m or {}).get("total_ret", float("nan")) for m in ms]
        trades = sum((m or {}).get("trades", 0) for m in ms)
        valid = [x for x in rets if np.isfinite(x)]
        if not valid:
            continue
        bad = [rets[k] for k in bad_idx if np.isfinite(rets[k])]
        rows.append({
            "cfg": cfg, "rets": rets, "trades": trades,
            "mean": float(np.mean(valid)), "worst": float(np.min(valid)),
            "bad_mean": float(np.mean(bad)) if bad else float("nan"),
        })
    rows.sort(key=lambda r: -r["worst"])

    print(f"\n{'=' * 118}")
    print("■ 구간 × 설정 매트릭스 — 각 12개월 구간 총수익률(%) · 최악 구간 성과 내림차순")
    print("=" * 118)
    hdr = f"{'주기':<5}{'교체':<11}{'매도룰':<10}{'시장필터':<10}"
    for lb in labels:
        hdr += f"{lb:>14}"
    hdr += f"{'평균':>8}{'최악':>8}{'나쁜2평균':>11}{'총거래':>7}"
    print(hdr)
    print("-" * 118)
    for r in rows:
        cfg = r["cfg"]
        line = f"{_LBL[cfg[0]]:<5}{_LBL[cfg[1]]:<11}{_LBL[cfg[2]]:<10}{_LBL[cfg[3]]:<10}"
        for x in r["rets"]:
            line += f"{_fmt(x, 1):>14}"
        line += (f"{_fmt(r['mean'], 1):>8}{_fmt(r['worst'], 1):>8}"
                 f"{_fmt(r['bad_mean'], 1):>11}{r['trades']:>7}")
        if cfg == BASELINE:
            line += "  ◀ 실제운용"
        print(line)
    print("-" * 118)
    line = f"{'[SPY]':<36}"
    for x in spy_rets:
        line += f"{_fmt(x, 1):>14}"
    print(line)
    print(f"→ '나쁜2평균' = SPY 대비 열위가 컸던 구간 {bad_k}개의 평균. "
          f"여기서 살아남는 설정이 진짜 방어력이 있는 설정이다.")
    print("→ 최근 1구간만 좋고 나머지가 나쁜 설정은 '구간 운'이지 실력이 아니다.")
    return rows


def print_reset_vs_continuous(engine: RankEngine, close_df: pd.DataFrame,
                              adj_df: pd.DataFrame, segs: list, matrix: dict,
                              n_years: int = 3) -> None:
    """매년 $5,000 리셋 vs 연속 운용 — 14pp 격차의 원인이 집중도 드리프트인지 확인."""
    use = segs[-n_years:]
    if len(use) < n_years:
        return
    lo, hi = use[0][0], use[-1][1]
    print(f"\n{'=' * 118}")
    print(f"■ 매년 리셋 vs 연속 운용 ({n_years}년) — 경로 차이의 원인 진단")
    print("=" * 118)
    print(f"{'주기':<5}{'교체':<11}{'매도룰':<10}{'시장필터':<10}"
          f"{'연속%':>9}{'리셋체이닝%':>13}{'격차pp':>9}"
          f"{'연속 평균최대비중%':>20}{'연속 최대비중피크%':>20}")
    print("-" * 118)
    targets = [BASELINE,
               ("weekly", "rebal", "top5", "none"),
               ("monthly", "rebal", "top5", "no_new")]
    for cfg in targets:
        cont = simulate(cfg, engine, close_df, adj_df, lo, hi)
        if not cont:
            continue
        seg_ms = matrix.get(cfg) or []
        chain = 1.0
        ok = True
        for (slo, shi) in use:
            k = next((j for j, (a, b) in enumerate(segs) if (a, b) == (slo, shi)), None)
            m = seg_ms[k] if (k is not None and k < len(seg_ms)) else None
            if not m:
                ok = False
                break
            chain *= (1.0 + m["total_ret"] / 100.0)
        if not ok:
            continue
        chain_ret = (chain - 1.0) * 100.0
        print(f"{_LBL[cfg[0]]:<5}{_LBL[cfg[1]]:<11}{_LBL[cfg[2]]:<10}{_LBL[cfg[3]]:<10}"
              f"{_fmt(cont['total_ret'], 1):>9}{_fmt(chain_ret, 1):>13}"
              f"{_fmt(chain_ret - cont['total_ret'], 1):>9}"
              f"{_fmt(cont.get('maxw_mean'), 1):>20}{_fmt(cont.get('maxw_peak'), 1):>20}")
    print("-" * 118)
    print("→ '교체분만' 은 승자를 계속 태우므로 시간이 갈수록 한 종목 비중이 커진다.")
    print("   평균/피크 최대비중이 20%(=1/5 균등)를 크게 넘으면 격차의 원인은 집중도 드리프트다.")
    print("   '균등재조정' 의 격차가 훨씬 작다면 그 해석이 맞다는 확증이다.")


def print_rebalance_log(m: dict, last_n: int | None = 12, title: str | None = None) -> None:
    """리밸런싱 내역 — last_n=None 이면 전체 출력."""
    log = (m or {}).get("log") or []
    if not log:
        return
    shown = log if last_n is None else log[-last_n:]
    head = title or f"기준선 최근 {len(shown)}회 리밸런싱 (네 이메일과 대조 검증용)"
    print(f"\n▶ {head}")
    print(f"   {'신호일':<12}{'체결일':<12}{'매도':<28}{'매수':<28}{'보유 Top5':<30}{'자산$':>10}")
    for r in shown:
        print(f"   {str(r['sig'].date()):<12}{str(r['exec'].date()):<12}"
              f"{(','.join(r['sold']) or '-'):<28}{(','.join(r['bought']) or '-'):<28}"
              f"{(','.join(r['held']) or '(전량현금)'):<30}{r['equity']:>10,.0f}")


def print_factor_summary(win_label: str, results: dict) -> None:
    """기준선(실제 운용) 대비 한 축씩만 바꿨을 때의 차이 — 인과 해석이 가능한 비교."""
    base = results.get(BASELINE)
    if not base:
        return
    print(f"\n▶ {win_label} · 기준선(주간·교체분만·Top5이탈·필터없음) 대비 1축 변경 효과")
    axes = [(0, FREQS, "리밸런싱 주기"), (1, SWAPS, "교체 방식"),
            (2, SELLRULES, "매도 룰"), (3, MKTFILTERS, "시장 필터")]
    for pos, opts, title in axes:
        lines = []
        for o in opts:
            if o == BASELINE[pos]:
                continue
            cfg = tuple(o if k == pos else BASELINE[k] for k in range(4))
            m = results.get(cfg)
            if not m:
                continue
            lines.append(f"    {_LBL[o]:<10} CAGR {_fmt(m['cagr'], 1):>7}% "
                         f"({_fmt(m['cagr'] - base['cagr'], 1, 'pp'):>8}) · "
                         f"MDD {_fmt(m['mdd'], 1):>7}% ({_fmt(m['mdd'] - base['mdd'], 1, 'pp'):>8}) · "
                         f"거래 {m['trades']:>3}건")
        if lines:
            print(f"  · {title}")
            print("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# Google Sheets 기록
# ══════════════════════════════════════════════════════════════════════════════
# 2026-09-04: `_gs_is_transient` + `_gs` 구현 55줄을 gs_retry.py 로 위임했다.
# run_signal_backtest.py 에 거의 같은 55줄이 또 있었고 gs_retry 와도 정책이
# 겹쳤다 — 같은 로직 세 벌. 자세한 배경은 gs_retry.py 의 '재시도 프로파일' 절.
#
# 예산 62초는 유지한다(PROFILE_BATCH). 이 진단은 유니버스 전체를 훑은 뒤
# 마지막에 Satellite_Backtest 시트에 쓰므로, 그 한 번의 실패가 런 전체를
# 무효로 만든다. gs_retry 기본값 22초는 알림 경로용이라 여기 맞지 않는다.
#
# ⚠️ 의도적 변경 1건: 상태를 못 읽는 예외(google.auth TransportError,
#    ssl.SSLError, RemoteDisconnected 등)를 이전에는 재시도하지 않고 죽였다.
#    이제 gs_retry 정책에 따라 재시도한다 — 버그 수정이다.
#
# 호출부 11곳은 무변경.
def _gs(fn, *args, **kwargs):
    """gspread 호출 재시도 래퍼 (gs_retry SSOT · 배치 프로파일)."""
    return gsr.call(fn, *args, _profile=gsr.PROFILE_BATCH, **kwargs)


def _safe_append_rows(ws, rows, ncols: int) -> None:
    """append_row 계단식 드리프트 회피 — A열 기준 마지막 다음 행에 명시 range update."""
    if not rows:
        return
    rows = [list(r) for r in rows if r is not None]
    existing = _gs(ws.get_all_values) or []
    last_row = 0
    for idx, r in enumerate(existing, start=1):
        if any(str(c).strip() != "" for c in r):
            last_row = idx
    start_row, end_row = last_row + 1, last_row + len(rows)
    try:
        if end_row > ws.row_count:
            _gs(ws.add_rows, end_row - ws.row_count + 50)
    except Exception:
        pass
    last_col = chr(ord("A") + max(0, ncols - 1))
    _gs(ws.update, rows, range_name=f"A{start_row}:{last_col}{end_row}",
        value_input_option="USER_ENTERED")


def write_results(all_rows: list) -> None:
    if not GSPREAD_KEY_JSON:
        print("\n[INFO] GSPREAD_KEY 없음 — 시트 기록 생략(콘솔 출력만).")
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GSPREAD_KEY_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"])
        gc = gspread.authorize(creds)
        sh = _gs(gc.open, _SPREADSHEET_TITLE)
        titles = [w.title for w in _gs(sh.worksheets)]
        last_col = chr(ord("A") + len(_RESULT_COLS) - 1)
        if _RESULT_WORKSHEET in titles:
            ws = _gs(sh.worksheet, _RESULT_WORKSHEET)
            if (_gs(ws.row_values, 1) or []) != _RESULT_COLS:
                _gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1",
                    value_input_option="USER_ENTERED")
                print(f"[INFO] {_RESULT_WORKSHEET} 헤더 갱신")
        else:
            ws = _gs(sh.add_worksheet, title=_RESULT_WORKSHEET,
                     rows=2000, cols=len(_RESULT_COLS))
            _gs(ws.update, [_RESULT_COLS], range_name=f"A1:{last_col}1",
                value_input_option="USER_ENTERED")
        _safe_append_rows(ws, all_rows, ncols=len(_RESULT_COLS))
        print(f"[OK] {_RESULT_WORKSHEET} 시트에 {len(all_rows)}행 기록")
    except Exception as exc:
        print(f"[WARN] 시트 기록 실패(콘솔 결과는 유효): {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# 자체검증 (네트워크 불필요)
# ══════════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    print("=" * 70)
    print("자체검증 — 합성 데이터로 엔진 기계적 정합성 확인")
    print("=" * 70)
    fails = []

    # 1) trailing return
    v = np.array([100.0] * 30 + [110.0])
    r = _trailing_return(v, 21)
    if abs(r - 10.0) > 1e-9:
        fails.append(f"trailing_return 오차: {r}")

    # 2) 합성 패널 — 한 종목만 계속 상승, 나머지는 평탄
    n = 400
    idx = pd.bdate_range("2023-01-02", periods=n)
    pool = fx.satellite_candidate_pool()
    tickers = sorted({t for lst in pool.values() for t in lst} | {"SPY", "QQQ"})
    close = pd.DataFrame(index=idx)
    for tk in tickers:
        close[tk] = 100.0
    winner = "XLK"
    close[winner] = 100.0 * (1.0005 ** np.arange(n))     # 꾸준한 우상향
    close["SPY"] = 100.0 * (1.0002 ** np.arange(n))
    adj = close.copy()

    eng = RankEngine(close)
    champs = eng.rank_at(idx[-1])
    if not champs or champs[0]["ticker"] != winner:
        fails.append(f"랭킹 1위가 {winner} 가 아님: {[c['ticker'] for c in champs[:3]]}")

    # 3) 섹터당 1개 제약
    secs = [c["sector"] for c in champs]
    if len(secs) != len(set(secs)):
        fails.append("같은 섹터가 2개 이상 랭킹에 존재")

    # 4) 무비용 가정 시 시뮬레이터 ≈ 매수후보유
    global SLIPPAGE
    old_slip = SLIPPAGE
    SLIPPAGE = 0.0
    try:
        m = simulate(BASELINE, eng, close, adj, start_i=200, end_i=n - 1)
        if not m:
            fails.append("시뮬레이터가 결과를 내지 못함")
        else:
            # 평탄 종목이 대부분이라 최종 자산은 자본금 이상이어야 하고 폭발하면 안 됨
            if not (CAPITAL * 0.95 <= m["final"] <= CAPITAL * 1.5):
                fails.append(f"최종 자산 비정상: {m['final']:.2f}")
            if m["mdd"] > 0.01:
                fails.append(f"MDD 부호 이상: {m['mdd']}")
        bh = buy_hold(["SPY"], adj, 200, n - 1)
        expect = CAPITAL * float(adj["SPY"].iloc[n - 1] / adj["SPY"].iloc[200])
        if not bh or abs(bh["final"] - expect) > 1.0:
            fails.append(f"buy_hold 검증 실패: {bh.get('final')} vs {expect:.2f}")
    finally:
        SLIPPAGE = old_slip

    # 5) 슬리피지가 성과를 낮추는 방향인지
    SLIPPAGE_TEST = 0.01
    old = SLIPPAGE
    m_free = simulate(BASELINE, eng, close, adj, 200, n - 1)
    SLIPPAGE = SLIPPAGE_TEST
    m_cost = simulate(BASELINE, eng, close, adj, 200, n - 1)
    SLIPPAGE = old
    if m_free and m_cost and m_cost["final"] > m_free["final"]:
        fails.append("슬리피지를 키웠는데 성과가 좋아짐")

    # 6) 신호일/체결일 분리 확인
    sd = signal_dates(idx, "weekly")
    if any(pd.Timestamp(d).weekday() != 4 for d in sd[1:-1]):
        fails.append("주간 신호일이 금요일이 아님")

    # 7) 12개월 구간 분해 — 겹치지 않고 워밍업 침범 없이 끝에서부터 잘리는가
    segs = build_segments(idx, n - 1)
    if not segs:
        fails.append("구간 분해 결과가 비어 있음")
    else:
        if segs[-1][1] != n - 1:
            fails.append("마지막 구간이 데이터 끝에 붙어 있지 않음")
        if any(hi - lo + 1 != SEG_BARS for lo, hi in segs):
            fails.append("구간 길이가 252봉이 아님")
        if any(segs[k][1] >= segs[k + 1][0] for k in range(len(segs) - 1)):
            fails.append("구간이 서로 겹침")
        if segs[0][0] < WARMUP_BARS:
            fails.append("구간이 워밍업 영역을 침범함")

    # 8) 수수료를 키우면 성과가 나빠지는가
    global COMMISSION_PER_TRADE
    old_c = COMMISSION_PER_TRADE
    m_free2 = simulate(BASELINE, eng, close, adj, 200, n - 1)
    COMMISSION_PER_TRADE = 20.0
    m_fee = simulate(BASELINE, eng, close, adj, 200, n - 1)
    COMMISSION_PER_TRADE = old_c
    if m_free2 and m_fee and m_fee["final"] >= m_free2["final"]:
        fails.append("건당 수수료를 $20 로 올렸는데 성과가 나빠지지 않음")

    # 9) 배당조정 패널이 성과에 반영되는가 (adj 를 1% 더 올리면 최종자산도 올라야)
    adj_up = adj * 1.0
    adj_up.iloc[-1] = adj_up.iloc[-1] * 1.01
    m_up = simulate(BASELINE, eng, close, adj_up, 200, n - 1)
    if m_up and m_free2 and m_up["final"] <= m_free2["final"]:
        fails.append("성과 패널(adj)이 최종 자산에 반영되지 않음")

    # 10) [회귀] risk-off 로 슬롯이 비어도 균등재조정이 몰빵하지 않는가
    #     버그 이력: tgt = equity/len(targets) 였을 때 생존 1종목이면 비중 100% 가 됐다.
    #     ⚠️ 랭킹이 실제로 교체돼야 슬롯이 비므로, 이 케이스만 랜덤워크 패널을 쓴다.
    rng2 = np.random.default_rng(1234)
    close_ro = pd.DataFrame(index=idx)
    for tk in tickers:
        close_ro[tk] = 100.0 * np.exp(np.cumsum(
            rng2.normal(rng2.normal(0.0, 0.0006), 0.018, n)))
    # SPY 는 전반 상승(risk_on → 포지션 구축) 후 하락(risk_off → 슬롯 공백)이어야
    # 문제 경로를 밟는다. 처음부터 하락시키면 아예 매수가 없어 테스트가 무효가 된다.
    turn = int(n * 0.67)
    close_ro["SPY"] = 100.0 * np.exp(np.cumsum(np.concatenate(
        [np.full(turn, 0.0012), np.full(n - turn, -0.0025)])))
    adj_ro = close_ro.copy()
    eng_ro = RankEngine(close_ro)
    exercised = False
    for mf in ("no_new", "all_cash"):
        m_ro = simulate(("weekly", "rebal", "top5", mf), eng_ro, close_ro, adj_ro,
                        250, n - 1)
        if not m_ro:
            continue
        # 슬롯이 실제로 비는 구간이 발생했는지 확인 (테스트가 경로를 밟았는지 검증)
        if any(len(r["held"]) < SLOTS for r in (m_ro.get("log") or [])):
            exercised = True
        peak = m_ro.get("maxw_peak", np.nan)
        if np.isfinite(peak) and peak > 40.0:
            fails.append(f"risk-off 균등재조정 최대비중 과다({mf}): "
                         f"{peak:.1f}% — 슬롯 분모가 깨졌다")
    if not exercised:
        fails.append("회귀 테스트가 risk-off 슬롯 공백 경로를 밟지 못함 — 테스트 자체가 무효")

    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 전 항목 통과 (수익률·섹터제약·무비용정합·슬리피지방향·신호일·"
          "구간분해·수수료방향·배당패널반영·risk-off슬롯비중)")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    t0 = time.time()
    print("=" * 108)
    print(f"🛰️  위성 섹터 로테이션 백테스트 — {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 108)

    pool = fx.satellite_candidate_pool()
    universe = sorted({t for lst in pool.values() for t in lst})
    fetch_list = sorted(set(universe) | set(BENCH_TICKERS))
    print(f"[STEP 1] 후보 풀 {len(universe)}개 + 벤치 {len(BENCH_TICKERS)}개 = "
          f"{len(fetch_list)}종목 × 2엔드포인트(원종가·배당조정) 수집 중...")
    # 창을 먼저 확정해 로그로 남긴다. 실제로 몇 봉을 요청했는지가 결과 해석의
    # 전제인데, v2.8 까지는 어디에도 남지 않아 사후에 확인할 방법이 없었다.
    _win_days = _window_days_for(HISTORY_BARS)
    print(f"[STEP 1] 조회 창 — 요구 {HISTORY_BARS}봉 → {_win_days}달력일 "
          f"({fx.hist_range_params(_win_days).lstrip('&').replace('&', ' ')})")
    raw, adjmap, fallback, _reasons, _failed = _batch_fetch(fetch_list, bars=HISTORY_BARS)

    _n = len(fetch_list)
    _rate = (len(raw) / _n) if _n else 1.0
    print(f"[STEP 1] 원종가 확보 {len(raw)}/{_n}종목 ({_rate * 100:.1f}%) · "
          f"배당조정 확보 {len(raw) - len(fallback)}/{_n}종목")
    if _reasons:
        print("[STEP 1] 사유별 — " + " · ".join(
            f"{ep}:{k}={v}" for (ep, k), v in
            sorted(_reasons.items(), key=lambda kv: -kv[1])))
    print("[STEP 1] " + fh.fmp_stats_line())
    missing = sorted(set(fetch_list) - set(raw))
    if missing:
        print(f"[WARN] 이력 미확보: {missing}")
    if _failed:
        _head = ", ".join(f"{t}/{e}({k})" for t, e, k in sorted(_failed)[:20])
        print(f"[STEP 1] 탈락 {len(_failed)}건 — {_head}"
              + (" …" if len(_failed) > 20 else ""))
    if fallback:
        print(f"[WARN] 배당조정 실패 → 원 종가 폴백 {len(fallback)}종목: {fallback[:15]}")
    if "SPY" not in raw:
        print("[ERROR] SPY 이력 확보 실패 — 중단")
        return 1

    # ── v2.8 게이트: 부분 유니버스는 시트에 넣지 않는다 ─────────────────────
    # 후보 풀이 줄면 Top5 를 고르는 모집단 자체가 달라진다. 그런데 결과 행의
    # 모양은 온전한 run 과 구별되지 않는다 — 그래서 **쓰기 전에** 막는다.
    if _rate < MIN_FETCH_RATE:
        print(f"[ABORT] 원종가 페치 성공률 {_rate * 100:.1f}% < 임계 "
              f"{MIN_FETCH_RATE * 100:.1f}% — 시트에 기록하지 않고 중단한다.")
        print("[ABORT] 위 '사유별' 을 볼 것. rate_limited 가 많으면 "
              "FMP_RATE_LIMIT_PER_MIN 을 낮추고, exception 이 많으면 "
              "_FMP_TIMEOUT 을 늘린다.")
        return 1

    # 배당조정은 별도로 판정한다. "empty"(원래 그 시리즈가 없음)는 다시 돌려도
    # 같지만, 인프라성 실패는 다시 돌리면 달라진다. 둘을 섞어 세면 게이트가
    # 영구 빨간불이 되거나 반대로 진짜 오염을 놓친다.
    _adj_infra = sum(v for (ep, k), v in _reasons.items()
                     if ep == "dividend-adjusted" and k in _INFRA_KINDS)
    if _n and (_adj_infra / _n) > (1.0 - MIN_FETCH_RATE):
        print(f"[ABORT] 배당조정 인프라성 실패 {_adj_infra}/{_n}건 — 성과 기준이 "
              f"종목마다 뒤섞인다. 시트에 기록하지 않고 중단한다.")
        return 1

    close_df, adj_df = build_panels(raw, adjmap)
    idx = close_df.index
    print(f"[INFO] 캘린더 {len(idx)}봉 · {idx[0].date()} ~ {idx[-1].date()}")

    # 배당 반영이 실제로 작동했는지 검증 — 총수익/가격수익 괴리를 연율로 환산
    gaps = []
    for tk in sorted(raw):
        c, a = close_df[tk].dropna(), adj_df[tk].dropna()
        if len(c) < 260 or len(a) < 260:
            continue
        n = min(len(c), len(a))
        cr = float(c.iloc[-1] / c.iloc[-n])
        ar = float(a.iloc[-1] / a.iloc[-n])
        if cr > 0 and ar > 0:
            gaps.append((ar / cr) ** (252.0 / n) - 1.0)
    med_gap = float(np.median(gaps)) * 100.0 if gaps else 0.0
    div_basis = "dividend-adjusted(배당재투자)" if med_gap > 0.15 else "close(배당미반영)"
    # 폴백이 있었다면 그 사실을 **행 자체에** 남긴다. 로그는 사라지지만 시트는
    # 남는다 — 나중에 이 행을 다시 볼 때 성과 기준이 균일했는지 알아야 한다.
    if fallback:
        div_basis += f" · 혼합({len(fallback)}종목 close 대체)"
    print(f"[INFO] 성과 기준: {div_basis} · 배당 기여 중앙값 ≈ 연 {med_gap:.2f}%p")
    if med_gap <= 0.15:
        print("[WARN] 배당조정 시리즈가 원 종가와 사실상 동일 — 배당이 반영되지 않았다.")
    spy_gap = 0.0
    try:
        c, a = close_df["SPY"].dropna(), adj_df["SPY"].dropna()
        n = min(len(c), len(a))
        spy_gap = (((float(a.iloc[-1] / a.iloc[-n])) / (float(c.iloc[-1] / c.iloc[-n])))
                   ** (252.0 / n) - 1.0) * 100.0
        print(f"[INFO] 참고 — SPY 배당 기여 연 {spy_gap:.2f}%p "
              f"(위성 풀 중앙값 {med_gap:.2f}%p 와의 차이가 상대비교에 미치는 영향)")
    except Exception:
        pass

    max_eval = len(idx) - WARMUP_BARS - ENTRY_LAG_DAYS
    print(f"[INFO] 워밍업 {WARMUP_BARS}봉 제외 후 평가 가능 최대 {max_eval}거래일 "
          f"(≈{max_eval / 252:.1f}년)")

    engine = RankEngine(close_df)
    end_i = len(idx) - 1
    all_rows = []
    run_date = datetime.now(_ET).strftime("%Y-%m-%d")

    for win_label, win_bars in WINDOWS.items():
        start_i = end_i - win_bars + 1
        if start_i < WARMUP_BARS:
            print(f"\n[SKIP] {win_label} — 이력 부족(필요 {win_bars + WARMUP_BARS}봉 / "
                  f"보유 {len(idx)}봉)")
            continue
        print(f"\n[STEP 2] {win_label} 시뮬레이션 중... "
              f"({idx[start_i].date()} ~ {idx[end_i].date()})")

        results = {}
        for freq in FREQS:
            for swap in SWAPS:
                for sr in SELLRULES:
                    for mf in MKTFILTERS:
                        cfg = (freq, swap, sr, mf)
                        try:
                            results[cfg] = simulate(cfg, engine, close_df, adj_df,
                                                    start_i, end_i)
                        except Exception as exc:
                            print(f"[WARN] {cfg} 실패: {exc}")
                            results[cfg] = {}

        benches = {}
        for b in BENCH_TICKERS:
            benches[b] = buy_hold([b], adj_df, start_i, end_i)
        # 초기 Top5 고정 보유 — 로테이션이 값을 더했는지의 직답
        try:
            first_sig = [d for d in signal_dates(idx, "weekly")
                         if start_i <= idx.get_loc(d) <= end_i][0]
            top5_0 = [c["ticker"] for c in engine.rank_at(first_sig)[:SLOTS]]
            benches[f"초기Top5 고정({','.join(top5_0)})"] = buy_hold(
                top5_0, adj_df, idx.get_loc(first_sig) + ENTRY_LAG_DAYS, end_i)
        except Exception as exc:
            print(f"[WARN] 초기 Top5 벤치 실패: {exc}")

        print_window_table(win_label, results, benches)
        print_factor_summary(win_label, results)
        if win_label == list(WINDOWS)[-1]:
            print_rebalance_log(results.get(BASELINE))

        spy_cagr = benches.get("SPY", {}).get("cagr", float("nan"))
        for cfg, m in results.items():
            if not m:
                continue
            all_rows.append([
                run_date, win_label, _LBL[cfg[0]], _LBL[cfg[1]], _LBL[cfg[2]], _LBL[cfg[3]],
                str(m["start"].date()), str(m["end"].date()),
                round(CAPITAL, 2), round(m["final"], 2), round(m["total_ret"], 2),
                round(m["cagr"], 2), round(m["mdd"], 2),
                round(m["sharpe"], 3) if np.isfinite(m["sharpe"]) else "",
                m["trades"], round(m["win"], 1) if np.isfinite(m["win"]) else "",
                round(m["turnover"], 2), round(m["slip"], 2),
                round(m["cagr"] - spy_cagr, 2) if np.isfinite(spy_cagr) else "",
                div_basis,
            ])

    # ── STEP 3: 12개월 구간별 분해 — 3년 성과가 어느 해에서 갈렸는지 격리 ──────
    segs = build_segments(idx, end_i)
    if segs:
        print(f"\n[STEP 3] 12개월 구간 {len(segs)}개 분해 중...")
        seg_rows = run_segments(engine, close_df, adj_df, segs)
        worst = print_segments(seg_rows)
        if worst:
            print_rebalance_log(
                worst["m"], last_n=None,
                title=f"최악 구간 전체 리밸런싱 내역 "
                      f"({worst['from'].date()} ~ {worst['to'].date()}) — 실패 모드 진단")
        for r in seg_rows:
            m = r["m"]
            spy_c = (r.get("SPY") or {}).get("cagr", float("nan"))
            all_rows.append([
                run_date, f"구간 {r['from'].date()}~{r['to'].date()}",
                _LBL[BASELINE[0]], _LBL[BASELINE[1]], _LBL[BASELINE[2]], _LBL[BASELINE[3]],
                str(m["start"].date()), str(m["end"].date()),
                round(CAPITAL, 2), round(m["final"], 2), round(m["total_ret"], 2),
                round(m["cagr"], 2), round(m["mdd"], 2),
                round(m["sharpe"], 3) if np.isfinite(m["sharpe"]) else "",
                m["trades"], round(m["win"], 1) if np.isfinite(m["win"]) else "",
                round(m["turnover"], 2), round(m["slip"], 2),
                round(m["cagr"] - spy_c, 2) if np.isfinite(spy_c) else "",
                div_basis,
            ])

        # ── STEP 4: 구간 × 설정 매트릭스 ────────────────────────────────────
        print(f"\n[STEP 4] 구간 {len(segs)}개 × 설정 24개 매트릭스 계산 중...")
        matrix = run_segment_matrix(engine, close_df, adj_df, segs)
        mrows = print_segment_matrix(matrix, seg_rows)
        print_reset_vs_continuous(engine, close_df, adj_df, segs, matrix)

        for r in (mrows or []):
            cfg = r["cfg"]
            for (lo, hi), m in zip(segs, matrix[cfg]):
                if not m:
                    continue
                all_rows.append([
                    run_date, f"매트릭스 {idx[lo].date()}~{idx[hi].date()}",
                    _LBL[cfg[0]], _LBL[cfg[1]], _LBL[cfg[2]], _LBL[cfg[3]],
                    str(m["start"].date()), str(m["end"].date()),
                    round(CAPITAL, 2), round(m["final"], 2), round(m["total_ret"], 2),
                    round(m["cagr"], 2), round(m["mdd"], 2),
                    round(m["sharpe"], 3) if np.isfinite(m["sharpe"]) else "",
                    m["trades"], round(m["win"], 1) if np.isfinite(m["win"]) else "",
                    round(m["turnover"], 2), round(m["slip"], 2), "", div_basis,
                ])

    # 랭킹 모집단 = 후보 풀 ∩ 실제 확보분. RankEngine 이 close_df.columns 로
    # 거르므로 이것이 Top5 선정에 실제로 쓰인 집합이다(벤치 SPY/QQQ 는 제외).
    _uhash = universe_hash(set(raw) & set(universe))
    print(f"[INFO] 랭킹 후보 {len(set(raw) & set(universe))}종목 · 지문 {_uhash}")
    all_rows = [list(r) + [_uhash] for r in all_rows]

    write_results(all_rows)

    print("\n" + "=" * 108)
    print("⚠️  해석 주의 — 이 숫자는 상한선이다")
    print("   1) 후보 풀 56개는 2026년 현재 시점에 고른 목록 → 미래를 아는 풀(선택 편향).")
    print("   2) FMP 이력 5년 한도로 2020 코로나·2022 초입 하락장이 데이터에 없다.")
    print("   3) 이 룰의 실거래 기록은 아직 없다 — 리밸런싱 로그가 과거 실계좌와 겹쳐 보여도")
    print("      그건 검증이 아니다. 진짜 대조는 앞으로 쌓일 주차 기록으로만 가능하다.")
    print("   → 절대 수익률이 아니라 '조건 간 상대 비교'만 신뢰할 것.")
    # gs_retry 위임 확인용 — 이 줄이 없으면 gs_retry.py 락스텝 업로드 누락.
    print("[GS] " + gsr.stats_line())
    print(f"[DONE] {time.time() - t0:.0f}초 소요")
    print("=" * 108)
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        sys.exit(main() or 0)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
