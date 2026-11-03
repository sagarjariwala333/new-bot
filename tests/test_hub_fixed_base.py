"""Strategy Hub: ENTRY SIZE method "Fixed base amount (grows with the last closed total)" and the MAX LOSS box
(basis: % of available balance / % of entry amount / fixed $). Arbitrary test numbers only.

Owner rules tested here:
  * the amount is margin in USDT; position = amount x leverage (leverage is its own box)
  * first trade = the base; after each FULL close the next entry = the total of that trade
    (its entry amount + every fill added up), never below the base
  * trend and counter-trend each have their own base; ONE running total for both
  * no cap, no reset
  * older plans (no method / no basis) behave exactly as before"""
import copy
import dataclasses
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_trading as T  # noqa: E402  (also sets up the stubs + a temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_hub_trading import snap_of  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import hub_state  # noqa: E402

FIXED = H.SIZE_FIXED
ALLOC = H.SIZE_ALLOC


def fixed_sizing(**over):
    return {"method": FIXED, "base_amount": 100.0, "leverage": 10, "stop_on": True, "stop_pct": 5.0,
            "stop_basis": H.STOP_ENTRY, **over}


def plan_of(**over):
    return make_plan(ind_ma1={"entry": True, "flip": True}, **over)


def snap_e(t, ema, close=100.0, **kw):
    return dataclasses.replace(snap_of(t, close=close, **kw), size_ema=ema)


class TestValidation(unittest.TestCase):
    def test_older_plan_is_valid_and_is_allocation_and_balance_basis(self):
        p = plan_of()
        self.assertNotIn("method", p["sizing"])
        self.assertNotIn("stop_basis", p["sizing"])
        H.validate_plan(p)
        c = H.complete_plan(p)
        self.assertEqual(c["sizing"]["method"], ALLOC)
        self.assertEqual(c["sizing"]["stop_basis"], H.STOP_BAL)
        self.assertFalse(H.uses_fixed_base(c))

    def test_fixed_needs_base_amount_but_not_allocation(self):
        sz = fixed_sizing()
        p = plan_of(sizing=sz)
        del p["sizing"]["alloc_pct"]
        H.validate_plan(p)
        c = H.complete_plan(p)
        self.assertTrue(H.uses_fixed_base(c))
        p2 = copy.deepcopy(p)
        del p2["sizing"]["base_amount"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p2)

    def test_allocation_method_still_needs_allocation(self):
        p = plan_of()
        del p["sizing"]["alloc_pct"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_base_amount_limits(self):
        for bad in (0, -5, 0.001, float("nan"), float("inf"), True, "100"):
            p = plan_of(sizing=fixed_sizing(base_amount=bad))
            with self.assertRaises(H.PlanError, msg=repr(bad)):
                H.validate_plan(p)
        H.validate_plan(plan_of(sizing=fixed_sizing(base_amount=0.01)))
        H.validate_plan(plan_of(sizing=fixed_sizing(base_amount=1_000_000)))   # no upper cap on the amount

    def test_unknown_method_is_refused(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_of(sizing={"method": "whatever"}))

    def test_counter_fixed_needs_counter_base_not_counter_allocation(self):
        ct = dict(on=True, ema_length=50, base_amount=50.0, leverage=2)
        H.validate_plan(plan_of(sizing=fixed_sizing(counter=ct)))
        for k in ("ema_length", "base_amount", "leverage"):
            c2 = dict(ct)
            del c2[k]
            with self.assertRaises(H.PlanError, msg=k):
                H.validate_plan(plan_of(sizing=fixed_sizing(counter=c2)))
        # owner 2026-10-25: 0 = no counter-trend entry at all (allowed); below 0 is refused
        H.validate_plan(plan_of(sizing=fixed_sizing(counter={**ct, "base_amount": 0})))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_of(sizing=fixed_sizing(counter={**ct, "base_amount": -1})))

    def test_counter_allocation_method_unchanged(self):
        ct = dict(on=True, ema_length=50, alloc_pct=5.0, leverage=2)
        H.validate_plan(plan_of(sizing={"counter": ct}))
        c2 = dict(ct)
        del c2["alloc_pct"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_of(sizing={"counter": c2}))

    def test_max_loss_bases(self):
        # % bases need a %, $ needs a $
        for basis in (H.STOP_BAL, H.STOP_ENTRY):
            H.validate_plan(plan_of(sizing={"stop_basis": basis, "stop_pct": 3.0}))
            p = plan_of(sizing={"stop_basis": basis})
            del p["sizing"]["stop_pct"]
            with self.assertRaises(H.PlanError):
                H.validate_plan(p)
            with self.assertRaises(H.PlanError):
                H.validate_plan(plan_of(sizing={"stop_basis": basis, "stop_pct": 101}))
        p = plan_of(sizing={"stop_basis": H.STOP_USD, "stop_usd": 7.5})
        del p["sizing"]["stop_pct"]
        H.validate_plan(p)      # a $ stop needs no %
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_of(sizing={"stop_basis": H.STOP_USD}))
        for bad in (0, -1, float("nan")):
            with self.assertRaises(H.PlanError, msg=repr(bad)):
                H.validate_plan(plan_of(sizing={"stop_basis": H.STOP_USD, "stop_usd": bad}))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_of(sizing={"stop_basis": "nonsense"}))

    def test_max_loss_off_needs_no_basis(self):
        H.validate_plan(plan_of(sizing={"stop_on": False}))


