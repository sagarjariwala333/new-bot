"""Arbitrage Step 2: the mock engines (hand-calculated numbers from the standalone bot's own tests) and the driver."""
import asyncio
import copy
import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import tests._aiohttp_stub  # noqa: F401
from app import api_guard
from app.arb import scanner as sc
from app.arb import settings as st
from app.arb.context import Ctx
from app.arb.funding_engine import Funding
from app.arb.instruments import Instr
from app.arb.mock_broker import MockBroker
from app.arb.spatial_engine import Spatial
from app.arb.store import ArbStateError, Journal, Risk
from app.arb.trader import ArbTrader
from app.arb.triangular_engine import Triangular


def base_settings(tmp, **over):
    s = copy.deepcopy(st.DEFAULTS)
    s["data_folder"] = os.path.join(tmp, "data")
    s["pairs"]["min_top_size_usdt"] = 0
    s["risk"].update(max_size_per_trade_usdt=0, daily_loss_limit_usdt=0, kill_switch_file="STOP.txt")
    for k, v in over.items():
        cur = s
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur[p]
        cur[parts[-1]] = v
    return s


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_ctx(tmp, s, fee_spot=0.001, fee_fut=0.0005):
    os.makedirs(s["data_folder"], exist_ok=True)
    log = logging.getLogger("t_arb_mock")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.propagate = False
    clock = Clock()
    ctx = Ctx(s, log, Risk(s["risk"], s["data_folder"], log), Journal(s["data_folder"]), clock)
    ctx.exchanges = ["binance", "okx"]
    for ex in ctx.exchanges:
        ctx.fees[(ex, "spot")] = {"maker": fee_spot, "taker": fee_spot}
        ctx.fees[(ex, "futures")] = {"maker": fee_fut, "taker": fee_fut}
    return ctx, clock


def add_instr(ctx, ex, market, base, quote="USDT", step="0.001", minq="0.001", tick="0.01", minnot="5"):
    sym = Ctx.symbol(ex, market, base) if quote == "USDT" else \
        (base + quote if ex == "binance" else "%s-%s" % (base, quote))
    ctx.instr.setdefault((ex, market), {})[sym] = Instr(ex, market, sym, base, quote, step, minq, tick, minnot, 1)
    return sym


def set_book(ctx, ex, market, sym, bid, ask, bq=10.0, aq=10.0):
    ctx.books.setdefault((ex, market), {})[sym] = {"bid": bid, "ask": ask, "bq": bq, "aq": aq}


def mock_factory(ctx, cfg, spot_inventory=None):
    def make(st_):
        inv = cfg.get("mock_coin_inventory_usdt", 0) if spot_inventory is None else spot_inventory
        return MockBroker(st_.wallets, lambda ex, m: ctx.fee(ex, m), cfg.get("slippage_pct", 0),
                          cfg.get("leverage", 1), {e: cfg.get("mock_start_usdt", 0) for e in ctx.exchanges}, inv)
    return make


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arbt_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, ctx):
        return ctx.journal.rows()

    def near(self, got, want, tol=1e-6):
        self.assertLessEqual(abs(got - want), tol, "got %r want %r" % (got, want))


