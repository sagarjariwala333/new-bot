"""
instance.py
===========

BotInstance = one live-trading loop for exactly one (account, pair).
Up to 12 accounts x 3 pairs (Binance) / 5 x 5 (OKX) can run concurrently as
asyncio tasks, each fully isolated (own client, own state, own log ring
buffer) so a problem on one pair never touches another.

The trading DECISIONS below follow the rules in app/strategy.py. All
execution / safety infrastructure is kept:
ambiguous-order recovery, client-order-id tagging, duplicate protection,
filled-quantity confirmation, naked-position recovery, idempotent closes,
pending reconciliation, exposure cap, leverage-bracket check, singleton
lock, restart recovery, alerts, ledger.

What happens each poll:

  1. Top priority: an UNPROTECTED position (no resting stop) or a close in
     progress (CLOSING) is resolved before anything else.
  2. Klines are fetched (HISTORY_CANDLES closed candles, so every indicator
     is fully warmed up). Strategy decisions happen ONCE per newly closed candle -
     the last processed candle is persisted, so a restart can never process
     the same candle twice.
  3. On a newly closed candle, in this order:
       a. shadow (paper) step, if shadow mode is active
       b. flat  -> enter if there is a signal and real entries are allowed
          in position -> TP1/TP2 tracking, then Hold Rule / close on an
          opposite signal. A flip only CLOSES on the signal candle; the new
          side can only open on the next candle through the normal entry.
  4. One FIXED stop order is placed right after entry (real fill price,
     real filled quantity, the configured % of the available balance read at
     entry). It is never amended. There are no TP orders.
  5. Every real close updates the ledger and the separate Base V3 tracker
     (app/base_v3_tracker.py), which is what switches shadow mode on.

Everything that touches strategy *decisions* calls into app.strategy;
nothing here re-implements that math.

SMOOTHED vs REAL PRICES: smoothed-candle values are analysis inputs only.
Orders are always sized from the NORMAL candle close and protected from the
REAL exchange fill price - a smoothed price is never sent to an exchange.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import aiohttp
import pandas as pd

from app import strategy as strat
from app.exchange_adapter import BaseExchangeAdapter, ExchangeAPIError
from app.binance_futures import BinanceFuturesClient
from app.store import PairConfig
from app import telegram_notifier as tg
from app import ledger
from app import risk_guard
from app import position_state
from app import reconciliation
from app import base_v3_tracker as tracker_mod
from app.ws_feed import MarkPriceFeed, OKXMarkPriceFeed, NullMarkFeed

MAX_LOG_LINES = 300


_STRATEGY_FIELDS = (
    "hma_length", "ema_filter_length", "ema_sizing_length", "atr_length", "use_hold_rule",
    "adx_di_length", "adx_smoothing", "hold_adx_level", "trend_alloc_pct", "counter_alloc_pct",
    "leverage", "counter_leverage", "stop_loss_pct_equity", "tp1_atr_mult", "tp2_atr_mult",
    "use_shadow", "shadow_trades", "shadow_slippage_ticks",
)


def _params_from_pair(pc) -> strat.StrategyParams:
    """Builds the StrategyParams from a Binance PairConfig or an OKX
    OKXPairConfig (identical field names on both). Refuses (RuntimeError)
    if any setting is still blank - there are no fallback values."""
    blank = [n for n in _STRATEGY_FIELDS if getattr(pc, n, None) is None]
    if blank:
        raise RuntimeError("These settings are blank on this pair, so the strategy cannot "
                           "run: " + ", ".join(blank))
    return strat.StrategyParams(
        hma_length=pc.hma_length, ema_filter_length=pc.ema_filter_length, ema_sizing_length=pc.ema_sizing_length,
        atr_length=pc.atr_length,
        use_hold_rule=pc.use_hold_rule, adx_di_length=pc.adx_di_length,
        adx_smoothing=pc.adx_smoothing, hold_adx_level=pc.hold_adx_level,
        trend_alloc_pct=pc.trend_alloc_pct, counter_alloc_pct=pc.counter_alloc_pct,
        leverage=pc.leverage, counter_leverage=pc.counter_leverage,
        stop_loss_pct_equity=pc.stop_loss_pct_equity,
        tp1_atr_mult=pc.tp1_atr_mult, tp2_atr_mult=pc.tp2_atr_mult,
        use_shadow=pc.use_shadow, shadow_trades=pc.shadow_trades,
        shadow_slippage_ticks=pc.shadow_slippage_ticks,
    )


class StopAlreadyCrossedError(Exception):
    """Raised when the fixed Base V3 stop price is already on the wrong side
    of the current mark price at the moment it would be placed (the market
    moved through the stop between the fill and the stop placement). The
    stop has effectively already been hit, so the position is closed at
    market instead - the stop price itself is never moved."""

    def __init__(self, requested: float, mark: float, detail: str):
        super().__init__(f"requested stop {requested} is already crossed (mark {mark}): {detail}")
        self.requested = requested
        self.mark = mark


# 2026-09-16 fix (audit finding #1): how many consecutive tick failures
# (any exception type) the main loop tolerates before stopping itself
# rather than retrying forever. At the default 15s poll interval this is
# ~75 seconds of sustained failure - long enough that a couple of
# transient blips (a slow response, a momentary network hiccup) never
# trip it, short enough that a genuinely persistent problem doesn't spam
# alerts indefinitely without ever forcing a human to look at it.
MAX_CONSECUTIVE_TICK_FAILURES = 5


class _FailureEscalator:
    """2026-09-15 fix (flagged twice across review rounds: "some network/
    stream paths still catch broadly and only log"). Reused for any
    internally-caught, internally-swallowed exception path that would
    otherwise never surface past a log line - the websocket reconnect and
    mark-price staleness paths already got this treatment individually;
    this is the same pattern factored out so it's not copy-pasted a third
    and fourth time. Escalates once per sustained streak of failures (not
    on every single one, which would spam the same alert every tick during
    a real outage), and resets the moment a call succeeds again."""

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.count = 0
        self._alerted = False

    def record_failure(self) -> bool:
        """Returns True exactly once per streak, the moment the threshold
        is crossed - False on every other call, including calls after the
        streak has already been alerted on."""
        self.count += 1
        if self.count >= self.threshold and not self._alerted:
            self._alerted = True
            return True
        return False

    def record_success(self):
        self.count = 0
        self._alerted = False


def _leverage_fits_bracket(brackets: list[dict], notional: float, leverage: float) -> tuple[bool, str]:
    """Added 2026-09-15. `brackets` is the normalized, ascending-by-floor
    list get_leverage_brackets() returns for either platform. An empty
    list (fetch/parse failed, or genuinely no data) is treated as "nothing
    to check against" - the caller decides what that means (this function
    just reports True/no-op, matching check_min_notional's own shape for
    an unavailable check elsewhere in this codebase)."""
    if not brackets:
        return True, ""
    for i, b in enumerate(brackets):
        # Cap is inclusive (this bracket covers up to and including its own
        # cap); floor is exclusive for every bracket after the first, so a
        # notional sitting exactly on a boundary unambiguously belongs to
        # the LOWER bracket (the one whose cap it equals), not both.
        floor_ok = notional >= b["notional_floor"] if i == 0 else notional > b["notional_floor"]
        if floor_ok and notional <= b["notional_cap"]:
            if leverage > b["max_leverage"]:
                return False, (
                    f"at this position's notional (~{notional:.2f}), the exchange's own "
                    f"leverage-tier table caps leverage at {b['max_leverage']}x - this pair "
                    f"is configured for {leverage}x, which the exchange would reduce or reject"
                )
            return True, ""
    # Notional exceeds every bracket's cap - the exchange doesn't offer
    # this position ANY leverage above whatever the highest bracket allows.
    highest = brackets[-1]
    return False, (
        f"position notional (~{notional:.2f}) exceeds every leverage bracket the exchange "
        f"publishes for this symbol (highest cap: {highest['notional_cap']:.2f} at "
        f"{highest['max_leverage']}x)"
    )


@dataclass
class RuntimeState:
    status: str = "IDLE"                 # IDLE | IN_POSITION | UNPROTECTED | CLOSING | ERROR | STOPPED
    direction: str | None = None          # LONG | SHORT
    entry_price: float | None = None      # REAL fill price
    qty: float | None = None              # current REAL position size
    sl_price: float | None = None         # the resting stop's price (fixed for the trade)
    entry_order_id: int | None = None
    sl_order_id: int | None = None
    opened_at: float | None = None
    # ---- Base V3 trade state ----
    equity_at_entry: float | None = None  # AVAILABLE balance read at entry (sizing, stop, tracker %)
    stop_target: float | None = None      # the fixed stop price requested at entry (never recalculated)
    entry_atr: float | None = None        # ATR on the signal candle (TP tracking only)
    tp1_price: float | None = None        # tracking only - never an order
    tp2_price: float | None = None
    tp1_touched: bool = False
    tp2_touched: bool = False
    alloc_pct: float | None = None        # trend-aligned or counter-trend allocation used
    leverage: float | None = None         # leverage this trade was sized with (trend / counter)
    bot_qty: float | None = None          # quantity the BOT opened (top-up detection, tracker share)
    entry_candle_time: int | None = None
    held_signal_count: int = 0            # opposite signals ignored by the Hold Rule this trade
    last_decision: str | None = None      # last per-candle decision, for the dashboard
    last_withdraw_check: float = 0.0      # wall-clock throttle for the withdrawal-alert equity check
    closing_reason: str | None = None
    last_candle_time: int | None = None   # latest CLOSED candle seen (display only; see tracker for processing)
    last_error: str | None = None
    feed_stale: bool = False
    last_indicators: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


class BotInstance:
    def __init__(self, account_id: str, account_name: str, symbol: str,
                 api_key: str | None, api_secret: str | None, testnet: bool, pair_config: PairConfig,
                 adapter: BaseExchangeAdapter | None = None, platform: str = "binance"):
        self.account_id = account_id
        self.account_name = account_name
        self.symbol = symbol
        self.pc = pair_config
        self.testnet = testnet
        self.api_key = api_key
        self.api_secret = api_secret
        self.platform = platform  # "binance" or "okx" - see app/exchange_adapter.py
        # Real-time commission cache (item 6 fix, 2026-09-14, per a
        # third-party review): populated by _on_order_update from Binance's
        # own ORDER_TRADE_UPDATE push events for this bot's own orders -
        # gives that event type genuine use (previously registered on the
        # stream but never actually wired to a handler) and lets
        # _fetch_actual_commission use real, already-known data instead of
        # a separate REST call when it's available. Cleared on every
        # position reset so it never grows unbounded across many trades.
        self._commission_cache: dict[int, float] = {}
        self._feed_unhealthy_alerted: bool = False
        # 2026-09-15 fix: same escalation pattern as the feed/reconnect
        # alerts, applied to the two remaining internally-swallowed
        # exception paths in this file (see _FailureEscalator's own
        # docstring) - the leverage-bracket fetch, and the pending-
        # reconciliation resolution query, both of which previously only
        # ever logged a failure with no escalation at all.
        self._bracket_fetch_failures = _FailureEscalator()
        self._pending_reconciliation_query_failures = _FailureEscalator()
        # 2026-09-16 fix (audit finding #1, confirmed real): the main tick
        # loop's broad exception handler previously logged, alerted, slept,
        # and retried FOREVER on any exception - no counter, no threshold,
        # no escalation to a hard stop. A persistent bug or a genuinely
        # broken exchange response would alert Telegram endlessly without
        # the instance ever stopping itself, relying entirely on the
        # operator noticing alert fatigue and intervening by hand. This
        # counter tracks consecutive tick failures; MAX_CONSECUTIVE_TICK_
        # FAILURES below is the threshold that actually stops the loop -
        # see _tick_loop_with_escalation for where it's enforced.
        self._consecutive_tick_failures: int = 0
        # 2026-09-15, owner request/decision: a config edit while a position
        # is open must NOT change that position's behavior mid-trade - the
        # owner's own words: "these parameters should be decided before any
        # trade, not mid trade." Set by the update_pair/update_okx_pair API
        # routes instead of restarting immediately when a position is open;
        # acted on by the manager's own restart_any_pending() once this
        # instance is genuinely flat again (see manager.py/okx_manager.py).
        self._pending_restart_on_flat: bool = False
        # Step 3 (2026-09-14): self.client is now INJECTED (any
        # BaseExchangeAdapter), not hardcoded to Binance. Every existing
        # Binance call site is untouched - if the caller doesn't pass an
        # adapter (every existing Binance call site, and every existing
        # test), behavior is byte-for-byte identical to before this change:
        # a BinanceFuturesClient is still built right here from
        # api_key/api_secret/testnet. Only a NEW OKX call site (BotManager's
        # OKX counterpart) passes adapter= explicitly.
        if adapter is not None:
            self.client = adapter
        else:
            if platform != "binance":
                raise ValueError(
                    f"platform={platform!r} requires an explicit adapter= to be passed in - "
                    f"only platform='binance' can build its own client from api_key/api_secret."
                )
            self.client = BinanceFuturesClient(api_key=api_key, api_secret=api_secret, testnet=testnet,
                                                account_id=account_id)
        self.state = RuntimeState()
        self.logs: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._task: asyncio.Task | None = None
        self._stop_requested = False
        self.log_obj = logging.getLogger(f"bot.{account_id}.{symbol}")
        # 2026-09-15 fix: OKX now gets its own real mark-price websocket
        # feed (OKXMarkPriceFeed), same as Binance - previously a no-op
        # stand-in (NullMarkFeed), deliberately deferred and flagged at the
        # time. `testnet` doubles as OKX's "demo" flag here (same meaning:
        # practice/simulated trading vs live).
        if platform == "binance":
            self.mark_feed = MarkPriceFeed(symbol, testnet=testnet,
                                            stale_after_seconds=pair_config.ws_staleness_seconds)
        elif platform == "okx":
            self.mark_feed = OKXMarkPriceFeed(symbol, demo=testnet,
                                               stale_after_seconds=pair_config.ws_staleness_seconds)
        else:
            self.mark_feed = NullMarkFeed(symbol, stale_after_seconds=pair_config.ws_staleness_seconds)
        # BASE V3 tracker (separate memory per platform + account + pair):
        # decides shadow mode only, never touches a real order. Loaded here
        # so it (and the last processed candle) survive restarts.
        self.tracker = tracker_mod.load(self.platform, account_id, symbol,
                                        pair_config.tracker_start_balance)
        # One-shot alert latches (reset when the situation clears).
        self._foreign_position_alerted: bool = False
        self._topup_alerted: bool = False
        # Set by BotManager right after construction - lets this instance ask
        # "what's my account's total open exposure across my sibling pairs?"
        # without instance.py importing manager.py (avoids a circular import).
        self.manager = None

    # ---------------------------------------------------------------- logging
    def _log(self, msg: str):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        self.logs.append(line)
        self.log_obj.info(msg)

    async def _notify(self, coro):
        try:
            await coro
        except Exception as e:  # notifications must never break the loop
            self._log(f"telegram notify failed: {e}")

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        if self._task and not self._task.done():
            return
        self._stop_requested = False
        self.mark_feed.start()
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        # 2026-09-15 fix (owner request): plain stop() only ever paused the
        # loop - it never closed an open position (by design, for a normal
        # "pause to reconfigure" use case, where resting SL/TP orders keep
        # protecting the position while the bot itself isn't actively
        # managing it). emergency_flatten_and_stop() below is the
        # DIFFERENT, more aggressive action for the new global "STOP ALL"
        # button - closes first, then calls this same stop().
        self._stop_requested = True
        await self._cleanup_external_connections()
        if self._task:
            done, pending = await asyncio.wait([self._task], timeout=30)
            if pending:
                # The task didn't notice _stop_requested within 30s (e.g. stuck
                # deep in a retry/backoff sequence for a single HTTP call).
                # Cancel it explicitly rather than closing the client out from
                # under a still-running task and leaving it to exit on its own
                # unknown schedule.
                self._log("Task did not stop within 30s - cancelling it directly.")
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    self._log(f"Task raised while being cancelled during shutdown: {e}")
        # client.close() deliberately stays HERE, not in the shared cleanup
        # helper above - it must only happen once the task is confirmed
        # actually finished (either it noticed _stop_requested, or was
        # force-cancelled just above), never while it might still be
        # mid-tick using this same client.
        await self.client.close()
        self.state.status = "STOPPED"
        await self._notify(tg.notify_stopped(self.account_name, self.symbol, self.pc.telegram_enabled))

    async def _cleanup_external_connections(self):
        """2026-09-18 fix (found while re-auditing the consecutive-failure
        escalation added earlier - a genuine subtle bug, not present until
        that fix was written): stop() always tore down the mark-price feed
        and unregistered from the shared user-data stream before this
        extraction existed; the NEW escalation path (triggered from
        INSIDE the tick loop's own task when it gives up after repeated
        failures) originally skipped all of this - it only set the status
        and returned, leaving the mark-price WebSocket feed and this
        symbol's user-data-stream registration running indefinitely even
        though the instance had stopped ticking. Extracted so both paths
        share the same real cleanup. Deliberately excludes client.close()
        - see stop()'s own comment on why that one has to happen at a
        different moment depending on which caller this is."""
        await self.mark_feed.stop()
        if self.manager is not None:
            stream = self.manager.user_data_streams.get(self.account_id)
            if stream:
                stream.unregister(self.symbol)
            await self.manager.maybe_teardown_user_data_stream(self.account_id)

    async def emergency_flatten_and_stop(self, reason: str = "manual_flatten_all"):
        """2026-09-15, owner request: the actual "STOP ALL" action. Closes
        any open position first (via the same _close_position/_retry_close
        path every other close in this bot uses - retried, ambiguous-order-
        safe, confirmed against the exchange), THEN pauses the loop via the
        normal stop() above. Does NOT touch this pair's `enabled` flag in
        the store - "START ALL" resuming exactly what was running before is
        what that preserves; this method only acts on the live instance,
        not the saved configuration."""
        if self.state.status in ("IN_POSITION", "UNPROTECTED"):
            self._log(f"Emergency flatten requested ({reason}) - closing the open position first.")
            await self._close_position(reason=reason)
        elif self.state.status == "CLOSING":
            # Already in the middle of closing (e.g. a routine flip
            # close overlapped with this) - let that finish rather than
            # racing a second close attempt against the first.
            await self._retry_close()
        await self.stop()

    async def _run(self):
        # Fail CLOSED: if we can't confirm the account is in one-way position
        # mode, this bot's entire position-tracking model (one signed qty per
        # symbol) may not hold - refuse to trade rather than proceed on an
        # unconfirmed assumption. This is a hard stop, not a retryable tick.
        try:
            await self.client.verify_one_way_mode()
        except Exception as e:
            self.state.status = "ERROR"
            self.state.last_error = str(e)
            self._log(f"Refusing to start: {e}")
            await self._notify(tg.notify_error(self.account_name, self.symbol, str(e),
                                                self.pc.telegram_enabled))
            return  # do NOT enter the trading loop

        # Fail CLOSED here too: startup reconciliation is what tells us whether
        # a real position already exists and what protects it. If it can't be
        # completed, the bot must not proceed into the trading loop with
        # incomplete/incorrect state (e.g. thinking it's flat when a real
        # position exists) - that combination previously caused unhandled
        # exceptions deep in position management. Retry a few times first in
        # case it's just a transient network blip, then give up cleanly.
        reconciled = False
        last_reconcile_error = None
        for attempt in range(1, 4):
            try:
                await self._startup_reconcile()
                reconciled = True
                break
            except Exception as e:
                last_reconcile_error = e
                self._log(f"Startup reconciliation attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    await asyncio.sleep(3)

        if not reconciled:
            self.state.status = "ERROR"
            self.state.last_error = f"startup reconciliation failed after 3 attempts: {last_reconcile_error}"
            self._log(f"Refusing to start: {self.state.last_error}")
            await self._notify(tg.notify_error(self.account_name, self.symbol, self.state.last_error,
                                                self.pc.telegram_enabled))
            return  # do NOT enter the trading loop with incomplete/unknown position state

        await self._notify(tg.notify_started(self.account_name, self.symbol, self.pc.timeframe,
                                              self.pc.telegram_enabled))
        # Deliberately values-free: the configured numbers are never written to the log.
        self._log(f"Bot started on {self.symbol} ({self.pc.timeframe}) - settings loaded from the "
                  f"dashboard (values are not logged).")

        # Real-time SL/TP fill detection (see user_data_stream.py) - the
        # primary, fast path for BINANCE specifically. REST polling in the
        # loop below remains fully active as the safety net regardless of
        # platform; this just gets ahead of it when available. OKX has no
        # equivalent yet (see okx_manager.py's module docstring) - checked
        # explicitly here so OKX pairs log an accurate "not available for
        # this platform" message instead of a misleading "could not start"
        # failure message on every single startup.
        if self.manager is not None and hasattr(self.manager, "get_or_create_user_data_stream"):
            try:
                stream = self.manager.get_or_create_user_data_stream(
                    self.account_id, self.api_key, self.api_secret, self.testnet)
                stream.register(self.symbol, on_algo_update=self._on_algo_update,
                                 on_order_update=self._on_order_update,
                                 on_unhealthy=self._on_stream_unhealthy)
            except Exception as e:
                self._log(f"Could not start real-time update stream ({e}) - "
                          f"continuing on REST polling alone.")
        elif self.platform != "binance":
            self._log(f"No real-time push-based SL/TP detection for platform={self.platform!r} yet - "
                      f"relying on REST reconciliation polling alone (same mechanism Binance itself "
                      f"used before its own real-time stream was added).")

        while not self._stop_requested:
            try:
                await self._tick()
                self._consecutive_tick_failures = 0
            except ExchangeAPIError as e:
                self.state.last_error = str(e)
                self._log(f"Exchange API error: {e}")
                await self._notify(tg.notify_error(self.account_name, self.symbol, str(e),
                                                    self.pc.telegram_enabled))
                if await self._escalate_on_repeated_tick_failure():
                    break
            except Exception as e:
                self.state.last_error = str(e)
                self._log(f"Unexpected error: {e}")
                await self._notify(tg.notify_error(self.account_name, self.symbol, str(e),
                                                    self.pc.telegram_enabled))
                if await self._escalate_on_repeated_tick_failure():
                    break
            await asyncio.sleep(max(self.pc.poll_seconds, 5))

    async def _escalate_on_repeated_tick_failure(self) -> bool:
        """2026-09-16 fix (audit finding #1). Called from both exception
        branches of the main tick loop above. Returns True exactly once
        the failure streak crosses MAX_CONSECUTIVE_TICK_FAILURES, at which
        point the caller breaks out of the loop and this pair stops
        itself - deliberately choosing to stop entirely rather than merely
        "pause new entries", since a tick that can't even complete
        successfully likely also can't be trusted to manage an existing
        position's protection correctly either. This does NOT remove any
        already-resting SL/TP order - those live on the exchange
        independent of whether this loop is running at all; stopping only
        means the bot can no longer react to new conditions (new candle
        signals, Hold Rule / flip closes, shadow steps) until manually
        restarted, which is exactly the point: force a human to look at
        it rather than let a persistent, unknown problem keep silently
        retrying forever."""
        self._consecutive_tick_failures += 1
        if self._consecutive_tick_failures < MAX_CONSECUTIVE_TICK_FAILURES:
            return False
        self.state.status = "ERROR"
        message = (
            f"Stopping this pair - {self._consecutive_tick_failures} consecutive tick failures "
            f"in a row (last error: {self.state.last_error}). Any resting stop order on the "
            f"exchange are untouched by this, but this pair will no longer react to new "
            f"conditions until manually restarted. Investigate and restart when ready."
        )
        self._log(f"ESCALATING: {message}")
        await self._notify(tg.notify_error(self.account_name, self.symbol, message,
                                            self.pc.telegram_enabled))
        # 2026-09-18 fix (found while re-auditing this same escalation
        # feature - a genuine bug, not caught when it was first written):
        # this path exits the tick loop from WITHIN the task itself, which
        # means nothing else will ever call stop()'s own cleanup for this
        # instance - the mark-price feed and this symbol's user-data-stream
        # registration would otherwise keep running indefinitely even
        # though the instance stopped ticking. Mirrors stop()'s cleanup,
        # deliberately NOT calling stop() itself (which tries to
        # asyncio.wait() on self._task - awaiting its own task from inside
        # that same task would deadlock). Safe to close the client here,
        # unlike stop()'s own ordering concern: this coroutine is the task,
        # and it is unconditionally about to return right after this.
        await self._cleanup_external_connections()
        await self.client.close()
        return True

    # ---------------------------------------------------------------- order cleanup
    async def _flag_and_notify_pending_reconciliation(self, context: str, client_order_id: str, detail: str):
        """Item 9 fix (2026-09-14, per a third-party review): called when an
        order's outcome couldn't be determined even after binance_futures.py's
        own ambiguous-response recovery (querying by client-order-id)
        already failed too. Persists a durable, visible flag (survives a
        restart) and sends an ESCALATED Telegram notification distinct from
        the routine error notifications elsewhere in this file, since this
        specific situation means a human may need to check the exchange
        manually - nothing else in the bot's normal retry/reconciliation
        logic is guaranteed to resolve genuine, sustained ambiguity on its
        own. See app/reconciliation.py for the durable-record side, and
        clear_pending_reconciliation_on_success (called after any
        successful order action) for how this gets cleared automatically
        once things are clearly working again."""
        reconciliation.flag_pending_reconciliation(
            self.account_id, self.symbol, context, client_order_id, detail,
        )
        await self._notify(tg.notify_error(
            self.account_name, self.symbol,
            f"⚠️ PENDING RECONCILIATION - {context} outcome could not be confirmed even after "
            f"querying the exchange directly (client order id {client_order_id}). This needs a "
            f"manual check on Binance - the bot cannot currently tell whether this order went "
            f"through. Detail: {detail}",
            self.pc.telegram_enabled,
        ))

    def _clear_pending_reconciliation_on_success(self, client_order_id: str | None = None):
        """Called after any order action completes successfully (entry,
        close, SL/TP placement/amendment).

        BUG FIX (2026-09-15, flagged twice across review rounds, owner-
        approved): this used to ALWAYS blanket-clear every pending record
        for the symbol, on the reasoning that any successful order proves
        the exchange connection works again. That's true, but it doesn't
        prove the SPECIFIC earlier ambiguous order resolved - only that
        connectivity is fine NOW. An unrelated successful SL amendment
        clearing a still-genuinely-unresolved ambiguous ENTRY from minutes
        earlier is exactly the gap both reviews pointed at.

        Now prefers clearing ONLY the record for client_order_id when the
        caller can identify which specific order just succeeded (every
        call site that has access to the order response can pass its
        clientOrderId). Blanket-clear remains as a fallback ONLY when no
        specific id is available, preserving the old behavior for any
        caller that genuinely can't identify one - better than never
        clearing anything in that case."""
        if not reconciliation.has_pending_reconciliation(self.account_id, self.symbol):
            return
        if client_order_id:
            reconciliation.clear_pending_reconciliation(self.account_id, self.symbol, client_order_id)
            self._log(f"Cleared the pending-reconciliation record for order {client_order_id} - "
                      f"it just completed successfully.")
        else:
            reconciliation.clear_pending_reconciliation(self.account_id, self.symbol)
            self._log("Cleared ALL pending-reconciliation flags for this symbol - a subsequent "
                      "order action succeeded, but its specific client order id wasn't available "
                      "to clear just that one record.")

    async def _cancel_own_orders(self):
        """Cancels ONLY orders this bot placed (matched by CLIENT_ORDER_ID_PREFIX) -
        never a symbol-wide blanket endpoint, which would also cancel manual
        orders or orders from another bot/workflow on the same symbol. This
        bot's SL/TP/trailing-stops are algo (conditional) orders - a
        separate Binance system from regular orders since the 2025-12-09
        migration - so this checks the algo order list, not the regular one;
        this bot never creates resting regular orders (entries/closes are
        MARKET, filled instantly)."""
        try:
            open_orders = await self.client.get_open_algo_orders(self.symbol)
        except ExchangeAPIError as e:
            self._log(f"Could not list open algo orders for cleanup ({e}) - skipping this cycle.")
            return
        own = [o for o in open_orders if str(o.get("clientAlgoId", "")).startswith(self.client.client_order_id_prefix)]
        foreign = [o for o in open_orders if o not in own]
        if foreign:
            self._log(f"Leaving {len(foreign)} foreign algo order(s) on {self.symbol} untouched during cleanup.")
        for o in own:
            try:
                await self.client.cancel_algo_order(o["algoId"])
            except ExchangeAPIError:
                pass

    async def _dedupe_own_algo_orders(self, order_type: str,
                                       keep_id: int | None = None) -> tuple[int | None, float | None]:
        """Ensures at most ONE of this bot's own resting algo orders of the
        given type exists on this symbol. If a previous placement attempt
        actually succeeded on Binance's side but its response was lost/
        timed out (the caller never learned its algo ID - see 0l), repeated
        retries at startup OR during an ongoing amendment could otherwise
        accumulate a phantom duplicate over time. If `keep_id` is currently
        resting among them, it's kept; otherwise the most recently created
        one is kept and every other one found is cancelled.
        Returns (kept_algo_id, kept_trigger_price), or (None, None) if none
        exist at all."""
        try:
            open_orders = await self.client.get_open_algo_orders(self.symbol)
        except Exception as e:
            self._log(f"Could not check for duplicate {order_type} orders: {e}")
            return keep_id, None
        own = [o for o in open_orders
               if str(o.get("clientAlgoId", "")).startswith(self.client.client_order_id_prefix)
               and o["orderType"] == order_type]
        if not own:
            return None, None
        own.sort(key=lambda o: o["algoId"])
        keep = next((o for o in own if keep_id is not None and o["algoId"] == keep_id), own[-1])
        if len(own) > 1:
            self._log(f"Found {len(own)} of our own {order_type} algo orders - "
                      f"keeping one (algoId {keep['algoId']}), cancelling the rest.")
            for stale in own:
                if stale["algoId"] != keep["algoId"]:
                    try:
                        await self.client.cancel_algo_order(stale["algoId"])
                    except ExchangeAPIError:
                        pass
        return keep["algoId"], float(keep["triggerPrice"])

    # ---------------------------------------------------------------- startup reconciliation
    async def _startup_reconcile(self):
        pos = await self.client.get_position_risk(self.symbol)
        if pos:
            amt = float(pos["positionAmt"])
            direction = "LONG" if amt > 0 else "SHORT"
            self.state.direction = direction
            self.state.entry_price = float(pos["entryPrice"])
            self.state.qty = abs(amt)
            open_orders = await self.client.get_open_algo_orders(self.symbol)
            own_orders = [o for o in open_orders
                          if str(o.get("clientAlgoId", "")).startswith(self.client.client_order_id_prefix)]
            foreign_orders = [o for o in open_orders if o not in own_orders]
            if foreign_orders:
                self._log(f"Found {len(foreign_orders)} algo order(s) on {self.symbol} this bot did NOT "
                          f"place - leaving them untouched. This bot expects exclusive control "
                          f"of the symbols it trades.")

            # BASE V3 has no take-profit ORDER (TP1/TP2 are tracking only).
            # A bot-tagged TAKE_PROFIT_MARKET still resting here can only be
            # a leftover from the pre-migration strategy - retire it so it
            # can't close a Base V3 trade early.
            for o in own_orders:
                if o.get("orderType") == "TAKE_PROFIT_MARKET":
                    self._log(f"Cancelling leftover take-profit order (algoId {o['algoId']}) - "
                              f"Base V3 does not use take-profit orders.")
                    try:
                        await self.client.cancel_algo_order(o["algoId"])
                    except ExchangeAPIError:
                        pass

            # Keep exactly one of our own stops (dedupe any phantom leftovers).
            self.state.sl_order_id, self.state.sl_price = await self._dedupe_own_algo_orders("STOP_MARKET")

            # Restore the Base V3 trade state saved at entry - cross-checked
            # against the real position first (a mismatching file is stale).
            persisted = position_state.load_position_state(self.account_id, self.symbol)
            if persisted and position_state.matches_resumed_position(
                    persisted, direction, self.state.entry_price):
                self._restore_trade_state(persisted)
                self._log(f"Restored Base V3 trade state from disk: balance at entry "
                          f"{persisted.equity_at_entry:.2f}, fixed stop {persisted.stop_price}, "
                          f"TP1 {persisted.tp1_price} ({'touched' if persisted.tp1_touched else 'not touched'}), "
                          f"TP2 {persisted.tp2_price} ({'touched' if persisted.tp2_touched else 'not touched'}).")
            else:
                if persisted:
                    self._log("Found a saved trade state, but it doesn't match the resumed position "
                              "(direction/entry price) - likely stale from an earlier trade. Not using it.")
                self.state.bot_qty = self.state.qty
                self._log("No matching saved Base V3 trade state for this position - TP1/TP2 "
                          "tracking is unavailable for it, and its tracker % will be approximated "
                          "when it closes. The resting stop (if any) is kept exactly as it is.")

            # Only IN_POSITION (protected) if a real stop is confirmed resting.
            if self.state.sl_order_id:
                self.state.status = "IN_POSITION"
                if self.state.stop_target is None:
                    self.state.stop_target = self.state.sl_price
                self._log(f"Resumed - existing open {direction} position, qty {self.state.qty}, "
                          f"stop confirmed resting at {self.state.sl_price}.")
                await self._notify(tg.notify_resumed(self.account_name, self.symbol, direction,
                                                     self.pc.telegram_enabled))
            else:
                self.state.status = "UNPROTECTED"
                self._log(f"Resumed - existing open {direction} position, qty {self.state.qty}, "
                          f"but NO stop is resting - treating as UNPROTECTED, not a normal resume.")
                await self._notify(tg.notify_error(
                    self.account_name, self.symbol,
                    f"CRITICAL - resumed into an existing {direction} position with no stop. "
                    f"Attempting to protect or flatten immediately.",
                    self.pc.telegram_enabled,
                ))
        else:
            self.state.status = "IDLE"
            # Belt-and-braces cleanup of anything left from a previous run - but
            # ONLY our own orders (see _cancel_own_orders). A blanket symbol-wide
            # cancel here would remove manual or foreign orders on this symbol,
            # which this bot must never touch.
            await self._cancel_own_orders()

        # 2026-10-01: with per-trade leverage, a resumed position may be a counter-trend trade
        # opened at a different leverage than pc.leverage. Never touch leverage under an open
        # position (it would move its liquidation price); each new entry sets its own leverage.
        if not self.state.direction:
            await self.client.set_leverage(self.symbol, int(self.pc.leverage))
        await self.client.set_margin_type(self.symbol, self.pc.isolated_margin)

    def _restore_trade_state(self, persisted):
        self.state.equity_at_entry = persisted.equity_at_entry
        self.state.stop_target = persisted.stop_price
        self.state.entry_atr = persisted.entry_atr
        self.state.tp1_price = persisted.tp1_price
        self.state.tp2_price = persisted.tp2_price
        self.state.tp1_touched = bool(persisted.tp1_touched)
        self.state.tp2_touched = bool(persisted.tp2_touched)
        self.state.alloc_pct = persisted.alloc_pct
        self.state.leverage = getattr(persisted, "leverage", None)
        self.state.bot_qty = persisted.bot_qty if persisted.bot_qty else persisted.qty
        self.state.entry_candle_time = persisted.entry_candle_time
        self.state.opened_at = persisted.opened_at

    def _persist_trade_state(self):
        """Saves the full Base V3 trade state (called at entry and whenever
        a TP tracker flag changes) so a restart restores it exactly."""
        if not self.state.direction or self.state.entry_price is None:
            return
        position_state.save_position_state(
            self.account_id, self.symbol,
            position_state.PositionSnapshot(
                direction=self.state.direction, entry_price=self.state.entry_price,
                qty=self.state.qty or 0.0, equity_at_entry=self.state.equity_at_entry or 0.0,
                opened_at=self.state.opened_at or time.time(),
                stop_price=self.state.stop_target, entry_atr=self.state.entry_atr,
                tp1_price=self.state.tp1_price, tp2_price=self.state.tp2_price,
                tp1_touched=self.state.tp1_touched, tp2_touched=self.state.tp2_touched,
                alloc_pct=self.state.alloc_pct, leverage=self.state.leverage, bot_qty=self.state.bot_qty,
                entry_candle_time=self.state.entry_candle_time,
            ),
        )

    async def _maybe_check_withdraw_alert(self):
        """Account-wide equity threshold check (the reference Pine script's
        alertcondition() for the withdrawal trigger) - notification only, no
        auto-withdrawal. Runs at most once per ~60s, and only from ONE
        designated pair per account (the alphabetically-first symbol
        currently running), so 3 sibling pairs don't triple-fire the same
        alert or triple the API weight cost of an extra equity check that
        isn't otherwise part of the per-tick budget."""
        if self.manager is None:
            return
        if time.time() - self.state.last_withdraw_check < 60:
            return
        self.state.last_withdraw_check = time.time()

        # 2026-09-16 fix (flagged twice across review rounds): the leader
        # used to be picked from EVERY sibling instance regardless of
        # status - if the alphabetically-first symbol happened to be
        # stopped, nobody checked the threshold at all (not it, since it
        # isn't ticking; not the others, since they assumed it was
        # handled). Now only RUNNING siblings are considered, so the
        # responsibility naturally shifts to the next running pair the
        # moment the current leader stops.
        siblings = sorted(i.symbol for i in self.manager.instances_for_account(self.account_id)
                          if i.state.status != "STOPPED")
        if siblings and siblings[0] != self.symbol:
            return  # not the designated leader for this account - another pair handles it

        acc = self.manager.store.get_account(self.account_id)
        if not acc or not acc.withdraw_alert_enabled or acc.withdraw_alert_fired or not acc.withdraw_alert_threshold:
            return

        try:
            equity = await self.client.get_equity()
        except Exception as e:
            self._log(f"Could not check equity for withdrawal alert: {e}")
            return

        if equity >= acc.withdraw_alert_threshold:
            self._log(f"Withdrawal alert threshold reached: equity {equity:.2f} >= {acc.withdraw_alert_threshold:.2f}")
            await self._notify(tg.notify_withdraw_alert(self.account_name, equity,
                                                          acc.withdraw_alert_threshold, self.pc.telegram_enabled))
            self.manager.store.mark_withdraw_alert_fired(self.account_id)

    async def _fetch_snapshot(self, p: strat.StrategyParams):
        """Fetches HISTORY_CANDLES closed candles and computes the Base V3
        snapshot for the latest CLOSED candle. Pure read - it does NOT
        decide whether the candle is new (that is _tick's job, against the
        persisted last-processed candle), so helper callers (restart
        recovery etc.) can never accidentally "use up" a candle.
        Returns (snapshot, df_with_indicators) or (None, None) if there
        isn't enough history yet."""
        raw = await self.client.get_klines(self.symbol, self.pc.timeframe,
                                           limit=strat.HISTORY_CANDLES + 1)
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tbbav", "tbqav", "ignore",
        ])
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        df["open_time"] = df["open_time"].astype("int64")

        # Only CLOSED candles are valid for signals - the exchange returns
        # the still-forming candle as the last row, so drop it.
        closed = df.iloc[:-1].reset_index(drop=True)
        if len(closed) < p.min_bars_required:
            return None, None

        ind = strat.compute_indicators(closed, p)
        snap = strat.last_snapshot(ind)
        self.state.last_candle_time = snap.open_time
        self.state.last_indicators = snap.to_display_dict()
        self.state.updated_at = time.time()
        return snap, ind

    # ---------------------------------------------------------------- main tick
    async def _tick(self):
        # Highest priority, every single tick, before anything else: if a
        # previous cycle left this position unprotected (stop placement
        # failed after a market entry), retry resolving that BEFORE
        # evaluating any new klines/signals. This must never be starved.
        if self.state.status == "UNPROTECTED":
            await self._resolve_unprotected_position()
            return

        # Second-highest priority: a close was requested but not yet confirmed
        # flat by the exchange - keep retrying every tick, not gated on the
        # next candle, until exchange truth actually confirms it.
        if self.state.status == "CLOSING":
            await self._retry_close()
            return

        # Stale mark-price feed: edge-triggered alert; only ever pauses NEW
        # entries (inside _do_enter), never managing/closing a position.
        self.state.feed_stale = self.mark_feed.is_stale()
        if self.state.feed_stale and not self._feed_unhealthy_alerted:
            self._feed_unhealthy_alerted = True
            await self._notify(tg.notify_error(
                self.account_name, self.symbol,
                f"Mark-price feed has been stale for longer than "
                f"{self.mark_feed.stale_after_seconds:.0f}s - new entries are paused until it "
                f"recovers. Existing positions and their stop are unaffected.",
                self.pc.telegram_enabled,
            ))
        elif not self.state.feed_stale:
            self._feed_unhealthy_alerted = False

        await self._maybe_check_withdraw_alert()

        p = _params_from_pair(self.pc)
        snap, ind = await self._fetch_snapshot(p)
        if snap is None:
            self._log(f"Waiting for more history (need {p.min_bars_required} closed candles).")
            return

        # Reconcile with exchange truth every tick (stop fills, manual closes, etc.)
        pos = await self.client.get_position_risk(self.symbol)

        if pos is None and self.state.status == "IN_POSITION":
            # The stop filled (or the position was closed outside the bot)
            # between ticks. Finalize it, then CONTINUE to this candle's
            # normal processing below - if this poll is also the first one
            # after a candle close, Pine would still evaluate that candle
            # as flat (stop filled intrabar -> entry allowed on its close).
            await self._handle_position_closed_externally()
            if self.state.status != "IDLE":
                return

        if pos is not None and self.state.status == "IDLE":
            # A position exists that this bot did not open (manual trade on
            # a bot pair). Never trade on top of it; alert once; keep the
            # paper (shadow) side ticking so shadow timing stays correct.
            if not self._foreign_position_alerted:
                self._foreign_position_alerted = True
                msg = (f"A position exists on {self.symbol} that this bot did not open "
                       f"(size {pos.get('positionAmt')}). Real trading on this pair is PAUSED "
                       f"until it is closed. The bot will not touch it.")
                self._log(msg)
                await self._notify(tg.notify_error(self.account_name, self.symbol, msg,
                                                   self.pc.telegram_enabled))
            if self._is_new_candle(snap):
                self._mark_candle_processed(snap)
                await self._run_shadow_step(snap, p)
            return
        if pos is None:
            self._foreign_position_alerted = False

        if pos is not None and self.state.status == "IN_POSITION":
            await self._check_position_size_changes(pos)

        if not self._is_new_candle(snap):
            return
        # First-reversal rule: while flat and the pair has never made a real trade, arm it (once).
        if pos is None:
            self._arm_first_reversal_if_untraded(snap, ind)
        # Mark BEFORE acting (persisted) - a candle is never processed twice,
        # even if something below raises or the process restarts.
        self._mark_candle_processed(snap)

        # Same order as the Pine script: shadow block first, then real orders.
        await self._run_shadow_step(snap, p)

        if pos is None:
            shadow = self.tracker.get_shadow()
            if strat.real_entry_allowed(shadow, snap.open_time):
                if self._first_reversal_blocks_entry(snap, ind):
                    return
                await self._maybe_enter(snap, p)
            else:
                direction = strat.entry_direction(snap)
                if shadow.active:
                    self.state.last_decision = (f"SHADOW - real entries paused "
                                                f"({shadow.count}/{p.shadow_trades} paper trades done)")
                else:
                    self.state.last_decision = "SHADOW ENDED this candle - real entries resume next candle"
                if direction:
                    self._log(f"{direction} signal on this candle - real entry skipped: "
                              f"{self.state.last_decision}.")
            return

        await self._manage_open_position(snap, p)

    # ---------------------------------------------------------------- first-reversal rule
    def _arm_first_reversal_if_untraded(self, snap: strat.IndicatorSnapshot, ind) -> None:
        """Owner decision 2026-10-01 (\"NEVER MID TREND\"): ANY pair that has not made its first real trade
        yet - brand new, or an existing tracker with no real trades - must not take that first trade
        mid-trend. Armed once (while flat); persists in the tracker file until the first real entry. A
        pair that already has real trades is never armed, so running pairs keep their behaviour.
        Per-pair switch: wait_first_reversal."""
        t = self.tracker
        if not self.pc.wait_first_reversal:   # always set: a pair with a blank setting cannot start
            return
        if t.first_reversal is not None or t.real_trades:
            return
        ref = strat.last_signal_direction(ind, len(ind) - 1) if ind is not None else None
        t.first_reversal = {"waiting": True, "reference": ref, "armed_candle": snap.open_time}
        tracker_mod.save(t)
        self._log(f"First-reversal rule ARMED: this pair has never traded, so no entry on this candle "
                  f"(latest signal direction: {ref or 'none yet'}). The first trade waits for a fresh "
                  f"{'SHORT' if ref == 'LONG' else 'LONG' if ref == 'SHORT' else 'LONG/SHORT'} reversal.")

    def _first_reversal_blocks_entry(self, snap: strat.IndicatorSnapshot, ind) -> bool:
        """True = do NOT enter on this candle. Fails CLOSED if the indicator frame is unavailable."""
        fr = self.tracker.first_reversal
        if not fr or not fr.get("waiting"):
            return False
        if fr.get("armed_candle") == snap.open_time:
            # The candle the rule was armed on is never traded, even if it happens to be a reversal: the
            # bot may be starting hours after it closed, so entering now would be a late (mid-trend) entry.
            self.state.last_decision = "WAITING first reversal - rule armed on this candle, no entry on it"
            self._log(f"First-reversal rule: {self.state.last_decision}.")
            return True
        if ind is not None and strat.is_fresh_reversal(ind):
            self._log("First-reversal rule: a fresh reversal signal on this candle - releasing the first entry.")
            return False
        direction = strat.entry_direction(snap)
        self.state.last_decision = (f"WAITING first reversal (latest direction "
                                    f"{fr.get('reference') or 'none'}) - no mid-trend first entry")
        if direction:
            self._log(f"{direction} signal on this candle - first entry skipped: {self.state.last_decision}.")
        return True

    def _clear_first_reversal(self) -> None:
        fr = self.tracker.first_reversal
        if fr and fr.get("waiting"):
            fr["waiting"] = False
            tracker_mod.save(self.tracker)
            self._log("First-reversal rule satisfied: first real entry opened. Normal entries from now on.")

    # ---------------------------------------------------------------- candle bookkeeping
    def _is_new_candle(self, snap: strat.IndicatorSnapshot) -> bool:
        last = self.tracker.last_processed_candle
        return last is None or snap.open_time > last

    def _mark_candle_processed(self, snap: strat.IndicatorSnapshot) -> None:
        self.tracker.last_processed_candle = snap.open_time
        tracker_mod.save(self.tracker)

    async def _run_shadow_step(self, snap: strat.IndicatorSnapshot, p: strat.StrategyParams) -> None:
        """Pine shadow block for this closed candle. Paper only - never
        sends anything to the exchange."""
        shadow = self.tracker.get_shadow()
        if not shadow.active:
            return
        try:
            tick = (await self.client.get_symbol_info(self.symbol)).price_tick or 0.0
        except Exception as e:
            tick = 0.0
            self._log(f"Could not read the price tick for paper slippage ({e}) - using 0.")
        events = strat.shadow_step(shadow, snap, p, tick, max_history=tracker_mod.MAX_HISTORY)
        self.tracker.set_shadow(shadow)
        tracker_mod.save(self.tracker)
        for ev in events:
            self._log(f"[SHADOW] {ev}")
        if events and any("ended" in ev for ev in events):
            await self._notify(tg.notify_info(
                self.account_name, self.symbol,
                f"Shadow mode ended after {p.shadow_trades} paper trades ({shadow.last_end_reason}). "
                f"Real trading resumes.", self.pc.telegram_enabled, email_kind="Shadow Ended"))

    async def _check_position_size_changes(self, pos: dict) -> None:
        """Manual top-up / partial close detection (owner decision
        2026-09-28). The bot never resizes its stop for this:
          * bigger than the bot opened -> alert once (the stop still closes
            the WHOLE position, so the loss at the stop would exceed the
            configured % of balance);
          * smaller -> update the tracked size and keep managing the rest."""
        try:
            real_qty = abs(float(pos.get("positionAmt", 0)))
        except (TypeError, ValueError):
            return
        if not real_qty or self.state.qty is None:
            return
        bot_qty = self.state.bot_qty or self.state.qty
        if real_qty > bot_qty + 1e-9:
            if not self._topup_alerted:
                self._topup_alerted = True
                msg = (f"Position size on {self.symbol} is now {real_qty}, bigger than the {bot_qty} "
                       f"the bot opened (manual top-up?). The fixed stop at {self.state.sl_price} "
                       f"will close the WHOLE position, so the loss there would be larger than "
                       f"{self.pc.stop_loss_pct_equity}% of balance. The bot will not move the stop. "
                       f"The tracker will only count the bot's own {bot_qty}.")
                self._log(msg)
                await self._notify(tg.notify_error(self.account_name, self.symbol, msg,
                                                   self.pc.telegram_enabled))
        if abs(real_qty - self.state.qty) > 1e-9:
            self._log(f"Real position size changed {self.state.qty} -> {real_qty} - tracking the real size.")
            self.state.qty = real_qty

    # ---------------------------------------------------------------- entries
    async def _maybe_enter(self, snap: strat.IndicatorSnapshot, p: strat.StrategyParams):
        # Serialize the exposure-check -> market-entry sequence per account so
        # two sibling pairs on the same account can't both read the same
        # pre-entry exposure and both pass the cap in the same instant (see
        # risk_guard.py and manager.get_account_lock).
        if self.manager is not None:
            async with self.manager.get_account_lock(self.account_id):
                await self._do_enter(snap, p)
        else:
            await self._do_enter(snap, p)

    async def _do_enter(self, snap: strat.IndicatorSnapshot, p: strat.StrategyParams):
        direction = strat.entry_direction(snap)
        if direction is None:
            self.state.status = "IDLE"
            self.state.last_decision = "NO SIGNAL"
            return

        # BUG FIX (2026-09-14, found during a third-party review, owner-
        # approved): a pending-reconciliation flag (an earlier order whose
        # outcome couldn't be confirmed even after the adapter's own
        # ambiguous-response recovery) used to be purely informational - it
        # showed up in logs/Telegram/dashboard, but nothing stopped a brand
        # new entry from being placed on the SAME symbol while the earlier
        # order's real outcome was still unknown. Now actively resolved
        # here rather than just blocked forever: query the exchange for
        # ground truth right now. If it confirms flat (no unexpected
        # position), the ambiguity clearly didn't result in an open
        # position - safe to clear every pending record for this symbol and
        # proceed with this entry in the same tick. If a position DOES
        # exist, or the query itself fails, refuse to enter and stay
        # flagged - a human needs to look at this, not the bot guessing.
        if reconciliation.has_pending_reconciliation(self.account_id, self.symbol):
            try:
                pos = await self.client.get_position_risk(self.symbol)
                self._pending_reconciliation_query_failures.record_success()
            except Exception as e:
                self._log(f"Pending reconciliation exists and the exchange couldn't be reached "
                          f"to resolve it ({e}) - refusing to open a new position until this "
                          f"is confirmed one way or the other.")
                if self._pending_reconciliation_query_failures.record_failure():
                    await self._notify(tg.notify_error(
                        self.account_name, self.symbol,
                        f"Could not resolve a pending-reconciliation flag against the exchange "
                        f"{self._pending_reconciliation_query_failures.threshold} times in a row "
                        f"({e}). New entries stay blocked on this symbol until this is confirmed "
                        f"one way or the other - this may need a manual check.",
                        self.pc.telegram_enabled,
                    ))
                return
            if pos is not None:
                self._log("Pending reconciliation exists AND the exchange shows an open position "
                          "on this symbol - refusing to open a new position on top of it. "
                          "This needs a manual check.")
                return
            # Confirmed flat - the earlier ambiguity didn't leave a real
            # position behind. Safe to clear and proceed.
            self._log("Pending reconciliation resolved: exchange confirms flat. "
                      "Clearing the flag and proceeding with this entry.")
            reconciliation.clear_pending_reconciliation(self.account_id, self.symbol)

        # Watchdog only - never blocks managing/closing an existing position,
        # only pauses taking a brand-new one while the live price feed is stale.
        self.state.feed_stale = self.mark_feed.is_stale()
        if self.state.feed_stale:
            self._log("Mark-price feed is stale - skipping this entry signal until it recovers.")
            return

        # Sizing: read the AVAILABLE balance for this account at the moment
        # of entry, and use that ONE number for the size, the fixed stop and
        # the tracker %.
        #   allocation = trend% if the smoothed close is on the trend side of the
        #                sizing EMA, else counter%  - the sizing EMA never blocks
        #   notional   = balance x allocation% x leverage
        #   qty        = notional / NORMAL candle close  (never a smoothed price)
        # Available balance (not total equity) keeps one pair's entry from
        # ignoring margin a sibling pair on the same account is already using.
        available_balance = await self.client.get_available_balance()
        alloc = strat.allocation_pct(direction, snap)
        # Leverage is per trade type (trend-aligned vs counter-trend), chosen by the
        # same sizing-EMA test as the allocation. The stop is derived from the resulting qty below.
        lev = strat.trade_leverage(direction, snap, p)
        raw_qty = strat.position_qty(available_balance, alloc, lev, snap.close)
        qty = await self.client.round_qty(self.symbol, raw_qty)
        if qty <= 0:
            self._log(f"Computed qty rounded to 0 (available balance {available_balance}, "
                      f"allocation {alloc}%, close {snap.close}) - skipping entry.")
            return

        notional_ok, notional_msg = await self.client.check_min_notional(self.symbol, qty, snap.close)
        if not notional_ok:
            self._log(f"Skipping entry: {notional_msg}")
            await self._notify(tg.notify_error(self.account_name, self.symbol, notional_msg,
                                                self.pc.telegram_enabled))
            return

        # 2026-09-15 fix: the min/max notional check above catches an order
        # the exchange will flatly reject, but says nothing about whether
        # the CONFIGURED LEVERAGE is actually achievable at this position's
        # notional value - a large enough position at high leverage can
        # land in a lower-max-leverage bracket than the pair is set to use,
        # which the exchange handles by capping/rejecting, not something
        # this bot should only discover after the fact. Fails OPEN on a
        # fetch/parse failure (this is a supplementary check, not something
        # to add as a new way entries get blocked over a network hiccup -
        # the exchange still enforces its own limits regardless), fails
        # CLOSED only on a genuine, successfully-confirmed bracket mismatch.
        try:
            brackets = await self.client.get_leverage_brackets(self.symbol)
            self._bracket_fetch_failures.record_success()
        except Exception as e:
            self._log(f"Could not fetch leverage brackets ({e}) - skipping this check; "
                      f"the exchange still enforces its own limits regardless.")
            brackets = []
            if self._bracket_fetch_failures.record_failure():
                await self._notify(tg.notify_error(
                    self.account_name, self.symbol,
                    f"Leverage-bracket check has failed {self._bracket_fetch_failures.threshold} "
                    f"times in a row ({e}). Entries are proceeding WITHOUT this check (the exchange "
                    f"still enforces its own limits regardless) - this may be worth a look if it "
                    f"keeps happening.",
                    self.pc.telegram_enabled,
                ))
        bracket_ok, bracket_msg = _leverage_fits_bracket(brackets, qty * snap.close, lev)
        if not bracket_ok:
            self._log(f"Skipping entry: {bracket_msg}")
            await self._notify(tg.notify_error(self.account_name, self.symbol, bracket_msg,
                                                self.pc.telegram_enabled))
            return

        # TOTAL equity is used ONLY for the account exposure-cap ratio below
        # (a "how leveraged is my whole book" check) - never for sizing or
        # the stop, which both use the available balance read above.
        equity = await self.client.get_equity()

        # The exposure CAP, unlike sizing above, deliberately stays on TOTAL
        # equity - it's a standard "how leveraged is my whole book" ratio
        # (notional exposure / equity), and using available balance as the
        # denominator here would be circular: available balance shrinks
        # precisely because of existing exposure, which would make the cap
        # behave backwards (easier to hit at low exposure, oddly harder to
        # reason about at high exposure) instead of a stable ceiling.
        if self.manager is not None:
            acc = self.manager.store.get_account(self.account_id)
            cap = acc.max_account_exposure_pct if acc else None
            if cap:
                # Real exchange data, not local instance state - see
                # risk_guard.py's module docstring for why (a crashed/
                # never-started sibling instance, or any position this
                # process doesn't know about, would otherwise be invisible
                # to this check).
                try:
                    real_positions = await self.client.get_all_open_positions()
                except Exception as e:
                    self._log(f"Could not fetch real positions for exposure check ({e}) - "
                              f"skipping entry rather than risking an uncapped check.")
                    return
                allowed, exp_msg = risk_guard.check_new_entry_allowed(
                    real_positions,
                    exclude_symbol=self.symbol, new_notional=qty * snap.close,
                    equity=equity, max_account_exposure_pct=cap,
                )
                if not allowed:
                    self._log(f"Entry skipped: {exp_msg}")
                    await self._notify(tg.notify_error(self.account_name, self.symbol, exp_msg,
                                                        self.pc.telegram_enabled))
                    return

        side = "BUY" if direction == "LONG" else "SELL"

        # Explicit account-owner instruction: this pair is never manually
        # traded, so any resting order here (regardless of origin) is either
        # this bot's own leftover or something that shouldn't exist. In
        # one-way mode, a leftover position/order would silently ADD TO the
        # new entry rather than create an isolated one, corrupting this
        # trade's own fixed-stop math (which assumes a clean
        # entry price and qty). Unlike the routine reconciliation paths
        # elsewhere (which only ever cancel this bot's own tagged orders, to
        # protect a genuinely shared symbol), this is a deliberate blanket
        # cleanup, only run right before a brand-new entry, on an
        # owner-declared bot-exclusive symbol.
        try:
            await self.client.cancel_all_open_orders(self.symbol)
        except ExchangeAPIError as e:
            # 2026-09-14 fix (item 13, per a third-party review): previously
            # proceeded with the entry regardless. Now re-verifies the
            # symbol is actually clean before deciding - if the cleanup
            # call itself failed but nothing is genuinely resting anyway
            # (e.g. the failure was network/timing-related, not a real
            # rejection), it's still safe to proceed; only skip the entry
            # if something is CONFIRMED still resting.
            self._log(f"Pre-entry cleanup (cancel all orders) failed: {e} - verifying the symbol "
                      f"is actually clean before deciding whether to proceed.")
            try:
                regular_orders = await self.client.get_open_orders(self.symbol)
                algo_orders = await self.client.get_open_algo_orders(self.symbol)
            except Exception as e2:
                self._log(f"Could not verify the symbol is clean either ({e2}) - skipping this "
                          f"entry rather than risking a new position alongside unknown stale orders.")
                return
            if regular_orders or algo_orders:
                self._log(f"Cleanup failed AND {len(regular_orders) + len(algo_orders)} order(s) "
                          f"are still confirmed resting on {self.symbol} - skipping this entry "
                          f"rather than proceeding with stale orders present.")
                await self._notify(tg.notify_error(
                    self.account_name, self.symbol,
                    "Entry skipped: pre-entry cleanup failed and orders are still confirmed resting.",
                    self.pc.telegram_enabled,
                ))
                return
            self._log("Verified the symbol is actually clean despite the cleanup call's own "
                      "error - proceeding with the entry.")

        # 2026-10-01: set the exchange leverage for THIS trade's type immediately before entering
        # (flat here, so changing it is safe). Fail CLOSED: entering at the wrong leverage would
        # move the liquidation price, so a failure here skips the entry rather than proceeding.
        try:
            await self.client.set_leverage(self.symbol, int(lev))
        except Exception as e:
            lev_msg = (f"Entry skipped: could not set exchange leverage to {int(lev)}x for this "
                       f"{direction} trade ({e}). Not entering at an unconfirmed leverage.")
            self._log(lev_msg)
            await self._notify(tg.notify_error(self.account_name, self.symbol, lev_msg,
                                                self.pc.telegram_enabled))
            return

        try:
            order = await self.client.market_order(self.symbol, side, qty)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # Item 9 fix (2026-09-14, per a third-party review): market_order
            # already tried its own ambiguous-response recovery (querying by
            # client-order-id - see binance_futures.py) and STILL couldn't
            # resolve it. Nobody would otherwise be tracking that a real
            # position might exist right now, completely unmanaged, until
            # the next startup reconciliation happens to notice it. Flag a
            # durable, visible record and escalate loudly, then re-raise -
            # this is additive (visibility + durability), not a replacement
            # for whatever the caller already does with this exception.
            await self._flag_and_notify_pending_reconciliation(
                context="market entry", client_order_id=getattr(e, "client_order_id", "unknown"),
                detail=str(e),
            )
            raise
        entry_price = float(order.get("avgPrice") or 0) or None
        entry_order_id = order.get("orderId")
        if entry_price is None:
            # BUG FIX (2026-09-14, found during a third-party review, owner-
            # approved): this used to fall back straight to the candle's
            # CLOSE price when the order response didn't include a fill
            # price - not the real fill, and it fed SL/TP/trailing/Max Loss
            # Cap/ledger P&L for the entire life of the trade. The close
            # side already recovers the REAL fill via get_user_trades()
            # when its response is missing a price - entry never did the
            # same thing. Now mirrors that exact recovery: try the real
            # fill first, mark price only as a last resort, candle close
            # never used as a stand-in for what was actually paid.
            try:
                recent_trades = await self.client.get_user_trades(
                    self.symbol, order_id=entry_order_id, limit=5)
                if recent_trades:
                    entry_price = float(recent_trades[-1].get("price") or 0) or None
            except Exception as e:
                self._log(f"Could not recover the real fill price after entry ({e}) - "
                          f"falling back to a mark-price estimate.")
            if entry_price is None:
                try:
                    entry_price = await self.client.get_mark_price(self.symbol)
                except Exception:
                    entry_price = snap.close  # last resort only - see class docstring above
                self._log(f"Order response had no fill price - recovered {entry_price} "
                          f"as an estimate rather than using it directly.")
        self._clear_pending_reconciliation_on_success(order.get("clientOrderId"))

        # BUG FIX (2026-09-15, found during a third-party review, owner-
        # approved): qty here was still the locally CALCULATED size (from
        # position sizing math, before rounding to the exchange's step
        # size) - never replaced with what actually got filled. A market
        # order almost always fills completely, but a partial fill is a
        # real exchange possibility, and trusting the assumed size means
        # SL/TP would be sized for a position that may not exactly match
        # the real one. Mirrors the same principle already used on the
        # CLOSE side (re-query the real position rather than trust a
        # locally cached number) - query it here too, right after entry,
        # before it's used for anything.
        try:
            real_pos = await self.client.get_position_risk(self.symbol)
            if real_pos is not None:
                real_qty = abs(float(real_pos["positionAmt"]))
                if abs(real_qty - qty) > 1e-9:
                    self._log(f"Real filled quantity ({real_qty}) differs from the calculated "
                              f"size ({qty}) - using the real exchange quantity for the fixed stop.")
                qty = real_qty
            else:
                self._log("Position query right after entry shows flat - unexpected given the "
                          "order just succeeded. Using the calculated quantity, but flagging this.")
        except Exception as e:
            self._log(f"Could not confirm the real filled quantity ({e}) - using the calculated "
                      f"size ({qty}) rather than blocking the entry over this.")

        # From this point on, a real position is open on the exchange. Every
        # failure path below must end with either a protected position or an
        # explicit UNPROTECTED state that gets retried every tick - never
        # silently falling back to IDLE while a real position is open.
        #
        # BASE V3 FIXED STOP from the REAL fill and the REAL filled qty:
        #   stop = fill -/+ (available_balance_at_entry x stop%) / real_qty
        entry_atr = snap.atr_sc
        tp1, tp2 = strat.tp_levels(direction, entry_price, entry_atr, p)
        requested_stop = strat.stop_price(direction, entry_price, available_balance, qty, p)

        self.state.direction = direction
        self.state.entry_price = entry_price
        self.state.qty = qty
        self.state.bot_qty = qty
        self.state.entry_order_id = entry_order_id
        self.state.opened_at = time.time()
        self.state.equity_at_entry = available_balance
        self.state.stop_target = requested_stop
        self.state.entry_atr = entry_atr
        self.state.tp1_price, self.state.tp2_price = tp1, tp2
        self.state.tp1_touched = self.state.tp2_touched = False
        self.state.alloc_pct = alloc
        self.state.leverage = lev
        self.state.entry_candle_time = snap.open_time
        self._clear_first_reversal()
        self.state.held_signal_count = 0
        self._topup_alerted = False
        # Persist BEFORE placing the stop, so even a crash right here leaves
        # the exact fixed stop on disk for restart recovery to use.
        self._persist_trade_state()

        try:
            sl_order, sl_price = await self._place_stop_order(direction, requested_stop)
        except StopAlreadyCrossedError as crossed:
            self.state.status = "IN_POSITION"
            msg = (f"Price already moved through the fixed stop before it could be placed "
                   f"(requested stop {crossed.requested}, mark {crossed.mark}). The stop has "
                   f"effectively been hit - closing at market. The stop price was NOT moved.")
            self._log(msg)
            await self._notify(tg.notify_error(self.account_name, self.symbol, msg, self.pc.telegram_enabled))
            await self._close_position(reason="stop_crossed_at_entry")
            return
        except Exception as e1:
            self._log(f"Stop placement failed right after entry ({e1}) - retrying once immediately.")
            await asyncio.sleep(2)
            try:
                sl_order, sl_price = await self._place_stop_order(direction, requested_stop)
            except StopAlreadyCrossedError as crossed:
                self.state.status = "IN_POSITION"
                self._log(f"Stop {crossed.requested} already crossed (mark {crossed.mark}) on retry - "
                          f"closing at market.")
                await self._close_position(reason="stop_crossed_at_entry")
                return
            except Exception as e2:
                await self._enter_unprotected_state(direction, qty, entry_price,
                                                    reason=f"stop placement failed twice after entry: {e2}")
                return

        self.state.status = "IN_POSITION"
        self.state.sl_price = sl_price
        self.state.sl_order_id = sl_order.get("algoId")
        self.state.last_decision = f"ENTERED {direction}"
        self._persist_trade_state()

        self._log(f"Opened {direction} {qty} @ {entry_price} | balance {available_balance:.2f} | "
                  f"allocation {alloc}% | fixed stop {sl_price} (requested {requested_stop}) | "
                  f"TP1 {tp1} / TP2 {tp2} (tracking only)")
        await self._notify(tg.notify_entry(
            self.account_name, self.symbol, direction, qty, entry_price,
            sl_price, tp1, tp2, lev, self.pc.telegram_enabled, alloc_pct=alloc,
        ))

    async def _find_own_resting_stop(self) -> dict | None:
        """Checks for any of THIS bot's own resting STOP_MARKET algo orders
        on this symbol right now. Used before placing a stop so a retry
        (after a placement call raised due to a lost/timed-out response,
        not necessarily a real rejection) can detect and reuse an order
        that actually succeeded on the exchange instead of blindly placing
        a duplicate. If several are found, picks the most recent (highest
        algoId) - same convention as startup reconciliation."""
        try:
            open_orders = await self.client.get_open_algo_orders(self.symbol)
        except Exception as e:
            self._log(f"Could not check for an existing stop before placing a new one: {e}")
            return None
        own = [o for o in open_orders if str(o.get("clientAlgoId", "")).startswith(self.client.client_order_id_prefix)]
        own_stops = sorted([o for o in own if o["orderType"] == "STOP_MARKET"], key=lambda o: o["algoId"])
        return own_stops[-1] if own_stops else None

    async def _place_stop_order(self, direction: str, requested_stop: float):
        """Places the ONE Base V3 protective order: a reduce-only /
        close-whole-position STOP_MARKET at the FIXED stop price. Base V3
        places NO take-profit order (TP1/TP2 are tracking only).

        Raises on failure (deliberately) so the caller can retry/escalate.
        Raises StopAlreadyCrossedError if the requested stop is already on
        the wrong side of the mark price - the stop is never moved to make
        it valid (that would change the strategy stop); the caller closes
        at market instead.

        Before placing, reuses an already-resting bot stop if one exists
        (a previous attempt whose confirmation was lost) instead of placing
        a duplicate. Logs requested vs accepted price after exchange
        rounding."""
        stop_price = await self.client.round_price(self.symbol, requested_stop)
        close_side = "SELL" if direction == "LONG" else "BUY"

        existing = await self._find_own_resting_stop()
        if existing is not None:
            self._log(f"Found an already-resting stop (algoId {existing['algoId']}) - reusing it "
                      f"instead of placing a duplicate (likely a prior attempt whose confirmation was lost).")
            return existing, float(existing["triggerPrice"])

        mark_price = await self.client.get_mark_price(self.symbol)
        ok, msg = self.client.validate_stop_side(direction, stop_price, mark_price)
        if not ok:
            raise StopAlreadyCrossedError(stop_price, mark_price, msg)

        order = await self.client.stop_market_order(self.symbol, close_side, stop_price,
                                                    skip_throttle=True,
                                                    trigger_px_type=getattr(self.pc, "trigger_px_type", "mark"))
        if abs(stop_price - requested_stop) > 0:
            self._log(f"Stop requested {requested_stop} -> placed at {stop_price} "
                      f"(exchange price-tick rounding).")
        return order, stop_price

    # ---------------------------------------------------------------- naked-position recovery
    async def _enter_unprotected_state(self, direction: str, qty: float, entry_price: float, reason: str):
        """A position is open with NO resting stop. Log/notify loudly, then
        immediately try to flatten it (removing the risk entirely, rather than
        leaving a real position with no protection). If even that fails, park
        in UNPROTECTED so `_tick` retries every single cycle until resolved."""
        self.state.status = "UNPROTECTED"
        self.state.direction = direction
        self.state.qty = qty
        self.state.entry_price = entry_price
        self.state.opened_at = self.state.opened_at or time.time()
        self._log(f"CRITICAL: {reason} - position is OPEN with NO resting stop.")
        await self._notify(tg.notify_error(
            self.account_name, self.symbol,
            f"CRITICAL - unprotected {direction} position ({reason}). Attempting emergency close.",
            self.pc.telegram_enabled,
        ))
        await self._resolve_unprotected_position()

    async def _resolve_unprotected_position(self):
        """Called on entry into UNPROTECTED, and again every tick thereafter
        until it's resolved. Order of preference: (1) confirm the position is
        actually still open at all - if Binance already shows it flat, just
        recover into IDLE; (2) try once more to place the fixed stop so the
        position can stay open, protected; (3) if that still fails, flatten
        immediately; (4) if even flattening fails, stay UNPROTECTED and try
        again next tick - this is the only state where that's acceptable,
        because the alternative is silently forgetting a real position."""
        direction, qty, entry_price = self.state.direction, self.state.qty, self.state.entry_price
        if not direction or not qty:
            self.state.status = "IDLE"
            return

        try:
            pos = await self.client.get_position_risk(self.symbol)
        except Exception as e:
            self._log(f"Could not check position while UNPROTECTED ({e}) - will retry next tick.")
            return

        if pos is None:
            self._log("Position is already flat (closed externally while unprotected) - recovering to IDLE.")
            await self._notify(tg.notify_exit(self.account_name, self.symbol, direction,
                                               "external_while_unprotected", entry_price or 0.0, None,
                                               self.pc.telegram_enabled))
            await self._record_close(direction, qty, entry_price, None, "external_while_unprotected")
            self._reset_position_state()
            return

        # BASE V3: re-place the SAME fixed stop the trade was opened with -
        # never a recalculated one (prompt Part 20). Only if no stop was ever
        # recorded for this position (e.g. a position adopted at startup with
        # no saved state) is one computed, from the saved balance-at-entry if
        # available, otherwise from the current available balance - logged
        # loudly, because protecting a real position beats leaving it naked.
        p = _params_from_pair(self.pc)
        try:
            target = self.state.stop_target
            if target is None:
                persisted = position_state.load_position_state(self.account_id, self.symbol)
                if persisted and position_state.matches_resumed_position(persisted, direction, entry_price):
                    self._restore_trade_state(persisted)
                    target = self.state.stop_target
            if target is None:
                basis = self.state.equity_at_entry
                if not basis:
                    basis = await self.client.get_available_balance()
                    self._log(f"No saved fixed stop and no saved balance-at-entry for this position - "
                              f"computing a protective stop from the CURRENT available balance "
                              f"({basis:.2f}) as a best effort.")
                    self.state.equity_at_entry = basis
                target = strat.stop_price(direction, entry_price, basis, self.state.bot_qty or qty, p)
                self.state.stop_target = target
                self._persist_trade_state()
            sl_order, sl_price = await self._place_stop_order(direction, target)
        except StopAlreadyCrossedError as crossed:
            self._log(f"The fixed stop {crossed.requested} is already crossed (mark {crossed.mark}) - "
                      f"the stop has effectively been hit; flattening.")
        except Exception as e:
            self._log(f"Retry to protect position also failed ({e}) - attempting emergency flatten.")
        else:
            self.state.status = "IN_POSITION"
            self.state.sl_price = sl_price
            self.state.sl_order_id = sl_order.get("algoId")
            self._log(f"Recovered - fixed stop now resting at {sl_price}.")
            await self._notify(tg.notify_sl_update(self.account_name, self.symbol, direction, sl_price,
                                                   False, self.pc.telegram_enabled))
            return

        # Placing protection failed again - flatten rather than leave it naked.
        # Re-fetch the REAL position size right before closing (2026-09-14
        # fix, item 10) rather than reusing `pos` from the top of this
        # function or the cached state.qty - either could be stale by now,
        # since placing protection just above may have taken a retry or two.
        close_side = "SELL" if direction == "LONG" else "BUY"
        try:
            fresh_pos = await self.client.get_position_risk(self.symbol)
        except Exception as e:
            self._log(f"Could not re-fetch position size before emergency close ({e}) - "
                      f"using the last-known cached qty ({qty}).")
            fresh_pos = "unknown"
        if fresh_pos is None:
            self._log("Position already flat by the time of the emergency close attempt - "
                      "recovering to IDLE without sending a close.")
            await self._notify(tg.notify_exit(self.account_name, self.symbol, direction,
                                               "external_while_unprotected", entry_price or 0.0, None,
                                               self.pc.telegram_enabled))
            await self._record_close(direction, qty, entry_price, None, "external_while_unprotected")
            self._reset_position_state()
            return
        if fresh_pos != "unknown":
            real_qty = abs(float(fresh_pos["positionAmt"]))
            if abs(real_qty - qty) > 1e-9:
                self._log(f"Real position size ({real_qty}) differs from cached ({qty}) - "
                          f"using the real exchange quantity for the emergency close.")
            qty = real_qty
        await self._cancel_own_orders()
        exit_order_id = None
        try:
            order = await self.client.close_position_market(self.symbol, close_side, qty)
            exit_price = float(order.get("avgPrice") or 0) or None
            exit_order_id = order.get("orderId")
            self._clear_pending_reconciliation_on_success(order.get("clientOrderId"))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self._log(f"Emergency close request failed ({e}) - checking exchange truth before deciding.")
            await self._flag_and_notify_pending_reconciliation(
                context="emergency close (UNPROTECTED)", client_order_id=getattr(e, "client_order_id", "unknown"),
                detail=str(e),
            )
            exit_price = None
        except Exception as e:
            self._log(f"Emergency close request failed ({e}) - checking exchange truth before deciding.")
            exit_price = None

        # Idempotent: trust the exchange, not the response object. Even if the
        # close request above raised, the exchange may have accepted it anyway
        # (or it may genuinely still be open) - re-query rather than guess.
        try:
            still_open = await self.client.get_position_risk(self.symbol)
        except Exception as e:
            self._log(f"Could not confirm emergency close via position query ({e}) - retrying next tick.")
            return
        if still_open is not None:
            self._log("Position still shows open after emergency close attempt - retrying next tick.")
            await self._notify(tg.notify_error(
                self.account_name, self.symbol,
                "Emergency close did not confirm flat yet, still unprotected, retrying.",
                self.pc.telegram_enabled,
            ))
            return  # stays UNPROTECTED - _tick will call this again next cycle

        pnl = None
        if exit_price and entry_price:
            sign = 1 if direction == "LONG" else -1
            pnl = sign * (exit_price - entry_price) * qty
        self._log(f"Emergency close confirmed flat @ {exit_price}.")
        await self._notify(tg.notify_exit(self.account_name, self.symbol, direction,
                                           "emergency_close_unprotected", exit_price or 0.0, pnl,
                                           self.pc.telegram_enabled))
        try:
            await self._record_close(direction, qty, exit_price, pnl, "emergency_close_unprotected",
                                      exit_order_id=exit_order_id)
        except Exception as e:
            self._log(f"Ledger record failed ({e}) - position already confirmed flat, continuing.")
        self._reset_position_state()

    # ---------------------------------------------------------------- managing an open position
    async def _manage_open_position(self, snap: strat.IndicatorSnapshot, p: strat.StrategyParams):
        """BASE V3, once per newly closed candle while in a position - same
        order as the Pine script:
          1. TP1 / TP2 tracking (normal high/low vs the levels set at entry).
             Tracking only - nothing is ever closed at TP1/TP2.
          2. Opposite signal:
               held by the Hold Rule (open profit > 0 and ADX < level) -> keep
               otherwise -> close at this candle (flip). The new side is NOT
               opened here; it can only open on the next candle through the
               normal flat-entry path, if the signal is still valid then.
        The fixed stop is never touched here (no trailing, no ATR moves, no
        EMA force-close, no max-loss cap - none of those exist in Base V3)."""
        direction = self.state.direction
        entry_price = self.state.entry_price

        t1, t2, new1, new2 = strat.update_tp_touched(
            direction, snap.high, snap.low, self.state.tp1_price, self.state.tp2_price,
            self.state.tp1_touched, self.state.tp2_touched)
        if new1 or new2:
            self.state.tp1_touched, self.state.tp2_touched = t1, t2
            self._persist_trade_state()
            if new1:
                self._log(f"TP1 tracker touched ({self.state.tp1_price}) - tracking only, position stays open.")
            if new2:
                self._log(f"TP2 tracker touched ({self.state.tp2_price}) - tracking only, position stays open.")

        action = strat.in_position_action(direction, snap, entry_price, p)
        if action == "HOLD":
            self.state.held_signal_count += 1
            self.state.last_decision = (f"HOLD - opposite signal ignored (in profit, ADX "
                                        f"{snap.adx:.1f} < {p.hold_adx_level})")
            self._log(f"Opposite signal while {direction} - HELD by the Hold Rule "
                      f"(close {snap.close} vs entry {entry_price}, ADX {snap.adx:.2f} < {p.hold_adx_level}).")
            return
        if action == "CLOSE":
            self.state.last_decision = f"CLOSE {direction} - opposite signal (flip)"
            self._log(f"Opposite signal while {direction} - not held (close {snap.close} vs entry "
                      f"{entry_price}, ADX {snap.adx:.2f}) - closing at this candle. The new side can "
                      f"only open on the next candle if its signal is still valid.")
            await self._close_position(reason="signal_flip")
            return
        self.state.last_decision = f"IN {direction} - no opposite signal"

    async def _close_position(self, reason: str):
        """Kicks off a close and hands off to _retry_close, which is idempotent
        and re-invoked by _tick every cycle (not gated on the next candle) until
        exchange truth actually confirms the position is flat - see F-05 in the
        code review this responds to: a close that fails or races with an
        existing SL/TP fill must never leave local state stuck as IN_POSITION
        while the exchange is already flat, or vice versa."""
        self.state.status = "CLOSING"
        self.state.closing_reason = reason
        await self._retry_close()

    async def _retry_close(self):
        direction = self.state.direction
        entry_price = self.state.entry_price
        reason = self.state.closing_reason or "close"
        if not direction or not self.state.qty:
            self.state.status = "IDLE"
            return

        # Re-fetch the REAL position size right before closing, rather than
        # trusting the locally-cached state.qty (2026-09-14 fix, per a
        # third-party review): a partial fill, a manual change, or an
        # external event could have altered the real quantity since it was
        # last cached. This same query also tells us if the position is
        # ALREADY flat, in which case there's nothing left to close at all -
        # avoiding a second, unnecessary close attempt.
        qty = self.state.qty
        try:
            pos = await self.client.get_position_risk(self.symbol)
        except Exception as e:
            self._log(f"Could not fetch the real position size before closing ({e}) - "
                      f"using the last-known cached qty ({qty}); will retry next tick regardless.")
            pos = "unknown"

        exit_price = None
        exit_order_id = None
        if pos is None:
            self._log("Position already shows flat on the exchange - finalizing without "
                      "another close attempt.")
        else:
            if pos != "unknown":
                real_qty = abs(float(pos["positionAmt"]))
                if abs(real_qty - qty) > 1e-9:
                    self._log(f"Real position size ({real_qty}) differs from cached ({qty}) - "
                              f"using the real exchange quantity for this close.")
                qty = real_qty
            await self._cancel_own_orders()
            try:
                close_side = "SELL" if direction == "LONG" else "BUY"
                order = await self.client.close_position_market(self.symbol, close_side, qty)
                exit_price = float(order.get("avgPrice") or 0) or None
                exit_order_id = order.get("orderId")
                self._clear_pending_reconciliation_on_success(order.get("clientOrderId"))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # Item 9 fix (2026-09-14, per a third-party review):
                # close_position_market already tried its own ambiguous-
                # response recovery and STILL couldn't resolve it - flag it
                # durably rather than only logging, since nobody else is
                # tracking whether this close actually happened.
                self._log(f"Close request failed/ambiguous ({e}) - checking exchange truth before deciding.")
                await self._flag_and_notify_pending_reconciliation(
                    context="position close", client_order_id=getattr(e, "client_order_id", "unknown"),
                    detail=str(e),
                )
            except Exception as e:
                self._log(f"Close request failed/ambiguous ({e}) - checking exchange truth before deciding.")

        # Idempotent: never reset local state from the response object alone -
        # only from a confirmed-flat position query. If the exchange still
        # shows the position open (close rejected, raced with an SL/TP fill,
        # partial fill, etc.), stay in CLOSING and retry next tick.
        try:
            still_open = await self.client.get_position_risk(self.symbol)
        except Exception as e:
            self._log(f"Could not confirm close via position query ({e}) - retrying next tick.")
            return
        if still_open is not None:
            self._log("Position still shows open after close attempt - retrying next tick.")
            return

        if exit_price is None:
            # The close request's response was lost/ambiguous, but the
            # position is NOW confirmed flat - the close itself was safe
            # (never guessed from the response alone), but the ledger would
            # otherwise be left with no exit price/PnL for a trade that
            # genuinely closed. Try to recover the REAL fill from Binance's
            # own trade records first (most accurate), falling back to the
            # current mark price only if that's unavailable too - still
            # better than leaving the ledger blank, though clearly less
            # precise than a real fill, so this order only tries it as a
            # second resort.
            try:
                recent_trades = await self.client.get_user_trades(self.symbol, limit=5)
                if recent_trades:
                    last_trade = recent_trades[-1]
                    exit_price = float(last_trade.get("price") or 0) or None
                    if exit_order_id is None:
                        recovered_id = last_trade.get("orderId")
                        if recovered_id:
                            try:
                                exit_order_id = int(recovered_id)
                            except (TypeError, ValueError):
                                pass
            except Exception as e:
                self._log(f"Could not recover the real fill price after an ambiguous close ({e}) - "
                          f"falling back to a mark-price estimate for the ledger.")
            if exit_price is None:
                try:
                    exit_price = await self.client.get_mark_price(self.symbol)
                except Exception:
                    pass  # exit_price stays None - _record_ledger already handles that safely

        pnl = None
        if exit_price and entry_price:
            sign = 1 if direction == "LONG" else -1
            pnl = sign * (exit_price - entry_price) * qty
        self._log(f"Closed {direction} position ({reason}) qty {qty} @ {exit_price}")
        await self._notify(tg.notify_exit(self.account_name, self.symbol, direction, reason,
                                           exit_price or 0.0, pnl, self.pc.telegram_enabled))
        try:
            await self._record_close(direction, qty, exit_price, pnl, reason,
                                      exit_order_id=exit_order_id)
        except Exception as e:
            self._log(f"Ledger record failed ({e}) - position already confirmed flat, continuing.")
        self._reset_position_state()

        # BASE V3: a flip only CLOSES on the signal candle. No re-entry here -
        # the next closed candle is evaluated through the normal flat-entry
        # path (exactly like the Pine script's next-bar entry).

    async def _handle_position_closed_externally(self):
        """The position disappeared between ticks (REST poll noticed it) -
        the fixed stop triggered, or someone closed it on the exchange (or
        it was liquidated). This is the SAFETY-NET path; the fast path is
        _on_algo_update. Both converge on _finalize_external_close, which is
        idempotent. Queries the actual algo status of our own stop first -
        real fill data beats guessing from mark price."""
        sl_price = self.state.sl_price
        reason, exit_price = None, None
        matched_order_id = None  # a REGULAR order id (from actualOrderId), not an algoId
        algo_id = self.state.sl_order_id
        if algo_id:
            try:
                o = await self.client.get_algo_order(algo_id)
            except Exception:
                o = {}
            if o.get("algoStatus") in ("TRIGGERED", "FINISHED"):
                reason = "SL"
                actual_order_id = o.get("actualOrderId")
                if actual_order_id:
                    try:
                        matched_order_id = int(actual_order_id)
                    except (TypeError, ValueError):
                        matched_order_id = None
                fill_price = float(o.get("actualPrice") or 0) or float(o.get("triggerPrice") or 0) or None
                if fill_price:
                    exit_price = fill_price

        if reason is None:
            # Not confirmed as our stop (manual close, liquidation, API hiccup):
            # use the real last fill if available, else mark price - for the
            # ledger/tracker only; never affects any trading decision.
            reason = "external"
            try:
                recent = await self.client.get_user_trades(self.symbol, limit=5)
                if recent:
                    exit_price = float(recent[-1].get("price") or 0) or None
            except Exception:
                pass
            if exit_price is None:
                try:
                    exit_price = await self.client.get_mark_price(self.symbol)
                except Exception:
                    exit_price = None
            exit_price = exit_price or sl_price or 0.0

        await self._finalize_external_close(reason, exit_price, matched_order_id, source="poll")

    async def _on_algo_update(self, o: dict):
        """Real-time handler for Binance's ALGO_UPDATE push event (see
        user_data_stream.py) - THIS is the fast path for noticing an SL/TP
        fill, reacting the instant Binance pushes it rather than waiting up
        to ~15s for the next REST poll. Field names here are the WebSocket
        shorthand keys, confirmed against Binance's own raw JSON example -
        genuinely different from the REST Algo Order API's field names for
        the same concepts (aid not algoId, X not algoStatus, ai/ap not
        actualOrderId/actualPrice)."""
        if self.state.status != "IN_POSITION":
            return  # not currently tracking an open position - nothing to do
        algo_status = o.get("X")
        if algo_status not in ("TRIGGERED", "FINISHED"):
            return  # NEW/CANCELED/REJECTED/etc. - not a fill
        algo_id = o.get("aid")
        if algo_id == self.state.sl_order_id:
            reason = "SL"
        else:
            return  # not our fixed stop (Base V3 places no other algo orders) - ignore safely

        matched_order_id = None
        actual_order_id = o.get("ai")
        if actual_order_id:
            try:
                matched_order_id = int(actual_order_id)
            except (TypeError, ValueError):
                matched_order_id = None
        exit_price = float(o.get("ap") or 0) or None

        await self._finalize_external_close(reason, exit_price, matched_order_id, source="realtime")

    async def _on_order_update(self, o: dict):
        """Real-time handler for Binance's ORDER_TRADE_UPDATE push event -
        this bot's regular (MARKET) entry/close orders. Added 2026-09-14
        (item 6, per a third-party review): this event type was already
        registered on the shared stream but had no handler wired to it at
        all - dead code presented as "structural completeness" without
        actual use. Now gives it a real, concrete purpose: captures REAL
        commission the moment a fill happens, so _fetch_actual_commission
        can use already-known data instead of a separate REST call when
        available (falling back to that REST call exactly as before for
        anything not seen here - this is a cache, not a replacement).

        Field names are the classic, long-stable ORDER_TRADE_UPDATE
        shorthand keys: `c` (clientOrderId), `x` (execution type - "TRADE"
        specifically means a fill occurred, as opposed to `X`, the order's
        overall status), `i` (orderId), `n`/`N` (commission/asset for that
        specific fill)."""
        client_order_id = str(o.get("c", ""))
        if not client_order_id.startswith(self.client.client_order_id_prefix):
            return  # not our own order - never touch/track anything not tagged as ours
        if o.get("x") != "TRADE":
            return  # NEW/CANCELED/EXPIRED/etc. - no fill, nothing to record
        order_id = o.get("i")
        if not order_id:
            return
        try:
            commission = float(o.get("n") or 0)
        except (TypeError, ValueError):
            return
        # Accumulate rather than overwrite - a single order can receive
        # multiple TRADE events for partial fills, each with its own
        # commission for that specific fill.
        self._commission_cache[int(order_id)] = self._commission_cache.get(int(order_id), 0.0) + commission

    async def _on_stream_unhealthy(self, message: str):
        """2026-09-15 fix (flagged across two review rounds): a websocket
        reconnect used to only ever be logged, never escalated - a
        genuinely sustained outage (not a single blip) could go unnoticed
        unless someone happened to be watching logs. Wired to
        UserDataStream's on_unhealthy callback (fires once per sustained
        streak, not on every retry - see RECONNECT_ALERT_THRESHOLD)."""
        await self._notify(tg.notify_error(self.account_name, self.symbol, message, self.pc.telegram_enabled))

    async def _finalize_external_close(self, reason: str, exit_price: float | None,
                                        matched_order_id: int | None, source: str):
        """Shared by both the real-time event path (_on_algo_update) and the
        REST-polling safety net (_handle_position_closed_externally) -
        whichever notices the closure first does the work; the other is a
        safe no-op. The IN_POSITION check-and-clear happens with no `await`
        between them, which is what makes this safe under asyncio's
        cooperative (single-threaded, no true preemption) scheduling - by
        the time either path reaches its first `await` here, the other
        path (if it runs concurrently) will already see the status has
        moved on and return immediately above.

        Confirms exchange-flat before actually finalizing (fixed 2026-09-14,
        per a third-party review): the real-time path only knows an
        ALGO_UPDATE said TRIGGERED/FINISHED - unlike the polling path, whose
        own calling condition already confirms get_position_risk() is None
        before this is ever called, the event path had no independent
        confirmation of its own. If the exchange doesn't yet agree the
        position is flat, this reverts to IN_POSITION (not CLOSING - there's
        no bot-initiated close order in flight to retry here, unlike
        _retry_close's use case) and lets the next tick's normal poll (or a
        later, more conclusive event) pick it up fresh, rather than forcing
        a close or trusting the event alone."""
        if self.state.status != "IN_POSITION":
            return
        self.state.status = "CLOSING"

        try:
            still_open = await self.client.get_position_risk(self.symbol)
        except Exception as e:
            self._log(f"Could not confirm exchange-flat after a {reason} event ({e}) - "
                      f"reverting to IN_POSITION; the next tick's poll will re-check.")
            self.state.status = "IN_POSITION"
            return
        if still_open is not None:
            # Item 5 fix (2026-09-14, per a third-party review): the exchange
            # still shows SOME position, but it may be a SMALLER remaining
            # amount than what's locally cached - a partial fill/close, not
            # "nothing happened yet". Previously this just reverted to
            # IN_POSITION unconditionally, silently discarding that
            # information. Now compares the real remaining quantity against
            # the cached one and updates state.qty to match reality when
            # they differ, so PnL/exposure/future closing decisions are all
            # based on what's actually left - not a stale, larger number.
            # The resting SL/TP still correctly cover whatever remains
            # either way, since closePosition=true always closes the entire
            # CURRENT position regardless of size - no resizing needed there.
            real_remaining_qty = abs(float(still_open.get("positionAmt", 0)))
            if real_remaining_qty > 0 and abs(real_remaining_qty - self.state.qty) > 1e-9:
                self._log(f"{reason} event received (via {source}) - the exchange now shows a "
                          f"SMALLER remaining position ({real_remaining_qty}, was "
                          f"{self.state.qty}) - a partial fill/close happened. Updating local "
                          f"qty to match reality; continuing to manage the remaining position.")
                await self._notify(tg.notify_error(
                    self.account_name, self.symbol,
                    f"Partial {reason}: position size reduced from {self.state.qty} to "
                    f"{real_remaining_qty} - continuing to manage the remainder.",
                    self.pc.telegram_enabled,
                ))
                self.state.qty = real_remaining_qty
                self.state.status = "IN_POSITION"
                return
            self._log(f"{reason} event received (via {source}) but the exchange still shows an "
                      f"open position - not finalizing yet; reverting to IN_POSITION for the next "
                      f"tick to re-check.")
            self.state.status = "IN_POSITION"
            return

        direction = self.state.direction
        qty = self.state.qty
        entry_price = self.state.entry_price
        if not exit_price:
            exit_price = self.state.sl_price

        self._log(f"Position closed externally (confirmed {reason}, via {source}).")
        await self._cancel_own_orders()
        pnl = None
        if exit_price and entry_price and qty:
            sign = 1 if direction == "LONG" else -1
            pnl = sign * (exit_price - entry_price) * qty
        await self._notify(tg.notify_exit(self.account_name, self.symbol, direction or "?",
                                           reason, exit_price, pnl, self.pc.telegram_enabled))
        try:
            await self._record_close(direction, qty, exit_price, pnl, reason,
                                      confirmed=(reason == "SL"), exit_order_id=matched_order_id)
        except Exception as e:
            self._log(f"Ledger record failed ({e}) - position already confirmed flat, continuing.")
        self._reset_position_state()

    async def _fetch_actual_commission(self, order_ids: list[int | None]) -> float | None:
        """Sums REAL commission Binance charged (in quote-asset terms) across
        the given order ids. Checks the real-time commission cache first
        (populated by _on_order_update from ORDER_TRADE_UPDATE push events -
        see item 6, 2026-09-14) - only falls back to a REST get_user_trades
        call for any order id NOT already known from that cache. Never
        estimated or simulated either way. Returns None (not 0.0) if it
        can't be determined for ANY of the orders, so callers can tell
        "confirmed zero fees" apart from "couldn't find out" and must not
        substitute a guess in the latter case."""
        total = 0.0
        found_any = False
        for oid in order_ids:
            if not oid:
                continue
            cached = self._commission_cache.get(oid)
            if cached is not None:
                total += cached
                found_any = True
                continue
            try:
                fills = await self.client.get_user_trades(self.symbol, order_id=oid)
            except Exception as e:
                self._log(f"Could not fetch real commission for order {oid}: {e}")
                continue
            for f in fills:
                # USDⓈ-M futures commission is charged in the contract's
                # margin asset (USDT for the USDT-margined symbols this bot
                # targets) - not converting other commissionAsset values.
                try:
                    total += float(f.get("commission", 0))
                    found_any = True
                except (TypeError, ValueError):
                    continue
        return total if found_any else None

    async def _record_ledger(self, direction, qty, exit_price, pnl, reason, confirmed: bool = True,
                              exit_order_id: int | None = None):
        """Writes the trade to the ledger. Returns the PnL actually recorded
        (NET of real commission when it could be fetched, otherwise gross),
        or None if nothing could be recorded."""
        if not direction or not qty or not self.state.entry_price:
            return None
        commission = await self._fetch_actual_commission([self.state.entry_order_id, exit_order_id])
        commission_included = commission is not None
        net_pnl = pnl
        if commission_included and pnl is not None:
            net_pnl = pnl - commission
        elif not commission_included:
            self._log("Could not fetch real commission for this trade - ledger PnL is GROSS "
                      "(pre-fees), not net. Never estimated as a substitute.")
        ledger.record_trade(ledger.TradeRecord(
            account_id=self.account_id, symbol=self.symbol, direction=direction,
            qty=qty, entry_price=self.state.entry_price, exit_price=exit_price or 0.0,
            pnl=net_pnl, reason=reason, confirmed=confirmed,
            opened_at=self.state.opened_at or 0.0, closed_at=time.time(),
            commission=commission, commission_included=commission_included,
        ))
        return net_pnl

    async def _record_close(self, direction, qty, exit_price, pnl, reason, confirmed: bool = True,
                            exit_order_id: int | None = None):
        """Every REAL close goes through here: ledger first, then the Base V3
        tracker. Each is independent - a failure in one never blocks the
        other, and neither can ever affect the (already confirmed) close."""
        net_pnl = pnl
        try:
            recorded = await self._record_ledger(direction, qty, exit_price, pnl, reason,
                                                 confirmed=confirmed, exit_order_id=exit_order_id)
            if recorded is not None:
                net_pnl = recorded
        except Exception as e:
            self._log(f"Ledger record failed ({e}) - position already confirmed flat, continuing.")
        try:
            await self._update_tracker_on_close(direction, qty, exit_price, net_pnl, reason)
        except Exception as e:
            self._log(f"Tracker update failed ({e}) - the real close is unaffected; check the tracker tab.")

    async def _update_tracker_on_close(self, direction, qty, exit_price, net_pnl, reason):
        """Applies a REAL closed trade to the Base V3 tracker by its %
        result (net PnL of the bot's own share / available balance at
        entry). Deposits, withdrawals and manual size changes never reach
        the tracker. Starts shadow mode on a winning new high."""
        if net_pnl is None:
            self._log(f"Tracker not updated for this close ({reason}): exit price/PnL unknown.")
            return
        closed_qty = qty or 0.0
        bot_qty = self.state.bot_qty or closed_qty
        share = (min(bot_qty, closed_qty) / closed_qty) if closed_qty else 1.0
        bot_pnl = net_pnl * share
        basis = self.state.equity_at_entry
        approximated = False
        if not basis:
            approximated = True
            try:
                basis = (await self.client.get_available_balance()) - bot_pnl
            except Exception:
                basis = None
        if not basis or basis <= 0:
            self._log(f"Tracker not updated for this close ({reason}): balance at entry unknown.")
            return
        result_pct = bot_pnl / basis * 100.0
        p = _params_from_pair(self.pc)
        started = tracker_mod.apply_real_close(
            self.tracker, result_pct=result_pct, pnl=bot_pnl, direction=direction,
            entry_price=self.state.entry_price, exit_price=exit_price, reason=reason,
            balance_at_entry=basis, use_shadow=p.use_shadow, approximated=approximated)
        tracker_mod.save(self.tracker)
        self._log(f"Tracker: {result_pct:+.3f}% -> {self.tracker.balance:,.2f} "
                  f"(high {self.tracker.peak:,.2f}){' [approximated basis]' if approximated else ''}.")
        if started:
            msg = (f"New tracker high after a winning trade - SHADOW MODE ON: the next "
                   f"{p.shadow_trades} trades are paper only (no real orders).")
            self._log(msg)
            await self._notify(tg.notify_info(self.account_name, self.symbol, msg,
                                              self.pc.telegram_enabled, email_kind="Shadow Started"))

    def _reset_position_state(self):
        self.state.status = "IDLE"
        self.state.direction = None
        self.state.entry_price = None
        self.state.qty = None
        self.state.sl_price = None
        self.state.entry_order_id = None
        self.state.sl_order_id = None
        self.state.opened_at = None
        self.state.closing_reason = None
        self.state.equity_at_entry = None
        self.state.stop_target = None
        self.state.entry_atr = None
        self.state.tp1_price = None
        self.state.tp2_price = None
        self.state.tp1_touched = False
        self.state.tp2_touched = False
        self.state.alloc_pct = None
        self.state.leverage = None
        self.state.bot_qty = None
        self.state.entry_candle_time = None
        self.state.held_signal_count = 0
        self._topup_alerted = False
        # Must never linger past its own trade's lifetime - see
        # position_state.py's own docstring on why a stale file here would
        # be actively dangerous (a future restart could mistake it for the
        # NEXT trade's state).
        position_state.clear_position_state(self.account_id, self.symbol)
        # Clear the real-time commission cache too, so it never grows
        # unbounded and never leaks into a future, unrelated trade.
        self._commission_cache.clear()

    # ---------------------------------------------------------------- serialization for the API
    def to_status_dict(self) -> dict:
        # Item 9 fix (2026-09-14): surface any pending-reconciliation flag
        # directly in the status the dashboard reads, not just Telegram/logs
        # - a human checking the dashboard should see this immediately, not
        # only if they happened to catch the notification.
        pending = reconciliation.get_pending_reconciliations(self.account_id, self.symbol)
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "symbol": self.symbol,
            "status": self.state.status,
            "direction": self.state.direction,
            "entry_price": self.state.entry_price,
            "qty": self.state.qty,
            "sl_price": self.state.sl_price,
            "strategy": strat.STRATEGY_NAME,
            "base_v3": {
                "fixed_stop": self.state.sl_price,
                "stop_requested": self.state.stop_target,
                "balance_at_entry": self.state.equity_at_entry,
                "allocation_pct": self.state.alloc_pct,
                "entry_atr": self.state.entry_atr,
                "tp1_price": self.state.tp1_price, "tp1_touched": self.state.tp1_touched,
                "tp2_price": self.state.tp2_price, "tp2_touched": self.state.tp2_touched,
                "bot_qty": self.state.bot_qty,
                "held_signal_count": self.state.held_signal_count,
                "last_decision": self.state.last_decision,
                "tracker": self.tracker.summary(),
            },
            "feed_stale": self.state.feed_stale,
            "opened_at": self.state.opened_at,
            "last_error": self.state.last_error,
            "indicators": self.state.last_indicators,
            "updated_at": self.state.updated_at,
            "logs": list(self.logs)[-50:],
            # Plural, as of the 2026-09-14 redesign - a symbol can now have
            # more than one genuinely unresolved order at once, and each is
            # shown rather than only the most recently flagged one.
            "pending_reconciliations": [
                {"context": p.context, "client_order_id": p.client_order_id,
                 "detail": p.detail, "flagged_at": p.flagged_at}
                for p in pending
            ],
            "config_update_pending": self._pending_restart_on_flat,
        }
