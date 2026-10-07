"""
test_instance_safety.py
========================

Regression tests for the execution-layer safety fixes made in response to
code review (never touches strategy.py):

  - F-01: startup/cleanup must NEVER cancel an order it didn't place itself.
  - F-04 residual: duplicate own SL/TP orders found on resume must be
    deduplicated (keep newest, cancel the rest) rather than picked arbitrarily.
  - Naked-position recovery: if protective-order placement fails after a
    market entry, the instance must end up either protected or in an explicit
    UNPROTECTED state that keeps retrying - never silently back to IDLE while
    a real position is open.
  - F-05: closing a position must be idempotent - local state is only reset
    once a position query actually confirms flat, not from the order
    response alone, and retries every tick (not gated on the next candle)
    until that's confirmed.

Uses tests/fake_client.py, a deterministic stand-in for the real Binance
client - no network, no real account needed.
"""

import os
import sys
import shutil
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402  (registers a stub only if aiohttp isn't really installed)

TEST_DATA_DIR = "/tmp/hull_bot_test_instance_safety"
os.environ["DATA_DIR"] = TEST_DATA_DIR
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

from app.instance import BotInstance  # noqa: E402
from app.store import PairConfig  # noqa: E402
from app import strategy as strat  # noqa: E402
from tests._snap import make_snap  # noqa: E402
from app import risk_guard  # noqa: E402
from tests.fake_client import FakeClient  # noqa: E402
from tests import _test_values as tv  # noqa: E402

_TREND_ALLOC = tv.STRATEGY_VALUES["trend_alloc_pct"]
_COUNTER_ALLOC = tv.STRATEGY_VALUES["counter_alloc_pct"]
_LEV = tv.STRATEGY_VALUES["leverage"]
_STOP_PCT = tv.STRATEGY_VALUES["stop_loss_pct_equity"]
_HOLD_ADX = tv.STRATEGY_VALUES["hold_adx_level"]


def make_instance() -> BotInstance:
    # BASE V3: every test instance starts with a FRESH tracker (the tracker
    # file persists per account+pair, so tests must not share one).
    from app import base_v3_tracker
    base_v3_tracker._path("binance", "acc1", "BTCUSDT").unlink(missing_ok=True)
    pc = tv.pair_config(symbol="BTCUSDT")
    inst = BotInstance(
        account_id="acc1", account_name="Test Account", symbol="BTCUSDT",
        api_key="fake", api_secret="fake", testnet=True, pair_config=pc,
    )
    inst.client = FakeClient()
    return inst


class TestCancelOwnOrdersOnly(unittest.IsolatedAsyncioTestCase):
    """F-01: the one and only cleanup path must never touch a foreign order."""

    async def test_only_own_prefixed_orders_are_cancelled(self):
        inst = make_instance()
        inst.client.open_algo_orders = [
            {"algoId": 1, "orderType": "STOP_MARKET", "triggerPrice": "95",
             "clientAlgoId": "hullbot_own_stop"},
            {"algoId": 2, "orderType": "STOP_MARKET", "triggerPrice": "0",
             "clientAlgoId": "some_other_tool_order"},
            {"algoId": 3, "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "110",
             "clientAlgoId": ""},  # manual order, no client id at all
        ]

        await inst._cancel_own_orders()

        cancelled_ids = list(inst.client.cancel_order_calls)
        self.assertEqual(cancelled_ids, [1], "must cancel ONLY the order with the bot's client-id prefix")
        self.assertNotIn(2, cancelled_ids, "must never cancel a foreign order")
        self.assertNotIn(3, cancelled_ids, "must never cancel an order with no matching client id")

    async def test_blanket_cancel_is_only_called_in_the_pre_entry_cleanup(self):
        """Static check, updated per an explicit, informed instruction from the
        account owner (who confirmed this symbol is never manually traded):
        the symbol-wide blanket cancel is now DELIBERATELY called in exactly
        one place - right before a brand-new entry, to guarantee a clean
        slate in one-way mode (a leftover order/position would otherwise
        silently ADD TO the new entry rather than create an isolated one).
        Every other cleanup path (routine reconciliation, closing, emergency
        flatten) must still use _cancel_own_orders exclusively - this test
        guards that the blanket call did not leak into any of those."""
        import inspect
        import app.instance as instance_module
        source = inspect.getsource(instance_module.BotInstance)

        occurrences = source.count("self.client.cancel_all_open_orders")
        self.assertEqual(occurrences, 1,
                         "blanket cancel-all must appear in exactly one place (pre-entry), not zero, not more")

        # Confirm it's specifically inside _do_enter, not some other method.
        do_enter_src = inspect.getsource(instance_module.BotInstance._do_enter)
        self.assertIn("self.client.cancel_all_open_orders", do_enter_src)

        for method_name in ("_startup_reconcile", "_cancel_own_orders", "_retry_close",
                            "_resolve_unprotected_position", "_handle_position_closed_externally"):
            method_src = inspect.getsource(getattr(instance_module.BotInstance, method_name))
            self.assertNotIn("self.client.cancel_all_open_orders", method_src,
                             f"{method_name} must only ever cancel this bot's own tagged orders")

    async def test_pre_entry_cleanup_actually_invokes_cancel_all(self):
        """Behavioral companion to the static check above: confirm _do_enter
        really calls the blanket cancel at runtime, not just that the source
        contains the string."""
        inst = make_instance()
        inst.client.open_algo_orders = [
            {"algoId": 1, "orderType": "STOP_MARKET", "triggerPrice": "0", "clientAlgoId": "some_other_tool_order"},
        ]
        # A minimal snapshot that triggers a LONG entry.
        import pandas as pd
        from app import strategy as strat
        snap = make_snap(
            close=100.0, high=100.5, low=99.5,
            di_plus=30.0, di_minus=10.0, adx=30.0, atr=2.0,
            long_condition=True, short_condition=False,
        )
        called = {"count": 0}
        original = inst.client.cancel_all_open_orders

        async def wrapped(symbol):
            called["count"] += 1
            return await original(symbol)

        inst.client.cancel_all_open_orders = wrapped
        inst.mark_feed.last_message_at = time.time()  # feed must be "fresh", not fail-safe stale, to enter
        p = tv.params()
        await inst._do_enter(snap, p)
        self.assertEqual(called["count"], 1, "pre-entry cleanup must call cancel_all_open_orders exactly once")


class TestStartupDedup(unittest.IsolatedAsyncioTestCase):
    """F-04 residual: multiple own SL (or TP) orders found on resume must be
    deduplicated to the most recent one, not picked arbitrarily."""

    async def test_duplicate_stop_orders_are_deduplicated(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        inst.client.open_algo_orders = [
            {"algoId": 10, "orderType": "STOP_MARKET", "triggerPrice": "90",
             "clientAlgoId": "hullbot_old_stop"},
            {"algoId": 20, "orderType": "STOP_MARKET", "triggerPrice": "95",
             "clientAlgoId": "hullbot_new_stop"},
        ]

        await inst._startup_reconcile()

        cancelled_ids = list(inst.client.cancel_order_calls)
        self.assertEqual(cancelled_ids, [10], "the older duplicate stop must be cancelled")
        self.assertEqual(inst.state.sl_order_id, 20, "the newer duplicate stop must be the one kept")
        self.assertEqual(inst.state.sl_price, 95.0)


class TestStartupUnprotectedDetection(unittest.IsolatedAsyncioTestCase):
    """Only IN_POSITION if a real STOP is confirmed resting; otherwise
    UNPROTECTED, which _tick() retries every cycle with top priority.
    BASE V3: there is no take-profit ORDER, so the stop alone is full
    protection - a missing TP must NOT mark the position unprotected, and a
    leftover take-profit order from the old strategy is cancelled."""

    async def test_stop_found_marks_in_position(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        inst.client.open_algo_orders = [
            {"algoId": 1, "orderType": "STOP_MARKET", "triggerPrice": "90", "clientAlgoId": "hullbot_sl"},
        ]
        await inst._startup_reconcile()
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(inst.state.sl_price, 90.0)

    async def test_leftover_old_strategy_tp_is_cancelled_and_stop_kept(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        inst.client.open_algo_orders = [
            {"algoId": 1, "orderType": "STOP_MARKET", "triggerPrice": "90", "clientAlgoId": "hullbot_sl"},
            {"algoId": 2, "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "110", "clientAlgoId": "hullbot_tp"},
        ]
        await inst._startup_reconcile()
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertIn(2, inst.client.cancel_order_calls, "a leftover TP must be retired - Base V3 has none")
        self.assertNotIn(1, inst.client.cancel_order_calls, "the stop must never be cancelled")

    async def test_foreign_tp_is_never_touched(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        inst.client.open_algo_orders = [
            {"algoId": 1, "orderType": "STOP_MARKET", "triggerPrice": "90", "clientAlgoId": "hullbot_sl"},
            {"algoId": 9, "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "110", "clientAlgoId": "manual_x"},
        ]
        await inst._startup_reconcile()
        self.assertNotIn(9, inst.client.cancel_order_calls)

    async def test_missing_sl_marks_unprotected_not_in_position(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        inst.client.open_algo_orders = [
            {"algoId": 2, "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "110", "clientAlgoId": "hullbot_tp"},
        ]
        await inst._startup_reconcile()
        self.assertEqual(inst.state.status, "UNPROTECTED",
                         "a resumed position missing its stop must never be marked IN_POSITION")

    async def test_neither_found_marks_unprotected(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        inst.client.open_algo_orders = []
        await inst._startup_reconcile()
        self.assertEqual(inst.state.status, "UNPROTECTED")

    async def test_recovery_replaces_the_saved_fixed_stop_not_a_recalculated_one(self):
        """Restart with no stop resting but a saved Base V3 trade state: the
        recovery must place the SAME fixed stop the trade opened with."""
        import app.position_state as ps
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        inst.client.open_algo_orders = []
        inst.client.available_balance = 999_999.0   # would give a very different stop if recalculated
        ps.save_position_state(inst.account_id, inst.symbol, ps.PositionSnapshot(
            direction="LONG", entry_price=100.0, qty=0.5, equity_at_entry=10_000.0,
            opened_at=time.time(), stop_price=88.0, bot_qty=0.5))
        await inst._startup_reconcile()
        self.assertEqual(inst.state.status, "UNPROTECTED")
        await inst._resolve_unprotected_position()
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(inst.client.stop_orders[-1][2], 88.0, "must re-place the saved fixed stop exactly")
        ps.clear_position_state(inst.account_id, inst.symbol)


class TestNakedPositionRecovery(unittest.IsolatedAsyncioTestCase):
    """Protective-order placement failing after entry must never silently
    fall back to IDLE while a real position is open."""

    async def test_placement_failure_then_recovery_reprotects(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}
        inst.client.fail_stop_market_order = True
        inst.client.fail_close_position = True  # emergency flatten also fails on this first attempt,
                                                  # so the very first _enter_unprotected_state call
                                                  # (which itself immediately tries to resolve once)
                                                  # has nothing that can succeed yet.

        await inst._enter_unprotected_state("LONG", 0.1, 100.0, reason="simulated placement failure")

        self.assertEqual(inst.state.status, "UNPROTECTED",
                         "must land in the explicit UNPROTECTED state, never silently IDLE")

        # Now the exchange calls start succeeding again (transient failure resolved).
        inst.client.fail_stop_market_order = False
        inst.client.fail_close_position = False
        await inst._resolve_unprotected_position()

        self.assertEqual(inst.state.status, "IN_POSITION", "must recover to protected once placement succeeds")
        self.assertIsNotNone(inst.state.sl_order_id)
        self.assertEqual(inst.client.tp_orders, [], "Base V3 never places a take-profit order")

    async def test_placement_failure_persists_then_emergency_flattens(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}
        inst.client.fail_stop_market_order = True   # protection placement keeps failing
        inst.client.close_result_confirms_flat = True

        await inst._enter_unprotected_state("LONG", 0.1, 100.0, reason="simulated placement failure")
        self.assertEqual(inst.state.status, "IDLE", "with protection unrecoverable, must flatten and confirm flat")
        self.assertEqual(len(inst.client.close_orders), 1, "must have attempted exactly one emergency close")

    async def test_emergency_close_not_confirmed_stays_unprotected(self):
        """If Binance's own position query still shows the position open right
        after the close attempt (race/partial fill), state must NOT be reset -
        it must stay UNPROTECTED so the next tick retries."""
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}
        inst.client.fail_stop_market_order = True
        inst.client.close_result_confirms_flat = False  # position query still shows it open after "closing"

        await inst._enter_unprotected_state("LONG", 0.1, 100.0, reason="simulated placement failure")

        self.assertEqual(inst.state.status, "UNPROTECTED",
                         "must not reset to IDLE until a position query actually confirms flat")


class TestIdempotentClose(unittest.IsolatedAsyncioTestCase):
    """F-05: closing must be driven by exchange truth, not the order response,
    and retried every tick (via CLOSING) until confirmed flat."""

    async def test_close_not_confirmed_stays_in_closing_state(self):
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.opened_at = time.time()
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}
        inst.client.close_result_confirms_flat = False  # simulate: still open after the close attempt

        await inst._close_position(reason="force_close_ema")

        self.assertEqual(inst.state.status, "CLOSING", "must stay CLOSING until exchange confirms flat")
        self.assertEqual(inst.state.direction, "LONG", "must not clear position fields before confirmation")

        # Next tick: exchange now confirms flat.
        inst.client.position = None
        await inst._retry_close()

        self.assertEqual(inst.state.status, "IDLE")
        self.assertIsNone(inst.state.direction)

    async def test_close_confirmed_immediately_resets_state(self):
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = "SHORT"
        inst.state.qty = 0.2
        inst.state.entry_price = 200.0
        inst.state.opened_at = time.time()
        inst.client.position = {"positionAmt": "-0.2", "entryPrice": "200.0"}
        inst.client.close_result_confirms_flat = True

        await inst._close_position(reason="force_close_ema")

        self.assertEqual(inst.state.status, "IDLE")
        self.assertIsNone(inst.state.direction)
        self.assertIsNone(inst.state.closing_reason)


class TestAmbiguousCloseFillRecovery(unittest.IsolatedAsyncioTestCase):
    """Item 15 fix: if a close request's response is lost/ambiguous but the
    position is confirmed flat afterward, the bot must try to recover the
    REAL fill price from Binance's own trade records (get_user_trades)
    rather than leaving the ledger with no exit price/PnL for a trade that
    genuinely closed."""

    def setUp(self):
        import shutil
        from pathlib import Path
        import app.ledger as ledger_module
        self._ledger_dir = Path("/tmp/hull_bot_test_ambiguous_close")
        shutil.rmtree(self._ledger_dir, ignore_errors=True)
        self._ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger_module.LEDGER_DIR = self._ledger_dir / "ledger"
        ledger_module.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger_module = ledger_module

    async def test_recovers_real_fill_price_from_user_trades(self):
        inst = make_instance()
        inst.state.status = "CLOSING"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.closing_reason = "force_close_ema"
        inst.state.opened_at = time.time()
        inst.client.fail_close_position = True   # vestigial here - see comment below
        # Starting "already flat" means the new pre-close check (item 10)
        # skips attempting a close at all, going straight to fill-recovery -
        # same end behavior this test is actually about (recovering a real
        # fill price when no order response is available), just reached via
        # "already flat from the start" rather than "close attempt failed
        # then confirmed flat". fail_close_position is unexercised in this
        # specific path but left set to document the original intent.
        inst.client.position = None              # the exchange confirms it's actually flat
        inst.client.user_trades_by_order_id = {777: [{"price": "103.5", "orderId": 777, "commission": "0.1"}]}

        await inst._retry_close()

        self.assertEqual(inst.state.status, "IDLE")
        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].exit_price, 103.5, places=6,
                               msg="must recover the real fill price, not leave it blank")

    async def test_falls_back_to_mark_price_when_no_trade_records_available(self):
        inst = make_instance()
        inst.state.status = "CLOSING"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.closing_reason = "force_close_ema"
        inst.state.opened_at = time.time()
        inst.client.fail_close_position = True  # vestigial - see the comment in the test above
        inst.client.position = None  # already flat - triggers the same fill-recovery path either way
        inst.client.user_trades_by_order_id = {}   # nothing recoverable
        inst.client.mark_price = 101.2

        await inst._retry_close()

        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertAlmostEqual(trades[0].exit_price, 101.2, places=6,
                               msg="must fall back to mark price rather than leave the ledger blank")

    async def test_recovery_path_never_runs_when_the_response_was_conclusive(self):
        """If the close response DID come back with real fill data (no
        exception), the ambiguous-close recovery path (get_user_trades)
        must never even be called - it's a fallback for the ambiguous case
        only, not a routine double-check."""
        inst = make_instance()
        inst.state.status = "CLOSING"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.closing_reason = "force_close_ema"
        inst.state.opened_at = time.time()
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}  # still open, about to be closed
        inst.client.close_result_confirms_flat = True  # the close succeeds and reflects flat immediately

        await inst._retry_close()

        # get_user_trades still gets called for commission lookup (a
        # separate, pre-existing concern - see _fetch_actual_commission),
        # always with a specific order_id. The fill-price RECOVERY path
        # specifically calls it with no order_id (order_id=None) - that
        # call must not happen when the response was already conclusive.
        self.assertNotIn(None, inst.client.get_user_trades_calls,
                         "must not attempt fill-price recovery when the close response was already conclusive")


class TestEquityAtEntryPersistence(unittest.IsolatedAsyncioTestCase):
    """BASE V3: the balance read at entry (AVAILABLE balance - owner
    decision) plus the fixed stop, TP1/TP2 tracker levels and the bot's own
    quantity are persisted on entry, cleared on close, and restored on
    restart - only after cross-checking direction/entry price against the
    actually-resumed position, so a stale leftover file is never used."""

    def setUp(self):
        import shutil
        from pathlib import Path
        import app.position_state as position_state_module
        self._dir = Path("/tmp/hull_bot_test_position_state")
        shutil.rmtree(self._dir, ignore_errors=True)
        position_state_module.POSITION_STATE_DIR = self._dir
        position_state_module.POSITION_STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.position_state_module = position_state_module

    @staticmethod
    def _long_snap(close=100.0):
        return make_snap(close=close, long_condition=True, atr=2.0)

    async def test_entry_persists_the_full_base_v3_trade_state(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.available_balance = 12345.0
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        saved = self.position_state_module.load_position_state(inst.account_id, inst.symbol)
        self.assertIsNotNone(saved, "a position snapshot must be persisted on entry")
        self.assertEqual(saved.equity_at_entry, 12345.0)
        self.assertEqual(saved.direction, "LONG")
        self.assertEqual(saved.stop_price, inst.state.stop_target)
        self.assertEqual(saved.tp1_price, inst.state.tp1_price)
        self.assertEqual(saved.tp2_price, inst.state.tp2_price)
        self.assertEqual(saved.bot_qty, inst.state.qty)
        self.assertEqual(saved.alloc_pct, _TREND_ALLOC)

    async def test_reset_clears_the_persisted_state(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        p = tv.params()
        await inst._do_enter(self._long_snap(), p)
        self.assertIsNotNone(self.position_state_module.load_position_state(inst.account_id, inst.symbol))

        inst._reset_position_state()

        self.assertIsNone(self.position_state_module.load_position_state(inst.account_id, inst.symbol),
                          "the snapshot must not linger past its own trade's lifetime")

    async def test_startup_reconcile_restores_persisted_state_when_it_matches(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        inst.client.available_balance = 999999.0  # deliberately different - must NOT be used
        self.position_state_module.save_position_state(
            inst.account_id, inst.symbol,
            self.position_state_module.PositionSnapshot(
                direction="LONG", entry_price=100.0, qty=0.5, equity_at_entry=5000.0,
                opened_at=time.time(), stop_price=92.0, tp1_price=102.0, tp2_price=106.0,
                tp1_touched=True, bot_qty=0.5, alloc_pct=20.0,
            ),
        )
        await inst._startup_reconcile()
        self.assertEqual(inst.state.equity_at_entry, 5000.0)
        self.assertEqual(inst.state.stop_target, 92.0)
        self.assertTrue(inst.state.tp1_touched)
        self.assertFalse(inst.state.tp2_touched)
        self.assertEqual(inst.state.tp2_price, 106.0)

    async def test_startup_reconcile_ignores_state_when_direction_does_not_match(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}  # resumed as LONG
        self.position_state_module.save_position_state(
            inst.account_id, inst.symbol,
            self.position_state_module.PositionSnapshot(
                direction="SHORT", entry_price=100.0, qty=0.5, equity_at_entry=5000.0,
                opened_at=time.time(), stop_price=108.0),
        )
        await inst._startup_reconcile()
        self.assertIsNone(inst.state.equity_at_entry,
                          "a direction mismatch means the saved file is stale - must not be trusted")
        self.assertIsNone(inst.state.tp1_price)

    async def test_startup_reconcile_ignores_state_when_entry_price_does_not_match(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        self.position_state_module.save_position_state(
            inst.account_id, inst.symbol,
            self.position_state_module.PositionSnapshot(
                direction="LONG", entry_price=250.0, qty=0.5, equity_at_entry=5000.0,
                opened_at=time.time()),
        )
        await inst._startup_reconcile()
        self.assertIsNone(inst.state.equity_at_entry)

    async def test_startup_reconcile_with_nothing_persisted_keeps_the_resting_stop(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "0.5", "entryPrice": "100.0"}
        inst.client.open_algo_orders = [
            {"algoId": 7, "orderType": "STOP_MARKET", "triggerPrice": "91", "clientAlgoId": "hullbot_sl"}]
        await inst._startup_reconcile()
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(inst.state.sl_price, 91.0)
        self.assertEqual(inst.state.stop_target, 91.0, "the resting stop is taken as-is, never recalculated")
        self.assertEqual(inst.state.bot_qty, 0.5)

    async def test_old_format_file_without_base_v3_fields_still_loads(self):
        import json
        path = self.position_state_module._path("acc1", "BTCUSDT")
        path.write_text(json.dumps({"direction": "LONG", "entry_price": 100.0, "qty": 0.5,
                                    "equity_at_entry": 5000.0, "opened_at": 1.0}))
        snap = self.position_state_module.load_position_state("acc1", "BTCUSDT")
        self.assertIsNotNone(snap)
        self.assertIsNone(snap.stop_price)
        self.assertFalse(snap.tp1_touched)


class TestFreshQuantityBeforeClose(unittest.IsolatedAsyncioTestCase):
    """Item 10 fix: closes previously used the locally-cached state.qty
    directly - a partial fill, manual change, or external event could have
    altered the real exchange quantity since it was last cached. Both close
    paths now re-fetch the real position size immediately before closing."""

    async def test_retry_close_uses_the_real_exchange_qty_not_the_stale_cached_one(self):
        inst = make_instance()
        inst.state.status = "CLOSING"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1  # stale - the real exchange qty has since changed
        inst.state.entry_price = 100.0
        inst.state.closing_reason = "force_close_ema"
        inst.state.opened_at = time.time()
        inst.client.position = {"positionAmt": "0.3", "entryPrice": "100.0"}  # real qty is 0.3, not 0.1
        inst.client.close_result_confirms_flat = True

        await inst._retry_close()

        self.assertEqual(len(inst.client.close_orders), 1)
        _symbol, _side, closed_qty = inst.client.close_orders[0]
        self.assertEqual(closed_qty, 0.3, "must close using the REAL exchange quantity, not the stale cached one")

    async def test_retry_close_skips_the_close_entirely_when_already_flat(self):
        inst = make_instance()
        inst.state.status = "CLOSING"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.closing_reason = "force_close_ema"
        inst.state.opened_at = time.time()
        inst.client.position = None  # already flat before any close attempt

        await inst._retry_close()

        self.assertEqual(len(inst.client.close_orders), 0,
                         "must not send a redundant close when the exchange already shows flat")
        self.assertEqual(inst.state.status, "IDLE", "must still finalize normally")

    async def test_emergency_close_uses_the_real_exchange_qty(self):
        inst = make_instance()
        inst.state.status = "UNPROTECTED"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1  # stale
        inst.state.entry_price = 100.0
        inst.state.opened_at = time.time()
        inst.client.position = {"positionAmt": "0.7", "entryPrice": "100.0"}  # real qty is 0.7
        inst.client.fail_stop_market_order = True  # force past the "re-protect" attempt straight to emergency close
        inst.client.close_result_confirms_flat = True

        await inst._resolve_unprotected_position()

        self.assertEqual(len(inst.client.close_orders), 1)
        _symbol, _side, closed_qty = inst.client.close_orders[0]
        self.assertEqual(closed_qty, 0.7, "the emergency close must use the REAL exchange quantity")


class TestFixedStopNeverAmended(unittest.IsolatedAsyncioTestCase):
    """BASE V3: the stop is FIXED for the life of the trade - there is no
    amendment path at all any more (the old _amend_sl/_amend_tp were
    removed). Managing the position over later candles must never place,
    cancel or move a stop order."""

    async def test_many_candles_never_touch_the_stop(self):
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.bot_qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.sl_order_id = 111
        inst.state.sl_price = 94.0
        inst.state.stop_target = 94.0
        inst.state.tp1_price, inst.state.tp2_price = 102.0, 106.0
        inst.client.open_algo_orders = [
            {"algoId": 111, "orderType": "STOP_MARKET", "triggerPrice": "94.0", "clientAlgoId": "hullbot_a"}]
        p = tv.params()
        for i, (close, atr) in enumerate([(101, 2.0), (105, 9.0), (110, 0.5), (99, 30.0)]):
            await inst._manage_open_position(make_snap(close=close, atr=atr, open_time=10 + i), p)
        self.assertEqual(inst.client.stop_orders, [], "no new stop may ever be placed while managing")
        self.assertEqual(inst.client.cancel_order_calls, [], "the resting stop may never be cancelled")
        self.assertEqual(inst.state.sl_price, 94.0, "the stop price never moves (no trailing, no ATR)")
        self.assertEqual(inst.state.status, "IN_POSITION")


class TestStopPlacementDuplicateProtection(unittest.IsolatedAsyncioTestCase):
    """Order-duplication protection for the ONE Base V3 protective order:
    a prior attempt whose stop actually went through (but whose response
    was lost) must be reused, never duplicated. No TP order is ever placed."""

    async def test_retry_reuses_a_stop_that_actually_succeeded_but_was_unconfirmed(self):
        inst = make_instance()
        inst.client.open_algo_orders.append({
            "algoId": 555, "orderType": "STOP_MARKET", "triggerPrice": "95.0",
            "clientAlgoId": "hullbot_prior_unconfirmed_attempt",
        })
        order, price = await inst._place_stop_order("LONG", 94.0)
        self.assertEqual(order["algoId"], 555, "must reuse the pre-existing stop, not create a new one")
        self.assertEqual(price, 95.0)
        self.assertEqual(len(inst.client.stop_orders), 0)
        stops = [o for o in inst.client.open_algo_orders if o["orderType"] == "STOP_MARKET"]
        self.assertEqual(len(stops), 1, "must never end up with two resting stops")

    async def test_fresh_placement_places_exactly_one_stop_and_no_tp(self):
        inst = make_instance()
        order, price = await inst._place_stop_order("LONG", 94.0)
        self.assertEqual(len(inst.client.stop_orders), 1)
        self.assertEqual(inst.client.tp_orders, [])
        self.assertEqual(price, 94.0)

    async def test_crossed_stop_raises_instead_of_moving_the_stop(self):
        from app.instance import StopAlreadyCrossedError
        inst = make_instance()
        inst.client.mark_price = 90.0   # already below a LONG stop at 94
        with self.assertRaises(StopAlreadyCrossedError):
            await inst._place_stop_order("LONG", 94.0)
        self.assertEqual(inst.client.stop_orders, [], "a crossed stop is never placed at a different price")

    async def test_crossed_stop_at_entry_closes_at_market(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        # fill at 100 but mark already collapsed far below the fixed stop
        orig_market = inst.client.market_order

        async def market_then_crash(symbol, side, qty):
            r = await orig_market(symbol, side, qty)
            inst.client.position = {"positionAmt": str(qty), "entryPrice": "100.0"}
            inst.client.mark_price = 1.0
            return {**r, "avgPrice": "100.0"}
        inst.client.market_order = market_then_crash
        await inst._do_enter(make_snap(close=100.0, long_condition=True), tv.params())
        self.assertEqual(inst.client.stop_orders, [])
        self.assertEqual(len(inst.client.close_orders), 1, "must close at market")
        self.assertEqual(inst.state.status, "IDLE")


class TestExposureCapUsesRealExchangeData(unittest.IsolatedAsyncioTestCase):
    """Item 17 fix: the exposure cap must be checked against REAL Binance
    positions (get_all_open_positions), not local BotInstance state - a
    sibling pair that crashed, never started, or a manually-placed
    position would otherwise be completely invisible to the old local-
    state-only check. These tests deliberately register ZERO sibling
    BotInstance objects with the manager, to prove the block/allow decision
    comes entirely from the client's real position data, not from anything
    a local instance is tracking."""

    @staticmethod
    def _long_snap(close=100.0):
        return make_snap(
            close=close, high=close + 0.5, low=close - 0.5,
            di_plus=30.0, di_minus=10.0, adx=30.0, atr=2.0,
            long_condition=True, short_condition=False,
        )

    async def test_blocked_by_a_real_position_with_no_matching_local_instance(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0
        # A real, large position on a DIFFERENT symbol exists on the
        # exchange - but NO sibling BotInstance is registered for it at all.
        inst.client.all_positions = [
            {"symbol": "ETHUSDT", "positionAmt": "10.0", "entryPrice": "900.0"},  # 9000 notional
        ]
        inst.manager = _FakeManagerForAccount(
            type("S", (), {"get_account": lambda self, aid: type(
                "A", (), {"max_account_exposure_pct": 95.0})()})(),
            [inst],  # only itself registered - no sibling instance for ETHUSDT at all
        )
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(inst.state.status, "IDLE",
                         "must be blocked by the real ETHUSDT position even though no local "
                         "instance tracks it")

    async def test_allowed_when_real_positions_are_within_the_cap(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0
        inst.client.all_positions = [
            {"symbol": "ETHUSDT", "positionAmt": "0.1", "entryPrice": "900.0"},  # small, 90 notional
        ]
        # The entry notional produced by the test sizing values is well below the cap -
        # the cap needs enough headroom above it plus the existing 90 to still count
        # as "comfortably within".
        inst.manager = _FakeManagerForAccount(
            type("S", (), {"get_account": lambda self, aid: type(
                "A", (), {"max_account_exposure_pct": 600.0})()})(),
            [inst],
        )
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(inst.state.status, "IN_POSITION", "must be allowed when comfortably within the cap")

    async def test_own_symbol_position_is_excluded_from_the_real_data_too(self):
        """A pair re-checking its own entry must not double-count a stale
        position entry for ITSELF that might still be in the real data
        (e.g. right at the edge of a just-closed trade)."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 10_000.0
        inst.client.all_positions = [
            {"symbol": inst.symbol, "positionAmt": "999.0", "entryPrice": "999.0"},  # huge, but it's US
        ]
        inst.manager = _FakeManagerForAccount(
            type("S", (), {"get_account": lambda self, aid: type(
                "A", (), {"max_account_exposure_pct": 600.0})()})(),
            [inst],
        )
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(inst.state.status, "IN_POSITION",
                         "must exclude this pair's OWN symbol from the exposure sum")


class TestPreEntryCleanupVerification(unittest.IsolatedAsyncioTestCase):
    """Item 13 fix: previously, if the pre-entry cancel-all-orders call
    failed, the bot proceeded with the entry regardless. Now it re-verifies
    the symbol is actually clean first - only proceeding if nothing is
    confirmed still resting, and skipping the entry entirely if something
    is (or if verification itself can't be completed)."""

    @staticmethod
    def _long_snap(close=100.0):
        return make_snap(
            close=close, high=close + 0.5, low=close - 0.5,
            di_plus=30.0, di_minus=10.0, adx=30.0, atr=2.0,
            long_condition=True, short_condition=False,
        )

    async def test_proceeds_when_cleanup_fails_but_symbol_is_actually_clean(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.fail_cancel_all_open_orders = True
        # Nothing actually resting, despite the cleanup call itself failing.
        inst.client.open_orders = []
        inst.client.open_algo_orders = []
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(inst.state.status, "IN_POSITION",
                         "must still proceed when verification confirms the symbol really is clean")

    async def test_skips_entry_when_cleanup_fails_and_orders_are_still_resting(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.fail_cancel_all_open_orders = True
        inst.client.open_orders = [{"orderId": 1, "type": "LIMIT"}]  # something genuinely still resting
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(inst.state.status, "IDLE",
                         "must skip the entry rather than proceed with confirmed stale orders present")
        self.assertEqual(len(inst.client.market_orders), 0, "no entry order should have been sent at all")

    async def test_skips_entry_when_cleanup_fails_and_verification_also_fails(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.fail_cancel_all_open_orders = True

        async def failing_get_open_orders(symbol):
            raise RuntimeError("simulated network failure")

        inst.client.get_open_orders = failing_get_open_orders
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(inst.state.status, "IDLE",
                         "must skip the entry rather than guess when verification itself fails")
        self.assertEqual(len(inst.client.market_orders), 0)

    async def test_normal_entry_unaffected_when_cleanup_succeeds(self):
        """Regression guard: the ordinary, successful-cleanup path must
        behave exactly as before."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(inst.state.status, "IN_POSITION")


class TestAvailableBalanceSizing(unittest.IsolatedAsyncioTestCase):
    """The AVAILABLE balance read at entry is the one number used for the
    size, the fixed stop and the tracker %. Total equity is used ONLY for the
    account exposure-cap ratio. (Expected values are calculated from the
    test placeholder settings, never hard-coded.)"""

    @staticmethod
    def _long_snap(close=100.0, alloc=None):
        return make_snap(close=close, long_condition=True, atr=2.0,
                         long_alloc_pct=_TREND_ALLOC if alloc is None else alloc)

    async def test_qty_is_sized_from_available_balance_not_total_equity(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 2_000.0
        p = tv.params()
        await inst._do_enter(self._long_snap(), p)
        expected_qty = round(strat.position_qty(2_000.0, _TREND_ALLOC, _LEV, 100.0), 6)
        self.assertEqual(inst.state.qty, expected_qty)
        self.assertGreater(expected_qty, 0)

    async def test_counter_trend_allocation_is_the_one_in_the_snapshot(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.available_balance = 10_000.0
        await inst._do_enter(self._long_snap(alloc=_COUNTER_ALLOC), tv.params())
        # this snapshot is trend-aligned for leverage purposes, so the trend leverage applies
        self.assertAlmostEqual(inst.state.qty, 10_000 * (_COUNTER_ALLOC / 100.0) * _LEV / 100.0)
        self.assertEqual(inst.state.alloc_pct, _COUNTER_ALLOC)

    async def test_stop_uses_available_balance_and_real_filled_qty(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 2_000.0
        await inst._do_enter(self._long_snap(), tv.params())
        # stop = entry - (available balance x stop%) / real filled qty
        expected_stop = 100.0 - (2_000.0 * _STOP_PCT / 100.0) / inst.state.qty
        self.assertAlmostEqual(inst.state.stop_target, expected_stop)
        self.assertAlmostEqual(inst.state.sl_price, expected_stop, delta=0.01)   # placed price is rounded to the 0.01 tick
        self.assertEqual(inst.state.equity_at_entry, 2_000.0)

    async def test_exposure_cap_check_uses_total_equity_not_available_balance(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        inst.client.equity = 10_000.0
        inst.client.available_balance = 2_000.0
        p = tv.params()
        captured = {}
        original = risk_guard.check_new_entry_allowed

        def spy(instances, exclude_symbol, new_notional, equity, max_account_exposure_pct):
            captured["equity"] = equity
            return original(instances, exclude_symbol, new_notional, equity, max_account_exposure_pct)

        risk_guard.check_new_entry_allowed = spy
        try:
            inst.manager = _FakeManagerForAccount(
                type("S", (), {"get_account": lambda self, aid: type(
                    "A", (), {"max_account_exposure_pct": 500.0})()})(),
                [inst],
            )
            await inst._do_enter(self._long_snap(), p)
        finally:
            risk_guard.check_new_entry_allowed = original
        self.assertEqual(captured.get("equity"), 10_000.0,
                         "the exposure cap ratio must be checked against TOTAL equity")


class TestUnprotectedRecoveryWithoutSavedState(unittest.IsolatedAsyncioTestCase):
    """A position with no resting stop and NO saved Base V3 state (e.g.
    adopted at startup): the recovery must still protect it, computing a
    stop from the best balance available and saving it, rather than leaving
    the position naked."""

    async def test_protects_using_current_available_balance_as_best_effort(self):
        inst = make_instance()
        inst.client.position = {"positionAmt": "6", "entryPrice": "100.0"}
        inst.client.available_balance = 2_000.0
        inst.state.status = "UNPROTECTED"
        inst.state.direction = "LONG"
        inst.state.qty = 6.0
        inst.state.bot_qty = 6.0
        inst.state.entry_price = 100.0
        await inst._resolve_unprotected_position()
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertAlmostEqual(inst.state.sl_price, 100.0 - 2_000.0 * _STOP_PCT / 100.0 / 6.0, delta=0.01)
        self.assertEqual(inst.client.tp_orders, [])


class TestStartupReconcileFailsClosed(unittest.IsolatedAsyncioTestCase):
    """Regression test: previously, if _startup_reconcile() raised, the
    exception was logged but _run() fell through into the main trading loop
    anyway with incomplete state (no direction/entry_price even though a real
    position might exist) - which could crash deep inside position
    management. It must now fail closed: retry a few times, then return
    without ever entering the loop."""

    async def test_persistent_reconcile_failure_returns_without_entering_loop(self):
        inst = make_instance()

        async def always_fails():
            raise RuntimeError("simulated network failure")

        inst._startup_reconcile = always_fails
        # Avoid actually sleeping through the real 3s backoff between retries.
        with unittest.mock.patch("app.instance.asyncio.sleep", new=unittest.mock.AsyncMock()):
            await inst._run()

        self.assertEqual(inst.state.status, "ERROR")
        self.assertIn("startup reconciliation failed", inst.state.last_error)
        # The main loop must never have started - no tick-related state should
        # have been touched, and no "started" log line should be present.
        self.assertFalse(any("Bot started on" in line for line in inst.logs))

    async def test_reconcile_succeeding_on_a_later_attempt_still_starts_normally(self):
        inst = make_instance()
        inst.client.position = None
        attempts = {"n": 0}
        real_reconcile = inst._startup_reconcile

        async def flaky_then_ok():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("transient failure")
            await real_reconcile()

        inst._startup_reconcile = flaky_then_ok
        inst._stop_requested = True  # so the while loop exits immediately after starting
        with unittest.mock.patch("app.instance.asyncio.sleep", new=unittest.mock.AsyncMock()):
            await inst._run()

        self.assertNotEqual(inst.state.status, "ERROR")
        self.assertTrue(any("Bot started on" in line for line in inst.logs))


class TestRealTimeAlgoUpdate(unittest.IsolatedAsyncioTestCase):
    """Real-time SL/TP fill detection via Binance's ALGO_UPDATE push event
    (see user_data_stream.py) - the fast path in front of the existing
    REST-polling safety net (_handle_position_closed_externally). Field
    names match the WebSocket shorthand keys confirmed against Binance's
    own raw JSON example (aid/X/ai/ap), genuinely different from the REST
    Algo Order API's field names for the same concepts."""

    def setUp(self):
        import shutil
        from pathlib import Path
        import app.ledger as ledger_module
        self._ledger_dir = Path("/tmp/hull_bot_test_realtime_algo_update")
        shutil.rmtree(self._ledger_dir, ignore_errors=True)
        ledger_module.LEDGER_DIR = self._ledger_dir / "ledger"
        ledger_module.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger_module = ledger_module

    def _in_position_instance(self):
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.sl_order_id = 111
        inst.state.sl_price = 95.0
        inst.state.opened_at = time.time()
        return inst

    async def test_sl_trigger_closes_the_position(self):
        inst = self._in_position_instance()
        await inst._on_algo_update({"aid": 111, "X": "TRIGGERED", "ai": "999", "ap": "94.5"})

        self.assertEqual(inst.state.status, "IDLE")
        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "SL")
        self.assertAlmostEqual(trades[0].exit_price, 94.5, places=6)

    async def test_non_stop_algo_id_is_ignored_base_v3_has_no_tp_order(self):
        inst = self._in_position_instance()
        await inst._on_algo_update({"aid": 222, "X": "FINISHED", "ai": "888", "ap": "110.2"})
        self.assertEqual(inst.state.status, "IN_POSITION",
                         "Base V3 places no TP order - any other algo id is not ours and is ignored")

    async def test_unrelated_algo_id_is_ignored(self):
        inst = self._in_position_instance()
        await inst._on_algo_update({"aid": 999999, "X": "TRIGGERED", "ai": "1", "ap": "100.0"})

        self.assertEqual(inst.state.status, "IN_POSITION", "an algo update for an unrecognized order must be ignored")

    async def test_non_terminal_status_is_ignored(self):
        inst = self._in_position_instance()
        for status in ("NEW", "CANCELED", "TRIGGERING", "REJECTED", "EXPIRED"):
            await inst._on_algo_update({"aid": 111, "X": status, "ai": "", "ap": "0"})
            self.assertEqual(inst.state.status, "IN_POSITION",
                             f"algoStatus={status} must not be treated as a fill")

    async def test_ignored_when_not_currently_in_position(self):
        inst = self._in_position_instance()
        inst.state.status = "IDLE"
        await inst._on_algo_update({"aid": 111, "X": "TRIGGERED", "ai": "1", "ap": "94.5"})

        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(trades), 0, "must not process a fill when not tracking an open position")

    async def test_race_against_the_poll_path_only_processes_once(self):
        """Simulates the exact scenario this design is meant to handle
        safely: the real-time event and the REST-polling safety net both
        notice the same closure. Only one must actually finalize it."""
        inst = self._in_position_instance()

        # Real-time path fires first...
        await inst._on_algo_update({"aid": 111, "X": "TRIGGERED", "ai": "999", "ap": "94.5"})
        # ...then the poll-based path (_handle_position_closed_externally)
        # also runs, as it would on the next tick before it's noticed the
        # status already moved on. It queries algo-order status itself, so
        # give it something to find (harmless either way, since the
        # IN_POSITION guard inside _finalize_external_close should make this
        # a no-op regardless of what it queries).
        inst.client.open_algo_orders = []  # nothing resting anymore - already handled
        await inst._handle_position_closed_externally()

        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(trades), 1, "the closure must be recorded exactly once, not twice")

    async def test_reverts_to_in_position_when_exchange_still_shows_open(self):
        """Item 5 fix: the real-time path must not trust the ALGO_UPDATE
        event alone - it must confirm exchange-flat before finalizing. If
        the exchange still shows an open position, revert to IN_POSITION
        (not CLOSING - there's no bot-initiated close order to retry here)
        and let the next tick's poll or a later event re-check, rather than
        finalizing on unconfirmed data or forcing an unnecessary close."""
        inst = self._in_position_instance()
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}  # exchange says still open

        await inst._on_algo_update({"aid": 111, "X": "TRIGGERED", "ai": "999", "ap": "94.5"})

        self.assertEqual(inst.state.status, "IN_POSITION",
                         "must revert to IN_POSITION, not finalize, when the exchange disagrees")
        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(trades), 0, "must not record a ledger entry until exchange-flat is confirmed")

    async def test_partial_fill_updates_qty_and_keeps_managing_the_remainder(self):
        """2026-09-14 fix: a SMALLER remaining position (not the same
        quantity as before) means a partial fill/close happened - the bot
        must update its own qty to match reality and keep managing what's
        left, not silently revert as if nothing changed at all."""
        inst = self._in_position_instance()  # cached qty is 0.1
        inst.client.position = {"positionAmt": "0.04", "entryPrice": "100.0"}  # real remaining is smaller

        await inst._on_algo_update({"aid": 111, "X": "TRIGGERED", "ai": "999", "ap": "94.5"})

        self.assertEqual(inst.state.status, "IN_POSITION", "must keep managing the remaining position")
        self.assertAlmostEqual(inst.state.qty, 0.04, places=6,
                               msg="must update qty to the REAL remaining amount, not the stale cached one")
        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(trades), 0, "a partial fill is not a full close - no ledger entry yet")

    async def test_same_remaining_qty_does_not_trigger_partial_fill_handling(self):
        """Regression guard: if the remaining quantity is genuinely the
        SAME as cached (a premature/stale event, not an actual partial
        fill), it must just revert normally - not be misread as a partial
        close of its own full size."""
        inst = self._in_position_instance()  # cached qty is 0.1
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}  # identical remaining qty

        await inst._on_algo_update({"aid": 111, "X": "TRIGGERED", "ai": "999", "ap": "94.5"})

        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertAlmostEqual(inst.state.qty, 0.1, places=6, msg="qty must be unchanged when it already matches")

    async def test_finalizes_once_exchange_confirms_flat(self):
        """Companion to the test above: once the exchange DOES confirm
        flat, the exact same event now finalizes normally - proving the
        new check is a genuine gate, not something that silently blocks
        every closure."""
        inst = self._in_position_instance()
        inst.client.position = None  # exchange confirms flat

        await inst._on_algo_update({"aid": 111, "X": "TRIGGERED", "ai": "999", "ap": "94.5"})

        self.assertEqual(inst.state.status, "IDLE")
        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(trades), 1)

    async def test_flatness_check_failure_reverts_safely_rather_than_finalizing_blind(self):
        """If the confirmation check itself fails (network hiccup), the
        code must NOT fall back to trusting the event alone - it should
        revert to IN_POSITION and let a later attempt re-check, exactly
        like the "still open" case."""
        inst = self._in_position_instance()

        async def failing_get_position_risk(symbol):
            raise RuntimeError("simulated network failure")

        inst.client.get_position_risk = failing_get_position_risk

        await inst._on_algo_update({"aid": 111, "X": "TRIGGERED", "ai": "999", "ap": "94.5"})

        self.assertEqual(inst.state.status, "IN_POSITION",
                         "a failed confirmation check must never be treated as confirmed flat")
        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(trades), 0)


class TestRealCommissionFetch(unittest.IsolatedAsyncioTestCase):
    """Regression tests for the commission fix: live trading must pull REAL
    commission from Binance (via get_user_trades) and never estimate/simulate
    a fee. _record_ledger must report commission_included=False (not a guess)
    when the real data can't be fetched."""

    def setUp(self):
        import shutil
        from pathlib import Path
        import app.ledger as ledger_module
        self._ledger_dir = Path("/tmp/hull_bot_test_commission_fetch")
        shutil.rmtree(self._ledger_dir, ignore_errors=True)
        ledger_module.LEDGER_DIR = self._ledger_dir / "ledger"
        ledger_module.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger_module = ledger_module

    async def test_sums_commission_across_entry_and_exit_orders(self):
        inst = make_instance()
        inst.client.user_trades_by_order_id = {
            111: [{"commission": "0.50"}, {"commission": "0.10"}],  # entry, two partial fills
            222: [{"commission": "0.30"}],                           # exit
        }
        total = await inst._fetch_actual_commission([111, 222])
        self.assertAlmostEqual(total, 0.90, places=6)

    async def test_returns_none_when_nothing_found(self):
        inst = make_instance()
        total = await inst._fetch_actual_commission([999])  # no data configured for this id
        self.assertIsNone(total)

    async def test_none_and_zero_order_ids_are_skipped_not_errors(self):
        inst = make_instance()
        inst.client.user_trades_by_order_id = {111: [{"commission": "0.20"}]}
        total = await inst._fetch_actual_commission([None, 0, 111])
        self.assertAlmostEqual(total, 0.20, places=6)

    async def test_fetch_failure_returns_none_not_a_guess(self):
        inst = make_instance()
        inst.client.fail_get_user_trades = True
        total = await inst._fetch_actual_commission([111])
        self.assertIsNone(total, "a failed fetch must never be treated as zero or estimated")

    async def test_record_ledger_nets_out_real_commission(self):
        inst = make_instance()
        inst.state.direction = "LONG"
        inst.state.qty = 1.0
        inst.state.entry_price = 100.0
        inst.state.entry_order_id = 111
        inst.state.opened_at = time.time()
        inst.client.user_trades_by_order_id = {
            111: [{"commission": "1.00"}],
            222: [{"commission": "1.00"}],
        }
        gross_pnl = 50.0
        await inst._record_ledger("LONG", 1.0, 105.0, gross_pnl, "TP", exit_order_id=222)

        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertTrue(t.commission_included)
        self.assertAlmostEqual(t.commission, 2.0, places=6)
        self.assertAlmostEqual(t.pnl, gross_pnl - 2.0, places=6, msg="ledger pnl must be net of REAL commission")

    async def test_record_ledger_reports_gross_when_commission_unavailable(self):
        inst = make_instance()
        inst.state.direction = "LONG"
        inst.state.qty = 1.0
        inst.state.entry_price = 100.0
        inst.state.entry_order_id = 111
        inst.state.opened_at = time.time()
        # no user_trades configured at all - commission fetch will find nothing

        gross_pnl = 50.0
        await inst._record_ledger("LONG", 1.0, 105.0, gross_pnl, "TP", exit_order_id=222)

        trades = self.ledger_module.list_trades(inst.account_id, inst.symbol)
        t = trades[0]
        self.assertFalse(t.commission_included)
        self.assertIsNone(t.commission)
        self.assertAlmostEqual(t.pnl, gross_pnl, places=6,
                               msg="must report GROSS pnl (not a guessed net) when real commission is unavailable")


class TestTpTrackingOnly(unittest.IsolatedAsyncioTestCase):
    """BASE V3 TP1/TP2 are TRACKING ONLY: touching them is recorded (and
    persisted), but never closes or partially closes the position and never
    places an order."""

    def _inst(self, direction="LONG"):
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = direction
        inst.state.qty = inst.state.bot_qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.sl_price = inst.state.stop_target = 94.0 if direction == "LONG" else 106.0
        inst.state.sl_order_id = 1
        if direction == "LONG":
            inst.state.tp1_price, inst.state.tp2_price = 102.0, 106.0
        else:
            inst.state.tp1_price, inst.state.tp2_price = 98.0, 94.0
        inst.client.position = {"positionAmt": "0.1" if direction == "LONG" else "-0.1", "entryPrice": "100"}
        return inst

    async def test_long_tp1_touched_no_close(self):
        inst = self._inst("LONG")
        await inst._manage_open_position(make_snap(close=101.0, high=102.5, low=100.5), tv.params())
        self.assertTrue(inst.state.tp1_touched)
        self.assertFalse(inst.state.tp2_touched)
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(inst.client.close_orders, [])

    async def test_long_tp2_touched_no_close(self):
        inst = self._inst("LONG")
        await inst._manage_open_position(make_snap(close=105.0, high=106.0, low=104.0), tv.params())
        self.assertTrue(inst.state.tp1_touched and inst.state.tp2_touched)
        self.assertEqual(inst.client.close_orders, [])
        self.assertEqual(inst.client.tp_orders, [])

    async def test_short_tp_levels_use_the_low(self):
        inst = self._inst("SHORT")
        await inst._manage_open_position(make_snap(close=99.0, high=99.5, low=97.9), tv.params())
        self.assertTrue(inst.state.tp1_touched)
        self.assertFalse(inst.state.tp2_touched)
        self.assertEqual(inst.state.status, "IN_POSITION")


class TestSignalFlip(unittest.IsolatedAsyncioTestCase):
    """BASE V3 flips (Pine: strategy.close_all on the signal candle; the new
    side only comes from the normal flat-entry block on the NEXT candle):
      * opposite signal NOT held -> close with reason signal_flip
      * the close path never opens the new side
      * Hold Rule: in profit AND ADX < level -> keep the position
      * no EMA force-close exists any more"""

    def _long(self):
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = "LONG"
        inst.state.qty = inst.state.bot_qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.opened_at = time.time()
        inst.state.sl_price = inst.state.stop_target = 94.0
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}
        return inst

    async def test_opposite_signal_in_loss_closes_with_flip_reason(self):
        inst = self._long()
        inst.client.close_result_confirms_flat = False
        await inst._manage_open_position(make_snap(close=99.0, short_condition=True, adx=10.0),
                                         tv.params())
        self.assertEqual(inst.state.status, "CLOSING")
        self.assertEqual(inst.state.closing_reason, "signal_flip")

    async def test_opposite_signal_in_profit_with_low_adx_is_held(self):
        inst = self._long()
        await inst._manage_open_position(make_snap(close=101.0, short_condition=True, adx=_HOLD_ADX - 0.1),
                                         tv.params())
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(inst.state.held_signal_count, 1)
        self.assertEqual(inst.client.close_orders, [])

    async def test_opposite_signal_in_profit_with_adx_at_level_is_not_held(self):
        inst = self._long()
        inst.client.close_result_confirms_flat = False
        await inst._manage_open_position(make_snap(close=101.0, short_condition=True, adx=_HOLD_ADX),
                                         tv.params())
        self.assertEqual(inst.state.status, "CLOSING")

    async def test_hold_rule_off_always_closes(self):
        inst = self._long()
        inst.client.close_result_confirms_flat = False
        await inst._manage_open_position(make_snap(close=101.0, short_condition=True, adx=10.0),
                                         tv.params(use_hold_rule=False))
        self.assertEqual(inst.state.status, "CLOSING")

    async def test_flip_close_never_reenters_in_the_same_call(self):
        inst = self._long()
        inst.mark_feed.last_message_at = time.time()

        async def fake_fetch_snapshot(pp):
            return make_snap(short_condition=True), None
        inst._fetch_snapshot = fake_fetch_snapshot
        inst.client.close_result_confirms_flat = True
        await inst._close_position(reason="signal_flip")
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(inst.client.market_orders, [], "the new side may only open on the NEXT candle")

    async def test_same_direction_signal_does_nothing(self):
        inst = self._long()
        await inst._manage_open_position(make_snap(close=90.0, long_condition=True), tv.params())
        self.assertEqual(inst.state.status, "IN_POSITION")

    async def test_price_far_below_ema_without_opposite_signal_does_not_close(self):
        """Old strategy had an EMA force-close. Base V3 has none."""
        inst = self._long()
        await inst._manage_open_position(make_snap(close=80.0, ema_filter=95.0, ema_sizing=120.0),
                                         tv.params())
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(inst.client.close_orders, [])


class TestNoMaxLossCapInBaseV3(unittest.IsolatedAsyncioTestCase):
    """The old bar-close Max Loss Cap is not part of Base V3: an unrealized
    loss, however large, never closes the position at bar close - only the
    fixed stop (on the exchange) or an unheld opposite signal can."""

    async def test_large_unrealized_loss_without_signal_keeps_position(self):
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = "LONG"
        inst.state.qty = inst.state.bot_qty = 10.0
        inst.state.entry_price = 100.0
        inst.state.equity_at_entry = 1000.0
        inst.state.sl_price = inst.state.stop_target = 94.0
        await inst._manage_open_position(make_snap(close=94.5), tv.params())
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(inst.client.close_orders, [])


class _FakeManagerForAccount:
    """Minimal stand-in for BotManager, exposing just what
    _maybe_check_withdraw_alert needs: .store and .instances_for_account()."""
    def __init__(self, store, instances):
        self.store = store
        self._instances = instances

    def instances_for_account(self, account_id):
        return self._instances


class TestWithdrawAlert(unittest.IsolatedAsyncioTestCase):
    """The reference Pine script's withdrawal alertcondition(), translated as
    a Telegram-only notification (no auto-withdrawal) - checked from exactly
    one designated "leader" pair per account, throttled to ~60s, and latched
    so it never fires twice for the same crossing."""

    def setUp(self):
        import shutil
        from pathlib import Path
        import app.store as store_module
        self.store_module = store_module
        self._data_dir = Path("/tmp/hull_bot_test_withdraw_alert")
        shutil.rmtree(self._data_dir, ignore_errors=True)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        store_module.ACCOUNTS_FILE = self._data_dir / "accounts.json"
        self.store = store_module.Store()
        self.acc = self.store.create_account("Main", "k", "s", True)
        self.store.update_account(self.acc.id, withdraw_alert_enabled=True, withdraw_alert_threshold=1000.0)

    def _make_pair_instance(self, symbol):
        pc = tv.pair_config(symbol=symbol)
        inst = BotInstance(account_id=self.acc.id, account_name="Main", symbol=symbol,
                            api_key="k", api_secret="s", testnet=True, pair_config=pc)
        inst.client = FakeClient()
        return inst

    async def test_leader_pair_fires_alert_above_threshold(self):
        leader = self._make_pair_instance("AAAUSDT")
        follower = self._make_pair_instance("ZZZUSDT")
        mgr = _FakeManagerForAccount(self.store, [leader, follower])
        leader.manager = mgr
        follower.manager = mgr
        leader.client.equity = 1500.0

        await leader._maybe_check_withdraw_alert()

        acc = self.store.get_account(self.acc.id)
        self.assertTrue(acc.withdraw_alert_fired)

    async def test_follower_pair_never_checks_or_fires(self):
        leader = self._make_pair_instance("AAAUSDT")
        follower = self._make_pair_instance("ZZZUSDT")
        mgr = _FakeManagerForAccount(self.store, [leader, follower])
        leader.manager = mgr
        follower.manager = mgr
        follower.client.equity = 1500.0

        await follower._maybe_check_withdraw_alert()

        acc = self.store.get_account(self.acc.id)
        self.assertFalse(acc.withdraw_alert_fired, "only the alphabetically-first pair should perform the check")

    async def test_does_not_fire_below_threshold(self):
        leader = self._make_pair_instance("AAAUSDT")
        mgr = _FakeManagerForAccount(self.store, [leader])
        leader.manager = mgr
        leader.client.equity = 500.0

        await leader._maybe_check_withdraw_alert()

        acc = self.store.get_account(self.acc.id)
        self.assertFalse(acc.withdraw_alert_fired)

    async def test_throttled_within_60_seconds(self):
        leader = self._make_pair_instance("AAAUSDT")
        mgr = _FakeManagerForAccount(self.store, [leader])
        leader.manager = mgr
        leader.state.last_withdraw_check = time.time()  # just checked - inside the throttle window
        leader.client.equity = 1500.0

        await leader._maybe_check_withdraw_alert()

        acc = self.store.get_account(self.acc.id)
        self.assertFalse(acc.withdraw_alert_fired, "must not re-check within the 60s throttle window")

    async def test_disabled_alert_never_fires(self):
        self.store.update_account(self.acc.id, withdraw_alert_enabled=False)
        leader = self._make_pair_instance("AAAUSDT")
        mgr = _FakeManagerForAccount(self.store, [leader])
        leader.manager = mgr
        leader.client.equity = 999999.0

        await leader._maybe_check_withdraw_alert()

        acc = self.store.get_account(self.acc.id)
        self.assertFalse(acc.withdraw_alert_fired)

    async def test_already_fired_short_circuits_before_fetching_equity(self):
        self.store.mark_withdraw_alert_fired(self.acc.id)
        leader = self._make_pair_instance("AAAUSDT")
        mgr = _FakeManagerForAccount(self.store, [leader])
        leader.manager = mgr
        calls = {"n": 0}
        original_get_equity = leader.client.get_equity

        async def counting_get_equity():
            calls["n"] += 1
            return await original_get_equity()

        leader.client.get_equity = counting_get_equity

        await leader._maybe_check_withdraw_alert()

        self.assertEqual(calls["n"], 0, "already-fired accounts must not even fetch equity")


class TestRealTimeCommissionCapture(unittest.IsolatedAsyncioTestCase):
    """Item 6 fix (2026-09-14, per a third-party review): ORDER_TRADE_UPDATE
    was registered on the shared stream but never actually wired to a
    handler - dead code presented as structural completeness. Now
    _on_order_update captures real commission from these push events, and
    _fetch_actual_commission uses that cache before falling back to a
    separate REST call."""

    def _in_position_instance(self):
        inst = make_instance()
        inst.state.status = "IN_POSITION"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.opened_at = time.time()
        return inst

    async def test_captures_commission_for_own_order_on_trade_event(self):
        inst = self._in_position_instance()

        await inst._on_order_update({"c": "hullbot_entry_123", "x": "TRADE", "i": 555, "n": "0.42", "N": "USDT"})

        self.assertEqual(inst._commission_cache.get(555), 0.42)

    async def test_ignores_foreign_orders(self):
        inst = self._in_position_instance()

        await inst._on_order_update({"c": "some_other_tool_order", "x": "TRADE", "i": 555, "n": "0.42"})

        self.assertEqual(len(inst._commission_cache), 0, "must never track a foreign order's data")

    async def test_ignores_non_trade_execution_types(self):
        inst = self._in_position_instance()

        for exec_type in ("NEW", "CANCELED", "EXPIRED", "CALCULATED"):
            await inst._on_order_update({"c": "hullbot_x", "x": exec_type, "i": 555, "n": "0.42"})

        self.assertEqual(len(inst._commission_cache), 0, "only a TRADE execution type is an actual fill")

    async def test_accumulates_across_multiple_partial_fills(self):
        inst = self._in_position_instance()

        await inst._on_order_update({"c": "hullbot_x", "x": "TRADE", "i": 555, "n": "0.07"})
        await inst._on_order_update({"c": "hullbot_x", "x": "TRADE", "i": 555, "n": "0.18"})

        self.assertAlmostEqual(inst._commission_cache[555], 0.25, places=6,
                               msg="multiple TRADE events for the same order must accumulate, not overwrite")

    async def test_fetch_actual_commission_uses_the_cache_before_a_rest_call(self):
        inst = self._in_position_instance()
        inst._commission_cache[555] = 0.42
        calls = {"n": 0}
        original = inst.client.get_user_trades

        async def counting_get_user_trades(*a, **k):
            calls["n"] += 1
            return await original(*a, **k)

        inst.client.get_user_trades = counting_get_user_trades

        result = await inst._fetch_actual_commission([555])

        self.assertEqual(result, 0.42)
        self.assertEqual(calls["n"], 0, "a cached order id must never trigger a redundant REST call")

    async def test_fetch_actual_commission_falls_back_to_rest_for_uncached_orders(self):
        inst = self._in_position_instance()
        inst._commission_cache[555] = 0.42  # only 555 is cached
        inst.client.user_trades_by_order_id = {777: [{"commission": "0.10"}]}

        result = await inst._fetch_actual_commission([555, 777])

        self.assertAlmostEqual(result, 0.52, places=6,
                               msg="must combine cached data for one order with a REST fetch for the other")

    async def test_cache_is_cleared_on_position_reset(self):
        inst = self._in_position_instance()
        inst._commission_cache[555] = 0.42

        inst._reset_position_state()

        self.assertEqual(len(inst._commission_cache), 0,
                         "the cache must not leak a stale order's commission into a future trade")


class TestSkipThrottleWiring(unittest.IsolatedAsyncioTestCase):
    """Initial stop placement right after a fresh entry (a real naked window)
    must pass skip_throttle=True."""

    async def test_initial_stop_placement_skips_the_throttle(self):
        inst = make_instance()
        calls = []

        async def spy(*args, **kwargs):
            calls.append(kwargs)
            return {"algoId": 1, "triggerPrice": "94.0"}
        inst.client.stop_market_order = spy
        await inst._place_stop_order("LONG", 94.0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get("skip_throttle"), "initial stop placement must skip the throttle")


class TestPendingReconciliationWiring(unittest.IsolatedAsyncioTestCase):
    """Item 9 fix, instance-level wiring: an unresolved ambiguous order
    failure (market_order/close_position_market already tried their own
    recovery and still couldn't confirm) must flag a durable record and
    escalate via Telegram; a clean rejection must never trigger this; and
    any subsequent successful order action must clear a stale flag."""

    def setUp(self):
        import shutil
        from pathlib import Path
        import app.reconciliation as reconciliation_module
        self._dir = Path("/tmp/hull_bot_test_reconciliation_wiring")
        shutil.rmtree(self._dir, ignore_errors=True)
        reconciliation_module.RECONCILIATION_DIR = self._dir
        reconciliation_module.RECONCILIATION_DIR.mkdir(parents=True, exist_ok=True)
        self.reconciliation_module = reconciliation_module

    @staticmethod
    def _long_snap(close=100.0):
        return make_snap(
            close=close, high=close + 0.5, low=close - 0.5,
            di_plus=30.0, di_minus=10.0, adx=30.0, atr=2.0,
            long_condition=True, short_condition=False,
        )

    async def test_unresolved_ambiguous_entry_flags_reconciliation(self):
        import aiohttp
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()

        exc = aiohttp.ClientError("connection reset")
        exc.client_order_id = "hullbot_xyz"

        async def failing_market_order(*a, **k):
            raise exc

        inst.client.market_order = failing_market_order
        p = tv.params()

        with self.assertRaises(aiohttp.ClientError):
            await inst._do_enter(self._long_snap(), p)

        records = self.reconciliation_module.get_pending_reconciliations(inst.account_id, inst.symbol)
        self.assertEqual(len(records), 1, "an unresolved ambiguous entry must flag a durable record")
        self.assertEqual(records[0].client_order_id, "hullbot_xyz")
        self.assertEqual(records[0].context, "market entry")

    async def test_clean_rejection_does_not_flag_reconciliation(self):
        """A clean BinanceAPIError means Binance definitively said no - no
        ambiguity to flag, unlike a network-level failure."""
        from app.binance_futures import BinanceAPIError
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()

        async def rejecting_market_order(*a, **k):
            raise BinanceAPIError(400, {"code": -2019, "msg": "Margin is insufficient"})

        inst.client.market_order = rejecting_market_order
        p = tv.params()

        with self.assertRaises(BinanceAPIError):
            await inst._do_enter(self._long_snap(), p)

        records = self.reconciliation_module.get_pending_reconciliations(inst.account_id, inst.symbol)
        self.assertEqual(records, [], "a clean rejection must never flag pending reconciliation")

    async def test_successful_entry_clears_a_stale_flag(self):
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        self.reconciliation_module.flag_pending_reconciliation(
            inst.account_id, inst.symbol, "market entry", "hullbot_old", "stale from earlier",
        )
        p = tv.params()

        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(self.reconciliation_module.get_pending_reconciliations(inst.account_id, inst.symbol), [],
                         "a subsequent successful entry must clear a stale flag")

    async def test_unresolved_ambiguous_close_flags_reconciliation(self):
        import aiohttp
        inst = make_instance()
        inst.state.status = "CLOSING"
        inst.state.direction = "LONG"
        inst.state.qty = 0.1
        inst.state.entry_price = 100.0
        inst.state.closing_reason = "force_close_ema"
        inst.state.opened_at = time.time()
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}

        exc = aiohttp.ClientError("connection reset")
        exc.client_order_id = "hullbot_close_1"

        async def failing_close(*a, **k):
            raise exc

        inst.client.close_position_market = failing_close

        await inst._retry_close()

        records = self.reconciliation_module.get_pending_reconciliations(inst.account_id, inst.symbol)
        self.assertEqual(len(records), 1, "an unresolved ambiguous close must flag a durable record")
        self.assertEqual(records[0].context, "position close")

    async def test_pending_reconciliation_is_visible_in_the_status_dict(self):
        inst = make_instance()
        self.reconciliation_module.flag_pending_reconciliation(
            inst.account_id, inst.symbol, "market entry", "hullbot_xyz", "connection reset",
        )

        status = inst.to_status_dict()

        self.assertEqual(len(status["pending_reconciliations"]), 1,
                             "a human checking the dashboard must see this without needing Telegram/logs")
        self.assertEqual(status["pending_reconciliations"][0]["client_order_id"], "hullbot_xyz")

    async def test_status_dict_shows_empty_list_when_nothing_pending(self):
        inst = make_instance()

        status = inst.to_status_dict()

        self.assertEqual(status["pending_reconciliations"], [])

    async def test_multiple_pending_records_all_visible_in_status_dict(self):
        """Regression test for the overwrite bug - both records must show
        up, not just the most recently flagged one."""
        inst = make_instance()
        self.reconciliation_module.flag_pending_reconciliation(
            inst.account_id, inst.symbol, "market entry", "order-A", "first")
        self.reconciliation_module.flag_pending_reconciliation(
            inst.account_id, inst.symbol, "SL placement", "order-B", "second")

        status = inst.to_status_dict()

        ids = {r["client_order_id"] for r in status["pending_reconciliations"]}
        self.assertEqual(ids, {"order-A", "order-B"})

    # ---------------------------------------------------------------- entry-blocking gate (2026-09-14 fix)
    async def test_new_entry_refused_when_pending_reconciliation_and_a_real_position_exists(self):
        """The core fix: a pending flag must actually stop a new entry, not
        just be informational - and specifically here because the exchange
        confirms there IS an unexpected open position."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        self.reconciliation_module.flag_pending_reconciliation(
            inst.account_id, inst.symbol, "market entry", "hullbot_old", "ambiguous earlier entry")
        inst.client.position = {"positionAmt": "0.1", "entryPrice": "100.0"}  # exchange shows a real position
        entered = {"called": False}
        original_market_order = inst.client.market_order

        async def spy_market_order(*a, **k):
            entered["called"] = True
            return await original_market_order(*a, **k)
        inst.client.market_order = spy_market_order

        p = tv.params()
        await inst._do_enter(self._long_snap(), p)

        self.assertFalse(entered["called"], "must refuse to enter on top of an unexpected real position")
        records = self.reconciliation_module.get_pending_reconciliations(inst.account_id, inst.symbol)
        self.assertEqual(len(records), 1, "the flag must stay - this was NOT resolved, still needs a human")

    async def test_new_entry_proceeds_when_pending_reconciliation_but_exchange_confirms_flat(self):
        """Active resolution: if the exchange confirms flat, the earlier
        ambiguity clearly didn't leave a position behind - safe to clear
        and proceed with the entry in the same tick, not block forever."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        self.reconciliation_module.flag_pending_reconciliation(
            inst.account_id, inst.symbol, "market entry", "hullbot_old", "ambiguous earlier entry")
        inst.client.position = None  # exchange confirms flat

        p = tv.params()
        await inst._do_enter(self._long_snap(), p)

        self.assertEqual(inst.state.status, "IN_POSITION", "must proceed with the entry once resolved")
        records = self.reconciliation_module.get_pending_reconciliations(inst.account_id, inst.symbol)
        self.assertEqual(records, [], "resolved flag must be cleared")

    async def test_new_entry_refused_when_position_query_itself_fails(self):
        """Fail closed: if we can't even ask the exchange what's true,
        don't guess - refuse the entry and leave the flag exactly as-is."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        self.reconciliation_module.flag_pending_reconciliation(
            inst.account_id, inst.symbol, "market entry", "hullbot_old", "ambiguous earlier entry")

        async def failing_position_query(*a, **k):
            raise ConnectionError("network down")
        inst.client.get_position_risk = failing_position_query
        entered = {"called": False}

        async def spy_market_order(*a, **k):
            entered["called"] = True
        inst.client.market_order = spy_market_order

        p = tv.params()
        await inst._do_enter(self._long_snap(), p)

        self.assertFalse(entered["called"])
        records = self.reconciliation_module.get_pending_reconciliations(inst.account_id, inst.symbol)
        self.assertEqual(len(records), 1, "must stay flagged - the query itself failed, nothing was resolved")

    async def test_repeated_position_query_failures_escalate_to_telegram_once(self):
        """2026-09-15 fix: this path used to only ever log a failure, with
        no escalation at all - a sustained outage while a flag is pending
        could block entries silently, forever. 3 consecutive failures must
        now trigger exactly one alert."""
        inst = make_instance()
        inst.mark_feed.last_message_at = time.time()
        self.reconciliation_module.flag_pending_reconciliation(
            inst.account_id, inst.symbol, "market entry", "hullbot_old", "ambiguous earlier entry")

        async def failing_position_query(*a, **k):
            raise ConnectionError("network down")
        inst.client.get_position_risk = failing_position_query

        alerts = []
        import app.instance as instance_module

        async def fake_notify_error(account_name, symbol, error, enabled):
            if "pending-reconciliation flag" in error.lower():
                alerts.append(error)

        with unittest.mock.patch.object(instance_module.tg, "notify_error", fake_notify_error):
            p = tv.params()
            await inst._do_enter(self._long_snap(), p)
            await inst._do_enter(self._long_snap(), p)
            self.assertEqual(len(alerts), 0, "must not alert before the threshold is reached")
            await inst._do_enter(self._long_snap(), p)

        self.assertEqual(len(alerts), 1, "must alert exactly once once the threshold is crossed")


if __name__ == "__main__":
    unittest.main()
