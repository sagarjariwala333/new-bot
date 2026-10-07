import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import risk_guard as rg


def _position(symbol, qty, entry_price):
    """A raw position dict shaped like a real get_all_open_positions() entry."""
    return {"symbol": symbol, "positionAmt": str(qty), "entryPrice": str(entry_price)}


class TestExposureCap(unittest.TestCase):
    """Fixed 2026-09-14 per a third-party review: exposure must be computed
    from REAL exchange positions (get_all_open_positions()), not local
    BotInstance state - the earlier version could miss a crashed/never-
    started sibling instance, or any position this process simply didn't
    know about. These tests use raw position dicts (as the real API
    returns), not fake local instance objects, since that IS the fix."""

    def test_no_cap_always_allows(self):
        allowed, msg = rg.check_new_entry_allowed([], exclude_symbol="BTCUSDT", new_notional=99999,
                                                    equity=100, max_account_exposure_pct=None)
        self.assertTrue(allowed)

    def test_within_cap_allowed(self):
        positions = [_position("BTCUSDT", 0.1, 60_000)]
        allowed, msg = rg.check_new_entry_allowed(positions, exclude_symbol="ETHUSDT", new_notional=3000,
                                                    equity=10_000, max_account_exposure_pct=100)
        self.assertTrue(allowed, msg)

    def test_exceeding_cap_blocked(self):
        positions = [_position("BTCUSDT", 0.1, 60_000)]
        allowed, msg = rg.check_new_entry_allowed(positions, exclude_symbol="ETHUSDT", new_notional=5000,
                                                    equity=10_000, max_account_exposure_pct=100)
        self.assertFalse(allowed)
        self.assertIn("exceed cap", msg)

    def test_excludes_own_symbol_from_existing_exposure(self):
        # A pair re-evaluating its own entry shouldn't double-count its own
        # (not-yet-open) position against itself.
        positions = [_position("BTCUSDT", 0.1, 60_000)]
        existing = rg.compute_real_exposure(positions, exclude_symbol="BTCUSDT")
        self.assertEqual(existing, 0.0)

    def test_flat_symbols_are_never_in_the_list_at_all(self):
        # get_all_open_positions() already filters to positionAmt != 0 - a
        # flat symbol simply isn't in the list, unlike the old local-state
        # version which had to check status == "IN_POSITION" explicitly.
        existing = rg.compute_real_exposure([], exclude_symbol="ETHUSDT")
        self.assertEqual(existing, 0.0)

    def test_sums_across_multiple_real_positions(self):
        positions = [_position("BTCUSDT", 0.1, 60_000), _position("ETHUSDT", 2.0, 3_000)]
        existing = rg.compute_real_exposure(positions, exclude_symbol="SOLUSDT")
        self.assertAlmostEqual(existing, 0.1 * 60_000 + 2.0 * 3_000, places=6)

    def test_short_position_notional_uses_absolute_quantity(self):
        # A SHORT position has a negative positionAmt on Binance - notional
        # exposure must still be a positive magnitude.
        positions = [_position("BTCUSDT", -0.1, 60_000)]
        existing = rg.compute_real_exposure(positions, exclude_symbol="ETHUSDT")
        self.assertAlmostEqual(existing, 0.1 * 60_000, places=6)

    def test_malformed_position_entry_is_skipped_not_fatal(self):
        positions = [{"symbol": "BTCUSDT", "positionAmt": "not_a_number", "entryPrice": "60000"}]
        existing = rg.compute_real_exposure(positions, exclude_symbol="ETHUSDT")
        self.assertEqual(existing, 0.0, "a malformed entry must be skipped safely, not raise")


if __name__ == "__main__":
    unittest.main()
