"""对已挖掘的连板启动样本做二次规律发现。

不强行寻找80%单一条件，而是：
1. 比较正样本与同日对照的单因子阈值；
2. 寻找高覆盖、高区分度的双因子组合；
3. 用按日期的70/30时间切分检查候选规则是否稳定；
4. 输出可供后续选股算法使用的规则候选，而不是直接修改选股器。
"""
from __future__ import annotations

import json
import math
from itertools import combinations
from pathlib import Path
from statistics import median

DATA = Path("data/pattern_mining")
FEATURE_FILE = DATA / "latest_feature_rows.json"
CONTROL_FILE = DATA / "latest_control_rows.json"
REPORT_FILE = DATA / "latest_advanced_pattern_report.json"
RULE_FILE = DATA / "latest_rule_candidates.json"

NUMERIC = [
    "ret_1d", "ret_3d", "ret_5d", "ret_10d", "ret_20d",
    "turnover_1d", "turnover_3d_avg", "turnover_5d_avg",
    "turnover_10d_avg", "turnover_20d_avg", "turnover_5_vs_20",
    "volume_5_vs_20", "near_high20_pct", "near_high60_pct",
    "above_low20_pct", "range_1d_pct", "close_position_1d",
    "up_days_5", "max_drawdown_20d_pct", "recent10_volatility_pct",
    "current_circ_mcap_billion",
]
BOOLS = [
    "close_above_ma5", "close_above_ma10", "close_above_ma20",
    "ma5_gt_ma10", "ma10_gt_ma20", "ma20_gt_ma60", "bull_alignment",
    "turnover_rising_5_vs_20",
]


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def coverage(rows, predicate):
    return sum(bool(predicate(r)) for r in rows) / len(rows) if rows else 0.0


def quantile(values, p):
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def rule_metrics(fn, pos, ctl):
    p = coverage(pos, fn)
    c = coverage(ctl, fn)
    lift = p / c if c > 0 else (999.0 if p > 0 else 0.0)
    precision = p / (p + c) if (p + c) > 0 else 0.0
    return p, c, lift, precision


def make_single_rules(pos, ctl):
    rules = []
    for feature in NUMERIC:
        pv = [num(r.get(feature)) for r in pos]
        pv = [x for x in pv if x is not None]
        if len(pv) < 30:
            continue
        thresholds = sorted({quantile(pv, p) for p in (.10, .25, .50, .75, .90)})
        for t in thresholds:
            for direction in (">=", "<="):
                if direction == ">=":
                    fn = lambda r, f=feature, t=t: (num(r.get(f)) is not None and num(r.get(f)) >= t)
                else:
                    fn = lambda r, f=feature, t=t: (num(r.get(f)) is not None and num(r.get(f)) <= t)
                p, c, lift, precision = rule_metrics(fn, pos, ctl)
                if p >= .50 and c <= .75 and lift >= 1.20:
                    rules.append({
                        "kind": "single", "feature": feature, "operator": direction,
                        "threshold": round(t, 6), "positive_coverage": round(p, 4),
                        "control_coverage": round(c, 4), "lift": round(lift, 4),
                        "precision_proxy": round(precision, 4), "fn": fn,
                    })
    for feature in BOOLS:
        fn = lambda r, f=feature: bool(r.get(f))
        p, c, lift, precision = rule_metrics(fn, pos, ctl)
        if p >= .50 and c <= .75 and lift >= 1.20:
            rules.append({
                "kind": "single", "feature": feature, "operator": "==",
                "threshold": True, "positive_coverage": round(p, 4),
                "control_coverage": round(c, 4), "lift": round(lift, 4),
                "precision_proxy": round(precision, 4), "fn": fn,
            })
    rules.sort(key=lambda x: (x["lift"], x["positive_coverage"]), reverse=True)
    return rules[:40]


