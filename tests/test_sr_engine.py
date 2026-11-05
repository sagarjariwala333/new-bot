"""Support / Resistance (owner 2026-11-05): the level finder, bounce, breakout, retest, the wait, the same-candle
conflict, no look-ahead, the plan checks and the exits. Made-up candles only."""
import copy
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from app import sr_engine as E
from app import strategy_hub as H
from tests._hub_plan import make_plan


def base_cfg(**over):
    c = {k: False for k in H.SR_TICKS}
    c.update(H.SR_NUMBERS)
    c.update(on=True, find_swing=True, swing_n=3, count=1)
    c.update(over)
    return c


def bars(rows):
    """rows: (open, high, low, close) -> arrays"""
    a = np.array(rows, float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.full(len(a), 100.0), np.full(len(a), 1.0)


def fixed_levels(supports, resistances):
    """Patch the finder: the same flat lines on every bar, each one a support when it is at / below the last close
    and a resistance above it (like the real finder), nearest first."""
    allv = sorted(set(list(supports) + list(resistances)))

    def f(t, ref, cfg, h, l, sw):
        sup = sorted([v for v in allv if v <= ref], reverse=True)
        res = sorted([v for v in allv if v > ref])
        mk = lambda v: E._Lv(0.0, v, "swing", ("f", v), 0, "x")      # noqa: E731
        n = int(cfg["count"])
        return [mk(v) for v in sup[:n]], [mk(v) for v in res[:n]]
    return mock.patch.object(E, "_levels_at", f)


def frame(closes, spread=0.6, vol=100.0):
    closes = np.array(closes, float)
    o = np.r_[closes[0], closes[:-1]]
    h = np.maximum(o, closes) + spread
    l = np.minimum(o, closes) - spread
    return pd.DataFrame({"open_time": np.arange(len(closes)) * 60000, "open": o, "high": h, "low": l,
                         "close": closes, "volume": np.full(len(closes), vol)})


class TestPlanChecks(unittest.TestCase):
    def sr_plan(self, **sr):
        base = {"on": True, "find_hhll": True, "hhll_n": 20, "count": 1, "bounce_long": True}
        base.update(sr)
        return make_plan(ind_ma1={"entry": True}, sr=base)

    def test_no_sr_section_means_off(self):
        p = make_plan(ind_ma1={"entry": True})
        H.validate_plan(p)
        self.assertFalse(H.complete_plan(p)["sr"]["on"])
        self.assertIsNone(H.sr_cfg(H.complete_plan(p)))

    def test_ticked_in_the_list_but_function_off_is_refused(self):
        p = make_plan(ind_ma1={"entry": True}, ind_sr={"entry": True}, sr={"on": False})
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_a_valid_plan(self):
        H.validate_plan(self.sr_plan())

    def test_nothing_to_find_the_range_is_refused(self):
        p = self.sr_plan(find_hhll=False)
        with self.assertRaises(H.PlanError) as cm:
            H.validate_plan(p)
        self.assertIn("find the range", str(cm.exception))

    def test_every_number_is_needed_when_its_tick_is_on(self):
        cases = [("find_hhll", {"find_hhll": True}, "hhll_n"),
                 ("swing", {"find_swing": True}, "swing_n"),
                 ("diag", {"diagonal": True}, "diag_points"),
                 ("zone", {"zone_on": True, "atr_length": 14}, "zone_atr_pct"),
                 ("stop", {"stop_zone_on": True, "atr_length": 14}, "stop_zone_atr_pct"),
                 ("edge", {"bounce_edges": True}, "bounce_edge_pct"),
                 ("margin", {"brk_long": True, "brk_close_on": True, "atr_length": 14}, "brk_close_atr_pct"),
                 ("vol", {"brk_long": True, "brk_vol_on": True, "brk_vol_len": 20}, "brk_vol_mult"),
                 ("vol", {"brk_long": True, "brk_vol_on": True, "brk_vol_mult": 1.5}, "brk_vol_len"),
                 ("run", {"retest_long": True, "wait_runaway": True, "atr_length": 14}, "wait_runaway_atr_pct")]
        for name, extra, key in cases:
            p = self.sr_plan(**extra)
            p["sr"].pop(key, None)
            if key == "hhll_n" and "find_hhll" in extra:
                pass
            with self.assertRaises(H.PlanError, msg=name):
                H.validate_plan(p)

    def test_atr_length_is_needed_only_when_an_atr_tick_is_on(self):
        H.validate_plan(self.sr_plan())                              # no ATR tick, no ATR length
        p = self.sr_plan(zone_on=True, zone_atr_pct=25.0)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p["sr"]["atr_length"] = 14
        H.validate_plan(p)

    def test_diagonal_points_are_2_or_3(self):
        for pts, ok in ((2, True), (3, True), (4, False), (1, False)):
            p = self.sr_plan(diagonal=True, swing_n=3, diag_points=pts)
            if ok:
                H.validate_plan(p)
            else:
                with self.assertRaises(H.PlanError):
                    H.validate_plan(p)

    def test_edge_width_is_at_most_half_the_range(self):
        p = self.sr_plan(bounce_edges=True, bounce_edge_pct=60.0)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p["sr"]["bounce_edge_pct"] = 25.0
        H.validate_plan(p)

    def test_edges_only_belongs_to_a_bounce(self):
        p = self.sr_plan(bounce_long=False, brk_long=True, bounce_edges=True, bounce_edge_pct=20.0, wait_none=True)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_used_for_entries_needs_an_entry_ticked(self):
        p = make_plan(ind_ma1={"entry": True}, ind_sr={"entry": True},
                      sr={"on": True, "find_hhll": True, "hhll_n": 20, "count": 1})
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        p = self.sr_plan(bounce_long=False, alone=True)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)

    def test_the_wait_needs_a_tick_when_a_retest_or_bounce_plus_breakout_is_on(self):
        for extra in ({"retest_long": True}, {"bounce_short": True, "brk_long": True}):
            p = self.sr_plan(**extra)
            with self.assertRaises(H.PlanError, msg=str(extra)):
                H.validate_plan(p)
            p["sr"]["wait_fail"] = True
            H.validate_plan(p)
        H.validate_plan(self.sr_plan())            # a bounce alone has no wait

    def test_exits_need_a_place_to_apply(self):
        p = self.sr_plan(bx_mid=True)
        with self.assertRaises(H.PlanError):
            H.validate_plan(p)
        for k in ("exit_sr_only", "exit_all"):
            q = self.sr_plan(bx_mid=True, **{k: True})
            H.validate_plan(q)

    def test_sr_alone_with_exits_only_is_allowed(self):
        p = make_plan(ind_ma1={"entry": True},
                      sr={"on": True, "find_hhll": True, "hhll_n": 20, "count": 2, "bx_break": True, "exit_all": True})
        H.validate_plan(p)

    def test_an_old_plan_loads_and_nothing_is_preselected(self):
        c = H.complete_plan(make_plan(ind_ma1={"entry": True}))
        for k in H.SR_TICKS:
            self.assertFalse(c["sr"][k], k)
        self.assertFalse(c["ind"]["sr"]["entry"] or c["ind"]["sr"]["flip"])

    def test_inert_numbers_do_not_change_the_result(self):
        df = frame(105 + 5 * np.sin(np.arange(200) / 20 * 2 * np.pi))
        a = self.sr_plan(find_hhll=False, find_swing=True, swing_n=3, count=1, alone=True, atr_length=14,
                         bounce_short=True)
        b = copy.deepcopy(a)
        b["sr"].update(hhll_n=7, zone_atr_pct=99.0, stop_zone_atr_pct=99.0, bounce_edge_pct=3.0,
                       brk_close_atr_pct=77.0, brk_vol_mult=9.0, brk_vol_len=3, wait_runaway_atr_pct=1.0,
                       diag_points=3)
        fa, fb = H.compute_hub(df, a), H.compute_hub(df, b)
        for col in ("sr_long", "sr_short", "entry_long", "entry_short", "sr_origin"):
            self.assertTrue((fa[col].values == fb[col].values).all(), col)


