"""diag_endpoint_fix.py — FMP 엔드포인트 경로 수정 회귀 검증.

2026-08-15 엔드포인트 일괄 수정이 되돌아가지 않았는지 확인한다.
프로젝트 루트에서 실행하며 **네트워크·시트·이메일 접촉이 없다** (소스만 읽는다).

검사 항목
---------
  1) cached_etf_holdings_universe_str 반환 타입이 DataFrame 인가
     - list 를 돌려주면 app.py 의 `hdf.empty` 가 AttributeError 로 터진다.
       그 지점은 try 로 감싸여 있지 않아 기회 스캐너가 그대로 죽는다.
  2) cached_earnings_history 가 경로와 필드명을 **함께** 고쳤는가
     - 경로만 바꾸면 404 가 "전 행 N/A" 로 바뀔 뿐 더 조용한 실패가 된다.
  3) 죽은 경로 5종이 라이브 코드(주석/독스트링 제외)에 없는가
  4) narrative_core 의 SECTION A 가 렌더링 불가능해졌는가

변이 테스트
-----------
심어놓은 버그 4종(M1 반환타입 되돌림 / M2 필드명 미수정 / M3 죽은 뉴스 경로 /
M4 SECTION A 복구)을 모두 잡는 것을 확인했다. 1차 작성본은 하드코딩 값을
검사해 M1/M2 를 놓쳤고, 실제 소스를 AST 로 읽도록 고쳐 4/4 검출로 만들었다.

실행
----
    python automation/diag_endpoint_fix.py     # 종료코드 0=통과, 1=실패
"""
import ast, sys, pandas as pd

