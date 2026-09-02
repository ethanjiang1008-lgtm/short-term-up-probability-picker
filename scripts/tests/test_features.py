import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features import make_features
from labels import next_day_labels
from scanner import _eligible, _derived_volume_ratio, _is_rising_5ma


def _bars(n=70):
    rows = []
    for i in range(n):
        close = 10 + i * 0.05
        rows.append({
            "date": f"2026-08-{(i % 28) + 1:02d}",
            "open": close - 0.02,
            "high": close + 0.10,
            "low": close - 0.05,
            "close": close,
            "volume": 1000 + i * 5,
            "turnover": 3.0,
        })
    return rows


def test_feature_score_range():
    f = make_features("600000", "TEST", _bars(), sector_change_pct=2.0, sector_up_ratio=0.8, sector_limit_up_count=4, market_breadth=0.65, market_limit_up_count=50, event_score=80)
    assert 0 <= f.score <= 100
    assert 0 <= f.close_strength <= 100


def test_labels():
    y = next_day_labels(10, 10.5)
    assert y["next_day_up"] == 1
    assert y["next_day_strong_up"] == 1
    assert y["next_day_return"] == 0.05


def test_universe_filters():
    base = {"price": 20, "circ_mcap": 10_000_000, "turnover_rate": 5.0}
    assert _eligible({**base, "code": "600000", "name": "浦发银行"})
    assert not _eligible({**base, "code": "688001", "name": "科创测试"})
    assert not _eligible({**base, "code": "300001", "name": "创业测试"})
    assert not _eligible({**base, "code": "600001", "name": "ST测试"})
    assert not _eligible({**base, "code": "600002", "name": "退市测试"})
    assert not _eligible({**base, "code": "600003", "name": "高价股", "price": 50.01})
    assert not _eligible({**base, "code": "600004", "name": "小市值", "circ_mcap": 199_999})
    assert not _eligible({**base, "code": "600005", "name": "高换手", "turnover_rate": 10.0})


def test_five_ma_rising_and_volume_ratio():
    bars = _bars(10)
    assert _is_rising_5ma(bars)
    assert _derived_volume_ratio(bars) < 5

    flat = [dict(bars[-1], close=10.0) for _ in range(6)]
    assert not _is_rising_5ma(flat)
