import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_targeted_clear")
import tests._aiohttp_stub  # noqa: F401,E402

from tests.test_instance_safety import make_instance  # noqa: E402
import app.reconciliation as reconciliation_module  # noqa: E402


class TestClearPendingReconciliationOnSuccessMethod(unittest.TestCase):
    """Regression tests for the fix flagged across TWO separate review
    rounds: a successful order used to blanket-clear EVERY pending
    reconciliation record for the symbol, not just the one that actually
    resolved. Tested directly against the method itself (not routed
    through the full _do_enter flow) because _do_enter's OWN entry-
    blocking gate (a separate, earlier fix) already does its own
    blanket-clear whenever ANY pending record exists and the exchange
    confirms flat - which is legitimate there (confirmed-flat is strong
    evidence for every record on that symbol, not just one), but means
    testing THIS method's precision has to happen at the method level to
    actually isolate it from that earlier gate."""

    def setUp(self):
        self.inst = make_instance()
        # Ensure no leftover records from a previous test - reconciliation
        # storage persists on disk across tests unless explicitly cleared.
        reconciliation_module.clear_pending_reconciliation(self.inst.account_id, self.inst.symbol)

    def test_clears_only_the_matching_record_not_an_unrelated_one(self):
        reconciliation_module.flag_pending_reconciliation(
            self.inst.account_id, self.inst.symbol, "SL placement", "unrelated-order", "still unresolved")
        reconciliation_module.flag_pending_reconciliation(
            self.inst.account_id, self.inst.symbol, "market entry", "this-orders-id", "was ambiguous before")

        self.inst._clear_pending_reconciliation_on_success("this-orders-id")

        records = reconciliation_module.get_pending_reconciliations(self.inst.account_id, self.inst.symbol)
        ids = {r.client_order_id for r in records}
        self.assertEqual(ids, {"unrelated-order"},
                         "only the matching id's record should clear - the unrelated one must survive")

    def test_no_matching_record_is_a_safe_no_op(self):
        reconciliation_module.flag_pending_reconciliation(
            self.inst.account_id, self.inst.symbol, "SL placement", "unrelated-order", "still unresolved")

        self.inst._clear_pending_reconciliation_on_success("some-id-that-was-never-flagged")

        records = reconciliation_module.get_pending_reconciliations(self.inst.account_id, self.inst.symbol)
        ids = {r.client_order_id for r in records}
        self.assertEqual(ids, {"unrelated-order"}, "clearing a non-existent id must not touch anything else")

    def test_no_client_order_id_falls_back_to_blanket_clear(self):
        """Backward-compatible fallback: if the caller genuinely has no id
        to identify (e.g. the order response didn't include one), blanket-
        clear is still better than never clearing anything at all."""
        reconciliation_module.flag_pending_reconciliation(
            self.inst.account_id, self.inst.symbol, "SL placement", "order-a", "unresolved")
        reconciliation_module.flag_pending_reconciliation(
            self.inst.account_id, self.inst.symbol, "market entry", "order-b", "unresolved")

        self.inst._clear_pending_reconciliation_on_success(None)

        records = reconciliation_module.get_pending_reconciliations(self.inst.account_id, self.inst.symbol)
        self.assertEqual(records, [], "with no id to target, the safe fallback still clears everything")


class TestCloseSideUsesTargetedClearing(unittest.IsolatedAsyncioTestCase):
    """The close path has no equivalent pre-gate the way _do_enter does -
    closes must always be allowed to proceed regardless of pending
    reconciliation state, so this integration test through the real
    _retry_close flow is valid and meaningful, unlike the entry-side case
    above."""

    async def test_successful_close_clears_only_its_own_record(self):
        inst = make_instance()
        reconciliation_module.clear_pending_reconciliation(inst.account_id, inst.symbol)  # test isolation
        inst.state.status = "CLOSING"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.closing_reason = "force_close_ema"
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}

        reconciliation_module.flag_pending_reconciliation(
            inst.account_id, inst.symbol, "market entry", "unrelated-earlier-entry", "still unresolved")

        original_close = inst.client.close_position_market

        async def close_with_known_client_id(symbol, side, qty):
            result = await original_close(symbol, side, qty)
            result["clientOrderId"] = "this-closes-order-id"
            return result

        inst.client.close_position_market = close_with_known_client_id

        await inst._retry_close()

        records = reconciliation_module.get_pending_reconciliations(inst.account_id, inst.symbol)
        ids = {r.client_order_id for r in records}
        self.assertEqual(ids, {"unrelated-earlier-entry"}, "the unrelated entry record must survive a close")


if __name__ == "__main__":
    unittest.main()
