"""Stop Loss tab (owner 2026-10-26).
- Stop Loss is always in the menu; a plan can be saved without it ("these can be done later").
- No SL set -> the trade still opens (bot, shadow, Analysis), Telegram + email: "No SL was set for this trade. Set
  it manually." ("nothing to stop ops from ongoing.. SL can be fixed manually")
- SL price cannot be worked out at entry -> the trade still opens, Telegram + email: "SL could not be worked out
  for this trade. Set it manually."
- Measured on: % of ATR, % of price movement, Fixed $ amount, Structure (swing low / high), Chandelier, Moving
  average. Worked out once at entry, then fixed. A wrong-side result = cannot be worked out.
- Time stop (optional): open N candles and not in profit -> close at the candle close.
"""
import dataclasses
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from tests import test_hub_trading as T
from tests._hub_plan import make_plan
from app import strategy_hub as H
from app import shadow_book as B
from app import hub_api
from app.hub_trading import CANDLE_MS

ATR, PRICE = H.BASIS_ATR, H.BASIS_PRICE


def plan_sl(**sl):
    p = make_plan(ind_ma1={"entry": True, "flip": True})
    p["sl"] = {"on": True, **sl}
    return p


def snap(t=1, close=100.0, dir_e=0, dir_f=0, **kw):
    s = T.snap_of(t, dir_e=dir_e, dir_f=dir_f, close=close)
    return dataclasses.replace(s, **kw)


# ======================================================================================== plan rules (pure)
class TestValidation(unittest.TestCase):
    def ok(self, **sl):
        H.validate_plan(plan_sl(**sl))

    def bad(self, **sl):
        with self.assertRaises(H.PlanError):
            H.validate_plan(plan_sl(**sl))

    def test_every_measured_on_choice(self):
        self.assertEqual(H.SL_BASES, (ATR, PRICE, "Fixed $ amount", "Structure (swing low / high)", "Chandelier",
                                      "Moving average"))
        self.ok(basis=ATR, value=200.0, atr_length=14)
        self.ok(basis=PRICE, value=2.0)
        self.ok(basis=H.SL_USD, usd=25.0)
        self.ok(basis=H.SL_STRUCT, length=10)
        self.ok(basis=H.SL_CHAND, value=300.0, atr_length=22, length=22)
        self.ok(basis=H.SL_MA, ma="MA 2")

    def test_each_choice_needs_its_own_boxes(self):
        self.bad(basis=H.SL_USD)                                          # no amount
        self.bad(basis=H.SL_USD, usd=0)
        self.bad(basis=H.SL_STRUCT)                                       # no N candles
        self.bad(basis=H.SL_STRUCT, length=0)
        self.bad(basis=H.SL_CHAND, value=300.0, length=22)                # no ATR length
        self.bad(basis=H.SL_CHAND, value=300.0, atr_length=22)            # no N candles
        self.bad(basis=H.SL_MA)                                           # no MA
        self.bad(basis=H.SL_MA, ma="MA 3")
        self.bad(basis="Something else", value=1.0)

    def test_time_stop_needs_n_candles(self):
        self.ok(basis=PRICE, value=2.0, time_on=True, time_candles=5)
        self.ok(basis=PRICE, value=2.0, time_on=False)
        self.bad(basis=PRICE, value=2.0, time_on=True)
        self.bad(basis=PRICE, value=2.0, time_on=True, time_candles=0)

    def test_old_plan_without_sl_is_still_readable(self):
        p = make_plan(ind_ma1={"entry": True})
        del p["sl"]
        H.validate_plan(p)
        self.assertFalse(H.upgrade_plan(p)["sl"]["on"])

    def test_sl_ma_needs_that_ma(self):
        p = H.complete_plan(plan_sl(basis=H.SL_MA, ma="MA 2"))
        self.assertIn("ma2", H._needed_blocks(p))


