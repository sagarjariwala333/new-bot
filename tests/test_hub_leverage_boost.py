"""Owner 2026-11-19: entry is decided as always (Supertrend colour change); when a divergence on the SAME side has already been proven,
that entry uses a higher leverage (editable). Amount unchanged, Max Loss unchanged. Arbitrary test numbers."""
import copy
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hub_exit_div import candles, osc_cfg  # noqa: E402
from tests.test_hub_indicator_tf import agg  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402


def boost(lev=5, combine="ANY", keep=3, turn=False, price=False, **osc):
    d = {"combine": combine, "keep": keep, "turn": turn, "price": price}
    for nm in H.DIV_INDS:
        d[nm] = osc.get(nm) or {"on": False}
    return {"on": True, "leverage": lev, "div": d}


def plan_b(b=None, **kw):
    p = make_plan(ind_ma1={"entry": True}, **kw)
    if b is not None:
        p["sizing"]["boost"] = b
    return p


class TestBoost(unittest.TestCase):
    def setUp(self):
        self.df = candles(1500, seed=21, vol=1.6)
        self.t = self.df["open_time"].to_numpy()
        self.h, self.l, self.c = (self.df[k].to_numpy() for k in ("high", "low", "close"))

    def mirror(self, keep=3, turn=False, price=False, **kw):
        osc = H.pine_rsi(self.c, 14)
        bull, bear, bl, brl = H.osc_divergence(self.l, self.h, osc, 3, 30, 70.0, 30.0, False)
        okl = H.div_exit_arrays(osc, bull, bl, self.c, self.t, keep, False, 70.0, 30.0, turn, price)[0]
        oks = H.div_exit_arrays(osc, bear, brl, self.c, self.t, keep, True, 70.0, 30.0, turn, price)[0]
        return okl, oks, bull, bear

    def test_off_by_default_and_old_plans_are_unchanged(self):
        p = make_plan(ind_ma1={"entry": True})
        self.assertNotIn("boost", p["sizing"])
        H.validate_plan(p)
        out = H.compute_hub(self.df, p)
        self.assertFalse(out["boost_long"].any() or out["boost_short"].any())
        snap = H.hub_snapshot_at(out, 300)
        self.assertEqual(H.entry_size("LONG", snap, H.complete_plan(p))[1], float(p["sizing"]["leverage"]))
        p2 = plan_b({"on": False})
        H.validate_plan(p2)

    def test_leverage_has_no_default_number_and_must_be_keyed_in(self):
        """Owner 2026-11-21: the leverage box starts empty. A completed plan keeps it None while OFF; ON with no number is refused."""
        p = H.complete_plan(plan_b({"on": False}))
        self.assertIsNone(p["sizing"]["boost"]["leverage"])
        for lev in (None, 0):
            b = boost(lev=lev, rsi=osc_cfg())
            with self.assertRaises(H.PlanError):
                H.validate_plan(H.complete_plan(plan_b(b)))
        H.validate_plan(H.complete_plan(plan_b(boost(lev=20, rsi=osc_cfg()))))

    def test_boost_matches_an_independent_calculation(self):
        for keep in (0, 3, 10):
            p = plan_b(boost(keep=keep, rsi=osc_cfg()))
            H.validate_plan(p)
            out = H.compute_hub(self.df, p)
            okl, oks, bull, bear = self.mirror(keep)
            self.assertTrue(np.array_equal(out["boost_long"].to_numpy(), okl), keep)
            self.assertTrue(np.array_equal(out["boost_short"].to_numpy(), oks), keep)
            self.assertTrue(okl.any() and oks.any(), "test data never shows a divergence")

    def test_long_needs_a_bullish_and_short_a_bearish_divergence(self):
        p = plan_b(boost(keep=0, rsi=osc_cfg()))
        out = H.compute_hub(self.df, p)
        _, _, bull, bear = self.mirror(0)
        self.assertTrue(np.array_equal(out["boost_long"].to_numpy(), bull))
        self.assertTrue(np.array_equal(out["boost_short"].to_numpy(), bear))

    def test_it_stays_valid_for_keep_candles_only(self):
        keep = 4
        out = H.compute_hub(self.df, plan_b(boost(keep=keep, rsi=osc_cfg())))
        _, _, bull, _ = self.mirror(keep)
        bl = out["boost_long"].to_numpy()
        for d in np.flatnonzero(bull)[:20]:
            self.assertTrue(bl[d:d + keep + 1].all())
        # a candle more than `keep` after the last divergence is off
        last = -10 ** 9
        for i in range(len(bl)):
            if bull[i]:
                last = i
            self.assertEqual(bool(bl[i]), i - last <= keep, i)

    def test_leverage_changes_only_on_a_boosted_entry_and_the_amount_does_not(self):
        p = plan_b(boost(lev=5, keep=3, rsi=osc_cfg()))
        base_lev = float(p["sizing"]["leverage"])
        out = H.compute_hub(self.df, p)
        cp = H.complete_plan(p)
        seen_boost = seen_plain = 0
        for i in range(100, len(out), 7):
            snap = H.hub_snapshot_at(out, i)
            for d in ("LONG", "SHORT"):
                a, lev, kind = H.entry_size(d, snap, cp)
                flag = snap.boost_long if d == "LONG" else snap.boost_short
                self.assertEqual(lev, 5.0 if flag else base_lev)
                self.assertEqual(a, float(p["sizing"]["alloc_pct"]))
                self.assertEqual(kind, "single")
                seen_boost += bool(flag)
                seen_plain += not flag
        self.assertGreater(seen_boost, 0)
        self.assertGreater(seen_plain, 0)

    def test_counter_trend_entries_are_not_boosted(self):
        p = plan_b(boost(lev=5, keep=50, rsi=osc_cfg()))
        p["sizing"]["counter"] = {"on": True, "by": "ema", "ema_length": 50, "alloc_pct": 1.0, "leverage": 2}
        p["sizing"]["counter"]["by"] = H.CT_EMA
        out = H.compute_hub(self.df, p)
        cp = H.complete_plan(p)
        got = set()
        for i in range(100, len(out), 3):
            snap = H.hub_snapshot_at(out, i)
            for d in ("LONG", "SHORT"):
                a, lev, kind = H.entry_size(d, snap, cp)
                if kind == "counter":
                    self.assertEqual(lev, 2.0)
                got.add((kind, lev))
        self.assertIn(("counter", 2.0), got)
        self.assertTrue(any(k == "trend" and lv == 5.0 for k, lv in got))

    def test_any_all_and_two_oscillators(self):
        a = H.compute_hub(self.df, plan_b(boost(combine="ANY", keep=3, rsi=osc_cfg(), cci=osc_cfg(length=20, ob=100.0, os_=-100.0))))
        b = H.compute_hub(self.df, plan_b(boost(combine="ALL", keep=3, rsi=osc_cfg(), cci=osc_cfg(length=20, ob=100.0, os_=-100.0))))
        r = H.compute_hub(self.df, plan_b(boost(keep=3, rsi=osc_cfg())))
        c = H.compute_hub(self.df, plan_b(boost(keep=3, cci=osc_cfg(length=20, ob=100.0, os_=-100.0))))
        for side in ("boost_long", "boost_short"):
            self.assertTrue(np.array_equal(a[side].to_numpy(), r[side].to_numpy() | c[side].to_numpy()))
            self.assertTrue(np.array_equal(b[side].to_numpy(), r[side].to_numpy() & c[side].to_numpy()))
        self.assertGreater(a["boost_long"].sum(), b["boost_long"].sum())

    def test_turn_back_and_price_break_ticks_narrow_it(self):
        base = H.compute_hub(self.df, plan_b(boost(keep=6, rsi=osc_cfg())))
        for kw in ({"turn": True}, {"price": True}, {"turn": True, "price": True}):
            out = H.compute_hub(self.df, plan_b(boost(keep=6, rsi=osc_cfg(), **kw)))
            okl, oks, _, _ = self.mirror(6, kw.get("turn", False), kw.get("price", False))
            self.assertTrue(np.array_equal(out["boost_long"].to_numpy(), okl), kw)
            self.assertTrue(np.array_equal(out["boost_short"].to_numpy(), oks), kw)
            self.assertTrue((out["boost_long"].to_numpy() <= base["boost_long"].to_numpy()).all())
            self.assertLess(out["boost_long"].sum() + out["boost_short"].sum(), base["boost_long"].sum() + base["boost_short"].sum())

    def test_on_another_timeframe(self):
        h4 = agg(self.df, 4)
        p = plan_b(boost(keep=2, rsi=dict(osc_cfg(), tf="4h")))
        H.validate_plan(p)
        self.assertEqual(H.div_tfs_needed(p), ["4h"])
        with self.assertRaises(H.HtfMissing):
            H.compute_hub(self.df, p)
        out = H.compute_hub(self.df, p, htf={"4h": h4})
        self.assertTrue(out["boost_long"].any())
        # nothing from a 4h candle that has not closed yet
        h4b = h4.copy()
        cut = 150
        h4b.loc[cut:, ["open", "high", "low", "close"]] *= 1.5
        alt = H.compute_hub(self.df, p, htf={"4h": h4b})
        upto = cut * 4 + 3
        self.assertTrue(np.array_equal(out["boost_long"].to_numpy()[:upto], alt["boost_long"].to_numpy()[:upto]))
        self.assertTrue(np.array_equal(out["boost_short"].to_numpy()[:upto], alt["boost_short"].to_numpy()[:upto]))

    def test_validation(self):
        H.validate_plan(plan_b(boost(lev=1, rsi=osc_cfg())))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_b(boost(lev=126, rsi=osc_cfg())))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_b(boost(lev=0, rsi=osc_cfg())))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_b(boost(lev=5)))                                   # no oscillator ticked
        b = boost(rsi=osc_cfg())
        del b["leverage"]
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_b(b))
        b = boost(rsi=osc_cfg(lookback=3, pivot=3))
        with self.assertRaises(H.PlanError) as cm:
            H.validate_plan(plan_b(b))
        self.assertIn("Higher leverage", str(cm.exception))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_b(boost(combine="SOME", rsi=osc_cfg())))
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_b(boost(turn=True, rsi={"on": True, "length": 14, "pivot": 3, "lookback": 30})))   # band needed
        H.validate_plan(plan_b({"on": False}))                                       # off: nothing else needed

    def test_the_exit_divergence_is_not_disturbed(self):
        from tests.test_hub_exit_div import div_cfg
        p = plan_b(boost(keep=3, rsi=osc_cfg()), exit_when=div_cfg(rsi=osc_cfg()))
        H.validate_plan(p)
        out = H.compute_hub(self.df, p)
        self.assertIn("xd_rsi_ok_l", out.columns)
        q = make_plan(ind_ma1={"entry": True}, exit_when=div_cfg(rsi=osc_cfg()))
        out2 = H.compute_hub(self.df, q)
        self.assertTrue(np.array_equal(out["xd_rsi_ok_l"].to_numpy(), out2["xd_rsi_ok_l"].to_numpy()))

    def test_review_text(self):
        txt = " ".join(" ".join(map(str, r)) for r in H.plan_lines(H.complete_plan(plan_b(boost(lev=5, keep=3, rsi=osc_cfg(), turn=True)))))
        self.assertIn("Higher leverage on divergence", txt)
        self.assertIn("leverage 5", txt)
        off = " ".join(" ".join(map(str, r)) for r in H.plan_lines(H.complete_plan(plan_b())))
        self.assertNotIn("Higher leverage on divergence", off)

    def test_paper_tracker_and_backtest_use_the_boosted_leverage(self):
        from app import hub_backtest as BT
        p = plan_b(boost(lev=5, keep=8, rsi=osc_cfg()), allow_reentry=True)
        p["ind"]["ma1"].update(type="EMA", length=9)
        p["ind"]["ma2"].update(type="EMA", length=21)
        p["ind"]["ma1"].update(entry=False, flip=False)
        p["ind"]["mx"].update(entry=True, flip=True, modes=[H.MX_MODES[0]])
        p["sizing"].update(stop_on=False)
        p["sl"] = {"on": False}
        res = BT.run(self.df, p, 1000.0, 0.0, 0.0)
        levs = {t.get("leverage") for t in res["trades"]}
        self.assertTrue(levs <= {float(p["sizing"]["leverage"]), 5.0}, levs)
        self.assertEqual(levs, {float(p["sizing"]["leverage"]), 5.0})
        p0 = copy.deepcopy(p)
        p0["sizing"]["boost"] = {"on": False}
        res0 = BT.run(self.df, p0, 1000.0, 0.0, 0.0)
        self.assertEqual({t.get("leverage") for t in res0["trades"]}, {float(p["sizing"]["leverage"])})
        # same entries, only the size differs
        self.assertEqual([t["entry_time"] for t in res["trades"]], [t["entry_time"] for t in res0["trades"]])


