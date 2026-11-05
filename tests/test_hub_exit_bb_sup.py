"""Owner 2026-11-04: EXIT WHEN (optional function), the Candle colour option "previous candle was the opposite colour",
Bollinger Bands, Supertrend, the Bollinger-half counter-trend size and the Trailing Stop type "Supertrend line".
Arbitrary test numbers - the product has no default plan."""
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_ladder import LadderBase  # noqa: E402  (stubs + temp data dir)
from tests.test_hub_trading import snap_of  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import shadow_book as B  # noqa: E402
from app import hub_backtest as BT  # noqa: E402


# ---------------------------------------------------------------------------------------------- data helpers
def candles(n=600, seed=7, start=100.0, vol=1.0):
    rng = np.random.default_rng(seed)
    c = start + np.cumsum(rng.normal(0, vol, n))
    o = np.concatenate([[start], c[:-1]]) + rng.normal(0, vol * 0.2, n)
    h = np.maximum(o, c) + np.abs(rng.normal(0, vol * 0.5, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, vol * 0.5, n))
    return pd.DataFrame({"open_time": np.arange(n) * 3_600_000, "open": o, "high": h, "low": l, "close": c,
                         "volume": rng.uniform(100, 200, n)})


def exit_cfg(signal=H.EXIT_BOTH, after=H.EXIT_WAIT, on=True):
    return {"on": on, "signal": signal, "after": after}