class TestSlLevel(unittest.TestCase):
    def lvl(self, d, sl, entry=100.0, atr=2.0, s=None, qty=None):
        return H.sl_level(d, entry, atr, H.complete_plan(plan_sl(**sl)), snap=s, qty=qty)

    def test_atr_and_price(self):
        self.assertAlmostEqual(self.lvl("LONG", dict(basis=ATR, value=150.0, atr_length=14)), 97.0)
        self.assertAlmostEqual(self.lvl("SHORT", dict(basis=PRICE, value=2.0)), 102.0)
        self.assertIsNone(self.lvl("LONG", dict(basis=ATR, value=150.0, atr_length=14), atr=float("nan")))

    def test_fixed_usd(self):
        # qty 2: losing 10 USDT = 5 price units
        self.assertAlmostEqual(self.lvl("LONG", dict(basis=H.SL_USD, usd=10.0), qty=2.0), 95.0)
        self.assertAlmostEqual(self.lvl("SHORT", dict(basis=H.SL_USD, usd=10.0), qty=2.0), 105.0)
        self.assertIsNone(self.lvl("LONG", dict(basis=H.SL_USD, usd=10.0)))           # qty unknown
        self.assertIsNone(self.lvl("LONG", dict(basis=H.SL_USD, usd=500.0), qty=2.0))  # below 0: cannot

    def test_structure(self):
        s = snap(sl_low=94.0, sl_high=107.0)
        self.assertAlmostEqual(self.lvl("LONG", dict(basis=H.SL_STRUCT, length=10), s=s), 94.0)
        self.assertAlmostEqual(self.lvl("SHORT", dict(basis=H.SL_STRUCT, length=10), s=s), 107.0)
        self.assertIsNone(self.lvl("LONG", dict(basis=H.SL_STRUCT, length=10), s=snap()))   # not ready (nan)
        self.assertIsNone(self.lvl("LONG", dict(basis=H.SL_STRUCT, length=10)))             # no candles

    def test_chandelier(self):
        s = snap(sl_low=94.0, sl_high=107.0)
        # LONG: highest high 107 - 300% x ATR 2 = 101 -> above the entry 100 = wrong side
        self.assertIsNone(self.lvl("LONG", dict(basis=H.SL_CHAND, value=300.0, atr_length=22, length=22), s=s))
        # LONG: 107 - 500% x 2 = 97
        self.assertAlmostEqual(self.lvl("LONG", dict(basis=H.SL_CHAND, value=500.0, atr_length=22, length=22),
                                        s=s), 97.0)
        # SHORT: lowest low 94 + 500% x 2 = 104
        self.assertAlmostEqual(self.lvl("SHORT", dict(basis=H.SL_CHAND, value=500.0, atr_length=22, length=22),
                                        s=s), 104.0)

    def test_moving_average_and_wrong_side(self):
        s = snap(ma1=96.0, ma2=103.0)
        self.assertAlmostEqual(self.lvl("LONG", dict(basis=H.SL_MA, ma="MA 1"), s=s), 96.0)
        self.assertAlmostEqual(self.lvl("SHORT", dict(basis=H.SL_MA, ma="MA 2"), s=s), 103.0)
        self.assertIsNone(self.lvl("LONG", dict(basis=H.SL_MA, ma="MA 2"), s=s))     # MA above a LONG entry
        self.assertIsNone(self.lvl("SHORT", dict(basis=H.SL_MA, ma="MA 1"), s=s))    # MA below a SHORT entry

    def test_sl_off_gives_none(self):
        p = H.complete_plan(make_plan(ind_ma1={"entry": True}, sl={"on": False}))
        self.assertIsNone(H.sl_level("LONG", 100.0, 2.0, p))


class TestTimeStop(unittest.TestCase):
    def test_rule(self):
        p = H.complete_plan(plan_sl(basis=PRICE, value=2.0, time_on=True, time_candles=3))
        self.assertFalse(H.time_stop_due("LONG", p, 2, 99.0, 100.0))       # not N candles yet
        self.assertTrue(H.time_stop_due("LONG", p, 3, 99.0, 100.0))        # N candles, losing
        self.assertTrue(H.time_stop_due("LONG", p, 3, 100.0, 100.0))       # N candles, flat = not in profit
        self.assertFalse(H.time_stop_due("LONG", p, 3, 101.0, 100.0))      # in profit: stays
        self.assertTrue(H.time_stop_due("SHORT", p, 4, 101.0, 100.0))
        self.assertFalse(H.time_stop_due("SHORT", p, 4, 99.0, 100.0))
        off = H.complete_plan(plan_sl(basis=PRICE, value=2.0))
        self.assertFalse(H.time_stop_due("LONG", off, 99, 50.0, 100.0))


class TestCompute(unittest.TestCase):
    def test_structure_extremes_on_normal_candles(self):
        n = 260
        rng = np.random.default_rng(3)
        c = 100 + np.cumsum(rng.normal(0, 1, n))
        df = pd.DataFrame({"open_time": np.arange(n) * 60_000, "open": c, "high": c + 1, "low": c - 1, "close": c,
                           "volume": np.ones(n)})
        p = H.complete_plan(plan_sl(basis=H.SL_STRUCT, length=7))
        f = H.compute_hub(df, p)
        self.assertAlmostEqual(f["sl_low"].iloc[-1], float(df["low"].iloc[-7:].min()))
        self.assertAlmostEqual(f["sl_high"].iloc[-1], float(df["high"].iloc[-7:].max()))
        s = H.hub_snapshot_at(f, n - 1)
        self.assertAlmostEqual(s.sl_low, float(df["low"].iloc[-7:].min()))


