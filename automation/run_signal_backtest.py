# -*- coding: utf-8 -*-
"""
run_signal_backtest.py
──────────────────────
GitHub Actions 자동 실행: 레짐 엔진 신호 자기검증(워크포워드 백테스트). 월 1회/수동.

목적
----
라이브와 *동일한* regime_core.analyze_ticker 를 과거 시점마다(미래 훔쳐보기 없이) 호출하여,
verdict(entry/wait/overheat/trend_break/avoid)가 켜진 시점 이후 실제로 어떻게 됐는지
(+5/+20/+60일 수익 · SPY 대비 초과수익 · MFE · MAE)를 버킷별로 집계한다.

  → "🎯 entry 가 정말 돈이 됐나? 시장(SPY) 대비 알파가 있나? entry>wait>overheat>avoid 로 갈리나?"

v2.9 변경 (2026-08-27 · integrated_sell_verdict SSOT 정합)
---------------------------------------------------------
`_pos_label_at` 이 `gap_ma200_pct` 를 넘기지 않아, 200일선 이탈에 **일괄 4.0점**
(🔴 청산 문턱)을 주는 보수적 폴백을 타고 있었다. 프로덕션(app.py:10834 ·
regime_core.position_sell_verdict:2407)은 넘기므로 이격 비례 2.0~4.0 램프(3E)를 탄다.

  → 백테스트가 프로덕션보다 **체계적으로 매도에 적극적**이었다.

실측(2026-08-27 diag_sell_verdict [4] · 200일선 아래 21종목):

    이격 0~-4%    13종목(61.9%)   프로덕션 2.0~3.0  vs  백테스트 4.0
    이격 -4~-8%    1종목( 4.8%)   프로덕션 3.0~4.0  vs  백테스트 4.0
    이격 -8% 초과   7종목(33.3%)   양쪽 4.0 (포화) ✅

즉 200일선 아래 종목의 약 3분의 2에서 판정이 갈렸다. 영향 범위는
`Signal_Backtest_RT`(왕복) 시트뿐 — entry/wait/overheat 표는 안 움직인다.
청산이 **늦어지는** 방향이다.

왜 몇 달간 안 보였나: `_pos_label_at` 독스트링이 "(SSOT 그대로)"라고 **주장만**
하고 있었고 그 주장을 검사하는 것이 없었다. 주석은 계약을 강제하지 않는다.
→ `diag_fmp_ssot.py` **A4** 가 이제 `integrated_sell_verdict` 호출부를 저장소
   전역으로 정적 강제한다(누락 시 하드 실패).

설계 메모: gap 은 `_position_features` 에서 미리 계산하지 않고 `_pos_label_at`
  안에서 만든다. 미리 계산하면 분자(가격)가 인자 `price` 에서, 분모(ma200)가
  feats 배열에서 — 서로 다른 경로로 오게 되어, 호출부가 다른 price 를 넘기는
  순간 `dd` 와 `gap` 이 조용히 갈라진다. feats 는 `ma200` 원값만 내보낸다.

v1.5 변경 (ETF / 개별주 분리 집계)
----------------------------------
유니버스 413종목 중 ETF_Universe 가 343개(83%)라 기존 집계는 사실상 'ETF 성적표'였다.
그러나 워치리스트 알림은 주로 개별주에 쓰인다 → 두 모집단을 섞으면 결론이 오염된다.

이제 Segment 열로 all / etf / stock 을 분리 집계한다(모드 × 세그먼트 = 6개 표).
분류 기준: ETF_Universe 시트에 있으면 etf, 아니면 stock.
  ※ 워치리스트/포트폴리오에만 있고 ETF_Universe 에 없는 ETF(예: 계좌 코어 ETF)는
    stock 으로 잡힐 수 있다. 정확도를 높이려면 해당 티커를 ETF_Universe 에 넣으면 된다.

v1.4 변경 (실제 알림 기준 모드 추가)
------------------------------------
기존 집계는 화면 판정(timing.code)이 확정된 '모든 날'을 셌다. 그러나 실제 이메일은
regime_core.evaluate_alert_transitions 상태머신(2일 확정 + 발동 후 재무장)을 통과한
날에만 나간다. 같은 entry 구간이 30일 이어져도 메일은 1통이다 → 두 모집단은 다르다.

이제 한 번의 워크포워드에서 두 모드를 동시에 집계한다:
  - Mode="verdict" : 화면 판정 기준 (기존 v1.1~1.3 로직 그대로 — 판별력 진단용)
  - Mode="alert"   : 라이브 알림과 *동일한* 상태머신 기준 (실전 성적표)
                     entry 발동 시 build_watchlist_plan 으로 R:R 게이트까지 재현해
                     alert_entry_pass / alert_entry_skip 으로 분리 → 게이트 효용 측정.
분석(analyze_ticker) 호출은 날짜당 1회로 공유하므로 실행 시간은 거의 늘지 않는다.

주의: 워치리스트 행별 사용자 설정(목표 매수가·RSI·200일선)은 과거 재현이 불가능하므로
watch 이벤트는 발동하지 않는다. 즉 alert 모드는 '시스템이 만드는 알림'만 측정한다.

v1.3 변경 (Sheets 일시 장애 내성)
--------------------------------
- gspread 호출 전부를 지수 백오프 재시도(`_gs`)로 감쌌다. 503/500/502/504/429 및
  네트워크 예외는 최대 6회(2·4·8·16·32·60초 + 지터, 누적 ~2분) 재시도한다.
  이유: 월 1회 무인 실행이라 일시적 503 한 번에 한 달치 결과가 통째로 날아간다.
  일간 워크플로는 다음날 자기복구되지만 이 잡은 그렇지 않다.
- 401/403(인증)·404(시트 없음) 같은 영구 오류는 재시도하지 않고 즉시 실패한다.

v1.2 변경 (진입 시점 현실화)
---------------------------
- **진입가를 신호일 종가 → 신호일 +ENTRY_LAG_DAYS(=1) 거래일 종가로 이동.**
  이유: 신호는 장 마감 종가로 확정되고 알림 메일은 그 *후* 16:00 ET 에 발송된다.
  즉 `close[t]` 는 구조적으로 체결 불가능한 가격이라 기존 집계는 실현 불가능한
  성과를 측정했다. 이제 실제로 잡을 수 있는 최초 가격으로 측정한다.
- forward-return / MFE·MAE / SPY 초과수익 모두 '진입 봉' 기준으로 재정렬
  (보유 h거래일 = 진입일로부터 h일). SPY 진입가도 같은 봉의 종가 → 알파 비교 정합.
- 결과 시트에 `Entry_Rule` 열 추가 → 구/신 규칙 행이 한 시트에 섞여도 구분 가능.

v1.1 변경
---------
- 초과수익(excess): 각 이벤트 +Nd 수익에서 SPY 동일 캘린더창 수익을 빼 베타 제거 → 알파 측정.
- 디플랩(de-flap): raw verdict 가 confirm_days(2) 연속 유지된 그날만 1회 이벤트로 확정,
  같은 code 는 cooldown_days(5) 내 재기록 금지 → 경계 진동(flapping)으로 인한 중복 폭증 제거.

흐름
----
  [1] 유니버스: ETF_Universe + Watchlist + Portfolios (합집합, 중복 제거)
  [2] SPY + 유니버스 장기 일봉 fetch (FMP /stable historical-price-eod/full)
  [3] 티커마다 워크포워드: hist[:D] 슬라이스 → analyze_ticker → 2일 확정 시 이벤트
      forward-return(+5/+20/+60일) · SPY 대비 초과수익 · MFE/MAE(20일창) 측정
  [4] 버킷별 집계: 이벤트수 · 승률 · 평균/중앙 수익 · MFE/MAE · 초과20d 평균 · 초과승률
  [5] Signal_Backtest 시트에 'run당 × 버킷당 1행' append (헤더 불일치 시 자동 갱신)

설계 메모
---------
- 무결성: 분류엔 D 이전 데이터만(엄격 슬라이스). 최소 220봉 선행. 미래 부족분 NaN.
- 실행 가능성: 분류일(t)과 진입일(t+ENTRY_LAG_DAYS)을 분리. 분류는 t 까지 데이터만 쓰고,
  진입가는 t 이후 봉 → 미래 훔쳐보기 없이 '메일 받고 다음날 매수' 흐름을 그대로 재현.
- SSOT: regime_core 를 '소비'만(재구현 금지). app.py·run_watchlist_alerts 와 동일 판정.
- 순수 엔진(_forward_metrics / walk_forward_events / aggregate_events)은 numpy/pandas 만 의존 →
  FMP·시트 없이 단위 테스트 가능(analyze_fn 주입). I/O·main 은 하단 분리.
- MFE/MAE 는 절대값(사이징·R:R 의 손절/목표 거리 입력). 초과수익은 점수익(5/20/60d)에만.

실행 주기: 월 1회 또는 workflow_dispatch 수동.
"""

from __future__ import annotations

import os
import sys
import json
import time
import concurrent.futures
import hashlib
from datetime import datetime

import numpy as np
import pandas as pd
import pytz

# ── repo root 를 sys.path 에 추가 → regime_core(app.py와 동일 모듈) import ──────
#    (automation/ 하위에 두는 전제: dirname(dirname(file)) = 레포 루트)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import regime_core as rc  # noqa: E402
import fmp_http as fh     # noqa: E402
import fmp_extras as fx   # noqa: E402
import gs_retry as gsr    # noqa: E402  Sheets 재시도 SSOT
#   ⚠️ v2.9: 조회 창(봉수 → 달력일) 환산 정책은 fmp_extras 가 유일 소유자다.
#      여기서 0.6871 이나 창 상수를 복제하면 정책이 두 벌이 되고, 한쪽만
#      갱신됐을 때 창이 조용히 짧아진다 — 그 실패는 에러 로그를 남기지 않는다.
#   ⚠️ v2.8: FMP 호출은 반드시 fmp_http 를 거친다. 원시 requests.get 은
#      레이트리밋을 우회해 분당 300 을 넘기고, 초과분이 429 → 빈 DataFrame 으로
#      조용히 사라진다. 2026-08-26 진단: 474종목 중 174종목이 몇 주간 무성 탈락
#      (로그에 '299/474' 로만 남았다. 299 = 300/분 − SPY 1콜).


# ──────────────────────────────────────────────────────────────────────────
# 환경변수 (기존 run_*.py 와 동일 시크릿). 테스트/CI 에서 import 만 해도 깨지지 않도록 .get 사용.
# ──────────────────────────────────────────────────────────────────────────
FMP_API_KEY      = os.environ.get("FMP_API_KEY", "")
GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")

# ── 상수 ───────────────────────────────────────────────────────────────────────
_KST = pytz.timezone("Asia/Seoul")
_ET  = pytz.timezone("America/New_York")

_SPREADSHEET_TITLE = "Quant_DB"

# 유니버스 소스 (다른 run_*.py 와 lockstep 한 시트명/컬럼 위치)
_ETF_UNIVERSE_WORKSHEET = "ETF_Universe"   # A열 = Ticker
_WATCHLIST_WORKSHEET    = "Watchlist"      # col idx 1 = Ticker  (_WL_COLS[1])
_PORTFOLIO_WORKSHEET    = "Portfolios"     # col idx 2 = Ticker  ([ID,Account,Ticker,...])

