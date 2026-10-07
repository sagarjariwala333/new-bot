import base64
import hashlib
import hmac
import json
import os
import sys
import unittest
import unittest.mock as mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

from app.okx_futures import (  # noqa: E402
    OKXFuturesClient, OKXAPIError, OKXPositionLimitError,
)
from app.binance_futures import SymbolInfo  # noqa: E402


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    """Records every call made through it; returns canned responses queued
    by the test, in order. Mirrors the pattern used elsewhere in this
    project's tests (fake_client.py) but scoped to HTTP request/response
    shape, since that's the layer this adapter's own logic lives in."""

    def __init__(self, responses):
        self.calls: list[dict] = []
        self._responses = list(responses)
        self.closed = False

    def request(self, method, url, headers=None, data=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return _FakeResponse(status, payload)

    async def close(self):
        self.closed = True


def _install_fake_session(client: OKXFuturesClient, responses):
    fake = _FakeSession(responses)

    async def _get_session():
        return fake

    client._get_session = _get_session
    return fake


class TestOKXSigning(unittest.TestCase):
    """Verifies the signature is computed exactly per OKX's documented
    formula: base64(HMAC-SHA256(secret, timestamp + method + requestPath + body)).
    This is the single most common OKX integration failure point, per the
    owner's explicit instruction - so this is checked against hand-computed
    expected output, not just "does it not crash"."""

    def test_signature_matches_manual_computation(self):
        client = OKXFuturesClient(api_key="k", api_secret="mysecret", passphrase="p")
        timestamp = "2026-09-14T12:00:00.000Z"
        method = "GET"
        request_path = "/api/v5/account/balance"
        body = ""
        expected_msg = f"{timestamp}{method}{request_path}{body}"
        expected_digest = hmac.new(b"mysecret", expected_msg.encode(), hashlib.sha256).digest()
        expected_sig = base64.b64encode(expected_digest).decode()
        actual_sig = client._sign(timestamp, method, request_path, body)
        self.assertEqual(actual_sig, expected_sig)

    def test_signature_includes_json_body_for_post(self):
        client = OKXFuturesClient(api_key="k", api_secret="mysecret", passphrase="p")
        timestamp = "2026-09-14T12:00:00.000Z"
        body = json.dumps({"instId": "BTC-USDT-SWAP", "sz": "1"}, separators=(",", ":"))
        sig_with_body = client._sign(timestamp, "POST", "/api/v5/trade/order", body)
        sig_without_body = client._sign(timestamp, "POST", "/api/v5/trade/order", "")
        # A signature that ignored the body would be a critical bug - every
        # POST would sign identically regardless of what's actually sent.
        self.assertNotEqual(sig_with_body, sig_without_body)

    def test_timestamp_format(self):
        ts = OKXFuturesClient._iso_timestamp()
        # e.g. 2026-09-14T12:34:56.789Z - exactly one 'T', ends in '.mmmZ'
        self.assertEqual(ts.count("T"), 1)
        self.assertTrue(ts.endswith("Z"))
        frac = ts.split(".")[1]
        self.assertEqual(len(frac), 4)  # 3 digits + 'Z'

    def test_headers_include_demo_flag_only_when_demo(self):
        live = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p", demo=False)
        demo = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p", demo=True)
        self.assertNotIn("x-simulated-trading", live._headers("ts", "sig"))
        self.assertEqual(demo._headers("ts", "sig")["x-simulated-trading"], "1")


class TestContractSizeConversion(unittest.IsolatedAsyncioTestCase):
    """The coin<->contract conversion is the other explicitly-flagged risk
    area (class docstring point 1) - instance.py must keep working entirely
    in coin quantities, with contracts only appearing inside this adapter."""

    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        # BTC-USDT-SWAP-like instrument: 1 contract = 0.01 BTC, lot step = 1 contract, min 1 contract
        info = SymbolInfo(symbol="BTC-USDT-SWAP", price_tick=0.1, qty_step=0.01,
                           min_qty=0.01, min_notional=0.0, market_qty_step=0.01,
                           market_min_qty=0.01, market_max_qty=float("inf"), max_notional=float("inf"))
        info.ct_val = 0.01
        self.client._symbol_cache["BTC-USDT-SWAP"] = info

    async def test_round_qty_snaps_to_whole_contracts_in_coin_terms(self):
        # 0.0357 BTC at ctVal=0.01 -> 3.57 contracts -> truncates to 3 contracts -> 0.03 BTC
        result = await self.client.round_qty("BTC-USDT-SWAP", 0.0357)
        self.assertEqual(result, 0.03)

    async def test_coin_qty_to_contracts_string(self):
        sz = await self.client._coin_qty_to_contracts("BTC-USDT-SWAP", 0.05)
        self.assertEqual(sz, "5")  # 0.05 / 0.01 = 5 contracts exactly

    async def test_check_min_notional_rejects_below_one_contract(self):
        ok, msg = await self.client.check_min_notional("BTC-USDT-SWAP", 0.001, 60000)
        self.assertFalse(ok)
        self.assertIn("minimum", msg)


class TestPositionLimitHandling(unittest.IsolatedAsyncioTestCase):
    """Hard requirement 5: on OKX error 54030, block NEW entries on that
    instrument (close-only), but never block close_position_market."""

    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        info = SymbolInfo(symbol="BTC-USDT-SWAP", price_tick=0.1, qty_step=0.01,
                           min_qty=0.01, min_notional=0.0, market_qty_step=0.01,
                           market_min_qty=0.01, market_max_qty=float("inf"), max_notional=float("inf"))
        info.ct_val = 0.01
        self.client._symbol_cache["BTC-USDT-SWAP"] = info
        self.client._leverage_state["BTC-USDT-SWAP"] = ("isolated", 5)

    async def test_54030_raises_and_blocks_instrument(self):
        error_payload = {"code": "1", "msg": "", "data": [{"sCode": "54030", "sMsg": "position limit exceeded"}]}
        _install_fake_session(self.client, [(200, error_payload)])
        with self.assertRaises(OKXPositionLimitError):
            await self.client.market_order("BTC-USDT-SWAP", "BUY", 0.05)
        self.assertIn("BTC-USDT-SWAP", self.client._blocked_instruments)

    async def test_blocked_instrument_rejects_new_entry_without_a_network_call(self):
        self.client._blocked_instruments.add("BTC-USDT-SWAP")
        fake = _install_fake_session(self.client, [])
        with self.assertRaises(OKXPositionLimitError):
            await self.client.market_order("BTC-USDT-SWAP", "BUY", 0.05)
        self.assertEqual(len(fake.calls), 0)  # never even tried - proactively blocked

    async def test_close_position_still_works_on_blocked_instrument(self):
        self.client._blocked_instruments.add("BTC-USDT-SWAP")
        ok_payload = {"code": "0", "msg": "", "data": [{"ordId": "1", "sCode": "0", "sMsg": ""}]}
        fake = _install_fake_session(self.client, [(200, ok_payload)])
        result = await self.client.close_position_market("BTC-USDT-SWAP", "SELL", 0.05)
        self.assertEqual(result["orderId"], "1")  # normalized to Binance-style field name
        # Confirm it actually went out as reduceOnly, not silently dropped
        sent_body = json.loads(fake.calls[0]["data"])
        self.assertTrue(sent_body["reduceOnly"])


class TestResponseNormalization(unittest.IsolatedAsyncioTestCase):
    """instance.py is written entirely against Binance's field names
    (positionAmt, entryPrice, algoId, clientAlgoId, orderType, triggerPrice,
    algoStatus, commission) - for ONE instance.py to run unchanged against
    either exchange, OKX's adapter must translate its native response shape
    into that same shape, not just match method signatures. This is the
    single most important correctness surface in this whole file."""

    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        info = SymbolInfo(symbol="BTC-USDT-SWAP", price_tick=0.1, qty_step=0.01,
                           min_qty=0.01, min_notional=0.0, market_qty_step=0.01,
                           market_min_qty=0.01, market_max_qty=float("inf"), max_notional=float("inf"))
        info.ct_val = 0.01
        self.client._symbol_cache["BTC-USDT-SWAP"] = info

    async def test_position_normalizes_contracts_to_coins_with_sign(self):
        raw = {"instId": "BTC-USDT-SWAP", "pos": "300", "avgPx": "60000"}  # 300 contracts long
        normalized = await self.client._normalize_position(raw)
        self.assertEqual(normalized["positionAmt"], 3.0)  # 300 * ctVal(0.01) = 3.0 BTC
        self.assertEqual(normalized["entryPrice"], "60000")

    async def test_position_preserves_short_sign(self):
        raw = {"instId": "BTC-USDT-SWAP", "pos": "-150", "avgPx": "60000"}
        normalized = await self.client._normalize_position(raw)
        self.assertEqual(normalized["positionAmt"], -1.5)

    async def test_order_normalizes_ordid_to_orderid(self):
        raw = {"ordId": "12345", "clOrdId": "abc"}
        normalized = self.client._normalize_order(raw)
        self.assertEqual(normalized["orderId"], "12345")
        self.assertNotIn("avgPrice", normalized)  # never fabricated when OKX didn't provide one

    async def test_algo_order_stop_maps_to_stop_market(self):
        raw = {"algoId": "1", "algoClOrdId": "abc", "state": "live", "slTriggerPx": "58000"}
        normalized = self.client._normalize_algo_order(raw, order_type_hint="STOP_MARKET")
        self.assertEqual(normalized["orderType"], "STOP_MARKET")
        self.assertEqual(normalized["clientAlgoId"], "abc")
        self.assertEqual(normalized["algoStatus"], "NEW")
        self.assertEqual(normalized["triggerPrice"], "58000")

    async def test_algo_order_effective_state_maps_to_finished(self):
        raw = {"algoId": "1", "algoClOrdId": "abc", "state": "effective", "slTriggerPx": "58000"}
        normalized = self.client._normalize_algo_order(raw, order_type_hint="STOP_MARKET")
        # instance.py checks algoStatus in ("TRIGGERED", "FINISHED") to detect a fill
        self.assertIn(normalized["algoStatus"], ("TRIGGERED", "FINISHED"))

    async def test_algo_order_infers_type_from_trigger_field_when_no_hint(self):
        sl_raw = {"algoId": "1", "algoClOrdId": "a", "state": "live", "slTriggerPx": "58000", "tpTriggerPx": "0"}
        tp_raw = {"algoId": "2", "algoClOrdId": "b", "state": "live", "slTriggerPx": "0", "tpTriggerPx": "65000"}
        self.assertEqual(self.client._normalize_algo_order(sl_raw)["orderType"], "STOP_MARKET")
        self.assertEqual(self.client._normalize_algo_order(tp_raw)["orderType"], "TAKE_PROFIT_MARKET")

    async def test_user_trades_fee_becomes_positive_commission(self):
        ok_payload = {"code": "0", "msg": "",
                      "data": [{"ordId": "1", "fee": "-1.2345", "fillPx": "60000"}]}
        _install_fake_session(self.client, [(200, ok_payload)])
        trades = await self.client.get_user_trades("BTC-USDT-SWAP")
        self.assertEqual(trades[0]["commission"], 1.2345)  # positive magnitude, sign flipped
        self.assertEqual(trades[0]["orderId"], "1")

    async def test_user_trades_fillpx_is_mapped_to_price(self):
        """Regression test for a real bug found 2026-09-15 while adding
        entry-price recovery: instance.py's fill-price recovery reads
        trade["price"] (Binance's real field name) - OKX's raw fill rows
        use "fillPx" instead. Without this mapping, that lookup would
        silently find nothing and fall through to a less accurate fallback,
        even though the real fill price was available all along."""
        ok_payload = {"code": "0", "msg": "",
                      "data": [{"ordId": "1", "fee": "-0.5", "fillPx": "63451.7"}]}
        _install_fake_session(self.client, [(200, ok_payload)])
        trades = await self.client.get_user_trades("BTC-USDT-SWAP")
        self.assertEqual(trades[0]["price"], "63451.7")


class TestAlgoOrderInstIdCache(unittest.IsolatedAsyncioTestCase):
    """Class docstring point 3: cancel/query-by-algoId need instId on OKX,
    unlike Binance - this adapter must cache it rather than silently
    sending a malformed request."""

    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")

    async def test_cancel_unknown_algo_id_raises_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            await self.client.cancel_algo_order("999")

    async def test_cancel_uses_cached_inst_id(self):
        self.client._algo_inst_cache["777"] = "BTC-USDT-SWAP"
        ok_payload = {"code": "0", "msg": "", "data": [{"algoId": "777", "sCode": "0", "sMsg": ""}]}
        fake = _install_fake_session(self.client, [(200, ok_payload)])
        await self.client.cancel_algo_order("777")
        sent_body = json.loads(fake.calls[0]["data"])
        self.assertEqual(sent_body["algosData"][0]["instId"], "BTC-USDT-SWAP")


class TestGetKlinesNormalization(unittest.IsolatedAsyncioTestCase):
    """Regression tests for a critical bug found 2026-09-15, before it was
    ever caught by a test or a real run: OKX's real candle response is
    9 fields wide ([ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]), but every
    downstream caller (instance.py, analysis endpoints) builds a DataFrame
    assuming Binance's 12-column shape. Unnormalized, this would crash
    immediately - `ValueError: 12 columns passed, passed data had 9
    columns` - on a live OKX pair's very first candle fetch. Never caught
    before because FakeClient.get_klines() (used by every other test)
    always returns Binance-shaped synthetic data regardless of platform."""

    async def test_real_okx_shaped_response_is_normalized_to_12_columns(self):
        client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        # Realistic raw OKX response - newest-first, 9 fields, exactly the
        # documented [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm] shape.
        payload = {"code": "0", "msg": "", "data": [
            ["1700000120000", "60150", "60300", "60100", "60250", "9.1", "547000", "547000", "0"],
            ["1700000060000", "60050", "60200", "60000", "60150", "8.2", "493000", "493000", "1"],
            ["1700000000000", "60000", "60100", "59900", "60050", "10.5", "630000", "630000", "1"],
        ]}
        _install_fake_session(client, [(200, payload)])

        rows = await client.get_klines("BTC-USDT-SWAP", "1h", limit=3)

        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(len(row), 12, "every row must be normalized to Binance's 12-column shape")

    async def test_normalized_output_survives_the_exact_dataframe_construction_instance_py_uses(self):
        """The actual regression proof: reproduces instance.py's own
        DataFrame construction line-for-line against this function's
        output - this must NOT raise, and open/high/low/close/open_time
        must be correct, not just present."""
        client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        payload = {"code": "0", "msg": "", "data": [
            ["1700000060000", "60050", "60200", "60000", "60150", "8.2", "493000", "493000", "1"],
            ["1700000000000", "60000", "60100", "59900", "60050", "10.5", "630000", "630000", "1"],
        ]}
        _install_fake_session(client, [(200, payload)])

        raw = await client.get_klines("BTC-USDT-SWAP", "1h", limit=2)

        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tbbav", "tbqav", "ignore",
        ])
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        df["open_time"] = df["open_time"].astype("int64")

        # Oldest-first (get_klines reverses OKX's newest-first response).
        self.assertEqual(df["open_time"].tolist(), [1700000000000, 1700000060000])
        self.assertEqual(df["open"].tolist(), [60000.0, 60050.0])
        self.assertEqual(df["close"].tolist(), [60050.0, 60150.0])
        self.assertEqual(df["high"].tolist(), [60100.0, 60200.0])
        self.assertEqual(df["low"].tolist(), [59900.0, 60000.0])

    async def test_oldest_first_ordering_preserved_after_normalization(self):
        client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        payload = {"code": "0", "msg": "", "data": [
            ["3000", "3", "3", "3", "3", "1", "1", "1", "1"],   # newest
            ["2000", "2", "2", "2", "2", "1", "1", "1", "1"],
            ["1000", "1", "1", "1", "1", "1", "1", "1", "1"],  # oldest
        ]}
        _install_fake_session(client, [(200, payload)])

        rows = await client.get_klines("BTC-USDT-SWAP", "1h", limit=3)

        self.assertEqual([r[0] for r in rows], [1000, 2000, 3000], "must be oldest-first, not OKX's native newest-first")


