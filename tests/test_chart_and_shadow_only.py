"""Chart tab data (read-only) and the shadow-only start paths (2026-10-24). Arbitrary test numbers only."""
import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_trading as T  # noqa: E402,F401  (stubs + temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from app import chart_api, hub_state, ledger, hub_api  # noqa: E402
from app import shadow_book as B  # noqa: E402


class FakeKlines:
    def __init__(self, n=5, t0=3_600_000, step=3_600_000):
        self.rows = [[t0 + k * step, "100", "102", "98", str(100 + k), "1", 0, 0, 0, 0, 0, 0] for k in range(n)]
        self.closed = False
        self.calls = []

    async def get_klines(self, symbol, interval, limit=500):
        self.calls.append((symbol, interval, limit))
        return self.rows[-limit:]

    async def close(self):
        self.closed = True


class PC:
    timeframe = "1h"
    plan = make_plan(ind_ma1={"entry": True}, shadow={"on": True, "trades": 2, "slippage_ticks": 0,
                                                     "tracker_start": 1000.0})


class TestChartData(T.Base):
    async def test_candles_markers_levels_and_the_shadow_line(self):
        st = hub_state.HubState(platform="binance", account_id="accC", symbol="BTCUSDT")
        book = B.new_book(1000.0)
        book["curve"] = [[3_600_000 * 2 + 10, 1000.0], [3_600_000 * 4 + 10, 1010.0]]
        book["trades"] = [{"direction": "LONG", "entry_time": 3_600_000 * 2, "signal_close": 101.0,
                           "exit_time": 3_600_000 * 4 + 10, "exit_price": 104.0, "exit_reason": "TP1",
                           "result_pct": 1.0}]
        st.shadow_book = book
        st.ladder = {"direction": "LONG", "entry": 100.0, "ml": 88.0, "sl": 97.0, "ts": None,
                     "ex": {"name": "sl", "kind": "limit"}}
        st.tp = {"levels": [{"index": 1, "on": True, "price": 103.0, "state": "placed"},
                            {"index": 2, "on": True, "price": 106.0, "state": "filled"},
                            {"index": 3, "on": False, "price": 0, "state": "off"}]}
        hub_state.save(st)
        rec = ledger.TradeRecord(account_id="accC", symbol="BTCUSDT", direction="LONG", qty=1.0, entry_price=100.0,
                                 exit_price=99.0, pnl=-1.0, reason="SL", opened_at=3_600_000 * 2 / 1000.0,
                                 closed_at=3_600_000 * 3 / 1000.0)
        client = FakeKlines()
        with mock.patch.object(chart_api, "_pair_and_client", return_value=(PC, client, True)), \
                mock.patch.object(chart_api.ledger, "list_trades", return_value=[rec]):
            d = await chart_api.chart_data("binance", "accC", "BTCUSDT", limit=5)
        self.assertTrue(client.closed)                                  # a short-lived client is closed again
        self.assertEqual(client.calls, [("BTCUSDT", "1h", 10)])          # at least 10 candles are asked for
        self.assertEqual(len(d["candles"]), 5)
        self.assertEqual(d["candles"][0], {"t": 3_600_000, "o": 100.0, "h": 102.0, "l": 98.0, "c": 100.0})
        kinds = [m["kind"] for m in d["real_markers"]]
        self.assertEqual(kinds, ["real_entry", "real_exit"])
        self.assertEqual([m["kind"] for m in d["shadow_markers"]], ["shadow_entry", "shadow_exit"])
        labels = [l["label"] for l in d["levels"]]
        self.assertEqual(labels, ["Stop Loss (on the exchange)", "Max Loss per Trade", "TP1"])   # filled TP2 not shown
        self.assertTrue(d["shadow_on"])
        # one point per candle (equity at its close) from where the shadow started: the candles opening at 2h and
        # 3h are at 1000; the 4h candle holds the 1010 point (4h + 10 ms), and so does the 5h candle after it
        self.assertEqual(d["shadow_line"], [[7_200_000, 1000.0], [10_800_000, 1000.0], [14_400_000, 1010.0],
                                            [18_000_000, 1010.0]])
        self.assertEqual(d["candle_ms"], 3_600_000)

    async def test_unknown_platform_is_refused(self):
        with self.assertRaises(chart_api.ChartError):
            await chart_api.chart_data("kraken", "a", "BTCUSDT")


class TestShadowOnlyStartPaths(unittest.IsolatedAsyncioTestCase):
    async def test_start_all_enabled_runs_the_shadow_of_a_stopped_pair(self):
        from app.manager import BotManager
        m = BotManager()

        class P:
            enabled = False
            plan = {"shadow": {"on": True}}

        class P2:
            enabled = False
            plan = None

        class Acc:
            id = "a1"
            name = "Main"
            pairs = {"BTCUSDT": P, "ETHUSDT": P2}
        with mock.patch("app.manager.store") as st, mock.patch.object(m, "start_shadow_only", new=mock.AsyncMock()) as sso, \
                mock.patch.object(m, "start", new=mock.AsyncMock()) as start:
            st.list_accounts.return_value = [Acc]
            await m.start_all_enabled()
        sso.assert_awaited_once_with("a1", "BTCUSDT")                    # only the pair with a shadow
        start.assert_not_awaited()                                        # no real start for a stopped pair

    async def test_saving_a_plan_on_a_stopped_pair_refreshes_its_shadow(self):
        class S:
            def update_pair(self, *a, **k):
                class R:
                    enabled = False
                return R()

        inst = mock.Mock(_shadow_only=True)
        mgr = mock.Mock()
        mgr.get.return_value = inst
        mgr.refresh_shadow_only = mock.AsyncMock()
        mgr.start_shadow_only = mock.AsyncMock()
        mgr.restart = mock.AsyncMock()
        with mock.patch.object(hub_api, "asdict", side_effect=lambda x: {}):
            await hub_api.apply_plan(S(), mgr, "a1", "BTCUSDT", make_plan(ind_ma1={"entry": True}))
        mgr.refresh_shadow_only.assert_awaited_once_with("a1", "BTCUSDT")
        mgr.restart.assert_not_awaited()

    async def test_a_problem_refreshing_the_shadow_never_undoes_the_saved_plan(self):
        saved = {}

        class S:
            def update_pair(self, acc, sym, upd):
                saved.update(upd)

                class R:
                    enabled = False
                return R()
        mgr = mock.Mock()
        mgr.get.return_value = None
        mgr.refresh_shadow_only = mock.AsyncMock()
        mgr.start_shadow_only = mock.AsyncMock(side_effect=RuntimeError("lock"))
        with mock.patch.object(hub_api, "asdict", side_effect=lambda x: {}):
            await hub_api.apply_plan(S(), mgr, "a1", "BTCUSDT", make_plan(ind_ma1={"entry": True}))
        self.assertIn("plan", saved)


if __name__ == "__main__":
    unittest.main()
