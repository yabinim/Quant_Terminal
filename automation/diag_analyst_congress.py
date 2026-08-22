# -*- coding: utf-8 -*-
"""diag_analyst_congress.py — ratings-snapshot 제거 · 의회 거래 프롬프트 분리 회귀 검사

설계 원칙(프로젝트 표준):
  · 소스를 문자열로 훑는 데서 그치지 않고, **프롬프트를 실제로 렌더**해 본다.
    AST/grep 은 '무엇을 호출하는가'는 보지만 f-string 안에서 빈 문자열이
    어떻게 찍히는지는 못 본다 — 이번에 고친 결함이 정확히 그 종류였다.
  · 각 검사는 원본(구버전)에 대해 반드시 **실패**해야 한다. 역검증 모드(--reverse)로
    확인한다. 통과만 하는 검사는 false-green 이다.
  · FMP 호출 0건. 네트워크 없이 돈다.

사용:
    python3 diag_analyst_congress.py <app.py 경로>
    python3 diag_analyst_congress.py <원본 app.py 경로> --reverse
"""
import sys
import re
import ast

PASS, FAIL = [], []


def check(cid, desc, ok, detail=""):
    (PASS if ok else FAIL).append((cid, desc, detail))
    print(f"  {'✅' if ok else '❌'} {cid} {desc}" + (f" — {detail}" if detail else ""))


# ──────────────────────────────────────────────────────────────────────
# A군: 엔드포인트 배선
# ──────────────────────────────────────────────────────────────────────
def group_a(src):
    print("\n[A군] 엔드포인트 배선")

    n_rs = len(re.findall(r"ratings-snapshot\?symbol", src))
    check("A-1", "ratings-snapshot 호출 0건",
          n_rs == 0, f"{n_rs}건 발견")

    n_gc = len(re.findall(r"grades-consensus\?symbol", src))
    check("A-2", "grades-consensus 호출 3건 (스타일점수/애널리스트섹션/정밀검사)",
          n_gc == 3, f"{n_gc}건")

    # ratings-snapshot 이 '재무 스코어'임을 코드에 못 박아 두었는가.
    # 주석이 없으면 다음 사람이 같은 실수를 반복한다.
    check("A-3", "재무 스코어 경고 주석 존재",
          "재무 스코어" in src and "ratings-snapshot" in src)


# ──────────────────────────────────────────────────────────────────────
# B군: 의회 거래 — 표시는 살고 프롬프트는 죽었는가
# ──────────────────────────────────────────────────────────────────────
def group_b(src):
    print("\n[B군] 의회 거래 경로 분리")

    n_fetch = len(re.findall(r"fetch_senate_house_trading\(str", src))
    check("B-1", "의회 fetch 호출부 1곳만 (표시 전용)",
          n_fetch == 1, f"{n_fetch}곳")

    check("B-2", "표시 위젯(st.dataframe(congress_df)) 유지",
          "congress_df" in src and "st.dataframe(congress_df" in src)

    check("B-3", "프롬프트 변수 _diag_cg_* 완전 제거",
          "_diag_cg_buy" not in src and "_diag_cg_sell" not in src)

    check("B-4", "캡션에서 '선행 지표' 주장 제거",
          "정책 방향 선행 지표" not in src)

    check("B-5", "캡션에 공시 지연 경고 존재",
          "공시 지연" in src)

    check("B-6", "되돌림 방지 근거(VERDICT 파일명) 주석에 명시",
          "VERDICT_2026-08-22_congress_trades_terminated" in src)


# ──────────────────────────────────────────────────────────────────────
# C군: 프롬프트를 **실제로 렌더**한다 (핵심)
# ──────────────────────────────────────────────────────────────────────
_PROMPT_ANCHOR = "당신은 15년 경력의 헤지펀드 포트폴리오 매니저입니다."


def _extract_prompt_template(src):
    """정밀검사 프롬프트 f-string 본문을 원문 그대로 떼어 온다."""
    i = src.find(_PROMPT_ANCHOR)
    if i < 0:
        return None
    start = src.rfind('f"""', 0, i)
    if start < 0:
        return None
    body_start = start + 4
    end = src.find('"""', body_start)
    if end < 0:
        return None
    return src[body_start:end]


