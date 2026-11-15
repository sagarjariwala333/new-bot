"""Step 3 tests: live broker (fake in-memory clients), live gates, keys, hub-skip, locks, mock-only reset.
Nothing here talks to a real exchange."""
import copy
import json
import logging
import os
import tempfile
import unittest

import tests._aiohttp_stub  # noqa: F401
from app.arb import settings as st
from app.arb.fills import Leg
from app.arb.instruments import Instr
from app.arb.keys import ArbKeyStore, KeyError_
from app.arb.live_broker import LiveBroker, LiveRules
from app.arb.live_http import ApiError
from app.arb.trader import ArbTrader, live_blockers, needed_clients

LOG = logging.getLogger("t")


class FakeClient:
    """Same calls as BinanceClient / OKXClient, all in memory."""

    def __init__(self, ex, market, usdt=10000.0, base_coins=None, positions=None, fill_ratio=1.0, key="K1"):
        self.name, self.market, self.testnet, self.api_key = ex, market, False, key
        self.tag = "%s-%s[fake]" % (ex, market)
        self.usdt, self.coins, self.pos = usdt, dict(base_coins or {}), dict(positions or {})
        self.fill_ratio = fill_ratio
        self.calls, self.orders, self.fail_place, self.px = [], [], None, {}

    def get_balances(self, assets=None):
        out = {"USDT": {"total": self.usdt, "available": self.usdt}}
        for a, q in self.coins.items():
            out[a] = {"total": q, "available": q}
        return out

    def open_positions(self):
        return dict(self.pos)

    def one_way_mode(self):
        return True

    def set_isolated(self, ins):
        self.calls.append(("isolated", ins.symbol))

    def set_leverage(self, ins, lev):
        self.calls.append(("leverage", ins.symbol, lev))

    def get_fees(self, ref_symbol="BTCUSDT"):
        return {"maker": 0.0002, "taker": 0.0005}

    def place(self, ins, side, qty, otype, price=None, tif="GTC", reduce_only=False, cl_id=None):
        if self.fail_place:
            raise self.fail_place
        self.orders.append(dict(symbol=ins.symbol, side=side, qty=float(qty), type=otype, price=price,
                                reduce_only=reduce_only))
        filled = float(qty) * self.fill_ratio
        return {"id": str(len(self.orders)), "filled": filled, "avg": self.px.get(ins.symbol, 100.0), "net": None, "open": False,
                "state": "FILLED"}

    def get_order(self, ins, side, order_id=None, cl_id=None):
        return {"id": order_id or "x", "filled": 0.0, "avg": 0.0, "net": None, "open": False, "state": "CANCELED"}

    def cancel(self, ins, order_id):
        pass


def ins(ex="binance", market="futures", sym="ETHUSDT", base="ETH"):
    return Instr(ex, market, sym, base, "USDT", "0.001", "0.001", "0.01", 5, 1)


def leg(ex="binance", market="futures", side="sell", qty=1.0, px=100.0, closing=False, sym="ETHUSDT", base="ETH", top=10):
    return Leg(ex, market, base, side, qty, px, ins(ex, market, sym, base), top, closing)


def broker(clients, rules=None, orders=None, lev=2):
    o = {"type": "market", "exit_type": "market", "sequence": "simultaneous", "poll_ms": 10, "wait_seconds": 0.05}
    o.update(orders or {})
    alerts = []
    b = LiveBroker(clients, lambda ex, m: 0.001, o, lev, LOG, rules=rules or LiveRules(margin_cap_usdt=1000),
                   sleep=lambda s: None, alert=alerts.append)
    b.alerts = alerts
    return b


