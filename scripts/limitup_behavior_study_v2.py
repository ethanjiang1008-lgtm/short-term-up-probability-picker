"""独立A股连板启动前行为研究 V2。
核心原则：先收集原始数据，再从数据中发现共同现象；绝不预先限定“换手率>=X”之类的研究条件。
本脚本只做历史研究，不参与任何选股工作流。
"""
from __future__ import annotations
import json, math, statistics, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from market_data_sina import fetch_all_stocks, is_main_board

LOOKBACK_DAYS=370; KLINE_COUNT=320; WORKERS=10; MIN_HISTORY=61; MAX_PRESTART_DAYS=20; COMMON_COVERAGE=.80
OUTPUT=Path('data/limitup_behavior_study_v2')
EM_URL='https://push2his.eastmoney.com/api/qt/stock/kline/get'; FIELDS1='f1,f2,f3,f4,f5,f6'; FIELDS2='f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'

def num(x:Any, default=None):
    try:
        v=float(x); return v if math.isfinite(v) else default
    except (TypeError,ValueError): return default

def pct(a,b): return (a/b-1)*100 if b else None

def http_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
    last=None
    for wait in (0,1,2):
        if wait: time.sleep(wait)
        try:
            with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read().decode('utf-8'))
        except Exception as e: last=e
    raise RuntimeError(last)

def fetch_kline(code):
    market=1 if code.startswith('6') else 0
    url=f'{EM_URL}?secid={market}.{code}&fields1={FIELDS1}&fields2={FIELDS2}&klt=101&fqt=0&beg=0&end=20500101&lmt={KLINE_COUNT}'
    payload=http_json(url); out=[]
    for s in ((payload.get('data') or {}).get('klines') or []):
        p=s.split(',')
        if len(p)<11: continue
        try: out.append({'date':p[0][:10],'open':float(p[1]),'close':float(p[2]),'high':float(p[3]),'low':float(p[4]),'volume':float(p[5]),'amount':float(p[6]),'amplitude':float(p[7]),'pct':float(p[8]),'change':float(p[9]),'turnover':float(p[10])})
        except (TypeError,ValueError): pass
    return out

def clean_name(name): return ''.join(str(name or '').upper().split()).replace('*','')

def is_limit_up(bar,prev,name):
    clean=clean_name(name)
    if clean.startswith('ST') or '退' in clean: return False
    p=num(prev.get('close'),0) or 0; c=num(bar.get('close'),0) or 0
    return p>0 and c/p-1>=.095

def find_start_events(bars,name,cutoff):
    bs=[b for b in bars if b['date']>=cutoff]; out=[]; i=1
    while i<len(bs):
        if is_limit_up(bs[i],bs[i-1],name):
            st=i; j=i+1
            while j<len(bs) and is_limit_up(bs[j],bs[j-1],name): j+=1
            if j-st>=2 and st>=MIN_HISTORY: out.append((st,j-st))
            i=j
        else: i+=1
    return out

def mean(xs): return statistics.fmean(xs) if xs else None

