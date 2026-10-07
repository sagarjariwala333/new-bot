import os
import sys
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATA_DIR", "/tmp/hull_bot_test_leverage_bracket")
import tests._aiohttp_stub  # noqa: F401,E402

from app.instance import _leverage_fits_bracket  # noqa: E402
from tests.test_instance_safety import make_instance  # noqa: E402
import app.strategy as strat  # noqa: E402
from tests._snap import make_snap  # noqa: E402
from tests import _test_values as tv  # noqa: E402


SAMPLE_BRACKETS = [
    {"max_leverage": 125, "notional_floor": 0.0, "notional_cap": 50_000.0},
    {"max_leverage": 100, "notional_floor": 50_000.0, "notional_cap": 250_000.0},
    {"max_leverage": 50, "notional_floor": 250_000.0, "notional_cap": 1_000_000.0},
]


class TestLeverageFitsBracket(unittest.TestCase):
    """Pure logic tests for the comparison itself, independent of which
    platform the bracket data came from."""

    def test_empty_brackets_is_a_no_op(self):
        ok, msg = _leverage_fits_bracket([], notional=10_000, leverage=125)
        self.assertTrue(ok, "no data to check against must never block an entry")

    def test_leverage_within_the_matching_bracket_passes(self):
        ok, msg = _leverage_fits_bracket(SAMPLE_BRACKETS, notional=10_000, leverage=100)
        self.assertTrue(ok)

    def test_leverage_exceeding_the_matching_bracket_fails(self):
        # notional 100,000 falls in the second bracket (max 100x), but the
        # pair is configured for 125x - the exchange would cap/reject this.
        ok, msg = _leverage_fits_bracket(SAMPLE_BRACKETS, notional=100_000, leverage=125)
        self.assertFalse(ok)
        self.assertIn("100x", msg)

    def test_notional_above_every_bracket_fails(self):
        ok, msg = _leverage_fits_bracket(SAMPLE_BRACKETS, notional=5_000_000, leverage=10)
        self.assertFalse(ok)
        self.assertIn("exceeds every leverage bracket", msg)

    def test_boundary_notional_belongs_to_the_lower_bracket_unambiguously(self):
        # Exactly on the boundary between bracket 1 (cap 50,000) and
        # bracket 2 (floor 50,000) - must belong to bracket 1 (its cap is
        # inclusive), not bracket 2, so 125x still passes here.
        ok, msg = _leverage_fits_bracket(SAMPLE_BRACKETS, notional=50_000.0, leverage=125)
        self.assertTrue(ok, "the boundary value's own bracket cap is inclusive - must use bracket 1, not bracket 2")

        # Just above the boundary, it genuinely belongs to bracket 2 - 125x must now fail.
        ok2, msg2 = _leverage_fits_bracket(SAMPLE_BRACKETS, notional=50_000.01, leverage=125)
        self.assertFalse(ok2, "just past the boundary must now be evaluated against bracket 2's cap (100x)")


class TestLeverageBracketCheckIsWiredIntoEntry(unittest.IsolatedAsyncioTestCase):
    """Integration test: proves the check is actually called during a real
    entry and can genuinely refuse one - not just that the pure function
    above is correct in isolation."""

    def _long_snap(self, close=100.0):
        return make_snap(
            close=close, high=close + 0.5, low=close - 0.5,
            di_plus=30.0, di_minus=10.0, adx=30.0, atr=2.0,
            long_condition=True, short_condition=False,
        )

    async def test_entry_refused_when_configured_leverage_exceeds_the_bracket(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0

        async def restrictive_brackets(symbol):
            # Even a tiny position is only allowed 2x here - guarantees a
            # mismatch against any realistic configured leverage.
            return [{"max_leverage": 2, "notional_floor": 0.0, "notional_cap": float("inf")}]

        inst.client.get_leverage_brackets = restrictive_brackets
        entered = {"called": False}
        original_market_order = inst.client.market_order

        async def spy_market_order(*a, **k):
            entered["called"] = True
            return await original_market_order(*a, **k)
        inst.client.market_order = spy_market_order

        p = tv.params()  # default leverage=12.0, well above the 2x cap above

        await inst._do_enter(self._long_snap(), p)

        self.assertFalse(entered["called"], "must refuse the entry when leverage exceeds the bracket's cap")

    async def test_fetch_failure_does_not_block_the_entry(self):
        """Fails OPEN on a fetch/parse error - this is a supplementary
        check, not a new way to block trading over a network hiccup."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0

        async def failing_brackets(symbol):
            raise ConnectionError("network down")

        inst.client.get_leverage_brackets = failing_brackets
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(inst.state.status, "IN_POSITION",
                         "a failed bracket fetch must not block the entry")

    async def test_repeated_fetch_failures_escalate_to_telegram_once(self):
        """2026-09-15 fix: 3 consecutive bracket-fetch failures must
        trigger exactly one Telegram alert - not zero (silent), not one
        per failure (spam). Patches tg.notify_error directly (it has no
        return value to inspect via _notify's coroutine) and filters for
        the bracket-specific message, since a routine "trade opened"
        notification also fires on every successful entry regardless."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0

        async def failing_brackets(symbol):
            raise ConnectionError("network down")

        inst.client.get_leverage_brackets = failing_brackets
        bracket_alerts = []
        import app.instance as instance_module

        async def fake_notify_error(account_name, symbol, error, enabled):
            if "leverage-bracket check" in error.lower():
                bracket_alerts.append(error)

        with mock.patch.object(instance_module.tg, "notify_error", fake_notify_error):
            p = tv.params()
            await inst._do_enter(self._long_snap(), p)
            await inst._do_enter(self._long_snap(), p)
            self.assertEqual(len(bracket_alerts), 0, "must not alert before the threshold is reached")
            await inst._do_enter(self._long_snap(), p)

        self.assertEqual(len(bracket_alerts), 1, "must alert exactly once once the threshold is crossed")

    async def test_recovering_resets_the_escalation_streak(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0

        call_count = {"n": 0}
        original = inst.client.get_leverage_brackets

        async def sometimes_failing(symbol):
            call_count["n"] += 1
            if call_count["n"] <= 3:
                raise ConnectionError("network down")
            return await original(symbol)

        inst.client.get_leverage_brackets = sometimes_failing
        p = tv.params()

        for _ in range(3):
            await inst._do_enter(self._long_snap(), p)
        self.assertEqual(inst._bracket_fetch_failures.count, 3)

        await inst._do_enter(self._long_snap(), p)  # 4th call succeeds - resets the streak
        self.assertEqual(inst._bracket_fetch_failures.count, 0)


if __name__ == "__main__":
    unittest.main()
