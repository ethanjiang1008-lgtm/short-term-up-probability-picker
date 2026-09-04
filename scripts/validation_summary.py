"""Normalize validation summary metrics so headline win rate only covers focus stocks."""
from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    summary_path = Path("data/validation/latest_validation.json")
    if not summary_path.exists():
        return

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    prediction_date = str(summary.get("prediction_date") or "")
    if not prediction_date:
        return

    detail_path = Path("data/validation") / f"{prediction_date}_validation.json"
    if not detail_path.exists():
        return

    results = json.loads(detail_path.read_text(encoding="utf-8"))
    focus_results = [r for r in results if r.get("重点观察") == "是"]
    focus_samples = len(focus_results)
    focus_wins = sum(r.get("次日上涨") == "是" for r in focus_results)

    summary["win_rate_scope"] = "重点观察"
    summary["samples"] = focus_samples
    summary["wins"] = focus_wins
    summary["losses"] = focus_samples - focus_wins
    summary["win_rate"] = round(focus_wins / focus_samples, 4) if focus_samples else None
    summary["avg_return_pct"] = (
        round(sum(float(r.get("次日收益%", 0) or 0) for r in focus_results) / focus_samples, 4)
        if focus_samples
        else None
    )
    summary["strong_up_count"] = sum(r.get("次日≥3%") == "是" for r in focus_results)
    summary["limit_up_count"] = sum(r.get("次日涨停") == "是" for r in focus_results)
    summary["focus_samples"] = focus_samples
    summary["focus_wins"] = focus_wins
    summary["focus_win_rate"] = summary["win_rate"]

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"validation_win_rate_scope=重点观察 samples={focus_samples} "
        f"wins={focus_wins} win_rate={summary['win_rate']}"
    )


if __name__ == "__main__":
    main()
