"""Strategy Hub (2026-11-01): COOLDOWN AFTER A LOSS and LOSS LIMIT (strategy / account / all accounts).
Owner rules tested here:
  * both only stop a NEW entry - "no trade should be distracted by external factors.. only its own price action"
  * Cooldown: after a losing trade (all fills added up, net of fees) the pair skips N candle closes; optional "losses in
    a row"; a win or a flat trade ends the row
  * Loss limit: daily / weekly amount in USDT; three levels (this strategy, this account, all accounts together);
    days and weeks in Singapore time (week = Monday 00:00)
  * a plan without the new keys behaves exactly as before (the plan hash does not change)
Arbitrary test numbers only."""
import copy
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_trading as T  # noqa: E402  (also sets up the stubs + a temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_hub_trading import snap_of  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import hub_state, ledger, loss_limits as LL  # noqa: E402
from app.hub_trading import CANDLE_MS  # noqa: E402

NONE4 = {"daily": None, "weekly": None, "daily_pct": None, "weekly_pct": None}   # 2026-11-02: % keys
L1H = CANDLE_MS["1h"]
T0 = 1_000 * L1H           # an aligned 1h candle open time (ms)
SGT = timezone(timedelta(hours=8))


def t_(k):
    """Open time of the k-th 1h candle after T0."""
    return T0 + k * L1H


def plan_of(**over):
    over.setdefault("allow_reentry", True)
    return make_plan(ind_ma1={"entry": True, "flip": True}, **over)


# ---------------------------------------------------------------------------------------------- plan checks
class TestPlanChecks(unittest.TestCase):
    def test_older_plan_has_neither_and_the_hash_inputs_do_not_change(self):
        p = plan_of()
        H.validate_plan(p)
        c = H.complete_plan(p)
        self.assertNotIn("cooldown", c)
        self.assertNotIn("loss_limit", c)
        self.assertIsNone(H.cooldown_cfg(c))
        self.assertIsNone(H.loss_limit_cfg(c))
        up = H.upgrade_plan(p)
        self.assertNotIn("cooldown", up)
        self.assertNotIn("loss_limit", up)

    def test_switched_off_is_valid_and_needs_no_numbers(self):
        for k in ("cooldown", "loss_limit"):
            p = plan_of(**{k: {"on": False}})
            H.validate_plan(p)
            self.assertIsNone(H.cooldown_cfg(H.complete_plan(p)))
            self.assertIsNone(H.loss_limit_cfg(H.complete_plan(p)))

    def test_cooldown_needs_candles(self):
        H.validate_plan(plan_of(cooldown={"on": True, "candles": 3}))
        H.validate_plan(plan_of(cooldown={"on": True, "candles": 3, "after_losses": 2}))
        for bad in ({"on": True}, {"on": True, "candles": 0}, {"on": True, "candles": -1}, {"on": True, "candles": 1.5},
                    {"on": True, "candles": True}, {"on": True, "candles": "3"},
                    {"on": True, "candles": 3, "after_losses": 0}, {"on": True, "candles": 3, "after_losses": 1.5},
                    {"candles": 3}, {"on": "yes", "candles": 3}):
            with self.assertRaises(H.PlanError, msg=repr(bad)):
                H.validate_plan(plan_of(cooldown=bad))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_of(cooldown=[1]))

    def test_loss_limit_needs_one_amount(self):
        H.validate_plan(plan_of(loss_limit={"on": True, "daily": 20.0}))
        H.validate_plan(plan_of(loss_limit={"on": True, "weekly": 50}))
        H.validate_plan(plan_of(loss_limit={"on": True, "daily": 20.0, "weekly": 50.0}))
        for bad in ({"on": True}, {"on": True, "daily": 0}, {"on": True, "daily": -5}, {"on": True, "daily": 0.001},
                    {"on": True, "weekly": float("nan")}, {"on": True, "weekly": float("inf")},
                    {"on": True, "daily": True}, {"on": True, "daily": "20"}, {"daily": 20}, {"on": 1, "daily": 20}):
            with self.assertRaises(H.PlanError, msg=repr(bad)):
                H.validate_plan(plan_of(loss_limit=bad))

    def test_complete_plan_keeps_the_keys_as_given(self):
        p = plan_of(cooldown={"on": True, "candles": 3}, loss_limit={"on": True, "daily": 20.0})
        c = H.complete_plan(p)
        self.assertEqual(c["cooldown"], {"on": True, "candles": 3})
        self.assertEqual(c["loss_limit"], {"on": True, "daily": 20.0})