def snapshot(bars,start_idx,d,current_mcap,current_price):
    idx=start_idx-d
    if idx<MIN_HISTORY or idx>=len(bars): return None
    pre=bars[:idx+1]; c=[x['close'] for x in pre]; v=[x['volume'] for x in pre]; a=[x['amount'] for x in pre]; tr=[x['turnover'] for x in pre]; day=pre[-1]
    ma5,ma10,ma20,ma60=mean(c[-5:]),mean(c[-10:]),mean(c[-20:]),mean(c[-60:]); vol3,vol5,vol10,vol20=mean(v[-3:]),mean(v[-5:]),mean(v[-10:]),mean(v[-20:]); amt5,amt10,amt20=mean(a[-5:]),mean(a[-10:]),mean(a[-20:]); tr3,tr5,tr10,tr20=mean(tr[-3:]),mean(tr[-5:]),mean(tr[-10:]),mean(tr[-20:]); high20,high60=max(c[-20:]),max(c[-60:]); low20=min(c[-20:]); price=c[-1]
    est_mcap=current_mcap*price/current_price if current_mcap and current_price else None
    return {'distance_to_start_days':d,'date':day['date'],'open':day['open'],'high':day['high'],'low':day['low'],'close':price,'return_1d_pct':pct(price,c[-2]),'return_3d_pct':pct(price,c[-4]),'return_5d_pct':pct(price,c[-6]),'return_10d_pct':pct(price,c[-11]),'return_20d_pct':pct(price,c[-21]),'volume':v[-1],'amount':a[-1],'turnover_pct':tr[-1],'volume_ratio_3d':v[-1]/vol3 if vol3 else None,'volume_ratio_5d':v[-1]/vol5 if vol5 else None,'volume_ratio_10d':v[-1]/vol10 if vol10 else None,'volume_ratio_20d':v[-1]/vol20 if vol20 else None,'amount_ratio_5d':a[-1]/amt5 if amt5 else None,'amount_ratio_10d':a[-1]/amt10 if amt10 else None,'amount_ratio_20d':a[-1]/amt20 if amt20 else None,'turnover_avg_3d_pct':tr3,'turnover_avg_5d_pct':tr5,'turnover_avg_10d_pct':tr10,'turnover_avg_20d_pct':tr20,'turnover_ratio_5_vs_20':tr5/tr20 if tr20 else None,'close_above_ma5':price>ma5 if ma5 is not None else None,'close_above_ma10':price>ma10 if ma10 is not None else None,'close_above_ma20':price>ma20 if ma20 is not None else None,'ma5_gt_ma10':ma5>ma10 if ma5 is not None and ma10 is not None else None,'ma10_gt_ma20':ma10>ma20 if ma10 is not None and ma20 is not None else None,'ma20_gt_ma60':ma20>ma60 if ma20 is not None and ma60 is not None else None,'bull_alignment':ma5>ma10>ma20>ma60 if None not in (ma5,ma10,ma20,ma60) else None,'distance_high20_pct':pct(price,high20),'distance_high60_pct':pct(price,high60),'distance_low20_pct':pct(price,low20),'amplitude_pct':day['amplitude'],'close_position_in_day':(price-day['low'])/(day['high']-day['low']) if day['high']>day['low'] else .5,'up_days_5':sum(c[i]>c[i-1] for i in range(len(c)-5,len(c))),'volatility_10d_pct':statistics.pstdev(c[-10:])/price*100 if len(c)>=10 and price else None,'estimated_historical_float_mcap_billion':est_mcap}

def quantile(xs,p):
    xs=sorted(x for x in xs if x is not None and math.isfinite(x))
    if not xs:return None
    k=(len(xs)-1)*p; lo,hi=math.floor(k),math.ceil(k)
    return xs[lo] if lo==hi else xs[lo]+(xs[hi]-xs[lo])*(k-lo)

def narrowest_interval(xs,target=.80):
    xs=sorted(x for x in xs if x is not None and math.isfinite(x)); n=len(xs)
    if n<30:return None
    width_n=math.ceil(n*target); best=None
    for i in range(n-width_n+1):
        lo,hi=xs[i],xs[i+width_n-1]; cand=(hi-lo,lo,hi)
        if best is None or cand<best: best=cand
    _,lo,hi=best; matched=sum(lo<=x<=hi for x in xs)
    return {'type':'data_driven_interval','lower':lo,'upper':hi,'matched':matched,'total':n,'coverage':matched/n,'width':hi-lo}