class TestFinder(unittest.TestCase):
    def test_a_swing_is_known_only_n_candles_later(self):
        h = np.array([1, 2, 3, 9, 3, 2, 1, 2, 3, 2, 1.0])
        l = h - 0.5
        hi, lo = E.swing_points(h, l, 2)
        self.assertEqual(hi[0][0], 3)
        sw = E._Sw(h, l, 2, True)
        self.assertEqual(int(sw.hc[0]), 5)           # high at 3, known after bar 5
        cfg = base_cfg(find_swing=True, swing_n=2)
        sup, res = E._levels_at(4, 2.0, cfg, h, l, sw)
        self.assertEqual(res, [])                     # not yet known at bar 4
        sup, res = E._levels_at(5, 2.0, cfg, h, l, sw)
        self.assertEqual([x.icpt for x in res], [9.0])

    def test_highest_high_lowest_low(self):
        h = np.array([5, 7, 6, 8, 6, 5.5, 5.2])
        l = h - 1
        cfg = base_cfg(find_swing=False, find_hhll=True, hhll_n=4)
        sup, res = E._levels_at(6, 5.0, cfg, h, l, E._Sw(h, l, 1, False))
        self.assertEqual(res[0].icpt, 8.0)
        self.assertEqual(sup[0].icpt, 4.2)

    def test_a_level_below_the_price_is_a_support_and_above_is_a_resistance(self):
        h = np.array([1, 2, 3, 9, 3, 2, 1, 2, 3, 2, 1, 2, 3.0])
        l = h - 0.5
        cfg = base_cfg(find_swing=True, swing_n=2, count=3)
        sw = E._Sw(h, l, 2, True)
        sup, res = E._levels_at(12, 20.0, cfg, h, l, sw)       # price above the old top: it is a support now
        self.assertIn(9.0, [x.icpt for x in sup])
        self.assertEqual(res, [])

    def test_count_gives_the_nearest_n(self):
        h = np.array([1, 1, 5, 1, 1, 1, 7, 1, 1, 1, 9, 1, 1, 1, 1, 1, 1, 1.0])
        l = h - 0.1
        sw = E._Sw(h, l, 2, True)
        for cnt in (1, 2, 3):
            cfg = base_cfg(find_swing=True, swing_n=2, count=cnt)
            sup, res = E._levels_at(17, 2.0, cfg, h, l, sw)
            self.assertEqual([x.icpt for x in res], [5.0, 7.0, 9.0][:cnt])

    def test_diagonal_through_two_and_three_points(self):
        # tops at 10 (j=5), 12 (j=15), 14 (j=25): a rising line of slope 0.2
        h = np.full(40, 5.0)
        for j, p in ((5, 10.0), (15, 12.0), (25, 14.0)):
            h[j] = p
        l = h - 1
        sw = E._Sw(h, l, 2, True)
        for pts in (2, 3):
            cfg = base_cfg(find_swing=False, diagonal=True, swing_n=2, diag_points=pts, count=3)
            sup, res = E._levels_at(35, 4.0, cfg, h, l, sw)
            dl = [x for x in res if x.src == "diagonal"][0]
            self.assertAlmostEqual(dl.slope, 0.2, places=6)
            self.assertAlmostEqual(dl.at(36), 14.0 + 0.2 * 11, places=6)

    def test_the_same_line_found_twice_is_listed_once(self):
        h = np.array([1, 2, 3, 9, 3, 2, 1, 2, 3, 2, 1, 2, 3.0])
        l = h - 0.5
        cfg = base_cfg(find_swing=True, find_hhll=True, hhll_n=12, swing_n=2, count=3)
        sw = E._Sw(h, l, 2, True)
        sup, res = E._levels_at(12, 4.0, cfg, h, l, sw)
        self.assertEqual([x.icpt for x in res].count(9.0), 1)