class LiveBrokerTests(unittest.TestCase):
    def test_two_legs_fill_and_isolated_is_set_first(self):
        f, s = FakeClient("binance", "futures"), FakeClient("binance", "spot", base_coins={"ETH": 5})
        b = broker({("binance", "futures"): f, ("binance", "spot"): s})
        legs = [leg(market="futures", side="sell"), leg(market="spot", side="sell")]
        ok, why = b.can_do_all(legs)
        self.assertTrue(ok, why)
        r = b.exec_legs(legs)
        self.assertEqual([x.status for x in r], ["filled", "filled"])
        self.assertEqual(f.calls[0], ("isolated", "ETHUSDT"))
        self.assertEqual(f.calls[1], ("leverage", "ETHUSDT", 2))

    def test_hub_pair_is_skipped(self):
        f = FakeClient("binance", "futures")
        rules = LiveRules(skip=lambda ex, base: "hub trades it" if base == "ETH" else None, margin_cap_usdt=1000)
        ok, why = broker({("binance", "futures"): f}, rules).can_do_all([leg()])
        self.assertFalse(ok)
        self.assertIn("hub trades it", why)
        self.assertEqual(f.orders, [])

    def test_existing_position_is_refused(self):
        f = FakeClient("binance", "futures", positions={"ETHUSDT": 2.0})
        ok, why = broker({("binance", "futures"): f}).can_do_all([leg()])
        self.assertFalse(ok)
        self.assertIn("already has a position", why)

    def test_unreadable_positions_are_refused(self):
        f = FakeClient("binance", "futures")

        def boom():
            raise ApiError("down")
        f.open_positions = boom
        ok, why = broker({("binance", "futures"): f}).can_do_all([leg()])
        self.assertFalse(ok)
        self.assertIn("cannot read the open positions", why)

    def test_margin_cap(self):
        f = FakeClient("binance", "futures")
        # margin = 1 * 100 / 2 = 50
        ok, _ = broker({("binance", "futures"): f}, LiveRules(margin_cap_usdt=60)).can_do_all([leg()])
        self.assertTrue(ok)
        rules = LiveRules(margin_used=lambda ex: 20.0, margin_cap_usdt=60)
        ok, why = broker({("binance", "futures"): f}, rules).can_do_all([leg()])
        self.assertFalse(ok)
        self.assertIn("margin cap", why)
        ok, why = broker({("binance", "futures"): f}, LiveRules(margin_cap_usdt=0)).can_do_all([leg()])
        self.assertFalse(ok)
        self.assertIn("no margin cap", why)

    def test_not_enough_balance(self):
        f = FakeClient("binance", "futures", usdt=10)
        ok, why = broker({("binance", "futures"): f}).can_do_all([leg()])
        self.assertFalse(ok)
        self.assertIn("not enough USDT", why)

    def test_closing_leg_skips_entry_checks(self):
        f = FakeClient("binance", "futures", positions={"ETHUSDT": -1.0}, usdt=0)
        rules = LiveRules(skip=lambda ex, base: "hub", margin_cap_usdt=0)
        b = broker({("binance", "futures"): f}, rules)
        ok, why = b.can_do_all([leg(side="buy", closing=True)])
        self.assertTrue(ok, why)
        r = b.exec_legs([leg(side="buy", closing=True)])
        self.assertTrue(f.orders[0]["reduce_only"])
        self.assertEqual(r[0].status, "filled")

    def test_harder_first_does_not_place_second_if_first_fails(self):
        f, s = FakeClient("binance", "futures", fill_ratio=0.0), FakeClient("binance", "spot", base_coins={"ETH": 5})
        b = broker({("binance", "futures"): f, ("binance", "spot"): s}, orders={"sequence": "harder_first"})
        legs = [leg(market="futures", side="sell", top=0.5), leg(market="spot", side="sell", top=50)]
        r = b.exec_legs(legs)
        self.assertEqual(r[0].status, "none")
        self.assertEqual(s.orders, [])
        self.assertEqual(r[1].status, "none")

    def test_limit_order_uses_price_buffer_rounded_against_us(self):
        f = FakeClient("binance", "futures")
        b = broker({("binance", "futures"): f}, orders={"type": "limit_ioc", "price_buffer_pct": 0.1})
        b.exec_legs([leg(side="buy", px=100.0)])
        self.assertEqual(f.orders[0]["type"], "limit_ioc")
        self.assertAlmostEqual(float(f.orders[0]["price"]), 100.10, places=2)
        b.exec_legs([leg(side="sell", px=100.0)])
        self.assertAlmostEqual(float(f.orders[1]["price"]), 99.90, places=2)

    def test_rejected_order_is_a_clean_miss_and_unclear_one_alerts(self):
        f = FakeClient("binance", "futures")
        f.fail_place = ApiError("HTTP 400 insufficient balance")
        b = broker({("binance", "futures"): f})
        r = b.exec_legs([leg()])
        self.assertIn(r[0].status, ("none", "error"))
        f2 = FakeClient("binance", "futures")
        f2.fail_place = ApiError("timeout")
        f2.get_order = lambda *a, **k: (_ for _ in ()).throw(ApiError("timeout"))
        b2 = broker({("binance", "futures"): f2})
        r2 = b2.exec_legs([leg()])
        self.assertEqual(r2[0].status, "error")
        self.assertTrue(b2.alerts and "UNCERTAIN" in b2.alerts[0])