def main():
    cutoff=(date.today()-timedelta(days=LOOKBACK_DAYS)).isoformat(); universe=[r for r in fetch_all_stocks() if is_main_board(str(r.get('code','')),str(r.get('name','')))]; meta={str(r['code']):r for r in universe}; all_bars={}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fs={ex.submit(fetch_kline,c):c for c in meta}
        for i,f in enumerate(as_completed(fs),1):
            code=fs[f]
            try:
                bars=sorted([b for b in f.result() if b['date']>=cutoff],key=lambda x:x['date'])
                if bars: all_bars[code]=bars
            except Exception as e: print(f'fetch_failed={code} {e}')
            if i%200==0: print(f'progress={i}/{len(meta)}')
    events=[]; snapshots=[]
    for code,bars in all_bars.items():
        info=meta[code]; name=str(info.get('name','')); cp=num(info.get('price')); cm=(num(info.get('circ_mcap'))/100000) if num(info.get('circ_mcap')) else None
        for idx,streak in find_start_events(bars,name,cutoff):
            start=bars[idx]['date']; events.append({'code':code,'name':name,'start_date':start,'consecutive_limit_up':streak,'start_close':bars[idx]['close']})
            for d in range(1,MAX_PRESTART_DAYS+1):
                row=snapshot(bars,idx,d,cm,cp)
                if row: row.update({'code':code,'name':name,'start_date':start}); snapshots.append(row)
    dedup={(e['code'],e['start_date']):e for e in events}; events=list(dedup.values())
    by_distance={d:[r for r in snapshots if r['distance_to_start_days']==d] for d in range(1,MAX_PRESTART_DAYS+1)}
    numeric=[k for k in snapshots[0] if k not in {'distance_to_start_days','code','name','start_date'} and isinstance(snapshots[0].get(k),(int,float))] if snapshots else []
    bools=['close_above_ma5','close_above_ma10','close_above_ma20','ma5_gt_ma10','ma10_gt_ma20','ma20_gt_ma60','bull_alignment']
    distributions={}; common={}
    for d,rows in by_distance.items():
        distributions[str(d)]={}; common[str(d)]=[]
        for feature in numeric:
            vals=[num(r.get(feature)) for r in rows if num(r.get(feature)) is not None]
            if len(vals)>=30:
                distributions[str(d)][feature]={'count':len(vals),'p10':quantile(vals,.1),'p25':quantile(vals,.25),'median':quantile(vals,.5),'p75':quantile(vals,.75),'p90':quantile(vals,.9)}
                it=narrowest_interval(vals)
                if it: common[str(d)].append({'feature':feature,**it})
        for feature in bools:
            vals=[r.get(feature) for r in rows if r.get(feature) is not None]
            if len(vals)>=30:
                t=sum(bool(x) for x in vals); f=len(vals)-t; cov=max(t,f)/len(vals)
                if cov>=COMMON_COVERAGE: common[str(d)].append({'feature':feature,'type':'boolean_state','state':t>=f,'matched':max(t,f),'total':len(vals),'coverage':cov})
        common[str(d)].sort(key=lambda x:(-x['coverage'],x['feature']))
    report={'version':'standalone-V2','analysis_date':date.today().isoformat(),'cutoff_date':cutoff,'universe_stocks':len(universe),'stocks_with_data':len(all_bars),'limitup_events':len(events),'unique_limitup_stocks':len({e['code'] for e in events}),'purpose':'数据先行：先输出过去一年所有连板启动事件的逐日原始数据，再从样本分布自动发现覆盖率>=80%的共同现象。','data_collection':{'universe':'existing Sina full-market stock list','historical_daily':'Eastmoney daily OHLCV/amount/turnover','current_snapshot':'Sina current price/current float market cap'},'common_phenomena_rule':'数值特征寻找能够覆盖80%样本的最窄区间；不预先指定业务阈值。布尔特征仅报告覆盖率>=80%的单一状态。','by_distance':{str(d):{'sample_count':len(by_distance[d]),'distribution':distributions[str(d)],'common_phenomena_80pct':common[str(d)]} for d in range(1,MAX_PRESTART_DAYS+1)},'notes':['T-1表示启动日前一个交易日，T-2表示前两个交易日，以此类推。','原始逐日快照完整保存，分析结论不覆盖原始数据。','量比为历史日频量能比值，不冒充盘中实时量比。','历史流通市值为估算值，仅作为研究维度。','本工作流完全独立，不参与现有选股逻辑。']}
    OUTPUT.mkdir(parents=True,exist_ok=True)
    (OUTPUT/'limitup_events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUTPUT/'raw_startup_snapshots.json').write_text(json.dumps(snapshots,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUTPUT/'behavior_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'events':len(events),'snapshots':len(snapshots),'t1_common':common.get('1',[])[:10]},ensure_ascii=False,indent=2))

if __name__=='__main__': main()
