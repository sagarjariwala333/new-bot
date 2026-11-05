"""Two options added 2026-11-05 (owner: "Both"):
 1. Trailing Stop "Signal candle high / low +/- ATR": when it may take charge ("Tighter than the Stop Loss" = how it
    always worked, or "Only once in profit" = after a candle closes at least X % of the signal ATR beyond the entry).
 2. Stop Loss "Structure": how the level is found (lowest low / highest high of N candles = how it always worked,
    or a real swing point with N candles on each side, known only N candles after it formed).
Arbitrary test numbers - the product has no default plan."""
import copy
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_ladder import LadderBase, sl, ATR, PRICE  # noqa: E402  (stubs + temp data dir)
from tests.test_hub_trading import snap_of  # noqa: E402
from tests.test_hub_trail_candle import candle_trail, snap  # noqa: E402
from tests.test_hub_amend_open_trade import AmendBase  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import shadow_book as B  # noqa: E402


def profit_trail(profit=100.0, **kw):
    t = candle_trail(**kw)
    t.update(candle_start=H.TCS_PROFIT, candle_profit=profit)
    return t


def tplan(**tr):
    return make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": PRICE, "value": 20.0}, trail=profit_trail(**tr))


# ----------------------------------------------------------------------------------------------------- 1. the trail
class TestTrailStartRules(unittest.TestCase):
    def test_arm_price_is_entry_plus_or_minus_a_share_of_the_signal_atr(self):
        p = H.complete_plan(tplan(profit=100.0))
        self.assertEqual(H.candle_arm_price("LONG", 100.0, 2.0, p), 102.0)
        self.assertEqual(H.candle_arm_price("SHORT", 100.0, 2.0, p), 98.0)
        p = H.complete_plan(tplan(profit=50.0))
        self.assertEqual(H.candle_arm_price("LONG", 100.0, 2.0, p), 101.0)
        p = H.complete_plan(tplan(profit=250.0))
        self.assertEqual(H.candle_arm_price("SHORT", 100.0, 2.0, p), 95.0)

    def test_no_arm_price_without_an_atr_or_when_the_choice_is_not_profit(self):
        p = H.complete_plan(tplan())
        self.assertIsNone(H.candle_arm_price("LONG", 100.0, float("nan"), p))
        q = make_plan(ind_ma1={"entry": True}, trail=candle_trail())
        self.assertIsNone(H.candle_arm_price("LONG", 100.0, 2.0, H.complete_plan(q)))
        q["trail"]["candle_start"] = H.TCS_TIGHTER
        self.assertIsNone(H.candle_arm_price("LONG", 100.0, 2.0, H.complete_plan(q)))
        self.assertFalse(H.candle_start_profit(H.complete_plan(q)))
        other = make_plan(ind_ma1={"entry": True}, trail={"on": True, "type": H.TRAIL_LLHH, "length": 5,
                                                          "candle_start": H.TCS_PROFIT})
        self.assertFalse(H.candle_start_profit(H.complete_plan(other)))

    def test_armed_means_a_close_at_or_beyond_it(self):
        self.assertTrue(H.candle_armed("LONG", 102.0, 102.0))
        self.assertFalse(H.candle_armed("LONG", 101.99, 102.0))
        self.assertTrue(H.candle_armed("SHORT", 98.0, 98.0))
        self.assertFalse(H.candle_armed("SHORT", 98.01, 98.0))
        self.assertFalse(H.candle_armed("LONG", 110.0, None))
        self.assertFalse(H.candle_armed("LONG", float("nan"), 102.0))


