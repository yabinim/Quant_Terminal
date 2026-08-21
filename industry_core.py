"""industry_core.py — 업종 모멘텀·백분위 SSOT.

무엇을 위한 모듈인가
────────────────────
Phase 3 정밀진단에 **"이 종목의 업종이 149개 중 어디쯤인가"** 를 붙이기 위한
데이터 계층이다. 신호가 아니라 **정보**다 — 매수/매도 판정을 바꾸지 않고
판단 재료만 더한다.

왜 신호가 아닌가
────────────────
업종 모멘텀 백테스트(diag_industry_momentum) 결과가 갈렸다.

    ① 무작위 대조군      ✅ 통과 — 무작위 30회 중 1회만 최고 설정을 이겼다
                            → **순위에 정보가 있다**
    ② 워크포워드         🔴 실패 — 전반 1위가 후반에 동일가중 대비 -15.3%
    ③ 반분할             🟠 1/8
    ④ MDD                🔴 0/18 (다만 집중 전략에 불리한 기준이었다)

정보는 있는데 **거래 규칙으로 전환되지 않았다.** 그래서 순위를 보여주기만 하고
매매 판정에는 연결하지 않는다. 연결은 롤링 워크포워드가 통과한 뒤에 다시 본다.

⚠️ 왜 스냅샷만으로는 안 되나 — 설계의 핵심
──────────────────────────────────────────
`industry-performance-snapshot` 은 **하루치 변화율**이다. 모멘텀이 아니다.
하루 등락으로 149개를 줄 세우면 그냥 잡음이다("상위 8%"가 내일 "하위 30%").

제대로 하려면 20일·120일 누적이 필요한데, 그걸 매일 API 로 받으면
`historical-industry-performance` 149콜/일이 든다. 불가능하다.

그래서 **히스토리를 직접 쌓는다.**

    ① 일회성 백필   historical-industry-performance × 149콜 → 시트에 754행
    ② 매일 1콜      industry-performance-snapshot → 1행 append
    ③ 모멘텀 계산   시트에서 산출 (API 호출 0)

유지비가 하루 1콜이다.

시트 형식 — 왜 와이드인가
─────────────────────────
롱 포맷(날짜·업종·값 3열)이면 149행/일 × 754일 = **11만 행**이다. 시트가
버거워지고 append 도 느리다.

와이드 포맷(날짜 1행 × 업종 150열)이면 **754행**이다. 같은 데이터인데
행 수가 149분의 1이다. 셀 수는 11만으로 동일하지만 Sheets 는 행 수에 훨씬
민감하다.

    Date        | Advertising Agencies | Aerospace & Defense | ... (149개)
    2023-08-21  | 0.42                 | -1.13               | ...

⚠️ 열 순서는 **헤더가 SSOT** 다. 나중에 업종이 추가돼도 기존 열을 밀지 않고
   끝에 붙인다. 밀면 과거 데이터가 통째로 어긋난다.

의존성
──────
`reminders_core` / `calendar_core` 패턴을 따라 **모듈 레벨에서 gspread 를
import 하지 않는다.** requests 도 함수 안에서 지연 import 한다.
app.py 가 나중에 흡수해도 임포트 비용이 붙지 않게 하기 위함이다 —
로딩 시간을 늘리지 않는 것이 이 프로젝트의 기본 제약이다.
"""
from __future__ import annotations

INDUSTRY_CORE_VERSION = "1.0.0"

PERF_SHEET = "Industry_Perf"
DATE_COL = "Date"

# 표시용 모멘텀 창(거래일). 백테스트에서 전 구간 최고는 LB20 이었고,
# 반분할을 유일하게 통과한 것은 LB120 이었다. 어느 쪽이 맞는지 모르므로
# 둘 다 보여주고 관찰한다 — 어차피 신호가 아니라 정보다.
LOOKBACKS = (20, 120)

# 방향 표시용 — 며칠 전 백분위와 비교할 것인가
DIRECTION_LAG = 5          # 약 1주

# 창 안에 값이 이만큼은 있어야 모멘텀을 신뢰한다.
# 업종 데이터는 결측이 흔하다(159개 중 10개는 아예 데이터가 없었다).
MIN_COVERAGE = 0.70

