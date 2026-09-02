"""Build the independent pre-open review from the frozen tail snapshot plus overnight news."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from news_crawler import crawl_overnight_news, match_candidates

BEIJING = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def overnight_window(now_utc: datetime) -> tuple[datetime, datetime]:
    """Previous trading-day 15:30 Beijing through current 09:30 Beijing.

    The workflow runs around 09:10 Beijing, so the end is deliberately fixed at
    09:30 rather than 'now'. News posted after 09:30 must never enter this review.
    """
    now_bj = now_utc.astimezone(BEIJING)
    today = now_bj.date()
    start_bj = datetime.combine(today - timedelta(days=1), time(15, 30), tzinfo=BEIJING)
    end_bj = datetime.combine(today, time(9, 30), tzinfo=BEIJING)
    return start_bj.astimezone(UTC), end_bj.astimezone(UTC)


def _load_tail() -> list[dict]:
    p = Path("data/daily_candidates.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _impact_delta(title: str) -> int:
    rules = {
        15: ("重大订单", "中标", "签署重大合同", "大额合同"),
        12: ("业绩预增", "业绩快报", "净利润增长", "获批", "核准", "产品涨价"),
        8: ("订单", "合同", "签约", "项目"),
        -18: ("立案", "监管", "处罚", "预亏", "大额减持", "风险提示"),
        -12: ("减持", "诉讼", "亏损"),
    }
    for delta, words in rules.items():
        if any(w in title for w in words):
            return delta
    return 3


def build_preopen_review(tail_rows: list[dict], matched_news: list[dict], window_start: datetime, window_end: datetime) -> dict:
    by_code: dict[str, list[dict]] = {}
    for item in matched_news:
        for candidate in item.get("candidates", []):
            by_code.setdefault(str(candidate.get("code", ""))[:6], []).append(item)

    rows = []
    for tail in tail_rows:
        code = str(tail.get("code", ""))[:6]
        news = by_code.get(code, [])
        delta = max(-20, min(20, sum(_impact_delta(str(item.get("title", ""))) for item in news)))
        tail_score = float(tail.get("score", 0.0))
        preopen_score = max(0.0, min(100.0, tail_score + delta))
        rows.append({
            "code": code,
            "name": tail.get("name", ""),
            "tail_decision": tail.get("decision"),
            "tail_score": tail_score,
            "preopen_score": round(preopen_score, 2),
            "score_change": round(preopen_score - tail_score, 2),
            "change_status": "UPGRADE" if delta >= 5 else "DOWNGRADE" if delta <= -5 else "UNCHANGED",
            "overnight_news_count": len(news),
            "overnight_news": news[:8],
            "auction_action": "WAIT_FOR_AUCTION",
        })
    rows.sort(key=lambda x: x["preopen_score"], reverse=True)
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "window_start_utc": window_start.isoformat(),
        "window_end_utc": window_end.isoformat(),
        "tail_snapshot": "data/daily_candidates.json",
        "principle": "Tail snapshot is immutable; this file shows only overnight deltas and pre-open review.",
        "rows": rows,
    }


def main() -> None:
    tail_rows = _load_tail()
    if not tail_rows:
        print("no frozen tail candidates; skip pre-open review")
        return

    now_utc = datetime.now(UTC)
    start_utc, end_utc = overnight_window(now_utc)
    news = crawl_overnight_news(start_utc=start_utc, end_utc=end_utc)
    matched = match_candidates(news, tail_rows)

    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/overnight_news.json").write_text(json.dumps({
        "window_start_utc": start_utc.isoformat(),
        "window_end_utc": end_utc.isoformat(),
        "raw_count": len(news),
        "matched_count": len(matched),
        "items": matched,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    review = build_preopen_review(tail_rows, matched, start_utc, end_utc)
    Path("data/preopen_review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"tail={len(tail_rows)} overnight={len(news)} matched={len(matched)}")


if __name__ == "__main__":
    main()