# 결과 시트
_RESULT_WORKSHEET = "Signal_Backtest"
# v1.6: 왕복(round-trip) 결과는 열 구조가 달라 별도 시트로 분리(기존 행 보존)
_RT_WORKSHEET = "Signal_Backtest_RT"
_RT_COLS = [
    "Run_Date", "History_Start", "History_End", "Universe_Size", "Mode", "Segment", "Year",
    "Trades", "Closed", "OpenAtEnd_Pct", "WinRate", "Avg_R", "Median_R",
    "Avg_Ret_Pct", "Median_Ret_Pct", "Avg_Hold_Days", "EarlyExit_Pct",
    "Excess_vs_SPY_Pct", "Top_Exit_Reason",
    "Universe_Hash",                       # v2.8: 세그먼트 티커 집합 지문
]
_RESULT_COLS = [
    "Run_Date", "History_Start", "History_End", "Universe_Size", "Verdict",
    "Event_Count", "WinRate_20d", "Ret_5d_Mean", "Ret_20d_Mean", "Ret_20d_Median",
    "Ret_60d_Mean", "MFE_20d_Mean", "MAE_20d_Mean",
    "Excess_20d_Mean", "ExcessWin_20d",   # v1.1: SPY 대비 알파
    "Entry_Rule",                          # v1.2: 진입가 규칙(close[t+N]) — 구/신 행 구분
    "Mode",                                # v1.4: verdict(화면 판정) | alert(실제 이메일)
    "Segment",                             # v1.5: all | etf | stock
    "Confirm_Days",                        # v2.7: 확정 일수 — 스윕 행 구분(Entry_Rule 과 같은 목적)
    "Universe_Hash",                       # v2.8: 이 행 세그먼트의 티커 집합 지문
]

_FMP_BASE    = "https://financialmodelingprep.com/stable"
_FMP_TIMEOUT = 20            # v2.8: 7(fmp_http 기본)·8 은 1,255봉 페이로드에 짧다.
#   레이트리밋만 고치고 타임아웃을 그대로 두면 탈락 사유만 바뀔 뿐 결과가 같다.
_FETCH_WORKERS = 8           # FMP Starter 300 req/min 여유

# 백테스트 파라미터 (튜닝 한 곳)
HORIZONS       = (5, 20, 60)   # forward-return 측정 거래일
MFE_WINDOW     = 20            # MFE/MAE 측정 창(거래일)
MIN_PRIOR_BARS = 220           # 200일선 산출 위한 최소 선행 봉수
TEST_LOOKBACK  = 934           # v2.5: 평가 구간(거래일 ≈ 3.7년) — FMP 이력 한도에 맞춘 상한.
#   진단(diag_fmp_depth) 결과: FMP 계정은 limit 값과 무관하게 항상 1255봉(정확히 5년,
#   롤링)만 반환한다. 500 을 요청하든 5000 을 요청하든 시작일이 동일했다.
#   ⚠️ v2.9(2026-09-03): 그래서 이 파일은 **limit 을 더 이상 보내지 않는다.**
#      `from`/`to` 달력일 창으로 바꿨다(fmp_extras 가 환산 정책 소유). 이유는
#      스타일이 아니다 — limit 이 남아 있으면 이 상수를 올렸을 때 URL 만 바뀌고
#      데이터는 그대로여서 **에러도 경고도 없이 평가 구간이 안 늘어난다.**
#      v2.4 에서 1260→2140 으로 올렸을 때 실제로 그렇게 됐고, 원인을 오진해
#      아래 v2.8 정정까지 두 번을 돌았다. 같은 함정을 구조적으로 제거한다.
#   → 2018 Q4·2020 코로나 폭락은 데이터 자체가 없어 아웃오브샘플 검증이 불가능하다.
#   가능한 최대 = 1255 − MIN_PRIOR_BARS(220) − 꼬리(101) = 934.
#   ⚠️ v2.8 정정: 예전 주석은 '2140 이 한도를 넘어 종목이 조건 미달 탈락해
#   유니버스가 227→159 로 줄었다' 고 적었으나 **틀렸다.** 탈락 게이트는
#   `n < min_prior+2` 와 `start_i = max(min_prior, n - test_lookback)` 둘뿐이고
#   test_lookback 을 늘리면 평가일은 오히려 **늘어난다**. 실제 원인은 FMP
#   분당 300 한도였다 — 8-01 로그가 `299/415`, 여기에 d0 생존율 53% 를 곱하면
#   정확히 159 다(227 은 페치가 전부 성공한 run). v2.8 에서 fmp_http 스로틀로
#   해결. 934 라는 값 자체는 1,255봉 한도 때문에 여전히 타당하다.

# ── v2.7(2026-08-25): 확정 일수 스윕 ──────────────────────────────────────
# "2일 확정이 값어치를 하는가" 를 재려고 외부에서 조절할 수 있게 뺐다.
# confirm=1 은 '조건이 처음 참이 된 다음날 매수'(장중 헤드업이 잡아주는 시점),
# confirm=2 는 현행. 같은 유니버스·같은 날 1/2/3 을 돌려 비교한다.
#
# ⚠️ 폴백은 반드시 시끄러워야 한다. 입력을 3으로 주고 돌렸는데 조용히 2로
#    떨어지면, 세 번 돌려 같은 숫자를 얻고 "확정 일수는 영향이 없다" 는
#    정반대 결론이 나온다. 그래서 폴백 시 [WARN] 을 찍는다.
def _env_confirm_days(raw=None, default: int = 2) -> int:
    """CONFIRM_DAYS 파싱. 정수 1~10 만 허용, 그 외는 경고 후 default."""
    if raw is None:
        raw = os.environ.get("CONFIRM_DAYS", "")
    s = str(raw).strip()
    if not s:
        return default
    try:
        v = int(s)
    except (TypeError, ValueError):
        print("[WARN] CONFIRM_DAYS=" + repr(s) + " 는 정수가 아니다 → "
              + str(default) + "일로 폴백")
        return default
    if not (1 <= v <= 10):
        print("[WARN] CONFIRM_DAYS=" + str(v) + " 는 범위(1~10) 밖 → "
              + str(default) + "일로 폴백")
        return default
    return v


CONFIRM_DAYS   = _env_confirm_days()   # v1.1: raw code 가 N일 연속 유지돼야 확정


# ── v2.8(2026-08-26): 부분 유니버스 차단 ────────────────────────────────────
# 페치가 일부만 성공해도 백테스트는 정상 완료되고 결과가 시트에 기록된다.
# 크기만 다른 '정상처럼 보이는 오염 행' 이라 사후에 골라낼 수 없다.
#
# ⚠️ 우회 스위치(SKIP_FETCH_GATE=1 류)는 두지 않는다. '이번만 넘기자' 용 플래그는
#    반드시 기본값이 된다. 낮추려면 이 값을 명시적으로 내려야 하고 로그에 남는다.
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


