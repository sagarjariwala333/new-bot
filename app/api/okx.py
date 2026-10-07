"""
app/api/okx.py
================

The OKX tab's own API router, under /api/okx - deliberately separate from
/api (Binance's router in app/api/__init__.py), mirroring its structure
exactly so the two tabs behave identically from an operator's perspective
while using entirely separate storage (okx_store) and instance registry
(okx_manager). Reuses the SAME auth/CSRF dependencies - one dashboard
login covers both tabs, only the account data underneath is separate.
"""

from __future__ import annotations

from dataclasses import asdict

import pandas as pd
from fastapi import APIRouter, HTTPException, Depends, Response

from app.api.schemas import (
    OKXAccountCreate, OKXAccountUpdate, OKXPairCreate, OKXPairUpdate,
    WalkForwardRequest, MonteCarloRequest,
)
from app.okx_store import okx_store, OKXPairConfig
from app.okx_manager import okx_manager
from app import base_v3_tracker
from app.auth import require_auth, require_csrf
from app import ledger
from app import analysis
from app.instance import _params_from_pair
from app import default_settings as ds

router = APIRouter(prefix="/api/okx")

auth_dep = Depends(require_auth)
csrf_dep = Depends(require_csrf)
mutating_deps = [auth_dep, csrf_dep]


def _redact_account(acc) -> dict:
    d = asdict(acc)
    d.pop("api_key_enc", None)
    d.pop("api_secret_enc", None)
    d.pop("passphrase_enc", None)
    d["api_key_masked"] = "•" * 8
    d["api_secret_masked"] = "•" * 8
    d["passphrase_masked"] = "•" * 8
    d["pair_count"] = len(acc.pairs)
    return d


# ---------------------------------------------------------------- accounts
@router.get("/accounts", dependencies=[auth_dep])
def list_okx_accounts():
    return [_redact_account(a) for a in okx_store.list_accounts()]


@router.post("/accounts", dependencies=mutating_deps)
def create_okx_account(body: OKXAccountCreate):
    try:
        acc = okx_store.create_account(body.name, body.api_key, body.api_secret,
                                        body.passphrase, body.demo, body.sub_account_label)
        if (body.max_account_exposure_pct is not None or body.withdraw_alert_enabled
                or body.withdraw_alert_threshold is not None):
            acc = okx_store.update_account(
                acc.id,
                max_account_exposure_pct=body.max_account_exposure_pct,
                withdraw_alert_enabled=body.withdraw_alert_enabled,
                withdraw_alert_threshold=body.withdraw_alert_threshold,
            )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _redact_account(acc)


@router.put("/accounts/{account_id}", dependencies=mutating_deps)
def update_okx_account(account_id: str, body: OKXAccountUpdate):
    if okx_store.get_account(account_id) is None:
        raise HTTPException(404, "OKX account not found")
    updates = body.model_dump(exclude_unset=True)

    # 2026-09-16 fix, owner decision - see app/api/__init__.py's
    # update_account and _refuse_if_credential_change_while_running for
    # the full reasoning. Same refuse-rather-than-silently-apply behavior,
    # reusing the same shared helper - OKX's own credential fields
    # (api_key/api_secret/passphrase/demo).
    from app.api import _refuse_if_credential_change_while_running
    running = [i.symbol for i in okx_manager.instances_for_account(account_id)
               if i.state.status != "STOPPED"]
    _refuse_if_credential_change_while_running(
        updates, running, {"api_key", "api_secret", "passphrase", "demo"}, "demo/live")

    acc = okx_store.update_account(
        account_id,
        name=updates.get("name"),
        api_key=updates.get("api_key"),
        api_secret=updates.get("api_secret"),
        passphrase=updates.get("passphrase"),
        demo=updates.get("demo"),
        sub_account_label=updates.get("sub_account_label"),
        max_account_exposure_pct=updates.get("max_account_exposure_pct", "unset"),
        withdraw_alert_enabled=updates.get("withdraw_alert_enabled"),
        withdraw_alert_threshold=updates.get("withdraw_alert_threshold", "unset"),
    )
    return _redact_account(acc)


@router.delete("/accounts/{account_id}", dependencies=mutating_deps)
async def delete_okx_account(account_id: str):
    acc = okx_store.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "OKX account not found")
    from app.api import _refuse_if_unsafe_to_delete
    instances_by_symbol = {i.symbol: i for i in okx_manager.instances_for_account(account_id)}
    _refuse_if_unsafe_to_delete(account_id, list(acc.pairs.keys()), instances_by_symbol, "OKX")
    for symbol in list(acc.pairs.keys()):
        await okx_manager.stop(account_id, symbol)
    okx_store.delete_account(account_id)
    return {"deleted": True}


