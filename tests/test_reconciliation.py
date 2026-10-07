import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.reconciliation as reconciliation_module


class TestReconciliation(unittest.TestCase):
    """Item 9 fix: a durable, visible record for an order whose outcome
    couldn't be determined even after the exchange adapter's own ambiguous-
    response recovery already failed too.

    REDESIGNED 2026-09-14 (owner-approved): now supports MULTIPLE
    concurrent unresolved records per account/symbol, keyed by
    client_order_id - the original one-record-per-symbol design meant a
    second ambiguous order silently overwrote the first, losing it
    entirely. These tests were updated for the new plural API
    (get_pending_reconciliations / has_pending_reconciliation) and extended
    with cases that specifically pin the overwrite bug staying fixed."""

    def setUp(self):
        import shutil
        from pathlib import Path
        self._dir = Path("/tmp/hull_bot_test_reconciliation")
        shutil.rmtree(self._dir, ignore_errors=True)
        reconciliation_module.RECONCILIATION_DIR = self._dir
        reconciliation_module.RECONCILIATION_DIR.mkdir(parents=True, exist_ok=True)

    def test_flag_then_get_round_trips(self):
        reconciliation_module.flag_pending_reconciliation(
            "acc1", "BTCUSDT", "market entry", "hullbot_abc123", "connection reset",
        )

        records = reconciliation_module.get_pending_reconciliations("acc1", "BTCUSDT")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].context, "market entry")
        self.assertEqual(records[0].client_order_id, "hullbot_abc123")
        self.assertEqual(records[0].detail, "connection reset")

    def test_get_returns_empty_list_when_nothing_flagged(self):
        records = reconciliation_module.get_pending_reconciliations("acc1", "ETHUSDT")
        self.assertEqual(records, [])
        self.assertFalse(reconciliation_module.has_pending_reconciliation("acc1", "ETHUSDT"))

    def test_clear_removes_the_flag(self):
        reconciliation_module.flag_pending_reconciliation("acc1", "BTCUSDT", "close", "abc", "detail")
        reconciliation_module.clear_pending_reconciliation("acc1", "BTCUSDT")

        self.assertEqual(reconciliation_module.get_pending_reconciliations("acc1", "BTCUSDT"), [])
        self.assertFalse(reconciliation_module.has_pending_reconciliation("acc1", "BTCUSDT"))

    def test_clear_on_nothing_flagged_does_not_raise(self):
        reconciliation_module.clear_pending_reconciliation("acc1", "NEVERUSDT")  # must not raise

    def test_list_all_returns_every_flagged_account_symbol(self):
        reconciliation_module.flag_pending_reconciliation("acc1", "BTCUSDT", "entry", "a", "d1")
        reconciliation_module.flag_pending_reconciliation("acc2", "ETHUSDT", "close", "b", "d2")

        results = reconciliation_module.list_all_pending_reconciliations()

        symbols = {r.symbol for r in results}
        self.assertEqual(symbols, {"BTCUSDT", "ETHUSDT"})

    def test_different_symbols_on_the_same_account_are_independent(self):
        reconciliation_module.flag_pending_reconciliation("acc1", "BTCUSDT", "entry", "a", "d1")

        self.assertEqual(reconciliation_module.get_pending_reconciliations("acc1", "ETHUSDT"), [],
                          "flagging one symbol must not affect a sibling symbol on the same account")

    # ---------------------------------------------------------------- the actual bug fix
    def test_second_flag_on_same_symbol_does_not_overwrite_the_first(self):
        """This is the exact bug reported: a second ambiguous order on the
        same symbol used to silently destroy the first record entirely."""
        reconciliation_module.flag_pending_reconciliation(
            "acc1", "BTCUSDT", "market entry", "order-A", "first ambiguous order")
        reconciliation_module.flag_pending_reconciliation(
            "acc1", "BTCUSDT", "SL placement", "order-B", "second ambiguous order")

        records = reconciliation_module.get_pending_reconciliations("acc1", "BTCUSDT")
        ids = {r.client_order_id for r in records}

        self.assertEqual(len(records), 2, "both records must coexist, not one overwriting the other")
        self.assertEqual(ids, {"order-A", "order-B"})

    def test_clearing_one_record_leaves_the_other_untouched(self):
        reconciliation_module.flag_pending_reconciliation("acc1", "BTCUSDT", "entry", "order-A", "d1")
        reconciliation_module.flag_pending_reconciliation("acc1", "BTCUSDT", "close", "order-B", "d2")

        reconciliation_module.clear_pending_reconciliation("acc1", "BTCUSDT", client_order_id="order-A")

        records = reconciliation_module.get_pending_reconciliations("acc1", "BTCUSDT")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].client_order_id, "order-B")

    def test_reflagging_same_client_order_id_updates_rather_than_duplicates(self):
        reconciliation_module.flag_pending_reconciliation("acc1", "BTCUSDT", "entry", "order-A", "first detail")
        reconciliation_module.flag_pending_reconciliation("acc1", "BTCUSDT", "entry", "order-A", "updated detail")

        records = reconciliation_module.get_pending_reconciliations("acc1", "BTCUSDT")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].detail, "updated detail")

    def test_clear_with_no_client_order_id_clears_everything(self):
        reconciliation_module.flag_pending_reconciliation("acc1", "BTCUSDT", "entry", "order-A", "d1")
        reconciliation_module.flag_pending_reconciliation("acc1", "BTCUSDT", "close", "order-B", "d2")

        reconciliation_module.clear_pending_reconciliation("acc1", "BTCUSDT")  # no id = clear all

        self.assertEqual(reconciliation_module.get_pending_reconciliations("acc1", "BTCUSDT"), [])

    def test_old_single_record_file_format_still_reads_correctly(self):
        """Backward-compat: a file written before this redesign is a plain
        dict, not a list - must not crash, should be read as one record."""
        import json
        path = reconciliation_module._path("acc1", "BTCUSDT")
        old_format_record = {
            "account_id": "acc1", "symbol": "BTCUSDT", "context": "entry",
            "client_order_id": "old-order", "detail": "old format", "flagged_at": 123.0,
        }
        path.write_text(json.dumps(old_format_record))

        records = reconciliation_module.get_pending_reconciliations("acc1", "BTCUSDT")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].client_order_id, "old-order")


if __name__ == "__main__":
    unittest.main()
