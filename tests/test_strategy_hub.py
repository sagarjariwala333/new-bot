import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app import strategy as strat  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from tests._hub_plan import base_plan, make_plan  # noqa: E402


def make_df(n=400, seed=7):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0004, 0.012, n)
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[100.0], close[:-1]]) * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    vol = rng.uniform(100, 1000, n)
    return pd.DataFrame({"open_time": np.arange(n) * 3600000, "open": open_, "high": high, "low": low,
                         "close": close, "volume": vol})


class TestPlanValidation(unittest.TestCase):
    def test_valid_plan_with_an_entry_indicator(self):
        p = make_plan(ind_ma1=dict(entry=True))
        self.assertIs(H.validate_plan(p), p)

    def test_no_entry_indicator_is_refused(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan())

    def test_no_default_plan_none_is_refused(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(None)
        with self.assertRaises(H.PlanError):
            H.validate_plan({})

    def test_every_top_level_key_is_required(self):
        # flip_action is not asked any more (2026-10-25: a flip always opens the new side)
        for key in ["chart_type", "combine_entry", "combine_flip",
                    "entry_wait", "wait_first_reversal", "ind", "hold", "sizing", "tp", "trail"]:
            p = make_plan(ind_ma1=dict(entry=True))
            del p[key]
            with self.assertRaises(H.PlanError, msg=key):
                H.validate_plan(p)

    def test_settings_of_a_ticked_indicator_are_required(self):
        for b, params in H.SCHEMA.items():
            for key in params:
                p = make_plan(ind_ma1=dict(entry=True))
                p["ind"][b]["flip"] = True if b != "ma1" else p["ind"][b]["flip"]
                p["ind"][b]["entry"] = True
                if b in ("ma1", "ma2"):
                    p["ind"][b].update(type="ALMA", mode="Price crossed MA within N candles")
                if b == "mx":
                    p["ind"][b].update(mode="MA 1 crossed MA 2 within N candles")
                if key == "modes":
                    if b in ("rsi", "macd"):
                        continue   # 2026-10-25: a plan without a Condition keeps the old one (see upgrade_plan)
                    p["ind"][b].pop("mode", None)
                    p["ind"][b]["modes"] = None   # deleted below: no condition at all must be refused
                if (b, key) in H.OPTIONAL_KEYS:
                    continue   # 2026-10-25 option ticks: left out = OFF (an older plan)
                if b == "rsi":   # each RSI number is needed only by its own Condition
                    use = (H.RSI_MACROSS if key.startswith("ma_") else H.RSI_DIV if key in ("pivot", "lookback")
                           else H.RSI_OBOS if key in ("oversold", "overbought", "n") else H.RSI_LEVEL)
                    p["ind"][b].update(mode=use, oversold=30.0, overbought=70.0, n=1, pivot=5, lookback=60,
                                       ma_type="ALMA", ma_length=9, ma_alma_offset=0.85, ma_alma_sigma=6.0)   # 2026-11-01
                if b == "macd":
                    p["ind"][b].update(mode=H.MACD_HIST)
                del p["ind"][b][key]
                with self.assertRaises(H.PlanError, msg=f"{b}.{key}"):
                    H.validate_plan(p)

    def test_alma_numbers_and_n_only_when_used(self):
        p = make_plan(ind_ma1=dict(entry=True, type="EMA", mode="Price above / below MA"))
        del p["ind"]["ma1"]["alma_offset"], p["ind"]["ma1"]["alma_sigma"], p["ind"]["ma1"]["n"]
        H.validate_plan(p)
        p["ind"]["ma1"]["type"] = "ALMA"
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p["ind"]["ma1"].update(type="EMA", mode="Price crossed MA within N candles")
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_the_two_switches_of_every_block_are_always_required(self):
        for b in H.BLOCKS:
            for key in ("entry", "flip"):
                p = make_plan(ind_ma1=dict(entry=True))
                del p["ind"][b][key]
                with self.assertRaises(H.PlanError, msg=f"{b}.{key}"):
                    H.validate_plan(p)

    def test_an_indicator_that_is_off_needs_no_numbers(self):
        p = make_plan(ind_ma1=dict(entry=True))
        for b in H.BLOCKS:
            if b != "ma1":
                p["ind"][b] = {"entry": False, "flip": False}
        H.validate_plan(p)

    def test_mx_needs_both_mas_and_hold_needs_adx(self):
        p = make_plan(ind_mx=dict(entry=True))
        p["ind"]["ma1"] = {"entry": False, "flip": False}
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)                        # MA 1 has no numbers but MA1 vs MA2 uses it
        p = make_plan(ind_ma1=dict(entry=True), hold={"on": True, "adx_level": 25.0})
        p["ind"]["adx"] = {"entry": False, "flip": False}
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)                        # the hold rule needs the ADX settings

    def test_unused_parts_need_no_numbers(self):
        p = make_plan(ind_ma1=dict(entry=True), chart_type="Candles")
        del p["renko_atr_length"]
        p["hold"] = {"on": False}
        p["sizing"] = {"alloc_pct": 10.0, "leverage": 5, "stop_on": False}
        p["tp"] = {"on": False}
        p["trail"] = {"on": False}
        H.validate_plan(p)
        # and each becomes required when switched on
        for fix, key in ((lambda q: q.update(chart_type="Renko"), "renko"),
                         (lambda q: q["hold"].update(on=True), "hold"),
                         (lambda q: q["sizing"].update(stop_on=True), "stop"),
                         (lambda q: q["tp"].update(on=True), "tp"),
                         (lambda q: q["trail"].update(on=True), "trail")):
            import copy as _c
            q = _c.deepcopy(p)
            fix(q)
            with self.assertRaises(H.PlanError, msg=key):
                H.validate_plan(q)

    def test_tp_on_needs_a_ticked_level_and_unticked_levels_need_no_numbers(self):
        p = make_plan(ind_ma1=dict(entry=True))
        p["tp"] = {"on": True, "atr_length": 14, "levels": [{"on": True, "mult": 1, "pct": 50}, {"on": False}, {"on": False}]}
        H.validate_plan(p)
        p["tp"]["levels"][0]["on"] = False
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_complete_plan_fills_only_unused_settings_and_changes_no_signal(self):
        df = make_df()
        full = make_plan(ind_ma1=dict(entry=True, flip=True))
        slim = make_plan(ind_ma1=dict(entry=True, flip=True))
        for b in H.BLOCKS:
            if b != "ma1":
                slim["ind"][b] = {"entry": False, "flip": False}
        del slim["renko_atr_length"]
        slim["tp"] = {"on": False}
        slim["trail"] = {"on": False}
        slim["hold"] = {"on": False}
        slim["sizing"] = {"alloc_pct": 10.0, "leverage": 5, "stop_on": False}
        done = H.complete_plan(slim)
        self.assertEqual(done["ind"]["ma1"], H.upgrade_plan(slim)["ind"]["ma1"])   # used settings untouched
        self.assertNotIn("length", slim["ind"]["rsi"])                   # the saved plan itself is not changed
        a = H.compute_hub(df, full)
        b = H.compute_hub(df, slim)
        self.assertTrue((a["dir_e"].values == b["dir_e"].values).all())
        self.assertTrue((a["dir_f"].values == b["dir_f"].values).all())
        # a different inert value for an unused setting changes nothing either
        other = H.complete_plan(slim)
        other["ind"]["rsi"]["length"] = 3
        other["ind"]["macd"]["fast"], other["ind"]["macd"]["slow"] = 2, 9
        c = H.compute_hub(df, other)
        self.assertTrue((a["dir_e"].values == c["dir_e"].values).all())

    def test_bad_values(self):
        bad = [
            dict(chart_type="Bars"), dict(combine_entry="BOTH"), dict(entry_wait=-1), dict(entry_wait=1.5),
            dict(flip_action="x"), dict(sizing={"alloc_pct": 101}), dict(sizing={"alloc_pct": 0}),
            dict(sizing={"leverage": 126}), dict(sizing={"stop_pct": 0}), dict(sizing={"stop_pct": 101}),
            dict(trail={"on": True, "length": 0}), dict(hold={"on": True, "adx_level": -1}),
        ]
        for over in bad:
            p = make_plan(ind_ma1=dict(entry=True), **over)
            with self.assertRaises(H.PlanError, msg=str(over)):
                H.validate_plan(p)

    def test_macd_slow_must_exceed_fast(self):
        p = make_plan(ind_ma1=dict(entry=True), ind_macd=dict(entry=True, fast=26, slow=12))
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_tp_percentages(self):
        p = make_plan(ind_ma1=dict(entry=True))
        p["tp"]["on"] = True
        p["tp"]["levels"] = [{"on": True, "mult": 1, "pct": 60}, {"on": True, "mult": 2, "pct": 60},
                             {"on": False, "mult": 3, "pct": 34}]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p["tp"]["levels"][1]["on"] = False          # only TP1 ticked now -> 60 is fine
        H.validate_plan(p)
        p["tp"]["levels"] = p["tp"]["levels"][:2]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_bool_is_not_a_number(self):
        p = make_plan(ind_ma1=dict(entry=True, length=True))
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)


