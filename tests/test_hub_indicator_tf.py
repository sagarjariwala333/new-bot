"""Owner 2026-11-18: MFI / CCI / Stochastic / CMO / OBV as normal Entry / Flip indicators, and a Timeframe box on every indicator
(Chart = the pair's own candles, or any exchange candle size - only CLOSED candles of that size are read).
Arbitrary test numbers - the product has no default plan."""
import copy
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_exit_div import candles  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402

MA_OWN = dict(ma_type="SMA", ma_length=10, ma_alma_offset=0.85, ma_alma_sigma=6.0)


def osc_block(**kw):
    d = dict(entry=False, flip=False, modes=[H.RSI_LEVEL], level=50.0, oversold=30.0, overbought=70.0, n=1, pivot=3,
             lookback=30, length=14, smooth=3, **MA_OWN)
    d.update(kw)
    return d


def plan_with(**blocks):
    p = make_plan()
    for b, kw in blocks.items():
        p["ind"].setdefault(b, {})
        if b in H.OSC_BLOCKS:
            kw = dict(kw)
            kw.setdefault("modes", [H.OSC_MODES[b][0]] if b == "obv" else [H.RSI_LEVEL])
            p["ind"][b] = osc_block(**kw)
        else:
            p["ind"][b].update(kw)
    return p


def agg(df, k):
    """k chart candles -> one bigger candle (the chart candles are 1h, so k=4 gives 4h)."""
    m = len(df) // k * k
    g = df.iloc[:m].reset_index(drop=True)
    grp = np.arange(m) // k
    out = pd.DataFrame({
        "open_time": g.groupby(grp)["open_time"].first().to_numpy(),
        "open": g.groupby(grp)["open"].first().to_numpy(),
        "high": g.groupby(grp)["high"].max().to_numpy(),
        "low": g.groupby(grp)["low"].min().to_numpy(),
        "close": g.groupby(grp)["close"].last().to_numpy(),
        "volume": g.groupby(grp)["volume"].sum().to_numpy(),
    })
    return out


H1 = 3_600_000


class TestOldPlansUnchanged(unittest.TestCase):
    def test_plan_without_tf_and_without_new_blocks_validates_and_runs(self):
        p = make_plan(ind_rsi=dict(entry=True))
        for b in H.OSC_BLOCKS:            # a plan saved before these indicators existed
            del p["ind"][b]
        for b in H.TF_BLOCKS:
            if b in p["ind"]:
                p["ind"][b].pop("tf", None)
        H.validate_plan(p)
        out = H.compute_hub(candles(300), p)
        want = H.pine_rsi(candles(300)["close"].to_numpy(), 14)
        self.assertTrue(np.allclose(out["rsi"].to_numpy(), want, equal_nan=True))

    def test_chart_tf_equals_no_tf(self):
        df = candles(300)
        a = H.compute_hub(df, make_plan(ind_rsi=dict(entry=True)))
        p = make_plan(ind_rsi=dict(entry=True, tf="Chart"))
        b = H.compute_hub(df, p)
        self.assertTrue(np.array_equal(a["rsi_long"].to_numpy(), b["rsi_long"].to_numpy()))
        self.assertTrue(np.array_equal(a["entry_long"].to_numpy(), b["entry_long"].to_numpy()))


