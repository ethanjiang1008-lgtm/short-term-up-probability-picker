"""严格按时间顺序评估候选分数与次日表现。

本模块不随机打乱样本。实际生产回测时，应把每天的候选快照与次日真实结果
保存成一行，然后按日期滚动评估。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class BacktestResult:
    samples: int
    wins: int
    win_rate: float
    avg_return: float
    avg_win: float
    avg_loss: float
    expectancy: float


def evaluate(rows: Iterable[dict], score_threshold: float = 60.0) -> BacktestResult:
    selected = [r for r in rows if float(r.get("score", 0)) >= score_threshold and r.get("next_day_return") is not None]
    returns = [float(r["next_day_return"]) for r in selected]
    if not returns:
        return BacktestResult(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    win_rate = len(wins) / len(returns)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    return BacktestResult(len(returns), len(wins), win_rate, sum(returns) / len(returns), avg_win, avg_loss, expectancy)


def walk_forward(rows: list[dict], train_days: int = 60) -> list[BacktestResult]:
    """按日期排序后，使用过去窗口评估下一时段；这里只提供框架，不做模型拟合。"""
    dates = sorted({str(r["date"]) for r in rows})
    results: list[BacktestResult] = []
    for i in range(train_days, len(dates)):
        eval_date = dates[i]
        history = [r for r in rows if str(r["date"]) < eval_date]
        test = [r for r in rows if str(r["date"]) == eval_date]
        if not history or not test:
            continue
        results.append(evaluate(test))
    return results


if __name__ == "__main__":
    print("backtest module ready; feed daily candidate snapshots with next_day_return")
