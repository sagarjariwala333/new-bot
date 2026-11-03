"""Strategy Hub STOP LADDER and the 2026-10-23 owner rules (arbitrary test numbers only, NOT the owner's settings):
  * Stop Loss (% of ATR or % of price movement, from the entry price)
  * Trailing Stop types: Step (live), Lowest low / highest high, Moving average - only tightens
  * Take Profit distance in % of ATR or % of price movement (old ATR multiples are converted: 1.5 -> 150)
  * the ladder: the NEAREST level rests on the exchange (Stop Loss / Trailing Stop as stop-LIMIT with
    trigger = limit, Max Loss as stop-MARKET); the levels behind it are in memory and close at market when the
    live price goes through them while the trade is still open
  * "Allow Mid-Trend Re-entry" (OFF by default)"""
import copy
import math
import os
import sys
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_trading import Base, HubFake, snap_of, plan_with  # noqa: E402  (sets up stubs + data dir)
from tests.test_hub_tp_trail import TPBase  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from tests import _test_values as tv  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import hub_state, hub_api, ledger  # noqa: E402
from app.instance import BotInstance  # noqa: E402

PRICE = H.BASIS_PRICE
ATR = H.BASIS_ATR


def sl(basis=PRICE, value=3.0, atr_length=None):
    d = {"on": True, "basis": basis, "value": value}
    if atr_length is not None:
        d["atr_length"] = atr_length
    return d


def step(start=H.START_SL, gap=0.5, basis=PRICE, start_value=None, atr_length=None):
    d = {"on": True, "type": H.TRAIL_STEP, "basis": basis, "start": start, "gap": gap}
    if start_value is not None:
        d["start_value"] = start_value
    if atr_length is not None:
        d["atr_length"] = atr_length
    return d


# ======================================================================================== plan rules (pure)
class TestPlanUpgrade(unittest.TestCase):
    def test_old_plan_is_read_in_todays_format_and_still_valid(self):
        old = make_plan(ind_ma1=dict(entry=True))
        del old["sl"]                                                    # plans saved before the Stop Loss box
        old["tp"] = {"on": True, "atr_length": 14,
                     "levels": [{"on": True, "mult": 1.5, "pct": 50.0}, {"on": False, "mult": 3.0, "pct": 30.0},
                                {"on": False, "mult": 5.0, "pct": 20.0}]}
        snapshot = copy.deepcopy(old)
        self.assertIs(H.validate_plan(old), old)
        self.assertEqual(old, snapshot)                                  # the saved plan itself is not changed
        up = H.upgrade_plan(old)
        self.assertEqual(up["tp"]["basis"], ATR)
        self.assertEqual([lv["value"] for lv in up["tp"]["levels"]], [150.0, 300.0, 500.0])
        self.assertNotIn("mult", up["tp"]["levels"][0])
        self.assertEqual(up["sl"], {"on": False})
        self.assertEqual(up["trail"]["type"], H.TRAIL_LLHH)
        self.assertIs(up["allow_reentry"], False)                        # owner: NOT allowed by default

    def test_plan_view_shows_the_old_multiple_as_percent_of_atr(self):
        class PC:
            plan = make_plan(ind_ma1=dict(entry=True))
        PC.plan["tp"]["levels"][0]["mult"] = 2.0
        view = hub_api.plan_view(PC)
        self.assertEqual(view["plan"]["tp"]["levels"][0]["value"], 200.0)


