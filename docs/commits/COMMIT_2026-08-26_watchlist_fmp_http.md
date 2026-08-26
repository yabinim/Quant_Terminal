# fix(fmp): run_watchlist_alerts 원시 FMP 호출 4곳 → fmp_http SSOT 전환

## 배경 — 무엇을 사는 건지 (정직하게)

이 전환은 **분당 한도 문제를 고치는 게 아니다.** 실측 호출량은 5PM 약 90콜,
2PM 약 180콜로 한도(200/분) 아래이고 순차 호출이라 캡에 닿지 않는다.

실제로 사는 것은 세 가지다.

1. **재시도.** 전환 전에는 429 한 번이나 타임아웃 한 번이면 그 종목이 곧바로
   `_nodata` 로 떨어졌다.
2. **그 결과 A-2b 가 헛돌았다.** 우리 쪽 일시 실패인데 원인을 모르니 `profile` 을
   **한 콜 더 써서** 조회하고, `isActivelyTrading=True` 를 보고 🟡 "일시적 데이터
   공백" 으로 이메일에 썼다. 콜도 낭비하고 알림도 부정확했다.
3. **단일 카운터 편입** — 다른 워크플로와 같은 분에 겹칠 때.

---

## 변경 파일

| 파일 | 위치 | 줄 수 | 이전 |
|---|---|---|---|
| `run_watchlist_alerts.py` | `automation/` | **2,040** | 2,000 |
| `diag_nodata_radar.py` | `automation/` | **1,046** | 1,004 |
| `diag_fmp_ssot.py` | `automation/` | **773** | 774 |
| `market_2pm_weekday.yml` | `.github/workflows/` | **56** | 48 |
| `market_5pm_weekday.yml` | `.github/workflows/` | **141** | 135 |

---

## 1. `run_watchlist_alerts.py` — 4곳 전환

| 함수 | 엔드포인트 | 비고 |
|---|---|---|
| `_fmp_price_history` | `historical-price-eod/full` | 계약 불변(빈 `DataFrame`) |
| `_fmp_quote_price` | `quote` | 계약 불변(`None`) |
| `_fmp_profile_status` | `profile` (A-2b) | **상태코드를 두 번째 반환값에서 읽는다** |
| `_fmp_actively_trading_symbols` | `actively-trading-list` (A-2c) | 계약 불변(`None` ≠ 빈 집합) |

### 반환 계약은 일부러 바꾸지 않았다

`(df, kind)` 로 넓히면 nodata 회계 · 이메일 렌더 · `diag_nodata_radar` 139항목까지
파급된다. **실운용 발송 경로**다. 재시도를 먼저 넣으면 일시 실패 자체가 줄어
위 2번의 빈도가 먼저 내려간다. `kind` 를 원인 분류에 태우는 것은 **Phase 2**.

### `_fmp_profile_status` 의 함정

`fmp_get_ex` 는 402/에러일 때 `r=None` 을 준다. 기존 코드처럼 `r.status_code` 를
쓰면 **AttributeError** 가 난다. 상태는 두 번째 반환값에서 읽도록 고쳤다.

또한 재시도까지 소진한 실패를 `"gone"` 으로 흘려보내지 않는다 — 우리 쪽 네트워크
문제를 티커 소멸로 오진하면 이메일에 🟠 "티커 소멸(개명 추정)" 이 뜬다.

### `_FMP_TIMEOUT` 15 → 20

`diag_fmp_depth` 로 **FMP 가 `historical-price-eod` 의 `limit` 을 무시**한다는 것이
확인됐다(limit=500 요청 → 1254봉 수신). 이 파이프라인의 모든 이력 호출은 `limit`
값과 무관하게 항상 5년치 페이로드를 받는다. 15초는 빠듯하다.

### `import requests` 제거 — 규칙이 아니라 구조로

전환 후 `requests` 라는 이름이 파일 전체에서 사라졌다(AST 확인). 임포트를 지웠다.
원시 호출을 되살리려면 **임포트를 다시 추가해야 하므로 리뷰에서 눈에 띈다.**

