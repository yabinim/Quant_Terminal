feat(diag): 미사용 FMP 엔드포인트 실측 프로브 추가 — 신규 기능 후보 검증

## 왜 지금인가

`api-docs` 재대조 결과, 공식 문서의 고유 경로 **242개 중 코드가 쓰는 것은 48개**다.
2026-08-15 감사는 "이미 박힌 경로가 죽었나"를 봤고, 죽은 경로 6개를 정리했다.
이번은 반대 방향 — **안 쓰고 있는 194개 중 쓸 만한 것**을 찾는다.

다만 그 감사에서 확인된 대로 이 플랜에는 402 계층이 분명히 존재한다.

    etf/holdings                     402
    analyst-estimates?period=quarter 402
    news/press-releases*  (3경로 전부) 402
    earning-call-transcript-dates    402

**"문서에 있다" ≠ "이 플랜에서 쓸 수 있다"** 가 이미 네 번 확인됐으므로,
설계 문서를 쓰기 전에 실측이 먼저다 (개발원칙 6).

## 변경 내역

### `automation/diag_fmp_newcaps.py` (신규, 602줄)

미사용 엔드포인트 21개를 4개 티어로 나눠 실측한다.

**`diag_fmp_endpoints.py` 와 판정 로직이 다르다.** 그쪽은 "200 이면 살아있음"으로
충분했다(기존 경로의 생사 확인이 목적이므로). 이쪽은 **필드까지 본다.**

| 판정 | 조건 | 의미 |
|---|---|---|
| ✅ `LIVE` | 200 + 데이터 + **필요 필드 전부 존재** | 설계 진행 가능 |
| 🟠 `FIELDS` | 200 + 데이터 + **필요 필드 누락 또는 요청 범위 밖** | 설계 전제 재검토 |
| ⚠️ `EMPTY` | 200 + 빈 배열 | 날짜/파라미터 문제일 수 있음 — 재실행 |
| 🔒 `PLAN` | 402 | 코드로 해결 안 됨 — 후보 제외 |
| ❌ `404`/`ERRMSG` | 경로 실패 | 문서에서 경로 재확인 |

`FIELDS` 버킷을 새로 만든 이유: 200 을 받고 넘어갔다가 구현 도중에 필요한 키가
없어서 되돌아오는 것을 막기 위함이다. 각 대상마다 설계가 실제로 의존하는 키를
`need` 로 명시했다.

**날짜를 하드코딩하지 않는다.** 실행 시점에서 만든다 — 안 그러면 프로브 자체가
내년에 낡는다. 스냅샷 계열은 `_recent_weekday(3)` 으로 최근 평일을 쓴다
(휴장일까지는 피하지 못하며, 그 경우 `EMPTY` 로 나오고 이는 경로 실패와
별도 버킷으로 분리된다).

#### 티어별 대상

**tierA (5콜) — 실제 결함 해결**

| 엔드포인트 | 대응 결함 |
|---|---|
| `holidays-by-exchange` | 🔴 `_NYSE_HOLIDAYS` 가 **2026-12-25 에서 끝난다.** `run_watchlist_alerts:165` / `run_drg_predict:58` / `run_drg_verify:54` 3중 하드코딩. 2027-01-01 부터 모든 휴장일을 거래일로 오판 |
| `symbol-change` | 🔴 티커 변경 → `historical-price-eod` 빈 배열 → `run_watchlist_alerts:823` 조용히 skip → 영구 침묵 |
| `delisted-companies` | 🔴 상장폐지 보유 종목의 **매도 신호가 아예 안 온다** — 손실방지 직격 |
| `actively-trading-list` | 위 둘이 막혔을 때의 대안 멤버십 판정 |
| `etf/asset-exposure` | 🟡 `app.py:6450 find_etfs_holding_stock` 스텁의 정답 후보. `weightPercentage` 포함 시 보류 중인 가중치 표시 설계도 함께 해결 |

`holidays-by-exchange` 만 `contains` 검증을 추가로 건다 — `from/to` 를 **내년**으로
넣고 응답 본문에 내년 연도가 실제로 있는지 본다. "200 이 왔다"가 아니라
"내년 데이터가 왔다"가 답해야 할 질문이기 때문이다.

**tierB (9콜) — 기존 기능 강화**

