fix(automation): Watchlist_Metrics 프리컴퓨트 복구 + gemini_core 시그니처 정합

## 배경 — 왜 이 커밋이 필요한가

2026-08-12 시장 진입 게이트 작업에서 `run_watchlist_alerts.py` 를 **지표 프리컴퓨트
이전 버전** 위에서 편집해 배포했다. 결과로 `persist_watchlist_metrics` 와
`--scope metrics` 가 저장소에서 사라졌다.

증상:
- `Watchlist_Metrics` 시트가 더 이상 갱신되지 않음 (쓰는 주체 소멸)
- `app.py` 는 계속 읽으려 하므로 전 종목이 실시간 계산 폴백 → 워치리스트 탭 재저하
- `backfill_metrics.yml` 이 `argparse: invalid choice: 'metrics'` 로 실패

`app.py` 는 2026-08-13 에 같은 사고를 복구했으나 자동화 쪽은 누락돼 있었다.

## 파일별 변경

### automation/run_watchlist_alerts.py
- `import watchlist_metrics_core as wm` 추가 (app.py 와 동일 모듈 = SSOT)
- `_open_metrics_ws(sh)` 신규 — 탭이 없으면 헤더까지 생성. `_open_pf_state` 와 동일 패턴
- `persist_watchlist_metrics(spy_close, hist_cache, today, completed_only=False)` 신규
  - 전 사용자 워치리스트 티커의 **합집합**을 계산 (티커 단위 공용 데이터, 개인정보 없음)
  - 계산·직렬화는 전부 `watchlist_metrics_core` 에 위임 — 앱의 폴백 계산과 구조적 동일
  - 헤더 + 데이터를 `A1:L{n}` 한 번의 `update` 로 기록 (호출 2회: 읽기 1 + 쓰기 1)
  - 종목이 줄면 남는 옛 행을 빈칸으로 덮어 유령 티커 제거
  - 계산 0건이면 시트를 건드리지 않음 (일시적 FMP 장애 시 기존 값 보존)
- `--scope` 에 `metrics` 추가 — 알림 없이 백필만
- 휴장일 게이트를 `args.scope != "metrics"` 로 한정
- 정기 EOD(`watchlist`/`both`) 종료 후 지표 저장. `try/except` 로 알림 경로와 분리

### gemini_core.py
- `generate_text()` 에 `response_mime_type=None` 파라미터 추가 → `GenerateContentConfig` 전달
- 모듈 독스트링의 소비자 목록에 `run_weekly_report.py` · `scanner_core.py` 반영

`scanner_core.py:288` 이 `response_mime_type="application/json"` 을 넘기는데 파라미터가
없어 **호출 즉시 TypeError** 가 나는 상태였다. 2026-08-06 스캐너 이관 때 함께 나갔어야 할
변경이 저장소에 반영되지 않았다.

### app.py
- SSOT 검사 블록의 중복 `st.stop()` 한 줄 제거 (무해했으나 오독 소지)

### automation/diag_watchlist_metrics.py
쓰기 경로 검사(W1~W8)를 **기존 파일에 통합**했다. 별도 진단 파일을 만들지 않은 이유는
같은 파이프라인의 앞뒤(계산 → 저장)라 분리하면 어느 쪽이 뭘 보는지 헷갈리고,
이미 워크플로가 있는 파일에 붙이면 버튼 하나로 같이 돌기 때문이다.

- FMP·Sheets 를 가짜로 갈아끼우고 **실제 소스를 임포트**해 부른다(로직 복사본 아님)
- `FakeWS.update()` 는 지정 범위만 덮어쓰고 나머지 행을 남긴다 — 실제 시트와 같은
  동작. 통째로 갈아끼우게 만들면 '옛 행이 남는' 버그를 구조적으로 못 잡는다
- W1 폭·헤더·명시범위·앱리더 왕복 / W2 백필 확정봉 / W3 유령 티커 제거 /
  W4 계산 0건 시 보존 / W5 방어 / W6 `hist_cache` 재사용 / W7 사용자 간 중복 dedupe /
  W8 `main()` 배선 소스 계약(장중 미저장 포함)
- 쓰기 경로 전용 뮤테이션 4종 추가(3번 섹션): 유령 행 정리 제거 / 0건 가드 제거 /
  백필 절단 무시 / 행 폭 정규화 우회 — **전건 탐지 확인**
- W 섹션은 core 뮤테이션 때도 함께 돌아 '계산은 맞는데 저장이 틀린' 경우를 잡는다

