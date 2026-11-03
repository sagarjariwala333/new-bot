"""RSI crosses its own MA (owner 2026-11-01: "Add in rsi crossing it's own moving average (sma, ema, wma, hma)",
then "Yes include alma .. let's go", "add in pine script").

Long = the RSI line crosses UP through its own moving average, short = it crosses DOWN through it. The average is
taken of the RSI line. Arbitrary test numbers only, NOT the owner's settings.

The expected values are worked out here with SEPARATE code (pandas / plain loops written in this file), not with the
bot's own helpers, so a mistake in the bot's helper cannot hide itself."""
import copy
import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app import strategy_hub as H  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_strategy_hub import make_df  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CROSS = H.RSI_MACROSS


def rsi_plan(**rsi):
    base = dict(entry=True, modes=[CROSS], length=14, ma_type="SMA", ma_length=9, n=0)
    base.update(rsi)
    return make_plan(ind_rsi=base)


# ---------------------------------------------------------------- independent oracles (this file only)
def o_sma(x, L):
    return pd.Series(x).rolling(L, min_periods=L).mean().to_numpy()


def o_wma(x, L):
    out = np.full(len(x), np.nan)
    w = np.arange(1, L + 1, dtype=float)
    for i in range(L - 1, len(x)):
        win = x[i - L + 1:i + 1]
        if not np.isnan(win).any():
            out[i] = (win * w).sum() / w.sum()
    return out


def o_ema(x, L):
    out = np.full(len(x), np.nan)
    a = 2.0 / (L + 1)
    prev = None
    for i in range(len(x)):
        if prev is None:
            if i >= L - 1 and not np.isnan(x[i - L + 1:i + 1]).any():
                prev = x[i - L + 1:i + 1].mean()
                out[i] = prev
        else:
            prev = a * x[i] + (1 - a) * prev
            out[i] = prev
    return out


