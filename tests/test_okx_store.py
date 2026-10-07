import os
import sys
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATA_DIR = Path("/tmp/hull_bot_test_okx_store")

import app.okx_store as okx_store_module  # noqa: E402
from app.okx_store import OKXPairConfig  # noqa: E402
from tests import _test_values as tv  # noqa: E402


def fresh_store() -> "okx_store_module.OKXStore":
    """Same isolation technique as test_store.py's fresh_store(): override
    the module-level path attribute directly (not an env var), since
    _save()/_load() read it dynamically on every call."""
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
    return okx_store_module.OKXStore()


class TestOKXAccountPersistence(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store()

    def test_create_and_reload_account(self):
        acc = self.store.create_account("Sub1", "key123", "secret456", "pass789", demo=True)
        acc = self.store.update_account(acc.id, max_account_exposure_pct=150)
        self.assertEqual(acc.max_account_exposure_pct, 150)

        reloaded = okx_store_module.OKXStore()  # same file path, still overridden
        acc2 = reloaded.get_account(acc.id)
        self.assertEqual(acc2.max_account_exposure_pct, 150)
        self.assertTrue(acc2.demo)

        key, secret, passphrase = reloaded.get_credentials(acc.id)
        self.assertEqual((key, secret, passphrase), ("key123", "secret456", "pass789"))

    def test_credentials_encrypted_at_rest(self):
        acc = self.store.create_account("Sub1", "supersecretkey", "supersecretvalue", "mypassphrase", demo=False)
        raw = okx_store_module.OKX_ACCOUNTS_FILE.read_text()
        self.assertNotIn("supersecretkey", raw)
        self.assertNotIn("supersecretvalue", raw)
        self.assertNotIn("mypassphrase", raw)

    def test_max_five_accounts(self):
        for i in range(5):
            self.store.create_account(f"Sub{i}", "k", "s", "p", demo=True)
        with self.assertRaises(ValueError):
            self.store.create_account("Sub6", "k", "s", "p", demo=True)

    def test_max_five_pairs_per_account(self):
        acc = self.store.create_account("Sub1", "k", "s", "p", demo=True)
        for i in range(5):
            self.store.add_pair(acc.id, tv.okx_pair_config(symbol=f"SYM{i}-USDT-SWAP"))
        with self.assertRaises(ValueError):
            self.store.add_pair(acc.id, tv.okx_pair_config(symbol="SYM6-USDT-SWAP"))

    def test_sub_account_label_is_informational_only(self):
        acc = self.store.create_account("Sub1", "k", "s", "p", demo=True, sub_account_label="alt-1")
        self.assertEqual(acc.sub_account_label, "alt-1")


if __name__ == "__main__":
    unittest.main()
