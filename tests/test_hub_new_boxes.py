"""Strategy Hub, Layout 2 boxes that are new in the bot (arbitrary test numbers only):
  * combine "At least N" (ENTRY and FLIP each with their own N)
  * lock-out % inside ENTRY SIZE (fixed base only; off = the whole total is carried)
  * "Email alert on margin"
  * the strategy name (a label only)"""
import os
import sys
import unittest
import unittest.mock as mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_trading as T  # noqa: E402  (also sets up the stubs + a temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_hub_trading import snap_of  # noqa: E402
from tests.test_strategy_hub import make_df  # noqa: E402
from app import strategy_hub as H  # noqa: E402

AT = H.COMBINE_ATLEAST


def three_block_plan(**over):
    """Candle colour + MA 1 + RSI ticked for ENTRY."""
    return make_plan(ind_candle=dict(entry=True), ind_ma1=dict(entry=True, type="EMA", length=20),
                     ind_rsi=dict(entry=True), **over)


class TestAtLeastNValidation(unittest.TestCase):
    def test_at_least_needs_n(self):
        p = three_block_plan(combine_entry=AT)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_n_must_be_a_whole_number_of_at_least_1(self):
        for bad in (0, -1, 1.5, True, "2", None):
            p = three_block_plan(combine_entry=AT, entry_min=bad)
            with self.assertRaises(H.PlanError, msg=repr(bad)):
                H.validate_plan(p)

    def test_n_cannot_be_more_than_the_ticked_indicators(self):
        p = three_block_plan(combine_entry=AT, entry_min=4)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_n_equal_to_the_ticked_count_is_fine(self):
        for n in (1, 2, 3):
            H.validate_plan(three_block_plan(combine_entry=AT, entry_min=n))

    def test_flip_has_its_own_n(self):
        p = make_plan(ind_candle=dict(entry=True, flip=True), ind_ma1=dict(entry=True, flip=True),
                      ind_rsi=dict(entry=True), combine_entry=AT, entry_min=3, combine_flip=AT)
        with self.assertRaises(H.PlanError):           # flip N missing
            H.validate_plan(p)
        p["flip_min"] = 3                              # only 2 ticked for flip
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p["flip_min"] = 2
        H.validate_plan(p)

    def test_all_and_any_need_no_n(self):
        for mode in ("ALL", "ANY"):
            p = three_block_plan(combine_entry=mode)
            self.assertNotIn("entry_min", p)
            H.validate_plan(p)

    def test_unknown_mode_still_refused(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(three_block_plan(combine_entry="BOTH"))

    def test_complete_plan_gives_inert_n(self):
        c = H.complete_plan(three_block_plan())
        self.assertEqual(c["entry_min"], 1)
        self.assertEqual(c["flip_min"], 1)

    def test_summary_shows_n(self):
        s = H.plan_summary(H.complete_plan(three_block_plan(combine_entry=AT, entry_min=2)))
        self.assertIn("At least 2", s)


class TestAtLeastNSignal(unittest.TestCase):
    def setUp(self):
        self.df = make_df()

    def test_n_equals_count_is_all_and_n_1_is_any(self):
        fa = H.compute_hub(self.df, three_block_plan(combine_entry="ALL"))
        fy = H.compute_hub(self.df, three_block_plan(combine_entry="ANY"))
        f3 = H.compute_hub(self.df, three_block_plan(combine_entry=AT, entry_min=3))
        f1 = H.compute_hub(self.df, three_block_plan(combine_entry=AT, entry_min=1))
        np.testing.assert_array_equal(f3["entry_long"].values, fa["entry_long"].values)
        np.testing.assert_array_equal(f3["entry_short"].values, fa["entry_short"].values)
        np.testing.assert_array_equal(f3["dir_e"].values, fa["dir_e"].values)
        np.testing.assert_array_equal(f1["entry_long"].values, fy["entry_long"].values)
        np.testing.assert_array_equal(f1["dir_e"].values, fy["dir_e"].values)

    def test_n_2_of_3_matches_a_hand_count(self):
        f = H.compute_hub(self.df, three_block_plan(combine_entry=AT, entry_min=2))
        nl = sum(f[f"{b}_long"].values.astype(int) for b in ("candle", "ma1", "rsi"))
        ns = sum(f[f"{b}_short"].values.astype(int) for b in ("candle", "ma1", "rsi"))
        np.testing.assert_array_equal(f["entry_long"].values, nl >= 2)
        np.testing.assert_array_equal(f["entry_short"].values, ns >= 2)
        both = (nl >= 2) & (ns >= 2)
        d = np.where((nl >= 2) & ~(ns >= 2), 1, np.where((ns >= 2) & ~(nl >= 2), -1, 0))
        np.testing.assert_array_equal(f["dir_e"].values, d)
        if both.any():
            self.assertTrue((f["dir_e"].values[both] == 0).all())

    def test_there_are_bars_where_2_of_3_fires_but_all_3_does_not(self):
        f2 = H.compute_hub(self.df, three_block_plan(combine_entry=AT, entry_min=2))
        f3 = H.compute_hub(self.df, three_block_plan(combine_entry="ALL"))
        self.assertTrue((f2["entry_long"].values & ~f3["entry_long"].values).any())

    def test_only_ticked_indicators_count(self):
        # RSI is not ticked for ENTRY: N = 2 of the two ticked ones is the same as ALL of those two
        pa = make_plan(ind_candle=dict(entry=True), ind_ma1=dict(entry=True, type="EMA", length=20),
                       ind_rsi=dict(entry=False, flip=True), combine_entry="ALL")
        pn = make_plan(ind_candle=dict(entry=True), ind_ma1=dict(entry=True, type="EMA", length=20),
                       ind_rsi=dict(entry=False, flip=True), combine_entry=AT, entry_min=2)
        fa, fn = H.compute_hub(self.df, pa), H.compute_hub(self.df, pn)
        np.testing.assert_array_equal(fa["dir_e"].values, fn["dir_e"].values)

    def test_flip_uses_flip_min(self):
        p = make_plan(ind_candle=dict(entry=True, flip=True), ind_ma1=dict(entry=True, flip=True, type="EMA", length=20),
                      ind_rsi=dict(flip=True), combine_entry="ALL", combine_flip=AT, flip_min=2)
        f = H.compute_hub(self.df, p)
        nl = sum(f[f"{b}_long"].values.astype(int) for b in ("candle", "ma1", "rsi"))
        np.testing.assert_array_equal(f["flip_long"].values, nl >= 2)


class TestNameValidation(unittest.TestCase):
    def test_name_is_optional_and_a_label(self):
        H.validate_plan(three_block_plan())
        H.validate_plan(three_block_plan(name="BTC 1H compounding"))

    def test_bad_names_refused(self):
        for bad in ("", "   ", "x" * 61, 5, ["a"]):
            with self.assertRaises(H.PlanError, msg=repr(bad)):
                H.validate_plan(three_block_plan(name=bad))

    def test_name_does_not_change_the_rules_hash(self):
        from app.hub_trading import HubMixin
        a = three_block_plan()
        b = three_block_plan(name="Other")
        self.assertEqual(HubMixin._plan_hash(a), HubMixin._plan_hash(b))
        c = three_block_plan(entry_wait=2)
        self.assertNotEqual(HubMixin._plan_hash(a), HubMixin._plan_hash(c))


def fixed_sz(**over):
    return {"method": H.SIZE_FIXED, "base_amount": 100.0, "leverage": 10, "stop_on": False, "stop_pct": 5.0,
            "stop_basis": H.STOP_ENTRY, **over}


class TestLockOutMath(unittest.TestCase):
    def test_off_carries_everything(self):
        self.assertAlmostEqual(H.carry_total(100, 20, None), 120)
        self.assertAlmostEqual(H.carry_total(100, -30, None), 70)

    def test_profit_adds_only_the_percent(self):
        self.assertAlmostEqual(H.carry_total(100, 20, 50), 110)
        self.assertAlmostEqual(H.carry_total(110, 10, 50), 115)
        self.assertAlmostEqual(H.carry_total(100, 20, 100), 120)
        self.assertAlmostEqual(H.carry_total(100, 20, 0), 100)

    def test_a_loss_comes_off_in_full(self):
        self.assertAlmostEqual(H.carry_total(110, -12, 50), 98)
        self.assertAlmostEqual(H.carry_total(110, -12, 0), 98)
        self.assertAlmostEqual(H.carry_total(110, -12, 100), 98)

    def test_zero_result(self):
        self.assertAlmostEqual(H.carry_total(100, 0, 50), 100)

    def test_ten_trade_run_with_base_as_minimum(self):
        base, amount = 100.0, 100.0
        results = [20, -30, 10, 40, -15, -60, 8, 8, -2, 30]
        want = []
        for r in results:
            carried = H.carry_total(amount, r, 50)
            amount = max(base, carried)
            want.append(round(amount, 4))
        self.assertEqual(want, [110.0, 100.0, 105.0, 125.0, 110.0, 100.0, 104.0, 108.0, 106.0, 121.0])


class TestLockOutValidation(unittest.TestCase):
    def plan(self, **sz):
        return make_plan(ind_ma1=dict(entry=True), sizing=fixed_sz(**sz))

    def test_off_when_absent(self):
        p = self.plan()
        H.validate_plan(p)
        self.assertIsNone(H.complete_plan(p)["sizing"]["lock_pct"])

    def test_values(self):
        for ok in (0, 50, 100, 12.5):
            H.validate_plan(self.plan(lock_pct=ok))
        for bad in (-1, 100.5, "50", True):
            with self.assertRaises(H.PlanError, msg=repr(bad)):
                H.validate_plan(self.plan(lock_pct=bad))

    def test_only_with_the_fixed_base(self):
        p = make_plan(ind_ma1=dict(entry=True), sizing={"lock_pct": 50})
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_margin_alert_is_a_switch(self):
        H.validate_plan(self.plan(margin_alert=True))
        H.validate_plan(self.plan(margin_alert=False))
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.plan(margin_alert="yes"))
        self.assertFalse(H.complete_plan(self.plan())["sizing"]["margin_alert"])


