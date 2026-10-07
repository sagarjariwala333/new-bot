import os
import sys
import shutil
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402

TEST_DATA_DIR = Path("/tmp/hull_bot_test_deferred_restart")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)

import app.store as store_module  # noqa: E402
import app.manager as manager_module  # noqa: E402
from app.manager import BotManager  # noqa: E402
from tests import _test_values as tv  # noqa: E402


class TestRestartAnyPending(unittest.IsolatedAsyncioTestCase):
    """2026-09-15, owner decision: a config edit while a position is open
    must not change that position's behavior mid-trade - deferred until
    the pair is genuinely flat. These test the iteration/decision logic
    directly against mock instances (matching test_global_control.py's own
    approach), not a full real restart - manager.restart() itself is
    already covered by other existing manager tests."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
        self.manager = BotManager()
        self.manager.restart = mock.AsyncMock()

    def _mock_instance(self, account_id, symbol, pending, status):
        inst = mock.Mock()
        inst.account_id = account_id
        inst.symbol = symbol
        inst._pending_restart_on_flat = pending
        inst.state = mock.Mock()
        inst.state.status = status
        inst._log = mock.Mock()
        return inst

    async def test_pending_and_flat_gets_restarted(self):
        inst = self._mock_instance("acc1", "BTCUSDT", pending=True, status="IDLE")
        self.manager.instances = {"acc1:BTCUSDT": inst}

        await self.manager.restart_any_pending()

        self.manager.restart.assert_called_once_with("acc1", "BTCUSDT")

    async def test_pending_but_still_in_position_is_not_restarted(self):
        inst = self._mock_instance("acc1", "BTCUSDT", pending=True, status="IN_POSITION")
        self.manager.instances = {"acc1:BTCUSDT": inst}

        await self.manager.restart_any_pending()

        self.manager.restart.assert_not_called()

    async def test_flat_but_no_pending_update_is_not_restarted(self):
        inst = self._mock_instance("acc1", "BTCUSDT", pending=False, status="IDLE")
        self.manager.instances = {"acc1:BTCUSDT": inst}

        await self.manager.restart_any_pending()

        self.manager.restart.assert_not_called()

    async def test_only_the_pending_and_flat_instance_is_restarted_among_several(self):
        inst_a = self._mock_instance("acc1", "AAAUSDT", pending=True, status="IDLE")
        inst_b = self._mock_instance("acc1", "BBBUSDT", pending=True, status="IN_POSITION")
        inst_c = self._mock_instance("acc1", "CCCUSDT", pending=False, status="IDLE")
        self.manager.instances = {
            "acc1:AAAUSDT": inst_a, "acc1:BBBUSDT": inst_b, "acc1:CCCUSDT": inst_c,
        }

        await self.manager.restart_any_pending()

        self.manager.restart.assert_called_once_with("acc1", "AAAUSDT")


class TestUpdatePairDefersWhenPositionOpen(unittest.IsolatedAsyncioTestCase):
    """The API-level half: update_pair must set the flag (not restart
    immediately) when a position is open, and must restart immediately as
    before when the pair is flat - unchanged behavior for that case."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
        self.store = store_module.Store()

        import app.api as api_module
        api_module.store = self.store
        self.api_module = api_module

        from app.store import PairConfig
        self.acc = self.store.create_account("Main", "key", "secret", True)
        self.store.add_pair(self.acc.id, tv.pair_config(symbol="BTCUSDT"))

    async def test_open_position_sets_flag_instead_of_restarting(self):
        fake_manager = mock.Mock()
        fake_manager.store = self.store
        inst = mock.Mock()
        inst.state = mock.Mock()
        inst.state.status = "IN_POSITION"
        inst._pending_restart_on_flat = False
        inst._log = mock.Mock()
        fake_manager.get = mock.Mock(return_value=inst)
        fake_manager.restart = mock.AsyncMock()
        self.api_module.manager = fake_manager

        from app.api.schemas import PairUpdate
        await self.api_module.update_pair(self.acc.id, "BTCUSDT", PairUpdate())

        self.assertTrue(inst._pending_restart_on_flat, "must set the deferred-update flag")
        fake_manager.restart.assert_not_called()

    async def test_flat_pair_still_restarts_immediately(self):
        fake_manager = mock.Mock()
        fake_manager.store = self.store
        inst = mock.Mock()
        inst.state = mock.Mock()
        inst.state.status = "IDLE"
        inst._pending_restart_on_flat = False
        fake_manager.get = mock.Mock(return_value=inst)
        fake_manager.restart = mock.AsyncMock()
        self.api_module.manager = fake_manager

        from app.api.schemas import PairUpdate
        await self.api_module.update_pair(self.acc.id, "BTCUSDT", PairUpdate())

        self.assertFalse(inst._pending_restart_on_flat)
        fake_manager.restart.assert_called_once_with(self.acc.id, "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
