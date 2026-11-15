"""API-ban guard: budget, pause on 429/418, urgent vs normal, weights (2026-11-11)."""
import asyncio
import unittest

import tests._aiohttp_stub  # noqa
from app import api_guard
from app.api_guard import ApiGuard, binance_weight, binance_urgent


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make(limit=100, **kw):
    c = Clock()
    g = ApiGuard("binance", limit, clock=c.now, sleep=c.sleep, jitter=False, **kw)
    return g, c


class WeightTests(unittest.TestCase):
    def test_kline_weights(self):
        self.assertEqual(binance_weight("GET", "/fapi/v1/klines", {"limit": 99}), 1)
        self.assertEqual(binance_weight("GET", "/fapi/v1/klines", {"limit": 100}), 2)
        self.assertEqual(binance_weight("GET", "/fapi/v1/klines", {"limit": 499}), 2)
        self.assertEqual(binance_weight("GET", "/fapi/v1/klines", {"limit": 500}), 5)
        self.assertEqual(binance_weight("GET", "/fapi/v1/klines", {"limit": 1000}), 5)
        self.assertEqual(binance_weight("GET", "/fapi/v1/klines", {"limit": 1001}), 10)
        self.assertEqual(binance_weight("GET", "/fapi/v2/positionRisk", {}), 5)

    def test_urgent(self):
        self.assertTrue(binance_urgent("POST", "/fapi/v1/order"))
        self.assertTrue(binance_urgent("DELETE", "/fapi/v1/allOpenOrders"))
        self.assertFalse(binance_urgent("GET", "/fapi/v1/klines"))
        self.assertFalse(binance_urgent("GET", "/fapi/v2/positionRisk"))


class BudgetTests(unittest.TestCase):
    def test_normal_waits_above_soft_urgent_does_not(self):
        g, c = make(100, soft_ratio=0.5, hard_ratio=0.9)
        async def go():
            await g.acquire(45)
            self.assertEqual(c.slept, [])
            await g.acquire(10, urgent=True)       # 55 <= 90 -> no wait
            self.assertEqual(c.slept, [])
            t0 = c.t
            await g.acquire(10)                     # 65 > 50 -> waits for the window to free
            self.assertGreater(c.t - t0, 0)
        asyncio.run(go())

    def test_header_number_is_trusted(self):
        g, c = make(100, soft_ratio=0.5)
        g.note_reported(80)
        async def go():
            t0 = c.t
            await g.acquire(1)
            self.assertGreater(c.t - t0, 0)
        asyncio.run(go())

    def test_window_forgets(self):
        g, c = make(100)
        async def go():
            await g.acquire(50)
            c.t += 61
            self.assertEqual(g.used(), 0)
        asyncio.run(go())


class PauseTests(unittest.TestCase):
    def test_429_pauses_everyone_and_alerts_once(self):
        g, c = make(100)
        msgs = []
        g.alert_hook = msgs.append
        g.trigger_pause(20, 429)
        g.trigger_pause(20, 429)
        self.assertEqual(len(msgs), 1)
        async def go():
            t0 = c.t
            await g.acquire(1)
            self.assertGreaterEqual(c.t - t0, 20)
            t0 = c.t
            g.trigger_pause(10, 429)
            await g.acquire(1, urgent=True)
            self.assertGreaterEqual(c.t - t0, 10)
        asyncio.run(go())

    def test_ban_lets_one_urgent_attempt_per_gap(self):
        g, c = make(100)
        g.alert_hook = lambda m: None
        g.trigger_pause(3600, 418)
        self.assertTrue(g.banned)
        async def go():
            t0 = c.t
            await g.acquire(1, urgent=True)         # first careful attempt is allowed at once
            self.assertLess(c.t - t0, 1)
            t1 = c.t
            await g.acquire(1, urgent=True)         # next must wait ATTEMPT_GAP
            self.assertGreaterEqual(c.t - t1, api_guard.ATTEMPT_GAP - 1)
        asyncio.run(go())

    def test_alert_hook_failure_is_harmless(self):
        g, c = make(100)
        def boom(m):
            raise RuntimeError("x")
        g.alert_hook = boom
        g.trigger_pause(5, 429)

    def test_ban_message_parse(self):
        import time
        ms = int((time.time() + 600) * 1000)
        s = api_guard.ban_seconds_from_message({"msg": f"Way too many requests; IP banned until {ms}."})
        self.assertTrue(590 < s < 615)
        self.assertEqual(api_guard.ban_seconds_from_message({"msg": "x"}), 0.0)

    def test_retry_after(self):
        self.assertEqual(api_guard.retry_after_seconds({"Retry-After": "7"}, 3), 7.0)
        self.assertEqual(api_guard.retry_after_seconds({}, 3), 3)


class ClientWiringTests(unittest.TestCase):
    """A 429 seen by one pair pauses the shared guard for all pairs."""
    def test_429_response_sets_pause(self):
        from app.binance_futures import BinanceFuturesClient, BinanceAPIError
        api_guard.reset_guards_for_tests()

        class Resp:
            status = 429
            headers = {"Retry-After": "9"}
            async def json(self, content_type=None):
                return {"code": -1003, "msg": "Too many requests"}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class Sess:
            def request(self, *a, **k): return Resp()

        c = BinanceFuturesClient(api_key="x", api_secret="y")
        g = api_guard.get_guard("binance")
        g.alert_hook = lambda m: None
        async def go():
            with self.assertRaises(BinanceAPIError):
                await c._send_guarded(Sess(), g, "GET", "http://x/fapi/v1/klines", {})
        asyncio.run(go())
        self.assertTrue(g.is_paused())
        api_guard.reset_guards_for_tests()


if __name__ == "__main__":
    unittest.main()
