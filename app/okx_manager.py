"""
okx_manager.py
===============

OKXBotManager - the OKX tab's own instance registry, deliberately separate
from BotManager (app/manager.py), per the owner's decision that Binance and
OKX are two different platforms with two different tabs. Nothing here is
shared with the Binance manager except the BotInstance class itself (which
doesn't care which adapter it's holding - see app/exchange_adapter.py).

Deliberately SIMPLER than BotManager in one respect: no user-data-stream
equivalent. Binance's real-time push-based SL/TP detection
(user_data_stream.py) is a whole separate system that was built and hardened
over its own dedicated session; building OKX's equivalent (a private
websocket subscription with its own reconnect/keepalive logic) is out of
scope for this pass and flagged explicitly, not silently skipped - see
app/ws_feed.py's NullMarkFeed docstring for the same disclosure applied to
the mark-price staleness feed. In its place, OKX pairs rely on the same
per-candle reconciliation poll every pair already runs each cycle (this is
exactly how Binance itself worked before user_data_stream existed - a real,
previously-shipped-and-safe mode, not a new invention).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from app.instance import BotInstance
from app.okx_store import okx_store
from app.okx_futures import OKXFuturesClient
from app import telegram_notifier as tg
from app import singleton_lock
from app import default_settings as ds
from app import startup_guard

log = logging.getLogger("okx_manager")


def _key(account_id: str, symbol: str) -> str:
    return f"{account_id}:{symbol}"


class OKXBotManager:
    def __init__(self):
        self.instances: dict[str, BotInstance] = {}
        self.store = okx_store
        self._account_locks: dict[str, asyncio.Lock] = {}
        self.startup_failures: dict[str, str] = {}
        # 2026-09-16 fix - see BotManager's identical field for the full
        # reasoning, separate implementation for OKX.
        self._withdraw_alert_gap_notified: set[str] = set()

    def get_account_lock(self, account_id: str) -> asyncio.Lock:
        """Same purpose as BotManager's - one lock per OKX account, shared
        by every pair on it, serializing the exposure-check-then-enter
        sequence (see risk_guard.py). A SEPARATE lock namespace from
        Binance's - an OKX account_id and a Binance account_id are never
        the same lock, matching the owner's decision that exposure caps are
        separate per platform."""
        lock = self._account_locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[account_id] = lock
        return lock

    def _build_instance(self, account_id: str, symbol: str) -> BotInstance:
        acc = okx_store.get_account(account_id)
        if acc is None:
            raise ValueError("OKX account not found")
        pc = acc.pairs.get(symbol)
        if pc is None:
            raise ValueError("pair not found on this OKX account")
        api_key, api_secret, passphrase = okx_store.get_credentials(account_id)
        adapter = OKXFuturesClient(api_key=api_key, api_secret=api_secret,
                                    passphrase=passphrase, demo=acc.demo)
        # 2026-09-16 fix - see BotManager's identical fix (app/manager.py)
        # for the full reasoning: this used to pass the STORE's own
        # OKXPairConfig object directly, letting a config edit change the
        # running instance's parameters immediately, independent of
        # whether the restart itself was deferred. Every field is a plain
        # primitive, so a shallow copy is fully sufficient.
        pc_snapshot = dataclasses.replace(pc)
        instance = BotInstance(
            account_id=account_id, account_name=acc.name, symbol=symbol,
            api_key=None, api_secret=None, testnet=acc.demo,
            pair_config=pc_snapshot, adapter=adapter, platform="okx",
        )
        instance.manager = self
        return instance

    def instances_for_account(self, account_id: str) -> list[BotInstance]:
        return [inst for key, inst in self.instances.items() if inst.account_id == account_id]

    # No get_or_create_user_data_stream / maybe_teardown_user_data_stream -
    # see module docstring. BotInstance.stop() only calls these on
    # self.manager if the manager actually HAS them; since BotInstance is
    # shared code, this manager provides harmless no-op equivalents instead
    # of requiring instance.py to special-case "does my manager support
    # this" logic.
    user_data_streams: dict = {}

    async def maybe_teardown_user_data_stream(self, account_id: str):
        return None

    def _check_may_start(self, account_id: str, symbol: str) -> None:
        """OKX equivalent of BotManager._check_may_start - see that one for the
        reasoning. OKX demo accounts are exempt from the live-trading switch."""
        startup_guard.require_storage_ok()   # tracker/ledger/state must be on persistent storage
        acc = okx_store.get_account(account_id)
        pc = acc.pairs.get(symbol) if acc is not None else None
        if acc is None or pc is None:
            return  # _build_instance reports "account/pair not found"
        ds.require_pair_ready("okx", dataclasses.asdict(pc))
        ds.require_may_trade("okx", bool(acc.demo))

    async def start(self, account_id: str, symbol: str) -> BotInstance:
        # 2026-09-16 fix - see BotManager.start()'s identical check for the
        # full reasoning (single shared choke point for every start path:
        # manual start, restart, START ALL, deferred restart-on-flat).
        if not singleton_lock.is_lock_held():
            raise RuntimeError(
                "Refusing to start - a singleton-lock conflict was detected for this process "
                "(another instance appears to be running). Resolve that first; see /healthz."
            )
        key = _key(account_id, symbol)
        existing = self.instances.get(key)
        if existing and existing._task is not None and not existing._task.done():
            return existing
        self._check_may_start(account_id, symbol)
        instance = self._build_instance(account_id, symbol)
        self.instances[key] = instance
        instance.start()
        okx_store.set_pair_enabled(account_id, symbol, True)
        return instance

    async def stop(self, account_id: str, symbol: str):
        key = _key(account_id, symbol)
        instance = self.instances.get(key)
        okx_store.set_pair_enabled(account_id, symbol, False)
        if instance:
            await instance.stop()

    async def restart(self, account_id: str, symbol: str) -> BotInstance:
        # Checked BEFORE stopping, so a refused restart never takes down a running instance.
        self._check_may_start(account_id, symbol)
        await self.stop(account_id, symbol)
        return await self.start(account_id, symbol)

    async def restart_any_pending(self):
        """OKX equivalent of BotManager's own restart_any_pending() -
        separate implementation, matching every other Binance/OKX
        separation in this project. See that one's docstring for the full
        reasoning (deferred config updates, why this runs from the status
        endpoint's own task rather than self-restarting from within an
        instance's own tick)."""
        for key, inst in list(self.instances.items()):
            if inst._pending_restart_on_flat and inst.state.status == "IDLE":
                inst._log("Applying a deferred config update now that this pair is flat.")
                await self.restart(inst.account_id, inst.symbol)

    async def check_for_orphaned_withdrawal_alerts(self):
        """OKX equivalent of BotManager's own check - see that one's
        docstring for the full reasoning, separate implementation."""
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
        for acc in okx_store.list_accounts():
            for symbol, pc in acc.pairs.items():
                if not pc.enabled:
                    continue
                key = _key(acc.id, symbol)
                try:
                    await self.start(acc.id, symbol)
                    self.startup_failures.pop(key, None)
                except Exception as e:
                    msg = f"Failed to start OKX {acc.name}/{symbol} at startup: {e}"
                    log.error(msg)
                    self.startup_failures[key] = str(e)

    async def shutdown_all(self):
        for instance in list(self.instances.values()):
            await instance.stop()


okx_manager = OKXBotManager()
