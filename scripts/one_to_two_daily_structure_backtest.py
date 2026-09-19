#!/usr/bin/env python3
"""第二轮快速交叉验证：首板日K封板结构代理 -> 次日二板。
不使用分钟数据，因此用于判断“日K可稳定重建的封板形态”是否能提升Top1。
"""
from __future__ import annotations
import csv, json, math, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from market_data_sina import fetch_all_stocks, fetch_kline, is_main_board

OUT=Path("data/backtest_one_to_two_round2_daily_proxy")
MIN_HISTORY=60
LIMIT_UP=.095

BASE=[
"ret_1d","ret_3d","ret_5d","ret_10d","ret_20d","turnover_1d","turnover_5d_avg","turnover_20d_avg",
"turnover_5_vs_20","volume_5_vs_20","amount_5_vs_20","close_above_ma20","ma5_gt_ma10","ma10_gt_ma20",
"near_high20_pct","above_low20_pct","up_days_5","volatility_10d","amplitude_1d","market_first_count",
"market_2plus_count","market_zt_count","prev_market_first_count","prev_market_2plus_count","prev_market_1to2_rate",
]
STRUCT=[
"open_gap_pct","open_gap_band_high","open_gap_band_mid","open_gap_band_low","open_to_close_pct",
"lower_wick_ratio","intraday_pullback_pct","open_position_in_day","amplitude_vs_20d","turnover_vs_20d",
"volume_vs_20d","amount_vs_20d","near_limit_open","close_near_high",
]
ALL=BASE+STRUCT

def f(x,d=0.0):
    try:
        v=float(x); return v if math.isfinite(v) else d
    except (TypeError,ValueError): return d
def pct(a,b): return (a/b-1)*100 if b else 0
def lim(b,p,n):
    s="".join(str(n or "").upper().split()).replace("*","")
    return not (s.startswith("ST") or "退" in s) and f(p.get("close"))>0 and f(b.get("close"))/f(p.get("close"))-1>=LIMIT_UP

def base_feats(bs,i):
    pre=bs[:i+1]; c=[f(x["close"]) for x in pre];v=[f(x["volume"]) for x in pre];t=[f(x.get("turnover")) for x in pre];a=[f(x.get("amount")) for x in pre]
    ma5=statistics.fmean(c[-5:]);ma10=statistics.fmean(c[-10:]);ma20=statistics.fmean(c[-20:])
    t5=statistics.fmean(t[-5:]);t20=statistics.fmean(t[-20:]);v5=statistics.fmean(v[-5:]);v20=statistics.fmean(v[-20:]);a5=statistics.fmean(a[-5:]);a20=statistics.fmean(a[-20:])
    p=c[-1]
    return {
      "ret_1d":pct(p,c[-2]),"ret_3d":pct(p,c[-4]),"ret_5d":pct(p,c[-6]),"ret_10d":pct(p,c[-11]),"ret_20d":pct(p,c[-21]),
      "turnover_1d":t[-1],"turnover_5d_avg":t5,"turnover_20d_avg":t20,"turnover_5_vs_20":t5/t20 if t20 else 1,
      "volume_5_vs_20":v5/v20 if v20 else 1,"amount_5_vs_20":a5/a20 if a20 else 1,
      "close_above_ma20":int(p>ma20),"ma5_gt_ma10":int(ma5>ma10),"ma10_gt_ma20":int(ma10>ma20),
      "near_high20_pct":pct(p,max(c[-20:])),"above_low20_pct":pct(p,min(c[-20:])),
      "up_days_5":sum(c[z]>c[z-1] for z in range(len(c)-5,len(c))),
      "volatility_10d":statistics.pstdev(c[-10:])/p*100 if p else 0,
      "amplitude_1d":(f(bs[i]["high"])-f(bs[i]["low"]))/p*100 if p else 0,
    }

