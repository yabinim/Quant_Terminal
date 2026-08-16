"""seed_reminders.py — 초기 리마인더 4건 적재 (일회성).

앱 UI 로도 추가할 수 있지만 What_To_Check 본문이 길다. 손으로 치다 보면
"내부자 블록 확인" 같은 한 줄로 줄어들고, 3개월 뒤에는 그게 무슨 뜻인지
모르게 된다. 그래서 스크립트로 넣는다.

멱등하다 — ID 가 같으면 덮어쓴다. 여러 번 돌려도 중복이 생기지 않는다.
기존 항목의 Status / Snoozed_Until 은 **보존한다.** 이미 완료·연기 처리한 것을
스크립트 재실행이 되살리면 안 된다.

실행
────
    DRY_RUN=1 python automation/seed_reminders.py     # 먼저 이걸로 확인
    python automation/seed_reminders.py
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reminders_core as rmc  # noqa: E402

DRY_RUN = str(os.environ.get("DRY_RUN", "") or "").strip() not in ("", "0")

SEEDS = [
    rmc.make(
        title="내부자 백필 재실행 판단",
        due="2026-10-15",
        category="검증",
        what_to_check=(
            "① Earnings_Events 행 수를 센다. "
            "② 30건 이상이면 backfill_insider_stats 워크플로를 DRY_RUN 으로 돌려 "
            "'결합 N행'을 확인하고, 유효 표본이 20행 이상이면 실제로 적재한다. "
            "③ 30건 미만이면 11-15 '내부자 거래 블록 유효성 검증' 리마인더를 "
            "60일 연기한다 — 소급 데이터 없이 그날 라이브 스냅샷만으로는 "
            "판단이 안 된다. "
            "④ 적재했다면 Insider_Backfill 시트에서 Insider_Sales_Q 와 "
            "Pre_Ret_D3_Pct 의 방향을 먼저 훑어본다."
        ),
        why=("2026-08-15 첫 DRY_RUN 에서 Earnings_Events 가 3건뿐이라 결합이 2행에 "
             "그쳤고, 그중 MBX 는 사전 수익률이 전부 공란이라 실질 표본이 1건이었다. "
             "'백필로 몇 주 안에 조기 판독'이라는 원래 계획이 성립하지 않았다. "
             "분기 지연은 37일로 양호해 방법론 자체는 유효하다 — 데이터만 없다."),
        source="2026-08-15 백필 DRY_RUN 결과",
    ),
    rmc.make(
        title="Hidden Alpha 위성 로테이션 재평가",
        due="2026-11-03",
        category="백테스트",
        what_to_check=(
            "① Trade_History 에서 위성 실거래를 뽑아 백테스트 기대치와 대조한다. "
            "② 주간 리밸런싱을 실제로 실행한 주와 건너뛴 주를 분리해서 본다 — "
            "성과가 나쁘면 전략 문제인지 실행 누락 문제인지 먼저 갈라야 한다. "
            "③ 로테이션 주기를 주간에서 월간으로 늦추는 대안과 비교한다. "
            "④ USO 처럼 롤 비용이 있는 선물형 ETF 가 다시 편입됐는지 확인하고, "
            "편입됐다면 보유 기간을 점검한다."
        ),
        why=("백테스트 표본이 부족해 실거래 3개월을 쌓고 판단하기로 했다. "
             "당시 리밸런싱 1주 누락과 USO 를 로테이션 신호 이후까지 보유한 건이 있었다."),
        source="2026-08-03 결정",
    ),
    rmc.make(
        title="내부자 거래 블록 유효성 검증",
        due="2026-11-15",
        category="검증",
        what_to_check=(
            "① Earnings_Preview 에서 Insider_Sale_Val_90d 와 "
            "Pre_Ret_D1/D3/D7_Pct 의 관계를 본다. "
            "② Insider_Cov_D 가 90 미만인 행은 금액이 잘린 값이므로 제외하거나 "
            "따로 본다. ③ Insider_Backfill 시트가 있으면 소급 데이터와 방향이 "
            "일치하는지 대조한다 — **없을 수 있다.** 10-15 리마인더에서 표본 부족으로 "
            "백필을 미뤘다면 이 항목도 함께 연기하는 것이 맞다. "
            "④ 스냅샷이 20건 미만이면 판단하지 말고 60일 연기한다 — 적은 표본으로 "
            "'관계 없음'을 결론내면 멀쩡한 신호를 버리게 된다. "
            "⑤ 충분한 표본에서 관계가 없으면 컬럼 4개를 폐기한다 — "
            "PREVIEW_COLS 30~33 제거 + run_earnings_watch 콜 5 제거로 "
            "스냅샷당 1콜을 되돌린다."
        ),
        why=("실적 레이더는 방향성 엣지가 확인되지 않아 radar-only 모드다. "
             "내부자 블록도 검증된 신호가 아니라 축적 중인 가설로 넣었다."),
        source="2026-08-15 C블록 확정",
    ),
    rmc.make(
        title="신호 기반 매매 성과 평가",
        due="2027-02-02",
        category="백테스트",
        what_to_check=(
            "① diag_trade_history.py 를 재실행해 진입 사유별 실현 성과를 낸다. "
            "② 이번에는 매도 태그가 있으므로 [매도:라벨] 과 [판정:...] 을 대조한다 — "
            "시스템이 매도 신호를 냈는데 다른 사유로 판 건, 반대로 신호가 없는데 "
            "판 건이 실행 갭이다. ③ 엔진 문제(신호가 틀림)와 실행 문제(신호를 "
            "안 따름)를 분리한 뒤에만 파라미터를 건드린다."
        ),
        why=("2026-08-02 당시 SELL 행에 사유가 3건뿐이었고 내용도 '많이 오름' 수준이라 "
             "왜 팔았는지 가릴 수 없었다. 실데이터 6~12개월을 쌓고 보기로 했다."),
        source="2026-08-02 결정",
    ),
]


def _open_ws():
    import json

    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ.get("GSPREAD_KEY", "") or ""
    if not raw:
        raise RuntimeError("GSPREAD_KEY 없음")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    sh = gspread.authorize(creds).open("Quant_DB")
    try:
        return sh.worksheet(rmc.REMINDERS_WORKSHEET)
    except Exception:
        ws = sh.add_worksheet(title=rmc.REMINDERS_WORKSHEET,
                              rows=300, cols=rmc.REMINDER_NCOL)
        print(f"[INIT] '{rmc.REMINDERS_WORKSHEET}' 시트 생성")
        return ws


def merge(existing: list, seeds: list) -> list:
    """ID 기준 병합. 기존 항목의 Status / Snoozed_Until 은 보존한다."""
    by_id = {r.get("ID"): r for r in existing}
    out = list(existing)
    for s in seeds:
        old = by_id.get(s["ID"])
        if old is None:
            out.append(s)
            continue
        merged = dict(s)
        merged["Status"] = old.get("Status") or s["Status"]
        merged["Snoozed_Until"] = old.get("Snoozed_Until") or ""
        merged["Created"] = old.get("Created") or s["Created"]
        out[out.index(old)] = merged
    return out


def main():
    if DRY_RUN:
        print("[DRY_RUN] 시트에 쓰지 않는다. 적재할 내용:\n")
        for s in SEEDS:
            print(f"  · {s['Due_Date']}  {s['Title']}  (ID {s['ID']})")
            print(f"      {s['What_To_Check'][:100]}...")
        return 0

    ws = _open_ws()
    existing = rmc.parse_reminders(ws.get_all_values() or [])
    rows = merge(existing, SEEDS)

    last = chr(64 + rmc.REMINDER_NCOL)
    body = [rmc.REMINDER_COLS] + [rmc.to_row(r) for r in rows]
    ws.clear()
    ws.update(body, range_name=f"A1:{last}{len(body)}",
              value_input_option="USER_ENTERED")
    print(f"[OK] 리마인더 {len(rows)}건 기록 (신규/갱신 {len(SEEDS)}건)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
