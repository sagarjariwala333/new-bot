"""Loss limit in % (owner 2026-11-02: "Have both so user can choose $ or %", base = "A": the account balance at the start
of the day / week).

  * every limit (this strategy / this account / all accounts together, daily / weekly) is a USDT amount OR a % of the
    account balance at the start of that period; both together = whichever is reached first
  * the % is worked out once from the SAVED start balance, so it stays the same USDT number all day
  * the start balance is saved the first time the bot sees the account in the period, minus what the ledger shows as
    already closed in that period
  * a % limit whose base is not known cannot be checked; a USDT limit still can
Arbitrary test numbers only."""
import math
import os
import sys
import time
import unittest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_loss_cooldown as B  # noqa: E402  (stubs + helpers)
from tests.test_hub_trading import snap_of  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import loss_limits as LL  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402

SGT = timezone(timedelta(hours=8))
t_ = B.t_
plan_of = B.plan_of


class TestPlanCheck(unittest.TestCase):
    def ll(self, **kw):
        return make_plan(ind_ma1={"entry": True}, loss_limit={"on": True, **kw})

    def test_a_percent_alone_is_enough(self):
        H.validate_plan(self.ll(daily_pct=5.0))
        H.validate_plan(self.ll(weekly_pct=10.0))
        H.validate_plan(self.ll(daily_pct=100.0))
        H.validate_plan(self.ll(daily=20.0, daily_pct=5.0, weekly=50.0, weekly_pct=10.0))

    def test_none_of_the_four_is_refused(self):
        with self.assertRaises(H.PlanError):
            H.validate_plan(self.ll())

    def test_wrong_percents_are_refused(self):
        for bad in (0, -1, 0.001, 100.01, 101, float("nan"), float("inf"), True, "5"):
            for key in ("daily_pct", "weekly_pct"):
                with self.subTest(key=key, bad=bad), self.assertRaises(H.PlanError):
                    H.validate_plan(self.ll(**{key: bad}))

    def test_the_usdt_amount_rules_are_unchanged(self):
        H.validate_plan(self.ll(daily=20.0))
        for bad in (0, -1, 0.001, float("nan"), True, "5"):
            with self.subTest(bad=bad), self.assertRaises(H.PlanError):
                H.validate_plan(self.ll(daily=bad))

    def test_off_needs_nothing_and_a_plan_without_it_is_unchanged(self):
        H.validate_plan(make_plan(ind_ma1={"entry": True}, loss_limit={"on": False}))
        p = make_plan(ind_ma1={"entry": True})
        self.assertNotIn("loss_limit", H.complete_plan(p))


class TestSettingsFile(B.LimitsFile):
    def test_percents_are_checked_and_saved(self):
        for bad in (0, -1, 0.001, 100.5, float("nan"), True, "5"):
            with self.assertRaises(LL.LossLimitError, msg=repr(bad)):
                LL.save_settings({"daily_pct": bad}, None)
            with self.assertRaises(LL.LossLimitError, msg=repr(bad)):
                LL.save_settings(None, {"a1": {"weekly_pct": bad}})
        self.assertEqual(LL.load()["entire"], B.NONE4)                 # nothing half-saved
        LL.save_settings({"daily": 5, "daily_pct": 2.5}, {"a1": {"weekly_pct": 10}, "a2": {"daily_pct": None}})
        d = LL.load()
        self.assertEqual(d["entire"], dict(B.NONE4, daily=5.0, daily_pct=2.5))
        self.assertEqual(d["accounts"]["a1"], dict(B.NONE4, weekly_pct=10.0))
        self.assertNotIn("a2", d["accounts"])                         # all four empty = the account entry goes away
        LL.save_settings(None, {"a1": {"weekly_pct": None}})
        self.assertNotIn("a1", LL.load()["accounts"])

    def test_100_percent_is_the_top(self):
        LL.save_settings({"daily_pct": 100}, None)
        self.assertEqual(LL.load()["entire"]["daily_pct"], 100.0)

    def test_an_older_file_without_the_new_keys_still_loads(self):
        (self.dir / "loss_limits.json").write_text('{"entire": {"daily": 5.0, "weekly": null}, '
                                                   '"accounts": {"a1": {"daily": 3.0, "weekly": null}}, "sent": {}}')
        d = LL.load()
        self.assertEqual(d["entire"], dict(B.NONE4, daily=5.0))
        self.assertEqual(d["accounts"]["a1"], dict(B.NONE4, daily=3.0))
        self.assertEqual(d["base"], {})

    def test_the_saved_balances_survive_other_saves(self):
        now = time.time()
        LL.save_settings(None, {"a1": {"daily_pct": 5}})
        LL.record_balance("a1", 1000.0, now)
        LL.save_settings({"weekly": 9}, None)                          # a settings save keeps the balances
        LL.first_time("some-key", now)                                  # and so does an email mark
        base = LL.load()["base"]
        self.assertEqual(sorted(base), sorted([LL._pkey(now, "daily"), LL._pkey(now, "weekly")]))
        self.assertEqual(base[LL._pkey(now, "daily")], {"a1": 1000.0})

    def test_a_broken_base_entry_is_dropped_not_trusted(self):
        (self.dir / "loss_limits.json").write_text('{"base": {"d:1": {"a1": "x", "a2": 5, "a3": true, "a4": null}, "w:1": 7}}')
        self.assertEqual(LL.load()["base"], {"d:1": {"a2": 5.0}})


