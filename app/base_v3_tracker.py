"""
base_v3_tracker.py
==================

BASE V3 TRACKER (owner design, 2026-09-28) - the bot's own separate memory
per account + pair, used ONLY to decide shadow mode, exactly like the Pine
backtest's closed equity (strategy.initial_capital + strategy.netprofit).

Rules agreed with the owner:
  * Starts at a fixed amount (entered per pair on the dashboard).
  * Moves ONLY when a REAL bot trade closes, by that trade's PERCENTAGE
    result (net profit / available balance at entry). Deposits,
    withdrawals, manual trades, margin top-ups and other pairs never touch
    it - it always assumes nothing like that ever happened.
  * A real WINNING trade that lifts the tracker above its previous high
    starts shadow mode (Pine: closedEq > peakEqClosed and profit > 0).
  * Paper (shadow) trades are recorded here too, but NEVER change the
    tracker balance (same as Pine - paper trades are not strategy trades).
  * Nothing in here ever sizes, places, changes or closes a real order.
    Its only link to live trading is the shadow on/off switch.

Also persists the last candle this pair has fully processed, so a restart
never processes the same closed candle twice (prompt Part 16).

One JSON file per platform + account + pair:
  data/base_v3_tracker/<platform>_<account_id>_<symbol>.json
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from app.fsutil import atomic_write, harden_permissions
from app.store import DATA_DIR
from app import strategy as strat

log = logging.getLogger("base_v3_tracker")

TRACKER_DIR = DATA_DIR / "base_v3_tracker"
TRACKER_DIR.mkdir(parents=True, exist_ok=True)
harden_permissions(TRACKER_DIR, is_dir=True)

MAX_HISTORY = 500          # rows kept per list (real trades / paper trades)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


@dataclass
class TrackerState:
    platform: str
    account_id: str
    symbol: str
    start_balance: float = 0.0     # always set from the pair's tracker_start_balance on creation
    balance: float = 0.0
    peak: float = 0.0
    real_trades: list = field(default_factory=list)
    shadow: dict = field(default_factory=lambda: asdict(strat.ShadowState()))
    last_processed_candle: int | None = None
    # 2026-10-01 first-reversal rule. None = not armed (pair already has real trades, or the rule is off).
    # {"waiting": bool, "reference": "LONG"|"SHORT"|None, "armed_candle": int} once armed on a pair that
    # has not made its first real trade. waiting turns False at that first real entry.
    first_reversal: dict | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---------------------------------------------------------------- shadow (typed view)
    def get_shadow(self) -> strat.ShadowState:
        known = set(strat.ShadowState.__dataclass_fields__)
        return strat.ShadowState(**{k: v for k, v in (self.shadow or {}).items() if k in known})

    def set_shadow(self, sh: strat.ShadowState) -> None:
        self.shadow = asdict(sh)

    # ---------------------------------------------------------------- summaries
    def pct_from_start(self) -> float:
        return (self.balance / self.start_balance - 1.0) * 100.0 if self.start_balance else 0.0

    def summary(self) -> dict:
        sh = self.get_shadow()
        return {
            "start_balance": self.start_balance,
            "balance": self.balance,
            "peak": self.peak,
            "pct_from_start": self.pct_from_start(),
            "pct_below_peak": (self.balance / self.peak - 1.0) * 100.0 if self.peak else 0.0,
            "real_trade_count": len(self.real_trades),
            "shadow_active": sh.active,
            "shadow_done": sh.count,
            "shadow_open_paper_trade": sh.current,
            "shadow_periods": sh.periods,
            "shadow_trades_total": sh.trades_total,
            "shadow_last_end_reason": sh.last_end_reason,
            "last_processed_candle": self.last_processed_candle,
            "waiting_first_reversal": bool((self.first_reversal or {}).get("waiting")),
            "first_reversal_reference": (self.first_reversal or {}).get("reference"),
        }


class TrackerUnreadableError(RuntimeError):
    """The tracker file exists but neither it nor its backup can be read. The pair must NOT
    start (a fresh tracker would silently throw away the all-time high and the shadow state).
    The unreadable file is left exactly where it is; a copy is kept aside for inspection."""


def _path(platform: str, account_id: str, symbol: str) -> Path:
    return TRACKER_DIR / f"{platform}_{account_id}_{symbol}.json"


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _read_state(path: Path) -> TrackerState:
    raw = json.loads(path.read_text())
    known = set(TrackerState.__dataclass_fields__)
    return TrackerState(**{k: v for k, v in raw.items() if k in known})


def load(platform: str, account_id: str, symbol: str,
         start_balance: float) -> TrackerState:
    """Loads the tracker, creating it at `start_balance` if it doesn't exist
    yet. If the configured start balance changed since last time, the
    tracker is RESCALED (balance, peak and every row's $ figures x the same
    factor) - because it moves by percentages, rescaling keeps every new
    high at exactly the same trade, so shadow timing is unchanged."""
    path = _path(platform, account_id, symbol)
    with _lock_for(str(path)):
        state = None
        if path.exists():
            try:
                state = _read_state(path)
            except Exception as e:
                # NEVER silently restart a tracker that exists but cannot be read: that would
                # throw away its all-time high and shadow state. Try the last good backup;
                # if that fails too, keep the file where it is and refuse to start this pair.
                bak = _backup_path(path)
                aside = path.with_name(path.name + f".unreadable-{int(time.time())}")
                try:
                    shutil.copy2(path, aside)
                except OSError:
                    pass
                try:
                    state = _read_state(bak)
                except Exception as e2:
                    log.error("Tracker file for %s/%s/%s is unreadable (%s) and so is its backup (%s). "
                              "A copy was kept at %s.", platform, account_id, symbol, e, e2, aside)
                    raise TrackerUnreadableError(
                        f"The tracker file for {platform}/{account_id}/{symbol} cannot be read ({e}) and "
                        f"its backup cannot be read either. This pair will NOT start, so the tracker "
                        f"(all-time high, shadow state) is not silently reset. Restore the file from a "
                        f"volume backup, or - only if you really want a brand-new tracker - delete "
                        f"{path.name} and {bak.name} from the tracker folder and start the pair again. "
                        f"A copy of the unreadable file was kept as {aside.name}.") from e
                log.error("Tracker file for %s/%s/%s was unreadable (%s) - RESTORED from its last good "
                          "backup. A copy of the bad file was kept at %s.", platform, account_id, symbol, e, aside)
                _save_unlocked(path, state)
        if state is None:
            if not start_balance or float(start_balance) <= 0:
                raise ValueError("tracker start balance is blank - set it on the dashboard first")
            sb = float(start_balance)
            state = TrackerState(platform=platform, account_id=account_id, symbol=symbol,
                                 start_balance=sb, balance=sb, peak=sb)
            _save_unlocked(path, state)
            return state

        want = float(start_balance or 0)
        if want > 0 and abs(want - state.start_balance) > 1e-9:
            factor = want / state.start_balance if state.start_balance else 1.0
            state.start_balance = want
            state.balance *= factor
            state.peak *= factor
            for row in state.real_trades:
                for k in ("tracker_before", "tracker_after"):
                    if row.get(k) is not None:
                        row[k] *= factor
            _save_unlocked(path, state)
            log.info("Tracker for %s/%s/%s rescaled to new start balance %.2f (factor %.6f) - "
                     "shadow timing unchanged.", platform, account_id, symbol, want, factor)
        return state


def _save_unlocked(path: Path, state: TrackerState) -> None:
    state.updated_at = time.time()
    # Re-create the folder if it vanished (e.g. a volume remount) - a
    # tracker write must never fail just because its directory is missing.
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the last GOOD file as a backup before replacing it (only if it is readable).
    try:
        if path.exists():
            _read_state(path)
            atomic_write(_backup_path(path), path.read_text(), secret=False)
    except Exception:
        pass   # an unreadable current file must never overwrite a good backup
    atomic_write(path, json.dumps(asdict(state), indent=1), secret=False)


def save(state: TrackerState) -> None:
    path = _path(state.platform, state.account_id, state.symbol)
    with _lock_for(str(path)):
        try:
            _save_unlocked(path, state)
        except Exception as e:
            log.error("Could not save tracker for %s/%s/%s: %s",
                      state.platform, state.account_id, state.symbol, e)


def apply_real_close(state: TrackerState, *, result_pct: float, pnl: float, direction: str,
                     entry_price: float | None, exit_price: float | None, reason: str,
                     balance_at_entry: float | None, use_shadow: bool,
                     approximated: bool = False) -> bool:
    """Applies one REAL closed trade to the tracker. Returns True if this
    trade started a new shadow period.

    Pine:  closedEq = initial_capital + netprofit
           if closedEq > peakEqClosed
               peakEqClosed := closedEq
               if useShadow and lastTradeProfit > 0 -> shadowActive := true ...
    """
    before = state.balance
    state.balance = before * (1.0 + result_pct / 100.0)
    new_high = state.balance > state.peak
    started = False
    if new_high:
        state.peak = state.balance
        if use_shadow and pnl > 0:
            sh = state.get_shadow()
            strat.shadow_activate(sh)
            state.set_shadow(sh)
            started = True
    state.real_trades.append({
        "closed_at": time.time(), "direction": direction,
        "entry_price": entry_price, "exit_price": exit_price, "reason": reason,
        "pnl": pnl, "balance_at_entry": balance_at_entry, "result_pct": result_pct,
        "tracker_before": before, "tracker_after": state.balance,
        "new_high": new_high, "shadow_started": started, "approximated": approximated,
    })
    if len(state.real_trades) > MAX_HISTORY:
        del state.real_trades[: len(state.real_trades) - MAX_HISTORY]
    return started
