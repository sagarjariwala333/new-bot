"""Trailing Stop, 4th type "Signal candle high / low +/- ATR" (owner 2026-10-28): "the SL is 1 (editable) ATR +/- signal
candle high / low .. it moves closer to entry and into profit every time a candle closes .. never further from entry ..
when the live price hits the TS the trade closes". The ATR is the one captured at the signal. Arbitrary test numbers."""
import math
import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_ladder import LadderBase, sl, ATR, PRICE  # noqa: E402  (stubs + temp data dir)
from tests.test_hub_trading import snap_of  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import shadow_book as B  # noqa: E402
from app import hub_trading  # noqa: E402


def candle_trail(value=100.0, atr_length=14):
    return {"on": True, "type": H.TRAIL_CANDLE, "value": value, "atr_length": atr_length}


def plan(**tr):
    return make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": PRICE, "value": 20.0},
                     trail=candle_trail(**tr))


def snap(t, close=100.0, low=None, high=None, dir_=1, ts_atr=2.0):
    s = snap_of(t, dir_e=dir_, dir_f=dir_, close=close)
    s.ts_atr = ts_atr
    if low is not None:
        s.low = low
    if high is not None:
        s.high = high
    return s


class TestRule(unittest.TestCase):
    def test_distance_is_a_percent_of_the_signal_candle_atr(self):
        p = H.complete_plan(plan(value=100.0))
        self.assertEqual(H.candle_trail_distance(p, 100.0, 2.0), 2.0)          # 100 = 1 ATR
        p = H.complete_plan(plan(value=50.0))
        self.assertEqual(H.candle_trail_distance(p, 100.0, 2.0), 1.0)          # 50 = 0.5 ATR
        p = H.complete_plan(plan(value=250.0))
        self.assertEqual(H.candle_trail_distance(p, 100.0, 2.0), 5.0)

    def test_no_distance_without_an_atr_or_for_other_types(self):
        p = H.complete_plan(plan())
        self.assertIsNone(H.candle_trail_distance(p, 100.0, float("nan")))
        other = H.complete_plan(make_plan(ind_ma1={"entry": True}, trail={"on": True, "type": H.TRAIL_LLHH, "length": 5}))
        self.assertIsNone(H.candle_trail_distance(other, 100.0, 2.0))

    def test_level_long_is_the_low_minus_the_distance_short_is_the_high_plus_it(self):
        self.assertEqual(H.candle_trail_level("LONG", 98.0, 103.0, 2.0), 96.0)
        self.assertEqual(H.candle_trail_level("SHORT", 98.0, 103.0, 2.0), 105.0)
        self.assertIsNone(H.candle_trail_level("LONG", 98.0, 103.0, None))
        self.assertIsNone(H.candle_trail_level("LONG", float("nan"), 103.0, 2.0))
        self.assertIsNone(H.candle_trail_level("LONG", 98.0, 103.0, 0.0))

    def test_only_nearer_is_tighter(self):
        self.assertTrue(H.is_tighter("LONG", 98.5, 97.0))
        self.assertFalse(H.is_tighter("LONG", 96.0, 97.0))
        self.assertTrue(H.is_tighter("SHORT", 101.5, 103.0))
        self.assertFalse(H.is_tighter("SHORT", 104.0, 103.0))


class TestValidation(unittest.TestCase):
    def test_ok_and_listed_as_a_type(self):
        self.assertIn(H.TRAIL_CANDLE, H.TRAIL_TYPES)
        self.assertEqual(len(H.TRAIL_TYPES), 4)
        H.validate_plan(plan())
        H.complete_plan(plan())

    def test_the_distance_and_the_atr_length_are_needed(self):
        for k in ("value", "atr_length"):
            p = plan()
            del p["trail"][k]
            with self.assertRaises(H.PlanError):
                H.validate_plan(p)

    def test_bad_numbers_refused(self):
        for bad in (0, -1, "x", None):
            p = plan()
            p["trail"]["value"] = bad
            with self.assertRaises(H.PlanError):
                H.validate_plan(p)
        p = plan()
        p["trail"]["atr_length"] = 0
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_old_types_still_validate_and_complete_plan_adds_no_value_key(self):
        for tr in ({"on": True, "type": H.TRAIL_LLHH, "length": 5}, {"on": True, "type": H.TRAIL_MA, "ma": "MA 1"},
                   {"on": False}):
            H.validate_plan(make_plan(ind_ma1={"entry": True}, trail=dict(tr)))
        # no new default key: a saved plan's rules hash does not change because of this feature
        self.assertNotIn("value", H.complete_plan(make_plan(ind_ma1={"entry": True}, trail={"on": True, "type": H.TRAIL_LLHH, "length": 5}))["trail"])


