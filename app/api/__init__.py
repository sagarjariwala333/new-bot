from __future__ import annotations

import logging
import os
from dataclasses import asdict

import pandas as pd
from fastapi import APIRouter, HTTPException, Depends, Response

log = logging.getLogger("api")

from app.api.schemas import (
    AccountCreate, AccountUpdate, PairCreate, PairUpdate,
    LoginRequest, ChangePasswordRequest, ForgotPasswordRequest, ResetPasswordRequest,
    WalkForwardRequest, MonteCarloRequest,
    DefaultSettingsSave, AlertEmailSave, LiveTradingSwitch,
)
from app import default_settings as ds
from app.store import store, PairConfig
from app.manager import manager
from app import base_v3_tracker
from app.auth import (
    auth_store, require_auth, require_csrf, issue_csrf_token,
    SESSION_COOKIE, CSRF_COOKIE, SESSION_TTL_SECONDS,
)
from app import ledger
from app import reconciliation
from app import analysis
from app.instance import _params_from_pair
from app import global_control

router = APIRouter(prefix="/api")

# Set COOKIE_SECURE=true in .env once the dashboard is served over HTTPS
# (behind a reverse proxy, Tailscale, etc.) - browsers will otherwise send
# the session/CSRF cookies over plain HTTP, which is fine for localhost-only
# use but not once the dashboard is reachable over a real network.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"


def _redact_account(acc) -> dict:
    d = asdict(acc)
    d.pop("api_key_enc", None)
    d.pop("api_secret_enc", None)
    d["api_key_masked"] = "•" * 8
    d["api_secret_masked"] = "•" * 8
    d["pair_count"] = len(acc.pairs)
    return d


# ---------------------------------------------------------------- auth (not behind require_auth)
@router.post("/auth/login")
def login(body: LoginRequest, response: Response):
    token = auth_store.attempt_login(body.username, body.password)
    if not token:
        raise HTTPException(401, "Invalid username or password")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                         secure=COOKIE_SECURE, max_age=SESSION_TTL_SECONDS)
    # Double-submit CSRF cookie: deliberately NOT httponly, so the dashboard's
    # own JS can read it and echo it back as a header (see require_csrf).
    response.set_cookie(CSRF_COOKIE, issue_csrf_token(), httponly=False, samesite="lax",
                         secure=COOKIE_SECURE, max_age=SESSION_TTL_SECONDS)
    return {"ok": True}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"ok": True}


# 2026-09-15, owner request: password reset via email, reusing the same
# email channel built earlier tonight for trade alerts (email_notifier.py).
# Deliberately public, same security model as /auth/login itself - there's
# no session yet at this point, so CSRF protection isn't meaningful here;
# the actual protections are the per-username request cooldown (see
# AuthStore.request_password_reset) and the random, single-use, time-
# limited token, not a login wall.
@router.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest):
    from app.email_notifier import email_notifier
    token = auth_store.request_password_reset(body.username)
    # ALWAYS the same generic response, whether the username matched, the
    # cooldown was hit, or the reset actually got sent - a response that
    # varied would let an attacker enumerate whether a given username
    # exists on this dashboard at all. See request_password_reset's own
    # docstring for the same reasoning on the AuthStore side.
    generic_response = {
        "ok": True,
        "message": "If that username is correct, a reset code has been sent to the configured recovery email.",
    }
    if token is None:
        return generic_response
    if not email_notifier.configured:
        log.warning("Password reset requested for %s but no SMTP is configured - "
                    "the token exists but cannot be delivered. Server-level recovery "
                    "(delete data/auth.json and restart) is the only option until "
                    "SMTP is set up.", body.username)
        return generic_response
    await email_notifier.send(
        "Trade-Bot - Password Reset",
        f"A password reset was requested for your Trade-Bot dashboard.\n\n"
        f"Reset code: {token}\n\n"
        f"This code expires in {AuthStore.RESET_TOKEN_TTL_SECONDS // 60} minutes and can "
        f"only be used once.\n\n"
        f"If you didn't request this, ignore this email - your password has not been changed.",
    )
    return generic_response


@router.post("/auth/reset-password")
async def reset_password(body: ResetPasswordRequest):
    ok = auth_store.reset_password_with_token(body.token, body.new_password)
    if not ok:
        raise HTTPException(400, "That reset code is invalid or has expired - request a new one.")
    return {"ok": True}


