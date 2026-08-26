"""refresh_market_calendar.py — 시장 캘린더 주간 갱신 + 규칙 대조.

무엇을 하나
───────────
1) FMP `holidays-by-exchange` 에서 올해+내년 캘린더를 받는다
2) `Market_Calendar` 시트에 저장한다 (전체 교체)
3) **calendar_core 의 규칙 계산과 대조**해서 불일치를 보고한다
4) 규칙에 없는 휴장일(= 임시 휴장)이 나오면 `Reminders` 시트에 항목을 만든다

왜 필요한가 — 규칙으로 못 잡는 것
─────────────────────────────────
`calendar_core` 의 규칙 계산은 NYSE 정규 휴일 10개를 100% 재현한다(2025·2026
하드코딩 20개와 완전 일치를 회귀 테스트로 확인). 하지만 **임시 휴장**은 규칙이
없다.

    2025-01-09  카터 전 대통령 국장일   ← 기존 하드코딩 5벌 어디에도 없었다
    2018-12-05  부시 전 대통령 국장일

즉 2025-01-09 에 이 시스템의 자동화는 전부 헛돌았다. 그날 알림 상태머신의
확정 카운터가 진행됐고(휴장일에 진행시키지 않으려는 설계였는데), DRG 는 없는
종가를 검증하려 들었다.

이 스크립트는 그런 날을 **미리** 찾아낸다. FMP 가 알려주는 휴장일 중 규칙에
없는 것이 나오면 그게 임시 휴장 후보다.

왜 판정 경로에 넣지 않았나
──────────────────────────
다섯 개 자동화의 개장 여부 가드는 전부 **시트를 열기 전 최상단**에 있다.
거기에 시트/네트워크를 붙이면 "휴장일이라 즉시 종료"하는 실행에까지 왕복
비용이 붙는다. 그래서 판정은 규칙 계산(비용 0)이 하고, 이 스크립트는 주 1회
따로 돌면서 **규칙이 놓친 것만** 찾는다.

다중 사용자
───────────
시장 캘린더는 **전역 공유 데이터**다. 사용자별 소유 개념이 없고, 관리자
소유 1벌을 모두가 쓴다. 메일을 보내지 않으므로 라우팅도 없고, 개인이 켜고 끌
성질이 아니므로 Users 토글도 추가하지 않는다.

실행
────
    FMP_API_KEY=... GSPREAD_KEY=... python automation/refresh_market_calendar.py
    DRY_RUN=1 ...                       # 시트 쓰기 없이 대조 결과만 출력
"""
import json
import os
import sys
from datetime import datetime

import gspread
import pytz
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import calendar_core as cc      # noqa: E402  — 시장 캘린더 SSOT
import reminders_core as rc     # noqa: E402  — 리마인더 SSOT
import gs_retry as gsr          # noqa: E402  — Sheets 재시도 SSOT

FMP_API_KEY = str(os.environ.get("FMP_API_KEY", "") or "").strip()
GSPREAD_KEY_JSON = os.environ.get("GSPREAD_KEY", "")   # 시크릿 이름은 GSPREAD_KEY (타 자동화와 동일)
DRY_RUN = str(os.environ.get("DRY_RUN", "") or "").strip() in ("1", "true", "TRUE", "yes")

_SPREADSHEET_TITLE = "Quant_DB"
_ET = pytz.timezone("US/Eastern")
_KST = pytz.timezone("Asia/Seoul")


