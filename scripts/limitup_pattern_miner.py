"""连板启动前规律挖掘器 V2。

目标：从过去约一年主板股票中识别 >=2 连板事件，把连续连板的第一板作为启动点，
只使用启动日前已经完成的日K线构建特征，并与同一启动日的非连板对照样本比较。

设计原则：
1. 非 ST / 非退市 / 主板；ST 判断兼容 *ST。
2. 历史换手率使用东方财富公开日K字段，不再把缺失值当 0。
3. 正样本与对照样本在同一交易日抽样，降低市场环境造成的伪相关。
4. 任何特征均来自 T-1 或更早，绝不使用启动日及之后数据。
5. 80% 只是“共同出现”门槛；同时要求对照组覆盖明显更低，才进入可用规律。
6. 当前流通市值只作为当前横截面尺寸标签，不伪装成历史市值。
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
KLINE_COUNT = 320
MIN_CONSECUTIVE_LIMIT_UP = 2
COMMON_RATIO = 0.80
MIN_POSITIVE_SAMPLES = 30
CONTROL_PER_EVENT = 2
MAX_CONTROL_ROWS = 5000
WORKERS = 10
OUTPUT_DIR = Path("data/pattern_mining")
RANDOM_SEED = 20260914

EM_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_FIELDS1 = "f1,f2,f3,f4,f5,f6"
EM_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b else 0.0


def _is_st(name: str) -> bool:
    normalized = "".join(str(name or "").strip().upper().split()).replace("*", "")
    return normalized.startswith("ST") or "退" in normalized


def _http_get_json(url: str, timeout: int = 15) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last: Exception | None = None
    for wait in (0, 1, 2):
        if wait:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Eastmoney request failed: {last}") from last


def fetch_eastmoney_kline(code: str, count: int = KLINE_COUNT) -> list[dict[str, Any]]:
    market = 1 if code.startswith("6") else 0
    params = (
        f"secid={market}.{code}&fields1={EM_FIELDS1}&fields2={EM_FIELDS2}"
        f"&klt=101&fqt=0&beg=0&end=20500101&lmt={count}"
    )
    payload = _http_get_json(f"{EM_URL}?{params}")
    items = ((payload.get("data") or {}).get("klines")) or []
    out: list[dict[str, Any]] = []
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
                "amplitude": float(p[7]),
                "pct": float(p[8]),
                "change": float(p[9]),
                "turnover": float(p[10]),
            })
        except (TypeError, ValueError):
            continue
    return out


def _is_limit_up(bar: dict[str, Any], prev: dict[str, Any], name: str) -> bool:
    if _is_st(name):
        return False
    prev_close = _f(prev.get("close"))
    close = _f(bar.get("close"))
    if prev_close <= 0 or close <= 0:
        return False
    return close / prev_close - 1.0 >= 0.095


def _events_for_stock(bars: list[dict[str, Any]], name: str, cutoff: str) -> list[int]:
    bars = [b for b in bars if str(b.get("date", "")) >= cutoff]
    out: list[int] = []
    i = 1
    while i < len(bars):
        if _is_limit_up(bars[i], bars[i - 1], name):
            start = i
            j = i + 1
            while j < len(bars) and _is_limit_up(bars[j], bars[j - 1], name):
                j += 1
            if j - start >= MIN_CONSECUTIVE_LIMIT_UP and start >= 21:
                out.append(start)
            i = j
        else:
            i += 1
    return out


def _max_drawdown(values: list[float]) -> float:
    peak = values[0] if values else 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst * 100.0


def build_feature_row(code: str, name: str, bars: list[dict[str, Any]], idx: int, label: int) -> dict[str, Any] | None:
    # idx 本身是启动日；严格排除 idx，最后可用数据为 idx-1。
    if idx < 21 or idx >= len(bars):
        return None
    pre = bars[:idx]
    closes = [_f(x.get("close")) for x in pre]
    volumes = [_f(x.get("volume")) for x in pre]
    turnovers = [_f(x.get("turnover")) for x in pre]
    highs = [_f(x.get("high")) for x in pre]
    lows = [_f(x.get("low")) for x in pre]
    if len(closes) < 21 or not all(v > 0 for v in closes[-21:]):
        return None

    close = closes[-1]
    ma5 = statistics.fmean(closes[-5:])
    ma10 = statistics.fmean(closes[-10:])
    ma20 = statistics.fmean(closes[-20:])
    ma60 = statistics.fmean(closes[-60:]) if len(closes) >= 60 else 0.0
    vol5 = statistics.fmean(volumes[-5:])
    vol20 = statistics.fmean(volumes[-20:])
    turnover5 = statistics.fmean(turnovers[-5:])
    turnover10 = statistics.fmean(turnovers[-10:])
    turnover20 = statistics.fmean(turnovers[-20:])
    recent20 = closes[-20:]
    high20 = max(recent20)
    low20 = min(recent20)
    high60 = max(closes[-60:]) if len(closes) >= 60 else high20

    return {
        "code": code,
        "name": name,
        "start_date": str(bars[idx].get("date", "")),
        "prev_date": str(bars[idx - 1].get("date", "")),
        "label": label,
        "pre_close": close,
        "ret_1d": _pct(close, closes[-2]),
        "ret_3d": _pct(close, closes[-4]),
        "ret_5d": _pct(close, closes[-6]),
        "ret_10d": _pct(close, closes[-11]),
        "ret_20d": _pct(close, closes[-21]),
        "turnover_1d": turnovers[-1],
        "turnover_3d_avg": statistics.fmean(turnovers[-3:]),
        "turnover_5d_avg": turnover5,
        "turnover_10d_avg": turnover10,
        "turnover_20d_avg": turnover20,
        "turnover_5_vs_20": turnover5 / turnover20 if turnover20 > 0 else 0.0,
        "turnover_rising_5_vs_20": turnover5 > turnover20 > 0,
        "volume_5_vs_20": vol5 / vol20 if vol20 > 0 else 0.0,
        "close_above_ma5": close > ma5,
        "close_above_ma10": close > ma10,
        "close_above_ma20": close > ma20,
        "ma5_gt_ma10": ma5 > ma10,
        "ma10_gt_ma20": ma10 > ma20,
        "ma20_gt_ma60": ma60 > 0 and ma20 > ma60,
        "bull_alignment": ma60 > 0 and ma5 > ma10 > ma20 > ma60,
        "near_high20_pct": _pct(close, high20),
        "near_high60_pct": _pct(close, high60),
        "above_low20_pct": _pct(close, low20),
        "range_1d_pct": ((highs[-1] - lows[-1]) / close * 100.0) if close else 0.0,
        "close_position_1d": ((close - lows[-1]) / (highs[-1] - lows[-1])) if highs[-1] > lows[-1] else 0.5,
        "up_days_5": sum(closes[i] > closes[i - 1] for i in range(len(closes) - 5, len(closes))),
        "max_drawdown_20d_pct": _max_drawdown(recent20),
        "recent10_volatility_pct": statistics.pstdev(closes[-10:]) / close * 100.0 if len(closes) >= 10 else 0.0,
        "current_circ_mcap_billion": None,
    }


def _prepare_intervals(rows: list[dict[str, Any]], key: str, bins: list[tuple[float, float]]) -> list[dict[str, Any]]:
    total = len(rows)
    out: list[dict[str, Any]] = []
    for lo, hi in bins:
        matched = sum(lo <= _f(r.get(key)) < hi for r in rows)
        coverage = matched / total if total else 0.0
        out.append({"feature": key, "type": "range", "lower": lo, "upper": hi, "coverage": round(coverage, 4), "matched": matched, "total": total})
    return out


def _positive_common(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    specs = {
        "ret_5d": [(-999, -5), (-5, 0), (0, 3), (3, 5), (5, 10), (10, 999)],
        "ret_10d": [(-999, -10), (-10, -3), (-3, 3), (3, 8), (8, 15), (15, 999)],
        "ret_20d": [(-999, -15), (-15, -5), (-5, 5), (5, 10), (10, 20), (20, 999)],
        "turnover_1d": [(0, 1), (1, 2), (2, 3), (3, 5), (5, 8), (8, 12), (12, 999)],
        "turnover_5d_avg": [(0, 1), (1, 2), (2, 3), (3, 5), (5, 8), (8, 12), (12, 999)],
        "turnover_5_vs_20": [(0, 0.7), (0.7, 1.0), (1.0, 1.2), (1.2, 1.5), (1.5, 2.0), (2.0, 999)],
        "volume_5_vs_20": [(0, 0.7), (0.7, 1.0), (1.0, 1.2), (1.2, 1.5), (1.5, 2.0), (2.0, 999)],
        "near_high20_pct": [(-30, -15), (-15, -10), (-10, -5), (-5, -2), (-2, 0), (0, 3), (3, 8), (8, 999)],
        "near_high60_pct": [(-40, -20), (-20, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 999)],
        "max_drawdown_20d_pct": [(-999, -30), (-30, -20), (-20, -15), (-15, -10), (-10, -5), (-5, 0)],
        "up_days_5": [(0, 2), (2, 3), (3, 4), (4, 6)],
        "range_1d_pct": [(0, 1), (1, 2), (2, 3), (3, 5), (5, 8), (8, 999)],
    }
    bools = [
        "close_above_ma5", "close_above_ma10", "close_above_ma20",
        "ma5_gt_ma10", "ma10_gt_ma20", "ma20_gt_ma60", "bull_alignment", "turnover_rising_5_vs_20",
    ]
    out: list[dict[str, Any]] = []
    for key, bins in specs.items():
        candidates = _prepare_intervals(rows, key, bins)
        eligible = [x for x in candidates if x["coverage"] >= COMMON_RATIO]
        if eligible:
            eligible.sort(key=lambda x: (-x["coverage"], x["upper"] - x["lower"]))
            out.append(eligible[0])
    for key in bools:
        total = len(rows)
        matched = sum(bool(r.get(key)) for r in rows)
        coverage = matched / total if total else 0.0
        if coverage >= COMMON_RATIO:
            out.append({"feature": key, "type": "boolean", "value": True, "coverage": round(coverage, 4), "matched": matched, "total": total})
    return sorted(out, key=lambda x: (-x["coverage"], x["feature"]))


def _contrast(common: list[dict[str, Any]], positives: list[dict[str, Any]], controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not positives or not controls:
        return []
    control_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in controls:
        control_index[str(row.get("start_date"))].append(row)
    out: list[dict[str, Any]] = []
    for p in common:
        key = str(p["feature"])
        if p["type"] == "boolean":
            c_matched = sum(bool(r.get(key)) for r in controls)
        else:
            lo = _f(p.get("lower"))
            hi = _f(p.get("upper"))
            c_matched = sum(lo <= _f(r.get(key)) < hi for r in controls)
        control_cov = c_matched / len(controls)
        diff = p["coverage"] - control_cov
        odds = ((p["coverage"] / max(1e-6, 1 - p["coverage"])) / (control_cov / max(1e-6, 1 - control_cov))) if 0 < control_cov < 1 and 0 < p["coverage"] < 1 else None
        item = dict(p)
        item.update({"control_coverage": round(control_cov, 4), "coverage_diff": round(diff, 4), "odds_ratio": round(odds, 3) if odds is not None else None})
        # 80% 共性 + 至少 20 个百分点区分度，或非常高的相对优势。
        if diff >= 0.20 or (control_cov <= 0.50 and p["coverage"] >= 0.90):
            out.append(item)
    return sorted(out, key=lambda x: (-x["coverage_diff"], -(x.get("odds_ratio") or 0), x["feature"]))


def _sample_controls(all_rows: dict[str, list[dict[str, Any]]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rng = random.Random(RANDOM_SEED)
    by_date = defaultdict(list)
    for code, bars in all_rows.items():
        # 由 caller 传入的 bars 已经过滤；这里按事件日期寻找非事件股票。
        for idx in range(21, len(bars)):
            by_date[str(bars[idx].get("date", ""))].append((code, idx))

    event_keys = {(e["code"], e["start_date"]) for e in events}
    controls: list[dict[str, Any]] = []
    used = set()
    for event in events:
        pool = []
        for code, idx in by_date.get(event["start_date"], []):
            if code == event["code"]:
                continue
            if (code, str(all_rows[code][idx].get("date", ""))) in event_keys:
                continue
            pool.append((code, idx))
        rng.shuffle(pool)
        chosen = 0
        for code, idx in pool:
            key = (code, event["start_date"])
            if key in used:
                continue
            row = build_feature_row(code, str(event.get("control_name_map", {}).get(code, "")), all_rows[code], idx, 0)
            if row:
                row["start_date"] = event["start_date"]
                controls.append(row)
                used.add(key)
                chosen += 1
            if chosen >= CONTROL_PER_EVENT:
                break
            if len(controls) >= MAX_CONTROL_ROWS:
                return controls
    return controls


def main() -> None:
    today = date.today()
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    universe_rows = [r for r in fetch_all_stocks() if is_main_board(str(r.get("code", "")), str(r.get("name", ""))) and not _is_st(str(r.get("name", "")))]
    names = {str(r.get("code", "")): str(r.get("name", "")) for r in universe_rows}
    # 当前流通市值：仅做尺寸标签，单位由新浪原始字段映射为“亿”。
    current_mcap = {str(r.get("code", "")): _f(r.get("circ_mcap", 0.0)) / 100000.0 for r in universe_rows}

    all_rows: dict[str, list[dict[str, Any]]] = {}
    events: list[dict[str, Any]] = []
    positive_rows: list[dict[str, Any]] = []

    def work(code: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            return code, fetch_eastmoney_kline(code, KLINE_COUNT)
        except Exception as exc:
            print(f"kline_failed={code} {exc}")
            return code, []

    codes = list(names)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(work, code) for code in codes]
        for i, fut in enumerate(as_completed(futures), start=1):
            code, bars = fut.result()
            if bars:
                bars = [b for b in bars if str(b.get("date", "")) >= cutoff]
                bars.sort(key=lambda x: str(x.get("date", "")))
                all_rows[code] = bars
            if i % 200 == 0:
                print(f"progress={i}/{len(codes)}")

    for code, bars in all_rows.items():
        name = names.get(code, "")
        for idx in _events_for_stock(bars, name, cutoff):
            row = build_feature_row(code, name, bars, idx, 1)
            if not row:
                continue
            mcap = current_mcap.get(code)
            row["current_circ_mcap_billion"] = mcap
            positive_rows.append(row)
            events.append({"code": code, "name": name, "start_date": row["start_date"], "consecutive_limit_up": MIN_CONSECUTIVE_LIMIT_UP})

    # 同一股票同一启动日去重。
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in positive_rows:
        dedup[(row["code"], row["start_date"])] = row
    positive_rows = list(dedup.values())
    events = [e for e in events if (e["code"], e["start_date"]) in dedup]

    # 给对照样本准备同日股票池；修正 build_feature_row 需要名称的问题。
    controls: list[dict[str, Any]] = []
    rng = random.Random(RANDOM_SEED)
    by_date = defaultdict(list)
    event_date_keys = {(e["code"], e["start_date"]) for e in events}
    for code, bars in all_rows.items():
        for idx in range(21, len(bars)):
            day = str(bars[idx].get("date", ""))
            by_date[day].append((code, idx))
    used_controls: set[tuple[str, str]] = set()
    for event in events:
        candidates = [x for x in by_date.get(event["start_date"], []) if x[0] != event["code"] and (x[0], event["start_date"]) not in event_date_keys]
        rng.shuffle(candidates)
        chosen = 0
        for code, idx in candidates:
            key = (code, event["start_date"])
            if key in used_controls:
                continue
            row = build_feature_row(code, names.get(code, ""), all_rows[code], idx, 0)
            if not row:
                continue
            row["start_date"] = event["start_date"]
            row["current_circ_mcap_billion"] = current_mcap.get(code)
            controls.append(row)
            used_controls.add(key)
            chosen += 1
            if chosen >= CONTROL_PER_EVENT or len(controls) >= MAX_CONTROL_ROWS:
                break
        if len(controls) >= MAX_CONTROL_ROWS:
            break

    common = _positive_common(positive_rows)
    discriminating = _contrast(common, positive_rows, controls)

    size_buckets = Counter()
    for row in positive_rows:
        mcap = row.get("current_circ_mcap_billion")
        if mcap is None:
            continue
        if mcap < 30:
            size_buckets["<30亿"] += 1
        elif mcap < 80:
            size_buckets["30-80亿"] += 1
        elif mcap < 150:
            size_buckets["80-150亿"] += 1
        elif mcap < 300:
            size_buckets["150-300亿"] += 1
        else:
            size_buckets[">=300亿"] += 1

    report = {
        "version": "V2",
        "analysis_date": today.isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "cutoff_date": cutoff,
        "minimum_consecutive_limit_up": MIN_CONSECUTIVE_LIMIT_UP,
        "positive_samples": len(positive_rows),
        "unique_positive_stocks": len({r["code"] for r in positive_rows}),
        "control_samples": len(controls),
        "common_ratio_threshold": COMMON_RATIO,
        "minimum_positive_samples": MIN_POSITIVE_SAMPLES,
        "common_patterns_80pct": common[:50],
        "discriminating_patterns": discriminating[:30],
        "current_circ_mcap_bucket_count": dict(size_buckets),
        "data_sources": {
            "daily_kline_price_volume": "Eastmoney public daily K-line",
            "historical_turnover_rate": "Eastmoney daily K-line turnover field",
            "current_float_mcap": "Sina current ranking snapshot; static cross-sectional label only",
        },
        "method_notes": [
            "正样本：主板非ST股票，连续至少2个涨停的第一板作为启动日。",
            "启动日前特征仅使用T-1及更早日K。",
            "对照组从相同启动日随机抽取同样主板股票，避免把大盘日期效应误当成个股规律。",
            "80%覆盖率只有在对照组覆盖明显更低时才进入discriminating_patterns。",
            "当前流通市值不是历史市值，因此不参与启动规律判定。",
        ],
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "latest_limitup_events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "latest_feature_rows.json").write_text(json.dumps(positive_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "latest_control_rows.json").write_text(json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "latest_pattern_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