# Everything below requires a valid session -----------------------------------
auth_dep = Depends(require_auth)
csrf_dep = Depends(require_csrf)
# Every state-changing (POST/PUT/DELETE) route behind auth also gets the CSRF
# check; GET routes only need auth_dep since they don't mutate anything.
mutating_deps = [auth_dep, csrf_dep]


@router.get("/auth/status", dependencies=[auth_dep])
def auth_status():
    return {"ok": True}


@router.post("/auth/change-password", dependencies=mutating_deps)
def change_password(body: ChangePasswordRequest):
    ok = auth_store.change_password(body.current_password, body.new_password)
    if not ok:
        raise HTTPException(400, "Current password is incorrect")
    return {"ok": True}


# ---------------------------------------------------------------- accounts
@router.get("/accounts", dependencies=[auth_dep])
def list_accounts():
    return [_redact_account(a) for a in store.list_accounts()]


@router.post("/accounts", dependencies=mutating_deps)
def create_account(body: AccountCreate):
    try:
        acc = store.create_account(body.name, body.api_key, body.api_secret, body.testnet)
        if body.max_account_exposure_pct is not None or body.withdraw_alert_enabled or body.withdraw_alert_threshold is not None:
            acc = store.update_account(
                acc.id,
                max_account_exposure_pct=body.max_account_exposure_pct,
                withdraw_alert_enabled=body.withdraw_alert_enabled,
                withdraw_alert_threshold=body.withdraw_alert_threshold,
            )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _redact_account(acc)


def _refuse_if_credential_change_while_running(updates: dict, running_symbols: list[str],
                                                credential_fields: set[str], platform_label: str) -> None:
    """2026-09-16, owner decision - pulled out as its own pure function
    (rather than inlined in each route) specifically so it's directly
    testable with an explicit `updates` dict, independent of whether the
    request body's own model_dump(exclude_unset=True) behaves exactly as
    expected in any given environment. See update_account's own comment
    for the full reasoning on why this refuses rather than silently
    applying or auto-restarting."""
    if not (credential_fields & updates.keys()):
        return
    if running_symbols:
        raise HTTPException(
            409,
            f"Cannot change API credentials or {platform_label} while pairs are still running "
            f"on this account: {', '.join(sorted(running_symbols))}. Stop them first, then "
            f"retry - this prevents a running pair from silently continuing to trade on the "
            f"old credentials/environment after the dashboard shows something different."
        )


@router.put("/accounts/{account_id}", dependencies=mutating_deps)
def update_account(account_id: str, body: AccountUpdate):
    if store.get_account(account_id) is None:
        raise HTTPException(404, "account not found")
    # exclude_unset (not `is not None`) so an omitted field is left alone,
    # while an explicit `"max_account_exposure_pct": null` still clears the
    # cap on purpose - otherwise every partial PUT that doesn't re-send the
    # current cap would silently wipe it out. Same reasoning applies to
    # withdraw_alert_threshold.
    updates = body.model_dump(exclude_unset=True)

    # 2026-09-16 fix, owner decision: credentials and the testnet/live flag
    # are baked into each running pair's exchange connection at the moment
    # it was built - changing them while pairs are still running would
    # leave the dashboard showing the new setting while the actual
    # connection quietly keeps using the old one (most dangerous for a
    # testnet<->live switch specifically). Refused outright rather than
    # silently applied or auto-restarted, unlike the deferred-update
    # pattern used for ordinary pair settings elsewhere in this project -
    # a credential/environment change is a deliberate, attention-heavy
    # action (going live, rotating a compromised key), not a routine
    # tuning tweak, so it should require a conscious "stop these pairs
    # first" step rather than happening automatically underneath the owner.
    # Non-credential fields (exposure cap, withdrawal alert) are NOT
    # affected by this check - those already apply on the next read,
    # no restart needed, no risk either way.
    running = [i.symbol for i in manager.instances_for_account(account_id)
               if i.state.status != "STOPPED"]
    _refuse_if_credential_change_while_running(
        updates, running, {"api_key", "api_secret", "testnet"}, "testnet/live")

    acc = store.update_account(
        account_id,
        name=updates.get("name"),
        api_key=updates.get("api_key"),
        api_secret=updates.get("api_secret"),
        testnet=updates.get("testnet"),
        max_account_exposure_pct=updates.get("max_account_exposure_pct", "unset"),
        withdraw_alert_enabled=updates.get("withdraw_alert_enabled"),
        withdraw_alert_threshold=updates.get("withdraw_alert_threshold", "unset"),
    )
    return _redact_account(acc)


