"""MAIN for ENTRY and for FLIP (owner 2026-10-24): "there's only ONE and that ONE turning point that confirms
the move in a strategy.. the rest makes up as confluence". The Main has to happen; the others show confluence
(Combine decides how many). Arbitrary test numbers only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import numpy as np  # noqa: E402

from app import strategy_hub as H  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_strategy_hub import make_df  # noqa: E402

RSI = dict(entry=True, flip=True, length=14, level=50)
ADX = dict(entry=True, flip=True, di_length=14, smoothing=14, level=20, di_required=True)
MA1 = dict(entry=True, flip=True, type="EMA", length=20, mode="Price above / below MA")


class TestMainValidation(unittest.TestCase):
    def test_no_main_is_as_before(self):
        p = make_plan(ind_rsi=RSI)
        self.assertIs(H.validate_plan(p), p)

    def test_main_must_be_ticked_for_its_side(self):
        p = make_plan(ind_rsi=dict(RSI, flip=False), ind_ma1=MA1, main_flip="rsi")
        with self.assertRaises(H.PlanError) as c:
            H.validate_plan(p)
        self.assertIn("Main for FLIP", str(c.exception))
        p = make_plan(ind_rsi=RSI, main_entry="adx")
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_unknown_main_refused(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan(ind_rsi=RSI, main_entry="nope"))

    def test_entry_and_exit_main_can_differ(self):
        H.validate_plan(make_plan(ind_rsi=RSI, ind_ma1=MA1, main_entry="rsi", main_flip="ma1"))

    def test_summary_shows_the_main(self):
        s = H.plan_summary(make_plan(ind_rsi=RSI, ind_ma1=MA1, main_entry="rsi", main_flip="ma1"))
        self.assertIn("ENTRY main: RSI", s)
        self.assertIn("FLIP main: MA 1", s)
        self.assertNotIn("main", H.plan_summary(make_plan(ind_rsi=RSI)))


class TestMainSignal(unittest.TestCase):
    def setUp(self):
        self.df = make_df()

    def test_no_main_unchanged(self):
        a = H.compute_hub(self.df, make_plan(ind_rsi=RSI, ind_adx=ADX))
        b = H.compute_hub(self.df, make_plan(ind_rsi=RSI, ind_adx=ADX, main_entry=None, main_flip=None))
        np.testing.assert_array_equal(a["dir_e"].values, b["dir_e"].values)
        np.testing.assert_array_equal(a["dir_f"].values, b["dir_f"].values)

    def test_main_go_but_one_confluence_no_is_still_no_go(self):
        """Owner example: main is a go, ADX is no, RSI is good - still a no go (Combine ALL)."""
        f = H.compute_hub(self.df, make_plan(ind_ma1=MA1, ind_adx=ADX, ind_rsi=RSI, main_entry="ma1"))
        m, a, r = f["ma1_long"].values, f["adx_long"].values, f["rsi_long"].values
        np.testing.assert_array_equal(f["dir_e"].values == 1, m & a & r)
        case = m & ~a & r
        self.assertTrue(case.any())                       # the example happens in the test data
        self.assertTrue((f["dir_e"].values[case] != 1).all())

    def test_main_has_to_happen_even_with_any(self):
        f = H.compute_hub(self.df, make_plan(ind_ma1=MA1, ind_rsi=RSI, combine_entry="ANY", main_entry="ma1"))
        m, r = f["ma1_long"].values, f["rsi_long"].values
        np.testing.assert_array_equal(f["dir_e"].values == 1, m)            # (m or r) and m
        no_main = r & ~m
        self.assertTrue(no_main.any())
        self.assertTrue((f["dir_e"].values[no_main] != 1).all())             # confluence alone is not enough
        g = H.compute_hub(self.df, make_plan(ind_ma1=MA1, ind_rsi=RSI, combine_entry="ANY"))
        self.assertTrue(g["entry_long"].values[no_main].all())               # without a Main it counted as LONG

    def test_at_least_n_counts_the_main_and_needs_it(self):
        f = H.compute_hub(self.df, make_plan(ind_ma1=MA1, ind_rsi=RSI, ind_adx=ADX, combine_entry="At least N",
                                             entry_min=2, main_entry="rsi"))
        m, r, a = f["ma1_long"].values, f["rsi_long"].values, f["adx_long"].values
        cnt = m.astype(int) + r.astype(int) + a.astype(int)
        np.testing.assert_array_equal(f["entry_long"].values, (cnt >= 2) & r)

    def test_flip_has_its_own_main(self):
        p = make_plan(ind_ma1=MA1, ind_rsi=RSI, combine_entry="ANY", combine_flip="ANY",
                      main_entry="rsi", main_flip="ma1")
        f = H.compute_hub(self.df, p)
        np.testing.assert_array_equal(f["entry_long"].values, f["rsi_long"].values)
        np.testing.assert_array_equal(f["flip_long"].values, f["ma1_long"].values)
        np.testing.assert_array_equal(f["flip_short"].values, f["ma1_short"].values)

    def test_short_side_too(self):
        f = H.compute_hub(self.df, make_plan(ind_ma1=MA1, ind_rsi=RSI, combine_entry="ANY", main_entry="ma1"))
        np.testing.assert_array_equal(f["dir_e"].values == -1, f["ma1_short"].values)


if __name__ == "__main__":
    unittest.main()