def make_pair_rules(pos, ctl, singles):
    out = []
    for a, b in combinations(singles[:24], 2):
        if a["feature"] == b["feature"]:
            continue
        fa, fb = a["fn"], b["fn"]
        fn = lambda r, fa=fa, fb=fb: fa(r) and fb(r)
        p, c, lift, precision = rule_metrics(fn, pos, ctl)
        if p >= .35 and c <= .45 and lift >= 1.50:
            out.append({
                "kind": "pair",
                "rules": [
                    {k: v for k, v in a.items() if k != "fn"},
                    {k: v for k, v in b.items() if k != "fn"},
                ],
                "positive_coverage": round(p, 4),
                "control_coverage": round(c, 4),
                "lift": round(lift, 4),
                "precision_proxy": round(precision, 4),
                "fn": fn,
            })
    out.sort(key=lambda x: (x["lift"], x["positive_coverage"]), reverse=True)
    return out[:30]


def serializable(rule):
    out = dict(rule)
    out.pop("fn", None)
    return out


def stability(rule, pos, ctl):
    dates = sorted({str(r.get("start_date")) for r in pos if r.get("start_date")})
    if len(dates) < 10:
        return None
    cut = dates[int(len(dates) * .70)]
    train_pos = [r for r in pos if str(r.get("start_date")) <= cut]
    test_pos = [r for r in pos if str(r.get("start_date")) > cut]
    train_ctl = [r for r in ctl if str(r.get("start_date")) <= cut]
    test_ctl = [r for r in ctl if str(r.get("start_date")) > cut]
    if not test_pos or not test_ctl:
        return None
    tp = coverage(test_pos, rule["fn"])
    tc = coverage(test_ctl, rule["fn"])
    return {
        "split_date": cut,
        "train_positive_coverage": round(coverage(train_pos, rule["fn"]), 4),
        "train_control_coverage": round(coverage(train_ctl, rule["fn"]), 4),
        "test_positive_coverage": round(tp, 4),
        "test_control_coverage": round(tc, 4),
        "test_lift": round(tp / tc, 4) if tc > 0 else 999.0,
    }


def main():
    if not FEATURE_FILE.exists() or not CONTROL_FILE.exists():
        raise SystemExit("missing mining outputs")
    pos = json.loads(FEATURE_FILE.read_text(encoding="utf-8"))
    ctl = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
    if isinstance(pos, dict):
        pos = pos.get("rows", [])
    if isinstance(ctl, dict):
        ctl = ctl.get("rows", [])

    singles = make_single_rules(pos, ctl)
    pairs = make_pair_rules(pos, ctl, singles)
    candidates = singles[:15] + pairs[:15]
    for r in candidates:
        r["stability"] = stability(r, pos, ctl)

    distribution = {}
    for feature in NUMERIC:
        pv = [num(r.get(feature)) for r in pos]
        cv = [num(r.get(feature)) for r in ctl]
        pv = [x for x in pv if x is not None]
        cv = [x for x in cv if x is not None]
        if not pv or not cv:
            continue
        distribution[feature] = {
            "positive_median": round(median(pv), 6),
            "control_median": round(median(cv), 6),
            "median_delta": round(median(pv) - median(cv), 6),
            "positive_p10": round(quantile(pv, .10), 6),
            "positive_p90": round(quantile(pv, .90), 6),
            "control_p10": round(quantile(cv, .10), 6),
            "control_p90": round(quantile(cv, .90), 6),
        }

    report = {
        "version": "advanced-V1",
        "analysis_date": "2026-09-14",
        "positive_samples": len(pos),
        "control_samples": len(ctl),
        "single_rules": [serializable(r) for r in singles],
        "pair_rules": [serializable(r) for r in pairs],
        "top_stable_candidates": [serializable(r) for r in candidates],
        "feature_distribution": distribution,
        "method_notes": [
            "不再强制单一条件覆盖80%；80%若不存在则保留高覆盖高区分度候选。",
            "单因子候选要求正样本覆盖>=50%、对照覆盖<=75%、lift>=1.2。",
            "双因子候选要求正样本覆盖>=35%、对照覆盖<=45%、lift>=1.5。",
            "候选规则再按时间切分做稳定性检查，不能仅凭样本内结果进入选股器。",
            "所有特征仍严格只使用启动日前T-1及更早数据。",
        ],
    }
    RULE_FILE.write_text(json.dumps({
        "single_rules": [serializable(r) for r in singles],
        "pair_rules": [serializable(r) for r in pairs],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "positive": len(pos), "control": len(ctl),
        "top_single": report["single_rules"][:5],
        "top_pair": report["pair_rules"][:5],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
