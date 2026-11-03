"""Take profit "Measured from" (2026-10-27): Entry price (default, as before) or Signal candle close.
Arbitrary test numbers, not the owner's settings."""
import copy
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_tp_trail import TPBase, tp_plan, snap_of  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import hub_state  # noqa: E402


def planned(**tp_over):
    p = tp_plan()
    p["tp"].update(tp_over)
    return p


class TestPlanKey(unittest.TestCase):
    def valid(self, **tp_over):
        plan = H.complete_plan(H.upgrade_plan(planned(**tp_over)))
        return H.validate_plan(plan)

    def test_plan_without_the_key_is_valid_and_means_entry_price(self):
        plan = self.valid()
        self.assertNotIn("from", plan["tp"])
        self.assertEqual(H.tp_from(plan), H.TP_FROM_ENTRY)

    def test_both_choices_are_valid(self):
        for v in H.TP_FROMS:
            self.assertEqual(H.tp_from(self.valid(**{"from": v})), v)

    def test_a_wrong_choice_is_refused(self):
        with self.assertRaises(H.PlanError) as cm:
            self.valid(**{"from": "Last candle open"})
        self.assertIn("measured from", str(cm.exception))

    def test_upgrade_and_complete_do_not_add_the_key(self):
        # no key added = an old plan's rules hash does not change on upgrade
        plan = H.complete_plan(H.upgrade_plan(planned()))
        self.assertNotIn("from", plan["tp"])

    def test_blank_choice_means_entry_price(self):
        self.assertEqual(H.tp_from({"tp": {"from": ""}}), H.TP_FROM_ENTRY)
        self.assertEqual(H.tp_from({}), H.TP_FROM_ENTRY)


class TestBasePrice(unittest.TestCase):
    def plan(self, v=None):
        p = {"tp": {}}
        if v is not None:
            p["tp"]["from"] = v
        return p

    def test_entry_price_is_the_default(self):
        self.assertEqual(H.tp_base_price(self.plan(), 100.4, 100.0), 100.4)
        self.assertEqual(H.tp_base_price(self.plan(H.TP_FROM_ENTRY), 100.4, 100.0), 100.4)

    def test_signal_close_is_used_when_chosen(self):
        self.assertEqual(H.tp_base_price(self.plan(H.TP_FROM_SIGNAL), 100.4, 100.0), 100.0)

    def test_a_bad_signal_close_falls_back_to_the_entry_price(self):
        for bad in (None, float("nan"), 0, -5, "x", float("inf")):
            self.assertEqual(H.tp_base_price(self.plan(H.TP_FROM_SIGNAL), 100.4, bad), 100.4, bad)

    def test_prices_follow_the_base_for_both_measures(self):
        plan = H.complete_plan(H.upgrade_plan(planned(**{"from": H.TP_FROM_SIGNAL})))
        long_ = H.tp_prices("LONG", 100.0, 2.0, plan)
        self.assertEqual([round(t["price"], 6) for t in long_], [102.0, 106.0, 110.0])
        short = H.tp_prices("SHORT", 100.0, 2.0, plan)
        self.assertEqual([round(t["price"], 6) for t in short], [98.0, 94.0, 90.0])
        # % of price movement is counted on the same base price: 3% of 100 = 3
        plan["tp"]["basis"] = H.BASIS_PRICE
        plan["tp"]["levels"][0]["value"] = 3.0
        self.assertAlmostEqual(H.tp_prices("LONG", 100.0, 2.0, plan)[0]["price"], 103.0)


class TestLiveBot(TPBase):
    async def open_with_fill(self, inst, fill, close=100.0, side=1):
        inst.client.mark_price = fill            # the real fill price differs from the signal candle close
        await self.tick(inst, snap_of(1000, dir_e=side, dir_f=side, close=close))
        self.assertEqual(inst.state.direction, "LONG" if side == 1 else "SHORT")

    def levels(self, inst):
        return [round(l["price"], 4) for l in inst.hub.tp["levels"]]

    async def test_default_is_the_entry_price_as_before(self):
        inst = self.make(tp_plan())                                  # no "from" key = an old plan
        await self.open_with_fill(inst, 100.4)
        self.assertEqual(self.levels(inst), [102.4, 106.4, 110.4])
        self.assertAlmostEqual(inst.client.limit_calls[0][3], 102.4, places=2)

    async def test_entry_price_chosen_is_the_same_as_the_default(self):
        inst = self.make(planned(**{"from": H.TP_FROM_ENTRY}))
        await self.open_with_fill(inst, 100.4)
        self.assertEqual(self.levels(inst), [102.4, 106.4, 110.4])

    async def test_signal_candle_close_long(self):
        inst = self.make(planned(**{"from": H.TP_FROM_SIGNAL}))
        await self.open_with_fill(inst, 100.4)                       # filled above the signal close
        self.assertEqual(self.levels(inst), [102.0, 106.0, 110.0])   # 100 + 1 / 3 / 5 x ATR 2
        self.assertAlmostEqual(inst.client.limit_calls[0][3], 102.0, places=2)
        saved = hub_state.load(inst.platform, inst.account_id, inst.symbol)
        self.assertEqual([round(l["price"], 4) for l in saved.tp["levels"]], [102.0, 106.0, 110.0])
        self.assertAlmostEqual(saved.tp["entry_price"], 100.4, places=4)   # the memory still keeps the real fill

    async def test_signal_candle_close_short(self):
        inst = self.make(planned(**{"from": H.TP_FROM_SIGNAL}))
        await self.open_with_fill(inst, 99.6, side=-1)
        self.assertEqual(self.levels(inst), [98.0, 94.0, 90.0])
        self.assertEqual(inst.client.limit_calls[0][1], "BUY")

    async def test_signal_candle_close_with_no_slippage_equals_the_entry_price(self):
        a = self.make(planned(**{"from": H.TP_FROM_SIGNAL}))
        await self.open_with_fill(a, 100.0)
        self.assertEqual(self.levels(a), [102.0, 106.0, 110.0])

    async def test_a_tp_already_behind_the_market_is_still_placed_not_crashed(self):
        # filled far above the signal close: TP1 (102) is below the fill (105) for a long. The order is sent as
        # a reduce-only limit (the exchange fills it at once); the bot must not crash.
        inst = self.make(planned(**{"from": H.TP_FROM_SIGNAL}))
        await self.open_with_fill(inst, 105.0)
        self.assertEqual(len(inst.client.limit_calls), 1)
        self.assertAlmostEqual(inst.client.limit_calls[0][3], 102.0, places=2)

    async def test_tp_off_ignores_the_choice(self):
        p = make_plan(ind_ma1={"entry": True, "flip": True})
        p["tp"] = {"on": False, "basis": H.BASIS_ATR, "atr_length": 14, "from": H.TP_FROM_SIGNAL,
                   "levels": [{"on": False, "value": 100.0, "pct": 1.0} for _ in range(3)]}
        inst = self.make(p)
        await self.open_with_fill(inst, 100.4)
        self.assertEqual(inst.client.limit_calls, [])
        self.assertIsNone(inst.hub.tp)


if __name__ == "__main__":
    unittest.main()
