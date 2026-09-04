chore(diag): tierC·grades 프로브 그룹 종결 · tierE(업종 PER 깊이) 신설

TierC(4콜)와 grades(3콜)는 백로그에 "미실행"으로 남아 있었으나 실제로는
2026-08-22 에 이미 실행됐거나(tierC) 콜 없이 답이 나와 있었다(grades).
장부만 낡아 있었다. 판정을 파일에 박고 실행 그룹을 제거해, 닫힌 질문에
콜이 나가지 않게 한다. 진짜로 열려 있던 잔여분 1건만 tierE 로 남긴다.

FMP 호출 변화: 이 커밋 자체는 0콜. 배포 후 tierE 수동 실행 시 1콜.

---

## 파일별 변경

### 1. `automation/diag_fmp_newcaps.py` (1250 → 1278줄)

**제거 — `TIER_C` 실행 그룹 (4콜)**
2026-08-22 00:28 UTC 실행 완료. 판정을 §7 주석 블록으로 대체했다.

| 엔드포인트 | 판정 |
|---|---|
| `senate-latest` | ✅ 200 · 10건 → tierD 가 전제 붕괴(지연 중앙값 572일) → 🔴 종결 |
| `house-latest` | ✅ 200 · 10건 → tierD 지연 중앙값 27일 · 7일 이내 3% → 🔴 종결 |
| `institutional-ownership/holder-performance-summary` | 🔒 402 플랜 미포함 → 영구 제외 |
| `historical-industry-pe` | ✅ 200 · 65건 · 키 `date/exchange/industry/pe` → 깊이 미측정, tierE 로 이월 |

의회 거래 2건의 최종 근거는 `VERDICT_2026-08-22_congress_trades_terminated.md`
및 `app.py:21546` 에 이미 기록돼 있다.

**제거 — `GRADES` 실행 그룹 (3콜) · 콜 소모 0으로 종결**
원래 질문은 "`grades?symbol=` 가 기존 등급 엔드포인트와 겹치는가"였다.
답이 이미 두 곳에 있었다.

- `grades` 응답 키 — tierD 로그(2026-08-22): `action, date, gradingCompany, newGrade, previousGrade, symbol`
- `grades-consensus` 응답 키 — `app.py:8428` / `app.py:21200` 파싱부: `strongBuy, buy, hold, sell, strongSell, consensus, symbol`
- `ratings-snapshot` — 이미 제거 완료(호출 0건, `diag_analyst_congress` A-1 이 래칫)

겹치는 필드는 `symbol` 뿐. **중복이 아니다** — `grades` 는 등급 변경 이벤트
로그, `grades-consensus` 는 현재 스냅샷 집계로 축이 다르다.

실사용 제약도 함께 확정(tierD D-3/D-4 실측): `grades` 는 `limit` 도 `from/to`
도 무시한다(요청 10건 → 1787건 / 1년 구간 요청에 2012년 데이터 포함, 구간 밖
1674건). **워치리스트 전체 적용 불가.** 단일 종목 온디맨드 1콜은 기술적으로
가능하나 신규 기능 영역이며 이 프로브의 잔여 과제가 아니다.

**신설 — `TIER_E` (1콜)**
```
historical-industry-pe?industry=Semiconductors&from=<today-7y>&to=<today>
필요 필드: date, industry, pe · 렌더러: depth_req (기존 재사용)
```
tierC 는 `from=today-90d` 를 보내 90일치를 받았다. **요청한 만큼 왔다는 것은
한도를 못 쟀다는 뜻**이지 한도가 그 값이라는 뜻이 아니다 — tierB3 에서 이미
같은 착오를 했다. 형제 엔드포인트(`historical-industry-performance` 7.0년)로
추론하지 않는 것도 §7 원칙이다.

판별자는 `min(date) − 요청 from`. **사전 확정 기준**(결과를 보고 고치지 않음):

- 확보 폭 **5년 이상** → 데이터는 쓸 만하다. **기록 후 보류**
- **5년 미만** → 종결(학습창에 2022 하락장이 안 들어온다)

부수 확인: tierC 의 90일 요청에 65건(≈62 거래일)이 온 것으로 보아 이
엔드포인트는 `from/to` 를 실제로 존중한다. `grades` 와 정반대이며, 범위 통제는
이미 통과한 상태다.

**콜 수 표기 정합** — `all` 이 세 군데에서 갈려 있었다(py 주석 32 · yml 주석 32
· yml 입력설명 36 · 실제 36). 지금은 실제 30 으로 전부 일치시키고, 손으로 세지
말고 실행 헤더의 `호출 N콜`(len 기반)을 보라는 경고를 양쪽에 남겼다.

**티어 허용목록** — `("tiera","tierb","tierb2","tierb3","tierb4","tierd","tiere","all")`.
`tierC`/`grades` 가 들어오면 조용히 `tierA`(5콜)로 떨어지므로, 그 사실을 주석에
명시했다(낡은 북마크 대비).

### 2. `.github/workflows/diag_fmp_newcaps.yml` (167 → 195줄)

- 드롭다운에서 `tierC` · `grades` 제거, `tierE` 추가
- **기본값 `tierA` → `tierE`**. tierA~tierD 는 전부 판정이 끝난 그룹이라 실수로
  눌리면 닫힌 질문에 5콜이 나간다. 지금 열려 있는 질문은 tierE 하나뿐이다