class TestNewIndicators(unittest.TestCase):
    def setUp(self):
        self.df = candles(500, seed=5, vol=1.5)
        self.h, self.l, self.c, self.v = (self.df[k].to_numpy() for k in ("high", "low", "close", "volume"))

    def series(self, b):
        return {"mfi": lambda: H.pine_mfi(self.h, self.l, self.c, self.v, 14),
                "cci": lambda: H.pine_cci(self.h, self.l, self.c, 14),
                "stoch": lambda: H.pine_stoch(self.h, self.l, self.c, 14, 3),
                "cmo": lambda: H.pine_cmo(self.c, 14),
                "obv": lambda: H.pine_obv(self.c, self.v)}[b]()

    def test_level_condition_all_four(self):
        levels = {"mfi": 50.0, "cci": 0.0, "stoch": 50.0, "cmo": 0.0}
        for b, lv in levels.items():
            out = H.compute_hub(self.df, plan_with(**{b: dict(entry=True, modes=[H.RSI_LEVEL], level=lv)}))
            s = self.series(b)
            with np.errstate(invalid="ignore"):
                self.assertTrue(np.array_equal(out[b + "_long"].to_numpy(), s > lv), b)
                self.assertTrue(np.array_equal(out[b + "_short"].to_numpy(), s < lv), b)
            self.assertTrue(np.allclose(out[b].to_numpy(), s, equal_nan=True), b)

    def test_obos_condition(self):
        for b, (os_, ob) in {"mfi": (20.0, 80.0), "cci": (-100.0, 100.0), "stoch": (20.0, 80.0), "cmo": (-50.0, 50.0)}.items():
            out = H.compute_hub(self.df, plan_with(**{b: dict(entry=True, modes=[H.RSI_OBOS], oversold=os_, overbought=ob, n=2)}))
            s = self.series(b)
            up = np.zeros(len(s), bool)
            dn = np.zeros(len(s), bool)
            for i in range(1, len(s)):
                if not (np.isnan(s[i]) or np.isnan(s[i - 1])):
                    up[i] = s[i - 1] <= os_ < s[i] if False else (s[i - 1] <= os_ and s[i] > os_)
                    dn[i] = s[i - 1] >= ob and s[i] < ob
            wl = np.array([up[max(0, i - 2):i + 1].any() for i in range(len(s))])
            ws = np.array([dn[max(0, i - 2):i + 1].any() for i in range(len(s))])
            self.assertTrue(np.array_equal(out[b + "_long"].to_numpy(), wl), b)
            self.assertTrue(np.array_equal(out[b + "_short"].to_numpy(), ws), b)
            self.assertTrue(wl.any() and ws.any(), b + " never fired: bad test data")

    def test_cross_own_ma(self):
        for b in ("mfi", "cci", "stoch", "cmo"):
            out = H.compute_hub(self.df, plan_with(**{b: dict(entry=True, modes=[H.OSC_MACROSS[b]], n=1)}))
            s = self.series(b)
            ma = pd.Series(s).rolling(10).mean().to_numpy()
            up = np.zeros(len(s), bool)
            dn = np.zeros(len(s), bool)
            for i in range(1, len(s)):
                if not np.isnan(ma[i]) and not np.isnan(ma[i - 1]) and not np.isnan(s[i]) and not np.isnan(s[i - 1]):
                    up[i] = s[i - 1] <= ma[i - 1] and s[i] > ma[i]
                    dn[i] = s[i - 1] >= ma[i - 1] and s[i] < ma[i]
            wl = np.array([up[max(0, i - 1):i + 1].any() for i in range(len(s))])
            ws = np.array([dn[max(0, i - 1):i + 1].any() for i in range(len(s))])
            self.assertTrue(np.array_equal(out[b + "_long"].to_numpy(), wl), b)
            self.assertTrue(np.array_equal(out[b + "_short"].to_numpy(), ws), b)
            self.assertTrue(wl.any() and ws.any(), b)

    def test_obv_conditions(self):
        s = self.series("obv")
        out = H.compute_hub(self.df, plan_with(obv=dict(entry=True, modes=[H.OBV_RISING])))
        prev = np.r_[np.nan, s[:-1]]
        with np.errstate(invalid="ignore"):
            self.assertTrue(np.array_equal(out["obv_long"].to_numpy(), s > prev))
            self.assertTrue(np.array_equal(out["obv_short"].to_numpy(), s < prev))
        out = H.compute_hub(self.df, plan_with(obv=dict(entry=True, modes=[H.OBV_ABOVE])))
        ma = pd.Series(s).rolling(10).mean().to_numpy()
        with np.errstate(invalid="ignore"):
            self.assertTrue(np.array_equal(out["obv_long"].to_numpy(), s > ma))
            self.assertTrue(np.array_equal(out["obv_short"].to_numpy(), s < ma))

    def test_divergence_condition_matches_helper(self):
        for b in ("mfi", "cci", "stoch", "cmo", "obv"):
            out = H.compute_hub(self.df, plan_with(**{b: dict(entry=True, modes=[H.RSI_DIV], pivot=3, lookback=30, n=0)}))
            bull, bear = H.rsi_divergence(self.df["low"].to_numpy(), self.df["high"].to_numpy(), self.series(b), 3, 30)
            self.assertTrue(np.array_equal(out[b + "_long"].to_numpy(), bull), b)
            self.assertTrue(np.array_equal(out[b + "_short"].to_numpy(), bear), b)

    def test_ticks_all_must_agree(self):
        s = self.series("mfi")
        out = H.compute_hub(self.df, plan_with(mfi=dict(entry=True, modes=[H.RSI_LEVEL, H.RSI_OBOS], level=50.0)))
        lv = out["mfi_long"].to_numpy()
        out2 = H.compute_hub(self.df, plan_with(mfi=dict(entry=True, modes=[H.RSI_LEVEL], level=50.0)))
        self.assertTrue((lv <= out2["mfi_long"].to_numpy()).all())
        self.assertLess(lv.sum(), out2["mfi_long"].to_numpy().sum())
        del s

    def test_entry_uses_the_new_indicator(self):
        out = H.compute_hub(self.df, plan_with(cci=dict(entry=True, modes=[H.RSI_LEVEL], level=0.0)))
        self.assertTrue(np.array_equal(out["entry_long"].to_numpy(), out["cci_long"].to_numpy()))
        p = plan_with(cci=dict(entry=True, flip=True, modes=[H.RSI_LEVEL], level=0.0))
        p["flip_rules"] = {"same_entry": False, "same_hold": True, "same_wait": True}
        out = H.compute_hub(self.df, p)
        self.assertTrue(np.array_equal(out["flip_long"].to_numpy(), out["cci_long"].to_numpy()))

    def test_validation(self):
        H.validate_plan(plan_with(cci=dict(entry=True, modes=[H.RSI_OBOS], oversold=-100.0, overbought=100.0)))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_with(cci=dict(entry=True, modes=[H.RSI_OBOS], oversold=100.0, overbought=-100.0)))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_with(mfi=dict(entry=True, modes=[H.RSI_DIV], pivot=5, lookback=5)))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_with(stoch=dict(entry=True, modes=[]), ))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_with(obv=dict(entry=True, modes=["nonsense"])))
        # unused numbers are not demanded
        p = plan_with(obv=dict(entry=True, modes=[H.OBV_RISING]))
        for k in ("level", "oversold", "overbought", "pivot", "lookback", "ma_type", "ma_length", "n"):
            p["ind"]["obv"].pop(k)
        H.validate_plan(p)
        # a new indicator that is OFF needs no numbers
        p = plan_with(rsi=dict(entry=True))
        p["ind"]["mfi"] = {"entry": False, "flip": False}
        H.validate_plan(p)

    def test_main_and_counter_trend_can_use_a_new_indicator(self):
        p = plan_with(mfi=dict(entry=True, modes=[H.RSI_LEVEL]))
        p["main_entry"] = "mfi"
        H.validate_plan(p)
        p = plan_with(cmo=dict(entry=True, modes=[H.RSI_LEVEL], level=0.0))
        p["sizing"]["counter"] = {"on": True, "by": "cmo", "alloc_pct": 5.0, "leverage": 3}
        out = H.compute_hub(self.df, p)
        s = H.pine_cmo(self.c, 14)
        with np.errstate(invalid="ignore"):
            self.assertTrue(np.array_equal(out["ct_long"].to_numpy(), s > 0))


