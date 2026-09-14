"""过去一年连板股票启动前综合特征挖掘器。

核心目标：不是训练预测模型，而是还原“后来出现>=2连板的股票，在第一板启动前
到底有什么共同特征”。数据严格截至启动日前一个交易日，避免未来函数。

数据：
- 股票池、当前流通市值：复用 scripts/market_data_sina.py
- 历史 OHLCV、成交额、历史换手率：东方财富公开日K
- 历史估算流通市值：当前流通股本 * 历史收盘价（注明为估算）
- 量比：以启动日前各日成交量 / 前5日平均成交量计算收盘版量比

输出：
- 全部连板启动事件及启动前特征
- 正样本与同日随机对照组
- 单因子覆盖率、正负样本差异、lift
- 启动前1/3/5/10日的典型区间
- 最终“共同特征画像”，不强制80%单一条件
"""
from __future__ import annotations

import json
import math
import random
import statistics
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from market_data_sina import fetch_all_stocks, is_main_board

LOOKBACK_DAYS = 370
KLINE_COUNT = 420
MIN_CONSECUTIVE = 2
WORKERS = 10
CONTROL_PER_EVENT = 2
RANDOM_SEED = 20260914
DATA = Path("data/pattern_mining")

EM_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FIELDS1 = "f1,f2,f3,f4,f5,f6"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b else 0.0


def is_st(name: str) -> bool:
    s = "".join(str(name or "").strip().upper().split()).replace("*", "")
    return s.startswith("ST") or "退" in s


def http_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last = None
    for wait in (0, 1, 2):
        if wait:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            last = exc
    raise RuntimeError(f"request failed: {last}")


def fetch_kline(code: str) -> list[dict[str, Any]]:
    market = 1 if code.startswith("6") else 0
    url = (
        f"{EM_URL}?secid={market}.{code}&fields1={FIELDS1}&fields2={FIELDS2}"
        f"&klt=101&fqt=0&beg=0&end=20500101&lmt={KLINE_COUNT}"
    )
    payload = http_json(url)
    items = ((payload.get("data") or {}).get("klines")) or []
    out = []
    for item in items:
        p = item.split(",") if isinstance(item, str) else []
        if len(p) < 11:
            continue
        try:
            out.append({
                "date": p[0][:10], "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                "amount": float(p[6]), "amplitude": float(p[7]), "pct": float(p[8]),
                "change": float(p[9]), "turnover": float(p[10]),
            })
        except (ValueError, TypeError):
            continue
    return out


def limit_up(bar: dict[str, Any], prev: dict[str, Any], name: str) -> bool:
    if is_st(name):
        return False
    pc, c = f(prev.get("close")), f(bar.get("close"))
    return pc > 0 and c > 0 and (c / pc - 1) >= 0.095


def consecutive_events(bars: list[dict[str, Any]], name: str, cutoff: str) -> list[int]:
    bs = [b for b in bars if str(b.get("date", "")) >= cutoff]
    events = []
    i = 1
    while i < len(bs):
        if limit_up(bs[i], bs[i - 1], name):
            start = i
            j = i + 1
            while j < len(bs) and limit_up(bs[j], bs[j - 1], name):
                j += 1
            if j - start >= MIN_CONSECUTIVE and start >= 25:
                events.append(start)
            i = j
        else:
            i += 1
    return events