class TestTriggerPxTypeIsActuallyUsed(unittest.IsolatedAsyncioTestCase):
    """2026-09-16 fix, owner request: trigger_px_type used to be hardcoded
    to "mark" regardless of what was passed in - the dashboard let you
    pick a trigger price basis that had zero actual effect on the real
    order. Confirms the caller's chosen value now genuinely reaches the
    request body."""

    async def test_stop_market_order_uses_the_passed_trigger_px_type(self):
        client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        ok_payload = {"code": "0", "msg": "", "data": [{"algoId": "1", "sCode": "0"}]}
        fake = _install_fake_session(client, [(200, ok_payload)])

        await client.stop_market_order("BTC-USDT-SWAP", "sell", 95.5, trigger_px_type="last")

        sent_body = json.loads(fake.calls[0]["data"])
        self.assertEqual(sent_body["slTriggerPxType"], "last",
                         "must use the caller's chosen trigger price basis, not a hardcoded one")

    async def test_take_profit_market_order_uses_the_passed_trigger_px_type(self):
        client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        ok_payload = {"code": "0", "msg": "", "data": [{"algoId": "1", "sCode": "0"}]}
        fake = _install_fake_session(client, [(200, ok_payload)])

        await client.take_profit_market_order("BTC-USDT-SWAP", "buy", 3200.0, trigger_px_type="index")

        sent_body = json.loads(fake.calls[0]["data"])
        self.assertEqual(sent_body["tpTriggerPxType"], "index")

    async def test_default_trigger_px_type_is_still_mark(self):
        """Backward compatible - an existing pair's config that never set
        trigger_px_type explicitly must keep behaving exactly as before
        this fix (mark price), not silently change to something else."""
        client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        ok_payload = {"code": "0", "msg": "", "data": [{"algoId": "1", "sCode": "0"}]}
        fake = _install_fake_session(client, [(200, ok_payload)])

        await client.stop_market_order("BTC-USDT-SWAP", "sell", 95.5)  # no trigger_px_type passed

        sent_body = json.loads(fake.calls[0]["data"])
        self.assertEqual(sent_body["slTriggerPxType"], "mark")


