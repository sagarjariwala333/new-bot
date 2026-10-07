"""
okx_futures.py
===============

OKX v5 USDT-margined perpetual swap ("SWAP") adapter, implementing the same
BaseExchangeAdapter contract as BinanceFuturesClient (see
app/exchange_adapter.py). instance.py's trading logic is unaware which of
the two it's holding.

STEP 2 SCOPE (2026-09-14): this file is built and unit-tested in isolation.
It is NOT yet imported by manager.py/instance.py/store.py - that wiring is a
separate, later step so it can be reviewed on its own. Nothing in this file
changes any existing behavior.

Three things are genuinely different from Binance and deserve explicit
attention (each documented again at its point of use below):

  1. CONTRACT-SIZE CONVERSION. Binance orders are sized directly in the
     underlying coin (e.g. "0.05" BTC). OKX SWAP orders are sized in
     CONTRACTS ("sz"), converted via the instrument's `ctVal`. instance.py's
     sizing math (equity * marginPct * leverage / price) produces a COIN
     quantity exactly like it always has - the coin<->contract conversion
     happens ONLY inside this adapter's round_qty/market_order/
     close_position_market, so nothing above this file needs to know
     contracts exist.

  2. SL/TP AS TWO INDEPENDENT ALGO ORDERS, NOT ATTACHED-AT-ENTRY. OKX
     supports attaching SL/TP directly onto the entry order
     (attachAlgoOrds). This bot does NOT use that - instance.py places SL
     and TP as two separate calls, tracks their ids separately
     (sl_order_id/tp_order_id), and can amend/cancel one without touching
     the other (see instance.py's "place new before cancelling old"
     amendment pattern). To preserve that exact behavior, stop_market_order
     and take_profit_market_order below each place their OWN standalone
     /trade/order-algo conditional order - mirroring Binance's two separate
     algoIds, not OKX's combined-order feature.

  3. algoId/orderId ARE NOT GLOBALLY UNIQUE ACROSS INSTRUMENTS THE WAY
     BINANCE'S ARE FOR THIS ADAPTER'S PURPOSES - OKX's cancel-algo and
     query-algo-order endpoints require instId alongside algoId (Binance's
     equivalent calls take algoId alone). Since BaseExchangeAdapter's
     interface (shared with Binance) only passes algo_id, this adapter
     keeps a small in-memory {algo_id: instId} cache, populated whenever an
     algo order is placed or listed, and consulted by cancel_algo_order/
     get_algo_order. This is an adapter-internal accommodation - it does
     not change the interface or instance.py.

Signing (OKX v5, confirmed against the current official API docs):
  sign = base64( HMAC-SHA256( secret, timestamp + method + requestPath + body ) )
  timestamp = ISO-8601 UTC with milliseconds and a literal "Z", e.g.
              "2026-09-14T12:34:56.789Z" - NOT the Binance-style epoch-ms
              integer.
  headers = OK-ACCESS-KEY, OK-ACCESS-SIGN, OK-ACCESS-TIMESTAMP,
            OK-ACCESS-PASSPHRASE, Content-Type: application/json, and
            x-simulated-trading: "1" when running against demo trading.
  requestPath INCLUDES the querystring for GETs (e.g. "/api/v5/market/candles?instId=...")
  and the exact JSON body string (compact, no extra whitespace) for
  POSTs - the signature is computed over the literal bytes sent on the wire.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from app.exchange_adapter import BaseExchangeAdapter, ExchangeAPIError
from app.binance_futures import SymbolInfo  # shared value type - see note in exchange_adapter.py

MAINNET_BASE = "https://www.okx.com"
DEMO_BASE = "https://www.okx.com"  # OKX demo trading uses the SAME host, distinguished by the
                                    # x-simulated-trading header, not a separate base URL/subdomain -
                                    # confirmed against current OKX v5 docs (unlike Binance, which
                                    # uses a genuinely separate testnet host).

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)

CLIENT_ORDER_ID_PREFIX = "hullbotokx"  # OKX clOrdId: letters/digits only, <=32 chars - no underscore
# Extra prefixes still recognised as this bot's own (none in this package).
LEGACY_CLIENT_ORDER_ID_PREFIXES = ()
OWN_ORDER_PREFIXES = (CLIENT_ORDER_ID_PREFIX,) + LEGACY_CLIENT_ORDER_ID_PREFIXES

log = logging.getLogger("okx")

# Error-code classification (OKX v5 published codes, string-typed unlike Binance's ints).
POSITION_LIMIT_CODES = {"54030"}          # this account/sub-account's max open-position notional exceeded
ORDER_ALREADY_GONE_CODES = {"51400", "51401", "51403", "51603"}  # cancel/query on an order that's already done or never existed
CLOCK_DRIFT_CODES = {"50113"}             # timestamp expired / request outside receive window
RATE_LIMIT_CODES = {"50011"}
NON_RETRYABLE_CODES = {"51004", "51006", "51008", "51020"}  # bad qty/price/insufficient margin etc - needs a human


class OKXAPIError(ExchangeAPIError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        # OKX wraps the real per-order result in payload["data"][0], with an
        # outer "code"/"msg" that's often just "0"/"" (meaning "the HTTP
        # call itself worked, check `data` for the real per-order result").
        # Callers care about the INNER code, so surface that as .code.
        inner = None
        if isinstance(payload, dict):
            data = payload.get("data") or []
            if data and isinstance(data[0], dict):
                inner = data[0]
        self.code = (inner or {}).get("sCode") or (payload.get("code") if isinstance(payload, dict) else None)
        self.msg = (inner or {}).get("sMsg") or (payload.get("msg") if isinstance(payload, dict) else "")
        super().__init__(f"OKX API error {status} (code={self.code}): {self.msg or payload}")


class OKXPositionLimitError(OKXAPIError):
    """Raised specifically for error 54030 (position-limit exceeded). Per
    the owner's hard requirement: on this error the caller must (a) close
    the position via close_position_market - reduce-only orders are never
    subject to this limit - and (b) stop opening new entries on this
    instrument. Part (b) is enforced by THIS adapter (see
    block_new_entries/_blocked_instruments below); part (a) is the caller's
    responsibility since this adapter has no standing authority to decide
    when to close a position on its own."""


# BASE V3 (2026-09-28): OKX bar codes. The bot stores timeframes Binance-style
# ("12h"); OKX expects its own codes. 6H/12H/1D use OKX's UTC-aligned
# variants so candles line up with TradingView's OKX candles (00:00 UTC
# boundaries). OKX publishes NO 8H candle - 8h is intentionally absent (the
# dashboard/API refuse it for OKX; nothing is ever built by joining candles).
OKX_BAR_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H",
    "6h": "6Hutc", "12h": "12Hutc", "1d": "1Dutc",
}
OKX_MAX_CANDLES_PER_REQUEST = 300


def okx_bar(interval: str) -> str:
    try:
        return OKX_BAR_MAP[interval]
    except KeyError:
        raise ValueError(f"OKX has no {interval!r} candle - allowed: {sorted(OKX_BAR_MAP)}")


@dataclass
class OKXFuturesClient(BaseExchangeAdapter):
    api_key: str
    api_secret: str
    passphrase: str
    demo: bool = False  # x-simulated-trading flag; see DEMO_BASE note above
    _session: Optional[aiohttp.ClientSession] = field(default=None, init=False, repr=False)
    _symbol_cache: dict[str, SymbolInfo] = field(default_factory=dict, init=False, repr=False)
    _clock_offset_ms: int = field(default=0, init=False, repr=False)
    # algo_id -> instId, populated by place/list calls - see class docstring point 3.
    _algo_inst_cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    # instId -> last-known (mgnMode, leverage) - OKX's set-leverage call takes
    # both together; set_leverage/set_margin_type below are two separate
    # BaseExchangeAdapter methods, so each caches what it doesn't own and
    # re-sends both on every call, rather than guessing/omitting one.
    _leverage_state: dict[str, tuple[str, int]] = field(default_factory=dict, init=False, repr=False)
    # Instruments this adapter has been told (via a 54030 response) to stop
    # opening NEW entries on. Never touched for closes - see
    # OKXPositionLimitError.
    _blocked_instruments: set[str] = field(default_factory=set, init=False, repr=False)

    # ---------------------------------------------------------------- response normalization
    # instance.py is written entirely against Binance's response field names
    # (positionAmt, entryPrice, orderId, avgPrice, algoId, clientAlgoId,
    # orderType, triggerPrice, algoStatus) - BaseExchangeAdapter's contract
    # covers method signatures, but for a SINGLE instance.py to run
    # unchanged against either exchange, the returned dict SHAPE must match
    # too. These helpers translate OKX's native response fields into that
    # same shape (extra OKX-native fields are left in place alongside them,
    # never removed, in case anything downstream wants them later).
    async def _normalize_position(self, raw: dict) -> dict:
        info = await self.get_symbol_info(raw["instId"])
        ct_val = getattr(info, "ct_val", 1.0) or 1.0
        normalized = dict(raw)
        normalized["symbol"] = raw.get("instId")
        normalized["positionAmt"] = float(raw.get("pos", 0) or 0) * ct_val
        normalized["entryPrice"] = raw.get("avgPx")
        return normalized

    @staticmethod
    def _normalize_order(raw: dict) -> dict:
        normalized = dict(raw)
        normalized["orderId"] = raw.get("ordId")
        # 2026-09-15 fix: also surface the client order id under Binance's
        # naming (clientOrderId) - needed so instance.py can clear the
        # SPECIFIC pending-reconciliation record for this order rather than
        # blanket-clearing every unresolved record for the symbol whenever
        # any unrelated order succeeds. OKX already echoes clOrdId back in
        # its response; this was just never surfaced under the name the
        # rest of the code actually looks for.
        if raw.get("clOrdId"):
            normalized["clientOrderId"] = raw["clOrdId"]
        # OKX's order-placement response doesn't carry a synchronous fill
        # price the way Binance's MARKET order response often does - only
        # set avgPrice when OKX actually provided one (e.g. from a later
        # query), never fabricate it. Callers already tolerate a missing
        # avgPrice gracefully (fall back to the last known close).
        if raw.get("avgPx"):
            normalized["avgPrice"] = raw["avgPx"]
        return normalized

    # OKX algo `state` -> Binance-style `algoStatus`, since instance.py
    # checks algoStatus in ("TRIGGERED", "FINISHED") to detect a fill.
    _ALGO_STATE_MAP = {
        "live": "NEW", "pause": "NEW", "partially_effective": "NEW",
        "effective": "FINISHED",
        "canceled": "CANCELED", "order_failed": "CANCELED",
    }

    @classmethod
    def _normalize_algo_order(cls, raw: dict, order_type_hint: str | None = None) -> dict:
        normalized = dict(raw)
        normalized["clientAlgoId"] = raw.get("algoClOrdId", "")
        normalized["algoStatus"] = cls._ALGO_STATE_MAP.get(raw.get("state"), "NEW")
        if order_type_hint:
            normalized["orderType"] = order_type_hint
        elif float(raw.get("slTriggerPx") or 0) > 0:
            normalized["orderType"] = "STOP_MARKET"
        elif float(raw.get("tpTriggerPx") or 0) > 0:
            normalized["orderType"] = "TAKE_PROFIT_MARKET"
        normalized["triggerPrice"] = raw.get("slTriggerPx") or raw.get("tpTriggerPx")
        return normalized

    @property
    def base_url(self) -> str:
        return DEMO_BASE if self.demo else MAINNET_BASE

    @property
    def client_order_id_prefix(self) -> tuple[str, ...]:
        """Current tag first, then legacy tags - see binance_futures.py."""
        return OWN_ORDER_PREFIXES

    @property
    def supports_realtime_stream(self) -> bool:
        # KNOWN GAP (2026-09-14): no OKX websocket order/position stream
        # built yet - this adapter is REST-polling only for now. instance.py
        # already treats the real-time stream as optional (see the
        # try/except around get_or_create_user_data_stream in
        # BotInstance._run), so this is a real but bounded gap: OKX pairs
        # detect fills at the normal poll interval rather than near-
        # instantly, same as Binance behaved before its own stream was
        # added. Not unsafe, just slower to notice - flagged here rather
        # than silently shipped as equivalent to Binance's behavior.
        return False

    # ---------------------------------------------------------------- session/signing
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _iso_timestamp(offset_ms: int = 0) -> str:
        """ISO-8601 UTC with milliseconds + literal 'Z' - OKX's required
        format, distinct from Binance's epoch-ms integer."""
        dt = datetime.fromtimestamp((time.time() * 1000 + offset_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, timestamp: str, signature: str) -> dict:
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.demo:
            headers["x-simulated-trading"] = "1"
        return headers

    def _new_client_order_id(self) -> str:
        # OKX clOrdId: letters/digits only, max 32 chars.
        return f"{CLIENT_ORDER_ID_PREFIX}{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}"

    async def _request_once(self, method: str, path: str, params: dict | None = None,
                             body: dict | None = None, signed: bool = True) -> Any:
        session = await self._get_session()
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        request_path = f"{path}{query}"
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        timestamp = self._iso_timestamp(self._clock_offset_ms)
        headers = {}
        if signed:
            signature = self._sign(timestamp, method, request_path, body_str)
            headers = self._headers(timestamp, signature)
        url = f"{self.base_url}{request_path}"
        async with session.request(method, url, headers=headers,
                                    data=body_str if body_str else None) as resp:
            data = await resp.json(content_type=None)
            outer_code = data.get("code") if isinstance(data, dict) else None
            # OKX returns HTTP 200 for almost everything, including per-order
            # failures nested in data[].sCode - so "is this an error" is
            # judged by outer code/HTTP status/inner sCode, not HTTP status
            # alone (unlike Binance, where a 4xx status is the sole signal).
            if resp.status >= 400 or (outer_code not in (None, "0")):
                raise OKXAPIError(resp.status, data)
            inner = (data.get("data") or [{}])[0] if isinstance(data, dict) else {}
            if isinstance(inner, dict) and inner.get("sCode") not in (None, "0"):
                raise OKXAPIError(resp.status, data)
            return data

    async def _request(self, method: str, path: str, params: dict | None = None,
                        body: dict | None = None, signed: bool = True,
                        retries: int = 3, delay: float = 1.5) -> Any:
        """Same retry-classification shape as BinanceFuturesClient._request,
        adapted to OKX's string error codes. 54030 is deliberately raised as
        OKXPositionLimitError and NEVER retried - it's a real, persistent
        limit, not a transient condition."""
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return await self._request_once(method, path, params, body, signed)
            except OKXAPIError as e:
                last_exc = e
                if e.code in POSITION_LIMIT_CODES:
                    inst_id = (params or {}).get("instId") or (body or {}).get("instId")
                    if inst_id:
                        self._blocked_instruments.add(inst_id)
                        log.error("OKX 54030 (position limit) on %s - blocking NEW entries on "
                                  "this instrument until manually cleared. Existing position "
                                  "must still be closed by the caller via close_position_market "
                                  "(reduce-only is never subject to this limit).", inst_id)
                    raise OKXPositionLimitError(e.status, e.payload)
                if e.code in ORDER_ALREADY_GONE_CODES or e.code in NON_RETRYABLE_CODES:
                    raise
                if e.code in CLOCK_DRIFT_CODES:
                    log.warning("Clock drift (50113) on %s - resyncing and retrying.", path)
                    await self.sync_clock()
                    await asyncio.sleep(0.5)
                    continue
                if e.code in RATE_LIMIT_CODES:
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
        try:
            data = await self._request_once("GET", "/api/v5/public/time", signed=False)
            server_ms = int(data["data"][0]["ts"])
            self._clock_offset_ms = server_ms - int(time.time() * 1000)
        except Exception as e:
            log.error("Could not resync clock against OKX: %s", e)

    # ---------------------------------------------------------------- lifecycle
    async def verify_one_way_mode(self):
        """This bot requires OKX's net position mode (one position per
        instrument), the direct equivalent of Binance's one-way mode. FAILS
        CLOSED on any error, same policy as BinanceFuturesClient."""
        try:
            data = await self._request("GET", "/api/v5/account/config")
            pos_mode = data["data"][0]["posMode"]
        except Exception as e:
            raise RuntimeError(
                f"Could not verify OKX position mode: {e}. Refusing to start rather than "
                f"assume net mode."
            )
        if pos_mode != "net_mode":
            raise RuntimeError(
                f"This OKX account is in '{pos_mode}' mode. This bot requires NET mode "
                f"(posMode=net_mode). Change it in OKX under Preferences > Position Mode "
                f"(only allowed with no open positions/orders), then restart."
            )

    # ---------------------------------------------------------------- market data
    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[list]:
        """Returns candles oldest-first, matching BaseExchangeAdapter's
        contract.

        BUG FIX (2026-09-15, found during a from-scratch review, before
        this was ever caught by any test or a real run): OKX's actual raw
        candle response is `[ts, o, h, l, c, vol, volCcy, volCcyQuote,
        confirm]` - 9 fields - but this function used to return that raw
        shape directly. Every caller downstream (instance.py's
        _fetch_snapshot, and the analysis endpoints) builds a pandas
        DataFrame assuming Binance's 12-column kline shape
        (open_time/open/high/low/close/volume/close_time/qav/trades/
        tbbav/tbqav/ignore). Constructing that DataFrame from 9-column
        OKX rows raises `ValueError: 12 columns passed, passed data had 9
        columns` immediately - meaning a live OKX pair would have crashed
        on its very first candle fetch. This was never caught because
        FakeClient.get_klines() (used by every test) always returns
        Binance-shaped synthetic data regardless of which platform a test
        is simulating - there was no test exercising OKX's REAL response
        shape end-to-end. Fixed here, not in instance.py/analysis.py, so
        every caller gets one consistent, already-normalized kline shape
        regardless of platform - matching how every other OKX<->Binance
        difference in this adapter is handled (normalize at the adapter
        boundary, not scattered through calling code). Only the first
        five fields (open_time/open/high/low/close) are ever read
        downstream (confirmed by direct inspection) - the rest are
        harmless placeholders, not fabricated data passed off as real."""
        # The bot needs 1,000+ candles (so every indicator is fully warmed
        # up), but OKX returns at most 300 per request - page
        # backwards with `after` (= "records older than this ts") until
        # `limit` candles are collected. Timeframe is mapped to OKX's own
        # bar code (see OKX_BAR_MAP) - native OKX candles only.
        bar = okx_bar(interval)
        rows: list = []
        after = None
        endpoint = "/api/v5/market/candles"
        while len(rows) < limit:
            params = {"instId": symbol, "bar": bar,
                      "limit": min(OKX_MAX_CANDLES_PER_REQUEST, limit - len(rows))}
            if after is not None:
                params["after"] = after
            data = await self._request("GET", endpoint, params=params, signed=False)
            page = data.get("data") or []
            if not page:
                if endpoint == "/api/v5/market/candles" and after is not None:
                    # /market/candles only serves the most recent ~1,440
                    # candles; older ones come from /market/history-candles.
                    endpoint = "/api/v5/market/history-candles"
                    continue
                break
            rows.extend(page)                  # OKX returns newest -> oldest
            after = page[-1][0]                # oldest ts in this page
        raw_rows = list(reversed(rows[:limit]))
        # De-duplicate by timestamp (a page boundary can repeat a candle).
        seen = set()
        deduped = []
        for row in raw_rows:
            if row[0] in seen:
                continue
            seen.add(row[0])
            deduped.append(row)
        normalized = []
        for row in deduped:
            ts, o, h, l, c, vol = row[0], row[1], row[2], row[3], row[4], row[5]
            normalized.append([
                int(ts), o, h, l, c, vol,   # open_time, open, high, low, close, volume
                int(ts),                     # close_time - not provided by this endpoint; a
                                              # placeholder, never read downstream (confirmed)
                "0", 0, "0", "0", "0",       # qav, trades, tbbav, tbqav, ignore - not provided,
                                              # never read downstream (confirmed)
            ])
        return normalized

    async def get_mark_price(self, symbol: str) -> float:
        data = await self._request(
            "GET", "/api/v5/public/mark-price",
            params={"instType": "SWAP", "instId": symbol}, signed=False,
        )
        return float(data["data"][0]["markPx"])

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """ctVal (contract value in underlying coin) and lotSz/minSz
        (contract-count granularity) are the two OKX-specific fields the
        rest of this adapter needs for coin<->contract conversion - see the
        class docstring's point 1. Cached the same way Binance's
        exchangeInfo lookup is."""
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        data = await self._request(
            "GET", "/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": symbol}, signed=False,
        )
        rows = data["data"]
        if not rows:
            raise ValueError(f"Instrument {symbol} not found on OKX")
        row = rows[0]
        ct_val = float(row["ctVal"])
        lot_sz = float(row["lotSz"])       # contract-count step
        min_sz = float(row["minSz"])       # contract-count minimum
        tick_sz = float(row["tickSz"])
        # SymbolInfo is expressed in COIN terms throughout this adapter (see
        # class docstring point 1) - qty_step/min_qty are the coin-equivalent
        # of one lotSz/minSz worth of contracts, so round_qty/
        # check_min_notional above this line never need to know contracts
        # exist. max_notional isn't separately published by OKX per-symbol
        # the way Binance's NOTIONAL filter is - left at inf (uncapped here;
        # the account-level Max Loss Cap / exposure caps are the real limits).
        info = SymbolInfo(
            symbol=symbol, price_tick=tick_sz, qty_step=lot_sz * ct_val,
            min_qty=min_sz * ct_val, min_notional=0.0,
            market_qty_step=lot_sz * ct_val, market_min_qty=min_sz * ct_val,
            market_max_qty=float("inf"), max_notional=float("inf"),
        )
        info.ct_val = ct_val  # stashed for market_order/close_position_market's coin->contract step
        self._symbol_cache[symbol] = info
        return info

    # ---------------------------------------------------------------- account
    async def _balance_detail(self) -> dict:
        data = await self._request("GET", "/api/v5/account/balance")
        details = data["data"][0].get("details", [])
        for d in details:
            if d.get("ccy") == "USDT":
                return d
        return data["data"][0]  # fall back to the top-level summary if no USDT line item

    async def get_equity(self) -> float:
        d = await self._balance_detail()
        return float(d.get("eq") or d.get("totalEq") or 0.0)

    async def get_available_balance(self) -> float:
        d = await self._balance_detail()
        return float(d.get("availEq") or d.get("availBal") or 0.0)

    async def get_position_risk(self, symbol: str) -> dict | None:
        data = await self._request("GET", "/api/v5/account/positions", params={"instId": symbol})
        for p in data["data"]:
            if p["instId"] == symbol and float(p.get("pos", 0)) != 0:
                return await self._normalize_position(p)
        return None

    async def get_all_open_positions(self) -> list[dict]:
        data = await self._request("GET", "/api/v5/account/positions")
        return [await self._normalize_position(p) for p in data["data"] if float(p.get("pos", 0)) != 0]

    # ---------------------------------------------------------------- leverage/margin
    async def _apply_leverage(self, symbol: str, mgn_mode: str, leverage: int):
        await self._request(
            "POST", "/api/v5/account/set-leverage",
            body={"instId": symbol, "lever": str(int(leverage)), "mgnMode": mgn_mode},
        )
        self._leverage_state[symbol] = (mgn_mode, int(leverage))

    async def set_leverage(self, symbol: str, leverage: int):
        mgn_mode, _ = self._leverage_state.get(symbol, ("isolated", leverage))
        await self._apply_leverage(symbol, mgn_mode, leverage)

    async def set_margin_type(self, symbol: str, isolated: bool):
        _, leverage = self._leverage_state.get(symbol, ("isolated", 1))
        await self._apply_leverage(symbol, "isolated" if isolated else "cross", leverage)

    # ---------------------------------------------------------------- rounding/sizing
    # Same Decimal-based truncation as Binance, for the same reason: step
    # values aren't exactly representable in binary floats, and rounding the
    # wrong way gets the order rejected instead of silently truncated.
    #
    # BUG FIX (2026-09-14, found during a full bug-hunt pass, owner-approved):
    # the original version used Decimal.quantize(step_dec, ...), which rounds
    # to the same number of DECIMAL PLACES as step_dec - NOT to the nearest
    # MULTIPLE of step's value. These happen to be the same operation for
    # Binance's real step sizes (always fractional, e.g. 0.01, 0.001) - so
    # this bug never manifested there - but they are NOT the same operation
    # whenever step is a whole number (e.g. OKX lot sizes of 1, 5, or 10
    # contracts, which are common). Concretely, the old code rounded 0.1
    # contracts to "0.1" when lot_sz=1 (should truncate to 0 - below the
    # minimum tradeable size), and rounded 7.8 contracts to "7.8" when
    # lot_sz=5 (should truncate to 5.0). Verified against both cases below
    # in test_okx_futures.py's TestRoundStepDecimalMultiples.
    @staticmethod
    def _round_step_decimal(value: float, step: float) -> float:
        if step <= 0:
            return value
        step_dec = Decimal(str(step))
        value_dec = Decimal(str(value))
        multiples = (value_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
        return float(multiples * step_dec)

    async def round_qty(self, symbol: str, qty: float) -> float:
        """Rounds a COIN quantity down to the nearest whole number of
        contracts (lotSz), expressed back in coins - see class docstring
        point 1. This keeps every caller above this adapter working in
        coins, identical to Binance."""
        info = await self.get_symbol_info(symbol)
        q = self._round_step_decimal(qty, info.qty_step)
        return max(q, 0.0)

    async def round_price(self, symbol: str, price: float) -> float:
        info = await self.get_symbol_info(symbol)
        return self._round_step_decimal(price, info.price_tick)

    async def check_min_notional(self, symbol: str, qty: float, price: float) -> tuple[bool, str]:
        info = await self.get_symbol_info(symbol)
        if info.min_qty and qty < info.min_qty:
            return False, f"order qty {qty} is below OKX's minimum contract size for {symbol}"
        return True, ""

    async def get_leverage_brackets(self, symbol: str) -> list[dict]:
        """OKX's public position-tiers endpoint (GET /api/v5/public/
        position-tiers, confirmed against current OKX v5 docs) gives tiers
        by POSITION SIZE (in contracts), each with its own max leverage -
        not by notional value the way Binance's leverageBracket does.
        Converted here into the same notional_floor/notional_cap shape via
        this instrument's ctVal and current mark price, so callers get one
        unified interface regardless of platform. Public endpoint - no
        signing needed, matches get_symbol_info's own instruments call."""
        info = await self.get_symbol_info(symbol)
        ct_val = getattr(info, "ct_val", None)
        if not ct_val:
            return []
        mgn_mode, _ = self._leverage_state.get(symbol, ("isolated", 1))
        price = await self.get_mark_price(symbol)
        data = await self._request(
            "GET", "/api/v5/public/position-tiers",
            params={"instType": "SWAP", "tdMode": mgn_mode, "instId": symbol},
            signed=False,
        )
        brackets = []
        for row in data["data"]:
            min_sz = float(row.get("minSz", 0) or 0)
            max_sz_raw = row.get("maxSz")
            max_sz = float(max_sz_raw) if max_sz_raw not in (None, "") else None
            brackets.append({
                "max_leverage": int(float(row["maxLever"])),
                "notional_floor": min_sz * ct_val * price,
                "notional_cap": (max_sz * ct_val * price) if max_sz else float("inf"),
            })
        brackets.sort(key=lambda b: b["notional_floor"])
        return brackets

    @staticmethod
    def validate_stop_side(direction: str, stop_price: float, mark_price: float) -> tuple[bool, str]:
        """Identical pure math to BinanceFuturesClient.validate_stop_side -
        this check has nothing to do with which exchange is involved."""
        if direction == "LONG" and stop_price >= mark_price:
            return False, f"LONG stop {stop_price} is at/above mark price {mark_price} - would trigger immediately"
        if direction == "SHORT" and stop_price <= mark_price:
            return False, f"SHORT stop {stop_price} is at/below mark price {mark_price} - would trigger immediately"
        return True, ""

    # ---------------------------------------------------------------- coin<->contract helper
    async def _coin_qty_to_contracts(self, symbol: str, qty: float) -> str:
        info = await self.get_symbol_info(symbol)
        ct_val = getattr(info, "ct_val", None)
        if not ct_val:
            raise ValueError(f"Missing ctVal for {symbol} - get_symbol_info must run first")
        contracts = qty / ct_val
        lot_sz = info.qty_step / ct_val
        contracts = self._round_step_decimal(contracts, lot_sz)
        # OKX's `sz` is a string number - avoid a spurious trailing ".0" on
        # whole-contract sizes (e.g. send "5", not "5.0"); fractional lot
        # sizes still render with their real decimal places.
        if contracts == int(contracts):
            return str(int(contracts))
        return str(contracts)

    # ---------------------------------------------------------------- entries/exits
    async def get_order_by_client_id(self, symbol: str, client_order_id: str) -> dict | None:
        """GET /api/v5/trade/order with clOrdId - OKX's equivalent of
        Binance's get_order_by_client_id (see binance_futures.py for the
        full rationale). Added 2026-09-14 after a third-party review
        correctly found this was missing on the OKX side: market_order and
        close_position_market below now use this exact same pattern Binance
        already had - on an ambiguous network failure, query back by the
        SAME client_order_id before ever concluding the order didn't go
        through.

        Two different "genuinely not found" shapes are handled explicitly,
        since OKX can plausibly signal "no such order" either way and this
        function must not mistake one for a real error:
        - An OKXAPIError with a recognized "doesn't exist" code -> None.
        - A successful response with an empty `data` array (no error, just
          nothing found) -> None.
        Any OTHER error is NOT swallowed here - that would be guessing; the
        caller decides what a genuinely inconclusive check means."""
        try:
            resp = await self._request(
                "GET", "/api/v5/trade/order",
                params={"instId": symbol, "clOrdId": client_order_id},
            )
        except OKXAPIError as e:
            if e.code in ORDER_ALREADY_GONE_CODES:
                return None
            raise
        rows = resp.get("data") or []
        if not rows:
            return None
        return self._normalize_order(rows[0])

    async def market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """side: 'BUY'/'SELL' (matching BaseExchangeAdapter's Binance-style
        convention) - translated to OKX's lowercase 'buy'/'sell'.

        BUG FIX (2026-09-14, found during a third-party review, owner-
        approved): this used to have NO ambiguous-order recovery at all -
        unlike Binance's market_order, a lost response here meant the bot
        could not tell whether OKX actually accepted the order. Now mirrors
        Binance's exact pattern: on a network-level failure specifically
        (never a clean rejection - that means OKX definitively said no),
        query back by the same clOrdId before concluding anything."""
        if symbol in self._blocked_instruments:
            raise OKXPositionLimitError(
                0, {"code": "54030", "msg": f"{symbol} is blocked for new entries after a prior "
                                            f"position-limit rejection - close-only until cleared."})
        sz = await self._coin_qty_to_contracts(symbol, quantity)
        mgn_mode, _ = self._leverage_state.get(symbol, ("isolated", 1))
        client_order_id = self._new_client_order_id()
        try:
            data = await self._request(
                "POST", "/api/v5/trade/order",
                body={"instId": symbol, "tdMode": mgn_mode, "side": side.lower(),
                      "ordType": "market", "sz": sz, "clOrdId": client_order_id},
            )
            return self._normalize_order(data["data"][0])
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
            ambiguous_exc.client_order_id = client_order_id
            raise ambiguous_exc

    async def close_position_market(self, symbol: str, side: str, quantity: float) -> dict:
        """A reduce-only market order for the given quantity (NOT OKX's
        /trade/close-position endpoint, which always closes the ENTIRE
        position with no quantity control) - this preserves instance.py's
        ability to close a partial quantity exactly like Binance's
        reduceOnly market order does. Reduce-only orders are never subject
        to the 54030 position-limit check (per the owner's hard
        requirement), so this is always safe to call even on a blocked
        instrument.

        Same ambiguous-response handling as market_order above (2026-09-14
        fix), applied here too since the exact same risk exists for closes."""
        sz = await self._coin_qty_to_contracts(symbol, quantity)
        mgn_mode, _ = self._leverage_state.get(symbol, ("isolated", 1))
        client_order_id = self._new_client_order_id()
        try:
            data = await self._request(
                "POST", "/api/v5/trade/order",
                body={"instId": symbol, "tdMode": mgn_mode, "side": side.lower(),
                      "ordType": "market", "sz": sz, "reduceOnly": True,
                      "clOrdId": client_order_id},
            )
            return self._normalize_order(data["data"][0])
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
            ambiguous_exc.client_order_id = client_order_id
            raise ambiguous_exc

    # ---------------------------------------------------------------- native stop/TP (algo) orders
    async def _place_conditional(self, symbol: str, side: str, trigger_price: float,
                                  trigger_key: str, trigger_px_type: str = "mark") -> dict:
        """trigger_key is 'sl' or 'tp'. ordPx=-1 means 'execute as market
        once triggered', OKX's equivalent of Binance's *_MARKET types.

        BASE V3 MUST-HAVE FIX 1 (2026-09-28): OKX requires either `sz` or
        `closeFraction` on a conditional order - the previous version sent
        neither, which OKX's documentation says is rejected (so no OKX trade
        would ever have received its stop). Now sends closeFraction="1"
        (close the WHOLE position when triggered, reduce-only) - the direct
        equivalent of Binance's closePosition=true. MUST BE VERIFIED ON OKX
        DEMO before live use.

        BASE V3 MUST-HAVE FIX 2 (2026-09-28): lost-reply recovery. If the
        placement raises for ANY reason (network timeout, or an error after
        the adapter's own retries - e.g. a retry rejected as a duplicate of
        an order that actually went through), the bot now asks OKX whether
        an algo order with this SAME algoClOrdId exists before giving up. If
        it does, that real order is returned instead of raising - so the
        caller neither places a duplicate nor believes the position has no
        stop. Mirrors market_order's clOrdId recovery above."""
        client_algo_id = self._new_client_order_id()
        mgn_mode, _ = self._leverage_state.get(symbol, ("isolated", 1))
        body = {
            "instId": symbol, "tdMode": mgn_mode, "side": side.lower(),
            "ordType": "conditional", "algoClOrdId": client_algo_id,
            "reduceOnly": True,
            "closeFraction": "1",
            f"{trigger_key}TriggerPx": str(trigger_price),
            f"{trigger_key}OrdPx": "-1",
            f"{trigger_key}TriggerPxType": trigger_px_type,
        }
        order_type_hint = "STOP_MARKET" if trigger_key == "sl" else "TAKE_PROFIT_MARKET"
        try:
            data = await self._request("POST", "/api/v5/trade/order-algo", body=body)
        except Exception as place_exc:
            log.warning("Stop/conditional order for %s: placement raised (%s) - checking OKX for "
                        "algoClOrdId %s before concluding it failed.", symbol, place_exc, client_algo_id)
            try:
                existing = await self.get_algo_order_by_client_id(symbol, client_algo_id)
            except Exception as query_exc:
                log.warning("Could not confirm algoClOrdId %s either (%s).", client_algo_id, query_exc)
                existing = None
            if existing is not None:
                log.warning("Confirmed: the stop for %s actually went through (algoId %s) - using the "
                            "real order, not placing another.", symbol, existing.get("algoId"))
                self._algo_inst_cache[existing["algoId"]] = symbol
                if order_type_hint and not existing.get("orderType"):
                    existing["orderType"] = order_type_hint
                return existing
            try:
                place_exc.client_order_id = client_algo_id
            except Exception:
                pass
            raise
        raw = data["data"][0]
        algo_id = raw["algoId"]
        self._algo_inst_cache[algo_id] = symbol
        raw.setdefault("algoClOrdId", client_algo_id)
        return self._normalize_algo_order(raw, order_type_hint=order_type_hint)

    async def get_algo_order_by_client_id(self, symbol: str, client_algo_id: str) -> dict | None:
        """GET /api/v5/trade/order-algo by algoClOrdId. Returns the
        normalized order, or None if OKX says it doesn't exist."""
        try:
            data = await self._request("GET", "/api/v5/trade/order-algo",
                                       params={"algoClOrdId": client_algo_id, "instId": symbol})
        except OKXAPIError as e:
            if e.code in ORDER_ALREADY_GONE_CODES:
                return None
            raise
        rows = data.get("data") or []
        if not rows:
            return None
        return self._normalize_algo_order(rows[0])

    async def stop_market_order(self, symbol: str, side: str, trigger_price: float,
                                 skip_throttle: bool = False, trigger_px_type: str = "mark") -> dict:
        return await self._place_conditional(symbol, side, trigger_price, "sl", trigger_px_type)

    async def take_profit_market_order(self, symbol: str, side: str, trigger_price: float,
                                        skip_throttle: bool = False, trigger_px_type: str = "mark") -> dict:
        return await self._place_conditional(symbol, side, trigger_price, "tp", trigger_px_type)

    async def get_open_algo_orders(self, symbol: str) -> list[dict]:
        data = await self._request(
            "GET", "/api/v5/trade/orders-algo-pending",
            params={"instId": symbol, "ordType": "conditional"},
        )
        normalized = []
        for row in data["data"]:
            self._algo_inst_cache[row["algoId"]] = symbol
            normalized.append(self._normalize_algo_order(row))
        return normalized

    async def get_algo_order(self, algo_id) -> dict:
        inst_id = self._algo_inst_cache.get(algo_id)
        params = {"algoId": algo_id}
        if inst_id:
            params["instId"] = inst_id
        data = await self._request("GET", "/api/v5/trade/order-algo", params=params)
        return self._normalize_algo_order(data["data"][0])

    async def cancel_algo_order(self, algo_id):
        """See class docstring point 3 - OKX requires instId alongside
        algoId, unlike Binance. Raises if this adapter never saw this
        algo_id (e.g. a fresh process restart before a get_open_algo_orders
        call repopulated the cache) rather than silently sending a
        malformed request."""
        inst_id = self._algo_inst_cache.get(algo_id)
        if inst_id is None:
            raise ValueError(
                f"cancel_algo_order: no cached instId for algoId {algo_id} - call "
                f"get_open_algo_orders(symbol) first to repopulate the cache."
            )
        return await self._request(
            "POST", "/api/v5/trade/cancel-algos",
            body={"algosData": [{"algoId": algo_id, "instId": inst_id}]},
        )

    # ---------------------------------------------------------------- regular orders / cleanup
    async def get_open_orders(self, symbol: str) -> list[dict]:
        data = await self._request("GET", "/api/v5/trade/orders-pending", params={"instId": symbol})
        return data["data"]

    async def cancel_all_open_orders(self, symbol: str):
        """Symbol-wide cancel of both regular AND algo orders, matching
        BinanceFuturesClient.cancel_all_open_orders's scope exactly."""
        errors = []
        try:
            regular = await self.get_open_orders(symbol)
            if regular:
                await self._request(
                    "POST", "/api/v5/trade/cancel-batch-orders",
                    body=[{"instId": symbol, "ordId": o["ordId"]} for o in regular],
                )
        except OKXAPIError as e:
            errors.append(f"regular orders: {e}")
        try:
            algos = await self.get_open_algo_orders(symbol)
            if algos:
                await self._request(
                    "POST", "/api/v5/trade/cancel-algos",
                    body={"algosData": [{"algoId": a["algoId"], "instId": symbol} for a in algos]},
                )
        except OKXAPIError as e:
            errors.append(f"algo orders: {e}")
        if errors:
            raise OKXAPIError(0, {"msg": "; ".join(errors)})

    # ---------------------------------------------------------------- trade history
    async def get_user_trades(self, symbol: str, order_id=None, limit: int = 50) -> list[dict]:
        params = {"instType": "SWAP", "instId": symbol, "limit": limit}
        if order_id is not None:
            params["ordId"] = order_id
        data = await self._request("GET", "/api/v5/trade/fills", params=params)
        normalized = []
        for row in data["data"]:
            r = dict(row)
            # OKX's "fee" is a negative deduction (e.g. "-0.0012"); Binance's
            # "commission" is the positive amount charged - instance.py sums
            # commission directly as a cost, so this must be a positive
            # magnitude, not a raw negative pass-through.
            r["commission"] = abs(float(row.get("fee", 0) or 0))
            r["orderId"] = row.get("ordId")
            # BUG FIX (2026-09-15, found while adding entry-price recovery):
            # instance.py's fill-price recovery (both the pre-existing close-
            # side version and the newly-added entry-side version) reads
            # trade["price"] - Binance's real field name. OKX's raw fill rows
            # use "fillPx" instead; without this mapping, that lookup would
            # silently find nothing and fall through to a LESS accurate
            # fallback (mark price) even though the real fill price was
            # available all along, just under a different key.
            r["price"] = row.get("fillPx")
            normalized.append(r)
        return normalized
