import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

from app.ws_feed import OKXMarkPriceFeed  # noqa: E402


class TestOKXMarkPriceFeedParsing(unittest.TestCase):
    """The message-parsing logic - the part most likely to have a real bug
    (OKX's subscribe-ack-vs-push-data distinction) - pulled out into its
    own static method specifically so it's testable without needing to
    mock a live websocket connection."""

    def test_subscribe_ack_has_no_data_and_is_ignored(self):
        ack = {"event": "subscribe", "arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"}}
        self.assertIsNone(OKXMarkPriceFeed._extract_mark_price(ack))

    def test_real_push_data_extracts_the_mark_price(self):
        push = {
            "arg": {"channel": "mark-price", "instId": "BTC-USDT-SWAP"},
            "data": [{"instType": "SWAP", "instId": "BTC-USDT-SWAP", "markPx": "63451.7", "ts": "1700000000000"}],
        }
        self.assertEqual(OKXMarkPriceFeed._extract_mark_price(push), 63451.7)

    def test_empty_data_array_is_ignored(self):
        push = {"arg": {"channel": "mark-price"}, "data": []}
        self.assertIsNone(OKXMarkPriceFeed._extract_mark_price(push))

    def test_missing_markpx_field_is_ignored(self):
        push = {"data": [{"instId": "BTC-USDT-SWAP"}]}  # malformed/unexpected shape
        self.assertIsNone(OKXMarkPriceFeed._extract_mark_price(push))


class TestOKXMarkPriceFeedStaleness(unittest.TestCase):
    def test_never_received_a_message_is_stale(self):
        feed = OKXMarkPriceFeed("BTC-USDT-SWAP", stale_after_seconds=45.0)
        self.assertTrue(feed.is_stale())

    def test_recent_message_is_not_stale(self):
        feed = OKXMarkPriceFeed("BTC-USDT-SWAP", stale_after_seconds=45.0)
        feed.last_message_at = time.time()
        self.assertFalse(feed.is_stale())

    def test_old_message_is_stale(self):
        feed = OKXMarkPriceFeed("BTC-USDT-SWAP", stale_after_seconds=45.0)
        feed.last_message_at = time.time() - 60.0
        self.assertTrue(feed.is_stale())


class TestOKXMarkPriceFeedConfig(unittest.TestCase):
    def test_demo_flag_selects_the_demo_websocket_base(self):
        live = OKXMarkPriceFeed("BTC-USDT-SWAP", stale_after_seconds=45.0, demo=False)
        demo = OKXMarkPriceFeed("BTC-USDT-SWAP", stale_after_seconds=45.0, demo=True)
        self.assertEqual(live.base_ws, OKXMarkPriceFeed.MAINNET_WS)
        self.assertEqual(demo.base_ws, OKXMarkPriceFeed.DEMO_WS)
        self.assertNotEqual(live.base_ws, demo.base_ws)


if __name__ == "__main__":
    unittest.main()