class GateTests(unittest.TestCase):
    def settings(self):
        s = copy.deepcopy(st.DEFAULTS)
        s["modules"]["spatial"].update(enabled=True, mode="live", markets=["futures"])
        return s

    def test_blockers_list_everything_missing(self):
        s = self.settings()
        b = live_blockers(s, "spatial", s["modules"]["spatial"], lambda slot: False)
        text = " | ".join(b)
        for part in ("Max size per trade", "daily loss", "Margin cap", "Binance futures", "OKX"):
            self.assertIn(part, text)

    def test_no_blockers_when_all_filled(self):
        s = self.settings()
        s["risk"].update(max_size_per_trade_usdt=50, daily_loss_limit_usdt=20, margin_cap_usdt=100)
        self.assertEqual(live_blockers(s, "spatial", s["modules"]["spatial"], lambda slot: True), [])

    def test_zero_means_off_is_accepted_but_blank_is_not(self):
        s = self.settings()
        s["risk"].update(max_size_per_trade_usdt=0, daily_loss_limit_usdt=0, margin_cap_usdt=100)
        self.assertEqual(live_blockers(s, "spatial", s["modules"]["spatial"], lambda slot: True), [])
        s["risk"]["daily_loss_limit_usdt"] = None
        self.assertEqual(len(live_blockers(s, "spatial", s["modules"]["spatial"], lambda slot: True)), 1)

    def test_margin_cap_zero_blocks_futures_but_not_triangular(self):
        s = self.settings()
        s["risk"].update(max_size_per_trade_usdt=50, daily_loss_limit_usdt=20, margin_cap_usdt=0)
        self.assertTrue(live_blockers(s, "spatial", s["modules"]["spatial"], lambda slot: True))
        self.assertEqual(live_blockers(s, "triangular", s["modules"]["triangular"], lambda slot: True), [])

    def test_needed_clients(self):
        s = self.settings()
        self.assertEqual(sorted(needed_clients("funding", s["modules"]["funding"], ["binance"])),
                         [("binance", "futures"), ("binance", "spot")])
        self.assertEqual(needed_clients("triangular", s["modules"]["triangular"], ["binance", "okx"]),
                         [("binance", "spot"), ("okx", "spot")])


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.k = ArbKeyStore(os.path.join(self.d.name, "keys.json"))

    def tearDown(self):
        self.d.cleanup()

    def test_save_status_shows_only_tail_and_file_is_not_plain(self):
        self.k.update("binance_futures", {"key": "ABCDEFGH1234", "secret": "SECRETVALUE99"})
        st_ = self.k.status()["binance_futures"]
        self.assertTrue(st_["set"])
        self.assertEqual(st_["tail"], "1234")
        self.assertNotIn("SECRETVALUE99", json.dumps(st_))
        raw = open(self.k.path).read()
        self.assertNotIn("SECRETVALUE99", raw)
        self.assertNotIn("ABCDEFGH1234", raw)
        self.assertEqual(self.k.get("binance_futures")["secret"], "SECRETVALUE99")

    def test_spot_falls_back_to_futures_key(self):
        self.k.update("binance_futures", {"key": "K", "secret": "S"})
        self.assertEqual(self.k.get("binance_spot"), {"key": "K", "secret": "S"})
        self.assertTrue(self.k.status()["binance_spot"]["uses_futures_key"])

    def test_clear_and_bad_input(self):
        self.k.update("okx", {"key": "a", "secret": "b", "passphrase": "c"})
        self.k.clear("okx")
        self.assertIsNone(self.k.get("okx"))
        with self.assertRaises(KeyError_):
            self.k.update("okx", {"key": "a", "secret": "b"})
        with self.assertRaises(KeyError_):
            self.k.update("okx", {"key": "a b", "secret": "b", "passphrase": "c"})
        with self.assertRaises(KeyError_):
            self.k.update("nope", {})

    def test_fingerprint_changes_with_keys(self):
        a = self.k.fingerprint()
        self.k.update("okx", {"key": "a", "secret": "b", "passphrase": "c"})
        self.assertNotEqual(a, self.k.fingerprint())


class TraderLiveTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.keys = ArbKeyStore(os.path.join(self.d.name, "keys.json"))
        self.tr = ArbTrader(folder=self.d.name, keys=self.keys)

    def tearDown(self):
        self.d.cleanup()

    def live_settings(self):
        s = copy.deepcopy(st.DEFAULTS)
        s["modules"]["spatial"].update(enabled=True, mode="live", markets=["futures"])
        s["modules"]["funding"]["enabled"] = False
        return s

    def test_wanted_has_live_and_shadow(self):
        s = self.live_settings()
        self.assertEqual([w[3] for w in ArbTrader.wanted(s)], ["live", "live-mock"])
        s["live_mock"]["enabled"] = False
        self.assertEqual([w[3] for w in ArbTrader.wanted(s)], ["live"])

    def test_triangular_needs_confirm_and_typed_yes(self):
        s = self.live_settings()
        s["modules"]["spatial"]["enabled"] = False
        s["modules"]["triangular"].update(enabled=True, mode="live", confirm_enable=False)
        self.assertEqual(ArbTrader.wanted(s), [])
        s["modules"]["triangular"]["confirm_enable"] = True
        self.assertTrue(ArbTrader.wanted(s))
        s["risk"].update(max_size_per_trade_usdt=10, daily_loss_limit_usdt=10, margin_cap_usdt=10)
        for slot, v in (("binance_futures", {"key": "k", "secret": "s"}), ("okx", {"key": "k", "secret": "s", "passphrase": "p"})):
            self.keys.update(slot, v)
        self.tr.loop = object()
        with self.assertRaises(Exception) as cm:
            self.tr._live_group(s, "triangular", s["modules"]["triangular"], ["binance", "okx"])
        self.assertIn("LOCKED", str(cm.exception))

    def test_live_without_keys_never_starts_and_says_why(self):
        s = self.live_settings()
        self.tr.loop = object()

        class Scn:
            rules, books, funding, volumes, stamp = {}, {}, {}, {}, 1
            universe = {"spatial": [], "funding": [], "triangular": []}
            loop = None

            def book_age(self, k):
                return 0
        self.tr.step(s, Scn())
        self.assertFalse([k for k in self.tr.engines if k[2] == "live"])
        self.assertFalse([k for k in self.tr.engines if k[2] == "live-mock"], "no shadow without the live module")
        snap = self.tr.snapshot(s)
        self.assertTrue(any("Live NOT started" in p for p in snap["problems"]))

    def test_stop_button_makes_and_removes_kill_file(self):
        s = copy.deepcopy(st.DEFAULTS)
        self.assertTrue(self.tr.set_stop(True, s))
        self.assertTrue(self.tr.snapshot(s)["kill_switch"])
        self.assertFalse(self.tr.set_stop(False, s))
        self.assertFalse(self.tr.snapshot(s)["kill_switch"])

    def test_reset_keeps_live_rows_and_live_state(self):
        import csv
        from app.arb.store import JOURNAL_FIELDS
        path = os.path.join(self.d.name, "trades.csv")
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerow({"mode": "mock", "module": "spatial", "net_pnl_usdt": "1"})
            w.writerow({"mode": "live", "module": "spatial", "net_pnl_usdt": "2"})
        for name in ("state_spatial_futures_mock.json", "state_spatial_futures_live.json"):
            open(os.path.join(self.d.name, name), "w").write("{}")
        self.tr.reset()
        self.assertFalse(os.path.exists(os.path.join(self.d.name, "state_spatial_futures_mock.json")))
        self.assertTrue(os.path.exists(os.path.join(self.d.name, "state_spatial_futures_live.json")))
        rows = list(csv.DictReader(open(path)))
        self.assertEqual([r["mode"] for r in rows], ["live"])


