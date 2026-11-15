"""LIVE order / account HTTP for the arbitrage tab (Step 3).

Everything signed goes through here and EVERY call first passes the shared API guard (app/api_guard.py), so the live
arbitrage orders and the hub's own pairs share one request budget per exchange, and a 429 / 418 pauses everybody.
The engines run in a worker thread; the guard lives on the event loop, so the guard is asked through the loop.
"""
from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from app import api_guard

USER_AGENT = "strategy-hub-arb/1.0"
OKX_RATE_CODES = {"50011", "50061"}
GUARD_WAIT_SECONDS = 600


class ApiError(Exception):
    """Any problem talking to an exchange (network, HTTP, bad data)."""


def _clean(url: str) -> str:
    return url.split("?")[0]            # never show a query string (it holds the signature)


def definitely_rejected(err) -> bool:
    """True if the exchange clearly said NO. Timeouts / network / 5xx mean the order may or may not exist."""
    s = str(err)
    return s.startswith("HTTP 4") or s.startswith("OKX error code") or s.startswith("OKX order rejected")


def guard_for(ex: str, url: str, api_key: str = ""):
    if ex == "okx":
        return api_guard.okx_guard(True, api_key)
    host = urlparse(url).hostname or ""
    if "testnet" in host:
        return api_guard.get_guard("binance-test", 1200)
    if host == "api.binance.com":
        return api_guard.get_guard("binance-spot", 6000)
    return api_guard.get_guard("binance")


class Transport:
    """Guarded sender. `loop` = the hub's event loop (where the guard lives)."""

    def __init__(self, loop):
        self.loop = loop

    def _ask(self, guard, weight, urgent):
        fut = asyncio.run_coroutine_threadsafe(guard.acquire(weight, urgent=urgent), self.loop)
        try:
            fut.result(GUARD_WAIT_SECONDS)
        except Exception as e:
            fut.cancel()
            raise ApiError("request guard did not release in time: %s" % e)

    def _pause(self, guard, seconds, status, detail):
        self.loop.call_soon_threadsafe(guard.trigger_pause, seconds, status, detail)

    def request(self, method, url, *, guard, weight=1, urgent=False, headers=None, data=None, timeout=10, okx=False):
        self._ask(guard, weight, urgent)
        hdrs = {"User-Agent": USER_AGENT}
        hdrs.update(headers or {})
        req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:400]
            if e.code in (418, 429):
                default = api_guard.DEFAULT_418_PAUSE if e.code == 418 else api_guard.DEFAULT_429_PAUSE
                secs = api_guard.retry_after_seconds(e.headers, default)
                if e.code == 418 and e.headers.get("Retry-After") is None:
                    try:
                        secs = max(secs, api_guard.ban_seconds_from_message(json.loads(body)))
                    except ValueError:
                        pass
                self._pause(guard, secs, e.code, "(arbitrage live)")
            raise ApiError("HTTP %s from %s: %s" % (e.code, _clean(url), body))
        except urllib.error.URLError as e:
            raise ApiError("Network error for %s: %s" % (_clean(url), e.reason))
        except (socket.timeout, TimeoutError):
            raise ApiError("Timeout for %s" % _clean(url))
        try:
            out = json.loads(raw)
        except ValueError:
            raise ApiError("Bad JSON from %s" % _clean(url))
        if used is not None:
            guard.note_reported(used)
        if okx and isinstance(out, dict) and out.get("code") in OKX_RATE_CODES:
            self._pause(guard, 5.0, 200, "(arbitrage live, OKX code %s)" % out.get("code"))
        return out
