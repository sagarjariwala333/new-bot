"""Owner 2026-11-17: Exit when -> Divergence now has 7 oscillators (RSI, MFI, CCI, MACD histogram, Stochastic, OBV, CMO), each with
its own on/off, its own numbers and its own timeframe (the chart's own, or any candle size - higher for swing, lower for scalping).
Arbitrary test numbers - the product has no default plan."""
import asyncio
import copy
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_exit_div import candles, osc_cfg, div_cfg, NAN  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import hub_htf  # noqa: E402
from app import hub_backtest as BT  # noqa: E402


def all_off():
    return {k: {"on": False} for k in H.DIV_INDS}


def cfg(**blocks):
    c = div_cfg()
    c["div"].update(all_off())
    c["div"].update(blocks)
    return c


# ---------------------------------------------------------------------------------------------- the new oscillators' math
class TestNewOscillators(unittest.TestCase):
    def setUp(self):
        df = candles(400, seed=11, vol=1.5)
        self.h, self.l, self.c, self.v = (df[k].to_numpy() for k in ("high", "low", "close", "volume"))

    def test_stochastic_matches_the_definition(self):
        got = H.pine_stoch(self.h, self.l, self.c, 14, 3)
        raw = np.full(len(self.c), np.nan)
        for i in range(13, len(self.c)):
            hh, ll = self.h[i - 13:i + 1].max(), self.l[i - 13:i + 1].min()
            raw[i] = 100.0 * (self.c[i] - ll) / (hh - ll)
        want = np.full(len(self.c), np.nan)
        for i in range(15, len(self.c)):
            want[i] = raw[i - 2:i + 1].mean()
        ok = ~np.isnan(want)
        self.assertTrue(np.allclose(got[ok], want[ok], atol=1e-9))
        self.assertTrue(np.isnan(got[:15]).all())
        self.assertTrue(((got[ok] >= 0) & (got[ok] <= 100)).all())
        raw_only = H.pine_stoch(self.h, self.l, self.c, 14, 1)
        self.assertTrue(np.allclose(raw_only[13:], raw[13:], atol=1e-9))

    def test_stochastic_flat_window_is_nan_not_an_error(self):
        flat = np.full(30, 100.0)
        self.assertTrue(np.isnan(H.pine_stoch(flat, flat, flat, 14, 3)).all())

    def test_obv_matches_the_definition(self):
        got = H.pine_obv(self.c, self.v)
        want = np.zeros(len(self.c))
        for i in range(1, len(self.c)):
            want[i] = want[i - 1] + (np.sign(self.c[i] - self.c[i - 1]) * self.v[i])
        self.assertTrue(np.allclose(got, want))
        self.assertEqual(got[0], 0.0)

    def test_cmo_matches_the_definition(self):
        got = H.pine_cmo(self.c, 9)
        want = np.full(len(self.c), np.nan)
        for i in range(9, len(self.c)):
            ch = np.diff(self.c[i - 9:i + 1])
            su, sd = ch[ch > 0].sum(), -ch[ch < 0].sum()
            want[i] = 100.0 * (su - sd) / (su + sd)
        ok = ~np.isnan(want)
        self.assertTrue(np.allclose(got[ok], want[ok], atol=1e-9))
        self.assertTrue(np.isnan(got[:9]).all())
        self.assertTrue(((got[ok] >= -100) & (got[ok] <= 100)).all())

    def test_cmo_all_up_is_plus_100(self):
        c = np.arange(30, dtype=float) + 10
        self.assertAlmostEqual(H.pine_cmo(c, 9)[-1], 100.0)

    def test_macd_histogram_is_the_macd_blocks_histogram(self):
        got = H.div_oscillator("macd", {"fast": 12, "slow": 26, "signal": 9}, self.h, self.l, self.c, self.v)
        line = H.pine_ema(self.c, 12) - H.pine_ema(self.c, 26)
        want = line - H.pine_ema(line, 9)
        ok = ~np.isnan(want)
        self.assertTrue(np.allclose(got[ok], want[ok]))

    def test_div_oscillator_picks_the_right_one(self):
        for name, d in (("rsi", {"length": 14}), ("mfi", {"length": 14}), ("cci", {"length": 20}), ("stoch", {"length": 14, "smooth": 3}),
                        ("obv", {}), ("cmo", {"length": 9}), ("macd", {"fast": 12, "slow": 26, "signal": 9})):
            v = H.div_oscillator(name, d, self.h, self.l, self.c, self.v)
            self.assertEqual(len(v), len(self.c), name)
            self.assertTrue(np.isfinite(v).sum() > 100, name)