def _refuse_if_frozen(pc, symbol: str, action: str) -> None:
    """2026-09-19, owner request: Freeze is a deliberate, persistent lock
    (survives restarts, unlike the pair-edit dialog's own per-open
    unlock) protecting against accidental human clicks - editing, start,
    stop, restart, and delete are all refused while frozen, with no
    exception, until explicitly unfrozen first. Has zero effect on the
    bot's own internal behavior - this is checked only at the API layer,
    never inside the trading loop itself, so a frozen pair that's
    currently running keeps trading, keeps its fixed stop, and keeps
    reacting to its own safety systems (failure escalation, reconciliation)
    exactly as if it weren't frozen at all."""
    if pc is not None and getattr(pc, "frozen", False):
        raise HTTPException(
            409,
            f"{symbol} is frozen - {action} is refused until it's explicitly unfrozen first."
        )


def _refuse_if_unsafe_to_delete(account_id: str, symbols: list[str],
                                 instances_by_symbol: dict, platform_label: str) -> None:
    """2026-09-16, owner decision (item #4, same philosophy as item #3's
    credential-change refusal): deleting an account is destructive and
    hard to undo - it removes the local configuration needed to find and
    manage a position again. Refuses if any pair has an open/transitioning
    position OR an unresolved pending-reconciliation warning, rather than
    silently deleting and losing track of it. Pure function, tested
    directly with explicit data - see _refuse_if_credential_change_while_
    running's own docstring for why this pattern is used instead of
    inlining the check in each route.

    SCOPE, stated plainly rather than overstated: this checks the LOCAL
    instance's last-known status and the persisted pending-reconciliation
    records - both available without a live network call, so deletion
    itself can't hang or fail because the exchange happens to be slow or
    unreachable at that moment. It does NOT make a fresh live query to the
    exchange for a pair whose instance isn't currently running at all
    (e.g. one that failed to start, or was stopped before this process's
    current run) - such a pair could theoretically still hold a real
    position this check wouldn't see. Flagging this limitation explicitly
    rather than silently narrowing scope."""
    blocking = []
    for symbol in symbols:
        if reconciliation.has_pending_reconciliation(account_id, symbol):
            blocking.append(f"{symbol} (pending reconciliation - unresolved)")
            continue
        inst = instances_by_symbol.get(symbol)
        if inst is not None and inst.state.status in ("IN_POSITION", "UNPROTECTED", "CLOSING"):
            blocking.append(f"{symbol} ({inst.state.status})")
    if blocking:
        raise HTTPException(
            409,
            f"Cannot delete this {platform_label} account - not confirmed flat: "
            f"{', '.join(blocking)}. Close or resolve these first, then retry. Deleting now "
            f"would remove the configuration needed to find and manage them again."
        )


@router.delete("/accounts/{account_id}", dependencies=mutating_deps)
async def delete_account(account_id: str):
    acc = store.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    instances_by_symbol = {i.symbol: i for i in manager.instances_for_account(account_id)}
    _refuse_if_unsafe_to_delete(account_id, list(acc.pairs.keys()), instances_by_symbol, "Binance")
    for symbol in list(acc.pairs.keys()):
        await manager.stop(account_id, symbol)
    store.delete_account(account_id)
    return {"deleted": True}


# ---------------------------------------------------------------- default settings
# One page for the owner: blank defaults per platform + Confirm, the alert
# email, and the live-trading master switch. See app/default_settings.py.
@router.get("/default-settings", dependencies=[auth_dep])
def get_default_settings():
    return {
        "binance": ds.default_settings.get("binance"),
        "okx": ds.default_settings.get("okx"),
        "alert_email": ds.default_settings.get_alert_email(),
        "live_trading_enabled": ds.default_settings.live_trading_enabled(),
    }


@router.put("/default-settings/{platform}", dependencies=mutating_deps)
def save_default_settings(platform: str, body: DefaultSettingsSave):
    if platform not in ds.PLATFORMS:
        raise HTTPException(404, "unknown platform")
    try:
        return ds.default_settings.save(platform, body.values)
    except ds.SettingsError as e:
        raise HTTPException(400, str(e))


@router.post("/default-settings/{platform}/confirm", dependencies=mutating_deps)
def confirm_default_settings(platform: str):
    if platform not in ds.PLATFORMS:
        raise HTTPException(404, "unknown platform")
    try:
        return ds.default_settings.confirm(platform)
    except ds.SettingsError as e:
        raise HTTPException(400, str(e))