from tests import test_hub_trading as T  # noqa: E402


class TestLiveEntry(T.Base):
    """The real entry path: the exchange gets the boosted leverage, the quantity is bigger, the amount (margin) and the Max Loss are the same."""
    async def run_entry(self, boosted):
        inst = self.make(make_plan(ind_ma1={"entry": True, "flip": True},
                                   sizing={"alloc_pct": 10.0, "leverage": 3, "stop_on": True, "stop_pct": 6.0,
                                           "boost": {"on": True, "leverage": 5, "div": boost(rsi=osc_cfg())["div"]}}))
        self.go_long(inst)
        inst.client.available_balance = 8000.0
        s = T.snap_of(1000, dir_e=-1, dir_f=-1, close=100.0)
        s.boost_short = boosted                      # the entry is a SHORT here (dir_e = -1)
        await self.tick(inst, s)
        return inst

    async def test_boosted_entry_uses_the_higher_leverage(self):
        inst = await self.run_entry(True)
        self.assertEqual(inst.client.leverage_calls[-1], 5)
        self.assertAlmostEqual(inst.client.market_orders[0][2], 8000 * 0.10 * 5 / 100.0, places=3)     # 40: margin 800 x 5x
        # Max Loss 6% of 8000 = 480 lost at the stop whatever the leverage: stop = 100 + 480/40
        self.assertAlmostEqual(inst.client.stop_orders[0][2], 100 + 480 / 40.0, places=2)

    async def test_plain_entry_keeps_the_normal_leverage(self):
        inst = await self.run_entry(False)
        self.assertEqual(inst.client.leverage_calls[-1], 3)
        self.assertAlmostEqual(inst.client.market_orders[0][2], 8000 * 0.10 * 3 / 100.0, places=3)     # 24
        self.assertAlmostEqual(inst.client.stop_orders[0][2], 100 + 480 / 24.0, places=2)


if __name__ == "__main__":
    unittest.main()