class TestGetLeverageBrackets(unittest.IsolatedAsyncioTestCase):
    """OKX's public position-tiers endpoint returns SIZE-based tiers, not
    notional-based like Binance - this confirms the conversion via ctVal
    and mark price produces the correctly-shaped, comparable output."""

    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        info = SymbolInfo(symbol="BTC-USDT-SWAP", price_tick=0.1, qty_step=0.01,
                           min_qty=0.01, min_notional=0.0, market_qty_step=0.01,
                           market_min_qty=0.01, market_max_qty=float("inf"), max_notional=float("inf"))
        info.ct_val = 0.01  # 1 contract = 0.01 BTC
        self.client._symbol_cache["BTC-USDT-SWAP"] = info
        self.client._leverage_state["BTC-USDT-SWAP"] = ("isolated", 5)

    async def test_size_tiers_converted_to_notional_via_ctval_and_price(self):
        tiers_payload = {"code": "0", "msg": "", "data": [
            {"tier": "1", "minSz": "0", "maxSz": "1000", "maxLever": "125", "mmr": "0.004", "imr": "0.01"},
            {"tier": "2", "minSz": "1000", "maxSz": "", "maxLever": "50", "mmr": "0.01", "imr": "0.02"},
        ]}
        mark_price_payload = {"code": "0", "msg": "", "data": [{"markPx": "60000"}]}
        _install_fake_session(self.client, [(200, mark_price_payload), (200, tiers_payload)])

        brackets = await self.client.get_leverage_brackets("BTC-USDT-SWAP")

        self.assertEqual(len(brackets), 2)
        # 1000 contracts * 0.01 ctVal * 60000 price = 600,000 notional cap
        self.assertAlmostEqual(brackets[0]["notional_cap"], 600_000.0, places=1)
        self.assertEqual(brackets[0]["max_leverage"], 125)
        self.assertEqual(brackets[1]["notional_cap"], float("inf"))  # empty maxSz = uncapped
        self.assertEqual(brackets[1]["max_leverage"], 50)


