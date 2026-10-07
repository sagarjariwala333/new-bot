import os
import sys
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_withdraw_alert")
import tests._aiohttp_stub  # noqa: F401,E402

from tests.test_instance_safety import make_instance  # noqa: E402


def _mock_sibling(symbol, status):
    inst = mock.Mock()
    inst.symbol = symbol
    inst.state = mock.Mock()
    inst.state.status = status
    return inst


class TestWithdrawAlertLeaderSelection(unittest.IsolatedAsyncioTestCase):
    """2026-09-16 fix (flagged twice across review rounds): the leader used
    to be picked from every sibling regardless of status - if the
    alphabetically-first symbol happened to be stopped, nobody checked the
    threshold at all. Now only running siblings are considered."""

    def _setup_manager(self, inst, siblings, acc):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(return_value=siblings)
        fake_manager.store = mock.Mock()
        fake_manager.store.get_account = mock.Mock(return_value=acc)
        inst.manager = fake_manager

    async def test_stopped_alphabetically_first_sibling_does_not_block_the_next_one(self):
        inst = make_instance()
        inst.symbol = "BBBUSDT"  # would be second alphabetically if AAAUSDT were running
        inst.state.last_withdraw_check = 0.0
        acc = mock.Mock()
        acc.withdraw_alert_enabled = True
        acc.withdraw_alert_fired = False
        acc.withdraw_alert_threshold = 1000.0

        siblings = [
            _mock_sibling("AAAUSDT", "STOPPED"),  # alphabetically first, but stopped
            _mock_sibling("BBBUSDT", "IN_POSITION"),  # this instance itself - running
        ]
        self._setup_manager(inst, siblings, acc)
        inst.client.equity = 500.0  # below threshold - checked, but shouldn't fire

        await inst._maybe_check_withdraw_alert()

        self.assertGreater(inst.state.last_withdraw_check, 0.0)

    async def test_running_alphabetically_first_sibling_still_is_the_leader(self):
        """Confirms the fix didn't break the original, correct case - a
        genuinely running first-alphabetically sibling should still be the
        one responsible, and everyone else should defer to it."""
        inst = make_instance()
        inst.symbol = "BBBUSDT"
        inst.state.last_withdraw_check = 0.0
        acc = mock.Mock()

        siblings = [
            _mock_sibling("AAAUSDT", "IN_POSITION"),  # running, alphabetically first
            _mock_sibling("BBBUSDT", "IN_POSITION"),  # this instance - defers
        ]
        self._setup_manager(inst, siblings, acc)

        await inst._maybe_check_withdraw_alert()

        inst.manager.store.get_account.assert_not_called()


class TestOrphanedWithdrawalAlertCheck(unittest.IsolatedAsyncioTestCase):
    """2026-09-16 fix (item #6, second half): if EVERY pair on an account
    is stopped, nothing ticks at all, so the leader-selection fix alone
    can't help - nothing would ever notice the gap. This is the periodic,
    manager-level check that catches it instead."""

    def setUp(self):
        import app.manager as manager_module
        self.manager = manager_module.BotManager()
        self.sent = []

        async def fake_notify_error(account_name, symbol, error, enabled):
            self.sent.append((account_name, symbol, error))
        manager_module.tg.notify_error = fake_notify_error

    def _mock_account(self, id_, name, enabled=True, fired=False):
        acc = mock.Mock()
        acc.id = id_
        acc.name = name
        acc.withdraw_alert_enabled = enabled
        acc.withdraw_alert_fired = fired
        return acc

    async def test_alerts_when_every_pair_on_an_enabled_account_is_stopped(self):
        acc = self._mock_account("acc1", "Main")
        self.manager.store = mock.Mock()
        self.manager.store.list_accounts = mock.Mock(return_value=[acc])
        self.manager.instances_for_account = mock.Mock(
            return_value=[_mock_sibling("BTCUSDT", "STOPPED")])

        await self.manager.check_for_orphaned_withdrawal_alerts()

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "Main")

    async def test_does_not_alert_when_a_pair_is_running(self):
        acc = self._mock_account("acc1", "Main")
        self.manager.store = mock.Mock()
        self.manager.store.list_accounts = mock.Mock(return_value=[acc])
        self.manager.instances_for_account = mock.Mock(
            return_value=[_mock_sibling("BTCUSDT", "IN_POSITION")])

        await self.manager.check_for_orphaned_withdrawal_alerts()

        self.assertEqual(self.sent, [])

    async def test_does_not_alert_when_withdraw_alert_disabled(self):
        acc = self._mock_account("acc1", "Main", enabled=False)
        self.manager.store = mock.Mock()
        self.manager.store.list_accounts = mock.Mock(return_value=[acc])
        self.manager.instances_for_account = mock.Mock(
            return_value=[_mock_sibling("BTCUSDT", "STOPPED")])

        await self.manager.check_for_orphaned_withdrawal_alerts()

        self.assertEqual(self.sent, [])

    async def test_does_not_alert_when_already_fired(self):
        acc = self._mock_account("acc1", "Main", fired=True)
        self.manager.store = mock.Mock()
        self.manager.store.list_accounts = mock.Mock(return_value=[acc])
        self.manager.instances_for_account = mock.Mock(
            return_value=[_mock_sibling("BTCUSDT", "STOPPED")])

        await self.manager.check_for_orphaned_withdrawal_alerts()

        self.assertEqual(self.sent, [])

    async def test_only_alerts_once_per_gap_not_every_poll(self):
        acc = self._mock_account("acc1", "Main")
        self.manager.store = mock.Mock()
        self.manager.store.list_accounts = mock.Mock(return_value=[acc])
        self.manager.instances_for_account = mock.Mock(
            return_value=[_mock_sibling("BTCUSDT", "STOPPED")])

        await self.manager.check_for_orphaned_withdrawal_alerts()
        await self.manager.check_for_orphaned_withdrawal_alerts()
        await self.manager.check_for_orphaned_withdrawal_alerts()

        self.assertEqual(len(self.sent), 1, "must not spam the same alert every poll cycle")

    async def test_alerts_again_after_recovering_then_gapping_a_second_time(self):
        acc = self._mock_account("acc1", "Main")
        self.manager.store = mock.Mock()
        self.manager.store.list_accounts = mock.Mock(return_value=[acc])

        self.manager.instances_for_account = mock.Mock(
            return_value=[_mock_sibling("BTCUSDT", "STOPPED")])
        await self.manager.check_for_orphaned_withdrawal_alerts()  # gap #1 -> alert

        self.manager.instances_for_account = mock.Mock(
            return_value=[_mock_sibling("BTCUSDT", "IN_POSITION")])
        await self.manager.check_for_orphaned_withdrawal_alerts()  # recovered - clears the flag

        self.manager.instances_for_account = mock.Mock(
            return_value=[_mock_sibling("BTCUSDT", "STOPPED")])
        await self.manager.check_for_orphaned_withdrawal_alerts()  # gap #2 -> alerts again

        self.assertEqual(len(self.sent), 2)


if __name__ == "__main__":
    unittest.main()
