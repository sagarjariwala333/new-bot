"""Arbitrage tab, Step 1: settings checks, gap maths, read-only scan through the API guard."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

import tests._aiohttp_stub  # noqa: F401
from app import api_guard
from app.arb import scanner as sc
from app.arb import settings as st


def _s(**over):
    s = copy.deepcopy(st.DEFAULTS)
    for k, v in over.items():
        s = st._merge(s, {k: v})
    return s


class ValidateTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(st.validate(st.DEFAULTS), [])

    def test_live_futures_needs_margin_cap(self):
        s = _s(modules={"spatial": {"mode": "live", "enabled": True}})
        errs = st.validate(s)
        self.assertTrue(any("margin_cap" in e for e in errs), errs)
        s["risk"]["margin_cap_usdt"] = 100
        self.assertFalse(any("margin_cap" in e for e in st.validate(s)))

    def test_all_problems_at_once_in_plain_words(self):
        s = _s(loop_seconds=1, pairs={"top_n": 0}, orders={"type": "banana"})
        s["modules"]["spatial"]["trade_size_usdt"] = "abc"
        errs = st.validate(s)
        text = " | ".join(errs)
        for need in ("loop_seconds", "pairs.top_n", "orders.type", "trade_size_usdt"):
            self.assertIn(need, text)
        self.assertGreaterEqual(len(errs), 4)

    def test_blank_number_is_named(self):
        s = _s()
        s["modules"]["funding"]["min_funding_pct"] = None
        self.assertIn("modules.funding.min_funding_pct is missing - please fill it in", st.validate(s))

    def test_spatial_needs_both_exchanges(self):
        s = _s(exchanges={"okx": {"enabled": False}})
        self.assertTrue(any("BOTH" in e for e in st.validate(s)))

    def test_risk_null_only_blocks_live(self):
        self.assertEqual(st.validate(_s()), [])      # null is fine while everything is mock

    def test_kill_switch_must_be_plain_name(self):
        self.assertTrue(st.validate(_s(risk={"kill_switch_file": "../x"})))

    def test_bool_must_be_bool(self):
        s = _s()
        s["scanner_on"] = "yes"
        self.assertTrue(any("scanner_on" in e for e in st.validate(s)))
        s = _s()
        s["modules"]["triangular"]["enabled"] = 1
        self.assertTrue(any("triangular.enabled" in e for e in st.validate(s)))

    def test_triangular_off_by_default_and_unconfirmed(self):
        t = st.DEFAULTS["modules"]["triangular"]
        self.assertFalse(t["enabled"])
        self.assertFalse(t["confirm_enable"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = Path(self.d.name) / "arb" / "settings.json"
        self.store = st.ArbSettingsStore(self.path)

    def tearDown(self):
        self.d.cleanup()

    def test_missing_file_gives_defaults(self):
        self.assertEqual(self.store.load(), st.DEFAULTS)

    def test_bad_save_writes_nothing(self):
        errs = self.store.save(_s(loop_seconds=0))
        self.assertTrue(errs)
        self.assertFalse(self.path.exists())

    def test_save_and_load_roundtrip(self):
        s = _s(pairs={"top_n": 12})
        self.assertEqual(self.store.save(s), [])
        self.assertEqual(self.store.load()["pairs"]["top_n"], 12)

    def test_new_setting_gets_default_when_file_is_old(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"pairs": {"top_n": 7}}))
        got = self.store.load()
        self.assertEqual(got["pairs"]["top_n"], 7)
        self.assertEqual(got["pairs"]["refresh_minutes"], 30)

    def test_damaged_file_is_reported_not_replaced(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            self.store.load()
        self.assertEqual(self.path.read_text(), "{not json")


class MathTests(unittest.TestCase):
    def books(self):
        return {("binance", "futures"): {"BTCUSDT": {"bid": 99.9, "ask": 100.0, "bq": 5, "aq": 5}},
                ("okx", "futures"): {"BTC-USDT-SWAP": {"bid": 100.6, "ask": 100.7, "bq": 4, "aq": 4}}}

    def test_spatial_both_directions_and_net(self):
        out = sc.spatial_edges(["BTC"], self.books(), "futures", "binance", "okx", 0.0005, 0.0005, 0.01)
        self.assertEqual(len(out), 2)
        best = out[0]
        self.assertEqual((best["buy"], best["sell"]), ("binance", "okx"))
        self.assertAlmostEqual(best["gap"], 0.6, places=6)
        # futures: 2 rounds * (0.05+0.05)% = 0.2 ; 4 fills * 0.01 = 0.04
        self.assertAlmostEqual(best["net"], 0.6 - 0.2 - 0.04, places=6)
        self.assertLess(out[1]["net"], 0)

    def test_spatial_spot_uses_one_round_two_fills(self):
        b = {("binance", "spot"): {"BTCUSDT": {"bid": 99.9, "ask": 100.0, "bq": 1, "aq": 1}},
             ("okx", "spot"): {"BTC-USDT": {"bid": 100.6, "ask": 100.7, "bq": 1, "aq": 1}}}
        best = sc.spatial_edges(["BTC"], b, "spot", "binance", "okx", 0.001, 0.001, 0.01)[0]
        self.assertAlmostEqual(best["net"], 0.6 - 0.2 - 0.02, places=6)

    def test_spatial_skips_coin_missing_on_one_side(self):
        b = self.books()
        del b[("okx", "futures")]["BTC-USDT-SWAP"]
        self.assertEqual(sc.spatial_edges(["BTC"], b, "futures", "binance", "okx", 0, 0, 0), [])

    def test_funding_edges(self):
        books = {("binance", "spot"): {"BTCUSDT": {"bid": 99.9, "ask": 100.0, "bq": 1, "aq": 1}},
                 ("binance", "futures"): {"BTCUSDT": {"bid": 100.1, "ask": 100.2, "bq": 1, "aq": 1}}}
        fees = {("binance", "spot"): 0.001, ("binance", "futures"): 0.0005,
                ("okx", "spot"): 0.001, ("okx", "futures"): 0.0005}
        fund = {"binance": {"BTC": {"rate": 0.0005, "next_time_ms": 1}}}
        out = sc.funding_edges(["binance", "okx"], {"binance": ["BTC"], "okx": ["BTC"]}, fund, books, fees, 3)
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertAlmostEqual(e["rate"], 0.05)
        self.assertAlmostEqual(e["fees"], 0.3)             # 2*(0.1+0.05)
        self.assertAlmostEqual(e["basis"], 0.1, places=6)
        self.assertAlmostEqual(e["periods"], 6.0)
        self.assertAlmostEqual(e["net"], 0.05 * 3 - 0.3)

    def test_funding_negative_rate_has_no_breakeven(self):
        books = {("binance", "spot"): {"BTCUSDT": {"bid": 1, "ask": 1, "bq": 1, "aq": 1}},
                 ("binance", "futures"): {"BTCUSDT": {"bid": 1, "ask": 1, "bq": 1, "aq": 1}}}
        fees = {k: 0.001 for k in (("binance", "spot"), ("binance", "futures"))}
        out = sc.funding_edges(["binance"], {"binance": ["BTC"]},
                               {"binance": {"BTC": {"rate": -0.0001, "next_time_ms": 1}}}, books, fees, 3)
        self.assertIsNone(out[0]["periods"])

    def test_triangular(self):
        instr = {"XUSDT": {"base": "X", "quote": "USDT"}, "BTCUSDT": {"base": "BTC", "quote": "USDT"},
                 "XBTC": {"base": "X", "quote": "BTC"}}
        cycles = sc.build_cycles("binance", instr, ["X", "BTC"], ["BTC", "ETH"])
        self.assertEqual(cycles, [("binance", "X", "BTC", "XUSDT", "XBTC", "BTCUSDT")])
        self.assertEqual(len(cycles), 1)           # ETH has no pair, BTC cannot bridge itself
        books = {("binance", "spot"): {"XUSDT": {"bid": 1.0, "ask": 1.0, "bq": 1, "aq": 1},
                                       "XBTC": {"bid": 0.00002, "ask": 0.00002, "bq": 1, "aq": 1},
                                       "BTCUSDT": {"bid": 51000.0, "ask": 51000.0, "bq": 1, "aq": 1}}}
        out = sc.triangular_edges(cycles, books, {("binance", "spot"): 0.001}, 0.0)
        fwd = [e for e in out if e["dir"] == "fwd"][0]
        # 51000 * 0.00002 / 1.0 = 1.02 ; x (0.999^3) = 1.0189... -> +1.89%
        self.assertAlmostEqual(fwd["net"], (1.02 * 0.999 ** 3 - 1) * 100, places=6)
        self.assertEqual(out[0]["dir"], "fwd")

    def test_pick_top(self):
        ranked = [("A", 9e6), ("B", 8e6), ("C", 1e6), ("D", 7e6)]
        pairs = {"include": ["c"], "exclude": ["B"], "min_volume_usdt_24h": 2e6}
        self.assertEqual(sc.pick_top(ranked, pairs, 2), ["A", "D", "C"])
        self.assertEqual(sc.pick_top(ranked, {"include": [], "exclude": [], "min_volume_usdt_24h": 0}, 3), ["A", "B", "C"])


# ---------------------------------------------------------------- fake exchange
class FakeResp:
    def __init__(self, status, data, headers=None):
        self.status, self._data, self.headers = status, data, headers or {}

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    closed = False

    def __init__(self, router):
        self.router, self.calls = router, []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return FakeResp(*self.router(url, params or {}))

    def post(self, *a, **k):                      # the scan must never call this
        raise AssertionError("the scanner must never POST")

    async def close(self):
        self.closed = True


def okx(rows):
    return 200, {"code": "0", "msg": "", "data": rows}


def router(url, p):
    if "fapi.binance.com" in url:
        if url.endswith("/exchangeInfo"):
            return 200, {"symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "BTC", "quoteAsset": "USDT"},
                {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "ETH", "quoteAsset": "USDT"},
                {"symbol": "BTCUSDT_261225", "status": "TRADING", "contractType": "CURRENT_QUARTER", "baseAsset": "BTC", "quoteAsset": "USDT"}]}
        if url.endswith("/ticker/24hr"):
            return 200, [{"symbol": "BTCUSDT", "quoteVolume": "9000000"}, {"symbol": "ETHUSDT", "quoteVolume": "5000000"}]
        if url.endswith("/ticker/bookTicker"):
            return 200, [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100", "bidQty": "5", "askQty": "5"},
                         {"symbol": "ETHUSDT", "bidPrice": "10", "askPrice": "10.01", "bidQty": "5", "askQty": "5"}]
        if url.endswith("/premiumIndex"):
            return 200, [{"symbol": "BTCUSDT", "lastFundingRate": "0.0006", "nextFundingTime": 123},
                         {"symbol": "ETHUSDT", "lastFundingRate": "0.0001", "nextFundingTime": 123}]
    if "api.binance.com" in url:
        if url.endswith("/exchangeInfo"):
            return 200, {"symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING", "isSpotTradingAllowed": True, "baseAsset": "BTC", "quoteAsset": "USDT"},
                {"symbol": "ETHUSDT", "status": "TRADING", "isSpotTradingAllowed": True, "baseAsset": "ETH", "quoteAsset": "USDT"},
                {"symbol": "ETHBTC", "status": "TRADING", "isSpotTradingAllowed": True, "baseAsset": "ETH", "quoteAsset": "BTC"}]}
        if url.endswith("/ticker/24hr"):
            return 200, [{"symbol": "BTCUSDT", "quoteVolume": "9000000"}, {"symbol": "ETHUSDT", "quoteVolume": "5000000"}]
        if url.endswith("/ticker/bookTicker"):
            return 200, [{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100", "bidQty": "5", "askQty": "5"},
                         {"symbol": "ETHUSDT", "bidPrice": "10", "askPrice": "10.01", "bidQty": "5", "askQty": "5"},
                         {"symbol": "ETHBTC", "bidPrice": "0.1", "askPrice": "0.1", "bidQty": "5", "askQty": "5"}]
    if "okx.com" in url:
        it = p.get("instType")
        if url.endswith("/public/instruments"):
            if it == "SWAP":
                return okx([{"instId": "BTC-USDT-SWAP", "settleCcy": "USDT", "ctType": "linear", "instFamily": "BTC-USDT",
                             "ctVal": "0.01", "ctMult": "1", "state": "live", "lotSz": "1", "minSz": "1", "tickSz": "0.1"},
                            {"instId": "ETH-USDT-SWAP", "settleCcy": "USDT", "ctType": "linear", "instFamily": "ETH-USDT",
                             "ctVal": "0.1", "ctMult": "1", "state": "live", "lotSz": "1", "minSz": "1", "tickSz": "0.01"},
                            {"instId": "BTC-USD-SWAP", "settleCcy": "BTC", "ctType": "inverse", "instFamily": "BTC-USD",
                             "ctVal": "100", "ctMult": "1", "state": "live", "lotSz": "1", "minSz": "1", "tickSz": "0.1"}])
            return okx([{"instId": "BTC-USDT", "baseCcy": "BTC", "quoteCcy": "USDT", "state": "live", "lotSz": "0.0001", "minSz": "0.0001", "tickSz": "0.1"},
                        {"instId": "ETH-USDT", "baseCcy": "ETH", "quoteCcy": "USDT", "state": "live", "lotSz": "0.0001", "minSz": "0.0001", "tickSz": "0.1"}])
        if url.endswith("/market/tickers"):
            if it == "SWAP":
                return okx([{"instId": "BTC-USDT-SWAP", "volCcy24h": "90000", "last": "100.65", "bidPx": "100.6", "askPx": "100.7",
                             "bidSz": "400", "askSz": "400"},
                            {"instId": "ETH-USDT-SWAP", "volCcy24h": "500000", "last": "10", "bidPx": "10", "askPx": "10.01",
                             "bidSz": "40", "askSz": "40"}])
            return okx([{"instId": "BTC-USDT", "volCcy24h": "90000", "last": "100", "bidPx": "100", "askPx": "100.1",
                         "bidSz": "4", "askSz": "4"},
                        {"instId": "ETH-USDT", "volCcy24h": "500000", "last": "10", "bidPx": "10", "askPx": "10.01",
                         "bidSz": "4", "askSz": "4"}])
        if url.endswith("/public/funding-rate"):
            return okx([{"fundingRate": "0.0002", "fundingTime": "456"}])
    return 404, {"msg": "unexpected " + url}


class ScanTests(unittest.TestCase):
    def setUp(self):
        api_guard.reset_guards_for_tests()
        for name, lim in (("binance", 2400), ("binance-spot", 6000)):
            api_guard.get_guard(name, lim)._jitter = False
        api_guard.okx_guard(False)._jitter = False
        self.settings = _s(scanner_on=True)
        self.settings["modules"]["triangular"].update(enabled=True, confirm_enable=True)
        self.sess = FakeSession(router)

        async def factory():
            return self.sess
        self.sc = sc.ArbScanner(lambda: self.settings, factory)

    def run_scan(self, n=1):
        async def go():
            for _ in range(n):
                await self.sc.scan_once(self.settings)
        asyncio.run(go())

    def test_full_scan_fills_three_boards(self):
        self.run_scan()
        b = self.sc.board
        self.assertTrue(b["spatial"] and b["funding"] and b["triangular"])
        top = b["spatial"][0]
        self.assertEqual((top["base"], top["market"], top["buy"], top["sell"]), ("BTC", "futures", "binance", "okx"))
        self.assertAlmostEqual(top["gap"], (100.6 - 100.0) / 100.0 * 100, places=6)
        # OKX contract size: 400 contracts * 0.01 = 4 coins
        self.assertEqual(top["bq"], 4.0)
        self.assertEqual(b["funding"][0]["base"], "BTC")
        self.assertEqual(b["funding"][0]["ex"], "binance")
        self.assertEqual(b["triangular"][0]["base"], "ETH")

    def test_only_get_requests_and_no_keys(self):
        self.run_scan()
        self.assertTrue(self.sess.calls)
        for url, params in self.sess.calls:
            self.assertNotIn("signature", json.dumps(params or {}).lower())
        src = Path(sc.__file__).read_text()
        for bad in (".post(", ".delete(", ".put(", "X-MBX-APIKEY", "OK-ACCESS-KEY", "api_secret"):
            self.assertNotIn(bad, src)

    def test_inverse_and_quarterly_contracts_are_ignored(self):
        self.run_scan()
        self.assertNotIn("BTC-USD-SWAP", self.sc.instr[("okx", "futures")])
        self.assertNotIn("BTCUSDT_261225", self.sc.instr[("binance", "futures")])

    def test_top_n_applies_at_once(self):
        self.run_scan()
        self.assertEqual(len(self.sc.universe["spatial"]["futures"]), 2)
        self.settings["pairs"].update(top_n=1, include=[])
        self.run_scan()
        self.assertEqual(self.sc.universe["spatial"]["futures"], ["BTC"])

    def test_nothing_requested_when_all_modules_off(self):
        for m in self.settings["modules"].values():
            m["enabled"] = False
        self.run_scan()
        self.assertEqual(self.sess.calls, [])
        self.assertIn("No module is switched on.", self.sc.status["notes"])

    def test_triangular_locked_without_confirm(self):
        self.settings["modules"]["triangular"]["confirm_enable"] = False
        self.run_scan()
        self.assertEqual(self.sc.board["triangular"], [])
        self.assertTrue(any("LOCKED" in n for n in self.sc.status["notes"]))

    def test_funding_read_not_every_loop(self):
        self.run_scan(2)
        n = sum(1 for u, _ in self.sess.calls if u.endswith("/premiumIndex"))
        self.assertEqual(n, 1)

    def test_rules_not_reloaded_every_loop(self):
        self.run_scan(3)
        n = sum(1 for u, _ in self.sess.calls if u.endswith("/exchangeInfo"))
        self.assertEqual(n, 2)                          # futures + spot, once

    def test_429_pauses_guard_and_loop_survives(self):
        def r429(url, p):
            return 429, {"msg": "too many"}, {"Retry-After": "7"}
        self.sess = FakeSession(r429)
        with self.assertRaises(sc.ScanError):
            self.run_scan()
        self.assertTrue(api_guard.get_guard("binance").is_paused())

    def test_snapshot_shape(self):
        self.run_scan()
        snap = self.sc.snapshot()
        self.assertEqual(set(snap), {"status", "board"})
        self.assertIn("binance_weight_used", snap["status"])
        self.assertFalse(snap["status"]["paused"])


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        api_guard.reset_guards_for_tests()
        api_guard.get_guard("binance", 2400)._jitter = False
        api_guard.get_guard("binance-spot", 6000)._jitter = False
        api_guard.okx_guard(False)._jitter = False

    def scanner(self, resp):
        sess = FakeSession(lambda u, p: resp)

        async def factory():
            return sess
        return sc.ArbScanner(lambda: _s(), factory)

    def test_http_429_pauses_that_exchange(self):
        s = self.scanner((429, {"msg": "x"}, {"Retry-After": "9"}))

        async def go():
            with self.assertRaises(sc.ScanError):
                await s._fetch("binance", "https://fapi.binance.com/fapi/v1/ticker/bookTicker", None, 5)
        asyncio.run(go())
        self.assertTrue(api_guard.get_guard("binance").is_paused())
        self.assertFalse(api_guard.okx_guard(False).is_paused())
        self.assertTrue(s.snapshot()["status"]["paused"])

    def test_http_418_pauses_longer(self):
        s = self.scanner((418, {"msg": "banned"}, {"Retry-After": "300"}))

        async def go():
            with self.assertRaises(sc.ScanError):
                await s._fetch("binance", "https://fapi.binance.com/fapi/v1/ticker/bookTicker", None, 5)
        asyncio.run(go())
        g = api_guard.get_guard("binance")
        self.assertTrue(g.is_paused())
        self.assertTrue(g.banned)

    def test_okx_rate_code_pauses(self):
        s = self.scanner((200, {"code": "50011", "msg": "Too many requests", "data": []}))

        async def go():
            with self.assertRaises(sc.ScanError):
                await s._fetch("okx", "https://www.okx.com/api/v5/market/tickers", {"instType": "SWAP"}, 1)
        asyncio.run(go())
        self.assertTrue(api_guard.okx_guard(False).is_paused())

    def test_spot_and_testnet_use_their_own_guards(self):
        g = sc.ArbScanner._guard_for
        self.assertIs(g("binance", "https://fapi.binance.com/x"), api_guard.get_guard("binance"))
        self.assertIsNot(g("binance", "https://api.binance.com/x"), api_guard.get_guard("binance"))
        self.assertIsNot(g("binance", "https://testnet.binancefuture.com/x"), api_guard.get_guard("binance"))
        self.assertIs(g("okx", "https://www.okx.com/x"), api_guard.okx_guard(False))

    def test_run_loop_survives_a_failed_scan(self):
        s = self.scanner((500, {"msg": "boom"}))
        cfg = _s(scanner_on=True, loop_seconds=2)
        s.get_settings = lambda: cfg

        async def go():
            s.start()
            await asyncio.sleep(0.3)
            running = s.running
            await s.stop()
            return running
        self.assertTrue(asyncio.run(go()))
        self.assertIn("ScanError", s.status["last_error"] or "")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        from app.arb import service
        self.service = service
        self.d = tempfile.TemporaryDirectory()
        self.old = service.arb_settings.path
        service.arb_settings.path = Path(self.d.name) / "arb" / "settings.json"

    def tearDown(self):
        self.service.arb_settings.path = self.old
        self.d.cleanup()

    def test_state_has_defaults_first(self):
        s = self.service.get_state()
        self.assertEqual(s["settings"], st.DEFAULTS)
        self.assertTrue(s["live_available"])
        self.assertEqual(s["step"], 4)
        self.assertFalse(s["status"]["running"])

    def test_bad_save_raises_with_all_errors(self):
        async def go():
            await self.service.save_settings(_s(loop_seconds=1, pairs={"top_n": 0}))
        with self.assertRaises(self.service.ArbSaveError) as cm:
            asyncio.run(go())
        self.assertGreaterEqual(len(cm.exception.errors), 2)
        self.assertFalse(self.service.arb_settings.path.exists())

    def test_save_off_keeps_scanner_stopped(self):
        async def go():
            out = await self.service.save_settings(_s(pairs={"top_n": 9}))
            return out
        out = asyncio.run(go())
        self.assertEqual(out["settings"]["pairs"]["top_n"], 9)
        self.assertFalse(out["status"]["running"])

    def test_damaged_file_shows_defaults_and_message(self):
        p = self.service.arb_settings.path
        p.parent.mkdir(parents=True)
        p.write_text("{bad")
        s = self.service.get_state()
        self.assertIn("damaged", s["problem"])
        self.assertEqual(s["settings"], st.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