class TestLiveLockOutAndAlert(T.Base):
    def setUp(self):
        super().setUp()
        import tempfile
        import shutil
        from pathlib import Path
        from app import ledger
        d = tempfile.mkdtemp(prefix="ledger_test_")
        p = mock.patch.object(ledger, "LEDGER_DIR", Path(d))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, d, True)

    def inst_for(self, **sz):
        return self.make(make_plan(ind_ma1={"entry": True, "flip": True}, sizing=fixed_sz(**sz)))

    async def test_lock_out_50_percent_through_the_live_loop(self):
        """Base 100. Trade 1 +20 -> next entry 100 + 50% of 20 = 110. Trade 2 loses -> comes off in full, base is the floor."""
        inst = self.inst_for(lock_pct=50)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))              # LONG, margin 100, qty 10
        self.assertAlmostEqual(inst.client.market_orders[0][2], 10.0, places=3)
        inst.client.mark_price = 102.0                                                    # +2 x 10 = +20
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1, close=102.0))            # flip -> SHORT on 110
        self.assertAlmostEqual(inst.hub.trade_amount, 110.0, places=6)
        self.assertAlmostEqual(inst.client.market_orders[1][2], 110.0 * 10 / 102.0, places=2)
        qty2 = inst.client.market_orders[1][2]
        inst.client.mark_price = 103.0                                                    # short loses 1 x qty2 (~10.8)
        await self.tick(inst, snap_of(3000, dir_e=1, dir_f=1, close=103.0))              # flip -> LONG
        loss = 1.0 * qty2
        want = max(100.0, 110.0 - loss)
        self.assertAlmostEqual(inst.hub.trade_amount, want, places=4)
        self.assertLess(110.0 - loss, 110.0)

    async def test_lock_out_off_carries_the_whole_total(self):
        inst = self.inst_for()
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        inst.client.mark_price = 102.0
        await self.tick(inst, snap_of(2000, dir_e=-1, dir_f=-1, close=102.0))
        self.assertAlmostEqual(inst.hub.trade_amount, 120.0, places=6)

    async def test_lock_out_total_is_saved(self):
        inst = self.make(make_plan(ind_ma1={"entry": True, "flip": True}, sizing=fixed_sz(lock_pct=50),
                                   flip_action="Close only (go flat)"))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        inst.client.mark_price = 102.0
        await self.tick(inst, snap_of(2000, dir_e=0, dir_f=-1, close=102.0))
        self.assertAlmostEqual(inst.hub.last_total, 110.0, places=6)

    async def test_margin_alert_sent_once_per_candle_when_margin_exceeds_balance(self):
        inst = self.inst_for(margin_alert=True)
        inst.client.available_balance = 50.0           # the entry needs 100 margin
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em, \
                mock.patch("app.hub_trading.tg.notify_info", new=mock.AsyncMock(return_value=None)) as ni:
            em.send = mock.AsyncMock()
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
            self.assertEqual(em.send.await_count, 1)
            subject, body = em.send.await_args.args
            self.assertIn("Margin Alert", subject)
            self.assertIn("100.00", body)
            self.assertIn("50.00", body)
            self.assertEqual(ni.await_count, 1)
            # the same candle again: no second email
            await inst._hub_margin_alert(10.0, 100.0, 10, 50.0, 1000)
            self.assertEqual(em.send.await_count, 1)
            # a new candle: alert again
            await inst._hub_margin_alert(10.0, 100.0, 10, 50.0, 2000)
            self.assertEqual(em.send.await_count, 2)

    async def test_no_alert_when_the_balance_is_enough(self):
        inst = self.inst_for(margin_alert=True)
        inst.client.available_balance = 5000.0
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em:
            em.send = mock.AsyncMock()
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
            self.assertEqual(em.send.await_count, 0)

    async def test_no_alert_when_the_box_is_off(self):
        inst = self.inst_for()
        inst.client.available_balance = 50.0
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em:
            em.send = mock.AsyncMock()
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
            self.assertEqual(em.send.await_count, 0)

    async def test_an_email_error_never_stops_the_entry(self):
        inst = self.inst_for(margin_alert=True)
        inst.client.available_balance = 50.0
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em:
            em.send = mock.AsyncMock(side_effect=RuntimeError("smtp down"))
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)


if __name__ == "__main__":
    unittest.main()
