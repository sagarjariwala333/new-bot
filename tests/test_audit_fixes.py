"""Audit fixes before live trading (owner 2026-10-26: "Ok fix all").
A. A stop-limit on the exchange that the price went THROUGH (its limit did not fill) and the trade still open:
   after STOP_LIMIT_GRACE_S the bot closes at market.
B. OKX: a restart with an open position must not send a leverage (it went to 1x).
C. Binance: Isolated / Cross is never changed while a position is open; it is set at every hub entry.
D. A trade closed by the trailing level already broken / the time stop is not followed by an entry on the same
   candle (live bot, shadow and Analysis alike).
"""
import dataclasses
import unittest
from unittest import mock

from tests import test_hub_trading as T
from tests._hub_plan import make_plan
from app import strategy_hub as H
from app import shadow_book as B
from app import hub_trading


def plan_sl_only():
    p = make_plan(ind_ma1={"entry": True, "flip": True}, sizing={"stop_on": False})
    p["sl"] = {"on": True, "basis": H.BASIS_PRICE, "value": 2.0}
    return p


class TestFixA(T.Base):
    async def opened(self):
        inst = self.make(plan_sl_only())
        inst.client.mark_price = 100.0
        await self.tick(inst, T.snap_of(1000, dir_e=1, dir_f=1))
        ex = inst.hub.ladder["ex"]
        self.assertEqual((ex["name"], ex["kind"], ex["price"]), ("sl", "limit", 98.0))
        return inst

    async def test_gone_through_and_not_filled_closes_at_market_after_the_grace(self):
        inst = await self.opened()
        inst.hub.ladder["ex"]["triggered"] = True
        inst.client.open_algo_orders.clear()
        inst.client.mark_price = 90.0
        with mock.patch.object(hub_trading.time, "time", return_value=1000.0):
            self.assertFalse(await inst._hub_ladder_check(90.0))         # the limit gets its time first
        self.assertEqual(inst.client.close_orders, [])
        with mock.patch.object(hub_trading.time, "time", return_value=1000.0 + hub_trading.STOP_LIMIT_GRACE_S):
            self.assertTrue(await inst._hub_ladder_check(90.0))
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertIsNone(inst.client.position)

    async def test_price_back_above_before_the_grace_does_nothing(self):
        inst = await self.opened()
        with mock.patch.object(hub_trading.time, "time", return_value=1000.0):
            await inst._hub_ladder_check(97.5)
        self.assertIn("ex_passed_at", inst.hub.ladder)
        await inst._hub_ladder_check(99.0)
        self.assertNotIn("ex_passed_at", inst.hub.ladder)
        with mock.patch.object(hub_trading.time, "time", return_value=1010.0):
            await inst._hub_ladder_check(99.0)
        self.assertEqual(inst.client.close_orders, [])

    async def test_already_flat_sends_no_close(self):
        inst = await self.opened()
        with mock.patch.object(hub_trading.time, "time", return_value=1000.0):
            await inst._hub_ladder_check(90.0)
        inst.client.position = None                                       # the limit filled after all
        with mock.patch.object(hub_trading.time, "time", return_value=1005.0):
            await inst._hub_ladder_check(90.0)
        self.assertEqual(inst.client.close_orders, [])

    async def test_a_new_stop_clears_the_timer(self):
        inst = await self.opened()
        with mock.patch.object(hub_trading.time, "time", return_value=1000.0):
            await inst._hub_ladder_check(97.5)
        lad = inst.hub.ladder
        inst._hub_record_ex(lad, "sl", "limit", 95.0, 1.0, 999, None)
        self.assertNotIn("ex_passed_at", lad)


class TestFixBC(T.Base):
    async def test_restart_with_an_open_position_sends_no_leverage_or_margin_mode(self):
        inst = self.make()
        inst.client.position = {"positionAmt": "1.0", "entryPrice": "100"}
        inst.client.set_margin_type = mock.AsyncMock(return_value={})
        await inst._startup_reconcile()
        self.assertEqual(inst.client.leverage_calls, [])
        inst.client.set_margin_type.assert_not_awaited()

    async def test_restart_flat_sets_both(self):
        inst = self.make()
        inst.client.set_margin_type = mock.AsyncMock(return_value={})
        await inst._startup_reconcile()
        self.assertEqual(len(inst.client.leverage_calls), 1)
        inst.client.set_margin_type.assert_awaited_once()

    async def test_every_entry_sets_the_margin_mode_before_the_leverage(self):
        inst = self.make()
        order = []
        async def mm(symbol, isolated):
            order.append(("margin", isolated))
        inst.client.set_margin_type = mm
        await self.tick(inst, T.snap_of(1000, dir_e=1, dir_f=1))
        self.assertEqual(order, [("margin", inst.pc.isolated_margin)])
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_margin_mode_refused_no_entry(self):
        inst = self.make()
        inst.client.set_margin_type = mock.AsyncMock(side_effect=RuntimeError("refused"))
        await self.tick(inst, T.snap_of(1000, dir_e=1, dir_f=1))
        self.assertEqual(inst.client.market_orders, [])


class TestFixD(unittest.TestCase):
    def candle(self, book, s, plan):
        return B.on_candle(book, s, plan, tick_size=0.0, last_total=None, balance=1000.0, t_ms=s.open_time + 1)

    def snap(self, t, close, dir_e, **kw):
        return dataclasses.replace(T.snap_of(t, dir_e=dir_e, dir_f=dir_e, close=close), **kw)

    def test_time_stop_close_no_entry_on_the_same_candle(self):
        p = make_plan(ind_ma1={"entry": True, "flip": True})
        p["sl"].update(time_on=True, time_candles=1)
        p["allow_reentry"] = True
        plan = H.complete_plan(p)
        book = B.new_book(1000.0)
        self.candle(book, self.snap(1, 100.0, 1), plan)
        self.assertEqual(book["pos"], 1)
        self.candle(book, self.snap(2, 99.0, 1), plan)                    # time stop; LONG signal still on
        self.assertEqual(book["pos"], 0)
        self.candle(book, self.snap(3, 99.0, 1), plan)                    # the next candle: may enter again
        self.assertEqual(book["pos"], 1)

    def test_trailing_level_broken_no_entry_on_the_same_candle(self):
        p = make_plan(ind_ma1={"entry": True, "flip": True})
        p["trail"] = {"on": True, "type": H.TRAIL_LLHH, "length": 5}
        p["allow_reentry"] = True
        plan = H.complete_plan(p)
        book = B.new_book(1000.0)
        self.candle(book, self.snap(1, 100.0, 1, trail_low=95.0), plan)
        ev, closed = self.candle(book, self.snap(2, 96.0, 1, trail_low=97.0), plan)   # new low 97 above the close
        self.assertEqual(closed[0]["exit_reason"], "Trailing Stop")
        self.assertEqual(book["pos"], 0)
        self.candle(book, self.snap(3, 96.0, 1, trail_low=90.0), plan)
        self.assertEqual(book["pos"], 1)


if __name__ == "__main__":
    unittest.main()
