"""Strategy Hub: trend / counter-trend sizing (ENTRY SIZE box) and Shadow mode (paper trades after a new
tracker high). Arbitrary test numbers only. Both are OFF unless ticked, and absent from an older plan = OFF."""
import copy
import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_trading as T  # noqa: E402  (also sets up the stubs + a temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_hub_trading import snap_of  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import strategy as strat  # noqa: E402
import numpy as np  # noqa: E402

COUNTER = dict(on=True, ema_length=50, alloc_pct=5.0, leverage=2)
SHADOW = dict(on=True, trades=2, slippage_ticks=10, tracker_start=1000.0)


def sizing(**over):
    return {"alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_pct": 6.0, **over}


def plan_of(**over):
    return make_plan(ind_ma1={"entry": True, "flip": True}, **over)


def snap_e(t, ema, close=100.0, **kw):
    return dataclasses.replace(snap_of(t, close=close, **kw), size_ema=ema)


class TestValidation(unittest.TestCase):
    def test_older_plan_without_the_new_boxes_is_valid_and_off(self):
        p = plan_of()
        self.assertNotIn("counter", p["sizing"])
        H.validate_plan(p)
        c = H.complete_plan(p)
        self.assertFalse(c["sizing"]["counter"]["on"])
        self.assertFalse(c["shadow"]["on"])

    def test_counter_on_needs_every_number(self):
        for k in ("ema_length", "alloc_pct", "leverage"):
            ct = dict(COUNTER)
            del ct[k]
            with self.assertRaises(H.PlanError, msg=k):
                H.validate_plan(plan_of(sizing=sizing(counter=ct)))

    def test_counter_off_needs_no_numbers(self):
        H.validate_plan(plan_of(sizing=sizing(counter={"on": False})))

    def test_counter_limits(self):
        H.validate_plan(plan_of(sizing=sizing(counter={**COUNTER, "alloc_pct": 0})))   # 0 = no counter entry (2026-10-25)
        for bad in (dict(ema_length=0), dict(ema_length=501), dict(alloc_pct=-1), dict(alloc_pct=101),
                    dict(leverage=0), dict(leverage=126), dict(leverage=2.5)):
            with self.assertRaises(H.PlanError, msg=str(bad)):
                H.validate_plan(plan_of(sizing=sizing(counter={**COUNTER, **bad})))
        H.validate_plan(plan_of(sizing=sizing(counter=COUNTER)))

    def test_shadow_on_needs_every_number(self):
        for k in ("trades", "slippage_ticks", "tracker_start"):
            sh = dict(SHADOW)
            del sh[k]
            with self.assertRaises(H.PlanError, msg=k):
                H.validate_plan(plan_of(shadow=sh))

    def test_shadow_off_needs_no_numbers_and_limits_hold(self):
        H.validate_plan(plan_of(shadow={"on": False}))
        for bad in (dict(trades=0), dict(trades=101), dict(slippage_ticks=-1), dict(tracker_start=0)):
            with self.assertRaises(H.PlanError, msg=str(bad)):
                H.validate_plan(plan_of(shadow={**SHADOW, **bad}))
        H.validate_plan(plan_of(shadow=SHADOW))


class TestEntrySize(unittest.TestCase):
    def sz(self, direction, close, ema, counter=True):
        plan = H.complete_plan(plan_of(sizing=sizing(counter=COUNTER if counter else {"on": False})))
        return H.entry_size(direction, snap_e(1, ema, close=close), plan)

    def test_off_is_always_the_single_size(self):
        self.assertEqual(self.sz("LONG", 90, 100, counter=False), (10.0, 5.0, "single"))
        self.assertEqual(self.sz("SHORT", 110, 100, counter=False), (10.0, 5.0, "single"))

    def test_long_above_ema_is_trend_below_is_counter(self):
        self.assertEqual(self.sz("LONG", 110, 100), (10.0, 5.0, "trend"))
        self.assertEqual(self.sz("LONG", 90, 100), (5.0, 2.0, "counter"))

    def test_short_below_ema_is_trend_above_is_counter(self):
        self.assertEqual(self.sz("SHORT", 90, 100), (10.0, 5.0, "trend"))
        self.assertEqual(self.sz("SHORT", 110, 100), (5.0, 2.0, "counter"))

    def test_equal_or_missing_ema_is_counter(self):
        self.assertEqual(self.sz("LONG", 100, 100)[2], "counter")
        self.assertEqual(self.sz("SHORT", 100, 100)[2], "counter")
        self.assertEqual(self.sz("LONG", 100, float("nan"))[2], "counter")


