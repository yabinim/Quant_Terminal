#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_py311.py — Python 3.11 f-string 호환성 검사기.

왜 필요한가
-----------
Streamlit Community Cloud 와 GitHub Actions 러너의 Python 버전이 다를 수 있다.
Python 3.12 는 PEP 701 로 f-string 문법을 완화해서 아래 세 가지를 허용하는데,
**3.11 에서는 SyntaxError** 다:

  1. 치환식 안에서 바깥과 **같은 따옴표** 사용      f"{d["key"]}"
  2. 치환식 안의 **백슬래시**                        f"{'\\n'.join(x)}"
  3. **같은 따옴표로 중첩된 f-string**               f"{f"{v}"}"

`py_compile` 은 실행 중인 인터프리터 문법으로 검사하므로, 3.12 에서 돌리면
이 셋을 전부 통과시킨다. 3.11 배포 환경에서만 죽는 조용한 사고가 된다.

동작 원리
---------
3.12 의 tokenize 는 f-string 을 FSTRING_START / FSTRING_MIDDLE / FSTRING_END 로
쪼개므로, 치환식 구간을 **정확히** 특정할 수 있다. 정규식으로 흉내내면
`f"...] = {"a": 1}` 같은 평범한 코드를 오탐한다(실제로 6건 오탐 경험).

3.11 이하에서 실행하면 f-string 이 단일 STRING 토큰이라 이 검사가 무의미하므로
검사를 건너뛰고 안내만 한다(그 버전에서는 py_compile 자체가 진짜 검사다).

사용법
------
    python check_py311.py app.py automation/run_watchlist_alerts.py ...
    python check_py311.py            # 인자 없으면 현재 트리의 *.py 전부

종료 코드: 위반 0건이면 0, 있으면 1.
"""
from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

RULE_SAME_QUOTE = "치환식 안에서 바깥과 같은 따옴표"
RULE_BACKSLASH = "치환식 안의 백슬래시"
RULE_NESTED_FSTR = "같은 따옴표로 중첩된 f-string"


def _quote_of(fstring_start: str) -> str:
    """FSTRING_START 토큰(f\" / rf''' / F\"\"\" ...) → 따옴표 부분만."""
    i = 0
    while i < len(fstring_start) and fstring_start[i] not in "\"'":
        i += 1
    return fstring_start[i:]


def scan(path: Path) -> list[tuple[int, int, str, str]]:
    """파일 하나 검사 → [(행, 열, 규칙, 발췌)]."""
    hits: list[tuple[int, int, str, str]] = []
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [(0, 0, "읽기 실패", f"{type(e).__name__}: {e}")]

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError) as e:
        # 구문 자체가 깨진 파일 — py_compile 이 잡을 문제다.
        return [(0, 0, "토큰화 실패", str(e))]

    # 열려 있는 f-string 들의 따옴표 스택. 최상단이 현재 컨텍스트.
    stack: list[str] = []
    for tok in toks:
        typ, txt, start, _end, _line = tok

        if typ == tokenize.FSTRING_START:
            q = _quote_of(txt)
            # 중첩 f-string 도 같은 규칙: 바깥 구분자가 안쪽 시작 토큰에 나타나면 위반.
            #   바깥 "   · 안쪽 f"   → 위반
            #   바깥 """ · 안쪽 f"   → 정상
            if stack and stack[-1] in txt:
                hits.append((start[0], start[1], RULE_NESTED_FSTR, txt))
            stack.append(q)
            continue

        if typ == tokenize.FSTRING_END:
            if stack:
                stack.pop()
            continue

        if not stack:
            continue                      # f-string 바깥 — 관심 없음

        # 여기부터는 f-string 치환식 내부. FSTRING_MIDDLE 은 리터럴 조각이라 제외.
        if typ == tokenize.FSTRING_MIDDLE:
            continue

        cur = stack[-1]
        if typ == tokenize.STRING:
            # 3.11 은 닫는 구분자를 먼저 찾은 뒤 안쪽을 파싱한다. 따라서 위반 조건은
            # "안쪽 토큰에 **바깥 구분자 시퀀스 그대로**가 등장하는가" 다.
            #   바깥 "   · 안쪽 "k"     → 위반
            #   바깥 """ · 안쪽 "k"     → 정상 (" 는 """ 를 닫지 못함)
            #   바깥 """ · 안쪽 """x""" → 위반
            if cur in txt:
                hits.append((start[0], start[1], RULE_SAME_QUOTE, txt[:60]))
            if "\\" in txt:
                hits.append((start[0], start[1], RULE_BACKSLASH, txt[:60]))
        elif "\\" in txt:
            hits.append((start[0], start[1], RULE_BACKSLASH, txt[:60]))

    return hits


def main(argv: list[str]) -> int:
    if argv:
        targets = [Path(a) for a in argv]
    else:
        targets = sorted(p for p in Path(".").rglob("*.py")
                         if ".git" not in p.parts and "__pycache__" not in p.parts)

    missing = [t for t in targets if not t.is_file()]

    if sys.version_info < (3, 12):
        # 3.11 이하에서 실행 중이면 인터프리터 자체가 3.11 문법을 강제한다.
        # 토큰 분석 대신 **직접 컴파일**하는 게 더 강한 검사다(전 문법 커버).
        ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        print(f"ℹ️  Python {ver} 로 실행 중 — 토큰 분석 대신 직접 컴파일로 검사합니다.")
        bad = 0
        for m in missing:
            print(f"  ❌ 파일 없음: {m}")
        for t in targets:
            if not t.is_file():
                continue
            try:
                compile(t.read_text(encoding="utf-8"), str(t), "exec")
                print(f"  ✅ {t}")
            except SyntaxError as e:
                bad += 1
                print(f"  ❌ {t} — {e.lineno}:{e.offset}  {e.msg}")
                if e.text:
                    print(f"       → {e.text.strip()[:80]}")
        print()
        if bad or missing:
            print(f"❌ Python {ver} 구문 오류 {bad}건"
                  + (f" · 누락 파일 {len(missing)}건" if missing else ""))
            return 1
        print(f"✅ {len(targets)}개 파일 — Python {ver} 컴파일 통과")
        return 0

    for m in missing:
        print(f"❌ 파일 없음: {m}")
    targets = [t for t in targets if t.is_file()]
    if not targets:
        print("검사할 파일이 없습니다.")
        return 1 if missing else 0

    total = 0
    for t in targets:
        hits = scan(t)
        if not hits:
            print(f"  ✅ {t}")
            continue
        total += len(hits)
        print(f"  ❌ {t} — {len(hits)}건")
        for line, col, rule, frag in hits:
            print(f"       {line}:{col}  {rule}  →  {frag}")

    print()
    if total or missing:
        print(f"❌ Python 3.11 비호환 {total}건"
              + (f" · 누락 파일 {len(missing)}건" if missing else ""))
        print("   해결: 안쪽 따옴표를 바깥과 다르게 쓰거나, 치환식을 변수로 빼내세요.")
        return 1
    print(f"✅ {len(targets)}개 파일 — Python 3.11 호환")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
