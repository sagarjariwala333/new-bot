"""
position_state.py
==================

Persists "equity at the moment this trade opened" (plus a couple of other
fields used only as a sanity cross-check on restore) to disk, so the Max
Loss Cap's baseline survives a restart instead of being approximated with
current equity. (BASE V3: now also holds the fixed stop, TP1/TP2 tracker state and the bot's own qty - see PositionSnapshot.) The original note follows:
a REAL "equity when this trade opened" figure, not a stand-in. Several
third-party reviews flagged the approximation as a real (if non-crashing)
correctness gap: after any restart mid-trade, the cap was checked against
a fixed reference the strategy design never actually specified.

Written the moment a position opens, deleted the moment it closes - it
must never linger past its own trade's lifetime, or a future restart could
mistake a leftover file for the NEXT trade's baseline. On restore, the
persisted direction/entry_price are cross-checked against the actual
resumed position from Binance before the persisted equity is trusted - if
they don't match closely enough, this is very likely a stale file from an
earlier trade, and the existing (documented, safe) approximation is used
instead rather than trusting data that may belong to a different trade.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

from app.fsutil import atomic_write, harden_permissions
from app.store import DATA_DIR

log = logging.getLogger("position_state")

POSITION_STATE_DIR = DATA_DIR / "position_state"
POSITION_STATE_DIR.mkdir(parents=True, exist_ok=True)
harden_permissions(POSITION_STATE_DIR, is_dir=True)

# How close the persisted entry_price must be to the resumed position's real
# entryPrice (from Binance) before the persisted equity_at_entry is trusted.
# A small relative tolerance, not exact-match, since Binance's own reported
# entryPrice can differ very slightly from what was locally recorded (e.g.
# minor rounding) even for the genuinely same trade.
ENTRY_PRICE_MATCH_TOLERANCE = 0.001  # 0.1%


@dataclass
class PositionSnapshot:
    direction: str
    entry_price: float
    qty: float
    # The AVAILABLE balance read at entry - used for sizing, for the fixed
    # stop, and for the tracker's % result.
    equity_at_entry: float
    opened_at: float
    # ---- BASE V3 trade state (all optional so older files still load) ----
    stop_price: float | None = None        # the FIXED stop requested at entry (never recalculated)
    entry_atr: float | None = None         # ATR on the signal candle (TP1/TP2 tracking only)
    tp1_price: float | None = None
    tp2_price: float | None = None
    tp1_touched: bool = False
    tp2_touched: bool = False
    alloc_pct: float | None = None         # trend / counter allocation chosen at entry
    leverage: float | None = None          # trend / counter leverage the trade was sized with; None in older files
    bot_qty: float | None = None           # the quantity the BOT opened (top-up detection + tracker share)
    entry_candle_time: int | None = None   # open_time of the signal candle


def _path(account_id: str, symbol: str) -> Path:
    return POSITION_STATE_DIR / f"{account_id}_{symbol}.json"


def save_position_state(account_id: str, symbol: str, snapshot: PositionSnapshot):
    """Called the moment a position opens. Best-effort: a failure here is
    logged, not fatal - the bot still works normally, just falls back to
    the equity approximation on a later restart if this couldn't be
    written this time."""
    try:
        atomic_write(_path(account_id, symbol), json.dumps(asdict(snapshot)), secret=False)
    except Exception as e:
        log.warning("Could not persist position state for %s/%s: %s", account_id, symbol, e)


def load_position_state(account_id: str, symbol: str) -> PositionSnapshot | None:
    """Returns None (not an exception) for a missing or unreadable file -
    callers already have a safe fallback (the equity approximation) for
    exactly this case."""
    path = _path(account_id, symbol)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        # Tolerate both older files (missing the Base V3 fields - they take
        # their defaults) and unknown extra keys (ignored), so a restart
        # never fails to read its own state across versions.
        known = set(PositionSnapshot.__dataclass_fields__)
        return PositionSnapshot(**{k: v for k, v in data.items() if k in known})
    except Exception as e:
        log.warning("Could not read persisted position state for %s/%s (%s) - ignoring it.",
                    account_id, symbol, e)
        return None


def clear_position_state(account_id: str, symbol: str):
    """Called the moment a position closes - must never linger past its
    own trade's lifetime, or a future restart could mistake it for the
    NEXT trade's baseline."""
    path = _path(account_id, symbol)
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        log.warning("Could not clear position state for %s/%s: %s", account_id, symbol, e)


def matches_resumed_position(snapshot: PositionSnapshot, direction: str, entry_price: float) -> bool:
    """The cross-check before trusting a persisted equity_at_entry on
    restore: same direction, and entry_price within a small tolerance of
    what Binance itself reports for the resumed position. If this doesn't
    match, the persisted file is very likely stale (left over from an
    earlier trade that wasn't cleaned up for some reason), not this one."""
    if snapshot.direction != direction:
        return False
    if entry_price == 0:
        return snapshot.entry_price == 0
    return abs(snapshot.entry_price - entry_price) / abs(entry_price) <= ENTRY_PRICE_MATCH_TOLERANCE
