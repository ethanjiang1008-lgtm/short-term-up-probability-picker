import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration import calibrate_by_score


def test_score_calibration():
    rows = []
    rows += [{"score": 82, "next_day_up": 1}] * 18
    rows += [{"score": 82, "next_day_up": 0}] * 2
    calibrated = calibrate_by_score(rows, bin_width=5)
    assert 0.8 < calibrated[80] < 1.0


def test_small_bin_is_ignored():
    rows = [{"score": 82, "next_day_up": 1}] * 19
    assert 80 not in calibrate_by_score(rows, bin_width=5)
