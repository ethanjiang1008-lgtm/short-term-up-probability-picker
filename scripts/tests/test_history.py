import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from history import attach_next_day_returns


def test_attach_next_day_returns():
    rows = [{"code": "600000", "today_close": 10.0}]
    result = attach_next_day_returns(rows, {"600000": 10.5})
    assert result[0]["next_day_return"] == 0.05
    assert result[0]["next_day_up"] == 1
    assert result[0]["next_day_strong_up"] == 1


def test_attach_does_not_require_other_stocks():
    rows = [{"code": "600000", "today_close": 10.0}, {"code": "000001", "today_close": 20.0}]
    result = attach_next_day_returns(rows, {"600000": 9.9})
    assert result[0]["next_day_up"] == 0
    assert "next_day_return" not in result[1]
