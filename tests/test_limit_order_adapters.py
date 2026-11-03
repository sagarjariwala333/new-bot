"""Request shape of the new reduce-only LIMIT order calls (Strategy Hub take profit) on both adapters.
These check what is SENT and how the answer is read. They do NOT prove the exchange accepts it:
that needs Binance testnet / OKX demo."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_limit_adapters")
import tests._aiohttp_stub  # noqa: F401,E402

from tests.test_algo_order_api import _RecordingClient  # noqa: E402
from tests.test_okx_futures import _install_fake_session  # noqa: E402
from app.okx_futures import OKXFuturesClient  # noqa: E402
from app.binance_futures import SymbolInfo, BinanceAPIError  # noqa: E402


class TestBinanceLimitReduce(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = _RecordingClient(api_key="x", api_secret="y")

    async def test_limit_reduce_order_shape(self):
        self.client.canned_response = {"orderId": 5, "status": "NEW"}
        out = await self.client.limit_reduce_order("BTCUSDT", "SELL", 0.016, 102.5)
        method, path, params = self.client.calls[-1]
        self.assertEqual((method, path), ("POST", "/fapi/v1/order"))
        self.assertEqual(params["type"], "LIMIT")
        self.assertEqual(params["timeInForce"], "GTC")
        self.assertEqual(params["reduceOnly"], "true")
        self.assertEqual(params["side"], "SELL")
        self.assertEqual(params["quantity"], "0.016")
        self.assertEqual(params["price"], "102.5")
        self.assertTrue(params["newClientOrderId"].startswith("hullbot_"))
        self.assertNotIn("closePosition", params)
        self.assertEqual(out["orderId"], 5)

    async def test_numbers_are_plain_decimals_never_exponent(self):
        await self.client.limit_reduce_order("BTCUSDT", "SELL", 0.00001, 0.00002)
        params = self.client.calls[-1][2]
        self.assertEqual(params["quantity"], "0.00001")
        self.assertEqual(params["price"], "0.00002")

    async def test_get_order_by_id(self):
        self.client.canned_response = {"orderId": 5, "status": "FILLED", "executedQty": "0.016", "avgPrice": "102.5"}
        o = await self.client.get_order("BTCUSDT", 5)
        method, path, params = self.client.calls[-1]
        self.assertEqual((method, path), ("GET", "/fapi/v1/order"))
        self.assertEqual(params, {"symbol": "BTCUSDT", "orderId": 5})
        self.assertEqual(o["status"], "FILLED")

    async def test_get_order_gone_is_none(self):
        async def boom(*a, **k):
            raise BinanceAPIError(400, {"code": -2013, "msg": "Order does not exist."})
        self.client._request = boom
        self.assertIsNone(await self.client.get_order("BTCUSDT", 5))

    async def test_cancel_regular_order_shape(self):
        await self.client.cancel_regular_order("BTCUSDT", 5)
        method, path, params = self.client.calls[-1]
        self.assertEqual((method, path), ("DELETE", "/fapi/v1/order"))
        self.assertEqual(params, {"symbol": "BTCUSDT", "orderId": 5})


class TestOKXLimitReduce(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        info = SymbolInfo(symbol="BTC-USDT-SWAP", price_tick=0.1, qty_step=0.01, min_qty=0.01, min_notional=0.0,
                          market_qty_step=0.01, market_min_qty=0.01, market_max_qty=float("inf"),
                          max_notional=float("inf"))
        info.ct_val = 0.01
        self.client._symbol_cache["BTC-USDT-SWAP"] = info
        self.client._leverage_state["BTC-USDT-SWAP"] = ("isolated", 5)

    async def test_limit_reduce_order_shape(self):
        ok = {"code": "0", "msg": "", "data": [{"ordId": "123", "clOrdId": "x", "sCode": "0", "sMsg": ""}]}
        fake = _install_fake_session(self.client, [(200, ok)])
        out = await self.client.limit_reduce_order("BTC-USDT-SWAP", "SELL", 0.05, 102.5)
        body = json.loads(fake.calls[0]["data"])
        self.assertEqual(body["ordType"], "limit")
        self.assertEqual(body["side"], "sell")
        self.assertEqual(body["sz"], "5")                  # 0.05 BTC / 0.01 per contract
        self.assertEqual(body["px"], "102.5")
        self.assertIs(body["reduceOnly"], True)
        self.assertEqual(body["instId"], "BTC-USDT-SWAP")
        self.assertEqual(out["orderId"], "123")

    async def test_get_order_is_shaped_like_binance(self):
        payload = {"code": "0", "msg": "", "data": [{"ordId": "123", "state": "partially_filled", "sz": "5",
                                                       "accFillSz": "2", "avgPx": "102.5"}]}
        _install_fake_session(self.client, [(200, payload)])
        o = await self.client.get_order("BTC-USDT-SWAP", "123")
        self.assertEqual(o["status"], "PARTIALLY_FILLED")
        self.assertAlmostEqual(o["executedQty"], 0.02, places=6)
        self.assertAlmostEqual(o["origQty"], 0.05, places=6)
        self.assertEqual(o["avgPrice"], "102.5")

    async def test_state_map(self):
        for state, expect in (("live", "NEW"), ("filled", "FILLED"), ("canceled", "CANCELED")):
            payload = {"code": "0", "msg": "", "data": [{"ordId": "1", "state": state, "sz": "5", "accFillSz": "0", "avgPx": ""}]}
            _install_fake_session(self.client, [(200, payload)])
            self.assertEqual((await self.client.get_order("BTC-USDT-SWAP", "1"))["status"], expect)

    async def test_cancel_regular_order_shape(self):
        ok = {"code": "0", "msg": "", "data": [{"ordId": "123", "sCode": "0", "sMsg": ""}]}
        fake = _install_fake_session(self.client, [(200, ok)])
        await self.client.cancel_regular_order("BTC-USDT-SWAP", "123")
        body = json.loads(fake.calls[0]["data"])
        self.assertEqual(body, {"instId": "BTC-USDT-SWAP", "ordId": "123"})
        self.assertIn("cancel-order", fake.calls[0]["url"] if "url" in fake.calls[0] else str(fake.calls[0]))


if __name__ == "__main__":
    unittest.main()
