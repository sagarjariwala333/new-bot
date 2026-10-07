"""
auth.py
=======

Minimal, dependency-free login for the dashboard. Single admin account
(this is a personal trading dashboard, not a multi-tenant product) with:

  - PBKDF2-SHA256 password hashing (200k iterations), timing-safe compare
  - Server-side session tokens (random 256-bit), held in memory + a cookie
  - Brute-force lockout: 5 failed attempts -> 15 minute lock, per username

Credentials are seeded once from DASHBOARD_USERNAME / DASHBOARD_PASSWORD in
.env on first run and then stored (hashed) in data/auth.json - the
plaintext password is never written to disk and never returned by any API
response. If no password is set in .env on first run, a random one is
generated and printed to the server log ONCE so you can log in and change
it via /api/auth/change-password.

Password reset via email (2026-09-15, owner request): before this, the
only recovery path if locked out was server-level access (delete
data/auth.json and restart, reseeding from .env). /api/auth/forgot-
password now emails a random, single-use, 30-minute token via the same
email channel built for trade alerts (email_notifier.py) - deliberately
public, same security model as /auth/login itself (no session exists yet
at this point, so CSRF isn't meaningful here). Protected instead by a
per-request cooldown and the token's own randomness/short lifetime, not a
login wall. Requires SMTP to actually be configured to deliver anywhere -
without it, server-level recovery remains the only option, same as
before this existed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from fastapi import Request, HTTPException

from app.fsutil import atomic_write, harden_permissions

log = logging.getLogger("auth")

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
harden_permissions(DATA_DIR, is_dir=True)
AUTH_FILE = DATA_DIR / "auth.json"

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf_token"
SESSION_TTL_SECONDS = 12 * 3600
PBKDF2_ITERATIONS = 200_000
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(expected.hex(), digest_hex)


@dataclass
class _AuthRecord:
    username: str
    password_hash: str


class AuthStore:
    def __init__(self):
        self._sessions: dict[str, float] = {}          # token -> expires_at
        self._failed: dict[str, list[float]] = {}       # username -> [failure timestamps]
        self._lockout_until: dict[str, float] = {}       # username -> unlock timestamp
        # 2026-09-15, owner request: password reset via email, before this
        # went to no server-level-access recovery at all (delete
        # data/auth.json + restart). token -> expires_at, same pattern as
        # _sessions above.
        self._reset_tokens: dict[str, float] = {}
        self._last_reset_request_at: float = 0.0
        self._record = self._load_or_seed()

    def _load_or_seed(self) -> _AuthRecord:
        if AUTH_FILE.exists():
            data = json.loads(AUTH_FILE.read_text())
            return _AuthRecord(**data)
        username = os.environ.get("DASHBOARD_USERNAME", "admin")
        password = os.environ.get("DASHBOARD_PASSWORD")
        generated = False
        if not password:
            password = secrets.token_urlsafe(12)
            generated = True
        record = _AuthRecord(username=username, password_hash=_hash_password(password))
        atomic_write(AUTH_FILE, json.dumps(asdict(record), indent=2), secret=True)
        if generated:
            log.warning(
                "No DASHBOARD_PASSWORD set in .env - generated one for first run.\n"
                "  username: %s\n  password: %s\n"
                "Log in once, then change it (or set DASHBOARD_PASSWORD in .env and "
                "delete data/auth.json to reseed).",
                username, password,
            )
        return record

    # ---------------------------------------------------------------- login
    def is_locked_out(self, username: str) -> tuple[bool, float]:
        until = self._lockout_until.get(username, 0)
        remaining = until - time.time()
        return remaining > 0, max(remaining, 0)

    def attempt_login(self, username: str, password: str) -> str | None:
        locked, remaining = self.is_locked_out(username)
        if locked:
            raise HTTPException(429, f"Too many failed attempts. Try again in {int(remaining)}s.")

        ok = (username == self._record.username) and _verify_password(password, self._record.password_hash)
        if not ok:
            fails = self._failed.setdefault(username, [])
            fails.append(time.time())
            fails[:] = [t for t in fails if time.time() - t < LOCKOUT_SECONDS]
            if len(fails) >= MAX_FAILED_ATTEMPTS:
                self._lockout_until[username] = time.time() + LOCKOUT_SECONDS
                log.warning("Account %s locked out for %ds after repeated failed logins.",
                            username, LOCKOUT_SECONDS)
            return None

        self._failed.pop(username, None)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + SESSION_TTL_SECONDS
        return token

    def logout(self, token: str):
        self._sessions.pop(token, None)

    def validate(self, token: str | None) -> bool:
        if not token:
            return False
        expires = self._sessions.get(token)
        if expires is None or expires < time.time():
            self._sessions.pop(token, None)
            return False
        return True

    def change_password(self, current_password: str, new_password: str) -> bool:
        if not _verify_password(current_password, self._record.password_hash):
            return False
        self._record.password_hash = _hash_password(new_password)
        atomic_write(AUTH_FILE, json.dumps(asdict(self._record), indent=2), secret=True)
        return True

    # ---------------------------------------------------------------- forgot password (2026-09-15)
    RESET_TOKEN_TTL_SECONDS = 30 * 60      # 30 minutes - long enough to check email, short enough to matter
    RESET_REQUEST_COOLDOWN_SECONDS = 5 * 60  # prevents spamming the recovery inbox

    def request_password_reset(self, username: str) -> str | None:
        """Returns a fresh token to email, or None if EITHER the username
        doesn't match OR a request was already made within the cooldown
        window. The caller (the API route) must return the SAME generic
        response either way - this method deliberately doesn't distinguish
        "wrong username" from "rate limited" in what it reveals, since a
        response that differs would let an attacker enumerate whether a
        given username exists on this dashboard at all."""
        if username != self._record.username:
            return None
        now = time.time()
        if now - self._last_reset_request_at < self.RESET_REQUEST_COOLDOWN_SECONDS:
            return None
        self._last_reset_request_at = now
        token = secrets.token_urlsafe(32)
        # Only one live reset token at a time - requesting a new one
        # invalidates any previous unused one, rather than accumulating
        # valid tokens indefinitely.
        self._reset_tokens = {token: now + self.RESET_TOKEN_TTL_SECONDS}
        return token

    def reset_password_with_token(self, token: str, new_password: str) -> bool:
        expires = self._reset_tokens.get(token)
        if expires is None or expires < time.time():
            self._reset_tokens.pop(token, None)
            return False
        self._record.password_hash = _hash_password(new_password)
        atomic_write(AUTH_FILE, json.dumps(asdict(self._record), indent=2), secret=True)
        self._reset_tokens.pop(token, None)
        # A successful reset is a legitimate reason to also clear any
        # standing lockout - the owner just proved they control the
        # recovery email, which is at least as strong a proof of identity
        # as the password itself.
        self._failed.pop(self._record.username, None)
        self._lockout_until.pop(self._record.username, None)
        return True


auth_store = AuthStore()


def require_auth(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not auth_store.validate(token):
        raise HTTPException(401, "Not authenticated")


def require_csrf(request: Request):
    """Double-submit-cookie CSRF check for state-changing requests. On login,
    a second, non-httponly cookie (CSRF_COOKIE) is issued alongside the session
    cookie; the dashboard JS reads it and echoes it back as the X-CSRF-Token
    header on every POST/PUT/DELETE. A cross-site request can get the browser
    to send cookies automatically, but it cannot read this cookie's value to
    put in a custom header (browsers block cross-origin reads of it), so a
    mismatch or missing header means the request didn't originate from this
    dashboard's own JS."""
    if request.method.upper() in ("POST", "PUT", "DELETE", "PATCH"):
        cookie_val = request.cookies.get(CSRF_COOKIE)
        header_val = request.headers.get("x-csrf-token")
        if not cookie_val or not header_val or not hmac.compare_digest(cookie_val, header_val):
            raise HTTPException(403, "CSRF check failed - missing or mismatched X-CSRF-Token header")


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(24)
