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

TEST_DATA_DIR = Path("/tmp/hull_bot_test_credential_refusal")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)

import app.store as store_module  # noqa: E402
import app.okx_store as okx_store_module  # noqa: E402
from app.store import PairConfig  # noqa: E402
from app.okx_store import OKXPairConfig  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from app.api import _refuse_if_credential_change_while_running  # noqa: E402
from app.api import _refuse_if_unsafe_to_delete  # noqa: E402
from app import reconciliation  # noqa: E402
from tests import _test_values as tv  # noqa: E402


class TestRefuseIfUnsafeToDeleteHelper(unittest.TestCase):
    """2026-09-16, owner decision (item #4): deleting an account is
    destructive and hard to undo - refuses if any pair has an open/
    transitioning position or an unresolved pending-reconciliation
    warning, same "refuse rather than silently proceed" philosophy as
    item #3's credential-change check."""

    def setUp(self):
        reconciliation.RECONCILIATION_DIR = TEST_DATA_DIR / "reconciliation"
        reconciliation.RECONCILIATION_DIR.mkdir(parents=True, exist_ok=True)
        reconciliation.clear_pending_reconciliation("acc1", "BTCUSDT")
        reconciliation.clear_pending_reconciliation("acc1", "ETHUSDT")

    def test_open_position_blocks_deletion(self):
        inst = mock.Mock()
        inst.state.status = "IN_POSITION"
        with self.assertRaises(HTTPException) as ctx:
            _refuse_if_unsafe_to_delete("acc1", ["BTCUSDT"], {"BTCUSDT": inst}, "Binance")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("BTCUSDT", ctx.exception.detail)
        self.assertIn("IN_POSITION", ctx.exception.detail)

    def test_unprotected_blocks_deletion(self):
        inst = mock.Mock()
        inst.state.status = "UNPROTECTED"
        with self.assertRaises(HTTPException):
            _refuse_if_unsafe_to_delete("acc1", ["BTCUSDT"], {"BTCUSDT": inst}, "Binance")

    def test_closing_blocks_deletion(self):
        inst = mock.Mock()
        inst.state.status = "CLOSING"
        with self.assertRaises(HTTPException):
            _refuse_if_unsafe_to_delete("acc1", ["BTCUSDT"], {"BTCUSDT": inst}, "Binance")

    def test_idle_does_not_block_deletion(self):
        inst = mock.Mock()
        inst.state.status = "IDLE"
        _refuse_if_unsafe_to_delete("acc1", ["BTCUSDT"], {"BTCUSDT": inst}, "Binance")  # must not raise

    def test_stopped_does_not_block_deletion(self):
        inst = mock.Mock()
        inst.state.status = "STOPPED"
        _refuse_if_unsafe_to_delete("acc1", ["BTCUSDT"], {"BTCUSDT": inst}, "Binance")  # must not raise

    def test_no_instance_at_all_does_not_block_by_itself(self):
        """A pair whose instance never started (never in the dict) isn't,
        by itself, treated as unsafe - only a genuinely pending
        reconciliation or a known-open position blocks deletion. See the
        function's own docstring on the stated scope limitation here."""
        _refuse_if_unsafe_to_delete("acc1", ["BTCUSDT"], {}, "Binance")  # must not raise

    def test_pending_reconciliation_blocks_deletion_even_with_no_running_instance(self):
        reconciliation.flag_pending_reconciliation("acc1", "BTCUSDT", "market entry", "abc", "unresolved")
        with self.assertRaises(HTTPException) as ctx:
            _refuse_if_unsafe_to_delete("acc1", ["BTCUSDT"], {}, "Binance")
        self.assertIn("pending reconciliation", ctx.exception.detail)

    def test_multiple_blocking_pairs_all_named(self):
        inst_a = mock.Mock()
        inst_a.state.status = "IN_POSITION"
        reconciliation.flag_pending_reconciliation("acc1", "ETHUSDT", "close", "xyz", "unresolved")
        with self.assertRaises(HTTPException) as ctx:
            _refuse_if_unsafe_to_delete("acc1", ["BTCUSDT", "ETHUSDT"],
                                        {"BTCUSDT": inst_a}, "Binance")
        self.assertIn("BTCUSDT", ctx.exception.detail)
        self.assertIn("ETHUSDT", ctx.exception.detail)

    def test_clean_account_with_multiple_flat_pairs_deletes_fine(self):
        inst_a = mock.Mock()
        inst_a.state.status = "IDLE"
        inst_b = mock.Mock()
        inst_b.state.status = "STOPPED"
        _refuse_if_unsafe_to_delete("acc1", ["BTCUSDT", "ETHUSDT"],
                                    {"BTCUSDT": inst_a, "ETHUSDT": inst_b}, "Binance")  # must not raise


