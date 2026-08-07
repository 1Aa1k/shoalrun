"""The rank maths behind stage-exposure detection.

These two functions decide what counts as a rock. If `spearman_against` has its
sign flipped, the detector reports the deepest water on the lake as hazard; if
`monotone_break` mislabels a lawful sequence, the one physical filter here stops
filtering. Neither failure is visible in the output -- both produce a plausible
map. Hence tests.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from exposure_stack import monotone_break, spearman_against  # noqa: E402


def px(*series):
    """Pack per-flight scalars into the (flights, y, x) shape the functions take."""
    return np.array(series, "float32").reshape(len(series), 1, 1)


ORDER = np.array([0.0, 0.2, 0.4, 0.6, 0.8, 1.0], "float32")  # already low -> high


class TestSpearman:
    def test_a_rock_drowning_as_the_lake_rises_scores_plus_one(self):
        # NDWI climbs from dry-rock negative to open-water positive in stage order.
        stack = px(-0.6, -0.4, -0.1, 0.2, 0.3, 0.35)
        rho = spearman_against(ORDER, stack, np.ones_like(stack, bool))
        assert rho[0, 0] == pytest.approx(1.0, abs=1e-5)

    def test_the_reverse_sequence_scores_minus_one(self):
        stack = px(0.35, 0.3, 0.2, -0.1, -0.4, -0.6)
        rho = spearman_against(ORDER, stack, np.ones_like(stack, bool))
        assert rho[0, 0] == pytest.approx(-1.0, abs=1e-5)

    def test_stage_order_drives_the_answer_not_flight_order(self):
        # Same six observations, shuffled, with the order vector shuffled to match.
        stack = px(0.2, -0.6, 0.35, -0.1, 0.3, -0.4)
        order = np.array([0.6, 0.0, 1.0, 0.4, 0.8, 0.2], "float32")
        rho = spearman_against(order, stack, np.ones_like(stack, bool))
        assert rho[0, 0] == pytest.approx(1.0, abs=1e-5)

    def test_deep_water_with_no_trend_scores_near_zero(self):
        rng = np.random.default_rng(7)
        stack = rng.normal(0.3, 0.01, (6, 40, 40)).astype("float32")
        rho = spearman_against(ORDER, stack, np.ones_like(stack, bool))
        assert abs(float(np.nanmean(rho))) < 0.15

    def test_a_masked_flight_is_excluded_not_treated_as_zero(self):
        # Flight 3 is cloud. The remaining five are still perfectly ordered, so a
        # correct implementation returns 1.0; treating NaN as a value would not.
        stack = px(-0.6, -0.4, np.nan, 0.2, 0.3, 0.35)
        valid = np.isfinite(stack)
        rho = spearman_against(ORDER, stack, valid)
        assert rho[0, 0] == pytest.approx(1.0, abs=1e-5)

    def test_a_constant_series_yields_nan_rather_than_a_fake_correlation(self):
        stack = px(0.3, 0.3, 0.3, 0.3, 0.3, 0.3)
        rho = spearman_against(ORDER, stack, np.ones_like(stack, bool))
        assert np.isnan(rho[0, 0])


class TestMonotoneBreak:
    def dry(self, *flags):
        return np.array(flags, bool).reshape(len(flags), 1, 1)

    def test_dry_in_the_two_lowest_flights_puts_the_top_on_rung_two(self):
        k, clean = monotone_break(self.dry(1, 1, 0, 0, 0, 0), ORDER)
        assert int(k[0, 0]) == 2
        assert bool(clean[0, 0])

    def test_never_dry_is_rung_zero(self):
        k, clean = monotone_break(self.dry(0, 0, 0, 0, 0, 0), ORDER)
        assert int(k[0, 0]) == 0
        assert bool(clean[0, 0])

    def test_always_dry_is_the_top_rung(self):
        k, clean = monotone_break(self.dry(1, 1, 1, 1, 1, 1), ORDER)
        assert int(k[0, 0]) == 6
        assert bool(clean[0, 0])

    def test_dry_at_high_water_but_wet_at_low_water_is_unlawful(self):
        # Physically impossible for a static rock -- this is the glint signature.
        _, clean = monotone_break(self.dry(0, 1, 0, 1, 0, 0), ORDER)
        assert not bool(clean[0, 0])

    def test_a_single_late_dry_flight_is_unlawful(self):
        _, clean = monotone_break(self.dry(0, 0, 0, 0, 0, 1), ORDER)
        assert not bool(clean[0, 0])

    def test_flights_are_read_in_stage_order_not_array_order(self):
        # Array order says 1,1,0,1,0,0 -- unlawful, a dry flight sitting above a
        # wet one. But the three dry flights ARE the three lowest stages, so read
        # in stage order it is the lawful step function.
        order = np.array([0.4, 0.0, 0.6, 0.2, 0.8, 1.0], "float32")
        dry = self.dry(1, 1, 0, 1, 0, 0)
        k, clean = monotone_break(dry, order)
        assert bool(clean[0, 0])
        assert int(k[0, 0]) == 3

    def test_it_labels_a_whole_raster_elementwise(self):
        dry = np.zeros((6, 3, 3), bool)
        dry[:2, 0, 0] = True          # rung 2, lawful
        dry[:, 1, 1] = True           # island
        dry[[1, 4], 2, 2] = True      # unlawful
        k, clean = monotone_break(dry, ORDER)
        assert int(k[0, 0]) == 2 and bool(clean[0, 0])
        assert int(k[1, 1]) == 6 and bool(clean[1, 1])
        assert not bool(clean[2, 2])
