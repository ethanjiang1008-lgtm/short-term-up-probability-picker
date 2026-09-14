"""过去一年连板股票的启动前共同规律挖掘器。

目标：从主板过去一年真实出现的 >=2 连板事件中，回溯第一板前 N 个交易日，
统计量价、市值、换手、趋势、波动、突破等特征，寻找 >=80% 样本共同满足的条件。

所有特征均只使用启动日前已经完成的日K；避免把连板当天及之后的数据泄漏到启动前特征。
"""
from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from market_data_sina import fetch_all_stocks, fetch_kline, is_main_board

LOOKBACK_DAYS = 370
KLINE_COUNT = 320
MIN_CONSECUTIVE_LIMIT_UP = 2
PRESTART_WINDOWS = (1, 3, 5, 10, 20)
COMMON_RATIO = 0.80
MIN_SAMPLE_COUNT = 20
OUTPUT_DIR = Path("data/pattern_mining")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        if math.isfinite(x):
            return x
        return default
    except (TypeError, ValueError):
        return default


def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100 if b else 0.0


def _is_limit_up(bar: dict[str, Any], prev: dict[str, Any], name: str = "") -> bool:
    """主板非ST默认按10%涨停判定，并允许价格四舍五入造成的小误差。"""
    if name.upper().startswith("ST") or "退" in name:
        return False
    prev_close = _f(prev.get("close"))
    close = _f(bar.get("close"))
    if prev_close <= 0 or close <= 0:
        return False
    change = close / prev_close - 1.0
    return change >= 0.095


def _rolling_mean(values: list[float], n: int) -> float:
    if len(values) < n:
        return 0.0
    return statistics.fmean(values[-n:])


def _max_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for x in values:
        peak = max(peak, x)
        if peak > 0:
            worst = min(worst, x / peak - 1.0)
    return worst * 100


def build_feature_row(
    code: str,
    name: str,
    bars: list[dict[str, Any]],
    first_limit_idx: int,
) -> dict[str, Any] | None:
    # first_limit_idx 本身不能进入任何启动前特征。
    end = first_limit_idx
    if end < 20:
        return None
    pre = bars[:end]
    close = _f(pre[-1].get("close"))
    if close <= 0:
        return None

    closes = [_f(x.get("close")) for x in pre]
    volumes = [_f(x.get("volume")) for x in pre]
    turnovers = [_f(x.get("turnover")) for x in pre]
    highs = [_f(x.get("high")) for x in pre]
    lows = [_f(x.get("low")) for x in pre]

    def ret(n: int) -> float:
        return _pct(close, closes[-n - 1]) if len(closes) > n else 0.0

    ma5 = _rolling_mean(closes, 5)
    ma10 = _rolling_mean(closes, 10)
    ma20 = _rolling_mean(closes, 20)
    ma60 = _rolling_mean(closes, 60)
    vol5 = _rolling_mean(volumes, 5)
    vol20 = _rolling_mean(volumes, 20)
    tr_today = (highs[-1] - lows[-1]) / close * 100 if close else 0.0
    close_pos = ((close - lows[-1]) / (highs[-1] - lows[-1])) if highs[-1] > lows[-1] else 0.5
    recent20 = closes[-20:]
    high20 = max(recent20) if recent20 else close
    low20 = min(recent20) if recent20 else close

    return {
        "code": code,
        "name": name,
        "start_date": bars[first_limit_idx].get("date", ""),
        "prev_date": pre[-1].get("date", ""),
        "start_price": _f(bars[first_limit_idx].get("close")),
        "pre_close": close,
        "ret_1d": ret(1),
        "ret_3d": ret(3),
        "ret_5d": ret(5),
        "ret_10d": ret(10),
        "ret_20d": ret(20),
        "turnover_1d": turnovers[-1] if turnovers else 0.0,
        "turnover_3d_avg": statistics.fmean(turnovers[-3:]) if len(turnovers) >= 3 else 0.0,
        "turnover_5d_avg": statistics.fmean(turnovers[-5:]) if len(turnovers) >= 5 else 0.0,
        "volume_ratio_5v20": vol5 / vol20 if vol20 else 0.0,
        "close_above_ma5": close > ma5 if ma5 else False,
        "close_above_ma10": close > ma10 if ma10 else False,
        "close_above_ma20": close > ma20 if ma20 else False,
        "ma5_gt_ma10": ma5 > ma10 if ma10 else False,
        "ma10_gt_ma20": ma10 > ma20 if ma20 else False,
        "ma20_gt_ma60": ma20 > ma60 if ma60 else False,
        "near_high20_pct": _pct(close, high20),
        "above_low20_pct": _pct(close, low20),
        "range_1d_pct": tr_today,
        "close_position_1d": close_pos,
        "up_days_5": sum(closes[i] > closes[i - 1] for i in range(max(1, len(closes) - 5), len(closes))),
        "max_drawdown_20d_pct": _max_drawdown(closes[-20:]),
        # 当前流通市值是历史时点不可得的“静态尺寸代理”，单独标注，避免伪装成历史值。
        "circ_mcap_billion_current": None,
    }


def _events_for_stock(bars: list[dict[str, Any]], name: str) -> list[int]:
    out: list[int] = []
    i = 1
    while i < len(bars):
        if _is_limit_up(bars[i], bars[i - 1], name):
            j = i + 1
            while j < len(bars) and _is_limit_up(bars[j], bars[j - 1], name):
                j += 1
            if j - i >= MIN_CONSECUTIVE_LIMIT_UP:
                out.append(i)
            i = j
        else:
            i += 1
    return out


