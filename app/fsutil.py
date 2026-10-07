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
    finally:
        # If the replace already happened, tmp_path no longer exists and this
        # is a harmless no-op; if something raised before the replace, this
        # cleans up the partial temp file rather than leaving debris behind.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