class SpatialFuturesTests(Base):
    def setup_engine(self, **over):
        s = base_settings(self.tmp, **over)
        ctx, clock = make_ctx(self.tmp, s)
        ctx.universe["futures"] = ["BTC"]
        self.sb = add_instr(ctx, "binance", "futures", "BTC")
        self.so = add_instr(ctx, "okx", "futures", "BTC")
        cfg = s["modules"]["spatial"]
        eng = Spatial("futures", "mock", cfg, ctx, mock_factory(ctx, cfg))
        set_book(ctx, "binance", "futures", self.sb, 99990, 100000)
        set_book(ctx, "okx", "futures", self.so, 100400, 100410)
        return ctx, clock, eng

    def test_open_then_close_with_profit(self):
        ctx, clock, eng = self.setup_engine(**{"modules.spatial.slippage_pct": 0,
                                               "modules.spatial.trade_size_usdt": 1000,
                                               "modules.spatial.leverage": 5})
        eng.step(clock())
        self.assertEqual(len(eng.pos), 1)
        p = eng.pos[0]
        self.near(p["legs"][0]["qty0"], 0.01)
        self.near(p["legs"][0]["fee"], 0.5)
        self.near(p["legs"][1]["fee"], 0.502)
        self.near(eng.broker.w["fpos"]["binance|BTC"]["margin"], 0.01 * 100000 / 5)
        eng.step(clock())
        self.assertEqual(len(eng.pos), 1, "not duplicated")
        set_book(ctx, "binance", "futures", self.sb, 100200, 100210)
        set_book(ctx, "okx", "futures", self.so, 100190, 100200)
        eng.step(clock())
        self.assertEqual(len(eng.pos), 0)
        rows = self.rows(ctx)
        self.assertEqual(len(rows), 1)
        self.near(float(rows[0]["net_pnl_usdt"]), 1.996)
        self.assertEqual(rows[0]["reason"], "take_profit")
        self.near(float(rows[0]["fees_usdt"]), 2.004)
        self.near(eng.broker.total_usdt() - 2 * 5000, 1.996)
        self.assertFalse(eng.broker.w["fpos"])
        self.assertEqual(rows[0]["mode"], "mock")

    def test_stop_loss(self):
        ctx, clock, eng = self.setup_engine(**{"modules.spatial.trade_size_usdt": 1000,
                                               "modules.spatial.stop_loss_pct": 0.3})
        eng.step(clock())
        set_book(ctx, "binance", "futures", self.sb, 99490, 99500)
        set_book(ctx, "okx", "futures", self.so, 100900, 100910)
        eng.step(clock())
        rows = self.rows(ctx)
        self.assertEqual([r["reason"] for r in rows], ["stop_loss"])

    def test_max_hold(self):
        ctx, clock, eng = self.setup_engine(**{"modules.spatial.max_hold_seconds": 60,
                                               "modules.spatial.trade_size_usdt": 1000})
        eng.step(clock())
        clock.t += 30
        eng.step(clock())
        self.assertEqual(len(eng.pos), 1)
        clock.t += 31
        eng.step(clock())
        self.assertEqual([r["reason"] for r in self.rows(ctx)], ["max_hold"])

    def test_shrinks_to_top_size(self):
        ctx, clock, eng = self.setup_engine(**{"modules.spatial.trade_size_usdt": 1000})
        set_book(ctx, "binance", "futures", self.sb, 99990, 100000, aq=0.004)
        eng.step(clock())
        self.assertEqual(len(eng.pos), 1)
        self.assertLess(abs(eng.pos[0]["legs"][0]["qty0"] - 0.004), 1e-12)

    def test_thin_book_filter(self):
        ctx, clock, eng = self.setup_engine(**{"pairs.min_top_size_usdt": 500})
        set_book(ctx, "binance", "futures", self.sb, 99990, 100000, aq=0.004)
        eng.step(clock())
        self.assertFalse(eng.pos)

    def test_below_min_qty_skipped(self):
        ctx, clock, eng = self.setup_engine()
        set_book(ctx, "binance", "futures", self.sb, 99990, 100000, aq=0.0004)
        eng.step(clock())
        self.assertFalse(eng.pos)

    def test_board_sorted_and_threshold(self):
        s = base_settings(self.tmp)
        ctx, clock = make_ctx(self.tmp, s)
        ctx.universe["futures"] = ["BTC", "ETH"]
        for b in ("BTC", "ETH"):
            add_instr(ctx, "binance", "futures", b)
            add_instr(ctx, "okx", "futures", b)
        cfg = s["modules"]["spatial"]
        eng = Spatial("futures", "mock", cfg, ctx, mock_factory(ctx, cfg))
        set_book(ctx, "binance", "futures", "BTCUSDT", 99990, 100000)
        set_book(ctx, "okx", "futures", "BTC-USDT-SWAP", 100400, 100410)
        set_book(ctx, "binance", "futures", "ETHUSDT", 3000, 3000.5)
        set_book(ctx, "okx", "futures", "ETH-USDT-SWAP", 2999, 2999.5)
        eng.step(clock())
        self.assertGreaterEqual(eng.board[0]["net"], eng.board[1]["net"])
        self.assertEqual(eng.board[0]["base"], "BTC")
        self.assertEqual([p["base"] for p in eng.pos], ["BTC"])

    def test_kill_switch_stops_entries_only(self):
        ctx, clock, eng = self.setup_engine(**{"modules.spatial.trade_size_usdt": 1000})
        Path(ctx.risk.kill_file).write_text("stop")
        eng.step(clock())
        self.assertFalse(eng.pos, "no new trade while the STOP file exists")
        os.remove(ctx.risk.kill_file)
        eng.step(clock())
        self.assertEqual(len(eng.pos), 1)
        Path(ctx.risk.kill_file).write_text("stop")
        set_book(ctx, "binance", "futures", self.sb, 100200, 100210)
        set_book(ctx, "okx", "futures", self.so, 100190, 100200)
        eng.step(clock())
        self.assertEqual(len(eng.pos), 0, "exits still work")


