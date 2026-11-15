"""Arbitrage scanner - STEP 1, READ ONLY (owner 2026-11-15).

It only READS public prices (no keys, no orders). Every request goes through the shared API guard
(app/api_guard.py), so the scanner can never push the Binance / OKX request limits and risk a block together with
the trading pairs. It fills three boards: spatial (Binance vs OKX), funding rate, triangular.
The gap maths is the same as the standalone arbitrage bot (spatial.py / funding.py / triangular.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

import aiohttp

from app import api_guard
from app.arb.dec import D, lcm_step
from app.arb.instruments import Instr

log = logging.getLogger("arb.scanner")

BINANCE_URLS = {
    ("futures", False): "https://fapi.binance.com",
    ("futures", True): "https://testnet.binancefuture.com",
    ("spot", False): "https://api.binance.com",
    ("spot", True): "https://testnet.binance.vision",
}
OKX_URL = "https://www.okx.com"
BOARD_ROWS = 10
NEVER = float("-inf")      # 'not read yet' (a clock that has just started must not look like 'read recently')


class ScanError(Exception):
    pass


def iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def symbol_of(ex: str, market: str, base: str) -> str:
    if ex == "binance":
        return base + "USDT"
    return "%s-USDT%s" % (base, "-SWAP" if market == "futures" else "")


# ------------------------------------------------------------------ pure maths (tested without a network)
def usdt_volumes(instr: dict, volumes: dict) -> dict:
    """{base: 24h USDT volume} for the USDT-quoted instruments of one (exchange, market)."""
    return {i["base"]: volumes.get(sym, 0.0) for sym, i in instr.items() if i["quote"] == "USDT"}


def pick_top(ranked: list, pairs: dict, top_n: int) -> list:
    inc = [b.upper() for b in pairs.get("include", [])]
    exc = {b.upper() for b in pairs.get("exclude", [])}
    minv = float(pairs.get("min_volume_usdt_24h", 0))
    top = [b for b, v in ranked if v >= minv and b not in exc][: int(top_n)]
    for b in inc:
        if b not in top and b not in exc and any(b == r[0] for r in ranked):
            top.append(b)
    return top


def spatial_edges(bases, books, market, a, b, fee_a, fee_b, slip_pct):
    """Both directions for every coin. net = gap - fees - price slippage (same as the standalone bot)."""
    rounds = 2 if market == "futures" else 1          # futures: open + close, spot: one swap
    nfills = 4 if market == "futures" else 2
    out = []
    for base in bases:
        bA = books.get((a, market), {}).get(symbol_of(a, market, base))
        bB = books.get((b, market), {}).get(symbol_of(b, market, base))
        if not (bA and bB):
            continue
        for buy, sell, bb, sb, fb, fs in ((a, b, bA, bB, fee_a, fee_b), (b, a, bB, bA, fee_b, fee_a)):
            gap = (sb["bid"] - bb["ask"]) / bb["ask"] * 100
            fees = rounds * (fb + fs) * 100
            out.append({"base": base, "market": market, "buy": buy, "sell": sell, "ask": bb["ask"],
                        "bid": sb["bid"], "aq": bb["aq"], "bq": sb["bq"], "gap": gap,
                        "net": gap - fees - nfills * slip_pct})
    out.sort(key=lambda e: e["net"], reverse=True)
    return out


def funding_edges(exchanges, bases_by_ex, funding, books, fees, expected_periods):
    out = []
    for ex in exchanges:
        fund = funding.get(ex, {})
        for base in bases_by_ex.get(ex, []):
            f = fund.get(base)
            sp = books.get((ex, "spot"), {}).get(symbol_of(ex, "spot", base))
            pf = books.get((ex, "futures"), {}).get(symbol_of(ex, "futures", base))
            if not (f and sp and pf):
                continue
            rate_pct = f["rate"] * 100
            fees_rt = 2 * (fees[(ex, "spot")] + fees[(ex, "futures")]) * 100
            basis = (pf["bid"] - sp["ask"]) / sp["ask"] * 100
            periods = fees_rt / rate_pct if rate_pct > 0 else None
            out.append({"base": base, "ex": ex, "rate": rate_pct, "basis": basis, "fees": fees_rt,
                        "periods": periods, "net": rate_pct * float(expected_periods) - fees_rt})
    out.sort(key=lambda e: e["rate"], reverse=True)
    return out


def build_cycles(ex, instr, bases, bridges):
    by = {(i["base"], i["quote"]): sym for sym, i in instr.items()}
    out = []
    for x in bases:
        if (x, "USDT") not in by:
            continue
        for b in bridges:
            if b != x and (b, "USDT") in by and (x, b) in by:
                out.append((ex, x, b, by[(x, "USDT")], by[(x, b)], by[(b, "USDT")]))
    return out


def triangular_edges(cycles, books, fees, slip_pct):
    out = []
    for ex, x, b, s_xu, s_xb, s_bu in cycles:
        bk = books.get((ex, "spot"), {})
        xu, xb, bu = bk.get(s_xu), bk.get(s_xb), bk.get(s_bu)
        if not (xu and xb and bu):
            continue
        f = (1 - fees[(ex, "spot")]) ** 3
        fwd = bu["bid"] * xb["bid"] / xu["ask"]
        rev = xu["bid"] / (bu["ask"] * xb["ask"])
        for direction, ratio in (("fwd", fwd), ("rev", rev)):
            out.append({"base": x, "bridge": b, "ex": ex, "dir": direction,
                        "net": (ratio * f - 1) * 100 - 3 * slip_pct})
    out.sort(key=lambda e: e["net"], reverse=True)
    return out


# ------------------------------------------------------------------ the scanner
class ArbScanner:
    def __init__(self, get_settings, session_factory=None):
        self.get_settings = get_settings
        self._session_factory = session_factory or self._default_session
        self._session = None
        self._task: asyncio.Task | None = None
        self.instr: dict = {}        # (ex, market) -> {symbol: {"base","quote","unit"}}
        self.rules: dict = {}        # (ex, market) -> {symbol: Instr}  (lot size, tick, min size: used by mock trading)
        self.stamp = 0               # bumped whenever rules / coin lists are refreshed
        self.loop = None             # the hub's event loop (the live worker thread asks the API guard through it)
        self.on_scan = None          # callable(settings, scanner): the mock trader hooks in here
        self.extra_funding = lambda: set()   # coins with an open funding trade: their rate is always read
        self.books: dict = {}        # (ex, market) -> {symbol: {bid, ask, bq, aq}}
        self.books_at: dict = {}     # (ex, market) -> time.monotonic() of the last good price list
        self.volumes: dict = {}      # (ex, market) -> {symbol: 24h USDT volume}
        self.funding: dict = {}      # ex -> {base: {rate, next_time_ms}}
        self.universe: dict = {"spatial": {}, "funding": {}, "triangular": {}}
        self.cycles: list = []
        self._last_refresh = NEVER
        self._last_funding = NEVER
        self._sig = None
        self.board: dict = {"spatial": [], "funding": [], "triangular": []}
        self.status: dict = {"running": False, "last_scan_utc": None, "scan_ms": None, "loops": 0,
                             "last_error": None, "notes": []}

    # ---------------------------------------------------------------- run control
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self):
        if self.running:
            return
        self._task = asyncio.create_task(self._run())
        self.status["running"] = True

    async def stop(self):
        t, self._task = self._task, None
        self.status["running"] = False
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _default_session(self):
        timeout = aiohttp.ClientTimeout(total=float(self.get_settings().get("timeout_seconds", 10)))
        return aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "strategy-hub-arb-scan/1.0"})

    async def _run(self):
        try:
            while True:
                t0 = time.monotonic()
                s = self.get_settings()
                try:
                    await self.scan_once(s)
                    self.status["last_error"] = None
                except asyncio.CancelledError:
                    raise
                except Exception as e:                         # a bad scan never kills the loop
                    self.status["last_error"] = "%s: %s" % (type(e).__name__, e)
                    log.warning("arb scan failed: %s", self.status["last_error"])
                self.status["scan_ms"] = int((time.monotonic() - t0) * 1000)
                await asyncio.sleep(max(2.0, float(s.get("loop_seconds", 3)) - (time.monotonic() - t0)))
        except asyncio.CancelledError:
            raise

    # ---------------------------------------------------------------- one scan
    def _exchanges(self, s):
        return [n for n in ("binance", "okx") if s["exchanges"][n]["enabled"]]

    def _fees(self, s):
        fb = s["fallback_fees_pct"]
        return {(ex, m): float(fb[ex][m]) / 100.0 for ex in ("binance", "okx") for m in ("futures", "spot")}

    @staticmethod
    def _signature(s):
        m = s["modules"]
        return repr((s["pairs"], s["exchanges"], m["spatial"]["enabled"], m["spatial"]["markets"],
                     m["funding"]["enabled"], m["funding"]["top_n"], m["triangular"]["enabled"],
                     m["triangular"].get("confirm_enable"), m["triangular"]["top_n"],
                     m["triangular"]["bridge_coins"]))

    def _needed(self, s):
        """Which (exchange, market) price books are needed by the switched-on modules."""
        exs, m, need = self._exchanges(s), s["modules"], set()
        sp, fu, tr = m["spatial"], m["funding"], m["triangular"]
        if sp["enabled"] and len(exs) == 2:
            for mk in sp["markets"]:
                need |= {(ex, mk) for ex in exs}
        if fu["enabled"]:
            for ex in exs:
                need |= {(ex, "spot"), (ex, "futures")}
        if tr["enabled"] and tr.get("confirm_enable") is True:
            need |= {(ex, "spot") for ex in exs}
        return need

    async def scan_once(self, s):
        need = self._needed(s)
        notes = []
        m = s["modules"]
        if m["triangular"]["enabled"] and m["triangular"].get("confirm_enable") is not True:
            notes.append("Triangular is LOCKED: tick 'confirm_enable' to see its board.")
        if not need:
            self.board = {"spatial": [], "funding": [], "triangular": []}
            self.status.update({"last_scan_utc": iso(), "notes": notes + ["No module is switched on."]})
            return
        now = time.monotonic()
        refresh_s = float(s["pairs"]["refresh_minutes"]) * 60
        sig = self._signature(s)
        if sig != self._sig:                      # the owner changed Top N, coins, switches... -> apply at once
            self._sig = sig
            self._last_refresh = NEVER
            self._last_funding = NEVER
        if now - self._last_refresh >= refresh_s or any(k not in self.instr for k in need):
            await self._refresh(s, need)
            self._last_refresh = now
        await asyncio.gather(*[self._tickers(s, ex, mk) for ex, mk in sorted(need)])
        if m["funding"]["enabled"] and (now - self._last_funding >= float(m["funding"]["refresh_seconds"])):
            await self._load_funding(s)
            self._last_funding = now
        self._compute(s, notes)
        if self.on_scan is not None:
            self.loop = asyncio.get_running_loop()
            try:
                # the engines run in a worker thread: a live order cycle takes seconds and must not freeze the hub
                await asyncio.to_thread(self.on_scan, s, self)
            except Exception:
                log.exception("arbitrage mock trading step failed (it will try again next scan)")
        self.status["loops"] += 1
        self.status["last_scan_utc"] = iso()

    # ---------------------------------------------------------------- compute boards
    def _compute(self, s, notes):
        exs, m, fees = self._exchanges(s), s["modules"], self._fees(s)
        board = {"spatial": [], "funding": [], "triangular": []}
        if m["spatial"]["enabled"] and len(exs) == 2:
            rows = []
            for mk in m["spatial"]["markets"]:
                rows += spatial_edges(self.universe["spatial"].get(mk, []), self.books, mk, exs[0], exs[1],
                                      fees[(exs[0], mk)], fees[(exs[1], mk)], float(m["spatial"]["slippage_pct"]))
            rows.sort(key=lambda e: e["net"], reverse=True)
            board["spatial"] = rows[:BOARD_ROWS]
        if m["funding"]["enabled"]:
            board["funding"] = funding_edges(exs, self.universe["funding"], self.funding, self.books, fees,
                                             m["funding"]["expected_periods"])[:BOARD_ROWS]
        if m["triangular"]["enabled"] and m["triangular"].get("confirm_enable") is True:
            board["triangular"] = triangular_edges(self.cycles, self.books, fees,
                                                   float(m["triangular"]["slippage_pct"]))[:BOARD_ROWS]
        self.board = board
        if self._any_paused():
            notes.append("Requests are PAUSED by the API guard (rate limit). The board will resume by itself.")
        self.status["notes"] = notes

    # ---------------------------------------------------------------- refresh: rules, volumes, universes
    async def _refresh(self, s, need):
        for ex, mk in sorted(need):
            ins, vols = await self._instruments_and_volumes(s, ex, mk)
            self.instr[(ex, mk)] = ins
            self.volumes[(ex, mk)] = vols
        exs, m, pairs = self._exchanges(s), s["modules"], s["pairs"]
        if m["spatial"]["enabled"] and len(exs) == 2:
            for mk in m["spatial"]["markets"]:
                va = usdt_volumes(self.instr[(exs[0], mk)], self.volumes[(exs[0], mk)])
                vb = usdt_volumes(self.instr[(exs[1], mk)], self.volumes[(exs[1], mk)])
                common = sorted(((b, min(va[b], vb[b])) for b in va if b in vb), key=lambda t: t[1], reverse=True)
                self.universe["spatial"][mk] = pick_top(common, pairs, pairs["top_n"])
        if m["funding"]["enabled"]:
            for ex in exs:
                vs = usdt_volumes(self.instr[(ex, "spot")], self.volumes[(ex, "spot")])
                vf = usdt_volumes(self.instr[(ex, "futures")], self.volumes[(ex, "futures")])
                both = sorted(((b, vf[b]) for b in vf if b in vs), key=lambda t: t[1], reverse=True)
                self.universe["funding"][ex] = pick_top(both, pairs, m["funding"]["top_n"])
        self.cycles = []
        if m["triangular"]["enabled"] and m["triangular"].get("confirm_enable") is True:
            for ex in exs:
                v = usdt_volumes(self.instr[(ex, "spot")], self.volumes[(ex, "spot")])
                ranked = sorted(v.items(), key=lambda t: t[1], reverse=True)
                bases = pick_top(ranked, pairs, m["triangular"]["top_n"])
                self.universe["triangular"][ex] = bases
                self.cycles += build_cycles(ex, self.instr[(ex, "spot")], bases, m["triangular"]["bridge_coins"])
        self.stamp += 1

    # ---------------------------------------------------------------- exchange calls (all guarded)
    @staticmethod
    def _guard_for(ex: str, url: str):
        """Futures use the same budget as the hub's futures pairs. Spot and testnet hosts have their own limits
        at Binance, so they get their own guards (the hub's futures budget is never used up by spot scans)."""
        if ex == "okx":
            return api_guard.okx_guard(False)
        host = urlparse(url).hostname or ""
        if "testnet" in host:
            return api_guard.get_guard("binance-test", 1200)
        if host == "api.binance.com":
            return api_guard.get_guard("binance-spot", 6000)
        return api_guard.get_guard("binance")

    async def _fetch(self, ex: str, url: str, params: dict | None, weight: int):
        if self._session is None or self._session.closed:
            self._session = await self._session_factory()
        guard = self._guard_for(ex, url)
        async with guard.normal_slot():
            await guard.acquire(weight, urgent=False)
            try:
                async with self._session.get(url, params=params) as resp:
                    try:
                        guard.note_reported(resp.headers.get("X-MBX-USED-WEIGHT-1M"))
                    except Exception:
                        pass
                    data = await resp.json(content_type=None)
                    if resp.status in (418, 429):
                        default = api_guard.DEFAULT_418_PAUSE if resp.status == 418 else api_guard.DEFAULT_429_PAUSE
                        secs = api_guard.retry_after_seconds(resp.headers, default)
                        if resp.status == 418 and resp.headers.get("Retry-After") is None:
                            secs = max(secs, api_guard.ban_seconds_from_message(data))
                        guard.trigger_pause(secs, resp.status, "(arbitrage scan)")
                        raise ScanError("%s rate limit (HTTP %s)" % (ex, resp.status))
                    if resp.status >= 400:
                        raise ScanError("%s HTTP %s from %s" % (ex, resp.status, url.split("?")[0]))
                    if ex == "okx":
                        code = data.get("code") if isinstance(data, dict) else None
                        if code in ("50011", "50061"):
                            guard.trigger_pause(5.0, 200, "(arbitrage scan, OKX code %s)" % code)
                            raise ScanError("okx rate limit (code %s)" % code)
                        if code != "0" or not isinstance(data.get("data"), list):
                            raise ScanError("okx error %s: %s" % (code, (data or {}).get("msg")))
                        return data["data"]
                    guard.note_success()
                    return data
            except asyncio.TimeoutError:
                raise ScanError("%s timeout" % ex)
            except aiohttp.ClientError as e:
                raise ScanError("%s network error: %s" % (ex, e))

    def _base_url(self, s, ex, market):
        testnet = bool(s["exchanges"][ex]["testnet"])
        return BINANCE_URLS[(market, testnet)] if ex == "binance" else OKX_URL

    async def _instruments_and_volumes(self, s, ex, market):
        base = self._base_url(s, ex, market)
        ins, vols, rules = {}, {}, {}
        if ex == "binance":
            pre = "/fapi/v1" if market == "futures" else "/api/v3"
            info = await self._fetch(ex, base + pre + "/exchangeInfo", None, 10 if market == "futures" else 20)
            for r in (info.get("symbols") if isinstance(info, dict) else None) or []:
                if r.get("status") != "TRADING":
                    continue
                if market == "futures" and r.get("contractType") != "PERPETUAL":
                    continue
                if market == "spot" and r.get("isSpotTradingAllowed") is False:
                    continue
                ins[r["symbol"]] = {"base": r["baseAsset"], "quote": r["quoteAsset"], "unit": 1.0}
                try:
                    rules[r["symbol"]] = self._binance_rule(r, market)
                except Exception:
                    ins.pop(r["symbol"], None)
            v = await self._fetch(ex, base + pre + "/ticker/24hr", None, 40 if market == "futures" else 80)
            for r in v if isinstance(v, list) else []:
                try:
                    vols[r["symbol"]] = float(r.get("quoteVolume") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
        else:
            inst_type = "SWAP" if market == "futures" else "SPOT"
            rows = await self._fetch(ex, base + "/api/v5/public/instruments", {"instType": inst_type}, 1)
            for r in rows:
                if r.get("state") not in (None, "", "live"):
                    continue
                try:
                    if market == "futures":
                        if r.get("settleCcy") != "USDT" or r.get("ctType") not in (None, "", "linear"):
                            continue
                        b, q = r["instFamily"].split("-")[:2]
                        unit = float(Decimal(r["ctVal"]) * Decimal(r.get("ctMult") or "1"))
                    else:
                        b, q, unit = r["baseCcy"], r["quoteCcy"], 1.0
                    ins[r["instId"]] = {"base": b, "quote": q, "unit": unit}
                    u = D(r["ctVal"]) * D(r.get("ctMult") or "1") if market == "futures" else D(1)
                    rules[r["instId"]] = Instr("okx", market, r["instId"], b, q, D(r["lotSz"]) * u,
                                               D(r["minSz"]) * u, D(r["tickSz"]), 0, u)
                except Exception:
                    ins.pop(r["instId"], None)
                    continue
            tk = await self._fetch(ex, base + "/api/v5/market/tickers", {"instType": inst_type}, 1)
            for r in tk:
                try:
                    v = float(r.get("volCcy24h") or 0)
                    if market == "futures":
                        v *= float(r.get("last") or 0)
                    vols[r["instId"]] = v
                except (KeyError, TypeError, ValueError):
                    continue
        if not ins:
            raise ScanError("%s %s: no tradable instruments returned" % (ex, market))
        self.rules[(ex, market)] = rules
        return ins, vols

    @staticmethod
    def _binance_rule(r, market):
        f = {x.get("filterType"): x for x in r.get("filters", [])}
        lot = f.get("LOT_SIZE", {})
        step, minq = D(lot.get("stepSize", "0")), D(lot.get("minQty", "0"))
        ml = f.get("MARKET_LOT_SIZE")
        if ml:
            mstep = D(ml.get("stepSize", "0"))
            if mstep > 0 and step > 0 and mstep != step:
                step = lcm_step(step, mstep)
            elif step == 0:
                step = mstep
            minq = max(minq, D(ml.get("minQty", "0")))
        tick = D(f.get("PRICE_FILTER", {}).get("tickSize", "0"))
        mn = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        min_notional = D(mn.get("minNotional") or mn.get("notional") or "0")
        return Instr("binance", market, r["symbol"], r["baseAsset"], r["quoteAsset"], step, minq, tick,
                     min_notional, 1)

    async def _tickers(self, s, ex, market):
        base = self._base_url(s, ex, market)
        ins = self.instr.get((ex, market), {})
        out = {}
        if ex == "binance":
            pre = "/fapi/v1" if market == "futures" else "/api/v3"
            d = await self._fetch(ex, base + pre + "/ticker/bookTicker", None, 5 if market == "futures" else 4)
            for r in d if isinstance(d, list) else []:
                sym = r.get("symbol")
                if sym not in ins:
                    continue
                try:
                    bid, ask = float(r["bidPrice"]), float(r["askPrice"])
                    bq, aq = float(r.get("bidQty") or 0), float(r.get("askQty") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if bid > 0 and ask > 0 and ask >= bid:
                    out[sym] = {"bid": bid, "ask": ask, "bq": bq, "aq": aq}
        else:
            inst_type = "SWAP" if market == "futures" else "SPOT"
            for r in await self._fetch(ex, base + "/api/v5/market/tickers", {"instType": inst_type}, 1):
                i = ins.get(r.get("instId"))
                if i is None:
                    continue
                try:
                    bid, ask = float(r["bidPx"]), float(r["askPx"])
                    bq = float(r.get("bidSz") or 0) * i["unit"]
                    aq = float(r.get("askSz") or 0) * i["unit"]
                except (KeyError, TypeError, ValueError):
                    continue
                if bid > 0 and ask > 0 and ask >= bid:
                    out[r["instId"]] = {"bid": bid, "ask": ask, "bq": bq, "aq": aq}
        if out:
            self.books[(ex, market)] = out
            self.books_at[(ex, market)] = time.monotonic()

    def book_age(self, key) -> float:
        """Seconds since this price list was last refreshed (infinite = never)."""
        at = self.books_at.get(key)
        return float("inf") if at is None else time.monotonic() - at

    async def _load_funding(self, s):
        for ex in self._exchanges(s):
            bases = sorted(set(self.universe["funding"].get(ex, [])) | set(self.extra_funding()))
            base = self._base_url(s, ex, "futures")
            out = {}
            if ex == "binance":
                d = await self._fetch(ex, base + "/fapi/v1/premiumIndex", None, 10)
                by_sym = {}
                for r in d if isinstance(d, list) else []:
                    try:
                        by_sym[r["symbol"]] = {"rate": float(r["lastFundingRate"]),
                                               "next_time_ms": int(r["nextFundingTime"])}
                    except (KeyError, TypeError, ValueError):
                        continue
                for b in bases:
                    if symbol_of(ex, "futures", b) in by_sym:
                        out[b] = by_sym[symbol_of(ex, "futures", b)]
            else:
                sem = asyncio.Semaphore(4)

                async def one(b):
                    async with sem:
                        try:
                            d = (await self._fetch(ex, base + "/api/v5/public/funding-rate",
                                                   {"instId": symbol_of(ex, "futures", b)}, 1))[0]
                            return b, {"rate": float(d["fundingRate"]), "next_time_ms": int(d["fundingTime"])}
                        except ScanError:
                            raise
                        except Exception:
                            return b, None
                for b, v in await asyncio.gather(*[one(b) for b in bases]):
                    if v:
                        out[b] = v
            self.funding[ex] = out

    @staticmethod
    def _any_paused() -> bool:
        gs = (api_guard.get_guard("binance"), api_guard.get_guard("binance-spot", 6000),
              api_guard.okx_guard(False))
        return any(g.is_paused() for g in gs)

    # ---------------------------------------------------------------- what the dashboard reads
    def snapshot(self) -> dict:
        st = dict(self.status)
        st["running"] = self.running
        st["binance_weight_used"] = round(api_guard.get_guard("binance").used())
        st["paused"] = self._any_paused()
        return {"status": st, "board": self.board}
