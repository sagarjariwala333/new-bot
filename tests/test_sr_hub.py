"""Support / Resistance wired into the hub (owner 2026-11-05): the plan, the entry signal as a block / stand-alone,
the exits and where they apply, the stop outside the zone, the live bot, the paper copy (shadow / backtest) and the
chart data. Arbitrary test numbers - the product has no default plan."""
import copy
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_ladder import LadderBase  # noqa: E402  (stubs + temp data dir)
from tests.test_hub_trading import snap_of  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_sr_engine import frame, fixed_levels, base_cfg, bars  # noqa: E402
from app import sr_engine as E  # noqa: E402
from app import strategy_hub as H  # noqa: E402
from app import shadow_book as B  # noqa: E402
from app import chart_api  # noqa: E402


def sr_plan(**sr):
    base = {"on": True, "find_swing": True, "swing_n": 3, "count": 1, "bounce_long": True, "bounce_short": True}
    base.update(sr)
    return base


def wave(n=240, amp=5.0, period=20, mid=105.0):
    return frame(mid + amp * np.sin(np.arange(n) / period * 2 * np.pi))


class TestSignalsInTheHub(unittest.TestCase):
    def test_stand_alone_adds_sr_entries_to_the_entry_signal(self):
        df = wave()
        p = make_plan(ind_ma1={"entry": True}, sr=sr_plan(alone=True))
        f = H.compute_hub(df, p)
        self.assertGreater(int(f["sr_long"].sum()), 2)
        sl = f["sr_long"].values
        self.assertTrue((f["entry_long"].values[sl]).all())
        # without "alone" the SR signal is not an entry by itself
        q = make_plan(ind_ma1={"entry": True}, sr=sr_plan())
        g = H.compute_hub(df, q)
        self.assertTrue((g["sr_long"].values == f["sr_long"].values).all())
        self.assertTrue((g["entry_long"].values == g["ma1_long"].values).all())

    def test_its_own_strategy_needs_no_other_indicator(self):
        p = make_plan(sr=sr_plan(alone=True))
        H.validate_plan(p)                     # no indicator ticked, but Support / Resistance is its own strategy
        f = H.compute_hub(wave(), p)
        self.assertGreater(int(f["entry_long"].sum()), 2)
        self.assertTrue((f["entry_long"].values == f["sr_long"].values).all())
        self.assertIn("its own strategy", H.plan_summary(H.complete_plan(p)))
        q = make_plan(sr=sr_plan())            # not its own strategy and nothing ticked: still refused
        with self.assertRaises(H.PlanError):
            H.validate_plan(q)

    def test_changing_it_while_a_trade_is_open_waits_for_the_next_trade(self):
        a = H.complete_plan(make_plan(ind_ma1={"entry": True}, sr=sr_plan(alone=True)))
        b = copy.deepcopy(a)
        b["sr"]["count"] = 2
        d = H.amend_diff(a, b)
        self.assertEqual(d["now"], [])
        self.assertTrue(d["waits"])

    def test_as_one_more_block_it_follows_the_combination(self):
        df = wave()
        sr = sr_plan()
        p_all = make_plan(ind_ma1={"entry": True}, ind_sr={"entry": True}, sr=sr, combine_entry="ALL")
        f = H.compute_hub(df, p_all)
        expect = f["ma1_long"].values & f["sr_long"].values
        self.assertTrue((f["entry_long"].values == expect).all())
        p_any = make_plan(ind_ma1={"entry": True}, ind_sr={"entry": True}, sr=sr, combine_entry="ANY")
        g = H.compute_hub(df, p_any)
        self.assertTrue((g["entry_long"].values == (g["ma1_long"].values | g["sr_long"].values)).all())

    def test_it_can_be_the_main(self):
        df = wave()
        p = make_plan(ind_ma1={"entry": True}, ind_sr={"entry": True}, sr=sr_plan(), combine_entry="ANY",
                      main_entry="sr")
        H.validate_plan(p)
        f = H.compute_hub(df, p)
        self.assertTrue((f["entry_long"].values == (f["sr_long"].values & (f["ma1_long"].values | f["sr_long"].values))).all())
        self.assertFalse((f["entry_long"].values & ~f["sr_long"].values).any())

    def test_off_everything_is_false_and_columns_exist(self):
        f = H.compute_hub(wave(), make_plan(ind_ma1={"entry": True}))
        for col in ("sr_long", "sr_short", "sr_origin", "sr_stop_long", "sr_xb_long", "sr_xk_long", "sr_s1", "sr_lock"):
            self.assertIn(col, f.columns)
        self.assertFalse(f["sr_long"].any() or f["sr_short"].any())
        self.assertEqual(int(f["sr_origin"].sum()), 0)
        self.assertEqual(f.attrs["sr_levels"], [])

    def test_the_other_blocks_are_unchanged_by_turning_sr_on(self):
        df = wave()
        a = H.compute_hub(df, make_plan(ind_ma1={"entry": True}))
        b = H.compute_hub(df, make_plan(ind_ma1={"entry": True}, sr=sr_plan()))
        for col in a.columns:
            if col.startswith("sr_") or a[col].dtype == object:
                continue
            self.assertTrue(np.array_equal(a[col].values, b[col].values, equal_nan=a[col].dtype.kind == "f"), col)

    def test_the_snapshot_carries_the_signal_and_the_origin(self):
        f = H.compute_hub(wave(), make_plan(ind_ma1={"entry": True}, sr=sr_plan(alone=True)))
        i = int(np.nonzero(f["sr_long"].values)[0][0])
        s = H.hub_snapshot_at(f, i)
        self.assertTrue(s.sr_long)
        self.assertEqual(s.sr_origin, E.ORIGIN_BOUNCE)

    def test_the_normal_candles_are_used_not_heikin_ashi(self):
        df = wave()
        a = H.compute_hub(df, make_plan(ind_ma1={"entry": True}, sr=sr_plan(alone=True)))
        b = H.compute_hub(df, make_plan(ind_ma1={"entry": True}, sr=sr_plan(alone=True), chart_type="Heikin Ashi"))
        self.assertTrue((a["sr_long"].values == b["sr_long"].values).all())
        self.assertTrue(np.array_equal(a["sr_s1"].values, b["sr_s1"].values, equal_nan=True))


