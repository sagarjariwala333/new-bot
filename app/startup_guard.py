"""
startup_guard.py
================

Persistent-storage guard. The tracker (its balance, its all-time high, the shadow
state, the last processed candle), the ledger, the trade state and the encrypted
exchange keys all live in DATA_DIR. On Railway the container's own disk is wiped
on every deploy, so DATA_DIR MUST be on an attached Volume - otherwise every
redeploy silently restarts every tracker from its starting amount, which changes
when shadow mode starts.

What this does
  * Detects "running on Railway" (Railway injects RAILWAY_PROJECT_ID,
    RAILWAY_ENVIRONMENT_NAME and RAILWAY_SERVICE_ID).
  * On Railway, DATA_DIR must be inside the attached volume:
      1. RAILWAY_VOLUME_MOUNT_PATH is set and DATA_DIR is inside it, OR
      2. (fallback, in case Railway does not inject that variable) DATA_DIR sits on
         a different mounted filesystem from the container's root disk.
    Otherwise trading is NOT started and the dashboard shows why (/healthz field
    "storage_problem"). The dashboard itself still comes up.
  * Off Railway (your own server, local run) this check does nothing.

Deliberate escape hatch
  Setting ALLOW_EPHEMERAL_DATA_DIR to exactly the phrase in OVERRIDE_PHRASE turns
  the check off. The phrase is awkward on purpose: it exists only for a throw-away
  test deployment and must never be set on a live deployment.

This module never changes any trading decision; it only decides whether the
process is allowed to start trading at all.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("startup_guard")

OVERRIDE_VAR = "ALLOW_EPHEMERAL_DATA_DIR"
OVERRIDE_PHRASE = "I_UNDERSTAND_THE_TRACKER_WILL_BE_LOST"
_RAILWAY_MARKERS = ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_SERVICE_ID")

_problem: str | None = None


def running_on_railway(env=None) -> bool:
    env = os.environ if env is None else env
    return any(env.get(k) for k in _RAILWAY_MARKERS)


def _device(path: Path) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def check_storage(data_dir: Path, env=None, device_of=_device) -> str | None:
    """Returns a plain-English problem description, or None if storage is acceptable.
    `env` and `device_of` are parameters only so the tests can simulate Railway."""
    env = os.environ if env is None else env
    if not running_on_railway(env):
        return None
    if env.get(OVERRIDE_VAR, "") == OVERRIDE_PHRASE:
        log.warning("%s is set: persistent-storage check is OFF. Trackers WILL be lost on redeploy.",
                    OVERRIDE_VAR)
        return None

    try:
        data_dir = Path(data_dir).resolve()
    except OSError:
        data_dir = Path(data_dir)

    mount = (env.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if mount:
        try:
            if data_dir == Path(mount).resolve() or Path(mount).resolve() in data_dir.parents:
                return None
        except OSError:
            pass
        return (f"DATA_DIR ({data_dir}) is not inside the attached Railway volume ({mount}). "
                f"The tracker, ledger and trade state would be lost on every redeploy. "
                f"Set DATA_DIR to the volume's mount path (e.g. DATA_DIR={mount}) and redeploy.")

    # No volume variable. Fall back to comparing filesystems: a Railway volume is a
    # separate mount, the container's own disk is the root filesystem.
    dd, root = device_of(data_dir), device_of(Path("/"))
    if dd is not None and root is not None and dd != root:
        log.warning("RAILWAY_VOLUME_MOUNT_PATH is not set, but DATA_DIR is on a separate "
                    "filesystem - accepting it as persistent storage.")
        return None
    return (f"DATA_DIR ({data_dir}) is on the container's own disk, not on a Railway volume. "
            f"The tracker, ledger and trade state would be lost on every redeploy. "
            f"Add a Volume to this service (mount path e.g. /data), set DATA_DIR to that path, and redeploy.")


def evaluate_at_startup(data_dir: Path) -> str | None:
    """Called once by the app's startup. Remembers the result for require_storage_ok()."""
    global _problem
    _problem = check_storage(data_dir)
    if _problem:
        log.error("TRADING WILL NOT START: %s", _problem)
    return _problem


def current_problem() -> str | None:
    return _problem


def set_problem_for_tests(value: str | None) -> None:
    global _problem
    _problem = value


def require_storage_ok() -> None:
    """Raises RuntimeError if startup found the data folder is not persistent.
    Called from the managers' start gate, so every start / restart path honours it."""
    if _problem:
        raise RuntimeError("Refusing to start - " + _problem)
