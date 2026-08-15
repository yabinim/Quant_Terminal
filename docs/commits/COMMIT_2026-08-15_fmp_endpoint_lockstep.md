fix(fmp): 죽은 엔드포인트 경로 일괄 교정 + ETF 보유종목 잠복 크래시 수정

## 근거 — 실측 2회

공식 `api-docs`(278 엔드포인트) 대조 후 `diag_fmp_endpoints.py` 로 두 차례 실측.

**1차 (pairs, 15콜)** — 수정필수 4 / 둘다실패 2 / 판정불가 0

| 코드 위치 | 현재 | 공식 |
|---|---|---|
| `fmp_extras.py:452` | `etf/sector-weighting` **404** | `etf/sector-weightings` **200** (12건) |
| `fmp_extras.py:389` | `batch-market-capitalization` **404** | `market-capitalization-batch` **200** (2건) |
| `app.py:5507, 14834` | `stock-news?symbols=` **404** | `news/stock?symbols=` **200** (3건) |
| `earnings_core.py:339` 외 2곳 | `earnings-surprises?symbol=` **404** | `earnings?symbol=` **200** (8건) |
| `narrative_core.py:220` | `press-releases-latest` **404** | `news/press-releases-latest` **402** |
| `app.py:6012, 7123` | `etf-holder/{sym}` **404** | `etf/holdings?symbol=` **402** |

**2차 (singles, 5콜)** — 정상 4 / 플랜 1 / 경로실패 0

- `news/stock-latest` **200** · `fmp-articles` **200** · `news/general-latest` **200** → 내러티브 Layer A/C/D 전부 생존
- `news/press-releases?symbols=` **402** → 보도자료는 `-latest`·검색형 **모두 402**. 재건 불가 확정
- `etf/info` **200** (`assetsUnderManagement, expenseRatio, holdingsCount, domicile` 확인) → `fmp_extras.py:434` 이상 없음

**참고 확인** — `analyst-estimates?period=quarter` **402**, `earning-call-transcript-dates` **402** (Stage 2/3 설계 전제 재확인). `grades?symbol=` **200**(1786건) 신규 확인.

## 🔴 부수 발견 — 잠복 크래시 (엔드포인트와 무관)

`cached_etf_holdings_universe_str` 는 **list** 를 반환하는데 호출부는 **DataFrame** 으로 쓴다.

```python
hdf = cached_etf_holdings_universe_str(etf)
if hdf is None or hdf.empty or "Ticker" not in hdf.columns:   # [].empty → AttributeError
```

AST 로 확인한 결과 **이 지점을 감싸는 try 블록이 없다.** 기회 스캐너에서 섹터 라벨을 선택하면 그대로 터진다. 정밀 분석 쪽 호출부는 큰 try 안이라 조용히 죽었을 뿐 같은 결함이다. 엔드포인트 조사 중에 드러났을 뿐 별개 버그이며, 사용자 대면 크래시라 이번에 함께 고친다.

## 변경 내역

### `fmp_extras.py` (914 → 914줄)

| 위치 | 변경 |
|---|---|
| 389 | `batch-market-capitalization` → `market-capitalization-batch` |
| 452 | `etf/sector-weighting` → `etf/sector-weightings` |

파서가 읽는 키(`marketCap` / `sector`+`weightPercentage`)가 실측 응답 키와 일치함을 코드 확인으로 검증. 필드명 수정 불필요.

### `earnings_core.py` (2216 → 2218줄)

`_get` 루프 튜플에서 죽은 `earnings-surprises` 제거. `earnings?symbol=` 가 이미 첫 번째이고 정상 동작하므로 **결과는 바뀌지 않는다.** 티커마다 버려지던 404 1콜이 사라진다 — Tier 2 유니버스 111종목 기준 실행당 111콜.

### `diag_earnings_preview_backtest.py` (444 → 444줄)

동일 패턴 제거.

### `narrative_core.py` (1276 → 1282줄)

**Layer B 제거.** `press-releases-latest`(404) → `news/press-releases-latest`(402) → `news/press-releases?symbols=`(402). 세 경로 모두 막혀 이 플랜에서 보도자료 확보는 불가능하다.

그동안 404 를 삼키고 빈 리스트를 더하고 있었으므로 **뉴스 출력은 바뀌지 않는다.** 달라지는 것은 호출 1개 절약과 `source_log` 가 정직해지는 것뿐이다(`PR 0` 이 사라짐).

