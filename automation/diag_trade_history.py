#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""실제 매매 이력 분석 (19B) — Trade_History FIFO 실현손익 · 진입 사유별 성과.

배경
────
백테스트(run_signal_backtest)는 '규칙을 100% 따랐다면' 을 가정한다. 실제로는
신호가 떴는데 안 산 날, 신호 없이 산 날, 리밸런싱이 신호 포지션을 밀어낸 날이 있다.
이 스크립트는 그 실행 갭을 본다. 백테스트가 구조적으로 볼 수 없는 유일한 데이터다.

설계 (확정)
──────────
  20A FIFO      : 먼저 산 것을 먼저 판 것으로 매칭(부분 매도 자연 처리)
  21A 실현분만  : 미청산 잔량은 집계 제외(백테스트 코호트와 같은 논리)
  22  리밸런싱  : 같은 날·같은 계좌에서 REBAL_MIN_LOTS 건 이상 동시 매도 → 리밸런싱 분류
                  (사용자 확인: 2026-06-01 / 2026-07-20 대량 매도는 리밸런싱)

한계 (해석 시 반드시 감안)
─────────────────────────
  · 매도 사유(H열)가 대부분 비어 있어 '청산 사유별' 분해는 불가능하다.
  · 기간이 짧고 신호 매수 표본이 한 자릿수일 수 있다 → 통계가 아니라 사례 검토다.
  · SPY 대비는 진입일~청산일 동일 캘린더 창(백테스트 Excess_vs_SPY 와 같은 기준).

읽기 전용. 시트에 아무것도 쓰지 않는다.
실행:  python automation/diag_trade_history.py
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict, deque

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_signal_backtest as bt  # noqa: E402

TRADE_SHEET = "Trade_History"
REBAL_MIN_LOTS = 5          # 같은 날·같은 계좌 동시 매도 N건 이상 → 리밸런싱
SPY_TICKER = "SPY"

# 진입 사유 분류 — memo(H열) 텍스트 기준. 순서대로 먼저 맞는 것을 채택.
ENTRY_TAGS = [
    ("신호(최적 매수 타이밍)", ("최적 매수 타이밍",)),
    ("신호(얼리버드)", ("얼리버드",)),
    ("신호(기타)", ("[진입:",)),
    ("정기적립(DCA)", ("정기 적립", "DCA")),
    ("재량/미기재", ()),
]


def classify_entry(memo: str) -> str:
    m = str(memo or "")
    for label, keys in ENTRY_TAGS:
        if label.startswith("정기적립") and any(k in m for k in keys):
            return label
        if keys and any(k in m for k in keys):
            # DCA 태그가 '[진입:' 안에 함께 들어오므로 DCA 를 먼저 걸러낸다
            if "정기 적립" in m or "DCA" in m:
                return "정기적립(DCA)"
            return label
    return "재량/미기재"


def load_trades(gc) -> pd.DataFrame:
    sh = bt._gs(gc.open, bt._SPREADSHEET_TITLE)
    ws = bt._gs(sh.worksheet, TRADE_SHEET)
    vals = bt._gs(ws.get_all_values) or []
    if not vals:
        return pd.DataFrame()
    cols = ["user_id", "account", "ticker", "action", "shares", "price", "date", "memo"]
    rows = []
    for r in vals:
        if not r or not str(r[0]).strip():
            continue
        r = list(r) + [""] * (len(cols) - len(r))
        rows.append(r[:len(cols)])
    df = pd.DataFrame(rows, columns=cols)
    # 헤더 행이 있으면 제거
    df = df[df["user_id"].astype(str).str.lower() != "user_id"]
    df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["action"] = df["action"].astype(str).str.upper().str.strip()
    df = df.dropna(subset=["shares", "price", "date"])
    df = df[df["shares"] > 0]
    return df.sort_values("date", kind="stable").reset_index(drop=True)


def mark_rebalance(df: pd.DataFrame) -> pd.DataFrame:
    """같은 날·같은 계좌에서 여러 종목을 동시에 판 매도 = 리밸런싱."""
    df = df.copy()
    df["is_rebal"] = False
    sells = df[df["action"] == "SELL"]
    grp = sells.groupby([sells["date"].dt.date, "account"])["ticker"].transform("size")
    df.loc[sells.index, "is_rebal"] = (grp >= REBAL_MIN_LOTS).to_numpy()
    return df