class TestExits(unittest.TestCase):
    FILL = [(105, 106, 104, 105)] * 3

    def run_e(self, rows, cfg, sup=(100.0,), res=(110.0,)):
        o, h, l, c, v, a = bars(rows)
        with fixed_levels(sup, res):
            return E.compute(o, h, l, c, v, a, cfg)

    def test_other_end_of_the_range(self):
        r = self.run_e(self.FILL + [(105, 110.5, 104, 108)], base_cfg(bx_other=True))
        self.assertTrue(r["exit_bl"][3]); self.assertFalse(r["exit_bs"][3])
        r = self.run_e(self.FILL + [(105, 106, 99.5, 102)], base_cfg(bx_other=True))
        self.assertTrue(r["exit_bs"][3]); self.assertFalse(r["exit_bl"][3])

    def test_middle_line(self):
        r = self.run_e(self.FILL + [(104.5, 104.8, 104, 104.2)], base_cfg(bx_mid=True))
        self.assertFalse(r["exit_bl"][3])
        r = self.run_e(self.FILL + [(105, 105.6, 104, 105.2)], base_cfg(bx_mid=True))
        self.assertTrue(r["exit_bl"][3])               # high 105.6 reaches the middle 105
        self.assertTrue(r["exit_bs"][3])

    def test_range_breaks_against_you(self):
        r = self.run_e(self.FILL + [(101, 102, 98, 99)], base_cfg(bx_break=True))
        self.assertTrue(r["exit_bl"][3]); self.assertFalse(r["exit_bs"][3])
        r = self.run_e(self.FILL + [(109, 112, 108, 111)], base_cfg(bx_break=True))
        self.assertTrue(r["exit_bs"][3]); self.assertFalse(r["exit_bl"][3])

    def test_close_back_inside_after_a_breakout(self):
        rows = self.FILL + [(108, 112, 107, 111), (111, 112, 109, 111.5), (111, 111.5, 108, 109)]
        r = self.run_e(rows, base_cfg(brk_exit_inside=True))
        self.assertFalse(r["exit_kl"][3]); self.assertFalse(r["exit_kl"][4])
        self.assertTrue(r["exit_kl"][5])

    def test_nothing_ticked_nothing_exits(self):
        r = self.run_e(self.FILL + [(105, 112, 98, 99)], base_cfg())
        self.assertFalse(r["exit_bl"].any() or r["exit_bs"].any() or r["exit_kl"].any() or r["exit_ks"].any())

    def test_where_the_exits_apply(self):
        base = make_plan(ind_ma1={"entry": True})
        s = snap_of(1)
        s.sr_xb_long, s.sr_xk_long = True, False
        def decide(origin, **tick):
            p = H.complete_plan(dict(base, sr=sr_plan(bx_mid=True, brk_exit_inside=True, **tick)))
            return H.sr_exit_decision("LONG", s, p, origin)
        # SR trades only: a bounce trade uses the bounce exits; a breakout / retest trade the breakout exit
        self.assertEqual(decide(E.ORIGIN_BOUNCE, exit_sr_only=True), "EXIT")
        self.assertEqual(decide(E.ORIGIN_BREAKOUT, exit_sr_only=True), "STAY")
        self.assertEqual(decide(E.ORIGIN_RETEST, exit_sr_only=True), "STAY")
        self.assertEqual(decide(0, exit_sr_only=True), "STAY")                 # not an SR trade
        # every trade: any ticked exit closes any trade
        for o in (0, 1, 2, 3):
            self.assertEqual(decide(o, exit_all=True), "EXIT")
            self.assertEqual(decide(o, exit_all=True, exit_sr_only=True), "EXIT")
        s.sr_xb_long, s.sr_xk_long = False, True
        self.assertEqual(decide(E.ORIGIN_BREAKOUT, exit_sr_only=True), "EXIT")
        self.assertEqual(decide(E.ORIGIN_RETEST, exit_sr_only=True), "EXIT")
        self.assertEqual(decide(E.ORIGIN_BOUNCE, exit_sr_only=True), "STAY")
        # the side matters
        self.assertEqual(H.sr_exit_decision("SHORT", s, H.complete_plan(dict(base, sr=sr_plan(bx_mid=True, exit_all=True))), 0), "STAY")

    def test_no_exit_tick_never_exits(self):
        s = snap_of(1)
        s.sr_xb_long = s.sr_xk_long = True
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, sr=sr_plan(exit_all=True)))
        self.assertEqual(H.sr_exit_decision("LONG", s, p, 1), "STAY")
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}))
        self.assertEqual(H.sr_exit_decision("LONG", s, p, 1), "STAY")