class TestSizingEma(unittest.TestCase):
    def test_column_is_the_ema_of_the_chart_close(self):
        import pandas as pd
        rng = np.random.default_rng(3)
        c = 100 + np.cumsum(rng.normal(0, 1, 400))
        df = pd.DataFrame({"open_time": np.arange(400) * 60000, "open": c - 0.2, "high": c + 1, "low": c - 1,
                           "close": c, "volume": 100 + rng.random(400)})
        for chart in ("Candles", "Heikin Ashi"):
            p = plan_of(sizing=sizing(counter=COUNTER), chart_type=chart)
            f = H.compute_hub(df, p)
            np.testing.assert_allclose(f["size_ema"].values, strat.pine_ema(f["c_close"].values, 50), equal_nan=True)
            self.assertEqual(H.last_hub_snapshot(f).size_ema, f["size_ema"].iloc[-1])


class IsolatedTracker(T.Base):
    """Own tracker folder per test (the tracker file is keyed by account + pair and would leak between tests)."""
    def setUp(self):
        super().setUp()
        import shutil
        import tempfile
        import unittest.mock as mock
        from app import base_v3_tracker as tr
        d = tempfile.mkdtemp(prefix="tracker_test_")
        pt = mock.patch.object(tr, "TRACKER_DIR", __import__("pathlib").Path(d))
        pt.start()
        self.addCleanup(pt.stop)
        self.addCleanup(shutil.rmtree, d, True)


class TestBotSizing(IsolatedTracker):
    async def test_trend_trade_uses_the_main_size_counter_trade_the_counter_size(self):
        plan = plan_of(sizing=sizing(counter=COUNTER))
        for ema, kind_alloc, kind_lev in ((90.0, 10.0, 5), (110.0, 5.0, 2)):
            T.Base.n += 1                      # a fresh instance (own hub memory) per round
            inst = self.make(plan)
            await self.tick(inst, snap_e(1000, ema, close=100.0, dir_e=1, dir_f=1))   # LONG at close 100
            # qty = 10000 * alloc% * lev / 100
            self.assertAlmostEqual(inst.client.market_orders[0][2], 10000 * kind_alloc / 100 * kind_lev / 100, places=3)
            self.assertEqual(inst.client.leverage_calls[-1], kind_lev)
            self.assertEqual(inst.state.alloc_pct, kind_alloc)
            self.assertEqual(inst.state.leverage, kind_lev)

    async def test_short_trend_and_counter(self):
        plan = plan_of(sizing=sizing(counter=COUNTER))
        inst = self.make(plan)
        await self.tick(inst, snap_e(1000, 110.0, close=100.0, dir_e=-1, dir_f=-1))   # SHORT below the EMA = trend
        self.assertEqual(inst.client.leverage_calls[-1], 5)
        T.Base.n += 1
        inst = self.make(plan)
        await self.tick(inst, snap_e(1000, 90.0, close=100.0, dir_e=-1, dir_f=-1))    # SHORT above the EMA = counter
        self.assertEqual(inst.client.leverage_calls[-1], 2)

    async def test_off_uses_the_single_size_even_when_far_from_the_ema(self):
        inst = self.make(plan_of(sizing=sizing()))
        await self.tick(inst, snap_e(1000, 500.0, close=100.0, dir_e=1, dir_f=1))
        self.assertEqual(inst.client.leverage_calls[-1], 5)
        self.assertAlmostEqual(inst.client.market_orders[0][2], 10000 * 0.10 * 5 / 100, places=3)

    async def test_max_loss_stop_uses_the_counter_size_quantity(self):
        inst = self.make(plan_of(sizing=sizing(counter=COUNTER)))
        await self.tick(inst, snap_e(1000, 110.0, close=100.0, dir_e=1, dir_f=1))   # counter: qty = 10000*5%*2/100 = 10
        # stop = entry - 10000 * 6% / 10 = 100 - 60 = 40
        self.assertAlmostEqual(inst.client.stop_orders[0][2], 40.0, places=2)