def classify_patterns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对数值特征同时测试多个合理阈值，选择覆盖率 >=80%的最强区间。"""
    if not rows:
        return []
    specs = {
        "ret_5d": [(-999, -5), (-5, 0), (0, 3), (3, 5), (5, 10), (10, 999)],
        "ret_10d": [(-999, -10), (-10, -3), (-3, 3), (3, 8), (8, 15), (15, 999)],
        "ret_20d": [(-999, -15), (-15, -5), (-5, 5), (5, 10), (10, 20), (20, 999)],
        "turnover_1d": [(0, 1), (1, 2), (2, 3), (3, 5), (5, 8), (8, 12), (12, 999)],
        "turnover_5d_avg": [(0, 1), (1, 2), (2, 3), (3, 5), (5, 8), (8, 12), (12, 999)],
        "volume_ratio_5v20": [(0, 0.7), (0.7, 1.0), (1.0, 1.2), (1.2, 1.5), (1.5, 2.0), (2.0, 999)],
        "near_high20_pct": [(-30, -10), (-10, -5), (-5, -2), (-2, 0), (0, 2), (2, 5), (5, 10), (10, 999)],
        "max_drawdown_20d_pct": [(-999, -20), (-20, -15), (-15, -10), (-10, -7), (-7, -3), (-3, 0)],
        "up_days_5": [(0, 2), (2, 3), (3, 4), (4, 6)],
    }
    bools = [
        "close_above_ma5", "close_above_ma10", "close_above_ma20",
        "ma5_gt_ma10", "ma10_gt_ma20", "ma20_gt_ma60",
    ]
    result: list[dict[str, Any]] = []
    total = len(rows)
    for key, intervals in specs.items():
        best = None
        for lo, hi in intervals:
            matched = sum(lo <= _f(r.get(key)) < hi for r in rows)
            ratio = matched / total
            if ratio >= COMMON_RATIO and (best is None or ratio > best["coverage"] or (ratio == best["coverage"] and (hi - lo) < (best["upper"] - best["lower"]))):
                best = {"feature": key, "type": "range", "lower": lo, "upper": hi, "coverage": round(ratio, 4), "matched": matched, "total": total}
        if best:
            result.append(best)
    for key in bools:
        matched = sum(bool(r.get(key)) for r in rows)
        ratio = matched / total
        if ratio >= COMMON_RATIO:
            result.append({"feature": key, "type": "boolean", "value": True, "coverage": round(ratio, 4), "matched": matched, "total": total})
    result.sort(key=lambda x: (-x["coverage"], x["feature"]))
    return result


def main() -> None:
    today = date.today()
    cutoff = today - timedelta(days=LOOKBACK_DAYS)
    cutoff_str = cutoff.isoformat()
    print(f"pattern_mining_cutoff={cutoff_str}")

    universe = [r for r in fetch_all_stocks() if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    print(f"universe={len(universe)}")

    events: list[dict[str, Any]] = []
    feature_rows: list[dict[str, Any]] = []

    def work(row: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
        code = str(row.get("code", ""))
        name = str(row.get("name", ""))
        try:
            return code, name, fetch_kline(code, KLINE_COUNT)
        except Exception as exc:
            print(f"kline_failed={code} {exc}")
            return code, name, []

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(work, r) for r in universe]
        for idx, fut in enumerate(as_completed(futures), start=1):
            code, name, bars = fut.result()
            if idx % 200 == 0:
                print(f"progress={idx}/{len(futures)}")
            if not bars:
                continue
            bars = [b for b in bars if str(b.get("date", "")) >= cutoff_str]
            for start_idx in _events_for_stock(bars, name):
                row = build_feature_row(code, name, bars, start_idx)
                if row:
                    events.append({"code": code, "name": name, "start_date": row["start_date"]})
                    feature_rows.append(row)

    # 同一股票同一段连板只保留一个样本；不同月份可重复进入样本。
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in feature_rows:
        dedup[(row["code"], row["start_date"])] = row
    feature_rows = list(dedup.values())

    # 当前市值只作为补充横截面标签：不影响启动前历史规律本身。
    try:
        current_by_code = {str(r.get("code")): _f(r.get("circ_mcap")) / 1e8 for r in universe}
        for r in feature_rows:
            r["circ_mcap_billion_current"] = current_by_code.get(r["code"])
    except Exception:
        pass

    common = classify_patterns(feature_rows)
    size_buckets = Counter()
    for r in feature_rows:
        mcap = r.get("circ_mcap_billion_current")
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
        "analysis_date": today.isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "cutoff_date": cutoff_str,
        "minimum_consecutive_limit_up": MIN_CONSECUTIVE_LIMIT_UP,
        "sample_count": len(feature_rows),
        "unique_stocks": len({r["code"] for r in feature_rows}),
        "common_ratio_threshold": COMMON_RATIO,
        "common_patterns": common[:30],
        "current_circ_mcap_bucket_count": dict(size_buckets),
        "note": "流通市值为当前值，仅做补充，不参与历史启动规律的判定；历史特征全部不使用启动当日及之后K线。",
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "latest_limitup_events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "latest_feature_rows.json").write_text(json.dumps(feature_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "latest_pattern_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