def get_gspread_client():
    creds = Credentials.from_service_account_info(
        json.loads(GSPREAD_KEY_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"],
    )
    return gspread.authorize(creds)


def _open_or_create(sh, title, rows, cols):
    try:
        return sh.worksheet(title)
    except Exception:
        print("[INFO] " + title + " 시트가 없어 새로 만듭니다.")
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def write_calendar(ws, rows):
    """전체 교체. 캘린더는 누적 데이터가 아니라 스냅샷이므로 append 하지 않는다.

    ⚠️ 행 수가 줄어드는 경우(예: 3년치 → 2년치)를 대비해 먼저 clear 한다.
       clear 없이 update 만 하면 예전 꼬리가 남아 파싱 결과가 오염된다.

    ⚠️ RAW 로 쓴다 — USER_ENTERED 는 이 시트를 조용히 망가뜨린다
    ────────────────────────────────────────────────────────────
    이 표는 8열이 **전부 리터럴 문자열**이다("2026-11-27" · "Y"/"N" ·
    "13:00" · Updated_At). USER_ENTERED 는 그중 셋을 숫자로 바꾼다.

      Adj_Close  "13:00"      → 0.5416666… (시간 값 + 시간 서식)
      Date       "2026-11-27" → 날짜 값. 시트 로케일 서식이 M/D/YYYY 면
                                get_all_values() 가 "11/27/2026" 을 준다.
                                parse_calendar_values 는 len==10 을 통과시킨 뒤
                                int(ds[:4]) = int("11/2") 에서 예외 → **그 행을
                                조용히 버린다.** 지금 ISO 로 보이는 건 우연이다.

    읽는 쪽(parse_calendar_values)은 get_all_values() 의 **표시 문자열**을 쓴다.
    그래서 여기서는 UNFORMATTED 읽기가 아니라 **RAW 쓰기**가 맞는 짝이다
    (숫자 열이었다면 반대였다 — 2026-08-25 Account_Profile 사례 참조).

    셀에 남아 있는 옛 시간/날짜 서식은 문제가 되지 않는다. clear() 는 값만
    지우고 서식은 남기지만, **텍스트 값은 숫자 서식의 영향을 받지 않는다.**
    전체 교체 방식이라 이 잡이 한 번 돌면 과거 오염분도 같이 정상화된다.
    """
    ws.clear()
    body = [cc.CAL_COLS] + rows
    gsr.call(ws.update, body, range_name="A1",
                   value_input_option="RAW")
    return len(rows)


def add_reminder(sh, title, what, why, source):
    """임시 휴장 발견 시 Reminders 에 항목 추가.

    Reminders 는 관리자 전용 개발 로드맵이다(설계상 게스트에게 안 간다).
    시장 캘린더 이상은 정확히 그 성격이다 — 투자 알림이 아니라 시스템 점검
    항목이므로 여기에 넣는 것이 맞다.
    """
    try:
        ws = _open_or_create(sh, rc.REMINDERS_WORKSHEET, 500, rc.REMINDER_NCOL)
        vals = gsr.call(ws.get_all_values) or []
        if not vals or not any(str(c).strip() for c in vals[0]):
            gsr.call(ws.update, [rc.REMINDER_COLS], range_name="A1",
                           value_input_option="USER_ENTERED")
            vals = [rc.REMINDER_COLS]
        # 같은 제목의 open 항목이 이미 있으면 중복 생성하지 않는다.
        existing = rc.parse_reminders(vals)
        for r in existing:
            if (str(r.get("Title", "")).strip() == title
                    and str(r.get("Status", "")).strip() == rc.STATUS_OPEN):
                print("  [SKIP] 동일 리마인더가 이미 열려 있음: " + title)
                return False
        today = datetime.now(_ET).strftime("%Y-%m-%d")
        rem = rc.make(title=title, due=today, what_to_check=what,
                      why=why, category="검증", source=source)
        gsr.call(ws.append_row, rc.to_row(rem),
                       value_input_option="USER_ENTERED",
                       table_range="A1")
        print("  [REMINDER] 추가됨: " + title)
        return True
    except Exception as e:
        print("  [WARN] 리마인더 추가 실패(계속 진행): " + str(e))
        return False


def main():
    print("=" * 60)
    print("[START] 시장 캘린더 갱신: "
          + datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
          + ("  (DRY_RUN)" if DRY_RUN else ""))
    print("  calendar_core v" + cc.CALENDAR_CORE_VERSION)

    if not FMP_API_KEY:
        print("[ABORT] FMP_API_KEY 없음.")
        return 1

    this_year = datetime.now(_ET).year
    years = [this_year, this_year + 1]
    print("[STEP 1] FMP 조회 — " + str(years[0]) + "~" + str(years[1]))

    records = cc.fetch_calendar_fmp(FMP_API_KEY, years)
    if not records:
        # 판정 경로가 이 결과에 의존하지 않으므로 실패해도 시스템은 정상이다.
        print("[WARN] FMP 응답 없음(402/네트워크 등). 규칙 계산은 계속 동작합니다.")
        print("[END] 갱신 없이 종료 — 알림/판정에는 영향 없음.")
        return 0
    print("  수신 " + str(len(records)) + "건")

    # ── STEP 2: 규칙 대조 ────────────────────────────────────────────────
    print("[STEP 2] 규칙 계산과 대조")
    diff = cc.diff_against_rules(records, years=years)
    extra = diff["extra_closed"]
    missing = diff["missing"]
    half = diff["half_days"]

    for y in years:
        n = len(cc.nyse_regular_holidays(y))
        print("  규칙 " + str(y) + ": " + str(n) + "일")

    if half:
        print("  반일장(FMP) " + str(len(half)) + "일: "
              + ", ".join(d + "(" + t + ")" for d, t in sorted(half.items())))

    # ── 반일장 대조 — 규칙이 판정하고 FMP 가 검증한다 ────────────────────────
    # app.py 헤더와 2PM 가드는 규칙 계산으로 반일장을 판정한다(핫 패스라 시트를
    # 못 읽는다). 그 규칙이 여전히 맞는지 확인하는 유일한 채널이 여기다.
    # 전휴장의 [CALENDAR-ALERT] 와 같은 역할.
    mm = diff.get("half_mismatch") or {}
    _fo, _ro, _td = (mm.get("fmp_only") or {}, mm.get("rule_only") or {},
                     mm.get("time_diff") or {})
    for y in years:
        n = len(cc.nyse_early_close_days(y))
        print("  규칙 반일장 " + str(y) + ": " + str(n) + "일")
    if _fo or _ro or _td:
        print("  🔴 [HALFDAY-ALERT] 규칙과 FMP 가 어긋난다 — 규칙 수정 필요")
        for d, t in sorted(_fo.items()):
            print("     FMP 에만 있음  " + d + " (" + str(t) + ")"
                  " — 규칙이 못 잡는 조기 마감")
        for d, t in sorted(_ro.items()):
            print("     규칙에만 있음  " + d + " (" + str(t) + ")"
                  " — 규칙이 잘못 반일장이라고 한다")
        for d, (rt, ft) in sorted(_td.items()):
            print("     시각 불일치    " + d + " 규칙=" + str(rt)
                  + " FMP=" + str(ft))
        print("     ⚠️ app.py 헤더와 2PM 가드가 이 규칙을 쓴다. "
              "calendar_core.nyse_early_close_days 를 확인할 것.")
    else:
        print("  ✅ [HALFDAY-OK] 반일장 규칙과 FMP 일치")

    if missing:
        # 규칙이 휴장이라는데 FMP 응답에 없다. 규칙 오류이거나 응답 범위 누락.
        print("  ⚠️ [CALENDAR-MISSING] 규칙에는 있으나 FMP 응답에 없음 "
              + str(len(missing)) + "일: " + ", ".join(sorted(missing)))

    if extra:
        print("  🔴 [CALENDAR-ALERT] 규칙에 없는 휴장일 발견 "
              + str(len(extra)) + "일 — 임시 휴장 후보")
        for d, n in sorted(extra.items()):
            print("     " + d + "  " + n)
    else:
        print("  ✅ [CALENDAR-OK] 임시 휴장 없음 — 규칙 계산과 FMP 일치")

    # ── STEP 3: 시트 저장 ────────────────────────────────────────────────
    now_str = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
    rows = cc.rows_from_fmp(records, source="FMP", now_str=now_str)

    if DRY_RUN:
        print("[DRY_RUN] 시트 쓰기 생략. 저장 예정 " + str(len(rows)) + "행")
        print("[END] 완료(DRY_RUN)")
        return 0

    if not GSPREAD_KEY_JSON:
        print("[ABORT] GSPREAD_KEY 없음 — 대조는 끝났으나 저장 못 함.")
        return 1

    print("[STEP 3] " + cc.CAL_SHEET + " 시트 저장")
    try:
        gc = get_gspread_client()
        sh = gc.open(_SPREADSHEET_TITLE)
        ws = _open_or_create(sh, cc.CAL_SHEET, 400, len(cc.CAL_COLS))
        n = write_calendar(ws, rows)
        print("  저장 " + str(n) + "행")
    except Exception as e:
        print("[ERROR] 시트 저장 실패: " + str(e))
        return 1

    # ── STEP 4: 임시 휴장이면 리마인더 ───────────────────────────────────
    if extra:
        days = ", ".join(sorted(extra))
        add_reminder(
            sh,
            title="임시 휴장일 확인 — " + days,
            what=("FMP 가 휴장이라고 한 날이 calendar_core 규칙 계산에 없습니다. "
                  "대통령 국장일 같은 임시 휴장일 가능성이 큽니다. "
                  "사실이면 calendar_core 에 예외 날짜를 추가하거나, "
                  "Market_Calendar 시트를 판정 경로에 연결하는 설계를 검토하세요. "
                  "해당 날짜: " + days),
            why=("규칙 계산은 정규 휴일 10개만 재현합니다. 임시 휴장은 구조적으로 "
                 "잡을 수 없어 이 대조가 유일한 발견 경로입니다."),
            source="refresh_market_calendar.py",
        )

    print("[END] 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
