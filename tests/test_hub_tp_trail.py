"""Strategy Hub take profit (sequential limit orders with memory) and trailing stop. Arbitrary test numbers."""
import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_trading import (Base, HubFake, snap_of, plan_with, TEST_DATA_DIR)  # noqa: E402,F401
from tests._hub_plan import make_plan  # noqa: E402
from tests import _test_values as tv  # noqa: E402
from app.instance import BotInstance  # noqa: E402
from app import hub_state, ledger  # noqa: E402

TP3 = {"on": True, "atr_length": 14,
       "levels": [{"on": True, "mult": 1.0, "pct": 33.0}, {"on": True, "mult": 3.0, "pct": 33.0},
                  {"on": True, "mult": 5.0, "pct": 34.0}]}


def tp_plan(**over):
    return plan_with(tp=dict(TP3), **over)


class TPBase(Base):
    def setUp(self):
        super().setUp()
        self.records = []
        p = mock.patch.object(ledger, "record_trade", side_effect=lambda r: self.records.append(r))
        p.start()
        self.addCleanup(p.stop)

    async def open_long(self, inst, t=1000, close=100.0):
        await self.tick(inst, snap_of(t, dir_e=1, dir_f=1, close=close))
        self.assertEqual(inst.state.direction, "LONG")

    @staticmethod
    def fill(inst, order_id, qty=None, status="FILLED", price=None):
        o = inst.client.limit_orders[order_id]
        q = float(o["origQty"]) if qty is None else qty
        o["status"], o["executedQty"], o["avgPrice"] = status, str(q), str(price or o["price"])
        pos = inst.client.position
        if pos is not None:
            left = abs(float(pos["positionAmt"])) - q
            sign = 1 if float(pos["positionAmt"]) > 0 else -1
            inst.client.position = None if left <= 1e-9 else {**pos, "positionAmt": str(sign * left)}


