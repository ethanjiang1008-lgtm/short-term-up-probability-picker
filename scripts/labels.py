"""次日标签定义。"""
from __future__ import annotations


def next_day_labels(today_close: float, tomorrow_close: float, limit_up_return: float = 0.099) -> dict[str, float | int]:
    if today_close <= 0:
        raise ValueError("today_close must be positive")
    ret = tomorrow_close / today_close - 1.0
    return {
        "next_day_return": ret,
        "next_day_up": int(ret > 0.0),
        "next_day_strong_up": int(ret >= 0.03),
        "next_day_limit_up": int(ret >= limit_up_return),
    }