class TestRefuseIfCredentialChangeWhileRunningHelper(unittest.TestCase):
    """The pure decision logic, tested directly with an explicit dict -
    independent of whether any given pydantic environment (real or the
    test stub) implements exclude_unset() filtering faithfully. This is
    what update_account/update_okx_account actually call."""

    def test_credential_field_present_and_something_running_raises(self):
        with self.assertRaises(HTTPException) as ctx:
            _refuse_if_credential_change_while_running(
                {"api_key": "new"}, ["BTCUSDT"], {"api_key", "api_secret", "testnet"}, "testnet/live")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("BTCUSDT", ctx.exception.detail)

    def test_credential_field_present_but_nothing_running_does_not_raise(self):
        _refuse_if_credential_change_while_running(
            {"api_key": "new"}, [], {"api_key", "api_secret", "testnet"}, "testnet/live")  # must not raise

    def test_no_credential_field_present_never_raises_even_if_running(self):
        _refuse_if_credential_change_while_running(
            {"max_account_exposure_pct": 50.0}, ["BTCUSDT"],
            {"api_key", "api_secret", "testnet"}, "testnet/live")  # must not raise

    def test_multiple_running_symbols_all_named_in_the_message(self):
        with self.assertRaises(HTTPException) as ctx:
            _refuse_if_credential_change_while_running(
                {"testnet": False}, ["ETHUSDT", "BTCUSDT"],
                {"api_key", "api_secret", "testnet"}, "testnet/live")
        self.assertIn("BTCUSDT", ctx.exception.detail)
        self.assertIn("ETHUSDT", ctx.exception.detail)


def _mock_instance(symbol, status):
    inst = mock.Mock()
    inst.symbol = symbol
    inst.state = mock.Mock()
    inst.state.status = status
    return inst