class TestTrailStartValidation(unittest.TestCase):
    def test_old_plan_without_the_choice_is_valid_and_complete_plan_adds_no_choice(self):
        p = make_plan(ind_ma1={"entry": True}, trail=candle_trail())
        H.validate_plan(p)
        self.assertNotIn("candle_start", H.complete_plan(p)["trail"])

    def test_both_choices_are_valid(self):
        for c in H.TCS_CHOICES:
            tr = candle_trail()
            tr.update(candle_start=c, candle_profit=75.0)
            H.validate_plan(make_plan(ind_ma1={"entry": True}, trail=tr))

    def test_profit_choice_needs_its_number(self):
        for bad in (None, 0, -5, "x"):
            tr = profit_trail()
            tr["candle_profit"] = bad
            with self.assertRaises(H.PlanError):
                H.validate_plan(make_plan(ind_ma1={"entry": True}, trail=tr))
        tr = profit_trail()
        del tr["candle_profit"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan(ind_ma1={"entry": True}, trail=tr))

    def test_the_tighter_choice_needs_no_number(self):
        tr = candle_trail()
        tr["candle_start"] = H.TCS_TIGHTER
        H.validate_plan(make_plan(ind_ma1={"entry": True}, trail=tr))

    def test_unknown_choice_refused(self):
        tr = candle_trail()
        tr["candle_start"] = "whenever"
        with self.assertRaises(H.PlanError):
            H.validate_plan(make_plan(ind_ma1={"entry": True}, trail=tr))

    def test_the_inert_number_is_filled_but_does_nothing(self):
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, trail=candle_trail()))
        self.assertIn("candle_profit", p["trail"])
        self.assertFalse(H.candle_start_profit(p))


class TestTrailStartLive(LadderBase):
    def make_p(self, **tr):
        return self.make_l(trail=profit_trail(**tr))

    async def test_nothing_at_the_entry_it_waits_for_a_candle_to_close_in_profit(self):
        inst = self.make_p()
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        lad = inst.hub.ladder
        self.assertIsNone(lad["ts"])
        self.assertAlmostEqual(lad["tc_arm"], 102.0)
        self.assertAlmostEqual(lad["tc_dist"], 2.0)
        self.assertEqual(lad["ex"]["name"] if lad.get("ex") else "ml", "ml")      # the Max Loss still protects it
        saved = __import__("app.hub_state", fromlist=["x"]).load(inst.platform, inst.account_id, inst.symbol)
        self.assertAlmostEqual(saved.ladder["tc_arm"], 102.0)                     # survives a restart

    async def test_a_close_short_of_the_arm_price_changes_nothing_then_one_beyond_takes_charge_long(self):
        inst = self.make_p()
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        inst.client.mark_price = 101.5
        await self.tick(inst, snap(2000, close=101.5, low=100.0, high=102.5))      # high touched 102 but the CLOSE did not
        self.assertIsNone(inst.hub.ladder["ts"])
        inst.client.mark_price = 103.0
        await self.tick(inst, snap(3000, close=103.0, low=101.0, high=104.0))      # closes beyond 102: 101 - 2 = 99
        self.assertAlmostEqual(inst.hub.ladder["ts"], 99.0)
        self.assertEqual(inst.client.stop_limit_calls[-1][2], 99.0)

    async def test_once_in_charge_it_only_tightens_and_no_longer_needs_the_profit(self):
        inst = self.make_p()
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        inst.client.mark_price = 103.0
        await self.tick(inst, snap(2000, close=103.0, low=101.0, high=104.0))      # 99
        inst.client.mark_price = 101.0
        await self.tick(inst, snap(3000, close=101.0, low=100.5, high=102.0))      # close below the arm price: 98.5 is
        self.assertAlmostEqual(inst.hub.ladder["ts"], 99.0)                        # not tighter, the level stays
        inst.client.mark_price = 101.9
        await self.tick(inst, snap(4000, close=101.9, low=101.5, high=102.0))      # 99.5 is tighter, arm price not needed
        self.assertAlmostEqual(inst.hub.ladder["ts"], 99.5)
        self.assertEqual(inst.state.status, "IN_POSITION")

    async def test_short_is_mirrored(self):
        inst = self.make_p()
        inst.client.mark_price = 100.0
        await self.tick(inst, snap(1000, close=100.0, high=102.0, dir_=-1))
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertIsNone(inst.hub.ladder["ts"])
        self.assertAlmostEqual(inst.hub.ladder["tc_arm"], 98.0)
        inst.client.mark_price = 98.5
        await self.tick(inst, snap(2000, close=98.5, high=99.5, low=97.5, dir_=-1))
        self.assertIsNone(inst.hub.ladder["ts"])
        inst.client.mark_price = 97.0
        await self.tick(inst, snap(3000, close=97.0, high=99.0, low=96.0, dir_=-1))   # 99 + 2 = 101
        self.assertAlmostEqual(inst.hub.ladder["ts"], 101.0)

    async def test_the_profit_number_is_editable(self):
        inst = self.make_p(profit=50.0)                                            # 0.5 ATR = 1
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        self.assertAlmostEqual(inst.hub.ladder["tc_arm"], 101.0)

    async def test_no_atr_means_no_trailing_but_the_trade_opens(self):
        inst = self.make_p()
        await self.open_long_at(inst, ts_atr=float("nan"), low=98.0)
        self.assertEqual(inst.state.direction, "LONG")
        self.assertIsNone(inst.hub.ladder["ts"])
        self.assertNotIn("tc_arm", inst.hub.ladder)

    async def test_the_old_choice_still_takes_charge_at_the_entry(self):
        inst = self.make_l(trail=candle_trail())
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 96.0)
        self.assertNotIn("tc_arm", inst.hub.ladder)

    async def test_the_tighter_choice_written_out_is_the_same(self):
        tr = candle_trail()
        tr["candle_start"] = H.TCS_TIGHTER
        inst = self.make_l(trail=tr)
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 96.0)
        self.assertNotIn("tc_arm", inst.hub.ladder)

    async def test_stop_loss_stays_in_charge_until_the_trail_takes_over(self):
        inst = self.make_l(sl=sl(PRICE, 3.0), trail=profit_trail())                # SL 97
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        self.assertEqual(inst.hub.ladder["ex"]["name"], "sl")
        inst.client.mark_price = 101.0
        await self.tick(inst, snap(2000, close=101.0, low=99.0, high=101.5))       # not in profit enough: SL stays
        self.assertEqual(inst.hub.ladder["ex"]["name"], "sl")
        inst.client.mark_price = 104.0
        await self.tick(inst, snap(3000, close=104.0, low=102.0, high=105.0))      # 100: tighter than 97
        self.assertAlmostEqual(inst.hub.ladder["ts"], 100.0)
        self.assertEqual(inst.hub.ladder["ex"]["name"], "ts")


