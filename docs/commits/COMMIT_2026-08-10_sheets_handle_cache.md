perf(sheets): 워크시트 핸들 캐시 + 워치리스트 제자리 수정으로 Sheets 왕복 15콜→3콜

앱 체감 지연의 대부분은 Streamlit rerun 이 아니라 Google Sheets API 왕복 횟수였다.
호출 자체를 줄이는 세 갈래(핸들 캐시 · 제자리 수정 · 저장 후 재읽기 제거)를 적용하고,
드리프트 방지 가드를 뮤테이션 테스트로 검증했다.

---

## 1. 문제 실측

워치리스트 종목 1개 수정 시 발생하던 왕복:

| 구간 | 호출 | 콜 |
|---|---|---|
| `delete_from_watchlist` | `gc.open()` 2 + `sh.worksheets()` 1 + `get_all_values` 1 + `delete_rows` 1 | 5 |
| `add_to_watchlist` | `gc.open()` 2 + `sh.worksheets()` 1 + `get_all_values` 1 + `_safe_append_rows`(read 1 + update 1) | 6 |
| `st.rerun()` 후 재읽기 | `gc.open()` 2 + `sh.worksheets()` 1 + `get_all_values` 1 | 4 |
| **합계** | | **15콜** |

`gc.open(제목)` 은 Drive `files.list` 검색 + Sheets 메타데이터 조회로 매번 2왕복이다.
`app.py` 에 36군데(전용 함수 17개 + 인라인 19군데) 흩어져 있어, 시트를 건드릴 때마다
데이터를 읽기도 전에 3왕복을 버리고 시작했다.

---

## 2. 파일별 변경

### `app.py`

**(A) 스프레드시트/탭 핸들 캐시 — 36곳 전환**

- `_open_quant_db()` / `_ws_handle(탭명)` 을 `@st.cache_resource(ttl=600)` 로 신설.
  핸들은 (스프레드시트 ID, 탭 ID) 참조라 사용자와 무관해 전 세션 공유가 안전하다.
- `open_*_worksheet` 17개 + 인라인 호출 19곳을 전부 핸들 경유로 전환.
- `_ws_handle_required()` 는 탭 부재 시 **'not found' 문구를 포함한** 예외를 던진다.
  기존 호출부(narratives / portfolios / trade_history)가 `str(exc).lower()` 에
  'not found' 가 있는지로 탭 자동 생성 분기를 타기 때문 — 문구를 바꾸면 자동 생성이
  조용히 죽는다.
- `add_worksheet` / `add_cols` 직후 `_invalidate_ws_handles()` 호출.
  없는 탭에 대해 `None` 이 10분간 캐시되면 생성 직후에도 계속 `None` 이 나온다.
- `ttl=600` 은 안전장치다. 자동화가 탭을 추가·삭제해도 최대 10분 내 자연 복구된다.

**(B) `update_watchlist_row()` 신설 — 삭제+재추가(9콜) → 제자리 수정(3콜)**

변경된 필드만 1칸짜리 range 로 `batch_update` 한다. 드리프트 방지 4중 가드:

1. 행 번호는 방금 읽은 스냅샷에서만 얻는다 (기억해 둔 옛 좌표 사용 금지)
2. 기록 직전 **별도의 새 읽기**(`A{r}:B{r}`)로 좌표를 재확인한다
3. 변경 칸만 단일 셀로 기록 — 연속 범위를 덮지 않아 열 밀림 여지가 없다
4. `_WL_EDITABLE_COL_IDX` 화이트리스트(B~M 중 10개)만 허용, A열(ID) 금지

**(C) 워치리스트 세션 캐시 레이어 — 저장 후 재읽기 제거**

`@st.cache_data` 는 외부에서 값 주입이 불가능해(`clear` 만 가능) 저장할 때마다
"쓰기 → 캐시 폐기 → rerun → 시트 재읽기" 가 강제됐다. 그 위에 얇은 세션 레이어를
얹어, 쓰기 성공 시 메모리 목록만 갱신한다. 조회 진입점은 `get_watchlist_items()`
(호출부 8곳 전환). `load_watchlist_sheet` 직접 호출은 금지.