def _render(template, values):
    """f-string 본문을 실제 f-string 으로 컴파일해 렌더한다.

    문자열 치환이 아니라 **컴파일**이라는 게 중요하다. `{x or "N/A"}` 같은
    표현식이 실제로 어떻게 평가되는지는 실행해 봐야만 안다.
    """
    code = "f" + repr(template).replace("\\n", "\\n")
    # repr 로 감싸면 따옴표 이스케이프가 보장된다. f 접두사를 붙여 컴파일.
    return eval(compile(ast.Expression(ast.parse(code, mode="eval").body),
                        "<prompt>", "eval"), {}, values)


def group_c(src):
    print("\n[C군] 프롬프트 실제 렌더 (스텁 주입)")

    tpl = _extract_prompt_template(src)
    if tpl is None:
        check("C-0", "프롬프트 템플릿 추출", False, "앵커 문자열 없음 — 검사 불가")
        return
    check("C-0", "프롬프트 템플릿 추출", True, f"{len(tpl)}자")

    # 애널리스트 라인만 떼어 렌더한다(전체 프롬프트는 변수가 수십 개라 과잉).
    line = None
    for ln in tpl.splitlines():
        if ln.startswith("애널리스트 매수의견:"):
            line = ln
            break
    if line is None:
        check("C-1", "애널리스트 매수의견 라인 존재", False, "라인 없음")
        return
    check("C-1", "애널리스트 매수의견 라인 존재", True)

    # ── 결함 재현 시나리오: 컨센서스 조회 실패 → 세 변수 모두 "" ──
    empty = {"_diag_buy_pct": "", "_diag_rat_tot": "", "_diag_rat_label": ""}
    try:
        out_empty = _render(line, empty)
    except Exception as e:
        check("C-2", "빈 값 렌더", False, f"{type(e).__name__}: {e}")
        return

    check("C-2", "빈 값일 때 라벨 뒤가 비지 않는다",
          out_empty.count("N/A") >= 3,
          repr(out_empty))

    # ── 정상 시나리오 ──
    filled = {"_diag_buy_pct": "72%", "_diag_rat_tot": "26/36명",
              "_diag_rat_label": "Buy"}
    out_filled = _render(line, filled)
    check("C-3", "정상 값이 그대로 실린다",
          "72%" in out_filled and "26/36명" in out_filled and "Buy" in out_filled,
          repr(out_filled))

    # ── 의회 거래가 프롬프트에 남아 있지 않은가 ──
    check("C-4", "프롬프트 본문에 '의회 거래' 라인 없음",
          "의회 거래:" not in tpl)


# ──────────────────────────────────────────────────────────────────────
# D군: 구문/구조 무결성
# ──────────────────────────────────────────────────────────────────────
def group_d(src, path):
    print("\n[D군] 구조 무결성")
    try:
        ast.parse(src)
        check("D-1", "AST 파싱", True)
    except SyntaxError as e:
        check("D-1", "AST 파싱", False, f"line {e.lineno}: {e.msg}")
        return

    # grades-consensus 를 쓰는 3곳 모두 consensus 또는 카운트 필드를 읽는가
    check("D-2", "consensus 필드 사용 (종합의견 라벨)",
          src.count('.get("consensus")') >= 2,
          f'{src.count(chr(46) + "get(" + chr(34) + "consensus" + chr(34) + ")")}곳')

    # 존재하지 않는 v3 필드명이 남아 있지 않은가
    ghosts = [g for g in ("ratingDetailsStrongBuyCount", "ratingRecommendation",
                          "ratingScore", "ratingStrongBuy")
              if g in src]
    check("D-3", "존재하지 않는 v3 필드명 잔존 0", not ghosts, ", ".join(ghosts))


def main():
    if len(sys.argv) < 2:
        print("usage: diag_analyst_congress.py <app.py> [--reverse]")
        sys.exit(2)
    path = sys.argv[1]
    reverse = "--reverse" in sys.argv
    src = open(path, encoding="utf-8").read()

    print("=" * 66)
    print(f"대상: {path}  ({src.count(chr(10)) + 1}줄)")
    print(f"모드: {'역검증 (구버전 — 실패해야 정상)' if reverse else '정검증'}")
    print("=" * 66)

    group_a(src)
    group_b(src)
    group_c(src)
    group_d(src, path)

    print("\n" + "=" * 66)
    print(f"통과 {len(PASS)} · 실패 {len(FAIL)}")
    if reverse:
        ok = len(FAIL) > 0
        print(f"역검증: 구버전에서 {len(FAIL)}건 실패 → "
              + ("✅ 검사가 판별력을 가진다" if ok else "❌ FALSE-GREEN — 검사 무의미"))
        sys.exit(0 if ok else 1)
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
