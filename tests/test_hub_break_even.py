"""Break-Even tab (owner 2026-10-26): the Stop Loss moves to the entry price
- After TP: you input 1, 2, 3 (that TP is filled)
- After ATR: you input eg. 100 = 1x ATR (the price moved that much in your favour; ATR at the entry candle)
Both ticked: whichever comes first. It only tightens.
"""
import dataclasses
import unittest

from tests import test_hub_trading as T
from tests._hub_plan import make_plan
from app import strategy_hub as H
from app import shadow_book as B


def plan_be(tp_on=False, **be):
    p = make_plan(ind_ma1={"entry": True, "flip": True})
    p["be"] = {"on": True, **be}
    if tp_on:
        p["tp"] = {"on": True, "basis": H.BASIS_PRICE, "atr_length": 14,
                   "levels": [{"on": True, "value": 2.0, "pct": 50.0}, {"on": True, "value": 4.0, "pct": 50.0},
                              {"on": False, "value": 6.0, "pct": 0.0}]}
    return p


def snap(t=1, close=100.0, dir_e=0, dir_f=0, **kw):
    return dataclasses.replace(T.snap_of(t, dir_e=dir_e, dir_f=dir_f, close=close), **kw)


class TestValidation(unittest.TestCase):
    def bad(self, p, words=None):
        with self.assertRaises(H.PlanError) as cm:
            H.validate_plan(p)
        if words:
            self.assertIn(words, str(cm.exception))

    def test_rules(self):
        H.validate_plan(plan_be(tp_on=True, after_tp=True, tp_n=2))
        H.validate_plan(plan_be(after_atr=True, atr_value=100.0, atr_length=14))
        H.validate_plan(plan_be(tp_on=True, after_tp=True, tp_n=1, after_atr=True, atr_value=50.0, atr_length=14))
        self.bad(plan_be(), "tick After TP and / or After ATR")
        self.bad(plan_be(after_tp=True, tp_n=1), "TP1 is not used")                 # Take profit off
        self.bad(plan_be(tp_on=True, after_tp=True, tp_n=3), "TP3 is not used")     # TP3 not ticked
        self.bad(plan_be(tp_on=True, after_tp=True, tp_n=0))
        self.bad(plan_be(tp_on=True, after_tp=True))                                 # no n
        self.bad(plan_be(after_atr=True, atr_length=14))                             # no value
        self.bad(plan_be(after_atr=True, atr_value=100.0))                           # no ATR length

    def test_old_plan_has_it_off(self):
        p = make_plan(ind_ma1={"entry": True})
        p.pop("be", None)
        H.validate_plan(p)
        self.assertFalse(H.upgrade_plan(p)["be"]["on"])
        self.assertIsNone(H.be_setup("LONG", 100.0, 2.0, H.complete_plan(p)))


class TestRule(unittest.TestCase):
    def test_setup_and_due(self):
        p = H.complete_plan(plan_be(tp_on=True, after_tp=True, tp_n=2, after_atr=True, atr_value=150.0, atr_length=14))
        be = H.be_setup("LONG", 100.0, 2.0, p)
        self.assertEqual(be, {"tp": 2, "trigger": 103.0, "done": False})
        self.assertFalse(H.be_due("LONG", be, 102.9, {1}))
        self.assertTrue(H.be_due("LONG", be, 103.0, set()))                 # After ATR first
        self.assertTrue(H.be_due("LONG", be, 101.0, {1, 2}))                # After TP 2 first
        s = H.be_setup("SHORT", 100.0, 2.0, p)
        self.assertEqual(s["trigger"], 97.0)
        self.assertTrue(H.be_due("SHORT", s, 96.5, set()))
        self.assertFalse(H.be_due("SHORT", s, 97.5, set()))
        be["done"] = True
        self.assertFalse(H.be_due("LONG", be, 200.0, {2}))

    def test_atr_not_ready(self):
        p = H.complete_plan(plan_be(after_atr=True, atr_value=100.0, atr_length=14))
        self.assertEqual(H.be_setup("LONG", 100.0, float("nan"), p)["trigger"], None)