# ======================================================================================== shadow
class TestShadow(unittest.TestCase):
    def candle(self, book, s, plan):
        return B.on_candle(book, s, plan, tick_size=0.0, last_total=None, balance=1000.0, t_ms=s.open_time + 1)

    def test_no_sl_paper_trade_still_opens(self):
        plan = H.complete_plan(make_plan(ind_ma1={"entry": True, "flip": True}, sl={"on": False}))
        book = B.new_book(1000.0)
        ev, _ = self.candle(book, snap(1, dir_e=1, dir_f=1), plan)
        self.assertEqual(book["pos"], 1)
        self.assertIsNone(book["levels"]["sl"])
        self.assertTrue(any("No SL was set for this trade. Set it manually." in e for e in ev))

    def test_sl_not_worked_out_still_opens_with_a_note(self):
        plan = H.complete_plan(plan_sl(basis=H.SL_STRUCT, length=10))
        book = B.new_book(1000.0)
        ev, _ = self.candle(book, snap(1, dir_e=1, dir_f=1), plan)            # sl_low not ready
        self.assertEqual(book["pos"], 1)
        self.assertIsNone(book["levels"]["sl"])
        self.assertTrue(any("SL could not be worked out for this trade. Set it manually." in e for e in ev))

    def test_structure_and_fixed_usd_in_the_book(self):
        book = B.new_book(1000.0)
        self.candle(book, snap(1, dir_e=1, dir_f=1, sl_low=93.0), H.complete_plan(plan_sl(basis=H.SL_STRUCT,
                                                                                          length=10)))
        self.assertAlmostEqual(book["levels"]["sl"], 93.0)
        book = B.new_book(1000.0)
        # margin 1000 x 10% = 100, x5 = 500 USDT position = 5 units at 100; losing 10 USDT = 2 price units
        self.candle(book, snap(1, dir_e=1, dir_f=1), H.complete_plan(plan_sl(basis=H.SL_USD, usd=10.0)))
        self.assertAlmostEqual(book["levels"]["sl"], 98.0)

    def test_time_stop_closes_a_trade_not_in_profit(self):
        plan = H.complete_plan(plan_sl(basis=PRICE, value=50.0, time_on=True, time_candles=2))
        book = B.new_book(1000.0)
        self.candle(book, snap(1, dir_e=1, dir_f=1, close=100.0), plan)
        _, closed = self.candle(book, snap(2, dir_e=1, dir_f=1, close=99.0), plan)
        self.assertEqual((book["pos"], closed), (1, []))                      # 1 candle: stays
        _, closed = self.candle(book, snap(3, dir_e=1, dir_f=1, close=99.5), plan)
        self.assertEqual(book["pos"], 0)
        self.assertEqual(closed[0]["exit_reason"], "Time stop")

    def test_time_stop_keeps_a_trade_in_profit(self):
        plan = H.complete_plan(plan_sl(basis=PRICE, value=50.0, time_on=True, time_candles=1))
        book = B.new_book(1000.0)
        self.candle(book, snap(1, dir_e=1, dir_f=1, close=100.0), plan)
        self.candle(book, snap(2, dir_e=1, dir_f=1, close=101.0), plan)
        self.candle(book, snap(3, dir_e=1, dir_f=1, close=102.0), plan)
        self.assertEqual(book["pos"], 1)


