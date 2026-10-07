"""
default_settings.py
===================

Owner-entered default settings. THIS PACKAGE SHIPS WITH NO VALUES.

Every strategy / sizing / risk / tracker / operational number is blank until
the owner types it into the dashboard ("Default Settings") and presses
CONFIRM. Nothing in the source code supplies a fallback.

What this module provides
  * FIELD_SPECS      - the single list of every owner-supplied setting, with
                       its type and allowed range (ranges are generic limits,
                       not strategy values).
  * DefaultSettingsStore (persisted in DATA_DIR/default_settings.json)
      - one set of defaults per platform ("binance", "okx")
      - save()    : stores values and marks that platform UNCONFIRMED
      - confirm() : refuses unless EVERY field is filled and valid
      - the alert email address (entered here, never in source code)
      - the live-trading master switch (OFF until the owner turns it on)
  * missing_fields() / validate_pair_values() - used by the pair create route
    and by the managers' start() choke point, so a pair with any blank or
    invalid setting can never be started.

Editing defaults later marks that platform UNCONFIRMED again. That blocks
CREATING new pairs until the owner presses Confirm again; it never stops or
blocks a pair that already exists with a complete set of its own values.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.fsutil import atomic_write

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
SETTINGS_FILE = DATA_DIR / "default_settings.json"

PLATFORMS = ("binance", "okx")

VALID_TIMEFRAMES = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
}
OKX_VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
TRIGGER_PX_TYPES = {"mark", "last", "index"}

# kind: int | float | whole (float that must be a whole number) | bool | timeframe | trigger
# lo/hi: inclusive bounds, lo_exclusive makes the lower bound exclusive.
# These are generic sanity limits only.
FIELD_SPECS: dict[str, dict] = {
    "timeframe":             {"kind": "timeframe"},
    "hma_length":            {"kind": "int", "lo": 1, "hi": 500},
    "ema_filter_length":     {"kind": "int", "lo": 1, "hi": 2000},
    "ema_sizing_length":     {"kind": "int", "lo": 1, "hi": 2000},
    "atr_length":            {"kind": "int", "lo": 1, "hi": 500},
    "use_hold_rule":         {"kind": "bool"},
    "adx_di_length":         {"kind": "int", "lo": 1, "hi": 500},
    "adx_smoothing":         {"kind": "int", "lo": 1, "hi": 500},
    "hold_adx_level":        {"kind": "float", "lo": 0, "hi": 100},
    "trend_alloc_pct":       {"kind": "float", "lo": 0.1, "hi": 100},
    "counter_alloc_pct":     {"kind": "float", "lo": 0.1, "hi": 100},
    "leverage":              {"kind": "whole", "lo": 1, "hi": 125},
    "counter_leverage":      {"kind": "whole", "lo": 1, "hi": 125},
    "isolated_margin":       {"kind": "bool"},
    "wait_first_reversal":   {"kind": "bool"},
    "stop_loss_pct_equity":  {"kind": "float", "lo": 0.1, "hi": 100},
    "tp1_atr_mult":          {"kind": "float", "lo": 0.1, "hi": 100},
    "tp2_atr_mult":          {"kind": "float", "lo": 0.1, "hi": 100},
    "use_shadow":            {"kind": "bool"},
    "shadow_trades":         {"kind": "int", "lo": 1, "hi": 100},
    "shadow_slippage_ticks": {"kind": "int", "lo": 0, "hi": 100000},
    "tracker_start_balance": {"kind": "float", "lo": 0, "lo_exclusive": True, "hi": 1e12},
    "poll_seconds":          {"kind": "int", "lo": 5, "hi": 3600},
    "telegram_enabled":      {"kind": "bool"},
    "ws_staleness_seconds":  {"kind": "float", "lo": 5, "hi": 600},
    "trigger_px_type":       {"kind": "trigger", "platforms": ("okx",)},
}


class SettingsError(ValueError):
    """A value is blank, out of range, or the wrong type."""


class DefaultsNotConfirmed(RuntimeError):
    """The owner has not pressed Confirm on this platform's default settings."""


def fields_for(platform: str) -> list[str]:
    _check_platform(platform)
    return [n for n, spec in FIELD_SPECS.items() if platform in spec.get("platforms", PLATFORMS)]


