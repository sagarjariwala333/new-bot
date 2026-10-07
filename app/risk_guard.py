"""
risk_guard.py
=============

Account-level exposure cap. This is a RISK GUARD, not a strategy input:
it never changes what the strategy decides to do, it only decides whether
a *new* entry is allowed to proceed right now, based on how much of the
account's equity is already committed across its other positions.

Explicitly opt-in per account (`max_account_exposure_pct` on AccountConfig,
default None = no cap, preserving current behavior unless you set one).

Fixed (2026-09-14, per a third-party review): exposure is now computed from
REAL Binance account data (BinanceFuturesClient.get_all_open_positions -
every open position on the account, queried directly from the exchange),
not from this process's own in-memory BotInstance state. The earlier
version summed sibling pairs' local `state.qty`/`state.entry_price`, which
could understate real exposure if a sibling instance crashed/never started
(no BotInstance object to sum at all), was stuck in an inconsistent state,
or a position existed that this process simply didn't know about (manual
trade, another tool). Real exchange data doesn't have any of those blind
spots - it's account-wide truth, not a sum of what this process happens
to be tracking.
"""

from __future__ import annotations


def compute_real_exposure(positions: list[dict], exclude_symbol: str | None = None) -> float:
    """positions: raw entries from get_all_open_positions() (or an
    equivalent list of dicts with "symbol", "positionAmt", "entryPrice").
    Notional = abs(positionAmt) * entryPrice per position, matching the
    same notional-exposure measure used for position sizing elsewhere in
    this project."""
    total = 0.0
    for p in positions:
        if exclude_symbol and p.get("symbol") == exclude_symbol:
            continue
        try:
            amt = abs(float(p.get("positionAmt", 0)))
            entry_price = float(p.get("entryPrice", 0))
        except (TypeError, ValueError):
            continue
        total += amt * entry_price
    return total


def check_new_entry_allowed(positions: list[dict], exclude_symbol: str, new_notional: float,
                             equity: float, max_account_exposure_pct: float | None) -> tuple[bool, str]:
    """positions must be REAL exchange data (get_all_open_positions()), not
    local instance state - see the module docstring for why."""
    if not max_account_exposure_pct:
        return True, ""
    existing = compute_real_exposure(positions, exclude_symbol=exclude_symbol)
    cap = equity * (max_account_exposure_pct / 100.0)
    projected = existing + new_notional
    if projected > cap:
        return False, (
            f"blocked by account exposure cap: existing {existing:.2f} + new {new_notional:.2f} "
            f"= {projected:.2f} would exceed cap {cap:.2f} ({max_account_exposure_pct}% of equity {equity:.2f})"
        )
    return True, ""
