"""事件/新闻/公告雷达的数据结构与时点约束。"""
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
    url: str = ""
    summary: str = ""
    published_at: str = ""


def visible_at(events: list[Event], decision_time: datetime) -> list[Event]:
    """只保留在决策时点已经公开的信息，防止未来函数。"""
    return [e for e in events if e.event_time <= decision_time]


def aggregate_event_score(events: list[Event], now: datetime) -> float:
    """将当前时点可见事件映射到 0-100 的简易分数。"""
    visible = visible_at(events, now)
    if not visible:
        return 50.0
    return min(100.0, max(0.0, max(e.impact_score for e in visible)))