@router.put("/alert-email", dependencies=mutating_deps)
def save_alert_email(body: AlertEmailSave):
    try:
        return {"alert_email": ds.default_settings.set_alert_email(body.email)}
    except ds.SettingsError as e:
        raise HTTPException(400, str(e))


@router.put("/live-trading", dependencies=mutating_deps)
def set_live_trading(body: LiveTradingSwitch):
    return {"live_trading_enabled": ds.default_settings.set_live_trading_enabled(body.enabled)}


# ---------------------------------------------------------------- pairs
@router.post("/accounts/{account_id}/pairs", dependencies=mutating_deps)
def add_pair(account_id: str, body: PairCreate):
    if store.get_account(account_id) is None:
        raise HTTPException(404, "account not found")
    try:
        values = ds.build_pair_values("binance", body.model_dump())
    except ds.DefaultsNotConfirmed as e:
        raise HTTPException(409, str(e))
    except ds.SettingsError as e:
        raise HTTPException(400, str(e))
    pc = PairConfig(symbol=body.symbol, **values)
    try:
        pc = store.add_pair(account_id, pc)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return asdict(pc)


@router.put("/accounts/{account_id}/pairs/{symbol}", dependencies=mutating_deps)
async def update_pair(account_id: str, symbol: str, body: PairUpdate):
    acc = store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    _refuse_if_frozen(acc.pairs[symbol], symbol, "editing settings")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}


    pc = store.update_pair(account_id, symbol, updates)
    inst = manager.get(account_id, symbol)
    if inst and inst.state.status != "STOPPED":
        # 2026-09-15, owner decision: "these parameters should be decided
        # before any trade, not mid trade." A position that's open (or
        # mid-transition) must finish entirely under the settings it was
        # opened with - the update is saved (above) but deferred, not
        # applied to the running instance, until it's genuinely flat. See
        # BotInstance._pending_restart_on_flat and manager.restart_any_
        # pending() for the other half of this.
        if inst.state.status in ("IN_POSITION", "UNPROTECTED", "CLOSING"):
            inst._pending_restart_on_flat = True
            inst._log("Config updated - will apply once this pair is flat (a position is "
                      "currently open or being closed).")
        else:
            await manager.restart(account_id, symbol)
    return asdict(pc)


@router.delete("/accounts/{account_id}/pairs/{symbol}", dependencies=mutating_deps)
async def delete_pair(account_id: str, symbol: str):
    acc = store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    _refuse_if_frozen(acc.pairs[symbol], symbol, "deletion")
    # 2026-09-16 fix (pre-live audit finding #4, confirmed real): account
    # deletion already refuses when a position isn't confirmed flat -
    # pair-level deletion didn't have the same guard, meaning a pair with
    # a real open position could be deleted, losing the local record
    # needed to find and manage it again. Reuses the same pure function
    # account deletion already uses, called with just this one symbol.
    instances_by_symbol = {i.symbol: i for i in manager.instances_for_account(account_id)
                            if i.symbol == symbol}
    _refuse_if_unsafe_to_delete(account_id, [symbol], instances_by_symbol, "Binance")
    await manager.stop(account_id, symbol)
    store.delete_pair(account_id, symbol)
    return {"deleted": True}


# ---------------------------------------------------------------- control
@router.post("/accounts/{account_id}/pairs/{symbol}/start", dependencies=mutating_deps)
async def start_pair(account_id: str, symbol: str):
    acc = store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    _refuse_if_frozen(acc.pairs[symbol], symbol, "starting")
    key = f"{account_id}:{symbol}"
    try:
        inst = await manager.start(account_id, symbol)
        manager.startup_failures.pop(key, None)  # a successful manual start clears any stale failure marker
    except Exception as e:
        manager.startup_failures[key] = str(e)
        raise HTTPException(400, str(e))
    return inst.to_status_dict()


@router.post("/accounts/{account_id}/pairs/{symbol}/stop", dependencies=mutating_deps)
async def stop_pair(account_id: str, symbol: str):
    acc = store.get_account(account_id)
    if acc is not None and symbol in acc.pairs:
        _refuse_if_frozen(acc.pairs[symbol], symbol, "stopping")
    await manager.stop(account_id, symbol)
    return {"stopped": True}


@router.post("/accounts/{account_id}/pairs/{symbol}/restart", dependencies=mutating_deps)
async def restart_pair(account_id: str, symbol: str):
    acc = store.get_account(account_id)
    if acc is not None and symbol in acc.pairs:
        _refuse_if_frozen(acc.pairs[symbol], symbol, "restarting")
    inst = await manager.restart(account_id, symbol)
    return inst.to_status_dict()