class TestOwnerExample(unittest.TestCase):
    """The owner's rule, step by step: base 100."""

    def test_example_100_120_100_110_110(self):
        plan = H.complete_plan(plan_of(sizing=fixed_sizing()))
        last = None
        amounts = []
        for result in (+20, -30, +10, 0):
            amount = H.fixed_entry_amount("single", plan, last)
            amounts.append(amount)
            last = amount + result            # total of the trade = entry amount + all fills added up
        self.assertEqual(amounts, [100.0, 120.0, 100.0, 110.0])
        self.assertEqual(last, 110.0)

    def test_never_below_base(self):
        plan = H.complete_plan(plan_of(sizing=fixed_sizing()))
        for last in (99.99, 0.0, -50.0):
            self.assertEqual(H.fixed_entry_amount("single", plan, last), 100.0)

    def test_first_trade_is_base(self):
        plan = H.complete_plan(plan_of(sizing=fixed_sizing()))
        self.assertEqual(H.fixed_entry_amount("single", plan, None), 100.0)
        self.assertEqual(H.fixed_entry_amount("single", plan, float("nan")), 100.0)

    def test_trend_and_counter_have_own_base_one_running_total(self):
        ct = dict(on=True, ema_length=50, base_amount=50.0, leverage=2)
        plan = H.complete_plan(plan_of(sizing=fixed_sizing(counter=ct)))
        self.assertEqual(H.fixed_entry_amount("trend", plan, None), 100.0)
        self.assertEqual(H.fixed_entry_amount("single", plan, None), 100.0)
        self.assertEqual(H.fixed_entry_amount("counter", plan, None), 50.0)
        # ONE running total: after a trade that closed at 130 both kinds build on 130 (each never below its own base)
        self.assertEqual(H.fixed_entry_amount("counter", plan, 130.0), 130.0)
        self.assertEqual(H.fixed_entry_amount("trend", plan, 130.0), 130.0)
        # a small total: counter still above its own (lower) base, trend back to its base
        self.assertEqual(H.fixed_entry_amount("counter", plan, 70.0), 70.0)
        self.assertEqual(H.fixed_entry_amount("trend", plan, 70.0), 100.0)

    def test_no_cap_by_the_bot(self):
        plan = H.complete_plan(plan_of(sizing=fixed_sizing()))
        self.assertEqual(H.fixed_entry_amount("single", plan, 5_000_000.0), 5_000_000.0)