class TestIndicators(unittest.TestCase):
    def setUp(self):
        self.df = make_df()

    def test_heikin_ashi_matches_existing_smoothed_candles(self):
        ha = strat.smoothed_candles(self.df)
        o, h, l, c = H.heikin_ashi(self.df["open"], self.df["high"], self.df["low"], self.df["close"])
        np.testing.assert_allclose(o, ha["sc_open"].values)
        np.testing.assert_allclose(c, ha["sc_close"].values)
        np.testing.assert_allclose(h, ha["sc_high"].values)
        np.testing.assert_allclose(l, ha["sc_low"].values)

    def test_alma_naive_reference(self):
        s = self.df["close"].values
        length, off, sig = 9, 0.85, 6.0
        got = H.pine_alma(s, length, off, sig)
        m = off * (length - 1)
        sd = length / sig
        for i in (length - 1, 50, 200, 399):
            norm = 0.0
            tot = 0.0
            for j in range(length):                       # Pine: series[length - j - 1] * weight(j)
                w = math.exp(-((j - m) ** 2) / (2 * sd * sd))
                norm += w
                tot += s[i - (length - j - 1)] * w
            self.assertAlmostEqual(got[i], tot / norm, places=9)
        self.assertTrue(np.isnan(got[length - 2]))

    def test_rsi_naive_reference(self):
        s = self.df["close"].values
        n = 14
        got = H.pine_rsi(s, n)
        up = [max(s[i] - s[i - 1], 0.0) for i in range(1, len(s))]
        dn = [max(s[i - 1] - s[i], 0.0) for i in range(1, len(s))]
        ru = sum(up[:n]) / n
        rd = sum(dn[:n]) / n
        ref = {n: 100 - 100 / (1 + ru / rd)}
        for k in range(n, len(up)):
            ru = (ru * (n - 1) + up[k]) / n
            rd = (rd * (n - 1) + dn[k]) / n
            ref[k + 1] = 100 - 100 / (1 + ru / rd)
        for i in (n, n + 1, 100, 399):
            self.assertAlmostEqual(got[i], ref[i], places=7)
        self.assertTrue(np.isnan(got[n - 1]))

    def test_rsi_all_up_is_100(self):
        s = np.arange(1.0, 60.0)
        self.assertAlmostEqual(H.pine_rsi(s, 14)[-1], 100.0)

    def test_macd_naive_reference(self):
        s = self.df["close"].values
        line, sig, hist = H.pine_macd(s, 12, 26, 9)
        e12 = strat.pine_ema(s, 12)
        e26 = strat.pine_ema(s, 26)
        np.testing.assert_allclose(line[40:], (e12 - e26)[40:])
        np.testing.assert_allclose(hist[60:], (line - sig)[60:])

    def test_cross_within(self):
        a = np.array([1, 1, 3, 3, 3, 3, 1, 1.0])
        b = np.full(8, 2.0)
        up = H._cross_up(a, b)
        self.assertEqual(list(up), [False, False, True, False, False, False, False, False])
        w1 = H._within(up, 1)
        self.assertEqual(list(w1), [False, False, True, True, False, False, False, False])
        w0 = H._within(up, 0)
        self.assertEqual(list(w0), list(up))
        dn = H._cross_down(a, b)
        self.assertEqual(list(dn), [False, False, False, False, False, False, True, False])

    def test_highest_lowest(self):
        a = np.array([1, 5, 3, 2, 8, 1.0])
        np.testing.assert_array_equal(H.rolling_highest(a, 3)[2:], [5, 5, 8, 8])
        np.testing.assert_array_equal(H.rolling_lowest(a, 3)[2:], [1, 2, 2, 1])
        self.assertTrue(np.isnan(H.rolling_highest(a, 3)[1]))

    def test_renko_makes_bricks_on_a_strong_trend_and_none_when_flat(self):
        n = 120
        c = 100 + np.arange(n) * 1.0
        o, h, l, rc = H.renko_candles(c - 0.5, c + 0.5, c - 0.5, c, 5)
        self.assertTrue(np.isfinite(rc[-1]))
        self.assertGreater(rc[-1], rc[20])                    # bricks climbed
        self.assertTrue(np.all((rc[30:] >= o[30:])))          # up bricks only in a straight uptrend
        flat = np.full(60, 100.0)
        o2, h2, l2, c2 = H.renko_candles(flat, flat + 0.01, flat - 0.01, flat, 5)
        self.assertTrue(np.all(np.isnan(c2) | (c2 == 100.0)))


