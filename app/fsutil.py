"""
fsutil.py
=========

Small shared helpers so every place that persists sensitive/important data
(account config with encrypted credentials, the auth store, the master
encryption key, the trade ledger) does it the same safe way:

  - atomic_write: write to a temp file in the same directory, fsync it, then
    os.replace() over the target. os.replace is atomic on POSIX and on
    Windows (as of Python's implementation), so a crash mid-write can never
    leave a half-written, corrupted JSON file in place - readers either see
    the old complete file or the new complete file, never a torn one.
  - harden_permissions: best-effort chmod to owner-only (0o600 for files,
    0o700 for directories). Best-effort because not every filesystem/OS
    supports POSIX permission bits the same way (e.g. Windows) - failures
    here are logged, not fatal.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

log = logging.getLogger("fsutil")


def harden_permissions(path: Path, is_dir: bool = False):
    try:
        mode = 0o700 if is_dir else 0o600
        os.chmod(path, mode)
    except OSError as e:
        log.warning("Could not set restrictive permissions on %s: %s", path, e)


def atomic_write(path: Path, content: str, secret: bool = True):
    """Writes `content` to `path` atomically. If `secret` is True, the file
    is chmod'd to owner-read/write-only (0o600) immediately after the atomic
    rename, before any other process could plausibly read it."""
    path = Path(path)
    tmp_path = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        with open(tmp_path, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if secret:
            harden_permissions(tmp_path)
        os.replace(tmp_path, path)
        if secret:
            harden_permissions(path)
    except Exception as e:
        _alert_save_failed(path, e)
        raise
    finally:
        # If the replace already happened, tmp_path no longer exists and this
        # is a harmless no-op; if something raised before the replace, this
        # cleans up the partial temp file rather than leaving debris behind.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


_save_alerted: dict = {}
SAVE_ALERT_GAP = 900.0      # at most one alert per file every 15 minutes


def _alert_save_failed(path: Path, err: Exception) -> None:
    """A state file could not be saved (disk full / read-only / volume gone): tell the owner (2026-11-11).
    Never raises - the caller still gets the original error."""
    try:
        import time
        now = time.monotonic()
        key = str(path)
        if now - _save_alerted.get(key, -1e9) < SAVE_ALERT_GAP:
            return
        _save_alerted[key] = now
        from app import api_guard
        api_guard.send_alert(f"A bot file could not be saved: {path.name} ({type(err).__name__}: {err}). "
                             f"Check the disk / volume. Trading memory may not survive a restart.", "File not saved")
    except Exception:
        pass
