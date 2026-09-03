refactor(backtest): run_signal_backtest 의 limit 잔재 제거 — from/to 창 전환 + 관문 신설 (v2.9)

## 왜

`run_signal_backtest._fmp_price_history` 만 아직 `&limit=` 을 보내고 있었다.
FMP 는 `historical-price-eod` 의 `limit` 을 **조용히 무시**한다(§7 확정 사실).
지금까지 무해했던 이유는 우연이다 — `HISTORY_LIMIT` = 1,255 가 FMP 가 항상
돌려주는 봉수와 같았을 뿐이다.

위험은 값이 아니라 구조에 있었다. `TEST_LOOKBACK` 을 올리면 `HISTORY_LIMIT` 이
따라 커지고 URL 도 바뀌지만 **데이터는 그대로**다. 에러도 경고도 없다.
v2.4 에서 1260→2140 으로 올렸을 때 실제로 그렇게 됐고, 원인을 "종목이 조건
미달로 탈락했다"로 오진해 v2.8 에서 다시 정정하는 데 두 번을 돌았다.

여기에 더해 이 파일은 **창 정책을 지키는 관문이 하나도 없었다.**
`diag_hist_window`[S] 는 `run_watchlist_alerts` 전용, `diag_hist_window_consumers`[H]
는 `rotation_core` 전용이라 `run_signal_backtest` 는 어느 래칫에도 없었다.
고쳐도 되돌아올 수 있는 상태였다.

## 파일별 변경

### `run_signal_backtest.py` (1,553 → 1,597줄)

- `import fmp_extras as fx` 추가. 봉수→달력일 환산 정책은 `fmp_extras` 가 유일
  소유자다. 여기서 `0.6871` 이나 창 상수를 복제하지 않는다.
- `_fmp_price_history(ticker, limit=HISTORY_LIMIT)`
  → `_fmp_price_history(ticker, *, bars)`.
  URL 은 `fx.hist_range_params(fx.hist_days_for_bars(bars))` 로 `from`/`to` 를 만든다.
- `_batch_fetch_history(tickers, limit=…)` → `(tickers, *, bars)`. 중간층까지
  포함해 **기본값을 없앴다**(§7). 중간층 기본값은 상위 호출부가 요구를 빠뜨려도
  그럴듯한 값으로 메워줘서, 요구가 바뀔 때 한쪽만 갱신되는 사고를 만든다.
- `ex.submit(_fmp_price_history, tk, bars=bars)` — 위치 인자 → 키워드.
- 호출부 2곳(`main`)이 `bars=HISTORY_BARS` 를 **명시**한다. 같은 값을 두 번 쓰는
  중복처럼 보이지만, SPY(벤치마크)와 유니버스가 서로 다른 창을 받으면 초과수익
  계산이 조용히 다른 기간 대비가 된다 — 기본값에 맡기면 그 갈림이 안 보인다.
- `HISTORY_LIMIT` → **`HISTORY_BARS` 개명.** 단위는 예전부터 봉수였고 이름만
  틀렸다. 개명은 장식이 아니라 락스텝 장치다 — 이 상수를 빌려 쓰는 진단 파일이
  옛 이름으로 남아 있으면 `AttributeError` 로 **크게** 죽는다.

### `diag_universe_funnel.py` (599 → 716줄) — 관문 신설

`_fmp_price_history` 를 AST 로 들여다보는 유일한 파일이라, 새 파일을 만들지 않고
여기에 붙였다. 1절에 6개 추가:

| 검사 | 내용 |
|---|---|
| S5 | `fmp_extras`(창 환산 SSOT)를 임포트한다 |
| S6 | `_fmp_price_history` **코드**에 `limit=` 이 없다 |
| S7 | `hist_range_params` 로 `from`/`to` 를 만든다 |
| S8 / S8b | `(*, bars)` 키워드 전용 (두 함수) |
| S9 | `bars` 인자에 기본값이 없다 (§7) |
| S10 | `HISTORY_BARS` 로 개명 · 옛 `HISTORY_LIMIT` 부재 |

- S6 은 **함수 본문 AST** 로 범위를 좁히고 `ast.unparse` 로 주석을 버린 뒤
  독스트링도 뗀다. 파일 전역 문자열 검색으로 하면 전환 이력을 설명하는 주석의
  "limit" 때문에 영구 빨간불이 된다 — `diag_regime_window.price_window_ok` 가
  같은 함정을 밟고 고친 이력이 있어 그 기법을 그대로 가져왔다.
