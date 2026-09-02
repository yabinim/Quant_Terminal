# fix(hidden-alpha): 앱 라이브 재계산 경로에 rotation_core 게이트 연결 + 배선 회귀 진단

## 배경 — 무엇이 잘못돼 있었나

2026-09-01 에 `rotation_core.py` 를 만들어 유동성·AUM·기초자산 중복·크립토 캡
게이트를 도입하고 `run_hidden_alpha.py` 에 연결했다. 앱의 **주 경로**
(`load_hidden_alpha_ranks` → `HiddenAlpha_Snapshot`)도 같은 날 자동화가 저장한
`selected` 를 읽도록 정리됐다.

그런데 **관리자 전용 「지금 돈이 몰리는 미지의 ETF 찾기」 버튼**만 라이브
재계산 경로였고, 여기에는 게이트가 **하나도** 걸려 있지 않았다.
`cached_etf_universe_rankings_full` 은 1개월 수익률 정렬 +
`rk_1w<=5 & rk_1m<=5` 주도주 표식이 전부다 — 저유동성(KMCA)·래퍼 중복
(THYP/BHYP/HYPG)이 상위로 몰리던 그 로직 그대로다. 그리고 그 아래에
"상위 3~5개 ETF에 분할 투자" 를 권하고 있었다.

`diag_rotation_policy.py` 는 이때 **80/80 초록불**이었다. `rotation_core` 의
순수 함수만 검사하고 호출부 배선을 보지 않았기 때문이다. 게이트가 아무리
옳아도 안 부르면 화면은 게이트 없는 순위표다.

## 파일별 변경

### `app.py` (28,038 → 28,218 · GitHub 표시)

1. **`import rotation_core as rot`** 추가 (43줄 아래).
   별칭이 자동화와 다른 이유를 주석으로 못 박았다 — 이 파일에서 `rc` 는 이미
   `regime_core` 다. `run_hidden_alpha.py` 는 `regime_core` 를 안 쓰므로 거기선
   `rc` 가 `rotation_core` 다. **두 파일 사이에 코드를 옮겨 붙일 때 반드시 확인.**
   `calendar_core as mcal` 과 같은 종류의 함정이다.

2. **`cached_hidden_alpha_gates()` 신설** (`_HA_VERIFY_TOP_N = 15` 포함).
   순위 오름차순 티커 튜플 → `{selected, excluded, detail, adv, crypto, meta}`.
   - 가격·거래량: `scanner_core._fmp_price_history` 가 **이미 Close/Volume 을
     함께 돌려준다.** 새 OHLCV 헬퍼를 만들지 않고 있는 배관을 썼다.
   - 창 깊이는 `rot.REQUIRED_BARS` 가 소유한다. 여기 숫자를 다시 쓰면 두 벌이 된다.
   - 메타 조립 순서는 `run_hidden_alpha.verify_and_gate` 와 동일하다.
     `profile.companyName` 이 시트/etf-list 이름을 **이긴다** — 신규 상장 ETF 는
     이름이 여기서 처음 채워지고, 이름이 없으면 레버리지 정규식은 아무것도
     주장하지 못한다(SPAX 가 뚫렸던 자리).
   - 거래량이 빈 티커는 프레임에서 빠진다 → '판정 불가' → 제외. **0 으로 채우지
     않는다** — 0 은 '거래 없음'이라는 다른 주장이다.

3. **버튼 경로**에서 랭킹 계산 직후 게이트를 계산해
   `st.session_state["_hidden_alpha_gates"]` 에 저장. 실패 분기는 `None` 으로 초기화.

4. **렌더**: 순위표 티커에 `✅`(게이트 통과 슬롯) 접두, 그 아래
   「🔒 로테이션 게이트」 블록(최종 슬롯 / 제외 사유 + 상세). 기존
   "상위 3~5개에 분할 투자" 문구를 **"분할 투자 대상은 ✅ 최종 슬롯"** 으로 교체.
   게이트 결과가 없는 상태(옛 세션)에서는 원시 순위표임을 캡션으로 명시한다.

**랭킹 표는 걸러내지 않는다.** 게이트는 `selected`/`excluded` 를 따로 돌려줄 뿐
순위표는 전원 유지한다. `rotation_core` 설계 의도(정렬 책임은 호출부, 게이트는
슬롯 선정만)와 같고, `build_pool_monthly_returns_table`(다른 소비자)도 전원
목록을 기대한다. 이 불변식은 W-11 이 못 박는다.

### `diag_rotation_policy.py` (301 → 607) — **80 → 111 검사**

불변식 **I8(소비자 배선)** 추가. 새 그룹 두 개:

