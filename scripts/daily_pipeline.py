"""每日候选快照：扫描、保存，并持久化数据完整性诊断。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from history import save_snapshot
from scanner import scan_with_diagnostics


def main() -> None:
    rows, diagnostics = scan_with_diagnostics()
    captured_at = datetime.now(timezone.utc).isoformat()
    diagnostics["captured_at_utc"] = captured_at
    for row in rows:
        row["captured_at_utc"] = captured_at

    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "scan_diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (data_dir / "daily_candidates.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if rows:
        save_snapshot(rows)
    print(f"scan_status={diagnostics['status']} raw={diagnostics['raw_market_rows']} eligible={diagnostics['eligible_rows']} kline={diagnostics['kline_success']}/{diagnostics['kline_requested']}")
    print(f"saved {len(rows)} candidates")


if __name__ == "__main__":
    main()
