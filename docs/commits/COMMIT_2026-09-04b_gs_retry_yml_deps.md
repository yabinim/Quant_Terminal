fix(ci): diag_gs_retry.yml 의존성 누락 — 락스텝 짝 diag_fmp_ssot 이 B군에서 죽던 문제

## 증상

`🔁 진단 — Sheets 재시도 SSOT 통합 가드` 첫 실행(2026-09-04 07:29 UTC)에서
두 번째 스텝이 exit 1 로 끝났다.

    File ".../automation/diag_fmp_ssot.py", line 636, in <module>
        import pandas as pd   # noqa: E402
    ModuleNotFoundError: No module named 'pandas'

## 원인

새 워크플로의 의존성 스텝에 `gspread` 만 넣었다. 그런데 이 워크플로는
락스텝 짝으로 `diag_fmp_ssot.py` 도 돌리고, 그쪽은 B군에서
`diag_satellite_backtest` 를 **실제로 import** 한다. 그 모듈이
numpy·pandas·pytz 를 쓴다.

기존 `diag_fmp_ssot.yml` 은 처음부터
`numpy pandas pytz requests gspread google-auth` 를 전부 설치하고 있었다.
새 워크플로를 만들며 그 목록을 따라가지 않고 diag_gs_retry 자신의
필요분(gspread)만 보고 줄인 것이 원인이다.

## 검사 결과 자체는 정상이었다

죽은 지점은 A군 전부를 통과한 뒤다. 로그에 그대로 남아 있다:

    ✅ A1  기준선을 넘는 원시 FMP 호출이 없다
        (현재 부채 총 71곳 / 10개 파일 — 0 이 목표)
    ✅ A2  튜플 반환 함수를 단일 값으로 받는 곳이 없다 (286개 추적)
    ✅ A4a / A4b  integrated_sell_verdict 인자 계약

`diag_gs_retry.py` 는 그 앞에서 **53/53 전부 통과**했다.
별도 실행한 `🛡️ 진단 — FMP SSOT` 워크플로(의존성 완비)도 전부 통과했다.
즉 코드 결함이 아니라 워크플로 설정 결함이다.

## 수정

`.github/workflows/diag_gs_retry.yml` 의존성 한 줄:

    - pip install gspread
    + pip install numpy pandas pytz requests gspread google-auth

왜 이 셋이 필요한지를 주석으로 남겼다. `diag_fmp_ssot.yml` 과 **같은 목록을
유지한다**는 것이 규칙이다 — 한쪽만 줄이면 같은 사고가 반복된다.

## 부수적으로 확인된 것

신선도 지문 스텝에 붙여 둔 `if: always()` 가 실제로 작동했다. 앞 스텝이
exit 1 이었는데도 지문 표가 로그에 남아 배포 상태를 확인할 수 있었다:

    gs_retry.py                        242  3/3
    narrative_core.py                 1315  2/2
    run_earnings_watch.py             1242  6/6   ← automation/ 탐색 동작 확인
    run_watchlist_alerts.py           2141  -

`check_freshness.py` 의 `automation/` 폴백도 레포 루트 실행에서 의도대로
동작했다(이전에는 이 두 파일이 "사본 없음"으로 빠졌다).

2026-09-03 에 A1 빨간불로 뒤 스텝이 통째로 스킵돼
check_freshness / check_py311 이 한 번도 못 돈 사고가 있었다.
이번엔 같은 구조에서 지문 표가 살아남았다.

## 검증

- YAML 파싱 OK · 스텝 7개 정상
- 러너와 같은 순서로 로컬 재현: diag_gs_retry 53/53 → diag_fmp_ssot 60/60
  → check_freshness 정합성 8/8

## 배포

`.github/workflows/diag_gs_retry.yml` **1개만** 교체.
코드 변경 없음 · Streamlit 재부팅 불필요 · 롤백 시 데이터 손실 없음.

재실행 후 기대값: 7개 스텝 전부 초록,
`✅ 전부 통과 — 53건` + `✅ 전부 통과 — 60건`.