def avg(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def snapshot(bs: list[dict[str, Any]], idx: int, lag: int) -> dict[str, Any]:
    """在启动日 idx 的 T-lag 日提取横截面与短周期特征。"""
    k = idx - lag
    if k < 20:
        return {}
    b = bs[k]
    prior = bs[: k + 1]
    closes = [f(x.get("close")) for x in prior]
    volumes = [f(x.get("volume")) for x in prior]
    amounts = [f(x.get("amount")) for x in prior]
    turnovers = [f(x.get("turnover")) for x in prior]
    highs = [f(x.get("high")) for x in prior]
    lows = [f(x.get("low")) for x in prior]
    c = closes[-1]
    vol5_prev = avg(volumes[-6:-1]) if len(volumes) >= 6 else 0
    vol10_prev = avg(volumes[-11:-1]) if len(volumes) >= 11 else 0
    amt5_prev = avg(amounts[-6:-1]) if len(amounts) >= 6 else 0
    amt10_prev = avg(amounts[-11:-1]) if len(amounts) >= 11 else 0
    turnover5 = avg(turnovers[-5:])
    turnover10 = avg(turnovers[-10:])
    turnover20 = avg(turnovers[-20:])
    high20 = max(highs[-20:])
    high60 = max(highs[-60:]) if len(highs) >= 60 else high20
    low20 = min(lows[-20:])
    return {
        "date": str(b.get("date", "")),
        "close": c,
        "ret_1d": pct(c, closes[-2]) if len(closes) >= 2 else 0,
        "ret_3d": pct(c, closes[-4]) if len(closes) >= 4 else 0,
        "ret_5d": pct(c, closes[-6]) if len(closes) >= 6 else 0,
        "ret_10d": pct(c, closes[-11]) if len(closes) >= 11 else 0,
        "ret_20d": pct(c, closes[-21]) if len(closes) >= 21 else 0,
        "volume": volumes[-1],
        "amount": amounts[-1],
        "turnover_rate": turnovers[-1],
        "turnover_5d_avg": turnover5,
        "turnover_10d_avg": turnover10,
        "turnover_20d_avg": turnover20,
        "turnover_5_vs_20": turnover5 / turnover20 if turnover20 > 0 else 0,
        "volume_ratio_5d": volumes[-1] / vol5_prev if vol5_prev > 0 else 0,
        "volume_ratio_10d": volumes[-1] / vol10_prev if vol10_prev > 0 else 0,
        "amount_ratio_5d": amounts[-1] / amt5_prev if amt5_prev > 0 else 0,
        "amount_ratio_10d": amounts[-1] / amt10_prev if amt10_prev > 0 else 0,
        "close_above_ma5": c > avg(closes[-5:]),
        "close_above_ma10": c > avg(closes[-10:]),
        "close_above_ma20": c > avg(closes[-20:]),
        "ma5_gt_ma10": avg(closes[-5:]) > avg(closes[-10:]),
        "ma10_gt_ma20": avg(closes[-10:]) > avg(closes[-20:]),
        "near_high20_pct": pct(c, high20),
        "near_high60_pct": pct(c, high60),
        "above_low20_pct": pct(c, low20),
        "range_pct": (highs[-1] - lows[-1]) / c * 100 if c else 0,
        "close_position": (c - lows[-1]) / (highs[-1] - lows[-1]) if highs[-1] > lows[-1] else .5,
        "up_days_5": sum(closes[z] > closes[z-1] for z in range(len(closes)-5, len(closes))),
        "volatility_10d": statistics.pstdev(closes[-10:]) / c * 100 if len(closes) >= 10 and c else 0,
        "estimated_float_mcap_billion": None,
    }


def event_row(code: str, name: str, bars: list[dict[str, Any]], idx: int, float_shares: float | None, label: int) -> dict[str, Any] | None:
    if idx < 25:
        return None
    row = {
        "code": code,
        "name": name,
        "start_date": str(bars[idx].get("date", "")),
        "label": label,
        "pre_days": {},
    }
    for lag in (1, 3, 5, 10):
        s = snapshot(bars, idx, lag)
        if not s:
            return None
        if float_shares and s["close"] > 0:
            s["estimated_float_mcap_billion"] = s["close"] * float_shares / 1e8
        row["pre_days"][f"T-{lag}"] = s
    t1 = row["pre_days"]["T-1"]
    # 用T-1作为主分析横截面，同时保留不同提前期。
    for k, v in t1.items():
        if k != "date":
            row[f"t1_{k}"] = v
    return row


def quantile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    k = (len(xs)-1)*p
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi]-xs[lo])*(k-lo)


def feature_stats(pos: list[dict[str, Any]], ctl: list[dict[str, Any]], prefix: str, feature: str) -> dict[str, Any] | None:
    pv = [f(r.get(prefix + feature), math.nan) for r in pos]
    cv = [f(r.get(prefix + feature), math.nan) for r in ctl]
    pv = [x for x in pv if math.isfinite(x)]
    cv = [x for x in cv if math.isfinite(x)]
    if len(pv) < 30 or len(cv) < 30:
        return None
    p50, c50 = quantile(pv,.5), quantile(cv,.5)
    p75 = quantile(pv,.75)
    upper = sum(x >= p75 for x in pv) / len(pv)
    ctl_upper = sum(x >= p75 for x in cv) / len(cv)
    return {
        "feature": feature,
        "positive_p10": round(quantile(pv,.10),4),
        "positive_p25": round(quantile(pv,.25),4),
        "positive_median": round(p50,4),
        "positive_p75": round(p75,4),
        "positive_p90": round(quantile(pv,.90),4),
        "control_median": round(c50,4),
        "median_delta": round(p50-c50,4),
        "positive_above_p75_coverage": round(upper,4),
        "control_above_positive_p75": round(ctl_upper,4),
        "lift_at_positive_p75": round(upper / ctl_upper,4) if ctl_upper > 0 else 999.0,
    }