# ---------------------------------------------------------------------------------------------- validation
class TestValidation(unittest.TestCase):
    def test_a_plan_without_the_new_things_is_still_valid_and_they_are_off(self):
        p = make_plan(ind_ma1={"entry": True})
        del p["ind"]["bb"], p["ind"]["sup"]                   # a plan saved before 2026-11-04
        H.validate_plan(p)
        c = H.complete_plan(p)
        self.assertFalse(c["ind"]["bb"]["entry"] or c["ind"]["sup"]["entry"])
        self.assertEqual(c["exit_when"]["on"], False)
        self.assertIsNone(H.exit_cfg(c))

    def test_blocks_are_listed(self):
        self.assertIn("bb", H.BLOCKS)
        self.assertIn("sup", H.BLOCKS)
        self.assertEqual(H.BLOCK_TITLES["bb"], "Bollinger Bands")
        self.assertEqual(H.BLOCK_TITLES["sup"], "Supertrend")
        self.assertIn("bb", H.CT_BLOCKS)
        self.assertNotIn("sup", H.CT_BLOCKS)

    def test_exit_when_needs_its_two_choices(self):
        for bad in ({"on": True}, {"on": True, "signal": H.EXIT_BOTH}, {"on": True, "after": H.EXIT_WAIT},
                    {"on": True, "signal": "nope", "after": H.EXIT_WAIT},
                    {"on": True, "signal": H.EXIT_BOTH, "after": "nope"}, {"signal": H.EXIT_BOTH}, "on", 5):
            with self.assertRaises(H.PlanError, msg=str(bad)):
                H.validate_plan(make_plan(ind_ma1={"entry": True}, exit_when=bad))

    def test_exit_when_off_needs_no_choices(self):
        H.validate_plan(make_plan(ind_ma1={"entry": True}, exit_when={"on": False}))

    def test_exit_when_every_choice_is_accepted(self):
        for sg in H.EXIT_SIGNALS:
            for af in H.EXIT_AFTERS:
                H.validate_plan(make_plan(ind_ma1={"entry": True}, exit_when=exit_cfg(sg, af)))

    def test_exit_when_needs_the_ma_numbers(self):
        p = make_plan(ind_ma1={"entry": True}, exit_when=exit_cfg(H.EXIT_BOTH))
        del p["ind"]["ma2"]["length"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p = make_plan(ind_ma1={"entry": True}, exit_when=exit_cfg(H.EXIT_BOTH))
        del p["ind"]["ma1"]["length"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        # MA 2 only: MA 1 numbers are not needed (MA 1 is not ticked here either)
        p = make_plan(ind_rsi={"entry": True}, exit_when=exit_cfg(H.EXIT_MA2))
        del p["ind"]["ma1"]["length"], p["ind"]["ma1"]["type"]
        H.validate_plan(p)
        del p["ind"]["ma2"]["length"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_bollinger_needs_its_numbers_when_ticked_only(self):
        H.validate_plan(make_plan(ind_ma1={"entry": True}))
        for key in ("length", "mult", "n"):
            p = make_plan(ind_bb={"entry": True})
            del p["ind"]["bb"][key]
            with self.assertRaises(H.PlanError, msg=key):
                H.validate_plan(p)
        for key, bad in (("length", 1), ("mult", 0), ("n", -1), ("n", 1.5)):
            p = make_plan(ind_bb={"entry": True})
            p["ind"]["bb"][key] = bad
            with self.assertRaises(H.PlanError, msg=f"{key}={bad}"):
                H.validate_plan(p)
        for ok_n in (0, 1, 3):
            p = make_plan(ind_bb={"entry": True})
            p["ind"]["bb"]["n"] = ok_n
            H.validate_plan(p)

    def test_supertrend_needs_its_numbers_and_n_only_for_the_fresh_flip(self):
        for key in ("atr_length", "factor", "modes"):
            p = make_plan(ind_sup={"entry": True})
            del p["ind"]["sup"][key]
            with self.assertRaises(H.PlanError, msg=key):
                H.validate_plan(p)
        p = make_plan(ind_sup={"entry": True})
        del p["ind"]["sup"]["n"]
        H.validate_plan(p)                                      # Trend direction: no N
        p["ind"]["sup"]["modes"] = [H.SUP_FLIP]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p["ind"]["sup"]["n"] = 2
        H.validate_plan(p)
        p["ind"]["sup"]["modes"] = [H.SUP_TREND, H.SUP_FLIP]
        H.validate_plan(p)
        for bad in ([], ["nope"], [H.SUP_TREND, H.SUP_TREND]):
            p["ind"]["sup"]["modes"] = bad
            with self.assertRaises(H.PlanError, msg=str(bad)):
                H.validate_plan(p)

    def test_supertrend_trailing_type(self):
        self.assertIn(H.TRAIL_SUP, H.TRAIL_TYPES)
        p = make_plan(ind_ma1={"entry": True}, trail={"on": True, "type": H.TRAIL_SUP})
        H.validate_plan(p)                                     # needs only the Supertrend ATR length and factor
        needed = H._needed_blocks(H.upgrade_plan(p))
        self.assertIn("sup", needed)
        for key in ("atr_length", "factor"):
            q = make_plan(ind_ma1={"entry": True}, trail={"on": True, "type": H.TRAIL_SUP})
            del q["ind"]["sup"][key]
            with self.assertRaises(H.PlanError, msg=key):
                H.validate_plan(q)
        q = make_plan(ind_ma1={"entry": True}, trail={"on": True, "type": H.TRAIL_SUP})
        del q["ind"]["sup"]["modes"], q["ind"]["sup"]["n"]    # the conditions are not used by the trailing line
        H.validate_plan(q)

    def test_bollinger_half_for_the_counter_trend(self):
        p = make_plan(ind_ma1={"entry": True},
                      sizing={"counter": {"on": True, "by": "bb", "alloc_pct": 1.0, "leverage": 2}})
        H.validate_plan(p)
        self.assertIn("bb", H._needed_blocks(H.upgrade_plan(p)))
        del p["ind"]["bb"]["mult"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p = make_plan(ind_ma1={"entry": True},
                      sizing={"counter": {"on": True, "by": "bb", "alloc_pct": 1.0, "leverage": 2}})
        del p["ind"]["bb"]["n"]                                # N is not used by the half
        H.validate_plan(p)

    def test_candle_option_is_optional(self):
        p = make_plan(ind_candle={"entry": True})
        H.validate_plan(p)                                     # left out = OFF (an older plan)
        p["ind"]["candle"]["prev_opposite"] = True
        H.validate_plan(p)
        p["ind"]["candle"]["prev_opposite"] = "yes"
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_unused_blocks_do_not_change_the_signal(self):
        a = make_plan(ind_ma1={"entry": True})
        b = make_plan(ind_ma1={"entry": True})
        b["ind"]["bb"].update(length=5, mult=0.5)
        b["ind"]["sup"].update(atr_length=3, factor=1.0)
        df = candles()
        fa, fb = H.compute_hub(df, a), H.compute_hub(df, b)
        for col in ("entry_long", "entry_short", "flip_long", "dir_e", "dir_f"):
            self.assertTrue((fa[col].values == fb[col].values).all(), col)


# ---------------------------------------------------------------------------------------------- the signals
class TestCandlePrevOpposite(unittest.TestCase):
    def test_oracle_on_heikin_ashi(self):
        df = candles(400, seed=3)
        plain = H.compute_hub(df, make_plan(ind_candle={"entry": True}))
        withp = make_plan(ind_candle={"entry": True})
        withp["ind"]["candle"]["prev_opposite"] = True
        f = H.compute_hub(df, withp)
        o, c = f["c_open"].values, f["c_close"].values
        exp_l = np.zeros(len(f), bool)
        exp_s = np.zeros(len(f), bool)
        for i in range(1, len(f)):
            exp_l[i] = c[i] > o[i] and c[i - 1] < o[i - 1]
            exp_s[i] = c[i] < o[i] and c[i - 1] > o[i - 1]
        self.assertTrue((f["candle_long"].values == exp_l).all())
        self.assertTrue((f["candle_short"].values == exp_s).all())
        self.assertTrue(exp_l.sum() > 10 and exp_s.sum() > 10)                       # the test is not empty
        # it can only make the signal rarer, never create one
        self.assertTrue((~f["candle_long"].values | plain["candle_long"].values).all())
        self.assertLess(f["candle_long"].sum(), plain["candle_long"].sum())

    def test_off_is_the_plain_colour(self):
        df = candles(300, seed=4)
        a = H.compute_hub(df, make_plan(ind_candle={"entry": True}))
        p = make_plan(ind_candle={"entry": True})
        p["ind"]["candle"]["prev_opposite"] = False
        b = H.compute_hub(df, p)
        self.assertTrue((a["candle_long"].values == b["candle_long"].values).all())

    def test_works_together_with_strong_candle(self):
        df = candles(300, seed=5)
        p = make_plan(ind_candle={"entry": True, "strong": True})
        p["ind"]["candle"]["prev_opposite"] = True
        f = H.compute_hub(df, p)
        o, c, l = f["c_open"].values, f["c_close"].values, f["c_low"].values
        for i in np.where(f["candle_long"].values)[0]:
            self.assertTrue(c[i] > o[i] and l[i] >= o[i] - 1e-6 and c[i - 1] < o[i - 1])


class TestBollinger(unittest.TestCase):
    def frame(self, n_touch=1, length=20, mult=2.0, chart="Heikin Ashi", seed=11):
        p = make_plan(ind_bb={"entry": True}, chart_type=chart)
        p["ind"]["bb"].update(length=length, mult=mult, n=n_touch)
        return H.compute_hub(candles(500, seed=seed), p)

    def test_bands_match_pandas(self):
        f = self.frame()
        s = pd.Series(f["c_close"].values)
        basis = s.rolling(20).mean()
        dev = 2.0 * s.rolling(20).std(ddof=0)
        np.testing.assert_allclose(f["bb_basis"].values, basis.values, equal_nan=True)
        np.testing.assert_allclose(f["bb_upper"].values, (basis + dev).values, equal_nan=True)
        np.testing.assert_allclose(f["bb_lower"].values, (basis - dev).values, equal_nan=True)

    def test_touch_within_n_candles(self):
        for n in (0, 1, 3):
            f = self.frame(n_touch=n)
            low, high = f["c_low"].values, f["c_high"].values
            lo_b, up_b = f["bb_lower"].values, f["bb_upper"].values
            with np.errstate(invalid="ignore"):
                tl, ts = low <= lo_b, high >= up_b
            exp_l, exp_s = np.zeros(len(f), bool), np.zeros(len(f), bool)
            for i in range(len(f)):
                exp_l[i] = tl[max(0, i - n):i + 1].any()
                exp_s[i] = ts[max(0, i - n):i + 1].any()
            self.assertTrue((f["bb_long"].values == exp_l).all(), n)
            self.assertTrue((f["bb_short"].values == exp_s).all(), n)
            self.assertTrue(exp_l.sum() > 3 and exp_s.sum() > 3)

    def test_n_zero_is_the_signal_candle_only(self):
        f = self.frame(n_touch=0)
        with np.errstate(invalid="ignore"):
            exp = f["c_low"].values <= f["bb_lower"].values
        self.assertTrue((f["bb_long"].values == exp).all())

    def test_a_wider_band_touches_less(self):
        narrow, wide = self.frame(mult=1.0), self.frame(mult=3.0)
        self.assertGreater(narrow["bb_long"].sum(), wide["bb_long"].sum())

    def test_works_on_plain_candles_too(self):
        f = self.frame(chart="Candles")
        self.assertTrue(f["bb_long"].sum() > 0)

    def test_the_bollinger_half_decides_the_counter_trend(self):
        p = make_plan(ind_ma1={"entry": True},
                      sizing={"counter": {"on": True, "by": "bb", "alloc_pct": 1.0, "leverage": 2}})
        f = H.compute_hub(candles(300, seed=2), p)
        c, basis = f["c_close"].values, f["bb_basis"].values
        with np.errstate(invalid="ignore"):
            self.assertTrue((f["ct_long"].values == (c < basis)).all())
            self.assertTrue((f["ct_short"].values == (c > basis)).all())
        # entry size: LONG in the lower half = with the trend (normal size), in the upper half = against (smaller)
        plan = H.complete_plan(make_plan(
            ind_ma1={"entry": True},
            sizing={"alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_pct": 6.0,
                    "counter": {"on": True, "by": "bb", "alloc_pct": 2.0, "leverage": 3}}))
        lower = snap_of(1, dir_e=1, close=100.0)
        lower.ct_long, lower.ct_short = True, False             # close below the middle line
        upper = snap_of(2, dir_e=1, close=100.0)
        upper.ct_long, upper.ct_short = False, True            # close above the middle line
        self.assertEqual(H.entry_size("LONG", lower, plan), (10.0, 5.0, "trend"))
        self.assertEqual(H.entry_size("LONG", upper, plan), (2.0, 3.0, "counter"))
        self.assertEqual(H.entry_size("SHORT", upper, plan), (10.0, 5.0, "trend"))
        self.assertEqual(H.entry_size("SHORT", lower, plan), (2.0, 3.0, "counter"))
        mid = snap_of(3, dir_e=1, close=100.0)
        mid.ct_long, mid.ct_short = False, False               # exactly on the middle line: the smaller size
        self.assertEqual(H.entry_size("LONG", mid, plan)[2], "counter")
        self.assertEqual(H.entry_size("SHORT", mid, plan)[2], "counter")


def textbook_supertrend(h, l, c, length, factor):
    """An independent Supertrend (the textbook form): returns (line, up) from the first bar the ATR is known."""
    n = len(c)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr = np.full(n, np.nan)
    first = length                          # the RMA starts with the simple average of the first `length` true ranges
    atr[first] = np.mean(tr[1:first + 1])
    for i in range(first + 1, n):
        atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length
    mid = (h + l) / 2
    ub, lb = mid + factor * atr, mid - factor * atr
    fub, flb = ub.copy(), lb.copy()
    up = np.zeros(n, bool)
    line = np.full(n, np.nan)
    up[first] = c[first] > mid[first]
    line[first] = flb[first] if up[first] else fub[first]
    for i in range(first + 1, n):
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        if up[i - 1]:
            up[i] = not (c[i] < flb[i])
        else:
            up[i] = c[i] > fub[i]
        line[i] = flb[i] if up[i] else fub[i]
    return line, up


class TestSupertrend(unittest.TestCase):
    def run_plan(self, modes=(H.SUP_TREND,), n=1, length=10, factor=3.0, seed=21, bars=700):
        p = make_plan(ind_sup={"entry": True}, chart_type="Heikin Ashi")
        p["ind"]["sup"].update(modes=list(modes), n=n, atr_length=length, factor=factor)
        df = candles(bars, seed=seed, vol=1.5)
        return H.compute_hub(df, p), p

    def test_line_and_direction_match_an_independent_supertrend(self):
        for (length, factor, seed) in ((10, 3.0, 21), (7, 2.0, 22), (14, 1.5, 23)):
            f, _ = self.run_plan(length=length, factor=factor, seed=seed)
            line, up = textbook_supertrend(f["c_high"].values, f["c_low"].values, f["c_close"].values, length, factor)
            a, b = f["sup_line"].values, line
            skip = 120                                      # the two start from a different first state; they agree after
            np.testing.assert_allclose(a[skip:], b[skip:], rtol=1e-9, equal_nan=False)
            self.assertTrue((f["sup_up"].values[skip:] == up[skip:]).all())
            self.assertTrue(up[skip:].any() and (~up[skip:]).any())     # both trends occur: the check means something

    def test_trend_direction_signal(self):
        f, _ = self.run_plan()
        self.assertTrue((f["sup_long"].values == f["sup_up"].values).all())
        valid = ~np.isnan(f["sup_line"].values)
        self.assertTrue((f["sup_short"].values == (valid & ~f["sup_up"].values)).all())
        # never both sides, nothing before the ATR is known
        self.assertFalse((f["sup_long"].values & f["sup_short"].values).any())
        self.assertFalse(f["sup_long"].values[:5].any() or f["sup_short"].values[:5].any())

    def test_the_line_is_on_the_right_side_of_the_close(self):
        f, _ = self.run_plan()
        line, c, up = f["sup_line"].values, f["c_close"].values, f["sup_up"].values
        ok = ~np.isnan(line)
        self.assertTrue((line[ok & up] <= c[ok & up] + 1e-9).all())      # up trend: the line is below
        dn = ok & ~up
        self.assertTrue((line[dn] >= c[dn] - 1e-9).all())               # down trend: the line is above

    def test_fresh_flip_within_n(self):
        for n in (0, 2, 5):
            f, _ = self.run_plan(modes=(H.SUP_FLIP,), n=n)
            up = f["sup_up"].values
            valid = ~np.isnan(f["sup_line"].values)
            down = valid & ~up
            flip_up = np.zeros(len(f), bool)
            flip_dn = np.zeros(len(f), bool)
            for i in range(1, len(f)):
                flip_up[i] = up[i] and down[i - 1]
                flip_dn[i] = down[i] and up[i - 1]
            exp_l = np.array([flip_up[max(0, i - n):i + 1].any() for i in range(len(f))])
            exp_s = np.array([flip_dn[max(0, i - n):i + 1].any() for i in range(len(f))])
            self.assertTrue((f["sup_long"].values == exp_l).all(), n)
            self.assertTrue((f["sup_short"].values == exp_s).all(), n)
            self.assertTrue(exp_l.sum() > 3 and exp_s.sum() > 3)

    def test_both_conditions_ticked_must_agree(self):
        both, _ = self.run_plan(modes=(H.SUP_TREND, H.SUP_FLIP), n=2)
        flip, _ = self.run_plan(modes=(H.SUP_FLIP,), n=2)
        trend, _ = self.run_plan(modes=(H.SUP_TREND,))
        self.assertTrue((both["sup_long"].values == (flip["sup_long"].values & trend["sup_long"].values)).all())

    def test_the_trailing_level_helper(self):
        self.assertEqual(H.sup_trail_level("LONG", 95.0, 100.0), 95.0)
        self.assertIsNone(H.sup_trail_level("LONG", 101.0, 100.0))          # wrong side
        self.assertIsNone(H.sup_trail_level("LONG", 100.0, 100.0))
        self.assertEqual(H.sup_trail_level("SHORT", 104.0, 100.0), 104.0)
        self.assertIsNone(H.sup_trail_level("SHORT", 99.0, 100.0))
        self.assertIsNone(H.sup_trail_level("LONG", float("nan"), 100.0))
        self.assertIsNone(H.sup_trail_level("LONG", None, 100.0))


class TestExitSignals(unittest.TestCase):
    def frame(self, signal, ma1="EMA", ma2="EMA", l1=9, l2=21):
        p = make_plan(ind_mx={"entry": True}, exit_when=exit_cfg(signal))
        p["ind"]["mx"]["modes"] = [H.MX_MODES[0]]
        p["ind"]["ma1"].update(type=ma1, length=l1)
        p["ind"]["ma2"].update(type=ma2, length=l2)
        return H.compute_hub(candles(500, seed=31), p)

    def test_both_must_curve_against_the_trade(self):
        f = self.frame(H.EXIT_BOTH)
        m1, m2 = f["ma1"].values, f["ma2"].values
        for i in range(30, len(f)):
            self.assertEqual(bool(f["exit_long"].values[i]), bool(m1[i] < m1[i - 1] and m2[i] < m2[i - 1]), i)
            self.assertEqual(bool(f["exit_short"].values[i]), bool(m1[i] > m1[i - 1] and m2[i] > m2[i - 1]), i)
        self.assertTrue(f["exit_long"].sum() > 5 and f["exit_short"].sum() > 5)

    def test_ma2_only_is_the_slower_one(self):
        f = self.frame(H.EXIT_MA2)
        m2 = f["ma2"].values
        for i in range(30, len(f)):
            self.assertEqual(bool(f["exit_long"].values[i]), bool(m2[i] < m2[i - 1]), i)
            self.assertEqual(bool(f["exit_short"].values[i]), bool(m2[i] > m2[i - 1]), i)
        both = self.frame(H.EXIT_BOTH)
        self.assertTrue((both["exit_long"].values <= f["exit_long"].values).all())     # both is stricter
        self.assertGreater(f["exit_long"].sum(), both["exit_long"].sum())

    def test_the_ma_value_is_read_at_the_candle_close_not_inside_it(self):
        # the signal of candle i uses only the closes up to candle i: cutting the future off changes nothing
        p = make_plan(ind_mx={"entry": True}, exit_when=exit_cfg(H.EXIT_BOTH))
        p["ind"]["mx"]["modes"] = [H.MX_MODES[0]]
        df = candles(500, seed=31)
        full = H.compute_hub(df, p)
        cut = H.compute_hub(df.iloc[:400].reset_index(drop=True), p)
        self.assertTrue((full["exit_long"].values[:400] == cut["exit_long"].values).all())

    def test_off_gives_no_signal(self):
        p = make_plan(ind_mx={"entry": True})
        p["ind"]["mx"]["modes"] = [H.MX_MODES[0]]
        f = H.compute_hub(candles(300, seed=31), p)
        self.assertFalse(f["exit_long"].any() or f["exit_short"].any())
        p["exit_when"] = exit_cfg(on=False)
        f = H.compute_hub(candles(300, seed=31), p)
        self.assertFalse(f["exit_long"].any() or f["exit_short"].any())

    def test_other_ma_types_work(self):
        for t in H.MA_TYPES:
            f = self.frame(H.EXIT_BOTH, ma1=t, ma2=t)
            self.assertTrue(f["exit_long"].sum() > 0, t)

    def test_decision_functions(self):
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when=exit_cfg(H.EXIT_BOTH, H.EXIT_FLIP)))
        s = snap_of(1)
        s.exit_long = True
        self.assertEqual(H.exit_decision("LONG", s, p), "EXIT")
        self.assertEqual(H.exit_decision("SHORT", s, p), "STAY")
        s.exit_long, s.exit_short = False, True
        self.assertEqual(H.exit_decision("SHORT", s, p), "EXIT")
        self.assertEqual(H.exit_decision("LONG", s, p), "STAY")
        self.assertEqual(H.exit_after(p), H.EXIT_FLIP)
        off = H.complete_plan(make_plan(ind_ma1={"entry": True}))
        s.exit_long = s.exit_short = True
        self.assertEqual(H.exit_decision("LONG", s, off), "STAY")
        self.assertEqual(H.exit_after(off), H.EXIT_WAIT)


# ---------------------------------------------------------------------------------------------- the live bot
def live_plan(after=H.EXIT_WAIT, signal=H.EXIT_BOTH, **over):
    return make_plan(ind_ma1={"entry": True, "flip": True}, exit_when=exit_cfg(signal, after), **over)


class TestLiveExit(LadderBase):
    async def long_with_exit(self, inst, **snap_over):
        await self.open_long_at(inst)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        for k, v in snap_over.items():
            setattr(s, k, v)
        inst.client.mark_price = 101.0
        await self.tick(inst, s)
        return s

    async def test_wait_closes_and_stays_flat(self):
        inst = self.make_l(exit_when=exit_cfg(H.EXIT_BOTH, H.EXIT_WAIT))
        await self.long_with_exit(inst, exit_long=True)
        c = inst.client
        self.assertEqual(len(c.close_orders), 1)
        self.assertEqual(c.close_orders[0][1], "SELL")
        self.assertEqual(len(c.market_orders), 1)                 # only the first entry: nothing opened on the exit candle
        self.assertEqual(inst.state.status, "IDLE")
        self.assertIsNone(inst.hub.open_now)
        self.assertEqual(inst.hub.reentry_block, 1)
        self.assertTrue(inst.hub.reentry_forced)

    async def test_wait_blocks_the_same_side_until_the_signal_has_ended_even_if_reentry_is_allowed(self):
        inst = self.make_l(exit_when=exit_cfg(H.EXIT_BOTH, H.EXIT_WAIT), allow_reentry=True)
        await self.long_with_exit(inst, exit_long=True)
        # the entry signal (long) is still on the next candles: no entry
        await self.tick(inst, snap_of(3000, dir_e=1, dir_f=1, close=102.0))
        await self.tick(inst, snap_of(4000, dir_e=1, dir_f=1, close=103.0))
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(len(inst.client.market_orders), 1)
        # the signal ends -> the block ends; a fresh long signal enters
        await self.tick(inst, snap_of(5000, dir_e=0, dir_f=0, close=102.0))
        self.assertEqual(inst.hub.reentry_block, 0)
        self.assertFalse(inst.hub.reentry_forced)
        inst.client.mark_price = 102.0
        await self.tick(inst, snap_of(6000, dir_e=1, dir_f=1, close=102.0))
        self.assertEqual(inst.state.direction, "LONG")
        self.assertEqual(len(inst.client.market_orders), 2)

    async def test_wait_the_other_side_may_enter_at_once(self):
        inst = self.make_l(exit_when=exit_cfg(H.EXIT_BOTH, H.EXIT_WAIT))
        await self.long_with_exit(inst, exit_long=True)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(3000, dir_e=-1, dir_f=-1, close=100.0))
        self.assertEqual(inst.state.direction, "SHORT")

    async def test_flip_option_closes_and_opens_the_opposite_side(self):
        inst = self.make_l(exit_when=exit_cfg(H.EXIT_BOTH, H.EXIT_FLIP))
        await self.long_with_exit(inst, exit_long=True)           # the flip signal itself is still long
        c = inst.client
        self.assertEqual(len(c.close_orders), 1)
        self.assertEqual(len(c.market_orders), 2)
        self.assertEqual(c.market_orders[1][1], "SELL")           # the opposite side
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertFalse(inst.hub.reentry_forced)

    async def one_flip_not_two(self, after):
        inst = self.make_l(exit_when=exit_cfg(H.EXIT_BOTH, after))
        await self.open_long_at(inst)
        inst.client.mark_price = 99.0
        s = snap_of(2000, dir_e=-1, dir_f=-1, close=99.0)
        s.exit_long = True
        await self.tick(inst, s)
        self.assertEqual(len(inst.client.close_orders), 1, after)
        self.assertEqual(len(inst.client.market_orders), 2, after)
        self.assertEqual(inst.state.direction, "SHORT", after)

    async def test_a_flip_signal_on_the_same_candle_is_one_flip_not_two_wait(self):
        await self.one_flip_not_two(H.EXIT_WAIT)

    async def test_a_flip_signal_on_the_same_candle_is_one_flip_not_two_flip(self):
        await self.one_flip_not_two(H.EXIT_FLIP)

    async def test_off_ignores_the_signal(self):
        inst = self.make_l()
        await self.long_with_exit(inst, exit_long=True)
        self.assertEqual(inst.client.close_orders, [])
        self.assertEqual(inst.state.direction, "LONG")

    async def test_no_exit_signal_no_exit(self):
        inst = self.make_l(exit_when=exit_cfg())
        await self.long_with_exit(inst, exit_long=False, exit_short=True)    # against a SHORT, not a LONG
        self.assertEqual(inst.client.close_orders, [])
        self.assertEqual(inst.state.direction, "LONG")

    async def test_short_is_mirrored(self):
        inst = self.make_l(exit_when=exit_cfg(H.EXIT_BOTH, H.EXIT_WAIT))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=100.0))
        self.assertEqual(inst.state.direction, "SHORT")
        s = snap_of(2000, dir_e=-1, dir_f=-1, close=99.0)
        s.exit_short = True
        inst.client.mark_price = 99.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(inst.client.close_orders[0][1], "BUY")
        self.assertEqual(inst.hub.reentry_block, -1)
        self.assertTrue(inst.hub.reentry_forced)

    async def test_exit_closes_even_when_the_hold_rule_holds_the_flip(self):
        inst = self.make_l(exit_when=exit_cfg(H.EXIT_BOTH, H.EXIT_WAIT), hold={"on": True, "adx_level": 25.0})
        await self.open_long_at(inst)
        inst.client.mark_price = 105.0
        s = snap_of(2000, dir_e=-1, dir_f=-1, close=105.0, adx=10.0)      # flip signal held (in profit, ADX low)
        s.exit_long = True
        await self.tick(inst, s)
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_the_same_candle_is_not_acted_on_twice(self):
        inst = self.make_l(exit_when=exit_cfg(H.EXIT_BOTH, H.EXIT_WAIT))
        await self.open_long_at(inst)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.exit_long = True
        inst.client.mark_price = 101.0
        await self.tick(inst, s)
        await self.tick(inst, s)
        self.assertEqual(len(inst.client.close_orders), 1)

    async def test_a_normal_close_after_an_exit_clears_the_forced_block(self):
        # the block of an exit is forced; the block of any other close follows "Allow Mid-Trend Re-entry" again
        inst = self.make_l(exit_when=exit_cfg(H.EXIT_BOTH, H.EXIT_WAIT), allow_reentry=True)
        await self.long_with_exit(inst, exit_long=True)
        self.assertTrue(inst.hub.reentry_forced)
        await self.tick(inst, snap_of(3000, dir_e=0, dir_f=0, close=101.0))      # signal ended: block ends
        inst.client.mark_price = 101.0
        await self.tick(inst, snap_of(4000, dir_e=1, dir_f=1, close=101.0))      # fresh long
        self.assertEqual(inst.state.direction, "LONG")
        inst.client.mark_price = 99.0
        await self.tick(inst, snap_of(5000, dir_e=-1, dir_f=-1, close=99.0))     # a plain flip
        self.assertFalse(inst.hub.reentry_forced)

    async def test_old_hub_memory_without_the_flag_loads(self):
        from app import hub_state
        st = hub_state.load("binance", "no_such_account", "BTCUSDT")
        self.assertFalse(st.reentry_forced)
        hub_state.HubState.__dataclass_fields__["reentry_forced"]