class ServiceLiveTests(unittest.TestCase):
    def setUp(self):
        from app.arb import service
        self.service = service
        self.d = tempfile.TemporaryDirectory()
        self._ks = service.arb_keys._path
        from pathlib import Path
        service.arb_keys._path = Path(self.d.name) / "keys.json"

    def tearDown(self):
        self.service.arb_keys._path = self._ks
        self.d.cleanup()

    def new(self):
        s = copy.deepcopy(st.DEFAULTS)
        s["modules"]["spatial"].update(enabled=True, mode="live", markets=["futures"])
        s["risk"].update(max_size_per_trade_usdt=50, daily_loss_limit_usdt=20, margin_cap_usdt=100)
        return s

    def test_going_live_needs_typed_LIVE(self):
        s = self.new()
        errs = self.service.check_save(s, None)
        self.assertTrue(any("Type LIVE" in e for e in errs), errs)
        errs = self.service.check_save(s, "live")
        self.assertTrue(any("Type LIVE" in e for e in errs))

    def test_going_live_needs_keys(self):
        errs = self.service.check_save(self.new(), "LIVE")
        self.assertTrue(any("API key" in e for e in errs), errs)
        self.service.arb_keys.update("binance_futures", {"key": "k", "secret": "s"})
        self.service.arb_keys.update("okx", {"key": "k", "secret": "s", "passphrase": "p"})
        self.assertEqual(self.service.check_save(self.new(), "LIVE"), [])

    def test_triangular_lock_needs_exact_YES_and_confirm(self):
        svc = self.service
        with self.assertRaises(svc.ArbSaveError):
            svc.triangular_lock("yes")
        with self.assertRaises(svc.ArbSaveError):
            svc.triangular_lock("YES")        # not switched on / confirmed in saved settings
        self.assertFalse(svc.trader.tri_unlocked)