def struct_feats(bs,i):
    b=bs[i]; prev=bs[i-1]; pc=f(prev["close"]);o=f(b["open"]);h=f(b["high"]);l=f(b["low"]);c=f(b["close"])
    rng=max(h-l,1e-9)
    pre=bs[:i+1]; vols=[f(x["volume"]) for x in pre]; amts=[f(x.get("amount")) for x in pre]; trs=[f(x.get("turnover")) for x in pre]
    v20=statistics.fmean(vols[-20:]);a20=statistics.fmean(amts[-20:]);t20=statistics.fmean(trs[-20:])
    open_gap=pct(o,pc)
    # 由于首板收盘涨停，以下变量用于区分“一字/高开后回封/低开后强拉”等日K形态。
    return {
      "open_gap_pct":open_gap,
      "open_gap_band_high":int(open_gap>=8.5),
      "open_gap_band_mid":int(5<=open_gap<8.5),
      "open_gap_band_low":int(open_gap<5),
      "open_to_close_pct":pct(c,o),
      "lower_wick_ratio":(o-l)/rng,
      "intraday_pullback_pct":pct(o,l),
      "open_position_in_day":(o-l)/rng,
      "amplitude_vs_20d":((h-l)/pc*100)/(statistics.fmean((f(x["high"])-f(x["low"]))/f(x["close"])*100 for x in pre[-20:]) or 1),
      "turnover_vs_20d":f(b.get("turnover"))/t20 if t20 else 1,
      "volume_vs_20d":f(b.get("volume"))/v20 if v20 else 1,
      "amount_vs_20d":f(b.get("amount"))/a20 if a20 else 1,
      "near_limit_open":int(open_gap>=8.0),
      "close_near_high":int(h>0 and (h-c)/h<=0.002),
    }

def sigmoid(z): return 1/(1+math.exp(max(-35,min(35,-z))))
class LR:
    def fit(self,X,y):
        n=len(X);d=len(X[0]);self.mu=[statistics.fmean(r[j] for r in X) for j in range(d)];self.sd=[max(statistics.pstdev([r[j] for r in X]),1e-9) for j in range(d)]
        Z=[[(r[j]-self.mu[j])/self.sd[j] for j in range(d)] for r in X];self.w=[0.0]*d
        self.b=math.log((sum(y)+.5)/(n-sum(y)+.5))
        for _ in range(1200):
            gw=[0.0]*d;gb=0
            for r,t in zip(Z,y):
                p=sigmoid(self.b+sum(a*b for a,b in zip(self.w,r)));e=p-t;gb+=e
                for j in range(d):gw[j]+=e*r[j]
            self.b-=.05*gb/n
            for j in range(d):self.w[j]-=.05*(gw[j]/n+self.w[j]/n)
    def predict(self,X):
        return [sigmoid(self.b+sum(self.w[j]*(r[j]-self.mu[j])/self.sd[j] for j in range(len(r)))) for r in X]

def top1(rows,scores):
    by={}
    for r,s in zip(rows,scores):by.setdefault(r["date"],[]).append((s,r))
    picks=[max(v,key=lambda x:x[0])[1] for v in by.values()]
    return sum(r["label"] for r in picks)/len(picks) if picks else 0,len(by)

def cond(rows,defs):
    base=sum(r["label"] for r in rows)/len(rows) if rows else 0; out=[]
    for name,fn in defs:
        q=[r for r in rows if fn(r)]
        if len(q)>=20:
            rate=sum(r["label"] for r in q)/len(q);out.append({"condition":name,"n":len(q),"rate":rate,"lift":rate/base if base else None})
    return sorted(out,key=lambda x:(-x["rate"],-x["n"]))