### .github/workflows/diag_watchlist_metrics.yml
의존성에 `pytz` · `requests` 추가. 진단이 이제 `run_watchlist_alerts.py` 를 임포트하는데
`gspread`·`google-auth` 는 스텁으로 대체되지만 이 둘은 모듈 로드 시점에 실제로 필요하다.
(빠뜨리면 워크플로가 ImportError 로 죽는다)

## 설계 판단과 기각안

**전체 재작성 (채택)** vs 행 단위 upsert — 티커 단위 공용 데이터라 소유권 충돌이 없고,
재작성이 호출 2회로 끝나며 열 밀림 드리프트가 원천적으로 불가능하다. upsert 는 종목
삭제 시 유령 행이 남는다.

**백필만 `completed_only=True` (채택)** — 정기 EOD 는 당일 봉이 이미 확정이라 자르면
하루 낡은 값을 저장하게 된다. 반대로 백필은 아무 때나 돌 수 있어야 하므로 확정 봉으로
고정해야 실행 시각과 무관하게 결정적이다. `wm.last_completed_session` 기준과도 일치.

**휴장일에도 백필 허용 (채택)** — 확정 세션 기준이라 결과가 같다. 알림 경로만 막는다
(상태머신 카운터를 진행시키면 안 되므로).

**장중(intraday) 저장 금지 (채택)** — 미완성 봉으로 계산한 값이 저장되면 다음 EOD 까지
앱 전체에 퍼진다.

**알림과 지표 저장의 예외 격리 (채택)** — 한쪽 실패가 다른 쪽을 막지 않는다. 지표가
없으면 앱은 실시간 계산으로 떨어질 뿐 오답을 내지 않는다.

## 검증

- `py_compile` 전 파일 통과 / f-string 3.11 호환 검사 통과
- 호출부 시그니처 정합: 불일치 1건 → **0건** (`gemini_core.generate_text`)
- 모듈 간 심볼 참조 누락 0건 (AST 크로스레퍼런스)
- `diag_watchlist_metrics.py` 통합본: 원본 검증 전 항목 통과 +
  뮤테이션 10종(core 6 + 쓰기 경로 4) **전건 탐지**
- 기존 스위트 회귀 없음: `diag_watchlist_writepath` · `diag_market_gate`(126심볼 전수) ·
  `diag_gate_relabel` · `diag_dividend_drip` 전부 통과
- `app.py` 의 `_SSOT_NEEDS` 매니페스트를 실제 모듈에 실행 — 누락 0건

## 배포 순서 (lockstep)

```
1. gemini_core.py                                    (루트)
2. automation/run_watchlist_alerts.py
3. app.py                                            (루트)
4. automation/diag_watchlist_metrics.py              (덮어쓰기)
5. .github/workflows/diag_watchlist_metrics.yml      (덮어쓰기 — 4와 같은 커밋)
```

`watchlist_metrics_core.py` 는 이미 저장소에 있고 변경 없음. SSOT 버전 스탬프 조정은
불필요하다 — 2026-08-13 부터 버전 문자열 일치 검사 대신 기능 존재 검사를 쓴다.

4·5 는 반드시 같이 올린다 — yml 의 의존성 없이 새 진단을 올리면 워크플로가 ImportError 로 죽는다.

→ **Streamlit 재부팅**

## 배포 후 확인

1. Actions → 「🧰 지표 백필」 수동 실행 → `[OK] Watchlist_Metrics 저장: N/N종목 (확정 봉)`
2. `Quant_DB` → `Watchlist_Metrics` 시트의 `Updated_At` 갱신 확인
3. 앱 워치리스트 탭에 **「지표 기준일 …」 캡션** 복귀
4. 오늘 5PM 정기 실행 로그에 `[OK] Watchlist_Metrics 저장: … (당일 봉 포함)`

## 남은 한계 / 후속

- 이 커밋은 소실 이전 구현의 **재작성**이다. 외부 계약(시트 스키마·`wm` API·앱 리더)은
  동일하지만 내부 구현이 원본과 문자 단위로 같지는 않다.
- 지표 대상은 워치리스트 티커만이다. 포트폴리오 전용 종목은 여전히 앱이 실시간 계산한다.
- Phase 2(`regime_core` 의 `build_buy_card` hist 의존 제거) · Phase 3(장중 주기 증가)는
  기존 결정대로 범위 밖.
