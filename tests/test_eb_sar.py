"""EMA Band and Parabolic SAR (owner 2026-11-05): the signals, the ticks, no look-ahead, validation, the counter-trend
size decider and the paper / backtest copy. Arbitrary test numbers - the product has no default plan."""
import copy
import itertools
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402

TICKS = ("long_low", "long_high", "short_high", "short_low")


def walk(n=400, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1.2, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.random(n) * 1.0
    l = np.minimum(o, c) - rng.random(n) * 1.0
    return pd.DataFrame({"open_time": np.arange(n) * 3600000, "open": o, "high": h, "low": l, "close": c,
                         "volume": rng.random(n) * 100 + 50})


def eb_plan(length=8, n=1, ticks=TICKS, entry=True, **extra):
    blk = {"entry": entry, "length": length, "n": n}
    blk.update({k: (k in ticks) for k in TICKS})
    blk.update(extra)
    return make_plan(ind_eb=blk)


def brute(df, length, n, tick):
    """The rule written the long way, candle by candle (independent of the vectorised code)."""
    c = df["close"].values
    top = H.pine_ema(df["high"].values, length)
    bot = H.pine_ema(df["low"].values, length)
    line = {"long_low": bot, "long_high": top, "short_high": top, "short_low": bot}[tick]
    up = tick.startswith("long")
    out = np.zeros(len(c), dtype=bool)
    for i in range(len(c)):
        if np.isnan(line[i]):
            continue
        now = c[i] > line[i] if up else c[i] < line[i]
        was = any((not np.isnan(line[j])) and (c[j] < line[j] if up else c[j] > line[j]) for j in range(max(0, i - n), i))
        out[i] = now and was
    return out


class TestEmaBandSignals(unittest.TestCase):
    def test_the_three_lines_are_emas_of_the_high_close_and_low(self):
        df = walk()
        f = H.compute_hub(df, eb_plan(length=8))
        self.assertTrue(np.allclose(f["eb_top"].values, H.pine_ema(df["high"].values, 8), equal_nan=True))
        self.assertTrue(np.allclose(f["eb_mid"].values, H.pine_ema(df["close"].values, 8), equal_nan=True))
        self.assertTrue(np.allclose(f["eb_bot"].values, H.pine_ema(df["low"].values, 8), equal_nan=True))
        ok = ~np.isnan(f["eb_top"].values)
        self.assertTrue((f["eb_top"].values[ok] >= f["eb_bot"].values[ok]).all())

    def test_each_tick_alone_matches_the_rule_written_the_long_way(self):
        for seed in (1, 2, 3):
            df = walk(seed=seed)
            for n in (1, 3):
                for tick in TICKS:
                    f = H.compute_hub(df, eb_plan(length=8, n=n, ticks=(tick,)))
                    exp = brute(df, 8, n, tick)
                    side = "eb_long" if tick.startswith("long") else "eb_short"
                    self.assertTrue((f[side].values == exp).all(), (seed, n, tick))
                    other = "eb_short" if tick.startswith("long") else "eb_long"
                    self.assertFalse(f[other].any(), (tick, "the other side must stay off"))
                    self.assertGreater(int(exp.sum()), 3, (seed, n, tick, "the test data must contain signals"))

    def test_two_ticks_on_one_side_means_either_line_is_enough(self):
        df = walk()
        both = H.compute_hub(df, eb_plan(ticks=("long_low", "long_high")))["eb_long"].values
        lo = H.compute_hub(df, eb_plan(ticks=("long_low",)))["eb_long"].values
        hi = H.compute_hub(df, eb_plan(ticks=("long_high",)))["eb_long"].values
        self.assertTrue((both == (lo | hi)).all())
        self.assertGreater(int((lo ^ hi).sum()), 0, "the two lines must differ somewhere")
        both_s = H.compute_hub(df, eb_plan(ticks=("short_low", "short_high")))["eb_short"].values
        a = H.compute_hub(df, eb_plan(ticks=("short_low",)))["eb_short"].values
        b = H.compute_hub(df, eb_plan(ticks=("short_high",)))["eb_short"].values
        self.assertTrue((both_s == (a | b)).all())

    def test_a_hand_made_cross(self):
        # length 1: the lines are the candle's own high (top), close (middle) and low (bottom)
        # bar0 close 10 / low 9; bar1 close 8 (below the bottom of bar 1? low 7.5 <= 8, so not below) -> use length 3 instead
        h = [11, 11, 11, 11, 11, 11, 11, 11, 11, 11.0]
        l = [9, 9, 9, 9, 9, 9, 9, 9, 9, 9.0]
        c = [10, 10, 10, 10, 8.0, 8.0, 10.5, 10.5, 10.5, 10.5]       # drops under the bottom line, then back above it
        df = pd.DataFrame({"open_time": np.arange(10) * 3600000, "open": c, "high": h, "low": l, "close": c,
                           "volume": np.ones(10) * 10})
        f = H.compute_hub(df, eb_plan(length=3, n=1, ticks=("long_low",)))
        bot = f["eb_bot"].values
        self.assertLess(c[4], bot[4])                       # really below the bottom line
        self.assertLess(c[5], bot[5])
        self.assertGreater(c[6], bot[6])                    # the cross back up
        sig = f["eb_long"].values
        self.assertTrue(sig[6], "the candle that closes back above, right after a candle below, is the long")
        self.assertFalse(sig[4] or sig[5])                  # still below: nothing
        self.assertFalse(sig[7], "N = 1: one candle later the candle before was no longer below")
        f3 = H.compute_hub(df, eb_plan(length=3, n=3, ticks=("long_low",)))
        self.assertTrue(f3["eb_long"].values[6] and f3["eb_long"].values[7] and f3["eb_long"].values[8])
        self.assertFalse(f3["eb_long"].values[9] and False)

    def test_nothing_looks_ahead(self):
        df = walk()
        full = H.compute_hub(df, eb_plan(n=2))
        for cut in (60, 150, 300):
            part = H.compute_hub(df.iloc[:cut].reset_index(drop=True), eb_plan(n=2))
            for col in ("eb_long", "eb_short", "eb_top", "eb_bot"):
                self.assertTrue(np.allclose(part[col].values.astype(float), full[col].values[:cut].astype(float), equal_nan=True), (col, cut))

    def test_nothing_ticked_gives_no_signal(self):
        f = H.compute_hub(walk(), make_plan(ind_ma1={"entry": True}))
        self.assertFalse(f["eb_long"].any() or f["eb_short"].any())

    def test_it_is_an_entry_block_like_the_others(self):
        df = walk()
        f = H.compute_hub(df, eb_plan())
        self.assertTrue((f["entry_long"].values == f["eb_long"].values).all())
        self.assertTrue((f["entry_short"].values == f["eb_short"].values).all())
        self.assertGreater(int(f["entry_long"].sum()), 3)


class TestEmaBandPlan(unittest.TestCase):
    def test_valid_with_a_tick(self):
        H.validate_plan(eb_plan())

    def test_a_tick_is_needed_when_it_is_used(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(eb_plan(ticks=()))

    def test_no_tick_needed_when_it_is_not_used(self):
        H.validate_plan(make_plan(ind_ma1={"entry": True}))
        p = eb_plan(ticks=(), entry=False)
        p["ind"]["ma1"]["entry"] = True
        H.validate_plan(p)

    def test_length_and_n_must_be_whole_numbers_of_one_or_more(self):
        for key in ("length", "n"):
            for bad in (0, -1, 2.5, "x", None):
                p = eb_plan()
                p["ind"]["eb"][key] = bad
                with self.assertRaises(H.PlanError, msg=(key, bad)):
                    H.validate_plan(p)

    def test_old_plans_without_the_block_load_with_it_off(self):
        p = make_plan(ind_ma1={"entry": True})
        del p["ind"]["eb"], p["ind"]["sar"]
        H.validate_plan(H.upgrade_plan(p))
        up = H.upgrade_plan(p)
        self.assertEqual(up["ind"]["eb"], {"entry": False, "flip": False})
        self.assertEqual(up["ind"]["sar"], {"entry": False, "flip": False})

    def test_the_unused_numbers_are_filled_with_inert_values(self):
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}))
        for k in TICKS:
            self.assertIs(p["ind"]["eb"][k], False)
        self.assertEqual(set(p["ind"]["eb"]) >= {"length", "n"}, True)

    def test_the_summary_lists_it(self):
        self.assertIn("EMA Band", H.plan_summary(H.complete_plan(eb_plan())))

    def test_flip_uses_it_too(self):
        p = eb_plan(entry=False, flip=True)
        p["ind"]["ma1"]["entry"] = True
        p["flip_rules"] = {"same_entry": False, "same_hold": True, "same_wait": True}
        H.validate_plan(p)
        f = H.compute_hub(walk(), p)
        self.assertTrue((f["flip_long"].values == f["eb_long"].values).all())


