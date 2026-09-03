refactor(satellite): diag_satellite_backtest 의 limit 잔재 제거 — from/to 창 전환 + 상한 경고 신설 (v2.9)

## 왜

`run_signal_backtest` 를 v2.9 로 옮기면서 `diag_satellite_backtest._fmp_eod`
하나가 남았다. 같은 버그 클래스인데 파일이 달라서 범위 밖으로 밀렸던 것이다.

FMP 는 `historical-price-eod` 의 `limit` 을 **조용히 무시**한다(§7 확정 사실).
지금까지 무해했던 이유는 우연이다 — `HISTORY_LIMIT` = 1,300 이 FMP 가 항상
돌려주는 봉수(~1,255)보다 커서 늘 전량을 받았을 뿐이다.

위험은 값이 아니라 구조에 있다. `HISTORY_LIMIT` 을 올리면 URL 은 바뀌는데
**데이터는 그대로**다. 에러도 경고도 없다. `run_signal_backtest` v2.4 에서
정확히 그 일이 일어났고, 원인을 "종목이 조건 미달로 탈락했다"로 오진해
v2.8 에서 정정하는 데 두 번을 돌았다.

### 전환만으로는 부족하다는 것을 확인했다

환산 시뮬 결과 `hist_days_for_bars(1300)` → 1,826달력일(`HIST_MAX_DAYS` 상한)
→ ~1,254봉. FMP 실제 상한과 사실상 같다. 즉 **데이터 무변화 전환**이다.

그런데 그 말은 곧, `limit` 을 `from`/`to` 로 바꾸는 것만으로는 원래 위험이
안 없어진다는 뜻이기도 하다. "숫자를 올려도 아무 일 없음"이 FMP 의 미공개
quirk 에서 `HIST_MAX_DAYS` 상한으로 **자리만 옮긴다.** `HISTORY_BARS` 를
1,500 으로 올려도 창은 여전히 1,826일이고 같은 데이터가 온다.

그래서 상한 경고를 함께 넣었다. 이 커밋의 핵심은 전환이 아니라 그 경고다.

## 파일별 변경

### `diag_satellite_backtest.py` (1,327 → 1,395줄)

- `HISTORY_LIMIT` → **`HISTORY_BARS` 개명.** 단위는 예전부터 봉수였고 이름만
  FMP 의 `limit` 파라미터를 따라갔다. 개명은 장식이 아니라 락스텝 장치다 —
  이 상수를 빌려 쓰는 파일이 옛 이름으로 남아 있으면 `AttributeError` 로
  **크게** 죽는다. 조용히 다른 값을 쓰는 것보다 낫다.
- **`_window_days_for(bars, warn=print)` 신설.** 봉수→창 환산을
  `fmp_extras` 에 위임하고, `HIST_MAX_DAYS` 상한에 걸리면 경고를 **한 번**
  찍는다. `threading.Lock` + `_WARNED_CEILING` 으로 1회 제한 — 8워커가
  동시에 찍으면 로그가 묻혀서 못 읽는다.
- `_fmp_eod(ticker, endpoint, limit=HISTORY_LIMIT)`
  → `_fmp_eod(ticker, endpoint, *, bars)`.
  URL 의 `&limit=` 을 지우고 `fx.hist_range_params(_window_days_for(bars))` 로
  `from`/`to` 를 만든다.
- `_batch_fetch(tickers)` → `(tickers, *, bars)`. **중간층까지 포함해 키워드
  전용 · 기본값 없음**(§7). 여기서는 특히 나쁘다 — 랭킹용(`full`)과 성과측정용
  (`dividend-adjusted`)이 같은 창을 받아야 하는데, 한쪽만 다른 창을 받으면
  수익률 기준이 서로 다른 기간이 되고 결과 행 모양은 똑같아서 사후에 못 골라낸다.
- `ex.submit(_fmp_eod, tk, ep, bars=bars)` — 키워드로 넘긴다.
- `main()` 이 창을 먼저 확정해 로그로 남긴다. 실제로 몇 봉을 요청했는지가
  결과 해석의 전제인데, v2.8 까지는 어디에도 안 남아 사후 확인이 불가능했다.

### `diag_fmp_ssot.py` (1,202 → 1,429줄) — 락스텝 짝

`_fmp_eod` 를 3곳에서 스텁으로 갈아끼우는 유일한 파일이라 함께 간다.

**신규 게이트 11개 (B4a~B4k)**

| 검사 | 내용 |
|---|---|
| B4a | `fmp_extras`(창 환산 SSOT)를 임포트한다 |
| B4b | `_fmp_eod` **코드**에 `limit=` 이 없다 |
| B4c | `hist_range_params` 를 **호출**한다 (언급이 아니라) |
| B4d / B4e | `(*, bars)` 키워드 전용 (두 함수) |
| B4f | `bars` 에 기본값이 없다 (§7 · 두 층 모두) |
| B4g | `HISTORY_BARS` 개명 · 옛 `HISTORY_LIMIT` 부재 |
| B4h | 환산 비율을 숫자 리터럴로 복제하지 않는다 |
| B4i / B4j | 상한에 걸리면 경고 · 미만이면 조용 |
| B4k | 같은 요구는 1회만 경고 (8워커 대비) |

**신규 양성대조 4개 (P2~P5)**