from app import shadow_book as B  # noqa: E402


class TestShadowBook(unittest.TestCase):
    """The shadow (2026-10-24): a paper copy of the whole plan that runs all the time. Pure logic."""
    def plan(self, **over):
        return H.complete_plan(plan_of(**{"shadow": SHADOW, "sizing": sizing(), **over}))

    def candle(self, book, snap, plan, tick=0.5, last_total=None, balance=None):
        return B.on_candle(book, snap, plan, tick_size=tick, last_total=last_total, balance=balance,
                           t_ms=snap.open_time * 1000 + 1)

    def test_flat_signal_opens_a_paper_long_with_slippage_and_the_max_loss(self):
        book = B.new_book(1000.0)
        ev, _ = self.candle(book, snap_e(1, 90.0, close=100.0, dir_e=1, dir_f=1), self.plan())
        self.assertEqual(book["pos"], 1)
        self.assertAlmostEqual(book["entry"], 100 + 10 * 0.5)            # 10 ticks of 0.5
        self.assertEqual(book["signal_close"], 100.0)
        # Max loss = close * (1 - 6% / (10% * 5)) = 88
        self.assertAlmostEqual(book["levels"]["ml"], 88.0)
        self.assertEqual((book["alloc"], book["lev"]), (10.0, 5.0))
        self.assertTrue(any("paper LONG opened" in e for e in ev))

    def test_paper_short_mirror(self):
        book = B.new_book(1000.0)
        self.candle(book, snap_e(1, 90.0, close=100.0, dir_e=-1, dir_f=-1), self.plan())
        self.assertEqual(book["pos"], -1)
        self.assertAlmostEqual(book["entry"], 95.0)
        self.assertAlmostEqual(book["levels"]["ml"], 112.0)

    def test_paper_trade_uses_counter_trend_size(self):
        book = B.new_book(1000.0)
        self.candle(book, snap_e(1, 110.0, close=100.0, dir_e=1, dir_f=1), self.plan(sizing=sizing(counter=COUNTER)))
        self.assertEqual((book["alloc"], book["lev"]), (5.0, 2.0))
        self.assertAlmostEqual(book["levels"]["ml"], 40.0)              # 100 * (1 - 6% / (5% * 2))

    def test_max_loss_off_only_the_stop_loss_stops(self):
        book = B.new_book(1000.0)
        plan = self.plan(sizing=sizing(stop_on=False))                   # test Stop Loss: 99.9% away (0.1)
        self.candle(book, snap_of(1, dir_e=1, dir_f=1), plan)
        self.assertEqual(book["pos"], 1)
        self.assertIsNone(book["levels"]["ml"])
        self.assertEqual(B.on_price(book, 1.0, plan, 5000), [])          # above the Stop Loss: still open
        self.assertEqual(book["pos"], 1)

    def test_live_price_through_the_max_loss_closes_at_the_level(self):
        book = B.new_book(1000.0)
        plan = self.plan()
        self.candle(book, snap_of(1, dir_e=1, dir_f=1, close=100.0), plan)
        closed = B.on_price(book, 87.5, plan, 5000)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "Max Loss")
        self.assertEqual(closed[0]["exit_price"], 88.0)
        self.assertLess(closed[0]["result_pct"], 0)
        self.assertLess(book["equity"], 1000.0)
        self.assertEqual(book["pos"], 0)

    def test_flip_closes_and_reverse_opens_the_opposite_on_the_same_candle(self):
        book = B.new_book(1000.0)
        plan = self.plan()
        self.candle(book, snap_of(1, dir_e=1, dir_f=1, close=100.0), plan)
        ev, closed = self.candle(book, snap_of(2, dir_e=-1, dir_f=-1, close=95.0), plan)
        self.assertEqual([c["exit_reason"] for c in closed], ["FLIP"])
        self.assertEqual(book["pos"], -1)

    def test_an_old_close_only_plan_now_flips_in_the_shadow_too(self):
        book = B.new_book(1000.0)
        plan = self.plan(flip_action="Close only (go flat)")
        self.candle(book, snap_of(1, dir_e=1, dir_f=1, close=100.0), plan)
        self.candle(book, snap_of(2, dir_e=0, dir_f=-1, close=95.0), plan)
        self.assertEqual(book["pos"], -1)
        self.assertEqual(len(book["trades"]), 1)

    def test_hold_rule_holds_a_paper_flip(self):
        book = B.new_book(1000.0)
        plan = self.plan(hold={"on": True, "adx_level": 25.0})
        self.candle(book, snap_of(1, dir_e=1, dir_f=1, close=100.0), plan)
        self.candle(book, snap_of(2, dir_e=-1, dir_f=-1, close=110.0, adx=10.0), plan)   # in profit, ADX low
        self.assertEqual(book["pos"], 1)
        self.assertEqual(book["trades"], [])

    def test_entry_wait_delays_the_paper_entry(self):
        book = B.new_book(1000.0)
        plan = self.plan(entry_wait=2)
        for t in (1, 2):
            self.candle(book, snap_of(t, dir_e=1, dir_f=1, close=100.0), plan)
            self.assertEqual(book["pos"], 0, t)
        self.candle(book, snap_of(3, dir_e=1, dir_f=1, close=100.0), plan)
        self.assertEqual(book["pos"], 1)

    def test_no_signal_no_paper_trade(self):
        book = B.new_book(1000.0)
        self.candle(book, snap_of(1, dir_e=0, dir_f=0), self.plan())
        self.assertEqual(book["pos"], 0)

    def test_the_whole_plan_stop_loss_step_trailing_and_partial_take_profit(self):
        plan = self.plan(sl={"on": True, "basis": H.BASIS_PRICE, "value": 3.0},
                         trail={"on": True, "type": H.TRAIL_STEP, "basis": H.BASIS_PRICE, "start": H.START_SL,
                                "gap": 1.0},
                         tp={"on": True, "basis": H.BASIS_PRICE,
                             "levels": [{"on": True, "value": 3.0, "pct": 50.0}, {"on": False}, {"on": False}]},
                         shadow={**SHADOW, "slippage_ticks": 0})
        book = B.new_book(1000.0)
        self.candle(book, snap_of(1, dir_e=1, dir_f=1, close=100.0), plan)
        self.assertAlmostEqual(book["levels"]["sl"], 97.0)
        self.assertEqual(B.on_price(book, 103.0, plan, 5000), [])        # TP1 closes half; trailing to 97 + 3 x 1
        self.assertAlmostEqual(book["frac"], 0.5)
        self.assertAlmostEqual(book["levels"]["ts"], 100.0)
        self.assertGreater(book["equity"], 1000.0)
        closed = B.on_price(book, 99.9, plan, 6000)                      # back down: the Trailing Stop at 100
        self.assertEqual(closed[0]["exit_reason"], "TP1, Trailing Stop")
        self.assertEqual(closed[0]["exit_price"], 100.0)
        # 3% x 10% x 5 x 0.5 = 0.75% on TP1, 0% at the entry price on the rest
        self.assertAlmostEqual(closed[0]["result_pct"], 0.75, places=6)

    def test_mid_trend_re_entry_not_allowed_by_default(self):
        book = B.new_book(1000.0)
        plan = self.plan()
        self.candle(book, snap_of(1, dir_e=1, dir_f=1, close=100.0), plan)
        B.on_price(book, 80.0, plan, 5000)                               # stopped out
        self.candle(book, snap_of(2, dir_e=1, dir_f=1, close=100.0), plan)   # same signal still on
        self.assertEqual(book["pos"], 0)
        self.candle(book, snap_of(3, dir_e=0, dir_f=0, close=100.0), plan)   # it ended
        self.candle(book, snap_of(4, dir_e=1, dir_f=1, close=100.0), plan)   # a new signal
        self.assertEqual(book["pos"], 1)

    def test_mid_trend_re_entry_allowed(self):
        book = B.new_book(1000.0)
        plan = self.plan(allow_reentry=True)
        self.candle(book, snap_of(1, dir_e=1, dir_f=1, close=100.0), plan)
        B.on_price(book, 80.0, plan, 5000)
        self.candle(book, snap_of(2, dir_e=1, dir_f=1, close=100.0), plan)
        self.assertEqual(book["pos"], 1)

    def test_equity_line_per_candle(self):
        book = B.new_book(1000.0)
        plan = self.plan()
        t0 = 60_000_000                                                  # 1-minute candles (ms)
        B.on_candle(book, snap_of(t0, dir_e=1, dir_f=1, close=100.0), plan, tick_size=0.5, last_total=None,
                    balance=None, t_ms=t0 + 60_000)                      # opened at that candle's close
        B.on_price(book, 87.0, plan, t0 + 90_000)                        # stopped half way through the next one
        line = B.line_on_candles(book, [t0, t0 + 60_000], 60_000)
        self.assertEqual(line[0], [t0, 1000.0])
        self.assertLess(line[1][1], 1000.0)
        self.assertEqual(line[1][0], t0 + 60_000)