class StubTransport:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def request(self, method, url, **kw):
        self.seen.append((method, url, kw))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class ClientRequestTests(unittest.TestCase):
    def test_binance_futures_order_is_signed_and_isolated(self):
        from urllib.parse import parse_qs, urlparse
        from app.arb.live_clients import BinanceClient
        tp = StubTransport([{"orderId": 7, "executedQty": "1", "avgPrice": "100", "status": "FILLED"}])
        c = BinanceClient("futures", False, "KEY", "SECRET", tp)
        o = c.place(ins(), "sell", 1, "limit_ioc", price=99.9, tif="IOC", reduce_only=True, cl_id="abc")
        method, url, kw = tp.seen[0]
        q = parse_qs(urlparse(url).query)
        self.assertEqual((method, urlparse(url).hostname, urlparse(url).path), ("POST", "fapi.binance.com", "/fapi/v1/order"))
        self.assertEqual((q["side"][0], q["type"][0], q["timeInForce"][0], q["reduceOnly"][0]), ("SELL", "LIMIT", "IOC", "true"))
        self.assertIn("signature", q)
        self.assertEqual(kw["headers"]["X-MBX-APIKEY"], "KEY")
        self.assertTrue(kw["urgent"])
        self.assertEqual((o["filled"], o["avg"], o["open"]), (1.0, 100.0, False))

    def test_binance_isolated_already_set_is_fine(self):
        from app.arb.live_clients import BinanceClient
        tp = StubTransport([ApiError("HTTP 400 from x: {\"code\":-4046,\"msg\":\"No need to change margin type.\"}")])
        BinanceClient("futures", False, "K", "S", tp).set_isolated(ins())
        tp2 = StubTransport([ApiError("HTTP 500 boom")])
        with self.assertRaises(ApiError):
            BinanceClient("futures", False, "K", "S", tp2).set_isolated(ins())
        self.assertIn("ISOLATED", tp.seen[0][1])

    def test_okx_order_isolated_for_futures_cash_for_spot_and_contracts(self):
        from app.arb.live_clients import OKXClient
        import json as _j
        swap = Instr("okx", "futures", "ETH-USDT-SWAP", "ETH", "USDT", "0.01", "0.01", "0.01", 0, "0.1")
        spot = Instr("okx", "spot", "ETH-USDT", "ETH", "USDT", "0.0001", "0.0001", "0.01", 1, 1)
        row = [{"ordId": "9", "accFillSz": "10", "avgPx": "100", "state": "filled"}]
        tp = StubTransport([{"code": "0", "data": [{"ordId": "9", "sCode": "0"}]}, {"code": "0", "data": row}])
        c = OKXClient("futures", False, "K", "S", "P", tp)
        o = c.place(swap, "sell", 1.0, "market")        # 1 ETH = 10 contracts of 0.1
        body = _j.loads(tp.seen[0][2]["data"])
        self.assertEqual((body["tdMode"], body["sz"], body["ordType"]), ("isolated", "10", "market"))
        self.assertAlmostEqual(o["filled"], 1.0)        # 10 contracts back to 1 ETH
        tp2 = StubTransport([{"code": "0", "data": [{"ordId": "9", "sCode": "0"}]}, {"code": "0", "data": row}])
        OKXClient("spot", False, "K", "S", "P", tp2).place(spot, "buy", 1.0, "market")
        b2 = _j.loads(tp2.seen[0][2]["data"])
        self.assertEqual((b2["tdMode"], b2.get("tgtCcy")), ("cash", "base_ccy"))

    def test_okx_demo_header_and_rejection(self):
        from app.arb.live_clients import OKXClient
        tp = StubTransport([{"code": "0", "data": [{"ordId": "", "sCode": "51008", "sMsg": "insufficient"}]}])
        c = OKXClient("spot", True, "K", "S", "P", tp)
        with self.assertRaises(ApiError) as cm:
            c.place(Instr("okx", "spot", "ETH-USDT", "ETH", "USDT", "0.0001", "0.0001", "0.01", 1, 1), "buy", 1.0, "market")
        self.assertTrue(str(cm.exception).startswith("OKX order rejected"))
        self.assertEqual(tp.seen[0][2]["headers"]["x-simulated-trading"], "1")


class FakeHub:
    def __init__(self, hit=None):
        self.hit = hit or {}

    def matches(self, ex, key):
        return self.hit

    def accounts_using(self, ex, key):
        return list(set(self.hit.values()))