class TestCompute(unittest.TestCase):
    def setUp(self):
        self.df = make_df()

    def test_candle_colour_entry(self):
        p = make_plan(ind_candle=dict(entry=True))
        f = H.compute_hub(self.df, p)
        up = f["c_close"].values > f["c_open"].values       # the colour of the CHART candle (Heikin Ashi)
        down = f["c_close"].values < f["c_open"].values
        np.testing.assert_array_equal(f["dir_e"].values == 1, up)
        np.testing.assert_array_equal(f["dir_e"].values == -1, down)
        self.assertTrue((f["dir_f"].values == 0).all())       # nothing ticked for flip

    def test_ha_chart_type_changes_the_colour(self):
        ph = make_plan(ind_candle=dict(entry=True, flip=True), chart_type="Heikin Ashi")
        fh = H.compute_hub(self.df, ph)
        normal_up = (self.df["close"] > self.df["open"]).values
        self.assertFalse((fh["candle_long"].values == normal_up).all())   # HA colour is not the normal colour
        ha_c = (self.df["open"] + self.df["high"] + self.df["low"] + self.df["close"]) / 4
        np.testing.assert_allclose(fh["c_close"].values, ha_c.values)

    def test_candle_colour_refused_on_normal_candles(self):
        for sw in ("entry", "flip"):
            p = make_plan(ind_candle={sw: True}, chart_type="Candles")
            with self.assertRaises(H.PlanError):
                H.validate_plan(p)
        for ct in ("Heikin Ashi", "Renko"):
            H.validate_plan(make_plan(ind_candle=dict(entry=True), chart_type=ct))
        H.validate_plan(make_plan(ind_rsi=dict(entry=True), chart_type="Candles"))      # OFF on normal candles is fine

    def test_orders_side_data_stay_on_normal_candles(self):
        """Stop / take-profit ATR / trailing extremes and ADX, CI, volume read the NORMAL candles even on HA."""
        pn = make_plan(ind_adx=dict(entry=True), ind_ci=dict(entry=True))
        ph = make_plan(ind_adx=dict(entry=True), ind_ci=dict(entry=True), chart_type="Heikin Ashi",
                       ind_candle=dict(entry=True))
        a, b = H.compute_hub(self.df, pn), H.compute_hub(self.df, ph)
        for col in ("tp_atr", "trail_low", "trail_high", "adx", "ci"):
            np.testing.assert_array_equal(a[col].values, b[col].values, col)

    def test_price_above_ema_state(self):
        p = make_plan(ind_ma1=dict(entry=True, flip=True, type="EMA", length=20, mode="Price above / below MA"))
        f = H.compute_hub(self.df, p)
        ema = strat.pine_ema(self.df["close"].values, 20)
        c = self.df["close"].values
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["dir_e"].values == 1, c > ema)
            np.testing.assert_array_equal(f["dir_e"].values == -1, c < ema)
        np.testing.assert_array_equal(f["dir_e"].values, f["dir_f"].values)

    def test_ma_rising(self):
        p = make_plan(ind_ma1=dict(entry=True, type="HMA", length=12, mode="MA rising / falling"))
        f = H.compute_hub(self.df, p)
        hma = strat.pine_hma(self.df["close"].values, 12)
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["dir_e"].values == 1, hma > np.concatenate([[np.nan], hma[:-1]]))

    def test_price_cross_within_n(self):
        p = make_plan(ind_ma1=dict(entry=True, type="SMA", length=20, mode="Price crossed MA within N candles", n=2))
        f = H.compute_hub(self.df, p)
        c = self.df["close"].values
        sma = strat.pine_sma(c, 20)
        up = H._cross_up(c, sma)
        ref = np.array([up[max(0, i - 2):i + 1].any() for i in range(len(c))])
        np.testing.assert_array_equal(f["ma1_long"].values, ref)

    def test_two_ma_cross_block(self):
        p = make_plan(ind_ma1=dict(type="EMA", length=9), ind_ma2=dict(type="EMA", length=21),
                      ind_mx=dict(entry=True, flip=True, mode="MA 1 above / below MA 2"))
        f = H.compute_hub(self.df, p)
        e9, e21 = strat.pine_ema(self.df["close"].values, 9), strat.pine_ema(self.df["close"].values, 21)
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["dir_e"].values == 1, e9 > e21)
            np.testing.assert_array_equal(f["dir_e"].values == -1, e9 < e21)

    def test_all_vs_any(self):
        both = dict(entry=True)
        p_all = make_plan(ind_candle=both, ind_ma1=dict(entry=True, type="EMA", length=20), combine_entry="ALL")
        p_any = make_plan(ind_candle=both, ind_ma1=dict(entry=True, type="EMA", length=20), combine_entry="ANY")
        fa, fy = H.compute_hub(self.df, p_all), H.compute_hub(self.df, p_any)
        la = fa["candle_long"].values & fa["ma1_long"].values
        np.testing.assert_array_equal(fa["entry_long"].values, la)
        ly = fy["candle_long"].values | fy["ma1_long"].values
        np.testing.assert_array_equal(fy["entry_long"].values, ly)
        # ANY with a long on one block and a short on the other = both true = no signal
        conflict = fy["entry_long"].values & fy["entry_short"].values
        self.assertTrue(conflict.any())
        self.assertTrue((fy["dir_e"].values[conflict] == 0).all())

    def test_entry_and_flip_use_their_own_switches(self):
        p = make_plan(ind_candle=dict(entry=True, flip=False), ind_ma1=dict(entry=False, flip=True, length=20))
        f = H.compute_hub(self.df, p)
        np.testing.assert_array_equal(f["dir_e"].values == 1, f["candle_long"].values)
        np.testing.assert_array_equal(f["dir_f"].values == 1, f["ma1_long"].values)

    def test_each_filter_block_runs(self):
        for b in ("adx", "ci", "rsi", "macd", "vol", "atr", "st"):
            p = make_plan(ind_candle=dict(entry=True), **{"ind_" + b: dict(entry=True)})
            f = H.compute_hub(self.df, p)
            self.assertEqual(len(f), len(self.df), b)
            self.assertIn(f"{b}_long", f.columns)
            # a filter can only remove signals compared with the candle alone
            base = H.compute_hub(self.df, make_plan(ind_candle=dict(entry=True)))
            self.assertTrue((f["entry_long"].values <= base["entry_long"].values).all(), b)
            self.assertTrue((f["entry_short"].values <= base["entry_short"].values).all(), b)

    def test_rsi_macd_volume_structure_values(self):
        p = make_plan(ind_rsi=dict(entry=True, level=50.0),
                      ind_vol=dict(entry=True, length=5, mult=1.0), ind_st=dict(entry=True, length=5))
        f = H.compute_hub(self.df, p)
        rsi = H.pine_rsi(self.df["close"].values, 14)
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["rsi_long"].values, rsi > 50)
        vol = self.df["volume"].values
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["vol_long"].values, vol >= strat.pine_sma(vol, 5))
        hh = np.concatenate([[np.nan], H.rolling_highest(self.df["high"].values, 5)[:-1]])
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["st_long"].values, self.df["close"].values > hh)

    def test_adx_block(self):
        p = make_plan(ind_adx=dict(entry=True, level=20.0, di_required=True), ind_candle=dict(entry=False))
        f = H.compute_hub(self.df, p)
        plus, minus, adx = strat.pine_dmi(self.df, 14, 14)
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["adx_long"].values, (adx >= 20) & (plus > minus))
            np.testing.assert_array_equal(f["adx_short"].values, (adx >= 20) & (minus > plus))
        p2 = make_plan(ind_adx=dict(entry=True, level=20.0, di_required=False))
        f2 = H.compute_hub(self.df, p2)
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f2["adx_long"].values, adx >= 20)

    def test_ci_block(self):
        p = make_plan(ind_ci=dict(entry=True, length=14, max=55.0))
        f = H.compute_hub(self.df, p)
        h, l, c = self.df["high"].values, self.df["low"].values, self.df["close"].values
        i = 200
        tr = [max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1])) for k in range(i - 13, i + 1)]
        ref = 100 * math.log10(sum(tr) / (h[i - 13:i + 1].max() - l[i - 13:i + 1].min())) / math.log10(14)
        self.assertAlmostEqual(f["ci"].iloc[i], ref, places=9)

    def test_tp_atr_and_trail_columns(self):
        p = make_plan(ind_candle=dict(entry=True))
        p["tp"]["atr_length"] = 10
        p["trail"]["length"] = 7
        f = H.compute_hub(self.df, p)
        h, l, c = self.df["high"].values, self.df["low"].values, self.df["close"].values
        tr = np.array([np.nan] + [max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1])) for k in range(1, len(c))])
        np.testing.assert_allclose(f["tp_atr"].values, strat.pine_rma(tr, 10), equal_nan=True)
        self.assertAlmostEqual(f["trail_low"].iloc[100], l[94:101].min())
        self.assertAlmostEqual(f["trail_high"].iloc[100], h[94:101].max())

    def test_no_lookahead_last_candle_result_does_not_depend_on_future(self):
        p = make_plan(ind_candle=dict(entry=True, flip=True), ind_ma1=dict(entry=True, flip=True, type="HMA", length=12,
                                                                           mode="MA rising / falling"),
                      ind_rsi=dict(entry=True), ind_macd=dict(entry=True), chart_type="Heikin Ashi")
        full = H.compute_hub(self.df, p)
        for cut in (150, 250, 399):
            part = H.compute_hub(self.df.iloc[:cut].copy(), p)
            for col in ("dir_e", "dir_f", "adx", "ma1"):
                a, b = full[col].iloc[cut - 1], part[col].iloc[cut - 1]
                if isinstance(a, float) and math.isnan(a):
                    self.assertTrue(math.isnan(b), col)
                else:
                    self.assertAlmostEqual(float(a), float(b), places=8, msg=f"{col}@{cut}")

    def test_snapshot(self):
        p = make_plan(ind_candle=dict(entry=True, flip=True))
        f = H.compute_hub(self.df, p)
        s = H.last_hub_snapshot(f)
        self.assertEqual(s.open_time, int(self.df["open_time"].iloc[-1]))
        self.assertEqual(s.dir_e, int(f["dir_e"].iloc[-1]))
        self.assertIn(s.to_display_dict()["entry_signal"], ("LONG", "SHORT", "NONE"))


