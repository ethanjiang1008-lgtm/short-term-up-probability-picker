"""Build the second-stage pre-open decision snapshot from tail candidates and news."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    candidates_path = Path("data/daily_candidates.json")
    news_path = Path("data/evening_news.json")
    candidates = json.loads(candidates_path.read_text(encoding="utf-8")) if candidates_path.exists() else []
    news = json.loads(news_path.read_text(encoding="utf-8")) if news_path.exists() else {"items": []}

    matched: dict[str, list[dict]] = {}
    for item in news.get("items", []):
        for c in item.get("candidates", []):
            matched.setdefault(str(c.get("code")), []).append(item)

    rows = []
    for c in candidates:
        code = str(c.get("code", ""))
        items = matched.get(code, [])
        row = dict(c)
        row["evening_news_count"] = len(items)
        row["evening_news"] = items[:5]
        # Keep the decision explicit: auction confirmation is still manual until
        # a reliable pre-open quote source is added.
        row["next_day_status"] = "AUCTION_CONFIRM"
        rows.append(row)

    out = Path("data/next_day_watch.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "decision_note": "Tail score + public evening news; auction confirmation remains required.",
                "candidates": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"saved {len(rows)} next-day watch rows")


if __name__ == "__main__":
    main()
