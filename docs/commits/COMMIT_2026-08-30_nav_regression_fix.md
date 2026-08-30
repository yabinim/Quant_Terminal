fix(nav,diag): A-1 회귀 수복 — diag_startup 을 이름 상수 구조로 이관 + 계측 복구

## 요약

A-1(네비게이션 인덱스 → 이름 상수) 배포 후 `regression` 워크플로가 크래시했다.
`automation/diag_startup.py` 가 `_MAIN_NAV_OPTIONS` 를 **문자열 리터럴 튜플로 가정**하고
AST 파싱하고 있었는데, A-1 이 원소를 `ast.Name` 으로 바꿨기 때문이다.

**원인은 lockstep 누락이다.** A-1 을 `app.py` 단독 변경으로 취급했으나,
`automation/` 에 소비자가 있었다. 배포 전 `automation/` 을 grep 하지 않았다.

추가로 **B단계(매도 레이더 4구획 분해)에서 들어간 회귀 1건**도 같이 잡았다.

- `app.py` 27,739 → 27,740줄 (+1, `_timed` 래퍼 복구)
- `automation/diag_startup.py` 378 → 441줄

---

## 1. 장애 내용

```
File "automation/diag_startup.py", line 74, in <module>
    opts = [e.value for e in _nav.value.elts]
AttributeError: 'Name' object has no attribute 'value'
##[error]Process completed with exit code 1.
```

### 베이스라인 측정 (원본 diag 기준)

| 대상 app.py | 통과 | 실패 |
|---|---|---|
| B 이전 (27,696) | 145 | **0** |
| B 배포본 (27,746) | 144 | **1** ← B단계 회귀 |
| A-1 배포본 (27,739) | — | **크래시** |

B단계 회귀는 배포 시점에 이미 있었으나 워크플로를 돌리지 않아 드러나지 않았다.

---

## 2. 파일별 변경

### `app.py` — `PF 매도레이더 계산` 계측 복구 (+1줄)

B단계에서 `_crdf` 중복 호출을 제거하며 `with _timed("PF 매도레이더 계산"):`
래퍼를 주석으로 대체했고, 그 결과 **사이드바 `구간별 소요` 에서 이 구간이
사라졌다.** 호이스팅된 단일 계산 지점에 래퍼를 다시 씌웠다.

```python
with st.spinner("기관급 포트폴리오 레이더를 계산하는 중..."):
    with _timed("PF 매도레이더 계산"):          # ← 복구
        sell_radar_df = build_portfolio_sell_radar_df(filtered_portfolio_df)
```

기능 영향은 없다. 계측 전용이며, 이 값이 없으면 `_tab_hint` 의 소요 시간
안내가 실제보다 짧게 나온다.

### `automation/diag_startup.py` — [1][2][3] 목적 이관 (+63줄)

**인덱스 밀림 위험은 A-1 로 구조적으로 사라졌다.** 같은 검사를 고쳐 쓰는 대신
**새 불변식**을 지키도록 목적을 옮겼다.

#### [1] 메뉴 구성 — 이름 상수로만 묶여야 한다

| 검사 | 내용 |
|---|---|
| 항목 17개 | 기존 유지 |
| **전 항목이 이름 참조** | `ast.Name` 이 아닌 원소(=문자열 리터럴)가 있으면 실패 |
| 참조 상수가 전부 정의됨 | 튜플이 미정의 이름을 쓰면 실패 |
| 라벨이 전부 유일 | 기존 유지 |
| **첫 항목이 `NAV_HOME`** | 로그인 후 착지 지점 |
| **상수 정의 < 튜플 정의** | A-1 이 새로 만든 제약 |
| **상수 정의 < `_render_home`** | 바로가기가 상수를 참조하므로 |

기존의 `index 0 = Daily Risk Gauge` 류 위치 검사 7개는 **삭제**했다.
표시 순서가 곧 튜플 순서가 되어 위치를 고정할 이유가 없어졌다.

#### [2] 인덱스 결합 회귀 금지 (신설)

| 검사 | 내용 |
|---|---|
| `_MAIN_NAV_OPTIONS[n]` 잔재 0 | **주석 제외** 코드에서 0곳 |
| `main_nav_idx` 잔재 0 | 〃 |
| 재배치 로직 제거됨 | `_nav_opts.insert(` 없음 |
| 이동 통로는 라벨 기반 | `pop("main_nav_goto"` 존재 |
| dispatch 분기 17개 | 상수당 정확히 1회 |
| **분기 집합 == 메뉴 집합** | 메뉴에만/분기에만 있는 항목을 이름으로 지목 |