class TestRoundStepDecimalMultiples(unittest.TestCase):
    """Regression test for a real bug found 2026-09-14: the original
    implementation used Decimal.quantize(), which rounds to the same number
    of DECIMAL PLACES as the step value - not to the nearest MULTIPLE of it.
    This is silently wrong whenever step is a whole number (common for OKX
    lot sizes: 1, 5, 10 contracts), even though it happened to work for
    Binance's always-fractional step sizes."""

    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")

    def test_fractional_step_unaffected_regression_guard(self):
        # Confirms the fix doesn't change behavior for the case that always
        # worked (fractional steps, matching Binance's real-world values).
        self.assertEqual(self.client._round_step_decimal(0.0357, 0.01), 0.03)
        self.assertEqual(self.client._round_step_decimal(63451.7, 0.1), 63451.7)

    def test_whole_number_lot_size_of_one(self):
        # The exact bug found: 0.1 contracts with lot_sz=1 must truncate to
        # 0 (below minimum tradeable size), not stay at 0.1.
        self.assertEqual(self.client._round_step_decimal(0.1, 1.0), 0.0)

    def test_whole_number_lot_size_of_five(self):
        # 7.8 contracts with lot_sz=5 must truncate down to the nearest
        # multiple of 5, i.e. 5.0 - not stay at 7.8.
        self.assertEqual(self.client._round_step_decimal(7.8, 5.0), 5.0)

    def test_whole_number_lot_size_of_ten(self):
        self.assertEqual(self.client._round_step_decimal(23.0, 10.0), 20.0)

    def test_exact_multiple_is_unchanged(self):
        self.assertEqual(self.client._round_step_decimal(15.0, 5.0), 15.0)

    def test_zero_step_returns_value_unchanged(self):
        self.assertEqual(self.client._round_step_decimal(3.456, 0), 3.456)


