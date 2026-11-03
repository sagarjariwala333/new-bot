"""
strategy_library.py
===================

STRATEGY LIBRARY (owner request 2026-10-24): "a strategy if proven good needs to be saved.. to avoid if I forget
or overlooked" and "I can look back in the library when I wanna test it with different pairs."

Two lists, kept in one file on the server (DATA_DIR, the Railway volume), so they survive restarts and new zips:

  saved   : strategies the owner saved by name ("Save to library"). Each keeps the date and time (SGT), the pair,
            account, platform and timeframe it came from, and a one-line summary.
  history : made automatically. Every time a pair's strategy is changed or removed, the OLD version is kept here
            with its date and time (SGT) and the pair, so it can always be loaded again.

Nothing here is deleted unless the owner deletes it himself (Delete button, with a confirm). There is no limit and
no automatic clean-up. The library never trades: loading a strategy only fills the Strategy Hub form, and the
owner still saves it on a pair himself.

Written atomically (temp file + rename), so a crash never leaves half a file.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.fsutil import atomic_write, harden_permissions
from app.store import DATA_DIR
from app import strategy_hub as hub

log = logging.getLogger("strategy_library")

LIBRARY_FILE = DATA_DIR / "strategy_library.json"
SGT = timezone(timedelta(hours=8))
KINDS = ("saved", "history")
NAME_MAX = 60

_lock = threading.Lock()


class LibraryError(ValueError):
    """Shown to the owner. status = HTTP status for the route."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def sgt_text(ts: float) -> str:
    return datetime.fromtimestamp(ts, SGT).strftime("%Y-%m-%d %H:%M SGT")


def _read() -> dict:
    """The whole library. A missing file = an empty library. An unreadable file is NEVER overwritten (that would
    lose the owner's strategies): LibraryError is raised instead."""
    if not LIBRARY_FILE.exists():
        return {"saved": [], "history": []}
    try:
        raw = json.loads(LIBRARY_FILE.read_text())
    except Exception as e:
        log.error("The strategy library file %s cannot be read (%s). It is left untouched.", LIBRARY_FILE, e)
        raise LibraryError(500, "The strategy library file cannot be read. Nothing was changed, so nothing in it "
                                "is lost. Ask your IT to look at data/strategy_library.json.")
    if not isinstance(raw, dict):
        raise LibraryError(500, "The strategy library file is not in the expected format. Nothing was changed.")
    return {k: list(raw.get(k) or []) for k in KINDS}


def _write(lib: dict) -> None:
    atomic_write(LIBRARY_FILE, json.dumps(lib), secret=False)
    harden_permissions(LIBRARY_FILE)


def _summary(plan: dict) -> str:
    try:
        return hub.plan_summary(hub.upgrade_plan(plan))
    except Exception:
        return "(summary not available)"


def _entry(kind: str, name: str, plan: dict, platform: str, account_id: str, account_name: str, symbol: str,
           timeframe: str | None, note: str = "") -> dict:
    ts = time.time()
    return {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "name": name,
        "saved_at": ts,
        "saved_at_sgt": sgt_text(ts),
        "platform": platform,
        "account_id": account_id,
        "account_name": account_name or "",
        "symbol": symbol,
        "timeframe": timeframe or "",
        "chart_type": plan.get("chart_type", ""),
        "summary": _summary(plan),
        "note": note,
        "plan": copy.deepcopy(plan),
    }


def _view(e: dict) -> dict:
    """An entry as the dashboard gets it: the plan in today's format (an old plan is upgraded for showing and
    loading; the stored copy is not changed)."""
    out = dict(e)
    try:
        out["plan"] = hub.upgrade_plan(e.get("plan") or {})
    except Exception:
        out["plan"] = e.get("plan")
    return out


def list_all() -> dict:
    with _lock:
        lib = _read()
    newest_first = lambda rows: sorted(rows, key=lambda r: r.get("saved_at") or 0, reverse=True)
    return {k: [_view(e) for e in newest_first(lib[k])] for k in KINDS}


def save(name: str, plan: dict, platform: str, account_id: str, account_name: str, symbol: str,
         timeframe: str | None) -> dict:
    """Save to library. Only a complete strategy is kept (same check as saving it on a pair), so anything in the
    library can be loaded and used."""
    name = (name or "").strip()
    if not name:
        raise LibraryError(400, "Give the strategy a name first.")
    if len(name) > NAME_MAX:
        raise LibraryError(400, f"The name is too long (at most {NAME_MAX} characters).")
    if platform not in ("binance", "okx"):
        raise LibraryError(400, "platform must be binance or okx")
    try:
        hub.validate_plan(plan)
    except hub.PlanError as e:
        raise LibraryError(400, "Not saved to the library - the strategy is not complete: " + str(e))
    plan = copy.deepcopy(plan)
    plan["name"] = name
    e = _entry("saved", name, plan, platform, account_id, account_name, symbol, timeframe)
    with _lock:
        lib = _read()
        lib["saved"].append(e)
        _write(lib)
    return _view(e)


def add_history(old_plan: dict | None, platform: str, account_id: str, account_name: str, symbol: str,
                timeframe: str | None, note: str) -> dict | None:
    """Keeps the OLD strategy of a pair before it is changed or removed. Never raises: the library is a record,
    a problem here must never stop the owner's change (it is logged)."""
    if not old_plan:
        return None
    try:
        name = (old_plan.get("name") or "").strip() or "(no name)"
        e = _entry("history", name, old_plan, platform, account_id, account_name, symbol, timeframe, note)
        with _lock:
            lib = _read()
            lib["history"].append(e)
            _write(lib)
        return e
    except Exception as ex:
        log.error("Could not keep the old strategy of %s/%s/%s in the library history: %s",
                  platform, account_id, symbol, ex)
        return None


def delete(entry_id: str) -> dict:
    """Only by the owner (the dashboard asks him to confirm first)."""
    with _lock:
        lib = _read()
        for k in KINDS:
            for i, e in enumerate(lib[k]):
                if e.get("id") == entry_id:
                    gone = lib[k].pop(i)
                    _write(lib)
                    return {"deleted": True, "kind": k, "name": gone.get("name")}
    raise LibraryError(404, "That strategy is not in the library (already deleted?).")
