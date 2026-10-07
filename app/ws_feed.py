"""
ws_feed.py
==========

A lightweight watchdog, not a strategy input: subscribes to Binance's
markPrice@1s stream for one symbol purely to detect when the live price
feed has gone stale (network blip, exchange-side issue, local connectivity
problem). If no message arrives within `stale_after_seconds`, the feed
reports itself stale.

This is used ONLY to pause new entries when the bot cannot be confident
prices are current - it never substitutes for the REST klines the strategy
actually trades on, and it never blocks managing/closing an existing
position (protecting an open position should never be paused).

2026-09-14 fix: mainnet base URL updated to Binance's current documented
"/market" path. Found incidentally while researching the real-time
user-data-stream feature (0r) - Binance's own "Important WebSocket Change
Notice" (fetched from developers.binance.com, last modified 2026-09-11,
days before this fix) confirms the legacy `wss://fstream.binance.com/ws`
base is being phased out in favor of three dedicated bases (`/public`,
`/market`, `/private`); markPrice specifically belongs under `/market`.
The notice states legacy URLs still work for now but will eventually stop
pushing data for anything outside `/public`. Testnet's equivalent split
(if any) was not found in the documentation available while making this
fix, so testnet keeps its existing, long-stable URL pattern unchanged -
same reasoning already applied to the user-data-stream's testnet URL (0r).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

log = logging.getLogger("ws_feed")

MAINNET_WS = "wss://fstream.binance.com/market/ws"
TESTNET_WS = "wss://stream.binancefuture.com/ws"


class MarkPriceFeed:
    def __init__(self, symbol: str, stale_after_seconds: float, testnet: bool = False):
        self.symbol = symbol.lower()
        self.testnet = testnet
        self.stale_after_seconds = stale_after_seconds
        self.last_message_at: float = 0.0
        self.last_mark_price: float | None = None
        self._task: asyncio.Task | None = None
        self._stop = False

    @property
    def base_ws(self) -> str:
        return TESTNET_WS if self.testnet else MAINNET_WS

    def start(self):
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()

    def is_stale(self) -> bool:
        if self.last_message_at == 0:
            return True  # never received a message yet - treat as stale (fail-safe)
        return (time.time() - self.last_message_at) > self.stale_after_seconds

    async def _run(self):
        url = f"{self.base_ws}/{self.symbol}@markPrice@1s"
        while not self._stop:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=15) as ws:
                        async for msg in ws:
                            if self._stop:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                self.last_mark_price = float(data.get("p", 0)) or self.last_mark_price
                                self.last_message_at = time.time()
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
            except Exception as e:
                log.warning("Mark-price feed for %s disconnected (%s) - reconnecting in 5s.",
                            self.symbol.upper(), e)
            if not self._stop:
                await asyncio.sleep(5)


class OKXMarkPriceFeed:
    """OKX's real equivalent of MarkPriceFeed above - added 2026-09-15,
    owner-approved (was previously deferred, see NullMarkFeed's own
    docstring on why it existed as a stand-in). Subscribes to OKX's public
    "mark-price" channel (confirmed against current OKX v5 docs - public
    channel, no auth/signing needed, unlike the private user-data-stream
    equivalent). Push data uses "markPx" as the field name, matching OKX's
    consistent naming for mark price across its whole v5 API (the same
    field name already used in this project's REST get_mark_price call).

    Same interface as MarkPriceFeed (start/stop/is_stale) so instance.py
    doesn't need any conditional logic beyond which one to construct -
    OKX pairs can now use this instead of NullMarkFeed."""

    MAINNET_WS = "wss://ws.okx.com:8443/ws/v5/public"
    DEMO_WS = "wss://wspap.okx.com:8443/ws/v5/public"  # OKX demo trading's own public WS base

    def __init__(self, symbol: str, stale_after_seconds: float, demo: bool = False):
        self.symbol = symbol  # OKX instId format, e.g. "BTC-USDT-SWAP"
        self.demo = demo
        self.stale_after_seconds = stale_after_seconds
        self.last_message_at: float = 0.0
        self.last_mark_price: float | None = None
        self._task: asyncio.Task | None = None
        self._stop = False

    @property
    def base_ws(self) -> str:
        return self.DEMO_WS if self.demo else self.MAINNET_WS

    def start(self):
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()

    def is_stale(self) -> bool:
        if self.last_message_at == 0:
            return True  # never received a message yet - treat as stale (fail-safe)
        return (time.time() - self.last_message_at) > self.stale_after_seconds

    @staticmethod
    def _extract_mark_price(raw_message: dict) -> float | None:
        """Pulled out as its own function specifically so this parsing
        logic - the part most likely to have a real bug, like OKX's
        subscribe-ack-vs-push-data distinction - is unit-testable without
        needing to mock a live websocket connection at all."""
        rows = raw_message.get("data")
        if not rows:
            return None  # e.g. the initial {"event":"subscribe",...} ack frame - no data yet
        mark_px = rows[0].get("markPx")
        return float(mark_px) if mark_px else None

    async def _run(self):
        sub_msg = json.dumps({
            "op": "subscribe",
            "args": [{"channel": "mark-price", "instId": self.symbol}],
        })
        while not self._stop:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.base_ws, heartbeat=15) as ws:
                        await ws.send_str(sub_msg)
                        async for msg in ws:
                            if self._stop:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                price = self._extract_mark_price(data)
                                if price is not None:
                                    self.last_mark_price = price
                                    self.last_message_at = time.time()
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
            except Exception as e:
                log.warning("OKX mark-price feed for %s disconnected (%s) - reconnecting in 5s.",
                            self.symbol, e)
            if not self._stop:
                await asyncio.sleep(5)


class NullMarkFeed:
    """No-op stand-in satisfying the same interface (start/stop/is_stale)
    as MarkPriceFeed/OKXMarkPriceFeed - always reports "not stale". Used
    only where no real feed is wired in (e.g. a bare instance built
    without a platform-specific feed) - since 2026-09-15, OKX pairs use
    OKXMarkPriceFeed above instead of this, now that a real OKX feed
    exists (see instance.py's mark_feed construction)."""

    def __init__(self, symbol: str, stale_after_seconds: float):
        self.symbol = symbol
        self.stale_after_seconds = stale_after_seconds

    def start(self):
        pass

    async def stop(self):
        pass

    def is_stale(self) -> bool:
        return False