class SpatialSpotTests(Base):
    def test_instant_swap(self):
        s = base_settings(self.tmp, **{"modules.spatial.trade_size_usdt": 1000,
                                       "modules.spatial.mock_coin_inventory_usdt": 5000,
                                       "modules.spatial.slippage_pct": 0})
        ctx, clock = make_ctx(self.tmp, s)
        ctx.universe["spot"] = ["BTC"]
        sb = add_instr(ctx, "binance", "spot", "BTC")
        so = add_instr(ctx, "okx", "spot", "BTC")
        cfg = s["modules"]["spatial"]
        eng = Spatial("spot", "mock", cfg, ctx, mock_factory(ctx, cfg))
        set_book(ctx, "binance", "spot", sb, 99990, 100000)
        set_book(ctx, "okx", "spot", so, 100300, 100310)
        eng.step(clock())
        rows = self.rows(ctx)
        self.assertEqual(len(rows), 1)
        self.assertFalse(eng.pos)
        self.near(float(rows[0]["net_pnl_usdt"]), 0.997)
        self.near(float(rows[0]["fees_usdt"]), 2.003)
        w = eng.broker.w["bal"]
        self.near(w["binance|spot"]["USDT"], 5000 - 1000 - 1.0)
        self.near(w["okx|spot"]["USDT"], 5000 + 1003 - 1.003)
        self.near(w["binance|spot"]["BTC"], 5000 / 100000 + 0.01, 1e-9)
        self.near(w["okx|spot"]["BTC"], 5000 / 100300 - 0.01, 1e-9)

    def test_blocked_without_coins_to_sell(self):
        s = base_settings(self.tmp, **{"modules.spatial.trade_size_usdt": 1000,
                                       "modules.spatial.mock_coin_inventory_usdt": 500})
        ctx, clock = make_ctx(self.tmp, s)
        ctx.universe["spot"] = ["BTC"]
        sb = add_instr(ctx, "binance", "spot", "BTC")
        so = add_instr(ctx, "okx", "spot", "BTC")
        cfg = s["modules"]["spatial"]
        eng = Spatial("spot", "mock", cfg, ctx, mock_factory(ctx, cfg))
        set_book(ctx, "binance", "spot", sb, 99990, 100000)
        set_book(ctx, "okx", "spot", so, 100300, 100310)
        eng.step(clock())
        self.assertFalse(self.rows(ctx))


