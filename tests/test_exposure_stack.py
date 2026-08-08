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
from exposure_stack import monotone_break, score_tile, spearman_against  # noqa: E402


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


class TestScoreTile:
    """Drive the whole per-tile decision with a synthetic lake.

    Unit tests on the rank maths cannot catch a wiring fault -- a reference
    taken over the wrong pixels, or the dry test and the rho test disagreeing
    about which way is up. Those still produce a map, just not of rocks.
    """

    def build(self, n_flights=6, size=60, seed=3):
        """Open water everywhere, with per-flight brightness offsets to defeat
        anything that reads the flights as interchangeable."""
        rng = np.random.default_rng(seed)
        stack = rng.normal(0.30, 0.005, (n_flights, size, size)).astype("float32")
        stack += np.linspace(-0.03, 0.03, n_flights, dtype="float32")[:, None, None]
        return stack, np.ones((size, size), bool), np.linspace(0, 1, n_flights, "float32")

    def test_it_finds_a_rock_that_drowns_as_the_lake_rises(self):
        stack, water, order = self.build()
        stack[:3, 20:24, 20:24] = -0.5     # dry in the three lowest-water flights
        cand, score, rung, reasons = score_tile(stack, water, order)
        assert cand[21, 21], reasons
        assert rung[21, 21] == 3
        assert score["dry_margin"][21, 21] > 6.0

    def test_it_ignores_open_water(self):
        stack, water, order = self.build()
        cand, _, _, reasons = score_tile(stack, water, order)
        assert not cand.any(), reasons

    def test_it_ignores_an_island_that_is_dry_in_every_flight(self):
        stack, water, order = self.build()
        stack[:, 30:36, 30:36] = -0.5
        cand, _, _, _ = score_tile(stack, water, order)
        assert not cand[33, 33]

    def test_it_rejects_glint_that_is_dry_out_of_stage_order(self):
        stack, water, order = self.build()
        stack[[1, 4], 40:44, 40:44] = -0.5   # dry at high water, wet at low
        cand, _, _, _ = score_tile(stack, water, order)
        assert not cand[41, 41]

    def test_a_uniform_per_flight_brightness_shift_does_not_manufacture_rocks(self):
        # The failure this guards: if the flights were not referenced to their own
        # water, a seasonal brightness trend would correlate with stage everywhere
        # and the whole lake would score as hazard.
        stack, water, order = self.build()
        stack += np.linspace(-0.15, 0.15, len(order), dtype="float32")[:, None, None]
        cand, _, _, reasons = score_tile(stack, water, order)
        assert not cand.any(), reasons

    def test_it_declines_a_tile_with_no_stable_water_to_reference(self):
        stack, water, order = self.build(size=12)   # 144 px, under DEEP_REF_MIN_PX
        cand, _, _, reasons = score_tile(stack, water, order)
        assert cand is None
        assert "skipped" in reasons

    def test_it_rejects_a_pixel_that_stays_dark_after_it_drowns(self):
        # Weed and shadow are dark in every flight. Monotone dryness alone would
        # pass this; requiring the drowned half to look like ordinary water is
        # what rejects it.
        stack, water, order = self.build()
        stack[:3, 50:54, 50:54] = -0.5
        stack[3:, 50:54, 50:54] = 0.05     # still well below the ~0.30 water
        cand, _, _, _ = score_tile(stack, water, order)
        assert not cand[51, 51]


class TestOtsu:
    """The threshold that decides how big the lake is.

    Two fixed values failed in opposite directions on real flights -- 0.0 counted
    marsh as lake, 0.40 cut a third off the 2015 flight -- so this is the piece
    the stage ladder now rests on.
    """

    def bimodal(self, land_mean, water_mean, land_frac=0.45, n=200000, seed=1):
        rng = np.random.default_rng(seed)
        n_land = int(n * land_frac)
        return np.concatenate([rng.normal(land_mean, 0.05, n_land),
                               rng.normal(water_mean, 0.05, n - n_land)]).astype("float32")

    def test_it_lands_between_the_two_modes(self):
        from exposure_stack import otsu
        thr = otsu(self.bimodal(-0.3, 0.8))
        assert -0.3 < thr < 0.8

    def test_it_separates_both_a_bright_flight_and_a_dim_one(self):
        # 2015's water reads far dimmer than 2021's. What matters is not where
        # the split lands -- anywhere in the empty gap classifies identically --
        # but that both flights come out correctly separated.
        from exposure_stack import otsu
        for water_mean in (0.30, 0.55, 0.85):
            vals = self.bimodal(-0.3, water_mean)
            thr = otsu(vals)
            assert -0.3 < thr < water_mean
            assert float(np.mean(vals[vals > water_mean - 0.05] > thr)) > 0.99
            assert float(np.mean(vals[vals < -0.25] < thr)) > 0.99

    def test_a_balanced_sample_keeps_essentially_all_the_water(self):
        # The 2015 failure was a split landing inside the water distribution and
        # discarding a third of the lake. On a balanced sample it must not.
        from exposure_stack import otsu
        vals = self.bimodal(-0.3, 0.8)
        thr = otsu(vals)
        assert float(np.mean(vals[vals > 0.5] > thr)) > 0.99

    def test_overlapping_modes_still_keep_the_water(self):
        # The 2015 flight's contrast is 3.87 against 2021's 25.74, so its land and
        # water modes overlap instead of leaving a clean gap. That is the case a
        # fixed 0.40 threshold got wrong by discarding a third of the lake.
        from exposure_stack import otsu
        rng = np.random.default_rng(4)
        vals = np.concatenate([rng.normal(-0.05, 0.18, 90000),
                               rng.normal(0.30, 0.18, 110000)]).astype("float32")
        thr = otsu(vals)
        assert float(np.mean(vals[vals > 0.45] > thr)) > 0.95

    def test_too_few_pixels_returns_the_midpoint_rather_than_guessing(self):
        from exposure_stack import otsu
        assert otsu(np.array([0.5, 0.2], "float32")) == pytest.approx(0.35)
