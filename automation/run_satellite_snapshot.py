"""run_satellite_snapshot.py — 위성 슬리브 월별 평가액 스냅샷.

무엇을 위한 것인가
──────────────────
`SATELLITE_MANDATE.md` §4③(자본 손상 −30%)은 **고점**을 알아야 판정된다.
고점을 잴 시계열이 없어서 그 조항은 지금까지 의도 선언이었다. 이 러너가
그 시계열을 만든다. ①(전제 붕괴)·②(실행 실패)와 달리 ③은 **자동으로만**
채워질 수 있다 — 사람이 매달 기억해서 적는 기록은 하락장에서 가장 먼저
끊기고, 하필 그때가 필요한 때다.

왜 run_hidden_alpha 에 얹지 않았나
──────────────────────────────────
두 가지 이유다.

  1) run_hidden_alpha 는 ISO 주 가드로 **일요일 1회**만 돈다. 월말이 화요일이면
     스냅샷 날짜가 "그 달 마지막 일요일"이 된다. md §4③ 은 "월말"이라고
     못박혀 있으므로, 문서와 코드가 처음부터 갈라진 채로 시작한다.
  2) run_hidden_alpha 에는 랭킹 산출 실패 시 `sys.exit(1)` 경로가 있다.
     데이터가 모자라 랭킹이 죽는 상황은 **시장이 나쁠 때 더 잘 일어난다.**
     ③이 가장 필요한 구간에서 기록이 조용히 빠진다.

독립 러너로 두면 다른 스텝의 성패와 무관하게 돌고, 평일 5PM 에 매일 붙어
있으므로 월말이 무슨 요일이든 잡는다.

무엇을 재나
───────────
`Portfolios` 시트에서 관리자(users_core.ADMIN_CONTENT_OWNER_ID)의
`fmp_extras.SATELLITE_BOOKS` 두 계좌 보유를 읽어, 대상일 종가로 평가한다.

⚠️ 현금은 재지 않는다. 두 장부의 현금은 현재 추적되지 않으므로 `Cash` 열은
   **공란**으로 남긴다. 0 으로 채우면 "재서 0이었다"가 되어, 나중에 실제로
   추적을 시작했을 때 과거 행과 구분이 안 된다. 이 한계는 md §4③·§6 에
   명시돼 있고 `diag_satellite_mandate.py` 가 문서와 코드를 대조한다.

⚠️ 보유 0종목인 장부는 평가액이 0 이 아니라 **모른다**. 행은 남기되
   `fmp_extras.satellite_sleeve_totals` 가 그 날짜를 합산에서 제외한다 —
   0 으로 합치면 시장 필터에 따라 전량 현금화한 달에 슬리브가 반토막 난 것으로
   찍혀 −50% 가 가짜로 발동한다. 규칙을 지킨 행동이 트리거를 당기면 안 된다.

실행
────
    FMP_API_KEY=.. GSPREAD_KEY=.. python automation/run_satellite_snapshot.py
        → 오늘이 그 달 **마지막 거래일**이면 2행 기록, 아니면 스킵(exit 0)

    ... python automation/run_satellite_snapshot.py --seed
        → 개시일(fmp_extras.SATELLITE_START_DATE) 기준선 1행씩. **최초 1회.**

    ... python automation/run_satellite_snapshot.py --check
        → 시트를 읽어 현재 낙폭만 출력. 쓰기 없음. FMP 콜 0.

    DRY_RUN=1   계산만 하고 시트에 쓰지 않는다 (읽기는 한다)
    FORCE=1     월말 가드 무시 / 시드 경과일 경고 무시

FMP 콜: 보유 종목 수만큼(최대 10). 월 1회. 무시할 수준이다.
"""
import argparse
import json
import os
import sys
import traceback
from datetime import datetime

import gspread
import pytz
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import calendar_core as cc      # noqa: E402  — 거래일 판정 SSOT
import fmp_extras as fx         # noqa: E402  — 위성 상수·스키마·낙폭 SSOT
import gs_retry as gsr          # noqa: E402
import portfolio_core as pc     # noqa: E402  — Portfolios 정규화 SSOT
import users_core as uc         # noqa: E402  — 관리자 uid

GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")
DRY_RUN = str(os.environ.get("DRY_RUN", "") or "").strip() in ("1", "true", "TRUE", "yes")
FORCE = str(os.environ.get("FORCE", "") or "").strip() in ("1", "true", "TRUE", "yes")