# 윈저화 클립(일간 %). 극단값이 순위를 흔드는지 **재보기 위한** 진단용이다.
#
# 왜 필요한가: averageChange 는 업종 내 상장 종목 전체의 동일가중 평균이라
# 마이크로캡·투기적 소형주가 업종 평균을 통째로 흔든다. 실측에서
# Tobacco 120일 -51.8%, Agricultural Inputs -75.4% 가 나왔는데, 같은 기간
# 실제 담배 대형주(MO/PM/BTI)는 올랐다. 즉 이 버킷은 우리가 아는 그 업종이
# 아니다.
#
# ⚠️ 다만 '크기가 왜곡됐다'가 곧 '순서도 무작위다'는 아니다. 백테스트에서
#    무작위 30회 중 1회만 최고 설정을 이겼으므로 순위에는 정보가 있었다.
#    그래서 채택 여부를 추측으로 정하지 않고, 원본과 윈저화의 **순위 상관**을
#    재서 결정한다.
WINSOR_CLIP = 5.0

# 원본 백분위와 윈저화 백분위가 이만큼(%p) 넘게 벌어지면 **불안정**으로 본다.
#
# 왜 전환이 아니라 플래그인가
# ───────────────────────────
# 실측(2026-08-21, 149업종 3년):
#     [20일]  클립 3.8%  순위상관 0.989  상위15 교집합 14/15
#     [120일] 클립 3.9%  순위상관 0.941  상위15 교집합 12/15
# 집계로는 통과했다. 그런데 개별로 보면
#     Oil & Gas Energy   원본 9.6% → 윈저화 92.8%  (+83.2%p)
# 원본 상위 10% 인데 윈저화하면 하위 8% 다. 단일 종목 아티팩트가 거의
# 확실한데, 이게 **화면 상위 15 에 뜰 자리**에 있었다.
#
# 반대로 Tobacco 는 변동 목록에 없었다 — 크기는 이상해도(120일 -51.8%)
# 버킷 전체가 움직인 것이라 순위는 일관된다.
#
# 즉 원본을 통째로 버릴 근거도, 그냥 믿을 근거도 없다. 그래서 **불일치 자체를
# 신뢰도 신호로** 쓴다. 모르는 것을 아는 척 표시하지 않는 쪽이 맞다.
STABILITY_GAP = 20.0


# ══════════════════════════════════════════════════════════════════════════
# 시트 파싱 — 순수 함수. gspread 를 모르는 채로 값 배열만 받는다.
# ══════════════════════════════════════════════════════════════════════════
def _f(v):
    try:
        s = str(v).strip()
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def parse_perf_values(values) -> dict:
    """Industry_Perf 시트 get_all_values() → 구조화.

    반환: {"dates": [오래된순], "industries": [...], "data": {업종: [값 or None]}}

    헤더가 없거나 깨져 있으면 빈 결과를 돌려준다 — 예외를 던지지 않는다.
    이 데이터는 **표시용**이라, 없어도 앱이 죽으면 안 된다.
    """
    out = {"dates": [], "industries": [], "data": {}}
    if not values or len(values) < 2:
        return out
    hdr = [str(c).strip() for c in values[0]]
    if not hdr or hdr[0] != DATE_COL:
        return out
    inds = [h for h in hdr[1:] if h]
    if not inds:
        return out

    rows = []
    for r in values[1:]:
        if not r:
            continue
        ds = str(r[0]).strip()[:10] if len(r) > 0 else ""
        if len(ds) != 10:
            continue
        rows.append((ds, r))
    rows.sort(key=lambda x: x[0])          # 오래된 순 — 모멘텀 계산 전제

    out["dates"] = [d for d, _ in rows]
    out["industries"] = inds
    for j, nm in enumerate(inds, start=1):
        col = []
        for _, r in rows:
            col.append(_f(r[j]) if j < len(r) else None)
        out["data"][nm] = col
    return out


def build_row(header, records, date_str) -> list:
    """스냅샷 응답 → 헤더 순서에 맞춘 1행.

    ⚠️ 헤더에 없는 업종은 **버리지 않고** 호출측이 알 수 있게 따로 돌려준다
       (new_industries). 조용히 버리면 새 업종이 영영 안 들어온다.
    """
    vals = {}
    for rec in (records or []):
        if not isinstance(rec, dict):
            continue
        nm = str(rec.get("industry") or "").strip()
        if not nm:
            continue
        v = rec.get("averageChange")
        try:
            vals[nm] = float(v)
        except Exception:
            continue
    row = [date_str]
    for nm in header[1:]:
        v = vals.get(nm)
        row.append("" if v is None else v)
    new_inds = sorted(set(vals) - set(header[1:]))
    return row, new_inds


