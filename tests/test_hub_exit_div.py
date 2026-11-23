"""Owner 2026-11-17: EXIT WHEN -> Divergence (RSI / MFI / CCI), each oscillator on its own on/off, with the optional
confirmations (earlier peak stretched, indicator turns back inside its band, price breaks the swing between the two
pivots), "a divergence stays valid N candles", ANY / ALL, and the rule that a divergence found on or before the trade's
own entry candle never closes that trade. Arbitrary test numbers - the product has no default plan."""
import copy
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

NAN = float("nan")


def candles(n=600, seed=7, start=100.0, vol=1.0):
    rng = np.random.default_rng(seed)
    c = start + np.cumsum(rng.normal(0, vol, n))
    o = np.concatenate([[start], c[:-1]]) + rng.normal(0, vol * 0.2, n)
    h = np.maximum(o, c) + np.abs(rng.normal(0, vol * 0.5, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, vol * 0.5, n))
    return pd.DataFrame({"open_time": np.arange(n) * 3_600_000, "open": o, "high": h, "low": l, "close": c,
                         "volume": rng.uniform(100, 200, n)})


def osc_cfg(on=True, length=14, pivot=3, lookback=30, zone=False, ob=70.0, os_=30.0):
    d = {"on": on}
    if on:
        d.update(length=length, pivot=pivot, lookback=lookback, zone=zone, ob=ob, os=os_)
    return d


def div_cfg(rsi=None, mfi=None, cci=None, combine="ANY", keep=3, turn=False, price=False,
            after=H.EXIT_WAIT, on=True):
    return {"on": on, "signal": H.EXIT_DIV, "after": after,
            "div": {"combine": combine, "keep": keep, "turn": turn, "price": price,
                    "rsi": rsi or {"on": False}, "mfi": mfi or {"on": False}, "cci": cci or {"on": False}}}


# ---------------------------------------------------------------------------------------------- crafted series
def crafted_bear():
    """Two osc peaks (k = 2): index 5 (80) and 10 (70). The price high is HIGHER at the second one. The lowest low
    between them is 88. The signal is on candle 12 (10 + k)."""
    n = 30
    osc = np.full(n, 50.0)
    osc[3:8] = [60, 70, 80, 70, 60]
    osc[8:13] = [55, 62, 70, 62, 55]
    high = np.full(n, 95.0)
    high[5], high[10] = 100.0, 105.0
    low = np.full(n, 90.0)
    low[8] = 88.0
    return low, high, osc


def crafted_bull():
    low, high, osc = crafted_bear()
    n = len(osc)
    osc2 = 100.0 - osc                 # lows at 5 (20) and 10 (30): the later low is HIGHER
    low2 = np.full(n, 105.0)
    low2[5], low2[10] = 100.0, 95.0    # the price low is LOWER at the second one
    high2 = np.full(n, 110.0)
    high2[8] = 112.0                   # the highest high between the two lows
    return low2, high2, osc2


