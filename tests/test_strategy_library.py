"""Strategy library (2026-10-24): saved strategies by name, automatic history of old versions, remove plan from a
pair. Arbitrary test numbers only."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_trading as T  # noqa: E402,F401  (stubs + temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_library as L, hub_api, hub_state, position_state  # noqa: E402


def good_plan(**kw):
    return make_plan(ind_ma1={"entry": True}, **kw)


class LibFile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(L, "LIBRARY_FILE", self.dir / "strategy_library.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


class TestLibrary(LibFile):
    def test_empty_library(self):
        self.assertEqual(L.list_all(), {"saved": [], "history": []})

    def test_save_keeps_name_date_where_from_and_plan(self):
        with mock.patch.object(L.time, "time", return_value=1_761_264_000.0):   # 2025-10-24 00:00 UTC
            e = L.save("  BTC 1H trend ", good_plan(), "binance", "a1", "Main", "BTCUSDT", "1h")
        self.assertEqual(e["name"], "BTC 1H trend")
        self.assertEqual(e["saved_at_sgt"], "2025-10-24 08:00 SGT")
        self.assertEqual((e["platform"], e["account_id"], e["account_name"], e["symbol"], e["timeframe"]),
                         ("binance", "a1", "Main", "BTCUSDT", "1h"))
        self.assertEqual(e["plan"]["name"], "BTC 1H trend")
        self.assertIn("ENTRY", e["summary"])
        rows = L.list_all()["saved"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], e["id"])
        on_disk = json.loads(L.LIBRARY_FILE.read_text())
        self.assertEqual(len(on_disk["saved"]), 1)

    def test_same_name_twice_makes_two_copies_nothing_overwritten(self):
        L.save("X", good_plan(), "binance", "a1", "Main", "BTCUSDT", "1h")
        L.save("X", good_plan(entry_wait=3), "okx", "o1", "OKX main", "ETH-USDT-SWAP", "4h")
        self.assertEqual(len(L.list_all()["saved"]), 2)

    def test_newest_first(self):
        with mock.patch.object(L.time, "time", return_value=1000.0):
            L.save("old", good_plan(), "binance", "a1", "", "BTCUSDT", "1h")
        with mock.patch.object(L.time, "time", return_value=2000.0):
            L.save("new", good_plan(), "binance", "a1", "", "BTCUSDT", "1h")
        self.assertEqual([e["name"] for e in L.list_all()["saved"]], ["new", "old"])

    def test_incomplete_or_unnamed_is_refused_and_nothing_written(self):
        bad = good_plan()
        del bad["chart_type"]
        for args in (("", good_plan()), ("x" * 61, good_plan()), ("ok", bad)):
            with self.assertRaises(L.LibraryError) as c:
                L.save(args[0], args[1], "binance", "a1", "", "BTCUSDT", "1h")
            self.assertEqual(c.exception.status, 400)
        with self.assertRaises(L.LibraryError):
            L.save("ok", good_plan(), "kraken", "a1", "", "BTCUSDT", "1h")
        self.assertFalse(L.LIBRARY_FILE.exists())

    def test_the_callers_plan_is_not_changed(self):
        p = good_plan()
        L.save("named", p, "binance", "a1", "", "BTCUSDT", "1h")
        self.assertNotIn("name", p)

    def test_delete_only_that_entry(self):
        a = L.save("A", good_plan(), "binance", "a1", "", "BTCUSDT", "1h")
        b = L.save("B", good_plan(), "binance", "a1", "", "BTCUSDT", "1h")
        h = L.add_history(good_plan(), "binance", "a1", "", "BTCUSDT", "1h", "changed")
        self.assertEqual(L.delete(a["id"]), {"deleted": True, "kind": "saved", "name": "A"})
        self.assertEqual([e["id"] for e in L.list_all()["saved"]], [b["id"]])
        self.assertEqual(L.delete(h["id"])["kind"], "history")
        with self.assertRaises(L.LibraryError) as c:
            L.delete(a["id"])
        self.assertEqual(c.exception.status, 404)

    def test_unreadable_file_is_never_overwritten(self):
        L.LIBRARY_FILE.write_text("{not json")
        with self.assertRaises(L.LibraryError):
            L.save("A", good_plan(), "binance", "a1", "", "BTCUSDT", "1h")
        with self.assertRaises(L.LibraryError):
            L.list_all()
        self.assertIsNone(L.add_history(good_plan(), "binance", "a1", "", "BTCUSDT", "1h", "changed"))
        self.assertEqual(L.LIBRARY_FILE.read_text(), "{not json")

    def test_history_without_a_name(self):
        e = L.add_history(good_plan(), "binance", "a1", "", "BTCUSDT", "1h", "removed")
        self.assertEqual((e["name"], e["note"], e["kind"]), ("(no name)", "removed", "history"))
        self.assertIsNone(L.add_history(None, "binance", "a1", "", "BTCUSDT", "1h", "removed"))

    def test_old_plan_is_shown_in_todays_format(self):
        old = good_plan()
        old.pop("allow_reentry", None)
        L.add_history(old, "binance", "a1", "", "BTCUSDT", "1h", "changed")
        shown = L.list_all()["history"][0]["plan"]
        self.assertIn("allow_reentry", shown)
        self.assertNotIn("allow_reentry", json.loads(L.LIBRARY_FILE.read_text())["history"][0]["plan"])


class FakePC:
    def __init__(self, plan, enabled=False, timeframe="1h"):
        self.plan = plan
        self.enabled = enabled
        self.timeframe = timeframe
        self.frozen = False


class FakeStore:
    def __init__(self, plan, enabled=False):
        class Acc:
            name = "Main"
        self.acc = Acc()
        self.acc.pairs = {"BTCUSDT": FakePC(plan, enabled)}

    def get_account(self, account_id):
        return self.acc if account_id == "a1" else None

    def update_pair(self, account_id, symbol, upd):
        pc = self.acc.pairs[symbol]
        for k, v in upd.items():
            setattr(pc, k, v)
        return pc


def fake_mgr(inst=None):
    m = mock.Mock()
    m.get.return_value = inst
    m.refresh_shadow_only = mock.AsyncMock()
    m.start_shadow_only = mock.AsyncMock()
    m.restart = mock.AsyncMock()
    return m


class TestHistoryOnChange(LibFile, unittest.IsolatedAsyncioTestCase):
    async def test_changing_a_plan_keeps_the_old_one(self):
        old = good_plan(name="Old one")
        st = FakeStore(old)
        with mock.patch.object(hub_api, "asdict", side_effect=lambda x: {}):
            await hub_api.apply_plan(st, fake_mgr(), "a1", "BTCUSDT", good_plan(entry_wait=2), platform="okx")
        h = L.list_all()["history"]
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]["name"], h[0]["note"], h[0]["platform"], h[0]["account_name"], h[0]["timeframe"]),
                         ("Old one", "changed", "okx", "Main", "1h"))
        self.assertEqual(st.acc.pairs["BTCUSDT"].plan["entry_wait"], 2)

    async def test_saving_the_same_plan_again_adds_nothing(self):
        p = good_plan()
        st = FakeStore(p)
        with mock.patch.object(hub_api, "asdict", side_effect=lambda x: {}):
            await hub_api.apply_plan(st, fake_mgr(), "a1", "BTCUSDT", good_plan())
        self.assertEqual(L.list_all()["history"], [])

    async def test_first_plan_on_a_pair_adds_nothing(self):
        st = FakeStore(None)
        with mock.patch.object(hub_api, "asdict", side_effect=lambda x: {}):
            await hub_api.apply_plan(st, fake_mgr(), "a1", "BTCUSDT", good_plan())
        self.assertEqual(L.list_all()["history"], [])

    async def test_a_refused_plan_adds_nothing(self):
        st = FakeStore(good_plan())
        bad = good_plan()
        del bad["chart_type"]
        with self.assertRaises(hub_api.PlanRefused):
            await hub_api.apply_plan(st, fake_mgr(), "a1", "BTCUSDT", bad)
        self.assertEqual(L.list_all()["history"], [])

    async def test_a_broken_library_never_stops_the_plan_save(self):
        L.LIBRARY_FILE.write_text("{broken")
        st = FakeStore(good_plan())
        with mock.patch.object(hub_api, "asdict", side_effect=lambda x: {}):
            await hub_api.apply_plan(st, fake_mgr(), "a1", "BTCUSDT", good_plan(entry_wait=4))
        self.assertEqual(st.acc.pairs["BTCUSDT"].plan["entry_wait"], 4)


class TestRemovePlan(LibFile, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        hub_state.HUB_STATE_DIR.mkdir(parents=True, exist_ok=True)       # another test may have removed the temp dir
        position_state.POSITION_STATE_DIR.mkdir(parents=True, exist_ok=True)
        hub_state.save(hub_state.HubState(platform="binance", account_id="a1", symbol="BTCUSDT"))
        position_state.clear_position_state("a1", "BTCUSDT")

    async def test_remove_keeps_it_in_history_and_clears_the_pair(self):
        st = FakeStore(good_plan(name="Gone"))
        r = await hub_api.remove_plan(st, fake_mgr(), "a1", "BTCUSDT", "binance")
        self.assertEqual(r, {"removed": True, "symbol": "BTCUSDT"})
        self.assertIsNone(st.acc.pairs["BTCUSDT"].plan)
        h = L.list_all()["history"]
        self.assertEqual((h[0]["name"], h[0]["note"]), ("Gone", "removed"))

    async def test_refused_while_trading(self):
        st = FakeStore(good_plan(), enabled=True)
        with self.assertRaises(hub_api.PlanRefused) as c:
            await hub_api.remove_plan(st, fake_mgr(), "a1", "BTCUSDT", "binance")
        self.assertIn("Stop the pair first", str(c.exception))
        self.assertIsNotNone(st.acc.pairs["BTCUSDT"].plan)
        self.assertEqual(L.list_all()["history"], [])

    async def test_refused_while_a_real_task_runs_even_if_not_enabled(self):
        task = mock.Mock()
        task.done.return_value = False
        inst = mock.Mock(_task=task, _shadow_only=False)
        st = FakeStore(good_plan())
        with self.assertRaises(hub_api.PlanRefused):
            await hub_api.remove_plan(st, fake_mgr(inst), "a1", "BTCUSDT", "binance")

    async def test_refused_while_a_trade_may_be_open(self):
        st = FakeStore(good_plan())
        hs = hub_state.HubState(platform="binance", account_id="a1", symbol="BTCUSDT")
        hs.ladder = {"direction": "LONG", "entry": 100.0, "ml": 90.0}
        hub_state.save(hs)
        with self.assertRaises(hub_api.PlanRefused) as c:
            await hub_api.remove_plan(st, fake_mgr(), "a1", "BTCUSDT", "binance")
        self.assertIn("trade may still be open", str(c.exception))
        self.assertIsNotNone(st.acc.pairs["BTCUSDT"].plan)

    async def test_refused_when_the_bot_has_an_open_trade_record(self):
        st = FakeStore(good_plan())
        position_state.save_position_state("a1", "BTCUSDT", position_state.PositionSnapshot(
            direction="LONG", entry_price=100.0, qty=1.0, equity_at_entry=1000.0, opened_at=1.0))
        try:
            with self.assertRaises(hub_api.PlanRefused):
                await hub_api.remove_plan(st, fake_mgr(), "a1", "BTCUSDT", "binance")
        finally:
            position_state.clear_position_state("a1", "BTCUSDT")

    async def test_refused_when_there_is_no_plan(self):
        with self.assertRaises(hub_api.PlanRefused):
            await hub_api.remove_plan(FakeStore(None), fake_mgr(), "a1", "BTCUSDT", "binance")

    async def test_a_running_shadow_ends(self):
        task = mock.Mock()
        task.done.return_value = False
        inst = mock.Mock(_task=task, _shadow_only=True)
        inst.state.status = "STOPPED"
        mgr = fake_mgr(inst)
        st = FakeStore(good_plan())
        await hub_api.remove_plan(st, mgr, "a1", "BTCUSDT", "binance")
        mgr.refresh_shadow_only.assert_awaited_once_with("a1", "BTCUSDT")
        self.assertIsNone(st.acc.pairs["BTCUSDT"].plan)


class TestRoutesExist(unittest.TestCase):
    def test_routes(self):
        from app import api
        from app.api import okx
        ends = {r.endpoint.__name__: r.path for r in api.router.routes}
        self.assertTrue(ends["get_library"].endswith("/library"))
        self.assertTrue(ends["save_to_library"].endswith("/library"))
        self.assertTrue(ends["delete_from_library"].endswith("/library/{entry_id}"))
        self.assertTrue(ends["remove_pair_plan"].endswith("/accounts/{account_id}/pairs/{symbol}/plan"))
        okx_ends = {r.endpoint.__name__: r.path for r in okx.router.routes}
        self.assertTrue(okx_ends["remove_okx_pair_plan"].endswith("/accounts/{account_id}/pairs/{symbol}/plan"))

if __name__ == "__main__":
    unittest.main()