class TestBotShadow(IsolatedTracker):
    def plan(self, **over):
        return plan_of(**{"shadow": SHADOW, "sizing": sizing(), **over})

    async def winning_flip(self, inst):
        """A long wins 10 on a flip candle -> a new tracker high."""
        self.go_long(inst, price=100.0)
        inst.client.mark_price = 110.0
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=110.0))

    async def test_a_winning_new_high_starts_trades_off_and_blocks_the_real_opposite_entry(self):
        inst = self.make(self.plan())
        await self.winning_flip(inst)
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertTrue(inst.tracker.get_shadow().active)
        self.assertEqual(inst.client.market_orders, [])            # the opposite side was NOT opened for real
        self.assertEqual(inst.state.status, "IDLE")
        self.assertIsNone(inst.hub.open_now)
        self.assertEqual(inst.hub.shadow_book["pos"], -1)          # the shadow took it (paper)

    async def test_without_the_shadow_box_a_win_does_not_start_trades_off(self):
        inst = self.make(plan_of(sizing=sizing()))
        await self.winning_flip(inst)
        self.assertFalse(inst.tracker.get_shadow().active)
        self.assertEqual(len(inst.client.market_orders), 1)        # normal flip: opposite opened
        self.assertIsNone(inst.hub.shadow_book)                    # no shadow without the box
        self.assertGreater(inst.tracker.balance, inst.tracker.start_balance)

    async def test_a_losing_close_does_not_start_trades_off(self):
        inst = self.make(self.plan())
        self.go_long(inst, price=100.0)
        inst.client.mark_price = 95.0
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=95.0))
        self.assertFalse(inst.tracker.get_shadow().active)
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_the_shadow_runs_all_the_time_next_to_real_trading(self):
        inst = self.make(self.plan())
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)        # the real trade
        self.assertEqual(inst.hub.shadow_book["pos"], 1)            # and its shadow

    async def test_trades_off_count_on_the_shadow_then_real_trading_goes_live(self):
        inst = self.make(self.plan())
        await self.winning_flip(inst)                               # shadow SHORT already open (paper)
        sh = lambda: inst.tracker.get_shadow()  # noqa: E731
        await self.tick(inst, snap_of(1001, dir_e=1, dir_f=1, close=100.0))   # shadow short flips: trade 1 off
        self.assertEqual(sh().count, 1)
        self.assertEqual(inst.client.market_orders, [])
        await self.tick(inst, snap_of(1002, dir_e=-1, dir_f=-1, close=95.0))  # trade 2 off -> live again
        self.assertFalse(sh().active)
        self.assertEqual(sh().resume_block_time, 1002)              # not on the candle it ended by a flip
        self.assertEqual(inst.client.market_orders, [])
        self.assertTrue(all(r["during_trades_off"] for r in sh().history[-2:]))
        await self.tick(inst, snap_of(1003, dir_e=-1, dir_f=-1, close=94.0))  # real trading is back
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(inst.client.market_orders[0][1], "SELL")

    async def test_paper_trades_never_send_an_order_or_change_the_total(self):
        inst = self.make(self.plan())
        await self.winning_flip(inst)
        n_close = len(inst.client.close_orders)
        await self.tick(inst, snap_of(1001, dir_e=1, dir_f=1, close=100.0))
        await inst._hub_shadow_live(10.0)                             # paper Max Loss hit
        self.assertEqual(len(inst.client.close_orders), n_close)
        self.assertEqual(inst.client.market_orders, [])
        self.assertEqual(inst.client.stop_orders, [])

    async def test_shadow_only_runs_the_shadow_and_never_trades(self):
        inst = self.make(self.plan())
        inst._shadow_only = True
        inst.cur = snap_of(1000, dir_e=1, dir_f=1, close=100.0)
        await inst._tick()
        self.assertEqual(inst.client.market_orders, [])
        self.assertEqual(inst.hub.shadow_book["pos"], 1)

    async def test_stop_keeps_the_shadow_running(self):
        import asyncio
        inst = self.make(self.plan())
        inst._task = asyncio.create_task(asyncio.sleep(30))
        self.addCleanup(inst._task.cancel)
        await inst.stop(keep_shadow=True)
        self.assertTrue(inst._shadow_only)
        self.assertEqual(inst.state.status, "STOPPED")
        self.assertFalse(inst._task.done())                         # the loop keeps going (shadow only)

    async def test_stop_without_shadow_stops_fully(self):
        import asyncio
        inst = self.make(plan_of(sizing=sizing()))
        inst.manager = None
        inst._task = asyncio.create_task(asyncio.sleep(30))
        self.addCleanup(inst._task.cancel)
        inst._task.cancel()
        await inst.stop(keep_shadow=True)
        self.assertFalse(inst._shadow_only)

    async def test_shadow_state_survives_a_restart(self):
        inst = self.make(self.plan())
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        inst2 = self.make(self.plan())
        inst2.platform, inst2.account_id = inst.platform, inst.account_id
        from app import hub_state
        inst2.hub = hub_state.load(inst.platform, inst.account_id, inst.symbol)
        self.assertEqual(inst2.hub.shadow_book["pos"], 1)

    async def test_switching_shadow_off_in_the_plan_ends_a_running_period(self):
        inst = self.make(self.plan())
        await self.winning_flip(inst)
        self.assertTrue(inst.tracker.get_shadow().active)
        inst2 = self.make(plan_of(sizing=sizing()))        # same pair, shadow box not ticked any more
        self.assertFalse(inst2.tracker.get_shadow().active)
        self.assertEqual(inst2.tracker.get_shadow().last_end_reason, "shadow mode was switched off in the plan")

    async def test_tracker_start_comes_from_the_plan(self):
        inst = self.make(self.plan())
        self.assertEqual(inst.tracker.start_balance, 1000.0)

    async def test_status_reports_both_boxes_and_shadow_only(self):
        inst = self.make(self.plan(sizing=sizing(counter=COUNTER)))
        st = inst._hub_status()
        self.assertTrue(st["shadow_on"])
        self.assertTrue(st["counter_sizing_on"])
        self.assertFalse(st["shadow_only"])
        self.assertIn("shadow_only", inst.to_status_dict())