@router.post("/accounts/{account_id}/pairs/{symbol}/freeze", dependencies=mutating_deps)
async def freeze_pair(account_id: str, symbol: str):
    acc = store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    pc = store.set_pair_frozen(account_id, symbol, True)
    return asdict(pc)


@router.post("/accounts/{account_id}/pairs/{symbol}/unfreeze", dependencies=mutating_deps)
async def unfreeze_pair(account_id: str, symbol: str):
    acc = store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    pc = store.set_pair_frozen(account_id, symbol, False)
    return asdict(pc)


# ---------------------------------------------------------------- status
@router.get("/status", dependencies=[auth_dep])
async def all_status():
    # 2026-09-15/16: piggybacks two checks onto this existing periodic poll
    # (the dashboard already calls this every 5s) - applying any deferred
    # config update the moment its pair goes flat, and catching an account
    # whose withdrawal-alert check has nobody left running to do it. No new
    # infrastructure needed for either.
    await manager.restart_any_pending()
    await manager.check_for_orphaned_withdrawal_alerts()
    return {"instances": manager.all_status(), "startup_failures": manager.startup_failures}


@router.get("/status/{account_id}/{symbol}", dependencies=[auth_dep])
def one_status(account_id: str, symbol: str):
    inst = manager.get(account_id, symbol)
    if inst is None:
        raise HTTPException(404, "instance not running")
    return inst.to_status_dict()


# ---------------------------------------------------------------- trade ledger
@router.get("/ledger/{account_id}/{symbol}", dependencies=[auth_dep])
def get_ledger(account_id: str, symbol: str):
    return {
        "trades": [asdict(t) for t in ledger.list_trades(account_id, symbol)],
        "stats": ledger.stats(account_id, symbol),
        "equity_curve": ledger.equity_curve(account_id, symbol),
    }


@router.get("/ledger/{account_id}/{symbol}/csv", dependencies=[auth_dep])
def get_ledger_csv(account_id: str, symbol: str):
    csv_text = ledger.export_csv(account_id, symbol)
    return Response(
        content=csv_text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{account_id}_{symbol}_trades.csv"'},
    )


# ---------------------------------------------------------------- offline analysis
async def _fetch_history(account_id: str, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    acc = store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    api_key, api_secret = store.get_credentials(account_id)
    from app.binance_futures import BinanceFuturesClient
    client = BinanceFuturesClient(api_key=api_key, api_secret=api_secret, testnet=acc.testnet)
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
async def walk_forward(account_id: str, symbol: str, body: WalkForwardRequest):
    acc = store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    pc = acc.pairs[symbol]
    df = await _fetch_history(account_id, symbol, pc.timeframe, body.limit)
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
async def monte_carlo(account_id: str, symbol: str, body: MonteCarloRequest):
    acc = store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    pc = acc.pairs[symbol]
    df = await _fetch_history(account_id, symbol, pc.timeframe, body.limit)
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


# ---------------------------------------------------------------- global emergency control
# 2026-09-15, owner request - the ONE place in this API that deliberately
# spans both Binance and OKX at once (see global_control.py's own
# docstring for why this is the single exception to the "separate tabs"
# rule everywhere else). Mutating + irreversible (STOP ALL closes real
# positions) - CSRF-protected like every other mutating route, and the
# frontend shows a confirmation popup before ever calling these.
@router.post("/control/stop-all", dependencies=mutating_deps)
async def stop_all():
    result = await global_control.emergency_stop_all()
    return result


@router.post("/control/start-all", dependencies=mutating_deps)
async def start_all():
    result = await global_control.resume_all()
    return result


# ---------------------------------------------------------------- BASE V3 tracker tab
@router.get("/tracker/{account_id}/{symbol}", dependencies=[auth_dep])
def get_tracker(account_id: str, symbol: str):
    """BASE V3 tracker for one pair - read-only. Separate from the live
    account: never sizes, places, changes or closes a real order. Shows the
    tracker balance/high ($ and %), each real trade's % result, the paper
    (shadow) trades, and the shadow status."""
    acc = store.get_account(account_id)
    if acc is None or symbol not in acc.pairs:
        raise HTTPException(404, "pair not found")
    inst = manager.get(account_id, symbol)
    if inst is not None:
        t = inst.tracker
    else:
        t = base_v3_tracker.load("binance", account_id, symbol,
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