def main():
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument("--start",default="2025-10-16");ap.add_argument("--end",default="2026-09-18");ap.add_argument("--workers",type=int,default=10);a=ap.parse_args()
    stocks=[r for r in fetch_all_stocks() if is_main_board(str(r.get("code","")),str(r.get("name","")))];meta={str(r["code"]):str(r.get("name","")) for r in stocks}
    bars={}
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        fs={ex.submit(fetch_kline,c,420):c for c in meta}
        for i,fut in enumerate(as_completed(fs),1):
            c=fs[fut]
            try:
                b=[z for z in fut.result() if a.start<=str(z.get("date",""))<=a.end]
                if len(b)>=MIN_HISTORY:bars[c]=sorted(b,key=lambda z:z["date"])
            except Exception as e: print("kline_failed",c,e)
            if i%200==0: print("progress",i,"/",len(meta))
    days=sorted({z["date"] for bs in bars.values() for z in bs}); nxt={days[i]:days[i+1] for i in range(len(days)-1)};prv={days[i]:days[i-1] for i in range(1,len(days))}
    day_rows={};events=[]
    for code,bs in bars.items():
        name=meta[code];streak=0
        for i in range(1,len(bs)):
            if lim(bs[i],bs[i-1],name):streak+=1
            else:streak=0
            if streak>0:day_rows.setdefault(bs[i]["date"],[]).append((code,streak))
            if streak==1 and len(bs[:i])>=MIN_HISTORY:events.append((bs[i]["date"],code,name,i,bs))
    lookup={d:dict(v) for d,v in day_rows.items()};market={d:{"zt":len(v),"first":sum(s==1 for _,s in v),"two":sum(s>=2 for _,s in v)} for d,v in day_rows.items()}
    rows=[]
    for d,code,name,i,bs in events:
        nd=nxt.get(d)
        if not nd:continue
        pd=prv.get(d);pm=market.get(pd,{"first":0,"two":0}) if pd else {"first":0,"two":0}
        prev_first=[c for c,s in day_rows.get(pd,[]) if s==1] if pd else []
        prate=sum(lookup.get(d,{}).get(c,0)>=2 for c in prev_first)/len(prev_first) if prev_first else 0
        r={"date":d,"next_date":nd,"code":code,"name":name,"label":int(lookup.get(nd,{}).get(code,0)>=2),
           "market_first_count":market.get(d,{}).get("first",0),"market_2plus_count":market.get(d,{}).get("two",0),"market_zt_count":market.get(d,{}).get("zt",0),
           "prev_market_first_count":pm["first"],"prev_market_2plus_count":pm["two"],"prev_market_1to2_rate":prate}
        r.update(base_feats(bs,i));r.update(struct_feats(bs,i));rows.append(r)
    rows.sort(key=lambda x:(x["date"],x["code"]));dates=sorted({r["date"] for r in rows});split=dates[max(1,int(len(dates)*.7)-1)]
    tr=[r for r in rows if r["date"]<=split];te=[r for r in rows if r["date"]>split]
    m0=LR();m0.fit([[r[k] for k in BASE] for r in tr],[r["label"] for r in tr]);s0=m0.predict([[r[k] for k in BASE] for r in te]);b0,d0=top1(te,s0)
    m1=LR();m1.fit([[r[k] for k in ALL] for r in tr],[r["label"] for r in tr]);s1=m1.predict([[r[k] for k in ALL] for r in te]);b1,d1=top1(te,s1)
    report={"version":"round2-daily-proxy-v1","period":[a.start,a.end],"first_board_events":len(rows),"split_date":split,
      "baseline":{"train_n":len(tr),"test_n":len(te),"top1_precision":b0,"daily_hit_rate":d0},
      "enhanced":{"train_n":len(tr),"test_n":len(te),"top1_precision":b1,"daily_hit_rate":d1},
      "delta_pp":(b1-b0)*100,"structure_conditions_test":cond(te,[
        ("open_gap>=8.5%",lambda r:r["open_gap_pct"]>=8.5),("open_gap 5-8.5%",lambda r:5<=r["open_gap_pct"]<8.5),
        ("open_gap<5%",lambda r:r["open_gap_pct"]<5),("lower_wick_ratio>=0.5",lambda r:r["lower_wick_ratio"]>=.5),
        ("lower_wick_ratio<0.5",lambda r:r["lower_wick_ratio"]<.5),("intraday_pullback<=3%",lambda r:r["intraday_pullback_pct"]<=3),
        ("intraday_pullback>3%",lambda r:r["intraday_pullback_pct"]>3),("turnover_vs_20d>=1.5",lambda r:r["turnover_vs_20d"]>=1.5),
        ("volume_vs_20d>=1.5",lambda r:r["volume_vs_20d"]>=1.5),("amplitude_vs_20d>=1.5",lambda r:r["amplitude_vs_20d"]>=1.5),
      ]),
      "note":"这是日K封板结构代理，不等同于盘中首次触板时间、炸板次数、回封次数和封单金额；分钟级回测仍单独运行。"}
    OUT.mkdir(parents=True,exist_ok=True);(OUT/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    fields=["date","next_date","code","name","label"]+BASE+STRUCT
    with (OUT/"all_event_features.csv").open("w",encoding="utf-8-sig",newline="") as fh:
        w=csv.DictWriter(fh,fieldnames=fields);w.writeheader()
        for r in rows:w.writerow({k:r.get(k,0) for k in fields})
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__":main()
