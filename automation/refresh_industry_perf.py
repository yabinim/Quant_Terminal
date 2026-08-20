"""refresh_industry_perf.py — 업종 성과 히스토리 백필 + 일일 append.

두 가지 모드를 한 스크립트에 둔 이유
────────────────────────────────────
둘 다 같은 시트(`Industry_Perf`)의 같은 헤더 규약을 다룬다. 스크립트를 나누면
헤더 순서 규약이 두 곳에 생기고, 어긋나면 **과거 데이터가 통째로 밀린다**.
같은 파일에 두면 규약이 하나다.

    --backfill   historical-industry-performance × 149콜 → 754행 (일회성)
    (기본)       industry-performance-snapshot × 1콜 → 1행 append (매 평일)

왜 히스토리를 직접 쌓나
───────────────────────
스냅샷은 **하루치 변화율**이라 그것만으로 149개를 줄 세우면 잡음이다
("상위 8%"가 내일 "하위 30%"). 20일·120일 모멘텀이 필요한데, 매일 API 로
받으려면 149콜/일이 든다.

한 번 백필해 두면 이후 유지비가 **하루 1콜**이다.

시트 형식 — 와이드
──────────────────
    Date        | Advertising Agencies | Aerospace & Defense | ...
    2023-08-21  | 0.42                 | -1.13               | ...

롱 포맷이면 149행/일 × 754일 = 11만 행이다. 와이드는 754행이다.
셀 수는 같지만 Sheets 는 행 수에 훨씬 민감하다.

⚠️ 열 순서는 **헤더가 SSOT**. 새 업종이 생겨도 기존 열을 밀지 않고 끝에 붙인다.
   밀면 과거 데이터가 어긋나는데, 그건 조용히 일어나고 되돌리기 어렵다.

⚠️ 휴장일 행은 제외한다. FMP 는 휴장일에도 행을 주는데(3년 구간에서 +23행)
   그날 값은 무의미하다. calendar_core 로 거른다.

실행
────
    FMP_API_KEY=.. GSPREAD_KEY=.. python automation/refresh_industry_perf.py --backfill
    FMP_API_KEY=.. GSPREAD_KEY=.. python automation/refresh_industry_perf.py
    DRY_RUN=1 ...     # 시트 쓰기 없이 계산만
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import gspread
import pytz
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import calendar_core as cc   # noqa: E402  — 휴장일 필터 SSOT
import industry_core as ic   # noqa: E402  — 업종 모멘텀 SSOT
import gs_retry as gsr       # noqa: E402

FMP_API_KEY = str(os.environ.get("FMP_API_KEY", "") or "").strip()
GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")
DRY_RUN = str(os.environ.get("DRY_RUN", "") or "").strip() in ("1", "true", "TRUE", "yes")

_SPREADSHEET_TITLE = "Quant_DB"
_ET = pytz.timezone("US/Eastern")
_KST = pytz.timezone("Asia/Seoul")

BACKFILL_YEARS = 3
SLEEP_SEC = 0.20


def get_gspread_client():
    creds = Credentials.from_service_account_info(
        json.loads(GSPREAD_KEY_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)


def _open_or_create(sh, title, rows, cols):
    try:
        return gsr.call(sh.worksheet, title)
    except Exception:
        print("[INFO] " + title + " 시트를 새로 만듭니다.")
        return gsr.call(sh.add_worksheet, title=title, rows=rows, cols=cols)


# ══════════════════════════════════════════════════════════════════════════
def do_backfill(sh):
    print("[MODE] 백필 — historical-industry-performance")
    today = datetime.now(_ET).date()
    d_to = today.isoformat()
    d_from = (today - timedelta(days=365 * BACKFILL_YEARS)).isoformat()

    names = ic.fetch_industries(FMP_API_KEY)
    if not names:
        print("[ABORT] available-industries 조회 실패.")
        return 1
    print("  분류 " + str(len(names)) + "개 · " + d_from + " ~ " + d_to)

    # {업종: {날짜: 값}}
    table, empty, dropped = {}, [], 0
    for n, nm in enumerate(names, 1):
        recs, err = ic.fetch_history(FMP_API_KEY, nm, d_from, d_to)
        if err or not recs:
            empty.append(nm)
        else:
            col = {}
            for r in recs:
                if not isinstance(r, dict):
                    continue
                ds = str(r.get("date") or "").strip()[:10]
                if len(ds) != 10:
                    continue
                if not cc.is_market_open(ds):     # ⚠️ 휴장일 행 제거
                    dropped += 1
                    continue
                try:
                    col[ds] = float(r.get("averageChange"))
                except Exception:
                    continue
            if col:
                table[nm] = col
            else:
                empty.append(nm)
        if n % 25 == 0:
            print("    ... " + str(n) + "/" + str(len(names)))
        time.sleep(SLEEP_SEC)

    if empty:
        print("  ⚠️ 데이터 없음 " + str(len(empty)) + "개: "
              + ", ".join(empty[:8]) + (" 외" if len(empty) > 8 else ""))
    if not table:
        print("[ABORT] 유효 데이터 0개.")
        return 1
    print("  ✅ 유효 " + str(len(table)) + "개 · 휴장일 제거 " + str(dropped) + "행")

    inds = sorted(table)
    all_dates = sorted({d for col in table.values() for d in col})
    header = [ic.DATE_COL] + inds
    body = []
    for ds in all_dates:
        row = [ds]
        for nm in inds:
            v = table[nm].get(ds)
            row.append("" if v is None else v)
        body.append(row)
    print("  구성: " + str(len(body)) + "행 × " + str(len(header)) + "열 ("
          + all_dates[0] + " ~ " + all_dates[-1] + ")")

    if DRY_RUN:
        print("[DRY_RUN] 시트 쓰기 생략.")
        return 0

    ws = _open_or_create(sh, ic.PERF_SHEET, len(body) + 50, len(header) + 20)
    gsr.call(ws.clear)
    gsr.call(ws.resize, rows=len(body) + 50, cols=len(header) + 20)
    gsr.call(ws.update, [header] + body, range_name="A1",
             value_input_option="USER_ENTERED")
    print("  저장 완료 " + str(len(body)) + "행")
    return 0


# ══════════════════════════════════════════════════════════════════════════
def do_daily(sh):
    print("[MODE] 일일 append — industry-performance-snapshot")
    today = datetime.now(_ET).date()

    # 스냅샷 대상일: 오늘이 거래일이면 오늘, 아니면 직전 거래일.
    # 5PM ET 실행이므로 당일 종가가 나와 있다.
    d = today if cc.is_market_open(today) else cc.prev_trading_day(today)
    ds = d.isoformat()
    print("  대상일: " + ds + (" (휴장일이라 직전 거래일)" if d != today else ""))

    try:
        ws = gsr.call(sh.worksheet, ic.PERF_SHEET)
    except Exception:
        print("[ABORT] " + ic.PERF_SHEET + " 시트가 없습니다. 먼저 --backfill 을 "
              "실행하세요. (일일 append 는 헤더 규약을 백필에서 물려받는다)")
        return 1

    values = gsr.call(ws.get_all_values) or []
    if not values or not values[0] or str(values[0][0]).strip() != ic.DATE_COL:
        print("[ABORT] 헤더가 비정상입니다. --backfill 로 재구성하세요.")
        return 1
    header = [str(c).strip() for c in values[0] if str(c).strip()]

    # 이미 있는 날짜면 중복 append 하지 않는다. 워크플로가 재실행돼도 안전해야 한다.
    existing = {str(r[0]).strip()[:10] for r in values[1:] if r}
    if ds in existing:
        print("  [SKIP] " + ds + " 행이 이미 있습니다. 중복 append 안 함.")
        return 0

    recs, err = ic.fetch_snapshot(FMP_API_KEY, ds)
    if err or not recs:
        print("[WARN] 스냅샷 미수신(" + (err or "빈 응답") + "). "
              "표시 데이터가 하루 낡을 뿐, 판정에는 영향 없음.")
        return 0
    row, new_inds = ic.build_row(header, recs, ds)
    filled = sum(1 for v in row[1:] if v != "")
    print("  수신 " + str(len(recs)) + "건 · 헤더 매칭 " + str(filled)
          + "/" + str(len(header) - 1))

    if new_inds:
        # ⚠️ 조용히 버리지 않는다. 새 업종은 헤더 끝에 붙여야 과거 열이 안 밀린다.
        print("  🆕 헤더에 없는 신규 업종 " + str(len(new_inds)) + "개: "
              + ", ".join(new_inds[:6]))
        print("     → 헤더 끝에 추가합니다(기존 열 순서는 그대로).")

    if DRY_RUN:
        print("[DRY_RUN] append 생략. 행 길이 " + str(len(row)))
        return 0

    if new_inds:
        new_header = header + new_inds
        need = len(new_header) - int(ws.col_count)
        if need > 0:
            gsr.call(ws.add_cols, need + 5)
        gsr.call(ws.update, [new_header], range_name="A1",
                 value_input_option="USER_ENTERED")
        row, _ = ic.build_row(new_header, recs, ds)

    gsr.call(ws.append_row, row, value_input_option="USER_ENTERED",
             table_range="A1")
    print("  append 완료: " + ds)

    # 참고 출력 — 상위/하위 몇 개를 로그에 남겨 눈으로 확인 가능하게
    try:
        parsed = ic.parse_perf_values(gsr.call(ws.get_all_values) or [])
        ranks = ic.compute_ranks(parsed)
        lb = ic.LOOKBACKS[0]
        ok = [(v["pct_%d" % lb], nm) for nm, v in ranks.items()
              if v.get("pct_%d" % lb) is not None]
        if ok:
            ok.sort()
            print("  [RANK] 기준일 " + str(parsed["dates"][-1])
                  + " · 모집단 " + str(len(ok)) + "개 (" + str(lb) + "일 모멘텀)")
            for p, nm in ok[:5]:
                print("     상위 %5.1f%%  %s" % (p, nm))
    except Exception as e:
        print("  (순위 요약 생략: " + str(e)[:60] + ")")
    return 0


# ══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="149콜 일회성 백필. 시트를 전체 재구성한다.")
    args = ap.parse_args()

    print("=" * 60)
    print("[START] 업종 성과 히스토리: "
          + datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
          + ("  (DRY_RUN)" if DRY_RUN else ""))
    print("  industry_core v" + ic.INDUSTRY_CORE_VERSION)

    if not FMP_API_KEY:
        print("[ABORT] FMP_API_KEY 없음.")
        return 1

    # ⚠️ DRY_RUN 이라도 시트는 **연다**. 일일 모드는 헤더를 읽어야 행을 만들 수
    #    있어서, 시트 없이는 리허설 자체가 불가능하다.
    #    DRY_RUN 이 막는 것은 '쓰기'지 '읽기'가 아니다.
    #    (백필 모드는 시트를 안 읽으므로 키가 없어도 계산까지는 돌아간다.)
    sh = None
    if GSPREAD_KEY_JSON:
        try:
            sh = gsr.call(get_gspread_client().open, _SPREADSHEET_TITLE)
        except Exception as e:
            print("[ABORT] 시트 열기 실패: " + str(e))
            return 1
    elif args.backfill and DRY_RUN:
        print("[INFO] GSPREAD_KEY 없음 — 백필 계산만 리허설합니다.")
    else:
        print("[ABORT] GSPREAD_KEY 없음. 일일 모드는 헤더를 읽어야 합니다.")
        return 1

    rc = do_backfill(sh) if args.backfill else do_daily(sh)
    print("[END] 완료" if rc == 0 else "[END] 오류")
    return rc


if __name__ == "__main__":
    sys.exit(main())