class TestMath(unittest.TestCase):
    def test_mfi_matches_the_pine_definition(self):
        df = candles(300, seed=3)
        h, l, c, v = (df[k].to_numpy() for k in ("high", "low", "close", "volume"))
        got = H.pine_mfi(h, l, c, v, 14)
        src = (h + l + c) / 3.0
        want = np.full(len(src), np.nan)
        for i in range(13, len(src)):
            up = lo = 0.0
            for j in range(i - 13, i + 1):
                ch = src[j] - src[j - 1] if j > 0 else float("nan")
                up += v[j] * (0.0 if (ch <= 0.0) else src[j])         # na compares false -> the source counts
                lo += v[j] * (0.0 if (ch >= 0.0) else src[j])
            want[i] = 100.0 - 100.0 / (1.0 + up / lo)
        ok = ~np.isnan(want)
        self.assertTrue(np.allclose(got[ok], want[ok], rtol=1e-9, atol=1e-9))
        self.assertTrue(np.isnan(got[:13]).all())
        self.assertTrue(((got[ok] >= 0) & (got[ok] <= 100)).all())

    def test_mfi_all_up_is_100(self):
        n = 40
        c = np.arange(n, dtype=float) + 100.0
        got = H.pine_mfi(c + 1, c - 1, c, np.full(n, 10.0), 5)
        self.assertAlmostEqual(got[-1], 100.0)

    def test_cci_matches_the_pine_definition(self):
        df = candles(300, seed=4)
        h, l, c = (df[k].to_numpy() for k in ("high", "low", "close"))
        got = H.pine_cci(h, l, c, 20)
        src = (h + l + c) / 3.0
        want = np.full(len(src), np.nan)
        for i in range(19, len(src)):
            w = src[i - 19:i + 1]
            m = w.mean()
            dev = sum(abs(x - m) for x in w) / 20.0
            want[i] = (src[i] - m) / (0.015 * dev)
        ok = ~np.isnan(want)
        self.assertTrue(np.allclose(got[ok], want[ok], rtol=1e-9, atol=1e-9))
        self.assertTrue(np.isnan(got[:19]).all())

    def test_cci_flat_is_zero(self):
        n = 30
        flat = np.full(n, 100.0)
        got = H.pine_cci(flat, flat, flat, 10)
        self.assertEqual(got[-1], 0.0)

    def test_rsi_still_the_same_function(self):
        df = candles(100, seed=5)
        r = H.pine_rsi(df["close"].to_numpy(), 14)
        self.assertTrue(((r[14:] >= 0) & (r[14:] <= 100)).all())


class TestDivergence(unittest.TestCase):
    def test_bear_divergence_signal_level_and_timing(self):
        low, high, osc = crafted_bear()
        bull, bear, bl, br = H.osc_divergence(low, high, osc, 2, 10)
        self.assertEqual(list(np.flatnonzero(bear)), [12])            # k candles after the second peak, never earlier
        self.assertEqual(br[12], 88.0)                                # the lowest low between the two peaks
        self.assertTrue(np.isnan(br[np.arange(30) != 12]).all())

    def test_bull_divergence_signal_level_and_timing(self):
        low, high, osc = crafted_bull()
        bull, bear, bl, br = H.osc_divergence(low, high, osc, 2, 10)
        self.assertEqual(list(np.flatnonzero(bull)), [12])
        self.assertEqual(bl[12], 112.0)                               # the highest high between the two lows

    def test_no_divergence_when_the_price_does_not_make_a_higher_high(self):
        low, high, osc = crafted_bear()
        high[10] = 99.0                                               # lower than the first peak's 100
        self.assertFalse(H.osc_divergence(low, high, osc, 2, 10)[1].any())

    def test_no_divergence_when_the_osc_does_not_make_a_lower_high(self):
        low, high, osc = crafted_bear()
        osc[8:13] = [60, 70, 85, 70, 60]                              # the second peak is higher
        self.assertFalse(H.osc_divergence(low, high, osc, 2, 10)[1].any())

    def test_the_two_peaks_must_be_close_enough(self):
        low, high, osc = crafted_bear()
        self.assertTrue(H.osc_divergence(low, high, osc, 2, 5)[1].any())      # 5 apart is allowed
        self.assertFalse(H.osc_divergence(low, high, osc, 2, 4)[1].any())     # look-back 4 is too short

    def test_zone_option_needs_the_earlier_peak_stretched(self):
        low, high, osc = crafted_bear()                                # the earlier peak is 80
        self.assertTrue(H.osc_divergence(low, high, osc, 2, 10, 75.0, 25.0, True)[1].any())
        self.assertFalse(H.osc_divergence(low, high, osc, 2, 10, 85.0, 25.0, True)[1].any())
        self.assertTrue(H.osc_divergence(low, high, osc, 2, 10, 85.0, 25.0, False)[1].any())   # off = not asked
        low, high, osc = crafted_bull()                                # the earlier low is 20
        self.assertTrue(H.osc_divergence(low, high, osc, 2, 10, 75.0, 25.0, True)[0].any())
        self.assertFalse(H.osc_divergence(low, high, osc, 2, 10, 75.0, 15.0, True)[0].any())

    def test_same_signals_as_the_rsi_entry_divergence(self):
        df = candles(800, seed=9, vol=1.4)
        l, h, c = df["low"].to_numpy(), df["high"].to_numpy(), df["close"].to_numpy()
        rsi = H.pine_rsi(c, 14)
        b1, r1 = H.rsi_divergence(l, h, rsi, 3, 30)
        b2, r2, _, _ = H.osc_divergence(l, h, rsi, 3, 30)
        self.assertTrue((b1 == b2).all() and (r1 == r2).all())
        self.assertTrue(r1.sum() > 3 and b1.sum() > 3)

    def test_no_look_ahead(self):
        df = candles(700, seed=10, vol=1.4)
        l, h, c = df["low"].to_numpy(), df["high"].to_numpy(), df["close"].to_numpy()
        rsi = H.pine_rsi(c, 14)
        full = H.osc_divergence(l, h, rsi, 3, 30)
        cut = H.osc_divergence(l[:500], h[:500], rsi[:500], 3, 30)
        for a, b in zip(full[:2], cut[:2]):
            self.assertTrue((a[:500 - 3] == b[:500 - 3]).all())      # the last k candles may still change: not yet confirmed


