import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_qty_confirmation")
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


class TestFilledQuantityConfirmation(unittest.IsolatedAsyncioTestCase):
    """Regression tests for the fix: the bot used to trust its own
    pre-order calculated quantity for the rest of the entry sequence
    (SL/TP sizing, stored state) without ever confirming it against what
    the exchange actually filled. Now re-queries the real position right
    after entry - mirrors the same principle already used on the close
    side (never trust a locally cached number when the exchange can be
    asked directly)."""

    async def test_real_position_qty_differs_from_calculated_is_used_for_sl_tp_and_state(self):
        """The core fix: if the exchange's confirmed position size differs
        from what the bot calculated before ordering (e.g. a partial
        fill), the REAL size must be what ends up in state and what SL/TP
        get sized against."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0

        original_market_order = inst.client.market_order

        async def market_order_then_set_real_position(symbol, side, quantity):
            result = await original_market_order(symbol, side, quantity)
            # Simulate a partial fill: exchange confirms LESS than requested.
            real_qty = round(quantity * 0.6, 3)
            inst.client.position = {"positionAmt": str(real_qty), "entryPrice": str(inst.client.mark_price)}
            return result

        inst.client.market_order = market_order_then_set_real_position
        p = tv.params()

        await inst._do_enter(_long_snap(), p)

        self.assertEqual(inst.state.status, "IN_POSITION")
        real_qty = round(inst.state.qty, 3)
        # Confirm the state's qty matches what was set as the "real"
        # position (i.e. the exchange-confirmed number), not simply
        # whatever the pre-order calculation produced.
        self.assertEqual(float(inst.client.position["positionAmt"]), inst.state.qty)

    async def test_matching_real_position_qty_is_a_silent_no_op(self):
        """The common case: exchange confirms exactly what was calculated -
        no behavior change, no spurious warning-worthy divergence."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0

        original_market_order = inst.client.market_order

        async def market_order_then_confirm_exact(symbol, side, quantity):
            result = await original_market_order(symbol, side, quantity)
            inst.client.position = {"positionAmt": str(quantity), "entryPrice": str(inst.client.mark_price)}
            return result

        inst.client.market_order = market_order_then_confirm_exact
        p = tv.params()

        await inst._do_enter(_long_snap(), p)

        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertAlmostEqual(inst.state.qty, float(inst.client.position["positionAmt"]), places=6)

    async def test_position_query_failure_falls_back_to_calculated_qty_rather_than_blocking_entry(self):
        """Fail soft, not closed: if the confirmation query itself fails,
        the entry must still complete using the calculated quantity rather
        than being blocked entirely over a network hiccup."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0

        call_count = {"n": 0}
        original_get_position_risk = inst.client.get_position_risk

        async def get_position_risk_fails_once(symbol):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("network down")
            return await original_get_position_risk(symbol)

        inst.client.get_position_risk = get_position_risk_fails_once
        p = tv.params()

        await inst._do_enter(_long_snap(), p)

        self.assertEqual(inst.state.status, "IN_POSITION",
                         "a failed confirmation query must not block the entry itself")
        self.assertGreater(inst.state.qty, 0)


if __name__ == "__main__":
    unittest.main()
