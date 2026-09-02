# fix(rotation): AUM 필드 `totalAssets`/`mktCap` → `marketCap` + 시트 삭제 가드

프로브 `diag_aum_field` 결과 **A안 확정**.

## 실측 결과

| 티커 | `profile.marketCap` | `etf/info.assetsUnderManagement` | `mktCap` | `totalAssets` |
|---|---|---|---|---|
| GDX | $29,620M | $29,207M | 없음 | 없음 |
| IBIT | $73,037M | $61,435M | 없음 | 없음 |
| THYP | $15.6M | $86.0M | 없음 | 없음 |

- 크기 검사 ✅ · 분리 검사 ✅ (양쪽 모두)
- GDX 비 = **1.014** → 두 소스가 같은 것을 재고 있다
- 코드가 읽던 `mktCap`·`totalAssets` 는 **세 티커 모두 응답에 없다**

사전 확정 기준대로 **A안(`profile.marketCap`, 추가 콜 0)** 을 채택한다.

### ⚠️ 기록해 둘 것 — THYP에서 두 소스가 5.5배 갈린다

`marketCap` $15.6M vs `assetsUnderManagement` $86.0M. `MIN_AUM_M = 50` 이
정확히 그 사이에 있어서, **신규 상장 ETF 는 어느 소스를 쓰느냐로 통과/제외가
뒤집힌다.** ETF 의 marketCap 은 발행좌수×가격이라 신규 상장 직후에는 갱신이
늦는 것으로 보인다.

**판정은 바꾸지 않는다** — 기준은 결과를 보기 전에 정했고 A안이 깨끗하게
통과했다. 다만 오차 방향이 **보수적(과소 → 제외)** 이라는 점을 근거로 남긴다.
SPAX 가 뚫렸던 것이 바로 신규 상장 경로였으므로, 이 방향의 오차는 감수할 만하다.
문턱값 재조정은 별건으로 남긴다.

## 🔴 조사 중 발견 — 시트 행 대량 삭제 위험

`app.py:cleanup_low_quality_etfs_from_sheet` 는 **ETF_Universe 시트의 행을
삭제한다.** 그런데 이 함수도 같은 결함이 있었다.

```python
aum = float(p.get("totalAssets") or p.get("mktCap") or 0) / 1_000_000   # 항상 0
if aum < min_aum_m or avg_vol_m < min_avg_volume_m:
    rows_to_delete.append(i)
```

`aum` 이 **항상 0** 이므로 `aum < 100.0` 은 **항상 참**이었다. 상장 6개월이
지난 ETF 는 거래대금과 무관하게 전부 삭제 후보였다는 뜻이다. 그리고 이 함수는
`run_etf_auto_update_if_needed(silent=True)` 에서 **배경 실행**된다 —
조용히 지워진다. (유니버스가 아직 남아 있는 것은 상장일 파싱 실패로 대부분이
`continue` 로 빠졌기 때문으로 보인다. 운이었다.)

필드만 고치면 값이 실제로 들어와 위험이 줄지만, **조회 실패 한 번이면 같은 일이
재발한다.** 그래서 가드를 넣었다.

```python
# 모르면 지우지 않는다. 매수 게이트의 "모르면 안 산다"와 방향이 반대다 —
# 되돌릴 수 없는 연산이므로 판정 불가는 보존 쪽으로 넘어간다.
if aum <= 0 or avg_vol_m <= 0:
    continue
```

## 파일별 변경

### `app.py` (28,218 → 28,238)
1. `cached_hidden_alpha_gates` — `marketCap` 으로 교체
2. `fetch_new_etfs_from_fmp` (신규 ETF 발견) — 교체. **이 필터는 지금까지 한 번도
   동작하지 않았다**(`if aum and ...` 의 falsy 통과). 이제 실제로 걸러지므로
   편입되는 신규 ETF 수가 줄어드는 것이 정상이다.
3. `cleanup_low_quality_etfs_from_sheet` — 교체 + **미상 삭제 금지 가드**
4. 문서열 오기 `AUM(totalAssets)` → `AUM(marketCap)`

### `run_hidden_alpha.py` (966 → 972)
`:389`(신규 발견) · `:517`(게이트) 두 곳 교체. `app.py` 와 **lockstep**.

### `diag_rotation_policy.py` (607 → 647) — **111 → 119 검사**

- **스텁 수정**: 기존 스텁이 `totalAssets` 를 돌려주고 있었다. `/stable/profile`
  에 없는 키다. **이 스텁이 실결함을 정확히 가렸다** — 배포 전에 못 잡은 직접 원인.
- **W-R12**: 레거시 키(`totalAssets`/`mktCap`)만 오면 값이 커도 AUM 미상으로 제외
- **W-13/14**: 게이트 함수 본문 AST 에 레거시 키 없음 · `marketCap` 읽음 (양쪽 파일)
- **W-15/16/17**: 삭제 함수 존재 · **미상 보존 가드 존재** · 레거시 키 없음.
  이 함수는 Streamlit·gspread 의존이라 실행 시험이 불가능하다 — 구조 검사가
  유일한 방어선이다.

### 범위 밖 (손대지 않음)
`app.py:7729`(`marketCap` 우선, `mktCap` 은 무해한 폴백) · `app.py:20717`
(개별종목 표시 경로). 로테이션 게이트와 무관하다.

## 검증

| 항목 | 결과 |
|---|---|
| `py_compile` · `check_py311` | 3/3 OK |
| `pyflakes` 델타 | **0** |
| `diag_rotation_policy` | **119/119** (111 + 신규 8) |
| split 배치 재현 | 루트 + `automation/` 분리에서 119/119 |
| 변이 시험 | **4/4 검출** |

### 변이 상세
- **N1** app 게이트 레거시 키 회귀 → W-R12/13 이 잡음
- **N2** run_hidden_alpha 게이트 회귀 → W-13 이 잡음
- **N3** 삭제 가드 제거 → **처음엔 놓쳤다.** 그 자리를 덮는 검사가 없었다.
  W-15/16/17 추가 후 W-16 이 잡음.
- **N4** 스텁이 다시 레거시 키를 줌 → W-R12 가 잡음. 이번 결함을 가렸던 그
  실수가 재발하면 즉시 드러난다.

## 남은 한계

- `MIN_AUM_M = 50` 과 신규 상장의 marketCap/AUM 괴리 — 문턱값 재조정은 별건
- `fetch_new_etfs_from_fmp` 의 `if aum and aum < min` falsy 패턴은 그대로 두었다.
  이제 값이 실제로 오므로 동작하지만, `rc.passes_aum` 위임으로 정리하는 것이
  옳다 (후속)
- `cleanup_low_quality_etfs_from_sheet` 가 과거에 실제로 몇 행을 지웠는지는
  알 수 없다. 시트에 삭제 로그가 없다.