class FundingTests(Base):
    def test_full_cycle(self):
        s = base_settings(self.tmp, **{"modules.funding.trade_size_usdt": 1000,
                                       "modules.funding.exit_funding_pct": 0.02, "modules.funding.stop_loss_pct": 0})
        ctx, clock = make_ctx(self.tmp, s)
        ctx.exchanges = ["binance"]
        ctx.funding_universe = {"binance": ["BTC"]}
        ssp = add_instr(ctx, "binance", "spot", "BTC")
        sfu = add_instr(ctx, "binance", "futures", "BTC")
        cfg = s["modules"]["funding"]
        eng = Funding("mock", cfg, ctx, mock_factory(ctx, cfg, 0))
        now_ms = int(clock() * 1000)
        ctx.funding = {"binance": {"BTC": {"rate": 0.0005, "next_time_ms": now_ms + 1000}}}
        set_book(ctx, "binance", "spot", ssp, 99990, 100000)
        set_book(ctx, "binance", "futures", sfu, 100050, 100060)
        eng.step(clock())
        self.assertFalse(eng.pos, "break-even 6 periods > 5")
        ctx.funding = {"binance": {"BTC": {"rate": 0.001, "next_time_ms": now_ms + 1000}}}
        eng.step(clock())
        self.assertEqual(len(eng.pos), 1)
        p = eng.pos[0]
        self.near(p["legs"][0]["fee"], 1.0, 1e-9)
        self.near(p["legs"][1]["fee"], 0.50025, 1e-9)
        clock.t += 2
        eng.step(clock())
        self.near(eng.pos[0]["funding"], 1.00055, 1e-9)
        ctx.funding["binance"]["BTC"]["next_time_ms"] = now_ms + 8 * 3600 * 1000
        clock.t += 8 * 3600
        eng.step(clock())
        self.near(eng.pos[0]["funding"], 2.0011, 1e-9)
        ctx.funding["binance"]["BTC"] = {"rate": 0.0001, "next_time_ms": now_ms + 16 * 3600 * 1000}
        set_book(ctx, "binance", "spot", ssp, 100010, 100020)
        set_book(ctx, "binance", "futures", sfu, 100010, 100020)
        eng.step(clock())
        rows = self.rows(ctx)
        self.assertEqual(len(rows), 1)
        self.assertFalse(eng.pos)
        self.assertEqual(rows[0]["reason"], "funding_low")
        want = 0.1 + 0.3 + 2.0011 - (1.0 + 0.50025 + 0.01 * 100010 * 0.001 + 0.01 * 100020 * 0.0005)
        self.near(float(rows[0]["net_pnl_usdt"]), want)
        self.near(float(rows[0]["funding_usdt"]), 2.0011)


class TriangularTests(Base):
    def test_cycle(self):
        s = base_settings(self.tmp, **{"modules.triangular.trade_size_usdt": 100,
                                       "modules.triangular.slippage_pct": 0, "modules.triangular.enabled": True})
        ctx, clock = make_ctx(self.tmp, s)
        ctx.exchanges = ["binance"]
        ctx.tri_bases = {"binance": ["XRP"]}
        add_instr(ctx, "binance", "spot", "BTC", step="0.00001", minq="0.00001", minnot="5")
        add_instr(ctx, "binance", "spot", "XRP", step="0.1", minq="0.1", tick="0.0001", minnot="5")
        add_instr(ctx, "binance", "spot", "XRP", quote="BTC", step="0.1", minq="0.1", tick="1e-8", minnot="0.0001")
        cfg = s["modules"]["triangular"]
        eng = Triangular("mock", cfg, ctx, mock_factory(ctx, cfg, 0))
        set_book(ctx, "binance", "spot", "BTCUSDT", 100000, 100010)
        set_book(ctx, "binance", "spot", "XRPUSDT", 0.4995, 0.5000, bq=1e6, aq=1e6)
        set_book(ctx, "binance", "spot", "XRPBTC", 0.0000051, 0.00000511, bq=1e6, aq=1e6)
        eng.step(clock())
        self.near(eng.board[0]["net"], (100000 * 0.0000051 / 0.5 * 0.997002999 - 1) * 100)
        self.assertEqual(eng.board[0]["dir"], "fwd")
        rows = self.rows(ctx)
        self.assertEqual(len(rows), 1)
        fees = 0.1 + 0.00000102 * 100005 + 0.101
        self.near(float(rows[0]["net_pnl_usdt"]), (101.0 - 100.0) - fees)
        self.assertFalse(eng.pos)
        w = eng.broker.w["bal"]["binance|spot"]
        self.assertTrue(0 <= w.get("BTC", 0) < 0.00002)
        self.assertLess(abs(w.get("XRP", 0)), 1e-9)
        self.near(w["USDT"], 1000 - 100.1 + 101.0 - 0.101)


