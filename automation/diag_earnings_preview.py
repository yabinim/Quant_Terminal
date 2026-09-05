"""diag_earnings_preview.py — 실적 프리뷰 브리핑(2단계) 오프라인 회귀 검사.

FMP·Sheets 를 전혀 부르지 않는다. earnings_core 의 순수 함수만 합성 데이터로
검증한다. 배포 **전에** 돌려서 위험한 결함 유형을 잡는 것이 목적이다.

검사 항목
─────────
  1) 스키마      PREVIEW_COLS 29열 · 중복 없음 · Transcript_Summary 자리 확보
                 EVENTS_COLS 꼬리에 Pre_Ret 3열 (중간 삽입이면 실패)
  2) Phase 판정  D-7/D-3/최종 창, BMO/AMC 분기, dd=0 미발동, 창 비중첩
  3) B블록 산식  beat율·평균 서프라이즈·상대강도·의견 변화
                 → diag_earnings_preview_backtest 와 같은 값이 나와야 한다
  4) YoY 매칭    날짜 기반. 분기 누락·지연 종목에서 엉뚱한 분기를 잡지 않는지
  5) 행 조립     결측 → 공란 + Data_Flags, 왕복(preview_row → parse_preview)
  6) 뉴스 증분   since 경계, 상한, 깨진 JSON 방어

실행
────
    python automation/diag_earnings_preview.py
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

import earnings_core as ec  # noqa: E402

_FAIL = []
_N = [0]


def check(name, cond, detail=""):
    _N[0] += 1
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
        _FAIL.append(name)


def section(t):
    print(f"\n{'─' * 70}\n{t}\n{'─' * 70}")


# ══════════════════════════════════════════════════════════════════════
section("1) 스키마")

# ⚠️ 29 → 33 (2026-09-05 정정). 코드가 아니라 **이 단정문이 낡았다.**
#    C블록(내부자) 4열 — Insider_Sale_Val_90d · Insider_Sale_N_90d ·
#    Insider_Buy_Val_90d · Insider_Cov_D — 이 29~32번 자리, 즉 **맨 끝**에
#    규약대로 붙었는데 여기만 29 로 남아 있었다.
#    ⚠️ 그 결과 이 스위트는 그때부터 **항상 빨간불**이었다. 헤더는 "실패하면
#       배포하지 말 것" 이라고 하는데 언제나 실패하니 게이트 역할을 못 했다.
#       실패 개수만 보면 "원래 2개 실패하는 검사" 로 읽히고 아무도 안 본다.
#       열 수를 바꿀 때 이 줄을 같이 고치지 않으면 같은 일이 반복된다.
check("PREVIEW_COLS 33열", ec.PREVIEW_NCOL == 33, f"실제 {ec.PREVIEW_NCOL}")
check("열 이름 중복 없음", len(set(ec.PREVIEW_COLS)) == ec.PREVIEW_NCOL)
check("Transcript_Summary 자리 확보(3단계 예약)",
      "Transcript_Summary" in ec.PREVIEW_COLS)
check("Snapshot_ID 가 첫 열", ec.PREVIEW_COLS[0] == "Snapshot_ID")

# 꼬리 추가가 아니면 기존 행의 범위 지정이 깨진다 — 가장 위험한 회귀.
check("EVENTS_COLS 꼬리 = Pre_Ret 3열",
      ec.EVENTS_COLS[-3:] == ["Pre_Ret_D1_Pct", "Pre_Ret_D3_Pct", "Pre_Ret_D7_Pct"],
      f"실제 꼬리 {ec.EVENTS_COLS[-3:]}")
check("EVENTS_COLS 기존 순서 보존(Notes 가 -4)",
      ec.EVENTS_COLS[-4] == "Notes")
# 아래 두 검사는 변이 테스트에서 '중간 삽입'을 놓쳐 추가했다.
# 꼬리 3열만 보면, 중간에 같은 이름을 하나 더 끼워 넣어도 통과해버린다.
check("EVENTS_COLS 이름 중복 없음",
      len(set(ec.EVENTS_COLS)) == len(ec.EVENTS_COLS),
      f"중복 {[c for c in set(ec.EVENTS_COLS) if ec.EVENTS_COLS.count(c) > 1]}")
check("EVENTS_NCOL = 33 (기존 30 + 꼬리 3)",
      ec.EVENTS_NCOL == 33, f"실제 {ec.EVENTS_NCOL}")
check("snapshot_row 길이 = EVENTS_NCOL",
      len(ec.snapshot_row({"ticker": "T", "earnings_date": "2026-09-01"}, {})) == ec.EVENTS_NCOL)

# ══════════════════════════════════════════════════════════════════════
section("2) Phase 판정")

check("AMC 최종 = D-1", ec.preview_final_dd("amc") == 1)
check("BMO 최종 = D-2", ec.preview_final_dd("bmo") == 2)
check("timing 미상 → AMC 취급", ec.preview_final_dd("") == 1)

check("dd=7 → D7", ec.preview_phase(7, "amc") == "D7")
check("dd=6 → D7 (휴장 보정 창)", ec.preview_phase(6, "amc") == "D7")
check("dd=8 → D7 (휴장 보정 창)", ec.preview_phase(8, "amc") == "D7")
check("dd=3 → D3", ec.preview_phase(3, "amc") == "D3")
check("dd=4 → D3 (창)", ec.preview_phase(4, "amc") == "D3")
check("dd=1 AMC → FINAL", ec.preview_phase(1, "amc") == "FINAL")
check("dd=2 BMO → FINAL", ec.preview_phase(2, "bmo") == "FINAL")
check("dd=1 BMO → FINAL (지연 실행 보정)", ec.preview_phase(1, "bmo") == "FINAL")

# 발표 당일에는 절대 찍지 않는다 — AMC 면 5PM 실행이 발표 뒤다.
check("dd=0 → 미발동", ec.preview_phase(0, "amc") == "")
check("dd 음수 → 미발동", ec.preview_phase(-3, "bmo") == "")
check("dd=5 → 미발동(창 사이)", ec.preview_phase(5, "amc") == "")
check("dd=2 AMC → 미발동", ec.preview_phase(2, "amc") == "")
check("dd=9 → 미발동", ec.preview_phase(9, "bmo") == "")
check("dd None → 미발동", ec.preview_phase(None, "amc") == "")

# 창이 겹치면 하루에 두 Phase 가 발동해 스냅샷이 오염된다.
for tm in ("amc", "bmo", ""):
    seen = {}
    for dd in range(0, 15):
        p = ec.preview_phase(dd, tm)
        if p:
            seen.setdefault(p, []).append(dd)
    flat = [d for v in seen.values() for d in v]
    check(f"창 비중첩 ({tm or '미상'}) {seen}", len(flat) == len(set(flat)))

# ══════════════════════════════════════════════════════════════════════
section("3) B블록 산식 — 백테스트와 동일해야 한다")

past = [{"date": "2026-05-01", "eps_act": 1.10, "eps_est": 1.00},   # +10%
        {"date": "2026-02-01", "eps_act": 0.90, "eps_est": 1.00},   # -10%
        {"date": "2025-11-01", "eps_act": 2.10, "eps_est": 2.00},   # +5%
        {"date": "2025-08-01", "eps_act": 1.05, "eps_est": 1.00}]   # +5%
bs = ec.beat_stats(past)
check("beat율 = 75% (4분기 중 3회)", abs(bs["beat_rate_pct"] - 75.0) < 1e-6,
      str(bs))
check("평균 서프라이즈 = 2.5%", abs(bs["surprise_avg_pct"] - 2.5) < 1e-6, str(bs))
check("표본 수 = 4", bs["sample_n"] == 4)

# 표본 부족이면 산출을 거부해야 한다 (추정치로 지시 금지 원칙).
check("3분기 → 산출 거부", ec.beat_stats(past[:3])["ok"] is False)
check("거부 시 값은 None", ec.beat_stats(past[:3])["beat_rate_pct"] is None)
check("est=0 행은 제외(0 나눗셈 방어)",
      ec.beat_stats(past + [{"date": "2025-05-01", "eps_act": 1, "eps_est": 0}])["sample_n"] == 4)

# 상대강도 — 종목 +10%, SPY +4% → +6%p
idx = pd.date_range("2026-01-01", periods=40, freq="B")
stock = pd.DataFrame({"Close": [100.0] * 19 + [100.0] + [110.0] * 20}, index=idx)
stock = pd.DataFrame({"Close": [100.0] * 20 + [110.0] * 20}, index=idx)
spy = pd.DataFrame({"Close": [400.0] * 20 + [416.0] * 20}, index=idx)
rs = ec.rel_strength_pct(stock, spy, window=20)
check("상대강도 = +6.0%p", abs(rs - 6.0) < 1e-6, f"실제 {rs}")
check("SPY 없으면 절대 수익률",
      abs(ec.rel_strength_pct(stock, None, window=20) - 10.0) < 1e-6)
check("이력 부족 → None",
      ec.rel_strength_pct(stock.iloc[:5], spy, window=20) is None)

# 의견 변화 — look-ahead 차단이 핵심
series = [(pd.Timestamp("2026-01-01"), 50.0),
          (pd.Timestamp("2026-04-01"), 60.0),
          (pd.Timestamp("2026-07-01"), 70.0),
          (pd.Timestamp("2026-12-01"), 99.0)]   # 미래 — 절대 쓰이면 안 됨
drift, level = ec.grade_factors(series, pd.Timestamp("2026-08-01"), drift_days=90)
check("의견 수준 = 70 (미래 관측 미사용)", abs(level - 70.0) < 1e-6, f"실제 {level}")
check("의견 변화 = +10 (90일 전 대비)", abs(drift - 10.0) < 1e-6, f"실제 {drift}")
d2, l2 = ec.grade_factors(series, pd.Timestamp("2026-01-15"), drift_days=90)
check("기준일 이전 관측 없으면 변화는 None", d2 is None and abs(l2 - 50.0) < 1e-6)
check("빈 시계열 → (None, None)", ec.grade_factors([], pd.Timestamp("2026-08-01")) == (None, None))

# ══════════════════════════════════════════════════════════════════════
section("4) 전년 동기 매칭 (YoY)")

recs = ec.split_future_past([
    {"date": "2026-10-29", "_dt": pd.Timestamp("2026-10-29"),
     "eps_est": 2.0, "eps_act": None, "rev_est": 120e9, "rev_act": None},
    {"date": "2026-07-30", "_dt": pd.Timestamp("2026-07-30"),
     "eps_est": 1.8, "eps_act": 1.9, "rev_est": 100e9, "rev_act": 101e9},
    {"date": "2025-10-30", "_dt": pd.Timestamp("2025-10-30"),
     "eps_est": 1.6, "eps_act": 1.7, "rev_est": 95e9, "rev_act": 100e9},
], today=pd.Timestamp("2026-08-15"))
fut, pst = recs
check("미래 분기 선택 = 2026-10-29", fut and fut["date"] == "2026-10-29")
check("과거 분기 2건", len(pst) == 2)

py = ec.prior_year_quarter(pst, "2026-10-29")
check("전년 동기 = 2025-10-30 (인덱스가 아니라 날짜 매칭)",
      py and py["date"] == "2025-10-30", str(py))
check("매출 YoY = +20%", abs(ec.yoy_pct(120e9, py["rev_act"]) - 20.0) < 1e-6)
check("EPS YoY = +17.6%", abs(ec.yoy_pct(2.0, py["eps_act"]) - 17.647) < 0.01)

# 전년 분기가 아예 없으면 잘못된 짝을 만들면 안 된다
check("허용 오차 밖 → None",
      ec.prior_year_quarter([{"date": "2024-01-01", "_dt": pd.Timestamp("2024-01-01")}],
                            "2026-10-29") is None)
check("적자(음수) 기준 → None (오독 방지)", ec.yoy_pct(2.0, -1.0) is None)
check("0 기준 → None", ec.yoy_pct(2.0, 0.0) is None)
check("None 입력 → None", ec.yoy_pct(None, 5.0) is None)

# ══════════════════════════════════════════════════════════════════════
section("5) 행 조립 · 왕복")

ev = {"ticker": "nvda", "earnings_date": "2026-08-26", "days_until": 7, "timing": "amc"}
full = ec.preview_row(ev, "D7", {
    "price": 180.5, "exp_median_pct": 6.2, "exp_worst_pct": -11.0,
    "est_eps": 2.08, "est_eps_yoy_pct": 31.6, "est_revision_pct": 1.4,
    "est_revenue": 91.93e9, "est_revenue_yoy_pct": 34.9,
    "target_mean": 210.0, "target_upside_pct": 16.3,
    "rs_20d_pct": 4.1, "beat_rate_pct": 87.5, "surprise_avg_pct": 6.0,
    "grade_buy_pct": 88.0, "grade_drift_90d": 2.0, "sample_n_q": 8,
    "news_count": 3, "news_json": '[{"t":"x"}]', "flags": [],
}, now_et="2026-08-19 17:00 ET")
check("행 길이 = 29", len(full) == ec.PREVIEW_NCOL, str(len(full)))
check("티커 대문자화", full[ec.PREVIEW_COLS.index("Ticker")] == "NVDA")
check("Snapshot_ID = EventID_Phase",
      full[0] == "NVDA_2026-08-26_D7", full[0])
check("Transcript_Summary 는 공란", full[ec.PREVIEW_COLS.index("Transcript_Summary")] == "")

empty = ec.preview_row(ev, "FINAL",
                       {"flags": ["no_target", "no_grades"]}, now_et="x")
check("결측 → 공란(0 이 아님)",
      empty[ec.PREVIEW_COLS.index("Target_Mean")] == "")
# ⚠️ 기대값에 stale_caller 를 더했다(2026-09-05). 이것도 낡은 단정문이다.
#    preview_row 는 호출부가 내부자 값을 안 주면 [FATAL] 을 찍고 Data_Flags 에
#    stale_caller 를 남긴다 — **락스텝 감지 장치**다. 여기 합성 호출은 내부자
#    값을 일부러 안 주므로 그 플래그가 붙는 게 **정상**이다.
#    기대값에서 빼놓으면 락스텝 장치가 작동할 때마다 이 검사가 실패한다.
check("Data_Flags 기록(+ 내부자 미제공 → stale_caller)",
      empty[ec.PREVIEW_COLS.index("Data_Flags")]
      == "no_target,no_grades,stale_caller",
      empty[ec.PREVIEW_COLS.index("Data_Flags")])
check("Sample_N_Q 결측 → 0", empty[ec.PREVIEW_COLS.index("Sample_N_Q")] == 0)

back = ec.parse_preview([ec.PREVIEW_COLS, full, empty])
check("왕복 2행", len(back) == 2)
check("왕복 값 보존", str(back[0]["Est_EPS"]) == "2.08", str(back[0]["Est_EPS"]))
check("_row 부여(헤더 다음=2)", back[0]["_row"] == 2)
check("짧은 행 패딩", len(ec.parse_preview([ec.PREVIEW_COLS, ["A", "B"]])[0]) >= ec.PREVIEW_NCOL)

pidx = ec.preview_index(back)
check("중복 차단 인덱스", ("NVDA_2026-08-26", "D7") in pidx)
check("Phase 다르면 별개 키", ("NVDA_2026-08-26", "FINAL") in pidx)

# ══════════════════════════════════════════════════════════════════════
section("6) 뉴스 증분")

news = [{"date": "2026-08-14", "title": "새 기사", "site": "a", "url": "u1"},
        {"date": "2026-08-10", "title": "옛 기사", "site": "b", "url": "u2"},
        {"date": "2026-08-01", "title": "더 옛날", "site": "c", "url": "u3"}]
c, j = ec.news_digest(news, since="2026-08-12")
check("since 이후만 = 1건", c == 1, f"실제 {c}")
check("JSON 파싱 가능", len(ec.parse_news_json(j)) == 1)
c2, _ = ec.news_digest(news, since=None)
check("since 없으면 전체(상한 내) = 3건", c2 == 3)
c3, _ = ec.news_digest(news * 5, since=None, limit=5)
check("상한 5건 준수", c3 == 5)
c4, j4 = ec.news_digest([], since=None)
check("기사 없음 → 0건/공란", c4 == 0 and j4 == "")
check("깨진 JSON → 빈 목록(표시 안 죽음)", ec.parse_news_json("{not json") == [])
check("경계일 포함(since 당일)", ec.news_digest(news, since="2026-08-14")[0] == 1)

check("목표가 상승여력 +16.7%",
      abs(ec.target_upside_pct(210.0, 180.0) - 16.666) < 0.01)
check("목표가 이미 초과 → 음수", ec.target_upside_pct(150.0, 180.0) < 0)
check("가격 0 → None", ec.target_upside_pct(210.0, 0) is None)


# ══════════════════════════════════════════════════════════════════════
section("EPS 추정치 분기 아카이브 (est_archive_row)")
# 왜 여기 있나:
#   이 함수는 run_earnings_watch 가 부르는데, 그 러너는 main() 최상단에
#   휴장일 가드가 있어 **주말·공휴일엔 시트를 열기도 전에 종료**한다.
#   즉 배포 당일이 평일이 아니면 실행 경로로는 확인할 방법이 없다.
#   순수 함수라 여기서 합성 데이터로 검증한다 — 네트워크 0, 시트 0.
#
# 무엇을 지키나:
#   calendar_row 는 분기가 바뀌면 Est_History_JSON 을 "" 로 버린다.
#   그 직전에 est_archive_row 가 건지지 못하면 **그 분기 시계열은 영구히
#   사라진다.** 실패해도 러너는 [WARN] 한 줄만 남기고 초록불로 끝난다 —
#   조용한 유실이라 이 검사가 유일한 방어선이다.
_ARCH_HIST = ('[{"d":"2026-06-01","eps":2.1},{"d":"2026-06-15","eps":2.14},'
              '{"d":"2026-07-01","eps":2.22}]')
_ARCH_PREV = {"Ticker": "AAPL", "Earnings_Date": "2026-07-31", "Est_EPS": 2.22,
              "Est_History_JSON": _ARCH_HIST, "Est_Revision_Pct": 5.7}
_ai = {c: i for i, c in enumerate(ec.EST_ARCHIVE_COLS)}
_arow = ec.est_archive_row("AAPL", _ARCH_PREV, now_et="2026-11-01 08:00 ET")

check("아카이브 행 길이 == EST_ARCHIVE_NCOL",
      _arow is not None and len(_arow) == ec.EST_ARCHIVE_NCOL)
check("Snapshot_N = 3", _arow and _arow[_ai["Snapshot_N"]] == 3)
check("옛 Earnings_Date 보존 (새 날짜가 아니라)",
      _arow and _arow[_ai["Earnings_Date"]] == "2026-07-31")
check("이력 JSON 원문 보존",
      _arow and _arow[_ai["Est_History_JSON"]] == _ARCH_HIST)

# 거부되어야 하는 입력 — 하나라도 통과하면 쓰레기 행이 쌓인다
for _lbl, _p in (
        ("이력 없음", {**_ARCH_PREV, "Est_History_JSON": ""}),
        ("빈 배열", {**_ARCH_PREV, "Est_History_JSON": "[]"}),
        ("1점만 (리비전 계산 불가)",
         {**_ARCH_PREV, "Est_History_JSON": '[{"d":"2026-07-01","eps":2.2}]'}),
        ("깨진 JSON", {**_ARCH_PREV, "Est_History_JSON": "{not json"}),
        ("리스트 아님", {**_ARCH_PREV, "Est_History_JSON": '{"a":1}'}),
        ("Earnings_Date 없음", {**_ARCH_PREV, "Earnings_Date": ""}),
        ("dict 아님", None)):
    check(f"거부: {_lbl}", ec.est_archive_row("AAPL", _p) is None)

# ⚠️ 순서 검증. 이게 이 절의 핵심이다 — calendar_row 를 **먼저** 부르면
#    아카이브할 게 남지 않는다는 것을 실증한다. 러너에서 두 호출 순서가
#    뒤집히면 조용히 빈 손이 되므로, 여기서 그 사실을 못 박아 둔다.
_after = ec.calendar_row("AAPL", {"earnings_date": "2026-10-30"}, None,
                         today="2026-11-01", now_et="x",
                         prev=_ARCH_PREV, source="user")
_after_d = {c: _after[i] for i, c in enumerate(ec.CALENDAR_COLS)}
check("calendar_row 가 분기 전환 시 이력을 버린다 (아카이브가 유일한 기회)",
      json.loads(_after_d["Est_History_JSON"] or "[]") == []
      or len(json.loads(_after_d["Est_History_JSON"] or "[]")) < 3)
check("순서 뒤집으면 아무것도 못 건진다 (calendar_row 결과로는 None)",
      ec.est_archive_row("AAPL", _after_d) is None)

# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
if _FAIL:
    print(f"❌ {len(_FAIL)}/{_N[0]} 실패")
    for f in _FAIL:
        print(f"   · {f}")
    sys.exit(1)
print(f"✅ 전체 {_N[0]}개 검사 통과")
print("=" * 70)