class TestShadow(unittest.TestCase):
    def candle(self, book, s, plan):
        return B.on_candle(book, s, plan, tick_size=0.0, last_total=None, balance=1000.0, t_ms=s.open_time + 1)

    def test_after_tp1_moves_the_sl_to_entry_and_closes_there(self):
        plan = H.complete_plan(plan_be(tp_on=True, after_tp=True, tp_n=1))
        book = B.new_book(1000.0)
        self.candle(book, snap(1, dir_e=1, dir_f=1), plan)
        sl0 = book["levels"]["sl"]
        self.assertLess(sl0, 100.0)
        B.on_price(book, 101.0, plan, 10)
        self.assertEqual(book["levels"]["sl"], sl0)                        # TP1 not yet
        B.on_price(book, 102.0, plan, 20)                                  # TP1 filled
        self.assertAlmostEqual(book["levels"]["sl"], 100.0)
        closed = B.on_price(book, 100.0, plan, 30)
        self.assertEqual(closed[0]["exit_reason"], "TP1, Stop Loss")
        self.assertAlmostEqual(closed[0]["legs"][-1]["price"], 100.0)

    def test_after_atr(self):
        plan = H.complete_plan(plan_be(after_atr=True, atr_value=100.0, atr_length=14))
        book = B.new_book(1000.0)
        self.candle(book, snap(1, dir_e=-1, dir_f=-1, be_atr=2.0), plan)    # SHORT at 100, trigger 98
        B.on_price(book, 98.5, plan, 10)
        self.assertGreater(book["levels"]["sl"], 100.0)
        B.on_price(book, 98.0, plan, 20)
        self.assertAlmostEqual(book["levels"]["sl"], 100.0)
        B.on_price(book, 97.0, plan, 30)
        self.assertAlmostEqual(book["levels"]["sl"], 100.0)                # done once; stays

    def test_never_loosens(self):
        plan = H.complete_plan(plan_be(after_atr=True, atr_value=100.0, atr_length=14))
        book = B.new_book(1000.0)
        self.candle(book, snap(1, dir_e=1, dir_f=1, be_atr=2.0), plan)
        book["levels"]["sl"] = 101.0                                       # already above the entry
        B.on_price(book, 102.0, plan, 10)
        self.assertEqual(book["levels"]["sl"], 101.0)


class TestBot(T.Base):
    async def test_after_atr_moves_the_exchange_stop_to_entry(self):
        inst = self.make(plan_be(after_atr=True, atr_value=100.0, atr_length=14))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap(1000, dir_e=1, dir_f=1, be_atr=2.0))
        entry = inst.state.entry_price
        lad = inst.hub.ladder
        self.assertEqual(lad["be"]["trigger"], entry + 2.0)
        await inst._hub_ladder_check(entry + 1.0)
        self.assertFalse(inst.hub.ladder["be"]["done"])
        await inst._hub_ladder_check(entry + 2.0)
        self.assertTrue(inst.hub.ladder["be"]["done"])
        self.assertAlmostEqual(inst.hub.ladder["sl"], entry)
        self.assertAlmostEqual(inst.state.sl_price, entry)                 # the nearest level is on the exchange

    async def test_after_tp_waits_for_the_fill(self):
        inst = self.make(plan_be(tp_on=True, after_tp=True, tp_n=1))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap(1000, dir_e=1, dir_f=1))
        entry = inst.state.entry_price
        await inst._hub_ladder_check(entry + 5.0)
        self.assertFalse(inst.hub.ladder["be"]["done"])                   # TP1 not filled
        inst.hub.tp["levels"][0]["state"] = "filled"
        await inst._hub_ladder_check(entry + 1.0)
        self.assertAlmostEqual(inst.hub.ladder["sl"], entry)


if __name__ == "__main__":
    unittest.main()