class StoreTests(Base):
    def test_damaged_state_file_is_an_error_not_a_crash(self):
        folder = os.path.join(self.tmp, "d")
        os.makedirs(folder)
        Path(os.path.join(folder, "state_x.json")).write_text("{broken")
        from app.arb.store import EngineState
        with self.assertRaises(ArbStateError):
            EngineState(folder, "x")

    def test_risk_blank_limits_and_kill_path(self):
        r = Risk({"max_size_per_trade_usdt": None, "daily_loss_limit_usdt": None, "kill_switch_file": "STOP.txt"},
                 self.tmp, logging.getLogger("x"))
        self.assertEqual((r.max_size, r.loss_limit), (0.0, 0.0))
        self.assertEqual(r.kill_file, os.path.join(self.tmp, "STOP.txt"))
        self.assertTrue(r.entry_allowed("mock")[0])

    def test_mock_losses_never_count_toward_daily_limit(self):
        r = Risk({"max_size_per_trade_usdt": 0, "daily_loss_limit_usdt": 10, "kill_switch_file": "STOP.txt"},
                 self.tmp, logging.getLogger("x"))
        r.record(-500, "mock")
        self.assertTrue(r.entry_allowed("mock")[0])
        self.assertEqual(r.live_pnl, 0.0)


# ---------------------------------------------------------------- driver: scanner prices -> engines
class FakeScanner:
    def __init__(self):
        self.rules, self.books, self.funding, self.volumes = {}, {}, {}, {}
        self.universe = {"spatial": {}, "funding": {}, "triangular": {}}
        self.stamp = 1
        self.age = 0.0

    def book_age(self, key):
        return self.age