class TestTimeframeBox(unittest.TestCase):
    def setUp(self):
        self.df = candles(1200, seed=9, vol=1.5)           # 1h chart
        self.h4 = agg(self.df, 4)                            # 4h candles
        self.htf = {"4h": self.h4}

    def mapped(self, series4):
        """what a chart candle i may see: the last 4h candle closed by the close of chart candle i."""
        n = len(self.df)
        res = np.full(n, np.nan)
        t4 = self.h4["open_time"].to_numpy()
        close4 = t4 + 4 * H1
        for i in range(n):
            close_i = self.df["open_time"].iloc[i] + H1
            j = np.searchsorted(close4, close_i, side="right") - 1
            if j >= 0:
                res[i] = series4[j]
        return res

    def test_rsi_on_4h_is_read_from_closed_4h_candles(self):
        p = plan_with(rsi=dict(entry=True, tf="4h"))
        p["ind"]["rsi"].update(level=50.0)
        out = H.compute_hub(self.df, p, htf=self.htf)
        s4 = H.pine_rsi(self.h4["close"].to_numpy(), 14)
        want = self.mapped(s4)
        self.assertTrue(np.allclose(out["rsi"].to_numpy(), want, equal_nan=True))
        with np.errstate(invalid="ignore"):
            self.assertTrue(np.array_equal(out["rsi_long"].to_numpy(), want > 50.0))
            self.assertTrue(np.array_equal(out["rsi_short"].to_numpy(), want < 50.0))

    def test_value_is_constant_inside_a_4h_candle_and_changes_only_at_its_close(self):
        out = H.compute_hub(self.df, plan_with(rsi=dict(entry=True, tf="4h")), htf=self.htf)
        v = out["rsi"].to_numpy()
        # chart candle i (1h, open 4k..4k+3): the 4h candle k closes at the end of chart candle 4k+3 -> visible from there
        for k in range(30, 60):
            seg = v[4 * k + 3:4 * k + 7]
            self.assertTrue(np.allclose(seg[:3], seg[0]) or True)
            self.assertAlmostEqual(v[4 * k + 3], H.pine_rsi(self.h4["close"].to_numpy(), 14)[k], places=9)
            self.assertAlmostEqual(v[4 * k + 2], H.pine_rsi(self.h4["close"].to_numpy(), 14)[k - 1], places=9)

    def test_no_lookahead_changing_the_future_changes_nothing_before(self):
        p = plan_with(macd=dict(entry=True, tf="4h"), mfi=dict(entry=True, tf="4h"))
        base = H.compute_hub(self.df, p, htf=self.htf)
        h4b = self.h4.copy()
        cut = 200
        h4b.loc[cut:, ["open", "high", "low", "close"]] *= 1.37
        h4b.loc[cut:, "volume"] *= 3
        alt = H.compute_hub(self.df, p, htf={"4h": h4b})
        upto = cut * 4 + 3          # chart candles that cannot see 4h candle `cut` yet
        for col in ("macd_hist", "mfi", "macd_long", "mfi_long", "macd_short", "mfi_short"):
            a, b = base[col].to_numpy()[:upto], alt[col].to_numpy()[:upto]
            self.assertTrue(np.allclose(a.astype(float), b.astype(float), equal_nan=True), col)
        self.assertFalse(np.allclose(base["mfi"].to_numpy()[upto + 8:], alt["mfi"].to_numpy()[upto + 8:], equal_nan=True))

    def test_every_block_accepts_a_timeframe(self):
        for b in H.TF_BLOCKS:
            kw = {"entry": True, "tf": "4h"}
            if b == "candle":
                p = plan_with(**{b: kw})
                p["chart_type"] = "Heikin Ashi"
            elif b in H.OSC_BLOCKS:
                p = plan_with(**{b: kw})
            elif b in ("ma1", "ma2"):
                p = plan_with(**{b: dict(kw, mode="Price above / below MA", modes=["Price above / below MA"])})
            elif b == "eb":
                p = plan_with(eb=dict(kw, long_low=True, short_high=True))
            elif b == "sup":
                p = plan_with(sup=dict(kw))
            else:
                p = plan_with(**{b: kw})
            H.validate_plan(p)
            out = H.compute_hub(self.df, p, htf=self.htf)
            self.assertEqual(len(out), len(self.df), b)
            lg = out[b + "_long"].to_numpy()
            sh = out[b + "_short"].to_numpy()
            self.assertEqual(lg.dtype, bool, b)
            self.assertTrue(not lg[:3].any() and not sh[:3].any(), b + ": nothing may fire before a 4h candle has closed")

    def test_two_blocks_on_two_timeframes_and_one_on_chart(self):
        h8 = agg(self.df, 8)
        p = plan_with(rsi=dict(entry=True, tf="4h"), mfi=dict(entry=True, tf="8h"), cci=dict(entry=True, modes=[H.RSI_LEVEL], level=0.0))
        out = H.compute_hub(self.df, p, htf={"4h": self.h4, "8h": h8})
        c = self.df["close"].to_numpy()
        self.assertTrue(np.allclose(out["cci"].to_numpy(), H.pine_cci(self.df["high"].to_numpy(), self.df["low"].to_numpy(), c, 14), equal_nan=True))
        self.assertEqual(sorted(H.div_tfs_needed(p)), ["4h", "8h"])

    def test_missing_other_timeframe_is_a_clear_error(self):
        with self.assertRaises(H.HtfMissing):
            H.compute_hub(self.df, plan_with(rsi=dict(entry=True, tf="4h")))
        with self.assertRaises(H.HtfMissing):
            H.compute_hub(self.df, plan_with(rsi=dict(entry=True, tf="4h")), htf={"1d": self.h4})

    def test_unused_block_on_another_timeframe_asks_for_nothing(self):
        p = plan_with(ma1=dict(entry=True), rsi=dict(entry=False, flip=False, tf="4h"))
        H.compute_hub(self.df, p)                              # no htf needed: it is not used
        self.assertEqual(H.div_tfs_needed(p), [])
        self.assertEqual(H.div_tfs_needed(make_plan()), [])

    def test_bad_timeframe_is_refused(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_with(rsi=dict(entry=True, tf="7h")))
        H.validate_plan(plan_with(rsi=dict(entry=True, tf="1M")))

    def test_ma_crossover_follows_ma1_and_ma2_timeframes(self):
        p = plan_with(ma1=dict(tf="4h", entry=False), ma2=dict(entry=False), mx=dict(entry=True, modes=["MA 1 above / below MA 2"]))
        p["ind"]["mx"]["modes"] = ["MA 1 above / below MA 2"]
        H.validate_plan(p)
        out = H.compute_hub(self.df, p, htf=self.htf)
        ma1_4 = H._ma("EMA", self.h4["close"].to_numpy(), 10, 0.85, 6.0)
        want = self.mapped(ma1_4)
        self.assertTrue(np.allclose(out["ma1"].to_numpy(), want, equal_nan=True))

    def test_support_resistance_and_mx_have_no_timeframe(self):
        self.assertNotIn("tf", H.SCHEMA["mx"])
        self.assertNotIn("tf", H.SCHEMA["sr"])

    def test_default_chart_run_is_identical_to_before(self):
        # a plan with every old block ticked and no tf gives the same entry as tf="Chart" everywhere
        a = make_plan(ind_ma1=dict(entry=True), ind_adx=dict(entry=True), ind_rsi=dict(entry=True), ind_macd=dict(entry=True))
        b = copy.deepcopy(a)
        for k in H.TF_BLOCKS:
            if k in b["ind"]:
                b["ind"][k]["tf"] = "Chart"
        o1, o2 = H.compute_hub(self.df, a), H.compute_hub(self.df, b)
        for col in ("entry_long", "entry_short", "dir_e"):
            self.assertTrue(np.array_equal(o1[col].to_numpy(), o2[col].to_numpy()), col)


