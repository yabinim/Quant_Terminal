# -*- coding: utf-8 -*-
"""반일장(조기 마감) 실측 프로브 — 2콜.

무엇을 답하나
─────────────
단 하나. **FMP holidays-by-exchange 가 반일장을 실어주는가, 그리고 어떤 형식인가.**

`calendar_core` 에는 배관이 이미 있다:

    parse_calendar_values()  →  {"half": {날짜: 시각}}
    diff_against_rules()     →  half_days

그런데 **그 값을 읽고 판정하는 코드가 하나도 없다.** `rows_from_fmp` 는
`adjCloseTime` 을 `str()` 로 그냥 저장할 뿐이라, 실제로 무엇이 오는지
(`"13:00"` 인지 `"1:00 PM"` 인지 빈 문자열인지) 아무도 확인한 적이 없다.

엔드포인트 이름이 "holidays" 다. **반일장은 휴일이 아니다.** 아예 안 실릴 수도 있다.

왜 지금 재나
────────────
반일장 14:00 에 두 곳이 틀린 말을 한다.

  · app.py get_market_status()  → "🟢 Regular Market (Open)" (49행 주석이 인정)
  · 2PM 잡 (--mode intraday)    → 13:00 종가를 "잠정" 봉으로 주입하고
                                   "장중 헤드업" 메일 발송. 숫자는 맞고 라벨이 틀림

고치는 방식이 이 프로브 결과로 갈린다.

  실린다  → 시트를 주간 검증 채널로 쓸 수 있다 (A-1 과 동일한 패턴)
  안 실린다 → 규칙 계산만 남는다. 검증 채널이 없으므로 규칙 오류를 잡을
              방법이 사라진다 — 뮤테이션 테스트 비중을 더 올려야 한다

⚠️ 판별 설계 — 통과만 보지 않는다
─────────────────────────────────
이 프로젝트에서 판별력 없는 지표를 판별자로 고른 전례가 세 번 있다.
그래서 **정답을 아는 날짜**로 검사한다.

  ① 2025-11-28 (추수감사절 다음날)  = 반일장. adjCloseTime 이 있어야 한다
  ② 2025-11-27 (추수감사절)         = 전휴장. isClosed=True 여야 한다
  ③ 2025-11-25 (평범한 화요일)      = **응답에 없어야 한다**

③이 핵심이다. 모든 날짜가 다 실려 오면 "응답에 있다"는 사실 자체가
아무 정보도 아니다. ③이 실려 오면 ①의 존재는 판별력이 없다.

추가로 **왕복 검사**를 한다. 응답을 `rows_from_fmp` → `parse_calendar_values`
로 실제 파이프라인에 통과시켜 `half` 가 채워지는지 본다. 필드가 있어도
형식이 안 맞으면 조용히 빈 dict 가 된다 — 그건 배포 후에나 드러난다.

안전성
──────
· 시트 쓰기 없음 · 이메일 없음 · 상태머신 미접촉 — 완전 읽기 전용
· repository_dispatch / schedule 없음 (workflow_dispatch 전용)
· 호출 2콜
"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import calendar_core as cc  # noqa: E402

_KEY = str(os.environ.get("FMP_API_KEY", "") or "").strip()
_EX = str(os.environ.get("PROBE_EXCHANGE", "") or "NYSE").strip()

# ── 정답을 아는 날짜 ─────────────────────────────────────────────────────
# NYSE 반일장(13:00 조기 마감)은 통상 연 2~3회다.
#   · 추수감사절 다음 금요일        — 매년
#   · 7/3  — 독립기념일이 화~금일 때만 (토요일이면 7/3 이 전휴장)
#   · 12/24 — 성탄절이 화~금일 때만
KNOWN_HALF = {
    "2025-11-28": "추수감사절 다음날",
    "2025-07-03": "독립기념일 전날",
    "2025-12-24": "성탄절 전날",
    "2026-11-27": "추수감사절 다음날",
    "2026-12-24": "성탄절 전날",
}
KNOWN_CLOSED = {
    "2025-11-27": "추수감사절",
    "2025-12-25": "성탄절",
    "2026-11-26": "추수감사절",
}
# 평범한 거래일 — 응답에 **없어야** 한다
KNOWN_NORMAL = ["2025-11-25", "2025-06-17", "2026-03-10"]


def _bar():
    print("=" * 78)


def probe_year(year):
    """1콜. (records, ok) 반환."""
    print("")
    print("── " + str(year) + "년 조회 (exchange=" + _EX + ")")
    recs = cc.fetch_calendar_fmp(_KEY, [year], exchange=_EX)
    if not recs:
        print("   ❌ 빈 응답 — 402/네트워크/키 문제. fetch_calendar_fmp 는 조용히")
        print("      빈 리스트를 돌려주므로 여기서는 원인을 구분할 수 없다.")
        return [], False
    print("   ✅ " + str(len(recs)) + "건")
    keys = sorted({k for r in recs for k in r.keys()})
    print("   응답 키: " + ", ".join(keys))
    return recs, True


def analyze(recs, year):
    by_date = {}
    for r in recs:
        # fetch_calendar_fmp 가 이미 dict 만 통과시키지만, rows_from_fmp /
        # diff_against_rules 도 같은 가드를 갖고 있다. 형제 함수와 맞춘다.
        if not isinstance(r, dict):
            continue
        ds = str(r.get("date") or "").strip()[:10]
        if len(ds) == 10:
            by_date[ds] = r

    closed = [d for d, r in by_date.items() if r.get("isClosed")]
    notclosed = [d for d, r in by_date.items() if not r.get("isClosed")]
    print("")
    print("   isClosed=True  : " + str(len(closed)) + "건")
    print("   isClosed=False : " + str(len(notclosed)) + "건  ← 반일장 후보")

    if notclosed:
        print("")
        print("   ── isClosed=False 인 날 전부 (원문 그대로)")
        for d in sorted(notclosed):
            r = by_date[d]
            print("      " + d + "  name=" + repr(str(r.get("name") or ""))
                  + "  adjOpenTime=" + repr(str(r.get("adjOpenTime") or ""))
                  + "  adjCloseTime=" + repr(str(r.get("adjCloseTime") or "")))
    else:
        print("      (없음 — 이 엔드포인트는 전휴장만 싣는다는 뜻)")

    # ── 판별 ①②③ ───────────────────────────────────────────────────────
    print("")
    print("   ── 판별 (정답을 아는 날짜)")
    res = {}

    hits = [d for d in KNOWN_HALF if d.startswith(str(year))]
    for d in sorted(hits):
        r = by_date.get(d)
        if r is None:
            print("      ① " + d + " (" + KNOWN_HALF[d]
                  + ") → 🔴 응답에 없음")
            res[d] = "absent"
        elif r.get("isClosed"):
            print("      ① " + d + " (" + KNOWN_HALF[d]
                  + ") → 🟠 isClosed=True (전휴장으로 실려 있다)")
            res[d] = "closed"
        else:
            t = str(r.get("adjCloseTime") or "").strip()
            if t:
                print("      ① " + d + " (" + KNOWN_HALF[d]
                      + ") → ✅ adjCloseTime=" + repr(t))
                res[d] = "half:" + t
            else:
                print("      ① " + d + " (" + KNOWN_HALF[d]
                      + ") → 🔴 실려 있으나 adjCloseTime 이 비어 있다")
                res[d] = "empty"

    for d in sorted(k for k in KNOWN_CLOSED if k.startswith(str(year))):
        r = by_date.get(d)
        ok = bool(r and r.get("isClosed"))
        print("      ② " + d + " (" + KNOWN_CLOSED[d] + ") → "
              + ("✅ isClosed=True" if ok else "🔴 전휴장인데 그렇게 안 나온다"))

    # ★ 판별력 검사 — 평범한 거래일이 실려 오면 ①의 존재는 정보가 아니다
    norm = [d for d in KNOWN_NORMAL if d.startswith(str(year))]
    leaked = [d for d in norm if d in by_date]
    if not norm:
        pass
    elif leaked:
        print("      ③ 평범한 거래일 " + ", ".join(leaked)
              + " 이 응답에 있다 → 🔴 **판별력 없음**")
        print("         모든 날짜가 실려 온다면 '있다'는 사실이 아무 정보도 아니다.")
        print("         ① 결과를 신뢰할 수 없다.")
    else:
        print("      ③ 평범한 거래일 " + ", ".join(norm)
              + " → ✅ 응답에 없다 (특별한 날만 싣는다)")

    return res, bool(norm) and not leaked


def roundtrip(recs, year):
    """응답 → rows_from_fmp → parse_calendar_values 실제 파이프라인 통과.

    필드가 있어도 형식이 안 맞으면 half 가 조용히 비어 버린다. 그건 배포
    후에나 드러난다. 여기서 미리 확인한다.
    """
    print("")
    print("   ── 왕복 검사 (실제 파이프라인)")
    rows = cc.rows_from_fmp(recs, source="PROBE", now_str="probe")
    values = [list(cc.CAL_COLS)] + rows
    parsed = cc.parse_calendar_values(values)
    half = {d: t for d, t in parsed["half"].items() if d.startswith(str(year))}
    print("      rows_from_fmp        : " + str(len(rows)) + "행")
    print("      parse_calendar_values: closed "
          + str(len([d for d in parsed["closed"] if d.startswith(str(year))]))
          + "건 · half " + str(len(half)) + "건")
    if half:
        print("      ✅ half 가 채워진다 — 시트를 검증 채널로 쓸 수 있다")
        for d in sorted(half):
            print("         " + d + " → " + repr(half[d]))
    else:
        print("      🔴 half 가 비어 있다")
        print("         필드가 와도 형식이 안 맞으면 여기서 조용히 사라진다.")
        print("         시트 기반 반일장 판정은 성립하지 않는다.")
    return half


def rule_check(year):
    """규칙으로 계산한 반일장 후보 — FMP 와 대조할 기준선."""
    out = {}
    # 추수감사절(11월 넷째 목) 다음 금요일 — 매년
    tg = cc._nth_weekday(year, 11, 3, 4)      # weekday 3 = 목요일, 4번째
    out[(date(tg.year, tg.month, tg.day)).isoformat()] = "추수감사절"
    fri = date(tg.year, tg.month, tg.day).toordinal() + 1
    out[date.fromordinal(fri).isoformat()] = "추수감사절 다음날(반일장 후보)"
    # 7/3 · 12/24 — 요일 조건이 붙는다
    for m, d, nm in ((7, 3, "독립기념일 전날"), (12, 24, "성탄절 전날")):
        dt = date(year, m, d)
        if dt.weekday() < 5:
            out[dt.isoformat()] = nm + "(반일장 후보 — 요일 조건 확인 필요)"
    return out


def main():
    _bar()
    print("반일장(조기 마감) 실측 프로브 — 2콜")
    print("질문: FMP holidays-by-exchange 가 반일장을 싣는가, 어떤 형식인가")
    print("거래소: " + _EX + "   (PROBE_EXCHANGE 로 변경 가능)")
    _bar()

    if not _KEY:
        print("❌ FMP_API_KEY 없음")
        return 1

    summary = {}
    discriminating = True
    half_all = {}
    for y in (2025, 2026):
        recs, ok = probe_year(y)
        if not ok:
            summary[y] = "empty"
            continue
        res, disc = analyze(recs, y)
        discriminating = discriminating and disc
        h = roundtrip(recs, y)
        half_all.update(h)
        summary[y] = res
        print("")
        print("   ── 규칙 계산 기준선 (대조용)")
        for d, nm in sorted(rule_check(y).items()):
            mark = "  ← FMP half 에 있음" if d in h else ""
            print("      " + d + "  " + nm + mark)

    print("")
    _bar()
    print("최종 판정")
    _bar()

    if not discriminating:
        print("🔴 판별력 없음 — 평범한 거래일이 응답에 섞여 있다.")
        print("   '반일장이 실려 있다'는 관찰이 아무것도 증명하지 못한다.")
        print("   위 결과를 근거로 설계하지 말 것.")
    elif half_all:
        print("✅ 반일장이 실린다 — 시트를 주간 검증 채널로 쓸 수 있다")
        print("   형식: " + ", ".join(sorted({repr(t) for t in half_all.values()})))
        print("   → 규칙 계산(핫 패스) + 시트 대조(주간) 의 A-1 패턴이 성립한다.")
    else:
        print("🔴 반일장이 실리지 않는다 — 이 엔드포인트는 전휴장 전용이다")
        print("   → 시트 Adj_Close 는 영원히 비어 있다. 검증 채널이 없다.")
        print("   → 규칙 계산만 남는다. 규칙 오류를 잡을 외부 대조가 없으므로")
        print("      뮤테이션 테스트로만 방어해야 한다. 설계 재검토 필요.")

    print("")
    print("HALFDAY_JSON " + json.dumps(
        {"exchange": _EX, "discriminating": discriminating,
         "half": half_all, "summary": {str(k): v for k, v in summary.items()}},
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