class TestNoLookAhead(unittest.TestCase):
    def test_signals_never_change_when_later_candles_are_added(self):
        rng = np.random.default_rng(7)
        c = 100 + np.cumsum(rng.normal(0, 1.0, 400))
        df = frame(c, spread=0.8)
        sr = dict(on=True, find_hhll=True, hhll_n=15, find_swing=True, swing_n=3, diagonal=True, diag_points=2,
                  count=2, zone_on=True, zone_atr_pct=30.0, stop_zone_on=True, stop_zone_atr_pct=20.0,
                  atr_length=14, bounce_long=True, bounce_short=True, brk_long=True, brk_short=True,
                  brk_close_on=True, brk_close_atr_pct=5.0, retest_long=True, retest_short=True, wait_fail=True,
                  wait_newrange=True, wait_runaway=True, wait_runaway_atr_pct=300.0, bx_other=True, bx_mid=True,
                  bx_break=True, brk_exit_inside=True, exit_all=True, alone=True)
        plan = make_plan(ind_ma1={"entry": True}, sr=sr)
        full = H.compute_hub(df, plan)
        self.assertGreater(int((full["sr_long"] | full["sr_short"]).sum()), 3)
        cols = ["sr_long", "sr_short", "sr_origin", "sr_s1", "sr_r1", "sr_xb_long", "sr_xk_short", "sr_lock"]
        for cut in (120, 200, 301, 399):
            part = H.compute_hub(df.iloc[:cut].reset_index(drop=True), plan)
            for col in cols:
                a, b = part[col].values, full[col].values[:cut]
                if a.dtype.kind == "f":
                    same = np.allclose(a, b, equal_nan=True)
                else:
                    same = (a == b).all()
                self.assertTrue(same, f"{col} changed with later candles (cut {cut})")