# ---------------------------------------------------------------------------------------------- cooldown rule
class TestCooldownRule(unittest.TestCase):
    def plan(self, **cd):
        return H.complete_plan(plan_of(cooldown={"on": True, "candles": 3, **cd}))

    def test_a_loss_starts_it(self):
        streak, through = H.cooldown_after_close(self.plan(), 0, -1.0, t_(10), L1H)
        self.assertEqual(streak, 1)
        self.assertEqual(through, t_(13))          # signal candles 10, 11, 12, 13 are blocked

    def test_a_win_or_zero_starts_nothing_and_ends_the_row(self):
        for res in (5.0, 0.0):
            streak, through = H.cooldown_after_close(self.plan(), 4, res, t_(10), L1H)
            self.assertEqual((streak, through), (0, None))

    def test_off_still_counts_nothing_to_block(self):
        plan = H.complete_plan(plan_of())
        streak, through = H.cooldown_after_close(plan, 0, -5.0, t_(10), L1H)
        self.assertEqual(streak, 1)
        self.assertIsNone(through)

    def test_losses_in_a_row(self):
        plan = self.plan(after_losses=2)
        s1, th1 = H.cooldown_after_close(plan, 0, -1.0, t_(10), L1H)
        self.assertEqual((s1, th1), (1, None))
        s2, th2 = H.cooldown_after_close(plan, s1, -1.0, t_(20), L1H)
        self.assertEqual((s2, th2), (2, t_(23)))
        s3, th3 = H.cooldown_after_close(plan, s2, -1.0, t_(30), L1H)       # still in a row: starts again
        self.assertEqual((s3, th3), (3, t_(33)))
        s4, th4 = H.cooldown_after_close(plan, s3, +1.0, t_(40), L1H)       # a win ends the row
        self.assertEqual((s4, th4), (0, None))
        s5, th5 = H.cooldown_after_close(plan, s4, -1.0, t_(50), L1H)       # the count starts again
        self.assertEqual((s5, th5), (1, None))

    def test_unknown_candle_time_or_length_never_blocks(self):
        plan = self.plan()
        self.assertEqual(H.cooldown_after_close(plan, 0, -1.0, None, L1H), (1, None))
        self.assertEqual(H.cooldown_after_close(plan, 0, -1.0, t_(1), None), (1, None))

    def test_blocks_and_counts_left(self):
        through = t_(13)
        self.assertTrue(H.cooldown_blocks(through, t_(10)))
        self.assertTrue(H.cooldown_blocks(through, t_(13)))
        self.assertFalse(H.cooldown_blocks(through, t_(14)))
        self.assertFalse(H.cooldown_blocks(None, t_(10)))
        self.assertEqual(H.cooldown_candles_left(through, t_(10), L1H), 4)
        self.assertEqual(H.cooldown_candles_left(through, t_(13), L1H), 1)
        self.assertEqual(H.cooldown_candles_left(through, t_(14), L1H), 0)


# ---------------------------------------------------------------------------------------------- loss limit file / maths
class LimitsFile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="loss_limits_test_"))
        self.patches = [mock.patch.object(LL, "LIMITS_FILE", self.dir / "loss_limits.json"),
                        mock.patch.object(ledger, "LEDGER_DIR", self.dir / "ledger")]
        (self.dir / "ledger").mkdir()
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, account, symbol, pnl, closed_at=None, **kw):
        ledger.record_trade(ledger.TradeRecord(
            account_id=account, symbol=symbol, direction="LONG", qty=1.0, entry_price=100.0, exit_price=99.0, pnl=pnl,
            reason="SL", opened_at=0.0, closed_at=time.time() if closed_at is None else closed_at, **kw))


