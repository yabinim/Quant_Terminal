# -*- coding: utf-8 -*-
"""diag_aum_field.py — 로테이션 게이트 AUM 필드 실측 프로브 (6콜).

무엇을 재는가
─────────────
`rotation_core.passes_aum()` 에 넣을 순자산 값을 **어느 엔드포인트의 어느 필드**
에서 가져와야 하는지를 정한다.

왜 지금 재는가
──────────────
2026-09-02, app.py 히든알파 버튼에 게이트를 연결하자 상위 15개가 **전원**
"순자산 미달 · 데이터 없음"으로 떨어졌다. GDX·IBIT·USO 까지 포함해서다.
IBIT 는 수백억 달러 규모다 — 게이트가 아니라 값이 안 오는 것이다.

코드가 읽는 필드는 이렇다.

    raw = prof.get("totalAssets") or prof.get("mktCap")   # run_hidden_alpha:389,517

그런데 프로젝트의 api-docs 를 보면 `/stable/profile` 응답 필드는 **marketCap** 이다.
  · `mktCap`      = 레거시 v3 필드. stable 에서는 search-exchange-variants 에만 남음
  · `totalAssets` = 재무제표(balance-sheet) 필드. profile 에 없음

즉 두 키 모두 영원히 안 잡히고 `aum_m` 은 항상 None 이 된다. `passes_aum(None)`
은 설계대로 False 이므로 **전원 제외**가 된다. rotation_core:29 의 주석
"totalAssets 가 거의 항상 비므로"는 신규 상장 탓으로 봤지만, 실제로는 전 종목이
비어 있었다.

왜 고치기 전에 재는가
─────────────────────
`marketCap` 으로 바꾸는 것이 자명해 보이지만, **ETF 의 marketCap 이 순자산과
같은 수인지는 문서에 없다.** 한편 `/stable/etf/info` 에는 `assetsUnderManagement`
가 명시돼 있고 `fmp_extras.fmp_etf_info()` 가 이미 감싸고 있다. 실매매 슬롯을
가르는 게이트이므로 추측으로 고치지 않는다.

⚠️ 판별자 설계 — 값의 **존재**가 아니라 **판별력**을 본다
──────────────────────────────────────────────────────────
"필드가 있고 0 이 아니다"는 판별력이 없다. 잘못된 필드도 그 조건은 만족할 수
있다(주가·발행좌수·상수 전부). 그래서 두 가지를 함께 요구한다.

  1) 크기   GDX·IBIT 가 하한($1,000M)을 크게 넘는가
  2) 분리   THYP(신규 상장 대조군)가 GDX·IBIT 보다 **자릿수로** 작은가

2)가 이 프로브의 핵심이다. 세 티커가 비슷한 수를 돌려주면 그 필드는 순자산이
아니다 — 크기 검사만 했으면 통과했을 값이다. tierB4 에서 "요청한 날짜부터
왔으니 한도가 3년"으로 잘못 기록했던 것과 같은 실수를 반복하지 않는다.

사전 확정 기준 (결과를 보기 **전에** 정한다 — 본 뒤에 바꾸지 않는다)
────────────────────────────────────────────────────────────────────
  A안 채택:  profile.marketCap 이 크기✅ + 분리✅
             → 이미 받아오는 응답이므로 **추가 콜 0**

  B안 채택:  A안 불성립이고 etf/info.assetsUnderManagement 가 크기✅ + 분리✅
             → 티커당 +1콜 (상위 15개 기준 +15콜, 앱·자동화 각각)

  충돌:      A·B 둘 다 성립하지만 두 값의 비가 3배 밖
             → 같은 것을 재고 있지 않다. 문서에 명시된 B안을 택한다.

  둘 다 불성립: AUM 축을 이 두 소스로 세울 수 없다. 게이트 재설계로 넘긴다.

안전성
──────
· 시트 접근 없음 · 이메일 없음 · 알림 상태 머신 미접촉
· 프로젝트 모듈 import 없음 (requests 만 사용) → 사본 신선도와 무관
· 총 6콜. 몇 번을 돌려도 부작용이 없다.

실행:  python3 automation/diag_aum_field.py
"""
import os
import sys

import requests

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 20

# 대조군을 반드시 포함한다. 큰 것만 재면 "필드가 크다"까지만 알 수 있고
# "이 필드가 순자산이다"는 알 수 없다.
TARGETS = [
    ("GDX", "대형 · 금광 ETF — 유동성 게이트는 이미 통과했던 종목"),
    ("IBIT", "초대형 · 비트코인 현물 ETF"),
    ("THYP", "신규 상장 대조군 — 여기가 작아야 이 필드가 순자산이다"),
]

MIN_LARGE_M = 1_000.0    # 대형 판정 하한 (백만$)
SEP_RATIO = 100.0        # 대조군 분리 배수
RATIO_BAND = 3.0         # 두 소스가 '같은 것을 잰다'고 볼 허용 배수


def get(path: str, key: str):
    """(status, payload) — 402 와 404 를 뭉개지 않는다."""
    if not key:
        return "no_key", None
    url = f"{BASE}/{path}{'&' if '?' in path else '?'}apikey={key}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
    except Exception as exc:
        return f"error({type(exc).__name__})", None
    if r.status_code != 200:
        return f"http_{r.status_code}", None
    try:
        return "ok", r.json()
    except Exception:
        return "bad_json", None


def first_row(payload):
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