def _check_platform(platform: str) -> None:
    if platform not in PLATFORMS:
        raise SettingsError(f"unknown platform {platform!r}")


def validate_value(platform: str, name: str, value):
    """Returns the cleaned value, or raises SettingsError. None / "" means blank
    and is NOT accepted here - callers decide what blank means."""
    _check_platform(platform)
    spec = FIELD_SPECS.get(name)
    if spec is None or platform not in spec.get("platforms", PLATFORMS):
        raise SettingsError(f"{name} is not a setting on {platform}")
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise SettingsError(f"{name} is blank")
    kind = spec["kind"]

    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise SettingsError(f"{name} must be true or false")

    if kind == "timeframe":
        allowed = OKX_VALID_TIMEFRAMES if platform == "okx" else VALID_TIMEFRAMES
        if value not in allowed:
            raise SettingsError(f"{name} must be one of {sorted(allowed)}")
        return value

    if kind == "trigger":
        if value not in TRIGGER_PX_TYPES:
            raise SettingsError(f"{name} must be one of {sorted(TRIGGER_PX_TYPES)}")
        return value

    # numeric kinds - a bool is not a number here
    if isinstance(value, bool):
        raise SettingsError(f"{name} must be a number")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a number")
    if not math.isfinite(num):
        raise SettingsError(f"{name} must be a finite number")
    lo, hi = spec["lo"], spec["hi"]
    if spec.get("lo_exclusive"):
        if not (num > lo):
            raise SettingsError(f"{name} must be greater than {lo}")
    elif num < lo:
        raise SettingsError(f"{name} must be at least {lo}")
    if num > hi:
        raise SettingsError(f"{name} must be at most {hi}")
    if kind in ("int", "whole"):
        if num != int(num):
            raise SettingsError(f"{name} must be a whole number")
        return int(num) if kind == "int" else float(int(num))
    return num


def missing_fields(platform: str, values: dict) -> list[str]:
    """Names of every mandatory field that is blank or invalid in `values`."""
    bad = []
    for name in fields_for(platform):
        try:
            validate_value(platform, name, values.get(name))
        except SettingsError:
            bad.append(name)
    return bad


def require_pair_ready(platform: str, pair_values: dict) -> None:
    """Raises RuntimeError naming every blank/invalid setting on a pair."""
    bad = missing_fields(platform, pair_values)
    if bad:
        raise RuntimeError(
            "Refusing to start - these settings are blank or invalid on this pair: "
            + ", ".join(bad)
            + ". Fill them in on the Default Settings page / pair settings first."
        )


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")


def validate_email(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) > 254 or not _EMAIL_RE.match(v):
        raise SettingsError("that does not look like a valid email address")
    return v


def _now_sgt() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S SGT")


def _blank_platform() -> dict:
    return {"values": {}, "confirmed": False, "confirmed_at": None}


