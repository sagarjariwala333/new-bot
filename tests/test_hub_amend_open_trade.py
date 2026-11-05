"""Strategy Hub "CHANGE AN OPEN TRADE" (owner 2026-11-05). Arbitrary test numbers only.
What may change on an open trade: Trailing Stop, Stop Loss, Max Loss, Break-Even, Take profit, Exit when, Hold ADX level.
Everything else waits for the next trade. Stops: "tighter" (only if it protects more) or "looser" (as given)."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_trading import snap_of, plan_with  # noqa: E402
from tests.test_hub_ladder import LadderBase, sl, step, PRICE, ATR  # noqa: E402
from tests import _test_values as tv  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import hub_api  # noqa: E402


def tp_atr(value=100.0, on=True):
    return {"on": on, "basis": ATR, "atr_length": 14,
            "levels": [{"on": True, "value": value, "pct": 50.0}, {"on": True, "value": value * 3, "pct": 50.0},
                       {"on": False, "value": 500.0, "pct": 0.0}]}


def candle_trail(value=100.0, on=True):
    return {"on": on, "type": H.TRAIL_CANDLE, "value": value, "atr_length": 14}


class TestAmendPure(unittest.TestCase):
    def test_diff_lists_only_the_allowed_groups_and_flags_the_rest(self):
        old = plan_with(sl=sl(PRICE, 3.0))
        new = copy.deepcopy(old)
        new["sl"] = sl(PRICE, 2.0)
        new["hold"]["adx_level"] = 31.0
        new["name"] = "renamed"
        d = H.amend_diff(old, new)
        self.assertEqual(d["now"], ["Stop Loss", "Hold rule ADX level"])
        self.assertFalse(d["waits"])                                       # the name is a label only
        new["entry_wait"] = 3
        new["sizing"]["leverage"] = 7
        d = H.amend_diff(old, new)
        self.assertTrue(d["waits"])
        self.assertNotIn("Max Loss per Trade", d["now"])

    def test_merge_takes_only_the_allowed_settings(self):
        old = plan_with(sl=sl(PRICE, 3.0))
        new = copy.deepcopy(old)
        new["entry_wait"] = 4
        new["sizing"]["leverage"] = 9
        new["sizing"]["stop_pct"] = 2.0
        new["trail"] = candle_trail(50.0)
        new["exit_when"] = {"on": True, "signal": H.EXIT_BOTH, "after": H.EXIT_WAIT}
        m = H.amend_merge(old, new)
        self.assertEqual(m["entry_wait"], 0)
        self.assertEqual(m["sizing"]["leverage"], 5)
        self.assertEqual(m["sizing"]["stop_pct"], 2.0)
        self.assertEqual(m["trail"]["value"], 50.0)
        self.assertTrue(m["exit_when"]["on"])
        self.assertEqual(m["sl"], H.complete_plan(old)["sl"])

    def test_nothing_changed_means_nothing_to_apply(self):
        p = plan_with(sl=sl(PRICE, 3.0))
        self.assertEqual(H.amend_diff(p, copy.deepcopy(p)), {"now": [], "waits": False})


class AmendBase(LadderBase):
    def patch_entry_snap(self, inst, **over):
        s = snap_of(1000, dir_e=1, dir_f=1, close=100.0)
        for k, v in over.items():
            setattr(s, k, v)

        async def f(plan):
            return s
        inst._hub_entry_snapshot = f

    @staticmethod
    def remember(inst):
        inst._orig_rules = copy.deepcopy(inst._plan)

    @staticmethod
    def back_to_opened_rules(inst):
        """A second change on the same test trade starts from the rules the trade opened with (the first change has
        already replaced them), so the 'looser' run is compared with the same starting point."""
        inst._plan = copy.deepcopy(inst._orig_rules)

    def new_plan(self, old_inst, **over):
        p = copy.deepcopy(old_inst.pc.plan)
        for k, v in over.items():
            p[k] = v
        return p


class TestAmendStops(AmendBase):
    async def test_stop_loss_tighter_is_used_and_moves_the_stop_on_the_exchange(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))                            # SL 97 rests
        await self.open_long_at(inst)
        self.patch_entry_snap(inst)
        c = inst.client
        res = await inst.hub_amend_open_trade(self.new_plan(inst, sl=sl(PRICE, 2.0)), "tighter")
        self.assertAlmostEqual(inst.hub.ladder["sl"], 98.0)
        self.assertEqual(c.stop_limit_calls[-1][2], 98.0)
        self.assertEqual(len(c.open_algo_orders), 1)                     # the old stop is cancelled
        self.assertEqual(inst.state.sl_price, 98.0)
        self.assertTrue(res["applied"])
        self.assertEqual(res["kept"], [])
        self.assertEqual(inst._plan["sl"]["value"], 2.0)

    async def test_stop_loss_looser_is_refused_in_tighter_mode_and_used_in_looser_mode(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        self.patch_entry_snap(inst)
        self.remember(inst)
        n = len(inst.client.stop_limit_calls)
        res = await inst.hub_amend_open_trade(self.new_plan(inst, sl=sl(PRICE, 5.0)), "tighter")
        self.assertAlmostEqual(inst.hub.ladder["sl"], 97.0)               # kept
        self.assertEqual(len(inst.client.stop_limit_calls), n)            # no new order
        self.assertTrue(res["kept"])
        self.back_to_opened_rules(inst)
        res = await inst.hub_amend_open_trade(self.new_plan(inst, sl=sl(PRICE, 5.0)), "looser")
        self.assertAlmostEqual(inst.hub.ladder["sl"], 95.0)
        self.assertEqual(inst.client.stop_limit_calls[-1][2], 95.0)       # the stop on the exchange moved away
        self.assertEqual(len(inst.client.open_algo_orders), 1)

    async def test_max_loss_changed(self):
        inst = self.make_l()                                             # Max Loss 6% -> 88 (stop-market)
        await self.open_long_at(inst)
        self.patch_entry_snap(inst)
        self.remember(inst)
        p = self.new_plan(inst)
        p["sizing"]["stop_pct"] = 3.0                                    # 3% of 10000 / 50 -> 94
        await inst.hub_amend_open_trade(p, "tighter")
        self.assertAlmostEqual(inst.hub.ladder["ml"], 94.0)
        self.assertAlmostEqual(inst.client.stop_orders[-1][2], 94.0)
        self.assertAlmostEqual(inst.state.stop_target, 94.0)
        p["sizing"]["stop_pct"] = 9.0                                    # 82: looser
        res = await inst.hub_amend_open_trade(p, "tighter")
        self.assertAlmostEqual(inst.hub.ladder["ml"], 94.0)
        self.assertTrue(res["kept"])
        self.back_to_opened_rules(inst)
        await inst.hub_amend_open_trade(p, "looser")
        self.assertAlmostEqual(inst.hub.ladder["ml"], 82.0)
        self.assertAlmostEqual(inst.client.stop_orders[-1][2], 82.0)

    async def test_max_loss_turned_off_only_in_looser_mode(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        self.patch_entry_snap(inst)
        self.remember(inst)
        p = self.new_plan(inst)
        p["sizing"]["stop_on"] = False
        await inst.hub_amend_open_trade(p, "tighter")
        self.assertAlmostEqual(inst.hub.ladder["ml"], 88.0)              # not taken away
        self.back_to_opened_rules(inst)
        await inst.hub_amend_open_trade(p, "looser")
        self.assertIsNone(inst.hub.ladder["ml"])
        self.assertAlmostEqual(inst.hub.ladder["sl"], 97.0)              # the Stop Loss is still there

    async def test_trailing_candle_distance_changes_and_new_level_follows_the_mode(self):
        inst = self.make_l(trail=candle_trail(100.0))
        await self.open_long_at(inst, ts_atr=2.0)                        # candle low 99 - 2 = 97
        self.assertAlmostEqual(inst.hub.ladder["ts"], 97.0)
        self.patch_entry_snap(inst, ts_atr=2.0)
        await inst.hub_amend_open_trade(self.new_plan(inst, trail=candle_trail(50.0)), "tighter")   # 99 - 1 = 98
        self.assertAlmostEqual(inst.hub.ladder["ts"], 98.0)
        self.assertAlmostEqual(inst.hub.ladder["tc_dist"], 1.0)
        self.remember(inst)
        res = await inst.hub_amend_open_trade(self.new_plan(inst, trail=candle_trail(200.0)), "tighter")  # 99 - 4 = 95
        self.assertAlmostEqual(inst.hub.ladder["ts"], 98.0)              # level kept
        self.assertAlmostEqual(inst.hub.ladder["tc_dist"], 4.0)          # but the distance of the NEXT levels is new
        self.assertTrue(res["kept"])
        self.back_to_opened_rules(inst)
        await inst.hub_amend_open_trade(self.new_plan(inst, trail=candle_trail(200.0)), "looser")
        self.assertAlmostEqual(inst.hub.ladder["ts"], 95.0)

    async def test_trailing_turned_off(self):
        inst = self.make_l(trail=candle_trail(100.0))
        await self.open_long_at(inst, ts_atr=2.0)
        self.patch_entry_snap(inst, ts_atr=2.0)
        self.remember(inst)
        p = self.new_plan(inst, trail={"on": False})
        await inst.hub_amend_open_trade(p, "tighter")
        self.assertAlmostEqual(inst.hub.ladder["ts"], 97.0)
        self.back_to_opened_rules(inst)
        await inst.hub_amend_open_trade(p, "looser")
        self.assertIsNone(inst.hub.ladder["ts"])
        self.assertNotIn("tc_dist", inst.hub.ladder)

    async def test_break_even_changed_keeps_done(self):
        inst = self.make_l(sl=sl(PRICE, 3.0), be={"on": True, "after_atr": True, "atr_value": 100.0, "atr_length": 14,
                                                  "after_tp": False})
        await self.open_long_at(inst, be_atr=2.0)
        self.assertAlmostEqual(inst.hub.ladder["be"]["trigger"], 102.0)
        inst.hub.ladder["be"]["done"] = True
        self.patch_entry_snap(inst, be_atr=2.0)
        p = self.new_plan(inst)
        p["be"]["atr_value"] = 200.0
        await inst.hub_amend_open_trade(p, "tighter")
        self.assertAlmostEqual(inst.hub.ladder["be"]["trigger"], 104.0)
        self.assertTrue(inst.hub.ladder["be"]["done"])


class TestAmendOthers(AmendBase):
    async def test_only_the_allowed_settings_reach_the_open_trade(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        self.patch_entry_snap(inst)
        p = self.new_plan(inst)
        p["entry_wait"] = 5
        p["sizing"]["leverage"] = 9
        p["hold"]["adx_level"] = 33.0
        p["exit_when"] = {"on": True, "signal": H.EXIT_BOTH, "after": H.EXIT_WAIT}
        res = await inst.hub_amend_open_trade(p, "tighter")
        self.assertEqual(inst._plan["entry_wait"], 0)
        self.assertEqual(inst._plan["sizing"]["leverage"], 5)
        self.assertEqual(inst._plan["hold"]["adx_level"], 33.0)
        self.assertTrue(inst._plan["exit_when"]["on"])
        self.assertTrue(res["waits"])

    async def test_nothing_to_change_does_nothing(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        n = len(inst.client.stop_limit_calls)
        res = await inst.hub_amend_open_trade(self.new_plan(inst), "tighter")
        self.assertEqual(res, {"applied": [], "kept": [], "waits": False})
        self.assertEqual(len(inst.client.stop_limit_calls), n)

    async def test_no_open_trade_and_bad_choice(self):
        inst = self.make_l()
        with self.assertRaises(RuntimeError):
            await inst.hub_amend_open_trade(self.new_plan(inst), "tighter")
        await self.open_long_at(inst)
        with self.assertRaises(ValueError):
            await inst.hub_amend_open_trade(self.new_plan(inst), "whatever")

    async def test_take_profit_resting_level_is_cancelled_and_replaced(self):
        inst = self.make_l(tp=tp_atr(100.0))
        await self.open_long_at(inst)                                    # TP1 at 102 rests
        self.patch_entry_snap(inst)
        c = inst.client
        self.assertEqual(len(c.limit_calls), 1)
        old_id = inst.hub.tp["levels"][0]["order_id"]
        await inst.hub_amend_open_trade(self.new_plan(inst, tp=tp_atr(200.0)), "tighter")
        self.assertEqual(c.regular_cancels, [old_id])
        self.assertEqual(len(c.limit_calls), 2)
        self.assertAlmostEqual(c.limit_calls[-1][3], 104.0, places=2)
        self.assertEqual([l["state"] for l in inst.hub.tp["levels"]], ["placed", "pending", "off"])
        self.assertAlmostEqual(inst.hub.tp["levels"][1]["price"], 112.0, places=2)

    async def test_take_profit_filled_level_stays(self):
        inst = self.make_l(tp=tp_atr(100.0))
        await self.open_long_at(inst)
        self.fill(inst, inst.hub.tp["levels"][0]["order_id"])
        self.patch_entry_snap(inst)
        await inst.hub_amend_open_trade(self.new_plan(inst, tp=tp_atr(200.0)), "tighter")
        lv = inst.hub.tp["levels"]
        self.assertEqual(lv[0]["state"], "filled")
        self.assertEqual(lv[0]["index"], 1)
        self.assertAlmostEqual(lv[0]["price"], 102.0, places=2)
        self.assertEqual(lv[1]["state"], "placed")

    async def test_take_profit_turned_off_cancels_the_waiting_level(self):
        inst = self.make_l(tp=tp_atr(100.0))
        await self.open_long_at(inst)
        self.patch_entry_snap(inst)
        old_id = inst.hub.tp["levels"][0]["order_id"]
        await inst.hub_amend_open_trade(self.new_plan(inst, tp=tp_atr(100.0, on=False)), "tighter")
        self.assertEqual(inst.client.regular_cancels, [old_id])
        self.assertIsNone(inst.hub.tp)
        self.assertEqual(len(inst.client.limit_calls), 1)               # nothing new placed

    async def test_take_profit_turned_on_while_open(self):
        inst = self.make_l()
        await self.open_long_at(inst)
        self.patch_entry_snap(inst)
        await inst.hub_amend_open_trade(self.new_plan(inst, tp=tp_atr(100.0)), "tighter")
        self.assertEqual(len(inst.client.limit_calls), 1)
        self.assertAlmostEqual(inst.client.limit_calls[0][3], 102.0, places=2)

    async def test_take_profit_not_changed_when_the_atr_is_missing(self):
        inst = self.make_l(tp=tp_atr(100.0))
        await self.open_long_at(inst)
        self.patch_entry_snap(inst, tp_atr=float("nan"))
        res = await inst.hub_amend_open_trade(self.new_plan(inst, tp=tp_atr(200.0)), "tighter")
        self.assertEqual(inst.client.regular_cancels, [])               # nothing cancelled without a replacement
        self.assertTrue(res["kept"])

    async def test_signal_candle_missing_keeps_what_needs_it(self):
        inst = self.make_l(sl=sl(ATR, 200.0, atr_length=14))
        await self.open_long_at(inst, sl_atr=2.5)                       # 95
        inst._hub_entry_snapshot = _none
        res = await inst.hub_amend_open_trade(self.new_plan(inst, sl=sl(ATR, 100.0, atr_length=14)), "looser")
        self.assertAlmostEqual(inst.hub.ladder["sl"], 95.0)
        self.assertTrue(res["kept"])


async def _none(plan):
    return None


class TestAmendSafety(AmendBase):
    async def test_trade_closes_while_the_change_is_worked_out_nothing_is_committed(self):
        inst = self.make_l(sl=sl(ATR, 200.0, atr_length=14))
        await self.open_long_at(inst, sl_atr=2.5)

        async def closes_meanwhile(plan):
            inst.state.status = "IDLE"
            return snap_of(1000, dir_e=1, dir_f=1, close=100.0)
        inst._hub_entry_snapshot = closes_meanwhile
        with self.assertRaises(RuntimeError):
            await inst.hub_amend_open_trade(self.new_plan(inst, sl=sl(ATR, 100.0, atr_length=14)), "looser")
        self.assertEqual(inst._plan["sl"]["value"], 200.0)               # the rules were not replaced
        self.assertFalse(inst._hub_amending)                            # and the bot is not left waiting

    async def test_a_second_change_at_the_same_time_is_refused(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        inst._hub_amending = True
        with self.assertRaises(RuntimeError):
            await inst.hub_amend_open_trade(self.new_plan(inst, sl=sl(PRICE, 2.0)), "tighter")
        self.assertTrue(inst._hub_amending)                             # the other change's flag is not cleared

    async def test_the_bots_own_stop_watch_waits_while_a_change_is_applied(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        inst._hub_amending = True
        inst.client.mark_price = 50.0                                   # far through every level
        self.assertFalse(await inst._hub_ladder_check())
        self.assertEqual(inst.state.status, "IN_POSITION")
        inst._hub_amending = False
        self.assertTrue(await inst._hub_ladder_check())                 # normal again: the memory watch closes it


class TestAmendSaveRoute(AmendBase):
    def stores(self, inst):
        saved = {}

        class S:
            def update_pair(self, a, s, upd):
                saved.update(upd)
                return tv.pair_config(symbol="BTCUSDT", plan=upd["plan"])

        class M:
            restarted = False

            def get(self, a, s):
                return inst

            async def restart(self, a, s):
                M.restarted = True
        return S(), M(), saved

    async def test_choice_is_checked(self):
        for bad in ({}, {"when": "later"}, {"when": "now"}, {"when": "now", "stops": "x"}, "now"):
            with self.assertRaises(hub_api.PlanRefused):
                hub_api.check_amend(bad)
        self.assertIsNone(hub_api.check_amend(None))
        self.assertEqual(hub_api.check_amend({"when": "next", "stops": "tighter"}), {"when": "next", "stops": None})

    async def test_next_trade_changes_nothing_on_the_open_trade(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        S, M, saved = self.stores(inst)
        out = await hub_api.apply_plan(S, M, "a", "BTCUSDT", self.new_plan(inst, sl=sl(PRICE, 2.0)),
                                       amend={"when": "next"})
        self.assertEqual(out["amend_result"], {"status": "next"})
        self.assertEqual(saved["plan"]["sl"]["value"], 2.0)             # saved
        self.assertEqual(inst._plan["sl"]["value"], 3.0)                # the open trade keeps its rules
        self.assertTrue(inst._pending_restart_on_flat)

    async def test_now_changes_the_open_trade_and_the_rest_waits(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        self.patch_entry_snap(inst)
        S, M, saved = self.stores(inst)
        p = self.new_plan(inst, sl=sl(PRICE, 2.0))
        p["entry_wait"] = 4
        out = await hub_api.apply_plan(S, M, "a", "BTCUSDT", p, amend={"when": "now", "stops": "tighter"})
        self.assertEqual(out["amend_result"]["status"], "done")
        self.assertTrue(out["amend_result"]["waits"])
        self.assertAlmostEqual(inst.hub.ladder["sl"], 98.0)
        self.assertEqual(inst._plan["entry_wait"], 0)
        self.assertEqual(saved["plan"]["entry_wait"], 4)
        self.assertTrue(inst._pending_restart_on_flat)                  # the rest is applied when flat
        self.assertFalse(M.restarted)

    async def test_a_failed_change_leaves_the_trade_alone_and_the_plan_saved(self):
        inst = self.make_l(sl=sl(PRICE, 3.0))
        await self.open_long_at(inst)
        S, M, saved = self.stores(inst)

        async def boom(plan, stops):
            raise RuntimeError("exchange down")
        inst.hub_amend_open_trade = boom
        out = await hub_api.apply_plan(S, M, "a", "BTCUSDT", self.new_plan(inst, sl=sl(PRICE, 2.0)),
                                       amend={"when": "now", "stops": "tighter"})
        self.assertEqual(out["amend_result"]["status"], "error")
        self.assertIn("exchange down", out["amend_result"]["why"])
        self.assertEqual(saved["plan"]["sl"]["value"], 2.0)
        self.assertAlmostEqual(inst.hub.ladder["sl"], 97.0)

    async def test_no_open_trade_applies_the_plan_normally(self):
        inst = self.make_l()
        S, M, saved = self.stores(inst)
        out = await hub_api.apply_plan(S, M, "a", "BTCUSDT", self.new_plan(inst), amend={"when": "now", "stops": "tighter"})
        self.assertEqual(out["amend_result"], {"status": "no_open_trade"})
        self.assertTrue(M.restarted)


if __name__ == "__main__":
    unittest.main()
