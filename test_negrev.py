# -*- coding: utf-8 -*-
"""regime_core 네거티브 리버설 단위 테스트 (item1)."""
import numpy as np, pandas as pd, regime_core as rc
P=F=0
def ck(n,c):
    global P,F
    if c: P+=1; print(f"  ✓ {n}")
    else: F+=1; print(f"  ✗ FAIL: {n}")

def make_close():
    # peak1=110@idx10, dip, peak2=108@idx25(낮은 고점), 이후 하락 102
    seg = []
    seg += list(np.linspace(100,110,11))      # 0..10 ↑ (peak1=110@10)
    seg += list(np.linspace(108.5,103,7))      # 11..17 ↓
    seg += list(np.linspace(104,108,8))        # 18..25 ↑ (peak2=108@25)
    seg += list(np.linspace(107,102,14))       # 26..39 ↓
    return pd.Series(seg)

close = make_close()
# RSI: 기본 50, 고점1=58, 고점2=62(higher high), 마지막=55(꺾임)
def rsi_with(v10, v25, vlast):
    r = pd.Series([50.0]*len(close))
    r.iloc[10]=v10; r.iloc[25]=v25; r.iloc[-1]=vlast
    return r

print("[1] 정상 네거티브 리버설 → detected")
d = rc._detect_negative_reversal(close, rsi_with(58,62,55))
ck("detected True", d["detected"] is True)
ck("메시지에 '토핑'", "토핑" in d["message"])
ck("detail high1>high2", d["detail"]["high1"] > d["detail"]["high2"])

print("[2] 가격이 higher high면 미탐지")
ch2 = close.copy(); ch2.iloc[25]=112  # peak2 > peak1
ck("higher-high → not detected", rc._detect_negative_reversal(ch2, rsi_with(58,62,55))["detected"] is False)

print("[3] RSI가 lower high면 미탐지")
ck("rsi lower-high → not detected", rc._detect_negative_reversal(close, rsi_with(62,58,55))["detected"] is False)

print("[4] RSI<55(상단 아님)면 미탐지")
ck("rsi<55 → not detected", rc._detect_negative_reversal(close, rsi_with(48,52,45))["detected"] is False)

print("[5] 미확정(현재가가 2번째 고점 위)면 미탐지")
ch5 = close.copy(); ch5.iloc[-1]=109  # 현재가 > peak2(108)
ck("미확정 → not detected", rc._detect_negative_reversal(ch5, rsi_with(58,62,57))["detected"] is False)

print("[6] compute_exit_signals 통합 — warnings 키 존재 & is_exit 무영향")
N=160; di=pd.date_range("2024-01-02",periods=N,freq="B"); t=np.arange(N)
px=100*(1.0009**t); hist=pd.DataFrame({"Close":px,"High":px*1.01,"Low":px*0.99},index=di)
ex=rc.compute_exit_signals(hist)
ck("warnings 키 존재(list)", isinstance(ex.get("warnings"), list))
ck("detail에 negative_reversal", "negative_reversal" in ex.get("detail",{}))
ck("상승 추세 → is_exit False(경고가 청산 강제 안함)", ex["is_exit"] is False)

print(f"\n결과: {P} passed, {F} failed")
import sys; sys.exit(1 if F else 0)