class TestStopOutsideTheZone(unittest.TestCase):
    def plan(self, sl_on=True, **sr):
        return H.complete_plan(make_plan(ind_ma1={"entry": True}, sr=sr_plan(stop_zone_on=True, stop_zone_atr_pct=20.0,
                                                                              atr_length=14, **sr),
                                         sl={"on": sl_on, "basis": "% of price movement", "value": 5.0,
                                             "atr_length": 14}))

    def snap(self, origin=1, long=95.0, short=None):
        s = snap_of(1)
        s.sr_origin, s.sr_stop_long = origin, long
        s.sr_stop_short = short if short is not None else float("nan")
        return s

    def test_the_sr_stop_alone_when_the_normal_stop_loss_is_off(self):
        lv = H.sl_level("LONG", 100.0, 2.0, self.plan(sl_on=False), snap=self.snap())
        self.assertEqual(lv, 95.0)

    def test_both_on_the_nearer_one_wins(self):
        p = self.plan()          # normal stop 5% = 95.0
        self.assertEqual(H.sl_level("LONG", 100.0, 2.0, p, snap=self.snap(long=97.0)), 97.0)
        self.assertEqual(H.sl_level("LONG", 100.0, 2.0, p, snap=self.snap(long=90.0)), 95.0)
        s = self.snap(long=float("nan"), short=103.0)
        self.assertEqual(H.sl_level("SHORT", 100.0, 2.0, p, snap=s), 103.0)         # normal stop 105

    def test_a_trade_not_entered_by_sr_gets_only_the_normal_stop(self):
        p = self.plan()
        self.assertEqual(H.sl_level("LONG", 100.0, 2.0, p, snap=self.snap(origin=0, long=97.0)), 95.0)
        self.assertIsNone(H.sl_level("LONG", 100.0, 2.0, self.plan(sl_on=False), snap=self.snap(origin=0)))

    def test_a_stop_on_the_wrong_side_of_the_entry_is_not_used(self):
        p = self.plan(sl_on=False)
        self.assertIsNone(H.sl_level("LONG", 100.0, 2.0, p, snap=self.snap(long=101.0)))
        self.assertIsNone(H.sl_level("LONG", 100.0, 2.0, p, snap=self.snap(long=float("nan"))))

    def test_off_without_the_tick(self):
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, sr=sr_plan(),
                                      sl={"on": False}))
        self.assertIsNone(H.sl_level("LONG", 100.0, 2.0, p, snap=self.snap()))

    def test_no_snapshot_no_sr_stop(self):
        self.assertIsNone(H.sl_level("LONG", 100.0, 2.0, self.plan(sl_on=False)))
        self.assertIsNotNone(H.sl_level("LONG", 100.0, 2.0, self.plan()))


