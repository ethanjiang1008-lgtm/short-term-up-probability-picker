"""Second-stage update: preserve the tail snapshot and create a separate pre-open view."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from news_crawler import crawl_overnight_news, match_candidates


def _load_candidates() -> list[dict]:
    p = Path("data/daily_candidates.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _event_delta(item: dict) -> int:
    title = str(item.get("title", ""))
    keywords = {
        "order": ("订单", "中标", "合同", "签约", "交付"),
        "earnings": ("业绩", "预增", "预盈", "扭亏", "利润", "营收"),
        "policy": ("政策", "规划", "指导意见", "发布", "获批", "核准"),
        "price": ("涨价", "提价", "价格上调", "调价"),
        "negative": ("减持", "预亏", "亏损", "处罚", "监管", "诉讼", "立案", "风险"),
    }
    for group, words in keywords.items():
        if any(w in title for w in words):
            return -15 if group == "negative" else 12
    return 5


def build_preopen_view(candidates: list[dict], matched_news: list[dict], captured_at_utc: str) -> list[dict]:
    by_code: dict[str, list[dict]] = {}
    for item in matched_news:
        for c in item.get("candidates", []):
            by_code.setdefault(str(c.get("code", ""))[:6], []).append(item)

    rows: list[dict] = []
    for candidate in candidates:
        code = str(candidate.get("code", ""))[:6]
        news = by_code.get(code, [])
        delta = sum(_event_delta(item) for item in news)
        base_score = float(candidate.get("score", 0.0))
        # Second-stage score is deliberately separate from the immutable tail score.
        preopen_score = max(0.0, min(100.0, base_score + max(-20, min(20, delta))))
        change = round(preopen_score - base_score, 2)
        status = "UPGRADE" if change >= 5 else "DOWNGRADE" if change <= -5 else "UNCHANGED"
        rows.append({
            **candidate,
            "tail_snapshot_date": candidate.get("date"),
            "tail_score_frozen": base_score,
            "preopen_review": {
                "reviewed_at_utc": captured_at_utc,
                "overnight_news_count": len(news),
                "score_delta": change,
                "preopen_score": round(preopen_score, 2),
                "status": status,
                "news": news,
            },
        })
    return sorted(rows, key=lambda r: r["preopen_review"]["preopen_score"], reverse=True)


def main() -> None:
    candidates = _load_candidates()
    if not candidates:
        print("no tail candidates; skip overnight update")
        return
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=18)
    raw = crawl_overnight_news(start_utc=start, end_utc=now)
    matched = match_candidates(raw, candidates)
    captured = now.isoformat()
    out = Path("data/preopen_review.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "tail_snapshot": "data/daily_candidates.json",
        "window_start_utc": start.isoformat(),
        "window_end_utc": captured,
        "source_items": len(raw),
        "matched_items": len(matched),
        "rows": build_preopen_view(candidates, matched, captured),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"overnight raw={len(raw)} matched={len(matched)}")


if __name__ == "__main__":
    main()
