"""
ledger.py
=========

Records every CLOSED trade (SL, TP, force-close, or externally-closed) for
every account/pair, independent of the strategy math - this is bookkeeping
of what already happened, not a decision-maker. Backed by an append-only
JSON-lines file per account/pair so a crash never loses history and nothing
needs to be re-aggregated on every write.

Used for:
  - the dashboard's trade history table
  - CSV export
  - the equity-curve chart (cumulative realized PnL over time)
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from app.fsutil import harden_permissions

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
LEDGER_DIR = DATA_DIR / "ledger"
LEDGER_DIR.mkdir(parents=True, exist_ok=True)
harden_permissions(LEDGER_DIR, is_dir=True)

# One lock per ledger file, guarding appends against interleaving if more than
# one instance (or a request handler) ever writes to the same account/symbol
# ledger concurrently within this process. A dict + guard lock rather than a
# single global lock so unrelated pairs never block on each other.
_locks_guard = threading.Lock()
_file_locks: dict[Path, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        lock = _file_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _file_locks[path] = lock
        return lock


@dataclass
class TradeRecord:
    account_id: str
    symbol: str
    direction: str
    qty: float
    entry_price: float
    exit_price: float
    pnl: float | None
    reason: str            # "TP", "SL", "trailing_stop", "force_close_ema", "external"
    opened_at: float
    closed_at: float
    confirmed: bool = True  # False = exit_price/reason is a mark-price estimate, not a confirmed fill
    commission: float | None = None       # REAL commission from Binance, never estimated/simulated
    commission_included: bool = False     # True = `pnl` above is net of `commission`; False = `pnl` is gross


def _path(account_id: str, symbol: str) -> Path:
    return LEDGER_DIR / f"{account_id}_{symbol}.jsonl"


def record_trade(trade: TradeRecord):
    path = _path(trade.account_id, trade.symbol)
    lock = _lock_for(path)
    with lock:
        is_new_file = not path.exists()
        with open(path, "a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if is_new_file:
            harden_permissions(path)


def list_trades(account_id: str, symbol: str) -> list[TradeRecord]:
    path = _path(account_id, symbol)
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(TradeRecord(**json.loads(line)))
    return out


def stats(account_id: str, symbol: str) -> dict:
    trades = list_trades(account_id, symbol)
    closed = [t for t in trades if t.pnl is not None]
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in closed)
    return {
        "total_trades": len(trades),
        "closed_with_pnl": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed) * 100) if closed else None,
        "total_pnl": total_pnl,
        "avg_win": (sum(t.pnl for t in wins) / len(wins)) if wins else None,
        "avg_loss": (sum(t.pnl for t in losses) / len(losses)) if losses else None,
        "max_drawdown": max_drawdown(account_id, symbol),
        "sharpe_ratio": sharpe_ratio(account_id, symbol),
    }


def max_drawdown(account_id: str, symbol: str) -> dict:
    """2026-09-15, owner request ("business, not just a script"). Computed
    over the cumulative REALIZED PnL curve (equity_curve() below) - this is
    the worst peak-to-trough decline in trading PnL itself, not a percentage
    of total account equity (which isn't tracked historically per-point
    here, only per-trade at entry for the Max Loss Cap). Labeled explicitly
    as dollar drawdown of cumulative PnL, not implied to be anything else."""
    curve = equity_curve(account_id, symbol)
    if not curve:
        return {"max_drawdown": 0.0, "peak": 0.0, "trough": 0.0}
    peak = curve[0]["equity"]
    worst_dd = 0.0
    worst_peak = peak
    worst_trough = peak
    for point in curve:
        eq = point["equity"]
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > worst_dd:
            worst_dd = dd
            worst_peak = peak
            worst_trough = eq
    return {"max_drawdown": worst_dd, "peak": worst_peak, "trough": worst_trough}


def sharpe_ratio(account_id: str, symbol: str) -> float | None:
    """2026-09-15, owner request. Per-trade Sharpe (mean PnL / sample std
    of PnL across closed trades) - deliberately NOT annualized, since
    annualizing would require ASSUMING a trading frequency (trades/year)
    this bot has no basis to guess at reliably across different pairs/
    timeframes/market conditions. This is the honest, defensible version:
    "how much return per unit of variability, per trade" - not a number
    dressed up to look like a standard annualized fund metric it isn't.
    Returns None with fewer than 2 closed trades (can't compute a
    standard deviation) or when every trade had identical PnL (std=0,
    Sharpe undefined rather than infinite)."""
    trades = list_trades(account_id, symbol)
    closed_pnls = [t.pnl for t in trades if t.pnl is not None]
    if len(closed_pnls) < 2:
        return None
    mean = sum(closed_pnls) / len(closed_pnls)
    variance = sum((p - mean) ** 2 for p in closed_pnls) / (len(closed_pnls) - 1)  # sample std, ddof=1
    std = variance ** 0.5
    if std == 0:
        return None
    return mean / std


def equity_curve(account_id: str, symbol: str) -> list[dict]:
    """Cumulative realized PnL over time - one point per closed trade."""
    trades = sorted(list_trades(account_id, symbol), key=lambda t: t.closed_at)
    curve = []
    running = 0.0
    for t in trades:
        if t.pnl is None:
            continue
        running += t.pnl
        curve.append({"t": t.closed_at, "equity": running})
    return curve


def export_csv(account_id: str, symbol: str) -> str:
    trades = list_trades(account_id, symbol)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["opened_at", "closed_at", "direction", "qty", "entry_price",
                      "exit_price", "pnl", "reason"])
    for t in trades:
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.opened_at)) if t.opened_at else "",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.closed_at)) if t.closed_at else "",
            t.direction, t.qty, t.entry_price, t.exit_price, t.pnl, t.reason,
        ])
    return buf.getvalue()
