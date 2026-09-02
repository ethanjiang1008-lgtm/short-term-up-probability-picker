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

    print(
        "scan_status="
        f"{diagnostics['status']} "
        f"raw={diagnostics['raw_market_rows']} "
        f"eligible={diagnostics['final_universe_rows']} "
        f"fast_kline={diagnostics['fast_kline_success']}/{diagnostics['fast_kline_requested']} "
        f"full_kline={diagnostics['full_kline_success']}/{diagnostics['full_kline_requested']}"
    )
    if diagnostics.get("blocking_reason"):
        print(f"blocking_reason={diagnostics['blocking_reason']}")
    print(f"saved {len(rows)} candidates")


if __name__ == "__main__":
    main()
