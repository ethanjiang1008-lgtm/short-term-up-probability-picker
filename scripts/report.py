"""输出不同 score 阈值的真实次日胜率和收益。"""
from __future__ import annotations

import json
from pathlib import Path

from backtest import evaluate
from history import load_snapshots

THRESHOLDS = (60, 65, 68, 70, 75)
MIN_SAMPLES = 30


def _as_dict(result) -> dict:
    if result.samples == 0:
        return {
            "samples": 0,
            "wins": 0,
            "win_rate": None,
            "avg_return": None,
            "avg_win": None,
            "avg_loss": None,
            "expectancy": None,
            "sample_ok": False,
        }
    return {
        "samples": result.samples,
        "wins": result.wins,
        "win_rate": round(result.win_rate, 4),
        "avg_return": round(result.avg_return, 6),
        "avg_win": round(result.avg_win, 6),
        "avg_loss": round(result.avg_loss, 6),
        "expectancy": round(result.expectancy, 6),
        "sample_ok": result.samples >= MIN_SAMPLES,
    }


def build_report(rows: list[dict]) -> dict:
    labeled = [r for r in rows if r.get("next_day_return") is not None]
    report = {
        "labeled_samples": len(labeled),
        "min_samples": MIN_SAMPLES,
        "thresholds": {},
        "best_threshold_by_expectancy": None,
    }
    candidates = []
    for threshold in THRESHOLDS:
        item = _as_dict(evaluate(rows, score_threshold=float(threshold)))
        report["thresholds"][str(threshold)] = item
        if item["sample_ok"] and item["expectancy"] is not None:
            candidates.append((item["expectancy"], threshold))
    if candidates:
        report["best_threshold_by_expectancy"] = max(candidates)[1]
    return report


def main() -> None:
    report = build_report(load_snapshots())
    out = Path("data/backtest_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