class TestRecordBalance(B.LimitsFile):
    NOW = datetime(2026, 11, 5, 15, 0, tzinfo=SGT).timestamp()          # Thursday 15:00 SGT
    TODAY = datetime(2026, 11, 5, 9, 0, tzinfo=SGT).timestamp()
    YESTERDAY = datetime(2026, 11, 4, 20, 0, tzinfo=SGT).timestamp()

    def test_day_and_week_are_saved_and_the_first_sight_wins(self):
        LL.record_balance("a1", 1000.0, self.NOW)
        LL.record_balance("a1", 700.0, self.NOW + 60)                   # a later sight changes nothing
        base = LL.load()["base"]
        self.assertEqual(base[LL._pkey(self.NOW, "daily")], {"a1": 1000.0})
        self.assertEqual(base[LL._pkey(self.NOW, "weekly")], {"a1": 1000.0})

    def test_what_the_ledger_shows_as_closed_since_the_start_is_taken_back_off(self):
        self.add("a1", "BTCUSDT", -30.0, self.TODAY)                    # today
        self.add("a1", "BTCUSDT", -10.0, self.YESTERDAY)                # earlier this week
        self.add("a2", "BTCUSDT", -999.0, self.TODAY)                   # another account: not counted
        LL.record_balance("a1", 1000.0, self.NOW)
        base = LL.load()["base"]
        self.assertAlmostEqual(base[LL._pkey(self.NOW, "daily")]["a1"], 1030.0)     # balance now 1000 = 1030 - 30
        self.assertAlmostEqual(base[LL._pkey(self.NOW, "weekly")]["a1"], 1040.0)    # the week lost 40

    def test_a_new_day_is_a_new_sight(self):
        LL.record_balance("a1", 1000.0, self.YESTERDAY)
        LL.record_balance("a1", 900.0, self.NOW)
        base = LL.load()["base"]
        self.assertEqual(base[LL._pkey(self.NOW, "daily")], {"a1": 900.0})
        self.assertEqual(base[LL._pkey(self.YESTERDAY, "daily")], {"a1": 1000.0})
        self.assertEqual(base[LL._pkey(self.NOW, "weekly")], {"a1": 1000.0})        # same week: the first sight stays

    def test_bad_balances_are_ignored(self):
        for bad in (None, 0, -5, float("nan"), float("inf"), True, "100"):
            LL.record_balance("a1", bad, self.NOW)
        self.assertEqual(LL.load()["base"], {})

    def test_old_periods_are_dropped(self):
        for k in range(6):
            LL.record_balance("a1", 1000.0, self.NOW - k * 86400)
        days = [p for p in LL.load()["base"] if p.startswith("d:")]
        self.assertEqual(len(days), LL.KEEP_BASE_PERIODS)
        self.assertIn(LL._pkey(self.NOW, "daily"), days)               # the newest are kept

    def test_unreadable_file_is_never_overwritten(self):
        (self.dir / "loss_limits.json").write_text("{ not json")
        with self.assertRaises(LL.LossLimitError):
            LL.record_balance("a1", 1000.0, self.NOW)
        self.assertEqual((self.dir / "loss_limits.json").read_text(), "{ not json")


