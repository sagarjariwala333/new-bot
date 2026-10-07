"""
manager.py
==========

BotManager owns the live set of BotInstance objects (keyed by
"{account_id}:{symbol}") and is the only place that starts/stops them.
The dashboard's start/stop/status endpoints all go through this.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from app.instance import BotInstance
from app.store import store
from app.binance_futures import BinanceFuturesClient
from app.user_data_stream import UserDataStream
from app import telegram_notifier as tg
from app import singleton_lock
from app import default_settings as ds
from app import startup_guard

log = logging.getLogger("manager")


def _key(account_id: str, symbol: str) -> str:
    return f"{account_id}:{symbol}"


class BotManager:
    def __init__(self):
        self.instances: dict[str, BotInstance] = {}
        self.store = store   # exposed so BotInstance can read account-level config (e.g. exposure cap)
        self._account_locks: dict[str, asyncio.Lock] = {}
        # "account_id:symbol" -> error message, for a pair that failed to even
        # START (not a runtime ERROR after starting - see BotInstance.state
        # for that). Populated by start_all_enabled() when one pair's startup
        # failure must not be allowed to take the rest of the process down
        # with it. Cleared the moment that pair starts successfully.
        self.startup_failures: dict[str, str] = {}
        # ONE real-time user-data-stream connection per ACCOUNT (a listenKey
        # is account-wide, not per-symbol) - shared across every pair on
        # that account. See app/user_data_stream.py.
        self.user_data_streams: dict[str, UserDataStream] = {}
        # 2026-09-16 fix: tracks which accounts have already been alerted
        # that their withdrawal-alert threshold currently can't be checked
        # (every pair on the account is stopped) - throttles to one alert
        # per gap, not one every ~5s while the status endpoint keeps
        # polling. See check_for_orphaned_withdrawal_alerts() below.
        self._withdraw_alert_gap_notified: set[str] = set()

    def get_account_lock(self, account_id: str) -> asyncio.Lock:
        """One lock per account, shared by every pair on it. Serializes the
        exposure-check-then-enter sequence across sibling pairs so two pairs
        on the same account can't both read the same pre-entry exposure and
        both pass the cap in the same instant (see risk_guard.py)."""
        lock = self._account_locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[account_id] = lock
        return lock

    def _build_instance(self, account_id: str, symbol: str) -> BotInstance:
        acc = store.get_account(account_id)
        if acc is None:
            raise ValueError("account not found")
        pc = acc.pairs.get(symbol)
        if pc is None:
            raise ValueError("pair not found on this account")
        api_key, api_secret = store.get_credentials(account_id)
        # 2026-09-16 fix (flagged by a third-party review, confirmed real):
        # this used to pass the STORE's own PairConfig object directly - the
        # running instance's self.pc was a direct reference to the exact
        # same object update_pair() mutates via setattr(). That meant a
        # config edit changed the running instance's parameters IMMEDIATELY,
        # regardless of whether the restart itself was deferred (see
        # _pending_restart_on_flat) - the deferred-restart fix only ever
        # delayed rebuilding the instance, it never protected the instance
        # from the underlying object being mutated out from under it while
        # a trade was still open. Every PairConfig field is a plain
        # primitive (str/bool/int/float - confirmed, no nested mutable
        # state), so a shallow copy via dataclasses.replace() is fully
        # sufficient - the running instance now gets its own independent
        # snapshot, taken fresh each time an instance is actually built
        # (including by a deferred restart, which is exactly when it SHOULD
        # pick up the new values).
        pc_snapshot = dataclasses.replace(pc)
        instance = BotInstance(
            account_id=account_id, account_name=acc.name, symbol=symbol,
            api_key=api_key, api_secret=api_secret, testnet=acc.testnet,
            pair_config=pc_snapshot,
        )
        instance.manager = self
        return instance

    def instances_for_account(self, account_id: str) -> list[BotInstance]:
        return [inst for key, inst in self.instances.items() if inst.account_id == account_id]

    def get_or_create_user_data_stream(self, account_id: str, api_key: str, api_secret: str,
                                        testnet: bool) -> UserDataStream:
        """Returns this account's shared real-time stream, creating and
        starting it on first use. Every pair on the account registers its
        own symbol's event handlers on the SAME stream object rather than
        opening its own connection - a listenKey is account-wide.

        Fixed 2026-09-14 (per two independent third-party reviews that both
        caught the same bug): the stream gets its OWN dedicated
        BinanceFuturesClient, built directly from the account's
        credentials - it does NOT borrow a client from whichever pair
        happens to call this first. The previous version stored that
        pair's client, and BotInstance.stop() closes its own client - so if
        the pair that happened to create the stream stopped while sibling
        pairs kept running, the shared stream was left holding a dead
        client for its keepalive/reconnect calls. A dedicated client tied
        to the stream's own lifecycle (created here, closed in
        UserDataStream.stop()) has no such dependency on any single pair's
        lifecycle."""
        stream = self.user_data_streams.get(account_id)
        if stream is None:
            dedicated_client = BinanceFuturesClient(api_key=api_key, api_secret=api_secret, testnet=testnet,
                                                     account_id=account_id)
            stream = UserDataStream(dedicated_client, testnet=testnet)
            stream.start()
            self.user_data_streams[account_id] = stream
        return stream

    async def maybe_teardown_user_data_stream(self, account_id: str):
        """Called when a pair stops. If no pair on this account is running
        anymore, the shared stream has no subscribers left - tear it down
        rather than leaving an idle connection (and its listenKey) open.
        Now properly awaited by the caller (see BotInstance.stop) rather
        than fire-and-forget via asyncio.create_task - a third-party review
        correctly flagged that the previous version scheduled shutdown and
        moved on without confirming it actually completed."""
        stream = self.user_data_streams.pop(account_id, None)
        if stream and not stream.has_subscribers():
            await stream.stop()
        elif stream:
            # Still has subscribers after all - put it back, this call was
            # premature (shouldn't normally happen, but never leak the
            # reference either way).
            self.user_data_streams[account_id] = stream

    def _check_may_start(self, account_id: str, symbol: str) -> None:
        """Two gates, enforced here (the one choke point every start path goes
        through): (1) every setting on the pair is filled and valid - there
        are no fallback values; (2) the live-trading switch, for real-money
        (non-testnet) accounts. Raises RuntimeError with a plain message."""
        startup_guard.require_storage_ok()   # tracker/ledger/state must be on persistent storage
        acc = store.get_account(account_id)
        pc = acc.pairs.get(symbol) if acc is not None else None
        if acc is None or pc is None:
            return  # _build_instance reports "account/pair not found"
        ds.require_pair_ready("binance", dataclasses.asdict(pc))
        ds.require_may_trade("binance", bool(acc.testnet))

    async def start(self, account_id: str, symbol: str) -> BotInstance:
        # 2026-09-16 fix, flagged by a third-party review (confirmed real
        # and serious): a singleton-lock conflict at startup previously
        # only blocked the AUTOMATIC start_all_enabled() call - manual
        # start/restart, and START ALL, went through this same method
        # unchecked, so a conflicting second process could correctly skip
        # its own auto-start yet still trade the moment anyone hit a
        # manual start endpoint on it. Checked here, once, at the actual
        # shared choke point every start path goes through (manual start,
        # restart, START ALL, and the deferred-restart-on-flat mechanism
        # all call this same method) - rather than duplicated at every
        # individual API route, which would have been easy to miss one of.
        if not singleton_lock.is_lock_held():
            raise RuntimeError(
                "Refusing to start - a singleton-lock conflict was detected for this process "
                "(another instance appears to be running). Resolve that first; see /healthz."
            )
        key = _key(account_id, symbol)
        existing = self.instances.get(key)
        if not (existing and existing._task is not None and not existing._task.done()):
            self._check_may_start(account_id, symbol)
        # Judge liveness by the actual asyncio task, not the status string.
        # Previously this checked `state.status != "STOPPED"`, which meant a
        # dead instance stuck in ERROR (e.g. startup reconciliation failed
        # and _run() returned without entering the trading loop) was treated
        # as "still running" forever - clicking Start again was a silent
        # no-op instead of actually retrying.
        if existing and existing._task is not None and not existing._task.done():
            return existing
        instance = self._build_instance(account_id, symbol)
        self.instances[key] = instance
        instance.start()
        store.set_pair_enabled(account_id, symbol, True)
        return instance

    async def stop(self, account_id: str, symbol: str):
        key = _key(account_id, symbol)
        instance = self.instances.get(key)
        store.set_pair_enabled(account_id, symbol, False)
        if instance:
            await instance.stop()

    async def restart(self, account_id: str, symbol: str) -> BotInstance:
        # Checked BEFORE stopping, so a refused restart never takes down a running instance.
        self._check_may_start(account_id, symbol)
        await self.stop(account_id, symbol)
        return await self.start(account_id, symbol)

    async def restart_any_pending(self):
        """2026-09-15, owner decision: a config edit while a position is
        open is deferred (see BotInstance._pending_restart_on_flat's own
        docstring) rather than applied mid-trade. This is the other half -
        called from the status endpoint (piggybacking on the dashboard's
        existing periodic polling, so no new infrastructure was needed) to
        actually apply any deferred edit the moment its instance goes flat.
        Runs from the API request's own task, not from inside the
        instance's own tick loop - restarting (which cancels the running
        task) is only safe to do from a DIFFERENT task than the one being
        cancelled; self-cancellation from within one's own synchronous
        tick would be a much harder thing to reason about correctly."""
        for key, inst in list(self.instances.items()):
            if inst._pending_restart_on_flat and inst.state.status == "IDLE":
                inst._log("Applying a deferred config update now that this pair is flat.")
                await self.restart(inst.account_id, inst.symbol)

    async def check_for_orphaned_withdrawal_alerts(self):
        """2026-09-16 fix, owner decision (item #6, same "issues need a
        specific alert" principle as item #5): the withdrawal-alert leader
        selection fix (see BotInstance._maybe_check_withdraw_alert) only
        works if at least ONE pair on the account is running - if every
        pair is stopped, nothing ticks at all, so nothing would ever
        notice the gap on its own. Piggybacks on the same periodic status-
        endpoint polling as restart_any_pending() above, since that runs
        regardless of whether any pair is currently ticking. Throttled to
        one alert per gap (not one every ~5s while it persists) via
        _withdraw_alert_gap_notified, and clears itself the moment a pair
        on that account is running again OR the alert is disabled/already
        fired - so a genuinely new gap later still alerts again."""
        for acc in self.store.list_accounts():
            if not acc.withdraw_alert_enabled or acc.withdraw_alert_fired:
                self._withdraw_alert_gap_notified.discard(acc.id)
                continue
            running = [i for i in self.instances_for_account(acc.id) if i.state.status != "STOPPED"]
            if running:
                self._withdraw_alert_gap_notified.discard(acc.id)
                continue
            if acc.id in self._withdraw_alert_gap_notified:
                continue
            self._withdraw_alert_gap_notified.add(acc.id)
            await tg.notify_error(
                acc.name, "(all pairs stopped)",
                f"Withdrawal alert is enabled for this account, but every pair on it is "
                f"currently stopped - the equity threshold cannot be checked right now. "
                f"Start at least one pair to resume monitoring, or disable the withdrawal "
                f"alert on this account if that's intentional.",
                True,
            )

    def get(self, account_id: str, symbol: str) -> BotInstance | None:
        return self.instances.get(_key(account_id, symbol))

    def all_status(self) -> list[dict]:
        return [inst.to_status_dict() for inst in self.instances.values()]

    async def start_all_enabled(self):
        """Called on process startup so pairs marked enabled=true resume
        automatically. Each pair starts INDEPENDENTLY - one pair's failure
        (corrupted credentials, a bad encryption key, a malformed config
        entry for just that pair) is caught, logged, and recorded in
        startup_failures rather than propagating up through main.py's
        lifespan handler and preventing the ENTIRE web server - dashboard,
        every other account/pair, even /healthz - from starting at all."""
        for acc in store.list_accounts():
            for symbol, pc in acc.pairs.items():
                if not pc.enabled:
                    continue
                key = _key(acc.id, symbol)
                try:
                    await self.start(acc.id, symbol)
                    self.startup_failures.pop(key, None)
                except Exception as e:
                    msg = f"Failed to start {acc.name}/{symbol} at startup: {e}"
                    log.error(msg)
                    self.startup_failures[key] = str(e)

    async def shutdown_all(self):
        for instance in list(self.instances.values()):
            await instance.stop()
        for stream in list(self.user_data_streams.values()):
            await stream.stop()
        self.user_data_streams.clear()


manager = BotManager()