# ── SNAPSHOT_MODE — 수동 워크플로가 고른 값을 **그대로** 받는다 ──────────────
# 왜 매핑을 여기 두나
# ───────────────────
# yml 에서 `${{ inputs.mode == 'seed' && '1' || '' }}` 같은 표현식으로 플래그를
# 만들면, 선택지가 늘 때마다 yml 을 고쳐야 하고 **언젠가 안 고쳐진다**
# (2026-08-22 LIVENESS_FORCE 실제 사고). yml 은 문자열 하나만 넘기고 해석은
# 여기서 한다 — 매핑이 한 곳에 있고, 진단이 그 한 곳을 검사할 수 있다.
#
# ⚠️ 모르는 값은 **죽인다.** 조용히 기본(monthly)으로 떨어지면, 월말이 아닌 날
#    "[SKIP] 마지막 거래일이 아닙니다" 만 찍고 exit 0 으로 끝난다. Actions 는
#    초록불이고 사용자는 시드가 만들어졌다고 믿는다. 그 실패는 보이지 않는다.
SNAPSHOT_MODES = {
    "":              {"seed": False, "check": False},                  # 정기 실행
    "monthly":       {"seed": False, "check": False},
    "check":         {"seed": False, "check": True},                   # 읽기 전용
    "seed_dryrun":   {"seed": True,  "check": False, "dry": True},     # 시드 미리보기
    "seed":          {"seed": True,  "check": False},                  # 시드 확정
    "force_monthly": {"seed": False, "check": False, "force": True},   # 월말 실패 복구
}
SNAPSHOT_MODE = str(os.environ.get("SNAPSHOT_MODE", "") or "").strip().lower()

_SPREADSHEET_TITLE = "Quant_DB"
_ET = pytz.timezone("US/Eastern")
_KST = pytz.timezone("Asia/Seoul")

# 대상일 종가를 찾기 위해 거슬러 올라갈 깊이.
#   월말 실행: 당일이 거래일이므로 꼬리 몇 봉이면 충분하다.
#   시드 실행: 개시일이 휴장(2026-09-07 = Labor Day)일 수 있고, 실행이 며칠
#             늦어질 수도 있어 여유를 둔다.
# ⚠️ 기본값을 만들지 않는다 — 두 요구가 다르다(fmp_extras._closes 와 같은 규약).
BARS_MONTHLY = 8
BARS_SEED = 25