def fifo_match(df: pd.DataFrame) -> pd.DataFrame:
    """계좌·티커별 FIFO 매칭 → 실현 거래 목록."""
    books = defaultdict(deque)     # (account, ticker) -> deque of dict lots
    out = []
    unmatched_sell = 0
    for _, r in df.iterrows():
        key = (r["account"], r["ticker"])
        if r["action"] == "BUY":
            books[key].append({"qty": float(r["shares"]), "px": float(r["price"]),
                               "date": r["date"], "memo": r["memo"]})
            continue
        if r["action"] != "SELL":
            continue
        qty = float(r["shares"])
        while qty > 1e-9 and books[key]:
            lot = books[key][0]
            take = min(qty, lot["qty"])
            out.append({
                "account": r["account"], "ticker": r["ticker"],
                "entry_date": lot["date"], "exit_date": r["date"],
                "entry_px": lot["px"], "exit_px": float(r["price"]),
                "qty": take,
                "entry_reason": classify_entry(lot["memo"]),
                "exit_memo": str(r["memo"] or ""),
                "is_rebal": bool(r["is_rebal"]),
            })
            lot["qty"] -= take
            qty -= take
            if lot["qty"] <= 1e-9:
                books[key].popleft()
        if qty > 1e-9:
            unmatched_sell += 1
    open_lots = sum(len(v) for v in books.values())
    print(f"[INFO] FIFO 매칭 완료 — 실현 {len(out)}건 · 미청산 로트 {open_lots}건 · "
          f"매수기록 없는 매도 {unmatched_sell}건")
    if not out:
        return pd.DataFrame()
    t = pd.DataFrame(out)
    t["ret_pct"] = (t["exit_px"] / t["entry_px"] - 1.0) * 100.0
    t["hold_days"] = (t["exit_date"] - t["entry_date"]).dt.days
    t["pnl"] = (t["exit_px"] - t["entry_px"]) * t["qty"]
    t["cost"] = t["entry_px"] * t["qty"]
    return t


def attach_spy(t: pd.DataFrame) -> pd.DataFrame:
    """진입일~청산일 같은 창의 SPY 수익률을 붙여 초과수익(알파) 산출."""
    # ⚠️ v2.8 부터 (DataFrame, kind) 튜플이다. 단일 이름으로 받으면 바로 아래
    #    `.empty` 에서 AttributeError 가 나고, main() 이 감싸지 않아 STEP3 직전에
    #    통째로 죽는다 — STEP1·STEP2 를 다 출력한 뒤라 더 헷갈린다.
    spy, _spy_kind = bt._fmp_price_history(SPY_TICKER, limit=bt.HISTORY_LIMIT)
    if spy is None or spy.empty:
        print(f"[WARN] SPY 이력 fetch 실패({_spy_kind}) — 초과수익(excess_pct) 생략")
        t["excess_pct"] = np.nan
        return t
    c = pd.to_numeric(spy["Close"], errors="coerce").dropna()

    def at(d):
        try:
            s = c.loc[:pd.Timestamp(d)]
            return float(s.iloc[-1]) if len(s) else np.nan
        except Exception:
            return np.nan

    s0 = t["entry_date"].map(at)
    s1 = t["exit_date"].map(at)
    spy_ret = (s1 / s0 - 1.0) * 100.0
    t["spy_pct"] = spy_ret
    t["excess_pct"] = t["ret_pct"] - spy_ret
    return t


def _agg(sub: pd.DataFrame) -> dict:
    if sub.empty:
        return {}
    w = float((sub["ret_pct"] > 0).mean() * 100)
    cost = sub["cost"].sum()
    return {
        "n": len(sub),
        "win": round(w, 1),
        "avg_ret": round(float(sub["ret_pct"].mean()), 2),
        "med_ret": round(float(sub["ret_pct"].median()), 2),
        "wavg_ret": round(float(sub["pnl"].sum() / cost * 100), 2) if cost else np.nan,
        "hold": round(float(sub["hold_days"].mean()), 1),
        "excess": round(float(sub["excess_pct"].mean()), 2) if sub["excess_pct"].notna().any() else np.nan,
        "pnl": round(float(sub["pnl"].sum()), 2),
    }


def _table(title: str, groups: list, note: str = "") -> None:
    print(f"\n── {title} ──")
    if note:
        print(f"   {note}")
    print(f"{'구분':<26}{'N':>5}{'승률':>7}{'평균%':>8}{'중앙%':>8}"
          f"{'금액가중%':>10}{'보유일':>8}{'SPY초과%':>10}{'실현손익$':>11}")
    for label, sub in groups:
        a = _agg(sub)
        if not a:
            print(f"{label:<26}{0:>5}{'-':>7}{'-':>8}{'-':>8}{'-':>10}{'-':>8}{'-':>10}{'-':>11}")
            continue
        f = lambda v: "-" if (v is None or (isinstance(v, float) and not np.isfinite(v))) else v
        print(f"{label:<26}{a['n']:>5}{a['win']:>7}{a['avg_ret']:>8}{a['med_ret']:>8}"
              f"{f(a['wavg_ret']):>10}{a['hold']:>8}{f(a['excess']):>10}{a['pnl']:>11}")


