"""Condition ticks (several conditions on one indicator, all must agree) and Counter trend decided by an indicator,
with 0 = no counter-trend entry (owner 2026-10-25, "Ok go. So we don't have to rebuild it again and again").
Arbitrary test numbers only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import numpy as np  # noqa: E402

from app import strategy_hub as H  # noqa: E402
from app import shadow_book as B  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_strategy_hub import make_df  # noqa: E402

DF = make_df(600)
RISING, ABOVE = "MA rising / falling", "Price above / below MA"


class TestConditionTicks(unittest.TestCase):
    def test_one_ma_two_conditions_all_must_agree(self):
        one = lambda m: H.compute_hub(DF, make_plan(ind_ma1=dict(entry=True, modes=[m])))
        a, b = one(RISING), one(ABOVE)
        both = H.compute_hub(DF, make_plan(ind_ma1=dict(entry=True, modes=[RISING, ABOVE])))
        np.testing.assert_array_equal(both["ma1_long"].values, a["ma1_long"].values & b["ma1_long"].values)
        np.testing.assert_array_equal(both["ma1_short"].values, a["ma1_short"].values & b["ma1_short"].values)

    def test_old_single_condition_is_the_ticked_one(self):
        old = make_plan(ind_ma1=dict(entry=True, mode=RISING))
        new = make_plan(ind_ma1=dict(entry=True, modes=[RISING]))
        old["ind"]["ma1"].pop("modes", None)
        new["ind"]["ma1"].pop("mode", None)
        self.assertEqual(H.upgrade_plan(old)["ind"]["ma1"]["modes"], [RISING])
        np.testing.assert_array_equal(H.compute_hub(DF, old)["dir_e"].values, H.compute_hub(DF, new)["dir_e"].values)

    def test_rsi_and_macd_and_mx_several(self):
        r = H.compute_hub(DF, make_plan(ind_rsi=dict(entry=True, modes=[H.RSI_LEVEL, H.RSI_OBOS], oversold=40.0,
                                                     overbought=60.0, n=3)))
        r1 = H.compute_hub(DF, make_plan(ind_rsi=dict(entry=True, modes=[H.RSI_LEVEL])))
        r2 = H.compute_hub(DF, make_plan(ind_rsi=dict(entry=True, modes=[H.RSI_OBOS], oversold=40.0, overbought=60.0, n=3)))
        np.testing.assert_array_equal(r["rsi_long"].values, r1["rsi_long"].values & r2["rsi_long"].values)
        m = H.compute_hub(DF, make_plan(ind_macd=dict(entry=True, modes=[H.MACD_HIST, H.MACD_ZERO])))
        with np.errstate(invalid="ignore"):
            np.testing.assert_array_equal(m["macd_long"].values, (m["macd_hist"].values > 0) & (m["macd_line"].values > 0))
        x = H.compute_hub(DF, make_plan(ind_mx=dict(entry=True, modes=["MA 1 above / below MA 2", "MA 1 crossed MA 2 within N candles"], n=5)))
        x1 = H.compute_hub(DF, make_plan(ind_mx=dict(entry=True, modes=["MA 1 above / below MA 2"])))
        self.assertTrue((x["mx_long"].values <= x1["mx_long"].values).all())

    def test_validation(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan(ind_ma1=dict(entry=True, modes=[])))
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan(ind_ma1=dict(entry=True, modes=[RISING, RISING])))
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan(ind_ma1=dict(entry=True, modes=["nope"])))
        p = make_plan(ind_ma1=dict(entry=True, modes=[RISING, H.MA_PULLBACK]))
        del p["ind"]["ma1"]["n"]
        with self.assertRaises(H.PlanError):                      # pullback ticked: N is needed
            H.validate_plan(p)
        p = make_plan(ind_rsi=dict(entry=True, modes=[H.RSI_LEVEL, H.RSI_DIV], pivot=5, lookback=60, n=0))
        H.validate_plan(p)
        del p["ind"]["rsi"]["pivot"]
        with self.assertRaises(H.PlanError):                      # divergence ticked: its numbers are needed
            H.validate_plan(p)


def counter(**kw):
    return {"on": True, "alloc_pct": 3.0, "leverage": 2, **kw}


def sizing(ct):
    return {"alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_pct": 6.0, "counter": ct}


class TestCounterTrendBy(unittest.TestCase):
    def snap_at(self, plan, i):
        return H.hub_snapshot_at(H.compute_hub(DF, plan), i)

    def test_old_plan_uses_the_ema(self):
        p = make_plan(ind_rsi=dict(entry=True), sizing=sizing(counter(ema_length=50)))
        self.assertEqual(H.upgrade_plan(p)["sizing"]["counter"]["by"], H.CT_EMA)
        H.validate_plan(p)

    def test_decided_by_an_indicator(self):
        p = make_plan(ind_macd=dict(entry=True), ind_ma1=dict(type="EMA", length=50),
                      sizing=sizing(counter(by="ma1")))
        H.validate_plan(p)                                         # MA 1 needs only its numbers, no ENTRY tick
        f = H.compute_hub(DF, p)
        np.testing.assert_array_equal(f["ct_long"].values, f["ma1_long"].values)
        cp = H.complete_plan(p)
        for i in range(300, 600, 37):
            s = H.hub_snapshot_at(f, i)
            alloc, lev, kind = H.entry_size("LONG", s, cp)
            self.assertEqual(kind, "trend" if f["ma1_long"].values[i] else "counter")
            self.assertEqual((alloc, lev), (10.0, 5.0) if kind == "trend" else (3.0, 2.0))
            kind_s = H.entry_size("SHORT", s, cp)[2]
            self.assertEqual(kind_s, "trend" if f["ma1_short"].values[i] else "counter")

    def test_its_numbers_are_needed(self):
        p = make_plan(ind_macd=dict(entry=True), sizing=sizing(counter(by="rsi")))
        del p["ind"]["rsi"]["length"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p = make_plan(ind_macd=dict(entry=True), sizing=sizing(counter(by="ci")))
        with self.assertRaises(H.PlanError):                      # no side: cannot decide the trend
            H.validate_plan(p)
        p = make_plan(ind_macd=dict(entry=True), sizing=sizing(counter(by="candle")), chart_type="Candles")
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_zero_means_no_counter_entry(self):
        p = make_plan(ind_macd=dict(entry=True), ind_ma1=dict(type="EMA", length=50),
                      sizing=sizing(counter(by="ma1", alloc_pct=0)))
        H.validate_plan(p)
        cp = H.complete_plan(p)
        self.assertTrue(H.counter_entry_off("counter", cp))
        self.assertFalse(H.counter_entry_off("trend", cp))
        f = H.compute_hub(DF, p)
        i = next(k for k in range(300, 600) if not f["ma1_long"].values[k])
        s = H.hub_snapshot_at(f, i)
        self.assertIsNone(H.paper_size("LONG", s, cp, None, 1000.0))
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan(ind_macd=dict(entry=True), sizing=sizing(counter(alloc_pct=-1, ema_length=50))))

    def test_zero_fixed_base(self):
        p = make_plan(ind_macd=dict(entry=True),
                      sizing={"method": H.SIZE_FIXED, "base_amount": 100.0, "leverage": 5, "stop_on": True,
                              "stop_basis": H.STOP_ENTRY, "stop_pct": 6.0,
                              "counter": {"on": True, "ema_length": 50, "base_amount": 0, "leverage": 2}})
        H.validate_plan(p)
        cp = H.complete_plan(p)
        self.assertTrue(H.counter_entry_off("counter", cp))

    def test_shadow_does_not_open_a_counter_trade_at_zero(self):
        from tests.test_hub_trading import snap_of
        p = H.complete_plan(make_plan(ind_rsi=dict(entry=True, flip=True), sizing=sizing(counter(ema_length=50, alloc_pct=0)),
                                      shadow={"on": True, "trades": 1, "slippage_ticks": 0, "tracker_start": 1000.0}))
        s = snap_of(1, dir_e=1, dir_f=1, close=100.0)
        s.c_close, s.size_ema = 100.0, 120.0                 # long below the EMA = counter trend
        book = B.new_book(1000.0)
        ev, _ = B.on_candle(book, s, p, tick_size=0.1, last_total=None, balance=1000.0, t_ms=1)
        self.assertEqual(book["pos"], 0)
        self.assertTrue(any("counter-trend" in e for e in ev), ev)


if __name__ == "__main__":
    unittest.main()


from tests.test_hub_ladder import LadderBase  # noqa: E402


class TestLiveBotZeroCounter(LadderBase):
    async def test_live_bot_does_not_open_a_counter_trend_entry_at_zero(self):
        inst = self.make_l(sizing={"alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_pct": 6.0,
                                   "counter": {"on": True, "ema_length": 50, "alloc_pct": 0, "leverage": 2}})
        await self.enter_long(inst, size_ema=120.0, c_close=100.0)     # long below the EMA = counter trend
        self.assertIsNone(inst.state.direction)
        self.assertEqual(inst.client.market_orders, [])
        self.assertIn("counter-trend", inst.state.last_decision)

    async def test_live_bot_opens_with_the_trend(self):
        inst = self.make_l(sizing={"alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_pct": 6.0,
                                   "counter": {"on": True, "ema_length": 50, "alloc_pct": 0, "leverage": 2}})
        await self.enter_long(inst, size_ema=80.0, c_close=100.0)      # above the EMA = with the trend
        self.assertEqual(inst.state.direction, "LONG")
