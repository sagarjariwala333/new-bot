"""Strategy Hub trading in the bot: the flip order (CLOSE, FLAT, CHECK RULES, OPEN OPPOSITE), entry wait,
close-only, hold rule, rule change during a flip, max-loss stop, plan saving. Arbitrary test numbers only."""
import copy
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATA_DIR = Path("/tmp/hull_bot_test_hub_trading")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402

import pandas as pd  # noqa: E402

from tests.fake_client import FakeClient  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from tests import _test_values as tv  # noqa: E402
from app.instance import BotInstance  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import hub_state, hub_api  # noqa: E402
from app import default_settings as ds  # noqa: E402


class HubFake(FakeClient):
    """Market orders really open a position so the follow-up position query sees it."""
    async def market_order(self, symbol, side, quantity):
        r = await super().market_order(symbol, side, quantity)
        self.position = {"positionAmt": str(quantity if side == "BUY" else -quantity),
                         "entryPrice": str(self.mark_price)}
        return r


class _Store:
    def __init__(self, pc=None):
        self.pc = pc

    def get_account(self, account_id):
        if self.pc is None:
            return None

        class A:
            max_account_exposure_pct = None
            pairs = {self.pc.symbol: self.pc}
        return A()


class _Mgr:
    def __init__(self, pc=None):
        import asyncio
        self.store = _Store(pc)
        self._lock = asyncio.Lock()

    def get_account_lock(self, account_id):
        return self._lock

    async def maybe_teardown_user_data_stream(self, account_id):
        return None


def snap_of(t, dir_e=0, dir_f=0, close=100.0, adx=10.0):
    return H.HubSnapshot(open_time=t, open=close, high=close + 1, low=close - 1, close=close, dir_e=dir_e,
                         dir_f=dir_f, adx=adx, tp_atr=2.0, trail_low=close - 5, trail_high=close + 5,
                         c_close=close, ma1=close, ma2=close)


def plan_with(**over):
    p = make_plan(ind_ma1={"entry": True, "flip": True}, **over)
    return p


class Base(unittest.IsolatedAsyncioTestCase):
    n = 0

    def setUp(self):
        Base.n += 1
        # Own hub-state folder per test: other test files wipe the shared data dir.
        import tempfile
        import unittest.mock as _mock
        self._hub_dir = tempfile.mkdtemp(prefix="hub_state_test_")
        p = _mock.patch.object(hub_state, "HUB_STATE_DIR", Path(self._hub_dir))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self._hub_dir, True)

    def make(self, plan=None, **pc_over):
        plan = plan or plan_with()
        pc = tv.pair_config(symbol="BTCUSDT", enabled=True, plan=plan, **pc_over)
        inst = BotInstance(account_id=f"acc_hub_{Base.n}", account_name="Main", symbol="BTCUSDT",
                           api_key="k", api_secret="s", testnet=True, pair_config=pc)
        inst.client = HubFake()
        inst.manager = _Mgr()
        import time
        inst.mark_feed.last_message_at = time.time()   # a live price feed
        inst.cur = None

        async def fetch(p):
            s = inst.cur
            frame = pd.DataFrame({"open_time": [s.open_time - 2, s.open_time - 1, s.open_time],
                                  "dir_e": inst.hist + [s.dir_e]})
            inst.state.last_indicators = s.to_display_dict()
            return frame, s
        inst.hist = [0, 0]
        inst._hub_fetch = fetch
        return inst

    def go_long(self, inst, t=1, price=100.0):
        inst.client.position = {"positionAmt": "1.0", "entryPrice": str(price)}
        inst.client.mark_price = price
        st = inst.state
        st.status, st.direction, st.entry_price, st.qty, st.bot_qty = "IN_POSITION", "LONG", price, 1.0, 1.0
        st.equity_at_entry = 10000.0
        st.sl_order_id, st.sl_price = 5, price - 10
        inst.client.open_algo_orders.append({"algoId": 5, "orderType": "STOP_MARKET", "triggerPrice": str(price - 10),
                                             "clientAlgoId": "hullbot_fake_sl", "algoStatus": "NEW"})

    async def tick(self, inst, snap):
        inst.cur = snap
        await inst._tick_hub()