# ======================================================================================== the bot
class TestBot(T.Base):
    async def test_no_sl_trade_opens_and_alerts(self):
        inst = self.make(make_plan(ind_ma1={"entry": True, "flip": True}, sl={"on": False}))
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em, \
                mock.patch("app.hub_trading.tg.notify_info", new=mock.AsyncMock(return_value=None)) as ni:
            em.send = mock.AsyncMock()
            await self.tick(inst, T.snap_of(1000, dir_e=1, dir_f=1))
            self.assertEqual(len(inst.client.market_orders), 1)                 # the trade opens
            self.assertIsNone(inst.hub.ladder["sl"])
            self.assertEqual(em.send.await_count, 1)
            subject, body = em.send.await_args.args
            self.assertIn("SL Alert", subject)
            self.assertIn("No SL was set for this trade. Set it manually.", body)
            self.assertEqual(ni.await_count, 1)
        self.assertEqual(len(inst.client.stop_orders), 1)                       # the Max Loss still rests

    async def test_sl_not_worked_out_opens_and_alerts(self):
        inst = self.make(plan_sl(basis=H.SL_STRUCT, length=10))
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em, \
                mock.patch("app.hub_trading.tg.notify_info", new=mock.AsyncMock(return_value=None)) as ni:
            em.send = mock.AsyncMock()
            await self.tick(inst, T.snap_of(1000, dir_e=1, dir_f=1))           # swing low not ready (nan)
            self.assertEqual(len(inst.client.market_orders), 1)                 # the trade still opens
            self.assertIsNone(inst.hub.ladder["sl"])
            self.assertEqual(em.send.await_count, 1)
            subject, body = em.send.await_args.args
            self.assertIn("SL Alert", subject)
            self.assertIn("SL could not be worked out for this trade. Set it manually.", body)
            self.assertEqual(ni.await_count, 1)

    async def test_structure_sl_is_set_at_entry(self):
        inst = self.make(plan_sl(basis=H.SL_STRUCT, length=10))
        inst.client.mark_price = 100.0
        with mock.patch("app.hub_trading.email_notifier") as em:
            em.send = mock.AsyncMock()
            await self.tick(inst, snap(1000, dir_e=1, dir_f=1, sl_low=95.0))
            self.assertEqual(em.send.await_count, 0)
        self.assertAlmostEqual(inst.hub.ladder["sl"], 95.0)

    async def test_fixed_usd_sl_uses_the_real_qty(self):
        inst = self.make(plan_sl(basis=H.SL_USD, usd=50.0))
        inst.client.mark_price = 100.0
        await self.tick(inst, T.snap_of(1000, dir_e=1, dir_f=1))
        qty = inst.state.qty
        self.assertGreater(qty, 0)
        self.assertAlmostEqual(inst.hub.ladder["sl"], 100.0 - 50.0 / qty, places=6)

    async def test_time_stop_closes_at_the_candle_close(self):
        plan = plan_sl(basis=PRICE, value=50.0, time_on=True, time_candles=2)
        inst = self.make(plan)
        tf = CANDLE_MS[inst.pc.timeframe]
        self.go_long(inst, price=100.0)
        inst.state.entry_candle_time = 10 * tf
        inst.client.mark_price = 99.0
        await self.tick(inst, T.snap_of(11 * tf, dir_e=1, dir_f=1, close=99.0))
        self.assertEqual(inst.client.close_orders, [])                         # 1 candle: stays
        await self.tick(inst, T.snap_of(12 * tf, dir_e=1, dir_f=1, close=99.0))
        self.assertEqual(len(inst.client.close_orders), 1)                     # 2 candles, not in profit
        self.assertIn("TIME STOP", inst.state.last_decision or "")

    async def test_time_stop_keeps_a_trade_in_profit(self):
        inst = self.make(plan_sl(basis=PRICE, value=50.0, time_on=True, time_candles=1))
        tf = CANDLE_MS[inst.pc.timeframe]
        self.go_long(inst, price=100.0)
        inst.state.entry_candle_time = 10 * tf
        inst.client.mark_price = 101.0
        await self.tick(inst, T.snap_of(12 * tf, dir_e=1, dir_f=1, close=101.0))
        self.assertEqual(inst.client.close_orders, [])


class TestAnalysis(unittest.TestCase):
    def test_backtest_runs_a_plan_without_sl(self):
        from app import hub_backtest as BT
        n = 300
        c = 100 + np.cumsum(np.random.default_rng(2).normal(0, 1, n))
        df = pd.DataFrame({"open_time": np.arange(n) * 60_000, "open": c, "high": c + 1, "low": c - 1, "close": c,
                           "volume": np.ones(n)})
        res = BT.run(df, make_plan(ind_ma1={"entry": True, "flip": True}, sl={"on": False}), 1000.0, 0.05, 0.02)
        self.assertGreater(len(res["trades"]), 0)


class TestSaveWithoutSl(unittest.IsolatedAsyncioTestCase):
    async def test_save_without_sl_is_allowed(self):
        from tests import _test_values as tv
        p = make_plan(ind_ma1={"entry": True}, sl={"on": False})
        store = mock.MagicMock()
        store.update_pair.return_value = tv.pair_config(symbol="BTCUSDT", enabled=True, plan=p)
        mgr = mock.MagicMock()
        mgr.get.return_value = None
        with mock.patch.object(hub_api, "_old_plan_to_history"):
            out = await hub_api.apply_plan(store, mgr, "a", "BTCUSDT", p)
        self.assertFalse(out["plan"]["sl"]["on"])
        store.update_pair.assert_called_once()


if __name__ == "__main__":
    unittest.main()