class TestBounce(unittest.TestCase):
    def run_e(self, rows, cfg, sup=(100.0,), res=(110.0,), atr=1.0):
        o, h, l, c, v, a = bars(rows)
        a = a * atr
        with fixed_levels(sup, res):
            return E.compute(o, h, l, c, v, a, cfg)

    FILL = [(105, 106, 104, 105)] * 3

    def test_long_bounce_reaches_the_bottom_and_closes_back_up(self):
        r = self.run_e(self.FILL + [(101, 103, 99.5, 102)], base_cfg(bounce_long=True))
        self.assertTrue(r["long"][3]); self.assertEqual(r["origin"][3], E.ORIGIN_BOUNCE)

    def test_no_bounce_if_it_does_not_reach_the_bottom(self):
        r = self.run_e(self.FILL + [(104, 105, 102, 103)], base_cfg(bounce_long=True))
        self.assertFalse(r["long"][3])

    def test_no_bounce_if_it_closes_below_the_bottom_or_is_bearish(self):
        r = self.run_e(self.FILL + [(101, 102, 98, 99)], base_cfg(bounce_long=True))
        self.assertFalse(r["long"].any())
        r = self.run_e(self.FILL + [(103, 104, 99.5, 101)], base_cfg(bounce_long=True))     # closes above but bearish
        self.assertFalse(r["long"].any())

    def test_short_bounce_mirror(self):
        r = self.run_e(self.FILL + [(109, 110.5, 107, 108)], base_cfg(bounce_short=True))
        self.assertTrue(r["short"][3])

    def test_the_zone_widens_the_touch(self):
        rows = self.FILL + [(102, 105, 101.0, 103)]          # low 101 is 1 above the bottom 100
        self.assertFalse(self.run_e(rows, base_cfg(bounce_long=True))["long"].any())
        cfg = base_cfg(bounce_long=True, zone_on=True, zone_atr_pct=150.0)       # zone = 1.5 x ATR(1.0)
        self.assertTrue(self.run_e(rows, cfg)["long"][3])

    def test_the_zone_widens_the_short_touch(self):
        rows = self.FILL + [(108, 109.0, 107, 107.5)]        # high 109 is 1 below the top 110
        self.assertFalse(self.run_e(rows, base_cfg(bounce_short=True))["short"].any())
        cfg = base_cfg(bounce_short=True, zone_on=True, zone_atr_pct=150.0)
        self.assertTrue(self.run_e(rows, cfg)["short"][3])

    def test_a_ticked_side_only(self):
        r = self.run_e(self.FILL + [(101, 103, 99.5, 102)], base_cfg(bounce_short=True))
        self.assertFalse(r["long"].any())

    def test_edges_only_blocks_a_close_in_the_middle(self):
        rows = self.FILL + [(101, 108, 99.5, 107)]            # touches the bottom, closes high in the range
        cfg = base_cfg(bounce_long=True)
        self.assertTrue(self.run_e(rows, cfg)["long"][3])
        cfg = base_cfg(bounce_long=True, bounce_edges=True, bounce_edge_pct=25.0)      # edge = 2.5 from 100
        self.assertFalse(self.run_e(rows, cfg)["long"][3])
        rows = self.FILL + [(101, 103, 99.5, 102)]
        self.assertTrue(self.run_e(rows, cfg)["long"][3])

    def test_stop_outside_the_zone(self):
        rows = self.FILL + [(101, 103, 99.5, 102)]
        cfg = base_cfg(bounce_long=True, zone_on=True, zone_atr_pct=50.0, stop_zone_on=True, stop_zone_atr_pct=20.0)
        r = self.run_e(rows, cfg)
        self.assertAlmostEqual(r["stop_long"][3], 100.0 - 0.5 - 0.2, places=9)
        cfg = base_cfg(bounce_short=True, zone_on=True, zone_atr_pct=50.0, stop_zone_on=True, stop_zone_atr_pct=20.0)
        r = self.run_e(self.FILL + [(109, 110.5, 107, 108)], cfg)
        self.assertAlmostEqual(r["stop_short"][3], 110.0 + 0.5 + 0.2, places=9)

    def test_no_signal_without_atr_when_the_zone_needs_it(self):
        o, h, l, c, v, a = bars(self.FILL + [(101, 103, 99.5, 102)])
        a = a * np.nan
        with fixed_levels((100.0,), (110.0,)):
            r = E.compute(o, h, l, c, v, a, base_cfg(bounce_long=True, zone_on=True, zone_atr_pct=50.0))
        self.assertFalse(r["long"].any())


