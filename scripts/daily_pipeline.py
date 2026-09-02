"""每日候选快照：保存全量候选池，并单独输出重点观察名单。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from history import save_snapshot
from scanner import scan_with_diagnostics


HISTORY_SNAPSHOT_ROWS = 100


def main() -> None:
    rows, diagnostics = scan_with_diagnostics()
    captured_at = datetime.now(timezone.utc).isoformat()
    diagnostics["captured_at_utc"] = captured_at
    for row in rows:
        row["captured_at_utc"] = captured_at

    focus_rows = [r for r in rows if r.get("is_focus")]
    focus_rows.sort(key=lambda x: (-float(x.get("score", 0)), x.get("code", "")))
    focus_rows = focus_rows[:50]

    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "scan_diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (data_dir / "daily_candidates.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (data_dir / "focus_candidates.json").write_text(
        json.dumps(focus_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 历史标签暂保留当日最有交易意义的前100条，避免每天对数千只股票重复拉取次日K线。
    # 全量当前候选池仍完整保存在 daily_candidates.json。
    if rows:
        save_snapshot(rows[:HISTORY_SNAPSHOT_ROWS])

    print(
        "scan_status="
        f"{diagnostics['status']} "
        f"raw={diagnostics['raw_market_rows']} "
        f"eligible={diagnostics['final_universe_rows']} "
        f"full_kline={diagnostics['full_kline_success']}/{diagnostics['full_kline_requested']} "
        f"focus={len(focus_rows)} "
        f"limit_up={diagnostics.get('limit_up_rows', 0)}"
    )
    if diagnostics.get("blocking_reason"):
        print(f"blocking_reason={diagnostics['blocking_reason']}")
    print(f"saved full_pool={len(rows)} focus={len(focus_rows)}")


if __name__ == "__main__":
    main()