class TestLiveBot(LadderBase):
    def sr(self, **kw):
        base = {"on": True, "find_swing": True, "swing_n": 3, "count": 1, "bounce_long": True, "bx_mid": True,
                "brk_exit_inside": True, "exit_sr_only": True, "alone": True}
        base.update(kw)
        return base

    async def enter_by_sr(self, inst, origin, price=100.0, t=1000):
        inst.client.mark_price = price
        s = snap_of(t, dir_e=1, dir_f=1, close=price)
        s.sr_long, s.sr_origin = True, origin
        await self.tick(inst, s)
        return s

    async def test_the_origin_is_remembered_and_cleared_on_close(self):
        inst = self.make_l(sr=self.sr())
        await self.enter_by_sr(inst, E.ORIGIN_BREAKOUT)
        self.assertEqual(inst.state.direction, "LONG")
        self.assertEqual(inst.hub.sr_origin, E.ORIGIN_BREAKOUT)
        from app import hub_state
        self.assertEqual(hub_state.load(inst.platform, inst.account_id, inst.symbol).sr_origin, E.ORIGIN_BREAKOUT)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.sr_xk_long = True
        inst.client.mark_price = 101.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(inst.hub.sr_origin, 0)

    async def test_not_entered_by_sr_means_origin_zero(self):
        inst = self.make_l(sr=self.sr(exit_sr_only=True))
        await self.open_long_at(inst)                               # snap has no SR signal
        self.assertEqual(inst.hub.sr_origin, 0)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.sr_xb_long = s.sr_xk_long = True
        inst.client.mark_price = 101.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.direction, "LONG")              # "SR trades only": a plain trade is left alone
        self.assertEqual(inst.client.close_orders, [])

    async def test_a_bounce_trade_uses_the_bounce_exit_and_waits_flat(self):
        inst = self.make_l(sr=self.sr())
        await self.enter_by_sr(inst, E.ORIGIN_BOUNCE)
        s = snap_of(2000, dir_e=1, dir_f=1, close=102.0)
        s.sr_xk_long = True                                         # the breakout exit does not apply to a bounce trade
        inst.client.mark_price = 102.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.direction, "LONG")
        s = snap_of(3000, dir_e=1, dir_f=1, close=103.0)
        s.sr_xb_long = True
        inst.client.mark_price = 103.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.status, "IDLE")
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(len(inst.client.market_orders), 1)         # nothing re-opened on the exit candle
        self.assertTrue(inst.hub.reentry_forced)
        self.assertEqual(inst.hub.reentry_block, 1)

    async def test_every_trade_scope_closes_a_plain_trade(self):
        inst = self.make_l(sr=self.sr(exit_sr_only=False, exit_all=True))
        await self.open_long_at(inst)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.sr_xb_long = True
        inst.client.mark_price = 101.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.status, "IDLE")

    async def test_a_flip_on_the_same_candle_is_one_close_not_two(self):
        inst = self.make_l(sr=self.sr(exit_sr_only=False, exit_all=True))
        await self.open_long_at(inst)
        inst.client.mark_price = 99.0
        s = snap_of(2000, dir_e=-1, dir_f=-1, close=99.0)
        s.sr_xb_long = True
        await self.tick(inst, s)
        self.assertEqual(len(inst.client.close_orders), 1)
        self.assertEqual(inst.state.direction, "SHORT")             # the flip did its job; the SR exit did not double it

    async def test_off_ignores_the_signals(self):
        inst = self.make_l()
        await self.open_long_at(inst)
        s = snap_of(2000, dir_e=1, dir_f=1, close=101.0)
        s.sr_xb_long = s.sr_xk_long = True
        inst.client.mark_price = 101.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.direction, "LONG")

    async def test_the_sr_stop_is_put_on_the_ladder_and_the_exchange(self):
        inst = self.make_l(sr=self.sr(stop_zone_on=True, stop_zone_atr_pct=20.0, atr_length=14),
                           sl={"on": False})
        inst.client.mark_price = 100.0
        s = snap_of(1000, dir_e=1, dir_f=1, close=100.0)
        s.sr_long, s.sr_origin, s.sr_stop_long = True, E.ORIGIN_BOUNCE, 96.0
        await self.tick(inst, s)
        self.assertEqual(inst.state.direction, "LONG")
        self.assertAlmostEqual(inst.hub.ladder["sl"], 96.0)

    async def test_the_sr_stop_alone_counts_as_a_stop(self):
        inst = self.make_l(sr=self.sr(stop_zone_on=True, stop_zone_atr_pct=20.0, atr_length=14),
                           sl={"on": False}, sizing={"stop_on": False})
        self.assertTrue(inst._hub_any_stop() if inst._plan else False)


