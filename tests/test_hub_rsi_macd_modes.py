"""RSI and MACD conditions (owner 2026-10-25: "add those strategy in these indicators"). Arbitrary test numbers."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import numpy as np  # noqa: E402

from app import strategy_hub as H  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_strategy_hub import make_df  # noqa: E402


class TestValidationAndOldPlans(unittest.TestCase):
    def test_old_plan_without_a_condition_works_as_before(self):
        p = make_plan(ind_rsi=dict(entry=True), ind_macd=dict(entry=True))
        p["ind"]["rsi"].pop("mode", None)
        p["ind"]["macd"].pop("mode", None)
        H.validate_plan(p)
        up = H.upgrade_plan(p)
        self.assertEqual((up["ind"]["rsi"]["modes"], up["ind"]["macd"]["modes"]), ([H.RSI_LEVEL], [H.MACD_HIST]))
        df = make_df()
        f = H.compute_hub(df, p)
        rsi = f["rsi"].values
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["rsi_long"].values, rsi > 50.0)

    def test_obos_needs_its_numbers_and_order(self):
        p = make_plan(ind_rsi=dict(entry=True, mode=H.RSI_OBOS, oversold=30.0, overbought=70.0, n=0))
        H.validate_plan(p)
        bad = make_plan(ind_rsi=dict(entry=True, mode=H.RSI_OBOS, oversold=70.0, overbought=30.0, n=0))
        with self.assertRaises(H.PlanError):
            H.validate_plan(bad)
        miss = make_plan(ind_rsi=dict(entry=True, mode=H.RSI_OBOS, oversold=30.0, n=0))
        with self.assertRaises(H.PlanError):
            H.validate_plan(miss)

    def test_level_not_needed_for_other_modes(self):
        p = make_plan(ind_rsi=dict(entry=True, mode=H.RSI_DIV, pivot=5, lookback=60, n=0))
        p["ind"]["rsi"].pop("level")
        H.validate_plan(p)
        bad = make_plan(ind_rsi=dict(entry=True, mode=H.RSI_DIV, pivot=10, lookback=5, n=0))
        with self.assertRaises(H.PlanError):
            H.validate_plan(bad)


class TestRsiObos(unittest.TestCase):
    def test_cross_back_inside_the_band(self):
        df = make_df(600, seed=11)
        p = make_plan(ind_rsi=dict(entry=True, mode=H.RSI_OBOS, oversold=40.0, overbought=60.0, n=0))
        f = H.compute_hub(df, p)
        r = f["rsi"].values
        prev = np.concatenate([[np.nan], r[:-1]])
        with np.errstate(invalid="ignore"):
            up = (r > 40.0) & (prev <= 40.0)
            dn = (r < 60.0) & (prev >= 60.0)
        np.testing.assert_array_equal(f["rsi_long"].values, up)
        np.testing.assert_array_equal(f["rsi_short"].values, dn)
        self.assertTrue(up.any() and dn.any())

    def test_n_keeps_it_on(self):
        df = make_df(600, seed=11)
        a = H.compute_hub(df, make_plan(ind_rsi=dict(entry=True, mode=H.RSI_OBOS, oversold=40.0, overbought=60.0, n=0)))
        b = H.compute_hub(df, make_plan(ind_rsi=dict(entry=True, mode=H.RSI_OBOS, oversold=40.0, overbought=60.0, n=2)))
        np.testing.assert_array_equal(b["rsi_long"].values, H._within(a["rsi_long"].values, 2))


class TestDivergence(unittest.TestCase):
    def test_pivots(self):
        x = np.array([5, 4, 3, 2, 3, 4, 5, 6, 5, 4], dtype=float)
        self.assertEqual(list(np.flatnonzero(H._pivots(x, 2, True))), [3])
        self.assertEqual(list(np.flatnonzero(H._pivots(x, 2, False))), [7])

    def test_bullish_and_bearish_on_made_up_data(self):
        k = 2
        rsi = np.array([50, 40, 30, 40, 50, 45, 40, 35, 40, 50, 55, 50], dtype=float)
        #               pivot low at 2 (30)          pivot low at 7 (35): higher RSI low
        low = np.array([100, 99, 98, 99, 100, 99, 98, 96, 97, 99, 100, 100], dtype=float)
        high = low + 1
        bull, bear = H.rsi_divergence(low, high, rsi, k, 60)
        self.assertEqual(list(np.flatnonzero(bull)), [7 + k])      # price lower low 96 < 98: confirmed 2 later
        self.assertFalse(bear.any())
        low2 = low.copy()
        low2[7] = 99.0                                              # price NOT lower: no divergence
        bull2, _ = H.rsi_divergence(low2, low2 + 1, rsi, k, 60)
        self.assertFalse(bull2.any())
        bull3, _ = H.rsi_divergence(low, high, rsi, k, 4)          # pivots 5 apart > look-back 4
        self.assertFalse(bull3.any())
        # mirror: bearish
        r_b = 100 - rsi
        h_b = 200 - low
        _, bear_b = H.rsi_divergence(h_b - 1, h_b, r_b, k, 60)
        self.assertEqual(list(np.flatnonzero(bear_b)), [7 + k])

    def test_never_earlier_than_the_pivot_is_known(self):
        df = make_df(800, seed=5)
        f = H.compute_hub(df, make_plan(ind_rsi=dict(entry=True, mode=H.RSI_DIV, pivot=5, lookback=60, n=0)))
        g = H.compute_hub(df.iloc[:-5].reset_index(drop=True),
                          make_plan(ind_rsi=dict(entry=True, mode=H.RSI_DIV, pivot=5, lookback=60, n=0)))
        # dropping the last 5 candles never changes any earlier signal (no look-ahead)
        np.testing.assert_array_equal(f["rsi_long"].values[:-5], g["rsi_long"].values)
        np.testing.assert_array_equal(f["rsi_short"].values[:-5], g["rsi_short"].values)


class TestMacdZeroLine(unittest.TestCase):
    def test_zero_line_uses_the_macd_line(self):
        df = make_df(600)
        f = H.compute_hub(df, make_plan(ind_macd=dict(entry=True, mode=H.MACD_ZERO)))
        line = f["macd_line"].values
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["macd_long"].values, line > 0)
            np.testing.assert_array_equal(f["macd_short"].values, line < 0)
        g = H.compute_hub(df, make_plan(ind_macd=dict(entry=True, mode=H.MACD_HIST)))
        self.assertFalse(np.array_equal(f["macd_long"].values, g["macd_long"].values))

    def test_rising_works_with_zero_line(self):
        df = make_df(600)
        f = H.compute_hub(df, make_plan(ind_macd=dict(entry=True, mode=H.MACD_ZERO, rising=True)))
        line, hist = f["macd_line"].values, f["macd_hist"].values
        prev = np.concatenate([[np.nan], hist[:-1]])
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["macd_long"].values, (line > 0) & (hist > prev))


if __name__ == "__main__":
    unittest.main()