# ══════════════════════════════════════════════════════════════════════════
# 모멘텀 · 백분위 — 순수 계산
# ══════════════════════════════════════════════════════════════════════════
def momentum(series, lookback, end=None, clip=None):
    """최근 lookback 봉의 누적 수익률. 데이터가 모자라면 None.

    series 는 일간 변화율(%) 리스트다. 누적은 복리로 곱한다 —
    단순합은 20일만 돼도 눈에 띄게 어긋난다.

    clip: 주어지면 일간 값을 ±clip(%) 으로 자른 뒤 누적한다(윈저화).
      극단값이 순위에 얼마나 영향을 주는지 재기 위한 것이고, 채택 여부는
      원본과의 순위 상관을 보고 결정한다.
    """
    n = len(series) if end is None else end
    if n <= 0 or lookback <= 0 or n < lookback:
        return None
    window = series[n - lookback:n]
    seen = [v for v in window if v is not None]
    if len(seen) < lookback * MIN_COVERAGE:
        return None                          # 결측이 많으면 신뢰하지 않는다
    lvl = 1.0
    for v in window:
        if v is None:
            continue
        if clip is not None:
            v = max(-clip, min(clip, v))
        lvl *= (1.0 + v / 100.0)
    return lvl - 1.0


def count_extremes(series, clip, lookback=None):
    """창 안에서 |값| > clip 인 일수와 전체 유효 일수 → (초과, 전체)."""
    w = series if lookback is None else series[max(0, len(series) - lookback):]
    seen = [v for v in w if v is not None]
    return sum(1 for v in seen if abs(v) > clip), len(seen)


def rank_percentile(mom_map) -> dict:
    """{업종: 모멘텀} → {업종: 상위 백분위}.

    상위 백분위는 **작을수록 좋다.** 1위가 1/N*100 이다.
    동점은 같은 순위를 준다(경쟁 순위).
    """
    items = [(m, nm) for nm, m in mom_map.items() if m is not None]
    if not items:
        return {}
    items.sort(reverse=True)
    n = len(items)
    out, prev_m, prev_rank = {}, None, 0
    for i, (m, nm) in enumerate(items, start=1):
        rank = prev_rank if (prev_m is not None and m == prev_m) else i
        out[nm] = 100.0 * rank / n
        prev_m, prev_rank = m, rank
    return out


def compute_ranks(parsed, lookbacks=LOOKBACKS, direction_lag=DIRECTION_LAG,
                  clip=None, with_stability=True,
                  stability_gap=None) -> dict:
    """파싱 결과 → {업종: {mom_20, pct_20, pct_20_prev, gap_20, stable_20, ...}}.

    pct_*_prev 는 direction_lag 봉 전의 백분위다. "상위 8% (1주 전 24%)" 처럼
    **방향**을 보여주기 위한 것이다. 지금 좋은 것보다 올라오는 중인 것이
    초기 수혜주 발굴 철학에 맞다.

    with_stability=True 면 윈저화 백분위를 함께 구해 **불일치 폭**을 남긴다.

        gap_{lb}     |원본 백분위 − 윈저화 백분위|  (%p)
        stable_{lb}  gap <= stability_gap 인가

    극단값이 특정 업종의 순위만 크게 흔드는 경우가 실제로 있다
    (Oil & Gas Energy: 9.6% → 92.8%). 집계 지표로는 안 잡히므로 업종별로
    남겨야 한다. 추가 API 호출은 없다 — 같은 시계열을 한 번 더 계산할 뿐이다.

    clip 을 주면 **윈저화 쪽이 주 백분위**가 된다. 그 경우에도 안정성 비교는
    원본 대비로 계산한다(윈저화끼리 비교하면 항상 0 이라 의미가 없다).
    """
    # ⚠️ 기본값을 `stability_gap=STABILITY_GAP` 으로 쓰면 **정의 시점에 값이
    #    박힌다.** 나중에 상수를 조정해도 반영되지 않아, 튜닝했다고 생각하는
    #    사람과 실제 동작이 어긋난다(뮤테이션 M7 이 이걸 잡아냈다).
    #    None 으로 받고 호출 시점에 읽는다.
    if stability_gap is None:
        stability_gap = STABILITY_GAP

    data = parsed.get("data") or {}
    dates = parsed.get("dates") or []
    n = len(dates)
    out = {nm: {} for nm in data}
    if n == 0:
        return out

    need_wins = with_stability or (clip is not None)

    for lb in lookbacks:
        raw = {nm: momentum(s, lb) for nm, s in data.items()}
        pct_raw = rank_percentile(raw)
        pct_wins = {}
        if need_wins:
            wins = {nm: momentum(s, lb, clip=WINSOR_CLIP)
                    for nm, s in data.items()}
            pct_wins = rank_percentile(wins)

        primary_mom = (wins if clip is not None else raw)
        primary_pct = (pct_wins if clip is not None else pct_raw)

        prev_end = n - direction_lag
        if prev_end >= lb:
            prev = {nm: momentum(s, lb, end=prev_end, clip=clip)
                    for nm, s in data.items()}
            pct_prev = rank_percentile(prev)
        else:
            pct_prev = {}

        for nm in data:
            out[nm]["mom_%d" % lb] = primary_mom.get(nm)
            out[nm]["pct_%d" % lb] = primary_pct.get(nm)
            out[nm]["pct_%d_prev" % lb] = pct_prev.get(nm)
            a, b = pct_raw.get(nm), pct_wins.get(nm)
            if need_wins and a is not None and b is not None:
                g = abs(a - b)
                out[nm]["gap_%d" % lb] = g
                out[nm]["stable_%d" % lb] = bool(g <= stability_gap)
            else:
                out[nm]["gap_%d" % lb] = None
                # 판정 불가는 '불안정'이 아니라 '모름'이다. 없는 정보를
                # 안정으로 단정하면 과신이 된다 → None 으로 남긴다.
                out[nm]["stable_%d" % lb] = None

    for nm in out:
        out[nm]["as_of"] = dates[-1]
        out[nm]["n_universe"] = len(
            [1 for x in data.values()
             if momentum(x, lookbacks[0], clip=clip) is not None])
    return out


