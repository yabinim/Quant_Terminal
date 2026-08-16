"""diag_reminders.py — 리마인더 + 매도 사유 태그 회귀 스위트.

네트워크·시트 접근 없음. 순수 로직만 본다.

지키려는 위험
─────────────
  1. 연기가 만기를 못 이기면 — "봤지만 아직 못 함"을 done 으로 처리하게 되고
     항목이 영영 사라진다
  2. 만기 파싱 실패를 later 로 분류하면 — 조용히 숨어서 영원히 안 보인다
  3. 띄울 게 없는데 빈 블록을 만들면 — 매일 붙는 껍데기를 읽지 않게 되고
     정작 만기가 왔을 때도 안 읽는다
  4. seed 재실행이 완료·연기 상태를 되살리면 — 처리한 항목이 부활한다
  5. 매도 태그가 사용자 사유와 시스템 판정을 뭉개면 — 실행 갭이 사라진다

실행
────
    python automation/diag_reminders.py
"""
import os
import sys
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reminders_core as rmc  # noqa: E402

T = date(2026, 8, 15)
_fail, _pass = [], 0


def check(name, cond, detail=""):
    global _pass
    if cond:
        _pass += 1
        print(f"  ✅ {name}")
    else:
        _fail.append(name)
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def R(due, snooze="", status="open", title="T"):
    r = rmc.make(title, due, "확인할 것")
    r["Snoozed_Until"] = snooze
    r["Status"] = status
    return r


print("\n[1] 분류")
check("지난 만기 → overdue", rmc.classify(R("2026-08-01"), T) == "overdue")
check("오늘 만기 → due", rmc.classify(R("2026-08-15"), T) == "due")
check("7일 이내 → soon", rmc.classify(R("2026-08-22"), T) == "soon")
check("8일 뒤 → later", rmc.classify(R("2026-08-23"), T) == "later")
check("완료 → done", rmc.classify(R("2026-08-01", status="done"), T) == "done")

print("\n[2] 연기가 만기를 이긴다")
r = R("2026-08-01", snooze="2026-09-30")     # 지났지만 연기됨
check("연기하면 overdue 가 아니다", rmc.classify(r, T) == "later")
check("effective_due 가 연기일", rmc.effective_due(r) == date(2026, 9, 30))
check("남은 일수도 연기 기준", rmc.days_left(r, T) == 46)
r2 = R("2026-09-30", snooze="2026-08-10")    # 연기일이 더 이르면 그쪽을 따른다
check("연기일이 이르면 그게 적용", rmc.classify(r2, T) == "overdue")

print("\n[3] 만기 파싱 실패는 숨기지 않는다")
check("빈 만기 → overdue", rmc.classify(R(""), T) == "overdue")
check("쓰레기 만기 → overdue", rmc.classify(R("내일"), T) == "overdue")
check("완료면 파싱 실패도 done", rmc.classify(R("", status="done"), T) == "done")

print("\n[4] 정렬 — 급한 것이 위로")
rows = [R("2026-08-22", title="soon"), R("2026-07-01", title="overdue"),
        R("2026-08-15", title="due"), R("2027-01-01", title="later")]
live = rmc.active_reminders(rows, T)
check("later 는 제외", len(live) == 3, f"got {len(live)}")
check("순서 overdue→due→soon",
      [x["Title"] for x in live] == ["overdue", "due", "soon"],
      f"got {[x['Title'] for x in live]}")

print("\n[5] 메일 블록")
check("띄울 게 없으면 빈 문자열",
      rmc.build_email_html([R("2027-01-01")], T) == "")
check("완료만 있어도 빈 문자열",
      rmc.build_email_html([R("2026-08-01", status="done")], T) == "")
html = rmc.build_email_html(rows, T)
check("항목이 있으면 블록 생성", "개발 리마인더" in html)
check("later 는 메일에 없음", "later" not in html)
check("건수 표기", "3건" in html, html[:80])
ev = rmc.make("<b>주입</b>", "2026-08-15", "확인 <script>")
check("HTML 이스케이프", "<script>" not in rmc.build_email_html([ev], T))

print("\n[6] 파싱 — 열이 모자라거나 남아도 죽지 않는다")
vals = [rmc.REMINDER_COLS,
        ["id1", "2026-08-15", "제목"],                       # 열 부족
        ["id2", "2026-08-15", "제목2", "검증", "무엇", "왜",
         "open", "", "2026-08-15", "출처", "여분"],           # 열 초과
        ["", "", "", "", "", "", "", "", "", ""]]             # 빈 행