**(D) `_wl_row_to_item` / `_wl_item_to_row` 직렬화 SSOT 추출**

`save_watchlist_sheet` 와 `update_watchlist_row` 가 같은 규칙을 쓰도록 통일.
반환 길이를 항상 `_WL_NCOL`(13)로 강제 정규화한다.

**(E) 관리자 전용 Sheets 호출 계측 패널**

gspread 의 유일한 HTTP 진입점(`HTTPClient.request` / 5.x 는 `Client.request`)을
1회 래핑해 왕복 횟수·누적 시간을 집계한다. 상위 메서드를 감싸면
`get_all_values → get_values → get` 처럼 중복 집계되므로 최하위만 감싼다.
사이드바 하단 「🔧 Sheets 호출 계측」에서 초기화 → 동작 1회 → 증가분 확인.

### `automation/diag_watchlist_writepath.py` (신규)

`app.py` 에서 대상 함수만 AST 로 떼어내 가짜 워크시트 위에서 실행하는 회귀 테스트.
Streamlit·gspread·네트워크 없이 돌아간다. 뮤테이션 6종으로 가드 유효성 검증.

### `automation/diag_watchlist_writepath.yml` (신규)

`workflow_dispatch` 전용. 의존성 설치 없음(순수 정적 검증, 10초 내).

---

## 3. 설계 근거와 기각한 대안

**기각 1 — `_safe_append_rows` 내부 `get_all_values()` 제거 (1콜 절약)**
자동화(`run_scanner_scan.py`)가 같은 Watchlist 시트에 append 한다. 앱이 캐시한
마지막 행 번호는 신뢰할 수 없고, 이 읽기가 계단식 드리프트를 막는 안전장치다.
1콜 아끼려다 과거의 그 버그를 되살릴 수는 없다. **그대로 유지.**

**기각 2 — 세션 레이어로 `@st.cache_data` 대체 (C-2안)**
캐시 계층이 하나로 정리되지만 `cache_data` 가 주던 세션 간 공유가 사라져
게스트 수만큼 콜드 리드가 늘어난다. 계층 둘은 주석으로 해결 가능하므로 C-1 채택.

**기각 3 — 행 전체를 한 번에 덮어쓰기 (2콜, 1콜 더 적음)**
자동화가 L열(`Alert_LastState`)에 쓰는데 앱이 옛 스냅샷 기준으로 전체 행을 덮으면
자동화 결과가 사라진다. 변경 칸만 쓰면 이 충돌이 원천 차단되고, 숫자 표기가
`"150"` → `"150.0"` 으로 바뀌는 부작용도 없어진다. **1콜 더 쓰고 안전을 산다.**

**설계 수정 기록 — G2 가드가 죽은 코드였음 (뮤테이션 테스트로 발견)**
최초 구현은 좌표 재확인을 행 번호와 **같은 스냅샷**에서 했다. 항상 참이라 아무것도
막지 못하는 무의미한 가드였다. 뮤테이션 테스트에서 "이 가드를 부숴도 전부 통과"로
드러나 별도의 새 읽기로 재설계했다. 이 때문에 수정 경로가 2콜 → 3콜이 됐다.
설계안에서 "2콜" 이라 예고했던 부분을 3콜로 정정한다.

---

## 4. 검증 결과

```
py_compile app.py                → OK
check_py311.py app.py            → OK  (AST feature_version=(3,11) 백스톱 포함)
diag_watchlist_writepath.py      → 원본 전 항목 통과 + 뮤테이션 6/6 탐지
```

**뮤테이션 탐지 내역**

