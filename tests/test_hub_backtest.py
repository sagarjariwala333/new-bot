"""Analysis tab = the pair's own Strategy Hub plan replayed on past candles (owner 2026-10-24, "Start 3" / "Go").
Arbitrary test numbers only."""
import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app import hub_backtest as BT  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402

N = 260
E = BT.WARMUP          # the entry candle (signal on its close)


def flat_df(n=N):
    t = np.arange(n) * 3_600_000
    c = np.full(n, 100.0)
    return pd.DataFrame({"open_time": t, "open": c.copy(), "high": c + 0.01, "low": c - 0.01, "close": c.copy(),
                         "volume": np.full(n, 10.0)})


def set_candle(df, i, o, h, l, c):
    df.loc[i, ["open", "high", "low", "close"]] = [o, h, l, c]


def plan(**over):
    p = make_plan(ind_rsi=dict(entry=True, flip=True), sl={"on": True, "basis": H.BASIS_PRICE, "value": 3.0},
                  **over)
    return p


def with_signals(dir_e: dict, dir_f: dict | None = None):
    """compute_hub as usual, then the ENTRY / FLIP direction of chosen candles is set by the test."""
    real = H.compute_hub

    def fake(df, p):
        f = real(df, p)
        f["dir_e"] = 0
        f["dir_f"] = 0
        for i, d in dir_e.items():
            f.loc[i, "dir_e"] = d
        for i, d in (dir_f or {}).items():
            f.loc[i, "dir_f"] = d
        return f
    return mock.patch.object(BT.hub, "compute_hub", side_effect=fake)


