"""
singleton_lock.py
===================

Owner request (2026-09-16): prevent two copies of this bot from trading
at once - e.g. a Railway redeploy briefly overlapping with the old
container still shutting down, or accidentally running a second instance
by hand.

DELIBERATELY NOT a PID-file lock. A PID file only makes sense when both
the old and new process share the same machine's process table - on
Railway, a new deploy typically starts in a completely separate
container from the old one, so checking a PID from one container against
a different container's process list is meaningless; they don't share
that information at all.

What actually works here: a HEARTBEAT, not a PID. Both the old and new
container DO share the same persistent Railway volume (that's the same
reason credentials survive a redeploy) - so the check becomes "has any
process written a fresh timestamp to this shared file recently", not "is
this specific PID alive". A running instance refreshes the timestamp
regularly; a brand-new process checks it once at startup:
  - stale (or missing) -> nothing else is genuinely alive -> safe to
    claim the lock and proceed
  - fresh -> something else is alive RIGHT NOW -> refuse to start trading

STALE_THRESHOLD_SECONDS is deliberately generous (60s) relative to the
refresh interval (20s) - real heartbeat jitter (a slow GC pause, a busy
event loop) should never falsely trigger a conflict; a genuine second
instance still gets caught within one threshold window.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path

log = logging.getLogger("singleton_lock")

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"

STALE_THRESHOLD_SECONDS = 60
REFRESH_INTERVAL_SECONDS = 20

# Set once at process startup - identifies THIS process's own heartbeat
# writes, purely for diagnostics (shown in the conflict message so it's
# clear this isn't the same process talking to itself).
_INSTANCE_ID = uuid.uuid4().hex[:12]


def _read_heartbeat() -> dict | None:
    try:
        return json.loads(HEARTBEAT_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_heartbeat():
    payload = {
        "instance_id": _INSTANCE_ID,
        "hostname": socket.gethostname(),
        "updated_at": time.time(),
    }
    try:
        HEARTBEAT_FILE.write_text(json.dumps(payload))
    except OSError as e:
        log.warning("Could not write singleton-lock heartbeat (%s) - if this persists, the "
                    "single-instance safety check itself may not be able to do its job.", e)


def _claim_path() -> Path:
    """Resolved at call time (tests repoint HEARTBEAT_FILE)."""
    return HEARTBEAT_FILE.with_name(HEARTBEAT_FILE.stem + ".claim")


def _read_claim(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _try_create_claim(path: Path) -> bool:
    """ATOMIC claim (MUST-HAVE FIX, 2026-09-28): os.open with
    O_CREAT | O_EXCL succeeds for exactly ONE process even if two start at
    the same instant on the same shared volume - the second gets
    FileExistsError. The old version read the heartbeat and then wrote it
    as two separate steps, so two processes starting together could both
    see "no fresh heartbeat" and both claim the lock."""
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, json.dumps({"instance_id": _INSTANCE_ID, "hostname": socket.gethostname(),
                                 "claimed_at": time.time()}).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def check_and_claim_lock(quiet: bool = False) -> tuple[bool, str | None]:
    """Called once at startup, before any trading engine starts. Returns
    (claimed: bool, conflict_message: str | None). If claimed is False,
    the caller must NOT start manager.start_all_enabled()/okx_manager's
    equivalent - but should still bring up the web server/dashboard
    normally, so the conflict is actually visible.

    Two layers:
      1. heartbeat check (unchanged): a FRESH heartbeat from another
         instance means something else is alive right now -> refuse.
      2. atomic claim file (new): only one process can create it. A claim
         left behind by a crashed process is only removed once it is older
         than the staleness threshold AND its owner's heartbeat is stale."""
    global _lock_held
    # quiet=True is used by the startup retry loop (app/main.py): a conflict is then
    # logged at DEBUG instead of ERROR so retrying every few seconds cannot flood the log.
    _log_conflict = log.debug if quiet else log.error
    existing = _read_heartbeat()
    if existing is not None and existing.get("instance_id") != _INSTANCE_ID:
        age = time.time() - existing.get("updated_at", 0)
        if age < STALE_THRESHOLD_SECONDS:
            message = (
                f"Another instance of this bot appears to be running right now "
                f"(heartbeat from instance {existing.get('instance_id', '?')} on host "
                f"{existing.get('hostname', '?')}, last updated {age:.0f}s ago - still fresh). "
                f"Trading has NOT been started in this process to avoid two copies trading the "
                f"same accounts at once. If you're sure the other instance is actually gone, "
                f"wait a bit longer or delete data/heartbeat.json and restart this process."
            )
            _log_conflict(message)
            _lock_held = False
            return False, message
        log.info("Found a stale heartbeat (%.0fs old, past the %ds threshold) - safe to proceed.",
                 age, STALE_THRESHOLD_SECONDS)

    claim = _claim_path()
    claimed = False
    for _ in range(2):
        try:
            claimed = _try_create_claim(claim)
            break
        except FileExistsError:
            info = _read_claim(claim) or {}
            if info.get("instance_id") == _INSTANCE_ID:
                claimed = True
                break
            try:
                claim_age = time.time() - float(info.get("claimed_at") or claim.stat().st_mtime)
            except (OSError, TypeError, ValueError):
                claim_age = 0.0
            hb = _read_heartbeat()
            holder_alive = (hb is not None and hb.get("instance_id") == info.get("instance_id")
                            and time.time() - hb.get("updated_at", 0) < STALE_THRESHOLD_SECONDS)
            if holder_alive or claim_age < STALE_THRESHOLD_SECONDS:
                message = (
                    f"Another instance of this bot claimed the single-instance lock "
                    f"{claim_age:.0f}s ago (instance {info.get('instance_id', '?')} on host "
                    f"{info.get('hostname', '?')}). Trading has NOT been started in this process. "
                    f"If that instance is gone, wait {STALE_THRESHOLD_SECONDS}s and restart."
                )
                _log_conflict(message)
                _lock_held = False
                return False, message
            log.info("Removing a stale claim left by instance %s (%.0fs old, heartbeat stale).",
                     info.get("instance_id", "?"), claim_age)
            try:
                claim.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                message = f"Could not clear a stale single-instance claim ({e}) - not starting trading."
                log.error(message)
                _lock_held = False
                return False, message
    if not claimed:
        message = "Could not atomically claim the single-instance lock - not starting trading."
        log.error(message)
        _lock_held = False
        return False, message

    _write_heartbeat()
    log.info("Singleton lock claimed by instance %s.", _INSTANCE_ID)
    _lock_held = True
    return True, None


