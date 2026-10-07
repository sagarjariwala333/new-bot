import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

from tests.test_instance_safety import make_instance  # noqa: E402
from app.instance import MAX_CONSECUTIVE_TICK_FAILURES  # noqa: E402


class TestConsecutiveTickFailureEscalation(unittest.IsolatedAsyncioTestCase):
    """2026-09-16 fix (pre-live audit finding #1, confirmed real): the main
    tick loop previously logged, alerted, slept, and retried FOREVER on any
    exception - no counter, no threshold, no escalation to a hard stop.
    A persistent bug or a genuinely broken exchange response would alert
    endlessly without the instance ever stopping itself."""

    def setUp(self):
        self.inst = make_instance()
        self.inst.state.last_error = "simulated failure"
        self.notified = []

        async def fake_notify(coro):
            self.notified.append(await coro)
        self.inst._notify = fake_notify

    async def test_does_not_escalate_before_the_threshold(self):
        for _ in range(MAX_CONSECUTIVE_TICK_FAILURES - 1):
            escalated = await self.inst._escalate_on_repeated_tick_failure()
            self.assertFalse(escalated)
        self.assertEqual(self.inst.state.status, "IDLE",
                         "must not have changed status before the threshold is crossed")
        self.assertEqual(self.notified, [], "must not send an escalation alert before the threshold")

    async def test_escalates_exactly_at_the_threshold(self):
        for _ in range(MAX_CONSECUTIVE_TICK_FAILURES - 1):
            await self.inst._escalate_on_repeated_tick_failure()
        escalated = await self.inst._escalate_on_repeated_tick_failure()
        self.assertTrue(escalated)
        self.assertEqual(self.inst.state.status, "ERROR")
        self.assertEqual(len(self.notified), 1)

    async def test_a_success_resets_the_streak(self):
        for _ in range(MAX_CONSECUTIVE_TICK_FAILURES - 1):
            await self.inst._escalate_on_repeated_tick_failure()
        # Simulates what the tick loop itself does on a successful tick.
        self.inst._consecutive_tick_failures = 0

        # A fresh streak must need the FULL threshold again, not just one more.
        for _ in range(MAX_CONSECUTIVE_TICK_FAILURES - 1):
            escalated = await self.inst._escalate_on_repeated_tick_failure()
            self.assertFalse(escalated)
        self.assertEqual(self.inst.state.status, "IDLE")

    async def test_escalation_does_not_touch_any_resting_order_state(self):
        """Escalating must only affect the instance's own status - it must
        never attempt to cancel or modify a resting SL/TP order, since
        those protect the position independent of whether this loop is
        running at all."""
        self.inst.client.cancel_algo_order = mock.AsyncMock()
        for _ in range(MAX_CONSECUTIVE_TICK_FAILURES):
            await self.inst._escalate_on_repeated_tick_failure()
        self.inst.client.cancel_algo_order.assert_not_called()

    async def test_escalation_cleans_up_the_mark_feed_and_client(self):
        """2026-09-18 fix - a real, subtle bug found on re-audit: escalating
        used to only set status and return, leaving the mark-price feed
        running and the exchange client session open indefinitely even
        though the instance had stopped ticking. Confirms the cleanup that
        stop() has always done is now also performed here."""
        self.inst.mark_feed.stop = mock.AsyncMock()
        self.inst.client.close = mock.AsyncMock()
        for _ in range(MAX_CONSECUTIVE_TICK_FAILURES):
            await self.inst._escalate_on_repeated_tick_failure()
        self.inst.mark_feed.stop.assert_called_once()
        self.inst.client.close.assert_called_once()

    async def test_escalation_unregisters_from_the_shared_user_data_stream(self):
        fake_manager = mock.Mock()
        fake_stream = mock.Mock()
        fake_manager.user_data_streams = {self.inst.account_id: fake_stream}
        fake_manager.maybe_teardown_user_data_stream = mock.AsyncMock()
        self.inst.manager = fake_manager
        self.inst.mark_feed.stop = mock.AsyncMock()
        self.inst.client.close = mock.AsyncMock()

        for _ in range(MAX_CONSECUTIVE_TICK_FAILURES):
            await self.inst._escalate_on_repeated_tick_failure()

        fake_stream.unregister.assert_called_once_with(self.inst.symbol)
        fake_manager.maybe_teardown_user_data_stream.assert_called_once()


if __name__ == "__main__":
    unittest.main()