class TestLive(LadderBase):
    def make_c(self, **tr):
        return self.make_l(trail=candle_trail(**tr))

    async def test_first_level_at_the_entry_long(self):
        inst = self.make_c()
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)                 # signal candle low 98, ATR 2 -> 96
        lad = inst.hub.ladder
        self.assertAlmostEqual(lad["ts"], 96.0)
        self.assertAlmostEqual(lad["tc_dist"], 2.0)
        self.assertAlmostEqual(lad["ml"], 88.0)                             # Max Loss stays behind it
        self.assertEqual(lad["ex"]["name"], "ts")                           # the nearest level rests on the exchange
        self.assertEqual(inst.client.stop_limit_calls[-1][2], 96.0)
        saved = __import__("app.hub_state", fromlist=["x"]).load(inst.platform, inst.account_id, inst.symbol)
        self.assertAlmostEqual(saved.ladder["tc_dist"], 2.0)               # survives a restart

    async def test_it_moves_nearer_every_candle_close_and_never_back_long(self):
        inst = self.make_c()
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)                 # 96
        inst.client.mark_price = 103.0
        await self.tick(inst, snap(2000, close=103.0, low=100.0, high=104.0, ts_atr=9.0))   # 100 - 2 = 98
        self.assertAlmostEqual(inst.hub.ladder["ts"], 98.0)                 # the ATR is the signal ATR (2), not 9
        self.assertEqual(inst.client.stop_limit_calls[-1][2], 98.0)
        inst.client.mark_price = 104.0
        await self.tick(inst, snap(3000, close=104.0, low=102.0, high=105.0))               # 102 - 2 = 100
        self.assertAlmostEqual(inst.hub.ladder["ts"], 100.0)                # into profit (entry 100)
        inst.client.mark_price = 101.0
        await self.tick(inst, snap(4000, close=101.0, low=99.0, high=103.0))                # 97: further away
        self.assertAlmostEqual(inst.hub.ladder["ts"], 100.0)                # never back
        self.assertEqual(inst.state.status, "IN_POSITION")

    async def test_short_is_mirrored(self):
        inst = self.make_c()
        inst.client.mark_price = 100.0
        await self.tick(inst, snap(1000, close=100.0, high=102.0, dir_=-1))                 # 102 + 2 = 104
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertAlmostEqual(inst.hub.ladder["ts"], 104.0)
        inst.client.mark_price = 97.0
        await self.tick(inst, snap(2000, close=97.0, high=99.0, low=96.0, dir_=-1))         # 99 + 2 = 101
        self.assertAlmostEqual(inst.hub.ladder["ts"], 101.0)
        inst.client.mark_price = 99.0
        await self.tick(inst, snap(3000, close=99.0, high=103.0, dir_=-1))                  # 105: further away
        self.assertAlmostEqual(inst.hub.ladder["ts"], 101.0)

    async def test_the_distance_value_is_editable(self):
        inst = self.make_c(value=50.0)                                      # 0.5 ATR = 1
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 97.0)

    async def test_live_price_through_the_level_closes_the_trade(self):
        # the TS rests on the exchange as a stop-limit; if the live price goes THROUGH it and the trade is still
        # open after the grace (the limit did not fill), the bot closes at market (same rule as every stop)
        inst = self.make_c()
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)                 # 96
        self.assertEqual(inst.hub.ladder["ex"]["name"], "ts")
        inst.hub.ladder["ex"]["triggered"] = True
        inst.client.open_algo_orders.clear()
        inst.client.mark_price = 90.0
        with mock.patch.object(hub_trading.time, "time", return_value=1000.0):
            self.assertFalse(await inst._hub_ladder_check(95.9))             # the limit gets its time first
        self.assertEqual(inst.client.close_orders, [])
        with mock.patch.object(hub_trading.time, "time", return_value=1000.0 + hub_trading.STOP_LIMIT_GRACE_S):
            self.assertTrue(await inst._hub_ladder_check(95.9))
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertIsNone(inst.client.position)

    async def test_a_level_already_crossed_at_the_candle_close_closes_the_trade(self):
        inst = self.make_c()
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        inst.client.mark_price = 90.0                                       # price fell far below the new level
        await self.tick(inst, snap(2000, close=104.0, low=102.0, high=105.0))               # new level 100
        self.assertNotEqual(inst.state.status, "IN_POSITION")

    async def test_no_atr_means_no_trailing_but_the_trade_opens(self):
        inst = self.make_c()
        await self.open_long_at(inst, ts_atr=float("nan"), low=98.0)
        self.assertEqual(inst.state.direction, "LONG")
        self.assertIsNone(inst.hub.ladder["ts"])
        self.assertNotIn("tc_dist", inst.hub.ladder)
        self.assertAlmostEqual(inst.hub.ladder["ml"], 88.0)                 # the other stops still protect it

    async def test_other_trailing_types_are_not_touched(self):
        inst = self.make_l(trail={"on": True, "type": H.TRAIL_LLHH, "length": 5})
        await self.open_long_at(inst)
        self.assertIsNone(inst.hub.ladder["ts"])
        self.assertNotIn("tc_dist", inst.hub.ladder)