class TestDecisions(unittest.TestCase):
    def snap(self, **kw):
        d = dict(open_time=1, open=100, high=101, low=99, close=100, dir_e=0, dir_f=0, adx=30.0, tp_atr=2.0,
                 trail_low=95.0, trail_high=105.0, c_close=100, ma1=100, ma2=100)
        d.update(kw)
        return H.HubSnapshot(**d)

    def test_flip_decision(self):
        p = make_plan(ind_candle=dict(entry=True))
        self.assertEqual(H.flip_decision("LONG", self.snap(dir_f=-1), 100, p), "FLIP")
        self.assertEqual(H.flip_decision("LONG", self.snap(dir_f=1), 100, p), "STAY")
        self.assertEqual(H.flip_decision("LONG", self.snap(dir_f=0), 100, p), "STAY")
        self.assertEqual(H.flip_decision("SHORT", self.snap(dir_f=1), 100, p), "FLIP")
        self.assertEqual(H.flip_decision("SHORT", self.snap(dir_f=-1), 100, p), "STAY")

    def test_hold_rule(self):
        p = make_plan(ind_candle=dict(entry=True), hold={"on": True, "adx_level": 25.0})
        # long in profit, ADX below level -> hold
        self.assertEqual(H.flip_decision("LONG", self.snap(dir_f=-1, close=110, adx=20.0), 100, p), "HOLD")
        # ADX at/above level -> flip
        self.assertEqual(H.flip_decision("LONG", self.snap(dir_f=-1, close=110, adx=25.0), 100, p), "FLIP")
        # in loss -> flip
        self.assertEqual(H.flip_decision("LONG", self.snap(dir_f=-1, close=90, adx=20.0), 100, p), "FLIP")
        # short in profit
        self.assertEqual(H.flip_decision("SHORT", self.snap(dir_f=1, close=90, adx=20.0), 100, p), "HOLD")
        # NaN ADX -> not held
        self.assertEqual(H.flip_decision("LONG", self.snap(dir_f=-1, close=110, adx=float("nan")), 100, p), "FLIP")
        p["hold"]["on"] = False
        self.assertEqual(H.flip_decision("LONG", self.snap(dir_f=-1, close=110, adx=20.0), 100, p), "FLIP")

    def test_stop_price(self):
        self.assertAlmostEqual(H.stop_price_from_loss("LONG", 100.0, 1000.0, 10.0, 6.0), 100 - 6.0)
        self.assertAlmostEqual(H.stop_price_from_loss("SHORT", 100.0, 1000.0, 10.0, 6.0), 106.0)
        with self.assertRaises(ValueError):
            H.stop_price_from_loss("LONG", 100.0, 1000.0, 0.0, 6.0)

    def test_tp_prices(self):
        p = make_plan(ind_candle=dict(entry=True))
        p["tp"]["on"] = True
        p["tp"]["levels"] = [{"on": True, "mult": 1, "pct": 33}, {"on": True, "mult": 3, "pct": 33},
                             {"on": False, "mult": 5, "pct": 34}]
        lv = H.tp_prices("LONG", 100.0, 2.0, p)
        self.assertEqual([x["price"] for x in lv], [102.0, 106.0, 110.0])
        self.assertEqual([x["on"] for x in lv], [True, True, False])
        sv = H.tp_prices("SHORT", 100.0, 2.0, p)
        self.assertEqual([x["price"] for x in sv], [98.0, 94.0, 90.0])
        p["tp"]["on"] = False
        self.assertEqual([x["on"] for x in H.tp_prices("LONG", 100.0, 2.0, p)], [False, False, False])

    def test_trailing_stop_only_tightens(self):
        s = self.snap(trail_low=96.0, trail_high=104.0)
        self.assertEqual(H.trail_stop("LONG", 94.0, s), 96.0)     # tightens up
        self.assertEqual(H.trail_stop("LONG", 98.0, s), 98.0)     # never loosens
        self.assertEqual(H.trail_stop("SHORT", 106.0, s), 104.0)  # tightens down
        self.assertEqual(H.trail_stop("SHORT", 102.0, s), 102.0)  # never loosens
        self.assertEqual(H.trail_stop("LONG", None, s), 96.0)     # no max-loss stop: starts at the extreme
        self.assertEqual(H.trail_stop("LONG", 94.0, self.snap(trail_low=float("nan"))), 94.0)