# ---------------------------------------------------------------------------------------------- plan
class TestPlan(unittest.TestCase):
    def plan(self, **blocks):
        return make_plan(ind_ma1={"entry": True}, exit_when=cfg(**blocks))

    def test_all_seven_validate(self):
        H.validate_plan(self.plan(
            rsi=osc_cfg(), mfi=osc_cfg(ob=80.0, os_=20.0), cci=osc_cfg(ob=100.0, os_=-100.0),
            macd={"on": True, "fast": 12, "slow": 26, "signal": 9, "pivot": 3, "lookback": 40, "tf": "3d"},
            stoch={"on": True, "length": 14, "smooth": 3, "pivot": 3, "lookback": 40, "zone": True, "ob": 80.0, "os": 20.0, "tf": "1w"},
            obv={"on": True, "pivot": 3, "lookback": 40, "tf": "1h"},
            cmo={"on": True, "length": 9, "pivot": 3, "lookback": 40, "zone": True, "ob": 50.0, "os": -50.0, "tf": "15m"}))

    def test_every_timeframe_is_accepted_and_nonsense_is_not(self):
        for tf in H.DIV_TFS:
            H.validate_plan(self.plan(rsi=dict(osc_cfg(), tf=tf)))
        self.assertEqual(H.DIV_TFS[0], "Chart")
        self.assertEqual(set(H.DIV_TFS[1:]), {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "2d", "3d", "5d", "1w", "1M"})
        for bad in ("7d", "3D", "1m ", "", "weekly", "1Y"):
            with self.assertRaises(H.PlanError, msg=bad):
                H.validate_plan(self.plan(rsi=dict(osc_cfg(), tf=bad)))

    def test_numbers_are_checked_per_oscillator(self):
        macd = {"on": True, "fast": 12, "slow": 26, "signal": 9, "pivot": 3, "lookback": 40}
        H.validate_plan(self.plan(macd=macd))
        for key in ("fast", "slow", "signal", "pivot", "lookback"):
            m = dict(macd)
            del m[key]
            with self.assertRaises(H.PlanError, msg=key):
                H.validate_plan(self.plan(macd=m))
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.plan(macd=dict(macd, fast=26, slow=12)))
        stoch = {"on": True, "length": 14, "smooth": 3, "pivot": 3, "lookback": 40}
        H.validate_plan(self.plan(stoch=stoch))
        for key in ("length", "smooth"):
            s_ = dict(stoch)
            del s_[key]
            with self.assertRaises(H.PlanError, msg=key):
                H.validate_plan(self.plan(stoch=s_))
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.plan(cmo={"on": True, "pivot": 3, "lookback": 40}))     # no length
        H.validate_plan(self.plan(obv={"on": True, "pivot": 3, "lookback": 40}))         # OBV needs no length

    def test_levels_only_where_a_band_exists(self):
        # turn-back ticked: RSI needs its levels, MACD histogram and OBV do not
        p = self.plan(macd={"on": True, "fast": 12, "slow": 26, "signal": 9, "pivot": 3, "lookback": 40},
                      obv={"on": True, "pivot": 3, "lookback": 40})
        p["exit_when"]["div"]["turn"] = True
        H.validate_plan(p)
        p2 = self.plan(stoch={"on": True, "length": 14, "smooth": 3, "pivot": 3, "lookback": 40})
        p2["exit_when"]["div"]["turn"] = True
        with self.assertRaises(H.PlanError):
            H.validate_plan(p2)

    def test_an_older_plan_without_the_new_blocks_is_valid_and_they_are_off(self):
        p = make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg()))
        for k in ("macd", "stoch", "obv", "cmo"):
            p["exit_when"]["div"].pop(k, None)
        H.validate_plan(p)
        c = H.complete_plan(p)
        for k in ("macd", "stoch", "obv", "cmo"):
            self.assertFalse(c["exit_when"]["div"][k]["on"], k)
        self.assertEqual(c["exit_when"]["div"]["rsi"]["tf"], "Chart")

    def test_div_tfs_needed(self):
        self.assertEqual(H.div_tfs_needed(self.plan(rsi=osc_cfg())), [])
        p = self.plan(rsi=dict(osc_cfg(), tf="3d"), cci=dict(osc_cfg(ob=100.0, os_=-100.0), tf="3d"),
                      obv={"on": True, "pivot": 3, "lookback": 40, "tf": "1h"}, mfi=dict(osc_cfg(), tf="1w", on=False))
        self.assertEqual(H.div_tfs_needed(p), ["3d", "1h"])
        off = dict(p, exit_when=dict(p["exit_when"], on=False))
        self.assertEqual(H.div_tfs_needed(off), [])
        ma = make_plan(ind_ma1={"entry": True}, exit_when={"on": True, "signal": H.EXIT_BOTH, "after": H.EXIT_WAIT})
        self.assertEqual(H.div_tfs_needed(ma), [])

    def test_review_text_has_timeframe_and_numbers(self):
        c = H.complete_plan(self.plan(macd={"on": True, "fast": 12, "slow": 26, "signal": 9, "pivot": 3, "lookback": 40, "tf": "3d"},
                                      obv={"on": True, "pivot": 3, "lookback": 40, "tf": "1h"}))
        txt = H._exit_when_text(c["exit_when"])
        self.assertIn("MACD histogram on 3d candles (fast 12, slow 26, signal 9, pivot 3, look-back 40)", txt)
        self.assertIn("OBV on 1h candles (pivot 3, look-back 40)", txt)


