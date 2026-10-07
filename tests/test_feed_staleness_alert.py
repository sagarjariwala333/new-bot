import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_feed_staleness_alert")
import tests._aiohttp_stub  # noqa: F401,E402

from tests.test_instance_safety import make_instance  # noqa: E402


class TestFeedStalenessEscalation(unittest.IsolatedAsyncioTestCase):
    """Regression tests for the fix: a stale mark-price feed used to only
    ever surface when an entry happened to be attempted - a sustained
    outage during a quiet period (no qualifying signal) could go unnoticed.
    Now checked every tick, edge-triggered (fires once on becoming stale,
    not every tick while it stays stale)."""

    async def test_becoming_stale_sends_exactly_one_alert(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = 0.0  # never received a message - stale
        notified = []

        async def spy_notify(coro):
            await coro
            notified.append(True)
        inst._notify = spy_notify

        await inst._tick()
        await inst._tick()
        await inst._tick()

        self.assertEqual(len(notified), 1, "must alert once for this streak, not on every tick")

    async def test_recovering_then_going_stale_again_alerts_a_second_time(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = 0.0
        notified = []

        async def spy_notify(coro):
            await coro
            notified.append(True)
        inst._notify = spy_notify

        await inst._tick()  # stale -> alert #1
        inst.mark_feed.last_message_at = time.time()  # recovers
        await inst._tick()  # not stale -> no alert, resets the "already alerted" flag
        inst.mark_feed.last_message_at = 0.0  # goes stale again
        await inst._tick()  # stale again -> a genuinely NEW streak -> alert #2

        self.assertEqual(len(notified), 2)

    async def test_never_stale_never_alerts(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()

        # BASE V3: the synthetic klines can produce a real Base V3 signal,
        # whose ENTRY notification is legitimate and not what this test is
        # about - entries are stubbed out so only staleness alerts count.
        async def no_entry(*a, **k):
            return None
        inst._maybe_enter = no_entry
        notified = []

        async def spy_notify(coro):
            await coro
            notified.append(True)
        inst._notify = spy_notify

        await inst._tick()
        await inst._tick()

        self.assertEqual(notified, [])

    async def test_dashboard_status_reflects_staleness_even_without_an_entry_attempt(self):
        """Before this fix, state.feed_stale only updated inside _do_enter -
        meaning it could show an outdated dashboard status if no entry was
        being attempted. Confirms it's now current after any tick."""
        inst = make_instance()
        inst.mark_feed.last_message_at = 0.0

        await inst._tick()

        self.assertTrue(inst.state.feed_stale)


if __name__ == "__main__":
    unittest.main()