class TestNeedsAndDue(B.LimitsFile):
    NOW = datetime(2026, 11, 5, 15, 0, tzinfo=SGT).timestamp()

    def test_nothing_in_percent_nothing_needed(self):
        self.assertFalse(LL.needs_balance("a1", None))
        self.assertFalse(LL.needs_balance("a1", {"loss_limit": {"on": True, "daily": 5}}))
        LL.save_settings({"daily": 5}, {"a1": {"weekly": 5}})
        self.assertFalse(LL.needs_balance("a1", None))
        self.assertFalse(LL.balance_due("a1", None, self.NOW))

    def test_each_level_asks_for_the_balance(self):
        self.assertTrue(LL.needs_balance("a1", {"loss_limit": {"on": True, "daily_pct": 5}}))
        self.assertFalse(LL.needs_balance("a1", {"loss_limit": {"on": False, "daily_pct": 5}}))
        LL.save_settings(None, {"a1": {"weekly_pct": 5}})
        self.assertTrue(LL.needs_balance("a1", None))
        self.assertFalse(LL.needs_balance("a2", None))                  # another account has no % limit
        LL.save_settings({"daily_pct": 1}, None)
        self.assertTrue(LL.needs_balance("a2", None))                   # all accounts together: everyone

    def test_due_until_saved_then_due_again_the_next_day(self):
        LL.save_settings({"daily_pct": 1}, None)
        self.assertTrue(LL.balance_due("a1", None, self.NOW))
        LL.record_balance("a1", 1000.0, self.NOW)
        self.assertFalse(LL.balance_due("a1", None, self.NOW + 3600))
        self.assertTrue(LL.balance_due("a1", None, self.NOW + 86400))   # tomorrow: a new day
        self.assertTrue(LL.balance_due("a2", None, self.NOW))

    def test_unreadable_file_is_not_due(self):
        (self.dir / "loss_limits.json").write_text("{ not json")
        self.assertFalse(LL.needs_balance("a1", None))
        self.assertFalse(LL.balance_due("a1", None, self.NOW))
        self.assertTrue(LL.needs_balance("a1", {"loss_limit": {"on": True, "daily_pct": 5}}))