# ---------------------------------------------------------------------------------------------- other timeframes
def aggregate(df, g):
    m = len(df) // g
    d = df.iloc[:m * g]
    o = pd.DataFrame({
        "open_time": d["open_time"].to_numpy()[::g], "open": d["open"].to_numpy()[::g],
        "high": d["high"].to_numpy().reshape(m, g).max(1), "low": d["low"].to_numpy().reshape(m, g).min(1),
        "close": d["close"].to_numpy().reshape(m, g)[:, -1], "volume": d["volume"].to_numpy().reshape(m, g).sum(1)})
    return o


class TestMapping(unittest.TestCase):
    def test_a_signal_counts_once_on_the_first_chart_candle_that_can_see_it(self):
        chart = np.arange(12) * 100
        htf = np.array([0, 300, 600, 900])            # 3 chart candles each
        sig = np.array([False, True, False, True])
        lvl = np.array([np.nan, 5.0, np.nan, 7.0])
        osc = np.array([10.0, 20.0, 30.0, 40.0])
        s, l, o = H.htf_to_chart(sig, lvl, osc, htf, 300, chart, 100)
        # candle 5 (open 500) closes at 600: the htf candle 1 (300..600) has closed by then -> signal on chart candle 5
        self.assertEqual(list(np.flatnonzero(s)), [5, 11])
        self.assertEqual(l[5], 5.0)
        self.assertEqual(l[11], 7.0)
        self.assertTrue(np.isnan(l[[0, 1, 2, 3, 4, 6, 7, 8, 9, 10]]).all())
        self.assertEqual(o[2], 10.0)                    # chart candle 2 closes at 300: htf candle 0 closed
        self.assertTrue(np.isnan(o[1]))
        self.assertEqual(o[5], 20.0)

    def test_the_last_chart_candle_inside_an_htf_candle_does_not_see_it_early(self):
        chart = np.arange(6) * 100
        s, _l, o = H.htf_to_chart(np.array([True, False]), np.array([1.0, np.nan]), np.array([5.0, 6.0]), np.array([0, 300]), 300, chart, 100)
        self.assertEqual(list(np.flatnonzero(s)), [2])      # the candle that CLOSES at 300 = the end of htf candle 0
        self.assertFalse(s[1])

    def test_several_closed_candles_in_one_step_latest_signal_wins(self):
        chart = np.array([0, 1000, 2000])               # the second chart candle sees three new closed candles at once
        htf = np.array([0, 1000, 1100, 1200])           # 100 long: closes at 100, 1100, 1200, 1300
        sig = np.array([False, True, True, False])
        lvl = np.array([np.nan, 1.0, 2.0, np.nan])
        s, l, _o = H.htf_to_chart(sig, lvl, np.zeros(4), htf, 100, chart, 1000)
        self.assertEqual(list(np.flatnonzero(s)), [1])  # chart candle 1 (closes 2000): the candles closing 1100..1300 are all new
        self.assertEqual(l[1], 2.0)

    def test_a_lower_timeframe_works_the_same_way(self):
        chart = np.arange(4) * 300                       # 300 per chart candle, ltf candles 100
        ltf = np.arange(12) * 100
        sig = np.zeros(12, dtype=bool)
        sig[4] = True                                    # closes at 500 -> seen at the chart candle closing at 600 (index 1)
        s, l, _ = H.htf_to_chart(sig, np.where(sig, 9.0, np.nan), np.arange(12.0), ltf, 100, chart, 300)
        self.assertEqual(list(np.flatnonzero(s)), [1])
        self.assertEqual(l[1], 9.0)

    def test_nothing_before_the_other_timeframes_history_starts(self):
        chart = np.arange(6) * 100
        s, l, o = H.htf_to_chart(np.array([True]), np.array([1.0]), np.array([5.0]), np.array([400]), 300, chart, 100)
        self.assertFalse(s.any())
        self.assertTrue(np.isnan(o).all() and np.isnan(l).all())

    def test_empty_inputs(self):
        s, l, o = H.htf_to_chart([], [], [], [], 300, np.arange(5) * 100, 100)
        self.assertEqual((len(s), s.any()), (5, False))