class TestTrailStartAmend(AmendBase):
    async def test_changing_to_only_once_in_profit_on_an_open_trade(self):
        inst = self.make_l(trail=candle_trail())                                    # ts 96 at the entry
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        self.patch_entry_snap(inst, ts_atr=2.0, low=98.0)
        self.remember(inst)
        new = self.new_plan(inst, trail=profit_trail())
        res = await inst.hub_amend_open_trade(new, "tighter")
        self.assertAlmostEqual(inst.hub.ladder["tc_arm"], 102.0)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 96.0)                        # tighter-only: the old level stays
        self.assertTrue(any("take charge" in a for a in res["applied"]), res)
        self.back_to_opened_rules(inst)
        res = await inst.hub_amend_open_trade(new, "looser")
        self.assertIsNone(inst.hub.ladder["ts"])                                   # looser allowed: waits for profit
        self.assertAlmostEqual(inst.hub.ladder["tc_arm"], 102.0)

    async def test_changing_back_to_the_old_choice_drops_the_arm_price(self):
        inst = self.make_l(trail=profit_trail())
        await self.open_long_at(inst, ts_atr=2.0, low=98.0)
        self.patch_entry_snap(inst, ts_atr=2.0, low=98.0)
        self.remember(inst)
        await inst.hub_amend_open_trade(self.new_plan(inst, trail=candle_trail()), "tighter")
        self.assertNotIn("tc_arm", inst.hub.ladder)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 96.0)