class TestTakeProfit(TPBase):
    async def test_only_tp1_is_placed_at_entry(self):
        inst = self.make(tp_plan())
        await self.open_long(inst)
        c = inst.client
        self.assertEqual(len(c.limit_calls), 1)
        sym, side, qty, price = c.limit_calls[0]
        self.assertEqual(side, "SELL")
        self.assertAlmostEqual(price, 102.0, places=2)             # entry 100 + 1 x ATR 2
        self.assertAlmostEqual(qty, 16.5, places=3)                # 33% of 50
        # memory is on disk
        saved = hub_state.load(inst.platform, inst.account_id, inst.symbol)
        self.assertEqual([l["state"] for l in saved.tp["levels"]], ["placed", "pending", "pending"])
        self.assertAlmostEqual(saved.tp["levels"][1]["price"], 106.0, places=2)
        self.assertAlmostEqual(saved.tp["levels"][2]["price"], 110.0, places=2)

    async def test_next_level_only_after_the_previous_is_hit(self):
        inst = self.make(tp_plan())
        await self.open_long(inst)
        await inst._tick_hub() if False else None
        inst.cur = snap_of(1000, dir_e=1, dir_f=1)
        await inst._tick_hub()                                      # same candle, nothing hit: nothing new
        self.assertEqual(len(inst.client.limit_calls), 1)
        oid1 = inst.hub.tp["levels"][0]["order_id"]
        self.fill(inst, oid1)
        await inst._tick_hub()
        self.assertEqual(len(inst.client.limit_calls), 2)           # TP2 now
        self.assertAlmostEqual(inst.client.limit_calls[1][3], 106.0, places=2)
        self.assertEqual([l["state"] for l in inst.hub.tp["levels"]], ["filled", "placed", "pending"])
        self.assertEqual([r.reason for r in self.records], ["TP1"])
        self.assertAlmostEqual(self.records[0].qty, 16.5, places=3)
        self.assertGreater(self.records[0].pnl, 0)

    async def test_last_level_takes_everything_left_when_pcts_add_to_100(self):
        inst = self.make(tp_plan())
        await self.open_long(inst)
        for i in range(2):
            self.fill(inst, inst.hub.tp["levels"][i]["order_id"])
            inst.cur = snap_of(1000, dir_e=1, dir_f=1)
            await inst._tick_hub()
        # the exchange shows more left than the maths says (a rounding leftover) -> TP3 takes ALL of it
        inst.client.position = {"positionAmt": "20.0", "entryPrice": "100"}
        inst.hub.tp["levels"][2]["state"] = "pending"
        await inst._hub_manage_tp()
        self.assertAlmostEqual(inst.client.limit_calls[-1][2], 20.0, places=3)

    async def test_everything_closed_by_tp_ends_the_trade_without_a_second_ledger_row(self):
        inst = self.make(tp_plan())
        await self.open_long(inst)
        for i in range(3):
            self.fill(inst, inst.hub.tp["levels"][i]["order_id"])
            inst.cur = snap_of(1000, dir_e=1, dir_f=1)
            if i < 2:
                await inst._tick_hub()
        inst.client.position = None
        await inst._tick_hub()                                      # sees the position gone
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual([r.reason for r in self.records], ["TP1", "TP2", "TP3"])
        self.assertIsNone(inst.hub.tp)

    async def test_switched_off_level_is_skipped_in_the_order(self):
        plan = tp_plan()
        plan["tp"]["levels"][1]["on"] = False
        inst = self.make(plan)
        await self.open_long(inst)
        self.fill(inst, inst.hub.tp["levels"][0]["order_id"])
        inst.cur = snap_of(1000, dir_e=1, dir_f=1)
        await inst._tick_hub()
        self.assertAlmostEqual(inst.client.limit_calls[1][3], 110.0, places=2)   # TP3 follows TP1

    async def test_tp_off_places_no_orders(self):
        inst = self.make(plan_with())
        await self.open_long(inst)
        self.assertEqual(inst.client.limit_calls, [])
        self.assertIsNone(inst.hub.tp)

    async def test_flip_cancels_tp_orders_and_records_a_fill_that_happened_meanwhile(self):
        inst = self.make(tp_plan())
        await self.open_long(inst)
        oid1 = inst.hub.tp["levels"][0]["order_id"]
        self.fill(inst, oid1)                                       # TP1 filled, bot has not looked yet
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1, close=101.0))
        self.assertEqual([r.reason for r in self.records][:1], ["TP1"])
        self.assertIn("signal_flip", [r.reason for r in self.records])
        self.assertEqual(inst.client.limit_orders[oid1]["status"], "FILLED")
        self.assertEqual(inst.state.direction, "SHORT")
        # a fresh TP memory for the new trade, old orders gone
        self.assertEqual(inst.hub.tp["direction"], "SHORT")
        open_limits = [o for o in inst.client.limit_orders.values() if o["status"] == "NEW"]
        self.assertEqual(len(open_limits), 1)
        self.assertEqual(open_limits[0]["side"], "BUY")

    async def test_flip_cancels_a_resting_tp(self):
        plan = tp_plan()
        inst = self.make(plan)
        await self.open_long(inst)
        oid1 = inst.hub.tp["levels"][0]["order_id"]
        await self.tick(inst, snap_of(2000, dir_e=0, dir_f=-1, close=99.0))     # flip: close, flat, new side
        self.assertEqual(inst.client.limit_orders[oid1]["status"], "CANCELED")  # the old trade's TP is gone
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertEqual(inst.hub.tp["direction"], "SHORT")                      # the TP memory is the new trade's

    async def test_partial_fill_is_recorded_as_it_grows(self):
        inst = self.make(tp_plan())
        await self.open_long(inst)
        oid1 = inst.hub.tp["levels"][0]["order_id"]
        self.fill(inst, oid1, qty=5.0, status="PARTIALLY_FILLED")
        inst.cur = snap_of(1000, dir_e=1, dir_f=1)
        await inst._tick_hub()
        self.assertEqual(inst.hub.tp["levels"][0]["state"], "placed")     # still working
        self.assertEqual(len(inst.client.limit_calls), 1)                 # next level NOT placed yet
        self.assertAlmostEqual(self.records[-1].qty, 5.0, places=3)
        self.fill(inst, oid1, qty=11.5)                                   # now the rest: 16.5 total
        inst.client.limit_orders[oid1]["executedQty"] = "16.5"
        await inst._tick_hub()
        self.assertAlmostEqual(sum(r.qty for r in self.records), 16.5, places=3)
        self.assertEqual(inst.hub.tp["levels"][0]["state"], "filled")

    async def test_order_cancelled_outside_is_placed_again(self):
        inst = self.make(tp_plan())
        await self.open_long(inst)
        oid1 = inst.hub.tp["levels"][0]["order_id"]
        inst.client.limit_orders[oid1]["status"] = "CANCELED"
        inst.cur = snap_of(1000, dir_e=1, dir_f=1)
        await inst._tick_hub()
        self.assertEqual(len(inst.client.limit_calls), 2)
        self.assertAlmostEqual(inst.client.limit_calls[1][3], 102.0, places=2)   # TP1 again, not TP2

    async def test_placement_failure_three_times_skips_the_level_not_the_trade(self):
        inst = self.make(tp_plan())
        inst.client.fail_limit_order = True
        await self.open_long(inst)
        inst.cur = snap_of(1000, dir_e=1, dir_f=1)
        await inst._tick_hub()
        await inst._tick_hub()
        self.assertEqual(inst.hub.tp["levels"][0]["state"], "skipped")
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(len(inst.client.stop_orders), 1)                 # the max-loss stop is untouched

    async def test_memory_survives_a_restart_and_nothing_is_placed_twice(self):
        inst = self.make(tp_plan())
        await self.open_long(inst)
        pc = tv.pair_config(symbol="BTCUSDT", enabled=True, plan=tp_plan())
        inst2 = BotInstance(account_id=inst.account_id, account_name="Main", symbol="BTCUSDT",
                            api_key="k", api_secret="s", testnet=True, pair_config=pc)
        inst2.client = inst.client
        self.assertEqual([l["state"] for l in inst2.hub.tp["levels"]], ["placed", "pending", "pending"])
        inst2.state.status, inst2.state.direction = "IN_POSITION", "LONG"
        inst2.state.entry_price, inst2.state.qty, inst2.state.bot_qty = 100.0, 50.0, 50.0
        await inst2._hub_manage_tp()
        self.assertEqual(len(inst.client.limit_calls), 1)

    async def test_tp_hit_while_flat_check_then_stop_close_uses_remaining_qty(self):
        inst = self.make(tp_plan())
        await self.open_long(inst)
        self.fill(inst, inst.hub.tp["levels"][0]["order_id"])             # 16.5 closed by TP1
        inst.state.qty = 50.0                                              # bot has not noticed yet
        inst.client.position = None                                        # then the stop closed the rest
        await inst._finalize_external_close("SL", 90.0, None, source="poll")
        reasons = [r.reason for r in self.records]
        self.assertEqual(reasons, ["TP1", "SL"])
        self.assertAlmostEqual(self.records[1].qty, 50.0 - 16.5, places=3)