class TestManagerPause(unittest.IsolatedAsyncioTestCase):
    """The Stop button pauses: a hub pair with Shadow mode keeps its shadow; a real Start replaces the shadow-only
    instance."""
    async def test_pause_and_start(self):
        import unittest.mock as mock
        from app.manager import BotManager
        m = BotManager()

        class FakeInst:
            def __init__(self):
                self._shadow_only = False
                self._task = mock.Mock(done=lambda: False)
                self.stop = mock.AsyncMock(side_effect=self._stop)
                self.start = mock.Mock()
            async def _stop(self, keep_shadow=False):
                if keep_shadow:
                    self._shadow_only = True
        inst = FakeInst()
        m.instances["a:BTCUSDT"] = inst
        with mock.patch("app.manager.store") as st:
            await m.pause("a", "BTCUSDT")
            st.set_pair_enabled.assert_called_once_with("a", "BTCUSDT", False)
        inst.stop.assert_awaited_once_with(keep_shadow=True)
        self.assertTrue(inst._shadow_only)
        new = FakeInst()
        with mock.patch("app.manager.singleton_lock.is_lock_held", return_value=True), \
             mock.patch.object(m, "_check_may_start"), mock.patch.object(m, "_build_instance", return_value=new), \
             mock.patch("app.manager.store"):
            got = await m.start("a", "BTCUSDT")
        self.assertIs(got, new)
        self.assertEqual(inst.stop.await_args_list[-1], mock.call())      # the shadow-only one stopped fully
        self.assertIs(m.instances["a:BTCUSDT"], new)


if __name__ == "__main__":
    unittest.main()
