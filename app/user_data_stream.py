"""
user_data_stream.py
====================

Real-time push notifications from Binance for one account's own account/
order/algo-order events - ONE connection shared across every pair on that
account (a listenKey is account-wide, not per-symbol, so pairs register/
unregister interest in specific symbols on a shared stream rather than each
opening their own).

This is the PRIMARY, fast path for noticing SL/TP fills and entry/close
confirmations. REST polling (instance.py's normal ~15s tick loop) remains
fully in place as a safety net in case this connection drops or misses an
event - this ADDS a faster path in front of the existing safeguards, it
does not replace or remove any of them.

Confirmed directly against developers.binance.com's current documentation
(fetched 2026-09-14), not guessed:
  - Connect (mainnet): wss://fstream.binance.com/private/ws/<listenKey>
  - listenKey lifecycle: POST /fapi/v1/listenKey creates/refreshes (60 min
    validity), PUT /fapi/v1/listenKey extends another 60 min (a -1125 "does
    not exist" error means it already expired - must POST a new one
    instead), DELETE /fapi/v1/listenKey closes the stream. These three are
    UNSIGNED (API-KEY header only, no HMAC) - a long-stable Binance
    convention, unlike the endpoints below which were freshly re-checked.
  - Binance force-disconnects the WebSocket connection itself at the 24h
    mark regardless of keepalives - this class reconnects proactively
    before that, not just reactively after a drop.
  - Two event types are used here, and they are NOT interchangeable:
      ALGO_UPDATE       - this bot's SL/TP (STOP_MARKET/TAKE_PROFIT_MARKET
                          are algo/conditional orders since Binance's
                          2025-12-09 migration - see binance_futures.py's
                          algo-order section). Payload fields use SHORTHAND
                          keys that are genuinely different from the REST
                          Algo Order API's own field names: `aid` (not
                          algoId), `caid` (not clientAlgoId), `X` (not
                          algoStatus), `ai`/`ap` (not actualOrderId/
                          actualPrice) - confirmed from Binance's own raw
                          JSON example, not assumed to mirror the REST shape.
      ORDER_TRADE_UPDATE - regular orders (this bot's MARKET entries/
                          closes). Classic, long-documented shorthand keys:
                          `i` (orderId), `X` (order status), `ap` (avg
                          price), `n`/`N` (commission/asset).

NOT independently verified against a real Binance connection - there is no
network access in the environment this was built in, so this was built
strictly from Binance's current documented spec (endpoint paths, event
names, and field names were all freshly fetched, not recalled from
training). Recommend confirming this specific piece end-to-end on testnet
(place a real SL, let it fill, confirm the ALGO_UPDATE arrives and is
parsed correctly) before relying on it in place of the REST polling it
sits in front of - see the checks listed in README.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("user_data_stream")

MAINNET_WS = "wss://fstream.binance.com/private/ws"
# NOTE: Binance's documented base-URL split (see binance_futures.py's algo
# section for the sibling REST migration) is confirmed for MAINNET only.
# Testnet's equivalent private-stream URL was not found in the
# documentation available while building this - using the long-established,
# stable testnet pattern instead. VERIFY this specific URL on testnet before
# relying on it; if wrong, this will simply fail to connect (visible in
# logs and via the REST-polling safety net still catching everything), not
# silently misbehave.
TESTNET_WS = "wss://stream.binancefuture.com/ws"

KEEPALIVE_INTERVAL_SECONDS = 30 * 60      # Binance validity is 60 min - refresh well before that
RECONNECT_BEFORE_24H_SECONDS = 23 * 60 * 60  # Binance force-disconnects at 24h regardless of keepalives

AlgoUpdateHandler = Callable[[dict], Awaitable[None]]
OrderUpdateHandler = Callable[[dict], Awaitable[None]]


RECONNECT_ALERT_THRESHOLD = 3  # consecutive reconnect attempts before escalating past just a log line


class UserDataStream:
    """One instance per ACCOUNT (not per pair) - a listenKey and its stream
    cover every symbol on that account. Pairs register/unregister interest
    in specific symbols; incoming events are dispatched only to the
    registered handler for that event's own symbol, so one pair never sees
    another pair's fills."""

    def __init__(self, client, testnet: bool = False):
        self.client = client   # a BinanceFuturesClient - used for listenKey REST calls
        self.testnet = testnet
        self._listen_key: str | None = None
        self._task: asyncio.Task | None = None
        self._stop = False
        self._connected_at: float = 0.0
        self._warned_testnet_url = False
        self._handlers: dict[str, dict[str, object]] = {}   # SYMBOL -> {"algo": cb, "order": cb, "unhealthy": cb}
        # 2026-09-15 fix (flagged across two review rounds): a reconnect was
        # only ever logged, never escalated - a genuinely sustained outage
        # (not a single blip) could go unnoticed unless someone happened to
        # be watching logs. Tracks consecutive reconnect attempts since the
        # last confirmed-healthy connection; once RECONNECT_ALERT_THRESHOLD
        # is reached, every registered symbol's "unhealthy" callback (if
        # any) fires ONCE for this streak - not on every retry after that,
        # which would just spam the same alert every 5 seconds during a
        # real outage.
        self._consecutive_failures: int = 0
        self._alerted_this_streak: bool = False

    @property
    def base_ws(self) -> str:
        return TESTNET_WS if self.testnet else MAINNET_WS

    def register(self, symbol: str, on_algo_update: AlgoUpdateHandler | None = None,
                 on_order_update: OrderUpdateHandler | None = None,
                 on_unhealthy: object | None = None):
        entry = self._handlers.setdefault(symbol.upper(), {})
        if on_algo_update:
            entry["algo"] = on_algo_update
        if on_order_update:
            entry["order"] = on_order_update
        if on_unhealthy:
            entry["unhealthy"] = on_unhealthy

    def unregister(self, symbol: str):
        self._handlers.pop(symbol.upper(), None)

    def has_subscribers(self) -> bool:
        return bool(self._handlers)

    async def _notify_unhealthy(self, message: str):
        """Fires the 'unhealthy' callback for every currently-registered
        symbol on this account's stream - every pair sharing this one
        stream gets told, since the outage affects all of them equally.
        A callback raising is logged and does not stop the others from
        being notified."""
        for symbol, entry in self._handlers.items():
            callback = entry.get("unhealthy")
            if callback:
                try:
                    await callback(message)
                except Exception as e:
                    log.warning("Unhealthy-stream callback for %s raised (%s) - continuing "
                              "to notify any other registered symbols regardless.", symbol, e)

    def start(self):
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()
        if self._listen_key:
            try:
                await self.client.close_listen_key(self._listen_key)
            except Exception:
                pass
            self._listen_key = None
        # This stream's own dedicated client (see manager.py's
        # get_or_create_user_data_stream) - never a client borrowed from
        # any pair, so closing it here can't affect any other pair's
        # trading operations.
        try:
            await self.client.close()
        except Exception:
            pass

    async def _run(self):
        if self.testnet and not self._warned_testnet_url:
            # Item 12: this URL is a best-effort assumption, not confirmed
            # from Binance's documentation (unlike the mainnet URL, which
            # was fetched and verified directly - see the module docstring).
            # This can't be "fixed" from here - it genuinely needs a real
            # testnet connection to confirm one way or the other. Logged
            # loudly, once, so it's impossible to miss in the logs if you're
            # running on testnet - if this URL is wrong, connection will
            # simply fail to establish (visible right here), and the bot
            # falls back to REST polling exactly as it would for any other
            # disconnection.
            log.warning("Connecting to the TESTNET user-data stream at %s - this specific URL "
                        "is an assumed fallback, NOT confirmed against Binance's documentation "
                        "(unlike the mainnet URL). If it's wrong, connection attempts below will "
                        "simply keep failing - REST polling still covers you regardless, but "
                        "confirm this on testnet before relying on the real-time fast path.",
                        self.base_ws)
            self._warned_testnet_url = True
        while not self._stop:
            keepalive_task = None
            try:
                self._listen_key = await self.client.create_listen_key()
                self._connected_at = time.time()
                url = f"{self.base_ws}/{self._listen_key}"
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=15) as ws:
                        # Connection genuinely established - this streak of
                        # failures (if any) is over.
                        self._consecutive_failures = 0
                        self._alerted_this_streak = False
                        keepalive_task = asyncio.create_task(self._keepalive_loop(ws))
                        async for msg in ws:
                            if self._stop:
                                break
                            if time.time() - self._connected_at > RECONNECT_BEFORE_24H_SECONDS:
                                log.info("Proactively reconnecting user data stream before "
                                         "Binance's 24h forced disconnect.")
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._dispatch(json.loads(msg.data))
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
            except Exception as e:
                log.warning("User data stream disconnected (%s) - reconnecting in 5s.", e)
                self._consecutive_failures += 1
                if self._consecutive_failures >= RECONNECT_ALERT_THRESHOLD and not self._alerted_this_streak:
                    self._alerted_this_streak = True
                    await self._notify_unhealthy(
                        f"Real-time update stream has failed to reconnect "
                        f"{self._consecutive_failures} times in a row ({e}). REST polling is "
                        f"still covering entries/exits/fills as normal - this only affects how "
                        f"fast fills are noticed, not whether they're noticed at all."
                    )
            finally:
                if keepalive_task:
                    keepalive_task.cancel()
            if not self._stop:
                await asyncio.sleep(5)

    async def _keepalive_loop(self, ws):
        """2026-09-14 fix (item 4, per a third-party review): a failed
        keepalive previously only logged a warning and kept the existing
        (soon-to-expire) connection running - the listenKey could then
        expire with no explicit trigger to reconnect, silently losing the
        real-time fast path until Binance's own listenKeyExpired event
        happened to arrive (which itself depends on the connection still
        being alive to deliver it - not guaranteed once the key is dead).
        Now force-closes the WebSocket connection on any keepalive failure,
        which makes the main _run() loop's `async for msg in ws` exit
        naturally and reconnect with a FRESH listenKey - covering both a
        generic keepalive failure and the specific -1125 "listenKey does
        not exist" case the same way, since either way the right response
        is the same: stop trusting this connection, get a new key."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
            try:
                if self._listen_key:
                    await self.client.keepalive_listen_key(self._listen_key)
            except Exception as e:
                log.warning("listenKey keepalive failed (%s) - forcing a reconnect with a fresh "
                            "key rather than risking a silently expired connection.", e)
                try:
                    await ws.close()
                except Exception:
                    pass
                return  # this task's job is done either way - _run() will notice ws is closed

    async def _dispatch(self, data: dict):
        event_type = data.get("e")
        if event_type == "listenKeyExpired":
            # Forces the outer try/except in _run() to reconnect AND recreate
            # a fresh listenKey (create_listen_key() runs again at the top
            # of the next loop iteration) - never silently keep using an
            # expired key.
            raise ConnectionError("listenKey expired - reconnecting with a fresh key")
        if event_type == "ALGO_UPDATE":
            o = data.get("o", {})
            handler = self._handlers.get(o.get("s", "").upper(), {}).get("algo")
            if handler:
                await handler(o)
        elif event_type == "ORDER_TRADE_UPDATE":
            o = data.get("o", {})
            handler = self._handlers.get(o.get("s", "").upper(), {}).get("order")
            if handler:
                await handler(o)
