import os
import sys
import shutil
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATA_DIR = Path("/tmp/hull_bot_test_ledger")

import app.ledger as ledger_module  # noqa: E402
from app.ledger import TradeRecord  # noqa: E402


def _isolate_ledger_dir():
    """Module-level constants like LEDGER_DIR are only read from the
    DATA_DIR env var once, at first import - which happens exactly once for
    the whole test process regardless of which test file triggers it first.
    Relying on os.environ per-test-file is therefore NOT enough isolation
    when many test files share one process (unittest discover). Overriding
    the module attribute directly works because _path()/record_trade() look
    up ledger_module.LEDGER_DIR dynamically (as a module global) on every
    call, not a value captured once at import time."""
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    ledger_module.LEDGER_DIR = TEST_DATA_DIR / "ledger"
    ledger_module.LEDGER_DIR.mkdir(parents=True, exist_ok=True)


class TestLedger(unittest.TestCase):
    def setUp(self):
        _isolate_ledger_dir()

    def test_record_and_read_back(self):
        now = time.time()
        ledger_module.record_trade(TradeRecord("acc1", "BTCUSDT", "LONG", 0.01, 100, 105, 50.0, "TP",
                                                now - 100, now, confirmed=True))
        ledger_module.record_trade(TradeRecord("acc1", "BTCUSDT", "SHORT", 0.01, 105, 110, -50.0, "SL",
                                                now - 50, now, confirmed=True))

        trades = ledger_module.list_trades("acc1", "BTCUSDT")
        self.assertEqual(len(trades), 2)

        s = ledger_module.stats("acc1", "BTCUSDT")
        self.assertEqual(s["total_trades"], 2)
        self.assertEqual(s["wins"], 1)
        self.assertEqual(s["losses"], 1)
        self.assertAlmostEqual(s["win_rate"], 50.0)
        self.assertAlmostEqual(s["total_pnl"], 0.0)

        curve = ledger_module.equity_curve("acc1", "BTCUSDT")
        self.assertEqual(len(curve), 2)
        self.assertAlmostEqual(curve[0]["equity"], 50.0)
        self.assertAlmostEqual(curve[-1]["equity"], 0.0)

        csv_text = ledger_module.export_csv("acc1", "BTCUSDT")
        self.assertIn("LONG", csv_text)
        self.assertIn("SHORT", csv_text)

    def test_max_drawdown_hand_verified(self):
        """PnL sequence [100, -30, -40, 60, -10] -> cumulative curve
        [100, 70, 30, 90, 80] - worst peak-to-trough decline is
        100 -> 30 (dd=70), not the smaller 100->80 or 90->80 dips."""
        now = time.time()
        for i, pnl in enumerate([100, -30, -40, 60, -10]):
            ledger_module.record_trade(TradeRecord(
                "acc1", "BTCUSDT", "LONG", 0.01, 100, 105, pnl, "TP",
                now - (100 - i * 10), now - (90 - i * 10), confirmed=True))

        dd = ledger_module.max_drawdown("acc1", "BTCUSDT")
        self.assertAlmostEqual(dd["max_drawdown"], 70.0)
        self.assertAlmostEqual(dd["peak"], 100.0)
        self.assertAlmostEqual(dd["trough"], 30.0)

    def test_max_drawdown_with_no_trades_is_zero(self):
        dd = ledger_module.max_drawdown("acc1", "NEVERUSDT")
        self.assertEqual(dd["max_drawdown"], 0.0)

    def test_sharpe_ratio_hand_verified(self):
        """PnLs [10, 20, -10, 30, -5] - mean=9.0, sample std≈16.733,
        Sharpe = mean/std ≈ 0.5379."""
        now = time.time()
        for i, pnl in enumerate([10, 20, -10, 30, -5]):
            ledger_module.record_trade(TradeRecord(
                "acc1", "BTCUSDT", "LONG", 0.01, 100, 105, pnl, "TP",
                now - 100 + i, now - 90 + i, confirmed=True))

        sharpe = ledger_module.sharpe_ratio("acc1", "BTCUSDT")
        self.assertAlmostEqual(sharpe, 0.5378528742004771, places=6)

    def test_sharpe_ratio_none_with_fewer_than_two_trades(self):
        now = time.time()
        ledger_module.record_trade(TradeRecord("acc1", "BTCUSDT", "LONG", 0.01, 100, 105, 50.0, "TP",
                                                now - 100, now, confirmed=True))
        self.assertIsNone(ledger_module.sharpe_ratio("acc1", "BTCUSDT"))

    def test_sharpe_ratio_none_when_every_trade_pnl_is_identical(self):
        """std=0 -> undefined, not infinite - must return None, not crash
        on a division by zero."""
        now = time.time()
        for i in range(3):
            ledger_module.record_trade(TradeRecord("acc1", "BTCUSDT", "LONG", 0.01, 100, 105, 25.0, "TP",
                                                    now - 100 + i, now - 90 + i, confirmed=True))
        self.assertIsNone(ledger_module.sharpe_ratio("acc1", "BTCUSDT"))

    def test_stats_includes_max_drawdown_and_sharpe(self):
        now = time.time()
        for i, pnl in enumerate([10, 20, -10, 30, -5]):
            ledger_module.record_trade(TradeRecord(
                "acc1", "BTCUSDT", "LONG", 0.01, 100, 105, pnl, "TP",
                now - 100 + i, now - 90 + i, confirmed=True))

        s = ledger_module.stats("acc1", "BTCUSDT")
        self.assertIn("max_drawdown", s)
        self.assertIn("sharpe_ratio", s)
        self.assertIsNotNone(s["sharpe_ratio"])

    def test_confirmed_flag_defaults_true_for_backward_compatible_records(self):
        # Older ledger lines written before the `confirmed` field existed
        # must still load fine (default applies).
        import json
        path = ledger_module._path("acc2", "ETHUSDT")
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy_record = {
            "account_id": "acc2", "symbol": "ETHUSDT", "direction": "LONG",
            "qty": 1.0, "entry_price": 3000, "exit_price": 3100, "pnl": 100.0,
            "reason": "TP", "opened_at": 1.0, "closed_at": 2.0,
        }  # no "confirmed" key at all
        with open(path, "a") as f:
            f.write(json.dumps(legacy_record) + "\n")

        trades = ledger_module.list_trades("acc2", "ETHUSDT")
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0].confirmed)

    def test_empty_ledger_returns_sane_defaults(self):
        s = ledger_module.stats("no_such_account", "NOSYM")
        self.assertEqual(s["total_trades"], 0)
        self.assertIsNone(s["win_rate"])
        curve = ledger_module.equity_curve("no_such_account", "NOSYM")
        self.assertEqual(curve, [])


if __name__ == "__main__":
    unittest.main()