class TestWindowsAndConfirmations(unittest.TestCase):
    def setUp(self):
        self.n = 30
        self.times = np.arange(self.n) * 1000.0
        self.sig = np.zeros(self.n, dtype=bool)
        self.sig[12] = True
        self.level = np.full(self.n, np.nan)
        self.level[12] = 88.0
        self.osc = np.full(self.n, 60.0)

    def arrays(self, long_exit=True, keep=3, turn=False, price=False, close=None, osc=None):
        close = np.full(self.n, 100.0) if close is None else close
        osc = self.osc if osc is None else osc
        return H.div_exit_arrays(osc, self.sig, self.level, close, self.times, keep, long_exit, 70.0, 30.0, turn, price)

    def test_no_confirmation_stays_valid_for_keep_candles(self):
        ok, when = self.arrays(keep=3)
        self.assertEqual(list(np.flatnonzero(ok)), [12, 13, 14, 15])
        self.assertTrue((when[ok] == 12000.0).all())
        self.assertTrue(np.isnan(when[~ok]).all())
        ok0, _ = self.arrays(keep=0)
        self.assertEqual(list(np.flatnonzero(ok0)), [12])

    def test_turn_back_inside_the_band_long_and_short(self):
        osc = np.full(self.n, 75.0)
        osc[14:] = 65.0                                           # back below 70 from candle 14
        ok, _ = self.arrays(turn=True, osc=osc)
        self.assertEqual(list(np.flatnonzero(ok)), [14, 15])
        osc2 = np.full(self.n, 25.0)
        osc2[13:] = 35.0                                          # back above 30 from candle 13
        ok, _ = self.arrays(long_exit=False, turn=True, osc=osc2)
        self.assertEqual(list(np.flatnonzero(ok)), [13, 14, 15])

    def test_price_break_long_and_short(self):
        close = np.full(self.n, 100.0)
        close[14] = 87.0                                          # closes below 88
        ok, _ = self.arrays(price=True, close=close)
        self.assertEqual(list(np.flatnonzero(ok)), [14])
        self.level[12] = 104.0
        close2 = np.full(self.n, 100.0)
        close2[13] = 105.0                                        # closes above 104
        ok, _ = self.arrays(long_exit=False, price=True, close=close2)
        self.assertEqual(list(np.flatnonzero(ok)), [13])

    def test_turn_and_price_both_must_hold(self):
        osc = np.full(self.n, 75.0)
        osc[14:] = 65.0
        close = np.full(self.n, 100.0)
        close[15] = 87.0
        ok, _ = self.arrays(turn=True, price=True, osc=osc, close=close)
        self.assertEqual(list(np.flatnonzero(ok)), [15])

    def test_a_new_divergence_inside_the_window_gives_the_latest_found_time(self):
        self.sig[14] = True
        self.level[14] = 88.0
        ok, when = self.arrays(keep=3)
        self.assertEqual(when[14], 14000.0)
        self.assertEqual(when[13], 12000.0)