- 스텁 3종(`_stub_hist`·`_boom_hist`·`_old_style`)을 `(tk, *, bars=None)` 으로 갱신.
- **P3 하네스 자기검증 신설.** 스텁이 옛 시그니처면 `_batch_fetch_history` 의
  `except Exception` 이 TypeError 를 삼켜 전부 `"exception"` 으로 집계된다.
  B1/B2 는 틀린 숫자로 실패하지만 **B6 는 오히려 통과한다** — 의도한
  RuntimeError 가 아니라 시그니처 불일치로 통과하는 가짜 초록불이다.
  뮤테이션 N9 로 실측 확인한 뒤 추가했다.
- `_NEED` 선행조건에 `fx`·`HISTORY_BARS` 추가 (부분 롤백 시 스택 트레이스 대신
  "v2.9 이전 버전이다" 를 말하게 한다).

### `diag_fmp_depth.py` (150 → 160줄) — `probe_limits` 폐기

`probe_limits` 는 "limit 이 먹히는가" 를 재는 프로브였다. 그 답은 §7 에
'재조사 금지'로 확정돼 있고, `limit` 송신을 중단한 이상 **흔들 손잡이가 없다.**
달력일 창을 흔드는 프로브는 `diag_fmp_window.py` 가 이미 갖고 있어 되살리지 않는다.
`probe_universe`/`recommend` 는 `bars=` 로 이식해 남겼다 — "유니버스가 실제로
몇 봉을 확보하는가" 는 신규 상장·이력 짧은 종목 때문에 여전히 살아 있는 질문이다.
`main()` 에 `HISTORY_BARS`·`fx` 존재 가드를 추가했다(부분 롤백 시 원인 출력).

### `diag_trade_history.py` (288 → 291줄)

`bt._fmp_price_history(SPY, limit=bt.HISTORY_LIMIT)` → `bars=bt.HISTORY_BARS`.

## 검증

- `py_compile` 4/4 통과. `pyflakes` 델타 0 (기존 f-string 경고 2건만 유지).
- `diag_universe_funnel` **68 → 76건 전부 통과** (신규 7 + P3).
- `diag_fmp_ssot` **45건 전부 통과** (변경 전후 동일 — 회귀 없음, 수정 불필요).
- **뮤테이션 9/9 적발** (전 변이 `py_compile` 선검증 통과 = 문법 오류로 인한
  가짜 적발 아님):
  - N1 `limit=` 되살림 → S6·S7
  - N2 날짜 하드코딩 → S7
  - N3 `bars` 위치 인자화 → S8
  - N4 `bars` 기본값 부여 → S9
  - N5 `HISTORY_LIMIT` 이름 복귀 → S10
  - N6 `fmp_extras` 임포트 제거 → S5
  - N7 중간층 위치 인자화 → S8b
  - N8 스텁만 옛 시그니처 → B1/B2/B4
  - N9 `_boom_hist` 만 옛 시그니처 → **P3** (P3 추가 전에는 미적발이었다)
- 역검증: `_fmp_price_history("SPY", 600)`·인자 누락·배치 인자 누락 3종 모두
  `TypeError` 로 즉시 사망 확인.
- 실제 URL 확인:
  `…/historical-price-eod/full?symbol=SPY&from=2021-09-03&to=2026-09-04&apikey=…`
  (`limit=` 없음, `from`/`to` 있음)

## 창 숫자

```
HISTORY_BARS            = 1255 봉 요구
hist_days_for_bars(…)   = 1826 달력일  ← HIST_MAX_DAYS 에서 클램프
무마진 최소 달력일       = 1827         ← 상한을 이미 1일 초과
기대 봉수                ≈ 1254
실제 구속 조건           = MIN_PRIOR_BARS + TEST_LOOKBACK = 1154 봉
여유                     = 100 봉
```

`pad_bars=5` 여유는 상한에 먹힌다. 그래도 구속 조건 1,154봉 대비 100봉이
남으므로 평가 구간 934일은 온전하다. 체감 변화는 1,255봉 → 1,254봉,
평가 시작일이 하루 밀리는 정도다 — 어차피 롤링 5년 창이라 실행일마다
밀리던 값이다.

## 락스텝 배포 순서

1. `run_signal_backtest.py` (automation/)
2. `diag_universe_funnel.py` (automation/)
3. `diag_fmp_depth.py` (automation/)
4. `diag_trade_history.py` (automation/)

`app.py` 무관 · **Streamlit 재부팅 불필요**.

## 남은 한계

- `diag_satellite_backtest.py` 는 자체 `HISTORY_LIMIT = 1300` + `_fmp_eod(…, limit=…)`
  사본을 갖고 있다. 같은 무동작이지만 진단 파일이고 별도 락스텝 쌍이라 **이번
  패스에서 제외**했다. 별건으로 남긴다.
- `run_signal_backtest.py` 의 `_gs_is_transient`/`_gs` 가 `gs_retry.py` 와 중복일
  가능성. 이번 변경과 무관하나 확인 필요.
