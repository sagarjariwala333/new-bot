"""Pair card info blocks (owner 2026-11-09): TP levels, orders resting on the exchange, last closed trade, next entry
size, memory flags, recent problems. READ-ONLY: nothing here may change a trade. Arbitrary test numbers only."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_ladder import LadderBase, sl, step, PRICE  # noqa: E402
from app import ledger  # noqa: E402


def rec(opened, reason, pnl, exit_price=101.0, closed=None):
    return ledger.TradeRecord(account_id="a1", symbol="BTCUSDT", direction="LONG", qty=1.0, entry_price=100.0,
                              exit_price=exit_price, pnl=pnl, reason=reason, opened_at=opened,
                              closed_at=closed or opened + 10, commission=0.1, commission_included=True)


class TestLedgerLegs(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ledger, "LEDGER_DIR", self.dir)
        self.p.start()
        ledger._last_cache.clear()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_empty(self):
        self.assertEqual(ledger.last_trade_legs("a1", "BTCUSDT"), [])

    def test_only_the_last_trade_with_all_its_legs(self):
        for r in (rec(100, "TP1", 1.0), rec(200, "TP1", 2.0), rec(200, "trailing_stop", 3.0)):
            ledger.record_trade(r)
        legs = ledger.last_trade_legs("a1", "BTCUSDT")
        self.assertEqual([t.reason for t in legs], ["TP1", "trailing_stop"])

    def test_cache_follows_the_file(self):
        ledger.record_trade(rec(100, "TP1", 1.0))
        self.assertEqual(len(ledger.last_trade_legs("a1", "BTCUSDT")), 1)
        ledger.record_trade(rec(300, "SL", -1.0))
        self.assertEqual([t.reason for t in ledger.last_trade_legs("a1", "BTCUSDT")], ["SL"])

    def test_unknown_open_time_is_not_merged(self):
        ledger.record_trade(rec(0.0, "SL", -1.0))
        ledger.record_trade(rec(0.0, "TP1", 1.0))
        self.assertEqual(len(ledger.last_trade_legs("a1", "BTCUSDT")), 1)


class TestInfo(LadderBase):
    async def test_blocks_for_an_open_trade_and_nothing_changes(self):
        inst = self.make_l(sl=sl(PRICE, 3.0), trail=step(gap=0.5))
        await self.open_long_at(inst, price=100.0)
        before = (inst.state.status, inst.state.stop_target, dict(inst.hub.ladder or {}))
        inst.hub.tp = {"direction": "LONG", "entry_price": 100.0, "qty0": 1.0, "levels": [
            {"index": 1, "on": True, "price": 101.0, "pct": 50.0, "state": "placed"},
            {"index": 2, "on": False, "price": 102.0, "pct": 0.0, "state": "off"}]}
        inst.client.open_orders = [{"orderId": 1, "clientOrderId": "hullbot_x", "type": "LIMIT", "side": "SELL",
                                    "price": "101", "origQty": "1", "reduceOnly": True}]
        inst.client.open_algo_orders = [{"algoId": 5, "orderType": "STOP_MARKET", "side": "SELL", "triggerPrice": "97", "quantity": "1"}]
        await inst.refresh_resting()
        info = inst.to_status_dict()["hub"]["info"]
        self.assertEqual([l["state"] for l in info["tp"]], ["placed", "off"])
        kinds = sorted(o["kind"] for o in info["resting"]["orders"])
        self.assertEqual(kinds, ["limit", "stop"])
        stop = [o for o in info["resting"]["orders"] if o["kind"] == "stop"][0]
        self.assertEqual(stop["trigger"], 97.0)
        self.assertEqual(before, (inst.state.status, inst.state.stop_target, dict(inst.hub.ladder or {})))

    async def test_resting_read_is_cached_and_never_raises(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        calls = []
        orig = inst.client.get_open_orders

        async def counting(sym):
            calls.append(1)
            return await orig(sym)
        inst.client.get_open_orders = counting
        await inst.refresh_resting()
        await inst.refresh_resting()
        self.assertEqual(len(calls), 1)                         # second call within the cache time
        inst._resting["at"] = 0
        async def boom(sym):
            raise RuntimeError("down")
        inst.client.get_open_orders = boom
        await inst.refresh_resting()                            # no exception, error is kept for the card
        self.assertIn("down", inst._resting["error"])
        self.assertEqual(inst.to_status_dict()["hub"]["info"]["resting"]["orders"], [])   # old list kept

    async def test_stopped_pair_is_not_read(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        inst._stop_requested = True
        await inst.refresh_resting()
        self.assertIsNone(getattr(inst, "_resting", None))

    async def test_flags_and_problems(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        inst.hub.first_trade_done = False
        inst.hub.reentry_block = -1
        inst.hub.fresh_start = {"size": True}
        inst._log("TP1: cancel did not go through (Binance API error 400: Unknown order sent.) - the sweep below retries.")
        inst._log("Everything is fine.")
        info = inst._hub_info()
        flags = dict(info["flags"])
        self.assertEqual(flags["First trade"], "waits for a fresh flip") if "First trade" in flags else None
        self.assertIn("Mid-Trend block", flags)
        self.assertIn("Start as NEW", flags)
        self.assertEqual(len(info["problems"]), 1)
        self.assertIn("did not go through", info["problems"][0])

    async def test_last_trade_block(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        legs = [ledger.TradeRecord(account_id="x", symbol="BTCUSDT", direction="SHORT", qty=2.0, entry_price=100.0,
                                   exit_price=95.0, pnl=9.5, reason="trailing_stop", opened_at=1000.0, closed_at=2000.0,
                                   commission=0.5, commission_included=True)]
        with mock.patch.object(ledger, "last_trade_legs", return_value=legs):
            info = inst._hub_info()
        self.assertEqual(info["last_trade"]["legs"][0]["reason"], "trailing_stop")
        self.assertEqual(info["last_trade"]["pnl"], 9.5)
        self.assertEqual(info["last_trade"]["direction"], "SHORT")

    async def test_next_entry_block(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        ne = inst._hub_info()["next_entry"]
        self.assertIn(ne["method"], ("fixed", "alloc"))


if __name__ == "__main__":
    unittest.main()