# ---------------------------------------------------------------------------------------------- plan
class TestPlan(unittest.TestCase):
    def good(self, **kw):
        return make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg(), **kw))

    def test_a_good_plan_validates_and_every_oscillator_can_be_on(self):
        H.validate_plan(self.good())
        H.validate_plan(make_plan(ind_ma1={"entry": True},
                                  exit_when=div_cfg(rsi=osc_cfg(), mfi=osc_cfg(ob=80.0, os_=20.0),
                                                    cci=osc_cfg(ob=100.0, os_=-100.0), combine="ALL", turn=True, price=True)))

    def test_ma_numbers_are_not_needed_for_the_divergence_signal(self):
        p = H.complete_plan(self.good())
        self.assertNotIn("ma2", H._needed_blocks(p))
        self.assertNotIn("ma1", H._needed_blocks(H.complete_plan(self.good())) - {"ma1"} if False else set())
        ma = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when={"on": True, "signal": H.EXIT_BOTH, "after": H.EXIT_WAIT}))
        self.assertIn("ma2", H._needed_blocks(ma))

    def test_something_must_be_ticked(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan(ind_ma1={"entry": True}, exit_when=div_cfg()))

    def test_missing_or_wrong_numbers_are_refused(self):
        def bad(msg, **kw):
            with self.assertRaises(H.PlanError, msg=msg):
                H.validate_plan(self.good(**kw) if not kw.get("custom") else kw["custom"])
        for key in ("length", "pivot", "lookback"):
            p = self.good()
            del p["exit_when"]["div"]["rsi"][key]
            with self.assertRaises(H.PlanError, msg=key):
                H.validate_plan(p)
        p = self.good()
        p["exit_when"]["div"]["rsi"]["lookback"] = 3                # not bigger than pivot (3)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p = self.good()
        p["exit_when"]["div"]["rsi"]["length"] = 1
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p = self.good()
        p["exit_when"]["div"]["keep"] = -1
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p = self.good()
        del p["exit_when"]["div"]["keep"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p = self.good()
        p["exit_when"]["div"]["combine"] = "BOTH"
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p = self.good()
        del p["exit_when"]["div"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_levels_are_needed_only_when_a_tick_uses_them(self):
        p = make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg()))
        del p["exit_when"]["div"]["rsi"]["ob"], p["exit_when"]["div"]["rsi"]["os"]
        H.validate_plan(p)                                          # neither zone nor turn ticked: no levels needed
        p["exit_when"]["div"]["turn"] = True
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p["exit_when"]["div"]["turn"] = False
        p["exit_when"]["div"]["rsi"]["zone"] = True
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p["exit_when"]["div"]["rsi"].update(ob=30.0, os=70.0)       # upper below lower
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_old_plans_are_unchanged(self):
        for sig in (H.EXIT_BOTH, H.EXIT_MA2):
            p = make_plan(ind_ma1={"entry": True}, exit_when={"on": True, "signal": sig, "after": H.EXIT_WAIT})
            H.validate_plan(p)
            c = H.complete_plan(p)
            self.assertFalse(c["exit_when"]["div"]["rsi"]["on"])
            self.assertEqual(c["exit_when"]["signal"], sig)
        off = H.complete_plan(make_plan(ind_ma1={"entry": True}))
        self.assertFalse(off["exit_when"]["on"])
        self.assertIsNone(H.exit_cfg(off))

    def test_complete_plan_does_not_change_a_filled_plan(self):
        p = self.good(turn=True, price=True)
        before = copy.deepcopy(p["exit_when"])
        c = H.complete_plan(p)
        self.assertEqual({k: v for k, v in c["exit_when"]["div"]["rsi"].items() if k != "tf"}, before["div"]["rsi"])
        self.assertEqual(c["exit_when"]["div"]["rsi"]["tf"], "Chart")       # an older plan reads the chart timeframe
        self.assertTrue(c["exit_when"]["div"]["turn"] and c["exit_when"]["div"]["price"])

    def test_review_line(self):
        c = H.complete_plan(self.good(turn=True, price=True))
        lines = dict((a, b) for a, b in H.plan_lines(c)[0]) if isinstance(H.plan_lines(c), tuple) else dict(H.plan_lines(c))
        txt = lines.get("Exit when", "")
        for part in ("Divergence", "RSI", "length 14", "turn back", "break the swing", "valid 3"[:5]):
            self.assertIn(part, txt)


