"""Daily candidate snapshot helpers."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Iterable

def save_snapshot(rows: Iterable[dict], root: str = "data/history") -> Path:
    rows = list(rows)
    if not rows:
        raise ValueError("empty snapshot")
    day = str(rows[0].get("date"))
    if not day or day == "None":
        raise ValueError("snapshot rows require date")
    out = Path(root) / f"{day}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return out

def load_snapshots(root: str = "data/history") -> list[dict]:
    rows: list[dict] = []
    for path in sorted(Path(root).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows.extend(data)
        except (OSError, json.JSONDecodeError):
            continue
    return rows

def attach_next_day_returns(rows: list[dict], close_by_code: dict[str, float]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        code = str(item.get("code", ""))
        if item.get("today_close") is not None and code in close_by_code:
            tc, nc = float(item["today_close"]), float(close_by_code[code])
            if tc > 0:
                ret = round(nc / tc - 1.0, 6)
                item.update(next_day_return=ret, next_day_up=int(ret > 0), next_day_strong_up=int(ret >= 0.03), next_day_limit_up=int(ret >= 0.099))
        out.append(item)
    return out
