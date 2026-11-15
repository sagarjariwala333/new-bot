"""Counter trend decided by its OWN moving average (any type / length / simple condition), not by a ticked
indicator and not only by an EMA. Arbitrary test numbers only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
import numpy as np  # noqa: E402

from app import strategy_hub as H  # noqa: E402
from tests._hub_plan import make_plan  # noqa: E402
from tests.test_strategy_hub import make_df  # noqa: E402

DF = make_df(600)
ABOVE, RISING = "Price above / below MA", "MA rising / falling"


def own(**kw):
    c = {"on": True, "by": "own", "ma_type": "EMA", "ma_length": 50, "ma_modes": [ABOVE],
         "alloc_pct": 3.0, "leverage": 2}
    c.update(kw)
    return c


def plan(**kw):
    sz = {"method": H.SIZE_ALLOC, "alloc_pct": 10.0, "leverage": 5, "stop_on": True,
          "stop_basis": H.STOP_ENTRY, "stop_pct": 6.0, "counter": own(**kw)}
    return make_plan(ind_macd=dict(entry=True), sizing=sz)


class TestOwn(unittest.TestCase):
    def test_every_type_matches_the_ma(self):
        for ty in H.MA_TYPES:
            p = plan(ma_type=ty, ma_length=30)
            H.validate_plan(p)
            f = H.compute_hub(DF, p)
            cC = f["c_close"].values
            ma = H._ma(ty, cC, 30, 0.85, 6.0)
            with np.errstate(invalid="ignore"):
                np.testing.assert_array_equal(f["ct_long"].values, cC > ma)
                np.testing.assert_array_equal(f["ct_short"].values, cC < ma)

    def test_rising_and_both(self):
        f = H.compute_hub(DF, plan(ma_modes=[RISING]))
        ma = f["ct_ma"].values
        with np.errstate(invalid="ignore"):
            prev = np.concatenate([[np.nan], ma[:-1]])
            np.testing.assert_array_equal(f["ct_long"].values, ma > prev)
            np.testing.assert_array_equal(f["ct_short"].values, ma < prev)
        a = H.compute_hub(DF, plan(ma_modes=[ABOVE]))
        b = H.compute_hub(DF, plan(ma_modes=[ABOVE, RISING]))
        r = H.compute_hub(DF, plan(ma_modes=[RISING]))
        np.testing.assert_array_equal(b["ct_long"].values, a["ct_long"].values & r["ct_long"].values)
        np.testing.assert_array_equal(b["ct_short"].values, a["ct_short"].values & r["ct_short"].values)

    def test_independent_of_ma1_ma2_and_ticks(self):
        p = plan(ma_length=40)
        p["ind"]["ma1"].update(type="SMA", length=7)
        p["ind"]["ma2"].update(type="WMA", length=9)
        f = H.compute_hub(DF, p)
        g = H.compute_hub(DF, plan(ma_length=40))
        np.testing.assert_array_equal(f["ct_long"].values, g["ct_long"].values)

    def test_entry_size_with_and_against(self):
        p = plan()
        cp = H.complete_plan(p)
        f = H.compute_hub(DF, p)
        for i in range(300, 600, 29):
            s = H.hub_snapshot_at(f, i)
            for d, col in (("LONG", "ct_long"), ("SHORT", "ct_short")):
                alloc, lev, kind = H.entry_size(d, s, cp)
                self.assertEqual(kind, "trend" if f[col].values[i] else "counter")
                self.assertEqual((alloc, lev), (10.0, 5.0) if kind == "trend" else (3.0, 2.0))

    def test_zero_means_no_counter_entry(self):
        p = plan(alloc_pct=0)
        H.validate_plan(p)
        self.assertTrue(H.counter_entry_off("counter", H.complete_plan(p)))

    def test_fixed_base(self):
        p = make_plan(ind_macd=dict(entry=True), sizing={
            "method": H.SIZE_FIXED, "base_amount": 100.0, "leverage": 5, "stop_on": True, "stop_basis": H.STOP_ENTRY,
            "stop_pct": 6.0, "counter": own(base_amount=50.0)})
        H.validate_plan(p)
        f = H.compute_hub(DF, p)
        self.assertTrue(f["ct_long"].any())

    def test_ema_length_not_needed(self):
        p = plan()
        self.assertNotIn("ema_length", p["sizing"]["counter"])
        H.validate_plan(p)
        H.compute_hub(DF, p)                         # no KeyError

    def test_validation(self):
        for bad in (dict(ma_type="XYZ"), dict(ma_length=0), dict(ma_length=501), dict(ma_length=2.5),
                    dict(ma_modes=[]), dict(ma_modes=["nope"]), dict(ma_modes=[ABOVE, ABOVE]),
                    dict(ma_modes=["Price crossed MA within N candles"]),
                    dict(ma_type="ALMA", ma_alma_sigma=0)):
            with self.assertRaises(H.PlanError, msg=str(bad)):
                H.validate_plan(plan(**bad))
        for k in ("ma_type", "ma_length", "ma_modes"):
            p = plan()
            del p["sizing"]["counter"][k]
            with self.assertRaises(H.PlanError, msg=k):
                H.validate_plan(p)

    def test_alma(self):
        p = plan(ma_type="ALMA", ma_alma_offset=0.7, ma_alma_sigma=5.0)
        H.validate_plan(p)
        f = H.compute_hub(DF, p)
        np.testing.assert_allclose(f["ct_ma"].values, H._ma("ALMA", f["c_close"].values, 50, 0.7, 5.0), equal_nan=True)
        f2 = H.compute_hub(DF, plan(ma_type="ALMA"))   # default 0.85 / 6
        np.testing.assert_allclose(f2["ct_ma"].values, H._ma("ALMA", f2["c_close"].values, 50, 0.85, 6.0), equal_nan=True)

    def test_heikin_ashi_uses_chart_close(self):
        p = plan()
        p["chart_type"] = "Heikin Ashi"
        f = H.compute_hub(DF, p)
        self.assertFalse(np.array_equal(f["c_close"].values, DF["close"].values))
        np.testing.assert_allclose(f["ct_ma"].values, H.pine_ema(f["c_close"].values, 50), equal_nan=True)

    def test_old_and_indicator_paths_unchanged(self):
        old = make_plan(ind_macd=dict(entry=True), sizing={
            "method": H.SIZE_ALLOC, "alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_basis": H.STOP_ENTRY,
            "stop_pct": 6.0, "counter": {"on": True, "ema_length": 50, "alloc_pct": 3.0, "leverage": 2}})
        self.assertEqual(H.upgrade_plan(old)["sizing"]["counter"]["by"], H.CT_EMA)
        H.validate_plan(old)
        f = H.compute_hub(DF, old)
        np.testing.assert_allclose(f["size_ema"].values, H.pine_ema(f["c_close"].values, 50), equal_nan=True)
        ind = plan(by="ma1")
        ind["ind"]["ma1"].update(type="EMA", length=50)
        H.validate_plan(ind)
        g = H.compute_hub(DF, ind)
        np.testing.assert_array_equal(g["ct_long"].values, g["ma1_long"].values)

    def test_plan_lines_and_off(self):
        txt = str(H.plan_lines(H.complete_plan(plan())))
        self.assertIn("own EMA 50", txt)
        p = plan()
        p["sizing"]["counter"]["on"] = False
        H.validate_plan(p)


if __name__ == "__main__":
    unittest.main()


from tests.test_hub_ladder import LadderBase  # noqa: E402


class TestLiveBotOwn(LadderBase):
    def inst_with(self, **ct):
        c = {"on": True, "by": "own", "ma_type": "EMA", "ma_length": 50, "ma_modes": [ABOVE],
             "alloc_pct": 4.0, "leverage": 2}
        c.update(ct)
        return self.make_l(sizing={"alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_pct": 6.0, "counter": c})

    async def test_with_the_trend_uses_the_normal_size(self):
        inst = self.inst_with()
        await self.enter_long(inst, ct_long=True, ct_short=False)
        self.assertEqual(inst.state.direction, "LONG")
        self.assertEqual(inst.client.market_orders[0][2], 50.0)       # 10% x 5x of 10000 / 100

    async def test_against_the_trend_uses_the_counter_size_and_logs_why(self):
        inst = self.inst_with()
        logs = []
        inst._log = lambda m, *a, **k: logs.append(m)
        await self.enter_long(inst, ct_long=False, ct_short=True)
        self.assertEqual(inst.state.direction, "LONG")
        self.assertEqual(inst.client.market_orders[0][2], 8.0)        # 4% x 2x of 10000 / 100
        self.assertTrue(any("its own EMA 50" in m and "COUNTER" in m for m in logs), logs)

    async def test_zero_counter_does_not_open(self):
        inst = self.inst_with(alloc_pct=0)
        await self.enter_long(inst, ct_long=False, ct_short=True)
        self.assertIsNone(inst.state.direction)
        self.assertEqual(inst.client.market_orders, [])