def main() -> int:
    if not hasattr(bt, "fh"):
        print("[ERR] run_signal_backtest 가 v2.8 이전 버전이다 "
              "(fmp_http 미도입) — 두 파일을 함께 배포할 것")
        return 1
    if not bt.FMP_API_KEY:
        print("[WARN] FMP_API_KEY 없음 — SPY 초과수익은 생략됩니다")
    gc = bt.get_gspread_client()
    raw = load_trades(gc)
    if raw.empty:
        print("[ERR] Trade_History 가 비어 있거나 읽을 수 없습니다")
        return 1

    print(f"[STEP1] 원본 {len(raw)}행 · 기간 {raw['date'].min().date()} ~ {raw['date'].max().date()}")
    print(f"        BUY {int((raw['action'] == 'BUY').sum())} · "
          f"SELL {int((raw['action'] == 'SELL').sum())} · "
          f"계좌 {raw['account'].nunique()} · 종목 {raw['ticker'].nunique()}")

    raw = mark_rebalance(raw)
    n_rb = int(raw["is_rebal"].sum())
    print(f"[STEP2] 리밸런싱 매도 {n_rb}행 (같은 날·같은 계좌 {REBAL_MIN_LOTS}건 이상 동시 매도)")

    # 매수 태그 구성 — 실행 갭 진단의 핵심 숫자
    buys = raw[raw["action"] == "BUY"].copy()
    buys["tag"] = buys["memo"].map(classify_entry)
    print("\n── 매수 기록 구성 (신호 vs 재량) ──")
    vc = buys["tag"].value_counts()
    for k, v in vc.items():
        print(f"   {k:<26}{v:>5}건 ({v / len(buys) * 100:>5.1f}%)")

    t = fifo_match(raw)
    if t.empty:
        print("[ERR] 실현 거래가 없습니다")
        return 1
    t = attach_spy(t)

    print(f"\n[STEP3] 실현 {len(t)}건 · 총 투입 ${t['cost'].sum():,.0f} · "
          f"총 실현손익 ${t['pnl'].sum():,.0f} "
          f"({t['pnl'].sum() / t['cost'].sum() * 100:+.2f}%)")

    _table("전체 / 매도 성격별", [
        ("전체", t),
        ("리밸런싱 매도", t[t["is_rebal"]]),
        ("개별 매도", t[~t["is_rebal"]]),
    ], note="리밸런싱은 신호 매도가 아니므로 아래 분석에서는 개별 매도만 본다.")

    ind = t[~t["is_rebal"]]
    _table("진입 사유별 (개별 매도만)",
           [(k, ind[ind["entry_reason"] == k]) for k in
            sorted(ind["entry_reason"].unique())],
           note="'신호' 가 '재량/DCA' 보다 SPY초과가 높아야 엔진이 값을 하는 것이다.")

    _table("진입 사유별 (리밸런싱 포함 전체)",
           [(k, t[t["entry_reason"] == k]) for k in sorted(t["entry_reason"].unique())])

    _table("계좌별 (개별 매도만)",
           [(k, ind[ind["account"] == k]) for k in sorted(ind["account"].unique())])

    # 신호 매수 건은 표본이 작을 수 있어 개별 나열
    sig = t[t["entry_reason"].str.startswith("신호")]
    if not sig.empty and len(sig) <= 40:
        print(f"\n── 신호 진입 실현 건 전체 나열 ({len(sig)}건) ──")
        print(f"{'티커':<8}{'계좌':<18}{'진입':>12}{'청산':>12}{'보유':>6}"
              f"{'수익%':>8}{'SPY%':>8}{'초과%':>8}{'리밸':>6}")
        for _, r in sig.sort_values("entry_date").iterrows():
            f = lambda v: "-" if not np.isfinite(v) else f"{v:.2f}"
            print(f"{r['ticker']:<8}{str(r['account'])[:17]:<18}"
                  f"{str(r['entry_date'].date()):>12}{str(r['exit_date'].date()):>12}"
                  f"{r['hold_days']:>6}{r['ret_pct']:>8.2f}{f(r['spy_pct']):>8}"
                  f"{f(r['excess_pct']):>8}{'Y' if r['is_rebal'] else '':>6}")

    print("\n[해석 주의]")
    print("  · 매도 사유(H열)가 대부분 비어 있어 '청산 사유별' 분해는 불가능하다.")
    print("  · 표본이 작으면 통계가 아니라 사례 검토로 읽을 것.")
    print("  · SPY초과 = 같은 보유 창에서 SPY 를 들고 있었을 때 대비(베타 제거).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