class TestMaxLossMath(unittest.TestCase):
    def test_bases(self):
        bal = H.complete_plan(plan_of(sizing={"stop_basis": H.STOP_BAL, "stop_pct": 6.0}))
        ent = H.complete_plan(plan_of(sizing={"stop_basis": H.STOP_ENTRY, "stop_pct": 6.0}))
        usd = H.complete_plan(plan_of(sizing={"stop_basis": H.STOP_USD, "stop_usd": 7.5}))
        self.assertAlmostEqual(H.max_loss_usd(bal, available_balance=8000, margin=100), 480.0)
        self.assertAlmostEqual(H.max_loss_usd(ent, available_balance=8000, margin=100), 6.0)
        self.assertAlmostEqual(H.max_loss_usd(usd, available_balance=8000, margin=100), 7.5)

    def test_older_plan_is_the_old_formula(self):
        plan = H.complete_plan(plan_of())          # no basis saved
        loss = H.max_loss_usd(plan, available_balance=8000, margin=123)
        new = H.stop_price_from_usd("SHORT", 100.0, 40.0, loss)
        old = H.stop_price_from_loss("SHORT", 100.0, 8000, 40.0, 6.0)
        self.assertAlmostEqual(new, old)

    def test_stop_price_from_usd(self):
        self.assertAlmostEqual(H.stop_price_from_usd("LONG", 100.0, 10.0, 5.0), 99.5)
        self.assertAlmostEqual(H.stop_price_from_usd("SHORT", 100.0, 10.0, 5.0), 100.5)
        with self.assertRaises(ValueError):
            H.stop_price_from_usd("LONG", 100.0, 0.0, 5.0)


class TestPaperSize(unittest.TestCase):
    def test_allocation_unchanged(self):
        plan = H.complete_plan(plan_of())
        snap = snap_of(1, dir_e=1)
        alloc, lev, margin = H.paper_size("LONG", snap, plan, None, None)
        self.assertEqual((alloc, lev, margin), (10.0, 5.0, None))
        alloc, lev, margin = H.paper_size("LONG", snap, plan, None, 1000.0)
        self.assertAlmostEqual(margin, 100.0)
        # old formula
        self.assertAlmostEqual(H.paper_stop_fraction(plan, alloc, lev, margin), (6.0 / 100) / (10.0 / 100 * 5))

    def test_fixed_needs_the_balance(self):
        plan = H.complete_plan(plan_of(sizing=fixed_sizing()))
        snap = snap_of(1, dir_e=1)
        self.assertIsNone(H.paper_size("LONG", snap, plan, None, None))
        self.assertIsNone(H.paper_size("LONG", snap, plan, None, 0.0))
        alloc, lev, margin = H.paper_size("LONG", snap, plan, 150.0, 1000.0)
        self.assertEqual((alloc, lev, margin), (15.0, 10.0, 150.0))
        # % of entry amount: 5% of the margin on a 10x position = 0.5% of the price
        self.assertAlmostEqual(H.paper_stop_fraction(plan, alloc, lev, margin), 0.005)

    def test_usd_basis_fraction(self):
        plan = H.complete_plan(plan_of(sizing=fixed_sizing(stop_basis=H.STOP_USD, stop_usd=10.0)))
        # margin 100 at 10x = 1000 position; losing 10 = 1% of the price
        self.assertAlmostEqual(H.paper_stop_fraction(plan, 10.0, 10.0, 100.0), 0.01)
        self.assertIsNone(H.paper_stop_fraction(plan, 10.0, 10.0, None))

    def test_shadow_opens_with_fixed_amount_and_does_not_touch_the_total(self):
        from app import shadow_book as B
        plan = H.complete_plan(plan_of(sizing=fixed_sizing(),
                                       shadow=dict(on=True, trades=2, slippage_ticks=0, tracker_start=1000.0)))
        book = B.new_book(1000.0)
        ev, _ = B.on_candle(book, snap_of(1000, dir_e=1, dir_f=1, close=100.0), plan, tick_size=0.1,
                            last_total=130.0, balance=1300.0, t_ms=1)
        self.assertEqual(book["pos"], 1)
        self.assertAlmostEqual(book["alloc"], 10.0)       # 130 of 1300
        self.assertAlmostEqual(book["levels"]["ml"], 100.0 * (1 - 0.005))
        self.assertTrue(any("paper LONG opened" in e for e in ev))

    def test_shadow_skips_the_entry_without_a_balance(self):
        from app import shadow_book as B
        plan = H.complete_plan(plan_of(sizing=fixed_sizing(),
                                       shadow=dict(on=True, trades=2, slippage_ticks=0, tracker_start=1000.0)))
        book = B.new_book(1000.0)
        ev, _ = B.on_candle(book, snap_of(1000, dir_e=1, dir_f=1), plan, tick_size=0.1, last_total=None,
                            balance=None, t_ms=1)
        self.assertEqual(book["pos"], 0)
        self.assertTrue(any("skipped" in e for e in ev))