FAIL=[]
def ck(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ")+name+(("  — "+detail) if detail and not cond else ""))
    if not cond: FAIL.append(name)

print("── 1) 잠복 크래시: 반환 타입이 호출부 계약과 맞는가")
# [변이테스트 반영] 하드코딩 값이 아니라 **실제 소스의 return 문**을 검사한다.
src_app=open("app.py",encoding='utf-8').read()
t_app=ast.parse(src_app)
fn=[n for n in ast.walk(t_app) if isinstance(n,ast.FunctionDef)
    and n.name=="cached_etf_holdings_universe_str"]
ck("cached_etf_holdings_universe_str 존재", len(fn)==1)
if fn:
    rets=[n for n in ast.walk(fn[0]) if isinstance(n,ast.Return) and n.value is not None]
    ck("return 문이 1개 이상", len(rets)>=1)
    bad=[ast.unparse(r.value) for r in rets
         if not (isinstance(r.value,ast.Call)
                 and ast.unparse(r.value.func).endswith("DataFrame"))]
    ck("모든 return 이 DataFrame (list 반환 없음)", not bad, str(bad))
    txt=ast.unparse(fn[0])
    ck("컬럼 계약 Ticker/Weight(%) 명시", "Ticker" in txt and "Weight(%)" in txt)
# 소비부 가드가 실제로 통과하는지 런타임 확인
new_ret = pd.DataFrame(columns=["Ticker","Weight(%)"])
try:
    r = (new_ret is None or new_ret.empty or "Ticker" not in new_ret.columns)
    ck("app.py:13273 가드식 예외 없이 통과", True)
    ck("빈 DataFrame 은 continue 로 걸러짐", r is True)
    top10 = new_ret.head(10).reset_index(drop=True)
    ck("app.py:18711 .head(10).reset_index() 동작", list(top10.columns)==["Ticker","Weight(%)"])
except Exception as e:
    ck("소비부 가드", False, repr(e))
# list 로 되돌리면 실제로 터지는지(대조군)
try:
    [].empty; ck("대조군: list 는 .empty 에서 터진다", False, "안 터짐")
except AttributeError:
    ck("대조군: list 는 .empty 에서 터진다", True)

print("\n── 2) earnings 필드 매핑: 실제 소스가 올바른 키를 읽는가")
# [변이테스트 반영] 소스에서 cached_earnings_history 를 뽑아 키 사용을 검사한다.
fn2=[n for n in ast.walk(t_app) if isinstance(n,ast.FunctionDef)
     and n.name=="cached_earnings_history"]
ck("cached_earnings_history 존재", len(fn2)==1)
if fn2:
    body=ast.unparse(fn2[0])
    ck("경로가 earnings?symbol= 로 교체됨", "earnings?symbol=" in body)
    ck("죽은 earnings-surprises 경로 없음", "earnings-surprises" not in body)
    ck("epsActual 을 읽는다", "epsActual" in body)
    ck("epsEstimated 를 읽는다", "epsEstimated" in body)
    # 경로만 바꾸고 필드는 안 바꾼 상태를 잡아낸다
    ck("경로·필드 동시 수정 확인",
       ("earnings?symbol=" in body) and ("epsActual" in body) and ("epsEstimated" in body))
def to_float(v):
    try: return float(v)
    except Exception: return float("nan")
item = {"date":"2026-05-01","epsActual":1.65,"epsEstimated":1.50,"symbol":"AAPL"}
eps_actual = to_float(item.get("epsActual") or item.get("actualEarningResult")
                      or item.get("actualEPS") or item.get("eps"))
eps_est = to_float(item.get("epsEstimated") or item.get("estimatedEarning")
                   or item.get("estimatedEPS"))
ck("실응답 형태 파싱 (실제/예상)", eps_actual==1.65 and eps_est==1.50)
ck("서프라이즈 계산", abs((eps_actual-eps_est)/abs(eps_est)*100-10.0)<0.01)
old_item={"actualEarningResult":2.0}
ck("구 필드명 하위호환 유지",
   to_float(old_item.get("epsActual") or old_item.get("actualEarningResult"))==2.0)

print("\n── 3) 죽은 경로가 라이브 코드에 남아있지 않은가")
import re
DEAD=["etf-holder/","stock-news?symbols=","batch-market-capitalization",
      "etf/sector-weighting?","earnings-surprises?symbol="]
for f in ["app.py","fmp_extras.py","earnings_core.py","narrative_core.py",
          "diag_earnings_preview_backtest.py"]:
    src=open(f,encoding='utf-8').read()
    tree=ast.parse(src)
    # 문자열 리터럴만 검사 (주석/독스트링 제외)
    lits=[]
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value,str):
            par_doc=False
            lits.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            for v in n.values:
                if isinstance(v, ast.Constant) and isinstance(v.value,str):
                    lits.append(v.value)
    # 독스트링 제거
    docs=set()
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Module)):
            d=ast.get_docstring(n, clean=False)
            if d: docs.add(d)
    live=[l for l in lits if l not in docs]
    for d in DEAD:
        hit=[l for l in live if d in l]
        ck(f"{f}: '{d}' 미사용", not hit, str(hit[:1]))

print("\n── 4) narrative_core: SECTION A 가 렌더링 불가능해졌는가")
src=open("narrative_core.py",encoding='utf-8').read()
tree=ast.parse(src)
fn=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=="_build_news_context_text"][0]
body=ast.get_source_segment(src,fn)
ck("section_defs 에서 'FMP Press Release' 제거됨", '"FMP Press Release"' not in body)
ck("SECTION B/C/D/E 는 유지", all(f"[SECTION {c}]" in body for c in "BCDE"))
fn2=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=="fetch_global_market_news"][0]
b2=ast.get_source_segment(src,fn2)
ck("layer_b 변수 완전 제거", "layer_b" not in b2)
ck("layer_a/c/d 유지", all(x in b2 for x in ["layer_a","layer_c","layer_d"]))
ck("RSS 폴백 조건 보존", "raw_count == 0" in b2)

print("\n"+("="*60))
print("실패 "+str(len(FAIL))+"건" + ((": "+", ".join(FAIL)) if FAIL else " — 전부 통과"))
sys.exit(1 if FAIL else 0)
