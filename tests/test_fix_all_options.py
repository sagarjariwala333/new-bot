"""2026-11-11 options: counter-trend total, flip "close only, wait for entry rules", ignore late signals, file-save alert."""
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

import tests._aiohttp_stub  # noqa
from app import strategy_hub as hub
from app import fsutil, api_guard
from tests._hub_plan import make_plan
from tests.test_hub_trading import Base, snap_of, plan_with


def fixed_plan(ct_total=None, ct_on=True):
    ct = {"on": ct_on, "by": "ma1", "base_amount": 40.0, "alloc_pct": 5.0, "leverage": 15}
    if ct_total:
        ct["total"] = ct_total
    return make_plan(ind_ma1={"entry": True, "flip": True},
                     sizing={"method": hub.SIZE_FIXED, "base_amount": 80.0, "leverage": 15, "counter": ct})


class CounterTotal(unittest.TestCase):
    def test_shared_is_the_old_behaviour(self):
        p = hub.complete_plan(fixed_plan())
        self.assertFalse(hub.ct_total_separate(p))
        self.assertEqual(hub.fixed_entry_amount("counter", p, 100.0, None), 100.0)
        self.assertEqual(hub.fixed_entry_amount("counter", p, None, None), 40.0)

    def test_separate_totals(self):
        p = hub.complete_plan(fixed_plan("separate"))
        self.assertTrue(hub.ct_total_separate(p))
        self.assertEqual(hub.fixed_entry_amount("counter", p, 100.0, None), 40.0)     # trend total ignored
        self.assertEqual(hub.fixed_entry_amount("counter", p, 100.0, 55.0), 55.0)
        self.assertEqual(hub.fixed_entry_amount("counter", p, 100.0, 30.0), 40.0)     # never below the base
        self.assertEqual(hub.fixed_entry_amount("single", p, 100.0, 55.0), 100.0)     # trend uses its own
        self.assertEqual(hub.fixed_entry_amount("trend", p, None, 55.0), 80.0)

    def test_separate_only_when_counter_is_on(self):
        p = hub.complete_plan(fixed_plan("separate", ct_on=False))
        self.assertFalse(hub.ct_total_separate(p))

    def test_shared_key_is_dropped_so_old_plans_do_not_look_changed(self):
        a = hub.complete_plan(fixed_plan())
        b = hub.complete_plan(fixed_plan("shared"))
        self.assertEqual(a, b)
        self.assertNotIn("total", b["sizing"]["counter"])
        self.assertEqual(hub.amend_diff(a, b), {"now": [], "waits": False})

    def test_validation(self):
        hub.validate_plan(fixed_plan("separate"))
        hub.validate_plan(fixed_plan("shared"))
        with self.assertRaises(hub.PlanError):
            hub.validate_plan(fixed_plan("nonsense"))

    def test_review_text_mentions_it(self):
        txt = " ".join(" ".join(r) for r in hub.plan_lines(hub.complete_plan(fixed_plan("separate"))))
        self.assertIn("own running total", txt)
        txt = " ".join(" ".join(r) for r in hub.plan_lines(hub.complete_plan(fixed_plan())))
        self.assertIn("shared", txt)
        late = make_plan(ind_ma1={"entry": True, "flip": True}, late_skip={"on": True, "minutes": 30})
        self.assertIn("Ignore late signals", " ".join(" ".join(r) for r in hub.plan_lines(hub.complete_plan(late))))


class LateSkipPlan(unittest.TestCase):
    def test_off_by_default_and_normalised(self):
        p = hub.complete_plan(make_plan(ind_ma1={"entry": True, "flip": True}))
        self.assertIsNone(hub.late_skip_minutes(p))
        q = hub.complete_plan(make_plan(ind_ma1={"entry": True, "flip": True}, late_skip={"on": False}))
        self.assertEqual(p, q)

    def test_validation(self):
        base = make_plan(ind_ma1={"entry": True, "flip": True})
        hub.validate_plan(dict(base, late_skip={"on": True, "minutes": 30}))
        for bad in ({"on": True}, {"on": True, "minutes": 0}, {"on": True, "minutes": 5000}, {"on": "x"}):
            with self.assertRaises(hub.PlanError):
                hub.validate_plan(dict(base, late_skip=bad))

    def test_signal_is_late(self):
        p = hub.complete_plan(make_plan(ind_ma1={"entry": True, "flip": True}, late_skip={"on": True, "minutes": 30}))
        tf = 4 * 3600 * 1000
        closed_at = 10 * tf + tf
        self.assertEqual(hub.signal_is_late(p, 10 * tf, tf, closed_at + 10 * 60000)[0], False)
        self.assertEqual(hub.signal_is_late(p, 10 * tf, tf, closed_at + 31 * 60000)[0], True)
        off = hub.complete_plan(make_plan(ind_ma1={"entry": True, "flip": True}))
        self.assertEqual(hub.signal_is_late(off, 10 * tf, tf, closed_at + 10 ** 9)[0], False)


