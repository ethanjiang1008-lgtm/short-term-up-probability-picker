#!/usr/bin/env python3
"""第二轮研究：首板->次日二板 + 首板封板结构增量回测。

与 seven-factor-stock-picker 完全独立。
基线模型沿用第一轮特征；增强模型在基线上加入首板当日 5 分钟封板结构。
严禁使用次日数据构造特征。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from market_data_sina import fetch_all_stocks, fetch_kline, is_main_board

OUT = Path("data/backtest_one_to_two_round2")
MIN_HISTORY = 60
LIMIT_UP = 0.095
MINUTE_KLT = 5
MINUTE_LIMIT_UP = 0.095
DEFAULT_WORKERS = 10

BASE_FEATURES = [
    "ret_1d","ret_3d","ret_5d","ret_10d","ret_20d",
    "turnover_1d","turnover_5d_avg","turnover_20d_avg","turnover_5_vs_20",
    "volume_5_vs_20","amount_5_vs_20","close_above_ma20","ma5_gt_ma10","ma10_gt_ma20",
    "near_high20_pct","above_low20_pct","up_days_5","volatility_10d","amplitude_1d",
    "market_first_count","market_2plus_count","market_zt_count",
    "prev_market_first_count","prev_market_2plus_count","prev_market_1to2_rate",
]

STRUCT_FEATURES = [
    "structure_available",
    "one_word_board_proxy",
    "open_gap_pct",
    "first_touch_minute",
    "touch_count",
    "reopen_count",
    "seal_after_first_ratio",
    "final_30m_seal_ratio",
    "final_60m_seal_ratio",
    "last_bar_sealed",
    "touch_to_close_minutes",
    "max_retrace_after_touch_pct",
    "post_touch_volume_share",
    "post_touch_volume_vs_pre",
    "early_touch_before_1000",
    "early_touch_before_1030",
    "early_touch_before_1100",
    "touch_before_1330",
]

ALL_FEATURES = BASE_FEATURES + STRUCT_FEATURES


def f(x, d=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else d
    except (TypeError, ValueError):
        return d


def pct(a, b):
    return (a / b - 1.0) * 100 if b else 0.0


def limitup(bar, prev, name):
    s = "".join(str(name or "").upper().split()).replace("*", "")
    if s.startswith("ST") or "退" in s:
        return False
    pc, c = f(prev.get("close")), f(bar.get("close"))
    return pc > 0 and c / pc - 1 >= LIMIT_UP


def base_feats(bs, i):
    pre = bs[: i + 1]
    if len(pre) < MIN_HISTORY:
        return {}
    c = [f(x.get("close")) for x in pre]
    v = [f(x.get("volume")) for x in pre]
    t = [f(x.get("turnover")) for x in pre]
    a = [f(x.get("amount")) for x in pre]
    ma5 = statistics.fmean(c[-5:])
    ma10 = statistics.fmean(c[-10:])
    ma20 = statistics.fmean(c[-20:])
    t5 = statistics.fmean(t[-5:])
    t20 = statistics.fmean(t[-20:])
    v5 = statistics.fmean(v[-5:])
    v20 = statistics.fmean(v[-20:])
    a5 = statistics.fmean(a[-5:])
    a20 = statistics.fmean(a[-20:])
    price = c[-1]
    return {
        "ret_1d": pct(price, c[-2]), "ret_3d": pct(price, c[-4]),
        "ret_5d": pct(price, c[-6]), "ret_10d": pct(price, c[-11]),
        "ret_20d": pct(price, c[-21]), "turnover_1d": t[-1],
        "turnover_5d_avg": t5, "turnover_20d_avg": t20,
        "turnover_5_vs_20": t5 / t20 if t20 else 1,
        "volume_5_vs_20": v5 / v20 if v20 else 1,
        "amount_5_vs_20": a5 / a20 if a20 else 1,
        "close_above_ma20": int(price > ma20),
        "ma5_gt_ma10": int(ma5 > ma10), "ma10_gt_ma20": int(ma10 > ma20),
        "near_high20_pct": pct(price, max(c[-20:])),
        "above_low20_pct": pct(price, min(c[-20:])),
        "up_days_5": sum(c[z] > c[z - 1] for z in range(len(c)-5, len(c))),
        "volatility_10d": statistics.pstdev(c[-10:]) / price * 100 if price else 0,
        "amplitude_1d": (f(bs[i].get("high")) - f(bs[i].get("low"))) / price * 100 if price else 0,
    }


def http_json(url, timeout=15):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    last = None
    for wait in (0, 1, 2):
        if wait:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            last = exc
    raise RuntimeError(last)


def fetch_5m_day(code, day):
    """只拉首板当天5分钟K线；无法获取时返回None。"""
    market = 1 if code.startswith("6") else 0
    day8 = day.replace("-", "")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={market}.{code}"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt={MINUTE_KLT}&fqt=0&beg={day8}&end={day8}&lmt=500"
    )
    payload = http_json(url, timeout=20)
    rows = []
    for item in ((payload.get("data") or {}).get("klines") or []):
        p = item.split(",")
        if len(p) < 7:
            continue
        try:
            rows.append({
                "dt": p[0],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "volume": float(p[5]),
                "amount": float(p[6]),
            })
        except (TypeError, ValueError):
            pass
    return rows


def minute_structure(code, day, daily_prev_close):
    mins = fetch_5m_day(code, day)
    if not mins or daily_prev_close <= 0:
        return {"structure_available": 0}
    clean = [m for m in mins if str(m["dt"]).startswith(day)]
    if not clean:
        return {"structure_available": 0}

    def minute_from_open(dt):
        try:
            hh, mm = map(int, str(dt)[11:16].split(":"))
            return (hh * 60 + mm) - (9 * 60 + 30)
        except Exception:
            return 0

    touched = []
    sealed = []
    for m in clean:
        off = minute_from_open(m["dt"])
        hit = m["high"] / daily_prev_close - 1 >= MINUTE_LIMIT_UP
        is_sealed = m["close"] / daily_prev_close - 1 >= MINUTE_LIMIT_UP
        touched.append((off, hit, is_sealed, m))
        if hit:
            sealed.append((off, is_sealed, m))

    touch_rows = [x for x in touched if x[1]]
    if not touch_rows:
        return {"structure_available": 1, "one_word_board_proxy": 0, "open_gap_pct": pct(clean[0]["open"], daily_prev_close)}

    first_off = touch_rows[0][0]
    first_idx = next(i for i, x in enumerate(touched) if x[1])
    post = touched[first_idx:]
    reopen_count = sum(1 for _, hit, is_sealed, _ in post if hit and not is_sealed)

    # 首次触板后，连续以“收盘仍在板上”视为稳定封板代理。
    seal_after_first_ratio = (
        sum(1 for _, _, is_sealed, _ in post if is_sealed) / len(post)
        if post else 0
    )
    final_bars = [x for x in touched if x[0] >= 210]  # 13:00以后
    final_60 = [x for x in touched if x[0] >= 180]  # 12:30以后
    final_30 = [x for x in touched if x[0] >= 360]  # 15:30附近后，实际5m数据不足时按可见末段
    all_tail = final_30 or touched[-6:]
    all_tail_60 = final_60 or touched[-12:]
    last_sealed = int(touched[-1][2]) if touched else 0

    post_rows = [x[3] for x in post]
    pre_rows = [x[3] for x in touched[:first_idx]]
    post_vol = sum(f(x["volume"]) for x in post_rows)
    pre_vol = sum(f(x["volume"]) for x in pre_rows)

    retraces = []
    for _, _, _, m in post:
        retraces.append(max(0.0, (daily_prev_close * (1 + MINUTE_LIMIT_UP) - m["close"]) /
                            (daily_prev_close * (1 + MINUTE_LIMIT_UP)) * 100))
    max_retrace = max(retraces) if retraces else 0.0

    open_gap = pct(clean[0]["open"], daily_prev_close)
    # 开盘距离理论涨停阈值很近时，作为“一字/近一字板”代理，而非严格定义。
    one_word = int(clean[0]["open"] / daily_prev_close - 1 >= 0.085)

    return {
        "structure_available": 1,
        "one_word_board_proxy": one_word,
        "open_gap_pct": open_gap,
        "first_touch_minute": first_off,
        "touch_count": len(touch_rows),
        "reopen_count": reopen_count,
        "seal_after_first_ratio": round(seal_after_first_ratio, 6),
        "final_30m_seal_ratio": round(sum(1 for x in all_tail if x[2]) / len(all_tail), 6) if all_tail else 0,
        "final_60m_seal_ratio": round(sum(1 for x in all_tail_60 if x[2]) / len(all_tail_60), 6) if all_tail_60 else 0,
        "last_bar_sealed": last_sealed,
        "touch_to_close_minutes": max(0, touched[-1][0] - first_off),
        "max_retrace_after_touch_pct": max_retrace,
        "post_touch_volume_share": post_vol / (post_vol + pre_vol) if post_vol + pre_vol else 0,
        "post_touch_volume_vs_pre": post_vol / pre_vol if pre_vol else 10,
        "early_touch_before_1000": int(first_off <= 30),
        "early_touch_before_1030": int(first_off <= 60),
        "early_touch_before_1100": int(first_off <= 90),
        "touch_before_1330": int(first_off <= 240),
    }


def sigmoid(z):
    return 1 / (1 + math.exp(max(-35, min(35, -z))))


class LR:
    def fit(self, X, y):
        n, d = len(X), len(X[0])
        self.mu = [statistics.fmean(r[j] for r in X) for j in range(d)]
        self.sd = [max(statistics.pstdev([r[j] for r in X]), 1e-9) for j in range(d)]
        Z = [[(r[j] - self.mu[j]) / self.sd[j] for j in range(d)] for r in X]
        self.w = [0.0] * d
        p = (sum(y) + 0.5) / (n - sum(y) + 0.5)
        self.b = math.log(p)
        for _ in range(1200):
            gw = [0.0] * d
            gb = 0.0
            for r, target in zip(Z, y):
                pred = sigmoid(self.b + sum(a * b for a, b in zip(self.w, r)))
                e = pred - target
                gb += e
                for j in range(d):
                    gw[j] += e * r[j]
            self.b -= 0.05 * gb / n
            for j in range(d):
                self.w[j] -= 0.05 * (gw[j] / n + self.w[j] / n)

    def predict(self, X):
        return [
            sigmoid(
                self.b +
                sum(self.w[j] * (r[j] - self.mu[j]) / self.sd[j] for j in range(len(r)))
            )
            for r in X
        ]


def topk_metrics(rows, scores_by_row, k=1):
    by_day = {}
    for idx, score in scores_by_row.items():
        by_day.setdefault(rows[idx]["date"], []).append(idx)
    picks = []
    daily_hits = 0
    for day, idxs in by_day.items():
        chosen = sorted(idxs, key=lambda z: scores_by_row[z], reverse=True)[:k]
        picks.extend(chosen)
        daily_hits += int(any(rows[z]["label"] for z in chosen))
    precision = sum(rows[z]["label"] for z in picks) / len(picks) if picks else 0
    daily_hit = daily_hits / len(by_day) if by_day else 0
    return precision, daily_hit, len(by_day)


def feature_conditional_report(rows, feature_defs):
    out = []
    total = len(rows)
    base = sum(r["label"] for r in rows) / total if total else 0
    for name, fn in feature_defs:
        matched = [r for r in rows if fn(r)]
        if len(matched) < 20:
            continue
        rate = sum(r["label"] for r in matched) / len(matched)
        out.append({
            "condition": name,
            "n": len(matched),
            "next_day_2board_rate": rate,
            "lift_vs_all": rate / base if base else None,
        })
    return sorted(out, key=lambda x: (-x["next_day_2board_rate"], -x["n"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-16")
    ap.add_argument("--end", default="2026-09-18")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = ap.parse_args()

    stocks = [
        r for r in fetch_all_stocks()
        if is_main_board(str(r.get("code", "")), str(r.get("name", "")))
    ]
    meta = {str(r["code"]): str(r.get("name", "")) for r in stocks}
    bars = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_kline, c, 420): c for c in meta}
        for i, fut in enumerate(as_completed(futures), 1):
            c = futures[fut]
            try:
                b = [z for z in fut.result() if args.start <= str(z.get("date", "")) <= args.end]
                if len(b) >= MIN_HISTORY:
                    bars[c] = sorted(b, key=lambda z: z["date"])
            except Exception as exc:
                print("kline_failed", c, exc)
            if i % 200 == 0:
                print("progress", i, "/", len(meta))

    days = sorted({z["date"] for bs in bars.values() for z in bs})
    next_day = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    prev_day = {days[i]: days[i - 1] for i in range(1, len(days))}

    day_rows = {}
    events = []
    for code, bs in bars.items():
        name = meta[code]
        streak = 0
        for i in range(1, len(bs)):
            if limitup(bs[i], bs[i - 1], name):
                streak += 1
            else:
                streak = 0
            if streak > 0:
                day_rows.setdefault(bs[i]["date"], []).append((code, streak))
            if streak == 1 and len(bs[:i]) >= MIN_HISTORY:
                events.append((bs[i]["date"], code, name, i, bs))

    lookup = {d: dict(rows) for d, rows in day_rows.items()}
    market = {}
    for d, rows in day_rows.items():
        market[d] = {
            "zt": len(rows),
            "first": sum(s == 1 for _, s in rows),
            "two": sum(s >= 2 for _, s in rows),
        }

    # 先构造首板事件的基础特征，再并发拉取首板当天5分钟结构，避免串行网络请求成为瓶颈。
    pending_rows = []
    for d, code, name, i, bs in events:
        nd = next_day.get(d)
        if not nd:
            continue
        pd = prev_day.get(d)
        pm = market.get(pd, {"first": 0, "two": 0}) if pd else {"first": 0, "two": 0}
        previous_first = [c for c, s in day_rows.get(pd, []) if s == 1] if pd else []
        prate = (
            sum(lookup.get(d, {}).get(c, 0) >= 2 for c in previous_first) / len(previous_first)
            if previous_first else 0
        )
        r = {
            "date": d,
            "next_date": nd,
            "code": code,
            "name": name,
            "label": int(lookup.get(nd, {}).get(code, 0) >= 2),
            "market_first_count": market.get(d, {}).get("first", 0),
            "market_2plus_count": market.get(d, {}).get("two", 0),
            "market_zt_count": market.get(d, {}).get("zt", 0),
            "prev_market_first_count": pm["first"],
            "prev_market_2plus_count": pm["two"],
            "prev_market_1to2_rate": prate,
        }
        r.update(base_feats(bs, i))
        prev_close = f(bs[i - 1].get("close"))
        for k in BASE_FEATURES:
            r[k] = f(r.get(k))
        pending_rows.append((r, code, d, prev_close))

    structures = {}
    if pending_rows:
        with ThreadPoolExecutor(max_workers=min(max(args.workers, 4), 10)) as ex:
            futures = {
                ex.submit(minute_structure, code, d, prev_close): (code, d)
                for _, code, d, prev_close in pending_rows
            }
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    structures[key] = fut.result()
                except Exception as exc:
                    print("minute_failed", key[0], key[1], exc)
                    structures[key] = {"structure_available": 0}

    rows = []
    for r, code, d, _ in pending_rows:
        r.update(structures.get((code, d), {"structure_available": 0}))
        for k in STRUCT_FEATURES:
            r[k] = f(r.get(k))
        rows.append(r)

    rows.sort(key=lambda x: (x["date"], x["code"]))
    dates = sorted({r["date"] for r in rows})
    split = dates[max(1, int(len(dates) * 0.7) - 1)]
    train = [r for r in rows if r["date"] <= split]
    test = [r for r in rows if r["date"] > split]

    # 基线：完全复刻第一轮使用的基础特征。
    baseline = LR()
    baseline.fit([[r[k] for k in BASE_FEATURES] for r in train], [r["label"] for r in train])
    baseline_scores = baseline.predict([[r[k] for k in BASE_FEATURES] for r in test])

    # 增强：仅对封板结构可用的样本训练/测试，避免把缺失结构伪装成真实的0。
    tr2 = [r for r in train if r["structure_available"] == 1]
    te2 = [r for r in test if r["structure_available"] == 1]
    enhanced = None
    enhanced_scores = []
    if len(tr2) >= 100 and len(te2) >= 20:
        enhanced = LR()
        enhanced.fit([[r[k] for k in ALL_FEATURES] for r in tr2], [r["label"] for r in tr2])
        enhanced_scores = enhanced.predict([[r[k] for k in ALL_FEATURES] for r in te2])

    baseline_by_index = {i: s for i, s in enumerate(baseline_scores)}
    b_top1, b_daily, n_days = topk_metrics(test, baseline_by_index, 1)

    report = {
        "version": "round2-seal-structure-v1",
        "period": [args.start, args.end],
        "main_board_universe": len(meta),
        "stocks_with_kline": len(bars),
        "first_board_events": len(rows),
        "split_date": split,
        "structure_available_samples": sum(r["structure_available"] == 1 for r in rows),
        "structure_coverage": sum(r["structure_available"] == 1 for r in rows) / len(rows) if rows else 0,
        "base_model": {
            "train_n": len(train),
            "test_n": len(test),
            "test_rate": sum(r["label"] for r in test) / len(test) if test else 0,
            "top1_precision": b_top1,
            "top1_daily_hit_rate": b_daily,
            "test_days": n_days,
        },
        "enhanced_model": None,
        "incremental_comparison": None,
        "structure_conditions_on_test": feature_conditional_report(
            te2,
            [
                ("one_word_board_proxy", lambda r: r["one_word_board_proxy"] >= 1),
                ("first_touch<=10:00", lambda r: r["first_touch_minute"] <= 30),
                ("first_touch<=10:30", lambda r: r["first_touch_minute"] <= 60),
                ("first_touch<=11:00", lambda r: r["first_touch_minute"] <= 90),
                ("reopen_count=0", lambda r: r["reopen_count"] == 0),
                ("reopen_count<=1", lambda r: r["reopen_count"] <= 1),
                ("seal_after_first_ratio>=0.8", lambda r: r["seal_after_first_ratio"] >= 0.8),
                ("seal_after_first_ratio>=0.9", lambda r: r["seal_after_first_ratio"] >= 0.9),
                ("final_60m_seal_ratio>=0.8", lambda r: r["final_60m_seal_ratio"] >= 0.8),
                ("final_60m_seal_ratio=1", lambda r: r["final_60m_seal_ratio"] >= 0.999),
                ("max_retrace_after_touch<=1%", lambda r: r["max_retrace_after_touch_pct"] <= 1),
                ("max_retrace_after_touch<=2%", lambda r: r["max_retrace_after_touch_pct"] <= 2),
                ("post_touch_volume_share>=0.5", lambda r: r["post_touch_volume_share"] >= 0.5),
            ],
        ),
        "notes": [
            "基线模型与第一轮使用同一组基础特征，便于直接比较Top1。",
            "封板结构来自首板当天5分钟K线；结构不可用的样本不进入增强模型。",
            "5分钟K线只能近似识别触板/开板/回封，不能等同于真实盘口封单金额或精确封板秒级时间。",
            "任何结构条件最终是否采用，都应以时间顺序样本外结果为准，不用测试集调参。",
        ],
    }

    if enhanced is not None:
        scores_by_row = {i: s for i, s in enumerate(enhanced_scores)}
        e_top1, e_daily, e_days = topk_metrics(te2, scores_by_row, 1)
        # apples-to-apples：在结构数据齐全的测试子集上重算基线Top1。
        common_base_model = baseline
        common_base_scores = common_base_model.predict(
            [[r[k] for k in BASE_FEATURES] for r in te2]
        )
        cb = {i: s for i, s in enumerate(common_base_scores)}
        cb_top1, cb_daily, _ = topk_metrics(te2, cb, 1)
        report["enhanced_model"] = {
            "train_n": len(tr2),
            "test_n": len(te2),
            "test_rate": sum(r["label"] for r in te2) / len(te2),
            "top1_precision": e_top1,
            "top1_daily_hit_rate": e_daily,
            "test_days": e_days,
        }
        report["incremental_comparison"] = {
            "common_test_n": len(te2),
            "common_baseline_top1_precision": cb_top1,
            "enhanced_top1_precision": e_top1,
            "delta_pp": (e_top1 - cb_top1) * 100,
            "common_baseline_daily_hit_rate": cb_daily,
            "enhanced_daily_hit_rate": e_daily,
            "daily_hit_delta_pp": (e_daily - cb_daily) * 100,
        }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = ["date", "next_date", "code", "name", "label"] + BASE_FEATURES + STRUCT_FEATURES
    with (OUT / "all_event_features.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, 0) for k in fields})

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
