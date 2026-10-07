"""
reconciliation.py
==================

Durable record for an order whose outcome couldn't be determined even
after the ambiguous-response recovery already built into the exchange
adapters (market_order/close_position_market/stop_market_order/
take_profit_market_order all query back by their own client-order-id
before giving up). If THAT confirmation query is ALSO inconclusive (e.g.
the network is down for both the original request and the follow-up
check), nobody was previously tracking whether a real order exists on the
exchange or not - the original exception just propagated, got logged, and
the next tick started fresh with no memory of it. Fixed 2026-09-14, per a
third-party review: this persists a durable, visible flag instead, so a
human can see it and check manually, rather than it only appearing as a
single log line that scrolls away.

REDESIGNED 2026-09-14 (owner-approved, found during a later bug-hunt pass):
the original version stored ONE record per account+symbol - a second
ambiguous order on the same symbol would silently OVERWRITE the first,
losing its client_order_id and detail entirely with no trace it ever
existed. Now stores a LIST of records per account+symbol, keyed by
client_order_id, so multiple concurrent unresolved orders on the same
symbol are each tracked independently and cleared independently.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.fsutil import atomic_write, harden_permissions
from app.store import DATA_DIR

log = logging.getLogger("reconciliation")

RECONCILIATION_DIR = DATA_DIR / "reconciliation"
RECONCILIATION_DIR.mkdir(parents=True, exist_ok=True)
harden_permissions(RECONCILIATION_DIR, is_dir=True)


@dataclass
class PendingReconciliation:
    account_id: str
    symbol: str
    context: str           # e.g. "market entry", "SL placement", "position close"
    client_order_id: str
    detail: str             # the original error message, for a human to read
    flagged_at: float


def _path(account_id: str, symbol: str) -> Path:
    return RECONCILIATION_DIR / f"{account_id}_{symbol}.json"


def _load_all(account_id: str, symbol: str) -> list[dict]:
    path = _path(account_id, symbol)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
        # Backward-compat: a file written by the OLD single-record format
        # is a plain dict, not a list - treat it as a one-item list rather
        # than crashing on a pre-existing file from before this redesign.
        if isinstance(raw, dict):
            return [raw]
        return raw if isinstance(raw, list) else []
    except Exception as e:
        log.warning("Could not read pending-reconciliation records for %s/%s (%s) - "
                    "treating as empty rather than crashing.", account_id, symbol, e)
        return []


def _save_all(account_id: str, symbol: str, records: list[dict]):
    path = _path(account_id, symbol)
    if not records:
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            log.warning("Could not remove now-empty pending-reconciliation file for %s/%s: %s",
                        account_id, symbol, e)
        return
    atomic_write(path, json.dumps(records), secret=False)


def flag_pending_reconciliation(account_id: str, symbol: str, context: str,
                                 client_order_id: str, detail: str):
    """Best-effort: a failure here is logged, not fatal - it would mean the
    flag itself couldn't be written, not that anything about the actual
    trading decision changes. Upserts by client_order_id - re-flagging the
    same order updates its record rather than duplicating it."""
    record = PendingReconciliation(account_id, symbol, context, client_order_id, detail, time.time())
    try:
        records = _load_all(account_id, symbol)
        records = [r for r in records if r.get("client_order_id") != client_order_id]
        records.append(asdict(record))
        _save_all(account_id, symbol, records)
        log.error("PENDING RECONCILIATION flagged for %s/%s (%s) - client order id %s. "
                  "Check the exchange manually: %s", account_id, symbol, context, client_order_id, detail)
    except Exception as e:
        log.error("Could not persist pending-reconciliation flag for %s/%s: %s", account_id, symbol, e)


def clear_pending_reconciliation(account_id: str, symbol: str, client_order_id: str | None = None):
    """client_order_id=None clears EVERY pending record for this
    account/symbol (used for a manual dashboard "clear all" action, or by
    callers that have deliberately confirmed via exchange truth that
    nothing is actually pending here anymore) - passing a specific id
    clears only that one record, leaving any other still-unresolved
    records for this symbol untouched. Prefer passing a specific id
    wherever the caller actually knows which order was resolved."""
    try:
        if client_order_id is None:
            _save_all(account_id, symbol, [])
        else:
            records = _load_all(account_id, symbol)
            remaining = [r for r in records if r.get("client_order_id") != client_order_id]
            _save_all(account_id, symbol, remaining)
    except Exception as e:
        log.warning("Could not clear pending-reconciliation flag for %s/%s: %s", account_id, symbol, e)


def get_pending_reconciliations(account_id: str, symbol: str) -> list[PendingReconciliation]:
    """Returns EVERY unresolved record for this account/symbol - plural,
    since more than one can now genuinely coexist (see module docstring)."""
    results = []
    for raw in _load_all(account_id, symbol):
        try:
            results.append(PendingReconciliation(**raw))
        except Exception as e:
            log.warning("Could not parse a pending-reconciliation record for %s/%s (%s) - skipping it.",
                        account_id, symbol, e)
    return results


def has_pending_reconciliation(account_id: str, symbol: str) -> bool:
    """Convenience for callers that only need a yes/no answer (e.g. the
    entry guard) without needing the individual record details."""
    return len(get_pending_reconciliations(account_id, symbol)) > 0


def list_all_pending_reconciliations() -> list[PendingReconciliation]:
    """Used by the dashboard to show a banner for ANY account/pair with an
    unresolved flag, not just whichever one is currently being viewed."""
    results = []
    for f in RECONCILIATION_DIR.glob("*.json"):
        try:
            raw = json.loads(f.read_text())
            items = [raw] if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            for item in items:
                results.append(PendingReconciliation(**item))
        except Exception:
            continue
    return results
