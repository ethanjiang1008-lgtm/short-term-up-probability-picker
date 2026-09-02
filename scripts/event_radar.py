"""事件/公告雷达接口。

第一期先定义统一事件结构，不绑定单一新闻源。核心约束：event_time 必须早于
交易决策时间才能进入尾盘模型；盘后事件只能进入次日观察池，避免未来函数。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Event:
    code: str
    title: str
    event_time: datetime
    category: str
    impact_score: float
    source: str


def visible_at(events: list[Event], decision_time: datetime) -> list[Event]:
    return [e for e in events if e.event_time <= decision_time]


def aggregate_event_score(events: list[Event], now: datetime) -> float:
    """将当前时点可见事件映射到 0-100 的简易分数。

    正式版本会加入事件新鲜度、潜在业绩影响、市场关注度、是否主线等特征，
    并使用历史样本校准，而不是把人工分数直接当概率。
    """
    visible = visible_at(events, now)
    if not visible:
        return 0.0
    return min(100.0, max(0.0, max(e.impact_score for e in visible)))
