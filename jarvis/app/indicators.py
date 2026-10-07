import numpy as np
def sma(x,n): return np.convolve(np.asarray(x,float),np.ones(n)/n,'valid')
def ema(x,n):
    x=np.asarray(x,float); a=2/(n+1); out=[x[0]]
    for v in x[1:]: out.append(a*v+(1-a)*out[-1])
    return np.array(out)
def rsi(x,n=14):
    x=np.asarray(x,float); d=np.diff(x); up=np.maximum(d,0); dn=np.maximum(-d,0); ag=np.mean(up[:n]); al=np.mean(dn[:n]); vals=[50.0]
    for i in range(n,len(d)):
        ag=(ag*(n-1)+up[i])/n; al=(al*(n-1)+dn[i])/n; vals.append(100 if al==0 else 100-100/(1+ag/al))
    return np.array([50.0]*(len(x)-len(vals))+vals)
def bollinger(x,n=20,k=2):
    x=np.asarray(x,float); mid=np.convolve(x,np.ones(n)/n,'valid'); sd=np.array([np.std(x[i-n:i]) for i in range(n,len(x)+1)]); return mid,mid+k*sd,mid-k*sd
def macd(x,fast=12,slow=26,signal=9):
    ef,es=ema(x,fast),ema(x,slow); m=ef[-len(es):]-es; return m,ema(m,signal)