class TestCheckInPercent(B.LimitsFile):
    NOW = datetime(2026, 11, 5, 15, 0, tzinfo=SGT).timestamp()
    TODAY = datetime(2026, 11, 5, 9, 0, tzinfo=SGT).timestamp()
    YESTERDAY = datetime(2026, 11, 4, 20, 0, tzinfo=SGT).timestamp()

    def plan(self, **ll):
        return {"loss_limit": {"on": True, **ll}}

    def test_strategy_percent_of_the_saved_balance(self):
        LL.record_balance("a1", 1000.0, self.TODAY)
        self.add("a1", "BTCUSDT", -49.0, self.TODAY)
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0), self.NOW))
        self.add("a1", "BTCUSDT", -1.0, self.TODAY)
        hit = LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0), self.NOW)
        self.assertIsNotNone(hit)
        self.assertEqual((hit["scope"], hit["kind"], hit["unit"]), ("strategy", "daily", "pct"))
        self.assertAlmostEqual(hit["limit"], 50.0)
        self.assertAlmostEqual(hit["lost"], 50.0)
        self.assertIn("LOSS LIMIT reached", hit["text"])
        self.assertIn("5 % of the 1,000.00 USDT balance at the start", hit["text"])
        self.assertIn("50.00 USDT limit", hit["text"])
        self.assertIn("Open trades are not touched", hit["text"])

    def test_the_limit_stays_the_same_number_all_day(self):
        # the balance fell to 900 after the start. Base A: still 5 % of 1000 = 50, not 5 % of 900 = 45
        LL.record_balance("a1", 1000.0, self.TODAY)
        self.add("a1", "BTCUSDT", -46.0, self.TODAY)
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0), self.NOW, equity=954.0))
        self.add("a1", "BTCUSDT", -4.0, self.TODAY)
        self.assertIsNotNone(LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0), self.NOW, equity=900.0))

    def test_weekly_percent_uses_the_start_of_the_week(self):
        LL.record_balance("a1", 1000.0, self.YESTERDAY)                 # Wed: first sight of the week (and of Wed)
        LL.record_balance("a1", 800.0, self.TODAY)                      # Thu: the day starts at 800
        self.add("a1", "BTCUSDT", -90.0, self.YESTERDAY)
        self.add("a1", "BTCUSDT", -9.0, self.TODAY)
        p = self.plan(weekly_pct=10.0)                                  # 10 % of 1000 = 100 for the week
        self.assertIsNone(LL.check("a1", "BTCUSDT", p, self.NOW))
        self.add("a1", "BTCUSDT", -1.0, self.TODAY)
        hit = LL.check("a1", "BTCUSDT", p, self.NOW)
        self.assertEqual((hit["kind"], hit["unit"]), ("weekly", "pct"))
        self.assertAlmostEqual(hit["limit"], 100.0)
        p = self.plan(daily_pct=10.0)                                   # a day: 10 % of the 800 saved for Thursday = 80
        self.assertIsNone(LL.check("a1", "BTCUSDT", p, self.NOW))       # only 10 lost today

    def test_an_unknown_base_cannot_be_checked_but_a_usdt_limit_still_is(self):
        self.add("a1", "BTCUSDT", -500.0, self.TODAY)
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0), self.NOW))
        hit = LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0, daily=20.0), self.NOW)
        self.assertEqual(hit["unit"], "usdt")

    def test_the_balance_handed_in_is_saved_when_it_is_missing(self):
        self.add("a1", "BTCUSDT", -30.0, self.TODAY)
        # balance now 970 and 30 closed today = it started at 1000, so 5 % = 50
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0), self.NOW, equity=970.0))
        self.assertAlmostEqual(LL.load()["base"][LL._pkey(self.NOW, "daily")]["a1"], 1000.0)
        self.add("a1", "BTCUSDT", -20.0, self.TODAY)
        hit = LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0), self.NOW, equity=950.0)
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit["limit"], 50.0)

    def test_nothing_is_saved_when_no_percent_applies(self):
        LL.check("a1", "BTCUSDT", self.plan(daily=5.0), self.NOW, equity=970.0)
        self.assertEqual(LL.load()["base"], {})

    def test_account_level_percent_counts_all_pairs_of_the_account(self):
        LL.save_settings(None, {"a1": {"daily_pct": 5}})
        LL.record_balance("a1", 1000.0, self.TODAY)
        self.add("a1", "BTCUSDT", -30.0, self.TODAY)
        self.add("a1", "ETHUSDT", -20.0, self.TODAY)
        self.add("a2", "BTCUSDT", -500.0, self.TODAY)
        hit = LL.check("a1", "SOLUSDT", {}, self.NOW)
        self.assertEqual((hit["scope"], hit["unit"]), ("account", "pct"))
        self.assertIn("this account", hit["text"])
        self.assertIsNone(LL.check("a2", "BTCUSDT", {}, self.NOW))      # a2 has no limit of its own

    def test_entire_level_percent_is_of_the_saved_balances_added_up(self):
        LL.save_settings({"daily_pct": 5}, None)
        LL.record_balance("a1", 600.0, self.TODAY)
        LL.record_balance("a2", 400.0, self.TODAY)                      # together 1000: 5 % = 50
        self.add("a1", "BTCUSDT", -30.0, self.TODAY)
        self.add("a2", "BTCUSDT", -19.0, self.TODAY)
        self.assertIsNone(LL.check("a1", "BTCUSDT", {}, self.NOW))
        self.add("a2", "BTCUSDT", -1.0, self.TODAY)
        hit = LL.check("a1", "BTCUSDT", {}, self.NOW)
        self.assertEqual((hit["scope"], hit["unit"]), ("entire", "pct"))
        self.assertAlmostEqual(hit["limit"], 50.0)
        self.assertIn("all accounts together", hit["text"])
        self.assertIsNotNone(LL.check("a2", "ETHUSDT", {}, self.NOW))   # every account is stopped

    def test_usdt_and_percent_together_the_first_reached_counts(self):
        LL.record_balance("a1", 1000.0, self.TODAY)
        self.add("a1", "BTCUSDT", -12.0, self.TODAY)
        hit = LL.check("a1", "BTCUSDT", self.plan(daily=10.0, daily_pct=5.0), self.NOW)
        self.assertEqual(hit["unit"], "usdt")                           # 12 >= 10 USDT, 5 % = 50 not reached
        self.add("a1", "BTCUSDT", -48.0, self.TODAY)
        hit = LL.check("a1", "BTCUSDT", self.plan(daily=100.0, daily_pct=5.0), self.NOW)
        self.assertEqual(hit["unit"], "pct")                            # 60 >= 50 (5 %), 100 USDT not reached

    def test_a_day_limit_and_a_week_limit_are_independent(self):
        LL.record_balance("a1", 1000.0, self.TODAY)
        self.add("a1", "BTCUSDT", -60.0, self.YESTERDAY)
        p = self.plan(daily_pct=5.0, weekly_pct=5.0)
        hit = LL.check("a1", "BTCUSDT", p, self.NOW)
        self.assertEqual(hit["kind"], "weekly")                         # yesterday's loss is in the week, not in the day

    def test_the_emails_are_once_per_limit_per_period_and_different_for_usdt_and_percent(self):
        LL.record_balance("a1", 1000.0, self.TODAY)
        self.add("a1", "BTCUSDT", -60.0, self.TODAY)
        k_pct = LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0), self.NOW)["key"]
        k_usd = LL.check("a1", "BTCUSDT", self.plan(daily=10.0), self.NOW)["key"]
        self.assertNotEqual(k_pct, k_usd)
        self.assertEqual(k_pct, LL.check("a1", "BTCUSDT", self.plan(daily_pct=5.0), self.NOW + 60)["key"])
        self.assertTrue(LL.first_time(k_pct, self.NOW))
        self.assertFalse(LL.first_time(k_pct, self.NOW + 60))
        self.assertTrue(LL.first_time(k_usd, self.NOW))

    def test_the_old_usdt_hit_has_the_old_text(self):
        self.add("a1", "BTCUSDT", -30.0, self.TODAY)
        hit = LL.check("a1", "BTCUSDT", self.plan(daily=10.0), self.NOW)
        self.assertEqual(hit["unit"], "usdt")
        self.assertNotIn("%", hit["text"])
        self.assertEqual(hit["key"].split(":")[3], "daily")

    def test_a_base_of_zero_or_less_is_not_used(self):
        self.add("a1", "BTCUSDT", -2000.0, self.TODAY)
        LL.record_balance("a1", 1000.0, self.TODAY)                     # 1000 now minus (-2000 closed) = 3000: fine
        self.assertIsNotNone(LL.check("a1", "BTCUSDT", self.plan(daily_pct=50.0), self.NOW))
        self.add("a1", "BTCUSDT", +5000.0, self.NOW - 60)               # then a win makes the saved base negative
        (self.dir / "loss_limits.json").write_text('{"base": {"%s": {"a1": -5}}}' % LL._pkey(self.NOW, "daily"))
        self.assertIsNone(LL.check("a1", "BTCUSDT", self.plan(daily_pct=50.0), self.NOW))

    def test_status_for_the_dashboard(self):
        LL.save_settings({"daily": 50.0, "weekly_pct": 10.0}, {"a1": {"daily_pct": 5.0}})
        LL.record_balance("a1", 1000.0, self.TODAY)
        LL.record_balance("a2", 500.0, self.TODAY)
        self.add("a1", "BTCUSDT", -12.0, self.TODAY)                    # lost after the balances were saved
        st = LL.status(self.NOW, ["a1", "a2", "a3"])
        a1d = st["accounts"]["a1"]["daily"]
        self.assertEqual((a1d["pct"], a1d["limit"]), (5.0, None))
        self.assertAlmostEqual(a1d["base"], 1000.0)
        self.assertAlmostEqual(a1d["pct_usdt"], 50.0)
        self.assertAlmostEqual(a1d["result"], -12.0)
        self.assertIsNone(st["accounts"]["a3"]["daily"]["base"])
        self.assertIsNone(st["accounts"]["a3"]["daily"]["pct_usdt"])
        self.assertEqual(st["entire"]["daily"]["limit"], 50.0)
        self.assertIsNone(st["entire"]["daily"]["pct_usdt"])
        self.assertEqual(st["entire"]["weekly"]["pct"], 10.0)
        self.assertAlmostEqual(st["entire"]["weekly"]["base"], 1500.0)
        self.assertAlmostEqual(st["entire"]["weekly"]["pct_usdt"], 150.0)