class TestPeriods(unittest.TestCase):
    def test_day_and_week_follow_singapore_time(self):
        # Sunday 2026-11-01 07:57 SGT
        now = datetime(2026, 11, 1, 7, 57, tzinfo=SGT).timestamp()
        self.assertEqual(LL.period_start(now, "daily"), datetime(2026, 11, 1, 0, 0, tzinfo=SGT).timestamp())
        self.assertEqual(LL.period_start(now, "weekly"), datetime(2026, 10, 26, 0, 0, tzinfo=SGT).timestamp())   # Monday
        self.assertEqual(LL.period_end(now, "daily"), datetime(2026, 11, 2, 0, 0, tzinfo=SGT).timestamp())
        self.assertEqual(LL.period_end(now, "weekly"), datetime(2026, 11, 2, 0, 0, tzinfo=SGT).timestamp())

    def test_just_after_midnight_sgt_is_a_new_day_even_if_utc_is_still_yesterday(self):
        now = datetime(2026, 11, 3, 0, 5, tzinfo=SGT).timestamp()      # Tuesday 00:05 SGT = Monday 16:05 UTC
        self.assertEqual(LL.period_start(now, "daily"), datetime(2026, 11, 3, 0, 0, tzinfo=SGT).timestamp())
        self.assertEqual(LL.period_start(now, "weekly"), datetime(2026, 11, 2, 0, 0, tzinfo=SGT).timestamp())

    def test_monday_morning_starts_the_week(self):
        now = datetime(2026, 11, 2, 0, 0, 1, tzinfo=SGT).timestamp()
        self.assertEqual(LL.period_start(now, "weekly"), datetime(2026, 11, 2, 0, 0, tzinfo=SGT).timestamp())


