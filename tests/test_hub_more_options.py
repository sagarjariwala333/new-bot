"""Option ticks / condition added 2026-10-25 (owner: "Yes.. also amend the volume strategy", "1 and 5 - have an
option checkbox", "Go"). Arbitrary test numbers only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import numpy as np  # noqa: E402

from app import strategy_hub as H  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_strategy_hub import make_df  # noqa: E402

DF = make_df(600)


def frame(**kw):
    return H.compute_hub(DF, make_plan(**kw))


class TestOldPlansUnchanged(unittest.TestCase):
    def test_no_tick_means_as_before(self):
        for kw, extra in ((dict(ind_candle=dict(entry=True), chart_type="Heikin Ashi"), ("candle", "strong")),
                          (dict(ind_adx=dict(entry=True)), ("adx", "rising")),
                          (dict(ind_atr=dict(entry=True)), ("atr", "rising")),
                          (dict(ind_vol=dict(entry=True)), ("vol", "exclude_current"))):
            p = make_plan(**kw)
            p["ind"][extra[0]].pop(extra[1], None)
            H.validate_plan(p)                                  # an older plan without the tick is accepted
            a = H.compute_hub(DF, p)
            p["ind"][extra[0]][extra[1]] = False
            b = H.compute_hub(DF, p)
            b_name = extra[0]
            np.testing.assert_array_equal(a[f"{b_name}_long"].values, b[f"{b_name}_long"].values)

    def test_tick_must_be_on_or_off(self):
        p = make_plan(ind_adx=dict(entry=True, rising="yes"))
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)


class TestStrongCandle(unittest.TestCase):
    def test_no_wick_against_the_trade(self):
        f = frame(ind_candle=dict(entry=True, strong=True), chart_type="Heikin Ashi")
        o, h, l, c = (f[k].values for k in ("c_open", "c_high", "c_low", "c_close"))
        np.testing.assert_array_equal(f["candle_long"].values, (c > o) & (l >= o - 1e-9 * np.maximum(abs(o), 1)))
        np.testing.assert_array_equal(f["candle_short"].values, (c < o) & (h <= o + 1e-9 * np.maximum(abs(o), 1)))
        plain = frame(ind_candle=dict(entry=True), chart_type="Heikin Ashi")
        self.assertLess(f["candle_long"].sum(), plain["candle_long"].sum())


class TestPullback(unittest.TestCase):
    def test_touch_and_close_back(self):
        f = frame(ind_ma1=dict(entry=True, mode=H.MA_PULLBACK, n=0))
        ma, lo, hi, c = f["ma1"].values, f["c_low"].values, f["c_high"].values, f["c_close"].values
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(f["ma1_long"].values, (lo <= ma) & (c > ma))
            np.testing.assert_array_equal(f["ma1_short"].values, (hi >= ma) & (c < ma))

    def test_n_needed_and_used(self):
        p = make_plan(ind_ma1=dict(entry=True, mode=H.MA_PULLBACK))
        del p["ind"]["ma1"]["n"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        a = frame(ind_ma1=dict(entry=True, mode=H.MA_PULLBACK, n=0))
        b = frame(ind_ma1=dict(entry=True, mode=H.MA_PULLBACK, n=2))
        np.testing.assert_array_equal(b["ma1_long"].values, H._within(a["ma1_long"].values, 2))


class TestRising(unittest.TestCase):
    def test_adx_rising(self):
        a = frame(ind_adx=dict(entry=True))
        b = frame(ind_adx=dict(entry=True, rising=True))
        adx = b["adx"].values
        prev = np.concatenate([[np.nan], adx[:-1]])
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(b["adx_long"].values, a["adx_long"].values & (adx > prev))

    def test_atr_rising(self):
        a = frame(ind_atr=dict(entry=True))
        b = frame(ind_atr=dict(entry=True, rising=True))
        atr = b["atr_chart"].values
        prev = np.concatenate([[np.nan], atr[:-1]])
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(b["atr_long"].values, a["atr_long"].values & (atr > prev))


class TestVolumeWithoutSignalCandle(unittest.TestCase):
    def test_average_of_the_previous_candles(self):
        f = frame(ind_vol=dict(entry=True, length=20, mult=1.5, exclude_current=True))
        v = DF["volume"].values
        exp = np.zeros(len(v), dtype=bool)
        for i in range(20, len(v)):
            exp[i] = v[i] >= v[i - 20:i].mean() * 1.5
        np.testing.assert_array_equal(f["vol_long"].values, exp)
        g = frame(ind_vol=dict(entry=True, length=20, mult=1.5))
        self.assertFalse(np.array_equal(f["vol_long"].values, g["vol_long"].values))


if __name__ == "__main__":
    unittest.main()
