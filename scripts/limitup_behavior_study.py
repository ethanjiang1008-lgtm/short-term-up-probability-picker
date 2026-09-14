"""独立的A股连板启动前行为研究器。仅做历史分析，不参与任何选股逻辑。"""
from __future__ import annotations

import json
import math
import random
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from market_data_sina import fetch_all_stocks, is_main_board

LOOKBACK_DAYS = 370
KLINE_COUNT = 320
WORKERS = 10
MIN_BARS = 61
OUTPUT = Path("data/limitup_behavior_study")
EM_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FIELDS1 = "f1,f2,f3,f4,f5,f6"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
DISTANCES = (1, 3, 5, 10, 20)


def num(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def pct(a, b):
    return (a / b - 1.0) * 100.0 if b else 0.0


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last = None
    for wait in (0, 1, 2):
        if wait:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
    raise RuntimeError(last)


def fetch_detailed_kline(code):
    market = 1 if code.startswith("6") else 0
    url = (f"{EM_URL}?secid={market}.{code}&fields1={FIELDS1}&fields2={FIELDS2}"
           f"&klt=101&fqt=0&beg=0&end=20500101&lmt={KLINE_COUNT}")
    payload = http_json(url)
    rows = []
    for s in ((payload.get("data") or {}).get("klines") or []):
        p = s.split(",")
        if len(p) < 11:
            continue
        try:
            rows.append({
                "date": p[0][:10], "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                "amount": float(p[6]), "amplitude": float(p[7]),
                "pct": float(p[8]), "change": float(p[9]), "turnover": float(p[10])
            })
        except (TypeError, ValueError):
            pass
    return rows


def limit_up(bar, prev, name):
    clean = "".join(str(name or "").upper().split()).replace("*", "")
    if clean.startswith("ST") or "退" in clean:
        return False
    return num(prev.get("close")) > 0 and num(bar.get("close")) / num(prev.get("close")) - 1 >= 0.095


def find_events(bars, name, cutoff):
    bs = [b for b in bars if b["date"] >= cutoff]
    out = []
    i = 1
    while i < len(bs):
        if limit_up(bs[i], bs[i - 1], name):
            st = i
            j = i + 1
            while j < len(bs) and limit_up(bs[j], bs[j - 1], name):
                j += 1
            if j - st >= 2 and st >= MIN_BARS:
                out.append({"idx": st, "start_date": bs[st]["date"], "consecutive_limit_up": j - st})
            i = j
        else:
            i += 1
    return out


def avg(xs):
    return statistics.fmean(xs) if xs else None


def feature_snapshot(bars, idx, distance, current_circ_mcap, current_price):
    t = idx - distance
    if t < 60:
        return None
    pre = bars[:t + 1]
    c = [x["close"] for x in pre]
    v = [x["volume"] for x in pre]
    a = [x["amount"] for x in pre]
    tr = [x["turnover"] for x in pre]
    day = pre[-1]
    ma5, ma10, ma20, ma60 = avg(c[-5:]), avg(c[-10:]), avg(c[-20:]), avg(c[-60:])
    vol5, vol10, vol20 = avg(v[-5:]), avg(v[-10:]), avg(v[-20:])
    amt5, amt10, amt20 = avg(a[-5:]), avg(a[-10:]), avg(a[-20:])
    tr5, tr10, tr20 = avg(tr[-5:]), avg(tr[-10:]), avg(tr[-20:])
    high20, high60 = max(c[-20:]), max(c[-60:])
    low20 = min(c[-20:])
    hist_mcap = current_circ_mcap * day["close"] / current_price if current_price > 0 and current_circ_mcap else None
    return {
        "distance_to_start_days": distance,
        "date": day["date"], "price": day["close"], "open": day["open"], "high": day["high"], "low": day["low"],
        "return_1d": pct(c[-1], c[-2]), "return_3d": pct(c[-1], c[-4]), "return_5d": pct(c[-1], c[-6]),
        "return_10d": pct(c[-1], c[-11]), "return_20d": pct(c[-1], c[-21]),
        "volume": v[-1], "amount": a[-1], "turnover": tr[-1],
        "volume_ratio_5d": v[-1] / vol5 if vol5 else None,
        "volume_ratio_10d": v[-1] / vol10 if vol10 else None,
        "volume_ratio_20d": v[-1] / vol20 if vol20 else None,
        "amount_ratio_5d": a[-1] / amt5 if amt5 else None,
        "amount_ratio_10d": a[-1] / amt10 if amt10 else None,
        "amount_ratio_20d": a[-1] / amt20 if amt20 else None,
        "turnover_avg_3d": avg(tr[-3:]), "turnover_avg_5d": tr5, "turnover_avg_10d": tr10, "turnover_avg_20d": tr20,
        "turnover_ratio_5_vs_20": tr5 / tr20 if tr20 else None,
        "close_above_ma5": c[-1] > ma5, "close_above_ma10": c[-1] > ma10, "close_above_ma20": c[-1] > ma20,
        "ma5_gt_ma10": ma5 > ma10, "ma10_gt_ma20": ma10 > ma20, "ma20_gt_ma60": ma20 > ma60,
        "bull_alignment": ma5 > ma10 > ma20 > ma60,
        "distance_high20_pct": pct(c[-1], high20), "distance_high60_pct": pct(c[-1], high60),
        "distance_low20_pct": pct(c[-1], low20),
        "range_pct": (day["high"] - day["low"]) / c[-1] * 100 if c[-1] else 0,
        "close_position": (c[-1] - day["low"]) / (day["high"] - day["low"]) if day["high"] > day["low"] else 0.5,
        "up_days_5": sum(c[i] > c[i - 1] for i in range(len(c) - 5, len(c))),
        "volatility_10d_pct": statistics.pstdev(c[-10:]) / c[-1] * 100 if len(c) >= 10 else None,
        "estimated_historical_float_mcap_billion": hist_mcap,
    }


def quantile(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(rows):
    numeric = [k for k, v in rows[0].items() if isinstance(v, (int, float)) and k not in ("distance_to_start_days",)]
    summary = {}
    for k in numeric:
        vals = [r.get(k) for r in rows if isinstance(r.get(k), (int, float))]
        if vals:
            summary[k] = {"count": len(vals), "median": quantile(vals, .5), "p10": quantile(vals, .1), "p90": quantile(vals, .9)}
    return summary


def common_conditions(rows):
    tests = [
        ("turnover>=2%", lambda r: num(r.get("turnover")) >= 2),
        ("turnover>=3%", lambda r: num(r.get("turnover")) >= 3),
        ("turnover>=5%", lambda r: num(r.get("turnover")) >= 5),
        ("volume_ratio_5d>=1.2", lambda r: num(r.get("volume_ratio_5d"), -1) >= 1.2),
        ("volume_ratio_5d>=1.5", lambda r: num(r.get("volume_ratio_5d"), -1) >= 1.5),
        ("volume_ratio_10d>=1.2", lambda r: num(r.get("volume_ratio_10d"), -1) >= 1.2),
        ("turnover_ratio_5_vs_20>=1.2", lambda r: num(r.get("turnover_ratio_5_vs_20"), -1) >= 1.2),
        ("close_above_ma20", lambda r: bool(r.get("close_above_ma20"))),
        ("ma5_gt_ma10", lambda r: bool(r.get("ma5_gt_ma10"))),
        ("ma10_gt_ma20", lambda r: bool(r.get("ma10_gt_ma20"))),
        ("bull_alignment", lambda r: bool(r.get("bull_alignment"))),
        ("within_5pct_high20", lambda r: num(r.get("distance_high20_pct"), -999) >= -5),
        ("within_10pct_high20", lambda r: num(r.get("distance_high20_pct"), -999) >= -10),
        ("return20d>0", lambda r: num(r.get("return_20d"), -999) > 0),
        ("return20d>=10%", lambda r: num(r.get("return_20d"), -999) >= 10),
        ("up_days_5>=3", lambda r: num(r.get("up_days_5"), 0) >= 3),
    ]
    out = []
    for name, fn in tests:
        n = sum(fn(r) for r in rows)
        out.append({"condition": name, "matched": n, "total": len(rows), "coverage": round(n / len(rows), 4) if rows else 0})
    return sorted(out, key=lambda x: (-x["coverage"], x["condition"]))


def main():
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    universe = [r for r in fetch_all_stocks() if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    meta = {str(r["code"]): r for r in universe}
    codes = list(meta)
    all_bars = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fs = {ex.submit(fetch_detailed_kline, c): c for c in codes}
        for i, f in enumerate(as_completed(fs), 1):
            code = fs[f]
            try:
                bars = [b for b in f.result() if b["date"] >= cutoff]
                bars.sort(key=lambda x: x["date"])
                if bars:
                    all_bars[code] = bars
            except Exception as e:
                print(f"fetch_failed={code} {e}")
            if i % 200 == 0:
                print(f"progress={i}/{len(codes)}")

    events, snapshots = [], []
    for code, bars in all_bars.items():
        info = meta.get(code, {})
        name = str(info.get("name", ""))
        evs = find_events(bars, name, cutoff)
        current_price = num(info.get("price"))
        current_mcap = num(info.get("circ_mcap")) / 100000 if num(info.get("circ_mcap")) else None
        for ev in evs:
            event = {"code": code, "name": name, "start_date": ev["start_date"], "consecutive_limit_up": ev["consecutive_limit_up"]}
            events.append(event)
            for d in DISTANCES:
                row = feature_snapshot(bars, ev["idx"], d, current_mcap, current_price)
                if row:
                    row.update({"code": code, "name": name, "start_date": ev["start_date"]})
                    snapshots.append(row)

    by_distance = {d: [r for r in snapshots if r["distance_to_start_days"] == d] for d in DISTANCES}
    reports = {}
    for d, rows in by_distance.items():
        reports[str(d)] = {"sample_count": len(rows), "common_conditions": common_conditions(rows), "distribution": summarize(rows) if rows else {}}

    report = {
        "version": "standalone-V1",
        "analysis_date": date.today().isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "cutoff_date": cutoff,
        "universe_stocks": len(universe),
        "stocks_with_data": len(all_bars),
        "limitup_events": len(events),
        "unique_limitup_stocks": len({e["code"] for e in events}),
        "purpose": "仅分析过去一年2连板及以上股票在首次涨停启动日前的客观行为特征，不参与选股，不修改现有工作流。",
        "data_sources": {"universe_and_current_snapshot": "existing Sina data acquisition layer", "historical_daily_kline": "Eastmoney public daily K-line for OHLCV/amount/turnover"},
        "research_dimensions": ["K线", "涨跌幅", "成交量", "成交额", "历史换手率", "日量比代理", "均线结构", "价格位置", "估算历史流通市值", "启动前T-1/T-3/T-5/T-10/T-20变化"],
        "by_distance": reports,
        "notes": [
            "量比使用日频代理值：当日成交量/过去5、10、20个交易日平均成交量，不冒充盘中实时量比。",
            "历史流通市值为当前流通市值按历史价格比例反推的估算值，不视为真实历史股本口径。",
            "每个连板事件只使用启动日之前的数据；启动日及之后数据绝不进入特征。",
            "本工作流只做事实统计与现象发现，不生成买卖信号。",
        ],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "limitup_events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT / "startup_snapshots.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT / "behavior_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"events": len(events), "unique_stocks": len({e['code'] for e in events}), "samples": {str(k): len(v) for k,v in by_distance.items()}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