class TestFlipOrder(Base):
    async def test_flip_closes_then_opens_opposite_same_candle(self):
        inst = self.make()
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=95.0))
        c = inst.client
        self.assertEqual(len(c.close_orders), 1)
        self.assertEqual(c.close_orders[0][1], "SELL")
        self.assertEqual(len(c.market_orders), 1)
        self.assertEqual(c.market_orders[0][1], "SELL")        # opens SHORT
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(len(c.stop_orders), 1)
        self.assertEqual(c.stop_orders[0][1], "BUY")
        self.assertIsNone(inst.hub.open_now)
        self.assertTrue(inst.hub.first_trade_done)

    async def test_balance_is_read_after_the_close_and_sizes_the_trade(self):
        inst = self.make(make_plan(ind_ma1={"entry": True, "flip": True},
                                   sizing={"alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_pct": 6.0}))
        self.go_long(inst)
        inst.client.available_balance = 8000.0
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=100.0))
        # qty = 8000 * 10% * 5 / 100 = 40
        self.assertAlmostEqual(inst.client.market_orders[0][2], 40.0, places=3)
        # stop = entry + 8000*6%/40 = 100 + 12
        self.assertAlmostEqual(inst.client.stop_orders[0][2], 112.0, places=2)
        self.assertEqual(inst.client.leverage_calls[-1], 5)

    async def test_call_order_close_before_open(self):
        inst = self.make()
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        order = inst.client.call_order
        self.assertLess(order.index("set_leverage:5"), order.index("market_order"))
        self.assertEqual(len(inst.client.close_orders), 1)

    async def test_same_candle_is_not_acted_on_twice(self):
        inst = self.make()
        self.go_long(inst)
        s = snap_of(1000, dir_e=-1, dir_f=-1)
        await self.tick(inst, s)
        await self.tick(inst, s)
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_no_flip_signal_does_nothing(self):
        inst = self.make()
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1))
        self.assertEqual(inst.client.close_orders, [])
        self.assertEqual(inst.client.market_orders, [])
        self.assertEqual(inst.state.direction, "LONG")

    async def test_hold_rule_keeps_the_trade(self):
        plan = plan_with(hold={"on": True, "adx_level": 25.0})
        inst = self.make(plan)
        self.go_long(inst, price=100.0)
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=105.0, adx=10.0))   # in profit, ADX low
        self.assertEqual(inst.client.close_orders, [])
        self.assertEqual(inst.state.held_signal_count, 1)

    async def test_hold_rule_does_not_hold_a_losing_trade(self):
        plan = plan_with(hold={"on": True, "adx_level": 25.0})
        inst = self.make(plan)
        self.go_long(inst, price=100.0)
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=95.0, adx=10.0))
        self.assertEqual(len(inst.client.close_orders), 1)


class TestSlimPlan(Base):
    async def test_plan_with_only_what_is_used_runs(self):
        slim = make_plan(ind_ma1={"entry": True, "flip": True})
        for b in H.BLOCKS:
            if b != "ma1":
                slim["ind"][b] = {"entry": False, "flip": False}
        del slim["renko_atr_length"]
        slim["hold"] = {"on": False}
        slim["tp"] = {"on": False}
        slim["trail"] = {"on": False}
        slim["sizing"] = {"alloc_pct": 10.0, "leverage": 5, "stop_on": False}
        inst = self.make(slim)
        self.assertIsNone(inst._plan_error)
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1))
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(inst.client.stop_orders, [])
        self.assertTrue(inst.to_status_dict()["hub"]["plan_summary"])

    async def test_broken_plan_refuses_to_trade(self):
        bad = plan_with()
        del bad["sizing"]
        inst = self.make(bad)
        self.assertIsNotNone(inst._plan_error)
        with self.assertRaises(H.PlanError):
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1))
        self.assertEqual(inst.client.market_orders, [])


