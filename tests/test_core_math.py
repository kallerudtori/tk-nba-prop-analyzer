"""Odds arithmetic, CLV direction, and calibration scoring."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import calibration as cal
from core.clv import points_clv, probability_clv
from core.odds_math import (
    american_to_decimal, american_to_prob, breakeven_prob, decimal_to_american,
    devig_two_way, expected_value, kelly_fraction, overround, staked_units,
)


class TestConversions:
    @pytest.mark.parametrize("american", [-500, -200, -110, -101, 100, 150, 900])
    def test_american_decimal_roundtrip(self, american):
        assert decimal_to_american(american_to_decimal(american)) == american

    def test_standard_juice(self):
        assert breakeven_prob(-110) == pytest.approx(0.5238, abs=1e-4)
        assert overround(-110, -110) == pytest.approx(0.0476, abs=1e-4)


class TestDevig:
    def test_symmetric_market_devigs_to_even(self):
        a, b = devig_two_way(-110, -110)
        assert a == pytest.approx(0.5)
        assert b == pytest.approx(0.5)

    def test_devigged_probabilities_sum_to_one(self):
        a, b = devig_two_way(-200, +170)
        assert a + b == pytest.approx(1.0)

    def test_devig_lowers_the_favourite_probability(self):
        """
        The point of de-vigging: the raw implied probability overstates the
        favourite, and comparing a model number against it manufactures edge.
        """
        assert devig_two_way(-200, +170)[0] < american_to_prob(-200)


class TestStaking:
    def test_no_stake_without_edge(self):
        assert kelly_fraction(0.50, -110) < 0
        assert staked_units(0.50, -110, calibration_scale=1.0) == 0.0

    def test_uncalibrated_model_stakes_nothing(self):
        """Kelly on an unproven model overbets; the honest size is zero."""
        assert staked_units(0.60, -110, calibration_scale=0.0) == 0.0

    def test_stake_is_hard_capped(self):
        assert staked_units(0.95, +200, fraction=0.25, max_units=2.0,
                            calibration_scale=1.0) == 2.0

    def test_stake_scales_with_calibration(self):
        # p=0.55 keeps the stake below the hard cap, so the scaling is visible.
        # At p=0.60 quarter-Kelly already exceeds the 2-unit maximum and the
        # cap, not the calibration scale, is what sets the size.
        full = staked_units(0.55, -110, calibration_scale=1.0)
        half = staked_units(0.55, -110, calibration_scale=0.5)
        assert full < 2.0, "pick a probability below the cap for this test"
        assert half == pytest.approx(full / 2, abs=1e-3)

    def test_cap_binds_before_calibration_scaling_at_high_confidence(self):
        """
        Documents where the hard per-pick maximum takes over: quarter-Kelly on
        a 60% shot at -110 wants 4 units, and the cap holds it to 2.
        """
        assert staked_units(0.60, -110, calibration_scale=1.0) == 2.0

    def test_expected_value_sign(self):
        assert expected_value(0.60, -110) > 0
        assert expected_value(0.45, -110) < 0


class TestClvDirection:
    """Direction is the easiest thing to get backwards and the costliest."""

    @pytest.mark.parametrize("taken,side,close,expected", [
        (-3.0, None, -3.5, +0.5),
        (-3.0, None, -2.5, -0.5),
        (+3.0, None, +2.5, +0.5),
        (+3.0, None, +3.5, -0.5),
        (44.0, "over", 45.0, +1.0),
        (44.0, "over", 43.0, -1.0),
        (44.0, "under", 43.0, +1.0),
        (44.0, "under", 45.0, -1.0),
    ])
    def test_points_clv(self, taken, side, close, expected):
        pick = SimpleNamespace(line_at_pick=taken, side=side)
        assert points_clv(pick, close) == pytest.approx(expected)

    def test_missing_close_yields_no_clv(self):
        assert points_clv(SimpleNamespace(line_at_pick=-3.0, side=None), None) is None

    def test_probability_clv_positive_when_market_agrees(self):
        pick = SimpleNamespace(price_at_pick=+150, fair_probability=None, side=None)
        assert probability_clv(pick, +120) > 0   # shortened = market came to us
        assert probability_clv(pick, +200) < 0   # drifted = market moved away


class TestCalibration:
    def test_cold_start_is_provisional_and_unstaked(self):
        result = cal.evaluate([(0.6, 1)] * 10)
        assert result.is_provisional
        assert cal.calibration_scale(result) == 0.0

    def test_overconfident_model_scores_zero_scale(self):
        pairs = [(0.80, 1)] * 55 + [(0.80, 0)] * 45   # says 80%, wins 55%
        result = cal.evaluate(pairs)
        assert not result.is_provisional
        assert result.calibration_error > 0.2
        assert cal.calibration_scale(result) == 0.0

    def test_well_calibrated_model_scores_high(self):
        result = cal.evaluate([(0.60, 1)] * 60 + [(0.60, 0)] * 40)
        assert result.calibration_error < 0.02
        assert cal.calibration_scale(result) > 0.8

    def test_brier_of_coin_flip(self):
        assert cal.evaluate([(0.5, 1)] * 50 + [(0.5, 0)] * 50).brier == pytest.approx(0.25)

    def test_wilson_interval_is_wide_at_nfl_sample_sizes(self):
        """
        A 55% record over 100 picks does not exclude a coin flip. The
        Performance tab shows this interval so a hot streak is not mistaken
        for a proven edge.
        """
        lo, hi = cal.wilson_interval(55, 100)
        assert lo < 0.50 < hi

    def test_required_sample_size_for_breakeven_edge(self):
        """Detecting a 52.4% edge takes thousands of games — roughly 12 seasons."""
        assert cal.required_sample_size(0.024) > 3000
