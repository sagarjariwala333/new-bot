"""Arbitrage service: one scanner + one trader for the whole hub, and the functions the API routes call."""
from __future__ import annotations

import asyncio
import copy

from app.arb.keys import SLOTS, KeyError_, arb_keys
from app.arb.scanner import ArbScanner
from app.arb.settings import DEFAULTS, LIVE_AVAILABLE, arb_settings, validate
from app.arb.store import ArbStateError
from app.arb.trader import ArbTrader, live_blockers

scanner = ArbScanner(lambda: arb_settings.load())
trader = ArbTrader()
scanner.on_scan = trader.step
scanner.extra_funding = trader.funding_bases


class ArbSaveError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def _settings():
    try:
        return arb_settings.load(), None
    except ValueError as e:
        return copy.deepcopy(DEFAULTS), str(e)


def _have_key(slot):
    try:
        return arb_keys.get(slot) is not None
    except KeyError_:
        return False


def get_state() -> dict:
    settings, problem = _settings()
    snap = scanner.snapshot()
    mock = trader.snapshot(settings)
    checklist = {}
    for name in ("spatial", "funding", "triangular"):
        cfg = settings["modules"][name]
        if cfg["enabled"]:
            cfg2 = dict(cfg)
            checklist[name] = live_blockers(settings, name, cfg2, _have_key)
    status = snap["status"]
    if mock["live_open"] and not status["running"]:
        status["notes"] = list(status.get("notes") or []) + [
            "LIVE trades are open and the scanner is OFF: they are NOT being managed. Switch the scanner on."]
    return {"settings": settings, "problem": problem, "live_available": LIVE_AVAILABLE, "status": status,
            "board": snap["board"], "mock": mock, "keys": arb_keys.status(), "live_checklist": checklist,
            "triangular_unlocked": trader.tri_unlocked, "step": 4}


def apply_scanner() -> None:
    """Start or stop the scanner to match the 'scanner_on' switch. (Must run inside the event loop.)"""
    try:
        on = bool(arb_settings.load().get("scanner_on"))
    except ValueError:
        on = False
    if on:
        scanner.start()


def check_save(new: dict, confirm_live) -> list[str]:
    """Every reason NOT to save, in plain words (empty = fine)."""
    errs = validate(new)
    if errs:
        return errs
    prev, _ = _settings()
    newly = [n for n in ("spatial", "funding", "triangular")
             if new["modules"][n]["enabled"] and new["modules"][n]["mode"] == "live"
             and not (prev["modules"][n]["enabled"] and prev["modules"][n]["mode"] == "live")]
    if newly and confirm_live != "LIVE":
        errs.append("REAL MONEY: you are switching %s to live. Type LIVE to confirm." % ", ".join(newly))
    for n in ("spatial", "funding", "triangular"):
        cfg = new["modules"][n]
        if cfg["enabled"] and cfg["mode"] == "live":
            for b in live_blockers(new, n, cfg, _have_key):
                if b.startswith("enter the"):
                    errs.append("%s cannot go live: %s" % (n, b))
    return errs


async def save_settings(new: dict, confirm_live=None) -> dict:
    errs = check_save(new, confirm_live)
    if errs:
        raise ArbSaveError(errs)
    errs = arb_settings.save(new)
    if errs:
        raise ArbSaveError(errs)
    if new.get("scanner_on"):
        scanner.start()
    else:
        await scanner.stop()
        scanner.board = {"spatial": [], "funding": [], "triangular": []}
    return get_state()


def save_key(slot: str, values: dict) -> dict:
    try:
        if values.get("clear") is True:
            arb_keys.clear(slot)
        else:
            arb_keys.update(slot, values)
    except KeyError_ as e:
        raise ArbSaveError([str(e)])
    return get_state()


async def check_keys() -> dict:
    s, _ = _settings()
    loop = asyncio.get_running_loop()
    result = await asyncio.to_thread(trader.check_keys, s, loop)
    return {"checks": result, **get_state()}


def set_stop(on: bool) -> dict:
    s, _ = _settings()
    try:
        trader.set_stop(bool(on), s)
    except (ArbStateError, OSError) as e:
        raise ArbSaveError([str(e)])
    return get_state()


def triangular_lock(typed) -> dict:
    """Second lock for LIVE triangular: only the exact word YES unlocks it (until the next restart)."""
    if typed is None:
        trader.tri_unlocked = False
        return get_state()
    s, _ = _settings()
    t = s["modules"]["triangular"]
    if typed != "YES":
        raise ArbSaveError(["Type YES (capital letters) to unlock triangular."])
    if not (t["enabled"] and t.get("confirm_enable") is True):
        raise ArbSaveError(["First switch triangular on and tick 'I confirm' (and save)."])
    trader.tri_unlocked = True
    return get_state()


async def reset_mock() -> dict:
    """Delete the MOCK results (wallets, open mock trades, mock rows of the list). Live data is never touched."""
    try:
        removed = await asyncio.to_thread(trader.reset)
    except ArbStateError as e:
        raise ArbSaveError([str(e)])
    return {"removed_files": removed, **get_state()}


async def shutdown() -> None:
    await scanner.stop()