def main() -> None:
    today = date.today()
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    universe = [r for r in fetch_all_stocks() if is_main_board(str(r.get("code","")), str(r.get("name",""))) and not is_st(str(r.get("name","")))]
    names = {str(r.get("code")): str(r.get("name")) for r in universe}
    # nmc为万元口径；由当前市值/价格反推当前流通股本，再估算历史流通市值。
    float_shares = {}
    for r in universe:
        price = f(r.get("price"))
        nmc = f(r.get("nmc"))
        if price > 0 and nmc > 0:
            float_shares[str(r.get("code"))] = nmc * 10000 / price

    all_rows: dict[str,list[dict[str,Any]]] = {}
    def work(code: str):
        try:
            return code, fetch_kline(code)
        except Exception as exc:
            print(f"failed={code} {exc}")
            return code, []
    codes = list(names)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(work,c) for c in codes]
        for i, fut in enumerate(as_completed(futures),1):
            code,bars=fut.result()
            if bars:
                bars=[b for b in bars if str(b.get("date","")) >= cutoff]
                bars.sort(key=lambda x:str(x.get("date","")))
                all_rows[code]=bars
            if i % 200 == 0:
                print(f"progress={i}/{len(codes)}")

    pos=[]
    for code,bars in all_rows.items():
        name=names.get(code,"")
        for idx in consecutive_events(bars,name,cutoff):
            row=event_row(code,name,bars,idx,float_shares.get(code),1)
            if row: pos.append(row)
    dedup={(r["code"],r["start_date"]):r for r in pos}
    pos=list(dedup.values())

    # 同日对照：每个连板启动事件随机取2只不存在该事件的主板股。
    by_date=defaultdict(list)
    for code,bars in all_rows.items():
        for idx in range(25,len(bars)):
            by_date[str(bars[idx].get("date"))].append((code,idx))
    pos_keys=set(dedup)
    rng=random.Random(RANDOM_SEED)
    ctl=[]; used=set()
    for e in pos:
        candidates=list(by_date.get(e["start_date"],[])); rng.shuffle(candidates)
        n=0
        for code,idx in candidates:
            key=(code,e["start_date"])
            if code==e["code"] or key in pos_keys or key in used:
                continue
            row=event_row(code,names.get(code,""),all_rows[code],idx,float_shares.get(code),0)
            if row:
                row["start_date"]=e["start_date"]
                ctl.append(row); used.add(key); n+=1
            if n>=CONTROL_PER_EVENT: break

    # 主分析字段：t1_...；补充跨提前期稳定性。
    features=[
        "close","ret_1d","ret_3d","ret_5d","ret_10d","ret_20d","volume","amount",
        "turnover_rate","turnover_5d_avg","turnover_10d_avg","turnover_20d_avg","turnover_5_vs_20",
        "volume_ratio_5d","volume_ratio_10d","amount_ratio_5d","amount_ratio_10d",
        "near_high20_pct","near_high60_pct","above_low20_pct","range_pct","close_position","up_days_5",
        "volatility_10d","estimated_float_mcap_billion",
    ]
    stats=[]
    for feat in features:
        s=feature_stats(pos,ctl,"t1_",feat)
        if s: stats.append(s)
    stats.sort(key=lambda x:(-x["lift_at_positive_p75"],-abs(x["median_delta"])))

    # 每个提前期统计方向是否与T-1一致，判断规律是不是“启动前逐步形成”。
    progression={}
    for feat in features:
        med={}
        for lag in (1,3,5,10):
            key=f"pre_days"
            vals=[f(r[key][f"T-{lag}"].get(feat),math.nan) for r in pos]
            vals=[x for x in vals if math.isfinite(x)]
            if vals: med[f"T-{lag}"]=round(quantile(vals,.5),4)
        progression[feat]=med

    common=[]
    for s in stats:
        # 重点找覆盖不低且对照明显低的特征，不强制80%。
        cov=s["positive_above_p75_coverage"]
        ctlcov=s["control_above_positive_p75"]
        if cov >= .25 and s["lift_at_positive_p75"] >= 1.5:
            common.append(s)
    report={
        "version":"comprehensive-V1",
        "analysis_date":today.isoformat(),
        "cutoff_date":cutoff,
        "positive_samples":len(pos),
        "unique_positive_stocks":len({r["code"] for r in pos}),
        "control_samples":len(ctl),
        "common_features":common[:30],
        "feature_stats":stats,
        "median_progression_before_start":progression,
        "data_sources":{
            "universe_and_current_float_cap":"Sina existing market-data adapter",
            "historical_daily":"Eastmoney public daily K-line",
            "historical_turnover":"Eastmoney daily K-line turnover field",
            "historical_float_cap":"estimated from current float shares x historical close; approximate",
            "volume_ratio":"calculated from historical volume / previous 5 or 10 day average volume",
            "amount_ratio":"calculated from historical amount / previous 5 or 10 day average amount",
        },
        "interpretation":"这些结果用于发现连板启动前共同画像，不直接作为买入信号；正式接入选股器前应再做滚动样本外回测。",
    }
    DATA.mkdir(parents=True,exist_ok=True)
    (DATA/"latest_comprehensive_event_rows.json").write_text(json.dumps(pos,ensure_ascii=False,indent=2),encoding="utf-8")
    (DATA/"latest_comprehensive_control_rows.json").write_text(json.dumps(ctl,ensure_ascii=False,indent=2),encoding="utf-8")
    (DATA/"latest_comprehensive_pattern_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__ == "__main__":
    main()