`_strip_comments()` 를 추가했다. 설명 주석에 `_MAIN_NAV_OPTIONS[n]` 이라는
문구가 남아 있어 "잔재 0" 검사가 자기 문서에 걸리는 것을 막는다.
토큰 위치를 공백으로 덮어 **레이아웃을 보존**한다(토큰을 개행으로 이어붙이면
`main_nav == NAV_X` 같은 패턴이 끊긴다 — 1차 구현에서 실제로 발생한 실수다).

#### [3] 시작 페이지 — 이동 표현만 갱신

`main_nav_idx = 12` → `main_nav_goto"] = NAV_GUIDE`.
바로가기 3버튼이 `NAV_DRG`/`NAV_RADAR`/`NAV_WATCHLIST` 를 쓰는지도 확인한다.

#### 섹션 슬라이싱 앵커 8곳 치환

`[5]`~`[13]` 이 `SRC.find("elif main_nav == _MAIN_NAV_OPTIONS[15]:")` 같은
문자열로 탭 구간을 잘라내고 있었다. 앵커가 깨지면 그 안의 검사가 **연쇄로**
전부 실패한다(실측 33건). 전부 상수명으로 바꿨다.

| 인덱스 | 상수 | 용도 |
|---|---|---|
| 15 | `NAV_EARNINGS` | 실적 레이더 지연 게이트 |
| 12 / 14 | `NAV_GUIDE` / `NAV_SETTINGS` | 가이드 구간 [start, end) |
| 7 | `NAV_WATCHLIST` | 워치리스트 알림 게이트 |
| 6 / 7 | `NAV_RADAR` / `NAV_WATCHLIST` | 매도 레이더 구간 |
| 3 / 5 | `NAV_SECTOR` / `NAV_STOCK` | 섹터 탭 구간 |

---

## 3. 검증 결과

| 항목 | 결과 |
|---|---|
| `py_compile` (app.py, diag_startup.py) | ✅ PASS |
| `diag_startup.py` × 신 app.py | **148 통과 / 0 실패**, exit 0 |
| 복원 검증 (변이 정리 후 재실행) | ✅ exit 0, 148 통과 |

### 역검증 — 알려진 불량에서 실패하는가

신 diag × **구 app.py(인덱스 기반 27,746)** → **46건 실패, exit 1** ✅

```
❌ 전 항목이 이름 참조 — 리터럴 17개 전부 지목
❌ 상수 정의 < _render_home — 상수 미정의
❌ _MAIN_NAV_OPTIONS[n] 잔재 0 — 47곳
❌ main_nav_idx 잔재 0 — 7곳
```

### 변이 테스트 7건

| 변이 | 결과 |
|---|---|
| M1 튜플에 문자열 리터럴 1개 혼입 | ✅ 2건 검출 (리터럴 지목 + 분기 집합 불일치) |
| M2 `_MAIN_NAV_OPTIONS[8]` 부활 | ✅ 3건 검출 |
| M3 라벨 중복 | ✅ 검출 |
| M4 dispatch 분기 1개 제거 | ✅ 2건 검출 (누락 상수를 이름으로 지목) |
| M5 `main_nav_idx` 부활 | ✅ 2건 검출 |
| M6 `_timed("PF 매도레이더 계산")` 제거 | ✅ 검출 ← **B단계 회귀 재현** |
| M7 `NAV_HOME` 을 2번 자리로 | ✅ 검출 |

M6 은 1차 시도에서 미검출로 나왔는데, 변이 문자열이 들여쓰기를 깨뜨려
`ast.parse` 가 먼저 죽은 탓이었다. 구문상 유효한 변이로 다시 걸어 확인했다.
**변이가 진단을 크래시시키면 "미검출"과 구분되지 않는다** — 변이 후
`py_compile` 을 먼저 통과시켜야 한다.

---

## 4. 배포 순서 (lockstep)

**두 파일을 함께 올린다.** 하나만 올리면 워크플로가 계속 깨진다.

1. `app.py` → 저장소 루트 (27,740줄 / GitHub 표시 27,739)
2. `automation/diag_startup.py` → (441줄 / GitHub 표시 440)
3. Streamlit 재부팅 **필요** (`app.py` 본문 변경)

---

## 5. 남은 한계 · 후속

- `diag_earnings_landscape.py:30` 은 `elif main_nav == _NAV_ADMIN_APPROVAL:` 를
  앵커로 쓴다. 이 문자열은 A-1 에서 바뀌지 않아 **영향 없음**(확인 완료).
- `check_freshness.py` 지문 표에 `diag_startup` 이 없다. 다음 세션 시작 시
  이 파일은 여전히 "미검증" 상태로 잡힌다 — §6 백로그의 fingerprint 항목에 추가 필요.
- 섹션 슬라이싱이 여전히 **소스 문자열 탐색**에 의존한다. 탭 본문을 크게 옮기면
  또 깨진다(B단계에서 실제로 겪었다). AST 기반 분기 추출로 바꾸는 것이
  근본 해결이지만 이번 범위 밖이다.