class EndToEndLiveTests(unittest.TestCase):
    """The real trader + real engines + real live broker, with fake exchange clients."""

    def setUp(self):
        from tests.test_arb_mock import FakeScanner
        self.d = tempfile.TemporaryDirectory()
        self.keys = ArbKeyStore(os.path.join(self.d.name, "keys.json"))
        self.keys.update("binance_futures", {"key": "BK", "secret": "BS"})
        self.keys.update("okx", {"key": "OK", "secret": "OS", "passphrase": "OP"})
        self.clients = {}
        outer = self

        class T(ArbTrader):
            def _make_client(self, s, ex, market, tp):
                c = outer.clients.setdefault((ex, market), FakeClient(ex, market))
                c.px = {"BTCUSDT": 100000.0, "BTC-USDT-SWAP": 100400.0}
                return c
        self.tr = T(folder=self.d.name, keys=self.keys, hub=FakeHub())
        self.tr.loop = object()
        sc = FakeScanner()
        sc.rules = {("binance", "futures"): {"BTCUSDT": Instr("binance", "futures", "BTCUSDT", "BTC", "USDT", "0.001", "0.001", "0.1", 5, 1)},
                    ("okx", "futures"): {"BTC-USDT-SWAP": Instr("okx", "futures", "BTC-USDT-SWAP", "BTC", "USDT", "0.01", "0.01", "0.1", 0, "0.01")}}
        sc.universe["spatial"] = {"futures": ["BTC"]}
        sc.books = {("binance", "futures"): {"BTCUSDT": {"bid": 99990.0, "ask": 100000.0, "bq": 10, "aq": 10}},
                    ("okx", "futures"): {"BTC-USDT-SWAP": {"bid": 100400.0, "ask": 100410.0, "bq": 10, "aq": 10}}}
        self.sc = sc
        s = copy.deepcopy(st.DEFAULTS)
        s["modules"]["spatial"].update(enabled=True, mode="live", markets=["futures"], trade_size_usdt=1000,
                                       slippage_pct=0, leverage=2)
        s["modules"]["funding"]["enabled"] = False
        s["orders"]["type"] = "market"
        s["risk"].update(max_size_per_trade_usdt=5000, daily_loss_limit_usdt=100, margin_cap_usdt=2000)
        self.s = s

    def tearDown(self):
        self.d.cleanup()

    def test_live_trade_opens_with_real_orders_on_both_exchanges_and_shadow_runs(self):
        self.tr.step(self.s, self.sc)
        self.assertEqual(self.tr.problems, {})
        b, o = self.clients[("binance", "futures")], self.clients[("okx", "futures")]
        self.assertEqual([x["side"] for x in b.orders], ["buy"])
        self.assertEqual([x["side"] for x in o.orders], ["sell"])
        self.assertIn(("isolated", "BTCUSDT"), b.calls)
        self.assertIn(("leverage", "BTCUSDT", 2), b.calls)
        self.assertIn(("leverage", "BTC-USDT-SWAP", 2), o.calls)
        snap = self.tr.snapshot(self.s)
        modes = sorted(p["mode"] for p in snap["positions"])
        self.assertEqual(modes, ["live", "live-mock"])
        self.assertEqual(snap["live_open"], 1)
        self.assertGreater(self.tr._margin_used("binance"), 0)

    def test_live_open_sends_a_telegram_message(self):
        sent = []
        self.tr._alert = sent.append
        self.tr.step(self.s, self.sc)
        self.assertTrue(any("ARBITRAGE LIVE opened spatial/futures BTC" in x for x in sent), sent)
        self.assertFalse(any("SHADOW" in x or "MOCK" in x for x in sent), sent)

    def test_no_second_entry_on_a_coin_with_an_open_position(self):
        self.tr.step(self.s, self.sc)
        n = len(self.clients[("binance", "futures")].orders)
        self.tr.step(self.s, self.sc)
        self.assertEqual(len(self.clients[("binance", "futures")].orders), n)

    def test_hub_coin_is_skipped_and_nothing_is_sent(self):
        self.tr.hub = FakeHub({"BTC": "my-hub-account"})
        self.tr.step(self.s, self.sc)
        self.assertEqual(self.clients[("binance", "futures")].orders, [])
        self.assertEqual(self.clients[("okx", "futures")].orders, [])

    def test_margin_cap_stops_the_entry(self):
        self.s["risk"]["margin_cap_usdt"] = 100          # trade needs ~500 margin per side
        self.tr.step(self.s, self.sc)
        self.assertEqual(self.clients[("binance", "futures")].orders, [])

    def test_stop_file_blocks_new_live_entries(self):
        self.tr.set_stop(True, self.s)
        self.tr.step(self.s, self.sc)
        self.assertEqual(self.clients[("binance", "futures")].orders, [])

    def test_hedge_mode_account_is_refused(self):
        outer = self
        c = FakeClient("binance", "futures")
        c.one_way_mode = lambda: False
        self.clients[("binance", "futures")] = c
        self.tr.step(self.s, self.sc)
        self.assertTrue(any("HEDGE" in p for p in self.tr.problems.values()), self.tr.problems)
        self.assertEqual(c.orders, [])

    def test_bad_key_means_live_does_not_start(self):
        c = FakeClient("okx", "futures")

        def boom(assets=None):
            raise ApiError("HTTP 401 invalid key")
        c.get_balances = boom
        self.clients[("okx", "futures")] = c
        self.tr.step(self.s, self.sc)
        self.assertTrue(any("does not work" in p for p in self.tr.problems.values()), self.tr.problems)
        self.assertEqual(self.clients[("binance", "futures")].orders, [])

    def test_stale_prices_never_trade(self):
        self.sc.age = 999
        self.tr.step(self.s, self.sc)
        self.assertEqual(self.clients[("binance", "futures")].orders, [])

    def test_switching_module_off_keeps_closing_but_never_opens(self):
        self.tr.step(self.s, self.sc)
        self.s["modules"]["spatial"]["enabled"] = False
        n = len(self.clients[("binance", "futures")].orders)
        self.sc.books[("binance", "futures")]["BTCUSDT"].update(bid=100600.0, ask=100610.0)
        self.sc.books[("okx", "futures")]["BTC-USDT-SWAP"].update(bid=100400.0, ask=100410.0)
        self.tr.step(self.s, self.sc)
        eng = self.tr.engines[("spatial", "futures", "live")]
        self.assertTrue(eng.get("active") or eng["eng"].exit_only)
        orders = self.clients[("binance", "futures")].orders
        self.assertTrue(all(o["reduce_only"] for o in orders[n:]))

    def test_restart_resumes_open_live_trade_for_exits_only(self):
        self.tr.step(self.s, self.sc)
        self.assertEqual(len([1 for p in self.tr.snapshot(self.s)["positions"] if p["mode"] == "live"]), 1)
        for rec in self.tr.engines.values():
            rec["eng"].save()
        tr2 = type(self.tr)(folder=self.d.name, keys=self.keys, hub=FakeHub())
        tr2.loop = object()
        self.s["modules"]["spatial"]["enabled"] = False
        tr2.step(self.s, self.sc)
        live = [p for p in tr2.snapshot(self.s)["positions"] if p["mode"] == "live"]
        self.assertEqual(len(live), 1, "the open live trade must be picked up again after a restart")


