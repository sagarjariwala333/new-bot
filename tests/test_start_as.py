"""Save box "Start this plan as": Follow through (nothing restarts) or New (only the ticked memories restart).
Arbitrary test numbers only."""
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import tests._pydantic_stub  # noqa: F401,E402
import tests._fastapi_stub  # noqa: F401,E402
from app import hub_api, hub_state  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app.hub_trading import HubMixin  # noqa: E402
from tests.test_strategy_library import FakeStore, fake_mgr, good_plan  # noqa: E402

ALL = {"first_flip": True, "size": True, "loss_streak": True, "reentry": True}


def new(**ticked):
    d = {"mode": "new", "first_flip": False, "size": False, "loss_streak": False, "reentry": False}
    d.update(ticked)
    return d


def dirty_state():
    st = hub_state.HubState(platform="binance", account_id="a1", symbol="BTCUSDT")
    st.first_trade_done = True
    st.last_total = 80.0
    st.loss_streak = 2
    st.cooldown_through = 123
    st.reentry_block = 1
    st.reentry_forced = True
    return st


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(hub_state, "HUB_STATE_DIR", self.dir)
        self.p.start()
        self.lib = mock.patch("app.strategy_library.LIBRARY_FILE", self.dir / "lib.json")
        self.lib.start()

    def tearDown(self):
        self.lib.stop()
        self.p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


class TestPure(unittest.TestCase):
    def test_only_ticked_boxes_restart(self):
        for box in hub_state.START_AS_BOXES:
            st = dirty_state()
            done = hub_state.apply_start_as(st, new(**{box: True}))
            self.assertEqual(len(done), 1, box)
            before = dirty_state()
            changed = {k for k in ("first_trade_done", "last_total", "loss_streak", "cooldown_through",
                                   "reentry_block", "reentry_forced") if getattr(st, k) != getattr(before, k)}
            expect = {"first_flip": {"first_trade_done"}, "size": {"last_total"},
                      "loss_streak": {"loss_streak", "cooldown_through"}, "reentry": {"reentry_block", "reentry_forced"}}[box]
            self.assertEqual(changed, expect, box)

    def test_all_ticked(self):
        st = dirty_state()
        self.assertEqual(len(hub_state.apply_start_as(st, new(**ALL))), 4)
        self.assertFalse(st.first_trade_done)
        self.assertIsNone(st.last_total)
        self.assertEqual((st.loss_streak, st.cooldown_through, st.reentry_block, st.reentry_forced), (0, None, 0, False))


class TestCheck(unittest.TestCase):
    def test_none_means_no_choice(self):
        self.assertIsNone(hub_api.check_start_as(None))

    def test_follow(self):
        self.assertEqual(hub_api.check_start_as({"mode": "follow", "size": True}), {"mode": "follow"})

    def test_refused(self):
        for bad in ("x", {}, {"mode": "other"}, {"mode": "new"}, new(), {"mode": "new", "size": "yes"}):
            with self.assertRaises(hub_api.PlanRefused, msg=str(bad)):
                hub_api.check_start_as(bad)

    def test_new_keeps_only_known_boxes(self):
        r = hub_api.check_start_as({"mode": "new", "size": True, "junk": True})
        self.assertEqual(r, new(size=True))