class TestFlipActionAndWait(Base):
    async def test_an_old_close_only_plan_now_opens_the_new_side(self):
        # 2026-10-25: "Close only" removed - a flip closes, goes flat, checks the rules, enters the new side
        plan = plan_with(flip_action="Close only (go flat)")
        inst = self.make(plan)
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=0, dir_f=-1))
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertEqual(H.upgrade_plan(plan)["flip_action"], H.FLIP_ACTIONS[0])

    async def test_close_only_still_enters_when_the_entry_rules_say_so(self):
        # Same as the hub Pine: 'close only' means the flip itself does not open the opposite side;
        # a normal entry signal on the same candle is still an entry (flat, entry rules true).
        plan = plan_with(flip_action="Close only (go flat)")
        inst = self.make(plan)
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_entry_wait_1_waits_one_more_candle(self):
        plan = plan_with(entry_wait=1)
        inst = self.make(plan)
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(inst.client.market_orders, [])          # closed, but waiting
        self.assertEqual(inst.state.status, "IDLE")
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1))  # signal still holds -> now open
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(inst.state.direction, "SHORT")

    async def test_entry_wait_1_cancelled_if_signal_breaks(self):
        plan = plan_with(entry_wait=1)
        inst = self.make(plan)
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        await self.tick(inst, snap_of(2000, dir_e=0, dir_f=0))
        self.assertEqual(inst.client.market_orders, [])
        self.assertEqual(inst.hub.wait["pend_dir"], 0)

    async def test_fresh_entry_when_flat_wait_0_opens_at_once(self):
        inst = self.make()
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1))
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(inst.client.market_orders[0][1], "BUY")

    async def test_no_signal_stays_flat(self):
        inst = self.make()
        await self.tick(inst, snap_of(1000, dir_e=0, dir_f=0))
        self.assertEqual(inst.client.market_orders, [])
        self.assertEqual(inst.state.status, "IDLE")

    async def test_first_trade_waits_for_fresh_reversal(self):
        plan = plan_with(wait_first_reversal=True)
        inst = self.make(plan)
        inst.hist = [1, 1]
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1))      # mid-trend: same side as before -> no
        self.assertEqual(inst.client.market_orders, [])
        inst.hist = [1, 1]
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1))    # reversal -> yes
        self.assertEqual(len(inst.client.market_orders), 1)


class TestNotFlatYet(Base):
    async def test_open_waits_until_exchange_confirms_flat(self):
        inst = self.make()
        self.go_long(inst)
        inst.client.close_result_confirms_flat = False
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(inst.state.status, "CLOSING")
        self.assertEqual(inst.client.market_orders, [])
        self.assertIsNotNone(inst.hub.open_now)
        self.assertTrue(inst._hub_busy)
        inst.client.position = None                     # exchange now shows flat
        await inst._tick()                              # CLOSING branch finishes the close ...
        self.assertEqual(len(inst.client.market_orders), 1)   # ... and the flip continues at once
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertFalse(inst._hub_busy)

    async def test_stale_decision_from_an_older_candle_is_dropped(self):
        inst = self.make()
        inst.hub.open_now = {"dir": 1, "candle": 500, "rules_check": False}
        await self.tick(inst, snap_of(1000, dir_e=0, dir_f=0))
        self.assertEqual(inst.client.market_orders, [])
        self.assertIsNone(inst.hub.open_now)

    async def test_position_already_there_blocks_a_second_one(self):
        inst = self.make()
        inst.client.position = {"positionAmt": "1.0", "entryPrice": "100"}
        inst.hub.open_now = {"dir": 1, "candle": 1000, "rules_check": False}
        await inst._hub_try_open(snap_of(1000, dir_e=1))
        self.assertEqual(inst.client.market_orders, [])
        self.assertIsNone(inst.hub.open_now)


