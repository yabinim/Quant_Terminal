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
    DRY_RUN=1 ...     # 시트 쓰기 없이 계산만 (읽기는 한다)
    FORCE=1 ...       # 이미 데이터가 있는 시트를 백필로 덮어쓸 때만

⚠️ 백필은 시트를 **전체 교체**한다. 이미 행이 있으면 FORCE=1 없이는 거부한다 —
   그동안 쌓인 일일 append 가 날아가는 것이 149콜을 다시 태우는 것보다 아프다.
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
# 백필은 시트를 **전체 교체**한다. 이미 데이터가 있으면 기본적으로 거부하고,
# FORCE=1 일 때만 덮어쓴다. 실수로 반복 실행되는 것을 UI 가 아니라 코드가 막는다
# (버튼을 없애면 실행 경로 자체가 사라져서, 정작 필요할 때 돌릴 방법이 없다).
FORCE = str(os.environ.get("FORCE", "") or "").strip() in ("1", "true", "TRUE", "yes")

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
    print("[MODE] 백필 — historical-industry-performance"
          + ("  (FORCE)" if FORCE else ""))

    # ── 덮어쓰기 가드 ────────────────────────────────────────────────
    # 백필은 ws.clear() 후 전체를 다시 쓴다. 이미 쌓인 일일 append 가
    # 있으면 그게 날아간다. 149콜을 다시 태우는 것보다 그쪽이 더 아프다.
    if sh is not None:
        try:
            ws0 = gsr.call(sh.worksheet, ic.PERF_SHEET)
            n_exist = len([r for r in (gsr.call(ws0.get_all_values) or [])[1:]
                           if r and str(r[0]).strip()])
        except Exception:
            n_exist = 0
        if n_exist > 0 and not FORCE:
            print("[ABORT] " + ic.PERF_SHEET + " 에 이미 " + str(n_exist)
                  + "행이 있습니다.")
            print("        백필은 시트를 전체 교체하므로 그동안 쌓인 일일")
            print("        append 가 사라집니다. 정말 재구성하려면 FORCE=1.")
            print("        (평소에는 백필이 아니라 일일 모드만 돌면 됩니다)")
            return 1
        if n_exist > 0:
            print("  ⚠️ 기존 " + str(n_exist) + "행을 덮어씁니다 (FORCE).")

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
def _spearman(a_map, b_map):
    """두 순위의 스피어만 상관. 공통 키만 쓴다. 표본 부족이면 None."""
    keys = [k for k in a_map if k in b_map
            and a_map[k] is not None and b_map[k] is not None]
    n = len(keys)
    if n < 10:
        return None
    ra = sorted(keys, key=lambda k: a_map[k])
    rb = sorted(keys, key=lambda k: b_map[k])
    pa = {k: i for i, k in enumerate(ra)}
    pb = {k: i for i, k in enumerate(rb)}
    d2 = sum((pa[k] - pb[k]) ** 2 for k in keys)
    return 1.0 - (6.0 * d2) / (n * (n * n - 1))