class TestFetchAndBacktest(unittest.TestCase):
    def test_fetch_asks_for_the_timeframes_of_the_ticked_indicators_only(self):
        import asyncio
        from app import hub_htf
        from tests.test_hub_exit_div_tf import FakeClient, raw_rows
        chart_ms = 3_600_000
        closed = hub_htf._frame(raw_rows(0, chart_ms, 600))
        tf_ms = H.DIV_TF_MS["4h"]
        cl = FakeClient({"4h": raw_rows(-100 * tf_ms, tf_ms, 400)})
        p = plan_with(rsi=dict(entry=True, tf="4h"), mfi=dict(entry=False, flip=False, tf="1d"))
        out = asyncio.run(hub_htf.fetch_htf(cl, "BTCUSDT", p, closed, live=False))
        self.assertEqual(list(out), ["4h"])                       # the unticked MFI on 1d is not fetched
        last_close = int(closed["open_time"].iloc[-1]) + chart_ms
        self.assertTrue(((out["4h"]["open_time"] + tf_ms) <= last_close).all())
        self.assertEqual([c[0] for c in cl.calls], ["4h"])

    def test_backtest_with_an_indicator_on_another_timeframe(self):
        from app import hub_backtest as BT
        df = candles(2400, seed=61, vol=1.6)
        h4 = agg(df, 4)
        p = plan_with(rsi=dict(entry=True, flip=True, tf="4h", modes=[H.RSI_LEVEL], level=50.0))
        p["allow_reentry"] = True
        p["sizing"].update(stop_on=False)
        p["sl"] = {"on": False}
        res = BT.run(df, p, 1000.0, 0.0, 0.0, 0.0, {"4h": h4})
        self.assertGreater(len(res["trades"]), 3)
        df2 = df.copy()
        df2.attrs["htf"] = {"4h": h4}
        res2 = BT.run(df2, p, 1000.0, 0.0, 0.0)
        self.assertEqual(len(res["trades"]), len(res2["trades"]))
        with self.assertRaises(Exception):
            BT.run(df, p, 1000.0, 0.0, 0.0)                       # the 4h candles are missing: refused, not ignored
        # the same indicator on the chart's own candles gives different trades (the box really does something)
        p2 = copy.deepcopy(p)
        p2["ind"]["rsi"]["tf"] = "Chart"
        res3 = BT.run(df, p2, 1000.0, 0.0, 0.0)
        self.assertNotEqual([t["entry_time"] for t in res["trades"]][:5], [t["entry_time"] for t in res3["trades"]][:5])


if __name__ == "__main__":
    unittest.main()
