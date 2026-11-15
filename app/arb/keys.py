"""API keys for LIVE arbitrage (Step 3), entered in the tab and stored encrypted (same Fernet master key as the hub's
accounts). The browser never gets a key back: only "set" and the last 4 characters.

Use a SEPARATE sub-account's key when you can. If you enter the same key as a hub account, the arbitrage notices and
skips every coin that hub account trades (see hubmatch.py).
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from app.arb.settings import ARB_DIR
from app.fsutil import atomic_write

SLOTS = {
    "binance_futures": ("key", "secret"),
    "binance_spot": ("key", "secret"),            # optional: blank = the futures key is used for spot too
    "okx": ("key", "secret", "passphrase"),
}
LABELS = {"binance_futures": "Binance futures", "binance_spot": "Binance spot", "okx": "OKX"}


class KeyError_(ValueError):
    pass


def _enc(v: str) -> str:
    from app.store import encrypt
    return encrypt(v)


def _dec(v: str) -> str:
    from app.store import decrypt
    return decrypt(v)


class ArbKeyStore:
    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or (ARB_DIR / "keys.json")

    def _raw(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise KeyError_("The arbitrage key file is damaged. Enter the keys again.")
        return d if isinstance(d, dict) else {}

    def get(self, slot: str) -> dict | None:
        """Plain-text values for a slot, or None if not (completely) set. Binance spot falls back to futures."""
        with self._lock:
            raw = self._raw()
        want = SLOTS[slot]
        row = raw.get(slot) or {}
        if all(row.get(f) for f in want):
            try:
                return {f: _dec(row[f]) for f in want}
            except Exception:
                raise KeyError_("The saved %s key cannot be read (the hub's master key changed?). Enter it again." % LABELS[slot])
        if slot == "binance_spot":
            return self.get("binance_futures")
        return None

    def status(self) -> dict:
        out = {}
        with self._lock:
            try:
                raw = self._raw()
            except KeyError_:
                raw = {}
        for slot, fields in SLOTS.items():
            row = raw.get(slot) or {}
            ok = all(row.get(f) for f in fields)
            tail = ""
            if ok:
                try:
                    tail = _dec(row["key"])[-4:]
                except Exception:
                    ok = False
            out[slot] = {"label": LABELS[slot], "set": ok, "tail": tail,
                         "fields": list(fields), "uses_futures_key": slot == "binance_spot" and not ok and
                         bool((raw.get("binance_futures") or {}).get("key"))}
        return out

    def update(self, slot: str, values: dict) -> None:
        if slot not in SLOTS:
            raise KeyError_("Unknown key slot")
        want = SLOTS[slot]
        clean = {}
        for f in want:
            v = str((values or {}).get(f) or "").strip()
            if not v:
                raise KeyError_("%s: %s is missing" % (LABELS[slot], f))
            if len(v) > 300 or any(c in v for c in " \n\r\t"):
                raise KeyError_("%s: %s has spaces or is too long" % (LABELS[slot], f))
            clean[f] = v
        with self._lock:
            raw = self._raw()
            raw[slot] = {f: _enc(v) for f, v in clean.items()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(raw, indent=1), secret=True)

    def clear(self, slot: str) -> None:
        if slot not in SLOTS:
            raise KeyError_("Unknown key slot")
        with self._lock:
            raw = self._raw()
            raw.pop(slot, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(raw, indent=1), secret=True)

    def fingerprint(self) -> str:
        """Changes when any key changes (engines are rebuilt then). Not reversible."""
        with self._lock:
            try:
                raw = self._raw()
            except KeyError_:
                raw = {}
        return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]


arb_keys = ArbKeyStore()