# ---------------------------------------------------------------- pairs
@router.post("/accounts/{account_id}/pairs", dependencies=mutating_deps)
def add_okx_pair(account_id: str, body: OKXPairCreate):
    if okx_store.get_account(account_id) is None:
        raise HTTPException(404, "OKX account not found")
    try:
        values = ds.build_pair_values("okx", body.model_dump())
    except ds.DefaultsNotConfirmed as e:
        raise HTTPException(409, str(e))
    except ds.SettingsError as e:
        raise HTTPException(400, str(e))
    pc = OKXPairConfig(symbol=body.symbol, **values)
    try:
        pc = okx_store.add_pair(account_id, pc)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return asdict(pc)


@router.put("/accounts/{account_id}/pairs/{symbol}", dependencies=mutating_deps)
async def update_okx_pair(account_id: str, symbol: str, body: OKXPairUpdate):
    acc = okx_store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    from app.api import _refuse_if_frozen
    _refuse_if_frozen(acc.pairs[symbol], symbol, "editing settings")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}


    pc = okx_store.update_pair(account_id, symbol, updates)
    inst = okx_manager.get(account_id, symbol)
    if inst and inst.state.status != "STOPPED":
        # 2026-09-15, owner decision - see app/api/__init__.py's update_pair
        # for the full reasoning. Same deferred-update behavior, separate
        # implementation.
        if inst.state.status in ("IN_POSITION", "UNPROTECTED", "CLOSING"):
            inst._pending_restart_on_flat = True
            inst._log("Config updated - will apply once this pair is flat (a position is "
                      "currently open or being closed).")
        else:
            await okx_manager.restart(account_id, symbol)
    return asdict(pc)


@router.delete("/accounts/{account_id}/pairs/{symbol}", dependencies=mutating_deps)
async def delete_okx_pair(account_id: str, symbol: str):
    acc = okx_store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    from app.api import _refuse_if_unsafe_to_delete, _refuse_if_frozen
    _refuse_if_frozen(acc.pairs[symbol], symbol, "deletion")
    # 2026-09-16 fix - see app/api/__init__.py's delete_pair for the full
    # reasoning, same pattern reused here for OKX.
    instances_by_symbol = {i.symbol: i for i in okx_manager.instances_for_account(account_id)
                            if i.symbol == symbol}
    _refuse_if_unsafe_to_delete(account_id, [symbol], instances_by_symbol, "OKX")
    await okx_manager.stop(account_id, symbol)
    okx_store.delete_pair(account_id, symbol)
    return {"deleted": True}


# ---------------------------------------------------------------- control
@router.post("/accounts/{account_id}/pairs/{symbol}/start", dependencies=mutating_deps)
async def start_okx_pair(account_id: str, symbol: str):
    acc = okx_store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    from app.api import _refuse_if_frozen
    _refuse_if_frozen(acc.pairs[symbol], symbol, "starting")
    key = f"{account_id}:{symbol}"
    try:
        inst = await okx_manager.start(account_id, symbol)
        okx_manager.startup_failures.pop(key, None)
    except Exception as e:
        okx_manager.startup_failures[key] = str(e)
        raise HTTPException(400, str(e))
    return inst.to_status_dict()


@router.post("/accounts/{account_id}/pairs/{symbol}/stop", dependencies=mutating_deps)
async def stop_okx_pair(account_id: str, symbol: str):
    acc = okx_store.get_account(account_id)
    if acc is not None and symbol in acc.pairs:
        from app.api import _refuse_if_frozen
        _refuse_if_frozen(acc.pairs[symbol], symbol, "stopping")
    await okx_manager.stop(account_id, symbol)
    return {"stopped": True}


@router.post("/accounts/{account_id}/pairs/{symbol}/restart", dependencies=mutating_deps)
async def restart_okx_pair(account_id: str, symbol: str):
    acc = okx_store.get_account(account_id)
    if acc is not None and symbol in acc.pairs:
        from app.api import _refuse_if_frozen
        _refuse_if_frozen(acc.pairs[symbol], symbol, "restarting")
    inst = await okx_manager.restart(account_id, symbol)
    return inst.to_status_dict()


@router.post("/accounts/{account_id}/pairs/{symbol}/freeze", dependencies=mutating_deps)
async def freeze_okx_pair(account_id: str, symbol: str):
    acc = okx_store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    pc = okx_store.set_pair_frozen(account_id, symbol, True)
    return asdict(pc)


@router.post("/accounts/{account_id}/pairs/{symbol}/unfreeze", dependencies=mutating_deps)
async def unfreeze_okx_pair(account_id: str, symbol: str):
    acc = okx_store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    pc = okx_store.set_pair_frozen(account_id, symbol, False)
    return asdict(pc)


# ---------------------------------------------------------------- status
@router.get("/status", dependencies=[auth_dep])
async def okx_all_status():
    # 2026-09-15/16: see app/api/__init__.py's all_status() for the full
    # reasoning - identical pattern, separate implementation.
    await okx_manager.restart_any_pending()
    await okx_manager.check_for_orphaned_withdrawal_alerts()
    return {"instances": okx_manager.all_status(), "startup_failures": okx_manager.startup_failures}


