"""
hub_api.py
==========

Saving a STRATEGY HUB plan on a pair - the logic shared by the Binance and the OKX routes.
There is no default plan: it exists only after the owner saved a complete one. A plan is validated before
it is stored (an incomplete plan is refused, nothing is filled in). Same rule as every other pair setting:
a change while a position is open (or a flip is still being completed) is saved but applied only when flat.
"""

from __future__ import annotations

import copy
from dataclasses import asdict

from app import strategy_hub as hub
from app import strategy_library as library


class PlanRefused(ValueError):
    """The plan cannot be saved (message is shown to the owner)."""


def plan_view(pc) -> dict:
    plan = getattr(pc, "plan", None)
    if plan:
        plan = hub.upgrade_plan(plan)      # shown in today's format (an old ATR multiple 1.5 shows as 150 % of ATR)
    return {"plan": plan, "summary": hub.plan_summary(plan) if plan else None}


def _old_plan_to_history(store, account_id: str, symbol: str, platform: str, note: str, new_plan=None) -> None:
    """STRATEGY LIBRARY: the pair's strategy as it is NOW goes into the library history before it is changed or
    removed (only when there is one and it really changes). Never stops the change itself."""
    try:
        acc = store.get_account(account_id)
        old_pc = acc.pairs.get(symbol) if acc is not None else None
        old = getattr(old_pc, "plan", None)
        if old and old != new_plan:
            library.add_history(old, platform, account_id, getattr(acc, "name", ""), symbol,
                                getattr(old_pc, "timeframe", None), note)
    except Exception as e:
        import logging
        logging.getLogger("hub_api").error("Could not keep the old strategy of %s in the library history: %s", symbol, e)


def check_amend(amend) -> dict | None:
    """The "Change an open trade" choice sent with a Save: {"when": "now" | "next", "stops": "tighter" | "looser"}.
    "stops" is required (and only used) when "when" is "now". None = no choice sent = the old behaviour (the new rules
    wait for the next trade)."""
    if amend is None:
        return None
    if not isinstance(amend, dict):
        raise PlanRefused("amend must be an object")
    when = amend.get("when")
    if when not in (hub.AMEND_NOW, hub.AMEND_NEXT):
        raise PlanRefused("Choose when the change applies: to the open trade now, or from the next trade.")
    stops = amend.get("stops")
    if when == hub.AMEND_NOW and stops not in (hub.AMEND_TIGHTER, hub.AMEND_LOOSER):
        raise PlanRefused("Choose how the stops may change: only tighter, or looser too.")
    return {"when": when, "stops": stops if when == hub.AMEND_NOW else None}


async def apply_plan(store, mgr, account_id: str, symbol: str, plan, platform: str = "binance", amend=None) -> dict:
    try:
        hub.validate_plan(plan)
    except hub.PlanError as e:
        raise PlanRefused(str(e))
    amend = check_amend(amend)
    amend_result = None
    _old_plan_to_history(store, account_id, symbol, platform, "changed", plan)
    pc = store.update_pair(account_id, symbol, {"plan": copy.deepcopy(plan)})
    inst = mgr.get(account_id, symbol)
    stopped_pair = not getattr(pc, "enabled", True) and (inst is None or getattr(inst, "_task", None) is None
                                                         or inst._task.done() or getattr(inst, "_shadow_only", False))
    if stopped_pair and hasattr(mgr, "refresh_shadow_only"):
        # A STOPPED pair: its shadow restarts with the new plan (or starts, if Shadow mode was just picked; or ends,
        # if it was unpicked). The plan itself is already saved - a problem here never undoes that.
        try:
            if inst is not None and getattr(inst, "_shadow_only", False):
                await mgr.refresh_shadow_only(account_id, symbol)
            else:
                await mgr.start_shadow_only(account_id, symbol)
        except Exception as e:
            import logging
            logging.getLogger("hub_api").error("Could not refresh the shadow of %s: %s", symbol, e)
    elif inst and inst.state.status != "STOPPED":
        busy = getattr(inst, "_hub_busy", False) is True
        if inst.state.status in ("IN_POSITION", "UNPROTECTED", "CLOSING") or busy:
            inst._pending_restart_on_flat = True
            inst._log("Plan updated - the new rules will be applied once this pair is flat "
                      "(a position is open, closing, or a flip is being completed).")
            if amend is not None:
                amend_result = await _amend_open_trade(inst, plan, amend)
        else:
            await mgr.restart(account_id, symbol)
    out = asdict(pc)
    if amend is not None:
        out["amend_result"] = amend_result or {"status": "no_open_trade"}
    return out