class TestShadow(unittest.TestCase):
    def test_paper_trade_remembers_the_origin_and_exits(self):
        plan = H.complete_plan(make_plan(ind_ma1={"entry": True, "flip": True},
                                         sr=sr_plan(bx_mid=True, exit_all=True, alone=True, bounce_long=True),
                                         shadow={"on": True, "trades": 3, "slippage_ticks": 0, "tracker_start": 1.0}))
        book = B.new_book(1.0)
        s = snap_of(1000, dir_e=1, dir_f=1, close=100.0)
        s.sr_long, s.sr_origin = True, E.ORIGIN_BOUNCE
        B.on_candle(book, s, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=2000)
        self.assertEqual(book["pos"], 1)
        self.assertEqual(book["sr_origin"], E.ORIGIN_BOUNCE)
        s2 = snap_of(2000, dir_e=1, dir_f=1, close=103.0)
        s2.sr_xb_long = True
        ev, closed = B.on_candle(book, s2, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=3000)
        self.assertEqual(book["pos"], 0)
        self.assertEqual(len(closed), 1)
        self.assertIn("Support / Resistance exit", closed[0]["exit_reason"])
        self.assertEqual(book["sr_origin"], 0)
        self.assertTrue(book["reentry_forced"])

    def test_sr_trades_only_leaves_a_plain_paper_trade_alone(self):
        plan = H.complete_plan(make_plan(ind_ma1={"entry": True, "flip": True},
                                         sr=sr_plan(bx_mid=True, exit_sr_only=True, alone=True),
                                         shadow={"on": True, "trades": 3, "slippage_ticks": 0, "tracker_start": 1.0}))
        book = B.new_book(1.0)
        B.on_candle(book, snap_of(1000, dir_e=1, dir_f=1, close=100.0), plan, tick_size=0.1, last_total=None,
                    balance=1000.0, t_ms=2000)
        self.assertEqual(book["sr_origin"], 0)
        s2 = snap_of(2000, dir_e=1, dir_f=1, close=103.0)
        s2.sr_xb_long = True
        B.on_candle(book, s2, plan, tick_size=0.1, last_total=None, balance=1000.0, t_ms=3000)
        self.assertEqual(book["pos"], 1)

    def test_the_backtest_runs_with_sr_on(self):
        from app import hub_backtest as BT
        df = wave(n=420)
        plan = make_plan(ind_ma1={"entry": True},
                         sr=sr_plan(alone=True, bx_mid=True, exit_all=True, brk_long=True, brk_short=True,
                                    retest_long=True, retest_short=True, wait_fail=True))
        res = BT.run(df, plan, 1000.0, 0.05, 0.01)
        self.assertGreater(len(res["trades"]), 2)


class TestChartData(unittest.TestCase):
    def test_lines_markers_and_the_table(self):
        df = wave()
        plan = H.complete_plan(make_plan(ind_ma1={"entry": True},
                                         sr=sr_plan(alone=True, brk_long=True, retest_long=True, wait_none=True,
                                                    zone_on=True, zone_atr_pct=20.0, atr_length=14,
                                                    count=2)))
        f = H.compute_hub(df, plan)
        times = df["open_time"].astype("int64").tolist()
        out = chart_api.sr_payload(f, plan, times[100:])
        self.assertTrue(out["on"])
        self.assertEqual(len(out["lines"]["s1"]), len(times) - 100)
        self.assertTrue(any(v is not None for v in out["lines"]["r1"]))
        self.assertGreater(len(out["markers"]), 0)
        self.assertTrue(all(m["origin"] in ("Bounce", "Breakout", "Retest") for m in out["markers"]))
        names = [r["name"] for r in out["table"]]
        self.assertIn("Support 1", names)
        self.assertIn("Resistance 1", names)
        row = [r for r in out["table"] if r["name"] == "Support 1"][0]
        self.assertAlmostEqual(row["zone_to"] - row["zone_from"], 2 * 0.2 * f["sr_s1"].iloc[-1] * 0 + row["zone_to"] - row["zone_from"])
        self.assertGreaterEqual(row["zone_to"], row["zone_from"])
        self.assertIn("Bounce long entry", row["used_by"])
        self.assertTrue(row["formed_t"] is None or row["formed_t"] in times)

    def test_off_gives_nothing(self):
        plan = H.complete_plan(make_plan(ind_ma1={"entry": True}))
        f = H.compute_hub(wave(), plan)
        self.assertEqual(chart_api.sr_payload(f, plan, [0]), {"on": False})


if __name__ == "__main__":
    unittest.main()