class TestComputeWithOtherTimeframe(unittest.TestCase):
    G = 3

    def setUp(self):
        self.df = candles(1500, seed=5, vol=1.6)
        self.htf = aggregate(self.df, self.G)
        self.tf_ms = int(self.df["open_time"].iloc[1] - self.df["open_time"].iloc[0]) * self.G

    def run_tf(self, name, d, tf_key="1d", **extra):
        # candle sizes in the test are 1 hour; use "3h" candles as the other timeframe via a patched ms table
        H.DIV_TF_MS[tf_key] = self.tf_ms
        self.addCleanup(lambda: H.DIV_TF_MS.__setitem__(tf_key, {"1d": 86_400_000}.get(tf_key, self.tf_ms)))
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg(**{name: dict(d, tf=tf_key)}))
        return H.compute_hub(self.df, p, {tf_key: self.htf}), p

    def test_signal_matches_a_by_hand_mapping_for_every_oscillator(self):
        specs = {
            "rsi": {"on": True, "length": 14, "pivot": 3, "lookback": 40, "zone": False, "ob": 70.0, "os": 30.0},
            "mfi": {"on": True, "length": 14, "pivot": 3, "lookback": 40, "zone": False, "ob": 80.0, "os": 20.0},
            "cci": {"on": True, "length": 20, "pivot": 3, "lookback": 40, "zone": False, "ob": 100.0, "os": -100.0},
            "macd": {"on": True, "fast": 12, "slow": 26, "signal": 9, "pivot": 3, "lookback": 40},
            "stoch": {"on": True, "length": 14, "smooth": 3, "pivot": 3, "lookback": 40, "zone": False, "ob": 80.0, "os": 20.0},
            "obv": {"on": True, "pivot": 3, "lookback": 40},
            "cmo": {"on": True, "length": 9, "pivot": 3, "lookback": 40, "zone": False, "ob": 50.0, "os": -50.0},
        }
        h_, l_, c_, v_ = (self.htf[k].to_numpy() for k in ("high", "low", "close", "volume"))
        for name, d in specs.items():
            frame, _p = self.run_tf(name, d)
            osc_h = H.div_oscillator(name, d, h_, l_, c_, v_)
            bull, bear, bl, br = H.osc_divergence(l_, h_, osc_h, 3, 40)
            sc, lc, oc = H.htf_to_chart(bear, br, osc_h, self.htf["open_time"].to_numpy(), self.tf_ms,
                                        self.df["open_time"].to_numpy(), int(self.df["open_time"].iloc[1] - self.df["open_time"].iloc[0]))
            ok, when = H.div_exit_arrays(oc, sc, lc, self.df["close"].to_numpy(), self.df["open_time"].to_numpy(dtype=float), 3, True, None, None, False, False)
            self.assertTrue((frame[f"xd_{name}_ok_l"].to_numpy() == ok).all(), name)
            self.assertTrue(ok.sum() > 0, name)
            self.assertTrue(np.allclose(frame[f"xd_{name}_osc"].to_numpy(), oc, equal_nan=True), name)

    def test_the_signal_never_comes_before_the_other_candle_has_closed(self):
        frame, _ = self.run_tf("rsi", {"on": True, "length": 14, "pivot": 3, "lookback": 40, "zone": False, "ob": 70.0, "os": 30.0})
        ot = self.df["open_time"].to_numpy()
        found = frame["xd_rsi_t_l"].to_numpy()
        ok = frame["xd_rsi_ok_l"].to_numpy()
        for i in np.flatnonzero(ok):
            d_time = found[i]
            j = int(np.flatnonzero(ot == d_time)[0])
            # the chart candle the signal was found on closes on a boundary of the 3-candle groups (a group has just closed)
            self.assertEqual((j + 1) % self.G, 0)

    def test_no_look_ahead_cutting_the_future_changes_nothing(self):
        d = {"on": True, "length": 14, "pivot": 3, "lookback": 40, "zone": False, "ob": 70.0, "os": 30.0}
        full, p = self.run_tf("rsi", d)
        cut_n = 1200
        cut_df = self.df.iloc[:cut_n].reset_index(drop=True)
        last_close = int(cut_df["open_time"].iloc[-1]) + int(self.df["open_time"].iloc[1] - self.df["open_time"].iloc[0])
        cut_htf = self.htf[self.htf["open_time"] + self.tf_ms <= last_close].reset_index(drop=True)
        cut = H.compute_hub(cut_df, p, {"1d": cut_htf})
        a = full["xd_rsi_ok_l"].to_numpy()[:cut_n]
        b = cut["xd_rsi_ok_l"].to_numpy()
        self.assertTrue((a == b).all())

    def test_missing_candles_are_an_error_not_a_silent_no_signal(self):
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg(rsi=dict(osc_cfg(), tf="3d")))
        with self.assertRaises(H.HtfMissing):
            H.compute_hub(self.df, p)
        with self.assertRaises(H.HtfMissing):
            H.compute_hub(self.df, p, {"1h": self.htf})

    def test_chart_timeframe_needs_no_extra_candles(self):
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg(rsi=osc_cfg()))
        f = H.compute_hub(self.df, p)
        self.assertIn("xd_rsi_ok_l", f.columns)

    def test_mixed_timeframes_each_on_their_own(self):
        H.DIV_TF_MS["3d"], old = self.tf_ms, H.DIV_TF_MS["3d"]
        self.addCleanup(lambda: H.DIV_TF_MS.__setitem__("3d", old))
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg(rsi=dict(osc_cfg(pivot=3, lookback=40), tf="3d"),
                                                             cci=osc_cfg(length=20, pivot=3, lookback=40, ob=100.0, os_=-100.0)))
        f = H.compute_hub(self.df, p, {"3d": self.htf})
        self.assertGreater(f["xd_rsi_ok_l"].sum(), 0)
        self.assertGreater(f["xd_cci_ok_l"].sum(), 0)
        # the chart-timeframe CCI is the same as without any other timeframe
        p2 = make_plan(ind_ma1={"entry": True}, exit_when=cfg(cci=osc_cfg(length=20, pivot=3, lookback=40, ob=100.0, os_=-100.0)))
        f2 = H.compute_hub(self.df, p2)
        self.assertTrue((f["xd_cci_ok_l"].to_numpy() == f2["xd_cci_ok_l"].to_numpy()).all())

    def test_turn_back_does_not_apply_to_macd_and_obv(self):
        d = {"on": True, "fast": 12, "slow": 26, "signal": 9, "pivot": 3, "lookback": 40}
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg(macd=d))
        a = H.compute_hub(self.df, p)
        p_t = copy.deepcopy(p)
        p_t["exit_when"]["div"]["turn"] = True
        b = H.compute_hub(self.df, p_t)
        self.assertTrue((a["xd_macd_ok_l"].to_numpy() == b["xd_macd_ok_l"].to_numpy()).all())
        self.assertGreater(a["xd_macd_ok_l"].sum(), 0)