class SpyFake(HubFake):
    """Records how many stops were resting at the moment an old stop was cancelled."""
    def __init__(self):
        super().__init__()
        self.stops_resting_at_cancel = []

    async def cancel_algo_order(self, algo_id):
        self.stops_resting_at_cancel.append(len(self.open_algo_orders))
        return await super().cancel_algo_order(algo_id)


class TestTrailing(TPBase):
    def trail_inst(self, **over):
        inst = self.make(plan_with(trail={"on": True, "length": 10}, **over))
        inst.client = SpyFake()
        import time
        inst.mark_feed.last_message_at = time.time()
        return inst

    async def test_long_stop_moves_up_to_the_lookback_low_and_never_before_the_new_one_is_resting(self):
        inst = self.trail_inst()
        self.go_long(inst, price=100.0)                              # stop at 90
        s = snap_of(1000, dir_e=1, dir_f=1, close=110.0)
        s.trail_low = 104.0
        inst.client.mark_price = 110.0
        await self.tick(inst, s)
        self.assertAlmostEqual(inst.state.sl_price, 104.0, places=2)
        self.assertEqual(inst.client.stops_resting_at_cancel, [2])  # new stop already resting when the old goes
        self.assertEqual(len(inst.client.open_algo_orders), 1)
        self.assertEqual(inst.state.sl_order_id, inst.client.open_algo_orders[0]["algoId"])

    async def test_stop_never_loosens(self):
        inst = self.trail_inst()
        self.go_long(inst, price=100.0)
        s = snap_of(1000, dir_e=1, dir_f=1, close=110.0)
        s.trail_low = 95.0                                           # lower than the 90? no: 95 > 90 -> moves
        inst.client.mark_price = 110.0
        await self.tick(inst, s)
        self.assertAlmostEqual(inst.state.sl_price, 95.0, places=2)
        s2 = snap_of(2000, dir_e=1, dir_f=1, close=111.0)
        s2.trail_low = 92.0                                          # lower than current 95 -> ignored
        await self.tick(inst, s2)
        self.assertAlmostEqual(inst.state.sl_price, 95.0, places=2)

    async def test_same_level_does_not_place_another_stop(self):
        inst = self.trail_inst()
        self.go_long(inst, price=100.0)
        s = snap_of(1000, dir_e=1, dir_f=1, close=110.0)
        s.trail_low = 90.0                                           # exactly the stop that is already there
        await self.tick(inst, s)
        self.assertEqual(inst.client.stop_orders, [])
        self.assertEqual(inst.client.stops_resting_at_cancel, [])

    async def test_short_stop_moves_down(self):
        inst = self.trail_inst()
        inst.client.position = {"positionAmt": "-1.0", "entryPrice": "100"}
        inst.client.mark_price = 90.0
        st = inst.state
        st.status, st.direction, st.entry_price, st.qty, st.bot_qty = "IN_POSITION", "SHORT", 100.0, 1.0, 1.0
        st.sl_order_id, st.sl_price = 5, 110.0
        inst.client.open_algo_orders.append({"algoId": 5, "orderType": "STOP_MARKET", "triggerPrice": "110",
                                             "clientAlgoId": "hullbot_fake_sl", "algoStatus": "NEW"})
        s = snap_of(1000, dir_e=-1, dir_f=-1, close=90.0)
        s.trail_high = 96.0
        await self.tick(inst, s)
        self.assertAlmostEqual(inst.state.sl_price, 96.0, places=2)
        # 2026-10-23 (owner option B): the Trailing Stop rests as a stop-LIMIT (trigger = limit)
        self.assertEqual(inst.client.stop_limit_calls[-1][1], "BUY")
        self.assertEqual(inst.client.stop_limit_calls[-1][2], inst.client.stop_limit_calls[-1][3])

    async def test_level_already_crossed_closes_at_market(self):
        inst = self.trail_inst()
        self.go_long(inst, price=100.0)
        s = snap_of(1000, dir_e=1, dir_f=1, close=105.0)
        s.trail_low = 104.0
        inst.client.mark_price = 103.0                               # already below the new level
        await self.tick(inst, s)
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(inst.state.status, "IDLE")
        self.assertIn("trailing_stop", [r.reason for r in self.records])
        self.assertEqual(inst.client.market_orders, [])              # no fresh entry on that same candle

    async def test_flip_signal_wins_over_trailing(self):
        inst = self.trail_inst()
        self.go_long(inst, price=100.0)
        s = snap_of(1000, dir_e=-1, dir_f=-1, close=105.0)
        s.trail_low = 104.0
        await self.tick(inst, s)
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(inst.state.direction, "SHORT")

    async def test_trailing_off_never_moves_the_stop(self):
        inst = self.make(plan_with())
        self.go_long(inst, price=100.0)
        s = snap_of(1000, dir_e=1, dir_f=1, close=110.0)
        s.trail_low = 104.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.sl_price, 90.0)

    async def test_max_loss_off_trailing_starts_a_stop_from_the_extreme(self):
        inst = self.trail_inst(sizing={"alloc_pct": 10.0, "leverage": 5, "stop_on": False, "stop_pct": 6.0})
        inst.client.position = {"positionAmt": "1.0", "entryPrice": "100"}
        inst.client.mark_price = 110.0
        st = inst.state
        st.status, st.direction, st.entry_price, st.qty, st.bot_qty = "IN_POSITION", "LONG", 100.0, 1.0, 1.0
        s = snap_of(1000, dir_e=1, dir_f=1, close=110.0)
        s.trail_low = 104.0
        await self.tick(inst, s)
        self.assertAlmostEqual(inst.state.sl_price, 104.0, places=2)
        # Stop Loss is a must (2026-10-26): its stop-limit rests first, then the trailing one takes over
        self.assertEqual(len(inst.client.stop_limit_calls), 2)
        self.assertAlmostEqual(inst.client.stop_limit_calls[-1][2], 104.0, places=2)   # a stop-limit (option B)
        self.assertEqual(inst.client.stop_orders, [])

    async def test_failed_move_keeps_the_old_stop(self):
        inst = self.trail_inst()
        self.go_long(inst, price=100.0)
        inst.client.fail_stop_market_order = True
        inst.client.fail_stop_limit_order = True
        s = snap_of(1000, dir_e=1, dir_f=1, close=110.0)
        s.trail_low = 104.0
        inst.client.mark_price = 110.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.sl_price, 90.0)
        self.assertEqual(inst.state.sl_order_id, 5)
        self.assertEqual(inst.state.status, "IN_POSITION")


if __name__ == "__main__":
    unittest.main()