`sector-performance-snapshot` · `historical-sector-performance` ·
`industry-performance-snapshot` · `historical-industry-performance` ·
`price-target-summary` · `stock-peers` · `sec-filings-8k` ·
`dividends-calendar` · `mergers-acquisitions-latest`

**tierC (4콜) — 탐색적**

`senate-latest` · `house-latest` ·
`institutional-ownership/holder-performance-summary` · `historical-industry-pe`

**grades (3콜) — 미결 과제 정리**

`grades` / `grades-consensus` / `ratings-snapshot` 의 응답 키를 **나란히 출력**한다.
"`grades?symbol=` 가 기존 등급 엔드포인트와 겹치는가"는 지난 감사의 미검토 잔여
건이고, 응답 키 직접 비교가 가장 빠른 답이다.

### `.github/workflows/diag_fmp_newcaps.yml` (신규, 97줄)

`workflow_dispatch` 전용. **`schedule` 도 `repository_dispatch` 도 없다** —
일회성 프로브가 매일 콜을 태우면 안 된다.

`tier` 선택지: `tierA`(기본) / `tierB` / `tierC` / `grades` / `all`.
스크립트가 `.lower()` 로 정규화하므로 대소문자 표기 차이는 무해하다(검증 완료).

## 검증

**컴파일**

    py_compile              OK
    check_py311.py          ✅ Python 3.11 호환 (f-string 위반 0건)
    YAML 파싱               OK — jobs 1 / steps 5 / trigger workflow_dispatch

**판정 로직 뮤테이션 테스트 (오프라인, 네트워크 없이 `requests.get` 대체)**

13개 응답 시나리오를 주입해 판정 분기를 전수 확인 — **13/13 통과**

    402 → PLAN          404 → 404           429 → RATE          401 → AUTH
    500 → HTTP          200+비JSON → NOJSON  200+[] → EMPTY
    200+ErrorMessage → ERRMSG
    200+필드완비 → LIVE          200+필드누락 → FIELDS
    200+contains없음 → FIELDS     200+contains있음 → LIVE
    200+dict정상 → LIVE

특히 검증하고 싶었던 두 가지:
- **필드 누락이 LIVE 로 새지 않는가** → `FIELDS` 로 정확히 분리됨
- **`contains` 검증이 필드 검증보다 먼저 걸리는가** → 필드가 완비돼도 요청 범위
  밖이면 `FIELDS` 로 떨어짐 (휴장일 프로브의 핵심 동작)

**키 유출 방어** — `FMP_API_KEY` 미설정 시 exit 2 로 즉시 중단 확인.
예외 메시지·비JSON 본문·에러메시지 출력 전부 `_mask()` 를 통과한다.

## 락스텝 배포 순서

**없다.** 두 파일 모두 신규이고, 기존 모듈을 하나도 import 하지 않는다
(`requests` 만 사용). 어떤 순서로 올려도 무방하며 Streamlit 재부팅도 불필요하다.

    1. automation/diag_fmp_newcaps.py           (602줄)
    2. .github/workflows/diag_fmp_newcaps.yml   (97줄)

⚠️ `automation/` 하위에 올릴 것. 루트 중복본이 생기면 Actions 가 낡은 코드를 돈다.

## 남은 한계

- **프로브는 답이 아니라 질문지다.** 결과가 나와야 A-1/A-2 설계에 들어간다.
  현 시점에서 `etf/asset-exposure` 가 살아 있을지는 알 수 없다
  (`etf/holdings` 가 402 였으므로 같은 계열일 가능성이 있다).
- `actively-trading-list` 는 파라미터가 없어 **전체 목록**이 온다. 응답이 매우 클
  수 있어 프로브가 건수를 함께 출력한다. 실사용 시 캐싱 설계가 별도로 필요하다.
- 스냅샷 계열(`sector-performance-snapshot`, `industry-performance-snapshot`)은
  조회일이 휴장일이면 `EMPTY` 로 나온다. **경로 실패로 오독하지 말 것** —
  날짜를 바꿔 한 번 더 돌리면 된다.
- `historical-*-performance` 의 `sector` / `industry` 파라미터는 FMP 가 정한
  분류명을 정확히 써야 한다(`Technology`, `Semiconductors` 로 프로브). 실제 구현
  시에는 `available-sectors` / `available-industries` 로 목록을 받아야 한다
  — 이 둘도 미사용 경로이므로 필요해지면 프로브에 추가한다.
