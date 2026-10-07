"""
test_algo_order_api.py
=======================

Direct verification that BinanceFuturesClient's algo-order methods send the
EXACT endpoint paths and parameter names confirmed against Binance's current
official REST API reference (developers.binance.com, fetched 2026-09-12) -
not the guessed/wrong endpoints this project used before that fix. This is
the most direct check possible without a real network call: it mocks only
the low-level _request() call and asserts what each method actually sends.
"""

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402

import app.binance_futures as binance_futures  # noqa: E402
from app.binance_futures import BinanceFuturesClient  # noqa: E402


class _RecordingClient(BinanceFuturesClient):
    """Same client, but _request is replaced with a recorder instead of an
    actual HTTP call - captures (method, path, params) for assertion."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = []
        self.canned_response = {}
        # The one-time order-rate-limit fetch (see _ensure_order_rate_limits)
        # would otherwise show up as an extra recorded call here, ahead of
        # the actual method under test - mark it already-fetched so these
        # tests observe only the call each test is actually about.
        self._order_limits_fetched = True

    async def _request(self, method, path, signed=False, params=None, **kwargs):
        self.calls.append((method, path, dict(params or {})))
        return self.canned_response


class TestAlgoOrderPlacement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = _RecordingClient(api_key="x", api_secret="y")

    async def test_stop_market_order_uses_the_algo_endpoint(self):
        await self.client.stop_market_order("BTCUSDT", "SELL", 95.5)
        method, path, params = self.client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/fapi/v1/algoOrder",
                         "STOP_MARKET must go through the Algo Order API, not /fapi/v1/order")
        self.assertEqual(params["algoType"], "CONDITIONAL")
        self.assertEqual(params["symbol"], "BTCUSDT")
        self.assertEqual(params["side"], "SELL")
        self.assertEqual(params["type"], "STOP_MARKET")
        self.assertEqual(params["triggerPrice"], 95.5, "must use triggerPrice, not stopPrice, on this endpoint")
        self.assertNotIn("stopPrice", params, "stopPrice is the wrong field name for the Algo Order API")
        self.assertEqual(params["closePosition"], "true")
        self.assertNotIn("quantity", params, "quantity cannot be sent with closePosition=true")
        self.assertIn("clientAlgoId", params)
        self.assertTrue(params["clientAlgoId"].startswith("hullbot_"))
        self.assertEqual(params["priceProtect"], "false",
                         "explicit owner decision 2026-09-14: SL must fire unconditionally, no Binance delay")
        self.assertEqual(params["workingType"], "MARK_PRICE",
                         "2026-09-16 fix: must agree with validate_stop_side, which has always "
                         "validated against mark price - CONTRACT_PRICE here would let this order "
                         "trigger/reject based on a different, divergent price than what was checked")

    async def test_take_profit_market_order_uses_the_algo_endpoint(self):
        await self.client.take_profit_market_order("ETHUSDT", "BUY", 3200.0)
        method, path, params = self.client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/fapi/v1/algoOrder")
        self.assertEqual(params["type"], "TAKE_PROFIT_MARKET")
        self.assertEqual(params["triggerPrice"], 3200.0)
        self.assertEqual(params["closePosition"], "true")
        self.assertEqual(params["priceProtect"], "false",
                         "explicit owner decision 2026-09-14: TP must fire unconditionally, no Binance delay")
        self.assertEqual(params["workingType"], "MARK_PRICE",
                         "2026-09-16 fix: same reasoning as the stop-market test above")

    async def test_cancel_algo_order_uses_the_algo_endpoint_with_no_symbol(self):
        """Confirmed against the current docs: DELETE /fapi/v1/algoOrder
        takes algoId (or clientAlgoId) only - no symbol parameter, unlike
        the regular order cancel endpoint."""
        await self.client.cancel_algo_order(123456)
        method, path, params = self.client.calls[0]
        self.assertEqual(method, "DELETE")
        self.assertEqual(path, "/fapi/v1/algoOrder")
        self.assertEqual(params["algoId"], 123456)
        self.assertNotIn("symbol", params)

    async def test_get_open_algo_orders_uses_the_algo_endpoint_with_symbol(self):
        await self.client.get_open_algo_orders("BTCUSDT")
        method, path, params = self.client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/fapi/v1/openAlgoOrders")
        self.assertEqual(params["symbol"], "BTCUSDT")
        self.assertEqual(params["algoType"], "CONDITIONAL")

    async def test_get_algo_order_uses_the_algo_endpoint_with_no_symbol(self):
        await self.client.get_algo_order(999)
        method, path, params = self.client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/fapi/v1/algoOrder")
        self.assertEqual(params["algoId"], 999)
        self.assertNotIn("symbol", params)

    async def test_cancel_all_algo_open_orders_requires_symbol(self):
        await self.client.cancel_all_algo_open_orders("BTCUSDT")
        method, path, params = self.client.calls[0]
        self.assertEqual(method, "DELETE")
        self.assertEqual(path, "/fapi/v1/algoOpenOrders")
        self.assertEqual(params["symbol"], "BTCUSDT")

    async def test_market_order_still_uses_the_regular_endpoint(self):
        """MARKET orders were never part of the Dec 2025 migration - only
        STOP_MARKET/TAKE_PROFIT_MARKET/STOP/TAKE_PROFIT/TRAILING_STOP_MARKET
        moved. Entries/closes must stay on /fapi/v1/order."""
        await self.client.market_order("BTCUSDT", "BUY", 0.01)
        method, path, params = self.client.calls[0]
        self.assertEqual(path, "/fapi/v1/order")
        self.assertEqual(params["type"], "MARKET")

    async def test_close_position_market_still_uses_the_regular_endpoint(self):
        await self.client.close_position_market("BTCUSDT", "SELL", 0.01)
        method, path, params = self.client.calls[0]
        self.assertEqual(path, "/fapi/v1/order")
        self.assertEqual(params["type"], "MARKET")
        self.assertEqual(params["reduceOnly"], "true")


class TestOrderRateLimitTracking(unittest.IsolatedAsyncioTestCase):
    """Item 13 fix: the bot must track Binance's X-MBX-ORDER-COUNT-* headers
    proactively and self-throttle before hitting the limit, not just react
    after a rejection. Confirmed header format and rateLimits schema
    against Binance's current documentation, not guessed."""

    def setUp(self):
        # 2026-09-16: rate-limit tracking is now SHARED per account_id (see
        # _SharedAccountOrderRateTracker's own docstring in
        # binance_futures.py) via a module-level registry that persists for
        # the process lifetime - correct in production, but every client in
        # this test class uses the same default account_id (""), so without
        # this reset, one test's fetched/counted state would leak into the
        # next.
        binance_futures._reset_all_shared_rate_trackers_FOR_TESTS_ONLY()

    def _client_with_headers(self, headers: dict):
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        class _FakeResp:
            def __init__(self, hdrs):
                self.headers = hdrs
                self.status = 200

            async def json(self, content_type=None):
                return {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            closed = False

            def request(self, method, url, params=None):
                return _FakeResp(headers)

        client._session = _FakeSession()
        return client

    async def test_headers_are_captured_case_insensitively(self):
        client = self._client_with_headers({"X-MBX-ORDER-COUNT-10S": "7", "x-mbx-order-count-1m": "42"})
        await client._request_once("POST", "/fapi/v1/order", signed=False, params={})

        self.assertEqual(client._order_counts.get("10s"), 7)
        self.assertEqual(client._order_counts.get("1m"), 42)

    async def test_ensure_order_rate_limits_parses_exchange_info(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return {"rateLimits": [
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 2400},
                {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 300},
                {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 1200},
            ]}

        client._request = fake_request
        await client._ensure_order_rate_limits()

        self.assertEqual(client._order_limits.get("10s"), 300)
        self.assertEqual(client._order_limits.get("1m"), 1200)
        self.assertNotIn("1m_weight", client._order_limits)  # REQUEST_WEIGHT row must be ignored here

    async def test_ensure_order_rate_limits_only_fetches_once(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        calls = {"n": 0}

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            calls["n"] += 1
            return {"rateLimits": []}

        client._request = fake_request
        await client._ensure_order_rate_limits()
        await client._ensure_order_rate_limits()
        await client._ensure_order_rate_limits()

        self.assertEqual(calls["n"], 1, "exchangeInfo must only be fetched once, then cached")

    async def test_throttles_when_usage_is_near_the_limit(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True
        client._order_limits = {"10s": 100}
        client._order_counts = {"10s": 95}  # 95% used

        with unittest.mock.patch("app.binance_futures.asyncio.sleep", new=unittest.mock.AsyncMock()) as mock_sleep:
            await client._throttle_if_near_order_limit()

        mock_sleep.assert_awaited_once()

    async def test_wait_is_sized_to_the_windows_own_duration_not_a_flat_constant(self):
        """Item 7 refinement: previously a flat 2.0s regardless of which
        window was hot - now sized to roughly the window's own duration,
        since there's no way to get a fresher count without sending a
        request (re-checking the same stale counters wouldn't prove
        anything reset)."""
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True
        client._order_limits = {"10s": 100}
        client._order_counts = {"10s": 95}

        with unittest.mock.patch("app.binance_futures.asyncio.sleep", new=unittest.mock.AsyncMock()) as mock_sleep:
            await client._throttle_if_near_order_limit()

        mock_sleep.assert_awaited_once_with(10.0)

    async def test_wait_for_the_one_minute_window_is_sized_accordingly(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True
        client._order_limits = {"1m": 1200}
        client._order_counts = {"1m": 1150}  # ~96% used

        with unittest.mock.patch("app.binance_futures.asyncio.sleep", new=unittest.mock.AsyncMock()) as mock_sleep:
            await client._throttle_if_near_order_limit()

        mock_sleep.assert_awaited_once_with(60.0)

    async def test_wait_is_capped_at_a_sane_maximum(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True
        client._order_limits = {"1d": 100000}
        client._order_counts = {"1d": 99000}  # 99% used, but an 86400s wait would be absurd

        with unittest.mock.patch("app.binance_futures.asyncio.sleep", new=unittest.mock.AsyncMock()) as mock_sleep:
            await client._throttle_if_near_order_limit()

        mock_sleep.assert_awaited_once_with(65.0)

    def test_parse_interval_seconds_handles_every_unit(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        self.assertEqual(client._parse_interval_seconds("10s"), 10.0)
        self.assertEqual(client._parse_interval_seconds("1m"), 60.0)
        self.assertEqual(client._parse_interval_seconds("1h"), 3600.0)
        self.assertEqual(client._parse_interval_seconds("1d"), 86400.0)

    def test_parse_interval_seconds_falls_back_safely_on_unexpected_format(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        self.assertEqual(client._parse_interval_seconds("weird"), 10.0)
        self.assertEqual(client._parse_interval_seconds(""), 10.0)

    async def test_does_not_throttle_when_usage_is_comfortable(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True
        client._order_limits = {"10s": 100}
        client._order_counts = {"10s": 20}  # 20% used

        with unittest.mock.patch("app.binance_futures.asyncio.sleep", new=unittest.mock.AsyncMock()) as mock_sleep:
            await client._throttle_if_near_order_limit()

        mock_sleep.assert_not_awaited()

    async def test_missing_count_or_limit_data_is_a_safe_no_op(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True
        client._order_limits = {}
        client._order_counts = {}

        with unittest.mock.patch("app.binance_futures.asyncio.sleep", new=unittest.mock.AsyncMock()) as mock_sleep:
            await client._throttle_if_near_order_limit()  # must not raise

        mock_sleep.assert_not_awaited()

    async def test_market_order_and_close_are_deliberately_not_throttled(self):
        """Trade execution stays fast/direct - only routine SL/TP placement
        and cancellation get the proactive throttle, per the same
        "decisive, no exceptions" principle as priceProtect above."""
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True
        client._order_limits = {"10s": 100}
        client._order_counts = {"10s": 99}  # 99% used - would definitely throttle if checked

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return {"orderId": 1, "avgPrice": "100"}

        client._request = fake_request
        with unittest.mock.patch("app.binance_futures.asyncio.sleep", new=unittest.mock.AsyncMock()) as mock_sleep:
            await client.market_order("BTCUSDT", "BUY", 0.01)
            await client.close_position_market("BTCUSDT", "SELL", 0.01)

        mock_sleep.assert_not_awaited()


class TestSharedAccountOrderRateTracking(unittest.TestCase):
    """2026-09-16 fix (flagged by a third-party review, confirmed against
    Binance's own docs: "the order rate limit is counted against each
    account", not per symbol/pair): two clients for the SAME account_id
    must share rate-limit state; two clients for DIFFERENT account_ids
    must not."""

    def setUp(self):
        binance_futures._reset_all_shared_rate_trackers_FOR_TESTS_ONLY()

    def test_two_clients_same_account_share_order_counts(self):
        client_a = BinanceFuturesClient(api_key="k1", api_secret="s1", account_id="acc1")
        client_b = BinanceFuturesClient(api_key="k2", api_secret="s2", account_id="acc1")

        client_a._order_counts["10s"] = 95

        self.assertEqual(client_b._order_counts.get("10s"), 95,
                         "a second pair's client on the SAME account must see the first "
                         "pair's order-count usage, not a private copy of its own")

    def test_two_clients_different_accounts_do_not_share(self):
        client_a = BinanceFuturesClient(api_key="k1", api_secret="s1", account_id="acc1")
        client_b = BinanceFuturesClient(api_key="k2", api_secret="s2", account_id="acc2")

        client_a._order_counts["10s"] = 95

        self.assertIsNone(client_b._order_counts.get("10s"),
                          "different accounts must never share rate-limit state with each other")

    def test_default_account_id_is_unique_per_instance_not_shared(self):
        """The safe-by-default behavior: a caller that forgets to pass
        account_id must never accidentally share state with some other
        unrelated client that also forgot."""
        client_a = BinanceFuturesClient(api_key="k1", api_secret="s1")
        client_b = BinanceFuturesClient(api_key="k2", api_secret="s2")

        client_a._order_counts["10s"] = 95

        self.assertIsNone(client_b._order_counts.get("10s"),
                          "two clients that both omit account_id must NOT share state")

    def test_fetched_limits_are_also_shared_not_just_counts(self):
        client_a = BinanceFuturesClient(api_key="k1", api_secret="s1", account_id="acc1")
        client_b = BinanceFuturesClient(api_key="k2", api_secret="s2", account_id="acc1")

        client_a._order_limits["10s"] = 300
        client_a._order_limits_fetched = True

        self.assertEqual(client_b._order_limits.get("10s"), 300)
        self.assertTrue(client_b._order_limits_fetched,
                        "if pair A already fetched the account's real limits, pair B must "
                        "see that too, not re-fetch (or worse, assume it hasn't been fetched)")


class TestAmbiguousMarketOrderRetry(unittest.IsolatedAsyncioTestCase):
    """Item 6 fix - flagged by all three third-party reviews as the single
    most important remaining live-trading risk: if a market entry/close
    times out or hits a network error, Binance may have actually received
    and filled it - only the response was lost. Blindly treating that as
    "never happened" risks a real duplicate position on retry. Fixed by
    querying the SAME client_order_id (via GET /fapi/v1/order with
    origClientOrderId, confirmed against Binance's official "Query Order"
    docs) before giving up, and using the real order if it turns out to
    have gone through - never guessing in either direction."""

    def _scripted_client(self, post_behavior, query_behavior=None):
        """post_behavior: an exception instance to raise, or a dict to
        return, for the POST /fapi/v1/order call.
        query_behavior: same, for the follow-up GET query (only reached if
        post_behavior is an ambiguous network-style exception)."""
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        calls = {"post": 0, "query": 0}

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            if method == "POST":
                calls["post"] += 1
                if isinstance(post_behavior, Exception):
                    raise post_behavior
                return post_behavior
            elif method == "GET":
                calls["query"] += 1
                if isinstance(query_behavior, Exception):
                    raise query_behavior
                return query_behavior

        client._request = fake_request
        return client, calls

    async def test_market_order_recovers_when_the_ambiguous_order_actually_went_through(self):
        import aiohttp
        client, calls = self._scripted_client(
            post_behavior=aiohttp.ClientError("connection reset"),
            query_behavior={"orderId": 12345, "status": "FILLED", "avgPrice": "100.0"},
        )

        result = await client.market_order("BTCUSDT", "BUY", 0.01)

        self.assertEqual(result["orderId"], 12345, "must return the REAL order found by the query")
        self.assertEqual(calls["post"], 1, "must not place a second order")
        self.assertEqual(calls["query"], 1, "must query by the same client order id exactly once")

    async def test_market_order_reraises_when_the_query_is_also_inconclusive(self):
        import aiohttp
        original_error = aiohttp.ClientError("connection reset")
        client, calls = self._scripted_client(
            post_behavior=original_error,
            query_behavior=None,  # the fake_request function returns None for an unhandled GET below
        )

        # Simulate the query itself also failing (e.g. also a network error) -
        # get_order_by_client_id's own try/except in the caller treats any
        # exception here the same way: inconclusive, don't guess.
        async def failing_get_order_by_client_id(symbol, orig_client_order_id):
            raise RuntimeError("query also failed")

        client.get_order_by_client_id = failing_get_order_by_client_id

        with self.assertRaises(aiohttp.ClientError):
            await client.market_order("BTCUSDT", "BUY", 0.01)

        self.assertEqual(calls["post"], 1)

    async def test_market_order_never_queries_on_a_clean_rejection(self):
        """A clean BinanceAPIError (e.g. insufficient margin) means Binance
        is telling us definitively the order did NOT go through - there is
        no ambiguity to resolve, and querying would be pointless overhead
        on every single rejection."""
        from app.binance_futures import BinanceAPIError
        client, calls = self._scripted_client(
            post_behavior=BinanceAPIError(400, {"code": -2019, "msg": "Margin is insufficient"}),
        )
        query_called = {"n": 0}

        async def tracking_query(symbol, orig_client_order_id):
            query_called["n"] += 1
            return None

        client.get_order_by_client_id = tracking_query

        with self.assertRaises(BinanceAPIError):
            await client.market_order("BTCUSDT", "BUY", 0.01)

        self.assertEqual(query_called["n"], 0, "a clean rejection must never trigger the confirmation query")

    async def test_close_position_market_recovers_when_ambiguous_close_actually_went_through(self):
        import aiohttp
        client, calls = self._scripted_client(
            post_behavior=aiohttp.ClientError("connection reset"),
            query_behavior={"orderId": 999, "status": "FILLED", "avgPrice": "105.0"},
        )

        result = await client.close_position_market("BTCUSDT", "SELL", 0.01)

        self.assertEqual(result["orderId"], 999)
        self.assertEqual(calls["post"], 1, "must not send a second close order")

    async def test_get_order_by_client_id_returns_none_for_order_already_gone(self):
        from app.binance_futures import BinanceAPIError
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            raise BinanceAPIError(400, {"code": -2013, "msg": "Order does not exist"})

        client._request = fake_request
        result = await client.get_order_by_client_id("BTCUSDT", "some_client_id")
        self.assertIsNone(result)

    async def test_get_order_by_client_id_reraises_other_errors(self):
        from app.binance_futures import BinanceAPIError
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            raise BinanceAPIError(500, {"code": -1001, "msg": "Internal error"})

        client._request = fake_request
        with self.assertRaises(BinanceAPIError):
            await client.get_order_by_client_id("BTCUSDT", "some_client_id")

    async def test_get_order_by_client_id_uses_the_documented_parameter_name(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        captured = {}

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            return {"orderId": 1}

        client._request = fake_request
        await client.get_order_by_client_id("BTCUSDT", "abc123")

        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["path"], "/fapi/v1/order")
        self.assertEqual(captured["params"]["origClientOrderId"], "abc123",
                         "must use the documented parameter name, confirmed against Binance's own docs")
        self.assertEqual(captured["params"]["symbol"], "BTCUSDT")


class TestMarketLotSizeAndMaxNotionalFilters(unittest.IsolatedAsyncioTestCase):
    """Item 11 fix: MARKET_LOT_SIZE (a separate filter from LOT_SIZE,
    specifically governing market-order quantities - this bot's entries/
    closes are always MARKET orders) and MAX_NOTIONAL (the upper-bound
    counterpart to MIN_NOTIONAL) were not modeled at all before this fix. A
    quantity could pass the general LOT_SIZE/MIN_NOTIONAL checks and still
    be rejected by Binance for one of these market-specific limits."""

    @staticmethod
    def _exchange_info_response(include_market_lot_size=True, include_max_notional=True):
        filters = [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
        ]
        if include_market_lot_size:
            filters.append({"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01",
                             "minQty": "0.01", "maxQty": "1000.0"})
        if include_max_notional:
            filters.append({"filterType": "MAX_NOTIONAL", "maxNotional": "1000000.0"})
        return {"symbols": [{"symbol": "BTCUSDT", "filters": filters}]}

    async def test_market_lot_size_is_parsed_separately_from_lot_size(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return self._exchange_info_response()

        client._request = fake_request
        info = await client.get_symbol_info("BTCUSDT")

        self.assertEqual(info.qty_step, 0.001, "regular LOT_SIZE step must still be parsed")
        self.assertEqual(info.market_qty_step, 0.01, "MARKET_LOT_SIZE step must be parsed separately")
        self.assertEqual(info.market_min_qty, 0.01)
        self.assertEqual(info.market_max_qty, 1000.0)
        self.assertEqual(info.max_notional, 1000000.0)

    async def test_round_qty_uses_market_lot_size_not_the_general_lot_size(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return self._exchange_info_response()

        client._request = fake_request
        # 0.0157 rounds down to 0.001 steps -> 0.015 under the OLD (LOT_SIZE)
        # behavior, but to 0.01 steps -> 0.01 under the correct, MARKET_LOT_SIZE one.
        result = await client.round_qty("BTCUSDT", 0.0157)
        self.assertAlmostEqual(result, 0.01, places=6,
                               msg="must round to the MARKET_LOT_SIZE step (0.01), not LOT_SIZE's (0.001)")

    async def test_falls_back_to_regular_lot_size_when_market_lot_size_is_absent(self):
        """Not every symbol exposes MARKET_LOT_SIZE - must fall back to the
        general LOT_SIZE values rather than leaving market_qty_step at 0
        (which would disable rounding entirely) or rejecting everything."""
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return self._exchange_info_response(include_market_lot_size=False)

        client._request = fake_request
        info = await client.get_symbol_info("BTCUSDT")

        self.assertEqual(info.market_qty_step, 0.001, "must fall back to the general LOT_SIZE step")
        self.assertEqual(info.market_min_qty, 0.001)

    async def test_check_min_notional_rejects_above_max_notional(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return self._exchange_info_response()

        client._request = fake_request
        # qty * price = 10 * 200000 = 2,000,000 > the 1,000,000 max_notional set above.
        ok, msg = await client.check_min_notional("BTCUSDT", 10.0, 200000.0)
        self.assertFalse(ok)
        self.assertIn("maximum", msg)

    async def test_check_min_notional_still_rejects_below_min_notional(self):
        """Regression guard: adding the max-notional check must not break
        the existing min-notional check."""
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return self._exchange_info_response()

        client._request = fake_request
        ok, msg = await client.check_min_notional("BTCUSDT", 0.001, 1.0)  # notional = 0.001
        self.assertFalse(ok)
        self.assertIn("minimum", msg)

    async def test_check_min_notional_rejects_below_market_min_qty(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return self._exchange_info_response()

        client._request = fake_request
        # qty 0.005 is below MARKET_LOT_SIZE's minQty of 0.01, even though
        # notional (0.005 * 50000 = 250) is comfortably above MIN_NOTIONAL.
        ok, msg = await client.check_min_notional("BTCUSDT", 0.005, 50000.0)
        self.assertFalse(ok)
        self.assertIn("market-order minimum", msg)

    async def test_check_min_notional_rejects_above_market_max_qty(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return self._exchange_info_response()

        client._request = fake_request
        # qty 2000 exceeds MARKET_LOT_SIZE's maxQty of 1000.
        ok, msg = await client.check_min_notional("BTCUSDT", 2000.0, 1.0)
        self.assertFalse(ok)
        self.assertIn("market-order maximum", msg)

    async def test_check_min_notional_passes_within_all_bounds(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return self._exchange_info_response()

        client._request = fake_request
        ok, msg = await client.check_min_notional("BTCUSDT", 1.0, 50000.0)
        self.assertTrue(ok, msg)


class TestAmbiguousAlgoOrderRetry(unittest.IsolatedAsyncioTestCase):
    """Item 7 fix (2026-09-14, per a third-party review): the ambiguous-
    response recovery already applied to market_order/close_position_market
    (item 6 from the earlier batch) was never applied to SL/TP placement
    itself - only caught later, if at all, by the dedup-before-placing
    check in _place_protective_orders on a SUBSEQUENT call. This closes the
    gap at the source: stop_market_order/take_profit_market_order now query
    by the same clientAlgoId before giving up on an ambiguous response."""

    def _scripted_client(self, post_behavior, query_behavior=None):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True  # skip the throttle's own exchangeInfo GET - not what these tests measure
        calls = {"post": 0, "query": 0}

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            if method == "POST":
                calls["post"] += 1
                if isinstance(post_behavior, Exception):
                    raise post_behavior
                return post_behavior
            elif method == "GET":
                calls["query"] += 1
                if isinstance(query_behavior, Exception):
                    raise query_behavior
                return query_behavior

        client._request = fake_request
        return client, calls

    async def test_sl_placement_recovers_when_the_ambiguous_order_actually_went_through(self):
        import aiohttp
        client, calls = self._scripted_client(
            post_behavior=aiohttp.ClientError("connection reset"),
            query_behavior={"algoId": 777, "algoStatus": "NEW", "triggerPrice": "95.0"},
        )

        result = await client.stop_market_order("BTCUSDT", "SELL", 95.0)

        self.assertEqual(result["algoId"], 777, "must return the REAL algo order found by the query")
        self.assertEqual(calls["post"], 1, "must not place a second SL")
        self.assertEqual(calls["query"], 1)

    async def test_tp_placement_recovers_when_the_ambiguous_order_actually_went_through(self):
        import aiohttp
        client, calls = self._scripted_client(
            post_behavior=aiohttp.ClientError("connection reset"),
            query_behavior={"algoId": 888, "algoStatus": "NEW", "triggerPrice": "110.0"},
        )

        result = await client.take_profit_market_order("BTCUSDT", "SELL", 110.0)

        self.assertEqual(result["algoId"], 888)
        self.assertEqual(calls["post"], 1, "must not place a second TP")

    async def test_reraises_when_the_query_is_also_inconclusive(self):
        import aiohttp
        client, calls = self._scripted_client(post_behavior=aiohttp.ClientError("connection reset"))

        async def failing_query(client_algo_id):
            raise RuntimeError("query also failed")

        client.get_algo_order_by_client_id = failing_query

        with self.assertRaises(aiohttp.ClientError):
            await client.stop_market_order("BTCUSDT", "SELL", 95.0)

        self.assertEqual(calls["post"], 1)

    async def test_never_queries_on_a_clean_rejection(self):
        from app.binance_futures import BinanceAPIError
        client, calls = self._scripted_client(
            post_behavior=BinanceAPIError(400, {"code": -2022, "msg": "ReduceOnly Order is rejected"}),
        )
        query_called = {"n": 0}

        async def tracking_query(client_algo_id):
            query_called["n"] += 1
            return None

        client.get_algo_order_by_client_id = tracking_query

        with self.assertRaises(BinanceAPIError):
            await client.stop_market_order("BTCUSDT", "SELL", 95.0)

        self.assertEqual(query_called["n"], 0, "a clean rejection must never trigger the confirmation query")

    async def test_get_algo_order_by_client_id_uses_the_documented_parameter(self):
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        captured = {}

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            return {"algoId": 1}

        client._request = fake_request
        await client.get_algo_order_by_client_id("abc123")

        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["path"], "/fapi/v1/algoOrder")
        self.assertEqual(captured["params"]["clientAlgoId"], "abc123")
        self.assertNotIn("symbol", captured["params"])

    async def test_get_algo_order_by_client_id_returns_none_for_order_already_gone(self):
        from app.binance_futures import BinanceAPIError
        client = BinanceFuturesClient(api_key="x", api_secret="y")

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            raise BinanceAPIError(400, {"code": -2013, "msg": "Order does not exist"})

        client._request = fake_request
        result = await client.get_algo_order_by_client_id("abc123")
        self.assertIsNone(result)


    async def test_skip_throttle_true_bypasses_the_proactive_wait(self):
        """Item 8 fix: initial protective-order placement right after a
        fresh entry must never be delayed by the proactive throttle - a
        naked window there is a real risk the throttle itself would be
        creating, not preventing."""
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True
        client._order_limits = {"10s": 100}
        client._order_counts = {"10s": 99}  # 99% used - would definitely throttle if checked

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return {"algoId": 1}

        client._request = fake_request
        with unittest.mock.patch("app.binance_futures.asyncio.sleep", new=unittest.mock.AsyncMock()) as mock_sleep:
            await client.stop_market_order("BTCUSDT", "SELL", 95.0, skip_throttle=True)
            await client.take_profit_market_order("BTCUSDT", "SELL", 110.0, skip_throttle=True)

        mock_sleep.assert_not_awaited()

    async def test_skip_throttle_false_still_throttles_as_before(self):
        """Regression guard: routine amendments (_amend_sl/_amend_tp, which
        don't pass skip_throttle) must keep the proactive throttle exactly
        as before - old protection stays resting during any wait there, so
        there's no equivalent urgency to bypass it."""
        client = BinanceFuturesClient(api_key="x", api_secret="y")
        client._order_limits_fetched = True
        client._order_limits = {"10s": 100}
        client._order_counts = {"10s": 99}

        async def fake_request(method, path, signed=False, params=None, **kwargs):
            return {"algoId": 1}

        client._request = fake_request
        with unittest.mock.patch("app.binance_futures.asyncio.sleep", new=unittest.mock.AsyncMock()) as mock_sleep:
            await client.stop_market_order("BTCUSDT", "SELL", 95.0)  # skip_throttle defaults to False

        mock_sleep.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