class TestBreakout(unittest.TestCase):
    FILL = [(105, 106, 104, 105)] * 3

    def run_e(self, rows, cfg, vols=None, sup=(100.0,), res=(110.0,)):
        o, h, l, c, v, a = bars(rows)
        if vols is not None:
            v = np.array(vols, float)
        with fixed_levels(sup, res):
            return E.compute(o, h, l, c, v, a, cfg)

    def test_first_close_above_the_top_is_a_long_breakout(self):
        r = self.run_e(self.FILL + [(108, 112, 107, 111)], base_cfg(brk_long=True))
        self.assertTrue(r["long"][3]); self.assertEqual(r["origin"][3], E.ORIGIN_BREAKOUT)
        self.assertTrue(r["break_up"][3])

    def test_a_wick_above_the_top_is_not_a_breakout(self):
        r = self.run_e(self.FILL + [(108, 112, 107, 109)], base_cfg(brk_long=True))
        self.assertFalse(r["long"].any())

    def test_short_breakout_and_side_ticks(self):
        rows = self.FILL + [(102, 103, 97, 98)]
        self.assertTrue(self.run_e(rows, base_cfg(brk_short=True))["short"][3])
        self.assertFalse(self.run_e(rows, base_cfg(brk_long=True))["short"].any())

    def test_close_beyond_by_an_atr_percent(self):
        rows = self.FILL + [(108, 111, 107, 110.3)]            # 0.3 above the top (ATR 1.0)
        self.assertTrue(self.run_e(rows, base_cfg(brk_long=True))["long"][3])
        self.assertFalse(self.run_e(rows, base_cfg(brk_long=True, brk_close_on=True, brk_close_atr_pct=50.0))["long"].any())
        self.assertTrue(self.run_e(rows, base_cfg(brk_long=True, brk_close_on=True, brk_close_atr_pct=20.0))["long"][3])

    def test_volume_above_its_average(self):
        rows = self.FILL + [(108, 112, 107, 111)]
        cfg = base_cfg(brk_long=True, brk_vol_on=True, brk_vol_mult=1.5, brk_vol_len=3)
        self.assertFalse(self.run_e(rows, cfg, vols=[100, 100, 100, 120])["long"].any())
        self.assertTrue(self.run_e(rows, cfg, vols=[100, 100, 100, 160])["long"][3])

    def test_volume_average_excludes_the_breakout_candle(self):
        rows = self.FILL + [(108, 112, 107, 111)]
        cfg = base_cfg(brk_long=True, brk_vol_on=True, brk_vol_mult=1.0, brk_vol_len=3)
        # average of the 3 candles BEFORE it = 100; the candle itself (100) is not above 100 x 1.0 -> equal is enough
        self.assertTrue(self.run_e(rows, cfg, vols=[100, 100, 100, 100])["long"][3])
        self.assertFalse(self.run_e(rows, cfg, vols=[100, 100, 100, 99])["long"].any())

    def test_a_break_that_fails_the_filters_is_not_a_confirmed_break(self):
        rows = self.FILL + [(108, 112, 107, 111), (111, 113, 110, 112)]
        cfg = base_cfg(brk_long=True, retest_long=True, wait_fail=True, brk_vol_on=True, brk_vol_mult=2.0, brk_vol_len=3)
        r = self.run_e(rows, cfg, vols=[100, 100, 100, 120, 100])
        self.assertFalse(r["break_up"].any())
        self.assertTrue(np.isnan(r["lock"]).all())

    def test_stop_for_a_breakout_is_below_the_broken_level(self):
        rows = self.FILL + [(108, 112, 107, 111)]
        cfg = base_cfg(brk_long=True, stop_zone_on=True, stop_zone_atr_pct=30.0)
        r = self.run_e(rows, cfg)
        self.assertAlmostEqual(r["stop_long"][3], 110.0 - 0.3, places=9)


