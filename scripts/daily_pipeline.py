"""每日候选快照：只负责扫描、补齐今日收盘价、保存历史。"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

from scanner import scan
from history import save_snapshot
from market_data_sina import fetch_all_gainers


def main() -> None:
    rows = scan()
    quotes = {
        str(r.get("code")): float(r.get("trade", r.get("price", 0)) or 0)
        for r in fetch_all_gainers()
    }
    for row in rows:
        row["today_close"] = quotes.get(str(row["code"]))
        row["captured_at_utc"] = datetime.now(timezone.utc).isoformat()

    out = Path("data/daily_candidates.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if rows:
        save_snapshot(rows)
    print(f"saved {len(rows)} candidates")


if __name__ == "__main__":
    main()
