import os
import sys
import shutil
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATA_DIR = Path("/tmp/hull_bot_test_okx_analysis")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass123")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402

import app.okx_store as okx_store_module  # noqa: E402
from app.okx_store import OKXPairConfig  # noqa: E402
from app.api.schemas import WalkForwardRequest, MonteCarloRequest  # noqa: E402
import app.api.okx as okx_api  # noqa: E402
from tests import _test_values as tv  # noqa: E402


def _fake_binance_shaped_klines(n=2000, seed=3):
    """Reuses the EXACT synthetic data generator from test_analysis.py
    (make_synthetic_df) - not a fresh invention - since that one is already
    proven to reliably produce qualifying trades for the real strategy
    parameters. OKX's get_klines() already normalizes to this same
    Binance-compatible shape (see its own docstring on that fix), so a
    test exercising _fetch_okx_history only needs to fake THAT normalized
    shape, not OKX's raw 9-column response."""
    import numpy as np
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + np.abs(rng.normal(0, 0.3, n))
    low = close - np.abs(rng.normal(0, 0.3, n))
    open_ = close + rng.normal(0, 0.1, n)
    rows = []
    for i in range(n):
        rows.append([1700000000000 + i * 3600_000, float(open_[i]), float(high[i]), float(low[i]),
                     float(close[i]), "10", 0, "0", 0, "0", "0", "0"])
    return rows


class TestOKXAnalysisEndpoints(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        okx_store_module.OKX_ACCOUNTS_FILE = TEST_DATA_DIR / "okx_accounts.json"
        self.store = okx_store_module.OKXStore()
        okx_api.okx_store = self.store

        self.acc = self.store.create_account("Sub1", "key", "secret", "phrase", demo=True)
        self.store.add_pair(self.acc.id, tv.okx_pair_config(symbol="BTC-USDT-SWAP", timeframe="4h"))

    async def test_walk_forward_returns_a_result_for_a_real_okx_pair(self):
        with mock.patch("app.okx_futures.OKXFuturesClient.get_klines",
                         new=mock.AsyncMock(return_value=_fake_binance_shaped_klines())), \
             mock.patch("app.okx_futures.OKXFuturesClient.close", new=mock.AsyncMock()):
            result = await okx_api.okx_walk_forward(
                self.acc.id, "BTC-USDT-SWAP",
                WalkForwardRequest(limit=300, n_folds=2, initial_equity=tv.ANALYSIS['initial_equity'], commission_pct=0.0, slippage_pct=0.0))
        self.assertIsInstance(result, dict)

    async def test_monte_carlo_returns_a_result_for_a_real_okx_pair(self):
        with mock.patch("app.okx_futures.OKXFuturesClient.get_klines",
                         new=mock.AsyncMock(return_value=_fake_binance_shaped_klines())), \
             mock.patch("app.okx_futures.OKXFuturesClient.close", new=mock.AsyncMock()):
            result = await okx_api.okx_monte_carlo(
                self.acc.id, "BTC-USDT-SWAP",
                MonteCarloRequest(limit=300, n_sims=50, initial_equity=tv.ANALYSIS['initial_equity'], commission_pct=0.0, slippage_pct=0.0, seed=42))
        self.assertIsInstance(result, dict)

    async def test_unknown_pair_returns_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await okx_api.okx_walk_forward(
                self.acc.id, "NEVERUSDT-SWAP",
                WalkForwardRequest(limit=300, n_folds=2, initial_equity=tv.ANALYSIS['initial_equity'], commission_pct=0.0, slippage_pct=0.0))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_ledger_csv_export_returns_csv_content_type(self):
        response = okx_api.get_okx_ledger_csv(self.acc.id, "BTC-USDT-SWAP")
        self.assertEqual(response.media_type, "text/csv")
        self.assertIn(self.acc.id, response.headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()