class TestGetOrderByClientId(unittest.IsolatedAsyncioTestCase):
    """OKX's equivalent of Binance's get_order_by_client_id - added
    2026-09-14 after a third-party review found market_order/
    close_position_market had NO ambiguous-order recovery at all, unlike
    Binance's versions of the same two methods."""

    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")

    async def test_found_order_returns_normalized_dict(self):
        payload = {"code": "0", "msg": "", "data": [
            {"ordId": "999", "clOrdId": "abc", "state": "filled", "avgPx": "63000.5"}
        ]}
        _install_fake_session(self.client, [(200, payload)])
        result = await self.client.get_order_by_client_id("BTC-USDT-SWAP", "abc")
        self.assertEqual(result["orderId"], "999")  # normalized from ordId
        self.assertEqual(result["avgPrice"], "63000.5")

    async def test_empty_data_array_means_not_found(self):
        payload = {"code": "0", "msg": "", "data": []}
        _install_fake_session(self.client, [(200, payload)])
        result = await self.client.get_order_by_client_id("BTC-USDT-SWAP", "never-existed")
        self.assertIsNone(result)

    async def test_recognized_not_found_error_code_returns_none(self):
        error_payload = {"code": "1", "msg": "", "data": [{"sCode": "51603", "sMsg": "Order does not exist"}]}
        _install_fake_session(self.client, [(200, error_payload)])
        result = await self.client.get_order_by_client_id("BTC-USDT-SWAP", "abc")
        self.assertIsNone(result)

    async def test_unrecognized_error_is_not_swallowed(self):
        """Critical: an error that does NOT mean 'not found' must propagate,
        not be silently treated as 'the order never happened' - that would
        be guessing in exactly the direction that could cause a duplicate
        entry. An unrecognized code falls through to _request's generic
        retry path (3 attempts by default) before finally propagating -
        queuing 3 copies to correctly simulate that, not a single failure."""
        error_payload = {"code": "1", "msg": "", "data": [{"sCode": "50001", "sMsg": "service unavailable"}]}
        sleep_patcher = mock.patch("app.okx_futures.asyncio.sleep", new=mock.AsyncMock())
        sleep_patcher.start()
        try:
            _install_fake_session(self.client, [(200, error_payload)] * 3)
            with self.assertRaises(OKXAPIError):
                await self.client.get_order_by_client_id("BTC-USDT-SWAP", "abc")
        finally:
            sleep_patcher.stop()