if __name__ == "__main__":
    unittest.main()


class TradeAlertTests(unittest.TestCase):
    def run_it(self, want, mode):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        tr = ArbTrader(folder=d.name, keys=ArbKeyStore(os.path.join(d.name, "k.json")))
        s = copy.deepcopy(st.DEFAULTS)
        s["alerts"]["trades"] = want
        tr._ensure(s)
        sent = []
        tr._alert = sent.append
        tr._trade_event("open", mode, "opened spatial/futures BTC, size 10.00 USDT (x1)")
        tr.journal.add({"id": "x1", "module": "spatial", "market": "futures", "mode": mode, "base": "BTC",
                        "net_pnl_usdt": "1.5", "fees_usdt": "0.1", "reason": "profit"})
        return sent

    def test_default_is_live_only(self):
        self.assertEqual(st.DEFAULTS["alerts"]["trades"], "live")
        self.assertEqual(len(self.run_it("live", "live")), 2)
        self.assertEqual(self.run_it("live", "mock"), [])
        self.assertEqual(self.run_it("live", "live-mock"), [])

    def test_off_and_all(self):
        self.assertEqual(self.run_it("off", "live"), [])
        sent = self.run_it("all", "mock")
        self.assertEqual(len(sent), 2)
        self.assertIn("MOCK", sent[0])
        self.assertIn("+1.5000", sent[1])

    def test_bad_value_is_refused(self):
        s = copy.deepcopy(st.DEFAULTS)
        s["alerts"]["trades"] = "sometimes"
        self.assertTrue(any("alerts.trades" in e for e in st.validate(s)))
