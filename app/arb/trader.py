"""Trading driver: runs the spatial / funding / triangular engines on the scanner's prices.

Step 2: MOCK (pretend money).  Step 3: LIVE (real orders), only when every gate passes (see live_blockers()).
The engines are the ones of the standalone arbitrage bot (same maths, same hand-checked tests). They run in a WORKER
THREAD (orders take seconds); every exchange call from there goes through the shared API guard on the event loop.
The tab reads a published snapshot, so a slow order never freezes the dashboard.
"""
from __future__ import annotations

import csv
import logging
import os
import threading
import time
from collections import defaultdict

from app import api_guard
from app.arb.context import Ctx
from app.arb.funding_engine import Funding
from app.arb.hubmatch import HubPairs
from app.arb.keys import arb_keys
from app.arb.live_broker import LiveBroker, LiveRules
from app.arb.live_clients import BinanceClient, OKXClient
from app.arb.live_http import ApiError, Transport
from app.arb.mock_broker import MockBroker
from app.arb.spatial_engine import Spatial
from app.arb.store import ArbStateError, JOURNAL_FIELDS, Journal, Risk
from app.arb.triangular_engine import Triangular

log = logging.getLogger("arb.trade")
TRADE_ROWS = 40
DAYS = 14
MIN_PRICE_AGE = 20.0        # seconds: prices older than this (or 5 scan intervals) are never traded on
LIVE_RETRY_SECONDS = 60.0
FEES_REFRESH_SECONDS = 6 * 3600.0
ALERT_REPEAT_SECONDS = 600.0
MOCK_MODES = ("mock", "live-mock")


class LiveNotReady(Exception):
    pass


def needed_clients(module, cfg, exs):
    """[(exchange, market)] a module needs for live trading."""
    if module == "spatial":
        return [(e, m) for e in exs for m in cfg["markets"]]
    if module == "funding":
        return [(e, m) for e in exs for m in ("spot", "futures")]
    return [(e, "spot") for e in exs]


def key_slot(ex, market):
    return "okx" if ex == "okx" else ("binance_futures" if market == "futures" else "binance_spot")


def live_blockers(s, module, cfg, have_key):
    """Plain-word reasons why a module may NOT trade live yet. Empty list = every setting gate passed."""
    r, out = s["risk"], []
    if r.get("max_size_per_trade_usdt") is None:
        out.append("fill in 'Max size per trade' (0 = off)")
    if r.get("daily_loss_limit_usdt") is None:
        out.append("fill in 'Stop after daily loss' (0 = off)")
    exs = [n for n in ("binance", "okx") if s["exchanges"][n]["enabled"]]
    need = needed_clients(module, cfg, exs)
    if any(m == "futures" for _, m in need) and not (r.get("margin_cap_usdt") or 0) > 0:
        out.append("fill in the 'Margin cap' (most margin per exchange)")
    if module == "spatial" and len(exs) < 2:
        out.append("spatial needs both exchanges switched on")
    missing = sorted({key_slot(e, m) for e, m in need if not have_key(key_slot(e, m))})
    for slot in missing:
        out.append("enter the %s API key" % {"binance_futures": "Binance futures", "binance_spot": "Binance spot",
                                              "okx": "OKX"}[slot])
    return out