class TestBinanceAccountCredentialRefusal(unittest.TestCase):
    """2026-09-16, owner decision: changing API key/secret/testnet while
    pairs are running must be REFUSED, not silently applied or auto-
    restarted - unlike ordinary pair settings (which defer and auto-apply
    once flat), a credential/environment change is a deliberate,
    attention-heavy action that should require a conscious "stop first"
    step, since the running pair's connection was built once at startup
    and won't pick up the change on its own."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
        self.store = store_module.Store()

        import app.api as api_module
        api_module.store = self.store
        self.api_module = api_module

        self.acc = self.store.create_account("Main", "key", "secret", True)
        self.store.add_pair(self.acc.id, tv.pair_config(symbol="BTCUSDT"))

    def test_credential_change_refused_while_a_pair_is_running(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTCUSDT", "IN_POSITION")])
        self.api_module.manager = fake_manager

        from app.api.schemas import AccountUpdate
        with self.assertRaises(HTTPException) as ctx:
            self.api_module.update_account(self.acc.id, AccountUpdate(api_key="newkey"))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("BTCUSDT", ctx.exception.detail)

    def test_testnet_change_refused_while_a_pair_is_running(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTCUSDT", "IDLE")])
        self.api_module.manager = fake_manager

        from app.api.schemas import AccountUpdate
        with self.assertRaises(HTTPException) as ctx:
            self.api_module.update_account(self.acc.id, AccountUpdate(testnet=False))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_credential_change_allowed_when_every_pair_is_stopped(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTCUSDT", "STOPPED")])
        self.api_module.manager = fake_manager

        from app.api.schemas import AccountUpdate
        result = self.api_module.update_account(self.acc.id, AccountUpdate(api_key="newkey"))
        self.assertEqual(result["id"], self.acc.id)  # did not raise - succeeded

    def test_credential_change_allowed_with_no_instances_at_all(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(return_value=[])
        self.api_module.manager = fake_manager

        from app.api.schemas import AccountUpdate
        result = self.api_module.update_account(self.acc.id, AccountUpdate(api_key="newkey"))
        self.assertEqual(result["id"], self.acc.id)


class TestOKXAccountCredentialRefusal(unittest.TestCase):
    """OKX equivalent - separate implementation, same behavior."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
        self.store = okx_store_module.OKXStore()

        import app.api.okx as okx_api_module
        okx_api_module.okx_store = self.store
        self.okx_api_module = okx_api_module

        self.acc = self.store.create_account("Sub1", "key", "secret", "phrase", demo=True)
        self.store.add_pair(self.acc.id, tv.okx_pair_config(symbol="BTC-USDT-SWAP"))

    def test_credential_change_refused_while_a_pair_is_running(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTC-USDT-SWAP", "IN_POSITION")])
        self.okx_api_module.okx_manager = fake_manager

        from app.api.schemas import OKXAccountUpdate
        with self.assertRaises(HTTPException) as ctx:
            self.okx_api_module.update_okx_account(self.acc.id, OKXAccountUpdate(api_key="newkey"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_demo_flag_change_refused_while_a_pair_is_running(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTC-USDT-SWAP", "IN_POSITION")])
        self.okx_api_module.okx_manager = fake_manager

        from app.api.schemas import OKXAccountUpdate
        with self.assertRaises(HTTPException) as ctx:
            self.okx_api_module.update_okx_account(self.acc.id, OKXAccountUpdate(demo=False))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_credential_change_allowed_when_every_pair_is_stopped(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTC-USDT-SWAP", "STOPPED")])
        self.okx_api_module.okx_manager = fake_manager

        from app.api.schemas import OKXAccountUpdate
        result = self.okx_api_module.update_okx_account(self.acc.id, OKXAccountUpdate(api_key="newkey"))
        self.assertEqual(result["id"], self.acc.id)


class TestBinancePairDeletionSafety(unittest.IsolatedAsyncioTestCase):
    """2026-09-16 fix (pre-live audit finding #4, confirmed real): account
    deletion already refused when a position wasn't confirmed flat - pair
    deletion didn't have the same guard. A pair with a real open position
    could be deleted, losing the local record needed to find and manage
    it again."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
        self.store = store_module.Store()

        import app.api as api_module
        api_module.store = self.store
        self.api_module = api_module

        self.acc = self.store.create_account("Main", "key", "secret", True)
        self.store.add_pair(self.acc.id, tv.pair_config(symbol="BTCUSDT"))

    async def test_deletion_refused_while_the_pair_has_an_open_position(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTCUSDT", "IN_POSITION")])
        fake_manager.stop = mock.AsyncMock()
        self.api_module.manager = fake_manager

        with self.assertRaises(HTTPException) as ctx:
            await self.api_module.delete_pair(self.acc.id, "BTCUSDT")
        self.assertEqual(ctx.exception.status_code, 409)
        fake_manager.stop.assert_not_called()

    async def test_deletion_allowed_when_the_pair_is_flat(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTCUSDT", "IDLE")])
        fake_manager.stop = mock.AsyncMock()
        self.api_module.manager = fake_manager

        result = await self.api_module.delete_pair(self.acc.id, "BTCUSDT")
        self.assertEqual(result, {"deleted": True})
        fake_manager.stop.assert_called_once()


class TestOKXPairDeletionSafety(unittest.IsolatedAsyncioTestCase):
    """OKX equivalent of the same fix."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
        self.store = okx_store_module.OKXStore()

        import app.api.okx as okx_api_module
        okx_api_module.okx_store = self.store
        self.okx_api_module = okx_api_module

        self.acc = self.store.create_account("Sub1", "key", "secret", "phrase", demo=True)
        self.store.add_pair(self.acc.id, tv.okx_pair_config(symbol="BTC-USDT-SWAP"))

    async def test_deletion_refused_while_the_pair_has_an_open_position(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTC-USDT-SWAP", "UNPROTECTED")])
        fake_manager.stop = mock.AsyncMock()
        self.okx_api_module.okx_manager = fake_manager

        with self.assertRaises(HTTPException) as ctx:
            await self.okx_api_module.delete_okx_pair(self.acc.id, "BTC-USDT-SWAP")
        self.assertEqual(ctx.exception.status_code, 409)
        fake_manager.stop.assert_not_called()

    async def test_deletion_allowed_when_the_pair_is_flat(self):
        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(
            return_value=[_mock_instance("BTC-USDT-SWAP", "STOPPED")])
        fake_manager.stop = mock.AsyncMock()
        self.okx_api_module.okx_manager = fake_manager

        result = await self.okx_api_module.delete_okx_pair(self.acc.id, "BTC-USDT-SWAP")
        self.assertEqual(result, {"deleted": True})
        fake_manager.stop.assert_called_once()


class TestFreezeRefusesEveryMutatingAction(unittest.IsolatedAsyncioTestCase):
    """2026-09-19, owner request: Freeze is a deliberate, persistent lock
    protecting against accidental human clicks - editing, start, stop,
    restart, and deletion are ALL refused while frozen, with no
    exception, until explicitly unfrozen. Has nothing to do with whether
    the pair is currently running - purely a human-action lock."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
        self.store = store_module.Store()

        import app.api as api_module
        api_module.store = self.store
        self.api_module = api_module

        self.acc = self.store.create_account("Main", "key", "secret", True)
        self.store.add_pair(self.acc.id, tv.pair_config(symbol="BTCUSDT"))

        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(return_value=[])
        fake_manager.stop = mock.AsyncMock()
        fake_manager.start = mock.AsyncMock()
        fake_manager.restart = mock.AsyncMock()
        fake_manager.startup_failures = {}
        self.api_module.manager = fake_manager
        self.fake_manager = fake_manager

    async def test_freeze_then_every_action_is_refused(self):
        await self.api_module.freeze_pair(self.acc.id, "BTCUSDT")
        self.assertTrue(self.store.get_account(self.acc.id).pairs["BTCUSDT"].frozen)

        from app.api.schemas import PairUpdate
        with self.assertRaises(HTTPException) as ctx:
            await self.api_module.update_pair(self.acc.id, "BTCUSDT", PairUpdate(leverage=5.0))
        self.assertEqual(ctx.exception.status_code, 409)

        with self.assertRaises(HTTPException):
            await self.api_module.start_pair(self.acc.id, "BTCUSDT")
        with self.assertRaises(HTTPException):
            await self.api_module.stop_pair(self.acc.id, "BTCUSDT")
        with self.assertRaises(HTTPException):
            await self.api_module.restart_pair(self.acc.id, "BTCUSDT")
        with self.assertRaises(HTTPException):
            await self.api_module.delete_pair(self.acc.id, "BTCUSDT")

        # None of the underlying manager actions should have actually run.
        self.fake_manager.start.assert_not_called()
        self.fake_manager.stop.assert_not_called()
        self.fake_manager.restart.assert_not_called()

    async def test_unfreeze_restores_every_action(self):
        await self.api_module.freeze_pair(self.acc.id, "BTCUSDT")
        await self.api_module.unfreeze_pair(self.acc.id, "BTCUSDT")
        self.assertFalse(self.store.get_account(self.acc.id).pairs["BTCUSDT"].frozen)

        result = await self.api_module.stop_pair(self.acc.id, "BTCUSDT")
        self.assertEqual(result, {"stopped": True})
        self.fake_manager.stop.assert_called_once()

    async def test_freeze_has_no_effect_on_a_currently_running_pair(self):
        """Explicitly confirms the owner's stated design: freeze is purely
        a human-action lock - it must have zero bearing on whether a pair
        is currently running, and must not itself stop or start anything."""
        await self.api_module.freeze_pair(self.acc.id, "BTCUSDT")
        self.fake_manager.stop.assert_not_called()
        self.fake_manager.start.assert_not_called()

    async def test_unfrozen_pair_is_completely_unaffected(self):
        """Regression check: a pair that was never frozen must behave
        exactly as before this feature existed."""
        result = await self.api_module.stop_pair(self.acc.id, "BTCUSDT")
        self.assertEqual(result, {"stopped": True})


class TestOKXFreezeRefusesEveryMutatingAction(unittest.IsolatedAsyncioTestCase):
    """OKX equivalent of the same feature - separate implementation, same
    behavior."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
        self.store = okx_store_module.OKXStore()

        import app.api.okx as okx_api_module
        okx_api_module.okx_store = self.store
        self.okx_api_module = okx_api_module

        self.acc = self.store.create_account("Sub1", "key", "secret", "phrase", demo=True)
        self.store.add_pair(self.acc.id, tv.okx_pair_config(symbol="BTC-USDT-SWAP"))

        fake_manager = mock.Mock()
        fake_manager.instances_for_account = mock.Mock(return_value=[])
        fake_manager.stop = mock.AsyncMock()
        fake_manager.start = mock.AsyncMock()
        fake_manager.restart = mock.AsyncMock()
        fake_manager.startup_failures = {}
        self.okx_api_module.okx_manager = fake_manager
        self.fake_manager = fake_manager

    async def test_freeze_then_stop_is_refused(self):
        await self.okx_api_module.freeze_okx_pair(self.acc.id, "BTC-USDT-SWAP")
        with self.assertRaises(HTTPException) as ctx:
            await self.okx_api_module.stop_okx_pair(self.acc.id, "BTC-USDT-SWAP")
        self.assertEqual(ctx.exception.status_code, 409)
        self.fake_manager.stop.assert_not_called()

    async def test_unfreeze_restores_stop(self):
        await self.okx_api_module.freeze_okx_pair(self.acc.id, "BTC-USDT-SWAP")
        await self.okx_api_module.unfreeze_okx_pair(self.acc.id, "BTC-USDT-SWAP")
        result = await self.okx_api_module.stop_okx_pair(self.acc.id, "BTC-USDT-SWAP")
        self.assertEqual(result, {"stopped": True})


if __name__ == "__main__":
    unittest.main()