class TestFixedBaseTrading(T.Base):
    """The live loop with a fake exchange."""

    def setUp(self):
        super().setUp()
        # Own ledger folder: other test files wipe the shared data dir while the full suite runs.
        import tempfile
        import shutil
        import unittest.mock as _mock
        from pathlib import Path
        from app import ledger
        d = tempfile.mkdtemp(prefix="ledger_test_")
        p = _mock.patch.object(ledger, "LEDGER_DIR", Path(d))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, d, True)

    def fixed_inst(self, **sizing_over):
        return self.make(plan_of(sizing=fixed_sizing(**sizing_over)))

    async def test_first_entry_is_the_base_times_leverage(self):
        inst = self.fixed_inst()
        inst.client.available_balance = 5000.0
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        # margin 100 x 10x = 1000 position / price 100 = 10
        self.assertAlmostEqual(inst.client.market_orders[0][2], 10.0, places=3)
        self.assertEqual(inst.client.leverage_calls[-1], 10)
        self.assertEqual(inst.hub.trade_amount, 100.0)
        self.assertEqual(inst.hub.trade_pnl, 0.0)
        self.assertIsNone(inst.hub.last_total)

    async def test_allocation_method_does_not_use_the_running_total(self):
        inst = self.make(plan_of())
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        self.assertIsNone(inst.hub.trade_amount)
        inst.client.mark_price = 103.0
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1, close=103.0))
        self.assertIsNone(inst.hub.last_total)

    async def test_max_loss_percent_of_entry_amount(self):
        inst = self.fixed_inst(stop_basis=H.STOP_ENTRY, stop_pct=5.0)
        inst.client.available_balance = 5000.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        # margin 100, 5% = 5 USDT; qty 10 -> stop 0.5 below the entry. (Balance 5000 plays no part.)
        self.assertAlmostEqual(inst.client.stop_orders[0][2], 99.5, places=2)

    async def test_max_loss_percent_of_balance_still_works_with_fixed_amount(self):
        inst = self.fixed_inst(stop_basis=H.STOP_BAL, stop_pct=0.1)
        inst.client.available_balance = 5000.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        # 0.1% of 5000 = 5 USDT; qty 10 -> 0.5 below
        self.assertAlmostEqual(inst.client.stop_orders[0][2], 99.5, places=2)

    async def test_max_loss_fixed_dollar(self):
        inst = self.fixed_inst(stop_basis=H.STOP_USD, stop_usd=8.0)
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        self.assertAlmostEqual(inst.client.stop_orders[0][2], 100.0 - 0.8, places=2)

    async def test_allocation_method_stop_unchanged(self):
        # exactly the old test: balance 8000, 10% x5, stop 6% of balance
        inst = self.make(make_plan(ind_ma1={"entry": True, "flip": True},
                                   sizing={"alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_pct": 6.0}))
        self.go_long(inst)
        inst.client.available_balance = 8000.0
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=100.0))
        self.assertAlmostEqual(inst.client.market_orders[0][2], 40.0, places=3)
        self.assertAlmostEqual(inst.client.stop_orders[0][2], 112.0, places=2)

    async def test_the_owner_example_through_the_live_loop(self):
        """Base 100. Trade 1 closes +20 (flip) -> next entry 120. Trade 2 closes -30 -> next entry 100 (base)."""
        inst = self.fixed_inst(stop_on=False)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))          # LONG, margin 100, qty 10
        self.assertAlmostEqual(inst.client.market_orders[0][2], 10.0, places=3)
        inst.client.mark_price = 102.0                                                # +2 x 10 = +20
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1, close=102.0))        # flip: close, then SHORT
        # next entry is built on 120: qty = 120 x 10 / 102
        self.assertAlmostEqual(inst.client.market_orders[1][2], 120.0 * 10 / 102.0, places=2)
        self.assertEqual(inst.hub.trade_amount, 120.0)
        inst.client.mark_price = 104.0                                                # short loses 2 x qty
        qty2 = inst.client.market_orders[1][2]
        await self.tick(inst, snap_of(3000, dir_e=1, dir_f=1, close=104.0))          # flip: close, then LONG
        total2 = 120.0 - 2.0 * qty2                                                   # about 96.47 -> below the base
        self.assertLess(total2, 100.0)
        self.assertAlmostEqual(inst.client.market_orders[2][2], 100.0 * 10 / 104.0, places=2)   # back to the base
        self.assertEqual(inst.hub.trade_amount, 100.0)

    async def test_last_total_is_saved_and_survives_a_restart(self):
        # the flip closes the long (2026-10-25: and opens the new side, built on that total)
        inst = self.make(plan_of(sizing=fixed_sizing(stop_on=False)))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        inst.client.mark_price = 102.0
        await self.tick(inst, snap_of(2000, dir_e=0, dir_f=-1, close=102.0))
        self.assertAlmostEqual(inst.hub.last_total, 120.0, places=6)
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertAlmostEqual(inst.hub.trade_amount, 120.0, places=6)     # the new side uses the total
        self.assertEqual(inst.hub.trade_pnl, 0.0)
        reloaded = hub_state.load(inst.platform, inst.account_id, inst.symbol)     # what a restart reads
        self.assertAlmostEqual(reloaded.last_total, 120.0, places=6)

    async def test_all_fills_are_added_up(self):
        """TP1 + TP2 fills and the final close: the trade total is the entry amount + every fill."""
        inst = self.make(plan_of(sizing=fixed_sizing(stop_on=False)))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))      # qty 10 @ 100
        # two take-profit fills recorded by the bot (gross +6 and +8)
        await inst._record_ledger("LONG", 3.0, 102.0, 6.0, "TP1", include_entry_commission=False)
        await inst._record_ledger("LONG", 2.0, 104.0, 8.0, "TP2", include_entry_commission=False)
        self.assertAlmostEqual(inst.hub.trade_pnl, 14.0, places=6)
        # the rest (5 of qty) closes at 103 on the flip: +15
        inst.client.position = {"positionAmt": "5.0", "entryPrice": "100.0"}
        inst.state.qty = 5.0
        inst.client.mark_price = 103.0
        await self.tick(inst, snap_of(2000, dir_e=0, dir_f=-1, close=103.0))
        self.assertAlmostEqual(inst.hub.last_total, 100.0 + 6.0 + 8.0 + 15.0, places=6)
        self.assertAlmostEqual(inst.hub.trade_amount, 129.0, places=6)     # the new side (flip) uses the total

    async def test_fees_are_part_of_the_result(self):
        inst = self.make(plan_of(sizing=fixed_sizing(stop_on=False)))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        inst._commission_cache[inst.state.entry_order_id] = 0.5        # real commission of the entry
        inst.client.mark_price = 102.0
        await self.tick(inst, snap_of(2000, dir_e=0, dir_f=-1, close=102.0))
        self.assertAlmostEqual(inst.hub.last_total, 100.0 + 20.0 - 0.5, places=6)

    async def test_stop_loss_close_counts(self):
        """The max-loss stop fires between ticks: its result is the trade's result."""
        inst = self.fixed_inst(stop_basis=H.STOP_ENTRY, stop_pct=5.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))     # qty 10, stop 99.5
        # the stop was hit: position gone, price is at the stop
        inst.client.position = None
        inst.client.open_algo_orders.clear()
        inst.client.mark_price = 99.5
        await self.tick(inst, snap_of(2000, dir_e=0, dir_f=0, close=99.5))
        self.assertEqual(inst.state.status, "IDLE")
        # the loss is the max loss: about 5 USDT -> total about 95 -> the next entry is the base 100
        self.assertAlmostEqual(inst.hub.last_total, 100.0 - 5.0, places=1)
        self.assertEqual(H.fixed_entry_amount("single", inst._plan, inst.hub.last_total), 100.0)

    async def test_counter_trend_entry_uses_its_own_base(self):
        ct = dict(on=True, ema_length=50, base_amount=50.0, leverage=2)
        inst = self.make(plan_of(sizing=fixed_sizing(counter=ct, stop_on=False)))
        inst.client.mark_price = 100.0
        # chart close 100 BELOW the EMA 120 -> a LONG is AGAINST the trend
        await self.tick(inst, snap_e(1000, 120.0, dir_e=1, dir_f=1, close=100.0))
        self.assertAlmostEqual(inst.client.market_orders[0][2], 50.0 * 2 / 100.0, places=3)
        self.assertEqual(inst.client.leverage_calls[-1], 2)
        self.assertEqual(inst.hub.trade_amount, 50.0)

    async def test_trend_entry_uses_trend_base_and_leverage(self):
        ct = dict(on=True, ema_length=50, base_amount=50.0, leverage=2)
        inst = self.make(plan_of(sizing=fixed_sizing(counter=ct, stop_on=False)))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_e(1000, 80.0, dir_e=1, dir_f=1, close=100.0))     # above the EMA = with the trend
        self.assertAlmostEqual(inst.client.market_orders[0][2], 100.0 * 10 / 100.0, places=3)
        self.assertEqual(inst.hub.trade_amount, 100.0)

    async def test_a_trade_the_bot_did_not_open_never_changes_the_total(self):
        inst = self.fixed_inst()
        inst.hub.last_total = 130.0
        hub_state.save(inst.hub)
        inst._reset_position_state()                 # nothing was opened by the fixed method
        self.assertEqual(inst.hub.last_total, 130.0)

    async def test_paper_trades_do_not_change_the_total(self):
        inst = self.make(plan_of(sizing=fixed_sizing(stop_on=False),
                                 shadow=dict(on=True, trades=2, slippage_ticks=0, tracker_start=1000.0)))
        inst.hub.last_total = 130.0
        sh = inst.tracker.get_shadow()
        sh.active = True
        inst.tracker.set_shadow(sh)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(inst.client.market_orders, [])         # shadow: no real order
        self.assertEqual(inst.hub.last_total, 130.0)
        self.assertEqual(inst.hub.shadow_book["pos"], 1)        # but a paper trade was opened
        self.assertAlmostEqual(inst.hub.shadow_book["alloc"], 130.0 / 10000.0 * 100.0, places=6)

    async def test_hub_status_still_serialises(self):
        inst = self.fixed_inst()
        st = inst._hub_status()
        self.assertTrue(st["max_loss_on"])
        inst.to_status_dict()


if __name__ == "__main__":
    unittest.main()