def is_lock_held() -> bool:
    """2026-09-16 fix, flagged by a third-party review (confirmed real and
    serious): manual start/restart endpoints (start_pair, start_all, and
    their OKX equivalents) previously never checked whether a singleton
    conflict was detected at startup - they just called the manager
    directly. That meant a conflicting SECOND process could correctly skip
    its own automatic trading startup, yet still trade anyway the moment
    someone (or something) hit a manual start endpoint on it - completely
    defeating the point of the lock. Every manual start/restart path now
    calls this first and refuses with 409 if it's False. Exposed as a
    plain module-level flag (not imported from main.py directly) so
    app/api/__init__.py and app/api/okx.py can check it without any
    circular import between the API layer and the app entry point.

    Defaults to True (not False) - "no conflict known" - rather than
    "assume a conflict exists". In production this default never actually
    matters, since main.py's lifespan always calls check_and_claim_lock()
    before anything else can happen; it only matters for anything (tests,
    scripts) that constructs a manager directly without ever going through
    that startup sequence at all, where "conflict-checking was never even
    performed" should mean "proceed normally", not "block everything"."""
    return _lock_held


_lock_held = True


async def heartbeat_loop():
    """Run as a background asyncio task for the lifetime of the process,
    once the lock has been claimed - keeps the heartbeat fresh so a FUTURE
    restart's check_and_claim_lock() correctly sees this instance as
    genuinely still alive."""
    import asyncio
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        _write_heartbeat()


def release_lock_on_clean_shutdown():
    """Best-effort - deletes the heartbeat file on a graceful shutdown so
    an immediate restart doesn't need to wait out the full staleness
    window unnecessarily. Not relied on for correctness (a crash skips
    this entirely, same as any other cleanup step) - the staleness
    threshold in check_and_claim_lock() is the actual safety net; this is
    purely a convenience for the common, graceful-restart case."""
    global _lock_held
    _lock_held = False
    try:
        current = _read_heartbeat()
        if current is not None and current.get("instance_id") == _INSTANCE_ID:
            HEARTBEAT_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        claim = _claim_path()
        info = _read_claim(claim)
        if info is not None and info.get("instance_id") == _INSTANCE_ID:
            claim.unlink(missing_ok=True)
    except OSError:
        pass