class TestLossLimits(LimitsFile):
    NOW = datetime(2026, 11, 5, 15, 0, tzinfo=SGT).timestamp()       # Thursday 15:00 SGT
    TODAY = datetime(2026, 11, 5, 9, 0, tzinfo=SGT).timestamp()
    YESTERDAY = datetime(2026, 11, 4, 20, 0, tzinfo=SGT).timestamp()
    LAST_WEEK = datetime(2026, 11, 1, 20, 0, tzinfo=SGT).timestamp()

    def plan(self, **ll):
        return {"loss_limit": {"on": True, **ll}}

    def test_nothing_set_nothing_blocked(self):
        self.add("a1", "BTCUSDT", -500.0, self.TODAY)
        self.assertIsNone(LL.check("a1", "BTCUSDT", {}, self.NOW))
        self.assertIsNone(LL.check("a1", "BTCUSDT", None, self.NOW))
        self.assertIsNone(LL.check("a1", "BTCUSDT", {"loss_limit": {"on": False, "daily": 1}}, self.NOW))

    def test_strategy_daily(self):
        self.add("a1", "BTCUSDT", -12.0, self.TODAY)
        self.add("a1", "BTCUSDT", -5.0, self.TODAY)
        hit = LL.check("a1", "BTCUSDT", self.plan(daily=15.0), self.NOW)
        self.assertEqual((hit["scope"], hit["kind"]), ("strategy", "daily"))
        self.assertAlmostEqual(hit["lost"], 17.0)
        self.assertIn("LOSS LIMIT", hit["text"])
        self.assertIn("Open trades are not touched", hit["text"])
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily=17.01), self.NOW))
        self.assertIsNotNone(LL.check("a1", "BTCUSDT", self.plan(daily=17.0), self.NOW))     # exactly reached = blocked

    def test_wins_count_against_losses(self):
        self.add("a1", "BTCUSDT", -20.0, self.TODAY)
        self.add("a1", "BTCUSDT", +8.0, self.TODAY)
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily=15.0), self.NOW))        # net -12
        self.assertIsNotNone(LL.check("a1", "BTCUSDT", self.plan(daily=12.0), self.NOW))

    def test_an_open_loss_is_not_counted_and_only_pnl_known_counts(self):
        self.add("a1", "BTCUSDT", None, self.TODAY)
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily=1.0), self.NOW))

    def test_yesterday_does_not_count_for_the_day_but_does_for_the_week(self):
        self.add("a1", "BTCUSDT", -30.0, self.YESTERDAY)
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily=10.0), self.NOW))
        hit = LL.check("a1", "BTCUSDT", self.plan(daily=10.0, weekly=25.0), self.NOW)
        self.assertEqual((hit["scope"], hit["kind"]), ("strategy", "weekly"))

    def test_last_week_counts_for_neither(self):
        self.add("a1", "BTCUSDT", -300.0, self.LAST_WEEK)
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily=10.0, weekly=10.0), self.NOW))

    def test_strategy_level_counts_only_its_own_pair(self):
        self.add("a1", "ETHUSDT", -50.0, self.TODAY)
        self.add("a2", "BTCUSDT", -50.0, self.TODAY)
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily=10.0), self.NOW))

    def test_account_level_counts_all_pairs_of_that_account_only(self):
        self.add("a1", "BTCUSDT", -10.0, self.TODAY)
        self.add("a1", "ETHUSDT", -15.0, self.TODAY)
        self.add("a2", "BTCUSDT", -100.0, self.TODAY)
        LL.save_settings(None, {"a1": {"daily": 20.0, "weekly": None}})
        hit = LL.check("a1", "BTCUSDT", {}, self.NOW)
        self.assertEqual((hit["scope"], hit["kind"]), ("account", "daily"))
        self.assertAlmostEqual(hit["lost"], 25.0)
        self.assertIsNone(LL.check("a2", "BTCUSDT", {}, self.NOW))       # the other account has no limit of its own
        # an account's name is not a prefix match: "a1" must not pick up "a10"
        self.add("a10", "BTCUSDT", -999.0, self.TODAY)
        LL.save_settings(None, {"a1": {"daily": 26.0, "weekly": None}})
        self.assertIsNone(LL.check("a1", "BTCUSDT", {}, self.NOW))

    def test_entire_level_counts_every_account(self):
        self.add("a1", "BTCUSDT", -10.0, self.TODAY)
        self.add("a2", "ETHUSDT", -15.0, self.TODAY)
        LL.save_settings({"daily": 20.0, "weekly": None}, None)
        hit = LL.check("a1", "BTCUSDT", {}, self.NOW)
        self.assertEqual((hit["scope"], hit["kind"]), ("entire", "daily"))
        self.assertAlmostEqual(hit["lost"], 25.0)
        self.assertIsNotNone(LL.check("a2", "ETHUSDT", {}, self.NOW))
        self.assertIsNotNone(LL.check("a3", "XRPUSDT", {}, self.NOW))      # an account with no trades is stopped too

    def test_the_three_levels_are_independent_and_the_first_hit_is_named(self):
        self.add("a1", "BTCUSDT", -30.0, self.TODAY)
        LL.save_settings({"daily": 1000.0, "weekly": None}, {"a1": {"daily": 20.0, "weekly": None}})
        hit = LL.check("a1", "BTCUSDT", self.plan(daily=25.0), self.NOW)
        self.assertEqual(hit["scope"], "strategy")
        hit = LL.check("a1", "BTCUSDT", self.plan(daily=500.0), self.NOW)
        self.assertEqual(hit["scope"], "account")

    def test_keys_differ_per_period_so_the_email_goes_once_per_period(self):
        self.add("a1", "BTCUSDT", -30.0, self.TODAY)
        k1 = LL.check("a1", "BTCUSDT", self.plan(daily=10.0), self.NOW)["key"]
        k1b = LL.check("a1", "BTCUSDT", self.plan(daily=10.0), self.NOW + 60)["key"]
        self.assertEqual(k1, k1b)
        self.assertTrue(LL.first_time(k1, self.NOW))
        self.assertFalse(LL.first_time(k1, self.NOW + 60))
        nxt = self.NOW + 86400            # tomorrow: a new day, a new key
        self.add("a1", "BTCUSDT", -30.0, nxt - 3600)
        k2 = LL.check("a1", "BTCUSDT", self.plan(daily=10.0), nxt)["key"]
        self.assertNotEqual(k1, k2)
        self.assertTrue(LL.first_time(k2, nxt))
        # a different pair / scope never shares a key
        self.assertNotEqual(LL.check("a1", "BTCUSDT", self.plan(daily=10.0), self.NOW)["key"],
                            LL.check("a1", "BTCUSDT", self.plan(weekly=10.0), self.NOW)["key"])

    def test_old_email_marks_are_dropped(self):
        LL.first_time("old", self.NOW - 40 * 86400)
        LL.first_time("new", self.NOW)
        self.assertNotIn("old", LL.load()["sent"])
        self.assertIn("new", LL.load()["sent"])

    def test_settings_are_checked_and_saved(self):
        for bad in (0, -1, 0.001, float("nan"), float("inf"), True, "5"):
            with self.assertRaises(LL.LossLimitError, msg=repr(bad)):
                LL.save_settings({"daily": bad, "weekly": None}, None)
            with self.assertRaises(LL.LossLimitError, msg=repr(bad)):
                LL.save_settings(None, {"a1": {"daily": bad, "weekly": None}})
        with self.assertRaises(LL.LossLimitError):
            LL.save_settings("x", None)
        with self.assertRaises(LL.LossLimitError):
            LL.save_settings(None, ["x"])
        self.assertEqual(LL.load()["entire"], NONE4)     # nothing half-saved
        LL.save_settings({"daily": 5, "weekly": 20}, {"a1": {"daily": 3, "weekly": None}, "a2": {"daily": None, "weekly": 9}})
        d = LL.load()
        self.assertEqual(d["entire"], dict(NONE4, daily=5.0, weekly=20.0))
        self.assertEqual(d["accounts"]["a1"], dict(NONE4, daily=3.0))
        self.assertEqual(d["accounts"]["a2"], dict(NONE4, weekly=9.0))
        # empty both = the account entry goes away; an account not listed is left alone; entire not given is left alone
        LL.save_settings(None, {"a1": {"daily": None, "weekly": None}})
        d = LL.load()
        self.assertNotIn("a1", d["accounts"])
        self.assertIn("a2", d["accounts"])
        self.assertEqual(d["entire"], dict(NONE4, daily=5.0, weekly=20.0))
        LL.save_settings({"daily": None, "weekly": None}, None)
        self.assertEqual(LL.load()["entire"], NONE4)

    def test_an_unreadable_file_is_never_overwritten_and_never_breaks_the_check(self):
        (self.dir / "loss_limits.json").write_text("{ not json")
        with self.assertRaises(LL.LossLimitError):
            LL.load()
        with self.assertRaises(LL.LossLimitError):
            LL.save_settings({"daily": 5, "weekly": None}, None)
        self.assertEqual((self.dir / "loss_limits.json").read_text(), "{ not json")
        self.add("a1", "BTCUSDT", -30.0, self.TODAY)
        # the strategy level still works
        self.assertIsNotNone(LL.check("a1", "BTCUSDT", self.plan(daily=10.0), self.NOW))
        self.assertIsNone(LL.check("a1", "BTCUSDT", {}, self.NOW))

    def test_status_for_the_dashboard(self):
        self.add("a1", "BTCUSDT", -12.0, self.TODAY)
        self.add("a1", "BTCUSDT", -3.0, self.YESTERDAY)
        LL.save_settings({"daily": 50.0, "weekly": None}, {"a1": {"daily": 20.0, "weekly": 40.0}})
        st = LL.status(self.NOW, ["a1", "a2"])
        self.assertAlmostEqual(st["entire"]["daily"]["result"], -12.0)
        self.assertEqual(st["entire"]["daily"]["limit"], 50.0)
        self.assertAlmostEqual(st["entire"]["weekly"]["result"], -15.0)
        self.assertAlmostEqual(st["accounts"]["a1"]["weekly"]["result"], -15.0)
        self.assertEqual(st["accounts"]["a1"]["weekly"]["limit"], 40.0)
        self.assertIsNone(st["accounts"]["a2"]["daily"]["limit"])

    def test_bad_ledger_lines_are_skipped(self):
        self.add("a1", "BTCUSDT", -30.0, self.TODAY)
        with open(ledger._path("a1", "BTCUSDT"), "a") as f:
            f.write("not json\n\n")
        self.assertIsNotNone(LL.check("a1", "BTCUSDT", self.plan(daily=10.0), self.NOW))