got = rmc.parse_reminders(vals)
check("빈 행 제외 · 2건 파싱", len(got) == 2, f"got {len(got)}")
check("부족한 열은 공란 채움", got[0]["Status"] == "")
check("초과 열은 버림", len(got[1]) == rmc.REMINDER_NCOL)
check("헤더만 있으면 0건", rmc.parse_reminders([rmc.REMINDER_COLS]) == [])
check("빈 입력도 안전", rmc.parse_reminders([]) == [])

print("\n[7] ID 안정성")
a = rmc.make("내부자 블록 검증", "2026-11-15", "x")
b = rmc.make("내부자 블록 검증", "2026-11-15", "다른 내용")
check("같은 제목·만기 → 같은 ID", a["ID"] == b["ID"], f"{a['ID']} vs {b['ID']}")
check("만기 다르면 ID 다름",
      a["ID"] != rmc.make("내부자 블록 검증", "2026-12-01", "x")["ID"])

print("\n[8] seed 병합 — 처리한 항목이 부활하면 안 된다")
try:
    import seed_reminders as sd
    seeds = sd.SEEDS
    existing = [dict(seeds[0]), dict(seeds[1])]
    existing[0]["Status"] = "done"
    existing[1]["Snoozed_Until"] = "2027-01-01"
    merged = sd.merge(existing, seeds)
    check("중복 생기지 않음", len(merged) == len(seeds), f"got {len(merged)}")
    m0 = next(x for x in merged if x["ID"] == seeds[0]["ID"])
    m1 = next(x for x in merged if x["ID"] == seeds[1]["ID"])
    check("완료 상태 보존", m0["Status"] == "done", f"got {m0['Status']}")
    check("연기 상태 보존", m1["Snoozed_Until"] == "2027-01-01")
    check("본문은 갱신됨", m0["What_To_Check"] == seeds[0]["What_To_Check"])
    check("초기 3건", len(seeds) == 3, f"got {len(seeds)}")
    check("전부 What_To_Check 가 충실",
          all(len(s["What_To_Check"]) > 60 for s in seeds))
except Exception as e:
    check("seed 모듈 로드", False, str(e))

print("\n[9] 매도 사유 태그")
try:
    import re as _re
    _app = next((p for p in (os.path.join(_ROOT, "app.py"),
                             os.path.join(_HERE, "app.py"))
                 if os.path.exists(p)), None)
    if _app is None:
        raise FileNotFoundError("app.py 를 찾지 못했다")
    src = open(_app, encoding="utf-8").read()
    ns = {}
    for fn in ("_build_sell_memo", "_parse_sell_reason"):
        m = _re.search(r"^def " + fn + r"\(.*?(?=^def |\Z)", src,
                       _re.S | _re.M)
        exec(compile(m.group(0), fn, "exec"), ns)
    opts = _re.search(r"_SELL_REASON_OPTIONS = \[(.*?)\]", src, _re.S).group(1)
    ns["_SELL_REASON_DEFAULT"] = _re.findall(r'"([^"]+)"', opts)[0]
    build, parse = ns["_build_sell_memo"], ns["_parse_sell_reason"]

    memo = build("⚖️ 리밸런싱", "분기 조정", "🔴 매도")
    check("사유 태그 삽입", "[매도:⚖️ 리밸런싱]" in memo, memo)
    check("시스템 판정 별도 보존", "[판정:🔴 매도]" in memo, memo)
    check("원문 유지", memo.endswith("분기 조정"), memo)
    check("왕복 파싱", parse(memo) == "⚖️ 리밸런싱", parse(memo))
    check("기본값이면 사유 태그 생략",
          "[매도:" not in build(ns["_SELL_REASON_DEFAULT"], "메모"))
    check("태그 없는 옛 행 → 미기록",
          parse("그냥 팔았음") == "🧩 기타 / 미기록")
    check("빈 memo 안전", parse("") == "🧩 기타 / 미기록")
    check("판정 없으면 판정 태그 없음",
          "[판정:" not in build("✋ 재량 매도 (신호 무관)", "x", ""))
    # 사유와 판정이 어긋난 경우 — 실행 갭. 둘 다 남아야 한다
    gap = build("💵 현금 확보", "", "🔴 매도")
    check("실행 갭 케이스에 둘 다 기록",
          "[매도:💵 현금 확보]" in gap and "[판정:🔴 매도]" in gap, gap)
