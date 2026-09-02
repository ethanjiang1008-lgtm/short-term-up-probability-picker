"""基于历史样本把 score 映射为经验上涨概率。"""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class ScoreBin:
    low: float
    high: float
    samples: int
    wins: int
    @property
    def probability(self) -> float:
        return (self.wins + 1.0) / (self.samples + 2.0)

def fit_score_bins(rows: list[dict], edges: tuple[float, ...] = (0, 50, 60, 65, 70, 75, 80, 85, 90, 101)) -> list[ScoreBin]:
    bins: list[ScoreBin] = []
    for low, high in zip(edges[:-1], edges[1:]):
        selected = [r for r in rows if low <= float(r.get("score", 0)) < high and r.get("next_day_up") is not None]
        wins = sum(int(float(r["next_day_up"]) > 0) for r in selected)
        bins.append(ScoreBin(low, high, len(selected), wins))
    return bins

def score_to_probability(score: float, bins: list[ScoreBin]) -> float | None:
    for b in bins:
        if b.low <= score < b.high:
            return b.probability if b.samples else None
    return None