class FakeEquity:
    """get_equity of the fake exchange, counted."""

    def __init__(self, inst, value=1000.0, fail_first=0):
        self.calls = 0
        self.value = value
        self.fail_first = fail_first          # the first N calls fail (the entry itself reads the balance too)

        async def get_equity():
            self.calls += 1
            if self.calls <= self.fail_first:
                raise RuntimeError("exchange down")
            return self.value
        inst.client.get_equity = get_equity


class TestPercentLive(B.LiveBase):
    async def test_strategy_percent_stops_a_new_entry_and_says_why(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily_pct": 5.0}))
        eq = FakeEquity(inst, 950.0)                                    # 1000 at the start, 50 lost today
        self.add(inst, -50.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 0)
        self.assertEqual(inst.state.status, "IDLE")
        self.assertIn("LOSS LIMIT", inst.state.last_decision)
        self.assertIn("5 % of the 1,000.00 USDT", inst.state.last_decision)
        self.assertGreaterEqual(eq.calls, 1)
        self.assertAlmostEqual(LL.load()["base"][LL._pkey(time.time(), "daily")][inst.account_id], 1000.0)

    async def test_under_the_percent_the_entry_goes_ahead(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily_pct": 5.0}))
        FakeEquity(inst, 970.0)
        self.add(inst, -30.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)

    async def test_account_percent_stops_a_strategy_with_no_limit_of_its_own(self):
        inst = self.make(plan_of())
        FakeEquity(inst, 940.0)
        self.add(inst, -60.0)
        LL.save_settings(None, {inst.account_id: {"daily_pct": 5.0}})
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 0)
        self.assertIn("this account", inst.state.last_decision)

    async def test_the_balance_is_not_asked_for_when_no_percent_applies(self):
        # the entry itself reads the balance once; a USDT-only limit must not add a read to that
        inst = self.make(plan_of(loss_limit={"on": True, "daily": 50.0}))
        eq = FakeEquity(inst, 1000.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        await self.tick(inst, snap_of(t_(1), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(eq.calls, 1)

    async def test_no_limit_at_all_asks_for_nothing_extra(self):
        inst = self.make(plan_of())
        eq = FakeEquity(inst, 1000.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        await self.tick(inst, snap_of(t_(1), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(eq.calls, 1)

    async def test_a_percent_limit_does_add_the_reads(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily_pct": 50.0}))
        eq = FakeEquity(inst, 1000.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        await self.tick(inst, snap_of(t_(1), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(eq.calls, 3)              # the note at the start of the first tick, the check, the entry itself

    async def test_the_start_balance_is_saved_by_the_tick_once_per_period(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily_pct": 5.0}))
        eq = FakeEquity(inst, 1000.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=0, dir_f=0, close=100.0))     # no signal: only the balance is saved
        self.assertEqual(eq.calls, 1)
        base = LL.load()["base"]
        self.assertEqual(base[LL._pkey(time.time(), "daily")][inst.account_id], 1000.0)
        self.assertEqual(base[LL._pkey(time.time(), "weekly")][inst.account_id], 1000.0)
        eq.value = 800.0
        await self.tick(inst, snap_of(t_(1), dir_e=0, dir_f=0, close=100.0))
        await self.tick(inst, snap_of(t_(2), dir_e=0, dir_f=0, close=100.0))
        self.assertEqual(eq.calls, 1)                                             # saved: not asked again
        self.assertEqual(LL.load()["base"][LL._pkey(time.time(), "daily")][inst.account_id], 1000.0)
        self.assertEqual(len(inst.client.market_orders), 0)

    async def test_a_balance_that_cannot_be_read_never_stops_the_tick_or_the_trade(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily_pct": 5.0}))
        FakeEquity(inst, fail_first=2)             # the note at the start of the tick and the check both fail
        self.add(inst, -500.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        # the % limit has no base and cannot be checked: the entry goes ahead (and the log says so)
        self.assertEqual(len(inst.client.market_orders), 1)
        self.assertEqual(inst.state.status, "IN_POSITION")

    async def test_a_balance_that_cannot_be_read_still_lets_the_usdt_limit_work(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily": 20.0, "daily_pct": 5.0}))
        FakeEquity(inst, fail_first=99)
        self.add(inst, -30.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 0)
        self.assertIn("LOSS LIMIT", inst.state.last_decision)

    async def test_the_open_trade_is_never_touched_by_a_percent_limit(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily_pct": 1.0}))
        FakeEquity(inst, 1000.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))     # opens
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.add(inst, -500.0)                                                     # far over 1 %
        n_stops = len(inst.client.stop_orders)
        await self.tick(inst, snap_of(t_(1), dir_e=1, dir_f=1, close=100.5))
        await self.tick(inst, snap_of(t_(2), dir_e=1, dir_f=1, close=101.0))
        self.assertEqual(inst.state.status, "IN_POSITION")
        self.assertEqual(len(inst.client.close_orders), 0)
        self.assertEqual(len(inst.client.stop_orders), n_stops)

    async def test_one_email_per_percent_limit_per_period(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily_pct": 5.0}))
        FakeEquity(inst, 940.0)
        self.add(inst, -60.0)
        sent = []

        async def fake_send(subject, body):
            sent.append((subject, body))
        with mock.patch("app.hub_trading.email_notifier.send", side_effect=fake_send):
            for k in range(3):
                await self.tick(inst, snap_of(t_(k), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(sent), 1)
        self.assertIn("% of the", sent[0][1])

    async def test_a_plan_with_only_the_old_keys_behaves_as_before(self):
        inst = self.make(plan_of(loss_limit={"on": True, "daily": 15.0}))
        self.add(inst, -20.0)
        inst.client.mark_price = 100.0
        await self.tick(inst, snap_of(t_(0), dir_e=1, dir_f=1, close=100.0))
        self.assertEqual(len(inst.client.market_orders), 0)
        self.assertEqual(LL.load()["base"], {})


class TestDashboardAndHelp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app", "static", "dashboard.html"), encoding="utf-8") as fh:
            cls.html = fh.read()

    def test_the_strategy_tab_has_both_percent_boxes_with_the_plan_keys(self):
        self.assertIn("path: ['loss_limit', 'daily_pct']", self.html)
        self.assertIn("path: ['loss_limit', 'weekly_pct']", self.html)

    def test_the_boxes_have_no_starting_number_and_help(self):
        for k in ("loss_daily_pct", "loss_weekly_pct"):
            self.assertIn(f'"{k}": [', self.html)
            i = self.html.index("{ id: '%s'" % k)
            line = self.html[i:self.html.index("\n", i)]
            self.assertNotIn("dflt", line)

    def test_the_box_sends_the_four_keys(self):
        self.assertIn("daily_pct: one('daily_pct'), weekly_pct: one('weekly_pct')", self.html)

    def test_the_summary_line_names_the_percent(self):
        self.assertIn("% of the balance a day", self.html)
        self.assertIn("% of the balance a week", self.html)


if __name__ == "__main__":
    unittest.main()