def to_m(v):
    """원단위 → 백만$. 0·빈값·음수는 None(= 판정 불가)."""
    if v in (None, "", 0):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return (f / 1_000_000) if f > 0 else None


def verdict(src: dict, label: str) -> bool:
    """사전 확정 기준을 그대로 적용한다. 여기서 조건을 완화하지 않는다."""
    g, i, t = src.get("GDX"), src.get("IBIT"), src.get("THYP")
    big = (g is not None and g >= MIN_LARGE_M) and (i is not None and i >= MIN_LARGE_M)
    # 대조군이 None 이면 '분리됨'으로 본다 — 신규 상장은 값이 없는 것이 정상이고,
    # passes_aum(None) 이 제외로 가므로 게이트 동작도 옳다.
    sep = (t is None) or (big and t < g / SEP_RATIO and t < i / SEP_RATIO)
    print(f"\n  [{label}]")
    print(f"    GDX={g}  IBIT={i}  THYP={t}   (백만$)")
    print(f"    크기 검사 (둘 다 ≥ {MIN_LARGE_M:,.0f}M) : {'✅' if big else '❌'}")
    print(f"    분리 검사 (THYP < 1/{SEP_RATIO:.0f})      : {'✅' if sep else '❌'}")
    return big and sep


def decide(ok_a: bool, ok_b: bool, ratio: float | None) -> tuple[str, int]:
    """판정 분기 — 순수 함수로 떼어 둔다. 네트워크 없이 시험 가능해야 한다."""
    conflict = (ok_a and ok_b and ratio is not None
                and not (1 / RATIO_BAND <= ratio <= RATIO_BAND))
    if ok_a and not conflict:
        return "A", 0
    if ok_b:
        return "B", 0
    return "NONE", 1


def main() -> int:
    key = os.environ.get("FMP_API_KEY", "")
    print("=" * 78)
    print("diag_aum_field — 로테이션 게이트 AUM 필드 실측 (6콜)")
    print("=" * 78)
    if not key:
        print("❌ FMP_API_KEY 없음 — 프로브를 실행할 수 없습니다.")
        return 1

    prof_m, info_m = {}, {}
    for tk, note in TARGETS:
        print(f"\n── {tk} — {note}")

        st1, p1 = get(f"profile?symbol={tk}", key)
        row1 = first_row(p1)
        print(f"   profile    status={st1}")
        if st1 == "ok":
            # 세 후보 키를 나란히 찍는다. 코드가 읽던 두 키가 정말 없는지 눈으로 본다.
            for k in ("marketCap", "mktCap", "totalAssets"):
                print(f"     {k:12} = {row1.get(k, '<없음>')}")
            prof_m[tk] = to_m(row1.get("marketCap"))
            print(f"     → 백만$    = {prof_m[tk]}")
            print(f"     companyName = {str(row1.get('companyName', ''))[:60]}")
        else:
            prof_m[tk] = None

        st2, p2 = get(f"etf/info?symbol={tk}", key)
        row2 = first_row(p2)
        print(f"   etf/info   status={st2}")
        if st2 == "ok":
            for k in ("assetsUnderManagement", "aum", "nav"):
                print(f"     {k:22} = {row2.get(k, '<없음>')}")
            raw = row2.get("assetsUnderManagement")
            if raw in (None, "", 0):
                raw = row2.get("aum")
            info_m[tk] = to_m(raw)
            print(f"     → 백만$    = {info_m[tk]}")
            print(f"     isActivelyTrading = {row2.get('isActivelyTrading', '<없음>')}")
        else:
            info_m[tk] = None

    print("\n" + "=" * 78)
    print("판정 — 사전 확정 기준 적용")
    print("=" * 78)
    ok_a = verdict(prof_m, "A안  profile.marketCap  (추가 콜 0)")
    ok_b = verdict(info_m, "B안  etf/info.assetsUnderManagement  (티커당 +1콜)")

    ratio = None
    if prof_m.get("GDX") and info_m.get("GDX"):
        ratio = prof_m["GDX"] / info_m["GDX"]
        print(f"\n  두 소스 비 (GDX): profile / etf-info = {ratio:.3f}")
        print("    1.0 근처면 같은 것을 재고 있다는 뜻이다.")

    pick, code = decide(ok_a, ok_b, ratio)

    print("\n" + "=" * 78)
    if pick == "A":
        print("✅ 결론: **A안** — profile.marketCap 으로 교체. 추가 콜 0.")
    elif pick == "B":
        why = f"두 소스 비가 {ratio:.2f} 로 어긋남 — 같은 것을 재고 있지 않다" \
            if ok_a else "A안 불성립"
        print(f"✅ 결론: **B안** — etf/info.assetsUnderManagement 로 교체 ({why}).")
        print("   비용: 상위 15개 기준 +15콜 (앱·자동화 각각).")
    else:
        print("❌ 결론: 두 소스 모두 기준 미달. AUM 축을 이 둘로 세울 수 없다.")
        print("   게이트 재설계로 넘긴다 — 위 원시 응답을 근거로 판단할 것.")
        code = 1

    if code == 0:
        print("   수정 대상: run_hidden_alpha.py:389,517 · app.py:11338,11395")
        print("             · app.py cached_hidden_alpha_gates")
    print("=" * 78)
    print("\n⚠️ 이 판정은 결과를 보기 전에 정한 기준이다. 마음에 안 든다고 여기서")
    print("   기준을 바꾸면 프로브를 돌린 의미가 없다.")
    return code


if __name__ == "__main__":
    sys.exit(main())