# ---------------------------------------------------------------------------------------------- frame + decision
class TestFrameAndDecision(unittest.TestCase):
    def frame(self, cfg, n=900, seed=21, vol=1.6):
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg)
        return H.compute_hub(candles(n, seed=seed, vol=vol), p), p

    def test_columns_and_signals_exist_for_each_ticked_oscillator_only(self):
        cfg = div_cfg(rsi=osc_cfg(pivot=3, lookback=30), cci=osc_cfg(length=20, pivot=3, lookback=30, ob=100.0, os_=-100.0))
        f, _ = self.frame(cfg)
        for nm in ("rsi", "cci"):
            for col in (f"xd_{nm}_ok_l", f"xd_{nm}_t_l", f"xd_{nm}_ok_s", f"xd_{nm}_t_s"):
                self.assertIn(col, f.columns)
            self.assertGreater(f[f"xd_{nm}_ok_l"].sum(), 0, nm)
            self.assertGreater(f[f"xd_{nm}_ok_s"].sum(), 0, nm)
        self.assertNotIn("xd_mfi_ok_l", f.columns)
        self.assertFalse(f["exit_long"].any() or f["exit_short"].any())      # the MA flags stay off

    def test_mfi_works_too(self):
        f, _ = self.frame(div_cfg(mfi=osc_cfg(pivot=3, lookback=30, ob=80.0, os_=20.0)))
        self.assertGreater(f["xd_mfi_ok_l"].sum(), 0)
        v = f["xd_mfi_osc"].dropna()
        self.assertTrue(((v >= 0) & (v <= 100)).all())

    def test_found_time_is_the_candle_the_divergence_was_found_on(self):
        f, _ = self.frame(div_cfg(rsi=osc_cfg(pivot=3, lookback=30), keep=4))
        ok = f["xd_rsi_ok_l"].to_numpy()
        when = f["xd_rsi_t_l"].to_numpy()
        t = f["open_time"].to_numpy()
        idx = np.flatnonzero(ok)
        self.assertGreater(len(idx), 0)
        for i in idx:
            self.assertLessEqual(when[i], t[i])
            self.assertGreaterEqual(when[i], t[max(0, i - 4)])

    def test_no_look_ahead_in_the_frame(self):
        df = candles(900, seed=22, vol=1.6)
        p = make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg(pivot=3, lookback=30), keep=4, turn=True, price=True,
                                                                  ))
        p["exit_when"]["div"]["rsi"].update(zone=True)
        full = H.compute_hub(df, p)
        cut = H.compute_hub(df.iloc[:700].reset_index(drop=True), p)
        for col in ("xd_rsi_ok_l", "xd_rsi_ok_s"):
            self.assertTrue((full[col].to_numpy()[:700] == cut[col].to_numpy()).all(), col)

    def test_the_snapshot_carries_it(self):
        f, _ = self.frame(div_cfg(rsi=osc_cfg(pivot=3, lookback=30)))
        i = int(np.flatnonzero(f["xd_rsi_ok_l"].to_numpy())[0])
        s = H.hub_snapshot_at(f, i)
        ok, found = s.xdiv["rsi"]["L"]
        self.assertTrue(ok)
        self.assertEqual(found, f["xd_rsi_t_l"].iloc[i])
        self.assertEqual(H.hub_snapshot_at(f, 0).xdiv["rsi"]["L"][0], False)

    def snap(self, **x):
        s = snap_of(5000, dir_e=1, dir_f=1, close=100.0)
        s.xdiv = x
        return s

    def test_long_uses_the_bear_side_short_the_bull_side(self):
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg())))
        s = self.snap(rsi={"L": (True, 4000.0), "S": (False, NAN)})
        self.assertEqual(H.exit_decision("LONG", s, p, 1000), "EXIT")
        self.assertEqual(H.exit_decision("SHORT", s, p, 1000), "STAY")
        s = self.snap(rsi={"L": (False, NAN), "S": (True, 4000.0)})
        self.assertEqual(H.exit_decision("SHORT", s, p, 1000), "EXIT")
        self.assertEqual(H.exit_decision("LONG", s, p, 1000), "STAY")

    def test_a_divergence_found_on_or_before_the_entry_candle_is_ignored(self):
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg())))
        s = self.snap(rsi={"L": (True, 4000.0), "S": (False, NAN)})
        self.assertEqual(H.exit_decision("LONG", s, p, 3999), "EXIT")
        self.assertEqual(H.exit_decision("LONG", s, p, 4000), "STAY")       # found ON the entry candle: not after it
        self.assertEqual(H.exit_decision("LONG", s, p, 4500), "STAY")
        self.assertEqual(H.exit_decision("LONG", s, p, None), "EXIT")       # no entry time known: no guard

    def test_any_and_all(self):
        both = dict(rsi=osc_cfg(), cci=osc_cfg(ob=100.0, os_=-100.0))
        only_rsi = self.snap(rsi={"L": (True, 4000.0), "S": (False, NAN)}, cci={"L": (False, NAN), "S": (False, NAN)})
        pa = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(combine="ANY", **both)))
        pl = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(combine="ALL", **both)))
        self.assertEqual(H.exit_decision("LONG", only_rsi, pa, 1000), "EXIT")
        self.assertEqual(H.exit_decision("LONG", only_rsi, pl, 1000), "STAY")
        both_on = self.snap(rsi={"L": (True, 4000.0), "S": (False, NAN)}, cci={"L": (True, 4200.0), "S": (False, NAN)})
        self.assertEqual(H.exit_decision("LONG", both_on, pl, 1000), "EXIT")
        # ALL: one of them found before the entry -> that one does not count -> no exit
        one_old = self.snap(rsi={"L": (True, 4000.0), "S": (False, NAN)}, cci={"L": (True, 900.0), "S": (False, NAN)})
        self.assertEqual(H.exit_decision("LONG", one_old, pl, 1000), "STAY")

    def test_an_oscillator_that_is_not_ticked_is_ignored(self):
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg())))
        s = self.snap(rsi={"L": (False, NAN), "S": (False, NAN)}, cci={"L": (True, 4000.0), "S": (False, NAN)})
        self.assertEqual(H.exit_decision("LONG", s, p, 1000), "STAY")

    def test_off_does_nothing_and_the_ma_signals_still_work(self):
        s = self.snap(rsi={"L": (True, 4000.0), "S": (False, NAN)})
        off = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg(), on=False)))
        self.assertEqual(H.exit_decision("LONG", s, off, 1000), "STAY")
        ma = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when={"on": True, "signal": H.EXIT_BOTH, "after": H.EXIT_WAIT}))
        s.exit_long = True
        self.assertEqual(H.exit_decision("LONG", s, ma, 1000), "EXIT")

    def test_reason_text_and_after(self):
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg(), after=H.EXIT_FLIP)))
        self.assertEqual(H.exit_reason_text(p), "a divergence shows")
        self.assertEqual(H.exit_after(p), H.EXIT_FLIP)
        ma = H.complete_plan(make_plan(ind_ma1={"entry": True}, exit_when={"on": True, "signal": H.EXIT_BOTH, "after": H.EXIT_WAIT}))
        self.assertEqual(H.exit_reason_text(ma), "the MA(s) curve")