class TraderTests(Base):
    def make(self):
        tr = ArbTrader(os.path.join(self.tmp, "arb"))
        sc_ = FakeScanner()
        sc_.rules = {("binance", "futures"): {"BTCUSDT": Instr("binance", "futures", "BTCUSDT", "BTC", "USDT", "0.001", "0.001", "0.1", 5, 1)},
                     ("okx", "futures"): {"BTC-USDT-SWAP": Instr("okx", "futures", "BTC-USDT-SWAP", "BTC", "USDT", "0.01", "0.01", "0.1", 0, "0.01")}}
        sc_.universe["spatial"] = {"futures": ["BTC"]}
        sc_.books = {("binance", "futures"): {"BTCUSDT": {"bid": 99990.0, "ask": 100000.0, "bq": 10, "aq": 10}},
                     ("okx", "futures"): {"BTC-USDT-SWAP": {"bid": 100400.0, "ask": 100410.0, "bq": 10, "aq": 10}}}
        s = copy.deepcopy(st.DEFAULTS)
        s["modules"]["spatial"].update(markets=["futures"], trade_size_usdt=1000, slippage_pct=0)
        s["modules"]["funding"]["enabled"] = False
        return tr, sc_, s

    def test_only_mock_modules_run_and_triangular_needs_its_lock(self):
        _, _, s = self.make()
        s["modules"]["triangular"]["enabled"] = True
        names = [w[0] for w in ArbTrader.wanted(s)]
        self.assertNotIn("triangular", names)
        s["modules"]["triangular"]["confirm_enable"] = True
        self.assertIn("triangular", [w[0] for w in ArbTrader.wanted(s)])
        s["modules"]["spatial"]["mode"] = "live"
        modes = [w[3] for w in ArbTrader.wanted(s) if w[0] == "spatial"]
        self.assertEqual(modes, ["live", "live-mock"])
        s["live_mock"]["enabled"] = False
        modes = [w[3] for w in ArbTrader.wanted(s) if w[0] == "spatial"]
        self.assertEqual(modes, ["live"])

    def test_spatial_needs_both_exchanges(self):
        _, _, s = self.make()
        s["exchanges"]["okx"]["enabled"] = False
        self.assertNotIn("spatial", [w[0] for w in ArbTrader.wanted(s)])

    def test_enters_and_shows_in_snapshot(self):
        tr, scn, s = self.make()
        tr.step(s, scn)
        snap = tr.snapshot()
        self.assertEqual(len(snap["positions"]), 1)
        p = snap["positions"][0]
        self.assertEqual((p["module"], p["base"], p["kind"]), ("spatial", "BTC", "open"))
        self.assertIsNotNone(p["net_now"])
        self.assertEqual(snap["engines"][0]["open"], 1)
        self.assertEqual(snap["engines"][0]["mode"], "mock")
        # okx 1 contract = 0.01 BTC: quantity in coins
        self.assertTrue(any("okx" in l for l in p["legs"]))

    def test_waits_until_prices_of_both_sides_exist(self):
        tr, scn, s = self.make()
        del scn.books[("okx", "futures")]
        tr.step(s, scn)
        self.assertEqual(tr.snapshot()["positions"], [])

    def test_old_prices_are_never_traded_on(self):
        tr, scn, s = self.make()
        scn.age = 500.0
        tr.step(s, scn)
        self.assertEqual(tr.snapshot()["positions"], [])
        scn.age = 1.0
        tr.step(s, scn)
        self.assertEqual(len(tr.snapshot()["positions"]), 1)

    def test_close_gives_trade_row_and_daily_result(self):
        tr, scn, s = self.make()
        tr.step(s, scn)
        scn.books[("binance", "futures")]["BTCUSDT"].update(bid=100200.0, ask=100210.0)
        scn.books[("okx", "futures")]["BTC-USDT-SWAP"].update(bid=100190.0, ask=100200.0)
        tr.step(s, scn)
        snap = tr.snapshot()
        self.assertEqual(snap["positions"], [])
        self.assertEqual(len(snap["trades"]), 1)
        self.assertEqual(snap["trades"][0]["reason"], "take_profit")
        self.assertEqual(len(snap["daily"]), 1)
        self.assertEqual((snap["daily"][0]["module"], snap["daily"][0]["trades"], snap["daily"][0]["wins"]),
                         ("spatial", 1, 1))
        self.assertAlmostEqual(snap["daily"][0]["net"], snap["engines"][0]["net_pnl_usdt"], places=5)

    def test_switching_module_off_pauses_it_and_keeps_position(self):
        tr, scn, s = self.make()
        tr.step(s, scn)
        s["modules"]["spatial"]["enabled"] = False
        tr.step(s, scn)
        snap = tr.snapshot()
        self.assertFalse(snap["engines"][0]["active"])
        self.assertEqual(len(snap["positions"]), 1)
        s["modules"]["spatial"]["enabled"] = True
        tr.step(s, scn)
        self.assertTrue(tr.snapshot()["engines"][0]["active"])

    def test_settings_change_rebuilds_but_keeps_position(self):
        tr, scn, s = self.make()
        tr.step(s, scn)
        s["modules"]["spatial"]["leverage"] = 7
        tr.step(s, scn)
        snap = tr.snapshot()
        self.assertEqual(len(snap["positions"]), 1, "same position, not a second one")
        self.assertEqual(snap["engines"][0]["open"], 1)

    def test_restart_resumes_saved_position(self):
        tr, scn, s = self.make()
        tr.step(s, scn)
        tr2 = ArbTrader(os.path.join(self.tmp, "arb"))
        tr2.step(s, scn)
        self.assertEqual(len(tr2.snapshot()["positions"]), 1)

    def test_reset_clears_everything_mock(self):
        tr, scn, s = self.make()
        tr.step(s, scn)
        folder = os.path.join(self.tmp, "arb")
        self.assertTrue([f for f in os.listdir(folder) if f.startswith("state_")])
        removed = tr.reset()
        self.assertGreaterEqual(removed, 1)
        self.assertFalse([f for f in os.listdir(folder) if f.startswith("state_")])
        self.assertEqual(tr.snapshot()["engines"], [])
        tr.step(s, scn)          # starts fresh
        self.assertEqual(tr.snapshot()["engines"][0]["trades"], 0)

    def test_damaged_state_is_reported_and_other_engines_continue(self):
        tr, scn, s = self.make()
        os.makedirs(os.path.join(self.tmp, "arb"))
        Path(os.path.join(self.tmp, "arb", "state_spatial_futures_mock.json")).write_text("{bad")
        tr.step(s, scn)
        snap = tr.snapshot()
        self.assertTrue(any("damaged" in p for p in snap["problems"]), snap["problems"])
        self.assertEqual(snap["positions"], [])

    def test_funding_coin_is_always_read_while_a_trade_is_open(self):
        tr, scn, s = self.make()
        s["modules"]["spatial"]["enabled"] = False
        s["modules"]["funding"]["enabled"] = True
        s["exchanges"]["okx"]["enabled"] = False
        scn.rules = {("binance", "spot"): {"BTCUSDT": Instr("binance", "spot", "BTCUSDT", "BTC", "USDT", "0.001", "0.001", "0.01", 5, 1)},
                     ("binance", "futures"): {"BTCUSDT": Instr("binance", "futures", "BTCUSDT", "BTC", "USDT", "0.001", "0.001", "0.1", 5, 1)}}
        scn.universe["funding"] = {"binance": ["BTC"]}
        scn.books = {("binance", "spot"): {"BTCUSDT": {"bid": 99990.0, "ask": 100000.0, "bq": 10, "aq": 10}},
                     ("binance", "futures"): {"BTCUSDT": {"bid": 100050.0, "ask": 100060.0, "bq": 10, "aq": 10}}}
        import time as _t
        scn.funding = {"binance": {"BTC": {"rate": 0.002, "next_time_ms": int(_t.time() * 1000) + 3_600_000}}}
        s["modules"]["funding"].update(trade_size_usdt=1000)
        tr.step(s, scn)
        self.assertEqual(tr.funding_bases(), {"BTC"})