class TestParabolicSar(unittest.TestCase):
    def test_hand_worked_values(self):
        h = np.array([10, 11, 12, 13, 12.5], dtype=float)
        l = np.array([9, 10, 11, 12, 8], dtype=float)
        c = np.array([9.5, 10.5, 11.5, 12.5, 8.5])
        s = H.pine_sar(h, l, c, 0.02, 0.02, 0.2)
        self.assertTrue(np.isnan(s[0]))
        self.assertAlmostEqual(s[1], 9.0, places=9)
        self.assertAlmostEqual(s[2], 9.0, places=9)
        self.assertAlmostEqual(s[3], 9.12, places=9)
        self.assertAlmostEqual(s[4], 13.0, places=9)        # the price fell through the dots: they flip above the price

    def test_hand_worked_values_when_the_first_move_is_up(self):
        h = np.array([10, 10.5, 11, 12], dtype=float)
        l = np.array([9, 9.5, 10, 11], dtype=float)
        c = np.array([9.5, 10, 10.5, 11.5])
        s = H.pine_sar(h, l, c, 0.02, 0.02, 0.2)
        self.assertTrue(np.isnan(s[0]))
        self.assertAlmostEqual(s[1], 9.0, places=9)          # starts at the first low
        self.assertAlmostEqual(s[2], 9.0, places=9)          # 9 + 0.02*(10.5-9) = 9.03, held back by the lowest low of 2 candles
        self.assertAlmostEqual(s[3], 9.08, places=9)         # 9 + 0.04*(11-9)
        self.assertTrue((s[1:] < c[1:]).all())               # dots below the price: an up trend

    def test_a_close_up_on_an_inside_candle_still_starts_an_up_trend(self):
        h = np.array([11, 10.2, 10.4]); l = np.array([9, 9.6, 9.8]); c = np.array([9.5, 10, 10.1])
        s = H.pine_sar(h, l, c, 0.02, 0.02, 0.2)
        self.assertAlmostEqual(s[1], 9.0, places=9)          # dots start at the first low, below the price
        self.assertAlmostEqual(s[2], 9.0, places=9)

    def test_hand_worked_values_when_the_first_move_is_down(self):
        h = np.array([10, 9.5, 9, 8], dtype=float)
        l = np.array([9, 8.5, 8, 7], dtype=float)
        c = np.array([9.5, 9, 8.5, 7.5])
        s = H.pine_sar(h, l, c, 0.02, 0.02, 0.2)
        self.assertTrue(np.isnan(s[0]))
        self.assertAlmostEqual(s[1], 10.0, places=9)
        self.assertAlmostEqual(s[2], 10.0, places=9)
        self.assertAlmostEqual(s[3], 9.92, places=9)
        self.assertTrue((s[1:] > c[1:]).all())               # dots above the price: a down trend

    def test_when_the_dots_flip_they_start_at_the_extreme_of_the_trend_that_just_ended(self):
        flips = 0
        for seed in range(6):
            df = walk(n=500, seed=seed)
            h, l, c = df["high"].values, df["low"].values, df["close"].values
            s = H.pine_sar(h, l, c, 0.02, 0.02, 0.2)
            side = np.sign(c - s)
            start = 1
            for i in range(2, len(c)):
                if np.isnan(s[i]) or np.isnan(s[i - 1]) or side[i] == side[i - 1] or side[i] == 0 or side[i - 1] == 0:
                    continue
                flips += 1
                if side[i - 1] > 0:      # the dots were below (up trend) and now jump above: at the highest high of that trend
                    self.assertGreaterEqual(s[i] + 1e-9, h[start:i + 1].max(), (seed, i))
                else:                    # the dots were above and now jump below: at the lowest low of that trend
                    self.assertLessEqual(s[i] - 1e-9, l[start:i + 1].min(), (seed, i))
                start = i
        self.assertGreater(flips, 10)

    def test_dots_below_in_an_up_trend_and_above_in_a_down_trend(self):
        n = 60
        up = np.linspace(100, 130, n)
        h, l = up + 1, up - 1
        s = H.pine_sar(h, l, up, 0.02, 0.02, 0.2)
        self.assertTrue((s[1:] < up[1:]).all())
        dn = np.linspace(130, 100, n)
        s = H.pine_sar(dn + 1, dn - 1, dn, 0.02, 0.02, 0.2)
        self.assertTrue((s[1:] > dn[1:]).all())

    def test_the_acceleration_is_capped_by_the_maximum(self):
        n = 80
        up = np.linspace(100, 200, n)
        slow = H.pine_sar(up + 1, up - 1, up, 0.02, 0.02, 0.04)
        fast = H.pine_sar(up + 1, up - 1, up, 0.02, 0.02, 0.2)
        self.assertLess(slow[-1], fast[-1])

    def test_a_bigger_step_follows_the_price_closer(self):
        n = 80
        up = np.linspace(100, 200, n)
        a = H.pine_sar(up + 1, up - 1, up, 0.01, 0.01, 0.2)
        b = H.pine_sar(up + 1, up - 1, up, 0.05, 0.05, 0.2)
        self.assertLess(a[-1], b[-1])

    def test_long_and_short_in_the_hub(self):
        df = walk()
        p = make_plan(ind_sar={"entry": True, "step": 0.02, "limit": 0.2})
        f = H.compute_hub(df, p)
        dots = f["sar_dots"].values
        c = df["close"].values
        again = H.pine_sar(df["high"].values, df["low"].values, c, 0.02, 0.02, 0.2)      # the increase is the step
        self.assertTrue(np.allclose(dots, again, equal_nan=True))
        self.assertTrue((f["sar_long"].values == (c > dots)).all())
        self.assertTrue((f["sar_short"].values == (c < dots)).all())
        self.assertGreater(int(f["sar_long"].sum()), 20)
        self.assertGreater(int(f["sar_short"].sum()), 20)
        self.assertFalse((f["sar_long"].values & f["sar_short"].values).any())
        self.assertFalse(f["sar_long"].values[0] or f["sar_short"].values[0])

    def test_nothing_looks_ahead(self):
        df = walk()
        p = make_plan(ind_sar={"entry": True, "step": 0.02, "limit": 0.2})
        full = H.compute_hub(df, p)
        for cut in (40, 150, 300):
            part = H.compute_hub(df.iloc[:cut].reset_index(drop=True), p)
            self.assertTrue(np.allclose(part["sar_dots"].values, full["sar_dots"].values[:cut], equal_nan=True))
            self.assertTrue((part["sar_long"].values == full["sar_long"].values[:cut]).all())

    def test_validation(self):
        H.validate_plan(make_plan(ind_sar={"entry": True, "step": 0.02, "limit": 0.2}))
        H.validate_plan(make_plan(ind_sar={"entry": True, "step": 0.2, "limit": 0.2}))
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan(ind_sar={"entry": True, "step": 0.2, "limit": 0.1}))
        for key in ("step", "limit"):
            for bad in (0, -1, "x", None):
                p = make_plan(ind_sar={"entry": True, "step": 0.02, "limit": 0.2})
                p["ind"]["sar"][key] = bad
                with self.assertRaises(H.PlanError, msg=(key, bad)):
                    H.validate_plan(p)

    def test_unused_numbers_do_not_matter(self):
        p = make_plan(ind_ma1={"entry": True})
        H.validate_plan(p)
        q = copy.deepcopy(p)
        q["ind"]["sar"].update(step=0.5, limit=0.1)        # inconsistent, but the block is off: ignored
        H.validate_plan(q)


