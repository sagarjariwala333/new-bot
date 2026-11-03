"""
hub_state.py
============

The small memory of a STRATEGY HUB pair, saved per platform + account + pair so it survives restarts:

  last_processed_candle : open time of the last closed candle the hub acted on (a candle is never done twice)
  wait                  : the entry-wait machine (pend_dir / pend_bars / pend_from_flip)
  first_trade_done      : the "first trade only on a fresh reversal" rule is satisfied for good
  open_now              : an order to open that is decided but not done yet, e.g. the flip close is not
                          confirmed flat yet: {"dir": 1|-1, "candle": open_time, "rules_check": bool}
  tp                    : (Stage 3) memory of the take-profit levels of the open trade
  shadow_wait           : the entry-wait machine of the PAPER trades (shadow mode)
  last_total            : (fixed base amount method) the total of the last fully closed trade = its entry amount
                          + every fill of that trade. The next entry is this, but never below the base.
  trade_amount          : (fixed base amount method) the entry amount of the trade that is open now
  trade_pnl             : (fixed base amount method) all fills of the open trade added up so far (net of fees)
  ladder                : the stop ladder of the open trade: the Max Loss, Stop Loss and Trailing Stop levels, the
                          best price seen (Step trailing) and which level rests on the exchange (2026-10-23)
  reentry_block         : "Allow Mid-Trend Re-entry" OFF - the side of the last closed trade, blocked until the
                          entry signal has ended (2026-10-23)
  outside_closed_qty    : the part of the open trade closed OUTSIDE the bot (e.g. by hand on the exchange), already
                          recorded in the ledger and the fixed-base total (2026-10-24)
  outside_fill_ids      : the exchange fill ids of those outside closes (never recorded twice)
  res_open / res_pnl    : the open trade's result is being added up (every fill, net of fees) - for the Cooldown after
                          a loss (2026-11-01). Set at the entry, cleared when the trade has closed completely
  loss_streak           : losing trades in a row (a trade that ends at 0 or in profit ends the row)
  cooldown_through      : Cooldown after a loss: the open time (ms) of the last signal candle whose entry is blocked
  shadow_book           : the shadow - a paper copy of the whole plan that runs all the time while Shadow mode is
                          picked, also while real trading is paused (2026-10-24, app/shadow_book.py)

Written atomically. A missing or unreadable file means a fresh pair (no wait pending, nothing to open).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path

from app.fsutil import atomic_write, harden_permissions
from app.store import DATA_DIR

log = logging.getLogger("hub_state")

HUB_STATE_DIR = DATA_DIR / "hub_state"
HUB_STATE_DIR.mkdir(parents=True, exist_ok=True)
harden_permissions(HUB_STATE_DIR, is_dir=True)

_lock = threading.Lock()


@dataclass
class HubState:
    platform: str = ""
    account_id: str = ""
    symbol: str = ""
    last_processed_candle: int | None = None
    wait: dict = field(default_factory=lambda: {"pend_dir": 0, "pend_bars": 0, "pend_from_flip": False})
    first_trade_done: bool = False
    open_now: dict | None = None
    tp: dict | None = None
    shadow_wait: dict = field(default_factory=lambda: {"pend_dir": 0, "pend_bars": 0, "pend_from_flip": False})
    plan_hash: str = ""          # which plan the memory above belongs to
    last_total: float | None = None
    trade_amount: float | None = None
    trade_pnl: float = 0.0
    ladder: dict | None = None   # stop ladder of the open trade (Max Loss / Stop Loss / Trailing Stop levels and
                                 # which one rests on the exchange) - see hub_trading "STOP LADDER"
    reentry_block: int = 0       # "Allow Mid-Trend Re-entry" OFF: direction of the last closed trade (+1 / -1);
                                 # no new entry that way until the entry signal has ended. 0 = no block.
    outside_closed_qty: float = 0.0   # part of the open trade closed outside the bot, already recorded
    outside_fill_ids: list = field(default_factory=list)   # fill ids of those outside closes
    res_open: bool = False            # a trade is open and its result is being added up (Cooldown after a loss)
    res_pnl: float = 0.0              # all fills of that trade added up (net of fees)
    loss_streak: int = 0              # losing trades in a row
    cooldown_through: int | None = None   # last blocked signal candle (open time ms); None = no cooldown running
    shadow_book: dict | None = None   # SHADOW: the paper copy of the plan that runs all the time (app/shadow_book.py)


def _path(platform: str, account_id: str, symbol: str) -> Path:
    return HUB_STATE_DIR / f"{platform}_{account_id}_{symbol}.json"


def load(platform: str, account_id: str, symbol: str) -> HubState:
    path = _path(platform, account_id, symbol)
    st = HubState(platform=platform, account_id=account_id, symbol=symbol)
    with _lock:
        if not path.exists():
            return st
        try:
            raw = json.loads(path.read_text())
        except Exception as e:
            log.warning("Could not read hub state %s (%s) - starting this pair's hub memory fresh.", path, e)
            return st
    known = set(HubState.__dataclass_fields__)
    for k, v in raw.items():
        if k in known and k not in ("platform", "account_id", "symbol"):
            setattr(st, k, v)
    return st


def save(st: HubState) -> None:
    path = _path(st.platform, st.account_id, st.symbol)
    with _lock:
        try:
            atomic_write(path, json.dumps(asdict(st)), secret=False)
        except Exception as e:
            log.error("Could not save hub state for %s/%s/%s: %s", st.platform, st.account_id, st.symbol, e)
