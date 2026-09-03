import json
from pathlib import Path

from scanner import _eligible, _is_rising_5ma, _observation_label


def test_is_rising_5ma():
    rising = [{"close": 8}, {"close": 9}, {"close": 10}, {"close": 11}, {"close": 12}, {"close": 14}]
    flat = [{"close": 8}, {"close": 9}, {"close": 10}, {"close": 11}, {"close": 12}, {"close": 8}]
    assert _is_rising_5ma(rising)
    assert not _is_rising_5ma(flat)


def test_eligible_without_5ma_filter():
    base = {"price": 20, "circ_mcap": 10_000_000, "turnover_rate": 5.0, "code": "600006", "name": "无趋势测试"}
    assert _eligible(base)


def test_observation_tiers():
    technical = {"ma5_rising": True, "close_above_ma20": True, "ma_bull_alignment": True}
    label, focus, reason = _observation_label(80, False, technical)
    assert label == "重点观察"
    assert focus is True
    assert "趋势确认" in reason

    label, focus, _ = _observation_label(90, True, technical)
    assert label == "涨停观察"
    assert focus is False

    insufficient_technical = {"ma5_rising": True, "close_above_ma20": False, "ma_bull_alignment": False}
    label, focus, _ = _observation_label(70, False, insufficient_technical)
    assert label == "次重点"
    assert focus is False
