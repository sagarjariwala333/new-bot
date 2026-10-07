import os
import sys
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATA_DIR = Path("/tmp/hull_bot_test_store")

import app.store as store_module  # noqa: E402
from app.store import PairConfig  # noqa: E402
from tests import _test_values as tv  # noqa: E402


def fresh_store() -> "store_module.Store":
    """See the identical note in test_ledger.py: module-level path constants
    are only read from the DATA_DIR env var once, at first import, for the
    whole test process - overriding the module attribute directly (not the
    env var) is what actually isolates each test, since _save()/_load() look
    up store_module.ACCOUNTS_FILE dynamically on every call."""
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    store_module.ACCOUNTS_FILE = TEST_DATA_DIR / "accounts.json"
    return store_module.Store()


class TestAccountPersistence(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store()

    def test_create_and_reload_account(self):
        acc = self.store.create_account("Main", "key123", "secret456", False)
        acc = self.store.update_account(acc.id, max_account_exposure_pct=150)
        self.assertEqual(acc.max_account_exposure_pct, 150)

        reloaded = store_module.Store()  # same ACCOUNTS_FILE path, still overridden
        acc2 = reloaded.get_account(acc.id)
        self.assertEqual(acc2.max_account_exposure_pct, 150)

        key, secret = reloaded.get_credentials(acc.id)
        self.assertEqual(key, "key123")
        self.assertEqual(secret, "secret456")

    def test_max_accounts_enforced(self):
        for i in range(store_module.MAX_ACCOUNTS):
            self.store.create_account(f"Acc{i}", "k", "s", False)
        with self.assertRaises(ValueError):
            self.store.create_account("OneMore", "k", "s", False)

    def test_max_pairs_per_account_enforced(self):
        acc = self.store.create_account("Main", "k", "s", False)
        for i in range(store_module.MAX_PAIRS_PER_ACCOUNT):
            self.store.add_pair(acc.id, tv.pair_config(symbol=f"SYM{i}USDT"))
        with self.assertRaises(ValueError):
            self.store.add_pair(acc.id, tv.pair_config(symbol="ONEMORE"))


class TestExposureCapPartialUpdateFix(unittest.TestCase):
    """Regression test for the reviewed bug: a PUT that omits
    max_account_exposure_pct must NOT silently clear an existing cap - only
    an explicit null should clear it. This mirrors exactly what
    app/api/__init__.py's update_account route does with
    body.model_dump(exclude_unset=True), simulated here at the store layer
    since pydantic isn't a dependency of this test module."""

    def setUp(self):
        self.store = fresh_store()
        self.acc = self.store.create_account("Main", "k", "s", False)
        self.store.update_account(self.acc.id, max_account_exposure_pct=150)

    def _simulate_put(self, updates_present_in_request: dict):
        """`updates_present_in_request` mirrors body.model_dump(exclude_unset=True) -
        only keys the client actually sent."""
        return self.store.update_account(
            self.acc.id,
            name=updates_present_in_request.get("name"),
            api_key=updates_present_in_request.get("api_key"),
            api_secret=updates_present_in_request.get("api_secret"),
            testnet=updates_present_in_request.get("testnet"),
            max_account_exposure_pct=updates_present_in_request.get("max_account_exposure_pct", "unset"),
        )

    def test_unrelated_partial_update_preserves_exposure_cap(self):
        acc = self._simulate_put({"name": "Renamed"})
        self.assertEqual(acc.max_account_exposure_pct, 150,
                         "an unrelated field update must never silently clear the exposure cap")

    def test_explicit_null_clears_exposure_cap(self):
        acc = self._simulate_put({"max_account_exposure_pct": None})
        self.assertIsNone(acc.max_account_exposure_pct)

    def test_explicit_value_updates_exposure_cap(self):
        acc = self._simulate_put({"max_account_exposure_pct": 75})
        self.assertEqual(acc.max_account_exposure_pct, 75)


if __name__ == "__main__":
    unittest.main()

