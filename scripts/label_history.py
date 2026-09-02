"""给已保存的候选快照绑定下一交易日真实收盘结果。"""
from __future__ import annotations

import json
from pathlib import Path

from labels import next_day_labels
from market_data_sina import fetch_klines_parallel


def _load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _bar_date(bar: dict) -> str:
    return str(bar.get("date", bar.get("day", bar.get("d", ""))))[:10]


def _bar_close(bar: dict) -> float | None:
    try:
        return float(bar.get("close", bar.get("c")))
    except (TypeError, ValueError):
        return None


def label_history(root: str = "data/history") -> int:
    files = sorted(Path(root).glob("*.json"))
    updated_days = 0
    for path in files:
        rows = _load(path)
        day = path.stem
        pending = [
            r for r in rows
            if r.get("next_day_return") is None and r.get("today_close") is not None and r.get("code")
        ]
        if not pending:
            continue

        codes = [str(r["code"]) for r in pending]
        bars_map, _stats = fetch_klines_parallel(codes, count=10, workers=8)
        changed = False
        for row in pending:
            code = str(row["code"])
            future = []
            for bar in bars_map.get(code, []):
                d = _bar_date(bar)
                c = _bar_close(bar)
                if d and d > day and c is not None:
                    future.append((d, c))
            if not future:
                continue
            tomorrow_close = sorted(future, key=lambda x: x[0])[0][1]
            try:
                labels = next_day_labels(float(row["today_close"]), tomorrow_close)
            except (TypeError, ValueError):
                continue
            row.update(labels)
            row["next_day_close"] = tomorrow_close
            changed = True

        if changed:
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            updated_days += 1
    return updated_days


if __name__ == "__main__":
    print(f"updated_days={label_history()}")