class TestSave(Base, unittest.IsolatedAsyncioTestCase):
    async def save(self, start_as, inst=None, plan=None):
        st = FakeStore(plan or good_plan())
        with mock.patch.object(hub_api, "asdict", side_effect=lambda x: {}):
            return await hub_api.apply_plan(st, fake_mgr(inst), "a1", "BTCUSDT", plan or good_plan(), start_as=start_as)

    def put(self, st):
        hub_state.save(st)

    def get(self):
        return hub_state.load("binance", "a1", "BTCUSDT")

    async def test_no_choice_changes_nothing(self):
        self.put(dirty_state())
        out = await self.save(None)
        self.assertNotIn("start_as_result", out)
        self.assertEqual(self.get().last_total, 80.0)

    async def test_follow_changes_nothing(self):
        self.put(dirty_state())
        out = await self.save({"mode": "follow"})
        self.assertEqual(out["start_as_result"]["status"], "follow")
        g = self.get()
        self.assertEqual((g.last_total, g.loss_streak, g.first_trade_done, g.reentry_block), (80.0, 2, True, 1))

    async def test_new_flat_pair_restarts_ticked_only(self):
        self.put(dirty_state())
        out = await self.save(new(size=True))
        self.assertEqual(out["start_as_result"]["status"], "done")
        g = self.get()
        self.assertIsNone(g.last_total)
        self.assertEqual((g.loss_streak, g.first_trade_done, g.reentry_block), (2, True, 1))

    async def test_refused_choice_saves_nothing(self):
        st = FakeStore(good_plan(name="Old"))
        with self.assertRaises(hub_api.PlanRefused):
            await hub_api.apply_plan(st, fake_mgr(), "a1", "BTCUSDT", good_plan(name="New"), start_as=new())
        self.assertEqual(st.acc.pairs["BTCUSDT"].plan["name"], "Old")

    async def test_trade_open_in_memory_waits_and_changes_nothing_now(self):
        s = dirty_state()
        s.res_open = True
        self.put(s)
        out = await self.save(new(**ALL))
        self.assertEqual(out["start_as_result"]["status"], "after_close")
        g = self.get()
        self.assertEqual((g.last_total, g.loss_streak, g.first_trade_done), (80.0, 2, True))
        self.assertEqual(g.fresh_start, ALL)

    async def test_running_instance_in_position_waits(self):
        inst = types.SimpleNamespace(hub=dirty_state(), state=types.SimpleNamespace(status="IN_POSITION"),
                                     _hub_busy=False, _log=lambda *_: None, _task=None, _pending_restart_on_flat=False)
        await self.save(new(size=True), inst=inst)
        self.assertEqual(inst.hub.last_total, 80.0)
        self.assertEqual(inst.hub.fresh_start, {"first_flip": False, "size": True, "loss_streak": False, "reentry": False})

    async def test_follow_cancels_a_waiting_new(self):
        s = dirty_state()
        s.fresh_start = dict(ALL)
        self.put(s)
        await self.save({"mode": "follow"})
        self.assertIsNone(self.get().fresh_start)

    async def test_notes_when_a_tick_has_no_effect(self):
        self.put(dirty_state())
        plan = good_plan()
        plan["wait_first_reversal"] = False
        out = await self.save(new(first_flip=True, loss_streak=True), plan=plan)
        notes = " ".join(out["start_as_result"]["notes"])
        self.assertIn("Fresh Reversal", notes)
        self.assertIn("Cooldown", notes)
        self.assertTrue(self.get().first_trade_done is False)   # still restarted (harmless), told it has no effect


class TestWaitingApplied(Base):
    def test_applied_after_close_and_cleared(self):
        logs = []
        ns = types.SimpleNamespace(hub=dirty_state(), _is_hub=lambda: True, _log=logs.append)
        ns.hub.fresh_start = {"first_flip": False, "size": True, "loss_streak": False, "reentry": True}
        HubMixin._hub_apply_waiting_start_as(ns)
        self.assertIsNone(ns.hub.fresh_start)
        self.assertIsNone(ns.hub.last_total)
        self.assertEqual(ns.hub.reentry_block, 0)
        self.assertEqual((ns.hub.loss_streak, ns.hub.first_trade_done), (2, True))
        self.assertTrue(logs)

    def test_nothing_waiting_nothing_happens(self):
        ns = types.SimpleNamespace(hub=dirty_state(), _is_hub=lambda: True, _log=lambda *_: None)
        HubMixin._hub_apply_waiting_start_as(ns)
        self.assertEqual(ns.hub.last_total, 80.0)


if __name__ == "__main__":
    unittest.main()