class TestTrailStartShadow(unittest.TestCase):
    def test_paper_trade_uses_the_same_rule(self):
        from tests.test_hub_sizing_shadow import TestShadowBook, SHADOW
        tb = TestShadowBook()
        pl = tb.plan(sl={"on": True, "basis": PRICE, "value": 20.0}, trail=profit_trail(),
                     shadow={**SHADOW, "slippage_ticks": 0})
        book = B.new_book(1000.0)
        tb.candle(book, snap(1, close=100.0, low=98.0), pl)
        self.assertEqual(book["pos"], 1)
        self.assertIsNone(book["levels"]["ts"])
        self.assertAlmostEqual(book["tc_arm"], 102.0)
        self.assertAlmostEqual(book["tc_dist"], 2.0)
        tb.candle(book, snap(2, close=101.5, low=100.0, high=102.5), pl)
        self.assertIsNone(book["levels"]["ts"])
        tb.candle(book, snap(3, close=104.0, low=102.0, high=105.0, ts_atr=7.0), pl)
        self.assertAlmostEqual(book["levels"]["ts"], 100.0)                         # 102 - 2 (the signal ATR)
        tb.candle(book, snap(4, close=103.0, low=99.0, high=105.0), pl)
        self.assertAlmostEqual(book["levels"]["ts"], 100.0)                         # never back

    def test_paper_short(self):
        from tests.test_hub_sizing_shadow import TestShadowBook, SHADOW
        tb = TestShadowBook()
        pl = tb.plan(sl={"on": True, "basis": PRICE, "value": 20.0}, trail=profit_trail(),
                     shadow={**SHADOW, "slippage_ticks": 0})
        book = B.new_book(1000.0)
        tb.candle(book, snap(1, close=100.0, high=102.0, dir_=-1), pl)
        self.assertEqual(book["pos"], -1)
        self.assertIsNone(book["levels"]["ts"])
        self.assertAlmostEqual(book["tc_arm"], 98.0)
        tb.candle(book, snap(2, close=97.0, high=99.0, low=96.0, dir_=-1), pl)
        self.assertAlmostEqual(book["levels"]["ts"], 101.0)


# ----------------------------------------------------------------------------------------------------- 2. the SL
def swing_frame(highs, lows, closes=None):
    n = len(highs)
    c = closes if closes is not None else [(a + b) / 2.0 for a, b in zip(highs, lows)]
    return pd.DataFrame({"open_time": np.arange(n) * 3600000, "open": c, "high": highs, "low": lows, "close": c,
                         "volume": np.ones(n) * 100.0})


def swing_plan(n=2, **sl_over):
    s = {"on": True, "basis": H.SL_STRUCT, "length": 3, "struct_mode": H.SLS_SWING, "struct_n": n}
    s.update(sl_over)
    return make_plan(ind_ma1={"entry": True}, sl=s)