def describe(rank_row, lookback=LOOKBACKS[0]) -> str:
    """표시 문자열. '상위 8% (↑ 1주 전 24%)' 형태. 없으면 빈 문자열.

    UI 문자열을 코어에 둔 이유: app 과 자동화(이메일)가 같은 표현을 쓰게
    하기 위함이다. 표현이 갈리면 같은 값을 두 곳에서 다르게 읽는다.

    ⚠️ 불안정 업종은 방향(↑↓)을 붙이지 않는다. 백분위 자체가 극단값에
       흔들리는데 그 변화를 방향으로 읽으면 잡음을 신호로 만든다.
    """
    if not rank_row:
        return ""
    p = rank_row.get("pct_%d" % lookback)
    if p is None:
        return ""
    s = "상위 %.0f%%" % p
    if rank_row.get("stable_%d" % lookback) is False:
        return s + " ⚠️불안정"
    q = rank_row.get("pct_%d_prev" % lookback)
    if q is not None:
        if q - p >= 3.0:
            s += " (↑ 1주 전 %.0f%%)" % q
        elif p - q >= 3.0:
            s += " (↓ 1주 전 %.0f%%)" % q
        else:
            s += " (— 1주 전 %.0f%%)" % q
    return s


def is_stable(rank_row, lookback=LOOKBACKS[0]) -> bool:
    """표시해도 되는가. **판정 불가(None)는 불안정으로 취급한다** —
    모르는 것을 보여주지 않는 쪽이 과신 방지에 맞다."""
    return rank_row.get("stable_%d" % lookback) is True


# ══════════════════════════════════════════════════════════════════════════
# FMP 조회 — 자동화 전용. 표시 경로에서는 절대 호출하지 않는다.
# ══════════════════════════════════════════════════════════════════════════
_BASE = "https://financialmodelingprep.com/stable"


def _get(path, key, timeout=20.0):
    import requests                          # 지연 import — 임포트 비용 회피
    sep = "&" if "?" in path else "?"
    try:
        r = requests.get(_BASE + "/" + path + sep + "apikey=" + str(key),
                         timeout=timeout)
        if r.status_code != 200:
            return None, "HTTP " + str(r.status_code)
        d = r.json()
    except Exception as e:
        return None, type(e).__name__
    if isinstance(d, dict):
        return None, "ERRMSG"
    if not isinstance(d, list):
        return None, "ODD"
    return d, ""


def fetch_industries(key):
    d, err = _get("available-industries", key)
    if err or not d:
        return []
    return sorted({str(r.get("industry") or "").strip()
                   for r in d if isinstance(r, dict) and r.get("industry")})


def fetch_snapshot(key, date_str):
    d, err = _get("industry-performance-snapshot?date=" + str(date_str), key)
    return (d or []), err


def fetch_history(key, industry, d_from, d_to):
    import requests
    q = requests.utils.quote(str(industry))
    d, err = _get("historical-industry-performance?industry=" + q
                  + "&from=" + str(d_from) + "&to=" + str(d_to), key)
    return (d or []), err
