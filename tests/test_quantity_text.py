"""2026-11-09: AVAXUSDT entries were refused by Binance (-1111 "Precision is over the maximum defined for this asset").
The quantity went out as "33.0"; a coin whose quantity step is 1 needs "33". These check what is SENT.
They do NOT prove the exchange accepts it: that needs a real order on Binance."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_qty_text")
import tests._aiohttp_stub  # noqa: F401,E402

from tests.test_algo_order_api import _RecordingClient  # noqa: E402


class TestPlainNumber(unittest.TestCase):
    def test_values(self):
        f = _RecordingClient._plain_number
        self.assertEqual(f(33.0), "33")
        self.assertEqual(f(33), "33")
        self.assertEqual(f(100.0), "100")
        self.assertEqual(f(1000.0), "1000")
        self.assertEqual(f(0.013), "0.013")
        self.assertEqual(f(0.85), "0.85")
        self.assertEqual(f(20.5), "20.5")
        self.assertEqual(f(0.00001), "0.00001")
        self.assertEqual(f(0.016), "0.016")
        self.assertEqual(f(102.5), "102.5")
        self.assertEqual(f(1e-05), "0.00001")
        self.assertEqual(f(0.0), "0")


class TestQuantitySentAsPlainText(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = _RecordingClient(api_key="x", api_secret="y")
        self.client.canned_response = {"orderId": 1, "status": "NEW", "algoId": 2}

    async def test_market_entry_whole_number(self):
        await self.client.market_order("AVAXUSDT", "SELL", 33.0)
        self.assertEqual(self.client.calls[-1][2]["quantity"], "33")

    async def test_market_entry_with_decimals_unchanged(self):
        await self.client.market_order("BTCUSDT", "BUY", 0.013)
        self.assertEqual(self.client.calls[-1][2]["quantity"], "0.013")

    async def test_market_close_whole_number(self):
        await self.client.close_position_market("AVAXUSDT", "BUY", 33.0)
        self.assertEqual(self.client.calls[-1][2]["quantity"], "33")

    async def test_stop_limit_whole_number(self):
        await self.client.stop_limit_order("AVAXUSDT", "BUY", 20.5, 20.6, 33.0)
        self.assertEqual(self.client.calls[-1][2]["quantity"], "33")

    async def test_limit_reduce_whole_number(self):
        await self.client.limit_reduce_order("AVAXUSDT", "BUY", 16.0, 20.0)
        p = self.client.calls[-1][2]
        self.assertEqual((p["quantity"], p["price"]), ("16", "20"))


if __name__ == "__main__":
    unittest.main()