class FlipCloseOnly(Base):
    def plan(self, action):
        return make_plan(ind_ma1={"entry": True, "flip": True}, flip_action=action)

    async def test_close_only_closes_and_stays_flat_then_enters_on_entry_rules(self):
        inst = self.make(self.plan(hub.FLIP_CLOSE_WAIT))
        self.go_long(inst)
        # flip signal, but the ENTRY rules (e.g. ADX / DI) do not agree: dir_e == 0
        await self.tick(inst, snap_of(1000, dir_e=0, dir_f=-1, close=95.0))
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(inst.client.market_orders, [])
        self.assertEqual(inst.state.status, "IDLE")
        # next candle: the entry rules now agree short -> enters
        await self.tick(inst, snap_of(1000 + 4 * 3600 * 1000, dir_e=-1, dir_f=-1, close=94.0))
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(inst.client.market_orders[0][1], "SELL")

    async def test_reverse_unchanged(self):
        inst = self.make(self.plan(hub.FLIP_REVERSE))
        self.go_long(inst)
        await self.tick(inst, snap_of(1000, dir_e=0, dir_f=-1, close=95.0))
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(len(inst.client.market_orders), 1)       # opens at once without the entry rules

    def test_old_close_only_name_still_means_reverse(self):
        p = hub.complete_plan(dict(self.plan(hub.FLIP_REVERSE), flip_action=hub.FLIP_CLOSE_ONLY_OLD))
        self.assertEqual(p["flip_action"], hub.FLIP_REVERSE)
        hub.validate_plan(hub.complete_plan(self.plan(hub.FLIP_CLOSE_WAIT)))


class LateSkipRuns(Base):
    async def test_late_signal_is_not_opened_fresh_one_is(self):
        plan = make_plan(ind_ma1={"entry": True, "flip": True}, late_skip={"on": True, "minutes": 30})
        inst = self.make(plan)
        from app.hub_trading import CANDLE_MS
        tf = CANDLE_MS[inst.pc.timeframe]
        now_ms = int(time.time() * 1000)
        late_open = now_ms - tf - 50 * 60000           # candle closed 50 minutes ago
        await self.tick(inst, snap_of(late_open, dir_e=1, dir_f=1))
        self.assertEqual(inst.client.market_orders, [])
        self.assertIn("late signal", inst.state.last_decision.lower())

    async def test_fresh_signal_opens(self):
        plan = make_plan(ind_ma1={"entry": True, "flip": True}, late_skip={"on": True, "minutes": 30})
        inst = self.make(plan)
        from app.hub_trading import CANDLE_MS
        tf = CANDLE_MS[inst.pc.timeframe]
        now_ms = int(time.time() * 1000)
        await self.tick(inst, snap_of(now_ms - tf - 20000, dir_e=1, dir_f=1))
        self.assertEqual(len(inst.client.market_orders), 1)


class TradeFinishedTotals(Base):
    def test_counter_trade_feeds_its_own_total_when_separate(self):
        inst = self.make(fixed_plan("separate"))
        inst.hub.trade_amount, inst.hub.trade_pnl, inst.hub.trade_kind = 40.0, 10.0, "counter"
        inst._hub_trade_finished()
        self.assertEqual(inst.hub.last_total_ct, 50.0)
        self.assertIsNone(inst.hub.last_total)
        inst.hub.trade_amount, inst.hub.trade_pnl, inst.hub.trade_kind = 80.0, -5.0, "trend"
        inst._hub_trade_finished()
        self.assertEqual(inst.hub.last_total, 75.0)
        self.assertEqual(inst.hub.last_total_ct, 50.0)

    def test_shared_keeps_one_total(self):
        inst = self.make(fixed_plan())
        inst.hub.trade_amount, inst.hub.trade_pnl, inst.hub.trade_kind = 40.0, 10.0, "counter"
        inst._hub_trade_finished()
        self.assertEqual(inst.hub.last_total, 50.0)
        self.assertIsNone(inst.hub.last_total_ct)

    def test_size_starts_again_clears_both(self):
        from app import hub_state
        inst = self.make(fixed_plan("separate"))
        inst.hub.last_total, inst.hub.last_total_ct = 100.0, 60.0
        hub_state.apply_start_as(inst.hub, {"size": True})
        self.assertIsNone(inst.hub.last_total)
        self.assertIsNone(inst.hub.last_total_ct)


class SaveAlert(unittest.TestCase):
    def test_failed_write_sends_one_alert_and_still_raises(self):
        fsutil._save_alerted.clear()
        bad = Path(tempfile.mkdtemp()) / "nope" / "x.json"
        with mock.patch.object(api_guard, "send_alert") as sa:
            for _ in range(2):
                with self.assertRaises(OSError):
                    fsutil.atomic_write(bad, "{}", secret=False)
        self.assertEqual(sa.call_count, 1)
        self.assertIn("x.json", sa.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
