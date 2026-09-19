#!/usr/bin/env python3
"""A股首板 -> 二板历史回测。

目标：
1. 从东方财富历史涨停池恢复指定区间内全部主板首板；
2. 用 T 日收盘时可以观察到的信息构造特征；
3. 用 T+1 是否出现 >=2 连板作为唯一标签；
4. 严格按时间切分训练/测试，输出 baseline、AUC、PR-AUC、Top1/3/5 精度及日命中率；
5. 本脚本只做研究和回测，不修改主分支。

说明：
- 首板：T 日涨停池中连续连板数 lbc == 1；
- 二板成功：T+1 涨停池中同一股票 lbc >= 2；
- 市场/板块信息仅使用 T 日及更早数据；
- 技术特征使用 T-1 及更早日 K；
- 不把模型 score 直接称为可交易概率，除非后续做独立校准。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

ZT_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
UT = "7eea3edcaed734bea9cbfc24409ed989"
OUT = Path("data/backtest_one_to_two")

MAIN_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")


def f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def is_main_board(code: str) -> bool:
    c = str(code or "")
    return c.startswith(MAIN_PREFIXES)


def is_st(name: str) -> bool:
    s = "".join(str(name or "").strip().upper().split()).replace("*", "")
    return s.startswith("ST") or "退" in s


def pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b else 0.0


def parse_hhmmss(v: Any) -> float | None:
    s = str(v or "").strip()
    if not s or s in {"0", "-", "None"}:
        return None
    try:
        n = int(float(s))
        s = f"{n:06d}"[-6:]
        hh, mm, ss = int(s[:2]), int(s[2:4]), int(s[4:6])
        if hh > 23 or mm > 59 or ss > 59:
            return None
        return hh * 60 + mm + ss / 60.0
    except Exception:
        return None


def req_json(url: str, params: dict[str, Any], retries: int = 4, timeout: int = 20) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/ztb/detail",
    }
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if i + 1 < retries:
                time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"request failed: {last}")


def fetch_zt_pool(day: str) -> list[dict[str, Any]]:
    params = {
        "ut": UT,
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": 10000,
        "sort": "fbt:asc",
        "date": day.replace("-", ""),
    }
    payload = req_json(ZT_URL, params)
    data = payload.get("data") or {}
    pool = data.get("pool") or []
    out = []
    for x in pool:
        code = str(x.get("c", "")).zfill(6)
        name = str(x.get("n", ""))
        if not is_main_board(code) or is_st(name):
            continue
        out.append({
            "code": code,
            "name": name,
            "price": f(x.get("p")) / 1000.0,
            "pct": f(x.get("zdp")),
            "amount": f(x.get("amount")),
            "float_mcap": f(x.get("ltsz")),
            "turnover": f(x.get("hs")),
            "seal_fund": f(x.get("fund")),
            "first_time": x.get("fbt"),
            "last_time": x.get("lbt"),
            "open_times": int(f(x.get("zbc"), 0)),
            "lbc": int(f(x.get("lbc"), 0)),
            "sector": str(x.get("hybk") or "").strip() or "UNKNOWN",
        })
    return out


def fetch_kline(code: str) -> list[dict[str, Any]]:
    market = 1 if code.startswith("6") else 0
    params = {
        "secid": f"{market}.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101,
        "fqt": 0,
        "beg": 0,
        "end": 20500101,
        "lmt": 420,
    }
    payload = req_json(KLINE_URL, params, timeout=20)
    items = ((payload.get("data") or {}).get("klines")) or []
    out = []
    for item in items:
        if not isinstance(item, str):
            continue
        p = item.split(",")
        if len(p) < 11:
            continue
        try:
            out.append({
                "date": p[0][:10],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "volume": float(p[5]),
                "amount": float(p[6]),
                "turnover": float(p[10]),
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["date"])
    return out


def pre_t_features(bars: list[dict[str, Any]], t_day: str) -> dict[str, float | int | None]:
    idxs = [i for i, b in enumerate(bars) if b["date"] == t_day]
    if not idxs:
        idx = next((i for i, b in enumerate(bars) if b["date"] >= t_day), len(bars))
        if idx >= len(bars):
            idx = len(bars)
    else:
        idx = idxs[0]
    pre = bars[:idx]
    if len(pre) < 2:
        return {}
    closes = [f(b["close"]) for b in pre]
    volumes = [f(b["volume"]) for b in pre]
    amounts = [f(b["amount"]) for b in pre]
    turnovers = [f(b["turnover"]) for b in pre]
    highs = [f(b["high"]) for b in pre]
    lows = [f(b["low"]) for b in pre]
    c = closes[-1]
    out: dict[str, float | int | None] = {
        "pre_ret_1d": pct(c, closes[-2]) if len(closes) >= 2 else 0.0,
        "pre_ret_3d": pct(c, closes[-4]) if len(closes) >= 4 else 0.0,
        "pre_ret_5d": pct(c, closes[-6]) if len(closes) >= 6 else 0.0,
        "pre_ret_10d": pct(c, closes[-11]) if len(closes) >= 11 else 0.0,
        "pre_ret_20d": pct(c, closes[-21]) if len(closes) >= 21 else 0.0,
        "pre_turnover_1d": turnovers[-1],
        "pre_turnover_5d_avg": statistics.fmean(turnovers[-5:]) if len(turnovers) >= 5 else statistics.fmean(turnovers),
        "pre_turnover_20d_avg": statistics.fmean(turnovers[-20:]) if len(turnovers) >= 20 else statistics.fmean(turnovers),
        "pre_turnover_5_vs_20": (
            (statistics.fmean(turnovers[-5:]) / statistics.fmean(turnovers[-20:]))
            if len(turnovers) >= 20 and statistics.fmean(turnovers[-20:]) > 0
            else 1.0
        ),
        "pre_volume_5_vs_20": (
            (statistics.fmean(volumes[-5:]) / statistics.fmean(volumes[-20:]))
            if len(volumes) >= 20 and statistics.fmean(volumes[-20:]) > 0
            else 1.0
        ),
        "pre_close_above_ma20": int(c > statistics.fmean(closes[-20:])) if len(closes) >= 20 else 0,
        "pre_ma5_gt_ma10": int(statistics.fmean(closes[-5:]) > statistics.fmean(closes[-10:])) if len(closes) >= 10 else 0,
        "pre_ma10_gt_ma20": int(statistics.fmean(closes[-10:]) > statistics.fmean(closes[-20:])) if len(closes) >= 20 else 0,
        "pre_near_high20_pct": pct(c, max(highs[-20:])) if len(highs) >= 20 else 0.0,
        "pre_above_low20_pct": pct(c, min(lows[-20:])) if len(lows) >= 20 else 0.0,
        "pre_up_days_5": sum(closes[z] > closes[z - 1] for z in range(max(1, len(closes) - 5), len(closes))),
        "pre_volatility_10d": (
            statistics.pstdev(closes[-10:]) / c * 100.0
            if len(closes) >= 10 and c > 0
            else 0.0
        ),
    }
    return out


def auc(y_true: list[int], scores: list[float]) -> float | None:
    pairs = sorted(zip(scores, y_true), key=lambda x: x[0])
    pos = sum(y_true)
    neg = len(y_true) - pos
    if pos == 0 or neg == 0:
        return None
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i + 1
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        rank_sum += avg_rank * sum(v for _, v in pairs[i:j] if v == 1)
        i = j
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def pr_auc(y_true: list[int], scores: list[float]) -> float | None:
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    p = sum(y_true)
    if p == 0:
        return None
    tp = 0
    fp = 0
    prev_recall = 0.0
    area = 0.0
    for i in order:
        if y_true[i]:
            tp += 1
        else:
            fp += 1
        recall = tp / p
        precision = tp / (tp + fp)
        area += (recall - prev_recall) * precision
        prev_recall = recall
    return area


def sigmoid(z: float) -> float:
    if z >= 35:
        return 1.0
    if z <= -35:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


class Logistic:
    def __init__(self, l2: float = 1.0, lr: float = 0.05, epochs: int = 1800):
        self.l2 = l2
        self.lr = lr
        self.epochs = epochs
        self.w: list[float] = []
        self.b = 0.0
        self.mean: list[float] = []
        self.std: list[float] = []

    def fit(self, x: list[list[float]], y: list[int]) -> None:
        n = len(x)
        d = len(x[0])
        self.mean = [statistics.fmean(row[j] for row in x) for j in range(d)]
        self.std = []
        for j in range(d):
            vals = [row[j] for row in x]
            s = statistics.pstdev(vals)
            self.std.append(s if s > 1e-9 else 1.0)
        xs = [[(row[j] - self.mean[j]) / self.std[j] for j in range(d)] for row in x]
        self.w = [0.0] * d
        self.b = math.log((sum(y) + 0.5) / (n - sum(y) + 0.5))
        for _ in range(self.epochs):
            grad_w = [0.0] * d
            grad_b = 0.0
            for row, target in zip(xs, y):
                p = sigmoid(self.b + sum(a * b for a, b in zip(self.w, row)))
                err = p - target
                grad_b += err
                for j in range(d):
                    grad_w[j] += err * row[j]
            scale = 1.0 / max(1, n)
            self.b -= self.lr * grad_b * scale
            for j in range(d):
                self.w[j] -= self.lr * (grad_w[j] * scale + self.l2 * self.w[j] / n)

    def predict_proba(self, x: list[list[float]]) -> list[float]:
        out = []
        for row in x:
            z = self.b + sum(
                self.w[j] * ((row[j] - self.mean[j]) / self.std[j])
                for j in range(len(row))
            )
            out.append(sigmoid(z))
        return out


FEATURES = [
    "first_seal_min",
    "last_seal_min",
    "open_times",
    "seal_fund_ratio",
    "turnover",
    "amount_billion",
    "float_mcap_billion",
    "board_duration_min",
    "sector_zt_count",
    "sector_first_count",
    "sector_2plus_count",
    "sector_first_share",
    "market_zt_count",
    "market_first_count",
    "market_2plus_count",
    "prev_day_zt_count",
    "prev_day_first_count",
    "prev_day_2plus_count",
    "prev_day_1to2_rate",
    "pre_ret_5d",
    "pre_ret_20d",
    "pre_turnover_1d",
    "pre_turnover_5d_avg",
    "pre_turnover_5_vs_20",
    "pre_volume_5_vs_20",
    "pre_close_above_ma20",
    "pre_ma5_gt_ma10",
    "pre_ma10_gt_ma20",
    "pre_near_high20_pct",
    "pre_above_low20_pct",
    "pre_up_days_5",
    "pre_volatility_10d",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-16")
    ap.add_argument("--end", default="2026-09-18")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end <= start:
        raise SystemExit("end must be after start")

    all_calendar = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            all_calendar.append(d.isoformat())
        d += timedelta(days=1)

    print(f"fetch limit-up pools: {all_calendar[0]} -> {all_calendar[-1]}, weekdays={len(all_calendar)}")
    pools: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(fetch_zt_pool, day): day for day in all_calendar}
        for i, fut in enumerate(as_completed(futs), 1):
            day = futs[fut]
            try:
                rows = fut.result()
                if rows:
                    pools[day] = rows
            except Exception as exc:
                print(f"zt_pool_failed day={day}: {exc}")
            if i % 25 == 0:
                print(f"zt_pool_progress={i}/{len(all_calendar)}")

    trading_days = sorted(pools)
    if len(trading_days) < 80:
        raise SystemExit(f"too few trading days with data: {len(trading_days)}")

    day_index = {day: i for i, day in enumerate(trading_days)}
    next_day = {day: trading_days[i + 1] for i, day in enumerate(trading_days[:-1])}
    prev_day = {day: trading_days[i - 1] for i, day in enumerate(trading_days) if i > 0}
    pool_by_code = {day: {r["code"]: r for r in rows} for day, rows in pools.items()}

    market_stats: dict[str, dict[str, float]] = {}
    sector_stats: dict[str, dict[str, dict[str, float]]] = {}
    for day in trading_days:
        rows = pools[day]
        market_stats[day] = {
            "zt": len(rows),
            "first": sum(r["lbc"] == 1 for r in rows),
            "two_plus": sum(r["lbc"] >= 2 for r in rows),
        }
        ss: dict[str, dict[str, float]] = {}
        for r in rows:
            s = r["sector"]
            if s not in ss:
                ss[s] = {"zt": 0, "first": 0, "two_plus": 0}
            ss[s]["zt"] += 1
            if r["lbc"] == 1:
                ss[s]["first"] += 1
            if r["lbc"] >= 2:
                ss[s]["two_plus"] += 1
        sector_stats[day] = ss

    # 首板候选：T 日 lbc == 1；标签：T+1 同股 lbc >= 2。
    events: list[dict[str, Any]] = []
    candidate_codes = set()
    for day in trading_days[:-1]:
        nd = next_day[day]
        today_map = pool_by_code[day]
        next_map = pool_by_code[nd]
        for r in pools[day]:
            if r["lbc"] != 1:
                continue
            nxt = next_map.get(r["code"])
            label = 1 if nxt and nxt["lbc"] >= 2 else 0
            p = parse_hhmmss(r["first_time"])
            l = parse_hhmmss(r["last_time"])
            sstats = sector_stats[day].get(r["sector"], {"zt": 1, "first": 1, "two_plus": 0})
            prev = pool_by_code.get(prev_day.get(day, ""), {})
            prev_market = market_stats.get(prev_day.get(day, ""), {"zt": 0, "first": 0, "two_plus": 0})
            prev_rate = (
                sum(1 for x in prev.values() if x["lbc"] == 1 and r["code"] in pool_by_code[day] and r["code"] in pool_by_code[day])
                / max(1, prev_market["first"])
            )
            # 上式不能直接代表“昨日首板 -> 今日二板”，这里下面单独算市场真实昨日1->2。
            prev_day_str = prev_day.get(day)
            prev_rate = 0.0
            if prev_day_str:
                prev_rows = pools[prev_day_str]
                prev_first = [x["code"] for x in prev_rows if x["lbc"] == 1]
                if prev_first:
                    prev_rate = sum(1 for c in prev_first if c in pool_by_code[day] and pool_by_code[day][c]["lbc"] >= 2) / len(prev_first)

            event = {
                "date": day,
                "next_date": nd,
                "code": r["code"],
                "name": r["name"],
                "label": label,
                "first_seal_min": p if p is not None else 999.0,
                "last_seal_min": l if l is not None else 999.0,
                "open_times": r["open_times"],
                "seal_fund_ratio": r["seal_fund"] / r["amount"] if r["amount"] > 0 else 0.0,
                "turnover": r["turnover"],
                "amount_billion": r["amount"] / 1e9,
                "float_mcap_billion": r["float_mcap"] / 1e9,
                "board_duration_min": (l - p) if p is not None and l is not None else 0.0,
                "sector_zt_count": sstats["zt"],
                "sector_first_count": sstats["first"],
                "sector_2plus_count": sstats["two_plus"],
                "sector_first_share": sstats["first"] / max(1.0, sstats["zt"]),
                "market_zt_count": market_stats[day]["zt"],
                "market_first_count": market_stats[day]["first"],
                "market_2plus_count": market_stats[day]["two_plus"],
                "prev_day_zt_count": prev_market["zt"],
                "prev_day_first_count": prev_market["first"],
                "prev_day_2plus_count": prev_market["two_plus"],
                "prev_day_1to2_rate": prev_rate,
                "sector": r["sector"],
            }
            events.append(event)
            candidate_codes.add(r["code"])

    print(f"first_board_events={len(events)}, unique_codes={len(candidate_codes)}")

    # 拉取所有首板候选股票的历史 K 线；T 日特征只使用 T-1。
    kbars: dict[str, list[dict[str, Any]]] = {}
    codes = sorted(candidate_codes)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(fetch_kline, code): code for code in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            try:
                rows = fut.result()
                if rows:
                    kbars[code] = rows
            except Exception as exc:
                print(f"kline_failed code={code}: {exc}")
            if i % 100 == 0:
                print(f"kline_progress={i}/{len(codes)}")

    usable = []
    skipped = 0
    for e in events:
        feats = pre_t_features(kbars.get(e["code"], []), e["date"])
        if not feats:
            skipped += 1
            continue
        row = dict(e)
        row.update(feats)
        for k in FEATURES:
            if k not in row or row[k] is None or not math.isfinite(float(row[k])):
                row[k] = 0.0
        usable.append(row)

    print(f"usable_events={len(usable)}, skipped_no_kline={skipped}")

    dates = sorted({r["date"] for r in usable})
    split_date = dates[max(1, int(len(dates) * 0.70) - 1)]
    train = [r for r in usable if r["date"] <= split_date]
    test = [r for r in usable if r["date"] > split_date]

    x_train = [[float(r[f]) for f in FEATURES] for r in train]
    y_train = [int(r["label"]) for r in train]
    x_test = [[float(r[f]) for f in FEATURES] for r in test]
    y_test = [int(r["label"]) for r in test]

    model = Logistic(l2=1.0, lr=0.05, epochs=1800)
    model.fit(x_train, y_train)
    train_scores = model.predict_proba(x_train)
    test_scores = model.predict_proba(x_test)

    baseline_train = sum(y_train) / len(y_train)
    baseline_test = sum(y_test) / len(y_test)
    metrics: dict[str, Any] = {
        "train_n": len(train),
        "test_n": len(test),
        "train_positive_rate": baseline_train,
        "test_positive_rate": baseline_test,
        "test_auc": auc(y_test, test_scores),
        "test_pr_auc": pr_auc(y_test, test_scores),
    }

    test_rows = []
    for row, score in zip(test, test_scores):
        x = dict(row)
        x["score"] = score
        test_rows.append(x)

    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in test_rows:
        by_day.setdefault(r["date"], []).append(r)

    for k in (1, 3, 5):
        picks = []
        day_hits = 0
        for day, rows in by_day.items():
            rows = sorted(rows, key=lambda r: r["score"], reverse=True)
            chosen = rows[: min(k, len(rows))]
            picks.extend(chosen)
            if any(r["label"] == 1 for r in chosen):
                day_hits += 1
        precision = sum(r["label"] for r in picks) / len(picks) if picks else 0.0
        metrics[f"top{k}_precision"] = precision
        metrics[f"top{k}_lift_vs_random"] = precision / baseline_test if baseline_test > 0 else None
        metrics[f"top{k}_daily_hit_rate"] = day_hits / len(by_day) if by_day else 0.0
        metrics[f"top{k}_pick_count"] = len(picks)

    # Top 10% 作为跨日期归一的补充指标。
    n_pct = max(1, int(len(test_rows) * 0.10))
    top_pct_rows = sorted(test_rows, key=lambda r: r["score"], reverse=True)[:n_pct]
    metrics["top10pct_precision"] = sum(r["label"] for r in top_pct_rows) / len(top_pct_rows)
    metrics["top10pct_lift_vs_random"] = (
        metrics["top10pct_precision"] / baseline_test if baseline_test > 0 else None
    )

    # 系数只用于研究“哪些变量被模型利用”，不是因果结论。
    coefs = sorted(
        [{"feature": f_name, "coef": model.w[i], "abs_coef": abs(model.w[i])} for i, f_name in enumerate(FEATURES)],
        key=lambda z: z["abs_coef"],
        reverse=True,
    )

    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "version": "one-to-two-backtest-v1",
        "start": args.start,
        "end": args.end,
        "available_trading_days": len(trading_days),
        "first_board_events": len(events),
        "usable_events": len(usable),
        "unique_first_board_codes": len(candidate_codes),
        "time_split_date": split_date,
        "model": {
            "type": "logistic_regression",
            "l2": 1.0,
            "features": FEATURES,
            "note": "score用于横截面排序；未做独立概率校准。",
        },
        "metrics": metrics,
        "top_coefficients": coefs[:15],
        "data_limitations": [
            "涨停池使用东方财富历史涨停池，主板通过代码前缀过滤，ST/退市名称过滤。",
            "首板定义为T日连续连板数lbc=1；二板定义为T+1连续连板数lbc>=2。",
            "板块只采用涨停池中的行业字段hybk，没有加入概念题材。",
            "T日首板质量字段来自T日收盘涨停池；技术面来自T-1及更早日K。",
            "K线采用未复权公开日K，长期收益率因子可能受除权影响。",
            "模型为研究基线，不代表未来市场表现或可执行收益。",
        ],
    }
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with (OUT / "test_predictions.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "code", "name", "label", "score", "sector"])
        writer.writeheader()
        for r in sorted(test_rows, key=lambda x: (x["date"], -x["score"])):
            writer.writerow({k: r[k] for k in ["date", "code", "name", "label", "score", "sector"]})

    with (OUT / "daily_test_summary.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "first_boards", "positive", "baseline", "top1_hits", "top3_hits", "top5_hits"])
        for day in sorted(by_day):
            rows = sorted(by_day[day], key=lambda r: r["score"], reverse=True)
            writer.writerow([
                day,
                len(rows),
                sum(r["label"] for r in rows),
                sum(r["label"] for r in rows) / len(rows) if rows else 0.0,
                sum(r["label"] for r in rows[:1]),
                sum(r["label"] for r in rows[:3]),
                sum(r["label"] for r in rows[:5]),
            ])

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