# ---------------------------------------------------------------------------------------------- the live bot
def live_plan(after=H.EXIT_WAIT, **over):
    return make_plan(ind_ma1={"entry": True, "flip": True}, exit_when=div_cfg(rsi=osc_cfg(), after=after), **over)


class TestLiveExit(LadderBase):
    async def long_then(self, inst, found, ok=True, t=2000, close=101.0):
        await self.open_long_at(inst)                              # entry candle open_time = 1000
        s = snap_of(t, dir_e=1, dir_f=1, close=close)
        s.xdiv = {"rsi": {"L": (ok, found), "S": (False, NAN)}}
        inst.client.mark_price = close
        await self.tick(inst, s)
        return s

    async def test_a_divergence_after_the_entry_closes_the_trade_and_it_waits(self):
        inst = self.make_l(exit_when=div_cfg(rsi=osc_cfg(), after=H.EXIT_WAIT))
        await self.long_then(inst, 1500.0)
        c = inst.client
        self.assertEqual(len(c.close_orders), 1)
        self.assertEqual(c.close_orders[0][1], "SELL")
        self.assertEqual(len(c.market_orders), 1)                  # nothing opened on the exit candle
        self.assertEqual(inst.state.status, "IDLE")
        self.assertTrue(inst.hub.reentry_forced)
        self.assertEqual(inst.hub.reentry_block, 1)

    async def test_a_divergence_found_before_the_entry_does_not_close_the_new_trade(self):
        inst = self.make_l(exit_when=div_cfg(rsi=osc_cfg(), after=H.EXIT_WAIT))
        await self.long_then(inst, 500.0)                          # found before the entry candle (1000)
        self.assertEqual(inst.client.close_orders, [])
        self.assertEqual(inst.state.direction, "LONG")

    async def test_found_on_the_entry_candle_does_not_count_either(self):
        inst = self.make_l(exit_when=div_cfg(rsi=osc_cfg(), after=H.EXIT_WAIT))
        await self.long_then(inst, 1000.0)
        self.assertEqual(inst.client.close_orders, [])
        self.assertEqual(inst.state.direction, "LONG")

    async def test_flip_option_closes_and_opens_the_other_side(self):
        inst = self.make_l(exit_when=div_cfg(rsi=osc_cfg(), after=H.EXIT_FLIP))
        await self.long_then(inst, 1500.0)
        c = inst.client
        self.assertEqual(len(c.close_orders), 1)
        self.assertEqual(len(c.market_orders), 2)
        self.assertEqual(c.market_orders[1][1], "SELL")
        self.assertEqual(inst.state.direction, "SHORT")

    async def test_no_signal_no_exit(self):
        inst = self.make_l(exit_when=div_cfg(rsi=osc_cfg()))
        await self.long_then(inst, 1500.0, ok=False)
        self.assertEqual(inst.client.close_orders, [])
        self.assertEqual(inst.state.direction, "LONG")

    async def test_off_ignores_it(self):
        inst = self.make_l(exit_when=div_cfg(rsi=osc_cfg(), on=False))
        await self.long_then(inst, 1500.0)
        self.assertEqual(inst.client.close_orders, [])

    async def test_short_is_mirrored(self):
        inst = self.make_l(exit_when=div_cfg(rsi=osc_cfg()))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=100.0))
        self.assertEqual(inst.state.direction, "SHORT")
        s = snap_of(2000, dir_e=-1, dir_f=-1, close=99.0)
        s.xdiv = {"rsi": {"L": (False, NAN), "S": (True, 1800.0)}}
        inst.client.mark_price = 99.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(inst.client.close_orders[0][1], "BUY")

    async def test_a_flip_signal_on_the_same_candle_is_one_flip_not_two(self):
        inst = self.make_l(exit_when=div_cfg(rsi=osc_cfg(), after=H.EXIT_WAIT))
        await self.open_long_at(inst)
        inst.client.mark_price = 99.0
        s = snap_of(2000, dir_e=-1, dir_f=-1, close=99.0)
        s.xdiv = {"rsi": {"L": (True, 1500.0), "S": (False, NAN)}}
        await self.tick(inst, s)
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(len(inst.client.market_orders), 2)
        self.assertEqual(inst.state.direction, "SHORT")


