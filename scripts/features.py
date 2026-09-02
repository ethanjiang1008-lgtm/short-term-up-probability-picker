"""量价与尾盘特征。

输入只允许包含预测时点已经可见的数据；任何事件/公告字段由上层按时间过滤后传入。
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Sequence


def _f(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def sma(values: Sequence[float], n: int) -> float:
    if len(values) < n or n <= 0:
        return 0.0
    return mean(values[-n:])


def pct_change(a: float, b: float) -> float:
    return (a / b - 1.0) if b else 0.0


@dataclass
class FeatureRow:
    code: str
    name: str
    base_strength: float
    close_strength: float
    sector_strength: float
    event_strength: float
    market_environment: float
    score: float
    evidence: dict[str, float]


def make_features(
    code: str,
    name: str,
    kline: list[dict],
    *,
    sector_change_pct: float = 0.0,
    sector_up_ratio: float = 0.5,
    sector_limit_up_count: int = 0,
    market_breadth: float = 0.5,
    market_limit_up_count: int = 0,
    market_limit_down_count: int = 0,
    event_score: float = 0.0,
) -> FeatureRow:
    if not kline:
        return FeatureRow(code, name, 0, 0, 0, event_score, 0, 0, {})

    closes = [_f(x.get("close")) for x in kline]
    highs = [_f(x.get("high")) for x in kline]
    lows = [_f(x.get("low")) for x in kline]
    volumes = [_f(x.get("volume")) for x in kline]
    turnover = [_f(x.get("turnover")) for x in kline]
    close = closes[-1]

    ma5, ma20, ma60 = sma(closes, 5), sma(closes, 20), sma(closes, 60)
    ret5 = pct_change(close, closes[-6]) if len(closes) >= 6 else 0.0
    ret20 = pct_change(close, closes[-21]) if len(closes) >= 21 else 0.0
    relvol = (mean(volumes[-5:]) / mean(volumes[-20:])) if len(volumes) >= 20 and mean(volumes[-20:]) else 0.0

    day_range = (highs[-1] - lows[-1]) if highs and lows else 0.0
    close_location = (close - lows[-1]) / day_range if day_range else 0.5
    last3_closes = closes[-3:] if len(closes) >= 3 else closes
    tail_accel = pct_change(last3_closes[-1], last3_closes[0]) if len(last3_closes) >= 2 else 0.0

    # 基础强度：趋势 + 中期动量 + 流动性；避免将同一指标重复计分。
    trend = sum([close > ma5, close > ma20, ma20 > ma60]) / 3 if ma60 else 0.5
    momentum = min(1.0, max(0.0, 0.5 + ret20 * 2.5))
    liquidity = min(1.0, max(0.0, 0.5 + (relvol - 1.0) * 0.5))
    base = 100 * (0.5 * trend + 0.35 * momentum + 0.15 * liquidity)

    # 尾盘强度：收盘位置 + 相对量 + 最近价格加速度。
    close_score = 100 * (
        0.4 * close_location
        + 0.3 * min(1.0, max(0.0, relvol / 1.5))
        + 0.3 * min(1.0, max(0.0, 0.5 + tail_accel * 5))
    )

    sector = 100 * (
        0.45 * min(1.0, max(0.0, 0.5 + sector_change_pct / 4.0))
        + 0.35 * min(1.0, max(0.0, sector_up_ratio))
        + 0.20 * min(1.0, sector_limit_up_count / 5.0)
    )

    market = 100 * (
        0.55 * min(1.0, max(0.0, market_breadth))
        + 0.25 * min(1.0, market_limit_up_count / 80.0)
        + 0.20 * (1.0 - min(1.0, market_limit_down_count / 40.0))
    )

    weights = {"base_strength": 0.25, "close_strength": 0.25, "sector_strength": 0.20, "event_strength": 0.20, "market_environment": 0.10}
    score = (
        base * weights["base_strength"]
        + close_score * weights["close_strength"]
        + sector * weights["sector_strength"]
        + max(0.0, min(100.0, event_score)) * weights["event_strength"]
        + market * weights["market_environment"]
    )

    evidence = {
        "ret_5d": ret5,
        "ret_20d": ret20,
        "relative_volume": relvol,
        "close_location": close_location,
        "tail_acceleration": tail_accel,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
    }
    return FeatureRow(code, name, base, close_score, sector, event_score, market, score, evidence)