def universe_hash(tickers) -> str:
    """정렬된 티커 목록의 SHA1 앞 8자 — run 간 '같은 유니버스였나' 를 시트만 보고 판정.

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
    #    0 을 날린다("00123456" → 123456). 그러면 같은 유니버스인데 해시가 달라
    #    보이고, 그게 바로 이 열이 막으려던 사고다.
    return "u" + hashlib.sha1("\n".join(seq).encode("utf-8")).hexdigest()[:8]
COOLDOWN_DAYS  = 5             # v1.1: 같은 code 재기록 최소 간격(경계 진동 압축)
ENTRY_LAG_DAYS = 1             # v1.2: 신호일(t) 대비 실제 진입 거래일 지연.
                               #   1 = 메일(16:00 ET) 받고 '다음 거래일' 매수 = 라이브 구조.
                               #   0 = 구버전(신호일 종가 진입) — 체결 불가능. 비교용으로만.
HISTORY_BARS   = MIN_PRIOR_BARS + TEST_LOOKBACK + max(HORIZONS) + ENTRY_LAG_DAYS + 40
#   v2.9: 이름이 HISTORY_LIMIT 이었다. 단위는 그때도 **봉수**였지만 이름이
#   FMP 의 `limit` 파라미터와 같아, 이 값을 올리면 조회가 깊어진다는 착시를
#   줬다(실제로는 무시됐다). 개명은 장식이 아니다 — 이 상수를 빌려 쓰는
#   진단 파일이 옛 이름으로 남아 있으면 AttributeError 로 **크게** 죽는다.
#   조용히 옛 단위로 통과하는 것보다 낫다.
#
#   ⚠️ 숫자상 실제 구속 조건은 1255 가 아니라 MIN_PRIOR_BARS + TEST_LOOKBACK
#      = 1154 봉이다(`start_i = max(min_prior, n - test_lookback)`).
#      1255봉 요구는 hist_days_for_bars 에서 HIST_MAX_DAYS(1826일)로 클램프되어
#      실측 ≈1254봉이 온다 — pad 5봉은 상한에 먹힌다. 그래도 1154 대비 여유
#      100봉이 남으므로 평가 구간 934일은 온전하다. 이 여유가 사라지려면
#      TEST_LOOKBACK 이 1034 를 넘어야 하는데, 그 시점엔 FMP 5년 한도 자체가
#      먼저 막는다.

# 결과 시트 provenance — 어떤 진입 규칙으로 잰 행인지 표시(구/신 혼재 방지)
_ENTRY_RULE_LABEL = f"close[t+{ENTRY_LAG_DAYS}]"

# 집계 대상 버킷 (regime_core evaluate_timing code 와 일치, unknown 제외)
BUCKETS = ("entry", "wait", "overheat", "trend_break", "avoid")

# v1.4 — 실제 이메일 알림 기준 버킷 (run_watchlist_alerts.py 와 동일 상태머신)
ALERT_ENABLED_EVENTS = ("entry", "risk", "watch")   # _WL_ALERT_DEFAULT 와 동일

# ── v1.6 왕복 백테스트 ────────────────────────────────────────────────────────
#   진입은 3개 모드 공통(entry 알림 발동일 → close[t+1]). 청산 규칙만 다르다.
#     swing      : 라이브 스윙 청산 — exit 알림 상태머신 (MA50/RSI80/샹들리에)
#     pos_ideal  : 포지션 판정(integrated_sell_verdict ≥4)이 뜨는 즉시 — 자체 트리거가 있었다면
#     pos_actual : 알림(exit/risk)이 실제 발동한 날에만 포지션 카드를 볼 수 있었다 — 현 워크플로 재현
#   pos_ideal - pos_actual 차이 = '포지션 엔진에 트리거가 없었던 비용'.
#     pos_slowN  : 포지션 판정이 N일 연속 유지돼야 청산(N=3/5/10/20) — v1.9 확정일수 스윕.
#                  진입·재진입 규칙이 완전히 동일하고 지연만 다르므로 조건이 통제된다.
#                  곡선이 계속 우상향하면 '느릴수록 좋다=청산이 노이즈', 꺾이면 그 점이 최적값.
RT_SLOW_SWEEP = (3, 5, 10, 20, 30, 45, 60, 90)   # v2.1: 스윕 연장
# v1.9(3~20일)에서 곡선이 20일까지 단조 증가하고 꺾이지 않았다 → 최적점이 그 너머이거나
#   아예 없다(=팔지 마라)는 뜻. 90일까지 늘려 변곡점이 실재하는지 확정한다.
#   ※ 확정일수가 길수록 보유가 길어져 코호트(1년 컷) 안에서도 미청산이 다시 늘어난다.
#     60/90일 행은 미청산% 를 반드시 함께 볼 것.
_SLOW_MODES = tuple(f"pos_slow{n}" for n in RT_SLOW_SWEEP)
RT_SLOW_N = {f"pos_slow{n}": n for n in RT_SLOW_SWEEP}
# buy_hold 는 v1.9에서 제외: 청산 모드는 보유 중 오는 진입 신호를 놓치는데 buy_hold 는
#   전부 먹어 거래 집합이 달라진다(N 205~384 vs 43~87) → 순수 비교가 아니다.
#   같은 질문(청산이 가치를 더하는가)은 확정일수 스윕이 조건 통제 하에 더 깨끗하게 답한다.
# ── v2.2 레짐 조건부 확정일수 ────────────────────────────────────────────────
#   v2.1 결론: 확정일수를 늘리면 상승장(2023/25/26)에선 크게 이득, 하락장(2022/24)에선
#   더 손해였다. 즉 '고정 확정일수'는 알파가 아니라 '시장이 오른다'에 거는 베팅이다.
#   → 국면에 따라 확정일수를 바꾼다. 위험 국면엔 빠르게 손절, 안전 국면엔 러너를 태운다.
#     (regime_core.regime_params 가 DRG 점수로 손절 ATR배수를 조정하는 것과 같은 발상)
#   백테스트에는 DRG 이력이 없으므로 SPY 만으로 계산되는 경고 개수를 대용 지표로 쓴다.
#   경고(0~5): MA200 이탈 · MA50 이탈 · 20일 수익률 음수 · 52주 고점 대비 -10% 초과 ·
#              20일 실현변동성이 1년 중앙값의 1.5배 초과
#   ※ 모두 당일까지의 정보만 사용한다(lookahead 없음).
RT_REGIME_MAPS = {
    "pos_adapt_a": {0: 20, 1: 20, 2: 5, 3: 0, 4: 0, 5: 0},    # 보수적: 경고 2개부터 단축
    "pos_adapt_b": {0: 30, 1: 20, 2: 10, 3: 3, 4: 0, 5: 0},   # 완만: 단계적 단축
}
_ADAPT_MODES = tuple(RT_REGIME_MAPS)

# ── v2.3 진입 게이팅 ─────────────────────────────────────────────────────────
#   v2.2 결론: 13개 청산 모드 중 어느 것도 하락 연도(2022/2024)에서 SPY 를 이기지
#   못했다. 확정일수를 0일~90일 어디로 두든, 레짐 적응을 걸어도 마찬가지였다.
#   → 하락장 손실의 원인은 '언제 파느냐'가 아니라 '그때 샀다'는 데 있다.
#     청산은 20일 고정으로 통일하고 진입 게이트만 변수로 두어 이를 검증한다.
#     게이트 값 = 이 경고 개수 이상이면 entry 신호를 무시(신규 진입 금지).
RT_ENTRY_GATES = {"entry_gate3": 3, "entry_gate2": 2}
_GATE_MODES = tuple(RT_ENTRY_GATES)
RT_GATE_EXIT_CONFIRM = 20   # 게이트 모드의 청산 규칙(고정) — 비교 기준선과 동일

RT_MODES = (("swing", "pos_ideal") + _SLOW_MODES + _ADAPT_MODES
            + _GATE_MODES + ("pos_actual",))
RT_MODE_KR = {"swing": "스윙 청산(실제 실행)", "pos_ideal": "포지션 청산(즉시=0일)",
              "pos_actual": "포지션 청산(알림 게이팅)"}
RT_MODE_KR.update({m: f"포지션 청산({RT_SLOW_N[m]}일 확정)" for m in _SLOW_MODES})
RT_MODE_KR.update({"pos_adapt_a": "레짐적응A(20/20/5/0/0/0)",
                   "pos_adapt_b": "레짐적응B(30/20/10/3/0/0)"})
RT_MODE_KR.update({m: f"진입게이트(경고≥{RT_ENTRY_GATES[m]} 차단)+20일청산"
                   for m in _GATE_MODES})
RT_PF_EVENTS = {"swing": ("exit",), "pos_actual": ("exit", "risk")}
RT_STOP_ATR_MULT = 2.0    # R 분모용 플랜 손절 배수. 백테스트에는 DRG 이력이 없어 고정값 사용
RT_EARLY_EXIT_DAYS = 10   # 진입 후 N거래일 이내 청산 = 휩소로 집계
# v2.0 미청산 편향 통제(코호트):
#   확정일수를 늘릴수록 N 이 줄고 미청산(구간 종료 강제 마감)이 늘어난다(9%→22%).
#   오래 버티는 모드일수록 나쁜 거래가 실현 손실로 잡히지 않고 열린 채 끝나므로,
#   상승 구간에서는 성적이 부풀려진다. 평가 구간 끝 RT_COHORT_DAYS 이내 진입 건을
#   잘라내면 모든 모드가 청산될 시간을 동등하게 확보한 상태에서 비교할 수 있다.
#   (20일 확정의 평균 보유가 167거래일 ≈ 8개월 → 1년이면 대부분 결말이 난다)
RT_COHORT_DAYS = 365      # 캘린더 일수. 이보다 늦게 진입한 건은 코호트에서 제외
# v2.5 전·후반 분할: FMP 5년 한도로 진짜 아웃오브샘플(2018/2020)은 불가능하다.
#   차선책으로 평가 구간을 반으로 갈라 같은 결론이 양쪽에서 재현되는지만 본다.
#   ※ 각 구간 1.85년, 하락 표본은 전반=2022 후반=2024 조정뿐이라 근거는 약하다.
#     양쪽이 갈리면 '구간 특화'로 확정할 수 있지만, 일치해도 '검증됨'까지는 아니다.
#   빈 문자열이면 실행 시점의 평가 구간 중앙값으로 자동 설정한다.
RT_OOS_SPLIT_DATE = ""

ALERT_BUCKETS = (
    "alert_entry_pass",     # 매수 메일 발송 + R:R 게이트 통과 → 실제로 사는 신호
    "alert_entry_skip",     # 매수 메일 발송 + 고점 근접 구간(v3: 억제 아님, 측정용 유지)
    "alert_entry_na",       # 매수 메일 발송 + 게이트 판단 보류(플랜 산출 불가)
    "alert_risk",           # 위험 알림
    "alert_entry_invalid",  # 직전 매수 신호 조건 해제(무효화)
)


# ════════════════════════════════════════════════════════════════════════════
# 순수 엔진 (numpy/pandas 만 의존 — FMP·시트 없이 테스트 가능)
# ════════════════════════════════════════════════════════════════════════════

def _forward_metrics(close, high, low, pos: int, horizons=HORIZONS,
                     mfe_window: int = MFE_WINDOW, spy_arr=None,
                     entry_lag: int = ENTRY_LAG_DAYS) -> dict:
    """신호일 pos → 진입봉 epos(=pos+entry_lag) 종가 기준 forward-return / 초과수익 / MFE·MAE.

    - entry_price : close[epos] — 실제로 체결 가능한 최초 종가 (미래 부족 시 NaN)
    - ret_{h}d    : epos+h 종가 / 진입가 - 1            (보유 h거래일; 미래 부족 시 NaN)
    - excess_{h}d : ret_{h}d - (SPY 동일창 수익)        (spy_arr 정렬 제공 시; 베타 제거 알파)
    - mfe / mae   : [epos+1, epos+mfe_window] 최고/최저 / 진입가 - 1 (절대값, 사이징 입력)
    spy_arr: 종목 거래일에 정렬(ffill)된 SPY 종가 배열(없으면 초과수익 NaN).
             SPY 진입가도 epos 종가를 써서 종목과 같은 봉에서 출발 → 알파 비교 정합.
    entry_lag: 신호일 대비 진입 지연 거래일수. 0 이면 구버전(신호일 종가 진입).
    """
    n = len(close)
    out = {f"ret_{h}d": np.nan for h in horizons}
    out.update({f"excess_{h}d": np.nan for h in horizons})
    out["mfe"] = np.nan
    out["mae"] = np.nan
    out["entry_price"] = np.nan
    out["entry_pos"] = -1
    if pos < 0 or pos >= n:
        return out
    # 진입봉: 신호일 종가로 판정 → 메일 발송 → entry_lag 거래일 뒤 체결
    epos = pos + int(max(0, entry_lag))
    if epos >= n:
        return out          # 진입할 미래 봉이 없음 → 측정 불가(호출부에서 이벤트 제외)
    entry = float(close[epos])
    if not np.isfinite(entry) or entry <= 0:
        return out
    out["entry_price"] = entry
    out["entry_pos"] = epos

    spy_entry = None
    if spy_arr is not None and epos < len(spy_arr) and np.isfinite(spy_arr[epos]) and spy_arr[epos] > 0:
        spy_entry = float(spy_arr[epos])

    for h in horizons:
        j = epos + h
        if j < n and np.isfinite(close[j]):
            r = float(close[j]) / entry - 1.0
            out[f"ret_{h}d"] = r
            if spy_entry is not None and j < len(spy_arr) and np.isfinite(spy_arr[j]):
                out[f"excess_{h}d"] = r - (float(spy_arr[j]) / spy_entry - 1.0)

    end = min(epos + mfe_window, n - 1)
    if end > epos:
        hwin = high[epos + 1:end + 1]
        lwin = low[epos + 1:end + 1]
        if np.any(np.isfinite(hwin)):
            out["mfe"] = float(np.nanmax(hwin)) / entry - 1.0
        if np.any(np.isfinite(lwin)):
            out["mae"] = float(np.nanmin(lwin)) / entry - 1.0
    return out


def _alert_bucket(ev: dict, hist_slice, analysis: dict):
    """발동된 알림 1건 → 집계 버킷. entry 는 R:R 게이트로 pass/skip/na 분리.

    라이브에서 게이트는 메일을 '막지' 않고 라벨만 바꾼다(decorate_entry_alert).
    따라서 여기서도 억제하지 않고 버킷만 나눠, 게이트가 실제로 걸러주는지 측정한다.
    """
    e = str((ev or {}).get("event") or "")
    if e == "risk":
        return "alert_risk"
    if e == "entry_invalid":
        return "alert_entry_invalid"
    if e != "entry":
        return None                      # watch/exit/price/regime 은 집계 대상 아님
    # v2.6(2026-08-12): pass 버킷 오염 제거.
    #   resolve_target 은 (최근고점 − 진입) < 1R 이면 목표를 rr_derived 로 폴백하는데,
    #   이 경우 R:R 이 자기참조라 게이트 필터로 쓰이지 않고 gate 는 그대로 "fit" 이 된다.
    #   그 결과 '전고점 코앞이라 R:R 을 재지 못한' 건이 전부 alert_entry_pass 로 들어가
    #   실측 run 8개 전부에서 alert_entry_na = 0 이 나왔다(측정 불능 상태).
    #   → gate 는 건드리지 않고(앱/이메일 동작 불변) 버킷 분류에서만 rr_measured 로 분리.
    #     gate="na"(플랜 산출 실패)와 구분하기 위해 사유를 나눠 집계한다.
    try:
        plan = rc.build_watchlist_plan(hist_slice, analysis)
        gate = str((plan or {}).get("gate") or "na")
        rr_measured = bool((plan or {}).get("rr_measured"))
    except Exception:
        gate, rr_measured = "na", False
    if gate == "na":
        return "alert_entry_na"
    if gate in ("skip", "avoid"):
        return "alert_entry_skip"
    if not rr_measured:
        return "alert_entry_na"      # 독립 목표 미설정 → R:R 미실측(통과로 볼 수 없음)
    return "alert_entry_pass"


# ── v1.6: 포지션 판정 입력 사전계산 (벡터화) ─────────────────────────────────
def _position_features(h: pd.DataFrame) -> dict:
    """integrated_sell_verdict 에 필요한 일별 입력을 한 번에 벡터화 계산.

    보유 중 매일 position_sell_verdict 를 호출하면 같은 RSI/MACD/이평을 반복 계산해
    O(n^2) 가 된다. 무거운 부분은 티커당 1회만 만들고, 포지션별로 달라지는 것은
    '진입 이후 고점'뿐이므로 러닝 맥스로 O(1) 처리한다.
    """
    c = pd.to_numeric(h["Close"], errors="coerce")
    n = len(c)
    ma200 = c.rolling(200).mean()
    rsi = rc.compute_rsi(c)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    m, sg = macd.to_numpy(float), sig.to_numpy(float)
    st = np.full(n, "N/A", dtype=object)
    for i in range(1, n):
        if i < 34 or not (np.isfinite(m[i]) and np.isfinite(sg[i])):
            continue
        if m[i - 1] >= sg[i - 1] and m[i] < sg[i]:
            st[i] = "DEAD_CROSS"
        elif m[i] < sg[i]:
            st[i] = "BELOW_SIGNAL"
        else:
            st[i] = "ABOVE_SIGNAL"
    hi52 = c.rolling(252, min_periods=20).max()
    # ATR(Wilder 근사: TR 단순 롤링평균) — R 분모용 플랜 손절 계산에 사용
    hi = pd.to_numeric(h["High"], errors="coerce") if "High" in h.columns else c
    lo = pd.to_numeric(h["Low"], errors="coerce") if "Low" in h.columns else c
    pc = c.shift(1)
    tr = pd.concat([(hi - lo).abs(), (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(getattr(rc, "ATR_WINDOW", 14)).mean()
    return {
        "above_ma200": (c > ma200).to_numpy(bool),
        # v2.9(2026-08-27): ma200 을 **원값 그대로** 내보낸다. gap 을 여기서 미리
        #   계산해두면 분자(가격)가 _pos_label_at 에 넘어온 price 와, 분모(ma200)가
        #   이 배열과 — 서로 다른 경로에서 오게 된다. 오늘은 둘 다 h["Close"] 라
        #   값이 같지만, 나중에 호출부가 다른 price 를 넘기는 순간 dd 와 gap 이
        #   조용히 갈라진다. gap 산출은 price 를 아는 _pos_label_at 이 한다.
        "ma200": ma200.to_numpy(float),
        "ret_1m": (c / c.shift(22) - 1.0).mul(100.0).to_numpy(float),
        "rsi": rsi.to_numpy(float),
        "macd": st,
        "pct52": (c / hi52 - 1.0).mul(100.0).to_numpy(float),
        "atr": atr.to_numpy(float),
    }


def _market_warnings(spy_arr) -> np.ndarray:
    """SPY 정렬 배열 → 일자별 시장 경고 개수(0~5).

    v2.6(2026-08-12): 구현을 regime_core.market_warnings 로 이관했다(SSOT).
      라이브(run_watchlist_alerts 진입 게이트)와 백테스트가 **반드시 같은 함수**를
      써야 하기 때문이다. fill_neutral=2.0 은 이관 전 동작을 비트 단위로 재현한다
      (해당 fillna 는 실제로는 발동하지 않는 죽은 코드 — regime_core 주석 참조).
    """
    return rc.market_warnings(spy_arr, fill_neutral=2.0)


def _pos_label_at(feats: dict, price: float, ref_high: float, i: int):
    """사전계산 입력 + 진입 이후 고점 → integrated_sell_verdict.

    v2.9(2026-08-27): `gap_ma200_pct` 를 넘기지 않고 있었다. 이 인자를 빼면
      `integrated_sell_verdict` 는 200일선 이탈에 **일괄 4.0점**(🔴 청산 문턱)을
      주는 보수적 폴백으로 간다. 프로덕션(app.py · regime_core.position_sell_verdict)
      은 넘기므로 이격에 비례한 2.0~4.0 램프를 탄다.

      즉 백테스트가 프로덕션보다 체계적으로 매도에 적극적이었다. 실측(2026-08-27
      diag_sell_verdict [4], 200일선 아래 21종목): 이격 0~-4% 구간이 13종목(61.9%)
      으로, 프로덕션 2.0~3.0점 대 백테스트 4.0점. 이격 -8% 초과(포화) 7종목에서만
      두 경로가 일치했다. 200일선 바로 아래 종목의 3분의 2에서 판정이 갈렸다.

      이전 독스트링은 "(SSOT 그대로)" 라고 적혀 있었는데 **거짓**이었다. 주석이
      계약을 주장만 하고 아무도 검사하지 않으면 그 주석이 드리프트를 감춘다.
      → diag_fmp_ssot.py A4 가 이제 이 호출부를 정적으로 강제한다.

    gap 을 여기서 계산하는 이유: 분모 ma200 은 feats 에서, 분자는 인자로 받은
      `price` 에서 온다. dd 와 gap 이 **같은 price** 를 쓰도록 묶어둔다.
      가드 조건은 regime_core.position_sell_verdict 의 산출식과 문자 그대로 같다.
    """
    dd = (price / ref_high - 1.0) * 100.0 if (ref_high and np.isfinite(ref_high) and ref_high > 0) else np.nan
    _ma200 = float(feats["ma200"][i])
    gap = ((price / _ma200 - 1.0) * 100.0
           if (np.isfinite(_ma200) and _ma200 > 0) else np.nan)
    return rc.integrated_sell_verdict(
        above_ma200=bool(feats["above_ma200"][i]),
        one_month_return=float(feats["ret_1m"][i]),
        rsi=float(feats["rsi"][i]),
        macd_signal=feats["macd"][i],
        pct_from_52w_high=float(feats["pct52"][i]),
        drawdown_from_high_pct=dd,
        gap_ma200_pct=gap,
    )


def _close_trade(p: dict, mode: str, close, dates, exit_pos: int,
                 reason: str, spy_arr=None, open_at_end: bool = False) -> dict:
    """왕복 1건 확정. R = 실현손익 ÷ (진입가 − 플랜 손절가)."""
    ep, xp = float(p["entry_price"]), float(close[exit_pos])
    ret_pct = (xp / ep - 1.0) * 100.0 if ep > 0 else np.nan
    risk = ep - float(p["stop"]) if np.isfinite(p.get("stop", np.nan)) else np.nan
    r_mult = (xp - ep) / risk if (np.isfinite(risk) and risk > 0) else np.nan
    excess = np.nan
    if spy_arr is not None:
        try:
            s0, s1 = spy_arr[p["entry_pos"]], spy_arr[exit_pos]
            if np.isfinite(s0) and np.isfinite(s1) and s0 > 0:
                excess = ret_pct - (s1 / s0 - 1.0) * 100.0
        except Exception:
            excess = np.nan
    hold = int(exit_pos - p["entry_pos"])
    return {"mode": mode, "entry_date": p["entry_date"], "entry_year": p["entry_date"][:4],
            "exit_date": str(pd.Timestamp(dates[exit_pos]).date()),
            "entry_price": ep, "exit_price": xp, "ret_pct": ret_pct,
            "r_mult": r_mult, "hold_days": hold, "excess_pct": excess,
            "exit_reason": reason, "open_at_end": bool(open_at_end),
            "early_exit": bool(hold <= RT_EARLY_EXIT_DAYS and not open_at_end)}


def walk_forward_events(hist: pd.DataFrame, spy_close=None,
                        min_prior: int = MIN_PRIOR_BARS,
                        test_lookback: int = TEST_LOOKBACK,
                        horizons=HORIZONS, mfe_window: int = MFE_WINDOW,
                        confirm_days: int = CONFIRM_DAYS,
                        cooldown_days: int = COOLDOWN_DAYS,
                        entry_lag: int = ENTRY_LAG_DAYS,
                        alert_enabled=ALERT_ENABLED_EVENTS,
                        analyze_fn=None, roundtrip: bool = True):
    """한 티커의 워크포워드 평가 (v1.1: 2일 확정 + 쿨다운 디플랩).

    각 평가일 i 에서 hist[:i+1] → analyze_fn → raw code. 분류는 i 까지 데이터만(미래 차단).
    raw code 가 confirm_days 연속 유지된 '그날' 1회 확정 → 직전 기록과 다르거나(또는 같아도
    cooldown_days 경과 시) 이벤트로 기록. forward 측정만 미래 종가 참조.

    v1.2: 확정일(t)과 진입일(t+entry_lag)을 분리한다. 진입 봉이 데이터 끝을 넘어가
    체결 자체가 불가능한 이벤트는 기록하지 않는다(측정 불가 이벤트로 카운트 오염 방지).

    v1.4: 같은 루프에서 라이브 알림 상태머신(evaluate_alert_transitions)도 함께 돌려
    '실제로 메일이 나갔을 날'만 뽑은 alert 이벤트를 별도로 반환한다. analyze 호출은
    날짜당 1회를 두 모드가 공유하므로 추가 비용이 거의 없다.

    v1.6: 같은 루프에서 왕복(round-trip) 매매도 추적한다. 진입은 3개 모드 공통
    (entry 알림 발동일 → close[t+lag]), 청산 규칙만 RT_MODES 별로 다르다. 모드마다
    청산 시점이 달라 이후 보유 상태가 갈리므로 포지션/상태머신을 모드별로 유지한다.

    반환: (events, alert_events, trades, first_eval_date, last_eval_date)
    """
    if analyze_fn is None:
        analyze_fn = rc.analyze_ticker

    events: list[dict] = []
    alert_events: list[dict] = []
    trades: list[dict] = []
    if hist is None or hist.empty or "Close" not in hist.columns:
        return events, alert_events, trades, None, None

    h = hist.sort_index()
    close = pd.to_numeric(h["Close"], errors="coerce").to_numpy(dtype=float)
    high = (pd.to_numeric(h["High"], errors="coerce").to_numpy(dtype=float)
            if "High" in h.columns else close.copy())
    low = (pd.to_numeric(h["Low"], errors="coerce").to_numpy(dtype=float)
           if "Low" in h.columns else close.copy())
    dates = h.index
    n = len(close)
    if n < min_prior + 2:
        return events, alert_events, trades, None, None

    # SPY 를 종목 거래일에 정렬(ffill) → 같은 캘린더창 초과수익 계산
    spy_arr = None
    if spy_close is not None:
        try:
            spy_arr = (pd.to_numeric(spy_close, errors="coerce")
                       .reindex(dates, method="ffill").to_numpy(dtype=float))
        except Exception:
            spy_arr = None

    _warn = _market_warnings(spy_arr) if roundtrip else None

    start_i = max(min_prior, n - test_lookback)
    first_eval_date = None
    last_eval_date = None

    # v1.6 왕복 추적 상태
    _feats = _position_features(h) if roundtrip else None
    _rt_pos = {m: None for m in RT_MODES}
    _rt_pf = {m: "" for m in RT_MODES}   # 보유 중 포트폴리오 알림 상태머신(모드별)

    _alert_state = ""       # v1.4: 알림 상태머신 누적 상태(JSON) — 티커별로 이어짐
    pending_code = None     # 현재 연속 유지 중인 raw code
    pending_count = 0       # 연속 유지 일수
    last_rec_code = None    # 마지막으로 '기록'된 code
    last_rec_pos = -10 ** 9  # 마지막 기록 위치(쿨다운용)

    for i in range(start_i, n):
        date_i = dates[i]
        slice_ = h.iloc[:i + 1]
        spy_slice = None
        if spy_close is not None:
            try:
                spy_slice = spy_close.loc[:date_i]
            except Exception:
                spy_slice = spy_close
        try:
            res = analyze_fn(slice_, spy_close=spy_slice)
            code = (res.get("timing") or {}).get("code", "unknown")
        except Exception:
            res, code = None, "unknown"

        # ── [모드 B] 실제 이메일 알림 재현 (상태머신 SSOT 그대로 호출) ──
        if res is not None and alert_enabled:
            try:
                _fired, _alert_state = rc.evaluate_alert_transitions(
                    res, alert_enabled, _alert_state,
                    today_str=str(pd.Timestamp(date_i).date()),
                    price=float(close[i]) if np.isfinite(close[i]) else None,
                )
            except Exception:
                _fired = []
            for _ev_a in (_fired or []):
                _bucket = _alert_bucket(_ev_a, slice_, res)
                if _bucket is None:
                    continue
                _fma = _forward_metrics(close, high, low, i, horizons, mfe_window,
                                        spy_arr=spy_arr, entry_lag=entry_lag)
                if not np.isfinite(_fma.get("entry_price", np.nan)):
                    continue
                _ea = {"date": str(pd.Timestamp(date_i).date()),
                       "entry_date": str(pd.Timestamp(dates[int(_fma["entry_pos"])]).date()),
                       "code": _bucket}
                _ea.update(_fma)
                alert_events.append(_ea)

        # ── [v1.6] 왕복 추적 — 진입 공통 / 청산 모드별 ──
        if roundtrip and res is not None:
            _today_s = str(pd.Timestamp(date_i).date())
            _entry_fired = any(f.get("event") == "entry" for f in (_fired or []))
            for _m in RT_MODES:
                _p = _rt_pos[_m]
                if _p is None:
                    # 진입 게이트: 시장 경고가 임계 이상이면 신규 진입을 건너뛴다.
                    _gate = RT_ENTRY_GATES.get(_m)
                    if _gate is not None and _warn is not None and _warn[i] >= _gate:
                        continue
                    if _entry_fired:
                        _ei = i + entry_lag
                        if _ei < n and np.isfinite(close[_ei]) and close[_ei] > 0:
                            _atr = _feats["atr"][i]
                            _stop = (close[_ei] - RT_STOP_ATR_MULT * _atr) if np.isfinite(_atr) else np.nan
                            _rt_pos[_m] = {
                                "entry_pos": _ei, "entry_price": float(close[_ei]),
                                "entry_date": str(pd.Timestamp(dates[_ei]).date()),
                                "stop": float(_stop) if np.isfinite(_stop) else np.nan,
                                "peak": float(close[_ei]),
                                "slow_n": 0, "adapt_n": 0,
                            }
                            _rt_pf[_m] = ""
                    continue
                if i <= _p["entry_pos"]:
                    continue
                _p["peak"] = max(_p["peak"], float(close[i]))
                _hit, _why = False, ""
                if _m == "swing":
                    try:
                        _pf_fired, _rt_pf[_m] = rc.evaluate_alert_transitions(
                            res, RT_PF_EVENTS["swing"], _rt_pf[_m], today_str=_today_s,
                            price=float(close[i]))
                    except Exception:
                        _pf_fired = []
                    _hit = any(f.get("event") == "exit" for f in (_pf_fired or []))
                    if _hit:
                        _why = " · ".join((res.get("exit") or {}).get("codes") or ["exit"])
                else:
                    _lab, _rsn = _pos_label_at(_feats, float(close[i]),
                                               max(_p["peak"], _p["entry_price"]), i)
                    _is_sell = "청산" in str(_lab)
                    if _m == "pos_ideal":
                        _hit, _why = _is_sell, _rsn
                    elif _m in RT_ENTRY_GATES:
                        # 게이트 모드는 청산 규칙을 고정한다(20일 확정) → 진입 게이트만 변수
                        _p["slow_n"] = _p["slow_n"] + 1 if _is_sell else 0
                        _hit = _p["slow_n"] >= RT_GATE_EXIT_CONFIRM
                        _why = _rsn
                    elif _m in RT_REGIME_MAPS:
                        # 시장 국면(경고 개수)에 따라 필요한 확정일수가 매일 달라진다.
                        # 위험 국면으로 바뀌면 요구일수가 줄어 즉시 청산될 수 있다(빠른 방어).
                        _need = RT_REGIME_MAPS[_m].get(
                            int(_warn[i]) if _warn is not None else 2, 5)
                        _p["adapt_n"] = _p["adapt_n"] + 1 if _is_sell else 0
                        _hit = _is_sell and _p["adapt_n"] > _need
                        _why = _rsn
                    elif _m in RT_SLOW_N:
                        # 같은 판정이 N일 연속 유지돼야 실행(끊기면 리셋)
                        _p["slow_n"] = _p["slow_n"] + 1 if _is_sell else 0
                        _hit = _p["slow_n"] >= RT_SLOW_N[_m]
                        _why = _rsn
                    else:   # pos_actual — 메일이 실제로 온 날에만 카드를 볼 수 있었다
                        try:
                            _pf_fired, _rt_pf[_m] = rc.evaluate_alert_transitions(
                                res, RT_PF_EVENTS["pos_actual"], _rt_pf[_m],
                                today_str=_today_s, price=float(close[i]))
                        except Exception:
                            _pf_fired = []
                        _hit = bool(_pf_fired) and _is_sell
                        _why = _rsn
                if _hit:
                    _xi = min(i + entry_lag, n - 1)
                    trades.append(_close_trade(_p, _m, close, dates, _xi, _why, spy_arr))
                    _rt_pos[_m] = None

        if first_eval_date is None:
            first_eval_date = date_i
        last_eval_date = date_i

        # 연속 유지 카운트
        if code == pending_code:
            pending_count += 1
        else:
            pending_code = code
            pending_count = 1

        if code not in BUCKETS:
            continue
        # 확정: confirm_days 에 '도달한 그날'만 1회 (이후 같은 run 은 재발동 안 함)
        if pending_count != confirm_days:
            continue
        # de-dup: 직전 기록과 다른 code 이거나, 같아도 쿨다운 경과 시에만 기록
        if (code != last_rec_code) or (i - last_rec_pos >= cooldown_days):
            fm = _forward_metrics(close, high, low, i, horizons, mfe_window,
                                  spy_arr=spy_arr, entry_lag=entry_lag)
            _ep = fm.get("entry_price", np.nan)
            if not np.isfinite(_ep):
                continue        # 진입 봉 없음 → 체결 불가. 기록/쿨다운 모두 건드리지 않음
            _epos = int(fm.get("entry_pos", i))
            ev = {"date": str(pd.Timestamp(date_i).date()),          # 신호 확정일
                  "entry_date": str(pd.Timestamp(dates[_epos]).date()),  # 실제 진입일
                  "code": code}
            ev.update(fm)
            events.append(ev)
            last_rec_code = code
            last_rec_pos = i

    # 구간 끝에 열려 있는 포지션은 마지막 종가로 강제 마감 + 별도 표기
    if roundtrip:
        for _m, _p in _rt_pos.items():
            if _p is not None and n - 1 > _p["entry_pos"]:
                trades.append(_close_trade(_p, _m, close, dates, n - 1,
                                           "구간 종료(미청산)", spy_arr, open_at_end=True))

    return events, alert_events, trades, first_eval_date, last_eval_date


def _nan_pct(arr, fn) -> float:
    """fn(유한값) * 100, 소수 2자리. 유효값 없으면 NaN."""
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return np.nan
    return round(float(fn(a)) * 100.0, 2)


def aggregate_events(events: list[dict], buckets=BUCKETS) -> dict:
    """이벤트 리스트 → 버킷별 통계 dict. buckets 로 verdict/alert 모드 공용."""
    agg = {}
    for code in buckets:
        evs = [e for e in events if e.get("code") == code]
        r20 = np.array([e.get("ret_20d", np.nan) for e in evs], dtype=float)
        valid20 = r20[np.isfinite(r20)]
        winrate = round(float(np.mean(valid20 > 0)) * 100.0, 2) if valid20.size else np.nan
        ex20 = np.array([e.get("excess_20d", np.nan) for e in evs], dtype=float)
        ex20v = ex20[np.isfinite(ex20)]
        excess_win = round(float(np.mean(ex20v > 0)) * 100.0, 2) if ex20v.size else np.nan
        agg[code] = {
            "count": len(evs),
            "winrate_20d": winrate,
            "ret_5d_mean":  _nan_pct([e.get("ret_5d") for e in evs], np.mean),
            "ret_20d_mean": _nan_pct(r20, np.mean),
            "ret_20d_median": _nan_pct(r20, np.median),
            "ret_60d_mean": _nan_pct([e.get("ret_60d") for e in evs], np.mean),
            "mfe_20d_mean": _nan_pct([e.get("mfe") for e in evs], np.mean),
            "mae_20d_mean": _nan_pct([e.get("mae") for e in evs], np.mean),
            "excess_20d_mean": _nan_pct(ex20, np.mean),
            "excess_win_20d": excess_win,
        }
    return agg


def aggregate_trades(trades: list[dict]) -> dict:
    """왕복 거래 → 모드별 집계. RT_MODES 키."""
    out = {}
    for m in RT_MODES:
        ts = [t for t in trades if t.get("mode") == m]
        closed = [t for t in ts if not t.get("open_at_end")]
        n_all, n_cl = len(ts), len(closed)
        if n_all == 0:
            out[m] = {"trades": 0, "closed": 0, "open_pct": np.nan, "winrate": np.nan,
                      "avg_r": np.nan, "median_r": np.nan, "avg_ret": np.nan,
                      "median_ret": np.nan, "avg_hold": np.nan, "early_pct": np.nan,
                      "excess": np.nan, "top_reason": ""}
            continue
        _r = np.array([t["r_mult"] for t in ts], dtype=float)
        _ret = np.array([t["ret_pct"] for t in ts], dtype=float)
        _ex = np.array([t["excess_pct"] for t in ts], dtype=float)
        _hold = np.array([t["hold_days"] for t in ts], dtype=float)
        _reasons = {}
        for t in closed:
            k = str(t.get("exit_reason") or "-")[:40]
            _reasons[k] = _reasons.get(k, 0) + 1
        _top = max(_reasons.items(), key=lambda kv: kv[1])[0] if _reasons else ""
        out[m] = {
            "trades": n_all, "closed": n_cl,
            "open_pct": round((n_all - n_cl) / n_all * 100.0, 1),
            "winrate": _nan_pct(_ret, lambda a: float(np.mean(a > 0))),
            "avg_r": round(float(np.nanmean(_r)), 2) if np.isfinite(_r).any() else np.nan,
            "median_r": round(float(np.nanmedian(_r)), 2) if np.isfinite(_r).any() else np.nan,
            "avg_ret": round(float(np.nanmean(_ret)), 2) if np.isfinite(_ret).any() else np.nan,
            "median_ret": round(float(np.nanmedian(_ret)), 2) if np.isfinite(_ret).any() else np.nan,
            "avg_hold": round(float(np.nanmean(_hold)), 1) if np.isfinite(_hold).any() else np.nan,
            "early_pct": round(sum(1 for t in closed if t.get("early_exit")) / n_cl * 100.0, 1) if n_cl else np.nan,
            "excess": round(float(np.nanmean(_ex)), 2) if np.isfinite(_ex).any() else np.nan,
            "top_reason": _top,
        }
    return out


def _jsonable(v):
    """NaN/Inf → "" (gspread 는 JSON 비준수 float 를 거부한다).