class TestLiveSupertrendTrail(LadderBase):
    def trail_sup(self):
        return {"on": True, "type": H.TRAIL_SUP}

    async def test_first_level_is_the_line_at_the_entry(self):
        inst = self.make_l(trail=self.trail_sup())
        await self.open_long_at(inst, sup_line=95.0)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 95.0)
        self.assertEqual(inst.hub.ladder["ex"]["name"], "ts")

    async def test_wrong_side_at_the_entry_gives_no_level_and_the_trade_still_opens(self):
        inst = self.make_l(trail=self.trail_sup())
        await self.open_long_at(inst, sup_line=105.0)
        self.assertIsNone(inst.hub.ladder["ts"])
        self.assertEqual(inst.state.direction, "LONG")

    async def test_no_line_at_all_gives_no_level_and_the_trade_still_opens(self):
        inst = self.make_l(trail=self.trail_sup())
        await self.open_long_at(inst)                            # no line at all
        self.assertIsNone(inst.hub.ladder["ts"])
        self.assertEqual(inst.state.direction, "LONG")

    async def test_it_follows_the_line_at_each_candle_close_and_only_tightens_long(self):
        inst = self.make_l(trail=self.trail_sup())
        await self.open_long_at(inst, sup_line=95.0)
        for t, close, line, expect in ((2000, 102.0, 97.0, 97.0), (3000, 104.0, 99.0, 99.0),
                                       (4000, 103.0, 96.0, 99.0),       # the line drops: never back
                                       (5000, 105.0, 101.0, 101.0)):
            inst.client.mark_price = close
            s = snap_of(t, dir_e=1, dir_f=1, close=close)
            s.sup_line = line
            await self.tick(inst, s)
            self.assertAlmostEqual(inst.hub.ladder["ts"], expect, msg=str(t))
            self.assertEqual(inst.state.status, "IN_POSITION")

    async def test_a_line_on_the_wrong_side_does_not_take_charge_and_does_not_close(self):
        inst = self.make_l(trail=self.trail_sup())
        await self.open_long_at(inst, sup_line=95.0)
        inst.client.mark_price = 100.0
        s = snap_of(2000, dir_e=1, dir_f=1, close=100.0)
        s.sup_line = 103.0                                       # the trend turned: the line is above the price
        await self.tick(inst, s)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 95.0)
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(inst.client.close_orders, [])

    async def test_short_is_mirrored(self):
        inst = self.make_l(trail=self.trail_sup())
        inst.client.mark_price = 100.0
        s = snap_of(1000, dir_e=-1, dir_f=-1, close=100.0)
        s.sup_line = 105.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertAlmostEqual(inst.hub.ladder["ts"], 105.0)
        for t, close, line, expect in ((2000, 98.0, 103.0, 103.0), (3000, 99.0, 106.0, 103.0), (4000, 96.0, 100.0, 100.0)):
            inst.client.mark_price = close
            s = snap_of(t, dir_e=-1, dir_f=-1, close=close)
            s.sup_line = line
            await self.tick(inst, s)
            self.assertAlmostEqual(inst.hub.ladder["ts"], expect, msg=str(t))

    async def test_other_types_are_not_touched(self):
        inst = self.make_l(trail={"on": True, "type": H.TRAIL_LLHH, "length": 5})
        await self.open_long_at(inst, sup_line=95.0)
        self.assertIsNone(inst.hub.ladder["ts"])


