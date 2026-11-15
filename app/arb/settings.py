"""Arbitrage tab settings (owner 2026-11-15): defaults, plain-words checking and saving.

STEP 1 = read-only: the scanner only READS prices. Live orders arrive in Step 3, so "live" cannot be saved yet.
The structure is the same as the standalone arbitrage bot's settings.json (Step 3 build), so the trading code
can be ported over in the next steps without changing a single setting name.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

from app.fsutil import atomic_write

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent.parent / "data"))
ARB_DIR = DATA_DIR / "arb"
SETTINGS_FILE = ARB_DIR / "settings.json"

MARKETS = ("futures", "spot")
LIVE_AVAILABLE = True           # Step 3: live orders exist (every gate below must still pass)

DEFAULTS: dict = {
    "scanner_on": False,                       # nothing is requested from the exchanges until this is switched on
    "loop_seconds": 3,
    "timeout_seconds": 10,
    "exchanges": {
        "binance": {"enabled": True, "testnet": False},
        "okx": {"enabled": True, "testnet": False},
    },
    "fallback_fees_pct": {
        "binance": {"futures": 0.05, "spot": 0.10},
        "okx": {"futures": 0.05, "spot": 0.10},
    },
    "pairs": {
        "top_n": 30, "min_volume_usdt_24h": 2000000, "min_top_size_usdt": 50,
        "include": ["BTC", "ETH"], "exclude": [], "refresh_minutes": 30,
    },
    "orders": {
        "type": "limit_ioc", "price_buffer_pct": 0.02, "wait_seconds": 3,
        "exit_type": "market", "sequence": "simultaneous", "poll_ms": 300,
    },
    "risk": {"max_size_per_trade_usdt": None, "daily_loss_limit_usdt": None, "margin_cap_usdt": None,
             "kill_switch_file": "STOP.txt"},
    "one_leg": {
        "futures": {"action": "close_filled", "hold_profit_pct": 0.10, "hold_stop_loss_pct": 0.5,
                    "hold_max_minutes": 0, "block_pair_while_held": True},
        "spot": {"action": "hold_for_profit", "hold_profit_pct": 0.10, "hold_stop_loss_pct": 0,
                 "hold_max_minutes": 0, "block_pair_while_held": True},
    },
    "live_mock": {"enabled": True},
    "alerts": {"trades": "live"},       # Telegram message on every trade opened / closed: off | live | all
    "modules": {
        "spatial": {"enabled": True, "mode": "mock", "markets": ["futures", "spot"], "trade_size_usdt": 100,
                    "min_profit_pct": 0.05, "exit_profit_pct": 0.0, "max_hold_seconds": 0, "stop_loss_pct": 0,
                    "slippage_pct": 0.01, "leverage": 3, "max_open_trades": 5, "max_entries_per_loop": 3,
                    "mock_start_usdt": 5000, "mock_coin_inventory_usdt": 500},
        "funding": {"enabled": True, "mode": "mock", "trade_size_usdt": 100, "min_funding_pct": 0.03,
                    "max_breakeven_periods": 5, "min_basis_pct": -0.1, "expected_periods": 3,
                    "exit_funding_pct": 0.0, "max_hold_hours": 0, "stop_loss_pct": 1.0, "take_profit_pct": 0,
                    "leverage": 3, "max_open_trades": 3, "max_entries_per_loop": 1, "refresh_seconds": 120,
                    "top_n": 30, "mock_start_usdt": 5000},
        "triangular": {"enabled": False, "confirm_enable": False, "mode": "mock", "trade_size_usdt": 100,
                       "min_profit_pct": 0.10, "slippage_pct": 0.01, "bridge_coins": ["BTC", "ETH"],
                       "top_n": 40, "max_entries_per_loop": 1, "mock_start_usdt": 1000},
    },
}


class _V:
    def __init__(self):
        self.errs: list[str] = []

    def err(self, msg):
        self.errs.append(msg)

    def num(self, d, key, path, lo=None, hi=None, integer=False, required=True, gt=None):
        label = ("%s.%s" % (path, key)) if path else key
        if not isinstance(d, dict) or key not in d or d[key] is None:
            if required:
                self.err("%s is missing - please fill it in" % label)
            return None
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            self.err("%s must be a number (got %r)" % (label, v))
            return None
        if integer and int(v) != v:
            self.err("%s must be a whole number" % label)
        if lo is not None and v < lo:
            self.err("%s must be at least %s (got %s)" % (label, lo, v))
        if gt is not None and v <= gt:
            self.err("%s must be more than %s (got %s)" % (label, gt, v))
        if hi is not None and v > hi:
            self.err("%s must be at most %s (got %s)" % (label, hi, v))
        return v

    def choice(self, d, key, path, options):
        v = d.get(key) if isinstance(d, dict) else None
        if v not in options:
            self.err("%s.%s must be one of %s (got %r)" % (path, key, list(options), v))
        return v

    def boolean(self, d, key, path):
        if not isinstance(d, dict) or not isinstance(d.get(key), bool):
            self.err("%s.%s must be true or false" % (path, key))


def validate(s) -> list[str]:
    """Every problem at once, in plain words. Empty list = fine."""
    v = _V()
    if not isinstance(s, dict):
        return ["The settings must be an object"]
    v.boolean(s, "scanner_on", "")
    for k in ("loop_seconds", "timeout_seconds"):
        v.num(s, k, "", gt=0, hi=3600)
    if isinstance(s.get("loop_seconds"), (int, float)) and not isinstance(s.get("loop_seconds"), bool) \
            and 0 < s["loop_seconds"] < 2:
        v.err("loop_seconds must be at least 2 (a faster scan can get your IP blocked)")

    ex = s.get("exchanges")
    enabled = []
    if not isinstance(ex, dict):
        v.err("exchanges is missing")
    else:
        for name in ("binance", "okx"):
            e = ex.get(name)
            if not isinstance(e, dict):
                v.err("exchanges.%s is missing" % name)
                continue
            v.boolean(e, "enabled", "exchanges.%s" % name)
            v.boolean(e, "testnet", "exchanges.%s" % name)
            if e.get("enabled") is True:
                enabled.append(name)
        if not enabled:
            v.err("no exchange is switched on")
    fb = s.get("fallback_fees_pct")
    if not isinstance(fb, dict):
        v.err("fallback_fees_pct is missing")
    else:
        for name in ("binance", "okx"):
            for m in MARKETS:
                v.num((fb.get(name) or {}), m, "fallback_fees_pct.%s" % name, lo=0, hi=5)

    p = s.get("pairs")
    if not isinstance(p, dict):
        v.err("pairs is missing")
    else:
        v.num(p, "top_n", "pairs", lo=1, hi=200, integer=True)
        v.num(p, "min_volume_usdt_24h", "pairs", lo=0)
        v.num(p, "min_top_size_usdt", "pairs", lo=0)
        v.num(p, "refresh_minutes", "pairs", gt=0, hi=1440)
        for k in ("include", "exclude"):
            if not isinstance(p.get(k), list) or any(not isinstance(x, str) or not x.strip() for x in p.get(k, [])):
                v.err("pairs.%s must be a list of coin names like [\"BTC\"]" % k)

    o = s.get("orders")
    if not isinstance(o, dict):
        v.err("orders is missing")
    else:
        v.choice(o, "type", "orders", ("market", "limit_ioc", "limit_wait"))
        v.choice(o, "exit_type", "orders", ("market", "limit_ioc"))
        v.choice(o, "sequence", "orders", ("simultaneous", "harder_first"))
        v.num(o, "price_buffer_pct", "orders", lo=0, hi=2)
        v.num(o, "wait_seconds", "orders", gt=0, hi=60)
        v.num(o, "poll_ms", "orders", lo=50, hi=5000)

    modules = s.get("modules") if isinstance(s.get("modules"), dict) else {}
    live = [n for n, m in modules.items() if isinstance(m, dict) and m.get("enabled") and m.get("mode") == "live"]

    r = s.get("risk")
    if not isinstance(r, dict):
        v.err("risk is missing")
    else:
        for k in ("max_size_per_trade_usdt", "daily_loss_limit_usdt"):
            if k not in r:
                v.err("risk.%s is missing (use 0 for 'off')" % k)
            elif r[k] is None:
                if live:
                    v.err("risk.%s must be filled in before going live (a number; 0 = off)" % k)
            else:
                v.num(r, k, "risk", lo=0)
        # margin cap: the most USDT of isolated margin the arbitrage may use per exchange (futures legs)
        uses_futures = any(
            (n == "spatial" and "futures" in (m.get("markets") or [])) or n == "funding"
            for n, m in modules.items() if isinstance(m, dict) and m.get("enabled") and m.get("mode") == "live")
        if r.get("margin_cap_usdt") is None:
            if uses_futures:
                v.err("risk.margin_cap_usdt must be filled in before live futures trading (the most margin the "
                      "arbitrage may use per exchange, so it cannot eat the hub's free balance)")
        else:
            v.num(r, "margin_cap_usdt", "risk", gt=0)
        kf = r.get("kill_switch_file", "")
        if not isinstance(kf, str) or "/" in kf or "\\" in kf:
            v.err("risk.kill_switch_file must be a plain file name like STOP.txt")

    ol = s.get("one_leg")
    if not isinstance(ol, dict):
        v.err("one_leg is missing")
    else:
        for m, acts in (("futures", ("close_filled", "hold")), ("spot", ("hold_for_profit", "close_filled"))):
            d = ol.get(m)
            if not isinstance(d, dict):
                v.err("one_leg.%s is missing" % m)
                continue
            v.choice(d, "action", "one_leg.%s" % m, acts)
            v.num(d, "hold_profit_pct", "one_leg.%s" % m, lo=0)
            v.num(d, "hold_stop_loss_pct", "one_leg.%s" % m, lo=0)
            v.num(d, "hold_max_minutes", "one_leg.%s" % m, lo=0)
            v.boolean(d, "block_pair_while_held", "one_leg.%s" % m)

    al = s.get("alerts")
    if not isinstance(al, dict):
        v.err("alerts is missing")
    else:
        v.choice(al, "trades", "alerts", ("off", "live", "all"))
    lm = s.get("live_mock")
    if not isinstance(lm, dict) or not isinstance(lm.get("enabled"), bool):
        v.err("live_mock.enabled must be true or false")

    sp, fu, tr = modules.get("spatial"), modules.get("funding"), modules.get("triangular")
    for name, mod in (("spatial", sp), ("funding", fu), ("triangular", tr)):
        if not isinstance(mod, dict):
            v.err("modules.%s is missing" % name)
            continue
        v.boolean(mod, "enabled", "modules.%s" % name)
        v.choice(mod, "mode", "modules.%s" % name, ("mock", "live"))
        if mod.get("mode") == "live" and not LIVE_AVAILABLE:
            v.err("modules.%s.mode: live orders are not available yet (Step 3). Keep it on mock." % name)
    if isinstance(sp, dict) and sp.get("enabled") is True:
        P = "modules.spatial"
        if len(enabled) < 2:
            v.err("spatial needs BOTH exchanges switched on")
        mk = sp.get("markets")
        if not isinstance(mk, list) or not mk or any(m not in MARKETS for m in mk):
            v.err("%s.markets must be a list made of \"futures\" and/or \"spot\"" % P)
        v.num(sp, "trade_size_usdt", P, gt=0)
        for k in ("min_profit_pct", "exit_profit_pct"):
            v.num(sp, k, P)
        for k in ("max_hold_seconds", "stop_loss_pct", "slippage_pct", "mock_start_usdt", "mock_coin_inventory_usdt"):
            v.num(sp, k, P, lo=0)
        v.num(sp, "leverage", P, lo=1, hi=125, integer=True)
        v.num(sp, "max_open_trades", P, lo=1, integer=True)
        v.num(sp, "max_entries_per_loop", P, lo=1, integer=True)
    if isinstance(fu, dict) and fu.get("enabled") is True:
        P = "modules.funding"
        v.num(fu, "trade_size_usdt", P, gt=0)
        for k in ("min_funding_pct", "min_basis_pct", "exit_funding_pct"):
            v.num(fu, k, P)
        for k in ("max_breakeven_periods", "expected_periods", "refresh_seconds"):
            v.num(fu, k, P, gt=0)
        for k in ("max_hold_hours", "stop_loss_pct", "take_profit_pct", "mock_start_usdt"):
            v.num(fu, k, P, lo=0)
        v.num(fu, "leverage", P, lo=1, hi=125, integer=True)
        for k in ("max_open_trades", "max_entries_per_loop", "top_n"):
            v.num(fu, k, P, lo=1, integer=True)
    if isinstance(tr, dict) and tr.get("enabled") is True:
        P = "modules.triangular"
        v.boolean(tr, "confirm_enable", P)
        v.num(tr, "trade_size_usdt", P, gt=0)
        v.num(tr, "min_profit_pct", P)
        v.num(tr, "slippage_pct", P, lo=0)
        v.num(tr, "mock_start_usdt", P, lo=0)
        v.num(tr, "top_n", P, lo=1, integer=True)
        v.num(tr, "max_entries_per_loop", P, lo=1, integer=True)
        bc = tr.get("bridge_coins")
        if not isinstance(bc, list) or not bc or any(not isinstance(x, str) or not x.strip() for x in bc):
            v.err("%s.bridge_coins must be a list like [\"BTC\", \"ETH\"]" % P)
    return v.errs


def _merge(base, over):
    """Defaults first, the saved values on top (new settings added later get their default)."""
    out = copy.deepcopy(base)
    if isinstance(over, dict):
        for k, val in over.items():
            if isinstance(out.get(k), dict) and isinstance(val, dict):
                out[k] = _merge(out[k], val)
            else:
                out[k] = copy.deepcopy(val)
    return out


class ArbSettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else SETTINGS_FILE
        self._lock = threading.Lock()

    def load(self) -> dict:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return copy.deepcopy(DEFAULTS)
            except (OSError, ValueError) as e:
                # A damaged file is never silently replaced: it is reported and the defaults are shown, but
                # nothing is overwritten until the owner saves.
                raise ValueError("The arbitrage settings file is damaged (%s). Save again to replace it." % e)
            return _merge(DEFAULTS, raw)

    def save(self, new: dict) -> list[str]:
        """Checks, then saves. Returns the list of problems (empty = saved)."""
        errs = validate(new)
        if errs:
            return errs
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(new, indent=1), secret=False)
        return []


arb_settings = ArbSettingsStore()