class TestCounterTrendSizing(unittest.TestCase):
    def ct_plan(self, by, **blk):
        return make_plan(ind_ma1={"entry": True}, sizing={"counter": {"on": True, "by": by, "alloc_pct": 1.0, "leverage": 2}},
                         **{"ind_" + by: blk})

    def test_the_ctrend_list_has_both(self):
        self.assertIn("eb", H.CT_BLOCKS)
        self.assertIn("sar", H.CT_BLOCKS)

    def test_sar_decides_the_counter_trend(self):
        p = self.ct_plan("sar", step=0.02, limit=0.2)
        H.validate_plan(p)
        f = H.compute_hub(walk(), p)
        self.assertTrue((f["ct_long"].values == f["sar_long"].values).all())
        self.assertTrue((f["ct_short"].values == f["sar_short"].values).all())

    def test_eb_decides_the_counter_trend(self):
        p = self.ct_plan("eb", length=8, n=2, long_low=True, short_high=True)
        H.validate_plan(p)
        f = H.compute_hub(walk(), p)
        self.assertTrue((f["ct_long"].values == f["eb_long"].values).all())
        self.assertTrue((f["ct_short"].values == f["eb_short"].values).all())

    def test_eb_as_the_decider_still_needs_a_tick(self):
        p = self.ct_plan("eb", length=8, n=2)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)


class TestBacktestRuns(unittest.TestCase):
    def test_both_trade_in_the_backtest_on_their_own_signal(self):
        from app import hub_backtest as BT
        df = walk(n=900, seed=5)
        for p, key in ((eb_plan(length=8, n=2), "eb"),
                       (make_plan(ind_sar={"entry": True, "step": 0.02, "limit": 0.2}), "sar")):
            pc = H.complete_plan(p)
            f = H.compute_hub(df, pc)
            r = BT.run(df, pc, 1000.0, 0.05, 0.02)
            self.assertGreaterEqual(len(r["trades"]), 2)
            for t in r["trades"]:
                i = int(t["entry_time"] // 3600000)
                col = key + ("_long" if t["direction"] == "LONG" else "_short")
                self.assertTrue(f[col].values[i], (key, t["direction"], i))      # entered on its own signal candle


if __name__ == "__main__":
    unittest.main()