⚠️ 문자열 검색으로는 `requests.get` 이 1건 남은 것처럼 보인다 — 왜 전환했는지
적어둔 **독스트링**이다. `diag_fmp_ssot` 는 AST 로 읽으므로 걸리지 않는다
(2026-08-26 1차의 교훈 #6 과 같은 함정).

---

## 2. `diag_nodata_radar.py` — 락스텝 필수

F2 블록이 `_fmp_actively_trading_symbols` 를 AST 로 추출해 **가짜 `requests` 를
물려 exec** 한다. 하네스를 같이 바꾸지 않으면 네임스페이스에 `fh` 가 없어
`NameError` 가 난다.

### 그런데 그 NameError 는 조용히 통과했다 — 이번 세션 최대 교훈

옛 하네스에 새 코드를 물려 돌려봤더니:

```
✅ F2-1 네트워크 예외 → None      ← fh 가 없어 NameError → except 가 잡아 None 반환
✅ F2-2 HTTP 402 → None           ← 같은 이유
✅ F2-3 JSON 파싱 실패 → None     ← 같은 이유
✅ F2-4 응답 타입 이상 → None     ← 같은 이유
❌ F2-5 정상 → 대문자 심볼 집합
❌ F2-6 원소가 문자열이어도 견딤
```

**`None` 을 기대하는 테스트는 잘못된 이유로도 통과한다.** "실패를 올바르게
처리했다" 와 "하네스 자체가 고장났다" 를 구분하지 못한다. 판별력은 정상 경로
(F2-5·F2-6)에만 있었다.

→ 그래서 **구조 검사 `F2-0` 을 신설**했다. 추출한 함수의 AST 이름 집합에
`requests` 가 없고 `fh` 가 있는지 본다. 원시 호출로 되돌리면 여기서 걸린다.

### 추가된 검사

| | 내용 |
|---|---|
| F2-0 | 원시 `requests` 를 쓰지 않는다 (구조) |
| F2-1b | 네트워크 예외를 `kind="exception"` 으로 받아도 `None` |
| F2-2b | **레이트리밋 소진 → `None`** — 전환 전에는 없던 실패 모드 |
| F2-7 | **실물 `fmp_http.fmp_get_ex` 가 3-튜플만 반환하는지 대조** |

F2-7 이 핵심이다. 대역(mock)은 반드시 실물에서 멀어진다. 실물의 반환 원소 수가
바뀌면 이 스위트는 계속 초록불인데 실운용만 깨진다 — 그 침묵을 막는다.

---

## 3. `diag_fmp_ssot.py` — 기준선에서 제거

`_RAW_GET_BASELINE` 에서 `run_watchlist_alerts.py: 4` 항목을 삭제했다.
**기준선에 없다 = 한 곳이라도 생기면 "신규 우회" 로 실패**한다.

저장소 원시 FMP 호출 부채: **83곳 → 79곳** (11개 파일 → 10개 파일).

---

## 4. 워크플로

### `market_2pm_weekday.yml` — `timeout-minutes: 15 → 20`

여기가 유일한 실질 위험이었다. 실패한 호출마다 재시도 백오프(2+4+8초 + 지터)가
붙는다. 장중 경로는 히스토리 + quote 로 약 180콜이라 10종목이 실패하면 3분이
추가된다. **재시도를 줄이는 대신 예산을 늘렸다** — 조용히 종목을 잃는 쪽이 나쁘다.

### 양쪽에 `FMP_RATE_LIMIT_PER_MIN: '200'` 명시

기본값과 같지만 명시한다. 기본값에 의존하면 `fmp_http` 기본이 바뀔 때 이
파이프라인이 조용히 따라간다. ⚠️ 300(Starter 한도)으로 올리지 말 것.

---

## 검증 결과

| 항목 | 결과 |
|---|---|
| `diag_nodata_radar.py` | **139/139 통과** |
| `diag_fmp_ssot.py` | **38/38 통과** · 부채 79곳 |
| 실제 `import run_watchlist_alerts` | OK · `_FMP_TIMEOUT=20` · `fh` 연결 · `requests` 부재 |
| `py_compile` | 3개 파일 통과 |
| `check_py311.py` | 3개 파일 Python 3.11 호환 |
| YAML 파싱 | 2개 워크플로 통과 |

### 역검증

| 상태 | 종료 코드 | 관측 |
|---|---|---|
| 전부 패치본 | **0** | 139/139 · 38/38 |
| `run_watchlist_alerts` 만 원본 → `diag_nodata_radar` | **1** | `❌ F2-0 … — ['requests']` |
| `run_watchlist_alerts` 만 원본 → `diag_fmp_ssot` | **1** | `A1 … 4곳 — 기준선에 없는 **신규 우회**` |
| `diag_nodata_radar` 만 원본 → 새 코드 | **1** | F2-5·F2-6 실패 (위 교훈) |

부수적으로 `check()` 의 `detail` 인자에 리스트를 넘겨 `TypeError` 로 죽는 버그를
초안에서 발견해 고쳤다 — **진단이 진단을 가리는** 형태였다.

---

## 배포 순서 (락스텝)

1. `automation/run_watchlist_alerts.py` (2,040)
2. `automation/diag_nodata_radar.py` (1,046)
3. `automation/diag_fmp_ssot.py` (773)
4. `.github/workflows/market_2pm_weekday.yml` (56)
5. `.github/workflows/market_5pm_weekday.yml` (141)

Streamlit 리부트 **불필요** — `app.py` 및 앱이 import 하는 공유 모듈은 손대지 않았다.

---

## 남은 한계

- **Phase 2 미착수**: `kind` 가 nodata 원인 분류에 연결되지 않았다. 재시도까지
  소진한 실패는 여전히 🟡 "일시적 데이터 공백" 으로 표시되고 `profile` 을 한 콜
  더 쓴다. 빈도는 줄지만 경로는 그대로다
- **`app.py` 58곳은 그대로** — 대화형 + `@st.cache_data` 라 위험도가 다르다
- `run_drg_predict.py` 11곳도 그대로 (기준선 등재)
- **`limit` 무시의 페이로드 영향은 미해결.** 타임아웃만 올렸다. `from`/`to` 가
  대안인지 프로브 필요
