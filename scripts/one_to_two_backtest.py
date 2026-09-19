#!/usr/bin/env python3
"""独立研究：主板首板 -> 次日二板全量历史回测。
仅使用仓库已验证的新浪全市场股票列表+日K重建事件；不读取/修改任何七因子系统。
"""
from __future__ import annotations
import csv,json,math,statistics
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from market_data_sina import fetch_all_stocks,fetch_kline,is_main_board

OUT=Path("data/backtest_one_to_two"); MIN_HISTORY=60; LIMIT_UP=.095
FEATURES=["ret_1d","ret_3d","ret_5d","ret_10d","ret_20d","turnover_1d","turnover_5d_avg","turnover_20d_avg","turnover_5_vs_20","volume_5_vs_20","amount_5_vs_20","close_above_ma20","ma5_gt_ma10","ma10_gt_ma20","near_high20_pct","above_low20_pct","up_days_5","volatility_10d","amplitude_1d","market_first_count","market_2plus_count","market_zt_count","prev_market_first_count","prev_market_2plus_count","prev_market_1to2_rate"]

def f(x,d=0.0):
    try:v=float(x);return v if math.isfinite(v) else d
    except:return d
def pct(a,b):return (a/b-1)*100 if b else 0.0
def limitup(b,p,n):
    s="".join(str(n or "").upper().split()).replace("*","")
    if s.startswith("ST") or "退" in s:return False
    return f(p.get("close"))>0 and f(b.get("close"))/f(p.get("close"))-1>=LIMIT_UP
def feats(bs,i):
    pre=bs[:i+1]
    if len(pre)<MIN_HISTORY:return {}
    c=[f(x["close"]) for x in pre];v=[f(x["volume"]) for x in pre];t=[f(x.get("turnover")) for x in pre];a=[f(x.get("amount")) for x in pre]
    ma5=statistics.fmean(c[-5:]);ma10=statistics.fmean(c[-10:]);ma20=statistics.fmean(c[-20:])
    t5=statistics.fmean(t[-5:]);t20=statistics.fmean(t[-20:]);v5=statistics.fmean(v[-5:]);v20=statistics.fmean(v[-20:]);a5=statistics.fmean(a[-5:]);a20=statistics.fmean(a[-20:])
    lo=c[-1]
    return {"ret_1d":pct(lo,c[-2]),"ret_3d":pct(lo,c[-4]),"ret_5d":pct(lo,c[-6]),"ret_10d":pct(lo,c[-11]),"ret_20d":pct(lo,c[-21]),"turnover_1d":t[-1],"turnover_5d_avg":t5,"turnover_20d_avg":t20,"turnover_5_vs_20":t5/t20 if t20 else 1,"volume_5_vs_20":v5/v20 if v20 else 1,"amount_5_vs_20":a5/a20 if a20 else 1,"close_above_ma20":int(lo>ma20),"ma5_gt_ma10":int(ma5>ma10),"ma10_gt_ma20":int(ma10>ma20),"near_high20_pct":pct(lo,max(c[-20:])),"above_low20_pct":pct(lo,min(c[-20:])),"up_days_5":sum(c[z]>c[z-1] for z in range(len(c)-5,len(c))),"volatility_10d":statistics.pstdev(c[-10:])/lo*100 if lo else 0,"amplitude_1d":pct(bs[i]["high"],bs[i]["low"]) if f(bs[i]["low"]) else 0}
def sigmoid(z):return 1/(1+math.exp(max(-35,min(35,-z))))
def auc(y,s):
    p=sum(y);n=len(y)-p
    if not p or not n:return None
    order=sorted(range(len(y)),key=lambda i:s[i]);rs=0;i=0
    while i<len(order):
        j=i+1
        while j<len(order) and s[order[j]]==s[order[i]]:j+=1
        r=(i+1+j)/2
        rs+=r*sum(y[k] for k in order[i:j]);i=j
    return (rs-p*(p+1)/2)/(p*n)
def prauc(y,s):
    p=sum(y)
    if not p:return None
    tp=fp=last=area=0
    for i in sorted(range(len(y)),key=lambda i:s[i],reverse=True):
        if y[i]:tp+=1
        else:fp+=1
        rec=tp/p;area+=(rec-last)*(tp/(tp+fp));last=rec
    return area

class LR:
    def fit(self,X,y):
        n=len(X);d=len(X[0]);self.mu=[statistics.fmean(r[j] for r in X) for j in range(d)];self.sd=[max(statistics.pstdev([r[j] for r in X]),1e-9) for j in range(d)]
        X=[[((r[j]-self.mu[j])/self.sd[j]) for j in range(d)] for r in X];self.w=[0.0]*d;self.b=math.log((sum(y)+.5)/(n-sum(y)+.5))
        for _ in range(1200):
            gw=[0.0]*d;gb=0
            for r,t in zip(X,y):
                p=sigmoid(self.b+sum(a*b for a,b in zip(self.w,r)));e=p-t;gb+=e
                for j in range(d):gw[j]+=e*r[j]
            self.b-=.05*gb/n
            for j in range(d):self.w[j]-=.05*(gw[j]/n+self.w[j]/n)
    def predict(self,X):
        return [sigmoid(self.b+sum(self.w[j]*(r[j]-self.mu[j])/self.sd[j] for j in range(len(r)))) for r in X]

