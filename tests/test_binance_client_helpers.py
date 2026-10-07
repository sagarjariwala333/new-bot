import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

from app.binance_futures import BinanceFuturesClient, CLIENT_ORDER_ID_PREFIX  # noqa: E402


class TestRounding(unittest.TestCase):
    def setUp(self):
        self.client = BinanceFuturesClient(api_key="x", api_secret="y")

    def test_qty_step_rounding_truncates_not_rounds(self):
        # 0.123456 with step 0.001 must truncate to 0.123, not round to 0.123 or 0.124
        self.assertEqual(self.client._round_step_decimal(0.123456, 0.001), 0.123)

    def test_price_tick_rounding(self):
        self.assertEqual(self.client._round_step_decimal(1234.5678, 0.01), 1234.56)

    def test_binary_float_edge_case(self):
        # Classic float-imprecision trap: 0.1 + 0.2 != 0.3 in raw binary float.
        # Decimal-based rounding must still land exactly on a valid step.
        value = 0.1 + 0.2  # 0.30000000000000004 in raw float
        self.assertEqual(self.client._round_step_decimal(value, 0.01), 0.3)

    def test_zero_step_is_a_noop(self):
        self.assertEqual(self.client._round_step_decimal(5.4321, 0), 5.4321)


class TestStopSideValidation(unittest.TestCase):
    def test_long_stop_below_mark_is_valid(self):
        ok, msg = BinanceFuturesClient.validate_stop_side("LONG", 99.0, 100.0)
        self.assertTrue(ok)

    def test_long_stop_at_or_above_mark_is_invalid(self):
        ok, msg = BinanceFuturesClient.validate_stop_side("LONG", 100.0, 100.0)
        self.assertFalse(ok)
        ok, msg = BinanceFuturesClient.validate_stop_side("LONG", 101.0, 100.0)
        self.assertFalse(ok)

    def test_short_stop_above_mark_is_valid(self):
        ok, msg = BinanceFuturesClient.validate_stop_side("SHORT", 101.0, 100.0)
        self.assertTrue(ok)

    def test_short_stop_at_or_below_mark_is_invalid(self):
        ok, msg = BinanceFuturesClient.validate_stop_side("SHORT", 100.0, 100.0)
        self.assertFalse(ok)
        ok, msg = BinanceFuturesClient.validate_stop_side("SHORT", 99.0, 100.0)
        self.assertFalse(ok)


class TestClientOrderId(unittest.TestCase):
    def test_generated_ids_carry_the_prefix_and_are_unique(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        ids = {client._new_client_order_id() for _ in range(50)}
        self.assertEqual(len(ids), 50, "generated ids must be unique")
        for oid in ids:
            self.assertTrue(oid.startswith(CLIENT_ORDER_ID_PREFIX))


if __name__ == "__main__":
    unittest.main()