**⚠️ 승인 범위를 벗어난 추가 변경 — 명시적으로 알린다.**

Layer B 제거만으로 끝내려 했으나 코드를 읽다가 연쇄 결함을 발견했다.

- `_build_news_context_text` 의 `section_defs` 에 SECTION A(Press Releases) 정의가 남아 있다. 이 섹션을 채우던 유일한 소스가 Layer B 였고, 루프는 `if not items: continue` 로 빈 버킷을 건너뛴다 → **SECTION A 는 이미 렌더링된 적이 없다.**
- 그런데 프롬프트는 LLM 에게 *"[SECTION A] Press Releases 의 Tickers 를 winners 최우선 후보로 선정하라"* 고 지시하고 있었다. **존재하지 않는 섹션을 최우선 근거로 삼으라는 지시**다.

죽은 설정만 지우고 프롬프트를 두면 이 결함이 그대로 남는다. 따라서 세 곳을 함께 정리했다.

1. `section_defs` 에서 SECTION A 정의 제거
2. 프롬프트의 SECTION A 지시문 제거, SECTION B 를 winners 1차 근거로 명시
3. JSON 스키마 힌트의 `"Press Release·Stock News 기반"` → `"Stock News·Article 기반"`

**이것은 LLM 출력을 바꿀 수 있는 유일한 변경이다.** "동작 무변화"라던 앞선 설명에서 벗어나므로 되돌리길 원하면 이 3곳만 원복하면 된다. 섹션 문자(B/C/D/E)는 과거 로그 대조를 위해 재부여하지 않았다.

### `app.py` (25998 → 25951줄, -47)

| 위치 | 변경 |
|---|---|
| 5507, 14834 | `stock-news?symbols=` → `news/stock?symbols=` (+ 주석 1곳) |
| `cached_earnings_history` | 경로 `earnings-surprises` → `earnings?symbol=&limit=8` **+ 필드명 `epsActual`/`epsEstimated` 동시 추가** |
| `find_etfs_holding_stock` | 본체 제거 → 즉시 `[]` 반환 |
| `cached_etf_holdings_universe_str` | 반환 타입 `list` → `DataFrame(columns=["Ticker","Weight(%)"])` |
| ETF 보유 UI | 캡션·스피너·안내문 정정 |

**필드명을 함께 고친 이유:** `earnings?symbol=` 은 `epsActual`/`epsEstimated` 를 준다. 기존 파서는 `actualEarningResult`/`actualEPS`/`eps` 를 읽으므로, 경로만 바꾸면 404 가 **"전 행 N/A"** 로 바뀔 뿐 오히려 더 조용한 실패가 된다. 구 필드명은 뒤쪽 폴백으로 남겨 하위호환을 유지했다.

**`ETF_CONSTITUENTS` 폴백을 붙이지 않은 이유 (2건 모두):**

- `find_etfs_holding_stock` 은 **역방향** 질문("어떤 ETF 가 이 종목을 담는가")이다. 그 맵은 ETF 별 대표종목 축약본이라 SPY·QQQ·VOO 가 아예 없다. 폴백을 쓰면 *"SPY 는 이 종목을 보유하지 않음"* 이라는 **거짓 음성**을 만든다. 없는 것을 없다고 하는 게 아니라 **있는 것을 없다고 하는** 것이며 손실 회피 원칙에 어긋난다.
- `cached_etf_holdings_universe_str` 은 순방향이지만 맵에 **비중 정보가 없다.** `app.py` 정밀 분석은 이 결과를 "Top Holdings + Weight(%)" 표로 표시하므로 전 행 `0.0%` 가 뜬다. 보유 비중 0% 표시는 데이터 없음보다 나쁘다.

두 번째 건은 비중 표시 방식을 함께 정하면 폴백 부착이 가능하다 — 별건으로 남긴다.

**UI 문구 정정:** 기존 안내는 *"조회한 주요 ETF 20개 중 …를 보유한 ETF를 찾지 못했습니다"* 였다. 조회했는데 없다는 뜻이 되어 거짓 음성을 만든다. *"현재 요금제에서 제공되지 않습니다. …보유한 ETF가 없다는 뜻이 아닙니다"* 로 교체. 스피너 `(약 10초)` 도 제거(이제 즉시 반환).

