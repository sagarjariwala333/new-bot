"""2026-11-09: the exit price written to the ledger must be the REAL fill of the closing order.
The bug: a market close answered with a fill price of 0, and the fallback read the OLDEST trades of the symbol
(no order id, no start time), so a stale price became the exit (BTC short: real +8.28, ledger -14.18) and the
wrong profit fed the next entry size. Arbitrary test numbers only."""
import os
import sys
import time
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_instance_safety import make_instance  # noqa: E402


def _ledger(tag):
    import app.ledger as ledger_module
    d = Path(f"/tmp/hull_bot_test_exitfix_{tag}")
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    ledger_module.LEDGER_DIR = d / "ledger"
    ledger_module.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    return ledger_module


def _short(inst):
    inst.state.status = "CLOSING"
    inst.state.direction = "SHORT"
    inst.state.qty = 0.013
    inst.state.entry_price = 75739.5
    inst.state.closing_reason = "trailing_stop_limit_not_filled"
    inst.state.opened_at = time.time() - 3600
    inst.state.entry_order_id = 111
    inst.client.position = {"positionAmt": "-0.013", "entryPrice": "75739.5"}
    inst.client.close_result_confirms_flat = True
    inst.client.mark_price = 75030.0


class TestExitPriceIsTheRealFill(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ledger = _ledger(self._testMethodName)

    def _patch_close(self, inst, order):
        async def close_market(symbol, side, qty):
            inst.client.close_orders.append((symbol, side, qty))
            inst.client.position = None
            return order
        inst.client.close_position_market = close_market

    async def test_zero_price_in_response_uses_the_fills_of_that_order_not_old_trades(self):
        inst = make_instance()
        _short(inst)
        self._patch_close(inst, {"orderId": 900, "avgPrice": "0.00000"})
        # stale trades of other orders (this is what the old code read) and the real closing fill
        inst.client.user_trades_by_order_id = {
            1: [{"price": "76754.90", "orderId": 1, "qty": "0.013", "commission": "0.1"}],
            900: [{"price": "75027.10", "orderId": 900, "qty": "0.013", "commission": "0.1"}],
        }
        await inst._retry_close()
        t = self.ledger.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(t), 1)
        self.assertAlmostEqual(t[0].exit_price, 75027.10, places=4)
        self.assertGreater(t[0].pnl, 0)                       # a short closed lower is a profit
        self.assertNotIn(None, inst.client.get_user_trades_calls)   # never the unfiltered query

    async def test_two_partial_fills_are_averaged_by_size(self):
        inst = make_instance()
        _short(inst)
        self._patch_close(inst, {"orderId": 900, "avgPrice": "0"})
        inst.client.user_trades_by_order_id = {900: [
            {"price": "75000", "orderId": 900, "qty": "0.010", "commission": "0.1"},
            {"price": "75100", "orderId": 900, "qty": "0.003", "commission": "0.1"}]}
        await inst._retry_close()
        t = self.ledger.list_trades(inst.account_id, inst.symbol)
        self.assertAlmostEqual(t[0].exit_price, (75000 * 0.010 + 75100 * 0.003) / 0.013, places=4)

    async def test_no_fills_found_uses_the_order_own_average_price(self):
        inst = make_instance()
        _short(inst)
        self._patch_close(inst, {"orderId": 900, "avgPrice": "0"})
        inst.client.user_trades_by_order_id = {}
        inst.client.limit_orders[900] = {"orderId": 900, "avgPrice": "75027.10", "status": "FILLED"}
        await inst._retry_close()
        t = self.ledger.list_trades(inst.account_id, inst.symbol)
        self.assertAlmostEqual(t[0].exit_price, 75027.10, places=4)

    async def test_nothing_found_falls_back_to_the_mark_price_never_an_old_trade(self):
        inst = make_instance()
        _short(inst)
        self._patch_close(inst, {"orderId": 900, "avgPrice": "0"})
        inst.client.user_trades_by_order_id = {1: [{"price": "76754.90", "orderId": 1, "qty": "0.013"}]}
        await inst._retry_close()
        t = self.ledger.list_trades(inst.account_id, inst.symbol)
        self.assertAlmostEqual(t[0].exit_price, 75030.0, places=4)

    async def test_a_real_price_in_the_response_is_used_and_nothing_else_is_asked(self):
        inst = make_instance()
        _short(inst)
        self._patch_close(inst, {"orderId": 900, "avgPrice": "75027.10"})
        inst.client.user_trades_by_order_id = {1: [{"price": "76754.90", "orderId": 1, "qty": "0.013"}]}
        await inst._retry_close()
        t = self.ledger.list_trades(inst.account_id, inst.symbol)
        self.assertAlmostEqual(t[0].exit_price, 75027.10, places=4)