class TestRetestAndWait(unittest.TestCase):
    """Levels 100 / 110. Bar 3 breaks up through 110. Then:"""
    HEAD = [(105, 106, 104, 105)] * 3 + [(108, 112, 107, 111)]

    def run_e(self, tail, cfg, sup=(100.0,), res=(110.0,)):
        o, h, l, c, v, a = bars(self.HEAD + tail)
        with fixed_levels(sup, res):
            return E.compute(o, h, l, c, v, a, cfg)

    def cfg(self, **kw):
        return base_cfg(brk_long=True, retest_long=True, retest_short=True, **kw)

    def test_the_broken_level_is_locked_with_its_value(self):
        r = self.run_e([(111, 113, 111, 112)], self.cfg(wait_none=True))
        self.assertEqual(r["lock"][4], 110.0)
        self.assertEqual(r["lock_dir"][4], 1)

    def test_retest_long_touches_the_old_top_holds_and_moves_away(self):
        tail = [(112, 113, 111, 112), (111, 112, 109.5, 111.5)]
        r = self.run_e(tail, self.cfg(wait_none=True))
        self.assertTrue(r["long"][5]); self.assertEqual(r["origin"][5], E.ORIGIN_RETEST)

    def test_the_retest_is_used_once(self):
        tail = [(111, 112, 109.5, 111.5), (111, 112, 109.5, 111.5)]
        r = self.run_e(tail, self.cfg(wait_none=True))
        self.assertTrue(r["long"][4]); self.assertFalse(r["long"][5])

    def test_the_retest_must_close_above_and_be_bullish(self):
        r = self.run_e([(111, 112, 109.5, 109.8)], self.cfg(wait_none=True))
        self.assertFalse(r["long"].any() and r["origin"][4] == E.ORIGIN_RETEST)
        r = self.run_e([(112, 112.5, 109.5, 111.0)], self.cfg(wait_none=True))            # bearish
        self.assertFalse(r["origin"][4] == E.ORIGIN_RETEST)

    def test_the_level_fails(self):
        tail = [(111, 111.5, 108, 109), (109, 110, 108.5, 109.5), (109.5, 111.5, 109, 110.5)]
        r = self.run_e(tail, self.cfg(wait_fail=True))
        self.assertNotEqual(r["lock_dir"][5], 1, "the up-lock must be gone after a close back below the level")
        self.assertFalse(r["long"][6] and r["origin"][6] == E.ORIGIN_RETEST)

    def test_the_level_holds_so_the_wait_is_kept(self):
        # closes stay above the level 110, so "level fails" must not end the wait and the retest still fires
        tail = [(111, 112, 110.5, 111.5), (111.5, 112, 109.5, 111.8)]
        r = self.run_e(tail, self.cfg(wait_fail=True))
        self.assertEqual(r["lock_dir"][4], 1)
        self.assertTrue(r["long"][5]); self.assertEqual(r["origin"][5], E.ORIGIN_RETEST)

    def test_the_level_fails_ends_the_wait_and_no_retest_follows(self):
        # a half-ATR margin keeps the dip from being a confirmed down-break, so only "level fails" can end the wait
        tail = [(110.5, 111, 109.6, 109.8), (109.8, 110.8, 109.5, 110.4)]
        r = self.run_e(tail, self.cfg(wait_fail=True, brk_close_on=True, brk_close_atr_pct=50.0))
        self.assertTrue(np.isnan(r["lock"][5]))
        self.assertFalse(r["long"][5], "the wait ended when the close fell back below the level")
        r = self.run_e(tail, self.cfg(wait_none=True, brk_close_on=True, brk_close_atr_pct=50.0))
        self.assertTrue(r["long"][5] and r["origin"][5] == E.ORIGIN_RETEST)

    def test_nothing_ends_it_so_the_wait_survives_many_candles(self):
        tail = [(112, 113, 111, 112.5)] * 6
        r = self.run_e(tail, self.cfg(wait_none=True))
        for i in range(4, 10):
            self.assertEqual(r["lock"][i], 110.0, i)

    def test_the_wait_is_not_ended_by_things_that_are_not_ticked(self):
        tail = [(111, 111.5, 111, 111.2), (111.2, 111.6, 111.0, 111.4), (111.4, 111.5, 111.2, 111.3)]
        r = self.run_e(tail, self.cfg(wait_runaway=True, wait_runaway_atr_pct=5000.0))
        for i in range(4, 7):
            self.assertEqual(r["lock_dir"][i], 1)

    def test_price_runs_away(self):
        tail = [(111, 116, 111, 115), (115, 116, 109.5, 115.5)]
        cfg = self.cfg(wait_runaway=True, wait_runaway_atr_pct=300.0)       # ATR 1.0 -> 3 beyond the level
        r = self.run_e(tail, cfg)
        self.assertFalse(r["origin"][5] == E.ORIGIN_RETEST)
        cfg = self.cfg(wait_runaway=True, wait_runaway_atr_pct=800.0)
        r = self.run_e(tail, cfg)
        self.assertTrue(r["origin"][5] == E.ORIGIN_RETEST)

    def test_a_new_range_forms(self):
        # after the break a support appears at 108 (nearer to the price than the locked 110)
        o, h, l, c, v, a = bars(self.HEAD + [(112, 113, 111, 112), (111, 112, 109.5, 111.5)])
        calls = {"n": 0}

        def f(t, ref, cfg, hh, ll, sw):
            if t >= 4:
                return ([E._Lv(0.0, 111.0, "swing", ("a",), 0, "x"), E._Lv(0.0, 110.0, "swing", ("b",), 0, "x")],
                        [E._Lv(0.0, 120.0, "swing", ("c",), 0, "x")])
            return ([E._Lv(0.0, 100.0, "swing", ("d",), 0, "x")], [E._Lv(0.0, 110.0, "swing", ("b",), 0, "x")])
        with mock.patch.object(E, "_levels_at", f):
            r = E.compute(o, h, l, c, v, a, self.cfg(wait_newrange=True))
        self.assertFalse(r["origin"][5] == E.ORIGIN_RETEST)
        with mock.patch.object(E, "_levels_at", f):
            r = E.compute(o, h, l, c, v, a, self.cfg(wait_none=True))
        self.assertTrue(r["origin"][5] == E.ORIGIN_RETEST)

    def test_whichever_comes_first_ends_the_wait(self):
        tail = [(111, 116, 111, 115), (115, 116, 114, 115)]
        r = self.run_e(tail, self.cfg(wait_runaway=True, wait_runaway_atr_pct=300.0, wait_fail=True))
        self.assertTrue(np.isnan(r["lock"][5]))

    def test_a_diagonal_level_keeps_its_slope_for_the_retest(self):
        # a falling line from 120 at bar 0 by 1.0 per bar: value at bar i = 120 - i. Break up at bar 10 (value 110).
        n = 14
        rows = [(100, 101, 99, 100)] * 10
        rows += [(108, 112, 107, 111.5), (111, 112, 108.2, 111), (110, 111, 105, 109.5), (109, 111.5, 106, 110.4)]
        o, h, l, c, v, a = bars(rows)
        lv = E._Lv(-1.0, 120.0, "diagonal", ("d",), 0, "x")

        def f(t, ref, cfg, hh, ll, sw):
            if t >= 10:
                return ([E._Lv(0.0, 95.0, "swing", ("s",), 0, "x")], [E._Lv(0.0, 150.0, "swing", ("r",), 0, "x")])
            return ([E._Lv(0.0, 95.0, "swing", ("s",), 0, "x")], [lv])
        with mock.patch.object(E, "_levels_at", f):
            r = E.compute(o, h, l, c, v, a, self.cfg(wait_none=True))
        self.assertTrue(r["break_up"][10])
        self.assertAlmostEqual(r["lock"][12], 120.0 - 12, places=9)        # still the same sloping line
        self.assertAlmostEqual(r["lock"][13], 120.0 - 13, places=9)
        # the retest is judged on the sloping value (107 at bar 13), not on the flat 120
        self.assertEqual(r["origin"][13], E.ORIGIN_RETEST)
        self.assertTrue(r["long"][13])