def get_gspread_client():
    creds = Credentials.from_service_account_info(
        json.loads(GSPREAD_KEY_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)


def _open_or_create_snapshot_ws(sh):
    """스냅샷 시트를 열거나 헤더와 함께 만든다."""
    try:
        return gsr.call(sh.worksheet, fx.SATELLITE_SNAPSHOT_SHEET), False
    except gspread.exceptions.WorksheetNotFound:
        ws = gsr.call(sh.add_worksheet, title=fx.SATELLITE_SNAPSHOT_SHEET,
                      rows=200, cols=len(fx.SATELLITE_SNAPSHOT_COLS) + 4)
        gsr.call(ws.update, [fx.SATELLITE_SNAPSHOT_COLS], range_name="A1",
                 value_input_option="USER_ENTERED")
        return ws, True


def _safe_append_rows(ws, rows) -> None:
    """헤더 아래 첫 빈 행을 명시 지정해 append (app.py._safe_append_rows 동일 규약).

    gspread 의 암묵적 append 는 시트에 빈 행이 섞여 있으면 엉뚱한 위치를
    고른다. 범위를 직접 계산해 넘긴다.
    """
    if not rows:
        return
    existing = gsr.call(ws.get_all_values) or []
    start = len(existing) + 1
    need = start + len(rows) + 10
    if need > ws.row_count:
        gsr.call(ws.add_rows, need - ws.row_count)
    gsr.call(ws.update, rows, range_name=f"A{start}",
             value_input_option="USER_ENTERED")


def load_satellite_holdings(sh) -> dict:
    """{book: {TICKER: qty}} — 관리자의 위성 두 장부 보유 수량.

    보유가 없는 장부도 **키는 만든다.** 키가 없으면 그 장부의 행 자체가
    안 써지고, 짝이 안 맞는 날짜가 되어 그 달이 통째로 판정에서 빠진다.
    """
    out = {b: {} for b in fx.SATELLITE_BOOKS}
    ws = gsr.call(sh.worksheet, "Portfolios")
    owner = str(uc.ADMIN_CONTENT_OWNER_ID).strip().upper()
    for rid, acct, tk, _avg, qty, _dt in pc.load_rows(ws):
        if str(rid).strip().upper() != owner:
            continue
        acct = str(acct).strip()
        if acct not in out:
            continue
        try:
            q = float(str(qty).replace(",", "").strip() or 0.0)
        except Exception:
            q = 0.0
        if q <= 0:
            continue
        out[acct][str(tk).strip().upper()] = out[acct].get(str(tk).strip().upper(), 0.0) + q
    return out


def price_map(tickers, target: str, bars: int) -> tuple:
    """{TICKER: close} + 실제 사용된 날짜 집합. 창 계산은 fmp_extras 가 소유한다."""
    prices, used, failed = {}, set(), []
    for tk in sorted(set(tickers)):
        try:
            c, d = fx.satellite_close_on_or_before(tk, target, bars=bars)
        except Exception as exc:
            c, d = None, ""
            print(f"[WARN] {tk} 종가 조회 예외: {exc}")
        if c is None or c <= 0:
            failed.append(tk)
            continue
        prices[tk] = c
        if d:
            used.add(d)
    return prices, used, failed


def is_last_trading_day_of_month(d) -> bool:
    """오늘이 그 달의 마지막 거래일인가 (calendar_core 규칙 계산 · 콜 0)."""
    if not cc.is_market_open(d):
        return False
    return cc.next_trading_day(d).month != d.month


def do_check(sh) -> int:
    ws, created = _open_or_create_snapshot_ws(sh)
    if created:
        print("[INFO] 스냅샷 시트를 새로 만들었습니다 — 아직 데이터가 없습니다.")
        return 0
    rows = fx.parse_satellite_snapshots(gsr.call(ws.get_all_values) or [])
    dd = fx.satellite_drawdown(rows)
    print(f"[CHECK] 스냅샷 행 {len(rows)}건 · 합산 가능 시점 {len(dd['series'])}개")
    for s in dd["series"]:
        print(f"        {s['date']}  ${s['total']:>12,.2f}  "
              f"슬롯 {s['slots']}/{s['slots_max']}  {s['source']}")
    print("[CHECK] " + fx.satellite_drawdown_line(dd))
    if dd["incomplete_dates"]:
        print(f"[WARN] 한 장부만 기록된 날짜(합산 제외): {dd['incomplete_dates']}")
    if dd["unvaluable_dates"]:
        print(f"[WARN] 빈 장부 + 현금 미추적으로 평가 불가한 날짜: {dd['unvaluable_dates']}")
    return 0


def do_snapshot(sh, mode: str) -> int:
    now_et = datetime.now(_ET)
    today = now_et.date()

    if mode == "seed":
        target = fx.SATELLITE_START_DATE
        bars = BARS_SEED
        source = fx.SATELLITE_SNAP_SEED
        gap = (today - datetime.strptime(target, "%Y-%m-%d").date()).days
        if gap > 14 and not FORCE:
            print(f"[ABORT] 개시일({target})로부터 {gap}일 지났습니다. 시드는 "
                  f"**오늘 보유**를 개시일 종가로 평가하므로, 그 사이 매매가 "
                  f"있었다면 기준선이 거짓이 됩니다. 정말 진행하려면 FORCE=1.")
            return 1
        if gap > 0:
            print(f"[WARN] 개시일로부터 {gap}일 경과 — 그 사이 위성 매매가 "
                  f"있었다면 시드 기준선이 실제와 다릅니다.")
    else:
        target = today.strftime("%Y-%m-%d")
        bars = BARS_MONTHLY
        source = fx.SATELLITE_SNAP_MONTHLY
        if not is_last_trading_day_of_month(today):
            if not FORCE:
                print(f"[SKIP] {target} 은 이 달의 마지막 거래일이 아닙니다. 종료. "
                      f"(강제 실행은 FORCE=1)")
                return 0
            print(f"[WARN] FORCE=1 — 월말이 아닌 {target} 에 기록합니다.")

    ws, created = _open_or_create_snapshot_ws(sh)
    if created:
        print(f"[INFO] `{fx.SATELLITE_SNAPSHOT_SHEET}` 시트를 새로 만들었습니다.")

    existing = fx.parse_satellite_snapshots(gsr.call(ws.get_all_values) or [])
    have = {(r["date"], r["book"]) for r in existing}

    holdings = load_satellite_holdings(sh)
    for b in fx.SATELLITE_BOOKS:
        print(f"[INFO] {b}: 보유 {len(holdings[b])}종목 {sorted(holdings[b])}")

    all_tk = [tk for b in fx.SATELLITE_BOOKS for tk in holdings[b]]
    prices, used_dates, failed = ({}, set(), [])
    if all_tk:
        prices, used_dates, failed = price_map(all_tk, target, bars)
    if failed:
        # 한 종목이라도 값을 못 받으면 그 장부 평가액이 과소 계상되고,
        # 그 과소 계상은 **낙폭을 부풀려** 트리거를 앞당긴다. 조용히 넘어가면
        # 그 달의 숫자가 영구히 틀린 채 시계열에 남는다.
        print(f"[ABORT] 종가 미확보 {len(failed)}종목: {failed} — 기록하지 않습니다. "
              f"재실행하거나 FMP 키/네트워크를 확인하세요.")
        return 1
    if used_dates:
        print(f"[INFO] 사용된 종가 일자: {sorted(used_dates)} (대상일 {target})")

    rows, skipped = [], []
    for b in fx.SATELLITE_BOOKS:
        if (target, b) in have:
            skipped.append(b)
            continue
        hold = {tk: {"qty": q, "close": prices[tk]} for tk, q in holdings[b].items()}
        row = fx.satellite_snapshot_row(target, b, hold, cash=None, source=source)
        rows.append(row)
        i_mv = fx.SATELLITE_SNAPSHOT_COLS.index("Holdings_MV")
        i_sl = fx.SATELLITE_SNAPSHOT_COLS.index("Slots_Filled")
        print(f"[ROW] {target} {b}: MV ${row[i_mv]:,.2f} · 슬롯 {row[i_sl]}/{fx.SATELLITE_SLOTS}")
        if row[i_sl] == 0:
            print("      ⚠️ 보유 0종목 — 현금 미추적이라 이 날짜는 합산에서 제외됩니다.")

    if skipped:
        print(f"[SKIP] 이미 기록된 (날짜, 장부): {[(target, b) for b in skipped]}")
    if not rows:
        print("[DONE] 새로 쓸 행이 없습니다.")
        return 0

    if DRY_RUN:
        print(f"[DRY_RUN] {len(rows)}행을 기록하지 않고 종료합니다.")
        for r in rows:
            print("          ", r)
        return 0

    _safe_append_rows(ws, rows)
    print(f"[OK] {len(rows)}행 기록 완료.")

    after = fx.parse_satellite_snapshots(gsr.call(ws.get_all_values) or [])
    dd = fx.satellite_drawdown(after)
    print("[STATE] " + fx.satellite_drawdown_line(dd))
    if dd.get("triggered"):
        print("[ALERT] 🚨 §4③ 트리거 도달 — 양쪽 장부를 동시에 절반으로 축소해야 합니다.")
    return 0


def main() -> int:
    global DRY_RUN, FORCE

    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true",
                    help="개시일 기준선 1회 기록")
    ap.add_argument("--check", action="store_true",
                    help="읽기 전용 — 현재 낙폭만 출력")
    args = ap.parse_args()

    print("=" * 60)
    print(f"[START] 위성 슬리브 스냅샷: {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")

    want_seed, want_check = args.seed, args.check
    if SNAPSHOT_MODE not in SNAPSHOT_MODES:
        print(f"[ERROR] 알 수 없는 SNAPSHOT_MODE={SNAPSHOT_MODE!r}. "
              f"가능: {sorted(k for k in SNAPSHOT_MODES if k)}. 종료.")
        return 1
    if SNAPSHOT_MODE:
        cfg = SNAPSHOT_MODES[SNAPSHOT_MODE]
        want_seed = cfg["seed"] or want_seed
        want_check = cfg["check"] or want_check
        DRY_RUN = cfg.get("dry", DRY_RUN) or DRY_RUN
        FORCE = cfg.get("force", FORCE) or FORCE
        print(f"[MODE] {SNAPSHOT_MODE} → seed={want_seed} check={want_check} "
              f"DRY_RUN={DRY_RUN} FORCE={FORCE}")

    if not GSPREAD_KEY_JSON:
        print("[ERROR] GSPREAD_KEY 미설정. 종료.")
        return 1

    sh = gsr.call(get_gspread_client().open, _SPREADSHEET_TITLE)
    if want_check:
        rc = do_check(sh)
    else:
        rc = do_snapshot(sh, "seed" if want_seed else "monthly")
    print(f"[DONE] {datetime.now(_KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 60)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
