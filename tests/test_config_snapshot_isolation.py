import os
import sys
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

TEST_DATA_DIR = Path("/tmp/hull_bot_test_config_snapshot")

import app.store as store_module  # noqa: E402
import app.okx_store as okx_store_module  # noqa: E402
from app.manager import BotManager  # noqa: E402
from app.okx_manager import OKXBotManager  # noqa: E402
from app.store import PairConfig  # noqa: E402
from app.okx_store import OKXPairConfig  # noqa: E402
from tests import _test_values as tv  # noqa: E402


class TestBinanceConfigSnapshotIsolation(unittest.TestCase):
    """2026-09-16 fix (flagged by a third-party review, confirmed real - the
    single most serious finding of that round): a running instance used to
    hold a DIRECT REFERENCE to the exact same PairConfig object
    store.update_pair() mutates via setattr(). That meant a config edit
    changed a running instance's parameters IMMEDIATELY - completely
    independent of whether the restart itself was deferred (the
    "_pending_restart_on_flat" mechanism only ever delayed REBUILDING the
    instance; it never protected the instance from having its underlying
    config object mutated out from under it while a trade was still open).
    This is the actual, concrete proof that a running instance's own
    config is now a genuinely independent snapshot."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
        self.store = store_module.Store()
        self.manager = BotManager()
        import app.manager as manager_module
        manager_module.store = self.store
        self.manager.store = self.store

        self.acc = self.store.create_account("Main", "key", "secret", True)
        self.store.add_pair(self.acc.id, tv.pair_config(symbol="BTCUSDT", stop_loss_pct_equity=5.5, tp2_atr_mult=2.75))

    def test_editing_config_does_not_mutate_a_running_instances_own_copy(self):
        instance = self.manager._build_instance(self.acc.id, "BTCUSDT")

        # Confirmed BEFORE the edit: the instance's own values match what
        # was configured.
        self.assertEqual(instance.pc.stop_loss_pct_equity, 5.5)
        self.assertEqual(instance.pc.tp2_atr_mult, 2.75)

        # Simulates exactly what the dashboard's update_pair endpoint does -
        # mutating the STORE's own PairConfig object.
        self.store.update_pair(self.acc.id, "BTCUSDT", {"stop_loss_pct_equity": 3.5, "tp2_atr_mult": 1.75})

        # The store's own copy changed, as expected.
        self.assertEqual(self.store.get_account(self.acc.id).pairs["BTCUSDT"].stop_loss_pct_equity, 3.5)

        # The RUNNING instance's copy must be COMPLETELY UNAFFECTED - this
        # is the actual bug: before the fix, this would also read the edited value,
        # since both objects were literally the same object in memory.
        self.assertEqual(instance.pc.stop_loss_pct_equity, 5.5,
                        "a running instance's own config must never be mutated by a later edit")
        self.assertEqual(instance.pc.tp2_atr_mult, 2.75)

    def test_a_fresh_restart_correctly_picks_up_the_new_values(self):
        """The other half - confirms the fix doesn't ALSO break the
        intended behavior: once an instance is genuinely rebuilt (e.g. by
        the deferred-restart mechanism once the pair goes flat), it must
        pick up the LATEST stored values, not some frozen-forever
        snapshot from the very first time it was ever built."""
        instance1 = self.manager._build_instance(self.acc.id, "BTCUSDT")
        self.assertEqual(instance1.pc.stop_loss_pct_equity, 5.5)

        self.store.update_pair(self.acc.id, "BTCUSDT", {"stop_loss_pct_equity": 3.5})

        # A fresh build (what a real restart does) must reflect the update.
        instance2 = self.manager._build_instance(self.acc.id, "BTCUSDT")
        self.assertEqual(instance2.pc.stop_loss_pct_equity, 3.5)

        # And the FIRST instance's own snapshot must still be untouched -
        # confirms these are genuinely two separate objects, not the same
        # one being read at two different times.
        self.assertEqual(instance1.pc.stop_loss_pct_equity, 5.5)

    def test_instance_pc_is_a_different_object_than_the_stores_own(self):
        instance = self.manager._build_instance(self.acc.id, "BTCUSDT")
        stored_pc = self.store.get_account(self.acc.id).pairs["BTCUSDT"]
        self.assertIsNot(instance.pc, stored_pc,
                        "must be a genuinely separate object, not the same one by reference")


class TestOKXConfigSnapshotIsolation(unittest.TestCase):
    """OKX equivalent - separate implementation, same behavior."""

    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
        self.store = okx_store_module.OKXStore()
        self.manager = OKXBotManager()
        import app.okx_manager as okx_manager_module
        okx_manager_module.okx_store = self.store
        self.manager.store = self.store

        self.acc = self.store.create_account("Sub1", "key", "secret", "phrase", demo=True)
        self.store.add_pair(self.acc.id, tv.okx_pair_config(symbol="BTC-USDT-SWAP", stop_loss_pct_equity=5.5))

    def test_editing_config_does_not_mutate_a_running_instances_own_copy(self):
        instance = self.manager._build_instance(self.acc.id, "BTC-USDT-SWAP")
        self.assertEqual(instance.pc.stop_loss_pct_equity, 5.5)

        self.store.update_pair(self.acc.id, "BTC-USDT-SWAP", {"stop_loss_pct_equity": 3.5})

        self.assertEqual(instance.pc.stop_loss_pct_equity, 5.5,
                        "a running OKX instance's own config must never be mutated by a later edit")

    def test_instance_pc_is_a_different_object_than_the_stores_own(self):
        instance = self.manager._build_instance(self.acc.id, "BTC-USDT-SWAP")
        stored_pc = self.store.get_account(self.acc.id).pairs["BTC-USDT-SWAP"]
        self.assertIsNot(instance.pc, stored_pc)


if __name__ == "__main__":
    unittest.main()