@router.get("/status/{account_id}/{symbol}", dependencies=[auth_dep])
def okx_one_status(account_id: str, symbol: str):
    inst = okx_manager.get(account_id, symbol)
    if inst is None:
        raise HTTPException(404, "instance not running")
    return inst.to_status_dict()


# ---------------------------------------------------------------- trade ledger
# Reuses the SAME ledger module as Binance - trades are keyed by
# (account_id, symbol), and OKX account_ids/symbols never collide with
# Binance's (separate id namespaces, separate symbol formats), so trade
# history naturally stays segregated by tab without any extra plumbing.
@router.get("/ledger/{account_id}/{symbol}", dependencies=[auth_dep])
def get_okx_ledger(account_id: str, symbol: str):
    return {
        "trades": [asdict(t) for t in ledger.list_trades(account_id, symbol)],
        "stats": ledger.stats(account_id, symbol),
        "equity_curve": ledger.equity_curve(account_id, symbol),
    }


@router.get("/ledger/{account_id}/{symbol}/csv", dependencies=[auth_dep])
def get_okx_ledger_csv(account_id: str, symbol: str):
    csv_text = ledger.export_csv(account_id, symbol)
    return Response(
        content=csv_text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{account_id}_{symbol}_trades.csv"'},
    )


# ---------------------------------------------------------------- offline analysis (2026-09-15)
# Mirrors app/api/__init__.py's _fetch_history/walk_forward/monte_carlo
# exactly, structurally - separate implementation (own store, own
# adapter), not a shared function branching on platform, matching every
# other Binance/OKX separation in this project. get_klines() already
# normalizes OKX's raw candle shape to the same Binance-compatible
# 12-column format analysis.py expects (see that function's own docstring
# on this exact fix) - so this needed no special-casing beyond which
# client/store to use.
async def _fetch_okx_history(account_id: str, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    acc = okx_store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    api_key, api_secret, passphrase = okx_store.get_credentials(account_id)
    from app.okx_futures import OKXFuturesClient
    client = OKXFuturesClient(api_key=api_key, api_secret=api_secret,
                               passphrase=passphrase, demo=acc.demo)
    try:
        raw = await client.get_klines(symbol, timeframe, limit=limit)
    finally:
        await client.close()
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "qav", "trades", "tbbav", "tbqav", "ignore",
    ])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["open_time"] = df["open_time"].astype("int64")
    return df.iloc[:-1].reset_index(drop=True)  # drop the still-forming candle


@router.post("/analysis/{account_id}/{symbol}/walk-forward", dependencies=[auth_dep])
async def okx_walk_forward(account_id: str, symbol: str, body: WalkForwardRequest):
    acc = okx_store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    pc = acc.pairs[symbol]
    df = await _fetch_okx_history(account_id, symbol, pc.timeframe, body.limit)
    try:
        p = _params_from_pair(pc)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    try:
        return analysis.walk_forward_analysis(df, p, n_folds=body.n_folds,
                                               initial_equity=body.initial_equity,
                                               commission_pct=body.commission_pct,
                                               slippage_pct=body.slippage_pct)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/analysis/{account_id}/{symbol}/monte-carlo", dependencies=[auth_dep])
async def okx_monte_carlo(account_id: str, symbol: str, body: MonteCarloRequest):
    acc = okx_store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    pc = acc.pairs[symbol]
    df = await _fetch_okx_history(account_id, symbol, pc.timeframe, body.limit)
    try:
        p = _params_from_pair(pc)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    trades = analysis.simulate_trades(df, p, initial_equity=body.initial_equity,
                                      commission_pct=body.commission_pct, slippage_pct=body.slippage_pct)
    if not trades:
        raise HTTPException(400, "No simulated trades in this history window - widen `limit` or check "
                                  "the pair's signal frequency.")
    return analysis.monte_carlo_simulation([t.pnl for t in trades], n_sims=body.n_sims,
                                           initial_equity=body.initial_equity, seed=body.seed)


# ---------------------------------------------------------------- BASE V3 tracker tab
@router.get("/tracker/{account_id}/{symbol}", dependencies=[auth_dep])
def get_tracker(account_id: str, symbol: str):
    """BASE V3 tracker for one pair - read-only. Separate from the live
    account: never sizes, places, changes or closes a real order. Shows the
    tracker balance/high ($ and %), each real trade's % result, the paper
    (shadow) trades, and the shadow status."""
    acc = okx_store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    inst = okx_manager.get(account_id, symbol)
    if inst is not None:
        t = inst.tracker
    else:
        t = base_v3_tracker.load("okx", account_id, symbol,
                                 acc.pairs[symbol].tracker_start_balance)
    sh = t.get_shadow()
    return {
        "summary": t.summary(),
        "shadow_trades_setting": acc.pairs[symbol].shadow_trades,
        "use_shadow": acc.pairs[symbol].use_shadow,
        "real_trades": list(t.real_trades),
        "paper_trades": list(sh.history),
        "open_paper_trade": sh.current,
    }