class TestRulesChangeDuringFlip(Base):
    async def test_new_rules_decide_after_the_close(self):
        old = plan_with()
        inst = self.make(old)
        self.go_long(inst)
        new = plan_with(entry_wait=1)               # new rule: wait one candle
        new_pc = tv.pair_config(symbol="BTCUSDT", enabled=True, plan=new)
        inst.manager = _Mgr(new_pc)
        inst._pending_restart_on_flat = True
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(inst.client.market_orders, [])            # new rule says wait -> not opened at once
        self.assertEqual(inst._plan["entry_wait"], 1)              # new rules are the ones running now
        self.assertFalse(inst._pending_restart_on_flat)
        self.assertEqual(inst.hub.wait["pend_dir"], -1)
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1))
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_new_rules_that_switch_entry_off_do_not_open(self):
        inst = self.make(plan_with())
        self.go_long(inst)
        new = make_plan(ind_ma1={"entry": False, "flip": False}, ind_ma2={"entry": True, "flip": False})
        inst.manager = _Mgr(tv.pair_config(symbol="BTCUSDT", enabled=True, plan=new))
        inst._pending_restart_on_flat = True
        inst.hub.open_now = None
        # new rules judge this candle: entry signal of the NEW plan is absent (dir_e 0)
        await self.tick(inst, snap_of(1000, dir_e=0, dir_f=-1))   # old flip fires (dir_f -1); new entry says nothing
        self.assertEqual(inst.client.market_orders, [])

    async def test_unchanged_rules_open_at_once(self):
        inst = self.make()
        self.go_long(inst)
        inst.manager = _Mgr(tv.pair_config(symbol="BTCUSDT", enabled=True, plan=plan_with()))
        inst._pending_restart_on_flat = False
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_timeframe_change_hands_over_to_restart(self):
        inst = self.make()
        self.go_long(inst)
        inst.manager = _Mgr(tv.pair_config(symbol="BTCUSDT", enabled=True, plan=plan_with(), timeframe="4h"))
        inst._pending_restart_on_flat = True
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(inst.client.market_orders, [])            # restart applies it
        self.assertTrue(inst.hub.open_now["rules_check"])
        self.assertTrue(inst._pending_restart_on_flat)

    async def test_restarted_instance_rejudges_under_current_rules(self):
        inst = self.make()
        inst.hub.open_now = {"dir": -1, "candle": 1000, "rules_check": True}
        await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(len(inst.client.market_orders), 1)        # current rules also say SHORT, wait 0
        inst2 = self.make(plan_with(entry_wait=2))
        inst2.hub.open_now = {"dir": -1, "candle": 1000, "rules_check": True}
        await self.tick(inst2, snap_of(1000, dir_e=-1, dir_f=-1))
        self.assertEqual(inst2.client.market_orders, [])           # new rules: wait 2


class TestMaxLoss(Base):
    async def test_max_loss_off_opens_without_a_stop(self):
        plan = plan_with(sizing={"alloc_pct": 10.0, "leverage": 5, "stop_on": False, "stop_pct": 6.0})
        inst = self.make(plan)
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1))
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(inst.client.stop_orders, [])
        self.assertEqual(inst.state.status, "IN_POSITION")

    async def test_stop_failure_is_unprotected_not_idle(self):
        inst = self.make()
        inst.client.fail_stop_market_order = True
        inst.client.close_result_confirms_flat = False   # emergency close cannot confirm -> stays UNPROTECTED
        import asyncio
        real_sleep = asyncio.sleep
        asyncio.sleep = lambda *_a, **_k: real_sleep(0)
        try:
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1))
        finally:
            asyncio.sleep = real_sleep
        self.assertEqual(inst.state.status, "UNPROTECTED")   # never silently IDLE/IN_POSITION without a stop

    async def test_resolve_unprotected_uses_plan_stop(self):
        inst = self.make()
        inst.client.position = {"positionAmt": "50.0", "entryPrice": "100"}
        st = inst.state
        st.status, st.direction, st.entry_price, st.qty, st.bot_qty = "UNPROTECTED", "LONG", 100.0, 50.0, 50.0
        st.equity_at_entry = 10000.0
        await inst._resolve_unprotected_position()
        self.assertEqual(st.status, "IN_POSITION")
        # 10000 * 6% / 50 = 12 below the entry
        self.assertEqual(len(inst.client.stop_orders), 1)
        self.assertAlmostEqual(inst.client.stop_orders[-1][2], 88.0, places=2)


