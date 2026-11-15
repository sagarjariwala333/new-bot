"""Pairs table card and per-pair Close trade (owner 2026-11-08). Arbitrary test numbers only.
Close trade closes only THIS pair's open trade and leaves the pair running (not Stop, not STOP ALL)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_ladder import LadderBase, sl, step, PRICE  # noqa: E402
from app import strategy_hub as H  # noqa: E402


class TestCardPure(unittest.TestCase):
    def test_step_next_matches_step_level(self):
        # begins at a price move: nothing before `begin`, then every gap
        self.assertEqual(H.step_next("LONG", 100, 0.5, 100.2, begin=1.0, first=100.0), (101.0, 100.0))
        self.assertEqual(H.step_next("LONG", 100, 0.5, 101.7, begin=1.0, first=100.0), (102.0, 101.0))
        self.assertEqual(H.step_level("LONG", 100, 99, 0.5, 102.0, begin=1.0, first=100.0), 101.0)
        self.assertEqual(H.step_next("SHORT", 100, 0.5, 99.0, begin=1.0, first=100.0), (98.5, 99.5))
        self.assertEqual(H.step_level("SHORT", 100, 101, 0.5, 98.5, begin=1.0, first=100.0), 99.5)
        # own start: first checkpoint after one gap
        self.assertEqual(H.step_next("LONG", 100, 2, 100.5, start=98), (102, 100))
        self.assertEqual(H.step_level("LONG", 100, 98, 2, 102, ), 100)
        self.assertIsNone(H.step_next("LONG", 100, 0, 100.5, start=98))


class TestCardLive(LadderBase):
    async def test_live_numbers_and_card_for_an_open_long(self):
        inst = self.make_l(sl=sl(PRICE, 3.0), trail=step(gap=0.5))
        await self.open_long_at(inst, price=100.0)
        inst.mark_feed.last_mark_price = 101.0
        live = inst._live_numbers()
        self.assertEqual(live["last_price"], 101.0)
        self.assertAlmostEqual(live["pnl"], 1.0 * inst.state.qty)
        self.assertIsNotNone(live["pnl_pct_margin"])
        card = inst.to_status_dict()["hub"]["card"]
        self.assertEqual(card["stop_name"], "Stop Loss")
        self.assertAlmostEqual(card["stop_price"], 97.0)
        self.assertTrue(card["plan_lines"])
        self.assertEqual(inst.to_status_dict()["live"]["last_price"], 101.0)

    async def test_flat_pair_has_price_but_no_pnl(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        inst.mark_feed.last_mark_price = 55.0
        live = inst._live_numbers()
        self.assertEqual(live["last_price"], 55.0)
        self.assertIsNone(live["pnl"])

    async def test_stale_feed_gives_no_price(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        inst.mark_feed.last_mark_price = 55.0
        inst.mark_feed.last_message_at = 0
        self.assertIsNone(inst._live_numbers()["last_price"])


class TestCloseTradeNow(LadderBase):
    async def test_closes_only_this_trade_and_the_pair_keeps_running(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst, price=100.0)
        r = await inst.close_trade_now()
        self.assertTrue(r["closed"])
        self.assertEqual(inst.state.status, "IDLE")
        self.assertIsNone(inst.state.direction)
        self.assertFalse(inst._stop_requested)            # the pair was NOT stopped
        self.assertEqual(inst.hub.reentry_block, 1)       # the closed side waits for a fresh signal

    async def test_flat_pair_says_nothing_to_close(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        r = await inst.close_trade_now()
        self.assertFalse(r["closed"])
        self.assertIn("No open trade", r["message"])


if __name__ == "__main__":
    unittest.main()


class TestCardExtras(LadderBase):
    async def test_next_close_is_in_the_future_and_on_the_candle_grid(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        ms = 3_600_000 if inst.pc.timeframe.lower() == "1h" else None
        nxt = inst._next_close_ms()
        if ms:
            self.assertGreater(nxt, __import__("time").time() * 1000)
            self.assertEqual(nxt % ms, 0)
            self.assertLessEqual(nxt - __import__("time").time() * 1000, ms)

    async def test_today_result_counts_only_todays_closed_trades(self):
        import time
        from app import ledger
        inst = self.make_l(sl=sl(PRICE, 3.0))
        now = time.time()
        recs = [ledger.TradeRecord(
            account_id=inst.account_id, symbol=inst.symbol, direction="LONG", qty=1, entry_price=100,
            exit_price=101, pnl=pnl, reason="SL", opened_at=closed - 60, closed_at=closed)
            for pnl, closed in ((1.5, now), (-0.5, now), (9.0, now - 3 * 86400))]
        with __import__("unittest.mock").mock.patch.object(ledger, "list_trades", return_value=recs):
            t = inst._today_result()
        self.assertEqual(t["trades"], 2)
        self.assertAlmostEqual(t["pnl"], 1.0)

    async def test_funds_enough_and_not_enough(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        inst.client.balance = 10000.0
        await inst.refresh_funds(max_age=0)
        self.assertIsNotNone(inst._funds_card())
        self.assertTrue(inst._funds_card()["ok"])
        inst._funds = {"available": 0.5, "at": __import__("time").time()}
        inst._plan["sizing"]["method"] = H.SIZE_FIXED
        inst._plan["sizing"]["base_amount"] = 50.0
        inst._plan["sizing"].setdefault("counter", {})["base_amount"] = 50.0
        fc = inst._funds_card()
        self.assertEqual(fc["need"], 50.0)
        self.assertFalse(fc["ok"])
        why = inst._why_no_trade(None, fc)
        self.assertTrue(any("Not enough funds" in w for w in why))

    async def test_why_lists_a_blocked_side_and_a_stopped_pair(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        inst.hub.reentry_block = 1
        self.assertTrue(any("LONG is blocked" in w for w in inst._why_no_trade(None, None)))
        inst._stop_requested = True
        self.assertEqual(inst._why_no_trade(None, None), ["This pair is stopped. Press Start."])

    async def test_open_trade_card_has_no_why_or_funds(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst, price=100.0)
        card = inst.to_status_dict()["hub"]["card"]
        self.assertEqual(card["why"], [])
        self.assertIsNone(card["funds"])


class TestAuditFixes(LadderBase):
    async def test_plan_lines_show_every_feature_and_an_off_line(self):
        from tests._hub_plan import make_plan
        plan = H.complete_plan(make_plan(ind_sup=dict(entry=True, flip=True), ind_rsi=dict(entry=True)))
        plan["tp"]["on"] = True
        plan["be"].update(on=True, after_tp=True)
        plan["hold"].update(on=True, adx_level=22.0)
        plan["cooldown"] = {"on": True, "candles": 3, "after_losses": 2}
        plan["allow_reentry"] = True
        plan["entry_wait"] = 2
        plan["wait_first_reversal"] = True
        plan["exit_when"]["on"] = True
        lines = dict(tuple(l) for l in H.plan_lines(plan))
        for k in ("Chart", "ENTRY", "FLIP", "Flip rules", "Entry wait", "Fresh Reversal on first trade", "Mid-Trend Re-entry",
                  "Size", "Max Loss", "Stop Loss", "Take profit", "Break-Even", "Hold rule", "Exit when", "Cooldown", "OFF"):
            self.assertIn(k, lines, k)
        self.assertIn("22", lines["Hold rule"])
        self.assertIn("2 candle", lines["Entry wait"])
        self.assertEqual(lines["Mid-Trend Re-entry"], "allowed")
        self.assertIn("RSI 14", lines["ENTRY"])
        self.assertNotIn("Take profit", lines["OFF"])
        self.assertIn("Trailing Stop", lines["OFF"])

    async def test_last_decision_and_reentry_flag(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        inst.state.last_decision = "COOLDOWN after a loss - no new entry on this candle close"
        self.assertTrue(any("Bot's last decision: COOLDOWN" in w for w in inst._why_no_trade(None, None)))
        inst.state.last_decision = "NO SIGNAL"
        self.assertFalse(any("last decision" in w for w in inst._why_no_trade(None, None)))
        self.assertTrue(inst.to_status_dict()["hub"]["card"]["reentry_blocked_after_close"])
        inst._plan["allow_reentry"] = True
        self.assertFalse(inst.to_status_dict()["hub"]["card"]["reentry_blocked_after_close"])

    async def test_status_is_strict_json_even_with_bad_numbers(self):
        import json
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst, price=100.0)
        for bad in (float("nan"), float("inf")):
            inst.mark_feed.last_mark_price = bad
            d = inst.to_status_dict()
            json.dumps(d, allow_nan=False)
        inst.mark_feed.last_mark_price = 101.0
        inst.hub.ladder["step"] = {"gap": 0.5, "begin": 1.0, "first": float("nan")}
        inst.hub.ladder["best"] = 101.5
        card = inst._hub_card()                  # the card must stay clean even if the ladder holds a bad number
        json.dumps(card, allow_nan=False)
        self.assertIsNone(card["next_stop"])

    async def test_server_clock_is_sent(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        ms = inst.to_status_dict()["live"]["server_ms"]
        self.assertLess(abs(ms - __import__("time").time() * 1000), 5000)

    async def test_today_is_read_from_the_ledger_only_when_it_changed(self):
        import time
        from unittest import mock
        from app import ledger
        inst = self.make_l(sl=sl(PRICE, 3.0))

        class FakeStat:
            st_mtime_ns, st_size = 1, 1
        with mock.patch.object(ledger, "list_trades", return_value=[]) as lt, \
                mock.patch.object(type(ledger._path("a", "b")), "stat", return_value=FakeStat()):
            inst._today_result()
            inst._today_result()
            self.assertEqual(lt.call_count, 1)             # second call comes from the cache
            FakeStat.st_size = 2                           # the ledger grew
            inst._today_result()
            self.assertEqual(lt.call_count, 2)

    async def test_stopped_pair_with_a_trade_gets_a_clear_message(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst, price=100.0)
        inst.state.status = "STOPPED"
        r = await inst.close_trade_now()
        self.assertFalse(r["closed"])
        self.assertIn("press Start", r["message"])
