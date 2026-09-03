"""Weekly model review from frozen prediction snapshots and their next-day outcomes."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def collect_validation() -> list[dict]:
    rows = []
    for path in sorted(Path("data/validation").glob("*.json")):
        data = load_json(path)
        if isinstance(data, list):
            rows.extend(data)
    return rows


def main() -> None:
    rows = collect_validation()
    if not rows:
        print("weekly_review_samples=0")
        return

    total = len(rows)
    wins = sum(r.get("次日上涨") == "是" for r in rows)
    strong = sum(r.get("次日≥3%") == "是" for r in rows)
    avg_ret = sum(float(r.get("次日收益%", 0) or 0) for r in rows) / total
    by_band = defaultdict(list)
    for r in rows:
        try: score = float(r.get("Score"))
        except (TypeError, ValueError): continue
        if score < 64: band = "<64"
        elif score < 68: band = "64-67.9"
        elif score < 72: band = "68-71.9"
        elif score < 76: band = "72-75.9"
        else: band = "76+"
        by_band[band].append(r)

    wb = Workbook()
    ws = wb.active
    ws.title = "周度总览"
    ws.append(["指标", "值"])
    ws.append(["验证样本", total])
    ws.append(["次日上涨", wins])
    ws.append(["次日胜率", round(wins / total, 4)])
    ws.append(["次日≥3%", strong])
    ws.append(["平均次日收益%", round(avg_ret, 4)])

    wb2 = wb.create_sheet("Score分段")
    wb2.append(["Score区间", "样本", "胜率", "平均收益%"])
    for band in ["<64", "64-67.9", "68-71.9", "72-75.9", "76+"]:
        subset = by_band.get(band, [])
        n = len(subset)
        if not n:
            wb2.append([band, 0, None, None])
            continue
        w = sum(x.get("次日上涨") == "是" for x in subset)
        a = sum(float(x.get("次日收益%", 0) or 0) for x in subset) / n
        wb2.append([band, n, round(w / n, 4), round(a, 4)])

    out = Path("reports") / "weekly_model_review.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    for sheet in wb.worksheets:
        sheet.freeze_panes = "A2"
        for c in sheet[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="D9EAF7")
            c.alignment = Alignment(horizontal="center")
    wb.save(out)
    summary = {"samples": total, "win_rate": round(wins / total, 4), "avg_return_pct": round(avg_ret, 4), "strong_up_count": strong}
    Path("reports/weekly_model_review.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"weekly_review_samples={total} win_rate={summary['win_rate']} avg_return_pct={summary['avg_return_pct']}")


if __name__ == "__main__":
    main()