class TestShadow(unittest.TestCase):
    def test_shadow_uses_the_same_rule(self):
        from tests.test_hub_sizing_shadow import TestShadowBook, SHADOW
        tb = TestShadowBook()
        pl = tb.plan(sl={"on": True, "basis": PRICE, "value": 20.0}, trail=candle_trail(),
                     shadow={**SHADOW, "slippage_ticks": 0})
        book = B.new_book(1000.0)
        tb.candle(book, snap(1, close=100.0, low=98.0), pl)
        self.assertEqual(book["pos"], 1)
        self.assertAlmostEqual(book["levels"]["ts"], 96.0)
        self.assertAlmostEqual(book["tc_dist"], 2.0)
        tb.candle(book, snap(2, close=104.0, low=102.0, high=105.0, ts_atr=7.0), pl)
        self.assertAlmostEqual(book["levels"]["ts"], 100.0)                 # 102 - 2 (the signal ATR)
        tb.candle(book, snap(3, close=103.0, low=99.0, high=105.0), pl)
        self.assertAlmostEqual(book["levels"]["ts"], 100.0)                 # never back
        closed = B.on_price(book, 99.9, pl, 6000)
        self.assertEqual(closed[0]["exit_price"], 100.0)                    # the live price hit the TS: closed

    def test_shadow_short_and_a_broken_level_closes(self):
        from tests.test_hub_sizing_shadow import TestShadowBook, SHADOW
        tb = TestShadowBook()
        pl = tb.plan(sl={"on": True, "basis": PRICE, "value": 20.0}, trail=candle_trail(),
                     shadow={**SHADOW, "slippage_ticks": 0})
        book = B.new_book(1000.0)
        tb.candle(book, snap(1, close=100.0, high=102.0, dir_=-1), pl)
        self.assertEqual(book["pos"], -1)
        self.assertAlmostEqual(book["levels"]["ts"], 104.0)
        # the candle closes ABOVE its own new level (high 99 + 2 = 101 < close 101.5): already broken -> closed
        tb.candle(book, snap(2, close=101.5, high=99.0, low=97.0, dir_=-1), pl)
        self.assertEqual(book["pos"], 0)


if __name__ == "__main__":
    unittest.main()
