"""Part of a trade closed OUTSIDE the bot (owner 2026-10-24: "Fix both"): the part goes into the ledger and into
the fixed-base trade total. Take-profit fills and a triggered stop-limit are not outside closes.
Arbitrary test numbers only."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_ladder import LadderBase, sl, PRICE  # noqa: E402  (stubs + temp data dir)


class TestOutsidePartial(LadderBase):
    async def opened(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)                    # LONG 50 @ 100
        inst.hub.trade_amount = 1000.0                   # as with the fixed base amount method
        inst.hub.trade_pnl = 0.0
        self.records.clear()
        return inst

    def manual_fill(self, inst, qty, price, fid=1, oid=999, fee="0.5", side="SELL"):
        inst.client.user_trades_by_order_id.setdefault(oid, []).append(
            {"id": fid, "orderId": oid, "side": side, "price": str(price), "qty": str(qty), "commission": fee,
             "time": time.time() * 1000 + 1000})

    async def test_hand_close_goes_to_ledger_and_trade_total(self):
        inst = await self.opened()
        self.manual_fill(inst, 20, 103.0)
        inst.client.position = {"positionAmt": "30", "entryPrice": "100"}
        await inst._check_position_size_changes(inst.client.position)
        self.assertEqual(len(self.records), 1)
        r = self.records[0]
        self.assertEqual((r.reason, r.qty, r.exit_price, r.confirmed), ("manual_partial_close", 20.0, 103.0, True))
        self.assertAlmostEqual(r.pnl, 3.0 * 20 - 0.5)
        self.assertTrue(r.commission_included)
        self.assertAlmostEqual(inst.hub.trade_pnl, 59.5)
        self.assertEqual(inst.state.qty, 30.0)
        self.assertEqual(inst.hub.outside_closed_qty, 20.0)

    async def test_never_recorded_twice(self):
        inst = await self.opened()
        self.manual_fill(inst, 20, 103.0)
        inst.client.position = {"positionAmt": "30", "entryPrice": "100"}
        await inst._check_position_size_changes(inst.client.position)
        await inst._check_position_size_changes(inst.client.position)
        self.assertEqual(len(self.records), 1)
        self.manual_fill(inst, 10, 98.0, fid=2, oid=1000, fee="0.2")      # a second hand close later
        inst.client.position = {"positionAmt": "20", "entryPrice": "100"}
        await inst._check_position_size_changes(inst.client.position)
        self.assertEqual(len(self.records), 2)
        self.assertEqual((self.records[1].qty, self.records[1].exit_price), (10.0, 98.0))
        self.assertAlmostEqual(inst.hub.trade_pnl, 59.5 + (-2.0 * 10 - 0.2))

    async def test_no_fill_found_uses_the_mark_price_as_an_estimate(self):
        inst = await self.opened()
        inst.client.mark_price = 101.0
        inst.client.position = {"positionAmt": "40", "entryPrice": "100"}
        await inst._check_position_size_changes(inst.client.position)
        r = self.records[0]
        self.assertEqual((r.qty, r.exit_price, r.confirmed, r.commission_included), (10.0, 101.0, False, False))
        self.assertAlmostEqual(r.pnl, 10.0)

    async def test_bot_orders_and_wrong_side_fills_are_not_taken(self):
        inst = await self.opened()
        self.manual_fill(inst, 20, 110.0, fid=7, oid=inst.state.entry_order_id)   # the entry order itself
        self.manual_fill(inst, 20, 90.0, fid=8, oid=555, side="BUY")              # opening side, not a close
        inst.client.mark_price = 102.0
        inst.client.position = {"positionAmt": "30", "entryPrice": "100"}
        await inst._check_position_size_changes(inst.client.position)
        self.assertEqual(self.records[0].exit_price, 102.0)
        self.assertFalse(self.records[0].confirmed)

    async def test_take_profit_fill_is_not_an_outside_close(self):
        inst = await self.opened()
        inst.hub.tp = {"direction": "LONG", "entry_price": 100.0, "qty0": 50.0, "levels": [
            {"index": 1, "on": True, "price": 104.0, "pct": 40.0, "state": "filled", "order_id": 77, "qty": 20.0,
             "recorded_qty": 20.0, "attempts": 0}]}
        inst.client.position = {"positionAmt": "30", "entryPrice": "100"}
        await inst._check_position_size_changes(inst.client.position)
        self.assertEqual(self.records, [])
        self.assertEqual(inst.state.qty, 30.0)

    async def test_triggered_stop_limit_is_the_stop_not_an_outside_close(self):
        inst = await self.opened()
        inst.hub.ladder["ex"] = dict(inst.hub.ladder["ex"] or {}, triggered=True)
        inst.client.position = {"positionAmt": "30", "entryPrice": "100"}
        await inst._check_position_size_changes(inst.client.position)
        self.assertEqual(self.records, [])

    async def test_new_trade_starts_from_zero(self):
        inst = await self.opened()
        inst.hub.outside_closed_qty, inst.hub.outside_fill_ids = 5.0, ["x"]
        inst.client.position = None
        await inst._close_position(reason="test")
        inst.client.position = None
        inst.hub.reentry_block = 0                       # (Allow Mid-Trend Re-entry is not what is tested here)
        await self.open_long_at(inst, t=5000)
        self.assertEqual((inst.hub.outside_closed_qty, inst.hub.outside_fill_ids), (0.0, []))


if __name__ == "__main__":
    unittest.main()