class TestEndToEndOnTheBacktest(unittest.TestCase):
    def test_backtest_with_another_timeframe_runs_and_exits_on_it(self):
        df = candles(2400, seed=61, vol=1.6)
        G = 3
        htf = aggregate(df, G)
        tf_ms = int(df["open_time"].iloc[1] - df["open_time"].iloc[0]) * G
        old = H.DIV_TF_MS["3d"]
        H.DIV_TF_MS["3d"] = tf_ms
        try:
            p = make_plan(ind_mx={"entry": True, "flip": True}, allow_reentry=True,
                          exit_when=dict(cfg(rsi=dict(osc_cfg(pivot=3, lookback=40), tf="3d")), after=H.EXIT_WAIT))
            p["ind"]["mx"]["modes"] = [H.MX_MODES[0]]
            p["ind"]["ma1"].update(type="EMA", length=9)
            p["ind"]["ma2"].update(type="EMA", length=21)
            p["sizing"].update(stop_on=False)
            p["sl"] = {"on": False}
            res = BT.run(df, p, 1000.0, 0.0, 0.0, 0.0, {"3d": htf})
            self.assertTrue(any(t["exit_reason"] == "Exit when" for t in res["trades"]))
            df2 = df.copy()
            df2.attrs["htf"] = {"3d": htf}
            res2 = BT.run(df2, p, 1000.0, 0.0, 0.0)           # the Analysis tab carries it on the frame
            self.assertEqual(len(res["trades"]), len(res2["trades"]))
            with self.assertRaises(Exception):
                BT.run(df, p, 1000.0, 0.0, 0.0)                # no other-timeframe candles: refused, not silently ignored
        finally:
            H.DIV_TF_MS["3d"] = old