# ---------------------------------------------------------------------------------------------- the live loop
class LiveBase(T.Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="loss_cd_live_"))
        (self.dir / "ledger").mkdir()
        self.patches = [mock.patch.object(ledger, "LEDGER_DIR", self.dir / "ledger"),
                        mock.patch.object(LL, "LIMITS_FILE", self.dir / "loss_limits.json")]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(shutil.rmtree, self.dir, True)

    async def long_then_flip_at(self, inst, k_flip, close, k_open=0, open_close=100.0):
        inst.client.mark_price = open_close
        await self.tick(inst, snap_of(t_(k_open), dir_e=1, dir_f=1, close=open_close))
        inst.client.mark_price = close
        await self.tick(inst, snap_of(t_(k_flip), dir_e=-1, dir_f=-1, close=close))

    def add(self, inst, pnl, closed_at=None):
        ledger.record_trade(ledger.TradeRecord(
            account_id=inst.account_id, symbol=inst.symbol, direction="LONG", qty=1.0, entry_price=100.0,
            exit_price=99.0, pnl=pnl, reason="SL", opened_at=0.0,
            closed_at=time.time() if closed_at is None else closed_at))


class TestCooldownLive(LiveBase):
    async def test_a_losing_flip_blocks_the_opposite_entry_for_n_candle_closes(self):
        inst = self.make(plan_of(cooldown={"on": True, "candles": 2}))
        await self.long_then_flip_at(inst, 1, 98.0)                                  # long loses 2 x qty
        c = inst.client
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(len(c.close_orders), 1)
        self.assertEqual(len(c.market_orders), 1)                                    # only the first entry
        self.assertIn("COOLDOWN", inst.state.last_decision)
        self.assertEqual(inst.hub.loss_streak, 1)
        self.assertEqual(inst.hub.cooldown_through, t_(1) + 2 * L1H)
        self.assertFalse(inst.hub.res_open)
        self.assertIsNone(inst.hub.open_now)
        for k in (2, 3):                                                             # still blocked on the next 2 closes
            await self.tick(inst, snap_of(t_(k), dir_e=-1, dir_f=-1, close=98.0))
            self.assertEqual(len(c.market_orders), 1, k)
            self.assertEqual(inst.state.status, "IDLE", k)
            self.assertIn("COOLDOWN", inst.state.last_decision)
        await self.tick(inst, snap_of(t_(4), dir_e=-1, dir_f=-1, close=98.0))        # the wait is over
        self.assertEqual(len(c.market_orders), 2)
        self.assertEqual(inst.state.direction, "SHORT")

    async def test_a_stop_out_between_candles_skips_the_next_closes(self):
        inst = self.make(plan_of(cooldown={"on": True, "candles": 1}))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))        # LONG, Max Loss stop rests
        inst.client.position = None                                                  # the stop was hit inside candle 1
        inst.client.open_algo_orders.clear()
        inst.client.mark_price = 97.0
        await self.tick(inst, snap_of(t_(1), dir_e=1, dir_f=1, close=97.0))          # candle 1 closes: the stop is found
        self.assertEqual(inst.state.status, "IDLE")
        # the decisions: candle 1 is the first close after the loss -> skipped
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(inst.hub.loss_streak, 1)
        await self.tick(inst, snap_of(t_(2), dir_e=1, dir_f=1, close=97.0))          # allowed again
        self.assertEqual(len(inst.client.market_orders), 2)

    async def test_a_winning_trade_starts_no_cooldown(self):
        inst = self.make(plan_of(cooldown={"on": True, "candles": 3}))
        await self.long_then_flip_at(inst, 1, 102.0)                                 # profit
        self.assertEqual(len(inst.client.market_orders), 2)                          # the flip's opposite entry went ahead
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertEqual(inst.hub.loss_streak, 0)
        self.assertIsNone(inst.hub.cooldown_through)

    async def test_without_the_cooldown_a_loss_changes_nothing(self):
        inst = self.make(plan_of())
        await self.long_then_flip_at(inst, 1, 98.0)
        self.assertEqual(len(inst.client.market_orders), 2)
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertIsNone(inst.hub.cooldown_through)
        self.assertEqual(inst.hub.loss_streak, 1)          # counted (cheap), but nothing is blocked

    async def test_cooldown_off_in_the_plan_blocks_nothing_even_with_an_old_block_in_the_memory(self):
        inst = self.make(plan_of(cooldown={"on": False}))
        inst.hub.cooldown_through = t_(1) + 50 * L1H       # left over from an earlier plan
        await self.long_then_flip_at(inst, 1, 102.0)
        self.assertEqual(len(inst.client.market_orders), 2)

    async def test_losses_in_a_row(self):
        inst = self.make(plan_of(cooldown={"on": True, "candles": 2, "after_losses": 2}))
        await self.long_then_flip_at(inst, 1, 98.0)                                  # loss 1: no cooldown, SHORT opens
        self.assertEqual(inst.state.direction, "SHORT")
        self.assertEqual(inst.hub.loss_streak, 1)
        self.assertIsNone(inst.hub.cooldown_through)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(2), dir_e=1, dir_f=1, close=100.0))        # the short loses too (98 -> 100): flip to LONG
        self.assertEqual(inst.hub.loss_streak, 2)
        self.assertEqual(inst.hub.cooldown_through, t_(2) + 2 * L1H)
        self.assertEqual(inst.state.status, "IDLE")                                  # the new LONG is blocked
        self.assertIn("COOLDOWN", inst.state.last_decision)

    async def test_the_open_trade_is_never_touched_and_a_flip_still_closes_it(self):
        inst = self.make(plan_of(cooldown={"on": True, "candles": 5}))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))        # LONG
        inst.hub.cooldown_through = t_(0) + 50 * L1H                                 # a cooldown is running (left over)
        await self.tick(inst, snap_of(t_(1), dir_e=1, dir_f=1, close=101.0))        # nothing happens to the open trade
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(len(inst.client.close_orders), 0)
        inst.client.mark_price = 99.0
        await self.tick(inst, snap_of(t_(2), dir_e=-1, dir_f=-1, close=99.0))       # its own flip rule closes it (a loss)
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(inst.state.status, "IDLE")                                  # and the new entry waits
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_it_survives_a_restart(self):
        inst = self.make(plan_of(cooldown={"on": True, "candles": 2}))
        await self.long_then_flip_at(inst, 1, 98.0)
        again = hub_state.load(inst.platform, inst.account_id, inst.symbol)
        self.assertEqual(again.cooldown_through, t_(1) + 2 * L1H)
        self.assertEqual(again.loss_streak, 1)

    async def test_fixed_base_amount_is_not_changed_by_the_result_tracking(self):
        sizing = {"method": H.SIZE_FIXED, "base_amount": 100.0, "leverage": 10, "stop_on": False}
        inst = self.make(plan_of(sizing=sizing, cooldown={"on": True, "candles": 1}))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(inst.hub.trade_amount, 100.0)
        self.assertTrue(inst.hub.res_open)
        inst.client.mark_price = 102.0
        await self.tick(inst, snap_of(t_(1), dir_e=-1, dir_f=-1, close=102.0))
        self.assertAlmostEqual(inst.hub.last_total, 120.0, places=6)               # exactly as before
        self.assertEqual(inst.hub.loss_streak, 0)

    async def _tp_then_close(self, tp_pnl):
        """A take-profit fill (net tp_pnl) first, then the rest of the trade closes at a LOSS on the flip."""
        inst = self.make(plan_of(cooldown={"on": True, "candles": 2}))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        qty = inst.state.qty
        await inst._record_ledger("LONG", qty / 2, 110.0, tp_pnl, "TP1", include_entry_commission=False)
        self.assertAlmostEqual(inst.hub.res_pnl, tp_pnl, places=6)
        inst.client.position = {"positionAmt": str(qty / 2), "entryPrice": "100.0"}
        inst.state.qty = qty / 2
        inst.client.mark_price = 98.0                                      # the rest loses 2 x qty / 2 = qty
        await self.tick(inst, snap_of(t_(1), dir_e=-1, dir_f=-1, close=98.0))
        return inst, qty

    async def test_every_fill_counts_in_the_result_a_small_win_then_a_bigger_loss_is_a_loss(self):
        inst, qty = await self._tp_then_close(0.5)
        self.assertGreater(qty, 0.5)                                       # the final loss (= qty) is bigger than the win
        self.assertEqual(inst.hub.loss_streak, 1)
        self.assertEqual(inst.hub.cooldown_through, t_(1) + 2 * L1H)
        self.assertEqual(len(inst.client.market_orders), 1)                # the opposite entry is blocked

    async def test_every_fill_counts_in_the_result_a_big_win_then_a_smaller_loss_is_a_win(self):
        inst, qty = await self._tp_then_close(qty_win := 10_000.0)
        self.assertEqual(inst.hub.loss_streak, 0)                          # the last close alone lost, the trade won
        self.assertIsNone(inst.hub.cooldown_through)
        self.assertEqual(len(inst.client.market_orders), 2)                # the flip's opposite entry went ahead


