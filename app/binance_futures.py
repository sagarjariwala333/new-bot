"""
binance_futures.py
==================

Minimal, dependency-light async REST client for Binance USDⓈ-M Futures
(fapi). Handles request signing, mainnet/testnet base URLs, symbol precision
rounding, and the handful of endpoints the bot needs:

  - klines
  - account balance / equity
  - exchange info (tick size / step size, cached per symbol)
  - leverage / margin type
  - place order: MARKET (entries/closes) via /fapi/v1/order; STOP_MARKET /
    TAKE_PROFIT_MARKET (this bot's SL/TP) via the Algo Order API
    (/fapi/v1/algoOrder) - these are TWO SEPARATE Binance order systems
    since the 2025-12-09 conditional-order migration, not one system with a
    fallback. See the "algo (conditional) orders" section below for the
    full explanation and the official docs this was verified against.
  - position risk (open position + mark price)

Every account (up to 12) gets its own client instance with its own
api_key/api_secret, so accounts are fully isolated from one another.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from app.exchange_adapter import BaseExchangeAdapter, ExchangeAPIError

MAINNET_BASE = "https://fapi.binance.com"
TESTNET_BASE = "https://testnet.binancefuture.com"

# Bounded timeouts so a stalled connection can never hold a bot loop hostage
# indefinitely. `total` bounds the whole request; `connect` bounds just the
# TCP/TLS handshake. If a timeout fires on an ORDER request specifically, the
# order may still have been accepted exchange-side even though the response
# never arrived - every call site that places/cancels an order in this project
# reconciles against a fresh position/order query afterward rather than
# assuming the timeout meant "nothing happened".
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)

# Every order this bot places is tagged with this prefix so startup
# reconciliation can tell "an order THIS instance placed" apart from any
# other order that happens to be sitting on the same symbol (manual trade,
# another tool, etc.) - cleanup/cancel logic only ever touches its own.
CLIENT_ORDER_ID_PREFIX = "hullbot_"
# Extra prefixes still recognised as this bot's own (none in this package).
LEGACY_CLIENT_ORDER_ID_PREFIXES = ()
OWN_ORDER_PREFIXES = (CLIENT_ORDER_ID_PREFIX,) + LEGACY_CLIENT_ORDER_ID_PREFIXES

log = logging.getLogger("binance")

# --------------------------------------------------------------------------
# Error-code classification for the retry wrapper below. Codes per Binance's
# published USDⓈ-M Futures error list.
# --------------------------------------------------------------------------
CLOCK_DRIFT_CODES = {-1021}                       # timestamp outside recvWindow
RATE_LIMIT_CODES = {-1003, -1015}                 # too many requests / order rate limit
ORDER_ALREADY_GONE_CODES = {-2011, -2013}         # cancel/query on filled-or-cancelled order
NON_RETRYABLE_CODES = {                           # needs a human, not a retry
    -2010, -2018, -2019, -2021, -4003, -4164, -1013,
}


class BinanceAPIError(ExchangeAPIError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        self.code = payload.get("code") if isinstance(payload, dict) else None
        super().__init__(f"Binance API error {status}: {payload}")


@dataclass
class SymbolInfo:
    symbol: str
    price_tick: float
    qty_step: float
    min_qty: float
    min_notional: float
    # 2026-09-14 fix (item 11, per a third-party review): MARKET_LOT_SIZE is
    # a SEPARATE filter from LOT_SIZE, specifically governing market-order
    # quantities (this bot's entries/closes are always MARKET orders) - it
    # can have a different step/min/max than the general LOT_SIZE, which
    # also covers order types this bot never uses (LIMIT, etc.). A quantity
    # could pass the general LOT_SIZE check and still be rejected by
    # Binance for this one specifically. Falls back to the regular LOT_SIZE
    # values if a symbol doesn't expose MARKET_LOT_SIZE (not all do).
    market_qty_step: float = 0.0
    market_min_qty: float = 0.0
    market_max_qty: float = float("inf")
    # MAX_NOTIONAL is the upper-bound counterpart to MIN_NOTIONAL - not
    # modeled at all before this fix.
    max_notional: float = float("inf")


class _SharedAccountOrderRateTracker:
    """2026-09-16 fix, owner-confirmed (order-rate-limit accounting wasn't
    truly account-wide - flagged by a third-party review). Binance's own
    docs state plainly: "the order rate limit is counted against each
    account" - not per symbol, not per client. Previously each pair's own
    BinanceFuturesClient kept a PRIVATE copy of the order-count headers
    and the fetched limits, so pair A could see itself well under budget
    while pairs B and C on the SAME account had already used up most of
    the account's real, shared quota - exactly backwards from how Binance
    actually enforces this.

    This object holds nothing pair-specific - just the raw counters and
    limits Binance's own responses/exchangeInfo already report, same as
    before. The only change is WHO holds it: one of these per Binance
    account_id, shared by every pair's client on that account, instead of
    one private copy per pair. The actual numbers still come from
    Binance's live response headers and the account's real exchangeInfo
    rate limits - nothing here is a hardcoded guess, then or now, so this
    stays correct even if Binance changes the specific limit values later.

    DEPLOYMENT NOTE (owner-confirmed, 2026-09-16): this project runs as a
    single Python process on Railway.com - one process, one Python
    interpreter, for every account and pair this bot manages. That's
    exactly why a plain in-process dict (_registry below) is sufficient
    here: every pair's client lives in the same process and can share one
    of these objects directly. If this project is ever split across
    multiple processes or machines, this in-process sharing would stop
    working and would need an external shared store (e.g. Redis) instead -
    flagging this explicitly now so a future change in deployment
    topology doesn't silently reintroduce the exact per-pair-isolation bug
    this fix exists to close."""

    def __init__(self):
        self.order_counts: dict[str, int] = {}
        self.order_limits: dict[str, int] = {}
        self.order_limits_fetched: bool = False
        # MUST-HAVE FIX (2026-09-28): serialises the one-time limits fetch
        # across every pair sharing this account - see
        # BinanceFuturesClient._ensure_order_rate_limits.
        self.limits_lock: asyncio.Lock = asyncio.Lock()


_registry: dict[str, _SharedAccountOrderRateTracker] = {}


def _get_shared_rate_tracker(account_id: str) -> _SharedAccountOrderRateTracker:
    tracker = _registry.get(account_id)
    if tracker is None:
        tracker = _SharedAccountOrderRateTracker()
        _registry[account_id] = tracker
    return tracker


def _reset_all_shared_rate_trackers_FOR_TESTS_ONLY():
    """Exactly what the name says - the registry is a module-level dict
    that persists for the lifetime of the process (correct/needed in
    production, where it's meant to outlive any one client), which means
    tests that don't pass a unique account_id per client would otherwise
    leak state into each other. Call this in test setUp, not from any
    production code path."""
    _registry.clear()


@dataclass
class BinanceFuturesClient(BaseExchangeAdapter):
    api_key: str
    api_secret: str
    testnet: bool = False
    # 2026-09-16 fix - see _SharedAccountOrderRateTracker's own docstring
    # above. Defaults to a unique-per-instance value (never a fixed blank
    # string) specifically so that any caller which forgets to pass the
    # real account_id fails SAFE - falling back to its own isolated
    # tracker, exactly like the old pre-fix behavior - rather than
    # accidentally sharing rate-limit state with some unrelated client
    # that also left it blank. Every real production construction site in
    # this project has been updated to pass the real account_id
    # explicitly; this default only matters for a caller (or a test) that
    # doesn't care about sharing at all.
    account_id: str = field(default_factory=lambda: f"unshared-{uuid.uuid4().hex}")
    _session: Optional[aiohttp.ClientSession] = field(default=None, init=False, repr=False)
    _symbol_cache: dict[str, SymbolInfo] = field(default_factory=dict, init=False, repr=False)
    _clock_offset_ms: int = field(default=0, init=False, repr=False)
    # Order-count rate limit tracking (X-MBX-ORDER-COUNT-* headers) - a
    # SEPARATE limit from request weight: Binance counts every order
    # placement/cancellation (confirmed as of the 2026-06-20 changelog: algo
    # orders count here too, same as regular orders) against a 10-second and
    # a 1-minute window, independent of the IP weight budget this project
    # was already sized against (see the comments in this file).
    #
    # 2026-09-16 fix: this used to be a private dict living directly on
    # each client (each pair's own copy) - moved to a tracker SHARED across
    # every pair on the same Binance account, since Binance itself counts
    # this per account, not per pair (see _SharedAccountOrderRateTracker's
    # own docstring for the full reasoning and the Railway.com deployment
    # note). _rate_tracker is resolved once in __post_init__ from
    # account_id, then every method below reads/writes through it instead
    # of a private field - the property names below are kept the same
    # (_order_counts/_order_limits/_order_limits_fetched) so the rest of
    # this class's logic didn't need to change at all, only where the data
    # actually lives.
    _rate_tracker: _SharedAccountOrderRateTracker = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._rate_tracker = _get_shared_rate_tracker(self.account_id)

    @property
    def _order_counts(self) -> dict[str, int]:
        return self._rate_tracker.order_counts

    @_order_counts.setter
    def _order_counts(self, value: dict[str, int]):
        self._rate_tracker.order_counts = value

    @property
    def _order_limits(self) -> dict[str, int]:
        return self._rate_tracker.order_limits

    @_order_limits.setter
    def _order_limits(self, value: dict[str, int]):
        self._rate_tracker.order_limits = value

    @property
    def _order_limits_fetched(self) -> bool:
        return self._rate_tracker.order_limits_fetched

    @_order_limits_fetched.setter
    def _order_limits_fetched(self, value: bool):
        self._rate_tracker.order_limits_fetched = value

    @property
    def base_url(self) -> str:
        return TESTNET_BASE if self.testnet else MAINNET_BASE

    @property
    def client_order_id_prefix(self) -> tuple[str, ...]:
        """Every tag that marks an order as THIS bot's own (current first,
        then legacy). Used only with str.startswith(), which accepts a
        tuple. New orders are always created with CLIENT_ORDER_ID_PREFIX."""
        return OWN_ORDER_PREFIXES

    @property
    def supports_realtime_stream(self) -> bool:
        return True

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"X-MBX-APIKEY": self.api_key},
                                                    timeout=DEFAULT_TIMEOUT)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, params: dict) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        params["timestamp"] = int(time.time() * 1000) + self._clock_offset_ms
        params["recvWindow"] = 10000
        query = urlencode(params, doseq=True)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    def _new_client_order_id(self) -> str:
        return f"{CLIENT_ORDER_ID_PREFIX}{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}"

    async def _request_once(self, method: str, path: str, signed: bool, params: dict | None) -> Any:
        session = await self._get_session()
        params = dict(params or {})
        if signed:
            params = self._sign(params)
        url = f"{self.base_url}{path}"
        async with session.request(method, url, params=params) as resp:
            self._capture_order_count_headers(resp.headers)
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise BinanceAPIError(resp.status, data)
            return data

    def _capture_order_count_headers(self, headers) -> None:
        """Passively records X-MBX-ORDER-COUNT-<intervalNum><intervalLetter>
        from every response that includes it (confirmed header format from
        Binance's current docs) - e.g. X-MBX-ORDER-COUNT-10S,
        X-MBX-ORDER-COUNT-1M. Rejected/unsuccessful orders aren't guaranteed
        to include these, so absence here is expected sometimes, not an error."""
        for key, value in headers.items():
            if key.upper().startswith("X-MBX-ORDER-COUNT-"):
                interval_key = key.upper().removeprefix("X-MBX-ORDER-COUNT-").lower()
                try:
                    self._order_counts[interval_key] = int(value)
                except (TypeError, ValueError):
                    pass

    async def _ensure_order_rate_limits(self) -> None:
        """Fetches the account's actual ORDER rate limits from exchangeInfo's
        rateLimits array (once, cached) rather than hardcoding a guessed
        number - the exact limit can vary by account/VIP tier."""
        # MUST-HAVE FIX (2026-09-28): the "fetched" flag used to be set
        # BEFORE the fetch finished, so a sibling pair on the same account
        # calling this during that window saw fetched=True with NO limits
        # loaded and skipped the rate-limit check entirely. Now the first
        # caller holds the shared lock while it loads; everyone else waits
        # for the real limits instead of skipping. A failed fetch still
        # marks it done (unchanged intent: never retry on every order).
        if self._order_limits_fetched:
            return
        async with self._rate_tracker.limits_lock:
            if self._order_limits_fetched:
                return
            try:
                data = await self._request("GET", "/fapi/v1/exchangeInfo")
                interval_letter = {"SECOND": "s", "MINUTE": "m", "HOUR": "h", "DAY": "d"}
                for rl in data.get("rateLimits", []):
                    if rl.get("rateLimitType") != "ORDERS":
                        continue
                    letter = interval_letter.get(rl.get("interval"), "")
                    key = f"{rl.get('intervalNum')}{letter}"
                    self._order_limits[key] = int(rl["limit"])
            except Exception as e:
                log.warning("Could not fetch order rate limits from exchangeInfo: %s", e)
            finally:
                self._order_limits_fetched = True

    async def _throttle_if_near_order_limit(self) -> None:
        """Called before every order-placing/cancelling request (never
        before read-only GETs, which don't count against this limit).
        Proactively pauses if usage is already close to either window's
        limit, rather than only reacting after Binance actually rejects a
        request with -1015. This is the fix for a real gap: the bot
        previously only found out about this limit reactively, from a
        rejection, instead of seeing it coming from the headers Binance
        already sends on every order response.

        2026-09-14 refinement (per two independent third-party reviews):
        the wait used to be a flat 2 seconds regardless of which window was
        hot, with no guarantee that's actually enough time for a 1-minute
        window to clear. IMPORTANT LIMITATION that shapes this fix: the
        order-count headers this reads are only ever updated from a real
        response - there is no way to get a truly fresh count without
        sending a request, so re-checking the same (unavoidably stale)
        counters in a loop would not actually prove the window has reset.
        Given that, the more defensible fix is to size the wait to the
        window's own actual duration (parsed from its interval, e.g. ~10s
        for a "10s" window, ~60s for "1m") rather than a guessed constant -
        this is what actually gives the window a real chance to clear,
        capped at a sane maximum so a single throttle pause can never stall
        the bot indefinitely."""
        await self._ensure_order_rate_limits()
        MAX_WAIT_SECONDS = 65.0  # generous enough for the "1m" window, but never open-ended
        for interval_key, limit in self._order_limits.items():
            count = self._order_counts.get(interval_key)
            if count is None or limit <= 0:
                continue
            usage = count / limit
            if usage >= 0.9:
                wait_seconds = min(self._parse_interval_seconds(interval_key), MAX_WAIT_SECONDS)
                log.warning("Order rate limit at %.0f%% for the %s window (%d/%d) - "
                            "pausing %.1fs (roughly the window's own duration) before this order.",
                            usage * 100, interval_key, count, limit, wait_seconds)
                await asyncio.sleep(wait_seconds)

    @staticmethod
    def _parse_interval_seconds(interval_key: str) -> float:
        """Parses an interval key like "10s"/"1m"/"1h"/"1d" (as built by
        _ensure_order_rate_limits from exchangeInfo's rateLimits) into a
        seconds value. Falls back to a conservative 10s if the format is
        ever unexpected, rather than raising or waiting 0s."""
        try:
            number = int(interval_key[:-1])
            unit = interval_key[-1]
        except (ValueError, IndexError):
            return 10.0
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
        if multiplier is None:
            return 10.0
        return float(number * multiplier)

    async def _request(self, method: str, path: str, signed: bool = False, params: dict | None = None,
                        retries: int = 3, delay: float = 1.5) -> Any:
        """
        Retry wrapper with error-aware backoff:
          - clock drift (-1021): resync the timestamp offset and retry immediately
          - rate limited: exponential backoff, longer than a normal retry
          - "order already gone" (-2011/-2013): raise immediately, caller decides
            (this usually just means it was already filled/cancelled - not a fault)
          - other non-retryable codes (bad qty, insufficient margin, immediate-trigger
            stop price, etc.): raise immediately, needs a human to look at it
          - anything else (network blips, 5xx): fixed-delay retry
        """
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return await self._request_once(method, path, signed, params)
            except BinanceAPIError as e:
                last_exc = e
                if e.code in ORDER_ALREADY_GONE_CODES or e.code in NON_RETRYABLE_CODES:
                    raise
                if e.code in CLOCK_DRIFT_CODES:
                    log.warning("Clock drift (-1021) on %s - resyncing and retrying.", path)
                    await self.sync_clock()
                    await asyncio.sleep(0.5)
                    continue
                if e.code in RATE_LIMIT_CODES or e.status in (418, 429):
                    backoff = delay * (2 ** attempt)
                    log.warning("Rate-limited on %s (code=%s) - backing off %.1fs.", path, e.code, backoff)
                    await asyncio.sleep(backoff)
                    continue
                log.warning("%s failed (attempt %d/%d, code=%s): %s", path, attempt, retries, e.code, e)
                await asyncio.sleep(delay)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                log.warning("%s network error (attempt %d/%d): %s", path, attempt, retries, e)
                await asyncio.sleep(delay)
        raise last_exc

    async def sync_clock(self):
        """Resync against Binance's server time. Local clock drift is a common,
        documented real-world cause of every signed request suddenly failing with
        -1021 - this recovers without needing a restart."""
        try:
            data = await self._request_once("GET", "/fapi/v1/time", signed=False, params=None)
            server_time = data["serverTime"]
            self._clock_offset_ms = server_time - int(time.time() * 1000)
        except Exception as e:
            log.error("Could not resync clock against Binance: %s", e)

    # ---------------------------------------------------------------- market data
    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[list]:
        return await self._request(
            "GET", "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )

    async def get_mark_price(self, symbol: str) -> float:
        data = await self._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol})
        return float(data["markPrice"])

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        data = await self._request("GET", "/fapi/v1/exchangeInfo")
        for s in data["symbols"]:
            if s["symbol"] != symbol:
                continue
            price_tick = qty_step = min_qty = 0.0
            min_notional = 0.0
            market_qty_step = market_min_qty = 0.0
            market_max_qty = float("inf")
            max_notional = float("inf")
            for f in s["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    price_tick = float(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    qty_step = float(f["stepSize"])
                    min_qty = float(f["minQty"])
                elif f["filterType"] == "MARKET_LOT_SIZE":
                    market_qty_step = float(f["stepSize"])
                    market_min_qty = float(f["minQty"])
                    market_max_qty = float(f["maxQty"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    min_notional = float(f.get("notional", f.get("minNotional", 0)))
                elif f["filterType"] == "MAX_NOTIONAL" or f["filterType"] == "NOTIONAL":
                    # NOTIONAL sometimes carries both min and max on the same
                    # filter entry, depending on symbol/API version - read
                    # whichever key is actually present, don't assume one.
                    if "maxNotional" in f:
                        max_notional = float(f["maxNotional"])
            # Not every symbol exposes MARKET_LOT_SIZE - fall back to the
            # general LOT_SIZE values rather than leaving them at 0/inf,
            # which would incorrectly reject or never round a market qty.
            if market_qty_step <= 0:
                market_qty_step = qty_step
                market_min_qty = min_qty
            info = SymbolInfo(symbol, price_tick, qty_step, min_qty, min_notional,
                               market_qty_step, market_min_qty, market_max_qty, max_notional)
            self._symbol_cache[symbol] = info
            return info
        raise ValueError(f"Symbol {symbol} not found on exchange")

    # ---------------------------------------------------------------- user data stream (listenKey)
    # listenKey management endpoints are unsigned (API-KEY header only, no
    # HMAC signature/timestamp) - this specific convention has been stable
    # and unchanged on Binance for years, unlike the Algo Order endpoints
    # above which had a genuine, recent breaking change. Confirmed the
    # connect/event-schema details fresh against developers.binance.com
    # (2026-09-14); this one specific "unsigned" detail is carried over from
    # established, long-stable API convention rather than freshly re-checked.
    async def create_listen_key(self) -> str:
        """POST /fapi/v1/listenKey - starts (or returns the already-active)
        user data stream listenKey, valid 60 minutes from creation/last
        keepalive."""
        data = await self._request("POST", "/fapi/v1/listenKey", signed=False)
        return data["listenKey"]

    async def keepalive_listen_key(self, listen_key: str):
        """PUT /fapi/v1/listenKey - extends validity another 60 minutes.
        Per the docs, a -1125 error ("This listenKey does not exist") means
        the key expired anyway and a fresh one must be created instead."""
        return await self._request("PUT", "/fapi/v1/listenKey", signed=False)

    async def close_listen_key(self, listen_key: str):
        """DELETE /fapi/v1/listenKey - closes the stream, invalidates the key."""
        return await self._request("DELETE", "/fapi/v1/listenKey", signed=False)

    # ---------------------------------------------------------------- account
    async def get_equity(self) -> float:
        data = await self._request("GET", "/fapi/v2/account", signed=True)
        return float(data["totalMarginBalance"])

    async def get_available_balance(self) -> float:
        data = await self._request("GET", "/fapi/v2/account", signed=True)
        return float(data["availableBalance"])

    async def get_position_risk(self, symbol: str) -> dict | None:
        data = await self._request("GET", "/fapi/v2/positionRisk", signed=True, params={"symbol": symbol})
        for p in data:
            if p["symbol"] == symbol and float(p["positionAmt"]) != 0:
                return p
        return None

    async def get_all_open_positions(self) -> list[dict]:
        """GET /fapi/v2/positionRisk with no symbol filter - every open
        position on the ACCOUNT, not just one pair. Used for the account-
        wide exposure cap (see risk_guard.py) so it reflects real exchange
        state rather than only this process's own in-memory tracking of its
        sibling pairs, which could miss a crashed/ERROR sibling instance, a
        manually-placed position, or any position this process otherwise
        doesn't know about."""
        data = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        return [p for p in data if float(p.get("positionAmt", 0)) != 0]

    async def get_open_orders(self, symbol: str) -> list[dict]:
        """Regular (non-conditional) orders only. Since Binance's 2025-12-09
        migration, STOP_MARKET/TAKE_PROFIT_MARKET/etc. are NOT regular orders
        and will never appear here - see get_open_algo_orders()."""
        return await self._request("GET", "/fapi/v1/openOrders", signed=True, params={"symbol": symbol})

    # ---------------------------------------------------------------- algo (conditional) orders
    # Confirmed directly against developers.binance.com's current REST API
    # reference (fetched, not guessed) on 2026-09-12. Effective 2025-12-09,
    # Binance moved STOP_MARKET/TAKE_PROFIT_MARKET/STOP/TAKE_PROFIT/
    # TRAILING_STOP_MARKET off /fapi/v1/order entirely - submitting them
    # there now returns -4120 STOP_ORDER_SWITCH_ALGO. This bot's SL/TP are
    # both STOP_MARKET/TAKE_PROFIT_MARKET, so they MUST go through this
    # dedicated Algo Order API instead. Field names are genuinely different
    # from the regular order API: algoId (not orderId), clientAlgoId (not
    # clientOrderId), orderType (not type), triggerPrice (not stopPrice),
    # algoStatus (not status).
    async def get_open_algo_orders(self, symbol: str) -> list[dict]:
        """GET /fapi/v1/openAlgoOrders - all currently-resting algo
        (conditional) orders for this symbol, i.e. this bot's SL/TP/trailing
        stops. Does NOT accept a symbol-and-orderId style call; symbol alone
        scopes it (weight 1 with a symbol, 40 without - always pass symbol)."""
        return await self._request(
            "GET", "/fapi/v1/openAlgoOrders", signed=True,
            params={"algoType": "CONDITIONAL", "symbol": symbol},
        )

    async def get_algo_order(self, algo_id: int) -> dict:
        """GET /fapi/v1/algoOrder - status of a single algo order. Per the
        documented response shape, algoStatus can be NEW, CANCELED,
        TRIGGERED, or FINISHED - TRIGGERED/FINISHED is this bot's equivalent
        of the old regular-order "FILLED" check. `actualOrderId`/
        `actualPrice`/`actualQty` are only populated once triggered - they
        describe the REAL underlying order the trigger produced, which is
        itself a regular order with its own regular orderId (needed to look
        up real commission via get_user_trades - an algoId is a different ID
        space and cannot be used there directly).
        NOTE: unlike get_open_algo_orders, this does NOT take a symbol
        parameter - confirmed against the current docs, algoId alone is
        sufficient (it's account-wide unique, not per-symbol)."""
        return await self._request(
            "GET", "/fapi/v1/algoOrder", signed=True,
            params={"algoId": algo_id},
        )

    async def get_algo_order_by_client_id(self, client_algo_id: str) -> dict | None:
        """Same endpoint as get_algo_order, queried by clientAlgoId instead -
        confirmed against the current "Query Algo Order" docs ("algoId or
        clientAlgoId must be sent"). Used to resolve an AMBIGUOUS
        STOP_MARKET/TAKE_PROFIT_MARKET placement response (item 7 fix,
        2026-09-14, per a third-party review, closing the same gap already
        fixed for regular market orders - see market_order's own docstring)
        by querying the SAME client-assigned id used to place it, rather
        than guessing or blindly retrying with a fresh one. Returns None
        only for a clean "genuinely does not exist" response - any other
        error is NOT swallowed, since that would be guessing."""
        try:
            return await self._request(
                "GET", "/fapi/v1/algoOrder", signed=True,
                params={"clientAlgoId": client_algo_id},
            )
        except BinanceAPIError as e:
            if e.code in ORDER_ALREADY_GONE_CODES:
                return None
            raise

    async def stop_market_order(self, symbol: str, side: str, trigger_price: float,
                                 skip_throttle: bool = False, trigger_px_type: str = "mark") -> dict:
        """STOP_MARKET conditional order via the Algo Order API - see the
        module-level note above on why this can no longer go through
        /fapi/v1/order. closePosition=true still means "close the entire
        position", exactly as before; quantity is still omitted (mutually
        exclusive with closePosition on this endpoint too, per the current
        docs).

        trigger_px_type is accepted (matching the shared interface OKX
        actually uses this for) but deliberately IGNORED here - Binance
        always uses MARK_PRICE regardless of this argument, since that's
        already the one, deliberately-chosen basis both platforms agree on
        (see the workingType note right below). This isn't a partial
        implementation - it's a platform that only offers one basis,
        accepting the shared parameter so callers never need a
        Binance-vs-OKX branch just to place a protective order.

        workingType=MARK_PRICE (2026-09-16 fix, found during a third-party
        review): the local pre-send check (validate_stop_side, called from
        instance.py) has always validated the stop against MARK price -
        but this order was submitting workingType=CONTRACT_PRICE, telling
        Binance to evaluate/trigger the stop against last-traded price
        instead. Two different price bases for the same stop: local
        validation could pass against mark price while the order itself
        triggers (or gets rejected) based on a divergent contract price,
        especially during a fast move where the two prices separate the
        most. Changed to MARK_PRICE so both sides agree - also the safer,
        more manipulation-resistant choice, and what OKX's own
        (currently-unwired) trigger_px_type setting already defaults to.

        priceProtect=false (explicit owner decision, 2026-09-14): Binance's
        priceProtect adds an extra check before honoring a trigger - if the
        Mark Price and Last/Contract Price have diverged more than the
        symbol's own "triggerProtect" threshold at that instant, Binance
        delays/blocks the trigger even though the stop price was reached.
        That protects against a brief manipulated/anomalous price spike
        firing the stop for no real reason, but the same mechanism can also
        delay a genuine stop-loss in a fast, violent real move - exactly
        the ambiguity the owner does not want: "SL is SL, TP is TP" - once
        the price condition is met, it must fire, no exceptions.

        Ambiguous-response recovery (item 7 fix, 2026-09-14, per a third-
        party review): closes the same gap already fixed for market_order/
        close_position_market - if this request times out or hits a
        network error, Binance may have actually placed the algo order; the
        response is what's lost, not necessarily the order. Queries the
        SAME clientAlgoId before giving up, using the real order if it
        turns out to have gone through, rather than leaving the caller to
        retry blind (which the existing dedup-before-placing check in
        _place_protective_orders would likely catch on a LATER call, but
        only if one happens - this closes the gap at the source instead of
        relying on that as the only safety net).

        skip_throttle (item 8 fix, 2026-09-14, per a third-party review):
        the proactive order-rate throttle (see _throttle_if_near_order_limit)
        can sleep up to 65 seconds - fine for a routine amendment, where the
        OLD protection is still resting the whole time, but a real risk for
        the INITIAL placement right after a fresh entry, which would sit
        genuinely unprotected for that whole wait. _place_protective_orders
        (the initial-placement path) passes skip_throttle=True; _amend_sl/
        _amend_tp (routine amendments, where old protection stays active
        during any wait) do not, and keep the proactive throttle."""
        client_algo_id = self._new_client_order_id()
        if not skip_throttle:
            await self._throttle_if_near_order_limit()
        try:
            return await self._request(
                "POST", "/fapi/v1/algoOrder", signed=True,
                params={
                    "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
                    "type": "STOP_MARKET", "triggerPrice": trigger_price,
                    "closePosition": "true", "workingType": "MARK_PRICE",
                    "priceProtect": "false", "clientAlgoId": client_algo_id,
                },
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as ambiguous_exc:
            log.warning("SL placement for %s: response ambiguous (%s) - querying by "
                        "clientAlgoId %s to confirm before giving up.",
                        symbol, ambiguous_exc, client_algo_id)
            try:
                existing = await self.get_algo_order_by_client_id(client_algo_id)
            except Exception:
                existing = None
            if existing is not None:
                log.warning("Confirmed: the ambiguous SL placement for %s actually went "
                            "through (algoId %s) - using the real order, not placing another.",
                            symbol, existing.get("algoId"))
                return existing
            self._log_ambiguous_order_unresolved(symbol, client_algo_id, ambiguous_exc)
            raise ambiguous_exc

    async def take_profit_market_order(self, symbol: str, side: str, trigger_price: float,
                                        skip_throttle: bool = False, trigger_px_type: str = "mark") -> dict:
        """TAKE_PROFIT_MARKET conditional order via the Algo Order API -
        same reasoning as stop_market_order above, including the
        priceProtect=false decision, the ambiguous-response recovery, the
        skip_throttle parameter for initial post-entry placement, and
        trigger_px_type being accepted but ignored (Binance only offers
        MARK_PRICE regardless - see stop_market_order's own docstring)."""
        client_algo_id = self._new_client_order_id()
        if not skip_throttle:
            await self._throttle_if_near_order_limit()
        try:
            return await self._request(
                "POST", "/fapi/v1/algoOrder", signed=True,
                params={
                    "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
                    "type": "TAKE_PROFIT_MARKET", "triggerPrice": trigger_price,
                    "closePosition": "true", "workingType": "MARK_PRICE",
                    "priceProtect": "false", "clientAlgoId": client_algo_id,
                },
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as ambiguous_exc:
            log.warning("TP placement for %s: response ambiguous (%s) - querying by "
                        "clientAlgoId %s to confirm before giving up.",
                        symbol, ambiguous_exc, client_algo_id)
            try:
                existing = await self.get_algo_order_by_client_id(client_algo_id)
            except Exception:
                existing = None
            if existing is not None:
                log.warning("Confirmed: the ambiguous TP placement for %s actually went "
                            "through (algoId %s) - using the real order, not placing another.",
                            symbol, existing.get("algoId"))
                return existing
            self._log_ambiguous_order_unresolved(symbol, client_algo_id, ambiguous_exc)
            raise ambiguous_exc

    async def cancel_algo_order(self, algo_id: int):
        """DELETE /fapi/v1/algoOrder - cancels a single algo (conditional)
        order. NOTE: confirmed against the current docs, this endpoint does
        NOT take a symbol parameter - only algoId (or clientAlgoId) plus the
        usual timestamp/recvWindow/signature."""
        await self._throttle_if_near_order_limit()
        return await self._request(
            "DELETE", "/fapi/v1/algoOrder", signed=True,
            params={"algoId": algo_id},
        )

    async def cancel_all_algo_open_orders(self, symbol: str):
        """DELETE /fapi/v1/algoOpenOrders - cancels every resting algo order
        on a symbol in one call. Unlike cancel_algo_order, this DOES require
        symbol (confirmed against the current docs)."""
        await self._throttle_if_near_order_limit()
        return await self._request(
            "DELETE", "/fapi/v1/algoOpenOrders", signed=True,
            params={"symbol": symbol},
        )

    # ---------------------------------------------------------------- setup
    async def set_leverage(self, symbol: str, leverage: int):
        return await self._request(
            "POST", "/fapi/v1/leverage", signed=True,
            params={"symbol": symbol, "leverage": int(leverage)},
        )

    async def set_margin_type(self, symbol: str, isolated: bool):
        try:
            return await self._request(
                "POST", "/fapi/v1/marginType", signed=True,
                params={"symbol": symbol, "marginType": "ISOLATED" if isolated else "CROSSED"},
            )
        except BinanceAPIError as e:
            # -4046 = "No need to change margin type" - not a real error, ignore
            if isinstance(e.payload, dict) and e.payload.get("code") == -4046:
                return None
            raise

    # ---------------------------------------------------------------- rounding helpers
    # Decimal, not float, arithmetic - stepSize/tickSize values like 0.001 aren't exactly
    # representable in binary floats, and rounding a qty/price the wrong way by even one
    # tick gets the order rejected outright (-1111/-1013) instead of silently truncated.
    @staticmethod
    def _round_step_decimal(value: float, step: float) -> float:
        if step <= 0:
            return value
        step_dec = Decimal(str(step))
        v_dec = Decimal(str(value)).quantize(step_dec, rounding=ROUND_DOWN)
        return float(v_dec)

    async def round_qty(self, symbol: str, qty: float) -> float:
        """Uses MARKET_LOT_SIZE's step (2026-09-14 fix, item 11) - this bot's
        entries/closes are always MARKET orders, and that filter can have a
        genuinely different step than the general LOT_SIZE (which also
        covers order types this bot never places). get_symbol_info falls
        back to the general LOT_SIZE values for a symbol that doesn't
        expose MARKET_LOT_SIZE at all."""
        info = await self.get_symbol_info(symbol)
        q = self._round_step_decimal(qty, info.market_qty_step)
        q = max(q, 0.0)
        if info.market_max_qty and q > info.market_max_qty:
            q = self._round_step_decimal(info.market_max_qty, info.market_qty_step)
        return q

    async def round_price(self, symbol: str, price: float) -> float:
        info = await self.get_symbol_info(symbol)
        return self._round_step_decimal(price, info.price_tick)

    async def check_min_notional(self, symbol: str, qty: float, price: float) -> tuple[bool, str]:
        """Binance rejects an order outright if qty*price is below MIN_NOTIONAL
        or above MAX_NOTIONAL (2026-09-14 fix, item 11 - the upper bound was
        not modeled at all before this), independent of the LOT_SIZE (step)
        check - worth catching before we even try to place the order rather
        than parsing a rejection after the fact. Also checks MARKET_LOT_SIZE's
        own min/max quantity bounds, since a quantity can pass the general
        LOT_SIZE check and still be rejected for the market-specific one."""
        info = await self.get_symbol_info(symbol)
        notional = qty * price
        if info.min_notional and notional < info.min_notional:
            return False, f"order notional {notional:.4f} is below the exchange minimum {info.min_notional} for {symbol}"
        if info.max_notional and info.max_notional != float("inf") and notional > info.max_notional:
            return False, f"order notional {notional:.4f} is above the exchange maximum {info.max_notional} for {symbol}"
        if info.market_min_qty and qty < info.market_min_qty:
            return False, f"order qty {qty} is below the market-order minimum {info.market_min_qty} for {symbol}"
        if info.market_max_qty and info.market_max_qty != float("inf") and qty > info.market_max_qty:
            return False, f"order qty {qty} is above the market-order maximum {info.market_max_qty} for {symbol}"
        return True, ""

    async def get_leverage_brackets(self, symbol: str) -> list[dict]:
        """GET /fapi/v1/leverageBracket (USER_DATA, signed) - confirmed
        against Binance's current official docs. The response can come
        back either as a list containing one entry (querying without a
        symbol, or some account states) or as a single object directly
        (querying with a symbol - "OR (if symbol sent)" per the docs) -
        both shapes are handled here rather than assuming one."""
        data = await self._request("GET", "/fapi/v1/leverageBracket", signed=True,
                                    params={"symbol": symbol})
        if isinstance(data, list):
            entry = data[0] if data else {"brackets": []}
        else:
            entry = data
        brackets = []
        for b in entry.get("brackets", []):
            brackets.append({
                "max_leverage": int(b["initialLeverage"]),
                "notional_floor": float(b["notionalFloor"]),
                "notional_cap": float(b["notionalCap"]),
            })
        brackets.sort(key=lambda b: b["notional_floor"])
        return brackets

    @staticmethod
    def validate_stop_side(direction: str, stop_price: float, mark_price: float) -> tuple[bool, str]:
        """A stop-loss placed on the wrong side of the current market price triggers
        instantly (Binance error -2021) instead of protecting the position. Check
        before sending, not after the rejection.
        LONG (SELL stop) must sit BELOW mark price; SHORT (BUY stop) must sit ABOVE it."""
        if direction == "LONG" and stop_price >= mark_price:
            return False, f"LONG stop {stop_price} is at/above mark price {mark_price} - would trigger immediately"
        if direction == "SHORT" and stop_price <= mark_price:
            return False, f"SHORT stop {stop_price} is at/below mark price {mark_price} - would trigger immediately"
        return True, ""

    # ---------------------------------------------------------------- position mode
    async def get_position_mode(self) -> bool:
        """Returns True if the account is in Hedge Mode (dualSidePosition)."""
        data = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(data.get("dualSidePosition", False))

    async def verify_one_way_mode(self):
        """
        This bot's whole position model assumes ONE-WAY mode: exactly one signed
        positionAmt per symbol. In HEDGE MODE, Binance tracks separate LONG/SHORT
        legs per symbol and this code would silently read the wrong one (or miss a
        real position on the other side).

        FAILS CLOSED: if the check itself can't be performed (network error), this
        raises rather than assuming one-way mode and trading blind on an unverified
        assumption.
        """
        try:
            hedge_mode = await self.get_position_mode()
        except Exception as e:
            raise RuntimeError(
                f"Could not verify Binance position mode (one-way vs hedge): {e}. "
                f"Refusing to start rather than assume one-way mode."
            )
        if hedge_mode:
            raise RuntimeError(
                "This Binance account is in HEDGE MODE (dualSidePosition=True). This bot "
                "requires ONE-WAY mode. Switch it in Binance Futures settings > Position "
                "Mode (only allowed while you have no open positions/orders on ANY symbol), "
                "then restart."
            )

    # ---------------------------------------------------------------- orders
    # Every order below gets a newClientOrderId tagged with CLIENT_ORDER_ID_PREFIX
    # so reconciliation can tell "ours" apart from any other order sitting on the
    # same symbol, and never touches an order it didn't place.
    # Deliberately NOT throttled here (market_order/close_position_market):
    # these are actual trade execution, not routine housekeeping - adding a
    # proactive delay here would work against the same "decisive, no
    # exceptions" principle just applied to SL/TP triggering (see
    # priceProtect above). If this endpoint's own rate limit is genuinely
    # exhausted, the existing retry/backoff on rejection still handles it
    # safely - this is a deliberate scope choice, not an oversight.
    async def get_order_by_client_id(self, symbol: str, orig_client_order_id: str) -> dict | None:
        """GET /fapi/v1/order with origClientOrderId - confirmed against
        Binance's current official docs (Query Order): "Either orderId or
        origClientOrderId must be sent." Used to resolve an AMBIGUOUS
        market order/close response (network error/timeout after sending -
        Binance may have actually received and processed it, we just never
        saw the reply) by querying the SAME client-assigned id, rather than
        guessing or blindly retrying with a fresh one. Returns None only for
        a clean "genuinely does not exist" response (-2011/-2013) - any
        other error is NOT swallowed here, since that would be guessing;
        the caller decides what an inconclusive check means."""
        try:
            return await self._request(
                "GET", "/fapi/v1/order", signed=True,
                params={"symbol": symbol, "origClientOrderId": orig_client_order_id},
            )
        except BinanceAPIError as e:
            if e.code in ORDER_ALREADY_GONE_CODES:
                return None
            raise

    async def market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """Market entry. Confirmed 2026-09-14 (per three independent
        third-party reviews all flagging this as the single most important
        remaining live-trading risk): if this request times out or hits a
        network error, Binance may have actually received and filled it -
        the response is what's lost, not necessarily the order. Blindly
        treating that as "never happened" and letting a caller retry with
        a fresh order risks a real duplicate position. Instead, on exactly
        that ambiguous failure (never on a clean rejection - a rejection
        means Binance is telling us definitively it did NOT go through),
        this queries the SAME client_order_id it just used and returns the
        real order if it turns out to have gone through. If the query
        itself can't determine an answer either way, the original ambiguity
        is re-raised rather than guessing in either direction."""
        client_order_id = self._new_client_order_id()
        try:
            return await self._request(
                "POST", "/fapi/v1/order", signed=True,
                params={"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity,
                        "newClientOrderId": client_order_id},
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as ambiguous_exc:
            log.warning("Market entry for %s: response ambiguous (%s) - querying by "
                        "client order id %s to confirm before giving up.",
                        symbol, ambiguous_exc, client_order_id)
            try:
                existing = await self.get_order_by_client_id(symbol, client_order_id)
            except Exception:
                existing = None
            if existing is not None:
                log.warning("Confirmed: the ambiguous market entry for %s actually went "
                            "through (orderId %s) - using the real order, not placing another.",
                            symbol, existing.get("orderId"))
                return existing
            self._log_ambiguous_order_unresolved(symbol, client_order_id, ambiguous_exc)
            raise ambiguous_exc

    async def close_position_market(self, symbol: str, side: str, quantity: float) -> dict:
        """side = the side needed to CLOSE the position (opposite of entry
        side). Same ambiguous-response handling as market_order above,
        applied here too since the exact same risk exists for closes."""
        client_order_id = self._new_client_order_id()
        try:
            return await self._request(
                "POST", "/fapi/v1/order", signed=True,
                params={
                    "symbol": symbol, "side": side, "type": "MARKET",
                    "quantity": quantity, "reduceOnly": "true",
                    "newClientOrderId": client_order_id,
                },
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as ambiguous_exc:
            log.warning("Close order for %s: response ambiguous (%s) - querying by "
                        "client order id %s to confirm before giving up.",
                        symbol, ambiguous_exc, client_order_id)
            try:
                existing = await self.get_order_by_client_id(symbol, client_order_id)
            except Exception:
                existing = None
            if existing is not None:
                log.warning("Confirmed: the ambiguous close for %s actually went through "
                            "(orderId %s) - using the real order, not sending another close.",
                            symbol, existing.get("orderId"))
                return existing
            self._log_ambiguous_order_unresolved(symbol, client_order_id, ambiguous_exc)
            raise ambiguous_exc

    @staticmethod
    def _log_ambiguous_order_unresolved(symbol: str, client_order_id: str, exc: Exception):
        """2026-09-14 fix (item 9, per a third-party review): attaches the
        client_order_id to the exception itself (exc.client_order_id)
        before it propagates, so ANY caller catching this exception can
        durably flag/notify about it using the real id - without needing
        its own separate tracking of what id was used for this specific
        attempt. See app/reconciliation.py."""
        log.error("Could not confirm whether the order for %s (client id %s) went through "
                  "or not - Binance's own query was inconclusive too. Surfacing the original "
                  "error rather than guessing; check the exchange manually if this recurs.",
                  symbol, client_order_id)
        exc.client_order_id = client_order_id

    async def cancel_regular_order(self, symbol: str, order_id: int):
        """Cancels a regular (non-algo) order - MARKET/LIMIT. This bot's
        entries and market-closes fill instantly so they're never actually
        "open" long enough to need cancelling; kept for completeness /
        explicit operator use. SL/TP/trailing-stop cancellation goes through
        cancel_algo_order instead - they're a different order system
        entirely since Binance's 2025-12-09 migration (see the algo section
        above), not a fallback path off this method."""
        await self._throttle_if_near_order_limit()
        return await self._request(
            "DELETE", "/fapi/v1/order", signed=True,
            params={"symbol": symbol, "orderId": order_id},
        )

    async def cancel_all_open_orders(self, symbol: str):
        """Symbol-wide cancel of BOTH regular orders and algo (conditional
        SL/TP/trailing-stop) orders - two separate Binance systems since the
        2025-12-09 migration, so both endpoints are called to actually clear
        everything. NOT used automatically anywhere in routine operation
        (see BotInstance._cancel_own_orders, which only ever cancels orders
        this bot itself placed, via the algo endpoint specifically) - this
        blanket version is only invoked from the explicit, owner-approved
        pre-entry cleanup (see BotInstance._do_enter)."""
        await self._throttle_if_near_order_limit()
        errors = []
        try:
            await self._request("DELETE", "/fapi/v1/allOpenOrders", signed=True, params={"symbol": symbol})
        except BinanceAPIError as e:
            errors.append(f"regular orders: {e}")
        try:
            await self.cancel_all_algo_open_orders(symbol)
        except BinanceAPIError as e:
            errors.append(f"algo orders: {e}")
        if errors:
            raise BinanceAPIError(0, {"msg": "; ".join(errors)})

    async def get_user_trades(self, symbol: str, order_id: int | None = None, limit: int = 50) -> list[dict]:
        """Real fill records for this symbol, including the ACTUAL commission
        Binance charged - used to compute net (not estimated) PnL for the
        ledger. Never used to simulate or predict a fee; if this call fails,
        callers must report gross PnL with commission left unrecorded rather
        than substituting a guessed value.

        NOTE: `order_id` here must be a REGULAR order id (this endpoint's own
        `orderId` filter), never an algoId - they're different ID spaces. For
        a fill that came from a triggered SL/TP, resolve the algo order via
        get_algo_order() first and use its `actualOrderId`."""
        params = {"symbol": symbol, "limit": limit}
        if order_id is not None:
            params["orderId"] = order_id
        return await self._request("GET", "/fapi/v1/userTrades", signed=True, params=params)
