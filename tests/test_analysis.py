import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from app import strategy as strat
from app import analysis as an
from tests import _test_values as tv  # noqa: E402


def make_synthetic_df(n=2000, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + np.abs(rng.normal(0, 0.3, n))
    low = close - np.abs(rng.normal(0, 0.3, n))
    open_ = close + rng.normal(0, 0.1, n)
    return pd.DataFrame({
        "open_time": np.arange(n), "open": open_, "high": high, "low": low, "close": close,
    })


class TestSimulateTrades(unittest.TestCase):
    def test_runs_and_produces_consistent_trades(self):
        df = make_synthetic_df()
        p = tv.params()
        trades = an.simulate_trades(df, p, **tv.ANALYSIS)
        # Not asserting a specific count (depends on random data), just that
        # the simulation is internally consistent when it does trade.
        for t in trades:
            self.assertIn(t.direction, ("LONG", "SHORT"))
            self.assertIn(t.reason, ("SL", "signal_flip"))  # Base V3 exits only
            self.assertGreater(t.entry_price, 0)
            self.assertGreater(t.exit_price, 0)
            # Gross PnL sign follows price movement direction (unaffected by
            # costs); net pnl can differ in sign from gross on a small win
            # that doesn't cover commission+slippage - that's now correct
            # behavior, not a bug, so check gross here and costs separately.
            if t.direction == "LONG":
                expected_sign = 1 if t.filled_exit_price >= t.filled_entry_price else -1
            else:
                expected_sign = 1 if t.filled_exit_price <= t.filled_entry_price else -1
            self.assertEqual((t.gross_pnl >= 0), (expected_sign == 1),
                             "gross pnl sign must match direction and price movement")
            self.assertGreater(t.costs, 0, "commission+slippage costs must always be positive")
            self.assertAlmostEqual(t.pnl, t.gross_pnl - t.costs, places=6,
                                   msg="net pnl must always equal gross pnl minus costs")

    def test_deterministic_given_same_seed(self):
        df1 = make_synthetic_df(seed=7)
        df2 = make_synthetic_df(seed=7)
        p = tv.params()
        t1 = an.simulate_trades(df1, p, **tv.ANALYSIS)
        t2 = an.simulate_trades(df2, p, **tv.ANALYSIS)
        self.assertEqual(len(t1), len(t2))
        for a, b in zip(t1, t2):
            self.assertEqual(a.direction, b.direction)
            self.assertAlmostEqual(a.pnl, b.pnl, places=9)


class TestWalkForward(unittest.TestCase):
    def test_produces_requested_number_of_folds(self):
        df = make_synthetic_df(n=3000)
        p = tv.params()
        result = an.walk_forward_analysis(df, p, n_folds=3, **tv.ANALYSIS)
        self.assertEqual(len(result["folds"]), 3)
        for fold in result["folds"]:
            self.assertIn("win_rate", fold)
            self.assertIn("total_pnl", fold)

    def test_raises_with_insufficient_history(self):
        df = make_synthetic_df(n=50)
        p = tv.params()
        with self.assertRaises(ValueError):
            an.walk_forward_analysis(df, p, n_folds=4, **tv.ANALYSIS)


class TestMonteCarlo(unittest.TestCase):
    def test_reproducible_with_seed(self):
        pnls = [10.0, -5.0, 20.0, -8.0, 15.0]
        r1 = an.monte_carlo_simulation(pnls, n_sims=200, initial_equity=tv.ANALYSIS['initial_equity'], seed=42)
        r2 = an.monte_carlo_simulation(pnls, n_sims=200, initial_equity=tv.ANALYSIS['initial_equity'], seed=42)
        self.assertEqual(r1, r2)

    def test_raises_on_empty_trades(self):
        with self.assertRaises(ValueError):
            an.monte_carlo_simulation([], n_sims=100, initial_equity=tv.ANALYSIS['initial_equity'])

    def test_result_shape(self):
        pnls = [10.0, -5.0, 20.0, -8.0, 15.0]
        r = an.monte_carlo_simulation(pnls, n_sims=500, initial_equity=tv.ANALYSIS['initial_equity'], seed=1)
        self.assertEqual(r["n_sims"], 500)
        self.assertEqual(r["n_trades_per_sim"], 5)
        for key in ("p5", "p25", "median", "p75", "p95"):
            self.assertIn(key, r["final_equity"])
        self.assertGreaterEqual(r["probability_of_loss"], 0.0)
        self.assertLessEqual(r["probability_of_loss"], 100.0)


class TestTransactionCosts(unittest.TestCase):
    """Regression tests for the commission/slippage simulation costs.
    Simulation-only - never used by live trading, which pulls real
    commission from the exchange instead."""

    def test_zero_costs_reduce_to_pure_price_pnl(self):
        df = make_synthetic_df(seed=11)
        p = tv.params()
        trades = an.simulate_trades(df, p, initial_equity=tv.ANALYSIS['initial_equity'], commission_pct=0.0, slippage_pct=0.0)
        for t in trades:
            self.assertAlmostEqual(t.costs, 0.0, places=9)
            self.assertAlmostEqual(t.pnl, t.gross_pnl, places=9)
            self.assertAlmostEqual(t.filled_entry_price, t.entry_price, places=9)
            self.assertAlmostEqual(t.filled_exit_price, t.exit_price, places=9)

    def test_higher_commission_strictly_increases_costs(self):
        df = make_synthetic_df(seed=11)
        p = tv.params()
        trades_low = an.simulate_trades(df, p, initial_equity=tv.ANALYSIS['initial_equity'], commission_pct=0.01, slippage_pct=0.0)
        trades_high = an.simulate_trades(df, p, initial_equity=tv.ANALYSIS['initial_equity'], commission_pct=0.10, slippage_pct=0.0)
        self.assertEqual(len(trades_low), len(trades_high), "cost model must not change signal timing")
        for lo, hi in zip(trades_low, trades_high):
            self.assertGreater(hi.costs, lo.costs)

    def test_slippage_makes_long_entry_worse_not_better(self):
        df = make_synthetic_df(seed=11)
        p = tv.params()
        trades = an.simulate_trades(df, p, initial_equity=tv.ANALYSIS['initial_equity'], commission_pct=0.0, slippage_pct=0.5)
        for t in trades:
            if t.direction == "LONG":
                self.assertGreaterEqual(t.filled_entry_price, t.entry_price,
                                        "a LONG buy fill must never be better (lower) than the signal price")
            else:
                self.assertLessEqual(t.filled_entry_price, t.entry_price,
                                     "a SHORT sell fill must never be better (higher) than the signal price")


if __name__ == "__main__":
    unittest.main()