class TestSwingStopRules(unittest.TestCase):
    # lows: a swing low of 90 at bar 3 (needs 2 higher lows each side), a swing low of 95 at bar 8
    LOWS = [100, 99, 98, 90, 98, 99, 100, 99, 95, 99, 100, 101, 102]
    HIGHS = [x + 4 for x in LOWS]

    def frame(self):
        return H.compute_hub(swing_frame(self.HIGHS, self.LOWS), swing_plan(2))

    def test_a_swing_is_known_only_n_candles_after_it_formed(self):
        f = self.frame()
        # the swing low at bar 3 is confirmed on bar 5 (N = 2)
        self.assertTrue(math.isnan(f["sl_piv_low"].iloc[4]))
        self.assertEqual(f["sl_piv_low"].iloc[5], 90.0)
        # the one at bar 8 is confirmed on bar 10; before that the older one is still the latest
        self.assertEqual(f["sl_piv_low"].iloc[9], 90.0)
        self.assertEqual(f["sl_piv_low"].iloc[10], 95.0)

    def test_a_swing_high_is_known_only_n_candles_after_it_formed(self):
        highs = [104, 103, 102, 110, 102, 103, 104, 103, 109, 103, 102, 101, 100]
        lows = [x - 4 for x in highs]
        cl = [95.0] * 13
        f = H.compute_hub(swing_frame(highs, lows, cl), swing_plan(2))
        self.assertTrue(math.isnan(f["sl_piv_high"].iloc[4]))
        self.assertEqual(f["sl_piv_high"].iloc[5], 110.0)
        self.assertEqual(f["sl_piv_high"].iloc[9], 110.0)
        self.assertEqual(f["sl_piv_high"].iloc[10], 109.0)

    def test_it_must_be_below_the_close_for_a_long_and_above_it_for_a_short(self):
        f = self.frame()
        close = f["close"].iloc[12]
        self.assertLess(f["sl_piv_low"].iloc[12], close)
        self.assertTrue(math.isnan(f["sl_piv_high"].iloc[12]) or f["sl_piv_high"].iloc[12] > close)
        # a swing low that is not below the close is skipped for the next older one
        lows = [100, 99, 98, 90, 98, 99, 100, 99, 105, 109, 110, 111, 112]
        highs = [x + 1 for x in lows]
        cl = [95.0] * 13
        cl[10] = 92.0
        f2 = H.compute_hub(swing_frame(highs, lows, cl), swing_plan(2))
        self.assertEqual(f2["sl_piv_low"].iloc[10], 90.0)

    def test_nothing_changes_when_later_candles_are_added(self):
        full = self.frame()
        for cut in (6, 9, 11):
            part = H.compute_hub(swing_frame(self.HIGHS[:cut], self.LOWS[:cut]), swing_plan(2))
            for col in ("sl_piv_low", "sl_piv_high"):
                self.assertTrue(np.allclose(part[col].values, full[col].values[:cut], equal_nan=True), (col, cut))

    def test_off_unless_chosen(self):
        p = make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": H.SL_STRUCT, "length": 3})
        self.assertNotIn("sl_piv_low", H.compute_hub(swing_frame(self.HIGHS, self.LOWS), p).columns)

    def test_stop_level_long_and_short(self):
        f = self.frame()
        p = H.complete_plan(swing_plan(2))
        s = H.hub_snapshot_at(f, 11)
        self.assertEqual(H.sl_level("LONG", f["close"].iloc[11], float("nan"), p, snap=s), 95.0)
        # a short uses the latest swing high above the close
        highs = [104, 103, 102, 110, 102, 103, 104, 103, 109, 103, 102, 101, 100]
        lows = [x - 4 for x in highs]
        f2 = H.compute_hub(swing_frame(highs, lows), swing_plan(2))
        s2 = H.hub_snapshot_at(f2, 12)
        self.assertEqual(H.sl_level("SHORT", f2["close"].iloc[12], float("nan"), p, snap=s2), 109.0)

    def test_the_old_choice_is_unchanged(self):
        f = H.compute_hub(swing_frame(self.HIGHS, self.LOWS),
                          make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": H.SL_STRUCT, "length": 3}))
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": H.SL_STRUCT, "length": 3}))
        s = H.hub_snapshot_at(f, 12)
        self.assertEqual(H.sl_level("LONG", 103.0, float("nan"), p, snap=s), min(self.LOWS[10:13]))

    def test_no_swing_yet_means_no_stop_level(self):
        f = self.frame()
        p = H.complete_plan(swing_plan(2))
        s = H.hub_snapshot_at(f, 2)
        self.assertIsNone(H.sl_level("LONG", f["close"].iloc[2], float("nan"), p, snap=s))

    def test_a_level_on_the_wrong_side_of_the_entry_is_not_used(self):
        f = self.frame()
        p = H.complete_plan(swing_plan(2))
        s = H.hub_snapshot_at(f, 11)
        self.assertIsNone(H.sl_level("LONG", 90.0, float("nan"), p, snap=s))


class TestSwingStopValidation(unittest.TestCase):
    def test_old_plan_valid_and_no_choice_added(self):
        p = make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": H.SL_STRUCT, "length": 5})
        H.validate_plan(p)
        self.assertNotIn("struct_mode", H.complete_plan(p)["sl"])

    def test_both_choices_valid(self):
        H.validate_plan(make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": H.SL_STRUCT, "length": 5,
                                                                "struct_mode": H.SLS_EXTREME}))
        H.validate_plan(swing_plan(3))

    def test_swing_needs_its_n_but_not_the_old_n(self):
        p = swing_plan(3)
        del p["sl"]["length"]
        H.validate_plan(p)
        for bad in (None, 0, -1, "x", 2.5):
            q = swing_plan(3)
            q["sl"]["struct_n"] = bad
            with self.assertRaises(H.PlanError):
                H.validate_plan(q)
        q = swing_plan(3)
        del q["sl"]["struct_n"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(q)

    def test_extreme_still_needs_the_old_n(self):
        p = make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": H.SL_STRUCT, "struct_mode": H.SLS_EXTREME})
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_unknown_choice_refused(self):
        p = swing_plan(3)
        p["sl"]["struct_mode"] = "pivot"
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_swing_choice_is_ignored_for_other_bases(self):
        p = make_plan(ind_ma1={"entry": True}, sl={"on": True, "basis": PRICE, "value": 3.0,
                                                    "struct_mode": H.SLS_SWING, "struct_n": 3})
        H.validate_plan(p)
        self.assertFalse(H.sl_swing(H.complete_plan(p)))


class TestSwingStopLive(LadderBase):
    async def test_the_swing_level_is_the_stop_at_the_entry(self):
        inst = self.make_l(sl={"on": True, "basis": H.SL_STRUCT, "length": 3, "struct_mode": H.SLS_SWING, "struct_n": 2})
        await self.open_long_at(inst, sl_piv_low=95.0, sl_low=90.0)
        self.assertAlmostEqual(inst.hub.ladder["sl"], 95.0)                         # the swing, not the N-candle low
        self.assertEqual(inst.client.stop_limit_calls[-1][2], 95.0)

    async def test_no_swing_found_the_trade_still_opens_with_the_other_stops(self):
        inst = self.make_l(sl={"on": True, "basis": H.SL_STRUCT, "length": 3, "struct_mode": H.SLS_SWING, "struct_n": 2})
        await self.open_long_at(inst)
        self.assertEqual(inst.state.direction, "LONG")
        self.assertIsNone(inst.hub.ladder["sl"])
        self.assertAlmostEqual(inst.hub.ladder["ml"], 88.0)

    async def test_the_old_choice_uses_the_n_candle_low(self):
        inst = self.make_l(sl={"on": True, "basis": H.SL_STRUCT, "length": 3})
        await self.open_long_at(inst, sl_piv_low=95.0, sl_low=90.0)
        self.assertAlmostEqual(inst.hub.ladder["sl"], 90.0)


class TestSwingStopShadow(unittest.TestCase):
    def test_paper_trade_uses_the_swing_level(self):
        from tests.test_hub_sizing_shadow import TestShadowBook, SHADOW
        tb = TestShadowBook()
        pl = tb.plan(sl={"on": True, "basis": H.SL_STRUCT, "length": 3, "struct_mode": H.SLS_SWING, "struct_n": 2},
                     shadow={**SHADOW, "slippage_ticks": 0})
        book = B.new_book(1000.0)
        s = snap(1, close=100.0)
        s.sl_piv_low, s.sl_low = 95.0, 90.0
        tb.candle(book, s, pl)
        self.assertEqual(book["pos"], 1)
        self.assertAlmostEqual(book["levels"]["sl"], 95.0)


class TestBothTogether(unittest.TestCase):
    """The owner's plan: HMA 9 / HMA 18, swing Stop Loss, candle +/- ATR trail only once in profit."""

    def test_the_plan_validates_and_runs_in_the_backtest(self):
        from app import hub_backtest as BT
        rng = np.random.default_rng(7)
        n = 900
        c = 100 + np.cumsum(rng.normal(0, 1, n))
        o = np.r_[c[0], c[:-1]]
        h = np.maximum(o, c) + rng.random(n) * 0.8
        l = np.minimum(o, c) - rng.random(n) * 0.8
        df = pd.DataFrame({"open_time": np.arange(n) * 3600000, "open": o, "high": h, "low": l, "close": c,
                           "volume": rng.random(n) * 100 + 50})
        p = make_plan(ind_ma1={"type": "HMA", "length": 9}, ind_ma2={"type": "HMA", "length": 18},
                      ind_mx={"entry": True, "flip": True, "mode": "MA 1 above / below MA 2", "n": 1},
                      sl={"on": True, "basis": H.SL_STRUCT, "struct_mode": H.SLS_SWING, "struct_n": 3},
                      trail=profit_trail())
        p = H.complete_plan(p)
        r = BT.run(df, p, 1000.0, 0.05, 0.02)
        self.assertGreater(len(r["trades"]), 10)
        reasons = {t["exit_reason"] for t in r["trades"]}
        self.assertIn("Trailing Stop", reasons)
        # the same plan with the old choice: its Trailing Stop takes charge at the entry, so it exits earlier / more often
        old = copy.deepcopy(p)
        old["trail"].pop("candle_start")
        r2 = BT.run(df, H.complete_plan(old), 1000.0, 0.05, 0.02)
        self.assertNotEqual([t["exit_reason"] for t in r["trades"]], [t["exit_reason"] for t in r2["trades"]])


if __name__ == "__main__":
    unittest.main()
