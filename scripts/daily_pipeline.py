"""每日候选快照：扫描、保存，不重复请求同一行情。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from history import save_snapshot
from scanner import scan


def main() -> None:
    rows = scan()
    captured_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        row["captured_at_utc"] = captured_at

    out = Path("data/daily_candidates.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if rows:
        save_snapshot(rows)
    print(f"saved {len(rows)} candidates")


if __name__ == "__main__":
    main()