async def _amend_open_trade(inst, plan, amend: dict) -> dict:
    """The owner's choice on Save while a trade is open. The full new plan is ALREADY saved (it applies from the next
    trade); "now" additionally changes the allowed settings of the open trade. Never raises: a problem is returned."""
    if amend["when"] == hub.AMEND_NEXT:
        return {"status": "next"}
    if inst.state.status != "IN_POSITION":
        inst._log("Change open trade: the trade is not in a state to be changed right now "
                  f"({inst.state.status}) - the new rules wait for the next trade.")
        return {"status": "not_now", "why": f"The trade is {inst.state.status} - not changed. The new rules wait for the next trade."}
    try:
        res = await inst.hub_amend_open_trade(plan, amend["stops"])
    except Exception as e:
        inst._log(f"Change open trade FAILED ({e}) - the open trade keeps its rules; the new rules wait for the next trade.")
        return {"status": "error", "why": str(e)}
    return {"status": "done", **res}


async def remove_plan(store, mgr, account_id: str, symbol: str, platform: str) -> dict:
    """REMOVE PLAN (owner request 2026-10-24): takes the strategy off the pair. The strategy goes into the library
    history first. A pair with no plan never starts (app/default_settings.require_pair_ready).
    Refused while the pair is trading (Stop it first) and while a trade may still be open on it: a stopped pair can
    still hold a trade that its exchange orders look after, and its plan is still needed for that trade."""
    from app import hub_state, position_state, reconciliation
    acc = store.get_account(account_id)
    pc = acc.pairs.get(symbol) if acc is not None else None
    if pc is None:
        raise PlanRefused("pair not found")
    if not getattr(pc, "plan", None):
        raise PlanRefused(f"{symbol} has no strategy plan to remove.")
    inst = mgr.get(account_id, symbol)
    running = inst is not None and getattr(inst, "_task", None) is not None and not inst._task.done() \
        and not getattr(inst, "_shadow_only", False)
    if getattr(pc, "enabled", False) or running:
        raise PlanRefused(f"{symbol} is trading. Stop the pair first, then remove the plan.")
    why = []
    if inst is not None and getattr(getattr(inst, "state", None), "status", None) in ("IN_POSITION", "UNPROTECTED", "CLOSING"):
        why.append(f"status {inst.state.status}")
    if reconciliation.has_pending_reconciliation(account_id, symbol):
        why.append("pending reconciliation")
    if position_state.load_position_state(account_id, symbol) is not None:
        why.append("the bot's record of an open trade")
    hs = hub_state.load(platform, account_id, symbol)
    if hs.ladder or hs.tp or hs.open_now:
        why.append("the stop / take-profit memory of an open trade")
    if why:
        raise PlanRefused(f"{symbol}: a trade may still be open ({', '.join(why)}). Close it first, then remove the plan.")
    _old_plan_to_history(store, account_id, symbol, platform, "removed")
    store.update_pair(account_id, symbol, {"plan": None})
    if inst is not None and getattr(inst, "_shadow_only", False) and hasattr(mgr, "refresh_shadow_only"):
        try:
            await mgr.refresh_shadow_only(account_id, symbol)    # no plan = no shadow: it ends
        except Exception as e:
            import logging
            logging.getLogger("hub_api").error("Could not end the shadow of %s: %s", symbol, e)
    return {"removed": True, "symbol": symbol}