class TestMarketOrderAmbiguousRecovery(unittest.IsolatedAsyncioTestCase):
    """Regression tests for the exact gap found in the bug-hunt review:
    OKX's market_order/close_position_market had no equivalent of
    Binance's ambiguous-response recovery. Mirrors Binance's own tested
    behavior: on a network-level failure specifically, query back by the
    same client_order_id before concluding the order never happened.

    IMPORTANT test-design note discovered while writing these: _request()
    has its OWN internal retry loop (3 attempts by default) for network
    errors - so a single queued failure gets retried internally and never
    reaches market_order's own except block at all. Every test below
    queues exactly `retries` failures for the original POST to correctly
    exhaust that internal retry budget first, THEN queues the outer
    recovery GET's response - matching what actually happens against a
    real flaky connection, not an idealized single-failure case."""

    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        info = SymbolInfo(symbol="BTC-USDT-SWAP", price_tick=0.1, qty_step=0.01,
                           min_qty=0.01, min_notional=0.0, market_qty_step=0.01,
                           market_min_qty=0.01, market_max_qty=float("inf"), max_notional=float("inf"))
        info.ct_val = 0.01
        self.client._symbol_cache["BTC-USDT-SWAP"] = info
        self.client._leverage_state["BTC-USDT-SWAP"] = ("isolated", 5)
        # Don't actually sleep through _request's real retry backoff delays.
        self._sleep_patcher = mock.patch("app.okx_futures.asyncio.sleep", new=mock.AsyncMock())
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    async def test_market_order_recovers_when_it_actually_went_through(self):
        import tests._aiohttp_stub as stub

        found_payload = {"code": "0", "msg": "", "data": [
            {"ordId": "555", "clOrdId": "x", "state": "filled", "avgPx": "63001.0"}
        ]}
        fake = _install_fake_session(self.client, [
            stub.ClientError("connection reset"),   # POST attempt 1/3
            stub.ClientError("connection reset"),   # POST attempt 2/3
            stub.ClientError("connection reset"),   # POST attempt 3/3 - retries exhausted
            (200, found_payload),                    # the follow-up GET query finds it
        ])

        result = await self.client.market_order("BTC-USDT-SWAP", "BUY", 0.05)

        self.assertEqual(result["orderId"], "555")
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(fake.calls[-1]["method"], "GET",
                         "the final call must be the recovery query, after retries were exhausted")

    async def test_market_order_reraises_when_genuinely_not_found(self):
        """If the follow-up query confirms the order never happened, the
        original ambiguous exception is re-raised (not swallowed) - the
        caller (instance.py) still needs to know something went wrong and
        can flag pending reconciliation, exactly as it already does for
        Binance."""
        import tests._aiohttp_stub as stub

        not_found_payload = {"code": "0", "msg": "", "data": []}
        _install_fake_session(self.client, [
            stub.ClientError("connection reset"),
            stub.ClientError("connection reset"),
            stub.ClientError("connection reset"),
            (200, not_found_payload),
        ])

        with self.assertRaises(stub.ClientError):
            await self.client.market_order("BTC-USDT-SWAP", "BUY", 0.05)

    async def test_market_order_attaches_client_order_id_to_reraised_exception(self):
        """instance.py's pending-reconciliation flagging reads
        exc.client_order_id off the re-raised exception (same pattern as
        Binance) - confirms it's actually attached, not just that an
        exception of the right type comes back."""
        import tests._aiohttp_stub as stub

        not_found_payload = {"code": "0", "msg": "", "data": []}
        _install_fake_session(self.client, [
            stub.ClientError("connection reset"),
            stub.ClientError("connection reset"),
            stub.ClientError("connection reset"),
            (200, not_found_payload),
        ])

        with self.assertRaises(stub.ClientError) as ctx:
            await self.client.market_order("BTC-USDT-SWAP", "BUY", 0.05)
        self.assertTrue(hasattr(ctx.exception, "client_order_id"))
        self.assertTrue(ctx.exception.client_order_id)

    async def test_clean_rejection_is_not_treated_as_ambiguous(self):
        """A clean OKX rejection (e.g. insufficient margin) means OKX
        definitively said no - this must propagate immediately as an
        OKXAPIError, WITHOUT attempting a recovery query at all (there's
        nothing ambiguous to resolve), and WITHOUT being retried by
        _request's own network-error retry loop either (NON_RETRYABLE_CODES
        already covers this)."""
        rejection_payload = {"code": "1", "msg": "", "data": [{"sCode": "51008", "sMsg": "insufficient margin"}]}
        fake = _install_fake_session(self.client, [(200, rejection_payload)])

        with self.assertRaises(OKXAPIError):
            await self.client.market_order("BTC-USDT-SWAP", "BUY", 0.05)
        self.assertEqual(len(fake.calls), 1, "a clean rejection must not trigger a recovery query")


