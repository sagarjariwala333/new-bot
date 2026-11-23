"""Strategy Hub, Shadow box option "Start after every win" (shadow.every_win), arbitrary test numbers only:
  * plan checks and defaults (OFF when absent)
  * tracker: OFF = only a win that makes a new tracker high starts the period (unchanged); ON = every win does; a loss never does
  * the review line"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_trading as T  # noqa: E402,F401  (stubs + temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import base_v3_tracker as TR  # noqa: E402


def shadow(**over):
    return {"on": True, "trades": 1, "slippage_ticks": 0, "tracker_start": 1000.0, **over}


class TestEveryWinPlan(unittest.TestCase):
    def plan(self, **sh):
        return make_plan(ind_ma1=dict(entry=True), shadow=shadow(**sh))

    def test_off_when_absent(self):
        p = self.plan()
        H.validate_plan(p)
        self.assertFalse(H.complete_plan(p)["shadow"]["every_win"])

    def test_switch_values(self):
        H.validate_plan(self.plan(every_win=True))
        H.validate_plan(self.plan(every_win=False))
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.plan(every_win="yes"))

    def test_review_line(self):
        rows = H.plan_lines(H.complete_plan(self.plan(every_win=True)))
        self.assertIn("every winning trade", " ".join(" ".join(map(str, r)) for r in rows))
        rows = H.plan_lines(H.complete_plan(self.plan()))
        self.assertIn("new tracker high", " ".join(" ".join(map(str, r)) for r in rows))


class TestEveryWinTracker(unittest.TestCase):
    def state(self):
        return TR.TrackerState(platform="binance", account_id="a", symbol="BTCUSDT",
                               start_balance=1000.0, balance=1000.0, peak=1000.0)

    def close(self, st, pct, every_win):
        return TR.apply_real_close(st, result_pct=pct, pnl=pct, direction="LONG", entry_price=1.0, exit_price=1.0,
                                   reason="t", balance_at_entry=1000.0, use_shadow=True, every_win=every_win)

    def test_default_new_high_only(self):
        st = self.state()
        self.assertTrue(self.close(st, 10, False))      # 1100: new high
        self.assertFalse(self.close(st, -20, False))    # 880
        self.assertFalse(self.close(st, 10, False))     # 968: a win but NOT a new high
        self.assertEqual(st.get_shadow().periods, 1)

    def test_every_win_starts_even_without_new_high(self):
        st = self.state()
        self.assertTrue(self.close(st, 10, True))
        self.assertFalse(self.close(st, -20, True))     # a loss never starts it
        self.assertTrue(self.close(st, 10, True))       # 968: a win, not a new high -> starts
        self.assertEqual(st.get_shadow().periods, 2)
        self.assertAlmostEqual(st.peak, 1100.0)         # the high-watermark is unchanged by the option

    def test_off_in_the_plan_means_no_shadow_at_all(self):
        st = self.state()
        started = TR.apply_real_close(st, result_pct=10, pnl=10, direction="LONG", entry_price=1.0, exit_price=1.0,
                                      reason="t", balance_at_entry=1000.0, use_shadow=False, every_win=True)
        self.assertFalse(started)


if __name__ == "__main__":
    unittest.main()


def _live_cases():
    from tests.test_hub_sizing_shadow import IsolatedTracker, plan_of, sizing
    from tests.test_hub_trading import snap_of

    class Live(IsolatedTracker):
        async def run_case(self, resume_now):
            sh = dict(on=True, trades=1, slippage_ticks=0, tracker_start=1000.0, resume_now=resume_now)
            inst = self.make(plan_of(shadow=sh, sizing=sizing(), allow_reentry=False))
            self.go_long(inst, price=100.0)
            inst.client.mark_price = 110.0
            await self.tick(inst, snap_of(1000, dir_e=-1, dir_f=-1, close=110.0))   # long wins -> pause, paper short
            self.assertEqual(inst.client.market_orders, [])
            await self.tick(inst, snap_of(1001, dir_e=-1, dir_f=-1, close=108.0))
            await self.tick(inst, snap_of(1002, dir_e=1, dir_f=1, close=112.0))     # flip to long: paper short ends
            at_flip = len(inst.client.market_orders)
            await self.tick(inst, snap_of(1003, dir_e=1, dir_f=1, close=113.0))
            return inst, at_flip

        async def test_off_the_real_long_comes_one_candle_later(self):
            inst, at_flip = await self.run_case(False)
            self.assertEqual(at_flip, 0)
            self.assertEqual([o[1] for o in inst.client.market_orders], ["BUY"])

        async def test_on_the_real_long_comes_on_the_flip_candle(self):
            inst, at_flip = await self.run_case(True)
            self.assertEqual(at_flip, 1)
            self.assertEqual([o[1] for o in inst.client.market_orders], ["BUY"])
    return Live


Live = _live_cases()


class TestResumeNowPlan(unittest.TestCase):
    def plan(self, **sh):
        return make_plan(ind_ma1=dict(entry=True), shadow=shadow(**sh))

    def test_off_when_absent_and_switch(self):
        p = self.plan()
        H.validate_plan(p)
        self.assertFalse(H.complete_plan(p)["shadow"]["resume_now"])
        H.validate_plan(self.plan(resume_now=True))
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.plan(resume_now="yes"))

    def test_review_line(self):
        rows = H.plan_lines(H.complete_plan(self.plan(resume_now=True)))
        self.assertIn("enters right away", " ".join(" ".join(map(str, r)) for r in rows))