class TestInsideTheCandle(unittest.TestCase):
    def test_path_is_tradingview_order(self):
        self.assertEqual(BT._path(100, 105, 95, 103), [100, 95, 105])      # up candle: low first
        self.assertEqual(BT._path(100, 105, 95, 97), [100, 105, 95])       # down candle: high first

    def test_up_candle_hits_the_stop_before_the_target(self):
        df = flat_df()
        set_candle(df, E + 1, 100.0, 105.0, 96.0, 102.0)                  # up: low 96 first -> SL 97
        p = plan(tp={"on": True, "basis": H.BASIS_PRICE, "levels": [{"on": True, "value": 4.0, "pct": 100.0},
                                                                    {"on": False}, {"on": False}]})
        with with_signals({E: 1}):
            r = BT.run(df, p, 1000.0, 0.0, 0.0)
        t = r["trades"][0]
        self.assertEqual((t["direction"], t["exit_reason"], t["exit_price"]), ("LONG", "Stop Loss", 97.0))

    def test_down_candle_hits_the_target_before_the_stop(self):
        df = flat_df()
        set_candle(df, E + 1, 100.0, 105.0, 96.0, 98.0)                   # down: high 105 first -> TP 104
        p = plan(tp={"on": True, "basis": H.BASIS_PRICE, "levels": [{"on": True, "value": 4.0, "pct": 100.0},
                                                                    {"on": False}, {"on": False}]})
        with with_signals({E: 1}):
            r = BT.run(df, p, 1000.0, 0.0, 0.0)
        t = r["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("TP1", 104.0))
        self.assertAlmostEqual(t["gross_pct"], 4.0 * 0.10 * 5)            # 4% move x 10% x 5x = 2% of equity

    def test_gap_through_the_stop_fills_at_the_open(self):
        df = flat_df()
        set_candle(df, E + 1, 90.0, 91.0, 89.0, 90.5)
        with with_signals({E: 1}):
            r = BT.run(df, plan(), 1000.0, 0.0, 0.0)
        self.assertEqual(r["trades"][0]["exit_price"], 90.0)

    def test_flip_closes_at_the_close_and_reverses(self):
        df = flat_df()
        set_candle(df, E + 3, 100.0, 101.0, 99.5, 101.0)
        with with_signals({E: 1, E + 3: -1}, {E + 3: -1}):
            r = BT.run(df, plan(), 1000.0, 0.0, 0.0)
        t = r["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("FLIP", 101.0))
        self.assertEqual(r["trades"][0]["direction"], "LONG")


class TestCostsAndEquity(unittest.TestCase):
    def test_costs_on_every_fill(self):
        df = flat_df()
        set_candle(df, E + 1, 100.0, 105.0, 96.0, 102.0)
        with with_signals({E: 1}):
            r = BT.run(df, plan(), 1000.0, 0.05, 0.02)
        t = r["trades"][0]
        # (0.05 + 0.02)% of the traded size (10% x 5x = 0.5 of equity), entry + exit = 2 fills
        self.assertAlmostEqual(t["cost_pct"], 0.07 / 100 * 0.5 * 2 * 100)
        self.assertAlmostEqual(t["net_pct"], t["gross_pct"] - t["cost_pct"])
        self.assertAlmostEqual(t["pnl"], 1000.0 * t["net_pct"] / 100)
        self.assertAlmostEqual(t["equity_after"], 1000.0 + t["pnl"])

    def test_refusals(self):
        with self.assertRaises(BT.BacktestError):
            BT.run(flat_df(BT.WARMUP + 10), plan(), 1000.0, 0.0, 0.0)          # too short
        bad = plan()
        del bad["chart_type"]
        with self.assertRaises(BT.BacktestError):
            BT.run(flat_df(), bad, 1000.0, 0.0, 0.0)                            # incomplete plan
        with self.assertRaises(BT.BacktestError):
            BT.run(flat_df(), plan(), 0.0, 0.0, 0.0)


class TestTradesOff(unittest.TestCase):
    def three_trades(self, shadow_on):
        """Win (new high), then a loss, then a win. Shadow mode, 1 trade off."""
        df = flat_df()
        set_candle(df, E + 1, 100.0, 105.0, 99.5, 104.0)      # TP1 at 104
        set_candle(df, E + 4, 104.0, 104.5, 100.0, 101.0)     # second trade (entry 104): SL 3% = 100.88
        set_candle(df, E + 5, 101.0, 101.2, 100.8, 101.0)
        set_candle(df, E + 8, 101.0, 106.0, 100.9, 105.5)     # third trade (entry 101): TP 4% = 105.04
        for i in range(E + 2, E + 4):
            set_candle(df, i, 104.0, 104.1, 103.9, 104.0)
        for i in range(E + 6, E + 8):
            set_candle(df, i, 101.0, 101.1, 100.9, 101.0)
        p = plan(tp={"on": True, "basis": H.BASIS_PRICE, "levels": [{"on": True, "value": 4.0, "pct": 100.0},
                                                                    {"on": False}, {"on": False}]},
                 allow_reentry=True,
                 shadow={"on": shadow_on, "trades": 1, "slippage_ticks": 0, "tracker_start": 1000.0})
        with with_signals({E: 1, E + 3: 1, E + 7: 1}):
            return BT.run(df, p, 1000.0, 0.0, 0.0)

    def test_with_shadow_mode_the_trade_after_a_new_high_is_not_counted(self):
        r = self.three_trades(True)
        self.assertEqual([t["counted"] for t in r["trades"]], [True, False, True])
        self.assertEqual(r["trades"][1]["pnl"], 0.0)
        self.assertAlmostEqual(r["trades"][2]["equity_before"], r["trades"][0]["equity_after"])

    def test_without_shadow_mode_every_trade_counts(self):
        r = self.three_trades(False)
        self.assertEqual([t["counted"] for t in r["trades"]], [True, True, True])
        self.assertLess(r["trades"][1]["pnl"], 0)


class TestWalkForwardAndMonteCarlo(unittest.TestCase):
    def setUp(self):
        from tests.test_strategy_hub import make_df
        self.df = make_df(1200, seed=3)
        self.p = make_plan(ind_ma1=dict(entry=True, flip=True, type="EMA", length=20, mode="Price above / below MA"),
                           sl={"on": True, "basis": H.BASIS_PRICE, "value": 2.0})

    def test_folds_cover_the_tested_candles_and_add_up(self):
        res = BT.walk_forward(self.df, self.p, 4, 1000.0, 0.04, 0.01)
        self.assertEqual(len(res["folds"]), 4)
        self.assertEqual(sum(f["candles"] for f in res["folds"]), len(self.df) - BT.WARMUP)
        self.assertGreater(res["whole"]["trades"], 0)
        self.assertLessEqual(sum(f["trades"] for f in res["folds"]), res["whole"]["trades"] + 0)
        self.assertGreaterEqual(sum(f["trades"] for f in res["folds"]), res["whole"]["trades"] - 1)

    def test_monte_carlo_same_seed_same_result(self):
        a = BT.monte_carlo(self.df, self.p, 100, 1000.0, 0.04, 0.01, seed=7)
        b = BT.monte_carlo(self.df, self.p, 100, 1000.0, 0.04, 0.01, seed=7)
        self.assertEqual(a, b)
        self.assertLessEqual(a["final_equity"]["p5"], a["final_equity"]["median"])
        self.assertLessEqual(a["final_equity"]["median"], a["final_equity"]["p95"])

    def test_no_trades_is_refused(self):
        with with_signals({}):
            with self.assertRaises(BT.BacktestError):
                BT.monte_carlo(self.df, self.p, 10, 1000.0, 0.0, 0.0)


class TestBinanceKlinePaging(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_1500_candles_are_fetched_in_pages(self):
        from app.binance_futures import BinanceFuturesClient
        c = BinanceFuturesClient(api_key="k", api_secret="s", testnet=True)
        all_rows = [[i * 60_000] + [0] * 11 for i in range(4000)]
        calls = []

        async def req(method, path, params=None, **kw):
            calls.append(dict(params))
            end = params.get("endTime")
            rows = [r for r in all_rows if end is None or r[0] <= end]
            return rows[-params["limit"]:]
        with mock.patch.object(c, "_request", side_effect=req):
            got = await c.get_klines("BTCUSDT", "1m", limit=3200)
            one = await c.get_klines("BTCUSDT", "1m", limit=500)
        self.assertEqual(len(got), 3200)
        self.assertEqual([r[0] for r in got], [r[0] for r in all_rows[-3200:]])   # oldest first, no gaps
        self.assertEqual([x["limit"] for x in calls[:3]], [1500, 1500, 200])
        self.assertEqual(len(one), 500)
        self.assertNotIn("endTime", calls[-1])                                    # 1,500 or less: one request


if __name__ == "__main__":
    unittest.main()
