import os
import sys
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

TEST_DATA_DIR = Path("/tmp/hull_bot_test_okx_manager")

import app.okx_store as okx_store_module  # noqa: E402
import app.okx_manager as okx_manager_module  # noqa: E402
from app.okx_manager import OKXBotManager, _key  # noqa: E402
from app.okx_store import OKXPairConfig  # noqa: E402
from app.okx_futures import OKXFuturesClient  # noqa: E402
from app.instance import BotInstance  # noqa: E402
from app import singleton_lock  # noqa: E402
from tests import _test_values as tv  # noqa: E402


class TestOKXManualStartRefusedDuringSingletonConflict(unittest.IsolatedAsyncioTestCase):
    """OKX equivalent of the same Binance fix - see test_manager.py's
    identical test for the full reasoning."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
        self.store = okx_store_module.OKXStore()
        okx_manager_module.okx_store = self.store
        self.manager = OKXBotManager()
        self.manager.store = self.store

        self.acc = self.store.create_account("Sub1", "key", "secret", "phrase", demo=True)
        self.store.add_pair(self.acc.id, tv.okx_pair_config(symbol="BTC-USDT-SWAP"))

    def tearDown(self):
        singleton_lock._lock_held = True

    async def test_manual_start_refused_while_lock_conflict_is_active(self):
        singleton_lock._lock_held = False
        with self.assertRaises(RuntimeError):
            await self.manager.start(self.acc.id, "BTC-USDT-SWAP")
        self.assertNotIn(_key(self.acc.id, "BTC-USDT-SWAP"), self.manager.instances)

    async def test_manual_start_succeeds_once_the_lock_is_genuinely_held(self):
        singleton_lock._lock_held = True
        instance = await self.manager.start(self.acc.id, "BTC-USDT-SWAP")
        self.assertIsNotNone(instance)


class TestOKXInstanceWiring(unittest.TestCase):
    """Confirms _build_instance actually produces a platform='okx' BotInstance
    holding a real OKXFuturesClient - i.e. the OKX tab is genuinely wired to
    the OKX adapter, not silently defaulting to Binance anywhere."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
        self.store = okx_store_module.OKXStore()
        okx_manager_module.okx_store = self.store

        self.acc = self.store.create_account("Sub1", "key", "secret", "phrase", demo=True)
        self.store.add_pair(self.acc.id, tv.okx_pair_config(symbol="BTC-USDT-SWAP"))

        self.manager = OKXBotManager()

    def test_instance_has_okx_platform_and_adapter(self):
        inst = self.manager._build_instance(self.acc.id, "BTC-USDT-SWAP")
        self.assertEqual(inst.platform, "okx")
        self.assertIsInstance(inst.client, OKXFuturesClient)
        self.assertTrue(inst.client.demo)

    def test_instance_uses_real_okx_mark_feed(self):
        """2026-09-15: OKX pairs now use a real websocket mark-price feed
        (OKXMarkPriceFeed), not the old NullMarkFeed stand-in."""
        from app.ws_feed import OKXMarkPriceFeed
        inst = self.manager._build_instance(self.acc.id, "BTC-USDT-SWAP")
        self.assertIsInstance(inst.mark_feed, OKXMarkPriceFeed)
        self.assertTrue(inst.mark_feed.is_stale())  # never received a message yet - fails safe


class TestOKXStartAllEnabledIsolation(unittest.IsolatedAsyncioTestCase):
    """Same guarantee as Binance's equivalent test (test_manager.py): one
    broken OKX pair must not stop the rest of the OKX tab from starting."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
        self.store = okx_store_module.OKXStore()
        okx_manager_module.okx_store = self.store

        self.acc = self.store.create_account("Sub1", "key", "secret", "phrase", demo=True)
        for sym in ("AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"):
            self.store.add_pair(self.acc.id, tv.okx_pair_config(symbol=sym))
            self.store.set_pair_enabled(self.acc.id, sym, True)

        self.manager = OKXBotManager()

        self._original_instance_start = BotInstance.start
        BotInstance.start = lambda self: None  # don't spawn real background tasks in this test

    def tearDown(self):
        BotInstance.start = self._original_instance_start

    async def test_one_broken_pair_does_not_stop_the_others(self):
        original_build = self.manager._build_instance

        def failing_build(account_id, symbol):
            if symbol == "BBB-USDT-SWAP":
                raise ValueError("simulated corrupted credentials")
            return original_build(account_id, symbol)

        self.manager._build_instance = failing_build

        await self.manager.start_all_enabled()

        self.assertIn(_key(self.acc.id, "AAA-USDT-SWAP"), self.manager.instances)
        self.assertIn(_key(self.acc.id, "CCC-USDT-SWAP"), self.manager.instances)
        self.assertNotIn(_key(self.acc.id, "BBB-USDT-SWAP"), self.manager.instances)
        self.assertIn(_key(self.acc.id, "BBB-USDT-SWAP"), self.manager.startup_failures)


if __name__ == "__main__":
    unittest.main()
