"""Flip tab Yes / No (owner 2026-10-25): Same as entry / Same as hold rule / Same as entry wait; Yes = the flip
follows the main settings. Arbitrary test numbers only."""
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
from tests.test_hub_trading import Base, snap_of  # noqa: E402

DF = make_df(600)
YES = {"same_entry": True, "same_hold": True, "same_wait": True}


class TestRules(unittest.TestCase):
    def test_old_plan_keeps_its_flip_ticks(self):
        p = make_plan(ind_rsi=dict(entry=True), ind_ma1=dict(flip=True))
        p.pop("flip_rules", None)
        up = H.upgrade_plan(p)
        self.assertEqual(up["flip_rules"], {"same_entry": False, "same_hold": True, "same_wait": True})
        f = H.compute_hub(DF, p)
        np.testing.assert_array_equal(f["flip_long"].values, f["ma1_long"].values)

    def test_same_as_entry_uses_the_entry_signal(self):
        p = make_plan(ind_rsi=dict(entry=True), ind_ma1=dict(flip=True), flip_rules=dict(YES))
        f = H.compute_hub(DF, p)
        np.testing.assert_array_equal(f["dir_f"].values, f["dir_e"].values)

    def test_same_as_entry_needs_no_flip_settings(self):
        p = make_plan(ind_rsi=dict(entry=True), flip_rules=dict(YES), main_flip="ma2")
        del p["combine_flip"]
        H.validate_plan(p)                                   # FLIP settings are not used: not asked
        p2 = make_plan(ind_rsi=dict(entry=True), flip_rules=dict(YES, same_entry=False))
        del p2["combine_flip"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p2)

    def test_own_hold_and_wait(self):
        p = make_plan(ind_rsi=dict(entry=True), hold={"on": False},
                      flip_rules=dict(YES, same_hold=False, hold_on=True, hold_level=30.0, same_wait=False, wait=0),
                      entry_wait=3)
        H.validate_plan(p)
        cp = H.complete_plan(p)
        self.assertEqual(H.effective_hold(cp), {"on": True, "adx_level": 30.0})
        self.assertEqual(H.flip_wait(cp), 0)
        self.assertIn("adx", H._needed_blocks(cp))
        miss = make_plan(ind_rsi=dict(entry=True), flip_rules=dict(YES, same_wait=False))
        with self.assertRaises(H.PlanError):
            H.validate_plan(miss)
        miss2 = make_plan(ind_rsi=dict(entry=True), flip_rules=dict(YES, same_hold=False, hold_on=True))
        with self.assertRaises(H.PlanError):
            H.validate_plan(miss2)

    def test_same_hold_follows_the_hold_tab(self):
        cp = H.complete_plan(make_plan(ind_rsi=dict(entry=True), hold={"on": True, "adx_level": 22.0}, flip_rules=dict(YES)))
        self.assertEqual(H.effective_hold(cp)["adx_level"], 22.0)
        self.assertEqual(H.flip_wait(cp), cp["entry_wait"])

    def test_wait_machine_uses_the_flip_wait_for_a_flip(self):
        ws = H.WaitState()
        d = H.wait_step(ws, flipped_to=-1, reverse=True, flat=True, dir_e=-1, dir_f=-1, first_entry_ok=True,
                        entry_wait=3, flip_wait=0)
        self.assertEqual(d, -1)                                      # flip: instant
        ws = H.WaitState()
        d = H.wait_step(ws, flipped_to=0, reverse=True, flat=True, dir_e=1, dir_f=0, first_entry_ok=True,
                        entry_wait=3, flip_wait=0)
        self.assertEqual(d, 0)                                       # a fresh entry still waits 3


class TestOwnHoldInTheShadow(unittest.TestCase):
    def test_own_hold_holds_a_paper_flip(self):
        p = H.complete_plan(make_plan(ind_rsi=dict(entry=True, flip=True), hold={"on": False},
                                      flip_rules=dict(YES, same_hold=False, hold_on=True, hold_level=25.0),
                                      shadow={"on": True, "trades": 1, "slippage_ticks": 0, "tracker_start": 1000.0}))
        book = B.new_book(1000.0)
        B.on_candle(book, snap_of(1, dir_e=1, dir_f=1, close=100.0), p, tick_size=0.1, last_total=None, balance=1000.0, t_ms=1)
        B.on_candle(book, snap_of(2, dir_e=-1, dir_f=-1, close=110.0, adx=10.0), p, tick_size=0.1, last_total=None,
                    balance=1000.0, t_ms=2)
        self.assertEqual(book["pos"], 1)                            # held


class TestLiveBot(Base):
    async def test_own_wait_zero_flips_at_once_even_with_entry_wait(self):
        from tests.test_hub_trading import plan_with
        inst = self.make(plan_with(entry_wait=2, flip_rules=dict(YES, same_wait=False, wait=0)))
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(inst.state.direction, "SHORT")

    async def test_same_wait_waits_the_entry_wait(self):
        from tests.test_hub_trading import plan_with
        inst = self.make(plan_with(entry_wait=1, flip_rules=dict(YES)))
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(inst.state.status, "IDLE")                 # closed, waiting 1 candle
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1))
        self.assertEqual(inst.state.direction, "SHORT")


if __name__ == "__main__":
    unittest.main()