- 옵션 순서에서 `tierE` 를 맨 앞으로
- 설명 문자열에 종결 티어를 `✅종결` 로 표기
- 주석에 tierC/tierD/grades 실측 결과와 tierE 사전 확정 기준을 기록

---

## 검증 결과

| 항목 | 결과 |
|---|---|
| `py_compile` | ✅ 통과 |
| `check_py311.py` | ✅ 통과 |
| `pyflakes` | 0건 (원본도 0건 · 델타 0) |
| 임포트 스모크(워크플로 동일 명령) | ✅ `import OK` |
| YAML 파싱 | ✅ 통과 · 기본값이 옵션 안에 존재 |
| 티어 디스패치 | tierA 5 · tierB 9 · tierB2 5 · tierB3 3 · tierB4 3 · tierD 4 · **tierE 1** · **all 30** |
| 폐기 티어 폴백 | `tierC`/`grades`/`garbage` → `tiera`(5콜) — 의도대로 |
| 옵션↔허용목록 교차검증 | ✅ 완전 일치(양방향 차집합 공집합) |
| 렌더러 드라이런 | 합성 응답 2종으로 `depth_req` 확인 — 🟢 한도 미도달(7.0년) / 🔵 한도 발견(3.0년) 양 분기 정상 |
| A1 래칫(`diag_fmp_ssot.py:177`) | 원시 `requests.get` **1건 유지** — 기준선 변동 없음 |

**역검증**: 원본 파일로 콜수 정합 검사를 돌리면 `실제 36 · py주석 32 · yml주석
32 · yml설명 36` 으로 ❌ 불일치가 나온다. 수정본은 30 으로 4곳 전부 일치.

---

## 배포

`automation/` 1개 + `.github/workflows/` 1개. **Streamlit 재부팅 불필요**
(app.py 및 코어 모듈 무변경). 락스텝 대상 없음.

1. `automation/diag_fmp_newcaps.py` — GitHub 표시 **1277줄**
2. `.github/workflows/diag_fmp_newcaps.yml` — GitHub 표시 **194줄**

순서는 위 그대로. 1번을 먼저 올리면 그 사이에 낡은 드롭다운으로 실행해도
아무것도 깨지지 않는다(폐기 값은 tierA 로 폴백). 반대로 2번을 먼저 올리면
`tierE` 를 낡은 스크립트가 못 알아듣고 tierA 5콜이 나간다.

### 배포 직후 확인

Actions → `🔬 진단 — FMP 미사용 엔드포인트 실측 (신규 기능 후보 · 수동 전용)`
→ `Run workflow` → `확인 티어` 드롭다운이 **`tierE` 가 기본 선택**으로 뜨고
`tierC` · `grades` 가 목록에 없으면 정상.

### tierE 실행 (수동 1회 · 1콜)

그대로 `Run workflow` 실행. 로그에서 볼 것:

```
티어: tiere · 호출 1콜 · 시트/이메일 접촉 없음
Tier E — 업종 PER 이력 깊이 (tierC 잔여분)
── 업종 PER 시계열 — 이력 깊이 실측
      └ 요청 from : <today-7y>
         실제 최초 : ....-..-..   (요청 대비 +N일)
```

판정 줄이 셋 중 하나로 나온다:

- `🟢 한도 미도달` + `X.X년 확보` → **X ≥ 5 면 기준 통과(기록 후 보류)**
- `🔵 한도 발견 — 실제 상한 ≈ X.X년` → **X < 5 면 종결**
- `🟠 판정 보류` → from 을 더 앞으로 밀어 재확인

`티어: tiera · 호출 5콜` 이 찍히면 스크립트가 tierE 를 못 알아들은 것이다
(1번 파일이 안 올라갔다는 뜻) — 즉시 중단하고 업로드를 확인할 것.

### 정상적으로 **안 보이는** 것

- 시트 변화 0 · 이메일 0 · 알림 상태 머신 미접촉
- app.py / Streamlit 화면 변화 0
- 예약 실행 없음(`workflow_dispatch` 전용) — 누르지 않으면 콜이 나가지 않는다

### 롤백

두 파일을 되돌리면 끝. **데이터 손실 없음** — 이 프로브는 시트를 읽지도 쓰지도
않는다.

---

## 남은 한계 · 후속

1. **tierA~tierD 도 전부 종결된 그룹이다.** 이번엔 tierC/grades 만 제거했다.
   나머지도 드롭다운에 남아 있어 실수로 누르면 최대 21콜이 닫힌 질문에 나간다.
   기본값을 tierE 로 바꿔 위험은 낮췄지만 근본 정리는 하지 않았다 — 별도 작업.
2. **`industry-pe-snapshot` 존재 여부 미확인.** tierE 가 ✅ 로 나와도 기능화
   하려면 일회성 백필 149콜 + 새 와이드 시트 + 일일 유지콜이 필요한데, 그
   일일 유지콜에 해당하는 스냅샷 엔드포인트가 있는지 재보지 않았다. 없으면
   매일 149콜이라 유지 불가다.
3. **착공은 별건이다.** 형제 신호인 업종 모멘텀은 2026-09-01 롤링 워크포워드
   에서 6창 중 1창으로 부결됐다. tierE 의 ✅ 는 "데이터가 쓸 만하다"까지만
   말하며 신호 채택을 뜻하지 않는다.
4. `grades` 단일 종목 온디맨드(정밀검사 탭 1콜 + 클라이언트 필터)는 기술적으로
   가능하다. 신규 기능이므로 이번 커밋 범위 밖으로 뒀다.