class TestPlanValidation(unittest.TestCase):
    def p(self, **over):
        sl_ = over.pop("sl", {"on": False})          # the Stop Loss given here replaces the test plan's own
        d = make_plan(ind_ma1=dict(entry=True), **over)
        d["sl"] = sl_
        return d

    def test_stop_loss_needs_its_basis_and_value(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.p(sl={"on": True, "value": 3.0}))
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.p(sl={"on": True, "basis": PRICE}))
        H.validate_plan(self.p(sl=sl(PRICE, 3.0)))                       # price basis: no ATR length needed

    def test_stop_loss_atr_needs_an_atr_length(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.p(sl=sl(ATR, 200.0)))
        H.validate_plan(self.p(sl=sl(ATR, 200.0, atr_length=14)))

    def test_step_same_as_stop_loss_needs_a_stop_loss(self):
        with self.assertRaises(H.PlanError) as cm:
            H.validate_plan(self.p(trail=step()))
        self.assertIn("Same as Stop Loss", str(cm.exception))
        H.validate_plan(self.p(trail=step(), sl=sl()))
        H.validate_plan(self.p(trail=step(start=H.START_OWN, start_value=2.0)))
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.p(trail=step(start=H.START_OWN)))      # own value missing

    def test_step_needs_a_gap(self):
        t = step(start=H.START_OWN, start_value=2.0)
        del t["gap"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.p(trail=t))

    def test_moving_average_trailing_needs_the_ma_numbers_even_if_not_ticked(self):
        p = make_plan(ind_rsi=dict(entry=True), trail={"on": True, "type": H.TRAIL_MA, "ma": "MA 2"})
        for k in ("type", "length", "mode"):
            p["ind"]["ma2"].pop(k)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p2 = make_plan(ind_rsi=dict(entry=True), trail={"on": True, "type": H.TRAIL_MA, "ma": "MA 2"})
        H.validate_plan(p2)

    def test_new_take_profit_must_name_its_basis(self):
        p = self.p(tp={"on": True, "atr_length": 14,
                       "levels": [{"on": True, "value": 150.0, "pct": 50.0}, {"on": False}, {"on": False}]})
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p["tp"]["basis"] = PRICE
        del p["tp"]["atr_length"]
        H.validate_plan(p)                                               # price basis: no ATR length needed

    def test_allow_reentry_must_be_on_or_off(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.p(allow_reentry="yes"))
        H.validate_plan(self.p(allow_reentry=True))