class ScannerToTraderTests(Base):
    """Whole chain with a fake exchange: scan -> prices + trading rules -> mock engines -> results."""

    def test_scan_opens_mock_trades_on_real_shaped_data(self):
        from tests.test_arb import FakeSession, router
        api_guard.reset_guards_for_tests()
        for name, lim in (("binance", 2400), ("binance-spot", 6000)):
            api_guard.get_guard(name, lim)._jitter = False
        api_guard.okx_guard(False)._jitter = False
        s = copy.deepcopy(st.DEFAULTS)
        s["scanner_on"] = True
        s["modules"]["funding"]["enabled"] = False
        s["modules"]["spatial"]["markets"] = ["futures"]
        sess = FakeSession(router)

        async def factory():
            return sess
        scn = sc.ArbScanner(lambda: s, factory)
        tr = ArbTrader(os.path.join(self.tmp, "arb"))
        scn.on_scan, scn.extra_funding = tr.step, tr.funding_bases

        async def go():
            await scn.scan_once(s)
            await scn.scan_once(s)
        asyncio.run(go())
        snap = tr.snapshot()
        pos = snap["positions"]
        self.assertTrue(pos, snap)
        btc = [p for p in pos if p["base"] == "BTC"]
        self.assertEqual(len(btc), 1, "one position per coin, not one per scan")
        self.assertEqual(btc[0]["module"], "spatial")
        self.assertTrue(btc[0]["legs"][0].startswith("buy binance") or btc[0]["legs"][1].startswith("buy binance"))
        # only GET requests were made: mock trading never sends an order
        self.assertTrue(all(c[0].startswith("https://") for c in sess.calls))

    def test_engine_gap_equals_scanner_board_gap(self):
        """The engine's own board and the Step 1 board use the same maths: same number."""
        books = {("binance", "futures"): {"BTCUSDT": {"bid": 99.9, "ask": 100.0, "bq": 5, "aq": 5}},
                 ("okx", "futures"): {"BTC-USDT-SWAP": {"bid": 100.6, "ask": 100.7, "bq": 4, "aq": 4}}}
        s = base_settings(self.tmp, **{"modules.spatial.slippage_pct": 0.01})
        ctx, clock = make_ctx(self.tmp, s, fee_spot=0.001, fee_fut=0.0005)
        ctx.universe["futures"] = ["BTC"]
        ctx.books = books
        cfg = s["modules"]["spatial"]
        eng = Spatial("futures", "mock", cfg, ctx, mock_factory(ctx, cfg))
        mine = eng.compute_edges()
        theirs = sc.spatial_edges(["BTC"], books, "futures", "binance", "okx", 0.0005, 0.0005, 0.01)
        self.assertEqual(len(mine), len(theirs))
        for a, b in zip(mine, theirs):
            self.assertAlmostEqual(a["net"], b["net"], places=9)
            self.assertAlmostEqual(a["gap"], b["gap"], places=9)


if __name__ == "__main__":
    unittest.main()