except Exception as e:
    check("매도 태그 함수 추출", False, str(e))

print("\n[10] SSOT 매니페스트 등록 형식")
try:
    import ast as _ast
    _app2 = next((p for p in (os.path.join(_ROOT, "app.py"),
                              os.path.join(_HERE, "app.py"))
                  if os.path.exists(p)), None)
    _src2 = open(_app2, encoding="utf-8").read()
    _tree = _ast.parse(_src2)
    _mani = next(n for n in _ast.walk(_tree) if isinstance(n, _ast.Assign)
                 and getattr(n.targets[0], "id", "") == "_SSOT_NEEDS")
    _aliases = set()
    for _n2 in _ast.walk(_tree):
        if isinstance(_n2, _ast.Import):
            for _a in _n2.names:
                _aliases.add(_a.asname or _a.name)

    # 2번째 항목은 모듈 **객체**여야 한다. 문자열을 넣으면 hasattr 이 전부
    # False 가 되어 정상 배포를 '구버전'으로 오진하고 앱이 st.stop() 으로 죽는다.
    # 실제로 밟은 버그다 — 배포는 멀쩡한데 앱이 안 떴다.
    bad_type, bad_alias = [], []
    for _e in _mani.value.elts:
        _label = _e.elts[0].value
        _mn = _e.elts[1]
        if not isinstance(_mn, _ast.Name):
            bad_type.append(_label)
        elif _mn.id not in _aliases:
            bad_alias.append(_label)
    check("전 항목이 모듈 객체로 등록됨(문자열 아님)", not bad_type, str(bad_type))
    check("등록된 이름이 실제 import 별칭", not bad_alias, str(bad_alias))
    check("reminders_core 가 매니페스트에 있음",
          any(e.elts[0].value == "reminders_core" for e in _mani.value.elts))

    # 런타임 가드가 문자열 등록을 실제로 걸러내는가
    check("가드에 모듈 타입 검사 존재",
          "ModuleType" in _src2, "isinstance(_m, _types.ModuleType) 검사 없음")
except Exception as e:
    check("매니페스트 검사", False, str(e))

print("\n[11] 워크플로 — 존재와 시크릿명")
try:
    _repo = os.path.dirname(_HERE) if os.path.basename(_HERE) == "automation" else _ROOT
    _wf = os.path.join(_repo, ".github", "workflows")
    # 스크립트마다 짝이 되는 워크플로가 있어야 한다. 없으면 Actions 에서
    # 실행할 방법이 없다 — 실제로 seed 워크플로를 통째로 빠뜨린 적이 있다.
    for _s, _y in (("seed_reminders", "seed_reminders.yml"),
                   ("diag_reminders", "diag_reminders.yml")):
        check(f"{_y} 존재", os.path.exists(os.path.join(_wf, _y)),
              "워크플로 누락 — Actions 에서 실행할 수 없다")

    # 시크릿명은 기존 관례(GSPREAD_KEY)를 따라야 한다.
    # GCP_SERVICE_ACCOUNT_JSON 같은 새 이름을 쓰면 시크릿이 비어 즉시 죽는다.
    for _f in ("seed_reminders.py",):
        _p = os.path.join(_HERE, _f)
        if os.path.exists(_p):
            _t = open(_p, encoding="utf-8").read()
            check(f"{_f} 가 GSPREAD_KEY 사용", "GSPREAD_KEY" in _t)
            check(f"{_f} 에 미등록 시크릿명 없음",
                  "GCP_SERVICE_ACCOUNT_JSON" not in _t)
    _yp = os.path.join(_wf, "seed_reminders.yml")
    if os.path.exists(_yp):
        _yt = open(_yp, encoding="utf-8").read()
        check("seed 워크플로가 GSPREAD_KEY 주입", "secrets.GSPREAD_KEY" in _yt)
        check("seed 워크플로 DRY_RUN 기본 true", "default: true" in _yt)
except Exception as e:
    check("워크플로 검사", False, str(e))

print("\n" + "=" * 66)
if _fail:
    print(f"❌ 실패 {len(_fail)}건 / 통과 {_pass}건")
    for n in _fail:
        print(f"   · {n}")
    sys.exit(1)
print(f"✅ 전체 통과 — {_pass}건")
sys.exit(0)