class TestPlanSaving(Base):
    async def test_incomplete_plan_is_refused(self):
        class S:
            def update_pair(self, *a, **k):
                raise AssertionError("must not be saved")
        bad = plan_with()
        del bad["sizing"]
        with self.assertRaises(hub_api.PlanRefused):
            await hub_api.apply_plan(S(), None, "a", "BTCUSDT", bad)

    async def test_plan_with_nothing_for_entry_is_refused(self):
        class S:
            def update_pair(self, *a, **k):
                raise AssertionError("must not be saved")
        with self.assertRaises(hub_api.PlanRefused):
            await hub_api.apply_plan(S(), None, "a", "BTCUSDT", make_plan())

    async def test_saved_while_in_position_is_deferred(self):
        inst = self.make()
        self.go_long(inst)
        saved = {}

        class S:
            def update_pair(self, a, s, upd):
                saved.update(upd)
                return tv.pair_config(symbol="BTCUSDT", plan=upd["plan"])

        class M:
            restarted = False

            def get(self, a, s):
                return inst

            async def restart(self, a, s):
                M.restarted = True
        out = await hub_api.apply_plan(S(), M(), "a", "BTCUSDT", plan_with(entry_wait=3))
        self.assertTrue(inst._pending_restart_on_flat)
        self.assertFalse(M.restarted)
        self.assertEqual(saved["plan"]["entry_wait"], 3)
        self.assertEqual(inst._plan["entry_wait"], 0)              # the running trade keeps its rules

    async def test_saved_while_a_flip_is_unfinished_is_deferred(self):
        inst = self.make()
        inst.hub.open_now = {"dir": 1, "candle": 1, "rules_check": False}

        class S:
            def update_pair(self, a, s, upd):
                return tv.pair_config(symbol="BTCUSDT", plan=upd["plan"])

        class M:
            restarted = False

            def get(self, a, s):
                return inst

            async def restart(self, a, s):
                M.restarted = True
        await hub_api.apply_plan(S(), M(), "a", "BTCUSDT", plan_with())
        self.assertTrue(inst._pending_restart_on_flat)
        self.assertFalse(M.restarted)

    async def test_saved_when_flat_restarts(self):
        inst = self.make()

        class S:
            def update_pair(self, a, s, upd):
                return tv.pair_config(symbol="BTCUSDT", plan=upd["plan"])

        class M:
            restarted = False

            def get(self, a, s):
                return inst

            async def restart(self, a, s):
                M.restarted = True
        await hub_api.apply_plan(S(), M(), "a", "BTCUSDT", plan_with())
        self.assertTrue(M.restarted)

    def test_stored_plan_is_a_copy_of_the_running_plan(self):
        plan = plan_with()
        inst = self.make(plan)
        plan["entry_wait"] = 9
        self.assertEqual(inst._plan["entry_wait"], 0)


class TestReadyChecks(Base):
    def test_hub_pair_needs_only_pair_level_values_plus_plan(self):
        pc = tv.pair_config(symbol="BTCUSDT", plan=plan_with())
        import dataclasses
        vals = dataclasses.asdict(pc)
        for k in ("hma_length", "ema_filter_length", "trend_alloc_pct", "stop_loss_pct_equity", "tp1_atr_mult"):
            vals[k] = None
        ds.require_pair_ready("binance", vals)                    # no error

    def test_hub_pair_without_valid_plan_cannot_start(self):
        import dataclasses
        vals = dataclasses.asdict(tv.pair_config(symbol="BTCUSDT", plan=plan_with()))
        vals["plan"]["sizing"]["alloc_pct"] = None
        with self.assertRaises(RuntimeError):
            ds.require_pair_ready("binance", vals)

    def test_hub_pair_without_timeframe_cannot_start(self):
        import dataclasses
        vals = dataclasses.asdict(tv.pair_config(symbol="BTCUSDT", plan=plan_with()))
        vals["timeframe"] = None
        with self.assertRaises(RuntimeError):
            ds.require_pair_ready("binance", vals)

    def test_pair_without_plan_still_needs_all_old_settings(self):
        import dataclasses
        vals = dataclasses.asdict(tv.pair_config(symbol="BTCUSDT"))
        vals["hma_length"] = None
        with self.assertRaises(RuntimeError):
            ds.require_pair_ready("binance", vals)

    def test_pair_without_plan_is_not_hub(self):
        pc = tv.pair_config(symbol="BTCUSDT")
        inst = BotInstance(account_id="acc_nohub", account_name="Main", symbol="BTCUSDT",
                           api_key="k", api_secret="s", testnet=True, pair_config=pc)
        self.assertFalse(inst._is_hub())
        self.assertFalse(inst._hub_busy)
        self.assertIsNone(inst.to_status_dict()["hub"])

    def test_plan_change_resets_the_old_wait_memory(self):
        inst = self.make()
        inst.hub.wait = {"pend_dir": 1, "pend_bars": 1, "pend_from_flip": True}
        hub_state.save(inst.hub)
        pc = tv.pair_config(symbol="BTCUSDT", plan=plan_with(entry_wait=4))
        inst2 = BotInstance(account_id=inst.account_id, account_name="Main", symbol="BTCUSDT",
                            api_key="k", api_secret="s", testnet=True, pair_config=pc)
        self.assertEqual(inst2.hub.wait["pend_dir"], 0)


if __name__ == "__main__":
    unittest.main()