def do_ranks(sh):
    """현재 순위 조회 — **API 호출 0**. 시트만 읽는다.

    왜 필요한가
    ───────────
    시트에 3년치가 들어갔는데 눈으로 확인할 방법이 없었다. 일일 append 의
    `[RANK]` 요약은 append 가 성공해야 찍히는데, 백필 당일은 중복 가드에
    걸려 SKIP 이라 안 나온다.

    화면(Phase 3 표시)을 만들기 전에 **데이터가 말이 되는지** 먼저 본다.
    여기서 이상하면 화면을 만들어봐야 헛수고다.
    """
    print("[MODE] 순위 조회 (API 호출 0 · 시트 읽기만)")
    try:
        ws = gsr.call(sh.worksheet, ic.PERF_SHEET)
    except Exception:
        print("[ABORT] " + ic.PERF_SHEET + " 시트가 없습니다. 먼저 --backfill.")
        return 1

    parsed = ic.parse_perf_values(gsr.call(ws.get_all_values) or [])
    dates = parsed.get("dates") or []
    if not dates:
        print("[ABORT] 파싱 결과가 비었습니다. 헤더 첫 열이 'Date' 인지 확인.")
        return 1

    print("  행 " + str(len(dates)) + " · 열(업종) "
          + str(len(parsed["industries"])))
    print("  기간 " + dates[0] + " ~ " + dates[-1])

    # ── 신선도 ──────────────────────────────────────────────────────────
    last_td = cc.prev_trading_day(datetime.now(_ET).date() + timedelta(days=1))
    gap = 0
    d = last_td
    while d.isoformat() != dates[-1] and gap < 15:
        d = cc.prev_trading_day(d)
        gap += 1
    if gap == 0:
        print("  ✅ 최신 (마지막 거래일까지 반영)")
    elif gap < 15:
        print("  ⚠️ " + str(gap) + " 거래일 뒤처짐 — 일일 append 확인 필요")
    else:
        print("  🔴 15 거래일 이상 뒤처짐 — 일일 append 가 안 돌고 있다")

    ranks = ic.compute_ranks(parsed)

    for lb in ic.LOOKBACKS:
        key = "pct_%d" % lb
        mkey = "mom_%d" % lb
        rows = [(v[key], nm, v.get(mkey), ic.describe(v, lookback=lb))
                for nm, v in ranks.items() if v.get(key) is not None]
        n_excl = len(ranks) - len(rows)
        print("")
        print("  " + "=" * 74)
        print("  " + str(lb) + "일 모멘텀 — 모집단 " + str(len(rows)) + "개"
              + ("  (결측으로 제외 " + str(n_excl) + "개)" if n_excl else ""))
        print("  " + "=" * 74)
        if not rows:
            print("    (산출 가능한 업종 없음)")
            continue
        rows.sort()
        n_uns = sum(1 for nm, v in ranks.items()
                    if v.get("stable_%d" % lb) is False)
        print("    안정성: 불안정 " + str(n_uns) + "개 (원본 vs 윈저화 백분위 "
              + "%.0f%%p 초과 차이)" % ic.STABILITY_GAP)
        print("    ── 상위 15   (⚠️ = 불안정 · 화면에서 걸러야 할 후보)")
        for p, nm, m, desc in rows[:15]:
            flag = "" if ranks[nm].get("stable_%d" % lb) is not False else "  ⚠️"
            print("      %5.1f%%  %-42s %+7.1f%%%s" % (p, nm[:42], 100.0 * m, flag))
        print("    ── 하위 5")
        for p, nm, m, desc in rows[-5:]:
            flag = "" if ranks[nm].get("stable_%d" % lb) is not False else "  ⚠️"
            print("      %5.1f%%  %-42s %+7.1f%%%s" % (p, nm[:42], 100.0 * m, flag))

    # ── 온전성 검사 ─────────────────────────────────────────────────────
    # 두 창의 순위가 완전히 같으면 창 하나가 무의미하다는 뜻이고,
    # 완전히 무관하면 둘 중 하나가 잡음일 가능성이 있다.
    if len(ic.LOOKBACKS) >= 2:
        a, b = ic.LOOKBACKS[0], ic.LOOKBACKS[1]
        ma = {nm: v.get("pct_%d" % a) for nm, v in ranks.items()}
        mb = {nm: v.get("pct_%d" % b) for nm, v in ranks.items()}
        rho = _spearman(ma, mb)
        print("")
        print("  ── 온전성 검사")
        if rho is None:
            print("     " + str(a) + "일 vs " + str(b) + "일 순위 상관: 표본 부족")
        else:
            print("     " + str(a) + "일 vs " + str(b) + "일 순위 상관: %.2f" % rho)
            if rho > 0.95:
                print("     ⚠️ 거의 동일 — 창을 둘 다 보여줄 실익이 없다")
            elif rho < 0.10:
                print("     ⚠️ 거의 무관 — 둘 중 하나가 잡음일 수 있다")
            else:
                print("     ✅ 적당히 다르다 — 두 창을 함께 보여줄 의미가 있다")

    # ── 극단값 영향 진단 (윈저화) ───────────────────────────────────────
    # averageChange 는 업종 내 상장 종목 전체의 **동일가중** 평균이라
    # 마이크로캡·투기적 소형주가 업종 평균을 통째로 흔든다.
    # 실측: Tobacco 120일 -51.8%, Agricultural Inputs -75.4% — 같은 기간
    # 실제 담배 대형주(MO/PM/BTI)는 올랐다. 이 버킷은 그 업종이 아니다.
    #
    # 다만 '크기가 왜곡됐다'가 곧 '순서도 무작위다'는 아니다. 백테스트에서
    # 무작위 30회 중 1회만 최고 설정을 이겼으므로 순위에는 정보가 있었다.
    # 추측으로 정하지 말고 **재본다.**
    clip = ic.WINSOR_CLIP
    ranks_w = ic.compute_ranks(parsed, clip=clip)
    print("")
    print("  ── 극단값 영향 진단 (윈저화 ±%.0f%%/일)" % clip)

    data = parsed.get("data") or {}
    for lb in ic.LOOKBACKS:
        ext = tot = 0
        for sr in data.values():
            e, t = ic.count_extremes(sr, clip, lookback=lb)
            ext += e
            tot += t
        pctx = (100.0 * ext / tot) if tot else 0.0
        key = "pct_%d" % lb
        ra = {nm: v.get(key) for nm, v in ranks.items()}
        rb = {nm: v.get(key) for nm, v in ranks_w.items()}
        rho = _spearman(ra, rb)
        print("")
        print("     [" + str(lb) + "일] 클립 대상 일간값 " + str(ext) + "/"
              + str(tot) + " (%.1f%%)" % pctx)
        if rho is None:
            print("            원본 vs 윈저화 순위 상관: 표본 부족")
            continue
        # ⚠️ 전체 순위 상관만 보면 안 된다.
        #    합성 검증에서 rho=0.932 인데도 1위 업종이 2.0% → 91.3% 로
        #    튀는 경우가 나왔다. 149개 중 대부분이 안 움직이면 전체 상관은
        #    높게 유지되지만, **정작 화면에 뜨는 건 상위권**이다.
        #    상위 K개 교집합을 따로 재서 둘 중 나쁜 쪽으로 판정한다.
        K = 15
        top_a = [nm for _, nm in sorted((ra[nm], nm) for nm in ra
                                        if ra.get(nm) is not None)[:K]]
        top_b = [nm for _, nm in sorted((rb[nm], nm) for nm in rb
                                        if rb.get(nm) is not None)[:K]]
        ov = len(set(top_a) & set(top_b))
        ov_r = ov / float(K) if K else 0.0
        print("            원본 vs 윈저화 순위 상관: %.3f" % rho)
        print("            상위 %d 교집합: %d/%d (%.0f%%)  ← 화면에 뜨는 구간"
              % (K, ov, K, 100.0 * ov_r))

        if rho >= 0.90 and ov_r >= 0.80:
            print("            ✅ 극단값이 **순서까지 흔들지는 않았다**")
            print("               → 원본 유지. 화면에는 백분위만 표시하면 된다")
        elif rho < 0.70 or ov_r < 0.50:
            print("            🔴 순서가 크게 바뀐다 — 윈저화 채택 권장")
            if rho >= 0.90:
                print("               (전체 상관은 높지만 상위권이 갈린다."
                      " 화면 기준으로는 이쪽이 결정적이다)")
        else:
            print("            🟠 일부 바뀐다 — 아래 변동 목록을 보고 판단")
            if rho >= 0.90 > ov_r + 0.10:
                print("               (전체 상관이 높은 것에 속으면 안 된다 —"
                      " 상위권이 덜 겹친다)")

        gone = [nm for nm in top_a if nm not in set(top_b)]
        if gone:
            print("            원본 상위 %d 중 윈저화하면 빠지는 업종: %s"
                  % (K, ", ".join(g[:26] for g in gone[:6])))

        # 순위가 가장 크게 움직인 업종 — 어떤 이름이 문제인지 눈으로 본다
        moved = [(abs(ra[nm] - rb[nm]), nm, ra[nm], rb[nm])
                 for nm in ra if ra.get(nm) is not None and rb.get(nm) is not None]
        if moved:
            moved.sort(reverse=True)
            print("            순위 변동 상위 5 (원본 → 윈저화):")
            for d_, nm, x, y in moved[:5]:
                print("              %-40s %5.1f%% → %5.1f%%  (%+.1f%%p)"
                      % (nm[:40], x, y, y - x))

    # 방향 표시가 실제로 작동하는지 (전부 '—' 면 DIRECTION_LAG 가 무의미)
    lb0 = ic.LOOKBACKS[0]
    descs = [ic.describe(v, lookback=lb0) for v in ranks.values()]
    up = sum(1 for d_ in descs if "↑" in d_)
    dn = sum(1 for d_ in descs if "↓" in d_)
    fl = sum(1 for d_ in descs if "—" in d_)
    print("     방향 분포(" + str(lb0) + "일): ↑" + str(up)
          + " ↓" + str(dn) + " —" + str(fl))
    if up + dn == 0 and fl > 0:
        print("     ⚠️ 전부 '변화 없음' — DIRECTION_LAG 재검토 필요")
    return 0


# ══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="149콜 일회성 백필. 시트를 전체 재구성한다.")
    ap.add_argument("--ranks", action="store_true",
                    help="현재 순위 출력. API 호출 0, 시트 읽기만.")
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
        print("[ABORT] GSPREAD_KEY 없음. 일일/순위 모드는 시트를 읽어야 합니다.")
        return 1

    if args.ranks:
        rc = do_ranks(sh)
    elif args.backfill:
        rc = do_backfill(sh)
    else:
        rc = do_daily(sh)
    print("[END] 완료" if rc == 0 else "[END] 오류")
    return rc


if __name__ == "__main__":
    sys.exit(main())