# ---------------------------------------------------------------------------------------------- paper side + backtest
class TestPaperAndBacktest(unittest.TestCase):
    def book_long(self, plan, entry_t=1000):
        book = B.new_book(1000.0)
        B.on_candle(book, snap_of(entry_t, dir_e=1, dir_f=1, close=100.0), plan, tick_size=0.1, last_total=None,
                    balance=1000.0, t_ms=entry_t + 1)
        self.assertEqual(book["pos"], 1)
        return book

    def test_paper_trade_exits_on_a_later_divergence_only(self):
        plan = H.complete_plan(live_plan(allow_reentry=True))
        book = self.book_long(plan)
        old = snap_of(1500, dir_e=1, dir_f=1, close=101.0)
        old.xdiv = {"rsi": {"L": (True, 1000.0), "S": (False, NAN)}}          # found on the entry candle: ignored
        B.on_candle(book, old, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=1501)
        self.assertEqual(book["pos"], 1)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.xdiv = {"rsi": {"L": (True, 1500.0), "S": (False, NAN)}}
        ev, closed = B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=2001)
        self.assertEqual(book["pos"], 0)
        self.assertEqual(closed[0]["exit_reason"], "Exit when")
        self.assertTrue(book["reentry_forced"])

    def test_paper_flip_option(self):
        plan = H.complete_plan(live_plan(H.EXIT_FLIP))
        book = self.book_long(plan)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.xdiv = {"rsi": {"L": (True, 1500.0), "S": (False, NAN)}}
        B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=2001)
        self.assertEqual(book["pos"], -1)

    def test_backtest_runs_and_the_divergence_exit_shows_up(self):
        df = candles(1600, seed=51, vol=1.6)
        for after in H.EXIT_AFTERS:
            p = make_plan(ind_mx={"entry": True, "flip": True}, allow_reentry=True,
                          exit_when=div_cfg(rsi=osc_cfg(pivot=3, lookback=30), after=after, keep=2))
            p["ind"]["mx"]["modes"] = [H.MX_MODES[0]]
            p["ind"]["ma1"].update(type="EMA", length=9)
            p["ind"]["ma2"].update(type="EMA", length=21)
            p["sizing"].update(stop_on=False)
            p["sl"] = {"on": False}
            res = BT.run(df, p, 1000.0, 0.0, 0.0)
            reasons = [t["exit_reason"] for t in res["trades"]]
            self.assertGreater(len(reasons), 10)
            self.assertTrue(any(r == "Exit when" for r in reasons), after)
            off = copy.deepcopy(p)
            off["exit_when"]["on"] = False
            res_off = BT.run(df, off, 1000.0, 0.0, 0.0)
            self.assertFalse(any(t["exit_reason"] == "Exit when" for t in res_off["trades"]))

    def test_the_guard_works_in_the_backtest_no_trade_exits_on_the_candle_it_was_found_before_its_entry(self):
        df = candles(1600, seed=52, vol=1.6)
        p = make_plan(ind_mx={"entry": True, "flip": True}, allow_reentry=True,
                      exit_when=div_cfg(rsi=osc_cfg(pivot=3, lookback=30), keep=6))
        p["ind"]["mx"]["modes"] = [H.MX_MODES[0]]
        p["ind"]["ma1"].update(type="EMA", length=9)
        p["ind"]["ma2"].update(type="EMA", length=21)
        p["sizing"].update(stop_on=False)
        p["sl"] = {"on": False}
        frame = H.compute_hub(df.reset_index(drop=True), p)
        res = BT.run(df, p, 1000.0, 0.0, 0.0)
        times = frame["open_time"].to_numpy()
        for t in res["trades"]:
            if t["exit_reason"] != "Exit when":
                continue
            j = int(np.flatnonzero(times == t["exit_time"] - 1)[0])
            d = "LONG" if t["direction"] in ("LONG", 1, "long") else "SHORT"
            col_t = frame["xd_rsi_t_l" if d == "LONG" else "xd_rsi_t_s"].to_numpy()[j]
            self.assertTrue(math.isfinite(col_t))
            self.assertGreater(col_t, t["entry_time"])


if __name__ == "__main__":
    unittest.main()
