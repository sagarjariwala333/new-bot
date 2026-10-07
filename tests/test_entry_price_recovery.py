import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_entry_price_fix")
import tests._aiohttp_stub  # noqa: F401,E402

from tests.test_instance_safety import make_instance  # noqa: E402
import app.strategy as strat  # noqa: E402
from tests._snap import make_snap  # noqa: E402
from tests import _test_values as tv  # noqa: E402


def _long_snap(close=100.0):
    return make_snap(
        close=close, high=close + 0.5, low=close - 0.5,
        di_plus=30.0, di_minus=10.0, adx=30.0, atr=2.0,
        long_condition=True, short_condition=False,
    )


class TestEntryPriceRecovery(unittest.IsolatedAsyncioTestCase):
    """Regression tests for the fix: entry_price used to fall back straight
    to the candle's CLOSE price whenever the order response was missing a
    fill price - not a real fill, and it fed SL/TP/trailing/Max Loss Cap/
    ledger P&L for the entire life of the trade. Now mirrors the close
    side's existing recovery: real fill via get_user_trades() first, mark
    price as a fallback, candle close only as an absolute last resort."""

    async def test_avgprice_present_is_used_directly_unchanged_behavior(self):
        """The normal, common case must behave exactly as before - no
        regression for the path that already worked."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.mark_price = 105.0  # market_order returns this as avgPrice by default
        p = tv.params()

        await inst._do_enter(_long_snap(close=100.0), p)

        self.assertEqual(inst.state.entry_price, 105.0, "avgPrice must be used directly when present")

    async def test_missing_avgprice_recovers_real_fill_from_user_trades(self):
        """The core fix: when avgPrice is missing, the REAL fill (not the
        candle close) must be recovered from get_user_trades()."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.mark_price = 999.0  # deliberately different from everything else,
                                          # so accidentally using it would be obviously wrong

        original_market_order = inst.client.market_order

        async def market_order_missing_avgprice(*a, **k):
            result = await original_market_order(*a, **k)
            result.pop("avgPrice", None)
            order_id = result["orderId"]
            inst.client.user_trades_by_order_id[order_id] = [{"price": "103.7", "orderId": order_id}]
            return result

        inst.client.market_order = market_order_missing_avgprice
        p = tv.params()

        await inst._do_enter(_long_snap(close=100.0), p)

        self.assertEqual(inst.state.entry_price, 103.7,
                         "must use the REAL fill from get_user_trades, not the candle close (100.0) "
                         "or the mark price (999.0)")

    async def test_missing_avgprice_and_no_trades_falls_back_to_mark_price(self):
        """If the real-fill lookup comes back empty, mark price is the
        fallback - still not candle close."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.mark_price = 101.5

        original_market_order = inst.client.market_order

        async def market_order_missing_avgprice(*a, **k):
            result = await original_market_order(*a, **k)
            result.pop("avgPrice", None)
            return result  # no trades registered for this order_id

        inst.client.market_order = market_order_missing_avgprice
        p = tv.params()

        await inst._do_enter(_long_snap(close=100.0), p)

        self.assertEqual(inst.state.entry_price, 101.5,
                         "must fall back to mark price, not the candle close (100.0)")

    async def test_missing_avgprice_and_everything_fails_falls_back_to_candle_close_as_last_resort(self):
        """Only when BOTH the real-fill lookup AND the mark-price query fail
        does candle close get used - as a documented last resort, not the
        first thing tried. Mark price is only made to fail for the ENTRY-
        PRICE-RECOVERY call specifically (call #1) - later calls (protective
        order placement, which also needs mark price) succeed normally, so
        the entry actually completes rather than cascading into an unrelated
        unprotected/emergency-close path that would make this test check the
        wrong thing entirely."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()

        original_market_order = inst.client.market_order

        async def market_order_missing_avgprice(*a, **k):
            result = await original_market_order(*a, **k)
            result.pop("avgPrice", None)
            return result

        call_count = {"n": 0}
        original_get_mark_price = inst.client.get_mark_price

        async def get_mark_price_fails_once(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("network down")
            return await original_get_mark_price(*a, **k)

        inst.client.market_order = market_order_missing_avgprice
        inst.client.fail_get_user_trades = True
        inst.client.get_mark_price = get_mark_price_fails_once
        p = tv.params()

        await inst._do_enter(_long_snap(close=100.0), p)

        self.assertEqual(inst.state.entry_price, 100.0,
                         "candle close is only the LAST resort, used here since everything else failed")
        self.assertEqual(inst.state.status, "IN_POSITION",
                         "the entry itself must still complete normally once past the price-recovery step")


if __name__ == "__main__":
    unittest.main()
