"""Strategy Hub, optional box "Enter with what is available" (sizing.short_enter + short_buffer), arbitrary test numbers only:
  * plan checks and defaults (OFF when absent)
  * the pure sizing function short_balance_fit
  * through the live loop: smaller entry, one message per candle, max loss on the real margin, email error never blocks,
    OFF changes nothing, enough balance changes nothing"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_trading as T  # noqa: E402  (also sets up the stubs + a temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_hub_trading import snap_of  # noqa: E402
from tests.test_hub_new_boxes import fixed_sz  # noqa: E402
from app import strategy_hub as H  # noqa: E402


class TestShortBalanceValidation(unittest.TestCase):
    def plan(self, **sz):
        return make_plan(ind_ma1=dict(entry=True), sizing=fixed_sz(**sz))

    def test_off_when_absent(self):
        p = self.plan()
        H.validate_plan(p)
        c = H.complete_plan(p)["sizing"]
        self.assertFalse(c["short_enter"])
        self.assertIsNone(c["short_buffer"])

    def test_switch_values(self):
        H.validate_plan(self.plan(short_enter=True))
        H.validate_plan(self.plan(short_enter=False))
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.plan(short_enter="yes"))

    def test_buffer_values(self):
        for ok in (0, 2, 2.5, 50):
            H.validate_plan(self.plan(short_enter=True, short_buffer=ok))
        for bad in (-1, 50.5, "2", True):
            with self.assertRaises(H.PlanError, msg=repr(bad)):
                H.validate_plan(self.plan(short_enter=True, short_buffer=bad))

    def test_review_page_line(self):
        rows = H.plan_lines(H.complete_plan(self.plan(short_enter=True)))
        text = " ".join(" ".join(map(str, r)) for r in rows)
        self.assertIn("enter with what is available", text)
        self.assertIn("2%", text)                      # the default fee buffer
        rows = H.plan_lines(H.complete_plan(self.plan(short_enter=True, short_buffer=5)))
        self.assertIn("5%", " ".join(" ".join(map(str, r)) for r in rows))
        rows = H.plan_lines(H.complete_plan(self.plan()))
        self.assertNotIn("what is available", " ".join(" ".join(map(str, r)) for r in rows))


class TestShortBalanceMath(unittest.TestCase):
    def test_off_changes_nothing(self):
        self.assertEqual(H.short_balance_fit({}, 100.0, 50.0), (100.0, 0.0))
        self.assertEqual(H.short_balance_fit({"short_enter": False}, 100.0, 50.0), (100.0, 0.0))

    def test_enough_balance_changes_nothing(self):
        self.assertEqual(H.short_balance_fit({"short_enter": True}, 100.0, 5000.0), (100.0, 0.0))
        # exactly the usable balance (default 2% kept free): still fits
        use, short = H.short_balance_fit({"short_enter": True}, 98.0, 100.0)
        self.assertAlmostEqual(use, 98.0)
        self.assertAlmostEqual(short, 0.0)

    def test_short_balance_default_buffer(self):
        use, short = H.short_balance_fit({"short_enter": True}, 10000.0, 8000.0)
        self.assertAlmostEqual(use, 7840.0)
        self.assertAlmostEqual(short, 2160.0)

    def test_custom_and_zero_buffer(self):
        use, short = H.short_balance_fit({"short_enter": True, "short_buffer": 10}, 100.0, 50.0)
        self.assertAlmostEqual(use, 45.0)
        self.assertAlmostEqual(short, 55.0)
        use, short = H.short_balance_fit({"short_enter": True, "short_buffer": 0}, 100.0, 50.0)
        self.assertAlmostEqual(use, 50.0)
        self.assertAlmostEqual(short, 50.0)

    def test_no_balance(self):
        use, short = H.short_balance_fit({"short_enter": True}, 100.0, 0.0)
        self.assertEqual(use, 0.0)
        self.assertEqual(short, 100.0)
        use, short = H.short_balance_fit({"short_enter": True}, 100.0, -5.0)
        self.assertEqual(use, 0.0)


class TestShortBalanceLive(T.Base):
    def setUp(self):
        super().setUp()
        from app import ledger
        d = tempfile.mkdtemp(prefix="ledger_test_")
        p = mock.patch.object(ledger, "LEDGER_DIR", Path(d))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, d, True)

    def inst_for(self, **sz):
        return self.make(make_plan(ind_ma1={"entry": True, "flip": True}, sizing=fixed_sz(**sz)))

    async def test_short_balance_enters_smaller_and_sends_one_message(self):
        inst = self.inst_for(short_enter=True)
        inst.client.available_balance = 50.0              # the entry needs 100 margin (qty 10 at 10x, price 100)
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em, \
                mock.patch("app.hub_trading.tg.notify_info", new=mock.AsyncMock(return_value=None)) as ni:
            em.send = mock.AsyncMock()
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
            self.assertEqual(len(inst.client.market_orders), 1)
            qty = inst.client.market_orders[0][2]
            # usable = 50 x 98% = 49 margin -> position 490 -> qty 4.9
            self.assertAlmostEqual(qty, 4.9, places=2)
            self.assertLessEqual(qty * 100.0 / 10.0, 49.0 + 1e-9)
            self.assertEqual(em.send.await_count, 1)
            subject, body = em.send.await_args.args
            self.assertIn("Balance Short", subject)
            self.assertIn("100.00", body)
            self.assertIn("50.00", body)
            self.assertEqual(ni.await_count, 1)
            # the trade's total is built on the REAL margin, not the planned 100
            self.assertAlmostEqual(inst.hub.trade_amount, qty * 100.0 / 10.0, places=6)

    async def test_off_sends_the_full_amount(self):
        inst = self.inst_for()
        inst.client.available_balance = 50.0
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em:
            em.send = mock.AsyncMock()
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
            self.assertAlmostEqual(inst.client.market_orders[0][2], 10.0, places=3)
            self.assertEqual(em.send.await_count, 0)

    async def test_enough_balance_no_change_no_message(self):
        inst = self.inst_for(short_enter=True)
        inst.client.available_balance = 5000.0
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em:
            em.send = mock.AsyncMock()
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
            self.assertAlmostEqual(inst.client.market_orders[0][2], 10.0, places=3)
            self.assertEqual(em.send.await_count, 0)
            self.assertAlmostEqual(inst.hub.trade_amount, 100.0, places=6)

    async def test_zero_buffer_uses_all_the_balance(self):
        inst = self.inst_for(short_enter=True, short_buffer=0)
        inst.client.available_balance = 50.0
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em:
            em.send = mock.AsyncMock()
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
            self.assertAlmostEqual(inst.client.market_orders[0][2], 5.0, places=3)

    async def test_an_email_error_never_stops_the_smaller_entry(self):
        inst = self.inst_for(short_enter=True)
        inst.client.available_balance = 50.0
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em:
            em.send = mock.AsyncMock(side_effect=RuntimeError("smtp down"))
            await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertAlmostEqual(inst.client.market_orders[0][2], 4.9, places=2)

    async def test_max_loss_stop_is_placed_on_the_smaller_entry(self):
        # 10x, max loss 10% of the entry amount -> the stop is 1% from the entry price (99.0 for a long at 100),
        # and it is placed for the smaller quantity
        inst = self.inst_for(short_enter=True, stop_on=True, stop_pct=10.0, stop_basis=H.STOP_ENTRY)
        inst.client.available_balance = 50.0
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        qty = inst.client.market_orders[0][2]
        self.assertAlmostEqual(qty * 100.0 / 10.0, 49.0, places=1)
        self.assertEqual(len(inst.client.stop_orders), 1)
        self.assertEqual(inst.client.stop_orders[0][1], "SELL")
        self.assertAlmostEqual(inst.client.stop_orders[0][2], 99.0, places=2)

    async def test_balance_too_small_to_trade_skips_like_before(self):
        inst = self.inst_for(short_enter=True)
        inst.client.available_balance = 0.0
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(1000, dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 0)

    async def test_a_second_candle_sends_a_second_message(self):
        inst = self.inst_for(short_enter=True)
        inst.client.available_balance = 50.0
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em:
            em.send = mock.AsyncMock()
            await inst._hub_fit_short_balance(10.0, 100.0, 10, 50.0, 1000)
            await inst._hub_fit_short_balance(10.0, 100.0, 10, 50.0, 1000)
            self.assertEqual(em.send.await_count, 1)
            await inst._hub_fit_short_balance(10.0, 100.0, 10, 50.0, 2000)
            self.assertEqual(em.send.await_count, 2)


if __name__ == "__main__":
    unittest.main()