class TestClosePositionMarketAmbiguousRecovery(unittest.IsolatedAsyncioTestCase):
    """Same coverage as TestMarketOrderAmbiguousRecovery, for the close side."""

    def setUp(self):
        self.client = OKXFuturesClient(api_key="k", api_secret="s", passphrase="p")
        info = SymbolInfo(symbol="BTC-USDT-SWAP", price_tick=0.1, qty_step=0.01,
                           min_qty=0.01, min_notional=0.0, market_qty_step=0.01,
                           market_min_qty=0.01, market_max_qty=float("inf"), max_notional=float("inf"))
        info.ct_val = 0.01
        self.client._symbol_cache["BTC-USDT-SWAP"] = info
        self.client._leverage_state["BTC-USDT-SWAP"] = ("isolated", 5)
        self._sleep_patcher = mock.patch("app.okx_futures.asyncio.sleep", new=mock.AsyncMock())
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    async def test_close_recovers_when_it_actually_went_through(self):
        import tests._aiohttp_stub as stub

        found_payload = {"code": "0", "msg": "", "data": [
            {"ordId": "777", "clOrdId": "x", "state": "filled", "avgPx": "62999.0"}
        ]}
        _install_fake_session(self.client, [
            stub.ClientError("timeout"),
            stub.ClientError("timeout"),
            stub.ClientError("timeout"),
            (200, found_payload),
        ])

        result = await self.client.close_position_market("BTC-USDT-SWAP", "SELL", 0.05)
        self.assertEqual(result["orderId"], "777")

    async def test_close_reraises_when_genuinely_not_found(self):
        import tests._aiohttp_stub as stub

        not_found_payload = {"code": "0", "msg": "", "data": []}
        _install_fake_session(self.client, [
            stub.ClientError("timeout"),
            stub.ClientError("timeout"),
            stub.ClientError("timeout"),
            (200, not_found_payload),
        ])

        with self.assertRaises(stub.ClientError):
            await self.client.close_position_market("BTC-USDT-SWAP", "SELL", 0.05)


if __name__ == "__main__":
    unittest.main()
