import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from next_day_update import build_preopen_review, overnight_window

BEIJING = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


def test_overnight_window_uses_trading_dates():
    rows = [{"date": "2026-09-01", "code": "600000", "score": 72}]
    now = datetime(2026, 9, 2, 1, 10, tzinfo=UTC)
    start, end = overnight_window(rows, now)
    assert start.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M") == "2026-09-01 15:30"
    assert end.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M") == "2026-09-02 09:30"


def test_preopen_review_does_not_mutate_tail_score():
    tail = [{"date": "2026-09-01", "code": "600000", "name": "TEST", "score": 72, "decision": "WATCH"}]
    news = [{
        "title": "重大订单落地",
        "source": "TEST",
        "candidates": [{"code": "600000", "name": "TEST"}],
    }]
    result = build_preopen_review(
        tail,
        news,
        datetime(2026, 9, 1, 7, 30, tzinfo=UTC),
        datetime(2026, 9, 2, 1, 30, tzinfo=UTC),
    )
    row = result["rows"][0]
    assert row["tail_score"] == 72
    assert row["preopen_score"] > 72
    assert row["score_change"] > 0
    assert tail[0]["score"] == 72