- **P2 하네스 자기검증.** 스텁이 옛 `limit=` 시그니처면 `_batch_fetch` 가
  `bars=` 로 부르는 순간 TypeError 가 나고, 워커의 `except Exception` 이
  그것을 삼켜 **전 항목이 "exception" 으로 집계된다.** 그 모습은 대상 코드가
  고장난 것과 구별되지 않는다. B5~B9 가 무더기로 빨간불일 때 "스텁을 안
  고쳤나?" 를 먼저 의심할 수 있게 하려고 명시적으로 재현해 둔다.
- **P3~P5.** B4c 는 `hist_range_params` 를 호출하는지만 본다 — 호출해놓고
  결과를 URL 에 안 붙이면 통과한다. 실제 URL 을 잡아 `limit=` 부재 ·
  `from=`/`to=` 존재 · **bars 를 줄이면 from 도 따라 움직이는지**(창이
  고정 문자열이 아닌지)를 확인한다.

**보조 변경**

- `_body_mentions` · `_is_kwonly` · `_has_default` 신설
  (`diag_universe_funnel` S6/S8 기법 이식).
- `_NEED` 에 `fx` · `HISTORY_BARS` · `_window_days_for` · `_WARNED_CEILING`
  추가. 중단 배너 문구 v2.8 → v2.9.
- `import fmp_extras as fx` 직접 추가. `sb.fx` 로 우회하면 옛 사본일 때
  그쪽이 `AttributeError` 를 내면서 `_NEED` 의 친절한 중단 메시지를 삼킨다.
- 스텁 3종 `(tk, ep, limit=None)` → `(tk, ep, *, bars=None)`,
  `_batch_fetch` 호출부 4곳에 `bars=` 명시.

## 검증

| 항목 | 결과 |
|---|---|
| `py_compile` | 양쪽 통과 |
| `pyflakes` | 0건 (베이스라인도 0 — 델타 0) |
| `diag_fmp_ssot.py` | **60/60 전부 통과** |
| 뮤테이션 | **12/12 잡음 · 놓침 0 · 무효 0** |
| `--selftest` (엔진) | 전 항목 통과 |
| `check_py311.py` | 2개 파일 호환 |

### 뮤테이션이 찾아낸 진짜 구멍 2개

초록불만 봤으면 못 봤을 것들이다. 뮤테이션이 없었으면 둘 다 남았다.

**① `_has_kwdefault` 가 위치 인자 기본값에 눈이 멀었다.**
`*, bars` → `bars=1300` 으로 되돌리면 인자가 `kwonlyargs` 에서 사라져
"기본값 없음" 으로 통과했다. 마침 B4d 가 잡았지만 그러면 방어가 한 겹뿐이다.
`_has_default` 로 일반화 — 두 검사가 독립적이어야 한쪽을 완화해도 다른 쪽이
남는다.

**② P5 가 크래시했다.** `url.split("from=")[1]` 이 `from=` 부재 시
IndexError → 진단이 **요약 줄도 못 찍고 죽었다.** 몇 건이 실패했는지조차 알
수 없는 상태. 검사는 빨간불이 되어야지 크래시하면 안 된다 — 크래시는
빨간불보다 나쁘다. `_from_of()` 로 안전 추출.

### 초안에서 밟은 함정 하나

B4h 를 `"0.6871" in SB_SRC` 로 짰다가 **같은 커밋에서 방금 쓴 독스트링**
("여기서 0.6871 을 복제하지 않는다")에 걸려 즉시 오탐. 같은 파일
`_body_mentions` 주석에 "전역 문자열 검색 하지 말라" 고 적어놓고 그대로
밟았다. AST 숫자 리터럴 검사로 교체했다. 규칙을 적어둔 주석이 규칙 위반으로
집계되는 것은 이 계열 검사의 고질적 실패다.

## 락스텝 배포 순서

1. `automation/diag_satellite_backtest.py` (1,395줄 · GitHub 표시 1,394)
2. `automation/diag_fmp_ssot.py` (1,429줄 · GitHub 표시 1,428)

반대로 올리면 새 진단이 옛 대상을 보고 `❌ 중단 — v2.9 이전 버전이다` 로
멈춘다. 안전한 방향이지만 한 번 헛돈다.

`app.py` 는 두 파일 중 어느 것도 임포트하지 않는다 → **Streamlit 재부팅 불필요.**

## 남은 한계

- **`SEG_MAX = 6` 은 도달 불가능한 값이다.** 6×252 + 127(warmup) = 1,639봉이
  필요한데 확보 가능한 최대가 ~1,254봉이라 실제로는 최대 4구간이다. 전환
  전에도 그랬고 후에도 그렇다. 이번 범위 밖.
- `diag_fmp_ssot.yml` 주석이 낡았다 — "44항목" → 60, "v2.8 이전 버전이다"
  → v2.9. 주석 전용이라 동작에는 영향 없다.
- `_gs` / `_gs_is_transient` 가 `gs_retry.py` 와 중복 (이 파일 + 
  `run_signal_backtest.py`, 각 11곳·16곳 호출). 단순 중복이 아니다 —
  재시도 예산이 2분 vs 22초이고, 상태코드를 못 읽는 예외의 기본값이
  `False`(재시도 안 함) vs `True`(재시도함)로 **정반대**다. 기계적 치환하면
  조용히 동작이 바뀐다. 별도 작업으로 남긴다.