def main():
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument("--start",default="2025-10-16");ap.add_argument("--end",default="2026-09-18");ap.add_argument("--workers",type=int,default=10);a=ap.parse_args()
    stocks=[r for r in fetch_all_stocks() if is_main_board(str(r.get("code","")),str(r.get("name","")))]
    meta={str(r["code"]):str(r.get("name","")) for r in stocks};bars={}
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        fs={ex.submit(fetch_kline,c,420):c for c in meta}
        for i,x in enumerate(as_completed(fs),1):
            c=fs[x]
            try:
                b=[z for z in x.result() if a.start<=str(z.get("date",""))<=a.end]
                if len(b)>=MIN_HISTORY:bars[c]=sorted(b,key=lambda z:z["date"])
            except Exception as e:print("kline_failed",c,e)
            if i%200==0:print("progress",i,"/",len(meta))
    days=sorted({z["date"] for bs in bars.values() for z in bs});pos={d:i for i,d in enumerate(days)}
    day_rows={};events=[]
    for code,bs in bars.items():
        name=meta[code];streak=0
        for i in range(1,len(bs)):
            if limitup(bs[i],bs[i-1],name):streak+=1
            else:streak=0
            if streak>0:day_rows.setdefault(bs[i]["date"],[]).append((code,streak))
            if streak==1 and len(bs[:i])>=MIN_HISTORY:events.append((bs[i]["date"],code,name,i,bs))
    nxt={days[i]:days[i+1] for i in range(len(days)-1)};prv={days[i]:days[i-1] for i in range(1,len(days))}
    mkt={d:{"zt":len(day_rows.get(d,[])),"first":sum(x[1]==1 for x in day_rows.get(d,[])),"two":sum(x[1]>=2 for x in day_rows.get(d,[]))} for d in days}
    lookup={d:dict(rows) for d,rows in day_rows.items()};rows=[]
    for d,code,name,i,bs in events:
        nd=nxt.get(d)
        if not nd:continue
        pd=prv.get(d);pm=mkt.get(pd,{"zt":0,"first":0,"two":0});pfirst=[c for c,s in day_rows.get(pd,[]) if s==1] if pd else []
        prate=sum(lookup.get(d,{}).get(c,0)>=2 for c in pfirst)/len(pfirst) if pfirst else 0
        r={"date":d,"next_date":nd,"code":code,"name":name,"label":int(lookup.get(nd,{}).get(code,0)>=2),"market_zt_count":mkt[d]["zt"],"market_first_count":mkt[d]["first"],"market_2plus_count":mkt[d]["two"],"prev_market_first_count":pm["first"],"prev_market_2plus_count":pm["two"],"prev_market_1to2_rate":prate};r.update(feats(bs,i))
        if r: 
            for k in FEATURES:r[k]=f(r.get(k))
            rows.append(r)
    dates=sorted({r["date"] for r in rows});split=dates[max(1,int(len(dates)*.7)-1)];tr=[r for r in rows if r["date"]<=split];te=[r for r in rows if r["date"]>split]
    model=LR();model.fit([[r[k] for k in FEATURES] for r in tr],[r["label"] for r in tr]);scores=model.predict([[r[k] for k in FEATURES] for r in te]);base=sum(r["label"] for r in te)/len(te)
    by={}
    for r,s in zip(te,scores):r["score"]=s;by.setdefault(r["date"],[]).append(r)
    metrics={"train_n":len(tr),"test_n":len(te),"train_rate":sum(r["label"] for r in tr)/len(tr),"test_baseline_rate":base,"test_auc":auc([r["label"] for r in te],scores),"test_pr_auc":prauc([r["label"] for r in te],scores)}
    for k in (1,3,5,10):
        picks=[];hits=0
        for rs in by.values():
            q=sorted(rs,key=lambda r:r["score"],reverse=True)[:k];picks+=q;hits+=int(any(r["label"] for r in q))
        prec=sum(r["label"] for r in picks)/len(picks);metrics[f"top{k}_precision"]=prec;metrics[f"top{k}_lift"]=prec/base if base else None;metrics[f"top{k}_daily_hit_rate"]=hits/len(by)
    OUT.mkdir(parents=True,exist_ok=True);report={"version":"v2-sina-kline-reconstruction","period":[a.start,a.end],"main_board_universe":len(meta),"stocks_with_kline":len(bars),"first_board_events":len(rows),"split_date":split,"metrics":metrics,"definition":"主板首板=日K相对前一交易日涨幅>=9.5%且连续涨停计数为1；二板=次交易日连续涨停计数>=2。","note":"独立研究模块，不修改七因子系统；由于历史涨停池接口在Actions中只返回近端数据，本版改用仓库已验证新浪日K重建。盘中封板时间/炸板次数/封单资金/历史题材暂不纳入。"}
    (OUT/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with (OUT/"test_predictions.csv").open("w",encoding="utf-8-sig",newline="") as fh:
        w=csv.DictWriter(fh,fieldnames=["date","next_date","code","name","label","score"]);w.writeheader();w.writerows({k:r[k] for k in w.fieldnames} for r in sorted(te,key=lambda x:(x["date"],-x["score"])))
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