class TestSafetyNet(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ledger = _ledger(self._testMethodName)

    async def test_a_looked_up_price_far_from_the_market_is_not_used(self):
        inst = make_instance()
        _short(inst)

        async def close_market(symbol, side, qty):
            inst.client.position = None
            return {"orderId": 900, "avgPrice": "0"}
        inst.client.close_position_market = close_market
        # even the fills of the closing order show a price 10% away from the market: not believed
        inst.client.user_trades_by_order_id = {900: [{"price": "82500", "orderId": 900, "qty": "0.013"}]}
        await inst._retry_close()
        t = self.ledger.list_trades(inst.account_id, inst.symbol)
        self.assertAlmostEqual(t[0].exit_price, 75030.0, places=4)     # the mark price, as an estimate

    async def test_a_price_the_exchange_stated_is_never_overridden(self):
        inst = make_instance()
        _short(inst)

        async def close_market(symbol, side, qty):
            inst.client.position = None
            return {"orderId": 900, "avgPrice": "70000"}              # stated by the exchange, even if far
        inst.client.close_position_market = close_market
        await inst._retry_close()
        t = self.ledger.list_trades(inst.account_id, inst.symbol)
        self.assertAlmostEqual(t[0].exit_price, 70000.0, places=4)


class TestClosedByOthersFindsTheNewestClosingFill(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ledger = _ledger(self._testMethodName)

    async def test_no_order_id_uses_start_time_side_and_newest_order(self):
        inst = make_instance()
        _short(inst)
        now_ms = time.time() * 1000
        opened_ms = inst.state.opened_at * 1000
        inst.client.position = None
        inst.client.user_trades_by_order_id = {
            1: [{"price": "76754.90", "orderId": 1, "qty": "0.013", "side": "BUY", "time": opened_ms - 99999}],   # old
            111: [{"price": "75739.50", "orderId": 111, "qty": "0.013", "side": "SELL", "time": opened_ms + 1}],   # the entry
            800: [{"price": "75040.00", "orderId": 800, "qty": "0.013", "side": "BUY", "time": now_ms - 5000}],
            801: [{"price": "75027.10", "orderId": 801, "qty": "0.013", "side": "BUY", "time": now_ms - 1000}],    # newest close
        }
        px, oid = await inst._closing_fill_price(None, "SHORT")
        self.assertAlmostEqual(px, 75027.10, places=4)
        self.assertEqual(str(oid), "801")
        self.assertIsNotNone(inst.client.get_user_trades_start_ms[-1])    # a start time was sent

    async def test_unknown_start_of_trade_never_reads_old_trades(self):
        inst = make_instance()
        _short(inst)
        inst.state.opened_at = 0
        inst.client.user_trades_by_order_id = {1: [{"price": "76754.90", "orderId": 1, "qty": "0.013"}]}
        px, oid = await inst._closing_fill_price(None, "SHORT")
        self.assertIsNone(px)


class TestAvgFill(unittest.TestCase):
    def test_bad_rows_are_ignored(self):
        from app.instance import BotInstance as B
        self.assertIsNone(B._avg_fill([]))
        self.assertIsNone(B._avg_fill([{"price": "0"}, {"price": None}, {"price": "x"}, {"price": "nan"}]))
        self.assertEqual(B._avg_fill([{"price": "10"}]), 10.0)
        self.assertAlmostEqual(B._avg_fill([{"price": "10", "qty": "1"}, {"price": "20", "qty": "3"}]), 17.5)


if __name__ == "__main__":
    unittest.main()