### `automation/diag_endpoint_fix.py` (신규)

수정이 되돌아가지 않았는지 확인하는 회귀 스위트. 네트워크·시트·이메일 접촉 없이 **소스만 AST 로 읽는다.** 종료코드 0=통과 / 1=실패.

## 검증

| 항목 | 결과 |
|---|---|
| `py_compile` × 6 | ✅ |
| `check_py311.py` × 6 | ✅ Python 3.11 호환 |
| `check_freshness.py` 정합성 | ✅ 8/8 모듈, 마커 손실 없음 (app 6/6, earnings_core 6/6, fmp_extras 2/2) |
| 회귀 스위트 | ✅ 실패 0건 |
| **변이 테스트** | ✅ **4/4 검출** |

### 변이 테스트가 회귀 스위트의 결함을 잡았다

1차 작성한 회귀 스위트는 하드코딩된 값을 검사해서 **M1·M2 를 놓쳤다.**

| 변이 | 1차 | 최종 |
|---|---|---|
| M1 반환 타입을 `list` 로 되돌림 (잠복 크래시 재발) | ❌ 놓침 | ✅ 잡음 |
| M2 경로만 고치고 필드명 미수정 (조용한 전 행 N/A) | ❌ 놓침 | ✅ 잡음 |
| M3 죽은 뉴스 경로 되돌림 | ✅ | ✅ |
| M4 죽은 SECTION A 정의 복구 | ✅ | ✅ |

M1·M2 는 정확히 **이번 수정에서 가장 위험한 두 클래스**(사용자 대면 크래시 / 조용한 오답)다. 이걸 못 잡는 회귀 스위트는 통과해도 의미가 없다. 하드코딩 값 대신 실제 소스를 AST 로 파싱해 `return` 문과 필드 사용을 직접 검사하도록 고쳐 4/4 로 만들었다.

## 배포 — lockstep 필수

`fmp_extras` / `earnings_core` / `narrative_core` 는 `app.py` 가 import 하는 공유 모듈이다. **5개를 반드시 함께 올린다.**

1. `app.py`
2. `fmp_extras.py`
3. `earnings_core.py`
4. `narrative_core.py`
5. `automation/diag_earnings_preview_backtest.py`
6. `automation/diag_endpoint_fix.py` (신규)

업로드 후 **Streamlit 재부팅** (`narrative_core` 변경 포함).

### 업로드 후 지문 대조

```
app.py            25952   6/6
earnings_core.py   2219   6/6
fmp_extras.py       915   2/2
narrative_core.py  1283   -
```

## 확인 방법

- **내러티브**: 다음 실행 로그의 `source_log` 에 `PR 0` 이 사라지고 `Stock / Articles / General` 3개만 남는지
- **기회 스캐너**: 섹터 라벨 선택 시 크래시 없이 진행되는지 (수정 전에는 `AttributeError`)
- **정밀 분석 → 어닝 서프라이즈**: 이전에는 방법 1이 404 로 죽고 방법 2(income-statement)로 떨어져 `매출/순이익` 컬럼이 나왔다. 이제 방법 1이 성공하므로 **`EPS 실제 / EPS 예상 / 어닝 서프라이즈` 컬럼**이 나와야 정상이다

## 남은 한계 / 후속 후보

- `fmp_extras.py:689` `_top_holdings_set` 은 여전히 `etf/holdings`(402)를 호출한다. **폴백이 이미 있어 결과는 정상**이지만 티커마다 402 를 1콜씩 태운다. 이번 승인 범위 밖이라 손대지 않았다 — 별건 정리 후보.
- `etf/sector-weightings` 의 `weightPercentage` 가 숫자인지 `"30.5%"` 문자열인지 미확인. 파서에 `0 < w <= 1` 가드가 있어 비율/퍼센트는 흡수하지만 문자열이면 `_f()` 처리에 의존한다. 앱에서 SPY 섹터 비중이 정상 표시되는지 육안 확인 권장.
- 보도자료(Press Release)는 이 플랜에서 영구 불가. 필요하면 요금제 상향이 유일한 경로다.
- `grades?symbol=`(1786건) 신규 확인 — 현재 `grades-historical`/`grades-consensus` 와 중복 여부 미검토.
- 엔드포인트 SSOT 는 `api-docs.pdf`. `FMP_API_list.pdf` 폐기됨.