class TestLadderMaths(unittest.TestCase):
    def test_distance(self):
        self.assertAlmostEqual(H.distance(ATR, 200, 100.0, 5.0), 10.0)      # 200 = 2 x ATR
        self.assertAlmostEqual(H.distance(ATR, 75, 100.0, 4.0), 3.0)        # 75 = 0.75 x ATR
        self.assertAlmostEqual(H.distance(PRICE, 3, 100.0, float("nan")), 3.0)    # 3 = 3% of the entry price
        self.assertAlmostEqual(H.distance(PRICE, 0.5, 200.0, None), 1.0)    # 0.5 = 0.5% (not 50)
        self.assertIsNone(H.distance(ATR, 100, 100.0, float("nan")))        # no ATR yet

    def test_stop_loss_level_long_and_short(self):
        p = H.upgrade_plan(make_plan(ind_ma1=dict(entry=True), sl=sl(PRICE, 3.0)))
        self.assertAlmostEqual(H.sl_level("LONG", 100.0, None, p), 97.0)
        self.assertAlmostEqual(H.sl_level("SHORT", 100.0, None, p), 103.0)

    def test_owner_step_table_in_atr(self):
        # Owner's table: start 200% of ATR at a loss, gap 50% of ATR (ATR = 1, entry 100)
        start, gap = 100.0 - 2.0, 0.5
        rows = [(100.0, None), (100.5, 98.5), (101.0, 99.0), (101.5, 99.5), (102.0, 100.0), (102.5, 100.5)]
        for best, want in rows:
            got = H.step_level("LONG", 100.0, start, gap, best)
            if want is None:
                self.assertIsNone(got)
            else:
                self.assertAlmostEqual(got, want)

    def test_owner_step_example_in_price_percent(self):
        p = H.upgrade_plan(make_plan(ind_ma1=dict(entry=True), trail=step(start=H.START_OWN, start_value=2.0, gap=0.5)))
        st = H.step_setup("LONG", 100.0, None, p, None)
        self.assertAlmostEqual(st["start"], 98.0)
        self.assertAlmostEqual(st["gap"], 0.5)
        for best, want in [(100.5, 98.5), (101.0, 99.0), (102.0, 100.0), (102.5, 100.5)]:
            self.assertAlmostEqual(H.step_level("LONG", 100.0, st["start"], st["gap"], best), want)
        # a small gap of 20% of ATR = 0.2 ATR
        self.assertAlmostEqual(H.step_level("LONG", 100.0, 98.0, 0.2, 100.6), 98.6)

    def test_step_short_mirrors_long(self):
        self.assertAlmostEqual(H.step_level("SHORT", 100.0, 102.0, 0.5, 99.0), 101.0)
        self.assertIsNone(H.step_level("SHORT", 100.0, 102.0, 0.5, 99.8))

    def test_take_profit_in_price_percent(self):
        p = H.upgrade_plan(make_plan(ind_ma1=dict(entry=True)))
        p["tp"] = {"on": True, "basis": PRICE,
                   "levels": [{"on": True, "value": 3.0, "pct": 30.0}, {"on": True, "value": 5.0, "pct": 30.0},
                              {"on": True, "value": 8.0, "pct": 30.0}]}
        self.assertEqual([round(x["price"], 6) for x in H.tp_prices("LONG", 100.0, float("nan"), p)], [103.0, 105.0, 108.0])
        self.assertEqual([round(x["price"], 6) for x in H.tp_prices("SHORT", 100.0, float("nan"), p)], [97.0, 95.0, 92.0])

    def test_nearest_level_and_passed(self):
        self.assertEqual(H.nearest_level("LONG", {"ml": 88.0, "sl": 97.0, "ts": None}), ("sl", 97.0))
        self.assertEqual(H.nearest_level("LONG", {"ml": 98.0, "sl": 97.0, "ts": None}), ("ml", 98.0))
        self.assertEqual(H.nearest_level("SHORT", {"ml": 112.0, "sl": 103.0, "ts": 101.0}), ("ts", 101.0))
        self.assertEqual(H.nearest_level("LONG", {"ml": 97.0, "sl": 97.0, "ts": None})[0], "sl")   # tie: Stop Loss
        self.assertEqual(H.nearest_level("LONG", {"ml": None, "sl": None, "ts": None}), (None, None))
        self.assertTrue(H.passed("LONG", 97.0, 96.9))
        self.assertFalse(H.passed("LONG", 97.0, 97.0))           # touching is the exchange stop's job
        self.assertTrue(H.passed("SHORT", 103.0, 103.1))


# ======================================================================================== the bot
class SpyFake(HubFake):
    def __init__(self):
        super().__init__()
        self.stops_resting_at_cancel = []
        self.regular_cancels = []

    async def cancel_algo_order(self, algo_id):
        self.stops_resting_at_cancel.append(len(self.open_algo_orders))
        return await super().cancel_algo_order(algo_id)

    async def cancel_regular_order(self, symbol, order_id):
        self.regular_cancels.append(order_id)
        return await super().cancel_regular_order(symbol, order_id)


class LadderBase(TPBase):
    def make_l(self, **over):
        inst = self.make(plan_with(**over))
        inst.client = SpyFake()
        inst.mark_feed.last_message_at = time.time()
        return inst

    async def enter_long(self, inst, price=100.0, t=1000, **snap_over):
        inst.client.mark_price = price
        s = snap_of(t, dir_e=1, dir_f=1, close=price)
        for k, v in snap_over.items():
            setattr(s, k, v)
        await self.tick(inst, s)
        return s

    async def open_long_at(self, inst, price=100.0, t=1000, **snap_over):
        s = await self.enter_long(inst, price, t, **snap_over)
        self.assertEqual(inst.state.direction, "LONG")
        return s