class DefaultSettingsStore:
    def __init__(self, path: Path | None = None):
        self._path = path or SETTINGS_FILE
        self._lock = threading.Lock()
        self._data = self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict:
        data = {"binance": _blank_platform(), "okx": _blank_platform(),
                "alert_email": "", "live_trading_enabled": False}
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text())
            except (OSError, ValueError):
                raw = {}
            for plat in PLATFORMS:
                p = raw.get(plat) or {}
                data[plat] = {"values": dict(p.get("values") or {}),
                              "confirmed": bool(p.get("confirmed")),
                              "confirmed_at": p.get("confirmed_at")}
            data["alert_email"] = str(raw.get("alert_email") or "")
            data["live_trading_enabled"] = bool(raw.get("live_trading_enabled"))
        return data

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(self._data, indent=2), secret=True)

    # ------------------------------------------------------------ defaults
    def get(self, platform: str) -> dict:
        """What the dashboard shows: every field (None = blank), confirmed flag,
        and which fields are still missing."""
        _check_platform(platform)
        with self._lock:
            p = self._data[platform]
            values = {n: p["values"].get(n) for n in fields_for(platform)}
            return {"platform": platform, "values": values, "confirmed": p["confirmed"],
                    "confirmed_at": p["confirmed_at"],
                    "missing": missing_fields(platform, values)}

    def save(self, platform: str, updates: dict) -> dict:
        """Stores the given values (blank/None clears a field). Always marks the
        platform UNCONFIRMED. Raises SettingsError listing every bad field and
        stores nothing if any value is invalid."""
        _check_platform(platform)
        allowed = set(fields_for(platform))
        errors, clean = [], {}
        for name, raw in (updates or {}).items():
            if name not in allowed:
                errors.append(f"{name} is not a setting on {platform}")
                continue
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                clean[name] = None
                continue
            try:
                clean[name] = validate_value(platform, name, raw)
            except SettingsError as e:
                errors.append(str(e))
        if errors:
            raise SettingsError("; ".join(errors))
        with self._lock:
            p = self._data[platform]
            for name, v in clean.items():
                if v is None:
                    p["values"].pop(name, None)
                else:
                    p["values"][name] = v
            p["confirmed"] = False
            p["confirmed_at"] = None
            self._save()
        return self.get(platform)

    def confirm(self, platform: str) -> dict:
        _check_platform(platform)
        with self._lock:
            p = self._data[platform]
            values = {n: p["values"].get(n) for n in fields_for(platform)}
            bad = missing_fields(platform, values)
            if bad:
                raise SettingsError("cannot confirm - blank or invalid: " + ", ".join(bad))
            p["confirmed"] = True
            p["confirmed_at"] = _now_sgt()
            self._save()
        return self.get(platform)

    def is_confirmed(self, platform: str) -> bool:
        _check_platform(platform)
        with self._lock:
            return bool(self._data[platform]["confirmed"])

    def confirmed_values(self, platform: str) -> dict:
        """The values a NEW pair is created from. Raises DefaultsNotConfirmed
        unless the owner has confirmed (and every field is still filled)."""
        _check_platform(platform)
        with self._lock:
            p = self._data[platform]
            values = {n: p["values"].get(n) for n in fields_for(platform)}
            if not p["confirmed"] or missing_fields(platform, values):
                raise DefaultsNotConfirmed(
                    f"The {platform} default settings are not confirmed. Fill in every field "
                    f"on the Default Settings page and press Confirm first.")
            return values

    # ------------------------------------------------------------ alert email
    def get_alert_email(self) -> str:
        with self._lock:
            return self._data["alert_email"]

    def set_alert_email(self, value: str) -> str:
        v = validate_email(value)
        with self._lock:
            self._data["alert_email"] = v
            self._save()
        return v

    # ------------------------------------------------------------ live switch
    def live_trading_enabled(self) -> bool:
        with self._lock:
            return bool(self._data["live_trading_enabled"])

    def set_live_trading_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._data["live_trading_enabled"] = bool(enabled)
            self._save()
            return bool(enabled)


default_settings = DefaultSettingsStore()


def build_pair_values(platform: str, requested: dict) -> dict:
    """Values for a NEW pair: the owner's CONFIRMED defaults, with any field
    the request names explicitly validated and applied on top.
    Raises DefaultsNotConfirmed if Confirm has not been pressed, SettingsError
    if a supplied value is invalid. Never invents a value."""
    merged = default_settings.confirmed_values(platform)
    errors = []
    for name in fields_for(platform):
        raw = (requested or {}).get(name)
        if raw is None:
            continue
        try:
            merged[name] = validate_value(platform, name, raw)
        except SettingsError as e:
            errors.append(str(e))
    if errors:
        raise SettingsError("; ".join(errors))
    still_bad = missing_fields(platform, merged)
    if still_bad:
        raise SettingsError("blank or invalid: " + ", ".join(still_bad))
    return merged


def require_may_trade(platform: str, account_is_test: bool) -> None:
    """Master live-trading switch. Test accounts (Binance testnet / OKX demo)
    are always allowed so exchange connectivity can be checked without
    touching real money. Real accounts need the owner to turn the switch on."""
    if account_is_test:
        return
    if not default_settings.live_trading_enabled():
        raise RuntimeError(
            "Live trading is switched OFF. The owner must turn it on in the dashboard "
            "(Default Settings page) before a real-money account can start. "
            "Test/demo accounts are not affected.")