완결 거래가 0건인 모드는 EarlyExit_Pct 등이 NaN 이라
    이 정리를 거치지 않으면 시트 저장 전체가 실패한다.
    """
    if isinstance(v, (float, np.floating)):
        return "" if not np.isfinite(v) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def build_rt_rows(rt_aggs: dict, run_date: str, hist_start: str, hist_end: str,
                  universe_size: int, seg_size: dict = None,
                  seg_hash: dict = None) -> list:
    """v2.8: Universe_Size 를 그 행 세그먼트의 실 종목수로 기록하고 지문을 붙인다.

    seg_size/seg_hash 미제공 시 전역값으로 폴백(구 호출부 호환).
    """
    rows = []
    for _k, a in rt_aggs.items():
        m, seg, yr = (_k + ("all",))[:3] if len(_k) == 2 else _k
        _sz = (seg_size or {}).get(seg, universe_size)
        _hs = (seg_hash or {}).get(seg, "")
        rows.append([_jsonable(x) for x in
                     [run_date, hist_start, hist_end, _sz, m, seg, yr,
                      a.get("trades", 0), a.get("closed", 0), a.get("open_pct"),
                      a.get("winrate"), a.get("avg_r"), a.get("median_r"),
                      a.get("avg_ret"), a.get("median_ret"), a.get("avg_hold"),
                      a.get("early_pct"), a.get("excess"), a.get("top_reason", ""),
                      _hs]])
    return rows


def build_result_rows(agg: dict, run_date: str, hist_start: str, hist_end: str,
                      universe_size: int, buckets=BUCKETS,
                      mode: str = "verdict", segment: str = "all",
                      uni_hash: str = "") -> list[list]:
    """집계 dict → Signal_Backtest 시트 행들(_RESULT_COLS 순서).

    mode: "verdict"(화면 판정) | "alert"(실제 이메일 발송 기준) — 시트 Mode 열로 구분.

    v2.8 ⚠️ universe_size 의 의미가 바뀌었다: 전역 합계 → **그 행 세그먼트의 실
    종목수**. 이전 행의 stock 값에는 ETF 가 섞여 있어, 개별주 유니버스가 바뀐
    것인지 시트만 보고는 판별할 수 없었다. uni_hash 가 그 판정을 대신한다.
    """
    def _cell(v):
        return "" if (v is None or (isinstance(v, float) and not np.isfinite(v))) else v
    rows = []
    for code in buckets:
        a = agg.get(code, {})
        rows.append([
            run_date, hist_start, hist_end, universe_size, code,
            _cell(a.get("count")), _cell(a.get("winrate_20d")),
            _cell(a.get("ret_5d_mean")), _cell(a.get("ret_20d_mean")),
            _cell(a.get("ret_20d_median")), _cell(a.get("ret_60d_mean")),
            _cell(a.get("mfe_20d_mean")), _cell(a.get("mae_20d_mean")),
            _cell(a.get("excess_20d_mean")), _cell(a.get("excess_win_20d")),
            _ENTRY_RULE_LABEL, mode, segment, CONFIRM_DAYS, uni_hash,
        ])
    return rows


# ════════════════════════════════════════════════════════════════════════════
# I/O — FMP 데이터 / Google Sheets (자동화 전용)
# ════════════════════════════════════════════════════════════════════════════

# ── Sheets 일시 장애 재시도 (v1.4 — gs_retry SSOT 위임) ───────────────────────
# 2026-09-04 까지 이 자리에는 `_gs_is_transient` + `_gs` 구현 55줄이 있었고,
# diag_satellite_backtest.py 에 거의 같은 55줄이 또 있었다. 둘 다 gs_retry.py 와
# 정책이 겹쳤다 — 같은 로직 세 벌.
#
# 합치되 **예산은 지킨다.** 이 스크립트는 몇 시간치 FMP 수집이 끝난 맨 마지막에
# 시트에 쓴다. 그 한 번이 실패하면 런 전체가 날아가므로 62초를 기다릴 값어치가
# 있다. gs_retry 의 기본값 22초는 5PM 알림 경로용이라 여기 맞지 않는다.
# → PROFILE_BATCH 가 이전 `_gs` 의 값(6시도 · 2/4/8/16/32초 · 지터 25%)을
#   그대로 들고 있다. diag_gs_retry.py G2 가 초 단위로 대조한다.
#
# ⚠️ 통합하면서 **한 가지가 의도적으로 바뀌었다.** 이전 `_gs_is_transient` 는
#    HTTP 상태를 못 읽는 예외를 재시도하지 않았다(requests 예외 3종만 이름으로
#    잡았다). 그래서 google.auth TransportError · ssl.SSLError ·
#    RemoteDisconnected 가 오면 **재시도 없이 런이 죽었다.** gs_retry 는 이런
#    예외를 네트워크 계열로 보고 재시도한다(실패 방향이 안전하다).
#    정책 변경이 아니라 버그 수정이다.
#
# 호출부 16곳은 한 글자도 바뀌지 않는다 — 이 별칭이 시그니처를 그대로 받는다.
def _gs(fn, *args, **kwargs):
    """gspread 호출 재시도 래퍼 (gs_retry SSOT · 배치 프로파일).

    사용: _gs(gc.open, TITLE) / _gs(ws.get_all_values) / _gs(ws.update, rows, range_name=...)
    """
    return gsr.call(fn, *args, _profile=gsr.PROFILE_BATCH, **kwargs)


def get_gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(GSPREAD_KEY_JSON)
    creds = Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)


def _fmp_price_history(ticker: str, *, bars: int):
    """app.py·run_watchlist_alerts 와 동일한 /stable historical-price-eod/full.

    bars : 하류가 실제로 소비하는 **봉수**. 달력일 환산은 fmp_extras 가 한다.
           기본값을 두지 않는다 — 호출부가 자기 요구를 명시하게 강제한다.

    반환: (DataFrame, kind). kind ∈ {"ok","empty","no_key","rate_limited",
    "plan_limited","http_error","exception"}

    v2.9: `limit` → `from`/`to` 창.
      · 인자를 **키워드 전용**으로 못박았다. 옛 코드의 `_fmp_price_history(tk, 600)`
        같은 위치 인자가 새 의미로 조용히 통과하는 경로를 구조적으로 막는다
        (run_watchlist_alerts._fmp_price_history 가 같은 이유로 keyword-only 다).
      · 기본값도 없앴다. §7 "bars 인자에 기본값 금지" — 중간층까지 포함이다.
        여기서는 SPY(벤치마크)와 유니버스가 서로 다른 창을 받는 사고를 막는다.
        SPY 창만 짧아지면 초과수익(alpha)이 조용히 다른 기간 대비로 계산된다.

    v2.8: 두 가지를 고쳤다.
      1) requests.get → fh.fmp_get_ex — 레이트리밋(슬라이딩 윈도우) + 429 지수
         백오프 + 지터가 딸려온다. 이전에는 이 경로만 SSOT 를 우회해, 분당 한도를
         넘긴 요청이 429 로 되돌아와 빈 DataFrame 이 됐다.
      2) 실패를 빈 DataFrame 하나로 뭉개지 않는다. 402(플랜 제한)·429(레이트리밋)·
         빈 200 응답은 원인도 대응도 전혀 다른데 구분할 방법이 없었다.
    """
    if not FMP_API_KEY:
        return pd.DataFrame(), "no_key"
    _win = fx.hist_range_params(fx.hist_days_for_bars(bars))
    url = (f"{_FMP_BASE}/historical-price-eod/full"
           f"?symbol={ticker}{_win}&apikey={FMP_API_KEY}")
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
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df = df.rename(columns={"close": "Close", "open": "Open", "high": "High",
                                "low": "Low", "volume": "Volume", "adjClose": "Adj Close"})
        for col in ["Close", "Open", "High", "Low", "Volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df, "ok"
    except Exception:
        return pd.DataFrame(), "exception"


def _batch_fetch_history(tickers: list, *, bars: int):
    """ThreadPoolExecutor 병렬 fetch → (cache, reasons, failed).

    cache   : {ticker: DataFrame}
    reasons : {kind: count} — 성공/실패 사유 분포
    failed  : [(ticker, kind), ...] — 탈락 종목과 그 이유

    v2.9: 중간층에도 기본값을 두지 않는다. 중간층 기본값은 상위 호출부가
    창 요구를 빠뜨려도 "그럴듯한 값"으로 메워주기 때문에, 요구가 바뀌었을 때
    한쪽만 갱신되는 사고를 만든다.

    v2.8: 이전에는 `except: pass` 로 예외까지 삼켜 탈락 사유가 하나도 남지
    않았다. 스로틀은 fh.fmp_get_ex 안에 있으므로 워커 수는 그대로 둔다
    (워커는 한도에 걸리면 대기할 뿐 요청을 잃지 않는다).
    """
    out: dict = {}
    reasons: dict = {}
    failed: list = []
    if not tickers:
        return out, reasons, failed
    with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
        futs = {ex.submit(_fmp_price_history, tk, bars=bars): tk
                for tk in tickers}
        for fut in concurrent.futures.as_completed(futs):
            tk = futs[fut]
            try:
                df, kind = fut.result()
            except Exception:
                df, kind = pd.DataFrame(), "exception"
            reasons[kind] = reasons.get(kind, 0) + 1
            if df is not None and not df.empty:
                out[tk] = df
            else:
                failed.append((tk, kind))
    return out, reasons, failed


def _read_col(ws, col_idx: int) -> list:
    """워크시트 헤더 제외 특정 컬럼의 비어있지 않은 값들(대문자 정리)."""
    vals = _gs(ws.get_all_values) or []
    out = []
    for row in vals[1:]:
        if len(row) > col_idx:
            t = str(row[col_idx]).strip().upper()
            if t:
                out.append(t)
    return out


def load_universe(gc):
    """ETF_Universe + Watchlist + Portfolios 합집합(중복 제거).

    v1.5: (tickers, segment_map) 반환. segment_map[ticker] ∈ {"etf", "stock"} —
    ETF_Universe 시트 소속이면 etf, 아니면 stock.
    """
    sh = _gs(gc.open, _SPREADSHEET_TITLE)
    titles = {ws.title for ws in _gs(sh.worksheets)}
    tickers: set[str] = set()
    etf_set: set[str] = set()

    sources = [
        (_ETF_UNIVERSE_WORKSHEET, 0),
        (_WATCHLIST_WORKSHEET, 1),
        (_PORTFOLIO_WORKSHEET, 2),
    ]
    for title, col_idx in sources:
        if title not in titles:
            print(f"[INFO] '{title}' 시트 없음 — 스킵")
            continue
        try:
            ws = _gs(sh.worksheet, title)
            got = _read_col(ws, col_idx)
            tickers.update(got)
            if title == _ETF_UNIVERSE_WORKSHEET:
                etf_set.update(got)
            print(f"[OK] '{title}' 에서 {len(got)}개 로드")
        except Exception as e:
            print(f"[WARN] '{title}' 로드 실패 — 스킵: {e}")

    tickers.discard("SPY")  # SPY 는 벤치마크로 별도 fetch
    etf_set.discard("SPY")
    uni = sorted(tickers)
    seg = {t: ("etf" if t in etf_set else "stock") for t in uni}
    print(f"[INFO] 세그먼트: ETF {sum(1 for v in seg.values() if v == 'etf')}종목 · "
          f"개별주 {sum(1 for v in seg.values() if v == 'stock')}종목")
    return uni, seg


def open_result_worksheet(gc, title: str = None, cols: list = None):
    """결과 탭. 없으면 생성 + 헤더. 헤더가 현재 스키마와 다르면 헤더만 갱신(마이그레이션).

    v1.6: title/cols 를 받아 왕복 결과 시트(_RT_WORKSHEET)도 같은 경로로 처리한다.
    """
    title = title or _RESULT_WORKSHEET
    cols = cols or _RESULT_COLS
    sh = _gs(gc.open, _SPREADSHEET_TITLE)
    titles = [ws.title for ws in _gs(sh.worksheets)]
    last_col = chr(ord("A") + len(cols) - 1)
    if title in titles:
        ws = _gs(sh.worksheet, title)
        try:
            if (_gs(ws.row_values, 1) or []) != cols:
                _gs(ws.update, [cols], range_name=f"A1:{last_col}1",
                          value_input_option="USER_ENTERED")
                print(f"[INFO] {title} 헤더 갱신(스키마 변경 반영)")
        except Exception:
            pass
        return ws
    ws = _gs(sh.add_worksheet, title=title, rows=2000, cols=len(cols))
    _gs(ws.update, [cols], range_name=f"A1:{last_col}1", value_input_option="USER_ENTERED")
    return ws


def _safe_append_rows(ws, rows, ncols: int, value_input_option: str = "USER_ENTERED") -> None:
    """append_row 계단식 드리프트 회피 — A열 기준 마지막 다음 행에 update. (app.py 동일 로직)"""
    if not rows:
        return
    if not isinstance(rows[0], (list, tuple)):
        rows = [rows]
    rows = [list(r) for r in rows if r is not None]
    if not rows:
        return
    existing = _gs(ws.get_all_values) or []
    last_row = 0
    for idx, r in enumerate(existing, start=1):
        if any(str(c).strip() != "" for c in r):
            last_row = idx
    start_row = last_row + 1
    end_row = start_row + len(rows) - 1
    try:
        if end_row > ws.row_count:
            _gs(ws.add_rows, end_row - ws.row_count + 50)
    except Exception:
        pass
    last_col = chr(ord("A") + max(0, ncols - 1))
    _gs(ws.update, rows, range_name=f"A{start_row}:{last_col}{end_row}",
              value_input_option=value_input_option)


# ════════════════════════════════════════════════════════════════════════════
# 오케스트레이션
# ════════════════════════════════════════════════════════════════════════════

SEGMENTS = ("all", "etf", "stock")


def run_backtest(universe: list, spy_hist: pd.DataFrame, hist_cache: dict,
                 segment_map: dict | None = None):
    """유니버스 전체 워크포워드 → (aggs, meta).

    v1.5: aggs[(mode, segment)] = 집계 dict. mode ∈ {verdict, alert},
    segment ∈ SEGMENTS. segment_map 미제공 시 전부 stock 으로 간주.
    """
    spy_close = spy_hist["Close"] if (spy_hist is not None and "Close" in spy_hist.columns) else None

    all_events: list[dict] = []
    all_alerts: list[dict] = []
    all_trades: list[dict] = []
    eval_starts, eval_ends = [], []
    n_with_data = 0
    ok_by_seg: dict[str, list] = {}   # v2.8: d0 통과 티커를 세그먼트별로 보관

    for tk in universe:
        hist = hist_cache.get(tk)
        if hist is None or hist.empty:
            continue
        events, alerts, tds, d0, d1 = walk_forward_events(
            hist, spy_close=spy_close, confirm_days=CONFIRM_DAYS)
        _seg = (segment_map or {}).get(tk, "stock")
        if d0 is not None:
            n_with_data += 1
            eval_starts.append(pd.Timestamp(d0))
            eval_ends.append(pd.Timestamp(d1))
            ok_by_seg.setdefault(_seg, []).append(tk)
        for _e in events:
            _e["segment"] = _seg
        for _a in alerts:
            _a["segment"] = _seg
        for _t in tds:
            _t["segment"] = _seg
            _t["ticker"] = tk
        all_events.extend(events)
        all_alerts.extend(alerts)
        all_trades.extend(tds)

    def _seg_filter(evs, seg):
        return evs if seg == "all" else [e for e in evs if e.get("segment") == seg]

    aggs = {}
    for seg in SEGMENTS:
        aggs[("verdict", seg)] = aggregate_events(_seg_filter(all_events, seg))
        aggs[("alert", seg)] = aggregate_events(_seg_filter(all_alerts, seg),
                                                buckets=ALERT_BUCKETS)
    rt_aggs = {}
    for seg in SEGMENTS:
        for m, a in aggregate_trades(_seg_filter(all_trades, seg)).items():
            rt_aggs[(m, seg, "all")] = a
    # v1.8: 진입연도별 분해 — 개별주 한정(ETF 는 알파가 없어 배제).
    #   2022 하락 구간과 2023~ 상승 구간에서 최적 확정일수가 다른지 확인한다.
    _stock_trades = _seg_filter(all_trades, "stock")
    rt_years = sorted({str(t.get("entry_year") or "") for t in _stock_trades} - {""})
    for yr in rt_years:
        _yt = [t for t in _stock_trades if str(t.get("entry_year")) == yr]
        for m, a in aggregate_trades(_yt).items():
            rt_aggs[(m, "stock", yr)] = a

    # v2.4: 아웃오브샘플 분할 — 파라미터를 바꾸지 않고 두 구간에서 결론이 재현되는지
    _split = RT_OOS_SPLIT_DATE
    if not _split:
        try:
            _ds = sorted(str(t.get("entry_date") or "") for t in _stock_trades
                         if t.get("entry_date"))
            _split = _ds[len(_ds) // 2] if _ds else ""
        except Exception:
            _split = ""
    if _split:
        for _tag, _sel in (("half1", lambda d: d < _split),
                           ("half2", lambda d: d >= _split)):
            _sub = [t for t in _stock_trades if _sel(str(t.get("entry_date") or ""))]
            for m, a in aggregate_trades(_sub).items():
                rt_aggs[(m, "stock", _tag)] = a

    # v2.0: 미청산 편향 통제 코호트 — 구간 끝 1년 이내 진입 건 제외(개별주)
    _cutoff = ""
    try:
        if eval_ends:
            _cutoff = str((pd.Timestamp(max(eval_ends)) -
                           pd.Timedelta(days=RT_COHORT_DAYS)).date())
    except Exception:
        _cutoff = ""
    if _cutoff:
        _cohort = [t for t in _stock_trades if str(t.get("entry_date") or "") <= _cutoff]
        for m, a in aggregate_trades(_cohort).items():
            rt_aggs[(m, "stock", "cohort")] = a

    # v2.8: 세그먼트별 '실제로 평가된' 티커 집합 — 시트의 Universe_Size/Hash 근거.
    #   전역 해시로 하면 ETF 만 바뀌어도 개별주 비교까지 못 하게 된다.
    _seg_tickers = {
        "all": sorted(t for v in ok_by_seg.values() for t in v),
        "etf": sorted(ok_by_seg.get("etf", [])),
        "stock": sorted(ok_by_seg.get("stock", [])),
    }
    _n_etf = sum(1 for t in universe if (segment_map or {}).get(t) == "etf")
    meta = {
        "universe_size": n_with_data,
        "hist_start": str(min(eval_starts).date()) if eval_starts else "",
        "hist_end": str(max(eval_ends).date()) if eval_ends else "",
        "total_events": len(all_events),
        "total_alerts": len(all_alerts),
        "total_trades": len(all_trades),
        "n_etf": _n_etf,
        "n_stock": len(universe) - _n_etf,
    }
    meta["seg_size"] = {k: len(v) for k, v in _seg_tickers.items()}
    meta["seg_hash"] = {k: universe_hash(v) for k, v in _seg_tickers.items()}
    meta["rt_years"] = rt_years
    meta["rt_cohort_cutoff"] = _cutoff
    meta["rt_split_date"] = _split
    return aggs, rt_aggs, meta


def _print_summary(aggs: dict, meta: dict) -> None:
    print(f"\n[백테스트 요약] 유니버스 {meta['universe_size']}종목 · "
          f"구간 {meta['hist_start']}~{meta['hist_end']} · "
          f"판정 이벤트 {meta['total_events']} · 알림 이벤트 {meta.get('total_alerts', 0)} · "
          f"ETF {meta.get('n_etf', 0)} / 개별주 {meta.get('n_stock', 0)}")
    _ss, _sh = meta.get("seg_size") or {}, meta.get("seg_hash") or {}
    if _ss:
        print("[유니버스 지문] " + " · ".join(
            f"{k}={_ss.get(k, 0)}종목/{_sh.get(k, '') or '-'}"
            for k in ("all", "stock", "etf")))

    def _s(v):
        return "-" if (v is None or (isinstance(v, float) and not np.isfinite(v))) else f"{v}"

    def _table(title, agg_d, buckets):
        print(f"\n── {title} ──")
        print(f"{'버킷':<20}{'N':>6}{'승률20d':>9}{'평균5d':>8}{'평균20d':>8}{'중앙20d':>8}"
              f"{'평균60d':>8}{'MFE20':>7}{'MAE20':>7}{'초과20d':>8}{'초과승률':>9}")
        for code in buckets:
            a = agg_d.get(code, {})
            print(f"{code:<20}{a.get('count', 0):>6}{_s(a.get('winrate_20d')):>9}"
                  f"{_s(a.get('ret_5d_mean')):>8}{_s(a.get('ret_20d_mean')):>8}"
                  f"{_s(a.get('ret_20d_median')):>8}{_s(a.get('ret_60d_mean')):>8}"
                  f"{_s(a.get('mfe_20d_mean')):>7}{_s(a.get('mae_20d_mean')):>7}"
                  f"{_s(a.get('excess_20d_mean')):>8}{_s(a.get('excess_win_20d')):>9}")

    _seg_kr = {"all": "전체", "etf": "ETF만", "stock": "개별주만"}
    for seg in SEGMENTS:
        for mode, buckets in (("alert", ALERT_BUCKETS), ("verdict", BUCKETS)):
            _t = "실제 이메일 발송 기준" if mode == "alert" else "화면 판정 기준"
            _table(f"[{mode}/{seg}] {_t} — {_seg_kr.get(seg, seg)}",
                   aggs.get((mode, seg), {}), buckets)


def _print_rt_summary(rt_aggs: dict, meta: dict) -> None:
    print(f"\n[왕복(round-trip) 요약] 진입 규칙 공통 = entry 알림 발동일 → close[t+{ENTRY_LAG_DAYS}] · "
          f"총 거래 {meta.get('total_trades', 0)}건")

    def _s(v):
        return "-" if (v is None or (isinstance(v, float) and not np.isfinite(v))) else f"{v}"

    _seg_kr = {"all": "전체", "etf": "ETF만", "stock": "개별주만"}
    for seg in SEGMENTS:
        print(f"\n── 왕복 / {_seg_kr.get(seg, seg)} ──")
        print(f"{'모드':<26}{'N':>5}{'완결':>6}{'미청산%':>8}{'승률':>7}{'평균R':>7}"
              f"{'중앙R':>7}{'평균%':>8}{'평균보유':>9}{'조기청산%':>10}{'초과%':>8}")
        for m in RT_MODES:
            a = rt_aggs.get((m, seg, "all"), {})
            print(f"{RT_MODE_KR.get(m, m):<26}{a.get('trades', 0):>5}{a.get('closed', 0):>6}"
                  f"{_s(a.get('open_pct')):>8}{_s(a.get('winrate')):>7}{_s(a.get('avg_r')):>7}"
                  f"{_s(a.get('median_r')):>7}{_s(a.get('avg_ret')):>8}{_s(a.get('avg_hold')):>9}"
                  f"{_s(a.get('early_pct')):>10}{_s(a.get('excess')):>8}")
        def _g(m, k):
            return rt_aggs.get((m, seg, "all"), {}).get(k, np.nan)

        def _cmp(lhs, rhs, tag):
            a, b = _g(lhs, "excess"), _g(rhs, "excess")
            if np.isfinite(a) and np.isfinite(b):
                print(f"   → {tag}: SPY초과 {a - b:+.2f}%p (평균R {_g(lhs, 'avg_r') - _g(rhs, 'avg_r'):+.2f})")
        _cmp("pos_ideal", "pos_actual", "즉시청산 − 알림게이팅")
        for _n in RT_SLOW_SWEEP:
            _cmp(f"pos_slow{_n}", "pos_ideal", f"{_n:>2}일확정 − 즉시청산 (지연 효과)")
        for _am in _ADAPT_MODES:
            _cmp(_am, "pos_slow20", f"{RT_MODE_KR[_am][:6]} − 20일고정 (레짐적응 효과)")
        for _gm in _GATE_MODES:
            _cmp(_gm, "pos_slow20", f"경고≥{RT_ENTRY_GATES[_gm]} 진입차단 − 게이트없음 (진입 게이팅 효과)")
        _best = max(((_g(m, "excess"), m) for m in RT_MODES),
                    key=lambda t: (t[0] if np.isfinite(t[0]) else -1e9))
        if np.isfinite(_best[0]):
            print(f"   ★ 최선: {RT_MODE_KR.get(_best[1], _best[1])} (SPY초과 {_best[0]:+.2f}%)")

    # ── v2.4: 아웃오브샘플 검증 (개별주) ──
    if any(k[2] == "half1" for k in rt_aggs):
        print(f"\n\n[전·후반 재현성 / 개별주만] 분할 진입일 {meta.get('rt_split_date', '')}")
        print("   FMP 계정 이력이 5년(롤링)으로 제한돼 2018·2020 하락장 데이터가 없다.")
        print("   → 진짜 아웃오브샘플은 불가. 평가 구간을 반으로 갈라 재현성만 본다.")
        print("   양쪽이 갈리면 '구간 특화'로 확정할 수 있으나, 일치해도 '검증됨'은 아니다.\n")
        print(f"{'모드':<34}{'전반 N':>7}{'전반초과%':>10}{'전반평균R':>10}"
              f"{'  |':>4}{'후반N':>6}{'후반초과%':>9}{'후반평균R':>9}{'부호일치':>10}")
        for m in RT_MODES:
            o = rt_aggs.get((m, "stock", "half1"), {})
            v = rt_aggs.get((m, "stock", "half2"), {})
            _oe, _ie = o.get("excess", np.nan), v.get("excess", np.nan)
            _agree = ("-" if not (np.isfinite(_oe) and np.isfinite(_ie))
                      else ("○" if (_oe >= 0) == (_ie >= 0) else "✗"))
            print(f"{RT_MODE_KR.get(m, m):<34}{o.get('trades', 0):>7}{_s(_oe):>10}"
                  f"{_s(o.get('avg_r')):>10}{'  |':>4}{v.get('trades', 0):>6}{_s(_ie):>9}"
                  f"{_s(v.get('avg_r')):>9}{_agree:>10}")
        _ob = max(((rt_aggs.get((m, "stock", "half1"), {}).get("excess", np.nan), m)
                   for m in RT_MODES), key=lambda t: (t[0] if np.isfinite(t[0]) else -1e9))
        _ib = max(((rt_aggs.get((m, "stock", "half2"), {}).get("excess", np.nan), m)
                   for m in RT_MODES), key=lambda t: (t[0] if np.isfinite(t[0]) else -1e9))
        print(f"   ★ 전반 최선: {RT_MODE_KR.get(_ob[1], _ob[1])} ({_ob[0]:+.2f}%)")
        print(f"   ★ 후반 최선: {RT_MODE_KR.get(_ib[1], _ib[1])} ({_ib[0]:+.2f}%)")
        print(f"   → 두 최선이 {'같다(재현됨)' if _ob[1] == _ib[1] else '다르다(구간 특화 의심)'}")

    # ── v2.0: 미청산 편향 통제 코호트 (개별주) ──
    _cut = meta.get("rt_cohort_cutoff") or ""
    if _cut and any(k[2] == "cohort" for k in rt_aggs):
        print(f"\n\n[미청산 편향 통제 / 개별주만] {_cut} 이전 진입분만 집계")
        print("   확정일수가 길수록 나쁜 거래가 '구간 종료(미청산)'로 빠져 성적이 부풀려진다.")
        print("   모든 모드에 청산될 시간을 동등하게 준 뒤 비교한 결과.\n")
        print(f"{'모드':<26}{'N':>6}{'미청산%':>9}{'초과%':>9}{'평균R':>8}"
              f"{'  |':>4}{'전체N':>7}{'전체미청산%':>12}{'전체초과%':>10}{'차이':>8}")
        for m in RT_MODES:
            c = rt_aggs.get((m, "stock", "cohort"), {})
            a = rt_aggs.get((m, "stock", "all"), {})
            _d = (c.get("excess", np.nan) - a.get("excess", np.nan))
            print(f"{RT_MODE_KR.get(m, m):<26}{c.get('trades', 0):>6}{_s(c.get('open_pct')):>9}"
                  f"{_s(c.get('excess')):>9}{_s(c.get('avg_r')):>8}{'  |':>4}"
                  f"{a.get('trades', 0):>7}{_s(a.get('open_pct')):>12}{_s(a.get('excess')):>10}"
                  f"{('-' if not np.isfinite(_d) else f'{_d:+.2f}'):>8}")
        _cb = max(((rt_aggs.get((m, "stock", "cohort"), {}).get("excess", np.nan), m)
                   for m in RT_MODES), key=lambda t: (t[0] if np.isfinite(t[0]) else -1e9))
        if np.isfinite(_cb[0]):
            print(f"   ★ 편향 통제 후 최선: {RT_MODE_KR.get(_cb[1], _cb[1])} "
                  f"(SPY초과 {_cb[0]:+.2f}%)")

    _years = meta.get("rt_years") or []
    if _years:
        print(f"\n\n[진입연도별 / 개별주만] — SPY초과%(베타 제거) · 괄호는 거래수")
        print("   진입 연도가 최근일수록 관측 가능한 보유기간이 짧다 → 연도 간이 아니라")
        print("   같은 연도(열) 안에서 모드끼리만 비교할 것.\n")
        print(f"{'모드':<26}" + "".join(f"{y:>16}" for y in _years))
        for m in RT_MODES:
            _cells = []
            for y in _years:
                a = rt_aggs.get((m, "stock", y), {})
                _e, _n = a.get("excess", np.nan), a.get("trades", 0)
                _cells.append(f"{('-' if not np.isfinite(_e) else f'{_e:+.2f}')} ({_n})".rjust(16))
            print(f"{RT_MODE_KR.get(m, m):<26}" + "".join(_cells))
        print(f"\n{'모드':<26}" + "".join(f"{y:>16}" for y in _years) + "   ← 평균 R")
        for m in RT_MODES:
            _cells = []
            for y in _years:
                _r = rt_aggs.get((m, "stock", y), {}).get("avg_r", np.nan)
                _cells.append(("-" if not np.isfinite(_r) else f"{_r:+.2f}").rjust(16))
            print(f"{RT_MODE_KR.get(m, m):<26}" + "".join(_cells))


def main():
    if not FMP_API_KEY or not GSPREAD_KEY_JSON:
        print("[ERROR] FMP_API_KEY / GSPREAD_KEY 환경변수 필요 — 중단")
        return 1

    t0 = time.time()
    run_date = datetime.now(_ET).strftime("%Y-%m-%d %H:%M")
    print(f"[START] 신호 백테스트 run_date={run_date} (ET) · confirm={CONFIRM_DAYS}d cooldown={COOLDOWN_DAYS}d")

    gc = get_gspread_client()

    universe, segment_map = load_universe(gc)
    print(f"[STEP1] 유니버스 {len(universe)}종목")
    if not universe:
        print("[INFO] 유니버스 비어 있음 — 중단")
        return 0

    # v2.9: 두 호출 모두 창 요구를 명시한다. 같은 값을 두 번 쓰는 것이 중복처럼
    #       보이지만, SPY 와 유니버스가 서로 다른 창을 받으면 초과수익 계산이
    #       조용히 다른 기간 대비가 된다 — 기본값에 맡기면 그 갈림이 안 보인다.
    spy_hist, _spy_kind = _fmp_price_history("SPY", bars=HISTORY_BARS)
    if spy_hist.empty:
        print(f"[WARN] SPY 이력 fetch 실패({_spy_kind}) — 초과수익 NaN 으로 진행")
    hist_cache, _reasons, _failed = _batch_fetch_history(universe,
                                                        bars=HISTORY_BARS)

    _n_uni = len(universe)
    _rate = (len(hist_cache) / _n_uni) if _n_uni else 1.0
    print(f"[STEP2] 이력 확보 {len(hist_cache)}/{_n_uni}종목 ({_rate * 100:.1f}%) "
          f"(SPY {'OK' if not spy_hist.empty else '실패:' + _spy_kind})")
    if _reasons:
        print("[STEP2] 사유별 — " + " · ".join(
            f"{k}={v}" for k, v in sorted(_reasons.items(), key=lambda kv: -kv[1])))
    print("[STEP2] " + fh.fmp_stats_line())
    if _failed:
        _head = ", ".join(f"{t}({k})" for t, k in sorted(_failed)[:20])
        print(f"[STEP2] 탈락 {len(_failed)}종목 — {_head}"
              + (" …" if len(_failed) > 20 else ""))

    # v2.8: 부분 유니버스는 시트에 넣지 않는다. 크기만 다른 정상처럼 보이는 행이
    #   섞이면 run 간 비교가 조용히 오염되고, 사후에 골라낼 방법이 없다.
    if _rate < MIN_FETCH_RATE:
        print(f"[ABORT] 페치 성공률 {_rate * 100:.1f}% < 임계 "
              f"{MIN_FETCH_RATE * 100:.1f}% — 시트에 기록하지 않고 중단한다.")
        print("[ABORT] 위 '사유별' 을 볼 것. rate_limited 가 많으면 "
              "FMP_RATE_LIMIT_PER_MIN 을 낮추고, exception 이 많으면 "
              "_FMP_TIMEOUT 을 늘린다.")
        return 1

    aggs, rt_aggs, meta = run_backtest(universe, spy_hist, hist_cache,
                                       segment_map=segment_map)
    _print_summary(aggs, meta)
    _print_rt_summary(rt_aggs, meta)

    rows = []
    for seg in SEGMENTS:
        _sz = (meta.get("seg_size") or {}).get(seg, meta["universe_size"])
        _hs = (meta.get("seg_hash") or {}).get(seg, "")
        rows += build_result_rows(aggs.get(("alert", seg), {}), run_date,
                                  meta["hist_start"], meta["hist_end"],
                                  _sz, buckets=ALERT_BUCKETS,
                                  mode="alert", segment=seg, uni_hash=_hs)
        rows += build_result_rows(aggs.get(("verdict", seg), {}), run_date,
                                  meta["hist_start"], meta["hist_end"],
                                  _sz, mode="verdict", segment=seg, uni_hash=_hs)
    try:
        ws = open_result_worksheet(gc)
        _safe_append_rows(ws, rows, ncols=len(_RESULT_COLS))
        print(f"[OK] '{_RESULT_WORKSHEET}' 에 {len(rows)}행 저장")
    except Exception as e:
        print(f"[ERROR] 결과 저장 실패: {e}")
        return 1

    try:
        _rt_rows = build_rt_rows(rt_aggs, run_date, meta["hist_start"], meta["hist_end"],
                                 meta["universe_size"],
                                 seg_size=meta.get("seg_size"),
                                 seg_hash=meta.get("seg_hash"))
        _rtws = open_result_worksheet(gc, title=_RT_WORKSHEET, cols=_RT_COLS)
        _safe_append_rows(_rtws, _rt_rows, ncols=len(_RT_COLS))
        print(f"[OK] '{_RT_WORKSHEET}' 에 {len(_rt_rows)}행 저장")
    except Exception as e:
        print(f"[WARN] 왕복 결과 저장 실패(본 결과는 저장됨): {e}")

    # gs_retry 위임이 실제로 살아 있는지 배포 직후 눈으로 확인하는 줄이다.
    # 이 줄이 안 보이면 gs_retry.py 락스텝 업로드가 빠진 것이다.
    print("[GS] " + gsr.stats_line())
    print(f"[DONE] {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
