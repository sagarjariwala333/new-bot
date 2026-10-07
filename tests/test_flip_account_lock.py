import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_flip_lock")
import tests._aiohttp_stub  # noqa: F401,E402

from tests.fake_client import FakeClient  # noqa: E402
from app.instance import BotInstance  # noqa: E402
from app.store import PairConfig  # noqa: E402
from tests._snap import make_snap  # noqa: E402
from tests import _test_values as tv  # noqa: E402


class _LockSpy:
    """Wraps a real asyncio.Lock but records whether it was actually
    acquired via `async with`, so the test can prove the flip path goes
    through the SAME lock every other entry uses - not just that the code
    didn't crash."""

    def __init__(self):
        import asyncio
        self._lock = asyncio.Lock()
        self.enter_count = 0

    async def __aenter__(self):
        self.enter_count += 1
        return await self._lock.__aenter__()

    async def __aexit__(self, *a):
        return await self._lock.__aexit__(*a)


class _FakeStore:
    def get_account(self, account_id):
        return None   # no exposure cap configured


class _FakeManagerWithLockSpy:
    def __init__(self):
        self.lock_spy = _LockSpy()
        self.user_data_streams = {}
        self.store = _FakeStore()

    def get_account_lock(self, account_id):
        return self.lock_spy

    async def maybe_teardown_user_data_stream(self, account_id):
        return None


class TestFlipBaseV3(unittest.IsolatedAsyncioTestCase):
    """BASE V3 (2026-09-28): a flip only CLOSES on the signal candle - the
    new side is NOT opened inside the close path any more (Pine opens it on
    the next bar through the normal flat entry). The next-candle entry goes
    through _maybe_enter, which must still take the per-account exposure
    lock (the original purpose of this test file)."""

    def _make(self):
        pc = tv.pair_config(symbol="BTCUSDT", enabled=True)
        inst = BotInstance(account_id="acc_flip_lock", account_name="Main", symbol="BTCUSDT",
                           api_key="k", api_secret="s", testnet=True, pair_config=pc)
        inst.client = FakeClient()
        inst.manager = _FakeManagerWithLockSpy()
        return inst

    async def test_flip_close_does_not_reenter_in_the_same_close_path(self):
        inst = self._make()
        inst.state.status = "CLOSING"
        inst.state.closing_reason = "signal_flip"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.client.position = {"positionAmt": "0.1"}

        async def fake_fetch_snapshot(p):
            return make_snap(close=90.0, short_condition=True), None
        inst._fetch_snapshot = fake_fetch_snapshot

        await inst._retry_close()

        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(inst.client.market_orders, [], "no new position may open inside the flip close")
        self.assertEqual(inst.manager.lock_spy.enter_count, 0)

    async def test_next_candle_entry_acquires_the_account_lock(self):
        import time
        inst = self._make()
        inst.mark_feed.last_message_at = time.time()
        await inst._maybe_enter(make_snap(close=90.0, short_condition=True), inst_params(inst))
        self.assertGreaterEqual(inst.manager.lock_spy.enter_count, 1,
                                "every entry must go through the per-account lock")
        self.assertEqual(inst.state.direction, "SHORT")


def inst_params(inst):
    from app.instance import _params_from_pair
    return _params_from_pair(inst.pc)


if __name__ == "__main__":
    unittest.main()
