"""State files, CSV journal and risk limits for the arbitrage engines (hub version).

Differences from the standalone bot: a damaged file raises ArbStateError (a normal error shown in the tab) instead of
stopping the whole program, files are written with the hub's atomic writer, and everything lives under DATA_DIR/arb.
"""
import csv
import json
import os
from datetime import datetime, timezone

from app.fsutil import atomic_write

JOURNAL_FIELDS = ["id", "module", "market", "mode", "base", "opened_utc", "closed_utc",
                  "detail", "qty", "size_usdt", "fees_usdt", "funding_usdt",
                  "net_pnl_usdt", "net_pnl_pct", "reason"]


class ArbStateError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def iso(ts=None):
    return (ts or utc_now()).strftime("%Y-%m-%d %H:%M:%S")


def read_json(path, what):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise ArbStateError("%s is damaged. Use 'Reset mock results' (or move the file away) to start fresh: %s"
                            % (what, path))
    if not isinstance(data, dict):
        raise ArbStateError("%s is not valid. Use 'Reset mock results' to start fresh: %s" % (what, path))
    return data


def write_json(path, data):
    from pathlib import Path
    atomic_write(Path(path), json.dumps(data, indent=1), secret=False)


class Journal:
    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "trades.csv")
        self.on_add = None          # called with the row after it is written (trade-closed message)

    def add(self, row):
        new = not os.path.exists(self.path)
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)
        if self.on_add is not None:
            try:
                self.on_add(row)
            except Exception:
                pass

    def rows(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


class Risk:
    """Safety limits. Only REAL (live) money counts toward the daily loss (live arrives in Step 3)."""

    def __init__(self, cfg, folder, log):
        self.max_size = float(cfg.get("max_size_per_trade_usdt") or 0)
        self.loss_limit = float(cfg.get("daily_loss_limit_usdt") or 0)
        kf = cfg.get("kill_switch_file", "STOP.txt") or ""
        self.kill_file = os.path.join(folder, kf) if kf else ""
        self.log = log
        self.path = os.path.join(folder, "risk_state.json")
        self.day, self.live_pnl = None, 0.0
        d = read_json(self.path, "risk_state.json")
        if d:
            self.day, self.live_pnl = d.get("day"), float(d.get("live_pnl", 0.0))
        self._warned = set()
        self.alert = lambda text: None

    def update_limits(self, cfg):
        self.max_size = float(cfg.get("max_size_per_trade_usdt") or 0)
        self.loss_limit = float(cfg.get("daily_loss_limit_usdt") or 0)
        kf = cfg.get("kill_switch_file", "STOP.txt") or ""
        self.kill_file = os.path.join(os.path.dirname(self.path), kf) if kf else ""

    def _roll(self):
        today = utc_now().strftime("%Y-%m-%d")
        if self.day != today:
            self.day, self.live_pnl = today, 0.0
            self._warned.discard("loss")
            self._save()

    def _save(self):
        write_json(self.path, {"day": self.day, "live_pnl": self.live_pnl})

    def record(self, pnl, mode):
        if mode == "live":
            self._roll()
            self.live_pnl += pnl
            self._save()

    def kill_switch_on(self):
        return bool(self.kill_file) and os.path.exists(self.kill_file)

    def entry_allowed(self, mode):
        """(ok, reason). Exits are never blocked by this."""
        if self.kill_switch_on():
            if "kill" not in self._warned:
                self.log.warning("KILL-SWITCH file '%s' found: no new trades will open (exits continue)",
                                 self.kill_file)
                self._warned.add("kill")
            return False, "kill-switch"
        self._warned.discard("kill")
        if mode == "live" and self.loss_limit > 0:
            self._roll()
            if self.live_pnl <= -self.loss_limit:
                if "loss" not in self._warned:
                    self.log.warning("DAILY LOSS LIMIT hit (%.2f <= -%.2f USDT): no new live trades today",
                                     self.live_pnl, self.loss_limit)
                    self._warned.add("loss")
                    self.alert("ARBITRAGE: DAILY LOSS LIMIT hit (%.2f <= -%.2f USDT): no new live trades today"
                               % (self.live_pnl, self.loss_limit))
                return False, "daily-loss-limit"
        return True, ""

    def cap(self, size, mode):
        if mode == "live" and self.max_size > 0 and size > self.max_size:
            return self.max_size
        return size


class EngineState:
    """Per-engine saved state: open positions, mock wallets, counters."""

    def __init__(self, folder, name):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "state_%s.json" % name)
        d = read_json(self.path, "state file") or {}
        self.positions = d.get("positions", [])
        self.wallets = d.get("wallets")
        self.seq = int(d.get("seq", 0))
        self.totals = d.get("totals", {"trades": 0, "net_pnl_usdt": 0.0, "wins": 0})

    def save(self):
        write_json(self.path, {"positions": self.positions, "wallets": self.wallets,
                               "seq": self.seq, "totals": self.totals})

    def next_id(self, prefix):
        self.seq += 1
        return "%s-%05d" % (prefix, self.seq)
