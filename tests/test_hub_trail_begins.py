"""Trailing Stop, Step, start "Begins at a price move" (owner 2026-10-24): "the first SL is 2atr.. upon touching
0.5 ATR it goes to BE.. THEN FROM THERE IT TRAILS 0.25". Arbitrary test numbers only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_ladder import LadderBase, sl, ATR, PRICE  # noqa: E402  (stubs + temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import shadow_book as B  # noqa: E402


def begins(begin=50.0, lock=0.0, every=25.0, basis=ATR, atr_length=14):
    d = {"on": True, "type": H.TRAIL_STEP, "basis": basis, "start": H.START_BEGIN, "begin_value": begin,
         "lock_value": lock, "gap": every}
    if basis == ATR:
        d["atr_length"] = atr_length
    return d


def plan(**tr):
    return make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": ATR, "value": 200.0, "atr_length": 14},
                     trail=begins(**tr))


class TestRule(unittest.TestCase):
    def test_owner_example_long(self):
        p = plan()
        st = H.step_setup("LONG", 100.0, 10.0, H.complete_plan(p), 80.0)
        self.assertEqual((st["begin"], st["first"], st["gap"]), (5.0, 100.0, 2.5))
        lvl = lambda best: H.step_level("LONG", 100.0, st["start"], st["gap"], best, st["begin"], st["first"])
        self.assertIsNone(lvl(104.99))                     # the Stop Loss is still in charge
        self.assertEqual(lvl(105.0), 100.0)                # breakeven
        self.assertEqual(lvl(107.4), 100.0)
        self.assertEqual(lvl(107.5), 102.5)
        self.assertEqual(lvl(110.0), 105.0)                # at 1 x ATR the stop is at +0.5 ATR
        self.assertEqual(lvl(112.5), 107.5)

    def test_short_is_mirrored(self):
        st = H.step_setup("SHORT", 100.0, 10.0, H.complete_plan(plan()), 120.0)
        lvl = lambda best: H.step_level("SHORT", 100.0, st["start"], st["gap"], best, st["begin"], st["first"])
        self.assertIsNone(lvl(95.01))
        self.assertEqual(lvl(95.0), 100.0)
        self.assertEqual(lvl(92.5), 97.5)

    def test_lock_in_profit_and_below_entry(self):
        st = H.step_setup("LONG", 100.0, 10.0, H.complete_plan(plan(lock=20.0)), None)
        self.assertEqual(H.step_level("LONG", 100.0, st["start"], st["gap"], 105.0, st["begin"], st["first"]), 102.0)
        st = H.step_setup("LONG", 100.0, 10.0, H.complete_plan(plan(lock=-10.0)), None)
        self.assertEqual(H.step_level("LONG", 100.0, st["start"], st["gap"], 105.0, st["begin"], st["first"]), 99.0)

    def test_percent_of_price(self):
        st = H.step_setup("LONG", 100.0, float("nan"), H.complete_plan(plan(begin=2.0, lock=0.0, every=1.0, basis=PRICE)), None)
        self.assertEqual((st["begin"], st["first"], st["gap"]), (2.0, 100.0, 1.0))

    def test_old_step_unchanged(self):
        self.assertEqual(H.step_level("LONG", 100.0, 98.0, 0.5, 101.0), 99.0)
        self.assertIsNone(H.step_level("LONG", 100.0, 98.0, 0.5, 100.4))


class TestValidation(unittest.TestCase):
    def test_ok(self):
        H.validate_plan(plan())

    def test_lock_must_be_lower_than_begin(self):
        with self.assertRaises(H.PlanError) as c:
            H.validate_plan(plan(lock=50.0))
        self.assertIn("Stop goes to", str(c.exception))

    def test_missing_boxes_refused(self):
        for k in ("begin_value", "lock_value", "gap"):
            p = plan()
            del p["trail"][k]
            with self.assertRaises(H.PlanError):
                H.validate_plan(p)


class TestLive(LadderBase):
    async def test_ladder_sl_then_breakeven_then_trail(self):
        inst = self.make_l(sl=sl(ATR, 200.0, atr_length=14), trail=begins())
        await self.open_long_at(inst, sl_atr=10.0, ts_atr=10.0)          # entry 100, SL 80
        self.assertEqual(inst.hub.ladder["sl"], 80.0)
        await inst._hub_ladder_check(104.0)
        self.assertIsNone(inst.hub.ladder["ts"])
        await inst._hub_ladder_check(105.0)
        self.assertEqual(inst.hub.ladder["ts"], 100.0)
        self.assertEqual(inst.client.stop_limit_calls[-1][2], 100.0)    # breakeven rests on the exchange
        await inst._hub_ladder_check(110.0)
        self.assertEqual(inst.hub.ladder["ts"], 105.0)
        await inst._hub_ladder_check(106.0)                              # never back
        self.assertEqual(inst.hub.ladder["ts"], 105.0)
        self.assertEqual(inst.hub.ladder["sl"], 80.0)                    # the Stop Loss stays behind it


class TestShadow(unittest.TestCase):
    def test_shadow_uses_the_same_rule(self):
        from tests.test_hub_sizing_shadow import TestShadowBook, SHADOW, sizing
        from tests.test_hub_trading import snap_of
        tb = TestShadowBook()
        pl = tb.plan(sl={"on": True, "basis": PRICE, "value": 20.0},
                     trail=begins(begin=5.0, lock=0.0, every=2.5, basis=PRICE), shadow={**SHADOW, "slippage_ticks": 0})
        book = B.new_book(1000.0)
        tb.candle(book, snap_of(1, dir_e=1, dir_f=1, close=100.0), pl)
        self.assertEqual(book["pos"], 1)
        self.assertAlmostEqual(book["levels"]["sl"], 80.0)
        B.on_price(book, 104.0, pl, 5000)
        self.assertIsNone(book["levels"]["ts"])
        B.on_price(book, 105.0, pl, 5000)
        self.assertAlmostEqual(book["levels"]["ts"], 100.0)
        B.on_price(book, 110.0, pl, 5000)
        self.assertAlmostEqual(book["levels"]["ts"], 105.0)
        closed = B.on_price(book, 104.9, pl, 6000)
        self.assertEqual(closed[0]["exit_price"], 105.0)

if __name__ == "__main__":
    unittest.main()