def o_hma(x, L):
    raw = 2.0 * o_wma(x, L // 2) - o_wma(x, L)
    return o_wma(raw, int(math.floor(math.sqrt(L))))


def o_alma(x, L, off, sig):
    out = np.full(len(x), np.nan)
    m = off * (L - 1)
    s = L / sig
    w = np.array([math.exp(-((i - m) ** 2) / (2 * s * s)) for i in range(L)])
    for i in range(L - 1, len(x)):
        win = x[i - L + 1:i + 1]
        if not np.isnan(win).any():
            out[i] = (win * w).sum() / w.sum()
    return out


def o_rsi(close, L):
    """RSI from scratch (Wilder smoothing, seeded with the plain average of the first L changes)."""
    ch = np.diff(close, prepend=np.nan)
    up = np.where(np.isnan(ch), np.nan, np.maximum(ch, 0))
    dn = np.where(np.isnan(ch), np.nan, np.maximum(-ch, 0))

    def rma(v):
        out = np.full(len(v), np.nan)
        prev = None
        for i in range(len(v)):
            if prev is None:
                if i >= L - 1 and not np.isnan(v[i - L + 1:i + 1]).any():
                    prev = v[i - L + 1:i + 1].mean()
                    out[i] = prev
            else:
                prev = (v[i] + (L - 1) * prev) / L
                out[i] = prev
        return out
    ru, rd = rma(up), rma(dn)
    out = np.full(len(close), np.nan)
    for i in range(len(close)):
        if np.isnan(ru[i]) or np.isnan(rd[i]):
            continue
        out[i] = 100.0 if rd[i] == 0 else 0.0 if ru[i] == 0 else 100.0 - 100.0 / (1.0 + ru[i] / rd[i])
    return out


def o_cross(a, b):
    up = np.zeros(len(a), dtype=bool)
    dn = np.zeros(len(a), dtype=bool)
    for i in range(1, len(a)):
        if any(np.isnan(v) for v in (a[i], b[i], a[i - 1], b[i - 1])):
            continue
        up[i] = a[i] > b[i] and a[i - 1] <= b[i - 1]
        dn[i] = a[i] < b[i] and a[i - 1] >= b[i - 1]
    return up, dn


def o_within(cond, n):
    out = np.zeros(len(cond), dtype=bool)
    since = 10 ** 9
    for i, v in enumerate(cond):
        since = 0 if v else since + 1
        out[i] = since <= n
    return out


class TestPlanCheck(unittest.TestCase):
    def test_the_condition_is_in_the_list(self):
        self.assertEqual(H.RSI_MODES[-1], "RSI crosses its own MA")
        self.assertEqual(len(H.RSI_MODES), 4)

    def test_every_type_is_accepted(self):
        for t in ("SMA", "EMA", "WMA", "HMA"):
            with self.subTest(t=t):
                H.validate_plan(rsi_plan(ma_type=t))
        H.validate_plan(rsi_plan(ma_type="ALMA", ma_alma_offset=0.85, ma_alma_sigma=6.0))

    def test_alma_needs_its_two_numbers_other_types_do_not(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(rsi_plan(ma_type="ALMA"))
        with self.assertRaises(H.PlanError):
            H.validate_plan(rsi_plan(ma_type="ALMA", ma_alma_offset=0.85))
        with self.assertRaises(H.PlanError):
            H.validate_plan(rsi_plan(ma_type="ALMA", ma_alma_sigma=6.0))
        with self.assertRaises(H.PlanError):
            H.validate_plan(rsi_plan(ma_type="ALMA", ma_alma_offset=0.85, ma_alma_sigma=0.0))
        H.validate_plan(rsi_plan(ma_type="EMA"))        # no ALMA numbers needed

    def test_type_length_and_n_are_needed(self):
        for key in ("ma_type", "ma_length", "n", "length"):
            p = rsi_plan()
            p["ind"]["rsi"].pop(key)
            with self.subTest(missing=key), self.assertRaises(H.PlanError):
                H.validate_plan(p)

    def test_wrong_values_are_refused(self):
        for bad in ({"ma_length": 0}, {"ma_length": 2.5}, {"ma_length": True}, {"ma_type": "DEMA"},
                    {"ma_type": ""}, {"n": -1}):
            with self.subTest(bad=bad), self.assertRaises(H.PlanError):
                H.validate_plan(rsi_plan(**bad))

    def test_other_conditions_numbers_are_not_needed(self):
        p = rsi_plan()
        for k in ("level", "oversold", "overbought", "pivot", "lookback"):
            p["ind"]["rsi"].pop(k, None)
        H.validate_plan(p)

    def test_the_new_numbers_are_not_needed_when_it_is_not_ticked(self):
        p = make_plan(ind_rsi=dict(entry=True, modes=[H.RSI_LEVEL], length=14, level=50.0))
        H.validate_plan(p)          # an older plan stays valid, with no RSI average numbers
        up = H.upgrade_plan(p)
        self.assertNotIn("ma_type", up["ind"]["rsi"])

    def test_ticked_together_with_the_others_needs_all_their_numbers(self):
        p = rsi_plan(modes=[H.RSI_LEVEL, CROSS], level=50.0)
        H.validate_plan(p)
        p["ind"]["rsi"].pop("level")
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_a_blank_unticked_rsi_block_needs_nothing(self):
        # RSI not used by the plan at all (MA 1 is): no RSI number is asked for
        H.validate_plan(make_plan(ind_ma1=dict(entry=True)))

    def test_counter_trend_by_rsi_asks_for_the_numbers_of_the_ticked_condition(self):
        p = make_plan(ind_ma1=dict(entry=True), sizing=dict(counter=dict(on=True, by="rsi", alloc_pct=5.0, leverage=2)))
        p["ind"]["rsi"].update(modes=[CROSS], length=14, ma_type="EMA", ma_length=9, n=0)
        H.validate_plan(p)
        p["ind"]["rsi"].pop("ma_length")
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)


class TestMaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = make_df(700, seed=21)
        cls.rsi = o_rsi(cls.df["close"].to_numpy(), 14)

    def check(self, ma_type, ma, n=0, **extra):
        f = H.compute_hub(self.df, rsi_plan(ma_type=ma_type, n=n, **extra))
        np.testing.assert_allclose(f["rsi"].values, self.rsi, rtol=0, atol=1e-9, equal_nan=True)
        np.testing.assert_allclose(f["rsi_ma"].values, ma, rtol=0, atol=1e-9, equal_nan=True)
        up, dn = o_cross(self.rsi, ma)
        np.testing.assert_array_equal(f["rsi_long"].values, o_within(up, n))
        np.testing.assert_array_equal(f["rsi_short"].values, o_within(dn, n))
        self.assertTrue(up.sum() >= 5 and dn.sum() >= 5, "the data must really cross")
        return f

    def test_sma(self):
        self.check("SMA", o_sma(self.rsi, 9))

    def test_ema(self):
        self.check("EMA", o_ema(self.rsi, 9))

    def test_wma(self):
        self.check("WMA", o_wma(self.rsi, 9))

    def test_hma(self):
        self.check("HMA", o_hma(self.rsi, 9))

    def test_alma(self):
        self.check("ALMA", o_alma(self.rsi, 9, 0.85, 6.0), ma_alma_offset=0.85, ma_alma_sigma=6.0)

    def test_other_lengths_and_alma_numbers_change_the_result(self):
        self.check("EMA", o_ema(self.rsi, 21), ma_length=21)
        self.check("ALMA", o_alma(self.rsi, 12, 0.5, 3.0), ma_length=12, ma_alma_offset=0.5, ma_alma_sigma=3.0)

    def test_long_up_short_down_never_both(self):
        f = H.compute_hub(self.df, rsi_plan(ma_type="EMA"))
        self.assertFalse((f["rsi_long"].values & f["rsi_short"].values).any())

    def test_a_cross_is_a_cross_not_a_state(self):
        f = H.compute_hub(self.df, rsi_plan(ma_type="SMA"))
        # a cross happens on a few candles only, far from "RSI is above its MA" (about half of all candles)
        above = np.nan_to_num(f["rsi"].values) > np.nan_to_num(f["rsi_ma"].values)
        self.assertLess(f["rsi_long"].sum(), above.sum() / 2)
        self.assertTrue(f["rsi_long"].sum() > 0)

    def test_no_signal_while_the_average_is_not_ready(self):
        f = H.compute_hub(self.df, rsi_plan(ma_type="WMA", ma_length=30))
        first_ok = int(np.flatnonzero(~np.isnan(f["rsi_ma"].values))[0])
        self.assertFalse(f["rsi_long"].values[:first_ok + 1].any() or f["rsi_short"].values[:first_ok + 1].any())
        self.assertEqual(first_ok, 14 + 30 - 1)       # RSI needs 14 changes (first value on index 14), then 30 RSI values - 1

    def test_n_keeps_it_on(self):
        a = H.compute_hub(self.df, rsi_plan(n=0))
        b = H.compute_hub(self.df, rsi_plan(n=3))
        np.testing.assert_array_equal(b["rsi_long"].values, H._within(a["rsi_long"].values, 3))
        np.testing.assert_array_equal(b["rsi_short"].values, H._within(a["rsi_short"].values, 3))
        self.assertGreater(b["rsi_long"].sum(), a["rsi_long"].sum())

    def test_ticked_with_another_condition_both_must_agree(self):
        both = H.compute_hub(self.df, rsi_plan(modes=[H.RSI_LEVEL, CROSS], level=50.0))
        only_x = H.compute_hub(self.df, rsi_plan())
        only_l = H.compute_hub(self.df, make_plan(ind_rsi=dict(entry=True, modes=[H.RSI_LEVEL], length=14, level=50.0)))
        np.testing.assert_array_equal(both["rsi_long"].values, only_x["rsi_long"].values & only_l["rsi_long"].values)
        np.testing.assert_array_equal(both["rsi_short"].values, only_x["rsi_short"].values & only_l["rsi_short"].values)
        self.assertTrue(only_x["rsi_long"].sum() > both["rsi_long"].sum() > 0)

    def test_the_other_conditions_do_not_change(self):
        # same result for the old conditions whether or not the new numbers sit in the plan
        a = make_plan(ind_rsi=dict(entry=True, modes=[H.RSI_OBOS], length=14, oversold=35.0, overbought=65.0, n=1))
        b = copy.deepcopy(a)
        b["ind"]["rsi"].update(ma_type="HMA", ma_length=7)
        fa, fb = H.compute_hub(self.df, a), H.compute_hub(self.df, b)
        for col in ("rsi", "rsi_long", "rsi_short", "entry_long", "entry_short"):
            np.testing.assert_array_equal(fa[col].values, fb[col].values)
        self.assertNotIn("rsi_ma", fa.columns)

    def test_alma_numbers_are_ignored_for_other_types(self):
        a = H.compute_hub(self.df, rsi_plan(ma_type="EMA"))
        b = H.compute_hub(self.df, rsi_plan(ma_type="EMA", ma_alma_offset=0.1, ma_alma_sigma=2.0))
        np.testing.assert_array_equal(a["rsi_long"].values, b["rsi_long"].values)

    def test_it_is_the_entry_signal_of_the_hub(self):
        f = H.compute_hub(self.df, rsi_plan(ma_type="EMA"))
        np.testing.assert_array_equal(f["entry_long"].values, f["rsi_long"].values)
        np.testing.assert_array_equal(f["entry_short"].values, f["rsi_short"].values)

    def test_works_for_a_flip_with_its_own_rules(self):
        p = make_plan(ind_ma1=dict(entry=True), ind_rsi=dict(flip=True, modes=[CROSS], length=14, ma_type="WMA",
                                                             ma_length=9, n=0),
                      flip_rules={"same_entry": False, "same_hold": True, "same_wait": True})
        f = H.compute_hub(self.df, p)
        np.testing.assert_array_equal(f["flip_long"].values, f["rsi_long"].values)
        self.assertTrue(f["flip_long"].sum() > 0)

    def test_heikin_ashi_chart_uses_its_close(self):
        p = rsi_plan(ma_type="SMA")
        p["chart_type"] = "Heikin Ashi"
        f = H.compute_hub(self.df, p)
        ha_rsi = o_rsi(f["c_close"].to_numpy(), 14)
        up, dn = o_cross(ha_rsi, o_sma(ha_rsi, 9))
        np.testing.assert_array_equal(f["rsi_long"].values, up)
        np.testing.assert_array_equal(f["rsi_short"].values, dn)


class TestCompletePlan(unittest.TestCase):
    def test_complete_plan_keeps_the_owners_values(self):
        p = rsi_plan(ma_type="ALMA", ma_length=11, ma_alma_offset=0.7, ma_alma_sigma=4.0)
        c = H.complete_plan(p)["ind"]["rsi"]
        self.assertEqual((c["ma_type"], c["ma_length"], c["ma_alma_offset"], c["ma_alma_sigma"]),
                         ("ALMA", 11, 0.7, 4.0))

    def test_an_older_plan_still_loads_and_gets_only_inert_values(self):
        p = make_plan(ind_rsi=dict(entry=True, modes=[H.RSI_LEVEL], length=14, level=50.0))
        c = H.complete_plan(p)["ind"]["rsi"]
        self.assertEqual(c["modes"], [H.RSI_LEVEL])
        self.assertIn("ma_type", c)       # inert, never used while the condition is not ticked
        f = H.compute_hub(make_df(300, seed=3), p)
        self.assertNotIn("rsi_ma", f.columns)


class TestDashboardAndPine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "app", "static", "dashboard.html"), encoding="utf-8") as fh:
            cls.html = fh.read()
        with open(os.path.join(ROOT, "pine", "STRATEGY_HUB_BLANK.pine"), encoding="utf-8") as fh:
            cls.pine = fh.read()

    def test_dashboard_list_is_the_bots_list(self):
        m = re.search(r"const HUB_RSI_MODES = \[(.*?)\];", self.html)
        self.assertIsNotNone(m)
        shown = re.findall(r"'([^']*)'", m.group(1))
        self.assertEqual(tuple(shown), H.RSI_MODES)

    def test_dashboard_type_list_is_the_bots_list(self):
        m = re.search(r"const HUB_MA_TYPES = \[(.*?)\];", self.html)
        self.assertEqual(tuple(re.findall(r"'([^']*)'", m.group(1))), H.MA_TYPES)

    def test_dashboard_boxes_use_the_plan_keys(self):
        for k in ("ma_type", "ma_length", "ma_alma_offset", "ma_alma_sigma"):
            self.assertIn(f"{{ k: '{k}', l: 'RSI average:", self.html)
            self.assertIn(f"rsi_{k}", self.html)             # the help key
        for k in ("ma_type", "ma_length", "ma_alma_offset", "ma_alma_sigma"):
            self.assertIn(k, H.SCHEMA["rsi"])

    def test_dashboard_has_no_number_filled_in_for_the_owner(self):
        for k in ("ma_type", "ma_length", "ma_alma_offset", "ma_alma_sigma"):
            m = re.search(r"\{ k: '%s', l: 'RSI average:[^\n]*" % k, self.html)
            self.assertIsNotNone(m)
            self.assertNotIn("dflt", m.group(0))

    def test_pine_has_the_condition(self):
        for needle in ('rsMaOn   = input.bool(false, "RSI crosses its own MA"',
                       'rsMaType = input.string("SMA", "RSI average: Type", options = ["SMA", "EMA", "WMA", "HMA", "ALMA"]',
                       'rsMaLen  = input.int(1, "RSI average: Length", minval = 1',
                       'rsMaOff  = input.float(0.0, "RSI average: ALMA offset"',
                       'rsMaSig  = input.float(0.0001, "RSI average: ALMA sigma", minval = 0.0001',
                       "rsiMa = maOf(rsMaType, rsi, rsMaLen, rsMaOff, rsMaSig)",
                       "rsMaU = within(ta.crossover(rsi, rsiMa), rsN)",
                       "rsMaD = within(ta.crossunder(rsi, rsiMa), rsN)",
                       "rsAny = rsLvlOn or rsObOn or rsDivOn or rsMaOn",
                       "(not rsMaOn or rsMaU)", "(not rsMaOn or rsMaD)"):
            self.assertIn(needle, self.pine)

    def test_pine_types_are_the_bots_types(self):
        m = re.search(r'rsMaType = input\.string\("SMA", "RSI average: Type", options = \[(.*?)\]', self.pine)
        self.assertEqual(tuple(re.findall(r'"([^"]*)"', m.group(1))), H.MA_TYPES)

    def test_pine_ma_helper_has_all_five(self):
        m = re.search(r"maOf\(string t.*?\n\n", self.pine, re.S)
        for t in ("SMA", "EMA", "WMA", "HMA"):
            self.assertIn(f'"{t}" => ta.{t.lower()}(src, len)', m.group(0))
        self.assertIn("=> ta.alma(src, len, off, sig)", m.group(0))

    def test_pine_new_lines_come_after_the_rsi_is_made_and_before_it_is_used(self):
        i_rsi = self.pine.index("rsi = ta.rsi(cC, rsLen)")
        i_ma = self.pine.index("rsiMa = maOf(")
        i_use = self.pine.index("rsL = rsAny")
        i_fn = self.pine.index("maOf(string t")
        self.assertTrue(i_fn < i_rsi < i_ma < i_use)


if __name__ == "__main__":
    unittest.main()