- **[W] 정적 분석 (W-0 ~ W-12)** — AST 기반. 주석·문서열의 같은 글자에 안 속는다.
  import 존재 · **별칭 충돌 금지**(rotation_core 를 `rc` 로 두면 `rc.classify_regime`
  이 AttributeError) · `apply_rotation_gates` 실제 호출(양쪽) · `slots` 와 창 깊이가
  리터럴이 아니라 `rot.*` 참조 · `reason_label` 로 사유 번역 · 사전확정 임계값
  로컬 재정의 금지 · **랭킹 캐시 안에서 게이트 금지** · 버튼 경로 호출.
- **[W-R] 스텁 실행 (W-R0 ~ W-R11)** — `app.py` 를 import 하지 않고(Streamlit
  의존) AST 로 함수 본문만 떼어내 스텁 위에서 **실제로 돌린다**. 정적 분석은
  "부르긴 부른다"까지만 증명하고, 넘기는 자료의 모양이 틀리면 게이트는 조용히
  전원 제외를 돌려준다. 네트워크 0.
- **[W-P] 양성 대조 4건** — AST 헬퍼가 알려진 불량 소스에서 실제로 실패하는지.

### `check_freshness.py` (185 → 202)

- `app.py` 마커에 `cached_hidden_alpha_gates` 추가 → **패치 전 사본은 6/7 로 드러난다**
- `rotation_core.py`(5마커) · `run_hidden_alpha.py`(3마커) 지문 등록
- `CROSS_TARGETS` 에 `rotation_core` → app.py ↔ rotation_core 심볼 교차 검사 가동
- `AUTOMATION` 튜플화 + `run_hidden_alpha.py` 를 자동화 정합성 검사에 추가
- `path()` 에 **`automation/` 폴백** — 저장소에서 자동화 스크립트는 `automation/`
  아래 있고 프로젝트 사본은 평평하다. 폴백이 없으면 둘 중 한 배치에서 영구히
  '사본 없음'이 뜬다. `run_hidden_alpha` 등록에 필수라 함께 넣었다.

### `run_hidden_alpha.py` · `rotation_core.py` — **무변경**

lockstep 확인용으로만 읽었다. 게이트 로직은 이미 옳았고 배선만 빠져 있었다.

## 검증

| 항목 | 결과 |
|---|---|
| `py_compile` | app.py · diag · check_freshness 전부 OK |
| `pyflakes` 델타 | **0** (기존 34건 유지, 줄 번호만 이동) |
| `check_py311` | 3/3 호환 |
| `diag_rotation_policy` | **111/111** (기존 80 + 신규 31) |
| 역검증 (패치 전 `app.py`) | W-1·3·4·6·7·8·12·W-R0 **8건 실패** — 잡으려던 결함을 정확히 잡는다 |
| 역검증 (지문) | 패치 전 `app.py` → `6/7 ⚠ 누락: cached_hidden_alpha_gates` |
| 변이 시험 | **13/13 검출 · 무효 0** (각 변이 `py_compile` 사전 검증) |
| split 배치 재현 | 루트 + `automation/` 분리 배치에서 111/111 · 지문 정상 |

### 변이 시험에서 드러난 **테스트 설계 결함 2건** (수정 후 재통과)

1. **M10 놓침** — W-R3(순서 보존) 픽스처가 이미 알파벳 순이라
   `head = sorted(head)` 변이가 무동작이었다. 픽스처를 비알파벳 순
   (`EEE, AAA, BBB, …`)으로 바꿔 판별력을 만들었다.
2. **M13 놓침** — W-R6(레버리지 재검출)에서 `name_map` 을 비워 둬서
   `if cn and not nm` 변이(= profile 이름이 시트 이름을 못 이김)가 보이지
   않았다. 무해한 이름을 미리 넣어 '이긴다'를 못 박았다.

둘 다 코드 결함이 아니라 **검사가 옳은 이유로 통과하지 않고 있던 것**이다.
변이 시험 없이 111/111 을 믿었으면 이 두 구멍은 그대로 남았다.

## 남은 한계

- `주도주`(🔥) 표식은 여전히 `rk_1w<=5 & rk_1m<=5` 순위 기준이다. 게이트와 다른
  축이라 그대로 뒀고, 캡션에서 "보유 판정이 아니다"를 명시했다. 두 표식이 같은
  열에 섞여 있는 것은 남은 UX 부채다.
- `cached_etf_universe_momentum_rankings`(app.py) 는 저장소 전체에서 **소비자 0**
  인 죽은 호환 래퍼다. 범위 밖이라 손대지 않았다.
- 게이트 판정은 상위 `_HA_VERIFY_TOP_N=15` 까지만이다. 16위 밖은 판정하지 않으며
  화면에도 그렇게 적었다.
- W-R 그룹은 `cached_hidden_alpha_gates` 가 없으면 **실행되지 않고 사라진다**
  (검사 수가 줄어든다). W-R0 이 그 경우를 실패로 잡는 파수꾼이다.
- `check_freshness` 의 나머지 절반(`run_narrative`·`run_drg_*`·`diag_*` 지문
  7건, 자동화 6개 확장)은 **이번 범위 밖**이다. 별건으로 남는다.