# ---------------------------------------------------------------------------------------------- paper side + backtest
class TestPaperAndBacktest(unittest.TestCase):
    def book_long(self, plan, entry_t=1000):
        book = B.new_book(1000.0)
        s = snap_of(entry_t, dir_e=1, dir_f=1, close=100.0)
        B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=entry_t + 1)
        self.assertEqual(book["pos"], 1)
        return book

    def test_paper_exit_wait(self):
        plan = H.complete_plan(live_plan(H.EXIT_WAIT, allow_reentry=True))
        book = self.book_long(plan)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.exit_long = True
        ev, closed = B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=2001)
        self.assertEqual(book["pos"], 0)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "Exit when")
        self.assertTrue(book["reentry_forced"])
        # still long signal: no entry even with re-entry allowed
        ev, closed = B.on_candle(book, snap_of(3000, dir_e=1, dir_f=1, close=102.0), plan, tick_size=0.1,
                                 last_total=None, balance=1000.0, t_ms=3001)
        self.assertEqual(book["pos"], 0)
        # the signal ends, then a fresh one enters
        B.on_candle(book, snap_of(4000, dir_e=0, dir_f=0, close=102.0), plan, tick_size=0.1, last_total=None,
                    balance=1000.0, t_ms=4001)
        self.assertFalse(book["reentry_forced"])
        B.on_candle(book, snap_of(5000, dir_e=1, dir_f=1, close=102.0), plan, tick_size=0.1, last_total=None,
                    balance=1000.0, t_ms=5001)
        self.assertEqual(book["pos"], 1)

    def test_paper_exit_flip(self):
        plan = H.complete_plan(live_plan(H.EXIT_FLIP))
        book = self.book_long(plan)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.exit_long = True
        B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=2001)
        self.assertEqual(book["pos"], -1)                         # closed and opened the other side

    def test_paper_off_ignores_it(self):
        plan = H.complete_plan(make_plan(ind_ma1={"entry": True, "flip": True}))
        book = self.book_long(plan)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.exit_long = True
        B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=2001)
        self.assertEqual(book["pos"], 1)

    def test_paper_flip_signal_wins_on_the_same_candle(self):
        plan = H.complete_plan(live_plan(H.EXIT_WAIT))
        book = self.book_long(plan)
        s = snap_of(2000, dir_e=-1, dir_f=-1, close=99.0)
        s.exit_long = True
        _, closed = B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=2001)
        self.assertEqual(closed[0]["exit_reason"], "FLIP")
        self.assertEqual(book["pos"], -1)

    def test_paper_supertrend_trail(self):
        plan = H.complete_plan(make_plan(ind_ma1={"entry": True}, trail={"on": True, "type": H.TRAIL_SUP}))
        book = B.new_book(1000.0)
        s = snap_of(1000, dir_e=1, dir_f=1, close=100.0)
        s.sup_line = 95.0
        B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=1001)
        self.assertAlmostEqual(book["levels"]["ts"], 95.0)
        for t, close, line, expect in ((2000, 102.0, 97.0, 97.0), (3000, 103.0, 96.0, 97.0), (4000, 104.0, 108.0, 97.0)):
            s = snap_of(t, dir_e=1, dir_f=1, close=close)
            s.sup_line = line
            B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=t + 1)
            self.assertAlmostEqual(book["levels"]["ts"], expect, msg=str(t))
            self.assertEqual(book["pos"], 1)

    def test_backtest_runs_with_the_new_things_and_exit_when_shows_up(self):
        df = candles(1400, seed=41, vol=1.5)
        for after in H.EXIT_AFTERS:
            p = make_plan(ind_mx={"entry": True, "flip": True}, exit_when=exit_cfg(H.EXIT_BOTH, after),
                          allow_reentry=True)
            p["ind"]["mx"]["modes"] = [H.MX_MODES[0]]
            p["ind"]["ma1"].update(type="EMA", length=9)
            p["ind"]["ma2"].update(type="EMA", length=21)
            p["sizing"].update(stop_on=False)
            p["sl"] = {"on": False}
            res = BT.run(df, p, 1000.0, 0.0, 0.0)
            reasons = [t["exit_reason"] for t in res["trades"]]
            self.assertGreater(len(reasons), 10)
            self.assertTrue(any(r == "Exit when" for r in reasons), after)
            off = dict(p, exit_when={"on": False})
            res_off = BT.run(df, off, 1000.0, 0.0, 0.0)
            self.assertFalse(any(t["exit_reason"] == "Exit when" for t in res_off["trades"]))
            if after == H.EXIT_WAIT:
                # nothing opens on the exit candle: the next trade starts on a LATER candle
                rows = res["trades"]
                for a, b in zip(rows, rows[1:]):
                    if a["exit_reason"] == "Exit when":
                        self.assertGreater(b["entry_time"], a["exit_time"] - 1)
                        self.assertNotEqual(b["entry_time"], a["exit_time"] - 1)
            else:
                rows = res["trades"]
                hit = False
                for a, b in zip(rows, rows[1:]):
                    if a["exit_reason"] == "Exit when":
                        self.assertEqual(b["entry_time"], a["exit_time"] - 1)         # the other side, same candle
                        self.assertNotEqual(a["direction"], b["direction"])
                        hit = True
                self.assertTrue(hit)

    def test_backtest_with_bollinger_supertrend_and_candle_option(self):
        df = candles(1400, seed=43, vol=1.5)
        p = make_plan(ind_candle={"entry": True, "flip": True}, ind_bb={"entry": True}, ind_sup={"entry": False},
                      chart_type="Heikin Ashi", allow_reentry=True,
                      trail={"on": True, "type": H.TRAIL_SUP})
        p["ind"]["candle"]["prev_opposite"] = True
        p["ind"]["bb"].update(length=20, mult=1.5, n=1)
        p["sizing"].update(stop_on=False)
        p["sl"] = {"on": False}
        res = BT.run(df, p, 1000.0, 0.0, 0.0)
        self.assertGreater(len(res["trades"]), 5)
        self.assertTrue(any("Trailing Stop" in str(t["exit_reason"]) for t in res["trades"]))


if __name__ == "__main__":
    unittest.main()