# ---------------------------------------------------------------------------------------------- fetching
class FakeClient:
    def __init__(self, rows_by_tf):
        self.rows = rows_by_tf
        self.calls = []

    async def get_klines(self, symbol, interval, limit=500):
        self.calls.append((interval, limit))
        return self.rows[interval][-limit:]


def raw_rows(start, step, n, seed=0):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    return [[start + i * step, str(c[i]), str(c[i] + 1), str(c[i] - 1), str(c[i]), "10", start + (i + 1) * step - 1, "0", 0, "0", "0", "0"]
            for i in range(n)]


class TestFetch(unittest.TestCase):
    def test_nothing_is_fetched_without_another_timeframe(self):
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg(rsi=osc_cfg()))
        cl = FakeClient({})
        closed = pd.DataFrame(raw_rows(0, 3_600_000, 300), columns=hub_htf.COLS).astype({"open_time": "int64"})
        for col in ["open", "high", "low", "close", "volume"]:
            closed[col] = closed[col].astype(float)
        self.assertEqual(asyncio.run(hub_htf.fetch_htf(cl, "BTCUSDT", p, closed)), {})
        self.assertEqual(cl.calls, [])

    def test_only_closed_candles_come_back_and_the_limit_is_sensible(self):
        tf_ms = H.DIV_TF_MS["1d"]
        chart_ms = 8 * 3_600_000
        n = 300
        start = 100 * tf_ms
        chart_rows = raw_rows(start, chart_ms, n)
        closed = hub_htf._frame(chart_rows)
        last_close = int(closed["open_time"].iloc[-1]) + chart_ms
        day_rows = raw_rows(start - 400 * tf_ms, tf_ms, 600)          # includes candles that are still forming / not yet closed
        cl = FakeClient({"1d": day_rows})
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg(rsi=dict(osc_cfg(), tf="1d")))
        out = asyncio.run(hub_htf.fetch_htf(cl, "BTCUSDT", p, closed, live=False))
        got = out["1d"]
        self.assertTrue(len(got) > 0)
        self.assertTrue(((got["open_time"] + tf_ms) <= last_close).all())
        # the first daily candle at or just before the chart start is covered (warm-up included)
        self.assertLessEqual(int(got["open_time"].iloc[0]), int(closed["open_time"].iloc[0]))
        iv, limit = cl.calls[0]
        self.assertEqual(iv, "1d")
        self.assertGreaterEqual(limit, 300)
        self.assertLessEqual(limit, hub_htf.ANALYSIS_CAP)

    def test_live_limit_is_capped_for_a_tiny_candle_size(self):
        chart_ms = 5 * 60_000
        closed = hub_htf._frame(raw_rows(0, chart_ms, 1000))
        cl = FakeClient({"1m": raw_rows(-10_000_000, 60_000, 4000)})
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg(rsi=dict(osc_cfg(), tf="1m")))
        asyncio.run(hub_htf.fetch_htf(cl, "BTCUSDT", p, closed, live=False))
        self.assertLessEqual(cl.calls[0][1], hub_htf.ANALYSIS_CAP)
        cl2 = FakeClient({"1m": raw_rows(-10_000_000, 60_000, 4000)})
        asyncio.run(hub_htf.fetch_htf(cl2, "BTCUSDT", p, closed, live=True))
        self.assertLessEqual(cl2.calls[0][1], hub_htf.LIVE_CAP)

    def test_okx_knows_3d_and_1w(self):
        from app.okx_futures import okx_bar
        self.assertEqual(okx_bar("3d"), "3Dutc")
        self.assertEqual(okx_bar("1w"), "1Wutc")
        self.assertEqual(okx_bar("2d"), "2Dutc")
        self.assertEqual(okx_bar("5d"), "5Dutc")
        self.assertEqual(okx_bar("1M"), "1Mutc")

    def test_a_month_closes_on_the_first_of_the_next_month(self):
        o = np.array([1730419200000, 1733011200000, 1735689600000, 1738368000000])    # 1 Nov 24, 1 Dec 24, 1 Jan 25, 1 Feb 25 (UTC)
        c = H.div_tf_close("1M", o)
        self.assertEqual(list(c[:3]), list(o[1:]))
        self.assertEqual(int(c[3] - o[3]), 28 * 86_400_000)                            # February 2025 has 28 days
        self.assertEqual(list(H.div_tf_close("2d", o) - o), [172_800_000] * 4)

    def test_monthly_candles_are_only_used_after_the_month_has_closed(self):
        chart_ms = 8 * 3_600_000
        start = 1730419200000                                                         # 1 Nov 2024
        closed = hub_htf._frame(raw_rows(start + 30 * 86_400_000, chart_ms, 12))     # 30 Nov 2024 onwards (a few days)
        months = raw_rows(start - 400 * 86_400_000 // 1, 2_592_000_000, 3)
        months = [[start, "1", "2", "0.5", "1.5", "5", 0, "0", 0, "0", "0", "0"], [1733011200000, "1", "2", "0.5", "1.5", "5", 0, "0", 0, "0", "0", "0"]]
        cl = FakeClient({"1M": months})
        p = make_plan(ind_ma1={"entry": True}, exit_when=cfg(rsi=dict(osc_cfg(), tf="1M")))
        out = asyncio.run(hub_htf.fetch_htf(cl, "BTCUSDT", p, closed, live=False))["1M"]
        self.assertEqual(list(out["open_time"]), [start])                              # December is still running


# ---------------------------------------------------------------------------------------------- the live bot's fetch
from tests.test_hub_trading import Base  # noqa: E402


class RealisticClient:
    """Klines with real open times: 8h chart candles and 1d / 3d candles."""
    MS = {"8h": 28_800_000, "1d": 86_400_000, "3d": 259_200_000, "1h": 3_600_000}

    def __init__(self):
        self.calls = []

    async def get_klines(self, symbol, interval, limit=500):
        self.calls.append((interval, limit))
        step = self.MS[interval]
        end = 1_700_000_000_000 // step * step + step      # the last row is the still-forming candle
        rows = raw_rows(end - (limit - 1) * step, step, limit, seed=len(interval))
        return rows


class TestLiveFetch(Base):
    async def test_hub_fetch_brings_the_other_timeframe_and_the_frame_has_its_signals(self):
        plan = make_plan(ind_ma1={"entry": True, "flip": True},
                         exit_when=dict(cfg(rsi=dict(osc_cfg(pivot=3, lookback=40), tf="3d")), after=H.EXIT_WAIT))
        inst = self.make(plan, timeframe="8h")
        client = RealisticClient()
        inst.client = client
        real = type(inst)._hub_fetch
        frame, snap = await real(inst, plan)
        self.assertIsNotNone(frame)
        intervals = [c[0] for c in client.calls]
        self.assertIn("8h", intervals)
        self.assertIn("3d", intervals)
        self.assertIn("xd_rsi_ok_l", frame.columns)
        self.assertTrue(math.isfinite(float(frame["xd_rsi_osc"].dropna().iloc[-1])))
        self.assertIn("rsi", snap.xdiv)

    async def test_hub_fetch_without_another_timeframe_asks_for_no_extra_candles(self):
        plan = make_plan(ind_ma1={"entry": True, "flip": True}, exit_when=dict(cfg(rsi=osc_cfg(pivot=3, lookback=40)), after=H.EXIT_WAIT))
        inst = self.make(plan, timeframe="8h")
        client = RealisticClient()
        inst.client = client
        await type(inst)._hub_fetch(inst, plan)
        self.assertEqual([c[0] for c in client.calls], ["8h"])


if __name__ == "__main__":
    unittest.main()
