"""
test_leverage_bracket_signing.py
================================

Regression test for the Binance testnet error
  {'code': -1102, "Mandatory parameter 'timestamp' was not sent ..."}
on GET /fapi/v1/leverageBracket: that endpoint is a USER_DATA (signed) endpoint, but
the request was sent unsigned, so Binance rejected it every time and the bracket
safety check was silently skipped.

These tests do NOT contact Binance. They check that the request is marked signed and
that a signed request really carries a timestamp and signature.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._aiohttp_stub  # noqa: F401,E402
from app.binance_futures import BinanceFuturesClient  # noqa: E402

PUBLIC_PATHS = {"/fapi/v1/exchangeInfo", "/fapi/v1/klines", "/fapi/v1/premiumIndex", "/fapi/v1/time"}


class _Recorder(BinanceFuturesClient):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sent = []
        self.canned = {"symbol": "TESTUSDT", "brackets": [
            {"bracket": 1, "initialLeverage": 20, "notionalFloor": 0, "notionalCap": 50000}]}

    async def _request_once(self, method, path, signed, params):
        # record what the real _request wrapper decided, then build the params exactly as
        # the real code would for a signed call
        sent_params = self._sign(dict(params or {})) if signed else dict(params or {})
        self.sent.append((method, path, signed, sent_params))
        return self.canned


class TestLeverageBracketIsSigned(unittest.IsolatedAsyncioTestCase):
    async def test_the_bracket_request_is_signed_and_carries_timestamp_and_signature(self):
        c = _Recorder(api_key="x", api_secret="y")
        brackets = await c.get_leverage_brackets("TESTUSDT")
        method, path, signed, params = c.sent[0]
        self.assertEqual((method, path), ("GET", "/fapi/v1/leverageBracket"))
        self.assertTrue(signed, "leverageBracket is a signed USER_DATA endpoint")
        self.assertIn("timestamp", params)
        self.assertIn("signature", params)
        self.assertEqual(params["symbol"], "TESTUSDT")
        self.assertEqual(brackets[0]["max_leverage"], 20, "response parsing is unchanged")

    async def test_both_response_shapes_still_parse(self):
        c = _Recorder(api_key="x", api_secret="y")
        c.canned = [c.canned]          # list shape
        self.assertEqual(len(await c.get_leverage_brackets("TESTUSDT")), 1)

    async def test_no_other_account_endpoint_is_sent_unsigned(self):
        """Walks every request this client makes with a canned response and flags any
        non-public path that goes out unsigned (the exact class of bug above)."""
        import re
        src = open(os.path.join(os.path.dirname(__file__), "..", "app", "binance_futures.py")).read()
        offenders = []
        for m in re.finditer(r'_request\(\s*"(GET|POST|PUT|DELETE)",\s*"([^"]+)"([^)]*)\)', src, re.S):
            path, args = m.group(2), m.group(3)
            if path in PUBLIC_PATHS or path == "/fapi/v1/listenKey":
                continue
            if "signed=True" not in args:
                offenders.append(path)
        self.assertEqual(offenders, [], f"account endpoints sent unsigned: {offenders}")


if __name__ == "__main__":
    unittest.main()
