"""seed_reminders.py — 개발 리마인더 6건 적재 (멱등 · 재실행 가능).

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
        category="검증",
        what_to_check=(
            "⚠️ 이 시점은 A/B 개시(2026-09-07)로부터 2개월뿐이다. "
            "**성과를 판정하지 말 것.** 이번에 보는 것은 실행뿐이다. "
            "① Portfolios 에 hsa-위성-12-0 · hsa-위성-blend 두 장부가 실제로 서 있는가. "
            "② 9월·10월 월간 리밸런싱을 지시대로 실행했는가, 건너뛴 달이 있는가. "
            "③ 두 장부 사이에 자금을 옮긴 적이 없는가 — 옮겼다면 그 시점 이후 "
            "비교는 오염된 것이므로 SATELLITE_MANDATE §7 에 기록한다. "
            "④ USO 처럼 롤 비용이 있는 선물형 ETF 가 편입됐다면 보유 기간을 점검한다. "
            "⑤ 성과 판정은 2027-03-08 '위성 A/B 실거래 6개월 비교' 항목에서 한다."
        ),
        why=("원래는 실거래 3개월로 로테이션을 재평가하려던 항목이다. "
             "2026-09-07 A/B 분할로 측정 대상이 바뀌었고, 실행 이탈 기록이 "
             "아직 없어 전략 문제와 실행 문제를 가를 수 없다. "
             "그래서 성격을 성과 판정에서 실행 점검으로 바꿨다. "
             "당시 리밸런싱 1주 누락과 USO 를 로테이션 신호 이후까지 보유한 건이 있었다."),
        source="2026-08-03 결정 → 2026-09-07 A/B 분할로 갱신",
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
    rmc.make(
        title="위성 전제 점검 #1 (6개월 주기)",
        due="2027-03-08",
        category="백테스트",
        what_to_check=(
            "① diag_momentum_rule_compare 워크플로를 실행한다 (T2 그룹). "
            "② 12-0 이 blend 를 이긴 창이 **6개 중 4개 이상**인가? "
            "이 기준은 2026-09-07 에 결과를 보기 전에 정했다 — 미달했을 때 "
            "'5/6 은 아니지만 방향은 맞았다' 식으로 다시 협상하지 않는다. "
            "③ 미달이면 SATELLITE_MANDATE §4① 연속 카운트를 +1 하고 "
            "다음 점검을 6개월 뒤로 심는다. 3회 연속(18개월) 미달이면 "
            "슬리브 종료를 검토한다. "
            "④ 통과하면 연속 카운트를 0 으로 되돌린다. "
            "⑤ 이 점검은 **성과를 보지 않는다** — 수익률·QQQ/VOO 대비·"
            "개별 종목 손익은 판정에서 제외한다 (§5)."
        ),
        why=("A반을 12-0 으로 바꾼 근거는 2026-09-05 워크포워드 6/6 하나뿐이다. "
             "신호가 죽었는지 알 방법이 이것밖에 없다. 슬리브 추적오차가 "
             "연 약 17.5% 라 성과로는 운과 실력을 가를 수 없다."),
        source="SATELLITE_MANDATE §4①",
    ),
    rmc.make(
        title="위성 A/B 실거래 6개월 비교",
        due="2027-03-08",
        category="검증",
        what_to_check=(
            "① 2026-09-07 개시 이후 A반(hsa-위성-12-0)·B반(hsa-위성-blend) "
            "두 장부의 실현+평가 손익을 각각 낸다. "
            "② 짝지은 비교다 — 같은 시장·같은 시점이라 시장 방향은 상쇄된다. "
            "절대 수익률이 아니라 **두 장부의 격차**만 본다. "
            "③ 실행 이탈 기록이 켜져 있으면 지시대로 굴린 달만 골라서 본다. "
            "아직 없으면 격차를 신뢰하지 말고 관측값만 적어둔다. "
            "④ 겹침률도 함께 적는다 — 두 랭킹이 자주 같았다면 그 기간은 "
            "애초에 아무것도 재지 못한 것이다. "
            "⑤ **6개월로 결론내지 않는다.** 방향만 기록하고 다음 6개월로 넘긴다. "
            "⑥ 어느 한쪽으로 자본을 옮기지 않는다 (§1) — 옮기는 순간 "
            "측정하려던 값이 사라진다."
        ),
        why=("백테스트로는 후보 풀 편향(T6) 때문에 12-0 과 blend 의 절대 우열을 "
             "말할 수 없다. 같은 시장에서 실제 돈으로 나란히 굴리는 짝비교만이 "
             "답을 주기 때문에 슬리브를 두 장부로 갈랐다."),
        source="SATELLITE_MANDATE §1 · §2",
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
