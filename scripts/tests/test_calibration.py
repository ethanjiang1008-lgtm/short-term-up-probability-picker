import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration_v2 import fit_score_bins, score_to_probability


def test_score_bin_calibration():
    rows = [
        {"score": 82, "next_day_up": 1},
        {"score": 84, "next_day_up": 1},
        {"score": 86, "next_day_up": 0},
    ]
    bins = fit_score_bins(rows)
    p = score_to_probability(83, bins)
    assert p is not None
    assert 0.5 < p < 1.0


def test_empty_bin_is_unavailable():
    bins = fit_score_bins([])
    assert score_to_probability(82, bins) is None
