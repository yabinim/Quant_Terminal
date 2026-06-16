# -*- coding: utf-8 -*-
"""통일 결정(buy/sell decision + cards) 테스트."""
import numpy as np, pandas as pd, regime_core as rc
P=F=0
def ck(n,c):
    global P,F
    if c: P+=1; print(f"  ✓ {n}")
    else: F+=1; print(f"  ✗ FAIL: {n}")

print("[1] buy_decision 매핑")
ck("entry+fit → buy", rc.buy_decision("entry","fit","strong")["key"]=="buy")
ck("entry+skip(강세) → 대기(눌림)", rc.buy_decision("entry","skip","strong")["key"]=="wait_pullback")
ck("overheat → 매수(분할)", rc.buy_decision("overheat","caution","strong")["key"]=="buy_split")
ck("wait+strong → 대기(눌림)", rc.buy_decision("wait",None,"strong")["key"]=="wait_pullback")
ck("wait+sideways → 대기(횡보)", rc.buy_decision("wait",None,"sideways")["key"]=="wait_range")
ck("avoid → 회피", rc.buy_decision("avoid","avoid","weak")["key"]=="avoid")
ck("trend_break → 회피", rc.buy_decision("trend_break",None,"strong")["key"]=="avoid")
ck("gate avoid → 회피", rc.buy_decision("entry","avoid","strong")["key"]=="avoid")
ck("라벨 한글", rc.buy_decision("overheat",None,"strong")["label"]=="🟢 매수(분할)")

print("[2] build_buy_card — hist + 손수 analysis/plan")
N=260; di=pd.date_range("2024-01-02",periods=N,freq="B"); t=np.arange(N)
px=100*(1.0009**t)+4*np.sin(t/15.0)
hist=pd.DataFrame({"Close":px,"High":px*1.01,"Low":px*0.99},index=di)
plan={"gate":"fit","stop":float(px[-1]*0.95),"target":float(px[-1]*1.12),"rr_label":"1:2.4",
      "shares":43,"position_pct":18.0,"stop_pct":-5.0,"target_pct":12.0}
# wait(눌림)
an_wp={"timing":{"code":"wait"},"regime":{"regime":"strong"},"exit":{}}
c=rc.build_buy_card(hist, an_wp, {**plan,"gate":None}, confluence=72)
ck("대기(눌림) 라벨", c["label"]=="🟡 대기(눌림)")
ck("트리거 존재", len(c["trigger"])>0)
ck("무효화 존재", len(c["invalidation"])>0)
ck("glance 트리거", c["glance_num"].startswith("트리거"))
ck("why에 근거중첩+RSI", "근거중첩 72" in c["why"] and "RSI" in c["why"])
# buy
an_b={"timing":{"code":"entry"},"regime":{"regime":"strong"},"exit":{}}
cb=rc.build_buy_card(hist, an_b, plan, confluence=81)
ck("매수 라벨", cb["label"]=="🟢 매수")
ck("plan_line 진입/손절/목표", "진입" in cb["plan_line"] and "손절" in cb["plan_line"])
ck("glance R:R", cb["glance_num"].startswith("R:R"))
# buy_split
cs=rc.build_buy_card(hist, {"timing":{"code":"overheat"},"regime":{"regime":"strong"},"exit":{}}, plan)
ck("분할 1차/2차 트리거", "1차" in cs["trigger"] and "2차" in cs["trigger"])
ck("glance 1차 지금", cs["glance_num"]=="1차 지금")
# 횡보
crg=rc.build_buy_card(hist, {"timing":{"code":"wait"},"regime":{"regime":"sideways"},"exit":{}}, {**plan,"gate":None})
ck("횡보 박스 트리거", "박스 상단" in crg["trigger"] and "박스 하단" in crg["invalidation"])
# 회피
cav=rc.build_buy_card(hist, {"timing":{"code":"avoid"},"regime":{"regime":"weak"},"exit":{}}, {**plan,"gate":"avoid"})
ck("회피 headline", "회피" in cav["headline"] and cav["glance_num"]=="—")

print("[3] build_buy_card — 실제 analyze_ticker+build_trade_plan 통합")
an=rc.analyze_ticker(hist, spy_close=pd.Series(100*(1.0004**t),index=di), entry_price=None)
pl=rc.build_trade_plan(verdict_code=an["timing"].get("code"), entry=float(px[-1]),
    atr=rc.compute_atr(hist), ma200=float(pd.Series(px).rolling(200,min_periods=150).mean().dropna().iloc[-1]),
    equity=10000, risk_pct=1.0, atr_mult=2.0, rr_target=2.5, recent_high=float(hist["High"].tail(120).max()))
cint=rc.build_buy_card(hist, an, pl, confluence=70)
ck("통합 카드 key 유효", cint["key"] in rc.BUY_DECISION_LABELS)
ck("badge==label", cint["badge"]==cint["label"])

print("[4] sell_decision + build_sell_card")
ck("is_exit → 청산", rc.sell_decision({"is_exit":True,"reasons":["50일선 이탈"]})["key"]=="exit")
ck("warnings → 줄이기", rc.sell_decision({"is_exit":False,"warnings":["네거티브 리버설"]})["key"]=="trim")
ck("정상 → 보유", rc.sell_decision({"is_exit":False,"warnings":[]})["key"]=="hold")
sc=rc.build_sell_card({"exit":{"is_exit":False,"warnings":["네거티브 리버설: 토핑"]}}, {"stop":80.5,"target":94.4,"stop_pct":-6.8})
ck("줄이기 카드 detail+line", "토핑" in sc["detail"] and "손절" in sc["line"])

print(f"\n결과: {P} passed, {F} failed")
import sys; sys.exit(1 if F else 0)
