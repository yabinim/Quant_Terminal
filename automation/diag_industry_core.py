"""diag_industry_core.py — 업종 모멘텀·백분위 회귀 검증 + 뮤테이션 테스트.

무엇을 지키려는 검사인가
────────────────────────
이 데이터는 Phase 3 화면에 "상위 8%" 로 표시된다. 틀려도 **에러가 안 난다** —
그냥 잘못된 숫자가 그럴듯하게 찍힌다. 그래서 기계로 확인해야 한다.

특히 위험한 것 셋:

  1. **와이드 시트 열 정렬 붕괴** — 헤더 순서가 어긋나면 A 업종 자리에 B 업종
     값이 들어간다. 화면은 멀쩡해 보이고 숫자만 틀린다. 가장 찾기 어렵다.
  2. **백분위 방향 반전** — 상위/하위가 뒤집히면 최악을 최고로 표시한다.
  3. **결측 처리 실패** — 149개 중 일부는 데이터가 비는데, 이걸 0 으로 채우면
     "변화 없음"으로 읽혀 순위가 부풀려진다.

네트워크·시트 접근 없이 돈다.

    python automation/diag_industry_core.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import industry_core as ic  # noqa: E402

_PASS, _FAIL = [], []


def chk(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name
          + (("  — " + detail) if detail and not cond else ""))
    return cond


def _sheet(inds, rows):
    """rows: [(date, [값...])] → get_all_values 형태"""
    return [[ic.DATE_COL] + list(inds)] + [[d] + list(v) for d, v in rows]


# ══════════════════════════════════════════════════════════════════════════
def test_parse():
    print("\n── A. 시트 파싱")
    v = _sheet(["A", "B"], [("2026-01-05", [1.0, -1.0]),
                            ("2026-01-02", [0.5, -0.5])])
    p = ic.parse_perf_values(v)
    chk("날짜 오래된 순 정렬 (모멘텀 계산 전제)",
        p["dates"] == ["2026-01-02", "2026-01-05"], str(p["dates"]))
    chk("업종별 값이 날짜 순서와 정렬됨",
        p["data"]["A"] == [0.5, 1.0], str(p["data"]["A"]))
    chk("두 번째 업종도 정확히 매핑",
        p["data"]["B"] == [-0.5, -1.0], str(p["data"]["B"]))

    chk("빈 입력 → 빈 결과", ic.parse_perf_values([])["dates"] == [])
    chk("헤더만 → 빈 결과", ic.parse_perf_values([[ic.DATE_COL, "A"]])["dates"] == [])
    chk("첫 열이 Date 가 아니면 거부 (열 정렬 붕괴 방지)",
        ic.parse_perf_values([["날짜", "A"], ["2026-01-02", 1]])["dates"] == [])
    chk("빈 셀 → None (0 으로 채우지 않는다)",
        ic.parse_perf_values(_sheet(["A"], [("2026-01-02", [""])]))
        ["data"]["A"] == [None])
    chk("짧은 행 → None 으로 패딩 (예외 아님)",
        ic.parse_perf_values([[ic.DATE_COL, "A", "B"], ["2026-01-02", 1.0]])
        ["data"]["B"] == [None])
    chk("날짜 형식 불량 행은 버림",
        ic.parse_perf_values(_sheet(["A"], [("bad", [1.0]),
                                            ("2026-01-02", [2.0])]))
        ["dates"] == ["2026-01-02"])


def test_build_row():
    print("\n── B. 행 생성 (열 정렬)")
    hdr = [ic.DATE_COL, "A", "B", "C"]
    recs = [{"industry": "C", "averageChange": 3.0},
            {"industry": "A", "averageChange": 1.0}]
    row, new = ic.build_row(hdr, recs, "2026-01-05")
    chk("응답 순서가 아니라 **헤더 순서**를 따른다",
        row == ["2026-01-05", 1.0, "", 3.0], str(row))
    chk("헤더에 있으나 응답에 없는 업종 → 빈 칸 (0 아님)", row[2] == "")
    chk("신규 업종 없음", new == [])

    row2, new2 = ic.build_row(hdr, recs + [{"industry": "Z", "averageChange": 9.0}],
                              "2026-01-05")
    chk("신규 업종을 **조용히 버리지 않고** 보고한다", new2 == ["Z"], str(new2))
    chk("신규가 있어도 기존 열은 안 밀린다",
        row2[:4] == ["2026-01-05", 1.0, "", 3.0], str(row2))

    chk("잡음 레코드 무시",
        ic.build_row(hdr, [None, "x", {}, {"industry": ""}], "d")[0]
        == ["d", "", "", ""])


def test_momentum():
    print("\n── C. 모멘텀")
    s = [10.0] * 20
    m = ic.momentum(s, 20)
    chk("복리 누적 — +10% 20회면 약 +573%", abs(m - (1.1 ** 20 - 1)) < 1e-9,
        str(m))
    chk("단순합이 아니다 (20일이면 눈에 띄게 어긋난다)", m > 2.0)
    chk("데이터 부족이면 None", ic.momentum([1.0] * 5, 20) is None)
    chk("lookback 0 이면 None", ic.momentum(s, 0) is None)
    chk("빈 시계열이면 None", ic.momentum([], 20) is None)

    # 결측 처리 — 이게 순위를 부풀리는 주범
    partial = [1.0] * 10 + [None] * 10
    chk("결측 50% → None (신뢰하지 않는다)", ic.momentum(partial, 20) is None)
    ok = [1.0] * 16 + [None] * 4
    chk("결측 20% → 계산은 하되 결측일은 0% 취급",
        ic.momentum(ok, 20) is not None and abs(ic.momentum(ok, 20) - (1.01 ** 16 - 1)) < 1e-9)
    chk("MIN_COVERAGE 상수 70%", abs(ic.MIN_COVERAGE - 0.70) < 1e-9)

    # ── 윈저화(clip) ────────────────────────────────────────────────────
    wild = [30.0] * 5 + [1.0] * 15
    raw = ic.momentum(wild, 20)
    win = ic.momentum(wild, 20, clip=5.0)
    chk("clip 없으면 극단값이 그대로 반영", raw > 2.0, str(raw))
    chk("clip 주면 극단값이 잘린다", win < raw, str(win) + " vs " + str(raw))
    chk("clip 이 정확히 ±clip 로 자른다",
        abs(win - ((1.05 ** 5) * (1.01 ** 15) - 1)) < 1e-9, str(win))
    chk("clip 범위 안의 값은 안 건드린다",
        abs(ic.momentum([1.0] * 20, 20, clip=5.0)
            - ic.momentum([1.0] * 20, 20)) < 1e-12)
    chk("clip 은 음수 쪽도 자른다",
        abs(ic.momentum([-30.0] * 20, 20, clip=5.0)
            - ((0.95 ** 20) - 1)) < 1e-9)
    chk("WINSOR_CLIP 상수 5.0", abs(ic.WINSOR_CLIP - 5.0) < 1e-9)

    e, t = ic.count_extremes(wild, 5.0)
    chk("count_extremes — 초과 5 / 전체 20", (e, t) == (5, 20), str((e, t)))
    chk("count_extremes — 결측은 전체에서 제외",
        ic.count_extremes([30.0, None, 1.0], 5.0) == (1, 2))
    chk("count_extremes — lookback 창 적용",
        ic.count_extremes(wild, 5.0, lookback=10) == (0, 10),
        str(ic.count_extremes(wild, 5.0, lookback=10)))

    chk("compute_ranks 에 clip 전달됨",
        ic.compute_ranks({"data": {"A": wild}, "dates": ["d"] * 20},
                         lookbacks=(20,), clip=5.0)["A"]["mom_20"]
        != ic.compute_ranks({"data": {"A": wild}, "dates": ["d"] * 20},
                            lookbacks=(20,))["A"]["mom_20"])

    # end 인자 — 과거 시점 백분위 계산에 쓰인다
    s2 = [1.0] * 10 + [50.0] * 10
    chk("end 인자로 과거 시점 산출 (방향 표시용)",
        abs(ic.momentum(s2, 10, end=10) - (1.01 ** 10 - 1)) < 1e-9)
    chk("end 가 lookback 보다 작으면 None", ic.momentum(s2, 10, end=5) is None)


def test_percentile():
    print("\n── D. 백분위")
    p = ic.rank_percentile({"A": 0.5, "B": 0.1, "C": -0.2, "D": 0.9})
    chk("1위가 가장 작은 값 (상위 백분위)", p["D"] < p["A"] < p["B"] < p["C"],
        str(p))
    chk("1위 = 1/N*100", abs(p["D"] - 25.0) < 1e-9, str(p["D"]))
    chk("꼴찌 = 100%", abs(p["C"] - 100.0) < 1e-9)
    chk("None 은 순위에서 제외",
        "X" not in ic.rank_percentile({"A": 0.5, "X": None}))
    chk("전부 None 이면 빈 결과", ic.rank_percentile({"A": None}) == {})
    chk("빈 입력 → 빈 결과", ic.rank_percentile({}) == {})
    tie = ic.rank_percentile({"A": 1.0, "B": 1.0, "C": 0.0})
    chk("동점은 같은 순위", abs(tie["A"] - tie["B"]) < 1e-9, str(tie))


def test_ranks_and_describe():
    print("\n── E. 통합 · 표시 문자열")
    n = 200
    rows = []
    for i in range(n):
        rows.append(("2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                     [1.0, 0.0, -1.0]))
    v = _sheet(["UP", "FLAT", "DOWN"], rows)
    p = ic.parse_perf_values(v)
    r = ic.compute_ranks(p)
    chk("상승 업종이 1위", r["UP"]["pct_20"] < r["DOWN"]["pct_20"])
    chk("두 창 모두 산출", r["UP"]["mom_20"] is not None
        and r["UP"]["mom_120"] is not None)
    chk("as_of = 마지막 날짜", r["UP"]["as_of"] == p["dates"][-1])
    chk("모집단 수 기록", r["UP"]["n_universe"] == 3)

    s = ic.describe(r["UP"])
    chk("표시 문자열에 '상위' 포함", "상위" in s, s)
    chk("방향 표기 포함", ("↑" in s or "↓" in s or "—" in s), s)
    chk("데이터 없으면 빈 문자열", ic.describe({}) == "")
    chk("백분위 None 이면 빈 문자열", ic.describe({"pct_20": None}) == "")

    # 방향 — 최근에 급등한 업종은 ↑ 가 떠야 한다
    #
    # ⚠️ 이 검사를 처음엔 업종 3개로 짰다가 실패했다. 3개면 백분위 단위가
    #    33%p 라 순위가 움직일 여지가 없고, RISER 가 양쪽 시점 모두 1위여서
    #    변화가 안 잡혔다. **검사 대상이 아니라 검사가 틀렸던 것.**
    #    급등이 최근 DIRECTION_LAG 봉 안에서만 일어나야 t-5 시점 창에는
    #    안 들어간다. 그래야 순위 이동이 생긴다.
    # ⚠️ 일간 값을 WINSOR_CLIP(±5%) 안에 둔다.
    #    처음엔 급등을 30%/일 로 잡았다가 방향 검사가 실패했는데, 원인은
    #    코드가 아니라 **테스트 데이터가 비현실적**이었던 것이다 —
    #    안정성 플래그가 "극단값에 흔들리는 업종"으로 정확히 잡아냈고,
    #    불안정 업종은 설계상 방향(↑↓)을 안 붙인다.
    #    실제 일간 업종 평균이 30% 씩 움직일 리 없으므로 데이터를 고쳤다.
    others = [0.30, 0.25, 0.20, 0.15, 0.10, 0.05, 0.02, -0.05, -0.10]
    rows2 = []
    for i in range(200):
        riser = -0.2 if i < 200 - ic.DIRECTION_LAG else 4.9
        rows2.append(("2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                      [riser] + others))
    names2 = ["RISER"] + ["X%d" % k for k in range(len(others))]
    r2 = ic.compute_ranks(ic.parse_perf_values(_sheet(names2, rows2)))
    chk("최근 급등 업종은 상승 화살표", "↑" in ic.describe(r2["RISER"]),
        ic.describe(r2["RISER"]) + "  (pct_20="
        + str(r2["RISER"].get("pct_20")) + " prev="
        + str(r2["RISER"].get("pct_20_prev")) + ")")

    # 반대 방향도 확인 — 검사가 한쪽만 잡으면 반쪽짜리다
    rows3 = []
    for i in range(200):
        faller = 0.4 if i < 200 - ic.DIRECTION_LAG else -4.9
        rows3.append(("2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                      [faller] + others))
    names3 = ["FALLER"] + ["X%d" % k for k in range(len(others))]
    r3 = ic.compute_ranks(ic.parse_perf_values(_sheet(names3, rows3)))
    chk("최근 급락 업종은 하락 화살표", "↓" in ic.describe(r3["FALLER"]),
        ic.describe(r3["FALLER"]))

    chk("데이터 0행이면 예외 없이 빈 결과",
        ic.compute_ranks({"data": {}, "dates": []}) == {})

    # ── 안정성 플래그 ───────────────────────────────────────────────────
    # 실측에서 Oil & Gas Energy 가 원본 9.6% → 윈저화 92.8% 로 튀었다.
    # 집계 지표(순위 상관 0.941)로는 통과했지만 그 업종은 화면 상위 15 에
    # 뜰 자리였다. 업종별로 남겨야 잡힌다.
    # ⚠️ 이 검사도 처음엔 데이터가 틀렸다. spike 를 [0.5]*195 + [60]*5 로 잡았더니
    #    gap=0 이 나왔는데, **맞는 결과였다** — 윈저화해도 여전히 1위였기
    #    때문이다. 안정성은 **값이 아니라 순위**를 잰다. 값이 아무리 이상해도
    #    순위가 안 바뀌면 화면(백분위)에는 영향이 없으므로 안정이다.
    #    (실측의 Tobacco 가 정확히 이 경우다 — 값 -51.8% 인데 변동 목록에 없었다.)
    #
    #    순위가 흔들리려면 **기저가 남들보다 낮아야** 한다. 급등 덕분에 1위지만
    #    급등을 자르면 중하위로 내려가는 형태 — 그게 Oil & Gas Energy 였다.
    calm = [0.5] * 200
    spike = [-1.5] * 195 + [60.0] * 5
    ynames = ["Y%d" % k for k in range(9)]
    rows4 = []
    for i in range(200):
        rows4.append(("2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                      [calm[i], spike[i]] + others))
    r4 = ic.compute_ranks(ic.parse_perf_values(
        _sheet(["CALM", "SPIKE"] + ynames, rows4)))
    chk("극단값 업종은 불안정으로 표시", r4["SPIKE"]["stable_20"] is False,
        "gap=" + str(r4["SPIKE"].get("gap_20")))
    chk("평온한 업종은 안정", r4["CALM"]["stable_20"] is True,
        "gap=" + str(r4["CALM"].get("gap_20")))
    chk("gap 은 %p 단위 양수", (r4["SPIKE"].get("gap_20") or 0) > ic.STABILITY_GAP)
    chk("STABILITY_GAP 상수 20%p", abs(ic.STABILITY_GAP - 20.0) < 1e-9)
    chk("불안정이면 방향 화살표를 붙이지 않는다",
        "⚠️불안정" in ic.describe(r4["SPIKE"])
        and "↑" not in ic.describe(r4["SPIKE"]),
        ic.describe(r4["SPIKE"]))
    chk("is_stable — 안정 True", ic.is_stable(r4["CALM"]) is True)
    chk("is_stable — 불안정 False", ic.is_stable(r4["SPIKE"]) is False)
    chk("is_stable — 판정 불가(None)는 False (모르면 안 보여준다)",
        ic.is_stable({"stable_20": None}) is False)

    # Tobacco 형 — 값은 극단이지만 윈저화해도 순위가 그대로면 **안정**이다.
    # 화면에 뜨는 건 백분위이므로 순위가 안 바뀌면 표시에 문제가 없다.
    top_wild = [0.5] * 195 + [60.0] * 5       # 기저가 남들보다 높다
    rows5 = []
    for i in range(200):
        rows5.append(("2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                      [top_wild[i]] + others))
    r5 = ic.compute_ranks(ic.parse_perf_values(_sheet(["WILD"] + ynames, rows5)))
    chk("값은 극단이나 순위가 안 바뀌면 안정 (Tobacco 형)",
        r5["WILD"]["stable_20"] is True,
        "gap=" + str(r5["WILD"].get("gap_20")))
    chk("with_stability=False 면 gap 을 안 만든다",
        ic.compute_ranks(ic.parse_perf_values(
            _sheet(["CALM", "SPIKE"] + ynames, rows4)),
            with_stability=False)["SPIKE"]["gap_20"] is None)
    chk("clip 지정 시에도 안정성은 원본 대비로 잰다",
        (ic.compute_ranks(ic.parse_perf_values(
            _sheet(["CALM", "SPIKE"] + ynames, rows4)),
            clip=ic.WINSOR_CLIP)["SPIKE"].get("gap_20") or 0) > 0)


# ══════════════════════════════════════════════════════════════════════════
def test_mutation():
    print("\n── F. 뮤테이션")

    def align_ok():
        """헤더 순서대로 값이 들어가는가."""
        row, _ = ic.build_row([ic.DATE_COL, "A", "B", "C"],
                              [{"industry": "C", "averageChange": 3.0},
                               {"industry": "A", "averageChange": 1.0}], "d")
        return row == ["d", 1.0, "", 3.0]

    def direction_ok():
        """상위 백분위가 작을수록 좋은가."""
        p = ic.rank_percentile({"BEST": 1.0, "WORST": -1.0})
        return p["BEST"] < p["WORST"]

    def missing_ok():
        """결측이 많으면 None 인가."""
        return ic.momentum([1.0] * 5 + [None] * 15, 20) is None

    chk("기준 — 열 정렬 정상", align_ok())
    chk("기준 — 백분위 방향 정상", direction_ok())
    chk("기준 — 결측 방어 정상", missing_ok())

    # M1: 응답 순서대로 행을 만든다 (헤더 무시) → 열 정렬 붕괴
    orig_br = ic.build_row

    def _bad_br(header, records, date_str):
        vals = [float(r["averageChange"]) for r in records
                if isinstance(r, dict) and r.get("industry")]
        return [date_str] + vals, []
    ic.build_row = _bad_br
    caught = not align_ok()
    ic.build_row = orig_br
    chk("M1 응답 순서로 행 생성 (열 정렬 붕괴) → 검출", caught)

    # M2: 백분위 방향 반전
    orig_rp = ic.rank_percentile
    ic.rank_percentile = lambda m: {k: 100.0 - v
                                    for k, v in orig_rp(m).items()}
    caught = not direction_ok()
    ic.rank_percentile = orig_rp
    chk("M2 백분위 방향 반전 (최악을 최고로) → 검출", caught)

    # M3: 결측을 0 으로 채워 커버리지 검사 무력화
    orig_mom = ic.momentum

    def _bad_mom(series, lookback, end=None):
        n = len(series) if end is None else end
        if n < lookback or lookback <= 0:
            return None
        lvl = 1.0
        for v in series[n - lookback:n]:
            lvl *= (1.0 + (v or 0.0) / 100.0)
        return lvl - 1.0
    ic.momentum = _bad_mom
    caught = not missing_ok()
    ic.momentum = orig_mom
    chk("M3 결측을 0 으로 채움 (순위 부풀림) → 검출", caught)

    # M4: 복리 대신 단순합
    def _sum_mom(series, lookback, end=None):
        n = len(series) if end is None else end
        if n < lookback or lookback <= 0:
            return None
        return sum((v or 0.0) for v in series[n - lookback:n]) / 100.0
    ic.momentum = _sum_mom
    m = ic.momentum([10.0] * 20, 20)
    caught = abs(m - (1.1 ** 20 - 1)) > 1e-6
    ic.momentum = orig_mom
    chk("M4 복리 대신 단순합 → 검출", caught)

    # M5: 신규 업종을 조용히 버린다
    def _drop_new(header, records, date_str):
        row, _ = orig_br(header, records, date_str)
        return row, []
    ic.build_row = _drop_new
    _, new = ic.build_row([ic.DATE_COL, "A"],
                          [{"industry": "Z", "averageChange": 1.0}], "d")
    caught = (new == [])
    ic.build_row = orig_br
    chk("M5 신규 업종 조용히 버림 → 검출 (보고 계약 위반)", caught)

    # M6: clip 인자를 무시한다 → 윈저화 진단이 항상 "영향 없음"으로 나온다
    def clip_ok():
        w = [30.0] * 5 + [1.0] * 15
        return ic.momentum(w, 20, clip=5.0) < ic.momentum(w, 20)

    chk("기준 — clip 동작 정상", clip_ok())
    orig_m2 = ic.momentum
    ic.momentum = lambda s_, lb, end=None, clip=None: orig_m2(s_, lb, end=end)
    caught = not clip_ok()
    ic.momentum = orig_m2
    chk("M6 clip 인자 무시 (윈저화 진단 무력화) → 검출", caught)

    # M7: 안정성 판정을 항상 True 로 → 불안정 업종이 그냥 표시된다
    def stability_ok():
        others_ = [0.30, 0.25, 0.20, 0.15, 0.10, 0.05, 0.02, -0.05, -0.10]
        yn = ["Y%d" % k for k in range(9)]
        rows_ = []
        for i in range(200):
            rows_.append(("2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                          [0.5, (-1.5 if i < 195 else 60.0)] + others_))
        rr = ic.compute_ranks(ic.parse_perf_values(
            _sheet(["CALM", "SPIKE"] + yn, rows_)))
        return rr["SPIKE"]["stable_20"] is False

    chk("기준 — 안정성 판정 정상", stability_ok())
    orig_s = ic.STABILITY_GAP
    ic.STABILITY_GAP = 1e9
    caught = not stability_ok()
    ic.STABILITY_GAP = orig_s
    chk("M7 안정성 임계 무한대 (전부 안정 처리) → 검출", caught)

    chk("뮤테이션 원복 후 정상",
        align_ok() and direction_ok() and missing_ok() and clip_ok()
        and stability_ok())


# ══════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 70)
    print("industry_core 회귀 검증 — v" + ic.INDUSTRY_CORE_VERSION)
    print("  네트워크·시트 접근 없음 · 부작용 없음")
    print("=" * 70)
    test_parse()
    test_build_row()
    test_momentum()
    test_percentile()
    test_ranks_and_describe()
    test_mutation()
    print("")
    print("=" * 70)
    print("결과: 통과 " + str(len(_PASS)) + " · 실패 " + str(len(_FAIL)))
    if _FAIL:
        print("")
        for f in _FAIL:
            print("   · " + f)
        print("")
        print("⚠️ 배포하지 말 것. 이 값은 화면에 '상위 8%' 로 찍힌다 —")
        print("   틀려도 에러가 안 나고 그럴듯한 숫자가 그대로 나간다.")
    else:
        print("✅ 전 항목 통과 — 배포 가능")
    print("=" * 70)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