class TestConflict(unittest.TestCase):
    """A retest long and a breakout short on one candle (the nearest support above the locked level breaks)."""

    def test_opposite_signals_on_one_candle_cancel_and_the_wait_blocks_bounces(self):
        # bar 3 breaks up through 110 (lock 110). Bar 5: touches 110, closes above it, bullish (retest long) while it
        # also closes below the nearest support 112 (breakout short) -> conflict.
        rows = [(105, 106, 104, 105)] * 3 + [(108, 112.5, 107, 113)]
        rows += [(113, 114, 113, 113.5)]
        rows += [(110.2, 111.9, 109.8, 111.0)]
        rows += [(99.8, 105, 99.5, 101)]        # later: a bounce long at 100 while the wait blocks
        o, h, l, c, v, a = bars(rows)

        def f(t, ref, cfg, hh, ll, sw):
            if t < 3:
                return ([E._Lv(0.0, 100.0, "swing", ("s2",), 0, "x")], [E._Lv(0.0, 110.0, "swing", ("b",), 0, "x")])
            return ([E._Lv(0.0, 112.0, "swing", ("s",), 0, "x") if t >= 4 and t < 5 else E._Lv(0.0, 100.0, "swing", ("s2",), 0, "x")],
                    [E._Lv(0.0, 125.0, "swing", ("r",), 0, "x")])
        cfg = base_cfg(brk_long=True, brk_short=True, retest_long=True, bounce_long=True, wait_none=True)
        with mock.patch.object(E, "_levels_at", f):
            r = E.compute(o, h, l, c, v, a, cfg)
        self.assertTrue(r["conflict"][5])
        self.assertFalse(r["long"][5] or r["short"][5])
        # the level that broke (112, down) is locked, and the wait blocks new bounce / breakout entries
        self.assertEqual(r["lock"][6], 112.0)
        self.assertEqual(r["lock_dir"][6], -1)
        self.assertFalse(r["long"][6], "a bounce long must wait while the conflict wait runs")