class TestLossLimitLive(LiveBase):
    async def test_strategy_limit_stops_a_new_entry_and_says_why(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily": 15.0}))
        self.add(inst, -20.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 0)
        self.assertEqual(inst.state.status, "IDLE")
        self.assertIn("LOSS LIMIT", inst.state.last_decision)
        self.assertIsNone(inst.hub.open_now)
        # the next candle is judged fresh: still over the limit, still no entry
        await self.tick(inst, snap_of(t_(1), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 0)

    async def test_under_the_limit_the_entry_goes_ahead(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily": 25.0}))
        self.add(inst, -20.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_account_and_entire_limits_stop_a_strategy_that_has_no_limit_of_its_own(self):
        inst = self.make(plan_of())
        self.add(inst, -20.0)
        LL.save_settings(None, {inst.account_id: {"daily": 15.0, "weekly": None}})
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 0)
        self.assertIn("this account", inst.state.last_decision)
        LL.save_settings(None, {inst.account_id: {"daily": None, "weekly": None}})
        LL.save_settings({"daily": 15.0, "weekly": None}, None)
        await self.tick(inst, snap_of(t_(1), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 0)
        self.assertIn("all accounts together", inst.state.last_decision)
        LL.save_settings({"daily": None, "weekly": None}, None)
        await self.tick(inst, snap_of(t_(2), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_the_open_trade_is_never_touched_by_a_reached_limit(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily": 5.0}))
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))        # LONG opens (no loss yet)
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.add(inst, -50.0)                                                        # the limit is now reached
        n_stops = len(inst.client.stop_orders)
        await self.tick(inst, snap_of(t_(1), dir_e=1, dir_f=1, close=100.5))
        await self.tick(inst, snap_of(t_(2), dir_e=1, dir_f=1, close=101.0))
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(len(inst.client.close_orders), 0)
        self.assertEqual(len(inst.client.stop_orders), n_stops)
        # its own flip closes it as always; only the NEW entry is stopped
        inst.client.mark_price = 99.9
        await self.tick(inst, snap_of(t_(3), dir_e=-1, dir_f=-1, close=99.9))        # a small loss: still over the limit
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertIn("LOSS LIMIT", inst.state.last_decision)

    async def test_one_email_per_limit_per_period(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily": 15.0}))
        self.add(inst, -20.0)
        sent = []

        async def fake_send(subject, body):
            sent.append((subject, body))
        with mock.patch("app.hub_trading.email_notifier.send", side_effect=fake_send):
            for k in range(3):
                await self.tick(inst, snap_of(t_(k), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(sent), 1)
        self.assertIn("Loss Limit", sent[0][0])
        self.assertIn("LOSS LIMIT reached", sent[0][1])

    async def test_a_broken_check_never_stops_trading_by_surprise(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily": 15.0}))
        inst.client.mark_price = 100.0
        with mock.patch.object(LL, "check", side_effect=RuntimeError("boom")):
            await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_a_plan_without_a_loss_limit_never_looks_at_the_ledger_for_the_strategy_level(self):
        inst = self.make(plan_of())
        self.add(inst, -9999.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)


class TestRoutesExist(unittest.TestCase):
    def test_routes(self):
        from app import api
        ends = {r.endpoint.__name__: r.path for r in api.router.routes}
        self.assertTrue(ends["get_loss_limits"].endswith("/loss-limits"))
        self.assertTrue(ends["save_loss_limits"].endswith("/loss-limits"))


if __name__ == "__main__":
    unittest.main()