| 부순 가드 | 탐지 |
|---|---|
| G3 직렬화 정규화 제거 | 8건 |
| G4 화이트리스트를 전 컬럼으로 확대 | 2건 (L열 오염 포착) |
| G2 좌표 재확인 제거 | 2건 |
| G2 재확인을 옛 스냅샷으로 되돌림(죽은 가드 재발) | 1건 |
| 행 탐색에서 소유자 조건 제거 | 1건 |
| 변경 감지 무력화(전 필드 재기록) | 4건 |

**커버된 시나리오**: 게스트 행 무손상 · 같은 티커 다른 소유자 분리 · 미존재 행
`not_found` 폴백 · 빈 시트/헤더만 · 구 8열 레코드 정규화 · 스냅샷 이후 행 이동 시 거부 ·
L열 자동화 소유 영역 보호 · 무변경 시 쓰기 생략 · 세션 레이어 사용자 분리 ·
미적재 사용자에게 부분 목록 굳히지 않기

**왕복 실측 (가짜 시트)**: 수정 경로 `get_all_values` 1 + `get` 1 + `batch_update` 1 = **3콜**

---

## 5. 배포 순서 (lockstep)

`app.py` 단일 파일 변경이라 자동화 스크립트 동시 배포는 **불필요**하다.
공유 코어 모듈(`regime_core` / `narrative_core` / `users_core` / `portfolio_core` /
`accounts_core`)과 자동화 7종은 이번 변경에 포함되지 않는다.

1. `app.py` → 저장소 루트 덮어쓰기
2. `automation/diag_watchlist_writepath.py` → 신규 추가
3. `.github/workflows/diag_watchlist_writepath.yml` → 신규 추가
4. Streamlit 재부팅 (`@st.cache_resource` 신설분 반영을 위해 필수)
5. Actions 에서 「🔍 진단 — 워치리스트 쓰기 경로 드리프트 가드」 수동 1회 실행

---

## 6. 배포 후 확인 절차

1. 사이드바 「🔧 Sheets 호출 계측」 → **계측 초기화**
2. 워치리스트에서 종목 1개 수정 → 저장
3. 증가분이 **3콜** 이면 정상 (종전 15콜)
4. Google Sheets `Watchlist` 탭 직접 확인:
   - 수정한 행이 **제자리**에 있는지 (맨 아래로 이동하지 않았는지)
   - A열이 비어 오른쪽으로 밀린 행이 없는지
   - 손절가·목표가·Account·Alert_States 가 보존됐는지
   - L열(`Alert_LastState`)이 초기화되지 않았는지

---

## 7. 남은 한계 / 후속 과제

- **포트폴리오·매도 경로는 미적용(3안 범위).** `replace_user_portfolio_sheet_rows` 는
  여전히 `ws.clear()` 후 **전 사용자 행을 통째로 재작성**한다. 게스트가 늘수록 느려지고
  자동화와 동시 쓰기 시 lost update 위험이 있다. 워치리스트에서 이번 패턴이 검증된 뒤
  같은 방식으로 확장하는 것이 다음 단계.
- **페이지 이동 개선은 부분적.** 핸들 캐시로 콜드 리드가 4콜 → 1콜이 됐지만,
  `@st.cache_data(ttl=300)` 만료 후 첫 상호작용은 여전히 시트를 읽는다. 실사용
  계측치를 모은 뒤 TTL 조정 여부를 판단한다.
- **`update_watchlist_row` 는 워치리스트 수정 버튼에서만 사용 중.** 다른 경로
  (스캐너 원클릭 추가 등)는 기존 `add_to_watchlist` 를 그대로 쓴다 — 신규 추가는
  제자리 수정 대상이 아니므로 정상이다.
- **`check_py311.py` 는 저장소 것을 그대로 쓴다.** 프로젝트 사본에 없어 검증용으로
  동등 기능을 로컬 재작성해 사용했다. 저장소 파일을 덮어쓰지 않았다.
- **동시성 잔여 창.** 좌표 재확인과 기록 사이에 사실상 0에 가까운 창이 남는다.
  Sheets API 에 조건부 쓰기가 없어 완전 제거는 불가능하며, 현 구조에서 실용적으로
  가능한 최선이다.