class TestLadderAtEntry(LadderBase):
    async def test_stop_loss_nearer_than_max_loss_rests_as_stop_limit(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))                           # SL 97, Max Loss 6% of 10000 / 50 -> 88
        await self.open_long_at(inst)
        c = inst.client
        self.assertEqual(c.stop_orders, [])                             # no stop-market
        self.assertEqual(len(c.stop_limit_calls), 1)
        sym, side, trig, lim, qty = c.stop_limit_calls[0]
        self.assertEqual((side, trig, lim, qty), ("SELL", 97.0, 97.0, 50.0))   # trigger = limit, full quantity
        lad = inst.hub.ladder
        self.assertAlmostEqual(lad["ml"], 88.0)
        self.assertAlmostEqual(lad["sl"], 97.0)
        self.assertEqual(lad["ex"]["name"], "sl")
        self.assertEqual(inst.state.sl_order_id, c.open_algo_orders[0]["algoId"])
        saved = hub_state.load(inst.platform, inst.account_id, inst.symbol)
        self.assertAlmostEqual(saved.ladder["ml"], 88.0)                 # memory survives a restart

    async def test_stop_loss_from_atr_percent(self):
        inst = self.make_l(sl=sl(ATR, 200.0, atr_length=14))
        await self.open_long_at(inst, sl_atr=2.5)                       # 200% of ATR 2.5 = 5 -> 95
        self.assertEqual(inst.client.stop_limit_calls[0][2], 95.0)

    async def test_max_loss_nearer_than_the_stop_loss_rests_as_stop_market(self):
        inst = self.make_l(sl=sl(PRICE, 15.0))                          # SL 85 is farther than Max Loss 88
        await self.open_long_at(inst)
        self.assertEqual(inst.client.stop_limit_calls, [])
        self.assertEqual(len(inst.client.stop_orders), 1)
        self.assertEqual(inst.client.stop_orders[0][2], 88.0)
        self.assertEqual(inst.hub.ladder["ex"]["name"], "ml")

    async def test_old_plan_works_as_before_max_loss_stop_market_only(self):
        inst = self.make_l()
        await self.open_long_at(inst)
        self.assertEqual(len(inst.client.stop_orders), 1)
        self.assertEqual(inst.client.stop_orders[0][2], 88.0)
        self.assertEqual(inst.client.stop_limit_calls, [])

    async def test_refused_stop_limit_puts_the_max_loss_stop_market_on_instead(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        inst.client.fail_stop_limit_order = True
        await self.open_long_at(inst)
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(len(inst.client.stop_orders), 1)
        self.assertEqual(inst.client.stop_orders[0][2], 88.0)
        self.assertEqual(inst.hub.ladder["ex"]["name"], "ml")
        self.assertAlmostEqual(inst.hub.ladder["sl"], 97.0)              # the Stop Loss stays in memory

    async def test_refused_stop_limit_without_max_loss_never_leaves_a_naked_position(self):
        inst = self.make_l(sl=sl(PRICE, 3.0),
                           sizing={"alloc_pct": 10.0, "leverage": 5, "stop_on": False, "stop_pct": 6.0})
        inst.client.fail_stop_limit_order = True
        with mock.patch("app.hub_trading.asyncio.sleep", new=mock.AsyncMock()):
            await self.enter_long(inst)
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(len(inst.client.close_orders), 1)              # flattened, never left unprotected
        self.assertEqual(inst.state.status, "IDLE")

    async def test_stop_level_already_crossed_at_entry_closes(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        orig = inst.client.market_order

        async def entry_then_drop(symbol, side, quantity):
            r = await orig(symbol, side, quantity)
            inst.client.mark_price = 96.0                               # below the 97 Stop Loss before it rests
            return r
        inst.client.market_order = entry_then_drop
        await self.enter_long(inst)
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertIn("stop_crossed_at_entry", [r.reason for r in self.records])


class TestMemoryLevels(LadderBase):
    async def test_price_through_the_max_loss_while_the_stop_limit_did_not_fill_closes_at_market(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        inst.client.mark_price = 87.5                                    # through the stop-limit 97 AND Max Loss 88
        closed = await inst._hub_ladder_check(87.5)
        self.assertTrue(closed)
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertIn("max_loss_backup", [r.reason for r in self.records])
        self.assertEqual(inst.state.status, "IDLE")
        self.assertIsNone(inst.hub.ladder)

    async def test_price_between_the_exchange_stop_and_the_max_loss_waits(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        self.assertFalse(await inst._hub_ladder_check(96.0))            # the stop-limit's job: the bot waits
        self.assertEqual(inst.client.close_orders, [])

    async def test_triggered_stop_limit_leaves_a_limit_order_that_is_cancelled_at_the_close(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        algo = inst.state.sl_order_id
        inst.client.open_algo_orders = [o for o in inst.client.open_algo_orders if o["algoId"] != algo]   # triggered
        await inst._on_algo_update({"X": "TRIGGERED", "aid": algo, "ai": "", "ap": "0"})
        self.assertTrue(inst.hub.ladder["ex"]["triggered"])
        self.assertEqual(inst.state.status, "IN_POSITION")             # not filled: the trade is still open
        self.assertFalse(await inst._hub_ladder_check(96.5))            # no new stop on top of the working one
        self.assertEqual(len(inst.client.stop_limit_calls), 1)
        await inst._hub_ladder_check(87.0)                              # then the Max Loss in memory
        self.assertIn("max_loss_backup", [r.reason for r in self.records])
        self.assertEqual(len(inst.client.regular_cancels), 1)          # the child limit order is cancelled

    async def test_live_price_comes_from_the_feed_only_when_fresh(self):
        inst = self.make_l()
        inst.mark_feed.last_mark_price = 101.5
        inst.mark_feed.last_message_at = time.time()
        self.assertEqual(inst._hub_live_price(), 101.5)
        inst.mark_feed.last_message_at = time.time() - 10_000
        self.assertIsNone(inst._hub_live_price())


class TestStepTrailing(LadderBase):
    async def test_step_takes_charge_and_moves_live_new_stop_first(self):
        inst = self.make_l(sl=sl(PRICE, 3.0), trail=step(start=H.START_SL, gap=0.5))
        await self.open_long_at(inst)                                    # SL 97 on the exchange
        c = inst.client
        self.assertFalse(await inst._hub_ladder_check(100.4))           # before the first checkpoint
        self.assertIsNone(inst.hub.ladder["ts"])
        await inst._hub_ladder_check(100.5)                             # first checkpoint: 97 + 0.5
        self.assertAlmostEqual(inst.hub.ladder["ts"], 97.5)
        self.assertEqual(c.stop_limit_calls[-1][2], 97.5)
        self.assertEqual(inst.hub.ladder["ex"]["name"], "ts")
        self.assertEqual(c.stops_resting_at_cancel, [2])               # the new stop rested before the old went
        await inst._hub_ladder_check(101.6)                             # 3 gaps: 97 + 1.5
        self.assertAlmostEqual(inst.hub.ladder["ts"], 98.5)
        await inst._hub_ladder_check(100.2)                             # price back: the stop never loosens
        self.assertAlmostEqual(inst.hub.ladder["ts"], 98.5)
        self.assertAlmostEqual(inst.hub.ladder["sl"], 97.0)             # the Stop Loss stays in play in memory

    async def test_price_missed_the_trailing_stop_then_the_stop_loss_in_memory_closes(self):
        inst = self.make_l(sl=sl(PRICE, 3.0), trail=step(start=H.START_SL, gap=0.5))
        await self.open_long_at(inst)
        await inst._hub_ladder_check(101.0)                             # TS 98 on the exchange
        self.assertTrue(await inst._hub_ladder_check(96.9))             # missed 98, through the Stop Loss 97
        self.assertIn("stop_loss_backup", [r.reason for r in self.records])

    async def test_own_start_value(self):
        inst = self.make_l(trail=step(start=H.START_OWN, start_value=2.0, gap=0.5))
        await self.open_long_at(inst)                                    # Max Loss 88 rests, TS not in charge
        self.assertEqual(inst.client.stop_orders[0][2], 88.0)
        await inst._hub_ladder_check(101.0)                             # 98 + 2 x 0.5 = 99
        self.assertEqual(inst.client.stop_limit_calls[-1][2], 99.0)

    async def test_step_in_atr(self):
        inst = self.make_l(sl=sl(ATR, 200.0, atr_length=14), trail=step(start=H.START_SL, gap=50.0, basis=ATR,
                                                                      atr_length=14))
        await self.open_long_at(inst, sl_atr=1.0, ts_atr=1.0)           # SL 98, gap 0.5
        await inst._hub_ladder_check(102.5)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 100.5)             # owner table: +250% -> +50% in profit

    async def test_live_wait_between_ticks_moves_the_step(self):
        inst = self.make_l(sl=sl(PRICE, 3.0), trail=step(start=H.START_SL, gap=0.5))
        await self.open_long_at(inst)
        inst.mark_feed.last_mark_price = 101.0
        clock = {"t": 0.0}

        class _FakeTime:
            def monotonic(self):
                return clock["t"]

            def __getattr__(self, name):            # everything else is the real time module
                return getattr(time, name)
        FakeTime = _FakeTime()

        async def fake_sleep(sec):
            clock["t"] += sec
        with mock.patch("app.instance.time", FakeTime), mock.patch("app.instance.asyncio.sleep", new=fake_sleep):
            await inst._sleep_until_next_tick()
        self.assertAlmostEqual(inst.hub.ladder["ts"], 98.0)
        self.assertEqual(inst.client.stop_limit_calls[-1][2], 98.0)


class TestCandleCloseTrailing(LadderBase):
    async def test_moving_average_takes_over_only_on_the_safe_side(self):
        inst = self.make_l(sl=sl(PRICE, 3.0), trail={"on": True, "type": H.TRAIL_MA, "ma": "MA 1"})
        await self.open_long_at(inst)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.ma1 = 102.0                                                    # MA above the price: no take-over
        inst.client.mark_price = 101.0
        await self.tick(inst, s)
        self.assertIsNone(inst.hub.ladder["ts"])
        self.assertEqual(inst.state.status, "IN_POSITION")
        s2 = snap_of(3000, dir_e=1, dir_f=1, close=103.0)
        s2.ma1 = 99.0                                                    # below the price, tighter than SL 97
        inst.client.mark_price = 103.0
        await self.tick(inst, s2)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 99.0)
        self.assertEqual(inst.client.stop_limit_calls[-1][2], 99.0)

    async def test_lowest_low_looser_than_the_stop_loss_does_not_take_charge(self):
        inst = self.make_l(sl=sl(PRICE, 3.0), trail={"on": True, "type": H.TRAIL_LLHH, "length": 10})
        await self.open_long_at(inst)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.trail_low = 96.0                                               # looser than SL 97
        inst.client.mark_price = 101.0
        await self.tick(inst, s)
        self.assertIsNone(inst.hub.ladder["ts"])
        s2 = snap_of(3000, dir_e=1, dir_f=1, close=102.0)
        s2.trail_low = 98.0
        inst.client.mark_price = 102.0
        await self.tick(inst, s2)
        self.assertAlmostEqual(inst.hub.ladder["ts"], 98.0)


class TestQuantityAfterTakeProfit(LadderBase):
    async def test_stop_limit_is_put_back_with_the_remaining_quantity(self):
        tp = {"on": True, "basis": PRICE, "levels": [{"on": True, "value": 2.0, "pct": 33.0},
                                                    {"on": False}, {"on": False}]}
        inst = self.make_l(sl=sl(PRICE, 3.0), tp=tp)
        await self.open_long_at(inst)
        c = inst.client
        self.assertEqual(c.stop_limit_calls[-1][4], 50.0)
        self.assertEqual(c.limit_calls[0][3], 102.0)                    # TP in % of price movement
        self.fill(inst, inst.hub.tp["levels"][0]["order_id"])            # 16.5 closed by TP1
        inst.cur = snap_of(1000, dir_e=1, dir_f=1)
        await inst._tick_hub()
        self.assertEqual(c.stop_limit_calls[-1][4], 33.5)               # the remaining 33.5
        self.assertEqual(len(c.open_algo_orders), 1)                    # the old stop-limit was cancelled


class TestMidTrendReentry(LadderBase):
    async def stop_out_long(self, inst):
        await self.open_long_at(inst)
        inst.client.position = None                                      # the stop closed it on the exchange
        inst.cur = snap_of(1000, dir_e=1, dir_f=1)
        await inst._tick_hub()
        self.assertEqual(inst.state.status, "IDLE")

    async def test_not_allowed_by_default_waits_for_the_signal_to_end(self):
        inst = self.make_l()
        await self.stop_out_long(inst)
        await self.tick(inst, snap_of(2000, dir_e=1, dir_f=1))           # same signal still on: no re-entry
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertIn("mid-trend", inst.state.last_decision)
        await self.tick(inst, snap_of(3000, dir_e=0, dir_f=0))           # the signal ended
        self.assertEqual(inst.hub.reentry_block, 0)
        await self.tick(inst, snap_of(4000, dir_e=1, dir_f=1))           # a new signal: entry
        self.assertEqual(len(inst.client.market_orders), 2)

    async def test_the_other_side_is_never_blocked(self):
        inst = self.make_l()
        await self.stop_out_long(inst)
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1))
        self.assertEqual(inst.state.direction, "SHORT")

    async def test_allowed_re_enters_at_once(self):
        inst = self.make_l(allow_reentry=True)
        await self.stop_out_long(inst)
        await self.tick(inst, snap_of(2000, dir_e=1, dir_f=1))
        self.assertEqual(len(inst.client.market_orders), 2)

    async def test_an_old_close_only_plan_now_flips(self):
        # 2026-10-25: "Close only" removed - the flip signal IS the new entry signal
        inst = self.make_l(flip_action="Close only (go flat)")
        await self.open_long_at(inst)
        await self.tick(inst, snap_of(2000, dir_e=1, dir_f=-1, close=99.0))
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertEqual(len(inst.client.market_orders), 2)

    async def test_reverse_flip_still_opens_the_opposite_side(self):
        inst = self.make_l()
        await self.open_long_at(inst)
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1, close=99.0))
        self.assertEqual(inst.state.direction, "SHORT")


class TestRestart(LadderBase):
    async def test_restart_keeps_the_stop_limit_and_cancels_an_extra_stop(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        keep = inst.state.sl_order_id
        inst.client.open_algo_orders.append({"algoId": 1, "orderType": "STOP_MARKET", "triggerPrice": "88",
                                             "clientAlgoId": "hullbot_fake_sl", "algoStatus": "NEW"})
        pc = tv.pair_config(symbol="BTCUSDT", enabled=True, plan=plan_with(sl=sl(PRICE, 3.0)))
        inst2 = BotInstance(account_id=inst.account_id, account_name="Main", symbol="BTCUSDT",
                            api_key="k", api_secret="s", testnet=True, pair_config=pc)
        inst2.client = inst.client
        self.assertAlmostEqual(inst2.hub.ladder["sl"], 97.0)
        sid, sp = await inst2._hub_reconcile_stops()
        self.assertEqual(sid, keep)
        self.assertEqual(sp, 97.0)
        self.assertEqual([o["algoId"] for o in inst.client.open_algo_orders], [keep])


if __name__ == "__main__":
    unittest.main()