class ArbTrader:
    def __init__(self, folder=None, keys=None, hub=None):
        self._folder = folder
        self.keys = keys or arb_keys
        self.hub = hub or HubPairs()
        self.lock = threading.RLock()
        self.loop = None
        self.ctx = self.risk = self.journal = None
        self.engines: dict = {}        # (module, market, mode) -> {"eng", "sig", "active"}
        self.problems: dict = {}       # (module, market, mode) -> text
        self.live_groups: dict = {}    # module -> {"sig", "broker", "clients"}
        self.live_fail: dict = {}      # module -> (sig, monotonic time, reason)
        self.live_fees: dict = {}      # (ex, market) -> {"maker","taker"} from the real account
        self._fees_at = float("-inf")
        self._alerted: dict = {}
        self._csv_cache = (None, [])
        self._snap = None
        self.tri_unlocked = False       # typed-YES second lock for LIVE triangular; forgotten at every restart

    # ---------------------------------------------------------------- setup
    @property
    def folder(self) -> str:
        if self._folder is None:
            from app.arb.settings import ARB_DIR
            return str(ARB_DIR)
        return str(self._folder)

    def set_folder(self, folder):
        with self.lock:
            self._folder = folder
            self.ctx = self.risk = self.journal = None
            self.engines.clear()
            self.problems.clear()
            self.live_groups.clear()
            self.live_fail.clear()
            self._csv_cache = (None, [])
            self._snap = None

    def _ensure(self, s):
        if self.ctx is not None:
            return
        os.makedirs(self.folder, exist_ok=True)
        self.risk = Risk(s["risk"], self.folder, log)
        self.risk.alert = self._alert
        self.journal = Journal(self.folder)
        self.ctx = Ctx(dict(s, data_folder=self.folder), log, self.risk, self.journal)
        self.ctx.alert = self._alert
        self.ctx.trade_event = self._trade_event
        self.journal.on_add = self._row_event

    def _trade_event(self, kind, mode, text):
        """Telegram 'trade opened / closed'. Setting alerts.trades: off | live (default) | all (also mock + shadow)."""
        want = ((self.ctx.s.get("alerts") or {}).get("trades", "live")) if self.ctx else "off"
        if want == "off" or (want == "live" and mode != "live"):
            return
        label = {"live": "LIVE", "mock": "MOCK", "live-mock": "SHADOW"}.get(mode, mode)
        self._alert("ARBITRAGE %s %s" % (label, text))

    def _row_event(self, row):
        net = self._f(row.get("net_pnl_usdt"))
        self._trade_event("close", row.get("mode", ""), "closed %s/%s %s: net %+.4f USDT, fees %.4f (%s)"
                          % (row.get("module", ""), row.get("market", ""), row.get("base", ""), net,
                             self._f(row.get("fees_usdt")), row.get("reason", "")))

    def _alert(self, text: str):
        """Telegram from the worker thread (the notifier lives on the event loop). Same text is not repeated for 10 min."""
        now = time.monotonic()
        if now - self._alerted.get(text, float("-inf")) < ALERT_REPEAT_SECONDS:
            return
        self._alerted[text] = now
        if len(self._alerted) > 200:
            self._alerted = {k: v for k, v in self._alerted.items() if now - v < ALERT_REPEAT_SECONDS}
        if self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(api_guard.send_alert, text, "Arbitrage")
            except RuntimeError:
                pass

    def funding_bases(self) -> set:
        """Coins with an open funding trade: the scanner keeps reading their rate even if they leave the Top N."""
        out = set()
        for rec in list(self.engines.values()):            # called from the event loop while the worker thread runs
            if rec["eng"].module == "funding":
                out |= {p["base"] for p in list(rec["eng"].pos)}
        return out

    def kill_file(self, s=None) -> str:
        kf = ((s or {}).get("risk") or {}).get("kill_switch_file", "STOP.txt") or ""
        return os.path.join(self.folder, kf) if kf else ""

    # ---------------------------------------------------------------- which engines should run
    @staticmethod
    def wanted(s) -> list:
        """[(module, market, cfg, mode)] where mode is mock | live | live-mock (the shadow of a live module)."""
        m, out = s["modules"], []
        exs = [n for n in ("binance", "okx") if s["exchanges"][n]["enabled"]]
        sp, fu, tr = m["spatial"], m["funding"], m["triangular"]
        mods = []
        if sp["enabled"] and len(exs) == 2:
            mods += [("spatial", mk, sp) for mk in sp["markets"]]
        if fu["enabled"]:
            mods.append(("funding", "funding", fu))
        if tr["enabled"] and tr.get("confirm_enable") is True:
            mods.append(("triangular", "spot", tr))
        for mod, mk, cfg in mods:
            if cfg["mode"] == "live":
                out.append((mod, mk, cfg, "live"))
                if s["live_mock"]["enabled"]:
                    out.append((mod, mk, cfg, "live-mock"))
            else:
                out.append((mod, mk, cfg, "mock"))
        return out

    # ---------------------------------------------------------------- live set-up (worker thread)
    def _have_key(self, slot):
        try:
            return self.keys.get(slot) is not None
        except Exception:
            return False

    def _make_client(self, s, ex, market, tp):
        testnet = s["exchanges"][ex]["testnet"]
        timeout = float(s.get("timeout_seconds", 10))
        slot = key_slot(ex, market)
        k = self.keys.get(slot)
        if k is None:
            raise LiveNotReady("the %s API key is not entered" % slot)
        if ex == "binance":
            return BinanceClient(market, testnet, k["key"], k["secret"], tp, timeout)
        return OKXClient(market, testnet, k["key"], k["secret"], k["passphrase"], tp, timeout)

    def _live_group(self, s, module, cfg, exs):
        """The shared live broker of one module. Raises LiveNotReady with a plain reason."""
        if self.loop is None:
            raise LiveNotReady("not ready yet (event loop missing)")
        blockers = live_blockers(s, module, cfg, self._have_key)
        if module == "triangular" and not self.tri_unlocked:
            blockers.append("triangular is LOCKED: type YES in the tab to unlock it (needed again after every restart)")
        if blockers:
            raise LiveNotReady("; ".join(blockers))
        sig = repr((module, tuple(exs), cfg.get("leverage"), s["orders"], s["risk"].get("margin_cap_usdt"),
                    [s["exchanges"][e]["testnet"] for e in exs], self.keys.fingerprint(), cfg.get("markets")))
        g = self.live_groups.get(module)
        if g and g["sig"] == sig:
            return g
        f = self.live_fail.get(module)
        if f and f[0] == sig and time.monotonic() - f[1] < LIVE_RETRY_SECONDS:
            raise LiveNotReady(f[2])
        try:
            g = self._build_group(s, module, cfg, exs, sig)
        except LiveNotReady as e:
            self.live_fail[module] = (sig, time.monotonic(), str(e))
            raise
        self.live_fail.pop(module, None)
        self.live_groups[module] = g
        return g

    def _build_group(self, s, module, cfg, exs, sig):
        tp = Transport(self.loop)
        clients = {}
        for ex, mk in needed_clients(module, cfg, exs):
            cl = self._make_client(s, ex, mk, tp)
            clients[(ex, mk)] = cl
            try:
                bal = cl.get_balances(["USDT"])
            except ApiError as e:
                raise LiveNotReady("%s: the key does not work or cannot read the balance (%s)" % (cl.tag, str(e)[:160]))
            if mk == "futures":
                try:
                    if not cl.one_way_mode():
                        raise LiveNotReady("%s is in HEDGE (long/short) position mode. Switch the account to ONE-WAY mode"
                                           % cl.tag)
                except ApiError as e:
                    raise LiveNotReady("%s: cannot read the position mode (%s)" % (cl.tag, str(e)[:160]))
            log.warning("LIVE %s: key works, USDT available %.2f", cl.tag, bal["USDT"]["available"])
            try:
                ref = "BTCUSDT" if ex == "binance" else "BTC-USDT"
                self.live_fees[(ex, mk)] = cl.get_fees(ref)
            except ApiError as e:
                log.warning("Fee lookup failed on %s (%s) - using the fee % from the settings", cl.tag, str(e)[:120])
        rules = LiveRules(skip=lambda ex, base, c=clients: self._skip(c, ex, base),
                          margin_used=self._margin_used, margin_cap_usdt=s["risk"].get("margin_cap_usdt") or 0)
        ctx = self.ctx
        broker = LiveBroker(clients, lambda ex, m: ctx.fee(ex, m), s["orders"], cfg.get("leverage", 1), log,
                            rules=rules, alert=self._alert)
        return {"sig": sig, "broker": broker, "clients": clients}

    def _skip(self, clients, ex, base):
        cl = clients.get((ex, "futures"))
        if cl is None:
            return None
        try:
            hit = self.hub.matches(ex, cl.api_key).get(base)
        except Exception:
            return "cannot check which pairs the hub trades on this account"
        return ("the hub trades %s on its account '%s' (same API key): skipped" % (base, hit)) if hit else None

    def _margin_used(self, ex) -> float:
        used = 0.0
        for rec in list(self.engines.values()):
            e = rec["eng"]
            if e.mode != "live":
                continue
            lev = max(float(e.cfg.get("leverage", 1) or 1), 1.0)
            for p in e.pos:
                for l in p["legs"]:
                    if l["market"] == "futures" and l["ex"] == ex and l["qty"] > 0:
                        used += l["qty"] * l["entry_px"] / lev
        return used

    def _refresh_fees(self):
        """Real account fees replace the fee % of the settings once they were read (every 6 h)."""
        if time.monotonic() - self._fees_at < FEES_REFRESH_SECONDS or not self.live_groups:
            return
        self._fees_at = time.monotonic()
        for g in self.live_groups.values():
            for (ex, mk), cl in g["clients"].items():
                try:
                    self.live_fees[(ex, mk)] = cl.get_fees("BTCUSDT" if ex == "binance" else "BTC-USDT")
                except ApiError as e:
                    log.warning("Fee refresh failed on %s: %s", cl.tag, str(e)[:120])

    # ---------------------------------------------------------------- engine construction
    @staticmethod
    def _signature(cfg, exs, mode, group_sig=""):
        return repr((tuple(exs), mode, group_sig, cfg.get("leverage"), cfg.get("slippage_pct"),
                     cfg.get("mock_start_usdt"), cfg.get("mock_coin_inventory_usdt")))

    def _build(self, module, market, cfg, mode, group):
        ctx = self.ctx

        def make(st):
            if mode == "live":
                return group["broker"]
            inv = cfg.get("mock_coin_inventory_usdt", 0) if (module == "spatial" and market == "spot") else 0
            return MockBroker(st.wallets, lambda ex, m: ctx.fee(ex, m), cfg.get("slippage_pct", 0),
                              cfg.get("leverage", 1), {e: cfg.get("mock_start_usdt", 0) for e in ctx.exchanges}, inv)
        if module == "spatial":
            return Spatial(market, mode, cfg, ctx, make)
        if module == "funding":
            return Funding(mode, cfg, ctx, make)
        return Triangular(mode, cfg, ctx, make)

    # ---------------------------------------------------------------- one step (worker thread, after each scan)
    def step(self, s, scanner):
        with self.lock:
            self.loop = getattr(scanner, "loop", None) or self.loop
            try:
                self._step(s, scanner)
            finally:
                self._snap = self._build_snapshot(s)

    def _step(self, s, scanner):
        self._ensure(s)
        c = self.ctx
        c.s = dict(s, data_folder=self.folder)
        self.risk.update_limits(s["risk"])
        exs = [n for n in ("binance", "okx") if s["exchanges"][n]["enabled"]]
        fb = s["fallback_fees_pct"]
        c.exchanges = exs
        c.fees = {(ex, mk): self.live_fees.get((ex, mk)) or {"maker": fb[ex][mk] / 100.0, "taker": fb[ex][mk] / 100.0}
                  for ex in ("binance", "okx") for mk in ("futures", "spot")}
        c.instr, c.books, c.funding, c.volumes = scanner.rules, scanner.books, scanner.funding, scanner.volumes
        c.universe = scanner.universe["spatial"]
        c.funding_universe = scanner.universe["funding"]
        c.tri_bases = scanner.universe["triangular"]
        c.stamp = scanner.stamp
        self._refresh_fees()
        want = {(mod, mk, mode): cfg for mod, mk, cfg, mode in self.wanted(s)}
        for rec in self.engines.values():
            rec["active"] = False
        now = c.now()
        max_age = max(MIN_PRICE_AGE, 5.0 * float(s["loop_seconds"]))
        live_blocked = set()
        for key, cfg in want.items():
            module, market, mode = key
            if mode == "live-mock" and module in live_blocked:
                continue                                       # no shadow of a live module that cannot start
            group, sig_extra = None, ""
            try:
                if mode == "live":
                    group = self._live_group(s, module, cfg, exs)
                    sig_extra = group["sig"]
                sig = self._signature(cfg, exs, mode, sig_extra)
                rec = self.engines.get(key)
                if rec is None or rec["sig"] != sig:
                    if rec is not None:
                        rec["eng"].save()
                    rec = {"eng": self._build(module, market, cfg, mode, group), "sig": sig, "active": True}
                    self.engines[key] = rec
                    log.info("%s engine ready: %s", "LIVE" if mode == "live" else "Mock", rec["eng"].name)
                rec["eng"].cfg = cfg
                rec["eng"].exit_only = False
                rec["active"] = True
                self.problems.pop(key, None)
            except LiveNotReady as e:
                self.problems[key] = "Live NOT started: %s" % e
                live_blocked.add(module)
                continue
            except ArbStateError as e:
                self.problems[key] = str(e)
                continue
            if not self._books_ready(rec["eng"], scanner, max_age):
                if mode == "live" and rec["eng"].pos:
                    self._alert("ARBITRAGE: prices are OLD while live trades are open (%s) - they are not being managed"
                                % rec["eng"].name)
                continue
            self._run(rec, key, now)
        self._resume_live(s, scanner, exs, want, max_age, now)
        # a LIVE module that was switched off while trades are open keeps managing its exits (never entries)
        for key, rec in self.engines.items():
            if rec["active"] or key[2] != "live" or not rec["eng"].pos:
                continue
            if not self._books_ready(rec["eng"], scanner, max_age):
                continue
            rec["eng"].exit_only = True
            rec["active"] = True
            self._run(rec, key, now)

    def _candidates(self, s):
        m, out = s["modules"], []
        for mk in m["spatial"]["markets"]:
            out.append(("spatial", mk, m["spatial"]))
        out += [("funding", "funding", m["funding"]), ("triangular", "spot", m["triangular"])]
        return out

    def _resume_live(self, s, scanner, exs, want, max_age, now):
        """After a restart, live trades saved in a state file must be managed again even if their module is now
        switched off: build that live engine for EXITS ONLY (never new entries)."""
        for module, market, cfg in self._candidates(s):
            key = (module, market, "live")
            if key in want or key in self.engines or cfg["mode"] != "live":
                continue
            path = os.path.join(self.folder, "state_%s_%s_live.json" % (module, market))
            if not os.path.exists(path):
                continue
            try:
                from app.arb.store import read_json
                saved = read_json(path, "state file") or {}
                if not saved.get("positions"):
                    continue
                group = self._live_group(s, module, cfg, exs)
                eng = self._build(module, market, cfg, "live", group)
            except (LiveNotReady, ArbStateError) as e:
                self.problems[key] = "Live trades are open but cannot be managed: %s" % e
                self._alert("ARBITRAGE: live trades are open (%s) but cannot be managed: %s" % (module, e))
                continue
            self.engines[key] = {"eng": eng, "sig": "resumed", "active": False}
            log.warning("LIVE engine resumed for EXITS ONLY: %s (%d open)", eng.name, len(eng.pos))

    def _run(self, rec, key, now):
        try:
            rec["eng"].step(now)
        except Exception as e:                              # one engine's problem never stops the others
            log.exception("[%s] unexpected error in this step (it will try again next scan)", rec["eng"].name)
            self.problems[key] = "%s: %s" % (type(e).__name__, e)
            if key[2] == "live":
                self._alert("ARBITRAGE: unexpected error in %s: %s: %s" % (rec["eng"].name, type(e).__name__, e))

    @staticmethod
    def _books_ready(eng, scanner, max_age):
        """Trade only on fresh prices: every price list this engine needs must have arrived, and recently.
        (A price feed that quietly stops must never leave the engines trading on old prices.)"""
        if eng.module == "spatial":
            keys = [(ex, eng.market) for ex in eng.ctx.exchanges]
        elif eng.module == "funding":
            keys = [(ex, mk) for ex in eng.ctx.exchanges for mk in ("spot", "futures")]
        else:
            keys = [(ex, "spot") for ex in eng.ctx.exchanges]
        return all(scanner.books.get(k) and scanner.book_age(k) <= max_age for k in keys)

    # ---------------------------------------------------------------- reading results
    def _rows(self):
        if self.journal is None:
            return []
        try:
            st = os.stat(self.journal.path)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return []
        if self._csv_cache[0] != sig:
            try:
                rows = self.journal.rows()
            except (OSError, csv.Error):
                rows = []
            self._csv_cache = (sig, rows)
        return self._csv_cache[1]

    @staticmethod
    def _f(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    def _build_snapshot(self, s=None) -> dict:
        engines, positions = [], []
        for key, rec in sorted(self.engines.items()):
            e = rec["eng"]
            t = e.st.totals
            held = sum(1 for p in e.pos if p["kind"] == "held")
            try:
                wallet = round(e.broker.total_usdt(), 4) if e.mode != "live" else None
            except Exception:
                wallet = None
            engines.append({"name": e.name, "module": e.module, "market": key[1], "mode": e.mode,
                            "active": rec["active"], "exit_only": bool(e.exit_only), "open": len(e.pos) - held,
                            "held": held, "trades": t["trades"], "wins": t["wins"],
                            "net_pnl_usdt": round(t["net_pnl_usdt"], 6), "wallet_usdt": wallet})
            for p in e.pos:
                try:
                    unreal = e.unreal(p)
                except Exception:
                    unreal = None
                positions.append({"id": p["id"], "module": e.module, "market": p["market"], "mode": e.mode,
                                  "base": p["base"], "kind": p["kind"], "opened_utc": p["opened_utc"],
                                  "size_usdt": round(p["size_usdt"], 4),
                                  "legs": ["%s %s %.8g @ %.8g" % (l["side"], l["ex"], l["qty"], l["entry_px"])
                                           for l in p["legs"]],
                                  "funding": round(p["funding"], 6),
                                  "net_now": None if unreal is None else round(unreal, 6), "why": p.get("why", "")})
        rows = self._rows()
        trades = [dict(r) for r in rows[-TRADE_ROWS:]][::-1]
        daily = defaultdict(lambda: {"trades": 0, "wins": 0, "net": 0.0})
        for r in rows:
            key = ((r.get("closed_utc") or "")[:10], r.get("module", ""), r.get("mode", ""))
            d = daily[key]
            net = self._f(r.get("net_pnl_usdt"))
            d["trades"] += 1
            d["wins"] += 1 if net > 0 else 0
            d["net"] += net
        days = sorted({k[0] for k in daily if k[0]}, reverse=True)[:DAYS]
        daily_rows = [{"day": day, "module": mod, "mode": mode, "trades": v["trades"], "wins": v["wins"],
                       "net": round(v["net"], 6)}
                      for (day, mod, mode), v in sorted(daily.items(), reverse=True) if day in days]
        live_open = sum(1 for p in positions if p["mode"] == "live")
        return {"engines": engines, "positions": positions, "trades": trades, "daily": daily_rows,
                "live_open": live_open, "kill_switch": False,
                "problems": [("%s/%s/%s: %s" % (k[0], k[1], k[2], v)) for k, v in sorted(self.problems.items())]}

    def snapshot(self, s=None) -> dict:
        snap = self._snap
        if snap is None:
            # never wait for a running order cycle: if the worker holds the lock, show an empty page for a moment
            if self.lock.acquire(blocking=False):
                try:
                    snap = self._snap = self._build_snapshot(s)
                finally:
                    self.lock.release()
            else:
                snap = {"engines": [], "positions": [], "trades": [], "daily": [], "live_open": 0,
                        "kill_switch": False, "problems": []}
        out = dict(snap)
        path = self.kill_file(s) if s is not None else (self.risk.kill_file if self.risk else "")
        out["kill_switch"] = bool(path) and os.path.exists(path)
        return out

    def set_stop(self, on: bool, s) -> bool:
        """The 'Stop new trades' button: creates / removes the kill-switch file. Exits keep working."""
        path = self.kill_file(s)
        if not path:
            raise ArbStateError("No kill-switch file name is set (Safety > Kill-switch file name).")
        os.makedirs(self.folder, exist_ok=True)
        if on:
            with open(path, "w", encoding="utf-8") as f:
                f.write("Created by the Stop button in the Arbitrage tab. Delete this file (or use the button) to allow new trades.\n")
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return os.path.exists(path)

    # ---------------------------------------------------------------- key check (read only)
    def check_keys(self, s, loop) -> dict:
        """Tests every entered key with READ-ONLY calls: balance, position mode, open positions. No order is sent."""
        tp = Transport(loop)
        out = {}
        for slot, status in self.keys.status().items():
            if not status["set"] and not status.get("uses_futures_key"):
                out[slot] = {"ok": None, "text": "not entered"}
                continue
            ex = "okx" if slot == "okx" else "binance"
            market = "spot" if slot == "binance_spot" else "futures"
            try:
                cl = self._make_client(s, ex, market, tp)
                bal = cl.get_balances(["USDT"])["USDT"]["available"]
                parts = ["works, USDT available %.2f" % bal]
                if market == "futures":
                    parts.append("one-way mode: %s" % ("yes" if cl.one_way_mode() else "NO (switch the account to one-way)"))
                    parts.append("open futures positions: %d" % len(cl.open_positions()))
                names = self.hub.accounts_using(ex, cl.api_key)
                if names:
                    parts.append("same key as hub account: %s (its pairs will be skipped)" % ", ".join(names))
                out[slot] = {"ok": True, "text": "; ".join(parts), "hub_accounts": names}
            except (ApiError, LiveNotReady, ValueError) as e:
                out[slot] = {"ok": False, "text": str(e)[:240]}
        return out

    # ---------------------------------------------------------------- reset (MOCK results only)
    def reset(self):
        """Throw away the MOCK results: mock wallets, open mock trades, mock rows of the trade list.
        Live engines, live positions, live trades and the live risk state are never touched."""
        with self.lock:
            folder = self.folder
            for key in [k for k, r in self.engines.items() if r["eng"].mode in MOCK_MODES]:
                self.engines.pop(key)
                self.problems.pop(key, None)
            removed = 0
            if os.path.isdir(folder):
                for name in os.listdir(folder):
                    if name.startswith("state_") and (name.endswith("_mock.json") or name.endswith("_live-mock.json")):
                        try:
                            os.remove(os.path.join(folder, name))
                            removed += 1
                        except OSError as e:
                            raise ArbStateError("Could not delete %s: %s" % (name, e))
                path = os.path.join(folder, "trades.csv")
                if os.path.exists(path):
                    with open(path, newline="", encoding="utf-8") as f:
                        rows = list(csv.DictReader(f))
                    keep = [r for r in rows if r.get("mode") == "live"]
                    if keep:
                        with open(path, "w", newline="", encoding="utf-8") as f:
                            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
                            w.writeheader()
                            w.writerows(keep)
                    else:
                        os.remove(path)
                    removed += 1
            if not self.engines:
                self.ctx = self.risk = self.journal = None
                self.live_groups.clear()
            self._csv_cache = (None, [])
            self._snap = None
            return removed
