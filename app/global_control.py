"""
global_control.py
===================

The "STOP ALL" / "START ALL" emergency controls (2026-09-15, owner
request) - deliberately live outside both manager.py and okx_manager.py,
since this is the one place in the whole system that needs to reach across
BOTH platforms at once. Everything else in this project has kept Binance
and OKX strictly separate on purpose (see the many "separate tabs" notes
throughout) - this is the single, narrow exception, because an emergency
stop that only covers one platform isn't really an emergency stop.

STOP ALL closes every open position (via each instance's own
emergency_flatten_and_stop - the same retried, ambiguous-order-safe close
path every other close in this bot uses) and pauses every currently
running pair, on both platforms. It does NOT change any pair's `enabled`
flag in either store - that's what makes START ALL's behavior correct:
resuming "everything that's supposed to be running" naturally means
exactly what was running before STOP ALL was pressed, no separate
bookkeeping needed for that.

START ALL simply re-runs each manager's own start_all_enabled() - the
exact same, already-tested code path used on process startup. Nothing new
to get wrong there.
"""

from __future__ import annotations

import asyncio
import logging

from app.manager import manager
from app.okx_manager import okx_manager

log = logging.getLogger("global_control")


async def emergency_stop_all() -> dict:
    """Returns a summary dict: how many pairs were flattened/stopped, and
    any that failed to stop cleanly (still attempted, never silently
    skipped - a failure here is exactly what needs to be visible)."""
    results = {"stopped": [], "failed": []}
    all_instances = list(manager.instances.items()) + list(okx_manager.instances.items())

    async def _stop_one(key: str, inst):
        try:
            await inst.emergency_flatten_and_stop(reason="manual_stop_all")
            results["stopped"].append(key)
        except Exception as e:
            log.error("STOP ALL: failed to cleanly stop %s (%s) - it may still be running "
                      "or holding a position. Check it manually.", key, e)
            results["failed"].append({"key": key, "error": str(e)})

    # Concurrently, not sequentially - an emergency stop should not take
    # (number of pairs x however long one close takes) to finish.
    await asyncio.gather(*(_stop_one(key, inst) for key, inst in all_instances))
    return results


async def resume_all() -> dict:
    """Re-runs each manager's own start_all_enabled() - identical to what
    happens on process startup. Whatever STOP ALL left enabled=True (i.e.
    everything, since STOP ALL never touches that flag) comes back."""
    await manager.start_all_enabled()
    await okx_manager.start_all_enabled()
    return {
        "binance_running": len(manager.instances),
        "okx_running": len(okx_manager.instances),
    }