class TestWaitMachine(unittest.TestCase):
    def run_seq(self, entry_wait, sigs, reverse=True):
        """Simulates a real position + the wait machine over a list of per-candle signals (dir_e == dir_f)."""
        ws = H.WaitState()
        pos = 0
        log = []
        for i, s in enumerate(sigs):
            flipped_to = 0
            if pos != 0 and s == -pos:
                flipped_to = s
                pos = 0
                log.append((i, "CLOSE"))
            go = H.wait_step(ws, flipped_to=flipped_to, reverse=reverse, flat=(pos == 0), dir_e=s, dir_f=s,
                             first_entry_ok=True, entry_wait=entry_wait)
            if go != 0 and pos == 0:
                pos = go
                log.append((i, "OPEN", go))
        return log

    S = [0, 1, 1, 1, -1, -1, -1, 1, 0, -1, -1]

    def test_wait_0_immediate(self):
        self.assertEqual(self.run_seq(0, self.S),
                         [(1, "OPEN", 1), (4, "CLOSE"), (4, "OPEN", -1), (7, "CLOSE"), (7, "OPEN", 1),
                          (9, "CLOSE"), (9, "OPEN", -1)])

    def test_wait_1(self):
        self.assertEqual(self.run_seq(1, self.S),
                         [(2, "OPEN", 1), (4, "CLOSE"), (5, "OPEN", -1), (7, "CLOSE"), (10, "OPEN", -1)])

    def test_wait_2(self):
        self.assertEqual(self.run_seq(2, self.S),
                         [(3, "OPEN", 1), (4, "CLOSE"), (6, "OPEN", -1), (7, "CLOSE")])

    def test_close_only_goes_flat_then_fresh_entry(self):
        log = self.run_seq(0, [1, 1, -1, -1, -1], reverse=False)
        # close only: closes on the flip candle but the flat bot may then enter on the same candle by the entry rule
        self.assertEqual(log[0], (0, "OPEN", 1))
        self.assertEqual(log[1], (2, "CLOSE"))

    def test_first_entry_not_ok_blocks_fresh_entries_only(self):
        ws = H.WaitState()
        self.assertEqual(H.wait_step(ws, flipped_to=0, reverse=True, flat=True, dir_e=1, dir_f=1,
                                     first_entry_ok=False, entry_wait=0), 0)
        # a flip always opens (flip entries are not subject to the first-reversal rule)
        self.assertEqual(H.wait_step(ws, flipped_to=-1, reverse=True, flat=True, dir_e=0, dir_f=-1,
                                     first_entry_ok=False, entry_wait=0), -1)

    def test_flip_pending_cancelled_if_flip_signal_breaks(self):
        ws = H.WaitState()
        self.assertEqual(H.wait_step(ws, flipped_to=-1, reverse=True, flat=True, dir_e=-1, dir_f=-1,
                                     first_entry_ok=True, entry_wait=2), 0)
        self.assertEqual(H.wait_step(ws, flipped_to=0, reverse=True, flat=True, dir_e=0, dir_f=0,
                                     first_entry_ok=True, entry_wait=2), 0)
        self.assertEqual(ws.pend_dir, 0)

    def test_pending_cancelled_when_not_flat(self):
        ws = H.WaitState()
        H.wait_step(ws, flipped_to=0, reverse=True, flat=True, dir_e=1, dir_f=0, first_entry_ok=True, entry_wait=3)
        self.assertEqual(ws.pend_dir, 1)
        H.wait_step(ws, flipped_to=0, reverse=True, flat=False, dir_e=1, dir_f=0, first_entry_ok=True, entry_wait=3)
        self.assertEqual(ws.pend_dir, 0)


if __name__ == "__main__":
    unittest.main()
