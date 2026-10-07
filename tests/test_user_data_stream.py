"""
test_user_data_stream.py
=========================

Tests UserDataStream's dispatch/registration logic directly (no real
network) - the connection/reconnect loop itself can't be tested without a
real Binance connection, but the message-routing logic (which symbol gets
which event, listenKeyExpired handling) is fully testable and safety-
critical to get right.
"""

import asyncio
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

from app.user_data_stream import UserDataStream  # noqa: E402


class _FakeListenKeyClient:
    async def create_listen_key(self):
        return "fake_key"

    async def keepalive_listen_key(self, listen_key):
        return {}

    async def close_listen_key(self, listen_key):
        return {}


class TestUserDataStreamDispatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stream = UserDataStream(_FakeListenKeyClient(), testnet=True)

    async def test_algo_update_routes_to_the_matching_symbol_only(self):
        received = {"btc": None, "eth": None}

        async def btc_handler(o):
            received["btc"] = o

        async def eth_handler(o):
            received["eth"] = o

        self.stream.register("BTCUSDT", on_algo_update=btc_handler)
        self.stream.register("ETHUSDT", on_algo_update=eth_handler)

        await self.stream._dispatch({"e": "ALGO_UPDATE", "o": {"s": "BTCUSDT", "X": "TRIGGERED"}})

        self.assertIsNotNone(received["btc"], "the BTCUSDT handler must receive its own symbol's event")
        self.assertIsNone(received["eth"], "a different symbol's handler must not receive this event")

    async def test_order_update_routes_to_the_matching_symbol_only(self):
        received = []

        async def handler(o):
            received.append(o)

        self.stream.register("BTCUSDT", on_order_update=handler)
        await self.stream._dispatch({"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "X": "FILLED"}})
        await self.stream._dispatch({"e": "ORDER_TRADE_UPDATE", "o": {"s": "ETHUSDT", "X": "FILLED"}})

        self.assertEqual(len(received), 1, "must only fire for the registered symbol")

    async def test_unregistered_symbol_is_silently_ignored(self):
        # No handlers registered at all - must not raise.
        await self.stream._dispatch({"e": "ALGO_UPDATE", "o": {"s": "BTCUSDT", "X": "TRIGGERED"}})

    async def test_listen_key_expired_raises_to_force_a_reconnect(self):
        with self.assertRaises(ConnectionError):
            await self.stream._dispatch({"e": "listenKeyExpired"})

    async def test_unregister_removes_the_symbol(self):
        async def handler(o):
            pass

        self.stream.register("BTCUSDT", on_algo_update=handler)
        self.assertTrue(self.stream.has_subscribers())
        self.stream.unregister("BTCUSDT")
        self.assertFalse(self.stream.has_subscribers())

    async def test_register_is_case_insensitive_on_symbol(self):
        received = []

        async def handler(o):
            received.append(o)

        self.stream.register("btcusdt", on_algo_update=handler)
        await self.stream._dispatch({"e": "ALGO_UPDATE", "o": {"s": "BTCUSDT", "X": "TRIGGERED"}})

        self.assertEqual(len(received), 1)


class TestUnhealthyStreamNotification(unittest.IsolatedAsyncioTestCase):
    """2026-09-15 fix (flagged across two review rounds): a reconnect used
    to only ever be logged, never escalated. _notify_unhealthy is the part
    of that fix testable without a real connection (the threshold-counting
    logic lives inside _run's try/except, which - per this file's own
    docstring - genuinely needs a real connection to exercise naturally)."""

    def setUp(self):
        self.stream = UserDataStream(_FakeListenKeyClient(), testnet=True)

    async def test_notifies_every_registered_symbol(self):
        received = []

        async def cb_a(msg):
            received.append(("A", msg))

        async def cb_b(msg):
            received.append(("B", msg))

        self.stream.register("BTCUSDT", on_unhealthy=cb_a)
        self.stream.register("ETHUSDT", on_unhealthy=cb_b)

        await self.stream._notify_unhealthy("test message")

        self.assertEqual({r[0] for r in received}, {"A", "B"})
        self.assertTrue(all(r[1] == "test message" for r in received))

    async def test_symbol_with_no_unhealthy_callback_is_skipped_silently(self):
        self.stream.register("BTCUSDT", on_algo_update=lambda o: None)  # no on_unhealthy
        await self.stream._notify_unhealthy("test message")  # must not raise

    async def test_one_callback_raising_does_not_stop_the_others(self):
        received = []

        async def raising_cb(msg):
            raise RuntimeError("boom")

        async def working_cb(msg):
            received.append(msg)

        self.stream.register("BTCUSDT", on_unhealthy=raising_cb)
        self.stream.register("ETHUSDT", on_unhealthy=working_cb)

        await self.stream._notify_unhealthy("test message")  # must not raise

        self.assertEqual(received, ["test message"])


class TestKeepaliveFailureForcesReconnect(unittest.IsolatedAsyncioTestCase):
    """Item 4 fix: a failed keepalive must force the connection closed
    (triggering the main loop's reconnect-with-a-fresh-key path) rather
    than just logging a warning and leaving a soon-to-expire connection
    running with no explicit trigger to replace it."""

    async def test_keepalive_failure_closes_the_websocket(self):
        client = _FakeListenKeyClient()

        async def failing_keepalive(listen_key):
            raise RuntimeError("simulated keepalive failure")

        client.keepalive_listen_key = failing_keepalive
        stream = UserDataStream(client, testnet=True)
        stream._listen_key = "fake_key"

        closed = {"called": False}

        class _FakeWs:
            async def close(self):
                closed["called"] = True

        with unittest.mock.patch("app.user_data_stream.asyncio.sleep", new=unittest.mock.AsyncMock()):
            await stream._keepalive_loop(_FakeWs())

        self.assertTrue(closed["called"], "a failed keepalive must force-close the connection")

    async def test_successful_keepalive_does_not_close_the_websocket(self):
        """Regression guard: only a FAILURE should force a close - a
        successful keepalive must let the connection keep running
        undisturbed."""
        client = _FakeListenKeyClient()  # keepalive_listen_key succeeds by default
        stream = UserDataStream(client, testnet=True)
        stream._listen_key = "fake_key"

        closed = {"called": False}

        class _FakeWs:
            async def close(self):
                closed["called"] = True

        call_count = {"n": 0}

        async def sleep_then_stop(*a, **k):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise asyncio.CancelledError()  # stop the infinite loop after one successful iteration

        with unittest.mock.patch("app.user_data_stream.asyncio.sleep", new=sleep_then_stop):
            with self.assertRaises(asyncio.CancelledError):
                await stream._keepalive_loop(_FakeWs())

        self.assertFalse(closed["called"], "a successful keepalive must not close the connection")


if __name__ == "__main__":
    unittest.main()
